import { defineConfig } from 'tsup';
export default defineConfig({
  entry: { index: 'src/index.ts', browser: 'src/browser.ts', node: 'src/node.ts' },
  format: ['esm'], dts: true, clean: true, external: ['msw'],
});
