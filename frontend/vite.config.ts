import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, proxy /api to the FastAPI backend (ontology-qa-web, port 8000) so the
// browser makes same-origin requests and no CORS config is needed on the server.
// Override the target with VITE_API_TARGET if the backend runs elsewhere.
const API_TARGET = process.env.VITE_API_TARGET || "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: API_TARGET, changeOrigin: true },
    },
  },
  // Emit the production build into the Python package so the backend can serve
  // it as static files later if desired.
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
