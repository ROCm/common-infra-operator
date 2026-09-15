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
test_update_throttle_operator.py

Upgrades the amd-network-operator Helm release to the latest v1.2.0-* build
published on the internal assets server.
"""

import re
import json
import time
import subprocess
import os
import shutil
import pytest
from urllib.request import urlopen
from urllib.error import URLError

import logging

LOG = logging.getLogger(__name__)
TEST_TIMEOUT = 180

# ---------- Configuration ----------

NC_NAMESPACE = "kube-amd-network"
FETCH_TIMEOUT = 30  # seconds for HTTP requests to the assets server
VERSION_PREFIX = "v1.2.0"
ENV_JSON_PATH = os.path.join(os.path.dirname(__file__), "env.json")

NETWORK_OPERATOR_ASSETS_URL = os.environ.get(
    "NETWORK_OPERATOR_ASSETS_URL",
    "https://example.com/builds/hourly-network-operator/"
)
NETWORK_OPERATOR_HELM_RELEASE = "amd-network-operator"
NETWORK_OPERATOR_HELM_TARBALL_PATTERN = (
    "amdpsdo-network-operator-helm-k8s-v1.2.0-{tag}.tgz"
)


# ---------- Asset-scraping helpers ----------

def _fetch_page(url: str) -> str:
    """Fetch *url* and return its body as a Unicode string."""
    try:
        with urlopen(url, timeout=FETCH_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except URLError as exc:
        raise RuntimeError(f"Failed to fetch {url}: {exc}") from exc


def _latest_versioned_tag(assets_url: str, prefix: str) -> str:
    """
    Scrape *assets_url* for entries matching ``<prefix>-<N>`` and return the
    tag with the highest integer suffix, e.g. ``v1.2.0-3``.
    """
    html = _fetch_page(assets_url)
    escaped = re.escape(prefix)
    numbers = [
        int(m.group(1))
        for m in re.finditer(rf"\b{escaped}-(\d+)(?=/|\"|\s|<|$)", html)
    ]
    if not numbers:
        raise RuntimeError(
            f"No '{prefix}-<N>' build entries found at {assets_url}. "
            "Verify the URL is reachable and lists builds in the expected format."
        )
    latest = max(numbers)
    LOG.info("Latest %s-* build at %s: %s-%d", prefix, assets_url, prefix, latest)
    return f"{prefix}-{latest}"


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
    Return the APP VERSION of the running amd-network-operator helm release,
    e.g. ``v1.2.0-1``, by parsing ``helm list -A`` output.
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


def _helm_upgrade_operator_on_master(master: dict, tag: str, tarball_url: str) -> None:
    """Download chart tarball and run helm upgrade on master node."""
    cmd = (
        "set -euo pipefail; "
        "tmpdir=$(mktemp -d); "
        "trap 'rm -rf \"$tmpdir\"' EXIT; "
        f"curl -fsSL -o \"$tmpdir/chart.tgz\" \"{tarball_url}\"; "
        "helm upgrade "
        f"{NETWORK_OPERATOR_HELM_RELEASE} \"$tmpdir/chart.tgz\" "
        f"-n {NC_NAMESPACE} "
        "--set controllerManager.manager.imagePullSecrets=amdpsdo-secret "
        "--set kmm.controller.manager.imagePullSecrets=amdpsdo-secret "
        "--set kmm.webhookServer.webhookServer.imagePullSecrets=amdpsdo-secret"
    )
    LOG.info("Running helm upgrade on master node to tag %s using tarball URL: %s", tag, tarball_url)
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
def test_update_throttle_operator():
    """
    Upgrade the amd-network-operator Helm release to the latest v1.2.0-* build
    published on the assets server. Skips if already current.
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

    latest_tag = _latest_versioned_tag(NETWORK_OPERATOR_ASSETS_URL, VERSION_PREFIX)
    LOG.info("Latest operator build: %s", latest_tag)

    if current_tag == latest_tag:
        LOG.info("Operator is already at latest build (%s); skipping upgrade.", latest_tag)
        pytest.skip(f"Operator is already at the latest build: {latest_tag}")

    tarball_name = NETWORK_OPERATOR_HELM_TARBALL_PATTERN.format(tag=latest_tag)
    tarball_url  = f"{NETWORK_OPERATOR_ASSETS_URL}{latest_tag}/{tarball_name}"
    LOG.info("Using helm chart tarball URL: %s", tarball_url)

    try:
        _helm_upgrade_operator_on_master(master, latest_tag, tarball_url)
    except RuntimeError as exc:
        pytest.fail(str(exc))

    try:
        confirmed = _helm_confirm_upgrade(master, latest_tag)
    except TimeoutError as exc:
        pytest.fail(str(exc))

    LOG.info(
        "Operator upgrade complete: %s -> %s",
        current_tag, confirmed,
    )
