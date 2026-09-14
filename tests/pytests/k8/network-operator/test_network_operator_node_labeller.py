#!/usr/bin/env python3

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

import time
import yaml
import pytest
import logging
from typing import Dict, List

from kubernetes import client as k8s_client

import lib.k8_util as k8_util

from lib.nic_util import (
    list_networkconfigs_custom,
    patch_networkconfig_custom,
    replace_with_retry,
)

LOG = logging.getLogger(__name__)
TEST_TIMEOUT = 180

# ---------- Tests ----------

@pytest.mark.timeout(TEST_TIMEOUT)
def test_disable_node_labeller_removes_labels_pf():
    """
    Test that disabling node labeller removes node labels for PF (Physical Function) configs.
    
    Workflow:
    1) Fetch existing PF NetworkConfig objects (non-vf- prefix)
    2) Save originals
    3) Patch spec.devicePlugin.enableNodeLabeller to False
    4) Wait for reconciliation
    5) Verify that node labels are removed:
       - amd.com/nic.driver-name
       - amd.com/nic.driver-version
       - amd.com/nic.product-name
    6) Restore original NetworkConfig objects
    """
    v1 = k8s_client.CoreV1Api()
    
    nc_namespace = "kube-amd-network"
    
    # 1) List NetworkConfig CRs and save originals
    try:
        nc_items = list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources in {nc_namespace}: {e}")
    
    if not nc_items:
        pytest.skip(f"No NetworkConfig resources in {nc_namespace}")
    
    originals = {}
    for it in nc_items:
        name = it.get("metadata", {}).get("name")
        if not name or name.startswith("vf-"):
            continue
        originals[name] = yaml.safe_load(yaml.safe_dump(it))
    
    if not originals:
        pytest.skip("No PF NetworkConfig found")
    
    # Get nodes associated with PF NetworkConfigs
    nodes_by_config = {}
    for name in originals.keys():
        # Get node selector from NetworkConfig
        nc = originals[name]
        selector = nc.get("spec", {}).get("selector", {})
        
        # List nodes matching the selector
        try:
            if selector:
                # Convert selector dict to label selector string
                label_selector = ",".join([f"{k}={v}" for k, v in selector.items()])
                nodes = v1.list_node(label_selector=label_selector)
            else:
                nodes = v1.list_node()
            
            if nodes.items:
                nodes_by_config[name] = [n.metadata.name for n in nodes.items]
                LOG.info("PF NetworkConfig %s matches nodes: %s", name, nodes_by_config[name])
        except Exception as e:
            LOG.error("Failed to list nodes for PF config %s: %s", name, e)
    
    if not nodes_by_config:
        pytest.skip("No nodes found matching PF NetworkConfig selectors")
    
    # Collect initial labels from nodes
    initial_labels = {}
    expected_label_prefixes = [
        "amd.com/nic.driver-name",
        "amd.com/nic.driver-version",
        "amd.com/nic.product-name"
    ]
    
    for config_name, node_names in nodes_by_config.items():
        for node_name in node_names:
            try:
                node_labels = k8_util.k8_get_node_labels(node_name) or {}
                
                # Check for expected labels
                found_labels = {}
                for label_key in node_labels.keys():
                    for prefix in expected_label_prefixes:
                        if label_key.startswith(prefix):
                            found_labels[label_key] = node_labels[label_key]
                
                if found_labels:
                    initial_labels[node_name] = found_labels
                    LOG.info("PF Node %s has initial node labeller labels: %s", node_name, found_labels)
                else:
                    LOG.warning("PF Node %s does not have expected node labeller labels", node_name)
            except Exception as e:
                LOG.error("Failed to read PF node %s: %s", node_name, e)
    
    # 2) Build patch to disable node labeller
    patch_body = {
        "spec": {
            "devicePlugin": {
                "enableNodeLabeller": False
            }
        }
    }
    
    # 3) Apply patches
    applied = []
    failed = []
    for name in originals.keys():
        try:
            patch_networkconfig_custom(nc_namespace, name, patch_body)
            applied.append(name)
            LOG.info("PF Patched %s -> spec.devicePlugin.enableNodeLabeller=False", name)
        except Exception as e:
            LOG.error("Failed to patch PF %s: %s", name, e)
            failed.append(name)
    
    if failed:
        # rollback applied ones and fail early
        for rn in applied:
            try:
                replace_with_retry(nc_namespace, rn, originals[rn])
            except Exception as re:
                LOG.error("Rollback failed for PF %s: %s", rn, re)
        pytest.fail(f"Failed to apply node labeller disable patch to some PF NetworkConfigs: {failed}")
    
    # 4) Wait for operator to reconcile and remove labels (increase if needed)
    LOG.info("PF Waiting for operator to reconcile and remove node labels...")
    time.sleep(30)
    
    # 5) Verify labels are removed
    labels_still_present = {}
    labels_removed = {}
    
    for node_name, expected_labels in initial_labels.items():
        try:
            current_labels = k8_util.k8_get_node_labels(node_name) or {}
            
            # Check which labels are still present
            still_present = {}
            for label_key in expected_labels.keys():
                if label_key in current_labels:
                    still_present[label_key] = current_labels[label_key]
            
            if still_present:
                labels_still_present[node_name] = still_present
                LOG.error("PF Node %s still has node labeller labels: %s", node_name, still_present)
            else:
                labels_removed[node_name] = expected_labels
                LOG.info("PF Node %s labels successfully removed: %s", node_name, list(expected_labels.keys()))
        except Exception as e:
            LOG.error("Failed to read PF node %s for verification: %s", node_name, e)
    
    # 6) Restore all modified PF NetworkConfig objects
    revert_errors = []
    for rn, orig in originals.items():
        try:
            replace_with_retry(nc_namespace, rn, orig)
            LOG.info("Restored original PF NetworkConfig %s", rn)
        except Exception as e:
            LOG.error("Failed to restore PF %s: %s", rn, e)
            revert_errors.append((rn, str(e)))
    
    if revert_errors:
        LOG.error("One or more PF NetworkConfig originals failed to revert: %s", revert_errors)
        pytest.fail(f"Failed to revert original PF NetworkConfig(s): {revert_errors}")
    
    # Final verification
    if labels_still_present:
        LOG.error("PF Node labeller labels were not removed from nodes: %s", labels_still_present)
        pytest.fail(f"PF Node labeller labels still present on nodes after disabling: {labels_still_present}")
    
    if not labels_removed:
        LOG.warning("No PF nodes had node labeller labels to remove (they may not have been set initially)")
    
    LOG.info("PF Successfully verified that node labeller labels were removed from %d nodes", len(labels_removed))


@pytest.mark.timeout(TEST_TIMEOUT)
def test_enable_node_labeller_adds_labels_pf():
    """
    Test that enabling node labeller adds node labels for PF (Physical Function) configs.
    
    Workflow:
    1) Fetch existing PF NetworkConfig objects (non-vf- prefix)
    2) Save originals
    3) Patch spec.devicePlugin.enableNodeLabeller to True
    4) Wait for reconciliation
    5) Verify that node labels are present:
       - amd.com/nic.driver-name
       - amd.com/nic.driver-version
       - amd.com/nic.product-name
    6) Restore original NetworkConfig objects
    """
    v1 = k8s_client.CoreV1Api()
    
    nc_namespace = "kube-amd-network"
    
    # 1) List NetworkConfig CRs and save originals
    try:
        nc_items = list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources in {nc_namespace}: {e}")
    
    if not nc_items:
        pytest.skip(f"No NetworkConfig resources in {nc_namespace}")
    
    originals = {}
    for it in nc_items:
        name = it.get("metadata", {}).get("name")
        if not name or name.startswith("vf-"):
            continue
        originals[name] = yaml.safe_load(yaml.safe_dump(it))
    
    if not originals:
        pytest.skip("No PF NetworkConfig found")
    
    # Get nodes associated with PF NetworkConfigs
    nodes_by_config = {}
    for name in originals.keys():
        # Get node selector from NetworkConfig
        nc = originals[name]
        selector = nc.get("spec", {}).get("selector", {})
        
        # List nodes matching the selector
        try:
            if selector:
                # Convert selector dict to label selector string
                label_selector = ",".join([f"{k}={v}" for k, v in selector.items()])
                nodes = v1.list_node(label_selector=label_selector)
            else:
                nodes = v1.list_node()
            
            if nodes.items:
                nodes_by_config[name] = [n.metadata.name for n in nodes.items]
                LOG.info("PF NetworkConfig %s matches nodes: %s", name, nodes_by_config[name])
        except Exception as e:
            LOG.error("Failed to list nodes for PF config %s: %s", name, e)
    
    if not nodes_by_config:
        pytest.skip("No nodes found matching PF NetworkConfig selectors")
    
    # 2) Build patch to enable node labeller
    patch_body = {
        "spec": {
            "devicePlugin": {
                "enableNodeLabeller": True
            }
        }
    }
    
    # 3) Apply patches
    applied = []
    failed = []
    for name in originals.keys():
        try:
            patch_networkconfig_custom(nc_namespace, name, patch_body)
            applied.append(name)
            LOG.info("PF Patched %s -> spec.devicePlugin.enableNodeLabeller=True", name)
        except Exception as e:
            LOG.error("Failed to patch PF %s: %s", name, e)
            failed.append(name)
    
    if failed:
        # rollback applied ones and fail early
        for rn in applied:
            try:
                replace_with_retry(nc_namespace, rn, originals[rn])
            except Exception as re:
                LOG.error("Rollback failed for PF %s: %s", rn, re)
        pytest.fail(f"Failed to apply node labeller enable patch to some PF NetworkConfigs: {failed}")
    
    # 4) Wait for operator to reconcile and add labels (increase if needed)
    LOG.info("PF Waiting for operator to reconcile and add node labels...")
    time.sleep(30)
    
    # 5) Verify labels are present
    expected_label_prefixes = [
        "amd.com/nic.driver-name",
        "amd.com/nic.driver-version",
        "amd.com/nic.product-name"
    ]
    
    labels_found = {}
    labels_missing = {}
    
    for config_name, node_names in nodes_by_config.items():
        for node_name in node_names:
            try:
                node_labels = k8_util.k8_get_node_labels(node_name) or {}
                
                # Check for expected labels
                found_labels = {}
                missing_labels = []
                
                for prefix in expected_label_prefixes:
                    found = False
                    for label_key in node_labels.keys():
                        if label_key.startswith(prefix):
                            found_labels[label_key] = node_labels[label_key]
                            found = True
                            break
                    if not found:
                        missing_labels.append(prefix)
                
                if found_labels:
                    labels_found[node_name] = found_labels
                    LOG.info("PF Node %s has node labeller labels: %s", node_name, found_labels)
                
                if missing_labels:
                    labels_missing[node_name] = missing_labels
                    LOG.error("PF Node %s is missing expected labels: %s", node_name, missing_labels)
            except Exception as e:
                LOG.error("Failed to read PF node %s: %s", node_name, e)
    
    # 6) Restore all modified PF NetworkConfig objects
    revert_errors = []
    for rn, orig in originals.items():
        try:
            replace_with_retry(nc_namespace, rn, orig)
            LOG.info("Restored original PF NetworkConfig %s", rn)
        except Exception as e:
            LOG.error("Failed to restore PF %s: %s", rn, e)
            revert_errors.append((rn, str(e)))
    
    if revert_errors:
        LOG.error("One or more PF NetworkConfig originals failed to revert: %s", revert_errors)
        pytest.fail(f"Failed to revert original PF NetworkConfig(s): {revert_errors}")
    
    # Final verification
    if labels_missing:
        LOG.error("PF Node labeller labels are missing from nodes: %s", labels_missing)
        pytest.fail(f"PF Node labeller labels missing from nodes after enabling: {labels_missing}")
    
    if not labels_found:
        pytest.fail("No PF node labeller labels found on any PF nodes after enabling")
    
    LOG.info("PF Successfully verified that node labeller labels are present on %d nodes", len(labels_found))


@pytest.mark.timeout(TEST_TIMEOUT)
def test_disable_node_labeller_removes_labels_vf():
    """
    Test that disabling node labeller removes node labels for VF (Virtual Function) configs.
    
    Workflow:
    1) Fetch existing VF NetworkConfig objects (vf- prefix)
    2) Save originals
    3) Patch spec.devicePlugin.enableNodeLabeller to False
    4) Wait for reconciliation
    5) Verify that node labels are removed:
       - amd.com/nic.driver-name
       - amd.com/nic.driver-version
       - amd.com/nic.product-name
    6) Restore original NetworkConfig objects
    """
    v1 = k8s_client.CoreV1Api()
    
    nc_namespace = "kube-amd-network"
    
    # 1) List NetworkConfig CRs and save originals (VF only)
    try:
        nc_items = list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources in {nc_namespace}: {e}")
    
    if not nc_items:
        pytest.skip(f"No NetworkConfig resources in {nc_namespace}")
    
    originals = {}
    for it in nc_items:
        name = it.get("metadata", {}).get("name")
        if not name or not name.startswith("vf-"):
            continue
        originals[name] = yaml.safe_load(yaml.safe_dump(it))
    
    if not originals:
        pytest.skip("No VF NetworkConfig found")
    
    # Get nodes associated with VF NetworkConfigs
    nodes_by_config = {}
    for name in originals.keys():
        # Get node selector from NetworkConfig
        nc = originals[name]
        selector = nc.get("spec", {}).get("selector", {})
        
        # List nodes matching the selector
        try:
            if selector:
                # Convert selector dict to label selector string
                label_selector = ",".join([f"{k}={v}" for k, v in selector.items()])
                nodes = v1.list_node(label_selector=label_selector)
            else:
                nodes = v1.list_node()
            
            if nodes.items:
                nodes_by_config[name] = [n.metadata.name for n in nodes.items]
                LOG.info("VF NetworkConfig %s matches nodes: %s", name, nodes_by_config[name])
        except Exception as e:
            LOG.error("Failed to list nodes for VF config %s: %s", name, e)
    
    if not nodes_by_config:
        pytest.skip("No nodes found matching VF NetworkConfig selectors")
    
    # Collect initial labels from nodes
    initial_labels = {}
    expected_label_prefixes = [
        "amd.com/nic.driver-name",
        "amd.com/nic.driver-version",
        "amd.com/nic.product-name"
    ]
    
    for config_name, node_names in nodes_by_config.items():
        for node_name in node_names:
            try:
                node_labels = k8_util.k8_get_node_labels(node_name) or {}
                
                # Check for expected labels
                found_labels = {}
                for label_key in node_labels.keys():
                    for prefix in expected_label_prefixes:
                        if label_key.startswith(prefix):
                            found_labels[label_key] = node_labels[label_key]
                
                if found_labels:
                    initial_labels[node_name] = found_labels
                    LOG.info("VF Node %s has initial node labeller labels: %s", node_name, found_labels)
                else:
                    LOG.warning("VF Node %s does not have expected node labeller labels", node_name)
            except Exception as e:
                LOG.error("Failed to read VF node %s: %s", node_name, e)
    
    # 2) Build patch to disable node labeller
    patch_body = {
        "spec": {
            "devicePlugin": {
                "enableNodeLabeller": False
            }
        }
    }
    
    # 3) Apply patches
    applied = []
    failed = []
    for name in originals.keys():
        try:
            patch_networkconfig_custom(nc_namespace, name, patch_body)
            applied.append(name)
            LOG.info("VF Patched %s -> spec.devicePlugin.enableNodeLabeller=False", name)
        except Exception as e:
            LOG.error("Failed to patch VF %s: %s", name, e)
            failed.append(name)
    
    if failed:
        # rollback applied ones and fail early
        for rn in applied:
            try:
                replace_with_retry(nc_namespace, rn, originals[rn])
            except Exception as re:
                LOG.error("Rollback failed for %s: %s", rn, re)
        pytest.fail(f"Failed to apply node labeller disable patch to some VF NetworkConfigs: {failed}")
    
    # 4) Wait for operator to reconcile and remove labels (increase if needed)
    LOG.info("VF Waiting for operator to reconcile and remove node labels...")
    time.sleep(30)
    
    # 5) Verify labels are removed
    labels_still_present = {}
    labels_removed = {}
    
    for node_name, expected_labels in initial_labels.items():
        try:
            current_labels = k8_util.k8_get_node_labels(node_name) or {}
            
            # Check which labels are still present
            still_present = {}
            for label_key in expected_labels.keys():
                if label_key in current_labels:
                    still_present[label_key] = current_labels[label_key]
            
            if still_present:
                labels_still_present[node_name] = still_present
                LOG.error("VF Node %s still has node labeller labels: %s", node_name, still_present)
            else:
                labels_removed[node_name] = expected_labels
                LOG.info("VF Node %s labels successfully removed: %s", node_name, list(expected_labels.keys()))
        except Exception as e:
            LOG.error("Failed to read VF node %s for verification: %s", node_name, e)
    
    # 6) Restore all modified VF NetworkConfig objects
    revert_errors = []
    for rn, orig in originals.items():
        try:
            replace_with_retry(nc_namespace, rn, orig)
            LOG.info("Restored original VF NetworkConfig %s", rn)
        except Exception as e:
            LOG.error("Failed to restore VF %s: %s", rn, e)
            revert_errors.append((rn, str(e)))
    
    if revert_errors:
        LOG.error("One or more VF NetworkConfig originals failed to revert: %s", revert_errors)
        pytest.fail(f"Failed to revert original VF NetworkConfig(s): {revert_errors}")
    
    # Final verification
    if labels_still_present:
        LOG.error("VF Node labeller labels were not removed from nodes: %s", labels_still_present)
        pytest.fail(f"VF Node labeller labels still present on nodes after disabling: {labels_still_present}")
    
    if not labels_removed:
        LOG.warning("No VF nodes had node labeller labels to remove (they may not have been set initially)")
    
    LOG.info("VF Successfully verified that node labeller labels were removed from %d nodes", len(labels_removed))


@pytest.mark.timeout(TEST_TIMEOUT)
def test_enable_node_labeller_adds_labels_vf():
    """
    Test that enabling node labeller adds node labels for VF (Virtual Function) configs.
    
    Workflow:
    1) Fetch existing VF NetworkConfig objects (vf- prefix)
    2) Save originals
    3) Patch spec.devicePlugin.enableNodeLabeller to True
    4) Wait for reconciliation
    5) Verify that node labels are present:
       - amd.com/nic.driver-name
       - amd.com/nic.driver-version
       - amd.com/nic.product-name
    6) Restore original NetworkConfig objects
    """
    v1 = k8s_client.CoreV1Api()
    
    nc_namespace = "kube-amd-network"
    
    # 1) List NetworkConfig CRs and save originals (VF only)
    try:
        nc_items = list_networkconfigs_custom(nc_namespace)
    except Exception as e:
        pytest.skip(f"Could not list NetworkConfig resources in {nc_namespace}: {e}")
    
    if not nc_items:
        pytest.skip(f"No NetworkConfig resources in {nc_namespace}")
    
    originals = {}
    for it in nc_items:
        name = it.get("metadata", {}).get("name")
        if not name or not name.startswith("vf-"):
            continue
        originals[name] = yaml.safe_load(yaml.safe_dump(it))
    
    if not originals:
        pytest.skip("No VF NetworkConfig found")
    
    # Get nodes associated with VF NetworkConfigs
    nodes_by_config = {}
    for name in originals.keys():
        # Get node selector from NetworkConfig
        nc = originals[name]
        selector = nc.get("spec", {}).get("selector", {})
        
        # List nodes matching the selector
        try:
            if selector:
                # Convert selector dict to label selector string
                label_selector = ",".join([f"{k}={v}" for k, v in selector.items()])
                nodes = v1.list_node(label_selector=label_selector)
            else:
                nodes = v1.list_node()
            
            if nodes.items:
                nodes_by_config[name] = [n.metadata.name for n in nodes.items]
                LOG.info("VF NetworkConfig %s matches nodes: %s", name, nodes_by_config[name])
        except Exception as e:
            LOG.error("Failed to list nodes for VF config %s: %s", name, e)
    
    if not nodes_by_config:
        pytest.skip("No nodes found matching VF NetworkConfig selectors")
    
    # 2) Build patch to enable node labeller
    patch_body = {
        "spec": {
            "devicePlugin": {
                "enableNodeLabeller": True
            }
        }
    }
    
    # 3) Apply patches
    applied = []
    failed = []
    for name in originals.keys():
        try:
            patch_networkconfig_custom(nc_namespace, name, patch_body)
            applied.append(name)
            LOG.info("VF Patched %s -> spec.devicePlugin.enableNodeLabeller=True", name)
        except Exception as e:
            LOG.error("Failed to patch VF %s: %s", name, e)
            failed.append(name)
    
    if failed:
        # rollback applied ones and fail early
        for rn in applied:
            try:
                replace_with_retry(nc_namespace, rn, originals[rn])
            except Exception as re:
                LOG.error("Rollback failed for %s: %s", rn, re)
        pytest.fail(f"Failed to apply node labeller enable patch to some VF NetworkConfigs: {failed}")
    
    # 4) Wait for operator to reconcile and add labels (increase if needed)
    LOG.info("VF Waiting for operator to reconcile and add node labels...")
    time.sleep(30)
    
    # 5) Verify labels are present
    expected_label_prefixes = [
        "amd.com/nic.driver-name",
        "amd.com/nic.driver-version",
        "amd.com/nic.product-name"
    ]
    
    labels_found = {}
    labels_missing = {}
    
    for config_name, node_names in nodes_by_config.items():
        for node_name in node_names:
            try:
                node_labels = k8_util.k8_get_node_labels(node_name) or {}
                
                # Check for expected labels
                found_labels = {}
                missing_labels = []
                
                for prefix in expected_label_prefixes:
                    found = False
                    for label_key in node_labels.keys():
                        if label_key.startswith(prefix):
                            found_labels[label_key] = node_labels[label_key]
                            found = True
                            break
                    if not found:
                        missing_labels.append(prefix)
                
                if found_labels:
                    labels_found[node_name] = found_labels
                    LOG.info("VF Node %s has node labeller labels: %s", node_name, found_labels)
                
                if missing_labels:
                    labels_missing[node_name] = missing_labels
                    LOG.error("VF Node %s is missing expected labels: %s", node_name, missing_labels)
            except Exception as e:
                LOG.error("Failed to read VF node %s: %s", node_name, e)
    
    # 6) Restore all modified VF NetworkConfig objects
    revert_errors = []
    for rn, orig in originals.items():
        try:
            replace_with_retry(nc_namespace, rn, orig)
            LOG.info("Restored original VF NetworkConfig %s", rn)
        except Exception as e:
            LOG.error("Failed to restore VF %s: %s", rn, e)
            revert_errors.append((rn, str(e)))
    
    if revert_errors:
        LOG.error("One or more VF NetworkConfig originals failed to revert: %s", revert_errors)
        pytest.fail(f"Failed to revert original VF NetworkConfig(s): {revert_errors}")
    
    # Final verification
    if labels_missing:
        LOG.error("VF Node labeller labels are missing from nodes: %s", labels_missing)
        pytest.fail(f"VF Node labeller labels missing from nodes after enabling: {labels_missing}")
    
    if not labels_found:
        pytest.fail("No VF node labeller labels found on any nodes after enabling")
    
    LOG.info("VF Successfully verified that node labeller labels are present on %d nodes", len(labels_found))
