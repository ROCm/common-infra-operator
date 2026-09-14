
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
# Licensed under the Apache License, Version 2.0

"""
QEMU/KVM VM lifecycle helpers for SR-IOV hypervisor tests.

The pre-built QCOW2 image contains:
  - Ubuntu guest OS with kernel 6.8-HWE (22.04) / 6.8+ (24.04)
  - amdgpu DKMS pre-built for the HWE kernel
  - ROCm SMI tools (rocm-smi-lib / rocm-smi4)
  - Docker CE — workload runs in a ROCm container (no native torch install)
  - root password: docker, SSH password auth enabled
  - netplan configured for QEMU SLIRP NAT (en* DHCP)
  - cloud-init disabled (datasource_list: [None]) to avoid boot delays

UEFI boot:
  Ubuntu cloud images (both 22.04 and 24.04) use UEFI/GPT and do not have a
  functional legacy BIOS GRUB.  VMs are launched with -machine q35 + OVMF
  pflash firmware.  The CODE drive is shared read-only; a writable VARS copy
  is created per-VM at /tmp/sriov-vm-ovmf-vars-<port>.fd and deleted on
  teardown.

Image naming convention:
  ubuntu-{os_ver}-amdgpu-{driver_ver}-sriov.qcow2
  e.g. ubuntu-22.04-amdgpu-31.30-sriov.qcow2

The base URL comes from the image manifest hypervisor.vm-image.location.
The full filename is constructed at runtime from the detected host OS version
and the amdgpu driver version in the spec file.

All functions accept a common.cluster_node for SSH access, consistent with
the rest of the test infrastructure.
"""

import base64
import json
import logging
import os
import re
import shlex
import time
import pytest

Logger = logging.getLogger("vm_util")

_VM_SSH_PORT    = 2222
_VM_SSH_TIMEOUT = 300   # seconds to wait for guest SSH to become reachable
_VM_MEMORY_MB   = 8192
_VM_CPUS        = 8
_VM_USER        = "root"
_VM_PASSWORD    = "docker"

_VM_LOG_LINES_TO_PRINT = 100   # lines of QEMU/serial log printed to pytest output on failure

# Workload container — rocm/pytorch:latest tracks the well-maintained upstream tag used by
# k8s/openshift workload tests (workload-specs.json).  Native gfx950 (MI350X VF) support
# confirmed on ROCm 7.2.4+.  No --group-add or HSA_OVERRIDE_GFX_VERSION needed: the
# container runs as root and latest includes compiled gfx950 targets.
_WORKLOAD_CONTAINER      = "rocm/pytorch:latest"
_WORKLOAD_CONTAINER_NAME = "gemm-workload"
_DOCKER_PULL_TIMEOUT     = 900   # seconds; cold pull on first boot (~10 GB compressed)

# OVMF firmware paths searched in order on the hypervisor; first match wins.
# Ubuntu 24.04 ships the 4M variants; Ubuntu 22.04 ships the 2M variants.
# Both support non-Secure-Boot UEFI which is all we need.
_OVMF_CODE_CANDIDATES = [
    "/usr/share/OVMF/OVMF_CODE_4M.fd",   # Ubuntu 24.04 / Debian bookworm
    "/usr/share/OVMF/OVMF_CODE.fd",       # Ubuntu 22.04 / older
    "/usr/share/qemu/OVMF.fd",            # fallback (some distros)
]
_OVMF_VARS_CANDIDATES = [
    "/usr/share/OVMF/OVMF_VARS_4M.fd",
    "/usr/share/OVMF/OVMF_VARS.fd",
]


class VMSession:
    """Represents a running QEMU VM with VF passthrough."""

    def __init__(self, host_ip: str, ssh_port: int, vf_pci_addr: str,
                 gpu_series: str = ""):
        self.host_ip          = host_ip
        self.ssh_port         = ssh_port
        self.vf_pci_addr      = vf_pci_addr
        self.gpu_series       = gpu_series
        self.pid: int         = 0
        self.qemu_log_file    = f"/tmp/sriov-vm-{ssh_port}.log"
        self.serial_log_file  = f"/tmp/sriov-vm-serial-{ssh_port}.log"
        self.ovmf_vars_file   = f"/tmp/sriov-vm-ovmf-vars-{ssh_port}.fd"

    def _log_basename(self) -> str:
        """Context-rich stem used for saved log filenames in logdir."""
        bdf_safe = self.vf_pci_addr.replace(":", "_")
        series   = self.gpu_series or "GPU"
        return f"vm_boot_{series}_{bdf_safe}"

    def __repr__(self):
        return f"VMSession(host={self.host_ip}, port={self.ssh_port}, vf={self.vf_pci_addr})"


def _collect_vm_boot_logs(node, session: "VMSession", logdir: str) -> None:
    """
    Fetch QEMU stderr, serial console, and hypervisor dmesg into *logdir*.

    Files are named <logdir>/vm_boot_<GPU>_<VF>_{qemu,serial,host_dmesg}.log.
    Content is also printed to the pytest logger (capped at _VM_LOG_LINES_TO_PRINT lines).
    Safe to call even when log files are absent — missing files produce empty output.
    """
    os.makedirs(logdir, exist_ok=True)
    stem = session._log_basename()

    for remote_path, suffix in [
        (session.qemu_log_file,   "qemu"),
        (session.serial_log_file, "serial"),
    ]:
        _, content, _ = node.run_command(f"cat {remote_path} 2>/dev/null")
        head = "\n".join(content.splitlines()[:_VM_LOG_LINES_TO_PRINT])
        if head.strip():
            Logger.warning(f"[vm-boot/{suffix}] {remote_path}:\n{head}")
        local = os.path.join(logdir, f"{stem}_{suffix}.log")
        try:
            node.get(remote_path, local)
        except Exception as exc:
            Logger.warning(f"Could not fetch {remote_path}: {exc}")

    # Hypervisor kernel messages — VFIO bind/reset events and PCIe errors
    _, dmesg, _ = node.run_command(
        "sudo dmesg --time-format=reltime 2>/dev/null"
        " | grep -iE 'vfio|amdgpu|iommu|pcie.*error|reset|fatal'"
        " | tail -80"
    )
    if dmesg.strip():
        Logger.warning(f"[vm-boot/host_dmesg] (VFIO/amdgpu):\n{dmesg}")
    local_dmesg = os.path.join(logdir, f"{stem}_host_dmesg.log")
    try:
        with open(local_dmesg, "w") as fh:
            fh.write(dmesg)
    except Exception as exc:
        Logger.warning(f"Could not write host dmesg: {exc}")


def qcow2_filename(os_version: str, driver_version: str) -> str:
    return f"ubuntu-{os_version}-amdgpu-{driver_version}-sriov.qcow2"


def qcow2_url(base_url: str, os_version: str, driver_version: str) -> str:
    return f"{base_url.rstrip('/')}/{qcow2_filename(os_version, driver_version)}"


def ensure_qcow2_present(node, base_url: str, os_version: str, driver_version: str,
                          download_folder: str) -> str:
    """
    Download the QCOW2 image to the hypervisor node if not already present.
    Returns the absolute path of the image on the hypervisor node.
    """
    filename = qcow2_filename(os_version, driver_version)
    remote_path = os.path.join(download_folder, filename)
    url = qcow2_url(base_url, os_version, driver_version)

    rc, _, _ = node.run_command(f"test -f {remote_path}")
    if rc != 0:
        Logger.info(f"Downloading QCOW2 image: {url}")
        rc, _, err = node.run_command(
            f"mkdir -p {download_folder} && wget -q -O {remote_path} {url}",
            timeout=600
        )
        if rc != 0:
            pytest.fail(f"Failed to download QCOW2 image from {url}: {err}")
    else:
        Logger.info(f"QCOW2 image already present: {remote_path}")

    return remote_path


def _vfio_bind(node, vf_pci_addr: str) -> None:
    """Bind a VF PCI device to the vfio-pci driver on the hypervisor."""
    rc, _, err = node.run_command(
        f"sudo modprobe vfio-pci && "
        f"echo vfio-pci | sudo tee /sys/bus/pci/devices/{vf_pci_addr}/driver_override && "
        f"echo {vf_pci_addr} | sudo tee /sys/bus/pci/drivers_probe"
    )
    if rc != 0:
        pytest.fail(f"Failed to bind {vf_pci_addr} to vfio-pci: {err}")
    Logger.info(f"VF {vf_pci_addr} bound to vfio-pci")


def _find_ovmf(node) -> tuple:
    """Return (code_path, vars_path) for OVMF on the hypervisor node.

    Searches candidate paths in order so the same code works on both
    Ubuntu 22.04 (2M OVMF) and Ubuntu 24.04 (4M OVMF) hypervisor hosts.
    Fails the test with a clear install hint if OVMF is absent.
    """
    for code in _OVMF_CODE_CANDIDATES:
        rc, _, _ = node.run_command(f"test -f {code}")
        if rc != 0:
            continue
        for vars_ in _OVMF_VARS_CANDIDATES:
            rc2, _, _ = node.run_command(f"test -f {vars_}")
            if rc2 == 0:
                Logger.info(f"OVMF: code={code}  vars={vars_}")
                return code, vars_
    pytest.fail(
        "OVMF firmware not found on hypervisor — "
        "install with: sudo apt install ovmf"
    )


def launch_vm(node, qcow2_path: str, vf_pci_addr: str,
              ssh_port: int = _VM_SSH_PORT,
              memory_mb: int = _VM_MEMORY_MB,
              cpus: int = _VM_CPUS,
              seed_iso_path: str = "",
              gpu_series: str = "") -> VMSession:
    """
    Launch a QEMU VM with the given VF passed through via VFIO.

    SSH is forwarded from host port *ssh_port* to guest port 22.
    Credentials: root / docker (pre-configured in the QCOW2 image).

    seed_iso_path: optional cloud-init NoCloud seed ISO to attach as a
    second virtio drive.
    """
    _vfio_bind(node, vf_pci_addr)

    session = VMSession(host_ip=node.ip_address, ssh_port=ssh_port,
                        vf_pci_addr=vf_pci_addr, gpu_series=gpu_series)
    pid_file      = f"/tmp/sriov-vm-{ssh_port}.pid"
    log_file      = session.qemu_log_file
    serial_file   = session.serial_log_file

    # Kill any stale QEMU holding a write lock on the image, then clean up
    # tracking files so the new instance starts without conflicts.
    # Run the kill in a background subshell (nohup) so that the PCIe
    # disruption caused by vfio-pci detach does not drop the SSH session.
    rc, old_pid, _ = node.run_command(f"cat {pid_file} 2>/dev/null")
    if rc == 0 and old_pid.strip().isdigit():
        node.run_command(
            f"nohup sudo bash -c "
            f"'kill {old_pid.strip()} 2>/dev/null; sleep 2; "
            f"kill -9 {old_pid.strip()} 2>/dev/null' "
            f">/dev/null 2>&1 &"
        )
        time.sleep(4)   # wait for QEMU to die before taking the write lock
    node.run_command(f"sudo rm -f {pid_file} {log_file} {serial_file}")

    seed_drive = (
        f" -drive file={seed_iso_path},format=raw,if=virtio,readonly=on"
        if seed_iso_path else ""
    )

    # Detect QEMU version to decide vfio-pci flags.
    # QEMU 8.x (Ubuntu 24.04) has stricter vfio-pci BAR mmap handling that
    # causes a userspace spin when the MI350X/MI325X VF MMIO region is not
    # fully initialised.  rombar=0 suppresses the ROM BAR; x-no-mmap=on makes
    # QEMU emulate BAR accesses instead of directly mmapping them.
    # On QEMU 6.x (Ubuntu 22.04) the mmap works fine so we skip the flag.
    _, qemu_ver_out, _ = node.run_command("qemu-system-x86_64 --version 2>/dev/null | head -1")
    qemu_major = 0
    m = re.search(r'version\s+(\d+)\.', qemu_ver_out or "")
    if m:
        qemu_major = int(m.group(1))
    vfio_extra = "rombar=0,romfile="
    if qemu_major >= 8:
        vfio_extra += ",x-no-mmap=on"

    # UEFI firmware — Ubuntu cloud images (22.04 and 24.04) use GPT + UEFI
    # and do not have a working legacy BIOS GRUB.  We boot via OVMF with a
    # q35 machine (PCIe chipset, required for modern vfio-pci passthrough).
    # A writable VARS copy is created per-VM so EFI variable writes (boot
    # order, etc.) don't interfere across concurrent VMs.
    ovmf_code, ovmf_vars_tmpl = _find_ovmf(node)
    ovmf_vars = session.ovmf_vars_file
    node.run_command(f"cp {ovmf_vars_tmpl} {ovmf_vars}")

    # Use nohup+background instead of -daemonize: avoids pidfile permission
    # issues when a stale file exists and gives us a log for boot failures.
    qemu_cmd = (
        f"nohup sudo qemu-system-x86_64 -enable-kvm -cpu host "
        f"-machine q35 "
        f"-drive if=pflash,format=raw,readonly=on,file={ovmf_code} "
        f"-drive if=pflash,format=raw,file={ovmf_vars} "
        f"-m {memory_mb} -smp {cpus} "
        f"-display none "
        f"-drive file={qcow2_path},format=qcow2,if=virtio"
        f"{seed_drive} "
        f"-device vfio-pci,host={vf_pci_addr},{vfio_extra} "
        f"-net nic,model=virtio -net user,hostfwd=tcp::{ssh_port}-:22 "
        f"-serial file:{serial_file} -monitor none "
        f"> {log_file} 2>&1 & echo $!"
    )
    Logger.info(f"QEMU version: {qemu_major}.x — machine: q35+UEFI — vfio flags: {vfio_extra}")

    rc, pid_out, err = node.run_command(qemu_cmd)
    if rc != 0:
        pytest.fail(f"Failed to launch QEMU VM: {err}")

    pid = int(pid_out.strip()) if pid_out.strip().isdigit() else 0
    if pid:
        node.run_command(f"echo {pid} > {pid_file}")

    # Give QEMU 3 seconds to start and detect immediate crashes (e.g. image
    # write-lock from a previous run that wasn't cleaned up).
    time.sleep(3)
    rc_alive, _, _ = node.run_command(f"kill -0 {pid} 2>/dev/null")
    if rc_alive != 0:
        _, qemu_log, _ = node.run_command(f"cat {log_file} 2>/dev/null")
        pytest.fail(f"QEMU exited immediately after launch. Log:\n{qemu_log}")

    session.pid = pid
    Logger.info(f"VM launched: pid={pid}, vf={vf_pci_addr}, ssh_port={ssh_port}")
    return session


def wait_for_vm_ssh(node, session: "VMSession",
                    timeout: int = _VM_SSH_TIMEOUT,
                    logdir: str = "") -> None:
    """Poll until the guest sshd is ready on *session.ssh_port*.

    Uses a real SSH handshake so the caller can issue commands immediately
    without hitting 'connection reset by peer' during sshd startup.
    On timeout, collects QEMU stderr, serial console, and hypervisor dmesg
    into *logdir* before failing.
    """
    ssh_port  = session.ssh_port
    deadline  = time.time() + timeout
    ssh_probe = (
        f"sshpass -p {shlex.quote(_VM_PASSWORD)} ssh -o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 "
        f"-p {ssh_port} {shlex.quote(_VM_USER)}@127.0.0.1 true"
    )
    while time.time() < deadline:
        rc, _, _ = node.run_command(ssh_probe)
        if rc == 0:
            Logger.info(f"VM SSH ready on port {ssh_port}")
            return
        time.sleep(5)
    if logdir:
        _collect_vm_boot_logs(node, session, logdir)
    pytest.fail(
        f"VM SSH not ready on port {ssh_port} within {timeout}s"
        + (f" — boot logs saved to {logdir}" if logdir else "")
    )


def vm_run_command(node, ssh_port: int, cmd: str, vm_user: str = _VM_USER,
                   vm_password: str = _VM_PASSWORD,
                   timeout: int = 120) -> tuple[int, str, str]:
    """
    Run a command inside the guest VM by SSH-ing through the hypervisor's port-forward.
    Uses sshpass for password auth to the VM (key auth not required for disposable VMs).
    """
    ssh_cmd = (
        f"sshpass -p {shlex.quote(vm_password)} ssh -o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile=/dev/null "
        f"-p {ssh_port} {shlex.quote(vm_user)}@127.0.0.1 {shlex.quote(cmd)}"
    )
    return node.run_command(ssh_cmd, timeout=timeout)


def teardown_vm(node, session: VMSession, logdir: str = "") -> None:
    """Terminate the QEMU VM and release the VFIO binding.

    Collects QEMU/serial/dmesg boot logs into *logdir* before cleanup so
    they are available for offline diagnosis regardless of test outcome.
    """
    if logdir:
        _collect_vm_boot_logs(node, session, logdir)
    if session.pid:
        # Background kill: vfio-pci detach can disrupt the hypervisor SSH
        # session; running via nohup subshell decouples the kill from the
        # SSH connection so subsequent commands still work.
        node.run_command(
            f"nohup sudo bash -c "
            f"'kill {session.pid} 2>/dev/null; sleep 2; "
            f"kill -9 {session.pid} 2>/dev/null' "
            f">/dev/null 2>&1 &"
        )
        time.sleep(4)
        node.run_command(
            f"sudo rm -f /tmp/sriov-vm-{session.ssh_port}.pid"
            f" {session.qemu_log_file} {session.serial_log_file}"
        )
        node.run_command(f"sudo rm -f {session.ovmf_vars_file}")
        Logger.info(f"VM pid={session.pid} terminated")
    # Release VFIO binding so GIM can reclaim the VF.
    vf = session.vf_pci_addr
    node.run_command(
        f"echo {vf} | sudo tee /sys/bus/pci/drivers/vfio-pci/unbind 2>/dev/null; "
        f"echo | sudo tee /sys/bus/pci/devices/{vf}/driver_override 2>/dev/null"
    )
    Logger.info(f"VF {vf} unbound from vfio-pci")


# ---------------------------------------------------------------------------
# CPX / SPX topology helpers
# ---------------------------------------------------------------------------

# Port derived from VF index so tests are port-agnostic.
def _vm_port_from_vf_index(vf_index: int) -> int:
    return _VM_SSH_PORT + vf_index  # 2222, 2223, ...


class VFTopology:
    """Launched VM session(s) for one test run."""

    def __init__(self, vm0: VMSession, vm1=None, is_cpx: bool = False):
        self.vm0 = vm0
        self.vm1 = vm1      # VMSession | None
        self.is_cpx = is_cpx

    def __repr__(self):
        return f"VFTopology(vm0={self.vm0}, vm1={self.vm1}, cpx={self.is_cpx})"


def verify_amdgpu_in_vm(node, session: "VMSession") -> None:
    """Fail the test session if amdgpu is not loaded and probed inside the VM.

    Checks both:
      1. lsmod — module is present in the kernel
      2. /dev/kfd — KFD device node, only created when amdgpu successfully
         probes at least one GPU; absent if probe failed (e.g. TLB flush
         timeout, unsupported device ID, or GIM version mismatch)

    This is a hard fail (not a skip) because a missing driver means every
    subsequent workload-dependent test would be silently skipped, masking
    the failure in CI.
    """
    rc_lsmod, _, _ = vm_run_command(
        node, session.ssh_port, "lsmod | grep -q amdgpu"
    )
    rc_kfd, _, _ = vm_run_command(
        node, session.ssh_port, "test -e /dev/kfd"
    )
    if rc_lsmod != 0 or rc_kfd != 0:
        _, dmesg, _ = vm_run_command(
            node, session.ssh_port,
            "dmesg | grep -iE 'amdgpu|vfio|error|fail' | tail -30 2>/dev/null"
        )
        _, lspci, _ = vm_run_command(
            node, session.ssh_port, "lspci | grep -i amd 2>/dev/null"
        )
        reason = []
        if rc_lsmod != 0:
            reason.append("amdgpu module not in lsmod (driver did not load)")
        if rc_kfd != 0:
            reason.append("/dev/kfd absent (amdgpu probe failed — VF not initialized)")
        pytest.fail(
            f"GPU not available in VM (port={session.ssh_port}, "
            f"vf={session.vf_pci_addr}): {'; '.join(reason)}.\n"
            f"lspci:\n{lspci}\n"
            f"dmesg (last 30):\n{dmesg}"
        )
    Logger.info(
        f"amdgpu loaded and /dev/kfd present in VM (port={session.ssh_port})"
    )


def launch_spx_topology(node, qcow2_path: str, vf_pci_addr: str,
                        gpu_series: str = "", logdir: str = "") -> VFTopology:
    """SPX: one VM on VF0."""
    session = launch_vm(node, qcow2_path, vf_pci_addr,
                        ssh_port=_vm_port_from_vf_index(0),
                        gpu_series=gpu_series)
    wait_for_vm_ssh(node, session, logdir=logdir)
    return VFTopology(vm0=session, vm1=None, is_cpx=False)


def launch_cpx_topology(node, qcow2_path: str,
                        vf0_pci_addr: str, vf1_pci_addr: str,
                        gpu_series: str = "", logdir: str = "") -> VFTopology:
    """CPX: VM0 on VF0 (workload), VM1 on VF1 (idle witness)."""
    vm0 = launch_vm(node, qcow2_path, vf0_pci_addr,
                    ssh_port=_vm_port_from_vf_index(0),
                    gpu_series=gpu_series)
    vm1 = launch_vm(node, qcow2_path, vf1_pci_addr,
                    ssh_port=_vm_port_from_vf_index(1),
                    gpu_series=gpu_series)
    wait_for_vm_ssh(node, vm0, logdir=logdir)
    wait_for_vm_ssh(node, vm1, logdir=logdir)
    return VFTopology(vm0=vm0, vm1=vm1, is_cpx=True)


def teardown_topology(node, topology: VFTopology, logdir: str = "") -> None:
    """Terminate all VMs in a topology."""
    teardown_vm(node, topology.vm0, logdir=logdir)
    if topology.vm1:
        teardown_vm(node, topology.vm1, logdir=logdir)


# ---------------------------------------------------------------------------
# Workload helpers
# ---------------------------------------------------------------------------

_WORKLOAD_SPECS_PATH = os.path.join(os.path.dirname(__file__), "files", "workload-specs.json")


def load_workload_script(workload_type: str) -> str:
    """
    Return the script body for *workload_type* from workload-specs.json.

    For python3-based specs (command: ["python3", "-c"]), args[0] is the
    complete Python program.  The caller should write this to a file in the
    VM and run it with python3.
    """
    with open(_WORKLOAD_SPECS_PATH) as fp:
        data = json.load(fp)
    for entry in data.get("workload-specs", []):
        if entry["workload-type"] == workload_type:
            args = entry["spec"]["spec"]["containers"][0].get("args", [])
            if not args:
                raise ValueError(f"Workload '{workload_type}' has no args")
            return args[0]
    raise KeyError(f"Workload type '{workload_type}' not found in {_WORKLOAD_SPECS_PATH}")


def vm_write_script(node, ssh_port: int, script: str, remote_path: str) -> None:
    """Write *script* to *remote_path* inside the VM using base64 encoding."""
    encoded = base64.b64encode(script.encode()).decode()
    rc, _, err = vm_run_command(
        node, ssh_port,
        f"echo '{encoded}' | base64 -d > {remote_path}"
    )
    if rc != 0:
        pytest.fail(f"Failed to write script to VM at {remote_path}: {err}")
    Logger.info(f"Script written to VM: {remote_path}")


def vm_ensure_docker_image(node, ssh_port: int,
                            image: str = _WORKLOAD_CONTAINER) -> None:
    """Pull *image* inside the VM if not already present in the Docker cache."""
    rc, _, _ = vm_run_command(
        node, ssh_port, f"docker image inspect {image} > /dev/null 2>&1"
    )
    if rc == 0:
        Logger.info(f"Docker image already cached in VM: {image}")
        return
    Logger.info(f"Pulling Docker image in VM (may take up to {_DOCKER_PULL_TIMEOUT}s): {image}")
    rc, out, err = vm_run_command(
        node, ssh_port, f"docker pull {image}", timeout=_DOCKER_PULL_TIMEOUT
    )
    if rc != 0:
        pytest.fail(f"Failed to pull Docker image {image} in VM: {err or out}")
    Logger.info(f"Docker image pull complete: {image}")


def vm_get_render_device_flags(node, ssh_port: int) -> str:
    """Return --device flags for all /dev/dri/renderD* nodes present in the VM."""
    rc, out, _ = vm_run_command(
        node, ssh_port, "ls /dev/dri/renderD* 2>/dev/null"
    )
    if rc != 0 or not out.strip():
        Logger.warning("No /dev/dri/renderD* found in VM; falling back to renderD128")
        return "--device /dev/dri/renderD128"
    flags = " ".join(f"--device {d}" for d in out.strip().split())
    Logger.info(f"Render devices in VM: {out.strip()!r}")
    return flags
