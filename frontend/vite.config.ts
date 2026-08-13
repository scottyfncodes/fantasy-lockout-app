import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The dev server proxies API and WebSocket traffic to the FastAPI backend, so
// the frontend talks to same-origin paths in both dev and production.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
      '/ws': { target: 'ws://localhost:8000', ws: true },
    },
  },
  build: { outDir: 'dist' },
});
