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
Shared helper functions for collecting node GPU information.

This module is used by:
- conftest.py gather_device_info fixture (pytest tests)
- scripts/collect_node_gpu_data.py (standalone script)
- DRA driver tests (for comparison and validation)

By centralizing this logic, we ensure consistency between test fixture
data collection and standalone script data collection.
"""

import re
import logging
from typing import Dict, List, Tuple

from lib import common
import lib.k8_util as k8_util
import lib.amdgpu as amdgpu_util

Logger = logging.getLogger("lib.node_gpu_collector")


def collect_gpu_hardware_info_by_lspci(k8_cluster, node_name: str) -> Dict:
    """
    Collect GPU hardware information using lspci.

    This is the same logic used by gather_device_info fixture.

    Args:
        k8_cluster: K8 cluster object
        node_name: Name of the node

    Returns:
        Dict with GPU hardware information:
        {
            "gpus": [
                {
                    "pci_address": "83:00.0",
                    "device_id": "74a1",
                    "gpu_series": "MI300X",
                    "description": "...",
                    "full_line": "..."
                }
            ]
        }
    """
    hardware_info = {"gpus": []}

    # Run lspci to find AMD devices (vendor ID 1002)
    # This is exactly what gather_device_info does
    cmd = ["lspci", "-nn", "-d", "1002:"]
    result = k8_util.run_command_on_node(k8_cluster, node_name, cmd)

    # Handle case where run_command_on_node returns None (all retries failed)
    if result is None:
        Logger.error(
            f"run_command_on_node returned None for node {node_name} (all retries failed)"
        )
        return hardware_info

    ret_code, output = result

    if ret_code != 0:
        Logger.error(f"Failed to run lspci on node {node_name}: exit code {ret_code}")
        return hardware_info

    if not output:
        Logger.debug(f"Node {node_name} does not have any AMD GPU devices")
        return hardware_info

    # Parse lspci output
    # Pattern used by gather_device_info fixture
    pattern = r"(?:Processing accelerators|Display controller|VGA compatible controller).*1002:([0-9a-fA-F]{4})"

    # Enhanced pattern for full parsing
    full_pattern = r"([0-9a-fA-F:\.]+)\s+(.*?):\s+(.*)\[1002:([0-9a-fA-F]{4})\]"

    for line in output.split("\n"):
        if not line:
            continue

        # Try full pattern first
        match = re.search(full_pattern, line)
        if match:
            pci_addr = match.group(1)
            device_type = match.group(2).strip()
            description = match.group(3).strip()
            device_id = match.group(4)

            # Only include GPUs (same filter as gather_device_info)
            if (
                "Processing accelerators" in device_type
                or "Display controller" in device_type
                or "VGA compatible controller" in device_type
            ):
                # Get GPU series using same utility as gather_device_info
                gpu_series = amdgpu_util.get_amdgpu_device_series(device_id)

                hardware_info["gpus"].append(
                    {
                        "pci_address": pci_addr,
                        "device_id": device_id,
                        "device_type": device_type,
                        "description": description,
                        "gpu_series": gpu_series,
                        "full_line": line.strip(),
                    }
                )
        else:
            # Fallback to simple pattern (gather_device_info pattern)
            match = re.search(pattern, line)
            if match:
                device_id = match.group(1)
                gpu_series = amdgpu_util.get_amdgpu_device_series(device_id)

                hardware_info["gpus"].append(
                    {
                        "device_id": device_id,
                        "gpu_series": gpu_series,
                        "full_line": line.strip(),
                    }
                )

    Logger.info(f"Node {node_name}: Found {len(hardware_info['gpus'])} AMD GPU(s)")

    return hardware_info


def collect_gpu_series_by_lspci_ssh(node) -> str | None:
    """
    Detect AMD GPU series on a node accessible only via SSH (no K8s).

    Used for hypervisor nodes where K8s is not available. Runs lspci via
    node.run_command() and maps the first AMD GPU device ID to a series name
    using the same amdgpu_util.get_amdgpu_device_series() lookup as the K8s path.

    Returns the series name (e.g. 'MI300X') or None if not detectable.
    """
    rc, out, _ = node.run_command("lspci -nn -d 1002:")
    if rc != 0 or not out.strip():
        Logger.warning("lspci -nn -d 1002: failed or returned no AMD devices")
        return None

    full_pattern = r"([0-9a-fA-F:\.]+)\s+(.*?):\s+(.*)\[1002:([0-9a-fA-F]{4})\]"
    simple_pattern = (
        r"(?:Processing accelerators|Display controller|VGA compatible controller)"
        r".*1002:([0-9a-fA-F]{4})"
    )
    gpu_types = (
        "Processing accelerators",
        "Display controller",
        "VGA compatible controller",
    )

    for line in out.splitlines():
        if not line:
            continue
        match = re.search(full_pattern, line)
        if match:
            device_type = match.group(2).strip()
            device_id = match.group(4)
            if any(t in device_type for t in gpu_types):
                series = amdgpu_util.get_amdgpu_device_series(device_id)
                if series:
                    Logger.info(f"GPU series via SSH lspci: {series} (device_id={device_id})")
                    return series
        else:
            match = re.search(simple_pattern, line)
            if match:
                device_id = match.group(1)
                series = amdgpu_util.get_amdgpu_device_series(device_id)
                if series:
                    Logger.info(f"GPU series via SSH lspci: {series} (device_id={device_id})")
                    return series

    Logger.warning("No recognized AMD GPU device found via lspci on SSH node")
    return None


def collect_host_kernel_version(node) -> str:
    """Return the running kernel version (e.g. '6.8.0-136-generic') via SSH."""
    rc, out, _ = node.run_command("uname -r")
    if rc != 0 or not out.strip():
        Logger.warning("Could not determine host kernel version")
        return "unknown"
    return out.strip()


def collect_host_os_info_ssh(node) -> tuple:
    """Return (os_type, os_name, os_version) by parsing /etc/os-release via SSH."""
    rc, out, _ = node.run_command("cat /etc/os-release")
    if rc != 0 or not out.strip():
        Logger.warning(f"[{node.ip_address}] Could not read /etc/os-release")
        return ("linux", "unknown", "0.0")

    fields = {}
    for line in out.splitlines():
        if "=" in line:
            key, _, val = line.partition("=")
            fields[key.strip()] = val.strip().strip('"')

    os_id = fields.get("ID", "unknown")
    os_version = fields.get("VERSION_ID", "0.0")

    name_map = {
        "ubuntu": "Ubuntu",
        "debian": "Debian",
        "rhel": "RHEL",
        "centos": "CentOS",
        "rocky": "Rocky",
        "sles": "SLES",
        "opensuse-leap": "openSUSE",
    }
    os_name = name_map.get(os_id, os_id.capitalize() if os_id != "unknown" else "unknown")

    Logger.info(f"[{node.ip_address}] OS via SSH: {os_name} {os_version}")
    return ("linux", os_name, os_version)


def collect_hostname_ssh(node) -> str:
    """Return the hostname of the node via SSH; falls back to IP address."""
    rc, out, _ = node.run_command("hostname")
    if rc != 0 or not out.strip():
        Logger.warning(f"[{node.ip_address}] Could not determine hostname, using IP")
        return node.ip_address
    return out.strip()


def collect_gpu_count_and_device_id_ssh(node) -> tuple:
    """Count AMD GPUs and return the first device ID via SSH lspci.

    Returns (gpu_count, first_device_id) where first_device_id is None
    if no GPUs are detected.
    """
    rc, out, _ = node.run_command("lspci -nn -d 1002:")
    if rc != 0 or not out.strip():
        return (0, None)

    full_pattern = r"([0-9a-fA-F:\.]+)\s+(.*?):\s+(.*)\[1002:([0-9a-fA-F]{4})\]"
    simple_pattern = (
        r"(?:Processing accelerators|Display controller|VGA compatible controller)"
        r".*1002:([0-9a-fA-F]{4})"
    )
    gpu_types = (
        "Processing accelerators",
        "Display controller",
        "VGA compatible controller",
    )

    count = 0
    first_device_id = None

    for line in out.splitlines():
        if not line:
            continue
        match = re.search(full_pattern, line)
        if match:
            device_type = match.group(2).strip()
            device_id = match.group(4)
            if any(t in device_type for t in gpu_types):
                count += 1
                if first_device_id is None:
                    first_device_id = device_id
        else:
            match = re.search(simple_pattern, line)
            if match:
                count += 1
                if first_device_id is None:
                    first_device_id = match.group(1)

    Logger.info(f"[{node.ip_address}] GPU count via SSH lspci: {count} (device_id={first_device_id})")
    return (count, first_device_id)


def populate_node_info_ssh(node) -> None:
    """Populate a cluster_node's properties entirely via SSH.

    Discovers hostname, OS info, kernel version, GPU series, GPU count,
    and device ID using direct SSH commands. No K8s dependency.
    """
    node.host_name = collect_hostname_ssh(node)
    node.host_os_type, node.host_os_name, node.host_os_version = collect_host_os_info_ssh(node)
    node.kernel_version = collect_host_kernel_version(node)
    node.gpu_series = collect_gpu_series_by_lspci_ssh(node)
    gpu_count, device_id = collect_gpu_count_and_device_id_ssh(node)
    node.num_gpus = gpu_count
    node.device_id = device_id

    Logger.info(
        f"[{node.ip_address}] SSH discovery: host={node.host_name}, "
        f"os={node.host_os_name} {node.host_os_version}, kernel={node.kernel_version}, "
        f"gpu={node.gpu_series} x{node.num_gpus}"
    )


def collect_gpu_hardware_info_by_sysfs(k8_cluster, node_name: str) -> Dict:
    """
    Collect comprehensive GPU hardware information from sysfs.

    Reads from multiple sysfs sources and correlates them:
    1. /sys/module/amdgpu/drivers/pci:amdgpu/<pci_addr>/ - PCI device properties
    2. /sys/devices/platform/amdgpu_xcp_*/ - XCP partition platform devices
    3. /sys/class/kfd/kfd/topology/nodes/<nodeId>/ - KFD topology nodes
    4. /sys/class/drm/card*, /sys/class/drm/renderD* - DRM devices

    Optimized to use a single run_command_on_node call instead of multiple calls.

    Args:
        k8_cluster: K8 cluster object
        node_name: Name of the node

    Returns:
        Dict with GPU information organized by PCI address:
        {
            "gpus": {
                "0000:06:00.0": {
                    "pci_address": "0000:06:00.0",
                    "available_compute_partition": "SPX, DPX, QPX, CPX",
                    "current_compute_partition": "SPX",
                    "current_memory_partition": "NPS1",
                    "cardIndex": "0",
                    "renderIndex": "128",
                    "partitions": [  # XCP partitions (only if GPU is partitioned)
                        {
                            "xcp_index": 0,
                            "xcp_name": "amdgpu_xcp_0",
                            "kfd_node_id": 1,
                            "gpu_id": "12345",
                            "simd_count": "152",
                            "numa_node_id": "0",
                            ...
                        }
                    ],
                    ...
                }
            }
        }
    """
    Logger.info(f"Collecting comprehensive GPU sysfs information for node: {node_name}")
    sysfs_info = {"gpus": {}}

    # List of sysfs attributes to collect for each GPU
    attributes_to_read = [
        "available_compute_partition",
        "available_memory_partition",
        "current_compute_partition",
        "current_memory_partition",
        "device",
        "vendor",
        "product_name",
        "product_number",
        "serial_number",
        "vbios_version",
    ]

    Logger.debug(f"Attributes to collect per GPU: {', '.join(attributes_to_read)}")
    Logger.debug(
        "Also collecting: cardIndex, renderIndex, driverVersion, driverSrcVersion"
    )

    # Build a single shell script that collects all data in one execution
    # This reduces the number of pod creations from 1+(N*10) to just 1
    # Collects: PCI devices, XCP partitions, KFD nodes, DRM devices
    Logger.info("Building comprehensive sysfs collection script (single pod execution)")

    # Define tr filter outside f-string (f-strings cannot contain backslashes)
    null_filter = r"tr -d '\0'"

    script = f"""
    # Note: Not using 'set -e' to allow probe commands to fail gracefully
    # Collection should return partial/empty results rather than hard-failing on missing data
    base_dir="/sys/module/amdgpu/drivers/pci:amdgpu"

    if [ ! -d "$base_dir" ]; then
        echo "ERROR: Directory $base_dir not found"
        exit 1
    fi

    cd "$base_dir"

    # Get driver version info (global, same for all GPUs)
    driver_version=$(cat /sys/module/amdgpu/version 2>/dev/null || echo "")
    driver_src_version=$(cat /sys/module/amdgpu/srcversion 2>/dev/null || echo "")

    # Find all PCI addresses (format: 0000:06:00.0)
    for pci_addr in $(ls -1 | grep -E '^[0-9a-fA-F]{{4}}:[0-9a-fA-F]{{2}}:[0-9a-fA-F]{{2}}\\.[0-9]$'); do
        echo "GPU_START|$pci_addr"

        # Read each attribute from PCI device directory
        {' '.join([f'''
        if [ -f "$pci_addr/{attr}" ]; then
            value=$(cat "$pci_addr/{attr}" 2>/dev/null | {null_filter} || echo "")
            echo "ATTR|{attr}|$value"
        else
            echo "ATTR|{attr}|"
        fi''' for attr in attributes_to_read])}

        # Add driver version (same for all GPUs on this node)
        echo "ATTR|driverVersion|$driver_version"
        echo "ATTR|driverSrcVersion|$driver_src_version"

        # Find cardIndex by matching PCI address in /sys/class/drm/cardX/device symlink
        card_index=""
        for drm_card in /sys/class/drm/card*; do
            if [ -L "$drm_card/device" ]; then
                card_pci=$(readlink -f "$drm_card/device" 2>/dev/null || echo "")
                if [[ "$card_pci" == *"$pci_addr"* ]]; then
                    card_index=$(basename "$drm_card" | sed 's/card//')
                    break
                fi
            fi
        done
        echo "ATTR|cardIndex|$card_index"

        # Find renderIndex by matching PCI address in /sys/class/drm/renderDX/device symlink
        render_index=""
        for drm_render in /sys/class/drm/renderD*; do
            if [ -L "$drm_render/device" ]; then
                render_pci=$(readlink -f "$drm_render/device" 2>/dev/null || echo "")
                if [[ "$render_pci" == *"$pci_addr"* ]]; then
                    render_index=$(basename "$drm_render" | sed 's/renderD//')
                    break
                fi
            fi
        done
        echo "ATTR|renderIndex|$render_index"

        echo "GPU_END|$pci_addr"
    done

    # ===== Collect XCP partition information =====
    echo "XCP_COLLECTION_START"

    if [ -d "/sys/devices/platform" ]; then
        for xcp_dir in /sys/devices/platform/amdgpu_xcp_*; do
            if [ ! -d "$xcp_dir" ]; then
                continue
            fi

            xcp_name=$(basename "$xcp_dir")
            xcp_index=${{xcp_name#amdgpu_xcp_}}

            echo "XCP_START|$xcp_index|$xcp_name"

            # XCP devices don't have direct 'device' symlink to parent PCI
            # Instead, find the renderD and card devices under drm/
            # Then find parent GPU by checking for partitioned GPUs (compute_partition != SPX)

            # Get card index
            card_index=""
            for card_path in "$xcp_dir"/drm/card*; do
                [ ! -d "$card_path" ] && continue
                card_name=$(basename "$card_path")
                card_index=${{card_name#card}}
                echo "XCP_CARD_INDEX|$card_index"
                break
            done

            # Get renderD index
            render_index=""
            for render_path in "$xcp_dir"/drm/renderD*; do
                [ ! -d "$render_path" ] && continue
                render_name=$(basename "$render_path")
                render_index=${{render_name#renderD}}
                echo "XCP_RENDER_INDEX|$render_index"
                break
            done

            # Find parent PCI by following the device symlinks
            # XCP card devices are symlinked under the parent PCI device
            parent_found=false
            if [ -n "$card_index" ]; then
                # Follow the card symlink to find parent PCI
                card_dev_link="/sys/class/drm/card${{card_index}}/device"
                if [ -L "$card_dev_link" ]; then
                    card_dev_target=$(readlink -f "$card_dev_link")

                    # For XCP devices, the target is under amdgpu_xcp_*, traverse up to find PCI parent
                    # XCP structure: /sys/devices/platform/amdgpu_xcp_*/drm/cardX/device -> ../../
                    # Real PCI GPU cardY/device -> /sys/devices/pci*/0000:XX:YY.Z/

                    # Walk up directory tree to find PCI address
                    current_path="$card_dev_target"
                    while [ "$current_path" != "/" ] && [ "$current_path" != "/sys" ]; do
                        dir_name=$(basename "$current_path")
                        if [[ "$dir_name" =~ ^[0-9a-fA-F]{{4}}:[0-9a-fA-F]{{2}}:[0-9a-fA-F]{{2}}\\.[0-9]$ ]]; then
                            echo "XCP_PARENT_PCI|$dir_name"
                            parent_found=true
                            break
                        fi
                        current_path=$(dirname "$current_path")
                    done
                fi
            fi

            echo "XCP_END|$xcp_index"
        done
    fi

    echo "XCP_COLLECTION_END"

    # ===== Collect KFD topology node information =====
    echo "KFD_COLLECTION_START"

    if [ -d "/sys/class/kfd/kfd/topology/nodes" ]; then
        for node_dir in /sys/class/kfd/kfd/topology/nodes/*; do
            if [ ! -d "$node_dir" ]; then
                continue
            fi

            node_id=$(basename "$node_dir")

            # Skip CPU nodes (node 0 is typically CPU)
            if [ "$node_id" = "0" ]; then
                continue
            fi

            echo "KFD_NODE_START|$node_id"

            # Read properties file (contains PCI info, gpu_id, simd_count, etc.)
            if [ -f "$node_dir/properties" ]; then
                # Extract key properties
                while IFS= read -r line; do
                    # Parse lines like "cpu_cores_count 0" or "simd_count 304"
                    key=$(echo "$line" | awk '{{print $1}}')
                    value=$(echo "$line" | awk '{{print $2}}')

                    # Only capture important properties
                    case "$key" in
                        domain|bus|device|function|location_id|gpu_id|simd_count|\\
                        max_waves_per_simd|array_count|num_cp_queues|\\
                        mem_banks_count|caches_count|numa_node_id|drm_render_minor)
                            echo "KFD_PROP|$key|$value"
                            ;;
                    esac
                done < "$node_dir/properties"

                # Reconstruct PCI address from location_id
                # For MI300X partitions, location_id encodes the PCI BDF:
                # bits 15:8 = bus, bits 7:3 = device, bits 2:0 = function
                location_id=$(grep "^location_id" "$node_dir/properties" 2>/dev/null | awk '{{print $2}}' || echo "")

                if [ -n "$location_id" ] && [ "$location_id" != "0" ]; then
                    # Decode PCI BDF fields from location_id
                    bus=$((location_id >> 8))
                    device=$(((location_id >> 3) & 0x1F))
                    func=$((location_id & 0x7))

                    # Format as PCI address
                    pci_addr=$(printf "0000:%02x:%02x.%x" $bus $device $func 2>/dev/null || echo "")
                    if [ -n "$pci_addr" ]; then
                        echo "KFD_PCI_ADDR|$pci_addr"
                    fi
                fi
            fi

            # Read name file (GPU product name)
            if [ -f "$node_dir/name" ]; then
                gpu_name=$(cat "$node_dir/name" 2>/dev/null || echo "")
                echo "KFD_NAME|$gpu_name"
            fi

            echo "KFD_NODE_END|$node_id"
        done
    fi

    echo "KFD_COLLECTION_END"
    """

    # Execute the script once
    Logger.info(f"Executing sysfs collection on node {node_name}")
    cmd = ["bash", "-c", script]
    result = k8_util.run_command_on_node(k8_cluster, node_name, cmd)

    if result is None:
        Logger.error(
            f"run_command_on_node returned None for node {node_name} (all retries failed)"
        )
        return sysfs_info

    ret_code, output = result

    if ret_code != 0:
        # Check if this is because driver is not loaded
        if output and "ERROR: Directory /sys/module/amdgpu" in output:
            Logger.warning(
                f"Node {node_name}: amdgpu driver not loaded - skipping sysfs collection"
            )
        else:
            Logger.error(
                f"Failed to collect sysfs data on node {node_name}: exit code {ret_code}"
            )
            Logger.debug(f"Output: {output}")
        return sysfs_info

    if not output:
        Logger.info(
            f"Node {node_name} does not have any AMD GPU devices in amdgpu driver"
        )
        return sysfs_info

    # Parse the output and build comprehensive mapping
    Logger.info(f"Parsing comprehensive sysfs data from node {node_name}")

    current_gpu = None
    xcp_partitions = {}  # Map: xcp_index -> {xcp_name, parent_pci, ...}
    kfd_nodes = {}       # Map: kfd_node_id -> {gpu_id, pci_addr, properties, ...}

    in_xcp_section = False
    in_kfd_section = False
    current_xcp = None
    current_kfd_node = None

    for line in output.strip().split("\n"):
        line = line.strip()
        if not line:
            continue

        # ===== Parse PCI GPU data =====
        if line.startswith("GPU_START|"):
            pci_addr = line.split("|", 1)[1]
            current_gpu = {
                "pci_address": pci_addr,
                "partitions": []  # Will be populated from XCP/KFD correlation
            }
            Logger.debug(f"Processing GPU: {pci_addr}")

        elif line.startswith("ATTR|") and current_gpu is not None:
            parts = line.split("|", 2)
            if len(parts) == 3:
                attr_name = parts[1]
                attr_value = parts[2].strip() if parts[2] else ""
                current_gpu[attr_name] = attr_value

        elif line.startswith("GPU_END|") and current_gpu is not None:
            pci_addr = current_gpu["pci_address"]
            sysfs_info["gpus"][pci_addr] = current_gpu
            current_gpu = None

        # ===== Parse XCP partition data =====
        elif line.startswith("XCP_COLLECTION_START"):
            in_xcp_section = True
            Logger.debug("Parsing XCP partition data...")

        elif line.startswith("XCP_COLLECTION_END"):
            in_xcp_section = False
            Logger.debug(f"Parsed {len(xcp_partitions)} XCP partition(s)")

        elif line.startswith("XCP_START|") and in_xcp_section:
            parts = line.split("|")
            xcp_index = int(parts[1])
            xcp_name = parts[2]
            current_xcp = {
                "xcp_index": xcp_index,
                "xcp_name": xcp_name,
                "parent_pci": "",
                "card_index": "",
                "render_index": ""
            }
            Logger.debug(f"  Processing XCP partition: {xcp_name} (index {xcp_index})")

        elif line.startswith("XCP_PARENT_PCI|") and current_xcp is not None:
            parent_pci = line.split("|", 1)[1]
            current_xcp["parent_pci"] = parent_pci

        elif line.startswith("XCP_CARD_INDEX|") and current_xcp is not None:
            card_index = line.split("|", 1)[1]
            current_xcp["card_index"] = card_index

        elif line.startswith("XCP_RENDER_INDEX|") and current_xcp is not None:
            render_index = line.split("|", 1)[1]
            current_xcp["render_index"] = render_index

        elif line.startswith("XCP_END|") and current_xcp is not None:
            xcp_index = current_xcp["xcp_index"]
            xcp_partitions[xcp_index] = current_xcp
            current_xcp = None

        # ===== Parse KFD topology node data =====
        elif line.startswith("KFD_COLLECTION_START"):
            in_kfd_section = True
            Logger.debug("Parsing KFD topology node data...")

        elif line.startswith("KFD_COLLECTION_END"):
            in_kfd_section = False
            Logger.debug(f"Parsed {len(kfd_nodes)} KFD node(s)")

        elif line.startswith("KFD_NODE_START|") and in_kfd_section:
            node_id = line.split("|", 1)[1]
            current_kfd_node = {
                "kfd_node_id": node_id,
                "properties": {},
                "pci_addr": "",
                "name": ""
            }
            Logger.debug(f"  Processing KFD node: {node_id}")

        elif line.startswith("KFD_PROP|") and current_kfd_node is not None:
            parts = line.split("|", 2)
            if len(parts) == 3:
                prop_key = parts[1]
                prop_value = parts[2]
                current_kfd_node["properties"][prop_key] = prop_value

        elif line.startswith("KFD_PCI_ADDR|") and current_kfd_node is not None:
            pci_addr = line.split("|", 1)[1]
            current_kfd_node["pci_addr"] = pci_addr

        elif line.startswith("KFD_NAME|") and current_kfd_node is not None:
            gpu_name = line.split("|", 1)[1]
            current_kfd_node["name"] = gpu_name

        elif line.startswith("KFD_NODE_END|") and current_kfd_node is not None:
            node_id = current_kfd_node["kfd_node_id"]
            kfd_nodes[node_id] = current_kfd_node
            current_kfd_node = None

        # ===== Error handling =====
        elif line.startswith("ERROR:"):
            Logger.warning(f"Node {node_name}: {line}")

    # ===== Correlate XCP partitions and KFD nodes with PCI GPUs =====
    Logger.info(f"Correlating XCP partitions and KFD nodes with PCI devices...")

    # Build mapping: renderD index -> XCP partition
    # Normalize to string for consistent comparison
    xcp_by_render = {}
    for xcp_idx, xcp_data in xcp_partitions.items():
        render_idx = xcp_data.get("render_index")
        if render_idx:
            render_idx_str = str(render_idx)
            xcp_by_render[render_idx_str] = xcp_data
            Logger.debug(f"  XCP {xcp_idx}: render index {render_idx_str}")

    # Build mapping: renderD index -> KFD node
    # KFD nodes have drm_render_minor property
    kfd_by_render = {}
    for node_id, kfd_data in kfd_nodes.items():
        render_minor = kfd_data.get("properties", {}).get("drm_render_minor")
        if render_minor:
            # renderD index = render_minor (e.g., renderD129 -> 129)
            # Normalize to string for consistent comparison
            render_minor_str = str(render_minor)
            kfd_by_render[render_minor_str] = kfd_data
            Logger.debug(f"  KFD node {node_id}: render index {render_minor_str}")

    Logger.debug(f"XCP render indices: {sorted(xcp_by_render.keys())}")
    Logger.debug(f"KFD render indices: {sorted(kfd_by_render.keys())}")

    # Build mapping: parent PCI -> list of render indices
    xcp_renders_by_pci = {}
    for xcp_idx, xcp_data in xcp_partitions.items():
        parent_pci = xcp_data.get("parent_pci")
        render_idx = xcp_data.get("render_index")
        if parent_pci and render_idx:
            if parent_pci not in xcp_renders_by_pci:
                xcp_renders_by_pci[parent_pci] = []
            xcp_renders_by_pci[parent_pci].append(str(render_idx))

    # Merge partition data into GPU info
    Logger.debug(f"Node {node_name} raw sys_info data before processing: {sysfs_info}")
    for pci_addr, gpu_data in sysfs_info["gpus"].items():
        partitions = []

        # Check if GPU is partitioned
        compute_part = gpu_data.get("current_compute_partition", "").strip()
        memory_part = gpu_data.get("current_memory_partition", "").strip()
        is_partitioned = compute_part and memory_part and not (compute_part == "SPX" and memory_part == "NPS1")

        # Get the full GPU's renderD index (if partitioned, this becomes the 8th partition)
        full_gpu_render_idx = gpu_data.get("renderIndex")

        # Get all renderD indices for XCP partitions
        render_indices = xcp_renders_by_pci.get(pci_addr, [])

        # If GPU is partitioned and has a renderD, include it as a partition device
        # The DRA driver treats the full GPU renderD as an additional partition in CPX mode
        if is_partitioned and full_gpu_render_idx:
            all_render_indices = sorted(set(render_indices + [str(full_gpu_render_idx)]))
        else:
            all_render_indices = sorted(render_indices)

        # Correlate XCP with KFD using renderD index as the key
        for render_idx in all_render_indices:
            partition_info = {}

            # Get XCP data for this renderD (may not exist for full GPU renderD)
            if render_idx in xcp_by_render:
                partition_info.update(xcp_by_render[render_idx])
            else:
                # This is likely the full GPU renderD (no XCP device)
                # Create a pseudo-partition entry
                partition_info["xcp_index"] = None
                partition_info["xcp_name"] = f"full-gpu-renderD{render_idx}"
                partition_info["parent_pci"] = pci_addr
                partition_info["card_index"] = gpu_data.get("cardIndex")
                partition_info["render_index"] = render_idx

            # Get KFD data for this renderD
            if render_idx in kfd_by_render:
                kfd_data = kfd_by_render[render_idx]
                partition_info["kfd_node_id"] = kfd_data.get("kfd_node_id")
                partition_info["kfd_name"] = kfd_data.get("name")
                # Flatten KFD properties into partition_info
                for prop_key, prop_value in kfd_data.get("properties", {}).items():
                    partition_info[f"kfd_{prop_key}"] = prop_value

            if partition_info:
                partitions.append(partition_info)

        # Update GPU with partition data
        if partitions:
            gpu_data["partitions"] = partitions
            Logger.debug(f"  GPU {pci_addr}: Found {len(partitions)} partition(s)")
        else:
            gpu_data["partitions"] = []
            Logger.debug(f"  GPU {pci_addr}: No partitions (full GPU)")

    Logger.info(
        f"Node {node_name}: Collected comprehensive info for {len(sysfs_info['gpus'])} GPU(s), "
        f"{len(xcp_partitions)} XCP partition(s), {len(kfd_nodes)} KFD node(s)"
    )
    Logger.debug(f"Node {node_name} sys-info data: {sysfs_info}")

    return sysfs_info


def collect_gpu_hardware_info(k8_cluster, node_name: str) -> Dict:
    """
    Collect comprehensive GPU hardware information from both lspci and sysfs.

    Combines data from:
    1. lspci - PCI device detection, device IDs, GPU series
    2. sysfs - Partition configuration, product info, versions, XCP devices, KFD nodes

    This is the main method that should be used by tests. It provides complete
    GPU hardware information by merging both data sources based on PCI address.

    Args:
        k8_cluster: K8 cluster object
        node_name: Name of the node

    Returns:
        Dict with merged hardware information:
        {
            "gpus": [  # merged list of GPUs
                {
                    "pci_address": "06:00.0",  # short format from lspci
                    "pci_address_full": "0000:06:00.0",  # long format from sysfs
                    "device_id": "74a1",  # from lspci
                    "gpu_series": "MI300X",  # from lspci
                    "current_compute_partition": "CPX",  # from sysfs
                    "current_memory_partition": "NPS4",  # from sysfs
                    "cardIndex": "6",  # from sysfs
                    "renderIndex": "128",  # from sysfs
                    "partitions": [  # XCP + KFD correlation
                        {
                            "xcp_index": 0,
                            "xcp_name": "amdgpu_xcp_0",
                            "parent_pci": "0000:06:00.0",
                            "kfd_node_id": "1",
                            "kfd_gpu_id": "12345",
                            "kfd_simd_count": "152",
                            ...
                        }
                    ],
                    ...
                }
            ]
        }

        Use len(result["gpus"]) to get GPU count.
        Use [gpu["device_id"] for gpu in result["gpus"]] to get device IDs.
        Use gpu["partitions"] to access partition-specific data.
    """
    # Collect from lspci
    lspci_info = collect_gpu_hardware_info_by_lspci(k8_cluster, node_name)

    # Collect from sysfs (includes XCP + KFD correlation)
    sysfs_info = collect_gpu_hardware_info_by_sysfs(k8_cluster, node_name)

    # Validate partition correlations
    validate_partition_correlation(sysfs_info, node_name)

    # Merge data based on PCI address
    merged_gpus = []

    for lspci_gpu in lspci_info.get("gpus", []):
        # Get short PCI address from lspci (e.g., "06:00.0")
        short_pci = lspci_gpu.get("pci_address", "")

        # Convert short format to long format for sysfs lookup
        # Short: "06:00.0" -> Long: "0000:06:00.0"
        # Short: "0000:06:00.0" -> Long: "0000:06:00.0" (already long)
        if short_pci:
            if short_pci.count(":") == 1:
                # Short format, add domain prefix
                long_pci = f"0000:{short_pci}"
            else:
                # Already long format
                long_pci = short_pci
        else:
            long_pci = None

        # Start with lspci data
        merged_gpu = lspci_gpu.copy()

        # Merge sysfs data if available
        if long_pci and long_pci in sysfs_info.get("gpus", {}):
            sysfs_gpu = sysfs_info["gpus"][long_pci]
            merged_gpu["pci_address_full"] = long_pci

            # Add all sysfs attributes except pci_address (keep short format from lspci)
            for key, value in sysfs_gpu.items():
                if key != "pci_address":
                    merged_gpu[key] = value

            partition_count = len(sysfs_gpu.get("partitions", []))
            Logger.debug(
                f"Merged data for PCI {short_pci}: lspci + sysfs ({len(sysfs_gpu)} sysfs attrs, {partition_count} partitions)"
            )
        else:
            Logger.debug(
                f"No sysfs data found for PCI address {short_pci} (tried {long_pci})"
            )
            # Ensure partitions key exists even if no sysfs data
            merged_gpu["partitions"] = []

        merged_gpus.append(merged_gpu)

    # Return merged results
    combined_info = {
        "gpus": merged_gpus,
    }

    total_partitions = sum(len(gpu.get("partitions", [])) for gpu in merged_gpus)
    Logger.info(
        f"Node {node_name}: Combined hardware info - {len(combined_info['gpus'])} GPU(s), "
        f"{total_partitions} partition(s) with full XCP+KFD correlation"
    )

    return combined_info


def get_partition_count_from_profile(profile: str) -> int:
    """
    Get theoretical maximum partition count from partition profile string.

    NOTE: This is a theoretical calculation only! The actual partition count
    depends on hardware/firmware configuration and may be less than this value.
    The DRA driver discovers partitions dynamically by enumerating XCP devices
    and validating them against KFD topology, not by parsing profile strings.

    For AMD MI300X, the theoretical partition count is multiplicative:
    Each partition is a combination of compute slice and memory (NUMA) node.

    Theoretical examples:
        SPX_NPS1 = 1 × 1 = 1 partition  (full GPU)
        DPX_NPS2 = 2 × 2 = 4 partitions
        QPX_NPS4 = 4 × 4 = 16 partitions
        CPX_NPS4 = 2 × 4 = 8 partitions (theoretical max)

    Actual hardware examples:
        CPX_NPS4 on asrock-126-b3-1b = 7 partitions (hardware-dependent)

    Args:
        profile: Partition profile like "SPX_NPS1", "CPX_NPS4", "DPX_NPS2"

    Returns:
        Theoretical maximum number of partitions (1, 2, 4, 8, 16, etc.)
        Actual count may be lower - use XCP device enumeration for truth.
    """
    if not profile or "_" not in profile:
        return 1

    compute_part, memory_part = profile.upper().split("_", 1)

    # Compute partition multiplier
    compute_multiplier = {
        "SPX": 1,  # Single partition eXtreme (1 compute partition)
        "DPX": 2,  # Dual Partition eXtreme (2 compute partitions)
        "TPX": 3,  # Triple Partition eXtreme (3 compute partitions, rare)
        "QPX": 4,  # Quad Partition eXtreme (4 compute partitions)
        "CPX": 2,  # Compute Partition eXtreme (2 compute partitions for MI300X)
    }.get(compute_part, 1)

    # Memory partition multiplier (NUMA Per Socket)
    memory_multiplier = {
        "NPS1": 1,  # 1 NUMA node
        "NPS2": 2,  # 2 NUMA nodes
        "NPS4": 4,  # 4 NUMA nodes
        "NPS8": 8,  # 8 NUMA nodes
    }.get(memory_part, 1)

    # Theoretical total = compute × memory
    # Actual count discovered via /sys/devices/platform/amdgpu_xcp_* enumeration
    return compute_multiplier * memory_multiplier


def validate_partition_correlation(hw_info: Dict, node_name: str) -> bool:
    """
    Validate that partition data is properly correlated across sysfs sources.

    Checks:
    1. Number of KFD nodes >= XCP devices (KFD includes full GPU representation)
    2. All partitions have parent PCI reference
    3. All partitions have KFD node correlation

    Note: The actual partition count may differ from the theoretical maximum
    based on profile (e.g., CPX_NPS4 may create 7 partitions instead of 8
    due to hardware/firmware configuration).

    Args:
        hw_info: Hardware info dict from collect_gpu_hardware_info()
        node_name: Name of the node (for logging)

    Returns:
        True if validation passes, False otherwise
    """
    all_valid = True

    for pci_addr, gpu_data in hw_info.get("gpus", {}).items():
        compute_part = gpu_data.get("current_compute_partition", "").strip()
        memory_part = gpu_data.get("current_memory_partition", "").strip()

        # Skip if not partitioned
        if not compute_part or not memory_part:
            if gpu_data.get("partitions"):
                Logger.warning(
                    f"Node {node_name}, GPU {pci_addr}: No partition profile but found {len(gpu_data['partitions'])} partition(s)"
                )
                all_valid = False
            continue

        # Log partition count (informational only - don't fail on mismatch)
        profile = f"{compute_part}_{memory_part}"
        expected_count = get_partition_count_from_profile(profile)
        actual_count = len(gpu_data.get("partitions", []))

        if expected_count != actual_count:
            Logger.info(
                f"Node {node_name}, GPU {pci_addr}: Profile {profile} theoretical max: {expected_count} partition(s), "
                f"actual: {actual_count} partition(s) (hardware-dependent)"
            )
        else:
            Logger.debug(
                f"Node {node_name}, GPU {pci_addr}: ✓ {actual_count} partition(s) match profile {profile}"
            )

        # Validate each partition has required fields
        for i, part in enumerate(gpu_data.get("partitions", [])):
            if not part.get("parent_pci"):
                Logger.warning(
                    f"Node {node_name}, GPU {pci_addr}, partition {i}: Missing parent_pci"
                )
                all_valid = False

            if not part.get("kfd_node_id"):
                Logger.warning(
                    f"Node {node_name}, GPU {pci_addr}, partition {i}: Missing kfd_node_id"
                )
                all_valid = False

    return all_valid


def populate_cluster_node_with_gpu_info(k8_cluster, cluster_node, node_name: str):
    """
    Populate a cluster_node object with GPU information.

    This replicates what gather_device_info fixture does.
    Used by the fixture and can be used by other code.

    Args:
        k8_cluster: K8 cluster object
        cluster_node: cluster_node object to populate
        node_name: Name of the node
    """
    # Collect hardware info
    hw_info = collect_gpu_hardware_info(k8_cluster, node_name)

    # Populate cluster_node (same as gather_device_info does)
    if len(hw_info["gpus"]) > 0:
        # Use first GPU's info (same as gather_device_info)
        first_gpu = hw_info["gpus"][0]
        cluster_node.device_id = first_gpu["device_id"]
        cluster_node.gpu_series = first_gpu["gpu_series"]

        # For multi-die GPUs (e.g. MI300X), lspci shows 1 PCI device but
        # the device-plugin exposes one resource per KFD partition (die).
        # Use partition count when available; fall back to lspci GPU count.
        total_partitions = sum(len(gpu.get("partitions", [])) for gpu in hw_info["gpus"])
        if total_partitions > len(hw_info["gpus"]):
            cluster_node.num_gpus = total_partitions
        else:
            cluster_node.num_gpus = len(hw_info["gpus"])
    else:
        cluster_node.device_id = None
        cluster_node.gpu_series = None
        cluster_node.num_gpus = 0


def populate_all_cluster_nodes_with_gpu_info(k8_cluster):
    """
    Populate all cluster nodes with GPU and OS information.

    This encapsulates the entire collection loop from gather_device_info fixture.
    Can be used by both pytest fixtures and standalone scripts.

    Args:
        k8_cluster: K8 cluster object with nodes

    Returns:
        Tuple of (success: bool, error_message: str)
    """
    # Get all Kubernetes nodes
    ret_code, k8_nodes = k8_util.k8_get_nodes()
    if ret_code != 0:
        return False, "Unable to collect node information from k8-cluster"

    if len(k8_nodes) == 0:
        return False, "No nodes found in k8-cluster"

    is_openshift = k8_cluster.testbed_type == common.TestbedType.OPENSHIFT
    ocp_version = k8_util.k8_get_ocp_version() if is_openshift else "NA"

    # Populate each cluster node
    for node in k8_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        node_ip = k8_util.k8_get_node_address(node)
        os_type, os_name, os_version = k8_util.k8_get_node_os_info(node)
        kernel_version = node['status']['node_info'].get('kernel_version', 'NA')

        # Find corresponding cluster_node object
        cluster_node = k8_cluster.find_node_by_ip(node_ip)

        # Populate OS information
        cluster_node.host_name = node_name
        cluster_node.host_os_type = os_type
        cluster_node.host_os_name = os_name
        cluster_node.host_os_version = os_version
        cluster_node.kernel_version = kernel_version
        cluster_node.ocp_version = ocp_version

        # Populate GPU information using shared helper
        populate_cluster_node_with_gpu_info(k8_cluster, cluster_node, node_name)

    Logger.info(f"Populated {len(k8_nodes)} cluster node(s) with GPU information")

    # Topology-aware GPU detection validation.
    #
    # Three supported topologies:
    #   1. Single-node (OpenShift/K8s): control-plane node also runs GPU workloads.
    #   2. Multi-node, all GPU nodes: all workers have GPUs.
    #   3. Multi-node, VM master + GPU workers: master has no GPU (expected);
    #      worker nodes must have GPUs.
    #
    # In topology 1, there are no worker nodes so we validate the master itself.
    # In topologies 2/3, we validate the worker nodes.
    worker_nodes = [n for n in k8_cluster.cluster_nodes if n.node_type == "worker"]
    expected_gpu_nodes = worker_nodes if worker_nodes else list(k8_cluster.cluster_nodes)

    # Warn about partial detection: series visible but GPU count came back zero.
    # This usually means lspci/KFD found the device but the driver or KFD is not
    # fully initialised — the node will appear broken to every test that uses it.
    for node in expected_gpu_nodes:
        if node.gpu_series and node.num_gpus == 0:
            Logger.warning(
                f"Node {node.host_name} ({node.ip_address}): gpu_series={node.gpu_series} "
                f"detected but num_gpus=0 — check driver installation and KFD accessibility"
            )

    # Hard failure: none of the expected GPU nodes has a usable GPU.
    # Every test that calls gpu_cluster.get_gpu_variants() or
    # find_node_by_gpu_series() would fail with confusing errors; abort now.
    gpu_nodes_found = [n for n in expected_gpu_nodes if n.gpu_series and n.num_gpus > 0]
    if not gpu_nodes_found:
        node_summary = ", ".join(
            f"{n.host_name}(series={n.gpu_series or 'none'}, gpus={n.num_gpus})"
            for n in expected_gpu_nodes
        )
        return False, (
            f"GPU detection failed on all expected GPU nodes — no usable GPUs found. "
            f"Check driver installation and KFD accessibility. "
            f"Nodes checked: [{node_summary}]"
        )

    Logger.info(
        f"GPU detection validated: {len(gpu_nodes_found)}/{len(expected_gpu_nodes)} "
        f"expected GPU node(s) with usable GPUs"
    )

    return True, ""
