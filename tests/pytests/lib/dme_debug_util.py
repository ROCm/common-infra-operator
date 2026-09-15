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
Shared constants and helpers for DME debug-endpoint tests.

The DME exposes pprof/expvar debug endpoints that are controlled by the
``CommonConfig.Debug.EnableAPI`` runtime config toggle (default false).
When disabled, debug endpoints return 404. When enabled, they return 200
with the same access scope as /metrics.

"""

import copy
import logging
import os
import requests as http_requests

Logger = logging.getLogger("lib.dme_debug_util")

DEBUG_ENDPOINTS = [
    "debug/pprof/",
    "debug/pprof/cmdline",
    "debug/pprof/symbol",
    "debug/pprof/allocs",
    "debug/pprof/block",
    "debug/pprof/heap",
    "debug/pprof/mutex",
    "debug/pprof/goroutine",
    "debug/pprof/threadcreate",
    "debug/vars",
]

DEBUG_BLOCKING_ENDPOINTS = [
    "debug/pprof/profile?seconds=1",
    "debug/pprof/trace?seconds=1",
]

PPROF_CONTENT_ENDPOINTS = [
    "debug/pprof/",
    "debug/pprof/cmdline",
    "debug/pprof/symbol",
    "debug/pprof/allocs",
    "debug/pprof/block",
    "debug/pprof/heap",
    "debug/pprof/mutex",
    "debug/pprof/goroutine",
    "debug/pprof/threadcreate",
    "debug/vars",
]

EXPVAR_ENDPOINT = "debug/vars"


def verify_exporter_ready(node, port, logger, use_ssh=True, retries=5, interval=3):
    """Confirm the exporter is serving /metrics before running debug checks."""
    for attempt in range(retries):
        status, _ = _fetch_endpoint(node, port, "metrics", use_ssh=use_ssh)
        if status == "200":
            logger.info(f"[{node.ip_address}] Exporter ready — /metrics returned HTTP 200")
            return True
        logger.debug(f"[{node.ip_address}] /metrics returned HTTP {status}, retry {attempt + 1}/{retries}")
        import time
        time.sleep(interval)
    logger.error(f"[{node.ip_address}] Exporter not ready after {retries} attempts")
    return False


def build_debug_config(ref_config_data, enable_api: bool) -> dict:
    """Return a deep-copied config with CommonConfig.Debug.EnableAPI set."""
    config = copy.deepcopy(ref_config_data)
    config.setdefault('CommonConfig', {}).setdefault('Debug', {})['EnableAPI'] = enable_api
    return config


def _fetch_endpoint(node, port, path, use_ssh=True):
    """Fetch an endpoint and return (http_status_str, body_str).

    use_ssh=True: run curl on the node via SSH (standalone mode).
    use_ssh=False: use requests.get from CI container via node IP (k8s mode).
    """
    if use_ssh:
        rc, out, _ = node.run_command(
            f"/usr/bin/curl -s -o /dev/null -w '%{{http_code}}' "
            f"http://localhost:{port}/{path}", timeout=15)
        if rc != 0:
            Logger.warning(f"[{node.ip_address}] curl rc={rc} for localhost:{port}/{path}")
        return (out.strip() if rc == 0 else "000"), None
    else:
        url = f"http://{node.ip_address}:{port}/{path}"
        try:
            resp = http_requests.get(url, timeout=15)
            body = resp.content.decode("utf-8", errors="replace")
            Logger.debug(f"[{node.ip_address}] GET {url} → HTTP {resp.status_code} ({len(body)} bytes)")
            return str(resp.status_code), body
        except Exception as e:
            Logger.warning(f"[{node.ip_address}] GET {url} failed: {type(e).__name__}: {e}")
            return "000", None


def _fetch_endpoint_body(node, port, path, use_ssh=True):
    """Fetch an endpoint and return (rc_ok, body_str)."""
    if use_ssh:
        rc, out, _ = node.run_command(
            f"/usr/bin/curl -s http://localhost:{port}/{path}", timeout=15)
        body = out if isinstance(out, str) else out.decode("utf-8", errors="replace")
        if rc != 0:
            Logger.warning(f"[{node.ip_address}] curl rc={rc} for localhost:{port}/{path}")
        return rc == 0, body
    else:
        url = f"http://{node.ip_address}:{port}/{path}"
        try:
            resp = http_requests.get(url, timeout=15)
            body = resp.content.decode("utf-8", errors="replace")
            Logger.debug(f"[{node.ip_address}] GET {url} → HTTP {resp.status_code} ({len(body)} bytes)")
            return True, body
        except Exception as e:
            Logger.warning(f"[{node.ip_address}] GET {url} failed: {type(e).__name__}: {e}")
            return False, str(e)


BLOCKED_STATUS_CODES = {"404", "400", "000"}


def verify_debug_endpoints(node, port, expected_code, logger,
                           endpoints=None, use_ssh=True):
    """Probe each debug endpoint, return list of (path, actual_code) failures.

    When checking for disabled state (expected_code=404), only non-blocking
    endpoints are probed.  The server may return 404, 400, or 000 (connection
    reset) when debug is disabled — all three mean the endpoint is blocked.
    For non-404 blocked responses, the body is also checked to confirm no
    profiling data leaked.

    When checking for enabled state (expected_code=200), all endpoints
    including blocking ones are probed with exact status match.
    """
    if endpoints is None:
        endpoints = list(DEBUG_ENDPOINTS)
        if expected_code == 200:
            endpoints.extend(DEBUG_BLOCKING_ENDPOINTS)
    failures = []
    for path in endpoints:
        status, _ = _fetch_endpoint(node, port, path, use_ssh=use_ssh)
        logger.info(f"[{node.ip_address}] GET /{path} → HTTP {status} (expected {expected_code})")

        if expected_code == 404:
            if status not in BLOCKED_STATUS_CODES:
                failures.append((path, status))
            elif status != "404":
                _verify_no_data_leak(node, port, path, status, logger, failures, use_ssh)
        else:
            if status != str(expected_code):
                failures.append((path, status))
    return failures


def _verify_no_data_leak(node, port, path, status, logger, failures, use_ssh):
    """Fetch the body for a non-404 blocked response and verify no profiling data leaked."""
    ok, body = _fetch_endpoint_body(node, port, path, use_ssh=use_ssh)
    body_len = len((body or "").strip()) if ok else 0
    if body_len > 0 and _looks_like_profiling_data(body):
        logger.error(f"[{node.ip_address}] DATA LEAK: /{path} returned HTTP {status} "
                     f"with {body_len} bytes of profiling data")
        failures.append((path, f"{status}-LEAK({body_len}b)"))
    else:
        logger.info(f"[{node.ip_address}] /{path} returned HTTP {status} "
                     f"(blocked, no data leak, body={body_len}b)")


def _looks_like_profiling_data(body):
    """Heuristic: does the response body contain pprof/expvar content."""
    if not body or len(body.strip()) == 0:
        return False
    markers = ["goroutine", "heap profile:", "contentions", "runtime.",
               "memstats", "cmdline", "# runtime/pprof"]
    return any(m in body for m in markers)


def _safe_filename(path):
    """Convert an endpoint path to a safe filename."""
    return path.replace("/", "_").replace("?", "_").replace("=", "_").strip("_")


def dump_debug_endpoint_content(node, port, logdir, logger, use_ssh=True):
    """Fetch and dump all debug endpoint responses to files for post-run analysis.

    Each endpoint is fetched twice:
      - Raw (binary): saved as <endpoint>.bin
      - Text (?debug=1 for pprof): saved as <endpoint>_debug.txt

    Returns list of (path, reason) failures where the response body is empty.
    """
    dump_dir = os.path.join(logdir, f"debug-endpoints-{node.host_name}")
    os.makedirs(dump_dir, exist_ok=True)

    failures = []
    for path in PPROF_CONTENT_ENDPOINTS:
        slug = _safe_filename(path)

        ok, raw_body = _fetch_endpoint_body(node, port, path, use_ssh=use_ssh)
        raw_file = os.path.join(dump_dir, f"{slug}.bin")
        with open(raw_file, "w") as fp:
            fp.write(raw_body or "")
        raw_len = len((raw_body or "").strip())
        logger.info(f"[{node.ip_address}] /{path} raw: {raw_len} bytes → {raw_file}")

        if not ok or raw_len == 0:
            failures.append((path, f"empty or failed response (ok={ok}, len={raw_len})"))
            continue

        if path != EXPVAR_ENDPOINT:
            sep = "&" if "?" in path else "?"
            debug_path = f"{path}{sep}debug=1"
            ok, text_body = _fetch_endpoint_body(node, port, debug_path, use_ssh=use_ssh)
            text_file = os.path.join(dump_dir, f"{slug}_debug.txt")
            with open(text_file, "w") as fp:
                fp.write(text_body or "")
            text_len = len((text_body or "").strip())
            logger.info(f"[{node.ip_address}] /{path}?debug=1 text: {text_len} bytes → {text_file}")

            if not ok or text_len == 0:
                failures.append((f"{path}?debug=1", f"empty or failed response (ok={ok}, len={text_len})"))

    return failures
