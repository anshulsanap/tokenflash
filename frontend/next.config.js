/** @type {import('next').NextConfig} */
const nextConfig = {
  // Strict Mode double-invokes effects/renders in development, which caused the
  // streaming handler to fire twice and duplicate every chat message. Disable
  // it so the dev demo behaves like production.
  reactStrictMode: false,
};

module.exports = nextConfig;
