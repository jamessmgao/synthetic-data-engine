import type { NextConfig } from "next";

const config: NextConfig = {
  // A self-contained server (.next/standalone/server.js) for web/Dockerfile; `next dev` ignores it.
  output: "standalone",
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default config;
