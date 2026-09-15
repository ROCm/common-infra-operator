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

"""AMD GPU Operator Partitioning Scale Test Suite.

This module contains scale tests for GPU partitioning at maximum partition count.
Tests validate that the cluster can schedule one workload per virtual GPU and that
the metrics exporter correctly handles all virtual GPU endpoints when GPUs are
split at maximum CPX_NPS1 partition depth.

Background:
    With N physical GPUs each split 8-way (CPX), the maximum addressable virtual
    GPU count is (N * 8) - 1. For example, 8 physical GPUs yield 63 virtual GPUs
    (not 64 — hardware/driver limit). These tests stress the scheduler and exporter's
    ability to handle all partitions simultaneously.

Supported GPU Series:
    MI350X, MI350P
"""

import pprint
import pytest
import time
import logging
import lib.k8_util as k8_util
import lib.amdgpu as amdgpu_util
from lib.util import K8Helper
from test_config_manager import (
    get_gpu_series,
    run_partition_test_scenario,
    exporter_nodeport_exp_config,
)

Logger = logging.getLogger("k8.gpu-operator.scale.test_partitioning_scale")


@pytest.mark.parametrize("profile", ["CPX_NPS1", "CPX_NPS2", "CPX_NPS4"])
@pytest.mark.parametrize("gpu_series_filter", ["MI350X", "MI350P", "MI300X", "MI325X"])
def test_partitioning_max_workloads(gpu_cluster, deviceconfig_install, environment, request,
                                    gpu_series_filter, profile):
    """Scale test: launch one busybox workload per virtual GPU at maximum partition depth.

    Validates that:
    1. The partition profile applies successfully via DCM (amd-smi validated).
    2. The cluster can schedule one pod per virtual GPU simultaneously.
    3. The metrics exporter correctly enumerates and exports metrics for all virtual GPU endpoints.

    The expected pod count is read from device-plugin allocatable after partitioning,
    so this test is hardware-agnostic and correct for any GPU series or partition factor.

    Args:
        gpu_cluster: Cluster object with GPU node information.
        deviceconfig_install: DeviceConfig fixture with Config Manager enabled.
        environment: Test environment fixture.
        request: Pytest request for finalizers.
        gpu_series_filter: GPU series this test targets (parametrized).
        profile: Partition profile to apply (parametrized).
    """
    gpu_series = get_gpu_series(gpu_cluster, environment)
    if gpu_series_filter not in gpu_series:
        pytest.skip(f"Testcase targets {gpu_series_filter}, cluster has {gpu_series}")

    dut_node = gpu_cluster.find_node_by_gpu_series(gpu_series)
    if not amdgpu_util.supports_config_manager(dut_node.device_id):
        pytest.skip(f"GPU partitioning not supported for {gpu_series}")

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "Failed to get GPU nodes")
    K8Helper.triage(environment, len(gpu_nodes) > 0, "No GPU nodes found in cluster")

    run_partition_test_scenario(gpu_cluster, environment, request, profile, workload=False)

    # Derive expected pod count per node from device-plugin allocatable — ground truth for any GPU/profile.
    node_alloc = {}
    for node in gpu_nodes:
        worker = k8_util.k8_get_node_hostname(node)
        cap, alloc = k8_util.k8_get_node_gpu_capacity(worker)
        node_pods = int(alloc)
        K8Helper.triage(environment, node_pods > 0,
                         f"Device-plugin reported 0 allocatable vGPUs on {worker} after {profile} partition "
                         f"(capacity={cap})")
        Logger.info(f"Node {worker}: {profile} → capacity={cap} allocatable={alloc} vGPUs")
        node_alloc[worker] = node_pods

    expected_pods = sum(node_alloc.values())

    def _cleanup_workload():
        k8_util.k8_delete_all_pods("default")

    request.addfinalizer(_cleanup_workload)
    _cleanup_workload()

    # Launch one pod per vGPU per node; no_look=True skips per-pod wait,
    # we do one bulk poll below instead.
    for worker, count in node_alloc.items():
        for i in range(count):
            params = {
                "node_name": worker,
                "num_gpu_reqd": 1,
                "workload_selection": "busybox-workload",
                "pod_name": f"gpu-workload-{worker}-part-{i}",
                "no_look": True,
            }
            K8Helper.workload_operation(environment, K8Helper.WorkloadOp.START_WORKLOAD, **params)

    _POLL_INTERVAL = 10
    _POLL_TIMEOUT = expected_pods * 10  # ~10s per pod to schedule and reach Running
    running_pods = []
    list_of_pods = []
    for _ in range(_POLL_TIMEOUT // _POLL_INTERVAL):
        ret_code, list_of_pods = k8_util.k8_get_pods("default", pod_name_pattern="gpu-workload-")
        K8Helper.triage(environment, ret_code == 0, f"Failed to list pods during scheduling poll (ret={ret_code})")
        if list_of_pods:
            running_pods = [p for p in list_of_pods if p['status'].get('phase') == 'Running']
            Logger.info(f"Pods: {len(list_of_pods)} total, {len(running_pods)} Running "
                        f"(target: {expected_pods})")
            if len(running_pods) == expected_pods:
                break
        time.sleep(_POLL_INTERVAL)

    K8Helper.triage(environment, len(running_pods) == expected_pods,
                     f"Expected {expected_pods} Running pods, got {len(running_pods)} Running "
                     f"out of {len(list_of_pods)} total:\n{pprint.pformat(list_of_pods)}")

    exporter_nodeport_exp_config(request, gpu_cluster, deviceconfig_install, environment)
