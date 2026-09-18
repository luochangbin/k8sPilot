/**
 * Local viewer identity for read receipts (design §3.4).
 *
 * This is NOT authentication, authorization or RBAC: it only separates read
 * state per browser. Falls back to session memory when storage is unavailable.
 */

const VIEWER_KEY = 'ai-diagnosis-viewer-id';

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

let memoryViewer: string | null = null;

/** True for canonical, lower-case RFC 4122 v4 ids (what the server accepts). */
export function isUuidV4(value: string | null | undefined): boolean {
  return !!value && UUID_V4.test(value);
}

function bytesToUuid(bytes: Uint8Array): string {
  bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant 10xx
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function randomId(): string {
  const globalCrypto = window.crypto as Crypto | undefined;
  if (globalCrypto && typeof globalCrypto.randomUUID === 'function') {
    return globalCrypto.randomUUID();
  }
  const bytes = new Uint8Array(16);
  if (globalCrypto && typeof globalCrypto.getRandomValues === 'function') {
    globalCrypto.getRandomValues(bytes);
  } else {
    // Identity, not a security boundary: a well-formed v4 is what matters here.
    for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  }
  return bytesToUuid(bytes);
}

/** True when the viewer id is persisted in localStorage (not memory-only). */
export function isViewerPersistent(): boolean {
  try {
    return isUuidV4(window.localStorage.getItem(VIEWER_KEY));
  } catch {
    return false;
  }
}

export function getViewerId(): string {
  try {
    const existing = window.localStorage.getItem(VIEWER_KEY);
    // Regenerate non-conforming ids written by older builds: the server rejects
    // anything that is not a canonical UUIDv4.
    if (isUuidV4(existing)) return existing as string;
    const created = randomId();
    window.localStorage.setItem(VIEWER_KEY, created);
    return created;
  } catch {
    if (!isUuidV4(memoryViewer)) memoryViewer = randomId();
    return memoryViewer as string;
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
