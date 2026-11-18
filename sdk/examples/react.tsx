// Typechecked copy of the README "React" quickstart. Run `pnpm typecheck:examples`.
import { createClient } from '@frappe-saaskit/client';
import { SaasProvider, useMe, usePlans, useCheckout, useRazorpayCheckout } from '@frappe-saaskit/react';

const client = createClient({ baseUrl: '', auth: { type: 'cookie' } }); // same origin as the saas_core site

function Pricing() {
  useMe(); // loads the session and the CSRF token needed for the mutations below
  const plans = usePlans();
  const checkout = useCheckout();
  const rzp = useRazorpayCheckout();

  const buy = async (plan: string) => {
    const session = await checkout.mutateAsync({
      plan, interval: 'month', slug: 'acme',
      success_url: location.origin + '/done', cancel_url: location.href,
    });
    if (session.client_payload) await rzp.open(session); // Razorpay Checkout.js, then verifyPayment
    else if (session.url) location.href = session.url; // hosted gateways
  };

  if (plans.isLoading) return <p>Loading...</p>;
  return (
    <ul>
      {plans.data?.map((p) => (
        <li key={p.code}><button onClick={() => buy(p.code)}>{p.title ?? p.code}</button></li>
      ))}
    </ul>
  );
}

export const App = () => <SaasProvider client={client}><Pricing /></SaasProvider>;
