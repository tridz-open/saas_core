import { setupServer } from 'msw/node';
import { createMockHandlers } from './handlers.js';
import type { MockOptions } from './core.js';

export function createMockServer(opts: MockOptions = {}) {
  const h = createMockHandlers(opts);
  const server = setupServer(...h.handlers);
  return Object.assign(server, { mock: h, resetStore: h.reset });
}
