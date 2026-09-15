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
Source image driver deployment tests (OpenShift only).

Validates deploying the amdgpu driver via pre-built source images
(useSourceImage=true) with DME metrics verification.
"""

import pytest
import time
import logging
import lib.k8_util as k8_util
import lib.common as common
import lib.spec_util as spec_util
import lib.amdgpu as amdgpu
from lib.util import K8Helper

Logger = logging.getLogger("k8.amdgpu-driver.test_source_image_driver")

DEFAULT_SOURCE_IMAGE_REPO = "docker.io/rocm/amdgpu-driver"

debug_on_failure = K8Helper.triage


@pytest.fixture(autouse=True, scope="module")
def skip_non_openshift(environment):
    if getattr(environment, 'deployment_mode', None) != "openshift":
        pytest.skip("Source image driver tests are OpenShift only")
    if hasattr(environment, 'amdgpu_driver_spec'):
        if environment.amdgpu_driver_spec.get("driver-deployment") != "deviceconfig":
            pytest.skip("Source image tests require deviceconfig driver deployment")


@pytest.fixture(scope="module")
def deviceconfig_install(gpu_cluster, images, gpu_operator_install, environment):
    global Logger

    devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
    for devcfg_name, _ in devcfg_map.items():
        k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)
    time.sleep(10)

    class DeviceConfigCRInfo(object):
        pass

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    debug_on_failure(environment, ret_code == 0, "Error getting gpu-nodes")
    debug_on_failure(environment, len(gpu_nodes) > 0, "No GPU nodes found")

    driver_version = environment.amdgpu_driver_spec["default-version"]

    source_repo = images.get('driver.imageBuild.sourceImageRepo.repository', DEFAULT_SOURCE_IMAGE_REPO)
    source_secret = images.get('driver.imageBuild.sourceImageRepo.secret', None)

    test_config = {
        'metadata.namespace': environment.gpu_operator_namespace,
        'driver.enable': True,
        'driver.blacklist': True,
        'driver.useSourceImage': True,
        'driver.imageBuild.sourceImageRepo': source_repo,
        'devicePlugin.enableNodeLabeller': True,
        'metricsExporter.enable': True,
        'metricsExporter.serviceType' : 'NodePort',
    }
    if source_secret:
        test_config['driver.imageRegistrySecret'] = source_secret
    test_config.update(images)

    test_cfg_map = spec_util.build_deviceconfig_cr_template(
        test_config, gpu_nodes, 'source-image-driver', environment.amdgpu_driver_spec)
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
        ret_code, _, ret_stderr = k8_util.k8_create_deviceconfig_cr(cr_spec)
        debug_on_failure(environment, ret_code == 0,
                         f"Failed to create deviceconfig: {ret_stderr}")
        devicecfg_list.append(tcfg['metadata.name'])

    K8Helper.check_deviceconfig_status(environment, devicecfg_list)
    for devcfg in devicecfg_list:
        K8Helper.wait_kmm_worker_completion(environment, devcfg)
    K8Helper.update_node_driver_version(gpu_cluster, environment)

    devcfg_info = DeviceConfigCRInfo()
    setattr(devcfg_info, "test_cfg_map", test_cfg_map)
    setattr(devcfg_info, "exporter_port_map", exporter_port_map)
    setattr(devcfg_info, "devicecfg_list", devicecfg_list)
    setattr(devcfg_info, "driver_version", driver_version)
    yield devcfg_info

    device_cfg_info = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace, None)
    for devcfg_name, _ in device_cfg_info.items():
        k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)


@pytest.mark.level1
def test_source_image_driver_deploy(gpu_cluster, deviceconfig_install, environment):
    """Deploy driver via source image, verify driver loaded + DME serving metrics."""
    global Logger
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    debug_on_failure(environment, ret_code == 0, "Error getting gpu-nodes")
    debug_on_failure(environment, len(gpu_nodes) > 0, "No GPU nodes found")

    devicecfg_pods = [
        common.PodInfo('device-plugin', len(gpu_nodes), 1),
        common.PodInfo('metrics-exporter', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods)
    debug_on_failure(environment, not failed_pods,
                     f"One or more pods are not ready - {failed_pods}")

    # Verify GPU capacity on each node
    for node in gpu_nodes:
        worker = k8_util.k8_get_node_hostname(node)
        init_cap, alloc = k8_util.k8_get_node_gpu_capacity(worker)
        debug_on_failure(environment, init_cap > 0,
                         f"Node {worker}: GPU capacity is {init_cap}, expected > 0")
        Logger.info(f"Node {worker}: GPU capacity={init_cap}, allocatable={alloc}")

    # Verify DME is serving metrics
    for node in gpu_nodes:
        node_ip = k8_util.k8_get_node_address(node)
        cluster_node = gpu_cluster.find_node_by_ip(node_ip)
        if cluster_node is None:
            continue
        node_hostname = k8_util.k8_get_node_hostname(node)
        port = deviceconfig_install.exporter_port_map.get(node_hostname, 32500)
        ret_code, metrics_output, _ = cluster_node.http_get(port, "metrics")
        debug_on_failure(environment, ret_code == 0,
                         f"Failed to scrape metrics from {node_hostname}:{port}")
        if isinstance(metrics_output, bytes):
            metrics_output = metrics_output.decode('utf-8', errors='replace')
        for metric in ['gpu_clock', 'gpu_junction_temperature', 'gpu_total_vram']:
            debug_on_failure(environment, metric in metrics_output,
                             f"Metric {metric} not found in DME output from {node_hostname}")
        Logger.info(f"Node {node_hostname}: DME metrics endpoint healthy, key metrics present")

    Logger.info(f"Source image driver deploy verified: driver={deviceconfig_install.driver_version}")


def pytest_generate_tests(metafunc):
    if "upgrade_version" in metafunc.fixturenames:
        spec_path = metafunc.config.getoption("--amdgpu-driver-spec", None)
        versions = []
        if spec_path:
            import json
            try:
                with open(spec_path) as f:
                    spec = json.load(f)
                versions = spec.get("alternative-versions", [])
            except Exception:
                pass
        if not versions:
            versions = [pytest.param("none", marks=pytest.mark.skip(reason="No alternative versions in driver spec"))]
        metafunc.parametrize("upgrade_version", versions)


@pytest.mark.level2
def test_source_image_driver_version_upgrade(request, gpu_cluster, deviceconfig_install, environment, upgrade_version):
    """Upgrade driver version via source image, verify reload + DME still serving."""
    global Logger

    current_version = deviceconfig_install.driver_version
    Logger.info(f"Upgrading source image driver: {current_version} => {upgrade_version}")

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    if ret_code != 0 or len(gpu_nodes) == 0:
        pytest.skip(f"No GPU nodes available — cluster may not have recovered from a previous test")

    def _restore():
        try:
            Logger.info(f"Restoring source image driver: {upgrade_version} => {current_version}")
            for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
                tcfg['driver.version'] = current_version
                tcfg['driver.upgradePolicy.enable'] = False
                cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
                k8_util.k8_modify_deviceconfig_cr(cr_spec)
            K8Helper.check_deviceconfig_status(environment, deviceconfig_install.devicecfg_list)
            for devcfg in deviceconfig_install.devicecfg_list:
                K8Helper.wait_kmm_worker_completion(environment, devcfg)
            K8Helper.wait_for_driver_reload(environment, gpu_nodes, fail_on_timeout=False)
        except Exception as e:
            Logger.error(f"Restore failed (best-effort): {e}")

    request.addfinalizer(_restore)

    # Apply upgrade version
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['driver.version'] = upgrade_version
        tcfg['driver.upgradePolicy.enable'] = True
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, _, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        debug_on_failure(environment, ret_code == 0,
                         f"Failed to modify deviceconfig for upgrade: {ret_stderr}")

    K8Helper.check_deviceconfig_status(environment, deviceconfig_install.devicecfg_list)
    for devcfg in deviceconfig_install.devicecfg_list:
        K8Helper.wait_kmm_worker_completion(environment, devcfg)

    # Wait for driver reload (handles node reboot on OpenShift with rebootRequired=True)
    driver_ready = K8Helper.wait_for_driver_reload(environment, gpu_nodes)
    debug_on_failure(environment, driver_ready,
                     f"Driver reload failed after upgrade to {upgrade_version}")

    # Re-fetch gpu_nodes — node list may change after reboot
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    debug_on_failure(environment, ret_code == 0 and len(gpu_nodes) > 0,
                     f"No GPU nodes available after driver reload")

    # Verify all operand pods running after upgrade
    devicecfg_pods = [
        common.PodInfo('device-plugin', len(gpu_nodes), 1),
        common.PodInfo('metrics-exporter', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods)
    debug_on_failure(environment, not failed_pods,
                     f"Pods not ready after upgrade to {upgrade_version}: {failed_pods}")

    # Verify driver version matches upgrade_version
    driver_version = amdgpu.get_matching_driver_version(upgrade_version)
    K8Helper.check_deviceconfig_driver_version(gpu_cluster, upgrade_version, environment)
    K8Helper.check_node_driver_version(gpu_cluster, upgrade_version, driver_version, environment)

    # Verify DME still serving
    for node in gpu_nodes:
        node_ip = k8_util.k8_get_node_address(node)
        cluster_node = gpu_cluster.find_node_by_ip(node_ip)
        if cluster_node is None:
            continue
        node_hostname = k8_util.k8_get_node_hostname(node)
        port = deviceconfig_install.exporter_port_map.get(node_hostname, 32500)
        ret_code, metrics_output, _ = cluster_node.http_get(port, "metrics")
        debug_on_failure(environment, ret_code == 0,
                         f"DME not serving after upgrade on {node_hostname}")
        if isinstance(metrics_output, bytes):
            metrics_output = metrics_output.decode('utf-8', errors='replace')
        debug_on_failure(environment, 'gpu_clock' in metrics_output,
                         f"gpu_clock missing from DME after upgrade on {node_hostname}")

    Logger.info(f"Source image driver upgrade verified: {current_version} => {upgrade_version}")
