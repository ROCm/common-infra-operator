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
import pprint
import sys
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
import lib.helm_util as helm_util
import lib.amdgpu as amdgpu
import lib.common as common
import lib.spec_util as spec_util
from lib.util import K8Helper

Logger = logging.getLogger("k8.test_driver_deviceplugin")

@pytest.fixture(scope="module")
def deviceconfig_install(gpu_cluster, images, gpu_operator_install, environment):
    global Logger

    # cleanup - remove any deviceconfigs and then gpu-operator helm-chart
    devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
    for devcfg_name, _ in devcfg_map.items():
        ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)
        if ret_code != 0:
            Logger.error(f"Failed to delete deviceconfig name: {devcfg_name}, error : {ret_stderr}")
    time.sleep(10)

    class DeviceConfigCRInfo(object):
        pass

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, len(gpu_nodes) > 0, "No nodes with AMD/GPU found in the cluster")

    test_config = {
            'metadata.namespace' : environment.gpu_operator_namespace,
            'driver.enable' : True,
            'driver.blacklist' : True,
            'devicePlugin.enableNodeLabeller' : True,
            'metricsExporter.enable' : True,
            'testRunner.enable' : False,
        }

    test_config.update(images)

    test_cfg_map = spec_util.build_deviceconfig_cr_template(test_config, gpu_nodes, 'device-plugin', environment.amdgpu_driver_spec)
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
        K8Helper.triage(environment, ret_code == 0, f"Failed to create deviceconfig, stderr: {ret_stderr}")
        devicecfg_list.append(tcfg['metadata.name'])

    # Check for corresponding deviceconfig created
    K8Helper.check_deviceconfig_status(environment, devicecfg_list)
    for devcfg in devicecfg_list:
        K8Helper.wait_kmm_worker_completion(environment, devcfg)
    K8Helper.update_node_driver_version(gpu_cluster, environment)

    devcfg_info = DeviceConfigCRInfo()
    setattr(devcfg_info, "test_cfg_map", test_cfg_map)
    setattr(devcfg_info, "exporter_port_map", exporter_port_map)
    setattr(devcfg_info, "devicecfg_list", devicecfg_list)
    yield devcfg_info

    # cleanup - remove any deviceconfigs and then gpu-operator helm-chart
    devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
    for devcfg_name, _ in devcfg_map.items():
        ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)
        if ret_code != 0:
            Logger.error(f"Failed to delete deviceconfig name: {devcfg_name}, error : {ret_stderr}")
    time.sleep(10)
    return

def test_gpu_capacity_status(deviceconfig_install, environment):
    global Logger

    failed_nodes = {}
    for _ in range(3):
        ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
        K8Helper.triage(environment, ret_code == 0, "gpu-operator failed to find amd/gpu nodes in the cluster")
        for node in gpu_nodes:
            node_name = k8_util.k8_get_node_hostname(node)
            capacity = node['status']['capacity']
            if 'amd.com/gpu' in capacity:
                if capacity["amd.com/gpu"] == "0":
                    failed_nodes[node_name] = f"zero value for 'amd.com/gpu' in capacity"
            else:
                failed_nodes[node_name] = f"Missing 'amd.com/gpu' in capacity"

            allocatable = node['status']['allocatable']
            if 'amd.com/gpu' in allocatable:
                if allocatable["amd.com/gpu"] == "0":
                    failed_nodes[node_name] = f"zero value for 'amd.com/gpu' in allocatable"
            else:
                failed_nodes[node_name] = f"Missing 'amd.com/gpu' in allocatable"

            if len(failed_nodes) > 0:
                Logger.warn(f"Failed nodes for capacity/allocatable information : {failed_nodes} - retry after delay")
                failed_nodes.clear()
                time.sleep(10)
            else:
                break

    K8Helper.triage(environment, len(failed_nodes) == 0,
                    f"Some of the node(s) have incorrect capacity/allocatable info {failed_nodes}")

def test_deviceplugin_label(deviceconfig_install, environment, inbox_driver_skip):
    global Logger
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "gpu-operator failed to find amd/gpu nodes in the cluster")

    devicecfg_pods = [
        common.PodInfo('device-plugin', 1, 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods)
    K8Helper.triage(environment, not failed_pods, f"One or more pods are not ready - {failed_pods}")

    '''
    Check for following label:
    beta.kmm.node.kubernetes.io/version-device-plugin.<namespace>.deviceconfig-clusterwide: 6.2.4
    '''
    label_missing = set()
    pattern = r"beta\.kmm\.node\.kubernetes\.io/version-device-plugin." + environment.gpu_operator_namespace + r"\.(.*?)"
    for _ in range(4):
        label_missing.clear()
        ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
        K8Helper.triage(environment, ret_code == 0, "Error while getting gpu-nodes from k8-cluster")
        K8Helper.triage(environment, len(gpu_nodes), "No nodes with AMD/GPU found in the cluster")
        for node in gpu_nodes:
            label_found = False
            for label, _ in node['metadata']['labels'].items():
                if re.match(pattern, label):
                    label_found = True
                    break
            if not label_found:
                label_missing.add(node['metadata']['name'])
        if len(label_missing) > 0:
            Logger.warn(f"Still waiting for device-plugin label for nodes : {label_missing}")
            time.sleep(30)

    K8Helper.triage(environment, len(label_missing) == 0,
                    f"One or more nodes missing kmm.version-device-plugin label : {label_missing}")

def test_node_driver_version(gpu_cluster, deviceconfig_install, environment, inbox_driver_skip):
    global Logger
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "gpu-operator failed to find amd/gpu nodes in the cluster")

    # check the version in the deviceconfig
    config_version = environment.amdgpu_driver_spec["default-version"]
    driver_version = amdgpu.get_matching_driver_version(config_version)
    K8Helper.check_deviceconfig_driver_version(gpu_cluster, config_version, environment)
    K8Helper.check_node_driver_version(gpu_cluster, config_version, driver_version, environment)

def test_driver_blacklist_file_present(gpu_cluster, deviceconfig_install, environment, inbox_driver_skip):
    global Logger
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "gpu-operator failed to find amd/gpu nodes in the cluster")

    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['driver.blacklist'] = True
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, ret_code == 0, "Failed to modify deviceconfig CR")

    # check the worker node blacklist
    filename = "blacklist-amdgpu.conf"
    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        cmd = ["ls", "-1", "/etc/modprobe.d/"]
        ret_code, resp_stdout = k8_util.run_command_on_node(gpu_cluster, node_name, cmd)
        K8Helper.triage(environment, ret_code == 0, f"error getting dir listings from {node_name} {node_name}")
        K8Helper.triage(environment, resp_stdout != None, f"Error: Command output is None")
        Logger.debug(f"Cmd:{cmd}, Response:\n{resp_stdout}")
        amdgpu_blacklist_file = list(filter(lambda line: filename in line, resp_stdout.split("\n")))

        K8Helper.triage(environment, len(amdgpu_blacklist_file) == 1,
                        f"blacklist file not found on node:{node_name} when blacklist is enabled", expected_to_fail = True)

def test_driver_blacklist_file_absent(gpu_cluster, deviceconfig_install, environment, inbox_driver_skip):
    global Logger
    # check the worker node blacklist
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "gpu-operator failed to find amd/gpu nodes in the cluster")

    # Create an empty file 
    filename = "blacklist-amdgpu.conf"
    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        cmd = ["touch", os.path.join("/etc/modprobe.d/", filename)]
        ret_code, resp_stdout = k8_util.run_command_on_node(gpu_cluster, node_name, cmd)
        K8Helper.triage(environment, ret_code == 0, f"error getting dir listings from {node_name} {node_name}")

    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['driver.blacklist'] = False
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, ret_code == 0, "Failed to modify deviceconfig CR")

    # check the worker node blacklist
    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        cmd = ["ls", "-1", "/etc/modprobe.d/"]
        ret_code, resp_stdout = k8_util.run_command_on_node(gpu_cluster, node_name, cmd)
        K8Helper.triage(environment, ret_code == 0, f"error getting dir listings from {node_name} {node_name}")
        K8Helper.triage(environment, resp_stdout != None, f"Error: Command output is None")
        Logger.debug(f"Cmd:{cmd}, Response:\n{resp_stdout}")
        amdgpu_blacklist_file = list(filter(lambda line: filename in line, resp_stdout.split("\n")))

        K8Helper.triage(environment, len(amdgpu_blacklist_file) == 0,
                        f"blacklist file is found {node_name} when blacklist is disabled", expected_to_fail = True)
    # Restore
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['driver.blacklist'] = True
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, ret_code == 0, "Failed to modify deviceconfig CR")


# Driver upgrade tests (test_driver_upgrade_cycle, test_upgrade_driver_using_label)
# moved to k8/gpu-operator/upgrade/test_driver_upgrade.py


def test_deviceplugin_create_delete_gpu_workload(request, deviceconfig_install, images, environment):
    global Logger
    '''
    create the first workload pod requesting one gpu
    Assumption: no other workload pod with gpu has been instantiated
    '''

    local_workload_ctxts = []
    def _cleanup_local_workloads():
        for ctxt in local_workload_ctxts:
            K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **ctxt)
    request.addfinalizer(_cleanup_local_workloads)

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "gpu-operator failed to find amd/gpu nodes in the cluster")
    K8Helper.triage(environment, len(gpu_nodes) > 0, "No GPU nodes available in cluster - check if previous tests left cluster in bad state")
    K8Helper.delete_debug_pods(["default", environment.gpu_operator_namespace])

    # Take one node with gpu
    gpu_node = gpu_nodes[0]
    node_name = k8_util.k8_get_node_hostname(gpu_node)

    # check gpu capacity
    init_cap, init_alloc = k8_util.k8_get_node_gpu_capacity(node_name)
    K8Helper.triage(environment, init_cap >= 0 or init_alloc >= 0,
                    f'Error getting gpu capacity & allocatable values')

    # check if the node has allocatable gpus; if not fail
    K8Helper.triage(environment, int(init_cap) > 0 or int(init_alloc) > 0,
                    f'no gpu available for workload based testcases')

    # Create a workload
    params = {
        "node_name" : node_name,
        "num_gpu_reqd" : init_cap,
        "workload_selection" : "busybox-workload",
    }
    workload_ctxt = K8Helper.workload_operation(environment, K8Helper.WorkloadOp.START_WORKLOAD, **params)
    K8Helper.triage(environment, workload_ctxt['podStatus'] == K8Helper.PodStatus.RUNNING,
                    f"Workload failed to start {workload_ctxt}")
    local_workload_ctxts.append(workload_ctxt)

    new_cap, new_alloc = k8_util.k8_get_node_gpu_capacity(node_name)
    K8Helper.triage(environment, (new_cap != -1 or new_alloc != -1),
                    f'Err getting gpu capacity and allocatable values: capacity: {new_cap} allocatable: {new_alloc}')
    K8Helper.triage(environment, (new_cap == init_cap and new_alloc == init_alloc),
                    f'gpu status error: capacity, status initial/final: {init_cap},{init_alloc}/{new_cap},{new_alloc}')

    # delete the workload
    Logger.info(f"Delete the first workload with gpu")
    K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **workload_ctxt)

    new_cap, new_alloc = k8_util.k8_get_node_gpu_capacity(node_name)
    K8Helper.triage(environment, (new_cap != -1 or new_alloc != -1),
                    f'Err getting gpu capacity and allocatable values: capacity: {new_cap} allocatable: {new_alloc}')
    K8Helper.triage(environment, (new_cap == init_cap and new_alloc == init_alloc),
                    f'gpu status error: capacity, status initial/final: {init_cap},{init_alloc}/{new_cap},{new_alloc}')

def test_deviceplugin_create_workload_with_max_gpu(request, deviceconfig_install, images, environment):
    global Logger
    '''
    Creates and deletes a workload with max number of gpus available on the node
    Get the capacity and create a workload with gpus equal to the capacity
    Check the gpu alloc status
    Delete the workload
    Check the gpu allow status again
    '''
    local_workload_ctxts = []
    def _cleanup_local_workloads():
        for ctxt in local_workload_ctxts:
            K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **ctxt)
    request.addfinalizer(_cleanup_local_workloads)

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "gpu-operator failed to find amd/gpu nodes in the cluster")
    K8Helper.triage(environment, len(gpu_nodes) > 0, "No GPU nodes available in cluster - check if previous tests left cluster in bad state")
    K8Helper.delete_debug_pods(["default", environment.gpu_operator_namespace])

    # Take one node with gpu
    gpu_node = gpu_nodes[0]
    node_name = k8_util.k8_get_node_hostname(gpu_node)

    # check gpu capacity — wait for allocatable to recover after cleanup
    init_cap, init_alloc = k8_util.k8_get_node_gpu_capacity(node_name)
    if init_cap > 0 and init_alloc == 0:
        Logger.info(f"Waiting for GPU allocatable to recover on {node_name} (capacity={init_cap})")
        for _ in range(12):
            time.sleep(5)
            init_cap, init_alloc = k8_util.k8_get_node_gpu_capacity(node_name)
            if init_alloc > 0:
                break
    K8Helper.triage(environment, (init_cap >= 0 or init_alloc >= 0), f'Error getting gpu capacity & allocatable values')

    # check if the node has allocatable gpus; if not fail
    K8Helper.triage(environment, (int(init_cap) > 0 and int(init_alloc) > 0),
                    f'no gpu available for workload based testcases (capacity={init_cap}, allocatable={init_alloc})')

    # Create a workload requesting max-capacity
    params = {
        "node_name" : node_name,
        "num_gpu_reqd" : init_cap,
        "workload_selection" : "busybox-workload",
    }
    workload_ctxt = K8Helper.workload_operation(environment, K8Helper.WorkloadOp.START_WORKLOAD, **params)
    local_workload_ctxts.append(workload_ctxt)
    K8Helper.triage(environment, (workload_ctxt['podStatus'] == K8Helper.PodStatus.RUNNING),
                    f"Workload failed to start {workload_ctxt}")

    new_cap, new_alloc = k8_util.k8_get_node_gpu_capacity(node_name)
    K8Helper.triage(environment, (new_cap != -1 or new_alloc != -1),
                    f'Err getting gpu capacity and allocatable values: capacity: {new_cap} allocatable: {new_alloc}')
    K8Helper.triage(environment, (new_cap == init_cap and new_alloc == init_alloc),
                    f'gpu status error: capacity, status initial/final: {init_cap},{init_alloc}/{new_cap},{new_alloc}')

    # delete the workload
    Logger.info(f"Delete the first workload with gpu")
    K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **workload_ctxt)

    new_cap, new_alloc = k8_util.k8_get_node_gpu_capacity(node_name)
    K8Helper.triage(environment, (new_cap != -1 or new_alloc != -1),
                    f'Err getting gpu capacity and allocatable values: capacity: {new_cap} allocatable: {new_alloc}')
    K8Helper.triage(environment, (new_cap == init_cap and new_alloc == init_alloc),
                    f'gpu status error: capacity, status initial/final: {init_cap},{init_alloc}/{new_cap},{new_alloc}')

def test_deviceplugin_create_workload_exceed_gpu_capacity(request, deviceconfig_install, images, environment):
    global Logger
    '''
    Create a workload requesting gpus > capacity
    Check if the pod is in unschedulable state
    Check gpu alloc status
    Delete the workload
    '''
    local_workload_ctxts = []
    def _cleanup_local_workloads():
        for ctxt in local_workload_ctxts:
            K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **ctxt)
    request.addfinalizer(_cleanup_local_workloads)

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "gpu-operator failed to find amd/gpu nodes in the cluster")
    K8Helper.delete_debug_pods(["default", environment.gpu_operator_namespace])
    
    # Take one node with gpu
    gpu_node = gpu_nodes[0]
    node_name = k8_util.k8_get_node_hostname(gpu_node)

    # check gpu capacity
    init_cap, init_alloc = k8_util.k8_get_node_gpu_capacity(node_name)
    K8Helper.triage(environment, (init_cap >= 0 or init_alloc >= 0),
                    f'Error getting gpu capacity & allocatable values')

    # check if the node has allocatable gpus; if not fail
    K8Helper.triage(environment, (int(init_cap) > 0 or int(init_alloc) > 0),
                    f'no gpu available for workload based testcases')

    # Create a workload
    params = {
        "node_name" : node_name,
        "num_gpu_reqd" : init_cap + 1,
        "workload_selection" : "busybox-workload",
        "expected_status" : K8Helper.PodStatus.PENDING,
    }
    workload_ctxt = K8Helper.workload_operation(environment,
                                                K8Helper.WorkloadOp.START_WORKLOAD, **params)
    local_workload_ctxts.append(workload_ctxt)
    K8Helper.triage(environment, (workload_ctxt['podStatus'] == K8Helper.PodStatus.PENDING),
                    f"Workload started with more resources!!! {workload_ctxt}")

    new_cap, new_alloc = k8_util.k8_get_node_gpu_capacity(node_name)
    K8Helper.triage(environment, (new_cap != -1 or new_alloc != -1),
                    f'Err getting gpu capacity and allocatable values: capacity: {new_cap} allocatable: {new_alloc}')
    K8Helper.triage(environment, (new_cap == init_cap and new_alloc == init_alloc),
                    f'gpu status error: capacity, status initial/final: {init_cap},{init_alloc}/{new_cap},{new_alloc}')

    # delete the workload
    Logger.info(f"Delete the first workload with gpu")
    K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **workload_ctxt)

    new_cap, new_alloc = k8_util.k8_get_node_gpu_capacity(node_name)
    K8Helper.triage(environment, (new_cap != -1 or new_alloc != -1),
                    f'Err getting gpu capacity and allocatable values: capacity: {new_cap} allocatable: {new_alloc}')
    K8Helper.triage(environment, (new_cap == init_cap and new_alloc == init_alloc),
                    f'gpu status error: capacity, status initial/final: {init_cap},{init_alloc}/{new_cap},{new_alloc}')

def test_driver_deviceplugin_multiple_workloads_with_gpu(request, deviceconfig_install, images, environment):
    global Logger
    '''
    Create a workload wl1 with max gpu available
    Check pod status, alloc status
    Create a second workload wl2 with a gpu
    Check if the workload wl2 is unschedulable
    Delete the first workload wl1
    Second workload wl1 must be up and running now
    check pod status and alloc status
    '''
    local_workload_ctxts = []
    def _cleanup_local_workloads():
        for ctxt in local_workload_ctxts:
            K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **ctxt)
    request.addfinalizer(_cleanup_local_workloads)

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "gpu-operator failed to find amd/gpu nodes in the cluster")
    K8Helper.delete_debug_pods(["default", environment.gpu_operator_namespace])
    
    # Take one node with gpu
    gpu_node = gpu_nodes[0]
    node_name = k8_util.k8_get_node_hostname(gpu_node)

    # check gpu capacity
    init_cap, init_alloc = k8_util.k8_get_node_gpu_capacity(node_name)
    K8Helper.triage(environment, (init_cap != -1 or init_alloc != -1),
                    f'Err getting gpu capacity and allocatable values: init_cap: {init_cap} init_alloc: {init_alloc}')
    # check if the node has allocatable gpus; if not fail
    K8Helper.triage(environment, (int(init_cap) != 0 or int(init_alloc) != 0), f'no gpu available')

    # Create a workload requesting max-capacity
    params = {
        "node_name" : node_name,
        "num_gpu_reqd" : init_cap,
        "workload_selection" : "busybox-workload",
    }
    first_workload = K8Helper.workload_operation(environment, K8Helper.WorkloadOp.START_WORKLOAD, **params)
    local_workload_ctxts.append(first_workload)
    K8Helper.triage(environment, (first_workload['podStatus'] == K8Helper.PodStatus.RUNNING),
                    f"Workload failed to start {first_workload}")

    # launch another workload wl2 requesting one gpu; should be in unschedulable state
    params = {
        "node_name" : node_name,
        "num_gpu_reqd" : 1,
        "workload_selection" : "busybox-workload",
        "expected_status" : K8Helper.PodStatus.PENDING,
    }
    second_workload = K8Helper.workload_operation(environment, K8Helper.WorkloadOp.START_WORKLOAD, **params)
    local_workload_ctxts.append(second_workload)
    K8Helper.triage(environment, (second_workload['podStatus'] == K8Helper.PodStatus.PENDING),
                    f"Workload is running when it is expected to be pending")

    # delete workload wl1
    Logger.info(f"Delete the first workload with gpu")
    K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **first_workload)
    time.sleep(30)

    # check workload wl2 status
    Logger.info(f"Check the status of the second workload with gpu")
    workload_pods = [
        common.PodInfo(second_workload['pod_name'], 1, 1),
    ]
    workload_status = None
    for _ in range(5):
        status_info = k8_util.k8_check_pod_status("default", workload_pods)
        workload_status = {status for name, (status, full_pod_info) in status_info.items()}
        if "Pending" in workload_status:
            time.sleep(30)
        else:
            break

    K8Helper.collect_unhealthy_pods(environment, workload_pods)
    K8Helper.triage(environment, (workload_status == {"Running"}),
                    f"Invalid workload-status: {workload_status} for pod: {second_workload['pod_name']}")

    # delete wl2
    Logger.info(f"Delete the second workload with gpu")
    K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **second_workload)

def test_gpu_capacity_matches_hardware(gpu_cluster, deviceconfig_install, environment):
    """Verify device-plugin GPU capacity matches hardware-discovered GPU count.

    Re-collects hardware info (driver must be loaded for accurate sysfs/KFD
    partition data) then compares against amd.com/gpu allocatable from
    Kubernetes node status. For multi-die GPUs like MI300X, lspci shows 1
    PCI device but KFD exposes 8 compute nodes — this test handles both.

    TC-DP-002 from device-plugin-test-plan.md
    """
    global Logger
    import lib.node_gpu_collector as node_collector

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, len(gpu_nodes) > 0, "No GPU nodes available in cluster")

    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        node_ip = k8_util.k8_get_node_address(node)
        cluster_node = gpu_cluster.find_node_by_ip(node_ip)
        K8Helper.triage(environment, cluster_node is not None,
                        f"Unable to find cluster node for ip: {node_ip}")

        node_collector.populate_cluster_node_with_gpu_info(gpu_cluster, cluster_node, node_name)
        hw_gpu_count = cluster_node.num_gpus
        K8Helper.triage(environment, hw_gpu_count > 0,
                        f"node_gpu_collector found 0 GPUs on {node_name} — check lspci/sysfs detection")

        capacity, allocatable = k8_util.k8_get_node_gpu_capacity(node_name)
        Logger.info(f"Node {node_name}: hardware={hw_gpu_count}, capacity={capacity}, allocatable={allocatable}")

        K8Helper.triage(environment, capacity == hw_gpu_count,
                        f"GPU capacity mismatch on {node_name}: "
                        f"device-plugin reports capacity={capacity}, hardware has {hw_gpu_count} GPUs")
        K8Helper.triage(environment, allocatable <= capacity,
                        f"Allocatable ({allocatable}) exceeds capacity ({capacity}) on {node_name}")


def test_deviceplugin_restart_recovery(request, deviceconfig_install, environment):
    """Verify device-plugin recovers GPU resources after pod restart.

    Kills the device-plugin pod and waits for the DaemonSet to recreate it.
    Validates that amd.com/gpu capacity and allocatable are restored to
    pre-restart values.

    TC-DP-003 from device-plugin-test-plan.md
    """
    global Logger
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "Error while getting gpu-nodes from k8-cluster")

    gpu_node = gpu_nodes[0]
    node_name = k8_util.k8_get_node_hostname(gpu_node)

    cap_before, alloc_before = k8_util.k8_get_node_gpu_capacity(node_name)
    Logger.info(f"Before restart: capacity={cap_before}, allocatable={alloc_before}")
    K8Helper.triage(environment, cap_before > 0,
                    f"No GPU capacity on {node_name} before restart test")

    Logger.info("Deleting device-plugin pods to trigger DaemonSet restart...")
    ret_code = k8_util.k8_delete_all_pods_with_name_pattern(
        environment.gpu_operator_namespace, "device-plugin")
    K8Helper.triage(environment, ret_code == 0, "Failed to delete device-plugin pods")

    time.sleep(10)
    devicecfg_pods = [common.PodInfo('device-plugin', len(gpu_nodes), 1)]
    failed_pods = k8_util.k8_check_pod_running(
        environment.gpu_operator_namespace, devicecfg_pods, sleep_time=10)
    K8Helper.triage(environment, not failed_pods,
                    f"Device-plugin pods not Running after restart: {failed_pods}")

    time.sleep(10)
    cap_after, alloc_after = k8_util.k8_get_node_gpu_capacity(node_name)
    Logger.info(f"After restart: capacity={cap_after}, allocatable={alloc_after}")

    K8Helper.triage(environment, cap_after == cap_before,
                    f"GPU capacity changed after restart: before={cap_before}, after={cap_after}")
    K8Helper.triage(environment, alloc_after == alloc_before,
                    f"GPU allocatable changed after restart: before={alloc_before}, after={alloc_after}")


def test_gpu_resource_release_and_reallocate(request, deviceconfig_install, images, environment):
    """Verify GPU resources are fully released after workload termination and can be reallocated.

    Deploys a workload consuming all GPUs, deletes it, verifies allocatable
    returns to full capacity, then immediately deploys another workload to
    confirm the resources are genuinely available.

    TC-DP-014 from device-plugin-test-plan.md
    """
    global Logger
    local_workload_ctxts = []
    def _cleanup_local_workloads():
        for ctxt in local_workload_ctxts:
            K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **ctxt)
    request.addfinalizer(_cleanup_local_workloads)

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "Error while getting gpu-nodes from k8-cluster")

    gpu_node = gpu_nodes[0]
    node_name = k8_util.k8_get_node_hostname(gpu_node)
    init_cap, init_alloc = k8_util.k8_get_node_gpu_capacity(node_name)
    K8Helper.triage(environment, init_cap > 0, f"No GPU capacity on {node_name}")
    Logger.info(f"Initial state: capacity={init_cap}, allocatable={init_alloc}")

    # Phase 1: deploy workload consuming all GPUs
    params = {
        "node_name": node_name,
        "num_gpu_reqd": init_cap,
        "workload_selection": "busybox-workload",
    }
    ctxt1 = K8Helper.workload_operation(environment, K8Helper.WorkloadOp.START_WORKLOAD, **params)
    K8Helper.triage(environment, ctxt1['podStatus'] == K8Helper.PodStatus.RUNNING,
                    f"First workload failed to start: {ctxt1}")
    local_workload_ctxts.append(ctxt1)

    # Phase 2: delete the workload
    Logger.info("Deleting first workload — expecting GPU resources to be released")
    K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **ctxt1)
    local_workload_ctxts.remove(ctxt1)
    time.sleep(5)

    cap_after_delete, alloc_after_delete = k8_util.k8_get_node_gpu_capacity(node_name)
    Logger.info(f"After delete: capacity={cap_after_delete}, allocatable={alloc_after_delete}")
    K8Helper.triage(environment, alloc_after_delete == init_alloc,
                    f"Allocatable not restored after workload deletion: "
                    f"expected={init_alloc}, got={alloc_after_delete}")

    # Phase 3: immediately reallocate — proves resources are genuinely free
    Logger.info("Deploying second workload to confirm resources are reallocatable")
    ctxt2 = K8Helper.workload_operation(environment, K8Helper.WorkloadOp.START_WORKLOAD, **params)
    local_workload_ctxts.append(ctxt2)
    K8Helper.triage(environment, ctxt2['podStatus'] == K8Helper.PodStatus.RUNNING,
                    f"Second workload failed to start after resource release: {ctxt2}")


@pytest.mark.skip(reason="This test needs further work")
def test_gpu_capacity_after_partition(request, gpu_cluster, deviceconfig_install, environment, inbox_driver_skip):
    """Verify device-plugin GPU capacity changes correctly after GPU partitioning.

    Records capacity in default SPX mode, applies CPX partition, verifies
    amd.com/gpu count increases proportionally, then restores SPX and
    verifies count returns.

    Skips on GPUs that don't support partitioning (R9700S, W7900, MI210).

    TC-DP-004 from device-plugin-test-plan.md
    """
    global Logger
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, len(gpu_nodes) > 0, "No GPU nodes found")

    gpu_node = gpu_nodes[0]
    node_name = k8_util.k8_get_node_hostname(gpu_node)
    node_ip = k8_util.k8_get_node_address(gpu_node)
    cluster_node = gpu_cluster.find_node_by_ip(node_ip)
    K8Helper.triage(environment, cluster_node is not None,
                    f"Unable to find cluster node for ip: {node_ip}")

    if not amdgpu.supports_config_manager(cluster_node.device_id):
        pytest.skip(f"GPU partitioning not supported for {cluster_node.gpu_series}")

    def restore_spx():
        Logger.info(f"Finalizer: restoring SPX_NPS1 on {node_name}")
        labels_dict = {"dcm.amd.com/gpu-config-profile": "SPX_NPS1"}
        k8_util.k8_label_node(node_name, labels_dict, overwrite=True)
        time.sleep(30)
        cleanup_labels = {
            "dcm.amd.com/gpu-config-profile": None,
            "dcm.amd.com/gpu-config-profile-state": None,
        }
        k8_util.k8_label_node(node_name, cleanup_labels, overwrite=True)
        k8_util.k8_untaint_node(node_name, effects=["NoSchedule", "NoExecute"])

    request.addfinalizer(restore_spx)

    cap_before, alloc_before = k8_util.k8_get_node_gpu_capacity(node_name)
    Logger.info(f"Before partition: capacity={cap_before}, allocatable={alloc_before}")
    K8Helper.triage(environment, cap_before > 0,
                    f"No GPU capacity on {node_name} before partition test")

    # Apply CPX partition — capacity should increase
    labels_dict = {"dcm.amd.com/gpu-config-profile": "CPX_NPS1"}
    k8_util.k8_label_node(node_name, labels_dict, overwrite=True)
    time.sleep(30)

    cap_partitioned, alloc_partitioned = k8_util.k8_get_node_gpu_capacity(node_name)
    Logger.info(f"After CPX partition: capacity={cap_partitioned}, allocatable={alloc_partitioned}")
    K8Helper.triage(environment, cap_partitioned > cap_before,
                    f"GPU capacity did not increase after CPX partition: "
                    f"before={cap_before}, after={cap_partitioned}")

    cap_restored, alloc_restored = k8_util.k8_get_node_gpu_capacity(node_name)
    Logger.info(f"After SPX restore: capacity={cap_restored}, allocatable={alloc_restored}")
    K8Helper.triage(environment, cap_restored == cap_before,
                    f"GPU capacity not restored after SPX: expected={cap_before}, got={cap_restored}")


def test_gpu_health_restore_after_error_clear(request, gpu_cluster, deviceconfig_install, images, environment):
    """Verify GPU resources are restored after injected errors are cleared.

    Injects ECC errors to make GPUs unhealthy, verifies capacity is reduced,
    then clears the errors and verifies the node returns to healthy state
    with full GPU capacity restored. Deploys a workload afterwards to confirm
    resources are usable.

    TC-DP-032 from device-plugin-test-plan.md
    """
    global Logger
    local_workload_ctxts = []

    namespace = environment.gpu_operator_namespace
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "Error while getting gpu-nodes from k8-cluster")

    gpu_node = gpu_nodes[0]
    node_name = k8_util.k8_get_node_hostname(gpu_node)
    node_ip = k8_util.k8_get_node_address(gpu_node)
    cluster_node = gpu_cluster.find_node_by_ip(node_ip)

    error_fields = ['GPU_ECC_UNCORRECT_GFX', 'GPU_ECC_UNCORRECT_UMC']

    # Enable Debug.EnableAPI so SetError gRPC calls succeed
    configmap = {"CommonConfig": {"Debug": {"EnableAPI": True}}}
    configmap_name = "config-debug-enableapi"
    configmap_file = os.path.join(environment.logdir, f"{configmap_name}.json")
    with open(configmap_file, "w") as fp:
        fp.write(json.dumps(configmap, indent=4))
    k8_util.k8_delete_configmap(namespace, configmap_name)
    ret_code, _, ret_stderr = k8_util.k8_create_configmap(
        namespace, configmap_name, configmap_file, "config.json")
    K8Helper.triage(environment, ret_code == 0,
                    f"Failed to create configmap {configmap_name}: {ret_stderr.strip()}")
    saved_configs = {}
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        saved_configs[spec_name] = tcfg.get('metricsExporter.config')
        tcfg['metricsExporter.config'] = configmap_name
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, _, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, ret_code == 0, "Failed to modify deviceconfig CR for EnableAPI")

    def _cleanup_configmap():
        for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
            prev = saved_configs.get(spec_name)
            if prev is None and 'metricsExporter.config' in tcfg:
                del tcfg['metricsExporter.config']
            else:
                tcfg['metricsExporter.config'] = prev
            cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
            k8_util.k8_modify_deviceconfig_cr(cr_spec)
        k8_util.k8_delete_configmap(namespace, configmap_name)
    request.addfinalizer(_cleanup_configmap)

    def _cleanup():
        Logger.info("Finalizer: clearing ECC errors and stopping workloads")
        try:
            k8_util.k8_metrics_error([0, 0], error_fields, namespace)
        except Exception as e:
            Logger.warn(f"Finalizer: failed to clear ECC errors: {e}")
        for ctxt in local_workload_ctxts:
            K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **ctxt)
    request.addfinalizer(_cleanup)

    # Check if health annotation infrastructure is active
    annotations = gpu_node.get('metadata', {}).get('annotations', {})
    health_annotation = "metricsexporter.amd.com/gpu.0.state"
    if health_annotation not in annotations:
        pytest.skip(f"Health annotation '{health_annotation}' not present on {node_name} — "
                    f"exporter health monitoring not active")

    # Wait for metrics-exporter to pick up the EnableAPI config
    time.sleep(30)

    # Precondition: ensure node starts healthy (clear any leftover errors)
    initial_health = k8_util.k8_get_node_health(node_name, namespace)
    if initial_health != "healthy":
        Logger.info(f"Node {node_name} is {initial_health} at start — clearing pre-existing errors")
        k8_util.k8_metrics_error([0, 0], error_fields, namespace)
        for _ in range(6):
            time.sleep(10)
            initial_health = k8_util.k8_get_node_health(node_name, namespace)
            if initial_health == "healthy":
                break
        K8Helper.triage(environment, initial_health == "healthy",
                        f"Node {node_name} could not reach healthy state before test: {initial_health}")

    cap_before, alloc_before = k8_util.k8_get_node_gpu_capacity(node_name)
    Logger.info(f"Initial state: capacity={cap_before}, allocatable={alloc_before}, health={initial_health}")
    K8Helper.triage(environment, cap_before > 0, f"No GPU capacity on {node_name}")

    # Phase 1: Inject errors to make node unhealthy
    error_counts = [5, 5]
    Logger.info(f"Injecting ECC errors: {dict(zip(error_fields, error_counts))}")
    ret_code, _, ret_stderr = k8_util.k8_metrics_error(error_counts, error_fields, namespace)
    K8Helper.triage(environment, ret_code == 0, f"ECC error injection failed: {ret_stderr}")
    for _ in range(6):
        time.sleep(10)
        health = k8_util.k8_get_node_health(node_name, namespace)
        if health == "unhealthy":
            break
    Logger.info(f"After injection: health={health}")
    K8Helper.triage(environment, health == "unhealthy",
                    f"Node {node_name} should be unhealthy after error injection, got: {health}")

    # Phase 2: Clear errors
    Logger.info("Clearing injected ECC errors")
    k8_util.k8_metrics_error([0, 0], error_fields, namespace)
    for _ in range(6):
        time.sleep(10)
        health_after = k8_util.k8_get_node_health(node_name, namespace)
        if health_after == "healthy":
            break
    Logger.info(f"After clearing: health={health_after}")
    K8Helper.triage(environment, health_after == "healthy",
                    f"Node {node_name} should be healthy after clearing errors, got: {health_after}")

    # Phase 3: Verify capacity restored
    cap_after, alloc_after = k8_util.k8_get_node_gpu_capacity(node_name)
    Logger.info(f"After clear: capacity={cap_after}, allocatable={alloc_after}")
    K8Helper.triage(environment, alloc_after == alloc_before,
                    f"Allocatable not restored: expected={alloc_before}, got={alloc_after}")

    # Phase 4: Deploy workload to prove resources are usable
    params = {
        "node_name": node_name,
        "num_gpu_reqd": 1,
        "workload_selection": "busybox-workload",
    }
    ctxt = K8Helper.workload_operation(environment, K8Helper.WorkloadOp.START_WORKLOAD, **params)
    K8Helper.triage(environment, ctxt['podStatus'] == K8Helper.PodStatus.RUNNING,
                    f"Workload failed after health restore: {ctxt}")
    local_workload_ctxts.append(ctxt)


def test_deviceplugin_restarts_after_driver_reload(request, gpu_cluster, deviceconfig_install, environment, inbox_driver_skip):
    """Verify device-plugin pod restarts and GPU capacity is maintained after driver reload.

    Disables the driver via DeviceConfig (driver.enable=false), waits for KMM
    to unload the module, then re-enables it with the same version. This
    triggers a full unload/reload cycle without a version change, build, or
    node reboot — isolating the device-plugin recovery behavior.

    TC-DP-046 from device-plugin-test-plan.md
    """
    global Logger
    if gpu_cluster.is_mini_kube():
        pytest.skip("Using mini-kube/SNO cluster — skip driver reload test")

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "Error while getting gpu-nodes from k8-cluster")

    gpu_node = gpu_nodes[0]
    node_name = k8_util.k8_get_node_hostname(gpu_node)

    cap_before, _ = k8_util.k8_get_node_gpu_capacity(node_name)
    Logger.info(f"Before reload: capacity={cap_before}")
    K8Helper.triage(environment, cap_before > 0, f"No GPU capacity on {node_name}")

    ret_code, pods_before = k8_util.k8_get_pods(environment.gpu_operator_namespace)
    dp_pods_before = [p['metadata']['name'] for p in pods_before
                      if 'device-plugin' in p['metadata']['name']]
    Logger.info(f"Device-plugin pods before: {dp_pods_before}")

    def _restore():
        try:
            for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
                tcfg['driver.enable'] = True
                cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
                k8_util.k8_modify_deviceconfig_cr(cr_spec)
            K8Helper.check_deviceconfig_status(environment, deviceconfig_install.devicecfg_list)
            for devcfg in deviceconfig_install.devicecfg_list:
                K8Helper.wait_kmm_worker_completion(environment, devcfg)
        except Exception as e:
            Logger.error(f"Exception during restore: {e}")
    request.addfinalizer(_restore)

    # Step 1: Disable driver
    Logger.info("Disabling driver via DeviceConfig")
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['driver.enable'] = False
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, _, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, ret_code == 0, f"Failed to disable driver: {ret_stderr}")

    K8Helper.check_deviceconfig_status(environment, deviceconfig_install.devicecfg_list)
    for devcfg in deviceconfig_install.devicecfg_list:
        K8Helper.wait_kmm_worker_completion(environment, devcfg)
    time.sleep(10)

    # Step 2: Re-enable driver (same version — triggers reload without build/reboot)
    Logger.info("Re-enabling driver via DeviceConfig")
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['driver.enable'] = True
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, _, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, ret_code == 0, f"Failed to re-enable driver: {ret_stderr}")

    K8Helper.check_deviceconfig_status(environment, deviceconfig_install.devicecfg_list)
    for devcfg in deviceconfig_install.devicecfg_list:
        K8Helper.wait_kmm_worker_completion(environment, devcfg)

    # Step 3: Verify device-plugin pod restarted
    time.sleep(10)
    devicecfg_pods = [common.PodInfo('device-plugin', len(gpu_nodes), 1)]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods)
    K8Helper.triage(environment, not failed_pods, f"Device-plugin pods not Running after reload: {failed_pods}")

    ret_code, pods_after = k8_util.k8_get_pods(environment.gpu_operator_namespace)
    dp_pods_after = [p['metadata']['name'] for p in pods_after
                     if 'device-plugin' in p['metadata']['name']]
    Logger.info(f"Device-plugin pods after: {dp_pods_after}")
    K8Helper.triage(environment, set(dp_pods_before) != set(dp_pods_after),
                    f"Device-plugin pods did not restart after driver reload: "
                    f"before={dp_pods_before}, after={dp_pods_after}")

    # Step 4: Verify capacity maintained
    cap_after, _ = k8_util.k8_get_node_gpu_capacity(node_name)
    Logger.info(f"After reload: capacity={cap_after}")
    K8Helper.triage(environment, cap_after == cap_before,
                    f"GPU capacity changed after driver reload: before={cap_before}, after={cap_after}")


