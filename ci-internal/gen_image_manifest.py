# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""
gen_image_manifest.py — Generate pytest image manifest from baseline + scenario config.

Reads a scenario config YAML that references a versioned baseline (seed manifest),
resolves image tags for override components (DME, DCM), downloads helm charts and
debs from CloudFront, and writes a complete manifest for pytest --image-manifest.

Two modes:
  - nightly:     tag = {version}-{rocm_version}a{date}
  - pre-release: tag provided as image_tag in the scenario config

Usage:
  python3 ci-internal/gen_image_manifest.py ci-internal/nightly-dme-dcm.yaml
  python3 ci-internal/gen_image_manifest.py ci-internal/prerelease-10.1.0rc1.yaml
  python3 ci-internal/gen_image_manifest.py ci-internal/nightly-dme-dcm.yaml --date 20260923 --dry-run
"""

import argparse
import copy
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

CLOUDFRONT_BASE = "https://d2xt1y7cmoty0l.cloudfront.net"
DOCKERHUB_ORG = "amdpsdo"
PULL_SECRET = "docker-amdpsdo-auth"

# ---------------------------------------------------------------------------
# Component catalog — maps override component names to their artifacts.
# Catalog keys under "containers" match the entry names in the baseline manifest.
# ---------------------------------------------------------------------------
COMPONENT_CATALOG = {
    "dme": {
        "s3_prefix": "device-metrics-exporter",
        "containers": {
            "device-metrics-exporter": {
                "targets": ["k8", "openshift", "standalone"],
            },
            "device-metrics-exporter-sriov": {
                "targets": ["hypervisor"],
            },
            "test-runner": {
                "targets": ["k8", "openshift"],
            },
        },
        "helm_charts": {
            "device-metrics-exporter-helm": {
                "filename_template": "device-metrics-exporter-charts-{tag}.tgz",
                "targets": ["k8", "openshift"],
            },
        },
        "debs": {
            "exporter-debian-Ubuntu-22.04": {
                "filename_template": "amdgpu-exporter-{tag}~22.04_amd64.deb",
                "targets": ["standalone"],
            },
            "exporter-debian-Ubuntu-24.04": {
                "filename_template": "amdgpu-exporter-{tag}~24.04_amd64.deb",
                "targets": ["standalone"],
            },
            "sriov-exporter-debian-Ubuntu-22.04": {
                "filename_template": "amdgpu-exporter-sriov-{tag}~22.04_amd64.deb",
                "targets": ["hypervisor"],
            },
            "sriov-exporter-debian-Ubuntu-24.04": {
                "filename_template": "amdgpu-exporter-sriov-{tag}~24.04_amd64.deb",
                "targets": ["hypervisor"],
            },
        },
    },
    "dcm": {
        "s3_prefix": "device-config-manager",
        "containers": {
            "config-manager": {
                "targets": ["k8", "openshift"],
            },
        },
    },
}

VALID_TARGETS = ("k8", "openshift", "standalone", "hypervisor")


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def load_config(config_path):
    """Load and validate scenario config YAML."""
    path = Path(config_path)
    if not path.exists():
        die(f"scenario config not found: {path}")

    with open(path) as f:
        config = yaml.safe_load(f)

    if not isinstance(config, dict):
        die(f"{path}: config is not a YAML mapping")

    for key in ("baseline", "mode", "target", "overrides"):
        if key not in config:
            die(f"{path}: missing required key '{key}'")

    if config["mode"] not in ("nightly", "pre-release"):
        die(f"{path}: mode must be 'nightly' or 'pre-release', got '{config['mode']}'")

    if config["target"] not in VALID_TARGETS:
        die(f"{path}: target must be one of {VALID_TARGETS}, got '{config['target']}'")

    if not isinstance(config["overrides"], dict):
        die(f"{path}: 'overrides' must be a mapping")

    for comp in config["overrides"]:
        if comp not in COMPONENT_CATALOG:
            die(f"{path}: unknown override component '{comp}'. "
                f"Valid: {list(COMPONENT_CATALOG)}")

    if config["mode"] == "nightly" and "rocm_version" not in config:
        die(f"{path}: nightly mode requires 'rocm_version'")

    return config


def load_baseline(config, ci_internal_dir):
    """Load the baseline seed manifest, return a deep copy."""
    baseline_rel = config["baseline"]
    baseline_path = ci_internal_dir / baseline_rel
    if not baseline_path.exists():
        die(f"baseline not found: {baseline_path}")

    with open(baseline_path) as f:
        baseline = yaml.safe_load(f)

    if not isinstance(baseline, dict) or "images" not in baseline:
        die(f"{baseline_path}: baseline must have an 'images' key")

    target = config["target"]
    if target not in baseline["images"]:
        die(f"{baseline_path}: missing target section '{target}'")

    return copy.deepcopy(baseline)


def resolve_tags(config, date_override=None):
    """Resolve image_tag for each override component.

    Returns dict mapping component name to its resolved tag string.
    """
    mode = config["mode"]
    tags = {}

    for comp, comp_cfg in config["overrides"].items():
        if mode == "pre-release":
            if "image_tag" not in comp_cfg:
                die(f"pre-release override for '{comp}' must have 'image_tag'")
            tags[comp] = comp_cfg["image_tag"]

        else:  # nightly
            if "version" not in comp_cfg:
                die(f"nightly override for '{comp}' must have 'version'")
            version = comp_cfg["version"]
            rocm_version = config["rocm_version"]
            date = date_override or config.get("date") or datetime.now(timezone.utc).strftime("%Y%m%d")
            tags[comp] = f"{version}-{rocm_version}a{date}"

    return tags


def _s3_directory(s3_prefix, mode, tag, date=None):
    """Build the S3 directory path for a component's artifacts."""
    if mode == "nightly":
        if not date:
            # extract date from tag: v1.5.3-10.2.0a20260923 → 20260923
            date = tag.rsplit("a", 1)[-1] if "a" in tag else ""
        return f"{s3_prefix}/nightly/{date}"
    else:
        return f"{s3_prefix}/pre-release/{tag}"


def download_artifacts(config, tags, download_dir, dry_run=False):
    """Download helm charts and debs from CloudFront for override components.

    Returns dict mapping artifact_key to local filename (basename only).
    """
    target = config["target"]
    mode = config["mode"]
    date = config.get("date")
    downloads = {}
    failures = []

    os.makedirs(download_dir, exist_ok=True)

    for comp, tag in tags.items():
        catalog = COMPONENT_CATALOG[comp]
        s3_prefix = catalog["s3_prefix"]
        s3_dir = _s3_directory(s3_prefix, mode, tag, date)

        for art_key, art_cfg in catalog.get("helm_charts", {}).items():
            if target not in art_cfg["targets"]:
                continue
            filename = art_cfg["filename_template"].format(tag=tag)
            url = f"{CLOUDFRONT_BASE}/{s3_dir}/{filename}"
            local_path = os.path.join(download_dir, filename)

            if dry_run:
                print(f"  [dry-run] would download: {url}", file=sys.stderr)
                downloads[art_key] = filename
                continue

            if not _download_file(url, local_path):
                failures.append(f"{art_key}: {url}")
            else:
                downloads[art_key] = filename

        for art_key, art_cfg in catalog.get("debs", {}).items():
            if target not in art_cfg["targets"]:
                continue
            filename = art_cfg["filename_template"].format(tag=tag)
            url = f"{CLOUDFRONT_BASE}/{s3_dir}/{filename}"
            local_path = os.path.join(download_dir, filename)

            if dry_run:
                print(f"  [dry-run] would download: {url}", file=sys.stderr)
                downloads[art_key] = filename
                continue

            if not _download_file(url, local_path):
                failures.append(f"{art_key}: {url}")
            else:
                downloads[art_key] = filename

    if failures:
        print(f"\nwarn: {len(failures)} download(s) failed:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)

    return downloads


def _download_file(url, local_path):
    """Download a single file. Returns True on success."""
    print(f"  downloading {os.path.basename(local_path)} ...", file=sys.stderr, end=" ")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "gen-image-manifest/2.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            with open(local_path, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
        size_kb = os.path.getsize(local_path) / 1024
        print(f"ok ({size_kb:.0f} KB)", file=sys.stderr)
        return True
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        print(f"FAILED ({exc})", file=sys.stderr)
        return False


def merge_and_write(baseline, config, tags, downloads, output_path):
    """Merge override versions into baseline and write the output manifest."""
    target = config["target"]
    target_entries = baseline["images"][target]

    for comp, tag in tags.items():
        catalog = COMPONENT_CATALOG[comp]

        # Set version on container entries
        for container_name, container_cfg in catalog["containers"].items():
            if target not in container_cfg["targets"]:
                continue
            if container_name not in target_entries:
                die(f"baseline is missing expected entry '{container_name}' "
                    f"under images.{target}")
            target_entries[container_name]["version"] = tag

        # Add/update helm chart entries
        for art_key, art_cfg in catalog.get("helm_charts", {}).items():
            if target not in art_cfg["targets"]:
                continue
            filename = downloads.get(art_key)
            if not filename:
                continue
            target_entries[art_key] = {
                "location": f"file://downloads/{filename}",
                "version": tag,
                "kind": "helm-chart",
            }

        # Add/update deb entries
        for art_key, art_cfg in catalog.get("debs", {}).items():
            if target not in art_cfg["targets"]:
                continue
            filename = downloads.get(art_key)
            if not filename:
                continue
            target_entries[art_key] = {
                "location": f"file://downloads/{filename}",
                "version": tag,
                "kind": "debian",
            }

    # Strip non-target sections — output only meta + the selected target
    manifest = {
        "images": {
            "meta": baseline["images"]["meta"],
            target: target_entries,
        }
    }

    write_manifest(manifest, output_path)


def write_manifest(manifest, output_path):
    """Write manifest YAML to file or stdout."""
    content = yaml.dump(manifest, default_flow_style=False, sort_keys=False)
    if output_path == "-":
        sys.stdout.write(content)
    else:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w") as f:
            f.write(content)
        print(f"\nWrote manifest to {output_path}", file=sys.stderr)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate pytest image manifest from baseline + scenario config.",
        epilog="Run from the repository root.",
    )
    parser.add_argument(
        "config",
        help="Path to scenario config YAML (e.g. ci-internal/nightly-dme-dcm.yaml)",
    )
    parser.add_argument(
        "--target",
        choices=VALID_TARGETS,
        help="Override deployment target from config (k8, openshift, standalone, hypervisor)",
    )
    parser.add_argument(
        "--date",
        help="Override date for nightly mode (YYYYMMDD, default: today UTC)",
    )
    parser.add_argument(
        "--output", "-o",
        help="Output manifest path (default: tests/pytests/images.yaml)",
    )
    parser.add_argument(
        "--download-dir",
        help="Download directory (default: tests/pytests/downloads/)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print manifest to stdout, skip actual downloads",
    )
    parser.add_argument(
        "--repo-root",
        help="Repository root directory (default: auto-detect from script location)",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # Resolve repo root
    if args.repo_root:
        repo_root = Path(args.repo_root).resolve()
    else:
        repo_root = Path(__file__).resolve().parent.parent

    ci_internal_dir = repo_root / "ci-internal"
    default_output = str(repo_root / "tests" / "pytests" / "images.yaml")
    default_download_dir = str(repo_root / "tests" / "pytests" / "downloads")

    output_path = args.output or ("-" if args.dry_run else default_output)
    download_dir = args.download_dir or default_download_dir

    # Load config and baseline
    config = load_config(args.config)
    if args.target:
        config["target"] = args.target
    baseline = load_baseline(config, ci_internal_dir)

    # Resolve image tags
    tags = resolve_tags(config, date_override=args.date)

    print(f"Mode: {config['mode']}, Target: {config['target']}", file=sys.stderr)
    for comp, tag in tags.items():
        print(f"  {comp}: {tag}", file=sys.stderr)

    # Download artifacts
    downloads = download_artifacts(config, tags, download_dir, dry_run=args.dry_run)

    # Merge and write
    merge_and_write(baseline, config, tags, downloads, output_path)


if __name__ == "__main__":
    main()
