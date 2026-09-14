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
VF Activity Metrics Test Suite — Debian Package Deployment.

Validates that the SR-IOV exporter correctly reports per-VF activity metrics
under workload and idle conditions.  Requires an active VF topology (SPX or
CPX) launched by the session-scoped vf_topology fixture.

Coverage:
- amd_gpu_gfx_activity > 0 on VF0 during rocm-pytorch-gemm-stress workload
- amd_gpu_umc_activity > 0 on VF0 during workload
- amd_gpu_used_vram > 0 on VF0 during workload
- gfx and umc activity return below 5% after workload exits
- CPX isolation: VF1 remains idle while VF0 is under load (no bleed-over)

The exporter is deployed by the module-scoped deploy_sriov_exporter_debian
fixture imported from the hypervisor/debian test suite.  The vf_topology
fixture is session-scoped and shared across all test_vf_metrics tests.

GPUOP-924: gfx and umc activity readings may show zero during workload on some
firmware revisions.  Affected tests carry inline comments; they pass when the
hardware reports non-zero values as expected.
"""

import os
import time
import logging

import pytest

import lib.metric_util as metric_util
import lib.vm_util as vm_util
from lib.util import K8Helper

Logger = logging.getLogger("hypervisor.debian.test_vf_metrics")

_METRICS_PORT        = 5000
_WORKLOAD_SPEC_TYPE  = "rocm-pytorch-gemm-stress"
_WORKLOAD_SCRIPT     = "/tmp/gemm_stress.py"
_POST_START_WAIT     = 10   # seconds; ROCm HIP context init inside container needs a moment
_SCRAPE_INTERVAL     = 5    # seconds between scrape retries
_SCRAPE_RETRIES      = 12   # 12 × 5 s = 60 s total retry budget
_WORKLOAD_READY_TIMEOUT = 120  # seconds to wait for workload to reach iteration 10
_IDLE_WAIT           = 15   # seconds after workload exits before scraping
_GFX_IDLE_THRESHOLD  = 5.0  # percent
_UMC_IDLE_THRESHOLD  = 5.0  # percent
_VRAM_IDLE_BYTES     = 1_073_741_824  # 1 GiB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _scrape_vf_metric(node, metric_name: str, gpu_id: int,
                      retries: int = _SCRAPE_RETRIES,
                      interval: int = _SCRAPE_INTERVAL) -> float:
    """
    Poll the SR-IOV exporter until a non-zero sample is observed for
    *metric_name* filtered by gpu_id=<gpu_id>, or exhaust the retry budget.

    Returns the first non-zero float value observed.
    Raises AssertionError if all retries return zero or the metric is absent.
    """
    label_filter = {"gpu_id": str(gpu_id)}
    last_val = None
    for attempt in range(retries):
        metrics = metric_util.scrape_sriov_metrics(node, port=_METRICS_PORT)
        try:
            val = metric_util.get_sriov_metric_value(metrics, metric_name, label_filter)
            last_val = val
            Logger.info(
                f"[attempt {attempt+1}/{retries}] {metric_name}{{gpu_id={gpu_id}}} = {val}"
            )
            if val > 0:
                return val
        except AssertionError:
            Logger.debug(
                f"[attempt {attempt+1}/{retries}] {metric_name} not yet present "
                f"for gpu_id={gpu_id}"
            )
        if attempt < retries - 1:
            time.sleep(interval)

    raise AssertionError(
        f"{metric_name}{{gpu_id={gpu_id}}} never exceeded 0 after "
        f"{retries} retries (last observed value: {last_val})"
    )


def _scrape_vf_metric_latest(node, metric_name: str, gpu_id: int) -> float:
    """Return the current value of *metric_name* for gpu_id (no retry)."""
    label_filter = {"gpu_id": str(gpu_id)}
    metrics = metric_util.scrape_sriov_metrics(node, port=_METRICS_PORT)
    return metric_util.get_sriov_metric_value(metrics, metric_name, label_filter)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def deploy_sriov_exporter_debian(gim_node, images, environment):
    """
    Install the SR-IOV exporter Debian package on the hypervisor node.

    Identical to the fixture in test_sriov_exporter_debian.py; duplicated
    here so this module can run standalone without importing from a sibling
    test file.  Module scope: installed once for all test_vf_metrics tests.
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
def vm0_workload(vf_topology, gim_node, hypervisor_node, deploy_sriov_exporter_debian,
                 environment):
    """
    Start the rocm-pytorch-gemm-stress workload inside VM0.

    Launched once for the module; the workload keeps running until teardown.
    After launch the fixture waits _POST_START_WAIT seconds and then verifies
    the process is still alive — skips the module if it exited (e.g. torch not
    available or GPU not accessible inside the VM).
    """
    vm0 = vf_topology.vm0

    # Verify amdgpu is loaded and the VF was successfully probed (/dev/kfd
    # only exists after a successful probe).  This is a hard fail: if the
    # driver is absent every subsequent test would be silently skipped,
    # masking the failure in CI.
    vm_util.verify_amdgpu_in_vm(hypervisor_node, vm0)

    # Ensure the ROCm container image is cached in the VM (cold pull ~4 GB; warm = instant).
    vm_util.vm_ensure_docker_image(hypervisor_node, vm0.ssh_port)
    render_devices = vm_util.vm_get_render_device_flags(hypervisor_node, vm0.ssh_port)

    script = vm_util.load_workload_script(_WORKLOAD_SPEC_TYPE)
    vm_util.vm_write_script(hypervisor_node, vm0.ssh_port, script, _WORKLOAD_SCRIPT)

    Logger.info(f"Starting {_WORKLOAD_SPEC_TYPE} workload in container on VM0 (port={vm0.ssh_port})")
    rc, _, stderr = vm_util.vm_run_command(
        hypervisor_node, vm0.ssh_port,
        f"nohup docker run --rm --name {vm_util._WORKLOAD_CONTAINER_NAME} "
        f"--device /dev/kfd {render_devices} "
        f"-v {_WORKLOAD_SCRIPT}:{_WORKLOAD_SCRIPT} "
        f"{vm_util._WORKLOAD_CONTAINER} python3 {_WORKLOAD_SCRIPT} "
        f"> /tmp/workload.log 2>&1 &"
    )
    if rc != 0:
        pytest.fail(f"Failed to launch workload container in VM0: {stderr}")

    Logger.info(f"Workload container launched; waiting {_POST_START_WAIT}s for in-flight state")
    time.sleep(_POST_START_WAIT)

    # Confirm the container is still running.  If the GPU is inaccessible the
    # script exits immediately and Docker removes the container (--rm).
    alive_rc, _, _ = vm_util.vm_run_command(
        hypervisor_node, vm0.ssh_port,
        f"docker container ls --filter name={vm_util._WORKLOAD_CONTAINER_NAME} "
        f"| grep -q {vm_util._WORKLOAD_CONTAINER_NAME}"
    )
    if alive_rc != 0:
        _, log, _ = vm_util.vm_run_command(
            hypervisor_node, vm0.ssh_port, "cat /tmp/workload.log"
        )
        pytest.fail(
            f"Workload container exited prematurely — GPU inaccessible or ROCm init failed. "
            f"Log:\n{log}"
        )

    # Wait until the workload has completed at least 10 GEMM iterations, which
    # means torch init, HIP context setup, and kernel JIT are all done and the
    # GPU is genuinely computing.  This is a stronger readiness signal than the
    # pgrep check above, which only confirms the process is alive.
    Logger.info(
        f"Waiting up to {_WORKLOAD_READY_TIMEOUT}s for workload to reach iteration 10..."
    )
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
        pytest.fail(
            f"Workload did not reach iteration 10 within {_WORKLOAD_READY_TIMEOUT}s. "
            f"Last 20 lines of workload.log:\n{log}"
        )

    metric_util.collect_vf_workload_diagnostics(
        gim_node.node, environment, metrics_port=_METRICS_PORT, tag="workload_baseline"
    )

    yield

    Logger.info("Teardown: stopping workload container in VM0")
    vm_util.vm_run_command(
        hypervisor_node, vm0.ssh_port,
        f"docker stop --time 5 {vm_util._WORKLOAD_CONTAINER_NAME} 2>/dev/null; "
        f"docker rm -f {vm_util._WORKLOAD_CONTAINER_NAME} 2>/dev/null || true"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_vf_gfx_activity_under_workload(
    gim_node, hypervisor_node, vf_topology, deploy_sriov_exporter_debian,
    vm0_workload, environment
):
    """
    amd_gpu_gfx_activity must be > 0 on VF0 while rocm-pytorch-gemm-stress is running.

    # GPUOP-924: pending re-validation (zero readings observed on some firmware revisions)
    """
    node = gim_node.node
    val = _scrape_vf_metric(node, "amd_gpu_gfx_activity", gpu_id=0)
    Logger.info(f"amd_gpu_gfx_activity (VF0) under workload = {val}")
    assert val > 0, (
        f"Expected amd_gpu_gfx_activity > 0 on VF0 during workload, got {val}. "
        "GPUOP-924: verify firmware version supports VF gfx activity reporting."
    )


def test_vf_umc_activity_under_workload(
    gim_node, hypervisor_node, vf_topology, deploy_sriov_exporter_debian,
    vm0_workload, environment
):
    """
    amd_gpu_umc_activity must be > 0 on VF0 while rocm-pytorch-gemm-stress is running.

    # GPUOP-924: pending re-validation (zero readings observed on some firmware revisions)
    """
    node = gim_node.node
    val = _scrape_vf_metric(node, "amd_gpu_umc_activity", gpu_id=0)
    Logger.info(f"amd_gpu_umc_activity (VF0) under workload = {val}")
    assert val > 0, (
        f"Expected amd_gpu_umc_activity > 0 on VF0 during workload, got {val}. "
        "GPUOP-924: verify firmware version supports VF umc activity reporting."
    )


@pytest.mark.skip(reason="GPU_USED_VRAM not supported in hypervisor mode (hypervisor-mode=no)")
def test_vf_used_vram_under_workload(
    gim_node, hypervisor_node, vf_topology, deploy_sriov_exporter_debian,
    vm0_workload, environment
):
    """
    amd_gpu_used_vram must be > 0 on VF0 while rocm-pytorch-gemm-stress is running.

    VRAM allocation is visible immediately upon workload start; a single scrape
    with retry is sufficient.
    """
    node = gim_node.node
    val = _scrape_vf_metric(node, "amd_gpu_used_vram", gpu_id=0)
    Logger.info(f"amd_gpu_used_vram (VF0) under workload = {val} bytes")
    assert val > 0, (
        f"Expected amd_gpu_used_vram > 0 on VF0 during workload, got {val} bytes"
    )


def test_vf_activity_no_bleed(
    gim_node, hypervisor_node, vf_topology, deploy_sriov_exporter_debian,
    vm0_workload, environment
):
    """
    CPX isolation: VF1 (idle witness) must remain below the idle threshold
    while VF0 is under workload load.

    Skipped automatically when the topology is SPX (single-VM mode), which
    has no VF1 witness.  Only meaningful with partition_profile=CPX.

    Must run BEFORE test_vf_activity_returns_to_idle because this test
    requires the workload to still be running on VF0.
    """
    if not vf_topology.is_cpx:
        pytest.skip("CPX topology required for no-bleed isolation test (set GIM_PARTITION_PROFILE=CPX)")

    node = gim_node.node

    # VF0 should be active (workload running), VF1 should be idle.
    vf0_gfx = _scrape_vf_metric(node, "amd_gpu_gfx_activity", gpu_id=0)
    vf1_gfx = _scrape_vf_metric_latest(node, "amd_gpu_gfx_activity", gpu_id=1)

    Logger.info(
        f"CPX no-bleed check: VF0 gfx={vf0_gfx}%, VF1 gfx={vf1_gfx}%"
    )
    assert vf0_gfx > 0, (
        f"VF0 (workload VM) gfx_activity expected > 0, got {vf0_gfx}%"
    )
    assert vf1_gfx < _GFX_IDLE_THRESHOLD, (
        f"VF1 (idle witness) gfx_activity {vf1_gfx}% >= {_GFX_IDLE_THRESHOLD}% — "
        "activity bled from VF0 to VF1 (CPX isolation failure)"
    )


def test_vf_activity_returns_to_idle(
    gim_node, hypervisor_node, vf_topology, deploy_sriov_exporter_debian,
    vm0_workload, environment
):
    """
    After the workload process exits, gfx and umc activity on VF0 must
    drop below 5% within 15 seconds.

    Must run LAST in this module: it explicitly kills the workload inside
    the test body so the idle window is deterministic.  All tests that
    require an active workload (including test_vf_activity_no_bleed) must
    appear before this test in source order.
    """
    node = gim_node.node

    # Stop workload container in VM0 and wait for idle.
    Logger.info("Stopping workload container in VM0 for idle verification")
    vm_util.vm_run_command(
        hypervisor_node, vf_topology.vm0.ssh_port,
        f"docker stop --time 5 {vm_util._WORKLOAD_CONTAINER_NAME} 2>/dev/null; "
        f"docker rm -f {vm_util._WORKLOAD_CONTAINER_NAME} 2>/dev/null || true"
    )
    Logger.info(f"Waiting {_IDLE_WAIT}s for GPU to return to idle")
    time.sleep(_IDLE_WAIT)

    metric_util.collect_vf_workload_diagnostics(
        gim_node.node, environment, metrics_port=_METRICS_PORT, tag="idle_sample"
    )

    gfx_val  = _scrape_vf_metric_latest(node, "amd_gpu_gfx_activity", gpu_id=0)
    umc_val  = _scrape_vf_metric_latest(node, "amd_gpu_umc_activity", gpu_id=0)

    Logger.info(
        f"Post-workload idle: gfx_activity={gfx_val}%, umc_activity={umc_val}% (VF0)"
    )
    assert gfx_val < _GFX_IDLE_THRESHOLD, (
        f"amd_gpu_gfx_activity did not return to idle: {gfx_val}% >= {_GFX_IDLE_THRESHOLD}%"
    )
    assert umc_val < _UMC_IDLE_THRESHOLD, (
        f"amd_gpu_umc_activity did not return to idle: {umc_val}% >= {_UMC_IDLE_THRESHOLD}%"
    )
