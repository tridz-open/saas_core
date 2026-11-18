import { describe, it, expect, vi } from 'vitest';
import {
  createClient, SaasError, ForbiddenError, ValidationError, NotFoundError, AuthError, CsrfError,
} from '../src/index';

const mk = (f: any) => createClient({ baseUrl: 'http://x', fetch: f, retry: { retries: 0 } });
const native = (status: number, body: object) => new Response(JSON.stringify(body), { status });

describe('non-JSON responses', () => {
  it('2xx HTML page becomes SaasError(non_json_response), never SyntaxError', async () => {
    const f = vi.fn(async () => new Response('<html><body>Login</body></html>', { status: 200 }));
    const e = await mk(f).me.get().catch((x) => x);
    expect(e).toBeInstanceOf(SaasError);
    expect(e).not.toBeInstanceOf(SyntaxError);
    expect(e.code).toBe('non_json_response');
    expect(e.detail).toContain('<html>');
  });

  it('error status with HTML body is a status-only SaasError', async () => {
    const f = vi.fn(async () => new Response('<html>Bad gateway</html>', { status: 502 }));
    const e = await mk(f).me.get().catch((x) => x);
    expect(e).toBeInstanceOf(SaasError);
    expect(e.status).toBe(502);
  });
});

describe('Frappe-native error envelopes', () => {
  const msgs = (m: string) => JSON.stringify([JSON.stringify({ message: m })]);
  const cases: [string, number, any, string][] = [
    ['frappe.exceptions.PermissionError', 403, ForbiddenError, 'Not permitted'],
    ['frappe.exceptions.ValidationError', 417, ValidationError, 'Bad <b>value</b>'],
    ['frappe.exceptions.MandatoryError', 417, ValidationError, 'Missing x'],
    ['frappe.exceptions.DoesNotExistError', 404, NotFoundError, 'Gone'],
    ['frappe.exceptions.AuthenticationError', 401, AuthError, 'Login'],
    ['frappe.exceptions.SessionExpired', 401, AuthError, 'Expired'],
  ];
  for (const [exc, status, C, message] of cases) {
    it(`${exc} -> ${C.name}`, async () => {
      const f = vi.fn(async () => native(status, { exc_type: exc.split('.').pop(), _server_messages: msgs(message) }));
      const e = await mk(f).me.get().catch((x) => x);
      expect(e).toBeInstanceOf(C);
      expect(e.message).toBe(message.replace(/<[^>]+>/g, ''));
    });
  }

  it('CSRFTokenError -> CsrfError (an AuthError)', async () => {
    const f = vi.fn(async () => native(400, { exc_type: 'CSRFTokenError', exception: 'frappe.exceptions.CSRFTokenError: Invalid Request' }));
    const e = await mk(f).me.get().catch((x) => x);
    expect(e).toBeInstanceOf(CsrfError);
    expect(e).toBeInstanceOf(AuthError);
    expect(e.message).toBe('Invalid Request');
  });

  it('417 without exc_type is a ValidationError with HTTP message', async () => {
    const f = vi.fn(async () => native(417, {}));
    const e = await mk(f).me.get().catch((x) => x);
    expect(e).toBeInstanceOf(ValidationError);
    expect(e.message).toBe('HTTP 417');
  });

  it('saas_core envelope still wins', async () => {
    const f = vi.fn(async () => native(403, { error: { code: 'signup_disabled', message: 'Closed' } }));
    const e = await mk(f).me.get().catch((x) => x);
    expect(e).toBeInstanceOf(ForbiddenError);
    expect(e.code).toBe('signup_disabled');
  });
});
