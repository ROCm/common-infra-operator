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
import json
import logging
import pytest
import subprocess
import pprint
import base64
import time
import lib.common as common
import lib.k8_util as k8_util
from lib.k8_util import log_arguments

Logger = logging.getLogger("lib.olmutil")
LogPrettyPrinter = pprint.PrettyPrinter(indent = 2)

@log_arguments
def olm_install(k8_cluster : common.k8_cluster, repo_url : str, namespace: str, **kwargs) -> (int, str, str):
    """
    API to install OLM Bundle

    For example, following commands will be run:
    $PATH/operator-sdk run bundle <registry>/amd-gpu-operator-bundle:v1.4.1 --namespace <namespace> <options>

    Following options:
     --skip-tls --skip-tls-verify --use-http --security-context-config restricted

    Parameters:
    k8_cluster : intance of lib.common.k8_cluster
    repo_url   : repo url
    namespace  : Namespace to use
    """
    cmd = ["operator-sdk", "run", "bundle", repo_url, "--namespace", namespace]
    # Working version:
    # ./bin/operator-sdk run bundle docker.io/amdpsdo/gpu-operator-olm-bundle:v1.4.1-31 --namespace openshift-amd-gpu 
    # --pull-secret-name docker-amdpsdo-auth --security-context-config restricted --kubeconfig /root/.kube/config --verbose
    if kwargs.get('skip-tls', False):
        cmd.append("--skip-tls")
    if kwargs.get('skip-tls-verify', False):
        cmd.append("--skip-tls-verify")
    if kwargs.get('use-http', True):
        cmd.append("--use-http")
    if kwargs.get("pull-secret-name", None):
        cmd.extend(["--pull-secret-name", kwargs.get("pull-secret-name")])
        if k8_util.k8_create_auth_file(kwargs.get("pull-secret-name"), namespace):
            Logger.debug(f"Created auth-file for operator-sdk to work")
        else:
            Logger.warning(f"Failed to create auth-file for operator-sdk to pull olm-bundle from secure location - result unexpected")
    else:
        cmd.append("--skip-tls")
        cmd.append("--skip-tls-verify")
        cmd.append("--use-http")

    cmd.extend(["--security-context-config", kwargs.get("security-context-config", "restricted")])
    if k8_cluster.k8_kube_config:
        cmd.extend(["--kubeconfig", k8_cluster.k8_kube_config])

    _OLM_INSTALL_TIMEOUT = 600

    cmd.append("--verbose")
    Logger.debug(f"olm-install command: {cmd}")
    try:
        cmd_resp = subprocess.run(cmd, check=False,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  encoding='utf-8',
                                  timeout=_OLM_INSTALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        Logger.error(f"operator-sdk run bundle timed out after {_OLM_INSTALL_TIMEOUT}s")
        return 1, "", f"operator-sdk run bundle timed out after {_OLM_INSTALL_TIMEOUT}s"
    Logger.info(f"operator-sdk run bundle stdout:\n{cmd_resp.stdout}")
    if cmd_resp.stderr:
        Logger.info(f"operator-sdk run bundle stderr:\n{cmd_resp.stderr}")
    return cmd_resp.returncode, cmd_resp.stdout, cmd_resp.stderr

@log_arguments
def olm_subscription_install(namespace: str, catalog: str, channel: str,
                             package: str, csv: str, **kwargs) -> (int, str, str):
    """Install GPU operator via OLM Subscription from a certified catalog.

    Creates Namespace, OperatorGroup, and Subscription with Manual approval
    pinned to the specified CSV. Waits for the CSV to reach Succeeded state.

    Parameters:
    namespace  : Target namespace (e.g. openshift-amd-gpu)
    catalog    : CatalogSource name (e.g. certified-operators)
    channel    : Subscription channel (e.g. alpha)
    package    : Package name (e.g. amd-gpu-operator)
    csv        : ClusterServiceVersion name (e.g. amd-gpu-operator.v1.5.2)
    """
    Logger.info(f"Installing {package} via OLM Subscription (catalog={catalog}, csv={csv})")

    ret_code, _, ret_stderr = k8_util.k8_create_namespace(namespace)
    if ret_code != 0:
        Logger.error(f"Failed to create namespace {namespace}: {ret_stderr}")
        return ret_code, "", ret_stderr

    # Delete any pre-existing OperatorGroup so we always get AllNamespaces mode.
    # A stale OwnNamespace OperatorGroup (from prior onboarding or manual setup)
    # survives olm_force_cleanup and silently poisons the install via 409.
    k8_util.k8_delete_custom_resource(
        "operators.coreos.com", "v1", "operatorgroups",
        namespace, "amd-gpu-operator-group")

    og_spec = {
        "apiVersion": "operators.coreos.com/v1",
        "kind": "OperatorGroup",
        "metadata": {"name": "amd-gpu-operator-group", "namespace": namespace},
        "spec": {},
    }
    ret_code, _, ret_stderr = k8_util.k8_create_custom_resource(og_spec)
    if ret_code != 0:
        Logger.error(f"Failed to create OperatorGroup: {ret_stderr}")
        return ret_code, "", ret_stderr

    sub_spec = {
        "apiVersion": "operators.coreos.com/v1alpha1",
        "kind": "Subscription",
        "metadata": {"name": package, "namespace": namespace},
        "spec": {
            "channel": channel,
            "name": package,
            "source": catalog,
            "sourceNamespace": "openshift-marketplace",
            "startingCSV": csv,
            "installPlanApproval": "Manual",
        },
    }
    ret_code, _, ret_stderr = k8_util.k8_create_custom_resource(sub_spec)
    if ret_code != 0:
        Logger.error(f"Failed to create Subscription: {ret_stderr}")
        return ret_code, "", ret_stderr

    # Approve InstallPlan (Manual approval mode)
    for attempt in range(20):
        time.sleep(15)
        ret_code, approved_count, _ = olm_approve_install_plans(namespace)
        if ret_code == 0 and approved_count.strip() not in ("", "0"):
            Logger.info(f"InstallPlan approved for {csv} (count={approved_count.strip()})")
            break
        Logger.debug(f"Waiting for InstallPlan (attempt {attempt+1}/20)")

    # Wait for CSV to reach Succeeded
    for attempt in range(20):
        time.sleep(15)
        ret_code, csv_obj, err = k8_util.k8_get_namespaced_custom_resource(
            "operators.coreos.com", "v1alpha1", "clusterserviceversions", namespace, csv)
        if ret_code != 0:
            Logger.warning(f"CSV {csv} not found yet (attempt {attempt+1}/20): {err}")
            continue
        phase = csv_obj.get("status", {}).get("phase", "")
        Logger.debug(f"CSV {csv} phase={phase} (attempt {attempt+1}/20)")
        if phase == "Succeeded":
            Logger.info(f"CSV {csv} reached Succeeded state")
            return 0, f"CSV {csv} installed successfully", ""

    Logger.error(f"CSV {csv} did not reach Succeeded within timeout")
    return 1, "", f"CSV {csv} did not reach Succeeded state"


_OLM_CLEANUP_TIMEOUT = 300


def _diagnose_stuck_olm_resources(release_name, namespace):
    """Log OLM resources that may be blocking cleanup (finalizers, pending deletions)."""
    stuck = []

    for group, version, plural, label in [
        ("operators.coreos.com", "v1alpha1", "clusterserviceversions", "CSV"),
        ("operators.coreos.com", "v1alpha1", "subscriptions", "Subscription"),
        ("operators.coreos.com", "v1alpha1", "installplans", "InstallPlan"),
        ("operators.coreos.com", "v1alpha1", "catalogsources", "CatalogSource"),
        ("operators.coreos.com", "v1", "operators", "Operator"),
    ]:
        try:
            ret_code, items, _ = k8_util.k8_get_custom_resource_objects(
                group=group, version=version, plural=plural)
            if ret_code != 0:
                continue
            for item in items:
                name = item["metadata"]["name"]
                if release_name not in name and namespace not in name:
                    continue
                finalizers = item["metadata"].get("finalizers", [])
                deletion_ts = item["metadata"].get("deletionTimestamp")
                phase = item.get("status", {}).get("phase", "")
                if finalizers or deletion_ts:
                    entry = f"{label} {name}: finalizers={finalizers}"
                    if deletion_ts:
                        entry += f", deletionTimestamp={deletion_ts}"
                    if phase:
                        entry += f", phase={phase}"
                    stuck.append(entry)
                    Logger.warning(entry)
        except Exception as e:
            Logger.debug(f"Failed to inspect {plural}: {e}")

    if not stuck:
        Logger.info("No stuck OLM resources with finalizers or pending deletions found")
    return stuck


def _remove_finalizers_from_stuck_resources(release_name, namespace):
    """Strip finalizers from OLM resources blocking deletion."""
    custom_api = None
    try:
        from kubernetes import client as k8_client
        custom_api = k8_client.CustomObjectsApi()
    except Exception:
        Logger.warning("Cannot load kubernetes client for finalizer removal")
        return

    for group, version, plural in [
        ("operators.coreos.com", "v1alpha1", "clusterserviceversions"),
        ("operators.coreos.com", "v1alpha1", "subscriptions"),
        ("operators.coreos.com", "v1alpha1", "installplans"),
        ("operators.coreos.com", "v1", "operators"),
    ]:
        try:
            ret_code, items, _ = k8_util.k8_get_custom_resource_objects(
                group=group, version=version, plural=plural)
            if ret_code != 0:
                continue
            for item in items:
                name = item["metadata"]["name"]
                if release_name not in name and namespace not in name:
                    continue
                finalizers = item["metadata"].get("finalizers", [])
                if not finalizers:
                    continue
                item_ns = item["metadata"].get("namespace")
                Logger.warning(f"Removing finalizers {finalizers} from {plural}/{name}")
                try:
                    patch = {"metadata": {"finalizers": []}}
                    if item_ns:
                        custom_api.patch_namespaced_custom_object(
                            group, version, item_ns, plural, name, patch)
                    else:
                        custom_api.patch_cluster_custom_object(
                            group, version, plural, name, patch)
                except Exception as e:
                    Logger.error(f"Failed to remove finalizers from {plural}/{name}: {e}")
        except Exception as e:
            Logger.debug(f"Failed to iterate {plural} for finalizer removal: {e}")


@log_arguments
def olm_cleanup(k8_cluster : common.k8_cluster, release_name : str, namespace : str) -> (int, str, str):
    """
    API to remove OLM Bundle

    For example, following commands will be run:
    $PATH/operator-sdk cleanup <release-name> -n <namespace> --delete-all

    Parameters:
    k8_cluster : intance of lib.common.k8_cluster
    release_name : Name of OLM Bundle
    namespace   : Namespace to use
    """

    cmd = ["operator-sdk", "cleanup", release_name, "--namespace", namespace]
    if k8_cluster.k8_kube_config:
        cmd.extend(["--kubeconfig", k8_cluster.k8_kube_config])

    Logger.debug(f"olm-cleanup command: {cmd}")
    try:
        subprocess.run(cmd, check=False,
                       stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE,
                       encoding='utf-8',
                       timeout=_OLM_CLEANUP_TIMEOUT)
    except subprocess.TimeoutExpired:
        Logger.error(f"operator-sdk cleanup timed out after {_OLM_CLEANUP_TIMEOUT}s — likely stuck on a finalizer")
        _diagnose_stuck_olm_resources(release_name, namespace)

    return k8_util.k8_delete_custom_resource("operators.coreos.com", "v1alpha1", "catalogsources", namespace, f"{release_name}-catalog")

@log_arguments
def olm_force_cleanup(k8_cluster: common.k8_cluster, release_name: str, namespace: str):
    """Aggressively remove all OLM artifacts for an operator.

    Handles partial/broken installs where operator-sdk cleanup reports
    "not found" but stale CSVs, deployments, or Operator CRs remain and
    would block a fresh install.  Safe to call on a clean cluster (all
    delete calls are no-ops when the resource is absent).

    When operator-sdk cleanup times out, diagnoses stuck resources
    (finalizers, pending deletions), strips finalizers to unblock
    deletion, and retries the manual cleanup path.
    """
    # Graceful path first — covers the normal uninstall case
    olm_cleanup(k8_cluster, release_name, namespace)

    # Delete any lingering subscriptions for this release
    ret_code, subscriptions, _ = k8_util.k8_list_subscriptions()
    if ret_code == 0:
        for sub in subscriptions:
            if (sub['spec'].get('name') == release_name and
                    sub['metadata'].get('namespace') == namespace):
                k8_util.k8_delete_custom_resource(
                    "operators.coreos.com", "v1alpha1", "subscriptions",
                    namespace, sub['metadata']['name'])

    # Delete catalog source
    k8_util.k8_delete_custom_resource(
        "operators.coreos.com", "v1alpha1", "catalogsources",
        namespace, f"{release_name}-catalog")

    # Delete any CSVs matching this release name across all namespaces.
    # An AllNamespaces OperatorGroup causes OLM to copy the CSV to every namespace
    # in the cluster; scoping the delete to just `namespace` leaves stale Pending
    # copies everywhere and blocks the next install.
    ret_code, csv_list, _ = k8_util.k8_list_clusterserviceversions()
    if ret_code == 0:
        for csv in csv_list:
            if release_name in csv['metadata']['name']:
                csv_ns = csv['metadata'].get('namespace', namespace)
                k8_util.k8_delete_custom_resource(
                    "operators.coreos.com", "v1alpha1", "clusterserviceversions",
                    csv_ns, csv['metadata']['name'])

    # Delete stale controller-manager deployment (survives broken OLM installs)
    k8_util.k8_delete_deployment(namespace, f"{release_name}-controller-manager")

    # Delete stale install plans — leftover plans with Manual approval
    # or failed status prevent OLM from generating new ones
    ret_code, plans, _ = k8_util.k8_list_namespaced_custom_resource(
        "operators.coreos.com", "v1alpha1", "installplans", namespace)
    if ret_code == 0:
        for plan in plans:
            plan_name = plan["metadata"]["name"]
            Logger.info(f"Deleting stale install plan {plan_name}")
            k8_util.k8_delete_custom_resource(
                "operators.coreos.com", "v1alpha1", "installplans",
                namespace, plan_name)

    # Delete all resources carrying the OLM operator label BEFORE deleting
    # the Operator CR. OLM's reconciler recreates the Operator CR as long as
    # labeled resources (CRDs, ServiceAccounts, ClusterRoles, etc.) exist.
    olm_label = f"operators.coreos.com/{release_name}.{namespace}"
    k8_util.k8_delete_resources_by_label(namespace, olm_label)
    time.sleep(5)

    # Now delete the Operator CR — nothing labeled remains to trigger recreation
    k8_util.k8_delete_custom_resource(
        "operators.coreos.com", "v1", "operators", "",
        f"{release_name}.{namespace}")

    # Check for resources stuck on finalizers and unblock them
    stuck = _diagnose_stuck_olm_resources(release_name, namespace)
    if stuck:
        Logger.warning(f"Found {len(stuck)} stuck OLM resource(s) — removing finalizers to recover")
        _remove_finalizers_from_stuck_resources(release_name, namespace)
        time.sleep(5)
        remaining = _diagnose_stuck_olm_resources(release_name, namespace)
        if remaining:
            Logger.error(f"{len(remaining)} OLM resource(s) still stuck after finalizer removal")

    time.sleep(10)


def olm_approve_install_plans(namespace: str) -> (int, str, str):
    """Approve all pending Manual-approval OLM install plans in namespace.

    operator-sdk run bundle sets approval=Manual by default.  OLM will not
    proceed past the CRD registration step until the plan is approved.
    """
    ret_code, plans, err = k8_util.k8_list_namespaced_custom_resource(
        "operators.coreos.com", "v1alpha1", "installplans", namespace)
    if ret_code != 0:
        Logger.warning(f"Failed to list install plans in {namespace}: {err}")
        return ret_code, "", err

    approved_count = 0
    for plan in plans:
        if plan["spec"].get("approved", True):
            continue
        plan_name = plan["metadata"]["name"]
        Logger.info(f"Approving install plan {plan_name} in {namespace}")
        ret_code, _, err = k8_util.k8_patch_namespaced_custom_resource(
            "operators.coreos.com", "v1alpha1", "installplans",
            namespace, plan_name, {"spec": {"approved": True}})
        if ret_code != 0:
            Logger.error(f"Failed to approve install plan {plan_name}: {err}")
            return ret_code, "", err
        approved_count += 1

    Logger.debug(f"Approved {approved_count} install plan(s) in {namespace}")
    return 0, str(approved_count), ""


def olm_verify_healthy(namespace: str, release_name: str, timeout: int = 120) -> bool:
    """Return True when the OLM install is fully healthy.

    Polls until:
      - deviceconfigs.amd.com CRD is registered in the API server
      - The operator CSV is in Succeeded phase

    Returns False if neither condition is met within *timeout* seconds.
    """
    crd_name = "deviceconfigs.amd.com"
    deadline = time.time() + timeout
    while time.time() < deadline:
        missing = k8_util.k8_check_crds([crd_name])
        if missing:
            Logger.debug(f"CRD {crd_name} not yet registered, retrying in 10s...")
            time.sleep(10)
            continue

        ret_code, csv_list, _ = k8_util.k8_list_clusterserviceversions()
        if ret_code != 0:
            time.sleep(10)
            continue

        gpu_op_csv = next(
            (csv for csv in csv_list
             if release_name in csv['metadata']['name']
             and csv['metadata'].get('namespace') == namespace), None)
        if not gpu_op_csv:
            Logger.debug(f"No CSV for {release_name} in {namespace}, retrying in 10s...")
            time.sleep(10)
            continue

        phase = gpu_op_csv.get('status', {}).get('phase', '')
        if phase != 'Succeeded':
            Logger.debug(f"CSV phase is {phase!r}, waiting for Succeeded...")
            time.sleep(10)
            continue

        Logger.info(f"OLM install healthy: CRD {crd_name} registered, CSV Succeeded")
        return True

    Logger.error(
        f"OLM install unhealthy after {timeout}s: "
        f"CRD {crd_name} missing or CSV not in Succeeded phase")
    return False


@log_arguments
def olm_manage_amdgpu_driver_blacklist(enable : bool, is_mini_kube_cluster : bool) -> (int, str, str):
    """
    API to manage amdgpu driver-blacklist on openshift cluster

    Parameters:
    enable : True to enable, False to disable
    """

    # --- Resource Definition ---
    # The content "blacklist amdgpu\n" is base64 encoded as "YmxhY2tsaXN0IGFtZGdwdQo="
    # as shown in your YAML.

    GROUP = "machineconfiguration.openshift.io"
    VERSION = "v1"
    PLURAL = "machineconfigs" # The plural name for MachineConfig objects
    NAMESPACE = "openshift-machine-config-operator" # This is the standard namespace for MCO resources
    RESOURCE_NAME = "amdgpu-module-blacklist"
    ROLE_LABEL = "master" if is_mini_kube_cluster else "worker"
    CONFIG_PATH = "/etc/modprobe.d/amdgpu-blacklist.conf"
    CONFIG_CONTENT_BASE64 = "YmxhY2tsaXN0IGFtZGdwdQo="

    # Construct the MachineConfig body as a dictionary
    machine_config_body = {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "MachineConfig",
        "metadata": {
            "name": RESOURCE_NAME,
            "labels": {
                "machineconfiguration.openshift.io/role": ROLE_LABEL
            }
        },
        "spec": {
            "config": {
                "ignition": {
                    "version": "3.5.0"
                },
                "storage": {
                    "files": [
                        {
                            "path": CONFIG_PATH,
                            "mode": 420,
                            "overwrite": True,
                            "contents": {
                                "source": f"data:text/plain;base64,{CONFIG_CONTENT_BASE64}"
                            }
                        }
                    ]
                }
            }
        }
    }

    if enable:
        ret_code, cr_list, err = k8_util.k8_get_custom_resource_objects(group = GROUP, version = VERSION, plural = PLURAL)
        if ret_code != 0:
            return ret_code, cr_list, err
        amdgpu_mod_blklist_crobjs = list(filter(lambda x: x['metadata']['name'] == RESOURCE_NAME, cr_list))
        if len(amdgpu_mod_blklist_crobjs) == 0:
            Logger.info(f"{PLURAL} CustomResource object not found, create to blacklist amdgpu driver")
            return k8_util.k8_create_custom_resource(machine_config_body)
        else:
            Logger.warning(f"Found {len(amdgpu_mod_blklist_crobjs)} {PLURAL} CustomResource objects - proceeding as-is. Check logs for any inconsistency")
            Logger.debug(LogPrettyPrinter.pformat(amdgpu_mod_blklist_crobjs))
        return 0, "", ""
    else:
        return k8_util.k8_delete_custom_resource(GROUP, VERSION, PLURAL, NAMESPACE, RESOURCE_NAME)


@log_arguments
def patch_secrets(k8_cluster : common.k8_cluster, namespace : str) -> (int, str, str):
    """
    API to patch openshift serviceaccount with image pull secrets
    """
    patch = {
        "imagePullSecrets" : [],
    }
    for entry in k8_cluster.k8_secrets["secrets"]:
        patch["imagePullSecrets"].append({"name": entry.get("name")})

    Logger.debug(f"Applying following patch to Openshift default-namespace service-account, {patch}")
    ret_code, ret_stdout, ret_stderr = k8_util.k8_patch_serviceaccount(namespace, "amd-gpu-operator-controller-manager", patch)
    if ret_code != 0:
        Logger.error(f"failed to patch openshift serviceaccount with image pull-secret, stderr: {ret_stderr}")
        return ret_code, ret_stdout, ret_stderr
    return 0, "", ""
