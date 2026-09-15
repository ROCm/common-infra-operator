#!/usr/bin/python3
'''
 Copyright (c) Advanced Micro Devices, Inc. All rights reserved.

 Licensed under the Apache License, Version 2.0 (the \"License\");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at

      http://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an \"AS IS\" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
'''

import pdb
import logging
import json
import os
import time
import lib.k8_util as k8_util
import lib.helm_util as helm_util
from kubernetes import client

# Configuration settings
NPD_NAMESPACE = "node-problem-detector" #"kube-system"
NPD_APP_NAME = "node-problem-detector"
NPD_SA_NAME = "npd-service-account"
NPD_ROLE_NAME = "npd-amdgpu-role"
NPD_ROLE_BINDING_NAME = "npd-amdgpu-role-binding"

DEFAULT_NPD_DAEMONSET = {
    "apiVersion": "apps/v1",
    "kind": "DaemonSet",
    "metadata": {"name": NPD_APP_NAME, "namespace": NPD_NAMESPACE},
    "spec": {
        "selector": {"matchLabels": {"app": NPD_APP_NAME}},
        "template": {
            "metadata": {"labels": {"app": NPD_APP_NAME}},
            "spec": {
                "serviceAccountName": NPD_SA_NAME,
                "containers": [{
                    "name": NPD_APP_NAME,
                    "image": "registry.k8s.io/node-problem-detector/node-problem-detector:v0.8.15",
                    "args": [
                        "--logtostderr",
                    ],
                    "securityContext": {"privileged": True},
                    "env": [
                        {
                            "name": "NODE_NAME",
                            "valueFrom": {
                                "fieldRef": {
                                    "fieldPath": "spec.nodeName"
                                }
                            }
                        }
                    ],
                    "volumeMounts": [
                        {
                            "name": "config",
                            "mountPath": "/config",
                            "readOnly": True
                        },
                        {
                            "name": "log",
                            "mountPath": "/var/log",
                            "readOnly": True
                        },
                    ]
                }],
                "volumes": [
                    {
                        "name": "log",
                        "hostPath": {
                            "path": "/var/log"
                        }
                    },
                    {
                        "name": "config",
                        "configMap": {
                            "name": f"{NPD_APP_NAME}-config"
                        }
                    }
                ]
            }
        }
    }
}

# Default configmap defn
# --- 2. CONFIGMAP: The GPU Plugin ---
# Empty/minimal configs for default monitors to prevent NPD from failing
# NPD tries to load kernel-monitor.json and system-log-monitor.json by default
DEFAULT_EMPTY_LOG_MONITOR_CONFIG = {
    "plugin": "journald",
    "pluginConfig": {
        "source": "journald"
    },
    "logPath": "/var/log/journal",
    "lookback": "5m",
    "rules": []  # No rules = no-op
}

DEFAULT_NPD_CONFIGMAP = {
    "apiVersion": "v1",
    "kind": "ConfigMap",
    "metadata": {"name": f"{NPD_APP_NAME}-config", "namespace": NPD_NAMESPACE},
    "data": {
        "kernel-monitor.json": json.dumps(DEFAULT_EMPTY_LOG_MONITOR_CONFIG, indent=2),
        "system-log-monitor.json": json.dumps(DEFAULT_EMPTY_LOG_MONITOR_CONFIG, indent=2)
    }
}

Logger = logging.getLogger("lib.npd")

def _run_tasks(task_list, stop_on_failure = True) -> int:
    final_ret_code = 0
    for cb_func, error_msg, args in task_list:
        ret_code, ret_stdout, ret_stderr = cb_func(*args)
        if ret_code != 0:
            Logger.warning(f"{error_msg}, error : {ret_stderr}")
            if stop_on_failure:
                return ret_code
            else:
                final_ret_code = ret_code
    return final_ret_code

def init_npd_k8(gpu_cluster, environment) -> (int, str, str):
    """
    API to configure default service-account and rbac
    """

    # Lets cleanup for any trace from previous deployment
    fini_npd_k8(gpu_cluster, environment)
    # --- 1. RBAC: ServiceAccount ---

    rules = list()
    rules.append(k8_util.k8_create_rules_from_verbs(resources=["nodes", "pods", "services"], verbs=["get", "list", "watch"], api_groups=[""]))
    rules.append(k8_util.k8_create_rules_from_verbs(resources=["events"], verbs=["create", "patch"], api_groups=[""]))
    rules.append(k8_util.k8_create_rules_from_verbs(resources=["nodes/status"], verbs=["patch"], api_groups=[""]))
    rules.extend(k8_util.k8_create_rules_from_endpoint_list([("/metrics", "get"), ("/gpumetrics", "get"), ("/inbandraserrors", "get")]))
    todo_tasks = [
        (k8_util.k8_create_namespace, f"Failed to create namespace : {NPD_NAMESPACE}", (NPD_NAMESPACE,)),
        (k8_util.k8_create_service_account, f"Failed to create service-account {NPD_APP_NAME}", (NPD_SA_NAME, NPD_NAMESPACE,)),
        (k8_util.k8_create_cluster_role, f"Failed to create cluster-roles", (NPD_ROLE_NAME, rules,)),
        (k8_util.k8_create_role_binding, "Failed to create npd-role binding",
         (NPD_ROLE_BINDING_NAME, NPD_NAMESPACE, NPD_ROLE_NAME, NPD_SA_NAME,))
    ]

    ret_code = _run_tasks(todo_tasks)
    if ret_code != 0:
        return ret_code, "", "Failed to setup/create service-account, roles and role-binding"


    # --- 3. DAEMONSET: The NPD Workload ---
    Logger.info(f"Deploy/Configure node-problem-detector with default config-map")
    # TODO: Dump the DEFAULT_NPD_CONFIGMAP under log folder and 

    default_cfgmap_file = os.path.join(environment.logdir, "npd_default_configmap.json")
    with open(default_cfgmap_file, "w") as fp:
        json.dump(DEFAULT_NPD_CONFIGMAP, fp, indent=4)

    todo_tasks = [
        (k8_util.k8_create_configmap, "Failed to init npd config-map",
         (NPD_NAMESPACE, DEFAULT_NPD_CONFIGMAP["metadata"]["name"],
          default_cfgmap_file, DEFAULT_NPD_CONFIGMAP["metadata"]["name"],)),
        (k8_util.k8_patch_daemonset, "Failed to apply/patch daemonset",
         (NPD_APP_NAME, NPD_NAMESPACE, DEFAULT_NPD_DAEMONSET,))
    ]
    ret_code = _run_tasks(todo_tasks)
    if ret_code != 0:
        return ret_code, "", "Failed to deploy/configure node-problem-detector with default config-map"

    Logger.info(f"Successfully deployed node-problem-detector {NPD_APP_NAME} in namespace {NPD_NAMESPACE}")
    return ret_code, "", ""

def fini_npd_k8(gpu_cluster, environment) -> (int, str, str):
    """
    API to remove/uninstall node-problem-detector and custom plugin
    """

    Logger.info(f"Removing npd amdgpuhealth custom-plugin from the cluster")

    cleanup_tasks = [
        (k8_util.k8_delete_configmap, "Failed to delete config-map", (NPD_NAMESPACE, f"{NPD_APP_NAME}-config",)),
        (k8_util.k8_delete_daemonset, "Failed to delete daemonset", (NPD_NAMESPACE, NPD_APP_NAME,))
    ]

    ret_code = _run_tasks(cleanup_tasks, stop_on_failure = False)
    if ret_code != 0:
        Logger.warning("Failed to delete npd config-map and daemonset - ignoring error for now")

    Logger.info(f"Removing npd service-account, cluster-role and role-bindings")

    cleanup_tasks = [
        (k8_util.k8_delete_cluster_role_binding, "Failed to delete cluster-role-binding", (NPD_ROLE_BINDING_NAME,)),
        (k8_util.k8_delete_cluster_role, "Failed to delete cluster-role", (NPD_ROLE_NAME,)),
        (k8_util.k8_delete_service_account, "Failed to delete service-account", (NPD_SA_NAME, NPD_NAMESPACE,)),
    ]

    ret_code = _run_tasks(cleanup_tasks, stop_on_failure = False)
    if ret_code != 0:
        Logger.warning("Failed to cleanup npd cluster role-binding/cluster-role/service-account - ignored")
    return 0, "", ""

def init_npd_oc(gpu_cluster, environment) -> (int, str, str):
    """
    oc create clusterrolebinding npd-privileged-scc \
    --clusterrole=system:openshift:scc:privileged \
    --serviceaccount=node-problem-detector:npd

    oc create clusterrole npd-pod-endpoint-access \
    --verb=get,list,watch --resource=pods,endpoints

    oc create clusterrolebinding npd-pod-endpoint-access-binding \
    --clusterrole=npd-pod-endpoint-access \
    --serviceaccount=node-problem-detector:npd

    helm install npd oci://ghcr.io/deliveryhero/helm-charts/node-problem-detector \
    --version 2.4.0 \
    -n node-problem-detector \
    --set serviceAccount.name=npd \
    --set serviceAccount.create=false
    """

    OCI_NPD_HELMCHART_URL = "oci://ghcr.io/deliveryhero/helm-charts/node-problem-detector"
    OCI_NPD_HELMCHART_VERSION = "2.4.0"

    rules = list()
    rules.append(k8_util.k8_create_rules_from_verbs(resources=["nodes", "pods", "services"], verbs=["get", "list", "watch"], api_groups=[""]))
    rules.append(k8_util.k8_create_rules_from_verbs(resources=["events"], verbs=["create", "patch"], api_groups=[""]))
    rules.append(k8_util.k8_create_rules_from_verbs(resources=["nodes/status"], verbs=["patch"], api_groups=[""]))
    todo_tasks = [
        (k8_util.k8_create_namespace, f"Failed to create namespace : {NPD_NAMESPACE}", (NPD_NAMESPACE,)),
        (k8_util.k8_create_service_account, f"Failed to create service-account {NPD_APP_NAME}", (NPD_SA_NAME, NPD_NAMESPACE,)),
        # Bind to existing system:openshift:scc:privileged ClusterRole instead of trying to create it
        (k8_util.k8_create_role_binding, "Failed to create cluster-role binding for privileged SCC",
         ("npd-scc-privileged-binding", NPD_NAMESPACE, "system:openshift:scc:privileged", NPD_SA_NAME, ))
    ]

    ret_code = _run_tasks(todo_tasks)
    if ret_code != 0:
        return ret_code, "", "Failed to init privileged roles"

    rules.extend(k8_util.k8_create_rules_from_endpoint_list([("/metrics", "get"), ("/gpumetrics", "get"), ("/inbandraserrors", "get")]))
    todo_tasks = [
        (k8_util.k8_create_cluster_role, f"Failed to create npd-role", (NPD_ROLE_NAME, rules,)),
        (k8_util.k8_create_role_binding, "Failed to create npd-role binding",
         (NPD_ROLE_BINDING_NAME, NPD_NAMESPACE, NPD_ROLE_NAME, NPD_SA_NAME,))
    ]

    ret_code = _run_tasks(todo_tasks)
    if ret_code != 0:
        return ret_code, "", "Failed to init endpoint access/roles"

    opts = {
      "serviceAccount.name" : NPD_SA_NAME,
      "serviceAccount.create" : "false",
    }
    ret_code, ret_stdout, ret_stderr = helm_util.helm_install(gpu_cluster, "npd",
                                                              NPD_NAMESPACE, OCI_NPD_HELMCHART_URL, OCI_NPD_HELMCHART_VERSION,
                                                              values_yaml = None, **opts)
    return ret_code, ret_stdout, ret_stderr

def fini_npd_oc(gpu_cluster, environment) -> (int, str, str):
    ret_code, ret_stdout, ret_stderr = helm_util.helm_uninstall(gpu_cluster, "npd", NPD_NAMESPACE)
    return ret_code, ret_stdout, ret_stderr

# (vol_name, mount_path, arg_template)
# Bearer token flags expect a file path; root-ca/client-cert flags expect a directory
# (amdgpuhealth reads ca.crt/tls.crt/tls.key by name from directories).
# The generic secret key for bearer tokens is "token", so we append "/token" to the mount path.
_AUTH_VOLUME_SPECS = {
    "exporter_bearer_token_secret": ("exporter-token", "/auth/exporter", "--exporter-bearer-token={}/token"),
    "exporter_root_ca_secret":      ("exporter-rootca", "/auth/exporter-ca", "--exporter-root-ca={}"),
    "client_cert_secret":           ("client-cert", "/auth/client-cert", "--client-cert={}"),
    "prometheus_bearer_token_secret": ("prometheus-token", "/auth/prometheus", "--prometheus-bearer-token={}/token"),
    "prometheus_root_ca_secret":    ("prometheus-rootca", "/auth/prometheus-ca", "--prometheus-root-ca={}"),
}

def _apply_auth_config(auth, rule_args, volumes, volume_mounts):
    """Add secret volumes, mounts, and CLI args for amdgpuhealth auth."""
    if auth.get("prometheus_endpoint"):
        rule_args.append(f"--prometheus-endpoint={auth['prometheus_endpoint']}")

    for key, (vol_name, mount_path, arg_template) in _AUTH_VOLUME_SPECS.items():
        secret_name = auth.get(key)
        if secret_name:
            rule_args.append(arg_template.format(mount_path))
            volumes.append({"name": vol_name, "secret": {"secretName": secret_name}})
            volume_mounts.append({"name": vol_name, "mountPath": mount_path, "readOnly": True})


def deploy_npd_custom_condition(environment, metric_type: str, metric_to_test: str, threshold: int,
                                 condition_type: str, reason_healthy: str, reason_problem: str,
                                 message_healthy: str, message_problem: str, invoke_interval: str = "30s",
                                 auth: dict = None):
    """
    Deploy NPD with a custom condition configuration.

    Args:
        environment: Test environment object
        metric_type: Type of metric query ("counter-metric" or "gauge-metric")
        metric_to_test: Prometheus metric name (e.g., "amd_gpu_gfx_activity")
        threshold: Threshold value for the metric
        condition_type: Kubernetes condition type (e.g., "AMDGPUHighUtilization")
        reason_healthy: Reason when condition is healthy
        reason_problem: Reason when condition indicates a problem
        message_healthy: Message when condition is healthy
        message_problem: Message when condition indicates a problem
        invoke_interval: How often to run the health check (default: "30s")
        auth: Optional auth configuration dict. Supported keys:
            exporter_bearer_token_secret - Secret with bearer token for exporter
            exporter_root_ca_secret      - Secret with ca.crt for exporter TLS
            client_cert_secret           - TLS Secret with tls.crt/tls.key for mTLS
            prometheus_endpoint          - Prometheus URL (e.g. "http://localhost:9090")
            prometheus_bearer_token_secret - Secret with bearer token for Prometheus
            prometheus_root_ca_secret    - Secret with ca.crt for Prometheus TLS

    Returns:
        int: 0 on success, non-zero on failure
    """
    rule_args = ["query", f"{metric_type}", f"-m={metric_to_test}", f"-t={threshold}"]
    extra_volumes = []
    extra_volume_mounts = []

    if auth:
        _apply_auth_config(auth, rule_args, extra_volumes, extra_volume_mounts)

    amdgpu_config = {
        "plugin": "custom",
        "pluginConfig": {
            "invoke_interval": invoke_interval,
            "timeout": "15s",
            "max_output_length": 80,
            "concurrency": 3,
            "enable_message_change_based_condition_update": False
        },
        "source": "amdgpu-custom-plugin-monitor",
        "metricsReporting": True,
        "conditions": [{
            "type": condition_type,
            "reason": reason_healthy,
            "message": message_healthy
        }],
        "rules": [{
            "type": "permanent",
            "condition": condition_type,
            "reason": reason_problem,
            "path": "/amd-metrics-exporter/amdgpuhealth",
            "args": rule_args,
            "timeout": "10s"
        }]
    }

    # Empty/minimal configs for default monitors to prevent NPD from failing
    # NPD tries to load these by default even when not explicitly configured
    empty_log_monitor_config = {
        "plugin": "journald",
        "pluginConfig": {
            "source": "journald"
        },
        "logPath": "/var/log/journal",
        "lookback": "5m",
        "rules": []  # No rules = no-op
    }

    cm_body = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": f"{NPD_APP_NAME}-config", "namespace": NPD_NAMESPACE},
        "data": {
            "amdgpuhealth.json": json.dumps(amdgpu_config, indent=2),
            "kernel-monitor.json": json.dumps(empty_log_monitor_config, indent=2),
            "system-log-monitor.json": json.dumps(empty_log_monitor_config, indent=2)
        }
    }

    base_volume_mounts = [
        {"name": "config", "mountPath": "/config", "readOnly": True},
        {"name": "log", "mountPath": "/var/log", "readOnly": True},
        {"name": "amd-metrics-exporter", "mountPath": "/amd-metrics-exporter", "readOnly": True}
    ]

    base_volumes = [
        {
            "name": "config",
            "configMap": {
                "name": f"{NPD_APP_NAME}-config",
                "defaultMode": 0o755,
                "items": [
                    {"key": "amdgpuhealth.json", "path": "amdgpuhealth.json", "mode": 0o644},
                    {"key": "kernel-monitor.json", "path": "kernel-monitor.json", "mode": 0o644},
                    {"key": "system-log-monitor.json", "path": "system-log-monitor.json", "mode": 0o644}
                ]
            }
        },
        {"name": "log", "hostPath": {"path": "/var/log"}},
        {"name": "amd-metrics-exporter", "hostPath": {"path": "/var/lib/amd-metrics-exporter"}}
    ]

    ds_body = {
        "apiVersion": "apps/v1",
        "kind": "DaemonSet",
        "metadata": {"name": NPD_APP_NAME, "namespace": NPD_NAMESPACE},
        "spec": {
            "selector": {"matchLabels": {"app": NPD_APP_NAME}},
            "template": {
                "metadata": {"labels": {"app": NPD_APP_NAME}},
                "spec": {
                    "nodeSelector": {"feature.node.kubernetes.io/amd-gpu": "true"},
                    "serviceAccountName": NPD_SA_NAME,
                    "containers": [{
                        "name": NPD_APP_NAME,
                        "image": "registry.k8s.io/node-problem-detector/node-problem-detector:v0.8.15",
                        "args": [
                            "--logtostderr",
                            "--config.custom-plugin-monitor=/config/amdgpuhealth.json"
                        ],
                        "securityContext": {"privileged": True},
                        "env": [
                            {
                                "name": "NODE_NAME",
                                "valueFrom": {
                                    "fieldRef": {
                                        "fieldPath": "spec.nodeName"
                                    }
                                }
                            }
                        ],
                        "volumeMounts": base_volume_mounts + extra_volume_mounts
                    }],
                    "volumes": base_volumes + extra_volumes
                }
            }
        }
    }

    Logger.info(f"Deploy NPD with custom condition: {condition_type} for metric {metric_to_test}")
    if extra_volumes:
        Logger.info(f"  Auth volumes: {[v['name'] for v in extra_volumes]}")

    todo_tasks = [
        (k8_util.k8_patch_config_map, "Failed to apply/patch config-map", (cm_body["metadata"]["name"], NPD_NAMESPACE, cm_body,)),
        (k8_util.k8_patch_daemonset, "Failed to apply/patch daemonset", (NPD_APP_NAME, NPD_NAMESPACE, ds_body,))
    ]

    ret_code = _run_tasks(todo_tasks)
    return ret_code

def remove_npd_amdgpuhealth_plugin(environment):
    """
    Remove NPD DaemonSet and ConfigMap to ensure clean state between tests.

    This function deletes the NPD DaemonSet and ConfigMap, forcing pods to be
    recreated when the next test deploys NPD with a new configuration. This
    ensures each test starts with a fresh NPD deployment that picks up the
    correct ConfigMap.
    """
    Logger.info(f"Remove/Restore node-problem-detector")

    cleanup_tasks = [
        (k8_util.k8_delete_daemonset, "Failed to delete daemonset", (NPD_NAMESPACE, NPD_APP_NAME,)),
        (k8_util.k8_delete_configmap, "Failed to delete config-map", (NPD_NAMESPACE, f"{NPD_APP_NAME}-config",)),
    ]

    ret_code = _run_tasks(cleanup_tasks, stop_on_failure=False)
    if ret_code == 0:
        Logger.info("Successfully removed NPD DaemonSet and ConfigMap")
    else:
        Logger.warning("Failed to remove some NPD resources - continuing anyway")
    return 0  # Always return success to avoid blocking test execution


def clear_node_condition(condition_type):
    """Remove a custom NPD condition from all GPU nodes."""
    from kubernetes import client
    from kubernetes.client.rest import ApiException
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    if ret_code != 0 or not gpu_nodes:
        Logger.warning(f"Could not get GPU nodes to clear condition {condition_type}")
        return
    v1 = client.CoreV1Api()
    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        try:
            node_obj = v1.read_node(node_name)
        except ApiException:
            continue
        conditions = node_obj.status.conditions or []
        new_conditions = [c for c in conditions
                          if c.type != condition_type]
        if len(new_conditions) == len(conditions):
            continue
        body = {"status": {"conditions": [
            {"type": c.type, "status": c.status, "reason": c.reason,
             "message": c.message,
             "lastHeartbeatTime": c.last_heartbeat_time,
             "lastTransitionTime": c.last_transition_time}
            for c in new_conditions
        ]}}
        ret, _, err = k8_util.k8_patch_node_status(node_name, body)
        if ret == 0:
            Logger.info(f"Cleared condition {condition_type} from node {node_name}")
        else:
            Logger.warning(f"Failed to clear condition {condition_type} from {node_name}: {err}")



def get_node_condition(node_name, condition_type):
    """
    Get a specific condition from a node's status.

    Args:
        node_name: Name of the node
        condition_type: Type of condition to retrieve (e.g., "AMDGPUProblem", "Ready")

    Returns:
        dict or None: Condition object with keys: type, status, reason, message, lastTransitionTime
                      Returns None if condition not found
    """
    api = client.CoreV1Api()

    try:
        node = api.read_node(name=node_name)

        if node.status and node.status.conditions:
            for condition in node.status.conditions:
                if condition.type == condition_type:
                    return {
                        'type': condition.type,
                        'status': condition.status,
                        'reason': condition.reason or "",
                        'message': condition.message or "",
                        'lastTransitionTime': condition.last_transition_time
                    }

        Logger.debug(f"Condition '{condition_type}' not found on node '{node_name}'")
        return None

    except Exception as e:
        Logger.error(f"Error reading node conditions: {e}")
        return None


def wait_for_npd_daemonset_ready(namespace, daemonset_name, timeout=300, interval=10):
    """
    Wait for NPD DaemonSet to be fully rolled out.

    A DaemonSet is considered ready when:
    - All desired pods are scheduled
    - All pods are available (running and ready)

    Enhanced diagnostics on failure:
    - Logs pod status (phase, ready conditions)
    - Collects pod logs for crash-looping containers
    - Reports pod events (warnings, errors)

    Args:
        namespace: Namespace where DaemonSet is deployed
        daemonset_name: Name of the DaemonSet
        timeout: Maximum time to wait in seconds (default: 300)
        interval: Check interval in seconds (default: 10)

    Returns:
        bool: True if DaemonSet is ready, False otherwise
    """
    api_apps = client.AppsV1Api()
    api_core = client.CoreV1Api()
    elapsed = 0

    while elapsed < timeout:
        try:
            ds = api_apps.read_namespaced_daemon_set(name=daemonset_name, namespace=namespace)

            # Check DaemonSet status
            desired = ds.status.desired_number_scheduled or 0
            current = ds.status.current_number_scheduled or 0
            ready = ds.status.number_ready or 0
            available = ds.status.number_available or 0

            Logger.info(f"DaemonSet {daemonset_name}: desired={desired}, current={current}, ready={ready}, available={available}")

            # DaemonSet is ready when all desired pods are available and ready
            if desired > 0 and desired == current == ready == available:
                Logger.info(f"DaemonSet {daemonset_name} is fully rolled out")
                return True

            # Enhanced diagnostics: check pod status if not ready
            if elapsed > 0 and elapsed % 30 == 0:  # Log detailed pod status every 30s
                try:
                    pods = api_core.list_namespaced_pod(
                        namespace=namespace,
                        label_selector=f"app={daemonset_name}"
                    )
                    for pod in pods.items:
                        pod_name = pod.metadata.name
                        phase = pod.status.phase

                        # Check container statuses
                        container_states = []
                        if pod.status.container_statuses:
                            for cs in pod.status.container_statuses:
                                if cs.state.waiting:
                                    container_states.append(f"{cs.name}=Waiting({cs.state.waiting.reason})")
                                elif cs.state.terminated:
                                    container_states.append(f"{cs.name}=Terminated(exit={cs.state.terminated.exit_code}, reason={cs.state.terminated.reason})")
                                elif cs.state.running:
                                    container_states.append(f"{cs.name}=Running")

                                # Collect logs if container is crash-looping
                                if cs.state.waiting and cs.state.waiting.reason in ["CrashLoopBackOff", "Error"]:
                                    Logger.warning(f"Pod {pod_name} container {cs.name} is {cs.state.waiting.reason}, collecting logs...")
                                    try:
                                        logs = api_core.read_namespaced_pod_log(
                                            name=pod_name,
                                            namespace=namespace,
                                            container=cs.name,
                                            tail_lines=50
                                        )
                                        Logger.error(f"Pod {pod_name} container {cs.name} logs (last 50 lines):\n{logs}")
                                    except Exception as log_err:
                                        Logger.warning(f"Could not collect logs for {pod_name}/{cs.name}: {log_err}")

                        Logger.info(f"Pod {pod_name}: phase={phase}, containers=[{', '.join(container_states)}]")

                        # Get recent pod events
                        events = api_core.list_namespaced_event(
                            namespace=namespace,
                            field_selector=f"involvedObject.name={pod_name}"
                        )
                        warnings = [e for e in events.items if e.type == "Warning"]
                        if warnings:
                            Logger.warning(f"Pod {pod_name} has {len(warnings)} warning events:")
                            for event in warnings[-5:]:  # Show last 5 warnings
                                Logger.warning(f"  [{event.reason}] {event.message}")

                except Exception as diag_err:
                    Logger.warning(f"Error collecting pod diagnostics: {diag_err}")

        except Exception as e:
            Logger.warning(f"Error checking DaemonSet status: {e}")

        time.sleep(interval)
        elapsed += interval

    Logger.error(f"Timeout waiting for DaemonSet {daemonset_name} to be ready after {timeout}s")

    # Final diagnostic dump on timeout
    try:
        Logger.error(f"Collecting final diagnostic information for failed DaemonSet {daemonset_name}...")
        pods = api_core.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"app={daemonset_name}"
        )
        for pod in pods.items:
            Logger.error(f"Pod {pod.metadata.name} final status: phase={pod.status.phase}")
            if pod.status.container_statuses:
                for cs in pod.status.container_statuses:
                    Logger.error(f"  Container {cs.name}: ready={cs.ready}, restart_count={cs.restart_count}")
                    if cs.state.waiting:
                        Logger.error(f"    State: Waiting - {cs.state.waiting.reason}: {cs.state.waiting.message}")
                    elif cs.state.terminated:
                        Logger.error(f"    State: Terminated - exit_code={cs.state.terminated.exit_code}, reason={cs.state.terminated.reason}")
    except Exception as final_err:
        Logger.error(f"Error collecting final diagnostics: {final_err}")

    return False


def verify_npd_node_condition(gpu_nodes, condition_type, expected_status=None, expected_reason=None, timeout=120, interval=10):
    """
    Verify that NPD has set the expected condition on all GPU nodes.

    Args:
        gpu_nodes: List of GPU node objects
        condition_type: Condition type to check (e.g., "AMDGPUProblem")
        expected_status: Expected condition status ("True", "False", "Unknown"), None to skip check
        expected_reason: Expected reason string, None to skip check
        timeout: Maximum time to wait for condition to appear (default: 120s)
        interval: Check interval in seconds (default: 10)

    Returns:
        tuple: (success: bool, failed_nodes: list of node names)
    """
    elapsed = 0
    nodes_to_check = [k8_util.k8_get_node_hostname(node) for node in gpu_nodes]

    while elapsed < timeout:
        failed_nodes = []

        for node_name in nodes_to_check:
            condition = get_node_condition(node_name, condition_type)

            if condition is None:
                Logger.debug(f"Node {node_name}: condition '{condition_type}' not yet present")
                failed_nodes.append(node_name)
                continue

            # Check expected status if provided
            if expected_status is not None and condition['status'] != expected_status:
                Logger.debug(f"Node {node_name}: condition status is '{condition['status']}', expected '{expected_status}'")
                failed_nodes.append(node_name)
                continue

            # Check expected reason if provided
            if expected_reason is not None and condition['reason'] != expected_reason:
                Logger.debug(f"Node {node_name}: condition reason is '{condition['reason']}', expected '{expected_reason}'")
                failed_nodes.append(node_name)
                continue

            Logger.info(f"Node {node_name}: condition '{condition_type}' verified - status={condition['status']}, reason={condition['reason']}")

        # If all nodes pass validation, return success
        if not failed_nodes:
            return True, []

        # Wait and retry
        time.sleep(interval)
        elapsed += interval

    Logger.error(f"Timeout waiting for condition '{condition_type}' on nodes: {failed_nodes}")
    return False, failed_nodes
