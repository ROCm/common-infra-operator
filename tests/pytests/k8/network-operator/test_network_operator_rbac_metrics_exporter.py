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


import time
import yaml
import pytest
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from kubernetes import client as k8s_client

import lib.nic_util as nic_util
from lib.nic_util import (
    LOCAL_CERT_DIR,
    MAX_WORKERS,
    PROM_LINE_RE,
)

LOG = logging.getLogger(__name__)
TEST_TIMEOUT = 180


# Deliberately still on the raw client: k8_util.k8_get_pods returns List[dict]
# (via .to_dict(), which also snake_cases nested fields), but nic_util consumers
# are typed for V1Pod. Rewiring here alone would break them, so this moves with
# the nic_util signature change.
def list_workloads(v1, namespace="default"):
    pods = v1.list_namespaced_pod(namespace).items
    return [p for p in pods if p.status.phase == "Running"]


NC_NAMESPACE = "kube-amd-network"
NETWORKCONFIG_NAME = "test-networkconfig"
METRICS_SERVICE_NAME = "my-metrics-service"




@pytest.mark.timeout(TEST_TIMEOUT)
def test_rbac_disabled_metrics_fail_pf():
    """
    Test that when RBAC is disabled for PF configs, metrics fetch with mTLS certificates should fail.
    
    Workflow:
    1) Patch non-VF NetworkConfig objects to set spec.metricsExporter.rbacConfig.enable = False.
    2) Trigger IB traffic on PF workload pods.
    3) Try to fetch metrics using mTLS (nic_util.curl_metrics_from_local).
    4) Verify that metrics fetch fails or returns non-Prometheus data (RBAC disabled means mTLS not enforced).
    5) Restore all modified NetworkConfig objects to their originals.
    """
    v1 = k8s_client.CoreV1Api()

    # 1) List NetworkConfig CRs and save originals (PF only)
    try:
        nc_items = nic_util.list_networkconfigs_custom(NC_NAMESPACE)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources in {NC_NAMESPACE}: {e}")

    if not nc_items:
        pytest.skip(f"No NetworkConfig resources in {NC_NAMESPACE}")

    originals = {}
    for it in nc_items:
        name = it.get("metadata", {}).get("name")
        if not name or name.startswith("vf-"):
            continue
        originals[name] = yaml.safe_load(yaml.safe_dump(it))

    if not originals:
        pytest.skip("No PF NetworkConfig found")

    # Build patch to disable rbac
    patch_body = {
        "spec": {
            "metricsExporter": {
                "rbacConfig": {
                    "enable": False
                }
            }
        }
    }

    # Apply patches in parallel
    applied = []
    failed = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(originals)))) as ex:
        futures = {ex.submit(nic_util.patch_networkconfig_custom, NC_NAMESPACE, name, patch_body): name for name in originals.keys()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                fut.result()
                applied.append(name)
                LOG.info("PF Patched %s -> spec.metricsExporter.rbacConfig.enable=False", name)
            except Exception as e:
                LOG.error("Failed to patch PF %s: %s", name, e)
                failed.append(name)

    if failed:
        # rollback applied ones and fail early
        for rn in applied:
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, originals[rn])
            except Exception as re:
                LOG.error("Rollback failed for %s: %s", rn, re)
        pytest.fail(f"Failed to apply RBAC disable patch to some PF NetworkConfigs: {failed}")

    # Give operator time to reconcile
    time.sleep(2)

    # 2) Trigger IB traffic on PF workload pods
    all_pods = list_workloads(v1, namespace="default")
    wpods = [p for p in all_pods if not p.metadata.name.startswith("vf-workload")]
    if not wpods:
        # revert before skipping
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("Failed revert for %s during skip", rn)
        pytest.skip("No running PF workload pods in default namespace")

    exec_outputs = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(wpods)))) as ex:
        futures = {ex.submit(nic_util.run_ib_traffic, p.metadata.name, p.metadata.namespace): p for p in wpods}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                out = fut.result()
                exec_outputs.update(out)
            except Exception as e:
                LOG.warning("nic_util.run_ib_traffic failed for %s: %s", p.metadata.name, e)
                exec_outputs[p.metadata.name] = f"ERROR: {e}"

    # 3) Build map of PF NetworkConfig -> nodePort
    nodeport_by_config = {}
    for it in nic_util.list_networkconfigs_custom(NC_NAMESPACE):
        name = it.get("metadata", {}).get("name")
        if not name or name.startswith("vf-"):
            continue
        try:
            np = nic_util.get_metrics_nodeport_from_networkconfig(NC_NAMESPACE, name)
        except Exception:
            np = None
        nodeport_by_config[name] = np

    cfg = list(nodeport_by_config.keys())[0] if nodeport_by_config else None
    if not cfg:
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("Failed revert during no-config skip")
        pytest.skip("No PF NetworkConfig with nodePort found")

    # 4) Try to fetch metrics with mTLS - should fail when RBAC is disabled
    metrics_failed_correctly = {}
    metrics_succeeded_incorrectly = []

    port = nodeport_by_config.get(cfg)
    if not port:
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("Failed revert during no-port skip")
        pytest.skip(f"No numeric nodePort for PF config {cfg}")

    for p in wpods:
        pod_name = p.metadata.name
        node_name = getattr(p.spec, "node_name", None) or getattr(p.spec, "nodeName", None)
        if not node_name:
            LOG.warning("PF Pod %s has no nodeName; skipping", pod_name)
            continue

        node_ip = nic_util.get_node_ip(node_name)
        if not node_ip:
            LOG.warning("No InternalIP for node %s (PF pod %s)", node_name, pod_name)
            continue

        LOG.info("PF Attempting metrics fetch for pod %s -> node %s (%s) port %s (config=%s) with RBAC disabled", 
                 pod_name, node_name, node_ip, port, cfg)

        # Try to fetch metrics with mTLS - this should fail
        txt = nic_util.curl_metrics_from_local(node_ip, port, METRICS_SERVICE_NAME, timeout=8)
        
        # Check if we got valid Prometheus metrics (which would be incorrect when RBAC is disabled)
        has_valid_metrics = False
        if txt and txt.strip():
            for ln in txt.splitlines():
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                if PROM_LINE_RE.match(ln):
                    has_valid_metrics = True
                    break

        if has_valid_metrics:
            LOG.error("PF Metrics fetch succeeded for pod %s when RBAC is disabled - this should fail!", pod_name)
            metrics_succeeded_incorrectly.append((pod_name, f"node_ip={node_ip} port={port}"))
        else:
            LOG.info("PF Metrics fetch correctly failed for pod %s with RBAC disabled", pod_name)
            metrics_failed_correctly[pod_name] = {"node": node_name, "node_ip": node_ip, "port": port}

    # 5) Revert all modified NetworkConfig objects
    revert_errors = []
    for rn, orig in originals.items():
        try:
            nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            LOG.info("Restored original PF NetworkConfig %s", rn)
        except Exception as e:
            LOG.error("Failed to restore PF %s: %s", rn, e)
            revert_errors.append((rn, str(e)))

    if revert_errors:
        LOG.error("One or more PF NetworkConfig originals failed to revert: %s", revert_errors)
        pytest.fail(f"Failed to revert original PF NetworkConfig(s): {revert_errors}")

    if metrics_succeeded_incorrectly:
        LOG.error("PF Metrics fetch succeeded when RBAC disabled for: %s", metrics_succeeded_incorrectly)
        pytest.fail(f"PF Metrics fetch should fail when RBAC is disabled but succeeded for: {metrics_succeeded_incorrectly}")

    LOG.info("PF Successfully verified that metrics fetch fails with RBAC disabled for pods: %s", list(metrics_failed_correctly.keys()))


@pytest.mark.timeout(TEST_TIMEOUT)
def test_rbac_disabled_metrics_fail_vf():
    """
    Test that when RBAC is disabled for VF configs, metrics fetch with mTLS certificates should fail.
    
    Workflow:
    1) Patch VF NetworkConfig objects to set spec.metricsExporter.rbacConfig.enable = False.
    2) Trigger IB traffic on VF workload pods.
    3) Try to fetch metrics using mTLS (nic_util.curl_metrics_from_local).
    4) Verify that metrics fetch fails or returns non-Prometheus data (RBAC disabled means mTLS not enforced).
    5) Restore all modified NetworkConfig objects to their originals.
    """
    v1 = k8s_client.CoreV1Api()

    # 1) List NetworkConfig CRs and save originals (VF only)
    try:
        nc_items = nic_util.list_networkconfigs_custom(NC_NAMESPACE)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources in {NC_NAMESPACE}: {e}")

    if not nc_items:
        pytest.skip(f"No NetworkConfig resources in {NC_NAMESPACE}")

    originals = {}
    for it in nc_items:
        name = it.get("metadata", {}).get("name")
        if not name or not name.startswith("vf-"):
            continue
        originals[name] = yaml.safe_load(yaml.safe_dump(it))

    if not originals:
        pytest.skip("No VF NetworkConfig found")

    # Build patch to disable rbac
    patch_body = {
        "spec": {
            "metricsExporter": {
                "rbacConfig": {
                    "enable": False
                }
            }
        }
    }

    # Apply patches in parallel
    applied = []
    failed = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(originals)))) as ex:
        futures = {ex.submit(nic_util.patch_networkconfig_custom, NC_NAMESPACE, name, patch_body): name for name in originals.keys()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                fut.result()
                applied.append(name)
                LOG.info("VF Patched %s -> spec.metricsExporter.rbacConfig.enable=False", name)
            except Exception as e:
                LOG.error("Failed to patch VF %s: %s", name, e)
                failed.append(name)

    if failed:
        # rollback applied ones and fail early
        for rn in applied:
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, originals[rn])
            except Exception as re:
                LOG.error("Rollback failed for %s: %s", rn, re)
        pytest.fail(f"Failed to apply RBAC disable patch to some VF NetworkConfigs: {failed}")

    # Give operator time to reconcile
    time.sleep(2)

    # 2) Trigger IB traffic on VF workload pods
    all_pods = list_workloads(v1, namespace="default")
    wpods = [p for p in all_pods if p.metadata.name.startswith("vf-workload")]
    if not wpods:
        # revert before skipping
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("Failed revert for %s during skip", rn)
        pytest.skip("No running VF workload pods in default namespace")

    exec_outputs = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(wpods)))) as ex:
        futures = {ex.submit(nic_util.run_ib_traffic, p.metadata.name, p.metadata.namespace): p for p in wpods}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                out = fut.result()
                exec_outputs.update(out)
            except Exception as e:
                LOG.warning("nic_util.run_ib_traffic failed for %s: %s", p.metadata.name, e)
                exec_outputs[p.metadata.name] = f"ERROR: {e}"

    # 3) Build map of VF NetworkConfig -> nodePort
    nodeport_by_config = {}
    for it in nic_util.list_networkconfigs_custom(NC_NAMESPACE):
        name = it.get("metadata", {}).get("name")
        if not name or not name.startswith("vf-"):
            continue
        try:
            np = nic_util.get_metrics_nodeport_from_networkconfig(NC_NAMESPACE, name)
        except Exception:
            np = None
        nodeport_by_config[name] = np

    cfg = list(nodeport_by_config.keys())[0] if nodeport_by_config else None
    if not cfg:
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("Failed revert during no-config skip")
        pytest.skip("No VF NetworkConfig with nodePort found")

    # 4) Try to fetch metrics with mTLS - should fail when RBAC is disabled
    metrics_failed_correctly = {}
    metrics_succeeded_incorrectly = []

    port = nodeport_by_config.get(cfg)
    if not port:
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("Failed revert during no-port skip")
        pytest.skip(f"No numeric nodePort for VF config {cfg}")

    for p in wpods:
        pod_name = p.metadata.name
        node_name = getattr(p.spec, "node_name", None) or getattr(p.spec, "nodeName", None)
        if not node_name:
            LOG.warning("VF Pod %s has no nodeName; skipping", pod_name)
            continue

        node_ip = nic_util.get_node_ip(node_name)
        if not node_ip:
            LOG.warning("No InternalIP for node %s (VF pod %s)", node_name, pod_name)
            continue

        LOG.info("VF Attempting metrics fetch for pod %s -> node %s (%s) port %s (config=%s) with RBAC disabled", 
                 pod_name, node_name, node_ip, port, cfg)

        # Try to fetch metrics with mTLS - this should fail
        txt = nic_util.curl_metrics_from_local(node_ip, port, METRICS_SERVICE_NAME, timeout=8)
        
        # Check if we got valid Prometheus metrics (which would be incorrect when RBAC is disabled)
        has_valid_metrics = False
        if txt and txt.strip():
            for ln in txt.splitlines():
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                if PROM_LINE_RE.match(ln):
                    has_valid_metrics = True
                    break

        if has_valid_metrics:
            LOG.error("VF Metrics fetch succeeded for pod %s when RBAC is disabled - this should fail!", pod_name)
            metrics_succeeded_incorrectly.append((pod_name, f"node_ip={node_ip} port={port}"))
        else:
            LOG.info("VF Metrics fetch correctly failed for pod %s with RBAC disabled", pod_name)
            metrics_failed_correctly[pod_name] = {"node": node_name, "node_ip": node_ip, "port": port}

    # 5) Revert all modified NetworkConfig objects
    revert_errors = []
    for rn, orig in originals.items():
        try:
            nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            LOG.info("Restored original VF NetworkConfig %s", rn)
        except Exception as e:
            LOG.error("Failed to restore VF %s: %s", rn, e)
            revert_errors.append((rn, str(e)))

    if revert_errors:
        LOG.error("One or more VF NetworkConfig originals failed to revert: %s", revert_errors)
        pytest.fail(f"Failed to revert original VF NetworkConfig(s): {revert_errors}")

    if metrics_succeeded_incorrectly:
        LOG.error("VF Metrics fetch succeeded when RBAC disabled for: %s", metrics_succeeded_incorrectly)
        pytest.fail(f"VF Metrics fetch should fail when RBAC is disabled but succeeded for: {metrics_succeeded_incorrectly}")

    LOG.info("VF Successfully verified that metrics fetch fails with RBAC disabled for pods: %s", list(metrics_failed_correctly.keys()))


@pytest.mark.timeout(TEST_TIMEOUT)
def test_rbac_node_port_pf():
    """
    Test RBAC metrics exporter for PF configs with default nodePort.
    
    Workflow:
    1) Patch PF NetworkConfig objects to set
       spec.metricsExporter.rbacConfig.enable = True and ensure clientCAConfigMap.name = "client-ca".
    2) Trigger IB traffic on PF workload pods (this generates the traffic/metrics).
    3) After traffic, for each PF workload pod, read the NetworkConfig's spec.metricsExporter.nodePort,
       determine the pod's node InternalIP and pull metrics from https://<node_ip>:<nodePort>/metrics
       using local curl with mTLS (nic_util.curl_metrics_from_local which uses LOCAL_CERT_DIR/client.{crt,key} and ca.crt).
    4) Validate Prometheus numeric lines in responses.
    5) Restore all modified NetworkConfig objects to their originals.
    """
    v1 = k8s_client.CoreV1Api()

    # 1) List NetworkConfig CRs and save originals
    try:
        nc_items = nic_util.list_networkconfigs_custom(NC_NAMESPACE)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources in {NC_NAMESPACE}: {e}")

    if not nc_items:
        pytest.skip(f"No NetworkConfig resources in {NC_NAMESPACE}")

    originals = {}
    for it in nc_items:
        name = it.get("metadata", {}).get("name")
        if not name:
            continue
        originals[name] = yaml.safe_load(yaml.safe_dump(it))

    # Build patch to enable rbac under spec.metricsExporter for all NetworkConfigs
    patch_body = {
        "spec": {
            "metricsExporter": {
                "rbacConfig": {
                    "clientCAConfigMap": {"name": "client-ca"},
                    "enable": True
                }
            }
        }
    }

    # Apply patches in parallel
    applied = []
    failed = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(originals)))) as ex:
        futures = {ex.submit(nic_util.patch_networkconfig_custom, NC_NAMESPACE, name, patch_body): name for name in originals.keys()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                fut.result()
                applied.append(name)
                LOG.info("Patched %s -> spec.metricsExporter.rbacConfig.enable=True", name)
            except Exception as e:
                LOG.error("Failed to patch %s: %s", name, e)
                failed.append(name)

    if failed:
        # rollback applied ones and fail early
        for rn in applied:
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, originals[rn])
            except Exception as re:
                LOG.error("Rollback failed for %s: %s", rn, re)
        pytest.fail(f"Failed to apply RBAC enable patch to some NetworkConfigs: {failed}")

    # Optional: give operator a short moment to reconcile (increase if necessary)
    time.sleep(2)

    # 2) Trigger IB traffic on PF workload pods (best-effort)
    all_pods = list_workloads(v1, namespace="default")
    wpods = [p for p in all_pods if not p.metadata.name.startswith("vf-workload")]
    if not wpods:
        # revert before skipping
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("Failed revert for %s during skip", rn)
        pytest.skip("No running PF workload pods in default namespace")

    exec_outputs = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(wpods)))) as ex:
        futures = {ex.submit(nic_util.run_ib_traffic, p.metadata.name, p.metadata.namespace): p for p in wpods}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                out = fut.result()
                exec_outputs.update(out)
            except Exception as e:
                LOG.warning("nic_util.run_ib_traffic failed for %s: %s", p.metadata.name, e)
                exec_outputs[p.metadata.name] = f"ERROR: {e}"

    # 3) After traffic, build map of PF NetworkConfig -> nodePort
    nodeport_by_config = {}
    for it in nic_util.list_networkconfigs_custom(NC_NAMESPACE):
        name = it.get("metadata", {}).get("name")
        if not name or name.startswith("vf-"):
            continue
        try:
            np = nic_util.get_metrics_nodeport_from_networkconfig(NC_NAMESPACE, name)
        except Exception:
            np = None
        nodeport_by_config[name] = np

    cfg = list(nodeport_by_config.keys())[0] if nodeport_by_config else None
    if not cfg:
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("Failed revert during no-config skip")
        pytest.skip("No PF NetworkConfig with nodePort found")

    # 4) For each PF workload pod pull metrics using the nodePort
    metrics_found = {}
    metrics_missing = []

    port = nodeport_by_config.get(cfg)
    if not port:
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("Failed revert during no-port skip")
        pytest.skip(f"No numeric nodePort for PF config {cfg}")

    for p in wpods:
        pod_name = p.metadata.name
        node_name = getattr(p.spec, "node_name", None) or getattr(p.spec, "nodeName", None)
        if not node_name:
            LOG.warning("PF Pod %s has no nodeName; skipping", pod_name)
            metrics_missing.append((pod_name, "no-nodeName"))
            continue

        node_ip = nic_util.get_node_ip(node_name)
        if not node_ip:
            LOG.warning("No InternalIP for node %s (PF pod %s)", node_name, pod_name)
            metrics_missing.append((pod_name, f"no-node-ip node={node_name}"))
            continue

        LOG.info("PF Pulling metrics for pod %s -> node %s (%s) port %s (config=%s)", pod_name, node_name, node_ip, port, cfg)
        # try a few times (small retries) to account for timing
        txt = None
        ok = False
        for attempt in range(3):
            txt = nic_util.curl_metrics_from_local(node_ip, port, METRICS_SERVICE_NAME, timeout=8)
            if txt and txt.strip():
                # check for prometheus numeric line
                for ln in txt.splitlines():
                    ln = ln.strip()
                    if not ln or ln.startswith("#"):
                        continue
                    if PROM_LINE_RE.match(ln):
                        ok = True
                        break
            if ok:
                break
            time.sleep(1)

        if ok:
            metrics_found[pod_name] = {"node": node_name, "node_ip": node_ip, "port": port}
            LOG.info("PF Metrics OK for pod %s on %s:%s", pod_name, node_ip, port)
        else:
            sample = (txt or "")[:1000]
            LOG.warning("PF Failed to fetch valid metrics for pod %s from %s:%s sample=%s", pod_name, node_ip, port, sample)
            metrics_missing.append((pod_name, f"no-metrics node_ip={node_ip} port={port} sample={sample}"))

    # 5) Revert all modified PF NetworkConfig objects
    revert_errors = []
    for rn, orig in originals.items():
        try:
            nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            LOG.info("Restored original PF NetworkConfig %s", rn)
        except Exception as e:
            LOG.error("Failed to restore PF %s: %s", rn, e)
            revert_errors.append((rn, str(e)))

    if revert_errors:
        LOG.error("One or more PF NetworkConfig originals failed to revert: %s", revert_errors)
        pytest.fail(f"Failed to revert original PF NetworkConfig(s): {revert_errors}")

    if metrics_missing:
        LOG.error("PF Metrics missing/invalid after traffic: %s", metrics_missing)
        pytest.fail(f"PF Metrics missing/invalid after traffic: {metrics_missing}")

    LOG.info("PF Successfully validated metrics after enabling RBAC and running IB traffic for pods: %s", list(metrics_found.keys()))


@pytest.mark.timeout(TEST_TIMEOUT)
def test_rbac_node_port_vf():
    """
    Test RBAC metrics exporter for VF configs with default nodePort.
    
    Workflow:
    1) Patch VF NetworkConfig objects to set
       spec.metricsExporter.rbacConfig.enable = True and ensure clientCAConfigMap.name = "client-ca".
    2) Trigger IB traffic on VF workload pods (this generates the traffic/metrics).
    3) After traffic, for each VF workload pod, read the NetworkConfig's spec.metricsExporter.nodePort,
       determine the pod's node InternalIP and pull metrics from https://<node_ip>:<nodePort>/metrics
       using local curl with mTLS (nic_util.curl_metrics_from_local which uses LOCAL_CERT_DIR/client.{crt,key} and ca.crt).
    4) Validate Prometheus numeric lines in responses.
    5) Restore all modified NetworkConfig objects to their originals.
    """
    v1 = k8s_client.CoreV1Api()

    # 1) List NetworkConfig CRs and save originals (VF only)
    try:
        nc_items = nic_util.list_networkconfigs_custom(NC_NAMESPACE)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources in {NC_NAMESPACE}: {e}")

    if not nc_items:
        pytest.skip(f"No NetworkConfig resources in {NC_NAMESPACE}")

    originals = {}
    for it in nc_items:
        name = it.get("metadata", {}).get("name")
        if not name or not name.startswith("vf-"):
            continue
        originals[name] = yaml.safe_load(yaml.safe_dump(it))

    if not originals:
        pytest.skip("No VF NetworkConfig found")

    # Build patch to enable rbac under spec.metricsExporter for all VF NetworkConfigs
    patch_body = {
        "spec": {
            "metricsExporter": {
                "rbacConfig": {
                    "clientCAConfigMap": {"name": "client-ca"},
                    "enable": True
                }
            }
        }
    }

    # Apply patches in parallel
    applied = []
    failed = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(originals)))) as ex:
        futures = {ex.submit(nic_util.patch_networkconfig_custom, NC_NAMESPACE, name, patch_body): name for name in originals.keys()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                fut.result()
                applied.append(name)
                LOG.info("VF Patched %s -> spec.metricsExporter.rbacConfig.enable=True", name)
            except Exception as e:
                LOG.error("Failed to patch VF %s: %s", name, e)
                failed.append(name)

    if failed:
        # rollback applied ones and fail early
        for rn in applied:
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, originals[rn])
            except Exception as re:
                LOG.error("Rollback failed for %s: %s", rn, re)
        pytest.fail(f"Failed to apply RBAC enable patch to some VF NetworkConfigs: {failed}")

    # Optional: give operator a short moment to reconcile (increase if necessary)
    time.sleep(2)

    # 2) Trigger IB traffic on VF workload pods (best-effort)
    all_pods = list_workloads(v1, namespace="default")
    wpods = [p for p in all_pods if p.metadata.name.startswith("vf-workload")]
    if not wpods:
        # revert before skipping
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("Failed revert for %s during skip", rn)
        pytest.skip("No running VF workload pods in default namespace")

    exec_outputs = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(wpods)))) as ex:
        futures = {ex.submit(nic_util.run_ib_traffic, p.metadata.name, p.metadata.namespace): p for p in wpods}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                out = fut.result()
                exec_outputs.update(out)
            except Exception as e:
                LOG.warning("nic_util.run_ib_traffic failed for %s: %s", p.metadata.name, e)
                exec_outputs[p.metadata.name] = f"ERROR: {e}"

    # 3) After traffic, build map of VF NetworkConfig -> nodePort
    nodeport_by_config = {}
    for it in nic_util.list_networkconfigs_custom(NC_NAMESPACE):
        name = it.get("metadata", {}).get("name")
        if not name or not name.startswith("vf-"):
            continue
        try:
            np = nic_util.get_metrics_nodeport_from_networkconfig(NC_NAMESPACE, name)
        except Exception:
            np = None
        nodeport_by_config[name] = np

    cfg = list(nodeport_by_config.keys())[0] if nodeport_by_config else None
    if not cfg:
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("Failed revert during no-config skip")
        pytest.skip("No VF NetworkConfig with nodePort found")

    # 4) For each VF workload pod pull metrics using the nodePort
    metrics_found = {}
    metrics_missing = []

    port = nodeport_by_config.get(cfg)
    if not port:
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("Failed revert during no-port skip")
        pytest.skip(f"No numeric nodePort for VF config {cfg}")

    for p in wpods:
        pod_name = p.metadata.name
        node_name = getattr(p.spec, "node_name", None) or getattr(p.spec, "nodeName", None)
        if not node_name:
            LOG.warning("VF Pod %s has no nodeName; skipping", pod_name)
            metrics_missing.append((pod_name, "no-nodeName"))
            continue

        node_ip = nic_util.get_node_ip(node_name)
        if not node_ip:
            LOG.warning("No InternalIP for node %s (VF pod %s)", node_name, pod_name)
            metrics_missing.append((pod_name, f"no-node-ip node={node_name}"))
            continue

        LOG.info("VF Pulling metrics for pod %s -> node %s (%s) port %s (config=%s)", pod_name, node_name, node_ip, port, cfg)
        # try a few times (small retries) to account for timing
        txt = None
        ok = False
        for attempt in range(3):
            txt = nic_util.curl_metrics_from_local(node_ip, port, METRICS_SERVICE_NAME, timeout=8)
            if txt and txt.strip():
                # check for prometheus numeric line
                for ln in txt.splitlines():
                    ln = ln.strip()
                    if not ln or ln.startswith("#"):
                        continue
                    if PROM_LINE_RE.match(ln):
                        ok = True
                        break
            if ok:
                break
            time.sleep(1)

        if ok:
            metrics_found[pod_name] = {"node": node_name, "node_ip": node_ip, "port": port}
            LOG.info("VF Metrics OK for pod %s on %s:%s", pod_name, node_ip, port)
        else:
            sample = (txt or "")[:1000]
            LOG.warning("VF Failed to fetch valid metrics for pod %s from %s:%s sample=%s", pod_name, node_ip, port, sample)
            metrics_missing.append((pod_name, f"no-metrics node_ip={node_ip} port={port} sample={sample}"))

    # 5) Revert all modified VF NetworkConfig objects
    revert_errors = []
    for rn, orig in originals.items():
        try:
            nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            LOG.info("Restored original VF NetworkConfig %s", rn)
        except Exception as e:
            LOG.error("Failed to restore VF %s: %s", rn, e)
            revert_errors.append((rn, str(e)))

    if revert_errors:
        LOG.error("One or more VF NetworkConfig originals failed to revert: %s", revert_errors)
        pytest.fail(f"Failed to revert original VF NetworkConfig(s): {revert_errors}")

    if metrics_missing:
        LOG.error("VF Metrics missing/invalid after traffic: %s", metrics_missing)
        pytest.fail(f"VF Metrics missing/invalid after traffic: {metrics_missing}")

    LOG.info("VF Successfully validated metrics after enabling RBAC and running IB traffic for pods: %s", list(metrics_found.keys()))


@pytest.mark.timeout(TEST_TIMEOUT)
@pytest.mark.timeout(TEST_TIMEOUT)
def test_rbac_node_port_custom_port_pf():
    """
    Test RBAC metrics exporter with custom nodePort 32521 for PF configs.
    
    Workflow:
    1) Patch PF NetworkConfig objects to set spec.metricsExporter.nodePort = 32521,
       spec.metricsExporter.rbacConfig.enable = True and clientCAConfigMap.name = "client-ca".
    2) Trigger IB traffic on PF workload pods.
    3) Pull metrics from https://<node_ip>:32521/metrics using mTLS.
    4) Validate Prometheus numeric lines in responses.
    5) Restore all modified NetworkConfig objects to their originals.
    """
    v1 = k8s_client.CoreV1Api()

    # 1) List NetworkConfig CRs and save originals (PF only)
    try:
        nc_items = nic_util.list_networkconfigs_custom(NC_NAMESPACE)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources in {NC_NAMESPACE}: {e}")

    if not nc_items:
        pytest.skip(f"No NetworkConfig resources in {NC_NAMESPACE}")

    originals = {}
    for it in nc_items:
        name = it.get("metadata", {}).get("name")
        if not name or name.startswith("vf-"):
            continue
        originals[name] = yaml.safe_load(yaml.safe_dump(it))

    if not originals:
        pytest.skip("No PF NetworkConfig found")

    # Apply patches with port 32521 for PF
    applied = []
    failed = []
    port = 32521
    
    patch_body = {
        "spec": {
            "metricsExporter": {
                "nodePort": port,
                "rbacConfig": {
                    "clientCAConfigMap": {"name": "client-ca"},
                    "enable": True
                }
            }
        }
    }
    
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(originals)))) as ex:
        futures = {ex.submit(nic_util.patch_networkconfig_custom, NC_NAMESPACE, name, patch_body): name for name in originals.keys()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                fut.result()
                applied.append(name)
                LOG.info("PF Patched %s -> nodePort=%s, rbacConfig.enable=True", name, port)
            except Exception as e:
                LOG.error("PF Failed to patch %s: %s", name, e)
                failed.append(name)

    if failed:
        # rollback applied ones and fail early
        for rn in applied:
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, originals[rn])
            except Exception as re:
                LOG.error("PF Rollback failed for %s: %s", rn, re)
        pytest.fail(f"PF Failed to apply nodePort + RBAC patch to some NetworkConfigs: {failed}")

    # Give operator time to reconcile
    time.sleep(2)

    # 2) Trigger IB traffic on PF workload pods
    all_pods = list_workloads(v1, namespace="default")
    wpods = [p for p in all_pods if not p.metadata.name.startswith("vf-workload")]
    if not wpods:
        # revert before skipping
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("PF Failed revert for %s during skip", rn)
        pytest.skip("No running PF workload pods in default namespace")

    exec_outputs = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(wpods)))) as ex:
        futures = {ex.submit(nic_util.run_ib_traffic, p.metadata.name, p.metadata.namespace): p for p in wpods}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                out = fut.result()
                exec_outputs.update(out)
            except Exception as e:
                LOG.warning("PF nic_util.run_ib_traffic failed for %s: %s", p.metadata.name, e)
                exec_outputs[p.metadata.name] = f"ERROR: {e}"

    # 3) Build map of NetworkConfig -> nodePort after patching
    nodeport_by_config = {}
    for it in nic_util.list_networkconfigs_custom(NC_NAMESPACE):
        name = it.get("metadata", {}).get("name")
        if not name or name.startswith("vf-"):
            continue
        try:
            np = nic_util.get_metrics_nodeport_from_networkconfig(NC_NAMESPACE, name)
        except Exception:
            np = None
        nodeport_by_config[name] = np

    cfg = list(nodeport_by_config.keys())[0]

    # 4) Pull metrics using port 32521 for each PF workload pod
    metrics_found = {}
    metrics_missing = []

    for p in wpods:
        pod_name = p.metadata.name
        node_name = getattr(p.spec, "node_name", None) or getattr(p.spec, "nodeName", None)
        if not node_name:
            LOG.warning("PF Pod %s has no nodeName; skipping", pod_name)
            metrics_missing.append((pod_name, "no-nodeName"))
            continue

        node_ip = nic_util.get_node_ip(node_name)
        if not node_ip:
            LOG.warning("PF No InternalIP for node %s (pod %s)", node_name, pod_name)
            metrics_missing.append((pod_name, f"no-node-ip node={node_name}"))
            continue

        LOG.info("PF Pulling metrics for pod %s -> node %s (%s) port %s (config=%s)", pod_name, node_name, node_ip, port, cfg)

        # Try a few times to account for timing
        txt = None
        ok = False
        for attempt in range(3):
            txt = nic_util.curl_metrics_from_local(node_ip, port, METRICS_SERVICE_NAME, timeout=8)
            if txt and txt.strip():
                # check for prometheus numeric line
                for ln in txt.splitlines():
                    ln = ln.strip()
                    if not ln or ln.startswith("#"):
                        continue
                    if PROM_LINE_RE.match(ln):
                        ok = True
                        break
            if ok:
                break
            time.sleep(1)

        if ok:
            metrics_found[pod_name] = {"node": node_name, "node_ip": node_ip, "port": port}
            LOG.info("PF Metrics OK for pod %s on %s:%s", pod_name, node_ip, port)
        else:
            sample = (txt or "")[:1000]
            LOG.warning("PF Failed to fetch valid metrics for pod %s from %s:%s sample=%s", pod_name, node_ip, port, sample)
            metrics_missing.append((pod_name, f"no-metrics node_ip={node_ip} port={port} sample={sample}"))

    # 5) Revert all modified NetworkConfig objects
    revert_errors = []
    for rn, orig in originals.items():
        try:
            nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            LOG.info("PF Restored original NetworkConfig %s", rn)
        except Exception as e:
            LOG.error("PF Failed to restore %s: %s", rn, e)
            revert_errors.append((rn, str(e)))

    if revert_errors:
        LOG.error("PF One or more NetworkConfig originals failed to revert: %s", revert_errors)
        pytest.fail(f"PF Failed to revert original NetworkConfig(s): {revert_errors}")

    if metrics_missing:
        LOG.error("PF Metrics missing/invalid after traffic: %s", metrics_missing)
        pytest.fail(f"PF Metrics missing/invalid after traffic: {metrics_missing}")

    LOG.info("PF Successfully validated metrics on custom port 32521 for pods: %s", list(metrics_found.keys()))


@pytest.mark.timeout(TEST_TIMEOUT)
def test_rbac_node_port_custom_port_vf():
    """
    Test RBAC metrics exporter with custom nodePort 32520 for VF configs.
    
    Workflow:
    1) Patch VF NetworkConfig objects to set spec.metricsExporter.nodePort = 32520,
       spec.metricsExporter.rbacConfig.enable = True and clientCAConfigMap.name = "client-ca".
    2) Trigger IB traffic on VF workload pods.
    3) Pull metrics from https://<node_ip>:32520/metrics using mTLS.
    4) Validate Prometheus numeric lines in responses.
    5) Restore all modified NetworkConfig objects to their originals.
    """
    v1 = k8s_client.CoreV1Api()

    # 1) List NetworkConfig CRs and save originals (VF only)
    try:
        nc_items = nic_util.list_networkconfigs_custom(NC_NAMESPACE)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources in {NC_NAMESPACE}: {e}")

    if not nc_items:
        pytest.skip(f"No NetworkConfig resources in {NC_NAMESPACE}")

    originals = {}
    for it in nc_items:
        name = it.get("metadata", {}).get("name")
        if not name or not name.startswith("vf-"):
            continue
        originals[name] = yaml.safe_load(yaml.safe_dump(it))

    if not originals:
        pytest.skip("No VF NetworkConfig found")

    # Apply patches with port 32520 for VF
    applied = []
    failed = []
    port = 32520
    
    patch_body = {
        "spec": {
            "metricsExporter": {
                "nodePort": port,
                "rbacConfig": {
                    "clientCAConfigMap": {"name": "client-ca"},
                    "enable": True
                }
            }
        }
    }
    
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(originals)))) as ex:
        futures = {ex.submit(nic_util.patch_networkconfig_custom, NC_NAMESPACE, name, patch_body): name for name in originals.keys()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                fut.result()
                applied.append(name)
                LOG.info("VF Patched %s -> nodePort=%s, rbacConfig.enable=True", name, port)
            except Exception as e:
                LOG.error("VF Failed to patch %s: %s", name, e)
                failed.append(name)

    if failed:
        # rollback applied ones and fail early
        for rn in applied:
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, originals[rn])
            except Exception as re:
                LOG.error("VF Rollback failed for %s: %s", rn, re)
        pytest.fail(f"VF Failed to apply nodePort + RBAC patch to some NetworkConfigs: {failed}")

    # Give operator time to reconcile
    time.sleep(2)

    # 2) Trigger IB traffic on VF workload pods
    all_pods = list_workloads(v1, namespace="default")
    wpods = [p for p in all_pods if p.metadata.name.startswith("vf-workload")]
    if not wpods:
        # revert before skipping
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("VF Failed revert for %s during skip", rn)
        pytest.skip("No running VF workload pods in default namespace")

    exec_outputs = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(wpods)))) as ex:
        futures = {ex.submit(nic_util.run_ib_traffic, p.metadata.name, p.metadata.namespace): p for p in wpods}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                out = fut.result()
                exec_outputs.update(out)
            except Exception as e:
                LOG.warning("VF nic_util.run_ib_traffic failed for %s: %s", p.metadata.name, e)
                exec_outputs[p.metadata.name] = f"ERROR: {e}"

    # 3) Build map of NetworkConfig -> nodePort after patching
    nodeport_by_config = {}
    for it in nic_util.list_networkconfigs_custom(NC_NAMESPACE):
        name = it.get("metadata", {}).get("name")
        if not name or not name.startswith("vf-"):
            continue
        try:
            np = nic_util.get_metrics_nodeport_from_networkconfig(NC_NAMESPACE, name)
        except Exception:
            np = None
        nodeport_by_config[name] = np

    cfg = list(nodeport_by_config.keys())[0]

    # 4) Pull metrics using port 32520 for each VF workload pod
    metrics_found = {}
    metrics_missing = []

    for p in wpods:
        pod_name = p.metadata.name
        node_name = getattr(p.spec, "node_name", None) or getattr(p.spec, "nodeName", None)
        if not node_name:
            LOG.warning("VF Pod %s has no nodeName; skipping", pod_name)
            metrics_missing.append((pod_name, "no-nodeName"))
            continue

        node_ip = nic_util.get_node_ip(node_name)
        if not node_ip:
            LOG.warning("VF No InternalIP for node %s (pod %s)", node_name, pod_name)
            metrics_missing.append((pod_name, f"no-node-ip node={node_name}"))
            continue

        LOG.info("VF Pulling metrics for pod %s -> node %s (%s) port %s (config=%s)", pod_name, node_name, node_ip, port, cfg)

        # Try a few times to account for timing
        txt = None
        ok = False
        for attempt in range(3):
            txt = nic_util.curl_metrics_from_local(node_ip, port, METRICS_SERVICE_NAME, timeout=8)
            if txt and txt.strip():
                # check for prometheus numeric line
                for ln in txt.splitlines():
                    ln = ln.strip()
                    if not ln or ln.startswith("#"):
                        continue
                    if PROM_LINE_RE.match(ln):
                        ok = True
                        break
            if ok:
                break
            time.sleep(1)

        if ok:
            metrics_found[pod_name] = {"node": node_name, "node_ip": node_ip, "port": port}
            LOG.info("VF Metrics OK for pod %s on %s:%s", pod_name, node_ip, port)
        else:
            sample = (txt or "")[:1000]
            LOG.warning("VF Failed to fetch valid metrics for pod %s from %s:%s sample=%s", pod_name, node_ip, port, sample)
            metrics_missing.append((pod_name, f"no-metrics node_ip={node_ip} port={port} sample={sample}"))

    # 5) Revert all modified NetworkConfig objects
    revert_errors = []
    for rn, orig in originals.items():
        try:
            nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            LOG.info("VF Restored original NetworkConfig %s", rn)
        except Exception as e:
            LOG.error("VF Failed to restore %s: %s", rn, e)
            revert_errors.append((rn, str(e)))

    if revert_errors:
        LOG.error("VF One or more NetworkConfig originals failed to revert: %s", revert_errors)
        pytest.fail(f"VF Failed to revert original NetworkConfig(s): {revert_errors}")

    if metrics_missing:
        LOG.error("VF Metrics missing/invalid after traffic: %s", metrics_missing)
        pytest.fail(f"VF Metrics missing/invalid after traffic: {metrics_missing}")

    LOG.info("VF Successfully validated metrics on custom port 32520 for pods: %s", list(metrics_found.keys()))


@pytest.mark.timeout(TEST_TIMEOUT)
def test_rbac_source_port_pf():
    """
    Test RBAC metrics exporter using source port configuration for PF configs.
    
    Workflow:
    1) Patch PF NetworkConfig objects to set spec.metricsExporter.rbacConfig.enable = True,
       and ensure spec.metricsExporter.port (source port) is configured.
    2) Trigger IB traffic on PF workload pods.
    3) For each PF workload pod, read the source port from the corresponding NetworkConfig,
       and pull metrics using that source port with curl --local-port.
    4) Validate Prometheus numeric lines in responses.
    5) Restore all modified NetworkConfig objects to their originals.
    """
    v1 = k8s_client.CoreV1Api()

    # 1) List NetworkConfig CRs and save originals (PF only)
    try:
        nc_items = nic_util.list_networkconfigs_custom(NC_NAMESPACE)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources in {NC_NAMESPACE}: {e}")

    if not nc_items:
        pytest.skip(f"No NetworkConfig resources in {NC_NAMESPACE}")

    originals = {}
    for it in nc_items:
        name = it.get("metadata", {}).get("name")
        if not name or name.startswith("vf-"):
            continue
        originals[name] = yaml.safe_load(yaml.safe_dump(it))

    if not originals:
        pytest.skip("No PF NetworkConfig found")

    # Build patch to enable rbac - source port should already be configured in NetworkConfig
    patch_body = {
        "spec": {
            "metricsExporter": {
                "rbacConfig": {
                    "clientCAConfigMap": {"name": "client-ca"},
                    "enable": True
                }
            }
        }
    }

    # Apply patches in parallel
    applied = []
    failed = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(originals)))) as ex:
        futures = {ex.submit(nic_util.patch_networkconfig_custom, NC_NAMESPACE, name, patch_body): name for name in originals.keys()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                fut.result()
                applied.append(name)
                LOG.info("PF Patched %s -> spec.metricsExporter.rbacConfig.enable=True", name)
            except Exception as e:
                LOG.error("PF Failed to patch %s: %s", name, e)
                failed.append(name)

    if failed:
        # rollback applied ones and fail early
        for rn in applied:
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, originals[rn])
            except Exception as re:
                LOG.error("PF Rollback failed for %s: %s", rn, re)
        pytest.fail(f"PF Failed to apply RBAC enable patch to some NetworkConfigs: {failed}")

    # Give operator time to reconcile
    time.sleep(2)

    # 2) Trigger IB traffic on PF workload pods
    all_pods = list_workloads(v1, namespace="default")
    wpods = [p for p in all_pods if not p.metadata.name.startswith("vf-workload")]
    if not wpods:
        # revert before skipping
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("PF Failed revert for %s during skip", rn)
        pytest.skip("No running PF workload pods in default namespace")

    exec_outputs = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(wpods)))) as ex:
        futures = {ex.submit(nic_util.run_ib_traffic, p.metadata.name, p.metadata.namespace): p for p in wpods}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                out = fut.result()
                exec_outputs.update(out)
            except Exception as e:
                LOG.warning("nic_util.run_ib_traffic failed for %s: %s", p.metadata.name, e)
                exec_outputs[p.metadata.name] = f"ERROR: {e}"

    # 3) Build map of NetworkConfig -> source port and nodePort
    config_ports = {}
    for it in nic_util.list_networkconfigs_custom(NC_NAMESPACE):
        name = it.get("metadata", {}).get("name")
        if not name:
            continue
        spec = it.get("spec", {})
        metrics_exp = spec.get("metricsExporter", {})
        source_port = metrics_exp.get("port")  # source port
        node_port = metrics_exp.get("nodePort")  # destination port
        config_ports[name] = {"source_port": source_port, "node_port": node_port}

    cfg = list(config_ports.keys())[0]

    # 4) Pull metrics using source port for each PF workload pod
    metrics_found = {}
    metrics_missing = []

    for p in wpods:
        pod_name = p.metadata.name
        node_name = getattr(p.spec, "node_name", None) or getattr(p.spec, "nodeName", None)
        if not node_name:
            LOG.warning("PF Pod %s has no nodeName; skipping", pod_name)
            metrics_missing.append((pod_name, "no-nodeName"))
            continue

        node_ip = nic_util.get_node_ip(node_name)
        if not node_ip:
            LOG.warning("PF No InternalIP for node %s (pod %s)", node_name, pod_name)
            metrics_missing.append((pod_name, f"no-node-ip node={node_name}"))
            continue

        ports = config_ports.get(cfg, {})
        source_port = ports.get("source_port")
        node_port = ports.get("node_port")

        if not source_port:
            LOG.warning("PF No source port configured for config %s; skipping pod %s", cfg, pod_name)
            metrics_missing.append((pod_name, f"no-source-port-for-config {cfg}"))
            continue

        if not node_port:
            LOG.warning("PF No nodePort configured for config %s; skipping pod %s", cfg, pod_name)
            metrics_missing.append((pod_name, f"no-nodePort-for-config {cfg}"))
            continue

        LOG.info("PF Pulling metrics for pod %s -> node %s (%s) source_port=%s node_port=%s (config=%s)", 
                 pod_name, node_name, node_ip, source_port, node_port, cfg)

        # Try to pull metrics using source port with mTLS
        txt = None
        ok = False
        
        for attempt in range(3):
            txt = nic_util.curl_metrics_from_local(node_ip, node_port, METRICS_SERVICE_NAME, source_port=source_port, timeout=8)
            if txt and txt.strip():
                # check for prometheus numeric line
                for ln in txt.splitlines():
                    ln = ln.strip()
                    if not ln or ln.startswith("#"):
                        continue
                    if PROM_LINE_RE.match(ln):
                        ok = True
                        break
            if ok:
                break
            time.sleep(1)

        if ok:
            metrics_found[pod_name] = {"node": node_name, "node_ip": node_ip, "source_port": source_port, "node_port": node_port}
            LOG.info("PF Metrics OK for pod %s using source port %s", pod_name, source_port)
        else:
            sample = (txt or "")[:1000]
            LOG.warning("PF Failed to fetch valid metrics for pod %s using source port %s, sample=%s", pod_name, source_port, sample)
            metrics_missing.append((pod_name, f"no-metrics source_port={source_port} node_port={node_port} sample={sample}"))

    # 5) Revert all modified NetworkConfig objects
    revert_errors = []
    for rn, orig in originals.items():
        try:
            nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            LOG.info("PF Restored original NetworkConfig %s", rn)
        except Exception as e:
            LOG.error("PF Failed to restore %s: %s", rn, e)
            revert_errors.append((rn, str(e)))

    if revert_errors:
        LOG.error("PF One or more NetworkConfig originals failed to revert: %s", revert_errors)
        pytest.fail(f"PF Failed to revert original NetworkConfig(s): {revert_errors}")

    if metrics_missing:
        LOG.error("PF Metrics missing/invalid after traffic: %s", metrics_missing)
        pytest.fail(f"PF Metrics missing/invalid after traffic: {metrics_missing}")

    LOG.info("PF Successfully validated metrics using source port for pods: %s", list(metrics_found.keys()))


@pytest.mark.timeout(TEST_TIMEOUT)
def test_rbac_source_port_vf():
    """
    Test RBAC metrics exporter using source port configuration for VF configs.
    
    Workflow:
    1) Patch VF NetworkConfig objects to set spec.metricsExporter.rbacConfig.enable = True,
       and ensure spec.metricsExporter.port (source port) is configured.
    2) Trigger IB traffic on VF workload pods.
    3) For each VF workload pod, read the source port from the corresponding NetworkConfig,
       and pull metrics using that source port with curl --local-port.
    4) Validate Prometheus numeric lines in responses.
    5) Restore all modified NetworkConfig objects to their originals.
    """
    v1 = k8s_client.CoreV1Api()

    # 1) List NetworkConfig CRs and save originals (VF only)
    try:
        nc_items = nic_util.list_networkconfigs_custom(NC_NAMESPACE)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources in {NC_NAMESPACE}: {e}")

    if not nc_items:
        pytest.skip(f"No NetworkConfig resources in {NC_NAMESPACE}")

    originals = {}
    for it in nc_items:
        name = it.get("metadata", {}).get("name")
        if not name or not name.startswith("vf-"):
            continue
        originals[name] = yaml.safe_load(yaml.safe_dump(it))

    if not originals:
        pytest.skip("No VF NetworkConfig found")

    # Build patch to enable rbac - source port should already be configured in NetworkConfig
    patch_body = {
        "spec": {
            "metricsExporter": {
                "rbacConfig": {
                    "clientCAConfigMap": {"name": "client-ca"},
                    "enable": True
                }
            }
        }
    }

    # Apply patches in parallel
    applied = []
    failed = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(originals)))) as ex:
        futures = {ex.submit(nic_util.patch_networkconfig_custom, NC_NAMESPACE, name, patch_body): name for name in originals.keys()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                fut.result()
                applied.append(name)
                LOG.info("VF Patched %s -> spec.metricsExporter.rbacConfig.enable=True", name)
            except Exception as e:
                LOG.error("VF Failed to patch %s: %s", name, e)
                failed.append(name)

    if failed:
        # rollback applied ones and fail early
        for rn in applied:
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, originals[rn])
            except Exception as re:
                LOG.error("VF Rollback failed for %s: %s", rn, re)
        pytest.fail(f"VF Failed to apply RBAC enable patch to some NetworkConfigs: {failed}")

    # Give operator time to reconcile
    time.sleep(2)

    # 2) Trigger IB traffic on VF workload pods
    all_pods = list_workloads(v1, namespace="default")
    wpods = [p for p in all_pods if p.metadata.name.startswith("vf-workload")]
    if not wpods:
        # revert before skipping
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("VF Failed revert for %s during skip", rn)
        pytest.skip("No running VF workload pods in default namespace")

    exec_outputs = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(wpods)))) as ex:
        futures = {ex.submit(nic_util.run_ib_traffic, p.metadata.name, p.metadata.namespace): p for p in wpods}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                out = fut.result()
                exec_outputs.update(out)
            except Exception as e:
                LOG.warning("VF nic_util.run_ib_traffic failed for %s: %s", p.metadata.name, e)
                exec_outputs[p.metadata.name] = f"ERROR: {e}"

    # 3) Build map of NetworkConfig -> source port and nodePort (VF only)
    config_ports = {}
    for it in nic_util.list_networkconfigs_custom(NC_NAMESPACE):
        name = it.get("metadata", {}).get("name")
        if not name or not name.startswith("vf-"):
            continue
        spec = it.get("spec", {})
        metrics_exp = spec.get("metricsExporter", {})
        source_port = metrics_exp.get("port")  # source port
        node_port = metrics_exp.get("nodePort")  # destination port
        config_ports[name] = {"source_port": source_port, "node_port": node_port}

    cfg = list(config_ports.keys())[0]

    # 4) Pull metrics using source port for each VF workload pod
    metrics_found = {}
    metrics_missing = []

    for p in wpods:
        pod_name = p.metadata.name
        node_name = getattr(p.spec, "node_name", None) or getattr(p.spec, "nodeName", None)
        if not node_name:
            LOG.warning("VF Pod %s has no nodeName; skipping", pod_name)
            metrics_missing.append((pod_name, "no-nodeName"))
            continue

        node_ip = nic_util.get_node_ip(node_name)
        if not node_ip:
            LOG.warning("VF No InternalIP for node %s (pod %s)", node_name, pod_name)
            metrics_missing.append((pod_name, f"no-node-ip node={node_name}"))
            continue

        ports = config_ports.get(cfg, {})
        source_port = ports.get("source_port")
        node_port = ports.get("node_port")

        if not source_port:
            LOG.warning("VF No source port configured for config %s; skipping pod %s", cfg, pod_name)
            metrics_missing.append((pod_name, f"no-source-port-for-config {cfg}"))
            continue

        if not node_port:
            LOG.warning("VF No nodePort configured for config %s; skipping pod %s", cfg, pod_name)
            metrics_missing.append((pod_name, f"no-nodePort-for-config {cfg}"))
            continue

        LOG.info("VF Pulling metrics for pod %s -> node %s (%s) source_port=%s node_port=%s (config=%s)", 
                 pod_name, node_name, node_ip, source_port, node_port, cfg)

        # Try to pull metrics using source port with mTLS
        txt = None
        ok = False
        
        for attempt in range(3):
            txt = nic_util.curl_metrics_from_local(node_ip, node_port, METRICS_SERVICE_NAME, source_port=source_port, timeout=8)
            if txt and txt.strip():
                # check for prometheus numeric line
                for ln in txt.splitlines():
                    ln = ln.strip()
                    if not ln or ln.startswith("#"):
                        continue
                    if PROM_LINE_RE.match(ln):
                        ok = True
                        break
            if ok:
                break
            time.sleep(1)

        if ok:
            metrics_found[pod_name] = {"node": node_name, "node_ip": node_ip, "source_port": source_port, "node_port": node_port}
            LOG.info("VF Metrics OK for pod %s using source port %s", pod_name, source_port)
        else:
            sample = (txt or "")[:1000]
            LOG.warning("VF Failed to fetch valid metrics for pod %s using source port %s, sample=%s", pod_name, source_port, sample)
            metrics_missing.append((pod_name, f"no-metrics source_port={source_port} node_port={node_port} sample={sample}"))

    # 5) Revert all modified NetworkConfig objects
    revert_errors = []
    for rn, orig in originals.items():
        try:
            nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            LOG.info("VF Restored original NetworkConfig %s", rn)
        except Exception as e:
            LOG.error("VF Failed to restore %s: %s", rn, e)
            revert_errors.append((rn, str(e)))

    if revert_errors:
        LOG.error("VF One or more NetworkConfig originals failed to revert: %s", revert_errors)
        pytest.fail(f"VF Failed to revert original NetworkConfig(s): {revert_errors}")

    if metrics_missing:
        LOG.error("VF Metrics missing/invalid after traffic: %s", metrics_missing)
        pytest.fail(f"VF Metrics missing/invalid after traffic: {metrics_missing}")

    LOG.info("VF Successfully validated metrics using source port for pods: %s", list(metrics_found.keys()))


@pytest.mark.timeout(TEST_TIMEOUT)
def test_rbac_source_port_custom_pf():
    """
    Test RBAC metrics exporter using custom source port 2001 for PF configs.
    
    Workflow:
    1) Patch PF NetworkConfig objects to set spec.metricsExporter.port = 2001,
       spec.metricsExporter.rbacConfig.enable = True.
    2) Trigger IB traffic on PF workload pods.
    3) For each PF workload pod, verify source port is 2001 and pull metrics using that source port.
    4) Validate Prometheus numeric lines in responses.
    5) Restore all modified NetworkConfig objects to their originals.
    """
    v1 = k8s_client.CoreV1Api()

    # 1) List NetworkConfig CRs and save originals (PF only)
    try:
        nc_items = nic_util.list_networkconfigs_custom(NC_NAMESPACE)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources in {NC_NAMESPACE}: {e}")

    if not nc_items:
        pytest.skip(f"No NetworkConfig resources in {NC_NAMESPACE}")

    originals = {}
    for it in nc_items:
        name = it.get("metadata", {}).get("name")
        if not name or name.startswith("vf-"):
            continue
        originals[name] = yaml.safe_load(yaml.safe_dump(it))

    if not originals:
        pytest.skip("No PF NetworkConfig found")

    # Build patch to set source port to 2001 and enable rbac
    patch_body = {
        "spec": {
            "metricsExporter": {
                "port": 2001,
                "rbacConfig": {
                    "clientCAConfigMap": {"name": "client-ca"},
                    "enable": True
                }
            }
        }
    }

    # Apply patches in parallel
    applied = []
    failed = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(originals)))) as ex:
        futures = {ex.submit(nic_util.patch_networkconfig_custom, NC_NAMESPACE, name, patch_body): name for name in originals.keys()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                fut.result()
                applied.append(name)
                LOG.info("PF Patched %s -> spec.metricsExporter.port=2001, rbacConfig.enable=True", name)
            except Exception as e:
                LOG.error("PF Failed to patch %s: %s", name, e)
                failed.append(name)

    if failed:
        # rollback applied ones and fail early
        for rn in applied:
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, originals[rn])
            except Exception as re:
                LOG.error("PF Rollback failed for %s: %s", rn, re)
        pytest.fail(f"PF Failed to apply source port 2001 + RBAC patch to some NetworkConfigs: {failed}")

    # Give operator time to reconcile
    time.sleep(2)

    # 2) Trigger IB traffic on PF workload pods
    all_pods = list_workloads(v1, namespace="default")
    wpods = [p for p in all_pods if not p.metadata.name.startswith("vf-workload")]
    if not wpods:
        # revert before skipping
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("PF Failed revert for %s during skip", rn)
        pytest.skip("No running PF workload pods in default namespace")

    exec_outputs = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(wpods)))) as ex:
        futures = {ex.submit(nic_util.run_ib_traffic, p.metadata.name, p.metadata.namespace): p for p in wpods}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                out = fut.result()
                exec_outputs.update(out)
            except Exception as e:
                LOG.warning("PF nic_util.run_ib_traffic failed for %s: %s", p.metadata.name, e)
                exec_outputs[p.metadata.name] = f"ERROR: {e}"

    # 3) Build map of NetworkConfig -> source port and nodePort (PF only)
    config_ports = {}
    for it in nic_util.list_networkconfigs_custom(NC_NAMESPACE):
        name = it.get("metadata", {}).get("name")
        if not name or name.startswith("vf-"):
            continue
        spec = it.get("spec", {})
        metrics_exp = spec.get("metricsExporter", {})
        source_port = metrics_exp.get("port")  # source port
        node_port = metrics_exp.get("nodePort")  # destination port
        config_ports[name] = {"source_port": source_port, "node_port": node_port}

    cfg = list(config_ports.keys())[0]

    # 4) Pull metrics using source port 2001 for each PF workload pod
    metrics_found = {}
    metrics_missing = []

    for p in wpods:
        pod_name = p.metadata.name
        node_name = getattr(p.spec, "node_name", None) or getattr(p.spec, "nodeName", None)
        if not node_name:
            LOG.warning("PF Pod %s has no nodeName; skipping", pod_name)
            metrics_missing.append((pod_name, "no-nodeName"))
            continue

        node_ip = nic_util.get_node_ip(node_name)
        if not node_ip:
            LOG.warning("PF No InternalIP for node %s (pod %s)", node_name, pod_name)
            metrics_missing.append((pod_name, f"no-node-ip node={node_name}"))
            continue

        ports = config_ports.get(cfg, {})
        source_port = ports.get("source_port")
        node_port = ports.get("node_port")

        # Verify source port is 2001
        if source_port != 2001:
            LOG.error("PF Source port mismatch for config %s: expected 2001, got %s", cfg, source_port)
            metrics_missing.append((pod_name, f"source-port-mismatch expected=2001 got={source_port}"))
            continue

        if not node_port:
            LOG.warning("PF No nodePort configured for config %s; skipping pod %s", cfg, pod_name)
            metrics_missing.append((pod_name, f"no-nodePort-for-config {cfg}"))
            continue

        LOG.info("PF Pulling metrics for pod %s -> node %s (%s) source_port=%s node_port=%s (config=%s)", 
                 pod_name, node_name, node_ip, source_port, node_port, cfg)

        # Try to pull metrics using source port 2001 with mTLS
        txt = None
        ok = False
        
        for attempt in range(3):
            txt = nic_util.curl_metrics_from_local(node_ip, node_port, METRICS_SERVICE_NAME, source_port=source_port, timeout=8)
            if txt and txt.strip():
                # check for prometheus numeric line
                for ln in txt.splitlines():
                    ln = ln.strip()
                    if not ln or ln.startswith("#"):
                        continue
                    if PROM_LINE_RE.match(ln):
                        ok = True
                        break
            if ok:
                break
            time.sleep(1)

        if ok:
            metrics_found[pod_name] = {"node": node_name, "node_ip": node_ip, "source_port": source_port, "node_port": node_port}
            LOG.info("PF Metrics OK for pod %s using source port %s", pod_name, source_port)
        else:
            sample = (txt or "")[:1000]
            LOG.warning("PF Failed to fetch valid metrics for pod %s using source port %s, sample=%s", pod_name, source_port, sample)
            metrics_missing.append((pod_name, f"no-metrics source_port={source_port} node_port={node_port} sample={sample}"))

    # 5) Revert all modified NetworkConfig objects
    revert_errors = []
    for rn, orig in originals.items():
        try:
            nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            LOG.info("PF Restored original NetworkConfig %s", rn)
        except Exception as e:
            LOG.error("PF Failed to restore %s: %s", rn, e)
            revert_errors.append((rn, str(e)))

    if revert_errors:
        LOG.error("PF One or more NetworkConfig originals failed to revert: %s", revert_errors)
        pytest.fail(f"PF Failed to revert original NetworkConfig(s): {revert_errors}")

    if metrics_missing:
        LOG.error("PF Metrics missing/invalid after traffic: %s", metrics_missing)
        pytest.fail(f"PF Metrics missing/invalid after traffic: {metrics_missing}")

    LOG.info("PF Successfully validated metrics using custom source port 2001 for pods: %s", list(metrics_found.keys()))


@pytest.mark.timeout(TEST_TIMEOUT)
def test_rbac_source_port_custom_vf():
    """
    Test RBAC metrics exporter using custom source port 2001 for VF configs.
    
    Workflow:
    1) Patch VF NetworkConfig objects to set spec.metricsExporter.port = 2001,
       spec.metricsExporter.rbacConfig.enable = True.
    2) Trigger IB traffic on VF workload pods.
    3) For each VF workload pod, verify source port is 2001 and pull metrics using that source port.
    4) Validate Prometheus numeric lines in responses.
    5) Restore all modified NetworkConfig objects to their originals.
    """
    v1 = k8s_client.CoreV1Api()

    # 1) List NetworkConfig CRs and save originals (VF only)
    try:
        nc_items = nic_util.list_networkconfigs_custom(NC_NAMESPACE)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources in {NC_NAMESPACE}: {e}")

    if not nc_items:
        pytest.skip(f"No NetworkConfig resources in {NC_NAMESPACE}")

    originals = {}
    for it in nc_items:
        name = it.get("metadata", {}).get("name")
        if not name or not name.startswith("vf-"):
            continue
        originals[name] = yaml.safe_load(yaml.safe_dump(it))

    if not originals:
        pytest.skip("No VF NetworkConfig found")

    # Build patch to set source port to 2001 and enable rbac
    patch_body = {
        "spec": {
            "metricsExporter": {
                "port": 2001,
                "rbacConfig": {
                    "clientCAConfigMap": {"name": "client-ca"},
                    "enable": True
                }
            }
        }
    }

    # Apply patches in parallel
    applied = []
    failed = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(originals)))) as ex:
        futures = {ex.submit(nic_util.patch_networkconfig_custom, NC_NAMESPACE, name, patch_body): name for name in originals.keys()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                fut.result()
                applied.append(name)
                LOG.info("VF Patched %s -> spec.metricsExporter.port=2001, rbacConfig.enable=True", name)
            except Exception as e:
                LOG.error("VF Failed to patch %s: %s", name, e)
                failed.append(name)

    if failed:
        # rollback applied ones and fail early
        for rn in applied:
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, originals[rn])
            except Exception as re:
                LOG.error("VF Rollback failed for %s: %s", rn, re)
        pytest.fail(f"VF Failed to apply source port 2001 + RBAC patch to some NetworkConfigs: {failed}")

    # Give operator time to reconcile
    time.sleep(2)

    # 2) Trigger IB traffic on VF workload pods
    all_pods = list_workloads(v1, namespace="default")
    wpods = [p for p in all_pods if p.metadata.name.startswith("vf-workload")]
    if not wpods:
        # revert before skipping
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("VF Failed revert for %s during skip", rn)
        pytest.skip("No running VF workload pods in default namespace")

    exec_outputs = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(wpods)))) as ex:
        futures = {ex.submit(nic_util.run_ib_traffic, p.metadata.name, p.metadata.namespace): p for p in wpods}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                out = fut.result()
                exec_outputs.update(out)
            except Exception as e:
                LOG.warning("VF nic_util.run_ib_traffic failed for %s: %s", p.metadata.name, e)
                exec_outputs[p.metadata.name] = f"ERROR: {e}"

    # 3) Build map of NetworkConfig -> source port and nodePort (VF only)
    config_ports = {}
    for it in nic_util.list_networkconfigs_custom(NC_NAMESPACE):
        name = it.get("metadata", {}).get("name")
        if not name or not name.startswith("vf-"):
            continue
        spec = it.get("spec", {})
        metrics_exp = spec.get("metricsExporter", {})
        source_port = metrics_exp.get("port")  # source port
        node_port = metrics_exp.get("nodePort")  # destination port
        config_ports[name] = {"source_port": source_port, "node_port": node_port}

    cfg = list(config_ports.keys())[0]

    # 4) Pull metrics using source port 2001 for each VF workload pod
    metrics_found = {}
    metrics_missing = []

    for p in wpods:
        pod_name = p.metadata.name
        node_name = getattr(p.spec, "node_name", None) or getattr(p.spec, "nodeName", None)
        if not node_name:
            LOG.warning("VF Pod %s has no nodeName; skipping", pod_name)
            metrics_missing.append((pod_name, "no-nodeName"))
            continue

        node_ip = nic_util.get_node_ip(node_name)
        if not node_ip:
            LOG.warning("VF No InternalIP for node %s (pod %s)", node_name, pod_name)
            metrics_missing.append((pod_name, f"no-node-ip node={node_name}"))
            continue

        ports = config_ports.get(cfg, {})
        source_port = ports.get("source_port")
        node_port = ports.get("node_port")

        # Verify source port is 2001
        if source_port != 2001:
            LOG.error("VF Source port mismatch for config %s: expected 2001, got %s", cfg, source_port)
            metrics_missing.append((pod_name, f"source-port-mismatch expected=2001 got={source_port}"))
            continue

        if not node_port:
            LOG.warning("VF No nodePort configured for config %s; skipping pod %s", cfg, pod_name)
            metrics_missing.append((pod_name, f"no-nodePort-for-config {cfg}"))
            continue

        LOG.info("VF Pulling metrics for pod %s -> node %s (%s) source_port=%s node_port=%s (config=%s)", 
                 pod_name, node_name, node_ip, source_port, node_port, cfg)

        # Try to pull metrics using source port 2001 with mTLS
        txt = None
        ok = False
        
        for attempt in range(3):
            txt = nic_util.curl_metrics_from_local(node_ip, node_port, METRICS_SERVICE_NAME, source_port=source_port, timeout=8)
            if txt and txt.strip():
                # check for prometheus numeric line
                for ln in txt.splitlines():
                    ln = ln.strip()
                    if not ln or ln.startswith("#"):
                        continue
                    if PROM_LINE_RE.match(ln):
                        ok = True
                        break
            if ok:
                break
            time.sleep(1)

        if ok:
            metrics_found[pod_name] = {"node": node_name, "node_ip": node_ip, "source_port": source_port, "node_port": node_port}
            LOG.info("VF Metrics OK for pod %s using source port %s", pod_name, source_port)
        else:
            sample = (txt or "")[:1000]
            LOG.warning("VF Failed to fetch valid metrics for pod %s using source port %s, sample=%s", pod_name, source_port, sample)
            metrics_missing.append((pod_name, f"no-metrics source_port={source_port} node_port={node_port} sample={sample}"))

    # 5) Revert all modified NetworkConfig objects
    revert_errors = []
    for rn, orig in originals.items():
        try:
            nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            LOG.info("VF Restored original NetworkConfig %s", rn)
        except Exception as e:
            LOG.error("VF Failed to restore %s: %s", rn, e)
            revert_errors.append((rn, str(e)))

    if revert_errors:
        LOG.error("VF One or more NetworkConfig originals failed to revert: %s", revert_errors)
        pytest.fail(f"VF Failed to revert original NetworkConfig(s): {revert_errors}")

    if metrics_missing:
        LOG.error("VF Metrics missing/invalid after traffic: %s", metrics_missing)
        pytest.fail(f"VF Metrics missing/invalid after traffic: {metrics_missing}")

    LOG.info("VF Successfully validated metrics using custom source port 2001 for pods: %s", list(metrics_found.keys()))


@pytest.mark.timeout(TEST_TIMEOUT)
def test_rbac_disable_https_true_metrics_fail_pf():
    """
    Test that when RBAC is enabled with disableHttps=True for PF configs, metrics fetch should fail.
    
    Workflow:
    1) Patch PF NetworkConfig objects to set:
       - spec.metricsExporter.rbacConfig.enable = True
       - spec.metricsExporter.rbacConfig.disableHttps = True
       - spec.metricsExporter.rbacConfig.clientCAConfigMap.name = "client-ca"
    2) Trigger IB traffic on PF workload pods.
    3) Try to fetch metrics using mTLS (nic_util.curl_metrics_from_local with HTTPS).
    4) Verify that metrics fetch fails (disableHttps=True means HTTPS is disabled).
    5) Restore all modified NetworkConfig objects to their originals.
    """
    v1 = k8s_client.CoreV1Api()

    # 1) List NetworkConfig CRs and save originals (PF only)
    try:
        nc_items = nic_util.list_networkconfigs_custom(NC_NAMESPACE)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources in {NC_NAMESPACE}: {e}")

    if not nc_items:
        pytest.skip(f"No NetworkConfig resources in {NC_NAMESPACE}")

    originals = {}
    for it in nc_items:
        name = it.get("metadata", {}).get("name")
        if not name or name.startswith("vf-"):
            continue
        originals[name] = yaml.safe_load(yaml.safe_dump(it))

    if not originals:
        pytest.skip("No PF NetworkConfig found")

    # Build patch to enable RBAC with disableHttps=True
    patch_body = {
        "spec": {
            "metricsExporter": {
                "rbacConfig": {
                    "clientCAConfigMap": {"name": "client-ca"},
                    "enable": True,
                    "disableHttps": True
                }
            }
        }
    }

    # Apply patches in parallel
    applied = []
    failed = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(originals)))) as ex:
        futures = {ex.submit(nic_util.patch_networkconfig_custom, NC_NAMESPACE, name, patch_body): name for name in originals.keys()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                fut.result()
                applied.append(name)
                LOG.info("PF Patched %s -> spec.metricsExporter.rbacConfig.enable=True, disableHttps=True", name)
            except Exception as e:
                LOG.error("Failed to patch PF %s: %s", name, e)
                failed.append(name)

    if failed:
        # rollback applied ones and fail early
        for rn in applied:
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, originals[rn])
            except Exception as re:
                LOG.error("Rollback failed for %s: %s", rn, re)
        pytest.fail(f"Failed to apply RBAC disableHttps patch to some PF NetworkConfigs: {failed}")

    # Give operator time to reconcile
    time.sleep(2)

    # 2) Trigger IB traffic on PF workload pods
    all_pods = list_workloads(v1, namespace="default")
    wpods = [p for p in all_pods if not p.metadata.name.startswith("vf-workload")]
    if not wpods:
        # revert before skipping
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("Failed revert for %s during skip", rn)
        pytest.skip("No running PF workload pods in default namespace")

    exec_outputs = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(wpods)))) as ex:
        futures = {ex.submit(nic_util.run_ib_traffic, p.metadata.name, p.metadata.namespace): p for p in wpods}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                out = fut.result()
                exec_outputs.update(out)
            except Exception as e:
                LOG.warning("nic_util.run_ib_traffic failed for %s: %s", p.metadata.name, e)
                exec_outputs[p.metadata.name] = f"ERROR: {e}"

    # 3) Build map of PF NetworkConfig -> nodePort
    nodeport_by_config = {}
    for it in nic_util.list_networkconfigs_custom(NC_NAMESPACE):
        name = it.get("metadata", {}).get("name")
        if not name or name.startswith("vf-"):
            continue
        try:
            np = nic_util.get_metrics_nodeport_from_networkconfig(NC_NAMESPACE, name)
        except Exception:
            np = None
        nodeport_by_config[name] = np

    cfg = list(nodeport_by_config.keys())[0] if nodeport_by_config else None
    if not cfg:
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("Failed revert during no-config skip")
        pytest.skip("No PF NetworkConfig with nodePort found")

    # 4) Try to fetch metrics with mTLS - should fail when disableHttps=True
    metrics_failed_correctly = {}
    metrics_succeeded_incorrectly = []

    port = nodeport_by_config.get(cfg)
    if not port:
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("Failed revert during no-port skip")
        pytest.skip(f"No numeric nodePort for PF config {cfg}")

    for p in wpods:
        pod_name = p.metadata.name
        node_name = getattr(p.spec, "node_name", None) or getattr(p.spec, "nodeName", None)
        if not node_name:
            LOG.warning("PF Pod %s has no nodeName; skipping", pod_name)
            continue

        node_ip = nic_util.get_node_ip(node_name)
        if not node_ip:
            LOG.warning("No InternalIP for node %s (PF pod %s)", node_name, pod_name)
            continue

        LOG.info("PF Attempting HTTPS metrics fetch for pod %s -> node %s (%s) port %s (config=%s) with disableHttps=True", 
                 pod_name, node_name, node_ip, port, cfg)

        # Try to fetch metrics with HTTPS/mTLS - this should fail
        txt = nic_util.curl_metrics_from_local(node_ip, port, METRICS_SERVICE_NAME, timeout=8)
        
        # Check if we got valid Prometheus metrics (which would be incorrect when HTTPS is disabled)
        has_valid_metrics = False
        if txt and txt.strip():
            for ln in txt.splitlines():
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                if PROM_LINE_RE.match(ln):
                    has_valid_metrics = True
                    break

        if has_valid_metrics:
            LOG.error("PF Metrics fetch succeeded for pod %s when disableHttps=True - this should fail!", pod_name)
            metrics_succeeded_incorrectly.append((pod_name, f"node_ip={node_ip} port={port}"))
        else:
            LOG.info("PF Metrics fetch correctly failed for pod %s with disableHttps=True", pod_name)
            metrics_failed_correctly[pod_name] = {"node": node_name, "node_ip": node_ip, "port": port}

    # 5) Revert all modified PF NetworkConfig objects
    revert_errors = []
    for rn, orig in originals.items():
        try:
            nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            LOG.info("Restored original PF NetworkConfig %s", rn)
        except Exception as e:
            LOG.error("Failed to restore PF %s: %s", rn, e)
            revert_errors.append((rn, str(e)))

    if revert_errors:
        LOG.error("One or more PF NetworkConfig originals failed to revert: %s", revert_errors)
        pytest.fail(f"Failed to revert original PF NetworkConfig(s): {revert_errors}")

    if metrics_succeeded_incorrectly:
        LOG.error("PF Metrics fetch succeeded when disableHttps=True for: %s", metrics_succeeded_incorrectly)
        pytest.fail(f"PF Metrics fetch should fail when disableHttps=True but succeeded for: {metrics_succeeded_incorrectly}")

    LOG.info("PF Successfully verified that HTTPS metrics fetch fails with disableHttps=True for pods: %s", list(metrics_failed_correctly.keys()))


@pytest.mark.timeout(TEST_TIMEOUT)
def test_rbac_disable_https_false_metrics_succeed_pf():
    """
    Test that when RBAC is enabled with disableHttps=False for PF configs, metrics fetch should succeed.
    
    Workflow:
    1) Patch PF NetworkConfig objects to set:
       - spec.metricsExporter.rbacConfig.enable = True
       - spec.metricsExporter.rbacConfig.disableHttps = False
       - spec.metricsExporter.rbacConfig.clientCAConfigMap.name = "client-ca"
    2) Trigger IB traffic on PF workload pods.
    3) Fetch metrics using mTLS (nic_util.curl_metrics_from_local with HTTPS).
    4) Verify that metrics fetch succeeds (disableHttps=False means HTTPS is enabled).
    5) Restore all modified NetworkConfig objects to their originals.
    """
    v1 = k8s_client.CoreV1Api()

    # 1) List NetworkConfig CRs and save originals (PF only)
    try:
        nc_items = nic_util.list_networkconfigs_custom(NC_NAMESPACE)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources in {NC_NAMESPACE}: {e}")

    if not nc_items:
        pytest.skip(f"No NetworkConfig resources in {NC_NAMESPACE}")

    originals = {}
    for it in nc_items:
        name = it.get("metadata", {}).get("name")
        if not name or name.startswith("vf-"):
            continue
        originals[name] = yaml.safe_load(yaml.safe_dump(it))

    if not originals:
        pytest.skip("No PF NetworkConfig found")

    # Build patch to enable RBAC with disableHttps=False
    patch_body = {
        "spec": {
            "metricsExporter": {
                "rbacConfig": {
                    "clientCAConfigMap": {"name": "client-ca"},
                    "enable": True,
                    "disableHttps": False
                }
            }
        }
    }

    # Apply patches in parallel
    applied = []
    failed = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(originals)))) as ex:
        futures = {ex.submit(nic_util.patch_networkconfig_custom, NC_NAMESPACE, name, patch_body): name for name in originals.keys()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                fut.result()
                applied.append(name)
                LOG.info("PF Patched %s -> spec.metricsExporter.rbacConfig.enable=True, disableHttps=False", name)
            except Exception as e:
                LOG.error("Failed to patch PF %s: %s", name, e)
                failed.append(name)

    if failed:
        # rollback applied ones and fail early
        for rn in applied:
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, originals[rn])
            except Exception as re:
                LOG.error("Rollback failed for %s: %s", rn, re)
        pytest.fail(f"Failed to apply RBAC disableHttps patch to some PF NetworkConfigs: {failed}")

    # Give operator time to reconcile
    time.sleep(2)

    # 2) Trigger IB traffic on PF workload pods
    all_pods = list_workloads(v1, namespace="default")
    wpods = [p for p in all_pods if not p.metadata.name.startswith("vf-workload")]
    if not wpods:
        # revert before skipping
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("Failed revert for %s during skip", rn)
        pytest.skip("No running PF workload pods in default namespace")

    exec_outputs = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(wpods)))) as ex:
        futures = {ex.submit(nic_util.run_ib_traffic, p.metadata.name, p.metadata.namespace): p for p in wpods}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                out = fut.result()
                exec_outputs.update(out)
            except Exception as e:
                LOG.warning("nic_util.run_ib_traffic failed for %s: %s", p.metadata.name, e)
                exec_outputs[p.metadata.name] = f"ERROR: {e}"

    # 3) Build map of PF NetworkConfig -> nodePort
    nodeport_by_config = {}
    for it in nic_util.list_networkconfigs_custom(NC_NAMESPACE):
        name = it.get("metadata", {}).get("name")
        if not name or name.startswith("vf-"):
            continue
        try:
            np = nic_util.get_metrics_nodeport_from_networkconfig(NC_NAMESPACE, name)
        except Exception:
            np = None
        nodeport_by_config[name] = np

    cfg = list(nodeport_by_config.keys())[0] if nodeport_by_config else None
    if not cfg:
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("Failed revert during no-config skip")
        pytest.skip("No PF NetworkConfig with nodePort found")

    # 4) Fetch metrics with mTLS - should succeed when disableHttps=False
    metrics_found = {}
    metrics_missing = []

    port = nodeport_by_config.get(cfg)
    if not port:
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("Failed revert during no-port skip")
        pytest.skip(f"No numeric nodePort for PF config {cfg}")

    for p in wpods:
        pod_name = p.metadata.name
        node_name = getattr(p.spec, "node_name", None) or getattr(p.spec, "nodeName", None)
        if not node_name:
            LOG.warning("PF Pod %s has no nodeName; skipping", pod_name)
            metrics_missing.append((pod_name, "no-nodeName"))
            continue

        node_ip = nic_util.get_node_ip(node_name)
        if not node_ip:
            LOG.warning("No InternalIP for node %s (PF pod %s)", node_name, pod_name)
            metrics_missing.append((pod_name, f"no-node-ip node={node_name}"))
            continue

        LOG.info("PF Pulling HTTPS metrics for pod %s -> node %s (%s) port %s (config=%s) with disableHttps=False", 
                 pod_name, node_name, node_ip, port, cfg)

        # Try a few times (small retries) to account for timing
        txt = None
        ok = False
        for attempt in range(3):
            txt = nic_util.curl_metrics_from_local(node_ip, port, METRICS_SERVICE_NAME, timeout=8)
            if txt and txt.strip():
                # check for prometheus numeric line
                for ln in txt.splitlines():
                    ln = ln.strip()
                    if not ln or ln.startswith("#"):
                        continue
                    if PROM_LINE_RE.match(ln):
                        ok = True
                        break
            if ok:
                break
            time.sleep(1)

        if ok:
            metrics_found[pod_name] = {"node": node_name, "node_ip": node_ip, "port": port}
            LOG.info("PF Metrics OK for pod %s on %s:%s with disableHttps=False", pod_name, node_ip, port)
        else:
            sample = (txt or "")[:1000]
            LOG.warning("PF Failed to fetch valid metrics for pod %s from %s:%s sample=%s", pod_name, node_ip, port, sample)
            metrics_missing.append((pod_name, f"no-metrics node_ip={node_ip} port={port} sample={sample}"))

    # 5) Revert all modified PF NetworkConfig objects
    revert_errors = []
    for rn, orig in originals.items():
        try:
            nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            LOG.info("Restored original PF NetworkConfig %s", rn)
        except Exception as e:
            LOG.error("Failed to restore PF %s: %s", rn, e)
            revert_errors.append((rn, str(e)))

    if revert_errors:
        LOG.error("One or more PF NetworkConfig originals failed to revert: %s", revert_errors)
        pytest.fail(f"Failed to revert original PF NetworkConfig(s): {revert_errors}")

    if metrics_missing:
        LOG.error("PF Metrics missing/invalid after traffic with disableHttps=False: %s", metrics_missing)
        pytest.fail(f"PF Metrics missing/invalid after traffic with disableHttps=False: {metrics_missing}")

    LOG.info("PF Successfully validated HTTPS metrics with disableHttps=False for pods: %s", list(metrics_found.keys()))


@pytest.mark.timeout(TEST_TIMEOUT)
def test_rbac_disable_https_true_metrics_fail_vf():
    """
    Test that when RBAC is enabled with disableHttps=True for VF configs, metrics fetch should fail.
    
    Workflow:
    1) Patch VF NetworkConfig objects to set:
       - spec.metricsExporter.rbacConfig.enable = True
       - spec.metricsExporter.rbacConfig.disableHttps = True
       - spec.metricsExporter.rbacConfig.clientCAConfigMap.name = "client-ca"
    2) Trigger IB traffic on VF workload pods.
    3) Try to fetch metrics using mTLS (nic_util.curl_metrics_from_local with HTTPS).
    4) Verify that metrics fetch fails (disableHttps=True means HTTPS is disabled).
    5) Restore all modified NetworkConfig objects to their originals.
    """
    v1 = k8s_client.CoreV1Api()

    # 1) List NetworkConfig CRs and save originals (VF only)
    try:
        nc_items = nic_util.list_networkconfigs_custom(NC_NAMESPACE)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources in {NC_NAMESPACE}: {e}")

    if not nc_items:
        pytest.skip(f"No NetworkConfig resources in {NC_NAMESPACE}")

    originals = {}
    for it in nc_items:
        name = it.get("metadata", {}).get("name")
        if not name or not name.startswith("vf-"):
            continue
        originals[name] = yaml.safe_load(yaml.safe_dump(it))

    if not originals:
        pytest.skip("No VF NetworkConfig found")

    # Build patch to enable RBAC with disableHttps=True
    patch_body = {
        "spec": {
            "metricsExporter": {
                "rbacConfig": {
                    "clientCAConfigMap": {"name": "client-ca"},
                    "enable": True,
                    "disableHttps": True
                }
            }
        }
    }

    # Apply patches in parallel
    applied = []
    failed = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(originals)))) as ex:
        futures = {ex.submit(nic_util.patch_networkconfig_custom, NC_NAMESPACE, name, patch_body): name for name in originals.keys()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                fut.result()
                applied.append(name)
                LOG.info("VF Patched %s -> spec.metricsExporter.rbacConfig.enable=True, disableHttps=True", name)
            except Exception as e:
                LOG.error("Failed to patch VF %s: %s", name, e)
                failed.append(name)

    if failed:
        # rollback applied ones and fail early
        for rn in applied:
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, originals[rn])
            except Exception as re:
                LOG.error("Rollback failed for %s: %s", rn, re)
        pytest.fail(f"Failed to apply RBAC disableHttps patch to some VF NetworkConfigs: {failed}")

    # Give operator time to reconcile
    time.sleep(2)

    # 2) Trigger IB traffic on VF workload pods
    all_pods = list_workloads(v1, namespace="default")
    wpods = [p for p in all_pods if p.metadata.name.startswith("vf-workload")]
    if not wpods:
        # revert before skipping
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("Failed revert for %s during skip", rn)
        pytest.skip("No running VF workload pods in default namespace")

    exec_outputs = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(wpods)))) as ex:
        futures = {ex.submit(nic_util.run_ib_traffic, p.metadata.name, p.metadata.namespace): p for p in wpods}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                out = fut.result()
                exec_outputs.update(out)
            except Exception as e:
                LOG.warning("nic_util.run_ib_traffic failed for %s: %s", p.metadata.name, e)
                exec_outputs[p.metadata.name] = f"ERROR: {e}"

    # 3) Build map of VF NetworkConfig -> nodePort
    nodeport_by_config = {}
    for it in nic_util.list_networkconfigs_custom(NC_NAMESPACE):
        name = it.get("metadata", {}).get("name")
        if not name or not name.startswith("vf-"):
            continue
        try:
            np = nic_util.get_metrics_nodeport_from_networkconfig(NC_NAMESPACE, name)
        except Exception:
            np = None
        nodeport_by_config[name] = np

    cfg = list(nodeport_by_config.keys())[0] if nodeport_by_config else None
    if not cfg:
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("Failed revert during no-config skip")
        pytest.skip("No VF NetworkConfig with nodePort found")

    # 4) Try to fetch metrics with mTLS - should fail when disableHttps=True
    metrics_failed_correctly = {}
    metrics_succeeded_incorrectly = []

    port = nodeport_by_config.get(cfg)
    if not port:
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("Failed revert during no-port skip")
        pytest.skip(f"No numeric nodePort for VF config {cfg}")

    for p in wpods:
        pod_name = p.metadata.name
        node_name = getattr(p.spec, "node_name", None) or getattr(p.spec, "nodeName", None)
        if not node_name:
            LOG.warning("VF Pod %s has no nodeName; skipping", pod_name)
            continue

        node_ip = nic_util.get_node_ip(node_name)
        if not node_ip:
            LOG.warning("No InternalIP for node %s (VF pod %s)", node_name, pod_name)
            continue

        LOG.info("VF Attempting HTTPS metrics fetch for pod %s -> node %s (%s) port %s (config=%s) with disableHttps=True", 
                 pod_name, node_name, node_ip, port, cfg)

        # Try to fetch metrics with HTTPS/mTLS - this should fail
        txt = nic_util.curl_metrics_from_local(node_ip, port, METRICS_SERVICE_NAME, timeout=8)
        
        # Check if we got valid Prometheus metrics (which would be incorrect when HTTPS is disabled)
        has_valid_metrics = False
        if txt and txt.strip():
            for ln in txt.splitlines():
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                if PROM_LINE_RE.match(ln):
                    has_valid_metrics = True
                    break

        if has_valid_metrics:
            LOG.error("VF Metrics fetch succeeded for pod %s when disableHttps=True - this should fail!", pod_name)
            metrics_succeeded_incorrectly.append((pod_name, f"node_ip={node_ip} port={port}"))
        else:
            LOG.info("VF Metrics fetch correctly failed for pod %s with disableHttps=True", pod_name)
            metrics_failed_correctly[pod_name] = {"node": node_name, "node_ip": node_ip, "port": port}

    # 5) Revert all modified VF NetworkConfig objects
    revert_errors = []
    for rn, orig in originals.items():
        try:
            nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            LOG.info("Restored original VF NetworkConfig %s", rn)
        except Exception as e:
            LOG.error("Failed to restore VF %s: %s", rn, e)
            revert_errors.append((rn, str(e)))

    if revert_errors:
        LOG.error("One or more VF NetworkConfig originals failed to revert: %s", revert_errors)
        pytest.fail(f"Failed to revert original VF NetworkConfig(s): {revert_errors}")

    if metrics_succeeded_incorrectly:
        LOG.error("VF Metrics fetch succeeded when disableHttps=True for: %s", metrics_succeeded_incorrectly)
        pytest.fail(f"VF Metrics fetch should fail when disableHttps=True but succeeded for: {metrics_succeeded_incorrectly}")

    LOG.info("VF Successfully verified that HTTPS metrics fetch fails with disableHttps=True for pods: %s", list(metrics_failed_correctly.keys()))


@pytest.mark.timeout(TEST_TIMEOUT)
def test_rbac_disable_https_false_metrics_succeed_vf():
    """
    Test that when RBAC is enabled with disableHttps=False for VF configs, metrics fetch should succeed.
    
    Workflow:
    1) Patch VF NetworkConfig objects to set:
       - spec.metricsExporter.rbacConfig.enable = True
       - spec.metricsExporter.rbacConfig.disableHttps = False
       - spec.metricsExporter.rbacConfig.clientCAConfigMap.name = "client-ca"
    2) Trigger IB traffic on VF workload pods.
    3) Fetch metrics using mTLS (nic_util.curl_metrics_from_local with HTTPS).
    4) Verify that metrics fetch succeeds (disableHttps=False means HTTPS is enabled).
    5) Restore all modified NetworkConfig objects to their originals.
    """
    v1 = k8s_client.CoreV1Api()

    # 1) List NetworkConfig CRs and save originals (VF only)
    try:
        nc_items = nic_util.list_networkconfigs_custom(NC_NAMESPACE)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources in {NC_NAMESPACE}: {e}")

    if not nc_items:
        pytest.skip(f"No NetworkConfig resources in {NC_NAMESPACE}")

    originals = {}
    for it in nc_items:
        name = it.get("metadata", {}).get("name")
        if not name or not name.startswith("vf-"):
            continue
        originals[name] = yaml.safe_load(yaml.safe_dump(it))

    if not originals:
        pytest.skip("No VF NetworkConfig found")

    # Build patch to enable RBAC with disableHttps=False
    patch_body = {
        "spec": {
            "metricsExporter": {
                "rbacConfig": {
                    "clientCAConfigMap": {"name": "client-ca"},
                    "enable": True,
                    "disableHttps": False
                }
            }
        }
    }

    # Apply patches in parallel
    applied = []
    failed = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(originals)))) as ex:
        futures = {ex.submit(nic_util.patch_networkconfig_custom, NC_NAMESPACE, name, patch_body): name for name in originals.keys()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                fut.result()
                applied.append(name)
                LOG.info("VF Patched %s -> spec.metricsExporter.rbacConfig.enable=True, disableHttps=False", name)
            except Exception as e:
                LOG.error("Failed to patch VF %s: %s", name, e)
                failed.append(name)

    if failed:
        # rollback applied ones and fail early
        for rn in applied:
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, originals[rn])
            except Exception as re:
                LOG.error("Rollback failed for %s: %s", rn, re)
        pytest.fail(f"Failed to apply RBAC disableHttps patch to some VF NetworkConfigs: {failed}")

    # Give operator time to reconcile
    time.sleep(2)

    # 2) Trigger IB traffic on VF workload pods
    all_pods = list_workloads(v1, namespace="default")
    wpods = [p for p in all_pods if p.metadata.name.startswith("vf-workload")]
    if not wpods:
        # revert before skipping
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("Failed revert for %s during skip", rn)
        pytest.skip("No running VF workload pods in default namespace")

    exec_outputs = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(wpods)))) as ex:
        futures = {ex.submit(nic_util.run_ib_traffic, p.metadata.name, p.metadata.namespace): p for p in wpods}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                out = fut.result()
                exec_outputs.update(out)
            except Exception as e:
                LOG.warning("nic_util.run_ib_traffic failed for %s: %s", p.metadata.name, e)
                exec_outputs[p.metadata.name] = f"ERROR: {e}"

    # 3) Build map of VF NetworkConfig -> nodePort
    nodeport_by_config = {}
    for it in nic_util.list_networkconfigs_custom(NC_NAMESPACE):
        name = it.get("metadata", {}).get("name")
        if not name or not name.startswith("vf-"):
            continue
        try:
            np = nic_util.get_metrics_nodeport_from_networkconfig(NC_NAMESPACE, name)
        except Exception:
            np = None
        nodeport_by_config[name] = np

    cfg = list(nodeport_by_config.keys())[0] if nodeport_by_config else None
    if not cfg:
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("Failed revert during no-config skip")
        pytest.skip("No VF NetworkConfig with nodePort found")

    # 4) Fetch metrics with mTLS - should succeed when disableHttps=False
    metrics_found = {}
    metrics_missing = []

    port = nodeport_by_config.get(cfg)
    if not port:
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            except Exception:
                LOG.error("Failed revert during no-port skip")
        pytest.skip(f"No numeric nodePort for VF config {cfg}")

    for p in wpods:
        pod_name = p.metadata.name
        node_name = getattr(p.spec, "node_name", None) or getattr(p.spec, "nodeName", None)
        if not node_name:
            LOG.warning("VF Pod %s has no nodeName; skipping", pod_name)
            metrics_missing.append((pod_name, "no-nodeName"))
            continue

        node_ip = nic_util.get_node_ip(node_name)
        if not node_ip:
            LOG.warning("No InternalIP for node %s (VF pod %s)", node_name, pod_name)
            metrics_missing.append((pod_name, f"no-node-ip node={node_name}"))
            continue

        LOG.info("VF Pulling HTTPS metrics for pod %s -> node %s (%s) port %s (config=%s) with disableHttps=False", 
                 pod_name, node_name, node_ip, port, cfg)

        # Try a few times (small retries) to account for timing
        txt = None
        ok = False
        for attempt in range(3):
            txt = nic_util.curl_metrics_from_local(node_ip, port, METRICS_SERVICE_NAME, timeout=8)
            if txt and txt.strip():
                # check for prometheus numeric line
                for ln in txt.splitlines():
                    ln = ln.strip()
                    if not ln or ln.startswith("#"):
                        continue
                    if PROM_LINE_RE.match(ln):
                        ok = True
                        break
            if ok:
                break
            time.sleep(1)

        if ok:
            metrics_found[pod_name] = {"node": node_name, "node_ip": node_ip, "port": port}
            LOG.info("VF Metrics OK for pod %s on %s:%s with disableHttps=False", pod_name, node_ip, port)
        else:
            sample = (txt or "")[:1000]
            LOG.warning("VF Failed to fetch valid metrics for pod %s from %s:%s sample=%s", pod_name, node_ip, port, sample)
            metrics_missing.append((pod_name, f"no-metrics node_ip={node_ip} port={port} sample={sample}"))

    # 5) Revert all modified VF NetworkConfig objects
    revert_errors = []
    for rn, orig in originals.items():
        try:
            nic_util.replace_with_retry(NC_NAMESPACE, rn, orig)
            LOG.info("Restored original VF NetworkConfig %s", rn)
        except Exception as e:
            LOG.error("Failed to restore VF %s: %s", rn, e)
            revert_errors.append((rn, str(e)))

    if revert_errors:
        LOG.error("One or more VF NetworkConfig originals failed to revert: %s", revert_errors)
        pytest.fail(f"Failed to revert original VF NetworkConfig(s): {revert_errors}")

    if metrics_missing:
        LOG.error("VF Metrics missing/invalid after traffic with disableHttps=False: %s", metrics_missing)
        pytest.fail(f"VF Metrics missing/invalid after traffic with disableHttps=False: {metrics_missing}")

    LOG.info("VF Successfully validated HTTPS metrics with disableHttps=False for pods: %s", list(metrics_found.keys()))
