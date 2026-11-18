// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderHook, act, cleanup } from '@testing-library/react';
import { QueryClient } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { SaasProvider, useRazorpayCheckout, PaymentDismissedError } from './index';

afterEach(() => { cleanup(); delete (window as any).Razorpay; });

const session: any = { gateway: 'razorpay', session_id: 's', client_payload: { provider: 'razorpay', key: 'rzp_test_x', mode: 'subscription', subscription_id: 'sub_1', name: 'N', prefill: { email: 'a@b.c' }, notes: {} } };

function setup() {
  const verifyPayment = vi.fn(async () => ({ status: 'ok', tenant: 't1' }));
  const client: any = { checkout: { verifyPayment } };
  const qc = new QueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => <SaasProvider client={client} queryClient={qc}>{children}</SaasProvider>;
  const inst: any = { handlers: {} as Record<string, any> };
  (window as any).Razorpay = function (this: any, opts: any) {
    inst.opts = opts;
    this.on = (ev: string, cb: any) => { inst.handlers[ev] = cb; };
    this.open = () => { inst.opened = true; };
  };
  return { verifyPayment, wrapper, inst };
}

describe('useRazorpayCheckout', () => {
  it('opens modal with options and verifies on handler', async () => {
    const { verifyPayment, wrapper, inst } = setup();
    const { result } = renderHook(() => useRazorpayCheckout(), { wrapper });
    let p!: Promise<any>;
    await act(async () => { p = result.current.open(session); await Promise.resolve(); });
    expect(inst.opened).toBe(true);
    expect(inst.opts).toMatchObject({ key: 'rzp_test_x', subscription_id: 'sub_1', prefill: { email: 'a@b.c' } });
    const resp = { razorpay_payment_id: 'pay', razorpay_signature: 'sig', razorpay_subscription_id: 'sub_1' };
    await act(async () => { inst.opts.handler(resp); });
    await expect(p).resolves.toEqual({ status: 'ok', tenant: 't1' });
    expect(verifyPayment).toHaveBeenCalledWith('razorpay', resp);
  });

  it('rejects dismissed', async () => {
    const { wrapper, inst } = setup();
    const { result } = renderHook(() => useRazorpayCheckout(), { wrapper });
    let p!: Promise<any>;
    await act(async () => { p = result.current.open(session); p.catch(() => {}); await Promise.resolve(); });
    await act(async () => { inst.opts.modal.ondismiss(); });
    await expect(p).rejects.toThrow('dismissed');
    expect(result.current.error?.message).toBe('dismissed');
  });

  it('payment.failed does not reject: a later success resolves, onPaymentFailed fires', async () => {
    const { wrapper, inst } = setup();
    const onPaymentFailed = vi.fn();
    const { result } = renderHook(() => useRazorpayCheckout({ onPaymentFailed }), { wrapper });
    const order = { ...session, client_payload: { provider: 'razorpay', key: 'k', mode: 'order', order_id: 'o1', amount: 500, currency: 'INR' } };
    let p!: Promise<any>;
    await act(async () => { p = result.current.open(order); await Promise.resolve(); });
    expect(inst.opts).toMatchObject({ order_id: 'o1', amount: 500, currency: 'INR' });
    await act(async () => { inst.handlers['payment.failed']({ error: { description: 'Card declined' } }); });
    expect(onPaymentFailed).toHaveBeenCalledWith(expect.objectContaining({ message: 'Card declined' }));
    expect(result.current.lastFailure?.message).toBe('Card declined');
    await act(async () => { inst.opts.handler({ razorpay_payment_id: 'pay', razorpay_order_id: 'o1', razorpay_signature: 's' }); });
    await expect(p).resolves.toEqual({ status: 'ok', tenant: 't1' });
  });

  it('failed then dismissed rejects with PaymentDismissedError carrying lastFailure', async () => {
    const { wrapper, inst } = setup();
    const { result } = renderHook(() => useRazorpayCheckout(), { wrapper });
    let p!: Promise<any>;
    await act(async () => { p = result.current.open(session); p.catch(() => {}); await Promise.resolve(); });
    await act(async () => { inst.handlers['payment.failed']({ error: { description: 'Card declined' } }); });
    await act(async () => { inst.opts.modal.ondismiss(); });
    const e = await p.catch((x) => x);
    expect(e).toBeInstanceOf(PaymentDismissedError);
    expect(e.lastFailure.message).toBe('Card declined');
  });
});
