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

"""NIC-specific utilities for AMD network-operator tests.

Extracted from k8/network-operator/util.py, k8/device-plugin/util.py,
k8/metrics-exporter/util.py, and k8/node-labeller/util.py.

All SSH/sshpass/scp calls have been replaced with:
  - kubernetes Python client for resource operations
  - lib.k8_util.exec_command_in_pod for pod exec
  - lib.k8_util.run_command_on_node for host-level commands via debug pod
"""

import json
import logging
import os
import re
import time
import yaml
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from kubernetes import client as k8s_client
from kubernetes.client.exceptions import ApiException
from kubernetes.stream import stream

LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Section 10 — Constants
# ---------------------------------------------------------------------------

NETOP_NAMESPACE = "kube-amd-network"

PROM_LINE_RE = re.compile(
    r"^\s*([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(\{.*\})?\s+(-?\d+(\.\d+)?([eE][-+]?\d+)?)\s*$"
)
PROM_LABEL_RE = re.compile(
    r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"'
)

COMPOSITE_IB_CMD = (
    "timeout 60 ib_write_bw -d ionic_0 -i 1 -n 1000 -F -a -x 1 -q 10 -b "
    "& sleep 3 && ib_write_bw -d ionic_0 -i 1 -n 1000 -F -a -x 1 -q 10 -b localhost "
    "; pkill -9 ib_write_bw"
)

MAX_WORKERS = int(os.environ.get("TEST_MAX_WORKERS", "6"))
NETWORK_STATUS_ANNOTATION = "k8s.v1.cni.cncf.io/network-status"

IB_SERVER_TIMEOUT = 60
IB_CLIENT_TIMEOUT = 60
SERVER_STARTUP_DELAY = 3

NFD_NIC_LABEL = "feature.node.kubernetes.io/amd-nic"
NFD_VNIC_LABEL = "feature.node.kubernetes.io/amd-vnic"
NIC_RESOURCE = "amd.com/nic"

NL_LABEL_PREFIX = "amd.com/nic"
NL_LABEL_PREFIXES = ["amd.com/nic", "beta.amd.com"]

PF_LABEL_PATTERNS = [
    re.compile(r"^amd\.com/nic\.(\d+\.)?count$"),
    re.compile(r"^amd\.com/nic\.(\d+\.)?product-name$"),
    re.compile(r"^amd\.com/nic\.(\d+\.)?firmware-version$"),
    re.compile(r"^amd\.com/nic\.(\d+\.)?port-count$"),
    re.compile(r"^amd\.com/nic\.(\d+\.)?port\d*-?speed$"),
    re.compile(r"^amd\.com/nic\.driver-version$"),
    re.compile(r"^amd\.com/nic\.driver-name$"),
]
VF_LABEL_PATTERNS = [
    re.compile(r"^amd\.com/nic\.(\d+\.)?count$"),
    re.compile(r"^amd\.com/nic\.(\d+\.)?product-name$"),
]

LOCAL_CERT_DIR = os.environ.get("NIC_CERT_DIR", "/tmp/nic-certs")

LABEL_PROPAGATION_TIMEOUT = int(os.environ.get("LABEL_PROPAGATION_TIMEOUT", "60"))

# ---------------------------------------------------------------------------
# Section 1 — NetworkConfig CRD CRUD
# ---------------------------------------------------------------------------

_crd_cache: Optional[Tuple[str, str, str]] = None


def discover_networkconfig_crd() -> Tuple[str, str, str]:
    """Discover the NetworkConfig CRD group, version, and plural.

    Returns:
        (group, version, plural) tuple, cached after first call.
    """
    global _crd_cache
    if _crd_cache is not None:
        return _crd_cache

    ext = k8s_client.ApiextensionsV1Api()
    crds = ext.list_custom_resource_definition()
    for crd in crds.items:
        if crd.spec.names.kind == "NetworkConfig":
            group = crd.spec.group
            plural = crd.spec.names.plural
            version = crd.spec.versions[0].name if crd.spec.versions else "v1alpha1"
            _crd_cache = (group, version, plural)
            LOG.info("Discovered NetworkConfig CRD: %s/%s %s", group, version, plural)
            return _crd_cache

    raise RuntimeError("NetworkConfig CRD not found in cluster")


def list_networkconfigs_custom(namespace: str) -> List[Dict[str, Any]]:
    """List all NetworkConfig custom resources in a namespace."""
    group, version, plural = discover_networkconfig_crd()
    custom = k8s_client.CustomObjectsApi()
    result = custom.list_namespaced_custom_object(group, version, namespace, plural)
    return result.get("items", [])


def get_networkconfig_custom(namespace: str, name: str) -> Dict[str, Any]:
    """Get a single NetworkConfig custom resource."""
    group, version, plural = discover_networkconfig_crd()
    custom = k8s_client.CustomObjectsApi()
    return custom.get_namespaced_custom_object(group, version, namespace, plural, name)


def replace_networkconfig_custom(namespace: str, name: str, body: Dict[str, Any]) -> None:
    """Replace (PUT) a NetworkConfig custom resource."""
    group, version, plural = discover_networkconfig_crd()
    custom = k8s_client.CustomObjectsApi()
    custom.replace_namespaced_custom_object(group, version, namespace, plural, name, body)


def patch_networkconfig_custom(namespace: str, name: str, patch_body: Dict[str, Any]) -> None:
    """Patch (MERGE) a NetworkConfig custom resource."""
    group, version, plural = discover_networkconfig_crd()
    custom = k8s_client.CustomObjectsApi()
    custom.patch_namespaced_custom_object(
        group, version, namespace, plural, name, patch_body
    )


def replace_with_retry(
    namespace: str,
    name: str,
    body: Dict[str, Any],
    max_attempts: int = 5,
    backoff: float = 0.5,
) -> None:
    """Replace a NetworkConfig with retry on 409 Conflict."""
    for attempt in range(1, max_attempts + 1):
        try:
            fresh = get_networkconfig_custom(namespace, name)
            body["metadata"]["resourceVersion"] = fresh["metadata"]["resourceVersion"]
            replace_networkconfig_custom(namespace, name, body)
            return
        except ApiException as e:
            if e.status == 409 and attempt < max_attempts:
                LOG.warning("Conflict replacing %s (attempt %d/%d), retrying...", name, attempt, max_attempts)
                time.sleep(backoff * attempt)
            else:
                raise


# ---------------------------------------------------------------------------
# Section 2 — NIC/LIF ID parsing
# ---------------------------------------------------------------------------

def extract_nic_ids(output: str) -> List[str]:
    """Extract NIC IDs from ``nicctl show card`` output (JSON or text table)."""
    nic_ids: List[str] = []

    # Try JSON first
    data = _try_parse_json_or_pydict(output)
    if data is not None:
        if isinstance(data, dict):
            for key in ("nic", "cards", "nics", "devices"):
                if key in data and isinstance(data[key], list):
                    for item in data[key]:
                        if isinstance(item, dict) and "id" in item:
                            nic_ids.append(str(item["id"]))
            if "id" in data:
                nic_ids.append(str(data["id"]))
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "id" in item:
                    nic_ids.append(str(item["id"]))
        if nic_ids:
            return nic_ids

    # Fallback: text table — match UUID pattern
    uuid_re = re.compile(
        r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    )
    for line in output.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("-") or "Id" in line and "PCIe" in line:
            continue
        m = uuid_re.search(line)
        if m:
            nic_ids.append(m.group(1))
    return nic_ids


def extract_lif_ids(output: str) -> List[str]:
    """Extract LIF IDs from ``nicctl show lif`` output (JSON or text table)."""
    lif_ids: List[str] = []

    data = _try_parse_json_or_pydict(output)
    if data is not None:
        if isinstance(data, dict):
            for key in ("lif", "lifs"):
                if key in data and isinstance(data[key], list):
                    for item in data[key]:
                        if isinstance(item, dict) and "id" in item:
                            lif_ids.append(str(item["id"]))
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "id" in item:
                    lif_ids.append(str(item["id"]))
        if lif_ids:
            return lif_ids

    # Fallback: text table
    uuid_re = re.compile(
        r"\s*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    )
    for line in output.strip().splitlines():
        m = re.match(uuid_re, line)
        if m:
            lid = m.group(1)
            if lid not in lif_ids:
                lif_ids.append(lid)
    return lif_ids


def parse_card_ids(output: str) -> List[str]:
    """Parse card IDs from ``nicctl show card`` text output (UUID extraction)."""
    card_ids: List[str] = []
    uuid_re = re.compile(
        r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    )
    for line in output.strip().splitlines():
        m = uuid_re.search(line)
        if m:
            card_ids.append(m.group(1))
    return card_ids


def parse_lif_ids(output: str) -> List[str]:
    """Parse LIF IDs from ``nicctl show lif`` text output (UUID extraction)."""
    lif_ids: List[str] = []
    uuid_re = re.compile(
        r"\s*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    )
    for line in output.strip().splitlines():
        m = re.match(uuid_re, line)
        if m:
            lid = m.group(1)
            if lid not in lif_ids:
                lif_ids.append(lid)
    return lif_ids


def _try_parse_json_or_pydict(text: str) -> Any:
    """Try to parse text as JSON or Python dict literal. Returns None on failure."""
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        import ast
        return ast.literal_eval(text)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Section 3 — NIC node discovery
# ---------------------------------------------------------------------------

def get_amd_nic_nodes() -> List[k8s_client.V1Node]:
    """Return nodes with the NFD AMD NIC (PF/bare-metal) label."""
    v1 = k8s_client.CoreV1Api()
    nodes = v1.list_node().items
    return [n for n in nodes if (n.metadata.labels or {}).get(NFD_NIC_LABEL) == "true"]


def get_amd_vnic_nodes() -> List[k8s_client.V1Node]:
    """Return nodes with the NFD AMD VNIC (VF/VM) label."""
    v1 = k8s_client.CoreV1Api()
    nodes = v1.list_node().items
    return [n for n in nodes if (n.metadata.labels or {}).get(NFD_VNIC_LABEL) == "true"]


def get_node_allocatable_nic(node_name: str) -> Optional[str]:
    """Read ``node.status.allocatable['amd.com/nic']``."""
    v1 = k8s_client.CoreV1Api()
    try:
        node = v1.read_node(name=node_name)
        if node and node.status and node.status.allocatable:
            return node.status.allocatable.get(NIC_RESOURCE)
    except ApiException:
        pass
    return None


def get_node_capacity_nic(node_name: str) -> Optional[str]:
    """Read ``node.status.capacity['amd.com/nic']``."""
    v1 = k8s_client.CoreV1Api()
    try:
        node = v1.read_node(name=node_name)
        if node and node.status and node.status.capacity:
            return node.status.capacity.get(NIC_RESOURCE)
    except ApiException:
        pass
    return None


def wait_for_allocatable_change(
    node_name: str,
    expected: str,
    timeout: int = 120,
    interval: float = 3.0,
) -> bool:
    """Poll until ``amd.com/nic`` in allocatable matches *expected*."""
    start = time.time()
    while time.time() - start < timeout:
        alloc = get_node_allocatable_nic(node_name)
        LOG.info("Node %s allocatable amd.com/nic: %s (expected: %s)", node_name, alloc, expected)
        if str(alloc) == str(expected):
            return True
        time.sleep(interval)
    LOG.error("Node %s allocatable did not reach %s after %ds", node_name, expected, timeout)
    return False


def wait_for_allocatable_removed(
    node_name: str,
    timeout: int = 120,
    interval: float = 3.0,
) -> bool:
    """Poll until ``amd.com/nic`` is removed or zero in allocatable."""
    start = time.time()
    while time.time() - start < timeout:
        alloc = get_node_allocatable_nic(node_name)
        if alloc is None or str(alloc) == "0":
            LOG.info("Node %s: amd.com/nic removed/zero (val=%s)", node_name, alloc)
            return True
        LOG.info("Node %s: amd.com/nic still present (%s), waiting...", node_name, alloc)
        time.sleep(interval)
    LOG.error("Node %s: amd.com/nic not removed after %ds", node_name, timeout)
    return False


def get_topology_info_from_logs(
    pod_name: str,
    namespace: str,
    tail_lines: int = 500,
) -> Optional[str]:
    """Check pod logs for TopologyInfo entries."""
    v1 = k8s_client.CoreV1Api()
    try:
        logs = v1.read_namespaced_pod_log(pod_name, namespace, tail_lines=tail_lines)
    except ApiException:
        return None
    topo_lines = [ln for ln in logs.splitlines() if "TopologyInfo" in ln or "NUMA" in ln]
    return "\n".join(topo_lines) if topo_lines else None


# ---------------------------------------------------------------------------
# Section 4 — nicctl execution
# ---------------------------------------------------------------------------

def exec_in_pod_sync(
    pod_name: str,
    namespace: str,
    cmd: str,
    timeout: int = 120,
) -> str:
    """Run a shell command inside a pod and return combined stdout/stderr."""
    v1 = k8s_client.CoreV1Api()
    return stream(
        v1.connect_get_namespaced_pod_exec,
        pod_name,
        namespace,
        command=["/bin/sh", "-c", cmd],
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
        _request_timeout=timeout,
    ) or ""


def exec_nicctl_command(
    pod_name: str,
    namespace: str,
    cmd: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Execute a nicctl command in a pod. Returns (output, error)."""
    try:
        output = exec_in_pod_sync(pod_name, namespace, cmd, timeout=30)
        if output:
            lines = output.split("\n")
            cleaned = [ln for ln in lines if not ln.startswith("Defaulted container")]
            output = "\n".join(cleaned).strip()
        return output, None
    except Exception as e:
        LOG.error("Command failed in pod %s: %s — %s", pod_name, cmd, e)
        return None, str(e)


def run_command_in_container(
    namespace: str,
    pod: str,
    container: str,
    cmd: str,
) -> Dict[str, Any]:
    """Run a command in a specific container via pod exec."""
    v1 = k8s_client.CoreV1Api()
    try:
        resp = stream(
            v1.connect_get_namespaced_pod_exec,
            pod,
            namespace,
            container=container,
            command=["/bin/sh", "-c", cmd],
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            _preload_content=False,
        )
        stdout_data = ""
        stderr_data = ""
        while resp.is_open():
            resp.update(timeout=1)
            if resp.peek_stdout():
                stdout_data += resp.read_stdout()
            if resp.peek_stderr():
                stderr_data += resp.read_stderr()
        returncode = 0 if not stderr_data or "error" not in stderr_data.lower() else 1
        return {"stdout": stdout_data, "stderr": stderr_data, "returncode": returncode}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "returncode": -1}


def expand_commands_with_ids(
    commands: List[str],
    card_ids: List[str],
    lif_ids: List[str],
) -> List[Dict[str, Any]]:
    """Expand nicctl command templates replacing ``{CARD_ID}`` / ``{LIF_ID}``."""
    expanded: List[Dict[str, Any]] = []
    for cmd in commands:
        if "{CARD_ID}" in cmd:
            for cid in card_ids:
                expanded.append({
                    "command": cmd.replace("{CARD_ID}", cid),
                    "original": cmd,
                    "card_id": cid,
                    "lif_id": None,
                })
        elif "{LIF_ID}" in cmd:
            for lid in lif_ids:
                expanded.append({
                    "command": cmd.replace("{LIF_ID}", lid),
                    "original": cmd,
                    "card_id": None,
                    "lif_id": lid,
                })
        else:
            expanded.append({
                "command": cmd,
                "original": cmd,
                "card_id": None,
                "lif_id": None,
            })
    return expanded


def clean_command_output(output: str, cmd: str = "") -> str:
    """Remove ANSI escapes, shell prompts, and echoed command from output."""
    ansi_re = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
    text = ansi_re.sub("", output)

    lines = text.split("\n")
    cleaned: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("$") or stripped.endswith("$"):
            continue
        if cmd and stripped == cmd.strip():
            continue
        if "[sudo]" in stripped or "password" in stripped.lower():
            continue
        if stripped.startswith("Defaulted container"):
            continue
        cleaned.append(line)

    return "\n".join(cleaned).strip()


def clean_output(text: str) -> str:
    """Remove ANSI escapes and normalise whitespace."""
    ansi_re = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
    text = ansi_re.sub("", text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines)


def compare_json_with_tolerance(
    host_output: str,
    container_output: str,
    tolerance_percent: float = 10.0,
    absolute_tolerance: int = 100000,
    ignore_keys: Optional[set] = None,
) -> Tuple[bool, List[Dict[str, Any]]]:
    """Compare two JSON strings with numeric tolerance. Returns (match, diffs)."""
    ignore_keys = ignore_keys or set()
    try:
        host_data = json.loads(host_output)
        container_data = json.loads(container_output)
    except (json.JSONDecodeError, TypeError):
        return host_output.strip() == container_output.strip(), []

    diffs: List[Dict[str, Any]] = []

    def _compare(h: Any, c: Any, path: str = "") -> None:
        if isinstance(h, dict) and isinstance(c, dict):
            all_keys = set(h.keys()) | set(c.keys())
            for k in all_keys:
                if k in ignore_keys:
                    continue
                child_path = f"{path}.{k}" if path else k
                if k not in h:
                    diffs.append({"path": child_path, "issue": "missing in host"})
                elif k not in c:
                    diffs.append({"path": child_path, "issue": "missing in container"})
                else:
                    _compare(h[k], c[k], child_path)
        elif isinstance(h, list) and isinstance(c, list):
            for i in range(max(len(h), len(c))):
                child_path = f"{path}[{i}]"
                if i >= len(h):
                    diffs.append({"path": child_path, "issue": "extra in container"})
                elif i >= len(c):
                    diffs.append({"path": child_path, "issue": "missing in container"})
                else:
                    _compare(h[i], c[i], child_path)
        elif isinstance(h, (int, float)) and isinstance(c, (int, float)):
            if h == c:
                return
            if abs(h) > 0:
                pct = abs(h - c) / abs(h) * 100
                if pct <= tolerance_percent or abs(h - c) <= absolute_tolerance:
                    return
            elif abs(c - h) <= absolute_tolerance:
                return
            diffs.append({"path": path, "issue": "value mismatch", "host": h, "container": c})
        elif h != c:
            diffs.append({"path": path, "issue": "value mismatch", "host": str(h), "container": str(c)})

    _compare(host_data, container_data)
    return len(diffs) == 0, diffs


def compare_outputs_smart(
    host_output: str,
    container_output: str,
    cmd: str,
    ignore_keys: Optional[set] = None,
) -> Tuple[bool, List[Dict[str, Any]]]:
    """Smart comparison: try JSON comparison first, fall back to text."""
    try:
        json.loads(host_output)
        json.loads(container_output)
        return compare_json_with_tolerance(
            host_output, container_output, ignore_keys=ignore_keys
        )
    except (json.JSONDecodeError, TypeError):
        pass
    match = host_output.strip() == container_output.strip()
    diffs = [] if match else [{"issue": "text mismatch", "host_len": len(host_output), "container_len": len(container_output)}]
    return match, diffs


def compare_nicctl_outputs(
    pod_name: str,
    container_name: str,
    pf_node: str,
    namespace: str = "default",
    skip_card_commands: bool = False,
    skip_lif_commands: bool = False,
    output_file: Optional[str] = None,
    commands: Optional[List[str]] = None,
    run_host_cmd_fn=None,
) -> Dict[str, Any]:
    """Compare nicctl outputs between PF-node (via *run_host_cmd_fn*) and container.

    Args:
        run_host_cmd_fn: callable(v1, node_name, cmd, namespace) -> dict with
            stdout/stderr/returncode.  Callers should pass
            ``lib.k8_util.run_command_on_node`` or equivalent.
            When None, host-side commands are skipped.
    """
    v1 = k8s_client.CoreV1Api()
    if not commands:
        raise ValueError("commands must be provided")

    results: Dict[str, Any] = {
        "timestamp": datetime.now().isoformat(),
        "pf_node": pf_node,
        "pod": pod_name,
        "namespace": namespace,
        "container": container_name,
        "card_ids": [],
        "lif_ids": [],
        "total_commands": 0,
        "discrepancies": [],
    }

    card_ids: List[str] = []
    lif_ids: List[str] = []

    if not skip_card_commands:
        LOG.info("Fetching card IDs...")
        container_card_result = run_command_in_container(namespace, pod_name, container_name, "nicctl show card")
        if container_card_result["returncode"] == 0:
            card_ids = parse_card_ids(container_card_result["stdout"])
            LOG.info("Found %d cards in container", len(card_ids))
        results["card_ids"] = card_ids

    if not skip_lif_commands:
        LOG.info("Fetching LIF IDs...")
        container_lif_result = run_command_in_container(namespace, pod_name, container_name, "nicctl show lif")
        if container_lif_result["returncode"] == 0:
            lif_ids = parse_lif_ids(container_lif_result["stdout"])
            LOG.info("Found %d LIFs in container", len(lif_ids))
        results["lif_ids"] = lif_ids

    expanded_commands = expand_commands_with_ids(commands, card_ids, lif_ids)
    results["total_commands"] = len(expanded_commands)
    LOG.info("Total commands to execute: %d", len(expanded_commands))

    for idx, cmd_info in enumerate(expanded_commands, 1):
        cmd = cmd_info["command"]
        LOG.info("[%d/%d] Testing: %s", idx, len(expanded_commands), cmd)

        # Host
        if run_host_cmd_fn is not None:
            host_result = run_host_cmd_fn(v1, pf_node, cmd, namespace)
            host_stdout_cleaned = clean_command_output(host_result.get("stdout", ""), cmd)
        else:
            host_result = {"stdout": "", "stderr": "host cmd fn not provided", "returncode": -1}
            host_stdout_cleaned = ""

        # Container
        container_result = run_command_in_container(namespace, pod_name, container_name, cmd)
        container_stdout_cleaned = clean_command_output(container_result["stdout"], cmd)

        cmd_ignore_keys = None
        if "show port" in cmd:
            cmd_ignore_keys = {"transceiver_temperature"}
        if "version host-software" in cmd:
            cmd_ignore_keys = {"monitor", "logger"}

        outputs_match, differences = compare_outputs_smart(
            host_stdout_cleaned, container_stdout_cleaned, cmd, ignore_keys=cmd_ignore_keys
        )

        check_rc = "version host-software" not in cmd
        if not outputs_match or (check_rc and host_result["returncode"] != container_result["returncode"]):
            results["discrepancies"].append({
                "command": cmd,
                "original_command": cmd_info["original"],
                "card_id": cmd_info["card_id"],
                "lif_id": cmd_info["lif_id"],
                "host": {
                    "stdout": host_stdout_cleaned,
                    "stderr": host_result.get("stderr", ""),
                    "returncode": host_result["returncode"],
                },
                "container": {
                    "stdout": container_stdout_cleaned,
                    "stderr": container_result["stderr"],
                    "returncode": container_result["returncode"],
                },
                "differences": differences,
            })
            LOG.warning("[FAIL] Discrepancy for: %s", cmd)
        else:
            LOG.info("[OK] Outputs match")

    if output_file:
        with open(output_file, "w") as f:
            json.dump(results, f, indent=2)

    return results


# ---------------------------------------------------------------------------
# Section 5 — NIC metrics
# ---------------------------------------------------------------------------

def pull_metrics(
    pod_name: str,
    ns: str,
    port: int,
    node_ip: str,
) -> Optional[str]:
    """Curl /metrics from inside a pod."""
    curl_cmd = f"curl -sS --connect-timeout 3 http://{node_ip}:{port}/metrics || true"
    try:
        return exec_in_pod_sync(pod_name, ns, curl_cmd, timeout=5)
    except Exception as e:
        LOG.error("pull_metrics exec failed for %s: %s", pod_name, e)
        return None


def node_metrics_have_numeric(
    pod_name: str,
    ns: str,
    node_ip: str,
    port: int,
) -> bool:
    """Check if a metrics endpoint returns valid Prometheus numeric lines."""
    txt = pull_metrics(pod_name, ns, port, node_ip)
    if not txt:
        return False
    for ln in txt.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        if PROM_LINE_RE.match(ln):
            return True
    return False


def wait_for_metrics_ready(
    pod_name: str,
    ns: str,
    node_ip: str,
    port: int,
    timeout: int = 30,
    interval: float = 1.0,
) -> bool:
    """Poll until the metrics endpoint returns numeric data."""
    start = time.time()
    while time.time() - start < timeout:
        if node_metrics_have_numeric(pod_name, ns, node_ip, port):
            return True
        time.sleep(interval)
    return False


def metrics_text_has_prefix(metrics_text: Optional[str], prefix: str) -> bool:
    """Check if any metric name starts with *prefix*."""
    if not metrics_text:
        return False
    for ln in metrics_text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        m = PROM_LINE_RE.match(ln)
        if m and m.group(1).startswith(prefix):
            return True
    return False


def parse_prometheus_labels(label_block: str) -> Dict[str, str]:
    """Parse a Prometheus ``{key="val",...}`` block into a dict."""
    return dict(PROM_LABEL_RE.findall(label_block))


def find_metric_line(
    metrics_text: str,
    metric_names: Optional[List[str]] = None,
    metric_prefix: Optional[str] = None,
    required_labels: Optional[List[str]] = None,
    expected_label_values: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Find a metric line matching criteria."""
    for ln in metrics_text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        m = PROM_LINE_RE.match(ln)
        if not m:
            continue
        name = m.group(1)
        label_block = m.group(2) or ""

        if metric_names and name not in metric_names:
            continue
        if metric_prefix and not name.startswith(metric_prefix):
            continue

        if required_labels or expected_label_values:
            labels = parse_prometheus_labels(label_block)
            if required_labels and not all(k in labels for k in required_labels):
                continue
            if expected_label_values and not all(labels.get(k) == v for k, v in expected_label_values.items()):
                continue

        return ln
    return None


def metrics_missing_field_names(
    metrics_text: str,
    field_names: List[str],
    prefix: str,
) -> List[str]:
    """Return field names from *field_names* not found in metrics."""
    found: set = set()
    for ln in metrics_text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        m = PROM_LINE_RE.match(ln)
        if m:
            found.add(m.group(1))
    missing = []
    for field in field_names:
        normalised = re.sub(r"pri_?(\d)", r"pri_\1", f"{prefix}{field.lower()}")
        if normalised not in found:
            missing.append(field)
    return missing


def parse_all_metrics(metrics_text: str) -> Dict[str, float]:
    """Parse all metric lines into ``{name: value}``."""
    result: Dict[str, float] = {}
    for ln in metrics_text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        m = PROM_LINE_RE.match(ln)
        if m:
            try:
                result[m.group(1)] = float(m.group(3))
            except (ValueError, TypeError):
                pass
    return result


def diff_metrics(
    before: Dict[str, float],
    after: Dict[str, float],
) -> Dict[str, Dict[str, float]]:
    """Return metrics that changed between *before* and *after* snapshots."""
    changed: Dict[str, Dict[str, float]] = {}
    all_keys = set(before.keys()) | set(after.keys())
    for k in all_keys:
        b = before.get(k, 0.0)
        a = after.get(k, 0.0)
        if b != a:
            changed[k] = {"before": b, "after": a, "delta": a - b}
    return changed


def extract_metrics_by_prefix(metrics_text: str, prefix: str) -> List[str]:
    """Return raw metric lines whose name starts with *prefix*."""
    lines: List[str] = []
    for ln in metrics_text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        m = PROM_LINE_RE.match(ln)
        if m and m.group(1).startswith(prefix):
            lines.append(ln)
    return lines


def validate_metric_labels_by_type(
    metrics_text: str,
    prefix: str,
) -> Dict[str, List[str]]:
    """Validate metric labels. Returns ``{metric_name: [missing_labels]}``."""
    required_labels = ["device", "instance"]
    issues: Dict[str, List[str]] = {}
    for ln in metrics_text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        m = PROM_LINE_RE.match(ln)
        if not m or not m.group(1).startswith(prefix):
            continue
        label_block = m.group(2) or ""
        labels = parse_prometheus_labels(label_block)
        missing = [lbl for lbl in required_labels if lbl not in labels]
        if missing:
            issues.setdefault(m.group(1), []).extend(missing)
    return issues


def check_metrics_endpoint(
    pod_name: str,
    ns: str,
    node_ip: str,
    port: int,
    timeout: int = 10,
) -> Optional[str]:
    """Curl a metrics endpoint via pod exec (replaces SSH curl)."""
    curl_cmd = f"curl -sS --connect-timeout {timeout} http://{node_ip}:{port}/metrics || true"
    try:
        out = exec_in_pod_sync(pod_name, ns, curl_cmd, timeout=timeout + 15)
        return out if out and out.strip() else None
    except Exception as e:
        LOG.warning("check_metrics_endpoint failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Section 6 — RDMA/IB operations
# ---------------------------------------------------------------------------

def run_ib_traffic(pod_name: str, ns: str) -> Dict[str, str]:
    """Run InfiniBand write bandwidth test inside a pod."""
    LOG.info("Running IB in pod %s/%s", ns, pod_name)
    try:
        out = exec_in_pod_sync(pod_name, ns, COMPOSITE_IB_CMD, timeout=60)
        return {pod_name: out or ""}
    except Exception as e:
        LOG.error("IB exec failed in %s/%s: %s", ns, pod_name, e)
        return {pod_name: f"ERROR: {e}"}


def get_rdma_interfaces(pod: k8s_client.V1Pod) -> List[Dict[str, str]]:
    """Parse RDMA interfaces from the ``k8s.v1.cni.cncf.io/network-status`` annotation."""
    annotations = pod.metadata.annotations or {}
    raw = annotations.get(NETWORK_STATUS_ANNOTATION)
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    interfaces: List[Dict[str, str]] = []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("interface"):
            interfaces.append({
                "name": entry.get("name", ""),
                "interface": entry["interface"],
                "ips": ", ".join(entry.get("ips", [])),
                "mac": entry.get("mac", ""),
            })
    return interfaces


def run_ib_server(
    pod_name: str,
    ns: str,
    rdma_device: str,
    timeout: int = IB_SERVER_TIMEOUT,
) -> Optional[str]:
    """Start ``ib_write_bw`` server in the background inside a pod."""
    cmd = f"timeout {timeout} ib_write_bw -d {rdma_device} -i 1 -n 1000 -F -a -x 1 -q 10 -b &"
    try:
        return exec_in_pod_sync(pod_name, ns, cmd, timeout=timeout + 10)
    except Exception as e:
        LOG.error("IB server start failed in %s: %s", pod_name, e)
        return None


def run_ib_client(
    pod_name: str,
    ns: str,
    rdma_device: str,
    server_ip: str,
    timeout: int = IB_CLIENT_TIMEOUT,
) -> Optional[str]:
    """Run ``ib_write_bw`` client inside a pod."""
    cmd = f"ib_write_bw -d {rdma_device} -i 1 -n 1000 -F -a -x 1 -q 10 -b {server_ip}"
    try:
        return exec_in_pod_sync(pod_name, ns, cmd, timeout=timeout)
    except Exception as e:
        LOG.error("IB client failed in %s: %s", pod_name, e)
        return None


def cleanup_ib_processes(
    pod_name: str,
    ns: str,
) -> None:
    """Kill leftover ib_write_bw processes in a pod."""
    try:
        exec_in_pod_sync(pod_name, ns, "pkill -9 ib_write_bw || true", timeout=10)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Section 7 — NIC configmap management
# ---------------------------------------------------------------------------

def template_configmap_path(base_dir: str, filename: str = "configmap.yaml") -> str:
    """Return the path to a configmap template file."""
    return os.path.join(base_dir, filename)


def load_configmap_template(path: str) -> Dict[str, Any]:
    """Load a configmap YAML template from disk."""
    with open(path, "r") as f:
        return yaml.safe_load(f)


def target_configmap_names_from_networkconfigs(nc_items: List[Dict[str, Any]]) -> List[str]:
    """Extract configmap names referenced by NetworkConfig items."""
    names: List[str] = []
    for item in nc_items:
        me = item.get("spec", {}).get("metricsExporter", {}) or {}
        cfg = me.get("config", {}) or {}
        cm_name = cfg.get("name")
        if cm_name and cm_name not in names:
            names.append(cm_name)
    return names


def build_configmap_for_target(
    template_doc: Dict[str, Any],
    target_name: str,
    namespace: str,
) -> Dict[str, Any]:
    """Build a configmap manifest from a template, overriding name/namespace."""
    import copy
    doc = copy.deepcopy(template_doc)
    doc.setdefault("metadata", {})["name"] = target_name
    doc["metadata"]["namespace"] = namespace
    return doc


def load_nic_config_from_configmap(path: str) -> Dict[str, Any]:
    """Load NICConfig from configmap.yaml's ``data['config.json']``."""
    doc = load_configmap_template(path)
    data = doc.get("data", {})
    raw = data.get("config.json", "{}")
    return json.loads(raw)


def load_nic_labels_from_configmap(path: str) -> List[str]:
    """Load NICConfig.Labels from configmap template."""
    cfg = load_nic_config_from_configmap(path)
    nic_cfg = cfg.get("NICConfig", cfg)
    return nic_cfg.get("Labels", [])


def load_nic_custom_labels_from_configmap(path: str) -> Dict[str, str]:
    """Load NICConfig.CustomLabels from configmap template."""
    cfg = load_nic_config_from_configmap(path)
    nic_cfg = cfg.get("NICConfig", cfg)
    return nic_cfg.get("CustomLabels", {})


def load_nic_fields_from_configmap(template_path: str) -> List[str]:
    """Load NICConfig.Fields from configmap template."""
    cfg = load_nic_config_from_configmap(template_path)
    nic_cfg = cfg.get("NICConfig", cfg)
    return nic_cfg.get("Fields", [])


def assert_required_config_json(
    config_map_doc: Dict[str, Any],
    expected_server_port: int = 5001,
    expected_metrics_prefix: str = "amd_",
) -> None:
    """Assert that a configmap doc contains expected config.json fields."""
    data = config_map_doc.get("data", {})
    raw = data.get("config.json", "{}")
    cfg = json.loads(raw)
    nic_cfg = cfg.get("NICConfig", cfg)

    port = nic_cfg.get("ServerPort", nic_cfg.get("server_port"))
    if port is not None:
        assert int(port) == expected_server_port, (
            f"Expected ServerPort={expected_server_port}, got {port}"
        )

    prefix = nic_cfg.get("MetricsPrefix", nic_cfg.get("metrics_prefix"))
    if prefix is not None:
        assert prefix == expected_metrics_prefix, (
            f"Expected MetricsPrefix={expected_metrics_prefix!r}, got {prefix!r}"
        )


def update_configmap_config_json(
    config_map_doc: Dict[str, Any],
    server_port: Optional[int] = None,
    metrics_prefix: Optional[str] = None,
    nic_custom_labels: Optional[Dict[str, str]] = None,
    nic_fields: Optional[List[str]] = None,
    nic_labels: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Mutate ``data['config.json']`` inside a configmap doc."""
    import copy
    doc = copy.deepcopy(config_map_doc)
    data = doc.setdefault("data", {})
    raw = data.get("config.json", "{}")
    cfg = json.loads(raw)
    nic_cfg = cfg.setdefault("NICConfig", cfg)

    if server_port is not None:
        nic_cfg["ServerPort"] = server_port
    if metrics_prefix is not None:
        nic_cfg["MetricsPrefix"] = metrics_prefix
    if nic_custom_labels is not None:
        nic_cfg["CustomLabels"] = nic_custom_labels
    if nic_fields is not None:
        nic_cfg["Fields"] = nic_fields
    if nic_labels is not None:
        nic_cfg["Labels"] = nic_labels

    data["config.json"] = json.dumps(cfg, indent=2)
    return doc


def configmap_metrics_port(config_map_doc: Dict[str, Any]) -> int:
    """Read the ServerPort from a configmap doc."""
    data = config_map_doc.get("data", {})
    raw = data.get("config.json", "{}")
    cfg = json.loads(raw)
    nic_cfg = cfg.get("NICConfig", cfg)
    return int(nic_cfg.get("ServerPort", nic_cfg.get("server_port", 5001)))


def apply_configmap(
    config_doc: Dict[str, Any],
    namespace: str,
) -> None:
    """Create or replace a ConfigMap via the K8s Python client (replaces SSH apply)."""
    v1 = k8s_client.CoreV1Api()
    name = config_doc["metadata"]["name"]
    cm = k8s_client.V1ConfigMap(
        api_version="v1",
        kind="ConfigMap",
        metadata=k8s_client.V1ObjectMeta(name=name, namespace=namespace),
        data=config_doc.get("data", {}),
    )
    try:
        v1.read_namespaced_config_map(name=name, namespace=namespace)
        v1.replace_namespaced_config_map(name=name, namespace=namespace, body=cm)
        LOG.info("ConfigMap %s/%s replaced", namespace, name)
    except ApiException as e:
        if e.status == 404:
            v1.create_namespaced_config_map(namespace=namespace, body=cm)
            LOG.info("ConfigMap %s/%s created", namespace, name)
        else:
            raise


def delete_configmap_quietly(
    name: str,
    namespace: str,
) -> None:
    """Delete a ConfigMap, ignoring 404."""
    v1 = k8s_client.CoreV1Api()
    try:
        v1.delete_namespaced_config_map(name=name, namespace=namespace)
    except ApiException as e:
        if e.status != 404:
            LOG.error("Failed deleting configmap %s/%s: %s", namespace, name, e)


# ---------------------------------------------------------------------------
# Section 8 — Node labeller labels
# ---------------------------------------------------------------------------

def get_nl_labels_on_node(
    node_name: str,
) -> Dict[str, str]:
    """Return only ``amd.com/nic.*`` labels on a node."""
    v1 = k8s_client.CoreV1Api()
    try:
        node = v1.read_node(name=node_name)
        labels = node.metadata.labels or {}
        return {k: v for k, v in labels.items() if k.startswith(NL_LABEL_PREFIX)}
    except ApiException:
        return {}


def verify_pf_labels_present(labels: Dict[str, str]) -> Tuple[bool, List[str]]:
    """Check that PF label patterns are all satisfied in the label set."""
    missing = [pat.pattern for pat in PF_LABEL_PATTERNS if not any(pat.match(k) for k in labels)]
    return len(missing) == 0, missing


def verify_vf_labels_present(labels: Dict[str, str]) -> Tuple[bool, List[str]]:
    """Check that VF label patterns are all satisfied in the label set."""
    missing = [pat.pattern for pat in VF_LABEL_PATTERNS if not any(pat.match(k) for k in labels)]
    return len(missing) == 0, missing


def verify_nl_labels_absent(
    node_name: str,
) -> Tuple[bool, List[str]]:
    """Verify no NIC node-labeller labels remain (ignoring ``beta.amd.com/gpu.*``)."""
    v1 = k8s_client.CoreV1Api()
    try:
        node = v1.read_node(name=node_name)
        labels = node.metadata.labels or {}
    except ApiException:
        return False, ["failed to read node"]
    remaining = [
        k for k in labels
        if k.startswith(NL_LABEL_PREFIX)
        or (k.startswith("beta.amd.com") and not k.startswith("beta.amd.com/gpu"))
    ]
    return len(remaining) == 0, remaining


def wait_for_nl_labels(
    node_name: str,
    timeout: int = LABEL_PROPAGATION_TIMEOUT,
    interval: float = 2.0,
) -> bool:
    """Poll until ``amd.com/nic.*`` labels appear on the node."""
    start = time.time()
    while time.time() - start < timeout:
        nl_labels = get_nl_labels_on_node(node_name)
        if nl_labels:
            LOG.info("Node %s has %d NL labels", node_name, len(nl_labels))
            return True
        time.sleep(interval)
    LOG.warning("Timed out waiting for NL labels on node %s after %ds", node_name, timeout)
    return False


def wait_for_nl_labels_removed(
    node_name: str,
    timeout: int = LABEL_PROPAGATION_TIMEOUT,
    interval: float = 2.0,
) -> bool:
    """Poll until ``amd.com/nic.*`` labels are removed from the node."""
    start = time.time()
    while time.time() - start < timeout:
        clean, _ = verify_nl_labels_absent(node_name)
        if clean:
            LOG.info("Node %s: all NL labels removed", node_name)
            return True
        time.sleep(interval)
    LOG.warning("Timed out waiting for NL labels removal on node %s after %ds", node_name, timeout)
    return False


# ---------------------------------------------------------------------------
# Section 9 — Large verify_* test helpers
#
# These were refactored from the originals that called pytest.skip/fail
# directly.  Now they raise exceptions so the caller decides how to handle
# them in the test layer.
# ---------------------------------------------------------------------------

class NicTestSkip(Exception):
    """Raised when a verify helper determines the test should be skipped."""


class NicTestFailure(Exception):
    """Raised when a verify helper determines the test has failed."""


def get_operator_pods(
    namespace: str = NETOP_NAMESPACE,
) -> Dict[str, List[k8s_client.V1Pod]]:
    """Get running operator pods grouped by type (PF and VF variants)."""
    v1 = k8s_client.CoreV1Api()
    pods = v1.list_namespaced_pod(namespace).items
    by_type: Dict[str, List[k8s_client.V1Pod]] = {
        "device-plugin": [],
        "metrics-exporter": [],
        "node-labeler": [],
        "vf-device-plugin": [],
        "vf-metrics-exporter": [],
        "vf-node-labeler": [],
    }
    for pod in pods:
        if pod.status.phase != "Running":
            continue
        name = pod.metadata.name
        is_vf = name.startswith("vf-")
        if "device-plugin" in name:
            by_type["vf-device-plugin" if is_vf else "device-plugin"].append(pod)
        elif "metrics-exporter" in name:
            by_type["vf-metrics-exporter" if is_vf else "metrics-exporter"].append(pod)
        elif "node-labeler" in name or "node-labeller" in name:
            by_type["vf-node-labeler" if is_vf else "node-labeler"].append(pod)
    return by_type


def get_pf_vf_workload_pods(
    namespace: str = "default",
) -> Tuple[List[k8s_client.V1Pod], List[k8s_client.V1Pod]]:
    """Return (pf_pods, vf_pods) of running workload pods."""
    v1 = k8s_client.CoreV1Api()
    pods = v1.list_namespaced_pod(namespace).items
    pf: List[k8s_client.V1Pod] = []
    vf: List[k8s_client.V1Pod] = []
    for pod in pods:
        if pod.status.phase != "Running":
            continue
        (vf if pod.metadata.name.startswith("vf-") else pf).append(pod)
    return pf, vf


def wait_for_exporter_pod_running(
    namespace: str,
    config_name: str,
    timeout: int = 60,
    interval: float = 5.0,
) -> Optional[k8s_client.V1Pod]:
    """Wait until the metrics-exporter pod for *config_name* is Running."""
    prefix = "vf-" if config_name.startswith("vf-") else ""
    key = f"{prefix}metrics-exporter"
    start = time.time()
    while time.time() - start < timeout:
        op_pods = get_operator_pods(namespace=namespace)
        for pod in op_pods.get(key, []):
            if pod.metadata.name.startswith(config_name) and pod.status.phase == "Running":
                LOG.info("Exporter pod %s is Running", pod.metadata.name)
                return pod
        time.sleep(interval)
    LOG.warning("Timed out waiting for exporter pod for config %s", config_name)
    return None


def get_node_ip(node_name: str) -> Optional[str]:
    """Get the InternalIP of a node."""
    v1 = k8s_client.CoreV1Api()
    try:
        node = v1.read_node(name=node_name)
        if node.status and node.status.addresses:
            for addr in node.status.addresses:
                if addr.type == "InternalIP":
                    return addr.address
    except ApiException:
        pass
    return None


def get_metrics_nodeport_from_networkconfig(
    namespace: str,
    name: str,
) -> Optional[int]:
    """Read ``spec.metricsExporter.nodePort`` from a NetworkConfig."""
    try:
        nc = get_networkconfig_custom(namespace, name)
    except Exception as e:
        LOG.error("Failed to get NetworkConfig %s/%s: %s", namespace, name, e)
        return None
    me = nc.get("spec", {}).get("metricsExporter", {}) or {}
    val = me.get("nodePort")
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        LOG.error("nodePort value %r is not an integer", val)
        return None


def curl_metrics_from_local(
    node_ip: str,
    port: int,
    service_name: str,
    client_crt: Optional[str] = None,
    client_key: Optional[str] = None,
    ca_crt: Optional[str] = None,
    extra_headers: Optional[dict] = None,
    source_port: Optional[int] = None,
    timeout: int = 10,
) -> Optional[str]:
    """Curl metrics via mTLS from the local machine (for prometheus endpoints)."""
    import subprocess

    client_crt = client_crt or os.path.join(LOCAL_CERT_DIR, "client.crt")
    client_key = client_key or os.path.join(LOCAL_CERT_DIR, "client.key")
    ca_crt = ca_crt or os.path.join(LOCAL_CERT_DIR, "ca.crt")

    missing = [p for p in (client_crt, client_key, ca_crt) if not os.path.isfile(p)]
    if missing:
        LOG.error("Missing cert files for mTLS: %s", missing)
        return None

    cmd = [
        "curl",
        "--cert", client_crt,
        "--key", client_key,
        "--cacert", ca_crt,
        "-sS",
        "-H", "Accept: */*",
        "--resolve", f"{service_name}:{port}:{node_ip}",
        f"https://{node_ip}:{port}/metrics",
    ]
    if source_port:
        cmd.insert(1, "--local-port")
        cmd.insert(2, str(source_port))
    if extra_headers:
        for k, v in extra_headers.items():
            cmd.extend(["-H", f"{k}: {v}"])

    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=timeout,
        )
        if proc.returncode != 0:
            LOG.warning("curl rc=%s stderr=%s", proc.returncode, proc.stderr)
        return proc.stdout or None
    except Exception as e:
        LOG.error("curl_metrics_from_local failed: %s", e)
        return None


def detect_prometheus_base_url_from_k8s(namespace: str = "monitoring") -> Optional[str]:
    """Find a K8s Service exposing port 9090 (Prometheus)."""
    v1 = k8s_client.CoreV1Api()
    try:
        svcs = v1.list_namespaced_service(namespace=namespace)
    except ApiException:
        return None
    for svc in svcs.items:
        cluster_ip = svc.spec.cluster_ip
        if not cluster_ip or cluster_ip.lower() == "none":
            continue
        if svc.spec.ports:
            for p in svc.spec.ports:
                if p.port == 9090 or (p.name and "prom" in p.name.lower()):
                    return f"http://{cluster_ip}:9090/api/v1/query"
    return None


# ---------------------------------------------------------------------------
# Device-plugin specific NIC helpers
# ---------------------------------------------------------------------------

def get_dp_configmap_names(
    namespace: str,
) -> List[str]:
    """Find ConfigMap names related to device-plugin in a namespace."""
    v1 = k8s_client.CoreV1Api()
    cms = v1.list_namespaced_config_map(namespace)
    return [
        cm.metadata.name for cm in cms.items
        if "config" in cm.metadata.name.lower() or "device-plugin" in cm.metadata.name.lower()
    ]


def get_dp_config_json(
    namespace: str,
) -> Optional[Dict]:
    """Read ``config.json`` from a device-plugin ConfigMap."""
    v1 = k8s_client.CoreV1Api()
    for cm_name in get_dp_configmap_names(namespace):
        try:
            cm = v1.read_namespaced_config_map(cm_name, namespace)
        except ApiException:
            continue
        if cm and cm.data:
            for key, value in cm.data.items():
                if key.endswith(".json") or key == "config.json":
                    try:
                        return json.loads(value)
                    except (json.JSONDecodeError, TypeError):
                        continue
            for key, value in cm.data.items():
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, dict):
                        return parsed
                except (json.JSONDecodeError, TypeError):
                    continue
    return None


# ---------------------------------------------------------------------------
# NIC workload pod helpers
# ---------------------------------------------------------------------------

def create_nic_workload_pod(
    pod_name: str,
    node_name: str,
    namespace: str,
    nic_count: int = 1,
) -> k8s_client.V1Pod:
    """Create a pod that requests ``amd.com/nic`` resources on a specific node."""
    v1 = k8s_client.CoreV1Api()
    pod_manifest = k8s_client.V1Pod(
        api_version="v1",
        kind="Pod",
        metadata=k8s_client.V1ObjectMeta(name=pod_name, namespace=namespace),
        spec=k8s_client.V1PodSpec(
            node_name=node_name,
            containers=[
                k8s_client.V1Container(
                    name="test-workload",
                    image="registry.k8s.io/pause:3.9",
                    resources=k8s_client.V1ResourceRequirements(
                        limits={NIC_RESOURCE: str(nic_count)},
                        requests={NIC_RESOURCE: str(nic_count)},
                    ),
                )
            ],
            restart_policy="Never",
        ),
    )
    pod = v1.create_namespaced_pod(namespace, pod_manifest)
    LOG.info("Created workload pod %s on node %s requesting %d %s", pod_name, node_name, nic_count, NIC_RESOURCE)
    return pod


def wait_for_workload_pod_running(
    pod_name: str,
    namespace: str,
    timeout: int = 120,
    interval: float = 3.0,
) -> bool:
    """Wait until a workload pod reaches Running phase."""
    v1 = k8s_client.CoreV1Api()
    start = time.time()
    while time.time() - start < timeout:
        try:
            pod = v1.read_namespaced_pod(pod_name, namespace)
            phase = pod.status.phase if pod.status else None
            if phase == "Running":
                return True
            if phase in ("Failed", "Succeeded"):
                LOG.error("Workload pod %s ended with phase: %s", pod_name, phase)
                return False
        except ApiException as e:
            if e.status != 404:
                raise
        time.sleep(interval)
    LOG.error("Workload pod %s not running after %ds", pod_name, timeout)
    return False


def delete_workload_pod(
    pod_name: str,
    namespace: str,
) -> bool:
    """Delete a workload test pod."""
    v1 = k8s_client.CoreV1Api()
    try:
        v1.delete_namespaced_pod(
            pod_name, namespace,
            body=k8s_client.V1DeleteOptions(grace_period_seconds=0),
        )
        LOG.info("Deleted workload pod %s", pod_name)
        return True
    except ApiException as e:
        if e.status == 404:
            return True
        LOG.error("Failed to delete workload pod %s: %s", pod_name, e)
        return False


# ---------------------------------------------------------------------------
# ME-specific NIC helpers
# ---------------------------------------------------------------------------

def get_me_configmap_names(
    namespace: str,
) -> List[str]:
    """Find ConfigMap names related to metrics-exporter in a namespace."""
    v1 = k8s_client.CoreV1Api()
    cms = v1.list_namespaced_config_map(namespace)
    return [
        cm.metadata.name for cm in cms.items
        if "config" in cm.metadata.name.lower() or "exporter" in cm.metadata.name.lower()
    ]


def get_metrics_nodeport(
    namespace: str,
) -> Optional[int]:
    """Get metrics exporter nodePort from a Service in the namespace."""
    v1 = k8s_client.CoreV1Api()
    svcs = v1.list_namespaced_service(namespace)
    for svc in svcs.items:
        svc_name = svc.metadata.name
        if "metrics" in svc_name or "exporter" in svc_name:
            if svc.spec and svc.spec.ports:
                for port in svc.spec.ports:
                    if port.node_port:
                        return int(port.node_port)
    return None


def wait_for_config_reload(
    namespace: str,
    expected_prefix: str = "test_",
    timeout: int = 90,
) -> bool:
    """Poll ME pod logs for config reload confirmation."""
    v1 = k8s_client.CoreV1Api()
    start = time.time()
    pods = v1.list_namespaced_pod(namespace).items
    while time.time() - start < timeout:
        for pod in pods:
            try:
                logs = v1.read_namespaced_pod_log(pod.metadata.name, namespace, tail_lines=50)
            except ApiException:
                continue
            if expected_prefix in logs or "config reload" in logs.lower() or "configuration changed" in logs.lower():
                LOG.info("Config reload detected in pod %s", pod.metadata.name)
                return True
        time.sleep(5)
    LOG.warning("Config reload not detected after %ds", timeout)
    return False
