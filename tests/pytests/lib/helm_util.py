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
import os
import glob
import json
import logging
import pytest
import subprocess
import pprint
import time
from functools import wraps
import lib.common as common

Logger = logging.getLogger("lib.helmutil")
LogPrettyPrinter = pprint.PrettyPrinter(indent = 2)

def log_arguments(func):

    @wraps(func)
    def wrapper(*args, **kwargs):
        Logger.debug(f"Function::'{func.__name__}' with args: {args} kwargs: {kwargs}")
        return func(*args, **kwargs)
    return wrapper

@log_arguments
def helm_list(k8_cluster : common.k8_cluster, namespace : str) -> (int, str, str):
    """
    API to list installed helm-charts in a given namespace
    
    Parameters:
    k8_cluster : instance of lib.common.k8_cluster
    namespace : The name of namespace

    Returns:
    int: return-code, 0 for success else failure
    stdout : stdout from command execution
    stderr : stderr from command execution
    """
    cmd = ["helm", "list", "-a", "--namespace", namespace, "-o", "json"]
    if k8_cluster.k8_kube_config:
        cmd.extend(["--kubeconfig", k8_cluster.k8_kube_config])

    cmd_resp = subprocess.run(cmd, check=False,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,
                              encoding='utf-8')
    return cmd_resp.returncode, cmd_resp.stdout, cmd_resp.stderr

@log_arguments
def helm_add_repo(k8_cluster : common.k8_cluster, repo_name : str, repo_url : str) -> None:
    """
    API to add helm repo

    For example, following commands will be run:
    helm repo add rocm <repo>
    helm repo update

    Parameters:
    k8_cluster : intance of lib.common.k8_cluster
    repo_name  : Name of the repo
    repo_url   : repo url
    """
    cmd = ["helm", "repo", "add", repo_name, repo_url]
    if k8_cluster.k8_kube_config:
        cmd.extend(["--kubeconfig", k8_cluster.k8_kube_config])

    cmd_resp = subprocess.run(cmd, check=False,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                encoding='utf-8')
    ret_code = cmd_resp.returncode
    ret_stdout = cmd_resp.stdout
    ret_stderr = cmd_resp.stderr
    assert ret_code == 0, f"Failed to add helm repo {repo_name}, stdout : {ret_stdout} stderr: {ret_stderr}"

    cmd = ["helm", "repo", "update"]
    if k8_cluster.k8_kube_config:
        cmd.extend(["--kubeconfig", k8_cluster.k8_kube_config])

    cmd_resp = subprocess.run(cmd, check=False,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                encoding='utf-8')
    ret_code = cmd_resp.returncode
    assert ret_code == 0, f"Failed to update helm repo {repo_name}, stdout : {cmd_resp.stdout} stderr: {cmd_resp.stderr}"
    return

@log_arguments
def helm_registry_login(k8_cluster : common.k8_cluster, registry : str, username : str, password : str) -> None:
    """
    API to login to an OCI registry for helm chart pulls.

    Required for private OCI-hosted helm charts (e.g., docker.io/amdpsdo/...).

    Parameters:
    k8_cluster : instance of lib.common.k8_cluster
    registry   : Registry hostname (e.g., docker.io)
    username   : Registry username
    password   : Registry password/token
    """
    cmd = ["helm", "registry", "login", registry,
           "--username", username, "--password-stdin"]
    if k8_cluster.k8_kube_config:
        cmd.extend(["--kubeconfig", k8_cluster.k8_kube_config])

    cmd_resp = subprocess.run(cmd, input=password, check=False,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,
                              encoding='utf-8')
    ret_code = cmd_resp.returncode
    ret_stdout = cmd_resp.stdout
    ret_stderr = cmd_resp.stderr
    assert ret_code == 0, f"Failed to login to registry {registry}, stdout: {ret_stdout} stderr: {ret_stderr}"
    return

@log_arguments
def helm_install(k8_cluster : common.k8_cluster, release_name : str, namespace : str, helm_chart_path : str, version : str, values_yaml : str, **kwargs) -> (int, str, str):
    """
    API to install helm-chart

    For example, following command will be run:
    helm install <release-name> <path-to-helm-chart>
        -n kube-amd-gpu --create-namespace --version=<version>
        --set controllerManager.manager.image.repository=docker.io/rocm/gpu-operator
        --set controllerManager.manager.image.tag=latest

    Parameters:
    k8_cluster : instance of lib.common.k8_cluster
    release_name : release-name to use for helm-chart installation
    namespace : namespace in which to install helm-chart
    helm_chart_path : path to helm chart (file or repo path)
    version : version of the helm-chart to install
    values_yaml : values.yaml file

    Returns:
    ret_code   : Return code for command execution. 0 for success else failure
    ret_stdout : Stdout from command execution
    ret_stderr : Stderr from command execution
    """

    cmd = ["helm", "install", "--debug", f"{release_name}", f"{helm_chart_path}"]
    cmd.extend(["-n", f"{namespace}", "--create-namespace"])
    if version:
        cmd.extend([f"--version={version}"])

    for key, value in kwargs.items():
        cmd.extend(["--set", f"{key}={value}"])

    if release_name == 'gpu-operator':
        if os.getenv("GPU_DEVICE") == "VF":
            node_selection = {
                "feature.node.kubernetes.io/amd-gpu"    : None,
                "feature.node.kubernetes.io/amd-vgpu"   : "true",
            }
        else:
            node_selection = {
                "feature.node.kubernetes.io/amd-gpu"    : "true",
                "feature.node.kubernetes.io/amd-vgpu"   : None,
            }
        cmd.extend(["--set-json", f"deviceConfig.spec.selector={json.dumps(node_selection)}"])

    if k8_cluster.k8_kube_config:
        cmd.extend(["--kubeconfig", k8_cluster.k8_kube_config])

    if values_yaml:
        if not os.path.exists(values_yaml):
            return -1, "", f"Missing values.yaml : {values_yaml}"
        cmd.extend(["-f", values_yaml])
    Logger.debug(f"helm-install command: {' '.join(cmd)}")
    cmd_resp = subprocess.run(cmd, check=False,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,
                              encoding='utf-8')
    return cmd_resp.returncode, cmd_resp.stdout, cmd_resp.stderr

@log_arguments
def helm_upgrade(k8_cluster : common.k8_cluster, release_name : str, namespace : str, helm_chart_path : str, version : str, values_yaml : str, **kwargs) -> (int, str, str):
    """
    API to upgrade helm-chart

    For example, following command will be run:
    helm upgrade <release-name> <path-to-helm-chart>
        -n kube-amd-gpu --version=<version>
        --set controllerManager.manager.image.repository=docker.io/rocm/gpu-operator
        --set controllerManager.manager.image.tag=latest

    Parameters:
    k8_cluster : instance of lib.common.k8_cluster
    release_name : release-name of the helm-chart to upgrade
    namespace : namespace in which helm-chart is installed
    helm_chart_path : path to helm chart (file or repo path)
    version : version of the helm-chart to upgrade to
    values_yaml : values.yaml file

    Returns:
    ret_code   : Return code for command execution. 0 for success else failure
    ret_stdout : Stdout from command execution
    ret_stderr : Stderr from command execution
    """

    cmd = ["helm", "upgrade", "--debug", f"{release_name}", f"{helm_chart_path}"]
    cmd.extend(["-n", f"{namespace}"])
    if version:
        cmd.extend([f"--version={version}"])

    for key, value in kwargs.items():
        cmd.extend(["--set", f"{key}={value}"])

    if release_name == 'gpu-operator':
        if os.getenv("GPU_DEVICE") == "VF":
            node_selection = {
                "feature.node.kubernetes.io/amd-gpu"    : None,
                "feature.node.kubernetes.io/amd-vgpu"   : "true",
            }
        else:
            node_selection = {
                "feature.node.kubernetes.io/amd-gpu"    : "true",
                "feature.node.kubernetes.io/amd-vgpu"   : None,
            }
        cmd.extend(["--set-json", f"deviceConfig.spec.selector={json.dumps(node_selection)}"])

    if k8_cluster.k8_kube_config:
        cmd.extend(["--kubeconfig", k8_cluster.k8_kube_config])

    if values_yaml:
        if not os.path.exists(values_yaml):
            return -1, "", f"Missing values.yaml : {values_yaml}"
        cmd.extend(["-f", values_yaml])
    Logger.debug(f"helm-upgrade command: {' '.join(cmd)}")
    cmd_resp = subprocess.run(cmd, check=False,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,
                              encoding='utf-8')
    return cmd_resp.returncode, cmd_resp.stdout, cmd_resp.stderr

@log_arguments
def helm_uninstall(k8_cluster : common.k8_cluster, release_name : str, namespace : str) -> (int, str, str):
    """
    API to uninstall helm-chart

    Parameters:
    k8_cluster : instance of lib.common.k8_cluster
    release_name : Release-name of the helm-chart
    namespace : name-space in which helm-chart was installed

    Returns:
    int: return-code, 0 for success else failure
    stdout: stdout of command execution
    stderr: stderr of command execution
    """

    cmd = ["helm", "uninstall", "--debug", f"{release_name}", "--namespace", f"{namespace}"]
    if k8_cluster.k8_kube_config:
        cmd.extend(["--kubeconfig", k8_cluster.k8_kube_config])

    cmd_resp = subprocess.run(cmd, check=False,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                encoding='utf-8')
    return cmd_resp.returncode, cmd_resp.stdout, cmd_resp.stderr

@log_arguments
def helm_cleanup(k8_cluster : common.k8_cluster, release_name : str, namespace : str) -> (int, str, str):
    """
    API to do forceful cleanup, if helm-uninstall resulted in failure

    Following command will be run: "helm uninstall <release-name> --namespace <namespace> --no-hooks"

    Parameters:
    k8_cluster : instance of lib.common.k8_cluster
    release_name : helm-chart release name
    namespace : namespace to use

    Returns:
    int: return-code, 0 for success else failure
    stdout: stdout of command execution
    stderr: stderr of command execution
    """
    cmd = ["helm", "uninstall", f"{release_name}", "--namespace", f"{namespace}", "--no-hooks"]
    if k8_cluster.k8_kube_config:
        cmd.extend(["--kubeconfig", k8_cluster.k8_kube_config])

    cmd_resp = subprocess.run(cmd, check=False,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                encoding='utf-8')
    return cmd_resp.returncode, cmd_resp.stdout, cmd_resp.stderr

@log_arguments
def is_helm_chart_deployed(k8_cluster : common.k8_cluster, release_name : str, namespace : str) -> bool:
    """
    API to check if helm-chart is deployed. 

    Parameters:
    k8_cluster : instance of lib.common.k8_cluster
    release-name: release-name used for helm-chart
    namespace : namespace in which helm-chart is installed

    Returns:
    bool : True if chart is deployed else False
    """
    ret_code, ret_stdout, ret_stderr = helm_list(k8_cluster, namespace)

    """
    Sample output
    vm@master-node:~/sandbox$ helm list -n kube-amd-gpu -o json | jq .
    [
      {
        "name": "gpu-operator",
        "namespace": "kube-amd-gpu",
        "revision": "1",
        "updated": "2024-12-11 10:04:56.122288711 +0000 UTC",
        "status": "failed", or "deployed",
        "chart": "gpu-operator-v1.0.0",
        "app_version": "v1.0.0"
      }
    ]
    """
    for chart in json.loads(ret_stdout):
        if chart['name'] == release_name and chart['status'] != 'uninstalling':
            return True
    return False

@log_arguments
def is_helm_release_present(k8_cluster: common.k8_cluster, release_name: str, namespace: str) -> bool:
    """
    Return True if the release exists in any state (deployed, failed, uninstalling, …).

    Unlike is_helm_chart_deployed(), this catches releases stuck in 'uninstalling'
    so callers can run recovery before attempting helm install.
    """
    ret_code, ret_stdout, _ = helm_list(k8_cluster, namespace)
    if ret_code != 0:
        return False
    for chart in json.loads(ret_stdout):
        if chart['name'] == release_name:
            return True
    return False


@log_arguments
def helm_uninstall_with_recovery(k8_cluster: common.k8_cluster, release_name: str,
                                  namespace: str, node_rejoin_timeout: int = 600) -> tuple:
    """
    Uninstall a helm release, with automatic recovery if the normal uninstall hangs.

    Normal path (GPU node healthy):
      helm uninstall → succeeds → return.

    Recovery path (GPU node unreachable, pre-delete hook stalled on KMM finalizer):
      1. Delete unreachable K8s nodes — unblocks KMM / DeviceConfig finalizer chain.
      2. Poll until all DeviceConfig CRs are fully gone (finalizers resolved).
      3. helm uninstall --no-hooks — skips the pre-delete hook, cleans up resources.
      4. Wait for deleted nodes to re-register and become Ready with the amd-gpu label
         so the next helm install sees a healthy cluster.

    Returns (ret_code, stdout, stderr) from the final helm command.
    """
    import lib.k8_util as k8_util

    ret_code, ret_stdout, ret_stderr = helm_uninstall(k8_cluster, release_name, namespace)
    if ret_code == 0:
        return ret_code, ret_stdout, ret_stderr

    Logger.warning(f"helm_uninstall failed for {release_name} ({ret_stderr.strip()}), starting recovery")

    # Step 1: remove unreachable nodes so KMM gives up on them
    deleted_nodes = k8_util.k8_delete_unreachable_nodes()
    if deleted_nodes:
        Logger.info(f"Deleted unreachable nodes: {deleted_nodes}")

    # Step 2: wait for DeviceConfig CRs to clear (finalizers resolved after node removal)
    Logger.info("Waiting for DeviceConfig CRs to fully delete")
    deadline = time.time() + 120
    while time.time() < deadline:
        dc_info = k8_util.k8_get_deviceconfigs_info(namespace)
        if not dc_info:
            break
        Logger.info(f"DeviceConfigs still present: {list(dc_info.keys())}, waiting…")
        time.sleep(10)

    # Step 3: helm uninstall --no-hooks — skips the stalled pre-delete hook
    Logger.info(f"Running helm cleanup --no-hooks for {release_name}")
    ret_code, ret_stdout, ret_stderr = helm_cleanup(k8_cluster, release_name, namespace)
    if ret_code != 0:
        Logger.error(f"helm cleanup --no-hooks failed: {ret_stderr.strip()}")

    # Step 4: wait for deleted nodes to rejoin before returning
    if deleted_nodes:
        Logger.info(f"Waiting for deleted nodes to rejoin: {deleted_nodes}")
        k8_util.k8_wait_for_nodes_ready(deleted_nodes, timeout_seconds=node_rejoin_timeout)

    return ret_code, ret_stdout, ret_stderr


@log_arguments
def is_helm_chart_healthy(k8_cluster : common.k8_cluster, release_name : str, namespace : str) -> bool:
    """
    API to check if installed helm-chart is healthy

    Parameters:
    k8_cluster : instance of lib.common.k8_cluster
    release-name: release-name used for helm-chart
    namespace : namespace in which helm-chart is installed

    Returns:
    bool : True if chart is deployed and healthy else False
    """
    """
    Sample output
    vm@master-node:~/sandbox$ helm list -n kube-amd-gpu -o json | jq .
    [
      {
        "name": "gpu-operator",
        "namespace": "kube-amd-gpu",
        "revision": "1",
        "updated": "2024-12-11 10:04:56.122288711 +0000 UTC",
        "status": "failed", or "deployed",
        "chart": "gpu-operator-v1.0.0",
        "app_version": "v1.0.0"
      }
    ]
    """
    ret_code, ret_stdout, ret_stderr = helm_list(k8_cluster, namespace)
    for chart in json.loads(ret_stdout):
        if chart['name'] == release_name and chart['status'] == 'deployed':
            return True
    return False


# ---------------------------------------------------------------------------
# NIC test-suite helpers (added for network-operator migration)
# ---------------------------------------------------------------------------

@log_arguments
def helm_get_values(k8_cluster: common.k8_cluster, release_name: str, namespace: str) -> (int, str, str):
    """Get user-supplied values for a helm release as YAML."""
    cmd = ["helm", "get", "values", release_name, "--namespace", namespace, "-o", "yaml"]
    if k8_cluster.k8_kube_config:
        cmd.extend(["--kubeconfig", k8_cluster.k8_kube_config])
    cmd_resp = subprocess.run(cmd, check=False, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, encoding='utf-8')
    return cmd_resp.returncode, cmd_resp.stdout, cmd_resp.stderr

@log_arguments
def helm_template(k8_cluster: common.k8_cluster, release_name: str, chart_path: str,
                  namespace: str, set_values: dict = None) -> (int, str, str):
    """Render chart templates locally without installing."""
    cmd = ["helm", "template", release_name, chart_path, "--namespace", namespace]
    if set_values:
        for k, v in set_values.items():
            cmd.extend(["--set", f"{k}={v}"])
    if k8_cluster.k8_kube_config:
        cmd.extend(["--kubeconfig", k8_cluster.k8_kube_config])
    cmd_resp = subprocess.run(cmd, check=False, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, encoding='utf-8')
    return cmd_resp.returncode, cmd_resp.stdout, cmd_resp.stderr

@log_arguments
def helm_show_chart(k8_cluster: common.k8_cluster, chart_path: str) -> (int, str, str):
    """Show chart metadata (Chart.yaml contents)."""
    cmd = ["helm", "show", "chart", chart_path]
    if k8_cluster.k8_kube_config:
        cmd.extend(["--kubeconfig", k8_cluster.k8_kube_config])
    cmd_resp = subprocess.run(cmd, check=False, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, encoding='utf-8')
    return cmd_resp.returncode, cmd_resp.stdout, cmd_resp.stderr

@log_arguments
def helm_show_values(k8_cluster: common.k8_cluster, chart_path: str) -> (int, str, str):
    """Show default values from a chart."""
    cmd = ["helm", "show", "values", chart_path]
    if k8_cluster.k8_kube_config:
        cmd.extend(["--kubeconfig", k8_cluster.k8_kube_config])
    cmd_resp = subprocess.run(cmd, check=False, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, encoding='utf-8')
    return cmd_resp.returncode, cmd_resp.stdout, cmd_resp.stderr

@log_arguments
def helm_get_manifest(k8_cluster: common.k8_cluster, release_name: str, namespace: str) -> (int, str, str):
    """Get the rendered manifest of an installed release."""
    cmd = ["helm", "get", "manifest", release_name, "--namespace", namespace]
    if k8_cluster.k8_kube_config:
        cmd.extend(["--kubeconfig", k8_cluster.k8_kube_config])
    cmd_resp = subprocess.run(cmd, check=False, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, encoding='utf-8')
    return cmd_resp.returncode, cmd_resp.stdout, cmd_resp.stderr

# is_helm_release_deployed: equivalent to the existing is_helm_chart_healthy
# (both check status == "deployed"). Use is_helm_chart_healthy directly; this
# alias is provided for callers migrating from the old per-suite util.py.
is_helm_release_deployed = is_helm_chart_healthy

@log_arguments
def helm_ensure_release_cleaned_up(k8_cluster: common.k8_cluster, release_name: str, namespace: str) -> None:
    """Uninstall a release if deployed; retry with --no-hooks on failure."""
    if not is_helm_release_present(k8_cluster, release_name, namespace):
        return
    Logger.info(f"Cleaning up existing release {release_name} in {namespace}")
    ret_code, _, ret_stderr = helm_uninstall(k8_cluster, release_name, namespace)
    if ret_code != 0:
        Logger.warning(f"helm uninstall failed ({ret_stderr.strip()}), retrying with --no-hooks")
        helm_cleanup(k8_cluster, release_name, namespace)
    time.sleep(5)

@log_arguments
def find_latest_chart(builds_dir: str, chart_glob: str) -> str:
    """Find the latest chart tarball under builds_dir. Returns path or None."""
    if not os.path.isdir(builds_dir):
        Logger.warning(f"Builds directory does not exist: {builds_dir}")
        return None
    subdirs = sorted(
        [d for d in os.listdir(builds_dir) if os.path.isdir(os.path.join(builds_dir, d))],
        reverse=True,
    )
    if not subdirs:
        Logger.warning(f"No build folders found in {builds_dir}")
        return None
    latest_dir = os.path.join(builds_dir, subdirs[0])
    matches = glob.glob(os.path.join(latest_dir, chart_glob))
    if not matches:
        matches = glob.glob(os.path.join(latest_dir, "**", chart_glob), recursive=True)
    if not matches:
        Logger.warning(f"No chart matching '{chart_glob}' in {latest_dir}")
        return None
    Logger.info(f"Found chart: {matches[0]}")
    return matches[0]

