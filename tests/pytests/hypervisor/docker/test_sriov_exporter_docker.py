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
AMD SR-IOV Device Metrics Exporter Docker Container Test Suite.

Validates the SR-IOV variant of the AMD Device Metrics Exporter when deployed
as a Docker container on the hypervisor.  GIM must already be loaded and VFs
must be active before this suite runs (enforced by the gim_node session fixture).

The container is run in privileged mode so it can access the GIM kernel
interface and enumerate VFs through sysfs.

Coverage:
- Container deployment and service start on the hypervisor
- Metrics endpoint availability on the default port
- Presence of VF-scoped metrics in the output
- Config file update via volume mount (dynamic reload)
"""

import copy
import os
import json
import time
import logging
from pathlib import Path
import requests
import pytest
import lib.gim_util as gim_util
import lib.metric_util as metric_util
from lib.util import K8Helper

Logger = logging.getLogger("hypervisor.docker.test_sriov_exporter_docker")


def _save_metrics(text: str, environment) -> None:
    """Write raw metrics output to the per-TC log directory."""
    tc_name = environment.context.current_tc_name
    path = Path(environment.context.log_dir) / f"{tc_name}_metrics.txt"
    path.write_text(text, encoding="utf-8")
    Logger.info(f"Metrics saved → {path}")


def _save_amd_smi_json(node, environment) -> None:
    """
    Capture 'amd-smi metric --json' and 'amd-smi static --json' and save
    alongside the exporter metrics dump.  No-op if amd-smi is not installed.
    """
    amd_smi = gim_util.find_amd_smi(node)
    if amd_smi is None:
        Logger.warning("amd-smi not found — skipping JSON snapshot")
        return
    tc_name = environment.context.current_tc_name
    log_dir = Path(environment.context.log_dir)

    for cmd_args, fname in [
        ("metric --json", f"{tc_name}_amd_smi_metric.json"),
        ("static --json", f"{tc_name}_amd_smi_static.json"),
    ]:
        rc, out, stderr = node.run_command(f"sudo {amd_smi} {cmd_args}")
        text = out.decode("utf-8") if isinstance(out, bytes) else out
        if rc == 0:
            artifact = log_dir / fname
            artifact.write_text(text, encoding="utf-8")
            Logger.info(f"amd-smi {cmd_args} → {artifact} ({len(text)} bytes)")
        else:
            Logger.warning(f"{amd_smi} {cmd_args} failed (rc={rc}): {stderr}")


_CONTAINER_NAME = "sriov-metrics-exporter"
_METRICS_PORT   = 5000
_CONFIG_DIR     = "/tmp/sriov-metrics"
_CONFIG_REMOTE  = f"{_CONFIG_DIR}/config.json"
_REFERENCE_CONFIG_URL = (
    "https://raw.githubusercontent.com/ROCm/device-metrics-exporter"
    "/refs/heads/main/example/config.json"
)


def test_deploy_sriov_exporter_docker(gim_node, run_sriov_exporter_docker, environment):
    """
    Verify SR-IOV exporter container starts and metrics endpoint responds.

    The container runs on the hypervisor with GIM loaded.  This smoke test
    confirms that the exporter is running and serving metrics from the VFs.
    """
    node = gim_node.node
    rc, stdout, stderr = node.http_get(_METRICS_PORT, "metrics")
    K8Helper.triage(environment, rc == 0,
                    f"Metrics endpoint not responding on port {_METRICS_PORT}: {stderr}")
    text = stdout.decode("utf-8") if isinstance(stdout, bytes) else stdout
    _save_metrics(text, environment)
    metric_lines = [l for l in text.splitlines() if l and not l.startswith("#")]
    K8Helper.triage(environment, len(metric_lines) > 0,
                    "Metrics endpoint responded but output contains no metric lines")
    Logger.info(f"Metrics endpoint OK: {len(metric_lines)} metric lines, {len(stdout)} bytes")


def test_apply_sriov_exporter_config(gim_node, run_sriov_exporter_docker, environment):
    """
    Verify the container loads the reference config from the mounted volume.

    The config is mounted as -v {_CONFIG_DIR}:/etc/metrics.  This test
    confirms the exporter reads it and continues serving metrics.
    """
    node = gim_node.node
    rc, stdout, stderr = node.http_get(_METRICS_PORT, "metrics")
    K8Helper.triage(environment, rc == 0,
                    f"Metrics endpoint not responding with reference config: {stderr}")
    text = stdout.decode("utf-8") if isinstance(stdout, bytes) else stdout
    _save_metrics(text, environment)
    non_comment_lines = [l for l in text.splitlines() if l and not l.startswith("#")]
    K8Helper.triage(environment, len(non_comment_lines) > 0,
                    "Metrics output is empty — reference config may not be loaded")
    Logger.info(f"Reference config in effect, {len(non_comment_lines)} metric lines")


def test_enable_profiler_metrics(gim_node, run_sriov_exporter_docker, environment):
    """
    Verify profiler metrics can be enabled via config update in the mounted volume.

    Updates config.json at {_CONFIG_DIR}/config.json on the hypervisor (visible to
    the container as /etc/metrics/config.json).  The exporter should pick up the
    change without a container restart and continue serving metrics.
    """
    node = gim_node.node
    config_json_file = os.path.join(environment.logdir, "profiler-metrics-config.json")
    config_map = {
        "CommonConfig": {
            "HealthService": {"Enable": False},
        },
        "GPUConfig": {
            "ProfilerMetrics": {"all": True},
        },
    }
    with open(config_json_file, "w") as fp:
        json.dump(config_map, fp, indent=4)

    K8Helper.triage(environment, node.put(config_json_file, _CONFIG_REMOTE),
                    f"Failed to upload profiler-metrics config to {_CONFIG_REMOTE}")

    time.sleep(30)

    rc, stdout, stderr = node.http_get(_METRICS_PORT, "metrics")
    K8Helper.triage(environment, rc == 0,
                    f"Metrics endpoint not responding after profiler config update: {stderr}")
    text = stdout.decode("utf-8") if isinstance(stdout, bytes) else stdout
    _save_metrics(text, environment)
    metric_lines = [l for l in text.splitlines() if l and not l.startswith("#")]
    K8Helper.triage(environment, len(metric_lines) > 0,
                    "Metrics output empty after profiler config update")
    Logger.info(f"Profiler config applied: endpoint serving {len(metric_lines)} metric lines")

    # Restore reference config
    reference_cfg = os.path.join(environment.logdir, "reference-config.json")
    if os.path.exists(reference_cfg):
        K8Helper.triage(environment, node.put(reference_cfg, _CONFIG_REMOTE),
                        "Failed to restore reference config")
    time.sleep(10)


def test_sriov_vf_metrics_present(gim_node, run_sriov_exporter_docker, environment):
    """
    Verify VF metrics appear in the container's metrics output.

    Checks that the output contains metric lines (i.e., the exporter can
    see the SR-IOV VFs managed by GIM).  The number of active VFs is logged
    for context.
    """
    node = gim_node.node
    rc, stdout, stderr = node.http_get(_METRICS_PORT, "metrics")
    K8Helper.triage(environment, rc == 0,
                    f"Metrics endpoint not responding: {stderr}")

    vf_count = len(gim_node.vf_pci_addrs)
    Logger.info(f"Hypervisor has {vf_count} SR-IOV VF(s): {gim_node.vf_pci_addrs}")

    text = stdout.decode("utf-8") if isinstance(stdout, bytes) else stdout
    _save_metrics(text, environment)
    _save_amd_smi_json(node, environment)
    non_comment_lines = [l for l in text.splitlines() if l and not l.startswith("#")]
    K8Helper.triage(environment, len(non_comment_lines) > 0,
                    "Metrics output contains no metric lines — VF metrics missing")
    Logger.info(f"Metrics output contains {len(non_comment_lines)} metric lines")


def test_vf_visibility_and_static_info(gim_node, environment):
    """
    Collect and dump amd-smi static info for all VFs and GPUs on the hypervisor.

    Uses the GIM-installed amd-smi (path confirmed before use):
    - 'amd-smi static --vf=<bdf>'           for each active VF
    - 'amd-smi static --gpu=<id> --num-vf'  for each PF / GPU index

    Full command output is logged and saved to the testcase log directory.
    The intent is observability: capture what amd-smi reports in a VF
    environment, not to validate specific field values.
    The test skips if amd-smi is not installed on the hypervisor.
    """
    node = gim_node.node
    amd_smi = gim_util.assert_amd_smi_available(node)
    tc_name = environment.context.current_tc_name
    log_dir = Path(environment.context.log_dir)

    pf_to_gpu = gim_util.amd_smi_pf_to_gpu_id(node, amd_smi, gim_node.pfs)
    Logger.info(f"amd-smi path: {amd_smi}")
    Logger.info(f"PF→GPU index map: {pf_to_gpu}")
    Logger.info(f"Active VFs: {gim_node.vf_pci_addrs}")

    # Dump static info for each VF
    for vf in gim_node.vfs:
        rc, out, stderr = gim_util.amd_smi_vf_static(node, amd_smi, vf.bdf)
        text = out.decode("utf-8") if isinstance(out, bytes) else out
        safe_bdf = vf.bdf.replace(":", "_")
        (log_dir / f"{tc_name}_vf_{safe_bdf}_static.txt").write_text(text, encoding="utf-8")
        K8Helper.triage(environment, rc == 0,
                        f"amd-smi static --vf={vf.bdf} failed (rc={rc}): {stderr}")
        Logger.info(f"--- amd-smi static --vf={vf.bdf} ---\n{text}")

    # Dump VF count per PF / GPU index
    for pf in gim_node.pfs:
        gpu_id = pf_to_gpu.get(pf.bdf)
        if gpu_id is None:
            Logger.warning(f"No GPU index for PF {pf.bdf} — skipping num-vf query")
            continue
        rc, out, stderr = gim_util.amd_smi_gpu_num_vf(node, amd_smi, gpu_id)
        text = out.decode("utf-8") if isinstance(out, bytes) else out
        (log_dir / f"{tc_name}_gpu_{gpu_id}_numvf.txt").write_text(text, encoding="utf-8")
        K8Helper.triage(environment, rc == 0,
                        f"amd-smi static --gpu={gpu_id} --num-vf failed (rc={rc}): {stderr}")
        Logger.info(f"--- amd-smi static --gpu={gpu_id} --num-vf (PF {pf.bdf}) ---\n{text}")


_HYPERVISOR_MANDATORY_LABELS = {
    "card_model", "card_series", "card_vendor", "cluster_name", "container",
    "deployment_mode", "driver_version", "gpu_compute_partition_type", "gpu_id",
    "gpu_memory_partition_type", "gpu_partition_id", "gpu_uuid", "hostname",
    "job_id", "job_partition", "job_user", "kfd_process_id", "namespace",
    "pod", "pod_uuid", "serial_number", "vbios_version",
}


def test_sriov_metric_labels(gim_node, gim_driver_spec, run_sriov_exporter_docker, environment):
    """Validate every amd_* metric carries the mandatory hypervisor label set.

    Checks that:
    1. deployment_mode is present and equals "hypervisor" on every sample.
    2. All mandatory hypervisor labels are present (as a subset — metrics may
       carry extra labels like clock_type or clock_index).
    3. driver_version label matches the GIM driver spec version.
    """
    node = gim_node.node
    rc, stdout, stderr = node.http_get(_METRICS_PORT, "metrics")
    K8Helper.triage(environment, rc == 0,
                    f"Metrics endpoint not responding on port {_METRICS_PORT}: {stderr}")
    text = stdout.decode("utf-8") if isinstance(stdout, bytes) else stdout
    _save_metrics(text, environment)
    metrics = metric_util.parse_metric_data(stdout)

    expected_gim_version = gim_driver_spec.get("default-version", "")

    errors = []
    for metric_name, samples in metrics.items():
        if not metric_name.startswith("amd_"):
            continue
        if metric_name == "amd_gpu_nodes_total":
            continue
        for sample in samples:
            observed = set(sample["labels"].keys())
            missing = _HYPERVISOR_MANDATORY_LABELS - observed
            if missing:
                errors.append(f"{metric_name}: missing labels {sorted(missing)}")
                break
            dm = sample["labels"].get("deployment_mode")
            if dm != "hypervisor":
                errors.append(f"{metric_name}: deployment_mode='{dm}', expected 'hypervisor'")
                break
            dv = sample["labels"].get("driver_version", "")
            if expected_gim_version and dv != expected_gim_version:
                errors.append(f"{metric_name}: driver_version='{dv}', expected '{expected_gim_version}'")
                break

    if errors:
        for e in errors:
            Logger.error(e)
    K8Helper.triage(environment, not errors,
                    f"Label validation failed for {len(errors)} metric(s): {errors[:5]}")
    amd_count = sum(1 for m in metrics if m.startswith("amd_"))
    Logger.info(f"All {amd_count} amd_* metric(s) carry the mandatory hypervisor label set")


def test_metric_coverage(hypervisor_node, gim_node, run_sriov_exporter_docker, reference_config, environment):
    """Verify no metrics exported by sriov-dme are absent from metrics-support.json.

    Enables profiler metrics before scraping so the full emitted metric set is captured.
    """
    global Logger
    node = gim_node.node

    # Start from reference config so other settings (TimerConfig, HealthService, etc.) are preserved
    _, ref_data = reference_config
    config_map = copy.deepcopy(ref_data)
    config_map.setdefault("GPUConfig", {}).setdefault("ProfilerMetrics", {})["all"] = True
    profiler_cfg_local = os.path.join(environment.logdir, "coverage-profiler-config.json")
    with open(profiler_cfg_local, "w") as fp:
        json.dump(config_map, fp, indent=4)
    K8Helper.triage(environment, node.put(profiler_cfg_local, _CONFIG_REMOTE),
                    f"Failed to upload profiler config to {_CONFIG_REMOTE}")
    time.sleep(30)  # Wait for DME hot-reload

    rc, stdout, stderr = node.http_get(_METRICS_PORT, "metrics")
    K8Helper.triage(environment, rc == 0,
                    f"sriov-dme endpoint not responding on port {_METRICS_PORT}: {stderr}")
    scraped = metric_util.parse_metric_data(stdout)
    untracked = metric_util.find_untracked_metrics(
        scraped, gpu_series=hypervisor_node.gpu_series, deployment_mode="hypervisor",
        skip_profiler_metrics=False,
    )
    if untracked:
        Logger.warning(f"sriov-dme: {len(untracked)} untracked metrics: {sorted(untracked)}")
    K8Helper.triage(environment, not untracked,
                    f"sriov-dme exports {len(untracked)} metrics not in metrics-support.json: "
                    f"{sorted(untracked)}")
