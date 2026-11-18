import { describe, it, expect, vi } from 'vitest';
import { createClient } from '../src/index';

const P = 'http://x/api/method/saas_core.api.v1.';
const ok = (payload: unknown) => new Response(JSON.stringify({ message: payload }), { status: 200 });
const csrfFail = () => new Response(JSON.stringify({ exc_type: 'CSRFTokenError' }), { status: 400 });
const mk = (f: any, extra: object = {}) =>
  createClient({ baseUrl: 'http://x', fetch: f, auth: { type: 'cookie' }, retry: { retries: 0 }, ...extra });
const urls = (f: any) => f.mock.calls.map((c: any[]) => c[0] as string);
const hdr = (f: any, i: number) => (f.mock.calls[i][1].headers as Headers).get('X-Frappe-CSRF-Token');

describe('CSRF bootstrap (cookie auth)', () => {
  it('fresh client: first POST triggers exactly one me call, then sends the header', async () => {
    const f = vi.fn(async (url: string) => (url.endsWith('me') ? ok({ email: 'a', csrf_token: 'T1' }) : ok({})));
    await mk(f).tenants.openLoginLink('t1');
    expect(urls(f)).toEqual([`${P}me`, `${P}tenants.open_login_link`]);
    expect(hdr(f, 1)).toBe('T1');
  });

  it('three concurrent POSTs share one me call', async () => {
    const f = vi.fn(async (url: string) => (url.endsWith('me') ? ok({ email: 'a', csrf_token: 'T1' }) : ok({})));
    const c = mk(f);
    await Promise.all([c.tenants.openLoginLink('a'), c.tenants.openLoginLink('b'), c.tenants.openLoginLink('c')]);
    expect(urls(f).filter((u: string) => u.endsWith('me'))).toHaveLength(1);
    expect(urls(f)).toHaveLength(4);
  });

  it('csrfToken option avoids the me call', async () => {
    const f = vi.fn(async () => ok({}));
    await mk(f, { csrfToken: 'OPT' }).tenants.openLoginLink('t1');
    expect(f).toHaveBeenCalledTimes(1);
    expect(hdr(f, 0)).toBe('OPT');
  });

  it('csrfToken may be an async function', async () => {
    const f = vi.fn(async () => ok({}));
    await mk(f, { csrfToken: async () => 'FN' }).tenants.openLoginLink('t1');
    expect(hdr(f, 0)).toBe('FN');
  });

  it('uses window.csrf_token (globalThis) when present', async () => {
    (globalThis as any).csrf_token = 'WIN';
    try {
      const f = vi.fn(async () => ok({}));
      await mk(f).tenants.openLoginLink('t1');
      expect(f).toHaveBeenCalledTimes(1);
      expect(hdr(f, 0)).toBe('WIN');
    } finally {
      delete (globalThis as any).csrf_token;
    }
  });

  it('a CSRF rejection refreshes once via me and replays the request', async () => {
    let n = 0;
    const f = vi.fn(async (url: string) => {
      if (url.endsWith('me')) return ok({ email: 'a', csrf_token: `T${++n}` });
      return f.mock.calls.filter((c: any[]) => !(c[0] as string).endsWith('me')).length === 1 ? csrfFail() : ok({ done: true });
    });
    const r = await mk(f).tenants.openLoginLink('t1');
    expect(r).toEqual({ done: true });
    expect(urls(f).map((u: string) => u.replace(P, ''))).toEqual(['me', 'tenants.open_login_link', 'me', 'tenants.open_login_link']);
    expect(hdr(f, 3)).toBe('T2');
  });

  it('only replays once', async () => {
    const f = vi.fn(async (url: string) => (url.endsWith('me') ? ok({ email: 'a', csrf_token: 'T' }) : csrfFail()));
    await expect(mk(f).tenants.openLoginLink('t1')).rejects.toMatchObject({ name: 'CsrfError' });
    expect(urls(f).filter((u: string) => !u.endsWith('me'))).toHaveLength(2);
  });

  it('GET and non-cookie auth never bootstrap', async () => {
    const f = vi.fn(async () => ok({ items: [] }));
    await mk(f).plans.list();
    await createClient({ baseUrl: 'http://x', fetch: f, auth: { type: 'apiKey', key: 'k:s' } }).tenants.openLoginLink('t');
    expect(f).toHaveBeenCalledTimes(2);
  });
});
