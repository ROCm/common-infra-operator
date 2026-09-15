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
import logging
import time
from lib import common
import lib.helm_util as helm_util
import lib.k8_util as k8_util
import lib.spec_util as spec_util
import lib.amdgpu as amdgpu
from lib.util import K8Helper


pytest.skip("Skipping the whole module!", allow_module_level=True)

Logger = logging.getLogger("k8.gpu-operator.upgrade.test_operator_ondelete_upgrade")

def debug_on_failure(environment, condition, message):
    """Helper function for debugging on failure"""
    K8Helper.triage(environment, condition, message)

@pytest.mark.upgrade
@pytest.mark.level1
def test_gpu_operator_helm_upgrade(request, gpu_cluster, gpu_operator_release_name,
                                   base_deviceconfig_install, base_version, base_images,
                                   rc_images, environment):
    """
    Test GPU operator upgrade from base version to RC version.

    Test flow:
    1. Base version is deployed via base_deviceconfig_install fixture
    2. Validate base deployment is working
    3. Perform helm upgrade to RC version
    4. Validate upgraded deployment is working
    5. Verify all pods are running with RC images
    """
    global Logger

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    debug_on_failure(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    debug_on_failure(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")

    Logger.info("="*80)
    Logger.info(f"PHASE 1: Validate base version ({base_version}) deployment")
    Logger.info("="*80)

    gpu_op_release_info = amdgpu.get_supported_operands(base_version)
    if gpu_op_release_info == None:
        pytest.fail(f"No information available about this release {base_version}")

    # Verify base version pods are running
    devicecfg_pods = []
    for operand in gpu_op_release_info.get("operands", []):
        devicecfg_pods.append(common.PodInfo(operand["pod-name"], len(gpu_nodes), 1))

    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods, sleep_time=20)
    debug_on_failure(environment, (not failed_pods), f"Base version pods are not ready - {failed_pods}")

    # Check DeviceConfig status for base version
    K8Helper.check_deviceconfig_status(environment, base_deviceconfig_install.devicecfg_list)

    Logger.info(f"Base version ({base_version}) deployment validated successfully")

    Logger.info("="*80)
    Logger.info(f"PHASE 2: Upgrade GPU operator to RC version")
    Logger.info("="*80)

    # Collect currently installed helm-charts
    ret_code, ret_stdout, ret_stderr = helm_util.helm_list(gpu_cluster, environment.gpu_operator_namespace)
    debug_on_failure(environment, (ret_code == 0), f"Failed to currently installed helm-charts")

    secret_list = []
    for entry in gpu_cluster.k8_secrets["secrets"]:
        secret_list.append(entry["name"])
    # Generate RC version values.yaml
    values_yaml = os.path.join(environment.logdir, f"rc_values_{environment.gpu_operator_version}.yaml")
    if spec_util.generate_helmchart_deployment_config(environment.gpu_operator_version, rc_images, secret_list, values_yaml):
        Logger.info(f"Generated RC version values.yaml for helm upgrade: {values_yaml}")
    else:
        values_yaml = None

    # Perform helm upgrade
    Logger.info(f"Executing helm upgrade from {base_version} to RC")
    ret_code, ret_stdout, ret_stderr = helm_util.helm_upgrade(gpu_cluster, gpu_operator_release_name,
                                                               environment.gpu_operator_namespace,
                                                               rc_images.get('gpu-operator.helm-chart', None),
                                                               environment.gpu_operator_version, values_yaml)
    if ret_code != 0:
        Logger.error(f"Failed to upgrade helm chart for {gpu_operator_release_name}")
        Logger.error(f"Stdout: {ret_stdout}")
        Logger.error(f"Stderr: {ret_stderr}")
    debug_on_failure(environment, (ret_code == 0), f"Failed to upgrade {gpu_operator_release_name} to RC version")

    # Wait for operator pods to stabilize after upgrade
    Logger.info("Waiting for operator daemon rollout after upgrade...")
    K8Helper.watch_for_daemon_rollout(environment, environment.gpu_operator_namespace, len(gpu_cluster.cluster_nodes))
    time.sleep(30)

    helm_status = helm_util.is_helm_chart_deployed(gpu_cluster, gpu_operator_release_name, environment.gpu_operator_namespace)
    debug_on_failure(environment, (helm_status == True), f"helm-chart {gpu_operator_release_name} is not in deployed status after upgrade")

    helm_healthy = helm_util.is_helm_chart_healthy(gpu_cluster, gpu_operator_release_name, environment.gpu_operator_namespace)
    debug_on_failure(environment, (helm_healthy == True), f"helm-chart {gpu_operator_release_name} is not healthy after upgrade")

    Logger.info("="*80)
    Logger.info(f"PHASE 3: Validate RC version deployment after upgrade")
    Logger.info("="*80)

    # Verify all pods are running after upgrade
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods, sleep_time=20)
    debug_on_failure(environment, (not failed_pods), f"RC version pods are not ready after upgrade - {failed_pods}")

    # Check DeviceConfig status after upgrade
    K8Helper.check_deviceconfig_status(environment, base_deviceconfig_install.devicecfg_list)

    # Verify pods are using RC images
    Logger.info("Verifying pods are using RC version images...")
    ret_code, all_pods = k8_util.k8_get_pods(environment.gpu_operator_namespace)
    debug_on_failure(environment, (ret_code == 0), "Failed to get pods from namespace")

    # Check that operator pods have been updated (not just restarted with old images)
    # Verify key components are running RC images from rc_images manifest
    """
    components_to_check = {
        'gpu-operator-charts-controller-manager': 'gpu-operator.image.repository',
    }

    for component, repo_key in components_to_check.items():
        component_pods = [p for p in all_pods if component in p['metadata']['name']]
        debug_on_failure(environment, len(component_pods) > 0, f"No {component} pods found after upgrade")

        for pod in component_pods:
            for container_status in pod['status'].get('container_statuses', []):
                image = container_status.get('image', '')
                Logger.info(f"Pod {pod['metadata']['name']} running image: {image}")

                # Verify image is not empty
                debug_on_failure(environment, image != '', f"Empty image for {pod['metadata']['name']}")

                # Verify image matches RC manifest repository (if available)
                if repo_key in rc_images:
                    rc_repo = rc_images[repo_key]
                    # Extract repository part from full image string (repo:tag or repo@digest)
                    image_repo = image.split(':')[0].split('@')[0]
                    debug_on_failure(environment, rc_repo in image_repo,
                                   f"Expected RC repo '{rc_repo}' not found in image '{image}' for {component}")
    """

    Logger.info(f"Successfully upgraded from {base_version} to RC version")
    Logger.info("All pods running and DeviceConfigs healthy after upgrade")

@pytest.mark.upgrade
@pytest.mark.level1
def test_deviceconfig_operand_upgrade_after_operator_upgrade(request, gpu_cluster, base_deviceconfig_install,
                                                              base_version, rc_images, environment):
    """
    Test operand upgrade after operator has been upgraded.

    This test assumes operator is already at RC version (from previous test or fixture).
    It validates that we can upgrade individual operand images via DeviceConfig CR modification.

    Test flow:
    1. Operator is at RC version, operands at base version
    2. Modify DeviceConfig to use RC operand images
    3. Wait for operand pods to restart with new images
    4. Validate operands are working with RC images
    """
    global Logger

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    debug_on_failure(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    debug_on_failure(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")

    Logger.info("="*80)
    Logger.info(f"Test operand upgrade from {base_version} to RC")
    Logger.info("="*80)

    # Get current pods for comparison
    gpu_op_release_info = amdgpu.get_supported_operands(base_version)
    if gpu_op_release_info == None:
        pytest.fail(f"No information available about this release {base_version}")

    # Verify base version pods are running
    devicecfg_pods = []
    operands = gpu_op_release_info.get("operands", [])
    for operand in operands:
        devicecfg_pods.append(common.PodInfo(operand["pod-name"], len(gpu_nodes), 1))

    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods, sleep_time=20)
    debug_on_failure(environment, (not failed_pods), f"Base version pods are not ready - {failed_pods}")

    # Capture pod state before upgrade
    Logger.info("Capturing pod state before upgrade...")
    before_upgrade = {}
    for operand in operands:
        ret_code, pod_status = k8_util.k8_get_pods(environment.gpu_operator_namespace, pod_name_pattern=operand["pod-name"])
        debug_on_failure(environment, (ret_code == 0 and len(pod_status) > 0), f"No pods found before upgrade for {operand['name']}")
        before_upgrade[operand['name']] = pod_status

    Logger.info("="*80)
    Logger.info(f"PHASE: Trigger operand upgrade via DeviceConfig modification")
    Logger.info("="*80)

    # Update DeviceConfig with RC operand images
    Logger.info(f"Updating DeviceConfig CR with RC operand images for {operands}")
    for spec_name, tcfg in base_deviceconfig_install.test_cfg_map.items():
        # Update operand images to RC version (if supported)
        for operand in operands:
            imgKey = operand.get("image-key", None)
            if imgKey:
                tcfg[f"{imgKey}.repository"] = rc_images[f"{imgKey}.repository"]
                tcfg[f"{imgKey}.version"] = rc_images[f"{imgKey}.version"]
            else:
                Logger.debug(f"Operand {operand} does not support image-key, hence no upgrade is triggered")

        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        debug_on_failure(environment, (ret_code == 0), f"Failed to modify deviceconfig CR: {ret_stderr}")

    # Wait for operand pods to restart with new images
    Logger.info("Waiting for operand pods to restart with RC images...")
    time.sleep(30)

    # Verify operand pods are running
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods, sleep_time=20)
    debug_on_failure(environment, (not failed_pods), f"Operand pods not ready after upgrade - {failed_pods}")

    # Check DeviceConfig status
    K8Helper.check_deviceconfig_status(environment, base_deviceconfig_install.devicecfg_list)

    # Capture pod state after upgrade
    Logger.info("Capturing pod state after upgrade...")
    after_upgrade = {}
    for operand in operands:
        ret_code, pod_status = k8_util.k8_get_pods(environment.gpu_operator_namespace, pod_name_pattern=operand["pod-name"])
        debug_on_failure(environment, (ret_code == 0 and len(pod_status) > 0), f"No pods found after upgrade for {operand['name']}")
        after_upgrade[operand['name']] = pod_status

    Logger.info("="*80)
    Logger.info(f"PHASE: Verify operand upgrade succeeded")
    Logger.info("="*80)

    # Compare before/after upgrade to verify pods were recreated with new images
    for operand in operands:
        operand_name = operand['name']

        # Verify operand was found in both before and after upgrade
        debug_on_failure(environment, operand_name in before_upgrade,
                        f"Operand {operand_name} not found in before_upgrade - pods may not have been running before upgrade")
        debug_on_failure(environment, operand_name in after_upgrade,
                        f"Operand {operand_name} not found in after_upgrade - pods may not be running after upgrade")

        if operand_name not in before_upgrade or operand_name not in after_upgrade:
            continue

        before_pods = before_upgrade[operand_name]
        after_pods = after_upgrade[operand_name]

        Logger.info(f"Verifying {operand_name} upgrade:")
        # Check that pods were restarted (different UIDs or restart counts)
        before_uids = {p['metadata']['uid'] for p in before_pods}
        after_uids = {p['metadata']['uid'] for p in after_pods}

        # At least some pods should have different UIDs (indicating restart)
        if before_uids == after_uids:
            Logger.warning(f"  {operand_name} pods have same UIDs - may not have restarted")

        # Verify RC images are being used (if operand has image-key)
        imgKey = operand.get("image-key", None)
        if imgKey:
            rc_repo_key = f"{imgKey}.repository"
            rc_version_key = f"{imgKey}.version"
            if rc_repo_key in rc_images and rc_version_key in rc_images:
                expected_repo = rc_images[rc_repo_key]
                expected_version = rc_images[rc_version_key]

                for pod in after_pods:
                    for container_status in pod['status'].get('container_statuses', []):
                        image = container_status.get('image', '')
                        Logger.info(f"  Pod {pod['metadata']['name']} using image: {image}")
                        # Verify expected repository and version are in the image
                        debug_on_failure(environment, expected_repo in image and expected_version in image,
                                       f"Expected RC image with repo '{expected_repo}' and version '{expected_version}' not found in '{image}'")

    Logger.info(f"Successfully upgraded operands from {base_version} to RC")
