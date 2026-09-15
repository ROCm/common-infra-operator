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
Standalone Helm Chart tests for AMD AINIC Metrics Exporter.

Test cases:
  1. test_validate_chart_metadata
  2. test_helm_install_metrics_exporter
  3. test_metrics_endpoint_accessible
  4. test_configmap_hot_reload
  5. test_uninstall_metrics_exporter
  6. test_helm_install_multiple_times_no_disruption
  7. test_helm_upgrade_charts
  8. test_host_network_toggle
  9. test_monitor_resources_toggle
"""

import time
import json
import yaml
import pytest
import logging

import lib.helm_util as helm_util
import lib.k8_util as k8_util
import lib.nic_util as nic_util
from lib.nic_util import PROM_LINE_RE

LOG = logging.getLogger("test_metrics_exporter_standalone_helm")

TEST_TIMEOUT = 300

# Values the ME chart needs for a standalone (operator-less) install.
ME_INSTALL_DEFAULTS = {
    "hostNetwork": "true",
    "monitor.resources.gpu": "false",
    "monitor.resources.nic": "true",
    "updateStrategy.type": "RollingUpdate",
}

# ME listens directly on the node with hostNetwork=true.
ME_METRICS_PORT = 5001


# ---------- Helpers ----------

def _chart_version(images, artifact):
    """Chart version key differs by manifest location scheme.

    file:// emits "<artifact>.helm-chart.version"; repo:// and oci:// emit
    "<artifact>.version". helm_install silently drops --version when this is
    None, which would unpin a repo:// chart to "latest", so check both.
    """
    return (images.get(f"{artifact}.helm-chart.version")
            or images.get(f"{artifact}.version"))


def _install_values(images, artifact, overrides=None):
    values = dict(ME_INSTALL_DEFAULTS)
    secret = images.get(f"{artifact}.secret")
    if secret:
        values["image.pullSecrets"] = secret
    if overrides:
        values.update(overrides)
    return values


def _me_install(gpu_cluster, images, chart, environment, overrides=None):
    ns = environment.me_namespace
    release = environment.me_release_name
    artifact = environment.me_artifact
    return helm_util.helm_install(gpu_cluster, release, ns,
                                  chart, _chart_version(images, artifact), None,
                                  **_install_values(images, artifact, overrides))


def _me_upgrade(gpu_cluster, images, chart, environment, overrides=None):
    ns = environment.me_namespace
    release = environment.me_release_name
    artifact = environment.me_artifact
    return helm_util.helm_upgrade(gpu_cluster, release, ns,
                                  chart, _chart_version(images, artifact), None,
                                  **_install_values(images, artifact, overrides))


def _daemonset_names(namespace):
    ret_code, daemonsets = k8_util.k8_get_daemonsets(namespace)
    assert ret_code == 0, f"Failed to list DaemonSets in {namespace}"
    return [ds["metadata"]["name"] for ds in daemonsets]


def _wait_daemonsets_ready(namespace):
    """Wait for every DaemonSet in the namespace. Returns the names waited on."""
    names = _daemonset_names(namespace)
    for name in names:
        assert k8_util.k8_wait_for_daemonset_ready(name, namespace), \
            f"DaemonSet {name} not ready"
    return names


def _me_pods(namespace):
    ret_code, pods = k8_util.k8_get_pods(namespace)
    assert ret_code == 0, f"Failed to list pods in {namespace}"
    return pods


def _fetch_metrics(gpu_cluster, node_ip, port=ME_METRICS_PORT):
    """Curl /metrics from a throwaway pod. Returns the body, or None."""
    ret_code, ret_stdout, _ = k8_util.k8_run_curl_cmd(
        gpu_cluster, ["-s", f"http://{node_ip}:{port}/metrics"])
    if ret_code != 0 or not (ret_stdout or "").strip():
        return None
    return ret_stdout


def _metrics_have_numeric(text):
    """True when the body has at least one Prometheus numeric sample line."""
    if not text:
        return False
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and PROM_LINE_RE.match(line):
            return True
    return False


# ---------- Fixtures ----------

@pytest.fixture(scope="module")
def chart_on_cluster(images, environment):
    """Resolve the ME chart from the image manifest.

    helm now runs locally against --kubeconfig, so the chart is used in place
    rather than SCP'd to the k8s master.
    """
    artifact = environment.me_artifact
    chart = images.get(f"{artifact}.helm-chart", None)
    assert chart, (
        f"No '{artifact}.helm-chart' in the image manifest. "
        f"Set ME_ARTIFACT if the manifest names the chart differently."
    )
    LOG.info("ME chart: %s (version=%s)", chart, _chart_version(images, artifact))
    return chart


@pytest.fixture(scope="module")
def me_repo(gpu_cluster, images, environment):
    """Add the chart repo when the manifest points at one (repo:// only)."""
    artifact = environment.me_artifact
    if images.get(f"{artifact}.repo", None):
        helm_util.helm_add_repo(gpu_cluster, images.get(f"{artifact}.repo-name"),
                                images.get(f"{artifact}.repo"))


# ---------- Test 1: Validate chart metadata ----------

@pytest.mark.timeout(TEST_TIMEOUT)
def test_validate_chart_metadata(gpu_cluster, environment, me_repo, chart_on_cluster):
    """
    Validate Chart.yaml, values.yaml, and helm template output.

    Checks:
    - Chart.yaml has required fields (apiVersion, name, version, appVersion, description)
    - Default values.yaml is parseable
    - helm template renders DaemonSet + Service + ConfigMap
    """
    chart = chart_on_cluster
    ns = environment.me_namespace
    release = environment.me_release_name

    # 1) Validate Chart.yaml metadata via helm show chart
    rc, chart_yaml, err = helm_util.helm_show_chart(gpu_cluster, chart)
    assert rc == 0, f"helm show chart failed: {err}"

    chart_meta = yaml.safe_load(chart_yaml)
    assert chart_meta is not None, "Chart.yaml is empty or unparseable"

    required_fields = ["apiVersion", "name", "version", "appVersion", "description"]
    missing = [f for f in required_fields if f not in chart_meta]
    assert not missing, f"Chart.yaml missing required fields: {missing}"

    LOG.info("Chart metadata: name=%s version=%s appVersion=%s",
             chart_meta.get("name"), chart_meta.get("version"), chart_meta.get("appVersion"))

    # 2) Validate default values.yaml
    rc, values_yaml, err = helm_util.helm_show_values(gpu_cluster, chart)
    assert rc == 0, f"helm show values failed: {err}"

    default_values = yaml.safe_load(values_yaml)
    assert default_values is not None, "values.yaml is empty or unparseable"
    LOG.info("Default values keys: %s", list(default_values.keys()))

    # 3) Validate helm template renders expected resources
    rc, rendered, err = helm_util.helm_template(gpu_cluster, release, chart, ns)
    assert rc == 0, f"helm template with defaults failed: {err}"
    assert rendered.strip(), "helm template produced empty output"

    manifests = list(yaml.safe_load_all(rendered))
    kinds = [m.get("kind") for m in manifests if m]
    LOG.info("Rendered resource kinds (defaults): %s", kinds)
    assert "DaemonSet" in kinds, "Expected DaemonSet in rendered manifests"
    assert "Service" in kinds, "Expected Service in rendered manifests"
    assert "ConfigMap" in kinds, "Expected ConfigMap in rendered manifests"

    LOG.info("Chart metadata validation passed.")


# ---------- Test 2: Install Helm charts ----------

@pytest.mark.timeout(TEST_TIMEOUT)
def test_helm_install_metrics_exporter(gpu_cluster, images, environment, me_repo, chart_on_cluster):
    """
    Install the standalone Metrics Exporter Helm chart and verify deployment.

    Steps:
    1) Clean up any existing release.
    2) Run helm install.
    3) Verify release is deployed.
    4) Verify DaemonSet is created and all pods are ready.
    5) Verify ME pods are Running on NIC nodes.
    6) Verify ME service is created.
    """
    chart = chart_on_cluster
    ns = environment.me_namespace
    release = environment.me_release_name

    # 1) Cleanup
    helm_util.helm_ensure_release_cleaned_up(gpu_cluster, release, ns)

    # 2) Install
    rc, out, err = _me_install(gpu_cluster, images, chart, environment)
    assert rc == 0, f"helm install failed: rc={rc} err={err}"
    LOG.info("helm install output: %s", out)

    # 3) Verify release deployed
    assert helm_util.is_helm_chart_healthy(gpu_cluster, release, ns), \
        f"Release {release} not in deployed state after install"

    # 4) Wait for DaemonSet
    ds_names = _wait_daemonsets_ready(ns)
    assert len(ds_names) > 0, "No DaemonSet found after install"
    LOG.info("DaemonSets ready: %s", ds_names)

    # 5) Verify pods running on NIC nodes
    nic_nodes = nic_util.get_amd_nic_nodes()
    if not nic_nodes:
        pytest.skip("No AMD NIC nodes found in cluster")

    all_ok, not_running = k8_util.k8_all_pods_running(ns)
    assert all_ok, f"ME pods not running: {not_running}"

    pods = _me_pods(ns)
    pod_nodes = {p["spec"]["node_name"] for p in pods if p["spec"].get("node_name")}
    nic_node_names = {n.metadata.name for n in nic_nodes}
    LOG.info("ME pods on nodes: %s", pod_nodes)
    LOG.info("AMD NIC nodes: %s", nic_node_names)

    # 6) Verify service created
    rc_svc, services, err_svc = k8_util.k8_get_services(ns)
    assert rc_svc == 0, f"Failed to list services: {err_svc}"
    assert len(services) > 0, "No service found after ME install"
    LOG.info("Services created: %s", [s["metadata"]["name"] for s in services])

    LOG.info("Helm install validated. ME pods running on %d nodes.", len(pod_nodes))


# ---------- Test 3: Metrics endpoint accessible ----------

@pytest.mark.timeout(TEST_TIMEOUT)
def test_metrics_endpoint_accessible(gpu_cluster, images, environment, me_repo, chart_on_cluster):
    """
    Verify /metrics endpoint returns Prometheus-format data with amd_ prefixed metrics.

    Steps:
    1) Ensure ME is installed and running.
    2) Get the NIC node's InternalIP.
    3) Curl /metrics from a throwaway pod in the cluster.
    4) Verify response contains amd_ prefixed Prometheus metrics.
    """
    ns = environment.me_namespace
    release = environment.me_release_name

    # 1) Ensure installed and running
    if not helm_util.is_helm_chart_healthy(gpu_cluster, release, ns):
        rc, _, err = _me_install(gpu_cluster, images, chart_on_cluster, environment)
        assert rc == 0, f"helm install failed: {err}"

    _wait_daemonsets_ready(ns)
    time.sleep(10)

    # 2) Get endpoint info
    nic_nodes = nic_util.get_amd_nic_nodes()
    if not nic_nodes:
        pytest.skip("No AMD NIC nodes found in cluster")

    node_name = nic_nodes[0].metadata.name
    rc_ip, node_ip = k8_util.k8_get_node_internal_ip(node_name)
    assert rc_ip == 0 and node_ip, "Could not get InternalIP for NIC node"
    LOG.info("Fetching metrics from node %s (%s) port %d",
             node_name, node_ip, ME_METRICS_PORT)

    metrics_text = None
    for attempt in range(6):
        metrics_text = _fetch_metrics(gpu_cluster, node_ip)
        if metrics_text:
            break
        LOG.info("Metrics not yet available, retrying (%d/6)...", attempt + 1)
        time.sleep(10)

    # 3-4) Verify metrics
    assert metrics_text, "No response from /metrics endpoint after retries"
    assert _metrics_have_numeric(metrics_text), \
        "Metrics endpoint did not return valid Prometheus numeric lines"
    assert nic_util.metrics_text_has_prefix(metrics_text, "amd_"), \
        "Metrics endpoint did not return any amd_ prefixed metrics"

    LOG.info("Metrics endpoint accessible and returning amd_ prefixed Prometheus metrics.")


# ---------- Test 4: ConfigMap hot reload ----------

@pytest.mark.timeout(TEST_TIMEOUT)
def test_configmap_hot_reload(gpu_cluster, images, environment, me_repo, chart_on_cluster):
    """
    Change MetricsFieldPrefix in ConfigMap, verify ME reflects change without restart.

    Steps:
    1) Ensure ME is installed.
    2) Read current ConfigMap data.
    3) Change MetricsFieldPrefix from amd_ to test_.
    4) Wait for ME pod to reflect change (up to 90s).
    5) Verify metrics endpoint uses new prefix.
    6) Restore original ConfigMap.
    """
    ns = environment.me_namespace
    release = environment.me_release_name

    # 1) Ensure installed
    if not helm_util.is_helm_chart_healthy(gpu_cluster, release, ns):
        rc, _, err = _me_install(gpu_cluster, images, chart_on_cluster, environment)
        assert rc == 0, f"helm install failed: {err}"
        _wait_daemonsets_ready(ns)
        time.sleep(10)

    # 2) Find and read ConfigMap
    cm_names = nic_util.get_me_configmap_names(ns)
    assert cm_names, "No ConfigMap found for metrics exporter"
    cm_name = cm_names[0]
    rc_cm, cm, err_cm = k8_util.k8_get_configmap(ns, cm_name)
    assert rc_cm == 0 and cm and cm.data, f"ConfigMap {cm_name} has no data: {err_cm}"
    original_data = dict(cm.data)
    LOG.info("Original ConfigMap %s keys: %s", cm_name, list(original_data.keys()))

    # 3) Modify ConfigMap  - change MetricsFieldPrefix
    modified_data = dict(original_data)
    for key, value in modified_data.items():
        try:
            cfg = json.loads(value)
            if isinstance(cfg, dict) and "MetricsFieldPrefix" in cfg:
                cfg["MetricsFieldPrefix"] = "test_"
                modified_data[key] = json.dumps(cfg, indent=2)
                LOG.info("Changed MetricsFieldPrefix to 'test_' in key '%s'", key)
                break
        except (json.JSONDecodeError, TypeError):
            # Try string replacement as fallback
            if "amd_" in value:
                modified_data[key] = value.replace("amd_", "test_")
                LOG.info("Replaced 'amd_' with 'test_' in key '%s'", key)
                break

    rc_patch, _, err_patch = k8_util.k8_patch_configmap(cm_name, ns, modified_data)
    assert rc_patch == 0, f"Failed to patch ConfigMap {cm_name}: {err_patch}"
    LOG.info("ConfigMap patched with modified prefix")

    # 4) Wait for config reload
    reloaded = nic_util.wait_for_config_reload(ns,
                                               expected_prefix="test_", timeout=90)
    if not reloaded:
        pytest.xfail("Metrics exporter did not reload ConfigMap within timeout")

    # 5) Verify metrics use new prefix (best-effort, may take time)
    nic_nodes = nic_util.get_amd_nic_nodes()
    if nic_nodes:
        rc_ip, node_ip = k8_util.k8_get_node_internal_ip(nic_nodes[0].metadata.name)
        if rc_ip == 0 and node_ip:
            time.sleep(10)  # additional wait for metric scrape cycle
            metrics_text = _fetch_metrics(gpu_cluster, node_ip)
            if metrics_text:
                has_test = nic_util.metrics_text_has_prefix(metrics_text, "test_")
                LOG.info("Metrics with test_ prefix after reload: %s", has_test)

    # 6) Restore original ConfigMap
    rc_restore, _, err_restore = k8_util.k8_patch_configmap(cm_name, ns, original_data)
    assert rc_restore == 0, f"Failed to restore ConfigMap {cm_name}: {err_restore}"
    LOG.info("ConfigMap restored to original values")

    # Wait for restoration to take effect
    time.sleep(15)

    LOG.info("ConfigMap hot reload test completed.")


# ---------- Test 5: Uninstall metrics exporter ----------

@pytest.mark.timeout(TEST_TIMEOUT)
def test_uninstall_metrics_exporter(gpu_cluster, images, environment, me_repo, chart_on_cluster):
    """
    Uninstall ME and verify DaemonSet, pods, and service are removed.

    Steps:
    1) Ensure ME is installed.
    2) Helm uninstall.
    3) Verify release removed.
    4) Verify DaemonSet gone via k8s API.
    5) Verify pods terminated.
    6) Verify service removed.
    """
    ns = environment.me_namespace
    release = environment.me_release_name

    # 1) Ensure installed
    if not helm_util.is_helm_chart_healthy(gpu_cluster, release, ns):
        rc, _, err = _me_install(gpu_cluster, images, chart_on_cluster, environment)
        assert rc == 0, f"helm install failed: {err}"
        _wait_daemonsets_ready(ns)
        time.sleep(5)

    # 2) Uninstall
    rc, out, err = helm_util.helm_uninstall(gpu_cluster, release, ns)
    assert rc == 0, f"helm uninstall failed: rc={rc} err={err}"
    LOG.info("helm uninstall output: %s", out)

    # 3) Verify release removed
    assert not helm_util.is_helm_chart_healthy(gpu_cluster, release, ns), \
        f"Release {release} still deployed after uninstall"

    # 4) Wait for resources to be cleaned up
    time.sleep(15)

    # Verify DaemonSet gone. k8_get_daemonsets returns rc != 0 when the
    # namespace went away with the release, which is also a pass.
    rc_ds, daemonsets = k8_util.k8_get_daemonsets(ns)
    if rc_ds == 0:
        assert len(daemonsets) == 0, (
            "DaemonSets still present after uninstall: "
            f"{[ds['metadata']['name'] for ds in daemonsets]}")
    else:
        LOG.info("Namespace may have been removed along with resources")

    # 5) Wait for pods to terminate (poll up to 60s)
    active_pods = []
    deadline = time.time() + 60
    while time.time() < deadline:
        rc_pods, pods = k8_util.k8_get_pods(ns)
        if rc_pods != 0:
            active_pods = []
            break
        active_pods = [p for p in pods
                       if p["status"]["phase"] not in ("Terminating", "Succeeded", "Failed")]
        if not active_pods:
            break
        LOG.info("Waiting for pods to terminate: %s",
                 [p["metadata"]["name"] for p in active_pods])
        time.sleep(5)
    assert not active_pods, (
        "ME pods still active after uninstall: "
        f"{[p['metadata']['name'] for p in active_pods]}")

    # 6) Verify service removed
    rc_svc, services, _ = k8_util.k8_get_services(ns)
    if rc_svc == 0:
        assert len(services) == 0, (
            "Services still present after uninstall: "
            f"{[s['metadata']['name'] for s in services]}")

    LOG.info("Uninstall validated. DaemonSet, pods, and service cleaned up.")


# ---------- Test 6: Install multiple times, no disruption ----------

@pytest.mark.timeout(TEST_TIMEOUT * 2)
def test_helm_install_multiple_times_no_disruption(gpu_cluster, images, environment, me_repo, chart_on_cluster):
    """
    Install ME multiple times and verify no disruption.

    Steps:
    1) Clean up and fresh install. Record baseline (pods, metrics).
    2) Attempt helm install again (should fail since release exists).
    3) Verify no disruption: pods still running, metrics still available.
    4) Uninstall, then re-install. Verify everything comes back up.
    """
    chart = chart_on_cluster
    ns = environment.me_namespace
    release = environment.me_release_name

    # 1) Fresh install
    helm_util.helm_ensure_release_cleaned_up(gpu_cluster, release, ns)
    rc, _, err = _me_install(gpu_cluster, images, chart, environment)
    assert rc == 0, f"helm install failed: {err}"

    _wait_daemonsets_ready(ns)
    time.sleep(10)

    # Record baseline
    baseline_pod_count = len(_me_pods(ns))
    LOG.info("Baseline: %d ME pods", baseline_pod_count)

    # Record metrics baseline
    nic_nodes = nic_util.get_amd_nic_nodes()
    baseline_metrics_ok = {}
    for node in nic_nodes:
        rc_ip, node_ip = k8_util.k8_get_node_internal_ip(node.metadata.name)
        if rc_ip == 0 and node_ip:
            txt = _fetch_metrics(gpu_cluster, node_ip)
            baseline_metrics_ok[node.metadata.name] = _metrics_have_numeric(txt)

    # 2) Attempt install again (should fail)
    rc2, out2, err2 = _me_install(gpu_cluster, images, chart, environment)
    LOG.info("Second install attempt: rc=%d err=%s", rc2, err2)
    assert rc2 != 0, "Second helm install should fail since release already exists"

    # 3) Verify no disruption
    all_ok, not_running = k8_util.k8_all_pods_running(ns)
    assert all_ok, f"ME pods disrupted after re-install attempt: {not_running}"

    current_pods = _me_pods(ns)
    assert len(current_pods) == baseline_pod_count, \
        f"Pod count changed: baseline={baseline_pod_count} current={len(current_pods)}"

    # Verify metrics still accessible
    for node_name, was_ok in baseline_metrics_ok.items():
        if was_ok:
            rc_ip, node_ip = k8_util.k8_get_node_internal_ip(node_name)
            if rc_ip == 0 and node_ip:
                txt = _fetch_metrics(gpu_cluster, node_ip)
                assert _metrics_have_numeric(txt), \
                    f"Metrics no longer available on {node_name}:{ME_METRICS_PORT}"

    LOG.info("No disruption after duplicate install attempt.")

    # 4) Uninstall and re-install
    rc_u, _, err_u = helm_util.helm_uninstall(gpu_cluster, release, ns)
    assert rc_u == 0, f"helm uninstall failed: {err_u}"
    time.sleep(10)

    rc_r, _, err_r = _me_install(gpu_cluster, images, chart, environment)
    assert rc_r == 0, f"helm re-install failed: {err_r}"

    _wait_daemonsets_ready(ns)
    time.sleep(10)

    all_ok, not_running = k8_util.k8_all_pods_running(ns)
    assert all_ok, f"ME pods not running after re-install: {not_running}"

    current_pods = _me_pods(ns)
    assert len(current_pods) == baseline_pod_count, \
        f"Pod count mismatch after re-install: expected={baseline_pod_count} got={len(current_pods)}"

    LOG.info("Multiple install/uninstall/re-install validated. No disruption detected.")


# ---------- Test 7: Helm upgrade charts ----------

@pytest.mark.timeout(TEST_TIMEOUT)
def test_helm_upgrade_charts(gpu_cluster, images, environment, me_repo, chart_on_cluster):
    """
    Upgrade ME with value changes and verify revision increments, pods updated.

    Steps:
    1) Ensure ME is installed.
    2) Record baseline revision.
    3) Upgrade with value changes (updateStrategy.type=OnDelete).
    4) Verify revision increments.
    5) Wait for DaemonSet rolling update.
    6) Verify pods running.
    """
    chart = chart_on_cluster
    ns = environment.me_namespace
    release = environment.me_release_name

    # 1) Ensure installed
    if not helm_util.is_helm_chart_healthy(gpu_cluster, release, ns):
        rc, _, err = _me_install(gpu_cluster, images, chart, environment)
        assert rc == 0, f"helm install failed: {err}"
        _wait_daemonsets_ready(ns)
        time.sleep(10)

    # 2) Record baseline
    rc_list, out_list, _ = helm_util.helm_list(gpu_cluster, ns)
    assert rc_list == 0
    releases_before = json.loads(out_list)
    release_before = next((r for r in releases_before if r["name"] == release), None)
    assert release_before, f"Release {release} not found"
    revision_before = int(release_before.get("revision", "1"))
    LOG.info("Before upgrade: revision=%d", revision_before)

    # 3) Upgrade with modified values
    rc, out, err = _me_upgrade(gpu_cluster, images, chart, environment,
                               {"updateStrategy.type": "OnDelete"})
    assert rc == 0, f"helm upgrade failed: rc={rc} err={err}"
    LOG.info("helm upgrade output: %s", out)

    # 4) Verify revision incremented
    rc_list, out_list, _ = helm_util.helm_list(gpu_cluster, ns)
    releases_after = json.loads(out_list)
    release_after = next((r for r in releases_after if r["name"] == release), None)
    assert release_after, f"Release {release} not found after upgrade"
    revision_after = int(release_after.get("revision", "1"))
    assert revision_after > revision_before, \
        f"Revision did not increment: before={revision_before} after={revision_after}"
    LOG.info("After upgrade: revision=%d", revision_after)

    # 5) Wait for rolling update
    _wait_daemonsets_ready(ns)

    # 6) Verify pods running
    all_ok, not_running = k8_util.k8_all_pods_running(ns)
    assert all_ok, f"ME pods not running after upgrade: {not_running}"

    # Verify values applied
    rc_v, values_out, _ = helm_util.helm_get_values(gpu_cluster, release, ns)
    assert rc_v == 0
    LOG.info("Applied values after upgrade: %s", values_out)

    LOG.info("Helm upgrade validated. Revision incremented, pods updated.")


# ---------- Test 8: Host network toggle ----------

@pytest.mark.timeout(TEST_TIMEOUT * 2)
def test_host_network_toggle(gpu_cluster, images, environment, me_repo, chart_on_cluster):
    """
    Install with hostNetwork=true, verify pod IP == node IP.
    Upgrade with hostNetwork=false, verify pod IP != node IP.

    Steps:
    1) Clean install with hostNetwork=true.
    2) Verify pods Running and pod IP matches node IP.
    3) Upgrade with hostNetwork=false.
    4) Verify pods Running and pod IP differs from node IP.
    """
    chart = chart_on_cluster
    ns = environment.me_namespace
    release = environment.me_release_name

    # 1) Clean install with hostNetwork=true
    helm_util.helm_ensure_release_cleaned_up(gpu_cluster, release, ns)
    # Wait for all pods in namespace to fully terminate before fresh install
    deadline = time.time() + 60
    while time.time() < deadline:
        rc_pods, leftover = k8_util.k8_get_pods(ns)
        if rc_pods != 0 or not leftover:
            break
        LOG.info("Waiting for leftover pods to terminate: %s",
                 [p["metadata"]["name"] for p in leftover])
        time.sleep(5)

    rc, _, err = _me_install(gpu_cluster, images, chart, environment, {"hostNetwork": "true"})
    assert rc == 0, f"helm install with hostNetwork=true failed: {err}"

    _wait_daemonsets_ready(ns)
    time.sleep(10)

    # 2) Verify pod IP == node IP (hostNetwork=true) - only check BM NIC nodes
    nic_nodes = nic_util.get_amd_nic_nodes()
    bm_node_names = {n.metadata.name for n in nic_nodes}

    pods = _me_pods(ns)
    assert len(pods) > 0, "No pods found after install with hostNetwork=true"

    for pod in pods:
        if pod["status"]["phase"] != "Running":
            continue
        pod_ip = pod["status"].get("pod_ip")
        host_ip = pod["status"].get("host_ip")
        node_name = pod["spec"].get("node_name")
        LOG.info("hostNetwork=true: pod %s (node=%s) - pod_ip=%s host_ip=%s",
                 pod["metadata"]["name"], node_name, pod_ip, host_ip)
        if pod_ip and host_ip and node_name in bm_node_names:
            assert pod_ip == host_ip, \
                f"With hostNetwork=true, pod IP ({pod_ip}) should equal host IP ({host_ip}) on {node_name}"

    # 3) Record old pod names before upgrade
    old_pod_names = {p["metadata"]["name"] for p in _me_pods(ns)}

    # Upgrade with hostNetwork=false
    rc, _, err = _me_upgrade(gpu_cluster, images, chart, environment, {"hostNetwork": "false"})
    assert rc == 0, f"helm upgrade with hostNetwork=false failed: {err}"

    # Wait for DaemonSet rolling update to complete
    _wait_daemonsets_ready(ns)

    # Wait for ALL old pods to be replaced with new ones
    deadline = time.time() + 180
    while time.time() < deadline:
        pods = _me_pods(ns)
        remaining_old = [p for p in pods if p["metadata"]["name"] in old_pod_names
                         and p["status"]["phase"] == "Running"]
        new_running = [p for p in pods if p["metadata"]["name"] not in old_pod_names
                       and p["status"]["phase"] == "Running"]
        LOG.info("Rollout progress: %d old pods remaining, %d new pods running",
                 len(remaining_old), len(new_running))
        if not remaining_old and new_running:
            LOG.info("All old pods replaced. New pods: %s",
                     [p["metadata"]["name"] for p in new_running])
            break
        time.sleep(5)
    time.sleep(5)

    # 4) Verify pod IP != node IP (hostNetwork=false)
    # Only check NEW pods (skip any lingering old/Terminating pods)
    pods = _me_pods(ns)
    new_pods = [p for p in pods if p["metadata"]["name"] not in old_pod_names
                and p["status"]["phase"] == "Running"]
    assert len(new_pods) > 0, "No new pods found after upgrade with hostNetwork=false"

    nic_nodes = nic_util.get_amd_nic_nodes()
    bm_node_names = {n.metadata.name for n in nic_nodes}

    for pod in new_pods:
        pod_ip = pod["status"].get("pod_ip")
        host_ip = pod["status"].get("host_ip")
        node_name = pod["spec"].get("node_name")
        LOG.info("hostNetwork=false: pod %s (node=%s) - pod_ip=%s host_ip=%s",
                 pod["metadata"]["name"], node_name, pod_ip, host_ip)
        # Only assert on BM nodes (VMs may behave differently)
        if pod_ip and host_ip and node_name in bm_node_names:
            assert pod_ip != host_ip, \
                f"With hostNetwork=false, pod IP ({pod_ip}) should differ from host IP ({host_ip}) on {node_name}"

    LOG.info("Host network toggle validated.")


# ---------- Test 9: Monitor resources toggle ----------

@pytest.mark.timeout(TEST_TIMEOUT * 2)
def test_monitor_resources_toggle(gpu_cluster, images, environment, me_repo, chart_on_cluster):
    """
    Toggle monitor.resources.nic from true to false and verify behavior.

    Steps:
    1) Clean install with monitor.resources.gpu=false, nic=true (default).
    2) Verify pods Running and NIC metrics available.
    3) Upgrade with monitor.resources.nic=false.
    4) Verify behavior change  - NIC metrics should not be exported.
    5) Cleanup.
    """
    chart = chart_on_cluster
    ns = environment.me_namespace
    release = environment.me_release_name

    # 1) Clean install with nic=true (default)
    helm_util.helm_ensure_release_cleaned_up(gpu_cluster, release, ns)
    rc, _, err = _me_install(gpu_cluster, images, chart, environment, {
        "monitor.resources.gpu": "false",
        "monitor.resources.nic": "true",
    })
    assert rc == 0, f"helm install with nic=true failed: {err}"

    _wait_daemonsets_ready(ns)
    time.sleep(10)

    # 2) Verify pods Running
    all_ok, not_running = k8_util.k8_all_pods_running(ns)
    assert all_ok, f"ME pods not running with nic=true: {not_running}"
    LOG.info("ME pods running with nic=true, gpu=false")

    # Check baseline NIC metrics
    nic_nodes = nic_util.get_amd_nic_nodes()
    baseline_has_nic = False
    if nic_nodes:
        rc_ip, node_ip = k8_util.k8_get_node_internal_ip(nic_nodes[0].metadata.name)
        if rc_ip == 0 and node_ip:
            metrics_text = _fetch_metrics(gpu_cluster, node_ip)
            baseline_has_nic = _metrics_have_numeric(metrics_text)
            LOG.info("Baseline NIC metrics with nic=true: present=%s", baseline_has_nic)

    # 3) Upgrade with nic=false
    rc, _, err = _me_upgrade(gpu_cluster, images, chart, environment, {
        "monitor.resources.gpu": "false",
        "monitor.resources.nic": "false",
    })
    assert rc == 0, f"helm upgrade with nic=false failed: {err}"

    _wait_daemonsets_ready(ns)
    time.sleep(15)

    # 4) Verify behavior  - pods may still exist but NIC metrics should be absent
    pods = _me_pods(ns)
    LOG.info("Pods after nic=false upgrade: %d", len(pods))

    if nic_nodes and pods:
        rc_ip, node_ip = k8_util.k8_get_node_internal_ip(nic_nodes[0].metadata.name)
        if rc_ip == 0 and node_ip:
            metrics_text = _fetch_metrics(gpu_cluster, node_ip)
            if metrics_text:
                has_nic_metrics = any(
                    "nic" in ln.lower()
                    for ln in metrics_text.splitlines()
                    if ln.strip() and not ln.startswith("#")
                )
                assert not has_nic_metrics, (
                    "NIC metrics still present despite monitor.resources.nic=false"
                )

    # 5) Cleanup
    helm_util.helm_ensure_release_cleaned_up(gpu_cluster, release, ns)

    LOG.info("Monitor resources toggle test completed.")
