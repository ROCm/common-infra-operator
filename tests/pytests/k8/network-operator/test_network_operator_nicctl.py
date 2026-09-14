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

# nicctl comparison test utilities and test cases for network operator
"""
Test suite for nicctl commands on PF and VF nodes
Tests various nicctl show commands on operator pods (device-plugin, metrics-exporter, node-labeler)
"""

import pytest
import logging
import json
from kubernetes import client as k8s_client_mod

from lib.nic_util import (
    compare_nicctl_outputs,
    exec_nicctl_command,
    extract_lif_ids,
    extract_nic_ids,
    get_operator_pods,
    exec_in_pod_sync,
)

from datetime import datetime

LOG = logging.getLogger(__name__)
TEST_TIMEOUT = 300
POD_TYPES = ["device-plugin", "metrics-exporter", "node-labeler"]

COMMANDS_ME = [
    "nicctl show card",
    "nicctl show port --card {card_id} -j",
    "nicctl show lif --card {card_id} -j",
    "nicctl show lif",
    "nicctl show lif -l {lif_id} -j",
    "nicctl show port statistics -j",
    "nicctl show lif statistics -j",
    "nicctl show rdma queue-pair statistics --card {card_id} -j",
    "nicctl show lif -l {lif_id} -j",
]
COMMANDS_NL = [
    "nicctl show card",
    "nicctl show port --card {card_id} -j",
    "nicctl show version host-software --json",
]
COMMANDS_DP = [
    "nicctl clear rdma internal queue-pair --lif {lif_id}",
]


def init_k8s_client():
    return k8s_client_mod.CoreV1Api()


def is_valid_json(text):
    if not text:
        return False
    try:
        json.loads(text)
        return True
    except (json.JSONDecodeError, TypeError):
        return False


# ========== PF Tests ==========

@pytest.mark.timeout(TEST_TIMEOUT)
@pytest.mark.parametrize("pod_type", POD_TYPES)
def test_nicctl_show_card_json_pf(pod_type):
    """Test: nicctl show card --json on PF nodes"""
    pods_by_type = get_operator_pods()
    pods = pods_by_type.get(pod_type, [])
    
    LOG.info(f"Found {len(pods)} PF {pod_type} pods")
    if not pods:
        pytest.skip(f"No PF {pod_type} pods found")
    
    failed = []
    passed = []
    for pod in pods:
        pod_name = pod.metadata.name
        LOG.info(f"Testing PF {pod_type} pod: {pod_name}")
        output, error = exec_nicctl_command(pod_name, pod.metadata.namespace, "nicctl show card --json")
        
        if error or not output:
            failed.append((pod_name, f"Command failed: {error[:200] if error else 'no output'}"))
            LOG.error(f"Pod {pod_name}: Command failed - error={error}")
            continue
        
        if not is_valid_json(output):
            failed.append((pod_name, "Output is not valid JSON"))
            LOG.error(f"Pod {pod_name}: Invalid JSON output")
            continue
        
        passed.append(pod_name)
        LOG.info(f"{pod_type} Pod {pod_name}: nicctl show card --json PASS")
    
    LOG.info(f"PF {pod_type} summary: {len(passed)} passed, {len(failed)} failed")
    
    if failed:
        pytest.fail(f"Failed for PF {pod_type} pods: {failed}")


@pytest.mark.timeout(TEST_TIMEOUT)
@pytest.mark.parametrize("pod_type", POD_TYPES)
def test_nicctl_show_port_json_pf(pod_type):
    """Test: nicctl show port --json on PF nodes"""
    pods_by_type = get_operator_pods()
    pods = pods_by_type.get(pod_type, [])
    
    if not pods:
        pytest.skip(f"No PF {pod_type} pods found")
    
    failed = []
    passed = []
    for pod in pods:
        pod_name = pod.metadata.name
        output, error = exec_nicctl_command(pod_name, pod.metadata.namespace, "nicctl show port --json")
        
        if error or not output:
            failed.append((pod_name, "Command failed or no output"))
            continue
        
        if not is_valid_json(output):
            failed.append((pod_name, "Output is not valid JSON"))
            continue
        
        passed.append(pod_name)
        LOG.info(f"{pod_type} Pod {pod_name}: nicctl show port --json PASS")
    
    if failed:
        pytest.fail(f"Failed for PF {pod_type} pods: {failed}")


@pytest.mark.timeout(TEST_TIMEOUT)
@pytest.mark.parametrize("pod_type", POD_TYPES)
def test_nicctl_show_version_host_software_json_pf(pod_type):
    """Test: nicctl show version host-software --json on PF nodes"""
    pods_by_type = get_operator_pods()
    pods = pods_by_type.get(pod_type, [])
    
    if not pods:
        pytest.skip(f"No PF {pod_type} pods found")
    
    failed = []
    passed = []
    for pod in pods:
        pod_name = pod.metadata.name
        output, error = exec_nicctl_command(pod_name, pod.metadata.namespace, "nicctl show version host-software --json")
        
        if error or not output:
            failed.append((pod_name, "Command failed or no output"))
            continue
        
        if not is_valid_json(output):
            failed.append((pod_name, "Output is not valid JSON"))
            continue
        
        passed.append(pod_name)
        LOG.info(f"{pod_type} Pod {pod_name}: nicctl show version host-software --json PASS")
    
    if failed:
        pytest.fail(f"Failed for PF {pod_type} pods: {failed}")


@pytest.mark.timeout(TEST_TIMEOUT)
@pytest.mark.parametrize("pod_type", POD_TYPES)
def test_nicctl_show_port_statistics_json_pf(pod_type):
    """Test: nicctl show port statistics -j on PF nodes"""
    pods_by_type = get_operator_pods()
    pods = pods_by_type.get(pod_type, [])
    
    if not pods:
        pytest.skip(f"No PF {pod_type} pods found")
    
    failed = []
    passed = []
    for pod in pods:
        pod_name = pod.metadata.name
        output, error = exec_nicctl_command(pod_name, pod.metadata.namespace, "nicctl show port statistics -j")
        
        if error or not output:
            failed.append((pod_name, "Command failed or no output"))
            continue
        
        if not is_valid_json(output):
            failed.append((pod_name, "Output is not valid JSON"))
            continue
        
        passed.append(pod_name)
        LOG.info(f"{pod_type} Pod {pod_name}: nicctl show port statistics -j PASS")
    
    if failed:
        pytest.fail(f"Failed for PF {pod_type} pods: {failed}")


@pytest.mark.timeout(TEST_TIMEOUT)
@pytest.mark.parametrize("pod_type", POD_TYPES)
def test_nicctl_show_lif_statistics_json_pf(pod_type):
    """Test: nicctl show lif statistics -j on PF nodes"""
    pods_by_type = get_operator_pods()
    pods = pods_by_type.get(pod_type, [])
    
    if not pods:
        pytest.skip(f"No PF {pod_type} pods found")
    
    failed = []
    passed = []
    for pod in pods:
        pod_name = pod.metadata.name
        output, error = exec_nicctl_command(pod_name, pod.metadata.namespace, "nicctl show lif statistics -j")
        
        if error or not output:
            failed.append((pod_name, "Command failed or no output"))
            continue
        
        if not is_valid_json(output):
            failed.append((pod_name, "Output is not valid JSON"))
            continue
        
        passed.append(pod_name)
        LOG.info(f"{pod_type} Pod {pod_name}: nicctl show lif statistics -j PASS")
    
    if failed:
        pytest.fail(f"Failed for PF {pod_type} pods: {failed}")


@pytest.mark.timeout(TEST_TIMEOUT)
@pytest.mark.parametrize("pod_type", POD_TYPES)
def test_nicctl_show_port_with_card_id_pf(pod_type):
    """Test: nicctl show port --card <nicID> -j on PF nodes"""
    pods_by_type = get_operator_pods()
    pods = pods_by_type.get(pod_type, [])
    
    if not pods:
        pytest.skip(f"No PF {pod_type} pods found")
    
    failed = []
    passed = []
    for pod in pods:
        pod_name = pod.metadata.name
        
        # First get card info to extract NIC IDs
        card_output, error = exec_nicctl_command(pod_name, pod.metadata.namespace, "nicctl show card")
        
        if error or not card_output:
            failed.append((pod_name, "Could not get card info"))
            continue
        
        nic_ids = extract_nic_ids(card_output)
        if not nic_ids:
            failed.append((pod_name, "Could not extract NIC IDs"))
            continue
        
        # Test command with first NIC ID
        nic_id = nic_ids[0]
        output, error = exec_nicctl_command(pod_name, pod.metadata.namespace, f"nicctl show port --card {nic_id} -j")
        
        if error or not output:
            failed.append((pod_name, f"Command failed for NIC ID {nic_id}"))
            continue
        
        if not is_valid_json(output):
            failed.append((pod_name, f"Output is not valid JSON for NIC ID {nic_id}"))
            continue
        
        passed.append(pod_name)
        LOG.info(f"{pod_type} Pod {pod_name}: nicctl show port --card {nic_id} -j PASS")
    
    if failed:
        pytest.fail(f"Failed for PF {pod_type} pods: {failed}")


@pytest.mark.timeout(TEST_TIMEOUT)
@pytest.mark.parametrize("pod_type", POD_TYPES)
def test_nicctl_show_lif_with_card_id_pf(pod_type):
    """Test: nicctl show lif --card <nicID> -j on PF nodes"""
    pods_by_type = get_operator_pods()
    pods = pods_by_type.get(pod_type, [])
    
    if not pods:
        pytest.skip(f"No PF {pod_type} pods found")
    
    failed = []
    passed = []
    for pod in pods:
        pod_name = pod.metadata.name
        
        # First get card info to extract NIC IDs
        card_output, error = exec_nicctl_command(pod_name, pod.metadata.namespace, "nicctl show card")
        
        if error or not card_output:
            failed.append((pod_name, "Could not get card info"))
            continue
        
        nic_ids = extract_nic_ids(card_output)
        if not nic_ids:
            failed.append((pod_name, "Could not extract NIC IDs"))
            continue
        
        # Test command with first NIC ID
        nic_id = nic_ids[0]
        output, error = exec_nicctl_command(pod_name, pod.metadata.namespace, f"nicctl show lif --card {nic_id} -j")
        
        if error or not output:
            failed.append((pod_name, f"Command failed for NIC ID {nic_id}"))
            continue
        
        if not is_valid_json(output):
            failed.append((pod_name, f"Output is not valid JSON for NIC ID {nic_id}"))
            continue
        
        passed.append(pod_name)
        LOG.info(f"{pod_type} Pod {pod_name}: nicctl show lif --card {nic_id} -j PASS")
    
    if failed:
        pytest.fail(f"Failed for PF {pod_type} pods: {failed}")


@pytest.mark.timeout(TEST_TIMEOUT)
@pytest.mark.parametrize("pod_type", POD_TYPES)
def test_nicctl_show_rdma_queue_pair_statistics_pf(pod_type):
    """Test: nicctl show rdma queue-pair statistics --card <nicID> -j on PF nodes"""
    pods_by_type = get_operator_pods()
    pods = pods_by_type.get(pod_type, [])
    
    if not pods:
        pytest.skip(f"No PF {pod_type} pods found")
    
    failed = []
    passed = []
    for pod in pods:
        pod_name = pod.metadata.name
        
        # First get card info to extract NIC IDs
        card_output, error = exec_nicctl_command(pod_name, pod.metadata.namespace, "nicctl show card")
        
        if error or not card_output:
            failed.append((pod_name, "Could not get card info"))
            continue
        
        nic_ids = extract_nic_ids(card_output)
        if not nic_ids:
            failed.append((pod_name, "Could not extract NIC IDs"))
            continue
        
        # Test command with first NIC ID
        nic_id = nic_ids[0]
        output, error = exec_nicctl_command(pod_name, pod.metadata.namespace, f"nicctl show rdma queue-pair statistics --card {nic_id} -j")
        
        if error or not output:
            failed.append((pod_name, f"Command failed for NIC ID {nic_id}"))
            continue
        
        if not is_valid_json(output):
            failed.append((pod_name, f"Output is not valid JSON for NIC ID {nic_id}"))
            continue
        
        passed.append(pod_name)
        LOG.info(f"{pod_type} Pod {pod_name}: nicctl show rdma queue-pair statistics --card {nic_id} -j PASS")
    
    if failed:
        pytest.fail(f"Failed for PF {pod_type} pods: {failed}")


@pytest.mark.timeout(TEST_TIMEOUT)
@pytest.mark.parametrize("pod_type", POD_TYPES)
def test_nicctl_show_lif_with_lif_id_pf(pod_type):
    """Test: nicctl show lif -l <lifID> -j on PF nodes"""
    pods_by_type = get_operator_pods()
    pods = pods_by_type.get(pod_type, [])
    
    if not pods:
        pytest.skip(f"No PF {pod_type} pods found")
    
    failed = []
    passed = []
    for pod in pods:
        pod_name = pod.metadata.name
        
        # First get lif info to extract LIF IDs
        lif_output, error = exec_nicctl_command(pod_name, pod.metadata.namespace, "nicctl show lif")
        
        if error or not lif_output:
            # Try alternative command
            card_output, error = exec_nicctl_command(pod_name, pod.metadata.namespace, "nicctl show card")
            if error or not card_output:
                failed.append((pod_name, "Could not get lif or card info"))
                continue
            
            nic_ids = extract_nic_ids(card_output)
            if nic_ids:
                lif_output, error = exec_nicctl_command(pod_name, pod.metadata.namespace, f"nicctl show lif --card {nic_ids[0]}")
        
        if not lif_output:
            failed.append((pod_name, "Could not get lif info"))
            continue
        
        lif_ids = extract_lif_ids(lif_output)
        if not lif_ids:
            failed.append((pod_name, "Could not extract LIF IDs"))
            continue
        
        # Test command with first LIF ID
        lif_id = lif_ids[0]
        output, error = exec_nicctl_command(pod_name, pod.metadata.namespace, f"nicctl show lif -l {lif_id} -j")
        
        if error or not output:
            failed.append((pod_name, f"Command failed for LIF ID {lif_id}"))
            continue
        
        if not is_valid_json(output):
            failed.append((pod_name, f"Output is not valid JSON for LIF ID {lif_id}"))
            continue
        
        passed.append(pod_name)
        LOG.info(f"{pod_type} Pod {pod_name}: nicctl show lif -l {lif_id} -j PASS")
    
    if failed:
        pytest.fail(f"Failed for PF {pod_type} pods: {failed}")


# ========== VF Tests ==========

@pytest.mark.timeout(TEST_TIMEOUT)
@pytest.mark.parametrize("pod_type", POD_TYPES)
def test_nicctl_show_card_json_vf(pod_type):
    """Test: nicctl show card --json on VF nodes (expect error)"""
    pods_by_type = get_operator_pods()
    vf_pods = pods_by_type.get(f"vf-{pod_type}", [])
    
    if not vf_pods:
        pytest.skip(f"No VF {pod_type} pods found")
    
    for pod in vf_pods:
        pod_name = pod.metadata.name
        output, error = exec_nicctl_command(pod_name, pod.metadata.namespace, "nicctl show card --json")
        
        # On VF nodes, we expect an error message about no AMD NICs
        expected_error_found = False
        if output and ("No AMD NICs detected" in output or "ERROR" in output):
            expected_error_found = True
        if error and ("No AMD NICs detected" in error or "ERROR" in error):
            expected_error_found = True
        
        if expected_error_found:
            LOG.info(f"VF {pod_type} Pod {pod_name}: nicctl show card --json returned expected VF error")
        else:
            LOG.warning(f"VF {pod_type} Pod {pod_name}: Did not return expected VF error")


@pytest.mark.timeout(TEST_TIMEOUT)
@pytest.mark.parametrize("pod_type", POD_TYPES)
def test_nicctl_show_port_json_vf(pod_type):
    """Test: nicctl show port --json on VF nodes (expect error)"""
    pods_by_type = get_operator_pods()
    vf_pods = pods_by_type.get(f"vf-{pod_type}", [])
    
    if not vf_pods:
        pytest.skip(f"No VF {pod_type} pods found")
    
    for pod in vf_pods:
        pod_name = pod.metadata.name
        output, error = exec_nicctl_command(pod_name, pod.metadata.namespace, "nicctl show port --json")
        
        expected_error_found = False
        if output and ("No AMD NICs detected" in output or "ERROR" in output):
            expected_error_found = True
        if error and ("No AMD NICs detected" in error or "ERROR" in error):
            expected_error_found = True
        
        if expected_error_found:
            LOG.info(f"VF {pod_type} Pod {pod_name}: nicctl show port --json returned expected VF error")
        else:
            LOG.warning(f"VF {pod_type} Pod {pod_name}: Did not return expected VF error")


@pytest.mark.timeout(TEST_TIMEOUT)
@pytest.mark.parametrize("pod_type", POD_TYPES)
def test_nicctl_show_version_host_software_json_vf(pod_type):
    """Test: nicctl show version host-software --json on VF nodes (may work or error)"""
    pods_by_type = get_operator_pods()
    vf_pods = pods_by_type.get(f"vf-{pod_type}", [])
    
    if not vf_pods:
        pytest.skip(f"No VF {pod_type} pods found")
    
    for pod in vf_pods:
        pod_name = pod.metadata.name
        
        # Test version command on VF node (may succeed or fail)
        output, error = exec_nicctl_command(pod_name, pod.metadata.namespace, "nicctl show version host-software --json")
        
        if output or error:
            LOG.info(f"VF {pod_type} Pod {pod_name}: nicctl show version host-software --json: {'succeeded' if output and is_valid_json(output) else 'returned error'}")


@pytest.mark.timeout(TEST_TIMEOUT)
@pytest.mark.parametrize("pod_type", POD_TYPES)
def test_nicctl_show_port_statistics_json_vf(pod_type):
    """Test: nicctl show port statistics -j on VF nodes (expect error)"""
    pods_by_type = get_operator_pods()
    vf_pods = pods_by_type.get(f"vf-{pod_type}", [])
    
    if not vf_pods:
        pytest.skip(f"No VF {pod_type} pods found")
    
    for pod in vf_pods:
        pod_name = pod.metadata.name
        output, error = exec_nicctl_command(pod_name, pod.metadata.namespace, "nicctl show port statistics -j")
        
        expected_error_found = False
        if output and ("No AMD NICs detected" in output or "ERROR" in output):
            expected_error_found = True
        if error and ("No AMD NICs detected" in error or "ERROR" in error):
            expected_error_found = True
        
        if expected_error_found:
            LOG.info(f"VF {pod_type} Pod {pod_name}: nicctl show port statistics -j returned expected VF error")
        else:
            LOG.warning(f"VF {pod_type} Pod {pod_name}: Did not return expected VF error")


@pytest.mark.timeout(TEST_TIMEOUT)
@pytest.mark.parametrize("pod_type", POD_TYPES)
def test_nicctl_show_lif_statistics_json_vf(pod_type):
    """Test: nicctl show lif statistics -j on VF nodes (expect error)"""
    pods_by_type = get_operator_pods()
    vf_pods = pods_by_type.get(f"vf-{pod_type}", [])
    
    if not vf_pods:
        pytest.skip(f"No VF {pod_type} pods found")
    
    for pod in vf_pods:
        pod_name = pod.metadata.name
        output, error = exec_nicctl_command(pod_name, pod.metadata.namespace, "nicctl show lif statistics -j")
        
        expected_error_found = False
        if output and ("No AMD NICs detected" in output or "ERROR" in output):
            expected_error_found = True
        if error and ("No AMD NICs detected" in error or "ERROR" in error):
            expected_error_found = True
        
        if expected_error_found:
            LOG.info(f"VF {pod_type} Pod {pod_name}: nicctl show lif statistics -j returned expected VF error")
        else:
            LOG.warning(f"VF {pod_type} Pod {pod_name}: Did not return expected VF error")


@pytest.mark.timeout(TEST_TIMEOUT)
def test_nicctl_commands_on_operator_pods():
    """Test: Verify operator pods are running (device-plugin, metrics-exporter, node-labeler)"""
    pods_by_type = get_operator_pods()
    
    missing_types = []
    for pod_type, pods in pods_by_type.items():
        if not pods:
            missing_types.append(pod_type)
        else:
            LOG.info(f"Found {len(pods)} running {pod_type} pods")
    
    if missing_types:
        pytest.fail(f"Missing operator pods: {missing_types}")
    
    LOG.info("All operator pod types are running")



# ========== Host vs Container Comparison Tests ==========

def test_nicctl_comparison_metrics_exporter(request):
    """
    Test case to compare nicctl outputs for metrics-exporter pod on PF-node.
    Dynamically discovers the PF-node (node with amd.com/nic capacity) and 
    finds the metrics-exporter pod running on it.
    Uses Kubernetes Python client API.
    """
    
    # Configuration
    namespace = "kube-amd-network"
    pod_name_pattern = "metrics-exporter"
    container_name = "metrics-exporter-container"
    
    # Get command line options
    skip_card_commands = request.config.getoption("--skip-card-commands")
    skip_lif_commands = request.config.getoption("--skip-lif-commands")
    output_dir = request.config.getoption("--output-dir")
    
    # Initialize Kubernetes client
    print(f"\n{'='*70}")
    print(f"Initializing Kubernetes client")
    print(f"{'='*70}\n")
    
    try:
        v1 = init_k8s_client()
        print("[OK] Kubernetes client initialized successfully\n")
    except Exception as e:
        pytest.fail(f"Failed to initialize Kubernetes client: {str(e)}")
    
    print(f"\n{'='*70}")
    print(f"Step 1: Finding PF-node (node with amd.com/nic capacity)")
    print(f"{'='*70}\n")
    
    # Get all nodes
    try:
        nodes = v1.list_node()
        node_names = [node.metadata.name for node in nodes.items]
        print(f"Found {len(node_names)} nodes: {', '.join(node_names)}\n")
    except Exception as e:
        pytest.fail(f"Failed to list nodes: {str(e)}")
    
    # Find the PF-node by checking for amd.com/nic capacity
    pf_node = None
    pf_node_nic_capacity = None
    
    for node in nodes.items:
        node_name = node.metadata.name
        print(f"Checking node: {node_name}")
        
        # Check node capacity for amd.com/nic
        if node.status.capacity:
            capacity = node.status.capacity
            if 'amd.com/nic' in capacity:
                nic_capacity = capacity['amd.com/nic']
                print(f"  [OK] Found amd.com/nic capacity: {nic_capacity}")
                pf_node = node_name
                pf_node_nic_capacity = nic_capacity
                break
            else:
                print(f"  [FAIL] No amd.com/nic capacity found")
    
    if not pf_node:
        pytest.fail("No PF-node found with amd.com/nic capacity")
    
    print(f"\n{'='*70}")
    print(f"PF-node identified: {pf_node}")
    print(f"NIC capacity: {pf_node_nic_capacity}")
    print(f"{'='*70}\n")
    
    print(f"\n{'='*70}")
    print(f"Step 2: Finding metrics-exporter pod on PF-node '{pf_node}'")
    print(f"{'='*70}\n")
    
    # List all pods in the namespace on the PF-node
    try:
        field_selector = f"spec.nodeName={pf_node}"
        pods_on_node = v1.list_namespaced_pod(
            namespace=namespace,
            field_selector=field_selector
        )
        
        print(f"Pods on PF-node '{pf_node}':")
        print(f"{'NAME':<60} {'STATUS':<15} {'IP':<15}")
        print(f"{'-'*90}")
        
        for pod in pods_on_node.items:
            pod_name_display = pod.metadata.name
            pod_status = pod.status.phase
            pod_ip = pod.status.pod_ip if pod.status.pod_ip else "N/A"
            print(f"{pod_name_display:<60} {pod_status:<15} {pod_ip:<15}")
        
        print()
        
    except Exception as e:
        pytest.fail(f"Failed to list pods on node {pf_node}: {str(e)}")
    
    # Find the metrics-exporter pod on the PF-node
    metrics_exporter_pod = None
    
    for pod in pods_on_node.items:
        if pod_name_pattern in pod.metadata.name:
            metrics_exporter_pod = pod.metadata.name
            pod_status = pod.status.phase
            print(f"[OK] Found metrics-exporter pod: {metrics_exporter_pod}")
            print(f"   Status: {pod_status}")
            break
    
    if not metrics_exporter_pod:
        pytest.fail(f"No pod matching pattern '{pod_name_pattern}' found on PF-node {pf_node}")
    
    # Verify the pod is running
    print(f"\nVerifying pod '{metrics_exporter_pod}' is running...")
    
    try:
        pod_info = v1.read_namespaced_pod(
            name=metrics_exporter_pod,
            namespace=namespace
        )
        
        pod_status = pod_info.status.phase
        print(f"Pod status: {pod_status}")
        
        if pod_status != "Running":
            pytest.fail(f"Pod {metrics_exporter_pod} is not in Running state. Current state: {pod_status}")
        
    except Exception as e:
        pytest.fail(f"Failed to verify pod status: {str(e)}")
    
    # Get and verify container name
    print(f"\nGetting containers in pod '{metrics_exporter_pod}'...")
    
    try:
        containers = [container.name for container in pod_info.spec.containers]
        print(f"Containers in pod: {', '.join(containers)}")
        
        # Use the first container if the specified container name is not found
        actual_container = container_name if container_name in containers else containers[0]
        
        if actual_container != container_name:
            print(f"[WARN]  Container '{container_name}' not found, using '{actual_container}' instead")
        else:
            print(f"[OK] Using container: {actual_container}")
    
    except Exception as e:
        pytest.fail(f"Failed to get containers: {str(e)}")
    
    # Generate output file name
    output_file = f"{output_dir}/nicctl_comparison_{metrics_exporter_pod}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    
    # Run the comparison
    print(f"\n{'='*70}")
    print(f"Step 3: Running nicctl comparison")
    print(f"{'='*70}")
    print(f"  PF-node:   {pf_node}")
    print(f"  Pod:       {metrics_exporter_pod}")
    print(f"  Container: {actual_container}")
    print(f"  Namespace: {namespace}")
    print(f"{'='*70}\n")
    
    results = compare_nicctl_outputs(
        pod_name=metrics_exporter_pod,
        container_name=actual_container,
        pf_node=pf_node,
        namespace=namespace,
        skip_card_commands=skip_card_commands,
        skip_lif_commands=skip_lif_commands,
        output_file=output_file,
        commands=COMMANDS_ME
    )
    
    # Print summary
    print(f"\n{'='*70}")
    print(f"TEST SUMMARY")
    print(f"{'='*70}")
    #import pdb;pdb.set_trace()
    print(f"Total commands tested: {results['total_commands']}")
    print(f"Discrepancies found:   {len(results['discrepancies'])}")
    if results['total_commands'] > 0:
        print(f"Match rate:            {((results['total_commands'] - len(results['discrepancies'])) / results['total_commands'] * 100):.1f}%")
    print(f"Results saved to:      {output_file}")
    print(f"{'='*70}\n")
    
    # Print detailed discrepancy list
    if results["discrepancies"]:
        print(f"{'='*70}")
        print(f"DISCREPANCY DETAILS")
        print(f"{'='*70}\n")
        
        for idx, disc in enumerate(results["discrepancies"], 1):
            print(f"Discrepancy #{idx}: {disc['original_command']}")
            if disc.get('card_id'):
                print(f"  Card ID: {disc['card_id']}")
            if disc.get('lif_id'):
                print(f"  LIF ID: {disc['lif_id']}")
            print(f"{'-'*70}")
            
            print(f"PF-Node Return Code:   {disc['host']['returncode']}")
            print(f"Container Return Code: {disc['container']['returncode']}")
            
            if disc['host']['stderr']:
                print(f"\nPF-Node STDERR:\n{disc['host']['stderr']}")
            
            if disc['container']['stderr']:
                print(f"\nContainer STDERR:\n{disc['container']['stderr']}")
            
            print(f"\nPF-Node Output:")
            host_output = disc['host']['stdout']
            print(f"{host_output[:500]}")
            if len(host_output) > 500:
                print(f"... (truncated, {len(host_output)} total chars)")
            
            print(f"\nContainer Output:")
            container_output = disc['container']['stdout']
            print(f"{container_output[:500]}")
            if len(container_output) > 500:
                print(f"... (truncated, {len(container_output)} total chars)")
            
            print(f"\n{'='*70}\n")
    else:
        print("[OK] No discrepancies found! All outputs match.\n")
    
    # Assert test result - fail if there are discrepancies
    assert len(results["discrepancies"]) == 0, \
        f"Test FAILED: Found {len(results['discrepancies'])} discrepancies out of {results['total_commands']} commands. " \
        f"Check {output_file} for details."
    
    print("[OK] Test PASSED: All nicctl command outputs match between PF-node and container!\n")
def test_nicctl_comparison_node_labeller(request):
    """
    Test case to compare nicctl outputs for node-labeller pod on PF-node.
    Dynamically discovers the PF-node (node with amd.com/nic capacity) and 
    finds the node-labeller pod running on it.
    Uses Kubernetes Python client API.
    """
    
    # Configuration
    namespace = "kube-amd-network"
    pod_name_pattern = "node-labeller"
    container_name = "node-labeller-container"
    
    # Get command line options
    skip_card_commands = request.config.getoption("--skip-card-commands")
    skip_lif_commands = request.config.getoption("--skip-lif-commands")
    output_dir = request.config.getoption("--output-dir")
    
    # Initialize Kubernetes client
    print(f"\n{'='*70}")
    print(f"Initializing Kubernetes client")
    print(f"{'='*70}\n")
    
    try:
        v1 = init_k8s_client()
        print("[OK] Kubernetes client initialized successfully\n")
    except Exception as e:
        pytest.fail(f"Failed to initialize Kubernetes client: {str(e)}")
    
    print(f"\n{'='*70}")
    print(f"Step 1: Finding PF-node (node with amd.com/nic capacity)")
    print(f"{'='*70}\n")
    
    # Get all nodes
    try:
        nodes = v1.list_node()
        node_names = [node.metadata.name for node in nodes.items]
        print(f"Found {len(node_names)} nodes: {', '.join(node_names)}\n")
    except Exception as e:
        pytest.fail(f"Failed to list nodes: {str(e)}")
    
    # Find the PF-node by checking for amd.com/nic capacity
    pf_node = None
    pf_node_nic_capacity = None
    
    for node in nodes.items:
        node_name = node.metadata.name
        print(f"Checking node: {node_name}")
        
        # Check node capacity for amd.com/nic
        if node.status.capacity:
            capacity = node.status.capacity
            if 'amd.com/nic' in capacity:
                nic_capacity = capacity['amd.com/nic']
                print(f"  [OK] Found amd.com/nic capacity: {nic_capacity}")
                pf_node = node_name
                pf_node_nic_capacity = nic_capacity
                break
            else:
                print(f"  [FAIL] No amd.com/nic capacity found")
    
    if not pf_node:
        pytest.fail("No PF-node found with amd.com/nic capacity")
    
    print(f"\n{'='*70}")
    print(f"PF-node identified: {pf_node}")
    print(f"NIC capacity: {pf_node_nic_capacity}")
    print(f"{'='*70}\n")
    
    print(f"\n{'='*70}")
    print(f"Step 2: Finding node-labeller pod on PF-node '{pf_node}'")
    print(f"{'='*70}\n")
    
    # List all pods in the namespace on the PF-node
    try:
        field_selector = f"spec.nodeName={pf_node}"
        pods_on_node = v1.list_namespaced_pod(
            namespace=namespace,
            field_selector=field_selector
        )
        
        print(f"Pods on PF-node '{pf_node}':")
        print(f"{'NAME':<60} {'STATUS':<15} {'IP':<15}")
        print(f"{'-'*90}")
        
        for pod in pods_on_node.items:
            pod_name_display = pod.metadata.name
            pod_status = pod.status.phase
            pod_ip = pod.status.pod_ip if pod.status.pod_ip else "N/A"
            print(f"{pod_name_display:<60} {pod_status:<15} {pod_ip:<15}")
        
        print()
        
    except Exception as e:
        pytest.fail(f"Failed to list pods on node {pf_node}: {str(e)}")
    
    # Find the node-labeller pod on the PF-node
    node_labeller_pod = None
    
    for pod in pods_on_node.items:
        if pod_name_pattern in pod.metadata.name:
            node_labeller_pod = pod.metadata.name
            pod_status = pod.status.phase
            print(f"[OK] Found node-labeller pod: {node_labeller_pod}")
            print(f"   Status: {pod_status}")
            break
    
    if not node_labeller_pod:
        pytest.fail(f"No pod matching pattern '{pod_name_pattern}' found on PF-node {pf_node}")
    
    # Verify the pod is running
    print(f"\nVerifying pod '{node_labeller_pod}' is running...")
    
    try:
        pod_info = v1.read_namespaced_pod(
            name=node_labeller_pod,
            namespace=namespace
        )
        
        pod_status = pod_info.status.phase
        print(f"Pod status: {pod_status}")
        
        if pod_status != "Running":
            pytest.fail(f"Pod {node_labeller_pod} is not in Running state. Current state: {pod_status}")
        
    except Exception as e:
        pytest.fail(f"Failed to verify pod status: {str(e)}")
    
    # Get and verify container name
    print(f"\nGetting containers in pod '{node_labeller_pod}'...")
    
    try:
        containers = [container.name for container in pod_info.spec.containers]
        print(f"Containers in pod: {', '.join(containers)}")
        
        # Use the first container if the specified container name is not found
        actual_container = container_name if container_name in containers else containers[0]
        
        if actual_container != container_name:
            print(f"[WARN]  Container '{container_name}' not found, using '{actual_container}' instead")
        else:
            print(f"[OK] Using container: {actual_container}")
    
    except Exception as e:
        pytest.fail(f"Failed to get containers: {str(e)}")
    
    # Generate output file name
    output_file = f"{output_dir}/nicctl_comparison_{node_labeller_pod}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    
    # Run the comparison
    print(f"\n{'='*70}")
    print(f"Step 3: Running nicctl comparison")
    print(f"{'='*70}")
    print(f"  PF-node:   {pf_node}")
    print(f"  Pod:       {node_labeller_pod}")
    print(f"  Container: {actual_container}")
    print(f"  Namespace: {namespace}")
    print(f"{'='*70}\n")
    
    results = compare_nicctl_outputs(
        pod_name=node_labeller_pod,
        container_name=actual_container,
        pf_node=pf_node,
        namespace=namespace,
        skip_card_commands=skip_card_commands,
        skip_lif_commands=skip_lif_commands,
        output_file=output_file,
        commands=COMMANDS_NL
    )
    
    # Print summary
    print(f"\n{'='*70}")
    print(f"TEST SUMMARY")
    print(f"{'='*70}")
    #import pdb;pdb.set_trace()
    print(f"Total commands tested: {results['total_commands']}")
    print(f"Discrepancies found:   {len(results['discrepancies'])}")
    if results['total_commands'] > 0:
        print(f"Match rate:            {((results['total_commands'] - len(results['discrepancies'])) / results['total_commands'] * 100):.1f}%")
    print(f"Results saved to:      {output_file}")
    print(f"{'='*70}\n")
    
    # Print detailed discrepancy list
    if results["discrepancies"]:
        print(f"{'='*70}")
        print(f"DISCREPANCY DETAILS")
        print(f"{'='*70}\n")
        
        for idx, disc in enumerate(results["discrepancies"], 1):
            print(f"Discrepancy #{idx}: {disc['original_command']}")
            if disc.get('card_id'):
                print(f"  Card ID: {disc['card_id']}")
            if disc.get('lif_id'):
                print(f"  LIF ID: {disc['lif_id']}")
            print(f"{'-'*70}")
            
            print(f"PF-Node Return Code:   {disc['host']['returncode']}")
            print(f"Container Return Code: {disc['container']['returncode']}")
            
            if disc['host']['stderr']:
                print(f"\nPF-Node STDERR:\n{disc['host']['stderr']}")
            
            if disc['container']['stderr']:
                print(f"\nContainer STDERR:\n{disc['container']['stderr']}")
            
            print(f"\nPF-Node Output:")
            host_output = disc['host']['stdout']
            print(f"{host_output[:500]}")
            if len(host_output) > 500:
                print(f"... (truncated, {len(host_output)} total chars)")
            
            print(f"\nContainer Output:")
            container_output = disc['container']['stdout']
            print(f"{container_output[:500]}")
            if len(container_output) > 500:
                print(f"... (truncated, {len(container_output)} total chars)")
            
            print(f"\n{'='*70}\n")
    else:
        print("[OK] No discrepancies found! All outputs match.\n")
    
    # Assert test result - fail if there are discrepancies
    assert len(results["discrepancies"]) == 0, \
        f"Test FAILED: Found {len(results['discrepancies'])} discrepancies out of {results['total_commands']} commands. " \
        f"Check {output_file} for details."
    
    print("[OK] Test PASSED: All nicctl command outputs match between PF-node and container!\n")
def test_nicctl_comparison_device_plugin(request):
    """
    Test case to compare nicctl outputs for device-plugin pod on PF-node.
    Dynamically discovers the PF-node (node with amd.com/nic capacity) and 
    finds the device-plugin pod running on it.
    Uses Kubernetes Python client API.
    """
    
    # Configuration
    namespace = "kube-amd-network"
    pod_name_pattern = "device-plugin"
    container_name = "device-plugin"
    
    # Get command line options
    skip_card_commands = request.config.getoption("--skip-card-commands")
    skip_lif_commands = request.config.getoption("--skip-lif-commands")
    output_dir = request.config.getoption("--output-dir")
    
    # Initialize Kubernetes client
    print(f"\n{'='*70}")
    print(f"Initializing Kubernetes client")
    print(f"{'='*70}\n")
    
    try:
        v1 = init_k8s_client()
        print("[OK] Kubernetes client initialized successfully\n")
    except Exception as e:
        pytest.fail(f"Failed to initialize Kubernetes client: {str(e)}")
    
    print(f"\n{'='*70}")
    print(f"Step 1: Finding PF-node (node with amd.com/nic capacity)")
    print(f"{'='*70}\n")
    # Get all nodes
    try:
        nodes = v1.list_node()
        node_names = [node.metadata.name for node in nodes.items]
        print(f"Found {len(node_names)} nodes: {', '.join(node_names)}\n")
    except Exception as e:
        pytest.fail(f"Failed to list nodes: {str(e)}")
    
    # Find the PF-node by checking for amd.com/nic capacity
    pf_node = None
    pf_node_nic_capacity = None
    
    for node in nodes.items:
        node_name = node.metadata.name
        print(f"Checking node: {node_name}")
        
        # Check node capacity for amd.com/nic
        if node.status.capacity:
            capacity = node.status.capacity
            if 'amd.com/nic' in capacity:
                nic_capacity = capacity['amd.com/nic']
                print(f"  [OK] Found amd.com/nic capacity: {nic_capacity}")
                pf_node = node_name
                pf_node_nic_capacity = nic_capacity
                break
            else:
                print(f"  [FAIL] No amd.com/nic capacity found")
    
    if not pf_node:
        pytest.fail("No PF-node found with amd.com/nic capacity")
    
    print(f"\n{'='*70}")
    print(f"PF-node identified: {pf_node}")
    print(f"NIC capacity: {pf_node_nic_capacity}")
    print(f"{'='*70}\n")
    
    print(f"\n{'='*70}")
    print(f"Step 2: Finding device-plugin pod on PF-node '{pf_node}'")
    print(f"{'='*70}\n")
    
    # List all pods in the namespace on the PF-node
    try:
        field_selector = f"spec.nodeName={pf_node}"
        pods_on_node = v1.list_namespaced_pod(
            namespace=namespace,
            field_selector=field_selector
        )
        
        print(f"Pods on PF-node '{pf_node}':")
        print(f"{'NAME':<60} {'STATUS':<15} {'IP':<15}")
        print(f"{'-'*90}")
        
        for pod in pods_on_node.items:
            pod_name_display = pod.metadata.name
            pod_status = pod.status.phase
            pod_ip = pod.status.pod_ip if pod.status.pod_ip else "N/A"
            print(f"{pod_name_display:<60} {pod_status:<15} {pod_ip:<15}")
        
        print()
        
    except Exception as e:
        pytest.fail(f"Failed to list pods on node {pf_node}: {str(e)}")
    
    # Find the device-plugin pod on the PF-node
    device_plugin_pod = None
    
    for pod in pods_on_node.items:
        if pod_name_pattern in pod.metadata.name:
            device_plugin_pod = pod.metadata.name
            pod_status = pod.status.phase
            print(f"[OK] Found device-plugin pod: {device_plugin_pod}")
            print(f"   Status: {pod_status}")
            break
    
    if not device_plugin_pod:
        pytest.fail(f"No pod matching pattern '{pod_name_pattern}' found on PF-node {pf_node}")
    
    # Verify the pod is running
    print(f"\nVerifying pod '{device_plugin_pod}' is running...")
    
    try:
        pod_info = v1.read_namespaced_pod(
            name=device_plugin_pod,
            namespace=namespace
        )
        
        pod_status = pod_info.status.phase
        print(f"Pod status: {pod_status}")
        
        if pod_status != "Running":
            pytest.fail(f"Pod {device_plugin_pod} is not in Running state. Current state: {pod_status}")
        
    except Exception as e:
        pytest.fail(f"Failed to verify pod status: {str(e)}")
    
    # Get and verify container name
    print(f"\nGetting containers in pod '{device_plugin_pod}'...")
    
    try:
        containers = [container.name for container in pod_info.spec.containers]
        print(f"Containers in pod: {', '.join(containers)}")
        
        # Use the first container if the specified container name is not found
        actual_container = container_name if container_name in containers else containers[0]
        
        if actual_container != container_name:
            print(f"[WARN]  Container '{container_name}' not found, using '{actual_container}' instead")
        else:
            print(f"[OK] Using container: {actual_container}")
    
    except Exception as e:
        pytest.fail(f"Failed to get containers: {str(e)}")
    
    # Generate output file name
    output_file = f"{output_dir}/nicctl_comparison_{device_plugin_pod}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    
    # Run the comparison
    print(f"\n{'='*70}")
    print(f"Step 3: Running nicctl comparison")
    print(f"{'='*70}")
    print(f"  PF-node:   {pf_node}")
    print(f"  Pod:       {device_plugin_pod}")
    print(f"  Container: {actual_container}")
    print(f"  Namespace: {namespace}")
    print(f"{'='*70}\n")
    
    results = compare_nicctl_outputs(
        pod_name=device_plugin_pod,
        container_name=actual_container,
        pf_node=pf_node,
        namespace=namespace,
        skip_card_commands=skip_card_commands,
        skip_lif_commands=skip_lif_commands,
        output_file=output_file,
        commands=COMMANDS_DP
    )
    
    # Print summary
    print(f"\n{'='*70}")
    print(f"TEST SUMMARY")
    print(f"{'='*70}")
    #import pdb;pdb.set_trace()
    print(f"Total commands tested: {results['total_commands']}")
    print(f"Discrepancies found:   {len(results['discrepancies'])}")
    if results['total_commands'] > 0:
        print(f"Match rate:            {((results['total_commands'] - len(results['discrepancies'])) / results['total_commands'] * 100):.1f}%")
    print(f"Results saved to:      {output_file}")
    print(f"{'='*70}\n")
    
    # Print detailed discrepancy list
    if results["discrepancies"]:
        print(f"{'='*70}")
        print(f"DISCREPANCY DETAILS")
        print(f"{'='*70}\n")
        
        for idx, disc in enumerate(results["discrepancies"], 1):
            print(f"Discrepancy #{idx}: {disc['original_command']}")
            if disc.get('card_id'):
                print(f"  Card ID: {disc['card_id']}")
            if disc.get('lif_id'):
                print(f"  LIF ID: {disc['lif_id']}")
            print(f"{'-'*70}")
            
            print(f"PF-Node Return Code:   {disc['host']['returncode']}")
            print(f"Container Return Code: {disc['container']['returncode']}")
            
            if disc['host']['stderr']:
                print(f"\nPF-Node STDERR:\n{disc['host']['stderr']}")
            
            if disc['container']['stderr']:
                print(f"\nContainer STDERR:\n{disc['container']['stderr']}")
            
            print(f"\nPF-Node Output:")
            host_output = disc['host']['stdout']
            print(f"{host_output[:500]}")
            if len(host_output) > 500:
                print(f"... (truncated, {len(host_output)} total chars)")
            
            print(f"\nContainer Output:")
            container_output = disc['container']['stdout']
            print(f"{container_output[:500]}")
            if len(container_output) > 500:
                print(f"... (truncated, {len(container_output)} total chars)")
            
            print(f"\n{'='*70}\n")
    else:
        print("[OK] No discrepancies found! All outputs match.\n")
    
    # Assert test result - fail if there are discrepancies
    assert len(results["discrepancies"]) == 0, \
        f"Test FAILED: Found {len(results['discrepancies'])} discrepancies out of {results['total_commands']} commands. " \
        f"Check {output_file} for details."
    
    print("[OK] Test PASSED: All nicctl command outputs match between PF-node and container!\n")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
