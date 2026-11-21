import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

// API FastAPI chạy ở scripts/run_api.py (mặc định 127.0.0.1:8000).
// Đổi bằng biến môi trường VITE_API_TARGET hoặc file .env.local trong web/.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const target = env.VITE_API_TARGET || 'http://127.0.0.1:8000'
  return {
    plugins: [vue()],
    server: {
      port: 5173,
      proxy: {
        '/api': { target, changeOrigin: true },
        '/media': { target, changeOrigin: true },
      },
    },
  }
})
