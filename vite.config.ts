import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Tauri drives this dev server, so the port is fixed and failures must be loud:
// silently moving to another port would leave the webview pointing at nothing.
export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    host: '127.0.0.1',
    watch: {
      // Rust and Python sources are rebuilt by their own toolchains; watching
      // them only causes needless frontend reloads.
      ignored: ['**/src-tauri/**', '**/backend/**'],
    },
  },
  build: {
    target: 'esnext',
    sourcemap: false,
  },
});
