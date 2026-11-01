import assert from "node:assert/strict";
import { describe, it } from "vitest";

import {
  appControlMessage,
  grantActionMessage,
  releaseActionMessage,
  requestSentMessage,
  requestWithdrawnMessage,
  resourceActionMessage,
  updateOutcomeMessage,
} from "@/lib/app/outcomes";

describe("updateOutcomeMessage", () => {
  it("says a release was created and that the current one keeps serving", () => {
    const text = updateOutcomeMessage({ status: "pending", release_no: 9 });
    assert.match(text, /release 9/i);
    assert.match(text, /keeps serving/i);
  });

  it("says nothing was created when the folder equals a live release", () => {
    const text = updateOutcomeMessage({ status: "up_to_date", release_no: 7 });
    assert.match(text, /up to date/i);
    assert.match(text, /release 7/i);
    assert.match(text, /no new release/i);
  });
});

describe("appControlMessage", () => {
  it("names what was asked, not a state that has not been reached", () => {
    assert.match(appControlMessage("start"), /start/i);
    assert.match(appControlMessage("stop"), /stop/i);
    assert.match(appControlMessage("restart"), /restart/i);
  });
});

describe("releaseActionMessage", () => {
  it("covers each action with the release number", () => {
    for (const action of [
      "activate",
      "lock",
      "unlock",
      "delete",
      "edit",
    ] as const) {
      assert.match(releaseActionMessage(action, 5), /release 5/i, action);
    }
  });

  it("says activation is a health-gated swap, so the current release keeps serving until then", () => {
    assert.match(releaseActionMessage("activate", 5), /healthy|keeps serving/i);
  });
});

describe("resourceActionMessage", () => {
  it("says the change applies at the next start, because resources belong to the app", () => {
    for (const action of ["attach", "detach"] as const) {
      const text = resourceActionMessage(action, "warehouse");
      assert.match(text, /warehouse/);
      assert.match(text, /restart/i);
    }
    assert.match(resourceActionMessage("attach", "warehouse"), /attached/i);
    assert.match(resourceActionMessage("detach", "warehouse"), /detached/i);
  });
});

describe("grantActionMessage", () => {
  it("names the person or group and what happened", () => {
    assert.match(grantActionMessage("add", "sales-team"), /sales-team/);
    assert.match(grantActionMessage("add", "sales-team"), /added|can now/i);
    assert.match(grantActionMessage("change", "sales-team"), /changed/i);
    assert.match(grantActionMessage("remove", "sales-team"), /removed/i);
  });
});

describe("requestSentMessage and requestWithdrawnMessage", () => {
  it("says a platform admin decides, and which kind was sent", () => {
    assert.match(requestSentMessage("settings"), /platform admin/i);
    assert.match(requestSentMessage("settings"), /settings/i);
    assert.match(requestSentMessage("delegation"), /delegation/i);
  });

  it("says the request was taken back", () => {
    assert.match(requestWithdrawnMessage("delegation"), /withdrawn/i);
    assert.match(requestWithdrawnMessage("delegation"), /delegation/i);
  });
});
