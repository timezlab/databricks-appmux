/** The one error type every failed call becomes. */
import type { ErrorBody } from "@/lib/api/schemas";

/** No answer arrived (the connection failed, or a session lapse redirected to a login page). */
export const NETWORK_ERROR = "network_error";

/** The server answered, but not with JSON, or with a body the response schema refuses. */
export const INVALID_RESPONSE = "invalid_response";

/** The client refused to send a call whose query the route does not take (a bug in the page, not a lost connection). */
export const INVALID_REQUEST = "invalid_request";

export const HTTP_FORBIDDEN = 403;

/** The code of a failure the server answered without an error body. */
export function httpStatusCode(status: number): `http_${number}` {
  return `http_${status}`;
}

/** The codes the client raises on its own. */
type ClientErrorCode =
  typeof NETWORK_ERROR | typeof INVALID_RESPONSE | `http_${number}`;

/**
 * Any code an `ApiError` carries: one the client raises, or the contract's short
 * `snake_case` word from the server's error body. The `Record<never, never>`
 * keeps the client's own codes in editor completion without narrowing `string`.
 */
export type ApiErrorCode = ClientErrorCode | (string & Record<never, never>);

/**
 * A failed call. `status` is the HTTP status, or 0 when no answer arrived;
 * `code` is the contract's short `snake_case` word, which a page may branch on,
 * or one of the client's own (`NETWORK_ERROR`, `INVALID_RESPONSE`,
 * `INVALID_REQUEST`, `httpStatusCode(status)` when the server sent no error
 * body). `message` is
 * safe to show to the person.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: ApiErrorCode;

  constructor(
    status: number,
    code: ApiErrorCode,
    message: string,
    options?: ErrorOptions,
  ) {
    super(message, options);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }

  static fromBody(status: number, body: ErrorBody): ApiError {
    return new ApiError(status, body.error.code, body.error.message);
  }
}
