import {
  AuthError,
  ConflictError,
  CsrfError,
  ForbiddenError,
  ExperimentalDisabledError,
  NetworkError,
  NotFoundError,
  RateLimitError,
  SaasError,
  ServerError,
  ValidationError,
} from './errors';
import type { AuthStrategy, ClientOptions, RetryOptions } from './client';

export type HttpOptions = Pick<ClientOptions, 'baseUrl' | 'auth' | 'fetch' | 'retry' | 'csrfToken'>;

export interface RequestOptions {
  body?: unknown;
  query?: Record<string, string | undefined>;
  signal?: AbortSignal;
  /**
   * The server honours `Idempotency-Key` for this operation, so a retry cannot repeat the side effect.
   * Mutating requests without this flag (and without `idempotencyKey`) are never retried automatically.
   */
  idempotent?: boolean;
  /** Force a specific Idempotency-Key (implies idempotent). */
  idempotencyKey?: string;
}

export const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

export function backoff(attempt: number, r: RetryOptions | undefined): number {
  const base = r?.baseDelayMs ?? 250;
  const max = r?.maxDelayMs ?? 5000;
  const exp = Math.min(max, base * 2 ** attempt);
  return Math.round(exp / 2 + Math.random() * (exp / 2));
}

export function newKey(): string {
  const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
  if (c?.randomUUID) return c.randomUUID();
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
}

interface ErrorBody {
  code?: string;
  message?: string;
  field_errors?: Record<string, string>;
  request_id?: string;
  retry_after?: number;
}

const stripHtml = (s: string) =>
  s
    .replace(/<[^>]*>/g, '')
    .replace(/&nbsp;/g, ' ')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .trim();

/** Frappe `_server_messages`: a JSON string holding an array of JSON strings, each `{message, ...}`. */
function firstServerMessage(raw: unknown): string | undefined {
  if (typeof raw !== 'string') return undefined;
  try {
    const list = JSON.parse(raw) as unknown;
    if (!Array.isArray(list) || list.length === 0) return undefined;
    const first = list[0];
    const item = typeof first === 'string' ? (JSON.parse(first) as { message?: unknown }) : (first as { message?: unknown });
    return typeof item?.message === 'string' ? stripHtml(item.message) : undefined;
  } catch {
    return undefined;
  }
}

/** `exception` looks like `frappe.exceptions.ValidationError: message`; return the trailing message. */
function exceptionMessage(raw: unknown): string | undefined {
  if (typeof raw !== 'string') return undefined;
  const i = raw.indexOf(': ');
  return stripHtml(i >= 0 ? raw.slice(i + 2) : raw) || undefined;
}

type ErrorCtor = new (message: string, status?: number, code?: string, fieldErrors?: Record<string, string>, requestId?: string) => SaasError;

const FRAPPE_EXC: Record<string, ErrorCtor> = {
  PermissionError: ForbiddenError,
  ValidationError: ValidationError,
  MandatoryError: ValidationError,
  LinkValidationError: ValidationError,
  DoesNotExistError: NotFoundError,
  DuplicateEntryError: ConflictError,
  AuthenticationError: AuthError,
  SessionExpired: AuthError,
  CSRFTokenError: CsrfError,
};

function retryAfterMs(headers: Headers, eb: ErrorBody): number | undefined {
  const ra = headers.get('Retry-After');
  if (ra) {
    const secs = Number(ra);
    if (Number.isFinite(secs)) return secs * 1000;
    const d = Date.parse(ra);
    return Number.isNaN(d) ? undefined : Math.max(0, d - Date.now());
  }
  return typeof eb.retry_after === 'number' ? eb.retry_after * 1000 : undefined;
}

export function mapError(status: number, body: unknown, headers: Headers): SaasError {
  // Frappe wraps whitelisted-method responses as {message: <payload>}; unwrap before reading `error`.
  let root: unknown = body;
  if (root && typeof root === 'object' && !('error' in root) && 'message' in root) {
    const m = (root as { message?: unknown }).message;
    if (m && typeof m === 'object') root = m;
  }
  const rootObj = root && typeof root === 'object' ? (root as Record<string, unknown>) : undefined;
  const envelope = rootObj?.error && typeof rootObj.error === 'object' ? (rootObj.error as ErrorBody) : undefined;

  // 1. saas_core envelope: {error: {code, message, field_errors, request_id}}
  if (envelope) return byStatus(status, envelope, headers);

  // 2. Frappe-native envelope: {exc_type, exception, _server_messages}
  const bodyObj = body && typeof body === 'object' ? (body as Record<string, unknown>) : undefined;
  const excType = typeof bodyObj?.exc_type === 'string' ? bodyObj.exc_type : undefined;
  if (bodyObj && (excType || bodyObj.exception || bodyObj._server_messages)) {
    const message = firstServerMessage(bodyObj._server_messages) ?? exceptionMessage(bodyObj.exception) ?? `HTTP ${status}`;
    const ctor = excType ? FRAPPE_EXC[excType.split('.').pop() ?? ''] : undefined;
    if (ctor) return new ctor(message, status, excType);
    return byStatus(status, { message, code: excType }, headers);
  }

  return byStatus(status, {}, headers);
}

function byStatus(status: number, eb: ErrorBody, headers: Headers): SaasError {
  const args = [eb.message ?? `HTTP ${status}`, status, eb.code, eb.field_errors, eb.request_id] as const;
  if (status === 401) return new AuthError(...args);
  if (status === 403) return eb.code === 'experimental_disabled' ? new ExperimentalDisabledError(...args) : new ForbiddenError(...args);
  if (status === 404) return new NotFoundError(...args);
  if (status === 409) return new ConflictError(...args);
  if (status === 422 || status === 400 || status === 417) return new ValidationError(...args);
  if (status === 429) {
    const e = new RateLimitError(...args);
    e.retryAfterMs = retryAfterMs(headers, eb);
    return e;
  }
  if (status >= 500) return new ServerError(...args);
  return new SaasError(...args);
}

function unexpectedResponse(status: number, text: string): SaasError {
  const e = new SaasError(`Unexpected non-JSON response (HTTP ${status}); is baseUrl pointing at the saas_core site?`, status, 'non_json_response');
  e.detail = text.slice(0, 200);
  return e;
}

export function createHttp(o: HttpOptions) {
  const f = o.fetch ?? ((...a: Parameters<typeof fetch>) => globalThis.fetch(...a));
  const root = `${o.baseUrl.replace(/\/+$/, '')}/api/method/saas_core.api.v1.`;
  const retries = o.retry?.retries ?? 3;
  const cookieAuth = o.auth?.type === 'cookie';
  let csrf: string | undefined;
  let csrfInFlight: Promise<void> | undefined;

  async function authHeaders(h: Headers): Promise<void> {
    const a: AuthStrategy | undefined = o.auth;
    if (!a || a.type === 'cookie') return;
    if (a.header) h.set(a.header, a.key);
    else h.set('Authorization', `token ${a.key}`);
  }

  async function raw(method: string, path: string, opts: RequestOptions, key?: string, extra?: Record<string, string>): Promise<Response> {
    const h = new Headers({ Accept: 'application/json', ...extra });
    await authHeaders(h);
    if (key) h.set('Idempotency-Key', key);
    let body: string | undefined;
    if (opts.body !== undefined) {
      h.set('Content-Type', 'application/json');
      body = JSON.stringify(opts.body);
    }
    let url = root + path;
    if (method !== 'GET' && csrf && cookieAuth) h.set('X-Frappe-CSRF-Token', csrf);
    const q = Object.entries(opts.query ?? {}).filter(([, v]) => v !== undefined) as [string, string][];
    if (q.length) url += `?${new URLSearchParams(q).toString()}`;
    const init: RequestInit = { method, headers: h, body, signal: opts.signal };
    if (cookieAuth) init.credentials = 'include';
    try {
      return await f(url, init);
    } catch (e) {
      if (opts.signal?.aborted) throw e;
      throw new NetworkError(e instanceof Error ? e.message : 'Network error');
    }
  }

  /** Parse a 2xx body: unwrap Frappe's `{message: ...}`; anything that is not JSON becomes a SaasError. */
  async function parseOk<T>(res: Response): Promise<T> {
    if (res.status === 204) return undefined as T;
    const text = await res.text();
    if (!text) return undefined as T;
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      throw unexpectedResponse(res.status, text);
    }
    return (parsed && typeof parsed === 'object' && 'message' in parsed ? (parsed as { message?: T }).message : parsed) as T;
  }

  /** Error responses are usually JSON; fall back to an empty body (status-only error) when they are not. */
  async function errorFrom(res: Response): Promise<SaasError> {
    const text = await res.text().catch(() => '');
    let body: unknown;
    try {
      body = text ? JSON.parse(text) : undefined;
    } catch {
      body = undefined;
    }
    const err = mapError(res.status, body, res.headers);
    if (body === undefined && text) err.detail = text.slice(0, 200);
    return err;
  }

  /** Fetch the CSRF token via `me` (single flight). Failures are swallowed: the request itself will report them. */
  function refreshCsrf(): Promise<void> {
    csrfInFlight ??= (async () => {
      try {
        const me = await request<{ csrf_token?: string }>('GET', 'me');
        if (me?.csrf_token) csrf = me.csrf_token;
      } catch {
        // guest sessions may not be able to call `me`; proceed without a token
      } finally {
        csrfInFlight = undefined;
      }
    })();
    return csrfInFlight;
  }

  async function ensureCsrf(): Promise<void> {
    if (csrf) return;
    const opt = typeof o.csrfToken === 'function' ? await o.csrfToken() : o.csrfToken;
    const fromWindow = (globalThis as { csrf_token?: unknown }).csrf_token;
    const known = opt || (typeof fromWindow === 'string' && fromWindow && !fromWindow.includes('{{') ? fromWindow : undefined);
    if (known) {
      csrf = known;
      return;
    }
    await refreshCsrf();
  }

  /** Authenticated raw fetch for streaming (no retry). */
  async function stream(path: string, query: Record<string, string>, signal: AbortSignal, lastEventId?: string): Promise<Response> {
    const extra = { Accept: 'text/event-stream', ...(lastEventId ? { 'Last-Event-ID': lastEventId } : {}) };
    const res = await raw('GET', path, { query, signal }, undefined, extra);
    if (!res.ok) throw await errorFrom(res);
    return res;
  }

  async function request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
    const mutating = method !== 'GET';
    const retriable = !mutating || opts.idempotent === true || opts.idempotencyKey !== undefined;
    const key = mutating && retriable ? (opts.idempotencyKey ?? newKey()) : undefined; // reused across retries
    const needsCsrf = mutating && cookieAuth;
    if (needsCsrf) await ensureCsrf();
    let csrfReplayed = false;
    for (let attempt = 0; ; attempt++) {
      let err: SaasError;
      try {
        const res = await raw(method, path, opts, key);
        if (res.ok) return await parseOk<T>(res);
        err = await errorFrom(res);
      } catch (e) {
        if (!(e instanceof NetworkError)) throw e;
        err = e;
      }
      // A rejected CSRF check never reached the handler, so one replay with a fresh token is always safe.
      if (err instanceof CsrfError && needsCsrf && !csrfReplayed) {
        csrfReplayed = true;
        csrf = undefined;
        await refreshCsrf();
        attempt--;
        continue;
      }
      const transient = err instanceof NetworkError || err instanceof ServerError || err instanceof RateLimitError;
      if (!retriable || !transient || attempt >= retries) throw err;
      const wait = err instanceof RateLimitError && err.retryAfterMs !== undefined ? err.retryAfterMs : backoff(attempt, o.retry);
      await sleep(wait);
    }
  }

  /** Binary/redirect-free GET returning the raw response (e.g. invoice PDF). */
  async function download(path: string, query: Record<string, string>): Promise<Blob> {
    const res = await raw('GET', path, { query });
    if (!res.ok) throw await errorFrom(res);
    return res.blob();
  }

  return {
    request,
    stream,
    download,
    setCsrf: (t: string | undefined) => {
      csrf = t;
    },
  };
}
export type Http = ReturnType<typeof createHttp>;
