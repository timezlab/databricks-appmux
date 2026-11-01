// The base path is fixed at build time: the export is served under the reserved prefix of the edge.
const basePath = process.env.APPMUX_BASE_PATH ?? "/_appmux";

export default {
  output: "export",
  basePath,
  trailingSlash: true,
  images: { unoptimized: true },
  env: { NEXT_PUBLIC_BASE_PATH: basePath },
};
