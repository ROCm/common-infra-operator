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
import os
import sys
import time
import json
import glob
import logging
import re

Logger = logging.getLogger("lib.debutil")

def parse_ss_output(data):
    # The raw output from ss command
    """
    tcp  LISTEN 0      4096           127.0.0.1:10248      0.0.0.0:* users:(("kubelet",pid=2262073,fd=17))
    tcp  LISTEN 0      4096                   *:10250            *:* users:(("kubelet",pid=2262073,fd=20))
    """
    results = []

    # This regex looks for the local address column (the 5th column)
    # It captures everything before the last colon as 'address'
    # and the digits after the colon as 'port'
    pattern = r'\s+(?P<address>\S+):(?P<port>\d+)\s+'

    for line in data.strip().split('\n'):
        match = re.search(pattern, line)
        if match:
            results.append({
                "address": match.group("address"),
                "port": match.group("port")
            })

    return results
