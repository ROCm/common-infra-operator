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

"""
Pytest configuration and shared fixtures for standalone (debian/docker) tests.

Standalone tests run entirely via SSH — no K8s cluster required. Node and GPU
discovery uses direct SSH commands (lspci, uname, /etc/os-release) instead of
K8s debug pods.  This conftest overrides the root-level gpu_cluster,
gather_device_info, and generate_partition_configs fixtures so that the
standalone test tree is fully self-contained with only testbed.json + SSH.
"""

import pdb
import pytest
import os
import logging
import json
from lib import common
import lib.amdgpu as amdgpu_util
import lib.node_gpu_collector as node_gpu_collector

Logger = logging.getLogger("standalone.conftest")

@pytest.fixture(scope="session")
def inbox_driver_skip(environment):
    if environment.amdgpu_driver_spec["driver-deployment"] == "inbox":
        pytest.skip("Using inbox amdgpu driver - skip")
    return

@pytest.fixture(scope="session")
def exporter_release_name():
    return "device-metrics-exporter"


@pytest.fixture(scope="session")
def gpu_cluster(request, environment):
    """Build GPU cluster from testbed.json via SSH discovery — no K8s needed."""
    global Logger

    testbed_path = request.config.option.testbed
    if not testbed_path or not os.path.exists(testbed_path):
        pytest.fail("--testbed <testbed.json> is required for standalone tests")

    with open(testbed_path, "r") as fp:
        testbed = json.load(fp)

    instances = testbed.get("instances", [])
    if not instances:
        pytest.fail("No instances found in testbed.json")

    nodes = []
    for entry in instances:
        node = common.cluster_node(
            ip_address=entry["ip"],
            user_name=entry.get("username"),
            password=entry.get("password"),
            node_type=entry.get("type", "worker"),
        )
        node_gpu_collector.populate_node_info_ssh(node)
        nodes.append(node)

    cluster = common.standalone_gpu_nodes(nodes)

    if hasattr(environment, "k8_secrets_file"):
        with open(environment.k8_secrets_file) as fp:
            cluster.k8_secrets = json.load(fp)

    cluster.k8_registry = getattr(environment, "default_registry", "docker.io")

    gpu_nodes = [n for n in nodes if n.is_gpu_node()]
    if not gpu_nodes:
        node_summary = ", ".join(
            f"{n.host_name or n.ip_address}(series={n.gpu_series}, gpus={n.num_gpus})"
            for n in nodes
        )
        pytest.fail(f"No GPU nodes found via SSH discovery. Nodes: [{node_summary}]")

    setattr(pytest, "_k8_cluster_inst", cluster)
    Logger.info(f"Standalone cluster: {len(nodes)} node(s), {len(gpu_nodes)} GPU node(s)")
    return cluster


@pytest.fixture(scope="session", autouse=True)
def gather_device_info(gpu_cluster, images, environment):
    """Standalone override: GPU info already populated via SSH in gpu_cluster.

    Seeds amdgpu_driver_version from driver spec (same as root conftest).
    """
    if hasattr(environment, 'amdgpu_driver_spec'):
        spec_rocm_ver = environment.amdgpu_driver_spec.get('default-version')
        if spec_rocm_ver:
            amdgpu_ver = amdgpu_util.get_matching_driver_version(spec_rocm_ver) or spec_rocm_ver
            for node in gpu_cluster.cluster_nodes:
                if node.is_gpu_node() and node.amdgpu_driver_version is None:
                    node.amdgpu_driver_version = amdgpu_ver
                    Logger.info(f"Node {node.host_name}: seeded amdgpu_driver_version={amdgpu_ver}")


@pytest.fixture(scope="session", autouse=True)
def generate_partition_configs(gather_device_info, gpu_cluster, environment):
    """Generate partitioning_check JSON files for standalone GPU nodes."""
    seen = set()
    for node in gpu_cluster.cluster_nodes:
        if node.gpu_series and node.num_gpus > 0:
            key = (node.gpu_series, node.num_gpus)
            if key not in seen:
                seen.add(key)
                amdgpu_util.generate_partitioning_check_file(
                    node.gpu_series, node.num_gpus, environment.logdir
                )
    if seen:
        Logger.info(f"Generated partition configs for: {sorted(seen)}")


@pytest.fixture(scope="session", autouse=True)
def init_testbed(request, gpu_cluster, exporter_release_name, environment):
    """Standalone testbed initialisation — no K8s operations."""
    global Logger
    Logger.info(f"Standalone testbed ready: {len(gpu_cluster.cluster_nodes)} node(s)")
    yield
    Logger.info("Standalone test session complete")
    return
