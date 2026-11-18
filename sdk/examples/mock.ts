// Typechecked copy of the README "Mock" snippet. Run `pnpm typecheck:examples`.
import { createClient } from '@frappe-saaskit/client';
import { createMockFetch } from '@frappe-saaskit/mock';

export async function demo() {
  const fetch = createMockFetch({ stepMs: 0 }); // no network, no MSW
  const client = createClient({ baseUrl: 'http://localhost', auth: { type: 'cookie' }, fetch });

  await client.me.get(); // logged in by default
  const { job } = await client.tenants.create({ slug: 'newco', plan: 'pro' });
  console.log(job.name);
  fetch.reset(); // back to the seed data between tests

  // Experimental routes behave like a default saas_core site: 403 experimental_disabled. Opt in per flag:
  const withTokens = createMockFetch({ experimental: ['api_tokens'] });
  return createClient({ baseUrl: 'http://localhost', fetch: withTokens }).apiTokens.list();
}
