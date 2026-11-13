import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const proxy = {
    "/api": {
      target: env.NOTE_AGENT_API_TARGET || "http://127.0.0.1:8000",
      changeOrigin: true,
    },
  };
  return { plugins: [react()], server: { proxy }, preview: { proxy } };
});
