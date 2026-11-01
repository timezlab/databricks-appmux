// The base path is fixed at build time: the export is served under the
// reserved prefix of the edge (spike 19). `trailingSlash` makes every page a
// directory with an index.html, which the edge serves as a plain file.
const basePath = process.env.APPMUX_BASE_PATH ?? "/_appmux";

export default {
  output: "export",
  basePath,
  trailingSlash: true,
  images: { unoptimized: true },
  env: { NEXT_PUBLIC_BASE_PATH: basePath },
};
