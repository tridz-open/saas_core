import { createContext, useContext, useMemo, type ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { SaasClient } from '@frappe-saaskit/client';

const Ctx = createContext<SaasClient | null>(null);

export interface SaasProviderProps { client: SaasClient; queryClient?: QueryClient; children?: ReactNode; }

export function SaasProvider({ client, queryClient, children }: SaasProviderProps) {
  const qc = useMemo(() => queryClient ?? new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 5_000 } } }), [queryClient]);
  return (
    <Ctx.Provider value={client}>
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    </Ctx.Provider>
  );
}

export function useSaasClient(): SaasClient {
  const c = useContext(Ctx);
  if (!c) throw new Error('useSaasClient must be used inside <SaasProvider>');
  return c;
}
