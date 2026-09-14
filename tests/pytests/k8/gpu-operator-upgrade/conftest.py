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

import pytest
import os
import logging
import time
from lib import common
import lib.helm_util as helm_util
import lib.k8_util as k8_util
import lib.spec_util as spec_util
import lib.amdgpu as amdgpu
from lib.util import K8Helper

Logger = logging.getLogger("k8.gpu-operator.upgrade.conftest")

@pytest.fixture(scope="session")
def base_version(request):
    """Get base version from pytest command line option"""
    base_ver = request.config.getoption("--base-version")
    if not base_ver:
        pytest.fail("--base-version is required for upgrade tests")
    Logger.info(f"Base version for upgrade: {base_ver}")
    return base_ver

@pytest.fixture(scope="session")
def base_images(request, environment, all_image_versions, base_version):
    """
    Get base version images from all_image_versions fixture.
    This is the version we'll deploy initially before upgrading to RC.
    """
    operator_type = "gpu-operator"

    if operator_type not in all_image_versions:
        pytest.fail(f"Operator type '{operator_type}' not found in all_image_versions")

    if base_version not in all_image_versions[operator_type]:
        available = list(all_image_versions[operator_type].keys())
        pytest.fail(f"Base version '{base_version}' not found for {operator_type}. Available: {available}")

    base_imgs = all_image_versions[operator_type][base_version]
    Logger.info(f"Using base version images: {base_version}")
    return base_imgs

@pytest.fixture(scope="session")
def rc_images(images):
    """
    RC images are the current build artifacts, already loaded via --image-manifest.
    This is just an alias to the standard 'images' fixture for clarity in upgrade tests.
    """
    Logger.info("Using RC version images from --image-manifest")
    return images

@pytest.fixture(scope="module")
def base_gpu_operator_install(gpu_cluster, gpu_operator_release_name, base_images, base_version, environment):
    """
    Install GPU operator with base version images.
    This is the initial deployment before upgrade.
    """
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

    if base_images.get("gpu-operator.repo", None):
        helm_util.helm_add_repo(gpu_cluster, base_images.get("gpu-operator.repo-name"), base_images.get("gpu-operator.repo"))

    # Get base chart version from base_images (not RC version from environment)
    base_chart_version = base_images.get('gpu-operator.helm-chart.version', base_version)

    secret_list = []
    for entry in gpu_cluster.k8_secrets["secrets"]:
        secret_list.append(entry["name"])
    values_yaml = os.path.join(environment.logdir, f"base_values_{base_version}.yaml")
    if spec_util.generate_helmchart_deployment_config(base_version, base_images, secret_list, values_yaml):
        Logger.debug(f"Generated base version values.yaml for helm-chart install, {values_yaml}")
    else:
        values_yaml = None

    options = {
        "crds.defaultCR.install" : "false",
    }

    Logger.info(f"Installing GPU operator with base version {base_version}")
    ret_code, ret_stdout, ret_stderr = helm_util.helm_install(gpu_cluster, gpu_operator_release_name,
                                                              environment.gpu_operator_namespace,
                                                              base_images.get('gpu-operator.helm-chart', None),
                                                              base_chart_version, values_yaml, **options)
    if ret_code != 0:
        Logger.error(f"Failed to install helm chart for {gpu_operator_release_name} with base images")
        Logger.error(f"Stdout: {ret_stdout}")
        Logger.error(f"Stderr: {ret_stderr}")
    K8Helper.triage(environment, (ret_code == 0), f"Failed to install {gpu_operator_release_name} with base version")
    K8Helper.watch_for_daemon_rollout(environment, environment.gpu_operator_namespace, len(gpu_cluster.cluster_nodes))
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

@pytest.fixture(scope="module")
def base_deviceconfig_install(gpu_cluster, base_images, base_version, base_gpu_operator_install, environment):
    """
    Create DeviceConfig CR with base version images.
    This is the initial configuration before upgrade.
    """
    global Logger

    # cleanup - remove any existing deviceconfigs
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
    }
    test_config.update(base_images)
    gpu_op_release_info = amdgpu.get_supported_operands(base_version)
    if gpu_op_release_info == None:
        pytest.fail(f"No information available about this release {base_version}")

    for operand in gpu_op_release_info.get("operands", []):
        Logger.debug(f"Processing {operand['name']}")
        if operand.get("enable", None):
            test_config[operand["enable"]] = True

    test_cfg_map = spec_util.build_deviceconfig_cr_template(test_config, gpu_nodes, 'upgrade_test', environment.amdgpu_driver_spec)
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

    Logger.info(f"Creating DeviceConfig CR with base version {base_version}")
    for spec_name, tcfg in test_cfg_map.items():
        # Use base_version for CR generation to match base operator version
        cr_spec = spec_util.generate_k8_deviceconfig_cr(base_version, tcfg)
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

    # cleanup
    device_cfg_info = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace, None)
    for devcfg_name, _ in device_cfg_info.items():
        k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)
    return
