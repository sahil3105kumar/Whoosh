import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In dev, the Python signaling server runs separately on :8000
// (`uv run uvicorn apps.server.main:app`); proxy its routes so the app
// is same-origin, as it is in production.
const server = "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/rooms": { target: server, ws: true },
      "/config": server,
      "/healthz": server,
    },
  },
});
