#!/usr/bin/python3

"""
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
"""

"""
Test DRA driver device attributes.

This module combines two types of attribute tests:
1. Attribute structure/format validation (per DRA driver docs)
2. Attribute vs hardware validation (comparing with actual node data)

Based on: https://github.com/ROCm/k8s-gpu-dra-driver/blob/main/docs/driver-attributes.md
"""

import pytest
import time
import json
import logging
import re
import lib.k8_util as k8_util
import lib.dra_util as dra_util
import lib.amdgpu as amdgpu_util
import lib.node_gpu_collector as node_collector
from lib.util import K8Helper

Logger = logging.getLogger("k8.test_dra_driver_attributes")

# Common validation constants
# Based on: https://github.com/ROCm/k8s-gpu-dra-driver/blob/main/docs/driver-attributes.md
#
# Attributes shared by both full GPUs and partitions
COMMON_ATTRS = [
    "type",
    "productName",
    "driverVersion",
    "numaNode",
    "resource.kubernetes.io/pciBusID",
    "resource.kubernetes.io/pcieRoot",
]

# Required attributes for FULL GPUs
REQUIRED_FULL_GPU_ATTRS = COMMON_ATTRS + [
    "deviceID",
]

# Required attributes for PARTITIONS
# Partitions share the same attributes as full GPUs with type "amdgpu-partition"
# and an additional partitionProfile attribute
REQUIRED_PARTITION_ATTRS = COMMON_ATTRS + [
    "partitionProfile",
]

# Optional attributes
OPTIONAL_ATTRS = ["partitionProfile"]

# Critical attributes that must not be empty
CRITICAL_ATTRS = [
    "driverVersion",
]

REQUIRED_CAPACITY_ATTRS = ["memory", "computeUnits", "simdUnits"]


@pytest.fixture(autouse=True, scope="module")
def skip_module(environment):
    """Skip if not testing on K8s or if K8s version doesn't support DRA"""
    if environment.deployment_mode != "k8":
        pytest.skip(
            f"Skipping DRA driver attribute testcases for {environment.deployment_mode} deployment"
        )

    ret_code, version_info = k8_util.k8_get_version()
    if ret_code != 0:
        pytest.skip("Failed to get Kubernetes version")

    major_match = re.match(r"(\d+)", str(version_info.get("major", "0")))
    minor_match = re.match(r"(\d+)", str(version_info.get("minor", "0")))
    major = int(major_match.group(1)) if major_match else 0
    minor = int(minor_match.group(1)) if minor_match else 0

    if major < 1 or (major == 1 and minor < 32):
        pytest.skip(
            f"DRA requires Kubernetes 1.32+, but cluster is running {major}.{minor}"
        )

    dra_available, error_msg, _ = dra_util.check_dra_api_available()
    if not dra_available:
        pytest.skip(f"DRA API not available: {error_msg}")

    return


@pytest.fixture(scope="module")
def gpu_hardware_info(gpu_cluster, environment):
    """
    Collect hardware information for all GPU nodes once.

    This fixture collects hardware data only once per test module,
    reducing the number of pod creations from 3N to N (where N = number of nodes).

    Requires the amdgpu driver to be loaded before collecting hardware information.

    Returns:
        Dict mapping node_name -> {
            "hardware": {...},  # GPU hardware info from lspci + sysfs (includes partition data)
        }
    """
    Logger.info("=" * 70)
    Logger.info("Collecting hardware info for all GPU nodes (shared fixture)")
    Logger.info("=" * 70)

    gpu_nodes = [node for node in gpu_cluster.cluster_nodes if node.is_gpu_node()]
    K8Helper.triage(environment, len(gpu_nodes) > 0, "No AMD GPU nodes found in cluster")

    hardware_data = {}
    for node in gpu_nodes:
        node_name = node.host_name
        Logger.info(f"Collecting hardware info for node: {node_name}")

        # Collect all hardware info in one batch (includes partition data in sysfs)
        hw_info = node_collector.collect_gpu_hardware_info(gpu_cluster, node_name)

        hardware_data[node_name] = {
            "hardware": hw_info,
        }

        Logger.info(
            f"  ✓ {node_name}: {len(hw_info['gpus'])} GPUs with partition info"
        )

    Logger.info("=" * 70)
    Logger.info(f"✓ Hardware collection complete for {len(hardware_data)} node(s)")
    Logger.info("=" * 70)

    return hardware_data


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
    """Get all ResourceSlices from the cluster

    kubectl equivalent: kubectl get resourceslices.resource.k8s.io -o yaml

    Returns:
        list: List of ResourceSlice objects
    """
    api_version = _get_dra_api_version()
    ret_code, items, err = k8_util.k8_get_custom_resource_objects(
        group="resource.k8s.io", version=api_version, plural="resourceslices"
    )

    if ret_code != 0:
        Logger.error(f"Failed to get ResourceSlices: {err}")
        return []

    return items if items else []


def get_amd_gpu_devices_from_slices(resource_slices=None, node_name=None):
    """Extract AMD GPU devices from ResourceSlices

    Args:
        resource_slices: Optional - List of ResourceSlice objects. If None, fetches them automatically
        node_name: Optional - filter by specific node name

    Returns:
        list: List of device dicts with fields: name, type, node_name, attributes, capacity
    """
    # Fetch ResourceSlices if not provided
    if resource_slices is None:
        resource_slices = get_resource_slices()

    amd_devices = []

    Logger.info(f"Processing {len(resource_slices)} ResourceSlice(s)")

    for idx, slice_obj in enumerate(resource_slices):
        slice_name = slice_obj.get("metadata", {}).get("name", "unknown")
        driver_name = slice_obj.get("spec", {}).get("driver", "")
        slice_node = slice_obj.get("spec", {}).get("nodeName", "unknown")

        Logger.debug(
            f"  Slice {idx}: name={slice_name}, driver={driver_name}, node={slice_node}"
        )

        if driver_name != "gpu.amd.com":
            Logger.debug(f"    Skipping - driver is '{driver_name}' (not gpu.amd.com)")
            continue

        # Filter by node if specified
        if node_name and slice_node != node_name:
            continue

        devices = slice_obj.get("spec", {}).get("devices")

        # Skip if devices is null (e.g., controller nodes without GPUs)
        if devices is None:
            Logger.warning(
                f"  Slice '{slice_name}' on node '{slice_node}': No devices (null) - this may indicate a DRA driver bug for non-partitioned GPUs"
            )
            # Dump the ResourceSlice for debugging
            import yaml
            Logger.debug(f"ResourceSlice YAML for debugging:\n{yaml.dump(slice_obj, default_flow_style=False)}")
            continue

        Logger.info(
            f"  Slice '{slice_name}' on node '{slice_node}': {len(devices)} device(s)"
        )

        for dev_idx, device in enumerate(devices):
            device_name = device.get("name", "unknown")
            Logger.debug(f"    Device {dev_idx}: {device_name}")

            # Normalize the device structure to extract typed values
            # Actual structure has attributes like: cardIndex: {int: 9}
            normalized_device = normalize_device_attributes(device)

            # Extract normalized values
            gpu_attrs = (
                normalized_device.get("basic", {})
                .get("attributes", {})
                .get("gpu.amd.com", {})
            )
            device_type = gpu_attrs.get("type", "unknown")
            Logger.debug(f"      Type: {device_type}")

            # Create simpler device info structure
            device_info = {
                "name": normalized_device.get("name", ""),
                "type": device_type,
                "node_name": slice_node,
                "attributes": gpu_attrs,
                "capacity": normalized_device.get("basic", {}).get("capacity", {}),
            }

            amd_devices.append(device_info)

    Logger.info(f"Extracted {len(amd_devices)} total AMD GPU device(s) from all slices")

    return amd_devices


def normalize_device_attributes(device):
    """Normalize ResourceSlice device attributes from typed values to simple dict

    ResourceSlice attributes have structure like:
      cardIndex: {int: 9}
      deviceID: {string: "12151357581094058033"}
      type: {string: "amdgpu"}

    This function extracts the actual values.

    Args:
        device: Device object from ResourceSlice

    Returns:
        Normalized device dict with simple attribute values
    """
    normalized = {
        "name": device.get("name", ""),
        "basic": {"attributes": {"gpu.amd.com": {}}, "capacity": {}},
    }

    # Extract attributes
    raw_attributes = device.get("attributes", {})
    gpu_attrs = {}

    for attr_name, attr_value in raw_attributes.items():
        # Extract the actual value from typed structure
        if isinstance(attr_value, dict):
            for type_key in ("string", "int", "version", "bool"):
                if type_key in attr_value:
                    gpu_attrs[attr_name] = attr_value[type_key]
                    break
        else:
            gpu_attrs[attr_name] = attr_value

    normalized["basic"]["attributes"]["gpu.amd.com"] = gpu_attrs

    # Extract capacity
    raw_capacity = device.get("capacity", {})
    capacity = {}

    for cap_name, cap_value in raw_capacity.items():
        # Capacity values have structure like: {value: "256"}
        if isinstance(cap_value, dict):
            value = cap_value.get("value")
            if value is not None:
                # Prefix with gpu.amd.com for consistency
                capacity[f"gpu.amd.com/{cap_name}"] = value
        else:
            capacity[f"gpu.amd.com/{cap_name}"] = cap_value

    normalized["basic"]["capacity"] = capacity

    return normalized


def validate_required_attributes_present(
    device_name, gpu_attrs, required_attrs, environment
):
    """Validate that all required attributes are present"""
    for attr in required_attrs:
        K8Helper.triage(
            environment,
            attr in gpu_attrs,
            f"Device {device_name}: Missing required attribute '{attr}'",
        )
        if attr in gpu_attrs:
            Logger.info(f"  ✓ {attr}: {gpu_attrs[attr]}")


def validate_device_type(device_name, gpu_attrs, expected_type, environment):
    """Validate device type matches expected value"""
    device_type = gpu_attrs.get("type", "")
    K8Helper.triage(
        environment,
        device_type == expected_type,
        f"Device {device_name}: Expected type='{expected_type}', got '{device_type}'",
    )


def validate_pci_address_format(device_name, gpu_attrs, environment):
    """Validate PCI address format

    Per upstream docs, the attribute is "resource.kubernetes.io/pciBusID".
    """
    pci_addr = gpu_attrs.get("resource.kubernetes.io/pciBusID", "")

    K8Helper.triage(
        environment,
        pci_addr != "",
        f"Device {device_name}: resource.kubernetes.io/pciBusID is not set",
    )

    # Validate format if present
    if pci_addr:
        pci_pattern = r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9]$"
        K8Helper.triage(
            environment,
            re.match(pci_pattern, pci_addr) is not None,
            f"Device {device_name}: Invalid pciBusID format '{pci_addr}'",
        )


def validate_critical_attributes_not_empty(
    device_name, gpu_attrs, critical_attrs, environment
):
    """Validate that critical attributes are not null or empty"""
    for attr in critical_attrs:
        attr_value = gpu_attrs.get(attr)
        K8Helper.triage(
            environment,
            attr_value is not None and attr_value != "",
            f"Device {device_name}: Critical attribute '{attr}' is null or empty",
        )


def validate_capacity_attributes_present(
    device_name, capacity, required_capacity, environment
):
    """Validate that required capacity attributes are present"""
    for cap in required_capacity:
        qualified_cap = f"gpu.amd.com/{cap}"
        K8Helper.triage(
            environment,
            qualified_cap in capacity,
            f"Device {device_name}: Missing capacity '{qualified_cap}'",
        )
        if qualified_cap in capacity:
            Logger.info(f"  ✓ Capacity {cap}: {capacity[qualified_cap]}")


def validate_all_capacity_values_nonzero(device_name, capacity, environment):
    """Validate all capacity values (memory, computeUnits, simdUnits) are non-zero"""
    memory = capacity.get("gpu.amd.com/memory", "0")
    compute_units = capacity.get("gpu.amd.com/computeUnits", "0")
    simd_units = capacity.get("gpu.amd.com/simdUnits", "0")

    K8Helper.triage(
        environment,
        memory not in ["0", "", None],
        f"Device {device_name}: memory capacity is not set or zero",
    )

    K8Helper.triage(
        environment,
        compute_units not in ["0", "", None],
        f"Device {device_name}: computeUnits capacity is not set or zero",
    )

    K8Helper.triage(
        environment,
        simd_units not in ["0", "", None],
        f"Device {device_name}: simdUnits capacity is not set or zero",
    )


def validate_partition_profile_full_gpu(device_name, gpu_attrs, environment):
    """Validate partitionProfile for full GPU (should be spx_nps1)"""
    partition_profile = gpu_attrs.get("partitionProfile", "")
    K8Helper.triage(
        environment,
        partition_profile == "spx_nps1",
        f"Device {device_name}: Expected partitionProfile='spx_nps1', got '{partition_profile}'",
    )
    Logger.info(f"  ✓ partitionProfile: {partition_profile}")


def validate_partition_profile_format(device_name, gpu_attrs, environment):
    """Validate partitionProfile format for partition devices

    Format: <type>_<config>
    Type: spx, cpx, dpx, qpx
    Config: nps1, nps2, nps4
    """
    partition_profile = gpu_attrs.get("partitionProfile", "")

    # Check not empty
    K8Helper.triage(
        environment,
        partition_profile != "",
        f"Partition {device_name}: partitionProfile should not be empty",
    )

    # Split and validate format
    profile_parts = partition_profile.split("_")
    K8Helper.triage(
        environment,
        len(profile_parts) == 2,
        f"Partition {device_name}: partitionProfile '{partition_profile}' should have format '<type>_<config>' (e.g., 'spx_nps1')",
    )

    if len(profile_parts) == 2:
        partition_type = profile_parts[0]
        partition_config = profile_parts[1]

        # Validate first part (partition type)
        valid_partition_types = ["spx", "cpx", "dpx", "qpx"]
        K8Helper.triage(
            environment,
            partition_type in valid_partition_types,
            f"Partition {device_name}: partitionProfile type '{partition_type}' is invalid. Expected one of: {valid_partition_types}",
        )

        # Validate second part (configuration)
        valid_configs = ["nps1", "nps2", "nps4"]
        K8Helper.triage(
            environment,
            partition_config in valid_configs,
            f"Partition {device_name}: partitionProfile config '{partition_config}' is invalid. Expected one of: {valid_configs}",
        )

        Logger.info(
            f"  ✓ partitionProfile validated: type={partition_type}, config={partition_config}"
        )


def validate_common_device_attributes(
    device_name, gpu_attrs, capacity, required_attrs, environment
):
    """Validate common attributes shared by both full GPUs and partitions

    Validates:
    - Required attributes presence
    - PCI address format
    - Critical attributes not empty
    - Capacity attributes present
    """
    # Call common validation functions using module-level constants
    validate_required_attributes_present(
        device_name, gpu_attrs, required_attrs, environment
    )
    validate_pci_address_format(device_name, gpu_attrs, environment)
    validate_critical_attributes_not_empty(
        device_name, gpu_attrs, CRITICAL_ATTRS, environment
    )

    # Validate capacity attributes (common to both types)
    validate_capacity_attributes_present(
        device_name, capacity, REQUIRED_CAPACITY_ATTRS, environment
    )
    validate_all_capacity_values_nonzero(device_name, capacity, environment)


def validate_full_gpu_attributes(device, environment):
    """Validate attributes for a full GPU device

    As documented in: https://github.com/ROCm/k8s-gpu-dra-driver/blob/main/docs/driver-attributes.md

    Args:
        device: Device dict with fields: name, type, attributes, capacity
        environment: Test environment
    """
    global Logger

    device_name = device.get("name", "unknown")
    gpu_attrs = device.get("attributes", {})
    capacity = device.get("capacity", {})

    Logger.info(f"Validating full GPU device: {device_name}")
    Logger.info(f"  Attributes: {json.dumps(gpu_attrs, indent=2)}")
    Logger.info(f"  Capacity: {json.dumps(capacity, indent=2)}")

    # Validate full GPU required attributes
    validate_common_device_attributes(
        device_name, gpu_attrs, capacity, REQUIRED_FULL_GPU_ATTRS, environment
    )

    # Validate full GPU specific attributes
    validate_device_type(device_name, gpu_attrs, "amdgpu", environment)

    # Validate deviceID is not empty (critical for full GPUs)
    device_id = gpu_attrs.get("deviceID", "")
    K8Helper.triage(
        environment,
        device_id != "",
        f"Full GPU {device_name}: deviceID is required but missing or empty",
    )

    # Validate partitionProfile if present (optional for full GPUs)
    partition_profile = gpu_attrs.get("partitionProfile", "")
    if partition_profile:
        validate_partition_profile_full_gpu(device_name, gpu_attrs, environment)

    # Validate pcieRoot consistency with pciBusID
    pci_addr = gpu_attrs.get("resource.kubernetes.io/pciBusID", "")
    validate_pcie_root_attribute(device_name, pci_addr, gpu_attrs, environment)


def validate_partition_attributes(device, environment):
    """Validate attributes for a GPU partition device

    As documented in: https://github.com/ROCm/k8s-gpu-dra-driver/blob/main/docs/driver-attributes.md

    Partitions share the same attributes as full GPUs with type "amdgpu-partition".
    The pciBusID is identical across all partitions from the same physical device.

    Args:
        device: Device dict with fields: name, type, attributes, capacity
        environment: Test environment
    """
    global Logger

    device_name = device.get("name", "unknown")
    gpu_attrs = device.get("attributes", {})
    capacity = device.get("capacity", {})

    Logger.info(f"Validating GPU partition device: {device_name}")
    Logger.info(f"  Attributes: {json.dumps(gpu_attrs, indent=2)}")
    Logger.info(f"  Capacity: {json.dumps(capacity, indent=2)}")

    # Validate common attributes (using partition-specific required attrs)
    validate_common_device_attributes(
        device_name, gpu_attrs, capacity, REQUIRED_PARTITION_ATTRS, environment
    )

    # Validate partition specific attributes
    validate_device_type(device_name, gpu_attrs, "amdgpu-partition", environment)
    validate_partition_profile_format(device_name, gpu_attrs, environment)

    # Validate pcieRoot consistency with pciBusID
    pci_addr = gpu_attrs.get("resource.kubernetes.io/pciBusID", "")
    validate_pcie_root_attribute(device_name, pci_addr, gpu_attrs, environment)


def validate_device_identifiers_uniqueness(amd_devices, environment):
    """Validate device identifiers and naming convention per-node

    Validates that device names follow the gpu-<N>-<N> naming convention.
    Tracks deviceID and pciBusID per node for reference (partitions share these).
    """
    Logger.info("Validating uniqueness of device identifiers per-node...")

    # Group devices by node
    devices_by_node = {}
    for device in amd_devices:
        attrs = device.get("attributes", {})
        device_name = device.get("name", "")
        node_name = (
            attrs.get("nodeName")
            or attrs.get("node")
            or device.get("nodeName")
            or device.get("node")
        )
        if not node_name:
            node_name = "__unknown_node__"
            Logger.warning(
                f"Device '{device_name}' is missing node metadata; "
                f"validating it under fallback node bucket '{node_name}'"
            )
        devices_by_node.setdefault(node_name, []).append(device)

    Logger.info(f"Validating devices across {len(devices_by_node)} node(s)")
    Logger.debug(f"Nodes found: {list(devices_by_node.keys())}")
    for node_name, devices in devices_by_node.items():
        Logger.debug(f"  Node '{node_name}': {len(devices)} device(s)")

    # Validate uniqueness within each node
    for node_name, node_devices in devices_by_node.items():
        Logger.info(f"Validating {len(node_devices)} device(s) on node: {node_name}")

        # Maps: attribute_value -> device name (to detect duplicates within node)
        device_ids_map = {}
        pci_addrs_map = {}

        for device in node_devices:
            attrs = device.get("attributes", {})
            device_name = device.get("name", "")

            # Extract device attributes
            device_id = attrs.get("deviceID")
            pci_addr = attrs.get("resource.kubernetes.io/pciBusID")
            device_type = attrs.get("type")

            # DeviceID can be shared among partitions from same parent GPU
            # Just track them for reference, no uniqueness check
            if device_id:
                if device_id not in device_ids_map:
                    device_ids_map[device_id] = []
                device_ids_map[device_id].append(device_name)

            # pciBusID tracking - partitions share the same pciBusID
            if pci_addr:
                if pci_addr not in pci_addrs_map:
                    pci_addrs_map[pci_addr] = []
                pci_addrs_map[pci_addr].append(device_name)

            # Validate device naming convention: gpu-<N>-<N>
            name_pattern = r"^gpu-\d+-\d+$"
            K8Helper.triage(
                environment,
                re.match(name_pattern, device_name) is not None,
                f"Node {node_name}: Device name '{device_name}' doesn't match pattern 'gpu-<N>-<N>'",
            )

    Logger.info(
        "✓ All device identifiers validated per-node"
    )


def validate_common_attributes_consistency(amd_devices, environment):
    """Validate common attributes are consistent across all devices

    Checks that driverVersion and productName have the same value across all devices.
    productName may be empty on some GPU models (e.g. MI210) where firmware does not
    populate it — this is valid as long as it's consistent across all devices.
    """
    Logger.info("Validating common attributes are not null/empty and consistent...")

    # productName can be empty when firmware doesn't populate it (e.g. MI210)
    allow_empty = {"productName"}
    common_attrs = {
        "driverVersion": set(),
        "productName": set(),
    }

    for device in amd_devices:
        attrs = device.get("attributes", {})
        device_name = device.get("name", "")

        for attr_name in common_attrs.keys():
            attr_value = attrs.get(attr_name)

            if attr_name in allow_empty:
                if attr_value is None or attr_value == "":
                    Logger.info(
                        f"  ⓘ Device {device_name}: '{attr_name}' is empty "
                        f"(firmware/driver may not populate this field)"
                    )
                    attr_value = attr_value or ""
            else:
                K8Helper.triage(
                    environment,
                    attr_value is not None and attr_value != "",
                    f"Device {device_name}: Attribute '{attr_name}' is null or empty",
                )

            # Check consistency: all devices must have the same value
            if len(common_attrs[attr_name]) == 0:
                common_attrs[attr_name].add(attr_value)
            else:
                existing_value = list(common_attrs[attr_name])[0]
                K8Helper.triage(
                    environment,
                    attr_value == existing_value,
                    f"Device {device_name}: Attribute '{attr_name}' value '{attr_value}' differs from expected '{existing_value}'",
                )


def validate_partition_parent_correlation(amd_devices, environment):
    """Validate partitions are correctly linked to their parent GPUs

    Per upstream docs, partitions share the same pciBusID as their parent GPU.
    All partitions from the same physical device have identical resource.kubernetes.io/pciBusID.
    """
    Logger.info("Validating partition correlation to parent GPUs...")

    # Separate by type
    full_gpus = [d for d in amd_devices if d.get("type") == "amdgpu"]
    partitions = [d for d in amd_devices if d.get("type") == "amdgpu-partition"]

    if len(partitions) == 0:
        Logger.info("No partitions found - skipping partition correlation validation")
        return

    Logger.info(
        f"Found {len(partitions)} partition device(s), {len(full_gpus)} full GPU(s)"
    )

    # Build parent device maps by pciBusID
    parent_by_pci = {}

    # Add full GPU devices as potential parents
    for gpu in full_gpus:
        pci_addr = gpu.get("attributes", {}).get("resource.kubernetes.io/pciBusID", "")
        if pci_addr:
            parent_by_pci[pci_addr] = gpu

    # Group partitions by pciBusID (all partitions from same GPU share pciBusID)
    partitions_by_parent_pci = {}
    for part in partitions:
        parent_pci = part.get("attributes", {}).get("resource.kubernetes.io/pciBusID", "")
        if parent_pci:
            if parent_pci not in partitions_by_parent_pci:
                partitions_by_parent_pci[parent_pci] = []
            partitions_by_parent_pci[parent_pci].append(part)

    # Validate: all partitions sharing a pciBusID should have the same partitionProfile
    for pci_addr, parts in partitions_by_parent_pci.items():
        profiles = set(p.get("attributes", {}).get("partitionProfile", "") for p in parts)
        if len(profiles) > 1:
            K8Helper.triage(
                environment,
                False,
                f"Partitions at PCI {pci_addr}: Inconsistent partitionProfiles: {profiles}",
            )
        else:
            Logger.info(f"  ✓ PCI {pci_addr}: {len(parts)} partition(s), profile: {profiles.pop()}")

    Logger.info(
        f"✓ Validated {len(partitions)} partition(s) across {len(partitions_by_parent_pci)} PCI address(es)"
    )


def validate_pcie_root_attribute(device_name, pci_addr, gpu_attrs, environment):
    """Validate resource.kubernetes.io/pcieRoot attribute format and consistency with pciBusID

    The pcieRoot should be derived from the PCI address (domain:bus portion).
    For example, PCI address "0000:83:00.0" should have pcieRoot "pci0000:83".

    Args:
        device_name: Name of the device
        pci_addr: PCI address from resource.kubernetes.io/pciBusID attribute
        gpu_attrs: Device attributes dict
        environment: Test environment

    Returns:
        None
    """
    pcie_root = gpu_attrs.get("resource.kubernetes.io/pcieRoot", "")

    # pcieRoot is a required attribute per docs, but validate gracefully
    if not pcie_root:
        Logger.debug(f"  {device_name}: resource.kubernetes.io/pcieRoot not present")
        return

    # Validate format: should match "pciDDDD:BB" pattern (e.g., "pci0000:c9")
    pcie_root_pattern = r"^pci[0-9a-fA-F]{4}:[0-9a-fA-F]{2}$"
    if not re.match(pcie_root_pattern, pcie_root):
        K8Helper.triage(
            environment,
            False,
            f"Device {device_name}: pcieRoot '{pcie_root}' has invalid format. Expected format: pciDDDD:BB (e.g., 'pci0000:c9')",
        )

    # Validate consistency with PCI address
    # pciBusID: 0000:cc:00.0 -> pcieRoot: pci0000:cc (or parent bus)
    if not pci_addr:
        Logger.debug(
            f"  {device_name}: Cannot validate pcieRoot consistency - pciBusID is missing"
        )
        return

    Logger.info(f"  ✓ pcieRoot: {pcie_root}")


def test_dra_driver_device_attributes(dra_driver_install, environment, gpu_cluster):
    """Test that DRA driver advertises all required device attributes.

    This test validates that the DRA driver correctly advertises all required
    device attributes for AMD GPUs via ResourceSlices. It checks both the
    presence and validity of each attribute.

    Validates:
        - ResourceSlices are created and populated (waits up to 60 seconds)
        - Each GPU device has all required attributes per upstream docs:
            * type: Device type ("amdgpu" or "amdgpu-partition")
            * deviceID: PCI device ID (e.g., "0x740f") - full GPUs only
            * productName: Product name (normalized)
            * driverVersion: Kernel driver version (semver)
            * numaNode: NUMA node the GPU is attached to
            * resource.kubernetes.io/pciBusID: PCI bus address (e.g., "0000:19:00.0")
            * resource.kubernetes.io/pcieRoot: PCIe root complex identifier
            * partitionProfile: Partition profile (optional, e.g., "spx_nps1")
        - Capacity attributes: memory, computeUnits, simdUnits
        - Attribute values are valid (correct types, reasonable ranges)
        - Partition attributes if device is partitioned

    Fixtures:
        dra_driver_install: Ensures DRA driver is installed and running

    kubectl equivalents:
        kubectl get resourceslices.resource.k8s.io
        kubectl get resourceslices -o yaml

    Expected outcome:
        All GPU devices have complete and valid attribute sets

    Reference:
        https://github.com/ROCm/k8s-gpu-dra-driver/blob/main/docs/driver-attributes.md
    """
    global Logger

    # Wait for ResourceSlices to be created and populated
    # DRA driver may take some time to discover and advertise devices
    Logger.info("Waiting for ResourceSlices to be populated...")
    max_wait = 60  # Wait up to 60 seconds
    amd_devices = []

    for attempt in range(6):  # 6 attempts x 10 seconds = 60 seconds
        time.sleep(10)

        resource_slices = get_resource_slices()
        amd_devices = get_amd_gpu_devices_from_slices(resource_slices)

        if len(amd_devices) > 0:
            Logger.info(
                f"Found {len(amd_devices)} AMD GPU device(s) in ResourceSlices after {(attempt + 1) * 10} seconds"
            )
            break

        Logger.debug(f"Attempt {attempt + 1}: No AMD devices found yet, waiting...")

    # Final check
    resource_slices = get_resource_slices()
    K8Helper.triage(
        environment,
        len(resource_slices) > 0,
        "No ResourceSlices found in cluster - DRA driver may not be running correctly",
    )

    Logger.info(f"Found {len(resource_slices)} ResourceSlice(s) in cluster")

    # Extract AMD GPU devices
    amd_devices = get_amd_gpu_devices_from_slices(resource_slices)

    # Provide more detailed error message if no devices found
    if len(amd_devices) == 0:
        # Check if we have GPU nodes with AMD hardware and driver loaded
        gpu_nodes = [node for node in gpu_cluster.cluster_nodes if node.is_gpu_node()]

        # Check if we have ResourceSlices with null devices (DRA driver bug)
        null_device_slices = [
            rs for rs in resource_slices
            if rs.get("spec", {}).get("driver") == "gpu.amd.com" and rs.get("spec", {}).get("devices") is None
        ]

        error_msg = "No AMD GPU devices found in ResourceSlices after waiting. "

        if len(gpu_nodes) == 0:
            error_msg += "\nNo GPU nodes found with AMD GPU hardware (lspci). "
            error_msg += "Check if: 1) AMD GPU hardware exists, 2) amdgpu driver is loaded"
        elif null_device_slices:
            # We have GPU nodes but ResourceSlices have null devices - DRA driver bug
            error_msg += f"\nFound {len(gpu_nodes)} GPU node(s) with AMD GPUs, but {len(null_device_slices)} ResourceSlice(s) have null devices. "
            error_msg += "This is likely a DRA driver bug for non-partitioned GPUs. "
            error_msg += f"\nGPU nodes: {[n.host_name for n in gpu_nodes]}"
            error_msg += f"\nAffected ResourceSlices: {[rs.get('metadata', {}).get('name') for rs in null_device_slices]}"
        else:
            error_msg += f"\nFound {len(gpu_nodes)} GPU node(s) but no ResourceSlices from DRA driver. "
            error_msg += "Check if: 1) DRA driver pods are running, 2) DRA driver has permissions to publish ResourceSlices"

        K8Helper.triage(environment, False, error_msg)

    Logger.info(f"Found {len(amd_devices)} AMD GPU device(s) in ResourceSlices")

    # Track device types
    full_gpu_count = 0
    partition_count = 0

    # Validate each device
    for device in amd_devices:
        device_type = device.get("type", "")

        if device_type == "amdgpu":
            full_gpu_count += 1
            validate_full_gpu_attributes(device, environment)
        elif device_type == "amdgpu-partition":
            partition_count += 1
            validate_partition_attributes(device, environment)
        else:
            Logger.warning(f"Unknown device type: {device_type}")

    Logger.info(f"Validated {full_gpu_count} full GPU(s)")
    Logger.info(f"Validated {partition_count} partition(s)")

    # At least one device should be validated
    K8Helper.triage(
        environment,
        (full_gpu_count + partition_count) > 0,
        "No valid AMD GPU devices found",
    )

    # Cross-device validations
    validate_device_identifiers_uniqueness(amd_devices, environment)
    validate_common_attributes_consistency(amd_devices, environment)
    validate_partition_parent_correlation(amd_devices, environment)


def test_dra_gpu_count_matches_hardware(
    dra_driver_install, environment, gpu_hardware_info
):
    """Test that DRA advertises the same number of GPUs as detected by hardware.

    This test validates that the GPU count advertised by the DRA driver matches
    the actual number of GPUs detected on each node via hardware inspection
    (lspci and sysfs). This is a basic sanity check before detailed attribute
    validation.

    Validates:
        - DRA advertised GPU count matches hardware detected count
        - Count check performed per-node (all GPU nodes validated)
        - Only "amdgpu" type devices counted (partitions excluded)
        - Reports specific node if mismatch found

    Fixtures:
        dra_driver_install: Ensures DRA driver is installed
        gpu_hardware_info: Cached hardware info for all GPU nodes

    Hardware Detection Method:
        - Spawns privileged debug pod on each GPU node
        - Runs lspci to detect AMD GPUs
        - Reads /sys/class/drm to enumerate devices
        - Collects data once per test module (shared fixture)

    kubectl equivalents:
        kubectl get resourceslices -o yaml
        # Hardware detection uses privileged pod with lspci and sysfs

    Expected outcome:
        DRA count == Hardware count for all GPU nodes

    Notes:
        If this test fails but test_dra_devices_match_hardware passes,
        it may indicate a filtering or type classification issue.
    """
    global Logger

    # Check each GPU node using cached hardware info
    mismatches = []

    for node_name, hw_data in gpu_hardware_info.items():
        Logger.info(f"Validating node: {node_name}")

        # Use cached hardware info (collected once by fixture)
        hw_info = hw_data["hardware"]

        # Get DRA advertised devices for this node
        dra_devices = get_amd_gpu_devices_from_slices(node_name=node_name)

        # Count hardware GPUs and partitions
        hw_gpu_count = len(hw_info["gpus"])
        hw_partition_count = sum(len(gpu.get("partitions", [])) for gpu in hw_info["gpus"])

        # Count DRA advertised devices by type
        dra_full_gpu_count = len([d for d in dra_devices if d.get("type") == "amdgpu"])
        dra_partition_count = len([d for d in dra_devices if d.get("type") == "amdgpu-partition"])

        Logger.info(f"  Hardware: {hw_gpu_count} GPU(s), {hw_partition_count} partition(s)")
        Logger.info(f"  DRA advertised: {dra_full_gpu_count} full GPU(s), {dra_partition_count} partition(s)")

        # Validation logic:
        # If no partitions detected in hardware, DRA should report full GPUs
        # If partitions detected, DRA reports all as partitions (including full GPU renderD)
        if hw_partition_count == 0:
            # Unpartitioned GPUs - expect full GPU type
            if hw_gpu_count != dra_full_gpu_count:
                mismatches.append(
                    f"{node_name}: Unpartitioned GPUs - HW={hw_gpu_count}, DRA full GPUs={dra_full_gpu_count}"
                )
        else:
            # Partitioned GPUs - expect partition type for all devices
            if hw_partition_count != dra_partition_count:
                mismatches.append(
                    f"{node_name}: Partitioned GPUs - HW partitions={hw_partition_count}, DRA partitions={dra_partition_count}"
                )

    # Report results
    if mismatches:
        error_msg = f"GPU/partition count mismatches found:\n" + "\n".join(mismatches)
        Logger.error(error_msg)
        K8Helper.triage(environment, False, error_msg)
    else:
        Logger.info("✓ All nodes: DRA device count matches hardware detection")


def compare_hw_attribute_with_dra(
    node_name, pci_addr, hw_value, dra_value, attr_name, is_critical=True
):
    """Compare a hardware attribute value with DRA advertised value

    Args:
        node_name: Name of the node
        pci_addr: PCI address of the GPU
        hw_value: Hardware value
        dra_value: DRA advertised value
        attr_name: Name of the attribute being compared
        is_critical: If True, returns error message on mismatch; if False, logs warning only

    Returns:
        str or None: Error message if mismatch and is_critical=True, None otherwise
    """
    # Skip comparison if either value is empty
    if not hw_value or not dra_value:
        return None

    if hw_value != dra_value:
        msg = f"Node {node_name}, PCI {pci_addr}: {attr_name} mismatch - HW={hw_value}, DRA={dra_value}"
        if is_critical:
            Logger.error(msg)
            return msg
        else:
            Logger.warning(msg)
            return None
    else:
        Logger.debug(f"    ✓ {attr_name} matches: {hw_value}")
        return None


def validate_device_id_match(node_name, pci_addr, hw_gpu, dra_gpu):
    """Validate device ID matches between hardware and DRA

    Args:
        node_name: Name of the node
        pci_addr: PCI address of the GPU
        hw_gpu: Hardware GPU information dict
        dra_gpu: DRA GPU device dict

    Returns:
        str or None: Error message if mismatch, None if match
    """
    hw_device_id = hw_gpu.get("device_id", "").lower()
    dra_device_id = dra_gpu["attributes"].get("deviceID", "")

    # Normalize DRA device ID (remove 0x prefix if present)
    if dra_device_id.startswith("0x"):
        dra_device_id = dra_device_id[2:]
    dra_device_id = dra_device_id.lower()

    return compare_hw_attribute_with_dra(
        node_name, pci_addr, hw_device_id, dra_device_id, "Device ID", is_critical=True
    )


def validate_product_name_match(node_name, pci_addr, hw_gpu, dra_gpu):
    """Validate product name matches between hardware and DRA

    Product name may be empty if firmware/driver doesn't populate it.
    Mismatch is treated as a warning, not an error.

    Args:
        node_name: Name of the node
        pci_addr: PCI address of the GPU
        hw_gpu: Hardware GPU information dict
        dra_gpu: DRA GPU device dict

    Returns:
        None: Always returns None (warnings only, no errors)
    """
    hw_product = hw_gpu.get("product_name", "")
    dra_product = dra_gpu["attributes"].get("productName", "")

    if hw_product and not dra_product:
        msg = (
            f"Node {node_name}, PCI {pci_addr}: Hardware reports productName='{hw_product}' "
            f"but DRA driver advertises empty"
        )
        Logger.error(msg)
        return msg

    if not hw_product and not dra_product:
        Logger.info(
            f"  ⓘ Node {node_name}, PCI {pci_addr}: productName empty in both hardware and DRA "
            f"— firmware/driver does not populate this field"
        )
        return None

    # DRA driver normalizes product names (e.g., spaces → underscores)
    hw_normalized = hw_product.replace(" ", "_").lower()
    dra_normalized = dra_product.replace(" ", "_").lower()

    if hw_normalized != dra_normalized:
        msg = (
            f"Node {node_name}, PCI {pci_addr}: Product name mismatch - "
            f"HW='{hw_product}', DRA='{dra_product}'"
        )
        Logger.error(msg)
        return msg

    Logger.debug(f"    ✓ Product name matches: HW='{hw_product}', DRA='{dra_product}'")
    return None


def validate_driver_version_match(node_name, pci_addr, hw_gpu, dra_gpu):
    """Validate driverVersion matches between hardware and DRA

    Args:
        node_name: Name of the node
        pci_addr: PCI address of the GPU
        hw_gpu: Hardware GPU information dict
        dra_gpu: DRA GPU device dict

    Returns:
        str or None: Error message if mismatch, None if match
    """
    hw_driver_ver = hw_gpu.get("driverVersion", "")
    dra_driver_ver = dra_gpu["attributes"].get("driverVersion", "")

    # Normalize to semver (major.minor.patch) — the HW version from
    # /sys/module/amdgpu/version may include a build suffix (e.g. 6.19.14.31400000)
    # while the DRA driver publishes only the semver portion (e.g. 6.19.14).
    def _to_semver(v):
        parts = v.split(".")
        return ".".join(parts[:3]) if len(parts) > 3 else v

    hw_driver_ver = _to_semver(hw_driver_ver)
    dra_driver_ver = _to_semver(dra_driver_ver)

    return compare_hw_attribute_with_dra(
        node_name,
        pci_addr,
        hw_driver_ver,
        dra_driver_ver,
        "driverVersion",
        is_critical=True,
    )


def validate_partition_profile_from_hardware(
    node_name, pci_addr, hw_gpu, dra_devices_for_pci, environment
):
    """Validate partition profile and type based on hardware partition state.

    Cross-references the partition state from sysfs against what the DRA driver
    advertises in ResourceSlice objects. The sysfs source of truth is:

        /sys/module/amdgpu/drivers/pci:amdgpu/<pci_addr>/current_compute_partition
        /sys/module/amdgpu/drivers/pci:amdgpu/<pci_addr>/current_memory_partition

    Only GPUs whose PCI address appears under that sysfs path have valid
    partition state. GPUs visible to lspci but absent from that path are not
    bound to the amdgpu kernel driver (e.g., vfio-pci passthrough, driver not
    loaded, or blacklisted).

    Validation rules:
        1. current_compute_partition and current_memory_partition must be both
           empty or both non-empty (they are always set as a pair).
        2. If current_memory_partition is empty → ResourceSlice type = 'amdgpu'
           (full GPU, no partitioning).
        3. If SPX + NPS1 → type = 'amdgpu' (default full-GPU mode).
           Otherwise → type = 'amdgpu-partition'.
        4. If both fields are set → partitionProfile in ResourceSlice must equal
           "<compute>_<memory>" in lowercase (e.g., "dpx_nps2").
        5. If both fields are empty (driver not bound) → partitionProfile must
           be empty. The DRA driver must NOT advertise a stale or default
           partition profile for GPUs it cannot query via sysfs.

    Args:
        node_name: Kubernetes node hostname.
        pci_addr: Full PCI address (e.g., "0000:06:00.0").
        hw_gpu: Hardware GPU dict from node_gpu_collector (merged lspci + sysfs).
        dra_devices_for_pci: List of DRA ResourceSlice devices at this PCI addr.
        environment: Test environment fixture.
    """
    hw_compute_part = (hw_gpu.get("current_compute_partition") or "").strip().upper()
    hw_memory_part = (hw_gpu.get("current_memory_partition") or "").strip().upper()

    # Rule 1: compute/memory partition should be both empty or both non-empty
    K8Helper.triage(
        environment,
        bool(hw_compute_part) == bool(hw_memory_part),
        f"Node {node_name}, PCI {pci_addr}: partition state invalid - current_compute_partition='{hw_compute_part}', current_memory_partition='{hw_memory_part}' (both must be empty or both non-empty)",
    )

    # Rules 2 & 3: Determine expected type based on partition state
    if not hw_memory_part:
        # Rule 2: No partitioning
        expected_type = "amdgpu"
    elif hw_compute_part == "SPX" and hw_memory_part == "NPS1":
        # Rule 3: SPX_NPS1 is treated as full GPU
        expected_type = "amdgpu"
    else:
        # Rule 3: Any other partitioning scheme
        expected_type = "amdgpu-partition"

    # Validate advertised types match expected
    advertised_types = {d.get("type", "") for d in dra_devices_for_pci}
    K8Helper.triage(
        environment,
        expected_type in advertised_types,
        f"Node {node_name}, PCI {pci_addr}: Expected advertised type '{expected_type}' from hardware partition state ({hw_compute_part}, {hw_memory_part}), got {sorted(advertised_types)}",
    )

    # Rule 4: If partitioned, validate partition profile matches
    if hw_compute_part and hw_memory_part:
        expected_profile = f"{hw_compute_part.lower()}_{hw_memory_part.lower()}"

        # Collect advertised partition profiles from all devices at this PCI address
        advertised_profiles = set()
        for d in dra_devices_for_pci:
            profile = (
                (d.get("attributes", {}).get("partitionProfile") or "").strip().lower()
            )
            if profile:
                advertised_profiles.add(profile)

        K8Helper.triage(
            environment,
            expected_profile in advertised_profiles,
            f"Node {node_name}, PCI {pci_addr}: Expected partitionProfile '{expected_profile}' from hardware partition state, got {sorted(advertised_profiles)}",
        )
        Logger.debug(f"    ✓ partitionProfile matches: {expected_profile}")
    else:
        # Rule 5: GPU not bound to amdgpu driver (not under
        # /sys/module/amdgpu/drivers/pci:amdgpu/) — partitionProfile must be empty
        for d in dra_devices_for_pci:
            profile = (
                (d.get("attributes", {}).get("partitionProfile") or "").strip()
            )
            K8Helper.triage(
                environment,
                profile == "",
                f"Node {node_name}, PCI {pci_addr}: GPU has no driver-bound partition state "
                f"but DRA advertises partitionProfile='{profile}' (should be empty)",
            )
        Logger.debug(f"    ✓ partitionProfile correctly empty (no driver partition state)")


def test_dra_devices_match_hardware(dra_driver_install, environment, gpu_hardware_info):
    """Test that DRA advertised GPU attributes match hardware per PCI address.

    This is the most comprehensive validation test. It performs deep attribute
    comparison for each GPU individually, using PCI address as the correlation
    key. This provides precise debugging - identifies exactly which GPU and
    which attribute has mismatched data.

    Validates (per GPU, correlated by resource.kubernetes.io/pciBusID):
        - pciBusID: DRA matches hardware PCI address (normalized to long format)
        - deviceID: DRA matches hardware PCI device ID
        - productName: DRA matches hardware product name
        - driverVersion: DRA matches hardware driver version
        - Partition attributes (if GPU is partitioned):
            * partitionProfile: Matches compute_memory partition format

    Fixtures:
        dra_driver_install: Ensures DRA driver is installed
        gpu_hardware_info: Cached hardware info for all GPU nodes

    Hardware Detection Method:
        - Reads /sys/class/drm/card*/device/* for GPU attributes
        - Uses lspci for PCI address validation
        - Collects partition info from sysfs
        - Data collected once per module (shared fixture)

    Correlation Method:
        1. Build hardware GPU map by PCI address (normalized)
        2. For each DRA device, find matching hardware GPU by PCI
        3. Compare all attributes individually
        4. Report any mismatches with details

    kubectl equivalents:
        kubectl get resourceslices -o yaml
        # Hardware: privileged pod reading /sys/class/drm and lspci

    Expected outcome:
        All DRA GPU attributes match corresponding hardware values

    Error Reporting:
        - Lists all mismatches with node, PCI address, attribute name
        - Shows expected (hardware) vs actual (DRA) values
        - Reports missing GPUs (in hardware but not in DRA or vice versa)

    Notes:
        This test is most useful for debugging attribute issues. If it fails,
        check the detailed mismatch report to identify the specific problem.
    """
    global Logger

    all_mismatches = []

    for node_name, hw_data in gpu_hardware_info.items():
        Logger.info(f"Validating GPU attributes for node: {node_name}")

        # Use cached hardware info (collected once by fixture)
        hw_info = hw_data["hardware"]

        # Build hardware GPU map by PCI address (use full format from collector)
        hw_gpus_by_pci = {}
        for gpu in hw_info["gpus"]:
            # Prefer pci_address_full (always set by enhanced collector)
            # Fallback to pci_address with normalization for backward compatibility
            pci_addr = gpu.get("pci_address_full")
            if not pci_addr:
                pci_addr = gpu.get("pci_address", "")
                # Normalize short format to long format (0000:06:00.0)
                if pci_addr and pci_addr.count(":") == 1:
                    pci_addr = f"0000:{pci_addr}"

            if pci_addr:
                hw_gpus_by_pci[pci_addr] = gpu

        Logger.info(f"  Hardware GPUs by PCI: {list(hw_gpus_by_pci.keys())}")

        # Get DRA devices for this node
        dra_devices = get_amd_gpu_devices_from_slices(node_name=node_name)

        # Build DRA device map by PCI address (can have full GPU + partitions on same PCI)
        # Per upstream docs, attribute is "resource.kubernetes.io/pciBusID"
        dra_devices_by_pci = {}
        for device in dra_devices:
            pci_addr = device["attributes"].get("resource.kubernetes.io/pciBusID", "")
            if pci_addr:
                if pci_addr not in dra_devices_by_pci:
                    dra_devices_by_pci[pci_addr] = []
                dra_devices_by_pci[pci_addr].append(device)

        # Pick representative device per PCI for common attribute checks
        # Prefer full GPU (type: amdgpu), otherwise the partition with lowest renderIndex
        # (which represents the full GPU view in partitioned mode)
        dra_primary_by_pci = {}
        for pci_addr, devices in dra_devices_by_pci.items():
            full_gpu = next((d for d in devices if d.get("type") == "amdgpu"), None)
            if full_gpu:
                dra_primary_by_pci[pci_addr] = full_gpu
            else:
                # For partitioned GPUs, pick the one with lowest renderIndex
                # This matches the hardware GPU's cardIndex/renderIndex
                sorted_devices = sorted(
                    devices,
                    key=lambda d: d.get("attributes", {}).get("renderIndex", 999)
                )
                dra_primary_by_pci[pci_addr] = sorted_devices[0] if sorted_devices else devices[0]

        Logger.info(f"  DRA devices by PCI: {list(dra_devices_by_pci.keys())}")

        # Check for missing GPUs
        hw_pci_addrs = set(hw_gpus_by_pci.keys())
        dra_pci_addrs = set(dra_devices_by_pci.keys())

        missing_in_dra = hw_pci_addrs - dra_pci_addrs
        extra_in_dra = dra_pci_addrs - hw_pci_addrs

        if missing_in_dra:
            error_msg = f"Node {node_name}: GPUs in hardware but not advertised in DRA: {missing_in_dra}"
            Logger.error(error_msg)
            all_mismatches.append(error_msg)

        if extra_in_dra:
            error_msg = f"Node {node_name}: GPUs advertised in DRA but not found in hardware: {extra_in_dra}"
            Logger.error(error_msg)
            all_mismatches.append(error_msg)

        # Compare each matched GPU's attributes
        for pci_addr in hw_pci_addrs & dra_pci_addrs:
            hw_gpu = hw_gpus_by_pci[pci_addr]
            dra_devices_for_pci = dra_devices_by_pci[pci_addr]
            dra_gpu = dra_primary_by_pci[pci_addr]

            Logger.debug(f"  Comparing GPU at {pci_addr}")

            # Validate partition profile and type from hardware partition state
            validate_partition_profile_from_hardware(
                node_name, pci_addr, hw_gpu, dra_devices_for_pci, environment
            )

            # If hardware shows partitions, validate DRA reports correct partition count
            hw_partitions = hw_gpu.get("partitions", [])
            if hw_partitions:
                hw_partition_count = len(hw_partitions)
                dra_partition_count = len([d for d in dra_devices_for_pci if d.get("type") == "amdgpu-partition"])

                K8Helper.triage(
                    environment,
                    hw_partition_count == dra_partition_count,
                    f"Node {node_name}, PCI {pci_addr}: Hardware has {hw_partition_count} partition(s), "
                    f"but DRA advertises {dra_partition_count} partition(s)",
                )
                Logger.info(f"  ✓ Partition count matches: {dra_partition_count} partition(s)")

            # Compare GPU attributes between hardware and DRA
            error_msg = validate_device_id_match(node_name, pci_addr, hw_gpu, dra_gpu)
            if error_msg:
                all_mismatches.append(error_msg)

            error_msg = validate_product_name_match(node_name, pci_addr, hw_gpu, dra_gpu)
            if error_msg:
                all_mismatches.append(error_msg)

            error_msg = validate_driver_version_match(
                node_name, pci_addr, hw_gpu, dra_gpu
            )
            if error_msg:
                all_mismatches.append(error_msg)

        if not missing_in_dra and not all_mismatches:
            Logger.info(f"  ✓ All {len(hw_pci_addrs)} GPUs validated successfully")

    # Final triage
    K8Helper.triage(
        environment,
        len(all_mismatches) == 0,
        (
            f"GPU attribute mismatches found: {all_mismatches}"
            if all_mismatches
            else ""
        ),
    )

    Logger.info(
        f"✓ All nodes: DRA GPU attributes match hardware (validated {sum(len(hw_data['hardware']['gpus']) for hw_data in gpu_hardware_info.values())} total GPUs)"

    )


@pytest.mark.level1
def test_dra_partition_profile_requires_driver_binding(
    dra_driver_install, environment, gpu_hardware_info
):
    """Test that partitionProfile is only advertised for GPUs bound to the amdgpu driver.

    Background:
        The DRA driver discovers GPUs and publishes their attributes in
        ResourceSlice objects. One of those attributes is partitionProfile,
        which reports the current GPU partitioning mode (e.g., "spx_nps1",
        "dpx_nps2"). This value is read from sysfs:

            /sys/module/amdgpu/drivers/pci:amdgpu/<pci_addr>/current_compute_partition
            /sys/module/amdgpu/drivers/pci:amdgpu/<pci_addr>/current_memory_partition

        Only GPUs bound to the amdgpu kernel driver appear under that path.
        GPUs that are not bound (e.g., assigned to vfio-pci for passthrough,
        or the driver is blacklisted/not loaded) will NOT have entries there.

    Bug being validated:
        Today the DRA driver advertises a partitionProfile attribute regardless
        of whether the GPU is actually under /sys/module/amdgpu/drivers/pci:amdgpu/.
        When the driver is not bound, the profile value is stale or defaulted,
        which can mislead schedulers and users.

    Expected DRA driver behavior:
        - GPU bound to amdgpu (PCI addr exists under sysfs driver path):
            partitionProfile = "<compute>_<memory>" from sysfs (e.g., "dpx_nps2")
        - GPU NOT bound to amdgpu (PCI addr absent from sysfs driver path):
            partitionProfile = "" (empty string, or attribute omitted entirely)

    How the test works:
        1. The gpu_hardware_info fixture collects GPU data from two sources:
           - lspci: finds ALL AMD GPUs by PCI vendor ID 1002 (any driver)
           - sysfs: reads /sys/module/amdgpu/drivers/pci:amdgpu/ (only bound GPUs)
        2. GPUs that appear in lspci but have no sysfs partition fields
           (current_compute_partition is None) are classified as "driver-unbound".
        3. For each GPU, the test reads the partitionProfile attribute from the
           DRA ResourceSlice (kubectl get resourceslices -o yaml).
        4. Driver-bound GPUs: partitionProfile must match hardware sysfs state.
        5. Driver-unbound GPUs: partitionProfile must be empty.

    DRA driver fix guidance:
        In the GPU discovery/enumeration code, before setting partitionProfile:
            1. Check if the GPU's PCI address exists under
               /sys/module/amdgpu/drivers/pci:amdgpu/
            2. If not present, set partitionProfile to "" (or omit the attribute)
            3. Only read current_compute_partition / current_memory_partition
               when the GPU is confirmed under that path

    Fixtures:
        dra_driver_install: Ensures DRA driver DaemonSet is deployed and running.
        gpu_hardware_info: Cached per-node hardware info from lspci + sysfs.
            Only GPUs bound to amdgpu will have sysfs partition fields populated.
    """
    global Logger

    errors = []

    for node_name, hw_data in gpu_hardware_info.items():
        Logger.info(f"Checking partitionProfile driver binding for node: {node_name}")

        hw_info = hw_data["hardware"]

        # Build sets of PCI addresses with and without sysfs data.
        # sysfs data (current_compute_partition etc.) only exists for GPUs
        # under /sys/module/amdgpu/drivers/pci:amdgpu/
        driver_bound_pci = set()
        driver_unbound_pci = set()

        for gpu in hw_info["gpus"]:
            pci_addr = gpu.get("pci_address_full")
            if not pci_addr:
                pci_addr = gpu.get("pci_address", "")
                if pci_addr and pci_addr.count(":") == 1:
                    pci_addr = f"0000:{pci_addr}"

            if not pci_addr:
                continue

            has_sysfs = bool(
                gpu.get("current_compute_partition") is not None
                or gpu.get("current_memory_partition") is not None
            )
            if has_sysfs:
                driver_bound_pci.add(pci_addr)
            else:
                driver_unbound_pci.add(pci_addr)

        Logger.info(f"  Driver-bound GPUs: {sorted(driver_bound_pci)}")
        Logger.info(f"  Driver-unbound GPUs: {sorted(driver_unbound_pci)}")

        # Get DRA devices grouped by PCI address
        dra_devices = get_amd_gpu_devices_from_slices(node_name=node_name)
        dra_devices_by_pci = {}
        for device in dra_devices:
            pci = device["attributes"].get("resource.kubernetes.io/pciBusID", "")
            if pci:
                dra_devices_by_pci.setdefault(pci, []).append(device)

        # Check driver-bound GPUs: partitionProfile should be valid
        for pci_addr in driver_bound_pci:
            if pci_addr not in dra_devices_by_pci:
                continue

            hw_gpu = next(
                (g for g in hw_info["gpus"]
                 if g.get("pci_address_full", f"0000:{g.get('pci_address', '')}") == pci_addr),
                None,
            )
            if not hw_gpu:
                continue

            hw_compute = (hw_gpu.get("current_compute_partition") or "").strip().upper()
            hw_memory = (hw_gpu.get("current_memory_partition") or "").strip().upper()

            if hw_compute and hw_memory:
                expected = f"{hw_compute.lower()}_{hw_memory.lower()}"
                for d in dra_devices_by_pci[pci_addr]:
                    profile = (d.get("attributes", {}).get("partitionProfile") or "").strip().lower()
                    if profile and profile != expected:
                        msg = (
                            f"Node {node_name}, PCI {pci_addr}: "
                            f"partitionProfile='{profile}' != expected '{expected}' from hardware"
                        )
                        Logger.error(msg)
                        errors.append(msg)

                Logger.info(f"  ✓ {pci_addr}: partitionProfile matches hardware ({expected})")

        # Check driver-unbound GPUs: partitionProfile must be empty
        for pci_addr in driver_unbound_pci:
            if pci_addr not in dra_devices_by_pci:
                continue

            for d in dra_devices_by_pci[pci_addr]:
                profile = (d.get("attributes", {}).get("partitionProfile") or "").strip()
                if profile:
                    msg = (
                        f"Node {node_name}, PCI {pci_addr}: GPU not under "
                        f"/sys/module/amdgpu/drivers/pci:amdgpu/ but DRA advertises "
                        f"partitionProfile='{profile}' (should be empty)"
                    )
                    Logger.error(msg)
                    errors.append(msg)

            Logger.info(f"  ✓ {pci_addr}: partitionProfile correctly empty (driver not bound)")

        # Also check for DRA devices with PCI addresses not in hardware at all
        unknown_pci = set(dra_devices_by_pci.keys()) - driver_bound_pci - driver_unbound_pci
        for pci_addr in unknown_pci:
            for d in dra_devices_by_pci[pci_addr]:
                profile = (d.get("attributes", {}).get("partitionProfile") or "").strip()
                if profile:
                    msg = (
                        f"Node {node_name}, PCI {pci_addr}: DRA device not found in "
                        f"hardware scan but advertises partitionProfile='{profile}'"
                    )
                    Logger.error(msg)
                    errors.append(msg)

    K8Helper.triage(
        environment,
        len(errors) == 0,
        f"partitionProfile driver-binding violations: {errors}" if errors else "",
    )

    Logger.info(
        "✓ All nodes: partitionProfile correctly reflects driver binding state"
    )
