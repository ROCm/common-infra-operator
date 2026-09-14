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
import re
import subprocess
import sys
import copy
import time


TARGET_CONTAINER = "network-device-metrics-exporter"


def run(cmd):
    r = subprocess.run(
        cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def fetch_metrics(port, source_port=None, path="/metrics"):
    if source_port is not None:
        cmd = f"curl -sS --local-port {source_port} 'http://localhost:{port}{path}'"
    else:
        cmd = f"curl -sS 'http://localhost:{port}{path}'"
    return run(cmd)


def non_comment_lines(metrics_text):
    return [ln for ln in metrics_text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def write_config(config_path, cfg):
    with open(config_path, "w") as f:
        json.dump(cfg, f, indent=2)


def main():
    if len(sys.argv) != 2:
        print("Usage: python validate_exporter_docker.py <expected_image>")
        print("Example: python validate_exporter_docker.py rocm/device-metrics-exporter:nic-v1.1.0")
        sys.exit(1)

    expected_image = sys.argv[1].strip()
    fail = False
    failed_tests = []
    skipped_tests = []
    script_dir = os.path.dirname(os.path.abspath(__file__))
    can_run_remaining = True

    # Test 1: check docker ps has the target container
    print("\n" + "=" * 60)
    print("Test 1: Container Presence Validation")
    print("=" * 60)

    found = False
    actual_image = None
    rc, out, err = run("docker ps --format '{{.Names}}|{{.Image}}'")
    if rc != 0:
        print("docker ps command : FAIL")
        if err:
            print(err)
        fail = True
        failed_tests.append("Test 1: Container Presence Validation")
        can_run_remaining = False
    else:
        for line in out.splitlines():
            parts = line.split("|", 1)
            if len(parts) != 2:
                continue
            name, image = parts[0].strip(), parts[1].strip()
            if name == TARGET_CONTAINER:
                found = True
                actual_image = image
                break

        if not found:
            print(f"Container '{TARGET_CONTAINER}' present : FAIL")
            fail = True
            failed_tests.append("Test 1: Container Presence Validation")
            can_run_remaining = False
        else:
            print(f"Container '{TARGET_CONTAINER}' present : PASS")

    # Test 2: validate image of the target container
    print("\n" + "=" * 60)
    print("Test 2: Image Validation")
    print("=" * 60)

    if not can_run_remaining:
        print("SKIP: no running target container from Test 1, skipping remaining test cases")
        skipped_tests.append("Test 2: Image Validation")
    else:
        if actual_image == expected_image:
            print(f"Image match ({actual_image}) : PASS")
        else:
            print("Image match : FAIL")
            print(f"Expected: {expected_image}")
            print(f"Actual  : {actual_image}")
            fail = True
            failed_tests.append("Test 2: Image Validation")

    # Test 3: config/config.json -> ServerPort -> localhost:<port>/metrics
    print("\n" + "=" * 60)
    print("Test 3: Config ServerPort Metrics Validation")
    print("=" * 60)

    if not can_run_remaining:
        print("SKIP: no running target container from Test 1, skipping remaining test cases")
        skipped_tests.append("Test 3: Config ServerPort Metrics Validation")
        metrics_out = ""
        cfg = {}
    else:
        config_path = os.path.join(script_dir, "config", "config.json")
        try:
            with open(config_path, "r") as f:
                cfg = json.load(f)
            port = int(cfg.get("ServerPort"))
            print(f"ServerPort from config: {port}")
        except Exception as e:
            print(f"FAIL: could not load ServerPort from {config_path}: {e}")
            fail = True
            failed_tests.append("Test 3: Config ServerPort Metrics Validation")
            port = None

        if port is not None:
            cmd = f"curl -sS http://localhost:{port}/metrics"
            print(f"Running on host: {cmd}")
            rc, metrics_out, metrics_err = run(cmd)
            if rc != 0:
                print(f"FAIL: curl localhost:{port}/metrics failed")
                if metrics_err:
                    print(metrics_err)
                fail = True
                failed_tests.append("Test 3: Config ServerPort Metrics Validation")
            else:
                # Require non-empty metrics body with at least one non-comment line.
                non_comment = [ln for ln in metrics_out.splitlines() if ln.strip() and not ln.strip().startswith("#")]
                if non_comment:
                    print(f"PASS: localhost:{port}/metrics returned values")
                else:
                    print(f"FAIL: localhost:{port}/metrics returned empty/no metric values")
                    fail = True
                    failed_tests.append("Test 3: Config ServerPort Metrics Validation")

    # Test 4: Validate all NICConfig.Fields metrics are present in /metrics output.
    print("\n" + "=" * 60)
    print("Test 4: NICConfig Fields Metrics Validation")
    print("=" * 60)

    if not can_run_remaining:
        print("SKIP: no running target container from Test 1, skipping remaining test cases")
        skipped_tests.append("Test 4: NICConfig Fields Metrics Validation")
    else:
        config_path = os.path.join(script_dir, "config", "config.json")
        if not cfg:
            try:
                with open(config_path, "r") as f:
                    cfg = json.load(f)
            except Exception as e:
                print(f"FAIL: could not load config from {config_path}: {e}")
                fail = True
                failed_tests.append("Test 4: NICConfig Fields Metrics Validation")
                cfg = {}

        fields = ((cfg.get("NICConfig") or {}).get("Fields") or [])
        prefix = ((cfg.get("CommonConfig") or {}).get("MetricsFieldPrefix") or "amd_")
        if prefix and not prefix.endswith("_"):
            prefix += "_"

        if not fields:
            print("FAIL: NICConfig.Fields is empty or missing in config/config.json")
            fail = True
            failed_tests.append("Test 4: NICConfig Fields Metrics Validation")
        else:
            # Re-fetch metrics if previous test failed to populate output.
            if not metrics_out.strip():
                port = int(cfg.get("ServerPort", 5001))
                rc, metrics_out, metrics_err = run(f"curl -sS http://localhost:{port}/metrics")
                if rc != 0:
                    print(f"FAIL: could not fetch metrics from localhost:{port}/metrics")
                    if metrics_err:
                        print(metrics_err)
                    fail = True
                    failed_tests.append("Test 4: NICConfig Fields Metrics Validation")
                    metrics_out = ""

            if metrics_out.strip():
                metrics_lower = metrics_out.lower()
                missing = []
                for field in fields:
                    if not field or not field.strip():
                        continue
                    metric_name = f"{prefix}{field}".lower()
                    # Normalize PRI fields: PRI0 or PRI_0 -> pri_0
                    metric_name = re.sub(r'pri_?(\d)', r'pri_\1', metric_name)
                    if metric_name not in metrics_lower:
                        missing.append(metric_name)

                found_count = len(fields) - len(missing)
                print(f"Fields expected : {len(fields)}")
                print(f"Fields found    : {found_count}")
                print(f"Fields missing  : {len(missing)}")

                if missing:
                    print("Missing examples:", ", ".join(missing[:10]))
                    fail = True
                    failed_tests.append("Test 4: NICConfig Fields Metrics Validation")
                else:
                    print("PASS: all NICConfig.Fields metrics are present")

                # Identify QP fields from config (LIF_QP_* or QP_*)
                qp_fields = [f for f in fields if f and (f.startswith("LIF_QP_") or f.startswith("QP_"))]

                if qp_fields:
                    # 4a: Verify LIF_QP_*_TOTAL fields are present in /metrics (aggregate QP stats)
                    lif_qp_fields = [f for f in qp_fields if f.startswith("LIF_QP_")]
                    if lif_qp_fields:
                        lif_qp_missing = []
                        for field in lif_qp_fields:
                            metric_name = f"{prefix}{field.lower()}"
                            if metric_name not in metrics_lower:
                                lif_qp_missing.append(metric_name)
                        if lif_qp_missing:
                            print(f"FAIL: /metrics missing {len(lif_qp_missing)} LIF_QP fields: {lif_qp_missing[:5]}")
                            fail = True
                            failed_tests.append("Test 4: NICConfig Fields Metrics Validation")
                        else:
                            print(f"PASS: all {len(lif_qp_fields)} LIF_QP fields present in /metrics")

                    # 4b: Fetch /metrics?debug=qp and verify QP stats are present
                    current_port = int(cfg.get("ServerPort", 5001))
                    rc_qp, qp_out, qp_err = fetch_metrics(current_port, path="/metrics?debug=qp")
                    if rc_qp != 0:
                        print(f"FAIL: could not fetch /metrics?debug=qp")
                        if qp_err:
                            print(qp_err)
                        fail = True
                        failed_tests.append("Test 4: NICConfig Fields Metrics Validation")
                    else:
                        qp_metric_lines = non_comment_lines(qp_out)
                        if not qp_metric_lines:
                            print("FAIL: /metrics?debug=qp returned empty/no metric values")
                            fail = True
                            failed_tests.append("Test 4: NICConfig Fields Metrics Validation")
                        else:
                            print(f"PASS: /metrics?debug=qp returned {len(qp_metric_lines)} metric lines")

                            # 4c: Verify all QP config fields are present in debug=qp output
                            qp_metrics_lower = qp_out.lower()
                            qp_missing = []
                            for field in qp_fields:
                                if field.startswith("QP_"):
                                    # QP_ fields map to lif_qp_*_total
                                    metric_name = f"{prefix}lif_{field.lower()}_total"
                                else:
                                    # LIF_QP_*_TOTAL fields lower directly
                                    metric_name = f"{prefix}{field.lower()}"
                                if metric_name not in qp_metrics_lower:
                                    qp_missing.append(metric_name)

                            print(f"QP fields expected : {len(qp_fields)}")
                            print(f"QP fields found    : {len(qp_fields) - len(qp_missing)}")
                            print(f"QP fields missing  : {len(qp_missing)}")
                            if qp_missing:
                                print("Missing QP examples:", ", ".join(qp_missing[:10]))
                                fail = True
                                failed_tests.append("Test 4: NICConfig Fields Metrics Validation")
                            else:
                                print("PASS: all QP fields present in /metrics?debug=qp")

    # Test 5: Validate all NICConfig.Labels exist in pulled metrics output.
    print("\n" + "=" * 60)
    print("Test 5: NICConfig Labels Metrics Validation")
    print("=" * 60)

    if not can_run_remaining:
        print("SKIP: no running target container from Test 1, skipping remaining test cases")
        skipped_tests.append("Test 5: NICConfig Labels Metrics Validation")
    else:
        labels = ((cfg.get("NICConfig") or {}).get("Labels") or [])
        if not labels:
            print("FAIL: NICConfig.Labels is empty or missing in config/config.json")
            fail = True
            failed_tests.append("Test 5: NICConfig Labels Metrics Validation")
        else:
            # Ensure metrics are available for label validation.
            if not metrics_out.strip():
                port = int(cfg.get("ServerPort", 5001))
                rc, metrics_out, metrics_err = run(f"curl -sS http://localhost:{port}/metrics")
                if rc != 0:
                    print(f"FAIL: could not fetch metrics from localhost:{port}/metrics")
                    if metrics_err:
                        print(metrics_err)
                    fail = True
                    failed_tests.append("Test 5: NICConfig Labels Metrics Validation")
                    metrics_out = ""

            if metrics_out.strip():
                metrics_lower = metrics_out.lower()
                missing_labels = []
                for lbl in labels:
                    # Label names are expected in Prometheus format (lowercase) like label_name="..."
                    token = f'{lbl.lower()}="'
                    if token not in metrics_lower:
                        missing_labels.append(lbl)

                found_count = len(labels) - len(missing_labels)
                print(f"Labels expected : {len(labels)}")
                print(f"Labels found    : {found_count}")
                print(f"Labels missing  : {len(missing_labels)}")

                if missing_labels:
                    print("Missing labels:", ", ".join(missing_labels[:10]))
                    fail = True
                    failed_tests.append("Test 5: NICConfig Labels Metrics Validation")
                else:
                    print("PASS: all NICConfig.Labels are present in metrics output")

    # Test 6: Temporarily switch ServerPort to 5010, validate, then restore.
    print("\n" + "=" * 60)
    print("Test 6: Port 5010 Metrics Validation")
    print("=" * 60)

    if not can_run_remaining:
        print("SKIP: no running target container from Test 1, skipping remaining test cases")
        skipped_tests.append("Test 6: Port 5010 Metrics Validation")
    else:
        config_path = os.path.join(script_dir, "config", "config.json")
        original_port = int((cfg.get("ServerPort", 5001) if cfg else 5001))
        test6_failed = False

        try:
            if not cfg:
                with open(config_path, "r") as f:
                    cfg = json.load(f)
                original_port = int(cfg.get("ServerPort", 5001))

            cfg["ServerPort"] = 5010
            write_config(config_path, cfg)
            time.sleep(5)

            cmd = "curl -sS http://localhost:5010/metrics"
            print(f"Running on host: {cmd}")
            rc, src_metrics_out, src_metrics_err = run(cmd)

            if rc != 0:
                print("FAIL: metrics pull from localhost:5010 failed")
                if src_metrics_err:
                    print(src_metrics_err)
                test6_failed = True
            else:
                non_comment = [
                    ln for ln in src_metrics_out.splitlines()
                    if ln.strip() and not ln.strip().startswith("#")
                ]
                if non_comment:
                    print("PASS: localhost:5010/metrics works")
                else:
                    print("FAIL: localhost:5010/metrics request returned empty/no metric values")
                    test6_failed = True

            # Also validate /metrics?debug=qp on the new port
            if not test6_failed:
                qp_cmd = "curl -sS 'http://localhost:5010/metrics?debug=qp'"
                print(f"Running on host: {qp_cmd}")
                rc_qp, qp_out, qp_err = run(qp_cmd)
                if rc_qp != 0:
                    print("FAIL: metrics pull from localhost:5010/metrics?debug=qp failed")
                    if qp_err:
                        print(qp_err)
                    test6_failed = True
                else:
                    qp_non_comment = non_comment_lines(qp_out)
                    if qp_non_comment:
                        print("PASS: localhost:5010/metrics?debug=qp works")
                    else:
                        print("FAIL: localhost:5010/metrics?debug=qp returned empty/no metric values")
                        test6_failed = True
        except Exception as e:
            print(f"FAIL: Test 6 setup/validation error: {e}")
            test6_failed = True
        finally:
            try:
                cfg["ServerPort"] = original_port
                write_config(config_path, cfg)
                time.sleep(5)
                print(f"Restored ServerPort to {original_port}")
            except Exception as e:
                print(f"WARNING: failed to restore ServerPort to {original_port}: {e}")

        if test6_failed:
            fail = True
            failed_tests.append("Test 6: Port 5010 Metrics Validation")

    # Prepare for advanced config-mutation tests.
    config_path = os.path.join(script_dir, "config", "config.json")
    original_cfg = copy.deepcopy(cfg) if cfg else None
    qp_sum = None
    expected_qp_id = None

    # Test 7: Update MetricsFieldPrefix (amd_ -> pensando_)
    print("\n" + "=" * 60)
    print("Test 7: Update MetricsFieldPrefix (amd_ -> pensando_)")
    print("=" * 60)

    if not can_run_remaining:
        print("SKIP: no running target container from Test 1, skipping remaining test cases")
        skipped_tests.append("Test 7: Update MetricsFieldPrefix (amd_ -> pensando_)")
    elif not cfg:
        print("SKIP: config not loaded")
        skipped_tests.append("Test 7: Update MetricsFieldPrefix (amd_ -> pensando_)")
    else:
        test_name = "Test 7: Update MetricsFieldPrefix (amd_ -> pensando_)"
        test_failed = False
        try:
            current_port = int(cfg.get("ServerPort", 5001))
            old_prefix = ((cfg.get("CommonConfig") or {}).get("MetricsFieldPrefix") or "amd_")
            new_prefix = "pensando_"
            cfg.setdefault("CommonConfig", {})["MetricsFieldPrefix"] = new_prefix
            write_config(config_path, cfg)
            time.sleep(5)

            rc, out, err = fetch_metrics(current_port)
            if rc != 0:
                print(f"FAIL: unable to fetch metrics on port {current_port}")
                if err:
                    print(err)
                test_failed = True
            else:
                metric_lines = "\n".join(non_comment_lines(out)).lower()
                if new_prefix.lower() not in metric_lines:
                    print(f"FAIL: new prefix '{new_prefix}' not found in metrics")
                    test_failed = True
                else:
                    print(f"PASS: new prefix '{new_prefix}' found in metrics")
                if old_prefix.lower() in metric_lines:
                    print(f"WARNING: old prefix '{old_prefix}' still present in some metrics")
        except Exception as e:
            print(f"FAIL: {e}")
            test_failed = True

        if test_failed:
            fail = True
            failed_tests.append(test_name)

    # Test 8: Remove ETH_ Fields
    print("\n" + "=" * 60)
    print("Test 8: Remove ETH_ Fields")
    print("=" * 60)

    if not can_run_remaining:
        print("SKIP: no running target container from Test 1, skipping remaining test cases")
        skipped_tests.append("Test 8: Remove ETH_ Fields")
    elif not cfg:
        print("SKIP: config not loaded")
        skipped_tests.append("Test 8: Remove ETH_ Fields")
    else:
        test_name = "Test 8: Remove ETH_ Fields"
        test_failed = False
        try:
            current_port = int(cfg.get("ServerPort", 5001))
            fields = ((cfg.get("NICConfig") or {}).get("Fields") or [])
            non_eth = [f for f in fields if not str(f).startswith("ETH_")]
            cfg.setdefault("NICConfig", {})["Fields"] = non_eth
            write_config(config_path, cfg)
            time.sleep(5)

            rc, out, err = fetch_metrics(current_port)
            if rc != 0:
                print(f"FAIL: unable to fetch metrics on port {current_port}")
                if err:
                    print(err)
                test_failed = True
            else:
                metric_lines = "\n".join(non_comment_lines(out)).lower()
                if "_eth_" in metric_lines:
                    print("FAIL: ETH_ metrics still present after removal")
                    test_failed = True
                else:
                    print("PASS: ETH_ metrics removed from output")
        except Exception as e:
            print(f"FAIL: {e}")
            test_failed = True

        if test_failed:
            fail = True
            failed_tests.append(test_name)

    # Test 9: Update NICConfig Fields (replace first 2, validate, restore)
    print("\n" + "=" * 60)
    print("Test 9: Update NICConfig Fields")
    print("=" * 60)

    if not can_run_remaining:
        print("SKIP: no running target container from Test 1, skipping remaining test cases")
        skipped_tests.append("Test 9: Update NICConfig Fields")
    elif not cfg:
        print("SKIP: config not loaded")
        skipped_tests.append("Test 9: Update NICConfig Fields")
    else:
        test_name = "Test 9: Update NICConfig Fields"
        test_failed = False
        try:
            current_port = int(cfg.get("ServerPort", 5001))
            fields = list(((cfg.get("NICConfig") or {}).get("Fields") or []))
            if len(fields) < 2:
                print("SKIP: fewer than 2 NICConfig.Fields")
                skipped_tests.append(test_name)
            else:
                old_fields = fields[:2]
                new_fields = [
                    "NIC_PORT_STATS_FRAMES_RX_SDF",
                    "NIC_PORT_STATS_FRAMES_RX_JJJ",
                ]
                cfg.setdefault("NICConfig", {})["Fields"] = new_fields + fields[2:]
                write_config(config_path, cfg)
                time.sleep(5)

                rc, out, err = fetch_metrics(current_port)
                if rc != 0:
                    print(f"FAIL: unable to fetch metrics on port {current_port}")
                    if err:
                        print(err)
                    test_failed = True
                else:
                    metric_lines = "\n".join(non_comment_lines(out)).lower()
                    for oldf in old_fields:
                        if oldf.lower() in metric_lines:
                            print(f"FAIL: old field still present: {oldf}")
                            test_failed = True
                    if not test_failed:
                        print("PASS: old NICConfig fields removed from metrics output")
                
                if not test_failed:
                    cfg.setdefault("NICConfig", {})["Fields"] = old_fields + fields[2:]
                    write_config(config_path, cfg)
                    time.sleep(5)
                    rc, out, err = fetch_metrics(current_port)
                    if rc != 0:
                        print(f"FAIL: unable to fetch metrics after restore")
                        if err:
                            print(err)
                        test_failed = True
                    else:
                        metric_lines = "\n".join(non_comment_lines(out)).lower()
                        restored_ok = True
                        for oldf in old_fields:
                            if oldf.lower() not in metric_lines:
                                print(f"FAIL: original field not restored: {oldf}")
                                restored_ok = False
                                test_failed = True
                        if restored_ok:
                            print("PASS: original NICConfig fields restored")
        except Exception as e:
            print(f"FAIL: {e}")
            test_failed = True

        if test_failed:
            fail = True
            failed_tests.append(test_name)

    # Test 10: Remove Specific NICConfig Labels (validate per metric type)
    print("\n" + "=" * 60)
    print("Test 10: Remove Specific NICConfig Labels")
    print("=" * 60)

    if not can_run_remaining:
        print("SKIP: no running target container from Test 1, skipping remaining test cases")
        skipped_tests.append("Test 10: Remove Specific NICConfig Labels")
    elif not cfg:
        print("SKIP: config not loaded")
        skipped_tests.append("Test 10: Remove Specific NICConfig Labels")
    else:
        test_name = "Test 10: Remove Specific NICConfig Labels"
        test_failed = False
        try:
            current_port = int(cfg.get("ServerPort", 5001))
            labels = list(((cfg.get("NICConfig") or {}).get("Labels") or []))
            labels_to_remove = {"NIC_UUID", "POD", "POD_UUID", "NAMESPACE", "CONTAINER", "FIRMWARE_VERSION"}
            new_labels = [l for l in labels if l not in labels_to_remove]
            cfg.setdefault("NICConfig", {})["Labels"] = new_labels
            write_config(config_path, cfg)
            time.sleep(5)

            rc, out, err = fetch_metrics(current_port)
            if rc != 0:
                print(f"FAIL: unable to fetch metrics on port {current_port}")
                if err:
                    print(err)
                test_failed = True
            else:
                metric_lines = "\n".join(non_comment_lines(out))
                
                # nic_port_stats should have ONLY remaining labels (3: NIC_ID, HOSTNAME, SERIAL_NUMBER)
                nic_port_stats_lines = [ln for ln in metric_lines.split("\n") if "nic_port_stats" in ln.lower()]
                if nic_port_stats_lines:
                    for removed in labels_to_remove:
                        token = f'{removed.lower()}="'
                        for line in nic_port_stats_lines:
                            if token in line.lower():
                                print(f"FAIL: removed label '{removed}' still in nic_port_stats metrics")
                                test_failed = True
                    if not test_failed:
                        print("PASS: removed labels NOT present in nic_port_stats metrics")
                else:
                    print("WARNING: no nic_port_stats metrics found to validate")
                
                # eth_ metrics should keep all labels (test that at least some remain)
                eth_lines = [ln for ln in metric_lines.split("\n") if "eth_" in ln.lower()]
                if eth_lines:
                    eth_has_labels = any(f'{l.lower()}="' in " ".join(eth_lines).lower() for l in new_labels if new_labels)
                    if eth_has_labels:
                        print("PASS: eth_ metrics retain expected labels")
                    else:
                        print("WARNING: eth_ metrics may not have expected labels")
        except Exception as e:
            print(f"FAIL: {e}")
            test_failed = True

        if test_failed:
            fail = True
            failed_tests.append(test_name)

    # Test 11: Add CustomLabels
    print("\n" + "=" * 60)
    print("Test 11: Add CustomLabels")
    print("=" * 60)

    if not can_run_remaining:
        print("SKIP: no running target container from Test 1, skipping remaining test cases")
        skipped_tests.append("Test 11: Add CustomLabels")
    elif not cfg:
        print("SKIP: config not loaded")
        skipped_tests.append("Test 11: Add CustomLabels")
    else:
        test_name = "Test 11: Add CustomLabels"
        test_failed = False
        try:
            current_port = int(cfg.get("ServerPort", 5001))
            cfg.setdefault("NICConfig", {})["CustomLabels"] = {
                "CLUSTER_NAME": "amdnetwork-k8s-metrics-exporter",
                "CLUSTER_ENVIRONMENT": "systest",
            }
            write_config(config_path, cfg)
            time.sleep(5)

            rc, out, err = fetch_metrics(current_port)
            if rc != 0:
                print(f"FAIL: unable to fetch metrics on port {current_port}")
                if err:
                    print(err)
                test_failed = True
            else:
                metric_lines = "\n".join(non_comment_lines(out)).lower()
                if 'cluster_name="amdnetwork-k8s-metrics-exporter"' not in metric_lines:
                    print("FAIL: cluster_name custom label not found")
                    test_failed = True
                if 'cluster_environment="systest"' not in metric_lines:
                    print("FAIL: cluster_environment custom label not found")
                    test_failed = True
                if not test_failed:
                    print("PASS: custom labels found in metrics output")
        except Exception as e:
            print(f"FAIL: {e}")
            test_failed = True

        if test_failed:
            fail = True
            failed_tests.append(test_name)

    # Test 12: Restore Original Configuration
    print("\n" + "=" * 60)
    print("Test 12: Restore Original Configuration")
    print("=" * 60)

    if not can_run_remaining:
        print("SKIP: no running target container from Test 1, skipping remaining test cases")
        skipped_tests.append("Test 12: Restore Original Configuration")
    elif original_cfg is None:
        print("SKIP: original config backup unavailable")
        skipped_tests.append("Test 12: Restore Original Configuration")
    else:
        test_name = "Test 12: Restore Original Configuration"
        test_failed = False
        try:
            write_config(config_path, original_cfg)
            cfg = copy.deepcopy(original_cfg)
            time.sleep(5)
            current_port = int(cfg.get("ServerPort", 5001))
            rc, out, err = fetch_metrics(current_port)
            if rc != 0 or not non_comment_lines(out):
                print("FAIL: metrics not available after restore")
                if err:
                    print(err)
                test_failed = True
            else:
                print("PASS: original configuration restored")
        except Exception as e:
            print(f"FAIL: {e}")
            test_failed = True

        if test_failed:
            fail = True
            failed_tests.append(test_name)

    # Test 13: RDMA Queue-Pair Validation
    print("\n" + "=" * 60)
    print("Test 13: RDMA Queue-Pair Validation")
    print("=" * 60)

    if not can_run_remaining:
        print("SKIP: no running target container from Test 1, skipping remaining test cases")
        skipped_tests.append("Test 13: RDMA Queue-Pair Validation")
    else:
        test_name = "Test 13: RDMA Queue-Pair Validation"
        test_failed = False
        rc, rdma_out, err = run(f"docker exec {TARGET_CONTAINER} /bin/sh -lc \"nicctl show rdma queue-pair --summary\"")
        if rc != 0:
            print("FAIL: nicctl rdma queue-pair summary command failed")
            if err:
                print(err)
            test_failed = True
        else:
            qp_sum = 0
            for line in rdma_out.splitlines():
                if "Number of queue pairs" in line:
                    try:
                        qp_sum += int(line.split(":", 1)[1].strip())
                    except Exception:
                        pass

            expected_qp_id = qp_sum
            print(f"Total queue pairs : {qp_sum}")
            print(f"Expected metric count : {expected_qp_id}")
            if qp_sum <= 0:
                print("FAIL: queue pair sum is zero/invalid")
                test_failed = True

        if test_failed:
            fail = True
            failed_tests.append(test_name)

    # Test 14: Metric Count Validation
    print("\n" + "=" * 60)
    print("Test 14: Metric Count Validation")
    print("=" * 60)

    if not can_run_remaining:
        print("SKIP: no running target container from Test 1, skipping remaining test cases")
        skipped_tests.append("Test 14: Metric Count Validation")
    elif expected_qp_id is None:
        print("SKIP: expected_qp_id unavailable from Test 13")
        skipped_tests.append("Test 14: Metric Count Validation")
    else:
        test_name = "Test 14: Metric Count Validation"
        test_failed = False
        try:
            current_port = int((cfg.get("ServerPort", 5001) if cfg else 5001))
            rc, out, err = fetch_metrics(current_port, path="/metrics?debug=qp")
            if rc != 0:
                print("FAIL: could not fetch /metrics?debug=qp for count validation")
                if err:
                    print(err)
                test_failed = True
            else:
                metric_count = sum(1 for ln in non_comment_lines(out) if "amd_qp_rq_rsp_rx_num_packet" in ln)
                print(f"amd_qp_rq_rsp_rx_num_packet count : {metric_count}")
                if metric_count != expected_qp_id:
                    print(f"FAIL: expected {expected_qp_id}, got {metric_count}")
                    test_failed = True
                else:
                    print("PASS: metric count matches expected qp_id")
        except Exception as e:
            print(f"FAIL: {e}")
            test_failed = True

        if test_failed:
            fail = True
            failed_tests.append(test_name)

    # Test 15: Metric Value Sum Validation
    print("\n" + "=" * 60)
    print("Test 15: Metric Value Sum Validation")
    print("=" * 60)

    if not can_run_remaining:
        print("SKIP: no running target container from Test 1, skipping remaining test cases")
        skipped_tests.append("Test 15: Metric Value Sum Validation")
    elif expected_qp_id is None or qp_sum is None:
        print("SKIP: qp validation values unavailable from Test 13")
        skipped_tests.append("Test 15: Metric Value Sum Validation")
    else:
        test_name = "Test 15: Metric Value Sum Validation"
        test_failed = False
        try:
            current_port = int((cfg.get("ServerPort", 5001) if cfg else 5001))
            rc, out, err = fetch_metrics(current_port, path="/metrics?debug=qp")
            if rc != 0:
                print("FAIL: could not fetch metrics for value sum validation")
                if err:
                    print(err)
                test_failed = True
            else:
                qp_id_to_check = 3200 if qp_sum >= 4000 else max(1, expected_qp_id // 2)
                value_sum = 0.0
                for ln in non_comment_lines(out):
                    if "num_packet" not in ln:
                        continue
                    if str(qp_id_to_check) not in ln:
                        continue
                    parts = ln.split()
                    if not parts:
                        continue
                    try:
                        value_sum += float(parts[-1])
                    except Exception:
                        continue

                print(f"Metric value sum for qp_id {qp_id_to_check} : {int(value_sum)}")
                if value_sum <= 1000:
                    print("FAIL: metric value sum <= 1000")
                    test_failed = True
                else:
                    print("PASS: metric value sum validation")
        except Exception as e:
            print(f"FAIL: {e}")
            test_failed = True

        if test_failed:
            fail = True
            failed_tests.append(test_name)

    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    total_tests = 15
    passed_tests = total_tests - len(set(failed_tests))
    failed_count = len(set(failed_tests))
    skipped_count = len(set(skipped_tests))
    print(f"Total Tests  : {total_tests}")
    print(f"Passed       : {passed_tests}")
    print(f"Failed       : {failed_count}")
    print(f"Skipped      : {skipped_count}")
    print(f"Success Rate : {(passed_tests / total_tests) * 100:.1f}%")

    if failed_count:
        print("\nFailed Test Cases:")
        for i, t in enumerate(sorted(set(failed_tests)), 1):
            print(f"  {i}. {t}")

    if skipped_count:
        print("\nSkipped Test Cases:")
        for i, t in enumerate(sorted(set(skipped_tests)), 1):
            print(f"  {i}. {t}")

    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
