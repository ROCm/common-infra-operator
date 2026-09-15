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
import logging
import json
import re
import os
import pprint
import time
import threading
from packaging import version
from collections import defaultdict
from prometheus_client.parser import text_string_to_metric_families
import lib.k8_util as k8_util
from lib.util import K8Helper

Logger = logging.getLogger("lib.metricutil")
LogPrettyPrinter = pprint.PrettyPrinter(indent = 2)

def get_label_details(version_string):
    with open('lib/files/label-support-matrix.json', 'r') as fp:
        label_data = json.load(fp)

    if 'main' in version_string or 'exporter' in version_string or 'collab-7.12' in version_string:
        sw_version = version.Version("v99.99.99")
    else:
        sw_version = version.Version(version_string.split('-', 1)[0])

    label_support_info = {}
    for label, info in label_data.items():
        min_version = version.Version(info['min-version'])
        if min_version > sw_version:
            Logger.debug(f"skipping label : {label} with info: {info} for current-version : {sw_version}")
            continue
        if info.get("eos-version", None) != None:
            eos_version = version.Version(info["eos-version"])
            if sw_version > eos_version:
                Logger.debug(f"skipping label : {label} with info: {info} for current-version : {sw_version}")
                continue

        label_support_info[label] = info["mandatory"].get(f"v{str(sw_version)}", "no")
    return label_support_info

def dump_metrics(http_response, out_file):
    metric_data = str(http_response)
    with open(out_file, "w") as fp:
        for line in metric_data.split('\\n'):
            fp.write(line.strip())
            fp.write("\n")
    return

def dump_all_samples(all_metrics, file_prefix):
    for idx, sample in enumerate(all_metrics):
        out_file = f"{file_prefix}_{idx}.output"
        dump_metrics(sample, out_file)
    return

def dump_json_samples(all_json_samples, file_prefix):
    pattern = r'("[^"]+")\s*:\s*"(\[.*?\])"'
    replacement = r'\1: \2'
    for idx, sample in enumerate(all_json_samples):
        out_file = f"{file_prefix}_{idx}.json"
        try:
            new_sample = re.sub(pattern, replacement, sample.replace("'", "\""))
            with open(out_file, "w") as fp:
                json.dump(json.loads(new_sample), fp, indent=4)
        except:
            try:
                # Write as-is so that we can debug json parsing issue offline
                with open(out_file, "w") as fp:
                    fp.write(sample)
            except:
                # finally redirect to logging so that we have data to analyze failure
                Logger.debug(f"Failed to write json sample to file {out_file}")
                Logger.debug(f"{LogPrettyPrinter.pformat(sample)}")
    return

def parse_metric_data(http_response):
    metrics_content = http_response.decode('utf-8')
    metrics = defaultdict(list)
    for metrics_family in text_string_to_metric_families(metrics_content):
        for entry in metrics_family.samples:
            metrics[entry.name].append({
                'type' : metrics_family.type,
                'value' : entry.value,
                'labels' : entry.labels
            })
    return metrics

def get_supported_metrics(gpu_series = None, skip_profiler_metrics = True, amdgpu_driver = None, dme_version = None, deployment_mode = None, num_gpus = None):
    with open('lib/files/metrics-support.json', 'r') as fp:
        data = json.load(fp)

    metrics = data['metrics']
    # Remove profiler-metrics if enabled
    if skip_profiler_metrics:
        metrics = list(filter(lambda entry: '_PROF_' not in entry['name'], metrics))

    # FABRIC/XGMI metrics require multi-GPU topology (active inter-die links)
    if num_gpus is not None and num_gpus <= 1:
        metrics = [m for m in metrics if not m.get("requires-multi-gpu")]

    # In hypervisor mode, only include metrics explicitly marked as supported
    if deployment_mode == "hypervisor":
        metrics = list(filter(lambda entry: entry.get("hypervisor-mode") == "yes", metrics))

    # Filter by gpu-series if defined
    if gpu_series:
        supported_metrics = []
        for entry in metrics:
            for support in entry['gpu-support']:
                if gpu_series not in support.get('gpu', []):
                    continue
                # Hypervisor-only gpu-support entries are excluded outside hypervisor mode
                if support.get('hypervisor-mode') == 'yes' and deployment_mode != 'hypervisor':
                    continue
                supported_metrics.append(entry)
                break
        metrics = supported_metrics

    # check deprecation-matrix
    if amdgpu_driver:
        amdgpu_driver_version = version.Version(amdgpu_driver)
        supported_metrics = []
        for entry in metrics:
            if entry.get("driver-support", None):
                min_version = version.Version(entry["driver-support"].get("ini", "0.0.0"))
                max_version = version.Version(entry["driver-support"].get("fini", "99.99.99"))
                if min_version <= amdgpu_driver_version and amdgpu_driver_version <= max_version:
                    supported_metrics.append(entry)
            else:
                supported_metrics.append(entry)
        metrics = supported_metrics

    # Filter based on supported device-metrics-exporter version
    if dme_version:
        if "exporter-0.0.1" in dme_version or "collab-7.12" in dme_version:
            exporter_version = version.Version("v99.99.99") # TODO: Hack till CI/CD versioning is fixed
            #Logger.debug(f"Running latest/main DME {exporter_version}")
        else:
            exporter_version = version.Version(dme_version.split("-")[0])
            #Logger.debug(f"Running DME {exporter_version}")
        supported_metrics = []
        for entry in metrics:
            if entry.get("exporter-support", None):
                min_version = version.Version(entry["exporter-support"].get("ini", "v1.0.0"))
                max_version = version.Version(entry["exporter-support"].get("fini", "v99.99.99"))
                if min_version <= exporter_version and exporter_version <= max_version:
                    supported_metrics.append(entry)
            else:
                supported_metrics.append(entry)
        metrics = supported_metrics
    return metrics

def is_metric_supported(metric_to_test, gpu_series, amdgpu_driver, dme_version, num_gpus=None):

    supported_metrics = get_supported_metrics(gpu_series = gpu_series,
                                              skip_profiler_metrics = False,
                                              amdgpu_driver = amdgpu_driver, dme_version = dme_version,
                                              num_gpus = num_gpus)

    for entry in supported_metrics:
        metric_name = entry['name']
        if metric_name.lower() == metric_to_test.lower():
            return True
    return False

def get_metric_metadata(metric_to_test):

    all_metrics = get_supported_metrics(skip_profiler_metrics = False)
    for entry in all_metrics:
        metric_name = entry['name']
        if metric_name.lower() == metric_to_test.lower():
            return entry
    return None

def is_metric_contingent(metric_to_test):

    metric_metadata = get_metric_metadata(metric_to_test)
    if metric_metadata:
        metric_types = metric_metadata.get("type", {})
        if metric_types.get("contingent", "no") == "yes":
            return True
    return False

def get_metric_support_info(metric_metadata, gpu_series, deployment_mode=None):

    for support in metric_metadata['gpu-support']:
        if gpu_series not in support.get('gpu', []):
            continue
        # Skip hypervisor-only entries when not in hypervisor mode
        if support.get('hypervisor-mode') == 'yes' and deployment_mode != 'hypervisor':
            continue
        return support
    return None

def health(port, node):
    ret_code, _, _ = node.http_get(port, "metrics")
    assert ret_code == 0, f"Failed to get metrics for {node.ip_address}"

def service_start(node):
    node.run_command("sudo systemctl start amd-metrics-exporter")

def service_stop(node):
    node.run_command("sudo systemctl stop amd-metrics-exporter")

def cleanup_cfg(node):
    node.run_command("sudo rm -rf /etc/metrics/")

# ---------------------------------------------------------------------------
# SR-IOV / hypervisor helpers
# ---------------------------------------------------------------------------

def scrape_sriov_metrics(node, port=5000):
    """
    Fetch /metrics from the SR-IOV exporter on *node* and return parsed metrics dict.

    Return format matches parse_metric_data():
      {metric_name: [{"type": ..., "value": ..., "labels": {...}}, ...]}
    """
    ret_code, content, err = node.http_get(port, "metrics")
    if ret_code != 0:
        pytest.fail(
            f"Failed to reach SR-IOV exporter at {node.ip_address}:{port}/metrics — {err}"
        )
    return parse_metric_data(content)


def sriov_service_start(node):
    node.run_command("sudo systemctl start gpuagent-sriov amd-metrics-exporter-sriov")


def sriov_service_stop(node):
    node.run_command("sudo systemctl stop amd-metrics-exporter-sriov gpuagent-sriov")


def assert_sriov_metric_present(metrics, name, label_filter=None):
    """Fail if *name* (with optional label_filter) is absent from scraped metrics."""
    samples = metrics.get(name, [])
    if label_filter:
        samples = [s for s in samples
                   if all(s["labels"].get(k) == v for k, v in label_filter.items())]
    assert samples, f"Metric '{name}' with labels {label_filter} not found in /metrics"


def collect_vf_workload_diagnostics(node, environment, metrics_port: int = 5000,
                                     tag: str = "") -> None:
    """
    Save amd-smi JSON snapshots and the full metrics endpoint to per-TC log files.

    Intended to be called from vm0_workload fixtures in hypervisor/debian and
    hypervisor/docker test_vf_metrics modules after the workload is confirmed
    running, so every test run captures a baseline snapshot for triage.

    Files written (under environment.context.log_dir):
      <tc_name>[_<tag>]_amd_smi_metric.json  — amd-smi metric --json
      <tc_name>[_<tag>]_amd_smi_static.json  — amd-smi static --json
      <tc_name>[_<tag>]_metrics.txt           — full /metrics endpoint output

    No-op for amd-smi if the binary is not installed on the node.
    """
    import lib.gim_util as gim_util
    from pathlib import Path

    ctx = environment.context
    if ctx is not None:
        log_dir = Path(ctx.log_dir)
        prefix = f"{ctx.current_tc_name}_{tag}" if tag else ctx.current_tc_name
    else:
        log_dir = Path(environment.logdir)
        prefix = tag or "vf_workload"

    amd_smi = gim_util.find_amd_smi(node)
    if amd_smi is None:
        Logger.warning("amd-smi not found on node — skipping JSON snapshot")
    else:
        for cmd_args, fname in [
            ("metric --json", f"{prefix}_amd_smi_metric.json"),
            ("static --json", f"{prefix}_amd_smi_static.json"),
        ]:
            rc, out, stderr = node.run_command(f"sudo {amd_smi} {cmd_args}")
            text = out.decode("utf-8") if isinstance(out, bytes) else out
            if rc == 0:
                artifact = log_dir / fname
                artifact.write_text(text, encoding="utf-8")
                Logger.info(f"amd-smi {cmd_args} → {artifact} ({len(text)} bytes)")
            else:
                Logger.warning(f"{amd_smi} {cmd_args} failed (rc={rc}): {stderr}")

    rc, raw, err = node.http_get(metrics_port, "metrics")
    if rc == 0:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        artifact = log_dir / f"{prefix}_metrics.txt"
        artifact.write_text(text, encoding="utf-8")
        Logger.info(f"/metrics → {artifact} ({len(text)} bytes)")
    else:
        Logger.warning(f"/metrics endpoint unreachable on port {metrics_port}: {err}")


def collect_vf_samples(node, amd_smi_path, port, num_samples, interval, logdir, ctxt_name):
    """
    Collect num_samples parallel snapshots of (/metrics, amd-smi metric --json)
    on the hypervisor node for SR-IOV exporter accuracy testing.

    Mirrors collect_metrics_samples() but for the hypervisor context:
    - node.http_get() instead of pod exec curl
    - node.run_command() for amd-smi instead of pod exec
    - No gpuctl (not applicable in SR-IOV)
    - Returns a flat dict (no outer node_name nesting)

    Returns:
        {
            'num-samples': int,
            'exporter': [bytes, ...],   # raw Prometheus text per sample
            'amd-smi':   [str, ...],    # raw JSON string per sample
        }
    """
    exporter_samples, smi_samples = [], []

    def _collect_exporter():
        for _ in range(num_samples):
            rc, raw, _ = node.http_get(port, "metrics")
            if rc == 0:
                exporter_samples.append(raw)
            time.sleep(interval)

    def _collect_smi():
        for _ in range(num_samples):
            rc, out, _ = node.run_command(f"sudo {amd_smi_path} metric --json")
            if rc == 0:
                normalized = out.replace("'", '"').replace("True", '"True"').replace("False", '"False"')
                smi_samples.append(normalized)
            time.sleep(interval)

    threads = [
        threading.Thread(target=_collect_exporter),
        threading.Thread(target=_collect_smi),
    ]
    for t in threads:
        t.start()
    time.sleep(num_samples * interval)
    for t in threads:
        t.join()

    dump_all_samples(exporter_samples, os.path.join(logdir, f"{ctxt_name}_curl"))
    dump_json_samples(smi_samples, os.path.join(logdir, f"{ctxt_name}_smi_metrics"))
    return {
        'num-samples': len(exporter_samples),
        'exporter': exporter_samples,
        'amd-smi': smi_samples,
    }


def get_sriov_metric_value(metrics, name, label_filter=None):
    """Return the value of the first matching sample, or fail."""
    samples = metrics.get(name, [])
    if label_filter:
        samples = [s for s in samples
                   if all(s["labels"].get(k) == v for k, v in label_filter.items())]
    assert samples, f"Metric '{name}' with labels {label_filter} not found"
    return samples[0]["value"]


def assert_sriov_metric_range(metrics, name, label_filter, lo, hi):
    """Fail if the metric value falls outside [lo, hi]."""
    val = get_sriov_metric_value(metrics, name, label_filter)
    assert lo <= val <= hi, (
        f"Metric '{name}' value {val} outside expected range [{lo}, {hi}]"
    )


def assert_deployment_mode_label(metrics, name, expected="hypervisor"):
    """Check that at least one sample for *name* carries deployment_mode=expected."""
    assert_sriov_metric_present(metrics, name, {"deployment_mode": expected})


def _metric_base_name(prometheus_name):
    """Strip the optional 'amd_' prefix and lowercase for comparison with _entry_base_name."""
    name = prometheus_name[4:] if prometheus_name.startswith("amd_") else prometheus_name
    return name.lower()


def _entry_base_name(json_name):
    """Strip :LABEL_VALUE suffix from JSON metric names for Prometheus name comparison.

    Labeled metrics like GPU_CLOCK:SYSTEM map to the Prometheus metric name
    'gpu_clock' (with a clock_type label), not 'gpu_clock:system'.
    """
    return json_name.split(':')[0].lower()


def find_untracked_metrics(scraped_metrics, gpu_series=None, skip_profiler_metrics=True,
                           amdgpu_driver=None, dme_version=None, deployment_mode=None,
                           num_gpus=None):
    """
    Return the set of Prometheus metric names present in *scraped_metrics* but
    absent from metrics-support.json for the given parameters.

    Callers use this to detect metrics the exporter emits that the test
    infrastructure has not yet catalogued — a drift guard between the exporter
    and the test data set.

    The comparison is prefix-agnostic: the DME 'amd_' prefix is optional and
    controlled by config.json, so 'amd_gpu_clock' and 'gpu_clock' both map to
    the same metrics-support.json entry 'GPU_CLOCK'.

    Labeled JSON entries (e.g. GPU_CLOCK:SYSTEM) are matched by their base name
    only (gpu_clock), since the Prometheus metric name carries no colon suffix —
    the label value is a Prometheus label, not part of the metric name.

    Prometheus internal metrics (promhttp_*) are excluded from the comparison.
    """
    Logger.info(f"find_untracked_metrics: gpu_series={gpu_series}, amdgpu_driver={amdgpu_driver}, "
                f"dme_version={dme_version}, skip_profiler={skip_profiler_metrics}, "
                f"deployment_mode={deployment_mode}, num_gpus={num_gpus}")
    supported = get_supported_metrics(
        gpu_series=gpu_series,
        skip_profiler_metrics=skip_profiler_metrics,
        amdgpu_driver=amdgpu_driver,
        dme_version=dme_version,
        deployment_mode=deployment_mode,
        num_gpus=num_gpus,
    )
    expected_base_names = {_entry_base_name(entry["name"]) for entry in supported}

    # Build set of multi-gpu metric base names to ignore in the untracked scan
    # when num_gpus <= 1. The DME may still export these with value 0, but they
    # are not meaningful on a single-GPU node.
    multi_gpu_ignore = set()
    if num_gpus is not None and num_gpus <= 1:
        with open('lib/files/metrics-support.json', 'r') as fp:
            all_data = json.load(fp)
        multi_gpu_ignore = {_entry_base_name(m["name"]) for m in all_data['metrics'] if m.get("requires-multi-gpu")}

    Logger.info(f"find_untracked_metrics: {len(expected_base_names)} expected base names, "
                f"{len(scraped_metrics)} scraped metrics, {len(multi_gpu_ignore)} multi-gpu ignored")

    untracked = set()
    for name in scraped_metrics:
        if name.startswith("promhttp_"):
            continue
        base = _metric_base_name(name)
        if base in multi_gpu_ignore:
            continue
        if base not in expected_base_names:
            untracked.add(name)
    return untracked


def collect_metrics_samples(gpu_cluster, gpu_nodes, exporter_port_map, environment, ctxt_name):
    Logger.info(f"Collecting metrics-exporter curl output, amd-smi metrics and gpuctl metrics snapshot")

    def _collect_amd_smi_output(cmd_responses, exporter_pod_name, num_samples = 10, interval = 1):
        cmd = ["amd-smi", "metric", "--json"]
        while len(cmd_responses) < num_samples:
            ret_code, resp_stdout, resp_stderr = k8_util.exec_command_in_pod(environment.gpu_operator_namespace,
                                                                             cmd, exporter_pod_name,
                                                                             "metrics-exporter-container")
            if ret_code != 0:
                Logger.error(f"Cmd {cmd} failed on {exporter_pod_name} (rc={ret_code}): {resp_stderr}")
            else:
                if "TypeError" in resp_stdout:
                    Logger.warning(f"amd-smi returned TypeError on {exporter_pod_name}, retrying with '-g all'")
                    cmd = ["amd-smi", "metric", "-g", "all", "--json"]
                    continue
                cmd_responses.append(resp_stdout.replace("'", "\"").replace("True", "\"True\"").replace("False", "\"False\""))
            time.sleep(interval)
        return

    def _collect_gpuctl_output(cmd_responses, exporter_pod_name, num_samples = 10, interval = 1):
        cmd = ["gpuctl", "show", "gpu", "--json"]
        for _ in range(num_samples):
            ret_code, resp_stdout, resp_stderr = k8_util.exec_command_in_pod(environment.gpu_operator_namespace,
                                                                             cmd, exporter_pod_name,
                                                                             "metrics-exporter-container")
            if ret_code != 0:
                Logger.error(f"Cmd {cmd} failed on {exporter_pod_name} (rc={ret_code}): {resp_stderr}")
            else:
                cmd_responses.append(resp_stdout.replace("'", "\"").replace("True", "\"True\"").replace("False", "\"False\""))
            time.sleep(interval)
        return

    def _collect_exporter_metrics(cmd_responses, exporter_pod_name, num_samples = 10, interval = 1):
        cmd = ["curl", "-s", "http://localhost:5000/metrics"]
        for _ in range(num_samples):
            ret_code, resp_stdout, resp_stderr = k8_util.exec_command_in_pod(environment.gpu_operator_namespace,
                                                                             cmd, exporter_pod_name,
                                                                             "metrics-exporter-container")
            if ret_code != 0:
                Logger.error(f"Failed to get metrics from exporter pod {exporter_pod_name} (rc={ret_code}): {resp_stderr}")
            else:
                cmd_responses.append(resp_stdout.encode('utf-8'))
            time.sleep(interval)
        return

    num_samples = 10
    interval = 15
    collected_metrics = {}
    for node in gpu_nodes:
        node_ip = k8_util.k8_get_node_address(node)
        cluster_node = gpu_cluster.find_node_by_ip(node_ip)
        if not cluster_node:
            pytest.fail(f"Unable to get worker node from cluster for ip: {node_ip}")
        node_name = k8_util.k8_get_node_hostname(node)
        exporter_pod_name = k8_util.k8_get_pod_name("metrics-exporter", environment.gpu_operator_namespace, node_name)
        # Collect gpu information from the node
        cmd = ["amd-smi", "static", "--json"]
        ret_code, amd_smi_info, resp_stderr = k8_util.exec_command_in_pod(environment.gpu_operator_namespace,
                                                                          cmd, exporter_pod_name,
                                                                          "metrics-exporter-container")
        K8Helper.triage(environment, (ret_code == 0 and len(amd_smi_info) > 0),
                        f"Unable to collect amd-smi static information from node {node_name} (rc={ret_code}): {resp_stderr}")

        threads = []
        exporter_metrics = []
        smi_metrics = []
        gpuctl_metrics = []

        threads.append(threading.Thread(target = _collect_amd_smi_output, args=(smi_metrics, exporter_pod_name, num_samples, interval)))
        threads.append(threading.Thread(target = _collect_exporter_metrics, args=(exporter_metrics, exporter_pod_name, num_samples, interval)))
        if environment.builtin_gpuctl_support:
            threads.append(threading.Thread(target = _collect_gpuctl_output, args=(gpuctl_metrics, exporter_pod_name, num_samples, interval)))

        # Start all the threads
        for thr in threads:
            thr.start()

        time.sleep(num_samples * interval)

        # Wait for all threads to complete
        for thr in threads:
            thr.join()

        collected_metrics[node_name] = {}
        collected_metrics[node_name]['title'] = f"Metrics for {node_name} under {ctxt_name} conditions"
        collected_metrics[node_name]['num-samples'] = num_samples
        collected_metrics[node_name]['gpu-series'] = cluster_node.gpu_series
        collected_metrics[node_name]['gpu-info'] = amd_smi_info
        collected_metrics[node_name]['exporter'] = exporter_metrics
        collected_metrics[node_name]['amd-smi'] = smi_metrics
        collected_metrics[node_name]['gpuctl'] = gpuctl_metrics
        dump_json_samples(smi_metrics, os.path.join(environment.logdir, f"{ctxt_name}_{node_name}_smi_metrics"))
        dump_json_samples([amd_smi_info], os.path.join(environment.logdir, f"{ctxt_name}_{node_name}_smi_info"))
        dump_all_samples(exporter_metrics, os.path.join(environment.logdir, f"{ctxt_name}_{node_name}_curl"))
        dump_json_samples(gpuctl_metrics, os.path.join(environment.logdir, f"{ctxt_name}_{node_name}_gpuctl"))
        K8Helper.triage(environment, (len(smi_metrics) == num_samples),
                        f"Failed to collect all required number of amd-smi-metrics samples for node {node_name}")
        K8Helper.triage(environment, (len(exporter_metrics) == num_samples),
                        f"Failed to collect all required number of metrics-exporter samples for node {node_name}")
        if environment.builtin_gpuctl_support:
            K8Helper.triage(environment, (len(gpuctl_metrics) == num_samples),
                            f"Failed to collect all required number of gpuctl-metrics samples for node {node_name}")
    return collected_metrics

