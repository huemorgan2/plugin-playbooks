import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/postcss'

// Served by the plugin at /api/p/plugin-playbooks/ui/ — assets must resolve
// relative to that path (and to any reverse-proxy base), hence base './'.
export default defineConfig({
  plugins: [react()],
  base: './',
  css: {
    postcss: {
      plugins: [tailwindcss()],
    },
  },
  build: {
    outDir: '../ui',
    emptyOutDir: true,
    sourcemap: false,
  },
})
