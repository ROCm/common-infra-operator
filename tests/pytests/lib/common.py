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

import sys
import os
import time
import paramiko
import shutil
import logging
import requests
import hashlib
from datetime import datetime
from fabric import Connection
from invoke.exceptions import UnexpectedExit
from invoke.exceptions import CommandTimedOut
from collections import namedtuple
from enum import Enum
from requests_toolbelt.adapters.host_header_ssl import HostHeaderSSLAdapter

Logger = logging.getLogger("lib.common")

def log_struct_members(struct):
    Logger.info("Struct contents: " + ", ".join(f"{k}={v}" for k, v in vars(struct).items()))

Node = namedtuple("Node", ["IpAddress", "Username", "Password", "Identity", "NodeType", "K8Version", "NodeName"])
PodInfo = namedtuple("PodInfo", ["PodName", "NumInstances", "ContainerCount"])

class TestbedType(Enum):
    K8          = 1
    OPENSHIFT   = 2
    STANDALONE  = 3
    HYPERVISOR  = 4

class cluster_node(object):
    def __init__(self, ip_address = "localhost", user_name = None, password = None, identity = None, node_name = None, node_type = None):
        self._ip_address = ip_address
        self._user_name = user_name
        self._password = password
        self._identity = identity
        self._gpu_series = None
        self._host_name = None
        self._device_id = None
        self._num_gpus = 0
        self._connect_kwargs = {}
        self._amdgpu_driver_version = None
        self._node_name = node_name
        self._node_type = node_type
        if self._password:
            self._connect_kwargs['password'] = self._password
        elif self._identity:
            self._connect_kwargs['key_filename'] = self._identity
        self._host_os_type = "unknown"
        self._host_os_name = "unknown"
        self._host_os_version = "0.0"
        self._k8_version = "NA"
        self._kernel_version = "NA"
        self._ocp_version = "NA"

    @property
    def ip_address(self):
        return self._ip_address

    @property
    def user_name(self):
        return self._user_name

    @user_name.setter
    def user_name(self, user_name):
        self._user_name = user_name

    @property
    def password(self):
        return self._password

    @password.setter
    def password(self, password):
        self._password = password
        self._connect_kwargs['password'] = self._password

    @property
    def identity(self):
        return self._identity

    @property
    def gpu_series(self):
        return self._gpu_series

    @gpu_series.setter
    def gpu_series(self, gpu_series):
        self._gpu_series = gpu_series

    @property
    def amdgpu_driver_version(self):
        return self._amdgpu_driver_version

    @amdgpu_driver_version.setter
    def amdgpu_driver_version(self, version):
        self._amdgpu_driver_version = version

    @property
    def host_name(self):                
        return self._host_name

    @host_name.setter
    def host_name(self, host_name):   
        self._host_name = host_name

    @property
    def num_gpus(self):
        return self._num_gpus

    @num_gpus.setter
    def num_gpus(self, gpu_count):
        self._num_gpus = gpu_count

    @property
    def device_id(self):
        return self._device_id

    @device_id.setter
    def device_id(self, device_id):
        self._device_id = device_id

    @property
    def host_os_type(self):
        return self._host_os_type

    @host_os_type.setter
    def host_os_type(self, host_os):
        self._host_os_type = host_os

    @property
    def host_os_name(self):
        return self._host_os_name

    @host_os_name.setter
    def host_os_name(self, os_name):
        self._host_os_name = os_name

    @property
    def host_os_version(self):
        return self._host_os_version

    @host_os_version.setter
    def host_os_version(self, os_version):
        self._host_os_version = os_version

    @property
    def k8_version(self):
        return self._k8_version

    @k8_version.setter
    def k8_version(self, k8_version):
        self._k8_version = k8_version

    @property
    def node_name(self):
        return self._node_name

    @node_name.setter
    def node_name(self, name):
        self._node_name = name

    @property
    def kernel_version(self):
        return self._kernel_version

    @kernel_version.setter
    def kernel_version(self, version):
        self._kernel_version = version

    @property
    def ocp_version(self):
        return self._ocp_version

    @ocp_version.setter
    def ocp_version(self, version):
        self._ocp_version = version

    @property
    def node_type(self):
        return self._node_type

    @node_type.setter
    def node_type(self, ntype):
        self._node_type = ntype

    def is_gpu_node(self):
        if self._gpu_series != None:
            if self._num_gpus > 0:
                return True
        return False

    def is_local(self):
        return self._ip_address == "localhost"

    def run_command(self, cmd, timeout = 90):
        Logger.debug(f"Running Cmd: {cmd} on {self._ip_address}")
        with Connection(self._ip_address, user = self._user_name, connect_kwargs = self._connect_kwargs) as conn:
            try:
                if self.is_local():
                    result = conn.local(cmd, hide = True, in_stream=False, timeout = timeout)
                else:
                    result = conn.run(cmd, hide = True, in_stream=False, timeout = timeout)
                return result.return_code, result.stdout, result.stderr
            except UnexpectedExit as ue:
                return ue.result.exited, ue.result.stdout, ue.result.stderr
            except CommandTimedOut as to:
                return to.result.exited, to.result.stdout, to.result.stderr
        return -1, "", ""

    def run_commands(self, cmd_list, timeout = 90):
        overall_result = 0
        combined_result = {}
        with Connection(self._ip_address, user = self._user_name, connect_kwargs = self._connect_kwargs) as conn:
            for idx, cmd in enumerate(cmd_list):
                Logger.debug(f"Running Cmd: {cmd} on {self._ip_address}")
                try:
                    if self.is_local():
                        result = conn.local(cmd, hide = True, in_stream=False, timeout = timeout)
                    else:
                        result = conn.run(cmd, hide = True, in_stream=False, timeout = timeout)
                    if result.return_code != 0:
                        overall_result = 1
                    combined_result[cmd] = (result.return_code, result.stdout, result.stderr)
                except UnexpectedExit as ue:
                    overall_result = 1
                    combined_result[cmd] = (ue.result.exited, ue.result.stdout, ue.result.stderr)
                except CommandTimedOut as to:
                    overall_result = 1
                    combined_result[cmd] = (to.result.exited, to.result.stdout, to.result.stderr)
        return overall_result, combined_result

    def get(self, remote_file, local_file):
        if not os.path.exists(os.path.dirname(local_file)):
            os.makedirs(os.path.dirname(local_file))
        if self.is_local():
            # Using shutil to copy 
            shutil.copy(remote_file, local_file)
            return True
        else:
            with Connection(self._ip_address, user = self._user_name, connect_kwargs = self._connect_kwargs) as conn:
                conn.get(remote_file, local_file)
            return True
        return False

    def put(self, local_file, remote_file):
        assert os.path.exists(local_file), f"Could not find file : {local_file}"
        if self.is_local():
            os.makedirs(os.path.dirname(remote_file))
            shutil.copy(local_file, remote_file)
            return True
        else:
            with Connection(self._ip_address, user = self._user_name, connect_kwargs = self._connect_kwargs) as conn:
                result = conn.run(f"mkdir -p {os.path.dirname(remote_file)}", in_stream=False)
                if result.return_code == 0:
                    conn.put(local_file, remote_file)
                return result.return_code == 0
        return False

    def http_get(self, http_port, url_suffix, token = None, retries = 5):
        headers = {}
        if token:
            headers = {"Authorization": f"Bearer {token}"}
        url = f"http://{self._ip_address}:{http_port}/{url_suffix}"
        ret_code = 0
        ret_stdout = ""
        ret_stderr = ""
        for _ in range(retries):
            try:
                resp = requests.get(url, headers = headers, verify = False)
                if resp.status_code == 200:
                    return 0, resp.content, ""
                else:
                    ret_code = -1
                    ret_stderr = f"Error : {resp}"
            except Exception as e:
                ret_code = -1
                ret_stderr = f"Exception : {e}"
            time.sleep(5)
        return ret_code, ret_stdout, ret_stderr

    def https_get(self, http_port, url_suffix, token = None, retries = 5):
        headers = {}
        if token:
            headers = {"Authorization": f"Bearer {token}"}
        url = f"https://{self._ip_address}:{http_port}/{url_suffix}"
        ret_code = 0
        ret_stdout = ""
        ret_stderr = ""
        for _ in range(retries):
            try:
                resp = requests.get(url, headers=headers, verify = False)
                if resp.status_code == 200:
                    return 0, resp.content, None
                else:
                    ret_code = -1
                    ret_stderr = f"Error : {resp}"
            except Exception as e:
                ret_code = -1
                ret_stderr = f"Exception : {e}"
            time.sleep(5)
        return ret_code, ret_stdout, ret_stderr

    def session_https_get(self, http_port, url_suffix, retries = 5, cert= None, verify = False, CN= None):
        if CN:
            headers = {"Host": CN }
        url = f"https://{self._ip_address}:{http_port}/{url_suffix}"
        ret_code = 0
        ret_stdout = ""
        ret_stderr = ""
        
        with requests.Session() as session:
            session.mount('https://', HostHeaderSSLAdapter())
            for _ in range(retries):
                try:
                    resp = session.get(url, headers= headers, cert= cert, verify= verify)
                    if resp.status_code == 200:
                        return 0, resp.content, None
                    else:
                        ret_code = -1
                        ret_stderr = f"Error : {resp}"
                except Exception as e:
                    ret_code = -1
                    ret_stderr = f"Exception : {e}"
                time.sleep(5)
        return ret_code, ret_stdout, ret_stderr

    def proxy_http_get(self, http_ip, http_port, url_suffix, token = None, retries = 5):
        url = f"http://{http_ip}:{http_port}/{url_suffix}"
        ret_code = 0
        ret_stdout = ""
        ret_stderr = ""
        for _ in range(retries):
            if self.is_local():
                headers = {}
                if token:
                    headers = {"Authorization": f"Bearer {token}"}
                try:
                    resp = requests.get(url, headers=headers, verify = False)
                    if resp.status_code == 200:
                        return 0, resp.content, None
                    else:
                        ret_code = -1
                        ret_stderr = f"Error : {resp}"
                except Exception as e:
                    ret_code = -1
                    ret_stderr = f"Exception : {e}"
            else:
                '''
                curl -s -k -H "Authorization: Bearer $TOKEN" http://<node-ip>:<port>/metrics
                '''
                cmd = ['curl', '-s']
                if token:
                    cmd.extend(['-k', '-H', f'"Authorization: Bearer {token}"'])
                cmd.append(url)
                ret_code, ret_stdout, ret_stderr = self.run_command(" ".join(cmd))
                if ret_code == 0:
                    return ret_code, ret_stdout, ret_stderr
            time.sleep(5)
        return ret_code, ret_stdout, ret_stderr

    def proxy_https_get(self, http_ip, http_port, url_suffix, token = None, retries = 5):
        url = f"https://{http_ip}:{http_port}/{url_suffix}"
        ret_code = 0
        ret_stdout = ""
        ret_stderr = ""
        for _ in range(retries):
            if self.is_local():
                headers = {}
                if token:
                    headers = {"Authorization": f"Bearer {token}"}
                try:
                    resp = requests.get(url, headers=headers)
                    if resp.status_code == 200:
                        return 0, resp.content, None
                    else:
                        ret_code = -1
                        ret_stderr = f"Error : {resp}"
                except Exception as e:
                    ret_code = -1
                    ret_stderr = f"Exception : {e}"
            else:
                '''
                curl -s -k -H "Authorization: Bearer $TOKEN" https://<node-ip>:<port>/metrics
                '''
                cmd = ['curl', '-s']
                if token:
                    cmd.extend(['-k', '-H', f'"Authorization: Bearer {token}"'])
                cmd.append(url)
                ret_code, ret_stdout, ret_stderr = self.run_command(" ".join(cmd))
                if ret_code == 0:
                    return ret_code, ret_stdout, ret_stderr
            time.sleep(5)
        return ret_code, ret_stdout, ret_stderr

class cluster(object):

    def __init__(self, nodes, testbed_type):
        self._cluster_nodes = nodes
        self._testbed_type = testbed_type
        self._k8_secrets = {}
        self._k8_registry = 'docker.io'
        self._k8_kube_config = None

    @property
    def testbed_type(self):
        return self._testbed_type

    @property
    def cluster_nodes(self):
        return self._cluster_nodes

    @property
    def k8_secrets(self):
        return self._k8_secrets

    @k8_secrets.setter
    def k8_secrets(self, secrets):
        self._k8_secrets = secrets

    @property
    def k8_registry(self):
        return self._k8_registry

    @k8_registry.setter
    def k8_registry(self, registry):
        self._k8_registry = registry

    @property
    def k8_kube_config(self):
        return self._k8_kube_config

    @k8_kube_config.setter
    def k8_kube_config(self, kube_config_path):
        self._k8_kube_config = kube_config_path

    def find_node_by_ip(self, node_ip):
        node = next((n for n in self._cluster_nodes if n.ip_address == node_ip), None)
        return node

    def get_gpu_variants(self):
        return [node.gpu_series for node in self._cluster_nodes if node.gpu_series is not None]

    def find_node_by_gpu_series(self, gpu_series):
        node = next((n for n in self._cluster_nodes if n.gpu_series == gpu_series), None)
        return node

class k8_cluster(cluster):

    @staticmethod
    def BuildK8Cluster(nodes):
        cluster_nodes = list()
        master_nodes = list()
        for node in nodes:
            c_node = cluster_node(node.IpAddress, node.Username, node.Password, node.Identity, node.NodeName, node.NodeType)
            c_node.k8_version = node.K8Version
            cluster_nodes.append(c_node)
            if node.NodeType == "master":
                master_nodes.append(c_node)
        return k8_cluster(cluster_nodes, master_nodes)

    def __init__(self, cluster_nodes, master_nodes):
        super().__init__(cluster_nodes, TestbedType.K8)
        self._k8_masters = master_nodes

    @property
    def k8_master(self):
        return self._k8_masters[0] if self._k8_masters else None

    @property
    def k8_masters(self):
        return self._k8_masters

    def is_mini_kube(self):
        return any(node.gpu_series is not None for node in self._k8_masters)

 
class standalone_gpu_nodes(cluster):

    def __init__(self, ip_info_list):
        super().__init__(ip_info_list, TestbedType.STANDALONE)

class SlurmGpuCluster(cluster):

    def __init__(self):
        pass

def generate_8byte_sha(seed : str) -> str:
    """
    Generates an 8-byte SHA-256 hash from an input string.
    """
    # Encode the input string to bytes (UTF-8 is common)
    input_str = f"{seed}@{time.time()}"
    input_bytes = input_str.encode('utf-8')
    # Create a SHA-256 hash object
    sha256_hash = hashlib.sha256()
    # Update the hash object with the input bytes
    sha256_hash.update(input_bytes)
    # Get the hexadecimal representation of the full hash
    full_hex_digest = sha256_hash.hexdigest()
    # Truncate the hexadecimal string to the first 16 characters (8 bytes * 2 hex chars/byte)
    eight_byte_hex_digest = full_hex_digest[:16]
    return eight_byte_hex_digest

