import { describe, it, expect, vi } from 'vitest';
import { createClient, ServerError, RateLimitError } from '../src/index';

const json = (payload: unknown, status = 200, h: Record<string, string> = {}) =>
  new Response(JSON.stringify({ message: payload }), { status, headers: h });
const fail = (status: number, h: Record<string, string> = {}) =>
  new Response(JSON.stringify({ error: { code: 'x', message: 'm' } }), { status, headers: h });
const mk = (f: any, extra: object = {}) =>
  createClient({ baseUrl: 'http://x', fetch: f, retry: { retries: 3, baseDelayMs: 1, maxDelayMs: 2 }, ...extra });
const key = (f: any, i: number) => (f.mock.calls[i][1].headers as Headers).get('Idempotency-Key');

describe('retry policy', () => {
  it('auth.login is never retried on 503', async () => {
    const f = vi.fn(async () => fail(503));
    await expect(mk(f).auth.login({ email: 'a', password: 'p' } as any)).rejects.toBeInstanceOf(ServerError);
    expect(f).toHaveBeenCalledTimes(1);
    expect(key(f, 0)).toBeNull();
  });

  it('admin.tenants.create is never retried on 502', async () => {
    const f = vi.fn(async () => fail(502));
    await expect(mk(f).admin.tenants.create({} as any)).rejects.toBeInstanceOf(ServerError);
    expect(f).toHaveBeenCalledTimes(1);
  });

  it('tenants.create retries with the same Idempotency-Key', async () => {
    const f = vi.fn().mockResolvedValueOnce(fail(503)).mockResolvedValueOnce(json({ name: 't' }));
    await mk(f).tenants.create({} as any);
    expect(f).toHaveBeenCalledTimes(2);
    expect(key(f, 0)).toBeTruthy();
    expect(key(f, 0)).toBe(key(f, 1));
  });

  it('GET retries on 500', async () => {
    const f = vi.fn().mockResolvedValueOnce(fail(500)).mockResolvedValueOnce(json({ items: [] }));
    await mk(f).plans.list();
    expect(f).toHaveBeenCalledTimes(2);
  });

  it('429 on a non-idempotent POST throws RateLimitError with retryAfterMs and does not retry', async () => {
    const f = vi.fn(async () => fail(429, { 'Retry-After': '2' }));
    const e = await mk(f).auth.login({ email: 'a', password: 'p' } as any).catch((x) => x);
    expect(e).toBeInstanceOf(RateLimitError);
    expect(e.retryAfterMs).toBe(2000);
    expect(f).toHaveBeenCalledTimes(1);
  });

  it('network errors on non-idempotent POST are not retried', async () => {
    const f = vi.fn(async () => {
      throw new TypeError('fetch failed');
    });
    await expect(mk(f).auth.logout()).rejects.toMatchObject({ name: 'NetworkError' });
    expect(f).toHaveBeenCalledTimes(1);
  });
});
