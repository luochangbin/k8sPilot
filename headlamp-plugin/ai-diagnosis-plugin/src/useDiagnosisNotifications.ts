/**
 * Shared unread notifications state for the Diagnosis Center (design §4).
 *
 * One module-level poller is shared by the Center, the Detail page and the app
 * bar badge: 15s cadence, paused while the tab is hidden, exponential backoff
 * up to 30s, and a semantic (not bare-number) aria-live message that only
 * changes when the count changes.
 */

import { useEffect, useState } from 'react';
import { listNotifications } from './api';
import type { NotificationItem } from './types';
import { getViewerId, subscribeReadRevision } from './viewer';

const POLL_MS = 15000;
const MAX_BACKOFF_MS = 30000;

interface State {
  unreadCount: number;
  pendingAlertCount: number;
  items: NotificationItem[];
  error: string | null;
  ready: boolean;
  liveMessage: string;
}

let state: State = {
  unreadCount: 0,
  pendingAlertCount: 0,
  items: [],
  error: null,
  ready: false,
  liveMessage: '',
};
const listeners = new Set<() => void>();
let timer: ReturnType<typeof setTimeout> | null = null;
let delay = POLL_MS;
let generation = 0;
let started = false;

function emit(): void {
  listeners.forEach(listener => listener());
}

function setState(patch: Partial<State>): void {
  state = { ...state, ...patch };
  emit();
}

function liveMessageFor(unreadDiagnoses: number, pendingAlerts: number): string {
  if (unreadDiagnoses === 0 && pendingAlerts === 0) return '没有未读诊断或待处理告警';
  const parts: string[] = [];
  if (unreadDiagnoses > 0) parts.push(`${unreadDiagnoses} 条未读诊断`);
  if (pendingAlerts > 0) parts.push(`${pendingAlerts} 条待处理告警`);
  return parts.join('，');
}

function schedule(): void {
  if (timer) clearTimeout(timer);
  timer = setTimeout(() => {
    void tick();
  }, delay);
}

async function refresh(): Promise<void> {
  const gen = ++generation;
  try {
    const page = await listNotifications(getViewerId(), 50);
    if (gen !== generation) return;
    // Unread diagnoses and pending alerts are independent counts: only the
    // former drives the badge/mark-all-read affordance.
    const unreadDiagnoses = page.unread_diagnosis_count ?? page.unread_count;
    const pendingAlerts = page.pending_alert_count ?? 0;
    const changed =
      unreadDiagnoses !== state.unreadCount || pendingAlerts !== state.pendingAlertCount;
    delay = POLL_MS;
    setState({
      unreadCount: unreadDiagnoses,
      pendingAlertCount: pendingAlerts,
      items: page.items,
      error: null,
      ready: true,
      liveMessage:
        changed || !state.liveMessage
          ? liveMessageFor(unreadDiagnoses, pendingAlerts)
          : state.liveMessage,
    });
  } catch (err) {
    if (gen !== generation) return;
    delay = Math.min(delay * 2, MAX_BACKOFF_MS);
    setState({ error: (err as Error).message, ready: true });
  }
}

async function tick(): Promise<void> {
  if (document.visibilityState === 'visible') {
    await refresh();
  }
  schedule();
}

function onVisibilityChange(): void {
  if (document.visibilityState === 'visible') {
    void refresh();
  }
}

function ensureStarted(): void {
  if (started) return;
  started = true;
  void refresh().then(schedule);
  subscribeReadRevision(() => {
    void refresh();
  });
  document.addEventListener('visibilitychange', onVisibilityChange);
}

/** Re-read notifications now (e.g. right after marking a diagnosis read). */
export function refreshDiagnosisNotifications(): void {
  void refresh();
}

export function useDiagnosisNotifications(): State & { refresh: () => void } {
  const [, force] = useState(0);
  useEffect(() => {
    ensureStarted();
    const listener = () => force(value => value + 1);
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  }, []);
  return { ...state, refresh: refreshDiagnosisNotifications };
}
