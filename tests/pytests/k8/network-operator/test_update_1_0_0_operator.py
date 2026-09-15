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
test_update_1.0.0_operator.py

Upgrades the amd-network-operator Helm release to v1.0.0 using the
rocm-network helm repo.
"""

import json
import time
import subprocess
import os
import shutil
import pytest

import logging

LOG = logging.getLogger(__name__)
TEST_TIMEOUT = 180

# ---------- Configuration ----------

NC_NAMESPACE = "kube-amd-network"
TARGET_VERSION = "v1.0.0"
ENV_JSON_PATH = os.path.join(os.path.dirname(__file__), "env.json")

NETWORK_OPERATOR_HELM_RELEASE = "amd-network-operator"
HELM_CHART = "rocm-network/network-operator-charts"


# ---------- SSH helpers ----------

def _master_node_from_env_json() -> dict:
    """Read env.json and return master node connection details."""
    try:
        with open(ENV_JSON_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        raise RuntimeError(f"Failed to read {ENV_JSON_PATH}: {exc}") from exc

    try:
        instances = data["Instances"][0]["RawJSON"]["instances"]
    except Exception as exc:
        raise RuntimeError(
            f"Invalid env.json format in {ENV_JSON_PATH}: {exc}"
        ) from exc

    for inst in instances:
        if str(inst.get("type", "")).lower() == "master":
            ip = (inst.get("ip") or "").strip()
            user = (inst.get("username") or "").strip()
            password = inst.get("password") or ""
            if not ip or not user or not password:
                raise RuntimeError(
                    "Master entry in env.json must include ip, username, and password"
                )
            return {"ip": ip, "username": user, "password": password}

    raise RuntimeError(f"No master node found in {ENV_JSON_PATH}")


def _run_on_master(master: dict, command: str, timeout: int = 120) -> str:
    """Run a shell command on the master node via ssh and return stdout."""
    if shutil.which("sshpass") is None or shutil.which("ssh") is None:
        raise RuntimeError("sshpass/ssh not found in PATH; required for master-node commands")

    target = f"{master['username']}@{master['ip']}"
    cmd = [
        "sshpass", "-e",
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        target,
        command,
    ]
    env = {**os.environ, "SSHPASS": master["password"]}
    try:
        out = subprocess.check_output(
            cmd,
            timeout=timeout,
            stderr=subprocess.STDOUT,
            env=env,
        ).decode("utf-8", errors="replace")
        return out
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        output = getattr(exc, "output", b"") or b""
        if isinstance(exc, subprocess.CalledProcessError):
            detail = f"exit status {exc.returncode}"
        elif isinstance(exc, subprocess.TimeoutExpired):
            detail = f"timed out after {timeout}s"
        else:
            detail = exc.__class__.__name__
        raise RuntimeError(
            f"Remote command failed on {target} ({detail}).\n"
            + output.decode("utf-8", errors="replace")[:1000]
        ) from exc


# ---------- Helm helpers ----------

def _helm_current_operator_tag(master: dict) -> str:
    """
    Return the APP VERSION of the running amd-network-operator helm release
    by parsing ``helm list -A`` output.
    """
    out = _run_on_master(master, "helm list -A --no-headers", timeout=30)

    for line in out.splitlines():
        if NETWORK_OPERATOR_HELM_RELEASE in line:
            parts = line.split()
            if parts:
                return parts[-1].strip()
    raise RuntimeError(
        f"Helm release '{NETWORK_OPERATOR_HELM_RELEASE}' not found in 'helm list -A' output.\n"
        f"Output was:\n{out}"
    )


def _helm_upgrade_operator_on_master(master: dict) -> None:
    """Run helm upgrade to v1.0.0 using the rocm-network helm repo."""
    cmd = (
        f"helm upgrade {NETWORK_OPERATOR_HELM_RELEASE} {HELM_CHART} "
        f"-n {NC_NAMESPACE} --create-namespace --version={TARGET_VERSION}"
    )
    LOG.info("Running helm upgrade on master node: %s", cmd)
    out = _run_on_master(master, cmd, timeout=420)
    if out.strip():
        LOG.info("helm upgrade output: %s", out.strip())


def _helm_confirm_upgrade(master: dict, expected_tag: str, retries: int = 20, poll: int = 15) -> str:
    """
    Poll ``helm list -A`` until the running release shows *expected_tag* as
    its APP VERSION.  Returns the confirmed tag.
    """
    LOG.info(
        "Waiting up to %ds for operator helm release to show tag '%s'",
        retries * poll, expected_tag,
    )
    current = ""
    for attempt in range(1, retries + 1):
        try:
            current = _helm_current_operator_tag(master)
        except RuntimeError as exc:
            LOG.warning("Attempt %d/%d: could not read helm release: %s", attempt, retries, exc)
            time.sleep(poll)
            continue

        LOG.info("Attempt %d/%d: current APP VERSION = '%s'", attempt, retries, current)
        if current == expected_tag:
            LOG.info("Operator upgrade confirmed: APP VERSION = '%s'", expected_tag)
            return current
        time.sleep(poll)

    raise TimeoutError(
        f"Operator did not reach tag '{expected_tag}' within {retries * poll}s. "
        f"Last seen: '{current}'"
    )


# ---------- Test ----------

@pytest.mark.timeout(TEST_TIMEOUT)
def test_update_1_0_0_operator():
    """
    Upgrade the amd-network-operator Helm release to v1.0.0 using
    rocm-network/network-operator-charts. Skips if already at v1.0.0.
    """
    if shutil.which("sshpass") is None or shutil.which("ssh") is None:
        pytest.skip("Skipping operator upgrade test: sshpass/ssh are required to run helm on master node")

    try:
        master = _master_node_from_env_json()
    except RuntimeError as exc:
        pytest.fail(str(exc))

    LOG.info("Using master node from env.json: %s@%s", master["username"], master["ip"])
    current_tag = _helm_current_operator_tag(master)
    LOG.info("Current operator APP VERSION: %s", current_tag)

    if current_tag == TARGET_VERSION:
        LOG.info("Operator is already at %s; skipping upgrade.", TARGET_VERSION)
        pytest.skip(f"Operator is already at {TARGET_VERSION}")

    try:
        _helm_upgrade_operator_on_master(master)
    except RuntimeError as exc:
        pytest.fail(str(exc))

    try:
        confirmed = _helm_confirm_upgrade(master, TARGET_VERSION)
    except TimeoutError as exc:
        pytest.fail(str(exc))

    LOG.info(
        "Operator upgrade complete: %s -> %s",
        current_tag, confirmed,
    )
