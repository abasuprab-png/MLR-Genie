import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/jobs": "http://localhost:8000",
      "/health": "http://localhost:8000",
      "/evals": "http://localhost:8000"
    }
  },
  build: {
    outDir: "dist",
    emptyOutDir: true
  },
  test: {
    globals: true,
    environment: "node"
  }
});
