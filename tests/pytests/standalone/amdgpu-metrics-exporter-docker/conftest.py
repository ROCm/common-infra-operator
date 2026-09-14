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
Pytest configuration and shared fixtures for the AMD Exporter Docker Container
test suite.

Fixtures defined here are available to all test modules under
tests/pytests/standalone/docker/.
"""

import pdb
import pytest
import os
import logging
import time
import requests
from lib import common
from lib import k8_util
from lib import helm_util
from lib.util import K8Helper

Logger = logging.getLogger("standalone.docker.conftest")


def pytest_html_report_title(report):
    report.title = "AMD Exporter Docker Container Validation Test Results"


@pytest.fixture(scope="module")
def run_exporter_docker_container(gpu_cluster, images, environment):
    """
    Deploy AMD Metrics Exporter Docker container to all GPU nodes.

    Setup: pulls the image, creates /tmp/etc/metrics with a reference config,
    and starts the container in daemon mode with GPU device passthrough.

    Teardown: stops and removes the container.

    Yields:
        None
    """
    global Logger
    Logger.debug("Deploy exporter docker container on each node")

    img = None
    if images.get('metricsExporter.image.repository', None):
        img = f"{images['metricsExporter.image.repository']}:{images['metricsExporter.image.version']}"
    else:
        pytest.fail("Missing device-metrics-exporter container image")

    registry_credentials = None
    if images.get('metricsExporter.image.secret', None):
        secret_name = images['metricsExporter.image.secret']
        for entry in gpu_cluster.k8_secrets["secrets"]:
            if entry['name'] == secret_name:
                registry_credentials = (entry["username"], entry["password"])

    config_json_file = os.path.join(environment.logdir, "reference-config.json")
    try:
        url = "https://raw.githubusercontent.com/ROCm/device-metrics-exporter/refs/heads/main/example/config.json"
        resp = requests.get(url, timeout=30)
        K8Helper.triage(environment, (resp.status_code == 200), "Failed to download reference config.json file")
        with open(config_json_file, "wb") as fp:
            fp.write(resp.content)
    except Exception as ae:
        Logger.error(f"Failed to download config.json from {url}, error : {ae}")

    K8Helper.triage(environment, (os.path.exists(config_json_file)), "Failed to download reference config.json file")
    remote_file = "/tmp/etc/metrics/config.json"

    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            ret_code, ret_stdout, ret_stderr = node.run_command("rm -rf /tmp/etc && mkdir -p /tmp/etc/metrics")
            K8Helper.triage(environment, (ret_code == 0), f"Failed init tmp folder /tmp/etc/metrics, error: {ret_stderr}")
            K8Helper.triage(environment, (node.put(config_json_file, remote_file)),
                            "Unable to upload reference config.json")

            if registry_credentials:
                ret_code, ret_stdout, reg_stderr = node.run_command(f"docker login -u {registry_credentials[0]} -p {registry_credentials[1]}")
                Logger.debug(f"Result of docker login - retcode: {ret_code}")

            cmd = f"docker run -d --device=/dev/dri --device=/dev/kfd -p 5000:5000 -v /tmp/etc/metrics:/etc/metrics --name device-metrics-exporter {img}"
            ret_code, ret_stdout, ret_stderr = node.run_command(cmd)
            K8Helper.triage(environment, (ret_code == 0), f"Failed to deploy metrics-exporter container, error : {ret_stderr}")

            if registry_credentials:
                ret_code, ret_stdout, reg_stderr = node.run_command("docker logout")
                Logger.debug(f"Result of docker logout - retcode: {ret_code}")

            for attempt in range(15):
                rc, _, _ = node.run_command("curl -s -o /dev/null -w '%{http_code}' http://localhost:5000/metrics | grep -q 200")
                if rc == 0:
                    break
                time.sleep(2)
            K8Helper.triage(environment, rc == 0,
                            f"[{node.ip_address}] device-metrics-exporter not responding on :5000 after 30s")

    yield

    Logger.debug("Stop and remove exporter docker container on each node")
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            cmd = "docker stop device-metrics-exporter"
            ret_code, ret_stdout, ret_stderr = node.run_command(cmd)
            K8Helper.triage(environment, (ret_code == 0), f"Failed to stop metrics-exporter container, error : {ret_stderr}")

            cmd = "docker rm -f device-metrics-exporter"
            ret_code, ret_stdout, ret_stderr = node.run_command(cmd)
            K8Helper.triage(environment, (ret_code == 0), f"Failed to cleanup metrics-exporter container, error : {ret_stderr}")
    return
