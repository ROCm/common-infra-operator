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
test_upgrade_1_1_0_to_throttle.py

End-to-end upgrade test: v1.1.0 -> throttle (v1.2.0-*).

Phases run in strict order. If an upgrade phase fails, all subsequent
phases are skipped.

  Phase 1: Upgrade operator + operands to v1.1.0
  Phase 2: Upgrade operator to throttle (v1.2.0-*)
  Phase 3: Validation (metrics, node-labeller, nicctl, rbac)
  Phase 4: Upgrade operands to throttle (v1.2.0-*)
  Phase 5: Validation (metrics, node-labeller, nicctl, rbac)

Usage:
  pytest test_upgrade_1_1_0_to_throttle.py --html=report.html --self-contained-html
"""

import subprocess
import sys
import os
import pytest

import logging

LOG = logging.getLogger(__name__)
TEST_TIMEOUT = 180

# Directory containing the test files
TEST_DIR = os.path.dirname(os.path.abspath(__file__))

# Track phase failures to skip downstream phases
_phase_failed = {}


def _run_pytest(test_files, phase_label, timeout_sec=3600):
    """
    Run pytest on the given test files as a subprocess.
    Returns (exit_code, stdout+stderr).
    """
    cmd = [
        sys.executable, "-m", "pytest", "-q", "--tb=short",
    ] + [os.path.join(TEST_DIR, f) for f in test_files]

    LOG.info("Phase [%s]: running %s", phase_label, " ".join(test_files))
    try:
        result = subprocess.run(
            cmd,
            cwd=TEST_DIR,
            timeout=timeout_sec,
            capture_output=True,
            text=True,
        )
        LOG.info("Phase [%s] exit code: %d", phase_label, result.returncode)
        if result.stdout:
            LOG.info("Phase [%s] stdout:\n%s", phase_label, result.stdout)
        if result.stderr:
            LOG.info("Phase [%s] stderr:\n%s", phase_label, result.stderr)
        return result.returncode, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        LOG.error("Phase [%s] timed out after %ds", phase_label, timeout_sec)
        return 1, f"Phase {phase_label} timed out after {timeout_sec}s"


def _skip_if_phase_failed(*phases):
    """Skip current test if any of the listed phases failed."""
    for p in phases:
        if _phase_failed.get(p):
            pytest.skip(f"Skipped because phase {p} failed")


# ---------- Phase 1: Upgrade to v1.1.0 ----------

@pytest.mark.timeout(TEST_TIMEOUT)
def test_phase1_upgrade_to_1_1_0():
    """Phase 1: Upgrade operator and operands to v1.1.0."""
    exit_code, output = _run_pytest(
        [
            "test_update_1_1_0_operator.py",
            "test_update_1_1_0_operand.py",
        ],
        phase_label="1-upgrade-1.1.0",
        timeout_sec=3600,
    )
    if exit_code != 0:
        _phase_failed["1"] = True
        pytest.fail(f"Phase 1 (upgrade to v1.1.0) failed with exit code {exit_code}\n{output}")


# ---------- Phase 2: Upgrade operator to throttle ----------

@pytest.mark.timeout(TEST_TIMEOUT)
def test_phase2_upgrade_throttle_operator():
    """Phase 2: Upgrade operator to latest throttle (v1.2.0-*)."""
    _skip_if_phase_failed("1")

    exit_code, output = _run_pytest(
        ["test_update_throttle_operator.py"],
        phase_label="2-throttle-operator",
        timeout_sec=3600,
    )
    if exit_code != 0:
        _phase_failed["2"] = True
        pytest.fail(f"Phase 2 (throttle operator) failed with exit code {exit_code}\n{output}")


# ---------- Phase 3: Validation after throttle operator ----------

@pytest.mark.timeout(max(TEST_TIMEOUT, 10800))
def test_phase3_validation_post_throttle_operator():
    """Phase 3: Run validation tests after throttle operator upgrade."""
    _skip_if_phase_failed("1", "2")

    exit_code, output = _run_pytest(
        [
            "test_network_operator_metrics_exporter.py",
            "test_network_operator_node_labeller.py",
            "test_network_operator_nicctl.py",
            "test_network_operator_rbac_metrics_exporter.py",
        ],
        phase_label="3-validation-post-operator",
        timeout_sec=10800,
    )
    if exit_code != 0:
        _phase_failed["3"] = True
        pytest.fail(f"Phase 3 (validation post throttle operator) failed with exit code {exit_code}\n{output}")


# ---------- Phase 4: Upgrade operands to throttle ----------

@pytest.mark.timeout(TEST_TIMEOUT)
def test_phase4_upgrade_throttle_operand():
    """Phase 4: Upgrade operands to latest throttle (v1.2.0-*)."""
    _skip_if_phase_failed("1", "2")

    exit_code, output = _run_pytest(
        ["test_update_throttle_operand.py"],
        phase_label="4-throttle-operand",
        timeout_sec=3600,
    )
    if exit_code != 0:
        _phase_failed["4"] = True
        pytest.fail(f"Phase 4 (throttle operand) failed with exit code {exit_code}\n{output}")


# ---------- Phase 5: Validation after throttle operands ----------

@pytest.mark.timeout(max(TEST_TIMEOUT, 10800))
def test_phase5_validation_post_throttle_operand():
    """Phase 5: Run validation tests after throttle operand upgrade."""
    _skip_if_phase_failed("1", "2", "4")

    exit_code, output = _run_pytest(
        [
            "test_network_operator_metrics_exporter.py",
            "test_network_operator_node_labeller.py",
            "test_network_operator_nicctl.py",
            "test_network_operator_rbac_metrics_exporter.py",
        ],
        phase_label="5-validation-post-operand",
        timeout_sec=10800,
    )
    if exit_code != 0:
        _phase_failed["5"] = True
        pytest.fail(f"Phase 5 (validation post throttle operand) failed with exit code {exit_code}\n{output}")
