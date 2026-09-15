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
Pytest configuration and shared fixtures for the AMD Exporter Debian Package
test suite.

Fixtures defined here are available to all test modules under
tests/pytests/standalone/debian/.
"""

import pdb
import pytest
import os
import json
import logging
import time
import requests
from lib import common
from lib import k8_util
from lib import helm_util
from lib.util import K8Helper

Logger = logging.getLogger("standalone.debian.conftest")


def pytest_html_report_title(report):
    report.title = "AMD Exporter Debian Package Validation Test Results"


@pytest.fixture(scope="module")
def reference_config(environment):
    """
    Download and provide the reference configuration for AMD Metrics Exporter.

    Downloads the canonical example config.json from the ROCm
    device-metrics-exporter repository.

    Yields:
        tuple: (config_json_file_path, config_data_dict)
    """
    config_json_file = os.path.join(environment.logdir, "reference-config.json")
    try:
        url = "https://raw.githubusercontent.com/ROCm/device-metrics-exporter/refs/heads/main/example/config.json"
        resp = requests.get(url, timeout=30)
        K8Helper.triage(environment, (resp.status_code == 200), "Failed to download reference config.json file")
        with open(config_json_file, "wb") as fp:
            fp.write(resp.content)
        with open(config_json_file) as fp:
            config_data = json.load(fp)
    except Exception as ae:
        Logger.error(f"Failed to download config.json from {url}, error : {ae}")
    K8Helper.triage(environment, (os.path.exists(config_json_file)), "Failed to download reference config.json file")
    yield (config_json_file, config_data)
    return


@pytest.fixture(scope="module")
def deploy_debian_package(gpu_cluster, images, reference_config, environment):
    """
    Deploy AMD Metrics Exporter Debian package to all GPU nodes.

    Setup: installs the package via apt, enables and starts the systemd
    service, and copies the reference config to /etc/metrics/config.json.

    Teardown: uninstalls the package with dpkg -r.

    Yields:
        None
    """
    global Logger
    Logger.debug("Deploy exporter debian package on each node")
    config_json_file, _ = reference_config
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            image_name = f"exporter-debian-{node.host_os_name}-{node.host_os_version}.debian"
            if image_name in images:
                K8Helper.triage(environment, (node.put(config_json_file, "/tmp/config.json")),
                                f"Unable to upload {config_json_file} to /tmp/config.json")
                Logger.info(f"Deploy debian package {images[image_name]} on {node.host_name}")
                remote_file = os.path.join("/tmp", os.path.basename(images[image_name]))
                node.run_command(f"rm -f {remote_file}")
                if node.put(images[image_name], remote_file):
                    ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo apt install -y {remote_file}")
                    K8Helper.triage(environment, (ret_code == 0), f"Failed to install metrics-exporter debian, error : {ret_stderr}")

                    ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo systemctl status amd-metrics-exporter.service")
                    K8Helper.triage(environment, (ret_code != 0), f"Failed to check status metrics-exporter debian, error : {ret_stderr}")

                    ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo systemctl enable amd-metrics-exporter.service")
                    K8Helper.triage(environment, (ret_code == 0), f"Failed to enable metrics-exporter debian, error : {ret_stderr}")

                    ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo systemctl start amd-metrics-exporter.service")
                    K8Helper.triage(environment, (ret_code == 0), f"Failed to start metrics-exporter debian, error : {ret_stderr}")

                    ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo systemctl status amd-metrics-exporter.service")
                    K8Helper.triage(environment, (ret_code == 0), f"Failed to check status metrics-exporter debian, error : {ret_stderr}")

                    for attempt in range(15):
                        rc, _, _ = node.run_command("curl -s -o /dev/null -w '%{http_code}' http://localhost:5000/metrics | grep -q 200")
                        if rc == 0:
                            break
                        time.sleep(2)
                    K8Helper.triage(environment, rc == 0,
                                    f"[{node.ip_address}] amd-metrics-exporter not responding on :5000 after 30s")

                    ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo cp /tmp/config.json /etc/metrics/config.json")
                    K8Helper.triage(environment, (ret_code == 0), f"Unable to create /etc/metrics folder")
            else:
                Logger.error(f"Missing debian package for {image_name} for {node.host_name}")
    yield
    Logger.debug("Uninstall exporter debian package on each node")
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            image_name = f"exporter-debian-{node.host_os_name}-{node.host_os_version}.debian"
            if image_name in images:
                Logger.info(f"Remove debian package on {node.host_name}")
                remote_file = os.path.join("/tmp", os.path.basename(images[image_name]))
                ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo dpkg -r amdgpu-exporter")
                K8Helper.triage(environment, (ret_code == 0), f"Failed to uninstall metrics-exporter debian, error : {ret_stderr}")
                node.run_command(f"rm -f {remote_file}")
            else:
                Logger.error(f"Missing debian package for {image_name} for {node.host_name}")
    return
