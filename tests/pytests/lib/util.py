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
import os
import logging
import time
import re
import json
import pprint
import copy
import subprocess
import shutil
from enum import Enum
from datetime import datetime

import lib.common as common
import lib.k8_util as k8_util
import lib.spec_util as spec_util
import lib.amdgpu as amdgpu_util

Logger = logging.getLogger("k8.helper")
LogPrettyPrinter = pprint.PrettyPrinter(indent = 2)

class K8Helper:

    class PodStatus(Enum):
        NA          = 0
        PENDING     = 1
        RUNNING     = 2
        FAILED      = 3
        SUCCEEDED   = 4
        UNKNOWN     = 5
        STOPPED     = 6

    class WorkloadOp(Enum):
        UNKNOWN         = 0
        START_WORKLOAD  = 1
        STOP_WORKLOAD   = 2

    @staticmethod
    def get_amd_smi_path(environment):
        return "/opt/rocm/bin/amd-smi"

    @staticmethod
    def wait_for_upgrade_completion_status(environment, devicecfg_list, gpu_nodes, fail_on_timeout=True):
        # Check for kmm-worker-{gpu-node-name}-test-deviceconfig PODs to be started and completed
        # fail_on_timeout: if False, log error but don't fail test (useful for teardown recovery)
        if environment.amdgpu_driver_spec["driver-deployment"] == "inbox":
            Logger.info("Using inbox amdgpu driver - skip kmm verification")
            return

        time.sleep(10)
        upgrade_complete = False
        # Increase timeout to 20 minutes (was 10) - driver upgrade with reboot can take longer
        for iteration in range(20):
            pending_nodes = set(map(lambda x: x['metadata']['name'], gpu_nodes))
            for devcfg in devicecfg_list:
                devcfg_info = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace, devcfg)
                if not devcfg_info or devcfg not in devcfg_info:
                    error_msg = f"Failed to collect status of deviceconfig {devcfg}"
                    if fail_on_timeout:
                        K8Helper.triage(environment, False, error_msg)
                    else:
                        Logger.error(error_msg)
                        continue

                if devcfg_info[devcfg].get('status', None):
                    nodeStatusMap = devcfg_info[devcfg]['status'].get('nodeModuleStatus', None)

                    if nodeStatusMap:
                        for node in gpu_nodes:
                            node_name = node['metadata']['name']
                            if node_name in nodeStatusMap:
                                if 'status' in nodeStatusMap[node_name]:
                                    node_info = nodeStatusMap[node_name]
                                    node_status = node_info['status']

                                    # Enhanced logging with detailed node state
                                    container_image = node_info.get('containerImage', 'N/A')
                                    boot_id = node_info.get('bootId', 'N/A')
                                    last_transition_time = node_info.get('lastTransitionTime', 'N/A')

                                    if node_status == 'Upgrade-Complete':
                                        if node_name in pending_nodes:
                                            pending_nodes.remove(node_name)
                                            Logger.info(f"Node {node_name}: Upgrade-Complete (image: {container_image}, bootId: {boot_id}, transitioned: {last_transition_time})")
                                    else:
                                        Logger.info(f"Node {node_name}: {node_status} (image: {container_image}, bootId: {boot_id}, lastTransition: {last_transition_time})")
                                else:
                                    Logger.warn(f"DeviceConfig nodeModuleStatus does not have status information")
            if len(pending_nodes) > 0:
                Logger.info(f"Waiting for {pending_nodes} to complete upgrade process (attempt {iteration+1}/20)")
                time.sleep(60)
            else:
                upgrade_complete = True
                break

        if not upgrade_complete:
            Logger.error("Failed to complete upgrade-process for all nodes")
            for devcfg in devicecfg_list:
                devcfg_info = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace, devcfg)
                if not devcfg_info or devcfg not in devcfg_info:
                    error_msg = f"Failed to collect status of deviceconfig {devcfg}"
                    if fail_on_timeout:
                        K8Helper.triage(environment, False, error_msg)
                    else:
                        Logger.error(error_msg)
                        continue

                nodeStatus = devcfg_info[devcfg]['status']['nodeModuleStatus']
                Logger.debug(nodeStatus)
            if fail_on_timeout:
                K8Helper.triage(environment, upgrade_complete == True, "upgrade failed")
            else:
                Logger.warn("Upgrade did not complete within timeout, but continuing (fail_on_timeout=False)")
            return
        Logger.info("Upgrade complete for all nodes")

    @staticmethod
    def wait_for_upgrade_completion_label(environment, devicecfg_list):
        # Check for kmm-worker-{gpu-node-name}-test-deviceconfig PODs to be started and completed
        if environment.amdgpu_driver_spec["driver-deployment"] == "inbox":
            Logger.info("Using inbox amdgpu driver - skip kmm verification")
            return

        time.sleep(20)
        upgrade_complete = False
        for _ in range(10):
            # TODO: Check for label change for each node
            # ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
            pass

        if not upgrade_complete:
            Logger.error("Failed to complete upgrade-process for all nodes")
            for devcfg in devicecfg_list:
                devcfg_info = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace, devcfg)
                K8Helper.triage(environment, devcfg_info != None and devcfg in devcfg_info,
                                f"Failed to collect status of deviceconfig {devcfg}")

                nodeStatus = devcfg_info[devcfg]['status']['nodeModuleStatus']
                Logger.debug(nodeStatus)
            K8Helper.triage(environment, upgrade_complete == True, "upgrade failed")
            return
        Logger.info("Upgrade complete for all nodes")

    @staticmethod
    def wait_kmm_worker_completion(environment, devcfg_name):
        # Check for kmm-worker-{gpu-node-name}-test-deviceconfig PODs to be started and completed
        if environment.amdgpu_driver_spec["driver-deployment"] == "inbox":
            Logger.info("Using inbox amdgpu driver - skip kmm verification")
            return

        ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
        K8Helper.triage(environment, ret_code == 0, "Error while getting gpu-nodes from k8-cluster")
        K8Helper.triage(environment, len(gpu_nodes), "No nodes with AMD/GPU found in the cluster")

        # Check for build pods
        build_pods = []
        build_pods.append(common.PodInfo(f"{devcfg_name}-build", 1, 1))

        build_pod_status = set()
        time.sleep(10)
        for _ in range(15):
            build_pod_status.clear()
            status_info = k8_util.k8_check_pod_status(environment.gpu_operator_namespace, build_pods)
            Logger.debug(f"build pod status: {status_info}")

            for pod_name, (status, full_pod_info) in status_info.items():
                if status == 'Running':
                    build_pod_status.add(K8Helper.PodStatus.RUNNING)
                elif status == 'Pending':
                    build_pod_status.add(K8Helper.PodStatus.PENDING)
                elif status == 'Failed':
                    build_pod_status.add(K8Helper.PodStatus.FAILED)
                else:
                    Logger.warn(f"build pod status unknown, pod-name: {pod_name}")
                    build_pod_status.add(K8Helper.PodStatus.UNKNOWN)

            if K8Helper.PodStatus.PENDING in build_pod_status or K8Helper.PodStatus.RUNNING in build_pod_status:
                Logger.debug("Wait for 60-sec as some of the build pods are in Running/Pending status")
                time.sleep(60)
            else:
                break

        K8Helper.triage(environment, K8Helper.PodStatus.PENDING not in build_pod_status, "build pod still pending")
        K8Helper.triage(environment, K8Helper.PodStatus.RUNNING not in build_pod_status, "build pod still running")
        K8Helper.triage(environment, K8Helper.PodStatus.FAILED not in build_pod_status, "build pod failed")

        # Check for kmm pods
        kmm_worker_pods = []
        for node in gpu_nodes:
            node_name = node['metadata']['name']
            K8Helper.triage(environment, node_name != None, "Missing node-name for the gpu node in the node-info JSON")
            kmm_worker_pods.append(common.PodInfo(f"kmm-worker-{node_name}-", 1, 1))

        kmm_pod_status = set()
        time.sleep(10)
        for _ in range(8):
            kmm_pod_status.clear()
            status_info = k8_util.k8_check_pod_status(environment.gpu_operator_namespace, kmm_worker_pods)
            Logger.debug(f"kmm-worker status: {status_info}")

            for pod_name, (status, full_pod_info) in status_info.items():
                if status == 'Running':
                    kmm_pod_status.add(K8Helper.PodStatus.RUNNING)
                elif status == 'Pending':
                    kmm_pod_status.add(K8Helper.PodStatus.PENDING)
                elif status == 'Failed':
                    kmm_pod_status.add(K8Helper.PodStatus.FAILED)
                else:
                    Logger.warn(f"kmm pod status unknown, pod-name: {pod_name}")
                    kmm_pod_status.add(K8Helper.PodStatus.UNKNOWN)

            if K8Helper.PodStatus.PENDING in kmm_pod_status or K8Helper.PodStatus.RUNNING in kmm_pod_status:
                Logger.debug("Wait for 60-sec as some of the kmm-worker pods are in Running/Pending status")
                time.sleep(60)
            else:
                break

        K8Helper.triage(environment, K8Helper.PodStatus.PENDING not in kmm_pod_status, "kmm-worker pod still pending")
        K8Helper.triage(environment, K8Helper.PodStatus.RUNNING not in kmm_pod_status, "kmm-worker pod still running")
        K8Helper.triage(environment, K8Helper.PodStatus.FAILED not in kmm_pod_status, "kmm-worker pod failed")

        # Finally check for labels
        label_missing = set()
        for _ in range(8):
            label_missing.clear()
            ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
            K8Helper.triage(environment, ret_code == 0, "Error while getting gpu-nodes from k8-cluster")
            K8Helper.triage(environment, len(gpu_nodes), "No nodes with AMD/GPU found in the cluster")

            pattern = r"kmm\.node\.kubernetes\.io/" + environment.gpu_operator_namespace + r"\.(.*?)\.ready"
            for node in gpu_nodes:
                label_found = False
                for label, _ in node['metadata']['labels'].items():
                    if re.match(pattern, label):
                        label_found = True
                        break
                if not label_found:
                    label_missing.add(node['metadata']['name'])
            if len(label_missing) > 0:
                Logger.warn(f"Missing kmm.ready label for {label_missing}")
                time.sleep(60)
        K8Helper.triage(environment, label_found, f"One or more nodes missing kmm.ready label : {label_missing}")
        return

    # Check for corresponding deviceconfig created
    @staticmethod
    def check_deviceconfig_status(environment, devicecfg_list):
        for devcfg in devicecfg_list:
            devcfg_info = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace, devcfg)
            K8Helper.triage(environment, devcfg_info != None and devcfg in devcfg_info,
                            f"Failed to collect status of deviceconfig {devcfg}")
            #status_info = devcfg_info[devcfg].get('status')
            #if environment.gpu_operator_version > "v1.1.0":
            #    conditions = status_info.get('conditions', [])
            #    K8Helper.triage(environment, len(conditions) > 0, f"deviceconfig status.conditions is empty for {devcfg}")
            #    K8Helper.triage(environment, conditions[0].get('status') == 'True', f"deviceconfig {devcfg} status is not True")
            #    K8Helper.triage(environment, conditions[0].get('type') == 'Ready', f"deviceconfig {devcfg} type is not Ready")
        return

    @staticmethod
    def wait_for_driver_reload(environment, gpu_nodes, fail_on_timeout=True):
        """Wait for driver reload to complete after untainting nodes.

        After untainting nodes, KMM reloads the amdgpu driver. This function waits for
        device-plugin pods to be Running, which confirms the driver is loaded and ready
        for amd-smi queries.

        On SNO (Single Node OpenShift) with rebootRequired=True, KMM may reboot the
        node during driver reload. This function detects API server unavailability
        and waits for the cluster to recover before checking pod status.

        This prevents subsequent tests from failing with "Failed to parse amd-smi-partition
        JSON" errors when querying amd-smi while the driver is still loading.

        Args:
            environment: Test environment fixture.
            gpu_nodes: List of GPU node objects from k8_get_gpu_nodes().
            fail_on_timeout: If True, call triage() on timeout. If False, just log error.

        Returns:
            bool: True if pods are Running, False if timeout occurred.
        """
        import time
        Logger.info("Waiting for device-plugin to be Running (confirms driver reload complete)...")
        devicecfg_pods = [
            common.PodInfo('device-plugin', len(gpu_nodes), 1),
        ]
        from kubernetes.client.rest import ApiException
        from urllib3.exceptions import MaxRetryError, NewConnectionError
        _api_exceptions = (ApiException, MaxRetryError, NewConnectionError,
                           ConnectionError, OSError, AssertionError)

        retry_delay = 20
        failed_pods = None
        api_unreachable = False

        # Single call — k8_check_pod_running has its own retry loop (30 attempts)
        try:
            failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace,
                                                       devicecfg_pods, sleep_time=retry_delay)
        except _api_exceptions as e:
            Logger.warning(f"API server unreachable during driver reload (possible node reboot): {e}")
            api_unreachable = True

        if not api_unreachable:
            if not failed_pods:
                Logger.info("Device-plugin pods Running - driver reload complete")
                return True
            Logger.error(f"Pods failed to become Running: {failed_pods}")
        elif len(gpu_nodes) > 1:
            Logger.error("API server unreachable on multi-node cluster — not attempting SNO recovery")
        else:
            Logger.info("API server went down during driver reload — waiting for cluster recovery (KMM reboot on SNO)...")
            status = k8_util.k8_wait_for_cluster_ready(minikube=True)
            if status == 0:
                Logger.info("Cluster recovered after reboot, re-checking device-plugin pods...")
                time.sleep(30)
                try:
                    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace,
                                                               devicecfg_pods, sleep_time=retry_delay)
                except _api_exceptions as e:
                    Logger.error(f"API still unreachable after cluster recovery: {e}")
                    failed_pods = ["api-unreachable"]
                if not failed_pods:
                    Logger.info("Device-plugin pods Running after cluster recovery")
                    return True
                Logger.error(f"Device-plugin still not ready after cluster recovery: {failed_pods}")
            else:
                Logger.error("Cluster failed to recover after KMM-initiated reboot")
                failed_pods = failed_pods or ["cluster-recovery-failed"]

        # Timeout or failure
        if fail_on_timeout:
            K8Helper.triage(environment, False,
                           f"Device-plugin not Running after driver reload: {failed_pods}")
        return False

    @staticmethod
    def check_deviceconfig_driver_version(gpu_cluster, config_version, environment):
        devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
        for devcfg_name, devcfg_info in devcfg_map.items():
            devcfg_driver_version = devcfg_info.get('spec').get('driver').get('version')
            Logger.info(f'Configured Version: {devcfg_driver_version}') 
            K8Helper.triage(environment, config_version == devcfg_driver_version,
                            f"Expected config_version: {config_version}, device_config: {devcfg_driver_version}")
    @staticmethod
    def update_node_driver_version(gpu_cluster, environment):
        # collect currently applied deviceconfigs
        devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)

        ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
        for node in gpu_nodes:
            node_ip = k8_util.k8_get_node_address(node)
            cluster_node = gpu_cluster.find_node_by_ip(node_ip)
            # Match to one of the deviceconfigs
            for devcfg_name, _ in devcfg_map.items():
                version_module_label = f"kmm.node.kubernetes.io/version-module.{environment.gpu_operator_namespace}.{devcfg_name}"
                node_driver_version = node['metadata']['labels'].get(version_module_label, None)
                if node_driver_version:
                    amdgpu_ver = amdgpu_util.get_matching_driver_version(node_driver_version)
                    cluster_node.amdgpu_driver_version = amdgpu_ver if amdgpu_ver else node_driver_version
                    break
        return

    @staticmethod
    def check_node_driver_version(gpu_cluster, config_version, rocm_version, environment):

        # collect currently applied deviceconfigs
        devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)

        ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
        for node in gpu_nodes:
            node_name = k8_util.k8_get_node_hostname(node)
            label_found = False
            # Match to one of the deviceconfigs
            for devcfg_name, _ in devcfg_map.items():
                version_module_label = f"kmm.node.kubernetes.io/version-module.{environment.gpu_operator_namespace}.{devcfg_name}"
                node_driver_version = node['metadata']['labels'].get(version_module_label, None)
                if node_driver_version:
                    K8Helper.triage(environment, config_version == node_driver_version,
                                    f"failed for node {node_name}: {node_driver_version}")
                    label_found = True
                    break
            K8Helper.triage(environment, (label_found),
                            f"Missing label kmm.node.kubernetes.io/version-module.{environment.gpu_operator_namespace}.<devcfg_name> for node {node_name}")

            cmd = ["dmesg", "-T"]
            #cmd = ["sudo", "dmesg", "-T", "|", "grep", "'amdgpu version'", "|", "tail", "-1"]
            ret_code, resp_stdout = k8_util.run_command_on_node(gpu_cluster, node_name, cmd, skip_chroot = True)
            K8Helper.triage(environment, ret_code == 0, f"error getting dmesg from {node_name} {node_name}")
            K8Helper.triage(environment, resp_stdout != None, f"Error: Command output is None")
            Logger.debug(f"Cmd:{cmd}, Response:\n{LogPrettyPrinter.pformat(resp_stdout)}")
            amdgpu_lines = list(filter(lambda line: 'amdgpu version' in line, resp_stdout.split("\n")))
            K8Helper.triage(environment, len(amdgpu_lines) > 0, "No dmesg-lines with 'amdgpu version' information")
            if rocm_version:
                matching_lines = [line for line in amdgpu_lines if rocm_version in line]
                K8Helper.triage(environment, len(matching_lines) > 0, f"can't find {rocm_version} in {amdgpu_lines}")
            else:
                Logger.warning(f"No known amdgpu driver version for config version {config_version}, skipping dmesg version check")

            # Verify driver version via amd-smi from metrics-exporter pod
            exporter_pod_name = k8_util.k8_get_pod_name("metrics-exporter", environment.gpu_operator_namespace, node_name)
            K8Helper.triage(environment, exporter_pod_name is not None,
                            f"Could not find metrics-exporter pod on node {node_name}")
            ret_code, amd_smi_output, resp_stderr = k8_util.exec_command_in_pod(
                environment.gpu_operator_namespace,
                ["amd-smi", "static", "--json"],
                exporter_pod_name, "metrics-exporter-container")
            K8Helper.triage(environment, ret_code == 0,
                            f"amd-smi static --json failed on {node_name}: {resp_stderr}")
            # Newer amd-smi emits valid JSON; older versions emit Python-style repr
            try:
                smi_data = json.loads(amd_smi_output)
            except json.JSONDecodeError:
                import ast
                smi_data = ast.literal_eval(amd_smi_output)
            smi_driver_version = smi_data["gpu_data"][0]["driver"]["version"]
            Logger.info(f"Node {node_name}: amd-smi reports driver version {smi_driver_version}")
            if rocm_version:
                K8Helper.triage(environment, smi_driver_version == rocm_version,
                                f"Driver version mismatch on {node_name}: amd-smi reports {smi_driver_version}, expected {rocm_version}")
            else:
                K8Helper.triage(environment, len(smi_driver_version) > 0,
                                f"amd-smi returned empty driver version on {node_name}")

    @staticmethod
    def update_test_runner_configmap(recipe, worker, config_map=dict(), framework="RVS", trigger="AUTO_UNHEALTHY_GPU_WATCH"):
        testcase = {
                       "Recipe": recipe,
                       "Iterations": 1,
                       "StopOnFailure": True,
                       "TimeoutSeconds": 2400
                   }
        if framework == "AGFHC":
            testcase["Framework"] = framework
            testcase["Arguments"] = "--ignore-dmesg,--disable-sysmon"
            #TODO try all supported arguments

        trigger_dict = {
                           trigger: {
                               "TestCases": [
                                   testcase
                               ]
                           }
                       }
        if trigger != "AUTO_UNHEALTHY_GPU_WATCH":
            trigger_dict.update(
                       {
                           "AUTO_UNHEALTHY_GPU_WATCH": {
                               "TestCases": [
                                   testcase
                               ]
                           }
                       }
            )
        config_map.update(
            {"TestConfig": {
                "GPU_HEALTH_CHECK": {
                    "TestLocationTrigger": {
                        worker: {
                            "TestParameters": trigger_dict
                        }
                    }
                }
            }}
        )

    @staticmethod
    def collect_tech_support(environment, label : str = "baseline"):
        """Collect a tech-support snapshot independently of a test failure.

        Use this to capture cluster state at a known point (e.g. after sample
        collection) so that later comparison-only tests can reference it
        without collecting their own (stale) snapshots.
        """
        if not environment.tech_support_tool:
            Logger.warn("Missing tech-support-tool information, skipping proactive collection")
            return
        Logger.info(f"Collecting tech-support snapshot (label={label})")
        cmd = [environment.tech_support_tool["tool"], *(environment.tech_support_tool.get("args", []))]
        cmd_resp = subprocess.run(cmd, check=False,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  encoding='utf-8')
        Logger.info(f"tech-support tool returncode: {cmd_resp.returncode}")
        Logger.debug(f"tech-support tool stdout:\n{LogPrettyPrinter.pformat(cmd_resp.stdout)}")
        Logger.debug(f"tech-support tool stderr:\n{LogPrettyPrinter.pformat(cmd_resp.stderr)}")
        if cmd_resp.returncode == 0:
            ts_dir = os.path.join(environment.logdir, "tech-support")
            os.makedirs(ts_dir, exist_ok=True)
            for file_name in os.listdir(os.getcwd()):
                if "techsupport-" in file_name:
                    dest = os.path.join(ts_dir, f"tech_support_{label}.tgz")
                    shutil.move(file_name, dest)
                    Logger.info(f"Tech-support saved to {dest}")
                    break
        else:
            Logger.warn("Failed to generate/collect techsupport file")

    @staticmethod
    def triage(environment, condition : bool, message : str, expected_to_fail : bool = False, skip_techsupport : bool = False):
        if condition:
            # Test/condition passed
            return
        # Test/Condition Failed - triage
        ctx = getattr(environment, 'context', None)
        if ctx and hasattr(ctx, 'current_tc_name') and hasattr(ctx, 'unhealthy_pods'):
            Logger.info(f"Adding context about testcase with unhealthy pods information")
            context_file = os.path.join(environment.logdir, f"context_{ctx.current_tc_name}.json")
            with open(context_file, "w") as fp:
                fp.write(json.dumps(ctx.unhealthy_pods, indent=4, default=str))
        if expected_to_fail:
            pytest.xfail(message)
        else:
            if not skip_techsupport and environment.tech_support_tool:
                Logger.info(f"Running tech-support tool {environment.tech_support_tool}")
                cmd = [environment.tech_support_tool["tool"], *(environment.tech_support_tool.get("args", []))]
                cmd_resp = subprocess.run(cmd, check=False,
                                          stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE,
                                          encoding='utf-8')
                Logger.info(f"tech-support tool returncode: {cmd_resp.returncode}")
                Logger.debug(f"tech-support tool stdout:\n{LogPrettyPrinter.pformat(cmd_resp.stdout)}")
                Logger.debug(f"tech-support tool stderr:\n{LogPrettyPrinter.pformat(cmd_resp.stderr)}")
                if cmd_resp.returncode == 0:
                    for file_name in os.listdir(os.getcwd()):
                        if "techsupport-" in file_name:
                            if ctx and hasattr(ctx, 'current_tc_name'):
                                shutil.move(file_name,
                                            os.path.join(environment.logdir, "tech-support", f"tech_support_{ctx.current_tc_name}.tgz"))
                            else:
                                shutil.move(file_name, environment.logdir)
                            break
                else:
                    Logger.warn(f"Failed to generate/collect techsupport file")
            else:
                Logger.warn(f"Missing tech-support-tool information, no additional logs collected")
            pytest.fail(message)

    @staticmethod
    def _rocm_workload_handler(environment, wl_template, op_code, **kwargs):
        """
        create the workload pod requesting gpu(s)
        """

        workload_config = copy.deepcopy(kwargs)
        node_name = workload_config.get("node_name", None)

        if node_name is None:
            # Take one node with gpu
            ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
            K8Helper.triage(environment, ret_code == 0, "gpu-operator failed to find amd/gpu nodes in the cluster")
            gpu_node = gpu_nodes[0]
            node_name = k8_util.k8_get_node_hostname(gpu_node)

        if op_code == K8Helper.WorkloadOp.START_WORKLOAD:
            # check gpu capacity
            init_cap, init_alloc = k8_util.k8_get_node_gpu_capacity(node_name)
            K8Helper.triage(environment, int(init_cap) != -1 or int(init_alloc) != -1,
                            f'Err getting gpu capacity and allocatable values: capacity: {init_cap} allocatable: {init_alloc}')

            # check if the node has allocatable gpus; if not fail
            K8Helper.triage(environment, init_cap != 0 or init_alloc != 0, f'no gpu available')

            # create a workload requesting one gpu
            pod_name = f"gpu-workload-{node_name}-{common.generate_8byte_sha(node_name)}"

            #launch
            Logger.info(f"Create the workload with gpu")
            workload_config.update(
                {
                    'pod_name' : pod_name,
                    'podStatus' : K8Helper.PodStatus.NA,
                    'nodeSelector' : node_name,
                })
            wl_file = os.path.join(environment.logdir, f"{pod_name}.yaml")
            Logger.debug(f"New workload specification : {workload_config}")
            cr_spec = spec_util.generate_k8_workload_template(wl_template['spec'], workload_config, wl_file)
            ret_code, ret_stdout, ret_stderr = k8_util.k8_apply_cr(cr_spec, wl_file)

            workload_pods = [
                common.PodInfo(pod_name, 1, 1),
            ]
            if workload_config.get("no_look", False):
                return None
            expected_status = workload_config.get("expected_status", K8Helper.PodStatus.RUNNING)
            max_wait = workload_config.get("timeout", 1200)
            poll_interval = 30
            elapsed = 0
            for _ in range(max_wait // poll_interval):
                status_info = k8_util.k8_check_pod_status(cr_spec['metadata']['namespace'], workload_pods)
                Logger.debug(f"workload pod status: {status_info}")
                for pod_name, (status, full_pod_info) in status_info.items():
                    if pod_name == workload_config['pod_name']:
                        if status == 'Running':
                            workload_config['podStatus'] = K8Helper.PodStatus.RUNNING
                        elif status == 'Pending':
                            workload_config['podStatus'] = K8Helper.PodStatus.PENDING
                        elif status == 'Failed':
                            workload_config['podStatus'] = K8Helper.PodStatus.FAILED
                        else:
                            Logger.warn(f"workload pod status unknown, pod-name: {pod_name}")
                            workload_config['podStatus'] = K8Helper.PodStatus.UNKNOWN
                if workload_config['podStatus'] == expected_status:
                    Logger.info(f"Workload reached expected status: {expected_status} after {elapsed}s")
                    break
                if workload_config['podStatus'] == K8Helper.PodStatus.FAILED:
                    Logger.warn(f"Workload pod failed after {elapsed}s")
                    break
                time.sleep(poll_interval)
                elapsed += poll_interval
                if elapsed % 120 == 0:
                    Logger.info(f"Waiting for workload pod ({elapsed}s/{max_wait}s)...")
            workload_config['spec'] = cr_spec
            return workload_config
        elif op_code == K8Helper.WorkloadOp.STOP_WORKLOAD:
            # delete the workload
            Logger.info(f"Delete the workload with config: {workload_config}")
            ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_cr(workload_config['spec'], None)
            if ret_code != 0:
                Logger.warn(f"Failed to delete workload : {workload_config}")
                workload_config['podStatus'] = K8Helper.PodStatus.UNKNOWN
            else:
                workload_config['podStatus'] = K8Helper.PodStatus.STOPPED
            return workload_config
        return None

    @staticmethod
    def _test_runner_job_handler(environment, op_code, **kwargs):
        """
        create the test-runner job on gpu(s)
        """
        workload_config = copy.deepcopy(kwargs)
        if op_code == K8Helper.WorkloadOp.START_WORKLOAD:
            # TODO: recipe selection based on gpu_series
            #if gpu_series and 'MI2' in gpu_series and recipe == "iet_stress":
                #recipe = "iet_single"
            recipe = "gst_single"
            trigger = "MANUAL"
            Logger.info(f"Start test-runner job: {workload_config}")
            node_name = workload_config.get("node_name", None)

            images = workload_config.get("images", None)
            assert images != None, f"Running test-runner as way to load GPU need images/fixture"
            job_name = f"test-runner-manual-trigger-{common.generate_8byte_sha(node_name)}"
            namespace = environment.gpu_operator_namespace

            worker = workload_config.get("worker", node_name)
            framework = workload_config.get("framework", "RVS")

            configmap = {}
            K8Helper.update_test_runner_configmap(recipe, worker, configmap, framework, trigger)

            namespace = environment.gpu_operator_namespace
            sa_name = "test-run"
            cluster_role_name = "test-run-cluster-role"
            crb_name = 'test-run-rb'

            # Create ServiceAccount
            ret_code, ret_stdout, ret_stderr = k8_util.k8_create_service_account(sa_name, namespace)
            K8Helper.triage(environment, (ret_code == 0),
                            f"Failed to create service-account, error:{ret_stderr}")

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
            ret_code, ret_stdout, ret_stderr = k8_util.k8_create_cluster_role(cluster_role_name, rules)
            K8Helper.triage(environment, (ret_code == 0),
                            f"Failed to create test_runner clusterrole with GET, error:{ret_stderr}")

            ret_code, ret_stdout, ret_stderr = k8_util.k8_create_role_binding(crb_name, namespace, cluster_role_name, sa_name)
            K8Helper.triage(environment, (ret_code == 0),
                            f"Failed to create test_runner clusterrole with verbs, error:{ret_stderr}")
            # Create token for ServiceAccount
            token = k8_util.k8_create_token(namespace, sa_name, "1h")
            K8Helper.triage(environment, token is not None,
                            f"Failed to create token for the service-account : {sa_name}")
            Logger.info(f"TOKEN={token}")

            # Create Job
            k8_util.k8_create_test_runner_job(namespace,
                                              images,
                                              node_name,
                                              sa_name,
                                              job_name,
                                              True,
                                              False, None)

            sa_name = "test-run"
            cluster_role_name = "test-run-cluster-role"
            crb_name = 'test-run-rb'

            workload_config.update(
                {
                    'pod_name' : job_name,
                    'podStatus' : K8Helper.PodStatus.NA,
                })

            for _ in range(5):
                status_info = k8_util.k8_check_pod_status(namespace, job_name)
                Logger.debug(f"test-runner pod status: {status_info}")
                for pod_name, (status, full_pod_info) in status_info.items():
                    if workload_config['pod_name'] in pod_name:
                        if status == 'Running':
                            workload_config['podStatus'] = K8Helper.PodStatus.RUNNING
                            break
                        elif status == 'Pending':
                            workload_config['podStatus'] = K8Helper.PodStatus.PENDING
                        elif status == 'Failed':
                            workload_config['podStatus'] = K8Helper.PodStatus.FAILED
                            break
                        elif status == 'Completed':
                            workload_config['podStatus'] = K8Helper.PodStatus.SUCCEEDED
                            break
                        else:
                            Logger.warn(f"workload pod status unknown, pod-name: {pod_name}")
                            workload_config['podStatus'] = K8Helper.PodStatus.UNKNOWN
                    time.sleep(5) # give time for image download
            return workload_config
        elif op_code == K8Helper.WorkloadOp.STOP_WORKLOAD:
            Logger.info(f"Delete test-runner job: {workload_config}")
            k8_util.k8_delete_job(environment.gpu_operator_namespace, workload_config['pod_name'])
            workload_config['podStatus'] = K8Helper.PodStatus.UNKNOWN
            return workload_config

    @staticmethod
    def workload_operation(environment, op_code, **kwargs):
        """
        create the workload pod
        """
        workload_selection = kwargs.get("workload_selection", environment.default_workload)
        if workload_selection == "test-runner-manual-job":
            return K8Helper._test_runner_job_handler(environment, op_code, **kwargs)
        else:
            with open("lib/files/workload-specs.json", "r") as fp:
                wl_templates = json.load(fp)
            tmp_list = list(filter(lambda x: x['workload-type'] == workload_selection, wl_templates['workload-specs']))
            assert len(tmp_list) == 1, f"No workload found with workload-type={workload_selection}"
            return K8Helper._rocm_workload_handler(environment, tmp_list[0], op_code, **kwargs)

    @staticmethod
    def delete_debug_pods(namespaces) -> None:
        for namespace in namespaces:
            k8_util.k8_delete_all_pods_with_name_pattern(namespace, "node-debug-")
            k8_util.k8_delete_all_pods_with_name_pattern(namespace, "curl-cmd-pod-")
            k8_util.k8_delete_all_pods_with_name_pattern(namespace, "gpu-workload-")
            k8_util.k8_delete_all_pods_with_name_pattern(namespace, "techsupport-")
            k8_util.k8_delete_all_pods_with_name_pattern(namespace, "test-runner-manual-trigger-")
        return

    @staticmethod
    def watch_for_daemon_rollout(environment, namespace, gpu_nodes_count):
        # Check for daemon rollout to complete and PODs to be started and completed
        ds_status = k8_util.k8_watch_daemon_set_rollout(namespace) 
        
        if all(ds_status.values()) : 
            return 

        Logger.warning(f"Daemonset rollout did not complete in namespace '{namespace}'. Checking pod statuses...")
        ds_pods = [common.PodInfo(ds_name, gpu_nodes_count, 1) for ds_name in ds_status]
        unhealthy_pods = []
        status_summary = {}

        status_info = k8_util.k8_check_pod_status(namespace, ds_pods)
        for name, (phase, full_pod_info) in status_info.items():
            status_summary[name] = phase
            if  K8Helper.is_unhealthy_pod(full_pod_info):
                unhealthy_pods.append(full_pod_info)

        if unhealthy_pods:
            if getattr(environment, 'context', None) is not None:
                setattr(environment.context, 'unhealthy_pods', unhealthy_pods)
            K8Helper.triage(environment, False, f"Daemonset rollout failed in '{namespace}': {len(unhealthy_pods)} pods are unhealthy.")

    @staticmethod
    def is_unhealthy_pod(pod: dict) -> bool:
        metadata = pod.get('metadata', {})
        status = pod.get("status", {})
        phase = status.get('phase', 'Unknown')
        name = metadata.get('name', '')

        if metadata.get('deletionTimestamp'):
            Logger.warning(f"Pod {name} is being deleted.")
            return True
        if phase in {'Failed', 'Unknown'}:
            Logger.error(f"Pod {name} is in critical phase: {phase}")
            return True

        bad_reasons = {'CrashLoopBackOff', 'ImagePullBackOff', 'ErrImagePull', 'CreateContainerConfigError', 
                    'CreateContainerError', 'RunContainerError', 'ContainerCreating'}
        
        container_statuses = status.get('initContainerStatuses', []) + status.get('containerStatuses', [])
        for cs in container_statuses:
            waiting = cs.get('state', {}).get('waiting', {})
            reason = waiting.get('reason')
            if reason in bad_reasons:
                Logger.error(f"Pod {name} container '{cs.get('name')}' error: {reason}")
                return True
        if phase == 'Pending':
            msg = next((c.get('message') for c in status.get('conditions', []) if c.get('type') == 'PodScheduled' and c.get('status') == 'False'), 
                    "Waiting for resources/init")
            Logger.error(f"Pod {name} is Pending: {msg}")
            return True
        if phase == 'Running':
            if not any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in status.get('conditions', [])):
                Logger.error(f"Pod {name} is Running but not Ready.")
                return True
        return False

    @staticmethod
    def collect_unhealthy_pods(environment, pods):
        unhealthy_pods = []
        status_info = k8_util.k8_check_pod_status("default", pods)
        for name, (phase, full_pod_info) in status_info.items():
            if  K8Helper.is_unhealthy_pod(full_pod_info):
                unhealthy_pods.append(full_pod_info)

        if unhealthy_pods and getattr(environment, 'context', None) is not None:
            setattr(environment.context, 'unhealthy_pods', unhealthy_pods)
        return unhealthy_pods

    @staticmethod
    def log_deviceconfig_state(environment, devicecfg_name, checkpoint_description):
        """
        Log the current state of a DeviceConfig CR at a checkpoint during test execution.

        This helper captures and logs critical fields from the DeviceConfig CR including:
        - spec.driver.version
        - spec.driver.upgradePolicy.enable
        - metadata.resourceVersion
        - metadata.generation
        - status.nodeModuleStatus (per-node upgrade status)

        Use this at key checkpoints during driver upgrade tests to track when/if
        the CR state changes unexpectedly.

        Args:
            environment: Test environment fixture
            devicecfg_name: Name of the DeviceConfig CR to inspect
            checkpoint_description: Description of the checkpoint (e.g., "After CR modification",
                                   "Before waiting for upgrade", "After timeout")

        Example:
            K8Helper.log_deviceconfig_state(environment, "test-deviceconfig",
                                           "After modifying driver version to 30.20")
        """
        Logger.info(f"===== DeviceConfig State Checkpoint: {checkpoint_description} =====")

        devcfg_info = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace, devicecfg_name)
        if not devcfg_info or devicecfg_name not in devcfg_info:
            Logger.error(f"Failed to retrieve DeviceConfig '{devicecfg_name}' at checkpoint: {checkpoint_description}")
            return

        cfg = devcfg_info[devicecfg_name]

        # Log critical spec fields
        driver_version = cfg.get('spec', {}).get('driver', {}).get('version', 'N/A')
        upgrade_enable = cfg.get('spec', {}).get('driver', {}).get('upgradePolicy', {}).get('enable', 'N/A')

        Logger.info(f"DeviceConfig: {devicecfg_name}")
        Logger.info(f"  spec.driver.version: {driver_version}")
        Logger.info(f"  spec.driver.upgradePolicy.enable: {upgrade_enable}")

        # Log metadata for tracking CR modifications
        metadata = cfg.get('metadata', {})
        Logger.info(f"  metadata.resourceVersion: {metadata.get('resourceVersion', 'N/A')}")
        Logger.info(f"  metadata.generation: {metadata.get('generation', 'N/A')}")

        # Log node-level upgrade status if available
        node_status = cfg.get('status', {}).get('nodeModuleStatus', {})
        if node_status:
            Logger.info(f"  Node Upgrade Status:")
            for node_name, node_info in node_status.items():
                status = node_info.get('status', 'N/A')
                container_image = node_info.get('containerImage', 'N/A')
                Logger.info(f"    {node_name}: {status} (image: {container_image})")
        else:
            Logger.info(f"  Node Upgrade Status: Not available")

        Logger.info(f"===== End DeviceConfig State =====")

    @staticmethod
    def collect_additional_diagnostics(environment, devicecfg_list, gpu_nodes, output_dir):
        """
        Collect additional diagnostic information not captured by tech-support tool.

        This helper collects:
        1. KMM worker pod logs (both current and previous if crashed)
        2. GPU operator controller logs
        3. DeviceConfig CR snapshots for each deviceconfig

        Saves all diagnostics to the specified output directory.

        Args:
            environment: Test environment fixture
            devicecfg_list: List of DeviceConfig names to snapshot
            gpu_nodes: List of GPU node dictionaries from k8_get_gpu_nodes()
            output_dir: Directory path to save diagnostic files

        Example:
            K8Helper.collect_additional_diagnostics(environment,
                                                   deviceconfig_install.devicecfg_list,
                                                   gpu_nodes,
                                                   environment.logdir)
        """
        Logger.info(f"===== Collecting Additional Diagnostics to {output_dir} =====")

        # Create diagnostics directory if it doesn't exist
        os.makedirs(output_dir, exist_ok=True)

        # 1. Collect KMM worker pod logs
        Logger.info("Collecting KMM worker pod logs...")
        for node in gpu_nodes:
            node_name = node['metadata']['name']
            for devcfg_name in devicecfg_list:
                # KMM worker pod naming: kmm-worker-{node-name}-{deviceconfig-name}
                kmm_pod_name = f"kmm-worker-{node_name}-{devcfg_name}"

                # Try to get current logs
                log_file = os.path.join(output_dir, f"{kmm_pod_name}-current.log")
                ret_code, logs, _ = k8_util.k8_get_pod_logs(kmm_pod_name, environment.gpu_operator_namespace, since="180s", container=None, previous=False)
                if ret_code == 0 and logs:
                    with open(log_file, 'w') as f:
                        f.write(logs)
                    Logger.info(f"  Saved current logs for {kmm_pod_name}")
                else:
                    Logger.debug(f"  No current logs available for {kmm_pod_name}")

                # Try to get previous logs (if pod crashed/restarted)
                prev_log_file = os.path.join(output_dir, f"{kmm_pod_name}-previous.log")
                ret_code, prev_logs, _ = k8_util.k8_get_pod_logs(kmm_pod_name, environment.gpu_operator_namespace, since="180s", container=None, previous=True)
                if ret_code == 0 and prev_logs:
                    with open(prev_log_file, 'w') as f:
                        f.write(prev_logs)
                    Logger.info(f"  Saved previous logs for {kmm_pod_name} (pod had restarted)")
                else:
                    Logger.debug(f"  No previous logs available for {kmm_pod_name}")

        # 2. Collect GPU operator controller logs
        Logger.info("Collecting GPU operator controller logs...")
        operator_pod_name = k8_util.k8_get_pod_name("gpu-operator-controller", environment.gpu_operator_namespace)
        if operator_pod_name:
            operator_log_file = os.path.join(output_dir, "gpu-operator-controller.log")
            ret_code, logs, _ = k8_util.k8_get_pod_logs(operator_pod_name, environment.gpu_operator_namespace, since="180s", container=None, previous=False)
            if ret_code == 0 and logs:
                with open(operator_log_file, 'w') as f:
                    f.write(logs)
                Logger.info(f"  Saved operator controller logs")
            else:
                Logger.warn(f"  Failed to collect operator controller logs")
        else:
            Logger.warn(f"  GPU operator controller pod not found")

        # 3. Snapshot DeviceConfig CRs
        Logger.info("Snapshotting DeviceConfig CRs...")
        for devcfg_name in devicecfg_list:
            devcfg_info = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace, devcfg_name)
            if devcfg_info and devcfg_name in devcfg_info:
                snapshot_file = os.path.join(output_dir, f"deviceconfig-{devcfg_name}-snapshot.json")
                with open(snapshot_file, 'w') as f:
                    json.dump(devcfg_info[devcfg_name], f, indent=2)
                Logger.info(f"  Saved DeviceConfig snapshot for {devcfg_name}")
            else:
                Logger.warn(f"  Failed to snapshot DeviceConfig {devcfg_name}")

        Logger.info(f"===== Additional Diagnostics Collection Complete =====")

    @staticmethod
    def capture_rocm_version(namespace, pod_str, image_container_pairs):
        """
        Parse ROCm version from LD_LIBRARY_PATH inside running containers and store
        results in pytest._image_info so the HTML report Images table shows the ROCm
        build version baked into each image.

        Works on both Kubernetes and OpenShift (uses the standard pod exec API).

        Args:
            namespace:             Kubernetes namespace where the pod runs.
            pod_str:               Substring used to locate the pod (passed to k8_get_pod_name).
            image_container_pairs: List of (image_key_prefix, container_name) tuples, e.g.
                                   [("metricsExporter.image", "metrics-exporter-container"),
                                    ("configManager.image",   "device-config-manager-container")]
        """
        if not hasattr(pytest, "_image_info"):
            return

        try:
            pod_name = k8_util.k8_get_pod_name(pod_str, namespace)
        except Exception as e:
            Logger.warning(f"capture_rocm_version: failed to locate pod '{pod_str}' in {namespace}: {e}")
            return

        if not pod_name:
            Logger.warning(f"capture_rocm_version: pod matching '{pod_str}' not found in {namespace}")
            return

        for image_key, container_name in image_container_pairs:
            try:
                rc, out, err = k8_util.exec_command_in_pod(namespace, ["printenv", "LD_LIBRARY_PATH"], pod_name, container_name)
                if rc == 0 and out and out.strip():
                    match = re.search(r'/opt/rocm-([^/:]+)/', out)
                    if match:
                        rocm_version = match.group(1)
                        pytest._image_info[f"{image_key}.rocm_version"] = rocm_version
                        Logger.info(f"ROCm version for {image_key}: {rocm_version}")
                        continue
                    Logger.warning(f"ROCm version pattern not found in LD_LIBRARY_PATH for {container_name} ({pod_name}): {out.strip()!r}")
                else:
                    Logger.warning(f"ROCm version not found in LD_LIBRARY_PATH for {container_name} ({pod_name}): rc={rc} err={err}")
            except Exception as e:
                Logger.warning(f"capture_rocm_version: exec failed for {container_name} ({pod_name}): {e}")

