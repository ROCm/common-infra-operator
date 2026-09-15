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
VF Metrics Accuracy Test Suite — Debian Package Deployment.

Validates accuracy and completeness of ALL SR-IOV exporter metrics across idle
and workload conditions using multi-sample collection.  Complements the targeted
single-metric checks in test_vf_metrics.py with full-spectrum validation:

  - test_vf_metric_completeness   — each metric present in expected fraction of samples
  - test_vf_metric_value_accuracy — exporter vs amd-smi cross-validation (±5%)
  - test_vf_metric_coverage       — no exporter metric absent from metrics-support.json

Collection: _NUM_SAMPLES samples at _SAMPLE_INTERVAL seconds, two parallel threads
(curl /metrics + amd-smi metric --json), under both idle and workload conditions.
Total collection wall-time: 2 × _NUM_SAMPLES × _SAMPLE_INTERVAL seconds.
"""

import json
import logging
import os
import re
import threading
import time

import pytest

import lib.gim_util as gim_util
import lib.metric_util as metric_util
import lib.vm_util as vm_util
from lib.util import K8Helper

Logger = logging.getLogger("hypervisor.debian.test_metrics_accuracy")

_METRICS_PORT           = 5000
_NUM_SAMPLES            = 10
_SAMPLE_INTERVAL        = 10   # seconds; covers ~2 exporter cache cycles per sample
_WORKLOAD_SPEC_TYPE     = "rocm-pytorch-gemm-stress"
_WORKLOAD_SCRIPT        = "/tmp/gemm_stress.py"
_POST_START_WAIT        = 10
_SCRAPE_INTERVAL        = 5
_WORKLOAD_READY_TIMEOUT = 120


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _extract_amd_smi_value(amd_smi_obj, path):
    """Recursively extract a value from a nested dict/list by path segments."""
    if not path:
        return None
    if len(path) == 1:
        return amd_smi_obj.get(path[0], None) if isinstance(amd_smi_obj, dict) else None
    return _extract_amd_smi_value(amd_smi_obj.get(path[0], {}), path[1:])


def _prom_key(json_name):
    """Convert a metrics-support.json metric name to its Prometheus dict key.

    metrics-support.json names are uppercase with no 'amd_' prefix (e.g. GPU_CLOCK).
    parse_metric_data() stores entries keyed by the full Prometheus name (e.g. amd_gpu_clock).
    We try the prefixed form first, then fall back to the bare lowercase form for deployments
    that strip the 'amd_' prefix in config.json.
    """
    base = json_name.split(':')[0].lower()  # strip :LABEL_VALUE suffix
    return f'amd_{base}'


def _is_metric_present(metric_name, metric_types, gpu_id, parsed_metrics):
    """Return True if metric_name for gpu_id appears in parsed_metrics."""
    if 'labeled' in metric_types:
        if ':' not in metric_name:
            return True
        label_name = metric_types['labeled']['label']
        base_name, label_value = metric_name.split(':', 1)
        key = _prom_key(base_name)
        entries = parsed_metrics.get(key, parsed_metrics.get(base_name.lower(), []))
        if not entries:
            return False
        return any(
            e['labels'].get('gpu_id') == str(gpu_id) and
            e['labels'].get(label_name, '').lower() == label_value.lower()
            for e in entries
        )
    elif 'array' in metric_types:
        key = _prom_key(metric_name)
        entries = parsed_metrics.get(key, parsed_metrics.get(metric_name.lower(), []))
        if not entries:
            return False
        return any(e['labels'].get('gpu_id') == str(gpu_id) for e in entries)
    else:
        key = _prom_key(metric_name)
        entries = parsed_metrics.get(key, parsed_metrics.get(metric_name.lower(), []))
        if not entries:
            return False
        return any(e['labels'].get('gpu_id') == str(gpu_id) for e in entries)


def _parse_amd_smi_sample(raw_str):
    """Parse a raw amd-smi metric --json string, returning a list or dict."""
    pattern = r'("[^"]+")\s*:\s*"(\[.*?\])"'
    cleaned = re.sub(pattern, r'\1: \2', raw_str)
    return json.loads(cleaned)


def _amd_smi_val_for_gpu(amd_smi_json, gpu_id, path_str):
    """Extract a value from amd-smi JSON for a specific GPU index and dotted path."""
    path = path_str.split('.')
    if isinstance(amd_smi_json, list):
        if gpu_id >= len(amd_smi_json):
            return None
        return _extract_amd_smi_value(amd_smi_json[gpu_id], path)
    if isinstance(amd_smi_json, dict) and 'gpu_data' in amd_smi_json:
        data = amd_smi_json['gpu_data']
        if gpu_id >= len(data):
            return None
        return _extract_amd_smi_value(data[gpu_id], path)
    return None


def _compare_value(exporter_val, amd_smi_val):
    """
    Return (hit, miss) pair for one (exporter, amd-smi) value comparison.
    N/A amd-smi values are skipped (not counted as miss).
    Returns (None, None) when comparison cannot be made.
    """
    if isinstance(amd_smi_val, str) and amd_smi_val == 'N/A':
        return None, None
    if isinstance(amd_smi_val, dict):
        if amd_smi_val.get('value') == 'N/A':
            return None, None
        ref = float(amd_smi_val['value'])
    elif isinstance(amd_smi_val, (int, float)):
        ref = float(amd_smi_val)
    else:
        return None, None

    lo = int(0.95 * ref)
    hi = int(1.05 * ref)
    if ref == 0 and int(float(exporter_val)) == 0:
        return None, None  # zero-vs-zero: both broken or both idle — inconclusive
    if lo <= int(exporter_val) <= hi:
        return 1, 0
    return 0, 1


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def deploy_sriov_exporter_debian(gim_node, images, environment):
    """
    Install the SR-IOV exporter Debian package on the hypervisor node.

    Identical to the fixture in test_vf_metrics.py; duplicated here so this
    module can run standalone without importing from a sibling test file.
    """
    _GPU_AGENT_SERVICE = "gpuagent-sriov.service"
    _SRIOV_SERVICE     = "amd-metrics-exporter-sriov.service"
    _SRIOV_PKG_NAME    = "amdgpu-exporter-sriov"

    node = gim_node.node

    def _image_key(os_version: str) -> str:
        return f"sriov-exporter-debian-Ubuntu-{os_version}.debian"

    image_name = _image_key(gim_node.os_version)
    if image_name not in images:
        pytest.skip(
            f"No SR-IOV exporter debian for Ubuntu {gim_node.os_version} in image manifest"
        )

    local_deb  = images[image_name]
    remote_deb = f"/tmp/{os.path.basename(local_deb)}"
    node.run_command(f"rm -f {remote_deb}")

    Logger.info(f"Uploading {local_deb} → {node.ip_address}:{remote_deb}")
    K8Helper.triage(environment, node.put(local_deb, remote_deb),
                    f"Failed to upload {local_deb} to {remote_deb}")

    rc, _, stderr = node.run_command(f"sudo apt install -y {remote_deb}")
    K8Helper.triage(environment, rc == 0,
                    f"Failed to install {_SRIOV_PKG_NAME}: {stderr}")

    rc, _, stderr = node.run_command(f"sudo systemctl enable --now {_GPU_AGENT_SERVICE}")
    K8Helper.triage(environment, rc == 0,
                    f"Failed to enable/start {_GPU_AGENT_SERVICE}: {stderr}")

    rc, _, stderr = node.run_command(f"sudo systemctl enable --now {_SRIOV_SERVICE}")
    K8Helper.triage(environment, rc == 0,
                    f"Failed to enable/start {_SRIOV_SERVICE}: {stderr}")

    rc, _, stderr = node.run_command(f"sudo systemctl is-active {_SRIOV_SERVICE}")
    K8Helper.triage(environment, rc == 0,
                    f"{_SRIOV_SERVICE} not active after start: {stderr}")
    yield

    Logger.info(f"Teardown: removing {_SRIOV_PKG_NAME} from {node.ip_address}")
    node.run_command(f"sudo systemctl stop {_SRIOV_SERVICE} || true")
    node.run_command(f"sudo systemctl stop {_GPU_AGENT_SERVICE} || true")
    rc, _, stderr = node.run_command(f"sudo dpkg -r {_SRIOV_PKG_NAME}")
    K8Helper.triage(environment, rc == 0,
                    f"Failed to remove {_SRIOV_PKG_NAME}: {stderr}")
    node.run_command(f"rm -f {remote_deb}")


@pytest.fixture(scope="module")
def metrics_samples(gim_node, hypervisor_node, vf_topology,
                    deploy_sriov_exporter_debian, environment):
    """
    Collect multi-sample metrics snapshots under idle and workload conditions.

    Manages the full workload lifecycle internally (independent of vm0_workload
    in test_vf_metrics.py).  Each condition collects _NUM_SAMPLES parallel
    snapshots of /metrics and amd-smi metric --json.

    Yields: (idle_samples, load_samples) — each is the dict returned by
    metric_util.collect_vf_samples().  Also stores 'gpu-series' in each dict.
    """
    node = gim_node.node
    amd_smi = gim_util.find_amd_smi(node)
    if amd_smi is None:
        pytest.skip("amd-smi not found on hypervisor — cannot collect accuracy samples")

    host_series = getattr(node, 'gpu_series', None)
    if not host_series:
        pytest.skip("gpu_series not set on gim_node — cannot determine metric catalog")
    gpu_series = host_series
    Logger.info(f"GPU series (host): {gpu_series}")

    logdir = environment.logdir

    # Idle phase
    Logger.info("Waiting 30s for exporter to stabilize before idle sampling")
    time.sleep(30)
    Logger.info(f"Collecting {_NUM_SAMPLES} idle samples (interval={_SAMPLE_INTERVAL}s)")
    idle_samples = metric_util.collect_vf_samples(
        node, amd_smi, _METRICS_PORT, _NUM_SAMPLES, _SAMPLE_INTERVAL, logdir, "idle"
    )
    idle_samples['gpu-series'] = gpu_series

    # Start workload
    vm0 = vf_topology.vm0
    try:
        vm_util.verify_amdgpu_in_vm(hypervisor_node, vm0)
    except Exception as e:
        pytest.skip(f"amdgpu not ready in VM: {e}")
    vm_util.vm_ensure_docker_image(hypervisor_node, vm0.ssh_port)
    render_devices = vm_util.vm_get_render_device_flags(hypervisor_node, vm0.ssh_port)
    script = vm_util.load_workload_script(_WORKLOAD_SPEC_TYPE)
    vm_util.vm_write_script(hypervisor_node, vm0.ssh_port, script, _WORKLOAD_SCRIPT)

    Logger.info(f"Starting {_WORKLOAD_SPEC_TYPE} workload in container on VM0 (port={vm0.ssh_port})")
    rc, _, stderr = vm_util.vm_run_command(
        hypervisor_node, vm0.ssh_port,
        f"nohup docker run --rm --name {vm_util._WORKLOAD_CONTAINER_NAME} "
        f"--device /dev/kfd {render_devices} "
        f"-e GLIBC_TUNABLES=glibc.cpu.hwcaps=-AVX_Fast_Unaligned_Load,-MOVDIRI,-MOVDIR64B,-AVX512F,-AVX512VL "
        f"-v {_WORKLOAD_SCRIPT}:{_WORKLOAD_SCRIPT} "
        f"{vm_util._WORKLOAD_CONTAINER} python3 {_WORKLOAD_SCRIPT} "
        f"> /tmp/workload.log 2>&1 &"
    )
    if rc != 0:
        pytest.skip(f"Failed to launch workload container in VM0: {stderr}")

    Logger.info(f"Workload container launched; waiting {_POST_START_WAIT}s for in-flight state")
    time.sleep(_POST_START_WAIT)

    alive_rc, _, _ = vm_util.vm_run_command(
        hypervisor_node, vm0.ssh_port,
        f"docker container ls --filter name={vm_util._WORKLOAD_CONTAINER_NAME} "
        f"| grep -q {vm_util._WORKLOAD_CONTAINER_NAME}"
    )
    if alive_rc != 0:
        _, log, _ = vm_util.vm_run_command(
            hypervisor_node, vm0.ssh_port, "cat /tmp/workload.log"
        )
        pytest.skip(
            f"Workload container exited prematurely — GPU inaccessible or ROCm init failed. "
            f"Log:\n{log}"
        )

    Logger.info(f"Waiting up to {_WORKLOAD_READY_TIMEOUT}s for workload to reach iteration 10")
    deadline = time.time() + _WORKLOAD_READY_TIMEOUT
    ready = False
    while time.time() < deadline:
        rc, out, _ = vm_util.vm_run_command(
            hypervisor_node, vm0.ssh_port,
            "grep -m1 'iteration 10' /tmp/workload.log 2>/dev/null"
        )
        if rc == 0 and out.strip():
            Logger.info(f"Workload reached iteration 10: {out.strip()}")
            ready = True
            break
        time.sleep(_SCRAPE_INTERVAL)
    if not ready:
        _, log, _ = vm_util.vm_run_command(
            hypervisor_node, vm0.ssh_port, "tail -20 /tmp/workload.log 2>/dev/null"
        )
        vm_util.vm_run_command(
            hypervisor_node, vm0.ssh_port,
            f"docker stop --time 5 {vm_util._WORKLOAD_CONTAINER_NAME} 2>/dev/null; "
            f"docker rm -f {vm_util._WORKLOAD_CONTAINER_NAME} 2>/dev/null || true"
        )
        pytest.skip(
            f"Workload did not reach iteration 10 within {_WORKLOAD_READY_TIMEOUT}s. "
            f"Last 20 lines of workload.log:\n{log}"
        )

    # Load phase
    Logger.info(f"Collecting {_NUM_SAMPLES} load samples (interval={_SAMPLE_INTERVAL}s)")
    load_samples = metric_util.collect_vf_samples(
        node, amd_smi, _METRICS_PORT, _NUM_SAMPLES, _SAMPLE_INTERVAL, logdir, "load"
    )
    load_samples['gpu-series'] = gpu_series

    yield idle_samples, load_samples

    Logger.info("Teardown: stopping workload container in VM0")
    vm_util.vm_run_command(
        hypervisor_node, vm0.ssh_port,
        f"docker stop --time 5 {vm_util._WORKLOAD_CONTAINER_NAME} 2>/dev/null; "
        f"docker rm -f {vm_util._WORKLOAD_CONTAINER_NAME} 2>/dev/null || true"
    )


# ---------------------------------------------------------------------------
# Parametrization
# ---------------------------------------------------------------------------

def pytest_generate_tests(metafunc):
    if 'metric_to_test' in metafunc.fixturenames:
        metrics = [
            e['name']
            for e in metric_util.get_supported_metrics(
                skip_profiler_metrics=True, deployment_mode="hypervisor"
            )
            if e.get('skip-validation', 'no') != 'yes'
        ]
        metafunc.parametrize('metric_to_test', metrics)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_vf_metric_completeness(gim_node, metrics_samples, metric_to_test, environment):
    """
    Each supported metric must be present in a sufficient fraction of samples
    across both idle and load conditions for gpu_id=0.

    Hard-fail: metric absent in ≥50% of total_samples for any GPU.
    Xfail:     metric absent in >1 but <50% of samples (transient dropout).
    Skip:      metric not applicable to the VF GPU series on this hardware.
    """
    idle_samples, load_samples = metrics_samples
    gpu_series = idle_samples['gpu-series']

    if not metric_util.is_metric_supported(
        metric_to_test, gpu_series,
        amdgpu_driver=getattr(gim_node.node, 'amdgpu_driver_version', None),
        dme_version=None
    ):
        pytest.skip(f"{metric_to_test} not supported for {gpu_series}")

    metric_metadata = metric_util.get_metric_metadata(metric_to_test)
    metric_types = metric_metadata.get('type', {})
    gpu_id = 0

    num_samples = idle_samples['num-samples']
    total_samples = num_samples * 2  # idle + load

    missing = []
    for sample_id in range(num_samples):
        idle_parsed = metric_util.parse_metric_data(idle_samples['exporter'][sample_id])
        if not _is_metric_present(metric_to_test, metric_types, gpu_id, idle_parsed):
            missing.append(('idle', sample_id))
            Logger.warning(f"{metric_to_test} gpu:{gpu_id} absent in idle sample {sample_id}")

        load_parsed = metric_util.parse_metric_data(load_samples['exporter'][sample_id])
        if not _is_metric_present(metric_to_test, metric_types, gpu_id, load_parsed):
            missing.append(('load', sample_id))
            Logger.warning(f"{metric_to_test} gpu:{gpu_id} absent in load sample {sample_id}")

    present = total_samples - len(missing)
    Logger.info(f"{metric_to_test} gpu:{gpu_id} present in {present}/{total_samples} samples")

    hard_fail = len(missing) * 2 >= total_samples
    transient  = len(missing) > 1 and not hard_fail

    if hard_fail or transient:
        Logger.error(
            f"Completeness: {metric_to_test} absent in {len(missing)}/{total_samples} samples: {missing}"
        )

    assert not hard_fail, (
        f"Completeness: {metric_to_test} absent in ≥50% of samples "
        f"({len(missing)}/{total_samples}): {missing}"
    )

    K8Helper.triage(environment, not transient,
                    f"Completeness: {metric_to_test} transient dropout — {missing}",
                    skip_techsupport=True, expected_to_fail=True)


def test_vf_metric_value_accuracy(gim_node, metrics_samples, metric_to_test, environment):
    """
    Cross-validate exporter values against amd-smi metric --json (±5% tolerance).

    For each sample pair (exporter[i], amd-smi[i]) where both are available:
      - Compute hit (within ±5%) or miss.
    Hard-fail: ≥1 effective comparison AND zero hits.
    Xfail:     <50% hit rate among effective comparisons.
    Skip:      metric has no amd-smi path (cannot cross-validate).
    """
    idle_samples, load_samples = metrics_samples
    gpu_series = idle_samples['gpu-series']

    if not metric_util.is_metric_supported(
        metric_to_test, gpu_series,
        amdgpu_driver=getattr(gim_node.node, 'amdgpu_driver_version', None),
        dme_version=None
    ):
        pytest.skip(f"{metric_to_test} not supported for {gpu_series}")

    metric_metadata = metric_util.get_metric_metadata(metric_to_test)
    metric_types = metric_metadata.get('type', {})

    gpu_support_info = metric_util.get_metric_support_info(metric_metadata, gpu_series,
                                                           deployment_mode="hypervisor")
    if gpu_support_info is None:
        pytest.skip(f"No gpu-support-info for {metric_to_test} on {gpu_series}")

    amd_smi_path = gpu_support_info.get('amd-smi', '')
    if not amd_smi_path:
        pytest.skip(f"{metric_to_test}: no amd-smi path — cross-validation not possible")

    gpu_id = 0

    def _analyze(samples, label):
        num_samples = samples['num-samples']
        hit, miss, missing = 0, 0, 0
        for sample_id in range(num_samples):
            parsed = metric_util.parse_metric_data(samples['exporter'][sample_id])
            try:
                raw_smi = samples['amd-smi'][sample_id]
                amd_smi_json = _parse_amd_smi_sample(raw_smi)
            except (IndexError, json.JSONDecodeError, KeyError) as exc:
                Logger.warning(f"{label} sample {sample_id}: amd-smi parse failed: {exc}")
                missing += 1
                continue

            if 'labeled' in metric_types and ':' in metric_to_test:
                label_name = metric_types['labeled']['label']
                base_name, label_value = metric_to_test.split(':', 1)
                entries = [
                    e for e in parsed.get(_prom_key(base_name), parsed.get(base_name.lower(), []))
                    if e['labels'].get('gpu_id') == str(gpu_id) and
                       e['labels'].get(label_name, '').lower() == label_value.lower()
                ]
                if not entries:
                    Logger.warning(f"{label} sample {sample_id}: {metric_to_test} absent")
                    missing += 1
                    continue
                for idx, entry in enumerate(entries):
                    path_str = amd_smi_path.format(partition_id=0, idx=idx)
                    smi_val = _amd_smi_val_for_gpu(amd_smi_json, gpu_id, path_str)
                    h, m = _compare_value(entry['value'], smi_val)
                    if h is None:
                        continue
                    hit += h; miss += m
                    Logger.debug(f"{label} s{sample_id} {metric_to_test}[{idx}]: "
                                 f"exp={entry['value']} smi={smi_val} {'HIT' if h else 'MISS'}")

            elif 'array' in metric_types:
                entries = [
                    e for e in parsed.get(_prom_key(metric_to_test), parsed.get(metric_to_test.lower(), []))
                    if e['labels'].get('gpu_id') == str(gpu_id)
                ]
                if not entries:
                    Logger.warning(f"{label} sample {sample_id}: {metric_to_test} absent")
                    missing += 1
                    continue
                path_str = amd_smi_path.format(partition_id=0)
                smi_list = _amd_smi_val_for_gpu(amd_smi_json, gpu_id, path_str)
                if not isinstance(smi_list, list):
                    missing += 1
                    continue
                for entry, smi_val in zip(entries, smi_list):
                    h, m = _compare_value(entry['value'], smi_val)
                    if h is None:
                        continue
                    hit += h; miss += m

            else:
                entries = [
                    e for e in parsed.get(_prom_key(metric_to_test), parsed.get(metric_to_test.lower(), []))
                    if e['labels'].get('gpu_id') == str(gpu_id)
                ]
                if len(entries) != 1:
                    Logger.warning(f"{label} sample {sample_id}: {metric_to_test} entry count {len(entries)}")
                    missing += 1
                    continue
                path_str = amd_smi_path.format(partition_id=0)
                smi_val = _amd_smi_val_for_gpu(amd_smi_json, gpu_id, path_str)
                if smi_val is None:
                    Logger.warning(f"{label} sample {sample_id}: amd-smi path missing")
                    missing += 1
                    continue
                h, m = _compare_value(entries[0]['value'], smi_val)
                if h is None:
                    continue
                hit += h; miss += m
                Logger.debug(f"{label} s{sample_id} {metric_to_test}: "
                             f"exp={entries[0]['value']} smi={smi_val} {'HIT' if h else 'MISS'}")

        effective = hit + miss
        Logger.info(f"{label} gpu:{gpu_id} — hit:{hit} miss:{miss} missing:{missing} effective:{effective}")
        return hit, miss, missing, effective

    idle_hit, idle_miss, idle_missing, idle_eff = _analyze(idle_samples, "IDLE")
    load_hit, load_miss, load_missing, load_eff = _analyze(load_samples, "LOAD")

    if idle_eff == 0 and load_eff == 0:
        pytest.skip(
            f"{metric_to_test}: no comparable samples in all "
            f"{idle_samples['num-samples'] * 2} idle+load samples — "
            f"amd-smi N/A or exporter absent in every sample"
        )

    assert idle_eff == 0 or idle_hit >= 1, (
        f"IDLE {metric_to_test} gpu:{gpu_id}: zero hits among {idle_eff} effective comparisons "
        f"(miss={idle_miss}, missing={idle_missing})"
    )
    assert load_eff == 0 or load_hit >= 1, (
        f"LOAD {metric_to_test} gpu:{gpu_id}: zero hits among {load_eff} effective comparisons "
        f"(miss={load_miss}, missing={load_missing})"
    )

    K8Helper.triage(environment, (idle_eff == 0 or idle_hit >= int(0.50 * idle_eff)),
                    f"IDLE {metric_to_test} gpu:{gpu_id} <50% hit rate "
                    f"hit:{idle_hit} miss:{idle_miss} missing:{idle_missing}",
                    skip_techsupport=True, expected_to_fail=True)
    K8Helper.triage(environment, (load_eff == 0 or load_hit >= int(0.50 * load_eff)),
                    f"LOAD {metric_to_test} gpu:{gpu_id} <50% hit rate "
                    f"hit:{load_hit} miss:{load_miss} missing:{load_missing}",
                    skip_techsupport=True, expected_to_fail=True)


def test_vf_metric_nonzero_in_samples(gim_node, metrics_samples, metric_to_test, environment):
    """
    Guard against 'all-zero nirvana': at least one idle or load sample must
    report a non-zero value for this metric on gpu_id=0.

    A metric reporting 0 in every sample may indicate broken metric collection
    rather than genuine quiescence.  Metrics that legitimately stay at 0 under
    all conditions (error counters, inactive links) should be marked
    skip-validation: 'yes' in metrics-support.json to opt out of this check.

    Hard-fail: all idle+load samples report 0 for gpu_id=0.
    Skip: metric not supported for this GPU series or no samples collected.
    """
    idle_samples, load_samples = metrics_samples
    gpu_series = idle_samples['gpu-series']

    if not metric_util.is_metric_supported(
        metric_to_test, gpu_series,
        amdgpu_driver=getattr(gim_node.node, 'amdgpu_driver_version', None),
        dme_version=None
    ):
        pytest.skip(f"{metric_to_test} not supported for {gpu_series}")

    metric_metadata = metric_util.get_metric_metadata(metric_to_test)
    metric_types = metric_metadata.get('type', {})
    gpu_id = 0

    max_val = 0.0
    all_raw = (
        [(s, 'idle') for s in idle_samples['exporter']] +
        [(s, 'load') for s in load_samples['exporter']]
    )

    for raw, _phase in all_raw:
        parsed = metric_util.parse_metric_data(raw)
        if 'labeled' in metric_types and ':' in metric_to_test:
            label_name = metric_types['labeled']['label']
            base_name, label_value = metric_to_test.split(':', 1)
            key = _prom_key(base_name)
            entries = [
                e for e in parsed.get(key, parsed.get(base_name.lower(), []))
                if e['labels'].get('gpu_id') == str(gpu_id) and
                   e['labels'].get(label_name, '').lower() == label_value.lower()
            ]
        else:
            base = metric_to_test.split(':')[0]
            key = _prom_key(base)
            entries = [
                e for e in parsed.get(key, parsed.get(base.lower(), []))
                if e['labels'].get('gpu_id') == str(gpu_id)
            ]
        for e in entries:
            try:
                v = float(e['value'])
                if v > max_val:
                    max_val = v
            except (ValueError, TypeError):
                pass

    assert max_val > 0, (
        f"{metric_to_test} gpu:{gpu_id} reported 0 in all {len(all_raw)} samples "
        f"(idle+load) — possible broken metric collection. "
        f"Add skip-validation: 'yes' to metrics-support.json if this metric is legitimately always 0."
    )


def test_vf_metric_coverage(gim_node, metrics_samples, environment):
    """
    Verify no metric exported by the SR-IOV exporter is absent from metrics-support.json.

    Uses the first idle sample.  Fails if any amd_* metric in the Prometheus output
    is not tracked in the supported metrics catalog.
    """
    idle_samples, _ = metrics_samples
    gpu_series = idle_samples['gpu-series']

    if not idle_samples['exporter']:
        pytest.skip("No exporter samples collected — cannot check coverage")

    parsed = metric_util.parse_metric_data(idle_samples['exporter'][0])
    untracked = metric_util.find_untracked_metrics(
        parsed,
        gpu_series=gpu_series,
        skip_profiler_metrics=True,
        deployment_mode="hypervisor",
    )
    if untracked:
        Logger.warning(f"Untracked metrics ({len(untracked)}): {sorted(untracked)}")

    assert not untracked, (
        f"SR-IOV exporter exports {len(untracked)} metric(s) not in metrics-support.json: "
        f"{sorted(untracked)}"
    )
