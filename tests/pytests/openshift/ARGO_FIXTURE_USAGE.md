# Using the argo_workflow_setup Fixture for OpenShift ANR Tests

## Overview

The `argo_workflow_setup` fixture in `tests/pytests/openshift/conftest.py` automatically handles Argo Workflows installation and cleanup for OpenShift ANR tests.

## Fixture Details

**Location:** `tests/pytests/openshift/conftest.py`

**Scope:** `module` - Argo is installed once per test module and cleaned up after all tests in the module complete.

**What it does:**

1. Checks if Argo Workflows is already installed (e.g., via OpenShift AI)
2. Installs Argo (CRDs + controller) if not present
3. Verifies the installation is healthy
4. Cleans up after tests (if installed by fixture)

**Returns:**

```python
{
    "namespace": "argo-workflow",
    "installed_by_fixture": True/False,
    "version": "v3.6.5",
    "preexisting": True/False
}
```

## Usage in test_node_remediation.py

### Option 1: Add as dependency to deviceconfig_install

Modify the `deviceconfig_install` fixture to depend on `argo_workflow_setup`:

```python
@pytest.fixture(scope="module")
def deviceconfig_install(gpu_cluster, images, gpu_operator_install,
                        argo_workflow_setup, environment, request):  # Add argo_workflow_setup
    global Logger

    # Your existing deviceconfig setup code...
    # No need to install Argo manually - the fixture handles it!

    # ... rest of your code ...
```

### Option 2: Use directly in test functions

For specific tests that need Argo:

```python
def test_anr_workflow_openshift(gpu_cluster, images, deviceconfig_install,
                               argo_workflow_setup, environment, request):
    global Logger

    # Argo is already installed and ready
    argo_info = argo_workflow_setup
    Logger.info(f"Using Argo in namespace: {argo_info['namespace']}")

    # Your test code...
```

## Complete Example

Here's how to modify `test_node_remediation.py` for OpenShift:

```python
#!/usr/bin/python3

'''
 Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
'''

import pytest
import time
import logging
import pprint
import lib.k8_util as k8_util
import lib.spec_util as spec_util
import lib.autoremediation_util as anr_util  # Import ANR utilities
from lib.util import K8Helper
from datetime import datetime
from kubernetes import client, watch

Logger = logging.getLogger("k8.test_auto_node_remediation")
LogPrettyPrinter = pprint.PrettyPrinter(indent=2)

# For vanilla Kubernetes, skip this fixture
@pytest.fixture(autouse=True, scope="module")
def skip_module_vanilla_k8s(environment):
    """Skip ANR tests on vanilla Kubernetes (run on OpenShift only)"""
    if environment.deployment_mode != "openshift":
        pytest.skip(f"Skipping OpenShift ANR tests for {environment.deployment_mode}")
    return


@pytest.fixture(scope="module")
def deviceconfig_install(gpu_cluster, images, gpu_operator_install,
                        argo_workflow_setup, environment, request):
    """
    Setup DeviceConfig for ANR testing.

    Depends on argo_workflow_setup to ensure Argo is installed first.
    """
    global Logger

    # Argo is already installed by argo_workflow_setup fixture
    argo_info = argo_workflow_setup
    Logger.info(f"DeviceConfig setup - Argo available in {argo_info['namespace']}")

    # cleanup - remove any deviceconfigs
    def _deviceconfig_cleanup():
        devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
        for devcfg_name, _ in devcfg_map.items():
            ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_deviceconfig_cr(
                environment.gpu_operator_namespace, devcfg_name)
            if ret_code != 0:
                Logger.error(f"Failed to delete deviceconfig name: {devcfg_name}, error : {ret_stderr}")
        time.sleep(10)

    _deviceconfig_cleanup()
    request.addfinalizer(_deviceconfig_cleanup)

    class DeviceConfigCRInfo(object):
        pass

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")

    test_config = {
        'metadata.namespace': environment.gpu_operator_namespace,
        'driver.enable': True,
    }
    test_config.update(images)

    test_cfg_map = spec_util.build_deviceconfig_cr_template(
        test_config, gpu_nodes, 'auto_node_remediation', environment.amdgpu_driver_spec)

    exporter_port_map = {}
    devicecfg_list = []

    if len(test_cfg_map) > 1:
        # Assign unique NodePorts for each deviceconfig instance
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
        ret_code, ret_stdout, ret_stderr = k8_util.k8_create_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, (ret_code == 0), f"Failed to create deviceconfig, stderr: {ret_stderr}")
        devicecfg_list.append(tcfg['metadata.name'])

    # Check for corresponding deviceconfig created
    K8Helper.check_deviceconfig_status(environment, devicecfg_list)
    for devcfg in devicecfg_list:
        K8Helper.wait_kmm_worker_completion(environment, devcfg)

    devcfg_info = DeviceConfigCRInfo()
    setattr(devcfg_info, "test_cfg_map", test_cfg_map)
    setattr(devcfg_info, "exporter_port_map", exporter_port_map)
    setattr(devcfg_info, "devicecfg_list", devicecfg_list)
    setattr(devcfg_info, "argo_info", argo_info)  # Pass Argo info along

    yield devcfg_info


def test_anr_workflow_openshift(gpu_cluster, images, deviceconfig_install,
                                environment, request):
    """Test ANR workflow on OpenShift"""
    global Logger

    def _cleanup_workflow():
        # Cleanup logic
        for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
            tcfg['remediationWorkflow.enable'] = False
            cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
            ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
            K8Helper.triage(environment, (ret_code == 0),
                          f"Failed to disable remediation, stderr: {ret_stderr}")

        # Use ANR utility for comprehensive cleanup
        ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
        for node in gpu_nodes:
            labels = node.get('metadata', {}).get('labels', {})
            if 'node-role.kubernetes.io/control-plane' in labels or \
               'node-role.kubernetes.io/master' in labels:
                continue

            node_name = node['metadata']['labels']['kubernetes.io/hostname']
            anr_util.cleanup_node_for_anr_test(node_name, environment.gpu_operator_namespace)

    request.addfinalizer(_cleanup_workflow)

    ret_code, pods = k8_util.k8_get_pods(environment.gpu_operator_namespace)
    K8Helper.triage(environment, (ret_code == 0),
                   f"Failed to fetch GPU Operator pods in namespace {environment.gpu_operator_namespace}")

    # Enable remediation
    devcfg_name = ''
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['remediationWorkflow.enable'] = True
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, (ret_code == 0),
                       f"Failed to enable remediation, stderr: {ret_stderr}")
        devcfg_name = cr_spec['metadata']['name']

    time.sleep(10)

    # Verify workflow controller deployment (GPU Operator creates its own)
    is_healthy = anr_util.verify_workflow_controller_deployment(
        namespace=environment.gpu_operator_namespace,
        controller_name="amd-gpu-operator-workflow-controller"
    )
    K8Helper.triage(environment, is_healthy,
                   "Workflow controller is not healthy")

    # Verify workflow template created
    ret_code, workflowtemplates, err = k8_util.k8_get_custom_resource_objects(
        group="argoproj.io",
        version="v1alpha1",
        plural="workflowtemplates"
    )
    K8Helper.triage(environment, (ret_code == 0),
                   f"Failed to list workflowtemplates: {err}")
    template_names = [t['metadata']['name'] for t in workflowtemplates or []]
    K8Helper.triage(environment, "default-template" in template_names,
                   "Required WorkflowTemplate 'default-template' not found")

    # Verify configmap created
    ret_code, exists, err = anr_util.verify_remediation_configmap(
        namespace=environment.gpu_operator_namespace,
        deviceconfig_name=devcfg_name
    )
    K8Helper.triage(environment, exists,
                   f"Required remediation configmap not found")

    # Trigger remediation workflow
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0),
                   "Error while getting gpu-nodes from k8-cluster")

    for node in gpu_nodes:
        labels = node.get('metadata', {}).get('labels', {})
        if 'node-role.kubernetes.io/control-plane' in labels or \
           'node-role.kubernetes.io/master' in labels:
            continue

        node_name = node['metadata']['labels']['kubernetes.io/hostname']

        # Set node condition to trigger workflow
        ret_code, resp, err = anr_util.set_node_condition(
            node_name=node_name,
            condition_type="AMDGPUHwsHang",
            status="True",
            reason="GPUHangDetected",
            message="Simulated GPU hang for ANR testing"
        )
        K8Helper.triage(environment, ret_code == 0,
                       f"Failed to set node condition: {err}")

        # Wait for workflow completion (reuse existing function)
        from test_node_remediation import wait_for_argo_workflow
        ret_code, stdout, stderr = wait_for_argo_workflow(
            environment.gpu_operator_namespace, node_name
        )
        K8Helper.triage(environment, (ret_code == 0),
                       f"Workflow failed: {stderr}")

    # Verify cleanup
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    for node in gpu_nodes:
        node_name = node['metadata']['labels']['kubernetes.io/hostname']

        # Verify taint removed
        has_taint = anr_util.check_node_has_taint(
            node_name=node_name,
            taint_key="amd-gpu-unhealthy"
        )
        K8Helper.triage(environment, not has_taint,
                       f"Post-workflow check failed: Taint not removed on {node_name}")

        # Verify condition cleared
        has_condition = anr_util.check_node_has_condition(
            node_name=node_name,
            condition_type="AMDGPUHwsHang",
            expected_status="True"
        )
        K8Helper.triage(environment, not has_condition,
                       f"Node {node_name} still reports GPU Hang condition")
```

## Benefits

✅ **Automatic Management**: No manual Argo installation in tests
✅ **Smart Detection**: Skips installation if Argo already exists
✅ **Clean Isolation**: Module-scoped, won't interfere with other tests
✅ **Proper Cleanup**: Removes Argo only if fixture installed it
✅ **Reusability**: Can be used by multiple test modules
✅ **OpenShift AI Compatible**: Detects pre-existing Argo installations

## Testing the Fixture

Run OpenShift ANR tests:

```bash
# Run all OpenShift ANR tests
pytest tests/pytests/openshift/test_node_remediation.py -v

# Run specific test
pytest tests/pytests/openshift/test_node_remediation.py::test_anr_workflow_openshift -v
```

## Fixture Behavior Summary

| Scenario | Behavior |
| -------- | -------- |
| Argo already installed | Uses existing installation, skips cleanup |
| Argo not installed | Installs Argo + CRDs, cleans up after tests |
| CRDs already exist | Skips CRD installation, installs controller only |
| OpenShift AI present | Detects DataScienceCluster, uses existing Argo |
| Test failure | Still performs cleanup to avoid state pollution |

## Configuration Options

Modify in `openshift/conftest.py` if needed:

```python
argo_namespace = "argo-workflow"  # Change namespace
argo_version = "v3.6.5"           # Change version
remove_crds=False                  # Set to True for complete cleanup
```
