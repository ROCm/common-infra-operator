# lib/ Module Provenance

**Master copy: common-infra-operator** (as of 2026-09-03)

This directory is the single source of truth for shared test utilities.
Changes to lib/ modules go here first, then propagate to consuming repos
via repo-dependency.

## Sources

| Source | SHA | Date |
|---|---|---|
| gpu-operator | `f90e554d` | 2026-09-03 |
| network-operator (netop-test-migration) | `786ab4f` | 2026-09-03 |

## Module inventory

### From network-operator (superset — gpu-op base + NIC gap-fills)

| Module | gpu-op lines | netop additions | Notes |
|---|---|---|---|
| helm_util.py | 452 | +100 | helm_show_chart, helm_show_values, helm_template, helm_get_values, helm_get_manifest, helm_ensure_release_cleaned_up, find_latest_chart |
| k8_util.py | 3284 | +188 | k8_get_daemonsets, k8_get_daemonset, k8_wait_for_daemonset_ready, k8_all_pods_running, k8_wait_for_pods_ready, k8_patch_configmap, k8_ensure_namespace, k8_ensure_image_pull_secret, k8_get_service_cluster_ip, k8_get_node_internal_ip, k8_get_pod_ip, k8_get_pod_host_ip |

### From gpu-operator (identical in both repos)

common.py, metric_util.py, util.py, json_report.py, spec_util.py,
amdgpu.py, anr_util.py, autoremediation_util.py, deb_util.py,
dme_debug_util.py, dra_util.py, gim_util.py, node_gpu_collector.py,
npd_util.py, olm_util.py, vm_util.py

### Network-operator only

| Module | Lines | Purpose |
|---|---|---|
| nic_util.py | 1629 | NIC-specific: NetworkConfig CR ops, RDMA, metrics, topology |

### Data files (lib/files/)

28 JSON/YAML from gpu-operator: driver specs, label matrices, workload
specs, partitioning configs, manual job templates.

## Merged files (non-lib)

Tracked here for files outside lib/ that combine content from both repos.
Each entry records what was taken from each side.

| File | Base | Additions | Notes |
|---|---|---|---|
| conftest.py | netop (superset) | — | gpu-op content verified identical in shared sections |
| k8_test_launcher.sh | netop (superset) | — | +27L over gpu-op: --deployment, --testbed, --nic-config |
