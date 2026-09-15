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
import lib.metric_util as metric_util
from lib.util import K8Helper

#pytestmark = pytest.mark.skip("debugging")
Logger = logging.getLogger("k8.test_metrics_exporter")
LogPrettyPrinter = pprint.PrettyPrinter(indent = 2)


def test_exporter_helmchart_clusterip_deploy(request, gpu_cluster, images, environment):
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

    options = {
        "service.type" : "ClusterIP",
    }
    values_yaml = os.path.join(environment.logdir, f"exporter_values_{environment.context.current_tc_name}.yaml")
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

def test_exporter_helmchart_clusterip_custom_deploy(request, gpu_cluster, images, environment):
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

    options = {
        "service.type" : "ClusterIP",
        "service.ClusterIP.port" : 4999,
    }
    values_yaml = os.path.join(environment.logdir, f"exporter_values_{environment.context.current_tc_name}.yaml")
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
        ret_code, ret_stdout, ret_stderr = k8_util.k8_run_curl_cmd(gpu_cluster, ["-s", f"http://{ip}:5000/metrics"])
        if ret_code == 0:
            failed_endpoints.add(host_ip_port)
            Logger.error(f"Able to collect metrics from nodeport endpoint for {ip}:5000, stdout: {ret_stdout} stderr: {ret_stderr}")

        ret_code, ret_stdout, ret_stderr = k8_util.k8_run_curl_cmd(gpu_cluster, ["-s", f"http://{ip}:{port}/metrics"])
        if ret_code != 0:
            failed_endpoints.add(host_ip_port)
            Logger.error(f"Failed to get metrics from nodeport endpoint for {host_ip_port}, stdout: {ret_stdout} stderr: {ret_stderr}")

    K8Helper.triage(environment, (len(failed_endpoints) == 0), f"One or more metric endpoints HTTP-GET failed, nodes: {failed_endpoints}")

def test_exporter_helmchart_nodeport_deploy(request, gpu_cluster, images, environment):
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

    values_yaml = os.path.join(environment.logdir, f"exporter_values_{environment.context.current_tc_name}.yaml")
    options = {
        "service.type" : "NodePort",
    }
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

    failed_endpoints = set()
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            ret_code, ret_stdout, ret_stderr = node.http_get(32500, "metrics")
            if ret_code != 0:
                failed_endpoints.add(node.ip_address)
                Logger.error(f"Failed to get metrics from nodeport endpoint for {node.ip_address}, stdout: {ret_stdout} stderr: {ret_stderr}")
    K8Helper.triage(environment, (len(failed_endpoints) == 0),
                    f"One or more metric endpoints HTTP-GET failed, nodes: {failed_endpoints}")

def test_exporter_helmchart_nodeport_custom_deploy(request, gpu_cluster, images, environment):
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

    values_yaml = os.path.join(environment.logdir, f"exporter_values_{environment.context.current_tc_name}.yaml")
    options = {
        "service.type" : "NodePort",
        "service.NodePort.nodePort" : 32600,
        "service.NodePort.port" : 4999,
    }
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

    failed_endpoints = set()
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            ret_code, ret_stdout, ret_stderr = node.http_get(32500, "metrics")
            if ret_code == 0:
                failed_endpoints.add(node.host_name)
                Logger.error(f"Able to collect metrics from nodeport endpoint for {node.ip_address}:32500, stdout: {ret_stdout} stderr: {ret_stderr}")
            ret_code, ret_stdout, ret_stderr = node.http_get(32600, "metrics")
            if ret_code != 0:
                failed_endpoints.add(node.ip_address)
                Logger.error(f"Failed to collect metrics from nodeport endpoint for {node.ip_address}:32600, stdout: {ret_stdout} stderr: {ret_stderr}")
    K8Helper.triage(environment, (len(failed_endpoints) == 0),
                    f"One or more metric endpoints HTTP-GET failed, nodes: {failed_endpoints}")

def test_exporter_nodeport_exp_config(request, gpu_cluster, images, environment):
    global Logger
    # Generate set of config-maps in the k8 cluster with different set of labels and metrics

    exporter_release_name = "device-metrics-exporter"
    exporter_config_defn = {}
    label_support_info = metric_util.get_label_details(environment.exporter_version)
    non_mandatory_labels = list(filter(lambda x: label_support_info[x] == "no", label_support_info.keys()))
    mandatory_labels = list(filter(lambda x: label_support_info[x] == "yes", label_support_info.keys()))

    # Build common list of metrics across all nodes in the cluster (if different gpu-series are part of cluster)
    list_of_metrics_set = []
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            metrics_data = metric_util.get_supported_metrics(gpu_series = node.gpu_series,
                                                             skip_profiler_metrics = False,
                                                             amdgpu_driver = node.amdgpu_driver_version,
                                                             dme_version = environment.exporter_version,
                                                             num_gpus = node.num_gpus)
            list_of_metrics_set.append(set(map(lambda x: x['name'].split(":")[0].lower(), metrics_data)))
    common_metrics = list(functools.reduce(lambda s1, s2: s1.intersection(s2), list_of_metrics_set))
    Logger.info(f"Using {common_metrics} for metrics-exporter configmap validation")

    for idx in range(10):
        label_subset = random.sample(non_mandatory_labels, 5)
        metric_subset = random.sample(common_metrics, 5)
        config_map = {
            "GPUConfig" : {
                "Labels" : label_subset,
                "Fields" : metric_subset,
                "ProfilerMetrics": {
                    "all": True,
                }
            },
        }
        exp_config_name = f"exporter-config-{idx}"
        configmap_file = os.path.join(environment.logdir, f"{exp_config_name}.json")
        with open(configmap_file, "w") as fp:
            fp.write(json.dumps(config_map, indent=4))

        configmap_file = os.path.join(environment.logdir, f"config.json")
        with open(configmap_file, "w") as fp:
            fp.write(json.dumps(config_map, indent=4))

        # Delete if there is any previous instance with same name
        ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_configmap(environment.exporter_namespace, 
                                                                       exp_config_name)
        Logger.debug(f"Result of configmap delete operation, ret_code:{ret_code}, ret_stdout: {ret_stdout.strip()}, err: {ret_stderr.strip()}")
        # ignore ret_code
        ret_code, ret_stdout, ret_stderr = k8_util.k8_create_configmap(environment.exporter_namespace,
                                                                       exp_config_name, configmap_file, "config.json")
        K8Helper.triage(environment, ret_code == 0,
                        f"Failed to create configmap {exp_config_name} for {configmap_file}, err: {ret_stderr.strip()}")
        exporter_config_defn[exp_config_name] = (label_subset, metric_subset)
        Logger.info(f"Created configmap {exp_config_name} with labels: {label_subset} and metrics: {metric_subset}")

    def _cleanup_configmap():
        for exp_config, _ in exporter_config_defn.items():
            # Delete
            ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_configmap(environment.exporter_namespace, 
                                                                           exp_config)
            if ret_code != 0:
                Logger.warn(f"Failed to delete metrics-exporter configmap {exp_config}")
        return

    request.addfinalizer(_cleanup_configmap)

    def _uninstall_exporter_helmchart():
        ret_code, ret_stdout, ret_stderr = helm_util.helm_uninstall(gpu_cluster, exporter_release_name, environment.exporter_namespace)
        if ret_code != 0:
            helm_util.helm_cleanup(gpu_cluster, exporter_release_name, environment.exporter_namespace)
    request.addfinalizer(_uninstall_exporter_helmchart)
    _uninstall_exporter_helmchart()

    failed_exp_config_metrics = []
    failed_exp_config_labels = []
    failed_endpoints = set()
    for exp_config, label_metrics_tuple in exporter_config_defn.items():
        Logger.info(f"Testing with exporter-config {exp_config}")

        values_yaml = os.path.join(environment.logdir, f"exporter_values_{environment.context.current_tc_name}.yaml")
        options = {
            "service.type" : "NodePort",
            'configMap' : exp_config,
        }
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
        time.sleep(30) # Wait for config-map is read by exporter pod

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
        expected_metrics = set(label_metrics_tuple[1])
        expected_metrics.update(['promhttp_metric_handler_errors_total'])
        expected_labels = set(label_metrics_tuple[0])
        expected_labels.update(mandatory_labels)
        for node in gpu_cluster.cluster_nodes:
            if not node.is_gpu_node():
                continue
            ret_code, resp, _ = node.http_get(32500, "metrics")

            if ret_code != 0:
                Logger.error(f"Failed to get metrics from nodeport endpoint for {node.ip_address}, stdout: {ret_stdout} stderr: {ret_stderr}")
                failed_endpoints.add(node.ip_address)
                continue
            metric_util.dump_metrics(resp, os.path.join(environment.logdir, f"{node.ip_address}_{exp_config}_metrics.txt"))
            obs_metric_info = metric_util.parse_metric_data(resp)
            obs_metrics = set(obs_metric_info.keys())

            # To ignore any contingent metric(s), adding to obs_metrics
            for metric in expected_metrics:
                if metric in {'promhttp_metric_handler_errors_total', 'gpu_nodes_total'}:
                    continue
                if metric_util.is_metric_contingent(metric):
                    obs_metrics.add(metric)

            # Check for metrics
            if obs_metrics != expected_metrics:
                Logger.error(f"Mismatch in metrics Expected : {expected_metrics} vs Observed : {obs_metrics} config-map:{exp_config}")
                if expected_metrics - obs_metrics:
                    Logger.error(f"Missing: {expected_metrics - obs_metrics}")
                if obs_metrics - expected_metrics:
                    Logger.error(f"Unexpected: {obs_metrics - expected_metrics}")
                failed_exp_config_metrics.append((exp_config, f"Expected:{expected_metrics}, Observed:{obs_metrics}"))

            # Check for labels associated with each exported metric
            for metric_name, metric_data_list in obs_metric_info.items():
                if metric_name in {'promhttp_metric_handler_errors_total', 'gpu_nodes_total'}:
                    continue
                label_check_failed = False
                for metric_data in metric_data_list:
                    observed_labels = set(metric_data['labels'].keys())
                    if len(expected_labels - observed_labels) > 0:
                        Logger.error(f"Missing labels with config-map:{exp_config}, error: {expected_labels - observed_labels}")
                        label_check_failed = True
                if label_check_failed and exp_config not in failed_exp_config_labels:
                    failed_exp_config_labels.append((exp_config, f"Expected:{expected_labels}, Observed:{observed_labels}"))
        _uninstall_exporter_helmchart()

    # Do final verification
    K8Helper.triage(environment, len(failed_endpoints) == 0, f"One or more metric endpoints HTTP-GET failed, nodes: {failed_endpoints}")
    K8Helper.triage(environment, (len(failed_exp_config_metrics) == 0),
                    f"Export ConfigMap (Fields) failed for {failed_exp_config_metrics} cases")
    K8Helper.triage(environment, (len(failed_exp_config_labels) == 0),
                    f"Export ConfigMap (Labels) failed for {failed_exp_config_labels} cases")

def test_exporter_all_supported_metrics(request, gpu_cluster, images, environment):
    """
    Testcase to check if all metrics supported for each of gpu-series is observed in the curl output of exporter endpoint
    """
    global Logger
    global LogPrettyPrinter

    def _test_if_metrics_exported(metric_to_test, gpu_id, exporter_metrics):
        metric_metadata = metric_util.get_metric_metadata(metric_to_test)
        metric_types = metric_metadata.get("type", {})
        if 'labeled' in metric_types.keys():
            if ':' in metric_to_test:
                label_name = metric_types['labeled']['label']
                metric_name, label_value = metric_to_test.split(":")
                m_info_list = []
                for _, entry in enumerate(exporter_metrics[metric_name.lower()]):
                    if entry['labels']['gpu_id'] != str(gpu_id):
                        continue
                    K8Helper.triage(environment, (label_name in entry['labels']),
                                    f"Label {label_name} missing in exported metrics {entry}, {metric_metadata}")
                    lval = entry['labels'][label_name]
                    if lval.lower() != label_value.lower():
                        continue
                    m_info_list.append(entry)

                Logger.debug(f"Found total {len(m_info_list)} exported metrics for {metric_to_test}")
                if len(m_info_list) > 0:
                    Logger.info(f"Found {len(m_info_list)} entries of {metric_to_test}")
                    return True
        elif 'array' in metric_types.keys():
            label_name = metric_types['array']['label']
            m_info_list = []
            for _, entry in enumerate(exporter_metrics[metric_to_test.lower()]):
                if entry['labels']['gpu_id'] != str(gpu_id):
                    continue
                K8Helper.triage(environment, (label_name in entry['labels']),
                                f"Label {label_name} missing in exported metrics {entry}, {metric_metadata}")
                m_info_list.append(entry)
            Logger.debug(f"Found total {len(m_info_list)} exported metrics for {metric_to_test}")
            if len(m_info_list) > 0:
                Logger.info(f"Found {len(m_info_list)} entries of {metric_to_test}")
                return True
        else:
            if metric_to_test.lower() in exporter_metrics:
                Logger.info(f"Found {metric_to_test}")
                return True

        if metric_types.get("contingent", "no") == "yes":
            Logger.warning(f"Missing {metric_to_test} - contingent")
            return True

        Logger.error(f"Missing {metric_to_test}")
        return False

    config_map = {
        "GPUConfig" : {
            "ProfilerMetrics": {
                "all": True,
            }
        },
    }
    exporter_release_name = "device-metrics-exporter"
    exp_config_name = f"all-exporter-config"
    configmap_file = os.path.join(environment.logdir, f"{exp_config_name}.json")
    with open(configmap_file, "w") as fp:
        fp.write(json.dumps(config_map, indent=4))

    configmap_file = os.path.join(environment.logdir, f"config.json")
    with open(configmap_file, "w") as fp:
        fp.write(json.dumps(config_map, indent=4))

    # Delete if there is any previous instance with same name
    ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_configmap(environment.exporter_namespace, 
                                                                   exp_config_name)
    Logger.debug(f"Result of configmap delete operation, ret_code:{ret_code}, ret_stdout: {ret_stdout.strip()}, err: {ret_stderr.strip()}")
    # ignore ret_code
    ret_code, ret_stdout, ret_stderr = k8_util.k8_create_configmap(environment.exporter_namespace,
                                                                   exp_config_name, configmap_file, "config.json")
    K8Helper.triage(environment, ret_code == 0,
                    f"Failed to create configmap {exp_config_name} for {configmap_file}, err: {ret_stderr.strip()}")
    Logger.info(f"Created configmap {exp_config_name}")

    def _cleanup_configmap():
        # Delete
        ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_configmap(environment.exporter_namespace, 
                                                                       exp_config_name)
        if ret_code != 0:
            Logger.warn(f"Failed to delete metrics-exporter configmap {exp_config_name}")
        return
    request.addfinalizer(_cleanup_configmap)

    def _uninstall_exporter_helmchart():
        ret_code, ret_stdout, ret_stderr = helm_util.helm_uninstall(gpu_cluster, exporter_release_name, environment.exporter_namespace)
        if ret_code != 0:
            helm_util.helm_cleanup(gpu_cluster, exporter_release_name, environment.exporter_namespace)
    request.addfinalizer(_uninstall_exporter_helmchart)
    _uninstall_exporter_helmchart()

    values_yaml = os.path.join(environment.logdir, f"exporter_values_{environment.context.current_tc_name}.yaml")
    options = {
        "service.type" : "NodePort",
        'configMap' : exp_config_name,
    }
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
    time.sleep(30) # Wait for config-map is read by exporter pod

    failed_metrics = defaultdict(set)
    failed_endpoints = set()
    for node in gpu_cluster.cluster_nodes:
        if not node.is_gpu_node():
            continue

        # Collect metrics from given node
        ret_code, ret_stdout, ret_stderr = node.http_get(32500, "metrics")
        if ret_code != 0:
            failed_endpoints.add(node.ip_address)
            Logger.error(f"Failed to get metrics from nodeport endpoint for {node.ip_address}, stdout: {ret_stdout} stderr: {ret_stderr}")
            continue
        node_metrics = metric_util.parse_metric_data(ret_stdout)
        metric_util.dump_metrics(ret_stdout, os.path.join(environment.logdir, f"{node.ip_address}_{exp_config_name}_metrics.txt"))

        supported_metrics = metric_util.get_supported_metrics(gpu_series = node.gpu_series,
                                                              skip_profiler_metrics = False,
                                                              amdgpu_driver = node.amdgpu_driver_version,
                                                              dme_version = environment.exporter_version,
                                                              num_gpus = node.num_gpus)
        Logger.info(f"Node: {node.host_name} having {node.gpu_series} has {len(supported_metrics)} metrics")
        for entry in supported_metrics:
            metric_to_test = entry['name']
            Logger.info(f"Checking {metric_to_test} among exported metrics for node {node.host_name}")
            for gpu_id in range(node.num_gpus):
                if _test_if_metrics_exported(metric_to_test, gpu_id, node_metrics) == False:
                    Logger.error(f"Idle Conditions Metrics: {metric_to_test} failed for {gpu_id}")
                    failed_metrics[metric_to_test].add(gpu_id)

    K8Helper.triage(environment, (len(failed_endpoints) == 0), f"One or more metric endpoints HTTP-GET failed, nodes: {failed_endpoints}")
    K8Helper.triage(environment, (len(failed_metrics) == 0),
                    f"Metics validation failed: {list(failed_metrics.keys())} from exported-metrics\n{LogPrettyPrinter.pformat(failed_metrics)}")

def test_exporter_helmchart_servicemonitor_enable(request, gpu_cluster, images, environment):
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

    options = {
        "service.type" : "ClusterIP",
        "serviceMonitor.enabled" : True,
    }
    values_yaml = os.path.join(environment.logdir, f"exporter_values_{environment.context.current_tc_name}.yaml")
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
        if node.num_gpus:
            pod_name = k8_util.k8_get_pod_name("amdgpu-metrics-exporter", environment.exporter_namespace, node.host_name)
            exporter_pods.append(common.PodInfo(pod_name, 1, 1))
    failed_pods = k8_util.k8_check_pod_running(environment.exporter_namespace, exporter_pods)
    K8Helper.triage(environment, not failed_pods, f"One or more pods are not ready - {failed_pods}")

    ret_code, resp, err = k8_util.k8_get_servicemonitor_cr(environment.exporter_namespace)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to collect servicemonitors from namespace: {environment.exporter_namespace}, error : {err}")
    K8Helper.triage(environment, (len(resp) > 0), f"Found 0 entries of servicemonitors in namespace: {exporter_release_name}")

    # Uninstall exporter helm-chart and check if servicemonitor object is removed
    _uninstall_exporter_helmchart()
    time.sleep(30) # Wait for exporter pods to terminate
    exporter_pods = []
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            pod_name = k8_util.k8_get_pod_name("amdgpu-metrics-exporter", environment.exporter_namespace, node.host_name)
            exporter_pods.append(common.PodInfo(pod_name, 1, 1))
    failed_pods = k8_util.k8_check_pod_terminated(environment.exporter_namespace, exporter_pods)
    K8Helper.triage(environment, not failed_pods, f"One or more pods still running - {failed_pods}")

    ret_code, resp, err = k8_util.k8_get_servicemonitor_cr(environment.exporter_namespace)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to collect servicemonitors from namespace: {environment.exporter_namespace}, error : {err}")
    K8Helper.triage(environment, (len(resp) == 0), f"Found non-zero entries of servicemonitors in namespace: {exporter_release_name}")

def test_exporter_helmchart_pod_annotations(request, gpu_cluster, images, environment):
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

    NUM_ANNOTATIONS = 10
    options = {
        "service.type" : "ClusterIP",
        "podAnnotations" : {
            f"pod-annotation-{i}" : f"pod-value-{i}" for i in range(NUM_ANNOTATIONS)
        },
    }
    values_yaml = os.path.join(environment.logdir, f"exporter_values_{environment.context.current_tc_name}.yaml")
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

    # Check for dme pods
    ret_code, pods = k8_util.k8_get_pods(environment.exporter_namespace)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to to collect pods from {environment.exporter_namespace}")
    for pod in pods:
        pod_name = pod['metadata']['name']
        if "metrics-exporter" not in pod_name:
            continue

        # this is a metrics-exporter pod
        node_name = pod['spec']['node_name']
        K8Helper.triage(environment, pod["metadata"].get("annotations", None) != None,
                        f"pod : {pod_name} on node {node_name} missing annotations section")
        pod_annots = pod["metadata"]["annotations"]
        for i in range(NUM_ANNOTATIONS):
            lbl = f"pod-annotation-{i}"
            val = f"pod-value-{i}"
            K8Helper.triage(environment, lbl in pod_annots,
                            f"pod : {pod_name} on node {node_name} missing label : {lbl}")
            K8Helper.triage(environment, pod_annots[lbl] == val,
                            f"pod : {pod_name} on node {node_name} label-value mismatch, expected: {lbl}:{val}")


    # Uninstall exporter helm-chart
    _uninstall_exporter_helmchart()
    time.sleep(30) # Wait for exporter pods to terminate
    exporter_pods = []
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            pod_name = k8_util.k8_get_pod_name("amdgpu-metrics-exporter", environment.exporter_namespace, node.host_name)
            exporter_pods.append(common.PodInfo(pod_name, 1, 1))
    failed_pods = k8_util.k8_check_pod_terminated(environment.exporter_namespace, exporter_pods)
    K8Helper.triage(environment, not failed_pods, f"One or more pods still running - {failed_pods}")

def test_exporter_helmchart_service_annotations(request, gpu_cluster, images, environment):
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

    NUM_ANNOTATIONS = 10
    options = {
        "service.type" : "ClusterIP",
        "service.annotations" : {
            f"svc-annotation-{i}" : f"svc-value-{i}" for i in range(NUM_ANNOTATIONS)
        },
    }
    values_yaml = os.path.join(environment.logdir, f"exporter_values_{environment.context.current_tc_name}.yaml")
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

    # Check for dme svc
    ret_code, services, ret_err = k8_util.k8_get_services(environment.exporter_namespace)
    K8Helper.triage(environment, ret_code == 0,
                    f"Failed to get k8-services in {environment.exporter_namespace}, error : {ret_err}")
    K8Helper.triage(environment, len(services) > 0,f"No services found in {environment.exporter_namespace}")
    for svc in services:
        svc_name = svc["metadata"]["name"]
        if "metrics-exporter" in svc_name:
            K8Helper.triage(environment, svc["metadata"].get("annotations", None),
                            f"Missing annotations for svc : {svc_name}")
            svc_annots = svc["metadata"]["annotations"]
            for i in range(NUM_ANNOTATIONS):
                lbl = f"svc-annotation-{i}"
                val = f"svc-value-{i}"
                K8Helper.triage(environment, lbl in svc_annots, f"Missing annotation : {lbl}")
                K8Helper.triage(environment, svc_annots[lbl] == val, f"Mismatch in annotation, expected: {lbl}:{val}")

    # Uninstall exporter helm-chart
    _uninstall_exporter_helmchart()
    time.sleep(30) # Wait for exporter pods to terminate
    exporter_pods = []
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            pod_name = k8_util.k8_get_pod_name("amdgpu-metrics-exporter", environment.exporter_namespace, node.host_name)
            exporter_pods.append(common.PodInfo(pod_name, 1, 1))
    failed_pods = k8_util.k8_check_pod_terminated(environment.exporter_namespace, exporter_pods)
    K8Helper.triage(environment, not failed_pods, f"One or more pods still running - {failed_pods}")
