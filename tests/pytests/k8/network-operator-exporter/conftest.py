#!/usr/bin/env python3

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
conftest.py — pytest configuration for Metrics Exporter standalone Helm chart tests.

Automatically writes a timestamped log file for each test run under ./logs/.
"""

import os
import logging
from datetime import datetime

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


def pytest_configure(config):
    """Set up file logging for the entire test session."""
    os.makedirs(LOG_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(LOG_DIR, f"test_run_{timestamp}.log")

    # Root logger — captures all LOG.info / LOG.debug from util.py and tests
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    fh = logging.FileHandler(log_file, mode="w")
    fh.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh.setFormatter(fmt)
    root_logger.addHandler(fh)

    # Store path and handler so we can clean up and print it at the end
    config._me_log_file = log_file
    config._me_log_handler = fh
    logging.getLogger("test_metrics_exporter_standalone_helm").info(
        "Log file: %s", log_file
    )


def pytest_unconfigure(config):
    """Remove file logging handler and print the log file path after the session ends."""
    log_handler = getattr(config, "_me_log_handler", None)
    if log_handler is not None:
        root_logger = logging.getLogger()
        root_logger.removeHandler(log_handler)
        log_handler.close()
    log_file = getattr(config, "_me_log_file", None)
    if log_file and os.path.isfile(log_file):
        print(f"\nTest run log: {log_file}")
