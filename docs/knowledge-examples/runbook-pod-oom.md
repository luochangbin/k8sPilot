---
source_type: runbook
title: Pod OOMKilled triage
product: kubernetes
versions:
  - "1.x"
resource_kinds:
  - Pod
  - Node
status: active
source_uri: https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
---

# Symptoms

The container terminates with `reason=OOMKilled` and usually exit code 137.

## Checks

1. Compare container memory usage with its configured memory limit.
2. Inspect the previous container logs and recent Pod events.
3. Check whether the node reports memory pressure.

## Remediation

Review the application's memory profile before changing limits. Verify that the
restart count stops increasing after the change.
