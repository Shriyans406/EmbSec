import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The built dashboard is served by the backend at /ui/ ; in development (npm run dev, port 5173) it talks to the
// backend on port 8000 directly (CORS allows it).
export default defineConfig({
  plugins: [react()],
  base: './',
  server: { port: 5173, strictPort: true },
})
