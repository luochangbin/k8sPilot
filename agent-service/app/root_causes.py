"""Versioned root cause code vocabulary (design §23.4).

`root_cause_code` is the machine-scorable root cause; free-text `root_cause` is
for users. The vocabulary is versioned: adding/changing codes creates a new
version instead of silently editing an existing baseline.

Two levels exist on purpose:

* **v1 codes** describe the Kubernetes *failure mode* (symptom): CrashLoopBackOff,
  Pending/FailedScheduling, ImagePullBackOff, CreateContainerConfigError...
  They stay valid so old cases and benchmarks remain scoreable, but they are the
  coarse answer: submitting one is accepted only where no finer code applies.
* **v2 codes** describe the *cause* behind that mode: the container process exits
  non-zero, the scheduler could not match the node selector, the registry
  rejected the credentials, the ConfigMap is missing, the PVC is unbound...

Case ground truth for cause-level scenarios must use v2 codes (`accepted_root_cause_codes`)
so that "restate the Kubernetes status" cannot score as a root cause.
"""

ROOT_CAUSE_CODE_VERSION = "v2"

# v1: failure-mode level (kept for compatibility and for genuinely ambiguous cases).
ROOT_CAUSE_CODES_V1 = {
    "CONTAINER_OOMKILLED": "container was terminated by the OOM killer (exceeded memory limit)",
    "IMAGE_PULL_FAILED": "container image could not be pulled",
    "SCHEDULING_FAILED": "pod could not be scheduled to any node",
    "CRASH_LOOP_BACKOFF": "container keeps crashing and restarting without a definitive signal",
    "CONFIG_ERROR": "configuration error (mount, env, image config, invalid container config)",
    "NODE_UNAVAILABLE": "node is NotReady or otherwise unavailable",
    "VOLUME_MOUNT_FAILED": "volume mount failed",
    "HEALTHY": "resource is healthy; no anomaly found",
    "INSUFFICIENT_EVIDENCE": "not enough evidence to determine a unique root cause",
    "OTHER": "root cause outside the current vocabulary",
}

# v2: cause level — the actual reason behind the observed failure mode.
ROOT_CAUSE_CODES_V2 = {
    "APPLICATION_EXIT_NONZERO": "application process exits with a non-zero code on start",
    "NODE_SELECTOR_MISMATCH": "no node matches the pod's nodeSelector / node affinity",
    "TAINT_TOLERATION_MISMATCH": "no node tolerates the pod's taints/tolerations",
    "INSUFFICIENT_NODE_RESOURCES": "nodes lack the requested cpu/memory (or pod count) capacity",
    "PVC_UNBOUND": "referenced PersistentVolumeClaim is not bound (missing/mismatched StorageClass)",
    "MISSING_CONFIGMAP": "referenced ConfigMap does not exist (envFrom/volume/env value)",
    "MISSING_SECRET": "referenced Secret does not exist (envFrom/volume)",
    "REGISTRY_AUTH_FAILED": "image registry rejected the pull credentials",
    "IMAGE_NOT_FOUND": "image tag/reference does not exist in the registry",
    "VOLUME_MOUNT_FAILED": "volume could not be mounted by kubelet (driver/attachment error)",
}

# The combined vocabulary the agent may submit and the scorer validates.
ROOT_CAUSE_CODES = {**ROOT_CAUSE_CODES_V1, **ROOT_CAUSE_CODES_V2}

# Codes that only name the Kubernetes *failure mode* (what the status shows), not
# the reason behind it. Cause-level case ground truth must not accept these:
# doing so would let "restate the status" score as a root cause.
# CONTAINER_OOMKILLED / VOLUME_MOUNT_FAILED / NODE_UNAVAILABLE are also v1 but DO
# name a cause, so they are not listed here.
FAILURE_MODE_CODES = frozenset({
    "CRASH_LOOP_BACKOFF",
    "SCHEDULING_FAILED",
    "IMAGE_PULL_FAILED",
    "CONFIG_ERROR",
})


def is_valid_root_cause_code(code: str) -> bool:
    return code in ROOT_CAUSE_CODES
