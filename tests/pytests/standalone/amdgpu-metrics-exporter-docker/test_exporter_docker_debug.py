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

"""
DME debug-endpoint config toggle tests — Docker container variant.

Verifies CommonConfig.Debug.EnableAPI runtime config toggle:
  - Default (disabled): debug endpoints return 404, /metrics unaffected
  - Enabled via config.json: debug endpoints return 200 with valid content
  - Under load with debug enabled: /metrics remains functional

See: ROCm/device-metrics-exporter#1497
"""

import os
import json
import time
import logging
import pytest
from lib.util import K8Helper
from lib.dme_debug_util import (
    DEBUG_ENDPOINTS, build_debug_config,
    verify_debug_endpoints, verify_exporter_ready,
    dump_debug_endpoint_content,
)

Logger = logging.getLogger("standalone.docker.test_exporter_docker_debug")

_DME_PORT = 5000
_CONFIG_RELOAD_WAIT = 30
_CONCURRENT_PROFILES = 5
_PROFILE_DURATION    = 10
_SCRAPE_UNDER_LOAD   = 3
_LATENCY_MULTIPLIER  = 5.0

_DOCKER_CONFIG_PATH = "/tmp/etc/metrics/config.json"


def _push_config(node, config_data, environment):
    """Write config dict to the Docker bind-mount source path."""
    local_file = os.path.join(environment.logdir, "debug-toggle-config.json")
    with open(local_file, "w") as fp:
        json.dump(config_data, fp, indent=4)
    K8Helper.triage(environment, node.put(local_file, _DOCKER_CONFIG_PATH),
                    f"[{node.ip_address}] Failed to upload config to {_DOCKER_CONFIG_PATH}")


def _read_current_config(node):
    """Read the current config.json from the Docker bind-mount path."""
    rc, out, _ = node.run_command(f"cat {_DOCKER_CONFIG_PATH}")
    if rc == 0 and out.strip():
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            pass
    return {"CommonConfig": {"MetricsFieldPrefix": "amd_"}}


def test_debug_api_disabled_by_default(gpu_cluster, run_exporter_docker_container, environment):
    """Debug endpoints must return 404 when EnableAPI is not set (default)."""
    global Logger
    for node in gpu_cluster.cluster_nodes:
        if not node.is_gpu_node():
            continue

        K8Helper.triage(environment, verify_exporter_ready(node, _DME_PORT, Logger),
                        f"[{node.ip_address}] Exporter not ready before debug endpoint check")

        failures = verify_debug_endpoints(node, _DME_PORT, 404, Logger)

        dump_debug_endpoint_content(node, _DME_PORT, environment.logdir, Logger)

        K8Helper.triage(environment, len(failures) == 0,
                        f"[{node.ip_address}] Debug endpoints not disabled by default: "
                        f"{failures}")

        rc, out, _ = node.run_command(
            f"/usr/bin/curl -s -o /dev/null -w '%{{http_code}}' "
            f"http://localhost:{_DME_PORT}/metrics", timeout=15)
        K8Helper.triage(environment, out.strip() == "200",
                        f"[{node.ip_address}] /metrics returned HTTP {out.strip()} — "
                        f"should be unaffected by debug toggle")


def test_debug_api_explicit_disable(gpu_cluster, run_exporter_docker_container, environment):
    """Debug endpoints must return 404 when EnableAPI is explicitly set to false."""
    global Logger

    for node in gpu_cluster.cluster_nodes:
        if not node.is_gpu_node():
            continue

        K8Helper.triage(environment, verify_exporter_ready(node, _DME_PORT, Logger),
                        f"[{node.ip_address}] Exporter not ready before debug endpoint check")

        ref_config = _read_current_config(node)
        disabled_config = build_debug_config(ref_config, False)
        _push_config(node, disabled_config, environment)
        time.sleep(_CONFIG_RELOAD_WAIT)

        failures = verify_debug_endpoints(node, _DME_PORT, 404, Logger)

        dump_debug_endpoint_content(node, _DME_PORT, environment.logdir, Logger)

        K8Helper.triage(environment, len(failures) == 0,
                        f"[{node.ip_address}] Debug endpoints not disabled after "
                        f"explicit EnableAPI=false: {failures}")

        rc, out, _ = node.run_command(
            f"/usr/bin/curl -s -o /dev/null -w '%{{http_code}}' "
            f"http://localhost:{_DME_PORT}/metrics", timeout=15)
        K8Helper.triage(environment, out.strip() == "200",
                        f"[{node.ip_address}] /metrics returned HTTP {out.strip()} — "
                        f"should be unaffected when debug is explicitly disabled")


def test_debug_api_enable_via_config(gpu_cluster, run_exporter_docker_container, environment):
    """Toggle Debug.EnableAPI true, verify endpoints respond with valid content, then restore."""
    global Logger

    for node in gpu_cluster.cluster_nodes:
        if not node.is_gpu_node():
            continue

        K8Helper.triage(environment, verify_exporter_ready(node, _DME_PORT, Logger),
                        f"[{node.ip_address}] Exporter not ready before debug endpoint check")

        ref_config = _read_current_config(node)

        # Enable debug API
        enabled_config = build_debug_config(ref_config, True)
        _push_config(node, enabled_config, environment)
        time.sleep(_CONFIG_RELOAD_WAIT)

        # Verify all endpoints return 200
        failures = verify_debug_endpoints(node, _DME_PORT, 200, Logger)
        K8Helper.triage(environment, len(failures) == 0,
                        f"[{node.ip_address}] Debug endpoints not accessible after "
                        f"EnableAPI=true: {failures}")

        # Verify endpoint content is valid
        content_failures = dump_debug_endpoint_content(
            node, _DME_PORT, environment.logdir, Logger)
        K8Helper.triage(environment, len(content_failures) == 0,
                        f"[{node.ip_address}] Debug endpoint content validation failed: "
                        f"{content_failures}")

        # /metrics must remain unaffected
        rc, out, _ = node.run_command(
            f"/usr/bin/curl -s -o /dev/null -w '%{{http_code}}' "
            f"http://localhost:{_DME_PORT}/metrics", timeout=15)
        K8Helper.triage(environment, out.strip() == "200",
                        f"[{node.ip_address}] /metrics returned HTTP {out.strip()} — "
                        f"should be unaffected when debug is enabled")

        # Restore and verify endpoints return 404
        disabled_config = build_debug_config(ref_config, False)
        _push_config(node, disabled_config, environment)
        time.sleep(_CONFIG_RELOAD_WAIT)

        failures = verify_debug_endpoints(node, _DME_PORT, 404, Logger)
        K8Helper.triage(environment, len(failures) == 0,
                        f"[{node.ip_address}] Debug endpoints not disabled after "
                        f"EnableAPI=false: {failures}")


def test_debug_api_no_metrics_degradation(gpu_cluster, run_exporter_docker_container, environment):
    """With Debug.EnableAPI=true, /metrics must remain functional under concurrent profile load.

    Phase 1 — Baseline: 3 sequential /metrics scrapes, compute median latency.
    Phase 2 — Load: fire concurrent profile requests in the background.
    Phase 3 — Under load: scrape /metrics and assert HTTP 200 within latency ceiling.
    """
    global Logger

    for node in gpu_cluster.cluster_nodes:
        if not node.is_gpu_node():
            continue

        K8Helper.triage(environment, verify_exporter_ready(node, _DME_PORT, Logger),
                        f"[{node.ip_address}] Exporter not ready before latency test")

        ref_config = _read_current_config(node)
        enabled_config = build_debug_config(ref_config, True)
        _push_config(node, enabled_config, environment)
        time.sleep(_CONFIG_RELOAD_WAIT)

        # Phase 1: baseline
        baseline_times = []
        for _ in range(3):
            rc, out, _ = node.run_command(
                f"/usr/bin/curl -s -o /dev/null -w '%{{http_code}}:%{{time_total}}' "
                f"http://localhost:{_DME_PORT}/metrics", timeout=30)
            if rc == 0 and ":" in out:
                code, secs = out.strip().split(":", 1)
                if code == "200":
                    try:
                        baseline_times.append(float(secs))
                    except ValueError:
                        pass

        K8Helper.triage(environment, len(baseline_times) > 0,
                        f"[{node.ip_address}] Could not establish /metrics baseline")
        if not baseline_times:
            continue

        baseline_times.sort()
        baseline_median = baseline_times[len(baseline_times) // 2]
        gpu_scale = max(1, node.num_gpus)
        latency_ceiling = _LATENCY_MULTIPLIER * baseline_median * gpu_scale
        latency_ceiling = max(latency_ceiling, 0.5)
        Logger.info(f"[{node.ip_address}] baseline: {baseline_median:.3f}s  "
                    f"ceiling: {latency_ceiling:.3f}s (gpu_scale={gpu_scale})")

        # Phase 2: flood with concurrent profile requests
        bg_cmd = (
            f"nohup bash -c '"
            f"for i in $(seq 1 {_CONCURRENT_PROFILES}); do "
            f"/usr/bin/curl -s -o /dev/null "
            f"\"http://localhost:{_DME_PORT}/debug/pprof/profile?seconds={_PROFILE_DURATION}\" & "
            f"done' >/dev/null 2>&1 &"
        )
        node.run_command(bg_cmd, timeout=10)
        time.sleep(2)

        # Phase 3: scrape under load
        for i in range(1, _SCRAPE_UNDER_LOAD + 1):
            rc, out, _ = node.run_command(
                f"/usr/bin/curl -s -o /dev/null -w '%{{http_code}}:%{{time_total}}' "
                f"http://localhost:{_DME_PORT}/metrics", timeout=30)
            code, elapsed = "", ""
            if ":" in out:
                code, elapsed = out.strip().split(":", 1)
            Logger.info(f"[{node.ip_address}] Under-load scrape {i}/{_SCRAPE_UNDER_LOAD}: "
                        f"HTTP {code}  latency {elapsed}s")

            K8Helper.triage(environment, code == "200",
                            f"[{node.ip_address}] /metrics returned HTTP {code} during "
                            f"{_CONCURRENT_PROFILES} concurrent profile requests")
            if code == "200":
                try:
                    K8Helper.triage(
                        environment,
                        float(elapsed) <= latency_ceiling,
                        f"[{node.ip_address}] /metrics latency {float(elapsed):.3f}s exceeds "
                        f"{_LATENCY_MULTIPLIER}× baseline ({latency_ceiling:.3f}s)")
                except ValueError:
                    pass

        # Restore
        disabled_config = build_debug_config(ref_config, False)
        _push_config(node, disabled_config, environment)
        time.sleep(_CONFIG_RELOAD_WAIT)
