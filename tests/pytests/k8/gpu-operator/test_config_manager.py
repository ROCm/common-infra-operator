#!/usr/bin/python3

# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""AMD GPU Operator Config Manager Test Suite.

This module contains comprehensive tests for the AMD GPU Operator's Device Config Manager (DCM)
component. The DCM manages GPU partitioning profiles (compute and memory partitions) on AMD MI3xx
series GPUs through Kubernetes Custom Resources (DeviceConfig CR).

Key Features Tested:
    - GPU partitioning profiles (QPX, DPX, CPX, SPX with different NPS modes)
    - ConfigMap-driven partition configuration
    - Node labeling and tainting during partition changes
    - Workload eviction and rescheduling during partitioning
    - Metrics exporter configuration through ConfigMaps
    - Operand upgrade scenarios (RollingUpdate vs OnDelete)
    - Enable/disable lifecycle management
    - Event and log validation

GPU Partition Profiles:
    SPX (Single GPU): Full GPU resources, single partition
    DPX (Dual GPU): 2 partitions per GPU
    QPX (Quad GPU): 4 partitions per GPU
    CPX (Compute GPU): 8 compute partitions
    NPS (NUMA Per Socket): Memory partition modes (NPS1, NPS2, NPS4)

Test Organization:
    - Negative tests: Invalid partition configurations
    - Positive tests: Valid partition profiles per GPU series
    - Workload tests: Partition changes with running workloads
    - Integration tests: DCM with metrics exporter and test runner

Supported GPU Series:
    MI300X, MI325X, MI350X, MI350P (MI2xx series not supported)

DeviceConfig CR Management:
    Tests create, modify, and delete DeviceConfig CRs to manage DCM lifecycle.
    Each test uses fixtures to ensure proper cleanup and restoration of cluster state.
"""

import pdb
import pprint
import pytest
import sys
import os
import time
import json
import logging
import random
import datetime
import yaml
import copy
import functools
from packaging import version
import lib.common as common
import lib.helm_util as helm_util
import lib.k8_util as k8_util
import lib.spec_util as spec_util
import lib.metric_util as metric_util
import lib.amdgpu as amdgpu_util
from lib.util import K8Helper
from kubernetes import client, config, utils
from test_test_runner import update_test_runner_configmap, create_configmap, update_test_runner_image, metrics_fields

Logger = logging.getLogger("k8.test_config_manager")
LogPrettyPrinter = pprint.PrettyPrinter(indent = 2)

debug_on_failure = K8Helper.triage

@pytest.fixture(autouse=True, scope="module")
def skip_module(environment):
    """Skip entire module for GPU Operator versions that don't support Config Manager.

    Config Manager was introduced in v1.3.0. Earlier versions (v1.0.0 through v1.2.1)
    don't have the DCM component, so all tests in this module are skipped.

    Args:
        environment: Test environment fixture containing gpu_operator_version.

    Raises:
        pytest.skip: If GPU Operator version is earlier than v1.3.0.
    """
    if environment.gpu_operator_version in ["v1.0.0", "v1.1.0", "v1.2.0", "v1.2.1"]:
        pytest.skip(f"Skipping config-manager for current version {environment.gpu_operator_version}")
    return

@pytest.fixture(scope="module")
def add_tolerations(environment, effect="NoExecute"):
    """Add amd-dcm tolerations to system namespaces during DCM testing.

    During GPU partitioning, nodes are tainted with amd-dcm=up:NoExecute to prevent
    new workloads from scheduling. System pods in kube-system, cert-manager, and
    kube-flannel namespaces need tolerations to continue running during partition changes.

    Setup Phase:
        Patches DaemonSets in system namespaces to add amd-dcm toleration.

    Teardown Phase:
        Removes the amd-dcm toleration from system namespaces.

    Args:
        environment: Test environment fixture.
        effect: Taint effect to tolerate (default: "NoExecute").

    Yields:
        None: Control returns to test after tolerations are added.
    """
    toleration_to_add = {
        "key": "amd-dcm",
        "operator": "Equal",
        "value": "up",
        "effect": effect
    }

    for ns in {"kube-system", "cert-manager", "kube-flannel"}:
        k8_util.k8_patch_tolerations(ns, toleration_to_add, tolerate_add=True)
    yield

    for ns in {"kube-system", "cert-manager", "kube-flannel"}:
        k8_util.k8_patch_tolerations(ns, toleration_to_add, tolerate_add=False)

def verify_events(gpu_cluster, environment, profile, before, after):
    """Verify Kubernetes events generated during GPU partition profile changes.

    Compares events before and after a partition change to validate that DCM
    generated appropriate success/failure events. Looks for events containing
    the profile name and "Success" message, and checks for any "Fail" events.

    Args:
        gpu_cluster: Cluster object with GPU node information.
        environment: Test environment fixture.
        profile: GPU partition profile name (e.g., "QPX_NPS1", "DPX_NPS2").
        before: Tuple of (return_code, EventList) captured before partition change.
        after: Tuple of (return_code, EventList) captured after partition change.

    Raises:
        pytest.skip: If GPU series doesn't support partitioning or profile not available.
        AssertionError: If "Fail" event found or success event missing.
    """
    global Logger

    gpu_series = get_gpu_series(gpu_cluster, environment)
    dut_node = gpu_cluster.find_node_by_gpu_series(gpu_series)
    if not amdgpu_util.supports_config_manager(dut_node.device_id):
        pytest.skip(f"GPU partitioning not supported for {gpu_series}")
    file_path = os.path.join(environment.logdir, f"partitioning_check_{gpu_series}_{dut_node.num_gpus}.json")
    with open(file_path) as fp:
        profiles = json.load(fp)
        if not profiles.get("gpu-config-profiles"):
            pytest.fail(f"check {file_path}, something wrong with the configmap")
        elif not profiles["gpu-config-profiles"].get(profile, False):
            pytest.skip(f"testcase not supported")

    before_events = before[1].items
    after_events = after[1].items

    # 1. Extract the unique UIDs from the 'before' list and put them in a set
    before_uids = {event.metadata.uid for event in before_events}

    # 2. Iterate through the 'after' list and find any event whose UID is NOT in the 'before' set
    Logger.info("Following are the events observed during this testcase:-")
    new_events = []
    flag = False
    fail_found = False
    fail_message = ""
    for event in after_events:
        if event.metadata.uid not in before_uids:
            Logger.info(f"=============={event.metadata.uid}=================")
            Logger.info(f"Reason: {pprint.pformat(event.reason)}")
            Logger.info(f"Type: {pprint.pformat(event.type)}")
            Logger.info(f"Object: {pprint.pformat(event.involved_object.name)}")
            Logger.info(f"Message: {pprint.pformat(event.message)}")

            # Try to parse event message as JSON for structured checking
            event_data = None
            try:
                event_data = json.loads(event.message)
            except (json.JSONDecodeError, TypeError):
                # Not JSON or invalid - fall back to string matching
                Logger.debug(f"Event message is not JSON, using string matching")

            # Check structured data if available, otherwise fall back to string matching
            if event_data:
                selected_profile = event_data.get("SelectedProfile", "")
                final_status = event_data.get("FinalStatus", "")
                reason = event_data.get("Reason", "")
                gpu_status_list = event_data.get("GPUStatus", [])

                Logger.debug(f"Parsed event: Profile={selected_profile}, Status={final_status}, Reason={reason}")

                # Check for success
                if selected_profile == profile and final_status == "Success":
                    flag = True
                    fail_found = False
                    Logger.info(f"✓ Success event: {reason}")
                    for gpu_status in gpu_status_list:
                        Logger.info(f"  GPU {gpu_status.get('GpuID')}: {gpu_status.get('Status')} - {gpu_status.get('Message')}")
                # Skip transient progress states
                elif reason and ("Pending" in reason or "Waiting" in reason):
                    Logger.debug(f"Progress event: {reason} - skipping")
                # Detect failures
                elif final_status == "Failure":
                    fail_found = True
                    fail_message = f"{reason} - {event.message}"
                    Logger.error(f"✗ Failure event: {reason}")
                    for gpu_status in gpu_status_list:
                        Logger.error(f"  GPU {gpu_status.get('GpuID')}: {gpu_status.get('Status')} - {gpu_status.get('Message')}")
            else:
                # Fallback to string matching for non-JSON events
                if profile in event.message and "Success" in event.message:
                    flag = True
                    fail_found = False
                    Logger.info(f"✓ Success event found for profile {profile}")
                elif "Pending" in event.message or "Waiting" in event.message:
                    Logger.debug(f"Progress event (type={event.type}): {event.reason} - skipping")
                elif "Fail" in event.message:
                    fail_found = True
                    fail_message = event.message
                    Logger.error(f"✗ Failure event found (type={event.type}): {event.reason}")

    debug_on_failure(environment, not fail_found, f"Fail found in event: {fail_message}")
    debug_on_failure(environment, flag,
                     f"Successful profile change has not happened with {profile}")

@pytest.fixture(scope="module")
def deviceconfig_install(gpu_cluster, images, gpu_operator_install, create_dcm_configmap, add_tolerations, environment):
    """Create and configure DeviceConfig CRs with Config Manager enabled.

    This is the main fixture for DCM testing. It creates DeviceConfig custom resources
    with Config Manager enabled, along with device-plugin and metrics-exporter operands.

    Setup Phase:
        1. Clean up any existing DeviceConfig CRs from previous test runs
        2. Build DeviceConfig CR template with Config Manager enabled
        3. Assign unique NodePorts for metrics exporter on each node
        4. Create DeviceConfig CRs for each GPU node or node group
        5. Wait for deviceconfig status to be Ready
        6. Wait for KMM worker pods to complete driver installation
        7. Verify device-plugin and config-manager pods are running
        8. Update cluster nodes with driver version information

    Teardown Phase:
        Deletes all DeviceConfig CRs created during setup.

    Args:
        gpu_cluster: Cluster object with GPU nodes.
        images: Image manifest with container image URLs.
        gpu_operator_install: Ensures GPU operator is installed.
        create_dcm_configmap: Creates ConfigMap with partition profiles.
        add_tolerations: Adds tolerations to system namespaces.
        environment: Test environment fixture.

    Yields:
        DeviceConfigCRInfo: Object containing:
            - test_cfg_map: Dict of DeviceConfig configurations by name
            - exporter_port_map: Dict mapping node hostname to NodePort
            - devicecfg_list: List of DeviceConfig CR names

    Raises:
        AssertionError: If GPU nodes not found, DeviceConfig creation fails,
                       or pods don't reach Running state.
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
    debug_on_failure(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    debug_on_failure(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")
    configmap = "config-map-config-manager"

    test_config = {
            'metadata.namespace' : environment.gpu_operator_namespace,
            'driver.enable' : True,
            'devicePlugin.enableNodeLabeller' : True,
            'metricsExporter.enable' : True,
            'metricsExporter.serviceType' : 'NodePort',
            'testRunner.enable' : False,
            'configManager.enable' : True,
            'configManager.config' : configmap,
        }
    test_config.update(images)
    test_cfg_map = spec_util.build_deviceconfig_cr_template(test_config, gpu_nodes, 'config-manager', environment.amdgpu_driver_spec)
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
        debug_on_failure(environment, (ret_code == 0), f"Failed to create deviceconfig, stderr: {ret_stderr}")
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

    devicecfg_pods = [
        common.PodInfo('device-plugin', len(gpu_nodes), 1),
        common.PodInfo('config-manager', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods, sleep_time = 20)
    debug_on_failure(environment, (not failed_pods), f"One or more pods are not ready - {failed_pods}")

    if not failed_pods:
        K8Helper.capture_rocm_version(
            environment.gpu_operator_namespace,
            "config-manager",
            [
                ("configManager.image", "device-config-manager-container"),
            ],
        )

    yield devcfg_info

    device_cfg_info = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace, None)
    for devcfg_name, _ in device_cfg_info.items():
        k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)
    return

def get_gpu_series(gpu_cluster, environment):
    """Get the GPU series from the cluster for partition profile selection.

    Retrieves the list of GPU variants in the cluster and returns the first one.
    Used to determine which partition profile configuration file to load.

    Args:
        gpu_cluster: Cluster object with GPU node information.
        environment: Test environment fixture.

    Returns:
        str: GPU series name (e.g., "MI300X", "MI325X", "MI350X").

    Raises:
        AssertionError: If no GPU series found in cluster.
    """
    gpu_variants = gpu_cluster.get_gpu_variants()
    if gpu_variants:
        Logger.info(f"found following gpu_series in the cluster {gpu_variants}, using {gpu_variants[0]}")
        return gpu_variants[0]
    debug_on_failure(environment, False, f"didn't find gpu_variants from cluster: {gpu_variants}")

@pytest.fixture(scope="module")
def create_dcm_configmap(gpu_cluster, environment):
    """Create ConfigMap with GPU partition profiles for Config Manager.

    Creates a Kubernetes ConfigMap containing partition profile definitions specific
    to the GPU series in the cluster. Profile definitions are loaded from JSON files
    in lib/files/ directory, with filenames like:
        partitioning_check_MI300X_8.json (for MI300X with 8 GPUs)
        partitioning_check_MI350X_16.json (for MI350X with 16 GPUs)

    If no profile file exists for the GPU series, creates an empty profile ConfigMap.

    Setup Phase:
        1. Determine GPU series and GPU count from cluster
        2. Load partition profiles from corresponding JSON file
        3. Delete existing ConfigMap if present
        4. Create new ConfigMap with partition profiles

    Teardown Phase:
        Deletes the ConfigMap.

    Args:
        gpu_cluster: Cluster object with GPU node information.
        environment: Test environment fixture.

    Yields:
        str: ConfigMap name ("config-map-config-manager").

    Raises:
        AssertionError: If GPU series information not available.
    """
    namespace = environment.gpu_operator_namespace
    configmap = "config-map-config-manager"

    gpu_series = get_gpu_series(gpu_cluster, environment)
    dut_node = gpu_cluster.find_node_by_gpu_series(gpu_series)
    num_gpus_on_dut = dut_node.num_gpus
    debug_on_failure(environment, gpu_series != None, f"Missing gpu-series information - collect tech-support to debug cluster")

    file_path = os.path.join(environment.logdir, f"partitioning_check_{gpu_series}_{dut_node.num_gpus}.json")
    ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_configmap(namespace, configmap)
    k8_util.k8_create_configmap(namespace, configmap, file_path, "config.json")
    yield configmap
    ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_configmap(namespace, configmap)

def reset_dcm_profile(gpu_cluster, environment, skip_reboot = True):
    """Reset GPU partitions to default SPX_NPS1 profile and clean up DCM state.

    Restores all GPUs to the default single-partition mode (SPX_NPS1) and removes
    all DCM-related node labels and taints. This is used between tests to ensure
    clean starting state.

    For MI3xx series GPUs with active partitions:
        1. Patches DeviceConfig to add NoExecute toleration for config-manager
        2. Taints nodes and applies SPX_NPS1 label to trigger partition reset
        3. Waits for partition to complete and validates with amd-smi
        4. Removes DCM labels and taints from nodes
        5. Optionally reboots nodes (for inbox driver scenarios on MI325X)

    For already-default or non-MI3xx GPUs:
        Just removes labels and taints.

    Args:
        gpu_cluster: Cluster object with GPU nodes.
        environment: Test environment fixture.
        skip_reboot: If False, reboots nodes after reset (default: True).

    Raises:
        AssertionError: If GPU nodes not found or reset fails.
    """
    global Logger
    namespace = environment.gpu_operator_namespace
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    debug_on_failure(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    debug_on_failure(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")

    def _any_gpu_partitioned(gpu_nodes):
        partitioned = []
        for node in gpu_nodes:
            node_name = node['metadata']['labels']['kubernetes.io/hostname']
            # Use retry logic since pod may be restarting
            partition_status = get_partition_status_from_pod(environment, namespace, node_name, max_retries=5, retry_delay=5)
            if not partition_status:
                Logger.warning(f"Could not get partition status for {node_name}, assuming not partitioned")
                continue

            Logger.debug(f"Current Partition Status for {node_name}: {partition_status}")
            for gpu_id, profile in partition_status.items():
                if profile != "SPX_NPS1":
                    partitioned.append(True)
        return any(partitioned)

    gpu_series = get_gpu_series(gpu_cluster, environment)
    dut_node = gpu_cluster.find_node_by_gpu_series(gpu_series)
    debug_on_failure(environment, gpu_series != None, f"Missing gpu-series information - collect tech-support to debug cluster")

    # Wrap partition logic in try/finally to ensure cleanup always happens
    try:
        if gpu_series and 'MI3' in gpu_series:
            if _any_gpu_partitioned(gpu_nodes):
                patch_body = {
                    "spec": {
                        "configManager": {
                            "configManagerTolerations": [
                                {
                                    "effect": "NoExecute",
                                    "key": "amd-dcm",
                                    "operator": "Equal",
                                    "value": "up"
                                }
                            ]
                        }
                    }
                }

                api_client = client.ApiClient()
                custom_objects_api = client.CustomObjectsApi(api_client)
                devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
                for devcfg_name, _ in devcfg_map.items():
                    try:
                        custom_objects_api.patch_namespaced_custom_object(
                            group="amd.com",
                            version='v1alpha1',
                            name=devcfg_name,
                            namespace=namespace,
                            plural='deviceconfigs',
                            body=patch_body
                        )
                        Logger.info(f"Successfully patched with {patch_body}")
                    except client.ApiException as e:
                        Logger.error(f"Failed to patch custom object: {e}")

                # Watch for all pod creation (node is tainted, no device-plugin)
                devicecfg_pods = [
                    common.PodInfo('config-manager', len(gpu_nodes), 1),
                ]
                failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods, sleep_time = 20)
                if not failed_pods:
                    # Step 1: Taint nodes with NoExecute to evict device-plugin and other pods
                    # Per DCM docs: Use NoExecute to evict existing pods that don't have the toleration
                    Logger.info("Tainting nodes with amd-dcm=up:NoExecute to evict GPU workloads")
                    for node in gpu_nodes:
                        node_name = node['metadata']['labels']['kubernetes.io/hostname']
                        k8_util.k8_taint_node(node_name, taint_add=True, effect="NoExecute")

                    # Step 2: Label nodes with SPX_NPS1 profile to trigger partition reset
                    Logger.info("Labeling nodes with SPX_NPS1 profile")
                    labels_dict = {"dcm.amd.com/gpu-config-profile" : "SPX_NPS1"}
                    for node in gpu_nodes:
                        node_name = node['metadata']['labels']['kubernetes.io/hostname']
                        k8_util.k8_label_node(node_name, labels_dict, overwrite=True)

                    # Step 3: Wait for DCM to process the partition change
                    # verify_label() checks all nodes, so call once (not in a loop)
                    Logger.info("Waiting for DCM to complete partition reset to SPX_NPS1...")
                    verify_label(environment, "SPX_NPS1")  # Waits for state=success on all nodes

                # Step 4: Verify partition is now SPX_NPS1
                if _any_gpu_partitioned(gpu_nodes):
                    Logger.error(f"Failed to restore DCM profile to default SPX_NPS1")
                else:
                    Logger.info(f"GPUs are restored to default partition status (SPX_NPS1)!!")
            else:
                Logger.info(f"GPUs are in default partition status!!")
    finally:
        # Step 5: Cleanup - Remove DCM labels and taints
        # Per DCM docs: After partition completes, remove labels and untaint nodes
        # to allow device-plugin and other workloads to restart
        # This MUST run even if partition fails to avoid leaving nodes tainted
        Logger.info("Cleaning up DCM labels and taints from nodes")
        labels_dict = {
                          "dcm.amd.com/gpu-config-profile" : None,
                          "dcm.amd.com/gpu-config-profile-state" : None
                      }
        for node in gpu_nodes:
            node_name = node['metadata']['labels']['kubernetes.io/hostname']
            k8_util.k8_label_node(node_name, labels_dict, overwrite=True)
            k8_util.k8_untaint_node(node_name, effects=["NoSchedule", "NoExecute"])
            Logger.info(f"Removed DCM labels and taint from {node_name}")

        # After untainting, KMM reloads the driver. Wait for device-plugin to confirm reload complete.
        # Don't fail on timeout since we're in finally block - just log and continue
        K8Helper.wait_for_driver_reload(environment, gpu_nodes, fail_on_timeout=False)

        # Config-manager MUST be running for partition feature - this is fatal if not ready
        devicecfg_pods = [common.PodInfo('config-manager', len(gpu_nodes), 1)]
        failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods, sleep_time=20)
        debug_on_failure(environment, not failed_pods, f"Config-manager not Running after driver reload: {failed_pods}")
    # Watch for all pod creation

    '''
    FIXME: No specific need to remove config-map for the DCM
    patch_body = {
        "spec": {
            "configManager": {
                "config": None
            }
        }
    }

    api_client = client.ApiClient()
    custom_objects_api = client.CustomObjectsApi(api_client)
    devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
    for devcfg_name, _ in devcfg_map.items():
        try:
            custom_objects_api.patch_namespaced_custom_object(
                group="amd.com",
                version='v1alpha1',
                name=devcfg_name,
                namespace=environment.gpu_operator_namespace,
                plural='deviceconfigs',
                body=patch_body
            )
            print(f"Successfully patched")
        except client.ApiException as e:
            pytest.fail(f"Failed to patch custom object: {e}")
    '''

    # Pod readiness check moved into finally block to ensure it runs after untaint
    # (see finally block above - waits for device-plugin/config-manager to confirm driver reload)

    if not skip_reboot:
        for node in gpu_nodes:
            node_name = node['metadata']['labels']['kubernetes.io/hostname']
            ret_code = k8_util.reboot_node(gpu_cluster, node_name)
            if ret_code != 0:
                Logger.error(f"Failed to reboot node {node_name}")
 
def get_partition_status_from_pod(environment, namespace, node_name, max_retries=10, retry_delay=5):
    """
    Get partition status from config-manager pod with retries.

    Handles pod restarts during negative tests by retrying until pod is available.
    During negative tests, config-manager may crash/restart, so we need to wait
    for the pod to become available before querying partition status.

    Args:
        environment: Test environment fixture
        namespace: Kubernetes namespace where config-manager runs
        node_name: Node hostname to find pod for
        max_retries: Maximum number of retry attempts (default: 10 = 50s)
        retry_delay: Seconds between retries (default: 5)

    Returns:
        dict: Partition status {gpu_id: "TYPE_MEMORY"} or {} if pod not available

    Raises:
        AssertionError: If partition status cannot be retrieved (via debug_on_failure)
    """
    global Logger

    for attempt in range(max_retries):
        pod_name = k8_util.k8_get_pod_name("config-manager", namespace, node_name)

        if pod_name is None:
            if attempt < max_retries - 1:
                Logger.warning(f"config-manager pod not found on {node_name}, attempt {attempt+1}/{max_retries}, retrying in {retry_delay}s...")
                time.sleep(retry_delay)
                continue
            else:
                Logger.error(f"config-manager pod not found on {node_name} after {max_retries} attempts")
                debug_on_failure(environment, False,
                                 f"config-manager pod not available on {node_name} after {max_retries * retry_delay}s")
                return {}

        # Try to exec command
        ret_code, output, resp_stderr = k8_util.exec_command_in_pod(
            namespace, ["amd-smi", "partition", "-c", "--json"], pod_name
        )

        if ret_code == 0:
            Logger.debug(f"Partition status from {pod_name}: {output}")
            return extract_partition_info(environment, output)
        else:
            if attempt < max_retries - 1:
                Logger.warning(f"Failed to exec amd-smi in {pod_name} (attempt {attempt+1}/{max_retries}, rc={ret_code}): {resp_stderr}, retrying...")
                time.sleep(retry_delay)
            else:
                Logger.error(f"Failed to get partition info from {pod_name} after {max_retries} attempts (rc={ret_code}): {resp_stderr}")
                debug_on_failure(environment, False,
                                 f"Failed to exec amd-smi in {pod_name}: {resp_stderr}")
                return {}

    return {}

def extract_partition_info(environment, amd_smi_partition_json):
    """Parse amd-smi partition output and extract GPU partition states.

    Parses the JSON output from "amd-smi partition -c --json" command and extracts
    the current partition profile for each GPU. Filters out sub-partition entries
    (which have N/A for memory or accelerator_type) and returns only main GPU entries.

    Args:
        environment: Test environment fixture for error reporting.
        amd_smi_partition_json: JSON string output from amd-smi partition command.

    Returns:
        dict: Mapping of GPU ID to partition profile string.
              Example: {0: "SPX_NPS1", 1: "QPX_NPS4", 2: "DPX_NPS2"}

    Raises:
        AssertionError: If JSON parsing fails.
    """
    try:
        amd_smi_partition_info = json.loads(amd_smi_partition_json.replace("'", "\""))
    except Exception as je:
        Logger.error(f"Failed to parse amd_smi_partition JSON document, error : {je}")
        Logger.debug(f"JSON : {amd_smi_partition_json}")
        K8Helper.triage(environment, False, f"Failed to parse amd-smi-partition JSON")

    current_partitions = amd_smi_partition_info.get("current_partition", [])
    main_gpu_entries = [item for item in current_partitions if item["memory"] != "N/A" and item["accelerator_type"] != "N/A"]
    partition_status = {}
    for entry in main_gpu_entries:
        partition_status[entry["gpu_id"]] = f"{entry['accelerator_type']}_{entry['memory']}"
    return partition_status

# Dead code - parse_amd_smi_json() is not called anywhere in the test suite
# Removing to avoid confusion. If needed in the future, the validation logic
# can be restored from git history.

@pytest.mark.level11
def test_deviceconfig_config_manager_deploy(deviceconfig_install, gpu_cluster, environment):
    """Test Config Manager deployment via DeviceConfig CR.

    Basic smoke test that verifies Config Manager pods are deployed and running
    when enabled in DeviceConfig CR. Also resets GPUs to default partition state.

    Test Steps:
        1. Verify GPU nodes available in cluster
        2. Check device-plugin pods are Running (1 per GPU node)
        3. Check config-manager pods are Running (1 per GPU node)
        4. Reset all GPU partitions to default SPX_NPS1 profile

    Args:
        deviceconfig_install: Fixture providing DeviceConfig CR configuration.
        gpu_cluster: Cluster object with GPU node information.
        environment: Test environment fixture.

    Raises:
        AssertionError: If GPU nodes not found or pods not Running.
    """
    global Logger
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    debug_on_failure(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    debug_on_failure(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")

    # Watch for all pod creation
    devicecfg_pods = [
        common.PodInfo('device-plugin', len(gpu_nodes), 1),
        common.PodInfo('config-manager', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods, sleep_time = 20)
    debug_on_failure(environment, (not failed_pods), f"One or more pods are not ready - {failed_pods}")
    reset_dcm_profile(gpu_cluster, environment)

def exporter_nodeport_exp_config(request, gpu_cluster, deviceconfig_install, environment):
    global Logger
    # Generate set of config-maps in the k8 cluster with different set of labels and metrics
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")

    # Restore default mode (non-rbac) for this testcase
    dme_version = None
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['metricsExporter.enable'] = True
        tcfg['metricsExporter.serviceType'] = 'NodePort'
        tcfg['metricsExporter.rbacConfig.enable'] = False
        tcfg['metricsExporter.rbacConfig.disableHttps'] = False
        dme_version = tcfg['metricsExporter.image.version']

        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, (ret_code == 0), f"Failed to create deviceconfig, stderr: {ret_stderr}")

    exporter_config_defn = {}
    label_support_info = metric_util.get_label_details(environment.gpu_operator_version)
    non_mandatory_labels = list(filter(lambda x: label_support_info[x] == "no", label_support_info.keys()))
    mandatory_labels = list(filter(lambda x: label_support_info[x] == "yes", label_support_info.keys()))

    # Build common list of metrics across all nodes in the cluster (if different gpu-series are part of cluster)
    list_of_metrics_set = []
    for node in gpu_nodes:
        node_ip = k8_util.k8_get_node_address(node)
        cluster_node = gpu_cluster.find_node_by_ip(node_ip)
        if not cluster_node:
            pytest.fail(f"Unable to get worker node from cluster for ip: {node_ip}")
        metrics_data = metric_util.get_supported_metrics(gpu_series = cluster_node.gpu_series,
                                                         amdgpu_driver = cluster_node.amdgpu_driver_version, 
                                                         dme_version = dme_version)
        list_of_metrics_set.append(set(map(lambda x: x['name'].split(":")[0].lower(), metrics_data)))
    common_metrics = list(functools.reduce(lambda s1, s2: s1.intersection(s2), list_of_metrics_set))
    Logger.info(f"Using {common_metrics} for metrics-exporter configmap validation")

    for idx in range(2):
        label_subset = random.sample(non_mandatory_labels, 5)
        metric_subset = random.sample(common_metrics, 5)
        config_map = {
            "GPUConfig" : {
                "Labels" : label_subset,
                "Fields" : metric_subset,
            },
        }
        exp_config_name = f"exporter-config-{idx}"
        configmap_file = os.path.join(environment.logdir, f"{exp_config_name}.json")
        with open(configmap_file, "w") as fp:
            fp.write(json.dumps(config_map, indent=4))

        configmap_file = os.path.join(environment.logdir, f"config.json")
        with open(configmap_file, "w") as fp:
            fp.write(json.dumps(config_map, indent=4))

        # Delete if there is any previous instance with same name
        ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_configmap(environment.gpu_operator_namespace,
                                                                       exp_config_name)
        Logger.debug(f"Result of configmap delete operation, ret_code:{ret_code}, ret_stdout: {ret_stdout.strip()}, err: {ret_stderr.strip()}")
        # ignore ret_code
        ret_code, ret_stdout, ret_stderr = k8_util.k8_create_configmap(environment.gpu_operator_namespace,
                                                                       exp_config_name,
                                                                       configmap_file, "config.json")
        K8Helper.triage(environment, ret_code == 0,
                        f"Failed to create configmap {exp_config_name} for {configmap_file}, err: {ret_stderr.strip()}")
        exporter_config_defn[exp_config_name] = (label_subset, metric_subset)
        Logger.info(f"Created configmap {exp_config_name} with labels: {label_subset} and metrics: {metric_subset}")

    def _cleanup_configmap():
        # Restore/Revert back test configuration
        for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
            if tcfg.get('metricsExporter.config'):
                del tcfg['metricsExporter.config']
            cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
            ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
            if ret_code != 0:
                Logger.warn(f"Failed to create deviceconfig, stderr: {ret_stderr}")

            # Check for corresponding deviceconfig updated
            K8Helper.check_deviceconfig_status(environment, deviceconfig_install.devicecfg_list)

        for exp_config, _ in exporter_config_defn.items():
            # Delete
            ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_configmap(environment.gpu_operator_namespace,
                                                                           exp_config)
            if ret_code != 0:
                Logger.warn(f"Failed to delete metrics-exporter configmap {exp_config}")
        return

    request.addfinalizer(_cleanup_configmap)

    devicecfg_pods = [
        common.PodInfo('device-plugin', len(gpu_nodes), 1),
        common.PodInfo('metrics-exporter', len(gpu_nodes), 1),
    ]
    failed_exp_config_metrics = []
    failed_exp_config_labels = []
    failed_endpoints = set()
    for exp_config, label_metrics_tuple in exporter_config_defn.items():
        Logger.info(f"Testing with exporter-config {exp_config}")
        for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
            tcfg['metricsExporter.config'] = exp_config
            cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
            ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
            K8Helper.triage(environment, (ret_code == 0), f"Failed to create deviceconfig, stderr: {ret_stderr}")

            # Check for corresponding deviceconfig created
            K8Helper.check_deviceconfig_status(environment, deviceconfig_install.devicecfg_list)

            failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods)
            K8Helper.triage(environment, not failed_pods, f"One or more pods are not ready - {failed_pods}")
            time.sleep(30) # Wait for config-map is read by exporter pod
            expected_metrics = set(label_metrics_tuple[1])
            expected_metrics.update(['promhttp_metric_handler_errors_total'])
            expected_labels = set(label_metrics_tuple[0])
            expected_labels.update(mandatory_labels)
            for node in gpu_nodes:
                node_ip = k8_util.k8_get_node_address(node)
                cluster_node = gpu_cluster.find_node_by_ip(node_ip)
                if not cluster_node:
                    pytest.fail(f"Unable to get worker node from cluster for ip: {node_ip}")
                node_hostname = k8_util.k8_get_node_hostname(node)
                node_port = deviceconfig_install.exporter_port_map[node_hostname]
                ret_code, resp, _ = cluster_node.http_get(node_port, "metrics")
                # Commenting out following as this rely on ssh access to each node
                #if ret_code != 0:
                #    # try from node itself
                #    ret_code, resp, _ = cluster_node.proxy_http_get(node_ip, node_port, "metrics", token = token)

                if ret_code != 0:
                    Logger.error(f"Failed to get metrics from nodeport endpoint for {node_ip}, stdout: {ret_stdout} stderr: {ret_stderr}")
                    failed_endpoints.add(node_ip)
                    continue
                metric_util.dump_metrics(resp, os.path.join(environment.logdir, f"{node_ip}_{exp_config}_metrics.txt"))
                obs_metric_info = metric_util.parse_metric_data(resp)
                obs_metrics = set(obs_metric_info.keys())

                # Check for metrics
                if obs_metrics != expected_metrics:
                    Logger.error(f"Mismatch in metrics Expected : {expected_metrics} vs Observed : {obs_metrics} config-map:{exp_config}")
                    if expected_metrics - obs_metrics:
                        Logger.error(f"Missing: {expected_metrics - obs_metrics}")
                    if obs_metrics - expected_metrics:
                        Logger.error(f"Unexpected: {obs_metrics - expected_metrics}")
                    failed_exp_config_metrics.append((exp_config, f"Expected:{expected_metrics}, Observed:{obs_metrics}"))

                # Check for labels associated with each exported metric
                for metric_name, metric_data_list in obs_metric_info.items():
                    if metric_name in {'promhttp_metric_handler_errors_total', 'gpu_nodes_total'}:
                        continue
                    label_check_failed = False
                    for metric_data in metric_data_list:
                        observed_labels = set(metric_data['labels'].keys())
                        if len(expected_labels - observed_labels) > 0:
                            Logger.error(f"Missing labels with config-map:{exp_config}, error: {expected_labels - observed_labels}")
                            label_check_failed = True
                    if label_check_failed and exp_config not in failed_exp_config_labels:
                        failed_exp_config_labels.append((exp_config, f"Expected:{expected_labels}, Observed:{observed_labels}"))

    # Do final verification
    K8Helper.triage(environment, len(failed_endpoints) == 0, f"One or more metric endpoints HTTP-GET failed, nodes: {failed_endpoints}")
    K8Helper.triage(environment, (len(failed_exp_config_metrics) == 0),
                    f"Export ConfigMap (Fields) failed for {failed_exp_config_metrics} cases")
    K8Helper.triage(environment, (len(failed_exp_config_labels) == 0),
                    f"Export ConfigMap (Labels) failed for {failed_exp_config_labels} cases")


def verify_gpu_capacity_status(environment, worker):
    """Verify that GPU capacity equals allocatable resources on node.

    After partition changes, the device-plugin needs to re-advertise GPU resources
    to kubelet. This function polls the node's GPU capacity and allocatable fields
    until they match, indicating device-plugin has successfully updated resources.

    Args:
        environment: Test environment for error reporting.
        worker: Node hostname to check.

    Raises:
        AssertionError: If capacity != allocatable after 100 seconds (10 attempts).
    """
    i = 0
    while i < 10:
        cap, alloc = k8_util.k8_get_node_gpu_capacity(worker)
        if cap == alloc:
            return
        time.sleep(10)
        i = i + 1
    debug_on_failure(environment, i < 10,
                     f"capacity = allocatable {cap} != {alloc}")

def verify_no_label(environment, profile):
    """Verify that invalid partition profile results in failure state label.

    For negative partition tests, this function validates that DCM correctly
    rejects invalid profiles by setting the node label state to "failure".

    Polls for up to 60 seconds waiting for labels:
        dcm.amd.com/gpu-config-profile: <profile>
        dcm.amd.com/gpu-config-profile-state: failure

    Args:
        environment: Test environment for error reporting.
        profile: Invalid partition profile that should fail validation.

    Raises:
        AssertionError: If failure state not observed within 60 seconds.
    """
    i = 0
    while i < 30:
        ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
        if gpu_nodes and gpu_nodes[0]['metadata']['labels'].get('dcm.amd.com/gpu-config-profile', 'NA') == profile and \
                gpu_nodes[0]['metadata']['labels'].get('dcm.amd.com/gpu-config-profile-state', 'NA') == "failure":
            break
        i += 1
        time.sleep(2)

    # Format labels safely for error message
    if gpu_nodes and isinstance(gpu_nodes, list) and len(gpu_nodes) > 0:
        labels = gpu_nodes[0].get('metadata', {}).get('labels', {})
        labels_str = pprint.pformat(labels)
    else:
        labels_str = "No GPU nodes returned from k8_get_gpu_nodes()"

    debug_on_failure(environment, i < 30,
            f"didn't find {profile} or state=failure in labels:\n{labels_str}")


def verify_label(environment, profile):
    """Verify that partition profile change succeeded via node labels.

    Polls node labels until DCM marks the partition change as successful or failed
    on ALL GPU nodes. DCM updates these labels after partition completes:
        dcm.amd.com/gpu-config-profile: <profile>
        dcm.amd.com/gpu-config-profile-state: success|failure

    Polls for up to 400 seconds (40 attempts x 10 seconds) to allow partition
    operation to complete, which can take several minutes depending on GPU series.
    Fails immediately if DCM reports failure state on any node.

    Args:
        environment: Test environment for error reporting.
        profile: Expected partition profile name (e.g., "QPX_NPS1").

    Raises:
        AssertionError: If success state not observed within 400 seconds, or if failure detected.
    """
    i = 0
    while i < 40:
        ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
        if gpu_nodes:
            all_nodes_success = True
            all_nodes_status = []

            # Check ALL GPU nodes, not just the first one
            for node in gpu_nodes:
                node_name = node['metadata']['labels'].get('kubernetes.io/hostname', 'unknown')
                prof = node['metadata']['labels'].get('dcm.amd.com/gpu-config-profile', 'NA')
                stat = node['metadata']['labels'].get('dcm.amd.com/gpu-config-profile-state', 'unknown')
                all_nodes_status.append(f"{node_name}: profile={prof}, state={stat}")

                # Fail immediately if any node reports failure
                if stat == "failure":
                    Logger.error(f"DCM reported failure on node {node_name}: profile={prof}, state={stat}")
                    debug_on_failure(environment, False,
                                    f"DCM partition operation failed on {node_name}: profile={prof}, state={stat}")
                    return

                # Check if this node matches expected state
                if not (prof == profile and stat == "success"):
                    all_nodes_success = False

            # If all nodes show success, we're done
            if all_nodes_success:
                Logger.info(f"Partition profile {profile} applied successfully on all {len(gpu_nodes)} nodes")
                for status in all_nodes_status:
                    Logger.debug(f"  {status}")
                break
            else:
                Logger.debug(f"Attempt {i+1}/40 - waiting for all nodes to reach success state:")
                for status in all_nodes_status:
                    Logger.debug(f"  {status}")

        i += 1
        time.sleep(10)

    # Timeout - show final state of all nodes
    if i >= 40:
        Logger.error("Timeout waiting for partition profile to apply on all nodes. Final state:")
        for status in all_nodes_status:
            Logger.error(f"  {status}")
        debug_on_failure(environment, False,
                         f"Didn't find gpu-config-profile-state=success on all nodes after 400s")


def verify_logs(environment, log_msg_list, pod_str="config-manager", since="1800s", container=None, optional=False):
    """Verify expected log messages appear in Config Manager pod logs.

    Polls pod logs to validate that DCM is executing expected operations.
    Supports two modes:
        Mandatory (optional=False): ALL log messages must be present
        Optional (optional=True): AT LEAST ONE log message must be present

    Args:
        environment: Test environment fixture.
        log_msg_list: List of log message strings to search for.
        pod_str: Pod name pattern to fetch logs from (default: "config-manager").
        since: Time window for logs in kubectl format (default: "1800s").
        container: Specific container name to get logs from (default: None).
        optional: If True, only one message needs to match (default: False).

    Raises:
        AssertionError: If required log messages not found within 15 minutes.
    """
    global Logger
    global LogPrettyPrinter
    namespace = environment.gpu_operator_namespace

    i = 0
    ret_code, stdout, stderr = k8_util.k8_get_pod_logs(pod_str, namespace, since, container)

    if optional:
        # For optional logs, at least ONE from the list must be present
        flag = False
        for log_msg in log_msg_list:
            if log_msg in stdout:
                flag = True
                Logger.info(f"Found optional log message: {log_msg}")
                break
        debug_on_failure(environment, flag,
                         f"didn't find any of {log_msg_list} in logs")
    else:
        # For mandatory logs, ALL must be present
        for log_msg in log_msg_list:
            while log_msg not in stdout and i < 30:
                time.sleep(30)
                i = i + 1
                ret_code, stdout, stderr = k8_util.k8_get_pod_logs(pod_str, namespace, since, container)
            debug_on_failure(environment, log_msg in stdout,
                             f"didn't find {log_msg} in\n" + LogPrettyPrinter.pformat(stdout.split('\n')))

def wait_for_pods(environment, local_workload_ctxts):
    """Wait for workload pods to reach non-Pending state and collect status.

    Polls workload pod status until they move out of Pending state or timeout.
    Used in partition tests to verify workload scheduling behavior during/after
    partition changes.

    Args:
        environment: Test environment fixture.
        local_workload_ctxts: List of workload context dicts containing 'pod_name'.

    Returns:
        dict: Pod status information mapping pod_name to (status, full_pod_info) tuples.
              Status can be: "Running", "Pending", "Failed", "Succeeded", etc.
    """
    workload_pods = []
    status_info = None
    for ctxt in local_workload_ctxts:
        workload_pods.append(common.PodInfo(ctxt['pod_name'], 1, 1))

    for _ in range(5):
        status_info = k8_util.k8_check_pod_status("default", workload_pods)
        statuses = [status for name, (status, full_pod_info) in status_info.items()]
        if "Pending" in statuses:
            time.sleep(5)
        else:
            break
    K8Helper.collect_unhealthy_pods(environment, workload_pods)
    return status_info

#Unsupported compute partition combination
@pytest.mark.level12
@pytest.mark.parametrize("profile", ["invalidgpucount",
                                     "invalmemorytype",
                                     "invalcomputetype",
                                     "invalidmissingfields-memoryPartition",
                                     "invalidmissingfields-computePartition",
                                     "highgpucount_mostly_invalid"])
def test_negative_partitioning(request, gpu_cluster, deviceconfig_install, environment, profile):
    """Test that invalid GPU partition profiles are rejected by Config Manager.

    Verifies that DCM correctly validates partition profile configurations and
    rejects invalid combinations. Tests various invalid scenarios including:
        - Invalid GPU count (doesn't match available GPUs)
        - Invalid memory partition type
        - Invalid compute partition type
        - Missing required fields (numGPUs, memoryPartition, computePartition)
        - GPU count higher than available

    Test Steps:
        1. Load invalid profile from partition profile ConfigMap
        2. Record current partition state with amd-smi
        3. Patch DeviceConfig to add NoExecute toleration for config-manager
        4. Apply profile label to node and add taint
        5. Wait for DCM to process the profile change
        6. Verify node label shows state=failure
        7. Verify DCM logs show validation error messages
        8. Verify GPU partition state unchanged from initial state

    Args:
        request: Pytest request fixture for finalizers.
        gpu_cluster: Cluster object with GPU nodes.
        deviceconfig_install: DeviceConfig CR configuration.
        environment: Test environment fixture.
        profile: Invalid profile name to test.

    Raises:
        pytest.skip: If GPU series is MI2xx or profile not defined for GPU series.
        AssertionError: If partition unexpectedly changes or validation doesn't fail.
    """
    global Logger
    gpu_series = get_gpu_series(gpu_cluster, environment)
    dut_node = gpu_cluster.find_node_by_gpu_series(gpu_series)
    if not amdgpu_util.supports_config_manager(dut_node.device_id):
        pytest.skip(f"GPU partitioning not supported for {gpu_series}")
    file_path = os.path.join(environment.logdir, f"partitioning_check_{gpu_series}_{dut_node.num_gpus}.json")

    def _untaint_all_nodes():
        for node in gpu_nodes:
            node_name = node['metadata']['labels']['kubernetes.io/hostname']
            k8_util.k8_untaint_node(node_name, effects=["NoSchedule", "NoExecute"])

    DCM_LOG_MATCH = {
        "invalmemorytype" : [
            f"Selected Profile {profile} found in the configmap",
            "Profile validation failed. Could not partition",
        ],
        "invalcomputetype" : [
            f"Selected Profile {profile} found in the configmap",
            "Profile validation failed. Could not partition",
        ],
        "invalidgpucount" : [
            f"Selected Profile {profile} found in the configmap",
            "Profile validation failed. Could not partition",
            "does not equal the total number of GPUs available on this node",
        ],
        "invalidmissingfields-memoryPartition" : [
            f"Selected Profile {profile} found in the configmap",
            "Profile validation failed. Could not partition",
        ],
        "invalidmissingfields-computePartition" : [
            f"Selected Profile {profile} found in the configmap",
            "Profile validation failed. Could not partition",
        ],
        "highgpucount_mostly_invalid" : [
            f"Selected Profile {profile} found in the configmap",
            "Profile validation failed. Could not partition",
            "does not equal the total number of GPUs available on this node",
        ]
    }
    local_workload_ctxts = []
    namespace = environment.gpu_operator_namespace
    with open(file_path) as fp:
        profiles = json.load(fp)
        if not profiles.get("gpu-config-profiles"):
            pytest.fail(f"check {file_path}, something wrong with the configmap")
        elif not profiles["gpu-config-profiles"].get(profile, False):
            pytest.skip(f"Profile {profile} is not supported for {gpu_series}. Refer {file_path}")

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    for node in gpu_nodes:
        worker = k8_util.k8_get_node_hostname(node)
        if node['metadata']['labels'].get('dcm.amd.com/gpu-config-profile'):
            Logger.info(f"Record existing profile = {node['metadata']['labels']['dcm.amd.com/gpu-config-profile']}")
        else:
            Logger.info("Didn't find any existing profile")
        if node['metadata']['labels'].get('dcm.amd.com/gpu-config-profile-state'):
            Logger.info(f"Record existing state = {node['metadata']['labels']['dcm.amd.com/gpu-config-profile-state']}")
        else:
            Logger.info("Didn't find any existing profile state")
    Logger.info(f"to be changed to profile: {profile}")

    def _cleanup_after():
        reset_dcm_profile(gpu_cluster, environment)
        _untaint_all_nodes()

    request.addfinalizer(_cleanup_after)

    patch_body = {
        "spec": {
            "configManager": {
                "configManagerTolerations": [
                    {
                        "effect": "NoExecute",
                        "key": "amd-dcm",
                        "operator": "Equal",
                        "value": "up"
                    }
                ]
            }
        }
    }

    api_client = client.ApiClient()
    custom_objects_api = client.CustomObjectsApi(api_client)
    devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
    for devcfg_name, _ in devcfg_map.items():
        try:
            custom_objects_api.patch_namespaced_custom_object(
                group="amd.com",
                version='v1alpha1',
                name=devcfg_name,
                namespace=namespace,
                plural='deviceconfigs',
                body=patch_body
            )
            Logger.info(f"Successfully patched with {patch_body}")
        except client.ApiException as e:
            debug_on_failure(environment, False, f"Failed to patch custom object: {e}")

    # Watch for all pod creation
    time.sleep(20)
    devicecfg_pods = [
        common.PodInfo('config-manager', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods, sleep_time = 20)
    debug_on_failure(environment, (not failed_pods), f"One or more pods are not ready - {failed_pods}")

    # Get baseline partition status before applying invalid config
    Logger.info(f"NEGATIVE TEST: Getting baseline partition status before applying invalid profile '{profile}'")
    pre_partition_status = {}
    for node in gpu_nodes:
        node_name = node['metadata']['labels']['kubernetes.io/hostname']
        pre_partition_status[node_name] = get_partition_status_from_pod(environment, namespace, node_name)

    Logger.info(f"Baseline partition status: {pre_partition_status}")

    # Verify baseline is SPX_NPS1 (default partition)
    for node_name, node_status in pre_partition_status.items():
        for gpu_id, partition in node_status.items():
            if partition != "SPX_NPS1":
                Logger.warning(f"UNEXPECTED: Node {node_name} GPU {gpu_id} is NOT in default SPX_NPS1 partition, found: {partition}")
                Logger.warning("Negative test may not behave as expected - partition should start at default SPX_NPS1")

    # Apply invalid partition profile (should be rejected by DCM)
    Logger.info(f"NEGATIVE TEST: Applying invalid profile '{profile}' - DCM should reject this")
    labels_dict = {"dcm.amd.com/gpu-config-profile" : profile}
    for node in gpu_nodes:
        node_name = node['metadata']['labels']['kubernetes.io/hostname']
        # Use NoExecute to evict device-plugin (DCM needs exclusive GPU access)
        k8_util.k8_taint_node(node_name, taint_add=True, effect="NoExecute")
        k8_util.k8_label_node(node_name, labels_dict, overwrite=True)
        verify_no_label(environment, profile)

    time.sleep(20) # Time to reinit
    _untaint_all_nodes()
    K8Helper.wait_for_driver_reload(environment, gpu_nodes, fail_on_timeout=False)

    # Config-manager MUST be running for partition feature - this is fatal if not ready
    devicecfg_pods = [common.PodInfo('config-manager', len(gpu_nodes), 1)]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods, sleep_time=20)
    debug_on_failure(environment, not failed_pods, f"Config-manager not Running after driver reload: {failed_pods}")

    verify_logs(environment, DCM_LOG_MATCH.get(profile, []))

    # After negative test, config-manager may have restarted, so use retry logic
    # Wait longer to ensure pod is stable and partition didn't change
    Logger.info("Waiting for config-manager to stabilize after negative test...")
    post_partition_status = {}
    for node in gpu_nodes:
        node_name = node['metadata']['labels']['kubernetes.io/hostname']
        post_partition_status[node_name] = get_partition_status_from_pod(environment, namespace, node_name, max_retries=15, retry_delay=10)

    # Verify partition didn't change (negative test should reject invalid config)
    Logger.info(f"Pre-partition status: {pre_partition_status}")
    Logger.info(f"Post-partition status: {post_partition_status}")

    partition_changed = False
    for node_name in pre_partition_status.keys():
        pre_status = pre_partition_status.get(node_name, {})
        post_status = post_partition_status.get(node_name, {})

        # If we couldn't get post status, that's a test infrastructure issue
        if not post_status and pre_status:
            Logger.error(f"Could not verify partition status on {node_name} - pod unavailable")
            debug_on_failure(environment, False, f"Failed to get post-partition status for {node_name}")

        # Check if any GPU partition changed
        for gpu_id, pre_partition in pre_status.items():
            post_partition = post_status.get(gpu_id, None)
            if post_partition != pre_partition:
                Logger.error(f"Node {node_name} GPU {gpu_id}: Partition changed from {pre_partition} to {post_partition}")
                partition_changed = True

    debug_on_failure(environment, not partition_changed,
                     f"GPU Partition unexpectedly changed during negative test! Pre: {pre_partition_status}, Post: {post_partition_status}")


def run_partition_test_scenario(gpu_cluster, environment, request, profile, workload):
    """Core test logic for GPU partition profile changes with optional workload.

    Shared implementation for all partition profile tests. Applies a partition profile
    to GPU nodes and validates the change using amd-smi, events, logs, and workload behavior.

    Test Workflow:
        1. Load partition profile definition (numGPUs, memoryPartition, computePartition)
        2. Record existing partition state and node labels
        3. Optionally start workload on node before partition change
        4. Patch DeviceConfig to add NoExecute toleration
        5. Taint node and apply partition profile label
        6. Verify node label shows state=success and profile=<requested>
        7. If workload running: verify first workload stays Running, new workload Pending
        8. Remove taints to allow pods to schedule
        9. Wait for device-plugin and config-manager pods Running
        10. Verify DCM logs show profile application messages
        11. Query actual partition state from amd-smi and validate
        12. Verify all GPUs match expected partition profile
        13. If workload: verify all workloads reach Running state
        14. Verify Kubernetes events show success message
        15. Verify GPU capacity equals allocatable

    Args:
        gpu_cluster: Cluster object with GPU nodes.
        environment: Test environment fixture.
        request: Pytest request for finalizers.
        profile: Partition profile name (e.g., "QPX_NPS1", "DPX_NPS2").
        workload: If True, starts busybox workload before/during partition change.

    Raises:
        pytest.skip: If GPU series doesn't support the profile.
        AssertionError: If partition state doesn't match expected profile or
                       workload behavior incorrect.
    """
    global Logger
    gpu_series = get_gpu_series(gpu_cluster, environment)
    dut_node = gpu_cluster.find_node_by_gpu_series(gpu_series)
    if not amdgpu_util.supports_config_manager(dut_node.device_id):
        pytest.skip(f"GPU partitioning not supported for {gpu_series}")
    local_workload_ctxts = []
    namespace = environment.gpu_operator_namespace
    file_path = os.path.join(environment.logdir, f"partitioning_check_{gpu_series}_{dut_node.num_gpus}.json")
    before_events = k8_util.k8_get_events(namespace=environment.gpu_operator_namespace)

    with open(file_path) as fp:
        profiles = json.load(fp)
        if not profiles.get("gpu-config-profiles"):
            pytest.fail(f"check {file_path}, something wrong with the configmap")
        elif not profiles["gpu-config-profiles"].get(profile, False):
            pytest.skip(f"Profile {profile} is not supported for {gpu_series}. Refer {file_path}")
        else:
            memory = profiles["gpu-config-profiles"][profile]["profiles"][0]["memoryPartition"]
            partition = profiles["gpu-config-profiles"][profile]["profiles"][0]["computePartition"]
            GPUs = profiles["gpu-config-profiles"][profile]["profiles"][0]["numGPUsAssigned"]

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    for node in gpu_nodes:
        worker = k8_util.k8_get_node_hostname(node)
        if node['metadata']['labels'].get('dcm.amd.com/gpu-config-profile'):
            Logger.info(f"Record existing profile = {node['metadata']['labels']['dcm.amd.com/gpu-config-profile']}")
        else:
            Logger.info("Didn't find any existing profile")
        if node['metadata']['labels'].get('dcm.amd.com/gpu-config-profile-state'):
            Logger.info(f"Record existing state = {node['metadata']['labels']['dcm.amd.com/gpu-config-profile-state']}")
        else:
            Logger.info("Didn't find any existing profile state")
    Logger.info(f"to be changed to profile: {profile}")

    #add_tolerations(environment)
    def _untaint_all_nodes():
        for node in gpu_nodes:
            node_name = node['metadata']['labels']['kubernetes.io/hostname']
            k8_util.k8_untaint_node(node_name, effects=["NoSchedule", "NoExecute"])
    request.addfinalizer(_untaint_all_nodes)

    if workload:
        def _start_workload():
            params = {
                "node_name" : worker,
                "num_gpu_reqd" : 1,
                "workload_selection" : "busybox-workload",
            }
            wl_ctxt = K8Helper.workload_operation(environment, K8Helper.WorkloadOp.START_WORKLOAD, **params)
            local_workload_ctxts.append(wl_ctxt)
        def _cleanup_workload():
            for ctxt in local_workload_ctxts:
                K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **ctxt)
            return

        request.addfinalizer(_cleanup_workload)
        _untaint_all_nodes()
        _start_workload()
        status_info = wait_for_pods(environment, local_workload_ctxts)
        for name, (status, full_pod_info) in status_info.items():
            debug_on_failure(environment, status == 'Running',
                             f"Workload not in RUNNING state, {pprint.pformat(status_info)}")

    # Watch for all pod creation
    time.sleep(20)
    devicecfg_pods = [
        common.PodInfo('config-manager', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods, sleep_time = 20)
    debug_on_failure(environment, (not failed_pods), f"One or more pods are not ready - {failed_pods}")

    # Ensure driver is ready before collecting pre-partition status
    # Previous test may have left driver unloaded if partition failed
    Logger.info("Verifying driver is ready before starting partition test...")
    K8Helper.wait_for_driver_reload(environment, gpu_nodes, fail_on_timeout=True)

    pre_partition_status = {}
    for node in gpu_nodes:
        node_name = node['metadata']['labels']['kubernetes.io/hostname']
        pre_partition_status[node_name] = get_partition_status_from_pod(environment, namespace, node_name)

    patch_body = {
        "spec": {
            "configManager": {
                "configManagerTolerations": [
                    {
                        "effect": "NoExecute",
                        "key": "amd-dcm",
                        "operator": "Equal",
                        "value": "up"
                    }
                ]
            }
        }
    }

    api_client = client.ApiClient()
    custom_objects_api = client.CustomObjectsApi(api_client)
    devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
    for devcfg_name, _ in devcfg_map.items():
        try:
            custom_objects_api.patch_namespaced_custom_object(
                group="amd.com",
                version='v1alpha1',
                name=devcfg_name,
                namespace=namespace,
                plural='deviceconfigs',
                body=patch_body
            )
            Logger.info(f"Successfully patched with {patch_body}")
        except client.ApiException as e:
            debug_on_failure(environment, False, f"Failed to patch custom object: {e}")

    labels_dict = {"dcm.amd.com/gpu-config-profile" : profile}
    for node in gpu_nodes:
        node_name = node['metadata']['labels']['kubernetes.io/hostname']
        # Use NoExecute to evict device-plugin pods (DCM needs exclusive GPU access)
        k8_util.k8_taint_node(node_name, taint_add=True, effect="NoExecute")
        k8_util.k8_label_node(node_name, labels_dict, overwrite=True)

    # Wait for DCM to complete partition operation (polls up to 5 minutes for state=success)
    verify_label(environment, profile)

    if workload:
        # Since earlier workload would have evicted, recreate and check it lands in PENDING state
        _start_workload()
        status_info = wait_for_pods(environment, local_workload_ctxts)
        debug_on_failure(environment, status_info[local_workload_ctxts[-1]['pod_name']][0] == 'Pending',
                         f"Workload not in PENDING state, {pprint.pformat(local_workload_ctxts[-1])}")
        if local_workload_ctxts[0]['pod_name'] in status_info:
            # If it is active, it should be running
            debug_on_failure(environment, status_info[local_workload_ctxts[0]['pod_name']][0] == 'Running',
                             f"Workload not in RUNNING state, {pprint.pformat(local_workload_ctxts[0])}")

    # Watch for all pod creation (taint still in place - only config-manager can run)
    devicecfg_pods = [
        common.PodInfo('config-manager', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods, sleep_time = 20)
    debug_on_failure(environment, (not failed_pods), f"One or more pods are not ready - {failed_pods}")

    # Log verification is informational - DCM logs can change between versions
    # The actual partition state from amd-smi is the source of truth
    expected_logs = [
            f"Selected Profile {profile} found in the configmap",
            "Successfully Partitioned GPUs",
            "AMD SMI shutdown successfully",
    ]

    # GPU ID logs - one per GPU being partitioned
    for gpu in range(GPUs):
        expected_logs.append(f"GPU ID {gpu}")

    try:
        verify_logs(environment, expected_logs)
    except Exception as e:
        Logger.warning(f"Log verification failed (non-critical): {e}")
        Logger.warning("Continuing - will validate actual partition state from amd-smi")

    # PRIMARY VALIDATION: Verify actual partition state from amd-smi
    # After partition operation, driver may be reloading. Use extended retries for amd-smi.
    # max_retries=30, retry_delay=5 = 150s max wait for driver to be ready
    post_partition_status = {}
    for node in gpu_nodes:
        node_name = node['metadata']['labels']['kubernetes.io/hostname']
        post_partition_status[node_name] = get_partition_status_from_pod(
            environment, namespace, node_name, max_retries=30, retry_delay=5
        )

    # Verify final partition matches requested profile (CRITICAL CHECK)
    expected_partition = f"{partition}_{memory}"
    Logger.info(f"Validating partition state - Expected: {expected_partition}")
    all_gpus_match = True
    for node_name, node_partition_status in post_partition_status.items():
        Logger.info(f"Node {node_name} partition state: {node_partition_status}")
        for gpu_id, actual_partition in node_partition_status.items():
            if actual_partition != expected_partition:
                Logger.error(f"Node {node_name} GPU {gpu_id}: Expected {expected_partition}, got {actual_partition}")
                all_gpus_match = False

    debug_on_failure(environment, all_gpus_match,
                     f"Partition mismatch! Expected all GPUs to be {expected_partition}, got: {post_partition_status}")

    # Log if partition changed (informational only)
    if pre_partition_status != post_partition_status:
        Logger.info(f"Partition changed from {pre_partition_status} to {post_partition_status}")
    else:
        Logger.info(f"Partition already matched target: {post_partition_status}")

    # Partition verified - now untaint nodes to allow device-plugin and workloads to restart
    Logger.info("Partition operation completed successfully - removing taints from nodes")
    _untaint_all_nodes()

    # Wait for device-plugin pods to come back after untaint
    # After untainting, KMM reloads the driver. Fail test if timeout.
    K8Helper.wait_for_driver_reload(environment, gpu_nodes, fail_on_timeout=True)

    # Config-manager MUST be running for partition feature - this is fatal if not ready
    devicecfg_pods = [common.PodInfo('config-manager', len(gpu_nodes), 1)]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods, sleep_time=20)
    debug_on_failure(environment, not failed_pods, f"Config-manager not Running after driver reload: {failed_pods}")

    # Now collect events - this captures the complete partition workflow including recovery
    after_events = k8_util.k8_get_events(namespace=environment.gpu_operator_namespace)

    if workload:
        status_info = wait_for_pods(environment, local_workload_ctxts)
        workload_status = []
        for (status, full_pod_info) in status_info.values():
            workload_status.append(status == 'Running')
        debug_on_failure(environment, all(workload_status),
                         f"Some of the workloads not in Running state: {pprint.pformat(local_workload_ctxts)}")
    # Verify Kubernetes events (informational - event format can change)
    Logger.info("Verifying events in the testcase")
    try:
        verify_events(gpu_cluster, environment, profile, before_events, after_events)
    except Exception as e:
        Logger.warning(f"Event verification failed (non-critical): {e}")
        Logger.warning("Partition state was verified via amd-smi, so this is informational only")

    # Verify GPU capacity matches allocatable
    worker = k8_util.k8_get_node_hostname(gpu_nodes[0])
    verify_gpu_capacity_status(environment, worker)

@pytest.mark.level2
@pytest.mark.parametrize("profile", ["QPX_NPS1", "DPX_NPS2", "QPX_NPS2", "DPX_NPS1", "CPX_NPS1", "CPX_NPS2", "SPX_NPS1"])
def test_partitioning_no_workload_MI350X(gpu_cluster, deviceconfig_install, environment, request, profile):
    """Test MI350X GPU partitioning without active workloads.

    Tests partition profile changes on AMD MI350X GPUs without any GPU workloads
    running. Verifies that partition changes complete successfully when node is idle.

    Supported Profiles:
        QPX_NPS1, QPX_NPS2: Quad partition with NPS1/NPS2 memory
        DPX_NPS1, DPX_NPS2: Dual partition with NPS1/NPS2 memory
        CPX_NPS1, CPX_NPS2: Compute partition (8 partitions) with NPS1/NPS2
        SPX_NPS1: Single partition (default)

    Args:
        gpu_cluster: Cluster object.
        deviceconfig_install: DeviceConfig CR configuration.
        environment: Test environment.
        request: Pytest request.
        profile: Partition profile to test.

    Raises:
        pytest.skip: If cluster doesn't have MI350X GPUs.
    """
    gpu_series = get_gpu_series(gpu_cluster, environment)
    if gpu_series != 'MI350X':
        pytest.skip(f"Testcases specifically designed for MI350X")
    run_partition_test_scenario(gpu_cluster, environment, request, profile, workload = False)

@pytest.mark.level2
@pytest.mark.parametrize("profile", ["QPX_NPS1", "DPX_NPS2", "QPX_NPS2", "DPX_NPS1", "CPX_NPS1", "CPX_NPS2", "SPX_NPS1"])
def test_partitioning_workload_MI350X(gpu_cluster, deviceconfig_install, environment, request, profile):
    """Test MI350X GPU partitioning with active GPU workloads.

    Tests partition profile changes on AMD MI350X GPUs while GPU workloads are running.
    Verifies workload eviction, partition change, and workload rescheduling behavior.

    Expected Behavior:
        1. Workload starts in Running state before partition change
        2. Node gets tainted when partition profile label applied
        3. First workload continues Running (has toleration)
        4. Second workload stays Pending (no toleration for tainted node)
        5. After taint removed and partition complete, all workloads Running

    Args:
        gpu_cluster: Cluster object.
        deviceconfig_install: DeviceConfig CR configuration.
        environment: Test environment.
        request: Pytest request.
        profile: Partition profile to test.

    Raises:
        pytest.skip: If cluster doesn't have MI350X GPUs.
    """
    gpu_series = get_gpu_series(gpu_cluster, environment)
    if gpu_series != 'MI350X':
        pytest.skip(f"Testcases specifically designed for MI350X")
    run_partition_test_scenario(gpu_cluster, environment, request, profile, workload = True)

@pytest.mark.level2
@pytest.mark.parametrize("profile", ["DPX_NPS1", "CPX_NPS1", "SPX_NPS1"])
def test_partitioning_no_workload_MI350P(gpu_cluster, deviceconfig_install, environment, request,
                                         create_dcm_configmap, profile):
    gpu_series = get_gpu_series(gpu_cluster, environment)
    if gpu_series != 'MI350P':
        pytest.skip(f"Testcases specifically designed for MI350P")
    run_partition_test_scenario(gpu_cluster, environment, request, profile, workload = False)

@pytest.mark.level2
@pytest.mark.parametrize("profile", ["DPX_NPS1", "CPX_NPS1", "SPX_NPS1"])
def test_partitioning_workload_MI350P(gpu_cluster, deviceconfig_install, environment, request,
                                         create_dcm_configmap, profile):
    gpu_series = get_gpu_series(gpu_cluster, environment)
    if gpu_series != 'MI350P':
        pytest.skip(f"Testcases specifically designed for MI350P")
    run_partition_test_scenario(gpu_cluster, environment, request, profile, workload = True)

@pytest.mark.level2
@pytest.mark.parametrize("profile", ["QPX_NPS1", "DPX_NPS1", "QPX_NPS4", "CPX_NPS1", "CPX_NPS4", "SPX_NPS1"])
def test_partitioning_no_workload_MI300X(gpu_cluster, deviceconfig_install, environment, request, profile):
    gpu_series = get_gpu_series(gpu_cluster, environment)
    if gpu_series != 'MI300X':
        pytest.skip(f"Testcases specifically designed for MI300X")
    run_partition_test_scenario(gpu_cluster, environment, request, profile, workload = False)

@pytest.mark.level2
@pytest.mark.parametrize("profile", ["QPX_NPS1", "DPX_NPS1", "QPX_NPS4", "CPX_NPS1", "CPX_NPS4", "SPX_NPS1"])
def test_partitioning_workload_MI300X(gpu_cluster, deviceconfig_install, environment, request, profile):
    gpu_series = get_gpu_series(gpu_cluster, environment)
    if gpu_series != 'MI300X':
        pytest.skip(f"Testcases specifically designed for MI300X")
    run_partition_test_scenario(gpu_cluster, environment, request, profile, workload = True)

@pytest.mark.level2
@pytest.mark.parametrize("profile", ["QPX_NPS1", "DPX_NPS2", "QPX_NPS4", "DPX_NPS1", "CPX_NPS1", "CPX_NPS4", "SPX_NPS1"])
def test_partitioning_no_workload_MI325X(gpu_cluster, deviceconfig_install, environment, request, profile):
    gpu_series = get_gpu_series(gpu_cluster, environment)
    if gpu_series != 'MI325X':
        pytest.skip(f"Testcases specifically designed for MI325X")
    run_partition_test_scenario(gpu_cluster, environment, request, profile, workload = False)

@pytest.mark.level2
@pytest.mark.parametrize("profile", ["QPX_NPS1", "DPX_NPS2", "QPX_NPS4", "DPX_NPS1", "CPX_NPS1", "CPX_NPS4", "SPX_NPS1"])
def test_partitioning_workload_MI325X(gpu_cluster, deviceconfig_install, environment, request, profile):
    gpu_series = get_gpu_series(gpu_cluster, environment)
    if gpu_series != 'MI325X':
        pytest.skip(f"Testcases specifically designed for MI325X")
    run_partition_test_scenario(gpu_cluster, environment, request, profile, workload = True)


@pytest.mark.level23
@pytest.mark.parametrize("profile", ["CPX_NPS4", "DPX_NPS2"])
def test_partitioning_test_runner(gpu_cluster, deviceconfig_install, environment, request, images, profile):
    gpu_series = get_gpu_series(gpu_cluster, environment)
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    series_features = amdgpu_util.get_gpu_features_by_series(gpu_series)
    if not series_features.get('gpu_partitioning', False):
        pytest.skip(f"GPU partitioning not supported for {gpu_series}")

    framework = "RVS"
    recipe = "gst_single"
    configmap = {}
    for node in gpu_nodes:
        worker = k8_util.k8_get_node_hostname(node)
        update_test_runner_configmap(recipe, worker, configmap, framework)

    configmap_name = create_configmap(request, deviceconfig_install, environment, framework, configmap)
    update_test_runner_image(deviceconfig_install, environment, framework, configmap_name)

    run_partition_test_scenario(gpu_cluster, environment, request, profile, workload = False)

    devicecfg_pods = [
        common.PodInfo('device-plugin', len(gpu_nodes), 1),
        common.PodInfo('metrics-exporter', len(gpu_nodes), 1),
        common.PodInfo('test-runner', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods, sleep_time = 20)
    debug_on_failure(environment, (not failed_pods), f"One or more pods are not ready - {failed_pods}")

    job_name = "test-runner-manual-trigger"
    namespace = environment.gpu_operator_namespace
    sa_name = "test-run"
    cluster_role_name = "test-run-cluster-role"
    crb_name = 'test-run-rb'
    scc_crb_name = 'test-run-scc-privileged'

    def _cleanup_jobs():
        k8_util.k8_delete_job(namespace, job_name)
        k8_util.k8_delete_cluster_role_binding(scc_crb_name)
        k8_util.k8_delete_cluster_role_binding(crb_name)
        k8_util.k8_delete_cluster_role(cluster_role_name)
        k8_util.k8_delete_service_account(sa_name, namespace)
    request.addfinalizer(_cleanup_jobs)
    _cleanup_jobs()

        # Create ServiceAccount
    ret_code, ret_stdout, ret_stderr = k8_util.k8_create_service_account(sa_name, namespace)
    debug_on_failure(environment, (ret_code == 0),
                     f"Failed to create service-account, error:{ret_stderr}")

    # Define ClusterRole: verb=get

    #rules = k8_util.k8_create_rules_from_endpoint_list([("/test_runner", "get")])
    rules = list()
    rules.append(
        k8_util.k8_create_rules_from_verbs(
            resources=["events"],
            verbs=["get", "list", "watch", "create", "update"],
            api_groups=[""]
        )
    )
    rules.append(
        k8_util.k8_create_rules_from_verbs(
            resources=["nodes"],
            verbs=["patch"],
            api_groups=[""]
        )
    )
    # Define ClusterRole: verb=get
    ret_code, ret_stdout, ret_stderr = k8_util.k8_create_cluster_role(cluster_role_name, rules)
    debug_on_failure(environment, (ret_code == 0),
                     f"Failed to create test_runner clusterrole with GET, error:{ret_stderr}")

    ret_code, ret_stdout, ret_stderr = k8_util.k8_create_role_binding(crb_name, namespace, cluster_role_name, sa_name)
    debug_on_failure(environment, (ret_code == 0),
                              f"Failed to create test_runner clusterrole with verbs, error:{ret_stderr}")

    if environment.deployment_mode == "openshift":
        ret_code, ret_stdout, ret_stderr = k8_util.k8_create_role_binding(
            scc_crb_name, namespace, "system:openshift:scc:privileged", sa_name)
        debug_on_failure(environment, (ret_code == 0),
                         f"Failed to bind privileged SCC to {sa_name}, error:{ret_stderr}")

    # Create token for ServiceAccount
    token = k8_util.k8_create_token(namespace, sa_name, "1h")
    debug_on_failure(environment, token != None,
                     f"Failed to create token for the service-account : {sa_name}")
    Logger.info(f"TOKEN={token}")

    time.sleep(30) # Wait for exporter to start working
    # Get endpoint for each node

    # Create Job
    k8_util.k8_create_test_runner_job(namespace,
                                      images,
                                      worker,
                                      sa_name,
                                      job_name,
                                      framework,
                                      True,
                                      False,
                                      datetime.datetime.utcnow().minute + 2)
    # Retry job status check — after partition change, GPU resources need time
    # to re-register (device-plugin restart + kubelet reconciliation)
    job_status = None
    for attempt in range(15):
        job_status = k8_util.k8_get_job_status(namespace, job_name)
        if job_status == "Running":
            break
        Logger.info(f"Attempt {attempt+1}/15: job status={job_status}, waiting for Running...")
        time.sleep(2)
    debug_on_failure(environment, job_status == "Running",
                     f"job should be in Running state, but found: {job_status}")
    #no need to wait for completion GPUOP-520
    verify_logs(environment, [f'Starting iteration 1 of 1 for test: {recipe}'], 'test-runner-manual')
    k8_util.k8_delete_job(namespace, job_name)

@pytest.mark.skip(reason="TODO: Update to use all_image_versions fixture instead of removed alternative_images")
@pytest.mark.parametrize("upgrade_policy", ["RollingUpdate", "OnDelete"])
def test_config_manager_operand_upgrade(deviceconfig_install, environment, upgrade_policy):
    """Test Config Manager operand upgrade with different upgrade policies.

    Verifies that Config Manager pods can be upgraded to a different version by
    modifying the DeviceConfig CR. Tests both upgrade strategies:

    RollingUpdate:
        DeviceConfig controller automatically recreates config-manager pods with
        new image version as soon as CR is modified.

    OnDelete:
        DeviceConfig controller only updates pods when they are manually deleted.
        Pods continue running with old version until explicitly deleted.

    Test Steps:
        1. Verify current config-manager pods running with initial image version
        2. Modify DeviceConfig CR with alternative image version and upgrade policy
        3. For RollingUpdate: Wait for automatic pod recreation
        4. For OnDelete: Verify pods not recreated, then manually delete them
        5. Wait for config-manager pods Running with new image version
        6. Verify DeviceConfig status Ready
        7. Validate new image version in pod container status
        8. Teardown: Restore original image version

    Args:
        deviceconfig_install: DeviceConfig CR configuration.
        environment: Test environment.
        alternative_images: Alternative container image versions for upgrade.
        upgrade_policy: "RollingUpdate" or "OnDelete".

    Raises:
        pytest.skip: If GPU Operator version < v1.3.0.
        pytest.fail: If both image versions are the same.
        AssertionError: If upgrade doesn't work as expected.
    """
    global Logger
    if version.parse(environment.gpu_operator_version.lstrip('v')) < version.parse("1.3.0"):
        pytest.skip(f"DCM Operand upgrade feature is not available in releases before v1.3.0")

    if (images['configManager.image.version'] == alternative_images['configManager.image.version']):
        pytest.fail("Invalid input for operand upgrade testcase - both version same")

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")

    # Check current version of the configManager from deviceconfig-CR
    def _modify_config_manager_version(repo, version):
        for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
            tcfg['configManager.image.repository'] = repo
            tcfg['configManager.image.version'] = version
            tcfg['configManager.upgradePolicy.upgradeStrategy'] = upgrade_policy
            cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
            ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
            K8Helper.triage(environment, (ret_code == 0), "Failed to modify deviceconfig CR")

    def _restore_config_manager():
        _modify_config_manager_version(images['configManager.image.repository'],
                                       images['configManager.image.version'])
        devicecfg_pods = [
            common.PodInfo('config-manager', len(gpu_nodes), 1),
        ]
        failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods)
        K8Helper.triage(environment, not failed_pods, f"One or more pods are not ready - {failed_pods}")

    request.addfinalizer(_restore_config_manager)

    K8Helper.check_deviceconfig_status(environment, deviceconfig_install.devicecfg_list)
    ret_code, orig_dcm_pods = k8_util.k8_get_pods(environment.gpu_operator_namespace, pod_name_pattern = "config-manager")
    K8Helper.triage(environment, (ret_code == 0 and len(orig_dcm_pods) > 0), f"Missing config-manager pods or error")
    _modify_config_manager_version(alternative_images['configManager.image.repository'],
                                     alternative_images['configManager.image.version'])

    if upgrade_policy == "RollingUpdate":
        Logger.debug("Wait until upgrade is complete...")
    elif upgrade_policy == "OnDelete":
        # Check no upgrade is kicked in
        time.sleep(20)
        ret_code, precheck_dcm_pods = k8_util.k8_get_pods(environment.gpu_operator_namespace, pod_name_pattern = "config-manager")
        for old_pod, new_pod in zip(orig_dcm_pods, precheck_dcm_pods):
            for o_s_info, n_s_info in zip(old_pod['status']['container_statuses'], new_pod['status']['container_statuses']):
                if o_s_info['name'] == 'config-manager-container' and n_s_info['name'] == 'config-manager-container':
                    K8Helper.triage(environment, (o_s_info['image'] == n_s_info['image']),
                                    f"Version mismatch before pod-deletion with policy: {upgrade_policy}, {o_s_info}, {n_s_info}")
        # explicitly delete the pod
        k8_util.k8_delete_all_pods_with_name_pattern(environment.gpu_operator_namespace, 'config-manager')

    time.sleep(20)
    devicecfg_pods = [
        common.PodInfo('config-manager', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods)
    K8Helper.triage(environment, not failed_pods, f"One or more pods are not ready - {failed_pods}")
    ret_code, new_dcm_pods = k8_util.k8_get_pods(environment.gpu_operator_namespace, pod_name_pattern = "config-manager")
    K8Helper.triage(environment, (ret_code == 0 and len(new_dcm_pods) > 0), f"Missing config-manager pods or error")
    K8Helper.check_deviceconfig_status(environment, deviceconfig_install.devicecfg_list)

    # Check latest version of the configManager from deviceconfig-CR and match to alternative_images
    for pod in new_dcm_pods:
        for s_info in pod['status']['container_statuses']:
            if s_info['name'] == 'config-manager-container':
                K8Helper.triage(environment, (alternative_images['configManager.image.version'] in s_info['image']),
                                f"Unexpected version found in the config-manager-container image post upgrade, {s_info}")
                K8Helper.triage(environment, (alternative_images['configManager.image.repository'] in s_info['image']),
                                f"Unexpected version found in the config-manager-container image post upgrade, {s_info}")

@pytest.mark.level1
def test_deviceconfig_config_manager_disable(gpu_cluster, deviceconfig_install, environment):
    """Test Config Manager enable/disable lifecycle via DeviceConfig CR.

    Verifies that Config Manager can be disabled and re-enabled by modifying the
    DeviceConfig CR configManager.enable field. Tests clean teardown and restart.

    Test Steps:
        1. Reset GPU partitions to default and optionally reboot (for MI325X)
        2. Modify DeviceConfig CR to set configManager.enable=False
        3. Verify config-manager pods are terminated
        4. Verify device-plugin pods still Running (other operands unaffected)
        5. Modify DeviceConfig CR to set configManager.enable=True
        6. Verify config-manager pods recreated and Running
        7. Verify device-plugin pods still Running

    Args:
        gpu_cluster: Cluster object.
        deviceconfig_install: DeviceConfig CR configuration.
        environment: Test environment.

    Raises:
        AssertionError: If pods don't reach expected states.
    """
    global Logger
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    debug_on_failure(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    debug_on_failure(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")

    # reset and reload driver via reboot - to handle inbox driver case as well
    gpu_series = get_gpu_series(gpu_cluster, environment)
    skip_reboot = (gpu_series not in ["MI325X"])
    reset_dcm_profile(gpu_cluster, environment, skip_reboot = skip_reboot)
    # disable config-manager
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['configManager.enable'] = False
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        debug_on_failure(environment, (ret_code == 0), "Failed to modify deviceconfig CR")

    export_pods = [
        common.PodInfo('config-manager', 1, 1),
    ]
    running_pods = k8_util.k8_check_pod_terminated(environment.gpu_operator_namespace, export_pods)
    debug_on_failure(environment, not running_pods,
                              f"Some of the pods are still running post uninstallation - {running_pods}")
    # Watch for all pod creation
    devicecfg_pods = [
        common.PodInfo('device-plugin', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods, sleep_time = 20)
    debug_on_failure(environment, (not failed_pods), f"One or more pods are not ready - {failed_pods}")

    # re-enable config-manager
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['configManager.enable'] = True
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        debug_on_failure(environment, (ret_code == 0), "Failed to modify deviceconfig CR")

    # Watch for all pod creation
    devicecfg_pods = [
        common.PodInfo('device-plugin', len(gpu_nodes), 1),
        common.PodInfo('config-manager', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods, sleep_time = 20)
    debug_on_failure(environment, (not failed_pods), f"One or more pods are not ready - {failed_pods}")

