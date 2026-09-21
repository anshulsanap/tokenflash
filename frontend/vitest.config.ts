import { defineConfig } from "vitest/config";

// Minimal, dev-only test config. The artifact parser is pure (no React, no DOM),
// so a plain `node` environment is sufficient — we deliberately avoid pulling in
// jsdom/testing-library here (design §2, keep the parser unit-testable in isolation).
export default defineConfig({
  // The security check (components/**) renders <ArtifactPreview> via
  // react-dom/server. The component uses the automatic JSX runtime (no explicit
  // `import React`), matching Next.js/SWC, so tell esbuild to transform JSX with
  // the automatic runtime too. This affects transform only; the parser tests are
  // plain TS and are unaffected.
  esbuild: {
    jsx: "automatic",
  },
  test: {
    // Default env stays `node` so the pure parser tests (lib/**) keep running
    // without a DOM. The focused security check under components/** needs a DOM
    // (it exercises the in-sandbox bootstrap counters), so that ONE file opts
    // into jsdom via a per-file `// @vitest-environment jsdom` directive — the
    // suite as a whole is NOT converted to jsdom.
    environment: "node",
    include: ["lib/**/*.test.ts", "components/**/*.test.ts", "components/**/*.test.tsx"],
  },
});
