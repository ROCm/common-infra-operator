#!/usr/bin/env python3
"""
Generate operator image manifest from pre-resolved component versions.

Config-driven: reads operator config YAML (ci-internal/operators/*.yml)
to determine which repos, images, and charts to resolve. Supports both
gpu-operator and network-operator from the same codebase.

Versions are provided via --versions-file (JSON), eliminating the need
for build server queries. Artifacts are expected to already exist in
--download-dir (downloaded from S3 or GHA by the orchestrator).

Usage:
    python3 gen_image_manifest.py \
        --operator-config ci-internal/operators/gpu-operator.yml \
        --seed-image-manifest ci-internal/sanity-images.yml \
        --versions-file versions.json \
        --target k8 \
        --download-dir ./downloads \
        --output /tmp/images.yaml
"""

import argparse
import glob
import json
import os
import sys

try:
    from ruamel.yaml import YAML
    yaml = YAML()
    yaml.preserve_quotes = True
    def yaml_load(f):
        return dict(yaml.load(f))
    def yaml_dump(data, f):
        yaml.dump(data, f)
except ImportError:
    import yaml as _yaml
    def yaml_load(f):
        return _yaml.safe_load(f)
    def yaml_dump(data, f):
        _yaml.dump(data, f, default_flow_style=False)


DEFAULT_SECRET = "docker-amdpsdo-auth"


# ---------------------------------------------------------------------------
# Operator config loader
# ---------------------------------------------------------------------------

def load_operator_config(config_path):
    """Load operator config YAML (ci-internal/operators/*.yml)."""
    with open(config_path) as f:
        config = yaml_load(f)
    return config


# ---------------------------------------------------------------------------
# Seed manifest loader
# ---------------------------------------------------------------------------

def load_seed_manifest(seed_path, target):
    """Load static entries and meta from seed manifest for the given target."""
    with open(seed_path) as f:
        seed = yaml_load(f)

    meta = dict(seed.get("images", {}).get("meta", {}))
    seed_target = seed.get("images", {}).get(target, {})
    static_entries = {}
    for name, info in seed_target.items():
        if not isinstance(info, dict):
            continue
        if info.get("kind") == "olm-subscription":
            entry = {}
            for field in ("kind", "catalog", "channel", "package", "csv"):
                if field in info:
                    entry[field] = str(info[field])
            static_entries[name] = entry
        elif "location" in info:
            entry = {}
            for field in ("key", "location", "version", "kind", "secret"):
                if field in info:
                    entry[field] = str(info[field])
            static_entries[name] = entry
        elif "key" in info:
            entry = {"key": str(info["key"]), "kind": str(info.get("kind", "container"))}
            if "secret" in info:
                entry["secret"] = str(info["secret"])
            static_entries[name] = entry
        elif "image" in info:
            entry = {}
            for field in ("image", "kind", "version", "secret"):
                if field in info:
                    entry[field] = str(info[field])
            if "image" in entry:
                entry["location"] = entry.pop("image")
            static_entries[name] = entry

    return meta, static_entries


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_file(directory, pattern):
    """Find a file matching a glob pattern in a directory."""
    matches = glob.glob(os.path.join(directory, pattern))
    return matches[0] if matches else None


def _build_repo_to_download_dir(op_config):
    """Map build_repo key to download subdirectory name.

    Derived from assets_path by stripping the 'hourly-' prefix.
    """
    mapping = {}
    for key, info in op_config.get("build_repos", {}).items():
        ap = info.get("assets_path", "")
        mapping[key] = ap.replace("hourly-", "", 1) if ap.startswith("hourly-") else ap
    mapping.setdefault("dra", "k8s-gpu-dra-driver")
    return mapping


# ---------------------------------------------------------------------------
# Config-driven manifest builder
# ---------------------------------------------------------------------------

def build_manifest(args, op_config, versions, seed_meta, seed_static):
    """Build the output manifest dict from resolved versions + seed entries."""
    secret = args.secret
    download_dir = args.download_dir.rstrip("/")
    entries = dict(seed_static)
    repo_to_dir = _build_repo_to_download_dir(op_config)

    # Apply driver registry from registries.json (run_sanity.sh generates this)
    if args.registries and os.path.exists(args.registries):
        with open(args.registries) as f:
            reg_info = json.load(f)
        driver_registry = reg_info.get("driver-registry", {}).get("value")
        if driver_registry:
            driver_entry = entries.get("driver", {"key": "driver.image", "kind": "container"})
            driver_entry["location"] = f"container://{driver_registry}/amdgpu_kmod"
            entries["driver"] = driver_entry

    helm_charts = op_config.get("helm_charts", {})
    container_images = op_config.get("container_images", {})
    standalone_artifacts = op_config.get("standalone_artifacts", {})

    # Resolve container images → file:// paths to downloaded tarballs
    for artifact_name, img_info in container_images.items():
        component = img_info["build_repo"]
        ver = versions.get(component)
        if not ver:
            continue

        comp_dir = os.path.join(download_dir, repo_to_dir.get(component, component))
        filename_prefix = img_info.get("filename_prefix", img_info["image"])
        tag_prefix = img_info.get("tag_prefix", "")

        tarball = (_find_file(comp_dir, f"{filename_prefix}-*.tar.gz")
                   or _find_file(comp_dir, f"{filename_prefix}-*.tgz")
                   or _find_file(comp_dir, f"{filename_prefix}*.tar.gz"))
        if not tarball:
            print(f"  WARNING: No tarball found for {artifact_name} ({image_name}) in {comp_dir}")
            continue

        entries[artifact_name] = {
            "key": img_info["helm_key"],
            "image": f"file://{tarball}",
            "kind": "container",
            "version": f"{tag_prefix}{ver}",
        }

    # Resolve helm charts → file:// paths to downloaded charts
    for chart_name, chart_info in helm_charts.items():
        component = chart_info["build_repo"]
        ver = versions.get(component)
        if not ver:
            continue

        comp_dir = os.path.join(download_dir, repo_to_dir.get(component, component))
        filename = chart_info["filename"].format(version=ver)
        chart_path = os.path.join(comp_dir, filename)

        if not os.path.exists(chart_path):
            chart_path = _find_file(comp_dir, chart_info["filename"].replace("{version}", "*"))

        if not chart_path:
            print(f"  WARNING: Helm chart not found: {filename} in {comp_dir}")
            continue

        entries[chart_name] = {
            "location": f"file://{chart_path}",
            "version": ver,
            "kind": "helm-chart",
        }

    # Resolve OLM bundles (OpenShift)
    if args.target == "openshift":
        olm_config = op_config.get("olm", {})
        for bundle_name, bundle_info in olm_config.items():
            component = bundle_info["build_repo"]
            ver = versions.get(component)
            if not ver:
                continue

            comp_dir = os.path.join(download_dir, repo_to_dir.get(component, component))
            tarball = _find_file(comp_dir, f"*olm-bundle*{ver}*.tar.gz")
            if tarball:
                entries[bundle_name] = {
                    "image": f"file://{tarball}",
                    "version": ver,
                    "kind": "olm-bundle",
                }
            else:
                print(f"  WARNING: OLM bundle not found for {bundle_name} in {comp_dir}")

    # Resolve standalone artifacts (debian packages, sriov images)
    if args.target in ("standalone", "hypervisor"):
        artifacts_key = "hypervisor_artifacts" if args.target == "hypervisor" else "standalone_artifacts"
        for artifact_name, art_info in op_config.get(artifacts_key, {}).items():
            component = art_info["build_repo"]
            ver = versions.get(component)
            if not ver:
                continue

            comp_dir = os.path.join(download_dir, repo_to_dir.get(component, component))

            if "filename" in art_info:
                filename = art_info["filename"].format(version=ver)
                file_path = os.path.join(comp_dir, filename)
                if not os.path.exists(file_path):
                    file_path = _find_file(comp_dir, art_info["filename"].replace("{version}", "*"))
                if not file_path:
                    print(f"  WARNING: Artifact not found: {filename}")
                    continue
                entries[artifact_name] = {
                    "location": f"file://{file_path}",
                    "version": ver,
                    "kind": "debian",
                }
            elif "image" in art_info:
                tarball = _find_file(comp_dir, f"{art_info['image']}-*.tar.gz")
                if not tarball:
                    print(f"  WARNING: No tarball for {artifact_name}")
                    continue
                entries[artifact_name] = {
                    "key": art_info.get("helm_key", ""),
                    "image": f"file://{tarball}",
                    "version": ver,
                    "kind": "container",
                }

    # Build final manifest
    manifest = {
        "images": {
            "meta": seed_meta,
            args.target: entries,
        }
    }
    return manifest


def write_manifest(manifest, output_path):
    """Write manifest YAML to file."""
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w") as f:
        yaml_dump(manifest, f)
    print(f"\nManifest written to {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate operator image manifest")
    parser.add_argument("--operator-config", required=True,
                        help="Path to operator config YAML (ci-internal/operators/*.yml)")
    parser.add_argument("--seed-image-manifest", required=True,
                        help="Path to seed image manifest")
    parser.add_argument("--versions-file", required=True,
                        help="JSON file with pre-resolved versions per component")
    parser.add_argument("--target", required=True,
                        choices=["k8", "openshift", "standalone", "hypervisor"],
                        help="Deployment target")
    parser.add_argument("--output", default="/tmp/images.yaml",
                        help="Output manifest path")
    parser.add_argument("--download-dir", default="./downloads",
                        help="Directory where artifacts were downloaded")
    parser.add_argument("--registries", default=None,
                        help="Path to registries.json (for driver registry)")
    parser.add_argument("--secret", default=DEFAULT_SECRET,
                        help="K8s imagePullSecret name")

    args = parser.parse_args()

    # Load operator config
    print(f"Loading operator config: {args.operator_config}")
    op_config = load_operator_config(args.operator_config)
    print(f"Operator: {op_config.get('operator', 'unknown')}")

    # Load seed manifest
    print(f"Loading seed manifest: {args.seed_image_manifest}")
    seed_meta, seed_static = load_seed_manifest(args.seed_image_manifest, args.target)

    # Load pre-resolved versions
    with open(args.versions_file) as f:
        versions = json.load(f)
    print(f"Versions: {json.dumps(versions)}")

    # Build manifest
    print("\nBuilding manifest...")
    manifest = build_manifest(args, op_config, versions, seed_meta, seed_static)

    # Write output
    write_manifest(manifest, args.output)

    # Summary
    target_entries = manifest["images"][args.target]
    print(f"\n{'='*70}")
    print(f"Image Manifest Summary (target: {args.target})")
    print(f"{'='*70}")
    for name, info in target_entries.items():
        ver = info.get("version", "—")
        loc = info.get("image", info.get("location", ""))
        scheme = loc.split("://")[0] if "://" in loc else "?"
        print(f"  {name:40s} {ver:25s} [{scheme}]")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
