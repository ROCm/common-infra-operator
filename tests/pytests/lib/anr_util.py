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

import json
import os
import pdb
import re
import logging
import pytest
import time
import lib.k8_util as k8_util
import lib.amdgpu as amdgpu_util
import lib.spec_util as spec_util
from lib.util import K8Helper
from datetime import datetime
from kubernetes import client, watch
import urllib3.exceptions

# Argo Workflows CRDs exceed the OpenAPIv3 validation cost budget on K8s < 1.30.
ANR_MIN_K8S_MINOR = 30


def k8s_supports_anr():
    """Return True if K8s version supports ANR (>= 1.30)."""
    rc, ver = k8_util.k8_get_version()
    if rc != 0:
        return True
    minor_m = re.match(r"(\d+)", str(ver.get("minor", "0")))
    minor = int(minor_m.group(1)) if minor_m else 0
    return minor >= ANR_MIN_K8S_MINOR


def apply_anr_guard(options, logger=None):
    """Disable ANR in helm options if K8s doesn't support it. Returns True if disabled."""
    if not k8s_supports_anr():
        options["remediationWorkflow.enable"] = "false"
        options["remediation.enabled"] = "false"
        options["remediation.installCRDs"] = "false"
        if logger:
            logger.warning(f"K8s < 1.{ANR_MIN_K8S_MINOR}: disabling ANR + Argo CRDs (cost budget exceeded)")
        return True
    return False

Logger = logging.getLogger("lib.anr_util")

_REMEDIATION_INSTANCE_ID = "amd-gpu-operator-remediation-workflow"
_AMDGPU_FEATURES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "files", "amdgpu-features.json")
_RECIPE_OVERHEAD_SECONDS = 300  # pod startup + framework init + result collection

def _load_recipe_timeouts():
    """Load and return the recipe-timeouts map from amdgpu-features.json (cached at module scope)."""
    with open(_AMDGPU_FEATURES_FILE, "r") as fp:
        return json.load(fp).get("recipe-timeouts", {})

_RECIPE_TIMEOUTS = _load_recipe_timeouts()


def get_drain_step_timeout(tcfg=None) -> int:
    """Return the configured drain timeout (seconds).

    Reads nodeDrainPolicy.timeoutSeconds from tcfg when set, otherwise falls
    back to the operator default of 300 s. This is the time the operator gives
    kubectl drain; it does not include pre/post-step scheduling overhead.
    Used as a building block inside get_anr_monitor_timeout.
    """
    tcfg = tcfg or {}
    if 'remediationWorkflow.nodeDrainPolicy.timeoutSeconds' in tcfg:
        return tcfg['remediationWorkflow.nodeDrainPolicy.timeoutSeconds']
    drain_policy = tcfg.get('remediationWorkflow.nodeDrainPolicy') or {}
    return drain_policy.get('timeoutSeconds', 300)


def get_anr_monitor_timeout(gpu_cluster, gpu_node, recipe_timeout=0,
                             with_reboot=False, tcfg=None) -> int:
    """Return the total test ceiling (seconds): drain + recipe + reboot + overhead.

    - drain    : nodeDrainPolicy.timeoutSeconds (tcfg) or operator default 300 s
    - recipe   : pre-computed recipe execution budget (0 when no recipe runs)
    - reboot   : per-device from amdgpu-features.json (only when with_reboot=True)
    - overhead : covers condition→reconcile→workflow creation→pod scheduling (pre-drain,
                 120–200 s on slow/OCP clusters) + post-step Argo bookkeeping
    """
    cluster_node = gpu_cluster.find_node_by_ip(k8_util.k8_get_node_address(gpu_node))
    gpu_features  = amdgpu_util.get_gpu_features(cluster_node.device_id)
    reboot        = gpu_features.get("anr", {}).get("reboot-overhead-seconds", 600) if with_reboot else 0
    overhead      = 300
    return get_drain_step_timeout(tcfg) + recipe_timeout + reboot + overhead


def get_recipe_timeout(framework, recipe, default=600) -> int:
    """Return timeout in seconds for a given (framework, recipe) pair including test step overhead."""
    return _RECIPE_TIMEOUTS.get(framework, {}).get(recipe, default) + _RECIPE_OVERHEAD_SECONDS


def get_framework_and_recipe(gpu_cluster, gpu_node, default_timeout=600):
    """Return (framework, recipe, timeout_seconds) based on GPU capabilities from amdgpu-features.json.
    Skips the test if no test-runner framework is supported on the GPU."""
    cluster_node = gpu_cluster.find_node_by_ip(k8_util.k8_get_node_address(gpu_node))
    tr_support = amdgpu_util.get_test_runner_support(cluster_node.device_id)
    if tr_support.get("agfhc", False):
        recipes = tr_support.get("agfhc_recipes") or ["gfx_lvl1"]
        agfhc_timeouts = _RECIPE_TIMEOUTS.get("AGFHC", {})
        recipe = min(recipes, key=lambda r: agfhc_timeouts.get(r, default_timeout))
        return "AGFHC", recipe, agfhc_timeouts.get(recipe, default_timeout) + _RECIPE_OVERHEAD_SECONDS
    if tr_support.get("rvs", False):
        recipes = tr_support.get("rvs_recipes") or ["babel"]
        rvs_timeouts = _RECIPE_TIMEOUTS.get("RVS", {})
        recipe = min(recipes, key=lambda r: rvs_timeouts.get(r, default_timeout))
        return "RVS", recipe, rvs_timeouts.get(recipe, default_timeout) + _RECIPE_OVERHEAD_SECONDS
    pytest.skip(f"No test-runner framework (RVS/AGFHC) supported on {cluster_node.gpu_series}")

def get_worker_nodes(gpu_nodes, all=False, strict=False):
    """Return GPU node(s) for a remediation test.
    all:    False → return one node (default), True → return list of nodes
    strict: False → prefer pure workers, fall back to master+worker 
            True  → pure workers only; skip the test if none exist
    """

    workers = [n for n in gpu_nodes if 'node-role.kubernetes.io/control-plane' not in n.get('metadata', {}).get('labels', {})
               and 'node-role.kubernetes.io/master' not in n.get('metadata', {}).get('labels', {})]
    if strict and not workers:
        pytest.skip("Test requires pure worker node(s); none found in the cluster")
    nodes = workers if (strict or workers) else gpu_nodes
    return nodes if all else nodes[0]


def _is_node_workflow(wf, node_name):
    """Return True only if wf belongs to node_name's remediation workflow.
    """
    name = wf.get('metadata', {}).get('name', '')
    labels = wf.get('metadata', {}).get('labels', {})
    return (
        name.startswith(f"{node_name}-")
        and labels.get('workflows.argoproj.io/controller-instanceid') == _REMEDIATION_INSTANCE_ID
    )


def _get_step_phases(node_name):
    """Return {displayName: phase} for the most recent workflow for this node."""
    _, workflows, _ = k8_util.k8_get_custom_resource_objects(
        group="argoproj.io", version="v1alpha1", plural="workflows"
    )
    node_wfs = sorted(
        [w for w in (workflows or []) if _is_node_workflow(w, node_name)],
        key=lambda w: w['metadata']['creationTimestamp']
    )
    if not node_wfs:
        return {}
    return {
        n['displayName']: n.get('phase')
        for n in node_wfs[-1].get('status', {}).get('nodes', {}).values()
        if 'displayName' in n
    }


def _wait_for_step(node_name, step_name, expected_phase, timeout=120):
    """Poll until the named step reaches expected_phase in the most recent workflow for this node.
    Returns True if reached within timeout, False otherwise."""
    deadline = time.time() + max(1, timeout)
    while time.time() < deadline:
        phases = _get_step_phases(node_name)
        if phases.get(step_name) == expected_phase:
            return True
        time.sleep(20)
    return False


def _wait_for_workflow_terminal(node_name, timeout=300, wf_names=None):
    """Poll until the most recent workflow for this node reaches a terminal state.

    If wf_names is provided, only workflows whose names are in that set are
    considered.  This prevents a newly-created workflow 2 from shadowing
    workflow 1 when both exist on the same node simultaneously.

    Returns the terminal phase string, or None on timeout."""
    terminal = {'Succeeded', 'Failed', 'Error'}
    deadline = time.time() + max(1, timeout)
    while time.time() < deadline:
        _, workflows, _ = k8_util.k8_get_custom_resource_objects(
            "argoproj.io", "v1alpha1", "workflows"
        )
        node_wfs = sorted(
            [w for w in (workflows or [])
             if _is_node_workflow(w, node_name)
             and (wf_names is None or w['metadata']['name'] in wf_names)],
            key=lambda w: w['metadata']['creationTimestamp']
        )
        if node_wfs:
            phase = node_wfs[-1].get('status', {}).get('phase')
            if phase in terminal:
                return phase
        time.sleep(10)
    return None


def _patch_sa_image_pull_secret(namespace, sa_name, secret_name):
    """Add secret_name to imagePullSecrets of the given ServiceAccount if not already present."""
    from kubernetes import client as k8s_client
    v1 = k8s_client.CoreV1Api()
    sa = v1.read_namespaced_service_account(sa_name, namespace)
    existing = [s.name for s in (sa.image_pull_secrets or [])]
    if secret_name not in existing:
        sa.image_pull_secrets = (sa.image_pull_secrets or []) + [k8s_client.V1LocalObjectReference(name=secret_name)]
        v1.patch_namespaced_service_account(sa_name, namespace, sa)


def _abort_workflow(node_name, timeout=120):
    """Label the node to abort its running workflow, wait for termination, then remove the label."""
    abort_label = {"operator.amd.com/gpu-abort-workflow": "true"}
    k8_util.k8_label_node(node_name, abort_label, overwrite=True)

    terminal = _wait_for_workflow_terminal(node_name, timeout=timeout)
    if terminal is None:
        Logger.warning(f"Workflow on '{node_name}' did not reach terminal state within {timeout}s after abort label applied")

    remove_label = {"operator.amd.com/gpu-abort-workflow": None}
    k8_util.k8_label_node(node_name, remove_label, overwrite=True)
    return terminal


def _get_node_info(tag, node_name):
    """Fetch and return the node object for node_name, or None on error."""
    ret_code, nodes = k8_util.k8_get_nodes()
    if ret_code != 0:
        Logger.error(f"[{tag}] Failed to fetch nodes from cluster.")
        return None
    node_info = next((n for n in nodes if n['metadata']['name'] == node_name), None)
    if node_info is None:
        Logger.error(f"[{tag}] Node '{node_name}' not found in cluster.")
    return node_info

def _verify_node_taints(node_name, expected_taints, tag, expect_present=True):
    """Verify taint presence or absence on a node.

    expect_present=True  — all expected taints must be applied to the node (used after taint step).
    expect_present=False — all expected taint keys must be gone from the node (used after untaint step).
    """
    node_info = _get_node_info(tag, node_name)
    if node_info is None:
        return False

    applied = node_info.get('spec', {}).get('taints', []) or []

    if not expected_taints:
        Logger.info(f"[{tag}] No taints configured; {len(applied)} taint(s) currently on node.")
        return True

    if expect_present:
        applied_set = {(t['key'], t.get('value', ''), t['effect']) for t in applied}
        missing_taints = [t for t in expected_taints
                          if (t.partition(":")[0].partition("=")[0], t.partition(":")[0].partition("=")[2], t.partition(":")[2]) not in applied_set]
        if missing_taints:
            Logger.error(
                f"[{tag}] Expected taint(s) not found on '{node_name}': {missing_taints}. "
                f"Applied: {[(t['key'], t.get('value'), t['effect']) for t in applied]}"
            )
        else:
            Logger.info(f"[{tag}] {expected_taints} taint(s) confirmed on '{node_name}'.")
        return not missing_taints
    else:
        remaining_taints = [t for t in expected_taints if any(a['key'] == t.split(":")[0].split("=")[0] for a in applied)]
        if remaining_taints:
            Logger.error(f"[{tag}] Remediation taint(s) still on '{node_name}': {remaining_taints}")
        else:
            Logger.info(f"[{tag}] All remediation taint(s) removed from '{node_name}'.")
        return not remaining_taints

def _verify_drain(node_name, drain_policy):
    """Verify drain completed: no evictable pods remain on the node."""
    ignore_daemonsets = drain_policy.get('ignoreDaemonSets', True)
    ignore_namespaces = set(drain_policy.get('ignoreNamespaces') or [])

    ret_code, pods = k8_util.k8_get_pods(namespace=None, node_name=node_name)
    if ret_code != 0:
        Logger.error(f"[drain] Failed to list pods on '{node_name}'.")
        return False

    evictable_remaining = []
    for pod in pods:
        meta = pod['metadata']
        ns, pod_name = meta['namespace'], meta['name']

        if "kubernetes.io/config.mirror" in (meta.get('annotations') or {}):
            continue
        if (meta.get('labels') or {}).get("workflows.argoproj.io/controller-instanceid") == "amd-gpu-operator-remediation-workflow":
            continue
        if ignore_daemonsets and any(r.get('kind') == 'DaemonSet' for r in (meta.get('owner_references') or [])):
            continue
        if ns in ignore_namespaces:
            continue

        evictable_remaining.append(f"{ns}/{pod_name}")

    if evictable_remaining:
        Logger.error(f"[drain] {len(evictable_remaining)} evictable pod(s) still on '{node_name}': {evictable_remaining}")
        return False

    Logger.info(f"[drain] All evictable pods removed from '{node_name}'.")
    return True

def _verify_wait(node_name, condition_type):
    """After wait: confirm the triggering node condition has been resolved."""
    node_info = _get_node_info("wait", node_name)
    if node_info is None:
        return False

    conditions = node_info.get('status', {}).get('conditions', []) or []
    cond = next((c for c in conditions if c['type'] == condition_type), None)
    resolved = cond is None or cond.get('status') != "True"

    if resolved:
        Logger.info(f"[wait] Condition '{condition_type}' resolved on '{node_name}'.")
    else:
        Logger.error(f"[wait] Condition '{condition_type}' still 'True' on '{node_name}'.")
    return resolved

def _verify_node_labels(node_name, expected_labels, tag, expect_present=True):
    """Verify label presence or absence on a node.

    expect_present=True  — all label keys must be on the node (used after applylabels step).
    expect_present=False — all label keys must be gone from the node (used after removelabels step).
    """
    if not expected_labels:
        Logger.info(f"[{tag}] No labels configured; step is a no-op.")
        return True

    node_labels = k8_util.k8_get_node_labels(node_name)
    if node_labels is None:
        Logger.error(f"[{tag}] Failed to fetch labels for '{node_name}'.")
        return False

    label_keys = [lkv.partition("=")[0] for lkv in expected_labels]
    if expect_present:
        missing_labels = [k for k in label_keys if k not in node_labels]
        if missing_labels:
            Logger.error(f"[{tag}] Label(s) not found on '{node_name}': {missing_labels}")
        else:
            Logger.info(f"[{tag}] All configured label(s) confirmed on '{node_name}'.")
        return not missing_labels
    else:
        remaining_labels = [k for k in label_keys if k in node_labels]
        if remaining_labels:
            Logger.error(f"[{tag}] Remediation label(s) still on '{node_name}': {remaining_labels}")
        else:
            Logger.info(f"[{tag}] All remediation label(s) removed from '{node_name}'.")
        return not remaining_labels

def _verify_reboot(node_name, pre_reboot_boot_id):
    """Verify the node actually rebooted by checking the kernel bootID after waitfornodeready.

    Called when the 'waitfornodeready' step Succeeds — at that point the node is back up
    and the bootID reflects the new boot. Compares against pre_reboot_boot_id captured
    before the workflow started. Returns True if bootID changed, False if unchanged.
    If pre_reboot_boot_id is None the check is skipped with a warning and returns True.
    """
    if pre_reboot_boot_id is None:
        Logger.warning(f"[reboot] Pre-reboot bootID not captured for '{node_name}'; skipping verification.")
        return True

    node_info = _get_node_info("reboot", node_name)
    post_boot_id = (node_info or {}).get('status', {}).get('node_info', {}).get('boot_id') if node_info else None

    if post_boot_id is None:
        Logger.error(f"[reboot] Could not read post-reboot bootID for '{node_name}'.")
        return False

    if post_boot_id == pre_reboot_boot_id:
        Logger.error(
            f"[reboot] bootID unchanged on '{node_name}' (bootID={pre_reboot_boot_id!r}). "
            "Reboot step Succeeded in the workflow but the node was NOT actually rebooted."
        )
        return False

    Logger.info(f"[reboot] bootID changed on '{node_name}': {pre_reboot_boot_id!r} -> {post_boot_id!r}. Reboot confirmed.")
    return True

def _verify_notifybeforesuspend(namespace, node_name):
    """Verify the notifybeforesuspend step emitted a Warning event on the node.

    Mirrors _verify_node_labels / _verify_node_taints: called after the step has already
    Succeeded and checks only the side-effect — that a k8s Warning event with
    reason 'AMDGPUUnhealthy' (set by the workflow notify template) was created for the node.

    Returns True if the event is present, False otherwise.
    """
    ret_code, events, _ = k8_util.k8_get_events(namespace=namespace)
    if ret_code != 0:
        Logger.error(f"[notifybeforesuspend] Failed to fetch events from namespace '{namespace}'.")
        return False

    for ev in events.items:
        involved = ev.involved_object
        if (involved.kind == "Node" and involved.name == node_name
                and ev.reason == "AMDGPUUnhealthy"
                and ev.type == "Warning"):
            Logger.info(
                f"[notifybeforesuspend] Found expected Warning event "
                f"'{ev.metadata.name}' on node '{node_name}': {ev.message}"
            )
            return True

    Logger.error(
        f"[notifybeforesuspend] No Warning event with reason 'AMDGPUUnhealthy' "
        f"found on node '{node_name}'."
    )
    return False


def _build_step_dispatch(wf, node_name, condition_type, environment, pre_reboot_boot_id=None):
    """Extract workflow parameters and build the step-handler dispatch table.

    Returns (step_dispatch, expected_taints, expected_labels) or (None, None, None)
    if the workflow has no parameters yet.
    pre_reboot_boot_id: bootID captured before the workflow started, forwarded to
    _verify_reboot so it can confirm the node actually rebooted.
    """
    params = {
        p['name']: p.get('value')
        for p in wf.get('spec', {}).get('arguments', {}).get('parameters', [])
    }
    if not params:
        return None, None, None

    expected_taints = json.loads(params['node_taints'])  if params.get('node_taints')  else []
    expected_labels = json.loads(params['node_labels'])  if params.get('node_labels')  else []
    drain_policy    = json.loads(params['drain_policy']) if params.get('drain_policy') else {}

    step_dispatch = {
        'applylabels':      lambda _:  _verify_node_labels(node_name, expected_labels,   tag="applylabels", expect_present=True),
        'taint':            lambda _:  _verify_node_taints(node_name, expected_taints,   tag="taint",       expect_present=True),
        'drain':            lambda _:  _verify_drain(node_name, drain_policy),
        'suspend':          lambda _:  (Logger.info(f"[suspend] Gate passed on '{node_name}'.") or True),
        'reboot':           lambda _:  (Logger.info(f"[reboot] Reboot initiated on '{node_name}'; bootID will be checked after waitfornodeready.") or True),
        'waitfornodeready': lambda _:  _verify_reboot(node_name, pre_reboot_boot_id),
        'wait':             lambda _:  _verify_wait(node_name, condition_type),
        'untaint':          lambda _:  _verify_node_taints(node_name, expected_taints,   tag="untaint",     expect_present=False),
        'removelabels':     lambda dn: _verify_node_labels(node_name, expected_labels,   tag=dn,            expect_present=False),
    }
    return step_dispatch, expected_taints, expected_labels


def monitor_and_patch_remediation(environment, node_name, condition_type, timeout=800,
                                   stop_at_step=None, skip_test_patch=False):
    """
    Stream Argo Workflow events for the target node and verify each remediation step.

    Steps verified: applylabels, taint, drain, suspend, reboot, test, wait, untaint, removelabels.
    Returns (ret_code, stdout_msg, stderr_msg).

    stop_at_step: tuple (step_name, phase) — returns early with (0, ..., None) as soon as
    the named step reaches that phase, without waiting for the full workflow to complete.
    Example: stop_at_step=('suspend', 'Running')
    """
    Logger.info(f"Monitoring remediation workflow for node '{node_name}', condition '{condition_type}'")

    # Capture boot ID before the workflow runs so _verify_reboot can detect a genuine reboot.
    pre_reboot_boot_id = None
    node_info = _get_node_info("reboot-pre", node_name)
    if node_info:
        pre_reboot_boot_id = node_info.get('status', {}).get('node_info', {}).get('boot_id')
        Logger.info(f"[reboot-pre] Captured bootID for '{node_name}': {pre_reboot_boot_id!r}")

    seen_node_ids = set()
    step_dispatch = {}
    step_results  = {}
    deadline = time.time() + timeout

    while time.time() < deadline:
        remaining = deadline - time.time()
        if remaining <= 0:
            break

        try:
            for event in watch.Watch().stream(
                client.CustomObjectsApi().list_namespaced_custom_object,
                group="argoproj.io",
                version="v1alpha1",
                namespace=environment.gpu_operator_namespace,
                plural="workflows",
                timeout_seconds=int(remaining),
            ):
                if time.time() > deadline:
                    Logger.warning(f"Client-side deadline ({timeout}s) exceeded for workflow on '{node_name}'")
                    break

                wf = event["object"]
                if not _is_node_workflow(wf, node_name):
                    continue

                wf_name  = wf['metadata']['name']
                status   = wf.get('status', {})
                wf_phase = status.get('phase')

                if wf_phase in ('Failed', 'Error'):
                    Logger.error(f"Workflow '{wf_name}' reached terminal phase: {wf_phase}")
                    return -1, None, f"Workflow terminal phase: {wf_phase}"

                if not step_dispatch:
                    step_dispatch, _, _ = _build_step_dispatch(wf, node_name, condition_type, environment, pre_reboot_boot_id)

                for node_id, node_data in status.get('nodes', {}).items():
                    node_phase   = node_data.get('phase', '')
                    display_name = node_data.get('displayName', '')

                    if stop_at_step and display_name == stop_at_step[0] and node_phase == stop_at_step[1]:
                        Logger.info(f"[{stop_at_step[0]}] Reached '{stop_at_step[1]}' on '{node_name}'.")
                        return 0, f"{stop_at_step[0]} reached {stop_at_step[1]}", None

                    if node_id in seen_node_ids:
                        continue

                    if node_phase in ('Failed', 'Error'):
                        Logger.error(f"Step '{display_name}' reached terminal phase: {node_phase}")
                        return -1, None, f"Step '{display_name}' failed with phase: {node_phase}"

                    if node_phase == 'Skipped':
                        seen_node_ids.add(node_id)
                        Logger.info(f"[{display_name}] Step skipped.")
                        continue

                    if node_phase != 'Succeeded':
                        continue

                    seen_node_ids.add(node_id)
                    if display_name == 'test':
                        if not skip_test_patch:
                            patch_node_condition(environment, node_name,
                                                 condition_type=condition_type, condition_status=False)
                            Logger.info(f"[test] Cleared condition '{condition_type}' on '{node_name}'.")
                        else:
                            Logger.info(f"[test] skip_test_patch=True — not clearing condition '{condition_type}' synthetically.")
                        step_results['test'] = True
                    elif step_dispatch and (handler := step_dispatch.get(display_name)):
                        step_results[display_name] = handler(display_name)

                if wf_phase == 'Succeeded':
                    failed_steps = [s for s, ok in step_results.items() if not ok]
                    if failed_steps:
                        Logger.warning(f"Workflow '{wf_name}' succeeded but node checks failed for: {failed_steps}")
                        return -1, None, f"Node-level verification failed for steps: {failed_steps}"
                    Logger.info(
                        f"Workflow '{wf_name}' completed successfully. "
                        f"All node checks passed: {list(step_results.keys())}"
                    )
                    return 0, "workflow completed", None

        except urllib3.exceptions.ProtocolError as exc:
            Logger.warning(f"Watch stream dropped (node reboot?) for '{node_name}': {exc} — re-establishing watch")
        except (urllib3.exceptions.MaxRetryError, urllib3.exceptions.NewConnectionError) as exc:
            # API server is unreachable — happens on SNO/OCP clusters where the control plane
            # and GPU worker are the same node. Sleep before retrying so we don't spin.
            Logger.warning(f"API server unreachable for '{node_name}': {exc} — sleeping 30s before retry")
            time.sleep(30)

        # Stream ended (clean close, ProtocolError, or API-down) — check if already terminal
        # before re-opening the watch so we don't spin when the workflow finished during the drop.
        remaining = deadline - time.time()
        if remaining > 0:
            Logger.info(f"Watch stream ended for '{node_name}' — checking current state before re-establishing ({remaining:.0f}s remaining)")
            try:
                terminal_phase = _wait_for_workflow_terminal(node_name, timeout=min(30, remaining))
            except (urllib3.exceptions.MaxRetryError, urllib3.exceptions.NewConnectionError) as exc:
                Logger.info(f"API server still unreachable for '{node_name}': {exc} — will retry watch")
                terminal_phase = None
            if terminal_phase == 'Succeeded':
                Logger.info(f"Workflow for '{node_name}' already Succeeded after stream re-check")
                return 0, "workflow completed (detected after stream re-check)", None
            if terminal_phase in ('Failed', 'Error'):
                return -1, None, f"Workflow terminal phase (after stream re-check): {terminal_phase}"
            Logger.info(f"Workflow not yet terminal — re-establishing watch stream for '{node_name}'")

    return -1, None, "Watcher timed out"

def cleanup_workflow(deviceconfig_install, environment, condition_type, config_overrides=None, images=None):
    condition_types = condition_type if isinstance(condition_type, list) else [condition_type]
    custom_cm_name = None
    default_cm_names = []
    custom_taints = []
    remediation_labels = {}
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        remediation_labels.update(tcfg.get('remediationWorkflow.nodeRemediationLabels', {}))
        if tcfg.get('remediationWorkflow.config'):
            custom_cm_name = tcfg.get('remediationWorkflow.config')
            del tcfg['remediationWorkflow.config']
        # Restore testerImage to the fixture default (RVS test-runner) so that
        # tests which override it (e.g. to AGFHC) don't leak into subsequent tests,
        # and tests that don't override it still get a valid image (GPUOP-975).
        tcfg['remediationWorkflow.testerImage.repository'] = images.get('testRunner.image.repository')
        tcfg['remediationWorkflow.testerImage.version'] = images.get('testRunner.image.version')
        custom_taints = tcfg.get('remediationWorkflow.nodeRemediationTaints', [])
        if custom_taints:
            del tcfg['remediationWorkflow.nodeRemediationTaints']
        if tcfg.get('remediationWorkflow.nodeDrainPolicy.force', None) is not None:
            del tcfg['remediationWorkflow.nodeDrainPolicy.force']
        if tcfg.get('remediationWorkflow.nodeDrainPolicy.timeoutSeconds', None) is not None:
            del tcfg['remediationWorkflow.nodeDrainPolicy.timeoutSeconds']
        if tcfg.get('remediationWorkflow.nodeDrainPolicy.gracePeriodSeconds', None) is not None:
            del tcfg['remediationWorkflow.nodeDrainPolicy.gracePeriodSeconds']
        if tcfg.get('remediationWorkflow.nodeDrainPolicy.ignoreDaemonSets', None) is not None:
            del tcfg['remediationWorkflow.nodeDrainPolicy.ignoreDaemonSets']
        if config_overrides:
            for key, value in config_overrides.items():
                tcfg[key] = value
        else:
            tcfg['remediationWorkflow.enable'] = False

        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, _, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, (ret_code == 0), f"Failed to modify deviceconfig: {ret_stderr}")
        default_cm_names.append(f"{cr_spec['metadata']['name']}-default-conditional-workflow-mappings")

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    for node in gpu_nodes:
        node_name = node['metadata']['labels']['kubernetes.io/hostname']
        conditions = node.get('status', {}).get('conditions', [])
        for ctype in condition_types:
            hang_cond = next((c for c in conditions if c['type'] == ctype), {})
            if hang_cond.get('status') == "True":
                patch_node_condition(environment, node_name, condition_type=ctype, condition_status=False)

        # Keys to match: custom if configured, else the operator default
        remediation_keys = {ct['key'] for ct in custom_taints} if custom_taints else {"amd-gpu-unhealthy"}
        applied_taints = node.get('spec', {}).get('taints', []) or []
        for taint in applied_taints:
            if taint.get('key') in remediation_keys:
                k8_util.k8_untaint_node(node_name, effects=[taint['effect']],
                                        taint_key=taint['key'], taint_value=taint.get('value', ''))
                Logger.info(f"Removed taint '{taint['key']}={taint.get('value')}' from node '{node_name}'")

        # Remove remediation labels applied by the workflow's applylabels step
        if not remediation_labels:
            remediation_labels = {"amd.com/gpu.remediating": "true"}
        node_labels = node['metadata'].get('labels', {})
        labels_to_remove = {k: None for k in remediation_labels if k in node_labels}
        if labels_to_remove:
            k8_util.k8_label_node(node_name, labels_to_remove)
            Logger.info(f"Removed remediation labels from node '{node_name}': {list(labels_to_remove.keys())}")

    ret_code, workflows, err = k8_util.k8_get_custom_resource_objects( group="argoproj.io", version="v1alpha1", plural="workflows")
    K8Helper.triage(environment, (ret_code == 0) , f"failed to get workflows in {environment.gpu_operator_namespace}")
    for wf in workflows:
        wf_name = wf['metadata']['name']
        Logger.info(f"[anr-debug] workflow={wf_name}:\n{json.dumps(wf, indent=2, default=str)}")
        ret_code, std_out, std_err = k8_util.k8_delete_custom_resource( group="argoproj.io",version="v1alpha1", plural="workflows",  namespace=environment.gpu_operator_namespace, name=wf_name)
        K8Helper.triage(environment, (ret_code == 0) , f"unable to delete workflows in {environment.gpu_operator_namespace}: {std_err}")

    ret_code, rws_list, err = k8_util.k8_get_custom_resource_objects(group="amd.com", version="v1alpha1", plural="remediationworkflowstatuses")
    if ret_code == 0:
        if rws_list:
            Logger.info(f"[anr-debug] RemediationWorkflowStatus:\n{json.dumps(rws_list, indent=2, default=str)}")
        for rws in (rws_list or []):
            rws_name = rws['metadata']['name']
            ret_code, _, std_err = k8_util.k8_delete_custom_resource(group="amd.com", version="v1alpha1", plural="remediationworkflowstatuses", namespace=environment.gpu_operator_namespace, name=rws_name)
            if ret_code != 0:
                Logger.warning(f"Failed to delete RemediationWorkflowStatus {rws_name}: {std_err}")
            else:
                Logger.info(f"RemediationWorkflowStatus '{rws_name}' deleted (recovery policy reset)")
    else:
        Logger.warning(f"Failed to list RemediationWorkflowStatus objects: {err}")
    time.sleep(25)

    if custom_cm_name:
        ret_code, _, ret_stderr = k8_util.k8_delete_configmap(environment.gpu_operator_namespace, custom_cm_name)
        if ret_code != 0:
            Logger.warning(f"Failed to delete custom configmap {custom_cm_name}: {ret_stderr}")


def patch_node_condition(environment, node_name, condition_type, condition_status):
 
    if condition_status:
        condition_body = {
                        "status": {
                            "conditions": [
                                {   
                                    "type": condition_type,
                                    "lastTransitionTime": datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
                                    "message": "Simulated node condition",
                                    "reason": "Node Condition Detected",
                                    "status": "True",
                                }
                            ]
                        }
                    }   
    else:
        condition_body = {
                        "status": {
                            "conditions": [
                                {   
                                    "type": condition_type,
                                    "lastTransitionTime": datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
                                    "message": "",
                                    "reason": "Resolved",
                                    "status": "False",
                                }
                            ]
                        }
                    }   

    ret_code, resp , err = k8_util.k8_patch_node_status(node_name, condition_body)
    K8Helper.triage(environment, ret_code == 0 , f"failed to patch the node status : {err}")

def collect_workflow_logs(environment, node_names=None, since_time=None):
    """Log ANR diagnostics on test failure into the execution log.

    Logs are written to the captured pytest execution log (k8_test_run.log)
    so diagnostics are always available even without post-run cluster access.

    since_time: datetime (UTC) marking when the test started; only logs/CRs
    created at or after this time are included to avoid stale data from prior tests.
    """
    namespace = environment.gpu_operator_namespace

    since_seconds = None
    if since_time is not None:
        delta = (datetime.utcnow() - since_time).total_seconds()
        since_seconds = max(int(delta) + 5, 10)
    since_arg = f"{since_seconds}s" if since_seconds else "180s"

    # 1. GPU operator controller-manager logs
    try:
        rc_log, logs, _ = k8_util.k8_get_pod_logs("controller-manager", namespace, since=since_arg)
        if rc_log == 0 and logs:
            Logger.info(f"[anr-debug] controller-manager logs:\n{logs}")
    except Exception as exc:
        Logger.warning(f"[anr-debug] Error fetching controller-manager logs: {exc}")

    # 2. Argo workflow-controller logs
    wf_controller_name = "workflow-controller" if environment.deployment_mode == "openshift" else "amd-gpu-operator-workflow-controller"
    wf_controller_ns = namespace if environment.deployment_mode != "openshift" else "argo-workflow"
    try:
        rc_log, logs, _ = k8_util.k8_get_pod_logs(wf_controller_name, wf_controller_ns, since=since_arg)
        if rc_log == 0 and logs:
            Logger.info(f"[anr-debug] workflow-controller logs:\n{logs}")
    except Exception as exc:
        Logger.warning(f"[anr-debug] Error fetching workflow-controller logs: {exc}")


def _create_evictable_pod(node_name, namespace, name, grace_period_seconds=30, timeout=60):
    """Create a bare evictable pod pinned to node_name and wait until it is Running.

    The pod has no owner reference so it is a plain evictable target — not managed by any
    controller.  Uses a sleep container so it stays alive until evicted or deleted.
    Raises RuntimeError if the pod does not reach Running within timeout seconds.
    """
    from kubernetes import client as k8s_client
    v1 = k8s_client.CoreV1Api()
    pod = k8s_client.V1Pod(
        metadata=k8s_client.V1ObjectMeta(name=name, namespace=namespace),
        spec=k8s_client.V1PodSpec(
            node_name=node_name,
            termination_grace_period_seconds=grace_period_seconds,
            restart_policy="Never",
            containers=[k8s_client.V1Container(
                name="sleeper",
                image="busybox",
                command=["sleep", "3600"],
            )],
        ),
    )
    try:
        v1.create_namespaced_pod(namespace=namespace, body=pod)
    except k8s_client.ApiException as e:
        if e.status != 409:
            raise
        Logger.info(f"[evictable-pod] Pod '{name}' already exists in '{namespace}', reusing.")

    deadline = time.time() + timeout
    while time.time() < deadline:
        ret_code, pods = k8_util.k8_get_pods(namespace=namespace, node_name=node_name)
        if ret_code == 0:
            for p in (pods or []):
                if p['metadata']['name'] == name and p.get('status', {}).get('phase') == 'Running':
                    Logger.info(f"[evictable-pod] Pod '{name}' is Running on '{node_name}'.")
                    return
        time.sleep(5)
    raise RuntimeError(f"Pod '{name}' did not reach Running on '{node_name}' within {timeout}s")


def _pod_on_node(node_name, namespace, name, timeout=60):
    """Return True if pod is still present on node_name, False once fully gone.

    Polls until the pod disappears completely. If it is stuck in Terminating beyond
    timeout seconds, returns True (treated as still present — a real failure).
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        ret_code, pods = k8_util.k8_get_pods(namespace=namespace, node_name=node_name)
        if ret_code != 0:
            Logger.error(f"[pod-on-node] Failed to list pods on '{node_name}'.")
            return False
        match = next((p for p in (pods or []) if p['metadata']['name'] == name), None)
        if match is None:
            return False
        if match['metadata'].get('deletion_timestamp') is None:
            return True
        time.sleep(5)
    Logger.error(f"[pod-on-node] Pod '{name}' stuck Terminating on '{node_name}' after {timeout}s")
    return True


def _delete_pod_safe(pod_name, namespace):
    """Delete a pod only if it exists. Safe to use in finalizers."""
    ret_code, pods = k8_util.k8_get_pods(namespace=namespace, pod_name_pattern=pod_name)
    if ret_code != 0 or not pods:
        return
    ret_code, _, err = k8_util.k8_delete_pod(pod_name, namespace)
    if ret_code != 0:
        Logger.error(f"[delete-pod-safe] Failed to delete pod '{pod_name}' in '{namespace}': {err}")


def _get_daemonset_pod_uids(node_name):
    """Return {name: uid} for pods on node_name owned by a DaemonSet controller."""
    ret_code, pods = k8_util.k8_get_pods(namespace=None, node_name=node_name)
    if ret_code != 0:
        Logger.error(f"[ds-pods] Failed to list pods on '{node_name}'.")
        return {}
    return {
        p['metadata']['name']: p['metadata']['uid']
        for p in (pods or [])
        if any(r.get('kind') == 'DaemonSet' for r in (p['metadata'].get('owner_references') or []))
    }


def _wait_for_daemonset_pods_evicted(node_name, old_uids, timeout=120):
    """Poll until all UIDs in old_uids are gone from the node (pods were evicted).

    DaemonSet controllers create new pods with different names on recreation, so
    checking by name is unreliable. Instead, poll the current UID set and wait
    until none of the old UIDs are present on the node.

    Returns the set of old UIDs that were successfully evicted (no longer present).
    """
    old_uid_set = set(old_uids.values())
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = _get_daemonset_pod_uids(node_name)
        current_uid_set = set(current.values())
        evicted = old_uid_set - current_uid_set
        if evicted == old_uid_set:
            return evicted
        time.sleep(5)
    current = _get_daemonset_pod_uids(node_name)
    return old_uid_set - set(current.values())

def wait_for_configmap_from_image(environment, devcfg_name, timeout=120):
    """Wait for ConfigMap to be created from configMapImage by the operator's Job.
    Returns True if ConfigMap exists, False on timeout."""
    configmap_name = f"{devcfg_name}-default-conditional-workflow-mappings"
    deadline = time.time() + timeout
    last_error = None

    while time.time() < deadline:
        ret_code, config_map, err_msg = k8_util.k8_get_configmap(environment.gpu_operator_namespace, configmap_name)
        if ret_code == 0 and config_map is not None:
            Logger.info(f"ConfigMap '{configmap_name}' created successfully from configMapImage")
            return True
        if err_msg:
            last_error = err_msg
        time.sleep(5)

    if last_error:
        Logger.error(f"ConfigMap '{configmap_name}' not found after {timeout}s; last error from k8_get_configmap: {last_error}")
    else:
        Logger.error(f"ConfigMap '{configmap_name}' not found after {timeout}s")
    return False