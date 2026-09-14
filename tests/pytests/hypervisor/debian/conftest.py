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
import pytest
import logging
import requests
from lib.util import K8Helper

Logger = logging.getLogger("hypervisor.debian.conftest")

_REFERENCE_CONFIG_URL = (
    "https://raw.githubusercontent.com/ROCm/device-metrics-exporter"
    "/refs/heads/main/example/config.json"
)


def pytest_html_report_title(report):
    report.title = "AMD SR-IOV Exporter Debian Package Validation Test Results"


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
