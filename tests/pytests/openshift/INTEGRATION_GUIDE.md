# Integration Guide: Using argo_workflow_setup in test_node_remediation.py

## Quick Start

To use the `argo_workflow_setup` fixture from `tests/pytests/openshift/conftest.py` in your `test_node_remediation.py`, follow these simple steps:

## Step 1: Add the fixture as a dependency

Modify the `deviceconfig_install` fixture signature:

### Before

```python
@pytest.fixture(scope="module")
def deviceconfig_install(gpu_cluster, images, gpu_operator_install, environment, request):
```

### After

```python
@pytest.fixture(scope="module")
def deviceconfig_install(gpu_cluster, images, gpu_operator_install,
                        argo_workflow_setup, environment, request):  # Add argo_workflow_setup
```

## Step 2: (Optional) Access Argo info in your fixture

If you need to know about the Argo installation:

```python
@pytest.fixture(scope="module")
def deviceconfig_install(gpu_cluster, images, gpu_operator_install,
                        argo_workflow_setup, environment, request):
    global Logger

    # Access Argo information
    argo_info = argo_workflow_setup
    Logger.info(f"Argo Workflows available in namespace: {argo_info['namespace']}")
    Logger.info(f"Installed by fixture: {argo_info['installed_by_fixture']}")

    # Rest of your existing code...
    # cleanup - remove any deviceconfigs
    def _deviceconfig_cleanup():
        # ... existing cleanup code ...
```

## Step 3: That's it

No other changes needed! The fixture will:

- ✅ Check if Argo is installed
- ✅ Install it if needed (CRDs + controller)
- ✅ Verify it's working
- ✅ Clean up after tests (if it installed it)

## Complete Minimal Change Example

```python
# In test_node_remediation.py

# ONLY CHANGE: Add argo_workflow_setup to the parameter list
@pytest.fixture(scope="module")
def deviceconfig_install(gpu_cluster, images, gpu_operator_install,
                        argo_workflow_setup,  # <-- ADD THIS
                        environment, request):
    global Logger

    # All your existing code stays the same!
    # cleanup - remove any deviceconfigs
    def _deviceconfig_cleanup():
        devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
        # ... rest of existing code ...
```

## How It Works

```text
test_node_remediation.py::deviceconfig_install
    ↓ depends on
openshift/conftest.py::argo_workflow_setup
    ↓ automatically
    1. Checks for Argo (OpenShift AI or manual)
    2. Installs if not found
    3. Verifies installation
    4. Provides info to dependent fixtures
    5. Cleans up after module tests complete
```

## Execution Order

When you run a test that uses `deviceconfig_install`:

1. **pytest discovers dependencies**
   - `deviceconfig_install` needs `argo_workflow_setup`

2. **argo_workflow_setup runs first** (module scope)
   - Checks for Argo
   - Installs if needed
   - Returns installation info

3. **deviceconfig_install runs** (module scope)
   - Receives `argo_workflow_setup` result
   - Proceeds with DeviceConfig setup

4. **Test runs**
   - Uses deviceconfig_install
   - Argo is already available

5. **Cleanup (after all module tests)**
   - `deviceconfig_install` cleanup runs
   - `argo_workflow_setup` cleanup runs (removes Argo if it installed it)

## Platform-Specific Behavior

The fixture is smart about platform detection:

### On OpenShift

```python
# In openshift/conftest.py - fixture is available
@pytest.fixture(scope="module")
def argo_workflow_setup(gpu_cluster, environment, request):
    # Installs/detects Argo
    ...
```

### On Vanilla Kubernetes

```python
# In k8/conftest.py - fixture is a no-op shim
@pytest.fixture(scope="module")
def argo_workflow_setup(environment):
    # No-op on vanilla K8s: Argo (if needed) is handled by GPU Operator
    argo_info = {
        "namespace": environment.gpu_operator_namespace,
        "installed_by_fixture": False,
        "managed_by": "gpu-operator",
        "platform": "vanilla-kubernetes"
    }
    yield argo_info
    # No cleanup - GPU Operator manages Argo lifecycle
```

## Testing Different Scenarios

### Scenario 1: Argo Already Installed (OpenShift AI)

```text
Test starts
  → argo_workflow_setup checks for Argo
  → Finds existing Argo (via DataScienceCluster)
  → Returns info with installed_by_fixture=False
  → Test runs using existing Argo
  → Cleanup: Skips Argo removal (was pre-existing)
Test ends
```

### Scenario 2: Argo Not Installed

```text
Test starts
  → argo_workflow_setup checks for Argo
  → Argo not found
  → Installs CRDs via Python K8s API
  → Installs controller via Helm
  → Verifies installation
  → Returns info with installed_by_fixture=True
  → Test runs using newly installed Argo
  → Cleanup: Removes Argo (keeps CRDs for reuse)
Test ends
```

### Scenario 3: CRDs Exist, Controller Missing

```text
Test starts
  → argo_workflow_setup checks for Argo
  → Finds CRDs but no controller
  → Skips CRD installation
  → Installs controller via Helm
  → Returns info with installed_by_fixture=True
  → Test runs
  → Cleanup: Removes controller, keeps CRDs
Test ends
```

## Advantages of This Approach

✅ **Zero Duplication**: No need to write Argo installation code in each test
✅ **Automatic**: Works without manual intervention
✅ **Safe**: Won't remove pre-existing Argo installations
✅ **Reusable**: Multiple test modules can use the same fixture
✅ **Clean**: Proper cleanup after tests
✅ **Smart**: Detects OpenShift AI installations

## Troubleshooting

### Issue: Fixture not found

**Error:**

```text
fixture 'argo_workflow_setup' not found
```

**Solution:**

Make sure you're running tests from the OpenShift directory or that pytest can find `tests/pytests/openshift/conftest.py`.

### Issue: Argo installation fails

**Error:**

```text
Failed to install Argo Workflows: <error>
```

**Solution:**

Check:

1. Helm is available: `helm version`
2. Network access to GitHub (for CRD download)
3. Permissions to create namespaces and CRDs
4. Check logs in test output for specific error

### Issue: Cleanup not happening

**Behavior:**
Argo remains after tests complete

**Explanation:**
This is intentional! The fixture only cleans up Argo if it installed it. If Argo was pre-existing (e.g., from OpenShift AI), it won't be removed.

## Advanced: Customizing the Fixture

If you need different behavior, you can override in your test file:

```python
@pytest.fixture(scope="module")
def argo_workflow_setup(gpu_cluster, environment, request):
    """Custom Argo setup for this specific test module"""
    # Your custom implementation
    ...
```

Or modify the central fixture in `openshift/conftest.py` with parameters:

```python
@pytest.fixture(scope="module", params=["v3.6.5", "v3.7.10"])
def argo_workflow_setup(gpu_cluster, environment, request):
    argo_version = request.param  # Test multiple versions
    ...
```
