#!/usr/bin/python3

'''
 Copyright (c) Advanced Micro Devices, Inc. All rights reserved.

 Licensed under the Apache License, Version 2.0 (the \"License\");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at

      http://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an \"AS IS\" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
'''

"""
AMD GPU Operator Node Labeller Test Suite.

This test suite validates the Kubernetes node labeller functionality within the
AMD GPU Operator. The node labeller is a daemonset component that automatically
discovers AMD GPU properties and applies informative labels to Kubernetes nodes.

Node Labeller Functionality:
- Runs as a daemonset pod on each GPU node
- Queries GPU hardware properties using ROCm SMI or similar tools
- Applies standardized labels to node metadata
- Supports dynamic enable/disable via DeviceConfig CR
- Labels persist until explicitly removed by the labeller

Expected GA Labels (amd.com namespace):
- amd.com/gpu.device-id: GPU device PCI ID (e.g., 740f)
- amd.com/gpu.family: GPU architecture family (e.g., AI, CDNA)
- amd.com/gpu.simd-count: Total SIMD compute units across all GPUs
- amd.com/gpu.vram: Total video memory across all GPUs

Deployment Model:
- Enabled via DeviceConfig CR: devicePlugin.enableNodeLabeller: true
- Creates node-labeller daemonset pods (1 per GPU node)
- Labels applied immediately upon pod startup
- Labels removed when labeller is disabled

Test Coverage:
1. Verify node-labeller pod deployment when enabled
2. Validate presence and correctness of GA labels
3. Test dynamic disable/enable of labeller
4. Verify label cleanup when labeller disabled
5. Verify label restoration when labeller re-enabled
6. Validate label values against hardware properties

Test Environment:
- Requires GPU cluster with Kubernetes
- GPU Operator helm chart must be installed
- AMDGPU driver must be installed on nodes
- ROCm stack available for GPU property queries

Key Dependencies:
- lib.k8_util: Kubernetes cluster and node operations
- lib.spec_util: DeviceConfig CR generation utilities
- lib.util.K8Helper: Test assertion and triage utilities
"""

import pytest
import pprint
import pdb
import sys
import os
import time
import json
import logging
import lib.helm_util as helm_util
import lib.k8_util as k8_util
import lib.spec_util as spec_util
import lib.common as common
import lib.amdgpu as amdgpu_util
from lib.util import K8Helper

#pytestmark = pytest.mark.skip("debugging")
Logger = logging.getLogger("k8.test_node_labeller")

EXPECTED_GA_LABELS = {
    "amd.com/gpu.device-id",
    "amd.com/gpu.family",
    "amd.com/gpu.simd-count",
    "amd.com/gpu.vram",
}

EXPECTED_BETA_LABELS = {
    "beta.amd.com/gpu.device-id",
    "beta.amd.com/gpu.family",
    "beta.amd.com/gpu.simd-count",
    "beta.amd.com/gpu.vram",
}

@pytest.fixture(scope="module")
def deviceconfig_install(gpu_cluster, images, gpu_operator_install, environment):
    """
    Deploy DeviceConfig CR with node labeller enabled for testing.

    This fixture handles the complete lifecycle of DeviceConfig custom resources
    configured specifically for node labeller testing. It creates one or more
    DeviceConfig CRs depending on cluster GPU heterogeneity.

    Setup Phase:
    1. Cleanup any existing DeviceConfig CRs from previous tests
    2. Query cluster for GPU nodes
    3. Build DeviceConfig CR configuration with:
       - driver.enable: True (AMDGPU driver deployment)
       - devicePlugin.enableNodeLabeller: True (enable labeller daemonset)
       - metricsExporter.enable: False (not needed for this test)
       - testRunner.enable: False (not needed for this test)
    4. Generate DeviceConfig CR templates based on GPU node selector requirements
       - Single DeviceConfig if all nodes have identical GPUs
       - Multiple DeviceConfigs if cluster has heterogeneous GPUs
    5. Assign unique NodePorts for metrics exporter (if multiple configs)
    6. Create DeviceConfig CRs via kubectl apply
    7. Wait for DeviceConfig status to become Ready
    8. Wait for KMM worker completion (driver module compilation/loading)
    9. Update cluster node driver version information

    DeviceConfig CR Structure:
        apiVersion: amd.com/v1alpha1
        kind: DeviceConfig
        metadata:
          name: node-labeller-test-config
          namespace: {gpu_operator_namespace}
        spec:
          selector:
            feature.node.kubernetes.io/pci-1002.present: "true"
          driver:
            enable: true
          devicePlugin:
            enableNodeLabeller: true
          metricsExporter:
            enable: false

    Teardown Phase (after all tests complete):
    1. Query all DeviceConfig CRs in gpu-operator namespace
    2. Delete each DeviceConfig CR
    3. Kubernetes garbage collection removes associated daemonsets and pods

    Args:
        gpu_cluster: GPU cluster fixture providing node information
        images: Dictionary containing image repository and version information
        gpu_operator_install: Fixture ensuring GPU Operator helm chart is installed
        environment: Test environment fixture for logging and triaging

    Yields:
        DeviceConfigCRInfo: Object containing:
            - test_cfg_map: Dict mapping DeviceConfig name to configuration
            - exporter_port_map: Dict mapping node hostname to NodePort number
            - devicecfg_list: List of DeviceConfig CR names created

    Raises:
        AssertionError: If GPU nodes not found, DeviceConfig creation fails, or pods don't become ready
    """
    global Logger

    # cleanup - remove any deviceconfigs and then gpu-operator helm-chart
    devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
    for devcfg_name, _ in devcfg_map.items():
        ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)
        if ret_code != 0:
            Logger.error(f"Failed to delete deviceconfig name: {devcfg_name}, error : {ret_stderr}")
    time.sleep(10)

    class DeviceConfigCRInfo(object):
        pass

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")

    test_config = {
            'metadata.namespace' : environment.gpu_operator_namespace,
            'driver.enable' : True,
            'devicePlugin.enableNodeLabeller' : True,
            'metricsExporter.enable' : False,
            'testRunner.enable' : False,
        }
    test_config.update(images)

    test_cfg_map = spec_util.build_deviceconfig_cr_template(test_config, gpu_nodes, 'node_labeller', environment.amdgpu_driver_spec)
    exporter_port_map = {}
    devicecfg_list = []
    if len(test_cfg_map) > 1:
        # Assign unique NodePorts for each deviceconfig instance
        for idx, cfg_name in enumerate(test_cfg_map.keys()):
            cfg = test_cfg_map[cfg_name]
            cfg['metricsExporter.nodePort'] = 32500 + idx * 100
            exporter_port_map[cfg['selector.value']] = cfg['metricsExporter.nodePort']
    else:
        for node in gpu_nodes:
            node_hostname = k8_util.k8_get_node_hostname(node)
            exporter_port_map[node_hostname] = 32500

    for spec_name, tcfg in test_cfg_map.items():
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_create_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, (ret_code == 0), f"Failed to create deviceconfig, stderr: {ret_stderr}")
        devicecfg_list.append(tcfg['metadata.name'])

    # Check for corresponding deviceconfig created
    K8Helper.check_deviceconfig_status(environment, devicecfg_list)
    for devcfg in devicecfg_list:
        K8Helper.wait_kmm_worker_completion(environment, devcfg)
    K8Helper.update_node_driver_version(gpu_cluster, environment)

    devcfg_info = DeviceConfigCRInfo()
    setattr(devcfg_info, "test_cfg_map", test_cfg_map)
    setattr(devcfg_info, "exporter_port_map", exporter_port_map)
    setattr(devcfg_info, "devicecfg_list", devicecfg_list)
    yield devcfg_info

    device_cfg_info = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace, None)
    for devcfg_name, _ in device_cfg_info.items():
        k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)
    return

def test_node_labeller_enable_flag(deviceconfig_install, environment):
    """
    Verify node labeller can be dynamically enabled, disabled, and re-enabled.

    This test validates the complete lifecycle of node labeller management via
    DeviceConfig CR modifications. It ensures that:
    - Labels are applied when labeller is enabled
    - Labels are removed when labeller is disabled
    - Labels are re-applied when labeller is re-enabled

    Test Flow:
    1. Initial State - Verify labeller enabled (from fixture):
       - Query GPU nodes from cluster
       - Verify device-plugin and node-labeller pods are Running
       - Check all GPU nodes have expected GA labels applied
       - Verify labels contain valid values

    2. Disable Node Labeller:
       - Modify DeviceConfig CR: devicePlugin.enableNodeLabeller = false
       - Apply updated DeviceConfig CR via kubectl
       - Wait for node-labeller pods to terminate
       - Verify all amd.com/* labels removed from all GPU nodes

    3. Re-enable Node Labeller:
       - Modify DeviceConfig CR: devicePlugin.enableNodeLabeller = true
       - Apply updated DeviceConfig CR via kubectl
       - Wait for node-labeller pods to return to Running state
       - Verify labels are re-applied (tested implicitly by pod readiness)

    Expected Pod States:
        Enabled state:
            test-deviceconfig-device-plugin-8f7px      1/1     Running
            test-deviceconfig-node-labeller-54vpd      1/1     Running

        Disabled state:
            test-deviceconfig-device-plugin-8f7px      1/1     Running
            (node-labeller pods terminated/removed)

    Label Verification:
        When enabled, each node should have:
        - amd.com/gpu.device-id: <device-id>
        - amd.com/gpu.family: AI (or other family)
        - amd.com/gpu.simd-count: <count>
        - amd.com/gpu.vram: <vram>

        When disabled, all amd.com/* labels should be absent.

    Success Criteria:
        - Node labeller pods deploy/terminate correctly based on flag
        - Labels appear when labeller enabled
        - Labels removed when labeller disabled
        - Labels re-appear when labeller re-enabled
        - No residual labels remain after disable

    Args:
        deviceconfig_install: DeviceConfig deployment fixture with labeller enabled
        environment: Test environment for triaging and logging

    Raises:
        AssertionError: If pods fail to deploy/terminate, or labels not applied/removed correctly
    """
    global Logger

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error while collecting gpu-nodes")
    # Watch for all pod creation
    '''
    test-deviceconfig-device-plugin-8f7px                        1/1     Running       0                 12d
    test-deviceconfig-metrics-exporter-27gq9                     2/2     Running       0                 12d
    test-deviceconfig-node-labeller-54vpd                        1/1     Running       0                 12d
    '''
    devicecfg_pods = [
        common.PodInfo('device-plugin', len(gpu_nodes), 1),
        common.PodInfo('node-labeller', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods)
    K8Helper.triage(environment, not failed_pods, f"One or more pods are not ready - {failed_pods}")

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")

    # Check each worker node for annotations applied by node-labeller
    for node_info in gpu_nodes:
        node_name = node_info["metadata"]["name"]
        K8Helper.triage(environment, ('metadata' in node_info), f"Metadata missing in node_info for {node_info}")
        K8Helper.triage(environment, ('labels' in node_info['metadata']), f'Labels not found for node: {node_name}')
        assigned_labels = set(filter(lambda x: 'amd.com' in x, node_info['metadata']['labels'].keys()))
        K8Helper.triage(environment, (EXPECTED_GA_LABELS.issubset(assigned_labels)),
                        f"Missing {EXPECTED_GA_LABELS - assigned_labels} for node {node_name}")
        """
        K8Helper.triage(environment, (EXPECTED_BETA_LABELS.issubset(assigned_labels)),
                        f"Missing {EXPECTED_BETA_LABELS - assigned_labels} for node {node_name}", expected_to_fail=True)
        """

    # Now disable labeller
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['devicePlugin.enableNodeLabeller'] = False
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, (ret_code == 0), "Failed apply CR with devicePlugin.enableNodeLabeller disabled")

    labeller_pods = [
        common.PodInfo('node-labeller', 1, 1),
    ]
    running_pods = k8_util.k8_check_pod_terminated(environment.gpu_operator_namespace, labeller_pods)
    K8Helper.triage(environment, not running_pods, f"Some of the pods are still running - {running_pods}")

    # Check absense of annotations for each worker node after removal of node-labeller
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Failed to get nodes from cluster")
    for node_info in gpu_nodes:
        node_name = node_info["metadata"]["name"]
        K8Helper.triage(environment, ('metadata' in node_info), f"Metadata missing in node_info for {node_info}")
        K8Helper.triage(environment, ('labels' in node_info['metadata']),
                        f'Labels not found for node: {node_info["metadata"]["name"]}')
        assigned_labels = list(filter(lambda x: 'amd.com' in x, node_info['metadata']['labels'].keys()))
        for exp_label in EXPECTED_GA_LABELS:
            found = False
            for node_label in assigned_labels:
                if exp_label in node_label:
                    found = True
                    break
            K8Helper.triage(environment, (not found), f"{exp_label} still assigned to node {node_name} after node-labeller is disabled")

        for exp_label in EXPECTED_BETA_LABELS:
            found = False
            for node_label in assigned_labels:
                if exp_label in node_label:
                    found = True
                    break
            K8Helper.triage(environment, (not found), f"{exp_label} still assigned to node {node_name} after node-labeller is disabled")

    # Re-enable node-labeller
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['devicePlugin.enableNodeLabeller'] = True
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, (ret_code == 0), "Failed to modify deviceconfig CR")

    devicecfg_pods = [
        common.PodInfo('device-plugin', len(gpu_nodes), 1),
        common.PodInfo('node-labeller', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods)
    K8Helper.triage(environment, not failed_pods, f"One or more pods are not ready - {failed_pods}")

def test_node_labeller_check_labels(gpu_cluster, deviceconfig_install, environment):
    """
    Verify node labeller applies correct GPU property labels to all nodes.

    This test validates that the node labeller correctly discovers GPU hardware
    properties and applies accurate labels with valid values to each GPU node.

    Test Flow:
    1. Ensure node labeller is enabled in DeviceConfig CR
    2. Verify device-plugin and node-labeller pods are Running
    3. Query GPU nodes from cluster
    4. For each GPU node, validate label presence and values:
       - amd.com/gpu.family: Must equal "AI" (CDNA architecture)
       - amd.com/gpu.device-id: Must be present and non-empty
       - amd.com/gpu.vram: Must be present and non-empty
       - amd.com/gpu.simd-count: Must be present and > 0

    Label Validation Details:
        amd.com/gpu.family:
            - Expected value: "AI" (for CDNA/Instinct GPUs)
            - Other possible values: CDNA, RDNA, GCN (architecture-dependent)

        amd.com/gpu.device-id:
            - Format: 4-digit hex PCI device ID (e.g., "740f")
            - Unique identifier for GPU model

        amd.com/gpu.vram:
            - Format: Memory size string (e.g., "64G", "32G")
            - Total VRAM across all GPUs on the node

        amd.com/gpu.simd-count:
            - Format: Integer string (e.g., "104", "208")
            - Total SIMD compute units across all GPUs
            - Must be greater than 0

    Commented/Future Tests:
        Beta labels (beta.amd.com namespace):
            - Currently disabled in code
            - Legacy label format from earlier GPU operator versions
            - May be re-enabled for backward compatibility testing

        Device ID count labels:
            - Format: amd.com/gpu.device-id.<device-id>: "<count>"
            - Example: amd.com/gpu.device-id.740f: "2"
            - Currently disabled, may be added in future versions

        VRAM count labels:
            - Format: amd.com/gpu.vram.<size>: "<count>"
            - Example: amd.com/gpu.vram.64G: "2"
            - Currently disabled, may be added in future versions

    Success Criteria:
        - All expected GA labels present on every GPU node
        - All label values are non-empty
        - gpu.family equals "AI"
        - gpu.simd-count is positive integer
        - No missing or malformed labels

    Args:
        deviceconfig_install: DeviceConfig deployment fixture
        environment: Test environment for triaging and logging

    Raises:
        AssertionError: If any expected label is missing, empty, or has incorrect value
    """
    global Logger

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error while collecting gpu-nodes")

    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['devicePlugin.enableNodeLabeller'] = True
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, (ret_code == 0), "Failed to modify deviceconfig CR")

    devicecfg_pods = [
        common.PodInfo('device-plugin', len(gpu_nodes), 1),
        common.PodInfo('node-labeller', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods)
    K8Helper.triage(environment, not failed_pods, f"One or more pods are not ready - {failed_pods}")

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Failed to find amd/gpu nodes in the cluster")
    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        # expected labels list
        exp_label_list = ["amd.com/gpu.family", "amd.com/gpu.device-id", "amd.com/gpu.vram", "amd.com/gpu.simd-count"]
        # get node labels
        labels = k8_util.k8_get_node_labels(node_name)
        K8Helper.triage(environment, (labels != None), "Failed to get labels for node {node_name}")
        labels_dict = labels
        Logger.info(f'labels: {labels_dict}') 
        for label in exp_label_list:
            Logger.info(f'Check label: {label}')
            K8Helper.triage(environment, (label in labels_dict.keys()), f"Missing label - {label}")
            K8Helper.triage(environment, (labels_dict[label] != ""), f'labels.{label}: labels_dict[label]')
        
        node_ip = k8_util.k8_get_node_address(node)
        cluster_node = gpu_cluster.find_node_by_ip(node_ip)
        gpu_family = labels_dict["amd.com/gpu.family"]
        expected_family = amdgpu_util.get_gpu_features(cluster_node.device_id).get("gpu_family", "AI") if cluster_node else "AI"
        K8Helper.triage(environment, gpu_family == expected_family,
                        f"Incorrect value for label: amd.com/gpu.family={gpu_family}, expected {expected_family}")
        K8Helper.triage(environment, (int(labels_dict["amd.com/gpu.simd-count"]) > 0), "Incorrect value of amd.com/gpu.simd-count")

        # check the device id and count
        """
        Logger.info("Check device-id label is present with a count")
        device_id = labels_dict["amd.com/gpu.device-id"]
        K8Helper.triage(environment, f'amd.com/gpu.device-id.{device_id}' in labels_dict.keys(),
                        f"Missing label: amd.com/gpu.device-id.{device_id}", expected_to_fail=True)

        device_id_count = labels_dict[f'amd.com/gpu.device-id.{device_id}']
        K8Helper.triage(environment, int(device_id_count) > 0,
                        f'amd.com/gpu.device-id: {device_id}, amd.com/gpu.device-id.{device_id}: {device_id_count}', expected_to_fail=True)
        """

        # check beta labels
        """
        beta_label_list = ['beta.amd.com/gpu.device-id', 
                           'beta.amd.com/gpu.family', 'beta.amd.com/gpu.simd-count', 'beta.amd.com/gpu.vram' ]
        for label in beta_label_list:
            Logger.info(f'Check label: {label}')
            K8Helper.triage(environment, (label in labels_dict.keys()), f"Missing label : {label}", expected_to_fail=True)
            K8Helper.triage(environment, (labels_dict[label] != None), f'labels.{label}: labels_dict[label]', expected_to_fail=True)
        K8Helper.triage(environment, (int(labels_dict['beta.amd.com/gpu.simd-count']) > 0),
                        "Invalid value for label: beta.amd.com/gpu.simd-count", expected_to_fail=True)

        gpu_family = labels_dict['beta.amd.com/gpu.family']
        K8Helper.triage(environment, (gpu_family == "AI"), f"Invalid value for label beta.amd.com/gpu.family:{gpu_family}", expected_to_fail=True)
        K8Helper.triage(environment, (f'beta.amd.com/gpu.family.{gpu_family}' in labels_dict.keys()),
                        f"Missing label: beta.amd.com/gpu.family.{gpu_family}", expected_to_fail=True)
        gpu_family_count = labels_dict[f"beta.amd.com/gpu.family.{gpu_family}"]
        K8Helper.triage(environment, (int(gpu_family_count) > 0),
                        f'beta.amd.com/gpu.family: {gpu_family}, beta.amd.com/gpu.family.{gpu_family}: {gpu_family_count}', expected_to_fail=True)
        """

        # check the device id and count
        """
        Logger.info("Check device-id label is present with a count")
        device_id = labels_dict["beta.amd.com/gpu.device-id"]
        device_id_count_label = f'beta.amd.com/gpu.device-id.{device_id}'
        K8Helper.triage(environment, (device_id_count_label in labels_dict.keys()), f"Missing {device_id_count_label}", expected_to_fail=True)
        device_id_count = labels_dict[f'beta.amd.com/gpu.device-id.{device_id}']
        K8Helper.triage(environment, (int(device_id_count) > 0),
                        f'amd.com/gpu.device-id: {device_id}, beta.amd.com/gpu.device-id.{device_id}: {device_id_count}', expected_to_fail=True)
        """

        # check vram
        # eg: 'beta.amd.com/gpu.vram': '64G', 'beta.amd.com/gpu.vram.64G': '1'
        """
        Logger.info("Check gpu-vram label is present with a count")
        vram = labels_dict['beta.amd.com/gpu.vram']
        vram_label = f'beta.amd.com/gpu.vram.{vram}'
        K8Helper.triage(environment, (vram_label in labels_dict), f"Missing vram-label : {vram_label}", expected_to_fail=True)
        vram_count = labels_dict[vram_label]
        K8Helper.triage(environment, (int(vram_count) > 0),
                        f'beta.amd.com/gpu.vram: {vram}, beta.amd.com/gpu.vram.{vram}: {vram_count}', expected_to_fail=True)
        """

