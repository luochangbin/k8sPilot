/** Agent Service client for the Diagnosis Center (no auth; see §2 identity note). */

import type {
  CenterSessionsPage,
  Diagnosis,
  NotificationsPage,
  SessionFilters,
  TimelinePage,
  UnresolvedAlertsPage,
} from './types';

export const AGENT_BASE_URL =
  (window as unknown as { __K8S_PILOT_AGENT_BASE__?: string }).__K8S_PILOT_AGENT_BASE__ ??
  'http://localhost:8000';

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

function buildQuery(params: Record<string, string | number | boolean | undefined | null>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return;
    search.set(key, String(value));
  });
  const query = search.toString();
  return query ? `?${query}` : '';
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${AGENT_BASE_URL}${path}`, {
    ...init,
    headers: { ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => '');
    throw new ApiError(detail || `HTTP ${res.status}`, res.status);
  }
  return (await res.json()) as T;
}

export function listSessions(
  filters: SessionFilters,
  limit: number,
  viewerId?: string,
  signal?: AbortSignal
): Promise<CenterSessionsPage> {
  return request<CenterSessionsPage>(
    `/api/v1/diagnosis-center/sessions${buildQuery({ ...filters, limit, viewer_id: viewerId })}`,
    { signal }
  );
}

export function listNotifications(
  viewerId: string,
  limit = 50,
  after?: string,
  signal?: AbortSignal
): Promise<NotificationsPage> {
  return request<NotificationsPage>(
    `/api/v1/diagnosis-center/notifications${buildQuery({ viewer_id: viewerId, limit, after })}`,
    { signal }
  );
}

export function listUnresolvedAlerts(
  limit = 50,
  after?: string,
  signal?: AbortSignal
): Promise<UnresolvedAlertsPage> {
  return request<UnresolvedAlertsPage>(
    `/api/v1/diagnosis-center/unresolved-alerts${buildQuery({ limit, after })}`,
    { signal }
  );
}

export function markAllNotificationsRead(
  viewerId: string,
  signal?: AbortSignal
): Promise<{ read: number }> {
  return request<{ read: number }>('/api/v1/diagnosis-center/notifications/read-all', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ viewer_id: viewerId }),
    signal,
  });
}

export function listNamespaces(signal?: AbortSignal): Promise<{ items: string[] }> {
  return request<{ items: string[] }>('/api/v1/diagnosis-center/namespaces', { signal });
}

export function getDiagnosis(diagnosisId: string, signal?: AbortSignal): Promise<Diagnosis> {
  return request<Diagnosis>(`/api/v1/diagnoses/${encodeURIComponent(diagnosisId)}`, { signal });
}

export function getTimeline(
  diagnosisId: string,
  after: number,
  limit = 100,
  signal?: AbortSignal
): Promise<TimelinePage> {
  return request<TimelinePage>(
    `/api/v1/diagnoses/${encodeURIComponent(diagnosisId)}/timeline${buildQuery({
      after,
      limit,
    })}`,
    { signal }
  );
}

export function markRead(
  diagnosisId: string,
  viewerId: string,
  signal?: AbortSignal
): Promise<{ id: string; read: boolean }> {
  return request<{ id: string; read: boolean }>(
    `/api/v1/diagnosis-center/sessions/${encodeURIComponent(diagnosisId)}/read`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ viewer_id: viewerId }),
      signal,
    }
  );
}
