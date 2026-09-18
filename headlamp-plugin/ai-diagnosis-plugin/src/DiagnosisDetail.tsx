/** Diagnosis detail page: result, evidence and a growing timeline (design §4). */

import { SectionBox } from '@kinvolk/headlamp-plugin/lib/CommonComponents';
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Divider,
  Link as MuiLink,
  List,
  ListItem,
  ListItemText,
  Stack,
  Typography,
} from '@mui/material';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { getDiagnosis, getTimeline, markRead } from './api';
import InvestigationSteps from './InvestigationSteps';
import { centerUrl } from './routes';
import type { Diagnosis, TimelineItem, TimelinePage } from './types';
import { refreshDiagnosisNotifications } from './useDiagnosisNotifications';
import { bumpReadRevision, getViewerId } from './viewer';

const STATUS_POLL_MS = 2000;
const MAX_BACKOFF_MS = 30000;
const PENDING_RETRY_MS = 2000;
const PENDING_TIMEOUT_MS = 30000;
const MAX_PAGES_PER_DRAIN = 20;
const TERMINAL_WAIT_MS = 30000;

const TERMINAL_EVENT_KINDS = ['diagnosis_completed', 'diagnosis_failed'];

function isTerminal(status?: string): boolean {
  return status === 'completed' || status === 'failed';
}

export default function DiagnosisDetail() {
  const { diagnosisId } = useParams<{ diagnosisId: string }>();
  const [diagnosis, setDiagnosis] = useState<Diagnosis | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [timeline, setTimeline] = useState<TimelineItem[]>([]);
  const [timelineState, setTimelineState] = useState<TimelinePage['available']>('pending');
  const [timelineGap, setTimelineGap] = useState(false);
  const [readError, setReadError] = useState<string | null>(null);
  const [readDone, setReadDone] = useState(false);
  const [timelineIncomplete, setTimelineIncomplete] = useState(false);

  // Generation guards make late responses from a previous diagnosis harmless.
  const runRef = useRef(0);
  const offsetRef = useRef(0);
  const seenIdsRef = useRef<Set<string>>(new Set());
  const timelineInFlightRef = useRef(false);
  const timelineInFlightPromiseRef = useRef<Promise<void> | null>(null);
  const finalEventSeenRef = useRef(false);
  const terminalDeadlineRef = useRef<number | null>(null);
  const statusRef = useRef<string | undefined>(undefined);
  const pendingSinceRef = useRef<number | null>(null);
  const pendingTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const readAttemptedRef = useRef(false);
  const delayRef = useRef(STATUS_POLL_MS);
  // Set by refreshStatus/refreshTimeline when a request in the current polling
  // cycle fails. They swallow their own errors, so allSettled alone cannot tell
  // a failed cycle from a successful one.
  const cycleFailedRef = useRef(false);
  // A polling cycle is exclusive: a tick that fires while another cycle is still
  // awaiting its requests defers instead of starting a second cycle.
  const cycleInFlightRef = useRef(false);
  const refreshTimelineRef = useRef<() => void>(() => {});

  const refreshStatus = useCallback(async () => {
    const run = runRef.current;
    try {
      const body = await getDiagnosis(diagnosisId);
      if (run !== runRef.current) return;
      delayRef.current = STATUS_POLL_MS;
      statusRef.current = body.status;
      if (isTerminal(body.status) && terminalDeadlineRef.current === null) {
        // Keep consuming the trace for a bounded window after completion so a
        // late completion event is not missed.
        terminalDeadlineRef.current = Date.now() + TERMINAL_WAIT_MS;
      }
      setDiagnosis(body);
      setError(null);
      if (isTerminal(body.status)) {
        refreshTimelineRef.current();
      }
    } catch (err) {
      if (run !== runRef.current) return;
      cycleFailedRef.current = true;
      delayRef.current = Math.min(delayRef.current * 2, MAX_BACKOFF_MS);
      setError((err as Error).message);
    }
  }, [diagnosisId]);

  const schedulePendingRetry = useCallback(() => {
    if (pendingSinceRef.current === null) pendingSinceRef.current = Date.now();
    if (Date.now() - pendingSinceRef.current > PENDING_TIMEOUT_MS) {
      setTimelineState('unavailable');
      return;
    }
    if (pendingTimerRef.current) return;
    pendingTimerRef.current = setTimeout(() => {
      pendingTimerRef.current = null;
      refreshTimelineRef.current();
    }, PENDING_RETRY_MS);
  }, []);

  const refreshTimeline = useCallback(async () => {
    const run = runRef.current;
    // Serialize timeline reads. Concurrent callers coalesce onto the *same*
    // real in-flight promise: they must await it so a polling cycle cannot
    // settle while the read is outstanding. No rerun is queued — the next
    // scheduled poll fetches anything appended since, so a queued rerun can
    // never outlive the cycle that triggered it.
    const pendingRead = timelineInFlightRef.current
      ? timelineInFlightPromiseRef.current
      : null;
    if (pendingRead) {
      await pendingRead;
      return;
    }
    let release!: () => void;
    const inFlight = new Promise<void>(resolve => {
      release = resolve;
    });
    timelineInFlightPromiseRef.current = inFlight;
    timelineInFlightRef.current = true;
    try {
      for (let pages = 0; pages < MAX_PAGES_PER_DRAIN; pages += 1) {
        const before = offsetRef.current;
        const page = await getTimeline(diagnosisId, offsetRef.current, 100);
        if (run !== runRef.current) return;
        if (page.available !== 'available') {
          setTimelineState(page.available);
          if (page.available === 'pending') schedulePendingRetry();
          return;
        }
        pendingSinceRef.current = null;
        offsetRef.current = page.next_after;
        if (page.gap) setTimelineGap(true);
        const fresh = page.items.filter(item => {
          if (seenIdsRef.current.has(item.id)) return false;
          seenIdsRef.current.add(item.id);
          return true;
        });
        if (fresh.some(item => TERMINAL_EVENT_KINDS.includes(item.kind))) {
          finalEventSeenRef.current = true;
          setTimelineIncomplete(false);
        }
        if (fresh.length) setTimeline(prev => [...prev, ...fresh]);
        setTimelineState('available');
        // No progress (e.g. a half-written tail line): stop this pass and wait
        // for the next poll instead of hammering the same offset.
        if (!page.has_more || page.next_after <= before) return;
      }
    } catch (err) {
      if (run !== runRef.current) return;
      // A timeline failure slows this polling cycle's backoff, so the next
      // scheduled cycle already uses the increased delay.
      cycleFailedRef.current = true;
      delayRef.current = Math.min(delayRef.current * 2, MAX_BACKOFF_MS);
      setError((err as Error).message);
      setTimelineState('unavailable');
    } finally {
      timelineInFlightRef.current = false;
      timelineInFlightPromiseRef.current = null;
      // Release coalesced waiters only after the real read completed; nothing is
      // queued behind this promise.
      release();
    }
    await inFlight;
  }, [diagnosisId, schedulePendingRetry]);

  useEffect(() => {
    refreshTimelineRef.current = () => {
      void refreshTimeline();
    };
  }, [refreshTimeline]);

  // Reset per diagnosis and kick off the first loads.
  useEffect(() => {
    runRef.current += 1;
    offsetRef.current = 0;
    seenIdsRef.current = new Set();
    timelineInFlightRef.current = false;
    finalEventSeenRef.current = false;
    terminalDeadlineRef.current = null;
    statusRef.current = undefined;
    pendingSinceRef.current = null;
    if (pendingTimerRef.current) {
      clearTimeout(pendingTimerRef.current);
      pendingTimerRef.current = null;
    }
    readAttemptedRef.current = false;
    delayRef.current = STATUS_POLL_MS;
    setDiagnosis(null);
    setError(null);
    setTimeline([]);
    setTimelineGap(false);
    setTimelineState('pending');
    setReadDone(false);
    setReadError(null);
    setTimelineIncomplete(false);
    void refreshStatus();
    void refreshTimeline();
    return () => {
      if (pendingTimerRef.current) {
        clearTimeout(pendingTimerRef.current);
        pendingTimerRef.current = null;
      }
    };
  }, [diagnosisId, refreshStatus, refreshTimeline]);

  // Poll status while running; after completion keep draining the trace for a
  // bounded window until the terminal event appears.
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    let stopped = false;
    // Recursive scheduling re-reads delayRef each tick; a fixed interval would
    // keep hammering a failing service at the base cadence.
    const schedule = () => {
      if (stopped) return;
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        void tick();
      }, Math.min(delayRef.current, MAX_BACKOFF_MS));
    };
    const tick = async () => {
      if (stopped) return;
      if (cycleInFlightRef.current) {
        // Another cycle is still awaiting its requests: never start a second.
        schedule();
        return;
      }
      if (document.visibilityState !== 'visible') {
        schedule();
        return;
      }
      cycleInFlightRef.current = true;
      try {
        const status = statusRef.current ?? diagnosis?.status;
        if (!isTerminal(status)) {
          // Await BOTH refreshes before scheduling the next cycle: a slow request
          // must not overlap with the next poll. The cycle's outcome (not the
          // individual request order) decides the next delay, so a timeline
          // failure cannot be masked by a successful status call.
          const baseDelay = delayRef.current;
          cycleFailedRef.current = false;
          await Promise.allSettled([refreshStatus(), refreshTimeline()]);
          if (cycleFailedRef.current) {
            // Any failure in the cycle (status or timeline) backs off the next one.
            delayRef.current = Math.min(baseDelay * 2, MAX_BACKOFF_MS);
          } else {
            delayRef.current = STATUS_POLL_MS;
          }
        } else {
          const waitingForFinal =
            !finalEventSeenRef.current &&
            terminalDeadlineRef.current !== null &&
            Date.now() < terminalDeadlineRef.current;
          if (waitingForFinal) {
            await refreshTimeline();
          } else if (!finalEventSeenRef.current && terminalDeadlineRef.current !== null) {
            // Bounded wait expired without the completion event: keep what we have
            // and tell the user the timeline may be incomplete.
            setTimelineIncomplete(true);
          }
        }
      } finally {
        cycleInFlightRef.current = false;
      }
      schedule();
    };
    schedule();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [diagnosis, refreshStatus, refreshTimeline]);

  // Mark read only after a terminal state is rendered and the tab is visible.
  useEffect(() => {
    if (!diagnosis || !isTerminal(diagnosis.status) || readDone) return;
    if (document.visibilityState !== 'visible' || readAttemptedRef.current) return;
    readAttemptedRef.current = true;
    const run = runRef.current;
    markRead(diagnosis.diagnosis_id, getViewerId())
      .then(() => {
        if (run !== runRef.current) return;
        setReadDone(true);
        bumpReadRevision();
        refreshDiagnosisNotifications();
      })
      .catch(err => {
        if (run !== runRef.current) return;
        readAttemptedRef.current = false;
        setReadError((err as Error).message);
      });
  }, [diagnosis, readDone]);

  const retryTimeline = useCallback(() => {
    terminalDeadlineRef.current = Date.now() + TERMINAL_WAIT_MS;
    setTimelineIncomplete(false);
    refreshTimelineRef.current();
  }, []);

  if (error && !diagnosis) {
    return (
      <SectionBox title="诊断详情">
        <Alert severity="error">加载失败：{error}</Alert>
      </SectionBox>
    );
  }
  if (!diagnosis) {
    return (
      <SectionBox title="诊断详情">
        <Stack direction="row" spacing={1} alignItems="center">
          <CircularProgress size={18} />
          <Typography variant="body2">加载中…</Typography>
        </Stack>
      </SectionBox>
    );
  }

  const result = diagnosis.result;
  return (
    <SectionBox title={`诊断详情 ${diagnosis.diagnosis_id}`} backLink={centerUrl()}>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }} flexWrap="wrap">
        <Chip
          size="small"
          label={diagnosis.status}
          color={
            diagnosis.status === 'completed'
              ? 'success'
              : diagnosis.status === 'failed'
              ? 'error'
              : 'warning'
          }
        />
        <Chip size="small" variant="outlined" label={diagnosis.trigger} />
        {readDone && <Chip size="small" color="info" label="已读" />}
      </Stack>
      <Typography variant="body2" color="text.secondary">
        {[diagnosis.resource?.kind, diagnosis.resource?.namespace, diagnosis.resource?.name]
          .filter(Boolean)
          .join(' / ')}
        {` · ${new Date(diagnosis.created_at).toLocaleString()}`}
      </Typography>

      {readError && (
        <Alert severity="warning" sx={{ mt: 1 }}>
          标记已读失败（不影响诊断）：{readError}
        </Alert>
      )}
      {diagnosis.status === 'failed' && (
        <Alert severity="error" sx={{ mt: 1 }}>
          诊断失败：{diagnosis.error ?? '未知错误'}
        </Alert>
      )}

      <Divider sx={{ my: 1.5 }} />
      <Typography variant="subtitle1" fontWeight={600}>
        结论
      </Typography>
      {result && diagnosis.status !== 'failed' ? (
        <>
          <Typography variant="body2" sx={{ mb: 0.5 }}>
            症状：{result.symptom || '（无）'}
          </Typography>
          {result.root_cause ? (
            <Typography variant="body2">
              根因：{result.root_cause}
              {result.root_cause_code ? ` (${result.root_cause_code})` : ''}
            </Typography>
          ) : (
            <Alert severity="warning" sx={{ my: 0.5 }}>
              证据不足，无法确定唯一根因。
              {result.missing_evidence?.length
                ? ` 缺少证据：${result.missing_evidence.join('；')}`
                : ''}
            </Alert>
          )}
          <Typography variant="body2">置信度：{result.confidence ?? 'unknown'}</Typography>
        </>
      ) : diagnosis.status === 'failed' ? (
        <Typography variant="body2" color="text.secondary">
          诊断未完成，无结论；请查看上方错误信息与下方执行时间线。
        </Typography>
      ) : (
        <Typography variant="body2" color="text.secondary">
          （暂无结论）
        </Typography>
      )}

      <Divider sx={{ my: 1.5 }} />
      <Typography variant="subtitle1" fontWeight={600}>
        关键证据
      </Typography>
      <Typography variant="caption" color="text.secondary" display="block" sx={{ mb: 0.5 }}>
        {isTerminal(diagnosis.status)
          ? '来自最终诊断结论。'
          : '调查进行中；结论产出前请以下方执行时间线为准。'}
      </Typography>
      <List dense disablePadding>
        {(result?.evidence ?? []).map((ev, idx) => (
          <ListItem key={`${ev.source}-${idx}`} disableGutters>
            <ListItemText primary={ev.summary} secondary={`来源: ${ev.source}`} />
          </ListItem>
        ))}
        {!result?.evidence?.length && (
          <ListItemText
            primary={
              diagnosis.status === 'failed'
                ? '（诊断失败，无证据）'
                : result
                ? '（无）'
                : '（调查进行中，暂无结论）'
            }
          />
        )}
      </List>

      <Divider sx={{ my: 1.5 }} />
      <Typography variant="subtitle1" fontWeight={600}>
        修复建议
      </Typography>
      <List dense disablePadding>
        {(result?.recommendations ?? []).map((rec, idx) => (
          <ListItem key={idx} disableGutters>
            <ListItemText primary={rec} />
          </ListItem>
        ))}
        {!result?.recommendations?.length && <ListItemText primary="（无）" />}
      </List>

      <Divider sx={{ my: 1.5 }} />
      <Typography variant="subtitle1" fontWeight={600}>
        调查过程
      </Typography>
      <Typography variant="caption" color="text.secondary" display="block" sx={{ mb: 0.5 }}>
        仅列出已成功返回结果的工具调用；失败的调用请见下方执行时间线。
      </Typography>
      {result?.investigation_steps?.length ? (
        <InvestigationSteps steps={result.investigation_steps} />
      ) : (
        <List dense disablePadding>
          <ListItemText primary="（无）" />
        </List>
      )}

      <Divider sx={{ my: 1.5 }} />
      <Typography variant="subtitle1" fontWeight={600} sx={{ mb: 0.5 }}>
        执行时间线
      </Typography>
      {timelineState === 'pending' && (
        <Typography variant="body2" color="text.secondary">
          Trace 生成中…
        </Typography>
      )}
      {timelineState === 'unavailable' && (
        <Typography variant="body2" color="text.secondary">
          Trace 不可用（未启用或文件缺失）。
        </Typography>
      )}
      {timelineGap && (
        <Alert severity="info" sx={{ mb: 0.5 }}>
          部分事件被截断或无法解析，已跳过。
        </Alert>
      )}
      {timelineIncomplete && (
        <Alert
          severity="warning"
          sx={{ mb: 0.5 }}
          action={
            <Button color="inherit" size="small" onClick={retryTimeline}>
              刷新
            </Button>
          }
        >
          时间线可能不完整：等待完成事件超时，已保留现有步骤。
        </Alert>
      )}
      <List dense disablePadding>
        {timeline.map(item => (
          <ListItem key={item.id} disableGutters>
            <ListItemText
              primary={`${item.title} · ${item.status}`}
              secondary={
                item.duration_ms !== null && item.duration_ms !== undefined
                  ? `${Math.round(item.duration_ms)} ms`
                  : undefined
              }
            />
          </ListItem>
        ))}
        {timelineState === 'available' && timeline.length === 0 && (
          <ListItemText primary="（暂无事件）" />
        )}
      </List>

      {diagnosis.alert && (
        <>
          <Divider sx={{ my: 1.5 }} />
          <Typography variant="subtitle1" fontWeight={600}>
            告警
          </Typography>
          <Typography variant="body2">
            {diagnosis.alert.alertname ?? '告警'} · {diagnosis.alert.state} ·{' '}
            {diagnosis.alert.fingerprint}
          </Typography>
          <Typography variant="body2" color="text.secondary">
            最近告警：{diagnosis.alert.latest_alert_at ?? '-'}
            {diagnosis.alert.resolved_at ? ` · 已恢复：${diagnosis.alert.resolved_at}` : ''}
          </Typography>
        </>
      )}

      <Box sx={{ mt: 2 }}>
        <MuiLink component={Link} to={centerUrl()}>
          返回诊断中心
        </MuiLink>
      </Box>
    </SectionBox>
  );
}
