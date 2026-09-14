# AMD Network Operator Install / Uninstall

Scripts to install and uninstall the AMD Network Operator and its NetworkConfig operands on a Kubernetes cluster via SSH.

## Files

| File | Description |
|------|-------------|
| `install_network_operator.py` | Installs operator (Helm) and applies NetworkConfig operands |
| `uninstall_network_operator.py` | Deletes all NetworkConfig CRs, then Helm uninstalls the operator |
| `image_manifest_1_1_0.yaml` | Helm chart version and image references for v1.1.0 |
| `pf_networkconfig.yaml` | NetworkConfig CR for PF (bare-metal) nodes — selector: `amd-nic` |
| `vf_networkconfig.yaml` | NetworkConfig CR for VF (VM) nodes — selector: `amd-vnic` |
| `env.json` | Testbed config with master node IP, credentials, and worker node info |

## Prerequisites

- Python 3.8+
- `paramiko` (or `sshpass` on the system)
- Master node must have `helm` and `kubectl` installed
- Kubernetes cluster must be running

```bash
pip install paramiko pyyaml
```

## env.json Format

The scripts read master node IP and credentials from `env.json` (or `/warmd.json` as fallback).

```json
{
  "Instances": [
    {
      "RawJSON": {
        "instances": [
          {
            "ip": "<master-node-ip>",
            "username": "<username>",
            "password": "<password>",
            "type": "master"
          },
          {
            "ip": "<worker-node-ip>",
            "username": "<username>",
            "password": "<password>",
            "type": "worker"
          }
        ]
      }
    }
  ]
}
```

### PF vs VF Node Detection

Worker nodes are classified automatically from `env.json`:
- **PF node** (bare-metal): worker without a `vm` key -> `pf_networkconfig.yaml` applied
- **VF node** (VM): worker with a `vm` key -> `vf_networkconfig.yaml` applied

## Install

### Usage

```bash
# Default — uses env.json, image_manifest_1_1_0.yaml, pf/vf networkconfig in same directory
python3 install_network_operator.py --env env.json

# Operator only, skip operand (NetworkConfig)
python3 install_network_operator.py --env env.json --skip-operand

# Skip waiting for pods to be ready
python3 install_network_operator.py --env env.json --skip-wait

# Custom paths
python3 install_network_operator.py \
    --env /warmd.json \
    --manifest /path/to/image_manifest_1_1_0.yaml \
    --pf-networkconfig /path/to/pf_networkconfig.yaml \
    --vf-networkconfig /path/to/vf_networkconfig.yaml
```

### What It Does

1. **Step 1 — Install Operator**
   - Adds Helm repo (`rocm-network`)
   - Runs:
     ```
     helm upgrade --install amd-network-operator rocm-network/network-operator-charts \
       -n kube-amd-network --create-namespace \
       --version=<version from manifest> \
       --set kmm.enabled=false \
       --set node-feature-discovery.enabled=false
     ```
   - Waits for operator controller pod to be Running

2. **Step 2 — Apply NetworkConfig Operand(s)**
   - Detects PF/VF worker nodes from `env.json`
   - SCPs the appropriate `networkconfig.yaml` to the master node
   - Runs `kubectl apply -f <networkconfig.yaml>`
   - Waits for operand pods (device-plugin, node-labeller, metrics-exporter) to be Running

### CLI Options

| Option | Default | Description |
|--------|---------|-------------|
| `--env` | `env.json` | Path to env.json or warmd.json |
| `--manifest` | `image_manifest_1_1_0.yaml` | Image manifest with chart version |
| `--pf-networkconfig` | `pf_networkconfig.yaml` | NetworkConfig for PF nodes |
| `--vf-networkconfig` | `vf_networkconfig.yaml` | NetworkConfig for VF nodes |
| `--skip-operand` | off | Install operator only |
| `--skip-wait` | off | Do not wait for pods |

## Uninstall

### Usage

```bash
# Default
python3 uninstall_network_operator.py --env env.json

# Skip waiting for pods to terminate
python3 uninstall_network_operator.py --env env.json --skip-wait
```

### What It Does

Order is the reverse of install (operands must be deleted before operator):

1. **Step 1 — Delete all NetworkConfig CRs**
   - Lists all NetworkConfig resources in `kube-amd-network`
   - Deletes each one via `kubectl delete networkconfig <name>`
   - Waits for operand pods to terminate

2. **Step 2 — Helm uninstall operator**
   - Runs `helm uninstall amd-network-operator -n kube-amd-network`
   - Waits for all operator pods to terminate

### CLI Options

| Option | Default | Description |
|--------|---------|-------------|
| `--env` | `env.json` | Path to env.json or warmd.json |
| `--skip-wait` | off | Do not wait for pods to terminate |

## Notes

- All commands run on the master node via SSH as `sudo` with `KUBECONFIG=/etc/kubernetes/admin.conf`
- The scripts support both `paramiko` (Python SSH) and `sshpass` (CLI) for SSH connectivity
- NetworkConfig must be deleted before uninstalling the operator to ensure clean teardown
