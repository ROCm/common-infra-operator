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
DME debug-endpoint config toggle tests — Exporter Helm Chart variant.

Verifies CommonConfig.Debug.EnableAPI runtime config toggle via ConfigMap:
  - Default (disabled): debug endpoints return 404, /metrics unaffected
  - Enabled via ConfigMap: debug endpoints return 200 with valid content

"""

import os
import json
import time
import logging
import pytest
from lib import common
from lib import k8_util
from lib import helm_util
from lib import spec_util
from lib.util import K8Helper
from lib.dme_debug_util import (
    DEBUG_ENDPOINTS, build_debug_config,
    verify_debug_endpoints, verify_exporter_ready,
    dump_debug_endpoint_content,
)

Logger = logging.getLogger("k8.exporter.test_exporter_debug")

_EXPORTER_NODEPORT = 32500
_CONFIG_RELOAD_WAIT = 30


def _install_exporter_with_config(request, gpu_cluster, images, environment,
                                  config_data, config_name):
    """Create a ConfigMap and install the exporter helm chart referencing it."""
    exporter_release_name = "device-metrics-exporter"

    configmap_file = os.path.join(environment.logdir, f"{config_name}.json")
    with open(configmap_file, "w") as fp:
        json.dump(config_data, fp, indent=4)

    k8_util.k8_delete_configmap(environment.exporter_namespace, config_name)
    rc, _, err = k8_util.k8_create_configmap(
        environment.exporter_namespace, config_name, configmap_file, "config.json")
    K8Helper.triage(environment, rc == 0,
                    f"Failed to create configmap {config_name}: {err}")

    values_yaml = os.path.join(environment.logdir,
                               f"exporter_values_debug_{config_name}.yaml")
    options = {
        "service.type": "NodePort",
        "configMap": config_name,
    }
    spec_util.generate_exporter_helmchart_deployment_config(
        environment.exporter_version, images, values_yaml, **options)

    helm_util.helm_uninstall(gpu_cluster, exporter_release_name,
                             environment.exporter_namespace)

    rc, _, err = helm_util.helm_install(
        gpu_cluster, exporter_release_name,
        environment.exporter_namespace,
        images.get('exporter.helm-chart', None),
        environment.exporter_version, values_yaml)
    K8Helper.triage(environment, rc == 0,
                    f"Failed to install exporter helm chart: {err}")

    time.sleep(_CONFIG_RELOAD_WAIT)
    K8Helper.triage(environment,
                    helm_util.is_helm_chart_healthy(
                        gpu_cluster, exporter_release_name,
                        environment.exporter_namespace),
                    "Exporter helm chart not healthy")
    K8Helper.watch_for_daemon_rollout(
        environment, environment.exporter_namespace,
        len(gpu_cluster.cluster_nodes))
    time.sleep(20)

    exporter_pods = []
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            pod_name = k8_util.k8_get_pod_name(
                "amdgpu-metrics-exporter",
                environment.exporter_namespace, node.host_name)
            exporter_pods.append(common.PodInfo(pod_name, 1, 1))
    failed_pods = k8_util.k8_check_pod_running(
        environment.exporter_namespace, exporter_pods)
    K8Helper.triage(environment, not failed_pods,
                    f"Exporter pods not ready: {failed_pods}")

    def _cleanup():
        helm_util.helm_uninstall(gpu_cluster, exporter_release_name,
                                 environment.exporter_namespace)
        k8_util.k8_delete_configmap(environment.exporter_namespace, config_name)

    request.addfinalizer(_cleanup)


def test_debug_api_disabled_by_default(request, gpu_cluster, images, environment):
    """Debug endpoints must return 404 when EnableAPI is not set (default)."""
    global Logger
    ref_config = {"CommonConfig": {"MetricsFieldPrefix": "amd_"}}
    _install_exporter_with_config(
        request, gpu_cluster, images, environment,
        ref_config, "debug-test-default")

    for node in gpu_cluster.cluster_nodes:
        if not node.is_gpu_node():
            continue

        K8Helper.triage(environment,
                        verify_exporter_ready(node, _EXPORTER_NODEPORT, Logger, use_ssh=False),
                        f"[{node.ip_address}] Exporter not ready before debug endpoint check")

        failures = verify_debug_endpoints(node, _EXPORTER_NODEPORT, 404, Logger, use_ssh=False)

        dump_debug_endpoint_content(node, _EXPORTER_NODEPORT, environment.logdir, Logger, use_ssh=False)

        K8Helper.triage(environment, len(failures) == 0,
                        f"[{node.ip_address}] Debug endpoints not disabled by default: "
                        f"{failures}")

        rc, ret_stdout, _ = node.http_get(_EXPORTER_NODEPORT, "metrics")
        K8Helper.triage(environment, rc == 0,
                        f"[{node.ip_address}] /metrics not accessible on port {_EXPORTER_NODEPORT}")


def test_debug_api_explicit_disable(request, gpu_cluster, images, environment):
    """Debug endpoints must return 404 when EnableAPI is explicitly set to false."""
    global Logger
    ref_config = {"CommonConfig": {"MetricsFieldPrefix": "amd_"}}
    disabled_config = build_debug_config(ref_config, False)
    _install_exporter_with_config(
        request, gpu_cluster, images, environment,
        disabled_config, "debug-test-disabled")

    for node in gpu_cluster.cluster_nodes:
        if not node.is_gpu_node():
            continue

        K8Helper.triage(environment,
                        verify_exporter_ready(node, _EXPORTER_NODEPORT, Logger, use_ssh=False),
                        f"[{node.ip_address}] Exporter not ready before debug endpoint check")

        failures = verify_debug_endpoints(node, _EXPORTER_NODEPORT, 404, Logger, use_ssh=False)

        dump_debug_endpoint_content(node, _EXPORTER_NODEPORT, environment.logdir, Logger, use_ssh=False)

        K8Helper.triage(environment, len(failures) == 0,
                        f"[{node.ip_address}] Debug endpoints not disabled after "
                        f"explicit EnableAPI=false: {failures}")

        rc, ret_stdout, _ = node.http_get(_EXPORTER_NODEPORT, "metrics")
        K8Helper.triage(environment, rc == 0,
                        f"[{node.ip_address}] /metrics not accessible when debug is explicitly disabled")


def test_debug_api_enable_via_config(request, gpu_cluster, images, environment):
    """Toggle Debug.EnableAPI true via ConfigMap, verify endpoints respond with valid content."""
    global Logger
    ref_config = {"CommonConfig": {"MetricsFieldPrefix": "amd_"}}
    enabled_config = build_debug_config(ref_config, True)

    _install_exporter_with_config(
        request, gpu_cluster, images, environment,
        enabled_config, "debug-test-enabled")

    for node in gpu_cluster.cluster_nodes:
        if not node.is_gpu_node():
            continue

        K8Helper.triage(environment,
                        verify_exporter_ready(node, _EXPORTER_NODEPORT, Logger, use_ssh=False),
                        f"[{node.ip_address}] Exporter not ready before debug endpoint check")

        failures = verify_debug_endpoints(node, _EXPORTER_NODEPORT, 200, Logger, use_ssh=False)
        K8Helper.triage(environment, len(failures) == 0,
                        f"[{node.ip_address}] Debug endpoints not accessible after "
                        f"EnableAPI=true: {failures}")

        content_failures = dump_debug_endpoint_content(
            node, _EXPORTER_NODEPORT, environment.logdir, Logger, use_ssh=False)
        K8Helper.triage(environment, len(content_failures) == 0,
                        f"[{node.ip_address}] Debug endpoint content validation failed: "
                        f"{content_failures}")

        rc, ret_stdout, _ = node.http_get(_EXPORTER_NODEPORT, "metrics")
        K8Helper.triage(environment, rc == 0,
                        f"[{node.ip_address}] /metrics not accessible when debug is enabled")
