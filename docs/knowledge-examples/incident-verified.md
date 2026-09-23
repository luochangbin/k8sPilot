---
record_type: incident
incident_id: example-incident-verified
status: verified
product: payments-api
product_version: "2.4.1"
environment: staging
resource_kind: Pod
symptoms:
  - CrashLoopBackOff
  - database connection timeout
root_cause_code: DATABASE_CONNECTION_TIMEOUT
remediation_summary: Corrected the connection pool timeout configuration.
verification:
  outcome: success
  method: Pod became Ready and restart count stopped increasing.
evidence_summary: Previous container logs reported a database connection timeout.
---

Operator-confirmed example record. Replace all example details with reviewed,
non-sensitive incident information before importing real cases.
