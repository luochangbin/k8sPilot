/** Diagnosis Center API types (see docs/diagnosis-center-handoff.md §3). */

export interface AlertProjection {
  fingerprint: string;
  alertname?: string | null;
  state: string;
  starts_at?: string | null;
  latest_alert_at?: string | null;
  resolved_at?: string | null;
}

export interface ResourceSummary {
  kind?: string;
  namespace?: string;
  name?: string;
  uid?: string;
}

export interface DiagnosisSummary {
  symptom?: string;
  root_cause?: string | null;
  root_cause_code?: string | null;
  confidence?: string | null;
  insufficient_evidence?: boolean;
}

export interface Evidence {
  source: string;
  observed_at?: string | null;
  summary: string;
}

export interface KnowledgeReference {
  retrieval_id: string;
  content: string;
  used_for?: string;
  citation?: {
    document_id?: string;
    title?: string;
    source_uri?: string;
    section?: string;
    version?: string;
  };
}

export interface HistoricalCase {
  retrieval_id: string;
  incident_id?: string;
  product?: string;
  product_version?: string;
  root_cause_code?: string;
  symptoms?: string[];
  evidence_summary?: string;
  remediation_summary?: string;
  used_for?: string;
  verification?: { outcome?: string; verified_at?: string };
}

export interface DiagnosisResult {
  symptom: string;
  root_cause?: string | null;
  root_cause_code?: string | null;
  confidence?: string | null;
  evidence: Evidence[];
  recommendations: string[];
  missing_evidence: string[];
  insufficient_evidence?: boolean;
  investigation_steps: string[];
  historical_cases?: HistoricalCase[];
  knowledge_references?: KnowledgeReference[];
}

export interface Diagnosis {
  diagnosis_id: string;
  trigger: string;
  resource: ResourceSummary;
  status: 'queued' | 'investigating' | 'completed' | 'failed';
  result?: DiagnosisResult | null;
  error?: string | null;
  alert?: AlertProjection | null;
  created_at: string;
  updated_at: string;
}

export interface CenterSession {
  diagnosis_id: string;
  trigger: string;
  status: string;
  resource: ResourceSummary;
  alert?: AlertProjection | null;
  summary?: DiagnosisSummary | null;
  created_at: string;
  updated_at: string;
  unread?: boolean | null;
}

export interface CenterSessionsPage {
  items: CenterSession[];
  next_cursor: string | null;
}

export interface NotificationItem {
  diagnosis_id: string;
  status: string;
  unread: boolean;
}

export interface NotificationsPage {
  unread_count: number;
  items: NotificationItem[];
  next_cursor: string | null;
}

export interface TimelineItem {
  id: string;
  seq: number;
  timestamp?: number | null;
  kind: string;
  title: string;
  status: string;
  duration_ms?: number | null;
  failure_layer?: string | null;
}

export type TimelineAvailability = 'available' | 'pending' | 'unavailable';

export interface TimelinePage {
  items: TimelineItem[];
  next_after: number;
  has_more: boolean;
  available: TimelineAvailability;
  gap: boolean;
}

export interface UnresolvedAlert {
  id: number;
  alertname?: string | null;
  starts_at?: string | null;
  latest_alert_at?: string | null;
  state: string;
  target?: ResourceSummary | null;
}

export interface UnresolvedAlertsPage {
  items: UnresolvedAlert[];
  next_cursor: string | null;
}

export interface SessionFilters {
  status?: string;
  trigger?: string;
  resource_kind?: string;
  namespace?: string;
  name?: string;
  uid?: string;
  since?: string;
  until?: string;
  after?: string;
  /** 1-based page number (keyset pages, kept in the URL for refresh/back). */
  page?: number;
  /** Rows per page; defaults to the Headlamp table setting. */
  perPage?: number;
}
