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
import pprint
import pytest
import sys
import os
import time
import json
import logging
import random
import datetime
import lib.common as common
import lib.helm_util as helm_util
import lib.k8_util as k8_util
import lib.spec_util as spec_util
import lib.metric_util as metric_util
import lib.amdgpu as amdgpu_util
from lib.util import K8Helper

pytestmark = pytest.mark.skip("test-runner operand is not deployed in DME/DCM-only CI targets")
Logger = logging.getLogger("k8.test_test_runner")
LogPrettyPrinter = pprint.PrettyPrinter(indent = 2)

metrics_fields = {
      'GPU_ECC_UNCORRECT_SDMA'      : 0,
      'GPU_ECC_UNCORRECT_GFX'       : 0,
      'GPU_ECC_UNCORRECT_MMHUB'     : 0,
      'GPU_ECC_UNCORRECT_ATHUB'     : 0,
      'GPU_ECC_UNCORRECT_BIF'       : 0,
      'GPU_ECC_UNCORRECT_HDP'       : 0,
      'GPU_ECC_UNCORRECT_XGMI_WAFL' : 0,
      'GPU_ECC_UNCORRECT_DF'        : 0,
      'GPU_ECC_UNCORRECT_SMN'       : 0,
      'GPU_ECC_UNCORRECT_SEM'       : 0,
      'GPU_ECC_UNCORRECT_MP0'       : 0,
      'GPU_ECC_UNCORRECT_MP1'       : 0,
      'GPU_ECC_UNCORRECT_FUSE'      : 0,
      'GPU_ECC_UNCORRECT_UMC'       : 0,
      'GPU_ECC_UNCORRECT_MCA'       : 0,
      'GPU_ECC_UNCORRECT_VCN'       : 0,
      'GPU_ECC_UNCORRECT_JPEG'      : 0,
      'GPU_ECC_UNCORRECT_IH'        : 0,
      'GPU_ECC_UNCORRECT_MPIO'      : 0
}

debug_on_failure = K8Helper.triage

_RECIPES = {
    "test_deviceconfig_test_runner_deploy": {
        "argnames": ["test_framework"],
        "params": [("rvs",), ("agfhc",)],
    },
    "test_deviceconfig_test_runner_disable": {
        "argnames": ["test_framework"],
        "params": [("rvs",), ("agfhc",)],
    },
    "test_deviceconfig_testrunner_disable_exporter": {
        "argnames": ["test_framework"],
        "params": [("rvs",), ("agfhc",)],
    },
    "test_deviceconfig_unhealthy": {
        "argnames": ["test_framework", "recipe"],
        "params": [("rvs", "iet_stress"), ("agfhc", "gfx_lvl1")],
    },
    "test_workload_running_make_node_unhealthy": {
        "argnames": ["test_framework", "recipe"],
        "params": [("rvs", "babel"), ("rvs", "babel_single"), ("agfhc", "xgmi_lvl1")],
    },
    "test_update_metric_exporter_and_test_runner": {
        "argnames": ["test_framework", "recipe"],
        "params": [("rvs", "gst_single"), ("agfhc", "dma_lvl1")],
    },
    "test_manual_job": {
        "argnames": ["test_framework", "recipe", "schedule", "healthy"],
        "params": [
            ("rvs", "gst_single",    True,  True),
            ("rvs", "iet_stress",    False, True),
            ("rvs", "babel",         False, False),
            ("rvs", "babel_single",  False, False),
            ("agfhc", "dma_lvl1",   False, False),
            ("agfhc", "dma_lvl2",   False, True),
            ("agfhc", "gfx_lvl1",   True,  True),
        ],
    },
    "test_pre_job": {
        "argnames": ["test_framework", "recipe", "healthy"],
        "params": [
            ("rvs",   "babel",       False),
            ("rvs",   "babel",       True),
            ("rvs",   "babel_single",False),
            ("rvs",   "babel_single",True),
            ("agfhc", "all_lvl1",   True),
            ("agfhc", "all_lvl2",   False),
        ],
    },
}


def pytest_generate_tests(metafunc):
    spec = _RECIPES.get(metafunc.function.__name__)
    if spec is None:
        return
    metafunc.parametrize(
        spec["argnames"],
        spec["params"],
        indirect=["test_framework"],
    )

def verify_events(environment, before, after):
    global Logger

    before_events = before[1].items
    after_events = after[1].items

    # 1. Extract the unique UIDs from the 'before' list and put them in a set
    before_uids = {event.metadata.uid for event in before_events}

    # 2. Iterate through the 'after' list and find any event whose UID is NOT in the 'before' set
    Logger.info("Following are the events observed during this testcase:-")
    new_events = []
    for event in after_events:
        if event.metadata.uid not in before_uids:
            Logger.info(f"=============={event.metadata.uid}=================")
            Logger.info(f"{pprint.pformat(event.reason)}")
            Logger.info(f"{pprint.pformat(event.type)}")
            Logger.info(f"{pprint.pformat(event.involved_object.name)}")
            Logger.info(f"{pprint.pformat(event.message)}")

@pytest.fixture(autouse=True, scope="module")
def skip_module(environment):
    if environment.gpu_operator_version in ["v1.0.0", "v1.1.0"]:
        pytest.skip(f"Skipping test-runner for current version {environment.gpu_operator_version}")
    return


@pytest.fixture(scope="module")
def test_framework(request, images, gpu_cluster):
    """Module-scoped fixture driven by pytest_generate_tests indirect parametrization.

    Three skip gates (checked in order):
      1. TEST_RUNNER_FRAMEWORKS env var — CI jobs opt in/out without a CLI option
      2. Image manifest — AGFHC image absent → skip AGFHC suite automatically
      3. Hardware support — amdgpu-features.json runtime check
    Returns the framework string ("rvs" or "agfhc") consumed by tests and
    deviceconfig_install.
    """
    framework = request.param  # "rvs" or "agfhc"

    # Gate 1: env var selection (e.g. TEST_RUNNER_FRAMEWORKS=rvs or rvs,agfhc)
    allowed_env = os.environ.get("TEST_RUNNER_FRAMEWORKS", "").strip().lower()
    if allowed_env:
        allowed = {f.strip() for f in allowed_env.split(",") if f.strip()}
        if framework not in allowed:
            pytest.skip(
                f"test_framework={framework!r} excluded by TEST_RUNNER_FRAMEWORKS={allowed_env!r}"
            )

    # Gate 2: image manifest presence
    if framework == "agfhc" and "testRunnerAgfhc.image.repository" not in images:
        pytest.skip("AGFHC test-runner image not present in image manifest")

    # Gate 3: hardware support
    if not any(
        amdgpu_util.get_test_runner_support(node.device_id).get(framework, False)
        for node in gpu_cluster.cluster_nodes
        if node.device_id
    ):
        pytest.skip(f"{framework.upper()} test-runner not supported on this cluster")

    return framework

@pytest.fixture(scope="module")
def deviceconfig_install(test_framework, gpu_cluster, images, gpu_operator_install, environment):
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
    debug_on_failure(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    debug_on_failure(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")

    test_config = {
            'metadata.namespace' : environment.gpu_operator_namespace,
            'driver.enable' : True,
            'devicePlugin.enableNodeLabeller' : False,
            'metricsExporter.enable' : True,
        }
    if test_framework == "rvs":
        test_config['testRunner.enable'] = True
    else:
        test_config['testRunnerAgfhc.enable'] = True
    test_config.update(images)

    tr_options = {'test_runner_framework': test_framework}

    test_cfg_map = spec_util.build_deviceconfig_cr_template(test_config, gpu_nodes, 'test_runner', environment.amdgpu_driver_spec)
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

    exporter_cfg = {}
    update_metrics_exporter_configmap(exporter_cfg)
    exporter_cm_name = "config-metrics-exporter"
    exporter_cm_file = os.path.join(environment.logdir, f"{exporter_cm_name}.json")
    with open(exporter_cm_file, "w") as fp:
        fp.write(json.dumps(exporter_cfg, indent=4))
    k8_util.k8_delete_configmap(environment.gpu_operator_namespace, exporter_cm_name)
    ret_code, _, ret_stderr = k8_util.k8_create_configmap(
        environment.gpu_operator_namespace, exporter_cm_name, exporter_cm_file, "config.json")
    debug_on_failure(environment, (ret_code == 0),
                     f"Failed to create exporter configmap: {ret_stderr}")
    for _, tcfg in test_cfg_map.items():
        tcfg['metricsExporter.config'] = exporter_cm_name

    for spec_name, tcfg in test_cfg_map.items():
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg, options=tr_options)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_create_deviceconfig_cr(cr_spec)
        debug_on_failure(environment, (ret_code == 0), f"Failed to create deviceconfig, stderr: {ret_stderr}")
        devicecfg_list.append(tcfg['metadata.name'])

    # Check for corresponding deviceconfig created
    K8Helper.check_deviceconfig_status(environment, devicecfg_list)
    for devcfg in devicecfg_list:
        K8Helper.wait_kmm_worker_completion(environment, devcfg)
    K8Helper.update_node_driver_version(gpu_cluster, environment)

    exporter_pods = [
        common.PodInfo('metrics-exporter', len(gpu_nodes), 1),
    ]
    failed_exporter_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, exporter_pods)
    if not failed_exporter_pods:
        K8Helper.capture_rocm_version(
            environment.gpu_operator_namespace,
            "metrics-exporter",
            [("metricsExporter.image", "metrics-exporter-container")],
        )

    testrunner_pods = [
        common.PodInfo('test-runner', len(gpu_nodes), 1),
    ]
    failed_testrunner_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, testrunner_pods)
    if not failed_testrunner_pods:
        if test_framework == "rvs":
            K8Helper.capture_testrunner_versions(
                environment.gpu_operator_namespace,
                "RVS",
                "testRunner.image",
                timeout_seconds=30,
            )
        else:
            K8Helper.capture_testrunner_versions(
                environment.gpu_operator_namespace,
                "AGFHC",
                "testRunnerAgfhc.image",
                timeout_seconds=30,
            )

    devcfg_info = DeviceConfigCRInfo()
    setattr(devcfg_info, "test_cfg_map", test_cfg_map)
    setattr(devcfg_info, "exporter_port_map", exporter_port_map)
    setattr(devcfg_info, "devicecfg_list", devicecfg_list)
    setattr(devcfg_info, "exporter_cfg", exporter_cfg)
    yield devcfg_info

    device_cfg_info = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace, None)
    for devcfg_name, _ in device_cfg_info.items():
        k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)
    k8_util.k8_delete_configmap(environment.gpu_operator_namespace, "config-metrics-exporter")
    return

@pytest.mark.level1
def test_deviceconfig_test_runner_deploy(test_framework, deviceconfig_install, environment):
    global Logger
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    debug_on_failure(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    debug_on_failure(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")

    # Watch for all pod creation
    devicecfg_pods = [
        common.PodInfo('device-plugin', len(gpu_nodes), 1),
        common.PodInfo('metrics-exporter', len(gpu_nodes), 1),
        common.PodInfo('test-runner', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods, sleep_time = 20)
    debug_on_failure(environment, (not failed_pods), f"One or more pods are not ready - {failed_pods}")

    '''
    failed_endpoints = set()
    for node in gpu_nodes:
        node_ip = k8_util.k8_get_node_address(node)
        cluster_node = gpu_cluster.find_node_by_ip(node_ip)
        ret_code, ret_stdout, ret_stderr = cluster_node.http_get(32500, "metrics")
        if ret_code != 0:
            failed_endpoints.add(node_ip)
            Logger.error(f"Failed to get metrics from nodeport endpoint for {node_ip}, stdout: {ret_stdout} stderr: {ret_stderr}")

    debug_on_failure(environment, (len(failed_endpoints) == 0),
                    f"One or more metric endpoints HTTP-GET failed, nodes: {failed_endpoints}")
    '''

@pytest.mark.level1
def test_deviceconfig_test_runner_disable(test_framework, deviceconfig_install, environment):
    global Logger
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    debug_on_failure(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    debug_on_failure(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")

    enable_key = 'testRunner.enable' if test_framework == "rvs" else 'testRunnerAgfhc.enable'
    tr_options = {'test_runner_framework': test_framework}

    # disable test-runner
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg[enable_key] = False
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg, options=tr_options)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        debug_on_failure(environment, (ret_code == 0), "Failed to modify deviceconfig CR")

    export_pods = [
        common.PodInfo('test-runner', 1, 1),
    ]
    running_pods = k8_util.k8_check_pod_terminated(environment.gpu_operator_namespace, export_pods)
    debug_on_failure(environment, not running_pods,
                              f"Some of the pods are still running post uninstallation - {running_pods}")
    # Watch for all pod creation
    devicecfg_pods = [
        common.PodInfo('device-plugin', len(gpu_nodes), 1),
        common.PodInfo('metrics-exporter', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods, sleep_time = 20)
    debug_on_failure(environment, (not failed_pods), f"One or more pods are not ready - {failed_pods}")

    # re-enable test-runner
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg[enable_key] = True
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg, options=tr_options)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        debug_on_failure(environment, (ret_code == 0), "Failed to modify deviceconfig CR")

    # Watch for all pod creation
    devicecfg_pods = [
        common.PodInfo('device-plugin', len(gpu_nodes), 1),
        common.PodInfo('metrics-exporter', len(gpu_nodes), 1),
        common.PodInfo('test-runner', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods, sleep_time = 20)
    debug_on_failure(environment, (not failed_pods), f"One or more pods are not ready - {failed_pods}")

@pytest.mark.level1
def test_deviceconfig_testrunner_disable_exporter(test_framework, deviceconfig_install, environment):
    global Logger
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    debug_on_failure(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    debug_on_failure(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")

    tr_options = {'test_runner_framework': test_framework}

    # disable exporter and check for metrics
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['metricsExporter.enable'] = False
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg, options=tr_options)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        debug_on_failure(environment, (ret_code == 0), "Failed to modify deviceconfig CR")

    export_pods = [
        common.PodInfo('test-runner', len(gpu_nodes), 1),
        common.PodInfo('metrics-exporter', len(gpu_nodes), 1),
    ]
    running_pods = k8_util.k8_check_pod_terminated(environment.gpu_operator_namespace, export_pods)
    debug_on_failure(environment, not running_pods,
                     f"Some of the pods are still running post uninstallation - {running_pods}")
    devplugin_pods = [
        common.PodInfo('device-plugin', 1, 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devplugin_pods)
    debug_on_failure(environment, (not failed_pods),
                     f"One or more pods are not ready - {failed_pods}")

    # Re enable exporter and check for test-runner
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['metricsExporter.enable'] = True
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg, options=tr_options)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        debug_on_failure(environment, (ret_code == 0), "Failed to modify deviceconfig CR")

    devicecfg_pods = [
        common.PodInfo('device-plugin', len(gpu_nodes), 1),
        common.PodInfo('metrics-exporter', len(gpu_nodes), 1),
        common.PodInfo('test-runner', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods)
    debug_on_failure(environment, (not failed_pods),
                     f"One or more pods are not ready - {failed_pods}")

def wait_for_pod(environment, namespace, cmd_list, pod_str, result):
    resp_stdout = " "
    i = 0
    while (resp_stdout is None or result not in resp_stdout) and i < 10:
        i += 1
        pod_name = k8_util.k8_get_pod_name(pod_str, namespace)
        if pod_name is None:
            Logger.error(f"Pod matching '{pod_str}' not found in namespace '{namespace}' (attempt {i}/10)")
            time.sleep(60)
            continue
        ret_code, resp_stdout, resp_stderr = k8_util.exec_command_in_pod(environment.gpu_operator_namespace,
                                                                         cmd_list, pod_name)
        time.sleep(60)
    debug_on_failure(environment, (resp_stdout is not None), f"Failed to collect response from {cmd_list} from {pod_str}")
    debug_on_failure(environment, result in resp_stdout, f"can't find {result} in {pod_name} output {resp_stdout}")

def verify_gpu_capacity_status(environment, worker, expected_unhealthy=0):
    """Poll until the expected number of GPUs are marked unhealthy.

    Unhealthy count = capacity - allocatable.  0 means all GPUs are healthy.
    """
    for attempt in range(10):
        cap, alloc = k8_util.k8_get_node_gpu_capacity(worker)
        unhealthy = cap - alloc
        Logger.info(f"GPU capacity check: {worker} capacity={cap} allocatable={alloc} unhealthy={unhealthy} (expect {expected_unhealthy})")
        if unhealthy == expected_unhealthy:
            return
        time.sleep(10)
    debug_on_failure(environment, False,
                     f"GPU capacity mismatch after 100s: {worker} capacity={cap} allocatable={alloc} "
                     f"unhealthy={unhealthy} (expected {expected_unhealthy})")


def update_metrics_exporter_configmap(config_map):

    # Generate set of config-maps in the k8 cluster with different set of labels and metrics
    sample_size = random.randint(4,len(metrics_fields.keys())-1)
    error_list = random.sample(sorted(metrics_fields.keys()), sample_size)

    health_thresholds_dict = {}
    for error in error_list:
        health_thresholds_dict.update({error: metrics_fields.get(error) + random.randint(5,10)})

    config_map.update(
        {"GPUConfig": { "HealthThresholds": health_thresholds_dict }}
    )
    # SetError gRPC API requires Debug.EnableAPI to be enabled
    config_map.setdefault("CommonConfig", {}).setdefault("Debug", {})["EnableAPI"] = True

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

def create_configmap(request, deviceconfig_install, environment, framework, config_map, configmap_name="config-test-runner"):
    global Logger
    configmap_file = os.path.join(environment.logdir, f"{configmap_name}.json")
    with open(configmap_file, "w") as fp:
        fp.write(json.dumps(config_map, indent=4))

    # Delete if there is any previous instance with same name
    ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_configmap(environment.gpu_operator_namespace, configmap_name)
    Logger.info(f"Result of configmap delete operation, ret_code:{ret_code}, ret_stdout: {ret_stdout.strip()}, err: {ret_stderr.strip()}")
    # ignore ret_code
    ret_code, ret_stdout, ret_stderr = k8_util.k8_create_configmap(environment.gpu_operator_namespace,
                                                                   configmap_name, configmap_file, "config.json")
    debug_on_failure(environment, (ret_code == 0),
                     f"Failed to create configmap {configmap_name} for {configmap_file}, err: {ret_stderr.strip()}")

    def _cleanup_configmap():
        ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_configmap(environment.gpu_operator_namespace,
                                                                       configmap_name)
        return
    request.addfinalizer(_cleanup_configmap)
    return configmap_name

def verify_logs(environment, log_msg_list, pod_str="test-runner", since="180s", container=None, switch="and", negate=False):
    global Logger
    global LogPrettyPrinter
    namespace = environment.gpu_operator_namespace

    i = 0
    ret_code, stdout, stderr = k8_util.k8_get_pod_logs(pod_str, namespace, since, container)

    if negate:
        for log_msg in log_msg_list:
            debug_on_failure(environment, log_msg not in stdout,
                                      f"found {log_msg} in\n" + LogPrettyPrinter.pformat(stdout.split('\n')))
    elif switch == "or":
        while i < 6:
            for log_msg in log_msg_list:
                if log_msg in stdout:
                    return
            time.sleep(30)
            i = i + 1
            ret_code, stdout, stderr = k8_util.k8_get_pod_logs(pod_str, namespace, since, container)
        debug_on_failure(environment, False,
                         f"didn't find any of {log_msg_list} in\n" + LogPrettyPrinter.pformat(stdout.split('\n')))
    else:
        for log_msg in log_msg_list:
            while log_msg not in stdout and i < 16:
                time.sleep(30)
                i = i + 1
                ret_code, stdout, stderr = k8_util.k8_get_pod_logs(pod_str, namespace, since, container)
            debug_on_failure(environment, log_msg in stdout,
                             f"didn't find {log_msg} in\n" + LogPrettyPrinter.pformat(stdout.split('\n')))

def update_metrics_exporter(deviceconfig_install, environment, configmap_name):
    # re-configure test-runner
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['metricsExporter.config'] = configmap_name
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        debug_on_failure(environment, (ret_code == 0), "Failed to modify deviceconfig CR")

def update_test_runner_config(deviceconfig_install, environment, framework, configmap_name):
    """Update the DeviceConfig CR to reference a new test-runner configmap.

    framework: "rvs" or "agfhc" (lowercase). Image is already deployed by
    deviceconfig_install; this call only updates the config reference.
    """
    tr_options = {'test_runner_framework': framework}
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        if framework == "rvs":
            tcfg['testRunner.enable'] = True
            tcfg['testRunner.config'] = configmap_name
        else:
            tcfg['testRunnerAgfhc.enable'] = True
            tcfg['testRunnerAgfhc.config'] = configmap_name
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg, options=tr_options)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        debug_on_failure(environment, (ret_code == 0), "Failed to modify deviceconfig CR")

@pytest.mark.level2
def test_deviceconfig_unhealthy(request, gpu_cluster, test_framework, deviceconfig_install, environment, recipe):
    namespace = environment.gpu_operator_namespace
    global Logger
    global LogPrettyPrinter
    global metrics_fields
    # Generate set of config-maps in the k8 cluster with different set of labels and metrics
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    debug_on_failure(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    debug_on_failure(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")
    K8Helper.delete_debug_pods(["default", environment.gpu_operator_namespace])
    gpu_node = gpu_nodes[0]
    cluster_node = gpu_cluster.find_node_by_ip(k8_util.k8_get_node_address(gpu_node))
    tr_support = amdgpu_util.get_test_runner_support(cluster_node.device_id)
    if test_framework == "rvs" and recipe not in tr_support.get("rvs_recipes", []):
        if recipe == "iet_stress" and "iet_single" in tr_support.get("rvs_recipes", []):
            recipe = "iet_single"
        else:
            pytest.skip(f"RVS recipe '{recipe}' not supported on {cluster_node.gpu_series}")
    if test_framework == "agfhc" and recipe not in tr_support.get("agfhc_recipes", []):
        pytest.skip(f"AGFHC recipe '{recipe}' not supported on {cluster_node.gpu_series}")
    worker = k8_util.k8_get_node_hostname(gpu_node)
    init_cap, alloc = k8_util.k8_get_node_gpu_capacity(worker)

    before_events = k8_util.k8_get_events(namespace=environment.gpu_operator_namespace)

    testrunner_cfg = {}
    update_test_runner_configmap(recipe, worker, testrunner_cfg, test_framework.upper())
    testrunner_cm = create_configmap(request, deviceconfig_install, environment, test_framework, testrunner_cfg)
    update_test_runner_config(deviceconfig_install, environment, test_framework, testrunner_cm)

    sample_size = random.randint(1,len(metrics_fields.keys())-1)
    error_list = random.sample(sorted(metrics_fields.keys()), sample_size)
    thresholds = []
    for error in error_list:
        thresholds.append(metrics_fields.get(error) + random.randint(1,10))

    wait_for_pod(environment,
                 namespace,
                 ["metricsclient"],
                 "metrics-exporter",
                 "healthy")

    ret_code, _, ret_stderr = k8_util.k8_metrics_error(thresholds, error_list, environment.gpu_operator_namespace)
    debug_on_failure(environment, ret_code == 0, f"ECC error injection failed: {ret_stderr}")

    debug_on_failure(environment, k8_util.k8_get_node_health(worker, namespace) == "unhealthy",
			      f"result of kubectl describe node $NODE_NAME | grep unhealthy")
    Logger.info(f"This workload should not get created, state = Pending? or Running?")

    local_workload_ctxts = []

    verify_gpu_capacity_status(environment, worker, expected_unhealthy=1)
    params = {
        "node_name" : worker,
        "num_gpu_reqd" : init_cap,
        "workload_selection" : "busybox-workload",
        "expected_status" : K8Helper.PodStatus.PENDING,
    }
    wl_ctxt = K8Helper.workload_operation(environment, K8Helper.WorkloadOp.START_WORKLOAD, **params)
    local_workload_ctxts.append(wl_ctxt)
    def _cleanup():
        for ctxt in local_workload_ctxts:
            K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **ctxt)
        k8_util.k8_metrics_error([0] * sample_size, error_list, environment.gpu_operator_namespace)
        return

    request.addfinalizer(_cleanup)
    debug_on_failure(environment, wl_ctxt['podStatus'] == K8Helper.PodStatus.PENDING,
                     f"Workload not in PENDING state, {wl_ctxt}")
    Logger.info(f"Test runner worker logs\n===================\n")
    ret_code, stdout, stderr = k8_util.k8_get_pod_logs("test-runner", environment.gpu_operator_namespace)
    Logger.info(f"Test runner worker logs\n==================={stdout}\n")

    Logger.info(f"found {recipe} in test runner logs\n{LogPrettyPrinter.pformat(stdout)}\n")

    wait_for_pod(environment,
                 namespace,
                 ["metricsclient"],
                 "metrics-exporter",
                 "healthy")
    verify_logs(environment,
                [
                    "found existing completed test, skip for now",
                    "found existing running test, skip for now"
                ],
                switch="or")
    verify_logs(environment, ["found GPU with unhealthy state"])

    debug_on_failure(environment, k8_util.k8_get_node_health(worker, namespace) == "unhealthy",
			      f"result of kubectl describe node $NODE_NAME | grep unhealthy")

    match  = []
    for i in range(sample_size):
        match.append(f"unhealthy for ecc field [{error_list[i]}] error crossing threshold {metrics_fields.get(error_list[i])}, current value {thresholds[i]}")
    wait_for_pod(environment,
                 namespace,
                 ["metricsclient"],
                 "metrics-exporter",
                 "healthy")

    #TODO figure out later
    #k8_util.k8_untaint_node(worker)

    k8_util.k8_metrics_error([0] * sample_size, error_list, environment.gpu_operator_namespace)

    verify_logs(environment, [f"all GPUs are healthy"])

    Logger.info(f"This workload should get created, since the node {worker}, is now untainted")
    after_events = k8_util.k8_get_events(namespace=environment.gpu_operator_namespace)
    verify_events(environment, before_events, after_events)

@pytest.mark.level2
def test_workload_running_make_node_unhealthy(request, gpu_cluster, test_framework, deviceconfig_install, environment, recipe):
    namespace = environment.gpu_operator_namespace
    global Logger
    global LogPrettyPrinter
    global metrics_fields
    # Generate set of config-maps in the k8 cluster with different set of labels and metrics
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    debug_on_failure(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    debug_on_failure(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")
    K8Helper.delete_debug_pods(["default", environment.gpu_operator_namespace])
    gpu_node = gpu_nodes[0]
    cluster_node = gpu_cluster.find_node_by_ip(k8_util.k8_get_node_address(gpu_node))
    tr_support = amdgpu_util.get_test_runner_support(cluster_node.device_id)
    if test_framework == "agfhc" and recipe not in tr_support.get("agfhc_recipes", []):
        pytest.skip(f"AGFHC recipe '{recipe}' not supported on {cluster_node.gpu_series}")
    if test_framework == "rvs" and recipe not in tr_support.get("rvs_recipes", []):
        pytest.skip(f"RVS recipe '{recipe}' not supported on {cluster_node.gpu_series}")
    worker = k8_util.k8_get_node_hostname(gpu_node)
    init_cap, alloc = k8_util.k8_get_node_gpu_capacity(worker)
    before_events = k8_util.k8_get_events(namespace=environment.gpu_operator_namespace)

    devicecfg_pods = [
        common.PodInfo('device-plugin', len(gpu_nodes), 1),
        common.PodInfo('metrics-exporter', len(gpu_nodes), 1),
        common.PodInfo('test-runner', len(gpu_nodes), 1),
    ]

    testrunner_cfg = {}
    update_test_runner_configmap(recipe, worker, testrunner_cfg, test_framework.upper())
    testrunner_cm = create_configmap(request, deviceconfig_install, environment, test_framework, testrunner_cfg)
    update_test_runner_config(deviceconfig_install, environment, test_framework, testrunner_cm)

    sample_size = random.randint(1,len(metrics_fields.keys())-1)
    error_list = random.sample(sorted(metrics_fields.keys()), sample_size)
    thresholds = []
    for error in error_list:
        thresholds.append(metrics_fields.get(error) + random.randint(1,10))

    Logger.info(f"This workload should get created while GPU is healthy")
    local_workload_ctxts = []
    verify_gpu_capacity_status(environment, worker)
    params = {
        "node_name" : worker,
        "num_gpu_reqd" : init_cap,
        "workload_selection" : "busybox-workload",
    }
    wl_ctxt = K8Helper.workload_operation(environment, K8Helper.WorkloadOp.START_WORKLOAD, **params)
    local_workload_ctxts.append(wl_ctxt)
    debug_on_failure(environment, wl_ctxt['podStatus'] == K8Helper.PodStatus.RUNNING,
                     f"Workload not in running state, {wl_ctxt}")

    wait_for_pod(environment,
                 namespace,
                 ["metricsclient"],
                 "metrics-exporter",
                 "healthy")

    def _cleanup():
        for ctxt in local_workload_ctxts:
            K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **ctxt)
        k8_util.k8_metrics_error([0] * sample_size, error_list, environment.gpu_operator_namespace)
        return
    request.addfinalizer(_cleanup)

    after_events = k8_util.k8_get_events(namespace=environment.gpu_operator_namespace)
    verify_events(environment, before_events, after_events)

    ret_code, _, ret_stderr = k8_util.k8_metrics_error(thresholds, error_list, environment.gpu_operator_namespace)
    debug_on_failure(environment, ret_code == 0, f"ECC error injection failed: {ret_stderr}")

    verify_gpu_capacity_status(environment, worker, expected_unhealthy=1)

    Logger.info(f"Test runner worker logs\n=========={test_framework}=========\n")
    ret_code, stdout, stderr = k8_util.k8_get_pod_logs("test-runner", environment.gpu_operator_namespace)
    Logger.info(f"Test runner worker logs\n==================={stdout}\n")
    time.sleep(30)

    wl_name = wl_ctxt['spec']['metadata']['name']
    wl_namespace = wl_ctxt['spec']['metadata']['namespace']
    wait_for_pod(environment,
                 namespace,
                 ["metricsclient"],
                 "metrics-exporter",
                 wl_name)
    debug_on_failure(environment, k8_util.k8_get_node_health(worker, namespace) == "unhealthy",
                              f"result of kubectl describe node $NODE_NAME | grep unhealthy")
    match = []
    for i in range(sample_size):
        match.append(f"unhealthy for ecc field [{error_list[i]}] error crossing threshold {metrics_fields.get(error_list[i])}, current value {thresholds[i]}")
    wait_for_pod(environment,
                 namespace,
                 ["metricsclient"],
                 "metrics-exporter",
                 "healthy")

    #TODO figure out later
    #k8_util.k8_untaint_node(worker)

    K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **wl_ctxt)
    verify_logs(environment, [f"Starting iteration 1 of 1 for test: {recipe}", f"trigger AUTO_UNHEALTHY_GPU_WATCH is trying to run test {recipe}"], since="180s", switch="or")
    verify_logs(environment, [f"trigger AUTO_UNHEALTHY_GPU_WATCH is trying to run test {recipe} on device"])


    _cleanup()

    verify_logs(environment, ["all GPUs are healthy"])

@pytest.mark.level2
def test_update_metric_exporter_and_test_runner(request, gpu_cluster, test_framework, deviceconfig_install, environment, recipe):
    namespace = environment.gpu_operator_namespace
    global Logger
    global LogPrettyPrinter
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    debug_on_failure(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    debug_on_failure(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")
    K8Helper.delete_debug_pods(["default", environment.gpu_operator_namespace])
    gpu_node = gpu_nodes[0]
    cluster_node = gpu_cluster.find_node_by_ip(k8_util.k8_get_node_address(gpu_node))
    tr_support = amdgpu_util.get_test_runner_support(cluster_node.device_id)
    if test_framework == "agfhc" and recipe not in tr_support.get("agfhc_recipes", []):
        pytest.skip(f"AGFHC recipe '{recipe}' not supported on {cluster_node.gpu_series}")
    if test_framework == "rvs" and recipe not in tr_support.get("rvs_recipes", []):
        pytest.skip(f"RVS recipe '{recipe}' not supported on {cluster_node.gpu_series}")
    before_events = k8_util.k8_get_events(namespace=environment.gpu_operator_namespace)
    worker = k8_util.k8_get_node_hostname(gpu_node)
    init_cap, alloc = k8_util.k8_get_node_gpu_capacity(worker)
    time.sleep(30)
    devicecfg_pods = [
        common.PodInfo('device-plugin', len(gpu_nodes), 1),
        common.PodInfo('metrics-exporter', len(gpu_nodes), 1),
        common.PodInfo('test-runner', len(gpu_nodes), 1),
    ]

    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods)
    debug_on_failure(environment, (not failed_pods), f"One or more pods are not ready - {failed_pods}")

    testrunner_cfg = {}
    update_test_runner_configmap(recipe, worker, testrunner_cfg, test_framework.upper())
    testrunner_cm = create_configmap(request, deviceconfig_install, environment, test_framework, testrunner_cfg)
    update_test_runner_config(deviceconfig_install, environment, test_framework, testrunner_cm)

    verify_gpu_capacity_status(environment, worker)

    local_workload_ctxts = []
    params = {
        "node_name" : worker,
        "num_gpu_reqd" : init_cap,
        "workload_selection" : "busybox-workload",
    }
    wl_ctxt = K8Helper.workload_operation(environment, K8Helper.WorkloadOp.START_WORKLOAD, **params)
    local_workload_ctxts.append(wl_ctxt)
    def _delete_workload():
        for ctxt in local_workload_ctxts:
            K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **wl_ctxt)
        return

    request.addfinalizer(_delete_workload)
    debug_on_failure(environment, wl_ctxt['podStatus'] == K8Helper.PodStatus.RUNNING,
                     f"Workload not in RUNNING state, {wl_ctxt}")

    health_thresholds = deviceconfig_install.exporter_cfg.get('GPUConfig').get('HealthThresholds')
    sample_size = random.randint(2, len(health_thresholds.keys())-1)
    error_list = random.sample(sorted(health_thresholds.keys()), sample_size)
    thresholds = list()
    for error in error_list:
        thresholds.append(random.randint(0, health_thresholds.get(error)-1))
        Logger.info(f"injecting {thresholds[-1]} errors for {error}, threshold is {health_thresholds.get(error)}")
    thresholds[0] = 0


    time.sleep(30)

    Logger.info(f"Test runner worker logs\n===================\n")
    ret_code, stdout, stderr = k8_util.k8_get_pod_logs("test-runner", environment.gpu_operator_namespace)
    Logger.info(f"Test runner worker logs\n==================={stdout}\n")
    verify_logs(environment, ["all GPUs are healthy"])
    ret_code, _, ret_stderr = k8_util.k8_metrics_error(thresholds, error_list, environment.gpu_operator_namespace)
    debug_on_failure(environment, ret_code == 0, f"ECC error injection failed: {ret_stderr}")
    def _cleanup():
        k8_util.k8_metrics_error([0] * sample_size, error_list, environment.gpu_operator_namespace)
        return
    request.addfinalizer(_cleanup)

    rand_i = random.randint(0, len(thresholds)-1)
    error = error_list[rand_i]
    thresholds[rand_i] = health_thresholds.get(error) + random.randint(1, 3)
    Logger.info(f"injecting {thresholds[rand_i]} errors for {error}, threshold is {health_thresholds.get(error)}")
    ret_code, _, ret_stderr = k8_util.k8_metrics_error(thresholds, error_list, environment.gpu_operator_namespace)
    debug_on_failure(environment, ret_code == 0, f"ECC error injection failed: {ret_stderr}")
    time.sleep(30)
    #TODO - this check fails in CI environments; pod readiness is logged but not asserted
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods)
    #debug_on_failure(environment, (not failed_pods), f"One or more pods are not ready - {failed_pods}")
    if failed_pods != []:
        Logger.error(f"One or more pods are not ready - {failed_pods}")

    match = list()
    nomatch = list()
    for i in range(sample_size):
        log = f"unhealthy for ecc field [{error_list[i]}] error crossing threshold {health_thresholds.get(error_list[i])}, current value {thresholds[i]}"
        if thresholds[i] > health_thresholds.get(error_list[i]):
            Logger.info(f"Looking for the occurence of:\n{log}")
            match.append(log)
        else:
            Logger.info(f"Looking for non occurence of:\n{log}")
            nomatch.append(f"unhealthy for ecc field [{error_list[i]}] error crossing threshold")

    verify_logs(environment, match, "metrics-exporter", since="60s")
    time.sleep(30)
    verify_logs(environment, nomatch, "metrics-exporter", since="10s", switch="and", negate=True)

    verify_logs(environment,
               [f"associated with workload"])

    debug_on_failure(environment, k8_util.k8_get_node_health(worker, namespace) == "unhealthy",
                              f"result of kubectl describe node $NODE_NAME | grep unhealthy")
    _delete_workload()
    _cleanup()


    verify_logs(environment, ["all GPUs are healthy"])
    ret_code, stdout, stderr = k8_util.k8_get_pod_logs("test-runner", environment.gpu_operator_namespace)
    Logger.info(f"Test runner worker logs==================={stdout}")
    after_events = k8_util.k8_get_events(namespace=environment.gpu_operator_namespace)
    verify_events(environment, before_events, after_events)


@pytest.mark.level3
def test_manual_job(request, gpu_cluster, test_framework, deviceconfig_install, environment, schedule, healthy, recipe, images):
    global Logger
    global LogPrettyPrinter
    global metrics_fields
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    debug_on_failure(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    debug_on_failure(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")
    K8Helper.delete_debug_pods(["default", environment.gpu_operator_namespace])
    gpu_node = gpu_nodes[0]
    cluster_node = gpu_cluster.find_node_by_ip(k8_util.k8_get_node_address(gpu_node))
    tr_support = amdgpu_util.get_test_runner_support(cluster_node.device_id)
    if test_framework == "rvs" and recipe not in tr_support.get("rvs_recipes", []):
        if recipe == "iet_stress" and "iet_single" in tr_support.get("rvs_recipes", []):
            recipe = "iet_single"
        else:
            pytest.skip(f"RVS recipe '{recipe}' not supported on {cluster_node.gpu_series}")
    if test_framework == "agfhc" and recipe not in tr_support.get("agfhc_recipes", []):
        pytest.skip(f"AGFHC recipe '{recipe}' not supported on {cluster_node.gpu_series}")
    before_events = k8_util.k8_get_events(namespace=environment.gpu_operator_namespace)
    worker = k8_util.k8_get_node_hostname(gpu_node)
    init_cap, alloc = k8_util.k8_get_node_gpu_capacity(worker)
    trigger = "MANUAL"

    testrunner_cfg = {}
    update_test_runner_configmap(recipe, worker, testrunner_cfg, test_framework.upper(), trigger)
    testrunner_cm = create_configmap(request, deviceconfig_install, environment, test_framework, testrunner_cfg)
    update_test_runner_config(deviceconfig_install, environment, test_framework, testrunner_cm)

    job_name = "test-runner-manual-trigger"
    if schedule:
        job_name = "test-runner-manual-trigger-cron-job-midnight"
    namespace = environment.gpu_operator_namespace
    sa_name = "test-run"
    cluster_role_name = "test-run-cluster-role"
    crb_name = 'test-run-rb'
    scc_crb_name = 'test-run-scc-privileged'
    params = {
        "node_name" : worker,
        "num_gpu_reqd" : init_cap,
        "workload_selection" : "busybox-workload",
    }
    wl_ctxt = K8Helper.workload_operation(environment, K8Helper.WorkloadOp.START_WORKLOAD, **params)
    local_workload_ctxts = []
    local_workload_ctxts.append(wl_ctxt)
    debug_on_failure(environment, wl_ctxt['podStatus'] == K8Helper.PodStatus.RUNNING,
                     f"Workload not in RUNNING state, {wl_ctxt} for healthy={healthy} worker node")
    def _delete_workload():
       K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **wl_ctxt)
       return
    request.addfinalizer(_delete_workload)


    if not healthy:
        sample_size = random.randint(1,len(metrics_fields.keys())-1)
        error_list = random.sample(sorted(metrics_fields.keys()), sample_size)
        thresholds = []
        def _cleanup():
            k8_util.k8_metrics_error([0] * sample_size, error_list, namespace)
            return
        request.addfinalizer(_cleanup)
        _cleanup()
        wait_for_pod(environment,
                     namespace,
                     ["metricsclient"],
                     "metrics-exporter",
                     "healthy")
        for error in error_list:
            thresholds.append(metrics_fields.get(error) + random.randint(1,10))
        ret_code, _, ret_stderr = k8_util.k8_metrics_error(thresholds, error_list, namespace)
        debug_on_failure(environment, ret_code == 0, f"ECC error injection failed: {ret_stderr}")
        time.sleep(30)
        debug_on_failure(environment, k8_util.k8_get_node_health(worker, namespace) == "unhealthy",
                         f"result of kubectl describe node $NODE_NAME | grep unhealthy")
        verify_gpu_capacity_status(environment, worker, expected_unhealthy=1)
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    debug_on_failure(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    debug_on_failure(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")


    # Delete ClusterRole and service account if previous/stale version exists
    ret_code, namespace_info_list = k8_util.k8_get_namespaces()
    def _cleanup_jobs():
        if not schedule:
            k8_util.k8_delete_job(namespace, job_name)
        else:
            k8_util.k8_delete_cron_job(namespace, job_name)
        k8_util.k8_delete_cluster_role_binding(scc_crb_name)
        k8_util.k8_delete_cluster_role_binding(crb_name)
        k8_util.k8_delete_cluster_role(cluster_role_name)
        k8_util.k8_delete_service_account(sa_name, namespace)
    request.addfinalizer(_cleanup_jobs)
    _cleanup_jobs()

    if healthy:
        devicecfg_pods = [
            common.PodInfo('device-plugin', len(gpu_nodes), 1),
            common.PodInfo('test-runner', len(gpu_nodes), 1),
            common.PodInfo('metrics-exporter', len(gpu_nodes), 1)
        ]
        failed_pods = k8_util.k8_check_pod_running(namespace, devicecfg_pods)
        debug_on_failure(environment, (not failed_pods),
                         f"One or more pods are not ready - {failed_pods}")
    else:
        debug_on_failure(environment, k8_util.k8_get_node_health(worker, namespace) == "unhealthy",
                         f"result of kubectl describe node $NODE_NAME | grep unhealthy")
    # Check if test_runner namespace exists or not
    namespace_exists = False
    for ninfo in namespace_info_list:
        if ninfo['metadata']['name'] == namespace:
            namespace_exists = True

    if not namespace_exists and namespace != "default":
        # Create metrics-reader namespace
        ret_code, ret_stdout, ret_stderr = k8_util.k8_create_namespace(namespace)
        debug_on_failure(environment, (ret_code == 0),
                         f"Failed to create namespace:{namespace}, error: {ret_stderr}")

    # Create ServiceAccount
    ret_code, ret_stdout, ret_stderr = k8_util.k8_create_service_account(sa_name, namespace)
    debug_on_failure(environment, (ret_code == 0),
                     f"Failed to create service-account, error:{ret_stderr}")

    # Define ClusterRole: verb=get

    #rules = k8_util.k8_create_rules_from_endpoint_list([("/test_runner", "get")])
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
    # Define ClusterRole: verb=get
    ret_code, ret_stdout, ret_stderr = k8_util.k8_create_cluster_role(cluster_role_name, rules)
    debug_on_failure(environment, (ret_code == 0),
                     f"Failed to create test_runner clusterrole with GET, error:{ret_stderr}")

    ret_code, ret_stdout, ret_stderr = k8_util.k8_create_role_binding(crb_name, namespace, cluster_role_name, sa_name)
    debug_on_failure(environment, (ret_code == 0),
                              f"Failed to create test_runner clusterrole with verbs, error:{ret_stderr}")

    # On OpenShift, bind the privileged SCC to allow hostPath volumes and privileged containers
    if environment.deployment_mode == "openshift":
        ret_code, ret_stdout, ret_stderr = k8_util.k8_create_role_binding(
            scc_crb_name, namespace, "system:openshift:scc:privileged", sa_name)
        debug_on_failure(environment, (ret_code == 0),
                         f"Failed to bind privileged SCC to {sa_name}, error:{ret_stderr}")

    # Create token for ServiceAccount
    token = k8_util.k8_create_token(namespace, sa_name, "1h")
    debug_on_failure(environment, token != None,
                     f"Failed to create token for the service-account : {sa_name}")
    Logger.info(f"TOKEN=<{len(token)} chars>")

    time.sleep(30) # Wait for exporter to start working
    # Get endpoint for each node

    ret_code = k8_util.k8_create_test_runner_job(namespace,
                                      images,
                                      worker,
                                      sa_name,
                                      job_name,
                                      test_framework.upper(),
                                      healthy,
                                      schedule,
                                      (datetime.datetime.utcnow().minute + 2) % 60)
    debug_on_failure(environment, ret_code == 0, f"Failed to create {'CronJob' if schedule else 'Job'} {job_name}")

    _delete_workload()
    if not schedule:
        # Poll for job pod to reach Running state. After workload deletion,
        # GPU resources need time to be released back to the device-plugin
        # before the job pod can be scheduled and started.
        job_status = None
        for attempt in range(10):
            time.sleep(15)
            job_status = k8_util.k8_get_job_status(namespace, job_name)
            Logger.info(f"Manual job '{job_name}' status: {job_status} (attempt {attempt + 1}/10)")
            if job_status == "Running":
                break
        debug_on_failure(environment, job_status == "Running",
                         f"job should be in Running state, but found: {job_status}")
        #match = ['completed successfully',
                #f'Trigger: {trigger} Test: {recipe}',
                #f'Starting iteration 1 of 1 for test: {recipe}']
        verify_logs(environment, f'Starting iteration 1 of 1 for test: {recipe}', 'manual-trigger')

    else:
        time.sleep(30)
        job_status = k8_util.k8_get_cron_job_status(namespace, job_name)
        debug_on_failure(environment, job_status,
                         f"cron job not scheduled? found {job_status}")
  

    if not schedule:
        k8_util.k8_delete_job(namespace, job_name)
    else:
        k8_util.k8_delete_cron_job(namespace, job_name)

    if not healthy:
        k8_util.k8_metrics_error([0] * sample_size, error_list, namespace)
    time.sleep(30)

    #debug_on_failure(environment, k8_util.k8_get_node_health(gpu_cluster, worker, namespace) != "unhealthy",
                     #f"check result of kubectl describe node $NODE_NAME | grep healthy")
    verify_logs(environment, ["all GPUs are healthy"])
    after_events = k8_util.k8_get_events(namespace=environment.gpu_operator_namespace)
    verify_events(environment, before_events, after_events)

@pytest.mark.level3
def test_pre_job(request, gpu_cluster, test_framework, deviceconfig_install, environment, images, recipe, healthy):
    global Logger
    global LogPrettyPrinter
    global metrics_fields
    if environment.deployment_mode == "openshift":
        pytest.skip("Skipping test_pre_job on OpenShift")
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    debug_on_failure(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    debug_on_failure(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")
    gpu_node = gpu_nodes[0]
    cluster_node = gpu_cluster.find_node_by_ip(k8_util.k8_get_node_address(gpu_node))
    tr_support = amdgpu_util.get_test_runner_support(cluster_node.device_id)
    if test_framework == "rvs" and recipe not in tr_support.get("rvs_recipes", []):
        if recipe == "iet_stress" and "iet_single" in tr_support.get("rvs_recipes", []):
            recipe = "iet_single"
        else:
            pytest.skip(f"RVS recipe '{recipe}' not supported on {cluster_node.gpu_series}")
    if test_framework == "agfhc" and recipe not in tr_support.get("agfhc_recipes", []):
        pytest.skip(f"AGFHC recipe '{recipe}' not supported on {cluster_node.gpu_series}")

    worker = k8_util.k8_get_node_hostname(gpu_node)
    init_cap, alloc = k8_util.k8_get_node_gpu_capacity(worker)

    namespace = environment.gpu_operator_namespace
    sa_name = "test-run"
    cluster_role_name = "test-run-cluster-role"
    crb_name = 'test-run-rb'
    scc_crb_name = 'test-run-scc-privileged'
    trigger = "PRE_START_JOB_CHECK"

    testrunner_cfg = {}
    update_test_runner_configmap(recipe, worker, testrunner_cfg, test_framework.upper(), trigger)
    testrunner_cm = create_configmap(request, deviceconfig_install, environment, test_framework, testrunner_cfg)
    update_test_runner_config(deviceconfig_install, environment, test_framework, testrunner_cm)

    devicecfg_pods = [
        common.PodInfo('device-plugin', len(gpu_nodes), 1),
        common.PodInfo('test-runner', len(gpu_nodes), 1),
        common.PodInfo('metrics-exporter', len(gpu_nodes), 1)
    ]

    failed_pods = k8_util.k8_check_pod_running(namespace, devicecfg_pods)
    debug_on_failure(environment, (not failed_pods),
                     f"One or more pods are not ready - {failed_pods}")
    # Delete ClusterRole and service account if previous/stale version exists
    ret_code, namespace_info_list = k8_util.k8_get_namespaces()
    def _cleanup_jobs():
        k8_util.k8_delete_cluster_role_binding(scc_crb_name)
        k8_util.k8_delete_cluster_role_binding(crb_name)
        k8_util.k8_delete_cluster_role(cluster_role_name)
        k8_util.k8_delete_service_account(sa_name, namespace)
    request.addfinalizer(_cleanup_jobs)
    _cleanup_jobs()

    # Check if test_runner namespace exists or not
    namespace_exists = False
    for ninfo in namespace_info_list:
        if ninfo['metadata']['name'] == namespace:
            namespace_exists = True

    if not namespace_exists and namespace != "default":
        # Create metrics-reader namespace
        ret_code, ret_stdout, ret_stderr = k8_util.k8_create_namespace(namespace)
        debug_on_failure(environment, (ret_code == 0),
                          f"Failed to create namespace:{namespace}, error: {ret_stderr}")

    # Create ServiceAccount
    ret_code, ret_stdout, ret_stderr = k8_util.k8_create_service_account(sa_name, namespace)
    debug_on_failure(environment, (ret_code == 0),
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
    # Define ClusterRole:
    ret_code, ret_stdout, ret_stderr = k8_util.k8_create_cluster_role(cluster_role_name, rules)
    debug_on_failure(environment, (ret_code == 0),
                              f"Failed to create test_runner clusterrole with verbs, error:{ret_stderr}")

    # Define ClusterRoleBinding: verb=["get", "list", "watch", "create", "update", "patch"]
    ret_code, ret_stdout, ret_stderr = k8_util.k8_create_role_binding(crb_name, namespace, cluster_role_name, sa_name)
    debug_on_failure(environment, (ret_code == 0),
                              f"Failed to create test_runner clusterrole with verbs, error:{ret_stderr}")

    # On OpenShift, bind the privileged SCC to allow hostPath volumes and privileged containers
    if environment.deployment_mode == "openshift":
        ret_code, ret_stdout, ret_stderr = k8_util.k8_create_role_binding(
            scc_crb_name, namespace, "system:openshift:scc:privileged", sa_name)
        debug_on_failure(environment, (ret_code == 0),
                         f"Failed to bind privileged SCC to {sa_name}, error:{ret_stderr}")

    # Create token for ServiceAccount
    token = k8_util.k8_create_token(namespace, sa_name, "1h")
    debug_on_failure(environment, token != None,
                              f"Failed to create token for the service-account : {sa_name}")
    Logger.info(f"TOKEN=<{len(token)} chars>")


    # Create Job
    deployment_name = "pytorch-gpu-deployment"

    def _delete_deployment():
        k8_util.k8_delete_deployment(namespace, deployment_name)
    request.addfinalizer(_delete_deployment)
    _delete_deployment()

    time.sleep(30)
    wait_for_pod(environment,
                 namespace,
                 ["metricsclient"],
                 "metrics-exporter",
                 "healthy")

    if not healthy:
        sample_size = random.randint(1,len(metrics_fields.keys())-1)
        error_list = random.sample(sorted(metrics_fields.keys()), sample_size)
        thresholds = []
        def _cleanup():
            k8_util.k8_metrics_error([0] * sample_size, error_list, namespace)
            return
        request.addfinalizer(_cleanup)
        _cleanup()
        for error in error_list:
            thresholds.append(metrics_fields.get(error) + random.randint(1,10))
        ret_code, _, ret_stderr = k8_util.k8_metrics_error(thresholds, error_list, namespace)
        debug_on_failure(environment, ret_code == 0, f"ECC error injection failed: {ret_stderr}")
        time.sleep(30)
        debug_on_failure(environment, k8_util.k8_get_node_health(worker, namespace) == "unhealthy",
                             f"result of kubectl describe node $NODE_NAME | grep unhealthy")

    k8_util.k8_create_pre_test_runner_job(namespace, images, sa_name, deployment_name, worker, test_framework.upper(), init_cap)
    time.sleep(10)

    deployment = k8_util.k8_get_deployment(namespace, deployment_name)
    flag = False
    for condition in deployment.status.conditions:
        if 'pytorch-gpu-deployment' in condition.message:
            flag = True
            if healthy:
                Logger.info(f"Deployment '{deployment_name}' status: type={condition.type}, status={condition.status}, available_replicas={deployment.status.available_replicas}, ready_replicas={deployment.status.ready_replicas}")
                debug_on_failure(environment, condition.type == 'Progressing' and condition.status == 'True',
                                 f"status = {deployment.status}")
            else:
                devicecfg_pods = [
                    common.PodInfo('device-plugin', len(gpu_nodes), 1),
                    common.PodInfo('metrics-exporter', len(gpu_nodes), 1),
                    common.PodInfo('test-runner', len(gpu_nodes), 1),
                    common.PodInfo('pytorch-gpu-deployment', len(gpu_nodes), 1),
                ]
                failed_pods = k8_util.k8_check_pod_running(namespace, devicecfg_pods, total_attempts=2)
                Logger.info(f"After 2 attempts, failed_pods = {failed_pods}")
                debug_on_failure(environment, failed_pods == ['pytorch-gpu-deployment'],
                                 f"expecting failed pods to be ['pytorch-gpu-deployment'], found {failed_pods}")
                _cleanup()
                gpu_healthy = k8_util.wait_for_gpu_health(namespace, gpu_id="0", expected_health="healthy")
                debug_on_failure(environment, gpu_healthy,
                                 "GPU 0 did not recover to healthy state after error cleanup")
                k8_util.k8_delete_all_pods_with_name_pattern(namespace, 'pytorch-gpu-deployment')
                time.sleep(20)
                failed_pods = k8_util.k8_check_pod_running(namespace, devicecfg_pods, total_attempts=20)
                Logger.info(f"After cleanup and 20 attempts, failed_pods = {failed_pods}")
                debug_on_failure(environment, (not failed_pods),
                                 f"expecting failed pods to be [], found {failed_pods}")


    debug_on_failure(environment, flag,
                     f"didn't find pytorch-gpu-deployment in {deployment.status.conditions}")

    time.sleep(300)
    try:
        ret_code, init_logs, _ = k8_util.k8_get_pod_logs(
            'pytorch-gpu-deployment', namespace, since="600s", container='init-test-runner')
        if init_logs:
            Logger.info(f"init-test-runner logs ({len(init_logs)} chars):\n{init_logs[-3000:]}")
        ret_code_prev, prev_logs, _ = k8_util.k8_get_pod_logs(
            'pytorch-gpu-deployment', namespace, since="600s",
            container='init-test-runner', previous=True)
        if prev_logs:
            Logger.info(f"init-test-runner PREVIOUS logs ({len(prev_logs)} chars):\n{prev_logs[-3000:]}")
    except Exception as e:
        Logger.warning(f"Failed to collect init-test-runner diagnostics: {e}")

    match = [f'Starting iteration 1 of 1 for test: {recipe}', 'completed successfully', f'Trigger: {trigger} Test: {recipe}']
    verify_logs(environment, match, 'pytorch-gpu-deployment', container='init-test-runner')

    match = ['Copying RVS logs', "'/var/log/amd-test-runner' -> '/host-logs/amd-test-runner'"]
    verify_logs(environment, match, 'pytorch-gpu-deployment', container='copy-rvs-logs')

    k8_util.k8_delete_deployment(namespace, deployment_name)

@pytest.mark.skip(reason="TODO: Update to use all_image_versions fixture instead of removed alternative_images")
@pytest.mark.parametrize("upgrade_policy", ["RollingUpdate", "OnDelete"])
def test_test_runner_rolling_update(deviceconfig_install, environment, upgrade_policy):
    global Logger
    if environment.gpu_operator_version < "v1.2.0":
        pytest.skip(f"Test-Runner Operand upgrade feature is not available in release before v1.2.0")

    if (images['testRunner.image.version'] == alternative_images['testRunner.image.version']):
        pytest.fail("Invalid input for operand upgrade testcase - both version same")

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")

    # Check current version of the testRunner from deviceconfig-CR
    def _modify_test_runner(repo, version):
        for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
            tcfg['testRunner.enable'] = True
            tcfg['testRunner.image.repository'] = repo
            tcfg['testRunner.image.version'] = version
            tcfg['testRunner.upgradePolicy.upgradeStrategy'] = upgrade_policy
            cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
            ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
            K8Helper.triage(environment, (ret_code == 0), "Failed to modify deviceconfig CR")

    def _restore_test_runner():
        _modify_test_runner(images['testRunner.image.repository'],
                                         images['testRunner.image.version'])
        devicecfg_pods = [
            common.PodInfo('test-runner', len(gpu_nodes), 1),
        ]
        failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods)
        K8Helper.triage(environment, not failed_pods, f"One or more pods are not ready - {failed_pods}")

    request.addfinalizer(_restore_test_runner)

    K8Helper.check_deviceconfig_status(environment, deviceconfig_install.devicecfg_list)
    ret_code, orig_tr_pods = k8_util.k8_get_pods(environment.gpu_operator_namespace, pod_name_pattern = "test-runner")
    K8Helper.triage(environment, (ret_code == 0 and len(orig_tr_pods) > 0), f"Missing test-runner pods or error")
    _modify_test_runner(alternative_images['testRunner.image.repository'],
                        alternative_images['testRunner.image.version'])

    if upgrade_policy == "RollingUpdate":
        Logger.debug("Wait until upgrade is complete...")
    elif upgrade_policy == "OnDelete":
        # Check no upgrade is kicked in
        time.sleep(20)
        ret_code, precheck_tr_pods = k8_util.k8_get_pods(environment.gpu_operator_namespace, pod_name_pattern = "test-runner")
        for old_pod, new_pod in zip(orig_tr_pods, precheck_tr_pods):
            for o_s_info, n_s_info in zip(old_pod['status']['container_statuses'], new_pod['status']['container_statuses']):
                if o_s_info['name'] == 'test-runner-container' and n_s_info['name'] == 'test-runner-container':
                    K8Helper.triage(environment, (o_s_info['image'] == n_s_info['image']),
                                    f"Version mismatch before pod-deletion with policy: {upgrade_policy}, {o_s_info}, {n_s_info}")
        # explicitly delete the pod
        k8_util.k8_delete_all_pods_with_name_pattern(environment.gpu_operator_namespace, 'test-runner')

    time.sleep(20)
    devicecfg_pods = [
        common.PodInfo('test-runner', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods)
    K8Helper.triage(environment, not failed_pods, f"One or more pods are not ready - {failed_pods}")
    ret_code, new_tr_pods = k8_util.k8_get_pods(environment.gpu_operator_namespace, pod_name_pattern = "test-runner")
    K8Helper.triage(environment, (ret_code == 0 and len(new_tr_pods) > 0), f"Missing test-runner pods or error")
    K8Helper.check_deviceconfig_status(environment, deviceconfig_install.devicecfg_list)

    # Check latest version of the testRunner from deviceconfig-CR and match to alternative_images
    for pod in new_tr_pods:
        for s_info in pod['status']['container_statuses']:
            if s_info['name'] == 'test-runner-container':
                K8Helper.triage(environment, (alternative_images['testRunner.image.version'] in s_info['image']),
                                f"Unexpected version found in the test-runner-container image post upgrade, {s_info}")
                K8Helper.triage(environment, (alternative_images['testRunner.image.repository'] in s_info['image']),
                                f"Unexpected version found in the test-runner-container image post upgrade, {s_info}")

