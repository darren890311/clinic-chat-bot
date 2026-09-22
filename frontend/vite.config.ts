import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// The production build is served by FastAPI out of backend/static, so the app
// and the API share an origin and there is no CORS configuration in production.
export default defineConfig({
  plugins: [vue()],
  build: { outDir: '../backend/static', emptyOutDir: true },
  server: {
    port: 5173,
    proxy: { '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true } },
  },
})
