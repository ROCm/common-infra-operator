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

import os
import re
import json
import time
import yaml
import pytest
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, List

from kubernetes import client as k8s_client
from kubernetes.client.exceptions import ApiException


import lib.nic_util as nic_util
from lib.nic_util import (
    MAX_WORKERS,
    PROM_LINE_RE,
    SERVER_STARTUP_DELAY,
)

LOG = logging.getLogger(__name__)
TEST_TIMEOUT = 180


# Deliberately still on the raw client: k8_util.k8_get_pods returns List[dict]
# (via .to_dict(), which also snake_cases nested fields), but nic_util consumers
# such as get_rdma_interfaces are typed for V1Pod. Rewiring here alone would
# break them, so this moves with the nic_util signature change.
def list_pods(v1, namespace=None):
    if namespace:
        return v1.list_namespaced_pod(namespace).items
    return v1.list_pod_for_all_namespaces().items


def list_workloads(v1, namespace="default"):
    return [p for p in list_pods(v1, namespace) if p.status.phase == "Running"]


# nic_util.configmap_metrics_port reads NICConfig.ServerPort, but this suite's
# configmap.yaml and config-nic.json declare the port at the top level of
# config.json, so it would always fall back to the 5001 default here.
def configmap_metrics_port(config_map_doc: Dict[str, Any]) -> int:
    """Read the metrics server port from a ConfigMap doc's config.json/config-nic.json."""
    data = config_map_doc.get("data") or {}
    raw_cfg = data.get("config.json") or data.get("config-nic.json")
    if not raw_cfg:
        raise ValueError("configmap missing data.config.json and data.config-nic.json")
    cfg = json.loads(raw_cfg)
    port = cfg.get("ServicePort") if cfg.get("ServicePort") is not None else cfg.get("ServerPort")
    if port is None:
        raise ValueError("data.config.json missing both ServicePort and ServerPort")
    return int(port)


def set_exporter_configmap(namespace: str, name: str, configmap_name: str) -> None:
    """Point a NetworkConfig's metricsExporter.config.name at *configmap_name*."""
    nic_util.patch_networkconfig_custom(
        namespace, name, {"spec": {"metricsExporter": {"config": {"name": configmap_name}}}}
    )


def clear_exporter_configmap(namespace: str, name: str) -> None:
    """Drop metricsExporter.config from a NetworkConfig (merge-patch null removes the key)."""
    nic_util.patch_networkconfig_custom(
        namespace, name, {"spec": {"metricsExporter": {"config": None}}}
    )


CUSTOM_CONFIGMAP_NAME_PF = "nic-config-custom-pf"
CUSTOM_CONFIGMAP_NAME_VF = "nic-config-custom-vf"
CUSTOM_PREFIX_CONFIGMAP_NAME_PF = "nic-config-prefix-pf"
CUSTOM_PREFIX_CONFIGMAP_NAME_VF = "nic-config-prefix-vf"
CUSTOM_LABELS_CONFIGMAP_NAME_PF = "nic-config-labels-pf"
CUSTOM_LABELS_CONFIGMAP_NAME_VF = "nic-config-labels-vf"
# Field manipulation test configmaps
REMOVE_ETH_FIELDS_CONFIGMAP_NAME_PF = "nic-config-remove-eth-fields-pf"
REMOVE_ETH_FIELDS_CONFIGMAP_NAME_VF = "nic-config-remove-eth-fields-vf"
EXCLUDE_QP_FIELDS_CONFIGMAP_NAME_PF = "nic-config-exclude-qp-fields-pf"
CONFIG_NIC_CONFIGMAP_NAME_PF = "nic-config-nic-json-pf"
CONFIG_NIC_CONFIGMAP_NAME_VF = "nic-config-nic-json-vf"
REPLACE_FIELDS_CONFIGMAP_NAME_PF = "nic-config-replace-fields-pf"
REPLACE_FIELDS_CONFIGMAP_NAME_VF = "nic-config-replace-fields-vf"
EMPTY_FIELDS_CONFIGMAP_NAME_PF = "nic-config-empty-fields-pf"
EMPTY_FIELDS_CONFIGMAP_NAME_VF = "nic-config-empty-fields-vf"
# Label manipulation test configmaps
REMOVE_LABELS_CONFIGMAP_NAME_PF = "nic-config-remove-labels-pf"
REMOVE_LABELS_CONFIGMAP_NAME_VF = "nic-config-remove-labels-vf"
CONFIGMAP_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "configmap.yaml")
CONFIG_NIC_JSON_PATH = os.path.join(os.path.dirname(__file__), "config-nic.json")


@pytest.mark.timeout(TEST_TIMEOUT)
def test_all_pods_running():
    v1 = k8s_client.CoreV1Api()
    pods = list_pods(v1)
    for p in pods:
        if p.status.phase != "Running":
            LOG.error("Not Ok pod=%s", p.metadata.name)
    LOG.info("Checked pod phases.")


@pytest.mark.timeout(TEST_TIMEOUT * 3)
def test_ib_traffic_across_workloads():
    """Run ib_write_bw traffic across workload pods on each RDMA interface."""
    v1 = k8s_client.CoreV1Api()

    workloads = list_workloads(v1, namespace="default")

    if len(workloads) < 2:
        pytest.skip(f"Need at least 2 running workload pods, found {len(workloads)}")

    pod_a = workloads[0]
    pod_b = workloads[1]
    LOG.info("Selected pods: server=%s, client=%s", pod_a.metadata.name, pod_b.metadata.name)

    ifaces_a = nic_util.get_rdma_interfaces(pod_a)
    ifaces_b = nic_util.get_rdma_interfaces(pod_b)

    if not ifaces_a:
        pytest.fail(f"Pod {pod_a.metadata.name} has no RDMA interfaces in network-status annotation")
    if not ifaces_b:
        pytest.fail(f"Pod {pod_b.metadata.name} has no RDMA interfaces in network-status annotation")

    num_pairs = min(len(ifaces_a), len(ifaces_b))
    LOG.info("Will run traffic on %d interface pair(s)", num_pairs)

    failures = []

    for idx in range(num_pairs):
        server_iface = ifaces_a[idx]
        client_iface = ifaces_b[idx]

        LOG.info(
            "--- Interface pair %d/%d ---\n"
            "  Server: pod=%s iface=%s device=%s ip=%s\n"
            "  Client: pod=%s iface=%s device=%s ip=%s",
            idx + 1, num_pairs,
            pod_a.metadata.name, server_iface["interface"],
            server_iface["rdma_device"], server_iface["ip"],
            pod_b.metadata.name, client_iface["interface"],
            client_iface["rdma_device"], client_iface["ip"],
        )

        server_out = ""
        client_out = ""

        try:
            with ThreadPoolExecutor(max_workers=2) as ex:
                server_future = ex.submit(
                    nic_util.run_ib_server, pod_a.metadata.name, pod_a.metadata.namespace,
                    server_iface["rdma_device"],
                )
                time.sleep(SERVER_STARTUP_DELAY)

                client_future = ex.submit(
                    nic_util.run_ib_client, pod_b.metadata.name, pod_b.metadata.namespace,
                    client_iface["rdma_device"], server_iface["ip"],
                )

                client_out = client_future.result()
                server_out = server_future.result()

            LOG.info("Interface %d server output:\n%s", idx + 1, server_out[:500])
            LOG.info("Interface %d client output:\n%s", idx + 1, client_out[:500])

            if server_out.startswith("ERROR:"):
                failures.append({
                    "interface_idx": idx,
                    "role": "server",
                    "pod": pod_a.metadata.name,
                    "device": server_iface["rdma_device"],
                    "error": server_out,
                })
            if client_out.startswith("ERROR:"):
                failures.append({
                    "interface_idx": idx,
                    "role": "client",
                    "pod": pod_b.metadata.name,
                    "device": client_iface["rdma_device"],
                    "error": client_out,
                })

        finally:
            nic_util.cleanup_ib_processes(pod_a.metadata.name, pod_a.metadata.namespace)
            nic_util.cleanup_ib_processes(pod_b.metadata.name, pod_b.metadata.namespace)

    if failures:
        pytest.fail(f"IB traffic failures on {len(failures)} interface(s): {failures}")

    LOG.info("IB traffic across %d interface pair(s) completed successfully", num_pairs)


@pytest.mark.timeout(TEST_TIMEOUT)
def test_ib_traffic_and_pull_metrics_pf():
    """Test IB traffic and pull metrics for PF (Physical Function) workloads only"""
    v1 = k8s_client.CoreV1Api()

    nc_namespace = "kube-amd-network"
    try:
        nc_items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    nodeport_by_config: Dict[str, int] = {}
    for it in nc_items:
        name = it.get("metadata", {}).get("name")
        if not name:
            continue
        node_port = it.get("spec", {}).get("metricsExporter", {}).get("nodePort")
        if node_port is None:
            pytest.fail(f"{name} missing spec.metricsExporter.nodePort")
        nodeport_by_config[name] = int(node_port)

    non_vf_configs = [n for n in nodeport_by_config if not n.startswith("vf-")]
    if not non_vf_configs:
        pytest.skip("No PF NetworkConfig found")

    all_pods = list_pods(v1, "default")
    wpods = [p for p in all_pods if p.status.phase == "Running" and not p.metadata.name.startswith("vf-workload")]
    if not wpods:
        pytest.skip("No running PF workload pods in default")

    cfg = non_vf_configs[0]
    port = nodeport_by_config[cfg]

    if not nic_util.wait_for_exporter_pod_running(nc_namespace, cfg):
        pytest.fail(f"Metrics exporter pod for {cfg} not Running")

    pull_inputs = []
    for p in wpods:
        pod_name = p.metadata.name
        node_name = getattr(p.spec, "node_name", None) or getattr(p.spec, "nodeName", None)
        node_ip = nic_util.get_node_ip(node_name)
        pull_inputs.append((p, node_ip, port))

    metrics_by_pod = {}
    metrics_missing = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(pull_inputs)))) as ex:
        futures = {ex.submit(nic_util.pull_metrics, p.metadata.name, p.metadata.namespace, port, node_ip): (p, node_ip, port)
                   for (p, node_ip, port) in pull_inputs}
        for fut in as_completed(futures):
            p, node_ip, port = futures[fut]
            pod_name = p.metadata.name
            try:
                txt = fut.result()
            except Exception as e:
                LOG.error("nic_util.pull_metrics failed for %s: %s", pod_name, e)
                txt = None
            if not txt or not txt.strip():
                metrics_missing.append((pod_name, f"node_ip={node_ip} port={port}"))
                continue
            found = False
            for ln in txt.splitlines():
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                if PROM_LINE_RE.match(ln):
                    found = True
                    break
            if not found:
                metrics_missing.append((pod_name, f"no numeric lines from {node_ip}:{port}"))
            else:
                metrics_by_pod[pod_name] = txt

    if metrics_missing:
        LOG.error("PF Metrics missing/invalid: %s", metrics_missing)
        pytest.fail(f"PF Metrics missing/invalid: {metrics_missing}")

    LOG.info("PF Metrics validated for pods: %s", list(metrics_by_pod.keys()))


@pytest.mark.timeout(TEST_TIMEOUT)
def test_ib_traffic_and_pull_metrics_vf():
    """Test IB traffic and pull metrics for VF (Virtual Function) workloads only"""
    v1 = k8s_client.CoreV1Api()

    nc_namespace = "kube-amd-network"
    try:
        nc_items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    nodeport_by_config: Dict[str, int] = {}
    for it in nc_items:
        name = it.get("metadata", {}).get("name")
        if not name:
            continue
        node_port = it.get("spec", {}).get("metricsExporter", {}).get("nodePort")
        if node_port is None:
            pytest.fail(f"{name} missing spec.metricsExporter.nodePort")
        nodeport_by_config[name] = int(node_port)

    vf_configs = [n for n in nodeport_by_config if n.startswith("vf-")]
    if not vf_configs:
        pytest.skip("No VF NetworkConfig found")

    all_pods = list_pods(v1, "default")
    wpods = [p for p in all_pods if p.status.phase == "Running" and p.metadata.name.startswith("vf-workload")]
    if not wpods:
        pytest.skip("No running VF workload pods in default")

    cfg = vf_configs[0]
    port = nodeport_by_config[cfg]

    if not nic_util.wait_for_exporter_pod_running(nc_namespace, cfg):
        pytest.fail(f"Metrics exporter pod for {cfg} not Running")

    pull_inputs = []
    for p in wpods:
        pod_name = p.metadata.name
        node_name = getattr(p.spec, "node_name", None) or getattr(p.spec, "nodeName", None)
        node_ip = nic_util.get_node_ip(node_name)
        pull_inputs.append((p, node_ip, port))

    metrics_by_pod = {}
    metrics_missing = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(pull_inputs)))) as ex:
        futures = {ex.submit(nic_util.pull_metrics, p.metadata.name, p.metadata.namespace, port, node_ip): (p, node_ip, port)
                   for (p, node_ip, port) in pull_inputs}
        for fut in as_completed(futures):
            p, node_ip, port = futures[fut]
            pod_name = p.metadata.name
            try:
                txt = fut.result()
            except Exception as e:
                LOG.error("nic_util.pull_metrics failed for %s: %s", pod_name, e)
                txt = None
            if not txt or not txt.strip():
                metrics_missing.append((pod_name, f"node_ip={node_ip} port={port}"))
                continue
            found = False
            for ln in txt.splitlines():
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                if PROM_LINE_RE.match(ln):
                    found = True
                    break
            if not found:
                metrics_missing.append((pod_name, f"no numeric lines from {node_ip}:{port}"))
            else:
                metrics_by_pod[pod_name] = txt

    if metrics_missing:
        LOG.error("VF Metrics missing/invalid: %s", metrics_missing)
        pytest.fail(f"VF Metrics missing/invalid: {metrics_missing}")

    LOG.info("VF Metrics validated for pods: %s", list(metrics_by_pod.keys()))


@pytest.mark.timeout(TEST_TIMEOUT)
def test_update_nodeport_and_verify_metrics_pull_pf():
    """Test updating nodePort and verifying metrics pull for PF workloads only"""
    v1 = k8s_client.CoreV1Api()
    nc_namespace = "kube-amd-network"

    try:
        items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    if not items:
        pytest.skip("No NetworkConfig objects found")

    # Filter for PF configs only
    originals = {}
    modified_vals = {}
    for it in items:
        name = it.get("metadata", {}).get("name")
        if not name or name.startswith("vf-"):
            continue
        originals[name] = yaml.safe_load(yaml.safe_dump(it))
        modified_vals[name] = 32521

    if not originals:
        pytest.skip("No PF NetworkConfig found")

    applied = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(modified_vals)))) as ex:
        futures = {ex.submit(nic_util.patch_networkconfig_custom, nc_namespace, name, {"spec": {"metricsExporter": {"nodePort": port}}}): name
                   for name, port in modified_vals.items()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                fut.result()
                applied.append(name)
                LOG.info("Patched PF %s -> nodePort=%d", name, modified_vals[name])
            except Exception as e:
                LOG.error("Failed to patch %s: %s", name, e)
                for rn in applied:
                    try:
                        orig_port = originals[rn]["spec"]["metricsExporter"].get("nodePort")
                        nic_util.patch_networkconfig_custom(nc_namespace, rn, {"spec": {"metricsExporter": {"nodePort": orig_port}}})
                    except Exception as re:
                        LOG.error("Rollback failed for %s: %s", rn, re)
                pytest.fail(f"Patch apply failed: {e}")

    all_pods = list_pods(v1, "default")
    wpods = [p for p in all_pods if p.status.phase == "Running" and not p.metadata.name.startswith("vf-workload")]
    if not wpods:
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(nc_namespace, rn, orig)
            except Exception:
                LOG.error("Failed restore during skip")
        pytest.skip("No running PF workload pods")

    # Use first pod as representative
    rep_pod = wpods[0]
    cfg = list(modified_vals.keys())[0]
    port = modified_vals[cfg]

    if not nic_util.wait_for_exporter_pod_running(nc_namespace, cfg):
        pytest.fail(f"Metrics exporter pod for {cfg} not Running")

    node_name = getattr(rep_pod.spec, "node_name", None) or getattr(rep_pod.spec, "nodeName", None)
    node_ip = nic_util.get_node_ip(node_name)
    ok = nic_util.wait_for_metrics_ready(rep_pod.metadata.name, rep_pod.metadata.namespace, node_ip, port, timeout=20, interval=1.0)
    if not ok:
        LOG.warning("Metrics not ready for PF config %s on %s:%d", cfg, node_ip, port)

    pull_inputs = []
    for p in wpods:
        node_name = getattr(p.spec, "node_name", None) or getattr(p.spec, "nodeName", None)
        node_ip = nic_util.get_node_ip(node_name)
        pull_inputs.append((p, node_ip, port))

    metrics_missing = []
    metrics_found = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(pull_inputs)))) as ex:
        futures = {ex.submit(nic_util.pull_metrics, p.metadata.name, p.metadata.namespace, port, node_ip): (p, node_ip, port)
                   for (p, node_ip, port) in pull_inputs}
        for fut in as_completed(futures):
            p, node_ip, port = futures[fut]
            pod_name = p.metadata.name
            txt = fut.result()
            if not txt or not txt.strip():
                metrics_missing.append((pod_name, f"node_ip={node_ip} port={port}"))
                continue
            found = False
            for ln in txt.splitlines():
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                if PROM_LINE_RE.match(ln):
                    found = True
                    break
            if not found:
                metrics_missing.append((pod_name, f"no numeric lines from {node_ip}:{port}"))
            else:
                metrics_found[pod_name] = txt

    revert_errors = []
    for rn, orig in originals.items():
        try:
            nic_util.replace_with_retry(nc_namespace, rn, orig)
        except Exception as e:
            LOG.error("Failed to revert %s: %s", rn, e)
            revert_errors.append((rn, str(e)))

    if revert_errors:
        LOG.error("Revert errors: %s", revert_errors)

    if metrics_missing:
        LOG.error("PF Metrics missing after nodePort change: %s", metrics_missing)
        pytest.fail(f"PF Metrics missing after nodePort change: {metrics_missing}")

    LOG.info("PF Metrics OK for updated nodePorts on pods: %s", list(metrics_found.keys()))


@pytest.mark.timeout(TEST_TIMEOUT)
def test_update_nodeport_and_verify_metrics_pull_vf():
    """Test updating nodePort and verifying metrics pull for VF workloads only"""
    v1 = k8s_client.CoreV1Api()
    nc_namespace = "kube-amd-network"

    try:
        items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    if not items:
        pytest.skip("No NetworkConfig objects found")

    # Filter for VF configs only
    originals = {}
    modified_vals = {}
    for it in items:
        name = it.get("metadata", {}).get("name")
        if not name or not name.startswith("vf-"):
            continue
        originals[name] = yaml.safe_load(yaml.safe_dump(it))
        modified_vals[name] = 32520

    if not originals:
        pytest.skip("No VF NetworkConfig found")

    applied = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(modified_vals)))) as ex:
        futures = {ex.submit(nic_util.patch_networkconfig_custom, nc_namespace, name, {"spec": {"metricsExporter": {"nodePort": port}}}): name
                   for name, port in modified_vals.items()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                fut.result()
                applied.append(name)
                LOG.info("Patched VF %s -> nodePort=%d", name, modified_vals[name])
            except Exception as e:
                LOG.error("Failed to patch %s: %s", name, e)
                for rn in applied:
                    try:
                        orig_port = originals[rn]["spec"]["metricsExporter"].get("nodePort")
                        nic_util.patch_networkconfig_custom(nc_namespace, rn, {"spec": {"metricsExporter": {"nodePort": orig_port}}})
                    except Exception as re:
                        LOG.error("Rollback failed for %s: %s", rn, re)
                pytest.fail(f"Patch apply failed: {e}")

    all_pods = list_pods(v1, "default")
    wpods = [p for p in all_pods if p.status.phase == "Running" and p.metadata.name.startswith("vf-workload")]
    if not wpods:
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(nc_namespace, rn, orig)
            except Exception:
                LOG.error("Failed restore during skip")
        pytest.skip("No running VF workload pods")

    # Use first pod as representative
    rep_pod = wpods[0]
    cfg = list(modified_vals.keys())[0]
    port = modified_vals[cfg]

    if not nic_util.wait_for_exporter_pod_running(nc_namespace, cfg):
        pytest.fail(f"Metrics exporter pod for {cfg} not Running")

    node_name = getattr(rep_pod.spec, "node_name", None) or getattr(rep_pod.spec, "nodeName", None)
    node_ip = nic_util.get_node_ip(node_name)
    ok = nic_util.wait_for_metrics_ready(rep_pod.metadata.name, rep_pod.metadata.namespace, node_ip, port, timeout=20, interval=1.0)
    if not ok:
        LOG.warning("Metrics not ready for VF config %s on %s:%d", cfg, node_ip, port)

    pull_inputs = []
    for p in wpods:
        node_name = getattr(p.spec, "node_name", None) or getattr(p.spec, "nodeName", None)
        node_ip = nic_util.get_node_ip(node_name)
        pull_inputs.append((p, node_ip, port))

    metrics_missing = []
    metrics_found = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(pull_inputs)))) as ex:
        futures = {ex.submit(nic_util.pull_metrics, p.metadata.name, p.metadata.namespace, port, node_ip): (p, node_ip, port)
                   for (p, node_ip, port) in pull_inputs}
        for fut in as_completed(futures):
            p, node_ip, port = futures[fut]
            pod_name = p.metadata.name
            txt = fut.result()
            if not txt or not txt.strip():
                metrics_missing.append((pod_name, f"node_ip={node_ip} port={port}"))
                continue
            found = False
            for ln in txt.splitlines():
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                if PROM_LINE_RE.match(ln):
                    found = True
                    break
            if not found:
                metrics_missing.append((pod_name, f"no numeric lines from {node_ip}:{port}"))
            else:
                metrics_found[pod_name] = txt

    revert_errors = []
    for rn, orig in originals.items():
        try:
            nic_util.replace_with_retry(nc_namespace, rn, orig)
        except Exception as e:
            LOG.error("Failed to revert %s: %s", rn, e)
            revert_errors.append((rn, str(e)))

    if revert_errors:
        LOG.error("Revert errors: %s", revert_errors)

    if metrics_missing:
        LOG.error("VF Metrics missing after nodePort change: %s", metrics_missing)
        pytest.fail(f"VF Metrics missing after nodePort change: {metrics_missing}")

    LOG.info("VF Metrics OK for updated nodePorts on pods: %s", list(metrics_found.keys()))


@pytest.mark.timeout(TEST_TIMEOUT)
def test_disable_metrics_exporter_and_verify_no_metrics_pf():
    """Test disabling metrics exporter for PF workloads only"""
    v1 = k8s_client.CoreV1Api()
    nc_namespace = "kube-amd-network"

    try:
        items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    if not items:
        pytest.skip("No NetworkConfig objects found")

    # Filter for PF configs only
    originals = {}
    nodeport_by_config = {}
    for it in items:
        name = it.get("metadata", {}).get("name")
        if not name or name.startswith("vf-"):
            continue
        originals[name] = yaml.safe_load(yaml.safe_dump(it))
        node_port = it.get("spec", {}).get("metricsExporter", {}).get("nodePort")
        if node_port is None:
            pytest.fail(f"{name} missing spec.metricsExporter.nodePort")
        nodeport_by_config[name] = int(node_port)

    if not originals:
        pytest.skip("No PF NetworkConfig found")

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(originals)))) as ex:
        futures = {ex.submit(nic_util.patch_networkconfig_custom, nc_namespace, name, {"spec": {"metricsExporter": {"enable": False}}}): name
                   for name in originals.keys()}
        failed = []
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                fut.result()
                LOG.info("PF Disabled metrics for %s", name)
            except Exception as e:
                LOG.error("Failed to patch disable for PF %s: %s", name, e)
                failed.append(name)
        if failed:
            for rn in originals.keys():
                try:
                    nic_util.replace_with_retry(nc_namespace, rn, originals[rn])
                except Exception:
                    LOG.error("Rollback failed during disable error path")
            pytest.fail(f"Failed to disable some PF NetworkConfigs: {failed}")

    time.sleep(2)

    all_pods = list_pods(v1, "default")
    wpods = [p for p in all_pods if p.status.phase == "Running" and not p.metadata.name.startswith("vf-workload")]
    if not wpods:
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(nc_namespace, rn, orig)
            except Exception:
                LOG.error("Restore failed during skip")
        pytest.skip("No running PF workload pods")

    cfg = list(nodeport_by_config.keys())[0]
    port = nodeport_by_config[cfg]
    pull_inputs = []
    for p in wpods:
        pod_name = p.metadata.name
        node_name = getattr(p.spec, "node_name", None) or getattr(p.spec, "nodeName", None)
        node_ip = nic_util.get_node_ip(node_name)
        pull_inputs.append((p, node_ip, port))

    metrics_found = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(pull_inputs)))) as ex:
        futures = {ex.submit(nic_util.pull_metrics, p.metadata.name, p.metadata.namespace, port, node_ip): (p, node_ip, port)
                   for (p, node_ip, port) in pull_inputs}
        for fut in as_completed(futures):
            p, node_ip, port = futures[fut]
            pod_name = p.metadata.name
            txt = fut.result()
            has_numeric = False
            if txt and txt.strip():
                for ln in txt.splitlines():
                    ln = ln.strip()
                    if not ln or ln.startswith("#"):
                        continue
                    if PROM_LINE_RE.match(ln):
                        has_numeric = True
                        break
            if has_numeric:
                metrics_found[pod_name] = {"node_ip": node_ip, "port": port}

    for rn, orig in originals.items():
        try:
            nic_util.replace_with_retry(nc_namespace, rn, orig)
        except Exception as e:
            LOG.error("Failed to restore PF %s: %s", rn, e)

    if metrics_found:
        LOG.error("PF Expected no metrics, but found metrics for pods: %s", metrics_found)
        pytest.fail(f"PF Metrics were returned despite disabling exporter: {list(metrics_found.keys())}")

    LOG.info("PF Negative test passed: no numeric metrics found after disabling exporters.")


@pytest.mark.timeout(TEST_TIMEOUT)
def test_disable_metrics_exporter_and_verify_no_metrics_vf():
    """Test disabling metrics exporter for VF workloads only"""
    v1 = k8s_client.CoreV1Api()
    nc_namespace = "kube-amd-network"

    try:
        items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    if not items:
        pytest.skip("No NetworkConfig objects found")

    # Filter for VF configs only
    originals = {}
    nodeport_by_config = {}
    for it in items:
        name = it.get("metadata", {}).get("name")
        if not name or not name.startswith("vf-"):
            continue
        originals[name] = yaml.safe_load(yaml.safe_dump(it))
        node_port = it.get("spec", {}).get("metricsExporter", {}).get("nodePort")
        if node_port is None:
            pytest.fail(f"{name} missing spec.metricsExporter.nodePort")
        nodeport_by_config[name] = int(node_port)

    if not originals:
        pytest.skip("No VF NetworkConfig found")

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(originals)))) as ex:
        futures = {ex.submit(nic_util.patch_networkconfig_custom, nc_namespace, name, {"spec": {"metricsExporter": {"enable": False}}}): name
                   for name in originals.keys()}
        failed = []
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                fut.result()
                LOG.info("VF Disabled metrics for %s", name)
            except Exception as e:
                LOG.error("Failed to patch disable for VF %s: %s", name, e)
                failed.append(name)
        if failed:
            for rn in originals.keys():
                try:
                    nic_util.replace_with_retry(nc_namespace, rn, originals[rn])
                except Exception:
                    LOG.error("Rollback failed during disable error path")
            pytest.fail(f"Failed to disable some VF NetworkConfigs: {failed}")

    time.sleep(2)

    all_pods = list_pods(v1, "default")
    wpods = [p for p in all_pods if p.status.phase == "Running" and p.metadata.name.startswith("vf-workload")]
    if not wpods:
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(nc_namespace, rn, orig)
            except Exception:
                LOG.error("Restore failed during skip")
        pytest.skip("No running VF workload pods")

    cfg = list(nodeport_by_config.keys())[0]
    port = nodeport_by_config[cfg]
    pull_inputs = []
    for p in wpods:
        pod_name = p.metadata.name
        node_name = getattr(p.spec, "node_name", None) or getattr(p.spec, "nodeName", None)
        node_ip = nic_util.get_node_ip(node_name)
        pull_inputs.append((p, node_ip, port))

    metrics_found = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(pull_inputs)))) as ex:
        futures = {ex.submit(nic_util.pull_metrics, p.metadata.name, p.metadata.namespace, port, node_ip): (p, node_ip, port)
                   for (p, node_ip, port) in pull_inputs}
        for fut in as_completed(futures):
            p, node_ip, port = futures[fut]
            pod_name = p.metadata.name
            txt = fut.result()
            has_numeric = False
            if txt and txt.strip():
                for ln in txt.splitlines():
                    ln = ln.strip()
                    if not ln or ln.startswith("#"):
                        continue
                    if PROM_LINE_RE.match(ln):
                        has_numeric = True
                        break
            if has_numeric:
                metrics_found[pod_name] = {"node_ip": node_ip, "port": port}

    for rn, orig in originals.items():
        try:
            nic_util.replace_with_retry(nc_namespace, rn, orig)
        except Exception as e:
            LOG.error("Failed to restore VF %s: %s", rn, e)

    if metrics_found:
        LOG.error("VF Expected no metrics, but found metrics for pods: %s", metrics_found)
        pytest.fail(f"VF Metrics were returned despite disabling exporter: {list(metrics_found.keys())}")

    LOG.info("VF Negative test passed: no numeric metrics found after disabling exporters.")


@pytest.mark.timeout(TEST_TIMEOUT)
def test_out_of_range_nodeport_pf():
    """Test out-of-range nodePort rejection for PF NetworkConfigs only"""
    nc_namespace = "kube-amd-network"
    OUT_OF_RANGE_PORT = 32800  # outside Kubernetes NodePort default 30000-32767

    try:
        items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    if not items:
        pytest.skip("No NetworkConfig objects found in namespace")

    # Filter for PF configs only
    originals: Dict[str, Dict[str, Any]] = {}
    names = []
    for it in items:
        name = it.get("metadata", {}).get("name")
        if not name or name.startswith("vf-"):
            continue
        originals[name] = yaml.safe_load(yaml.safe_dump(it))
        names.append(name)

    if not names:
        pytest.skip("No PF NetworkConfig found")

    succeeded = []
    failed = []

    for name in names:
        patch_body = {"spec": {"metricsExporter": {"nodePort": OUT_OF_RANGE_PORT}}}
        try:
            nic_util.patch_networkconfig_custom(nc_namespace, name, patch_body)
            LOG.warning("PF Patch unexpectedly succeeded for %s with nodePort=%d", name, OUT_OF_RANGE_PORT)
            succeeded.append(name)
        except ApiException as e:
            LOG.info("PF Patch rejected for %s as expected: status=%s reason=%s", name, getattr(e, "status", None), getattr(e, "reason", None))
            failed.append((name, getattr(e, "status", None), getattr(e, "body", None)))
        except Exception as e:
            LOG.info("PF Patch raised exception for %s (treated as rejection): %s", name, e)
            failed.append((name, "exception", str(e)))

    if succeeded:
        restore_errors = []
        for rn in succeeded:
            try:
                nic_util.replace_with_retry(nc_namespace, rn, originals[rn])
            except Exception as e:
                LOG.error("Failed to restore original for PF %s after unexpected patch success: %s", rn, e)
                restore_errors.append((rn, str(e)))
        if restore_errors:
            LOG.error("PF Restore errors after unexpected acceptance: %s", restore_errors)
        pytest.fail(f"API unexpectedly accepted out-of-range nodePort for PF NetworkConfig(s): {succeeded}")

    revert_errors = []
    for rn, orig in originals.items():
        try:
            try:
                current = nic_util.get_networkconfig_custom(nc_namespace, rn)
                o = yaml.safe_load(yaml.safe_dump(orig))
                o.setdefault("metadata", {})["resourceVersion"] = current.get("metadata", {}).get("resourceVersion")
                nic_util.replace_with_retry(nc_namespace, rn, o)
            except Exception:
                nic_util.replace_with_retry(nc_namespace, rn, orig)
        except Exception as e:
            LOG.error("Failed to restore original PF NetworkConfig %s: %s", rn, e)
            revert_errors.append((rn, str(e)))

    if revert_errors:
        LOG.error("One or more PF NetworkConfig originals failed to revert: %s", revert_errors)
        pytest.fail(f"Failed to revert original PF NetworkConfig(s): {revert_errors}")

    LOG.info("PF Setting out-of-range nodePort was correctly rejected for all NetworkConfig objects.")


@pytest.mark.timeout(TEST_TIMEOUT)
def test_out_of_range_nodeport_vf():
    """Test out-of-range nodePort rejection for VF NetworkConfigs only"""
    nc_namespace = "kube-amd-network"
    OUT_OF_RANGE_PORT = 32800  # outside Kubernetes NodePort default 30000-32767

    try:
        items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    if not items:
        pytest.skip("No NetworkConfig objects found in namespace")

    # Filter for VF configs only
    originals: Dict[str, Dict[str, Any]] = {}
    names = []
    for it in items:
        name = it.get("metadata", {}).get("name")
        if not name or not name.startswith("vf-"):
            continue
        originals[name] = yaml.safe_load(yaml.safe_dump(it))
        names.append(name)

    if not names:
        pytest.skip("No VF NetworkConfig found")

    succeeded = []
    failed = []

    for name in names:
        patch_body = {"spec": {"metricsExporter": {"nodePort": OUT_OF_RANGE_PORT}}}
        try:
            nic_util.patch_networkconfig_custom(nc_namespace, name, patch_body)
            LOG.warning("VF Patch unexpectedly succeeded for %s with nodePort=%d", name, OUT_OF_RANGE_PORT)
            succeeded.append(name)
        except ApiException as e:
            LOG.info("VF Patch rejected for %s as expected: status=%s reason=%s", name, getattr(e, "status", None), getattr(e, "reason", None))
            failed.append((name, getattr(e, "status", None), getattr(e, "body", None)))
        except Exception as e:
            LOG.info("VF Patch raised exception for %s (treated as rejection): %s", name, e)
            failed.append((name, "exception", str(e)))

    if succeeded:
        restore_errors = []
        for rn in succeeded:
            try:
                nic_util.replace_with_retry(nc_namespace, rn, originals[rn])
            except Exception as e:
                LOG.error("Failed to restore original for VF %s after unexpected patch success: %s", rn, e)
                restore_errors.append((rn, str(e)))
        if restore_errors:
            LOG.error("VF Restore errors after unexpected acceptance: %s", restore_errors)
        pytest.fail(f"API unexpectedly accepted out-of-range nodePort for VF NetworkConfig(s): {succeeded}")

    revert_errors = []
    for rn, orig in originals.items():
        try:
            try:
                current = nic_util.get_networkconfig_custom(nc_namespace, rn)
                o = yaml.safe_load(yaml.safe_dump(orig))
                o.setdefault("metadata", {})["resourceVersion"] = current.get("metadata", {}).get("resourceVersion")
                nic_util.replace_with_retry(nc_namespace, rn, o)
            except Exception:
                nic_util.replace_with_retry(nc_namespace, rn, orig)
        except Exception as e:
            LOG.error("Failed to restore original VF NetworkConfig %s: %s", rn, e)
            revert_errors.append((rn, str(e)))

    if revert_errors:
        LOG.error("One or more VF NetworkConfig originals failed to revert: %s", revert_errors)
        pytest.fail(f"Failed to revert original VF NetworkConfig(s): {revert_errors}")

    LOG.info("VF Setting out-of-range nodePort was correctly rejected for all NetworkConfig objects.")


@pytest.mark.timeout(TEST_TIMEOUT)
def test_pull_metrics_using_source_port_pf():
    """
    Test metrics exporter using source port for PF workloads only.
    Verifies that:
    1. Metrics on nodePort (e.g., http://node_ip:32501/metrics) should PASS
    2. Metrics on source port (e.g., http://node_ip:5001/metrics) should PASS
    Both endpoints should return valid Prometheus metrics.
    """
    v1 = k8s_client.CoreV1Api()
    nc_namespace = "kube-amd-network"

    try:
        items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    if not items:
        pytest.skip("No NetworkConfig objects found in namespace kube-amd-network")

    # Build map of NetworkConfig -> source port, nodePort, and hostNetwork (PF only)
    config_ports = {}
    for it in items:
        name = it.get("metadata", {}).get("name")
        if not name or name.startswith("vf-"):
            continue
        spec = it.get("spec", {})
        metrics_exp = spec.get("metricsExporter", {})
        source_port = metrics_exp.get("port")  # source port
        node_port = metrics_exp.get("nodePort")  # destination port
        host_network = metrics_exp.get("hostNetwork", False)
        config_ports[name] = {"source_port": source_port, "node_port": node_port, "host_network": host_network}

    if not config_ports:
        pytest.skip("No PF NetworkConfig found")

    cfg = list(config_ports.keys())[0]
    if not nic_util.wait_for_exporter_pod_running(nc_namespace, cfg):
        pytest.fail(f"Metrics exporter pod for {cfg} not Running")

    all_pods = list_workloads(v1, namespace="default")
    wpods = [p for p in all_pods if not p.metadata.name.startswith("vf-workload")]
    if not wpods:
        pytest.skip("No running PF workload pods in default namespace")

    # Test metrics access for each PF workload pod
    metrics_both_ok = {}
    nodeport_failed = []
    source_port_failed = []

    cfg = list(config_ports.keys())[0]
    ports = config_ports.get(cfg, {})
    source_port = ports.get("source_port")
    node_port = ports.get("node_port")
    host_network = ports.get("host_network", False)

    if not source_port or not node_port:
        pytest.skip(f"PF config {cfg} missing source_port or nodePort")

    # Source port is only accessible directly on the node when hostNetwork=true
    test_source_port = bool(host_network)

    for p in wpods:
        pod_name = p.metadata.name
        node_name = getattr(p.spec, "node_name", None) or getattr(p.spec, "nodeName", None)
        if not node_name:
            LOG.warning("PF Pod %s has no nodeName; skipping", pod_name)
            nodeport_failed.append((pod_name, "no-nodeName"))
            continue

        node_ip = nic_util.get_node_ip(node_name)
        if not node_ip:
            LOG.warning("No InternalIP for node %s (PF pod %s)", node_name, pod_name)
            nodeport_failed.append((pod_name, f"no-node-ip node={node_name}"))
            continue

        LOG.info("PF Testing metrics for pod %s -> node %s (%s) source_port=%s node_port=%s (config=%s)", 
                 pod_name, node_name, node_ip, source_port, node_port, cfg)

        # POSITIVE TEST 1: Try to fetch from nodePort - this should PASS
        curl_cmd_nodeport = f"curl -sS --connect-timeout 5 http://{node_ip}:{node_port}/metrics"
        nodeport_ok = False
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                txt_nodeport = nic_util.exec_in_pod_sync(pod_name, p.metadata.namespace, curl_cmd_nodeport, timeout=10)
                if txt_nodeport and txt_nodeport.strip():
                    for ln in txt_nodeport.splitlines():
                        ln = ln.strip()
                        if not ln or ln.startswith("#"):
                            continue
                        if PROM_LINE_RE.match(ln):
                            nodeport_ok = True
                            break
                if nodeport_ok:
                    break
                LOG.debug("PF Attempt %d/%d: Empty or no numeric metrics from nodePort", attempt, max_retries)
            except Exception as e:
                LOG.warning("PF Attempt %d/%d failed for pod %s on nodePort: %s", attempt, max_retries, pod_name, e)
            if attempt < max_retries:
                time.sleep(1)

        if not nodeport_ok:
            LOG.error("PF Metrics not available on nodePort %s:%s for pod %s", node_ip, node_port, pod_name)
            nodeport_failed.append((pod_name, f"no-metrics node_ip={node_ip} node_port={node_port}"))

        # POSITIVE TEST 2: Try to fetch from source port - only when hostNetwork=true
        source_port_ok = False
        if test_source_port:
            curl_cmd_source = f"curl -sS --connect-timeout 5 http://{node_ip}:{source_port}/metrics"

            for attempt in range(1, max_retries + 1):
                try:
                    txt = nic_util.exec_in_pod_sync(pod_name, p.metadata.namespace, curl_cmd_source, timeout=10)
                    if txt and txt.strip():
                        # check for prometheus numeric line
                        for ln in txt.splitlines():
                            ln = ln.strip()
                            if not ln or ln.startswith("#"):
                                continue
                            if PROM_LINE_RE.match(ln):
                                source_port_ok = True
                                break
                    if source_port_ok:
                        break
                    LOG.debug("PF Attempt %d/%d: Empty or no numeric metrics from source port", attempt, max_retries)
                except Exception as e:
                    LOG.warning("PF Attempt %d/%d failed for pod %s on source port: %s", attempt, max_retries, pod_name, e)
                if attempt < max_retries:
                    time.sleep(1)

            if not source_port_ok:
                sample = (txt or "")[:1000]
                LOG.error("PF Metrics not available on source port %s:%s for pod %s", node_ip, source_port, pod_name)
                source_port_failed.append((pod_name, f"no-metrics source_port={source_port} sample={sample}"))
        else:
            source_port_ok = True  # skip source port check when hostNetwork=false
            LOG.info("PF Skipping source port test for pod %s (hostNetwork=false)", pod_name)

        if nodeport_ok and source_port_ok:
            metrics_both_ok[pod_name] = {"node": node_name, "node_ip": node_ip, "source_port": source_port, "node_port": node_port}
            LOG.info("PF Metrics OK for pod %s on nodePort %s%s", pod_name, node_port,
                     f" and source port {source_port}" if test_source_port else " (source port skipped, hostNetwork=false)")

    if nodeport_failed:
        LOG.error("PF Metrics failed on nodePort: %s", nodeport_failed)
        pytest.fail(f"PF Metrics failed on nodePort: {nodeport_failed}")

    if source_port_failed:
        LOG.error("PF Metrics failed on source port: %s", source_port_failed)
        pytest.fail(f"PF Metrics failed on source port: {source_port_failed}")

    LOG.info("PF Successfully validated metrics on both nodePort and source port for pods: %s", list(metrics_both_ok.keys()))


@pytest.mark.timeout(TEST_TIMEOUT)
def test_pull_metrics_using_source_port_vf():
    """
    Test metrics exporter using source port for VF workloads only.
    Verifies that:
    1. Metrics on nodePort (e.g., http://node_ip:32501/metrics) should PASS
    2. Metrics on source port (e.g., http://node_ip:5001/metrics) should PASS
    Both endpoints should return valid Prometheus metrics.
    """
    v1 = k8s_client.CoreV1Api()
    nc_namespace = "kube-amd-network"

    try:
        items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    if not items:
        pytest.skip("No NetworkConfig objects found in namespace kube-amd-network")

    # Build map of NetworkConfig -> source port, nodePort, and hostNetwork (VF only)
    config_ports = {}
    for it in items:
        name = it.get("metadata", {}).get("name")
        if not name or not name.startswith("vf-"):
            continue
        spec = it.get("spec", {})
        metrics_exp = spec.get("metricsExporter", {})
        source_port = metrics_exp.get("port")  # source port
        node_port = metrics_exp.get("nodePort")  # destination port
        host_network = metrics_exp.get("hostNetwork", False)
        config_ports[name] = {"source_port": source_port, "node_port": node_port, "host_network": host_network}

    if not config_ports:
        pytest.skip("No VF NetworkConfig found")

    cfg = list(config_ports.keys())[0]
    if not nic_util.wait_for_exporter_pod_running(nc_namespace, cfg):
        pytest.fail(f"Metrics exporter pod for {cfg} not Running")

    all_pods = list_workloads(v1, namespace="default")
    wpods = [p for p in all_pods if p.metadata.name.startswith("vf-workload")]
    if not wpods:
        pytest.skip("No running VF workload pods in default namespace")

    # Test metrics access for each VF workload pod
    metrics_both_ok = {}
    nodeport_failed = []
    source_port_failed = []
    ports = config_ports.get(cfg, {})
    source_port = ports.get("source_port")
    node_port = ports.get("node_port")
    host_network = ports.get("host_network", False)

    if not source_port or not node_port:
        pytest.skip(f"VF config {cfg} missing source_port or nodePort")

    # Source port is only accessible directly on the node when hostNetwork=true
    test_source_port = bool(host_network)

    for p in wpods:
        pod_name = p.metadata.name
        node_name = getattr(p.spec, "node_name", None) or getattr(p.spec, "nodeName", None)
        if not node_name:
            LOG.warning("VF Pod %s has no nodeName; skipping", pod_name)
            nodeport_failed.append((pod_name, "no-nodeName"))
            continue

        node_ip = nic_util.get_node_ip(node_name)
        if not node_ip:
            LOG.warning("No InternalIP for node %s (VF pod %s)", node_name, pod_name)
            nodeport_failed.append((pod_name, f"no-node-ip node={node_name}"))
            continue

        LOG.info("VF Testing metrics for pod %s -> node %s (%s) source_port=%s node_port=%s hostNetwork=%s (config=%s)",
                 pod_name, node_name, node_ip, source_port, node_port, host_network, cfg)

        # POSITIVE TEST 1: Try to fetch from nodePort - this should PASS
        curl_cmd_nodeport = f"curl -sS --connect-timeout 5 http://{node_ip}:{node_port}/metrics"
        nodeport_ok = False
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                txt_nodeport = nic_util.exec_in_pod_sync(pod_name, p.metadata.namespace, curl_cmd_nodeport, timeout=10)
                if txt_nodeport and txt_nodeport.strip():
                    for ln in txt_nodeport.splitlines():
                        ln = ln.strip()
                        if not ln or ln.startswith("#"):
                            continue
                        if PROM_LINE_RE.match(ln):
                            nodeport_ok = True
                            break
                if nodeport_ok:
                    break
                LOG.debug("VF Attempt %d/%d: Empty or no numeric metrics from nodePort", attempt, max_retries)
            except Exception as e:
                LOG.warning("VF Attempt %d/%d failed for pod %s on nodePort: %s", attempt, max_retries, pod_name, e)
            if attempt < max_retries:
                time.sleep(1)

        if not nodeport_ok:
            LOG.error("VF Metrics not available on nodePort %s:%s for pod %s", node_ip, node_port, pod_name)
            nodeport_failed.append((pod_name, f"no-metrics node_ip={node_ip} node_port={node_port}"))

        # POSITIVE TEST 2: Try to fetch from source port - only when hostNetwork=true
        source_port_ok = False
        if test_source_port:
            curl_cmd_source = f"curl -sS --connect-timeout 5 http://{node_ip}:{source_port}/metrics"

            for attempt in range(1, max_retries + 1):
                try:
                    txt = nic_util.exec_in_pod_sync(pod_name, p.metadata.namespace, curl_cmd_source, timeout=10)
                    if txt and txt.strip():
                        # check for prometheus numeric line
                        for ln in txt.splitlines():
                            ln = ln.strip()
                            if not ln or ln.startswith("#"):
                                continue
                            if PROM_LINE_RE.match(ln):
                                source_port_ok = True
                                break
                    if source_port_ok:
                        break
                    LOG.debug("VF Attempt %d/%d: Empty or no numeric metrics from source port", attempt, max_retries)
                except Exception as e:
                    LOG.warning("VF Attempt %d/%d failed for pod %s on source port: %s", attempt, max_retries, pod_name, e)
                if attempt < max_retries:
                    time.sleep(1)

            if not source_port_ok:
                sample = (txt or "")[:1000]
                LOG.error("VF Metrics not available on source port %s:%s for pod %s", node_ip, source_port, pod_name)
                source_port_failed.append((pod_name, f"no-metrics source_port={source_port} sample={sample}"))
        else:
            source_port_ok = True  # skip source port check when hostNetwork=false
            LOG.info("VF Skipping source port test for pod %s (hostNetwork=false)", pod_name)

        if nodeport_ok and source_port_ok:
            metrics_both_ok[pod_name] = {"node": node_name, "node_ip": node_ip, "source_port": source_port, "node_port": node_port}
            LOG.info("VF Metrics OK for pod %s on nodePort %s%s", pod_name, node_port,
                     f" and source port {source_port}" if test_source_port else " (source port skipped, hostNetwork=false)")

    if nodeport_failed:
        LOG.error("VF Metrics failed on nodePort: %s", nodeport_failed)
        pytest.fail(f"VF Metrics failed on nodePort: {nodeport_failed}")

    if source_port_failed:
        LOG.error("VF Metrics failed on source port: %s", source_port_failed)
        pytest.fail(f"VF Metrics failed on source port: {source_port_failed}")

    LOG.info("VF Successfully validated metrics on both nodePort and source port for pods: %s", list(metrics_both_ok.keys()))


@pytest.mark.timeout(TEST_TIMEOUT)
def test_default_source_port_cluster_ip_pf():
    """
    Test default source port (5001) with serviceType ClusterIP for PF workloads only.
    Verifies that after changing serviceType to ClusterIP:
    1. Old nodePort endpoint (e.g., http://node_ip:32501/metrics) should FAIL
    2. Default source port endpoint (e.g., http://node_ip:5001/metrics) should PASS
    """
    v1 = k8s_client.CoreV1Api()
    nc_namespace = "kube-amd-network"

    try:
        items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    if not items:
        pytest.skip("No NetworkConfig objects found in namespace kube-amd-network")

    # Save originals, old nodePort values, and source ports (PF only)
    originals: Dict[str, Dict[str, Any]] = {}
    old_nodeports: Dict[str, int] = {}
    source_ports: Dict[str, int] = {}
    for it in items:
        name = it.get("metadata", {}).get("name")
        if not name or name.startswith("vf-"):
            continue
        originals[name] = yaml.safe_load(yaml.safe_dump(it))
        raw_np = it.get("spec", {}).get("metricsExporter", {}).get("nodePort")
        if raw_np:
            old_nodeports[name] = int(raw_np)
        raw_sp = it.get("spec", {}).get("metricsExporter", {}).get("port")
        if raw_sp:
            source_ports[name] = int(raw_sp)

    if not originals:
        pytest.skip("No PF NetworkConfig found")

    # Patch to ClusterIP and ensure hostNetwork=true so source port is accessible on the node
    patched = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(originals)))) as ex:
        futures = {
            ex.submit(
                nic_util.patch_networkconfig_custom,
                nc_namespace,
                name,
                {"spec": {"metricsExporter": {"serviceType": "ClusterIP", "hostNetwork": True}}},
            ): name
            for name in originals.keys()
        }
        failed = []
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                fut.result()
                patched.append(name)
                LOG.info("PF Patched %s to metricsExporter.serviceType=ClusterIP", name)
            except Exception as e:
                LOG.error("Failed to patch PF %s: %s", name, e)
                failed.append(name)
        if failed:
            for rn in originals.keys():
                try:
                    nic_util.replace_with_retry(nc_namespace, rn, originals[rn])
                except Exception:
                    LOG.error("Rollback failed during metricsExporter patch error path")
            pytest.fail(f"Failed to set metricsExporter.serviceType=ClusterIP for some PF NetworkConfigs: {failed}")

    # Wait for configuration to propagate
    LOG.info("PF Waiting 10 seconds for ClusterIP service reconfiguration to complete")
    time.sleep(10)

    cfg = list(old_nodeports.keys())[0]
    if not nic_util.wait_for_exporter_pod_running(nc_namespace, cfg):
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(nc_namespace, rn, orig)
            except Exception:
                LOG.error("Failed to restore during exporter check failure")
        pytest.fail(f"Metrics exporter pod for {cfg} not Running")

    all_pods = list_workloads(v1, namespace="default")
    wpods = [p for p in all_pods if not p.metadata.name.startswith("vf-workload")]
    if not wpods:
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(nc_namespace, rn, orig)
            except Exception:
                LOG.error("Failed to restore during skip")
        pytest.skip("No running PF workload pods in default namespace")

    old_nodeport = old_nodeports[cfg]
    source_port = source_ports.get(cfg, 5001)  # default if not configured

    # Test both old nodePort (should fail) and default source port (should pass)
    nodeport_incorrectly_working = []
    source_port_failed = []
    both_tests_passed = {}

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

        LOG.info("PF Testing pod %s -> node %s (%s) old_nodeport=%s source_port=%s (config=%s)", 
                 pod_name, node_name, node_ip, old_nodeport, source_port, cfg)

        # NEGATIVE TEST: Try old nodePort - should FAIL (serviceType is now ClusterIP)
        curl_cmd_old = f"curl -sS --connect-timeout 3 http://{node_ip}:{old_nodeport}/metrics || true"
        old_nodeport_has_metrics = False
        try:
            txt_old = nic_util.exec_in_pod_sync(pod_name, p.metadata.namespace, curl_cmd_old, timeout=5)
            if txt_old and txt_old.strip():
                for ln in txt_old.splitlines():
                    ln = ln.strip()
                    if not ln or ln.startswith("#"):
                        continue
                    if PROM_LINE_RE.match(ln):
                        old_nodeport_has_metrics = True
                        break
        except Exception as e:
            LOG.debug("PF Expected failure fetching from old nodePort %s:%s: %s", node_ip, old_nodeport, e)

        if old_nodeport_has_metrics:
            LOG.error("PF Metrics incorrectly available on old nodePort %s:%s after ClusterIP change", node_ip, old_nodeport)
            nodeport_incorrectly_working.append((pod_name, f"node_ip={node_ip} old_nodeport={old_nodeport}"))

        # POSITIVE TEST: Try default source port - should PASS
        curl_cmd_new = f"curl -sS --connect-timeout 5 http://{node_ip}:{source_port}/metrics"
        source_port_ok = False
        max_retries = 3
        txt_new = None
        
        for attempt in range(1, max_retries + 1):
            try:
                txt_new = nic_util.exec_in_pod_sync(pod_name, p.metadata.namespace, curl_cmd_new, timeout=10)
                if txt_new and txt_new.strip():
                    for ln in txt_new.splitlines():
                        ln = ln.strip()
                        if not ln or ln.startswith("#"):
                            continue
                        if PROM_LINE_RE.match(ln):
                            source_port_ok = True
                            break
                if source_port_ok:
                    break
                LOG.debug("PF Attempt %d/%d: Empty or no numeric metrics from source port %d", attempt, max_retries, source_port)
            except Exception as e:
                LOG.warning("PF Attempt %d/%d failed for pod %s on source port %d: %s", attempt, max_retries, pod_name, source_port, e)
            if attempt < max_retries:
                time.sleep(1)

        if not source_port_ok:
            sample = (txt_new or "")[:1000]
            LOG.error("PF Metrics not available on default source port %s:%d for pod %s", node_ip, source_port, pod_name)
            source_port_failed.append((pod_name, f"node_ip={node_ip} source_port={source_port} sample={sample}"))

        # Both tests should pass: old nodePort blocked AND source port working
        if not old_nodeport_has_metrics and source_port_ok:
            both_tests_passed[pod_name] = {"node": node_name, "node_ip": node_ip, "old_nodeport": old_nodeport, "source_port": source_port}
            LOG.info("PF Correct behavior for pod %s: old nodePort %s blocked, source port %d working", pod_name, old_nodeport, source_port)

    # Revert to originals
    revert_errors = []
    for rn, orig in originals.items():
        try:
            nic_util.replace_with_retry(nc_namespace, rn, orig)
        except Exception as e:
            LOG.error("Failed to restore PF %s: %s", rn, e)
            revert_errors.append((rn, str(e)))

    if revert_errors:
        LOG.error("Failed to revert some PF NetworkConfigs: %s", revert_errors)

    if nodeport_incorrectly_working:
        LOG.error("PF Old nodePort incorrectly accessible after ClusterIP change: %s", nodeport_incorrectly_working)
        pytest.fail(f"PF Old nodePort incorrectly accessible after ClusterIP change: {nodeport_incorrectly_working}")

    if source_port_failed:
        LOG.error("PF Default source port failed: %s", source_port_failed)
        pytest.fail(f"PF Default source port failed: {source_port_failed}")

    LOG.info("PF Successfully verified ClusterIP with default source port (old nodePort blocked, source port working) for pods: %s", list(both_tests_passed.keys()))


@pytest.mark.timeout(TEST_TIMEOUT)
def test_default_source_port_cluster_ip_vf():
    """
    Test default source port (5001) with serviceType ClusterIP for VF workloads only.
    Verifies that after changing serviceType to ClusterIP:
    1. Old nodePort endpoint (e.g., http://node_ip:32501/metrics) should FAIL
    2. Default source port endpoint (e.g., http://node_ip:5001/metrics) should PASS
    """
    v1 = k8s_client.CoreV1Api()
    nc_namespace = "kube-amd-network"

    try:
        items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    if not items:
        pytest.skip("No NetworkConfig objects found in namespace kube-amd-network")

    # Save originals, old nodePort values, and source ports (VF only)
    originals: Dict[str, Dict[str, Any]] = {}
    old_nodeports: Dict[str, int] = {}
    source_ports: Dict[str, int] = {}
    for it in items:
        name = it.get("metadata", {}).get("name")
        if not name or not name.startswith("vf-"):
            continue
        originals[name] = yaml.safe_load(yaml.safe_dump(it))
        raw_np = it.get("spec", {}).get("metricsExporter", {}).get("nodePort")
        if raw_np:
            old_nodeports[name] = int(raw_np)
        raw_sp = it.get("spec", {}).get("metricsExporter", {}).get("port")
        if raw_sp:
            source_ports[name] = int(raw_sp)

    if not originals:
        pytest.skip("No VF NetworkConfig found")

    # Patch to ClusterIP and enable hostNetwork so source port is accessible on the node
    patched = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(originals)))) as ex:
        futures = {
            ex.submit(
                nic_util.patch_networkconfig_custom,
                nc_namespace,
                name,
                {"spec": {"metricsExporter": {"serviceType": "ClusterIP", "hostNetwork": True}}},
            ): name
            for name in originals.keys()
        }
        failed = []
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                fut.result()
                patched.append(name)
                LOG.info("VF Patched %s to metricsExporter.serviceType=ClusterIP, hostNetwork=true", name)
            except Exception as e:
                LOG.error("Failed to patch VF %s: %s", name, e)
                failed.append(name)
        if failed:
            for rn in originals.keys():
                try:
                    nic_util.replace_with_retry(nc_namespace, rn, originals[rn])
                except Exception:
                    LOG.error("Rollback failed during metricsExporter patch error path")
            pytest.fail(f"Failed to set metricsExporter.serviceType=ClusterIP for some VF NetworkConfigs: {failed}")

    # Wait for configuration to propagate
    LOG.info("VF Waiting 10 seconds for ClusterIP service reconfiguration to complete")
    time.sleep(10)

    cfg = list(old_nodeports.keys())[0]
    if not nic_util.wait_for_exporter_pod_running(nc_namespace, cfg):
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(nc_namespace, rn, orig)
            except Exception:
                LOG.error("Failed to restore during exporter check failure")
        pytest.fail(f"Metrics exporter pod for {cfg} not Running")

    all_pods = list_workloads(v1, namespace="default")
    wpods = [p for p in all_pods if p.metadata.name.startswith("vf-workload")]
    if not wpods:
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(nc_namespace, rn, orig)
            except Exception:
                LOG.error("Failed to restore during skip")
        pytest.skip("No running VF workload pods in default namespace")

    old_nodeport = old_nodeports[cfg]
    source_port = source_ports.get(cfg, 5001)  # default if not configured

    # Test both old nodePort (should fail) and default source port (should pass)
    nodeport_incorrectly_working = []
    source_port_failed = []
    both_tests_passed = {}

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

        LOG.info("VF Testing pod %s -> node %s (%s) old_nodeport=%s source_port=%s (config=%s)", 
                 pod_name, node_name, node_ip, old_nodeport, source_port, cfg)

        # NEGATIVE TEST: Try old nodePort - should FAIL (serviceType is now ClusterIP)
        curl_cmd_old = f"curl -sS --connect-timeout 3 http://{node_ip}:{old_nodeport}/metrics || true"
        old_nodeport_has_metrics = False
        try:
            txt_old = nic_util.exec_in_pod_sync(pod_name, p.metadata.namespace, curl_cmd_old, timeout=5)
            if txt_old and txt_old.strip():
                for ln in txt_old.splitlines():
                    ln = ln.strip()
                    if not ln or ln.startswith("#"):
                        continue
                    if PROM_LINE_RE.match(ln):
                        old_nodeport_has_metrics = True
                        break
        except Exception as e:
            LOG.debug("VF Expected failure fetching from old nodePort %s:%s: %s", node_ip, old_nodeport, e)

        if old_nodeport_has_metrics:
            LOG.error("VF Metrics incorrectly available on old nodePort %s:%s after ClusterIP change", node_ip, old_nodeport)
            nodeport_incorrectly_working.append((pod_name, f"node_ip={node_ip} old_nodeport={old_nodeport}"))

        # POSITIVE TEST: Try default source port - should PASS
        curl_cmd_new = f"curl -sS --connect-timeout 5 http://{node_ip}:{source_port}/metrics"
        source_port_ok = False
        max_retries = 3
        txt_new = None
        
        for attempt in range(1, max_retries + 1):
            try:
                txt_new = nic_util.exec_in_pod_sync(pod_name, p.metadata.namespace, curl_cmd_new, timeout=10)
                if txt_new and txt_new.strip():
                    for ln in txt_new.splitlines():
                        ln = ln.strip()
                        if not ln or ln.startswith("#"):
                            continue
                        if PROM_LINE_RE.match(ln):
                            source_port_ok = True
                            break
                if source_port_ok:
                    break
                LOG.debug("VF Attempt %d/%d: Empty or no numeric metrics from source port %d", attempt, max_retries, source_port)
            except Exception as e:
                LOG.warning("VF Attempt %d/%d failed for pod %s on source port %d: %s", attempt, max_retries, pod_name, source_port, e)
            if attempt < max_retries:
                time.sleep(1)

        if not source_port_ok:
            sample = (txt_new or "")[:1000]
            LOG.error("VF Metrics not available on default source port %s:%d for pod %s", node_ip, source_port, pod_name)
            source_port_failed.append((pod_name, f"node_ip={node_ip} source_port={source_port} sample={sample}"))

        # Both tests should pass: old nodePort blocked AND source port working
        if not old_nodeport_has_metrics and source_port_ok:
            both_tests_passed[pod_name] = {"node": node_name, "node_ip": node_ip, "old_nodeport": old_nodeport, "source_port": source_port}
            LOG.info("VF Correct behavior for pod %s: old nodePort %s blocked, source port %d working", pod_name, old_nodeport, source_port)

    # Revert to originals
    revert_errors = []
    for rn, orig in originals.items():
        try:
            nic_util.replace_with_retry(nc_namespace, rn, orig)
        except Exception as e:
            LOG.error("Failed to restore VF %s: %s", rn, e)
            revert_errors.append((rn, str(e)))

    if revert_errors:
        LOG.error("Failed to revert some VF NetworkConfigs: %s", revert_errors)

    if nodeport_incorrectly_working:
        LOG.error("VF Old nodePort incorrectly accessible after ClusterIP change: %s", nodeport_incorrectly_working)
        pytest.fail(f"VF Old nodePort incorrectly accessible after ClusterIP change: {nodeport_incorrectly_working}")

    if source_port_failed:
        LOG.error("VF Default source port failed: %s", source_port_failed)
        pytest.fail(f"VF Default source port failed: {source_port_failed}")

    LOG.info("VF Successfully verified ClusterIP with default source port (old nodePort blocked, source port working) for pods: %s", list(both_tests_passed.keys()))


@pytest.mark.timeout(TEST_TIMEOUT)
def test_custom_source_port_cluster_ip_pf():
    """
    Test custom source port 2001 with serviceType ClusterIP for PF workloads only.
    Verifies that after changing serviceType to ClusterIP:
    1. Old nodePort endpoint (e.g., http://node_ip:32501/metrics) should FAIL
    2. Custom source port endpoint (e.g., http://node_ip:2001/metrics) should PASS
    """
    v1 = k8s_client.CoreV1Api()
    nc_namespace = "kube-amd-network"
    TARGET_PORT = 2001

    try:
        items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    if not items:
        pytest.skip("No NetworkConfig objects found in namespace kube-amd-network")

    # Save originals and old nodePort values (PF only)
    originals: Dict[str, Dict[str, Any]] = {}
    old_nodeports: Dict[str, int] = {}
    for it in items:
        name = it.get("metadata", {}).get("name")
        if not name or name.startswith("vf-"):
            continue
        originals[name] = yaml.safe_load(yaml.safe_dump(it))
        raw_np = it.get("spec", {}).get("metricsExporter", {}).get("nodePort")
        if raw_np:
            old_nodeports[name] = int(raw_np)

    if not originals:
        pytest.skip("No PF NetworkConfig found")

    # Patch to ClusterIP with custom source port 2001 and ensure hostNetwork=true
    patched = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(originals)))) as ex:
        futures = {
            ex.submit(
                nic_util.patch_networkconfig_custom,
                nc_namespace,
                name,
                {"spec": {"metricsExporter": {"serviceType": "ClusterIP", "port": TARGET_PORT, "hostNetwork": True}}},
            ): name
            for name in originals.keys()
        }
        failed = []
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                fut.result()
                patched.append(name)
                LOG.info("PF Patched %s to metricsExporter.serviceType=ClusterIP, port=%d, hostNetwork=true", name, TARGET_PORT)
            except Exception as e:
                LOG.error("Failed to patch PF %s: %s", name, e)
                failed.append(name)
        if failed:
            for rn in originals.keys():
                try:
                    nic_util.replace_with_retry(nc_namespace, rn, originals[rn])
                except Exception:
                    LOG.error("Rollback failed during metricsExporter patch error path")
            pytest.fail(f"Failed to set metricsExporter.serviceType=ClusterIP for some PF NetworkConfigs: {failed}")

    # Wait for configuration to propagate
    LOG.info("PF Waiting 10 seconds for ClusterIP service reconfiguration to complete")
    time.sleep(10)

    cfg = list(old_nodeports.keys())[0]
    if not nic_util.wait_for_exporter_pod_running(nc_namespace, cfg):
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(nc_namespace, rn, orig)
            except Exception:
                LOG.error("Failed to restore during exporter check failure")
        pytest.fail(f"Metrics exporter pod for {cfg} not Running")

    all_pods = list_workloads(v1, namespace="default")
    wpods = [p for p in all_pods if not p.metadata.name.startswith("vf-workload")]
    if not wpods:
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(nc_namespace, rn, orig)
            except Exception:
                LOG.error("Failed to restore during skip")
        pytest.skip("No running PF workload pods in default namespace")

    old_nodeport = old_nodeports[cfg]

    # Test both old nodePort (should fail) and new source port (should pass)
    nodeport_incorrectly_working = []
    source_port_failed = []
    both_tests_passed = {}

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
        
        LOG.info("PF Testing pod %s -> node %s (%s) old_nodeport=%s new_source_port=%s (config=%s)", 
                 pod_name, node_name, node_ip, old_nodeport, TARGET_PORT, cfg)

        # NEGATIVE TEST: Try old nodePort - should FAIL (serviceType is now ClusterIP)
        curl_cmd_old = f"curl -sS --connect-timeout 3 http://{node_ip}:{old_nodeport}/metrics || true"
        old_nodeport_has_metrics = False
        try:
            txt_old = nic_util.exec_in_pod_sync(pod_name, p.metadata.namespace, curl_cmd_old, timeout=5)
            if txt_old and txt_old.strip():
                for ln in txt_old.splitlines():
                    ln = ln.strip()
                    if not ln or ln.startswith("#"):
                        continue
                    if PROM_LINE_RE.match(ln):
                        old_nodeport_has_metrics = True
                        break
        except Exception as e:
            LOG.debug("PF Expected failure fetching from old nodePort %s:%s: %s", node_ip, old_nodeport, e)

        if old_nodeport_has_metrics:
            LOG.error("PF Metrics incorrectly available on old nodePort %s:%s after ClusterIP change", node_ip, old_nodeport)
            nodeport_incorrectly_working.append((pod_name, f"node_ip={node_ip} old_nodeport={old_nodeport}"))

        # POSITIVE TEST: Try custom source port 2001 - should PASS
        curl_cmd_new = f"curl -sS --connect-timeout 5 http://{node_ip}:{TARGET_PORT}/metrics"
        source_port_ok = False
        max_retries = 3
        txt_new = None
        
        for attempt in range(1, max_retries + 1):
            try:
                txt_new = nic_util.exec_in_pod_sync(pod_name, p.metadata.namespace, curl_cmd_new, timeout=10)
                if txt_new and txt_new.strip():
                    for ln in txt_new.splitlines():
                        ln = ln.strip()
                        if not ln or ln.startswith("#"):
                            continue
                        if PROM_LINE_RE.match(ln):
                            source_port_ok = True
                            break
                if source_port_ok:
                    break
                LOG.debug("PF Attempt %d/%d: Empty or no numeric metrics from source port %d", attempt, max_retries, TARGET_PORT)
            except Exception as e:
                LOG.warning("PF Attempt %d/%d failed for pod %s on source port %d: %s", attempt, max_retries, pod_name, TARGET_PORT, e)
            if attempt < max_retries:
                time.sleep(1)

        if not source_port_ok:
            sample = (txt_new or "")[:1000]
            LOG.error("PF Metrics not available on custom source port %s:%d for pod %s", node_ip, TARGET_PORT, pod_name)
            source_port_failed.append((pod_name, f"node_ip={node_ip} source_port={TARGET_PORT} sample={sample}"))

        # Both tests should pass: old nodePort blocked AND new source port working
        if not old_nodeport_has_metrics and source_port_ok:
            both_tests_passed[pod_name] = {"node": node_name, "node_ip": node_ip, "old_nodeport": old_nodeport, "new_source_port": TARGET_PORT}
            LOG.info("PF Correct behavior for pod %s: old nodePort %s blocked, new source port %d working", pod_name, old_nodeport, TARGET_PORT)

    # Revert to originals
    revert_errors = []
    for rn, orig in originals.items():
        try:
            nic_util.replace_with_retry(nc_namespace, rn, orig)
        except Exception as e:
            LOG.error("Failed to restore PF %s: %s", rn, e)
            revert_errors.append((rn, str(e)))

    if revert_errors:
        LOG.error("Failed to revert some PF NetworkConfigs: %s", revert_errors)

    if nodeport_incorrectly_working:
        LOG.error("PF Old nodePort incorrectly accessible after ClusterIP change: %s", nodeport_incorrectly_working)
        pytest.fail(f"PF Old nodePort incorrectly accessible after ClusterIP change: {nodeport_incorrectly_working}")

    if source_port_failed:
        LOG.error("PF Custom source port failed: %s", source_port_failed)
        pytest.fail(f"PF Custom source port failed: {source_port_failed}")

    LOG.info("PF Successfully verified ClusterIP with custom source port 2001 (old nodePort blocked, new source port working) for pods: %s", list(both_tests_passed.keys()))


@pytest.mark.timeout(TEST_TIMEOUT)
def test_custom_source_port_cluster_ip_vf():
    """
    Test custom source port 2001 with serviceType ClusterIP for VF workloads only.
    Verifies that after changing serviceType to ClusterIP:
    1. Old nodePort endpoint (e.g., http://node_ip:32501/metrics) should FAIL
    2. Custom source port endpoint (e.g., http://node_ip:2001/metrics) should PASS
    """
    v1 = k8s_client.CoreV1Api()
    nc_namespace = "kube-amd-network"
    TARGET_PORT = 2001

    try:
        items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    if not items:
        pytest.skip("No NetworkConfig objects found in namespace kube-amd-network")

    # Save originals and old nodePort values (VF only)
    originals: Dict[str, Dict[str, Any]] = {}
    old_nodeports: Dict[str, int] = {}
    for it in items:
        name = it.get("metadata", {}).get("name")
        if not name or not name.startswith("vf-"):
            continue
        originals[name] = yaml.safe_load(yaml.safe_dump(it))
        raw_np = it.get("spec", {}).get("metricsExporter", {}).get("nodePort")
        if raw_np:
            old_nodeports[name] = int(raw_np)

    if not originals:
        pytest.skip("No VF NetworkConfig found")

    # Patch to ClusterIP with custom source port 2001 and enable hostNetwork
    patched = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(originals)))) as ex:
        futures = {
            ex.submit(
                nic_util.patch_networkconfig_custom,
                nc_namespace,
                name,
                {"spec": {"metricsExporter": {"serviceType": "ClusterIP", "port": TARGET_PORT, "hostNetwork": True}}},
            ): name
            for name in originals.keys()
        }
        failed = []
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                fut.result()
                patched.append(name)
                LOG.info("VF Patched %s to metricsExporter.serviceType=ClusterIP, port=%d, hostNetwork=true", name, TARGET_PORT)
            except Exception as e:
                LOG.error("Failed to patch VF %s: %s", name, e)
                failed.append(name)
        if failed:
            for rn in originals.keys():
                try:
                    nic_util.replace_with_retry(nc_namespace, rn, originals[rn])
                except Exception:
                    LOG.error("Rollback failed during metricsExporter patch error path")
            pytest.fail(f"Failed to set metricsExporter.serviceType=ClusterIP for some VF NetworkConfigs: {failed}")

    # Wait for configuration to propagate
    LOG.info("VF Waiting 10 seconds for ClusterIP service reconfiguration to complete")
    time.sleep(10)

    cfg = list(old_nodeports.keys())[0]
    if not nic_util.wait_for_exporter_pod_running(nc_namespace, cfg):
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(nc_namespace, rn, orig)
            except Exception:
                LOG.error("Failed to restore during exporter check failure")
        pytest.fail(f"Metrics exporter pod for {cfg} not Running")

    all_pods = list_workloads(v1, namespace="default")
    wpods = [p for p in all_pods if p.metadata.name.startswith("vf-workload")]
    if not wpods:
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(nc_namespace, rn, orig)
            except Exception:
                LOG.error("Failed to restore during skip")
        pytest.skip("No running VF workload pods in default namespace")

    old_nodeport = old_nodeports[cfg]

    # Test both old nodePort (should fail) and new source port (should pass)
    nodeport_incorrectly_working = []
    source_port_failed = []
    both_tests_passed = {}

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

        LOG.info("VF Testing pod %s -> node %s (%s) old_nodeport=%s new_source_port=%s (config=%s)", 
                 pod_name, node_name, node_ip, old_nodeport, TARGET_PORT, cfg)

        # NEGATIVE TEST: Try old nodePort - should FAIL (serviceType is now ClusterIP)
        curl_cmd_old = f"curl -sS --connect-timeout 3 http://{node_ip}:{old_nodeport}/metrics || true"
        old_nodeport_has_metrics = False
        try:
            txt_old = nic_util.exec_in_pod_sync(pod_name, p.metadata.namespace, curl_cmd_old, timeout=5)
            if txt_old and txt_old.strip():
                for ln in txt_old.splitlines():
                    ln = ln.strip()
                    if not ln or ln.startswith("#"):
                        continue
                    if PROM_LINE_RE.match(ln):
                        old_nodeport_has_metrics = True
                        break
        except Exception as e:
            LOG.debug("VF Expected failure fetching from old nodePort %s:%s: %s", node_ip, old_nodeport, e)

        if old_nodeport_has_metrics:
            LOG.error("VF Metrics incorrectly available on old nodePort %s:%s after ClusterIP change", node_ip, old_nodeport)
            nodeport_incorrectly_working.append((pod_name, f"node_ip={node_ip} old_nodeport={old_nodeport}"))

        # POSITIVE TEST: Try custom source port 2001 - should PASS
        curl_cmd_new = f"curl -sS --connect-timeout 5 http://{node_ip}:{TARGET_PORT}/metrics"
        source_port_ok = False
        max_retries = 3
        txt_new = None
        
        for attempt in range(1, max_retries + 1):
            try:
                txt_new = nic_util.exec_in_pod_sync(pod_name, p.metadata.namespace, curl_cmd_new, timeout=10)
                if txt_new and txt_new.strip():
                    for ln in txt_new.splitlines():
                        ln = ln.strip()
                        if not ln or ln.startswith("#"):
                            continue
                        if PROM_LINE_RE.match(ln):
                            source_port_ok = True
                            break
                if source_port_ok:
                    break
                LOG.debug("VF Attempt %d/%d: Empty or no numeric metrics from source port %d", attempt, max_retries, TARGET_PORT)
            except Exception as e:
                LOG.warning("VF Attempt %d/%d failed for pod %s on source port %d: %s", attempt, max_retries, pod_name, TARGET_PORT, e)
            if attempt < max_retries:
                time.sleep(1)

        if not source_port_ok:
            sample = (txt_new or "")[:1000]
            LOG.error("VF Metrics not available on custom source port %s:%d for pod %s", node_ip, TARGET_PORT, pod_name)
            source_port_failed.append((pod_name, f"node_ip={node_ip} source_port={TARGET_PORT} sample={sample}"))

        # Both tests should pass: old nodePort blocked AND new source port working
        if not old_nodeport_has_metrics and source_port_ok:
            both_tests_passed[pod_name] = {"node": node_name, "node_ip": node_ip, "old_nodeport": old_nodeport, "new_source_port": TARGET_PORT}
            LOG.info("VF Correct behavior for pod %s: old nodePort %s blocked, new source port %d working", pod_name, old_nodeport, TARGET_PORT)

    # Revert to originals
    revert_errors = []
    for rn, orig in originals.items():
        try:
            nic_util.replace_with_retry(nc_namespace, rn, orig)
        except Exception as e:
            LOG.error("Failed to restore VF %s: %s", rn, e)
            revert_errors.append((rn, str(e)))

    if revert_errors:
        LOG.error("Failed to revert some VF NetworkConfigs: %s", revert_errors)

    if nodeport_incorrectly_working:
        LOG.error("VF Old nodePort incorrectly accessible after ClusterIP change: %s", nodeport_incorrectly_working)
        pytest.fail(f"VF Old nodePort incorrectly accessible after ClusterIP change: {nodeport_incorrectly_working}")

    if source_port_failed:
        LOG.error("VF Custom source port failed: %s", source_port_failed)
        pytest.fail(f"VF Custom source port failed: {source_port_failed}")

    LOG.info("VF Successfully verified ClusterIP with custom source port 2001 (old nodePort blocked, new source port working) for pods: %s", list(both_tests_passed.keys()))


@pytest.mark.timeout(TEST_TIMEOUT)
def test_config_map_fields_in_metrics_pf():
    """
    Test that all NIC metric fields from configmap.yaml are present in pulled metrics for PF workloads only.
    """

    v1 = k8s_client.CoreV1Api()
    nc_namespace = "kube-amd-network"

    # Load expected fields from configmap.yaml -> data.config.json -> NICConfig.Fields
    try:
        expected_fields = nic_util.load_nic_fields_from_configmap(CONFIGMAP_TEMPLATE_PATH)
    except Exception as e:
        pytest.skip(f"Failed to load NICConfig.Fields from configmap.yaml: {e}")

    if not expected_fields:
        pytest.skip("No NICConfig.Fields defined in configmap.yaml")

    LOG.info("Loaded %d expected NIC metric fields from configmap.yaml for PF validation", len(expected_fields))

    # Get NetworkConfig resources
    try:
        items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    if not items:
        pytest.skip("No NetworkConfig objects found in namespace kube-amd-network")

    # Build map of NetworkConfig -> nodePort
    config_ports = {}
    for it in items:
        name = it.get("metadata", {}).get("name")
        if not name:
            continue
        node_port = it.get("spec", {}).get("metricsExporter", {}).get("nodePort")
        if node_port:
            config_ports[name] = int(node_port)

    non_vf_configs = [n for n in config_ports.keys() if not n.startswith("vf-")]
    if not non_vf_configs:
        pytest.skip("No PF NetworkConfig found")

    cfg = non_vf_configs[0]
    if not nic_util.wait_for_exporter_pod_running(nc_namespace, cfg):
        pytest.fail(f"Metrics exporter pod for {cfg} not Running")

    all_pods = list_workloads(v1, namespace="default")
    pf_pods = [p for p in all_pods if not p.metadata.name.startswith("vf-workload")]
    if not pf_pods:
        pytest.skip("No running PF workload pods in default namespace")

    LOG.info("Testing %d PF workload pods", len(pf_pods))
    port = config_ports[cfg]
    
    # Use first pod as representative
    p = pf_pods[0]
    pod_name = p.metadata.name
    node_name = getattr(p.spec, "node_name", None) or getattr(p.spec, "nodeName", None)
    
    if not node_name:
        pytest.fail(f"Pod {pod_name} has no nodeName")
    
    node_ip = nic_util.get_node_ip(node_name)
    if not node_ip:
        pytest.fail(f"No InternalIP for node {node_name}")
    
    LOG.info("Checking PF metrics from pod %s -> %s:%s (config=%s)", pod_name, node_ip, port, cfg)
    
    # Fetch metrics
    curl_cmd = f"curl -sS --connect-timeout 10 http://{node_ip}:{port}/metrics"
    max_retries = 3
    metrics_text = None
    
    for attempt in range(1, max_retries + 1):
        try:
            metrics_text = nic_util.exec_in_pod_sync(pod_name, p.metadata.namespace, curl_cmd, timeout=15)
            if metrics_text and metrics_text.strip():
                break
            LOG.debug("Attempt %d/%d: Empty metrics response", attempt, max_retries)
        except Exception as e:
            LOG.warning("Attempt %d/%d failed: %s", attempt, max_retries, e)
        if attempt < max_retries:
            time.sleep(1)
    
    if not metrics_text or not metrics_text.strip():
        pytest.fail(f"Failed to fetch PF metrics after {max_retries} attempts")

    # Extract metric names
    metric_names = set()
    for line in metrics_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "{" in line:
            metric_name = line.split("{")[0].strip()
        elif " " in line:
            metric_name = line.split(" ")[0].strip()
        else:
            continue
        if metric_name:
            metric_names.add(metric_name)

    # QP metrics are only exposed via /metrics?debug=qp, fetch and merge them
    qp_curl_cmd = f"curl -sS --connect-timeout 10 'http://{node_ip}:{port}/metrics?debug=qp'"
    for attempt in range(1, max_retries + 1):
        try:
            qp_text = nic_util.exec_in_pod_sync(pod_name, p.metadata.namespace, qp_curl_cmd, timeout=15)
            if qp_text and qp_text.strip():
                for line in qp_text.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "{" in line:
                        mn = line.split("{")[0].strip()
                    elif " " in line:
                        mn = line.split(" ")[0].strip()
                    else:
                        continue
                    if mn:
                        metric_names.add(mn)
                LOG.info("Merged QP debug metrics from /metrics?debug=qp")
                break
            LOG.debug("Attempt %d/%d: Empty QP debug metrics response", attempt, max_retries)
        except Exception as e:
            LOG.warning("Attempt %d/%d QP debug fetch failed: %s", attempt, max_retries, e)
        if attempt < max_retries:
            time.sleep(1)

    LOG.info("Found %d unique metric names for PF (including QP debug)", len(metric_names))
    sample_names = sorted(metric_names)[:10]
    LOG.info("Sample actual metric names for PF: %s", sample_names)

    # Check for expected fields — compare lowercased field name against actual metric names
    # PRI normalization: configmap may use PRI0 or PRI_0; endpoint always uses pri_0
    # QP normalization: QP_* fields in configmap map to lif_qp_*_total metric names
    #   e.g. QP_SQ_REQ_TX_NUM_PACKET -> lif_qp_sq_req_tx_num_packet_total
    missing_fields = []
    found_fields = []

    for field in expected_fields:
        metric_name = re.sub(r'pri_?(\d)', r'pri_\1', field.lower())
        # QP_ fields (without LIF_ prefix) map to lif_qp_*_total in the exporter
        if metric_name.startswith("qp_"):
            metric_name = "lif_" + metric_name + "_total"
        if metric_name in metric_names:
            found_fields.append(field)
        else:
            missing_fields.append(field)

    LOG.info("PF metrics: %d/%d fields found", len(found_fields), len(expected_fields))
    
    if missing_fields:
        LOG.error("PF missing fields (%d): %s", len(missing_fields), missing_fields[:10])
        pytest.fail(f"PF missing {len(missing_fields)} fields: {missing_fields[:5]}...")
    
    LOG.info("Successfully validated all configmap.yaml NICConfig.Fields present in PF metrics")


@pytest.mark.timeout(TEST_TIMEOUT)
def test_config_map_fields_in_metrics_vf():
    """
    Test that RDMA_ and ETH_ metric fields from configmap.yaml are present in pulled metrics for VF workloads only.
    """

    v1 = k8s_client.CoreV1Api()
    nc_namespace = "kube-amd-network"

    # Load expected fields from configmap.yaml -> data.config.json -> NICConfig.Fields
    try:
        all_fields = nic_util.load_nic_fields_from_configmap(CONFIGMAP_TEMPLATE_PATH)
    except Exception as e:
        pytest.skip(f"Failed to load NICConfig.Fields from configmap.yaml: {e}")

    if not all_fields:
        pytest.skip("No NICConfig.Fields defined in configmap.yaml")

    # Filter for VF: only RDMA_ and ETH_ stats (case-insensitive)
    expected_fields = [f for f in all_fields if f.lower().startswith("rdma_") or f.lower().startswith("eth_")]
    LOG.info("Loaded %d VF metric fields (RDMA_/ETH_ only) from configmap.yaml", len(expected_fields))

    # Get NetworkConfig resources
    try:
        items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    if not items:
        pytest.skip("No NetworkConfig objects found in namespace kube-amd-network")

    # Build map of NetworkConfig -> nodePort
    config_ports = {}
    for it in items:
        name = it.get("metadata", {}).get("name")
        if not name:
            continue
        node_port = it.get("spec", {}).get("metricsExporter", {}).get("nodePort")
        if node_port:
            config_ports[name] = int(node_port)

    vf_configs = [n for n in config_ports.keys() if n.startswith("vf-")]
    if not vf_configs:
        pytest.skip("No VF NetworkConfig found")

    cfg = vf_configs[0]
    if not nic_util.wait_for_exporter_pod_running(nc_namespace, cfg):
        pytest.fail(f"Metrics exporter pod for {cfg} not Running")

    all_pods = list_workloads(v1, namespace="default")
    vf_pods = [p for p in all_pods if p.metadata.name.startswith("vf-workload")]
    if not vf_pods:
        pytest.skip("No running VF workload pods in default namespace")

    LOG.info("Testing %d VF workload pods", len(vf_pods))
    port = config_ports[cfg]
    
    # Use first pod as representative
    p = vf_pods[0]
    pod_name = p.metadata.name
    node_name = getattr(p.spec, "node_name", None) or getattr(p.spec, "nodeName", None)
    
    if not node_name:
        pytest.fail(f"Pod {pod_name} has no nodeName")
    
    node_ip = nic_util.get_node_ip(node_name)
    if not node_ip:
        pytest.fail(f"No InternalIP for node {node_name}")
    
    LOG.info("Checking VF metrics from pod %s -> %s:%s (config=%s)", pod_name, node_ip, port, cfg)
    
    # Fetch metrics
    curl_cmd = f"curl -sS --connect-timeout 10 http://{node_ip}:{port}/metrics"
    max_retries = 3
    metrics_text = None
    
    for attempt in range(1, max_retries + 1):
        try:
            metrics_text = nic_util.exec_in_pod_sync(pod_name, p.metadata.namespace, curl_cmd, timeout=15)
            if metrics_text and metrics_text.strip():
                break
            LOG.debug("Attempt %d/%d: Empty metrics response", attempt, max_retries)
        except Exception as e:
            LOG.warning("Attempt %d/%d failed: %s", attempt, max_retries, e)
        if attempt < max_retries:
            time.sleep(1)
    
    if not metrics_text or not metrics_text.strip():
        pytest.fail(f"Failed to fetch VF metrics after {max_retries} attempts")
    
    # Extract metric names
    metric_names = set()
    for line in metrics_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "{" in line:
            metric_name = line.split("{")[0].strip()
        elif " " in line:
            metric_name = line.split(" ")[0].strip()
        else:
            continue
        if metric_name:
            metric_names.add(metric_name)
    
    LOG.info("Found %d unique metric names for VF", len(metric_names))
    sample_names = sorted(metric_names)[:10]
    LOG.info("Sample actual metric names for VF: %s", sample_names)
    if expected_fields:
        LOG.info(
            "Normalized expected field example: %r -> %r",
            expected_fields[0], expected_fields[0].lower(),
        )

    # Check for expected fields — compare lowercased field name against actual metric names
    # PRI normalization: configmap may use PRI0 or PRI_0; endpoint always uses pri_0
    missing_fields = []
    found_fields = []

    for field in expected_fields:
        metric_name = re.sub(r'pri_?(\d)', r'pri_\1', field.lower())
        if metric_name in metric_names:
            found_fields.append(field)
        else:
            missing_fields.append(field)

    LOG.info("VF metrics: %d/%d fields found (RDMA_/ETH_ only)", len(found_fields), len(expected_fields))
    
    if missing_fields:
        LOG.error("VF missing fields (%d): %s", len(missing_fields), missing_fields[:10])
        pytest.fail(f"VF missing {len(missing_fields)} fields: {missing_fields[:5]}...")
    
    LOG.info("Successfully validated all RDMA_/ETH_ configmap.yaml NICConfig.Fields present in VF metrics")


# ========== Metrics exporter configmap verification helpers ==========

def verify_host_network_false_metrics_access(target_vf: bool):
    """Shared validator for hostNetwork=false behavior for PF/VF NetworkConfigs."""
    v1 = k8s_client.CoreV1Api()
    nc_namespace = "kube-amd-network"
    scope = "VF" if target_vf else "PF"

    def _has_numeric_metric(text: str) -> bool:
        if not text:
            return False
        for ln in text.splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            if PROM_LINE_RE.match(ln):
                return True
        return False

    def _curl_from_pod(pod, target_ip: str, port: int, timeout: int = 8) -> str:
        cmd = f"curl -sS --connect-timeout 4 http://{target_ip}:{port}/metrics || true"
        return nic_util.exec_in_pod_sync(pod.metadata.name, pod.metadata.namespace, cmd, timeout=timeout)

    try:
        items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    if not items:
        pytest.skip("No NetworkConfig objects found")

    originals: Dict[str, Dict[str, Any]] = {}
    targets: Dict[str, int] = {}

    for it in items:
        name = it.get("metadata", {}).get("name")
        if not name:
            continue
        is_vf_cfg = name.startswith("vf-")
        if target_vf and not is_vf_cfg:
            continue
        if not target_vf and is_vf_cfg:
            continue

        me = it.get("spec", {}).get("metricsExporter", {}) or {}
        host_network = me.get("hostNetwork", None)
        port = me.get("port", None)
        if host_network is True and port is not None:
            originals[name] = yaml.safe_load(yaml.safe_dump(it))
            targets[name] = int(port)

    if not targets:
        pytest.skip(f"No {scope} NetworkConfig with metricsExporter.hostNetwork=true and metricsExporter.port found")

    patch_failures = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(targets)))) as ex:
        futures = {
            ex.submit(
                nic_util.patch_networkconfig_custom,
                nc_namespace,
                name,
                {"spec": {"metricsExporter": {"hostNetwork": False}}},
            ): name
            for name in targets.keys()
        }
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                fut.result()
                LOG.info("%s patched %s metricsExporter.hostNetwork -> false", scope, name)
            except Exception as e:
                LOG.error("%s failed patching hostNetwork for %s: %s", scope, name, e)
                patch_failures.append((name, str(e)))

    if patch_failures:
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(nc_namespace, rn, orig)
            except Exception:
                LOG.error("Rollback failed after patch failure for %s", rn)
        pytest.fail(f"{scope} failed to patch hostNetwork=false for: {patch_failures}")

    time.sleep(15)

    if target_vf:
        workloads = [p for p in list_workloads(v1, namespace="default") if p.metadata.name.startswith("vf-")]
    else:
        workloads = [p for p in list_workloads(v1, namespace="default") if not p.metadata.name.startswith("vf-")]

    if not workloads:
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(nc_namespace, rn, orig)
            except Exception:
                LOG.error("Failed restore during skip for %s", rn)
        pytest.skip(f"No running {scope} workload pods in default namespace")

    op_pods = nic_util.get_operator_pods(namespace=nc_namespace)
    exporter_pods = op_pods.get("metrics-exporter", []) + op_pods.get("vf-metrics-exporter", [])

    podip_failures = []
    nodeip_unexpected_success = []

    try:
        for cfg_name, cfg_port in targets.items():
            src_pod = workloads[0] if workloads else None
            if src_pod is None:
                LOG.warning("No matching workload pod available for config %s", cfg_name)
                continue

            mexp_pod = None
            prefix = f"{cfg_name}-metrics-exporter"
            for p in exporter_pods:
                if p.metadata.name.startswith(prefix) and p.status.phase == "Running":
                    mexp_pod = p
                    break

            if mexp_pod is None:
                LOG.warning("No running metrics-exporter pod found for config %s", cfg_name)
                continue

            pod_ip = getattr(mexp_pod.status, "pod_ip", None) or getattr(mexp_pod.status, "podIP", None)
            node_name = getattr(mexp_pod.spec, "node_name", None) or getattr(mexp_pod.spec, "nodeName", None)
            node_ip = nic_util.get_node_ip(node_name) if node_name else None

            if not pod_ip or not node_ip:
                LOG.warning("Missing podIP/nodeIP for exporter pod %s (cfg=%s)", mexp_pod.metadata.name, cfg_name)
                continue

            LOG.info(
                "%s validating hostNetwork=false for %s using src pod %s: podIP=%s:%d should PASS, nodeIP=%s:%d should FAIL",
                scope, cfg_name, src_pod.metadata.name, pod_ip, cfg_port, node_ip, cfg_port,
            )

            podip_ok = False
            for _ in range(3):
                txt = _curl_from_pod(src_pod, pod_ip, cfg_port)
                if _has_numeric_metric(txt):
                    podip_ok = True
                    break
                time.sleep(1)
            if not podip_ok:
                podip_failures.append((cfg_name, f"podIP={pod_ip} port={cfg_port}"))

            node_txt = _curl_from_pod(src_pod, node_ip, cfg_port)
            if _has_numeric_metric(node_txt):
                nodeip_unexpected_success.append((cfg_name, f"nodeIP={node_ip} port={cfg_port}"))

    finally:
        restore_errors = []
        for rn, orig in originals.items():
            try:
                nic_util.replace_with_retry(nc_namespace, rn, orig)
            except Exception as e:
                restore_errors.append((rn, str(e)))
        if restore_errors:
            LOG.error("Restore errors after hostNetwork test: %s", restore_errors)

    if podip_failures:
        pytest.fail(f"{scope} hostNetwork=false: podIP endpoint did not serve metrics for: {podip_failures}")

    if nodeip_unexpected_success:
        pytest.fail(f"{scope} hostNetwork=false: nodeIP endpoint unexpectedly served metrics for: {nodeip_unexpected_success}")

    LOG.info("%s hostNetwork=false behavior validated: podIP endpoint works and nodeIP endpoint fails", scope)


def verify_custom_configmap_metrics_port(target_vf: bool, configmap_name: str, template_path: str):
    """Apply a custom configmap and verify metrics use the port defined in config.json."""
    v1 = k8s_client.CoreV1Api()
    nc_namespace = "kube-amd-network"
    scope = "VF" if target_vf else "PF"

    try:
        items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    if not items:
        pytest.skip("No NetworkConfig objects found")

    try:
        template_doc = nic_util.load_configmap_template(template_path)
    except Exception as e:
        pytest.skip(f"Failed to load configmap template: {e}")

    config_doc = nic_util.build_configmap_for_target(template_doc, configmap_name, nc_namespace)
    configured_port = configmap_metrics_port(config_doc)

    originals: Dict[str, Dict[str, Any]] = {}
    targets: List[str] = []
    needs_host_network_patch: List[str] = []
    for it in items:
        name = it.get("metadata", {}).get("name")
        if not name:
            continue

        is_vf_cfg = name.startswith("vf-")
        if target_vf and not is_vf_cfg:
            continue
        if not target_vf and is_vf_cfg:
            continue

        me = it.get("spec", {}).get("metricsExporter", {}) or {}
        if me.get("enable") is False:
            continue

        originals[name] = yaml.safe_load(yaml.safe_dump(it))
        targets.append(name)
        if me.get("hostNetwork") is False:
            needs_host_network_patch.append(name)

    if not targets:
        pytest.skip(f"No eligible {scope} NetworkConfig objects found")

    if target_vf:
        workloads = [p for p in list_workloads(v1, namespace="default") if p.metadata.name.startswith("vf-")]
    else:
        workloads = [p for p in list_workloads(v1, namespace="default") if not p.metadata.name.startswith("vf-")]

    if not workloads:
        pytest.skip(f"No running {scope} workload pods in default namespace")

    src_pod = workloads[0]
    created_configmap = False
    try:
        nic_util.apply_configmap(config_doc, nc_namespace)
        created_configmap = True

        config_map = v1.read_namespaced_config_map(name=configmap_name, namespace=nc_namespace)
        actual_port = configmap_metrics_port({"data": config_map.data or {}})
        if actual_port != configured_port:
            pytest.fail(
                f"{scope} configmap {configmap_name} metrics port mismatch: expected {configured_port}, got {actual_port}"
            )

        patch_failures = []
        for name in targets:
            try:
                set_exporter_configmap(nc_namespace, name, configmap_name)
                LOG.info("%s patched %s metricsExporter.config.name -> %s", scope, name, configmap_name)
            except Exception as e:
                patch_failures.append((name, str(e)))

        if patch_failures:
            pytest.fail(f"{scope} failed to patch metricsExporter.config.name for: {patch_failures}")

        # Enable hostNetwork for configs that had it disabled so the configured port is accessible on node IP
        for name in needs_host_network_patch:
            try:
                nic_util.patch_networkconfig_custom(nc_namespace, name, {"spec": {"metricsExporter": {"hostNetwork": True}}})
                LOG.info("%s patched %s metricsExporter.hostNetwork -> true for port accessibility", scope, name)
            except Exception as e:
                LOG.warning("%s failed to patch hostNetwork for %s: %s", scope, name, e)

        # Record old exporter pod names so we can detect when the operator restarts them
        old_pod_names: Dict[str, str] = {}
        for cfg_name in targets:
            op_pods = nic_util.get_operator_pods(namespace=nc_namespace)
            exporter_pods = op_pods.get("metrics-exporter", []) + op_pods.get("vf-metrics-exporter", [])
            prefix = f"{cfg_name}-metrics-exporter"
            for pod in exporter_pods:
                if pod.metadata.name.startswith(prefix):
                    old_pod_names[cfg_name] = pod.metadata.name
                    LOG.info("%s old exporter pod for %s: %s", scope, cfg_name, pod.metadata.name)
                    break

        time.sleep(15)

        port_failures = []
        for cfg_name in targets:
            ready = False
            last_node_ip = None
            last_exporter_pod = None
            old_name = old_pod_names.get(cfg_name)
            deadline = time.time() + 180

            while time.time() < deadline:
                op_pods = nic_util.get_operator_pods(namespace=nc_namespace)
                exporter_pods = op_pods.get("metrics-exporter", []) + op_pods.get("vf-metrics-exporter", [])

                exporter_pod = None
                prefix = f"{cfg_name}-metrics-exporter"
                for pod in exporter_pods:
                    if pod.metadata.name.startswith(prefix) and pod.status.phase == "Running":
                        # Skip the old pod that hasn't been restarted yet
                        if old_name and pod.metadata.name == old_name:
                            LOG.debug(
                                "%s skipping old exporter pod %s (waiting for restart)",
                                scope, old_name,
                            )
                            continue
                        exporter_pod = pod
                        break

                if exporter_pod is None:
                    time.sleep(5)
                    continue

                node_name = getattr(exporter_pod.spec, "node_name", None) or getattr(exporter_pod.spec, "nodeName", None)
                node_ip = nic_util.get_node_ip(node_name) if node_name else None
                last_exporter_pod = exporter_pod.metadata.name
                last_node_ip = node_ip

                if node_ip and nic_util.wait_for_metrics_ready(
                    src_pod.metadata.name,
                    src_pod.metadata.namespace,
                    node_ip,
                    configured_port,
                    timeout=10,
                    interval=1.0,
                ):
                    ready = True
                    LOG.info(
                        "%s metrics reachable for %s via %s:%d using %s (new pod %s)",
                        scope,
                        cfg_name,
                        node_ip,
                        configured_port,
                        src_pod.metadata.name,
                        last_exporter_pod,
                    )
                    break

                time.sleep(5)

            if not ready:
                port_failures.append(
                    {
                        "networkconfig": cfg_name,
                        "configmap": configmap_name,
                        "exporter_pod": last_exporter_pod,
                        "node_ip": last_node_ip,
                        "port": configured_port,
                    }
                )

        if port_failures:
            pytest.fail(f"{scope} metrics were not reachable on configured port {configured_port}: {port_failures}")

        # --- Validate that all expected NICConfig.Fields appear in the metrics ---
        try:
            nic_fields = nic_util.load_nic_fields_from_configmap(template_path)
        except Exception as e:
            pytest.skip(f"Failed to load NICConfig.Fields for field validation: {e}")

        if nic_fields:
            # For VF, only validate RDMA_ and ETH_ fields
            if target_vf:
                expected_fields = [f for f in nic_fields if f.lower().startswith("rdma_") or f.lower().startswith("eth_")]
            else:
                expected_fields = nic_fields

            if expected_fields:
                # Use the first target's node_ip that was confirmed reachable
                cfg_name = targets[0]
                # Re-discover node_ip for the first target
                field_node_ip = None
                op_pods = nic_util.get_operator_pods(namespace=nc_namespace)
                exporter_pods = op_pods.get("metrics-exporter", []) + op_pods.get("vf-metrics-exporter", [])
                prefix = f"{cfg_name}-metrics-exporter"
                for pod in exporter_pods:
                    if pod.metadata.name.startswith(prefix) and pod.status.phase == "Running":
                        node_name = getattr(pod.spec, "node_name", None) or getattr(pod.spec, "nodeName", None)
                        field_node_ip = nic_util.get_node_ip(node_name) if node_name else None
                        break

                if field_node_ip:
                    # Fetch /metrics
                    curl_cmd = f"curl -sS --connect-timeout 10 http://{field_node_ip}:{configured_port}/metrics"
                    metrics_text = None
                    for attempt in range(1, 4):
                        try:
                            metrics_text = nic_util.exec_in_pod_sync(
                                src_pod.metadata.name, src_pod.metadata.namespace, curl_cmd, timeout=15
                            )
                            if metrics_text and metrics_text.strip():
                                break
                        except Exception as e:
                            LOG.warning("Attempt %d/3 metrics fetch for field validation failed: %s", attempt, e)
                        if attempt < 3:
                            time.sleep(1)

                    if metrics_text:
                        found_metric_names = set()
                        for line in metrics_text.splitlines():
                            line = line.strip()
                            if not line or line.startswith("#"):
                                continue
                            m = PROM_LINE_RE.match(line)
                            if m:
                                found_metric_names.add(m.group(1))

                        # Compare expected vs found with PRI normalization
                        missing_fields = [
                            field for field in expected_fields
                            if re.sub(r'pri_?(\d)', r'pri_\1', f"amd_{field.lower()}") not in found_metric_names
                        ]

                        LOG.info("%s field validation: %d/%d fields found", scope, len(expected_fields) - len(missing_fields), len(expected_fields))

                        if missing_fields:
                            pytest.fail(
                                f"{scope} custom configmap: {len(missing_fields)} NICConfig.Fields missing from metrics: "
                                f"{missing_fields[:10]}"
                            )
                    else:
                        LOG.warning("%s could not fetch metrics for field validation", scope)
                else:
                    LOG.warning("%s could not determine node_ip for field validation", scope)

    finally:
        restore_errors = []
        for name, orig in originals.items():
            try:
                nic_util.replace_with_retry(nc_namespace, name, orig)
            except Exception as e:
                restore_errors.append((name, str(e)))

        if created_configmap:
            nic_util.delete_configmap_quietly(configmap_name, nc_namespace)

        if restore_errors:
            pytest.fail(f"Failed to restore original {scope} NetworkConfig(s): {restore_errors}")

    LOG.info("%s custom configmap metrics port validated successfully on %d", scope, configured_port)


def verify_custom_configmap_prefix_and_labels(
    target_vf: bool,
    configmap_name: str,
    metrics_prefix: str,
    cluster_name: str,
    template_path: str,
    verify_prefix: bool = True,
    verify_labels: bool = True,
):
    """Apply a custom configmap and verify NIC metric prefix and/or labels are exposed in PF/VF metrics."""
    v1 = k8s_client.CoreV1Api()
    nc_namespace = "kube-amd-network"
    scope = "VF" if target_vf else "PF"

    try:
        items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    if not items:
        pytest.skip("No NetworkConfig objects found")

    try:
        template_doc = nic_util.load_configmap_template(template_path)
        nic_fields = nic_util.load_nic_fields_from_configmap(template_path)
        required_labels = nic_util.load_nic_labels_from_configmap(template_path)
        default_custom_labels = nic_util.load_nic_custom_labels_from_configmap(template_path)
    except Exception as e:
        pytest.skip(f"Failed to load configmap expectations: {e}")

    if not nic_fields:
        pytest.skip("No NICConfig.Fields defined in configmap.yaml")
    if verify_labels and not required_labels:
        pytest.skip("No NICConfig.Labels defined in configmap.yaml")

    config_doc = nic_util.build_configmap_for_target(template_doc, configmap_name, nc_namespace)
    config_doc = nic_util.update_configmap_config_json(
        config_doc,
        nic_custom_labels={**default_custom_labels, "CLUSTER_NAME": cluster_name},
    )
    # The exporter reads the prefix from CommonConfig.MetricsFieldPrefix, but
    # nic_util.update_configmap_config_json writes NICConfig.MetricsPrefix.
    cfg = json.loads(config_doc["data"]["config.json"])
    cfg.setdefault("CommonConfig", {})["MetricsFieldPrefix"] = metrics_prefix
    config_doc["data"]["config.json"] = json.dumps(cfg, indent=2)

    cm_port = configmap_metrics_port(config_doc)
    nic_metric_names = [f"{metrics_prefix}{field.lower()}" for field in nic_fields]

    originals: Dict[str, Dict[str, Any]] = {}
    target_ports: Dict[str, int] = {}
    targets: List[str] = []
    needs_host_network_patch: List[str] = []
    for it in items:
        name = it.get("metadata", {}).get("name")
        if not name:
            continue

        is_vf_cfg = name.startswith("vf-")
        if target_vf and not is_vf_cfg:
            continue
        if not target_vf and is_vf_cfg:
            continue

        me = it.get("spec", {}).get("metricsExporter", {}) or {}
        if me.get("enable") is False:
            continue

        originals[name] = yaml.safe_load(yaml.safe_dump(it))
        if me.get("hostNetwork") is False:
            needs_host_network_patch.append(name)
        target_ports[name] = int(me.get("port") or cm_port)
        targets.append(name)

    if not targets:
        pytest.skip(f"No eligible {scope} NetworkConfig objects found")

    if target_vf:
        workloads = [p for p in list_workloads(v1, namespace="default") if p.metadata.name.startswith("vf-")]
    else:
        workloads = [p for p in list_workloads(v1, namespace="default") if not p.metadata.name.startswith("vf-")]

    if not workloads:
        pytest.skip(f"No running {scope} workload pods in default namespace")

    src_pod = workloads[0]
    created_configmap = False
    try:
        nic_util.apply_configmap(config_doc, nc_namespace)
        created_configmap = True

        config_map = v1.read_namespaced_config_map(name=configmap_name, namespace=nc_namespace)
        applied_cfg = {"data": config_map.data or {}}
        applied_port = configmap_metrics_port(applied_cfg)
        actual_prefix_doc = json.loads((applied_cfg.get("data") or {}).get("config.json", "{}"))
        actual_prefix = ((actual_prefix_doc.get("CommonConfig") or {}).get("MetricsFieldPrefix"))
        actual_cluster_name = (((actual_prefix_doc.get("NICConfig") or {}).get("CustomLabels") or {}).get("CLUSTER_NAME"))

        if applied_port != cm_port:
            pytest.fail(f"{scope} configmap {configmap_name} metrics port mismatch: expected {cm_port}, got {applied_port}")
        if actual_prefix != metrics_prefix:
            pytest.fail(f"{scope} configmap {configmap_name} prefix mismatch: expected {metrics_prefix}, got {actual_prefix}")
        if actual_cluster_name != cluster_name:
            pytest.fail(
                f"{scope} configmap {configmap_name} CLUSTER_NAME mismatch: expected {cluster_name}, got {actual_cluster_name}"
            )

        patch_failures = []
        for name in targets:
            try:
                set_exporter_configmap(nc_namespace, name, configmap_name)
                LOG.info("%s patched %s metricsExporter.config.name -> %s", scope, name, configmap_name)
            except Exception as e:
                patch_failures.append((name, str(e)))

        if patch_failures:
            pytest.fail(f"{scope} failed to patch metricsExporter.config.name for: {patch_failures}")

        # Enable hostNetwork for configs that had it disabled so the configured port is accessible on node IP
        for name in needs_host_network_patch:
            try:
                nic_util.patch_networkconfig_custom(nc_namespace, name, {"spec": {"metricsExporter": {"hostNetwork": True}}})
                LOG.info("%s patched %s metricsExporter.hostNetwork -> true for port accessibility", scope, name)
            except Exception as e:
                LOG.warning("%s failed to patch hostNetwork for %s: %s", scope, name, e)

        time.sleep(15)

        verification_failures = []
        for cfg_name in targets:
            metrics_text = None
            last_node_ip = None
            last_exporter_pod = None
            deadline = time.time() + 90
            target_port = target_ports[cfg_name]

            while time.time() < deadline:
                op_pods = nic_util.get_operator_pods(namespace=nc_namespace)
                exporter_pods = op_pods.get("metrics-exporter", []) + op_pods.get("vf-metrics-exporter", [])

                exporter_pod = None
                prefix = f"{cfg_name}-metrics-exporter"
                for pod in exporter_pods:
                    if pod.metadata.name.startswith(prefix) and pod.status.phase == "Running":
                        exporter_pod = pod
                        break

                if exporter_pod is None:
                    time.sleep(3)
                    continue

                node_name = getattr(exporter_pod.spec, "node_name", None) or getattr(exporter_pod.spec, "nodeName", None)
                node_ip = nic_util.get_node_ip(node_name) if node_name else None
                last_exporter_pod = exporter_pod.metadata.name
                last_node_ip = node_ip
                if not node_ip:
                    time.sleep(3)
                    continue

                if not nic_util.wait_for_metrics_ready(
                    src_pod.metadata.name,
                    src_pod.metadata.namespace,
                    node_ip,
                    target_port,
                    timeout=10,
                    interval=1.0,
                ):
                    time.sleep(3)
                    continue

                metrics_text = nic_util.pull_metrics(
                    src_pod.metadata.name,
                    src_pod.metadata.namespace,
                    target_port,
                    node_ip,
                )
                if metrics_text:
                    break

                time.sleep(3)

            failure_reasons: List[str] = []
            if not metrics_text:
                failure_reasons.append("metrics not reachable")
            else:
                if verify_prefix and not nic_util.metrics_text_has_prefix(metrics_text, metrics_prefix):
                    failure_reasons.append(f"missing metric prefix {metrics_prefix}")

                if verify_labels:
                    required_labels_lower = [label.lower() for label in required_labels]
                    expected_labels_lower = {key.lower(): value for key, value in {"CLUSTER_NAME": cluster_name}.items()}

                    matched_line = nic_util.find_metric_line(
                        metrics_text,
                        metric_names=nic_metric_names,
                        required_labels=required_labels_lower,
                        expected_label_values=expected_labels_lower,
                    )
                    if matched_line is None:
                        failure_reasons.append(
                            f"missing NIC labels {required_labels} or CLUSTER_NAME={cluster_name} on prefixed NIC metrics"
                        )

            if failure_reasons:
                verification_failures.append(
                    {
                        "networkconfig": cfg_name,
                        "configmap": configmap_name,
                        "exporter_pod": last_exporter_pod,
                        "node_ip": last_node_ip,
                        "port": target_port,
                        "reasons": failure_reasons,
                    }
                )

        if verification_failures:
            pytest.fail(f"{scope} metrics prefix/labels validation failed: {verification_failures}")

    finally:
        restore_errors = []
        for name, orig in originals.items():
            try:
                nic_util.replace_with_retry(nc_namespace, name, orig)
            except Exception as e:
                restore_errors.append((name, str(e)))

        if created_configmap:
            nic_util.delete_configmap_quietly(configmap_name, nc_namespace)

        if restore_errors:
            pytest.fail(f"Failed to restore original {scope} NetworkConfig(s): {restore_errors}")

    validated_parts = []
    if verify_prefix:
        validated_parts.append(f"prefix {metrics_prefix}")
    if verify_labels:
        validated_parts.append(f"labels with CLUSTER_NAME {cluster_name}")
    LOG.info("%s custom configmap metrics %s validated successfully", scope, " and ".join(validated_parts))


@pytest.mark.timeout(TEST_TIMEOUT)
def test_host_network_false_metrics_access_pf():
    """PF: set metricsExporter.hostNetwork=false and verify podIP passes while nodeIP fails."""
    verify_host_network_false_metrics_access(target_vf=False)


@pytest.mark.timeout(TEST_TIMEOUT)
def test_host_network_false_metrics_access_vf():
    """VF: set metricsExporter.hostNetwork=false and verify podIP passes while nodeIP fails."""
    verify_host_network_false_metrics_access(target_vf=True)


@pytest.mark.timeout(TEST_TIMEOUT + 60)
def test_custom_configmap_metrics_port_pf():
    """PF: apply a custom configmap and verify metrics are served on the configured port."""
    verify_custom_configmap_metrics_port(target_vf=False, configmap_name=CUSTOM_CONFIGMAP_NAME_PF, template_path=CONFIGMAP_TEMPLATE_PATH)


@pytest.mark.timeout(TEST_TIMEOUT + 60)
def test_custom_configmap_metrics_port_vf():
    """VF: apply a custom configmap and verify metrics are served on the configured port."""
    verify_custom_configmap_metrics_port(target_vf=True, configmap_name=CUSTOM_CONFIGMAP_NAME_VF, template_path=CONFIGMAP_TEMPLATE_PATH)


@pytest.mark.timeout(TEST_TIMEOUT + 60)
def test_custom_configmap_metrics_prefix_pf():
    """PF: verify the default amd_ NIC metric prefix is exposed."""
    verify_custom_configmap_prefix_and_labels(
        target_vf=False,
        configmap_name=CUSTOM_PREFIX_CONFIGMAP_NAME_PF,
        metrics_prefix="amd_",
        cluster_name="amdnetwork-k8s-metrics-exporter",
        template_path=CONFIGMAP_TEMPLATE_PATH,
        verify_prefix=True,
        verify_labels=False,
    )


@pytest.mark.timeout(TEST_TIMEOUT + 60)
def test_custom_configmap_metrics_prefix_vf():
    """VF: verify the default amd_ NIC metric prefix is exposed."""
    verify_custom_configmap_prefix_and_labels(
        target_vf=True,
        configmap_name=CUSTOM_PREFIX_CONFIGMAP_NAME_VF,
        metrics_prefix="amd_",
        cluster_name="amdnetwork-k8s-metrics-exporter",
        template_path=CONFIGMAP_TEMPLATE_PATH,
        verify_prefix=True,
        verify_labels=False,
    )


@pytest.mark.timeout(TEST_TIMEOUT + 60)
def test_custom_configmap_metrics_labels_pf():
    """PF: verify NIC labels and CLUSTER_NAME custom label are exposed on NIC metrics."""
    verify_custom_configmap_prefix_and_labels(
        target_vf=False,
        configmap_name=CUSTOM_LABELS_CONFIGMAP_NAME_PF,
        metrics_prefix="amd_",
        cluster_name="amdnetwork-k8s-metrics-exporter",
        template_path=CONFIGMAP_TEMPLATE_PATH,
        verify_prefix=False,
        verify_labels=True,
    )


@pytest.mark.timeout(TEST_TIMEOUT + 60)
def test_custom_configmap_metrics_labels_vf():
    """VF: verify NIC labels and CLUSTER_NAME custom label are exposed on NIC metrics."""
    verify_custom_configmap_prefix_and_labels(
        target_vf=True,
        configmap_name=CUSTOM_LABELS_CONFIGMAP_NAME_VF,
        metrics_prefix="amd_",
        cluster_name="amdnetwork-k8s-metrics-exporter",
        template_path=CONFIGMAP_TEMPLATE_PATH,
        verify_prefix=False,
        verify_labels=True,
    )


# ========== NICConfig Field Manipulation Tests ==========

@pytest.mark.timeout(TEST_TIMEOUT + 60)
def test_remove_eth_fields_pf():
    """PF: Remove all ETH_ fields from NICConfig and verify ETH metrics are not exported."""
    v1 = k8s_client.CoreV1Api()
    nc_namespace = "kube-amd-network"

    try:
        items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    if not items:
        pytest.skip("No NetworkConfig objects found")

    try:
        template_doc = nic_util.load_configmap_template(CONFIGMAP_TEMPLATE_PATH)
        nic_fields = nic_util.load_nic_fields_from_configmap(CONFIGMAP_TEMPLATE_PATH)
    except Exception as e:
        pytest.skip(f"Failed to load configmap expectations: {e}")

    if not nic_fields:
        pytest.skip("No NICConfig.Fields defined in configmap.yaml")

    # Remove ETH_ fields
    filtered_fields = [f for f in nic_fields if not f.startswith("ETH_")]
    config_doc = nic_util.build_configmap_for_target(template_doc, REMOVE_ETH_FIELDS_CONFIGMAP_NAME_PF, nc_namespace)
    config_doc = nic_util.update_configmap_config_json(
        config_doc,
        nic_fields=filtered_fields,
    )
    configmap_port = nic_util.configmap_metrics_port(config_doc)

    targets = []
    original_config_names: Dict[str, str] = {}
    for it in items:
        name = it.get("metadata", {}).get("name")
        if not name or name.startswith("vf-"):
            continue
        targets.append(name)
        original_config_names[name] = (
            it.get("spec", {}).get("metricsExporter", {}).get("config", {}).get("name")
        )

    if not targets:
        pytest.skip("No eligible PF NetworkConfig objects found")

    workloads = [p for p in list_workloads(v1, namespace="default") if not p.metadata.name.startswith("vf-")]
    if not workloads:
        pytest.skip("No running PF workload pods in default namespace")

    src_pod = workloads[0]
    try:
        nic_util.apply_configmap(config_doc, nc_namespace)

        for name in targets:
            try:
                set_exporter_configmap(nc_namespace, name, REMOVE_ETH_FIELDS_CONFIGMAP_NAME_PF)
            except Exception as e:
                pytest.fail(f"Failed to patch metricsExporter config: {e}")

        time.sleep(15)

        # Try to pull metrics and verify no ETH_ metrics
        metrics_text = None
        deadline = time.time() + 90
        while time.time() < deadline:
            op_pods = nic_util.get_operator_pods(namespace=nc_namespace)
            exporter_pods = op_pods.get("metrics-exporter", [])

            exporter_pod = None
            for pod in exporter_pods:
                if pod.metadata.name.startswith(targets[0]) and pod.status.phase == "Running":
                    exporter_pod = pod
                    break

            if not exporter_pod:
                time.sleep(3)
                continue

            node_name = getattr(exporter_pod.spec, "node_name", None) or getattr(exporter_pod.spec, "nodeName", None)
            node_ip = nic_util.get_node_ip(node_name) if node_name else None
            if not node_ip:
                time.sleep(3)
                continue

            if not nic_util.wait_for_metrics_ready(src_pod.metadata.name, src_pod.metadata.namespace, node_ip, configmap_port, timeout=10):
                time.sleep(3)
                continue

            metrics_text = nic_util.pull_metrics(src_pod.metadata.name, src_pod.metadata.namespace, configmap_port, node_ip)
            if metrics_text:
                break
            time.sleep(3)

        if not metrics_text:
            pytest.skip("Could not pull metrics")

        # Verify ETH_ metrics are NOT present
        eth_metrics = [line for line in metrics_text.splitlines() if "amd_eth_" in line.lower() and not line.startswith("#")]
        if eth_metrics:
            pytest.fail(f"ETH metrics should be removed but found {len(eth_metrics)} metrics: {eth_metrics[:5]}")

    finally:
        for name in targets:
            try:
                if original_config_names[name]:
                    set_exporter_configmap(nc_namespace, name, original_config_names[name])
                else:
                    clear_exporter_configmap(nc_namespace, name)
            except Exception:
                pass
        nic_util.delete_configmap_quietly(REMOVE_ETH_FIELDS_CONFIGMAP_NAME_PF, nc_namespace)


@pytest.mark.timeout(TEST_TIMEOUT + 60)
def test_remove_eth_fields_vf():
    """VF: Remove all ETH_ fields from NICConfig and verify ETH metrics are not exported."""
    v1 = k8s_client.CoreV1Api()
    nc_namespace = "kube-amd-network"

    try:
        items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    if not items:
        pytest.skip("No NetworkConfig objects found")

    try:
        template_doc = nic_util.load_configmap_template(CONFIGMAP_TEMPLATE_PATH)
        nic_fields = nic_util.load_nic_fields_from_configmap(CONFIGMAP_TEMPLATE_PATH)
    except Exception as e:
        pytest.skip(f"Failed to load configmap expectations: {e}")

    if not nic_fields:
        pytest.skip("No NICConfig.Fields defined in configmap.yaml")

    # Remove ETH_ fields
    filtered_fields = [f for f in nic_fields if not f.startswith("ETH_")]
    config_doc = nic_util.build_configmap_for_target(template_doc, REMOVE_ETH_FIELDS_CONFIGMAP_NAME_VF, nc_namespace)
    config_doc = nic_util.update_configmap_config_json(
        config_doc,
        nic_fields=filtered_fields,
    )
    configmap_port = nic_util.configmap_metrics_port(config_doc)

    targets = []
    original_config_names: Dict[str, str] = {}
    for it in items:
        name = it.get("metadata", {}).get("name")
        if not name or not name.startswith("vf-"):
            continue
        targets.append(name)
        original_config_names[name] = (
            it.get("spec", {}).get("metricsExporter", {}).get("config", {}).get("name")
        )

    if not targets:
        pytest.skip("No eligible VF NetworkConfig objects found")

    workloads = [p for p in list_workloads(v1, namespace="default") if p.metadata.name.startswith("vf-")]
    if not workloads:
        pytest.skip("No running VF workload pods in default namespace")

    src_pod = workloads[0]
    try:
        nic_util.apply_configmap(config_doc, nc_namespace)

        for name in targets:
            try:
                set_exporter_configmap(nc_namespace, name, REMOVE_ETH_FIELDS_CONFIGMAP_NAME_VF)
            except Exception as e:
                pytest.fail(f"Failed to patch metricsExporter config: {e}")

        time.sleep(15)

        # Try to pull metrics and verify no ETH_ metrics
        metrics_text = None
        deadline = time.time() + 90
        while time.time() < deadline:
            op_pods = nic_util.get_operator_pods(namespace=nc_namespace)
            exporter_pods = op_pods.get("vf-metrics-exporter", [])

            exporter_pod = None
            for pod in exporter_pods:
                if pod.metadata.name.startswith(targets[0]) and pod.status.phase == "Running":
                    exporter_pod = pod
                    break

            if not exporter_pod:
                time.sleep(3)
                continue

            node_name = getattr(exporter_pod.spec, "node_name", None) or getattr(exporter_pod.spec, "nodeName", None)
            node_ip = nic_util.get_node_ip(node_name) if node_name else None
            if not node_ip:
                time.sleep(3)
                continue

            if not nic_util.wait_for_metrics_ready(src_pod.metadata.name, src_pod.metadata.namespace, node_ip, configmap_port, timeout=10):
                time.sleep(3)
                continue

            metrics_text = nic_util.pull_metrics(src_pod.metadata.name, src_pod.metadata.namespace, configmap_port, node_ip)
            if metrics_text:
                break
            time.sleep(3)

        if not metrics_text:
            pytest.skip("Could not pull metrics")

        # Verify ETH_ metrics are NOT present
        eth_metrics = [line for line in metrics_text.splitlines() if "amd_eth_" in line.lower() and not line.startswith("#")]
        if eth_metrics:
            pytest.fail(f"ETH metrics should be removed but found {len(eth_metrics)} metrics: {eth_metrics[:5]}")

    finally:
        for name in targets:
            try:
                if original_config_names[name]:
                    set_exporter_configmap(nc_namespace, name, original_config_names[name])
                else:
                    clear_exporter_configmap(nc_namespace, name)
            except Exception:
                pass
        nic_util.delete_configmap_quietly(REMOVE_ETH_FIELDS_CONFIGMAP_NAME_VF, nc_namespace)


@pytest.mark.timeout(TEST_TIMEOUT + 60)
def test_exclude_qp_fields_pf():
    """PF: Remove all QP_ fields from NICConfig, verify /metrics has no QP metrics,
    and /metrics?debug=qp also does not expose the removed QP fields."""
    v1 = k8s_client.CoreV1Api()
    nc_namespace = "kube-amd-network"

    try:
        items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    if not items:
        pytest.skip("No NetworkConfig objects found")

    try:
        template_doc = nic_util.load_configmap_template(CONFIGMAP_TEMPLATE_PATH)
        nic_fields = nic_util.load_nic_fields_from_configmap(CONFIGMAP_TEMPLATE_PATH)
    except Exception as e:
        pytest.skip(f"Failed to load configmap expectations: {e}")

    if not nic_fields:
        pytest.skip("No NICConfig.Fields defined in configmap.yaml")

    # Separate QP_ fields from non-QP fields
    qp_fields = [f for f in nic_fields if f.startswith("QP_")]
    filtered_fields = [f for f in nic_fields if not f.startswith("QP_")]

    if not qp_fields:
        pytest.skip("No QP_ fields found in configmap.yaml to exclude")

    LOG.info("Excluding %d QP_ fields, keeping %d non-QP fields", len(qp_fields), len(filtered_fields))

    config_doc = nic_util.build_configmap_for_target(template_doc, EXCLUDE_QP_FIELDS_CONFIGMAP_NAME_PF, nc_namespace)
    config_doc = nic_util.update_configmap_config_json(
        config_doc,
        nic_fields=filtered_fields,
    )
    configmap_port = nic_util.configmap_metrics_port(config_doc)

    targets = []
    original_config_names: Dict[str, str] = {}
    for it in items:
        name = it.get("metadata", {}).get("name")
        if not name or name.startswith("vf-"):
            continue
        targets.append(name)
        original_config_names[name] = (
            it.get("spec", {}).get("metricsExporter", {}).get("config", {}).get("name")
        )

    if not targets:
        pytest.skip("No eligible PF NetworkConfig objects found")

    workloads = [p for p in list_workloads(v1, namespace="default") if not p.metadata.name.startswith("vf-")]
    if not workloads:
        pytest.skip("No running PF workload pods in default namespace")

    src_pod = workloads[0]

    # Record old exporter pod names so we can wait for restart
    old_pod_names: Dict[str, str] = {}
    op_pods = nic_util.get_operator_pods(namespace=nc_namespace)
    for cfg_name in targets:
        for pod in op_pods.get("metrics-exporter", []):
            if pod.metadata.name.startswith(cfg_name):
                old_pod_names[cfg_name] = pod.metadata.name
                break

    try:
        nic_util.apply_configmap(config_doc, nc_namespace)

        for name in targets:
            try:
                set_exporter_configmap(nc_namespace, name, EXCLUDE_QP_FIELDS_CONFIGMAP_NAME_PF)
            except Exception as e:
                pytest.fail(f"Failed to patch metricsExporter config: {e}")

        time.sleep(15)

        # Wait for NEW exporter pod (skip old pod) to be ready and pull /metrics
        metrics_text = None
        node_ip = None
        deadline = time.time() + 180
        while time.time() < deadline:
            op_pods = nic_util.get_operator_pods(namespace=nc_namespace)
            exporter_pods = op_pods.get("metrics-exporter", [])

            exporter_pod = None
            old_name = old_pod_names.get(targets[0])
            for pod in exporter_pods:
                if pod.metadata.name.startswith(targets[0]) and pod.status.phase == "Running":
                    if old_name and pod.metadata.name == old_name:
                        LOG.debug("Skipping old exporter pod %s (waiting for restart)", old_name)
                        continue
                    exporter_pod = pod
                    break

            if not exporter_pod:
                time.sleep(5)
                continue

            LOG.info("Found new exporter pod %s", exporter_pod.metadata.name)
            node_name = getattr(exporter_pod.spec, "node_name", None) or getattr(exporter_pod.spec, "nodeName", None)
            node_ip = nic_util.get_node_ip(node_name) if node_name else None
            if not node_ip:
                time.sleep(5)
                continue

            if not nic_util.wait_for_metrics_ready(src_pod.metadata.name, src_pod.metadata.namespace, node_ip, configmap_port, timeout=10):
                time.sleep(5)
                continue

            metrics_text = nic_util.pull_metrics(src_pod.metadata.name, src_pod.metadata.namespace, configmap_port, node_ip)
            if metrics_text:
                break
            time.sleep(5)

        if not metrics_text:
            pytest.skip("Could not pull /metrics")

        # --- Verify /metrics does NOT have QP_ metrics ---
        metrics_metric_names = set()
        for line in metrics_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "{" in line:
                mn = line.split("{")[0].strip()
            elif " " in line:
                mn = line.split(" ")[0].strip()
            else:
                continue
            if mn:
                metrics_metric_names.add(mn)

        # Build expected QP metric names (QP_ fields map to lif_qp_*_total)
        qp_metric_names_in_regular = []
        for field in qp_fields:
            metric_name = re.sub(r'pri_?(\d)', r'pri_\1', field.lower())
            if metric_name.startswith("qp_"):
                metric_name = "lif_" + metric_name + "_total"
            if metric_name in metrics_metric_names:
                qp_metric_names_in_regular.append(field)

        if qp_metric_names_in_regular:
            pytest.fail(
                f"QP fields should NOT appear in /metrics but found {len(qp_metric_names_in_regular)}: "
                f"{qp_metric_names_in_regular[:5]}"
            )
        LOG.info("/metrics correctly has no QP_ metrics (%d QP fields excluded)", len(qp_fields))

        # --- Verify /metrics?debug=qp DOES have QP metrics ---
        qp_curl_cmd = f"curl -sS --connect-timeout 10 'http://{node_ip}:{configmap_port}/metrics?debug=qp'"
        qp_text = None
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                qp_text = nic_util.exec_in_pod_sync(src_pod.metadata.name, src_pod.metadata.namespace, qp_curl_cmd, timeout=15)
                if qp_text and qp_text.strip():
                    break
                LOG.debug("Attempt %d/%d: Empty QP debug metrics response", attempt, max_retries)
            except Exception as e:
                LOG.warning("Attempt %d/%d QP debug fetch failed: %s", attempt, max_retries, e)
            if attempt < max_retries:
                time.sleep(2)

        if not qp_text or not qp_text.strip():
            pytest.fail("Could not fetch /metrics?debug=qp")

        # Parse metric names from debug=qp response
        debug_qp_metric_names = set()
        for line in qp_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "{" in line:
                mn = line.split("{")[0].strip()
            elif " " in line:
                mn = line.split(" ")[0].strip()
            else:
                continue
            if mn:
                debug_qp_metric_names.add(mn)

        if not debug_qp_metric_names:
            LOG.info("/metrics?debug=qp returned no metric names (expected when all QP fields excluded)")

        # Check that the removed QP_ fields are also NOT present in debug=qp
        # (when QP fields are excluded from NICConfig.Fields, the exporter does not
        # expose them in either /metrics or /metrics?debug=qp)
        leaked_in_debug = []
        for field in qp_fields:
            metric_name = re.sub(r'pri_?(\d)', r'pri_\1', field.lower())
            # QP_ fields map to lif_qp_*_total in the exporter
            if metric_name.startswith("qp_"):
                metric_name = "lif_" + metric_name + "_total"
            if metric_name in debug_qp_metric_names:
                leaked_in_debug.append(field)

        if leaked_in_debug:
            pytest.fail(
                f"Excluded QP fields should NOT appear in /metrics?debug=qp but found "
                f"{len(leaked_in_debug)}: {leaked_in_debug[:10]}"
            )

        LOG.info(
            "PF QP exclusion test passed: /metrics has 0 QP metrics, "
            "/metrics?debug=qp also has 0 excluded QP metrics (%d QP fields excluded)",
            len(qp_fields),
        )

    finally:
        for name in targets:
            try:
                if original_config_names[name]:
                    set_exporter_configmap(nc_namespace, name, original_config_names[name])
                else:
                    clear_exporter_configmap(nc_namespace, name)
            except Exception:
                pass
        nic_util.delete_configmap_quietly(EXCLUDE_QP_FIELDS_CONFIGMAP_NAME_PF, nc_namespace)


@pytest.mark.timeout(TEST_TIMEOUT + 60)
def test_replace_first_three_fields_pf():
    """PF: Replace first 3 fields with dummy names and verify old field metrics removed, dummy fields absent."""
    v1 = k8s_client.CoreV1Api()
    nc_namespace = "kube-amd-network"

    try:
        items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    if not items:
        pytest.skip("No NetworkConfig objects found")

    try:
        template_doc = nic_util.load_configmap_template(CONFIGMAP_TEMPLATE_PATH)
        nic_fields = nic_util.load_nic_fields_from_configmap(CONFIGMAP_TEMPLATE_PATH)
    except Exception as e:
        pytest.skip(f"Failed to load configmap expectations: {e}")

    if not nic_fields or len(nic_fields) < 3:
        pytest.skip("NICConfig.Fields must have at least 3 fields")

    # Replace first 3 fields with dummy names
    original_first_three = nic_fields[:3]
    modified_fields = ["DUMMY_FIELD_1", "DUMMY_FIELD_2", "DUMMY_FIELD_3"] + nic_fields[3:]
    
    config_doc = nic_util.build_configmap_for_target(template_doc, REPLACE_FIELDS_CONFIGMAP_NAME_PF, nc_namespace)
    config_doc = nic_util.update_configmap_config_json(
        config_doc,
        nic_fields=modified_fields,
    )
    configmap_port = nic_util.configmap_metrics_port(config_doc)

    targets = []
    original_config_names: Dict[str, str] = {}
    for it in items:
        name = it.get("metadata", {}).get("name")
        if not name or name.startswith("vf-"):
            continue
        targets.append(name)
        original_config_names[name] = (
            it.get("spec", {}).get("metricsExporter", {}).get("config", {}).get("name")
        )

    if not targets:
        pytest.skip("No eligible PF NetworkConfig objects found")

    workloads = [p for p in list_workloads(v1, namespace="default") if not p.metadata.name.startswith("vf-")]
    if not workloads:
        pytest.skip("No running PF workload pods in default namespace")

    src_pod = workloads[0]
    try:
        nic_util.apply_configmap(config_doc, nc_namespace)

        for name in targets:
            try:
                set_exporter_configmap(nc_namespace, name, REPLACE_FIELDS_CONFIGMAP_NAME_PF)
            except Exception as e:
                pytest.fail(f"Failed to patch metricsExporter config: {e}")

        time.sleep(15)

        # Try to pull metrics and verify old 3 metrics removed, dummy metrics not present
        metrics_text = None
        deadline = time.time() + 90
        while time.time() < deadline:
            op_pods = nic_util.get_operator_pods(namespace=nc_namespace)
            exporter_pods = op_pods.get("metrics-exporter", [])

            exporter_pod = None
            for pod in exporter_pods:
                if pod.metadata.name.startswith(targets[0]) and pod.status.phase == "Running":
                    exporter_pod = pod
                    break

            if not exporter_pod:
                time.sleep(3)
                continue

            node_name = getattr(exporter_pod.spec, "node_name", None) or getattr(exporter_pod.spec, "nodeName", None)
            node_ip = nic_util.get_node_ip(node_name) if node_name else None
            if not node_ip:
                time.sleep(3)
                continue

            if not nic_util.wait_for_metrics_ready(src_pod.metadata.name, src_pod.metadata.namespace, node_ip, configmap_port, timeout=10):
                time.sleep(3)
                continue

            metrics_text = nic_util.pull_metrics(src_pod.metadata.name, src_pod.metadata.namespace, configmap_port, node_ip)
            if metrics_text:
                break
            time.sleep(3)

        if not metrics_text:
            pytest.skip("Could not pull metrics")

        # Verify old 3 metrics are NOT present
        failures = []
        for field in original_first_three:
            metric_name = f"amd_{field.lower()}"
            if any(metric_name in line for line in metrics_text.splitlines() if not line.startswith("#")):
                failures.append(f"Old field {field} should be removed but found {metric_name}")

        # Verify dummy metrics are NOT present
        for dummy in ["DUMMY_FIELD_1", "DUMMY_FIELD_2", "DUMMY_FIELD_3"]:
            metric_name = f"amd_{dummy.lower()}"
            if any(metric_name in line for line in metrics_text.splitlines() if not line.startswith("#")):
                failures.append(f"Dummy field {dummy} should not be exported but found {metric_name}")

        if failures:
            pytest.fail(", ".join(failures))

    finally:
        for name in targets:
            try:
                if original_config_names[name]:
                    set_exporter_configmap(nc_namespace, name, original_config_names[name])
                else:
                    clear_exporter_configmap(nc_namespace, name)
            except Exception:
                pass
        nic_util.delete_configmap_quietly(REPLACE_FIELDS_CONFIGMAP_NAME_PF, nc_namespace)


@pytest.mark.timeout(TEST_TIMEOUT + 60)
def test_replace_first_three_fields_vf():
    """VF: Replace first 3 fields with dummy names and verify old field metrics removed, dummy fields absent."""
    v1 = k8s_client.CoreV1Api()
    nc_namespace = "kube-amd-network"

    try:
        items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    if not items:
        pytest.skip("No NetworkConfig objects found")

    try:
        template_doc = nic_util.load_configmap_template(CONFIGMAP_TEMPLATE_PATH)
        nic_fields = nic_util.load_nic_fields_from_configmap(CONFIGMAP_TEMPLATE_PATH)
    except Exception as e:
        pytest.skip(f"Failed to load configmap expectations: {e}")

    if not nic_fields or len(nic_fields) < 3:
        pytest.skip("NICConfig.Fields must have at least 3 fields")

    # Replace first 3 fields with dummy names
    original_first_three = nic_fields[:3]
    modified_fields = ["DUMMY_FIELD_1", "DUMMY_FIELD_2", "DUMMY_FIELD_3"] + nic_fields[3:]
    
    config_doc = nic_util.build_configmap_for_target(template_doc, REPLACE_FIELDS_CONFIGMAP_NAME_VF, nc_namespace)
    config_doc = nic_util.update_configmap_config_json(
        config_doc,
        nic_fields=modified_fields,
    )
    configmap_port = nic_util.configmap_metrics_port(config_doc)

    targets = []
    original_config_names: Dict[str, str] = {}
    for it in items:
        name = it.get("metadata", {}).get("name")
        if not name or not name.startswith("vf-"):
            continue
        targets.append(name)
        original_config_names[name] = (
            it.get("spec", {}).get("metricsExporter", {}).get("config", {}).get("name")
        )

    if not targets:
        pytest.skip("No eligible VF NetworkConfig objects found")

    workloads = [p for p in list_workloads(v1, namespace="default") if p.metadata.name.startswith("vf-")]
    if not workloads:
        pytest.skip("No running VF workload pods in default namespace")

    src_pod = workloads[0]
    try:
        nic_util.apply_configmap(config_doc, nc_namespace)

        for name in targets:
            try:
                set_exporter_configmap(nc_namespace, name, REPLACE_FIELDS_CONFIGMAP_NAME_VF)
            except Exception as e:
                pytest.fail(f"Failed to patch metricsExporter config: {e}")

        time.sleep(15)

        # Try to pull metrics and verify old 3 metrics removed, dummy metrics not present
        metrics_text = None
        deadline = time.time() + 90
        while time.time() < deadline:
            op_pods = nic_util.get_operator_pods(namespace=nc_namespace)
            exporter_pods = op_pods.get("vf-metrics-exporter", [])

            exporter_pod = None
            for pod in exporter_pods:
                if pod.metadata.name.startswith(targets[0]) and pod.status.phase == "Running":
                    exporter_pod = pod
                    break

            if not exporter_pod:
                time.sleep(3)
                continue

            node_name = getattr(exporter_pod.spec, "node_name", None) or getattr(exporter_pod.spec, "nodeName", None)
            node_ip = nic_util.get_node_ip(node_name) if node_name else None
            if not node_ip:
                time.sleep(3)
                continue

            if not nic_util.wait_for_metrics_ready(src_pod.metadata.name, src_pod.metadata.namespace, node_ip, configmap_port, timeout=10):
                time.sleep(3)
                continue

            metrics_text = nic_util.pull_metrics(src_pod.metadata.name, src_pod.metadata.namespace, configmap_port, node_ip)
            if metrics_text:
                break
            time.sleep(3)

        if not metrics_text:
            pytest.skip("Could not pull metrics")

        # Verify old 3 metrics are NOT present
        failures = []
        for field in original_first_three:
            metric_name = f"amd_{field.lower()}"
            if any(metric_name in line for line in metrics_text.splitlines() if not line.startswith("#")):
                failures.append(f"Old field {field} should be removed but found {metric_name}")

        # Verify dummy metrics are NOT present
        for dummy in ["DUMMY_FIELD_1", "DUMMY_FIELD_2", "DUMMY_FIELD_3"]:
            metric_name = f"amd_{dummy.lower()}"
            if any(metric_name in line for line in metrics_text.splitlines() if not line.startswith("#")):
                failures.append(f"Dummy field {dummy} should not be exported but found {metric_name}")

        if failures:
            pytest.fail(", ".join(failures))

    finally:
        for name in targets:
            try:
                if original_config_names[name]:
                    set_exporter_configmap(nc_namespace, name, original_config_names[name])
                else:
                    clear_exporter_configmap(nc_namespace, name)
            except Exception:
                pass
        nic_util.delete_configmap_quietly(REPLACE_FIELDS_CONFIGMAP_NAME_VF, nc_namespace)


@pytest.mark.timeout(TEST_TIMEOUT + 60)
def test_empty_fields_array_pf():
    """PF: Set NICConfig.Fields to empty array and verify default metrics are still exported."""
    v1 = k8s_client.CoreV1Api()
    nc_namespace = "kube-amd-network"

    try:
        items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    if not items:
        pytest.skip("No NetworkConfig objects found")

    try:
        template_doc = nic_util.load_configmap_template(CONFIGMAP_TEMPLATE_PATH)
        nic_fields = nic_util.load_nic_fields_from_configmap(CONFIGMAP_TEMPLATE_PATH)
    except Exception as e:
        pytest.skip(f"Failed to load configmap expectations: {e}")

    if not nic_fields:
        pytest.skip("No NICConfig.Fields defined in configmap.yaml")

    # Set fields to empty array
    config_doc = nic_util.build_configmap_for_target(template_doc, EMPTY_FIELDS_CONFIGMAP_NAME_PF, nc_namespace)
    config_doc = nic_util.update_configmap_config_json(
        config_doc,
        nic_fields=[],  # Empty array
    )
    configmap_port = nic_util.configmap_metrics_port(config_doc)

    targets = []
    original_config_names: Dict[str, str] = {}
    for it in items:
        name = it.get("metadata", {}).get("name")
        if not name or name.startswith("vf-"):
            continue
        targets.append(name)
        original_config_names[name] = (
            it.get("spec", {}).get("metricsExporter", {}).get("config", {}).get("name")
        )

    if not targets:
        pytest.skip("No eligible PF NetworkConfig objects found")

    workloads = [p for p in list_workloads(v1, namespace="default") if not p.metadata.name.startswith("vf-")]
    if not workloads:
        pytest.skip("No running PF workload pods in default namespace")

    src_pod = workloads[0]
    try:
        nic_util.apply_configmap(config_doc, nc_namespace)

        for name in targets:
            try:
                set_exporter_configmap(nc_namespace, name, EMPTY_FIELDS_CONFIGMAP_NAME_PF)
            except Exception as e:
                pytest.fail(f"Failed to patch metricsExporter config: {e}")

        time.sleep(15)

        # Try to pull metrics and verify default metrics still exist
        metrics_text = None
        deadline = time.time() + 90
        while time.time() < deadline:
            op_pods = nic_util.get_operator_pods(namespace=nc_namespace)
            exporter_pods = op_pods.get("metrics-exporter", [])

            exporter_pod = None
            for pod in exporter_pods:
                if pod.metadata.name.startswith(targets[0]) and pod.status.phase == "Running":
                    exporter_pod = pod
                    break

            if not exporter_pod:
                time.sleep(3)
                continue

            node_name = getattr(exporter_pod.spec, "node_name", None) or getattr(exporter_pod.spec, "nodeName", None)
            node_ip = nic_util.get_node_ip(node_name) if node_name else None
            if not node_ip:
                time.sleep(3)
                continue

            if not nic_util.wait_for_metrics_ready(src_pod.metadata.name, src_pod.metadata.namespace, node_ip, configmap_port, timeout=10):
                time.sleep(3)
                continue

            metrics_text = nic_util.pull_metrics(src_pod.metadata.name, src_pod.metadata.namespace, configmap_port, node_ip)
            if metrics_text:
                break
            time.sleep(3)

        if not metrics_text:
            pytest.skip("Could not pull metrics")

        # Extract metric names from /metrics
        found_metric_names = set()
        for line in metrics_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = PROM_LINE_RE.match(line)
            if m:
                found_metric_names.add(m.group(1))

        # QP metrics are only exposed via /metrics?debug=qp, fetch and merge them
        qp_curl_cmd = f"curl -sS --connect-timeout 10 'http://{node_ip}:{configmap_port}/metrics?debug=qp'"
        for attempt in range(1, 4):
            try:
                qp_text = nic_util.exec_in_pod_sync(src_pod.metadata.name, src_pod.metadata.namespace, qp_curl_cmd, timeout=15)
                if qp_text and qp_text.strip():
                    for line in qp_text.splitlines():
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        m = PROM_LINE_RE.match(line)
                        if m:
                            found_metric_names.add(m.group(1))
                    break
            except Exception as e:
                LOG.warning("Attempt %d/3 QP debug fetch failed: %s", attempt, e)
            if attempt < 3:
                time.sleep(1)

        # Verify that the fields from configmap.yaml are still exported (empty array = use configmap defaults)
        # PRI normalization: configmap may use PRI0 or PRI_0; endpoint always uses pri_0
        # QP_ fields map to lif_qp_*_total in the exporter; non-QP fields get amd_ prefix
        missing_fields = []
        for field in nic_fields:
            metric_name = re.sub(r'pri_?(\d)', r'pri_\1', field.lower())
            if metric_name.startswith("qp_"):
                # Try both with and without amd_ prefix for QP fields
                qp_name = "lif_" + metric_name + "_total"
                qp_name_prefixed = "amd_lif_" + metric_name + "_total"
                if qp_name not in found_metric_names and qp_name_prefixed not in found_metric_names:
                    missing_fields.append(field)
            else:
                metric_name = f"amd_{metric_name}"
                if metric_name not in found_metric_names:
                    missing_fields.append(field)
        if missing_fields:
            pytest.fail(
                f"PF empty fields array: expected configmap fields still exported but {len(missing_fields)} missing: "
                f"{missing_fields[:10]}"
            )

    finally:
        for name in targets:
            try:
                if original_config_names[name]:
                    set_exporter_configmap(nc_namespace, name, original_config_names[name])
                else:
                    clear_exporter_configmap(nc_namespace, name)
            except Exception:
                pass
        nic_util.delete_configmap_quietly(EMPTY_FIELDS_CONFIGMAP_NAME_PF, nc_namespace)


@pytest.mark.timeout(TEST_TIMEOUT + 60)
def test_empty_fields_array_vf():
    """VF: Set NICConfig.Fields to empty array and verify RDMA_/ETH_ metrics (same as test_config_map_fields_in_metrics_vf) are still exported."""
    v1 = k8s_client.CoreV1Api()
    nc_namespace = "kube-amd-network"

    try:
        items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    if not items:
        pytest.skip("No NetworkConfig objects found")

    try:
        template_doc = nic_util.load_configmap_template(CONFIGMAP_TEMPLATE_PATH)
        all_nic_fields = nic_util.load_nic_fields_from_configmap(CONFIGMAP_TEMPLATE_PATH)
    except Exception as e:
        pytest.skip(f"Failed to load configmap expectations: {e}")

    # VF validates only RDMA_ and ETH_ fields (same subset as test_config_map_fields_in_metrics_vf)
    nic_fields = [f for f in all_nic_fields if f.lower().startswith("rdma_") or f.lower().startswith("eth_")]
    if not nic_fields:
        pytest.skip("No RDMA_/ETH_ fields found in NICConfig.Fields in configmap.yaml")

    # Set fields to empty array
    config_doc = nic_util.build_configmap_for_target(template_doc, EMPTY_FIELDS_CONFIGMAP_NAME_VF, nc_namespace)
    config_doc = nic_util.update_configmap_config_json(
        config_doc,
        nic_fields=[],  # Empty array
    )
    configmap_port = nic_util.configmap_metrics_port(config_doc)

    targets = []
    original_config_names: Dict[str, str] = {}
    for it in items:
        name = it.get("metadata", {}).get("name")
        if not name or not name.startswith("vf-"):
            continue
        targets.append(name)
        original_config_names[name] = (
            it.get("spec", {}).get("metricsExporter", {}).get("config", {}).get("name")
        )

    if not targets:
        pytest.skip("No eligible VF NetworkConfig objects found")

    workloads = [p for p in list_workloads(v1, namespace="default") if p.metadata.name.startswith("vf-")]
    if not workloads:
        pytest.skip("No running VF workload pods in default namespace")

    src_pod = workloads[0]
    try:
        nic_util.apply_configmap(config_doc, nc_namespace)

        for name in targets:
            try:
                set_exporter_configmap(nc_namespace, name, EMPTY_FIELDS_CONFIGMAP_NAME_VF)
            except Exception as e:
                pytest.fail(f"Failed to patch metricsExporter config: {e}")

        time.sleep(15)

        # Try to pull metrics and verify default metrics still exist
        metrics_text = None
        deadline = time.time() + 90
        while time.time() < deadline:
            op_pods = nic_util.get_operator_pods(namespace=nc_namespace)
            exporter_pods = op_pods.get("vf-metrics-exporter", [])

            exporter_pod = None
            for pod in exporter_pods:
                if pod.metadata.name.startswith(targets[0]) and pod.status.phase == "Running":
                    exporter_pod = pod
                    break

            if not exporter_pod:
                time.sleep(3)
                continue

            node_name = getattr(exporter_pod.spec, "node_name", None) or getattr(exporter_pod.spec, "nodeName", None)
            node_ip = nic_util.get_node_ip(node_name) if node_name else None
            if not node_ip:
                time.sleep(3)
                continue

            if not nic_util.wait_for_metrics_ready(src_pod.metadata.name, src_pod.metadata.namespace, node_ip, configmap_port, timeout=10):
                time.sleep(3)
                continue

            metrics_text = nic_util.pull_metrics(src_pod.metadata.name, src_pod.metadata.namespace, configmap_port, node_ip)
            if metrics_text:
                break
            time.sleep(3)

        if not metrics_text:
            pytest.skip("Could not pull metrics")

        # Verify that the fields from configmap.yaml are still exported (empty array = use configmap defaults)
        # PRI normalization: configmap may use PRI0 or PRI_0; endpoint always uses pri_0
        expected_metric_names = {re.sub(r'pri_?(\d)', r'pri_\1', f"amd_{field.lower()}") for field in nic_fields}
        found_metric_names = set()
        for line in metrics_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = PROM_LINE_RE.match(line)
            if m and m.group(1) in expected_metric_names:
                found_metric_names.add(m.group(1))

        missing_fields = [
            field for field in nic_fields
            if re.sub(r'pri_?(\d)', r'pri_\1', f"amd_{field.lower()}") not in found_metric_names
        ]
        if missing_fields:
            pytest.fail(
                f"VF empty fields array: expected RDMA_/ETH_ configmap fields still exported but {len(missing_fields)} missing: "
                f"{missing_fields[:10]}"
            )

    finally:
        for name in targets:
            try:
                if original_config_names[name]:
                    set_exporter_configmap(nc_namespace, name, original_config_names[name])
                else:
                    clear_exporter_configmap(nc_namespace, name)
            except Exception:
                pass
        nic_util.delete_configmap_quietly(EMPTY_FIELDS_CONFIGMAP_NAME_VF, nc_namespace)


@pytest.mark.timeout(TEST_TIMEOUT + 60)
def test_remove_labels_label_distribution_pf():
    """PF: validate mandatory base labels on NIC metrics and require pod labels on LIF, RDMA, ETH, and QP metrics."""
    v1 = k8s_client.CoreV1Api()
    nc_namespace = "kube-amd-network"

    try:
        items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    if not items:
        pytest.skip("No NetworkConfig objects found")

    try:
        template_doc = nic_util.load_configmap_template(CONFIGMAP_TEMPLATE_PATH)
        nic_labels = nic_util.load_nic_labels_from_configmap(CONFIGMAP_TEMPLATE_PATH)
    except Exception as e:
        pytest.skip(f"Failed to load configmap expectations: {e}")

    if not nic_labels:
        pytest.skip("No NICConfig.Labels defined in configmap.yaml")

    # Keep pod-facing labels for PF workload metrics; only remove optional labels.
    labels_to_remove = ["NIC_UUID", "FIRMWARE_VERSION"]
    reduced_labels = [l for l in nic_labels if l not in labels_to_remove]

    config_doc = nic_util.build_configmap_for_target(template_doc, REMOVE_LABELS_CONFIGMAP_NAME_PF, nc_namespace)
    config_doc = nic_util.update_configmap_config_json(
        config_doc,
        nic_labels=reduced_labels,
    )
    configmap_port = nic_util.configmap_metrics_port(config_doc)

    targets = []
    original_config_names: Dict[str, str] = {}
    for it in items:
        name = it.get("metadata", {}).get("name")
        if not name or name.startswith("vf-"):
            continue
        targets.append(name)
        original_config_names[name] = (
            it.get("spec", {}).get("metricsExporter", {}).get("config", {}).get("name")
        )

    if not targets:
        pytest.skip("No eligible PF NetworkConfig objects found")

    workloads = [p for p in list_workloads(v1, namespace="default") if not p.metadata.name.startswith("vf-")]
    if not workloads:
        pytest.skip("No running PF workload pods in default namespace")

    src_pod = workloads[0]
    try:
        nic_util.apply_configmap(config_doc, nc_namespace)

        for name in targets:
            try:
                set_exporter_configmap(nc_namespace, name, REMOVE_LABELS_CONFIGMAP_NAME_PF)
            except Exception as e:
                pytest.fail(f"Failed to patch metricsExporter config: {e}")

        time.sleep(15)

        # Try to pull metrics and validate label distribution
        metrics_text = None
        deadline = time.time() + 90
        while time.time() < deadline:
            op_pods = nic_util.get_operator_pods(namespace=nc_namespace)
            exporter_pods = op_pods.get("metrics-exporter", [])

            exporter_pod = None
            for pod in exporter_pods:
                if pod.metadata.name.startswith(targets[0]) and pod.status.phase == "Running":
                    exporter_pod = pod
                    break

            if not exporter_pod:
                time.sleep(3)
                continue

            node_name = getattr(exporter_pod.spec, "node_name", None) or getattr(exporter_pod.spec, "nodeName", None)
            node_ip = nic_util.get_node_ip(node_name) if node_name else None
            if not node_ip:
                time.sleep(3)
                continue

            if not nic_util.wait_for_metrics_ready(src_pod.metadata.name, src_pod.metadata.namespace, node_ip, configmap_port, timeout=10):
                time.sleep(3)
                continue

            metrics_text = nic_util.pull_metrics(src_pod.metadata.name, src_pod.metadata.namespace, configmap_port, node_ip)
            if metrics_text:
                break
            time.sleep(3)

        if not metrics_text:
            pytest.skip("Could not pull metrics")

        # Validate label distribution by metric type
        labels_by_type = nic_util.validate_metric_labels_by_type(metrics_text, "amd_")

        failures = []
        mandatory_base_labels = {"nic_id", "hostname", "serial_number"}
        mandatory_workload_labels = mandatory_base_labels | {"pod", "namespace", "container"}

        # nic_port_stats: must include the base mandatory labels.
        if "nic_port_stats" in labels_by_type:
            port_stats_labels = set(labels_by_type["nic_port_stats"])
            missing = mandatory_base_labels - port_stats_labels
            if missing:
                failures.append(f"nic_port_stats missing mandatory labels: {sorted(missing)}")

        # nic_lif_stats: must include base labels plus pod-facing workload labels.
        if "nic_lif_stats" in labels_by_type:
            lif_stats_labels = set(labels_by_type["nic_lif_stats"])
            missing = mandatory_workload_labels - lif_stats_labels
            if missing:
                failures.append(f"nic_lif_stats missing mandatory labels: {sorted(missing)}")

        # ETH, RDMA, and QP metrics must include base labels plus pod-facing workload labels.
        for metric_type in ["eth", "rdma", "qp"]:
            if metric_type in labels_by_type:
                labels = set(labels_by_type[metric_type])
                missing = mandatory_workload_labels - labels
                if missing:
                    failures.append(f"{metric_type} missing mandatory labels: {sorted(missing)}")

        if failures:
            pytest.fail(f"Label distribution validation failed: {'; '.join(failures)}")

    finally:
        for name in targets:
            try:
                if original_config_names[name]:
                    set_exporter_configmap(nc_namespace, name, original_config_names[name])
                else:
                    clear_exporter_configmap(nc_namespace, name)
            except Exception:
                pass
        nic_util.delete_configmap_quietly(REMOVE_LABELS_CONFIGMAP_NAME_PF, nc_namespace)


@pytest.mark.timeout(TEST_TIMEOUT + 60)
def test_remove_labels_label_distribution_vf():
    """VF: validate mandatory base labels on NIC metrics and require pod labels on RDMA metrics."""
    v1 = k8s_client.CoreV1Api()
    nc_namespace = "kube-amd-network"

    try:
        items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    if not items:
        pytest.skip("No NetworkConfig objects found")

    try:
        template_doc = nic_util.load_configmap_template(CONFIGMAP_TEMPLATE_PATH)
        nic_labels = nic_util.load_nic_labels_from_configmap(CONFIGMAP_TEMPLATE_PATH)
    except Exception as e:
        pytest.skip(f"Failed to load configmap expectations: {e}")

    if not nic_labels:
        pytest.skip("No NICConfig.Labels defined in configmap.yaml")

    # Keep pod-facing labels for VF RDMA metrics; only remove optional labels.
    labels_to_remove = ["NIC_UUID", "FIRMWARE_VERSION"]
    reduced_labels = [l for l in nic_labels if l not in labels_to_remove]

    config_doc = nic_util.build_configmap_for_target(template_doc, REMOVE_LABELS_CONFIGMAP_NAME_VF, nc_namespace)
    config_doc = nic_util.update_configmap_config_json(
        config_doc,
        nic_labels=reduced_labels,
    )
    configmap_port = nic_util.configmap_metrics_port(config_doc)

    targets = []
    original_config_names: Dict[str, str] = {}
    for it in items:
        name = it.get("metadata", {}).get("name")
        if not name or not name.startswith("vf-"):
            continue
        targets.append(name)
        original_config_names[name] = (
            it.get("spec", {}).get("metricsExporter", {}).get("config", {}).get("name")
        )

    if not targets:
        pytest.skip("No eligible VF NetworkConfig objects found")

    workloads = [p for p in list_workloads(v1, namespace="default") if p.metadata.name.startswith("vf-")]
    if not workloads:
        pytest.skip("No running VF workload pods in default namespace")

    src_pod = workloads[0]
    try:
        nic_util.apply_configmap(config_doc, nc_namespace)

        for name in targets:
            try:
                set_exporter_configmap(nc_namespace, name, REMOVE_LABELS_CONFIGMAP_NAME_VF)
            except Exception as e:
                pytest.fail(f"Failed to patch metricsExporter config: {e}")

        time.sleep(15)

        # Try to pull metrics and validate label distribution
        metrics_text = None
        deadline = time.time() + 90
        while time.time() < deadline:
            op_pods = nic_util.get_operator_pods(namespace=nc_namespace)
            exporter_pods = op_pods.get("vf-metrics-exporter", [])

            exporter_pod = None
            for pod in exporter_pods:
                if pod.metadata.name.startswith(targets[0]) and pod.status.phase == "Running":
                    exporter_pod = pod
                    break

            if not exporter_pod:
                time.sleep(3)
                continue

            node_name = getattr(exporter_pod.spec, "node_name", None) or getattr(exporter_pod.spec, "nodeName", None)
            node_ip = nic_util.get_node_ip(node_name) if node_name else None
            if not node_ip:
                time.sleep(3)
                continue

            if not nic_util.wait_for_metrics_ready(src_pod.metadata.name, src_pod.metadata.namespace, node_ip, configmap_port, timeout=10):
                time.sleep(3)
                continue

            metrics_text = nic_util.pull_metrics(src_pod.metadata.name, src_pod.metadata.namespace, configmap_port, node_ip)
            if metrics_text:
                break
            time.sleep(3)

        if not metrics_text:
            pytest.skip("Could not pull metrics")

        # Validate label distribution by metric type
        labels_by_type = nic_util.validate_metric_labels_by_type(metrics_text, "amd_")

        failures = []
        mandatory_base_labels = {"nic_id", "hostname", "serial_number"}
        mandatory_rdma_labels = mandatory_base_labels | {"pod", "namespace", "container"}

        # nic_port_stats: must include the base mandatory labels.
        if "nic_port_stats" in labels_by_type:
            port_stats_labels = set(labels_by_type["nic_port_stats"])
            missing = mandatory_base_labels - port_stats_labels
            if missing:
                failures.append(f"nic_port_stats missing mandatory labels: {sorted(missing)}")

        # nic_lif_stats: must include the base mandatory labels.
        if "nic_lif_stats" in labels_by_type:
            lif_stats_labels = set(labels_by_type["nic_lif_stats"])
            missing = mandatory_base_labels - lif_stats_labels
            if missing:
                failures.append(f"nic_lif_stats missing mandatory labels: {sorted(missing)}")

        # RDMA metrics must include base labels plus pod, namespace, and container.
        if "rdma" in labels_by_type:
            rdma_labels = set(labels_by_type["rdma"])
            missing = mandatory_rdma_labels - rdma_labels
            if missing:
                failures.append(f"rdma missing mandatory labels: {sorted(missing)}")

        # QP metrics must include the base mandatory labels.
        if "qp" in labels_by_type:
            qp_labels = set(labels_by_type["qp"])
            missing = mandatory_base_labels - qp_labels
            if missing:
                failures.append(f"qp missing mandatory labels: {sorted(missing)}")

        if failures:
            pytest.fail(f"Label distribution validation failed: {'; '.join(failures)}")

    finally:
        for name in targets:
            try:
                if original_config_names[name]:
                    set_exporter_configmap(nc_namespace, name, original_config_names[name])
                else:
                    clear_exporter_configmap(nc_namespace, name)
            except Exception:
                pass
        nic_util.delete_configmap_quietly(REMOVE_LABELS_CONFIGMAP_NAME_VF, nc_namespace)


@pytest.mark.timeout(TEST_TIMEOUT + 60)
def test_lif_qp_sum_matches_debug_qp_pf():
    """PF: Verify each lif_qp_*_total metric equals the sum of per-QP values from /metrics?debug=qp."""
    v1 = k8s_client.CoreV1Api()

    nc_namespace = "kube-amd-network"
    try:
        nc_items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    nodeport_by_config = {}
    for it in nc_items:
        name = it.get("metadata", {}).get("name")
        if not name:
            continue
        node_port = it.get("spec", {}).get("metricsExporter", {}).get("nodePort")
        if node_port is None:
            pytest.fail(f"{name} missing spec.metricsExporter.nodePort")
        nodeport_by_config[name] = int(node_port)

    non_vf_configs = [n for n in nodeport_by_config if not n.startswith("vf-")]
    if not non_vf_configs:
        pytest.skip("No PF NetworkConfig found")

    all_pods = list_pods(v1, "default")
    wpods = [p for p in all_pods if p.status.phase == "Running" and not p.metadata.name.startswith("vf-workload")]
    if not wpods:
        pytest.skip("No running PF workload pods in default")

    src_pod = wpods[0]
    cfg = non_vf_configs[0]
    port = nodeport_by_config[cfg]

    if not nic_util.wait_for_exporter_pod_running(nc_namespace, cfg):
        pytest.fail(f"Metrics exporter pod for {cfg} not Running")

    node_name = getattr(src_pod.spec, "node_name", None) or getattr(src_pod.spec, "nodeName", None)
    node_ip = nic_util.get_node_ip(node_name)
    if not node_ip:
        pytest.skip("Could not determine node IP")

    # Fetch /metrics
    metrics_text = None
    for attempt in range(5):
        metrics_text = nic_util.pull_metrics(src_pod.metadata.name, src_pod.metadata.namespace, port, node_ip)
        if metrics_text and metrics_text.strip():
            break
        time.sleep(3)
    if not metrics_text:
        pytest.skip("Could not pull /metrics")

    # Parse lif_qp_*_total metrics from /metrics
    # Key: (metric_name, frozenset_of_labels_without_qp_id) -> float value
    lif_qp_metrics = {}
    for line in metrics_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = PROM_LINE_RE.match(line)
        if not m:
            continue
        metric_name = m.group(1)
        if not metric_name.startswith("lif_qp_") or not metric_name.endswith("_total"):
            continue
        labels = nic_util.parse_prometheus_labels(m.group(2)) if m.group(2) else {}
        labels.pop("qp_id", None)
        value = float(m.group(3))
        key = (metric_name, frozenset(labels.items()))
        lif_qp_metrics[key] = value

    if not lif_qp_metrics:
        pytest.skip("No lif_qp_*_total metrics found in /metrics")

    LOG.info("Found %d lif_qp_*_total metric series in /metrics", len(lif_qp_metrics))

    # Fetch /metrics?debug=qp
    qp_text = None
    qp_curl_cmd = f"curl -sS --connect-timeout 10 'http://{node_ip}:{port}/metrics?debug=qp'"
    for attempt in range(1, 4):
        try:
            qp_text = nic_util.exec_in_pod_sync(src_pod.metadata.name, src_pod.metadata.namespace, qp_curl_cmd, timeout=15)
            if qp_text and qp_text.strip():
                break
        except Exception as e:
            LOG.warning("Attempt %d/3 QP debug fetch failed: %s", attempt, e)
        if attempt < 3:
            time.sleep(2)
    if not qp_text:
        pytest.fail("Could not fetch /metrics?debug=qp")

    # Parse per-QP metrics and sum by (lif_metric_name, labels_without_qp_id)
    qp_sums = {}
    for line in qp_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = PROM_LINE_RE.match(line)
        if not m:
            continue
        metric_name = m.group(1)
        # Map QP metric name to LIF metric name: qp_X -> lif_qp_X_total
        lif_name = "lif_" + metric_name + "_total"
        labels = nic_util.parse_prometheus_labels(m.group(2)) if m.group(2) else {}
        labels.pop("qp_id", None)
        value = float(m.group(3))
        key = (lif_name, frozenset(labels.items()))
        qp_sums[key] = qp_sums.get(key, 0.0) + value

    LOG.info("Found %d aggregated QP metric series from /metrics?debug=qp", len(qp_sums))

    # Validate: every lif_qp_*_total should match the sum of per-QP values
    mismatches = []
    missing_in_qp = []
    for key, lif_val in sorted(lif_qp_metrics.items()):
        metric_name, _ = key
        if key not in qp_sums:
            missing_in_qp.append(metric_name)
            continue
        qp_sum = qp_sums[key]
        if abs(lif_val - qp_sum) > max(1e-6, abs(lif_val) * 1e-9):
            mismatches.append(
                f"{metric_name} lif={lif_val} qp_sum={qp_sum} diff={lif_val - qp_sum}"
            )

    failures = []
    if missing_in_qp:
        failures.append(f"LIF metrics with no QP debug counterpart: {missing_in_qp}")
    if mismatches:
        failures.append(f"Value mismatches: {mismatches}")

    if failures:
        pytest.fail("; ".join(failures))

    LOG.info("All %d lif_qp_*_total metrics match their per-QP sums", len(lif_qp_metrics))


@pytest.mark.timeout(TEST_TIMEOUT * 2)
def test_metrics_change_after_traffic_pf():
    """Fetch PF metrics before and after IB traffic; fail if no metric values changed."""
    v1 = k8s_client.CoreV1Api()

    nc_namespace = "kube-amd-network"
    try:
        nc_items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    nodeport_by_config: Dict[str, int] = {}
    for it in nc_items:
        name = it.get("metadata", {}).get("name")
        if not name:
            continue
        node_port = it.get("spec", {}).get("metricsExporter", {}).get("nodePort")
        if node_port is None:
            pytest.fail(f"{name} missing spec.metricsExporter.nodePort")
        nodeport_by_config[name] = int(node_port)

    non_vf_configs = [n for n in nodeport_by_config if not n.startswith("vf-")]
    if not non_vf_configs:
        pytest.skip("No PF NetworkConfig found")

    all_pods = list_pods(v1, "default")
    wpods = [p for p in all_pods if p.status.phase == "Running" and not p.metadata.name.startswith("vf-workload")]
    if not wpods:
        pytest.skip("No running PF workload pods in default")

    cfg = non_vf_configs[0]
    port = nodeport_by_config[cfg]

    if not nic_util.wait_for_exporter_pod_running(nc_namespace, cfg):
        pytest.fail(f"Metrics exporter pod for {cfg} not Running")

    src_pod = wpods[0]
    node_name = getattr(src_pod.spec, "node_name", None) or getattr(src_pod.spec, "nodeName", None)
    node_ip = nic_util.get_node_ip(node_name)
    if not node_ip:
        pytest.fail(f"Could not resolve node IP for pod {src_pod.metadata.name}")

    if not nic_util.wait_for_metrics_ready(src_pod.metadata.name, src_pod.metadata.namespace, node_ip, port, timeout=30):
        pytest.fail(f"PF metrics not ready on {node_ip}:{port}")

    # Helper to fetch both /metrics and /metrics?debug=qp and merge them
    def _pull_merged_pf_metrics():
        base_text = nic_util.pull_metrics(src_pod.metadata.name, src_pod.metadata.namespace, port, node_ip)
        merged = nic_util.parse_all_metrics(base_text) if base_text else {}

        qp_curl_cmd = f"curl -sS --connect-timeout 10 'http://{node_ip}:{port}/metrics?debug=qp'"
        try:
            qp_text = nic_util.exec_in_pod_sync(src_pod.metadata.name, src_pod.metadata.namespace, qp_curl_cmd, timeout=15)
        except Exception as e:
            LOG.warning("Failed to fetch /metrics?debug=qp: %s", e)
            qp_text = None

        qp_parsed = nic_util.parse_all_metrics(qp_text) if qp_text else {}
        if qp_parsed:
            LOG.info("Fetched %d QP metric series from /metrics?debug=qp", len(qp_parsed))
            for key, val in qp_parsed.items():
                if key not in merged:
                    merged[key] = val

        return base_text, merged

    # --- Snapshot BEFORE traffic ---
    before_raw, before = _pull_merged_pf_metrics()
    if not before_raw:
        pytest.fail(f"PF: could not pull metrics BEFORE traffic from {node_ip}:{port}")
    LOG.info("PF before-traffic snapshot: %d metric series (/metrics + /metrics?debug=qp)", len(before))

    # --- Run IB traffic ---
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(wpods)))) as ex:
        futures = {ex.submit(nic_util.run_ib_traffic, p.metadata.name, p.metadata.namespace): p for p in wpods}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                fut.result()
            except Exception as e:
                LOG.error("nic_util.run_ib_traffic failed for %s: %s", p.metadata.name, e)

    # small settle time so counters are refreshed
    time.sleep(5)

    # --- Snapshot AFTER traffic ---
    after_raw, after = _pull_merged_pf_metrics()
    if not after_raw:
        pytest.fail(f"PF: could not pull metrics AFTER traffic from {node_ip}:{port}")
    LOG.info("PF after-traffic snapshot: %d metric series (/metrics + /metrics?debug=qp)", len(after))

    # --- Diff ---
    changed = nic_util.diff_metrics(before, after)

    if not changed:
        pytest.fail(
            "PF: no metric values changed after running IB traffic. "
            f"Checked both /metrics and /metrics?debug=qp. "
            f"Compared {len(set(before) & set(after))} common metric series."
        )

    LOG.info("PF: %d metric(s) changed after traffic:", len(changed))
    for key, vals in sorted(changed.items()):
        LOG.info("  %s  before=%.6g  after=%.6g", key, vals["before"], vals["after"])


@pytest.mark.timeout(TEST_TIMEOUT * 2)
def test_metrics_change_after_traffic_vf():
    """Fetch VF metrics before and after IB traffic; fail if no metric values changed."""
    v1 = k8s_client.CoreV1Api()

    nc_namespace = "kube-amd-network"
    try:
        nc_items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    nodeport_by_config: Dict[str, int] = {}
    for it in nc_items:
        name = it.get("metadata", {}).get("name")
        if not name:
            continue
        node_port = it.get("spec", {}).get("metricsExporter", {}).get("nodePort")
        if node_port is None:
            pytest.fail(f"{name} missing spec.metricsExporter.nodePort")
        nodeport_by_config[name] = int(node_port)

    vf_configs = [n for n in nodeport_by_config if n.startswith("vf-")]
    if not vf_configs:
        pytest.skip("No VF NetworkConfig found")

    all_pods = list_pods(v1, "default")
    wpods = [p for p in all_pods if p.status.phase == "Running" and p.metadata.name.startswith("vf-workload")]
    if not wpods:
        pytest.skip("No running VF workload pods in default")

    cfg = vf_configs[0]
    port = nodeport_by_config[cfg]

    if not nic_util.wait_for_exporter_pod_running(nc_namespace, cfg):
        pytest.fail(f"Metrics exporter pod for {cfg} not Running")

    src_pod = wpods[0]
    node_name = getattr(src_pod.spec, "node_name", None) or getattr(src_pod.spec, "nodeName", None)
    node_ip = nic_util.get_node_ip(node_name)
    if not node_ip:
        pytest.fail(f"Could not resolve node IP for pod {src_pod.metadata.name}")

    if not nic_util.wait_for_metrics_ready(src_pod.metadata.name, src_pod.metadata.namespace, node_ip, port, timeout=30):
        pytest.fail(f"VF metrics not ready on {node_ip}:{port}")

    # --- Snapshot BEFORE traffic ---
    before_text = nic_util.pull_metrics(src_pod.metadata.name, src_pod.metadata.namespace, port, node_ip)
    if not before_text:
        pytest.fail(f"VF: could not pull metrics BEFORE traffic from {node_ip}:{port}")
    before = nic_util.parse_all_metrics(before_text)
    LOG.info("VF before-traffic snapshot: %d metric series", len(before))

    # --- Run IB traffic ---
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(wpods)))) as ex:
        futures = {ex.submit(nic_util.run_ib_traffic, p.metadata.name, p.metadata.namespace): p for p in wpods}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                fut.result()
            except Exception as e:
                LOG.error("nic_util.run_ib_traffic failed for %s: %s", p.metadata.name, e)

    # small settle time so counters are refreshed
    time.sleep(5)

    # --- Snapshot AFTER traffic ---
    after_text = nic_util.pull_metrics(src_pod.metadata.name, src_pod.metadata.namespace, port, node_ip)
    if not after_text:
        pytest.fail(f"VF: could not pull metrics AFTER traffic from {node_ip}:{port}")
    after = nic_util.parse_all_metrics(after_text)
    LOG.info("VF after-traffic snapshot: %d metric series", len(after))

    # --- Diff ---
    changed = nic_util.diff_metrics(before, after)

    if not changed:
        pytest.fail(
            "VF: no metric values changed after running IB traffic. "
            f"Compared {len(set(before) & set(after))} common metric series."
        )

    LOG.info("VF: %d metric(s) changed after traffic:", len(changed))
    for key, vals in sorted(changed.items()):
        LOG.info("  %s  before=%.6g  after=%.6g", key, vals["before"], vals["after"])


@pytest.mark.timeout(TEST_TIMEOUT)
def test_debug_qp_endpoint_has_no_qp_stats_vf():
    """VF: Verify /metrics?debug=qp does NOT return per-QP metrics for VF workloads."""
    v1 = k8s_client.CoreV1Api()

    nc_namespace = "kube-amd-network"
    try:
        nc_items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    nodeport_by_config = {}
    for it in nc_items:
        name = it.get("metadata", {}).get("name")
        if not name:
            continue
        node_port = it.get("spec", {}).get("metricsExporter", {}).get("nodePort")
        if node_port is None:
            pytest.fail(f"{name} missing spec.metricsExporter.nodePort")
        nodeport_by_config[name] = int(node_port)

    vf_configs = [n for n in nodeport_by_config if n.startswith("vf-")]
    if not vf_configs:
        pytest.skip("No VF NetworkConfig found")

    all_pods = list_pods(v1, "default")
    wpods = [p for p in all_pods if p.status.phase == "Running" and p.metadata.name.startswith("vf-workload")]
    if not wpods:
        pytest.skip("No running VF workload pods in default")

    # Run IB traffic first to ensure QP counters would be non-zero if they existed
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(wpods)))) as ex:
        futures = {ex.submit(nic_util.run_ib_traffic, p.metadata.name, p.metadata.namespace): p for p in wpods}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                fut.result()
            except Exception as e:
                LOG.warning("nic_util.run_ib_traffic failed for %s: %s", p.metadata.name, e)

    src_pod = wpods[0]
    cfg = vf_configs[0]
    port = nodeport_by_config[cfg]

    if not nic_util.wait_for_exporter_pod_running(nc_namespace, cfg):
        pytest.fail(f"Metrics exporter pod for {cfg} not Running")

    node_name = getattr(src_pod.spec, "node_name", None) or getattr(src_pod.spec, "nodeName", None)
    node_ip = nic_util.get_node_ip(node_name)
    if not node_ip:
        pytest.skip("Could not determine node IP")

    if not nic_util.wait_for_metrics_ready(src_pod.metadata.name, src_pod.metadata.namespace, node_ip, port, timeout=30):
        pytest.fail(f"VF metrics not ready on {node_ip}:{port}")

    # Fetch /metrics?debug=qp
    qp_curl_cmd = f"curl -sS --connect-timeout 10 'http://{node_ip}:{port}/metrics?debug=qp'"
    qp_text = None
    for attempt in range(1, 4):
        try:
            qp_text = nic_util.exec_in_pod_sync(src_pod.metadata.name, src_pod.metadata.namespace, qp_curl_cmd, timeout=15)
            if qp_text and qp_text.strip():
                break
        except Exception as e:
            LOG.warning("Attempt %d/3 VF QP debug fetch failed: %s", attempt, e)
        if attempt < 3:
            time.sleep(2)

    # Parse any QP metric lines (lines matching PROM_LINE_RE that have qp_ prefix or qp_id label)
    qp_metric_lines = []
    if qp_text:
        for line in qp_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = PROM_LINE_RE.match(line)
            if not m:
                continue
            metric_name = m.group(1)
            label_block = m.group(2) or ""
            # A per-QP metric has a qp_ prefix or contains a qp_id label
            if metric_name.startswith("qp_") or "qp_id=" in label_block:
                qp_metric_lines.append(line)

    if qp_metric_lines:
        LOG.error("VF /metrics?debug=qp unexpectedly returned %d per-QP metric lines", len(qp_metric_lines))
        for line in qp_metric_lines[:10]:
            LOG.error("  %s", line)
        pytest.fail(
            f"VF: /metrics?debug=qp should NOT contain per-QP stats but found "
            f"{len(qp_metric_lines)} QP metric lines. First 5: {qp_metric_lines[:5]}"
        )

    LOG.info("VF: /metrics?debug=qp correctly contains no per-QP stats")


@pytest.mark.timeout(TEST_TIMEOUT * 3)
def test_metrics_with_zero_replicas_pf():
    """PF: Scale workload deployment to 0 replicas, verify all configmap fields
    are still present in /metrics and /metrics?debug=qp, then scale back to 1
    and confirm workloads are healthy."""
    v1 = k8s_client.CoreV1Api()
    apps_v1 = k8s_client.AppsV1Api()

    nc_namespace = "kube-amd-network"
    workload_namespace = "default"

    # --- Discover PF NetworkConfig and nodePort ---
    try:
        nc_items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    nodeport_by_config: Dict[str, int] = {}
    for it in nc_items:
        name = it.get("metadata", {}).get("name")
        if not name:
            continue
        node_port = it.get("spec", {}).get("metricsExporter", {}).get("nodePort")
        if node_port is None:
            pytest.fail(f"{name} missing spec.metricsExporter.nodePort")
        nodeport_by_config[name] = int(node_port)

    non_vf_configs = [n for n in nodeport_by_config if not n.startswith("vf-")]
    if not non_vf_configs:
        pytest.skip("No PF NetworkConfig found")

    cfg = non_vf_configs[0]
    port = nodeport_by_config[cfg]

    # --- Load expected fields from configmap.yaml ---
    try:
        expected_fields = nic_util.load_nic_fields_from_configmap(CONFIGMAP_TEMPLATE_PATH)
    except Exception as e:
        pytest.skip(f"Failed to load NICConfig.Fields from configmap.yaml: {e}")
    if not expected_fields:
        pytest.skip("No NICConfig.Fields defined in configmap.yaml")

    LOG.info("Loaded %d expected NIC metric fields for PF zero-replicas test", len(expected_fields))

    # --- Discover PF workload deployments ---
    deployments = apps_v1.list_namespaced_deployment(workload_namespace).items
    pf_deployments = [
        d for d in deployments
        if not d.metadata.name.startswith("vf-")
        and d.status.replicas
        and d.status.replicas > 0
    ]
    if not pf_deployments:
        pytest.skip("No PF workload deployments found in default namespace")

    # --- Find a metrics-exporter pod to use as curl source ---
    op_pods = nic_util.get_operator_pods(namespace=nc_namespace)
    exporter_pods = op_pods.get("metrics-exporter", [])
    if not exporter_pods:
        pytest.skip("No PF metrics-exporter pods found in kube-amd-network")

    exporter_pod = exporter_pods[0]
    node_name = getattr(exporter_pod.spec, "node_name", None) or getattr(exporter_pod.spec, "nodeName", None)
    node_ip = nic_util.get_node_ip(node_name) if node_name else None
    if not node_ip:
        pytest.skip("Could not determine node IP for metrics-exporter pod")

    # Remember original replica counts for rollback
    original_replicas: Dict[str, int] = {}
    for dep in pf_deployments:
        original_replicas[dep.metadata.name] = dep.spec.replicas

    try:
        # --- Run IB traffic before scaling down ---
        LOG.info("PF: Running IB traffic before scaling to 0 replicas")
        pf_workloads = [p for p in list_workloads(v1, namespace=workload_namespace) if not p.metadata.name.startswith("vf-")]
        if pf_workloads:
            with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(pf_workloads)))) as ex:
                futures = {ex.submit(nic_util.run_ib_traffic, p.metadata.name, p.metadata.namespace): p for p in pf_workloads}
                for fut in as_completed(futures):
                    p = futures[fut]
                    try:
                        fut.result()
                    except Exception as e:
                        LOG.warning("IB traffic failed for %s: %s", p.metadata.name, e)
            time.sleep(5)

        # --- Scale down to 0 replicas ---
        for dep_name, orig_count in original_replicas.items():
            LOG.info("PF scaling deployment %s from %d to 0 replicas", dep_name, orig_count)
            apps_v1.patch_namespaced_deployment_scale(
                dep_name, workload_namespace, {"spec": {"replicas": 0}}
            )

        # Wait for all PF workload pods to terminate
        deadline = time.time() + 120
        while time.time() < deadline:
            all_pods = list_pods(v1, workload_namespace)
            pf_pods_remaining = [
                p for p in all_pods
                if p.status.phase == "Running"
                and not p.metadata.name.startswith("vf-")
                and any(p.metadata.name.startswith(d) for d in original_replicas)
            ]
            if not pf_pods_remaining:
                break
            LOG.info("Waiting for %d PF workload pods to terminate...", len(pf_pods_remaining))
            time.sleep(5)
        else:
            LOG.warning("Timed out waiting for PF workload pods to terminate")

        LOG.info("PF workload pods scaled to 0. Verifying metrics are still available.")

        # --- Verify /metrics fields ---
        # Re-discover exporter pod (it may have restarted during scaling)
        op_pods = nic_util.get_operator_pods(namespace=nc_namespace)
        exporter_pods = op_pods.get("metrics-exporter", [])
        src_pod_name = None
        src_pod_ns = nc_namespace
        for ep in exporter_pods:
            if ep.status.phase == "Running":
                src_pod_name = ep.metadata.name
                src_pod_ns = ep.metadata.namespace
                nn = getattr(ep.spec, "node_name", None) or getattr(ep.spec, "nodeName", None)
                if nn:
                    node_ip = nic_util.get_node_ip(nn) or node_ip
                break
        if not src_pod_name:
            # Fallback: try any running pod in the namespace
            all_ns_pods = list_pods(v1, nc_namespace)
            for p in all_ns_pods:
                if p.status.phase == "Running":
                    src_pod_name = p.metadata.name
                    src_pod_ns = p.metadata.namespace
                    break
        if not src_pod_name:
            pytest.fail("No running pod available to exec into after scaling to 0 replicas")

        if not nic_util.wait_for_metrics_ready(src_pod_name, src_pod_ns, node_ip, port, timeout=30):
            pytest.fail(f"PF metrics not ready on {node_ip}:{port} after scaling to 0 replicas")

        # Fetch /metrics
        curl_cmd = f"curl -sS --connect-timeout 10 http://{node_ip}:{port}/metrics"
        metrics_text = None
        for attempt in range(1, 4):
            try:
                metrics_text = nic_util.exec_in_pod_sync(src_pod_name, src_pod_ns, curl_cmd, timeout=15)
                if metrics_text and metrics_text.strip():
                    break
            except Exception as e:
                LOG.warning("Attempt %d/3 /metrics fetch failed: %s", attempt, e)
            if attempt < 3:
                time.sleep(2)

        if not metrics_text or not metrics_text.strip():
            pytest.fail("PF: could not fetch /metrics after scaling to 0 replicas")

        # Extract metric names from /metrics
        found_metric_names = set()
        for line in metrics_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "{" in line:
                mn = line.split("{")[0].strip()
            elif " " in line:
                mn = line.split(" ")[0].strip()
            else:
                continue
            if mn:
                found_metric_names.add(mn)

        # Fetch /metrics?debug=qp and merge
        qp_curl_cmd = f"curl -sS --connect-timeout 10 'http://{node_ip}:{port}/metrics?debug=qp'"
        for attempt in range(1, 4):
            try:
                qp_text = nic_util.exec_in_pod_sync(src_pod_name, src_pod_ns, qp_curl_cmd, timeout=15)
                if qp_text and qp_text.strip():
                    for line in qp_text.splitlines():
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if "{" in line:
                            mn = line.split("{")[0].strip()
                        elif " " in line:
                            mn = line.split(" ")[0].strip()
                        else:
                            continue
                        if mn:
                            found_metric_names.add(mn)
                    LOG.info("Merged QP debug metrics from /metrics?debug=qp (zero replicas)")
                    break
            except Exception as e:
                LOG.warning("Attempt %d/3 QP debug fetch failed: %s", attempt, e)
            if attempt < 3:
                time.sleep(2)

        LOG.info("PF (zero replicas): found %d unique metric names from /metrics + /metrics?debug=qp", len(found_metric_names))

        # Check for missing fields
        # QP_ fields map to lif_qp_*_total in the exporter
        missing_fields = []
        for field in expected_fields:
            metric_name = re.sub(r'pri_?(\d)', r'pri_\1', field.lower())
            if metric_name.startswith("qp_"):
                metric_name = "lif_" + metric_name + "_total"
            if metric_name not in found_metric_names:
                missing_fields.append(field)

        if missing_fields:
            LOG.error("PF (zero replicas): %d fields missing: %s", len(missing_fields), missing_fields[:10])
            pytest.fail(
                f"PF (zero replicas): {len(missing_fields)} configmap fields missing from metrics: "
                f"{missing_fields[:10]}..."
            )

        LOG.info("PF (zero replicas): all %d configmap fields present in metrics", len(expected_fields))

    finally:
        # --- Scale back to original replicas ---
        for dep_name, orig_count in original_replicas.items():
            try:
                LOG.info("PF restoring deployment %s to %d replicas", dep_name, orig_count)
                apps_v1.patch_namespaced_deployment_scale(
                    dep_name, workload_namespace, {"spec": {"replicas": orig_count}}
                )
            except Exception as e:
                LOG.error("Failed to restore deployment %s: %s", dep_name, e)

    # --- Verify workloads are healthy after scale-up ---
    LOG.info("Waiting for PF workload pods to become healthy after scale-up...")
    deadline = time.time() + 120
    healthy = False
    while time.time() < deadline:
        all_pods = list_pods(v1, workload_namespace)
        running_pf = [
            p for p in all_pods
            if p.status.phase == "Running"
            and not p.metadata.name.startswith("vf-")
            and any(p.metadata.name.startswith(d) for d in original_replicas)
        ]
        if len(running_pf) >= len(original_replicas):
            # Check all containers are ready
            all_ready = True
            for p in running_pf:
                container_statuses = p.status.container_statuses or []
                if not container_statuses or not all(cs.ready for cs in container_statuses):
                    all_ready = False
                    break
            if all_ready:
                healthy = True
                break
        LOG.info("Waiting for PF pods... %d/%d running", len(running_pf), len(original_replicas))
        time.sleep(5)

    if not healthy:
        pytest.fail(
            f"PF workload pods did not become healthy within 120s after scaling back to original replicas"
        )

    LOG.info("PF: workload pods healthy after scale-up. Test passed.")


@pytest.mark.timeout(TEST_TIMEOUT * 3)
def test_metrics_with_zero_replicas_vf():
    """VF: Scale workload deployment to 0 replicas, verify all RDMA_/ETH_ configmap
    fields are still present in /metrics, then scale back to 1 and confirm workloads
    are healthy."""
    v1 = k8s_client.CoreV1Api()
    apps_v1 = k8s_client.AppsV1Api()

    nc_namespace = "kube-amd-network"
    workload_namespace = "default"

    # --- Discover VF NetworkConfig and nodePort ---
    try:
        nc_items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    nodeport_by_config: Dict[str, int] = {}
    for it in nc_items:
        name = it.get("metadata", {}).get("name")
        if not name:
            continue
        node_port = it.get("spec", {}).get("metricsExporter", {}).get("nodePort")
        if node_port is None:
            pytest.fail(f"{name} missing spec.metricsExporter.nodePort")
        nodeport_by_config[name] = int(node_port)

    vf_configs = [n for n in nodeport_by_config if n.startswith("vf-")]
    if not vf_configs:
        pytest.skip("No VF NetworkConfig found")

    cfg = vf_configs[0]
    port = nodeport_by_config[cfg]

    # --- Load expected fields from configmap.yaml (VF: RDMA_ and ETH_ only) ---
    try:
        all_fields = nic_util.load_nic_fields_from_configmap(CONFIGMAP_TEMPLATE_PATH)
    except Exception as e:
        pytest.skip(f"Failed to load NICConfig.Fields from configmap.yaml: {e}")
    if not all_fields:
        pytest.skip("No NICConfig.Fields defined in configmap.yaml")

    expected_fields = [f for f in all_fields if f.lower().startswith("rdma_") or f.lower().startswith("eth_")]
    LOG.info("Loaded %d VF metric fields (RDMA_/ETH_ only) for zero-replicas test", len(expected_fields))

    # --- Discover VF workload deployments ---
    deployments = apps_v1.list_namespaced_deployment(workload_namespace).items
    vf_deployments = [
        d for d in deployments
        if d.metadata.name.startswith("vf-")
        and d.status.replicas
        and d.status.replicas > 0
    ]
    if not vf_deployments:
        pytest.skip("No VF workload deployments found in default namespace")

    # --- Find a metrics-exporter pod to use as curl source ---
    op_pods = nic_util.get_operator_pods(namespace=nc_namespace)
    exporter_pods = op_pods.get("vf-metrics-exporter", [])
    if not exporter_pods:
        pytest.skip("No VF metrics-exporter pods found in kube-amd-network")

    exporter_pod = exporter_pods[0]
    node_name = getattr(exporter_pod.spec, "node_name", None) or getattr(exporter_pod.spec, "nodeName", None)
    node_ip = nic_util.get_node_ip(node_name) if node_name else None
    if not node_ip:
        pytest.skip("Could not determine node IP for VF metrics-exporter pod")

    # Remember original replica counts for rollback
    original_replicas: Dict[str, int] = {}
    for dep in vf_deployments:
        original_replicas[dep.metadata.name] = dep.spec.replicas

    try:
        # --- Run IB traffic before scaling down ---
        LOG.info("VF: Running IB traffic before scaling to 0 replicas")
        vf_workloads = [p for p in list_workloads(v1, namespace=workload_namespace) if p.metadata.name.startswith("vf-")]
        if vf_workloads:
            with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(vf_workloads)))) as ex:
                futures = {ex.submit(nic_util.run_ib_traffic, p.metadata.name, p.metadata.namespace): p for p in vf_workloads}
                for fut in as_completed(futures):
                    p = futures[fut]
                    try:
                        fut.result()
                    except Exception as e:
                        LOG.warning("IB traffic failed for %s: %s", p.metadata.name, e)
            time.sleep(5)

        # --- Scale down to 0 replicas ---
        for dep_name, orig_count in original_replicas.items():
            LOG.info("VF scaling deployment %s from %d to 0 replicas", dep_name, orig_count)
            apps_v1.patch_namespaced_deployment_scale(
                dep_name, workload_namespace, {"spec": {"replicas": 0}}
            )

        # Wait for all VF workload pods to terminate
        deadline = time.time() + 120
        while time.time() < deadline:
            all_pods = list_pods(v1, workload_namespace)
            vf_pods_remaining = [
                p for p in all_pods
                if p.status.phase == "Running"
                and p.metadata.name.startswith("vf-")
                and any(p.metadata.name.startswith(d) for d in original_replicas)
            ]
            if not vf_pods_remaining:
                break
            LOG.info("Waiting for %d VF workload pods to terminate...", len(vf_pods_remaining))
            time.sleep(5)
        else:
            LOG.warning("Timed out waiting for VF workload pods to terminate")

        LOG.info("VF workload pods scaled to 0. Verifying metrics are still available.")

        # --- Verify /metrics fields ---
        # Re-discover exporter pod (it may have restarted during scaling)
        op_pods = nic_util.get_operator_pods(namespace=nc_namespace)
        vf_exporter_pods = op_pods.get("vf-metrics-exporter", [])
        src_pod_name = None
        src_pod_ns = nc_namespace
        for ep in vf_exporter_pods:
            if ep.status.phase == "Running":
                src_pod_name = ep.metadata.name
                src_pod_ns = ep.metadata.namespace
                nn = getattr(ep.spec, "node_name", None) or getattr(ep.spec, "nodeName", None)
                if nn:
                    node_ip = nic_util.get_node_ip(nn) or node_ip
                break
        if not src_pod_name:
            # Fallback: try any running pod in the namespace
            all_ns_pods = list_pods(v1, nc_namespace)
            for p in all_ns_pods:
                if p.status.phase == "Running":
                    src_pod_name = p.metadata.name
                    src_pod_ns = p.metadata.namespace
                    break
        if not src_pod_name:
            pytest.fail("No running pod available to exec into after scaling VF to 0 replicas")

        if not nic_util.wait_for_metrics_ready(src_pod_name, src_pod_ns, node_ip, port, timeout=30):
            pytest.fail(f"VF metrics not ready on {node_ip}:{port} after scaling to 0 replicas")

        # Fetch /metrics
        curl_cmd = f"curl -sS --connect-timeout 10 http://{node_ip}:{port}/metrics"
        metrics_text = None
        for attempt in range(1, 4):
            try:
                metrics_text = nic_util.exec_in_pod_sync(src_pod_name, src_pod_ns, curl_cmd, timeout=15)
                if metrics_text and metrics_text.strip():
                    break
            except Exception as e:
                LOG.warning("Attempt %d/3 /metrics fetch failed: %s", attempt, e)
            if attempt < 3:
                time.sleep(2)

        if not metrics_text or not metrics_text.strip():
            pytest.fail("VF: could not fetch /metrics after scaling to 0 replicas")

        # Extract metric names
        found_metric_names = set()
        for line in metrics_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "{" in line:
                mn = line.split("{")[0].strip()
            elif " " in line:
                mn = line.split(" ")[0].strip()
            else:
                continue
            if mn:
                found_metric_names.add(mn)

        LOG.info("VF (zero replicas): found %d unique metric names from /metrics", len(found_metric_names))

        # Check for missing fields (VF: RDMA_ and ETH_ only)
        missing_fields = []
        for field in expected_fields:
            metric_name = re.sub(r'pri_?(\d)', r'pri_\1', field.lower())
            if metric_name not in found_metric_names:
                missing_fields.append(field)

        if missing_fields:
            LOG.error("VF (zero replicas): %d fields missing: %s", len(missing_fields), missing_fields[:10])
            pytest.fail(
                f"VF (zero replicas): {len(missing_fields)} configmap fields missing from metrics: "
                f"{missing_fields[:10]}..."
            )

        LOG.info("VF (zero replicas): all %d RDMA_/ETH_ configmap fields present in metrics", len(expected_fields))

    finally:
        # --- Scale back to original replicas ---
        for dep_name, orig_count in original_replicas.items():
            try:
                LOG.info("VF restoring deployment %s to %d replicas", dep_name, orig_count)
                apps_v1.patch_namespaced_deployment_scale(
                    dep_name, workload_namespace, {"spec": {"replicas": orig_count}}
                )
            except Exception as e:
                LOG.error("Failed to restore deployment %s: %s", dep_name, e)

    # --- Verify workloads are healthy after scale-up ---
    LOG.info("Waiting for VF workload pods to become healthy after scale-up...")
    deadline = time.time() + 120
    healthy = False
    while time.time() < deadline:
        all_pods = list_pods(v1, workload_namespace)
        running_vf = [
            p for p in all_pods
            if p.status.phase == "Running"
            and p.metadata.name.startswith("vf-")
            and any(p.metadata.name.startswith(d) for d in original_replicas)
        ]
        if len(running_vf) >= len(original_replicas):
            # Check all containers are ready
            all_ready = True
            for p in running_vf:
                container_statuses = p.status.container_statuses or []
                if not container_statuses or not all(cs.ready for cs in container_statuses):
                    all_ready = False
                    break
            if all_ready:
                healthy = True
                break
        LOG.info("Waiting for VF pods... %d/%d running", len(running_vf), len(original_replicas))
        time.sleep(5)

    if not healthy:
        pytest.fail(
            f"VF workload pods did not become healthy within 120s after scaling back to original replicas"
        )

    LOG.info("VF: workload pods healthy after scale-up. Test passed.")


@pytest.mark.timeout(TEST_TIMEOUT + 120)
def test_config_nic_json_fields_pf():
    """PF: Create a configmap from config-nic.json, apply it to PF NetworkConfigs and verify:
    - /metrics has all fields from config-nic.json
    - /metrics?debug=qp still exposes QP stats (compared against configmap.yaml QP_ fields)
    """
    nc_namespace = "kube-amd-network"

    # --- Step 2: Read local config-nic.json and parse fields/port ---
    try:
        with open(CONFIG_NIC_JSON_PATH, "r") as f:
            config_json_text = f.read()
        config_json = json.loads(config_json_text)
    except Exception as e:
        pytest.skip(f"Failed to read config-nic.json: {e}")

    nic_fields = (config_json.get("NICConfig") or {}).get("Fields") or []
    config_port = config_json.get("ServerPort") or config_json.get("ServicePort")
    if not nic_fields:
        pytest.skip("No NICConfig.Fields in config-nic.json")
    if not config_port:
        pytest.skip("No ServerPort/ServicePort in config-nic.json")
    config_port = int(config_port)

    LOG.info("config-nic.json: %d NICConfig.Fields, port=%d", len(nic_fields), config_port)

    # --- Step 3: Create the configmap from config-nic.json ---
    config_doc = {
        "metadata": {"name": CONFIG_NIC_CONFIGMAP_NAME_PF, "namespace": nc_namespace},
        "data": {"config.json": config_json_text},
    }
    try:
        nic_util.apply_configmap(config_doc, nc_namespace)
    except Exception as e:
        pytest.skip(f"Failed to create configmap {CONFIG_NIC_CONFIGMAP_NAME_PF}: {e}")

    LOG.info("Created configmap %s from %s", CONFIG_NIC_CONFIGMAP_NAME_PF, CONFIG_NIC_JSON_PATH)

    # --- Step 4: Get running NetworkConfigs ---
    v1 = k8s_client.CoreV1Api()

    try:
        items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    if not items:
        pytest.skip("No NetworkConfig objects found")

    targets = []
    original_config_names: Dict[str, str] = {}
    for it in items:
        name = it.get("metadata", {}).get("name")
        if not name or name.startswith("vf-"):
            continue
        targets.append(name)
        original_config_names[name] = (
            it.get("spec", {}).get("metricsExporter", {}).get("config", {}).get("name")
        )

    if not targets:
        pytest.skip("No eligible PF NetworkConfig objects found")

    # Load QP_ fields from configmap.yaml template for debug=qp comparison
    try:
        all_template_fields = nic_util.load_nic_fields_from_configmap(CONFIGMAP_TEMPLATE_PATH)
    except Exception as e:
        pytest.skip(f"Failed to load configmap.yaml template fields: {e}")

    qp_fields_from_template = [f for f in all_template_fields if f.startswith("QP_")]
    LOG.info("Loaded %d QP_ fields from configmap.yaml for debug=qp comparison", len(qp_fields_from_template))

    workloads = [p for p in list_workloads(v1, namespace="default") if not p.metadata.name.startswith("vf-")]
    if not workloads:
        pytest.skip("No running PF workload pods in default namespace")

    src_pod = workloads[0]

    # Record old exporter pod names to detect restart
    old_pod_names: Dict[str, str] = {}
    op_pods = nic_util.get_operator_pods(namespace=nc_namespace)
    exporter_pods = op_pods.get("metrics-exporter", [])
    for cfg_name in targets:
        for pod in exporter_pods:
            if pod.metadata.name.startswith(cfg_name):
                old_pod_names[cfg_name] = pod.metadata.name
                break

    try:
        # --- Step 5: Update NetworkConfig with this configmap ---
        for name in targets:
            try:
                set_exporter_configmap(nc_namespace, name, CONFIG_NIC_CONFIGMAP_NAME_PF)
            except Exception as e:
                pytest.fail(f"Failed to patch metricsExporter config: {e}")

        time.sleep(15)

        # --- Step 6: Wait for pod restart, then fetch metrics ---
        metrics_text = None
        node_ip = None
        deadline = time.time() + 180
        while time.time() < deadline:
            op_pods = nic_util.get_operator_pods(namespace=nc_namespace)
            exp_pods = op_pods.get("metrics-exporter", [])

            exporter_pod = None
            old_name = old_pod_names.get(targets[0])
            for pod in exp_pods:
                if pod.metadata.name.startswith(targets[0]) and pod.status.phase == "Running":
                    if old_name and pod.metadata.name == old_name:
                        LOG.debug("Skipping old exporter pod %s (waiting for restart)", old_name)
                        continue
                    exporter_pod = pod
                    break

            if not exporter_pod:
                time.sleep(5)
                continue

            LOG.info("Found new exporter pod %s", exporter_pod.metadata.name)
            node_name = getattr(exporter_pod.spec, "node_name", None) or getattr(exporter_pod.spec, "nodeName", None)
            node_ip = nic_util.get_node_ip(node_name) if node_name else None
            if not node_ip:
                time.sleep(5)
                continue

            if not nic_util.wait_for_metrics_ready(src_pod.metadata.name, src_pod.metadata.namespace, node_ip, config_port, timeout=10):
                time.sleep(5)
                continue

            metrics_text = nic_util.pull_metrics(src_pod.metadata.name, src_pod.metadata.namespace, config_port, node_ip)
            if metrics_text:
                break
            time.sleep(5)

        if not metrics_text:
            pytest.skip("Could not pull /metrics")

        # --- Parse metric names from /metrics ---
        metrics_metric_names = set()
        for line in metrics_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "{" in line:
                mn = line.split("{")[0].strip()
            elif " " in line:
                mn = line.split(" ")[0].strip()
            else:
                continue
            if mn:
                metrics_metric_names.add(mn)

        LOG.info("PF /metrics returned %d unique metric names", len(metrics_metric_names))
        sample_names = sorted(metrics_metric_names)[:20]
        LOG.info("Sample actual metric names: %s", sample_names)

        # Look for any metric containing "nic" or "port" to understand naming
        nic_like = sorted([mn for mn in metrics_metric_names if "nic" in mn.lower()])[:10]
        LOG.info("Metrics containing 'nic': %s", nic_like)
        port_like = sorted([mn for mn in metrics_metric_names if "port" in mn.lower()])[:10]
        LOG.info("Metrics containing 'port': %s", port_like)
        qp_like = sorted([mn for mn in metrics_metric_names if "qp" in mn.lower()])[:10]
        LOG.info("Metrics containing 'qp': %s", qp_like)

        # --- Verify /metrics has all fields from config-nic.json ---
        # config-nic.json has MetricsFieldPrefix: "amd_", so most metrics get amd_ prefix.
        # QP_ fields map to lif_qp_*_total; LIF_QP_*_TOTAL already lower to lif_qp_*_total.
        # Try multiple possible metric name formats for each field.
        missing_fields = []
        found_fields = []
        for field in nic_fields:
            metric_name = re.sub(r'pri_?(\d)', r'pri_\1', field.lower())
            # Build set of candidate names to check
            candidates = set()
            if metric_name.startswith("qp_"):
                candidates.add("lif_" + metric_name + "_total")
                candidates.add("amd_lif_" + metric_name + "_total")
            elif metric_name.startswith("lif_qp_"):
                candidates.add(metric_name)
                candidates.add(f"amd_{metric_name}")
            else:
                candidates.add(metric_name)
                candidates.add(f"amd_{metric_name}")
            if candidates & metrics_metric_names:
                found_fields.append(field)
            else:
                missing_fields.append(field)

        LOG.info("PF /metrics: %d/%d config-nic fields found", len(found_fields), len(nic_fields))

        if missing_fields:
            LOG.error("PF missing config-nic fields (%d): %s", len(missing_fields), missing_fields[:10])
            pytest.fail(f"PF /metrics missing {len(missing_fields)} config-nic fields: {missing_fields[:5]}...")

        # --- Verify /metrics?debug=qp has QP stats ---
        found_qp = []
        if qp_fields_from_template:
            qp_curl_cmd = f"curl -sS --connect-timeout 10 'http://{node_ip}:{config_port}/metrics?debug=qp'"
            qp_text = None
            max_retries = 3
            for attempt in range(1, max_retries + 1):
                try:
                    qp_text = nic_util.exec_in_pod_sync(src_pod.metadata.name, src_pod.metadata.namespace, qp_curl_cmd, timeout=15)
                    if qp_text and qp_text.strip():
                        break
                    LOG.debug("Attempt %d/%d: Empty QP debug metrics response", attempt, max_retries)
                except Exception as e:
                    LOG.warning("Attempt %d/%d QP debug fetch failed: %s", attempt, max_retries, e)
                if attempt < max_retries:
                    time.sleep(2)

            if not qp_text or not qp_text.strip():
                pytest.fail("Could not fetch /metrics?debug=qp")

            debug_qp_metric_names = set()
            for line in qp_text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "{" in line:
                    mn = line.split("{")[0].strip()
                elif " " in line:
                    mn = line.split(" ")[0].strip()
                else:
                    continue
                if mn:
                    debug_qp_metric_names.add(mn)

            missing_qp = []
            for field in qp_fields_from_template:
                metric_name = re.sub(r'pri_?(\d)', r'pri_\1', field.lower())
                # QP_ fields map to lif_qp_*_total in the exporter
                if metric_name.startswith("qp_"):
                    metric_name = "lif_" + metric_name + "_total"
                if metric_name in debug_qp_metric_names:
                    found_qp.append(field)
                else:
                    missing_qp.append(field)

            LOG.info(
                "PF /metrics?debug=qp: %d/%d QP fields found (from configmap.yaml)",
                len(found_qp), len(qp_fields_from_template),
            )

            if missing_qp:
                LOG.warning("QP fields not found in /metrics?debug=qp (%d): %s", len(missing_qp), missing_qp[:10])

            if not found_qp:
                pytest.fail(
                    f"None of the {len(qp_fields_from_template)} QP_ fields from configmap.yaml "
                    f"appeared in /metrics?debug=qp"
                )
        else:
            LOG.warning("No QP_ fields in configmap.yaml template; skipping debug=qp check")

        LOG.info(
            "PF config-nic.json test passed: /metrics has all %d config-nic fields, "
            "/metrics?debug=qp has %d QP fields",
            len(found_fields), len(found_qp),
        )

    finally:
        # --- Step 7: Cleanup - restore original configs and delete configmap ---
        for name in targets:
            try:
                if original_config_names[name]:
                    set_exporter_configmap(nc_namespace, name, original_config_names[name])
                else:
                    clear_exporter_configmap(nc_namespace, name)
            except Exception:
                pass
        nic_util.delete_configmap_quietly(CONFIG_NIC_CONFIGMAP_NAME_PF, nc_namespace)


@pytest.mark.timeout(TEST_TIMEOUT + 120)
def test_config_nic_json_fields_vf():
    """VF: Create a configmap from config-nic.json, apply it to VF NetworkConfigs and verify
    /metrics has only ETH_ and RDMA_ stats (no NIC_PORT, NIC_LIF, QP, etc.).
    """
    nc_namespace = "kube-amd-network"

    # --- Step 2: Read local config-nic.json and parse port ---
    try:
        with open(CONFIG_NIC_JSON_PATH, "r") as f:
            config_json_text = f.read()
        config_json = json.loads(config_json_text)
    except Exception as e:
        pytest.skip(f"Failed to read config-nic.json: {e}")

    config_port = config_json.get("ServerPort") or config_json.get("ServicePort")
    if not config_port:
        pytest.skip("No ServerPort/ServicePort in config-nic.json")
    config_port = int(config_port)

    LOG.info("config-nic.json for VF: port=%d", config_port)

    # --- Step 3: Create the configmap from config-nic.json ---
    config_doc = {
        "metadata": {"name": CONFIG_NIC_CONFIGMAP_NAME_VF, "namespace": nc_namespace},
        "data": {"config.json": config_json_text},
    }
    try:
        nic_util.apply_configmap(config_doc, nc_namespace)
    except Exception as e:
        pytest.skip(f"Failed to create configmap {CONFIG_NIC_CONFIGMAP_NAME_VF}: {e}")

    LOG.info("Created configmap %s from %s", CONFIG_NIC_CONFIGMAP_NAME_VF, CONFIG_NIC_JSON_PATH)

    # --- Step 4: Get running NetworkConfigs ---
    v1 = k8s_client.CoreV1Api()

    try:
        items = nic_util.list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources: {e}")

    if not items:
        pytest.skip("No NetworkConfig objects found")

    targets = []
    original_config_names: Dict[str, str] = {}
    for it in items:
        name = it.get("metadata", {}).get("name")
        if not name or not name.startswith("vf-"):
            continue
        targets.append(name)
        original_config_names[name] = (
            it.get("spec", {}).get("metricsExporter", {}).get("config", {}).get("name")
        )

    if not targets:
        pytest.skip("No eligible VF NetworkConfig objects found")

    workloads = [p for p in list_workloads(v1, namespace="default") if p.metadata.name.startswith("vf-")]
    if not workloads:
        pytest.skip("No running VF workload pods in default namespace")

    src_pod = workloads[0]

    # Record old VF exporter pod names to detect restart
    old_pod_names: Dict[str, str] = {}
    op_pods = nic_util.get_operator_pods(namespace=nc_namespace)
    for cfg_name in targets:
        for pod in op_pods.get("vf-metrics-exporter", []):
            if pod.metadata.name.startswith(cfg_name):
                old_pod_names[cfg_name] = pod.metadata.name
                break

    try:
        # --- Step 5: Update NetworkConfig with this configmap ---
        for name in targets:
            try:
                set_exporter_configmap(nc_namespace, name, CONFIG_NIC_CONFIGMAP_NAME_VF)
            except Exception as e:
                pytest.fail(f"Failed to patch metricsExporter config: {e}")

        time.sleep(15)

        # --- Step 6: Wait for pod restart, then fetch metrics ---
        metrics_text = None
        deadline = time.time() + 180
        while time.time() < deadline:
            op_pods = nic_util.get_operator_pods(namespace=nc_namespace)
            vf_exporter_pods = op_pods.get("vf-metrics-exporter", [])

            exporter_pod = None
            old_name = old_pod_names.get(targets[0])
            for pod in vf_exporter_pods:
                if pod.metadata.name.startswith(targets[0]) and pod.status.phase == "Running":
                    if old_name and pod.metadata.name == old_name:
                        LOG.debug("Skipping old VF exporter pod %s (waiting for restart)", old_name)
                        continue
                    exporter_pod = pod
                    break

            if not exporter_pod:
                time.sleep(5)
                continue

            LOG.info("Found new VF exporter pod %s", exporter_pod.metadata.name)
            node_name = getattr(exporter_pod.spec, "node_name", None) or getattr(exporter_pod.spec, "nodeName", None)
            node_ip = nic_util.get_node_ip(node_name) if node_name else None
            if not node_ip:
                time.sleep(5)
                continue

            if not nic_util.wait_for_metrics_ready(src_pod.metadata.name, src_pod.metadata.namespace, node_ip, config_port, timeout=10):
                time.sleep(5)
                continue

            metrics_text = nic_util.pull_metrics(src_pod.metadata.name, src_pod.metadata.namespace, config_port, node_ip)
            if metrics_text:
                break
            time.sleep(5)

        if not metrics_text:
            pytest.skip("Could not pull VF /metrics")

        # --- Parse metric names from /metrics ---
        metrics_metric_names = set()
        for line in metrics_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "{" in line:
                mn = line.split("{")[0].strip()
            elif " " in line:
                mn = line.split(" ")[0].strip()
            else:
                continue
            if mn:
                metrics_metric_names.add(mn)

        # --- Verify VF /metrics has ONLY ETH_ and RDMA_ metrics ---
        # nic_total is a metadata metric (NIC count) that the exporter always exposes;
        # only flag actual per-field NIC stat metrics (nic_port_stats_*, nic_lif_stats_*)
        eth_rdma_metrics = []
        non_eth_rdma_metrics = []
        for mn in metrics_metric_names:
            lower_mn = mn.lower()
            if lower_mn.startswith("amd_"):
                lower_mn = lower_mn[4:]
            if lower_mn.startswith("eth_") or lower_mn.startswith("rdma_"):
                eth_rdma_metrics.append(mn)
            elif (lower_mn.startswith("nic_port_stats_") or lower_mn.startswith("nic_lif_stats_")
                  or lower_mn.startswith("qp_") or lower_mn.startswith("lif_")):
                non_eth_rdma_metrics.append(mn)

        LOG.info(
            "VF /metrics: %d ETH/RDMA metrics, %d non-ETH/RDMA metrics (NIC/QP/LIF)",
            len(eth_rdma_metrics), len(non_eth_rdma_metrics),
        )

        if non_eth_rdma_metrics:
            pytest.fail(
                f"VF /metrics should only have ETH_ and RDMA_ stats but found "
                f"{len(non_eth_rdma_metrics)} unexpected metrics: {non_eth_rdma_metrics[:10]}"
            )

        if not eth_rdma_metrics:
            pytest.fail("VF /metrics has no ETH_ or RDMA_ metrics at all")

        LOG.info(
            "VF config-nic.json test passed: /metrics has %d ETH/RDMA metrics, "
            "no NIC/QP/LIF metrics as expected",
            len(eth_rdma_metrics),
        )

    finally:
        # --- Step 7: Cleanup - restore original configs and delete configmap ---
        for name in targets:
            try:
                if original_config_names[name]:
                    set_exporter_configmap(nc_namespace, name, original_config_names[name])
                else:
                    clear_exporter_configmap(nc_namespace, name)
            except Exception:
                pass
        nic_util.delete_configmap_quietly(CONFIG_NIC_CONFIGMAP_NAME_VF, nc_namespace)
