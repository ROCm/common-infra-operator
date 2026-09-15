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
import logging
from lib import common
from lib import k8_util
from lib import helm_util

Logger = logging.getLogger("exporter.conftest")

def pytest_html_report_title(report):
    # Add a custom title to the report
    report.title = f"AMD GPU Exporter Helmchart Validation Test Results"

@pytest.fixture(scope="session", autouse=True)
def setup_techsupport_args(request, exporter_release_name, environment):
    if environment.tech_support_tool:
        environment.tech_support_tool["args"] = ["-r", exporter_release_name, "all"]

