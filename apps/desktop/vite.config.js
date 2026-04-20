import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [svelte()],
  clearScreen: false,
  // Electron loads the built index.html via file://, where absolute "/"
  // paths resolve to the filesystem root, not the app.asar root. Emit
  // relative asset URLs ("./assets/…") so they stay inside the asar.
  base: "./",
  server: {
    port: 1420,
    strictPort: true,
    host: "127.0.0.1",
  },
  build: {
    target: "esnext",
    sourcemap: false,
  },
});
