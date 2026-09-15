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

import pytest
import os
import json
import logging
from pathlib import Path
import lib.amdgpu as amdgpu
import lib.gim_util as gim_util
import lib.spec_util as spec_util
import lib.vm_util as vm_util

Logger = logging.getLogger("hypervisor.conftest")


def check_vm_support_matrix(gpu_series, host_os_version, host_kernel):
    """Check if the host OS/kernel is supported for SR-IOV VM passthrough.

    Reads the sriov.vm_passthrough block from amdgpu-features.json.
    Returns None if supported, or a skip reason string if not.
    """
    features = amdgpu.get_gpu_features_by_series(gpu_series)
    if not features:
        return None
    sriov = features.get("sriov")
    if not sriov or not sriov.get("supported"):
        return f"{gpu_series}: SR-IOV not supported"
    vm = sriov.get("vm_passthrough")
    if not vm:
        return None

    host_os_tag = f"Ubuntu {host_os_version}"
    if host_os_tag not in vm.get("host_os", []):
        return (f"{gpu_series} SR-IOV VM requires host OS {vm.get('host_os', [])}, "
                f"got {host_os_tag}")

    kernel_ok = any(host_kernel.startswith(p) for p in vm.get("host_kernel_prefix", []))
    if not kernel_ok:
        return (f"{gpu_series} SR-IOV VM requires host kernel {vm.get('host_kernel_prefix', [])}, "
                f"got {host_kernel}")

    return None


@pytest.fixture(scope="session", autouse=True)
def init_testbed(request, environment):
    """Validate that testbed.json is present for hypervisor tests."""
    testbed_path = request.config.option.testbed
    if not testbed_path or not os.path.exists(testbed_path):
        pytest.fail("--testbed <testbed.json> is required for hypervisor tests")
    Logger.info("Hypervisor testbed validated: %s", testbed_path)


@pytest.fixture(scope="session")
def hypervisor_node(request, environment):
    """Build a cluster_node from testbed.json — does not require a K8s cluster.

    Looks for the first instance with type 'hypervisor'; falls back to the
    first non-master instance; falls back to the first instance overall.
    """
    testbed_path = request.config.option.testbed
    with open(testbed_path) as fp:
        testbed = json.load(fp)

    instances = testbed.get("instances", [])
    if not instances:
        pytest.fail("No instances found in testbed.json for hypervisor tests")

    entry = (
        next((e for e in instances if e.get("type") == "hypervisor"), None)
        or next((e for e in instances if e.get("type") != "master"), None)
        or instances[0]
    )

    node = gim_util.build_hypervisor_node(
        ip=entry["ip"],
        username=entry.get("username"),
        password=entry.get("password"),
        gpu_count=entry.get("gpu_count", 1),
    )
    Logger.info("Hypervisor node: %s (from testbed.json)", node.ip_address)
    return node


@pytest.fixture(scope="session")
def gim_node(hypervisor_node):
    """
    Assert GIM is loaded and discover PF/VF PCI topology on the hypervisor node.
    Skips the session if GIM is not loaded or no SR-IOV VFs are present.
    """
    gn = gim_util.discover_gim_node(hypervisor_node)
    if not gn.pf_pci_addrs:
        pytest.skip("No AMD GPU PFs with SR-IOV support found on hypervisor")
    if not gn.vf_pci_addrs:
        pytest.skip("No SR-IOV VFs found — GIM may not have enabled VFs yet")
    Logger.info(f"GIM topology: {gn}")
    setattr(pytest, "_gim_node_info", {
        "os_version": gn.os_version,
        "kernel_version": gn.kernel_version,
        "gpu_series": getattr(hypervisor_node, "gpu_series", "NA"),
    })
    return gn


@pytest.fixture(scope="session")
def gim_driver_spec(request):
    """Load and return the GIM driver spec from --gim-driver-spec, or the default spec."""
    spec_path = request.config.option.gim_driver_spec
    if not spec_path:
        spec_path = "lib/files/gim-driver-spec.json"
    spec_path = Path(spec_path)
    if not spec_path.exists():
        pytest.skip(f"GIM driver spec not found: {spec_path}")
    with open(spec_path) as fp:
        spec = json.load(fp)
    Logger.info(f"GIM driver spec: {spec}")
    setattr(pytest, "_gim_driver_spec", spec)
    return spec


@pytest.fixture(scope="session")
def hypervisor_images(request, environment):
    """Return the 'hypervisor' section from the image manifest."""
    from ruamel.yaml import YAML
    yaml = YAML()
    yaml.preserve_quotes = True

    manifest_path = Path(request.config.option.image_manifest)
    if not manifest_path.exists():
        pytest.fail(f"Image manifest not found: {manifest_path}")

    manifest = dict(yaml.load(manifest_path))
    hv_images = manifest.get("images", {}).get("hypervisor")
    if not hv_images:
        pytest.fail(f"No 'hypervisor' section in image manifest {manifest_path.name}")
    return hv_images


@pytest.fixture(scope="session")
def partition_profile() -> str:
    """Return GIM_PARTITION_PROFILE env var (default: SPX)."""
    return os.environ.get("GIM_PARTITION_PROFILE", "SPX").upper()


@pytest.fixture(scope="session")
def vf_topology(hypervisor_node, gim_node, hypervisor_images, partition_profile, environment):
    """
    Launch session-scoped VM(s) with VF passthrough.

    SPX: 1 VM on GPU0/VF0.
    CPX: 2 VMs — VM0 on GPU0/VF0 (workload), VM1 on GPU0/VF1 (idle witness).

    Skips the session if the host OS/kernel violates the GIM SR-IOV VM support
    matrix, the QCOW2 image is unavailable, the hypervisor has
    no PF/VF topology, or (for CPX) fewer than 2 VFs are present on GPU0.
    """
    # Check SR-IOV VM support matrix from amdgpu-features.json
    gpu_series = getattr(hypervisor_node, "gpu_series", "") or ""
    skip_reason = check_vm_support_matrix(gpu_series, gim_node.os_version, gim_node.kernel_version)
    if skip_reason:
        Logger.warning(f"SR-IOV VM support matrix violation: {skip_reason}")
        pytest.skip(skip_reason)

    # Resolve QCOW2 image location from hypervisor_images manifest section.
    # The manifest entry named 'vm-image' carries 'location' (base URL) and
    # 'version' (amdgpu driver version string).  The OS version comes from the
    # hypervisor node itself (gim_node.os_version).
    vm_image_info = hypervisor_images.get("vm-image")
    if not vm_image_info:
        pytest.skip("No 'vm-image' entry in hypervisor image manifest — cannot download QCOW2")

    base_url       = vm_image_info.get("location", "")
    driver_version = vm_image_info.get("version", "")
    if not base_url or not driver_version:
        pytest.skip(
            f"vm-image manifest entry missing 'location' or 'version': {vm_image_info}"
        )

    download_folder = getattr(environment, "download_folder", "/tmp/gpuop-downloads")
    qcow2_path = vm_util.ensure_qcow2_present(
        hypervisor_node,
        base_url,
        gim_node.os_version,
        driver_version,
        download_folder,
    )

    pf_bdf = gim_node.pfs[0].bdf if gim_node.pfs else None
    if not pf_bdf:
        pytest.skip("No GPU PF found on hypervisor")

    vfs_on_gpu0 = gim_node.vfs_for_pf(pf_bdf)
    if not vfs_on_gpu0:
        pytest.skip("No VFs found on GPU0 — GIM may not have enabled VFs yet")

    gpu_series = getattr(hypervisor_node, "gpu_series", "") or ""
    logdir     = getattr(environment, "logdir", "")

    try:
        if partition_profile == "CPX":
            if len(vfs_on_gpu0) < 2:
                pytest.skip(
                    f"CPX mode requires >=2 VFs on GPU0; found {len(vfs_on_gpu0)}"
                )
            topology = vm_util.launch_cpx_topology(
                hypervisor_node, qcow2_path,
                vfs_on_gpu0[0].bdf, vfs_on_gpu0[1].bdf,
                gpu_series=gpu_series, logdir=logdir,
            )
        else:
            topology = vm_util.launch_spx_topology(
                hypervisor_node, qcow2_path, vfs_on_gpu0[0].bdf,
                gpu_series=gpu_series, logdir=logdir,
            )
    except Exception as e:
        pytest.fail(f"VM launch failed — cannot run VF metrics tests: {e}")

    Logger.info(f"VF topology ready: {topology}")
    yield topology

    vm_util.teardown_topology(hypervisor_node, topology, logdir=logdir)
    Logger.info("VF topology torn down")
