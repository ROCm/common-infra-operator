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
DME debug-endpoint config toggle tests — GPU Operator (DeviceConfig) variant.

Verifies CommonConfig.Debug.EnableAPI runtime config toggle via ConfigMap
referenced from DeviceConfig CR metricsExporter.config:
  - Default (no ConfigMap): debug endpoints return 404, /metrics unaffected
  - Enabled via ConfigMap: debug endpoints return 200 with valid content
  - Restore: removing ConfigMap ref disables debug endpoints again

"""

import os
import json
import time
import logging
import pytest
from lib import common
from lib import k8_util
from lib import spec_util
import lib.amdgpu as amdgpu_util
from lib.util import K8Helper
from lib.dme_debug_util import (
    DEBUG_ENDPOINTS, build_debug_config,
    verify_debug_endpoints, dump_debug_endpoint_content,
)

Logger = logging.getLogger("k8.gpu-operator.test_exporter_debug")

_CONFIG_RELOAD_WAIT = 30
_DEBUG_CONFIGMAP_NAME = "debug-api-toggle"


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
    K8Helper.triage(environment, ret_code == 0, "Error getting gpu-nodes")
    K8Helper.triage(environment, len(gpu_nodes) > 0, "No GPU nodes found")

    test_config = {
        'metadata.namespace': environment.gpu_operator_namespace,
        'driver.enable': True,
        'devicePlugin.enableNodeLabeller': False,
        'metricsExporter.enable': True,
        'metricsExporter.serviceType': 'NodePort',
    }
    test_config.update(images)

    test_cfg_map = spec_util.build_deviceconfig_cr_template(
        test_config, gpu_nodes, 'exporter_debug', environment.amdgpu_driver_spec)
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
        K8Helper.triage(environment, ret_code == 0,
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

    exporter_pods = [common.PodInfo('metrics-exporter', len(gpu_nodes), 1)]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, exporter_pods)
    K8Helper.triage(environment, not failed_pods, f"Exporter pods not ready: {failed_pods}")

    yield devcfg_info

    devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
    for devcfg_name, _ in devcfg_map.items():
        k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)


def _get_exporter_port(deviceconfig_install, node_hostname):
    """Resolve the NodePort for the exporter on a given node."""
    return deviceconfig_install.exporter_port_map.get(node_hostname, 32500)


def test_debug_api_disabled_by_default(request, gpu_cluster, deviceconfig_install, environment):
    """Debug endpoints must return 404 when no debug ConfigMap is applied."""
    global Logger
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "Failed to get GPU nodes")

    for node in gpu_nodes:
        node_ip = k8_util.k8_get_node_address(node)
        cluster_node = gpu_cluster.find_node_by_ip(node_ip)
        if not cluster_node:
            continue
        node_hostname = k8_util.k8_get_node_hostname(node)
        port = _get_exporter_port(deviceconfig_install, node_hostname)

        failures = verify_debug_endpoints(cluster_node, port, 404, Logger, use_ssh=False)

        dump_debug_endpoint_content(cluster_node, port, environment.logdir, Logger, use_ssh=False)

        K8Helper.triage(environment, len(failures) == 0,
                        f"[{node_ip}] Debug endpoints not disabled by default: {failures}")

        rc, ret_stdout, _ = cluster_node.http_get(port, "metrics")
        K8Helper.triage(environment, rc == 0,
                        f"[{node_ip}] /metrics not accessible on port {port}")


def test_debug_api_explicit_disable(request, gpu_cluster, deviceconfig_install, environment):
    """Debug endpoints must return 404 when EnableAPI is explicitly set to false via ConfigMap."""
    global Logger
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "Failed to get GPU nodes")

    disabled_config = build_debug_config(
        {"CommonConfig": {"MetricsFieldPrefix": "amd_"}}, False)
    configmap_file = os.path.join(environment.logdir, "debug-explicit-disable.json")
    with open(configmap_file, "w") as fp:
        json.dump(disabled_config, fp, indent=4)

    config_name = "debug-test-explicit-off"
    k8_util.k8_delete_configmap(environment.gpu_operator_namespace, config_name)
    rc, _, err = k8_util.k8_create_configmap(
        environment.gpu_operator_namespace, config_name, configmap_file, "config.json")
    K8Helper.triage(environment, rc == 0,
                    f"Failed to create configmap {config_name}: {err}")

    def _cleanup():
        for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
            if 'metricsExporter.config' in tcfg:
                del tcfg['metricsExporter.config']
            cr_spec = spec_util.generate_k8_deviceconfig_cr(
                environment.gpu_operator_version, tcfg)
            k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.check_deviceconfig_status(
            environment, deviceconfig_install.devicecfg_list)
        time.sleep(_CONFIG_RELOAD_WAIT)
        k8_util.k8_delete_configmap(environment.gpu_operator_namespace, config_name)

    request.addfinalizer(_cleanup)

    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['metricsExporter.config'] = config_name
        cr_spec = spec_util.generate_k8_deviceconfig_cr(
            environment.gpu_operator_version, tcfg)
        rc, _, err = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, rc == 0,
                        f"Failed to modify deviceconfig: {err}")

    K8Helper.check_deviceconfig_status(
        environment, deviceconfig_install.devicecfg_list)
    time.sleep(_CONFIG_RELOAD_WAIT)

    for node in gpu_nodes:
        node_ip = k8_util.k8_get_node_address(node)
        cluster_node = gpu_cluster.find_node_by_ip(node_ip)
        if not cluster_node:
            continue
        node_hostname = k8_util.k8_get_node_hostname(node)
        port = _get_exporter_port(deviceconfig_install, node_hostname)

        failures = verify_debug_endpoints(cluster_node, port, 404, Logger, use_ssh=False)

        dump_debug_endpoint_content(cluster_node, port, environment.logdir, Logger, use_ssh=False)

        K8Helper.triage(environment, len(failures) == 0,
                        f"[{node_ip}] Debug endpoints not disabled after "
                        f"explicit EnableAPI=false: {failures}")

        rc, ret_stdout, _ = cluster_node.http_get(port, "metrics")
        K8Helper.triage(environment, rc == 0,
                        f"[{node_ip}] /metrics not accessible when debug is explicitly disabled")


def test_debug_api_enable_via_config(request, gpu_cluster, deviceconfig_install, environment):
    """Apply a ConfigMap with Debug.EnableAPI=true, verify endpoints, then restore."""
    global Logger
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "Failed to get GPU nodes")

    enabled_config = build_debug_config(
        {"CommonConfig": {"MetricsFieldPrefix": "amd_"}}, True)
    configmap_file = os.path.join(environment.logdir,
                                  f"{_DEBUG_CONFIGMAP_NAME}.json")
    with open(configmap_file, "w") as fp:
        json.dump(enabled_config, fp, indent=4)

    k8_util.k8_delete_configmap(environment.gpu_operator_namespace,
                                _DEBUG_CONFIGMAP_NAME)
    rc, _, err = k8_util.k8_create_configmap(
        environment.gpu_operator_namespace,
        _DEBUG_CONFIGMAP_NAME, configmap_file, "config.json")
    K8Helper.triage(environment, rc == 0,
                    f"Failed to create configmap {_DEBUG_CONFIGMAP_NAME}: {err}")

    def _cleanup():
        for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
            if 'metricsExporter.config' in tcfg:
                del tcfg['metricsExporter.config']
            cr_spec = spec_util.generate_k8_deviceconfig_cr(
                environment.gpu_operator_version, tcfg)
            k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.check_deviceconfig_status(
            environment, deviceconfig_install.devicecfg_list)
        time.sleep(_CONFIG_RELOAD_WAIT)
        k8_util.k8_delete_configmap(environment.gpu_operator_namespace,
                                    _DEBUG_CONFIGMAP_NAME)

    request.addfinalizer(_cleanup)

    # Apply ConfigMap to DeviceConfig CR
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['metricsExporter.config'] = _DEBUG_CONFIGMAP_NAME
        cr_spec = spec_util.generate_k8_deviceconfig_cr(
            environment.gpu_operator_version, tcfg)
        rc, _, err = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, rc == 0,
                        f"Failed to modify deviceconfig: {err}")

    K8Helper.check_deviceconfig_status(
        environment, deviceconfig_install.devicecfg_list)
    time.sleep(_CONFIG_RELOAD_WAIT)

    # Verify debug endpoints are accessible
    for node in gpu_nodes:
        node_ip = k8_util.k8_get_node_address(node)
        cluster_node = gpu_cluster.find_node_by_ip(node_ip)
        if not cluster_node:
            continue
        node_hostname = k8_util.k8_get_node_hostname(node)
        port = _get_exporter_port(deviceconfig_install, node_hostname)

        failures = verify_debug_endpoints(cluster_node, port, 200, Logger, use_ssh=False)
        K8Helper.triage(environment, len(failures) == 0,
                        f"[{node_ip}] Debug endpoints not accessible after "
                        f"EnableAPI=true: {failures}")

        content_failures = dump_debug_endpoint_content(
            cluster_node, port, environment.logdir, Logger, use_ssh=False)
        K8Helper.triage(environment, len(content_failures) == 0,
                        f"[{node_ip}] Debug endpoint content validation failed: "
                        f"{content_failures}")

        rc, ret_stdout, _ = cluster_node.http_get(port, "metrics")
        K8Helper.triage(environment, rc == 0,
                        f"[{node_ip}] /metrics not accessible when debug is enabled")

    # Remove ConfigMap reference and verify endpoints return 404
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        del tcfg['metricsExporter.config']
        cr_spec = spec_util.generate_k8_deviceconfig_cr(
            environment.gpu_operator_version, tcfg)
        rc, _, err = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, rc == 0,
                        f"Failed to restore deviceconfig: {err}")

    K8Helper.check_deviceconfig_status(
        environment, deviceconfig_install.devicecfg_list)
    time.sleep(_CONFIG_RELOAD_WAIT)

    for node in gpu_nodes:
        node_ip = k8_util.k8_get_node_address(node)
        cluster_node = gpu_cluster.find_node_by_ip(node_ip)
        if not cluster_node:
            continue
        node_hostname = k8_util.k8_get_node_hostname(node)
        port = _get_exporter_port(deviceconfig_install, node_hostname)

        failures = verify_debug_endpoints(cluster_node, port, 404, Logger, use_ssh=False)
        K8Helper.triage(environment, len(failures) == 0,
                        f"[{node_ip}] Debug endpoints not disabled after "
                        f"removing ConfigMap: {failures}")


def test_ecc_injection_blocked_when_debug_disabled(request, gpu_cluster, deviceconfig_install, environment):
    """SetError gRPC (ECC error injection) must be blocked when Debug.EnableAPI is not set."""
    global Logger
    namespace = environment.gpu_operator_namespace
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "Failed to get GPU nodes")

    gpu_node = gpu_nodes[0]
    node_name = k8_util.k8_get_node_hostname(gpu_node)

    # Ensure node starts healthy
    initial_health = k8_util.k8_get_node_health(node_name, namespace)
    if initial_health != "healthy":
        pytest.skip(f"Node {node_name} not healthy at start ({initial_health}) — cannot test")

    error_fields = ['GPU_ECC_UNCORRECT_GFX', 'GPU_ECC_UNCORRECT_UMC']
    error_counts = [5, 5]

    # Safety cleanup in case injection unexpectedly succeeds
    def _cleanup():
        try:
            k8_util.k8_metrics_error([0, 0], error_fields, namespace)
        except Exception:
            pass
    request.addfinalizer(_cleanup)

    # Attempt ECC injection without EnableAPI — should silently fail
    k8_util.k8_metrics_error(error_counts, error_fields, namespace)

    # Wait and retry — give enough time for health to transition if injection leaked through
    for _ in range(4):
        time.sleep(15)
        health_after = k8_util.k8_get_node_health(node_name, namespace)
        if health_after != "healthy":
            break
    Logger.info(f"After injection attempt without EnableAPI: health={health_after}")
    K8Helper.triage(environment, health_after == "healthy",
                    f"Node {node_name} became {health_after} — SetError should have been "
                    f"blocked without Debug.EnableAPI")


def test_ecc_injection_allowed_when_debug_enabled(request, gpu_cluster, deviceconfig_install, environment):
    """SetError gRPC (ECC error injection) must succeed when Debug.EnableAPI=true,
    and must be blocked again after the config is removed."""
    global Logger
    namespace = environment.gpu_operator_namespace
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "Failed to get GPU nodes")

    gpu_node = gpu_nodes[0]
    node_name = k8_util.k8_get_node_hostname(gpu_node)

    initial_health = k8_util.k8_get_node_health(node_name, namespace)
    if initial_health != "healthy":
        pytest.skip(f"Node {node_name} not healthy at start ({initial_health}) — cannot test")

    error_fields = ['GPU_ECC_UNCORRECT_GFX', 'GPU_ECC_UNCORRECT_UMC']
    error_counts = [5, 5]

    # Phase 1: Enable Debug.EnableAPI
    enabled_config = build_debug_config(
        {"CommonConfig": {"MetricsFieldPrefix": "amd_"}}, True)
    config_name = "debug-ecc-injection-test"
    configmap_file = os.path.join(environment.logdir, f"{config_name}.json")
    with open(configmap_file, "w") as fp:
        json.dump(enabled_config, fp, indent=4)

    k8_util.k8_delete_configmap(namespace, config_name)
    rc, _, err = k8_util.k8_create_configmap(
        namespace, config_name, configmap_file, "config.json")
    K8Helper.triage(environment, rc == 0,
                    f"Failed to create configmap {config_name}: {err}")

    saved_configs = {}
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        saved_configs[spec_name] = tcfg.get('metricsExporter.config')
        tcfg['metricsExporter.config'] = config_name
        cr_spec = spec_util.generate_k8_deviceconfig_cr(
            environment.gpu_operator_version, tcfg)
        rc, _, err = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, rc == 0,
                        f"Failed to modify deviceconfig: {err}")

    K8Helper.check_deviceconfig_status(
        environment, deviceconfig_install.devicecfg_list)
    time.sleep(_CONFIG_RELOAD_WAIT)

    def _cleanup():
        # Clear injected errors
        try:
            k8_util.k8_metrics_error([0, 0], error_fields, namespace)
        except Exception:
            pass
        # Restore CR config
        for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
            prev = saved_configs.get(spec_name)
            if prev is None and 'metricsExporter.config' in tcfg:
                del tcfg['metricsExporter.config']
            else:
                tcfg['metricsExporter.config'] = prev
            cr_spec = spec_util.generate_k8_deviceconfig_cr(
                environment.gpu_operator_version, tcfg)
            k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.check_deviceconfig_status(
            environment, deviceconfig_install.devicecfg_list)
        time.sleep(_CONFIG_RELOAD_WAIT)
        k8_util.k8_delete_configmap(namespace, config_name)
    request.addfinalizer(_cleanup)

    # Phase 2: Inject errors — should succeed now
    ret_code, _, ret_stderr = k8_util.k8_metrics_error(error_counts, error_fields, namespace)
    K8Helper.triage(environment, ret_code == 0, f"ECC error injection failed: {ret_stderr}")
    for _ in range(6):
        time.sleep(10)
        health = k8_util.k8_get_node_health(node_name, namespace)
        if health == "unhealthy":
            break
    Logger.info(f"After injection with EnableAPI=true: health={health}")
    K8Helper.triage(environment, health == "unhealthy",
                    f"Node {node_name} should be unhealthy after ECC injection "
                    f"with Debug.EnableAPI=true, got: {health}")

    # Phase 3: Clear errors, restore health
    k8_util.k8_metrics_error([0, 0], error_fields, namespace)
    for _ in range(6):
        time.sleep(10)
        health = k8_util.k8_get_node_health(node_name, namespace)
        if health == "healthy":
            break
    K8Helper.triage(environment, health == "healthy",
                    f"Node {node_name} should be healthy after clearing errors, got: {health}")

    # Phase 4: Remove EnableAPI, verify injection is blocked again
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        if 'metricsExporter.config' in tcfg:
            del tcfg['metricsExporter.config']
        cr_spec = spec_util.generate_k8_deviceconfig_cr(
            environment.gpu_operator_version, tcfg)
        rc, _, err = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, rc == 0,
                        f"Failed to restore deviceconfig: {err}")

    K8Helper.check_deviceconfig_status(
        environment, deviceconfig_install.devicecfg_list)
    time.sleep(_CONFIG_RELOAD_WAIT)

    k8_util.k8_metrics_error(error_counts, error_fields, namespace)
    time.sleep(15)

    health_final = k8_util.k8_get_node_health(node_name, namespace)
    Logger.info(f"After injection attempt with EnableAPI removed: health={health_final}")
    K8Helper.triage(environment, health_final == "healthy",
                    f"Node {node_name} became {health_final} — SetError should have been "
                    f"blocked after removing Debug.EnableAPI")
