/** Center: filter URL state, backend passthrough, root-cause rendering. */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

let currentSearch = '';
const replaceSpy = vi.fn(({ search }: { search: string }) => {
  currentSearch = search ? `?${search}` : '';
});

vi.mock('react-router-dom', () => ({
  useLocation: () => ({ search: currentSearch }),
  useHistory: () => ({ replace: replaceSpy }),
  Link: ({ to, children }: { to: string; children?: React.ReactNode }) =>
    React.createElement('a', { href: to }, children),
}));

vi.mock('@kinvolk/headlamp-plugin/lib', () => ({
  Router: { createRouteURL: (name: string) => `/c/test/${name}` },
}));

vi.mock('@kinvolk/headlamp-plugin/lib/CommonComponents', () => ({
  SectionBox: ({
    title,
    children,
    headerProps,
  }: {
    title?: React.ReactNode;
    children?: React.ReactNode;
    headerProps?: { actions?: React.ReactNode[] };
  }) =>
    React.createElement(
      'div',
      null,
      React.createElement('div', null, title),
      headerProps?.actions ?? null,
      children
    ),
}));

vi.mock('./api', () => ({
  listSessions: vi.fn(),
  listUnresolvedAlerts: vi.fn(),
  listNamespaces: vi.fn(),
  markAllNotificationsRead: vi.fn(),
}));

vi.mock('./useDiagnosisNotifications', () => ({
  useDiagnosisNotifications: vi.fn(),
  refreshDiagnosisNotifications: vi.fn(),
}));

vi.mock('./viewer', () => ({ getViewerId: () => 'viewer-1', bumpReadRevision: vi.fn() }));

vi.mock('./tablesRowsPerPage', () => ({
  getTablesRowsPerPage: vi.fn(() => 15),
  setTablesRowsPerPage: vi.fn(),
}));

import {
  listNamespaces,
  listSessions,
  listUnresolvedAlerts,
  markAllNotificationsRead,
} from './api';
import DiagnosisCenter from './DiagnosisCenter';
import { setTablesRowsPerPage } from './tablesRowsPerPage';
import type { SessionFilters } from './types';
import { useDiagnosisNotifications } from './useDiagnosisNotifications';

function mockNotifications(unreadCount: number, pendingAlertCount = 0) {
  const refresh = vi.fn();
  vi.mocked(useDiagnosisNotifications).mockReturnValue({
    unreadCount,
    pendingAlertCount,
    liveMessage: `${unreadCount} 条未读诊断`,
    ready: true,
    error: null,
    items: [],
    refresh,
  });
  return refresh;
}

function session(overrides: Record<string, unknown> = {}) {
  return {
    diagnosis_id: 'diag_1',
    trigger: 'alert',
    status: 'completed',
    resource: { kind: 'Pod', namespace: 'ns', name: 'p' },
    alert: { fingerprint: 'fp', alertname: 'PodCrashLooping', state: 'closed' },
    summary: null,
    created_at: '2026-09-17T00:00:00Z',
    updated_at: '2026-09-17T00:00:00Z',
    unread: false,
    ...overrides,
  };
}

beforeEach(() => {
  currentSearch = '';
  vi.clearAllMocks();
  mockNotifications(0);
  vi.mocked(listUnresolvedAlerts).mockResolvedValue({ items: [], next_cursor: null });
  vi.mocked(listNamespaces).mockResolvedValue({ items: ['team-a', 'team-b'] });
  vi.mocked(listSessions).mockResolvedValue({ items: [session()], next_cursor: null });
});

describe('DiagnosisCenter', () => {
  it('marks all unread as read from the header and refreshes list + badge', async () => {
    const refresh = mockNotifications(5);
    vi.mocked(markAllNotificationsRead).mockResolvedValue({ read: 5 });
    const callsBefore = vi.mocked(listSessions).mock.calls.length;

    render(<DiagnosisCenter />);
    await waitFor(() => expect(listSessions).toHaveBeenCalled());
    expect(screen.getByText(/未读诊断 5 条/)).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '全部标为已读' }));
    await waitFor(() => expect(markAllNotificationsRead).toHaveBeenCalledWith('viewer-1'));
    expect(refresh).toHaveBeenCalled();
    await waitFor(() =>
      expect(vi.mocked(listSessions).mock.calls.length).toBeGreaterThan(callsBefore)
    );
    expect(screen.queryByText(/标记失败/)).toBeNull();
  });

  it('disables the button when there are no unread diagnoses, even with pending alerts', async () => {
    mockNotifications(0, 3);
    render(<DiagnosisCenter />);
    await waitFor(() => expect(listSessions).toHaveBeenCalled());
    const button = screen.getByRole('button', { name: '全部标为已读' }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    // Pending alerts are surfaced separately from the unread diagnosis count.
    expect(screen.getByText('待处理告警 3 条')).toBeTruthy();
  });

  it('keeps state and shows a retry hint when marking fails', async () => {
    mockNotifications(3);
    vi.mocked(markAllNotificationsRead).mockRejectedValue(new Error('boom'));
    render(<DiagnosisCenter />);
    await waitFor(() => expect(listSessions).toHaveBeenCalled());

    fireEvent.click(screen.getByRole('button', { name: '全部标为已读' }));
    expect(await screen.findByText(/标记失败，请重试/)).toBeTruthy();
    const button = screen.getByRole('button', { name: '全部标为已读' }) as HTMLButtonElement;
    expect(button.disabled).toBe(false);
  });
  it('filters by namespace (dropdown) and resource name (fuzzy), dropping the cursor', async () => {
    const { rerender } = render(<DiagnosisCenter />);
    await waitFor(() => expect(listSessions).toHaveBeenCalled());

    // Namespace comes from the backend list and is chosen from a dropdown.
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Namespace' }));
    fireEvent.click(await screen.findByRole('option', { name: 'team-a' }));

    const afterNamespace = replaceSpy.mock.calls.at(-1)?.[0].search as string;
    expect(afterNamespace).toContain('namespace=team-a');
    expect(afterNamespace).not.toContain('after');

    rerender(<DiagnosisCenter />);
    await waitFor(() => {
      const last = vi.mocked(listSessions).mock.calls.at(-1)?.[0] as Record<string, unknown>;
      expect(last).toMatchObject({ namespace: 'team-a' });
    });

    // Resource name is a fuzzy substring filter.
    const name = screen.getByLabelText(/资源名称/);
    fireEvent.change(name, { target: { value: 'payment' } });
    fireEvent.keyDown(name, { key: 'Enter' });

    const combined = replaceSpy.mock.calls.at(-1)?.[0].search as string;
    expect(combined).toContain('namespace=team-a');
    expect(combined).toContain('name=payment');
    expect(combined).not.toContain('after');
    expect(combined).not.toContain('uid=');
  });

  it('has no UID filter control', async () => {
    render(<DiagnosisCenter />);
    await waitFor(() => expect(listSessions).toHaveBeenCalled());
    expect(screen.queryByLabelText(/UID/)).toBeNull();
  });

  it('clears all filters', async () => {
    currentSearch = '?status=completed&namespace=team-a';
    render(<DiagnosisCenter />);
    await waitFor(() => expect(listSessions).toHaveBeenCalled());

    fireEvent.click(screen.getByRole('button', { name: '清空筛选' }));
    expect(replaceSpy).toHaveBeenCalledWith({ search: '' });
  });

  it('supports the unread entry point and can toggle it off', async () => {
    currentSearch = '?unread=true';
    render(<DiagnosisCenter />);
    await waitFor(() => expect(listSessions).toHaveBeenCalled());
    const call = vi.mocked(listSessions).mock.calls.at(-1)?.[0] as Record<string, unknown>;
    expect(call).toMatchObject({ unread: true });
    // The unread view is diagnoses-only; unresolved alerts stay in their block.
    expect(screen.getByText(/未读列表只包含自动诊断/)).toBeTruthy();

    fireEvent.click(screen.getByText('只看未读'));
    const search = replaceSpy.mock.calls.at(-1)?.[0].search as string;
    expect(search).not.toContain('unread');
    expect(search).not.toContain('after'); // any condition change restarts paging
  });

  it('shows the root cause description first, with the code as a secondary tag and a tooltip', async () => {
    const longReason = '容器内存使用超过 cgroup 限制导致 OOMKilled，并持续重启';
    vi.mocked(listSessions).mockResolvedValue({
      items: [
        session({
          summary: {
            symptom: 's',
            root_cause: longReason,
            root_cause_code: 'CONTAINER_OOMKILLED',
            confidence: 'high',
            insufficient_evidence: false,
          },
        }),
      ],
      next_cursor: null,
    });

    render(<DiagnosisCenter />);
    const reason = await screen.findByText(longReason);
    expect(screen.getByText('CONTAINER_OOMKILLED')).toBeTruthy();

    fireEvent.mouseOver(reason);
    const tooltip = await screen.findByRole('tooltip');
    expect(tooltip.textContent).toContain(longReason);
  });

  it('prefers 证据不足 over a code when evidence is insufficient', async () => {
    vi.mocked(listSessions).mockResolvedValue({
      items: [
        session({
          summary: {
            symptom: 's',
            root_cause: null,
            root_cause_code: 'CONFIG_ERROR',
            confidence: 'low',
            insufficient_evidence: true,
          },
        }),
      ],
      next_cursor: null,
    });

    render(<DiagnosisCenter />);
    expect(await screen.findByText(/证据不足/)).toBeTruthy();
    expect(screen.queryByText('CONFIG_ERROR')).toBeNull();
  });

  it('falls back to the code when no description was returned', async () => {
    vi.mocked(listSessions).mockResolvedValue({
      items: [
        session({
          summary: { symptom: 's', root_cause: null, root_cause_code: 'IMAGE_PULL_FAILED' },
        }),
      ],
      next_cursor: null,
    });

    render(<DiagnosisCenter />);
    expect(await screen.findByText('IMAGE_PULL_FAILED')).toBeTruthy();
  });

  it('paginates with the Headlamp-style TablePagination control', async () => {
    vi.mocked(listSessions).mockImplementation((async (filters: SessionFilters) => {
      if (filters?.after === 'CUR2') {
        return { items: [session({ diagnosis_id: 'diag_p2' })], next_cursor: null };
      }
      return { items: [session({ diagnosis_id: 'diag_p1' })], next_cursor: 'CUR2' };
    }) as never);

    const { rerender } = render(<DiagnosisCenter />);
    await waitFor(() => expect(listSessions).toHaveBeenCalled());
    expect(screen.queryByRole('button', { name: '加载更多' })).toBeNull();

    const prev = screen.getByRole('button', { name: /previous page/i }) as HTMLButtonElement;
    const next = screen.getByRole('button', { name: /next page/i }) as HTMLButtonElement;
    expect(prev.disabled).toBe(true);
    expect(next.disabled).toBe(false);

    fireEvent.click(next);
    const search = replaceSpy.mock.calls.at(-1)?.[0].search as string;
    expect(search).toContain('after=CUR2');
    expect(search).toContain('page=2');

    currentSearch = `?${search}`;
    rerender(<DiagnosisCenter />);
    await waitFor(() => expect(screen.getByText('16-16')).toBeTruthy());
    const lastFilters = vi.mocked(listSessions).mock.calls.at(-1)?.[0] as SessionFilters;
    expect(lastFilters.after).toBe('CUR2');
    expect((screen.getByRole('button', { name: /next page/i }) as HTMLButtonElement).disabled).toBe(
      true
    );

    fireEvent.click(screen.getByRole('button', { name: /previous page/i }));
    const back = replaceSpy.mock.calls.at(-1)?.[0].search as string;
    expect(back).not.toContain('after=');
    expect(back).not.toContain('page=');
  });

  it('persists the rows-per-page choice and restarts from page one', async () => {
    render(<DiagnosisCenter />);
    await waitFor(() => expect(listSessions).toHaveBeenCalled());

    fireEvent.mouseDown(screen.getByRole('combobox', { name: /每页行数/ }));
    fireEvent.click(await screen.findByRole('option', { name: '25' }));
    expect(vi.mocked(setTablesRowsPerPage)).toHaveBeenCalledWith(25);
    const search = replaceSpy.mock.calls.at(-1)?.[0].search as string;
    expect(search).toContain('perPage=25');
    expect(search).not.toContain('page=');
    expect(search).not.toContain('after=');
  });
});
