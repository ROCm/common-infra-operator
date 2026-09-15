#!/usr/bin/python3

"""
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
"""

import pdb
import pytest
import pprint
import sys
import os
import re
import time
import json
import logging
import lib.helm_util as helm_util
import lib.k8_util as k8_util
import lib.dra_util as dra_util
import lib.common as common
from lib.util import K8Helper

Logger = logging.getLogger("k8.test_dra_driver_install")

# DRA driver chart name constant
DRA_DRIVER_CHART_NAME = "k8s-gpu-dra-driver"


def check_amd_gpu_deviceclass_exists(environment, deviceclass_name="gpu.amd.com"):
    """Check if AMD GPU DeviceClass exists

    Args:
        environment: Test environment object (contains cached dra_api_version)
        deviceclass_name: Name of the DeviceClass to check (default: gpu.amd.com)

    Returns:
        tuple: (bool, str) - (exists, deviceclass_name or error_message)
    """
    # Use cached DRA API version from environment
    dra_api_version = dra_util.get_dra_api_version(environment)
    if not dra_api_version:
        error_msg = "DRA API not available in this cluster"
        Logger.error(error_msg)
        return False, error_msg

    # Use existing k8_util helper
    # kubectl equivalent: kubectl get deviceclasses.resource.k8s.io
    ret_code, device_classes, err = k8_util.k8_get_custom_resource_objects(
        group="resource.k8s.io",
        version=dra_api_version,
        plural="deviceclasses"
    )

    if ret_code != 0:
        error_msg = f"Error checking DeviceClass: {err}"
        Logger.error(error_msg)
        return False, error_msg

    for dc in device_classes:
        dc_name = dc.get("metadata", {}).get("name", "")
        if dc_name == deviceclass_name:
            return True, dc_name

    return False, f"DeviceClass '{deviceclass_name}' not found"


def check_dra_driver_pods(dra_driver_release_name, dra_driver_namespace, environment, gpu_cluster):
    """Check that all DRA driver pods are running

    Args:
        dra_driver_release_name: Name of the Helm release
        dra_driver_namespace: Namespace where DRA driver is installed
        environment: Test environment
        gpu_cluster: GPU cluster object for hardware-based GPU node detection
    """
    gpu_node_count = sum(1 for node in gpu_cluster.cluster_nodes if node.is_gpu_node())
    K8Helper.triage(
        environment, gpu_node_count > 0, "No AMD GPU nodes found in cluster"
    )

    # Wait for all DRA driver pods to be created and running
    # DRA driver typically runs as a DaemonSet on GPU nodes
    # Pod name format: {release-name}-{chart-name}-kubeletplugin
    pod_name_prefix = f"{dra_driver_release_name}-{DRA_DRIVER_CHART_NAME}-kubeletplugin"
    exp_pod_list = [
        common.PodInfo(pod_name_prefix, gpu_node_count, 1),
    ]

    failed_pods = k8_util.k8_check_pod_running(dra_driver_namespace, exp_pod_list)
    K8Helper.triage(
        environment,
        not failed_pods,
        f"One or more DRA driver pods are not ready - {failed_pods}",
    )

    Logger.info(f"All DRA driver pods are running ({gpu_node_count} pod(s))")


def check_dra_driver_resource_class(environment):
    """Check that AMD GPU DeviceClass is created

    Args:
        environment: Test environment
    """
    # Wait a bit for DeviceClass to be created
    time.sleep(10)

    # Check if AMD GPU DeviceClass exists
    amd_gpu_class_found, message = check_amd_gpu_deviceclass_exists(environment, "gpu.amd.com")
    
    if amd_gpu_class_found:
        Logger.info(f"Found AMD GPU DeviceClass: {message}")
    else:
        Logger.error(f"AMD GPU DeviceClass not found: {message}")

    K8Helper.triage(
        environment,
        amd_gpu_class_found,
        "AMD GPU DeviceClass 'gpu.amd.com' not found",
    )


def check_dra_driver_logs_no_errors(dra_driver_namespace, environment):
    """Check that DRA driver pods have no critical errors in logs

    Args:
        dra_driver_namespace: Namespace where DRA driver is installed
        environment: Test environment
    """
    # Get all DRA driver pods
    # kubectl equivalent: kubectl get pods -n <namespace>
    ret_code, pods = k8_util.k8_get_pods(dra_driver_namespace)
    K8Helper.triage(environment, ret_code == 0, "Failed to get DRA driver pods")

    error_found = False
    for pod in pods:
        pod_name = pod["metadata"]["name"]
        # Check for DRA driver pods (k8s-gpu-dra-driver in the name)
        if DRA_DRIVER_CHART_NAME in pod_name:
            # Check pod logs for errors
            # kubectl equivalent: kubectl logs <pod_name> -n <namespace>
            ret_code, logs, error = k8_util.k8_get_pod_logs(
                pod_name, dra_driver_namespace
            )
            if ret_code == 0:
                # Look for common error patterns
                error_patterns = [
                    "fatal error",
                    "panic:",
                    "failed to start",
                    "connection refused",
                ]

                for pattern in error_patterns:
                    if pattern.lower() in logs.lower():
                        Logger.error(
                            f"Found error pattern '{pattern}' in pod {pod_name} logs"
                        )
                        error_found = True
            else:
                Logger.warn(f"Could not retrieve logs for pod {pod_name}")

    K8Helper.triage(
        environment, not error_found, "Critical errors found in DRA driver pod logs"
    )
    
    Logger.info("DRA driver pods have no critical errors")


@pytest.fixture(autouse=True, scope="module")
def skip_module(environment):
    """Skip if not testing on K8s or if K8s version doesn't support DRA"""
    if environment.deployment_mode != "k8":
        pytest.skip(
            f"Skipping DRA driver testcases for {environment.deployment_mode} deployment"
        )

    ret_code, version_info = k8_util.k8_get_version()
    if ret_code != 0:
        pytest.skip("Failed to get Kubernetes version")

    major_match = re.match(r"(\d+)", str(version_info.get("major", "0")))
    minor_match = re.match(r"(\d+)", str(version_info.get("minor", "0")))
    major = int(major_match.group(1)) if major_match else 0
    minor = int(minor_match.group(1)) if minor_match else 0

    if major < 1 or (major == 1 and minor < 32):
        pytest.skip(
            f"DRA requires Kubernetes 1.32+, but cluster is running {major}.{minor}"
        )

    dra_available, error_msg, _ = dra_util.check_dra_api_available()
    if not dra_available:
        pytest.skip(f"DRA API not available: {error_msg}")

    return


def test_dra_driver_install(
    gpu_cluster,
    dra_driver_release_name,
    dra_driver_namespace,
    dra_driver_install,
    environment,
):
    """Test DRA driver installation via Helm chart.

    This test validates that the DRA driver has been successfully installed
    and is running correctly in the cluster.

    Validates:
        - Helm release is deployed with status "deployed"
        - DRA driver namespace exists
        - DRA driver pods are running
        - No critical errors in pod logs

    Fixtures:
        dra_driver_install: Installs DRA driver before test runs

    kubectl equivalents:
        helm list -n <namespace>
        kubectl get namespaces
        kubectl get pods -n <namespace>
        kubectl logs <pod-name> -n <namespace>

    Expected outcome:
        DRA driver is fully installed and operational
    """
    global Logger

    # Verify Helm release is deployed
    ret_code, ret_stdout, ret_stderr = helm_util.helm_list(
        gpu_cluster, dra_driver_namespace
    )
    K8Helper.triage(environment, (ret_code == 0), "Failed to list helm-charts")

    dra_driver_running = False
    for chart in json.loads(ret_stdout):
        if chart["name"] == dra_driver_release_name and chart["status"] == "deployed":
            dra_driver_running = True
            Logger.info(f"DRA driver helm chart is deployed: {chart}")

    K8Helper.triage(
        environment,
        dra_driver_running,
        f"helm-chart {dra_driver_release_name} is not in expected state",
    )

    # Check if DRA driver namespace is created
    # kubectl equivalent: kubectl get namespaces
    ret_code, k8_namespaces = k8_util.k8_get_namespaces()
    K8Helper.triage(
        environment, (ret_code == 0), "Error checking k8-namespaces from cluster"
    )
    K8Helper.triage(
        environment,
        (
            len(
                list(
                    filter(
                        lambda x: x["metadata"]["name"] == dra_driver_namespace,
                        k8_namespaces,
                    )
                )
            )
            == 1
        ),
        f"Could not find {dra_driver_namespace} namespace in the cluster",
    )

    # Check DRA driver pods are running
    Logger.info("Checking DRA driver pods...")
    check_dra_driver_pods(dra_driver_release_name, dra_driver_namespace, environment, gpu_cluster)

    # Check AMD GPU DeviceClass is created
    Logger.info("Checking AMD GPU DeviceClass...")
    check_dra_driver_resource_class(environment)

    # Check DRA driver logs for errors
    Logger.info("Checking DRA driver logs for errors...")
    check_dra_driver_logs_no_errors(dra_driver_namespace, environment)

    Logger.info("DRA driver installation validation complete")


def test_dra_driver_gpu_node_labels(dra_driver_install, gpu_cluster, environment):
    """Test that GPU nodes have appropriate labels for DRA.

    This test validates that nodes with AMD GPUs have been properly labeled
    with the required feature labels for DRA resource discovery.

    Validates:
        - All GPU nodes have 'feature.node.kubernetes.io/amd-gpu=true' label
        - Label is present and set to correct value

    Fixtures:
        dra_driver_install: Ensures DRA driver is installed

    kubectl equivalents:
        kubectl get nodes -o jsonpath='{.items[*].metadata.labels}'

    Expected outcome:
        All GPU nodes have AMD GPU feature label
    """
    global Logger

    if environment.amdgpu_driver_spec["driver-deployment"] == "inbox":
        pytest.skip("Inbox driver mode: node labeller not installed, skipping label check")

    # Get GPU nodes via hardware detection (lspci-based, no label dependency)
    hw_gpu_nodes = [node for node in gpu_cluster.cluster_nodes if node.is_gpu_node()]
    K8Helper.triage(environment, len(hw_gpu_nodes) > 0, "No AMD GPU nodes found in cluster")

    # Get all K8s node objects to read their labels
    ret_code, all_k8s_nodes = k8_util.k8_get_nodes()
    K8Helper.triage(environment, ret_code == 0, "Failed to get K8s nodes")

    # Build a map of hostname -> K8s node dict for quick lookup
    k8s_node_map = {
        k8_util.k8_get_node_hostname(n): n
        for n in all_k8s_nodes
        if k8_util.k8_get_node_hostname(n)
    }

    for node in hw_gpu_nodes:
        node_name = node.host_name
        k8s_node = k8s_node_map.get(node_name)
        K8Helper.triage(
            environment,
            k8s_node is not None,
            f"GPU node '{node_name}' not found in K8s node list",
        )

        labels = k8s_node["metadata"].get("labels", {})
        has_amd_gpu_label = (
            "feature.node.kubernetes.io/amd-gpu" in labels
            or "feature.node.kubernetes.io/amd-vgpu" in labels
        )

        K8Helper.triage(
            environment,
            has_amd_gpu_label,
            f"Node {node_name} missing AMD GPU feature labels",
        )

        Logger.info(f"Node {node_name} has proper AMD GPU labels for DRA")


def test_dra_driver_uninstall(
    gpu_cluster,
    dra_driver_release_name,
    dra_driver_namespace,
    dra_driver_install,
    images,
    environment,
):
    """Test DRA driver uninstallation and cleanup.

    This test validates that the DRA driver can be cleanly uninstalled and
    all resources are properly cleaned up. After validation, it reinstalls
    the DRA driver to maintain test isolation.

    Test Flow:
        1. Verify DRA driver is installed
        2. Uninstall DRA driver via Helm
        3. Verify all pods are terminated
        4. Verify DeviceClass is deleted
        5. Reinstall DRA driver (for test isolation)
        6. Verify reinstallation succeeded

    Validates:
        - Helm uninstall succeeds
        - All DRA driver pods are terminated within 30 seconds
        - AMD GPU DeviceClass 'gpu.amd.com' is deleted
        - Reinstall succeeds (pods running)

    Fixtures:
        dra_driver_install: Ensures DRA driver is installed before test
        images: Provides image manifest for reinstallation

    kubectl equivalents:
        helm uninstall <release> -n <namespace>
        kubectl get pods -n <namespace>
        kubectl get deviceclasses.resource.k8s.io
        helm install <release> <chart> -n <namespace>

    Expected outcome:
        DRA driver uninstalls cleanly and reinstalls successfully

    Notes:
        This test should run as part of the full test suite, or explicitly
        when manual cleanup is desired. It maintains test isolation by
        reinstalling after validation.
    """
    global Logger

    # Check if installation exists
    # kubectl equivalent: kubectl get namespaces
    ret_code, k8_namespaces = k8_util.k8_get_namespaces()
    K8Helper.triage(environment, (ret_code == 0), "Error while collecting namespaces")
    namespace_list = list(
        filter(lambda x: x["metadata"]["name"] == dra_driver_namespace, k8_namespaces)
    )
    K8Helper.triage(
        environment,
        (len(namespace_list) == 1),
        f"Missing namespace: {dra_driver_namespace}",
    )

    # Verify helm release exists before uninstalling
    ret_code, ret_stdout, ret_stderr = helm_util.helm_list(
        gpu_cluster, dra_driver_namespace
    )
    K8Helper.triage(environment, (ret_code == 0), "Failed to list helm releases")

    release_exists = False
    for chart in json.loads(ret_stdout):
        if chart["name"] == dra_driver_release_name:
            release_exists = True
            Logger.info(f"Found helm release to uninstall: {chart}")
            break

    K8Helper.triage(
        environment,
        release_exists,
        f"Helm release {dra_driver_release_name} not found for uninstallation",
    )

    # Uninstall DRA driver
    Logger.info(f"Uninstalling DRA driver: {dra_driver_release_name}")
    ret_code, ret_stdout, ret_stderr = helm_util.helm_uninstall(
        gpu_cluster, dra_driver_release_name, dra_driver_namespace
    )
    K8Helper.triage(
        environment, ret_code == 0, f"Failed to uninstall DRA driver: {ret_stderr}"
    )

    # Wait for pods to terminate
    Logger.info("Waiting for DRA driver pods to terminate...")
    time.sleep(30)

    # Verify pods are gone
    # kubectl equivalent: kubectl get pods -n <namespace>
    ret_code, pods = k8_util.k8_get_pods(dra_driver_namespace)
    if ret_code == 0:
        dra_pods = [p for p in pods if DRA_DRIVER_CHART_NAME in p["metadata"]["name"]]
        K8Helper.triage(
            environment,
            len(dra_pods) == 0,
            f"DRA driver pods still exist after uninstall: {[p['metadata']['name'] for p in dra_pods]}",
        )

    # Verify AMD GPU DeviceClass is deleted
    amd_gpu_class_exists, message = check_amd_gpu_deviceclass_exists(environment, "gpu.amd.com")

    if amd_gpu_class_exists:
        Logger.warning(f"AMD GPU DeviceClass 'gpu.amd.com' still exists after uninstall")
    else:
        Logger.info("AMD GPU DeviceClass successfully deleted")

    K8Helper.triage(
        environment,
        not amd_gpu_class_exists,
        "AMD GPU DeviceClass 'gpu.amd.com' was not deleted after uninstall",
    )

    Logger.info("DRA driver successfully uninstalled and cleaned up")

    # Reinstall DRA driver to maintain test isolation
    Logger.info("=" * 70)
    Logger.info("Reinstalling DRA driver for test isolation")
    Logger.info("=" * 70)

    dra_chart = images.get("dra-driver.helm-chart", None)
    dra_version = images.get("dra-driver.version")

    # Add helm repo if using repo:// scheme
    if images.get("dra-driver.repo-name") and images.get("dra-driver.repo"):
        helm_util.helm_add_repo(gpu_cluster, images.get("dra-driver.repo-name"), images.get("dra-driver.repo"))

    K8Helper.triage(
        environment,
        dra_chart is not None,
        "DRA driver helm chart not found in image manifest",
    )

    # Generate values.yaml for DRA driver if needed
    values_yaml = None
    if images.get("image.repository.repository") or images.get("dra-driver-image.repository") or images.get("draDriver.image.repository"):
        values_yaml = os.path.join(
            environment.logdir, f"dra_driver_values_{dra_version}_reinstall.yaml"
        )
        dra_util.generate_dra_driver_values(images, values_yaml)
        Logger.info(f"Using values file: {values_yaml}")

    Logger.info(f"Reinstalling DRA driver:")
    Logger.info(f"  Chart: {dra_chart}")
    Logger.info(f"  Version: {dra_version}")
    Logger.info(f"  Release: {dra_driver_release_name}")
    Logger.info(f"  Namespace: {dra_driver_namespace}")

    ret_code, ret_stdout, ret_stderr = helm_util.helm_install(
        gpu_cluster,
        dra_driver_release_name,
        dra_driver_namespace,
        dra_chart,
        dra_version,
        values_yaml,
    )

    if ret_code != 0:
        Logger.error(f"Helm reinstall failed: {ret_stderr}")
    K8Helper.triage(
        environment,
        ret_code == 0,
        f"Failed to reinstall DRA driver: {ret_stderr}",
    )

    # Wait for DRA driver pods to be ready
    Logger.info("Waiting for DRA driver pods to be ready after reinstall...")
    time.sleep(30)

    # Verify reinstallation
    ret_code, pods = k8_util.k8_get_pods(dra_driver_namespace)
    K8Helper.triage(environment, ret_code == 0, "Failed to get DRA driver pods after reinstall")

    expected_pod_prefix = f"{dra_driver_release_name}-{DRA_DRIVER_CHART_NAME}"
    dra_pods = [p for p in pods if expected_pod_prefix in p["metadata"]["name"]]

    K8Helper.triage(
        environment,
        len(dra_pods) > 0,
        f"No DRA driver pods found after reinstall (expected prefix: {expected_pod_prefix})",
    )

    Logger.info(f"DRA driver successfully reinstalled - found {len(dra_pods)} pod(s)")
    Logger.info("Test isolation maintained - other tests can run in any order")


def test_dra_driver_device_class_create_false(
    gpu_cluster,
    dra_driver_release_name,
    dra_driver_namespace,
    dra_driver_install,
    images,
    environment,
):
    """Test that deviceClass.create=false prevents DeviceClass creation.

    When the Helm value draDriver.deviceClass.create is set to false, the DRA
    driver should deploy pods normally but NOT create the gpu.amd.com DeviceClass.
    This allows users to manage their own DeviceClass objects.

    Test Flow:
        1. Uninstall existing DRA driver
        2. Reinstall with --set deviceClass.create=false
        3. Verify pods are running
        4. Verify DeviceClass gpu.amd.com does NOT exist
        5. Restore: uninstall and reinstall with default settings

    Fixtures:
        dra_driver_install: Ensures DRA driver is installed before test
        images: Provides image manifest for reinstallation
    """
    global Logger

    # Step 1: Uninstall existing DRA driver
    Logger.info("Uninstalling DRA driver for deviceClass.create=false test")
    ret_code, _, ret_stderr = helm_util.helm_uninstall(
        gpu_cluster, dra_driver_release_name, dra_driver_namespace
    )
    K8Helper.triage(
        environment, ret_code == 0, f"Failed to uninstall DRA driver: {ret_stderr}"
    )
    time.sleep(30)

    # Verify DeviceClass is cleaned up after uninstall
    amd_gpu_class_exists, _ = check_amd_gpu_deviceclass_exists(environment, "gpu.amd.com")
    if amd_gpu_class_exists:
        Logger.warning("DeviceClass gpu.amd.com still exists after uninstall, cleaning up manually")
        dra_api_version = dra_util.get_dra_api_version(environment)
        k8_util.k8_delete_custom_resource(
            group="resource.k8s.io",
            version=dra_api_version,
            plural="deviceclasses",
            namespace=None,
            name="gpu.amd.com",
        )
        time.sleep(5)

    # Step 2: Reinstall with deviceClass.create=false
    Logger.info("Reinstalling DRA driver with deviceClass.create=false")
    dra_chart = images.get("dra-driver.helm-chart", None)
    dra_version = images.get("dra-driver.version")

    if images.get("dra-driver.repo-name") and images.get("dra-driver.repo"):
        helm_util.helm_add_repo(
            gpu_cluster, images.get("dra-driver.repo-name"), images.get("dra-driver.repo")
        )

    K8Helper.triage(
        environment,
        dra_chart is not None,
        "DRA driver helm chart not found in image manifest",
    )

    values_yaml = None
    if (
        images.get("image.repository.repository")
        or images.get("dra-driver-image.repository")
        or images.get("draDriver.image.repository")
    ):
        values_yaml = os.path.join(
            environment.logdir, f"dra_driver_values_{dra_version}_no_deviceclass.yaml"
        )
        dra_util.generate_dra_driver_values(images, values_yaml)

    # Install with deviceClass.create=false via --set
    ret_code, ret_stdout, ret_stderr = helm_util.helm_install(
        gpu_cluster,
        dra_driver_release_name,
        dra_driver_namespace,
        dra_chart,
        dra_version,
        values_yaml,
        **{"deviceClass.create": "false"},
    )
    K8Helper.triage(
        environment,
        ret_code == 0,
        f"Failed to install DRA driver with deviceClass.create=false: {ret_stderr}",
    )

    # Step 3: Wait for pods and verify they are running
    Logger.info("Waiting for DRA driver pods...")
    time.sleep(30)

    gpu_node_count = sum(1 for node in gpu_cluster.cluster_nodes if node.is_gpu_node())
    K8Helper.triage(environment, gpu_node_count > 0, "No AMD GPU nodes found in cluster")

    pod_name_prefix = f"{dra_driver_release_name}-{DRA_DRIVER_CHART_NAME}-kubeletplugin"
    expected_pods = [
        common.PodInfo(pod_name_prefix, gpu_node_count, 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(dra_driver_namespace, expected_pods)
    K8Helper.triage(
        environment,
        not failed_pods,
        f"DRA driver pods not ready with deviceClass.create=false - {failed_pods}",
    )
    Logger.info(f"DRA driver pods are running ({gpu_node_count} pod(s))")

    # Step 4: Verify DeviceClass gpu.amd.com was NOT created by the DRA driver chart
    # Note: The GPU operator helm chart also creates a DeviceClass with the same name.
    # We must check ownership via helm annotations to distinguish the two.
    Logger.info("Verifying DeviceClass gpu.amd.com was NOT created by DRA driver chart")
    time.sleep(10)
    dra_api_version = dra_util.get_dra_api_version(environment)
    ret_code, device_classes, err = k8_util.k8_get_custom_resource_objects(
        group="resource.k8s.io", version=dra_api_version, plural="deviceclasses"
    )
    K8Helper.triage(environment, ret_code == 0, f"Failed to get DeviceClasses: {err}")

    dra_driver_created = False
    for dc in (device_classes or []):
        dc_name = dc.get("metadata", {}).get("name", "")
        if dc_name != "gpu.amd.com":
            continue
        annotations = dc.get("metadata", {}).get("annotations", {})
        helm_release = annotations.get("meta.helm.sh/release-name", "")
        helm_ns = annotations.get("meta.helm.sh/release-namespace", "")
        Logger.info(f"  Found DeviceClass gpu.amd.com owned by release='{helm_release}', namespace='{helm_ns}'")
        if helm_release == dra_driver_release_name and helm_ns == dra_driver_namespace:
            dra_driver_created = True

    if not dra_driver_created:
        # DeviceClass may exist from GPU operator — that's expected and fine
        Logger.info("✓ DeviceClass gpu.amd.com was NOT created by DRA driver chart (deviceClass.create=false worked)")
    K8Helper.triage(
        environment,
        not dra_driver_created,
        f"DeviceClass gpu.amd.com was created by DRA driver chart '{dra_driver_release_name}' "
        f"despite deviceClass.create=false",
    )

    # Step 5: Restore — uninstall and reinstall with default settings
    Logger.info("Restoring DRA driver with default deviceClass.create=true")
    ret_code, _, ret_stderr = helm_util.helm_uninstall(
        gpu_cluster, dra_driver_release_name, dra_driver_namespace
    )
    if ret_code != 0:
        Logger.warning(f"Failed to uninstall for restore: {ret_stderr}")
    time.sleep(15)

    ret_code, ret_stdout, ret_stderr = helm_util.helm_install(
        gpu_cluster,
        dra_driver_release_name,
        dra_driver_namespace,
        dra_chart,
        dra_version,
        values_yaml,
    )
    K8Helper.triage(
        environment,
        ret_code == 0,
        f"Failed to restore DRA driver with defaults: {ret_stderr}",
    )

    time.sleep(30)
    failed_pods = k8_util.k8_check_pod_running(dra_driver_namespace, expected_pods)
    K8Helper.triage(
        environment,
        not failed_pods,
        f"DRA driver pods not ready after restore - {failed_pods}",
    )

    # Verify DeviceClass is back
    amd_gpu_class_exists, _ = check_amd_gpu_deviceclass_exists(
        environment, "gpu.amd.com"
    )
    K8Helper.triage(
        environment,
        amd_gpu_class_exists,
        "DeviceClass gpu.amd.com not restored after reinstall with defaults",
    )

    Logger.info("✓ deviceClass.create=false test completed: DeviceClass creation correctly suppressed")
