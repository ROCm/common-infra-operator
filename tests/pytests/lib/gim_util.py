
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
# Licensed under the Apache License, Version 2.0

"""
GIM (GPU IOV Module) utilities for hypervisor-side SR-IOV tests.

GIM itself is installed by Ansible (install-gim-driver.yml) before the test
session.  These helpers only assert state and discover hardware topology via SSH.

All functions accept a common.cluster_node for SSH access, consistent with
the rest of the test infrastructure (standalone, deb_util, etc.).
"""

import logging
import pytest

Logger = logging.getLogger("gim_util")

# sysfs-based discovery — works for any AMD GPU platform with SR-IOV support.
# PF: AMD vendor (0x1002) + sriov_totalvfs > 0  (populated by GIM when loaded)
# VF: physfn symlink present (kernel creates this for SR-IOV virtual functions)
#
# Each record is tab-separated: bdf <TAB> numvfs <TAB> iommu_group
_DISCOVER_PF_CMD = r"""
for d in /sys/bus/pci/devices/*/; do
    vendor=$(cat "${d}vendor" 2>/dev/null)
    total=$(cat "${d}sriov_totalvfs" 2>/dev/null)
    if [ "$vendor" = "0x1002" ] && [ -n "$total" ] && [ "$total" -gt 0 ]; then
        bdf=$(basename "$d")
        numvfs=$(cat "${d}sriov_numvfs" 2>/dev/null || echo 0)
        iommu=$(basename $(readlink -f "${d}iommu_group") 2>/dev/null || echo "unknown")
        echo "${bdf}	${numvfs}	${iommu}"
    fi
done
""".strip()

# VF record: bdf <TAB> pf_bdf <TAB> iommu_group
_DISCOVER_VF_CMD = r"""
for d in /sys/bus/pci/devices/*/; do
    if [ -L "${d}physfn" ]; then
        vendor=$(cat "${d}vendor" 2>/dev/null)
        if [ "$vendor" = "0x1002" ]; then
            bdf=$(basename "$d")
            pf_bdf=$(basename $(readlink -f "${d}physfn") 2>/dev/null || echo "unknown")
            iommu=$(basename $(readlink -f "${d}iommu_group") 2>/dev/null || echo "unknown")
            echo "${bdf}	${pf_bdf}	${iommu}"
        fi
    fi
done
""".strip()


class PFInfo:
    """SR-IOV Physical Function details."""
    def __init__(self, bdf: str, numvfs: int, iommu_group: str):
        self.bdf         = bdf
        self.numvfs      = numvfs
        self.iommu_group = iommu_group

    def __repr__(self):
        return f"PF({self.bdf}, vfs={self.numvfs}, iommu={self.iommu_group})"


class VFInfo:
    """SR-IOV Virtual Function details."""
    def __init__(self, bdf: str, pf_bdf: str, iommu_group: str):
        self.bdf         = bdf
        self.pf_bdf      = pf_bdf
        self.iommu_group = iommu_group

    def __repr__(self):
        return f"VF({self.bdf}, pf={self.pf_bdf}, iommu={self.iommu_group})"


class GimNode:
    """Runtime PCIe topology discovered from a single hypervisor node."""

    def __init__(self, node):
        self.node        = node         # common.cluster_node
        self.pfs: list[PFInfo] = []
        self.vfs: list[VFInfo] = []
        self.os_version: str = ""
        self.kernel_version: str = ""

    @property
    def pf_pci_addrs(self) -> list[str]:
        return [pf.bdf for pf in self.pfs]

    @property
    def vf_pci_addrs(self) -> list[str]:
        return [vf.bdf for vf in self.vfs]

    def vfs_for_pf(self, pf_bdf: str) -> list[VFInfo]:
        return [vf for vf in self.vfs if vf.pf_bdf == pf_bdf]

    def __repr__(self):
        return (
            f"GimNode({self.node.ip_address}, pfs={self.pfs}, "
            f"vfs={self.vfs}, os={self.os_version}, kernel={self.kernel_version})"
        )


def assert_gim_loaded(node) -> None:
    """Skip the test session if the GIM kernel module is not loaded on *node*."""
    rc, out, _ = node.run_command("lsmod | grep -w gim")
    if rc != 0:
        pytest.skip(
            "GIM kernel module not loaded on hypervisor — "
            "run install-gim-driver.yml before this session"
        )
    Logger.info(f"GIM loaded: {out.splitlines()[0] if out else '(gim found)'}")


def discover_pfs(node) -> list[PFInfo]:
    """Return AMD GPU SR-IOV PFs with numvfs and IOMMU group from sysfs."""
    rc, out, _ = node.run_command(_DISCOVER_PF_CMD)
    if rc != 0 or not out.strip():
        return []
    pfs = []
    for line in out.splitlines():
        parts = line.strip().split("\t")
        if len(parts) == 3:
            bdf, numvfs, iommu = parts
            pfs.append(PFInfo(bdf, int(numvfs), iommu))
    Logger.info(f"PFs discovered: {pfs}")
    return pfs


def discover_vfs(node) -> list[VFInfo]:
    """Return AMD SR-IOV VFs with their parent PF and IOMMU group from sysfs."""
    rc, out, _ = node.run_command(_DISCOVER_VF_CMD)
    if rc != 0 or not out.strip():
        return []
    vfs = []
    for line in out.splitlines():
        parts = line.strip().split("\t")
        if len(parts) == 3:
            bdf, pf_bdf, iommu = parts
            vfs.append(VFInfo(bdf, pf_bdf, iommu))
    Logger.info(f"VFs discovered: {vfs}")
    return vfs


def get_host_os_version(node) -> str:
    """Return the OS version string (e.g. '22.04') from lsb_release."""
    rc, out, _ = node.run_command("lsb_release -sr")
    if rc != 0 or not out.strip():
        Logger.warning("Could not determine host OS version")
        return "unknown"
    return out.strip()


def discover_gim_node(node) -> GimNode:
    """Assert GIM is loaded then discover full PF/VF PCIe topology for *node*."""
    from lib import node_gpu_collector
    gim_node = GimNode(node)
    assert_gim_loaded(node)
    gim_node.pfs            = discover_pfs(node)
    gim_node.vfs            = discover_vfs(node)
    gim_node.os_version     = get_host_os_version(node)
    gim_node.kernel_version = node_gpu_collector.collect_host_kernel_version(node)
    node.gpu_series         = node_gpu_collector.collect_gpu_series_by_lspci_ssh(node)
    return gim_node


# ---------------------------------------------------------------------------
# amd-smi helpers (GIM installs its own amd-smi on the hypervisor)
# ---------------------------------------------------------------------------

import re as _re

# Common candidate locations for GIM-installed amd-smi.
_AMD_SMI_CANDIDATES = [
    "/usr/bin/amd-smi",
    "/usr/local/bin/amd-smi",
    "/opt/gim/bin/amd-smi",
    "/opt/amd/bin/amd-smi",
]


def find_amd_smi(node) -> str | None:
    """
    Return the absolute path to amd-smi on *node*, or None if not found.

    Tries 'which amd-smi' first, then falls back to a set of known install
    locations.  Does NOT assume amd-smi is in PATH.
    """
    rc, out, _ = node.run_command("which amd-smi")
    if rc == 0 and out.strip():
        path = out.strip()
        Logger.info(f"amd-smi found via which: {path}")
        return path

    for candidate in _AMD_SMI_CANDIDATES:
        rc, _, _ = node.run_command(f"test -x {candidate}")
        if rc == 0:
            Logger.info(f"amd-smi found at: {candidate}")
            return candidate

    Logger.warning("amd-smi not found on hypervisor node")
    return None


def assert_amd_smi_available(node) -> str:
    """
    Return the amd-smi path, or pytest.skip if it is not installed.

    Call this at the top of any testcase that uses amd-smi so missing
    installation produces a clean SKIP rather than a command-not-found error.
    """
    path = find_amd_smi(node)
    if path is None:
        pytest.skip(
            "amd-smi not found on hypervisor — "
            "install GIM driver package before running this test"
        )
    return path


def amd_smi_pf_to_gpu_id(node, amd_smi: str, pfs: list) -> dict:
    """
    Return {pf_bdf: gpu_index} by parsing '<amd_smi> list'.

    Falls back to positional order (PFs sorted by BDF) when the command
    fails or the output cannot be parsed.
    """
    rc, out, _ = node.run_command(f"sudo {amd_smi} list")
    mapping = {}
    if rc == 0 and out.strip():
        for line in out.splitlines():
            m_idx = _re.search(r'GPU\[(\d+)\]', line)
            m_bdf = _re.search(r'([0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9])', line)
            if m_idx and m_bdf:
                mapping[m_bdf.group(1)] = int(m_idx.group(1))
        if mapping:
            return mapping
    # Fallback: sort PFs by BDF and assign indices 0, 1, 2 ...
    for idx, pf in enumerate(sorted(pfs, key=lambda p: p.bdf)):
        mapping[pf.bdf] = idx
    Logger.warning("amd-smi list parse failed — using positional PF order for GPU IDs")
    return mapping


def amd_smi_vf_static(node, amd_smi: str, vf_bdf: str) -> tuple:
    """Run 'sudo <amd_smi> static --vf=<bdf>' and return (rc, stdout, stderr)."""
    return node.run_command(f"sudo {amd_smi} static --vf={vf_bdf}")


def amd_smi_gpu_num_vf(node, amd_smi: str, gpu_id: int) -> tuple:
    """Run 'sudo <amd_smi> static --gpu=<id> --num-vf' and return (rc, stdout, stderr)."""
    return node.run_command(f"sudo {amd_smi} static --gpu={gpu_id} --num-vf")


def amd_smi_metric_json(node, amd_smi: str) -> tuple:
    """Run 'sudo <amd_smi> metric --json' and return (rc, stdout, stderr)."""
    return node.run_command(f"sudo {amd_smi} metric --json")


def amd_smi_static_json(node, amd_smi: str) -> tuple:
    """Run 'sudo <amd_smi> static --json' and return (rc, stdout, stderr)."""
    return node.run_command(f"sudo {amd_smi} static --json")


def build_hypervisor_node(ip: str, username: str, password: str, gpu_count: int = 1):
    """Construct a cluster_node from testbed.json entry — no K8s dependency."""
    from lib import common
    node = common.cluster_node(
        ip_address=ip,
        user_name=username,
        password=password,
        node_type=common.TestbedType.HYPERVISOR,
    )
    node._num_gpus = gpu_count
    return node


