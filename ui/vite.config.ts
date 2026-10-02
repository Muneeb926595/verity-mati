import { defineConfig } from "vite";
import solid from "vite-plugin-solid";

// The engine serves dist/index.html at "/" and dist/assets/* at "/assets/*" (see docs/ENGINE_API.md).
// Everything must be inside dist/assets — nothing else under dist/ is served — and nothing may load from the network.
export default defineConfig({
  base: "/",
  plugins: [solid()],
  publicDir: false,
  build: {
    outDir: "dist",
    assetsDir: "assets",
    emptyOutDir: true,
    target: "es2022",
    cssCodeSplit: false,
    modulePreload: { polyfill: false },
    assetsInlineLimit: 8192,
    reportCompressedSize: true,
  },
  server: {
    // `npm run dev` talks to an engine started with --port 8765 (see README)
    proxy: {
      "/rpc": "http://127.0.0.1:8765",
      "/events": { target: "http://127.0.0.1:8765", ws: false },
    },
  },
});
