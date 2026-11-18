# @frappe-saaskit/mock

An in-memory imitation of the saas_core v1 API, for tests, demos and front-end work without a backend.

> Alpha. Not published to npm yet. See the [repo README](../../README.md) for setup.

Seed data: logged-in user `demo@example.com`, plans `free` / `pro` / `business`, tenant `acme`, plus jobs, invoices and backups. Provisioning jobs advance every `stepMs` (default 800 ms) and emit SSE events.

```ts
import { createClient } from '@frappe-saaskit/client';
import { createMockFetch } from '@frappe-saaskit/mock';

const fetch = createMockFetch({ stepMs: 0 });   // plain fetch function, no MSW
const client = createClient({ baseUrl: 'http://localhost', auth: { type: 'cookie' }, fetch });
fetch.reset();                                  // restore the seed data
```

With MSW:

```ts
import { createMockServer } from '@frappe-saaskit/mock/node';     // Node (msw/node); the package root is browser-safe
const server = createMockServer(); server.listen();

import { createMockWorker } from '@frappe-saaskit/mock/browser';  // browser (msw/browser)
await createMockWorker().start();
```

Options (`MockOptions`): `loggedIn`, `stepMs`, `pingMs`, `baseUrl`, `forceUnauthenticated`, `publicSignup`, `signupMode` (`open` | `disabled` | `allowlist` | `invite`), `signupAllowlist`, `signupInvites`, `paymentBanner`, `admin`, `capacityExhausted`.

The mock follows the contract, but it is not a contract test. Always test against a real saas_core site before you ship.
