// Opt-in: runs against a real saas_core site when SAASCORE_URL is set; skipped otherwise.
import { describe, it, expect } from 'vitest';
import { createClient } from '../../packages/client/src/index';

const url = process.env.SAASCORE_URL;

describe.skipIf(!url)('contract against a real saas_core', () => {
  const client = () => createClient({ baseUrl: url!, auth: { type: 'cookie' } });

  it('plans.list returns plans', async () => {
    expect(Array.isArray(await client().plans.list())).toBe(true);
  });

  it('config.get returns public config', async () => {
    expect(await client().config.get()).toBeTypeOf('object');
  });

  it('tenants.checkSlug answers for a random slug', async () => {
    const r = await client().tenants.checkSlug(`zz${Date.now().toString(36)}`);
    expect(r).toHaveProperty('available');
  });

  it('auth.signup then me.get works with CSRF bootstrap', async () => {
    const c = client();
    const email = `sdk-contract-${Date.now()}@example.com`;
    const me = await c.auth.signup({ email, password: 'Sdk-Contract-1!', full_name: 'SDK Contract' } as never);
    expect(me.email).toBe(email);
    expect((await c.me.get()).email).toBe(email);
  });
});
