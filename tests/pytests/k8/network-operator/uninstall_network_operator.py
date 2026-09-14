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
uninstall_network_operator.py

Uninstalls AMD Network Operator from a Kubernetes cluster.

Order of operations:
  1. Delete all NetworkConfig CRs (operands must be removed before operator)
  2. Wait for operand pods to terminate
  3. Helm uninstall the operator

Master node IP and credentials are read from env.json (or /warmd.json).

Usage:
    python3 uninstall_network_operator.py [--env /path/to/env.json]
    python3 uninstall_network_operator.py --env /warmd.json
"""

import argparse
import json
import subprocess
import sys
import time

from pathlib import Path

try:
    import paramiko
except ImportError:
    paramiko = None


# --------------- defaults ---------------
SCRIPT_DIR = Path(__file__).parent
DEFAULT_ENV_JSON = SCRIPT_DIR / "env.json"

HELM_RELEASE = "amd-network-operator"
HELM_NAMESPACE = "kube-amd-network"

NC_DELETE_TIMEOUT = 300   # seconds
NC_DELETE_POLL = 10       # seconds
OPERATOR_DELETE_TIMEOUT = 120
OPERATOR_DELETE_POLL = 5


# --------------- helpers ---------------

def die(msg, code=1):
    print(f"FATAL: {msg}")
    sys.exit(code)


def info(msg):
    print(f"[INFO]  {msg}")


def warn(msg):
    print(f"[WARN]  {msg}")


def load_env_json(env_path):
    """Load env.json and return the parsed dict."""
    env_path = Path(env_path)
    if not env_path.is_file():
        warmd = Path("/warmd.json")
        if warmd.is_file():
            env_path = warmd
            info(f"Using fallback testbed file: {warmd}")
        else:
            die(f"env file not found: {env_path} (also checked /warmd.json)")
    with open(env_path, "r") as f:
        return json.load(f)


def find_master(cfg):
    """Return the first instance whose type == 'master'."""
    for group in cfg.get("Instances", []):
        raw = group.get("RawJSON", {})
        for inst in raw.get("instances", []):
            if str(inst.get("type", "")).lower() == "master":
                return inst
    for inst in cfg.get("instances", []):
        if str(inst.get("type", "")).lower() == "master":
            return inst
    die('No instance with "type": "master" found in env.json')


def _ssh_run_once(ip, username, password, cmd, timeout=120):
    """Execute a single command on a remote host via SSH."""
    if paramiko is None:
        ssh_cmd = [
            "sshpass", "-p", password,
            "ssh", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{username}@{ip}",
            cmd,
        ]
        try:
            proc = subprocess.run(
                ssh_cmd, capture_output=True, text=True, timeout=timeout
            )
            return proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            return -1, "", "Command timed out"
        except FileNotFoundError:
            die("Neither paramiko nor sshpass is available for SSH")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=ip, username=username, password=password,
            timeout=30, look_for_keys=False, allow_agent=False,
        )
        _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        rc = stdout.channel.recv_exit_status()
        return rc, stdout.read().decode("utf-8", errors="replace"), stderr.read().decode("utf-8", errors="replace")
    finally:
        client.close()


def ssh_run(ip, username, password, cmd, timeout=120):
    """Execute a command via SSH using sudo with admin kubeconfig."""
    sudo_cmd = f"sudo sh -c 'export KUBECONFIG=/etc/kubernetes/admin.conf && {cmd}'"
    return _ssh_run_once(ip, username, password, sudo_cmd, timeout)


# --------------- Step 1: Delete all NetworkConfigs ---------------

def delete_all_networkconfigs(ip, username, password):
    """Delete all NetworkConfig CRs in the operator namespace."""
    info("Listing NetworkConfig resources...")
    rc, out, err = ssh_run(
        ip, username, password,
        f"kubectl get networkconfigs -n {HELM_NAMESPACE} --no-headers"
    )

    if rc != 0 or not out.strip():
        info("No NetworkConfig resources found (or CRD not installed)")
        return []

    nc_names = []
    for line in out.strip().splitlines():
        cols = line.split()
        if cols:
            nc_names.append(cols[0])

    info(f"Found {len(nc_names)} NetworkConfig(s): {nc_names}")

    for name in nc_names:
        info(f"Deleting NetworkConfig: {name}")
        rc, out, err = ssh_run(
            ip, username, password,
            f"kubectl delete networkconfig {name} -n {HELM_NAMESPACE}"
        )
        if rc == 0:
            info(f"Deleted: {name}")
        else:
            warn(f"Failed to delete {name}: {err}")
        if out.strip():
            print(out.strip())

    return nc_names


def wait_for_operand_pods_gone(ip, username, password, timeout=NC_DELETE_TIMEOUT):
    """Wait for operand pods (device-plugin, node-labeller, metrics-exporter) to terminate."""
    operand_keywords = ["device-plugin", "node-labeller", "metrics-exporter"]
    info("Waiting for operand pods to terminate...")
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        rc, out, _ = ssh_run(
            ip, username, password,
            f"kubectl get pods -n {HELM_NAMESPACE} --no-headers"
        )
        if rc != 0:
            break

        remaining = []
        for line in out.strip().splitlines():
            cols = line.split()
            if cols and any(kw in cols[0] for kw in operand_keywords):
                remaining.append(cols[0])

        if not remaining:
            info("All operand pods terminated")
            return True

        info(f"Operand pods still running: {remaining}, waiting...")
        time.sleep(NC_DELETE_POLL)

    warn(f"Some operand pods did not terminate within {timeout}s")
    return False


# --------------- Step 2: Helm uninstall ---------------

def uninstall_operator(ip, username, password):
    """Helm uninstall the operator."""
    info(f"Uninstalling Helm release: {HELM_RELEASE}")
    rc, out, err = ssh_run(
        ip, username, password,
        f"helm uninstall {HELM_RELEASE} -n {HELM_NAMESPACE}",
        timeout=300
    )
    print(out)
    if rc != 0:
        if "not found" in (out + err).lower():
            info(f"Helm release '{HELM_RELEASE}' not found — already uninstalled")
            return
        die(f"Helm uninstall failed (rc={rc}): {err}")
    info("Helm uninstall completed successfully")


def wait_for_operator_pods_gone(ip, username, password, timeout=OPERATOR_DELETE_TIMEOUT):
    """Wait for all pods in the namespace to terminate."""
    info("Waiting for operator pods to terminate...")
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        rc, out, _ = ssh_run(
            ip, username, password,
            f"kubectl get pods -n {HELM_NAMESPACE} --no-headers"
        )
        if rc != 0 or not out.strip():
            info("All pods terminated")
            return True

        pods = [line.split()[0] for line in out.strip().splitlines() if line.split()]
        info(f"Pods still running: {pods}, waiting...")
        time.sleep(OPERATOR_DELETE_POLL)

    warn(f"Some pods did not terminate within {timeout}s")
    return False


# --------------- main ---------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Uninstall AMD Network Operator from the cluster"
    )
    parser.add_argument(
        "--env", default=str(DEFAULT_ENV_JSON),
        help="Path to env.json or warmd.json (default: %(default)s)",
    )
    parser.add_argument(
        "--skip-wait", action="store_true",
        help="Do not wait for pods to terminate",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # ---- Load env.json and find master ----
    cfg = load_env_json(args.env)
    master = find_master(cfg)
    ip = master.get("ip")
    username = master.get("username")
    password = master.get("password")

    if not ip or not username:
        die("Master instance must have 'ip' and 'username' fields")

    info(f"Master node: {username}@{ip}")

    # ---- Step 1: Delete all NetworkConfig CRs ----
    info("=" * 60)
    info("STEP 1: Deleting all NetworkConfig operands")
    info("=" * 60)
    deleted = delete_all_networkconfigs(ip, username, password)

    if deleted and not args.skip_wait:
        wait_for_operand_pods_gone(ip, username, password)

    # ---- Step 2: Helm uninstall operator ----
    info("=" * 60)
    info("STEP 2: Helm uninstall operator")
    info("=" * 60)
    uninstall_operator(ip, username, password)

    if not args.skip_wait:
        wait_for_operator_pods_gone(ip, username, password)

    info("=" * 60)
    info("Uninstall complete")
    info("=" * 60)


if __name__ == "__main__":
    main()
