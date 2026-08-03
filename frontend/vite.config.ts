import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build output goes to dist/, served by FastAPI in production.
// In dev, /api is proxied to the local uvicorn process.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist" },
  server: {
    proxy: {
      // 127.0.0.1, not localhost: localhost resolves to ::1 first on macOS,
      // but uvicorn binds IPv4 — the IPv6 attempt fails noisily before falling back.
      "/api": "http://127.0.0.1:8000",
    },
  },
});
