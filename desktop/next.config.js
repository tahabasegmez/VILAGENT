/**
 * The operator UI is a static export served by the VILAGENT gateway (and by
 * `next dev` during development), so there is no Next.js server at runtime.
 * @type {import("next").NextConfig}
 */
const config = {
  output: "export",
  devIndicators: false,
};

export default config;
