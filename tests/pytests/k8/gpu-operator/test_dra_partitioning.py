#!/usr/bin/python3

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

"""AMD GPU Operator DRA + DCM Partition Integration Test Suite.

This module tests the integration between Device Config Manager (DCM) partition
operations and the DRA driver's ResourceSlice advertisement. It verifies that
after DCM changes a GPU's partition profile, the DRA driver correctly updates
ResourceSlices to reflect the new partition state.

Integration Flow:
    1. DCM applies partition profile via node label + taint
    2. DCM repartitions GPUs and reports success
    3. Nodes are untainted, DRA driver pods restart
    4. DRA driver discovers new partition state from sysfs
    5. DRA driver publishes updated ResourceSlices
    6. Tests verify device count, type, and partitionProfile attributes

Partition Device Count Per Physical GPU:
    SPX: 1 device  (type=amdgpu, full GPU)
    DPX: 2 devices (type=amdgpu-partition)
    QPX: 4 devices (type=amdgpu-partition)
    CPX: 8 devices (type=amdgpu-partition)

Prerequisites:
    - Kubernetes 1.32+ with DRA API enabled
    - GPU Operator v1.5.0+ with DRA driver support
    - MI3xx series GPU (MI300X, MI325X, MI350X, MI350P)
    - Partition profile JSON files in lib/files/

Test Organization:
    Per-GPU-series tests with parametrized partition profiles, matching the
    pattern in test_config_manager.py. Module is skipped entirely if the GPU
    does not support partitioning (MI2xx) or if DRA/K8s prerequisites are not met.
"""

import pdb
import pprint
import pytest
import sys
import os
import re
import time
import json
import logging
import yaml
import lib.common as common
import lib.helm_util as helm_util
import lib.k8_util as k8_util
import lib.spec_util as spec_util
import lib.dra_util as dra_util
from lib.util import K8Helper
from kubernetes import client, config

Logger = logging.getLogger("k8.test_dra_partitioning")
LogPrettyPrinter = pprint.PrettyPrinter(indent=2)

debug_on_failure = K8Helper.triage

PARTITION_DEVICE_MULTIPLIER = {
    "SPX": 1,
    "DPX": 2,
    "QPX": 4,
    "CPX": 8,
}

PARTITION_DEVICE_MULTIPLIER_OVERRIDES = {
    "MI350P": {"CPX": 4},
}


@pytest.fixture(autouse=True, scope="module")
def skip_module(gpu_cluster, environment):
    """Skip entire module if DRA or partitioning is not supported.

    Checks:
    - Kubernetes version >= 1.32 (DRA requirement)
    - DRA API available in cluster
    - GPU Operator version >= v1.5.0 (draDriver field)
    - GPU series supports partitioning (MI3xx only, not MI2xx)
    """
    global Logger

    # Check Kubernetes version
    ret_code, version_info = k8_util.k8_get_version()
    if ret_code != 0:
        pytest.skip("Failed to get Kubernetes version")

    try:
        major_str = str(version_info.get("major", "0"))
        minor_str = str(version_info.get("minor", "0"))
        major_match = re.match(r"(\d+)", major_str)
        minor_match = re.match(r"(\d+)", minor_str)
        major = int(major_match.group(1)) if major_match else 0
        minor = int(minor_match.group(1)) if minor_match else 0
    except (ValueError, AttributeError) as e:
        pytest.skip(f"Failed to parse Kubernetes version: {e}")

    if major < 1 or (major == 1 and minor < 32):
        pytest.skip(f"DRA requires Kubernetes 1.32+, but cluster is running {major}.{minor}")

    # Check DRA API
    dra_available, error_msg, api_version = dra_util.check_dra_api_available()
    if not dra_available:
        pytest.skip(f"DRA API not available: {error_msg}")

    # Check GPU Operator version
    from packaging import version
    gpu_operator_version = getattr(environment, "gpu_operator_version", None)
    if gpu_operator_version:
        try:
            version_str = gpu_operator_version.lstrip("v")
            if version.parse(version_str) < version.parse("1.5.0"):
                pytest.skip(f"DRA partitioning tests require GPU Operator v1.5.0+, got {gpu_operator_version}")
        except Exception:
            pass

    # Check GPU series supports partitioning
    gpu_variants = gpu_cluster.get_gpu_variants()
    if not gpu_variants:
        pytest.skip("No GPU variants found in cluster")

    gpu_series = gpu_variants[0]
    if "MI2" in gpu_series:
        pytest.skip(f"GPU series {gpu_series} does not support partitioning")
    if "MI3" not in gpu_series:
        pytest.skip(f"GPU series {gpu_series} does not support partitioning (only MI3xx supported)")

    # Cache the discovered API version for use by get_resource_slices()
    setattr(environment, "dra_api_version", api_version)

    Logger.info(f"DRA partitioning tests enabled: K8s {major}.{minor}, DRA {api_version}, GPU {gpu_series}")
    return


def get_gpu_series(gpu_cluster, environment):
    gpu_variants = gpu_cluster.get_gpu_variants()
    if gpu_variants:
        return gpu_variants[0]
    debug_on_failure(environment, False, f"didn't find gpu_variants from cluster: {gpu_variants}")


@pytest.fixture(scope="module")
def add_tolerations(environment, effect="NoExecute"):
    """Add amd-dcm tolerations to system namespaces during partition testing.

    During GPU partitioning, nodes are tainted with amd-dcm=up:NoExecute.
    System pods in kube-system, cert-manager, and kube-flannel need tolerations.
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


@pytest.fixture(scope="module")
def create_dcm_configmap(gpu_cluster, environment):
    """Create ConfigMap with GPU partition profiles for Config Manager."""
    namespace = environment.gpu_operator_namespace
    configmap = "config-map-config-manager"

    gpu_series = get_gpu_series(gpu_cluster, environment)
    dut_node = gpu_cluster.find_node_by_gpu_series(gpu_series)
    num_gpus_on_dut = dut_node.num_gpus
    debug_on_failure(environment, gpu_series != None, f"Missing gpu-series information")

    file_path = os.path.join(environment.logdir, f"partitioning_check_{gpu_series}_{num_gpus_on_dut}.json")
    if not os.path.exists(file_path):
        Logger.warning(f"No partition profile file found at {file_path}, using empty profile")
        file_path = os.path.join("lib", "files", "partitioning_no_profiles.json")

    # Delete existing configmap if present
    k8_util.k8_delete_configmap(namespace, configmap)
    time.sleep(2)

    # Create configmap with partition profiles
    ret_code, ret_stdout, ret_stderr = k8_util.k8_create_configmap(
        namespace, configmap, file_path, "config.json"
    )
    debug_on_failure(environment, (ret_code == 0),
                     f"Failed to create DCM configmap, error: {ret_stderr}")

    yield configmap
    ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_configmap(namespace, configmap)


@pytest.fixture(scope="module")
def deviceconfig_install(gpu_cluster, images, gpu_operator_install, create_dcm_configmap,
                         add_tolerations, environment):
    """Create DeviceConfig with both DRA driver AND Config Manager enabled.

    This is the key integration fixture: it enables both draDriver and configManager
    in the same DeviceConfig CR so that partition changes via DCM are reflected in
    DRA ResourceSlices.
    """
    global Logger

    # Cleanup existing deviceconfigs
    devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
    for devcfg_name, _ in devcfg_map.items():
        ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_deviceconfig_cr(
            environment.gpu_operator_namespace, devcfg_name
        )
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
        'metadata.namespace': environment.gpu_operator_namespace,
        'driver.enable': True,
        'devicePlugin.enableDevicePlugin': False,
        'draDriver.enable': True,
        'metricsExporter.enable': False,
        'testRunner.enable': False,
        'configManager.enable': True,
        'configManager.config': configmap,
    }
    test_config.update(images)
    test_cfg_map = spec_util.build_deviceconfig_cr_template(
        test_config, gpu_nodes, 'dra-partition', environment.amdgpu_driver_spec
    )
    devicecfg_list = []

    for spec_name, tcfg in test_cfg_map.items():
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_create_deviceconfig_cr(cr_spec)
        debug_on_failure(environment, (ret_code == 0),
                         f"Failed to create deviceconfig, stderr: {ret_stderr}")
        devicecfg_list.append(tcfg['metadata.name'])

    K8Helper.check_deviceconfig_status(environment, devicecfg_list)
    for devcfg in devicecfg_list:
        K8Helper.wait_kmm_worker_completion(environment, devcfg)
    K8Helper.update_node_driver_version(gpu_cluster, environment)

    devcfg_info = DeviceConfigCRInfo()
    setattr(devcfg_info, "test_cfg_map", test_cfg_map)
    setattr(devcfg_info, "devicecfg_list", devicecfg_list)

    # Verify both DRA driver and config-manager pods are running
    devicecfg_pods = [
        common.PodInfo('dra-driver', len(gpu_nodes), 1),
        common.PodInfo('config-manager', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(
        environment.gpu_operator_namespace, devicecfg_pods, sleep_time=20
    )
    debug_on_failure(environment, (not failed_pods),
                     f"One or more pods are not ready - {failed_pods}")

    yield devcfg_info

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    if ret_code == 0 and gpu_nodes:
        K8Helper.wait_for_driver_reload(environment, gpu_nodes, fail_on_timeout=False)

    device_cfg_info = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace, None)
    for devcfg_name, _ in device_cfg_info.items():
        k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)


# ---------------------------------------------------------------------------
# ResourceSlice helpers (reused from test_dra_driver_attributes.py patterns)
# ---------------------------------------------------------------------------

def _get_dra_api_version():
    """Return the cached DRA API version, or discover it dynamically."""
    cached = getattr(_get_dra_api_version, "_version", None)
    if cached:
        return cached
    _, _, version = dra_util.check_dra_api_available()
    if not version:
        version = "v1"
    _get_dra_api_version._version = version
    return version


def get_resource_slices():
    api_version = _get_dra_api_version()
    ret_code, items, err = k8_util.k8_get_custom_resource_objects(
        group="resource.k8s.io", version=api_version, plural="resourceslices"
    )
    if ret_code != 0:
        Logger.error(f"Failed to get ResourceSlices: {err}")
        return []
    return items if items else []


def get_amd_gpu_devices_from_slices(node_name=None):
    """Extract AMD GPU devices from ResourceSlices, optionally filtered by node."""
    resource_slices = get_resource_slices()
    amd_devices = []

    for slice_obj in resource_slices:
        driver_name = slice_obj.get("spec", {}).get("driver", "")
        slice_node = slice_obj.get("spec", {}).get("nodeName", "unknown")

        if driver_name != "gpu.amd.com":
            continue
        if node_name and slice_node != node_name:
            continue

        devices = slice_obj.get("spec", {}).get("devices")
        if devices is None:
            continue

        for device in devices:
            device_name = device.get("name", "unknown")
            gpu_attrs = normalize_device_attributes(device)
            device_type = gpu_attrs.get("type", "unknown")

            amd_devices.append({
                "name": device_name,
                "type": device_type,
                "node_name": slice_node,
                "attributes": gpu_attrs,
            })

    return amd_devices


def normalize_device_attributes(device):
    """Normalize ResourceSlice device attributes to a simple {name: value} dict.

    Handles all DRA API versions:
      - v1beta1 (K8s 1.32):    device.basic.attributes  (QualifiedName keys)
      - v1beta2 (K8s 1.33+):   device.attributes        (QualifiedName keys)
      - v1      (K8s 1.34+):   device.attributes        (QualifiedName keys)

    QualifiedName keys are "domain/name" (e.g. "gpu.amd.com/type"); the domain
    prefix is stripped so callers can look up attrs by short name ("type").
    Typed-value dicts like {"string": "x"} are unwrapped to plain values.
    """
    simple_attrs = {}

    # v1beta1 nests under device.basic; v1beta2 and v1 put attrs directly on device
    basic = device.get("basic")
    raw_attrs = basic.get("attributes", {}) if basic else device.get("attributes", {})

    for qualified_name, attr_value in raw_attrs.items():
        short_name = qualified_name.rsplit("/", 1)[-1]
        if isinstance(attr_value, dict):
            for _, actual_value in attr_value.items():
                simple_attrs[short_name] = actual_value
                break
        else:
            simple_attrs[short_name] = attr_value

    return simple_attrs


# ---------------------------------------------------------------------------
# DCM partition helpers (reused from test_config_manager.py patterns)
# ---------------------------------------------------------------------------

def get_partition_status_from_pod(environment, namespace, node_name, max_retries=10, retry_delay=5):
    """Get partition status from config-manager pod with retries."""
    for attempt in range(max_retries):
        pod_name = k8_util.k8_get_pod_name("config-manager", namespace, node_name)
        if pod_name is None:
            if attempt < max_retries - 1:
                Logger.warning(f"config-manager pod not found on {node_name}, attempt {attempt+1}/{max_retries}")
                time.sleep(retry_delay)
                continue
            else:
                Logger.error(f"config-manager pod not found on {node_name} after {max_retries} attempts")
                return {}

        ret_code, output, resp_stderr = k8_util.exec_command_in_pod(
            namespace, ["amd-smi", "partition", "-c", "--json"], pod_name
        )
        if ret_code == 0:
            return extract_partition_info(environment, output)
        else:
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
            else:
                Logger.error(f"Failed to get partition info from {pod_name} (rc={ret_code}): {resp_stderr}")
                return {}
    return {}


def extract_partition_info(environment, amd_smi_partition_json):
    """Parse amd-smi partition output and extract GPU partition states."""
    try:
        amd_smi_partition_info = json.loads(amd_smi_partition_json.replace("'", "\""))
    except Exception as je:
        Logger.error(f"Failed to parse amd_smi_partition JSON: {je}")
        debug_on_failure(environment, False, f"Failed to parse amd-smi-partition JSON")

    current_partitions = amd_smi_partition_info.get("current_partition", [])
    main_gpu_entries = [item for item in current_partitions
                        if item["memory"] != "N/A" and item["accelerator_type"] != "N/A"]
    partition_status = {}
    for entry in main_gpu_entries:
        partition_status[entry["gpu_id"]] = f"{entry['accelerator_type']}_{entry['memory']}"
    return partition_status


def verify_label(environment, profile):
    """Wait for DCM to mark partition change as successful on all nodes."""
    i = 0
    while i < 40:
        ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
        if gpu_nodes:
            all_nodes_success = True
            for node in gpu_nodes:
                node_name = node['metadata']['labels'].get('kubernetes.io/hostname', 'unknown')
                prof = node['metadata']['labels'].get('dcm.amd.com/gpu-config-profile', 'NA')
                stat = node['metadata']['labels'].get('dcm.amd.com/gpu-config-profile-state', 'unknown')

                if stat == "failure":
                    debug_on_failure(environment, False,
                                    f"DCM partition failed on {node_name}: profile={prof}, state={stat}")
                    return

                if not (prof == profile and stat == "success"):
                    all_nodes_success = False

            if all_nodes_success:
                Logger.info(f"Partition profile {profile} applied successfully on all nodes")
                break
        i += 1
        time.sleep(10)

    if i >= 40:
        debug_on_failure(environment, False,
                         f"Timeout waiting for partition profile {profile} on all nodes after 400s")


def reset_dcm_profile(gpu_cluster, environment):
    """Reset GPU partitions to default SPX_NPS1 and clean up DCM state."""
    namespace = environment.gpu_operator_namespace
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    if ret_code != 0 or len(gpu_nodes) == 0:
        Logger.error("Failed to get GPU nodes for DCM reset")
        return

    def _any_gpu_partitioned():
        for node in gpu_nodes:
            node_name = node['metadata']['labels']['kubernetes.io/hostname']
            partition_status = get_partition_status_from_pod(environment, namespace, node_name, max_retries=5, retry_delay=5)
            if not partition_status:
                continue
            for gpu_id, profile in partition_status.items():
                if profile != "SPX_NPS1":
                    return True
        return False

    try:
        if _any_gpu_partitioned():
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
            devcfg_map = k8_util.k8_get_deviceconfigs_info(namespace)
            for devcfg_name, _ in devcfg_map.items():
                try:
                    custom_objects_api.patch_namespaced_custom_object(
                        group="amd.com", version='v1alpha1',
                        name=devcfg_name, namespace=namespace,
                        plural='deviceconfigs', body=patch_body
                    )
                except client.ApiException as e:
                    Logger.error(f"Failed to patch DeviceConfig: {e}")

            devicecfg_pods = [common.PodInfo('config-manager', len(gpu_nodes), 1)]
            failed_pods = k8_util.k8_check_pod_running(namespace, devicecfg_pods, sleep_time=20)
            if not failed_pods:
                for node in gpu_nodes:
                    node_name = node['metadata']['labels']['kubernetes.io/hostname']
                    k8_util.k8_taint_node(node_name, taint_add=True, effect="NoExecute")

                labels_dict = {"dcm.amd.com/gpu-config-profile": "SPX_NPS1"}
                for node in gpu_nodes:
                    node_name = node['metadata']['labels']['kubernetes.io/hostname']
                    k8_util.k8_label_node(node_name, labels_dict, overwrite=True)

                verify_label(environment, "SPX_NPS1")
    finally:
        labels_dict = {
            "dcm.amd.com/gpu-config-profile": None,
            "dcm.amd.com/gpu-config-profile-state": None
        }
        for node in gpu_nodes:
            node_name = node['metadata']['labels']['kubernetes.io/hostname']
            k8_util.k8_label_node(node_name, labels_dict, overwrite=True)
            k8_util.k8_untaint_node(node_name, effects=["NoSchedule", "NoExecute"])

        # Wait for both DRA driver and config-manager to be ready
        devicecfg_pods = [
            common.PodInfo('dra-driver', len(gpu_nodes), 1),
            common.PodInfo('config-manager', len(gpu_nodes), 1),
        ]
        failed_pods = k8_util.k8_check_pod_running(namespace, devicecfg_pods, sleep_time=20)
        if failed_pods:
            Logger.error(f"Pods not running after DCM reset: {failed_pods}")


# ---------------------------------------------------------------------------
# Core test scenario
# ---------------------------------------------------------------------------

def run_dra_partition_test(gpu_cluster, environment, request, profile):
    """Core integration test: DCM partition change → DRA ResourceSlice update.

    Steps:
        1. Record baseline ResourceSlice state (device count, types, profiles)
        2. Apply partition profile via DCM (taint + label)
        3. Wait for DCM to report success
        4. Untaint nodes → DRA driver restarts
        5. Wait for DRA driver pods to be running
        6. Verify ResourceSlices reflect new partition state:
           a. Device count matches expected (num_gpus × partition multiplier)
           b. Device type is 'amdgpu-partition' (or 'amdgpu' for SPX_NPS1)
           c. partitionProfile attribute matches requested profile
        7. Reset to SPX_NPS1
    """
    global Logger
    gpu_series = get_gpu_series(gpu_cluster, environment)
    dut_node = gpu_cluster.find_node_by_gpu_series(gpu_series)
    namespace = environment.gpu_operator_namespace

    # Load partition profile definitions
    file_path = os.path.join(environment.logdir, f"partitioning_check_{gpu_series}_{dut_node.num_gpus}.json")
    with open(file_path) as fp:
        profiles = json.load(fp)
        if not profiles.get("gpu-config-profiles"):
            pytest.fail(f"check {file_path}, something wrong with the configmap")
        elif not profiles["gpu-config-profiles"].get(profile, False):
            pytest.skip(f"Profile {profile} is not supported for {gpu_series}. Refer {file_path}")

    profile_def = profiles["gpu-config-profiles"][profile]["profiles"][0]
    compute_partition = profile_def["computePartition"]
    memory_partition = profile_def["memoryPartition"]
    num_gpus = profile_def["numGPUsAssigned"]

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    debug_on_failure(environment, (ret_code == 0 and len(gpu_nodes) > 0),
                     "Failed to get GPU nodes")

    def _untaint_all_nodes():
        for node in gpu_nodes:
            node_name = node['metadata']['labels']['kubernetes.io/hostname']
            k8_util.k8_untaint_node(node_name, effects=["NoSchedule", "NoExecute"])

    def _cleanup():
        reset_dcm_profile(gpu_cluster, environment)
        _untaint_all_nodes()

    request.addfinalizer(_cleanup)

    # -----------------------------------------------------------------------
    # Step 1: Record baseline ResourceSlice state
    # -----------------------------------------------------------------------
    Logger.info("Step 1: Recording baseline ResourceSlice state before partition change")
    baseline_devices = {}
    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        devices = get_amd_gpu_devices_from_slices(node_name=node_name)
        baseline_devices[node_name] = devices
        Logger.info(f"  Baseline: {node_name} has {len(devices)} DRA device(s)")
        for dev in devices:
            Logger.debug(f"    {dev['name']}: type={dev['type']}, "
                         f"partitionProfile={dev['attributes'].get('partitionProfile', 'N/A')}")

    # -----------------------------------------------------------------------
    # Step 2: Apply partition profile via DCM
    # -----------------------------------------------------------------------
    Logger.info(f"Step 2: Applying partition profile {profile} via DCM")

    # Ensure config-manager is running before patching
    devicecfg_pods = [common.PodInfo('config-manager', len(gpu_nodes), 1)]
    failed_pods = k8_util.k8_check_pod_running(namespace, devicecfg_pods, sleep_time=20)
    debug_on_failure(environment, (not failed_pods), f"Config-manager not ready: {failed_pods}")

    # Patch DeviceConfig with NoExecute toleration for config-manager
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
    devcfg_map = k8_util.k8_get_deviceconfigs_info(namespace)
    for devcfg_name, _ in devcfg_map.items():
        try:
            custom_objects_api.patch_namespaced_custom_object(
                group="amd.com", version='v1alpha1',
                name=devcfg_name, namespace=namespace,
                plural='deviceconfigs', body=patch_body
            )
            Logger.info(f"Patched DeviceConfig {devcfg_name} with configManagerTolerations")
        except client.ApiException as e:
            debug_on_failure(environment, False, f"Failed to patch DeviceConfig: {e}")

    # Taint nodes and apply profile label
    labels_dict = {"dcm.amd.com/gpu-config-profile": profile}
    for node in gpu_nodes:
        node_name = node['metadata']['labels']['kubernetes.io/hostname']
        k8_util.k8_taint_node(node_name, taint_add=True, effect="NoExecute")
        k8_util.k8_label_node(node_name, labels_dict, overwrite=True)

    # -----------------------------------------------------------------------
    # Step 3: Wait for DCM to complete partition
    # -----------------------------------------------------------------------
    Logger.info(f"Step 3: Waiting for DCM to complete partition to {profile}")
    verify_label(environment, profile)

    # Verify partition state via amd-smi
    expected_partition = f"{compute_partition}_{memory_partition}"
    post_partition_status = {}
    for node in gpu_nodes:
        node_name = node['metadata']['labels']['kubernetes.io/hostname']
        post_partition_status[node_name] = get_partition_status_from_pod(
            environment, namespace, node_name, max_retries=30, retry_delay=5
        )

    all_gpus_match = True
    for node_name, node_status in post_partition_status.items():
        for gpu_id, actual_partition in node_status.items():
            if actual_partition != expected_partition:
                Logger.error(f"Node {node_name} GPU {gpu_id}: Expected {expected_partition}, got {actual_partition}")
                all_gpus_match = False
    debug_on_failure(environment, all_gpus_match,
                     f"DCM partition mismatch. Expected {expected_partition}, got: {post_partition_status}")

    # -----------------------------------------------------------------------
    # Step 4: Untaint nodes → DRA driver restarts with new partition state
    # -----------------------------------------------------------------------
    Logger.info("Step 4: Removing taints to allow DRA driver pods to restart")
    _untaint_all_nodes()

    # Wait for both DRA driver and config-manager pods
    devicecfg_pods = [
        common.PodInfo('dra-driver', len(gpu_nodes), 1),
        common.PodInfo('config-manager', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(namespace, devicecfg_pods, sleep_time=20)
    debug_on_failure(environment, (not failed_pods),
                     f"Pods not running after untaint: {failed_pods}")

    # Give DRA driver time to discover devices and publish ResourceSlices
    Logger.info("Waiting for DRA driver to publish updated ResourceSlices...")
    time.sleep(30)

    # -----------------------------------------------------------------------
    # Step 5: Verify ResourceSlices reflect new partition state
    # -----------------------------------------------------------------------
    Logger.info(f"Step 5: Verifying ResourceSlices reflect partition profile {profile}")

    overrides = PARTITION_DEVICE_MULTIPLIER_OVERRIDES.get(gpu_series, {})
    expected_multiplier = overrides.get(compute_partition, PARTITION_DEVICE_MULTIPLIER.get(compute_partition, 1))
    is_full_gpu = (compute_partition == "SPX" and memory_partition == "NPS1")
    expected_type = "amdgpu" if is_full_gpu else "amdgpu-partition"
    expected_profile_attr = f"{compute_partition.lower()}_{memory_partition.lower()}"

    validation_errors = []

    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        node_gpu_count = dut_node.num_gpus
        expected_device_count = node_gpu_count * expected_multiplier

        # Retry ResourceSlice check — DRA driver may still be publishing
        devices = []
        for attempt in range(6):
            devices = get_amd_gpu_devices_from_slices(node_name=node_name)
            if len(devices) == expected_device_count:
                break
            Logger.info(f"  {node_name}: Got {len(devices)} devices, expected {expected_device_count}, "
                        f"retry {attempt+1}/6...")
            time.sleep(10)

        Logger.info(f"  {node_name}: {len(devices)} DRA devices "
                     f"(expected {expected_device_count} = {node_gpu_count} GPUs × {expected_multiplier})")

        # 5a: Device count
        if len(devices) != expected_device_count:
            validation_errors.append(
                f"{node_name}: Device count mismatch - got {len(devices)}, "
                f"expected {expected_device_count} ({node_gpu_count} GPUs × {expected_multiplier} for {compute_partition})"
            )

        # 5b: Device type
        for dev in devices:
            if dev['type'] != expected_type:
                validation_errors.append(
                    f"{node_name}/{dev['name']}: type={dev['type']}, expected {expected_type}"
                )

        # 5c: partitionProfile attribute
        for dev in devices:
            dev_profile = dev['attributes'].get('partitionProfile', '')
            if is_full_gpu:
                # SPX_NPS1: partitionProfile should be spx_nps1 or may be absent
                if dev_profile and dev_profile != expected_profile_attr:
                    validation_errors.append(
                        f"{node_name}/{dev['name']}: partitionProfile={dev_profile}, "
                        f"expected {expected_profile_attr}"
                    )
            else:
                if dev_profile != expected_profile_attr:
                    validation_errors.append(
                        f"{node_name}/{dev['name']}: partitionProfile={dev_profile}, "
                        f"expected {expected_profile_attr}"
                    )

    if validation_errors:
        for err in validation_errors:
            Logger.error(f"  FAIL: {err}")
    else:
        Logger.info(f"  All ResourceSlice validations passed for profile {profile}")

    debug_on_failure(environment, len(validation_errors) == 0,
                     f"ResourceSlice validation failed after partition to {profile}:\n" +
                     "\n".join(validation_errors))


# ---------------------------------------------------------------------------
# Per-GPU-series test cases
# ---------------------------------------------------------------------------

@pytest.mark.level2
@pytest.mark.parametrize("profile", ["CPX_NPS1"])
def test_dra_partition_MI350X(gpu_cluster, deviceconfig_install, environment, request, profile):
    """Test DRA ResourceSlice update after DCM partition change on MI350X."""
    gpu_series = get_gpu_series(gpu_cluster, environment)
    if gpu_series != 'MI350X':
        pytest.skip(f"Test designed for MI350X, cluster has {gpu_series}")
    run_dra_partition_test(gpu_cluster, environment, request, profile)


@pytest.mark.level2
@pytest.mark.parametrize("profile", ["CPX_NPS1"])
def test_dra_partition_MI350P(gpu_cluster, deviceconfig_install, environment, request, profile):
    """Test DRA ResourceSlice update after DCM partition change on MI350P."""
    gpu_series = get_gpu_series(gpu_cluster, environment)
    if gpu_series != 'MI350P':
        pytest.skip(f"Test designed for MI350P, cluster has {gpu_series}")
    run_dra_partition_test(gpu_cluster, environment, request, profile)


@pytest.mark.level2
@pytest.mark.parametrize("profile", ["CPX_NPS1"])
def test_dra_partition_MI300X(gpu_cluster, deviceconfig_install, environment, request, profile):
    """Test DRA ResourceSlice update after DCM partition change on MI300X."""
    gpu_series = get_gpu_series(gpu_cluster, environment)
    if gpu_series != 'MI300X':
        pytest.skip(f"Test designed for MI300X, cluster has {gpu_series}")
    run_dra_partition_test(gpu_cluster, environment, request, profile)


@pytest.mark.level2
@pytest.mark.parametrize("profile", ["CPX_NPS1"])
def test_dra_partition_MI325X(gpu_cluster, deviceconfig_install, environment, request, profile):
    """Test DRA ResourceSlice update after DCM partition change on MI325X."""
    gpu_series = get_gpu_series(gpu_cluster, environment)
    if gpu_series != 'MI325X':
        pytest.skip(f"Test designed for MI325X, cluster has {gpu_series}")
    run_dra_partition_test(gpu_cluster, environment, request, profile)
