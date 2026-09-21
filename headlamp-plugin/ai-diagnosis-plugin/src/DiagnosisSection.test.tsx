/** Manual-entry section: polling lifecycle and request contract. */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@kinvolk/headlamp-plugin/lib/CommonComponents', () => ({
  SectionBox: ({ children }: { children?: React.ReactNode }) =>
    React.createElement('div', null, children),
}));

vi.mock('react-router-dom', () => ({
  Link: ({ to, children }: { to: string; children?: React.ReactNode }) =>
    React.createElement('a', { href: to }, children),
}));

vi.mock('./routes', () => ({
  centerUrl: (query?: string) => (query ? `/c/x/ai-diagnosis?${query}` : '/c/x/ai-diagnosis'),
}));

import DiagnosisSection from './DiagnosisSection';

const resource = (overrides: Record<string, unknown> = {}) => ({
  kind: 'Pod',
  cluster: 'cluster-a',
  apiVersion: 'v1',
  metadata: { name: 'payment-api', namespace: 'payment', uid: 'uid-1' },
  ...overrides,
});

interface FetchCall {
  url: string;
  body?: Record<string, unknown>;
}

function stubFetch(handlers: Array<(call: FetchCall) => unknown>): FetchCall[] {
  const calls: FetchCall[] = [];
  let index = 0;
  vi.stubGlobal('fetch', (url: string, init?: RequestInit) => {
    const call: FetchCall = {
      url: String(url),
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    };
    calls.push(call);
    const handler = handlers[Math.min(index, handlers.length - 1)];
    index += 1;
    const value = handler(call);
    return Promise.resolve({
      ok: true,
      status: 200,
      json: async () => value,
      text: async () => JSON.stringify(value),
    } as unknown as Response);
  });
  return calls;
}

function diagnosis(status: string) {
  return {
    diagnosis_id: 'diag_A',
    status,
    result: null,
    error: null,
    created_at: '2026-09-18T00:00:00Z',
    updated_at: '2026-09-18T00:00:00Z',
  };
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe('DiagnosisSection', () => {
  it('stops polling when the first poll is already terminal', async () => {
    const calls = stubFetch([
      () => ({ diagnosis_id: 'diag_A' }), // POST /diagnoses
      () => diagnosis('completed'), // first GET
    ]);

    render(<DiagnosisSection resource={resource() as never} />);
    fireEvent.click(screen.getByRole('button', { name: '智能诊断' }));
    await waitFor(() => expect(calls.length).toBe(2));

    // A completed first poll must not schedule any further polling.
    await vi.advanceTimersByTimeAsync(30000);
    expect(calls.filter(call => call.url.includes('/api/v1/diagnoses/')).length).toBe(1);
  });

  it('does not overlap a slow poll with the next one', async () => {
    let releasePoll!: (value: unknown) => void;
    const gate = new Promise<unknown>(resolve => {
      releasePoll = resolve;
    });
    const calls = stubFetch([
      () => ({ diagnosis_id: 'diag_A' }), // POST
      () => gate, // first GET stays pending
      () => diagnosis('investigating'), // later GETs
    ]);

    render(<DiagnosisSection resource={resource() as never} />);
    fireEvent.click(screen.getByRole('button', { name: '智能诊断' }));
    await waitFor(() => expect(calls.length).toBe(2));

    // Many intervals pass while the first GET is still in flight: no new GET.
    await vi.advanceTimersByTimeAsync(20000);
    expect(calls.filter(call => call.url.includes('/api/v1/diagnoses/')).length).toBe(1);

    releasePoll(diagnosis('investigating'));
    await vi.advanceTimersByTimeAsync(2000);
    await waitFor(() =>
      expect(calls.filter(call => call.url.includes('/api/v1/diagnoses/')).length).toBe(2)
    );
  });

  it('sends the real apiVersion of the target resource', async () => {
    const calls = stubFetch([() => ({ diagnosis_id: 'diag_A' }), () => diagnosis('completed')]);

    render(
      <DiagnosisSection
        resource={resource({ kind: 'Deployment', apiVersion: 'apps/v1' }) as never}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: '智能诊断' }));
    await waitFor(() => expect(calls.length).toBeGreaterThan(0));

    const posted = calls[0].body as { resource: { apiVersion: string; kind: string } };
    expect(posted.resource.apiVersion).toBe('apps/v1');
    expect(posted.resource.kind).toBe('Deployment');
  });

  it('falls back to the kind-correct apiVersion when the resource omits one', async () => {
    const calls = stubFetch([() => ({ diagnosis_id: 'diag_A' }), () => diagnosis('completed')]);

    render(
      <DiagnosisSection resource={resource({ kind: 'Deployment', apiVersion: undefined }) as never} />
    );
    fireEvent.click(screen.getByRole('button', { name: '智能诊断' }));
    await waitFor(() => expect(calls.length).toBeGreaterThan(0));

    const posted = calls[0].body as { resource: { apiVersion: string } };
    expect(posted.resource.apiVersion).toBe('apps/v1');
  });
});
