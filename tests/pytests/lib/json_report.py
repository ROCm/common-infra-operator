
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
import logging
import os
import re
import time as _time

import pytest
from datetime import datetime

Logger = logging.getLogger("root.json_report")

# ---------------------------------------------------------------------------
# Module-level accumulators — reset per session via reset().
# ---------------------------------------------------------------------------
_results = []
_session_start = None
_run_progression = {}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
JOBD_ENV_VARS = [
    "JOB_ID", "JOB_PR", "JOB_BRANCH", "JOB_BRANCH_COMMIT",
    "JOB_BASE_BRANCH", "JOB_BASE_COMMIT", "JOB_REPOSITORY",
    "JOB_BASE_REPOSITORY", "JOB_FORK_REPOSITORY", "JOBD_VCPUS",
    "TARGET_ID", "TARGET_NAME", "RELEASE", "MAX_DURATION",
    "GITHUB_LABELS", "HOST_IP",
]

INFRA_FAILURE_PATTERNS = [
    (r"Failed to install\b", "helm-install-failure"),
    (r"Failed to uninstall\b", "helm-uninstall-failure"),
    (r"Failed to deploy\b", "deploy-failure"),
    (r"timed out waiting for the condition", "helm-timeout"),
    (r"connection refused", "connection-refused"),
    (r"Max retries exceeded", "connection-retry-exhausted"),
    (r"Failed to establish a new connection", "connection-failure"),
    (r"No route to host|Network is unreachable", "network-unreachable"),
    (r"Name or service not known|Could not resolve host", "dns-failure"),
    (r"ssh.*timeout|SSH.*timed out|paramiko.*timeout", "ssh-timeout"),
    (r"nodes are available.*untolerated taint", "node-taint-scheduling"),
    (r"ErrImagePull|ImagePullBackOff|Failed to pull image", "image-pull-failure"),
    (r"failed to find amd/gpu nodes", "gpu-discovery-failure"),
    (r"No GPU nodes found|gpu.*not.*found|0 GPUs", "gpu-not-found"),
    (r"kube_config_file|kubeconfig|KUBECONFIG", "kubeconfig-missing"),
    (r"Failed to watch DaemonSet rollout", "daemonset-rollout-failure"),
    (r"OOMKilled|OutOfMemory|Cannot allocate memory", "oom-failure"),
]
_INFRA_PATTERNS_COMPILED = [
    (re.compile(p, re.IGNORECASE), tag) for p, tag in INFRA_FAILURE_PATTERNS
]

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def classify_failure(error_message, failure_phase):
    if not error_message:
        return None, None
    for pattern, tag in _INFRA_PATTERNS_COMPILED:
        if pattern.search(error_message):
            return "infra", tag
    if failure_phase == "setup":
        return "infra", "fixture-setup-failure"
    return "product", None


def extract_setup_error_message(longrepr):
    if not longrepr:
        return None
    text = str(longrepr)
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith("E "):
            msg = stripped[2:].strip()
            if msg and msg != "Failed":
                return msg
    lines = text.splitlines()
    if lines:
        last = lines[-1].strip()
        if ": " in last:
            return last.split(": ", 1)[-1]
        return last
    return None

# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def reset(session_start):
    global _results, _session_start, _run_progression
    _session_start = session_start
    _results = []
    _run_progression = {}


def record_milestone(key, data):
    _run_progression[key] = data


def record_test_result(report, item):
    if report.when == "call":
        error_msg = report.error_summary if report.failed else None
        category, tag = classify_failure(error_msg, "call") if report.failed else (None, None)
        _results.append({
            "nodeid": report.nodeid,
            "outcome": report.outcome,
            "duration_seconds": round(report.duration, 3),
            "description": getattr(report, "description", ""),
            "error_message": error_msg,
            "failure_phase": "call" if report.failed else None,
            "failure_category": category,
            "failure_tag": tag,
            "markers": [m.name for m in item.iter_markers()],
        })
    elif report.when == "setup" and (report.skipped or report.failed):
        error_msg = extract_setup_error_message(report.longrepr) if report.failed else (
            (str(report.longrepr).splitlines() or [None])[-1] if report.longrepr else None
        )
        category, tag = classify_failure(error_msg, "setup") if report.failed else (None, None)
        _results.append({
            "nodeid": report.nodeid,
            "outcome": "skipped" if report.skipped else "error",
            "duration_seconds": round(report.duration, 3),
            "description": "",
            "error_message": error_msg,
            "failure_phase": "setup" if report.failed else None,
            "failure_category": category,
            "failure_tag": tag,
            "markers": [m.name for m in item.iter_markers()],
        })

# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate(session, logdir):
    gen_start = _time.monotonic()

    report = {"schema_version": "1.1"}

    # --- job_info ---
    report["job_info"] = {
        var.lower(): os.environ.get(var) for var in JOBD_ENV_VARS
    }

    # --- platform ---
    platform = {
        "deployment_mode": session.config.option.deployment
        if hasattr(session.config.option, "deployment") else None,
    }

    if hasattr(pytest, "_amdgpu_driver_spec"):
        spec = pytest._amdgpu_driver_spec
        platform["driver_version"] = spec.get("default-version")
        platform["driver_deployment"] = spec.get("driver-deployment")
        platform["driver_spec"] = spec
    else:
        platform["driver_version"] = None
        platform["driver_deployment"] = None
        platform["driver_spec"] = None

    if hasattr(pytest, "_image_info"):
        platform["image_manifest"] = {
            k: v for k, v in pytest._image_info.items() if k != "image_folder"
        }
    else:
        platform["image_manifest"] = None

    if hasattr(pytest, "_gim_driver_spec"):
        platform["gim_driver_version"] = pytest._gim_driver_spec.get("default-version")
    else:
        platform["gim_driver_version"] = None

    if hasattr(pytest, "_gim_node_info"):
        platform["gim_node_info"] = pytest._gim_node_info
    else:
        platform["gim_node_info"] = None

    report["platform"] = platform

    # --- nodes ---
    nodes_list = []
    if hasattr(pytest, "_k8_cluster_inst"):
        for node in pytest._k8_cluster_inst.cluster_nodes:
            nodes_list.append({
                "host_name": node.host_name,
                "ip_address": node.ip_address,
                "node_name": node.node_name,
                "node_type": node.node_type,
                "gpu_series": node.gpu_series,
                "device_id": node.device_id,
                "num_gpus": node.num_gpus,
                "amdgpu_driver_version": node.amdgpu_driver_version,
                "host_os_name": node.host_os_name,
                "host_os_version": node.host_os_version,
                "kernel_version": node.kernel_version,
                "k8_version": node.k8_version,
                "ocp_version": node.ocp_version,
            })
    report["nodes"] = nodes_list

    # --- test_summary ---
    summary = {
        "total": 0, "passed": 0, "failed": 0, "skipped": 0,
        "xfailed": 0, "xpassed": 0, "error": 0, "rerun": 0,
    }
    tr = session.config.pluginmanager.get_plugin("terminalreporter")
    if tr:
        for key in summary:
            if key != "total":
                summary[key] = len(tr.stats.get(key, []))
        summary["total"] = sum(v for k, v in summary.items() if k != "total")
    report["test_summary"] = summary

    # --- test_results ---
    report["test_results"] = _results

    # --- run_progression ---
    report["run_progression"] = _run_progression

    # --- failure_summary ---
    infra_count = 0
    product_count = 0
    infra_tags = {}
    for t in _results:
        cat = t.get("failure_category")
        if cat == "infra":
            infra_count += 1
            tag = t.get("failure_tag", "unknown")
            infra_tags[tag] = infra_tags.get(tag, 0) + 1
        elif cat == "product":
            product_count += 1
    report["failure_summary"] = {
        "infra_failures": infra_count,
        "product_failures": product_count,
        "infra_breakdown": infra_tags,
    }

    # --- metadata ---
    gen_elapsed = _time.monotonic() - gen_start
    session_duration = None
    if _session_start:
        session_duration = round(
            (datetime.now() - _session_start).total_seconds(), 3
        )

    report["metadata"] = {
        "generated_at": datetime.now().isoformat(),
        "pytest_version": pytest.__version__,
        "report_generation_seconds": round(gen_elapsed, 3),
        "session_start": _session_start.isoformat() if _session_start else None,
        "session_duration_seconds": session_duration,
    }

    # --- Write to disk ---
    json_path = os.path.join(logdir, "test_report.json")
    os.makedirs(logdir, exist_ok=True)
    try:
        with open(json_path, "w", encoding="utf-8") as fp:
            json.dump(report, fp, indent=2, default=str)
        Logger.info(f"JSON test report written to {json_path}")
    except Exception as exc:
        Logger.error(f"Failed to write JSON test report: {exc}")
