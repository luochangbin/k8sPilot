import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  bumpReadRevision,
  getReadRevision,
  getViewerId,
  isViewerPersistent,
  subscribeReadRevision,
} from './viewer';

describe('viewer identity', () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it('creates a persistent viewer id and reuses it', () => {
    const first = getViewerId();
    expect(first).toMatch(/[0-9a-f-]{8,}/i);
    expect(isViewerPersistent()).toBe(true);
    expect(getViewerId()).toBe(first);
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
      expect(id).toBeTruthy();
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
