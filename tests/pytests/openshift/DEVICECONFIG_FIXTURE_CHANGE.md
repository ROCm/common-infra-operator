# Using argo_workflow_setup in test_node_remediation.py

## Overview

The `argo_workflow_setup` fixture now exists in **both** k8 and OpenShift conftest files, allowing `test_node_remediation.py` to work seamlessly across both platforms.

## The Two Implementations

### 1. Vanilla Kubernetes (tests/pytests/k8/conftest.py)

**Dummy/No-op fixture** - Does nothing because GPU Operator manages Argo automatically.

```python
@pytest.fixture(scope="module")
def argo_workflow_setup(environment):
    """Dummy fixture - GPU Operator installs Argo automatically"""
    argo_info = {
        "namespace": environment.gpu_operator_namespace,
        "installed_by_fixture": False,
        "managed_by": "gpu-operator",
        "platform": "vanilla-kubernetes"
    }
    yield argo_info
    # No cleanup - GPU Operator handles it
```

### 2. OpenShift (tests/pytests/openshift/conftest.py)

**Real implementation** - Installs and manages Argo Workflows.

```python
@pytest.fixture(scope="module")
def argo_workflow_setup(gpu_cluster, environment, request):
    """Real fixture - Installs Argo for OpenShift ANR"""
    # Check if Argo exists
    # Install if needed
    # Verify installation
    # Cleanup after tests
    ...
```

## How to Use in test_node_remediation.py

### Single Change Required

Add `argo_workflow_setup` to the `deviceconfig_install` fixture:

**BEFORE:**

```python
@pytest.fixture(scope="module")
def deviceconfig_install(gpu_cluster, images, gpu_operator_install, environment, request):
```

**AFTER:**

```python
@pytest.fixture(scope="module")
def deviceconfig_install(gpu_cluster, images, gpu_operator_install,
                        argo_workflow_setup, environment, request):
```

That's it! No other changes needed.

## How It Works

pytest automatically selects the correct fixture based on test location:

```text
Running: tests/pytests/k8/gpu-operator/test_node_remediation.py
    ↓
Uses: tests/pytests/k8/conftest.py::argo_workflow_setup
    ↓
Result: Dummy fixture (GPU Operator manages Argo)


Running: tests/pytests/openshift/gpu-operator/test_node_remediation.py
    ↓
Uses: tests/pytests/openshift/conftest.py::argo_workflow_setup
    ↓
Result: Real fixture (Installs/manages Argo)
```

## Platform-Specific Behavior

### On Vanilla Kubernetes

```text
Test starts
  → argo_workflow_setup (dummy) runs
  → Returns info: managed_by="gpu-operator"
  → GPU Operator installs Argo via Helm
  → Test proceeds
  → No Argo cleanup needed
Test ends
```

### On OpenShift

```text
Test starts
  → argo_workflow_setup (real) runs
  → Checks for existing Argo
  → Installs if needed (CRDs + controller)
  → Verifies installation
  → Returns info: installed_by_fixture=True/False
  → Test proceeds
  → Cleanup: Removes Argo only if installed by fixture
Test ends
```

## Why This Approach?

✅ **Single test file** - Works on both platforms
✅ **No platform checks** - pytest handles fixture resolution
✅ **Clean separation** - Platform-specific code in conftest files
✅ **Maintainable** - Each platform owns its setup logic
✅ **Safe** - Dummy fixture prevents accidental Argo management on vanilla K8s

## Complete Example

```python
# test_node_remediation.py

@pytest.fixture(scope="module")
def deviceconfig_install(gpu_cluster, images, gpu_operator_install,
                        argo_workflow_setup, environment, request):
    global Logger

    # Optional: Access Argo info (works on both platforms)
    argo_info = argo_workflow_setup
    Logger.info(f"Argo managed by: {argo_info.get('managed_by', 'fixture')}")

    # Rest of your code stays the same...
    def _deviceconfig_cleanup():
        # ... cleanup code ...

    # ... rest of fixture ...
    yield devcfg_info


def test_anr_workflow(gpu_cluster, images, deviceconfig_install, environment, request):
    """This test now works on both vanilla K8s and OpenShift!"""

    # Enable remediation
    # ... your test code ...

    # Wait for workflow
    # ... your test code ...

    # Verify results
    # ... your test code ...
```

## Testing

### Test on Vanilla Kubernetes

```bash
pytest tests/pytests/k8/gpu-operator/test_node_remediation.py::test_anr_workflow -v
# Uses dummy fixture from k8/conftest.py
```

### Test on OpenShift

```bash
pytest tests/pytests/openshift/gpu-operator/test_node_remediation.py::test_anr_workflow -v
# Uses real fixture from openshift/conftest.py
```

## Fixture Return Values

Both fixtures return compatible dictionaries:

### Vanilla K8s

```python
{
    "namespace": "kube-amd-gpu",
    "installed_by_fixture": False,
    "managed_by": "gpu-operator",
    "platform": "vanilla-kubernetes"
}
```

### OpenShift

```python
{
    "namespace": "argo-workflow",
    "installed_by_fixture": True,  # or False if pre-existing
    "version": "v3.6.5",
    "preexisting": False  # or True if found existing installation
}
```

## Migration Checklist

- [ ] Add `argo_workflow_setup` to `deviceconfig_install` signature
- [ ] (Optional) Update tests to use ANR utilities from `lib/autoremediation_util.py`
- [ ] Test on vanilla Kubernetes cluster
- [ ] Test on OpenShift cluster
- [ ] Verify Argo cleanup happens correctly on both platforms

## Troubleshooting

### Issue: "fixture 'argo_workflow_setup' not found"

**Solution:** Make sure the fixture exists in the appropriate conftest.py:

- For k8 tests: `tests/pytests/k8/conftest.py`
- For OpenShift tests: `tests/pytests/openshift/conftest.py`

### Issue: Argo installed twice on vanilla K8s

**Solution:** This shouldn't happen with the dummy fixture. Check that you're using the k8/conftest.py version (dummy), not accidentally importing from openshift/.

### Issue: Argo not installed on OpenShift

**Solution:** Verify the real fixture is in `openshift/conftest.py` and check test logs for installation errors.

## Summary

| Platform | Fixture Location | Behavior |
| -------- | --------------- | -------- |
| Vanilla K8s | `k8/conftest.py` | Dummy/no-op (GPU Operator manages Argo) |
| OpenShift | `openshift/conftest.py` | Real (Installs/manages Argo) |

Both fixtures provide the same interface, so your test code works everywhere!
