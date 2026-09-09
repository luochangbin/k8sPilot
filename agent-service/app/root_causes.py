"""Versioned root cause code vocabulary (design §23.4).

`root_cause_code` is the machine-scorable root cause; free-text `root_cause` is
for users. The vocabulary is versioned: adding/changing codes creates a new
version instead of silently editing an existing baseline.
"""

ROOT_CAUSE_CODE_VERSION = "v1"

# code -> short description
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

ROOT_CAUSE_CODES = ROOT_CAUSE_CODES_V1


def is_valid_root_cause_code(code: str) -> bool:
    return code in ROOT_CAUSE_CODES
