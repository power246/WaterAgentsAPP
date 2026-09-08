import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    // Development-time proxy: Forward /ws and /api requests to the backend to avoid cross-domain issues
    proxy: {
      // WebSocket Proxy
      "/ws": {
        target: "ws://localhost:8000",
        ws: true,
        changeOrigin: true,
      },
      // REST API proxy (if direct calls to interfaces like /analyze are required in the future)
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});