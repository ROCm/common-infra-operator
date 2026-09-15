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
import re
import time
import json
import logging
import lib.helm_util as helm_util
import lib.k8_util as k8_util
import lib.amdgpu as amdgpu
import lib.common as common
import lib.spec_util as spec_util
import lib.anr_util as anr_util
from lib.util import K8Helper

Logger = logging.getLogger("k8.test_gpu_operator")

@pytest.fixture(autouse=True, scope="module")
def skip_module(environment):
    if environment.deployment_mode == "openshift":
        pytest.skip(f"Skipping gpu-operator testcases for {environment.deployment_mode} deployment")
    return

@pytest.fixture(scope="function")
def gpu_operator_install(gpu_cluster, gpu_operator_release_name, images, environment):
    global Logger

    # cleanup
    devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
    for devcfg_name, _ in devcfg_map.items():
        ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)
        if ret_code != 0:
            Logger.error(f"Failed to delete deviceconfig name: {devcfg_name}, error : {ret_stderr}")
    time.sleep(10)

    if helm_util.is_helm_chart_deployed(gpu_cluster, gpu_operator_release_name, environment.gpu_operator_namespace):
        Logger.warn(f"helm {gpu_operator_release_name} is already deployed - cleanup")
        ret_code, ret_stdout, ret_stderr = helm_util.helm_uninstall(gpu_cluster, gpu_operator_release_name,
                                                                    environment.gpu_operator_namespace)
        if ret_code != 0:
            helm_util.helm_cleanup(gpu_cluster, gpu_operator_release_name, environment.gpu_operator_namespace)
        #k8_util.k8_delete_namespace(environment.gpu_operator_namespace)

    if images.get("gpu-operator.repo", None):
        helm_util.helm_add_repo(gpu_cluster, images.get("gpu-operator.repo-name"), images.get("gpu-operator.repo"))

    secret_list = []
    for entry in gpu_cluster.k8_secrets["secrets"]:
        secret_list.append(entry["name"])
    values_yaml = os.path.join(environment.logdir, f"values_{environment.gpu_operator_version}.yaml")
    if spec_util.generate_helmchart_deployment_config(environment.gpu_operator_version, images, secret_list, values_yaml):
        Logger.debug(f"Generated values.yaml for helm-chart install command, {values_yaml}")
    else:
        values_yaml = None

    options = {
        "crds.defaultCR.install" : "false",
    }

    anr_util.apply_anr_guard(options, Logger)

    ret_code, ret_stdout, ret_stderr = helm_util.helm_install(gpu_cluster, gpu_operator_release_name,
                                                              environment.gpu_operator_namespace,
                                                              images.get('gpu-operator.helm-chart', None),
                                                              environment.gpu_operator_version, values_yaml, **options)
    if ret_code != 0:
        Logger.error(f"Failed to install helm chart for {gpu_operator_release_name}")
        Logger.error(f"Stdout: {ret_stdout}")
        Logger.error(f"Stderr: {ret_stderr}")
    K8Helper.triage(environment, (ret_code == 0), f"Failed to install {gpu_operator_release_name}")
    time.sleep(30)
    yield
    # cleanup - remove any deviceconfigs and then gpu-operator helm-chart
    devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
    for devcfg_name, _ in devcfg_map.items():
        ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)
        if ret_code != 0:
            Logger.error(f"Failed to delete deviceconfig name: {devcfg_name}, error : {ret_stderr}")
    time.sleep(10)

    ret_code, ret_stdout, ret_stderr = helm_util.helm_uninstall(gpu_cluster, gpu_operator_release_name, environment.gpu_operator_namespace)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to uninstall {gpu_operator_release_name} helm-chart, error: {ret_stderr}")
    return

def install_deviceconfig(gpu_cluster, images, environment):
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
            'metricsExporter.enable' : False,
            'testRunner.enable' : False,
        }

    test_config.update(images)

    test_cfg_map = spec_util.build_deviceconfig_cr_template(test_config, gpu_nodes, 'gpu-op', environment.amdgpu_driver_spec)
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
    return devcfg_info

@pytest.fixture(scope="module")
def deviceconfig_install(gpu_cluster, images, gpu_operator_install, environment):
    global Logger

    K8Helper.watch_for_daemon_rollout(environment, environment.gpu_operator_namespace, len(gpu_cluster.cluster_nodes))
    # cleanup
    device_cfg_info = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace, None)
    for devcfg_name, _ in device_cfg_info.items():
        k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)

    devcfg_info = install_deviceconfig(gpu_cluster, images, gpu_operator_install, environment)
    yield devcfg_info

    device_cfg_info = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace, None)
    for devcfg_name, _ in device_cfg_info.items():
        k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)
    return

def test_gpu_operator_install(gpu_cluster, gpu_operator_release_name, gpu_operator_install, environment):
    global Logger

    K8Helper.watch_for_daemon_rollout(environment, environment.gpu_operator_namespace, len(gpu_cluster.cluster_nodes))
    ret_code, ret_stdout, ret_stderr = helm_util.helm_list(gpu_cluster, environment.gpu_operator_namespace)
    K8Helper.triage(environment, (ret_code == 0), "Failed to list helm-charts")

    gpu_operator_running = False
    for chart in json.loads(ret_stdout):
        if (chart['name'] == gpu_operator_release_name and
            chart['status'] == 'deployed'):
            gpu_operator_running = True
    K8Helper.triage(environment, gpu_operator_running,
                    f"helm-chart {gpu_operator_release_name} is not in expected state {json.dumps(ret_stdout, indent=4)}")

    # Check if GPU_OPERATOR_NAMESPACE namespace is created
    ret_code, k8_namespaces = k8_util.k8_get_namespaces()
    K8Helper.triage(environment, (ret_code == 0), "Error checking k8-namespaces from cluster")
    K8Helper.triage(environment,
                    (len(list(filter(lambda x: x['metadata']['name'] == environment.gpu_operator_namespace, k8_namespaces))) == 1),
                    f"Could not find {environment.gpu_operator_namespace} in the cluster")

def test_gpu_operator_check_all_pods(gpu_cluster, gpu_operator_release_name, gpu_operator_install, environment):
    global Logger
    K8Helper.watch_for_daemon_rollout(environment, environment.gpu_operator_namespace, len(gpu_cluster.cluster_nodes))
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0),"gpu-operator failed to find amd/gpu nodes in the cluster")

    # Wait for all pods to be created
    exp_pod_list = [
        common.PodInfo(f'{gpu_operator_release_name}-gpu-operator-charts-controller-manager', 1, 1),
        common.PodInfo(f'{gpu_operator_release_name}-kmm-controller', 1, 1),
        common.PodInfo(f'{gpu_operator_release_name}-kmm-webhook-server', 1, 1),
        common.PodInfo(f'{gpu_operator_release_name}-node-feature-discovery-gc', 1, 1),
        common.PodInfo(f'{gpu_operator_release_name}-node-feature-discovery-master', 1, 1),
        common.PodInfo(f'{gpu_operator_release_name}-node-feature-discovery-worker', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, exp_pod_list)
    K8Helper.triage(environment, not failed_pods, f"One or more pods are not ready - {failed_pods}")

@pytest.mark.skip(reason="reviewing testcases dependencies - skipping for now")
def test_gpu_operator_check_deviceconfig_crd(gpu_cluster, gpu_operator_install, environment):
    global Logger

    deviceconfig_crd = k8_util.k8_get_crd("deviceconfigs.amd.com")
    K8Helper.triage(environment, deviceconfig_crd != None, f"Missing deviceconfigs.amd.com CRD post gpu-operator installation")

    # Check metadata
    if environment.gpu_operator_version != "v1.1.0":
        # TODO: Verify why this is failing for gpu-operator v1.1.0
        metadata = deviceconfig_crd['metadata']
        K8Helper.triage(environment, (environment.gpu_operator_version in metadata['labels']['app.kubernetes.io/version']),
                        f"Mismatch in metadata version {environment.gpu_operator_version} vs {metadata['labels']['app.kubernetes.io/version']}")

    status = deviceconfig_crd['status']
    K8Helper.triage(environment, (status.get('conditions', None) != None), f"Missing status.conditions to check current status")
    for c in status.get('conditions', None):
        K8Helper.triage(environment, (c.get('status', 'False') == 'True'), f"Condition {c['type']} is not True/Successful")

    # TODO: Check the spec.properties - based on gpu-operator release

def test_gpu_operator_uninstall(request, gpu_cluster, images, gpu_operator_release_name, gpu_operator_install, environment):
    global Logger
    # Check if installation was successful
    ret_code, k8_namespaces = k8_util.k8_get_namespaces()
    K8Helper.triage(environment, (ret_code == 0), "Error while collecting namespaces")
    namespace_list = list(filter(lambda x: x['metadata']['name'] == environment.gpu_operator_namespace, k8_namespaces))
    K8Helper.triage(environment, (len(namespace_list) == 1), f"Missing namespace : {environment.gpu_operator_namespace}")

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")

    def _restore_gpu_operator():
        # Restore gpu-operator
        secret_list = []
        for entry in gpu_cluster.k8_secrets["secrets"]:
            secret_list.append(entry["name"])
        values_yaml = os.path.join(environment.logdir, f"values_{environment.gpu_operator_version}.yaml")
        if spec_util.generate_helmchart_deployment_config(environment.gpu_operator_version, images, secret_list, values_yaml):
            Logger.debug(f"Generated values.yaml for helm-chart install command, {values_yaml}")
        else:
            values_yaml = None

        restore_options = {"crds.defaultCR.install": "false"}
        anr_util.apply_anr_guard(restore_options, Logger)
        ret_code, ret_stdout, ret_stderr = helm_util.helm_install(gpu_cluster, gpu_operator_release_name,
                                                                  environment.gpu_operator_namespace,
                                                                  images.get('gpu-operator.helm-chart', None),
                                                                  environment.gpu_operator_version, values_yaml,
                                                                  **restore_options)
        if ret_code != 0:
            Logger.error(f"Failed to install helm chart for {gpu_operator_release_name}")
            Logger.error(f"Stdout: {ret_stdout}")
            Logger.error(f"Stderr: {ret_stderr}")
        K8Helper.triage(environment, (ret_code == 0), f"Failed to install {gpu_operator_release_name}")
        time.sleep(30)
        # Wait for all pods to be re-created
        failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, exp_pod_list)
        K8Helper.triage(environment, not failed_pods, f"One or more pods are not ready - {failed_pods}")
    request.addfinalizer(_restore_gpu_operator)

    ret_code, ret_stdout, ret_stderr = helm_util.helm_uninstall(gpu_cluster, gpu_operator_release_name, environment.gpu_operator_namespace)
    K8Helper.triage(environment, ret_code == 0, f"Failed to uninstall {gpu_operator_release_name} helm-chart error: {ret_stderr}")

    # Check if GPU_OPERATOR_NAMESPACE namespace is deleted
    ret_code, k8_namespaces = k8_util.k8_get_namespaces()
    K8Helper.triage(environment, (ret_code == 0), "Error while collecting namespaces")
    namespace_list = list(filter(lambda x: x['metadata']['name'] == environment.gpu_operator_namespace, k8_namespaces))
    if len(namespace_list):
        # Wait for all pods to be deleted
        exp_pod_list = [
            common.PodInfo(f'{gpu_operator_release_name}-gpu-operator-charts-controller-manager', 1, 1),
            common.PodInfo(f'{gpu_operator_release_name}-kmm-controller', 1, 1),
            common.PodInfo(f'{gpu_operator_release_name}-kmm-webhook-server', 1, 1),
            common.PodInfo(f'{gpu_operator_release_name}-node-feature-discovery-gc', 1, 1),
            common.PodInfo(f'{gpu_operator_release_name}-node-feature-discovery-master', 1, 1),
            common.PodInfo(f'{gpu_operator_release_name}-node-feature-discovery-worker', len(gpu_nodes), 1),
        ]
        running_pods = k8_util.k8_check_pod_terminated(environment.gpu_operator_namespace, exp_pod_list)
        K8Helper.triage(environment, not running_pods,
                        f"Some of the pods are still running post uninstallation - {running_pods}")

    # Check kmm.ready labels are removed from GPU nodes
    kmm_label_pattern = re.compile(r"kmm\.node\.kubernetes\.io/.*\.ready")
    ret_code, current_nodes = k8_util.k8_get_gpu_nodes()
    stale_labels = {}
    if ret_code == 0:
        for node in current_nodes:
            node_name = node['metadata']['name']
            for label in node['metadata'].get('labels', {}):
                if kmm_label_pattern.match(label):
                    stale_labels.setdefault(node_name, []).append(label)
    if stale_labels:
        Logger.warn(f"Stale kmm.ready labels found after uninstall: {stale_labels}")
    K8Helper.triage(environment, len(stale_labels) == 0,
                    f"kmm.ready labels not cleaned up after gpu-operator uninstall: {stale_labels}")

    # Check deviceconfigs.amd.com CRDs are removed
    if environment.gpu_operator_version in ['v1.0.0', 'v1.1.0']:
        Logger.debug('Skipping checking of deviceconfigs.amd.com CRD cleanup for gpu-operator chart version v1.0.0 and v1.1.0')
    else:
        deviceconfig_crd = k8_util.k8_get_crd("deviceconfigs.amd.com")
        K8Helper.triage(environment, (deviceconfig_crd is None),
                        f"CRD deviceconfigs.amd.com still found post gpu-operator uninstallation")
    time.sleep(30)

