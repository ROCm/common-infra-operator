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

import os
import json
import shlex
import time
import pytest
import logging
import requests
from lib.util import K8Helper

Logger = logging.getLogger("hypervisor.docker.conftest")


def pytest_html_report_title(report):
    report.title = "AMD SR-IOV Exporter Docker Container Validation Test Results"


_CONTAINER_NAME = "sriov-metrics-exporter"
_METRICS_PORT   = 5000
_CONFIG_DIR     = "/tmp/sriov-metrics"
_CONFIG_REMOTE  = f"{_CONFIG_DIR}/config.json"
_REFERENCE_CONFIG_URL = (
    "https://raw.githubusercontent.com/ROCm/device-metrics-exporter"
    "/refs/heads/main/example/config.json"
)


@pytest.fixture(scope="module")
def reference_config(environment):
    """Download the reference config.json from the ROCm repo."""
    config_file = os.path.join(environment.logdir, "reference-config.json")
    try:
        resp = requests.get(_REFERENCE_CONFIG_URL, timeout=30)
        K8Helper.triage(environment, resp.status_code == 200,
                        "Failed to download reference config.json")
        with open(config_file, "wb") as fp:
            fp.write(resp.content)
        with open(config_file) as fp:
            config_data = json.load(fp)
    except Exception as e:
        Logger.error(f"Failed to download reference config: {e}")
        config_data = {}
    K8Helper.triage(environment, os.path.exists(config_file),
                    "Reference config.json not present after download")
    yield (config_file, config_data)


@pytest.fixture(scope="module")
def run_sriov_exporter_docker(gim_node, gpu_cluster, images, reference_config, environment):
    """
    Run the SR-IOV exporter Docker container on the hypervisor.

    Setup:
    1. Resolve the container image from the image manifest
       (key: sriovExporter.image.repository / sriovExporter.image.version).
    2. Upload reference config.json to the hypervisor config volume directory.
    3. Authenticate to the registry if a secret is provided.
    4. Launch the container in privileged mode with the config volume mounted.
    5. Wait briefly for the service to start before yielding.

    The container runs with --privileged so it can access the GIM kernel
    interface (/dev/gim or sysfs) to enumerate and query VF metrics.

    Teardown:
    - docker stop + docker rm -f
    """
    node = gim_node.node
    config_file, _ = reference_config

    if "sriovExporter.image.repository" not in images:
        pytest.skip("sriovExporter.image.repository not found in image manifest")

    img_repo    = images["sriovExporter.image.repository"]
    img_version = images.get("sriovExporter.image.version", "latest")
    img         = f"{img_repo}:{img_version}"

    registry_credentials = None
    if images.get("sriovExporter.image.secret"):
        secret_name = images["sriovExporter.image.secret"]
        secrets = getattr(gpu_cluster, "k8_secrets", {}).get("secrets", [])
        for entry in secrets:
            if entry["name"] == secret_name:
                registry_credentials = (entry["username"], entry["password"])
                break

    # Prepare config volume directory on the hypervisor
    rc, _, stderr = node.run_command(f"rm -rf {_CONFIG_DIR} && mkdir -p {_CONFIG_DIR}")
    K8Helper.triage(environment, rc == 0,
                    f"Failed to create config directory {_CONFIG_DIR}: {stderr}")
    K8Helper.triage(environment, node.put(config_file, _CONFIG_REMOTE),
                    f"Failed to upload reference config to {_CONFIG_REMOTE}")

    if registry_credentials:
        node.run_command(
            f"printf '%s' {shlex.quote(registry_credentials[1])} | "
            f"docker login -u {shlex.quote(registry_credentials[0])} --password-stdin"
        )

    # Remove any stale container from a previous run
    node.run_command(f"docker rm -f {_CONTAINER_NAME} 2>/dev/null || true")

    cmd = (
        f"docker run -d --privileged "
        f"-p {_METRICS_PORT}:{_METRICS_PORT} "
        f"-v {_CONFIG_DIR}:/etc/metrics "
        f"--name {_CONTAINER_NAME} "
        f"{img}"
    )
    rc, _, stderr = node.run_command(cmd)
    K8Helper.triage(environment, rc == 0,
                    f"Failed to start {_CONTAINER_NAME}: {stderr}")

    if registry_credentials:
        node.run_command("docker logout")

    # Give the exporter a moment to start serving
    time.sleep(10)

    yield

    Logger.info(f"Teardown: stopping {_CONTAINER_NAME} on {node.ip_address}")
    node.run_command(f"docker stop {_CONTAINER_NAME} || true")
    rc, _, stderr = node.run_command(f"docker rm -f {_CONTAINER_NAME}")
    K8Helper.triage(environment, rc == 0,
                    f"Failed to remove container {_CONTAINER_NAME}: {stderr}")
