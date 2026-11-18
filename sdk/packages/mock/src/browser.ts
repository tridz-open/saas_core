import { setupWorker } from 'msw/browser';
import { createMockHandlers } from './handlers.js';
import type { MockOptions } from './core.js';

export function createMockWorker(opts: MockOptions = {}) {
  const h = createMockHandlers(opts);
  const worker = setupWorker(...h.handlers);
  return Object.assign(worker, { mock: h, resetStore: h.reset });
}
export { createMockHandlers } from './handlers.js';
export type { MockOptions } from './core.js';
