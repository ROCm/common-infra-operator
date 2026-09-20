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
Driver upgrade/downgrade cycle tests.

Moved from test_driver_deviceplugin.py to enable selective execution of
driver upgrade tests independently from device-plugin functional tests.
"""

import pdb
import pytest
import os
import re
import time
import json
import logging
import random
import urllib.request
import urllib.error
from functools import lru_cache
import lib.k8_util as k8_util
import lib.amdgpu as amdgpu
import lib.common as common
import lib.spec_util as spec_util
from lib.util import K8Helper

Logger = logging.getLogger("k8.gpu-operator.upgrade.test_driver_upgrade")


@pytest.fixture(scope="module")
def deviceconfig_install(gpu_cluster, images, gpu_operator_install, environment):
    global Logger

    devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
    for devcfg_name, _ in devcfg_map.items():
        k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)
    time.sleep(10)

    class DeviceConfigCRInfo(object):
        pass

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "Error getting gpu-nodes")
    K8Helper.triage(environment, len(gpu_nodes) > 0, "No GPU nodes found")

    test_config = {
        'metadata.namespace': environment.gpu_operator_namespace,
        'driver.enable': True,
        'driver.blacklist': True,
        'devicePlugin.enableNodeLabeller': True,
        'metricsExporter.enable': True,
        'metricsExporter.serviceType' : 'NodePort',
        'configManager.enable': True,
        'testRunner.enable': True,
    }
    test_config.update(images)

    test_cfg_map = spec_util.build_deviceconfig_cr_template(
        test_config, gpu_nodes, 'driver-upgrade', environment.amdgpu_driver_spec)
    exporter_port_map = {}
    devicecfg_list = []
    if len(test_cfg_map) > 1:
        for idx, cfg_name in enumerate(test_cfg_map.keys()):
            cfg = test_cfg_map[cfg_name]
            cfg['metricsExporter.nodePort'] = 32500 + idx * 100
            exporter_port_map[cfg['selector.value']] = cfg['metricsExporter.nodePort']
    else:
        for node in gpu_nodes:
            node_hostname = k8_util.k8_get_node_hostname(node)
            exporter_port_map[node_hostname] = 32500

    for spec_name, tcfg in test_cfg_map.items():
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, _, ret_stderr = k8_util.k8_create_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, ret_code == 0,
                        f"Failed to create deviceconfig: {ret_stderr}")
        devicecfg_list.append(tcfg['metadata.name'])

    K8Helper.check_deviceconfig_status(environment, devicecfg_list)
    for devcfg in devicecfg_list:
        K8Helper.wait_kmm_worker_completion(environment, devcfg)
    K8Helper.update_node_driver_version(gpu_cluster, environment)

    devcfg_info = DeviceConfigCRInfo()
    setattr(devcfg_info, "test_cfg_map", test_cfg_map)
    setattr(devcfg_info, "exporter_port_map", exporter_port_map)
    setattr(devcfg_info, "devicecfg_list", devicecfg_list)
    yield devcfg_info

    devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
    for devcfg_name, _ in devcfg_map.items():
        k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)
    time.sleep(10)


def _get_driver_versions(metafunc):
    """Parse driver spec and return (current_version, alternative_versions) or None to skip."""
    if not metafunc.config.option.amdgpu_driver_spec:
        return None
    with open(metafunc.config.option.amdgpu_driver_spec, "r") as fp:
        driver_spec = json.load(fp)
    if driver_spec["driver-deployment"] == "inbox":
        return None
    current = driver_spec["default-version"]
    alts = [v for v in driver_spec.get('alternative-versions', []) if v != current]
    return current, alts


def pytest_generate_tests(metafunc):
    if 'upgrade_version' in metafunc.fixturenames:
        result = _get_driver_versions(metafunc)
        if result is None:
            metafunc.parametrize('upgrade_version', [pytest.param(None, marks=pytest.mark.skip(reason="Inbox driver or no driver spec"))])
        else:
            _, alts = result
            if not alts:
                metafunc.parametrize('upgrade_version', [pytest.param(None, marks=pytest.mark.skip(reason="No alternative driver versions available in spec"))])
            else:
                metafunc.parametrize('upgrade_version', alts)

    if 'limited_upgrade_version' in metafunc.fixturenames:
        result = _get_driver_versions(metafunc)
        if result is None:
            metafunc.parametrize('limited_upgrade_version', [pytest.param(None, marks=pytest.mark.skip(reason="Inbox driver or no driver spec"))])
        else:
            _, alts = result
            if not alts:
                metafunc.parametrize('limited_upgrade_version', [pytest.param(None, marks=pytest.mark.skip(reason="No alternative driver versions available in spec"))])
            else:
                metafunc.parametrize('limited_upgrade_version', random.sample(alts, 1))

def _skip_if_version_violates_gpu_constraints(gpu_cluster, target_version, current_version):
    """Skip the calling test if target_version violates driver constraints for the cluster's GPU series."""
    from packaging.version import Version
    gpu_variants = gpu_cluster.get_gpu_variants()
    for gpu_series in gpu_variants:
        features = amdgpu.get_gpu_features_by_series(gpu_series)
        driver = features.get("driver", {})
        dc = driver.get("deviceconfig", {})
        min_version = dc.get("min_version")
        if min_version and Version(target_version) < Version(min_version):
            pytest.skip(f"{gpu_series}: target version {target_version} below min_version {min_version}")


@lru_cache(maxsize=None)
def _is_version_buildable(driver_version, rhel_version_id):
    """Probe repo.radeon.com for an exact minor-version repo for this driver.

    Only an el/{rhel_version_id} hit counts as buildable. Old drivers (6.x/7.x)
    have a generic el/{major} repo, but those packages cannot satisfy the
    kernel-devel requirement for newer RHEL minor kernels — e.g. el/9 does not
    build on a 5.14.0-687.15.1.el9_8 kernel. Falling back to el/{major} caused
    false-positive "buildable" verdicts that led to 1200s+ build pod failures
    that then contaminated the cluster for all subsequent upgrade tests.

    Returns True (fail-open) on network errors to avoid silently dropping coverage.
    """
    url = f"https://repo.radeon.com/amdgpu/{driver_version}/el/{rhel_version_id}/main/x86_64/repodata/repomd.xml"
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        return True  # unexpected HTTP error → fail-open
    except Exception:
        return True  # network failure → fail-open


def _rhel_version_id_from_kernel(kernel_version):
    """Extract RHEL VERSION_ID from a kernel version string.

    '5.14.0-687.15.1.el9_8.x86_64' → '9.8'
    Returns None if the pattern is not found.
    """
    m = re.search(r'el(\d+)_(\d+)', kernel_version)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    return None


def _skip_if_version_not_buildable_on_cluster(driver_version, gpu_cluster, environment):
    """Skip if driver_version cannot be built by KMM on the current OpenShift cluster.

    Only active on OpenShift (DeviceConfig) deployments. Probes repo.radeon.com
    with the same URL fallback sequence the KMM Dockerfile uses. Fail-open on
    network errors so a transient outage does not silently drop test coverage.
    """
    if environment.deployment_mode != "openshift":
        return

    gpu_nodes = [n for n in gpu_cluster.cluster_nodes if n.num_gpus > 0]
    if not gpu_nodes:
        return

    kernel_version = gpu_nodes[0].kernel_version
    rhel_version_id = _rhel_version_id_from_kernel(kernel_version)
    if not rhel_version_id:
        Logger.warning(f"Could not parse RHEL VERSION_ID from kernel '{kernel_version}'; skipping repo probe")
        return

    if not _is_version_buildable(driver_version, rhel_version_id):
        pytest.skip(
            f"driver {driver_version} has no el/{rhel_version_id} repo on repo.radeon.com; "
            f"skipping on CoreOS {rhel_version_id}"
        )


def test_driver_upgrade_cycle(request, gpu_cluster, deviceconfig_install, environment, upgrade_version, inbox_driver_skip):
    global Logger
    if environment.gpu_operator_version in ["v1.0.0", "v1.1.0"]:
        pytest.skip(f"Skipping driver-upgrade testcase for current version {environment.gpu_operator_version}")

    current_version = environment.amdgpu_driver_spec["default-version"]
    _skip_if_version_violates_gpu_constraints(gpu_cluster, upgrade_version, current_version)
    _skip_if_version_not_buildable_on_cluster(upgrade_version, gpu_cluster, environment)
    Logger.info(f"Upgrading cluster/gpu-nodes from {current_version} => {upgrade_version}")
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "gpu-operator failed to find amd/gpu nodes in the cluster")

    # Restore
    def _restore():
        Logger.info(f"Restoring cluster/gpu-nodes from {upgrade_version} => {current_version}")
        try:
            # A failed build pod keeps KMM in a tight reconciliation loop that
            # causes 409 Conflicts on every DeviceConfig replace attempt. Delete
            # stale build pods first so the operator can settle before we patch.
            for devcfg in deviceconfig_install.devicecfg_list:
                k8_util.k8_delete_all_pods_with_name_pattern(environment.gpu_operator_namespace, f"{devcfg}-build")
            time.sleep(10)

            for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
                tcfg['driver.blacklist'] = True
                tcfg['driver.version'] = current_version
                tcfg['driver.upgradePolicy.enable'] = True
                cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
                ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
                if ret_code != 0:
                    Logger.error(f"Failed to modify deviceconfig CR during restore: {ret_stderr}")
                    # Don't fail teardown - continue trying to restore cluster state
                    continue

            # Check for reboot operation - but don't fail teardown if it times out
            try:
                # Use fail_on_timeout=False to prevent teardown from failing test
                K8Helper.wait_for_upgrade_completion_status(environment, deviceconfig_install.devicecfg_list, gpu_nodes, fail_on_timeout=False)
            except Exception as e:
                Logger.error(f"Failed to wait for upgrade completion during restore: {e}")
                Logger.warn("Teardown will continue despite upgrade not completing - cluster may need manual recovery")

            # Check for corresponding deviceconfig updated
            K8Helper.check_deviceconfig_status(environment, deviceconfig_install.devicecfg_list)
            for devcfg in deviceconfig_install.devicecfg_list:
                K8Helper.wait_kmm_worker_completion(environment, devcfg)

            driver_version = amdgpu.get_matching_driver_version(current_version)
            K8Helper.check_deviceconfig_driver_version(gpu_cluster, current_version, environment)
            K8Helper.check_node_driver_version(gpu_cluster, current_version, driver_version, environment)
        except Exception as e:
            Logger.error(f"Exception during restore teardown: {e}")
            Logger.warn("Teardown failed - cluster may be in inconsistent state and need manual recovery")

    request.addfinalizer(_restore)

    # Step 1: Modify DeviceConfig to set new driver version (with upgradePolicy disabled)
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['driver.blacklist'] = True
        tcfg['driver.version'] = upgrade_version
        tcfg['driver.upgradePolicy.enable'] = False
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, ret_code == 0, "Failed to modify deviceconfig CR")

    # Step 2: Validate that CR modification persisted (before enabling upgradePolicy)
    # This catches if the operator or another component is overwriting our changes
    for devcfg_name in deviceconfig_install.devicecfg_list:
        K8Helper.log_deviceconfig_state(environment, devcfg_name, f"After setting driver version to {upgrade_version} (upgradePolicy=False)")

    # Validate only the DeviceConfigs we just modified (not all in namespace)
    devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
    for devcfg_name in deviceconfig_install.devicecfg_list:
        if devcfg_name in devcfg_map:
            devcfg_info = devcfg_map[devcfg_name]
            devcfg_driver_version = devcfg_info.get('spec').get('driver').get('version')
            Logger.info(f'DeviceConfig {devcfg_name} configured version: {devcfg_driver_version}')
            K8Helper.triage(environment, upgrade_version == devcfg_driver_version,
                            f"Expected {upgrade_version}, found {devcfg_driver_version} - CR was overwritten after modification!")
        else:
            K8Helper.triage(environment, False, f"DeviceConfig {devcfg_name} not found after modification")

    # Step 3: Enable upgradePolicy to trigger the upgrade
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['driver.upgradePolicy.enable'] = True
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, ret_code == 0, "Failed to modify deviceconfig CR to enable upgradePolicy")

    # Step 4: Log CR state before waiting for upgrade
    for devcfg_name in deviceconfig_install.devicecfg_list:
        K8Helper.log_deviceconfig_state(environment, devcfg_name, f"After enabling upgradePolicy (before waiting for upgrade)")

    # Step 5: Wait for upgrade to complete
    try:
        K8Helper.wait_for_upgrade_completion_status(environment, deviceconfig_install.devicecfg_list, gpu_nodes)
    except Exception as e:
        # Step 6: If upgrade fails, collect additional diagnostics before failing
        Logger.error(f"Driver upgrade failed: {e}")
        Logger.info("Collecting additional diagnostics before failing test...")
        K8Helper.collect_additional_diagnostics(environment, deviceconfig_install.devicecfg_list, gpu_nodes, environment.logdir)
        for devcfg_name in deviceconfig_install.devicecfg_list:
            K8Helper.log_deviceconfig_state(environment, devcfg_name, "After upgrade timeout/failure")
        raise  # Re-raise the exception to fail the test
    if environment.gpu_operator_version in ["v1.2.0", "v1.2.1", "v1.2.2"]:
        # For v1.2.0 and v1.2.1, manual reboot is required
        Logger.info(f"For {environment.gpu_operator_version}, manual reboot of nodes required post driver upgrade")
        for node in gpu_nodes:
            node_name = k8_util.k8_get_node_hostname(node)
            ret_code = k8_util.reboot_node(gpu_cluster, node_name)
            K8Helper.triage(environment, ret_code == 0, f"Failed to reboot node {node_name}")

    driver_version = amdgpu.get_matching_driver_version(upgrade_version)
    K8Helper.check_deviceconfig_driver_version(gpu_cluster, upgrade_version, environment)
    K8Helper.check_node_driver_version(gpu_cluster, upgrade_version, driver_version, environment)

    # Validate DME is serving metrics post-upgrade
    Logger.info("Verifying DME metrics endpoint is healthy after driver upgrade")
    devicecfg_pods = [
        common.PodInfo('device-plugin', len(gpu_nodes), 1),
        common.PodInfo('metrics-exporter', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods)
    K8Helper.triage(environment, not failed_pods,
                    f"Post-upgrade: operand pods not Running - {failed_pods}")

    for node in gpu_nodes:
        node_ip = k8_util.k8_get_node_address(node)
        cluster_node = gpu_cluster.find_node_by_ip(node_ip)
        if cluster_node is None:
            continue
        node_hostname = k8_util.k8_get_node_hostname(node)
        port = deviceconfig_install.exporter_port_map.get(node_hostname, 32500)
        ret_code, metrics_output, ret_err = cluster_node.http_get(port, "metrics")
        K8Helper.triage(environment, ret_code == 0,
                        f"Post-upgrade: failed to scrape DME metrics from {node_hostname}:{port} - {ret_err}")
        if isinstance(metrics_output, bytes):
            metrics_output = metrics_output.decode('utf-8', errors='replace')
        for metric in ['gpu_clock', 'gpu_junction_temperature', 'gpu_total_vram']:
            K8Helper.triage(environment, metric in metrics_output,
                            f"Post-upgrade: metric {metric} missing from DME output on {node_hostname}")
        Logger.info(f"Post-upgrade: {node_hostname} DME metrics healthy (driver {upgrade_version})")

def test_upgrade_driver_using_label(request, gpu_cluster, environment, deviceconfig_install, limited_upgrade_version, inbox_driver_skip):
    global Logger
    '''
    Upgrade driver using label update method
    '''
    if environment.gpu_operator_version in ["v1.0.0", "v1.1.0"]:
        pytest.skip(f"Skipping driver-upgrade testcase for current version {environment.gpu_operator_version}")

    current_version = environment.amdgpu_driver_spec["default-version"]
    _skip_if_version_violates_gpu_constraints(gpu_cluster, limited_upgrade_version, current_version)
    _skip_if_version_not_buildable_on_cluster(limited_upgrade_version, gpu_cluster, environment)
    Logger.info(f"Upgrading cluster/gpu-nodes from {current_version} => {limited_upgrade_version}")
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "gpu-operator failed to find amd/gpu nodes in the cluster")
    # Restore
    def _restore():
        Logger.info(f"Restoring cluster/gpu-nodes from {limited_upgrade_version} => {current_version}")
        # Delete stale build pods before patching so a failed build does not leave
        # KMM in a tight reconciliation loop that causes 409 Conflicts on replace.
        for devcfg in deviceconfig_install.devicecfg_list:
            k8_util.k8_delete_all_pods_with_name_pattern(environment.gpu_operator_namespace, f"{devcfg}-build")
        time.sleep(10)

        for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
            tcfg['driver.blacklist'] = True
            tcfg['driver.version'] = current_version
            tcfg['driver.upgradePolicy.enable'] = True
            cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
            ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
            K8Helper.triage(environment, ret_code == 0, "Failed to modify deviceconfig CR to enable upgradePolicy")

        time.sleep(20)
        # Check for reboot operation
        K8Helper.wait_for_upgrade_completion_status(environment, deviceconfig_install.devicecfg_list, gpu_nodes)
        K8Helper.check_deviceconfig_status(environment, deviceconfig_install.devicecfg_list)
        for devcfg in deviceconfig_install.devicecfg_list:
            K8Helper.wait_kmm_worker_completion(environment, devcfg)

        driver_version = amdgpu.get_matching_driver_version(current_version)
        K8Helper.check_deviceconfig_driver_version(gpu_cluster, current_version, environment)
        K8Helper.check_node_driver_version(gpu_cluster, current_version, driver_version, environment)

    request.addfinalizer(_restore)

    # Update deviceconfig with new driver version (disable upgradePolicy)
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['driver.blacklist'] = True
        tcfg['driver.version'] = limited_upgrade_version
        tcfg['driver.upgradePolicy.enable'] = False
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, ret_code == 0, "Failed to modify deviceconfig CR to enable upgradePolicy")

    time.sleep(20)
    K8Helper.check_deviceconfig_status(environment, deviceconfig_install.devicecfg_list)
    for devcfg in deviceconfig_install.devicecfg_list:
        K8Helper.wait_kmm_worker_completion(environment, devcfg)

    # Build labels
    labels = {}
    for devcfg in deviceconfig_install.devicecfg_list:
        labels[f"kmm.node.kubernetes.io/version-module.{environment.gpu_operator_namespace}.{devcfg}"] = limited_upgrade_version
    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        K8Helper.triage(environment, (k8_util.k8_label_node(node_name, labels)), f"Failed to update label on node {node_name}")

    K8Helper.check_deviceconfig_status(environment, deviceconfig_install.devicecfg_list)
    for devcfg in deviceconfig_install.devicecfg_list:
        K8Helper.wait_kmm_worker_completion(environment, devcfg)

    driver_version = amdgpu.get_matching_driver_version(limited_upgrade_version)
    K8Helper.check_deviceconfig_driver_version(gpu_cluster, limited_upgrade_version, environment)
    K8Helper.check_node_driver_version(gpu_cluster, limited_upgrade_version, driver_version, environment)
    #K8Helper.wait_for_upgrade_completion_status(environment, deviceconfig_install.devicecfg_list, gpu_nodes)
    if environment.gpu_operator_version in ["v1.2.0", "v1.2.1", "v1.2.2"]:
        # For v1.2.0 and v1.2.1, manual reboot is required
        Logger.info(f"For {environment.gpu_operator_version}, manual reboot of nodes required post driver upgrade")
        for node in gpu_nodes:
            node_name = k8_util.k8_get_node_hostname(node)
            ret_code = k8_util.reboot_node(gpu_cluster, node_name)
            K8Helper.triage(environment, ret_code == 0, f"Failed to reboot node {node_name}")

    driver_version = amdgpu.get_matching_driver_version(limited_upgrade_version)
    K8Helper.check_node_driver_version(gpu_cluster, limited_upgrade_version, driver_version, environment)

    # Validate DME is serving metrics post-upgrade
    Logger.info("Verifying DME metrics endpoint is healthy after driver upgrade (label method)")
    devicecfg_pods = [
        common.PodInfo('device-plugin', len(gpu_nodes), 1),
        common.PodInfo('metrics-exporter', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods)
    K8Helper.triage(environment, not failed_pods,
                    f"Post-upgrade: operand pods not Running - {failed_pods}")

    for node in gpu_nodes:
        node_ip = k8_util.k8_get_node_address(node)
        cluster_node = gpu_cluster.find_node_by_ip(node_ip)
        if cluster_node is None:
            continue
        node_hostname = k8_util.k8_get_node_hostname(node)
        port = deviceconfig_install.exporter_port_map.get(node_hostname, 32500)
        ret_code, metrics_output, ret_err = cluster_node.http_get(port, "metrics")
        K8Helper.triage(environment, ret_code == 0,
                        f"Post-upgrade: failed to scrape DME metrics from {node_hostname}:{port} - {ret_err}")
        if isinstance(metrics_output, bytes):
            metrics_output = metrics_output.decode('utf-8', errors='replace')
        for metric in ['gpu_clock', 'gpu_junction_temperature', 'gpu_total_vram']:
            K8Helper.triage(environment, metric in metrics_output,
                            f"Post-upgrade: metric {metric} missing from DME output on {node_hostname}")
        Logger.info(f"Post-upgrade: {node_hostname} DME metrics healthy (driver {limited_upgrade_version})")

