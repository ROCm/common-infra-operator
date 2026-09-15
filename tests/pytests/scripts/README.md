# Test Support Scripts

## default-tech-support.sh

Default diagnostic collection script that runs automatically on test failures when no component-specific tech-support tool is provided.

### Features

**Auto-Detection**: Automatically detects deployed components and collects relevant logs:

- GPU Operator (namespace: `kube-amd-gpu`)
- DRA Driver (namespace: `kube-amd-gpu-dra`)
- Metrics Exporter:
  - Kubernetes deployment (namespace: `kube-amd-exporter`)
  - Docker containers (`amd-metrics-exporter`)
  - Debian/Ubuntu packages (`dpkg`)
  - RHEL/CentOS packages (`rpm`)
- Node Feature Discovery (namespace: `node-feature-discovery`)
- Cert-Manager (namespace: `cert-manager`)

**Collected Data**:

*Kubernetes Deployments:*

- Pod logs (current + previous if available)
- Pod descriptions and YAML manifests
- Namespace events
- Custom Resources:
  - DeviceConfig (GPU Operator)
  - AmdGpu CRs (GPU Operator v1.0.0)
  - ResourceSlices (DRA)
  - DeviceClasses (DRA)
  - ResourceClaims (DRA)
- Cluster-wide information:
  - Node status and labels
  - GPU nodes (with NFD labels)
  - API resources and versions
  - Kubernetes version

*Docker Deployments:*

- Container list and status
- Container logs (last 5000 lines)
- Container inspect output
- Container stats
- amdgpuhealth output (if available on host)

*Package Deployments (Debian/RPM):*

- Package information and status
- Installed files list
- Systemd service status and logs (last 5000 lines)
- Service unit files and properties
- Configuration files (`/etc/amd-metrics-exporter`)
- Runtime data directory listing (`/var/lib/amd-metrics-exporter`)
- amdgpuhealth output
- Log files (`/var/log/amd-metrics-exporter`)

**DRA API Version Detection**: Automatically detects and uses the correct DRA API version:

- `v1` (Kubernetes 1.34+, GA)
- `v1beta2` (Kubernetes 1.33+, OpenShift 4.20+)
- `v1beta1` (Kubernetes 1.32-1.33)

### Usage

#### Automatic (via pytest)

The script runs automatically when tests fail:

```bash
# No --tech-support-tool specified = uses default script
pytest tests/pytests/k8/dra-driver/ -v

# Use custom tech-support tool
pytest tests/pytests/k8/dra-driver/ --tech-support-tool=/path/to/custom-tool.sh
```

#### Manual Execution

```bash
# Basic usage
./tests/pytests/scripts/default-tech-support.sh

# Override namespace defaults
GPU_OPERATOR_NAMESPACE=custom-namespace ./tests/pytests/scripts/default-tech-support.sh

# With custom kubeconfig
KUBECONFIG=/path/to/kubeconfig ./tests/pytests/scripts/default-tech-support.sh
```

### Environment Variables

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `KUBECONFIG` | `/etc/kubernetes/admin.conf` | Path to kubeconfig file |
| `GPU_OPERATOR_NAMESPACE` | `kube-amd-gpu` | GPU Operator namespace |
| `DRA_DRIVER_NAMESPACE` | `kube-amd-gpu-dra` | DRA Driver namespace |
| `EXPORTER_NAMESPACE` | `kube-amd-exporter` | Metrics Exporter namespace |

### Output

Creates a tarball: `techsupport-<timestamp>.tgz`

Structure:

```text
techsupport-20260402_120000/
├── kube-amd-gpu/                    # GPU Operator namespace
│   ├── pods.json
│   ├── pods.txt
│   ├── events.txt
│   ├── <pod-name>_describe.txt
│   ├── <pod-name>.yaml
│   ├── <pod-name>_<container>.log
│   └── <pod-name>_<container>_previous.log
├── kube-amd-gpu-dra/                # DRA Driver namespace
│   └── ...
├── docker-exporter/                 # Docker-based exporter (if detected)
│   ├── containers.txt
│   ├── <container>_inspect.json
│   ├── <container>.log
│   ├── <container>_stats.txt
│   ├── amdgpuhealth_output.txt
│   └── amdgpuhealth_dir.txt
├── debian-exporter/                 # Debian package-based exporter (if detected)
│   ├── package_info.txt
│   ├── package_status.txt
│   ├── installed_files.txt
│   ├── service_status.txt
│   ├── service_unit.txt
│   ├── service_logs.txt
│   ├── service_properties.txt
│   ├── config/                      # /etc/amd-metrics-exporter
│   ├── runtime_dir.txt              # /var/lib listing
│   ├── amdgpuhealth_output.txt
│   └── logs/                        # /var/log/amd-metrics-exporter
├── rpm-exporter/                    # RPM package-based exporter (if detected)
│   └── (similar structure to debian-exporter)
├── dra-resources/                   # DRA custom resources
│   ├── resourceslices_v1.yaml
│   ├── deviceclasses_v1.yaml
│   └── resourceclaims_v1.yaml
├── deviceconfigs.yaml               # GPU Operator CRs
├── cluster-info/                    # Cluster-wide info
│   ├── nodes.txt
│   ├── gpu-nodes.txt
│   ├── api-resources.txt
│   └── version.txt
└── ...
```

### Metrics Exporter Deployment Variants

The script automatically detects and collects from all three deployment modes:

**1. Kubernetes Deployment** (default):

- Detection: Checks for namespace `kube-amd-exporter`
- Collected: Pod logs, events, YAML manifests
- Collection method: kubectl (runs locally on test runner)

**2. Docker Containers**:

- Detection: Gets GPU nodes via kubectl, checks Docker on each node
- Collected: Container logs, inspect output, stats, amdgpuhealth
- Collection method: kubectl debug pod + chroot /host (remote execution)
- Fallback: If no GPU nodes found, checks local Docker

**3. Package Installation** (Debian/RPM):

- Detection: Gets GPU nodes via kubectl, checks dpkg/rpm on each node
- Collected: Package info, systemd logs, config files, runtime data
- Collection method: kubectl debug pod + chroot /host (remote execution)
- Fallback: If no GPU nodes found, checks local packages

### Remote Collection for Standalone Tests

For Docker and package deployments (standalone tests), the script:

1. Queries GPU nodes using: `kubectl get nodes -l feature.node.kubernetes.io/amd-gpu=true`
2. For each node, spawns an ephemeral debug pod: `kubectl debug node/<name>`
3. Runs commands via `chroot /host` to access the actual host filesystem
4. Collects results back to test runner

This enables collection from remote nodes where Docker/packages are deployed,
while the test runner (where script executes) only needs kubectl access.

**Architecture:**

```text
Test Runner (script runs here)
     |
     | kubectl debug
     v
GPU Node 1           GPU Node 2
- Docker container   - Debian package
- Logs collected <-- - Logs collected <--
```

All three variants are checked **independently** - the script will collect from all detected deployments.

### Adding Support for New Components

To add support for a new component:

1. **Add namespace detection**:

   ```bash
   NEW_COMPONENT_NS="${NEW_COMPONENT_NAMESPACE:-default-namespace}"
   if namespace_exists "${NEW_COMPONENT_NS}"; then
       log_info "Detected New Component deployment"
       collect_namespace_logs "${NEW_COMPONENT_NS}"
   fi
   ```

2. **Add custom resource collection** (if applicable):

   ```bash
   collect_custom_resources "api.group.com" "resourcetype" "${OUTPUT_DIR}/resourcetype.yaml"
   ```

3. **Update this README** with the new component details

### Troubleshooting

**Script fails with permission errors**:

- Ensure proper RBAC permissions for the service account
- Check KUBECONFIG points to valid config with cluster-admin or sufficient permissions

**Missing logs for certain pods**:

- Check pod status - terminated pods may have no current logs
- Previous logs require at least one container restart

**DRA resources not collected**:

- Verify DRA API is available: `kubectl api-resources | grep resource.k8s.io`
- Check if DynamicResourceAllocation feature gate is enabled (K8s 1.32-1.33)

**Large tarball size**:

- Adjust `--tail=5000` in pod log collection to reduce size
- Consider filtering out verbose pods
