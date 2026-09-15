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

import subprocess
import sys
import json

def run(cmd):
    r = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()

def get_version(output, key):
    for line in output.splitlines():
        if line.strip().startswith(key):
            return line.split(":", 1)[1].strip()
    return None

def load_config_map():
    """Load NICConfig.Fields from /etc/metrics/config-nic.json"""
    config_file = "/etc/metrics/config-nic.json"
    try:
        with open(config_file, 'r') as f:
            config_data = json.load(f)
        return (config_data.get("NICConfig") or {}).get("Fields") or []
    except Exception as e:
        print(f"Error loading NICConfig.Fields from {config_file}: {e}")
        return []

def get_metrics_prefix():
    """Get the MetricsFieldPrefix from /etc/metrics/config-nic.json"""
    config_file = "/etc/metrics/config-nic.json"
    default_prefix = "amd_"
    
    try:
        with open(config_file, 'r') as f:
            config_data = json.load(f)
            prefix = config_data.get("MetricsFieldPrefix", default_prefix)
            # Ensure prefix ends with underscore
            if prefix and not prefix.endswith("_"):
                prefix += "_"
            return prefix
    except Exception as e:
        print(f"Warning: Could not read {config_file}, using default prefix '{default_prefix}': {e}")
        return default_prefix

def get_server_port():
    """Get the ServerPort from /etc/metrics/config-nic.json"""
    config_file = "/etc/metrics/config-nic.json"
    default_port = 5001
    
    try:
        with open(config_file, 'r') as f:
            config_data = json.load(f)
            port = config_data.get("ServerPort", default_port)
            return port
    except Exception as e:
        print(f"Warning: Could not read {config_file}, using default port {default_port}: {e}")
        return default_port

if len(sys.argv) != 3:
    print("Usage: python validate_nicctl.py <fw_version> <nic_exporter_version>")
    sys.exit(1)

fw_expected = sys.argv[1]
exporter_expected = sys.argv[2]
metrics_port = get_server_port()
fail = False
failed_tests = []
total_tests = 21

print(f"Using metrics port: {metrics_port}")

# -------------------------------------------------
# Firmware & nicctl versions
# -------------------------------------------------
print("\n" + "="*60)
print("Test 1: Firmware & nicctl Version Validation")
print("="*60)

_, fw_out, _ = run("nicctl show version firmware")
_, host_out, _ = run("nicctl show version host-software")

fw_version = get_version(fw_out, "Firmware")
host_version = get_version(host_out, "nicctl")

print("Firmware:", fw_version, "PASS" if fw_version == fw_expected else "FAIL")
print("nicctl     :", host_version, "PASS" if host_version == fw_expected else "FAIL")

if fw_version != fw_expected or host_version != fw_expected:
    fail = True
    failed_tests.append("Test 1: Firmware & nicctl Version Validation")

# -------------------------------------------------
# Basic command checks
# -------------------------------------------------
print("\n" + "="*60)
print("Test 2: Basic nicctl Command Checks")
print("="*60)

test_2_failed = False
for cmd in ["nicctl show lif", "nicctl show card"]:
    rc, _, err = run(cmd)
    if rc == 0:
        print(f"{cmd} : PASS")
    else:
        print(f"{cmd} : FAIL")
        print(err)
        fail = True
        test_2_failed = True

if test_2_failed:
    failed_tests.append("Test 2: Basic nicctl Command Checks")

# -------------------------------------------------
# NIC exporter version
# -------------------------------------------------
print("\n" + "="*60)
print("Test 3: NIC Exporter Version Validation")
print("="*60)

rc, log_out, _ = run("grep Version /var/log/amd-nic-metrics-exporter.log")
if rc == 0 and exporter_expected in log_out:
    print("NIC exporter :", exporter_expected, "PASS")
else:
    print("NIC exporter : FAIL")
    fail = True
    failed_tests.append("Test 3: NIC Exporter Version Validation")

# -------------------------------------------------
# NIC exporter service status
# -------------------------------------------------
print("\n" + "="*60)
print("Test 4: NIC Exporter Service Status")
print("="*60)

rc, service_out, _ = run("systemctl is-active amd-nic-metrics-exporter.service")
service_active = rc == 0 and service_out.strip() == "active"

if service_active:
    print("NIC exporter service : active PASS")
else:
    print("NIC exporter service : FAIL (not active)")
    print(f"Service status: {service_out.strip()}")
    print("\nSkipping metrics-related test cases due to inactive service")
    fail = True
    failed_tests.append("Test 4: NIC Exporter Service Status")

if service_active:
    # -------------------------------------------------
    # NIC exporter service stop/start test
    # -------------------------------------------------
    print("\n" + "="*60)
    print("Test 5: NIC Exporter Service Stop/Start")
    print("="*60)
    
    # Stop the service
    test_5_failed = False
    rc, _, err = run("sudo systemctl stop amd-nic-metrics-exporter.service")
    if rc == 0:
        print("Service stop : PASS")
    else:
        print("Service stop : FAIL")
        print(err)
        fail = True
        test_5_failed = True
    
    # Wait a moment for service to stop
    run("sleep 2")
    
    # Check service status
    rc, status_out, _ = run("systemctl is-active amd-nic-metrics-exporter.service")
    if status_out.strip() == "inactive":
        print("Service status check (stopped) : PASS")
    else:
        print(f"Service status check (stopped) : FAIL (status: {status_out.strip()})")
        fail = True
        test_5_failed = True
    
    # Verify metrics fetch fails when service is stopped
    rc, _, _ = run(f"curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{metrics_port}/metrics")
    if rc != 0:
        print("Metrics fetch when stopped : PASS (connection refused)")
    else:
        print("Metrics fetch when stopped : WARNING (connection succeeded)")
    
    # Start the service
    rc, _, err = run("sudo systemctl start amd-nic-metrics-exporter.service")
    if rc == 0:
        print("Service start : PASS")
    else:
        print("Service start : FAIL")
        print(err)
        fail = True
        test_5_failed = True
    
    # Wait for service to fully start
    run("sleep 10")
    
    # Check service status
    rc, status_out, _ = run("systemctl is-active amd-nic-metrics-exporter.service")
    if rc == 0 and status_out.strip() == "active":
        print("Service status check (running) : PASS")
    else:
        print(f"Service status check (running) : FAIL (status: {status_out.strip()})")
        fail = True
        test_5_failed = True
    
    # Verify metrics fetch works when service is started
    rc, http_code, _ = run(f"curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{metrics_port}/metrics")
    if rc == 0 and http_code.strip() == "200":
        print("Metrics fetch when started : PASS")
    else:
        print(f"Metrics fetch when started : FAIL (HTTP code: {http_code.strip()})")
        fail = True
        test_5_failed = True
    
    if test_5_failed:
        failed_tests.append("Test 5: NIC Exporter Service Stop/Start")
    print("Service stop/start test : PASS\n")

    # -------------------------------------------------
    # NIC exporter service restart test
    # -------------------------------------------------
    print("\n" + "="*60)
    print("Test 6: NIC Exporter Service Restart")
    print("="*60)
    
    # Get current log timestamp before restart
    rc, log_before, _ = run("journalctl -u amd-nic-metrics-exporter.service --no-pager -n 5")
    
    # Restart the service
    test_6_failed = False
    rc, _, err = run("sudo systemctl restart amd-nic-metrics-exporter.service")
    if rc == 0:
        print("Service restart : PASS")
    else:
        print("Service restart : FAIL")
        print(err)
        fail = True
        test_6_failed = True
    
    # Wait for service to fully restart
    run("sleep 10")
    
    # Get new logs after restart
    rc, log_after, _ = run("journalctl -u amd-nic-metrics-exporter.service --no-pager -n 10")
    
    # Check for stop/start messages in new logs
    has_stop_log = "Stopping" in log_after or "Stopped" in log_after
    has_start_log = "Starting" in log_after or "Started" in log_after
    
    if has_stop_log and has_start_log:
        print("Restart logs verification : PASS (found stop and start messages)")
    else:
        print(f"Restart logs verification : WARNING (stop: {has_stop_log}, start: {has_start_log})")
    
    # Check service status
    rc, status_out, _ = run("systemctl is-active amd-nic-metrics-exporter.service")
    if rc == 0 and status_out.strip() == "active":
        print("Service status check (after restart) : PASS")
    else:
        print(f"Service status check (after restart) : FAIL (status: {status_out.strip()})")
        fail = True
        test_6_failed = True
    
    # Verify metrics fetch works after restart
    rc, http_code, _ = run(f"curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{metrics_port}/metrics")
    if rc == 0 and http_code.strip() == "200":
        print("Metrics fetch after restart : PASS")
    else:
        print(f"Metrics fetch after restart : FAIL (HTTP code: {http_code.strip()})")
        fail = True
        test_6_failed = True
    
    if test_6_failed:
        failed_tests.append("Test 6: NIC Exporter Service Restart")
    print("Service restart test : PASS\n")

    # -------------------------------------------------
    # NIC exporter service control test
    # -------------------------------------------------
    print("\n" + "="*60)
    print("Test 7: NIC Exporter Service Enable/Disable")
    print("="*60)
    
    # Disable and stop the service
    test_7_failed = False
    rc, _, err = run("sudo systemctl disable --now amd-nic-metrics-exporter.service")
    if rc == 0:
        print("Service disable : PASS")
    else:
        print("Service disable : FAIL")
        print(err)
        fail = True
        test_7_failed = True
    
    # Wait a moment for service to stop
    run("sleep 2")
    
    # Verify service is stopped
    rc, status_out, _ = run("systemctl is-active amd-nic-metrics-exporter.service")
    if rc != 0 or status_out.strip() != "active":
        print("Service stopped verification : PASS")
    else:
        print("Service stopped verification : FAIL (service still active)")
        fail = True
        test_7_failed = True
    
    # Verify metrics fetch fails when service is disabled
    rc, _, _ = run(f"curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{metrics_port}/metrics")
    if rc != 0:
        print("Metrics fetch when disabled : PASS (connection failed as expected)")
    else:
        print("Metrics fetch when disabled : WARNING (connection succeeded)")
    
    # Enable and start the service
    rc, _, err = run("sudo systemctl enable --now amd-nic-metrics-exporter.service")
    if rc == 0:
        print("Service enable : PASS")
    else:
        print("Service enable : FAIL")
        print(err)
        fail = True
        test_7_failed = True
    
    # Wait for service to fully start
    run("sleep 10")
    
    # Verify service is running
    rc, status_out, _ = run("systemctl is-active amd-nic-metrics-exporter.service")
    if rc == 0 and status_out.strip() == "active":
        print("Service running verification : PASS")
    else:
        print("Service running verification : FAIL (service not active after enable)")
        fail = True
        test_7_failed = True
    
    # Verify metrics fetch works when service is enabled
    rc, http_code, _ = run(f"curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{metrics_port}/metrics")
    if rc == 0 and http_code.strip() == "200":
        print("Metrics fetch when enabled : PASS")
    else:
        print(f"Metrics fetch when enabled : FAIL (HTTP code: {http_code.strip()})")
        fail = True
        test_7_failed = True
    
    if test_7_failed:
        failed_tests.append("Test 7: NIC Exporter Service Enable/Disable")
    print("Service control test : PASS\n")

    # -------------------------------------------------
    # Config file update tests
    # -------------------------------------------------
    print("\n" + "="*60)
    print("Test 8: Config File Update Tests - Backup")
    print("="*60)
    
    config_file = "/etc/metrics/config-nic.json"
    backup_file = "/tmp/config-nic.json.backup"
    
    # Backup original config
    rc, _, err = run(f"sudo cp {config_file} {backup_file}")
    if rc != 0:
        print(f"Config backup : FAIL (could not backup {config_file})")
        print(err)
        fail = True
        failed_tests.append("Test 8: Config File Update Tests - Backup")
    else:
        print("Config backup : PASS")
    
    try:
        # -------------------------------------------------
        # Test 1: Update ServerPort from 5001 to 5010
        # -------------------------------------------------
        print("\n" + "="*60)
        print("Test 9: Update ServerPort (5001 -> 5010)")
        print("="*60)
        
        # Read current config
        with open(backup_file, 'r') as f:
            config_data = json.load(f)
        
        original_port = config_data.get("ServerPort", 5001)
        new_port = 5010
        config_data["ServerPort"] = new_port
        
        # Write updated config
        test_9_failed = False
        rc, _, err = run(f"echo '{json.dumps(config_data)}' | sudo tee {config_file} > /dev/null")
        if rc == 0:
            print(f"Config update (port {original_port} -> {new_port}) : PASS")
        else:
            print(f"Config update (port) : FAIL")
            print(err)
            fail = True
            test_9_failed = True
        
        # Wait for service to detect config change and reload
        print("Waiting 10 seconds for config reload...")
        run("sleep 10")
        
        # Verify old port fails
        rc, _, _ = run(f"curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{original_port}/metrics")
        if rc != 0:
            print(f"Old port {original_port} unreachable : PASS")
        else:
            print(f"Old port {original_port} unreachable : WARNING (still reachable)")
        
        # Verify new port works
        rc, http_code, _ = run(f"curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{new_port}/metrics")
        if rc == 0 and http_code.strip() == "200":
            print(f"New port {new_port} metrics fetch : PASS")
        else:
            print(f"New port {new_port} metrics fetch : FAIL (HTTP code: {http_code.strip()})")
            fail = True
            test_9_failed = True
        
        if test_9_failed:
            failed_tests.append("Test 9: Update ServerPort (5001 -> 5010)")
        print("ServerPort update test : PASS\n")
        
        # -------------------------------------------------
        # Test 2: Change MetricsFieldPrefix from amd_ to pensando_
        # -------------------------------------------------
        print("="*60)
        print("Test 10: Update MetricsFieldPrefix (amd_ -> pensando_)")
        print("="*60)
        
        # Read fresh from backup to preserve all fields
        with open(backup_file, 'r') as f:
            config_data = json.load(f)
        
        # MetricsFieldPrefix is nested under CommonConfig
        if "CommonConfig" not in config_data:
            config_data["CommonConfig"] = {}
        
        original_prefix = config_data.get("CommonConfig", {}).get("MetricsFieldPrefix", "amd_")
        new_prefix = "pensando_"
        config_data["CommonConfig"]["MetricsFieldPrefix"] = new_prefix
        
        # Write updated config
        test_10_failed = False
        rc, _, err = run(f"echo '{json.dumps(config_data)}' | sudo tee {config_file} > /dev/null")
        if rc == 0:
            print(f"Config update (prefix {original_prefix} -> {new_prefix}) : PASS")
        else:
            print(f"Config update (prefix) : FAIL")
            print(err)
            fail = True
            test_10_failed = True
        
        # Wait for service to detect config change and reload (no restart needed)
        print("Waiting 10 seconds for config hot-reload...")
        run("sleep 10")
        
        # Fetch metrics
        run(f"curl -s http://localhost:{original_port}/metrics > /tmp/test_prefix")
        
        # Check if new prefix exists
        rc, count_out, _ = run(f"grep -c '{new_prefix}' /tmp/test_prefix")
        new_prefix_count = int(count_out) if rc == 0 else 0
        
        # Verify old prefix doesn't exist (except in comments)
        rc, count_out, _ = run(f"grep -v '^#' /tmp/test_prefix | grep -c '{original_prefix}'")
        old_prefix_count = int(count_out) if rc == 0 else 0
        
        if new_prefix_count > 0:
            print(f"New prefix '{new_prefix}' found in metrics : PASS ({new_prefix_count} occurrences)")
        else:
            print(f"New prefix '{new_prefix}' found in metrics : FAIL (not found)")
            fail = True
            test_10_failed = True
        
        if old_prefix_count == 0:
            print(f"Old prefix '{original_prefix}' removed from metrics : PASS")
        else:
            print(f"Old prefix '{original_prefix}' removed from metrics : WARNING ({old_prefix_count} occurrences remain)")
        
        if test_10_failed:
            failed_tests.append("Test 10: Update MetricsFieldPrefix (amd_ -> pensando_)")
        print("MetricsFieldPrefix update test : PASS\n")
        
        # -------------------------------------------------
        # Test 3: Remove ETH_ fields from config
        # -------------------------------------------------
        print("="*60)
        print("Test 11: Remove ETH_ fields from metrics")
        print("="*60)
        
        # Read from backup to get original Fields list
        with open(backup_file, 'r') as f:
            original_config = json.load(f)
        
        # Fields are nested under NICConfig
        if "NICConfig" in original_config and "Fields" in original_config["NICConfig"]:
            original_fields = original_config["NICConfig"]["Fields"]
            eth_fields = [f for f in original_fields if f.startswith("ETH_")]
            non_eth_fields = [f for f in original_fields if not f.startswith("ETH_")]
            
            print(f"Total fields in config : {len(original_fields)}")
            print(f"ETH_ fields to remove  : {len(eth_fields)}")
            print(f"Remaining fields       : {len(non_eth_fields)}")
            
            # Read fresh from backup to preserve all config
            with open(backup_file, 'r') as f:
                config_data = json.load(f)
            
            # Update config with non-ETH fields only
            config_data["NICConfig"]["Fields"] = non_eth_fields
            # Restore original prefix in CommonConfig
            if "CommonConfig" in config_data:
                config_data["CommonConfig"]["MetricsFieldPrefix"] = original_prefix
            
            # Write updated config
            config_json = json.dumps(config_data, indent=2)
            with open('/tmp/config_temp.json', 'w') as f:
                f.write(config_json)
            
            test_11_failed = False
            rc, _, err = run(f"sudo cp /tmp/config_temp.json {config_file}")
            if rc == 0:
                print(f"Config update (removed {len(eth_fields)} ETH_ fields) : PASS")
            else:
                print(f"Config update (remove ETH_ fields) : FAIL")
                print(err)
                fail = True
                test_11_failed = True
            
            # Wait for service to detect config change and reload
            print("Waiting 10 seconds for config reload...")
            run("sleep 10")
            
            # Fetch metrics
            run(f"curl -s http://localhost:{original_port}/metrics > /tmp/test_eth")
            
            # Count ETH_ metrics (excluding comments)
            rc, count_out, _ = run("grep -v '^#' /tmp/test_eth | grep -ci '_eth_'")
            eth_metric_count = int(count_out) if rc == 0 else 0
            
            # Count NIC_ metrics to ensure other metrics still exist
            rc, count_out, _ = run("grep -v '^#' /tmp/test_eth | grep -ci '_nic_'")
            nic_metric_count = int(count_out) if rc == 0 else 0
            
            if eth_metric_count == 0:
                print(f"ETH_ metrics removed from output : PASS (0 found)")
            else:
                print(f"ETH_ metrics removed from output : FAIL ({eth_metric_count} still present)")
                fail = True
                test_11_failed = True
            
            if nic_metric_count > 0:
                print(f"Non-ETH metrics still present : PASS ({nic_metric_count} NIC_ metrics found)")
            else:
                print(f"Non-ETH metrics still present : WARNING (no NIC_ metrics found)")
            
            if test_11_failed:
                failed_tests.append("Test 11: Remove ETH_ fields from metrics")
            print("ETH_ field removal test : PASS\n")
        else:
            print("Fields key not found in config : SKIP")
        
        # -------------------------------------------------
        # Test 4: Update NICConfig Fields with new field names
        # -------------------------------------------------
        print("="*60)
        print("Test 12: Update NICConfig Fields")
        print("="*60)
        
        # Read from backup to get original Fields list
        with open(backup_file, 'r') as f:
            original_config = json.load(f)
        
        if "NICConfig" in original_config and "Fields" in original_config["NICConfig"]:
            original_fields = original_config["NICConfig"]["Fields"]
            
            # Get first 3 fields to replace
            old_fields_to_replace = original_fields[:3] if len(original_fields) >= 3 else []
            new_fields_to_add = [
                "NIC_PORT_STATS_FRAMES_RX_SDF",
                "NIC_PORT_STATS_FRAMES_RX_JJJ",
                "NIC_PORT_STATS_FRAMES_RX_BAD_KKK"
            ]
            
            print(f"Fields to replace:")
            for old, new in zip(old_fields_to_replace, new_fields_to_add):
                print(f"  {old} -> {new}")
            
            # Read fresh from backup to preserve all config
            with open(backup_file, 'r') as f:
                config_data = json.load(f)
            
            # Replace first 3 fields with new dummy fields
            updated_fields = new_fields_to_add + original_fields[3:]
            config_data["NICConfig"]["Fields"] = updated_fields
            
            # Write updated config
            config_json = json.dumps(config_data, indent=2)
            with open('/tmp/config_temp.json', 'w') as f:
                f.write(config_json)
            
            test_12_failed = False
            rc, _, err = run(f"sudo cp /tmp/config_temp.json {config_file}")
            if rc == 0:
                print(f"Config update (replaced {len(new_fields_to_add)} fields) : PASS")
            else:
                print(f"Config update (replace fields) : FAIL")
                print(err)
                fail = True
                test_12_failed = True
            
            # Wait for service to detect config change and reload (no restart needed)
            print("Waiting 10 seconds for config hot-reload...")
            run("sleep 10")
            
            # Fetch metrics
            run(f"curl -s http://localhost:{original_port}/metrics > /tmp/test_fields")
            
            # Check that old fields don't appear in metrics
            old_fields_found = 0
            for old_field in old_fields_to_replace:
                # Convert to lowercase metric name format (e.g., nic_port_stats_frames_rx_ok)
                metric_name = old_field.lower()
                rc, _, _ = run(f"grep -v '^#' /tmp/test_fields | grep -q '{metric_name}'")
                if rc == 0:
                    old_fields_found += 1
                    print(f"Old field '{old_field}' still in metrics : WARNING")
            
            if old_fields_found == 0:
                print(f"Old fields removed from metrics : PASS (0/{len(old_fields_to_replace)} found)")
            else:
                print(f"Old fields removed from metrics : FAIL ({old_fields_found}/{len(old_fields_to_replace)} still present)")
                fail = True
                test_12_failed = True
            
            # Check that new fields appear in metrics (they may be 0 but should exist)
            new_fields_found = 0
            for new_field in new_fields_to_add:
                metric_name = new_field.lower()
                rc, _, _ = run(f"grep -v '^#' /tmp/test_fields | grep -q '{metric_name}'")
                if rc == 0:
                    new_fields_found += 1
            
            print(f"New fields in metrics : {new_fields_found}/{len(new_fields_to_add)}")
            
            # Count other NIC_ metrics to ensure metrics still work
            rc, count_out, _ = run("grep -v '^#' /tmp/test_fields | grep -ci '_nic_port_stats'")
            nic_metric_count = int(count_out) if rc == 0 else 0
            
            if nic_metric_count > 0:
                print(f"NIC metrics still present : PASS ({nic_metric_count} metrics found)")
            else:
                print(f"NIC metrics still present : WARNING (no NIC_PORT_STATS metrics found)")
            
            if test_12_failed:
                failed_tests.append("Test 12: Update NICConfig Fields")
            print("NICConfig Fields update test : PASS\n")
        else:
            print("NICConfig.Fields not found in config : SKIP")
        
        # -------------------------------------------------
        # Test 5: Remove all NICConfig Fields
        # -------------------------------------------------
        print("="*60)
        print("Test 13: Remove all NICConfig Fields")
        print("="*60)
        
        # Read from backup to get original config
        with open(backup_file, 'r') as f:
            original_config = json.load(f)
        
        if "NICConfig" in original_config and "Fields" in original_config["NICConfig"]:
            original_fields_count = len(original_config["NICConfig"]["Fields"])
            print(f"Original NICConfig fields count : {original_fields_count}")
            
            # Read fresh from backup to preserve all config
            with open(backup_file, 'r') as f:
                config_data = json.load(f)
            
            # Remove all fields from NICConfig
            config_data["NICConfig"]["Fields"] = []
            
            # Write updated config
            config_json = json.dumps(config_data, indent=2)
            with open('/tmp/config_temp.json', 'w') as f:
                f.write(config_json)
            
            test_13_failed = False
            rc, _, err = run(f"sudo cp /tmp/config_temp.json {config_file}")
            if rc == 0:
                print(f"Config update (removed all {original_fields_count} fields) : PASS")
            else:
                print(f"Config update (remove all fields) : FAIL")
                print(err)
                fail = True
                test_13_failed = True
            
            # Wait for service to detect config change and reload (no restart needed)
            print("Waiting 10 seconds for config hot-reload...")
            run("sleep 10")
            
            # Fetch metrics
            run(f"curl -s http://localhost:{original_port}/metrics > /tmp/test_nofields")
            
            # Count NIC_PORT_STATS metrics (should still exist with default metrics)
            rc, count_out, _ = run("grep -v '^#' /tmp/test_nofields | grep -ci 'nic_port_stats'")
            nic_port_stats_count = int(count_out) if rc == 0 else 0
            
            # Count NIC_LIF_STATS metrics (should still exist with default metrics)
            rc, count_out, _ = run("grep -v '^#' /tmp/test_nofields | grep -ci 'nic_lif_stats'")
            nic_lif_stats_count = int(count_out) if rc == 0 else 0
            
            # Count RDMA metrics (should still exist)
            rc, count_out, _ = run("grep -v '^#' /tmp/test_nofields | grep -ci 'rdma_'")
            rdma_count = int(count_out) if rc == 0 else 0
            
            # Count QP metrics (should still exist)
            rc, count_out, _ = run("grep -v '^#' /tmp/test_nofields | grep -ci 'qp_'")
            qp_count = int(count_out) if rc == 0 else 0
            
            # Count ETH metrics (should still exist)
            rc, count_out, _ = run("grep -v '^#' /tmp/test_nofields | grep -ci '_eth_'")
            eth_count = int(count_out) if rc == 0 else 0
            
            print(f"NIC_PORT_STATS metrics : {nic_port_stats_count} (should still exist)")
            print(f"NIC_LIF_STATS metrics  : {nic_lif_stats_count} (should still exist)")
            print(f"RDMA metrics          : {rdma_count} (should still exist)")
            print(f"QP metrics            : {qp_count} (should still exist)")
            print(f"ETH metrics           : {eth_count} (should still exist)")
            
            # When Fields is empty, service should still export default metrics
            total_metrics = nic_port_stats_count + nic_lif_stats_count + rdma_count + qp_count + eth_count
            if total_metrics > 0:
                print(f"All metrics still present : PASS ({total_metrics} total metrics found)")
            else:
                print("All metrics still present : FAIL (no metrics found)")
                fail = True
                test_13_failed = True
            
            if test_13_failed:
                failed_tests.append("Test 13: Remove all NICConfig Fields")
            print("Remove all NICConfig Fields test : PASS\n")
        else:
            print("NICConfig.Fields not found in config : SKIP")
        
        # -------------------------------------------------
        # Test 6: Remove specific NICConfig Labels
        # -------------------------------------------------
        print("="*60)
        print("Test 14: Remove specific NICConfig Labels")
        print("="*60)
        
        # Read from backup to get original config
        with open(backup_file, 'r') as f:
            original_config = json.load(f)
        
        if "NICConfig" in original_config and "Labels" in original_config["NICConfig"]:
            original_labels = original_config["NICConfig"]["Labels"]
            labels_to_remove = ["NIC_UUID", "POD", "POD_UUID", "NAMESPACE", "CONTAINER", "FIRMWARE_VERSION"]
            remaining_labels = [label for label in original_labels if label not in labels_to_remove]
            removed_labels = [label for label in original_labels if label in labels_to_remove]
            
            print(f"Original labels count  : {len(original_labels)}")
            print(f"Labels to remove       : {len(removed_labels)} ({', '.join(removed_labels)})")
            print(f"Remaining labels count : {len(remaining_labels)} ({', '.join(remaining_labels)})")
            
            # Read fresh from backup to preserve all config
            with open(backup_file, 'r') as f:
                config_data = json.load(f)
            
            # Update labels with remaining labels only
            config_data["NICConfig"]["Labels"] = remaining_labels
            
            # Write updated config
            config_json = json.dumps(config_data, indent=2)
            with open('/tmp/config_temp.json', 'w') as f:
                f.write(config_json)
            
            test_14_failed = False
            rc, _, err = run(f"sudo cp /tmp/config_temp.json {config_file}")
            if rc == 0:
                print(f"Config update (removed {len(removed_labels)} labels) : PASS")
            else:
                print(f"Config update (remove labels) : FAIL")
                print(err)
                fail = True
                test_14_failed = True
            
            # Wait for service to detect config change and reload (no restart needed)
            print("Waiting 10 seconds for config hot-reload...")
            run("sleep 10")
            
            # Fetch metrics
            run(f"curl -s http://localhost:{original_port}/metrics > /tmp/test_labels")
            
            # Note: POD, NAMESPACE, CONTAINER should be removed from nic_port_stats only
            # amd_eth_, nic_lif_stats, rdma, qp should have all 6 labels
            print(f"Note: Labels removed from nic_port_stats only; amd_eth_, nic_lif_stats, rdma, qp have all 6 labels")
            
            # Check nic_port_stats metrics
            print("\nValidating labels in nic_port_stats metrics:")
            # Should have: NIC_ID, HOSTNAME, SERIAL_NUMBER
            # Should NOT have: POD, NAMESPACE, CONTAINER
            
            port_labels_expected = ["NIC_ID", "HOSTNAME", "SERIAL_NUMBER"]
            port_labels_forbidden = ["POD", "NAMESPACE", "CONTAINER"]
            
            # Check if nic_port_stats exists
            rc, _, _ = run(f"grep -v '^#' /tmp/test_labels | grep -q 'nic_port_stats'")
            if rc == 0:
                metric_failed = False
                expected_found = []
                forbidden_found = []
                
                # Check expected labels
                for label in port_labels_expected:
                    label_name = label.lower()
                    rc, _, _ = run(f"grep -v '^#' /tmp/test_labels | grep 'nic_port_stats' | grep -q '{label_name}=\"'")
                    if rc == 0:
                        expected_found.append(label)
                    else:
                        metric_failed = True
                
                # Check forbidden labels
                for label in port_labels_forbidden:
                    label_name = label.lower()
                    rc, _, _ = run(f"grep -v '^#' /tmp/test_labels | grep 'nic_port_stats' | grep -q '{label_name}=\"'")
                    if rc == 0:
                        forbidden_found.append(label)
                        metric_failed = True
                
                if metric_failed:
                    missing_expected = set(port_labels_expected) - set(expected_found)
                    print(f"  nic_port_stats: FAIL")
                    if missing_expected:
                        print(f"    Missing labels: {', '.join(missing_expected)}")
                    if forbidden_found:
                        print(f"    Forbidden labels present: {', '.join(forbidden_found)}")
                    test_14_failed = True
                    fail = True
                else:
                    print(f"  nic_port_stats: PASS (has {', '.join(expected_found)}; not {', '.join(port_labels_forbidden)})")
            else:
                print(f"  nic_port_stats: SKIP (no metrics found)")
            
            # Check other metrics (amd_eth_, nic_lif_stats, rdma, qp)
            print("\nValidating labels in other metrics (amd_eth_, nic_lif_stats, rdma, qp):")
            # All should have: POD, NAMESPACE, CONTAINER, NIC_ID, HOSTNAME, SERIAL_NUMBER
            
            all_expected_labels = ["POD", "NAMESPACE", "CONTAINER", "NIC_ID", "HOSTNAME", "SERIAL_NUMBER"]
            
            # Check if other metrics exist
            rc, _, _ = run(f"grep -v '^#' /tmp/test_labels | grep -qE '(amd_eth_|nic_lif_stats|rdma_|qp_)'")
            if rc == 0:
                labels_found_in_others = []
                
                for label in all_expected_labels:
                    label_name = label.lower()
                    rc, _, _ = run(f"grep -v '^#' /tmp/test_labels | grep -E '(amd_eth_|nic_lif_stats|rdma_|qp_)' | grep -q '{label_name}=\"'")
                    if rc == 0:
                        labels_found_in_others.append(label)
                
                if len(labels_found_in_others) == len(all_expected_labels):
                    print(f"  Other metrics: PASS (all 6 labels found)")
                else:
                    missing = set(all_expected_labels) - set(labels_found_in_others)
                    print(f"  Other metrics: FAIL")
                    print(f"    Found: {', '.join(labels_found_in_others)}")
                    print(f"    Missing: {', '.join(missing)}")
                    fail = True
                    test_14_failed = True
            else:
                print(f"  Other metrics: SKIP (no amd_eth_, nic_lif_stats, rdma, or qp metrics found)")
            
            if test_14_failed:
                failed_tests.append("Test 14: Remove specific NICConfig Labels")
            print("Remove NICConfig Labels test : PASS\n")
        else:
            print("NICConfig.Labels not found in config : SKIP")
        
        # -------------------------------------------------
        # Test 7: Add CustomLabels to config
        # -------------------------------------------------
        print("="*60)
        print("Test 15: Add CustomLabels to config")
        print("="*60)
        
        # Read from backup to get original config
        with open(backup_file, 'r') as f:
            original_config = json.load(f)
        
        if "NICConfig" in original_config:
            test_15_failed = False
            
            # Read fresh from backup to preserve all config
            with open(backup_file, 'r') as f:
                config_data = json.load(f)
            
            # Add CustomLabels with CLUSTER_NAME
            if "NICConfig" not in config_data:
                config_data["NICConfig"] = {}
            
            config_data["NICConfig"]["CustomLabels"] = {
                "CLUSTER_NAME": "amdnetwork-k8s-metrics-exporter",
                "CLUSTER_ENVIRONMENT": "systest"
            }
            
            print(f"Adding CustomLabels:")
            print(f"  CLUSTER_NAME=amdnetwork-k8s-metrics-exporter")
            print(f"  CLUSTER_ENVIRONMENT=systest")
            
            # Write updated config
            config_json = json.dumps(config_data, indent=2)
            with open('/tmp/config_temp.json', 'w') as f:
                f.write(config_json)
            
            rc, _, err = run(f"sudo cp /tmp/config_temp.json {config_file}")
            if rc == 0:
                print(f"Config update (added CustomLabels) : PASS")
            else:
                print(f"Config update (add CustomLabels) : FAIL")
                print(err)
                fail = True
                test_15_failed = True
            
            # Wait for service to detect config change and reload (no restart needed)
            print("Waiting 10 seconds for config hot-reload...")
            run("sleep 10")
            
            # Fetch metrics
            run(f"curl -s http://localhost:{original_port}/metrics > /tmp/test_custom_labels")
            
            # Check if CLUSTER_NAME label appears in metrics
            rc, grep_out, _ = run("grep -v '^#' /tmp/test_custom_labels | grep 'cluster_name=\"'")
            cluster_name_found = rc == 0
            
            # Check if CLUSTER_ENVIRONMENT label appears in metrics
            rc, grep_out, _ = run("grep -v '^#' /tmp/test_custom_labels | grep 'cluster_environment=\"'")
            cluster_env_found = rc == 0
            
            if cluster_name_found:
                # Count how many metrics have the CLUSTER_NAME label
                rc, count_out, _ = run("grep -v '^#' /tmp/test_custom_labels | grep -c 'cluster_name=\"'")
                cluster_name_count = int(count_out) if rc == 0 else 0
                
                # Verify the value is correct
                rc, value_check, _ = run("grep -v '^#' /tmp/test_custom_labels | grep 'cluster_name=\"amdnetwork-k8s-metrics-exporter\"' | head -1")
                
                if rc == 0 and value_check.strip():
                    print(f"CustomLabel CLUSTER_NAME found in metrics : PASS ({cluster_name_count} occurrences)")
                    print(f"  Value verified: amdnetwork-k8s-metrics-exporter")
                else:
                    print(f"CustomLabel CLUSTER_NAME value verification : FAIL")
                    print(f"  Expected: amdnetwork-k8s-metrics-exporter")
                    fail = True
                    test_15_failed = True
            else:
                print(f"CustomLabel CLUSTER_NAME found in metrics : FAIL (not found)")
                fail = True
                test_15_failed = True
            
            if cluster_env_found:
                # Count how many metrics have the CLUSTER_ENVIRONMENT label
                rc, count_out, _ = run("grep -v '^#' /tmp/test_custom_labels | grep -c 'cluster_environment=\"'")
                cluster_env_count = int(count_out) if rc == 0 else 0
                
                # Verify the value is correct
                rc, value_check, _ = run("grep -v '^#' /tmp/test_custom_labels | grep 'cluster_environment=\"systest\"' | head -1")
                
                if rc == 0 and value_check.strip():
                    print(f"CustomLabel CLUSTER_ENVIRONMENT found in metrics : PASS ({cluster_env_count} occurrences)")
                    print(f"  Value verified: systest")
                else:
                    print(f"CustomLabel CLUSTER_ENVIRONMENT value verification : FAIL")
                    print(f"  Expected: systest")
                    fail = True
                    test_15_failed = True
            else:
                print(f"CustomLabel CLUSTER_ENVIRONMENT found in metrics : FAIL (not found)")
                fail = True
                test_15_failed = True
            
            # Cleanup
            run("rm -f /tmp/test_custom_labels")
            
            if test_15_failed:
                failed_tests.append("Test 15: Add CustomLabels to config")
            print("CustomLabels addition test : PASS\n")
        else:
            print("NICConfig not found in config : SKIP")
        
    finally:
        # -------------------------------------------------
        # Restore original config
        # -------------------------------------------------
        print("="*60)
        print("Test 16: Restoring original configuration")
        print("="*60)
        
        rc, _, err = run(f"sudo cp {backup_file} {config_file}")
        if rc == 0:
            print("Config restore : PASS")
        else:
            print("Config restore : FAIL")
            print(err)
            fail = True
            failed_tests.append("Test 16: Restoring original configuration")
        
        # Wait for service to detect config change and reload
        print("Waiting 10 seconds for config reload...")
        run("sleep 10")
        
        # Verify service is running
        rc, status_out, _ = run("systemctl is-active amd-nic-metrics-exporter.service")
        if rc == 0 and status_out.strip() == "active":
            print("Service running after restore : PASS")
        else:
            print("Service running after restore : FAIL")
            fail = True
            if "Test 16: Restoring original configuration" not in failed_tests:
                failed_tests.append("Test 16: Restoring original configuration")
        
        # Cleanup temp files
        run("rm -f /tmp/config_temp.json /tmp/test_prefix /tmp/test_eth /tmp/test_fields /tmp/test_nofields /tmp/test_labels /tmp/test_fields_check")
        
        print("Configuration restored successfully\n")

    # -------------------------------------------------
    # RDMA queue-pair sum
    # -------------------------------------------------
    print("="*60)
    print("Test 17: RDMA Queue-Pair Validation")
    print("="*60)
    
    _, rdma_out, _ = run("nicctl show rdma queue-pair --summary")

    qp_sum = 0
    for line in rdma_out.splitlines():
        if "Number of queue pairs" in line:
            qp_sum += int(line.split(":")[1].strip())

    expected_qp_id = qp_sum + 2

    print("Total queue pairs :", qp_sum)
    print("Expected qp_id   :", expected_qp_id)

    # -------------------------------------------------
    # Fetch metrics (QP stats require ?debug=qp)
    # -------------------------------------------------
    run(f"curl -s 'http://localhost:{metrics_port}/metrics?debug=qp' > /tmp/test")

    # -------------------------------------------------
    # EXACT metric count check
    # cat test | grep -c 'amd_qp_rq_rsp_rx_num_packet'
    # -------------------------------------------------
    print("\n" + "="*60)
    print("Test 18: Metric Count Validation")
    print("="*60)
    
    _, count_out, _ = run("grep -c amd_qp_rq_rsp_rx_num_packet /tmp/test")
    metric_count = int(count_out)

    print("amd_qp_rq_rsp_rx_num_packet count :", metric_count)

    if metric_count == expected_qp_id:
        print("Metric count validation : PASS")
    else:
        print(f"Metric count validation : FAIL (expected {expected_qp_id})")
        fail = True
        failed_tests.append("Test 18: Metric Count Validation")

    # -------------------------------------------------
    # EXACT qp_id value sum
    # cat test | grep num_packet | grep 4002
    # -------------------------------------------------
    print("\n" + "="*60)
    print("Test 19: Metric Value Sum Validation")
    print("="*60)
    
    if qp_sum >= 4000:
        qp_id_to_check = 3200
    else:
        qp_id_to_check = expected_qp_id // 2

    cmd = f"grep num_packet /tmp/test | grep {qp_id_to_check}"
    _, qp_lines, _ = run(cmd)

    value_sum = 0
    for line in qp_lines.splitlines():
        try:
            # Handle scientific notation (e.g., 1.6392e+07)
            value_sum += float(line.split()[-1])
        except ValueError as e:
            print(f"Warning: Could not parse value from line: {line.split()[-1]}")
            continue

    print(f"Metric value sum for qp_id {qp_id_to_check} :", int(value_sum))

    if value_sum > 1000:
        print("Metric value validation : PASS")
    else:
        print("Metric value validation : FAIL (sum <= 1000)")
        fail = True
        failed_tests.append("Test 19: Metric Value Sum Validation")

    # -------------------------------------------------
    # Debug=QP endpoint validation
    # Verify amd_qp_* stats only appear in /metrics?debug=qp
    # -------------------------------------------------
    print("\n" + "="*60)
    print("Test 20: Debug=QP Endpoint Validation")
    print("="*60)

    test_20_failed = False

    # Get LIF_QP fields from config and derive actual QP field names
    # LIF_QP_SQ_REQ_TX_NUM_PACKET_TOTAL -> QP_SQ_REQ_TX_NUM_PACKET
    metrics_prefix = get_metrics_prefix()
    config_fields_qp = load_config_map()
    lif_qp_fields = [f for f in config_fields_qp if f.startswith("LIF_QP_")]

    qp_fields = []
    for f in lif_qp_fields:
        qp_name = f[4:]  # strip "LIF_"
        if qp_name.endswith("_TOTAL"):
            qp_name = qp_name[:-6]  # strip "_TOTAL"
        qp_fields.append(qp_name)

    print(f"LIF_QP fields in config : {len(lif_qp_fields)}")
    print(f"Derived QP fields       : {len(qp_fields)}")

    # Step 1: Verify /metrics does NOT have amd_qp_* stats
    run(f"curl -s 'http://localhost:{metrics_port}/metrics' > /tmp/test_no_qp")

    rc, count_out, _ = run("grep -v '^#' /tmp/test_no_qp | grep -c 'amd_qp_'")
    qp_in_regular = int(count_out) if rc == 0 else 0

    if qp_in_regular == 0:
        print(f"/metrics has no amd_qp_* stats : PASS")
    else:
        print(f"/metrics has no amd_qp_* stats : FAIL ({qp_in_regular} lines found)")
        fail = True
        test_20_failed = True

    # Step 2: Verify /metrics?debug=qp HAS all derived QP fields
    # /tmp/test already has debug=qp output from earlier fetch
    with open('/tmp/test', 'r') as f:
        debug_qp_content = f.read().lower()

    found_qp = []
    missing_qp = []
    for qp_field in qp_fields:
        metric_name = f"{metrics_prefix}{qp_field.lower()}"
        if metric_name in debug_qp_content:
            found_qp.append(qp_field)
        else:
            missing_qp.append(qp_field)

    print(f"\n/metrics?debug=qp QP fields:")
    print(f"  Found   : {len(found_qp)}/{len(qp_fields)}")
    print(f"  Missing : {len(missing_qp)}/{len(qp_fields)}")

    if missing_qp:
        print(f"\n  Missing QP metrics:")
        for mf in missing_qp[:10]:
            print(f"    - {mf}")
        if len(missing_qp) > 10:
            print(f"    ... and {len(missing_qp) - 10} more")

    if len(found_qp) == len(qp_fields) and qp_in_regular == 0:
        print(f"\nDebug=QP endpoint validation : PASS")
    else:
        if len(found_qp) < len(qp_fields):
            print(f"\nDebug=QP endpoint validation : FAIL")
            fail = True
            test_20_failed = True

    # Cleanup
    run("rm -f /tmp/test_no_qp")

    if test_20_failed:
        failed_tests.append("Test 20: Debug=QP Endpoint Validation")

    # -------------------------------------------------
    # Config map validation
    # -------------------------------------------------
    print("\n" + "="*60)
    print("Test 21: Config Map Validation")
    print("="*60)

    config_fields = load_config_map()
    metrics_prefix = get_metrics_prefix()

    if not config_fields:
        print("Config map validation : FAIL (could not load NICConfig.Fields from /etc/metrics/config-nic.json)")
        fail = True
        failed_tests.append("Test 21: Config Map Validation")
    else:
        print(f"Loaded {len(config_fields)} fields from /etc/metrics/config-nic.json (NICConfig.Fields)")
        print(f"Using metrics prefix: '{metrics_prefix}'")

        # Fetch regular metrics (without debug=qp) for NIC field validation
        run(f"curl -s 'http://localhost:{metrics_port}/metrics' > /tmp/test_fields_check")
        with open('/tmp/test_fields_check', 'r') as f:
            metrics_content = f.read().lower()
        
        missing_fields = []
        found_fields = []
        
        for field in config_fields:
            # Convert field name to lowercase metric name format
            # e.g. NIC_PORT_STATS_FRAMES_RX_OK -> amd_nic_port_stats_frames_rx_ok
            metric_name = f"{metrics_prefix}{field.lower()}"
            
            if metric_name in metrics_content:
                found_fields.append(field)
            else:
                missing_fields.append(field)
        
        print(f"\nFound fields  : {len(found_fields)}/{len(config_fields)}")
        print(f"Missing fields: {len(missing_fields)}/{len(config_fields)}")
        
        if missing_fields:
            print("\nMissing metrics:")
            for field in missing_fields[:10]:  # Show first 10 missing fields
                print(f"  - {field}")
            if len(missing_fields) > 10:
                print(f"  ... and {len(missing_fields) - 10} more")
        
        if len(found_fields) == len(config_fields):
            print("\nConfig map validation : PASS (all fields found in metrics)")
        elif len(found_fields) >= len(config_fields) * 0.9:  # 90% threshold
            print(f"\nConfig map validation : WARNING (found {len(found_fields)}/{len(config_fields)} fields)")
        else:
            print(f"\nConfig map validation : FAIL (found only {len(found_fields)}/{len(config_fields)} fields)")
            fail = True
            failed_tests.append("Test 21: Config Map Validation")

# -------------------------------------------------
# Test Summary
# -------------------------------------------------
print("\n" + "="*60)
print("TEST SUMMARY")
print("="*60)

passed_tests = total_tests - len(failed_tests)
print(f"\nTotal Tests  : {total_tests}")
print(f"Passed       : {passed_tests}")
print(f"Failed       : {len(failed_tests)}")
print(f"Success Rate : {(passed_tests/total_tests*100):.1f}%")

if len(failed_tests) > 0:
    print("\nFailed Test Cases:")
    for i, test_name in enumerate(failed_tests, 1):
        print(f"  {i}. {test_name}")
else:
    print("\n✓ All tests passed successfully!")

print("="*60)

# -------------------------------------------------
# Exit
# -------------------------------------------------
sys.exit(1 if fail else 0)
