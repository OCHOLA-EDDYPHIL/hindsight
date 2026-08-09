import path from "node:path";
import { fileURLToPath } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const root = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  root,
  publicDir: path.resolve(root, "public"),
  plugins: [
    react(),
    tailwindcss(),
    {
      name: "strip-built-asset-trailing-whitespace",
      generateBundle(_options, bundle) {
        for (const output of Object.values(bundle)) {
          if (output.type === "chunk") {
            output.code = output.code.replace(/[ \t]+$/gm, "");
          } else if (typeof output.source === "string") {
            output.source = output.source.replace(/[ \t]+$/gm, "");
          }
        }
      },
    },
  ],
  resolve: {
    alias: {
      "@": path.resolve(root, "src"),
    },
  },
  build: {
    outDir: path.resolve(root, "../src/hindsight/web"),
    emptyOutDir: true,
    cssCodeSplit: false,
    sourcemap: false,
    rollupOptions: {
      output: {
        entryFileNames: "assets/app.js",
        chunkFileNames: "assets/chunk-[name].js",
        assetFileNames: (assetInfo) =>
          assetInfo.name?.endsWith(".css")
            ? "assets/styles.css"
            : "assets/[name][extname]",
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: [path.resolve(root, "src/test/setup.ts")],
    css: true,
  },
});
