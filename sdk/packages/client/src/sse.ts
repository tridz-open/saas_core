import type { Http } from './http';
import { backoff, sleep } from './http';
import type { RetryOptions, Subscription } from './client';
import type { SaasEvent, Job, Tenant } from './types';
import { ExperimentalDisabledError } from './errors';
import { TERMINAL_JOB_STATES, STABLE_TENANT_STATUSES } from './types';

/** Incremental text/event-stream parser. */
export function createSseParser(onMessage: (m: { event: string; data: string; id?: string }) => void) {
  let buf = '';
  let event = '', data: string[] = [], id: string | undefined;
  const line = (l: string) => {
    if (l === '') {
      if (data.length) onMessage({ event: event || 'message', data: data.join('\n'), id });
      event = ''; data = [];
      return;
    }
    if (l.startsWith(':')) return;
    const i = l.indexOf(':');
    const f = i < 0 ? l : l.slice(0, i);
    let v = i < 0 ? '' : l.slice(i + 1);
    if (v.startsWith(' ')) v = v.slice(1);
    if (f === 'event') event = v; else if (f === 'data') data.push(v); else if (f === 'id') id = v;
  };
  return (chunk: string) => {
    buf += chunk;
    const parts = buf.split(/\r\n|\n|\r/);
    buf = parts.pop() ?? '';
    parts.forEach(line);
  };
}

export function subscribe(
  http: Http,
  target: { jobId: string } | { tenantId: string },
  onEvent: (e: SaasEvent) => void,
  onError: ((e: unknown) => void) | undefined,
  cfg: { retry?: RetryOptions; pollIntervalMs?: number; maxReconnects?: number },
): Subscription {
  const ctl = new AbortController();
  const isJob = 'jobId' in target;
  const query: Record<string, string> = isJob ? { job: target.jobId } : { tenant: (target as { tenantId: string }).tenantId };
  const maxReconnects = cfg.maxReconnects ?? 3;
  const interval = cfg.pollIntervalMs ?? 2000;
  let closed = false;

  const terminal = (e: SaasEvent) =>
    (e.type === 'job.updated' && TERMINAL_JOB_STATES.includes(e.data.state)) ||
    (!isJob && e.type === 'tenant.updated' && STABLE_TENANT_STATUSES.includes(e.data.status)); // tenant.updated: polling fallback only
  let lastId: string | undefined;
  const emit = (e: SaasEvent) => { if (!closed) onEvent(e); return terminal(e); };

  async function readStream(): Promise<'done' | 'ended'> {
    const res = await http.stream('events.stream', query, ctl.signal, lastId);
    if (!res.body) throw new Error('streaming not supported');
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let finished = false;
    const parse = createSseParser((m) => {
      if (m.id) lastId = m.id;
      let payload: unknown;
      try { payload = JSON.parse(m.data); } catch { return; }
      const ev = payload as SaasEvent;
      if (!ev || typeof ev !== 'object') return;
      if (!ev.type) (ev as { type: string }).type = m.event;
      if (emit(ev)) finished = true;
    });
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      parse(dec.decode(value, { stream: true }));
      if (finished) { await reader.cancel().catch(() => {}); return 'done'; }
    }
    return 'ended';
  }

  async function poll(): Promise<void> {
    while (!closed) {
      try {
        const at = new Date().toISOString();
        const ev: SaasEvent = isJob
          ? await http.request<Job>('GET', 'jobs.get', { query: { job: target.jobId } }).then((j) => ({ seq: 0, type: 'job.updated' as const, at, job: target.jobId, data: j }))
          : await http.request<Tenant>('GET', 'tenants.get', { query: { tenant: (target as { tenantId: string }).tenantId } }).then((t) => ({ seq: 0, type: 'tenant.updated' as const, at, tenant: (target as { tenantId: string }).tenantId, data: t }));
        if (emit(ev)) return;
      } catch (e) {
        if (closed) return;
        onError?.(e);
      }
      await sleep(interval);
    }
  }

  (async () => {
    let failures = 0;
    while (!closed && failures <= maxReconnects) {
      try {
        if ((await readStream()) === 'done') return;
        failures = 0; // clean end of stream: reconnect
        await sleep(backoff(0, cfg.retry));
      } catch (e) {
        if (closed) return;
        onError?.(e);
        if (e instanceof ExperimentalDisabledError) break; // SSE is off on this site: go straight to polling
        failures++;
        if (failures <= maxReconnects) await sleep(backoff(failures - 1, cfg.retry));
      }
    }
    if (!closed) await poll();
  })();

  return { close() { closed = true; ctl.abort(); } };
}
