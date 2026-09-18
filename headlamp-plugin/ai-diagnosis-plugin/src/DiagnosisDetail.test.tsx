/** Page-lifecycle tests: growing timeline, stale-response guard, auto read. */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

let currentId = 'diag_A';

vi.mock('react-router-dom', () => ({
  useParams: () => ({ diagnosisId: currentId }),
  Link: ({ children }: { children?: React.ReactNode }) =>
    React.createElement('span', null, children),
}));

vi.mock('@kinvolk/headlamp-plugin/lib', () => ({
  Router: { createRouteURL: (name: string) => `/c/test/${name}` },
}));

vi.mock('@kinvolk/headlamp-plugin/lib/CommonComponents', () => ({
  SectionBox: ({ children }: { children?: React.ReactNode }) =>
    React.createElement('div', null, children),
}));

vi.mock('./api', () => ({
  getDiagnosis: vi.fn(),
  getTimeline: vi.fn(),
  markRead: vi.fn(),
}));

vi.mock('./useDiagnosisNotifications', () => ({
  refreshDiagnosisNotifications: vi.fn(),
}));

vi.mock('./viewer', () => ({
  getViewerId: () => 'viewer-1',
  bumpReadRevision: vi.fn(),
}));

import { getDiagnosis, getTimeline, markRead } from './api';
import DiagnosisDetail from './DiagnosisDetail';

function runningDiagnosis() {
  return {
    diagnosis_id: currentId,
    trigger: 'alert',
    resource: { kind: 'Pod', namespace: 'ns', name: 'p' },
    status: 'investigating',
    result: null,
    error: null,
    alert: null,
    created_at: '2026-09-16T00:00:00Z',
    updated_at: '2026-09-16T00:00:00Z',
  };
}

function completedDiagnosis() {
  return {
    ...runningDiagnosis(),
    status: 'completed',
    result: {
      symptom: 's',
      evidence: [],
      recommendations: [],
      missing_evidence: [],
      investigation_steps: ['获取 Pod 状态', '查询事件'],
    },
  };
}

function timelinePage(title: string, nextAfter: number) {
  return {
    items: [
      {
        id: `diag:${nextAfter}`,
        seq: nextAfter,
        timestamp: null,
        kind: 'tool_completed',
        title,
        status: 'completed',
        duration_ms: 1,
        failure_layer: null,
      },
    ],
    next_after: nextAfter,
    has_more: false,
    available: 'available' as const,
    gap: false,
  };
}

function ev(id: string, kind: string, title: string) {
  return {
    id,
    seq: Number(id.replace(/\D/g, '')) || 0,
    timestamp: null,
    kind,
    title,
    status: 'completed',
    duration_ms: 1,
    failure_layer: null,
  };
}

function pageOf(items: ReturnType<typeof ev>[], nextAfter: number, hasMore = false) {
  return {
    items,
    next_after: nextAfter,
    has_more: hasMore,
    available: 'available' as const,
    gap: false,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(res => {
    resolve = res;
  });
  return { promise, resolve };
}

beforeEach(() => {
  currentId = 'diag_A';
  vi.useFakeTimers();
  vi.mocked(markRead).mockResolvedValue({ id: 'diag_A', read: true });
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe('DiagnosisDetail lifecycle', () => {
  it('keeps appending timeline events while the diagnosis runs, then marks read once', async () => {
    const emptyPage = {
      items: [],
      next_after: 20,
      has_more: false,
      available: 'available' as const,
      gap: false,
    };
    let timelineCalls = 0;
    vi.mocked(getDiagnosis)
      .mockResolvedValueOnce(runningDiagnosis() as never)
      .mockResolvedValue(completedDiagnosis() as never);
    vi.mocked(getTimeline).mockImplementation((async () => {
      timelineCalls += 1;
      if (timelineCalls === 1) return timelinePage('第一步', 10);
      if (timelineCalls === 2) return timelinePage('第二步', 20);
      return emptyPage;
    }) as never);

    render(<DiagnosisDetail />);
    await waitFor(() => expect(screen.getByText(/第一步/)).toBeTruthy());

    for (let i = 0; i < 5 && !screen.queryByText(/第二步/); i += 1) {
      await vi.advanceTimersByTimeAsync(2100);
    }
    expect(screen.getByText(/第二步/)).toBeTruthy();
    await waitFor(() => expect(markRead).toHaveBeenCalledTimes(1));
    expect(vi.mocked(markRead)).toHaveBeenCalledWith('diag_A', 'viewer-1');
  });

  it('renders a failed diagnosis as unfinished, not as insufficient evidence', async () => {
    vi.mocked(getDiagnosis).mockResolvedValue({
      ...runningDiagnosis(),
      status: 'failed',
      error: '目标资源已重建（当前 UID 与请求不一致），拒绝本次诊断',
      // A failed run still carries an (empty) result object from the agent.
      result: {
        symptom: '',
        evidence: [],
        recommendations: [],
        missing_evidence: [],
        investigation_steps: [],
      },
    } as never);
    vi.mocked(getTimeline).mockResolvedValue({
      items: [],
      next_after: 0,
      has_more: false,
      available: 'available' as const,
      gap: false,
    } as never);

    render(<DiagnosisDetail />);
    await waitFor(() => expect(screen.getByText(/目标资源已重建/)).toBeTruthy());
    expect(screen.getByText(/诊断未完成，无结论/)).toBeTruthy();
    expect(screen.getByText(/诊断失败，无证据/)).toBeTruthy();
    expect(screen.queryByText(/证据不足，无法确定唯一根因/)).toBeNull();
    expect(screen.queryByText(/调查进行中/)).toBeNull();
    // Failed tool calls never appear as green steps (backend only records
    // successes; the UI must not render a check for an empty step list).
    expect(screen.queryAllByTestId('step-check')).toHaveLength(0);
    expect(screen.getByText(/失败的调用请见下方执行时间线/)).toBeTruthy();
  });

  it('drains remaining pages (has_more) within one refresh pass', async () => {
    let calls = 0;
    vi.mocked(getDiagnosis).mockResolvedValue(completedDiagnosis() as never);
    vi.mocked(getTimeline).mockImplementation((async () => {
      calls += 1;
      if (calls === 1) {
        return pageOf([ev('diag:5', 'tool_completed', '第一页事件')], 10, true);
      }
      return pageOf([ev('diag:15', 'diagnosis_completed', '第二页事件')], 20, false);
    }) as never);

    render(<DiagnosisDetail />);
    await waitFor(() => expect(screen.getByText(/第一页事件/)).toBeTruthy());
    await waitFor(() => expect(screen.getByText(/第二页事件/)).toBeTruthy());
  });

  it('never appends the same event id twice', async () => {
    // Every read returns the same page and the same offset.
    vi.mocked(getDiagnosis).mockResolvedValue(completedDiagnosis() as never);
    vi.mocked(getTimeline).mockImplementation((async () =>
      pageOf([ev('diag:3', 'tool_completed', '重复事件')], 3, false)) as never);

    render(<DiagnosisDetail />);
    await waitFor(() => expect(screen.getByText(/重复事件/)).toBeTruthy());
    await vi.advanceTimersByTimeAsync(6200);
    expect(screen.getAllByText(/重复事件/)).toHaveLength(1);
  });

  it('keeps polling after completion until the terminal event arrives', async () => {
    let calls = 0;
    vi.mocked(getDiagnosis).mockResolvedValue(completedDiagnosis() as never);
    vi.mocked(getTimeline).mockImplementation((async () => {
      calls += 1;
      if (calls === 1) return pageOf([ev('diag:7', 'tool_completed', '工具完成')], 10, false);
      return pageOf([ev('diag:11', 'diagnosis_completed', '诊断完成事件')], 20, false);
    }) as never);

    render(<DiagnosisDetail />);
    await waitFor(() => expect(screen.getByText(/工具完成/)).toBeTruthy());
    // The diagnosis is already terminal, but the final event appears later.
    await vi.advanceTimersByTimeAsync(2100);
    await waitFor(() => expect(screen.getByText(/诊断完成事件/)).toBeTruthy());
  });

  it('stops the pass when the cursor does not advance', async () => {
    vi.mocked(getDiagnosis).mockResolvedValue(runningDiagnosis() as never);
    // A half-written tail line: has_more is true but the offset never moves.
    vi.mocked(getTimeline).mockImplementation((async () => ({
      items: [],
      next_after: 0,
      has_more: true,
      available: 'available',
      gap: false,
    })) as never);

    render(<DiagnosisDetail />);
    await waitFor(() => expect(getTimeline).toHaveBeenCalled());
    const afterMount = vi.mocked(getTimeline).mock.calls.length;
    expect(afterMount).toBeLessThanOrEqual(2);

    await vi.advanceTimersByTimeAsync(2100);
    const afterTick = vi.mocked(getTimeline).mock.calls.length;
    expect(afterTick - afterMount).toBeLessThanOrEqual(2);
  });

  it('warns when the completion event never arrives, and refresh picks it up', async () => {
    vi.mocked(getDiagnosis).mockResolvedValue(completedDiagnosis() as never);
    vi.mocked(getTimeline).mockImplementation((async () =>
      pageOf([ev('diag:7', 'tool_completed', '工具步骤')], 10, false)) as never);

    render(<DiagnosisDetail />);
    await waitFor(() => expect(screen.getByText(/工具步骤/)).toBeTruthy());

    // The bounded wait expires without a terminal event.
    for (let i = 0; i < 18; i += 1) {
      await vi.advanceTimersByTimeAsync(2100);
    }
    await waitFor(() => expect(screen.getByText(/时间线可能不完整/)).toBeTruthy());
    expect(screen.getByText(/工具步骤/)).toBeTruthy();

    // Refresh re-arms the wait and picks up the late event.
    vi.mocked(getTimeline).mockImplementation((async () =>
      pageOf([ev('diag:11', 'diagnosis_completed', '完成事件')], 20, false)) as never);
    fireEvent.click(screen.getByRole('button', { name: '刷新' }));
    await waitFor(() => expect(screen.getByText(/完成事件/)).toBeTruthy());
    expect(screen.queryByText(/时间线可能不完整/)).toBeNull();
  });

  it('ignores a late timeline response from the previous diagnosis', async () => {
    const stale = deferred<ReturnType<typeof timelinePage>>();
    vi.mocked(getDiagnosis).mockResolvedValue(runningDiagnosis() as never);
    vi.mocked(getTimeline).mockReturnValueOnce(stale.promise as never);

    const { rerender } = render(<DiagnosisDetail />);
    await waitFor(() => expect(getTimeline).toHaveBeenCalledTimes(1));

    currentId = 'diag_B';
    vi.mocked(getTimeline).mockResolvedValue(timelinePage('新页面步骤', 30) as never);
    rerender(<DiagnosisDetail />);
    await waitFor(() => expect(screen.getByText(/新页面步骤/)).toBeTruthy());

    stale.resolve(timelinePage('过期步骤', 5));
    await vi.advanceTimersByTimeAsync(50);
    expect(screen.queryByText(/过期步骤/)).toBeNull();
  });

  it('renders a check mark for every investigation step', async () => {
    vi.mocked(getDiagnosis).mockResolvedValue(completedDiagnosis() as never);
    vi.mocked(getTimeline).mockResolvedValue(pageOf([], 0, false) as never);

    render(<DiagnosisDetail />);
    await waitFor(() => expect(screen.getByText('获取 Pod 状态')).toBeTruthy());
    expect(screen.getAllByTestId('step-check')).toHaveLength(2);
  });
});
