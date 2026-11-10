import type { NextConfig } from "next";

const configuredBasePath = process.env.NEXT_PUBLIC_BASE_PATH ?? "/mahad";
const basePath = configuredBasePath === "/" ? "" : configuredBasePath;

if (
  basePath &&
  (!basePath.startsWith("/") ||
    basePath.endsWith("/") ||
    basePath
      .slice(1)
      .split("/")
      .some(
        (segment) =>
          !/^[A-Za-z0-9._~-]+$/.test(segment) ||
          segment === "." ||
          segment === "..",
      ))
) {
  throw new Error(
    "NEXT_PUBLIC_BASE_PATH must be empty or an absolute path without a trailing slash.",
  );
}

const nextConfig: NextConfig = {
  output: "export",
  trailingSlash: true,
  basePath,
  images: {
    unoptimized: true,
  },
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
