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

import pdb
import pytest
import pprint
import sys
import os
import time
import json
import logging
import random
import functools
import subprocess
import lib.common as common
import lib.helm_util as helm_util
import lib.k8_util as k8_util
import lib.spec_util as spec_util
import lib.npd_util as npd_util
from lib.util import K8Helper
#pytestmark = pytest.mark.skip("debugging")
Logger = logging.getLogger("k8.test_node_problem_detector")


def _build_ip_san_entries(gpu_nodes):
    """Build OpenSSL IP SAN entries from GPU node InternalIPs for TLS cert generation."""
    ip_entries = []
    for node in gpu_nodes:
        ip = k8_util.k8_get_node_address(node, "InternalIP")
        if ip:
            ip_entries.append(ip)
    return "\n".join(f"IP.{i+1} = {ip}" for i, ip in enumerate(ip_entries))


def query_gpu_metric_value(namespace, metric_name, timeout=120, interval=15):
    """Query current value of a GPU metric from all exporter pods.

    Waits for exporter pods to become ready and retries until a valid metric
    value is returned or timeout expires.

    In multi-node clusters each worker has its own exporter pod. This queries
    all of them and returns the max value so the dynamic threshold covers
    the hottest/busiest node at idle.

    Returns:
        float or None: Max metric value across all exporter pods, or None if not found.
    """
    cmd_template = "curl -s localhost:5000/metrics | grep '^{}{{' | head -1"
    deadline = time.time() + timeout

    while time.time() < deadline:
        ret_code, pods = k8_util.k8_get_pods(namespace, pod_name_pattern="metrics-exporter")
        if ret_code != 0 or not pods:
            Logger.debug(f"No exporter pods found, retrying in {interval}s")
            time.sleep(interval)
            continue

        max_value = None
        all_pods_ready = True
        cmd = ["sh", "-c", cmd_template.format(metric_name)]
        for pod in pods:
            pod_name = pod['metadata']['name']
            container_statuses = (pod.get('status') or {}).get('container_statuses') or []
            container_ready = any(
                cs.get('name') == 'metrics-exporter-container' and cs.get('ready')
                for cs in container_statuses
            )
            if not container_ready:
                Logger.debug(f"Pod {pod_name}: exporter container not ready")
                all_pods_ready = False
                continue
            try:
                ret_code, stdout, stderr = k8_util.exec_command_in_pod(
                    namespace, cmd, pod_name, container_name="metrics-exporter-container")
            except Exception:
                all_pods_ready = False
                continue
            if ret_code != 0 or not stdout or not stdout.strip():
                continue
            parts = stdout.strip().split()
            try:
                val = float(parts[-1])
                if max_value is None or val > max_value:
                    max_value = val
            except (ValueError, IndexError):
                continue

        if max_value is not None:
            return max_value

        remaining = int(deadline - time.time())
        Logger.debug(f"No valid metric value yet (pods_ready={all_pods_ready}), "
                     f"retrying in {interval}s ({remaining}s remaining)")
        time.sleep(interval)

    Logger.error(f"Failed to query {metric_name} from exporter pods after {timeout}s")
    return None


@pytest.fixture(scope="module")
def deviceconfig_install(gpu_cluster, images, gpu_operator_install, environment):
    """
    Fixture to deploy GPU operator DeviceConfig with metrics-exporter enabled.

    This fixture creates DeviceConfig CRs for all GPU nodes in the cluster with:
    - Driver enabled
    - Device plugin enabled
    - Metrics exporter enabled with NodePort service type
    - Unique NodePorts assigned per DeviceConfig when multiple configs exist

    The metrics exporter writes the amdgpuhealth binary to /var/lib/amd-metrics-exporter
    on each node, which is required for NPD custom plugin integration.

    Yields:
        DeviceConfigCRInfo: Object containing:
            - test_cfg_map: Map of DeviceConfig names to their configurations
            - exporter_port_map: Map of node hostnames to exporter NodePorts
            - devicecfg_list: List of created DeviceConfig names

    Cleanup:
        Removes all created DeviceConfig CRs
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
            'devicePlugin.enableNodeLabeller' : False,
            'metricsExporter.enable' : True,
            'metricsExporter.serviceType' : 'NodePort',
        }
    test_config.update(images)

    test_cfg_map = spec_util.build_deviceconfig_cr_template(test_config, gpu_nodes, 'exporter', environment.amdgpu_driver_spec)
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

def test_exporter_amdgpuhealth_hostpath(gpu_cluster, deviceconfig_install, environment):
    """
    Verify that amdgpuhealth binary is available on all GPU nodes.

    Test validates:
    1. DeviceConfig pods (device-plugin, metrics-exporter) are running
    2. Metrics exporter has written amdgpuhealth binary to /var/lib/amd-metrics-exporter
    3. The binary is executable on each GPU node

    This is a prerequisite for NPD custom plugin integration, which needs to mount
    /var/lib/amd-metrics-exporter from the host into the NPD container.

    Dependencies:
        - deviceconfig_install: Creates DeviceConfig with metrics-exporter enabled
    """
    global Logger
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")

    # Watch for all pod creation
    '''
    test-deviceconfig-device-plugin-8f7px                        1/1     Running       0                 12d
    test-deviceconfig-metrics-exporter-27gq9                     2/2     Running       0                 12d
    test-deviceconfig-node-labeller-54vpd                        1/1     Running       0                 12d
    '''
    devicecfg_pods = [
        common.PodInfo('device-plugin', len(gpu_nodes), 1),
        common.PodInfo('metrics-exporter', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods)
    K8Helper.triage(environment, not failed_pods, f"One or more pods are not ready - {failed_pods}")

    time.sleep(30) # Wait for exporter to start working

    # Check if amdgpuhealth utility is mounted on each node - /var/lib/amd-metrics-exporter
    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)

        # Check if directory exists
        cmd = ["test", "-d", "/var/lib/amd-metrics-exporter"]
        ret_code, resp_stdout = k8_util.run_command_on_node(gpu_cluster, node_name, cmd)
        K8Helper.triage(environment, ret_code == 0, f"Directory /var/lib/amd-metrics-exporter does not exist on {node_name}")
        Logger.debug(f"Directory /var/lib/amd-metrics-exporter exists on {node_name}")

        # List directory contents
        cmd = ["ls", "-la", "/var/lib/amd-metrics-exporter"]
        ret_code, resp_stdout = k8_util.run_command_on_node(gpu_cluster, node_name, cmd)
        Logger.info(f"Contents of /var/lib/amd-metrics-exporter on {node_name}:\n{resp_stdout}")

        # Check if file exists
        cmd = ["test", "-f", "/var/lib/amd-metrics-exporter/amdgpuhealth"]
        ret_code, resp_stdout = k8_util.run_command_on_node(gpu_cluster, node_name, cmd)
        K8Helper.triage(environment, ret_code == 0, f"File /var/lib/amd-metrics-exporter/amdgpuhealth does not exist on {node_name}")
        Logger.debug(f"File exists check passed for /var/lib/amd-metrics-exporter/amdgpuhealth on {node_name}")

        # Check if file is executable
        cmd = ["test", "-x", "/var/lib/amd-metrics-exporter/amdgpuhealth"]
        ret_code, resp_stdout = k8_util.run_command_on_node(gpu_cluster, node_name, cmd)
        K8Helper.triage(environment, ret_code == 0, f"File /var/lib/amd-metrics-exporter/amdgpuhealth is not executable on {node_name}")
        Logger.debug(f"File executable check passed for /var/lib/amd-metrics-exporter/amdgpuhealth on {node_name}")

        # Verify the utility can be executed with help command
        cmd = ["/var/lib/amd-metrics-exporter/amdgpuhealth", "--help"]
        ret_code, resp_stdout = k8_util.run_command_on_node(gpu_cluster, node_name, cmd)
        K8Helper.triage(environment, ret_code == 0, f"amdgpuhealth utility failed to execute on {node_name}: {resp_stdout}")
        Logger.debug(f"amdgpuhealth execution test passed on {node_name}")

        # Note: amdgpuhealth query commands require endpoint environment variables
        # NPD configures these when running via custom plugin monitor

def test_npd_deployment(request, gpu_cluster, deviceconfig_install, deploy_npd_daemonset,
                        images, environment):
    """
    Verify NPD deploys successfully and can report a condition on GPU nodes.

    Deploys NPD with a gauge-metric condition (gpu_junction_temperature), verifies
    the DaemonSet rolls out, and asserts the condition appears as healthy on every
    GPU node. This validates the full NPD deployment path: namespace, RBAC,
    ConfigMap, DaemonSet, and node condition reporting.
    """
    global Logger

    condition_type = "AMDGPUDeploymentCheck"
    reason_healthy = "GPUTemperatureNormal"
    metric_name = "gpu_junction_temperature"

    def _cleanup():
        npd_util.remove_npd_amdgpuhealth_plugin(environment)
        npd_util.clear_node_condition(condition_type)

    request.addfinalizer(_cleanup)
    _cleanup()

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, len(gpu_nodes) > 0, "No nodes with AMD/GPU found in the cluster")

    # Use a very high threshold so condition stays healthy
    threshold = 999
    ret_code = npd_util.deploy_npd_custom_condition(
        environment, "gauge-metric", metric_name, threshold,
        condition_type, reason_healthy, "GPUTemperatureHigh",
        "GPU temperature is within normal range",
        "GPU junction temperature exceeds threshold",
        invoke_interval="15s"
    )
    K8Helper.triage(environment, ret_code == 0, "Failed to deploy NPD with deployment check condition")

    ds_ready = npd_util.wait_for_npd_daemonset_ready(npd_util.NPD_NAMESPACE, npd_util.NPD_APP_NAME, timeout=300)
    K8Helper.triage(environment, ds_ready, f"NPD DaemonSet '{npd_util.NPD_APP_NAME}' failed to rollout")

    condition_ok, failed_nodes = npd_util.verify_npd_node_condition(
        gpu_nodes, condition_type=condition_type,
        expected_status="False", expected_reason=reason_healthy,
        timeout=180, interval=15
    )
    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        condition = npd_util.get_node_condition(node_name, condition_type)
        if condition:
            Logger.info(f"Node {node_name}: {condition_type} status={condition['status']}, reason={condition['reason']}")

    K8Helper.triage(environment, condition_ok,
                    f"NPD deployment check failed — condition {condition_type} not healthy on nodes: {failed_nodes}")

@pytest.mark.parametrize("test_scenario", [
    {
        "name": "gfx_activity_threshold",
        "metric_type": "gauge-metric",
        "metric_name": "gpu_gfx_activity",
        "threshold": 5,
        "condition_type": "AMDGPUHighUtilization",
        "reason_healthy": "GPUUtilizationNormal",
        "reason_problem": "GPUUtilizationHigh",
        "message_healthy": "GPU utilization is within normal range",
        "message_problem": "GPU utilization exceeds threshold"
    },
    {
        "name": "junction_temp_threshold",
        "metric_type": "gauge-metric",
        "metric_name": "gpu_junction_temperature",
        "threshold": 43,
        "condition_type": "AMDGPUHighTemperature",
        "reason_healthy": "GPUTemperatureNormal",
        "reason_problem": "GPUTemperatureHigh",
        "message_healthy": "GPU temperature is within normal range",
        "message_problem": "GPU junction temperature exceeds threshold"
    },
    {
        "name": "vram_usage_threshold",
        "metric_type": "gauge-metric",
        "metric_name": "gpu_used_vram",
        "threshold": 50000,
        "condition_type": "AMDGPUHighMemoryUsage",
        "reason_healthy": "GPUMemoryUsageNormal",
        "reason_problem": "GPUMemoryUsageHigh",
        "message_healthy": "GPU memory usage is within normal range",
        "message_problem": "GPU VRAM usage exceeds threshold"
    }
])
def test_npd_multi_condition_workload(request, gpu_cluster, deviceconfig_install, deploy_npd_daemonset,
                                       test_scenario, images, environment):
    """
    Test NPD with multiple conditions that trigger based on workload state or hardware errors.

    This test validates that NPD can detect GPU health issues by monitoring both gauge
    and counter metrics. Each test scenario:
    1. Configures NPD with a specific metric threshold
    2. Verifies condition is healthy in idle state
    3. Starts a GPU workload
    4. Verifies condition status (may or may not trigger depending on metric type)
    5. Stops workload and verifies final condition state

    Test scenarios cover different metric types:

    Gauge Metrics (change with workload):
    - Activity/Utilization: GPU activity percentage
    - Temperature: Junction temperature in Celsius
    - Memory: VRAM usage in MB

    Counter Metrics (detect hardware errors):
    - ECC Errors: Uncorrectable/correctable error counts
    - PCIe Errors: Replay count errors

    Note: Counter metrics are cumulative and typically remain at 0 in healthy systems.
    They're included to demonstrate NPD's error detection capabilities, but may not
    trigger during normal workload execution.

    Parameters:
        test_scenario: Dictionary containing:
            - name: Test scenario name
            - metric_type: "gauge-metric" or "counter-metric"
            - metric_name: Prometheus metric name
            - threshold: Value above which condition triggers
            - condition_type: Kubernetes condition type
            - reason_healthy: Reason when metric is below threshold
            - reason_problem: Reason when metric exceeds threshold
            - message_healthy: Message for healthy state
            - message_problem: Message for problem state

    Dependencies:
        - deviceconfig_install: Provides metrics-exporter with amdgpuhealth binary
        - deploy_npd_daemonset: Deploys base NPD DaemonSet with RBAC
        - images: Container images for workload pods

    Validates:
        - NPD DaemonSet rollout succeeds
        - Condition exists on all GPU nodes
        - Condition status reflects metric values
        - Test logs condition state at idle, load, and recovery phases
    """
    global Logger

    def _cleanup_npd_config():
        npd_util.remove_npd_amdgpuhealth_plugin(environment)
        npd_util.clear_node_condition(condition_type)

    def _cleanup_workloads():
        for ctxt in workload_contexts:
            K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **ctxt)

    request.addfinalizer(_cleanup_npd_config)
    request.addfinalizer(_cleanup_workloads)

    workload_contexts = []

    # Extract scenario parameters
    scenario_name = test_scenario["name"]
    metric_type = test_scenario["metric_type"]
    metric_name = test_scenario["metric_name"]
    threshold = test_scenario["threshold"]
    condition_type = test_scenario["condition_type"]
    reason_healthy = test_scenario["reason_healthy"]
    reason_problem = test_scenario["reason_problem"]
    message_healthy = test_scenario["message_healthy"]
    message_problem = test_scenario["message_problem"]

    Logger.info(f"Testing NPD condition: {condition_type} for metric {metric_name} with threshold {threshold}")

    # Clean up any existing NPD configuration
    _cleanup_npd_config()

    # Get GPU nodes
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")

    # Sample idle metric over 90s and use the peak to set a stable threshold.
    # A single-point reading can catch a momentary dip and set the threshold too low.
    Logger.info(f"Sampling idle {metric_name} over 90s to establish stable baseline")
    idle_samples = []
    for _ in range(6):
        v = query_gpu_metric_value(environment.gpu_operator_namespace, metric_name, timeout=30, interval=5)
        if v is not None:
            idle_samples.append(v)
        time.sleep(15)
    K8Helper.triage(environment, len(idle_samples) > 0,
                    f"Failed to read {metric_name} from metrics exporter — exporter pods may not be ready")
    idle_max = max(idle_samples)
    threshold = idle_max + max(idle_max * 0.30, 10)
    Logger.info(f"Idle {metric_name}: samples={idle_samples}, max={idle_max}, dynamic threshold={threshold}")

    # Deploy NPD with custom condition
    ret_code = npd_util.deploy_npd_custom_condition(
        environment, metric_type, metric_name, threshold,
        condition_type, reason_healthy, reason_problem,
        message_healthy, message_problem,
        invoke_interval="15s"
    )
    K8Helper.triage(environment, (ret_code == 0), f"Failed to deploy NPD with custom condition {condition_type}")

    # Wait for NPD DaemonSet rollout
    Logger.info(f"Waiting for NPD DaemonSet '{npd_util.NPD_APP_NAME}' to be ready")
    ds_ready = npd_util.wait_for_npd_daemonset_ready(npd_util.NPD_NAMESPACE, npd_util.NPD_APP_NAME, timeout=300)
    K8Helper.triage(environment, ds_ready, f"NPD DaemonSet '{npd_util.NPD_APP_NAME}' failed to rollout")

    # Phase 1: Verify condition is healthy at idle
    Logger.info(f"Phase 1: Verifying {condition_type} is healthy at idle (threshold={threshold})")
    condition_ok, failed_nodes = npd_util.verify_npd_node_condition(
        gpu_nodes, condition_type=condition_type,
        expected_status="False", expected_reason=reason_healthy,
        timeout=180, interval=15
    )
    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        condition = npd_util.get_node_condition(node_name, condition_type)
        if condition:
            Logger.info(f"IDLE - {node_name}: status={condition['status']}, reason={condition['reason']}")
    K8Helper.triage(environment, condition_ok,
                    f"Phase 1 FAILED: {condition_type} not healthy at idle on nodes: {failed_nodes}")

    # Phase 2: Start GPU workload
    Logger.info("Phase 2: Starting GPU workload on all nodes")
    for node in gpu_nodes:
        node_ip = k8_util.k8_get_node_address(node)
        cluster_node = gpu_cluster.find_node_by_ip(node_ip)
        if not cluster_node:
            pytest.fail(f"Unable to get worker node from cluster for ip: {node_ip}")

        node_name = k8_util.k8_get_node_hostname(node)
        gpu_cap, gpu_alloc = k8_util.k8_get_node_gpu_capacity(node_name)

        params = {
            "node_name": node_name,
            "images": images,
            "num_gpu_reqd": gpu_cap,
            "workload_selection": "rocm-hip-gfx-stress",
        }
        workload_ctxt = K8Helper.workload_operation(environment, K8Helper.WorkloadOp.START_WORKLOAD, **params)
        K8Helper.triage(environment, (workload_ctxt['podStatus'] == K8Helper.PodStatus.RUNNING),
                        f"Workload failed to start on node {node_name}: {workload_ctxt}")
        workload_contexts.append(workload_ctxt)
        Logger.info(f"Started workload on node {node_name}")

    # Wait for HIP stress workload to compile (~10-20s) and start GPU compute.
    Logger.info("Waiting 30s for HIP stress workload to compile and start GPU compute")
    time.sleep(30)

    # Phase 3: Verify condition triggers under load
    Logger.info(f"Phase 3: Verifying {condition_type} triggers under load")
    condition_ok, failed_nodes = npd_util.verify_npd_node_condition(
        gpu_nodes, condition_type=condition_type,
        expected_status="True", expected_reason=reason_problem,
        timeout=300, interval=15
    )
    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        condition = npd_util.get_node_condition(node_name, condition_type)
        if condition:
            Logger.info(f"LOAD - {node_name}: status={condition['status']}, reason={condition['reason']}")
    K8Helper.triage(environment, condition_ok,
                    f"Phase 3 FAILED: {condition_type} did not trigger under load on nodes: {failed_nodes}")

    # Phase 4: Stop workload
    Logger.info("Phase 4: Stopping GPU workload")
    for ctxt in workload_contexts:
        K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **ctxt)
    workload_contexts.clear()

    # Temperature needs longer to settle than utilization due to GPU thermal mass.
    cooldown = 180 if "temperature" in metric_name else 90
    Logger.info(f"Waiting {cooldown}s for {metric_name} to return to idle state")
    time.sleep(cooldown)

    # Phase 5: Verify condition recovers after workload stops
    Logger.info(f"Phase 5: Verifying {condition_type} recovers after workload stops")
    condition_ok, failed_nodes = npd_util.verify_npd_node_condition(
        gpu_nodes, condition_type=condition_type,
        expected_status="False", expected_reason=reason_healthy,
        timeout=480, interval=15
    )
    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        condition = npd_util.get_node_condition(node_name, condition_type)
        if condition:
            Logger.info(f"RECOVERY - {node_name}: status={condition['status']}, reason={condition['reason']}")
    K8Helper.triage(environment, condition_ok,
                    f"Phase 5 FAILED: {condition_type} did not recover after workload stop on nodes: {failed_nodes}")


@pytest.mark.parametrize("test_scenario", [
    {
        "name": "ecc_uncorrectable_errors",
        "metric_type": "counter-metric",
        "metric_name": "gpu_ecc_uncorrect_total",
        "threshold": 1,
        "condition_type": "AMDGPUUncorrectableECC",
        "reason_healthy": "NoUncorrectableECCErrors",
        "reason_problem": "UncorrectableECCErrorDetected",
        "message_healthy": "No uncorrectable ECC errors detected",
        "message_problem": "Uncorrectable ECC errors detected - potential hardware failure"
    },
    {
        "name": "ecc_correctable_umc_errors",
        "metric_type": "counter-metric",
        "metric_name": "gpu_ecc_correct_umc",
        "threshold": 100,
        "condition_type": "AMDGPUCorrectableECCUMC",
        "reason_healthy": "CorrectableECCWithinLimits",
        "reason_problem": "ExcessiveCorrectableECCErrors",
        "message_healthy": "Correctable ECC errors in UMC within acceptable limits",
        "message_problem": "Excessive correctable ECC errors in UMC - monitor for hardware degradation"
    }
])
@pytest.mark.level2
def test_npd_ecc_counter_monitor(request, gpu_cluster, deviceconfig_install, deploy_npd_daemonset,
                                  test_scenario, images, environment):
    """
    Test NPD monitoring of ECC error counters on healthy hardware.

    ECC counters are cumulative hardware error counts independent of GPU utilization.
    On healthy hardware, counters remain at 0 and NPD reports the condition as healthy.
    No workload is started since ECC errors do not correlate with GPU activity.

    Validates:
        - NPD DaemonSet deploys and rolls out
        - Counter-metric condition is created on all GPU nodes
        - Condition status is False (healthy) on hardware with no ECC errors
    """
    global Logger

    scenario_name = test_scenario["name"]
    metric_type = test_scenario["metric_type"]
    metric_name = test_scenario["metric_name"]
    threshold = test_scenario["threshold"]
    condition_type = test_scenario["condition_type"]
    reason_healthy = test_scenario["reason_healthy"]
    reason_problem = test_scenario["reason_problem"]
    message_healthy = test_scenario["message_healthy"]
    message_problem = test_scenario["message_problem"]

    Logger.info(f"Testing ECC counter monitor: {condition_type} for metric {metric_name} with threshold {threshold}")

    def _cleanup():
        npd_util.remove_npd_amdgpuhealth_plugin(environment)
        npd_util.clear_node_condition(condition_type)

    request.addfinalizer(_cleanup)

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")

    npd_util.clear_node_condition(condition_type)

    ret_code = npd_util.deploy_npd_custom_condition(
        environment, metric_type, metric_name, threshold,
        condition_type, reason_healthy, reason_problem,
        message_healthy, message_problem,
        invoke_interval="15s"
    )
    K8Helper.triage(environment, (ret_code == 0), f"Failed to deploy NPD with ECC condition {condition_type}")

    ds_ready = npd_util.wait_for_npd_daemonset_ready(npd_util.NPD_NAMESPACE, npd_util.NPD_APP_NAME, timeout=300)
    K8Helper.triage(environment, ds_ready, f"NPD DaemonSet '{npd_util.NPD_APP_NAME}' failed to rollout")

    # Verify condition is healthy (ECC count = 0 < threshold on healthy hardware)
    condition_ok, failed_nodes = npd_util.verify_npd_node_condition(
        gpu_nodes, condition_type=condition_type,
        expected_status="False", expected_reason=reason_healthy,
        timeout=180, interval=15
    )
    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        condition = npd_util.get_node_condition(node_name, condition_type)
        if condition:
            Logger.info(f"Node {node_name}: {condition_type} status={condition['status']}, reason={condition['reason']}")

    K8Helper.triage(environment, condition_ok,
                    f"ECC condition {condition_type} not healthy on nodes: {failed_nodes}. "
                    f"Expected status=False on hardware with no ECC errors.")

    Logger.info(f"ECC counter test '{scenario_name}' passed: condition is healthy on all nodes")


@pytest.mark.level2
def test_npd_amdgpuhealth_metrics_field_prefix(request, gpu_cluster, deviceconfig_install, deploy_npd_daemonset,
                                                images, environment):
    """
    Test that amdgpuhealth resolves metrics regardless of MetricsFieldPrefix.

    amdgpuhealth queries GPU metrics via the exporter's HTTP endpoints (/metrics,
    /gpumetrics). When MetricsFieldPrefix is configured, the exporter prefixes
    exported metric names. amdgpuhealth must still resolve the actual metric name
    (without prefix) because it queries the gRPC socket for metric discovery.

    Phase 1: Baseline — actual metric name works with default DME config
    Phase 2: Apply custom MetricsFieldPrefix, actual metric name must still work
    """
    global Logger

    METRIC_NAME = "gpu_junction_temperature"
    CUSTOM_PREFIX = "custom_"
    THRESHOLD = 200  # High threshold — won't trigger, testing metric resolution only
    CONDITION_TYPE = "AMDGPUPrefixTest"
    REASON_HEALTHY = "PrefixTestHealthy"
    REASON_PROBLEM = "PrefixTestProblem"
    MESSAGE_HEALTHY = "Prefix test - GPU metric query returned valid data"
    MESSAGE_PROBLEM = "Prefix test - GPU metric query failed"
    DME_CONFIGMAP_NAME = "dme-prefix-test-config"

    def _cleanup():
        npd_util.remove_npd_amdgpuhealth_plugin(environment)
        npd_util.clear_node_condition(CONDITION_TYPE)
        k8_util.k8_delete_configmap(environment.gpu_operator_namespace, DME_CONFIGMAP_NAME)
        for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
            if tcfg.get('metricsExporter.config'):
                del tcfg['metricsExporter.config']
            cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
            ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
            if ret_code != 0:
                Logger.warning(f"Cleanup: failed to restore deviceconfig: {ret_stderr}")
        K8Helper.check_deviceconfig_status(environment, deviceconfig_install.devicecfg_list)

    request.addfinalizer(_cleanup)

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error getting GPU nodes from cluster")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No AMD GPU nodes found in cluster")

    npd_util.clear_node_condition(CONDITION_TYPE)

    # ── Phase 1: Baseline — default DME config, actual metric name ──
    Logger.info(f"Phase 1: Baseline — default DME config with actual metric name '{METRIC_NAME}'")

    ret_code = npd_util.deploy_npd_custom_condition(
        environment, "gauge-metric", METRIC_NAME, THRESHOLD,
        CONDITION_TYPE, REASON_HEALTHY, REASON_PROBLEM,
        MESSAGE_HEALTHY, MESSAGE_PROBLEM, invoke_interval="15s"
    )
    K8Helper.triage(environment, (ret_code == 0), "Failed to deploy NPD for Phase 1")

    ds_ready = npd_util.wait_for_npd_daemonset_ready(npd_util.NPD_NAMESPACE, npd_util.NPD_APP_NAME, timeout=300)
    K8Helper.triage(environment, ds_ready, "NPD DaemonSet failed to rollout for Phase 1")

    condition_ok, failed_nodes = npd_util.verify_npd_node_condition(
        gpu_nodes, condition_type=CONDITION_TYPE,
        expected_status="False", expected_reason=REASON_HEALTHY,
        timeout=180, interval=15
    )
    K8Helper.triage(environment, condition_ok,
                    f"Phase 1 FAILED: amdgpuhealth could not resolve actual metric name '{METRIC_NAME}' "
                    f"on nodes: {failed_nodes}")
    Logger.info("Phase 1 PASSED: amdgpuhealth resolves actual metric name with default DME config")

    npd_util.remove_npd_amdgpuhealth_plugin(environment)
    time.sleep(10)

    # ── Apply custom MetricsFieldPrefix to DME ──
    Logger.info(f"Applying custom DME config with MetricsFieldPrefix='{CUSTOM_PREFIX}'")

    dme_config = {
        "CommonConfig": {
            "MetricsFieldPrefix": CUSTOM_PREFIX
        },
        "GPUConfig": {
            "Fields": [METRIC_NAME]
        }
    }
    dme_config_file = os.path.join(environment.logdir, f"{DME_CONFIGMAP_NAME}.json")
    with open(dme_config_file, "w") as fp:
        fp.write(json.dumps(dme_config, indent=4))

    k8_util.k8_delete_configmap(environment.gpu_operator_namespace, DME_CONFIGMAP_NAME)
    ret_code, ret_stdout, ret_stderr = k8_util.k8_create_configmap(
        environment.gpu_operator_namespace, DME_CONFIGMAP_NAME,
        dme_config_file, "config.json"
    )
    K8Helper.triage(environment, (ret_code == 0),
                    f"Failed to create DME config ConfigMap: {ret_stderr}")

    devicecfg_pods = [
        common.PodInfo('metrics-exporter', len(gpu_nodes), 1),
    ]
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['metricsExporter.config'] = DME_CONFIGMAP_NAME
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, (ret_code == 0),
                        f"Failed to update DeviceConfig with DME config: {ret_stderr}")

    K8Helper.check_deviceconfig_status(environment, deviceconfig_install.devicecfg_list)
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods)
    K8Helper.triage(environment, not failed_pods, f"Exporter pods not ready after config update: {failed_pods}")
    Logger.info("Waiting 30s for exporter to reload with custom prefix config")
    time.sleep(30)

    # ── Phase 2: Custom prefix — actual metric name must still work ──
    Logger.info(f"Phase 2: Custom MetricsFieldPrefix='{CUSTOM_PREFIX}' — actual metric name '{METRIC_NAME}'")

    ret_code = npd_util.deploy_npd_custom_condition(
        environment, "gauge-metric", METRIC_NAME, THRESHOLD,
        CONDITION_TYPE, REASON_HEALTHY, REASON_PROBLEM,
        MESSAGE_HEALTHY, MESSAGE_PROBLEM, invoke_interval="15s"
    )
    K8Helper.triage(environment, (ret_code == 0), "Failed to deploy NPD for Phase 2")

    ds_ready = npd_util.wait_for_npd_daemonset_ready(npd_util.NPD_NAMESPACE, npd_util.NPD_APP_NAME, timeout=300)
    K8Helper.triage(environment, ds_ready, "NPD DaemonSet failed to rollout for Phase 2")

    condition_ok, failed_nodes = npd_util.verify_npd_node_condition(
        gpu_nodes, condition_type=CONDITION_TYPE,
        expected_status="False", expected_reason=REASON_HEALTHY,
        timeout=180, interval=15
    )
    K8Helper.triage(environment, condition_ok,
                    f"Phase 2 FAILED: amdgpuhealth could not resolve actual metric name '{METRIC_NAME}' "
                    f"with MetricsFieldPrefix='{CUSTOM_PREFIX}' on nodes: {failed_nodes}. "
                    f"amdgpuhealth must query by actual metric name, not export-prefixed name.")
    Logger.info(f"Phase 2 PASSED: actual metric name '{METRIC_NAME}' works regardless of MetricsFieldPrefix")


@pytest.mark.level2
def test_npd_amdgpuhealth_invalid_metric_name(request, gpu_cluster, deviceconfig_install, deploy_npd_daemonset,
                                               images, environment):
    """
    Regression test: amdgpuhealth returns exit code 2 for invalid metric names, causing NPD to
    set the condition to Unknown (not True/False).

    NPD plugin exit code contract (docs/npd/node-problem-detector.md):
      0 → condition False (healthy)
      1 → condition True  (problem detected, triggers remediation)
      2 → condition Unknown (error/indeterminate — no remediation triggered)

    Phase 1: Baseline — valid metric name resolves, condition is False (healthy).
    Phase 2: Invalid metric name (typo) — amdgpuhealth exits 2, NPD sets condition Unknown.
    """
    global Logger

    VALID_METRIC = "gpu_used_vram"
    INVALID_METRIC = "gpu_user_vram"  # Typo: 'user' instead of 'used'
    THRESHOLD = 999999  # Very high threshold — won't trigger on valid metric
    CONDITION_TYPE = "AMDGPUInvalidMetricTest"
    REASON_HEALTHY = "MetricQueryOK"
    REASON_PROBLEM = "MetricQueryFailed"
    MESSAGE_HEALTHY = "GPU metric query returned valid data"
    MESSAGE_PROBLEM = "GPU metric query failed"

    def _cleanup():
        npd_util.remove_npd_amdgpuhealth_plugin(environment)
        npd_util.clear_node_condition(CONDITION_TYPE)

    request.addfinalizer(_cleanup)

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error getting GPU nodes from cluster")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No AMD GPU nodes found in cluster")

    npd_util.clear_node_condition(CONDITION_TYPE)

    # ── Phase 1: Baseline — valid metric name ──
    Logger.info(f"Phase 1: Baseline — valid metric name '{VALID_METRIC}'")

    ret_code = npd_util.deploy_npd_custom_condition(
        environment, "gauge-metric", VALID_METRIC, THRESHOLD,
        CONDITION_TYPE, REASON_HEALTHY, REASON_PROBLEM,
        MESSAGE_HEALTHY, MESSAGE_PROBLEM, invoke_interval="15s"
    )
    K8Helper.triage(environment, (ret_code == 0), "Failed to deploy NPD for Phase 1")

    ds_ready = npd_util.wait_for_npd_daemonset_ready(npd_util.NPD_NAMESPACE, npd_util.NPD_APP_NAME, timeout=300)
    K8Helper.triage(environment, ds_ready, "NPD DaemonSet failed to rollout for Phase 1")

    condition_ok, failed_nodes = npd_util.verify_npd_node_condition(
        gpu_nodes, condition_type=CONDITION_TYPE,
        expected_status="False", expected_reason=REASON_HEALTHY,
        timeout=180, interval=15
    )
    K8Helper.triage(environment, condition_ok,
                    f"Phase 1 FAILED: valid metric '{VALID_METRIC}' not resolved on nodes: {failed_nodes}")
    Logger.info(f"Phase 1 PASSED: valid metric '{VALID_METRIC}' resolves correctly")

    npd_util.remove_npd_amdgpuhealth_plugin(environment)
    time.sleep(10)

    # ── Phase 2: Invalid metric name (typo) ──
    Logger.info(f"Phase 2: Invalid metric name '{INVALID_METRIC}' (typo of '{VALID_METRIC}')")

    ret_code = npd_util.deploy_npd_custom_condition(
        environment, "gauge-metric", INVALID_METRIC, THRESHOLD,
        CONDITION_TYPE, REASON_HEALTHY, REASON_PROBLEM,
        MESSAGE_HEALTHY, MESSAGE_PROBLEM, invoke_interval="15s"
    )
    K8Helper.triage(environment, (ret_code == 0), "Failed to deploy NPD for Phase 2")

    ds_ready = npd_util.wait_for_npd_daemonset_ready(npd_util.NPD_NAMESPACE, npd_util.NPD_APP_NAME, timeout=300)
    K8Helper.triage(environment, ds_ready, "NPD DaemonSet failed to rollout for Phase 2")

    # amdgpuhealth exits 2 for invalid metric → NPD sets condition Unknown (not True/False).
    # Reason is NPD-internal for Unknown; only check the status.
    condition_ok, failed_nodes = npd_util.verify_npd_node_condition(
        gpu_nodes, condition_type=CONDITION_TYPE,
        expected_status="Unknown", expected_reason=None,
        timeout=180, interval=15
    )

    # Log actual condition state for diagnostics
    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        condition = npd_util.get_node_condition(node_name, CONDITION_TYPE)
        if condition:
            Logger.info(f"Node {node_name}: {CONDITION_TYPE} status={condition['status']}, "
                        f"reason={condition['reason']}, message={condition['message']}")

    K8Helper.triage(environment, condition_ok,
                    f"Phase 2 FAILED: expected NPD condition Unknown for invalid metric '{INVALID_METRIC}' "
                    f"(amdgpuhealth exit 2), but got non-Unknown on nodes: {failed_nodes}")

    Logger.info(f"Phase 2 PASSED: amdgpuhealth correctly exited 2 for invalid metric "
                f"'{INVALID_METRIC}', NPD set condition Unknown")


@pytest.mark.level2
def test_npd_amdgpuhealth_exporter_bearer_token(request, gpu_cluster, deviceconfig_install, deploy_npd_daemonset,
                                                  images, environment):
    """
    Test NPD amdgpuhealth with bearer token authentication to the metrics exporter.

    When the metrics exporter has RBAC enabled with HTTP mode (kube-rbac-proxy sidecar
    using --insecure-listen-address), amdgpuhealth must present a valid bearer token to
    query metrics. This test uses disableHttps=True so the proxy serves over HTTP,
    isolating the bearer token auth mechanism without TLS complexity.

    1. Enables RBAC on the metrics exporter with disableHttps=True (HTTP mode)
    2. Creates a ServiceAccount + ClusterRole + ClusterRoleBinding with GET /metrics and /gpumetrics permission
    3. Generates a bearer token for the ServiceAccount
    4. Stores the token in a Kubernetes Secret in the NPD namespace
    5. Deploys NPD with auth config pointing to the token Secret
    6. Verifies NPD can query metrics through kube-rbac-proxy and reports healthy condition
    """
    global Logger

    METRIC_NAME = "gpu_junction_temperature"
    THRESHOLD = 5  # Low threshold so actual temp (~40) triggers condition
    CONDITION_TYPE = "AMDGPUBearerTokenTest"
    REASON_HEALTHY = "BearerTokenQueryOK"
    REASON_PROBLEM = "BearerTokenQueryFailed"
    MESSAGE_HEALTHY = "GPU metric query with bearer token succeeded"
    MESSAGE_PROBLEM = "GPU metric query with bearer token failed"

    AUTH_SA_NS = "metrics-reader"
    AUTH_SA_NAME = "npd-exporter-client"
    AUTH_CR_NAME = "npd-metrics-reader"
    AUTH_CRB_NAME = "npd-metrics-reader"
    AUTH_SECRET_NAME = "npd-exporter-bearer-token"

    def _cleanup():
        npd_util.remove_npd_amdgpuhealth_plugin(environment)
        npd_util.clear_node_condition(CONDITION_TYPE)
        k8_util.k8_delete_secret(AUTH_SECRET_NAME, "generic", npd_util.NPD_NAMESPACE)
        k8_util.k8_delete_cluster_role_binding(AUTH_CRB_NAME)
        k8_util.k8_delete_cluster_role(AUTH_CR_NAME)
        k8_util.k8_delete_service_account(AUTH_SA_NAME, AUTH_SA_NS)
        for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
            tcfg['metricsExporter.enable'] = True
            tcfg['metricsExporter.serviceType'] = 'NodePort'
            tcfg['metricsExporter.rbacConfig.enable'] = False
            tcfg['metricsExporter.rbacConfig.disableHttps'] = False
            cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
            k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.check_deviceconfig_status(environment, deviceconfig_install.devicecfg_list)
        for devcfg in deviceconfig_install.devicecfg_list:
            K8Helper.wait_kmm_worker_completion(environment, devcfg)

    request.addfinalizer(_cleanup)

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error getting GPU nodes")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No AMD GPU nodes found")

    npd_util.clear_node_condition(CONDITION_TYPE)

    # Step 1: Enable RBAC on metrics exporter with HTTP mode
    Logger.info("Enabling RBAC on metrics exporter with disableHttps=True (HTTP mode)")
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['metricsExporter.rbacConfig.enable'] = True
        tcfg['metricsExporter.rbacConfig.disableHttps'] = True
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, (ret_code == 0), f"Failed to enable RBAC on DeviceConfig: {ret_stderr}")

    K8Helper.check_deviceconfig_status(environment, deviceconfig_install.devicecfg_list)
    for devcfg in deviceconfig_install.devicecfg_list:
        K8Helper.wait_kmm_worker_completion(environment, devcfg)

    devicecfg_pods = [
        common.PodInfo('device-plugin', len(gpu_nodes), 1),
        common.PodInfo('metrics-exporter', len(gpu_nodes), 2),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods)
    K8Helper.triage(environment, not failed_pods, f"Exporter pods not ready after RBAC enable: {failed_pods}")

    # Step 2: Create SA + ClusterRole + ClusterRoleBinding
    Logger.info("Creating ServiceAccount and RBAC for bearer token auth")
    k8_util.k8_delete_cluster_role_binding(AUTH_CRB_NAME)
    k8_util.k8_delete_cluster_role(AUTH_CR_NAME)
    k8_util.k8_delete_service_account(AUTH_SA_NAME, AUTH_SA_NS)

    ret_code, _, _ = k8_util.k8_create_namespace(AUTH_SA_NS)

    ret_code, _, ret_stderr = k8_util.k8_create_service_account(AUTH_SA_NAME, AUTH_SA_NS)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to create SA: {ret_stderr}")

    ret_code, _, ret_stderr = k8_util.k8_create_cluster_role(
        AUTH_CR_NAME, k8_util.k8_create_rules_from_endpoint_list([("/metrics", "get"), ("/gpumetrics", "get")]))
    K8Helper.triage(environment, (ret_code == 0), f"Failed to create ClusterRole: {ret_stderr}")

    ret_code, _, ret_stderr = k8_util.k8_create_role_binding(AUTH_CRB_NAME, AUTH_SA_NS, AUTH_CR_NAME, AUTH_SA_NAME)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to create ClusterRoleBinding: {ret_stderr}")

    # Step 3: Generate bearer token
    token = k8_util.k8_create_token(AUTH_SA_NS, AUTH_SA_NAME, "1h")
    K8Helper.triage(environment, (token is not None), f"Failed to create token for SA {AUTH_SA_NAME}")
    Logger.info(f"Bearer token created (length={len(token)})")

    # Step 4: Store token in Secret in NPD namespace
    k8_util.k8_delete_secret(AUTH_SECRET_NAME, "generic", npd_util.NPD_NAMESPACE)
    ret_code, _, ret_stderr = k8_util.k8_create_secret(
        AUTH_SECRET_NAME, "generic", token=token, namespace=npd_util.NPD_NAMESPACE)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to create bearer token Secret: {ret_stderr}")

    time.sleep(10)

    # Step 5: Deploy NPD with bearer token auth
    Logger.info("Deploying NPD with exporter bearer token auth")
    ret_code = npd_util.deploy_npd_custom_condition(
        environment, "gauge-metric", METRIC_NAME, THRESHOLD,
        CONDITION_TYPE, REASON_HEALTHY, REASON_PROBLEM,
        MESSAGE_HEALTHY, MESSAGE_PROBLEM, invoke_interval="15s",
        auth={"exporter_bearer_token_secret": AUTH_SECRET_NAME}
    )
    K8Helper.triage(environment, (ret_code == 0), "Failed to deploy NPD with bearer token auth")

    ds_ready = npd_util.wait_for_npd_daemonset_ready(npd_util.NPD_NAMESPACE, npd_util.NPD_APP_NAME, timeout=300)
    K8Helper.triage(environment, ds_ready, "NPD DaemonSet failed to rollout with bearer token auth")

    # Step 6: Verify condition is True (problem) — proves amdgpuhealth read the metric
    # value through auth and compared it against the low threshold
    condition_ok, failed_nodes = npd_util.verify_npd_node_condition(
        gpu_nodes, condition_type=CONDITION_TYPE,
        expected_status="True", expected_reason=REASON_PROBLEM,
        timeout=180, interval=15
    )

    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        condition = npd_util.get_node_condition(node_name, CONDITION_TYPE)
        if condition:
            Logger.info(f"Node {node_name}: {CONDITION_TYPE} status={condition['status']}, "
                        f"reason={condition['reason']}, message={condition['message']}")

    K8Helper.triage(environment, condition_ok,
                    f"Bearer token auth test FAILED: amdgpuhealth could not query metrics through "
                    f"kube-rbac-proxy with bearer token on nodes: {failed_nodes}")

    Logger.info("Bearer token auth test PASSED: amdgpuhealth queried RBAC-protected exporter successfully")


@pytest.mark.level2
def test_npd_amdgpuhealth_exporter_rbac_http(request, gpu_cluster, deviceconfig_install, deploy_npd_daemonset,
                                               images, environment):
    """
    Test NPD amdgpuhealth with bearer token auth over HTTP (no TLS).

    When the metrics exporter has RBAC enabled with disableHttps=True, kube-rbac-proxy
    fronts the exporter but serves over HTTP instead of HTTPS. amdgpuhealth must still
    present a valid bearer token for authorization, but no TLS certificates are needed.

    This covers the deployment scenario where TLS termination is handled externally
    (e.g., by a service mesh or ingress) and the exporter uses plain HTTP internally.

    1. Enables RBAC on the metrics exporter with disableHttps=True
    2. Creates a ServiceAccount + ClusterRole + ClusterRoleBinding with GET /metrics and /gpumetrics permission
    3. Generates a bearer token for the ServiceAccount
    4. Stores the token in a Kubernetes Secret in the NPD namespace
    5. Deploys NPD with bearer token auth (no root CA or client cert)
    6. Verifies NPD can query metrics through kube-rbac-proxy over HTTP
    """
    global Logger

    METRIC_NAME = "gpu_junction_temperature"
    THRESHOLD = 5  # Low threshold so actual temp (~40) triggers condition
    CONDITION_TYPE = "AMDGPURbacHttpTest"
    REASON_HEALTHY = "RbacHttpQueryOK"
    REASON_PROBLEM = "RbacHttpQueryFailed"
    MESSAGE_HEALTHY = "GPU metric query with RBAC over HTTP succeeded"
    MESSAGE_PROBLEM = "GPU metric query with RBAC over HTTP failed"

    AUTH_SA_NS = "metrics-reader"
    AUTH_SA_NAME = "npd-rbac-http-client"
    AUTH_CR_NAME = "npd-rbac-http-metrics"
    AUTH_CRB_NAME = "npd-rbac-http-metrics"
    AUTH_SECRET_NAME = "npd-rbac-http-bearer-token"

    def _cleanup():
        npd_util.remove_npd_amdgpuhealth_plugin(environment)
        npd_util.clear_node_condition(CONDITION_TYPE)
        k8_util.k8_delete_secret(AUTH_SECRET_NAME, "generic", npd_util.NPD_NAMESPACE)
        k8_util.k8_delete_cluster_role_binding(AUTH_CRB_NAME)
        k8_util.k8_delete_cluster_role(AUTH_CR_NAME)
        k8_util.k8_delete_service_account(AUTH_SA_NAME, AUTH_SA_NS)
        for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
            tcfg['metricsExporter.enable'] = True
            tcfg['metricsExporter.serviceType'] = 'NodePort'
            tcfg['metricsExporter.rbacConfig.enable'] = False
            tcfg['metricsExporter.rbacConfig.disableHttps'] = False
            cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
            k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.check_deviceconfig_status(environment, deviceconfig_install.devicecfg_list)
        for devcfg in deviceconfig_install.devicecfg_list:
            K8Helper.wait_kmm_worker_completion(environment, devcfg)

    request.addfinalizer(_cleanup)

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error getting GPU nodes")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No AMD GPU nodes found")

    npd_util.clear_node_condition(CONDITION_TYPE)

    # Step 1: Enable RBAC on metrics exporter with HTTP (no TLS)
    Logger.info("Enabling RBAC on metrics exporter with disableHttps=True (HTTP mode)")
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['metricsExporter.rbacConfig.enable'] = True
        tcfg['metricsExporter.rbacConfig.disableHttps'] = True
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, (ret_code == 0), f"Failed to enable RBAC HTTP on DeviceConfig: {ret_stderr}")

    K8Helper.check_deviceconfig_status(environment, deviceconfig_install.devicecfg_list)
    for devcfg in deviceconfig_install.devicecfg_list:
        K8Helper.wait_kmm_worker_completion(environment, devcfg)

    devicecfg_pods = [
        common.PodInfo('device-plugin', len(gpu_nodes), 1),
        common.PodInfo('metrics-exporter', len(gpu_nodes), 2),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods)
    K8Helper.triage(environment, not failed_pods, f"Exporter pods not ready after RBAC HTTP enable: {failed_pods}")

    # Step 2: Create SA + ClusterRole + ClusterRoleBinding
    Logger.info("Creating ServiceAccount and RBAC for HTTP bearer token auth")
    k8_util.k8_delete_cluster_role_binding(AUTH_CRB_NAME)
    k8_util.k8_delete_cluster_role(AUTH_CR_NAME)
    k8_util.k8_delete_service_account(AUTH_SA_NAME, AUTH_SA_NS)

    ret_code, _, _ = k8_util.k8_create_namespace(AUTH_SA_NS)

    ret_code, _, ret_stderr = k8_util.k8_create_service_account(AUTH_SA_NAME, AUTH_SA_NS)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to create SA: {ret_stderr}")

    ret_code, _, ret_stderr = k8_util.k8_create_cluster_role(
        AUTH_CR_NAME, k8_util.k8_create_rules_from_endpoint_list([("/metrics", "get"), ("/gpumetrics", "get")]))
    K8Helper.triage(environment, (ret_code == 0), f"Failed to create ClusterRole: {ret_stderr}")

    ret_code, _, ret_stderr = k8_util.k8_create_role_binding(AUTH_CRB_NAME, AUTH_SA_NS, AUTH_CR_NAME, AUTH_SA_NAME)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to create ClusterRoleBinding: {ret_stderr}")

    # Step 3: Generate bearer token
    token = k8_util.k8_create_token(AUTH_SA_NS, AUTH_SA_NAME, "1h")
    K8Helper.triage(environment, (token is not None), f"Failed to create token for SA {AUTH_SA_NAME}")
    Logger.info(f"Bearer token created (length={len(token)})")

    # Step 4: Store token in Secret in NPD namespace
    k8_util.k8_delete_secret(AUTH_SECRET_NAME, "generic", npd_util.NPD_NAMESPACE)
    ret_code, _, ret_stderr = k8_util.k8_create_secret(
        AUTH_SECRET_NAME, "generic", token=token, namespace=npd_util.NPD_NAMESPACE)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to create bearer token Secret: {ret_stderr}")

    time.sleep(10)

    # Step 5: Deploy NPD with bearer token auth (no TLS certs needed for HTTP)
    Logger.info("Deploying NPD with bearer token auth over HTTP")
    ret_code = npd_util.deploy_npd_custom_condition(
        environment, "gauge-metric", METRIC_NAME, THRESHOLD,
        CONDITION_TYPE, REASON_HEALTHY, REASON_PROBLEM,
        MESSAGE_HEALTHY, MESSAGE_PROBLEM, invoke_interval="15s",
        auth={"exporter_bearer_token_secret": AUTH_SECRET_NAME}
    )
    K8Helper.triage(environment, (ret_code == 0), "Failed to deploy NPD with RBAC HTTP auth")

    ds_ready = npd_util.wait_for_npd_daemonset_ready(npd_util.NPD_NAMESPACE, npd_util.NPD_APP_NAME, timeout=300)
    K8Helper.triage(environment, ds_ready, "NPD DaemonSet failed to rollout with RBAC HTTP auth")

    # Step 6: Verify condition is True (problem) — proves metric was read through auth
    condition_ok, failed_nodes = npd_util.verify_npd_node_condition(
        gpu_nodes, condition_type=CONDITION_TYPE,
        expected_status="True", expected_reason=REASON_PROBLEM,
        timeout=300, interval=15
    )

    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        condition = npd_util.get_node_condition(node_name, CONDITION_TYPE)
        if condition:
            Logger.info(f"Node {node_name}: {CONDITION_TYPE} status={condition['status']}, "
                        f"reason={condition['reason']}, message={condition['message']}")

    K8Helper.triage(environment, condition_ok,
                    f"RBAC HTTP auth test FAILED: amdgpuhealth could not query metrics through "
                    f"kube-rbac-proxy over HTTP with bearer token on nodes: {failed_nodes}")

    Logger.info("RBAC HTTP auth test PASSED: amdgpuhealth queried RBAC-protected exporter over HTTP")


@pytest.mark.level2
def test_npd_amdgpuhealth_exporter_root_ca(request, gpu_cluster, deviceconfig_install, deploy_npd_daemonset,
                                             images, environment):
    """
    Test NPD amdgpuhealth with TLS root CA verification for the metrics exporter.

    When the metrics exporter has RBAC enabled with a custom TLS certificate,
    amdgpuhealth must trust the server's CA to establish HTTPS connections.
    This test:

    1. Generates a CA and server certificate (signed by that CA)
    2. Creates a TLS Secret for kube-rbac-proxy server cert
    3. Enables RBAC on the exporter with the server TLS secret
    4. Stores the CA cert in a Secret in the NPD namespace
    5. Creates an SA + token for bearer auth (required to pass RBAC)
    6. Deploys NPD with both bearer token and root CA auth config
    7. Verifies NPD can query metrics with TLS verification
    """
    global Logger

    METRIC_NAME = "gpu_junction_temperature"
    THRESHOLD = 5  # Low threshold so actual temp (~40) triggers condition
    CONDITION_TYPE = "AMDGPURootCATest"
    REASON_HEALTHY = "RootCAQueryOK"
    REASON_PROBLEM = "RootCAQueryFailed"
    MESSAGE_HEALTHY = "GPU metric query with TLS root CA succeeded"
    MESSAGE_PROBLEM = "GPU metric query with TLS root CA failed"

    AUTH_SA_NS = "metrics-reader"
    AUTH_SA_NAME = "npd-rootca-client"
    AUTH_CR_NAME = "npd-rootca-metrics"
    AUTH_CRB_NAME = "npd-rootca-metrics"
    AUTH_TOKEN_SECRET = "npd-rootca-bearer-token"
    AUTH_CA_SECRET = "npd-exporter-rootca"
    SERVER_TLS_SECRET = "npd-server-metrics-tls"

    def _cleanup():
        npd_util.remove_npd_amdgpuhealth_plugin(environment)
        npd_util.clear_node_condition(CONDITION_TYPE)
        k8_util.k8_delete_secret(AUTH_TOKEN_SECRET, "generic", npd_util.NPD_NAMESPACE)
        k8_util.k8_delete_secret(AUTH_CA_SECRET, "generic", npd_util.NPD_NAMESPACE)
        k8_util.k8_delete_secret(SERVER_TLS_SECRET, "tls", environment.gpu_operator_namespace)
        k8_util.k8_delete_cluster_role_binding(AUTH_CRB_NAME)
        k8_util.k8_delete_cluster_role(AUTH_CR_NAME)
        k8_util.k8_delete_service_account(AUTH_SA_NAME, AUTH_SA_NS)
        for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
            tcfg['metricsExporter.enable'] = True
            tcfg['metricsExporter.serviceType'] = 'NodePort'
            tcfg['metricsExporter.rbacConfig.enable'] = False
            tcfg['metricsExporter.rbacConfig.disableHttps'] = False
            tcfg['metricsExporter.rbacConfig.secret.name'] = None
            cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
            k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.check_deviceconfig_status(environment, deviceconfig_install.devicecfg_list)
        for devcfg in deviceconfig_install.devicecfg_list:
            K8Helper.wait_kmm_worker_completion(environment, devcfg)

    request.addfinalizer(_cleanup)

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error getting GPU nodes")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No AMD GPU nodes found")

    npd_util.clear_node_condition(CONDITION_TYPE)

    # Step 1: Generate CA and server certificate
    Logger.info("Generating CA and server TLS certificate")
    ca_key_path = os.path.join(environment.logdir, "npd-rootca-ca.key")
    result = subprocess.run(
        ["openssl", "genrsa", "-out", ca_key_path, "2048"],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')
    K8Helper.triage(environment, (result.returncode == 0), "Failed to generate CA key")

    ca_crt_path = os.path.join(environment.logdir, "npd-rootca-ca.crt")
    result = subprocess.run(
        ["openssl", "req", "-x509", "-new", "-nodes", "-key", ca_key_path,
         "-subj", "/CN=npd-test-ca", "-days", "365", "-out", ca_crt_path],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')
    K8Helper.triage(environment, (result.returncode == 0), "Failed to generate CA cert")

    ip_sans = _build_ip_san_entries(gpu_nodes)
    san_cnf = f"""[req]
distinguished_name = req_distinguished_name
req_extensions = v3_req
prompt = no

[req_distinguished_name]
CN = metrics-exporter

[v3_req]
keyUsage = digitalSignature, keyEncipherment, dataEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = metrics-exporter
{ip_sans}
"""
    san_cnf_path = os.path.join(environment.logdir, "npd-rootca-san-server.cnf")
    with open(san_cnf_path, "w") as fp:
        fp.write(san_cnf)

    server_key_path = os.path.join(environment.logdir, "npd-rootca-server.key")
    result = subprocess.run(
        ["openssl", "genrsa", "-out", server_key_path, "2048"],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')
    K8Helper.triage(environment, (result.returncode == 0), "Failed to generate server key")

    server_csr_path = os.path.join(environment.logdir, "npd-rootca-server.csr")
    result = subprocess.run(
        ["openssl", "req", "-new", "-key", server_key_path, "-out", server_csr_path,
         "-config", san_cnf_path],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')
    K8Helper.triage(environment, (result.returncode == 0), "Failed to generate server CSR")

    server_crt_path = os.path.join(environment.logdir, "npd-rootca-server.crt")
    result = subprocess.run(
        ["openssl", "x509", "-req", "-in", server_csr_path, "-CA", ca_crt_path,
         "-CAkey", ca_key_path, "-CAcreateserial", "-out", server_crt_path,
         "-days", "365", "-sha256", "-extensions", "v3_req", "-extfile", san_cnf_path],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')
    K8Helper.triage(environment, (result.returncode == 0), "Failed to sign server cert")

    # Step 2: Create server TLS Secret for kube-rbac-proxy
    k8_util.k8_delete_secret(SERVER_TLS_SECRET, "tls", environment.gpu_operator_namespace)
    ret_code, _, ret_stderr = k8_util.k8_create_secret(
        SERVER_TLS_SECRET, "tls", cert_path=server_crt_path,
        key_path=server_key_path, namespace=environment.gpu_operator_namespace)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to create server TLS Secret: {ret_stderr}")

    # Step 3: Enable RBAC on exporter with server TLS secret
    Logger.info("Enabling RBAC on metrics exporter with custom TLS cert")
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['metricsExporter.rbacConfig.enable'] = True
        tcfg['metricsExporter.rbacConfig.disableHttps'] = False
        tcfg['metricsExporter.rbacConfig.secret.name'] = SERVER_TLS_SECRET
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, _, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, (ret_code == 0), f"Failed to update DeviceConfig: {ret_stderr}")

    K8Helper.check_deviceconfig_status(environment, deviceconfig_install.devicecfg_list)
    for devcfg in deviceconfig_install.devicecfg_list:
        K8Helper.wait_kmm_worker_completion(environment, devcfg)

    devicecfg_pods = [
        common.PodInfo('device-plugin', len(gpu_nodes), 1),
        common.PodInfo('metrics-exporter', len(gpu_nodes), 2),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods)
    K8Helper.triage(environment, not failed_pods, f"Exporter pods not ready: {failed_pods}")

    # Step 4: Store CA cert in NPD namespace Secret
    Logger.info("Creating root CA Secret in NPD namespace")
    k8_util.k8_delete_secret(AUTH_CA_SECRET, "generic", npd_util.NPD_NAMESPACE)
    with open(ca_crt_path, "r") as fp:
        ca_cert_pem = fp.read()
    ret_code, _, ret_stderr = k8_util.k8_create_secret(
        AUTH_CA_SECRET, "generic", namespace=npd_util.NPD_NAMESPACE, **{"ca.crt": ca_cert_pem})
    K8Helper.triage(environment, (ret_code == 0), f"Failed to create CA Secret: {ret_stderr}")

    # Step 5: Create SA + token for bearer auth (RBAC still requires identity)
    Logger.info("Creating SA and bearer token for RBAC access")
    k8_util.k8_delete_cluster_role_binding(AUTH_CRB_NAME)
    k8_util.k8_delete_cluster_role(AUTH_CR_NAME)
    k8_util.k8_delete_service_account(AUTH_SA_NAME, AUTH_SA_NS)
    k8_util.k8_create_namespace(AUTH_SA_NS)

    ret_code, _, ret_stderr = k8_util.k8_create_service_account(AUTH_SA_NAME, AUTH_SA_NS)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to create SA: {ret_stderr}")

    ret_code, _, ret_stderr = k8_util.k8_create_cluster_role(
        AUTH_CR_NAME, k8_util.k8_create_rules_from_endpoint_list([("/metrics", "get"), ("/gpumetrics", "get")]))
    K8Helper.triage(environment, (ret_code == 0), f"Failed to create ClusterRole: {ret_stderr}")

    ret_code, _, ret_stderr = k8_util.k8_create_role_binding(AUTH_CRB_NAME, AUTH_SA_NS, AUTH_CR_NAME, AUTH_SA_NAME)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to create ClusterRoleBinding: {ret_stderr}")

    token = k8_util.k8_create_token(AUTH_SA_NS, AUTH_SA_NAME, "1h")
    K8Helper.triage(environment, (token is not None), "Failed to create bearer token")

    k8_util.k8_delete_secret(AUTH_TOKEN_SECRET, "generic", npd_util.NPD_NAMESPACE)
    ret_code, _, ret_stderr = k8_util.k8_create_secret(
        AUTH_TOKEN_SECRET, "generic", token=token, namespace=npd_util.NPD_NAMESPACE)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to create token Secret: {ret_stderr}")

    time.sleep(10)

    # Step 6: Deploy NPD with bearer token + root CA
    Logger.info("Deploying NPD with bearer token + root CA auth")
    ret_code = npd_util.deploy_npd_custom_condition(
        environment, "gauge-metric", METRIC_NAME, THRESHOLD,
        CONDITION_TYPE, REASON_HEALTHY, REASON_PROBLEM,
        MESSAGE_HEALTHY, MESSAGE_PROBLEM, invoke_interval="15s",
        auth={
            "exporter_bearer_token_secret": AUTH_TOKEN_SECRET,
            "exporter_root_ca_secret": AUTH_CA_SECRET,
        }
    )
    K8Helper.triage(environment, (ret_code == 0), "Failed to deploy NPD with root CA auth")

    ds_ready = npd_util.wait_for_npd_daemonset_ready(npd_util.NPD_NAMESPACE, npd_util.NPD_APP_NAME, timeout=300)
    K8Helper.triage(environment, ds_ready, "NPD DaemonSet failed to rollout with root CA auth")

    # Step 7: Verify condition is True (problem) — proves metric was read through TLS auth
    condition_ok, failed_nodes = npd_util.verify_npd_node_condition(
        gpu_nodes, condition_type=CONDITION_TYPE,
        expected_status="True", expected_reason=REASON_PROBLEM,
        timeout=180, interval=15
    )

    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        condition = npd_util.get_node_condition(node_name, CONDITION_TYPE)
        if condition:
            Logger.info(f"Node {node_name}: {CONDITION_TYPE} status={condition['status']}, "
                        f"reason={condition['reason']}, message={condition['message']}")

    K8Helper.triage(environment, condition_ok,
                    f"Root CA auth test FAILED: amdgpuhealth could not query metrics with TLS root CA "
                    f"verification on nodes: {failed_nodes}")

    Logger.info("Root CA auth test PASSED: amdgpuhealth queried exporter with TLS root CA verification")


@pytest.mark.level2
def test_npd_amdgpuhealth_mtls(request, gpu_cluster, deviceconfig_install, deploy_npd_daemonset,
                                images, environment):
    """
    Test NPD amdgpuhealth with mTLS client certificate authentication to the metrics exporter.

    When the metrics exporter has RBAC enabled with client certificate verification,
    amdgpuhealth must present a valid client certificate signed by the trusted CA.
    This test:

    1. Generates CA, server cert, and client cert (all signed by same CA)
    2. Creates server TLS Secret + client CA ConfigMap for kube-rbac-proxy
    3. Enables RBAC on exporter with mTLS config (server TLS + clientCAConfigMap)
    4. Creates RBAC for the client cert CN
    5. Stores client cert as TLS Secret in NPD namespace + CA cert as generic Secret
    6. Deploys NPD with client_cert_secret and exporter_root_ca_secret
    7. Verifies NPD can query metrics with mutual TLS authentication
    """
    global Logger

    METRIC_NAME = "gpu_junction_temperature"
    THRESHOLD = 5  # Low threshold so actual temp (~40) triggers condition
    CONDITION_TYPE = "AMDGPUmTLSTest"
    REASON_HEALTHY = "mTLSQueryOK"
    REASON_PROBLEM = "mTLSQueryFailed"
    MESSAGE_HEALTHY = "GPU metric query with mTLS succeeded"
    MESSAGE_PROBLEM = "GPU metric query with mTLS failed"

    CLIENT_CN = "npd-amdgpuhealth-client"
    SERVER_TLS_SECRET = "npd-mtls-server-tls"
    CLIENT_CA_CM = "npd-mtls-client-ca"
    AUTH_CLIENT_SECRET = "npd-mtls-client-cert"
    AUTH_CA_SECRET = "npd-mtls-rootca"
    AUTH_CR_NAME = "npd-mtls-metrics"
    AUTH_CRB_NAME = "npd-mtls-metrics-cert-user"

    def _cleanup():
        npd_util.remove_npd_amdgpuhealth_plugin(environment)
        npd_util.clear_node_condition(CONDITION_TYPE)
        k8_util.k8_delete_secret(AUTH_CLIENT_SECRET, "tls", npd_util.NPD_NAMESPACE)
        k8_util.k8_delete_secret(AUTH_CA_SECRET, "generic", npd_util.NPD_NAMESPACE)
        k8_util.k8_delete_secret(SERVER_TLS_SECRET, "tls", environment.gpu_operator_namespace)
        k8_util.k8_delete_configmap(environment.gpu_operator_namespace, CLIENT_CA_CM)
        k8_util.k8_delete_cluster_role_binding(AUTH_CRB_NAME)
        k8_util.k8_delete_cluster_role(AUTH_CR_NAME)
        for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
            tcfg['metricsExporter.enable'] = True
            tcfg['metricsExporter.serviceType'] = 'NodePort'
            tcfg['metricsExporter.rbacConfig.enable'] = False
            tcfg['metricsExporter.rbacConfig.disableHttps'] = False
            tcfg['metricsExporter.rbacConfig.secret.name'] = None
            tcfg['metricsExporter.rbacConfig.clientCAConfigMap.name'] = None
            cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
            k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.check_deviceconfig_status(environment, deviceconfig_install.devicecfg_list)
        for devcfg in deviceconfig_install.devicecfg_list:
            K8Helper.wait_kmm_worker_completion(environment, devcfg)

    request.addfinalizer(_cleanup)

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error getting GPU nodes")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No AMD GPU nodes found")

    npd_util.clear_node_condition(CONDITION_TYPE)

    # Step 1: Generate CA, server cert, and client cert
    Logger.info("Generating CA, server, and client certificates for mTLS")

    ca_key_path = os.path.join(environment.logdir, "npd-mtls-ca.key")
    result = subprocess.run(
        ["openssl", "genrsa", "-out", ca_key_path, "2048"],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')
    K8Helper.triage(environment, (result.returncode == 0), "Failed to generate CA key")

    ca_crt_path = os.path.join(environment.logdir, "npd-mtls-ca.crt")
    result = subprocess.run(
        ["openssl", "req", "-x509", "-new", "-nodes", "-key", ca_key_path,
         "-subj", "/CN=npd-mtls-ca", "-days", "365", "-out", ca_crt_path],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')
    K8Helper.triage(environment, (result.returncode == 0), "Failed to generate CA cert")

    # Server SAN config — include GPU node IPs so TLS verifies when amdgpuhealth connects by IP
    ip_sans = _build_ip_san_entries(gpu_nodes)
    san_server_cnf = f"""[req]
distinguished_name = req_distinguished_name
req_extensions = v3_req
prompt = no

[req_distinguished_name]
CN = metrics-exporter

[v3_req]
keyUsage = digitalSignature, keyEncipherment, dataEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = metrics-exporter
{ip_sans}
"""
    san_server_cnf_path = os.path.join(environment.logdir, "npd-mtls-san-server.cnf")
    with open(san_server_cnf_path, "w") as fp:
        fp.write(san_server_cnf)

    server_key_path = os.path.join(environment.logdir, "npd-mtls-server.key")
    result = subprocess.run(
        ["openssl", "genrsa", "-out", server_key_path, "2048"],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')
    K8Helper.triage(environment, (result.returncode == 0), "Failed to generate server key")

    server_csr_path = os.path.join(environment.logdir, "npd-mtls-server.csr")
    result = subprocess.run(
        ["openssl", "req", "-new", "-key", server_key_path, "-out", server_csr_path,
         "-config", san_server_cnf_path],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')
    K8Helper.triage(environment, (result.returncode == 0), "Failed to generate server CSR")

    server_crt_path = os.path.join(environment.logdir, "npd-mtls-server.crt")
    result = subprocess.run(
        ["openssl", "x509", "-req", "-in", server_csr_path, "-CA", ca_crt_path,
         "-CAkey", ca_key_path, "-CAcreateserial", "-out", server_crt_path,
         "-days", "365", "-sha256", "-extensions", "v3_req", "-extfile", san_server_cnf_path],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')
    K8Helper.triage(environment, (result.returncode == 0), "Failed to sign server cert")

    # Client SAN config
    san_client_cnf = f"""[req]
distinguished_name = req_distinguished_name
req_extensions = v3_req
prompt = no

[req_distinguished_name]
CN = {CLIENT_CN}

[v3_req]
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = clientAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = {CLIENT_CN}
"""
    san_client_cnf_path = os.path.join(environment.logdir, "npd-mtls-san-client.cnf")
    with open(san_client_cnf_path, "w") as fp:
        fp.write(san_client_cnf)

    client_key_path = os.path.join(environment.logdir, "npd-mtls-client.key")
    result = subprocess.run(
        ["openssl", "genrsa", "-out", client_key_path, "2048"],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')
    K8Helper.triage(environment, (result.returncode == 0), "Failed to generate client key")

    client_csr_path = os.path.join(environment.logdir, "npd-mtls-client.csr")
    result = subprocess.run(
        ["openssl", "req", "-new", "-key", client_key_path, "-out", client_csr_path,
         "-config", san_client_cnf_path],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')
    K8Helper.triage(environment, (result.returncode == 0), "Failed to generate client CSR")

    client_crt_path = os.path.join(environment.logdir, "npd-mtls-client.crt")
    result = subprocess.run(
        ["openssl", "x509", "-req", "-in", client_csr_path, "-CA", ca_crt_path,
         "-CAkey", ca_key_path, "-CAcreateserial", "-out", client_crt_path,
         "-days", "365", "-sha256", "-extensions", "v3_req", "-extfile", san_client_cnf_path],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')
    K8Helper.triage(environment, (result.returncode == 0), "Failed to sign client cert")

    # Step 2: Create server TLS Secret + client CA ConfigMap for kube-rbac-proxy
    Logger.info("Creating server TLS Secret and client CA ConfigMap")
    k8_util.k8_delete_secret(SERVER_TLS_SECRET, "tls", environment.gpu_operator_namespace)
    ret_code, _, ret_stderr = k8_util.k8_create_secret(
        SERVER_TLS_SECRET, "tls", cert_path=server_crt_path,
        key_path=server_key_path, namespace=environment.gpu_operator_namespace)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to create server TLS Secret: {ret_stderr}")

    k8_util.k8_delete_configmap(environment.gpu_operator_namespace, CLIENT_CA_CM)
    ret_code, _, ret_stderr = k8_util.k8_create_configmap(
        environment.gpu_operator_namespace, CLIENT_CA_CM,
        ca_crt_path, "ca.crt")
    K8Helper.triage(environment, (ret_code == 0), f"Failed to create client CA ConfigMap: {ret_stderr}")

    # Step 3: Enable RBAC on exporter with mTLS
    Logger.info("Enabling RBAC on metrics exporter with mTLS config")
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['metricsExporter.rbacConfig.enable'] = True
        tcfg['metricsExporter.rbacConfig.disableHttps'] = False
        tcfg['metricsExporter.rbacConfig.secret.name'] = SERVER_TLS_SECRET
        tcfg['metricsExporter.rbacConfig.clientCAConfigMap.name'] = CLIENT_CA_CM
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, _, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, (ret_code == 0), f"Failed to update DeviceConfig: {ret_stderr}")

    K8Helper.check_deviceconfig_status(environment, deviceconfig_install.devicecfg_list)
    for devcfg in deviceconfig_install.devicecfg_list:
        K8Helper.wait_kmm_worker_completion(environment, devcfg)

    devicecfg_pods = [
        common.PodInfo('device-plugin', len(gpu_nodes), 1),
        common.PodInfo('metrics-exporter', len(gpu_nodes), 2),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods)
    K8Helper.triage(environment, not failed_pods, f"Exporter pods not ready: {failed_pods}")

    # Step 4: Create RBAC for client cert CN
    Logger.info(f"Creating RBAC for client cert CN={CLIENT_CN}")
    k8_util.k8_delete_cluster_role_binding(AUTH_CRB_NAME)
    k8_util.k8_delete_cluster_role(AUTH_CR_NAME)

    ret_code, _, ret_stderr = k8_util.k8_create_cluster_role(
        AUTH_CR_NAME, k8_util.k8_create_rules_from_endpoint_list([("/metrics", "get"), ("/gpumetrics", "get")]))
    K8Helper.triage(environment, (ret_code == 0), f"Failed to create ClusterRole: {ret_stderr}")

    ret_code, _, ret_stderr = k8_util.k8_create_role_binding_user(
        AUTH_CRB_NAME, AUTH_CR_NAME, CLIENT_CN)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to create ClusterRoleBinding for CN: {ret_stderr}")

    # Step 5: Store client cert + CA cert in NPD namespace
    Logger.info("Creating client cert and root CA Secrets in NPD namespace")
    k8_util.k8_delete_secret(AUTH_CLIENT_SECRET, "tls", npd_util.NPD_NAMESPACE)
    ret_code, _, ret_stderr = k8_util.k8_create_secret(
        AUTH_CLIENT_SECRET, "tls", cert_path=client_crt_path,
        key_path=client_key_path, namespace=npd_util.NPD_NAMESPACE)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to create client TLS Secret: {ret_stderr}")

    k8_util.k8_delete_secret(AUTH_CA_SECRET, "generic", npd_util.NPD_NAMESPACE)
    with open(ca_crt_path, "r") as fp:
        ca_cert_pem = fp.read()
    ret_code, _, ret_stderr = k8_util.k8_create_secret(
        AUTH_CA_SECRET, "generic", namespace=npd_util.NPD_NAMESPACE, **{"ca.crt": ca_cert_pem})
    K8Helper.triage(environment, (ret_code == 0), f"Failed to create CA Secret: {ret_stderr}")

    time.sleep(10)

    # Step 6: Deploy NPD with mTLS auth
    Logger.info("Deploying NPD with mTLS client certificate auth")
    ret_code = npd_util.deploy_npd_custom_condition(
        environment, "gauge-metric", METRIC_NAME, THRESHOLD,
        CONDITION_TYPE, REASON_HEALTHY, REASON_PROBLEM,
        MESSAGE_HEALTHY, MESSAGE_PROBLEM, invoke_interval="15s",
        auth={
            "client_cert_secret": AUTH_CLIENT_SECRET,
            "exporter_root_ca_secret": AUTH_CA_SECRET,
        }
    )
    K8Helper.triage(environment, (ret_code == 0), "Failed to deploy NPD with mTLS auth")

    ds_ready = npd_util.wait_for_npd_daemonset_ready(npd_util.NPD_NAMESPACE, npd_util.NPD_APP_NAME, timeout=300)
    K8Helper.triage(environment, ds_ready, "NPD DaemonSet failed to rollout with mTLS auth")

    # Step 7: Verify condition is True (problem) — proves metric was read through mTLS
    condition_ok, failed_nodes = npd_util.verify_npd_node_condition(
        gpu_nodes, condition_type=CONDITION_TYPE,
        expected_status="True", expected_reason=REASON_PROBLEM,
        timeout=180, interval=15
    )

    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        condition = npd_util.get_node_condition(node_name, CONDITION_TYPE)
        if condition:
            Logger.info(f"Node {node_name}: {CONDITION_TYPE} status={condition['status']}, "
                        f"reason={condition['reason']}, message={condition['message']}")

    K8Helper.triage(environment, condition_ok,
                    f"mTLS auth test FAILED: amdgpuhealth could not query metrics with client certificate "
                    f"authentication on nodes: {failed_nodes}")

    Logger.info("mTLS auth test PASSED: amdgpuhealth queried exporter with mutual TLS authentication")
