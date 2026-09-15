#!/usr/bin/python3

'''
 Copyright (c) Advanced Micro Devices, Inc. All rights reserved.

 Licensed under the Apache License, Version 2.0 (the \"License\");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at

      http://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an \"AS IS\" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
'''

import pdb
import pytest
import os
import re
import time
import logging
import lib.helm_util as helm_util
import lib.k8_util as k8_util
import lib.spec_util as spec_util
from lib.util import K8Helper
import subprocess

Logger = logging.getLogger("k8.test_anr_deployment")


@pytest.fixture(autouse=True, scope="module")
def skip_module(environment):
    if environment.deployment_mode == "openshift":
        pytest.skip(f"Skipping ANR deployment testcases for {environment.deployment_mode}")
    import lib.anr_util as anr_util
    if not anr_util.k8s_supports_anr():
        pytest.skip("ANR requires K8s 1.30+ (Argo CRD cost budget)")
    return

@pytest.fixture(scope="function")
def gpu_operator_install(gpu_cluster, gpu_operator_release_name, images, environment, request):
    global Logger
    # cleanup - remove gpu-operator helm-chart
    def _cleanup_gpu_operator():
        Logger.info(f"Cleaning up {gpu_operator_release_name} in {environment.gpu_operator_namespace}")
        devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
        for devcfg_name, _ in devcfg_map.items():
            ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)
            if ret_code != 0:
                Logger.error(f"Failed to delete deviceconfig name: {devcfg_name}, error : {ret_stderr}")
        time.sleep(30)

        if helm_util.is_helm_chart_deployed(gpu_cluster, gpu_operator_release_name, environment.gpu_operator_namespace):
            Logger.info(f"helm {gpu_operator_release_name}  - cleanup")
            ret_code, ret_stdout, ret_stderr = helm_util.helm_uninstall(gpu_cluster, gpu_operator_release_name,
                                                                        environment.gpu_operator_namespace)
            if ret_code != 0:
                helm_util.helm_cleanup(gpu_cluster, gpu_operator_release_name, environment.gpu_operator_namespace)
            #k8_util.k8_delete_namespace(environment.gpu_operator_namespace)                                

    _cleanup_gpu_operator()
    request.addfinalizer(_cleanup_gpu_operator)

    if images.get("gpu-operator.repo", None):
        helm_util.helm_add_repo(gpu_cluster, images.get("gpu-operator.repo-name"), images.get("gpu-operator.repo"))

    secret_list = []
    for entry in gpu_cluster.k8_secrets["secrets"]:
        secret_list.append(entry["name"])
    values_yaml = os.path.join(environment.logdir, f"values_{environment.gpu_operator_version}.yaml")
    if spec_util.generate_helmchart_deployment_config(environment.gpu_operator_version, images, secret_list, values_yaml):
        Logger.debug(f"Generated values.yaml for helm-chart install command, {values_yaml}")
    else:
        values_yaml = None

    options = { "crds.defaultCR.install" : "false", }
    options.update(getattr(request, 'param', {}))

    ret_code, ret_stdout, ret_stderr = helm_util.helm_install(gpu_cluster, gpu_operator_release_name,
                                                              environment.gpu_operator_namespace,
                                                              images.get('gpu-operator.helm-chart', None),
                                                              environment.gpu_operator_version, values_yaml, **options)
    if ret_code != 0:
        Logger.error(f"Failed to install helm chart for {gpu_operator_release_name}")
        Logger.error(f"Stdout: {ret_stdout}")
        Logger.error(f"Stderr: {ret_stderr}")
    K8Helper.triage(environment, (ret_code == 0), f"Failed to install {gpu_operator_release_name}")
    time.sleep(30)
    yield

@pytest.fixture(scope="function")
def deviceconfig_install(images, gpu_operator_install, environment, request):
    global Logger

    # cleanup - remove any deviceconfigs
    def _deviceconfig_cleanup():
        devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
        for devcfg_name, _ in devcfg_map.items():
            ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)
            if ret_code != 0:
                Logger.error(f"Failed to delete deviceconfig name: {devcfg_name}, error : {ret_stderr}")
        time.sleep(30)

    _deviceconfig_cleanup()
    request.addfinalizer(_deviceconfig_cleanup)

    class DeviceConfigCRInfo(object):
        pass

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")

    test_config = {
    'metadata.namespace' : environment.gpu_operator_namespace,
    'driver.enable' : True,
    }
    test_config.update(images)

    test_cfg_map = spec_util.build_deviceconfig_cr_template(test_config, gpu_nodes, 'auto_node_remediation', environment.amdgpu_driver_spec)
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
    yield devcfg_info

@pytest.fixture(scope="function")
def argo_install(gpu_cluster, environment, request):
    '''
    kubectl create namespace argo
    kubectl apply -n argo -f "https://github.com/argoproj/argo-workflows/releases/download/v3.6.5/install.yaml"
    '''
    namespace = 'argo' 
    file_path = os.path.join("lib", "files", "argo_install_v3.6.5.yaml")

    def _argo_cleanup():
        Logger.info(f"Starting cleanup for namespace: {namespace}")
        ret_code, namespace_info_list = k8_util.k8_get_namespaces()
        K8Helper.triage(environment, (ret_code == 0), "Error while fetching namespaces from k8-cluster")
        namespace_exists = any(ninfo['metadata']['name'] == namespace for ninfo in (namespace_info_list or []))
        
        if namespace_exists:
            check_cmd = ["kubectl", "get", "-n", namespace, "-f", file_path]
            check_res = subprocess.run(check_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if check_res.returncode == 0:
                kubectl_delete_cmd = ["kubectl", "delete", "-n", namespace, "-f", file_path]
                result = subprocess.run(kubectl_delete_cmd, stdout=subprocess.PIPE, stderr = subprocess.PIPE)
                K8Helper.triage(environment, (result.returncode == 0), f"Failed to delete : {kubectl_delete_cmd}, error: {result}")

            ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_namespace(namespace)
            K8Helper.triage(environment, (ret_code == 0), f"Failed to delete namespace:{namespace}:{ret_stderr}")
            time.sleep(10)

    _argo_cleanup()
    request.addfinalizer(_argo_cleanup)

    ret_code, ret_stdout, ret_stderr = k8_util.k8_create_namespace(namespace)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to create namespace:{namespace}:{ret_stderr}")
    
    if os.path.exists(file_path):
        kubectl_apply_cmd = ["kubectl", "apply", "-n", namespace, "-f", file_path]
        result = subprocess.run(kubectl_apply_cmd, stdout=subprocess.PIPE, stderr = subprocess.PIPE)
        K8Helper.triage(environment, (result.returncode == 0), f"Failed to apply : , error: {result.stderr}")
    else:
        K8Helper.triage(environment, False, f"Argo install file missing at {file_path}")
     
    yield namespace

@pytest.mark.parametrize("gpu_operator_install", [{"crds.defaultCR.install": "true"}], indirect=True)
def test_anr_verify_default_components(gpu_cluster, images, gpu_operator_install, environment):
    global Logger
    
    # check crds are not installed
    crd_names = ['clusterworkflowtemplates.argoproj.io', 'cronworkflows.argoproj.io', 'workflowartifactgctasks.argoproj.io',
                 'workfloweventbindings.argoproj.io', 'workflows.argoproj.io', 'workflowtaskresults.argoproj.io',
                 'workflowtasksets.argoproj.io', 'workflowtemplates.argoproj.io'
                ]              
    missing_crds = k8_util.k8_check_crds(crd_names)
    K8Helper.triage(environment, not missing_crds, f"Missing CRDs post gpu-operator (with remediationWorkflow) installation : {missing_crds}")

    # check workflow controller is not deployed
    controller_name = "amd-gpu-operator-workflow-controller"
    controller_deployment = k8_util.k8_get_deployment(environment.gpu_operator_namespace, controller_name)
    K8Helper.triage(environment, controller_deployment is not None, 
                    f"Argo Workflow Controller deployment '{controller_name}' not found in {environment.gpu_operator_namespace}")
    if controller_deployment:
        ready_replicas = controller_deployment.status.ready_replicas or 0
        available_replicas = controller_deployment.status.replicas or 0
        
        K8Helper.triage(environment, (ready_replicas == available_replicas and ready_replicas > 0), 
                        f"Argo Workflow Controller is not healthy. Ready: {ready_replicas}/{available_replicas}")

    # check default deviceconfig
    devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
    for devcfg_name, devcfg_data in devcfg_map.items():
        remediation = devcfg_data.get('spec', {}).get('remediationWorkflow', {})
        K8Helper.triage(environment, not remediation.get('enable'), f"Remediation is not disabled in {devcfg_name}")
    
@pytest.mark.parametrize("gpu_operator_install", [{"remediation.enabled": "false", "remediation.installCRDs": "false"}], indirect=True)
def test_anr_disable_remediation(gpu_cluster, images, deviceconfig_install, environment):
    global Logger

    crd_names = ['clusterworkflowtemplates.argoproj.io', 'cronworkflows.argoproj.io', 'workflowartifactgctasks.argoproj.io',
                 'workfloweventbindings.argoproj.io', 'workflows.argoproj.io', 'workflowtaskresults.argoproj.io',
                 'workflowtasksets.argoproj.io', 'workflowtemplates.argoproj.io'
                ]      
    present_crds = set(crd_names) -  set(k8_util.k8_check_crds(crd_names))
    K8Helper.triage(environment,  not present_crds, f"CRDs present post gpu-operator (without remediationWorkflow) installation : {present_crds}")

    controller_name = "amd-gpu-operator-workflow-controller"
    controller_deployment = k8_util.k8_get_deployment(environment.gpu_operator_namespace, controller_name)
    K8Helper.triage(environment, controller_deployment is None, 
                    f"Argo Workflow Controller deployment '{controller_name}' found in {environment.gpu_operator_namespace}")
    
    # check workflow controller is not deployed even when remediation is enabled 
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['remediationWorkflow.enable'] = True
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, (ret_code == 0), "Failed to modify deviceconfig CR")

    controller_deployment_post = k8_util.k8_get_deployment(environment.gpu_operator_namespace, controller_name)
    K8Helper.triage(environment, controller_deployment_post is None, 
                    "Argo Controller was unexpectedly deployed after enabling remediation in DeviceConfig.")

    present_crds = set(crd_names) -  set(k8_util.k8_check_crds(crd_names))
    K8Helper.triage(environment,  not present_crds, f"Argo CRDs were unexpectedly created: {present_crds}")

@pytest.mark.parametrize("gpu_operator_install", [{"remediation.enabled": "false", "remediation.installCRDs": "false"}], indirect=True)
def test_preinstalled_argo_workflow(gpu_cluster, images, argo_install, gpu_operator_install, environment, request):
    global Logger
    argo_ns = argo_install

    # verify argo installtion
    controller_name = "workflow-controller"
    controller_deployment = k8_util.k8_get_deployment(argo_ns, controller_name)
    K8Helper.triage(environment, controller_deployment , 
                    f"Argo Workflow Controller deployment '{controller_name}' not found in {argo_ns}")

    ret_code, pods = k8_util.k8_get_pods(argo_ns)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to fetch Argo pods in namespace {argo_ns}")

    # verify gpu operator installation 
    ret_code, pods = k8_util.k8_get_pods(environment.gpu_operator_namespace)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to fetch GPU Operator pods in namespace {environment.gpu_operator_namespace}")

    controller_deployment = k8_util.k8_get_deployment(environment.gpu_operator_namespace, controller_name)
    K8Helper.triage(environment, controller_deployment is None, 
                    f"Argo Workflow Controller deployment '{controller_name}'  present in {environment.gpu_operator_namespace}")
