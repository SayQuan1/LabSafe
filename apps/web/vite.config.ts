import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ mode }) => {
  const variables = { ...loadEnv(mode, process.cwd(), 'VITE_'), ...process.env }
  const environment = variables.VITE_APP_ENV
  if (!['dev', 'test'].includes(environment ?? '') || variables.APP_ENV !== environment) {
    throw new Error('I-01A requires matching APP_ENV/VITE_APP_ENV=dev or test; production is disabled')
  }
  return {
    plugins: [vue()],
    server: { host: '127.0.0.1', port: 5173 }
  }
})
