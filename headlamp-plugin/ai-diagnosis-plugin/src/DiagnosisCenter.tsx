/** Diagnosis Center list page: sessions + unresolved alerts (design §4). */

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
  MenuItem,
  Stack,
  TablePagination,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useHistory, useLocation } from 'react-router-dom';
import {
  listNamespaces,
  listSessions,
  listUnresolvedAlerts,
  markAllNotificationsRead,
} from './api';
import { detailUrl } from './routes';
import { getTablesRowsPerPage, setTablesRowsPerPage } from './tablesRowsPerPage';
import type { CenterSession, DiagnosisSummary, SessionFilters, UnresolvedAlert } from './types';
import { useDiagnosisNotifications } from './useDiagnosisNotifications';
import { bumpReadRevision, getViewerId } from './viewer';

const POLL_MS = 5000;
const MAX_BACKOFF_MS = 30000;
// Match Headlamp's table pagination (default options + shared persistence).
const PER_PAGE_OPTIONS = [15, 25, 50];
const DEFAULT_PER_PAGE = PER_PAGE_OPTIONS[0];

function statusColor(status: string): 'success' | 'error' | 'warning' | 'default' {
  switch (status) {
    case 'completed':
      return 'success';
    case 'failed':
      return 'error';
    case 'investigating':
    case 'queued':
      return 'warning';
    default:
      return 'default';
  }
}

function resourceLabel(session: CenterSession): string {
  const { kind, namespace, name } = session.resource ?? {};
  return [kind, namespace, name].filter(Boolean).join(' / ');
}

/** Root cause: description first (2 lines max + tooltip), code as a secondary tag. */
function RootCauseInline({ summary }: { summary?: DiagnosisSummary | null }) {
  if (!summary) return null;
  const text = (summary.root_cause ?? '').trim();
  const code = summary.root_cause_code;
  if (!text && summary.insufficient_evidence) {
    return (
      <Typography component="span" variant="body2" color="text.secondary">
        {' · 证据不足，未确认根因'}
      </Typography>
    );
  }
  if (!text) {
    return code ? <Chip size="small" variant="outlined" label={code} sx={{ ml: 1 }} /> : null;
  }
  return (
    <>
      {' · '}
      <Tooltip
        title={text}
        placement="top"
        slotProps={{ tooltip: { sx: { maxWidth: 420, whiteSpace: 'normal' } } }}
      >
        <Box
          component="span"
          tabIndex={0}
          sx={{
            display: '-webkit-box',
            WebkitLineClamp: 2,
            WebkitBoxOrient: 'vertical',
            overflow: 'hidden',
            whiteSpace: 'normal',
            maxWidth: '100%',
          }}
        >
          {text}
        </Box>
      </Tooltip>
      {code && <Chip size="small" variant="outlined" label={code} sx={{ ml: 1 }} />}
    </>
  );
}

export default function DiagnosisCenter() {
  const history = useHistory();
  const location = useLocation();
  const notifications = useDiagnosisNotifications();

  const params = useMemo(() => new URLSearchParams(location.search), [location.search]);
  const filters: SessionFilters = useMemo(
    () => ({
      status: params.get('status') ?? undefined,
      trigger: params.get('trigger') ?? undefined,
      namespace: params.get('namespace') ?? undefined,
      name: params.get('name') ?? undefined,
      uid: params.get('uid') ?? undefined,
      after: params.get('after') ?? undefined,
      unread: params.get('unread') === 'true' ? true : undefined,
      page: Number(params.get('page') ?? '1') || 1,
      perPage: Number(params.get('perPage') ?? '0') || getTablesRowsPerPage(DEFAULT_PER_PAGE),
    }),
    [params]
  );

  // Controlled inputs stay in sync with the URL (refresh / back / forward).
  const [draft, setDraft] = useState({ name: '' });
  useEffect(() => {
    setDraft({ name: filters.name ?? '' });
  }, [filters.name]);

  const [namespaces, setNamespaces] = useState<string[]>([]);
  useEffect(() => {
    let cancelled = false;
    const fetchNamespaces = async () => {
      try {
        const page = await listNamespaces();
        if (!cancelled) setNamespaces(page.items);
      } catch {
        // A missing dropdown must not block the list.
      }
    };
    void fetchNamespaces();
    const timer = setInterval(fetchNamespaces, 60000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  const [items, setItems] = useState<CenterSession[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [unresolved, setUnresolved] = useState<UnresolvedAlert[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [markingAll, setMarkingAll] = useState(false);
  const [markAllError, setMarkAllError] = useState<string | null>(null);
  // Keyset pagination: cursors per page number ('' = first page).
  const cursorByPage = useRef<Record<number, string>>({ 1: '' });
  // Ref keeps callbacks/effects independent of the (possibly unstable) history object.
  const historyRef = useRef(history);
  historyRef.current = history;
  const delay = useRef(POLL_MS);
  const generation = useRef(0);
  const page = Math.max(1, filters.page ?? 1);
  const perPage = filters.perPage ?? DEFAULT_PER_PAGE;

  const applyFilter = useCallback(
    (key: 'status' | 'trigger' | 'namespace' | 'name' | 'uid', value: string) => {
      const next = new URLSearchParams(location.search);
      const trimmed = value.trim();
      if (trimmed) next.set(key, trimmed);
      else next.delete(key);
      next.delete('after'); // any condition change restarts from page one
      historyRef.current.replace({ search: next.toString() });
    },
    [location.search]
  );

  const clearFilters = useCallback(() => {
    historyRef.current.replace({ search: '' });
  }, []);

  const toggleUnreadOnly = useCallback(() => {
    const next = new URLSearchParams(location.search);
    if (next.get('unread') === 'true') next.delete('unread');
    else next.set('unread', 'true');
    next.delete('after');
    next.delete('page');
    historyRef.current.replace({ search: next.toString() });
  }, [location.search]);

  const load = useCallback(
    async (cursor?: string, append = false) => {
      const gen = ++generation.current;
      try {
        const [page, unresolvedPage] = await Promise.all([
          listSessions({ ...filters, page: undefined, perPage: undefined }, perPage, getViewerId()),
          append ? Promise.resolve(null) : listUnresolvedAlerts(20),
        ]);
        if (gen !== generation.current) return;
        delay.current = POLL_MS;
        setError(null);
        setLoading(false);
        setItems(prev => (append ? [...prev, ...page.items] : page.items));
        setNextCursor(page.next_cursor);
        if (unresolvedPage) setUnresolved(unresolvedPage.items);
      } catch (err) {
        if (gen !== generation.current) return;
        delay.current = Math.min(delay.current * 2, MAX_BACKOFF_MS);
        setError((err as Error).message);
        setLoading(false);
      }
    },
    [filters]
  );

  const hasMorePages = page > 1;
  useEffect(() => {
    let cancelled = false;
    const ensureCursorsThenLoad = async () => {
      // After a refresh/back the in-memory cursor stack is gone: replay keyset
      // pages from the start (bounded) so prev/next still work.
      if (page > 1 && cursorByPage.current[page] === undefined) {
        try {
          let cursor = '';
          cursorByPage.current = { 1: '' };
          for (let p = 2; p <= page; p += 1) {
            const data = await listSessions(
              { ...filters, page: undefined, perPage: undefined, after: cursor || undefined },
              perPage,
              getViewerId()
            );
            if (cancelled) return;
            cursor = data.next_cursor ?? '';
            if (!cursor) {
              // The requested page no longer exists; fall back to page one.
              cursorByPage.current = { 1: '' };
              const next = new URLSearchParams(location.search);
              next.delete('page');
              next.delete('after');
              historyRef.current.replace({ search: next.toString() });
              return;
            }
            cursorByPage.current[p] = cursor;
          }
        } catch {
          cursorByPage.current = { 1: '' };
        }
      }
      setItems([]);
      setNextCursor(null);
      void load(filters.after);
    };
    void ensureCursorsThenLoad();
    return () => {
      cancelled = true;
    };
  }, [filters, location.search, page, load]);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    let stopped = false;
    // Recursive scheduling reads delay.current on every tick, so a failure
    // actually slows the next poll (a fixed interval would ignore the backoff).
    const schedule = () => {
      if (stopped) return;
      if (timer) clearTimeout(timer);
      timer = setTimeout(tick, Math.min(delay.current, MAX_BACKOFF_MS));
    };
    const tick = () => {
      // Only refresh the head page so the user's current page is not replaced.
      if (document.visibilityState === 'visible' && !hasMorePages) {
        void load().finally(schedule);
      } else {
        schedule();
      }
    };
    const onVisibility = () => {
      if (document.visibilityState === 'visible' && !hasMorePages) void load();
    };
    schedule();
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [load, hasMorePages]);

  const goNext = useCallback(() => {
    if (!nextCursor) return;
    cursorByPage.current[page + 1] = nextCursor;
    const next = new URLSearchParams(location.search);
    next.set('after', nextCursor);
    next.set('page', String(page + 1));
    historyRef.current.replace({ search: next.toString() });
  }, [location.search, nextCursor, page]);

  const goPrev = useCallback(() => {
    if (page <= 1) return;
    const prevCursor = cursorByPage.current[page - 1] ?? '';
    const next = new URLSearchParams(location.search);
    if (prevCursor) next.set('after', prevCursor);
    else next.delete('after');
    if (page - 1 <= 1) next.delete('page');
    else next.set('page', String(page - 1));
    historyRef.current.replace({ search: next.toString() });
  }, [location.search, page]);

  const goToPage = useCallback(
    (target: number) => {
      if (target === page + 1) goNext();
      else if (target === page - 1) goPrev();
      // Keyset pagination only supports one-page steps (no jump/last).
    },
    [page, goNext, goPrev]
  );

  const changePerPage = useCallback(
    (value: number) => {
      setTablesRowsPerPage(value);
      const next = new URLSearchParams(location.search);
      next.set('perPage', String(value));
      next.delete('page');
      next.delete('after');
      historyRef.current.replace({ search: next.toString() });
    },
    [location.search]
  );

  const markAllRead = useCallback(async () => {
    setMarkAllError(null);
    setMarkingAll(true);
    try {
      await markAllNotificationsRead(getViewerId());
      bumpReadRevision(); // other tabs refetch
      notifications.refresh();
      await load(filters.after); // refresh unread flags in the list
    } catch (err) {
      setMarkAllError((err as Error).message);
    } finally {
      setMarkingAll(false);
    }
  }, [filters.after, load, notifications]);

  const headerActions = [
    <Stack key="unread-actions" direction="row" spacing={1} alignItems="center">
      <Tooltip title="只显示尚未读过的自动诊断；已读/人工/评测诊断会被隐藏。待处理告警在下方独立区块。">
        <Chip
          size="small"
          color={filters.unread ? 'primary' : 'default'}
          variant={filters.unread ? 'filled' : 'outlined'}
          label="只看未读"
          onClick={toggleUnreadOnly}
        />
      </Tooltip>
      <Typography variant="body2" color="text.secondary">
        {`未读诊断 ${notifications.unreadCount} 条`}
      </Typography>
      <Chip
        size="small"
        variant="outlined"
        color={notifications.pendingAlertCount > 0 ? 'warning' : 'default'}
        label={`待处理告警 ${notifications.pendingAlertCount} 条`}
      />
      <Tooltip
        title="作用于当前浏览器、当前 Agent 的全部未读自动诊断（包含当前筛选与分页之外）；处理期间新完成的诊断保持未读。"
        slotProps={{ tooltip: { sx: { maxWidth: 360, whiteSpace: 'normal' } } }}
      >
        <span>
          <Button
            size="small"
            variant="outlined"
            disabled={notifications.unreadCount === 0 || markingAll}
            onClick={markAllRead}
          >
            {markingAll ? '处理中…' : '全部标为已读'}
          </Button>
        </span>
      </Tooltip>
      {markAllError && (
        <Tooltip title={markAllError}>
          <Typography variant="caption" color="error" role="alert">
            标记失败，请重试
          </Typography>
        </Tooltip>
      )}
    </Stack>,
  ];

  const textField = (key: 'name', label: string, helper: string) => (
    <TextField
      size="small"
      label={label}
      helperText={helper}
      value={draft[key]}
      onChange={e => setDraft(prev => ({ ...prev, [key]: e.target.value }))}
      onKeyDown={e => {
        if (e.key === 'Enter') applyFilter(key, (e.target as HTMLInputElement).value);
      }}
      onBlur={e => {
        if (e.target.value.trim() !== (filters[key] ?? '')) applyFilter(key, e.target.value);
      }}
      sx={{ minWidth: 220 }}
    />
  );

  return (
    <SectionBox title="智能诊断中心" headerProps={{ actions: headerActions }}>
      {notifications.error && (
        <Alert severity="warning" sx={{ mb: 1 }}>
          未读状态获取失败（不影响诊断）：{notifications.error}
        </Alert>
      )}
      {error && (
        <Alert
          severity="error"
          sx={{ mb: 1 }}
          action={
            <Button color="inherit" size="small" onClick={() => load(filters.after)}>
              重试
            </Button>
          }
        >
          加载诊断列表失败：{error}
        </Alert>
      )}

      <Stack direction="row" spacing={1} sx={{ mb: 2 }} flexWrap="wrap" useFlexGap>
        <TextField
          select
          size="small"
          label="状态"
          value={filters.status ?? ''}
          onChange={e => applyFilter('status', e.target.value)}
          sx={{ minWidth: 140 }}
        >
          <MenuItem value="">全部</MenuItem>
          {['queued', 'investigating', 'completed', 'failed'].map(value => (
            <MenuItem key={value} value={value}>
              {value}
            </MenuItem>
          ))}
        </TextField>
        <TextField
          select
          size="small"
          label="触发方式"
          value={filters.trigger ?? ''}
          onChange={e => applyFilter('trigger', e.target.value)}
          sx={{ minWidth: 140 }}
        >
          <MenuItem value="">全部</MenuItem>
          <MenuItem value="manual">manual</MenuItem>
          <MenuItem value="alert">alert</MenuItem>
        </TextField>
        <TextField
          select
          size="small"
          label="Namespace"
          value={filters.namespace ?? ''}
          onChange={e => applyFilter('namespace', e.target.value)}
          sx={{ minWidth: 200 }}
        >
          <MenuItem value="">全部</MenuItem>
          {namespaces.map(ns => (
            <MenuItem key={ns} value={ns}>
              {ns}
            </MenuItem>
          ))}
        </TextField>
        {textField('name', '资源名称', '模糊匹配（子串）')}
        <Button size="small" onClick={clearFilters}>
          清空筛选
        </Button>
      </Stack>

      {loading && (
        <Stack direction="row" spacing={1} alignItems="center">
          <CircularProgress size={18} />
          <Typography variant="body2">加载中…</Typography>
        </Stack>
      )}

      {!loading && items.length === 0 && (
        <Typography variant="body2" color="text.secondary">
          {filters.unread
            ? '暂无未读自动诊断；未解析目标的告警见下方独立区块。'
            : '暂无诊断记录。'}
        </Typography>
      )}

      <List dense disablePadding>
        {items.map(item => (
          <ListItem key={item.diagnosis_id} divider alignItems="flex-start">
            <ListItemText
              primary={
                <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap">
                  <Chip size="small" label={item.status} color={statusColor(item.status)} />
                  <Chip size="small" variant="outlined" label={item.trigger} />
                  {item.unread && <Chip size="small" color="info" label="未读" />}
                  <MuiLink component={Link} to={detailUrl(item.diagnosis_id)}>
                    {resourceLabel(item)}
                  </MuiLink>
                </Stack>
              }
              secondary={
                <Box component="span" sx={{ display: 'block' }}>
                  {item.alert
                    ? `告警 ${item.alert.alertname ?? ''} (${item.alert.state})`
                    : '人工诊断'}
                  <RootCauseInline summary={item.summary} />
                  {` · ${new Date(item.created_at).toLocaleString()}`}
                </Box>
              }
            />
          </ListItem>
        ))}
      </List>

      <TablePagination
        component="div"
        count={nextCursor ? -1 : (page - 1) * perPage + items.length}
        page={page - 1}
        onPageChange={(_event, newPage) => goToPage(newPage + 1)}
        rowsPerPage={perPage}
        onRowsPerPageChange={event => changePerPage(Number(event.target.value))}
        rowsPerPageOptions={PER_PAGE_OPTIONS}
        showFirstButton={false}
        showLastButton={false}
        SelectProps={{ inputProps: { 'aria-label': 'rows per page' } }}
        labelRowsPerPage="每页行数："
        labelDisplayedRows={({ from, to }) => `${from}-${to}`}
      />

      {filters.unread && (
        <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 1 }}>
          未读列表只包含自动诊断；未解析目标的告警不在此列表，见下方「未解析目标的告警」。
        </Typography>
      )}

      <Divider sx={{ my: 2 }} />
      <Typography variant="subtitle1" fontWeight={600}>
        未解析目标的告警
      </Typography>
      <Typography variant="caption" color="text.secondary">
        无法唯一映射到资源，不会自动生成诊断。
      </Typography>
      {unresolved.length === 0 ? (
        <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
          暂无。
        </Typography>
      ) : (
        <List dense disablePadding>
          {unresolved.map(alert => (
            <ListItem key={alert.id} disableGutters>
              <ListItemText
                primary={`${alert.alertname ?? '告警'} · ${alert.state}`}
                secondary={`${alert.starts_at ?? ''} · ${
                  alert.target
                    ? `${alert.target.kind ?? ''}/${alert.target.namespace ?? ''}/${
                        alert.target.name ?? ''
                      }`
                    : '无目标信息'
                }`}
              />
            </ListItem>
          ))}
        </List>
      )}
    </SectionBox>
  );
}
