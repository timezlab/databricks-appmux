/**
 * The one entry of the admin API client for pages and hooks: one function per
 * route of the contract (specs/001-appmux-v1/contracts/admin-api.md). Every
 * response is parsed with the Zod schema of `./schemas` before a page sees it,
 * and every failure becomes an ApiError, so a page handles one error type.
 *
 * Pure barrel: it names what the surface is, and keeps the transport helpers of
 * routes.ts and client.ts out of it.
 */
export { API_BASE_URL, API_ROUTES } from "./routes";
export type { ApiRoute } from "./routes";
export type { RequestOptions } from "./client";
export {
  ApiError,
  HTTP_FORBIDDEN,
  INVALID_REQUEST,
  INVALID_RESPONSE,
  NETWORK_ERROR,
  httpStatusCode,
} from "./errors";
export type { ApiErrorCode } from "./errors";
export * from "./launcher";
export * from "./apps";
export * from "./releases";
export * from "./config";
export * from "./grants";
export * from "./requests";
export * from "./delegations";
export * from "./egress";
export * from "./platform";
export * from "./metrics";
export * from "./principals";
export * from "./workspace";
export * from "./logs";
export * from "./environment";
export * from "./resource-catalogue";
export * from "./schemas";
