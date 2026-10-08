import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development the API runs separately (`uv run raac serve`, or RAAC_API=<url>); the UI reaches it only through /api.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": process.env.RAAC_API ?? "http://127.0.0.1:8000" } },
});
