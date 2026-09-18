import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  bumpReadRevision,
  getReadRevision,
  getViewerId,
  isUuidV4,
  isViewerPersistent,
  subscribeReadRevision,
} from './viewer';

const UUID_V4_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

describe('viewer identity', () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it('creates a persistent viewer id and reuses it', () => {
    const first = getViewerId();
    // The server rejects anything that is not a canonical UUIDv4.
    expect(first).toMatch(UUID_V4_RE);
    expect(isUuidV4(first)).toBe(true);
    expect(isViewerPersistent()).toBe(true);
    expect(getViewerId()).toBe(first);
  });

  it('falls back to a well-formed UUIDv4 without randomUUID', () => {
    const cryptoWithUuid = window.crypto as Crypto & { randomUUID?: () => string };
    const original = cryptoWithUuid.randomUUID;
    delete cryptoWithUuid.randomUUID; // simulate an older browser without randomUUID
    try {
      const id = getViewerId();
      expect(id).toMatch(UUID_V4_RE);
    } finally {
      if (original) cryptoWithUuid.randomUUID = original;
    }
  });

  it('replaces a non-conforming stored id', () => {
    window.localStorage.setItem('ai-diagnosis-viewer-id', 'legacy-not-a-uuid');
    const id = getViewerId();
    expect(id).toMatch(UUID_V4_RE);
    expect(window.localStorage.getItem('ai-diagnosis-viewer-id')).toBe(id);
  });

  it('falls back to session memory when storage is unavailable', () => {
    const original = Object.getOwnPropertyDescriptor(window, 'localStorage');
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      get() {
        throw new Error('storage denied');
      },
    });
    try {
      const id = getViewerId();
      expect(id).toMatch(UUID_V4_RE);
      expect(getViewerId()).toBe(id);
      expect(isViewerPersistent()).toBe(false);
    } finally {
      if (original) Object.defineProperty(window, 'localStorage', original);
    }
  });
});

describe('read revision', () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it('increments and notifies subscribers', () => {
    expect(getReadRevision()).toBe(0);
    bumpReadRevision();
    expect(getReadRevision()).toBe(1);

    const listener = vi.fn();
    const unsubscribe = subscribeReadRevision(listener);
    window.dispatchEvent(
      new StorageEvent('storage', { key: 'ai-diagnosis-read-revision', newValue: '2' })
    );
    expect(listener).toHaveBeenCalledWith(2);
    unsubscribe();
  });
});
