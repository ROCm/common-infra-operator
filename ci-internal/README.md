# ci-internal — Image Manifest Generator

`gen_image_manifest.py` generates the `images.yaml` manifest that pytest uses
(`--image-manifest`) for GPU Operator validation. It reads a scenario config,
overlays component overrides onto a versioned baseline, downloads helm charts
and debian packages from CloudFront, and writes the completed manifest.

## Quick Start

```bash
# From the repo root:

# Nightly — DME + DCM against today's ROCm 10.2.0 nightly (k8 target from config)
python3 ci-internal/gen_image_manifest.py ci-internal/nightly-dme-dcm.yaml

# Nightly — specific date
python3 ci-internal/gen_image_manifest.py ci-internal/nightly-dme-dcm.yaml --date 20260923

# Nightly — override target to standalone (downloads debs instead of helm charts)
python3 ci-internal/gen_image_manifest.py ci-internal/nightly-dme-dcm.yaml --target standalone

# Pre-release — DME + DCM against ROCm 10.1.0rc1
python3 ci-internal/gen_image_manifest.py ci-internal/prerelease-10.1.0rc1.yaml

# Dry run — print manifest to stdout, skip downloads
python3 ci-internal/gen_image_manifest.py ci-internal/nightly-dme-dcm.yaml --dry-run
```

## How It Works

```
Scenario config ──► gen_image_manifest.py ──► tests/pytests/images.yaml
        │                    │                         │
        │                    ▼                         │
        │           downloads helm/debs                │
        │           from CloudFront to:                │
        │           tests/pytests/downloads/            │
        ▼                                              ▼
Baseline file                                  pytest --image-manifest
(versioned seed manifest)                      tests/pytests/images.yaml
```

1. **Load** the scenario config and its referenced baseline
2. **Resolve** image tags per override component (nightly or pre-release)
3. **Download** helm charts and debian packages from CloudFront
4. **Merge** — set `version` on override entries, add helm/deb entries with local paths
5. **Write** the completed manifest to `tests/pytests/images.yaml`

## File Layout

```
ci-internal/
├── gen_image_manifest.py              # the script
├── README.md                          # this file
├── baselines/
│   ├── baseline-v1.5.1.yaml           # gpu-operator + KMM at v1.5.1
│   └── baseline-v1.5.2.yaml           # (created when baseline moves up)
├── nightly-dme-dcm.yaml              # scenario configs
└── prerelease-10.1.0rc1.yaml
```

## Scenario Config Format

### Nightly

```yaml
baseline: baselines/baseline-v1.5.1.yaml
mode: nightly
target: k8                    # k8 | openshift | standalone | hypervisor
rocm_version: "10.2.0"
# date: "20260923"            # optional — defaults to today (UTC)

overrides:
  dme:
    version: v1.5.3
  dcm:
    version: v1.5.3
```

Image tag is constructed as `{version}-{rocm_version}a{date}`,
e.g. `v1.5.3-10.2.0a20260923`.

### Pre-release

```yaml
baseline: baselines/baseline-v1.5.1.yaml
mode: pre-release
target: k8

overrides:
  dme:
    image_tag: v1.5.3-10.1.0rc1-1
  dcm:
    image_tag: v1.5.3-10.1.0rc1-1
```

Image tag is provided directly — no construction needed.
DME and DCM can have independent tags (different build numbers).

## Image Tag Formats

| Scenario | Format | Example |
|----------|--------|---------|
| Nightly | `{version}-{rocm}a{date}` | `v1.5.3-10.2.0a20260923` |
| Pre-release RC | `{version}-{rocm}rc{N}-{build}` | `v1.5.3-10.1.0rc2-1` |
| Pre-release GA | `{version}-{rocm}-{build}` | `v1.5.3-10.1.0-6` |
| Release | `{version}-{rocm}` | `v1.5.3-10.1.0` |

## Baseline Files

Baselines use the same YAML format that pytest consumes (the seed manifest).
They contain:

- **Pinned entries** — gpu-operator, KMM, device-plugin etc. with `version` set
- **Override slots** — DME, DCM, test-runner entries with no `version` field

Baseline files are **versioned and immutable** — when the baseline moves
(e.g. gpu-operator v1.5.1 → v1.5.2), create a new file. Old baselines remain
for reproducibility.

## CLI Reference

```
gen_image_manifest.py [-h] [--target TARGET] [--date DATE] [--output OUTPUT]
                      [--download-dir DIR] [--dry-run] [--repo-root DIR]
                      config
```

| Argument | Description |
|----------|-------------|
| `config` | Path to scenario config YAML (positional, required) |
| `--target` | Override deployment target (k8, openshift, standalone, hypervisor) |
| `--date` | Override date for nightly mode (YYYYMMDD). See date resolution below. |
| `--output`, `-o` | Output manifest path (default: `tests/pytests/images.yaml`) |
| `--download-dir` | Download directory (default: `tests/pytests/downloads/`) |
| `--dry-run` | Print manifest to stdout, skip downloads |
| `--repo-root` | Repository root (default: auto-detect from script location) |

### Date Resolution (nightly mode)

The date used to construct nightly image tags is resolved in order:

1. `--date` CLI argument (highest priority)
2. `date` field in the scenario config YAML
3. Today's date in UTC (default)

If there is no nightly build for the resolved date, artifact downloads will
fail with a warning (non-fatal). The manifest is still generated with the
expected artifact paths.

### Dry Run

`--dry-run` prints the generated manifest to stdout and skips CloudFront
downloads. No `images.yaml` file is written and no artifacts are downloaded.
To write the manifest file while skipping downloads, combine with `--output`:

```bash
python3 ci-internal/gen_image_manifest.py ci-internal/nightly-dme-dcm.yaml \
    --dry-run -o tests/pytests/images.yaml
```

## Artifact Sources

| Source | Usage |
|--------|-------|
| CloudFront (`d2xt1y7cmoty0l.cloudfront.net`) | Helm charts, debian packages |
| DockerHub (`docker.io/amdpsdo/`) | Container images (pulled at deploy time, not downloaded) |
| Helm repo (`rocm.github.io/gpu-operator`) | GA gpu-operator chart (baseline, not downloaded) |

CloudFront is accessible from AMD network / VPN.

## Override Components

| Component | Containers | Helm Charts | Debs |
|-----------|-----------|-------------|------|
| `dme` | device-metrics-exporter, device-metrics-exporter-sriov, test-runner | device-metrics-exporter-charts | amdgpu-exporter, amdgpu-exporter-sriov |
| `dcm` | config-manager | — | — |

Components not in the override list (gpu-operator, KMM, device-plugin, etc.)
pass through from the baseline unchanged.

## Target-Specific Artifacts

The output manifest contains only the selected target's section. Per-target artifacts:

| Target | Containers | Helm Charts | Debs |
|--------|-----------|-------------|------|
| `k8` | DME exporter, test-runner, config-manager | DME helm chart | — |
| `openshift` | DME exporter, test-runner, config-manager | DME helm chart | — |
| `standalone` | DME exporter | — | amdgpu-exporter (22.04, 24.04) |
| `hypervisor` | DME SR-IOV exporter | — | amdgpu-exporter-sriov (22.04, 24.04) |

## Creating a New Scenario Config

1. Copy an existing config as a starting point
2. Set `baseline` to the appropriate baseline file
3. Set `mode` (`nightly` or `pre-release`) and `target`
4. Add overrides with the desired version or image_tag per component
5. Run with `--dry-run` to verify before actual download

## Creating a New Baseline

When the baseline version changes (e.g. gpu-operator releases v1.5.2):

1. Copy the latest baseline: `cp baselines/baseline-v1.5.1.yaml baselines/baseline-v1.5.2.yaml`
2. Update pinned versions (gpu-operator, KMM, etc.) to the new release
3. Update `images.meta.version`
4. Leave override slots (DME, DCM, test-runner) without `version`
5. Update scenario configs to reference the new baseline
