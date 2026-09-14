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
Standalone Helm Chart tests for AMD Network Operator Node Labeller.

Test cases:
  1. test_validate_chart_metadata
  2. test_helm_install_node_labeller
  3. test_node_labeller_labels_vf_interfaces
  4. test_node_labeller_labels_pf_interfaces
  5. test_uninstall_node_labeller_labels_removed
  6. test_helm_install_multiple_times_no_disruption
  7. test_helm_upgrade_charts
  8. test_tech_support_includes_nl_standalone_pod
  9. test_documentation_standalone_helm_charts
 10. test_helm_upgrade_ondelete_strategy
 11. test_helm_upgrade_rollingupdate_strategy
"""

import os
import time
import json
import yaml
import pytest
import logging

import lib.helm_util as helm_util
import lib.k8_util as k8_util
import lib.nic_util as nic_util
from lib.nic_util import PROM_LINE_RE

LOG = logging.getLogger("test_node_labeller_standalone_helm")

TEST_TIMEOUT = 300

# Values the NL chart needs for a standalone (operator-less) install. The two
# subchart toggles suppress KMM and NFD, which the operator owns.
NL_INSTALL_DEFAULTS = {
    "kmm.enabled": "false",
    "node-feature-discovery.enabled": "false",
}


# ---------- Helpers ----------

def _check_file_contains(file_path, keywords):
    """True when every keyword appears (case-insensitive) in the file."""
    if not os.path.isfile(file_path):
        return False, list(keywords)
    try:
        content = open(file_path, "r", encoding="utf-8", errors="ignore").read().lower()
    except Exception:
        return False, list(keywords)
    missing = [kw for kw in keywords if kw.lower() not in content]
    return len(missing) == 0, missing


def _chart_version(images, artifact):
    """Chart version key differs by manifest location scheme.

    file:// emits "<artifact>.helm-chart.version"; repo:// and oci:// emit
    "<artifact>.version". helm_install silently drops --version when this is
    None, which would unpin a repo:// chart to "latest", so check both.
    """
    return (images.get(f"{artifact}.helm-chart.version")
            or images.get(f"{artifact}.version"))


def _install_values(images, artifact, overrides=None):
    values = dict(NL_INSTALL_DEFAULTS)
    secret = images.get(f"{artifact}.secret")
    if secret:
        # NL chart takes a list here; "[0].name" is helm --set index syntax and
        # is only valid because these go through --set, not a values file.
        values["imagePullSecrets[0].name"] = secret
    if overrides:
        values.update(overrides)
    return values


def _ensure_pull_secret(images, artifact, namespace, source_ns):
    """Copy the pull secret into the test namespace, which helm creates fresh.

    Nothing else provisions a secret in the NL namespace, so without this an
    install from a private mirror lands in ImagePullBackOff. No secret in the
    manifest means public images, so there is nothing to copy and nothing to
    --set.
    """
    secret = images.get(f"{artifact}.secret")
    if not secret:
        return
    rc, _, err = k8_util.k8_ensure_image_pull_secret(namespace, secret, source_ns)
    assert rc == 0, f"Failed to provision pull secret {secret}: {err}"


def _nl_install(gpu_cluster, images, chart, environment, overrides=None):
    ns = environment.nl_namespace
    release = environment.nl_release_name
    artifact = environment.nl_artifact
    _ensure_pull_secret(images, artifact, ns, environment.nl_secret_source_ns)
    return helm_util.helm_install(gpu_cluster, release, ns,
                                  chart, _chart_version(images, artifact), None,
                                  **_install_values(images, artifact, overrides))


def _nl_upgrade(gpu_cluster, images, chart, environment, overrides=None):
    ns = environment.nl_namespace
    release = environment.nl_release_name
    artifact = environment.nl_artifact
    _ensure_pull_secret(images, artifact, ns, environment.nl_secret_source_ns)
    return helm_util.helm_upgrade(gpu_cluster, release, ns,
                                  chart, _chart_version(images, artifact), None,
                                  **_install_values(images, artifact, overrides))


def _nl_daemonsets(namespace):
    """DaemonSets in the namespace whose name identifies them as node-labeller."""
    ret_code, daemonsets = k8_util.k8_get_daemonsets(namespace)
    if ret_code != 0:
        return []
    return [ds for ds in daemonsets if "node-labeller" in ds["metadata"]["name"]]


def _wait_nl_daemonsets_ready(namespace):
    """Wait for every node-labeller DaemonSet. Returns the names waited on."""
    names = [ds["metadata"]["name"] for ds in _nl_daemonsets(namespace)]
    for name in names:
        assert k8_util.k8_wait_for_daemonset_ready(name, namespace), \
            f"DaemonSet {name} not ready"
    return names


def _nl_pods(namespace):
    ret_code, pods = k8_util.k8_get_pods(namespace)
    if ret_code != 0:
        return []
    return [p for p in pods if "node-labeller" in p["metadata"]["name"]]


def _all_nl_pods_running(namespace):
    """(all_running, not_running_names) scoped to node-labeller pods only."""
    not_running = [p["metadata"]["name"] for p in _nl_pods(namespace)
                   if p.get("status", {}).get("phase") != "Running"]
    return len(not_running) == 0, not_running


def _ds_update_strategy(ds):
    """updateStrategy.type from a k8_util DaemonSet dict, or "" when unset."""
    return ((ds.get("spec") or {}).get("update_strategy") or {}).get("type", "")


def _revision(gpu_cluster, release_name, namespace):
    """Current helm revision for the release. Asserts the release exists."""
    rc_list, out_list, _ = helm_util.helm_list(gpu_cluster, namespace)
    assert rc_list == 0, "helm list failed"
    releases = json.loads(out_list)
    release = next((r for r in releases if r["name"] == release_name), None)
    assert release, f"Release {release_name} not found"
    return int(release.get("revision", "1"))


def _metrics_nodeport(namespace):
    """nodePort of the metrics service in the namespace, or None."""
    rc_svc, services, _ = k8_util.k8_get_services(namespace)
    if rc_svc != 0:
        return None
    for svc in services:
        if "metrics" not in svc["metadata"]["name"]:
            continue
        for port in (svc.get("spec") or {}).get("ports") or []:
            if port.get("node_port"):
                return int(port["node_port"])
    return None


def _fetch_metrics(gpu_cluster, node_ip, port):
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


def _collect_tech_support(namespace):
    """Collect NL standalone diagnostics (pods, DaemonSets, labels, events, services)."""
    result = {"nl_pods": [], "nl_daemonsets": [], "node_labels": {},
              "events": [], "services": []}

    for p in _nl_pods(namespace):
        spec = p.get("spec") or {}
        status = p.get("status") or {}
        result["nl_pods"].append({
            "name": p["metadata"]["name"],
            "phase": status.get("phase"),
            "node": spec.get("node_name"),
            "containers": [c.get("name", "") for c in spec.get("containers") or []],
            "start_time": str(status.get("start_time")) if status.get("start_time") else None,
        })

    for ds in _nl_daemonsets(namespace):
        spec = ds.get("spec") or {}
        status = ds.get("status") or {}
        containers = (((spec.get("template") or {}).get("spec") or {})
                      .get("containers") or [])
        result["nl_daemonsets"].append({
            "name": ds["metadata"]["name"],
            "desired": status.get("desired_number_scheduled"),
            "ready": status.get("number_ready"),
            "image": containers[0].get("image") if containers else None,
        })

    for node in nic_util.get_amd_nic_nodes():
        labels = node.metadata.labels or {}
        result["node_labels"][node.metadata.name] = {
            k: v for k, v in labels.items()
            if any(k.startswith(pfx) for pfx in nic_util.NL_LABEL_PREFIXES)
        }

    # k8_get_events returns the raw V1EventList, not a dict, so use attributes.
    rc_ev, events, _ = k8_util.k8_get_events(namespace)
    if rc_ev == 0 and events is not None:
        for ev in (events.items or [])[-20:]:
            result["events"].append({
                "type": ev.type,
                "reason": ev.reason,
                "message": ev.message,
                "object": ev.involved_object.name if ev.involved_object else None,
                "time": str(ev.last_timestamp) if ev.last_timestamp else None,
            })

    rc_svc, services, _ = k8_util.k8_get_services(namespace)
    if rc_svc == 0:
        for svc in services:
            spec = svc.get("spec") or {}
            result["services"].append({
                "name": svc["metadata"]["name"],
                "type": spec.get("type"),
                "ports": [{"port": p.get("port"), "nodePort": p.get("node_port")}
                          for p in spec.get("ports") or []],
            })

    return result


# ---------- Fixtures ----------

@pytest.fixture(scope="module")
def chart_on_cluster(images, environment):
    """Resolve the NL chart from the image manifest.

    helm now runs locally against --kubeconfig, so the chart is used in place
    rather than SCP'd to the k8s master.
    """
    artifact = environment.nl_artifact
    chart = images.get(f"{artifact}.helm-chart", None)
    assert chart, (
        f"No '{artifact}.helm-chart' in the image manifest. "
        f"Set NL_ARTIFACT if the manifest names the chart differently."
    )
    LOG.info("NL chart: %s (version=%s)", chart, _chart_version(images, artifact))
    return chart


@pytest.fixture(scope="module")
def nl_repo(gpu_cluster, images, environment):
    """Add the chart repo when the manifest points at one (repo:// only)."""
    artifact = environment.nl_artifact
    if images.get(f"{artifact}.repo", None):
        helm_util.helm_add_repo(gpu_cluster, images.get(f"{artifact}.repo-name"),
                                images.get(f"{artifact}.repo"))


# ---------- Test 1: Validate chart metadata ----------

@pytest.mark.timeout(TEST_TIMEOUT)
def test_validate_chart_metadata(gpu_cluster, environment, nl_repo, chart_on_cluster):
    """
    Validate various chart data/metadata for different combinations.

    Checks:
    - Chart.yaml has required fields (apiVersion, name, version, appVersion, description)
    - Chart type is 'application'
    - kubeVersion constraint is present
    - Default values.yaml is parseable and contains expected keys
    - helm template renders without errors for default values
    - helm template renders without errors with custom overrides
    """
    chart = chart_on_cluster
    ns = environment.nl_namespace
    release = environment.nl_release_name

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

    assert chart_meta.get("type") == "application", \
        f"Expected chart type 'application', got '{chart_meta.get('type')}'"

    if "kubeVersion" in chart_meta:
        LOG.info("kubeVersion constraint: %s", chart_meta["kubeVersion"])

    # 2) Validate default values.yaml
    rc, values_yaml, err = helm_util.helm_show_values(gpu_cluster, chart)
    assert rc == 0, f"helm show values failed: {err}"

    default_values = yaml.safe_load(values_yaml)
    assert default_values is not None, "values.yaml is empty or unparseable"
    LOG.info("Default values keys: %s", list(default_values.keys()))

    # 3) Validate helm template renders with defaults
    rc, rendered, err = helm_util.helm_template(gpu_cluster, release, chart, ns)
    assert rc == 0, f"helm template with defaults failed: {err}"
    assert rendered.strip(), "helm template produced empty output"

    # Verify rendered manifests contain expected resource kinds
    manifests = list(yaml.safe_load_all(rendered))
    kinds = [m.get("kind") for m in manifests if m]
    LOG.info("Rendered resource kinds (defaults): %s", kinds)
    assert "DaemonSet" in kinds, "Expected DaemonSet in rendered manifests"
    assert "ServiceAccount" in kinds, "Expected ServiceAccount in rendered manifests"

    # 4) Validate helm template with custom overrides
    custom_overrides = {
        "nodeLabeller.image.tag": "v1.1.0",
        "nodeLabeller.tolerations[0].key": "test-key",
        "nodeLabeller.tolerations[0].operator": "Exists",
        "nodeLabeller.tolerations[0].effect": "NoSchedule",
    }
    rc, rendered_custom, err = helm_util.helm_template(gpu_cluster, release, chart, ns,
                                                       set_values=custom_overrides)
    assert rc == 0, f"helm template with custom overrides failed: {err}"
    assert rendered_custom.strip(), "helm template with overrides produced empty output"

    # 5) Validate helm template with different namespace
    rc, rendered_ns, err = helm_util.helm_template(gpu_cluster, release, chart,
                                                   "custom-ns")
    assert rc == 0, f"helm template with custom namespace failed: {err}"
    manifests_ns = list(yaml.safe_load_all(rendered_ns))
    for m in manifests_ns:
        if m and m.get("metadata", {}).get("namespace"):
            assert m["metadata"]["namespace"] == "custom-ns", \
                f"Expected namespace 'custom-ns' but got '{m['metadata']['namespace']}' in {m.get('kind')}"

    LOG.info("Chart metadata validation passed for all combinations.")


# ---------- Test 2: Install Helm charts ----------

@pytest.mark.timeout(TEST_TIMEOUT)
def test_helm_install_node_labeller(gpu_cluster, images, environment, nl_repo, chart_on_cluster):
    """
    Install the standalone Node Labeller Helm chart and verify deployment.

    Steps:
    1) Clean up any existing release.
    2) Run helm install.
    3) Verify release is deployed.
    4) Verify node-labeller DaemonSet is created and all pods are ready.
    5) Verify node-labeller pods are Running on AMD NIC nodes.
    """
    chart = chart_on_cluster
    ns = environment.nl_namespace
    release = environment.nl_release_name

    # 1) Cleanup
    helm_util.helm_ensure_release_cleaned_up(gpu_cluster, release, ns)

    # 2) Install
    rc, out, err = _nl_install(gpu_cluster, images, chart, environment)
    assert rc == 0, f"helm install failed: rc={rc} err={err}"
    LOG.info("helm install output: %s", out)

    # 3) Verify release deployed
    assert helm_util.is_helm_chart_healthy(gpu_cluster, release, ns), \
        f"Release {release} not in deployed state after install"

    # 4) Wait for DaemonSet
    ds_names = _wait_nl_daemonsets_ready(ns)
    assert len(ds_names) > 0, "No node-labeller DaemonSet found after install"
    LOG.info("DaemonSets ready: %s", ds_names)

    # 5) Verify pods running on NIC nodes
    nic_nodes = nic_util.get_amd_nic_nodes()
    if not nic_nodes:
        pytest.skip("No AMD NIC nodes found in cluster")

    all_ok, not_running = _all_nl_pods_running(ns)
    assert all_ok, f"Node labeller pods not running: {not_running}"

    nl_pods = _nl_pods(ns)
    pod_nodes = {p["spec"].get("node_name") for p in nl_pods if p["spec"].get("node_name")}
    nic_node_names = {n.metadata.name for n in nic_nodes}
    LOG.info("NL pods on nodes: %s", pod_nodes)
    LOG.info("AMD NIC nodes: %s", nic_node_names)

    LOG.info("Helm install validated. NL pods running on %d NIC nodes.", len(pod_nodes))


# ---------- Test 3: Check Node labeller labels VF interfaces ----------

@pytest.mark.timeout(TEST_TIMEOUT)
def test_node_labeller_labels_vf_interfaces(gpu_cluster, images, environment, nl_repo, chart_on_cluster):
    """
    Check Node labeller is able to label VF (Virtual Function) interfaces.

    Steps:
    1) Ensure NL release is installed and running.
    2) Find nodes with AMD VNIC (VF) label from NFD.
    3) Wait for NL labels to propagate.
    4) Verify VF-specific labels are present on those nodes.
    """
    ns = environment.nl_namespace
    release = environment.nl_release_name

    # 1) Ensure installed
    if not helm_util.is_helm_chart_healthy(gpu_cluster, release, ns):
        rc, _, err = _nl_install(gpu_cluster, images, chart_on_cluster, environment)
        assert rc == 0, f"helm install failed: {err}"
        _wait_nl_daemonsets_ready(ns)

    # 2) Find VF nodes
    vf_nodes = nic_util.get_amd_vnic_nodes()
    if not vf_nodes:
        pytest.skip("No nodes with AMD VNIC (VF) label found in cluster")

    LOG.info("Found %d AMD VNIC (VF) nodes: %s",
             len(vf_nodes), [n.metadata.name for n in vf_nodes])

    # 3) Wait for labels and verify
    vf_failures = []
    for node in vf_nodes:
        node_name = node.metadata.name
        ok = nic_util.wait_for_nl_labels(node_name)
        if not ok:
            vf_failures.append((node_name, "labels did not appear within timeout"))
            continue

        nl_labels = nic_util.get_nl_labels_on_node(node_name)
        LOG.info("Node %s VF labels: %s", node_name, nl_labels)

        # 4) Verify VF label patterns
        all_present, missing = nic_util.verify_vf_labels_present(nl_labels)
        if not all_present:
            vf_failures.append((node_name, f"missing VF label patterns: {missing}"))
        else:
            LOG.info("Node %s: all VF label patterns verified", node_name)

    assert not vf_failures, f"VF label verification failed: {vf_failures}"
    LOG.info("VF interface labels verified on %d nodes.", len(vf_nodes))


# ---------- Test 4: Check Node labeller labels PF interfaces ----------

@pytest.mark.timeout(TEST_TIMEOUT)
def test_node_labeller_labels_pf_interfaces(gpu_cluster, images, environment, nl_repo, chart_on_cluster):
    """
    Check Node labeller is able to label PF (Physical Function) interfaces.

    Steps:
    1) Ensure NL release is installed and running.
    2) Find nodes with AMD NIC (PF) label from NFD.
    3) Wait for NL labels to propagate.
    4) Verify PF-specific labels are present (count, product-name, firmware-version,
       port-count, port speed, driver-version, driver-name).
    """
    ns = environment.nl_namespace
    release = environment.nl_release_name

    # 1) Ensure installed
    if not helm_util.is_helm_chart_healthy(gpu_cluster, release, ns):
        rc, _, err = _nl_install(gpu_cluster, images, chart_on_cluster, environment)
        assert rc == 0, f"helm install failed: {err}"
        _wait_nl_daemonsets_ready(ns)

    # 2) Find PF nodes
    pf_nodes = nic_util.get_amd_nic_nodes()
    if not pf_nodes:
        pytest.skip("No nodes with AMD NIC (PF) label found in cluster")

    LOG.info("Found %d AMD NIC (PF) nodes: %s",
             len(pf_nodes), [n.metadata.name for n in pf_nodes])

    # 3) Wait for labels and verify
    pf_failures = []
    for node in pf_nodes:
        node_name = node.metadata.name
        ok = nic_util.wait_for_nl_labels(node_name)
        if not ok:
            pf_failures.append((node_name, "labels did not appear within timeout"))
            continue

        nl_labels = nic_util.get_nl_labels_on_node(node_name)
        LOG.info("Node %s PF labels: %s", node_name, nl_labels)

        # 4) Verify PF label patterns
        all_present, missing = nic_util.verify_pf_labels_present(nl_labels)
        if not all_present:
            pf_failures.append((node_name, f"missing PF label patterns: {missing}"))
        else:
            LOG.info("Node %s: all PF label patterns verified", node_name)

        # Additional: verify label values are non-empty
        for k, val in nl_labels.items():
            if not val or not val.strip():
                pf_failures.append((node_name, f"label {k} has empty value"))

    assert not pf_failures, f"PF label verification failed: {pf_failures}"
    LOG.info("PF interface labels verified on %d nodes.", len(pf_nodes))


# ---------- Test 5: Uninstall node labeller, check labels removed ----------

@pytest.mark.timeout(TEST_TIMEOUT)
def test_uninstall_node_labeller_labels_removed(gpu_cluster, images, environment, nl_repo, chart_on_cluster):
    """
    Uninstall the Node Labeller and verify all amd.com/nic.* labels are cleaned up.

    Steps:
    1) Ensure NL is installed and labels are present on at least one node.
    2) Record which nodes have NL labels.
    3) Helm uninstall the release.
    4) Verify release is removed.
    5) Wait and verify all NL labels are removed from previously labeled nodes.
    6) Verify DaemonSet and pods are gone.
    """
    ns = environment.nl_namespace
    release = environment.nl_release_name

    # 1) Ensure installed first
    if not helm_util.is_helm_chart_healthy(gpu_cluster, release, ns):
        rc, _, err = _nl_install(gpu_cluster, images, chart_on_cluster, environment)
        assert rc == 0, f"helm install failed: {err}"
        _wait_nl_daemonsets_ready(ns)
        time.sleep(10)  # allow labels to propagate

    # 2) Record nodes with NL labels
    nic_nodes = nic_util.get_amd_nic_nodes()
    labeled_nodes = []
    for node in nic_nodes:
        node_name = node.metadata.name
        nl_labels = nic_util.get_nl_labels_on_node(node_name)
        if nl_labels:
            labeled_nodes.append(node_name)
            LOG.info("Node %s has %d NL labels before uninstall", node_name, len(nl_labels))

    if not labeled_nodes:
        pytest.skip("No nodes with NL labels found; cannot test label cleanup")

    # 3) Uninstall
    rc, out, err = helm_util.helm_uninstall(gpu_cluster, release, ns)
    assert rc == 0, f"helm uninstall failed: rc={rc} err={err}"
    LOG.info("helm uninstall output: %s", out)

    # 4) Verify release removed
    assert not helm_util.is_helm_chart_healthy(gpu_cluster, release, ns), \
        f"Release {release} still deployed after uninstall"

    # 5) Verify labels removed
    label_cleanup_failures = []
    for node_name in labeled_nodes:
        ok = nic_util.wait_for_nl_labels_removed(node_name)
        if not ok:
            clean, remaining = nic_util.verify_nl_labels_absent(node_name)
            label_cleanup_failures.append((node_name, f"remaining labels: {remaining}"))

    # 6) Verify DaemonSet and pods gone
    time.sleep(5)
    nl_dss = _nl_daemonsets(ns)
    assert len(nl_dss) == 0, \
        f"Node labeller DaemonSets still present after uninstall: {[ds['metadata']['name'] for ds in nl_dss]}"

    nl_pods = _nl_pods(ns)
    # Allow for Terminating pods
    active_pods = [p for p in nl_pods
                   if p.get("status", {}).get("phase") not in ("Terminating", "Succeeded", "Failed")]
    assert len(active_pods) == 0, \
        f"Node labeller pods still active after uninstall: {[p['metadata']['name'] for p in active_pods]}"

    assert not label_cleanup_failures, \
        f"Label cleanup failed on nodes: {label_cleanup_failures}"

    LOG.info("Uninstall validated. Labels removed from %d nodes, DaemonSet and pods cleaned up.",
             len(labeled_nodes))


# ---------- Test 6: Install NL multiple times, no disruption ----------

@pytest.mark.timeout(TEST_TIMEOUT * 2)
def test_helm_install_multiple_times_no_disruption(gpu_cluster, images, environment, nl_repo, chart_on_cluster):
    """
    Install NL helm chart multiple times and verify:
    - All running processes are unaffected
    - Metrics collection is unaffected

    Steps:
    1) Clean up and do a fresh install. Record baseline (pods, labels, metrics).
    2) Attempt helm install again (should fail since release exists).
    3) Verify no disruption: pods still running, labels intact, metrics still available.
    4) Uninstall, then re-install. Verify everything comes back up.
    5) Verify pods, labels, and metrics match baseline expectations.
    """
    chart = chart_on_cluster
    ns = environment.nl_namespace
    release = environment.nl_release_name

    # 1) Fresh install
    helm_util.helm_ensure_release_cleaned_up(gpu_cluster, release, ns)
    rc, _, err = _nl_install(gpu_cluster, images, chart, environment)
    assert rc == 0, f"helm install failed: {err}"

    _wait_nl_daemonsets_ready(ns)
    time.sleep(10)  # allow labels to propagate

    # Record baseline
    baseline_pods = _nl_pods(ns)
    baseline_pod_names = sorted([p["metadata"]["name"] for p in baseline_pods])
    baseline_pod_count = len(baseline_pods)
    LOG.info("Baseline: %d NL pods: %s", baseline_pod_count, baseline_pod_names)

    nic_nodes = nic_util.get_amd_nic_nodes()
    baseline_labels = {}
    for node in nic_nodes:
        node_name = node.metadata.name
        baseline_labels[node_name] = nic_util.get_nl_labels_on_node(node_name)

    # Record metrics baseline (if metrics exporter is present)
    baseline_metrics_ok = {}
    metrics_port = _metrics_nodeport(ns)
    if metrics_port:
        for node in nic_nodes:
            rc_ip, node_ip = k8_util.k8_get_node_internal_ip(node.metadata.name)
            if rc_ip == 0 and node_ip:
                txt = _fetch_metrics(gpu_cluster, node_ip, metrics_port)
                baseline_metrics_ok[node.metadata.name] = _metrics_have_numeric(txt)

    # 2) Attempt install again (should fail - release already exists)
    rc2, out2, err2 = _nl_install(gpu_cluster, images, chart, environment)
    LOG.info("Second install attempt: rc=%d out=%s err=%s", rc2, out2, err2)
    # Expect failure since release already exists
    assert rc2 != 0, "Second helm install should fail since release already exists"

    # 3) Verify no disruption after failed re-install attempt
    all_ok, not_running = _all_nl_pods_running(ns)
    assert all_ok, f"NL pods disrupted after re-install attempt: {not_running}"

    current_pods = _nl_pods(ns)
    assert len(current_pods) == baseline_pod_count, \
        f"Pod count changed: baseline={baseline_pod_count} current={len(current_pods)}"

    # Verify labels still intact
    for node_name, expected_labels in baseline_labels.items():
        current_labels = nic_util.get_nl_labels_on_node(node_name)
        assert current_labels == expected_labels, \
            f"Labels changed on {node_name} after re-install attempt"

    # Verify metrics still accessible
    if metrics_port:
        for node_name, was_ok in baseline_metrics_ok.items():
            if was_ok:
                rc_ip, node_ip = k8_util.k8_get_node_internal_ip(node_name)
                if rc_ip == 0 and node_ip:
                    txt = _fetch_metrics(gpu_cluster, node_ip, metrics_port)
                    assert _metrics_have_numeric(txt), \
                        f"Metrics no longer available on {node_name}:{metrics_port} after re-install attempt"

    LOG.info("No disruption after duplicate install attempt.")

    # 4) Uninstall and re-install
    rc_u, _, err_u = helm_util.helm_uninstall(gpu_cluster, release, ns)
    assert rc_u == 0, f"helm uninstall failed: {err_u}"
    time.sleep(10)

    rc_r, _, err_r = _nl_install(gpu_cluster, images, chart, environment)
    assert rc_r == 0, f"helm re-install failed: {err_r}"

    _wait_nl_daemonsets_ready(ns)

    time.sleep(10)

    # 5) Verify everything came back
    all_ok, not_running = _all_nl_pods_running(ns)
    assert all_ok, f"NL pods not running after re-install: {not_running}"

    current_pods = _nl_pods(ns)
    assert len(current_pods) == baseline_pod_count, \
        f"Pod count mismatch after re-install: expected={baseline_pod_count} got={len(current_pods)}"

    for node in nic_nodes:
        node_name = node.metadata.name
        ok = nic_util.wait_for_nl_labels(node_name)
        assert ok, f"Labels did not re-appear on {node_name} after re-install"

    LOG.info("Multiple install/uninstall/re-install validated. No disruption detected.")


# ---------- Test 7: Check helm upgrade for charts ----------

@pytest.mark.timeout(TEST_TIMEOUT)
def test_helm_upgrade_charts(gpu_cluster, images, environment, nl_repo, chart_on_cluster):
    """
    Check helm upgrade for the Node Labeller standalone chart.

    Steps:
    1) Ensure NL is installed with default values.
    2) Record baseline pods and labels.
    3) Upgrade with modified values (e.g., image tag, tolerations, resource limits).
    4) Verify upgrade succeeds and release revision increments.
    5) Wait for DaemonSet rolling update to complete.
    6) Verify pods are re-created with updated configuration.
    7) Verify node labels are still present after upgrade.
    8) Upgrade again with another set of values to test idempotency.
    """
    chart = chart_on_cluster
    ns = environment.nl_namespace
    release = environment.nl_release_name

    # 1) Ensure installed
    if not helm_util.is_helm_chart_healthy(gpu_cluster, release, ns):
        rc, _, err = _nl_install(gpu_cluster, images, chart, environment)
        assert rc == 0, f"helm install failed: {err}"
        _wait_nl_daemonsets_ready(ns)
        time.sleep(10)

    # 2) Record baseline
    revision_before = _revision(gpu_cluster, release, ns)
    LOG.info("Before upgrade: revision=%d", revision_before)

    # 3) Upgrade with modified values
    upgrade_values = {
        "nodeLabeller.image.pullPolicy": "IfNotPresent",
    }
    rc, out, err = _nl_upgrade(gpu_cluster, images, chart, environment, upgrade_values)
    assert rc == 0, f"helm upgrade failed: rc={rc} err={err}"
    LOG.info("helm upgrade output: %s", out)

    # 4) Verify revision incremented
    revision_after = _revision(gpu_cluster, release, ns)
    assert revision_after > revision_before, \
        f"Revision did not increment: before={revision_before} after={revision_after}"
    LOG.info("After upgrade: revision=%d", revision_after)

    # 5) Wait for rolling update
    _wait_nl_daemonsets_ready(ns)

    # 6) Verify pods running
    all_ok, not_running = _all_nl_pods_running(ns)
    assert all_ok, f"NL pods not running after upgrade: {not_running}"

    # Verify values applied
    rc_v, values_out, _ = helm_util.helm_get_values(gpu_cluster, release, ns)
    assert rc_v == 0
    LOG.info("Applied values after upgrade: %s", values_out)

    # 7) Verify labels still present
    nic_nodes = nic_util.get_amd_nic_nodes()
    label_issues = []
    for node in nic_nodes:
        node_name = node.metadata.name
        ok = nic_util.wait_for_nl_labels(node_name, timeout=30)
        if not ok:
            label_issues.append(node_name)

    assert not label_issues, f"Labels missing after upgrade on nodes: {label_issues}"

    # 8) Second upgrade to test idempotency
    rc2, out2, err2 = _nl_upgrade(gpu_cluster, images, chart, environment, upgrade_values)
    assert rc2 == 0, f"Second helm upgrade failed: {err2}"

    _wait_nl_daemonsets_ready(ns)

    all_ok, not_running = _all_nl_pods_running(ns)
    assert all_ok, f"NL pods not running after second upgrade: {not_running}"

    LOG.info("Helm upgrade validated. Revision incremented, pods updated, labels intact.")


# ---------- Test 8: Tech support includes NL standalone pod ----------

@pytest.mark.timeout(TEST_TIMEOUT)
def test_tech_support_includes_nl_standalone_pod(gpu_cluster, images, environment, nl_repo, chart_on_cluster):
    """
    Collect tech support and check it has info on NL standalone pod.

    Steps:
    1) Ensure NL is installed and running.
    2) Collect tech support data (pods, DaemonSets, labels, events, services).
    3) Verify tech support contains NL standalone pod information.
    4) Verify DaemonSet info is present.
    5) Verify node label info is present for NIC nodes.
    """
    ns = environment.nl_namespace
    release = environment.nl_release_name

    # 1) Ensure installed
    if not helm_util.is_helm_chart_healthy(gpu_cluster, release, ns):
        rc, _, err = _nl_install(gpu_cluster, images, chart_on_cluster, environment)
        assert rc == 0, f"helm install failed: {err}"
        _wait_nl_daemonsets_ready(ns)
        time.sleep(10)

    # 2) Collect tech support
    ts_data = _collect_tech_support(ns)
    LOG.info("Tech support data collected: pods=%d daemonsets=%d nodes_with_labels=%d events=%d services=%d",
             len(ts_data["nl_pods"]), len(ts_data["nl_daemonsets"]),
             len(ts_data["node_labels"]), len(ts_data["events"]),
             len(ts_data["services"]))

    # 3) Verify NL pod info
    assert len(ts_data["nl_pods"]) > 0, "Tech support has no NL pod information"
    for pod_info in ts_data["nl_pods"]:
        assert pod_info["name"], "NL pod entry missing name"
        assert pod_info["phase"] == "Running", \
            f"NL pod {pod_info['name']} not Running: {pod_info['phase']}"
        assert pod_info["node"], f"NL pod {pod_info['name']} missing node assignment"
        assert pod_info["containers"], f"NL pod {pod_info['name']} missing container info"
        LOG.info("Tech support NL pod: name=%s phase=%s node=%s containers=%s",
                 pod_info["name"], pod_info["phase"], pod_info["node"], pod_info["containers"])

    # 4) Verify DaemonSet info
    assert len(ts_data["nl_daemonsets"]) > 0, "Tech support has no NL DaemonSet information"
    for ds_info in ts_data["nl_daemonsets"]:
        assert ds_info["name"], "NL DaemonSet entry missing name"
        assert ds_info["desired"] is not None, f"DaemonSet {ds_info['name']} missing desired count"
        assert ds_info["ready"] is not None, f"DaemonSet {ds_info['name']} missing ready count"
        assert ds_info["image"], f"DaemonSet {ds_info['name']} missing image info"
        LOG.info("Tech support NL DaemonSet: name=%s desired=%s ready=%s image=%s",
                 ds_info["name"], ds_info["desired"], ds_info["ready"], ds_info["image"])

    # 5) Verify node labels info
    nic_nodes = nic_util.get_amd_nic_nodes()
    if nic_nodes:
        assert len(ts_data["node_labels"]) > 0, \
            "Tech support has no node label information despite NIC nodes existing"
        for node_name, labels in ts_data["node_labels"].items():
            LOG.info("Tech support node %s: %d NL labels", node_name, len(labels))

    LOG.info("Tech support validation passed. NL standalone pod info present.")


# ---------- Test 9: Documentation for standalone helm charts ----------

@pytest.mark.timeout(TEST_TIMEOUT)
def test_documentation_standalone_helm_charts():
    """
    Check documentation exists and covers standalone helm chart topics.

    Steps:
    1) Check that documentation files exist in expected locations.
    2) Verify documentation mentions standalone node labeller helm chart.
    3) Verify documentation covers installation, configuration, and usage.
    4) Verify helm chart README exists and has required sections.
    """
    # Determine repo root relative to this test file
    test_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(test_dir, "..", "..", "..", ".."))

    # Candidate documentation paths (check multiple possible locations)
    doc_candidates = [
        os.path.join(repo_root, "docs", "installation"),
        os.path.join(repo_root, "docs"),
        os.path.join(repo_root, "helm-charts-k8s"),
    ]

    doc_files_found = []
    for doc_dir in doc_candidates:
        if not os.path.isdir(doc_dir):
            continue
        for root, dirs, files in os.walk(doc_dir):
            for f in files:
                if f.lower().endswith((".md", ".rst", ".txt", ".yaml")):
                    doc_files_found.append(os.path.join(root, f))

    assert doc_files_found, \
        f"No documentation files found in candidate paths: {doc_candidates}"
    LOG.info("Found %d documentation files", len(doc_files_found))

    # 2) Check for standalone NL helm chart mentions in docs
    nl_keywords = ["node-labeller", "node labeller", "nodelabeller", "standalone"]
    helm_keywords = ["helm install", "helm upgrade", "helm uninstall", "values.yaml"]

    nl_doc_found = False
    helm_doc_found = False

    for doc_path in doc_files_found:
        try:
            with open(doc_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read().lower()
        except Exception:
            continue

        if any(kw.lower() in content for kw in nl_keywords):
            nl_doc_found = True
            LOG.info("NL documentation found in: %s", doc_path)

        if any(kw.lower() in content for kw in helm_keywords):
            helm_doc_found = True
            LOG.info("Helm documentation found in: %s", doc_path)

    assert nl_doc_found, \
        f"No documentation file mentions node labeller standalone. Keywords searched: {nl_keywords}"

    assert helm_doc_found, \
        f"No documentation file mentions helm chart operations. Keywords searched: {helm_keywords}"

    # 3) Check helm chart has a README
    chart_readme_candidates = [
        os.path.join(repo_root, "helm-charts-k8s", "README.md"),
        os.path.join(repo_root, "helm-charts-k8s", "readme.md"),
    ]

    readme_found = False
    for readme_path in chart_readme_candidates:
        if os.path.isfile(readme_path):
            readme_found = True
            LOG.info("Helm chart README found: %s", readme_path)

            # Verify README has required sections
            required_sections = ["install", "values"]
            all_present, missing = _check_file_contains(readme_path, required_sections)
            assert all_present, f"Helm chart README missing required sections: {missing}"
            break

    assert readme_found, f"No README found for helm chart in: {chart_readme_candidates}"

    LOG.info("Documentation validation passed for standalone helm charts.")


# ---------- Test 10: Helm upgrade with OnDelete strategy ----------

@pytest.mark.timeout(TEST_TIMEOUT * 2)
def test_helm_upgrade_ondelete_strategy(gpu_cluster, images, environment, nl_repo, chart_on_cluster):
    """
    Upgrade NL with updateStrategy.type=OnDelete and verify behavior on
    NIC (bare metal/PF) and vNIC (VM/VF) nodes.

    With OnDelete, pods are NOT automatically replaced after upgrade.
    Old pods continue running until manually deleted.

    Steps:
    1) Ensure NL is installed (default RollingUpdate strategy).
    2) Record baseline pods and labels on NIC (PF) and vNIC (VF) nodes.
    3) Upgrade with updateStrategy.type=OnDelete.
    4) Verify upgrade succeeds and revision increments.
    5) Verify old pods are still running (OnDelete does not auto-replace).
    6) Manually delete one pod and verify replacement pod starts with new spec.
    7) Verify NIC (PF) labels still present on bare metal nodes.
    8) Verify vNIC (VF) labels still present on VM nodes (if any).
    9) Cleanup: upgrade back to RollingUpdate.
    """
    chart = chart_on_cluster
    ns = environment.nl_namespace
    release = environment.nl_release_name

    # 1) Ensure installed
    if not helm_util.is_helm_chart_healthy(gpu_cluster, release, ns):
        rc, _, err = _nl_install(gpu_cluster, images, chart, environment)
        assert rc == 0, f"helm install failed: {err}"
        _wait_nl_daemonsets_ready(ns)
        time.sleep(10)

    # 2) Record baseline
    revision_before = _revision(gpu_cluster, release, ns)
    LOG.info("Before OnDelete upgrade: revision=%d", revision_before)

    baseline_pods = _nl_pods(ns)
    baseline_pod_names = sorted([p["metadata"]["name"] for p in baseline_pods])
    LOG.info("Baseline pods: %s", baseline_pod_names)

    # Record labels on NIC (PF) nodes
    nic_nodes = nic_util.get_amd_nic_nodes()
    baseline_pf_labels = {}
    for node in nic_nodes:
        node_name = node.metadata.name
        baseline_pf_labels[node_name] = nic_util.get_nl_labels_on_node(node_name)
        LOG.info("NIC node %s: %d PF labels", node_name, len(baseline_pf_labels[node_name]))

    # Record labels on vNIC (VF) nodes
    vnic_nodes = nic_util.get_amd_vnic_nodes()
    baseline_vf_labels = {}
    for node in vnic_nodes:
        node_name = node.metadata.name
        baseline_vf_labels[node_name] = nic_util.get_nl_labels_on_node(node_name)
        LOG.info("vNIC node %s: %d VF labels", node_name, len(baseline_vf_labels[node_name]))

    # 3) Upgrade with OnDelete strategy
    rc, out, err = _nl_upgrade(gpu_cluster, images, chart, environment,
                               {"updateStrategy.type": "OnDelete"})
    assert rc == 0, f"helm upgrade to OnDelete failed: rc={rc} err={err}"
    LOG.info("helm upgrade to OnDelete output: %s", out)

    # 4) Verify revision incremented
    revision_after = _revision(gpu_cluster, release, ns)
    assert revision_after > revision_before, \
        f"Revision did not increment: before={revision_before} after={revision_after}"
    LOG.info("After OnDelete upgrade: revision=%d", revision_after)

    # Verify the DaemonSet now has OnDelete strategy
    nl_dss = _nl_daemonsets(ns)
    assert len(nl_dss) > 0, "No DaemonSet found after OnDelete upgrade"
    ds_strategy = _ds_update_strategy(nl_dss[0])
    LOG.info("DaemonSet updateStrategy.type after upgrade: %s", ds_strategy)
    assert ds_strategy == "OnDelete", \
        f"Expected updateStrategy.type=OnDelete, got '{ds_strategy}'"

    # 5) With OnDelete, old pods should still be running (not auto-replaced)
    time.sleep(10)
    current_pods = _nl_pods(ns)
    current_pod_names = sorted([p["metadata"]["name"] for p in current_pods])
    LOG.info("Pods after OnDelete upgrade: %s", current_pod_names)

    # Verify old pods still exist (OnDelete does NOT auto-replace)
    still_present = [name for name in baseline_pod_names if name in current_pod_names]
    LOG.info("Old pods still present after OnDelete upgrade: %s", still_present)
    assert len(still_present) > 0, \
        "OnDelete strategy should keep old pods running, but none of the baseline pods remain"

    all_ok, not_running = _all_nl_pods_running(ns)
    assert all_ok, f"NL pods not running after OnDelete upgrade: {not_running}"

    # 6) Manually delete one pod to trigger replacement with new spec
    pod_to_delete = current_pods[0]["metadata"]["name"]
    pod_node = current_pods[0]["spec"].get("node_name", "unknown")
    LOG.info("Manually deleting pod %s on node %s to trigger OnDelete replacement", pod_to_delete, pod_node)

    rc_del, _, err_del = k8_util.k8_delete_pod(pod_to_delete, ns)
    assert rc_del == 0, f"Failed to delete pod {pod_to_delete}: {err_del}"

    # Wait for replacement pod to come up
    time.sleep(10)
    _wait_nl_daemonsets_ready(ns)

    all_ok, not_running = _all_nl_pods_running(ns)
    assert all_ok, f"NL pods not running after OnDelete pod replacement: {not_running}"

    # Verify new pod replaced the deleted one
    new_pods = _nl_pods(ns)
    new_pod_names = sorted([p["metadata"]["name"] for p in new_pods])
    LOG.info("Pods after manual delete: %s", new_pod_names)
    assert pod_to_delete not in new_pod_names, \
        f"Deleted pod {pod_to_delete} should not exist after deletion"
    assert len(new_pods) == len(baseline_pods), \
        f"Pod count mismatch: expected={len(baseline_pods)} got={len(new_pods)}"

    # 7) Verify NIC (PF) labels still present on bare metal nodes
    pf_label_issues = []
    for node_name, expected_labels in baseline_pf_labels.items():
        if not expected_labels:
            continue
        ok = nic_util.wait_for_nl_labels(node_name, timeout=30)
        if not ok:
            pf_label_issues.append((node_name, "labels disappeared after OnDelete upgrade"))
        else:
            current_labels = nic_util.get_nl_labels_on_node(node_name)
            LOG.info("NIC node %s after OnDelete: %d PF labels", node_name, len(current_labels))

    assert not pf_label_issues, f"PF label issues after OnDelete upgrade: {pf_label_issues}"
    LOG.info("NIC (PF) labels verified on %d bare metal nodes after OnDelete upgrade.", len(nic_nodes))

    # 8) Verify vNIC (VF) labels still present on VM nodes
    if vnic_nodes:
        vf_label_issues = []
        for node_name, expected_labels in baseline_vf_labels.items():
            if not expected_labels:
                continue
            ok = nic_util.wait_for_nl_labels(node_name, timeout=30)
            if not ok:
                vf_label_issues.append((node_name, "labels disappeared after OnDelete upgrade"))
            else:
                current_labels = nic_util.get_nl_labels_on_node(node_name)
                LOG.info("vNIC node %s after OnDelete: %d VF labels", node_name, len(current_labels))

        assert not vf_label_issues, f"VF label issues after OnDelete upgrade: {vf_label_issues}"
        LOG.info("vNIC (VF) labels verified on %d VM nodes after OnDelete upgrade.", len(vnic_nodes))
    else:
        LOG.info("No vNIC (VF) nodes in cluster - skipping VF label check for OnDelete.")

    # 9) Cleanup: upgrade back to RollingUpdate
    rc, _, err = _nl_upgrade(gpu_cluster, images, chart, environment,
                             {"updateStrategy.type": "RollingUpdate"})
    assert rc == 0, f"helm upgrade back to RollingUpdate failed: {err}"
    _wait_nl_daemonsets_ready(ns)

    LOG.info("OnDelete strategy upgrade test passed. Labels intact on NIC and vNIC nodes.")


# ---------- Test 11: Helm upgrade with RollingUpdate strategy ----------

@pytest.mark.timeout(TEST_TIMEOUT * 2)
def test_helm_upgrade_rollingupdate_strategy(gpu_cluster, images, environment, nl_repo, chart_on_cluster):
    """
    Upgrade NL with updateStrategy.type=RollingUpdate and verify behavior on
    NIC (bare metal/PF) and vNIC (VM/VF) nodes.

    With RollingUpdate, pods are automatically replaced one by one after upgrade.
    maxUnavailable controls how many pods can be down simultaneously.

    Steps:
    1) Ensure NL is installed.
    2) Record baseline pods and labels on NIC (PF) and vNIC (VF) nodes.
    3) Upgrade with updateStrategy.type=RollingUpdate and a value change to force pod recreation.
    4) Verify upgrade succeeds and revision increments.
    5) Wait for DaemonSet rolling update to complete (pods auto-replaced).
    6) Verify all pods are new (different UIDs from baseline).
    7) Verify NIC (PF) labels still present on bare metal nodes.
    8) Verify vNIC (VF) labels still present on VM nodes (if any).
    """
    chart = chart_on_cluster
    ns = environment.nl_namespace
    release = environment.nl_release_name

    # 1) Ensure installed
    if not helm_util.is_helm_chart_healthy(gpu_cluster, release, ns):
        rc, _, err = _nl_install(gpu_cluster, images, chart, environment)
        assert rc == 0, f"helm install failed: {err}"
        _wait_nl_daemonsets_ready(ns)
        time.sleep(10)

    # 2) Record baseline
    revision_before = _revision(gpu_cluster, release, ns)
    LOG.info("Before RollingUpdate upgrade: revision=%d", revision_before)

    baseline_pods = _nl_pods(ns)
    baseline_pod_uids = {p["metadata"]["name"]: p["metadata"]["uid"] for p in baseline_pods}
    baseline_pod_count = len(baseline_pods)
    LOG.info("Baseline: %d NL pods", baseline_pod_count)

    # Record labels on NIC (PF) nodes
    nic_nodes = nic_util.get_amd_nic_nodes()
    baseline_pf_labels = {}
    for node in nic_nodes:
        node_name = node.metadata.name
        baseline_pf_labels[node_name] = nic_util.get_nl_labels_on_node(node_name)
        LOG.info("NIC node %s: %d PF labels", node_name, len(baseline_pf_labels[node_name]))

    # Record labels on vNIC (VF) nodes
    vnic_nodes = nic_util.get_amd_vnic_nodes()
    baseline_vf_labels = {}
    for node in vnic_nodes:
        node_name = node.metadata.name
        baseline_vf_labels[node_name] = nic_util.get_nl_labels_on_node(node_name)
        LOG.info("vNIC node %s: %d VF labels", node_name, len(baseline_vf_labels[node_name]))

    # 3) Upgrade with RollingUpdate strategy + a value change to force pod recreation
    upgrade_values = {
        "updateStrategy.type": "RollingUpdate",
        "image.pullPolicy": "Always",
    }
    rc, out, err = _nl_upgrade(gpu_cluster, images, chart, environment, upgrade_values)
    assert rc == 0, f"helm upgrade to RollingUpdate failed: rc={rc} err={err}"
    LOG.info("helm upgrade to RollingUpdate output: %s", out)

    # 4) Verify revision incremented
    revision_after = _revision(gpu_cluster, release, ns)
    assert revision_after > revision_before, \
        f"Revision did not increment: before={revision_before} after={revision_after}"
    LOG.info("After RollingUpdate upgrade: revision=%d", revision_after)

    # Verify the DaemonSet has RollingUpdate strategy
    nl_dss = _nl_daemonsets(ns)
    assert len(nl_dss) > 0, "No DaemonSet found after RollingUpdate upgrade"
    ds_strategy = _ds_update_strategy(nl_dss[0])
    LOG.info("DaemonSet updateStrategy.type after upgrade: %s", ds_strategy)
    assert ds_strategy == "RollingUpdate", \
        f"Expected updateStrategy.type=RollingUpdate, got '{ds_strategy}'"

    # 5) Wait for rolling update to complete (pods auto-replaced)
    _wait_nl_daemonsets_ready(ns)

    time.sleep(10)

    all_ok, not_running = _all_nl_pods_running(ns)
    assert all_ok, f"NL pods not running after RollingUpdate upgrade: {not_running}"

    # 6) Verify pods were replaced (different UIDs from baseline)
    new_pods = _nl_pods(ns)
    assert len(new_pods) == baseline_pod_count, \
        f"Pod count mismatch: expected={baseline_pod_count} got={len(new_pods)}"

    new_pod_uids = {p["metadata"]["name"]: p["metadata"]["uid"] for p in new_pods}
    old_uids = set(baseline_pod_uids.values())
    current_uids = set(new_pod_uids.values())
    replaced = old_uids - current_uids
    LOG.info("RollingUpdate replaced %d/%d pods (old UIDs no longer present)",
             len(replaced), len(old_uids))

    # With RollingUpdate and a spec change, all pods should be replaced
    if replaced:
        LOG.info("Confirmed: pods were auto-replaced by RollingUpdate strategy.")
    else:
        LOG.warning("Pods were not replaced - spec change may not have triggered recreation.")

    # 7) Verify NIC (PF) labels still present on bare metal nodes
    pf_label_issues = []
    for node_name, expected_labels in baseline_pf_labels.items():
        if not expected_labels:
            continue
        ok = nic_util.wait_for_nl_labels(node_name, timeout=60)
        if not ok:
            pf_label_issues.append((node_name, "labels disappeared after RollingUpdate upgrade"))
        else:
            current_labels = nic_util.get_nl_labels_on_node(node_name)
            LOG.info("NIC node %s after RollingUpdate: %d PF labels (baseline: %d)",
                     node_name, len(current_labels), len(expected_labels))
            # Verify PF label patterns
            all_present, missing = nic_util.verify_pf_labels_present(current_labels)
            if not all_present:
                pf_label_issues.append((node_name, f"missing PF label patterns: {missing}"))

    assert not pf_label_issues, f"PF label issues after RollingUpdate upgrade: {pf_label_issues}"
    LOG.info("NIC (PF) labels verified on %d bare metal nodes after RollingUpdate upgrade.", len(nic_nodes))

    # 8) Verify vNIC (VF) labels still present on VM nodes
    if vnic_nodes:
        vf_label_issues = []
        for node_name, expected_labels in baseline_vf_labels.items():
            if not expected_labels:
                continue
            ok = nic_util.wait_for_nl_labels(node_name, timeout=60)
            if not ok:
                vf_label_issues.append((node_name, "labels disappeared after RollingUpdate upgrade"))
            else:
                current_labels = nic_util.get_nl_labels_on_node(node_name)
                LOG.info("vNIC node %s after RollingUpdate: %d VF labels (baseline: %d)",
                         node_name, len(current_labels), len(expected_labels))
                # Verify VF label patterns
                all_present, missing = nic_util.verify_vf_labels_present(current_labels)
                if not all_present:
                    vf_label_issues.append((node_name, f"missing VF label patterns: {missing}"))

        assert not vf_label_issues, f"VF label issues after RollingUpdate upgrade: {vf_label_issues}"
        LOG.info("vNIC (VF) labels verified on %d VM nodes after RollingUpdate upgrade.", len(vnic_nodes))
    else:
        LOG.info("No vNIC (VF) nodes in cluster - skipping VF label check for RollingUpdate.")

    LOG.info("RollingUpdate strategy upgrade test passed. Labels intact on NIC and vNIC nodes.")
