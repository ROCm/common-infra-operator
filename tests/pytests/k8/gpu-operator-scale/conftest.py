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

import sys
import os

# Allow importing test_config_manager from the parent gpu-operator directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
import logging
import pytest
import lib.common as common
import lib.k8_util as k8_util
import lib.spec_util as spec_util
from lib.util import K8Helper
from test_config_manager import (
    get_gpu_series,
    reset_dcm_profile,
)

Logger = logging.getLogger("k8.gpu-operator.scale.conftest")


@pytest.fixture(scope="module")
def add_tolerations(environment, effect="NoExecute"):
    toleration_to_add = {
        "key": "amd-dcm",
        "operator": "Equal",
        "value": "up",
        "effect": effect
    }
    for ns in {"kube-system", "cert-manager", "kube-flannel"}:
        k8_util.k8_patch_tolerations(ns, toleration_to_add, tolerate_add=True)
    yield
    for ns in {"kube-system", "cert-manager", "kube-flannel"}:
        k8_util.k8_patch_tolerations(ns, toleration_to_add, tolerate_add=False)


@pytest.fixture(scope="module")
def create_dcm_configmap(gpu_cluster, environment):
    namespace = environment.gpu_operator_namespace
    configmap = "config-map-config-manager"

    gpu_series = get_gpu_series(gpu_cluster, environment)
    K8Helper.triage(environment, gpu_series is not None,
                     "Missing gpu-series information - collect tech-support to debug cluster")
    dut_node = gpu_cluster.find_node_by_gpu_series(gpu_series)
    K8Helper.triage(environment, dut_node is not None,
                     f"No node found for gpu-series {gpu_series} - collect tech-support to debug cluster")

    file_path = os.path.join(environment.logdir, f"partitioning_check_{gpu_series}_{dut_node.num_gpus}.json")
    k8_util.k8_delete_configmap(namespace, configmap)
    k8_util.k8_create_configmap(namespace, configmap, file_path, "config.json")
    yield configmap
    k8_util.k8_delete_configmap(namespace, configmap)


@pytest.fixture(scope="module")
def deviceconfig_install(gpu_cluster, images, gpu_operator_install, create_dcm_configmap, add_tolerations, environment):
    devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
    for devcfg_name, _ in devcfg_map.items():
        ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)
        if ret_code != 0:
            Logger.error(f"Failed to delete deviceconfig name: {devcfg_name}, error: {ret_stderr}")
    time.sleep(10)

    class DeviceConfigCRInfo(object):
        pass

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")
    configmap = "config-map-config-manager"

    test_config = {
        'metadata.namespace': environment.gpu_operator_namespace,
        'driver.enable': True,
        'devicePlugin.enableNodeLabeller': True,
        'metricsExporter.enable': True,
        'metricsExporter.serviceType': 'NodePort',
        'testRunner.enable': False,
        'configManager.enable': True,
        'configManager.config': configmap,
    }
    test_config.update(images)
    test_cfg_map = spec_util.build_deviceconfig_cr_template(test_config, gpu_nodes, 'config-manager', environment.amdgpu_driver_spec)
    exporter_port_map = {}
    devicecfg_list = []
    if len(test_cfg_map) > 1:
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

    K8Helper.check_deviceconfig_status(environment, devicecfg_list)
    for devcfg in devicecfg_list:
        K8Helper.wait_kmm_worker_completion(environment, devcfg)
    K8Helper.update_node_driver_version(gpu_cluster, environment)

    devcfg_info = DeviceConfigCRInfo()
    setattr(devcfg_info, "test_cfg_map", test_cfg_map)
    setattr(devcfg_info, "exporter_port_map", exporter_port_map)
    setattr(devcfg_info, "devicecfg_list", devicecfg_list)

    devicecfg_pods = [
        common.PodInfo('device-plugin', len(gpu_nodes), 1),
        common.PodInfo('config-manager', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods, sleep_time=20)
    K8Helper.triage(environment, (not failed_pods), f"One or more pods are not ready - {failed_pods}")

    yield devcfg_info

    reset_dcm_profile(gpu_cluster, environment)

    for spec_name, tcfg in test_cfg_map.items():
        tcfg['driver.enable'] = False
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        k8_util.k8_modify_deviceconfig_cr(cr_spec)
    time.sleep(60)

    device_cfg_info = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace, None)
    for devcfg_name, _ in device_cfg_info.items():
        k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)
    return
