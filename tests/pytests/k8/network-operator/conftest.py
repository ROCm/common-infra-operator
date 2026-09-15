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
Pytest configuration for network operator tests.
"""
import re
import pytest
from kubernetes import client as k8s_client
from lib.k8_util import k8_lib_init


def _is_node_ready(v1, node_name):
    """Check if a Kubernetes node has condition Ready=True."""
    try:
        node = v1.read_node(name=node_name)
        for cond in (node.status.conditions or []):
            if cond.type == "Ready":
                return cond.status == "True"
        return False
    except Exception:
        return False

# ---------------------------------------------------------------------------
# Build-image capture
# Parsed from the captured log of test_update_all_operand_images and
# test_update_operator, stored here so the HTML report hook can read it.
# ---------------------------------------------------------------------------
_BUILD_IMAGES = {}   # populated by pytest_runtest_logreport

_IMAGE_PATTERNS = [
    ("devicePluginImage",  re.compile(r"devicePluginImage\s*:\s*(\S+)")),
    ("nodeLabellerImage",  re.compile(r"nodeLabellerImage\s*:\s*(\S+)")),
    ("metricsExporter",    re.compile(r"metricsExporter\s*:\s*(\S+)")),
]

# Throttle tests log: "Target devicePluginImage (throttle): <image>"
_THROTTLE_IMAGE_PATTERNS = [
    ("devicePluginImage",  re.compile(r"Target devicePluginImage \(throttle\):\s*(\S+)")),
    ("nodeLabellerImage",  re.compile(r"Target nodeLabellerImage \(throttle\):\s*(\S+)")),
    ("metricsExporter",    re.compile(r"Target metricsExporter\.image \(throttle\):\s*(\S+)")),
]

# Matches: "Current operator APP VERSION: main-95"
_OPERATOR_CURRENT_RE = re.compile(r"Current operator APP VERSION:\s*(\S+)")
# Matches: "Latest operator build: main-103"
_OPERATOR_LATEST_RE  = re.compile(r"Latest operator build:\s*(\S+)")

import os
from pathlib import Path

# Define the base directory paths
BASE_DIR = Path(__file__).parent
NAD_DIR = BASE_DIR / "resources" / "nad"
WORKLOAD_DIR = BASE_DIR / "resources" / "workload"
IPPOOL_DIR = BASE_DIR / "resources" / "ippool"



def pytest_configure(config):
    """Register custom pytest markers."""
    config.addinivalue_line(
        "markers", "timeout: mark test to run with a timeout"
    )


def pytest_runtest_logreport(report):
    """
    After test_update_all_operand_images or test_update_operator finishes,
    parse the captured log to extract resolved image/build references.
    """
    if report.when != "call":
        return

    log_text = ""
    for title, content in getattr(report, "sections", []):
        if "log" in title.lower():
            log_text += content

    if "test_update_all_operand_images" in report.nodeid:
        for label, pattern in _IMAGE_PATTERNS:
            m = pattern.search(log_text)
            if m:
                _BUILD_IMAGES[label] = m.group(1).strip()

    if "test_update_operator" in report.nodeid and "throttle" not in report.nodeid:
        m = _OPERATOR_CURRENT_RE.search(log_text)
        if m:
            _BUILD_IMAGES["operatorCurrent"] = m.group(1).strip()
        m = _OPERATOR_LATEST_RE.search(log_text)
        if m:
            _BUILD_IMAGES["operatorLatest"] = m.group(1).strip()

    if "test_update_cni_plugin_image" in report.nodeid and "throttle" not in report.nodeid:
        m = re.search(r"Target cniPlugins\.image:\s*(\S+)", log_text)
        if m:
            _BUILD_IMAGES["cniPlugins"] = m.group(1).strip()

    # Throttle operand tests — individual tests log their target image
    if "test_update_throttle_device_plugin_image" in report.nodeid:
        for label, pattern in _THROTTLE_IMAGE_PATTERNS:
            if label == "devicePluginImage":
                m = pattern.search(log_text)
                if m:
                    _BUILD_IMAGES["throttleDevicePlugin"] = m.group(1).strip()

    if "test_update_throttle_node_labeller_image" in report.nodeid:
        for label, pattern in _THROTTLE_IMAGE_PATTERNS:
            if label == "nodeLabellerImage":
                m = pattern.search(log_text)
                if m:
                    _BUILD_IMAGES["throttleNodeLabeller"] = m.group(1).strip()

    if "test_update_throttle_metrics_exporter_image" in report.nodeid:
        for label, pattern in _THROTTLE_IMAGE_PATTERNS:
            if label == "metricsExporter":
                m = pattern.search(log_text)
                if m:
                    _BUILD_IMAGES["throttleMetricsExporter"] = m.group(1).strip()

    if "test_update_throttle_operator" in report.nodeid:
        m = _OPERATOR_CURRENT_RE.search(log_text)
        if m:
            _BUILD_IMAGES["throttleOperatorCurrent"] = m.group(1).strip()
        m = _OPERATOR_LATEST_RE.search(log_text)
        if m:
            _BUILD_IMAGES["throttleOperatorLatest"] = m.group(1).strip()

    if "test_update_throttle_cni_plugin_image" in report.nodeid:
        m = re.search(r"Target cniPlugins\.image \(throttle\):\s*(\S+)", log_text)
        if m:
            _BUILD_IMAGES["throttleCniPlugins"] = m.group(1).strip()


def _builds_table_html():
    """Return a self-contained HTML snippet for the Builds under test table."""
    _LABELS = [
        ("devicePluginImage", "Device Plugin"),
        ("nodeLabellerImage", "Node Labeller"),
        ("metricsExporter",   "Metrics Exporter"),
        ("cniPlugins",        "CNI Plugins"),
    ]
    rows = []
    for key, label in _LABELS:
        image = _BUILD_IMAGES.get(key, "&mdash;")
        if ":" in image:
            repo, tag = image.rsplit(":", 1)
        else:
            repo, tag = image, "&mdash;"
        rows.append(
            "<tr>"
            "<td style='padding:4px 8px;border:1px solid #ccc'>{label}</td>"
            "<td style='padding:4px 8px;border:1px solid #ccc'>{repo}</td>"
            "<td style='padding:4px 8px;border:1px solid #ccc'><strong>{tag}</strong></td>"
            "</tr>".format(label=label, repo=repo, tag=tag)
        )

    # Operator row — show current → latest if both present, else whichever exists
    op_current = _BUILD_IMAGES.get("operatorCurrent", "")
    op_latest  = _BUILD_IMAGES.get("operatorLatest",  "")
    if op_current or op_latest:
        if op_current and op_latest and op_current != op_latest:
            op_tag = "{} &rarr; <strong>{}</strong>".format(op_current, op_latest)
        else:
            op_tag = "<strong>{}</strong>".format(op_latest or op_current)
        rows.append(
            "<tr>"
            "<td style='padding:4px 8px;border:1px solid #ccc'>Network Operator</td>"
            "<td style='padding:4px 8px;border:1px solid #ccc'>amd-network-operator (helm)</td>"
            "<td style='padding:4px 8px;border:1px solid #ccc'>{tag}</td>"
            "</tr>".format(tag=op_tag)
        )

    # Throttle builds section
    _THROTTLE_LABELS = [
        ("throttleDevicePlugin",    "Device Plugin (throttle)"),
        ("throttleNodeLabeller",    "Node Labeller (throttle)"),
        ("throttleMetricsExporter", "Metrics Exporter (throttle)"),
        ("throttleCniPlugins",      "CNI Plugins (throttle)"),
    ]
    has_throttle = any(_BUILD_IMAGES.get(k) for k, _ in _THROTTLE_LABELS) or \
                   _BUILD_IMAGES.get("throttleOperatorCurrent") or \
                   _BUILD_IMAGES.get("throttleOperatorLatest")

    if has_throttle:
        for key, label in _THROTTLE_LABELS:
            image = _BUILD_IMAGES.get(key, "&mdash;")
            if ":" in image:
                repo, tag = image.rsplit(":", 1)
            else:
                repo, tag = image, "&mdash;"
            rows.append(
                "<tr>"
                "<td style='padding:4px 8px;border:1px solid #ccc'>{label}</td>"
                "<td style='padding:4px 8px;border:1px solid #ccc'>{repo}</td>"
                "<td style='padding:4px 8px;border:1px solid #ccc'><strong>{tag}</strong></td>"
                "</tr>".format(label=label, repo=repo, tag=tag)
            )

        t_current = _BUILD_IMAGES.get("throttleOperatorCurrent", "")
        t_latest  = _BUILD_IMAGES.get("throttleOperatorLatest",  "")
        if t_current or t_latest:
            if t_current and t_latest and t_current != t_latest:
                t_tag = "{} &rarr; <strong>{}</strong>".format(t_current, t_latest)
            else:
                t_tag = "<strong>{}</strong>".format(t_latest or t_current)
            rows.append(
                "<tr>"
                "<td style='padding:4px 8px;border:1px solid #ccc'>Network Operator (throttle)</td>"
                "<td style='padding:4px 8px;border:1px solid #ccc'>amd-network-operator (helm)</td>"
                "<td style='padding:4px 8px;border:1px solid #ccc'>{tag}</td>"
                "</tr>".format(tag=t_tag)
            )

    return (
        "<div id='builds-under-test' style='margin:1em 0 1.5em 0'>"
        "<h2 style='font-size:1.1em;margin-bottom:0.4em'>Builds under test</h2>"
        "<table style='border-collapse:collapse;width:100%;font-size:0.9em'>"
        "<thead><tr>"
        "<th style='padding:4px 8px;border:1px solid #ccc;background:#f5f5f5'>Component</th>"
        "<th style='padding:4px 8px;border:1px solid #ccc;background:#f5f5f5'>Image</th>"
        "<th style='padding:4px 8px;border:1px solid #ccc;background:#f5f5f5'>Tag</th>"
        "</tr></thead>"
        "<tbody>{rows}</tbody>"
        "</table></div>"
    ).format(rows="".join(rows))


def pytest_sessionfinish(session, exitstatus):
    """
    Post-process the HTML report to inject the Builds table between the
    Environment section and the Summary section.
    Compatible with all pytest-html versions.
    """
    import os

    if not _BUILD_IMAGES:
        return

    report_path = getattr(session.config.option, "htmlpath", None)
    if not report_path or not os.path.exists(report_path):
        return

    with open(report_path, "r", encoding="utf-8") as fh:
        content = fh.read()

    table_snippet = _builds_table_html()

    # Try to insert just before the summary div; fall back to before </body>
    for marker in ('<div id="summary">', '<div class="summary">', "</body>"):
        if marker in content:
            content = content.replace(marker, table_snippet + marker, 1)
            break

    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(content)


# ---------------------------------------------------------------------------
# Node-readiness gate — auto-skip PF/VF/operand tests when nodes are not Ready
# ---------------------------------------------------------------------------
_readiness_cache = {}  # {"pf": True/False, "vf": True/False}


def _check_nodes_ready(kind):
    """Check if PF or VF nodes are Ready. Cached per session.

    Checks pods in both 'default' (workload pods) and 'kube-amd-network'
    (operator pods) namespaces to cover all test types (metrics, nicctl,
    node-labeller, etc.).
    """
    if kind in _readiness_cache:
        return _readiness_cache[kind]

    try:
        k8_lib_init(os.path.join(Path.home(), ".kube", "config"))
        v1 = k8s_client.CoreV1Api()
        all_pods = []
        for ns in ("default", "kube-amd-network"):
            try:
                all_pods.extend(v1.list_namespaced_pod(ns).items)
            except Exception:
                pass
    except Exception:
        _readiness_cache[kind] = False
        return False

    # VF pods start with "vf-"; PF pods do not
    if kind == "vf":
        target_pods = [p for p in all_pods if p.metadata.name.startswith("vf-")]
    else:
        target_pods = [p for p in all_pods if not p.metadata.name.startswith("vf-")]

    # Collect unique node names from target pods
    node_names = set()
    for pod in target_pods:
        node_name = getattr(pod.spec, "node_name", None) or getattr(pod.spec, "nodeName", None)
        if node_name:
            node_names.add(node_name)

    if not node_names:
        # No nodes found — let the test's own skip logic handle it
        _readiness_cache[kind] = True
        return True

    for node_name in node_names:
        if not _is_node_ready(v1, node_name):
            _readiness_cache[kind] = False
            return False

    _readiness_cache[kind] = True
    return True


@pytest.fixture(autouse=True)
def skip_if_nodes_not_ready(request):
    """Auto-skip PF/VF/operand tests when target workload nodes are not Ready."""
    # Strip parametrize suffix e.g. test_foo_vf[device-plugin] -> test_foo_vf
    base_name = request.node.name.split("[")[0]
    module_name = request.node.module.__name__

    if base_name.endswith("_pf"):
        if not _check_nodes_ready("pf"):
            pytest.skip("PF workload node(s) not Ready")
    elif base_name.endswith("_vf"):
        if not _check_nodes_ready("vf"):
            pytest.skip("VF workload node(s) not Ready")
    elif module_name == "test_update_operands":
        if not _check_nodes_ready("pf") or not _check_nodes_ready("vf"):
            pytest.skip("Operand update requires both PF and VF workload node(s) Ready")
