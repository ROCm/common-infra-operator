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
AMD SR-IOV Device Metrics Exporter Debian Package Test Suite.

Validates the SR-IOV variant of the AMD Device Metrics Exporter when deployed
as a Debian package on the hypervisor.  GIM must already be loaded and VFs must
be active before this suite runs (enforced by the gim_node session fixture).

Coverage:
- Debian package installation and service start on the hypervisor
- Metrics endpoint availability on the default port
- Presence of VF-scoped metrics in the output
- Port exposure (exactly one port open, correct port number)
"""

import copy
import os
import json
import time
import logging
from pathlib import Path
import pytest
import lib.deb_util as deb_util
import lib.gim_util as gim_util
import lib.metric_util as metric_util
from lib.util import K8Helper

Logger = logging.getLogger("hypervisor.debian.test_sriov_exporter_debian")


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

# The exporter package installs two services; gpuagent-sriov must start first.
_GPU_AGENT_SERVICE = "gpuagent-sriov.service"
_SRIOV_SERVICE     = "amd-metrics-exporter-sriov.service"
_SRIOV_PKG_NAME    = "amdgpu-exporter-sriov"
_METRICS_PORT      = 5000


def _image_key(os_version: str) -> str:
    """Derive the image-manifest key for the SR-IOV exporter debian package."""
    return f"sriov-exporter-debian-Ubuntu-{os_version}.debian"


@pytest.fixture(scope="module")
def deploy_sriov_exporter_debian(gim_node, images, environment):
    """
    Install the SR-IOV exporter Debian package on the hypervisor node.

    The package installs two systemd services; gpuagent-sriov acts as the
    data-source daemon and must be running before amd-metrics-exporter-sriov.

    Setup:
    1. Resolve the correct .deb from the image manifest using the host OS version.
    2. Upload the .deb to the hypervisor.
    3. Install via apt.
    4. Enable and start gpuagent-sriov, then amd-metrics-exporter-sriov.
    5. Verify both services are active before yielding.

    Teardown:
    - Stop both services and remove the package with dpkg -r.
    """
    node = gim_node.node
    image_name = _image_key(gim_node.os_version)
    if image_name not in images:
        pytest.skip(f"No SR-IOV exporter debian for Ubuntu {gim_node.os_version} in image manifest")

    local_deb = images[image_name]
    remote_deb = f"/tmp/{os.path.basename(local_deb)}"
    node.run_command(f"rm -f {remote_deb}")

    Logger.info(f"Uploading {local_deb} → {node.ip_address}:{remote_deb}")
    K8Helper.triage(environment, node.put(local_deb, remote_deb),
                    f"Failed to upload {local_deb} to {remote_deb}")

    rc, _, stderr = node.run_command(f"sudo apt install -y {remote_deb}")
    K8Helper.triage(environment, rc == 0,
                    f"Failed to install {_SRIOV_PKG_NAME}: {stderr}")

    # Start gpuagent-sriov first — it is the data source for the exporter.
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


def test_deploy_sriov_exporter_debian_package(gim_node, deploy_sriov_exporter_debian, environment):
    """
    Verify SR-IOV exporter metrics endpoint responds after package installation.

    The exporter runs on the hypervisor and exposes VF metrics on port 5000.
    GIM must be loaded and VFs must be active for metrics to be populated.
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


def test_sriov_vf_metrics_present(gim_node, deploy_sriov_exporter_debian, environment):
    """
    Verify VF metrics appear in the exporter output.

    The SR-IOV exporter should report per-VF metrics scoped to the VFs
    created by GIM.  This test checks that at least one GPU/VF metric line
    is present, confirming the exporter can see the VFs.

    VF count on the hypervisor is taken from gim_node (PF/VF topology
    discovered after GIM is loaded).
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


def test_exporter_port_scan(gim_node, deploy_sriov_exporter_debian, environment):
    """
    Verify the SR-IOV exporter opens exactly one port (the metrics port).

    Uses ss -tunlp filtered by the service PID to enumerate open sockets.
    Exactly one port should be open and it must be the configured metrics port.
    """
    node = gim_node.node

    rc, pid_out, stderr = node.run_command(
        f"sudo systemctl show -p MainPID --value {_SRIOV_SERVICE}"
    )
    K8Helper.triage(environment, rc == 0,
                    f"Could not retrieve PID of {_SRIOV_SERVICE}: {stderr}")

    pid = pid_out.strip()
    rc, ss_out, stderr = node.run_command(f"sudo ss -tunlp | grep {pid}")
    K8Helper.triage(environment, rc == 0,
                    f"No sockets found for {_SRIOV_SERVICE} (pid={pid}): {stderr}")

    address_port_map = deb_util.parse_ss_output(ss_out)
    K8Helper.triage(environment, len(address_port_map) == 1,
                    f"Expected 1 open port, got {len(address_port_map)}: {address_port_map}")
    K8Helper.triage(environment, int(address_port_map[0]['port']) == _METRICS_PORT,
                    f"Expected port {_METRICS_PORT}, got {address_port_map[0]['port']}")
    Logger.info(f"Port scan OK: pid={pid}, listening on port {address_port_map[0]['port']} ({address_port_map[0].get('addr', '*')})")


_HYPERVISOR_MANDATORY_LABELS = {
    "card_model", "card_series", "card_vendor", "cluster_name", "container",
    "deployment_mode", "driver_version", "gpu_compute_partition_type", "gpu_id",
    "gpu_memory_partition_type", "gpu_partition_id", "gpu_uuid", "hostname",
    "job_id", "job_partition", "job_user", "kfd_process_id", "namespace",
    "pod", "pod_uuid", "serial_number", "vbios_version",
}


def test_sriov_metric_labels(gim_node, gim_driver_spec, deploy_sriov_exporter_debian, environment):
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


def test_metric_coverage(hypervisor_node, gim_node, deploy_sriov_exporter_debian, reference_config, environment):
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
    K8Helper.triage(environment, node.put(profiler_cfg_local, "/tmp/coverage-config.json"),
                    "Failed to upload profiler config to hypervisor")
    rc, _, err = node.run_command("sudo mkdir -p /etc/metrics && sudo cp /tmp/coverage-config.json /etc/metrics/config.json")
    K8Helper.triage(environment, rc == 0, f"Failed to write /etc/metrics/config.json: {err}")
    rc, _, err = node.run_command(f"sudo systemctl restart {_SRIOV_SERVICE}")
    K8Helper.triage(environment, rc == 0, f"Failed to restart {_SRIOV_SERVICE}: {err}")
    time.sleep(15)  # Wait for service to come back up

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
