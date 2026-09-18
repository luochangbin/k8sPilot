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
  // The poll cycle only runs in a visible tab; make that explicit for jsdom.
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => 'visible',
  });
  vi.mocked(markRead).mockResolvedValue({ id: 'diag_A', read: true });
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

function emptyTimeline() {
  return { items: [], next_after: 0, has_more: false, available: 'available' as const, gap: false };
}

describe('DiagnosisDetail polling cycle', () => {
  it('does not start a new poll cycle until status and timeline settle', async () => {
    let hold = true;
    const gates: Array<() => void> = [];
    let statusCalls = 0;
    let timelineCalls = 0;
    vi.mocked(getDiagnosis).mockImplementation((async () => {
      statusCalls += 1;
      if (hold) await new Promise<void>(resolve => gates.push(resolve));
      return runningDiagnosis();
    }) as never);
    vi.mocked(getTimeline).mockImplementation((async () => {
      timelineCalls += 1;
      if (hold) await new Promise<void>(resolve => gates.push(resolve));
      return emptyTimeline();
    }) as never);

    render(<DiagnosisDetail />);
    await waitFor(() => expect(statusCalls).toBe(1));
    await waitFor(() => expect(timelineCalls).toBe(1));

    // Poll cycle #1 fires, but its requests never settle (the timeline call is
    // coalesced into the in-flight one, so only status is re-requested).
    await vi.advanceTimersByTimeAsync(2000);
    await waitFor(() => expect(statusCalls).toBe(2));
    expect(timelineCalls).toBe(1);

    // While cycle #1 is unsettled, further timer ticks must not stack cycle #2.
    await vi.advanceTimersByTimeAsync(30000);
    expect(statusCalls).toBe(2);
    expect(timelineCalls).toBe(1);

    // Release: the next cycle is scheduled only once the current one settles.
    hold = false;
    gates.forEach(resolve => resolve());
    await vi.advanceTimersByTimeAsync(2100);
    await waitFor(() => expect(statusCalls).toBeGreaterThan(2));
    expect(timelineCalls).toBeGreaterThan(1);
  });

  it('coalesces onto the in-flight timeline read and does not queue a rerun', async () => {
    let held = true;
    let releaseTimeline!: () => void;
    let statusCalls = 0;
    let timelineCalls = 0;
    vi.mocked(getDiagnosis).mockImplementation((async () => {
      statusCalls += 1;
      return runningDiagnosis();
    }) as never);
    vi.mocked(getTimeline).mockImplementation((async () => {
      timelineCalls += 1;
      if (held) {
        await new Promise<void>(resolve => {
          releaseTimeline = resolve;
        });
      }
      return emptyTimeline();
    }) as never);

    render(<DiagnosisDetail />);
    await waitFor(() => expect(timelineCalls).toBe(1));
    await waitFor(() => expect(statusCalls).toBeGreaterThan(0));

    // Cycle #1 fires while the initial timeline read is still open: it must
    // await that real promise (no new request, no resolved no-op).
    await vi.advanceTimersByTimeAsync(2000);
    const statusAfterCycle = statusCalls;
    await vi.advanceTimersByTimeAsync(30000);
    expect(statusCalls).toBe(statusAfterCycle); // no stacked cycle
    expect(timelineCalls).toBe(1); // coalesced: no redundant concurrent read

    // Release the real read. The cycle then settles and the *next scheduled*
    // poll fetches once — a queued rerun would add an extra, detached call.
    held = false;
    releaseTimeline();
    await vi.advanceTimersByTimeAsync(2100);
    await waitFor(() => expect(timelineCalls).toBe(2));
    await vi.advanceTimersByTimeAsync(10000);
    expect(timelineCalls).toBeGreaterThanOrEqual(2);
  });

  it('timeline failure increases the delay of the next cycle', async () => {
    let timelineCalls = 0;
    vi.mocked(getDiagnosis).mockResolvedValue(runningDiagnosis() as never);
    vi.mocked(getTimeline).mockImplementation((async () => {
      timelineCalls += 1;
      if (timelineCalls === 2) throw new Error('timeline boom');
      return emptyTimeline();
    }) as never);

    render(<DiagnosisDetail />);
    await waitFor(() => expect(timelineCalls).toBe(1));

    await vi.advanceTimersByTimeAsync(2000); // cycle #1: timeline read fails
    await waitFor(() => expect(timelineCalls).toBe(2));

    await vi.advanceTimersByTimeAsync(2100); // 2.1s after the failure: too early
    expect(timelineCalls).toBe(2);

    await vi.advanceTimersByTimeAsync(2100); // now past the doubled 4s delay
    await waitFor(() => expect(timelineCalls).toBe(3));
  });

  it('uses the current cycle backoff for the next schedule', async () => {
    let statusCalls = 0;
    vi.mocked(getDiagnosis).mockImplementation((async () => {
      statusCalls += 1;
      if (statusCalls === 2) throw new Error('boom');
      return runningDiagnosis();
    }) as never);
    vi.mocked(getTimeline).mockResolvedValue(emptyTimeline() as never);

    render(<DiagnosisDetail />);
    await waitFor(() => expect(statusCalls).toBe(1));

    await vi.advanceTimersByTimeAsync(2000); // cycle #1 fails -> delay 2s -> 4s
    await waitFor(() => expect(statusCalls).toBe(2));
    await vi.advanceTimersByTimeAsync(2100); // 2.1s since the failure: too early
    expect(statusCalls).toBe(2);

    await vi.advanceTimersByTimeAsync(2100); // now past 4s
    await waitFor(() => expect(statusCalls).toBe(3));
  });

  it('clears the poll timer on unmount', async () => {
    vi.mocked(getDiagnosis).mockResolvedValue(runningDiagnosis() as never);
    vi.mocked(getTimeline).mockResolvedValue(emptyTimeline() as never);
    const { unmount } = render(<DiagnosisDetail />);
    await waitFor(() => expect(vi.mocked(getDiagnosis).mock.calls.length).toBeGreaterThan(0));
    const before = vi.mocked(getDiagnosis).mock.calls.length;
    unmount();
    await vi.advanceTimersByTimeAsync(30000);
    expect(vi.mocked(getDiagnosis).mock.calls.length).toBe(before);
  });
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
