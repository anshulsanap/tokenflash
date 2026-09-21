# Known Issues

## Dependency vulnerabilities (frontend `npm audit`)

Recorded during Phase 4 (Private On-Device Artifacts), before building the
Sandpack live-preview sandbox. Captured here because the fixes are all
**major-version bumps** with real migration risk, and we chose to proceed with
the security-critical sandbox work first (the vulnerabilities below do NOT touch
the sandbox, CSP, iframe, or egress logic — see assessment).

### Critical

- **next `14.2.29`** (runtime, direct dependency) — two critical CVEs:
  - Unauthenticated Remote Code Execution on **Windows-hosted** servers
    ([GHSA-p293-qw3h-jr36](https://github.com/advisories/GHSA-p293-qw3h-jr36)).
  - Unauthenticated RCE in the **Image Optimization API** when AVIF files are
    used ([GHSA-2xp9-vwfh-vxw4](https://github.com/advisories/GHSA-2xp9-vwfh-vxw4)).
  - Both are **server-side**. This project runs locally on macOS (not a
    Windows host) and does not use the Image Optimization API for artifact
    previews. Fix requires `next@16` (major bump).

- **vitest `2.1.9`** (dev only) — when the **Vitest UI server** is listening, an
  arbitrary file can be read/executed
  ([GHSA-5xrq-8626-4rwp](https://github.com/advisories/GHSA-5xrq-8626-4rwp)).
  Our test setup uses `vitest run` (no `--ui`), so the UI server is never
  started. Fix requires `vitest@5` (major bump, would also move `vite`).

### High

- **next** (runtime, direct) — cluster of Denial-of-Service via React Server
  Components, SSRF (WebSocket upgrades, Server Actions on custom servers,
  rewrite destination hostname), and a Pages-Router middleware/proxy bypass with
  i18n. All **server-side request-handling** flaws; none reach the browser
  bundle or the sandboxed iframe. Fixed by the same `next@16` bump.
- **postcss** (`8.4.31` under next / `8.5.6` direct) — dev/build-time only:
  arbitrary `.map` file read / path traversal via attacker-controlled
  `sourceMappingURL` in CSS comments
  ([GHSA-6g55-p6wh-862q](https://github.com/advisories/GHSA-6g55-p6wh-862q),
  [GHSA-r28c-9q8g-f849](https://github.com/advisories/GHSA-r28c-9q8g-f849)).
- **vite** (dev only, via vitest) — `server.fs.deny` bypass on Windows alternate
  paths ([GHSA-fx2h-pf6j-xcff](https://github.com/advisories/GHSA-fx2h-pf6j-xcff)).
- **glob** (dev only, via eslint-config-next) — glob **CLI** command injection
  via `-c/--cmd` ([GHSA-5j98-mcp5-4vw2](https://github.com/advisories/GHSA-5j98-mcp5-4vw2));
  only affects CLI use, not library use.
- **jsondiffpatch** (runtime tree, via `ai` → `@ai-sdk/vue` → vue) — prototype
  pollution in patch APIs
  ([GHSA-j4fx-xxwh-2485](https://github.com/advisories/GHSA-j4fx-xxwh-2485)).
  This app uses the React path, so the Vue adapter code is not exercised.
- **eslint-config-next / @next/eslint-plugin-next** (dev only, lint) —
  transitively pull the vulnerable glob.

### Assessment vs the Sandpack / iframe / CSP work

None of the above touch the Phase 4 security boundary:
- The sandbox guarantee rests on browser primitives — an opaque-origin
  `sandbox` iframe (no `allow-same-origin`) plus a restrictive CSP
  (`default-src`/`connect-src`/`form-action`/`frame-ancestors`/`base-uri` all
  `'none'`) and an in-sandbox `postMessage` egress listener. These are
  client-side and independent of `next`, `postcss`, `vite`, `glob`, `vitest`,
  and `jsondiffpatch`.
- The `next` CVEs are server-runtime request-handling / Windows-host / image-API
  issues that do not run in the browser bundle or the sandboxed iframe.
- The dev/build-time vulns (vitest/vite/glob/postcss/eslint-config-next) are not
  shipped in the runtime bundle and are not exercised by our `vitest run` setup.

### Remediation plan

- **Deferred (deliberate):** upgrade `next@14 → 16` as a separate, focused task
  after Phase 4. It is a major bump likely to ripple through `app/page.tsx` and
  the streaming code, so it should not be folded into feature work. Live-exploit
  surface for this local-only, macOS, $0-API dev tool is low.
- **Deferred:** `vitest@2 → 5` (dev only) alongside or after the `next` bump.
- Re-run `npm audit` after each upgrade and update this file.
