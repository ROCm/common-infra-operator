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
AMD Device Metrics Exporter (DME) Docker Container Test Suite.

This test suite validates the Docker-based deployment of the AMD Device Metrics
Exporter on GPU nodes. It covers:

- Docker container deployment with proper device access (/dev/dri, /dev/kfd)
- Container registry authentication and image pulling
- Volume mounting for configuration files (/tmp/etc/metrics:/etc/metrics)
- Metrics endpoint availability and responsiveness
- Configuration file handling and dynamic updates
- Profiler metrics enablement

Docker Deployment Model:
- Runs as daemon container with GPU device passthrough
- Exposes metrics on port 5000
- Mounts config from host filesystem
- Requires AMDGPU driver on host

Test Environment:
- Requires GPU cluster with Docker installed
- AMDGPU driver must be installed on host
- Network access to download reference config from ROCm repository
- Optional: Container registry credentials for private images

Key Dependencies:
- lib.k8_util: Kubernetes cluster utilities
- lib.util.K8Helper: Test assertion and triage utilities
- Docker daemon on GPU nodes
"""

import copy
import pdb
import pytest
import pprint
import sys
import os
import re
import time
import json
import logging
import lib.k8_util as k8_util
import lib.amdgpu as amdgpu
import lib.common as common
import lib.spec_util as spec_util
import lib.metric_util as metric_util
from lib.util import K8Helper

Logger = logging.getLogger("standalone.docker.test_exporter_docker")

def test_deploy_exporter_docker_container(gpu_cluster, run_exporter_docker_container, environment):
    """
    Verify Docker container deployment and metrics endpoint availability.

    This test validates that the AMD Metrics Exporter Docker container is successfully
    deployed and the metrics endpoint is accessible on all GPU nodes.

    Test Flow:
    1. Iterate through all GPU nodes in the cluster
    2. Send HTTP GET request to port 5000 at /metrics endpoint
    3. Verify successful response from each node
    4. Collect any failed endpoints

    Success Criteria:
        - All GPU nodes return metrics successfully without errors
        - No failed endpoints reported

    This is the basic smoke test to verify:
    - Container is running
    - GPU device access is working (/dev/dri, /dev/kfd)
    - Port mapping is correct (5000:5000)
    - Metrics exporter application started successfully inside container
    - Network connectivity to metrics endpoint

    Args:
        gpu_cluster: GPU cluster fixture
        run_exporter_docker_container: Deployment fixture ensuring container is running
        environment: Test environment for triaging

    Raises:
        AssertionError: If any GPU node fails to respond with metrics
    """
    global Logger
    failed_endpoints = set()
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            ret_code, ret_stdout, ret_stderr = node.http_get(5000, "metrics")
            if ret_code != 0:
                failed_endpoints.add(node.ip_address)
                Logger.error(f"Failed to get metrics from nodeport endpoint for {node.ip_address}, stdout: {ret_stdout} stderr: {ret_stderr}")
    K8Helper.triage(environment, (len(failed_endpoints) == 0),
                    f"One or more metric endpoints HTTP-GET failed, nodes: {failed_endpoints}")

def test_apply_exporter_config(gpu_cluster, run_exporter_docker_container, environment):
    """
    Verify container uses reference configuration correctly.

    This test validates that the Docker container properly loads and applies the
    reference configuration file that was mounted via volume.

    Configuration Loading Mechanism:
        - Host config: /tmp/etc/metrics/config.json
        - Container mount: -v /tmp/etc/metrics:/etc/metrics
        - Container reads: /etc/metrics/config.json
        - Reference config URL: https://raw.githubusercontent.com/ROCm/device-metrics-exporter/refs/heads/main/example/config.json

    Test Flow:
    1. Query metrics endpoint on all GPU nodes
    2. Verify successful response from each node
    3. Log confirmation to check for supported metrics
    4. Collect any failed endpoints

    Success Criteria:
        - All GPU nodes return metrics successfully
        - Configuration is properly loaded from mounted volume
        - Default metrics from reference config are available

    Future Enhancement:
        Parse metrics output to verify specific metrics defined in reference config

    Args:
        gpu_cluster: GPU cluster fixture
        run_exporter_docker_container: Deployment fixture with reference config
        environment: Test environment for triaging

    Raises:
        AssertionError: If any GPU node fails to respond with metrics
    """
    global Logger

    # Verify metrics with reference config.json
    # https://raw.githubusercontent.com/ROCm/device-metrics-exporter/refs/heads/main/example/config.json
    failed_endpoints = set()
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            ret_code, ret_stdout, ret_stderr = node.http_get(5000, "metrics")
            if ret_code != 0:
                failed_endpoints.add(node.ip_address)
                Logger.error(f"Failed to get metrics from nodeport endpoint for {node.ip_address}, stdout: {ret_stdout} stderr: {ret_stderr}")
            else:
                Logger.debug("Check for all supported metrics in the output")
    K8Helper.triage(environment, (len(failed_endpoints) == 0),
                    f"One or more metric endpoints HTTP-GET failed, nodes: {failed_endpoints}")

def test_enable_profiler_metrics(gpu_cluster, run_exporter_docker_container, environment):
    """
    Verify profiler metrics can be enabled via configuration file update.

    This test validates that the Docker container detects configuration file changes
    in the mounted volume and applies the profiler metrics configuration dynamically.

    Profiler Metrics:
        Profiler metrics provide detailed GPU performance data including:
        - GPU utilization statistics
        - Memory bandwidth metrics
        - Compute unit activity
        - Power consumption details
        - Shader engine utilization

    Configuration Changes:
        Creates a new config with:
        - CommonConfig.HealthService.Enable = false
        - GPUConfig.ProfilerMetrics.all = true

    Test Flow:
    1. Create profiler-metrics-config.json with profiler enabled
    2. Upload modified config to /tmp/etc/metrics/config.json on each node
       (This updates the file in the volume mounted by the container)
    3. Container detects file change and reloads configuration
    4. Query metrics endpoint to verify it still responds
    5. (Future: Validate profiler-specific metrics are present in output)
    6. Restore original reference config
    7. Upload restored config back to nodes

    Volume Mount Behavior:
        Since the container mounts -v /tmp/etc/metrics:/etc/metrics, any changes
        to /tmp/etc/metrics/config.json on the host are immediately visible to
        the container, allowing dynamic configuration updates without restart.

    Success Criteria:
        - Config file can be updated via mounted volume
        - Container accepts profiler metrics configuration
        - Metrics endpoint continues to function
        - Configuration restoration works

    Future Enhancement:
        Parse metrics output to verify profiler-specific metrics are present

    Args:
        gpu_cluster: GPU cluster fixture
        run_exporter_docker_container: Container deployment fixture
        environment: Test environment for triaging

    Raises:
        AssertionError: If config upload fails or metrics endpoint fails
    """
    global Logger

    # Build config.json with profiler metrics enabled
    config_json_file = os.path.join(environment.logdir, "profiler-metrics-config.json")
    config_map = {
        "CommonConfig": {
            "HealthService": {
                "Enable": False,
            },
        },
        "GPUConfig": {
            "ProfilerMetrics": {
                "all": True,
            }
        },
    }
    with open(config_json_file, "w") as fp:
        fp.write(json.dumps(config_map, indent=4))

    K8Helper.triage(environment, (os.path.exists(config_json_file)), f"Failed to create profiler-metrics-config.json file")

    # Upload modified config to mounted volume location
    remote_file = "/tmp/etc/metrics/config.json"
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            K8Helper.triage(environment, (node.put(config_json_file, remote_file)),
                            f"Unable to upload profiler-metrics config.json")

    # Verify metrics endpoint with profiler config
    failed_endpoints = set()
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            ret_code, ret_stdout, ret_stderr = node.http_get(5000, "metrics")
            if ret_code != 0:
                failed_endpoints.add(node.ip_address)
                Logger.error(f"Failed to get metrics from nodeport endpoint for {node.ip_address}, stdout: {ret_stdout} stderr: {ret_stderr}")
            else:
                Logger.debug("Check for all profiler metrics in the output")
    K8Helper.triage(environment, (len(failed_endpoints) == 0),
                    f"One or more metric endpoints HTTP-GET failed, nodes: {failed_endpoints}")

    # Restore original reference configuration
    reference_cfg_json_file = os.path.join(environment.logdir, "reference-config.json")
    K8Helper.triage(environment, (os.path.exists(reference_cfg_json_file)), f"Failed to download reference config.json file")
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            K8Helper.triage(environment, (node.put(reference_cfg_json_file, remote_file)),
                            f"Unable to upload reference config.json")

def test_exporter_amdgpuhealth_hostpath(gpu_cluster, run_exporter_docker_container, environment):
    global Logger

    # Check if amdgpuhealth utility exists and is executable on each node - /var/lib/amd-metrics-exporter
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            # Check if directory exists
            cmd = "test -d /var/lib/amd-metrics-exporter"
            ret_code, resp_stdout, resp_stderr = node.run_command(cmd)
            K8Helper.triage(environment, ret_code == 0,
                            f"Directory /var/lib/amd-metrics-exporter does not exist - amdgpuhealth feature not currently supported",
                            expected_to_fail=True)
            Logger.debug(f"Directory /var/lib/amd-metrics-exporter exists")

            # List directory contents
            cmd = "ls -la /var/lib/amd-metrics-exporter"
            ret_code, resp_stdout, resp_stderr = node.run_command(cmd)
            Logger.info(f"Contents of /var/lib/amd-metrics-exporter:\n{resp_stdout}")

            # Check if file exists
            cmd = "test -f /var/lib/amd-metrics-exporter/amdgpuhealth"
            ret_code, resp_stdout, resp_stderr = node.run_command(cmd)
            K8Helper.triage(environment, ret_code == 0,
                            f"File /var/lib/amd-metrics-exporter/amdgpuhealth does not exist",
                            expected_to_fail=True)
            Logger.debug(f"File exists check passed for /var/lib/amd-metrics-exporter/amdgpuhealth")

            # Check if file is executable
            cmd = "test -x /var/lib/amd-metrics-exporter/amdgpuhealth"
            ret_code, resp_stdout, resp_stderr = node.run_command(cmd)
            K8Helper.triage(environment, ret_code == 0,
                            f"File /var/lib/amd-metrics-exporter/amdgpuhealth is not executable",
                            expected_to_fail=True)
            Logger.debug(f"File executable check passed for /var/lib/amd-metrics-exporter/amdgpuhealth")


def test_exporter_no_persistent_kfd_hold(gpu_cluster, run_exporter_docker_container, environment):
    """
    Verify the exporter container does not persistently hold /dev/kfd open.

    Root cause: amdsmi_init opens /dev/kfd and holds the fd for the container process
    lifetime. While held, amd-smi reset -r fails, blocking GPU partition-mode switching.
    See test_exporter_debian_pkg for context.

    Required behavior:
    - At idle (no active scrape): /dev/kfd must NOT be held by the container process.
    - After each /metrics scrape completes: /dev/kfd must be released immediately.

    fd-hold detection: 'sudo ls -la /proc/<host_pid>/fd/ | grep kfd' on the host node.
    /proc is used instead of lsof — available on any Linux node, no package dependency.
    The host-side PID is retrieved via 'docker inspect --format={{.State.Pid}}'.
    Empty output = fd not held = pass. Non-empty output = fd held = fail.

    The test performs 3 scrape-release cycles to catch lazy-init bugs where
    amdsmi_init is invoked per-scrape without a matching amdsmi_shutdown.
    """
    global Logger

    _CONTAINER_NAME = "device-metrics-exporter"
    _DEVICE         = "/dev/kfd"
    _SCRAPE_COUNT   = 3

    gpu_nodes = [node for node in gpu_cluster.cluster_nodes if node.is_gpu_node()]
    K8Helper.triage(environment, len(gpu_nodes) > 0,
                    "No GPU nodes found in cluster — no AMD GPU hardware detected")

    for node in gpu_nodes:
        # pre-flight: driver must be loaded
        rc, _, _ = node.run_command("lsmod | grep -w amdgpu")
        K8Helper.triage(environment, rc == 0,
                        f"[{node.ip_address}] amdgpu driver is not loaded — "
                        f"load the driver before running this test")

        # get container host-side PID via docker inspect
        inspect_cmd = "docker inspect --format='{{.State.Pid}}' " + _CONTAINER_NAME
        rc, pid_out, stderr = node.run_command(inspect_cmd)
        K8Helper.triage(environment, rc == 0,
                        f"[{node.ip_address}] docker inspect failed for {_CONTAINER_NAME}: {stderr}")
        host_pid = pid_out.strip()
        K8Helper.triage(environment, bool(host_pid) and host_pid != "0",
                        f"[{node.ip_address}] Invalid host PID '{host_pid}' — "
                        f"container {_CONTAINER_NAME} may not be running")
        Logger.info(f"[{node.ip_address}] Container {_CONTAINER_NAME} host PID={host_pid}")

        time.sleep(10)

        # idle check: fd must not be held at rest
        rc, fd_out, _ = node.run_command(f"sudo ls -la /proc/{host_pid}/fd/ | grep kfd")
        K8Helper.triage(environment, fd_out.strip() == "",
                        f"[{node.ip_address}] FAIL idle check: container {_CONTAINER_NAME} "
                        f"(host_pid={host_pid}) holds {_DEVICE} at idle. /proc/{host_pid}/fd output:\n{fd_out}")
        Logger.info(f"[{node.ip_address}] Idle check passed: {_DEVICE} not held by host_pid={host_pid}")

        # scrape 3 times and verify fd is released after each scrape
        for i in range(1, _SCRAPE_COUNT + 1):
            rc, ret_stdout, ret_stderr = node.http_get(5000, "metrics")
            K8Helper.triage(environment, rc == 0,
                            f"[{node.ip_address}] Scrape {i}/{_SCRAPE_COUNT}: "
                            f"metrics endpoint failed: {ret_stderr}")

            time.sleep(2)

            rc, fd_out, _ = node.run_command(f"sudo ls -la /proc/{host_pid}/fd/ | grep kfd")
            K8Helper.triage(environment, fd_out.strip() == "",
                            f"[{node.ip_address}] FAIL post-scrape check {i}/{_SCRAPE_COUNT}: "
                            f"container {_CONTAINER_NAME} (host_pid={host_pid}) holds "
                            f"{_DEVICE} after scrape. /proc/{host_pid}/fd output:\n{fd_out}")
            Logger.info(f"[{node.ip_address}] Scrape {i}/{_SCRAPE_COUNT} post-check passed: "
                        f"{_DEVICE} released")

        # rmmod must succeed — EBUSY means fd is still held
        time.sleep(5)
        rc, _, stderr = node.run_command("sudo rmmod amdgpu")
        # reload driver regardless of rmmod outcome so the node is always left in a clean state
        node.run_command("sudo modprobe amdgpu")
        K8Helper.triage(environment, rc == 0,
                        f"[{node.ip_address}] rmmod amdgpu failed — container {_CONTAINER_NAME} "
                        f"may still hold {_DEVICE} (EBUSY): {stderr}")
        Logger.info(f"[{node.ip_address}] rmmod amdgpu succeeded — {_DEVICE} not held by container {_CONTAINER_NAME}")

def test_metric_coverage(gpu_cluster, run_exporter_docker_container, environment):
    """Verify no metrics exported by the DME are absent from metrics-support.json.

    Enables profiler metrics before scraping so the full emitted metric set is captured.
    """
    global Logger

    # Start from reference config so other settings are preserved
    ref_cfg_path = os.path.join(environment.logdir, "reference-config.json")
    with open(ref_cfg_path) as fp:
        ref_config_data = json.load(fp)
    config_map = copy.deepcopy(ref_config_data)
    config_map.setdefault("GPUConfig", {}).setdefault("ProfilerMetrics", {})["all"] = True
    profiler_cfg = os.path.join(environment.logdir, "coverage-profiler-config.json")
    with open(profiler_cfg, "w") as fp:
        json.dump(config_map, fp, indent=4)
    remote_cfg = "/tmp/etc/metrics/config.json"
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            K8Helper.triage(environment, node.put(profiler_cfg, remote_cfg),
                            f"Failed to upload profiler config to {node.ip_address}")
    time.sleep(30)  # Wait for DME hot-reload

    failed_nodes = {}
    for node in gpu_cluster.cluster_nodes:
        if not node.is_gpu_node():
            continue
        ret_code, ret_stdout, ret_stderr = node.http_get(5000, "metrics")
        K8Helper.triage(environment, ret_code == 0,
                        f"Metrics endpoint not responding on {node.ip_address}: {ret_stderr}")
        metrics_dump = os.path.join(environment.logdir, f"coverage-metrics-{node.host_name}.txt")
        with open(metrics_dump, "wb") as fp:
            fp.write(ret_stdout)
        Logger.info(f"Raw metrics dump written to {metrics_dump}")
        scraped = metric_util.parse_metric_data(ret_stdout)
        untracked = metric_util.find_untracked_metrics(
            scraped, gpu_series=node.gpu_series, amdgpu_driver=node.amdgpu_driver_version,
            skip_profiler_metrics=False, num_gpus=node.num_gpus,
        )
        if untracked:
            Logger.warning(f"Node {node.host_name}: {len(untracked)} untracked metrics: {sorted(untracked)}")
            failed_nodes[node.host_name] = sorted(untracked)
    K8Helper.triage(environment, not failed_nodes,
                    f"DME exports metrics not tracked in metrics-support.json: {failed_nodes}")
