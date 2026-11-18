import { createMockApi, type MockApi, type MockOptions } from './core.js';

export type MockFetch = typeof fetch & { api: MockApi; reset(): void };

/** fetch-compatible function backed by the in-memory mock (no MSW needed). */
export function createMockFetch(opts: MockOptions = {}): MockFetch {
  const api = createMockApi(opts);
  const f = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const base = typeof location !== 'undefined' ? location.href : 'http://localhost';
    const req = input instanceof Request && !init ? input
      : new Request(input instanceof Request ? input : new URL(String(input), base), init);
    const res = await api.handle(req);
    return res ?? new Response(JSON.stringify({ error: { code: 'not_found', message: 'Not found', field_errors: {} } }), { status: 404, headers: { 'content-type': 'application/json' } });
  }) as MockFetch;
  f.api = api;
  f.reset = () => api.reset();
  return f;
}
