import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { ApiError, INVALID_REQUEST } from "@/lib/api";
import { describeError } from "@/components/error-message";

const STALE = "changed by someone else, reload and retry";

describe("describeError: the API's statuses", () => {
  it("409 with the stale-version message is a reload prompt", () => {
    const message = describeError(new ApiError(409, "version_conflict", STALE));
    assert.equal(message.kind, "conflict");
    assert.equal(message.heading, "Changed by someone else");
    assert.equal(message.detail, STALE);
    assert.equal(message.action, "reload-data");
    assert.match(message.hint ?? "", /reload/i);
  });

  it("409 for a state that forbids the action also asks for a reload, and says what the server said", () => {
    const message = describeError(
      new ApiError(
        409,
        "release_active",
        "release 3 is active and cannot be deleted",
      ),
    );
    assert.equal(message.kind, "conflict");
    assert.equal(message.heading, "Not possible in the current state");
    assert.equal(message.detail, "release 3 is active and cannot be deleted");
    assert.equal(message.action, "reload-data");
  });

  it("recognises the stale message whatever the code and the case", () => {
    const message = describeError(
      new ApiError(
        409,
        "anything",
        "Changed by someone else, reload and retry",
      ),
    );
    assert.equal(message.heading, "Changed by someone else");
  });

  it("401 tells the person to reload the page to sign in again", () => {
    const message = describeError(
      new ApiError(401, "unauthenticated", "no identity"),
    );
    assert.equal(message.kind, "signed-out");
    assert.equal(message.action, "reload-page");
    assert.match(message.hint ?? "", /sign in/i);
  });

  it("403 states the denial and shows the server's reason", () => {
    const message = describeError(
      new ApiError(403, "forbidden", "needs CAN_MANAGE on sales"),
    );
    assert.equal(message.kind, "forbidden");
    assert.equal(message.heading, "You do not have permission");
    assert.equal(message.detail, "needs CAN_MANAGE on sales");
    assert.equal(message.action, "none");
  });

  it("503 says appmux is unavailable and offers a retry", () => {
    const message = describeError(
      new ApiError(503, "registry_unavailable", "the registry is unreachable"),
    );
    assert.equal(message.kind, "unavailable");
    assert.equal(message.action, "retry");
    assert.equal(message.detail, "the registry is unreachable");
  });

  it("404 says what is missing and that it may be gone", () => {
    const message = describeError(
      new ApiError(404, "not_found", "no app 'sales'"),
    );
    assert.equal(message.kind, "not-found");
    assert.equal(message.detail, "no app 'sales'");
  });

  it("400 shows which field or rule the server refused", () => {
    const message = describeError(
      new ApiError(400, "slug_taken", "slug 'reports' is already taken"),
    );
    assert.equal(message.kind, "rejected");
    assert.equal(message.heading, "The change was not accepted");
    assert.equal(message.detail, "slug 'reports' is already taken");
  });

  it("any other 5xx is a server error with a retry", () => {
    for (const status of [500, 502, 504]) {
      const message = describeError(
        new ApiError(
          status,
          `http_${status}`,
          "The server answered without an error message; retry.",
        ),
      );
      assert.equal(message.kind, "server", String(status));
      assert.equal(message.action, "retry");
    }
  });

  it("a network failure (status 0) says appmux could not be reached, and that a signed-out session looks the same", () => {
    const message = describeError(
      new ApiError(
        0,
        "network_error",
        "Could not reach the appmux API; check the connection and retry.",
      ),
    );
    assert.equal(message.kind, "network");
    assert.equal(message.heading, "Could not reach appmux");
    assert.equal(message.action, "retry");
    assert.match(message.hint ?? "", /reload the page/i);
    assert.match(message.hint ?? "", /sign in/i);
  });

  it("a request the client refused to send (status 0, a bug in the page) is not taken for a lost connection", () => {
    const message = describeError(
      new ApiError(0, INVALID_REQUEST, "limit: too big"),
    );
    assert.equal(message.kind, "unknown");
    assert.equal(message.detail, "limit: too big");
    assert.equal(message.action, "none");
  });

  it("an answer that does not match the contract asks for a page reload", () => {
    const message = describeError(
      new ApiError(
        200,
        "invalid_response",
        "The server answered with an unexpected body (items: Required); reload.",
      ),
    );
    assert.equal(message.kind, "unexpected-response");
    assert.equal(message.action, "reload-page");
    assert.match(message.detail ?? "", /unexpected body/);
  });

  it("an unlisted status still shows the server's message", () => {
    const message = describeError(
      new ApiError(429, "slow_down", "too many requests"),
    );
    assert.equal(message.kind, "unknown");
    assert.equal(message.detail, "too many requests");
    assert.equal(message.action, "none");
  });
});

describe("describeError: what is not an ApiError", () => {
  it("an Error from the page shows its message and offers a reload", () => {
    const message = describeError(new TypeError("x is not a function"));
    assert.equal(message.kind, "unknown");
    assert.equal(message.heading, "Something went wrong");
    assert.equal(message.detail, "x is not a function");
    assert.equal(message.action, "reload-page");
  });

  it("a thrown string or object is not echoed", () => {
    for (const thrown of ["boom", { secret: "x" }, null, undefined, 42]) {
      const message = describeError(thrown);
      assert.equal(message.kind, "unknown");
      assert.equal(message.detail, null);
    }
  });
});

describe("describeError: the server's text is data", () => {
  it("passes markup through untouched, for the renderer to escape as text", () => {
    const markup = '<img src=x onerror="alert(1)">';
    const message = describeError(new ApiError(400, "bad", markup));
    assert.equal(message.detail, markup);
  });
});
