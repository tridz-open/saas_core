import { createClient } from '@frappe-saaskit/client';
import type { ClientOptions, SaasClient } from '@frappe-saaskit/client';
import { readFileSync, statSync } from 'node:fs';

export interface NodeClientOptions extends Omit<ClientOptions, 'auth'> {
  /** Frappe API credentials as `key:secret` (User > API Access). Sent as `Authorization: token key:secret`. */
  token: string;
}

let noticed = false;
function experimentalNotice(): void {
  if (noticed) return;
  noticed = true;
  console.warn('[frappe-saaskit] @frappe-saaskit/node is EXPERIMENTAL: it has no consumers and API-key auth has not been verified against a live saas_core.');
}

/**
 * EXPERIMENTAL. Client for agents/CLIs using Frappe API credentials. SSE in the client uses fetch (not EventSource),
 * so subscribe() works on Node 20+ with no polyfill.
 */
export function createNodeClient({ token, ...rest }: NodeClientOptions): SaasClient {
  experimentalNotice();
  if (!token) throw new Error('createNodeClient: token is required');
  return createClient({ ...rest, auth: { type: 'apiKey', key: token } });
}

export function tokenFromEnv(name = 'SAASKIT_TOKEN', env: Record<string, string | undefined> = process.env): string {
  const v = env[name]?.trim();
  if (!v) throw new Error(`Environment variable ${name} is not set`);
  return v;
}

export interface LoadTokenOptions { warn?: (msg: string) => void; }
/** Reads a token file (trimmed). Warns (does not throw) when group/world readable on POSIX. */
export function loadTokenFile(path: string, opts: LoadTokenOptions = {}): string {
  const warn = opts.warn ?? ((m: string) => console.warn(m));
  const st = statSync(path);
  if (process.platform !== 'win32' && (st.mode & 0o077) !== 0) {
    warn(`Token file ${path} is accessible by group/others (mode ${(st.mode & 0o777).toString(8)}); run: chmod 600 ${path}`);
  }
  const t = readFileSync(path, 'utf8').trim();
  if (!t) throw new Error(`Token file ${path} is empty`);
  return t;
}
