import { describe, it, expect, vi } from 'vitest';
import { mkdtempSync, writeFileSync, chmodSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const createClient = vi.fn((o: unknown) => ({ opts: o }));
vi.mock('@frappe-saaskit/client', () => ({ createClient: (o: unknown) => createClient(o) }));
import { createNodeClient, tokenFromEnv, loadTokenFile } from './index';

describe('node helpers', () => {
  it('createNodeClient wires apiKey (token key:secret) auth by default', () => {
    const c = createNodeClient({ baseUrl: 'http://x', token: 'k:s' }) as any;
    expect(c.opts.baseUrl).toBe('http://x'); expect(c.opts.auth).toEqual({ type: 'apiKey', key: 'k:s' });
    expect(() => createNodeClient({ baseUrl: 'x', token: '' })).toThrow();
  });
  it('prints the experimental notice once', () => {
    const w = vi.spyOn(console, 'warn').mockImplementation(() => {});
    createNodeClient({ baseUrl: 'http://x', token: 'k:s' });
    createNodeClient({ baseUrl: 'http://x', token: 'k:s' });
    expect(w.mock.calls.filter((c) => String(c[0]).includes('EXPERIMENTAL')).length).toBeLessThanOrEqual(1);
    w.mockRestore();
  });
  it('tokenFromEnv', () => {
    expect(tokenFromEnv('A', { A: ' tok \n' })).toBe('tok');
    expect(() => tokenFromEnv('B', {})).toThrow(/B/);
    process.env.SAASKIT_TOKEN = 'def'; expect(tokenFromEnv()).toBe('def');
  });
  it('loadTokenFile reads, trims, warns on loose perms', () => {
    const d = mkdtempSync(join(tmpdir(), 'tok-')); const f = join(d, 't');
    writeFileSync(f, 'secret\n'); chmodSync(f, 0o644);
    const warn = vi.fn(); expect(loadTokenFile(f, { warn })).toBe('secret'); expect(warn).toHaveBeenCalledOnce();
    chmodSync(f, 0o600); const w2 = vi.fn(); loadTokenFile(f, { warn: w2 }); expect(w2).not.toHaveBeenCalled();
    writeFileSync(f, ' \n'); chmodSync(f, 0o600); expect(() => loadTokenFile(f)).toThrow(/empty/);
  });
});
