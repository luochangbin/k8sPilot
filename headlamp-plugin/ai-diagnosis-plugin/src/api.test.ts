import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  ApiError,
  getDiagnosis,
  getTimeline,
  listNotifications,
  listSessions,
  markRead,
} from './api';

function mockFetch(body: unknown, ok = true, status = 200) {
  const spy = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    // Params are typed so callers can assert on the recorded arguments.
    void input;
    void init;
    return Promise.resolve({
      ok,
      status,
      json: async () => body,
      text: async () => JSON.stringify(body),
    });
  });
  vi.stubGlobal('fetch', spy);
  return spy;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('api url building', () => {
  it('passes filters, limit and viewer to sessions', async () => {
    const fetchSpy = mockFetch({ items: [], next_cursor: null });
    await listSessions({ status: 'completed', uid: 'uid-1' }, 20, 'viewer-1');
    const url = String(fetchSpy.mock.calls[0][0]);
    expect(url).toContain('/api/v1/diagnosis-center/sessions?');
    expect(url).toContain('status=completed');
    expect(url).toContain('uid=uid-1');
    expect(url).toContain('limit=20');
    expect(url).toContain('viewer_id=viewer-1');
  });

  it('encodes the diagnosis id and timeline offsets', async () => {
    const fetchSpy = mockFetch({ items: [], next_cursor: null });
    await getDiagnosis('diag_1', undefined);
    await getTimeline('diag_1', 256, 50);
    const urls = fetchSpy.mock.calls.map(call => String(call[0]));
    expect(urls[0]).toContain('/api/v1/diagnoses/diag_1');
    expect(urls[1]).toContain('/api/v1/diagnoses/diag_1/timeline?after=256&limit=50');
  });

  it('posts the viewer id when marking read and lists notifications', async () => {
    const fetchSpy = mockFetch({ id: 'diag_1', read: true });
    await markRead('diag_1', 'viewer-1');
    const init = fetchSpy.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe('POST');
    expect(JSON.parse(String(init.body))).toEqual({ viewer_id: 'viewer-1' });

    const notesSpy = mockFetch({ unread_count: 0, items: [], next_cursor: null });
    await listNotifications('viewer-1', 50);
    expect(String(notesSpy.mock.calls[0][0])).toContain('viewer_id=viewer-1');
  });

  it('maps non-2xx responses to ApiError with status', async () => {
    mockFetch({ detail: 'invalid viewer_id' }, false, 422);
    await expect(listNotifications('bad', 50)).rejects.toBeInstanceOf(ApiError);
    await expect(listNotifications('bad', 50)).rejects.toMatchObject({ status: 422 });
  });
});
