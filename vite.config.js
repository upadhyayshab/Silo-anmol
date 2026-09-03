import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [
    tailwindcss(),
    react()
  ],
  server: {
    allowedHosts: [ "hamlet-probably-numerous.ngrok-free.dev" ]
  },
  test: {
    environment: 'jsdom',
    setupFiles: './tests/setupTests.js',
    include: ['tests/**/*.test.{js,jsx}'],
    globals: true,
  },
})