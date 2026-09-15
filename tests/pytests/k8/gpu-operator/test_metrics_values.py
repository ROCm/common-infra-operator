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
import random
import threading
from collections import defaultdict
import lib.common as common
import lib.helm_util as helm_util
import lib.k8_util as k8_util
import lib.spec_util as spec_util
import lib.metric_util as metric_util
import lib.amdgpu as amdgpu_util
from lib.util import K8Helper

#pytestmark = pytest.mark.skip("debugging")
Logger = logging.getLogger("k8.test_metrics_values")
LogPrettyPrinter = pprint.PrettyPrinter(indent = 2)

@pytest.fixture(scope="module")
def deviceconfig_install(gpu_cluster, images, gpu_operator_install, environment):
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

    # Enable profile-metrics if GPU supports it
    gpu_node = gpu_nodes[0]
    cluster_node = gpu_cluster.find_node_by_ip(k8_util.k8_get_node_address(gpu_node))
    enable_profiler = amdgpu_util.get_gpu_features(cluster_node.device_id).get("profiler_metrics", True) if cluster_node else True
    Logger.info(f"Profiler metrics: {'enabled' if enable_profiler else 'disabled'} for {cluster_node.gpu_series if cluster_node else 'unknown'}")

    config_map_name = "prof-metrics-cfgmap"
    config_map = {
        "CommonConfig" : {
            "HealthService" : {
                "Enable" : False,
            },
        },
    }
    if enable_profiler:
        config_map["GPUConfig"] = {
            "ProfilerMetrics": {
                "all": True,
            }
        }
    configmap_file = os.path.join(environment.logdir, f"{config_map_name}.json")
    with open(configmap_file, "w") as fp:
        fp.write(json.dumps(config_map, indent=4))

    configmap_file = os.path.join(environment.logdir, f"config.json")
    with open(configmap_file, "w") as fp:
        fp.write(json.dumps(config_map, indent=4))

    # Delete if there is any previous instance with same name
    ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_configmap(environment.gpu_operator_namespace, config_map_name)
    Logger.debug(f"Configmap cleanup: ret_code:{ret_code}")
    # ignore ret_code
    ret_code, ret_stdout, ret_stderr = k8_util.k8_create_configmap(environment.gpu_operator_namespace,
                                                                   config_map_name, configmap_file, "config.json")
    test_config = {
            'metadata.namespace' : environment.gpu_operator_namespace,
            'driver.enable' : True,
            'devicePlugin.enableNodeLabeller' : False,
            'metricsExporter.enable' : True,
            'metricsExporter.serviceType' : 'ClusterIP',
            'metricsExporter.port' : 5000,
            'metricsExporter.rbacConfig.enable' : False,
            'metricsExporter.rbacConfig.disableHttps' : False,
            'metricsExporter.config' : config_map_name,
        }
    test_config.update(images)

    test_cfg_map = spec_util.build_deviceconfig_cr_template(test_config, gpu_nodes, 'exporter', environment.amdgpu_driver_spec)
    exporter_port_map = {}
    devicecfg_list = []

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

    devicecfg_pods = [
        common.PodInfo('metrics-exporter', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods)
    if not failed_pods:
        K8Helper.capture_rocm_version(
            environment.gpu_operator_namespace,
            "metrics-exporter",
            [
                ("metricsExporter.image", "metrics-exporter-container"),
            ],
        )

    devcfg_info = DeviceConfigCRInfo()
    setattr(devcfg_info, "test_cfg_map", test_cfg_map)
    setattr(devcfg_info, "exporter_port_map", exporter_port_map)
    setattr(devcfg_info, "devicecfg_list", devicecfg_list)
    yield devcfg_info

    device_cfg_info = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace, None)
    for devcfg_name, _ in device_cfg_info.items():
        k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)
    return

@pytest.fixture(scope="module")
def metrics_samples(gpu_cluster, images, deviceconfig_install, environment):
    global Logger
    Logger.info(f"Collecting metrics-exporter curl output, amd-smi metrics and gpuctl metrics snapshot")
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")
    K8Helper.delete_debug_pods(["default", environment.gpu_operator_namespace])

    # Watch for all pod creation
    '''
    test-deviceconfig-device-plugin-8f7px                        1/1     Running       0                 12d
    test-deviceconfig-metrics-exporter-27gq9                     2/2     Running       0                 12d
    '''
    devicecfg_pods = [
        common.PodInfo('device-plugin', len(gpu_nodes), 1),
        common.PodInfo('metrics-exporter', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods)
    K8Helper.triage(environment, not failed_pods, f"One or more pods are not ready - {failed_pods}")

    time.sleep(30) # Wait for exporter to start working
    idle_metrics = metric_util.collect_metrics_samples(gpu_cluster, gpu_nodes,
                                                       deviceconfig_install.exporter_port_map,
                                                       environment, ctxt_name = "idle")

    # Deploy one workload pod per GPU so every core is loaded (concurrent per node)
    # TODO: temporarily capped to 1 workload per node to avoid OOM on Radeon GPUs
    local_workload_ctxts = []
    loaded_gpu_count = {}
    for node in gpu_nodes:
        node_ip = k8_util.k8_get_node_address(node)
        cluster_node = gpu_cluster.find_node_by_ip(node_ip)
        if not cluster_node:
            pytest.fail(f"Unable to get worker node from cluster for ip: {node_ip}")
        node_name = k8_util.k8_get_node_hostname(node)
        gpu_cap, gpu_alloc = k8_util.k8_get_node_gpu_capacity(node_name)

        workloads_per_node = min(1, gpu_cap)  # TODO: restore to gpu_cap when memory issue is resolved
        loaded_gpu_count[node_name] = workloads_per_node
        node_ctxts = [None] * workloads_per_node
        node_errors = [None] * workloads_per_node

        def _start_workload(idx, n_name, imgs, env, ctxts, errors):
            try:
                params = {
                    "node_name" : n_name,
                    "images" : imgs,
                    "num_gpu_reqd" : 1,
                    "workload_selection" : "rocm-pytorch-gemm-stress",
                }
                ctxts[idx] = K8Helper.workload_operation(env, K8Helper.WorkloadOp.START_WORKLOAD, **params)
            except Exception as e:
                errors[idx] = str(e)

        threads = []
        for gpu_idx in range(workloads_per_node):
            t = threading.Thread(target=_start_workload,
                                 args=(gpu_idx, node_name, images, environment, node_ctxts, node_errors))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        for gpu_idx in range(workloads_per_node):
            if node_errors[gpu_idx]:
                K8Helper.triage(environment, False,
                                f"Job: workload thread failed on {node_name} gpu {gpu_idx}: {node_errors[gpu_idx]}")
            K8Helper.triage(environment, (node_ctxts[gpu_idx] is not None and
                            node_ctxts[gpu_idx]['podStatus'] == K8Helper.PodStatus.RUNNING),
                            f"Job: workload failed to start on {node_name} gpu {gpu_idx}: {node_ctxts[gpu_idx]}")
            local_workload_ctxts.append(node_ctxts[gpu_idx])

    # Allow workload to reach steady-state before sampling
    time.sleep(10)

    # Collect new sample of metrics
    workload_metrics = metric_util.collect_metrics_samples(gpu_cluster, gpu_nodes,
                                                           deviceconfig_install.exporter_port_map,
                                                           environment, ctxt_name = "load")

    # Stop all the workloads
    for ctxt in local_workload_ctxts:
        K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **ctxt)

    # Collect a single tech-support snapshot while cluster state is still relevant.
    # Comparison tests (parametrized by metric) skip per-failure tech-support since
    # they only compare pre-collected data; this baseline is the one to reference.
    K8Helper.collect_tech_support(environment, label="metrics_samples")

    yield (idle_metrics, workload_metrics, loaded_gpu_count)
    return

# Generate testcases for each metrics supported for value 
def pytest_generate_tests(metafunc):
    global Logger
    if 'metric_to_test' in metafunc.fixturenames:
        metrics_to_test = []
        for entry in metric_util.get_supported_metrics(skip_profiler_metrics=True):
            if entry.get('skip-validation', 'no') == 'yes':
                continue
            metrics_to_test.append(entry['name'])
        metafunc.parametrize('metric_to_test', metrics_to_test)
    if 'prof_metric_to_test' in metafunc.fixturenames:
        metrics_to_test = []
        for entry in metric_util.get_supported_metrics(skip_profiler_metrics=False):
            if '_PROF_' not in entry['name']:
                continue
            if entry.get('skip-validation', 'no') == 'yes':
                continue
            metrics_to_test.append(entry['name'])
        metafunc.parametrize('prof_metric_to_test', metrics_to_test)


def test_exporter_all_supported_metrics(gpu_cluster, metrics_samples, images, environment):
    """
    Ensure the exporter endpoint returns all supported metrics for the designated GPU series via curl.
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
                                    f"Label {label_name} missing in exported metrics {entry}, {metric_metadata}",
                                    skip_techsupport=True)
                    lval = entry['labels'][label_name]
                    if lval.lower() != label_value.lower():
                        continue
                    m_info_list.append(entry)

                Logger.debug(f"Found total {len(m_info_list)} exported metrics for {metric_to_test} gpu-id:{gpu_id}")
                if len(m_info_list) > 0:
                    Logger.info(f"Found {len(m_info_list)} entries of {metric_to_test} gpu-id:{gpu_id}")
                    return True
        elif 'array' in metric_types.keys():
            label_name = metric_types['array']['label']
            m_info_list = []
            for _, entry in enumerate(exporter_metrics[metric_to_test.lower()]):
                if entry['labels']['gpu_id'] != str(gpu_id):
                    continue
                K8Helper.triage(environment, (label_name in entry['labels']),
                                f"Label {label_name} missing in exported metrics {entry}, {metric_metadata}",
                                skip_techsupport=True)
                m_info_list.append(entry)
            Logger.debug(f"Found total {len(m_info_list)} exported metrics for {metric_to_test} gpu-id:{gpu_id}")
            if len(m_info_list) > 0:
                Logger.info(f"Found {len(m_info_list)} entries of {metric_to_test} gpu-id:{gpu_id}")
                return True
        else:
            if metric_to_test.lower() in exporter_metrics:
                Logger.info(f"Found {metric_to_test} gpu-id:{gpu_id}")
                return True

        if metric_types.get("contingent", "no") == "yes":
            Logger.warning(f"Missing {metric_to_test} gpu-id:{gpu_id} - contingent")
            return True

        Logger.error(f"Missing {metric_to_test}, gpu-id:{gpu_id} types: {metric_types}")
        return False

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")
    failed_metrics = defaultdict(set)
    all_idle_metrics, all_workload_metrics, loaded_gpu_count = metrics_samples
    for node in gpu_nodes:
        node_ip = k8_util.k8_get_node_address(node)
        cluster_node = gpu_cluster.find_node_by_ip(node_ip)
        if not cluster_node:
            pytest.fail(f"Unable to get worker node from cluster for ip: {node_ip}")
        node_name = k8_util.k8_get_node_hostname(node)
        K8Helper.triage(environment, (cluster_node.num_gpus > 0), f"Node {node_name} has no GPUs present")
        gpu_capacity, _ = k8_util.k8_get_node_gpu_capacity(node_name)
        gpu_count = min(cluster_node.num_gpus, gpu_capacity) if gpu_capacity > 0 else cluster_node.num_gpus

        # Pick up first sample of exporter metrics for given node
        idle_metrics = metric_util.parse_metric_data(all_idle_metrics[node_name]['exporter'][0])
        workload_metrics = metric_util.parse_metric_data(all_workload_metrics[node_name]['exporter'][0])

        enable_profiler = amdgpu_util.get_gpu_features(cluster_node.device_id).get("profiler_metrics", True) if cluster_node else True
        supported_metrics = metric_util.get_supported_metrics(gpu_series = cluster_node.gpu_series,
                                                              skip_profiler_metrics = not enable_profiler,
                                                              amdgpu_driver = cluster_node.amdgpu_driver_version,
                                                              dme_version = images['metricsExporter.image.version'],
                                                              num_gpus = cluster_node.num_gpus)
        Logger.info(f"Node: {node_name} having {cluster_node.gpu_series} has {len(supported_metrics)} metrics, gpu_count={gpu_count}, num_gpus={cluster_node.num_gpus}, profiler={'on' if enable_profiler else 'off'}")
        for entry in supported_metrics:
            metric_to_test = entry['name']
            Logger.info(f"Checking {metric_to_test} among exported metrics for node {node_name}")
            for gpu_id in range(gpu_count):
                if _test_if_metrics_exported(metric_to_test, gpu_id, idle_metrics) == False:
                    Logger.error(f"Idle Conditions Metrics: {metric_to_test} failed for {gpu_id}")
                    failed_metrics[metric_to_test].add(gpu_id)
                if _test_if_metrics_exported(metric_to_test, gpu_id, workload_metrics) == False:
                    Logger.error(f"Load Conditions Metrics: {metric_to_test} failed for {gpu_id}")
                    failed_metrics[metric_to_test].add(gpu_id)
    K8Helper.triage(environment, (len(failed_metrics) == 0),
                    f"Metics validation failed: {failed_metrics.keys()} from exported-metrics\n{LogPrettyPrinter.pformat(failed_metrics)}",
                    skip_techsupport=True)


def test_exporter_metrics_completeness(gpu_cluster, images, metrics_samples, metric_to_test, environment):
    """
    Verify that each metric is present in the exporter output for every GPU across all
    collected samples (idle and load). Catches transient metric dropouts that cause
    spurious failures in value-accuracy tests.
    Hard-fails if the metric is absent for a GPU across all samples.
    Xfails if more than 1 sample is missing (transient dropout threshold).
    """
    global Logger

    def _is_metric_present(metric_to_test, metric_types, gpu_id, exporter_metrics):
        if 'labeled' in metric_types:
            if ':' not in metric_to_test:
                return True
            label_name = metric_types['labeled']['label']
            metric_name, label_value = metric_to_test.split(":", 1)
            key = metric_name.lower()
            if key not in exporter_metrics:
                return False
            for entry in exporter_metrics[key]:
                if entry['labels'].get('gpu_id') != str(gpu_id):
                    continue
                if entry['labels'].get(label_name, '').lower() == label_value.lower():
                    return True
            return False
        elif 'array' in metric_types:
            key = metric_to_test.lower()
            if key not in exporter_metrics:
                return False
            return any(e['labels'].get('gpu_id') == str(gpu_id) for e in exporter_metrics[key])
        else:
            key = metric_to_test.lower()
            if key not in exporter_metrics:
                return False
            return any(e['labels'].get('gpu_id') == str(gpu_id) for e in exporter_metrics[key])

    metric_validated = False
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")
    all_idle_metrics, all_workload_metrics, loaded_gpu_count = metrics_samples

    for node in gpu_nodes:
        node_ip = k8_util.k8_get_node_address(node)
        cluster_node = gpu_cluster.find_node_by_ip(node_ip)
        if not cluster_node:
            pytest.fail(f"Unable to get worker node from cluster for ip: {node_ip}")
        node_name = k8_util.k8_get_node_hostname(node)

        if not metric_util.is_metric_supported(metric_to_test, cluster_node.gpu_series,
                                               cluster_node.amdgpu_driver_version,
                                               images["metricsExporter.image.version"],
                                               num_gpus=cluster_node.num_gpus):
            continue
        metric_validated = True

        metric_metadata = metric_util.get_metric_metadata(metric_to_test)
        metric_types = metric_metadata.get("type", {})

        idle_metrics = all_idle_metrics[node_name]
        workload_metrics = all_workload_metrics[node_name]
        gpu_capacity, _ = k8_util.k8_get_node_gpu_capacity(node_name)
        gpu_count = min(cluster_node.num_gpus, gpu_capacity) if gpu_capacity > 0 else cluster_node.num_gpus
        num_samples = idle_metrics['num-samples']
        total_samples = num_samples * 2  # idle + load

        # Scan all GPUs and all samples before making any verdict.
        # Track two dimensions:
        #   gpu_missing[gpu_id]              → samples where metric was absent for that GPU
        #   sample_absent_gpus[cond][sample] → GPUs where metric was absent in that sample
        gpu_missing = {}
        sample_absent_gpus = {'idle': defaultdict(list), 'load': defaultdict(list)}

        for gpu_id in range(gpu_count):
            missing = []
            for sample_id in range(num_samples):
                idle_exporter = metric_util.parse_metric_data(idle_metrics['exporter'][sample_id])
                if not _is_metric_present(metric_to_test, metric_types, gpu_id, idle_exporter):
                    missing.append(('idle', sample_id))
                    sample_absent_gpus['idle'][sample_id].append(gpu_id)
                    Logger.warning(f"Completeness: {metric_to_test} gpu:{gpu_id} absent in idle sample {sample_id}")

                load_exporter = metric_util.parse_metric_data(workload_metrics['exporter'][sample_id])
                if not _is_metric_present(metric_to_test, metric_types, gpu_id, load_exporter):
                    missing.append(('load', sample_id))
                    sample_absent_gpus['load'][sample_id].append(gpu_id)
                    Logger.warning(f"Completeness: {metric_to_test} gpu:{gpu_id} absent in load sample {sample_id}")

            gpu_missing[gpu_id] = missing
            present = total_samples - len(missing)
            Logger.info(f"Worker: {node_name} GPU: {gpu_id} — "
                        f"{metric_to_test} present in {present}/{total_samples} samples")

        # --- GPU dimension: classify by fraction of samples missing per GPU ---
        # hard-fail: ≥50% of total_samples missing for a GPU
        # xfail:     >1 but <50% missing (transient)
        hard_fail_gpus = [g for g in range(gpu_count)
                          if len(gpu_missing[g]) * 2 >= total_samples]
        transient_gpus  = [g for g in range(gpu_count)
                           if len(gpu_missing[g]) > 1 and len(gpu_missing[g]) * 2 < total_samples]

        # --- Sample dimension: classify by fraction of GPUs missing per sample ---
        # hard-fail: ≥50% of GPUs missing in a sample
        # xfail:     >1 GPU but <50% missing
        hard_fail_samples = {}
        transient_samples = {}
        for cond in ('idle', 'load'):
            for sid, absent in sample_absent_gpus[cond].items():
                if len(absent) * 2 >= gpu_count:
                    hard_fail_samples[(cond, sid)] = absent
                elif len(absent) > 1:
                    transient_samples[(cond, sid)] = absent

        any_hard = hard_fail_gpus or hard_fail_samples
        any_soft = transient_gpus or transient_samples
        if any_hard or any_soft:
            Logger.error(
                f"Completeness summary for {metric_to_test} on {node_name}:\n"
                f"  GPU dimension  — hard(≥50% samples missing): {hard_fail_gpus}, "
                f"transient(>1<50%): {transient_gpus}\n"
                f"  Sample dimension — hard(≥50% GPUs missing): {list(hard_fail_samples)}, "
                f"transient(>1<50%): {list(transient_samples)}"
            )

        # Hard fail — GPU dimension
        K8Helper.triage(environment, (len(hard_fail_gpus) == 0),
                        f"Completeness: {metric_to_test} missing ≥50% samples for "
                        f"{len(hard_fail_gpus)}/{gpu_count} GPUs: "
                        f"{[(g, len(gpu_missing[g]), total_samples) for g in hard_fail_gpus]}",
                        skip_techsupport=True)

        # Hard fail — sample dimension
        K8Helper.triage(environment, (len(hard_fail_samples) == 0),
                        f"Completeness: {metric_to_test} missing ≥50% GPUs in "
                        f"{len(hard_fail_samples)}/{total_samples} samples: "
                        f"{[(k, len(v), gpu_count) for k, v in hard_fail_samples.items()]}",
                        skip_techsupport=True)

        # Xfail — transient dropout on either dimension
        transient_desc = []
        if transient_gpus:
            transient_desc.append(f"GPUs {[(g, gpu_missing[g]) for g in transient_gpus]}")
        if transient_samples:
            transient_desc.append(f"samples {list(transient_samples)}")
        K8Helper.triage(environment, (not transient_desc),
                        f"Completeness: {metric_to_test} transient dropout — " + "; ".join(transient_desc),
                        skip_techsupport=True, expected_to_fail=True)

    if not metric_validated:
        pytest.skip(f"Metric {metric_to_test} cannot be validated in this setup — skip")


def test_exporter_metrics_value_accuracy(gpu_cluster, images, metrics_samples, metric_to_test, environment):
    """
    To verify that the metrics published by the AMD Device Metrics Exporter (DME)
    accurately reflect the real-time hardware state as reported by the amd-smi utility.
    """

    global Logger
    metric_metadata = metric_util.get_metric_metadata(metric_to_test)
    metric_types = metric_metadata.get("type", {})
    def _extract_amd_smi_value(amd_smi_metrics, path_to_metric):
        if len(path_to_metric) == 0:
            return None
        elif len(path_to_metric) == 1:
            return amd_smi_metrics.get(path_to_metric[0], None)
        else:
            return _extract_amd_smi_value(amd_smi_metrics.get(path_to_metric[0], {}), path_to_metric[1:])

    def _analyze_metrics_collection(metric_to_test, gpu_id, partition_id, metric_data):
        num_samples = metric_data['num-samples']
        Logger.info(f"Processing {metric_data['title']} - total samples {num_samples}")
        hit_count = 0
        miss_count = 0
        missing_count = 0  # samples where exporter had no entry for this metric+gpu
        all_amd_smi_metrics = metric_data['amd-smi']
        all_exporter_metrics = metric_data['exporter']
        pattern = r'("[^"]+")\s*:\s*"(\[.*?\])"'
        replacement = r'\1: \2'
        for sample_id in range(num_samples):
            # Extract exporter metrics for current sample_id
            exporter_metrics = metric_util.parse_metric_data(all_exporter_metrics[sample_id])

            # Extract amd-smi metrics for current sample_id
            new_sample = re.sub(pattern, replacement, all_amd_smi_metrics[sample_id].replace("'", "\""))
            amd_smi_metrics = json.loads(new_sample)
            gpu_support_info = metric_util.get_metric_support_info(metric_metadata, metric_data["gpu-series"])
            K8Helper.triage(environment, (gpu_support_info != None),
                            f"Missing gpu-support-info for {metric_to_test}, {metric_metadata}, {metric_data['gpu-series']}",
                            skip_techsupport=True)
            amd_smi_source = gpu_support_info.get('amd-smi', None)
            K8Helper.triage(environment, (amd_smi_source != None),
                            f"Missing amd-smi source information for {metric_to_test}, {gpu_support_info}",
                            skip_techsupport=True)

            if 'labeled' in metric_types.keys():
                if ':' not in metric_to_test:
                    pytest.fail(f"Invalid configuration - missing label-value for labeled metric {metric_to_test}")
                label_name = metric_types['labeled']['label']
                metric_name, label_value = metric_to_test.split(":")
                m_info_list = []
                for _, entry in enumerate(exporter_metrics[metric_name.lower()]):
                    if entry['labels']['gpu_id'] != str(gpu_id):
                        continue
                    K8Helper.triage(environment, (label_name in entry['labels']),
                                    f"Label {label_name} missing in exported metrics {entry}, {metric_metadata}",
                                    skip_techsupport=True)
                    lval = entry['labels'][label_name]
                    if lval.lower() == label_value.lower():
                        m_info_list.append(entry)

                Logger.debug(f"Found total {len(m_info_list)} exported metrics for {metric_to_test}")
                if len(m_info_list) == 0:
                    Logger.warning(f"Sample {sample_id}: {metric_to_test} absent from exporter for gpu:{gpu_id} — skipping sample")
                    missing_count += 1
                    continue
                idx = 0
                amd_smi_values = []
                while True:
                    path_to_metric = amd_smi_source.format(partition_id = partition_id, idx = idx).split(".")
                    if isinstance(amd_smi_metrics, list):
                        amd_smi_val = _extract_amd_smi_value(amd_smi_metrics[gpu_id], path_to_metric)
                    elif isinstance(amd_smi_metrics, dict) and 'gpu_data' in amd_smi_metrics.keys():
                        amd_smi_val = _extract_amd_smi_value(amd_smi_metrics['gpu_data'][gpu_id], path_to_metric)
                    if amd_smi_val != None:
                        amd_smi_values.append(amd_smi_val)
                    else:
                        break
                    idx = idx + 1
                Logger.debug(f"Found total {len(amd_smi_values)} from amd-smi output for {metric_to_test}")
                for idx, entry in enumerate(list(zip(m_info_list, amd_smi_values))):
                    metric_info, amd_smi_val = entry
                    if isinstance(amd_smi_val, dict):
                        if amd_smi_val["value"] == "N/A":
                            Logger.warn(f"No amd-smi metric information for idx {idx} {metric_to_test}, got {amd_smi_val}")
                            continue
                        lower_limit = int(0.95 * float(amd_smi_val["value"]))
                        upper_limit = int(1.05 * float(amd_smi_val["value"]))
                    elif isinstance(amd_smi_val, int):
                        lower_limit = int(0.95 * float(amd_smi_val))
                        upper_limit = int(1.05 * float(amd_smi_val))
                    elif isinstance(amd_smi_val, str) and amd_smi_val == "N/A":
                        Logger.warn(f"No amd-smi metric information for idx {idx} {metric_to_test}, got {amd_smi_val}")
                        continue
                    Logger.debug(f"{metric_to_test} Sample:{sample_id} AMD-SMI: {amd_smi_val}, exporter : {metric_info}")
                    if lower_limit == 0 and upper_limit == 0 and int(metric_info["value"]) == 0:
                        Logger.debug(f"{metric_to_test} Sample:{sample_id}: zero-vs-zero — inconclusive, skip")
                        continue
                    if lower_limit <= int(metric_info["value"]) <= upper_limit:
                        hit_count = hit_count + 1
                    else:
                        miss_count = miss_count + 1
            elif 'array' in metric_types.keys():
                label_name = metric_types['array']['label']
                m_info_list = []
                for _, entry in enumerate(exporter_metrics[metric_to_test.lower()]):
                    if entry['labels']['gpu_id'] != str(gpu_id):
                        continue
                    K8Helper.triage(environment, (label_name in entry['labels']),
                                    f"Label {label_name} missing in exported metrics {entry}, {metric_metadata}",
                                    skip_techsupport=True)
                    m_info_list.append(entry)
                Logger.debug(f"Found total {len(m_info_list)} exported metrics for {metric_to_test}")
                if len(m_info_list) == 0:
                    Logger.warning(f"Sample {sample_id}: {metric_to_test} absent from exporter for gpu:{gpu_id} — skipping sample")
                    missing_count += 1
                    continue
                path_to_metric = amd_smi_source.format(partition_id = partition_id).split(".")
                if isinstance(amd_smi_metrics, list):
                    amd_smi_value_list = _extract_amd_smi_value(amd_smi_metrics[gpu_id], path_to_metric)
                elif isinstance(amd_smi_metrics, dict) and 'gpu_data' in amd_smi_metrics.keys():
                    amd_smi_value_list = _extract_amd_smi_value(amd_smi_metrics['gpu_data'][gpu_id], path_to_metric)
                for amd_smi_val in amd_smi_value_list:
                    if amd_smi_val != None:
                        amd_smi_values.append(amd_smi_val)
                    else:
                        break
                Logger.debug(f"Found total {len(amd_smi_values)} from amd-smi output for {metric_to_test}")
                for idx, entry in enumerate(list(zip(m_info_list, amd_smi_values))):
                    metric_info, amd_smi_val = entry
                    if isinstance(amd_smi_val, dict):
                        if amd_smi_val["value"] == "N/A":
                            Logger.warn(f"No amd-smi metric information for idx {idx} {metric_to_test}, got {amd_smi_val}")
                            continue
                        lower_limit = int(0.95 * float(amd_smi_val["value"]))
                        upper_limit = int(1.05 * float(amd_smi_val["value"]))
                    elif isinstance(amd_smi_val, int):
                        lower_limit = int(0.95 * float(amd_smi_val))
                        upper_limit = int(1.05 * float(amd_smi_val))
                    elif isinstance(amd_smi_val, str) and amd_smi_val == "N/A":
                        Logger.warn(f"No amd-smi metric information for idx {idx} {metric_to_test}, got {amd_smi_val}")
                        continue
                    Logger.debug(f"{metric_to_test} Sample:{sample_id} AMD-SMI: {amd_smi_val}, exporter : {metric_info}")
                    if lower_limit == 0 and upper_limit == 0 and int(metric_info["value"]) == 0:
                        Logger.debug(f"{metric_to_test} Sample:{sample_id}: zero-vs-zero — inconclusive, skip")
                        continue
                    if lower_limit <= int(metric_info["value"]) <= upper_limit:
                        hit_count = hit_count + 1
                    else:
                        miss_count = miss_count + 1
            else:
                path_to_metric = amd_smi_source.format(partition_id = partition_id).split(".")
                if metric_to_test.lower() not in exporter_metrics:
                    Logger.warning(f"Sample {sample_id}: {metric_to_test} absent from exporter for gpu:{gpu_id} — skipping sample")
                    missing_count += 1
                    continue
                m_info_list = list(filter(lambda x: x['labels']['gpu_id'] == str(gpu_id), exporter_metrics[metric_to_test.lower()]))
                Logger.debug(f"Found total {len(m_info_list)} exported metrics for {metric_to_test}")
                if len(m_info_list) != 1:
                    Logger.warning(f"Sample {sample_id}: {metric_to_test} entry count {len(m_info_list)} for gpu:{gpu_id} — skipping sample")
                    missing_count += 1
                    continue
                metric_info = m_info_list[0]
                if isinstance(amd_smi_metrics, list):
                    amd_smi_val = _extract_amd_smi_value(amd_smi_metrics[gpu_id], path_to_metric)
                elif isinstance(amd_smi_metrics, dict) and 'gpu_data' in amd_smi_metrics.keys():
                    amd_smi_val = _extract_amd_smi_value(amd_smi_metrics['gpu_data'][gpu_id], path_to_metric)
                if amd_smi_val is None:
                    Logger.warning(f"Sample {sample_id}: {metric_to_test} amd-smi path missing for gpu:{gpu_id}")
                    missing_count += 1
                    continue
                if isinstance(amd_smi_val, dict):
                    if amd_smi_val["value"] == 'N/A':
                        Logger.warning(f"Sample {sample_id}: {metric_to_test} amd-smi N/A for gpu:{gpu_id} — metric unsupported on this GPU")
                        continue
                    lower_limit = int(0.95 * float(amd_smi_val["value"]))
                    upper_limit = int(1.05 * float(amd_smi_val["value"]))
                elif isinstance(amd_smi_val, int) or isinstance(amd_smi_val, float):
                    lower_limit = int(0.95 * float(amd_smi_val))
                    upper_limit = int(1.05 * float(amd_smi_val))
                elif isinstance(amd_smi_val, str) and amd_smi_val == "N/A":
                    Logger.warning(f"Sample {sample_id}: {metric_to_test} amd-smi N/A for gpu:{gpu_id} — metric unsupported on this GPU")
                    continue
                else:
                    Logger.warning(f"Sample {sample_id}: {metric_to_test} unexpected amd-smi type {type(amd_smi_val)} for gpu:{gpu_id}")
                    continue

                Logger.debug(f"{metric_to_test} Sample:{sample_id} AMD-SMI: {amd_smi_val}, exporter : {metric_info}")
                if lower_limit == 0 and upper_limit == 0 and int(metric_info["value"]) == 0:
                    Logger.debug(f"{metric_to_test} Sample:{sample_id}: zero-vs-zero — inconclusive, skip")
                    continue
                if lower_limit <= int(metric_info["value"]) <= upper_limit:
                    hit_count = hit_count + 1
                else:
                    miss_count = miss_count + 1

        return hit_count, miss_count, missing_count

    metric_validated = False
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")
    all_idle_metrics, all_workload_metrics, loaded_gpu_count = metrics_samples
    partition_id = 0 # TODO: Extend this when GPU is partitioned
    for node in gpu_nodes:
        node_ip = k8_util.k8_get_node_address(node)
        cluster_node = gpu_cluster.find_node_by_ip(node_ip)
        if not cluster_node:
            pytest.fail(f"Unable to get worker node from cluster for ip: {node_ip}")
        node_name = k8_util.k8_get_node_hostname(node)

        if not metric_util.is_metric_supported(metric_to_test, cluster_node.gpu_series, cluster_node.amdgpu_driver_version, 
                                               images["metricsExporter.image.version"],
                                               num_gpus=cluster_node.num_gpus):
            continue
        metric_validated = True

        """
        for idle-state metrics, access metrics values as below:

        idle_metrics['exporter'] = exporter_metrics
        idle_metrics['amd-smi'] = smi_metrics

        for workload-state metrics, access metrics values as below:

        workload_metrics['exporter'] = exporter_metrics
        workload_metrics['amd-smi'] = smi_metrics
        """
        idle_metrics = all_idle_metrics[node_name]
        workload_metrics = all_workload_metrics[node_name]
        gpu_capacity, _ = k8_util.k8_get_node_gpu_capacity(node_name)
        gpu_count = min(cluster_node.num_gpus, gpu_capacity) if gpu_capacity > 0 else cluster_node.num_gpus

        for gpu_id in range(gpu_count):
            num_samples = idle_metrics['num-samples']
            idle_hit_count, idle_miss_count, idle_missing_count = _analyze_metrics_collection(metric_to_test, gpu_id, partition_id, idle_metrics)
            Logger.info(f"Worker: {node_name} GPU: {gpu_id} - Idle Hit/Miss/Missing: {idle_hit_count}/{idle_miss_count}/{idle_missing_count}")

            load_hit_count, load_miss_count, load_missing_count = _analyze_metrics_collection(metric_to_test, gpu_id, partition_id, workload_metrics)
            Logger.info(f"Worker: {node_name} GPU: {gpu_id} - Loaded Hit/Miss/Missing: {load_hit_count}/{load_miss_count}/{load_missing_count}")

            # effective = samples where both amd-smi and exporter had comparable data
            idle_effective = idle_hit_count + idle_miss_count
            load_effective = load_hit_count + load_miss_count

            # If amd-smi returned N/A for all samples, the metric is not supported on this GPU
            if idle_effective == 0 and load_effective == 0:
                Logger.warning(f"Worker: {node_name} GPU: {gpu_id} - {metric_to_test}: no comparable samples "
                               f"(amd-smi N/A or exporter missing in all samples), skipping accuracy check")
                continue

            # At least one hit among samples where comparison was possible
            K8Helper.triage(environment, (idle_effective == 0 or idle_hit_count >= 1),
                            f"IDLE Metric: {metric_to_test} GPU: {gpu_id} not in sync, "
                            f"hit: {idle_hit_count}, miss: {idle_miss_count}, missing: {idle_missing_count}",
                            skip_techsupport=True)
            K8Helper.triage(environment, (load_effective == 0 or load_hit_count >= 1),
                            f"LOAD Metric: {metric_to_test} GPU: {gpu_id} not in sync, "
                            f"hit: {load_hit_count}, miss: {load_miss_count}, missing: {load_missing_count}",
                            skip_techsupport=True)

            # Relaxing passing conditions — ≥50% hit rate among effective comparisons
            K8Helper.triage(environment, (idle_effective == 0 or idle_hit_count >= int(0.50 * idle_effective)),
                            f"IDLE Metric: {metric_to_test} GPU: {gpu_id} not in sync, "
                            f"hit: {idle_hit_count}, miss: {idle_miss_count}, missing: {idle_missing_count}",
                            expected_to_fail=True)
            K8Helper.triage(environment, (load_effective == 0 or load_hit_count >= int(0.50 * load_effective)),
                            f"LOAD Metric: {metric_to_test} GPU: {gpu_id} not in sync, "
                            f"hit: {load_hit_count}, miss: {load_miss_count}, missing: {load_missing_count}",
                            expected_to_fail=True)


    if not metric_validated:
        pytest.skip(f"Metric {metric_to_test} cannot be validated in this setup - skip")

def test_exporter_prof_metrics_support(gpu_cluster, images, metrics_samples, prof_metric_to_test, environment):
    """
    Verify that variations are reflected in the profiler metrics published by the AMD Device Metrics Exporter (DME).
    """
    global Logger
    global LogPrettyPrinter

    for node in gpu_cluster.cluster_nodes:
        if node.device_id:
            if not amdgpu_util.get_gpu_features(node.device_id).get("profiler_metrics", True):
                pytest.skip(f"Profiler metrics disabled for {node.gpu_series} — skip profiler validation")

    metric_metadata = metric_util.get_metric_metadata(prof_metric_to_test)
    def _compare_idle_vs_load(prof_metric_to_test, gpu_id, partition_id, idle_metric_data, load_metric_data):
        num_samples = idle_metric_data['num-samples']
        Logger.info(f"Processing {idle_metric_data['title']} - total samples {num_samples}")
        idle_all_exporter_metrics = idle_metric_data['exporter']
        load_all_exporter_metrics = load_metric_data['exporter']
        variation = 0
        no_variation = 0
        for sample_id in range(num_samples):
            # Extract exporter metrics for current sample_id
            idle_exporter_metrics = metric_util.parse_metric_data(idle_all_exporter_metrics[sample_id])
            load_exporter_metrics = metric_util.parse_metric_data(load_all_exporter_metrics[sample_id])

            gpu_support_info = metric_util.get_metric_support_info(metric_metadata, idle_metric_data["gpu-series"])
            K8Helper.triage(environment, (gpu_support_info != None),
                            f"Missing gpu-support-info for {prof_metric_to_test}, {metric_metadata}, {idle_metric_data['gpu-series']}",
                            skip_techsupport=True)

            K8Helper.triage(environment, (prof_metric_to_test.lower() in idle_exporter_metrics),
                            f"Missing {prof_metric_to_test} in collected metrics from exporter endpoint idle condition, {metric_metadata}",
                            skip_techsupport=True)
            K8Helper.triage(environment, (prof_metric_to_test.lower() in load_exporter_metrics),
                            f"Missing {prof_metric_to_test} in collected metrics from exporter endpoint load conditions, {metric_metadata}",
                            skip_techsupport=True)
            idle_m_info_list = list(filter(lambda x: x['labels']['gpu_id'] == str(gpu_id), idle_exporter_metrics[prof_metric_to_test.lower()]))
            Logger.debug(f"Found total {len(idle_m_info_list)} ide exported metrics for {prof_metric_to_test}")
            load_m_info_list = list(filter(lambda x: x['labels']['gpu_id'] == str(gpu_id), load_exporter_metrics[prof_metric_to_test.lower()]))
            Logger.debug(f"Found total {len(load_m_info_list)} load exported metrics for {prof_metric_to_test}")

            K8Helper.triage(environment, len(idle_m_info_list) == 1,
                            f"Found {len(idle_m_info_list)} values for IDLE {prof_metric_to_test}, gpu-id: {gpu_id}, info: {gpu_support_info}",
                            skip_techsupport=True)
            K8Helper.triage(environment, len(load_m_info_list) == 1,
                            f"Found {len(load_m_info_list)} values for LOAD {prof_metric_to_test}, gpu-id: {gpu_id}, info: {gpu_support_info}",
                            skip_techsupport=True)
            idle_metric_info = idle_m_info_list[0]
            load_metric_info = load_m_info_list[0]

            Logger.debug(f"{prof_metric_to_test} Sample:{sample_id} IDLE-Value exporter : {idle_metric_info}")
            Logger.debug(f"{prof_metric_to_test} Sample:{sample_id} LOAD-Value exporter : {load_metric_info}")
            if float(idle_metric_info["value"]) != float(load_metric_info["value"]):
                variation = variation + 1
            else:
                no_variation = no_variation + 1

        return variation, no_variation

    metric_validated = False
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")
    all_idle_metrics, all_workload_metrics, loaded_gpu_count = metrics_samples
    partition_id = 0 # TODO: Extend this when GPU is partitioned
    for node in gpu_nodes:
        node_ip = k8_util.k8_get_node_address(node)
        cluster_node = gpu_cluster.find_node_by_ip(node_ip)
        if not cluster_node:
            pytest.fail(f"Unable to get worker node from cluster for ip: {node_ip}")
        node_name = k8_util.k8_get_node_hostname(node)

        enable_profiler = amdgpu_util.get_gpu_features(cluster_node.device_id).get("profiler_metrics", True) if cluster_node.device_id else True
        supported_metrics = metric_util.get_supported_metrics(gpu_series=cluster_node.gpu_series,
                                                              skip_profiler_metrics=not enable_profiler,
                                                              amdgpu_driver=cluster_node.amdgpu_driver_version,
                                                              dme_version=images['metricsExporter.image.version'],
                                                              num_gpus=cluster_node.num_gpus)
        if prof_metric_to_test not in [e['name'] for e in supported_metrics]:
            continue
        metric_validated = True

        """
        for idle-state metrics, access metrics values as below:

        idle_metrics['exporter'] = exporter_metrics
        idle_metrics['amd-smi'] = smi_metrics

        for workload-state metrics, access metrics values as below:

        workload_metrics['exporter'] = exporter_metrics
        workload_metrics['amd-smi'] = smi_metrics
        """
        idle_metrics = all_idle_metrics[node_name]
        workload_metrics = all_workload_metrics[node_name]
        gpu_capacity, _ = k8_util.k8_get_node_gpu_capacity(node_name)
        gpu_count = min(cluster_node.num_gpus, gpu_capacity) if gpu_capacity > 0 else cluster_node.num_gpus
        loaded_count = loaded_gpu_count.get(node_name, gpu_count)

        for gpu_id in range(gpu_count):
            num_samples = idle_metrics['num-samples']
            variation, no_variation = _compare_idle_vs_load(prof_metric_to_test, gpu_id, partition_id, idle_metrics, workload_metrics)
            Logger.info(f"Worker: {node_name} GPU: {gpu_id} - Variation/No-Variation: {variation}/{no_variation}")

            if gpu_id >= loaded_count:
                Logger.info(f"Worker: {node_name} GPU: {gpu_id} - skipping variation assert (no workload on this GPU)")
                continue

            # Atleast there should be one variation with workload
            K8Helper.triage(environment, (variation >= 1),
                            f"Metric: {prof_metric_to_test} GPU: {gpu_id} no-variation , variation: {variation}, no-variation {no_variation}",
                            skip_techsupport=True)

            # Atleast there should be some variation with workload
            # Relaxing passing conditions
            K8Helper.triage(environment, (variation >= int(0.50 * num_samples)),
                            f"Metric: {prof_metric_to_test} GPU: {gpu_id} too little variation, variation: {variation}, no-variation {no_variation}",
                            expected_to_fail = True)

    if not metric_validated:
        pytest.skip(f"Metric {prof_metric_to_test} cannot be validated in this setup - skip")


def test_metric_coverage(gpu_cluster, images, metrics_samples, environment):
    """Verify no metrics in the idle exporter sample are absent from metrics-support.json.

    The metrics_samples fixture is collected with profiler metrics enabled (per GPU type),
    so skip_profiler_metrics=False is used to include profiler entries in the expected set.
    """
    global Logger

    all_idle_metrics, _, _ = metrics_samples
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, len(gpu_nodes) > 0, "No nodes with AMD/GPU found in the cluster")

    failed_nodes = {}
    for node in gpu_nodes:
        node_ip = k8_util.k8_get_node_address(node)
        cluster_node = gpu_cluster.find_node_by_ip(node_ip)
        if not cluster_node:
            pytest.fail(f"Unable to get worker node from cluster for ip: {node_ip}")
        node_name = k8_util.k8_get_node_hostname(node)
        idle_metrics = metric_util.parse_metric_data(all_idle_metrics[node_name]['exporter'][0])
        untracked = metric_util.find_untracked_metrics(
            idle_metrics,
            gpu_series=cluster_node.gpu_series,
            amdgpu_driver=cluster_node.amdgpu_driver_version,
            dme_version=images['metricsExporter.image.version'],
            skip_profiler_metrics=False,
            num_gpus=cluster_node.num_gpus,
        )
        if untracked:
            Logger.warning(f"Node {node_name}: {len(untracked)} untracked metrics: {sorted(untracked)}")
            failed_nodes[node_name] = sorted(untracked)
    K8Helper.triage(environment, not failed_nodes,
                    f"DME exports metrics not tracked in metrics-support.json: {failed_nodes}")


def test_exporter_metric_nonzero_in_samples(gpu_cluster, images, metrics_samples, metric_to_test, environment):
    """
    Guard against 'all-zero nirvana': at least one idle or load sample must
    report a non-zero value for this metric on each GPU.

    A metric reporting 0 in every sample may indicate broken metric collection
    rather than genuine quiescence.  Metrics that legitimately stay at 0 under
    all conditions (error counters, inactive links) should be marked
    skip-validation: 'yes' in metrics-support.json to opt out of this check.

    Hard-fail: all idle+load samples report 0 for a GPU.
    Skip: metric not supported for this GPU series or no samples collected.
    """
    global Logger

    def _get_entries(parsed, key, gpu_id):
        """Return metric entries for gpu_id, trying amd_-prefixed key first."""
        amd_key = f"amd_{key}" if not key.startswith("amd_") else key
        return [
            e for e in parsed.get(amd_key, parsed.get(key, []))
            if e['labels'].get('gpu_id') == str(gpu_id)
        ]

    metric_validated = False
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")
    all_idle_metrics, all_workload_metrics, loaded_gpu_count = metrics_samples

    metric_metadata = metric_util.get_metric_metadata(metric_to_test)
    metric_types = metric_metadata.get('type', {})

    failed_gpus = {}

    for node in gpu_nodes:
        node_ip = k8_util.k8_get_node_address(node)
        cluster_node = gpu_cluster.find_node_by_ip(node_ip)
        if not cluster_node:
            pytest.fail(f"Unable to get worker node from cluster for ip: {node_ip}")
        node_name = k8_util.k8_get_node_hostname(node)

        if not metric_util.is_metric_supported(metric_to_test, cluster_node.gpu_series,
                                               cluster_node.amdgpu_driver_version,
                                               images["metricsExporter.image.version"],
                                               num_gpus=cluster_node.num_gpus):
            continue
        metric_validated = True

        idle_metrics = all_idle_metrics[node_name]
        workload_metrics = all_workload_metrics[node_name]
        gpu_capacity, _ = k8_util.k8_get_node_gpu_capacity(node_name)
        gpu_count = min(cluster_node.num_gpus, gpu_capacity) if gpu_capacity > 0 else cluster_node.num_gpus
        loaded_count = loaded_gpu_count.get(node_name, gpu_count)

        for gpu_id in range(gpu_count):
            if gpu_id >= loaded_count:
                Logger.info(f"Node {node_name} GPU:{gpu_id} — skipping nonzero check (no workload)")
                continue

            max_val = 0.0
            all_raw = (
                [(s, 'idle') for s in idle_metrics['exporter']] +
                [(s, 'load') for s in workload_metrics['exporter']]
            )
            for raw, _phase in all_raw:
                parsed = metric_util.parse_metric_data(raw)
                if 'labeled' in metric_types and ':' in metric_to_test:
                    label_name = metric_types['labeled']['label']
                    metric_name, label_value = metric_to_test.split(':', 1)
                    entries = [
                        e for e in _get_entries(parsed, metric_name.lower(), gpu_id)
                        if e['labels'].get(label_name, '').lower() == label_value.lower()
                    ]
                else:
                    base = metric_to_test.split(':')[0]
                    entries = _get_entries(parsed, base.lower(), gpu_id)
                for e in entries:
                    try:
                        v = float(e['value'])
                        if v > max_val:
                            max_val = v
                    except (ValueError, TypeError):
                        pass

            if max_val == 0:
                Logger.warning(
                    f"Node {node_name} GPU:{gpu_id} — {metric_to_test} reported 0 "
                    f"in all {len(all_raw)} samples (idle+load)"
                )
                failed_gpus[f"{node_name}/gpu:{gpu_id}"] = metric_to_test

    if not metric_validated:
        pytest.skip(f"Metric {metric_to_test} cannot be validated in this setup - skip")

    K8Helper.triage(environment, not failed_gpus,
        f"{metric_to_test} reported 0 in all idle+load samples for: {list(failed_gpus.keys())}. "
        f"Possible broken metric collection. "
        f"Add skip-validation: 'yes' to metrics-support.json if this metric is legitimately always 0.",
        skip_techsupport=True)
