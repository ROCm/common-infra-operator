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
from collections import defaultdict
import lib.common as common
import lib.helm_util as helm_util
import lib.k8_util as k8_util
import lib.spec_util as spec_util
import lib.npd_util as npd_util
from lib.util import K8Helper

#pytestmark = pytest.mark.skip("debugging")
Logger = logging.getLogger("k8.test_node_problem_detector")
LogPrettyPrinter = pprint.PrettyPrinter(indent = 2)


@pytest.fixture(scope="module")
def deploy_exporter_helmchart(request, gpu_cluster, images, environment):
    global Logger

    # Install exporter helm-chart
    if images.get("exporter.repo", None):
        helm_util.helm_add_repo(gpu_cluster, images.get("exporter.repo-name"), images.get("exporter.repo"))

    exporter_release_name = "device-metrics-exporter"
    def _uninstall_exporter_helmchart():
        ret_code, ret_stdout, ret_stderr = helm_util.helm_uninstall(gpu_cluster, exporter_release_name, environment.exporter_namespace)
        if ret_code != 0:
            helm_util.helm_cleanup(gpu_cluster, exporter_release_name, environment.exporter_namespace)
    request.addfinalizer(_uninstall_exporter_helmchart)
    _uninstall_exporter_helmchart()

    gpu_nodes = [node for node in gpu_cluster.cluster_nodes if node.is_gpu_node()]
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")

    # Check if amdgpuhealth utility is not mounted on each node - /var/lib/amd-metrics-exporter
    for node in gpu_nodes:
        node_name = node.host_name
        cmd = ["ls", "-1", "/var/lib/amd-metrics-exporter/amdgpuhealth"]
        ret_code, resp_stdout = k8_util.run_command_on_node(gpu_cluster, node_name, cmd)
        if ret_code != 0:
            Logger.debug(f"Found /var/lib/amd-metrics-exporter/amdgpuhealth lingering from previous installations: {resp_stdout}")
            cmd = ["sudo", "rm", "-r", "-f", "/var/lib/amd-metrics-exporter"]
            ret_code, resp_stdout = k8_util.run_command_on_node(gpu_cluster, node_name, cmd)
            cmd = ["ls", "-1", "/var/lib/amd-metrics-exporter/amdgpuhealth"]
            ret_code, resp_stdout = k8_util.run_command_on_node(gpu_cluster, node_name, cmd)
        K8Helper.triage(environment, ret_code != 0,
                        f"Utility /var/lib/amd-metrics-exporter/amdgpuhealth found on {node_name} before installation")

    options = {
        "service.type" : "ClusterIP",
    }
    values_yaml = os.path.join(environment.logdir, "exporter_values_deploy_exporter_helmchart.yaml")
    if spec_util.generate_exporter_helmchart_deployment_config(environment.exporter_version, images, values_yaml, **options):
        Logger.debug(f"Generated values.yaml for helm-chart install command, {values_yaml}")
    else:
        values_yaml = None

    ret_code, ret_stdout, ret_stderr = helm_util.helm_install(gpu_cluster, exporter_release_name,
                                                              environment.exporter_namespace,
                                                              images.get('exporter.helm-chart', None),
                                                              environment.exporter_version, values_yaml)
    if ret_code != 0:
        Logger.error(f"Failed to install helm chart for {exporter_release_name}")
        Logger.error(f"Stdout: {ret_stdout.strip()}")
        Logger.error(f"Stderr: {ret_stderr.strip()}")
    K8Helper.triage(environment, (ret_code == 0), f"Failed to install helm-chart for {exporter_release_name}")

    K8Helper.triage(environment,
                    helm_util.is_helm_chart_healthy(gpu_cluster, exporter_release_name, environment.exporter_namespace),
                    "exporter helm-chart is in failed state")
    K8Helper.watch_for_daemon_rollout(environment, environment.exporter_namespace, len(gpu_cluster.cluster_nodes))

    time.sleep(20) # Wait for exporter pods to start working
    exporter_pods = []
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            pod_name = k8_util.k8_get_pod_name("amdgpu-metrics-exporter", environment.exporter_namespace, node.host_name)
            exporter_pods.append(common.PodInfo(pod_name, 1, 1))
    failed_pods = k8_util.k8_check_pod_running(environment.exporter_namespace, exporter_pods)
    K8Helper.triage(environment, not failed_pods, f"One or more pods are not ready - {failed_pods}")

    # Get endpoint for each node
    ret_code, endpoint_values = k8_util.k8_get_endpoints(environment.exporter_namespace)
    K8Helper.triage(environment, (ret_code == 0), f"Error while collecting kubectl endpoints")
    failed_endpoints = set()
    service_name = f"device-metrics-exporter-amd-metrics-exporter-svc"
    K8Helper.triage(environment, (service_name in endpoint_values), f"No endpoint address found for {service_name}")
    K8Helper.triage(environment, (len(endpoint_values[service_name]) > 0),
                    f"No endpoint address found for {service_name}")
    for host_ip_port in endpoint_values[service_name]:
        host, ip, port = host_ip_port
        ret_code, ret_stdout, ret_stderr = k8_util.k8_run_curl_cmd(gpu_cluster, ["-s", f"http://{ip}:{port}/metrics"])
        if ret_code != 0:
            failed_endpoints.add(host_ip_port)
            Logger.error(f"Failed to get metrics from nodeport endpoint for {host_ip_port}, stdout: {ret_stdout} stderr: {ret_stderr}")

    K8Helper.triage(environment, (len(failed_endpoints) == 0), f"One or more metric endpoints HTTP-GET failed, nodes: {failed_endpoints}")
    yield
    _uninstall_exporter_helmchart()
    return

def test_exporter_amdgpuhealth_hostpath(gpu_cluster, deploy_exporter_helmchart, environment):
    global Logger

    gpu_nodes = [node for node in gpu_cluster.cluster_nodes if node.is_gpu_node()]
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")

    # Check if amdgpuhealth utility exists and is executable on each node - /var/lib/amd-metrics-exporter
    for node in gpu_nodes:
        node_name = node.host_name

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

