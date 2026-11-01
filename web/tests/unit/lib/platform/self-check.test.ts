import assert from "node:assert/strict";
import { describe, it } from "vitest";

import type { SelfCheckResult } from "@/lib/api/schemas";
import { newerSelfCheck, selfCheckView } from "@/lib/platform/self-check";

function result(overrides: Partial<SelfCheckResult> = {}): SelfCheckResult {
  return {
    passed: true,
    ran_at: "2026-10-30T09:14:00.000Z",
    duration_ms: 840,
    checks: [
      { name: "sandbox_user_namespace", passed: true, detail: null },
      { name: "egress_firewall", passed: true, detail: "10 rules loaded" },
    ],
    ...overrides,
  };
}

describe("selfCheckView", () => {
  it("passes only when the result and every check say so", () => {
    const view = selfCheckView(result());
    assert.equal(view.state, "passed");
    assert.equal(view.presentation.label, "Passed");
    assert.equal(view.presentation.tone, "success");
    assert.equal(view.summary, "2 checks passed in 840 ms");
    assert.deepEqual(view.failedNames, []);
  });

  it("lists every check with its own state and detail", () => {
    const view = selfCheckView(result());
    assert.deepEqual(
      view.rows.map((row) => [row.name, row.state, row.detail]),
      [
        ["sandbox_user_namespace", "passed", ""],
        ["egress_firewall", "passed", "10 rules loaded"],
      ],
    );
    assert.equal(view.rows[0]?.presentation.mark, "●");
  });

  it("fails when one check fails, even if the overall flag says passed", () => {
    const view = selfCheckView(
      result({
        passed: true,
        checks: [
          { name: "a", passed: true, detail: null },
          { name: "b", passed: false, detail: "no user namespaces" },
        ],
      }),
    );
    assert.equal(view.state, "failed");
    assert.deepEqual(view.failedNames, ["b"]);
    assert.equal(view.summary, "1 of 2 checks failed in 840 ms");
    assert.equal(view.rows[1]?.state, "failed");
    assert.equal(view.rows[1]?.detail, "no user namespaces");
  });

  it("fails when the overall flag says failed, even if every listed check passed", () => {
    const view = selfCheckView(result({ passed: false }));
    assert.equal(view.state, "failed");
    assert.deepEqual(view.failedNames, []);
    assert.match(view.summary, /reported a failure/i);
  });

  it("does not call a result with no checks a pass", () => {
    const view = selfCheckView(result({ checks: [] }));
    assert.equal(view.state, "unverified");
    assert.equal(view.presentation.tone, "warning");
    assert.match(view.summary, /no checks/i);
  });

  it("says 1 check, not 1 checks", () => {
    const view = selfCheckView(
      result({ checks: [{ name: "a", passed: true, detail: null }] }),
    );
    assert.equal(view.summary, "1 check passed in 840 ms");
  });

  it("gives every state a word and a mark, not a colour alone", () => {
    for (const checks of [
      [],
      [{ name: "a", passed: false, detail: null }],
      [{ name: "a", passed: true, detail: null }],
    ]) {
      const { presentation } = selfCheckView(
        result({ passed: checks.every((check) => check.passed), checks }),
      );
      assert.ok(presentation.label.length > 0);
      assert.ok(presentation.mark.length > 0);
    }
  });
});

describe("newerSelfCheck", () => {
  const older = result({ ran_at: "2026-10-30T09:00:00.000Z" });
  const newer = result({ ran_at: "2026-10-30T09:10:00.000Z" });

  it("keeps the one that ran later, in either order", () => {
    assert.equal(newerSelfCheck(older, newer), newer);
    assert.equal(newerSelfCheck(newer, older), newer);
  });

  it("prefers the first when they ran at the same time", () => {
    const twin = result({ ran_at: older.ran_at });
    assert.equal(newerSelfCheck(older, twin), older);
  });

  it("takes the one with a readable time when the other has none", () => {
    const broken = result({ ran_at: "yesterday" });
    assert.equal(newerSelfCheck(broken, older), older);
    assert.equal(newerSelfCheck(older, broken), older);
  });

  it("returns the second when there is no first", () => {
    assert.equal(newerSelfCheck(null, newer), newer);
    assert.equal(newerSelfCheck(newer, null), newer);
    assert.equal(newerSelfCheck(null, null), null);
  });
});
