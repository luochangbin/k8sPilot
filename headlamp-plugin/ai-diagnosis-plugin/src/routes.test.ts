import { Router } from '@kinvolk/headlamp-plugin/lib';
import { describe, expect, it, vi } from 'vitest';

vi.mock('@kinvolk/headlamp-plugin/lib', () => ({
  Router: {
    createRouteURL: vi.fn(
      (name: string, params?: Record<string, string>) =>
        `/c/test-cluster/${name}${params?.diagnosisId ? `/${params.diagnosisId}` : ''}`
    ),
  },
}));

import { centerUrl, detailUrl } from './routes';

describe('route helpers keep the cluster prefix', () => {
  it('builds the center url, with optional query', () => {
    expect(centerUrl()).toBe('/c/test-cluster/ai-diagnosis');
    expect(centerUrl('uid=u1')).toBe('/c/test-cluster/ai-diagnosis?uid=u1');
  });

  it('builds the detail url from the diagnosis id', () => {
    expect(detailUrl('diag_1')).toBe('/c/test-cluster/ai-diagnosis-detail/diag_1');
  });

  it('falls back to the plugin path when createRouteURL returns empty', () => {
    vi.mocked(Router.createRouteURL).mockReturnValueOnce('');
    expect(centerUrl()).toBe('/ai-diagnosis');
  });

  it('falls back when there is no cluster path param ("/")', () => {
    vi.mocked(Router.createRouteURL).mockReturnValueOnce('/');
    expect(detailUrl('diag_9')).toBe('/ai-diagnosis/diag_9');
  });
});
