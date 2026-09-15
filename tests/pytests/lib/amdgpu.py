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

import os
import sys
import pdb
import logging
import json
import lib.common
from datetime import datetime
from collections import defaultdict

Logger = logging.getLogger("lib.amdgpu")

def get_matching_driver_version(rocm_version):
    # Use absolute path relative to this module
    module_dir = os.path.dirname(os.path.abspath(__file__))
    json_file = os.path.join(module_dir, "files", "gpu-operator-rocm-info.json")

    with open(json_file, "r") as fp:
        rocm_info = json.load(fp)

    for entry in rocm_info['rocm-driver-matrix']:
        if entry['rocm-version'] == rocm_version:
            return entry['amdgpu-driver-version']
    return None

def get_amdgpu_device_series(device_id) -> str:
    """
    Lookup device-id in the amdgpu-features.json to retrieve GPU Series Name
    """
    # Use absolute path relative to this module
    module_dir = os.path.dirname(os.path.abspath(__file__))
    json_file = os.path.join(module_dir, "files", "amdgpu-features.json")

    with open(json_file, "r") as fp:
        amdgpu_feature_data = json.load(fp)

    dev_id_str = str(device_id).strip()
    if not dev_id_str.lower().startswith("0x"):
        dev_id_str = f"0x{dev_id_str}"

    for entry in amdgpu_feature_data['amd-gpu-devs']:
        if dev_id_str in entry.get("device-id", []):
            return entry.get("series", "UNKNOWN")
    return "UNKNOWN"

def get_supported_operands(gpu_op_version: str):
    """
    Look up gpu_op_version in the lib/files/gpu-operator-operands-support.json
    and retrieve supported operands
    """

    # Use absolute path relative to this module
    module_dir = os.path.dirname(os.path.abspath(__file__))
    json_file = os.path.join(module_dir, "files", "gpu-operator-operands-support.json")

    try:
        with open(json_file, "r") as fp:
            gpu_op_release_info = json.load(fp)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return None

    match = next((item for item in gpu_op_release_info.get("release-matrix", []) if item["gpu-operator"] == gpu_op_version), None)
    return match

def get_gpu_features(device_id) -> dict:
    """
    Lookup device-id in the amdgpu-features.json to retrieve GPU features/capabilities
    Returns dict with features like gpu_partitioning, config_manager, etc.
    """
    # Use absolute path relative to this module
    module_dir = os.path.dirname(os.path.abspath(__file__))
    json_file = os.path.join(module_dir, "files", "amdgpu-features.json")

    with open(json_file, "r") as fp:
        amdgpu_feature_data = json.load(fp)

    dev_id_str = str(device_id).strip()
    if not dev_id_str.lower().startswith("0x"):
        dev_id_str = f"0x{dev_id_str}"

    for entry in amdgpu_feature_data['amd-gpu-devs']:
        if dev_id_str in entry.get("device-id", []):
            return entry.get("features", {})
    return {}

def get_driver_constraints(device_id, mode: str = "inbox") -> dict:
    """
    Returns driver constraints for a GPU and install mode.
    mode: "inbox" or "deviceconfig"
    Result: {'min_version': str | None, 'max_version': str | None}
    """
    features = get_gpu_features(device_id)
    driver = features.get("driver", {})
    mode_block = driver.get(mode, {})
    return {
        "min_version": mode_block.get("min_version"),
        "max_version": mode_block.get("max_version"),
    }

def supports_config_manager(device_id) -> bool:
    """
    Check if a GPU device supports config manager (GPU partitioning)
    Config manager is only applicable to GPUs with partitioning support.
    """
    features = get_gpu_features(device_id)
    return features.get("gpu_partitioning", False)

def get_test_runner_support(device_id) -> dict:
    features = get_gpu_features(device_id)
    return features.get("test_runner", {"rvs": False, "agfhc": False})

def get_gpu_architecture(device_id) -> str:
    """
    Get GPU architecture (CDNA2, CDNA3, CDNA4, RDNA4, etc.)
    """
    module_dir = os.path.dirname(os.path.abspath(__file__))
    json_file = os.path.join(module_dir, "files", "amdgpu-features.json")

    with open(json_file, "r") as fp:
        amdgpu_feature_data = json.load(fp)

    dev_id_str = str(device_id).strip()
    if not dev_id_str.lower().startswith("0x"):
        dev_id_str = f"0x{dev_id_str}"

    for entry in amdgpu_feature_data['amd-gpu-devs']:
        if dev_id_str in entry.get("device-id", []):
            return entry.get("architecture", "UNKNOWN")
    return "UNKNOWN"

def get_gpu_features_by_series(gpu_series: str) -> dict:
    """Lookup GPU features by series name (e.g. 'MI350P') from amdgpu-features.json."""
    module_dir = os.path.dirname(os.path.abspath(__file__))
    json_file = os.path.join(module_dir, "files", "amdgpu-features.json")
    with open(json_file, "r") as fp:
        data = json.load(fp)
    for entry in data['amd-gpu-devs']:
        if entry.get("series") == gpu_series:
            return entry.get("features", {})
    return {}

def generate_partitioning_check(gpu_series: str, num_gpus: int) -> dict:
    """Generate a partitioning_check config dict for any GPU series and GPU count.

    Reads supported partition types and NPS modes from amdgpu-features.json and
    produces homogeneous, heterogeneous, and negative-test profiles scaled to num_gpus.
    """
    features = get_gpu_features_by_series(gpu_series)
    compute_partitions = features.get("partition_profiles", ["SPX", "DPX", "QPX", "CPX"])
    nps_modes = features.get("nps_modes", ["NPS1"])

    profiles = {}

    # Standard homogeneous profiles: one partition type, all GPUs assigned.
    # NPS4 is skipped here — it is only valid as a negative test (invalid combo).
    for cp in compute_partitions:
        for nps in nps_modes:
            if nps == "NPS4":
                continue
            profile_key = f"{cp}_{nps}"
            profiles[profile_key] = {
                "skippedGPUs": {"ids": []},
                "profiles": [{"computePartition": cp, "memoryPartition": nps, "numGPUsAssigned": num_gpus}]
            }

    # Named aliases used by specific tests
    profiles["homogenous"] = {
        "profiles": [{"computePartition": "SPX", "memoryPartition": "NPS1", "numGPUsAssigned": num_gpus}]
    }
    if "NPS2" in nps_modes and num_gpus > 1:
        profiles["nps2"] = {
            "profiles": [{"computePartition": "DPX", "memoryPartition": "NPS2", "numGPUsAssigned": num_gpus}]
        }

    # Heterogeneous profiles: skip last GPU, distribute remainder across partition types
    if num_gpus > 1:
        skip_id = num_gpus - 1
        active = num_gpus - 1
        if active >= 3 and "QPX" in compute_partitions:
            g1 = max(1, active // 3)
            g2 = max(1, active // 3)
            g3 = active - g1 - g2
            profiles["heterogenous"] = {
                "skippedGPUs": {"ids": [skip_id]},
                "profiles": [
                    {"computePartition": "DPX", "memoryPartition": "NPS1", "numGPUsAssigned": g1},
                    {"computePartition": "CPX", "memoryPartition": "NPS1", "numGPUsAssigned": g2},
                    {"computePartition": "QPX", "memoryPartition": "NPS1", "numGPUsAssigned": g3},
                ]
            }
        else:
            g1 = max(1, active // 2)
            g2 = active - g1
            het_profiles = [{"computePartition": "DPX", "memoryPartition": "NPS1", "numGPUsAssigned": g1}]
            if g2 > 0:
                het_profiles.append({"computePartition": "CPX", "memoryPartition": "NPS1", "numGPUsAssigned": g2})
            profiles["heterogenous"] = {
                "skippedGPUs": {"ids": [skip_id]},
                "profiles": het_profiles,
            }
        h2_active = num_gpus - 1
        h2_g1 = max(1, h2_active // 2)
        h2_g2 = h2_active - h2_g1
        het2_profiles = [{"computePartition": "SPX", "memoryPartition": "NPS1", "numGPUsAssigned": h2_g1}]
        if h2_g2 > 0:
            het2_profiles.append({"computePartition": "CPX", "memoryPartition": "NPS1", "numGPUsAssigned": h2_g2})
        profiles["heterogenous2"] = {
            "skippedGPUs": {"ids": [skip_id]},
            "profiles": het2_profiles,
        }
    else:
        profiles["heterogenous"] = {
            "skippedGPUs": {"ids": []},
            "profiles": [{"computePartition": "DPX", "memoryPartition": "NPS1", "numGPUsAssigned": 1}]
        }

    # --- Negative test profiles ---

    # invalidgpucount: skipped GPUs + assigned counts that don't match active → DCM rejects
    if num_gpus > 1:
        n_skip = max(1, num_gpus // 4)
        skip_ids = list(range(num_gpus - n_skip, num_gpus))
        active = num_gpus - n_skip
        # Intentionally assign more than active so count check fails
        g1 = max(1, active // 2 + 1)
        g2 = max(1, active // 2 + 1)
        profiles["invalidgpucount"] = {
            "skippedGPUs": {"ids": skip_ids},
            "profiles": [
                {"computePartition": "CPX", "memoryPartition": "NPS1", "numGPUsAssigned": g1},
                {"computePartition": "SPX", "memoryPartition": "NPS1", "numGPUsAssigned": g2},
            ]
        }
    else:
        # Skip the only GPU (active=0) but still assign 1 → count mismatch
        profiles["invalidgpucount"] = {
            "skippedGPUs": {"ids": [0]},
            "profiles": [{"computePartition": "CPX", "memoryPartition": "NPS1", "numGPUsAssigned": 1}]
        }

    # invalcomputetype: "invalid" compute partition name → DCM rejects
    if num_gpus > 1:
        active = num_gpus - 1
        g1 = max(1, active // 2)
        profiles["invalcomputetype"] = {
            "skippedGPUs": {"ids": [num_gpus - 1]},
            "profiles": [
                {"computePartition": "invalid", "memoryPartition": "NPS1", "numGPUsAssigned": g1},
                {"computePartition": "SPX", "memoryPartition": "NPS1", "numGPUsAssigned": active - g1},
            ]
        }
    else:
        profiles["invalcomputetype"] = {
            "skippedGPUs": {"ids": []},
            "profiles": [{"computePartition": "invalid", "memoryPartition": "NPS1", "numGPUsAssigned": 1}]
        }

    # invalmemorytype: "invalid" memory partition name → DCM rejects
    half = max(1, num_gpus // 2)
    if num_gpus > 1:
        profiles["invalmemorytype"] = {
            "profiles": [
                {"computePartition": "CPX", "memoryPartition": "invalid", "numGPUsAssigned": half},
                {"computePartition": "SPX", "memoryPartition": "NPS1", "numGPUsAssigned": num_gpus - half},
            ]
        }
    else:
        profiles["invalmemorytype"] = {
            "profiles": [{"computePartition": "CPX", "memoryPartition": "invalid", "numGPUsAssigned": 1}]
        }

    # invalmemorytypecombinationNPS1NPS4: mixing NPS1 and NPS4 in one config → DCM rejects
    if num_gpus > 1:
        profiles["invalmemorytypecombinationNPS1NPS4"] = {
            "profiles": [
                {"computePartition": "CPX", "memoryPartition": "NPS4", "numGPUsAssigned": half},
                {"computePartition": "SPX", "memoryPartition": "NPS1", "numGPUsAssigned": num_gpus - half},
            ]
        }
    else:
        profiles["invalmemorytypecombinationNPS1NPS4"] = {
            "profiles": [{"computePartition": "CPX", "memoryPartition": "NPS4", "numGPUsAssigned": 1}]
        }

    # invalidmissingfields-memoryPartition: entry missing memoryPartition field → DCM rejects
    if num_gpus > 1:
        active = num_gpus - 1
        g1 = max(1, active // 2)
        profiles["invalidmissingfields-memoryPartition"] = {
            "skippedGPUs": {"ids": [num_gpus - 1]},
            "profiles": [
                {"computePartition": "CPX", "numGPUsAssigned": g1},  # missing memoryPartition
                {"computePartition": "SPX", "memoryPartition": "NPS1", "numGPUsAssigned": active - g1},
            ]
        }
    else:
        profiles["invalidmissingfields-memoryPartition"] = {
            "skippedGPUs": {"ids": []},
            "profiles": [{"computePartition": "CPX", "numGPUsAssigned": 1}]  # missing memoryPartition
        }

    # invalidmissingfields-computePartition: entry missing computePartition field → DCM rejects
    if num_gpus > 1:
        active = num_gpus - 1
        g1 = max(1, active // 2)
        profiles["invalidmissingfields-computePartition"] = {
            "skippedGPUs": {"ids": [num_gpus - 1]},
            "profiles": [
                {"memoryPartition": "NPS1", "numGPUsAssigned": g1},  # missing computePartition
                {"computePartition": "SPX", "memoryPartition": "NPS1", "numGPUsAssigned": active - g1},
            ]
        }
    else:
        profiles["invalidmissingfields-computePartition"] = {
            "skippedGPUs": {"ids": []},
            "profiles": [{"memoryPartition": "NPS1", "numGPUsAssigned": 1}]  # missing computePartition
        }

    # highgpucount_mostly_invalid: absurd GPU count → DCM rejects regardless of node count
    profiles["highgpucount_mostly_invalid"] = {
        "profiles": [{"computePartition": "CPX", "memoryPartition": "NPS4", "numGPUsAssigned": 3543}]
    }

    return {
        "gpu-config-profiles": profiles,
        "gpuClientSystemdServices": {"names": ["amd-metrics-exporter", "gpuagent"]}
    }

def generate_partitioning_check_file(gpu_series: str, num_gpus: int, logs_dir: str) -> str:
    """Write partitioning_check_{gpu_series}_{num_gpus}.json to logs_dir.

    Always regenerates the file (called once per session after GPU info is collected).
    Returns the full path to the written file.
    """
    os.makedirs(logs_dir, exist_ok=True)
    file_path = os.path.join(logs_dir, f"partitioning_check_{gpu_series}_{num_gpus}.json")
    config = generate_partitioning_check(gpu_series, num_gpus)
    with open(file_path, "w") as fp:
        json.dump(config, fp, indent=4)
    Logger.info(f"Generated partition config: {file_path}")
    return file_path
