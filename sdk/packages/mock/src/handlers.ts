import { http, HttpResponse } from 'msw';
import { createMockApi, type MockApi, type MockOptions } from './core.js';

export interface MockHandlers {
  handlers: ReturnType<typeof http.all>[];
  api: MockApi;
  /** Reset the in-memory store to seed data and cancel timers/streams. */
  reset(): void;
}

export function createMockHandlers(opts: MockOptions = {}): MockHandlers {
  const api = createMockApi(opts);
  const base = (api.options.baseUrl || '').replace(/\/+$/, '');
  const handlers = [
    http.all(`${base || '*'}/api/method/saas_core.api.v1.*`, async ({ request }) => {
      const res = await api.handle(request);
      return res ?? HttpResponse.json({ error: { code: 'not_found', message: 'Not found', field_errors: {} } }, { status: 404 });
    }),
  ];
  return { handlers, api, reset: () => api.reset() };
}
