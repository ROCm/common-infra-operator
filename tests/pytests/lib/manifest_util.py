#
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""
Image manifest parsing and validation utilities.

Extracted from conftest.py to enable both pytest fixture usage and standalone
CI validation of generated manifests. This is the single source of truth for
what constitutes a valid image manifest entry and how entries are transformed
into the image_info dict consumed by test fixtures.

Usage (CI validation):
    from lib.manifest_util import validate_manifest
    errors = validate_manifest(manifest_dict, target="k8")

Usage (pytest):
    from lib.manifest_util import build_image_info
    image_info, errors = build_image_info(images, "k8")
    if errors:
        pytest.fail("\\n".join(errors))
"""

import os
import re
from urllib.parse import urlparse


# Required keys per entry kind.  'location' is required for all kinds except
# olm-subscription (which uses catalog/channel/package instead).
_REQUIRED_KEYS = {
    "olm-subscription": [],
    "qcow2":            ["location"],
    "helm-chart":       ["location", "version"],
    "olm-bundle":       ["location", "version"],
    "debian":           ["location", "version"],
    "container":        ["location", "key"],
}

# At least one of these must be present for olm-subscription entries.
_OLM_SUB_FIELDS = ("catalog", "channel", "package", "csv")


def validate_manifest_entry(name, entry):
    """Validate a single manifest entry has the required keys.

    Returns a list of error strings (empty if valid).
    """
    errors = []
    if not isinstance(entry, dict):
        errors.append(f"{name}: entry is not a dict")
        return errors

    kind = entry.get("kind")
    if not kind:
        errors.append(f"{name}: missing 'kind' field")
        return errors

    required = _REQUIRED_KEYS.get(kind)
    if required is None:
        errors.append(f"{name}: unknown kind '{kind}'")
        return errors

    for key in required:
        if key not in entry:
            errors.append(f"{name}: missing required key '{key}' for kind '{kind}'")

    if kind == "olm-subscription":
        if not any(f in entry for f in _OLM_SUB_FIELDS):
            errors.append(f"{name}: olm-subscription needs at least one of {_OLM_SUB_FIELDS}")

    location = entry.get("location", "")
    if location:
        known_schemes = ("container://", "file://", "oci://", "repo://", "http://", "https://")
        if not any(location.startswith(s) for s in known_schemes):
            errors.append(f"{name}: unrecognized location scheme in '{location}'")

    return errors


def validate_manifest(manifest, target):
    """Validate a full manifest dict for a given target.

    Returns a list of error strings (empty = valid).
    """
    errors = []

    if not isinstance(manifest, dict):
        errors.append("manifest is not a dict")
        return errors

    images = manifest.get("images")
    if not images:
        errors.append("missing 'images' key in manifest")
        return errors

    if target not in images:
        errors.append(f"missing target '{target}' under images")
        return errors

    target_entries = images[target]
    if not isinstance(target_entries, dict):
        errors.append(f"images.{target} is not a dict")
        return errors

    for name, entry in target_entries.items():
        errors.extend(validate_manifest_entry(name, entry))

    return errors


def build_image_info(images, deployment_mode, download_folder="downloads",
                     default_registry="docker.io"):
    """Build the image_info dict from a parsed manifest's images section.

    This is the core parsing loop extracted from conftest.py's _build_image_info.
    It transforms manifest entries into the flat key-value dict that test fixtures
    consume (e.g. 'metricsExporter.image.repository', 'gpu-operator.olm-bundle').

    Args:
        images: The 'images' section of the manifest (includes 'meta' and target dicts).
        deployment_mode: Target name ('k8', 'openshift', 'standalone', 'hypervisor').
        download_folder: Directory for local file references.
        default_registry: Default container registry for <registry> substitution.

    Returns:
        (image_info, errors) tuple. image_info is the parsed dict (may be partial
        if errors occurred). errors is a list of error strings (empty = success).
    """
    errors = []

    if deployment_mode not in images:
        return {}, [f"Missing deployment mode '{deployment_mode}' in manifest"]

    target_images = images[deployment_mode]
    image_info = {}

    os.makedirs(download_folder, exist_ok=True)
    image_info['image_folder'] = download_folder

    for artifact, artifact_info in target_images.items():
        if not isinstance(artifact_info, dict):
            continue

        kind = artifact_info.get('kind', '')

        if kind == 'olm-subscription':
            for field in ('catalog', 'channel', 'package', 'csv'):
                if field in artifact_info:
                    image_info[f'{artifact}.olm-subscription.{field}'] = artifact_info[field]
            image_info[f'{artifact}.olm-subscription'] = True
            continue

        if kind == 'qcow2':
            location = artifact_info.get('location')
            if not location:
                errors.append(f"{artifact}: qcow2 entry missing 'location'")
                continue
            image_info[f'{artifact}.qcow2-base-url'] = location
            continue

        location = artifact_info.get('location', '')

        if not location and kind not in ('olm-subscription',):
            errors.append(f"{artifact}: missing 'location' for kind '{kind}'")
            continue

        if 'oci://' in location:
            pattern = r"oci://([a-zA-Z0-9.-]+(?::\d+)?/[^:]+?)(?::([^/]+))?$"
            match = re.search(pattern, location)
            if not match:
                errors.append(f"{artifact}: failed to parse OCI chart from '{location}'")
                continue
            image_info[f'{artifact}.helm-chart'] = f"oci://{match.group(1)}"
            if artifact_info.get('version'):
                image_info[f'{artifact}.version'] = artifact_info['version']
            if 'secret' in artifact_info:
                image_info[f'{artifact}.secret'] = artifact_info['secret']

        elif 'repo://' in location:
            pattern = r"repo://([a-zA-Z0-9.-]+/[^:]+):([^/]+)"
            match = re.search(pattern, location)
            if not match:
                errors.append(f"{artifact}: failed to parse repo from '{location}'")
                continue
            image_info[f'{artifact}.repo-name'] = f"{artifact}-repo"
            image_info[f'{artifact}.repo'] = f"https://{match.group(1)}"
            image_info[f'{artifact}.repository'] = f"https://{match.group(1)}"
            image_info[f'{artifact}.helm-chart'] = f"{artifact}-repo/{match.group(2)}"
            if artifact_info.get('version'):
                image_info[f'{artifact}.version'] = artifact_info['version']
            if 'secret' in artifact_info:
                image_info[f'{artifact}.secret'] = artifact_info['secret']

        elif 'file://' in location:
            local_file = location.split('file://')[-1]
            file_path = local_file
            if kind == 'helm-chart':
                image_info[f'{artifact}.helm-chart'] = file_path
                image_info[f'{artifact}.helm-chart.version'] = artifact_info['version']
                image_info[f'{artifact}.helm-chart.repository'] = file_path
            elif kind == 'olm-bundle':
                image_info[f'{artifact}.olm-bundle'] = file_path
                image_info[f'{artifact}.olm-bundle.version'] = artifact_info['version']
                image_info[f'{artifact}.olm-bundle.repository'] = file_path
                if 'secret' in artifact_info:
                    image_info[f"{artifact}.olm-bundle.secret"] = artifact_info['secret']
            elif kind == 'debian':
                image_info[f'{artifact}.debian'] = file_path
                image_info[f'{artifact}.debian.version'] = artifact_info['version']
                image_info[f'{artifact}.debian.repository'] = file_path

        elif 'http://' in location or 'https://' in location:
            url = location
            local_file = os.path.join(download_folder, os.path.basename(urlparse(url).path))
            file_path = local_file
            if kind == 'helm-chart':
                image_info[f'{artifact}.helm-chart'] = file_path
            elif kind == 'olm-bundle':
                image_info[f'{artifact}.olm-bundle'] = file_path
                if 'secret' in artifact_info:
                    image_info[f"{artifact}.olm-bundle.secret"] = artifact_info['secret']

        elif 'container://' in location:
            if '<registry>' in location and default_registry:
                url = location.replace('<registry>', default_registry)
            else:
                url = location
            parsed_data = urlparse(url)
            if kind == 'container':
                key = artifact_info.get('key', artifact)
                image_info[f"{key}.repository"] = f"{parsed_data.netloc}{parsed_data.path}"
                if artifact_info.get('version'):
                    image_info[f"{key}.version"] = artifact_info['version']
                if 'secret' in artifact_info:
                    image_info[f"{key}.secret"] = artifact_info['secret']
            elif kind == 'olm-bundle':
                version = artifact_info.get('version', '')
                image_info[f'{artifact}.olm-bundle'] = f"{parsed_data.netloc}{parsed_data.path}:{version}"
                image_info[f'{artifact}.olm-bundle.version'] = version
                image_info[f'{artifact}.olm-bundle.repository'] = f"{parsed_data.netloc}{parsed_data.path}"
                if 'secret' in artifact_info:
                    image_info[f'{artifact}.olm-bundle.secret'] = artifact_info['secret']

    return image_info, errors
