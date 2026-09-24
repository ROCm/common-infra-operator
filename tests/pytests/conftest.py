
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

import pytest
import pdb
import os
import re
import json
import shutil
import requests
import logging
from datetime import datetime
from lib import common
from lib import k8_util
from lib import manifest_util
try:
    import lib.amdgpu as amdgpu_util
except ImportError:
    amdgpu_util = None

try:
    import lib.node_gpu_collector as node_collector
except ImportError:
    node_collector = None

import lib.json_report as json_report
from py.xml import html
from pathlib import Path
from urllib.parse import urlparse
import getpass

Logger = logging.getLogger("root.conftest")
logging.getLogger("paramiko").setLevel(logging.WARNING)
logging.getLogger('invoke').setLevel(logging.WARNING)
logging.getLogger('kubernetes').setLevel(logging.WARNING)

# Per-test log file support — updated by the environment fixture once logdir is known.
_session_logdir = "logs"
_test_log_handlers: dict = {}

_test_log_formatter = logging.Formatter(
    "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

def _test_log_path(nodeid: str) -> str:
    parts = nodeid.split("::")
    module = os.path.basename(parts[0]).replace(".py", "")
    test_name = "_".join(parts[1:]) if len(parts) > 1 else "session"
    test_name = re.sub(r"[\[\]/\\]", "_", test_name).strip("_")
    test_dir = os.path.join(_session_logdir, module)
    os.makedirs(test_dir, exist_ok=True)
    return os.path.join(test_dir, f"{test_name}.log")

def pytest_runtest_logstart(nodeid, location):
    log_path = _test_log_path(nodeid)
    handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(_test_log_formatter)
    logging.getLogger().addHandler(handler)
    _test_log_handlers[nodeid] = handler

def pytest_runtest_logfinish(nodeid, location):
    handler = _test_log_handlers.pop(nodeid, None)
    if handler:
        logging.getLogger().removeHandler(handler)
        handler.close()

def pytest_addoption(parser):
    parser.addoption(
            "--testbed",
            action="store",
            default=None,
            help="Testbed JSON file with details about platform/cluster",
    )

    parser.addoption(
            "--deployment",
            action = "store",
            default = "k8",
            choices = ["k8", "openshift", "standalone", "hypervisor"],
            help = "Deployment model to test against",
    )

    parser.addoption(
            "--image-manifest",
            action = "store",
            default = None,
            required = True,
            help = "Image manifest listing images to use for testing"
    )

    parser.addoption(
            "--secrets-json",
            action = "store",
            default = None,
            help = "K8 secrets json file"
    )

    parser.addoption(
            "--amdgpu-driver-spec",
            action = "store",
            default = "lib/files/amd-deviceconfig-default-driver-spec.json",
            required = False,
            help = "AMDGPU Driver to use"
    )

    parser.addoption(
            "--tech-support-tool",
            action="store",
            default=None,
            help="Path to tech-support tool to collect information",
    )
    parser.addoption(
            "--workload-selection",
            action="store",
            default="rocm-pytorch-benchmark",
            help="Workload template to use",
    )
    parser.addoption(
            "--base-version",
            action="store",
            default=None,
            help="Base version for upgrade tests (e.g., v1.4.1)"
    )

    parser.addoption(
            "--gim-driver-spec",
            action="store",
            default=None,
            required=False,
            help="GIM driver spec JSON file (hypervisor SR-IOV tests)",
    )

    parser.addoption(
            "--nic-config",
            action="store",
            default=None,
            required=False,
            help="NIC config JSON file with NIC-specific test parameters",
    )

def pytest_html_results_summary(prefix, summary, postfix):
    """
    Add custom information to the summary section of the report.

    Adds Environment info (AMDGPU Driver, Cluster Nodes) and Images table
    to the prefix section in a 2-column grid layout.
    """
    # Build left column (Images Used)
    left_column_content = []
    if hasattr(pytest, "_image_info"):
        left_column_content.extend([
            html.h3("Images"),
            transform_image_info(),
        ])

    # Add test results summary table to the summary section
    # The summary section shows test result counts
    # We'll add a styled table showing the breakdown
    results_table = None
    if hasattr(pytest, "_config"):
        # Get test results from terminalreporter
        config = pytest._config
        if hasattr(config, 'pluginmanager'):
            terminalreporter = config.pluginmanager.get_plugin('terminalreporter')
            if terminalreporter:
                stats = terminalreporter.stats

                # Count results by category
                passed = len(stats.get('passed', []))
                failed = len(stats.get('failed', []))
                skipped = len(stats.get('skipped', []))
                error = len(stats.get('error', []))
                xfailed = len(stats.get('xfailed', []))
                xpassed = len(stats.get('xpassed', []))
                rerun = len(stats.get('rerun', []))

                # Create results summary table
                table_style = "border: 1px solid black; border-collapse: collapse; min-width: 400px; margin: 20px 0;"
                cell_style = "border: 1px solid black; padding: 10px; min-width: 50px;"

                results_table = html.table(style=table_style)
                results_table.append(html.tr([
                    html.th("Result Type", scope="col", style=cell_style),
                    html.th("Count", scope="col", style=cell_style),
                ]))

                # Add rows for each result type
                results_table.append(html.tr([
                    html.td("Failed", style=cell_style + " color: #dc3545; font-weight: 600;"),
                    html.td(str(failed), style=cell_style + " text-align: center; font-weight: 600;"),
                ]))
                results_table.append(html.tr([
                    html.td("Passed", style=cell_style + " color: #28a745; font-weight: 600;"),
                    html.td(str(passed), style=cell_style + " text-align: center; font-weight: 600;"),
                ]))
                results_table.append(html.tr([
                    html.td("Skipped", style=cell_style + " color: #ffc107; font-weight: 600;"),
                    html.td(str(skipped), style=cell_style + " text-align: center; font-weight: 600;"),
                ]))
                results_table.append(html.tr([
                    html.td("Expected Failures", style=cell_style),
                    html.td(str(xfailed), style=cell_style + " text-align: center;"),
                ]))
                results_table.append(html.tr([
                    html.td("Unexpected Passes", style=cell_style),
                    html.td(str(xpassed), style=cell_style + " text-align: center;"),
                ]))
                results_table.append(html.tr([
                    html.td("Errors", style=cell_style + " color: #dc3545;"),
                    html.td(str(error), style=cell_style + " text-align: center;"),
                ]))
                results_table.append(html.tr([
                    html.td("Reruns", style=cell_style),
                    html.td(str(rerun), style=cell_style + " text-align: center;"),
                ]))


    # Test Results Summary at the top (where the Environment table used to be)
    if results_table:
        prefix.append(html.h3("Test Results Summary"))
        prefix.append(results_table)
        prefix.append(html.br())

    # Build right column (Cluster Nodes and AMDGPU Driver Version)
    right_column_content = []
    if hasattr(pytest, "_k8_cluster_inst"):
        right_column_content.extend([
            html.h3("Cluster Nodes"),
            cluster_info_table(),
        ])

    if hasattr(pytest, "_gim_node_info"):
        gim_info = pytest._gim_node_info
        gim_ver = pytest._gim_driver_spec.get('default-version', 'NA') if hasattr(pytest, "_gim_driver_spec") else 'NA'
        right_column_content.extend([
            html.h3("Hypervisor Host", style="margin-top: 30px;"),
            html.p(
                html.strong(f"OS: Ubuntu {gim_info.get('os_version', 'NA')} | "
                            f"Kernel: {gim_info.get('kernel_version', 'NA')} | "
                            f"GPU: {gim_info.get('gpu_series', 'NA')} | "
                            f"GIM: {gim_ver}"),
                style="font-size: 16px; color: #212529; margin: 10px 0;"
            ),
        ])
    else:
        if hasattr(pytest, "_amdgpu_driver_spec"):
            ver = pytest._amdgpu_driver_spec.get('default-version', 'NA')
            deployment_mode = pytest._amdgpu_driver_spec.get('driver-deployment', 'NA')
            right_column_content.extend([
                html.h3("AMDGPU Driver Version", style="margin-top: 30px;"),
                html.p(
                    html.strong(f"Version: {ver} | Deployment: {deployment_mode}"),
                    style="font-size: 16px; color: #212529; margin: 10px 0;"
                ),
            ])

    # Create 2-column grid layout with 60/40 split
    if left_column_content or right_column_content:
        grid_container = html.div(
            html.div(*left_column_content, style="grid-column: 1;") if left_column_content else html.div(),
            html.div(*right_column_content, style="grid-column: 2;") if right_column_content else html.div(),
            style="display: grid; grid-template-columns: 3fr 2fr; gap: 30px; margin: 20px 0;"
        )
        prefix.append(grid_container)
        prefix.append(html.br())

    
def cluster_info_table():
    gpu_series_by_host = {
        node.host_name: node.gpu_series
        for node in pytest._k8_cluster_inst.cluster_nodes
    }

    table_style = "border: 1px solid black; border-collapse: collapse; min-width: 300px; margin-bottom: 10px;"
    cell_style = "border: 1px solid black; padding: 5px; min-width: 50px;"

    table = html.table(style=table_style)
    header_row = html.tr([
        html.th("Node Name", scope="col", style=cell_style),
        html.th("IP Address", scope="col", style=cell_style),
        html.th("GPU-Series", scope="col", style=cell_style),
        html.th("GPU Count", scope="col", style=cell_style),
        html.th("OCP Version", scope="col", style=cell_style),
        html.th("K8-Version", scope="col", style=cell_style),
        html.th("Kernel Version", scope="col", style=cell_style),
        html.th("Host OS Name", scope="col", style=cell_style),
        html.th("Host OS Version", scope="col", style=cell_style),
    ])
    table.append(header_row)

    for node in pytest._k8_cluster_inst.cluster_nodes:
        table.append(html.tr([
            html.td(node.host_name, scope="col", style=cell_style),
            html.td(node.ip_address, scope="col", style=cell_style),
            html.td(node.gpu_series, scope="col", style=cell_style),
            html.td(node.num_gpus, scope="col", style=cell_style),
            html.td(node.ocp_version, scope="col", style=cell_style),
            html.td(node.k8_version, scope="col", style=cell_style),
            html.td(node.kernel_version, scope="col", style=cell_style),
            html.td(node.host_os_name, scope="col", style=cell_style),
            html.td(node.host_os_version, scope="col", style=cell_style)
        ]))
    return table

def transform_image_info():
    # convert dict image_info to table format
    pytest._image_info.pop("image_folder")
    transformed_dict = {}
 
    for key, value in  pytest._image_info.items():
        sub_key = key.split(".")[-1]
        base_key = ".".join(key.split(".")[:-1])
        if value:
            transformed_dict.setdefault(base_key, {})[sub_key] = value

    table_style = "border: 1px solid black; border-collapse: collapse; min-width: 300px; margin-bottom: 10px;"
    cell_style = "border: 1px solid black; padding: 5px; min-width: 50px;"
    table = html.table(style=table_style)
    header_row = html.tr([
        html.th("Image", scope="col", style=cell_style),
        html.th("Location", scope="col", style=cell_style),
        html.th("Version", scope="col", style=cell_style),
        ])
    table.append(header_row)

    for key, value in transformed_dict.items():
        if 'repository' in value.keys() or 'version' in value.keys():
            version_str = value.get('version', 'N/A')
            rocm_ver = value.get('rocm_version')
            if rocm_ver:
                version_str = f"{version_str} (ROCm {rocm_ver})"
            rvs_ver = value.get('rvs_version')
            if rvs_ver:
                version_str = f"{version_str} (RVS {rvs_ver})"
            agfhc_ver = value.get('agfhc_version')
            if agfhc_ver:
                version_str = f"{version_str} (AGFHC {agfhc_ver})"
            row = html.tr([
                  html.td(key, scope="col", style=cell_style),
                  html.td(value.get('repository', 'N/A'), scope="col", style=cell_style),
                  html.td(version_str, scope="col", style=cell_style),
                  ])
            table.append(row)
    return table

def pytest_metadata(metadata):
    """Remove default pytest fields from the Environment table.

    Cluster Nodes and AMDGPU Driver are added later in pytest_sessionfinish
    once fixture data is available.
    """
    for field in list(metadata.keys()):
        metadata.pop(field, None)


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    """Populate the Environment table with cluster and driver info.

    Runs with tryfirst=True so metadata is updated before pytest-html
    serialises it into the report's JSON blob.
    """
    # Always emit the JSON report regardless of whether pytest-metadata is installed.
    json_report.generate(session, _session_logdir)

    config = session.config

    # pytest-metadata >= 3.0 uses config.stash[metadata_key]; < 3.0 used config._metadata.
    try:
        from pytest_metadata.plugin import metadata_key
        metadata = config.stash[metadata_key]
    except (ImportError, KeyError):
        metadata = getattr(config, '_metadata', None)
    if metadata is None:
        return

    if hasattr(pytest, '_gim_node_info'):
        gim_info = pytest._gim_node_info
        gim_ver = pytest._gim_driver_spec.get('default-version', 'NA') if hasattr(pytest, '_gim_driver_spec') else 'NA'
        metadata['GIM Driver'] = gim_ver
        metadata['Hypervisor Host'] = (
            f"Ubuntu {gim_info.get('os_version', 'NA')} | "
            f"Kernel {gim_info.get('kernel_version', 'NA')} | "
            f"GPU {gim_info.get('gpu_series', 'NA')}"
        )
    else:
        if hasattr(pytest, '_amdgpu_driver_spec'):
            ver = pytest._amdgpu_driver_spec.get('default-version', 'NA')
            deployment = pytest._amdgpu_driver_spec.get('driver-deployment', 'NA')
            metadata['AMDGPU Driver'] = f"{ver} | {deployment}"

    if hasattr(pytest, '_k8_cluster_inst'):
        node_entries = []
        for node in pytest._k8_cluster_inst.cluster_nodes:
            gpu_info = f"{node.gpu_series} x{node.num_gpus}" if node.num_gpus else "no GPU"
            node_entries.append(
                f"{node.host_name} ({node.ip_address}) — {gpu_info}, "
                f"{node.host_os_name} {node.host_os_version}, k8s {node.k8_version}"
            )
        if node_entries:
            metadata['Testbed'] = node_entries


class Context(object):
    pass

@pytest.fixture(scope="function", autouse=True)
def context(request, environment):
    global Logger
    environment.context = Context()
    setattr(environment.context, 'current_tc_name', request.node.name)
    setattr(environment.context, 'log_dir', os.path.dirname(_test_log_path(request.node.nodeid)))

    module = request.node.module.__name__ if request.node.module else "unknown"
    nodeid = request.node.nodeid

    Logger.info(f">>> TC START: {nodeid} [module={module}]")
    start_time = datetime.now()
    yield
    elapsed = (datetime.now() - start_time).total_seconds()

    # Determine outcome from stashed reports
    reports = request.node.stash.get("reports", {})
    setup_report = reports.get("setup")
    call_report = reports.get("call")

    if setup_report and setup_report.skipped:
        outcome = "SKIPPED"
        reason = str(setup_report.longrepr[2]) if isinstance(setup_report.longrepr, tuple) else str(setup_report.longrepr).splitlines()[-1] if setup_report.longrepr else ""
    elif call_report:
        if call_report.passed:
            outcome = "PASSED"
            reason = ""
        elif call_report.skipped:
            outcome = "SKIPPED"
            reason = str(call_report.longrepr[2]) if isinstance(call_report.longrepr, tuple) else str(call_report.longrepr).splitlines()[-1] if call_report.longrepr else ""
        else:
            outcome = "FAILED"
            reason = str(call_report.longrepr).splitlines()[-1] if call_report.longrepr else ""
    elif setup_report and setup_report.failed:
        outcome = "ERROR (setup)"
        reason = str(setup_report.longrepr).splitlines()[-1] if setup_report.longrepr else ""
    else:
        outcome = "UNKNOWN"
        reason = ""

    msg = f"<<< TC {outcome}: {nodeid} [module={module}, duration={elapsed:.1f}s]"
    if reason:
        msg += f" -- {reason[:200]}"

    if outcome == "PASSED":
        Logger.info(msg)
    elif outcome == "SKIPPED":
        Logger.info(msg)
    else:
        Logger.error(msg)

    environment.context = None

@pytest.fixture(scope="session")
def environment(request):
    global Logger
    class Env(object):
        pass

    tenv = Env()
    setattr(tenv, 'deployment_mode', request.config.option.deployment)
    setattr(tenv, 'download_folder', 'downloads')
    setattr(tenv, 'logdir', "logs")
    global _session_logdir
    _session_logdir = tenv.logdir
    tenv.context = Context()
    if request.config.option.amdgpu_driver_spec:
        driver_spec_path = request.config.option.amdgpu_driver_spec
        if os.path.exists(driver_spec_path):
            with open(driver_spec_path, "r") as fp:
                driver_spec = json.load(fp)
                setattr(pytest, "_amdgpu_driver_spec", driver_spec)
                setattr(tenv, 'amdgpu_driver_spec', driver_spec)
        else:
            Logger.warning("amdgpu-driver-spec file not found: %s — skipping GPU driver spec", driver_spec_path)

    # GIM driver spec (optional)
    if request.config.option.gim_driver_spec:
        gim_spec_path = request.config.option.gim_driver_spec
        if os.path.exists(gim_spec_path):
            with open(gim_spec_path, "r") as fp:
                gim_spec = json.load(fp)
                setattr(pytest, "_gim_driver_spec", gim_spec)
                setattr(tenv, 'gim_driver_spec', gim_spec)
        else:
            Logger.warning("gim-driver-spec file not found: %s — skipping GIM driver spec", gim_spec_path)

    # NIC config (optional)
    if request.config.option.nic_config:
        nic_config_path = request.config.option.nic_config
        if os.path.exists(nic_config_path):
            with open(nic_config_path, "r") as fp:
                setattr(tenv, 'nic_config', json.load(fp))
                Logger.info("Loaded NIC config from %s", nic_config_path)
        else:
            Logger.warning("nic-config file not found: %s — skipping NIC config", nic_config_path)

    # Network-operator namespace
    setattr(tenv, 'network_operator_namespace',
            os.getenv('NETWORK_OPERATOR_NAMESPACE', 'kube-amd-network'))

    kube_config_file = os.path.join(Path.home(), ".kube", "config")
    if os.path.exists(kube_config_file):
        setattr(tenv, 'kube_config_file', kube_config_file)
    elif request.config.option.deployment not in ("hypervisor", "standalone"):
        pytest.fail("Failed to find kube_config_file for cluster operator - Aborting")

    # Secrets file
    secrets_json_file = os.path.join(Path.home(), ".kube", "secrets.json")
    if request.config.option.secrets_json:
        secrets_json_file = request.config.option.secrets_json
    if os.path.exists(secrets_json_file):
        setattr(tenv, 'k8_secrets_file', secrets_json_file)

    # Tech-support tool
    setattr(tenv, 'tech_support_tool', None)
    tech_support_path = None

    # Use user-provided tool if specified
    if request.config.option.tech_support_tool:
        if os.path.exists(request.config.option.tech_support_tool):
            tech_support_path = request.config.option.tech_support_tool
    else:
        # Fall back to default tech-support script
        default_script = os.path.join(os.path.dirname(__file__), "scripts", "default-tech-support.sh")
        if os.path.exists(default_script):
            tech_support_path = default_script
            Logger.info(f"Using default tech-support script: {default_script}")

    if tech_support_path:
        tst_info = {
            "tool": tech_support_path,
            "args": [],
        }
        setattr(tenv, 'tech_support_tool', tst_info)
        os.makedirs(os.path.join(tenv.logdir, "tech-support"), exist_ok=True)

    # Workload Template
    setattr(tenv, 'default_workload', request.config.option.workload_selection)
    setattr(tenv, 'exporter_namespace', os.getenv('EXPORTER_NAMESPACE', 'kube-amd-exporter'))
    setattr(tenv, 'gpu_operator_namespace', os.getenv('GPU_OPERATOR_NAMESPACE', 'kube-amd-gpu'))

    # NIC standalone component namespaces and config
    setattr(tenv, 'dp_namespace', 'kube-amd-network-dp')
    setattr(tenv, 'dp_release_name', 'st-device-plugin')
    setattr(tenv, 'dp_artifact', 'device-plugin')
    setattr(tenv, 'dp_upgrade_image_tag', 'v1.2.0-2')
    setattr(tenv, 'me_namespace', 'kube-amd-network-exporter')
    setattr(tenv, 'me_release_name', 'amd-ainic-exporter')
    setattr(tenv, 'me_artifact', 'exporter')
    setattr(tenv, 'nl_namespace', 'kube-amd-network-labeller')
    setattr(tenv, 'nl_release_name', 'nl-instance')
    setattr(tenv, 'nl_artifact', 'node-labeller')
    setattr(tenv, 'nl_secret_source_ns', 'kube-amd-network')
    setattr(tenv, 'networkconfig_name', 'test-networkconfig')
    setattr(tenv, 'daemonset_ready_timeout', 120)

    setattr(tenv, "amd_smi_collection_complete", False)
    json_report.record_milestone("session_init", {
        "status": "ok",
        "deployment_mode": tenv.deployment_mode,
        "has_kubeconfig": hasattr(tenv, 'kube_config_file'),
    })
    return tenv

@pytest.fixture(scope="session")
def gpu_cluster(request, environment):
    global Logger
    if not hasattr(environment, 'kube_config_file'):
        json_report.record_milestone("cluster_init", {"status": "skipped", "reason": "no kube_config_file"})
        stub = common.k8_cluster.BuildK8Cluster([])
        stub.k8_registry = ""
        stub.k8_kube_config = ""
        return stub
    k8_util.k8_lib_init(environment.kube_config_file)
    ret_code, k8_nodes = k8_util.k8_get_nodes()
    if ret_code != 0:
        json_report.record_milestone("cluster_init", {"status": "failed", "reason": "Failed to collect nodes from cluster"})
    assert ret_code == 0, "Failed to collect nodes from cluster"
    nodes = list()
    for node in k8_nodes:
        node_name = node['metadata']['name']
        k8_version = node['status']['node_info']['kubelet_version']
        node_ip = k8_util.k8_get_node_address(node)
        if 'node-role.kubernetes.io/control-plane' in node['metadata']['labels']:
            nodes.append(common.Node(node_ip, None, None, None, "master", k8_version, node_name))
        else:
            nodes.append(common.Node(node_ip, None, None, None, "worker", k8_version, node_name))
    k8_cluster_inst = common.k8_cluster.BuildK8Cluster(nodes)
    k8_cluster_inst.k8_kube_config = environment.kube_config_file
    if len(k8_cluster_inst.cluster_nodes) == 0:
        json_report.record_milestone("cluster_init", {"status": "failed", "reason": "zero nodes discovered"})
    assert len(k8_cluster_inst.cluster_nodes) > 0, f"Failed to collect nodes from k8/cluster"
    if hasattr(environment, "k8_secrets_file"):
        with open(environment.k8_secrets_file) as fp:
            k8_cluster_inst.k8_secrets = json.load(fp)
    # NIC node discovery — detect nodes with AMD NIC labels
    nic_nodes = []
    for node in k8_nodes:
        labels = node.get('metadata', {}).get('labels', {})
        if (labels.get('feature.node.kubernetes.io/amd-nic') == 'true'
                or labels.get('amd.com/nic') == 'present'):
            node_name = node['metadata']['name']
            nic_nodes.append(node_name)
    k8_cluster_inst.nic_nodes = nic_nodes
    if nic_nodes:
        Logger.info("NIC nodes discovered: %s", nic_nodes)
    else:
        Logger.info("No NIC nodes discovered (labels: feature.node.kubernetes.io/amd-nic, amd.com/nic)")

    setattr(pytest, "_k8_cluster_inst", k8_cluster_inst)
    json_report.record_milestone("cluster_init", {
        "status": "ok",
        "node_count": len(k8_cluster_inst.cluster_nodes),
        "nic_node_count": len(nic_nodes),
    })
    return k8_cluster_inst

@pytest.fixture(scope="session")
def images(request, gpu_cluster, environment):
    image_info = None
    from ruamel.yaml import YAML
    from ruamel.yaml import comments
    from ruamel.yaml import scalarstring
    import shutil

    yaml = YAML()
    yaml.preserve_quotes = True

    file_obj = Path(request.config.option.image_manifest)
    if not file_obj.exists():
        pytest.fail(f"Missing {request.config.option.image_manifest}")

    image_manifest = dict(yaml.load(file_obj))

    # Process metadata section of image-manifest
    image_metadata = image_manifest['images'].get('meta', {})

    # Optional metadata validation (warn if missing, don't fail)
    if 'operator' not in image_metadata:
        Logger.warning(f"{file_obj.name}: missing 'operator' field in images.meta")
    if 'version' not in image_metadata:
        Logger.warning(f"{file_obj.name}: missing 'version' field in images.meta")

    registry = 'docker.io'
    if 'registry' in image_metadata:
        registry = image_metadata['registry'].get('default', 'docker.io')
        if 'mirror' in image_metadata['registry']:
            if image_metadata['registry']['mirror'].get('enable', 'no') == 'yes':
                registry = image_metadata['registry']['mirror']['url']
    setattr(environment, 'default_registry', registry)
    if 'packaging' in image_metadata:
        if image_metadata['packaging'].get('gpuctl', 'enabled') == 'disabled':
            setattr(environment, "builtin_gpuctl_support", False)
        else:
            setattr(environment, "builtin_gpuctl_support", True)
    else:
        setattr(environment, "builtin_gpuctl_support", True)
    assert environment.deployment_mode in image_manifest['images'], f"Missing images for {environment.deployment_mode}"
    if environment.deployment_mode in ["standalone", "k8", "openshift", "hypervisor"]:
        image_info = _build_image_info(environment, image_manifest['images'])

    if image_info is None:
        json_report.record_milestone("image_manifest", {"status": "failed", "reason": f"Failed to build images for {environment.deployment_mode}"})
    assert image_info != None, f"Failed to build images for {environment.deployment_mode}"
    gpu_cluster.k8_registry = environment.default_registry
    image_info['driver.imageBuild.baseImageRegistry'] = environment.default_registry
    setattr(pytest, "_image_info", image_info)
    json_report.record_milestone("image_manifest", {"status": "ok"})
    return image_info

@pytest.fixture(scope="session")
def all_image_versions(request, environment):
    """
    Load all released operator image manifests from image-manifest/ directory.

    Scans tests/pytests/image-manifest/{operator}/ subdirectories
    and loads all *_external_images.yaml files.

    Returns:
        dict[str, dict[str, dict]]: Nested dictionary structure
        {
            "gpu-operator": {
                "v1.4.1": {image_info_dict},
                "v1.4.0": {image_info_dict},
                ...
            },
            "network-operator": {
                "v1.0.0": {image_info_dict},
                ...
            }
        }

    Validation:
        - Operator from directory name should match images.meta.operator
        - Version from filename should match images.meta.version
        - Warns on mismatch but doesn't fail (allows gradual migration)
    """
    global Logger
    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.preserve_quotes = True

    manifest_base_dir = Path(__file__).parent / "image-manifest"
    version_map = {}

    if not manifest_base_dir.exists():
        Logger.warning(f"Image manifest directory not found: {manifest_base_dir}")
        return version_map

    # Iterate through operator subdirectories
    for operator_dir in manifest_base_dir.iterdir():
        if not operator_dir.is_dir():
            continue

        operator_type = operator_dir.name  # "gpu-operator" or "network-operator"
        version_map[operator_type] = {}

        # Load all *_external_images.yaml files in this operator directory
        for manifest_file in sorted(operator_dir.glob("*_external_images.yaml")):
            try:
                # Extract version from filename: v1.4.1_external_images.yaml -> v1.4.1
                filename_version = manifest_file.stem.replace("_external_images", "")

                # Load manifest
                manifest_data = yaml.load(manifest_file)

                if not manifest_data or 'images' not in manifest_data:
                    Logger.warning(f"{manifest_file}: Invalid manifest structure")
                    continue

                # Validate metadata (optional - warn on mismatch)
                if 'meta' in manifest_data['images']:
                    meta = manifest_data['images']['meta']

                    # Check operator field
                    if 'operator' in meta:
                        if meta['operator'] != operator_type:
                            Logger.warning(
                                f"{manifest_file.name}: metadata operator '{meta['operator']}' "
                                f"doesn't match directory '{operator_type}'"
                            )
                    else:
                        Logger.debug(f"{manifest_file.name}: missing 'operator' field in metadata")

                    # Check version field
                    if 'version' in meta:
                        if meta['version'] != filename_version:
                            Logger.warning(
                                f"{manifest_file.name}: metadata version '{meta['version']}' "
                                f"doesn't match filename '{filename_version}'"
                            )
                    else:
                        Logger.debug(f"{manifest_file.name}: missing 'version' field in metadata")
                else:
                    Logger.debug(f"{manifest_file.name}: missing 'meta' section")

                # Build image_info WITHOUT mutating environment
                # (to avoid overwriting RC version with released version)
                image_info = _build_image_info_no_env_mutation(environment, manifest_data['images'])

                # Store in nested dict
                version_map[operator_type][filename_version] = image_info

                Logger.debug(f"Loaded {operator_type} {filename_version} from {manifest_file.name}")

            except Exception as e:
                Logger.error(f"Failed to load {manifest_file.name}: {e}")
                continue

    # Log summary
    for op_type, versions in version_map.items():
        if versions:
            Logger.info(f"Loaded {len(versions)} version(s) for {op_type}: {sorted(versions.keys())}")

    return version_map

def _build_image_info_no_env_mutation(environment, image_manifest):
    '''
    Build image-info WITHOUT mutating environment object.
    Used by all_image_versions to avoid overwriting RC version with released versions.
    '''
    # Temporarily save current environment versions
    saved_gpu_op_ver = getattr(environment, 'gpu_operator_version', None)
    saved_exporter_ver = getattr(environment, 'exporter_version', None)

    # Call the regular helper
    image_info = _build_image_info(environment, image_manifest)

    # Restore original environment versions
    if saved_gpu_op_ver is not None:
        setattr(environment, 'gpu_operator_version', saved_gpu_op_ver)
    if saved_exporter_ver is not None:
        setattr(environment, 'exporter_version', saved_exporter_ver)

    return image_info

def _build_image_info(environment, image_manifest):
    '''
    Build image-info used for testing.
    Delegates parsing to manifest_util.build_image_info() and handles
    environment mutations and HTTP downloads that require pytest context.
    '''
    global Logger

    images = image_manifest[environment.deployment_mode]

    # Extract operator/exporter versions for environment (pytest-specific)
    if images.get('gpu-operator', None) and images['gpu-operator']['kind'] in ['helm-chart', 'olm-bundle']:
        setattr(environment, 'gpu_operator_version', images['gpu-operator']['version'])
    elif images.get('gpu-operator', None) and images['gpu-operator']['kind'] == 'olm-subscription':
        csv = images['gpu-operator'].get('csv', '')
        version = csv.replace('amd-gpu-operator.', '') if csv.startswith('amd-gpu-operator.') else csv
        setattr(environment, 'gpu_operator_version', version)
    if images.get('exporter', None) and images['exporter']['kind'] == 'helm-chart':
        setattr(environment, 'exporter_version', images['exporter']['version'])

    # Validate manifest structure before parsing
    validation_errors = manifest_util.validate_manifest(
        {"images": image_manifest}, environment.deployment_mode)
    if validation_errors:
        pytest.fail("Image manifest validation failed:\n  " + "\n  ".join(validation_errors))

    # Delegate core parsing to manifest_util
    image_info, parse_errors = manifest_util.build_image_info(
        image_manifest, environment.deployment_mode,
        download_folder=environment.download_folder,
        default_registry=getattr(environment, 'default_registry', 'docker.io'))
    if parse_errors:
        pytest.fail("Image manifest parsing errors:\n  " + "\n  ".join(parse_errors))

    # Download http/https artifacts (manifest_util computes paths but doesn't download)
    for artifact, artifact_info in images.items():
        if not isinstance(artifact_info, dict):
            continue
        location = artifact_info.get('location', '')
        if 'http://' in location or 'https://' in location:
            url = location
            local_file = os.path.join(environment.download_folder, os.path.basename(urlparse(url).path))
            if not os.path.exists(local_file):
                try:
                    resp = requests.get(url)
                    if resp.status_code == 200:
                        with open(local_file, 'wb') as fp:
                            fp.write(resp.content)
                    else:
                        pytest.fail(f"Failed to download {local_file}, HTTP {resp.status_code}")
                except Exception as e:
                    Logger.error(f"Failed to download {local_file} from {url}, error : {e}")
                    pytest.fail("Could not download images - abort")

    # Validate file:// paths exist
    for artifact, artifact_info in images.items():
        if not isinstance(artifact_info, dict):
            continue
        location = artifact_info.get('location', '')
        if 'file://' in location:
            local_file = location.split('file://')[-1]
            if not os.path.exists(local_file):
                pytest.fail(f"Invalid file name or path not found : {local_file}")

    return image_info

@pytest.fixture(scope="session", autouse=True)
def gather_device_info(gpu_cluster, images, environment):
    if environment.deployment_mode == "hypervisor":
        json_report.record_milestone("gpu_discovery", {"status": "skipped", "reason": "hypervisor mode"})
        return

    if node_collector is None:
        Logger.info("GPU modules not available (lib.node_gpu_collector), skipping GPU discovery")
        json_report.record_milestone("gpu_discovery", {"status": "skipped", "reason": "node_gpu_collector not available"})
        return

    success, error_msg = node_collector.populate_all_cluster_nodes_with_gpu_info(gpu_cluster)
    if not success:
        json_report.record_milestone("gpu_discovery", {"status": "failed", "reason": error_msg})
        pytest.exit(f"Failed to collect node GPU information: {error_msg}")

    gpu_nodes = [n for n in gpu_cluster.cluster_nodes if n.is_gpu_node()]
    json_report.record_milestone("gpu_discovery", {
        "status": "ok",
        "gpu_node_count": len(gpu_nodes),
        "gpu_series": list({n.gpu_series for n in gpu_nodes if n.gpu_series}),
    })
    Logger.info("Collected amd-gpu information for all cluster nodes")

    if hasattr(environment, 'amdgpu_driver_spec') and amdgpu_util is not None:
        spec_rocm_ver = environment.amdgpu_driver_spec.get('default-version')
        if spec_rocm_ver:
            amdgpu_ver = amdgpu_util.get_matching_driver_version(spec_rocm_ver) or spec_rocm_ver
            for node in gpu_cluster.cluster_nodes:
                if node.is_gpu_node() and node.amdgpu_driver_version is None:
                    node.amdgpu_driver_version = amdgpu_ver
                    Logger.info(f"Node {node.host_name}: seeded amdgpu_driver_version={amdgpu_ver} (rocm {spec_rocm_ver})")

@pytest.fixture(scope="session", autouse=True)
def generate_partition_configs(gather_device_info, gpu_cluster, environment):
    """Generate partitioning_check JSON files for every GPU node in the cluster.

    Runs once per session after gather_device_info has populated gpu_series and
    num_gpus on each node. Emits one file per unique (gpu_series, num_gpus) pair
    into environment.logdir so the files are collected as part of CI/CD logs.
    """
    if environment.deployment_mode == "hypervisor":
        return

    if amdgpu_util is None:
        Logger.info("GPU modules not available (lib.amdgpu), skipping partition config generation")
        return

    seen = set()
    for node in gpu_cluster.cluster_nodes:
        if node.gpu_series and node.num_gpus > 0:
            key = (node.gpu_series, node.num_gpus)
            if key not in seen:
                seen.add(key)
                amdgpu_util.generate_partitioning_check_file(
                    node.gpu_series, node.num_gpus, environment.logdir
                )

    Logger.info(f"Generated partition configs for: {sorted(seen)}")

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()

    # Stash each phase's report on the item for the context fixture to read
    item.stash.setdefault("reports", {})[report.when] = report

    if report.when == 'call':
        # Get the docstring from the test function
        description = str(item.function.__doc__) if item.function.__doc__ else ""

        # Format the description to preserve structure
        if description:
            # Clean up the docstring (remove common leading whitespace)
            import textwrap
            import re
            description = textwrap.dedent(description).strip()

            # Extract first paragraph as summary (before first blank line)
            parts = description.split('\n\n', 1)
            summary = parts[0].replace('\n', ' ')

            # Store only the summary (first paragraph) for the description column
            report.description = summary
        else:
            report.description = ""

        if report.failed:
            # 1. Get the raw error message
            error_msg = str(call.excinfo.value) if call.excinfo else "Unknown Error"

            # Store these on the report object so the table hooks can see them
            report.error_summary = error_msg[:300] + ("..." if len(error_msg) > 300 else "")
        else:
            # Default values for passing tests
            report.error_summary = "-"

        json_report.record_test_result(report, item)
    elif report.when == 'setup' and (report.skipped or report.failed):
        json_report.record_test_result(report, item)

def pytest_html_results_table_header(cells):
    cells.insert(2, html.th("Description"))
    cells.insert(3, html.th("Failure Message"))

def pytest_html_results_table_row(report, cells):
    # Format the test ID (nodeid) to be more readable
    # Example: openshift/gpu-operator/test_metrics_values.py::test_exporter_metrics_value_accuracy[GPU_CLOCK:GPU_CLOCK_TYPE_DATA]
    # Should become multi-line format with deployment, application, module, test case, and parameters

    import re
    from py.xml import html as html_builder

    nodeid = getattr(report, 'nodeid', '')

    if nodeid:
        # Parse the nodeid components
        # Format: <path>::<test_name>[<params>]
        parts = nodeid.split('::')
        path_part = parts[0] if len(parts) > 0 else ''
        test_part = parts[1] if len(parts) > 1 else ''

        # Split path into deployment/application/module
        path_components = path_part.split('/')

        # Extract test name and parameters
        param_match = re.match(r'([^\[]+)(\[.+\])?', test_part)
        test_name = param_match.group(1) if param_match else test_part
        params = param_match.group(2) if param_match and param_match.group(2) else ''

        # Build formatted elements using html builder
        formatted_parts = []

        # Add path components (deployment/application)
        if len(path_components) > 2:
            # First component: deployment
            formatted_parts.append(
                html_builder.div(
                    html_builder.strong("Deployment: "),
                    path_components[0],
                    style="color: #6c757d; font-size: 11px;"
                )
            )
            # Second component: application
            formatted_parts.append(
                html_builder.div(
                    html_builder.strong("Application: "),
                    path_components[1],
                    style="color: #6c757d; font-size: 11px;"
                )
            )
            # Module (last component of path)
            formatted_parts.append(
                html_builder.div(
                    html_builder.strong("Module: "),
                    path_components[-1],
                    style="color: #212529; font-size: 12px;"
                )
            )
        else:
            # Just show the full path if it doesn't match expected format
            formatted_parts.append(
                html_builder.div(
                    html_builder.strong("Path: "),
                    path_part,
                    style="color: #212529; font-size: 12px;"
                )
            )

        # Add test name
        formatted_parts.append(
            html_builder.div(
                test_name,
                style="color: #212529; font-weight: 600; margin-top: 4px;"
            )
        )

        # Add parameters if present
        if params:
            # Remove brackets and format parameters
            params_clean = params.strip('[]')
            formatted_parts.append(
                html_builder.div(
                    html_builder.em(params_clean),
                    style="color: #17a2b8; font-size: 11px; margin-top: 2px;"
                )
            )

        # Replace the Test ID cell (cells[1]) with formatted version
        cells[1] = html.td(
            html_builder.div(*formatted_parts),
            style="white-space: normal; max-width: 350px;"
        )

    # Retrieve the description we stored in the previous hook
    description = getattr(report, 'description', "")
    cells.insert(2, html.td(description))
    msg = getattr(report, 'error_summary', "-")
    cells.insert(3, html.td(msg, class_="col-failure"))

def pytest_html_report_title(report):
    """
    Set a custom title for the HTML report based on test suite.
    """
    report.title = "AMD GPU Operator Test Report"

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    """Add custom CSS to beautify HTML reports."""
    yield

@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    json_report.reset(datetime.now())

    config.addinivalue_line(
        "markers", "upgrade: mark test as operator/operand upgrade test"
    )

    """
    Add custom CSS styling to the HTML report for better aesthetics.
    Writes CSS to a temporary file and registers it with pytest-html.
    Also stores config for access in pytest_metadata hook.
    """
    # Store config FIRST (tryfirst ensures this runs before pytest_metadata)
    setattr(pytest, "_config", config)

    # Define custom CSS content
    css_content = """
        /* ==================== Color Scheme ==================== */
        :root {
            --amd-red: #c8102e;
            --success-color: #28a745;
            --warning-color: #b8860b;
            --danger-color: #c0392b;
            --info-color: #17a2b8;
            --border-color: #dee2e6;
            --text-dark: #212529;
            --text-muted: #6c757d;
        }

        /* ==================== Global Styles ==================== */
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: #f5f5f5;
            color: var(--text-dark);
            line-height: 1.5;
            margin: 0;
            padding: 20px;
        }

        /* ==================== Header Styling ==================== */
        h1 {
            background: var(--amd-red) !important;
            color: #fff !important;
            padding: 20px 30px !important;
            margin: 0 0 20px 0 !important;
            font-size: 24px !important;
            font-weight: 600 !important;
            text-align: center !important;
        }

        h2 {
            color: var(--amd-red) !important;
            border-bottom: 2px solid var(--amd-red) !important;
            padding-bottom: 8px !important;
            margin: 24px 0 16px 0 !important;
            font-weight: 600 !important;
            font-size: 18px !important;
        }

        h3 {
            color: var(--text-dark) !important;
            font-weight: 600 !important;
            margin: 16px 0 10px 0 !important;
            font-size: 15px !important;
        }

        /* ==================== Summary Section ==================== */
        #environment, .metadata {
            background: #fff;
            padding: 16px;
            margin: 16px 0;
            border-left: 4px solid var(--amd-red);
            border: 1px solid var(--border-color);
        }

        #environment td {
            padding: 8px 12px !important;
            vertical-align: top !important;
            border-bottom: 1px solid var(--border-color) !important;
        }

        #environment tr:first-child td {
            font-weight: 600;
            min-width: 160px;
        }

        #environment tr:nth-child(odd) {
            background-color: #fafafa !important;
        }

        #environment tr:last-child td {
            border-bottom: none !important;
        }

        #environment ul {
            margin: 0 !important;
            padding: 0 0 0 18px !important;
            list-style-type: disc !important;
        }

        #environment ul li {
            margin: 2px 0 !important;
            line-height: 1.5 !important;
        }

        .summary {
            display: flex;
            flex-wrap: wrap;
            gap: 12px;
            margin: 16px 0;
        }

        .summary > span {
            background: #fff;
            padding: 12px 20px;
            border: 1px solid var(--border-color);
            font-size: 14px;
            font-weight: 600;
            min-width: 140px;
            text-align: center;
        }

        /* ==================== Results Table ==================== */
        #results-table {
            background: #fff;
            margin: 20px 0;
            border: 1px solid var(--border-color) !important;
        }

        #results-table-head {
            background: var(--amd-red) !important;
        }

        #results-table th {
            color: #fff !important;
            font-weight: 600 !important;
            font-size: 12px !important;
            padding: 10px 12px !important;
            border: none !important;
            text-align: left !important;
        }

        #results-table td {
            padding: 10px 12px !important;
            border-bottom: 1px solid var(--border-color) !important;
            font-size: 13px !important;
            vertical-align: middle !important;
        }

        #results-table tbody tr:hover {
            background-color: #f8f8f8 !important;
        }

        #results-table tbody tr:last-child td {
            border-bottom: none;
        }

        /* ==================== Test Status Styling ==================== */
        .passed, tr.passed td {
            background: #eafaf1 !important;
            color: #155724 !important;
            border-left: 4px solid var(--success-color) !important;
        }

        .failed, tr.failed td {
            background: #fdf0f0 !important;
            color: #721c24 !important;
            border-left: 4px solid var(--danger-color) !important;
        }

        .skipped, tr.skipped td {
            background: #fefce8 !important;
            color: #6b4c00 !important;
            border-left: 4px solid var(--warning-color) !important;
        }

        .error, tr.error td {
            background: #fdf0f0 !important;
            color: #721c24 !important;
            border-left: 4px solid var(--danger-color) !important;
        }

        .xfailed, tr.xfailed td,
        .xpassed, tr.xpassed td {
            background: #e8f4f8 !important;
            color: #0c5460 !important;
            border-left: 4px solid var(--info-color) !important;
        }

        /* ==================== Result Column ==================== */
        .col-result {
            text-align: center !important;
            font-weight: 700 !important;
            font-size: 12px !important;
            text-transform: uppercase !important;
        }

        /* ==================== Other Columns ==================== */
        .col-name {
            font-weight: 500 !important;
            color: var(--text-dark) !important;
        }

        .col-duration {
            font-family: 'Courier New', Consolas, monospace !important;
            color: var(--text-muted) !important;
        }

        .col-links a {
            color: var(--amd-red) !important;
            text-decoration: none !important;
            font-weight: 600 !important;
        }

        .col-links a:hover {
            text-decoration: underline !important;
        }

        /* ==================== Description & Failure Message ==================== */
        .col-description {
            color: var(--text-dark);
            max-width: 200px;
            line-height: 1.6;
            white-space: normal;
            word-wrap: break-word;
        }

        td.col-description {
            padding: 10px 12px !important;
        }

        .col-failure {
            max-width: 600px;
            line-height: 1.5;
            white-space: normal;
            word-wrap: break-word;
            overflow-wrap: break-word;
            color: var(--text-dark);
        }

        td.col-failure {
            padding: 10px 12px !important;
        }

        /* ==================== Log Sections ==================== */
        .log {
            background: #1e1e1e !important;
            color: #d4d4d4 !important;
            padding: 14px !important;
            font-family: 'Courier New', Consolas, monospace !important;
            font-size: 12px !important;
            line-height: 1.5 !important;
            overflow-x: auto !important;
            margin: 10px 0 !important;
        }

        /* ==================== Sortable ==================== */
        .sortable {
            cursor: pointer;
            user-select: none;
        }

        /* ==================== Status Counts ==================== */
        span.passed { color: var(--success-color) !important; font-weight: 700 !important; }
        span.failed { color: var(--danger-color) !important; font-weight: 700 !important; }
        span.skipped { color: var(--warning-color) !important; font-weight: 700 !important; }

        #environment p {
            margin: 6px 0;
            line-height: 1.6;
        }

        .logwrapper { overflow-x: auto !important; }
        .logwrapper .log { white-space: pre-wrap !important; word-break: break-all !important; overflow-wrap: break-word !important; }

        @media print {
            body { background: white; }
            .col-links { display: none; }
        }
    """

    # For pytest-html 4.x, we need to write CSS to a file and add it via --css option
    # Only do this if HTML reporting is enabled
    if config.getoption('htmlpath'):
        import tempfile
        import os

        # Create a temporary CSS file in the logs directory (so it persists for debugging)
        log_dir = os.path.join(os.getcwd(), 'logs')
        os.makedirs(log_dir, exist_ok=True)

        css_file_path = os.path.join(log_dir, 'pytest_custom.css')

        # Write CSS content to file
        with open(css_file_path, 'w') as css_file:
            css_file.write(css_content)

        # Add CSS file to pytest-html's css option
        # This is how pytest-html 4.x expects custom CSS
        if not hasattr(config.option, 'css'):
            config.option.css = []
        config.option.css.append(css_file_path)
