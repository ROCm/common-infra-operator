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
install_operator.py

Installs AMD Network Operator (via Helm) and creates the NetworkConfig operand
on the master node of a Kubernetes cluster.

Master node IP and credentials are read from env.json (or /warmd.json).

Usage:
    python3 install_operator.py [--env /path/to/env.json]
    python3 install_operator.py --env /warmd.json
    python3 install_operator.py --manifest /path/to/image_manifest_1_1_0.yaml
"""

import argparse
import json
import os
import subprocess
import sys
import time

from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

try:
    import paramiko
except ImportError:
    paramiko = None


# --------------- defaults ---------------
SCRIPT_DIR = Path(__file__).parent
DEFAULT_ENV_JSON = SCRIPT_DIR / "env.json"
DEFAULT_MANIFEST = SCRIPT_DIR / "image_manifest_1_1_0.yaml"
DEFAULT_PF_NC_YAML = SCRIPT_DIR / "pf_networkconfig.yaml"
DEFAULT_VF_NC_YAML = SCRIPT_DIR / "vf_networkconfig.yaml"

HELM_RELEASE = "amd-network-operator"
HELM_NAMESPACE = "kube-amd-network"
HELM_REPO_NAME = "rocm-network"
HELM_REPO_URL = "https://rocm.github.io/network-operator"
HELM_CHART = f"{HELM_REPO_NAME}/network-operator-charts"

OPERATOR_READY_TIMEOUT = 300  # seconds
OPERATOR_POLL_INTERVAL = 10   # seconds
NC_READY_TIMEOUT = 300        # seconds
NC_POLL_INTERVAL = 10         # seconds


# --------------- helpers ---------------

def die(msg, code=1):
    print(f"FATAL: {msg}")
    sys.exit(code)


def info(msg):
    print(f"[INFO]  {msg}")


def warn(msg):
    print(f"[WARN]  {msg}")


def load_yaml_file(path):
    """Load a YAML file. Uses PyYAML if available, otherwise basic parsing."""
    with open(path, "r") as f:
        if yaml is not None:
            return yaml.safe_load(f)
        # Minimal fallback: only handles simple key: value at top level
        content = f.read()
        data = {}
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                key, _, val = line.partition(":")
                data[key.strip()] = val.strip().strip('"').strip("'")
        return data


def load_env_json(env_path):
    """Load env.json and return the parsed dict."""
    env_path = Path(env_path)
    if not env_path.is_file():
        # Fallback: check /warmd.json
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
    # Flat fallback
    for inst in cfg.get("instances", []):
        if str(inst.get("type", "")).lower() == "master":
            return inst
    die('No instance with "type": "master" found in env.json')


def detect_node_types(cfg):
    """
    Inspect worker nodes in env.json to determine PF vs VF.
    Workers with a 'vm' key are VF nodes (running in a VM).
    Workers without 'vm' are PF nodes (bare-metal).

    Returns (has_pf, has_vf).
    """
    has_pf = False
    has_vf = False

    def check_instances(instances):
        nonlocal has_pf, has_vf
        for inst in instances:
            if str(inst.get("type", "")).lower() != "worker":
                continue
            if inst.get("vm"):
                has_vf = True
            else:
                has_pf = True

    for group in cfg.get("Instances", []):
        raw = group.get("RawJSON", {})
        check_instances(raw.get("instances", []))
    # Flat fallback
    check_instances(cfg.get("instances", []))

    return has_pf, has_vf


def get_manifest_version(manifest_path):
    """Read the Helm chart version from the image manifest YAML."""
    data = load_yaml_file(manifest_path)
    version = data.get("version", "")
    if not version:
        die(f"'version' not found in manifest: {manifest_path}")
    return version


def _ssh_run_once(ip, username, password, cmd, timeout=120):
    """Execute a single command on a remote host via SSH and return (rc, stdout, stderr)."""
    if paramiko is None:
        # Fallback to sshpass + ssh
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
            hostname=ip,
            username=username,
            password=password,
            timeout=30,
            look_for_keys=False,
            allow_agent=False,
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


def scp_file(ip, username, password, local_path, remote_path):
    """Copy a local file to the remote host."""
    if paramiko is not None:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=ip, username=username, password=password,
                timeout=30, look_for_keys=False, allow_agent=False,
            )
            sftp = client.open_sftp()
            sftp.put(str(local_path), remote_path)
            sftp.close()
            return True
        except Exception as e:
            warn(f"Paramiko SCP failed: {e}")
            return False
        finally:
            client.close()

    # Fallback: sshpass + scp
    cmd = [
        "sshpass", "-p", password,
        "scp", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        str(local_path),
        f"{username}@{ip}:{remote_path}",
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return True
    except Exception as e:
        warn(f"scp failed: {e}")
        return False


# --------------- operator install ---------------

def install_operator(ip, username, password, version):
    """Add Helm repo and install the AMD Network Operator on the master node."""
    info(f"Installing AMD Network Operator {version}...")
    cmd = (
        f"helm repo add {HELM_REPO_NAME} {HELM_REPO_URL} --force-update && "
        f"helm repo update && "
        f"helm upgrade --install {HELM_RELEASE} {HELM_CHART} "
        f"-n {HELM_NAMESPACE} --create-namespace "
        f"--version={version} "
        f"--set kmm.enabled=false "
        f"--set node-feature-discovery.enabled=false"
    )
    info(f"Running: {cmd}")
    rc, out, err = ssh_run(ip, username, password, cmd, timeout=300)
    print(out)
    if rc != 0:
        die(f"Helm install failed (rc={rc}): {err}")
    info("Helm install command completed successfully")


def wait_for_operator(ip, username, password, timeout=OPERATOR_READY_TIMEOUT):
    """Wait for the operator controller pod to be Running."""
    info("Waiting for operator pod to be ready...")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rc, out, _ = ssh_run(
            ip, username, password,
            f"kubectl get pods -n {HELM_NAMESPACE} --no-headers"
        )
        if rc == 0 and out.strip():
            for line in out.strip().splitlines():
                cols = line.split()
                if len(cols) >= 3 and "controller" in cols[0] and cols[2] == "Running":
                    info(f"Operator pod is Running: {cols[0]}")
                    return True
        info(f"Operator controller pod not Running yet, retrying...")
        time.sleep(OPERATOR_POLL_INTERVAL)

    die(f"Operator pod did not reach Running state within {timeout}s")


# --------------- operand (NetworkConfig) ---------------

def apply_networkconfig(ip, username, password, nc_yaml_path, label=""):
    """Copy the NetworkConfig YAML to the master node and apply it."""
    remote_name = Path(nc_yaml_path).name
    remote_path = f"/tmp/{remote_name}"
    desc = f" ({label})" if label else ""
    info(f"Copying{desc} NetworkConfig YAML to master node...")
    if not scp_file(ip, username, password, nc_yaml_path, remote_path):
        die(f"Failed to copy {remote_name} to master node")

    info(f"Applying{desc} NetworkConfig...")
    rc, out, err = ssh_run(
        ip, username, password,
        f"kubectl apply -f {remote_path}"
    )
    print(out)
    if rc != 0:
        die(f"kubectl apply failed (rc={rc}): {err}")
    info(f"NetworkConfig{desc} applied successfully")


def wait_for_networkconfig(ip, username, password, timeout=NC_READY_TIMEOUT):
    """Wait for NetworkConfig operand pods (device-plugin, node-labeller) to be ready."""
    info("Waiting for NetworkConfig operand pods to be ready...")
    operand_keywords = ["device-plugin", "node-labeller", "metrics-exporter"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rc, out, _ = ssh_run(
            ip, username, password,
            f"kubectl get pods -n {HELM_NAMESPACE} --no-headers"
        )
        if rc == 0 and out.strip():
            operand_pods = {}
            for line in out.strip().splitlines():
                cols = line.split()
                if len(cols) >= 3:
                    name, status = cols[0], cols[2]
                    if any(kw in name for kw in operand_keywords):
                        operand_pods[name] = status

            if operand_pods:
                all_running = all(v == "Running" for v in operand_pods.values())
                info(f"Operand pods: {operand_pods}")
                if all_running:
                    info("All operand pods are Running")
                    return True

        time.sleep(NC_POLL_INTERVAL)

    warn(f"Not all operand pods reached Running state within {timeout}s")
    rc, out, _ = ssh_run(
        ip, username, password,
        f"kubectl get pods -n {HELM_NAMESPACE} -o wide"
    )
    print(out)
    return False


# --------------- main ---------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Install AMD Network Operator and NetworkConfig on the master node"
    )
    parser.add_argument(
        "--env", default=str(DEFAULT_ENV_JSON),
        help="Path to env.json or warmd.json (default: %(default)s)",
    )
    parser.add_argument(
        "--manifest", default=str(DEFAULT_MANIFEST),
        help="Path to image manifest YAML (default: %(default)s)",
    )
    parser.add_argument(
        "--pf-networkconfig", default=str(DEFAULT_PF_NC_YAML),
        help="Path to PF networkconfig YAML (default: %(default)s)",
    )
    parser.add_argument(
        "--vf-networkconfig", default=str(DEFAULT_VF_NC_YAML),
        help="Path to VF networkconfig YAML (default: %(default)s)",
    )
    parser.add_argument(
        "--skip-operand", action="store_true",
        help="Install operator only, skip NetworkConfig operand",
    )
    parser.add_argument(
        "--skip-wait", action="store_true",
        help="Do not wait for pods to become ready",
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

    # ---- Detect PF / VF worker nodes ----
    has_pf, has_vf = detect_node_types(cfg)
    info(f"Node detection — PF nodes: {has_pf}, VF nodes: {has_vf}")
    if not has_pf and not has_vf:
        warn("No worker nodes found in env.json")

    # ---- Read version from manifest ----
    manifest_path = Path(args.manifest)
    if not manifest_path.is_file():
        die(f"Manifest file not found: {manifest_path}")
    version = get_manifest_version(manifest_path)
    info(f"Chart version from manifest: {version}")

    # ---- Step 1: Install Operator ----
    info("=" * 60)
    info("STEP 1: Installing AMD Network Operator")
    info("=" * 60)
    install_operator(ip, username, password, version)

    if not args.skip_wait:
        wait_for_operator(ip, username, password)

    # ---- Step 2: Apply NetworkConfig operand(s) ----
    if not args.skip_operand:
        info("=" * 60)
        info("STEP 2: Applying NetworkConfig operand(s)")
        info("=" * 60)

        if has_pf:
            pf_path = Path(args.pf_networkconfig)
            if not pf_path.is_file():
                die(f"PF NetworkConfig YAML not found: {pf_path}")
            apply_networkconfig(ip, username, password, pf_path, label="PF")

        if has_vf:
            vf_path = Path(args.vf_networkconfig)
            if not vf_path.is_file():
                die(f"VF NetworkConfig YAML not found: {vf_path}")
            apply_networkconfig(ip, username, password, vf_path, label="VF")

        if not has_pf and not has_vf:
            warn("No PF or VF worker nodes detected — skipping operand apply")

        if not args.skip_wait and (has_pf or has_vf):
            wait_for_networkconfig(ip, username, password)

    info("=" * 60)
    info("Installation complete")
    info("=" * 60)


if __name__ == "__main__":
    main()
