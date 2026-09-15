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
test_update_main_operand.py

Updates NetworkConfig operand images to the latest available main-* builds
fetched from the internal assets server.

  - spec.devicePlugin.devicePluginImage
        -> $DEVICE_PLUGIN_ASSETS_URL (set via env var DEVICE_PLUGIN_ASSETS_URL)
          latest tag pattern: main-<N>
          registry:           docker.io/amdpsdo/k8s-network-device-plugin

  - spec.devicePlugin.nodeLabellerImage
        -> $NODE_LABELLER_ASSETS_URL (set via env var NODE_LABELLER_ASSETS_URL)
          latest tag pattern: main-<N>
          registry:           docker.io/amdpsdo/k8s-network-node-labeller

  - spec.metricsExporter.image
        -> $METRICS_EXPORTER_ASSETS_URL (set via env var METRICS_EXPORTER_ASSETS_URL)
          latest tag pattern: exporter-0.0.1-<N>
          registry:           docker.io/amdpsdo/device-metrics-exporter-ainic
"""

import base64
import re
import json
import time
import subprocess
import os
import shutil
import shlex
import pytest
from urllib.request import urlopen
from urllib.error import URLError

import logging

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
FETCH_TIMEOUT = 30  # seconds for HTTP requests to the assets server
ENV_JSON_PATH = os.path.join(os.path.dirname(__file__), "env.json")

DEVICE_PLUGIN_ASSETS_URL = os.environ.get(
    "DEVICE_PLUGIN_ASSETS_URL",
    "https://example.com/builds/hourly-k8s-network-device-plugin/"
)
NODE_LABELLER_ASSETS_URL = os.environ.get(
    "NODE_LABELLER_ASSETS_URL",
    "https://example.com/builds/hourly-k8s-network-node-labeller/"
)
METRICS_EXPORTER_ASSETS_URL = os.environ.get(
    "METRICS_EXPORTER_ASSETS_URL",
    "https://example.com/builds/hourly-device-metrics-exporter/"
)

DEVICE_PLUGIN_REPO    = "docker.io/amdpsdo/k8s-network-device-plugin"
NODE_LABELLER_REPO    = "docker.io/amdpsdo/k8s-network-node-labeller"
METRICS_EXPORTER_REPO = "docker.io/amdpsdo/device-metrics-exporter-ainic"

METRICS_EXPORTER_VERSION_PREFIX = "exporter-0.0.1"

CNI_PLUGINS_REPO = "docker.io/amdpsdo/k8s-cni-plugins"
DS_SUFFIX_CNI_PLUGINS = "cni-plugins"
# Skopeo image used to list registry tags; requires Docker to be available.
CNI_SKOPEO_IMAGE = "quay.io/skopeo/stable"
# Set this env var to "user:token" to authenticate against the amdpsdo registry.
CNI_REGISTRY_CREDS_ENV = "CNI_REGISTRY_CREDS"
# Kubernetes secret holding dockerconfigjson credentials for the amdpsdo registry.
CNI_REGISTRY_SECRET_NAME = "amdpsdo-secret"

# DaemonSet name suffixes (must match the operator constants)
DS_SUFFIX_DEVICE_PLUGIN    = "device-plugin"
DS_SUFFIX_NODE_LABELLER    = "node-labeller"
DS_SUFFIX_METRICS_EXPORTER = "metrics-exporter"

# How long to wait for a full DaemonSet rollout to finish
POD_ROLLOUT_TIMEOUT = 600   # seconds
POD_ROLLOUT_POLL    = 15    # seconds between status checks


# ---------- Asset-scraping helpers ----------

def _fetch_page(url: str) -> str:
    """Fetch *url* and return its body as a Unicode string."""
    try:
        with urlopen(url, timeout=FETCH_TIMEOUT) as resp:  # noqa: S310 (internal URL)
            return resp.read().decode("utf-8", errors="replace")
    except URLError as exc:
        raise RuntimeError(f"Failed to fetch {url}: {exc}") from exc


def _latest_main_tag(assets_url: str) -> str:
    """
    Scrape *assets_url* for entries matching ``main-<N>`` and return the tag
    with the highest integer suffix, e.g. ``main-42``.
    """
    html = _fetch_page(assets_url)
    numbers = [
        int(m.group(1))
        for m in re.finditer(r"\bmain-(\d+)(?=/|\"|\s|<|$)", html)
    ]
    if not numbers:
        raise RuntimeError(
            f"No 'main-<N>' build entries found at {assets_url}. "
            "Verify the URL is reachable and lists builds in the expected format."
        )
    latest = max(numbers)
    LOG.info("Latest main-* build at %s: main-%d", assets_url, latest)
    return f"main-{latest}"


def _latest_versioned_tag(assets_url: str, prefix: str) -> str:
    """
    Scrape *assets_url* for entries matching ``<prefix>-<N>`` and return the
    tag with the highest integer suffix, e.g. ``exporter-0.0.1-300``.
    """
    html = _fetch_page(assets_url)
    escaped = re.escape(prefix)
    numbers = [
        int(m.group(1))
        for m in re.finditer(rf"\b{escaped}-(\d+)(?=/|\"|\s|<|$)", html)
    ]
    if not numbers:
        raise RuntimeError(
            f"No '{prefix}-<N>' build entries found at {assets_url}. "
            "Verify the URL is reachable and lists builds in the expected format."
        )
    latest = max(numbers)
    LOG.info("Latest %s-* build at %s: %s-%d", prefix, assets_url, prefix, latest)
    return f"{prefix}-{latest}"


def _image_ref(repo: str, tag: str) -> str:
    return f"{repo}:{tag}"


def _master_node_from_env_json() -> dict:
    """
    Read env.json and return master node connection details:
    {"ip": ..., "username": ..., "password": ...}
    """
    try:
        with open(ENV_JSON_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        raise RuntimeError(f"Failed to read {ENV_JSON_PATH}: {exc}") from exc

    try:
        instances = data["Instances"][0]["RawJSON"]["instances"]
    except Exception as exc:
        raise RuntimeError(
            f"Invalid env.json format in {ENV_JSON_PATH}: {exc}"
        ) from exc

    for inst in instances:
        if str(inst.get("type", "")).lower() == "master":
            ip = (inst.get("ip") or "").strip()
            user = (inst.get("username") or "").strip()
            password = inst.get("password") or ""
            if not ip or not user or not password:
                raise RuntimeError(
                    "Master entry in env.json must include ip, username, and password"
                )
            return {"ip": ip, "username": user, "password": password}

    raise RuntimeError(f"No master node found in {ENV_JSON_PATH}")


def _run_on_master(master: dict, command: str, timeout: int = 120) -> str:
    """Run a shell command on the master node via ssh and return stdout."""
    if shutil.which("sshpass") is None or shutil.which("ssh") is None:
        raise RuntimeError("sshpass/ssh not found in PATH; required for master-node helm commands")

    target = f"{master['username']}@{master['ip']}"
    cmd = [
        "sshpass", "-p", master["password"],
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        target,
        command,
    ]
    try:
        out = subprocess.check_output(
            cmd,
            timeout=timeout,
            stderr=subprocess.STDOUT,
        ).decode("utf-8", errors="replace")
        return out
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        output = getattr(exc, "output", b"") or b""
        if isinstance(exc, subprocess.CalledProcessError):
            detail = f"exit status {exc.returncode}"
        elif isinstance(exc, subprocess.TimeoutExpired):
            detail = f"timed out after {timeout}s"
        else:
            detail = exc.__class__.__name__
        raise RuntimeError(
            f"Remote command failed on {target} ({detail}).\n"
            + output.decode("utf-8", errors="replace")[:1000]
        ) from exc


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

    The function also handles the case where the DaemonSet has not yet been
    reconciled (404) by waiting up to *timeout_sec* for it to appear.
    """
    LOG.info(
        "Waiting up to %ds for DaemonSet %s/%s to roll out image '%s'",
        timeout_sec, namespace, ds_name, expected_image,
    )
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            ds = apps_v1.read_namespaced_daemon_set(name=ds_name, namespace=namespace)
        except ApiException as exc:
            if exc.status == 404:
                LOG.debug("DaemonSet %s/%s not found yet, retrying...", namespace, ds_name)
                time.sleep(poll_interval)
                continue
            raise

        # Confirm the operator has updated the pod template to the new image.
        spec_images = [
            c.image
            for c in (ds.spec.template.spec.containers or [])
        ]
        if expected_image not in spec_images:
            LOG.debug(
                "DaemonSet %s spec images %s do not include '%s' yet",
                ds_name, spec_images, expected_image,
            )
            time.sleep(poll_interval)
            continue

        status = ds.status
        desired  = status.desired_number_scheduled or 0
        updated  = status.updated_number_scheduled or 0
        ready    = status.number_ready            or 0
        available = status.number_available       or 0

        LOG.info(
            "DaemonSet %s -- desired=%d updated=%d ready=%d available=%d",
            ds_name, desired, updated, ready, available,
        )

        if desired == 0:
            LOG.info("DaemonSet %s has 0 desired pods -- rollout considered complete", ds_name)
            return

        if updated == desired and ready == desired:
            LOG.info(
                "DaemonSet %s rollout complete: all %d pod(s) running image '%s'",
                ds_name, desired, expected_image,
            )
            return

        time.sleep(poll_interval)

    raise TimeoutError(
        f"DaemonSet {namespace}/{ds_name} did not complete rollout with image "
        f"'{expected_image}' within {timeout_sec}s"
    )


def _assert_pods_have_image(
    v1: k8s_client.CoreV1Api,
    apps_v1: k8s_client.AppsV1Api,
    namespace: str,
    ds_name: str,
    expected_image: str,
) -> None:
    """
    Read the DaemonSet's pod selector, then verify every matching pod is
    Running and has *expected_image* in at least one container.
    Raises AssertionError on the first mismatch.
    """
    try:
        ds = apps_v1.read_namespaced_daemon_set(name=ds_name, namespace=namespace)
    except ApiException as exc:
        if exc.status == 404:
            LOG.warning("DaemonSet %s/%s not found -- skipping pod image assertion", namespace, ds_name)
            return
        raise

    match_labels = (ds.spec.selector.match_labels or {}) if ds.spec.selector else {}
    if not match_labels:
        LOG.warning("DaemonSet %s has no matchLabels -- skipping pod image assertion", ds_name)
        return

    label_selector = ",".join(f"{k}={v}" for k, v in match_labels.items())
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
        if expected_image not in running_images:
            failures.append(
                f"{pod_name}: expected image '{expected_image}' not found; "
                f"running images: {running_images}"
            )

    if failures:
        msg = (
            f"DaemonSet {ds_name} pod image verification failed:\n"
            + "\n".join(f"  - {f}" for f in failures)
        )
        LOG.error(msg)
        raise AssertionError(msg)

    LOG.info(
        "DaemonSet %s -- all %d pod(s) confirmed running image '%s'",
        ds_name, len(pods), expected_image,
    )


# ---------- CNI plugin helpers ----------

def _get_registry_creds_from_secret(
    namespace: str = NC_NAMESPACE,
    secret_name: str = CNI_REGISTRY_SECRET_NAME,
) -> str:
    """
    Read Docker registry credentials from a kubernetes.io/dockerconfigjson
    secret and return a ``user:password`` string for use with skopeo --creds.
    """
    v1 = k8s_client.CoreV1Api()
    secret = v1.read_namespaced_secret(name=secret_name, namespace=namespace)
    raw = base64.b64decode(secret.data[".dockerconfigjson"]).decode("utf-8")
    docker_cfg = json.loads(raw)

    for registry, auth_data in docker_cfg.get("auths", {}).items():
        auth_b64 = auth_data.get("auth", "")
        if auth_b64:
            return base64.b64decode(auth_b64).decode("utf-8")

    raise RuntimeError(
        f"No auth entry found in secret {namespace}/{secret_name}"
    )


def _latest_cni_tag(master: dict) -> str:
    """
    Query the amdpsdo registry for k8s-cni-plugins tags via skopeo and
    return the tag with the highest ``main-<N>`` number.

    Run skopeo on the master node via Docker and return the highest ``main-<N>`` tag.
    Credentials are resolved in order:
      1. ``CNI_REGISTRY_CREDS`` environment variable (``user:token``)
      2. ``amdpsdo-secret`` dockerconfigjson secret in ``kube-amd-network``
    """
    creds = os.environ.get(CNI_REGISTRY_CREDS_ENV, "").strip()
    if not creds:
        LOG.info("CNI_REGISTRY_CREDS not set; reading from secret %s/%s",
                 NC_NAMESPACE, CNI_REGISTRY_SECRET_NAME)
        creds = _get_registry_creds_from_secret()

    image_ref = CNI_PLUGINS_REPO.replace("docker.io/", "")

    sudo_pw = shlex.quote(master["password"])
    remote_cmd = (
        f"printf '%s\\n' {sudo_pw} | sudo -S -p '' "
        "docker run --rm -i "
        f"{CNI_SKOPEO_IMAGE} "
        "list-tags "
        f"--creds={creds} "
        f"docker://{image_ref}"
    )
    masked_remote_cmd = remote_cmd.replace(creds, "***")
    LOG.info("Listing CNI plugin tags on master node: %s", masked_remote_cmd)
    out = _run_on_master(master, remote_cmd, timeout=120)

    # Some environments prepend SSH/sudo informational lines before JSON.
    # Parse from the first JSON object found in the output.
    first = out.find("{")
    last = out.rfind("}")
    if first == -1 or last == -1 or first >= last:
        raise RuntimeError(
            "Failed to locate JSON object in skopeo output. "
            f"Output: {out[:500]}"
        )

    json_blob = out[first:last + 1]
    try:
        data = json.loads(json_blob)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Failed to parse skopeo JSON output: {exc}\nOutput: {out[:500]}"
        ) from exc

    tags = data.get("Tags", [])
    numbers = [
        int(m.group(1))
        for tag in tags
        for m in [re.fullmatch(r"main-(\d+)", tag)]
        if m
    ]
    if not numbers:
        raise RuntimeError(
            f"No 'main-<N>' tags found for {CNI_PLUGINS_REPO}. "
            f"Available tags (first 20): {sorted(tags)[:20]}"
        )

    latest = max(numbers)
    LOG.info("Latest main-* CNI plugin tag: main-%d", latest)
    return f"main-{latest}"


# ---------- Individual operand tests ----------

@pytest.mark.timeout(TEST_TIMEOUT)
def test_update_device_plugin_image():
    """
    Update spec.devicePlugin.devicePluginImage in every NetworkConfig to the
    latest main-* build published on the assets server, then wait for the
    DaemonSet rollout to finish and verify all pods are running the new image.
    """
    latest_tag   = _latest_main_tag(DEVICE_PLUGIN_ASSETS_URL)
    latest_image = _image_ref(DEVICE_PLUGIN_REPO, latest_tag)
    LOG.info("Target devicePluginImage: %s", latest_image)

    nc_items = list_networkconfigs_custom(NC_NAMESPACE)
    if not nc_items:
        pytest.skip(f"No NetworkConfig resources found in namespace '{NC_NAMESPACE}'")

    v1      = k8s_client.CoreV1Api()
    apps_v1 = k8s_client.AppsV1Api()

    updated, already_current = [], []

    for nc in nc_items:
        name    = nc.get("metadata", {}).get("name", "<unknown>")
        current = nc.get("spec", {}).get("devicePlugin", {}).get("devicePluginImage", "")

        if current == latest_image:
            LOG.info("[%s] devicePluginImage is already up-to-date: %s", name, current)
            already_current.append(name)
        else:
            LOG.info("[%s] devicePluginImage: '%s' -> '%s'", name, current, latest_image)
            body = get_networkconfig_custom(NC_NAMESPACE, name)
            body.setdefault("spec", {}).setdefault("devicePlugin", {})["devicePluginImage"] = latest_image
            replace_with_retry(NC_NAMESPACE, name, body)
            updated.append(name)
            LOG.info("[%s] devicePluginImage patch accepted", name)

    LOG.info("device-plugin update summary -- updated: %s, already-current: %s", updated, already_current)
    assert updated or already_current, "No NetworkConfig was processed"

    if not updated:
        pytest.skip(f"All NetworkConfigs already have the latest devicePluginImage: {latest_image}")

    # Wait for every affected DaemonSet to finish rolling out, then verify pods.
    all_nc_names = [nc.get("metadata", {}).get("name") for nc in nc_items if nc.get("metadata", {}).get("name")]
    for nc_name in all_nc_names:
        ds = _ds_name(nc_name, DS_SUFFIX_DEVICE_PLUGIN)
        _wait_for_daemonset_rollout(apps_v1, NC_NAMESPACE, ds, latest_image)
        _assert_pods_have_image(v1, apps_v1, NC_NAMESPACE, ds, latest_image)


@pytest.mark.timeout(TEST_TIMEOUT)
def test_update_node_labeller_image():
    """
    Update spec.devicePlugin.nodeLabellerImage in every NetworkConfig to the
    latest main-* build published on the assets server, then wait for the
    DaemonSet rollout to finish and verify all pods are running the new image.
    """
    latest_tag   = _latest_main_tag(NODE_LABELLER_ASSETS_URL)
    latest_image = _image_ref(NODE_LABELLER_REPO, latest_tag)
    LOG.info("Target nodeLabellerImage: %s", latest_image)

    nc_items = list_networkconfigs_custom(NC_NAMESPACE)
    if not nc_items:
        pytest.skip(f"No NetworkConfig resources found in namespace '{NC_NAMESPACE}'")

    v1      = k8s_client.CoreV1Api()
    apps_v1 = k8s_client.AppsV1Api()

    updated, already_current = [], []

    for nc in nc_items:
        name    = nc.get("metadata", {}).get("name", "<unknown>")
        current = nc.get("spec", {}).get("devicePlugin", {}).get("nodeLabellerImage", "")

        if current == latest_image:
            LOG.info("[%s] nodeLabellerImage is already up-to-date: %s", name, current)
            already_current.append(name)
        else:
            LOG.info("[%s] nodeLabellerImage: '%s' -> '%s'", name, current, latest_image)
            body = get_networkconfig_custom(NC_NAMESPACE, name)
            body.setdefault("spec", {}).setdefault("devicePlugin", {})["nodeLabellerImage"] = latest_image
            replace_with_retry(NC_NAMESPACE, name, body)
            updated.append(name)
            LOG.info("[%s] nodeLabellerImage patch accepted", name)

    LOG.info("node-labeller update summary -- updated: %s, already-current: %s", updated, already_current)
    assert updated or already_current, "No NetworkConfig was processed"

    if not updated:
        pytest.skip(f"All NetworkConfigs already have the latest nodeLabellerImage: {latest_image}")

    # Wait for every affected DaemonSet to finish rolling out, then verify pods.
    all_nc_names = [nc.get("metadata", {}).get("name") for nc in nc_items if nc.get("metadata", {}).get("name")]
    for nc_name in all_nc_names:
        ds = _ds_name(nc_name, DS_SUFFIX_NODE_LABELLER)
        _wait_for_daemonset_rollout(apps_v1, NC_NAMESPACE, ds, latest_image)
        _assert_pods_have_image(v1, apps_v1, NC_NAMESPACE, ds, latest_image)


@pytest.mark.timeout(TEST_TIMEOUT)
def test_update_metrics_exporter_image():
    """
    Update spec.metricsExporter.image in every NetworkConfig to the
    latest exporter-0.0.1-* build published on the assets server, then wait for the
    DaemonSet rollout to finish and verify all pods are running the new image.
    """
    latest_tag   = _latest_versioned_tag(METRICS_EXPORTER_ASSETS_URL, METRICS_EXPORTER_VERSION_PREFIX)
    latest_image = _image_ref(METRICS_EXPORTER_REPO, latest_tag)
    LOG.info("Target metricsExporter.image: %s", latest_image)

    nc_items = list_networkconfigs_custom(NC_NAMESPACE)
    if not nc_items:
        pytest.skip(f"No NetworkConfig resources found in namespace '{NC_NAMESPACE}'")

    v1      = k8s_client.CoreV1Api()
    apps_v1 = k8s_client.AppsV1Api()

    updated, already_current = [], []

    for nc in nc_items:
        name    = nc.get("metadata", {}).get("name", "<unknown>")
        current = nc.get("spec", {}).get("metricsExporter", {}).get("image", "")

        if current == latest_image:
            LOG.info("[%s] metricsExporter.image is already up-to-date: %s", name, current)
            already_current.append(name)
        else:
            LOG.info("[%s] metricsExporter.image: '%s' -> '%s'", name, current, latest_image)
            body = get_networkconfig_custom(NC_NAMESPACE, name)
            body.setdefault("spec", {}).setdefault("metricsExporter", {})["image"] = latest_image
            replace_with_retry(NC_NAMESPACE, name, body)
            updated.append(name)
            LOG.info("[%s] metricsExporter.image patch accepted", name)

    LOG.info("metrics-exporter update summary -- updated: %s, already-current: %s", updated, already_current)
    assert updated or already_current, "No NetworkConfig was processed"

    if not updated:
        pytest.skip(f"All NetworkConfigs already have the latest metricsExporter.image: {latest_image}")

    # Wait for every affected DaemonSet to finish rolling out, then verify pods.
    all_nc_names = [nc.get("metadata", {}).get("name") for nc in nc_items if nc.get("metadata", {}).get("name")]
    for nc_name in all_nc_names:
        ds = _ds_name(nc_name, DS_SUFFIX_METRICS_EXPORTER)
        _wait_for_daemonset_rollout(apps_v1, NC_NAMESPACE, ds, latest_image)
        _assert_pods_have_image(v1, apps_v1, NC_NAMESPACE, ds, latest_image)


# ---------- Combined convenience test ----------

@pytest.mark.timeout(TEST_TIMEOUT)
def test_update_all_operand_images():
    """
    Update all three operand images in a single pass per NetworkConfig.
    This is the recommended entry-point when you want to bring everything
    up-to-date in one shot.

    Resolves the latest image references from the assets server, then for
    each NetworkConfig issues a single replace if any of the three images
    differ from what is already set.
    """
    # --- Resolve the three latest images ---
    latest_dp_tag  = _latest_main_tag(DEVICE_PLUGIN_ASSETS_URL)
    latest_nl_tag  = _latest_main_tag(NODE_LABELLER_ASSETS_URL)
    latest_me_tag  = _latest_versioned_tag(METRICS_EXPORTER_ASSETS_URL, METRICS_EXPORTER_VERSION_PREFIX)

    latest_dp_image  = _image_ref(DEVICE_PLUGIN_REPO,    latest_dp_tag)
    latest_nl_image  = _image_ref(NODE_LABELLER_REPO,    latest_nl_tag)
    latest_me_image  = _image_ref(METRICS_EXPORTER_REPO, latest_me_tag)

    LOG.info("Latest images resolved:")
    LOG.info("  devicePluginImage  : %s", latest_dp_image)
    LOG.info("  nodeLabellerImage  : %s", latest_nl_image)
    LOG.info("  metricsExporter    : %s", latest_me_image)

    nc_items = list_networkconfigs_custom(NC_NAMESPACE)
    if not nc_items:
        pytest.skip(f"No NetworkConfig resources found in namespace '{NC_NAMESPACE}'")

    results = {}

    v1      = k8s_client.CoreV1Api()
    apps_v1 = k8s_client.AppsV1Api()

    for nc in nc_items:
        name = nc.get("metadata", {}).get("name", "<unknown>")
        spec = nc.get("spec", {})

        current_dp = spec.get("devicePlugin",    {}).get("devicePluginImage", "")
        current_nl = spec.get("devicePlugin",    {}).get("nodeLabellerImage", "")
        current_me = spec.get("metricsExporter", {}).get("image", "")

        dp_needs_update = current_dp != latest_dp_image
        nl_needs_update = current_nl != latest_nl_image
        me_needs_update = current_me != latest_me_image

        if not (dp_needs_update or nl_needs_update or me_needs_update):
            LOG.info("[%s] All three operand images are already up-to-date", name)
            results[name] = "skipped"
            continue

        # Fetch a fresh copy to get the latest resourceVersion.
        body = get_networkconfig_custom(NC_NAMESPACE, name)
        body.setdefault("spec", {})
        body["spec"].setdefault("devicePlugin",    {})
        body["spec"].setdefault("metricsExporter", {})

        if dp_needs_update:
            LOG.info("[%s] devicePluginImage : '%s' -> '%s'", name, current_dp, latest_dp_image)
            body["spec"]["devicePlugin"]["devicePluginImage"] = latest_dp_image

        if nl_needs_update:
            LOG.info("[%s] nodeLabellerImage : '%s' -> '%s'", name, current_nl, latest_nl_image)
            body["spec"]["devicePlugin"]["nodeLabellerImage"] = latest_nl_image

        if me_needs_update:
            LOG.info("[%s] metricsExporter.image: '%s' -> '%s'", name, current_me, latest_me_image)
            body["spec"]["metricsExporter"]["image"] = latest_me_image

        replace_with_retry(NC_NAMESPACE, name, body)
        results[name] = "updated"
        LOG.info("[%s] NetworkConfig patch accepted", name)

    updated         = [n for n, s in results.items() if s == "updated"]
    already_current = [n for n, s in results.items() if s == "skipped"]
    LOG.info(
        "All-operands update summary -- updated: %s, already-current: %s",
        updated, already_current,
    )
    assert results, "No NetworkConfig was processed"

    if not updated:
        pytest.skip("All operand images are already at the latest builds")

    # ------------------------------------------------------------------ #
    # Wait for every DaemonSet to roll out and confirm pod images.         #
    # ------------------------------------------------------------------ #
    all_nc_names = [
        nc.get("metadata", {}).get("name")
        for nc in nc_items
        if nc.get("metadata", {}).get("name")
    ]
    for nc_name in all_nc_names:
        for suffix, image in (
            (DS_SUFFIX_DEVICE_PLUGIN,    latest_dp_image),
            (DS_SUFFIX_NODE_LABELLER,    latest_nl_image),
            (DS_SUFFIX_METRICS_EXPORTER, latest_me_image),
        ):
            ds = _ds_name(nc_name, suffix)
            _wait_for_daemonset_rollout(apps_v1, NC_NAMESPACE, ds, image)
            _assert_pods_have_image(v1, apps_v1, NC_NAMESPACE, ds, image)



# ---------- CNI plugin upgrade test ----------

@pytest.mark.timeout(max(TEST_TIMEOUT, 900))
def test_update_cni_plugin_image():
    """
    Resolve the latest CNI plugin tag from master, then update
    spec.cniPlugins.image in every NetworkConfig via Kubernetes API.

    The SSH command for tag discovery is one-shot and exits immediately.

    Tag discovery uses skopeo against the amdpsdo Docker Hub registry.
    Credentials must be supplied via the ``CNI_REGISTRY_CREDS`` environment
    variable (``user:token``).
    """
    if shutil.which("sshpass") is None or shutil.which("ssh") is None:
        pytest.skip("Skipping CNI plugin upgrade test: sshpass/ssh are required to run on master node")

    try:
        master = _master_node_from_env_json()
    except RuntimeError as exc:
        pytest.fail(str(exc))

    try:
        latest_tag = _latest_cni_tag(master)
    except RuntimeError as exc:
        pytest.fail(str(exc))

    latest_image = _image_ref(CNI_PLUGINS_REPO, latest_tag)
    LOG.info("Target cniPlugins.image: %s", latest_image)

    nc_items = list_networkconfigs_custom(NC_NAMESPACE)
    if not nc_items:
        pytest.skip(f"No NetworkConfig resources found in namespace '{NC_NAMESPACE}'")

    v1      = k8s_client.CoreV1Api()
    apps_v1 = k8s_client.AppsV1Api()

    updated, already_current = [], []
    rollout_targets = []

    def _get_cni_cfg(spec: dict) -> tuple:
        """
        Return (path_key, cfg_dict) where path_key is either:
          - secondaryNetwork.cniPlugins (preferred)
          - cniPlugins (legacy fallback)
        """
        sec = (spec.get("secondaryNetwork", {}) or {})
        sec_cni = (sec.get("cniPlugins", {}) or {})
        if sec_cni:
            return "secondaryNetwork.cniPlugins", sec_cni
        return "cniPlugins", (spec.get("cniPlugins", {}) or {})

    for nc in nc_items:
        name    = nc.get("metadata", {}).get("name", "<unknown>")
        spec = nc.get("spec", {}) or {}
        cfg_path, cni_cfg = _get_cni_cfg(spec)
        current = cni_cfg.get("image", "")
        enabled = bool(cni_cfg.get("enable", False))
        if enabled:
            rollout_targets.append(name)

        if current == latest_image:
            LOG.info("[%s] cniPlugins.image is already up-to-date: %s", name, current)
            already_current.append(name)
        else:
            LOG.info("[%s] %s.image: '%s' -> '%s'", name, cfg_path, current, latest_image)
            body = get_networkconfig_custom(NC_NAMESPACE, name)
            body.setdefault("spec", {})
            if cfg_path == "secondaryNetwork.cniPlugins":
                body["spec"].setdefault("secondaryNetwork", {}).setdefault("cniPlugins", {})["image"] = latest_image
            else:
                body["spec"].setdefault("cniPlugins", {})["image"] = latest_image
            replace_with_retry(NC_NAMESPACE, name, body)
            updated.append(name)
            LOG.info("[%s] cniPlugins.image patch accepted", name)

    LOG.info(
        "cni-plugins update summary -- updated: %s, already-current: %s",
        updated, already_current,
    )
    assert updated or already_current, "No NetworkConfig was processed"

    if not updated:
        pytest.skip(f"All NetworkConfigs already have the latest cniPlugins.image: {latest_image}")

    if not rollout_targets:
        pytest.skip("No NetworkConfig with cniPlugins.enable=True found for DaemonSet rollout verification")

    # Wait only for NetworkConfig entries where cniPlugins.enable=True.
    for nc_name in rollout_targets:
        ds = _ds_name(nc_name, DS_SUFFIX_CNI_PLUGINS)
        _wait_for_daemonset_rollout(apps_v1, NC_NAMESPACE, ds, latest_image)
        _assert_pods_have_image(v1, apps_v1, NC_NAMESPACE, ds, latest_image)
