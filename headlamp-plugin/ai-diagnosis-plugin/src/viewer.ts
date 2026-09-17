/**
 * Local viewer identity for read receipts (design §3.4).
 *
 * This is NOT authentication, authorization or RBAC: it only separates read
 * state per browser. Falls back to session memory when storage is unavailable.
 */

const VIEWER_KEY = 'ai-diagnosis-viewer-id';

let memoryViewer: string | null = null;

function randomId(): string {
  const globalCrypto = window.crypto as Crypto | undefined;
  if (globalCrypto && typeof globalCrypto.randomUUID === 'function') {
    return globalCrypto.randomUUID();
  }
  return `${Date.now().toString(16)}-${Math.random().toString(16).slice(2, 10)}-${Math.random()
    .toString(16)
    .slice(2, 6)}`;
}

/** True when the viewer id is persisted in localStorage (not memory-only). */
export function isViewerPersistent(): boolean {
  try {
    return !!window.localStorage.getItem(VIEWER_KEY);
  } catch {
    return false;
  }
}

export function getViewerId(): string {
  try {
    const existing = window.localStorage.getItem(VIEWER_KEY);
    if (existing) return existing;
    const created = randomId();
    window.localStorage.setItem(VIEWER_KEY, created);
    return created;
  } catch {
    if (!memoryViewer) memoryViewer = randomId();
    return memoryViewer;
  }
}

/** Revision bumped after a successful read so other tabs refetch. */
const REVISION_KEY = 'ai-diagnosis-read-revision';

export function bumpReadRevision(): void {
  try {
    const next = String(Number(window.localStorage.getItem(REVISION_KEY) ?? '0') + 1);
    window.localStorage.setItem(REVISION_KEY, next);
  } catch {
    bumpMemoryRevision();
  }
}

let memoryRevision = 0;

function bumpMemoryRevision(): void {
  memoryRevision += 1;
}

export function getReadRevision(): number {
  try {
    return Number(window.localStorage.getItem(REVISION_KEY) ?? '0');
  } catch {
    return memoryRevision;
  }
}

export function subscribeReadRevision(listener: (revision: number) => void): () => void {
  const handler = (event: StorageEvent) => {
    if (event.key === REVISION_KEY) listener(Number(event.newValue ?? '0'));
  };
  window.addEventListener('storage', handler);
  return () => window.removeEventListener('storage', handler);
}
