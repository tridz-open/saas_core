// Typechecked copy of the README "Client" quickstart. Run `pnpm typecheck:examples`.
import {
  createClient, SaasError, ValidationError, ConflictError, ForbiddenError, ExperimentalDisabledError, RateLimitError,
} from '@frappe-saaskit/client';

export async function main() {
  const client = createClient({ baseUrl: 'https://app.example.com', auth: { type: 'cookie' } });

  // Session: reuse an existing one, otherwise log in (the client bootstraps the CSRF token for you).
  const me = await client.me.get().catch(() => client.auth.login({ email: 'me@example.com', password: '...' }));
  console.log(me.email);

  const plans = await client.plans.list();
  const slug = await client.tenants.checkSlug('acme'); // { available, reason?, suggestion? }
  if (!slug.available) console.log(slug.reason, slug.suggestion);

  try {
    const { tenant, job } = await client.tenants.create({ slug: 'acme', plan: plans[0]!.code });
    console.log(tenant.name);
    // Live provisioning progress (SSE, falls back to polling when SSE is off on the site)
    const sub = client.subscribe({ jobId: job.name }, (e) => console.log(e.type, e.data));
    // later: sub.close()
    sub.close();
  } catch (e) {
    if (e instanceof ValidationError) console.log(e.fieldErrors); // 400/417/422
    else if (e instanceof ConflictError) console.log(e.message); // 409, for example slug taken
    else if (e instanceof RateLimitError) console.log(e.retryAfterMs);
    else if (e instanceof SaasError) console.log(e.status, e.code, e.requestId);
    else throw e;
  }
}

/** Billing: preview, then cancel. This replaces the removed subscription.cancel. */
export async function cancelFlow(tenantId: string) {
  const client = createClient({ baseUrl: '', auth: { type: 'cookie' } });
  const preview = await client.billing.previewCancel(tenantId);
  if (!preview.can_cancel) return preview.blocked_reason;
  const result = await client.billing.cancel(tenantId, { reason: 'switching tools' });
  return result.refund_minor;
}

/** Experimental namespaces are 403 `experimental_disabled` unless the site enables them. */
export async function experimental() {
  const client = createClient({ baseUrl: '', auth: { type: 'cookie' } });
  try {
    return await client.apiTokens.list();
  } catch (e) {
    if (e instanceof ExperimentalDisabledError) return []; // feature flag is off on this site
    if (e instanceof ForbiddenError) throw e; // a normal permission error
    throw e;
  }
}
