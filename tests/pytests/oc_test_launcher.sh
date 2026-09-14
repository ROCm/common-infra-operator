#!/usr/bin/bash

#
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the \"License\");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an \"AS IS\" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

function usage() {
    echo ""
    echo "Usage: $0 [options]"
    echo "          --help print help/usage information"
    echo "          --app <app-name>. Default: gpu-operator"
    echo "          --secrets secrets.json file"
    echo "          --amdgpu-driver-spec <driver-version-spec>"
    echo "          --workload-selection <workload-name>"
    echo "          --image-manifest <path-to-image-manifest>"
    echo "          --module <module-name>. Eq: test_<module_name>.py"
    echo "          --testcase <testcase-name> Eq: def test_<tc_name>"
    echo "          --debug"
    echo ""
    echo "Environment Variables:"
    echo "TECH_SUPPORT_TOOL : Path to tech-support-dump.sh, default: $PWD/techsupport_dump.sh"
    echo ""
}

IMAGE_MANIFEST="NA"
SECRETS="NA"
APP_NAME="gpu-operator"
DEPLOYMENT="openshift"
TC_MODULE="ALL"
TC_NAME="ALL"
ENABLE_DEBUGGING="NA"
DRIVER_SPEC="NA"
WORKLOAD_NAME="NA"
TECH_SUPPORT_TOOL=${TECH_SUPPORT_TOOL:-"${PWD}/techsupport_dump.sh"}

function setup_pyenv() {
    if [[ -f venv/bin/activate ]] ;
    then
        echo "pyvenv is already ready"
        source venv/bin/activate &> /dev/null
    else
        echo "Setup pyenv with all required packages"
        $PWD/scripts/prepare_env.sh $PWD/venv &> /dev/null
        source $PWD/venv/bin/activate &> /dev/null
    fi
    export PYTHONPATH=$PYTHONPATH:$PWD
}

function install_operator_sdk_tool() {
    echo "Download operator-sdk tool install in local folder"
    if [[ -f $PWD/bin/operator-sdk ]];
    then
        echo "operator-sdk tool already downloaded"
    else
        mkdir -p $PWD/bin
        curl -LO  https://github.com/operator-framework/operator-sdk/releases/download/v1.42.0/operator-sdk_linux_amd64
        mv operator-sdk_linux_amd64 $PWD/bin/operator-sdk
        chmod +x $PWD/bin/operator-sdk
    fi
    export PATH=$PATH:$PWD/bin
}

function launch_pytest() {
    mkdir -p logs
    setup_pyenv
    install_operator_sdk_tool
    local test_sel=${DEPLOYMENT}/${APP_NAME}
    local html_file=logs/${DEPLOYMENT}_${APP_NAME}.html
    local xml_file=logs/${DEPLOYMENT}_${APP_NAME}.xml
    if [[ "${TC_MODULE}" != "ALL" ]];
    then
        if [[ "${TC_NAME}" != "ALL" ]];
        then
            html_file=logs/${DEPLOYMENT}_${TC_MODULE}_${TC_NAME}.html
            xml_file=logs/${DEPLOYMENT}_${TC_MODULE}_${TC_NAME}.xml
            test_sel="${DEPLOYMENT}/${APP_NAME}/test_${TC_MODULE}.py::test_${TC_NAME}"
        else
            html_file=logs/${DEPLOYMENT}_${APP_NAME}_${TC_MODULE}.html
            xml_file=logs/${DEPLOYMENT}_${APP_NAME}_${TC_MODULE}.xml
            test_sel="${DEPLOYMENT}/${APP_NAME}/ -k ${TC_MODULE}"
        fi
    fi
    # pytest options:
    # --verbose: Show detailed test output
    # --show-capture=log: Capture and display log output in reports
    # --self-contained-html: Embed all assets (CSS/JS) in HTML report for portability
    # -p no:warnings: Disable warnings plugin (suppresses pytest warnings)
    # --disable-warnings: Suppress all warnings in output
    CMD_OPT="--verbose --show-capture=log -p no:warnings --disable-warnings --self-contained-html"
    if [[ "${ENABLE_DEBUGGING}" == "YES" ]];
    then
        CMD_OPT+=" --pdb"
    fi
    if [[ "${SECRETS}" != "NA" ]];
    then
        CMD_OPT+=" --secrets-json ${SECRETS}"
    fi
    if [[ "${DRIVER_SPEC}" != "NA" ]];
    then
        CMD_OPT+=" --amdgpu-driver-spec ${DRIVER_SPEC}"
    fi
    if [[ "${WORKLOAD_NAME}" != "NA" ]];
    then
        CMD_OPT+=" --workload-selection ${WORKLOAD_NAME}"
    fi
    CMD_OPT+=" --image-manifest ${IMAGE_MANIFEST}"
    echo ""
    echo "****** USING FOLLOWING IMAGES FOR THE TEST ******"
    cat ${IMAGE_MANIFEST}
    cp $IMAGE_MANIFEST logs/
    echo ""

    if [[ -f $TECH_SUPPORT_TOOL ]];
    then
        CMD_OPT+=" --tech-support-tool ${TECH_SUPPORT_TOOL}"
    else
        echo ""
        echo "ALERT:  **** Missing ${TECH_SUPPORT_TOOL}, no tech-support will be collected ****"
        echo ""
    fi
    echo "Running test with cmd-opt ${CMD_OPT}"
    export PYTHONIOENCODING=utf-8
    export GPU_OPERATOR_NAMESPACE="openshift-amd-gpu"
    # Operator-specific test dirs (gpu-operator-*, network-operator-*) live outside
    # ${DEPLOYMENT}/${APP_NAME}/ so they are never collected during sanity runs.
    # To run them, use --app <dir-name> (e.g. --app gpu-operator-amdgpu-driver).
    pytest ${test_sel} \
        --log-file=logs/${DEPLOYMENT}_test_run.log \
        --log-file-level=INFO \
        --junit-xml=${xml_file} --deployment ${DEPLOYMENT} ${CMD_OPT} \
        --html ${html_file}
    ret=$?
    exit $ret
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --image-manifest)
            IMAGE_MANIFEST="$2"
            shift
        ;;
	--app)
	    APP_NAME="$2"
	    shift
	;;
	--module)
	    TC_MODULE="$2"
	    shift
	;;
	--testcase)
	    TC_NAME="$2"
	    shift
	;;
        --secrets)
            SECRETS="$2"
            shift
        ;;
        --amdgpu-driver-spec)
            DRIVER_SPEC="$2"
            shift
        ;;
        --workload-selection)
            WORKLOAD_NAME="$2"
            shift
        ;;
	--debug)
            ENABLE_DEBUGGING="YES"
	;;
        --help)
            usage
            exit 0
        ;;
        --*)
            echo "Unknown option $1"
            exit 1
        ;;
    esac
    shift
done

if [[ "${IMAGE_MANIFEST}" == "NA" ]];
then
    echo "ERROR: Missing argument --image-manifest"
    usage
    exit 1
fi

launch_pytest
