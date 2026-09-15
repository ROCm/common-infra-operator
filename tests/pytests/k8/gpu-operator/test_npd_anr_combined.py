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

import ast
import json
import re
import pytest
import time
import logging
from datetime import datetime
from kubernetes import client
import lib.k8_util as k8_util
import lib.spec_util as spec_util
import lib.anr_util as anr_util
import lib.npd_util as npd_util
from lib.util import K8Helper

Logger = logging.getLogger("k8.test_npd_anr_combined")


@pytest.fixture(autouse=True, scope="module")
def skip_if_anr_unsupported():
    """Skip ANR tests on K8s < 1.30 (Argo CRD cost budget exceeded)."""
    import lib.anr_util as anr_util
    if not anr_util.k8s_supports_anr():
        pytest.skip("ANR requires K8s 1.30+ (Argo CRD cost budget)")


@pytest.fixture(scope="module")
def deviceconfig_install(gpu_cluster, images, gpu_operator_install,
                         argo_workflow_setup, deploy_npd_daemonset, environment, request):
    """
    Module-scoped fixture for combined NPD + ANR tests.

    Installs DeviceConfig with metricsExporter enabled (required by NPD) and
    depends on argo_workflow_setup so that ANR workflows can be triggered later.
    Remediation is NOT enabled here — the test enables it after NPD detects the
    condition.
    """
    argo_info = argo_workflow_setup
    Logger.info(f"Argo Workflows managed by: {argo_info.get('managed_by', 'fixture')}")

    def _deviceconfig_cleanup():
        devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
        for devcfg_name in list(devcfg_map.keys()):
            ret_code, _, ret_stderr = k8_util.k8_delete_deviceconfig_cr(
                environment.gpu_operator_namespace, devcfg_name)
            if ret_code != 0:
                Logger.error(f"Failed to delete deviceconfig {devcfg_name}: {ret_stderr}")
        time.sleep(10)

    _deviceconfig_cleanup()
    request.addfinalizer(_deviceconfig_cleanup)

    class DeviceConfigCRInfo:
        pass

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, len(gpu_nodes) > 0, "No nodes with AMD/GPU found in the cluster")

    test_config = {
        'metadata.namespace': environment.gpu_operator_namespace,
        'driver.enable': True,
        'devicePlugin.enableNodeLabeller': False,
        'metricsExporter.enable': True,
        'metricsExporter.serviceType': 'NodePort',
    }
    test_config.update(images)

    test_cfg_map = spec_util.build_deviceconfig_cr_template(
        test_config, gpu_nodes, 'npd_anr_combined', environment.amdgpu_driver_spec)

    exporter_port_map = {}
    devicecfg_list = []
    if len(test_cfg_map) > 1:
        for idx, cfg_name in enumerate(test_cfg_map.keys()):
            cfg = test_cfg_map[cfg_name]
            cfg['metricsExporter.nodePort'] = 32500 + idx * 100
            exporter_port_map[cfg['selector.value']] = cfg['metricsExporter.nodePort']
    else:
        for node in gpu_nodes:
            exporter_port_map[k8_util.k8_get_node_hostname(node)] = 32500

    for spec_name, tcfg in test_cfg_map.items():
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, _, ret_stderr = k8_util.k8_create_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, ret_code == 0,
                        f"Failed to create deviceconfig, stderr: {ret_stderr}")
        devicecfg_list.append(tcfg['metadata.name'])

    K8Helper.check_deviceconfig_status(environment, devicecfg_list)
    for devcfg in devicecfg_list:
        K8Helper.wait_kmm_worker_completion(environment, devcfg)
    K8Helper.update_node_driver_version(gpu_cluster, environment)

    devcfg_info = DeviceConfigCRInfo()
    setattr(devcfg_info, "test_cfg_map", test_cfg_map)
    setattr(devcfg_info, "exporter_port_map", exporter_port_map)
    setattr(devcfg_info, "devicecfg_list", devicecfg_list)
    yield devcfg_info


@pytest.fixture(autouse=True)
def collect_logs_on_failure(request, environment):
    """Collect Argo workflow logs whenever a test in this module fails."""
    failures_before = request.session.testsfailed
    test_start_time = datetime.utcnow()
    yield
    if request.session.testsfailed <= failures_before:
        return
    node_names = []
    try:
        ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
        if ret_code == 0:
            node_names = [n['metadata']['labels']['kubernetes.io/hostname'] for n in gpu_nodes]
    except Exception:
        pass
    anr_util.collect_workflow_logs(
        environment, node_names=node_names or None, since_time=test_start_time)


def test_npd_anr_combined_used_vram(request, gpu_cluster, images, deviceconfig_install, environment):
    """
    End-to-end test: GPU workload drives VRAM above threshold → NPD sets AMDGPUHighVRAM=True
    → ANR operator triggers a remediation workflow → drain evicts the workload
    → VRAM drops → NPD clears the condition → workflow is deleted.
    """
    metric_type    = "counter-metric"
    metric_name    = "GPU_USED_VRAM"
    idle_json_path = ("VRAMUsage", "UsedVRAM")  # (section, field) in /gpumetrics Response[].Stats; value in MB
    json_scale     = 1.0
    condition_type = "AMDGPUHighVRAM"

    Logger.info(f"Starting NPD+ANR combined test — condition={condition_type}, metric={metric_name}")

    # -----------------------------------------------------------------------
    # Finalizers (LIFO — register first → runs last)
    # -----------------------------------------------------------------------
    request.addfinalizer(lambda: npd_util.remove_npd_amdgpuhealth_plugin(environment))
    request.addfinalizer(lambda: anr_util.cleanup_workflow(
        deviceconfig_install, environment, condition_type,
        {'remediationWorkflow.enable': False}, images=images))

    workload_ctxt = {}

    def _stop_workload():
        if workload_ctxt:
            K8Helper.workload_operation(
                environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **workload_ctxt)
    request.addfinalizer(_stop_workload)

    # -----------------------------------------------------------------------
    # Pre-Phase: Wait for metrics-exporter pod to be running so that
    # /var/lib/amd-metrics-exporter/amdgpuhealth exists on the host before
    # NPD is deployed and tries to validate that path.
    # -----------------------------------------------------------------------
    _k8s_core = client.CoreV1Api()
    exporter_ready = False
    for _ in range(20):          # 20 × 15s = 5 min max
        try:
            pods = _k8s_core.list_namespaced_pod(
                namespace=environment.gpu_operator_namespace,
                label_selector=f"daemonset-name={deviceconfig_install.devicecfg_list[0]},"
                               f"app.kubernetes.io/name=metrics-exporter").items
            if pods and all(p.status.phase == 'Running' for p in pods):
                Logger.info("Metrics-exporter pod is Running")
                exporter_ready = True
                break
            phases = [p.status.phase for p in pods] if pods else ['not found']
            Logger.info(f"Metrics-exporter pods: {phases} — waiting...")
        except Exception as e:
            Logger.warning(f"Error checking metrics-exporter pod: {e}")
        time.sleep(15)
    K8Helper.triage(environment, exporter_ready,
                    "Metrics-exporter pod did not become Running within 5 min")

    # Give the exporter a moment to write amdgpuhealth to /var/lib/amd-metrics-exporter
    time.sleep(30)

    # -----------------------------------------------------------------------
    # Phase 1: Select target node and measure idle metric → dynamic threshold
    # -----------------------------------------------------------------------
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, len(gpu_nodes) > 0, "No nodes with AMD/GPU found in the cluster")

    target_node = anr_util.get_worker_nodes(gpu_nodes, strict=True)
    node_name   = k8_util.k8_get_node_hostname(target_node)
    Logger.info(f"Target node: {node_name}")

    exporter_pod_name = None
    try:
        pods = _k8s_core.list_namespaced_pod(
            namespace=environment.gpu_operator_namespace,
            label_selector=f"daemonset-name={deviceconfig_install.devicecfg_list[0]},"
                           f"app.kubernetes.io/name=metrics-exporter").items
        exporter_pod_name = next(
            (p.metadata.name for p in pods
             if p.spec.node_name == node_name and p.status.phase == "Running"), None)
    except Exception as e:
        Logger.warning(f"Error finding exporter pod on {node_name}: {e}")
    K8Helper.triage(environment, exporter_pod_name is not None,
                    f"Exporter pod not found on {node_name} — cannot compute dynamic threshold")

    dynamic_threshold = None
    rc, curl_out, curl_err = k8_util.exec_command_in_pod(
        environment.gpu_operator_namespace,
        ["curl", "-s", "localhost:5000/gpumetrics"],
        exporter_pod_name,
        container_name="metrics-exporter-container")
    if rc != 0:
        Logger.warning(f"curl gpumetrics failed on {exporter_pod_name} (rc={rc}): {curl_err}")
    if rc == 0 and curl_out:
        try:
            raw = str(curl_out)
            raw = raw[raw.find('{'):raw.rfind('}')+1]
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                data = ast.literal_eval(raw)
            i_sec, i_fld = idle_json_path
            gpu_list = data.get("Response", []) if isinstance(data, dict) else []
            idle_values = [
                gpu["Stats"][i_sec][i_fld] * json_scale
                for gpu in gpu_list
                if gpu.get("Stats", {}).get(i_sec, {}).get(i_fld) is not None
            ]
            if idle_values:
                dynamic_threshold = int(max(idle_values) * 1.1)
                Logger.info(f"Idle {metric_name}: max={max(idle_values):.1f} → threshold={dynamic_threshold} (idle+10%)")
        except Exception as e:
            Logger.warning(f"Failed to parse /gpumetrics: {e}")
    K8Helper.triage(environment, dynamic_threshold is not None,
                    f"Could not measure idle {metric_name} from /gpumetrics")

    # -----------------------------------------------------------------------
    # Phase 2: Deploy NPD and verify idle state (condition=False)
    # -----------------------------------------------------------------------
    npd_util.remove_npd_amdgpuhealth_plugin(environment)
    ret_code = npd_util.deploy_npd_custom_condition(
        environment, metric_type, metric_name, dynamic_threshold,
        condition_type,
        "GPUVRAMNormal", "GPUVRAMHigh",
        "GPU VRAM usage is within normal range", "GPU VRAM usage exceeds threshold",
        invoke_interval="15s")
    K8Helper.triage(environment, ret_code == 0,
                    f"Failed to deploy NPD custom condition '{condition_type}'")

    # Patch NPD DaemonSet with hostNetwork + METRICS_EXPORTER_PORT so amdgpuhealth
    # can reach the metrics exporter on localhost:5000.
    # Applied before wait_for_npd_daemonset_ready so one rollout covers both patches.
    patch_rc, _, patch_err = k8_util.k8_patch_daemonset(
        npd_util.NPD_APP_NAME, npd_util.NPD_NAMESPACE, {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {"name": npd_util.NPD_APP_NAME, "namespace": npd_util.NPD_NAMESPACE},
            "spec": {
                "selector": {"matchLabels": {"app": npd_util.NPD_APP_NAME}},
                "template": {
                    "metadata": {"labels": {"app": npd_util.NPD_APP_NAME}},
                    "spec": {
                        "hostNetwork": True,
                        "dnsPolicy": "ClusterFirstWithHostNet",
                        "containers": [{
                            "name": npd_util.NPD_APP_NAME,
                            "env": [
                                {"name": "NODE_NAME",
                                 "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}},
                                {"name": "METRICS_EXPORTER_PORT", "value": "5000"},
                            ]
                        }]
                    }
                }
            }
        })
    K8Helper.triage(environment, patch_rc == 0,
                    f"Failed to patch NPD DaemonSet with hostNetwork/METRICS_EXPORTER_PORT: {patch_err}")

    ds_ready = npd_util.wait_for_npd_daemonset_ready(
        npd_util.NPD_NAMESPACE, npd_util.NPD_APP_NAME, timeout=120)
    K8Helper.triage(environment, ds_ready,
                    f"NPD DaemonSet '{npd_util.NPD_APP_NAME}' failed to rollout")

    condition_ok, failed = npd_util.verify_npd_node_condition(
        [target_node], condition_type, expected_status="False", timeout=120)
    K8Helper.triage(environment, condition_ok,
                    f"Idle check failed — {condition_type} not False on: {failed}")
    Logger.info(f"Idle state verified — {condition_type}=False on '{node_name}'")

    # -----------------------------------------------------------------------
    # Phase 3: Drive the metric above threshold
    # -----------------------------------------------------------------------
    gpu_cap, _ = k8_util.k8_get_node_gpu_capacity(node_name)
    ctxt = K8Helper.workload_operation(
        environment, K8Helper.WorkloadOp.START_WORKLOAD,
        node_name=node_name, images=images, num_gpu_reqd=gpu_cap)
    K8Helper.triage(environment,
                    ctxt['podStatus'] == K8Helper.PodStatus.RUNNING,
                    f"Workload failed to start on {node_name}: {ctxt}")
    workload_ctxt.update(ctxt)
    Logger.info(f"Workload started on {node_name}; waiting 30s for metric to stabilize")
    time.sleep(30)

    # -----------------------------------------------------------------------
    # Phase 4: Verify NPD detects the problem (condition=True)
    # -----------------------------------------------------------------------
    condition_ok, failed = npd_util.verify_npd_node_condition(
        [target_node], condition_type, expected_status="True", timeout=120)
    K8Helper.triage(environment, condition_ok,
                    f"NPD did not detect problem — {condition_type} not True on: {failed}")
    Logger.info(f"NPD detected {condition_type}=True on '{node_name}'")

    # -----------------------------------------------------------------------
    # Phase 5: Enable ANR remediation and configure workflow mapping
    # -----------------------------------------------------------------------
    framework, recipe, recipe_timeout = anr_util.get_framework_and_recipe(gpu_cluster, target_node)
    devcfg_name = ''

    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['remediationWorkflow.enable'] = True
        tcfg['remediationWorkflow.configMapImage.repository'] = images.get('remediationWorkflow.configMapImage.repository')
        tcfg['remediationWorkflow.configMapImage.version'] = images.get('remediationWorkflow.configMapImage.version')
        if framework == "AGFHC":
            tcfg['remediationWorkflow.testerImage.repository'] = images['testRunnerAgfhc.image.repository']
            tcfg['remediationWorkflow.testerImage.version'] = images['testRunnerAgfhc.image.version']
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, _, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, ret_code == 0,
                        f"Failed to enable remediation: {ret_stderr}")
        devcfg_name = cr_spec['metadata']['name']

    configmap_ready = anr_util.wait_for_configmap_from_image(environment, devcfg_name, timeout=120)
    K8Helper.triage(environment, configmap_ready,
                    "ConfigMap was not created from configMapImage within timeout")

    template_found = False
    for attempt in range(12):    # up to 60s
        ret_code, workflowtemplates, _ = k8_util.k8_get_custom_resource_objects(
            "argoproj.io", "v1alpha1", "workflowtemplates")
        if ret_code == 0 and workflowtemplates:
            if "default-template" in [t['metadata']['name'] for t in workflowtemplates]:
                template_found = True
                break
        Logger.info(f"Attempt {attempt+1}/12: WorkflowTemplate 'default-template' not yet available, waiting...")
        time.sleep(5)
    K8Helper.triage(environment, template_found,
                    "WorkflowTemplate 'default-template' not found after 60s")

    configmap_name = f"{devcfg_name}-default-conditional-workflow-mappings"
    ret_code, _, err = k8_util.k8_patch_workflow_config(
        environment.gpu_operator_namespace, configmap_name, {
            "nodeCondition": condition_type,
            "workflowTemplate": "default-template",
            "physicalActionNeeded": False,
            "skipRebootStep": True,
            "notifyRemediationMessage": f"Remediation triggered for {condition_type}.",
            "notifyTestFailureMessage": f"Validation failed after remediation for {condition_type}.",
            "recoveryPolicy": {"maxAllowedRunsPerWindow": 3, "windowSize": "15m"},
            "validationTestsProfile": {
                "framework": framework,
                "recipe": recipe,
                "iterations": 1,
                "stopOnFailure": True,
                "timeoutSeconds": recipe_timeout,
            },
        })
    K8Helper.triage(environment, ret_code == 0, f"Failed to patch configmap: {err}")
    time.sleep(10)

    # -----------------------------------------------------------------------
    # Phase 6: Monitor workflow until suspend, confirm condition cleared, delete workflow
    # -----------------------------------------------------------------------
    # Drain evicts the workload → metric drops → NPD clears condition=False.
    # Once NPD confirms condition=False the remediation goal is achieved delete the workflow and remove the taint.
    ret_code, _, stderr = anr_util.monitor_and_patch_remediation(
        environment, node_name, condition_type=condition_type,
        timeout=300, skip_test_patch=True,
        stop_at_step=('suspend', 'Running'))
    K8Helper.triage(environment, ret_code == 0,
                    f"Workflow did not reach suspend step: {stderr}")

    condition_ok, failed = npd_util.verify_npd_node_condition(
        [target_node], condition_type, expected_status="False", timeout=120)
    K8Helper.triage(environment, condition_ok,
                    f"{condition_type} not cleared by NPD after drain — still True on: {failed}")
    Logger.info(f"NPD cleared {condition_type}=False on '{node_name}' after drain")

    _, workflows, _ = k8_util.k8_get_custom_resource_objects("argoproj.io", "v1alpha1", "workflows")
    for wf in (workflows or []):
        if anr_util._is_node_workflow(wf, node_name):
            wf_name = wf['metadata']['name']
            ret_code, _, err = k8_util.k8_delete_custom_resource(
                group="argoproj.io", version="v1alpha1", plural="workflows",
                namespace=environment.gpu_operator_namespace, name=wf_name)
            if ret_code == 0:
                Logger.info(f"Deleted workflow '{wf_name}' — remediation goal achieved")
            else:
                Logger.warning(f"Failed to delete workflow '{wf_name}': {err}")

    applied_taints = target_node.get('spec', {}).get('taints') or []
    for taint in applied_taints:
        if taint.get('key') == 'amd-gpu-unhealthy':
            k8_util.k8_untaint_node(node_name, effects=[taint['effect']],
                                    taint_key=taint['key'], taint_value=taint.get('value', ''))
            Logger.info(f"Removed taint '{taint['key']}={taint.get('value')}' from '{node_name}'")

    Logger.info("test_npd_anr_combined_used_vram PASSED")
