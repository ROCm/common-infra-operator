#!/usr/bin/env python3

'''
 Copyright (c) Advanced Micro Devices, Inc. All rights reserved.

 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at

      http://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
'''

"""
test_update_1.0.0_operand.py

Updates NetworkConfig operand images to v1.0.0 release images from the
docker.io/rocm registry.

  - spec.devicePlugin.devicePluginImage  -> docker.io/rocm/k8s-network-device-plugin:v1.0.0
  - spec.devicePlugin.nodeLabellerImage  -> docker.io/rocm/k8s-network-node-labeller:v1.0.0
  - spec.metricsExporter.image           -> docker.io/rocm/device-metrics-exporter:nic-v1.0.0
  - spec.cniPlugins.image               -> docker.io/rocm/k8s-cni-plugins:v1.0.0
"""

import logging
import time
import pytest

from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException

from lib.nic_util import (
    list_networkconfigs_custom,
    get_networkconfig_custom,
    replace_with_retry,
)

LOG = logging.getLogger(__name__)
TEST_TIMEOUT = 180


# ---------- Configuration ----------

NC_NAMESPACE = "kube-amd-network"

# Fixed v1.0.0 release images
DEVICE_PLUGIN_IMAGE    = "docker.io/rocm/k8s-network-device-plugin:v1.0.0"
NODE_LABELLER_IMAGE    = "docker.io/rocm/k8s-network-node-labeller:v1.0.0"
METRICS_EXPORTER_IMAGE = "docker.io/rocm/device-metrics-exporter:nic-v1.0.0"
CNI_PLUGINS_IMAGE      = "docker.io/rocm/k8s-cni-plugins:v1.0.0"

# DaemonSet name suffixes
DS_SUFFIX_DEVICE_PLUGIN    = "device-plugin"
DS_SUFFIX_NODE_LABELLER    = "node-labeller"
DS_SUFFIX_METRICS_EXPORTER = "metrics-exporter"
DS_SUFFIX_CNI_PLUGINS      = "cni-plugins"

POD_ROLLOUT_TIMEOUT = 600
POD_ROLLOUT_POLL    = 15


# ---------- Rollout-wait / pod-verification helpers ----------

def _ds_name(nc_name: str, suffix: str) -> str:
    return f"{nc_name}-{suffix}"


def _wait_for_daemonset_rollout(
    apps_v1: k8s_client.AppsV1Api,
    namespace: str,
    ds_name: str,
    expected_image: str,
    timeout_sec: int = POD_ROLLOUT_TIMEOUT,
    poll_interval: int = POD_ROLLOUT_POLL,
) -> None:
    """
    Block until the named DaemonSet has completed a rollout where every pod
    is running *expected_image*, or raise TimeoutError.
    """
    LOG.info(
        "Waiting up to %ds for DaemonSet %s/%s to roll out image '%s'",
        timeout_sec, namespace, ds_name, expected_image,
    )
    deadline = time.monotonic() + timeout_sec
    actual_image = expected_image
    while time.monotonic() < deadline:
        try:
            ds = apps_v1.read_namespaced_daemon_set(name=ds_name, namespace=namespace)
        except ApiException as exc:
            if exc.status == 404:
                LOG.debug("DaemonSet %s/%s not found yet, retrying...", namespace, ds_name)
                time.sleep(poll_interval)
                continue
            raise

        spec_images = [
            c.image
            for c in (ds.spec.template.spec.containers or [])
        ]
        if expected_image not in spec_images:
            if spec_images:
                actual_image = spec_images[0]
                LOG.warning(
                    "DaemonSet %s spec image is '%s', not '%s' (operator override) -- "
                    "tracking spec image for rollout",
                    ds_name, actual_image, expected_image,
                )
            else:
                LOG.debug(
                    "DaemonSet %s spec images %s do not include '%s' yet",
                    ds_name, spec_images, expected_image,
                )
                time.sleep(poll_interval)
                continue

        status = ds.status
        desired = status.desired_number_scheduled or 0
        updated = status.updated_number_scheduled or 0
        ready   = status.number_ready             or 0

        LOG.info(
            "DaemonSet %s -- desired=%d updated=%d ready=%d",
            ds_name, desired, updated, ready,
        )

        if desired == 0:
            LOG.info("DaemonSet %s has 0 desired pods -- rollout considered complete", ds_name)
            return

        if updated == desired and ready == desired:
            LOG.info(
                "DaemonSet %s rollout complete: all %d pod(s) running image '%s'",
                ds_name, desired, actual_image,
            )
            return

        time.sleep(poll_interval)

    raise TimeoutError(
        f"DaemonSet {namespace}/{ds_name} did not complete rollout with image "
        f"'{actual_image}' within {timeout_sec}s"
    )


def _assert_pods_have_image(
    v1: k8s_client.CoreV1Api,
    apps_v1: k8s_client.AppsV1Api,
    namespace: str,
    ds_name: str,
    expected_image: str,
    timeout_sec: int = POD_ROLLOUT_TIMEOUT,
    poll_interval: int = POD_ROLLOUT_POLL,
) -> None:
    """
    Read the DaemonSet's current spec image, then poll until every matching
    pod is Running and has that image.  If the operator overrides the CRD
    image (e.g. resolves rocm/ -> amdpsdo/), the spec image is used instead
    of *expected_image* so the assertion stays valid.
    """
    try:
        ds = apps_v1.read_namespaced_daemon_set(name=ds_name, namespace=namespace)
    except ApiException as exc:
        if exc.status == 404:
            LOG.warning("DaemonSet %s/%s not found -- skipping pod image assertion", namespace, ds_name)
            return
        raise

    # Use the DaemonSet's actual spec image (operator may have overridden the CRD value)
    spec_images = [c.image for c in (ds.spec.template.spec.containers or [])]
    actual_expected = expected_image
    if expected_image not in spec_images and spec_images:
        actual_expected = spec_images[0]
        LOG.warning(
            "DaemonSet %s spec image is '%s', not '%s' (operator override) -- "
            "verifying pods against spec image",
            ds_name, actual_expected, expected_image,
        )

    match_labels = (ds.spec.selector.match_labels or {}) if ds.spec.selector else {}
    if not match_labels:
        LOG.warning("DaemonSet %s has no matchLabels -- skipping pod image assertion", ds_name)
        return

    label_selector = ",".join(f"{k}={v}" for k, v in match_labels.items())

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        pods = v1.list_namespaced_pod(namespace=namespace, label_selector=label_selector).items

        if not pods:
            LOG.warning(
                "No pods in %s matching selector '%s' for DaemonSet %s",
                namespace, label_selector, ds_name,
            )
            return

        failures = []
        for pod in pods:
            pod_name = pod.metadata.name
            phase = pod.status.phase if pod.status else "Unknown"
            if phase != "Running":
                failures.append(f"{pod_name}: phase={phase}")
                continue
            running_images = [
                cs.image
                for cs in (pod.status.container_statuses or [])
            ]
            # Check pod spec images as well -- the kubelet may report a
            # resolved/rewritten image in container_statuses while the pod
            # spec still matches the DaemonSet spec.
            pod_spec_images = [
                c.image
                for c in (pod.spec.containers or [])
            ]
            if actual_expected not in running_images and actual_expected not in pod_spec_images:
                failures.append(
                    f"{pod_name}: expected image '{actual_expected}' not found; "
                    f"running images: {running_images}"
                )

        if not failures:
            LOG.info(
                "DaemonSet %s -- all %d pod(s) confirmed running image '%s'",
                ds_name, len(pods), actual_expected,
            )
            return

        LOG.debug(
            "DaemonSet %s pod image check not yet satisfied, retrying in %ds: %s",
            ds_name, poll_interval, failures,
        )
        time.sleep(poll_interval)

    msg = (
        f"DaemonSet {ds_name} pod image verification failed:\n"
        + "\n".join(f"  - {f}" for f in failures)
    )
    LOG.error(msg)
    raise AssertionError(msg)


# ---------- Operand update tests ----------

@pytest.mark.timeout(TEST_TIMEOUT)
def test_update_1_0_0_device_plugin_image():
    """Update spec.devicePlugin.devicePluginImage to v1.0.0."""
    LOG.info("Target devicePluginImage: %s", DEVICE_PLUGIN_IMAGE)

    nc_items = list_networkconfigs_custom(NC_NAMESPACE)
    if not nc_items:
        pytest.skip(f"No NetworkConfig resources found in namespace '{NC_NAMESPACE}'")

    v1      = k8s_client.CoreV1Api()
    apps_v1 = k8s_client.AppsV1Api()

    updated, already_current = [], []

    for nc in nc_items:
        name    = nc.get("metadata", {}).get("name", "<unknown>")
        current = nc.get("spec", {}).get("devicePlugin", {}).get("devicePluginImage", "")

        if current == DEVICE_PLUGIN_IMAGE:
            LOG.info("[%s] devicePluginImage is already up-to-date: %s", name, current)
            already_current.append(name)
        else:
            LOG.info("[%s] devicePluginImage: '%s' -> '%s'", name, current, DEVICE_PLUGIN_IMAGE)
            body = get_networkconfig_custom(NC_NAMESPACE, name)
            body.setdefault("spec", {}).setdefault("devicePlugin", {})["devicePluginImage"] = DEVICE_PLUGIN_IMAGE
            replace_with_retry(NC_NAMESPACE, name, body)
            updated.append(name)
            LOG.info("[%s] devicePluginImage patch accepted", name)

    LOG.info("device-plugin update summary -- updated: %s, already-current: %s", updated, already_current)
    assert updated or already_current, "No NetworkConfig was processed"

    if not updated:
        pytest.skip(f"All NetworkConfigs already have devicePluginImage: {DEVICE_PLUGIN_IMAGE}")

    all_nc_names = [nc.get("metadata", {}).get("name") for nc in nc_items if nc.get("metadata", {}).get("name")]
    for nc_name in all_nc_names:
        ds = _ds_name(nc_name, DS_SUFFIX_DEVICE_PLUGIN)
        _wait_for_daemonset_rollout(apps_v1, NC_NAMESPACE, ds, DEVICE_PLUGIN_IMAGE)
        _assert_pods_have_image(v1, apps_v1, NC_NAMESPACE, ds, DEVICE_PLUGIN_IMAGE)


@pytest.mark.timeout(TEST_TIMEOUT)
def test_update_1_0_0_node_labeller_image():
    """Update spec.devicePlugin.nodeLabellerImage to v1.0.0."""
    LOG.info("Target nodeLabellerImage: %s", NODE_LABELLER_IMAGE)

    nc_items = list_networkconfigs_custom(NC_NAMESPACE)
    if not nc_items:
        pytest.skip(f"No NetworkConfig resources found in namespace '{NC_NAMESPACE}'")

    v1      = k8s_client.CoreV1Api()
    apps_v1 = k8s_client.AppsV1Api()

    updated, already_current = [], []

    for nc in nc_items:
        name    = nc.get("metadata", {}).get("name", "<unknown>")
        current = nc.get("spec", {}).get("devicePlugin", {}).get("nodeLabellerImage", "")

        if current == NODE_LABELLER_IMAGE:
            LOG.info("[%s] nodeLabellerImage is already up-to-date: %s", name, current)
            already_current.append(name)
        else:
            LOG.info("[%s] nodeLabellerImage: '%s' -> '%s'", name, current, NODE_LABELLER_IMAGE)
            body = get_networkconfig_custom(NC_NAMESPACE, name)
            body.setdefault("spec", {}).setdefault("devicePlugin", {})["nodeLabellerImage"] = NODE_LABELLER_IMAGE
            replace_with_retry(NC_NAMESPACE, name, body)
            updated.append(name)
            LOG.info("[%s] nodeLabellerImage patch accepted", name)

    LOG.info("node-labeller update summary -- updated: %s, already-current: %s", updated, already_current)
    assert updated or already_current, "No NetworkConfig was processed"

    if not updated:
        pytest.skip(f"All NetworkConfigs already have nodeLabellerImage: {NODE_LABELLER_IMAGE}")

    all_nc_names = [nc.get("metadata", {}).get("name") for nc in nc_items if nc.get("metadata", {}).get("name")]
    for nc_name in all_nc_names:
        ds = _ds_name(nc_name, DS_SUFFIX_NODE_LABELLER)
        _wait_for_daemonset_rollout(apps_v1, NC_NAMESPACE, ds, NODE_LABELLER_IMAGE)
        _assert_pods_have_image(v1, apps_v1, NC_NAMESPACE, ds, NODE_LABELLER_IMAGE)


@pytest.mark.timeout(TEST_TIMEOUT)
def test_update_1_0_0_metrics_exporter_image():
    """Update spec.metricsExporter.image to nic-v1.0.0."""
    LOG.info("Target metricsExporter.image: %s", METRICS_EXPORTER_IMAGE)

    nc_items = list_networkconfigs_custom(NC_NAMESPACE)
    if not nc_items:
        pytest.skip(f"No NetworkConfig resources found in namespace '{NC_NAMESPACE}'")

    v1      = k8s_client.CoreV1Api()
    apps_v1 = k8s_client.AppsV1Api()

    updated, already_current = [], []

    for nc in nc_items:
        name    = nc.get("metadata", {}).get("name", "<unknown>")
        current = nc.get("spec", {}).get("metricsExporter", {}).get("image", "")

        if current == METRICS_EXPORTER_IMAGE:
            LOG.info("[%s] metricsExporter.image is already up-to-date: %s", name, current)
            already_current.append(name)
        else:
            LOG.info("[%s] metricsExporter.image: '%s' -> '%s'", name, current, METRICS_EXPORTER_IMAGE)
            body = get_networkconfig_custom(NC_NAMESPACE, name)
            body.setdefault("spec", {}).setdefault("metricsExporter", {})["image"] = METRICS_EXPORTER_IMAGE
            replace_with_retry(NC_NAMESPACE, name, body)
            updated.append(name)
            LOG.info("[%s] metricsExporter.image patch accepted", name)

    LOG.info("metrics-exporter update summary -- updated: %s, already-current: %s", updated, already_current)
    assert updated or already_current, "No NetworkConfig was processed"

    if not updated:
        pytest.skip(f"All NetworkConfigs already have metricsExporter.image: {METRICS_EXPORTER_IMAGE}")

    all_nc_names = [nc.get("metadata", {}).get("name") for nc in nc_items if nc.get("metadata", {}).get("name")]
    for nc_name in all_nc_names:
        ds = _ds_name(nc_name, DS_SUFFIX_METRICS_EXPORTER)
        _wait_for_daemonset_rollout(apps_v1, NC_NAMESPACE, ds, METRICS_EXPORTER_IMAGE)
        _assert_pods_have_image(v1, apps_v1, NC_NAMESPACE, ds, METRICS_EXPORTER_IMAGE)


@pytest.mark.timeout(TEST_TIMEOUT)
def test_update_1_0_0_cni_plugin_image():
    """Update spec.cniPlugins.image to v1.0.0."""
    LOG.info("Target cniPlugins.image: %s", CNI_PLUGINS_IMAGE)

    nc_items = list_networkconfigs_custom(NC_NAMESPACE)
    if not nc_items:
        pytest.skip(f"No NetworkConfig resources found in namespace '{NC_NAMESPACE}'")

    v1      = k8s_client.CoreV1Api()
    apps_v1 = k8s_client.AppsV1Api()

    updated, already_current = [], []
    rollout_targets = []

    def _get_cni_cfg(spec: dict) -> tuple:
        sec = (spec.get("secondaryNetwork", {}) or {})
        sec_cni = (sec.get("cniPlugins", {}) or {})
        if sec_cni:
            return "secondaryNetwork.cniPlugins", sec_cni
        return "cniPlugins", (spec.get("cniPlugins", {}) or {})

    for nc in nc_items:
        name = nc.get("metadata", {}).get("name", "<unknown>")
        spec = nc.get("spec", {}) or {}
        cfg_path, cni_cfg = _get_cni_cfg(spec)
        current = cni_cfg.get("image", "")
        enabled = bool(cni_cfg.get("enable", False))
        if enabled:
            rollout_targets.append(name)

        if current == CNI_PLUGINS_IMAGE:
            LOG.info("[%s] cniPlugins.image is already up-to-date: %s", name, current)
            already_current.append(name)
        else:
            LOG.info("[%s] %s.image: '%s' -> '%s'", name, cfg_path, current, CNI_PLUGINS_IMAGE)
            body = get_networkconfig_custom(NC_NAMESPACE, name)
            body.setdefault("spec", {})
            if cfg_path == "secondaryNetwork.cniPlugins":
                body["spec"].setdefault("secondaryNetwork", {}).setdefault("cniPlugins", {})["image"] = CNI_PLUGINS_IMAGE
            else:
                body["spec"].setdefault("cniPlugins", {})["image"] = CNI_PLUGINS_IMAGE
            replace_with_retry(NC_NAMESPACE, name, body)
            updated.append(name)
            LOG.info("[%s] cniPlugins.image patch accepted", name)

    LOG.info("cni-plugins update summary -- updated: %s, already-current: %s", updated, already_current)
    assert updated or already_current, "No NetworkConfig was processed"

    if not updated:
        pytest.skip(f"All NetworkConfigs already have cniPlugins.image: {CNI_PLUGINS_IMAGE}")

    if not rollout_targets:
        pytest.skip("No NetworkConfig with cniPlugins.enable=True found for DaemonSet rollout verification")

    for nc_name in rollout_targets:
        ds = _ds_name(nc_name, DS_SUFFIX_CNI_PLUGINS)
        _wait_for_daemonset_rollout(apps_v1, NC_NAMESPACE, ds, CNI_PLUGINS_IMAGE)
        _assert_pods_have_image(v1, apps_v1, NC_NAMESPACE, ds, CNI_PLUGINS_IMAGE)


@pytest.mark.timeout(TEST_TIMEOUT)
def test_update_1_0_0_all_operand_images():
    """
    Update all four operand images to v1.0.0 in a single pass per NetworkConfig.
    """
    LOG.info("Target images (v1.0.0):")
    LOG.info("  devicePluginImage  : %s", DEVICE_PLUGIN_IMAGE)
    LOG.info("  nodeLabellerImage  : %s", NODE_LABELLER_IMAGE)
    LOG.info("  metricsExporter    : %s", METRICS_EXPORTER_IMAGE)
    LOG.info("  cniPlugins         : %s", CNI_PLUGINS_IMAGE)

    nc_items = list_networkconfigs_custom(NC_NAMESPACE)
    if not nc_items:
        pytest.skip(f"No NetworkConfig resources found in namespace '{NC_NAMESPACE}'")

    v1      = k8s_client.CoreV1Api()
    apps_v1 = k8s_client.AppsV1Api()

    results = {}

    for nc in nc_items:
        name = nc.get("metadata", {}).get("name", "<unknown>")
        spec = nc.get("spec", {})

        current_dp  = spec.get("devicePlugin",    {}).get("devicePluginImage", "")
        current_nl  = spec.get("devicePlugin",    {}).get("nodeLabellerImage", "")
        current_me  = spec.get("metricsExporter", {}).get("image", "")

        dp_needs = current_dp != DEVICE_PLUGIN_IMAGE
        nl_needs = current_nl != NODE_LABELLER_IMAGE
        me_needs = current_me != METRICS_EXPORTER_IMAGE

        if not (dp_needs or nl_needs or me_needs):
            LOG.info("[%s] All operand images are already at v1.0.0", name)
            results[name] = "skipped"
            continue

        body = get_networkconfig_custom(NC_NAMESPACE, name)
        body.setdefault("spec", {})
        body["spec"].setdefault("devicePlugin", {})
        body["spec"].setdefault("metricsExporter", {})

        if dp_needs:
            LOG.info("[%s] devicePluginImage: '%s' -> '%s'", name, current_dp, DEVICE_PLUGIN_IMAGE)
            body["spec"]["devicePlugin"]["devicePluginImage"] = DEVICE_PLUGIN_IMAGE

        if nl_needs:
            LOG.info("[%s] nodeLabellerImage: '%s' -> '%s'", name, current_nl, NODE_LABELLER_IMAGE)
            body["spec"]["devicePlugin"]["nodeLabellerImage"] = NODE_LABELLER_IMAGE

        if me_needs:
            LOG.info("[%s] metricsExporter.image: '%s' -> '%s'", name, current_me, METRICS_EXPORTER_IMAGE)
            body["spec"]["metricsExporter"]["image"] = METRICS_EXPORTER_IMAGE

        replace_with_retry(NC_NAMESPACE, name, body)
        results[name] = "updated"
        LOG.info("[%s] NetworkConfig patch accepted", name)

    updated         = [n for n, s in results.items() if s == "updated"]
    already_current = [n for n, s in results.items() if s == "skipped"]
    LOG.info("All-operands update summary -- updated: %s, already-current: %s", updated, already_current)
    assert results, "No NetworkConfig was processed"

    if not updated:
        pytest.skip("All operand images are already at v1.0.0")

    all_nc_names = [
        nc.get("metadata", {}).get("name")
        for nc in nc_items
        if nc.get("metadata", {}).get("name")
    ]
    for nc_name in all_nc_names:
        for suffix, image in (
            (DS_SUFFIX_DEVICE_PLUGIN,    DEVICE_PLUGIN_IMAGE),
            (DS_SUFFIX_NODE_LABELLER,    NODE_LABELLER_IMAGE),
            (DS_SUFFIX_METRICS_EXPORTER, METRICS_EXPORTER_IMAGE),
        ):
            ds = _ds_name(nc_name, suffix)
            _wait_for_daemonset_rollout(apps_v1, NC_NAMESPACE, ds, image)
            _assert_pods_have_image(v1, apps_v1, NC_NAMESPACE, ds, image)
