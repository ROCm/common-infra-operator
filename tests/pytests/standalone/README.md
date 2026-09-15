# Standalone Scripts

This directory contains standalone scripts for AMD network components that run directly on host systems.

## upgrade_operands.py

Standalone script to upgrade the AMD Network Operator and its operands. Runs directly on the master node (no SSH). Supports two modes: `main` (latest main-* builds) and `throttle` (latest v1.2.0-* builds).

### Prerequisites

- Python 3.6+ (no pip dependencies)
- `kubectl` CLI available and configured (via `~/.kube/config` or `KUBECONFIG` env var)
- `helm` CLI available
- `docker` and `sudo` available (for CNI tag discovery via skopeo)
- `curl` CLI available
- Network access to `example.com` and `docker.io`

### Usage

```bash
python3 upgrade_operands.py main              # upgrade to latest main-* builds
python3 upgrade_operands.py throttle          # upgrade to latest v1.2.0-* builds
python3 upgrade_operands.py main --dry-run    # preview changes only
python3 upgrade_operands.py throttle --skip-operator   # operands only
python3 upgrade_operands.py main --skip-operands       # operator only
python3 upgrade_operands.py main --namespace custom-ns
```

### Components Upgraded

| Component | main mode | throttle mode |
|-----------|-----------|---------------|
| Operator (helm) | `main-<N>` | `v1.2.0-<N>` |
| Device Plugin | `main-<N>` | `v1.2.0-<N>` |
| Node Labeller | `main-<N>` | `v1.2.0-<N>` |
| Metrics Exporter | `exporter-0.0.1-<N>` | `nic-v1.2.0-<N>` |
| CNI Plugins | `main-<N>` | `v1.2.0-<N>` |

### Execution Order

1. Discover latest tags from assets server (HTTP scrape) and Docker registry (skopeo)
2. Upgrade operator via `helm upgrade` (skipped if already current)
3. Patch each NetworkConfig CRD to update operand images
4. Wait for DaemonSet rollouts
5. Verify pod images match expected tags

### Exit Codes

- `0`: All upgrades passed or skipped (already current)
- `1`: One or more upgrades failed

### Environment Variables

- `KUBECONFIG` — Path to kubeconfig file (optional, uses `~/.kube/config` by default)
- `CNI_REGISTRY_CREDS` — Docker registry credentials in `user:token` format for CNI tag lookup (optional; if unset, credentials are read from the `amdpsdo-secret` dockerconfigjson secret in `kube-amd-network`)

---

## validate_debain_exporter.py

Comprehensive validation script for AMD NIC metrics exporter with 20 test cases covering service lifecycle, configuration hot-reload, and metrics validation.

### Purpose

This script performs comprehensive validation of:
- Firmware version consistency
- nicctl command-line tool functionality
- NIC metrics exporter service lifecycle (stop/start, restart, enable/disable)
- Configuration hot-reload capability
- Dynamic config updates (ServerPort, MetricsFieldPrefix, Fields, Labels, CustomLabels)
- Label distribution across different metric types
- RDMA queue pair metrics accuracy
- Config map field validation

### Prerequisites

- AMD NIC hardware installed
- `nicctl` command-line tool installed and in PATH
- `amd-nic-metrics-exporter` service running
- `/etc/metrics/config-nic.json` configuration file
- `curl` command available
- `sudo` privileges for service and config modifications
- Metrics exporter log at `/var/log/amd-nic-metrics-exporter.log`
- `config_map.json` file in the same directory as the script

### Usage

```bash
python3 validate_debain_exporter.py <fw_version> <nic_exporter_version>
```

**Arguments:**
- `fw_version`: Expected firmware version (e.g., "1.117.5-a-57")
- `nic_exporter_version`: Expected NIC exporter version string (e.g., "nic-v1.0.0-14")

**Example:**
```bash
python3 validate_debain_exporter.py 1.117.5-a-57 nic-v1.0.0-14
```

### Test Cases (20 Total)

#### Test 1: Firmware & nicctl Version Validation
- Validates `nicctl show version firmware` returns expected Firmware version
- Validates `nicctl show version host-software` returns expected nicctl version

#### Test 2: Basic nicctl Command Checks
- Tests `nicctl show lif` command executes successfully
- Tests `nicctl show card` command executes successfully

#### Test 3: NIC Exporter Version Validation
- Parses `/var/log/amd-nic-metrics-exporter.log` for version string
- Validates exporter version matches expected version

#### Test 4: NIC Exporter Service Status
- Verifies `amd-nic-metrics-exporter.service` is active
- Skips remaining tests if service is inactive

#### Test 5: NIC Exporter Service Stop/Start
- Stops the service and verifies status becomes inactive
- Validates metrics fetch fails with connection refused
- Starts the service and verifies status becomes active
- Validates metrics fetch returns HTTP 200

#### Test 6: NIC Exporter Service Restart
- Restarts the service using `systemctl restart`
- Checks journalctl logs for stop and start messages
- Verifies service status is active after restart
- Validates metrics fetch works after restart

#### Test 7: NIC Exporter Service Enable/Disable
- Disables and stops the service using `systemctl disable --now`
- Verifies service is stopped and metrics fetch fails
- Enables and starts the service using `systemctl enable --now`
- Verifies service is running and metrics fetch works

#### Test 8: Config File Backup
- Creates backup of `/etc/metrics/config-nic.json` to `/tmp/config-nic.json.backup`
- Used as restore point for subsequent config tests

#### Test 9: Update ServerPort (5001 → 5010)
- Updates `ServerPort` in config from 5001 to 5010
- Waits 10 seconds for hot-reload (no service restart)
- Validates old port becomes unreachable
- Validates new port serves metrics

#### Test 10: Update MetricsFieldPrefix (amd_ → pensando_)
- Updates `CommonConfig.MetricsFieldPrefix` from "amd_" to "pensando_"
- Waits 10 seconds for hot-reload
- Validates new prefix appears in metrics
- Validates old prefix is removed from metrics (excluding comments)

#### Test 11: Remove ETH_ Fields
- Removes all fields starting with "ETH_" from `NICConfig.Fields` array
- Waits 10 seconds for hot-reload
- Validates ETH_ metrics are removed from output
- Validates other metrics (NIC_) still present

#### Test 12: Update NICConfig Fields
- Replaces first 3 fields in `NICConfig.Fields` with dummy field names:
  - `NIC_PORT_STATS_FRAMES_RX_SDF`
  - `NIC_PORT_STATS_FRAMES_RX_JJJ`
  - `NIC_PORT_STATS_FRAMES_RX_BAD_KKK`
- Waits 10 seconds for hot-reload
- Validates old field names removed from metrics
- Validates metrics system still functional

#### Test 13: Remove All NICConfig Fields
- Sets `NICConfig.Fields` to empty array `[]`
- Waits 10 seconds for hot-reload
- Validates that default metrics are still exported (NIC_PORT_STATS, NIC_LIF_STATS, RDMA, QP, ETH)
- Confirms exporter falls back to default field set

#### Test 14: Remove Specific NICConfig Labels
- Removes labels: `NIC_UUID`, `POD`, `POD_UUID`, `NAMESPACE`, `CONTAINER`, `FIRMWARE_VERSION`
- Waits 10 seconds for hot-reload
- **Label Distribution Validation:**
  - **nic_port_stats metrics**: Should have only 3 labels (NIC_ID, HOSTNAME, SERIAL_NUMBER)
  - **amd_eth_ metrics**: Should have all 6 labels (POD, NAMESPACE, CONTAINER, NIC_ID, HOSTNAME, SERIAL_NUMBER)
  - **nic_lif_stats metrics**: Should have all 6 labels
  - **rdma metrics**: Should have all 6 labels
  - **qp metrics**: Should have all 6 labels
- Validates labels are correctly applied per metric type

#### Test 15: Add CustomLabels
- Adds `NICConfig.CustomLabels` object with:
  - `CLUSTER_NAME`: "amdnetwork-k8s-metrics-exporter"
  - `CLUSTER_ENVIRONMENT`: "systest"
- Waits 10 seconds for hot-reload
- Validates both custom labels appear in metrics
- Counts occurrences of each custom label
- Verifies label values are correct

#### Test 16: Restore Original Configuration
- Restores `/etc/metrics/config-nic.json` from backup
- Waits 10 seconds for hot-reload
- Verifies service remains active after restore
- Cleans up temporary test files

#### Test 17: RDMA Queue-Pair Validation
- Queries `nicctl show rdma queue-pair --summary`
- Calculates total queue pairs across all interfaces
- Determines expected queue pair ID (total QPs + 2)

#### Test 18: Metric Count Validation
- Fetches metrics from endpoint
- Counts occurrences of `amd_qp_rq_rsp_rx_num_packet` metric
- Validates metric count matches expected queue pair ID

#### Test 19: Metric Value Sum Validation
- Uses qp_id 3200 if total QPs >= 4000, otherwise expected_qp_id / 2
- Sums metric values for the selected queue pair ID
- Validates sum exceeds 1000 (indicates active traffic)

#### Test 20: Config Map Validation
- Loads expected metric fields from `config_map.json`
- Reads current `MetricsFieldPrefix` from config
- Checks if each field exists in the fetched metrics
- Reports found vs missing fields with detailed output
- Pass: All fields found, Warning: 90%+ found, Fail: <90% found

### Configuration Structure

The script validates the following config structure in `/etc/metrics/config-nic.json`:

```json
{
  "ServerPort": 5001,
  "CommonConfig": {
    "MetricsFieldPrefix": "amd_"
  },
  "NICConfig": {
    "Fields": ["FIELD_NAME_1", "FIELD_NAME_2", ...],
    "Labels": ["NIC_ID", "HOSTNAME", "SERIAL_NUMBER", "POD", "NAMESPACE", "CONTAINER", ...],
    "CustomLabels": {
      "CLUSTER_NAME": "value",
      "CLUSTER_ENVIRONMENT": "value"
    }
  }
}
```

### Exit Codes

- `0`: All validations passed
- `1`: One or more validations failed

### Test Summary Output

The script provides a comprehensive test summary at the end:

```
- Firmware version consistency
- nicctl command-line tool functionality
- NIC metrics exporter operation
- RDMA queue pair metrics accuracy

### Prerequisites

- AMD NIC hardware installed
- `nicctl` command-line tool installed and in PATH
- `amd-nic-metrics-exporter` service running
- `/etc/metrics/config-nic.json` configuration file (for ServerPort and MetricsFieldPrefix)
- `curl` command available
- Metrics exporter log at `/var/log/amd-nic-metrics-exporter.log`
- `config_map` file in the same directory as the script (contains list of expected metric fields)

### Usage

```bash
python3 validate_nicctl.py <fw_version> <nic_exporter_version>
```

**Arguments:**
- `fw_version`: Expected firmware version (e.g., "1.117.5-a-57")
- `nic_exporter_version`: Expected NIC exporter version string (e.g., "nic-v1.0.0-14")

**Configuration:**
The script automatically reads the following from `/etc/metrics/config-nic.json`:
- `ServerPort`: Metrics exporter port (defaults to 5001 if not found)
- `MetricsFieldPrefix`: Prefix for metric names (defaults to "amd_" if not found)

**Example:**
```bash
python3 validate_nicctl.py 1.117.5-a-57 nic-v1.0.0-14
```

### Validation Checks (12 Basic Tests)

1. **Firmware Version Check** - Firmware version validation
2. **Host Software Version Check** - nicctl tool version validation
3. **Basic Command Functionality** - `nicctl show lif` and `show card` commands
4. **NIC Exporter Version Check** - Log file version validation
5. **NIC Exporter Service Status Check** - Service active status
6. **Service Stop/Start Test** - Service lifecycle testing
7. **Service Restart Test** - Service restart validation
8. **Service Disable/Enable Test** - Service control validation
9. **RDMA Queue Pair Validation** - Queue pair count calculation
10. **Metrics Endpoint Validation** - Metric count validation
11. **Metric Value Validation** - Metric value sum validation
12. **Config Map Validation** - Field existence validation

**Note:** This is the legacy version. For comprehensive testing with config updates and hot-reload validation, use `validate_debain_exporter.py` (20 tests)

### Exit Codes

- `0`: All validations passed
- `1`: One or more validations failed

### Output Format

Each validation prints a status line:
```
Using metrics port: 5001
Firmware : 1.117.5-a-57 PASS
nicctl   : 1.117.5-a-57 PASS
nicctl show lif : PASS
nicctl show card : PASS
NIC exporter : nic-v1.0.0-14 PASS
NIC exporter service : active PASS

Testing service stop/start...
Service stop : PASS
Service status check (stopped) : PASS
Metrics fetch when stopped : PASS (connection refused)
Service start : PASS
Service status check (running) : PASS
Metrics fetch when started : PASS
Service stop/start test : PASS

Testing service restart...
Service restart : PASS
Restart logs verification : PASS (found stop and start messages)
Service status check (after restart) : PASS
Metrics fetch after restart : PASS
Service restart test : PASS

Testing service control...
Service disable : PASS
Service stopped verification : PASS
Metrics fetch when disabled : PASS (connection failed as expected)
Service enable : PASS
Service running verification : PASS
Metrics fetch when enabled : PASS
Service control test : PASS

Total queue pairs : 14000
Expected qp_id   : 14002
amd_qp_rq_rsp_rx_num_packet count : 14002
Metric count validation : PASS
Metric value sum for qp_id 3200 : 15234
Metric value validation : PASS

```

### Troubleshooting

**"nicctl: command not found"**
- Ensure nicctl is installed and in your PATH
- Verify AMD NIC drivers are properly installed

**"NIC exporter : FAIL"**
- Check if amd-nic-metrics-exporter service is running
- Verify log file exists at `/var/log/amd-nic-metrics-exporter.log`
- Ensure metrics endpoint is accessible on the specified port

**"NIC exporter service : FAIL (not active)"**
- Service is not running - metrics-related tests will be skipped
- Start the service with `sudo systemctl start amd-nic-metrics-exporter.service`
- Enable the service with `sudo systemctl enable amd-nic-metrics-exporter.service`

**"Service stop/start test : FAIL"**
- Service may not have proper permissions to stop/start
- Check systemd service configuration
- Verify user has sudo privileges

**"Service restart test : FAIL"**
- Service may not restart correctly
- Check journalctl logs for errors: `journalctl -u amd-nic-metrics-exporter.service -n 50`
- Verify service configuration is valid

**"Metrics fetch when stopped : WARNING"**
- Metrics endpoint is still accessible when service should be stopped
- May indicate another instance of the exporter is running
- Check for orphaned processes

**"Metric count validation : FAIL"**
- RDMA queue pairs may not be properly configured
- Metrics exporter may not be collecting all queue pair data
- Check nicctl RDMA configuration

**"Metric value validation : FAIL (sum <= 1000)"**
- Insufficient traffic through queue pairs
- Run RDMA traffic tests before validation
- Ensure workloads are actively using the NICs

**"Config map validation : FAIL"**
- Missing `config_map` file in script directory
- Metrics exporter not exposing expected fields
- Check `/etc/metrics/config-nic.json` for correct MetricsFieldPrefix
- Verify all expected metrics are being collected by the exporter

### Integration

This script is designed for:
- Post-installation validation
- CI/CD pipeline integration
- Regression testing after firmware/driver updates
- Pre-deployment health checks

### Related Documentation

- [AMD Network Operator Overview](../../../docs/overview.md)
- [Metrics Exporter Documentation](../../../docs/metrics/)
- [Device Plugin Documentation](../../../docs/device_plugin/)

---

## validate_exporter_docker.py

Comprehensive validation suite for the `network-device-metrics-exporter` Docker container with 15 test cases covering container lifecycle, configuration hot-reload, and RDMA metrics validation.

### Purpose

This script performs comprehensive validation of:
- Docker container presence and image correctness
- Metrics endpoint connectivity on configured ports
- Configuration-driven field and label validation
- Dynamic config hot-reload (ServerPort, MetricsFieldPrefix, Fields, Labels)
- Label distribution across different metric types (nic_port_stats, eth_, nic_lif_stats, rdma, qp)
- Port switching and connectivity validation
- RDMA queue pair metrics accuracy
- Config restoration after mutations

### Prerequisites

- Docker daemon running
- `network-device-metrics-exporter` container running
- `curl` command available on host
- Python 3.6+
- `config/config.json` file in the same directory as the script
- Network connectivity to container metrics endpoints

### Usage

```bash
python validate_exporter_docker.py <expected_image_tag>
```

**Arguments:**
- `expected_image_tag`: Expected Docker image tag (e.g., `rocm/device-metrics-exporter:nic-v1.1.0`)

**Example:**
```bash
python validate_exporter_docker.py rocm/device-metrics-exporter:nic-v1.1.0
```

### Configuration

Place a `config/config.json` file in the same directory as the script:

```json
{
  "ServerPort": 5005,
  "CommonConfig": {
    "MetricsFieldPrefix": "amd_",
    "HealthService": {
      "Enable": true,
      "PollingRate": "30s"
    }
  },
  "NICConfig": {
    "Fields": ["NIC_PORT_STATS_FRAMES_RX_OK", "NIC_PORT_STATS_FRAMES_TX_OK", ...],
    "Labels": ["NIC_ID", "HOSTNAME", "SERIAL_NUMBER", "POD", "NAMESPACE", "CONTAINER", ...],
    "CustomLabels": {}
  }
}
```

### Test Cases (15 Total)

#### Basic Validation (Tests 1-5)

| Test | Name | Purpose |
|------|------|---------|
| 1 | Container Presence Validation | Verify target container is running via `docker ps` |
| 2 | Image Validation | Confirm actual image matches expected input |
| 3 | Config ServerPort Metrics Validation | Fetch metrics via port from config.json on host machine |
| 4 | NICConfig Fields Metrics Validation | Validate all configured fields appear in metrics output |
| 5 | NICConfig Labels Metrics Validation | Validate all configured labels present in output |

#### Advanced Port Testing (Test 6)

| Test | Name | Purpose |
|------|------|---------|
| 6 | Port 5010 Metrics Validation | Temporarily change ServerPort to 5010, validate metrics, restore original |

#### Config Mutation Tests (Tests 7-11)

| Test | Name | Purpose |
|------|------|---------|
| 7 | Update MetricsFieldPrefix | Change `amd_` → `pensando_`, validate hot-reload, restore |
| 8 | Remove ETH_ Fields | Remove all fields starting with `ETH_` from config |
| 9 | Update NICConfig Fields | Replace first 2 fields with dummy names, validate removal, restore |
| 10 | Remove Specific NICConfig Labels | Remove 6 labels, validate per metric type (nic_port_stats vs eth_) |
| 11 | Add CustomLabels | Inject custom labels (CLUSTER_NAME, CLUSTER_ENVIRONMENT) |

#### Recovery & Validation (Tests 12-15)

| Test | Name | Purpose |
|------|------|---------|
| 12 | Restore Original Configuration | Verify original config fully restored after all mutations |
| 13 | RDMA Queue-Pair Validation | Extract total queue pairs from `nicctl show rdma queue-pair --summary` |
| 14 | Metric Count Validation | Verify metric count matches expected queue-pair total |
| 15 | Metric Value Sum Validation | Aggregate metric values for specific queue-pair ID |

### Configuration Structure

```json
{
  "ServerPort": 5005,
  "CommonConfig": {
    "MetricsFieldPrefix": "amd_"
  },
  "NICConfig": {
    "Fields": ["FIELD_NAME_1", "FIELD_NAME_2", ...],
    "Labels": ["NIC_ID", "HOSTNAME", "SERIAL_NUMBER", "POD", "NAMESPACE", "CONTAINER", ...],
    "CustomLabels": {
      "CLUSTER_NAME": "value",
      "CLUSTER_ENVIRONMENT": "value"
    }
  }
}
```

### Key Features

- **No CLI input required**: Auto-detects container and reads from config.json
- **Config-driven metrics**: All validation based on config.json definitions only
- **Hot-reload validation**: 5-second wait between config changes for service to reload
- **Cascading skip logic**: If container is missing (Test 1), Tests 2-15 are automatically skipped
- **Atomic config restoration**: Always restores original config in `finally` block, even on error
- **Per-metric-type validation**: Label removal validates correct metric types:
  - `nic_port_stats metrics`: Should have only 3 labels (NIC_ID, HOSTNAME, SERIAL_NUMBER)
  - `eth_` metrics: Should have all 6 labels (POD, NAMESPACE, CONTAINER, NIC_ID, HOSTNAME, SERIAL_NUMBER)
  - `nic_lif_stats metrics`: Should have all 6 labels
  - `rdma metrics`: Should have all 6 labels
  - `qp metrics`: Should have all 6 labels

### Output Format

```
============================================================
Test 1: Container Presence Validation
============================================================
Container 'network-device-metrics-exporter' present : PASS

============================================================
Test 3: Config ServerPort Metrics Validation
============================================================
ServerPort from config: 5005
Running on host: curl -sS http://localhost:5005/metrics
PASS: localhost:5005/metrics returned values

...

============================================================
TEST SUMMARY
============================================================
Total Tests  : 15
Passed       : 13
Failed       : 1
Skipped      : 1
Success Rate : 86.7%
```

### Exit Codes

- `0`: All tests passed
- `1`: One or more tests failed or skipped

### Troubleshooting

**"Container not found"**
- Ensure `network-device-metrics-exporter` container is running: `docker ps | grep network-device-metrics-exporter`
- Verify container name matches `TARGET_CONTAINER` in script (default: `network-device-metrics-exporter`)

**"Could not load ServerPort from config"**
- Verify `config/config.json` exists in same directory as script
- Ensure JSON is valid and contains `ServerPort` key
- Validate with: `python -m json.tool config/config.json`

**"Metrics pull from localhost:5010 failed"**
- Test 6 temporarily changes ServerPort to 5010
- Ensure metrics service can reach that port or increase sleep timeout from 5 seconds
- Verify service hot-reloads config changes within 5 seconds
- Check container logs: `docker logs network-device-metrics-exporter`

**"Expected metric count mismatch"**
- Test 13 extracts queue-pair count from `nicctl show rdma queue-pair --summary`
- Test 14 validates metric count matches extracted total
- Verify `nicctl` is available inside container: `docker exec network-device-metrics-exporter which nicctl`
- Check RDMA output: `docker exec network-device-metrics-exporter nicctl show rdma queue-pair --summary`

**"removed label still present in nic_port_stats metrics"**
- Test 10 validates that removed labels don't appear in nic_port_stats metrics
- Other metric types (eth_, nic_lif_stats, rdma, qp) are expected to retain all labels
- Ensure label removal only affects the metric types specified in config

### Integration

This script is designed for:
- Post-deployment validation of Docker-based metrics exporter
- CI/CD pipeline integration
- Regression testing after configuration updates
- Pre-launch health checks for containerized deployments
- Configuration hot-reload verification
