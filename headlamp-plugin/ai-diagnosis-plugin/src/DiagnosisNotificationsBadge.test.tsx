/** App-bar badge: links to the current cluster's center, no read marking. */

import { Utils } from '@kinvolk/headlamp-plugin/lib';
import { render, screen } from '@testing-library/react';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('react-router-dom', () => ({
  Link: ({ to, children, ...rest }: { to: string; children?: React.ReactNode }) =>
    React.createElement('a', { href: to, ...rest }, children),
}));

vi.mock('@kinvolk/headlamp-plugin/lib', () => ({
  Router: { createRouteURL: (name: string) => `/c/cluster-a/${name}` },
  Utils: { getCluster: vi.fn(() => 'cluster-a') },
}));

vi.mock('./useDiagnosisNotifications', () => ({
  useDiagnosisNotifications: vi.fn(),
  refreshDiagnosisNotifications: vi.fn(),
}));

import DiagnosisNotificationsBadge from './DiagnosisNotificationsBadge';
import { useDiagnosisNotifications } from './useDiagnosisNotifications';

function mockNotifications(unreadCount: number, liveMessage: string, pendingAlertCount = 0) {
  vi.mocked(useDiagnosisNotifications).mockReturnValue({
    unreadCount,
    pendingAlertCount,
    liveMessage,
    ready: true,
    error: null,
    items: [],
    refresh: vi.fn(),
  });
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('DiagnosisNotificationsBadge', () => {
  it('links to the current cluster diagnosis center and shows the count', () => {
    mockNotifications(3, '3 条未读的自动诊断或未解析告警');
    render(<DiagnosisNotificationsBadge />);

    const link = screen.getByTestId('diagnosis-center-link');
    // Unread entry point: lands directly on the unread view when there is unread.
    expect(link.getAttribute('href')).toBe('/c/cluster-a/ai-diagnosis?unread=true');
    expect(link.getAttribute('aria-label')).toBe('打开智能诊断中心');
    expect(screen.getByText('3')).toBeTruthy();
    expect(screen.getByRole('status').textContent).toBe('3 条未读的自动诊断或未解析告警');
  });

  it('stays navigable with zero unread', () => {
    mockNotifications(0, '没有未读诊断或待处理告警', 2);
    render(<DiagnosisNotificationsBadge />);

    const link = screen.getByTestId('diagnosis-center-link');
    expect(link.getAttribute('href')).toBe('/c/cluster-a/ai-diagnosis');
    expect(screen.queryByText('0')).toBeNull();
  });

  it('is disabled on the home page before a cluster is selected', () => {
    mockNotifications(2, '2 条未读的自动诊断或未解析告警');
    vi.mocked(Utils.getCluster).mockReturnValue(null);
    render(<DiagnosisNotificationsBadge />);

    const button = screen.getByTestId('diagnosis-center-link') as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.getAttribute('href')).toBeNull();
    expect(screen.queryByText('2')).toBeTruthy(); // unread still visible
  });
});
