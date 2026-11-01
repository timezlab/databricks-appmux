import assert from "node:assert/strict";
import { describe, it } from "vitest";

import type { EgressRule } from "@/lib/api/schemas";
import {
  createEgressDraft,
  egressDraftReducer,
  egressWarnings,
  hasRemoteEgressChange,
  isEgressDraftDirty,
  syncEgressDraft,
  validateEgressDraft,
} from "@/lib/platform/egress";
import type { EgressDraftState, EgressSource } from "@/lib/platform/egress";

function rule(
  action: "allow" | "deny",
  cidr: string,
  port = "any",
  comment = "",
): EgressRule {
  return { action, cidr, port, comment };
}

const A = rule("allow", "10.20.0.0/16", "443", "warehouse");
const B = rule("deny", "0.0.0.0/0", "any", "everything else");
const SOURCE: EgressSource = { rules: [A, B], version: 5 };

/** The reducer needs the rules to be what the page rendered; `source` is the latest poll. */
function act(
  state: EgressDraftState,
  action:
    | { type: "add" }
    | { type: "remove"; id: number }
    | { type: "move"; id: number; direction: "up" | "down" }
    | {
        type: "edit";
        id: number;
        field: "action" | "cidr" | "port" | "comment";
        value: string;
      }
    | { type: "discard" },
  source: EgressSource = SOURCE,
): EgressDraftState {
  return egressDraftReducer(state, { ...action, source });
}

function shown(state: EgressDraftState) {
  return state.rules.map(({ action, cidr, port, comment }) => ({
    action,
    cidr,
    port,
    comment,
  }));
}

describe("createEgressDraft", () => {
  it("copies the stored rules, in order, each with its own id", () => {
    const state = createEgressDraft(SOURCE);
    assert.deepEqual(shown(state), [A, B]);
    assert.equal(new Set(state.rules.map((r) => r.id)).size, 2);
    assert.equal(isEgressDraftDirty(state), false);
  });
});

describe("editing", () => {
  it("adds a deny-any rule at the end, ready to be filled in", () => {
    const state = act(createEgressDraft(SOURCE), { type: "add" });
    assert.deepEqual(state.rules.at(-1), {
      id: state.rules.at(-1)?.id,
      action: "deny",
      cidr: "",
      port: "any",
      comment: "",
    });
    assert.equal(state.rules.length, 3);
    assert.equal(isEgressDraftDirty(state), true);
  });

  it("gives every added rule a new id", () => {
    let state = createEgressDraft(SOURCE);
    state = act(state, { type: "add" });
    state = act(state, { type: "add" });
    assert.equal(new Set(state.rules.map((r) => r.id)).size, 4);
  });

  it("removes a rule", () => {
    const initial = createEgressDraft(SOURCE);
    const first = initial.rules[0]?.id ?? -1;
    const state = act(initial, { type: "remove", id: first });
    assert.deepEqual(shown(state), [B]);
  });

  it("moves a rule up or down, and leaves the ends where they are", () => {
    const initial = createEgressDraft(SOURCE);
    const [first, second] = initial.rules.map((r) => r.id);
    assert.deepEqual(
      shown(act(initial, { type: "move", id: second ?? -1, direction: "up" })),
      [B, A],
    );
    assert.deepEqual(
      shown(act(initial, { type: "move", id: first ?? -1, direction: "down" })),
      [B, A],
    );
    assert.deepEqual(
      shown(act(initial, { type: "move", id: first ?? -1, direction: "up" })),
      [A, B],
    );
    assert.deepEqual(
      shown(
        act(initial, { type: "move", id: second ?? -1, direction: "down" }),
      ),
      [A, B],
    );
  });

  it("edits one field of one rule", () => {
    const initial = createEgressDraft(SOURCE);
    const id = initial.rules[0]?.id ?? -1;
    const state = act(initial, {
      type: "edit",
      id,
      field: "port",
      value: "8443",
    });
    assert.equal(state.rules[0]?.port, "8443");
    assert.equal(state.rules[0]?.cidr, "10.20.0.0/16");
  });

  it("ignores an action on a rule that is gone", () => {
    const initial = createEgressDraft(SOURCE);
    assert.deepEqual(shown(act(initial, { type: "remove", id: 999 })), [A, B]);
    assert.deepEqual(
      shown(act(initial, { type: "edit", id: 999, field: "cidr", value: "x" })),
      [A, B],
    );
  });

  it("is not dirty after an edit is undone, or when only spaces differ", () => {
    const initial = createEgressDraft(SOURCE);
    const id = initial.rules[0]?.id ?? -1;
    const edited = act(initial, {
      type: "edit",
      id,
      field: "comment",
      value: "x",
    });
    assert.equal(isEgressDraftDirty(edited), true);
    const undone = act(edited, {
      type: "edit",
      id,
      field: "comment",
      value: "warehouse",
    });
    assert.equal(isEgressDraftDirty(undone), false);
    const spaced = act(initial, {
      type: "edit",
      id,
      field: "comment",
      value: " warehouse ",
    });
    assert.equal(isEgressDraftDirty(spaced), false);
  });

  it("starts over from the stored rules on discard", () => {
    const initial = createEgressDraft(SOURCE);
    const edited = act(initial, { type: "add" });
    const state = act(edited, { type: "discard" });
    assert.deepEqual(shown(state), [A, B]);
    assert.equal(isEgressDraftDirty(state), false);
  });

  it("holds the saved rules as the new starting point", () => {
    const initial = act(createEgressDraft(SOURCE), { type: "add" });
    const saved = egressDraftReducer(initial, {
      type: "saved",
      rules: [A],
      version: 6,
    });
    assert.deepEqual(shown(saved), [A]);
    assert.equal(isEgressDraftDirty(saved), false);
    assert.equal(saved.version, 6);
    const oldIds = new Set(initial.rules.map((r) => r.id));
    assert.ok(saved.rules.every((r) => !oldIds.has(r.id)));
  });
});

describe("syncEgressDraft: a new poll while the editor is open", () => {
  it("changes nothing when the stored rules are the ones the draft started from", () => {
    const state = createEgressDraft(SOURCE);
    assert.equal(syncEgressDraft(state, { ...SOURCE }), state);
  });

  it("follows a newer version of the same rules, so a later save sends it", () => {
    const state = createEgressDraft(SOURCE);
    const synced = syncEgressDraft(state, { rules: [A, B], version: 8 });
    assert.equal(synced.version, 8);
    assert.deepEqual(synced.rules, state.rules);
  });

  it("shows another admin's change at once when nothing was being edited", () => {
    const state = createEgressDraft(SOURCE);
    const c = rule("deny", "192.168.0.0/16");
    const synced = syncEgressDraft(state, { rules: [c], version: 6 });
    assert.deepEqual(shown(synced), [c]);
    assert.equal(synced.version, 6);
    assert.equal(isEgressDraftDirty(synced), false);
  });

  it("keeps what is being typed when someone else changed the rules, and says so", () => {
    const initial = createEgressDraft(SOURCE);
    const edited = act(initial, { type: "add" });
    const remote: EgressSource = { rules: [A], version: 6 };
    const synced = syncEgressDraft(edited, remote);
    assert.equal(synced.rules.length, 3);
    assert.equal(hasRemoteEgressChange(synced, remote), true);
  });

  it("clears the notice when the draft ends up equal to the new stored rules", () => {
    const initial = createEgressDraft(SOURCE);
    const id = initial.rules[1]?.id ?? -1;
    const removed = act(initial, { type: "remove", id });
    const remote: EgressSource = { rules: [A], version: 6 };
    const synced = syncEgressDraft(removed, remote);
    assert.equal(hasRemoteEgressChange(synced, remote), false);
    assert.equal(isEgressDraftDirty(synced), false);
    assert.equal(synced.version, 6);
  });

  it("lets Discard take the other admin's rules", () => {
    const edited = act(createEgressDraft(SOURCE), { type: "add" });
    const remote: EgressSource = { rules: [A], version: 6 };
    const state = act(edited, { type: "discard" }, remote);
    assert.deepEqual(shown(state), [A]);
    assert.equal(hasRemoteEgressChange(state, remote), false);
  });

  it("ignores a poll older than what is held, such as one that was in flight during a save", () => {
    const saved = egressDraftReducer(createEgressDraft(SOURCE), {
      type: "saved",
      rules: [A],
      version: 6,
    });
    const old: EgressSource = SOURCE;
    assert.equal(syncEgressDraft(saved, old), saved);
    assert.equal(hasRemoteEgressChange(saved, old), false);
  });

  it("applies an action to the rules the page showed, not to a stale draft", () => {
    // The page rendered the reset draft (ids from `syncEgressDraft`); the click carries the same source.
    const state = createEgressDraft(SOURCE);
    const remote: EgressSource = {
      rules: [rule("deny", "192.168.0.0/16")],
      version: 6,
    };
    const rendered = syncEgressDraft(state, remote);
    const id = rendered.rules[0]?.id ?? -1;
    const after = act(
      state,
      { type: "edit", id, field: "comment", value: "mine" },
      remote,
    );
    assert.equal(after.rules[0]?.comment, "mine");
    assert.equal(after.rules[0]?.cidr, "192.168.0.0/16");
  });
});

describe("validateEgressDraft", () => {
  it("builds the rules of the PUT from trimmed text", () => {
    const state = createEgressDraft({
      rules: [rule("allow", " 10.20.0.0/16 ", " 443 ", " warehouse ")],
      version: null,
    });
    const result = validateEgressDraft(state.rules);
    assert.deepEqual(result, {
      ok: true,
      rules: [rule("allow", "10.20.0.0/16", "443", "warehouse")],
    });
  });

  it("keeps the draft's order: the first matching rule decides, so a reordered PUT changes the outcome", () => {
    const draft = [
      rule("deny", "10.20.0.0/16", "443", "first"),
      rule("allow", "10.0.0.0/8", "any", "second"),
      rule("deny", "0.0.0.0/0", "any", "third"),
    ];
    const state = createEgressDraft({ rules: draft, version: null });
    assert.deepEqual(validateEgressDraft(state.rules), {
      ok: true,
      rules: draft,
    });
    const swapped = act(
      state,
      { type: "move", id: 1, direction: "down" },
      { rules: draft, version: null },
    );
    assert.deepEqual(validateEgressDraft(swapped.rules), {
      ok: true,
      rules: [draft[1], draft[0], draft[2]],
    });
  });

  it("refuses what the backend refuses: IPv6, host bits, no prefix length", () => {
    for (const cidr of ["2001:db8::/32", "10.0.0.1/8", "10.20.0.0"]) {
      const state = createEgressDraft({
        rules: [rule("deny", cidr)],
        version: null,
      });
      assert.equal(validateEgressDraft(state.rules).ok, false, cidr);
    }
  });

  it("refuses an allow that covers the address the server probes with", () => {
    for (const cidr of ["0.0.0.0/1", "0.0.0.0/8", "0.0.0.0/32"]) {
      const state = createEgressDraft({
        rules: [rule("allow", cidr, "443")],
        version: null,
      });
      assert.equal(validateEgressDraft(state.rules).ok, false, cidr);
    }
  });

  it("accepts an empty list: it clears the rules", () => {
    assert.deepEqual(validateEgressDraft([]), { ok: true, rules: [] });
  });

  it("accepts the placeholders", () => {
    const state = createEgressDraft({
      rules: [
        rule("deny", "@container_ip/32"),
        rule("deny", "0.0.0.0/0", "@appmux_port"),
      ],
      version: null,
    });
    assert.equal(validateEgressDraft(state.rules).ok, true);
  });

  it("names the rule and the field, in words", () => {
    const state = createEgressDraft({
      rules: [A, rule("deny", "ten", "99999")],
      version: null,
    });
    const result = validateEgressDraft(state.rules);
    assert.equal(result.ok, false);
    if (!result.ok) {
      const id = String(state.rules[1]?.id);
      assert.deepEqual(Object.keys(result.errors), [id]);
      assert.match(
        result.errors[id]?.cidr ?? "",
        /CIDR such as 10\.20\.0\.0\/16/,
      );
      assert.match(result.errors[id]?.port ?? "", /a port from 1 to 65535/);
    }
  });

  it("keeps the placeholder in the refusal of an allow that overlaps it", () => {
    const state = createEgressDraft({
      rules: [
        rule("allow", "@container_ip/32", "443"),
        rule("allow", "10.0.0.0/8", "@appmux_port"),
        rule("allow", "169.254.169.254/32", "80"),
        rule("allow", "0.0.0.0/0"),
      ],
      version: null,
    });
    const result = validateEgressDraft(state.rules);
    assert.equal(result.ok, false);
    if (!result.ok) {
      const messages = state.rules.map((r) => {
        const errors = result.errors[String(r.id)];
        return errors?.cidr ?? errors?.port;
      });
      assert.deepEqual(messages, [
        "an allow cannot overlap @container_ip",
        "an allow cannot overlap @appmux_port",
        "an allow cannot overlap 169.254.0.0/16",
        "an allow cannot overlap 169.254.0.0/16",
      ]);
    }
  });

  it("reports an empty address and an empty port as missing, not as a stack of zod text", () => {
    const state = createEgressDraft({
      rules: [rule("deny", "", "")],
      version: null,
    });
    const result = validateEgressDraft(state.rules);
    assert.equal(result.ok, false);
    if (!result.ok) {
      const errors = result.errors[String(state.rules[0]?.id)];
      assert.ok(errors?.cidr);
      assert.ok(errors?.port);
    }
  });
});

describe("egressWarnings: the global list", () => {
  const container = rule(
    "deny",
    "@container_ip/32",
    "any",
    "the container's own address",
  );
  const linkLocal = rule("deny", "169.254.0.0/16", "any", "metadata");

  it("is quiet for the default list", () => {
    assert.deepEqual(egressWarnings([container, linkLocal], "global"), []);
  });

  it("warns when the container's own address is not denied", () => {
    const warnings = egressWarnings([linkLocal], "global");
    assert.equal(warnings.length, 1);
    assert.match(warnings[0] ?? "", /@container_ip/);
  });

  it("warns when the metadata range is not denied", () => {
    const warnings = egressWarnings([container], "global");
    assert.equal(warnings.length, 1);
    assert.match(warnings[0] ?? "", /169\.254\.0\.0\/16/);
  });

  it("counts a deny of everything as covering the metadata range", () => {
    assert.deepEqual(
      egressWarnings([container, rule("deny", "0.0.0.0/0")], "global"),
      [],
    );
  });

  it("warns about both on an empty list", () => {
    assert.equal(egressWarnings([], "global").length, 2);
  });
});

describe("egressWarnings: an app's list", () => {
  it("says nothing about an empty list or a list of denies", () => {
    assert.deepEqual(egressWarnings([], "app"), []);
    assert.deepEqual(egressWarnings([rule("deny", "10.0.0.0/8")], "app"), []);
  });

  it("asks an allow-list to end with a deny of everything", () => {
    const warnings = egressWarnings(
      [rule("allow", "10.20.0.0/16", "443")],
      "app",
    );
    assert.equal(warnings.length, 1);
    assert.match(warnings[0] ?? "", /deny 0\.0\.0\.0\/0/);
    assert.deepEqual(
      egressWarnings(
        [rule("allow", "10.20.0.0/16", "443"), rule("deny", "0.0.0.0/0")],
        "app",
      ),
      [],
    );
  });

  it("does not take a deny of everything on one port for the closing deny", () => {
    assert.equal(
      egressWarnings(
        [rule("allow", "10.20.0.0/16", "443"), rule("deny", "0.0.0.0/0", "80")],
        "app",
      ).length,
      1,
    );
  });
});

describe("egressWarnings: rules that never match", () => {
  it("flags a rule that repeats an earlier destination, by its number", () => {
    const warnings = egressWarnings(
      [
        rule("deny", "@container_ip/32"),
        rule("deny", "169.254.0.0/16"),
        rule("allow", "10.20.0.0/16", "443"),
        rule("deny", "10.20.0.0/16", "443"),
      ],
      "global",
    );
    assert.deepEqual(warnings, [
      "Rule 4 can never match: rule 3 already decides the same traffic (the first match wins).",
    ]);
  });

  it("flags everything after a rule that matches all traffic", () => {
    const warnings = egressWarnings(
      [
        rule("deny", "0.0.0.0/0"),
        rule("deny", "@container_ip/32"),
        rule("deny", "169.254.0.0/16"),
      ],
      "global",
    );
    assert.deepEqual(warnings, [
      "Rule 2 can never match: rule 1 already decides the same traffic (the first match wins).",
      "Rule 3 can never match: rule 1 already decides the same traffic (the first match wins).",
    ]);
  });

  it("flags a rule on one port after the same address on every port", () => {
    const warnings = egressWarnings(
      [rule("deny", "10.0.0.0/8"), rule("deny", "10.0.0.0/8", "22")],
      "app",
    );
    assert.equal(warnings.length, 1);
    assert.match(warnings[0] ?? "", /Rule 2 can never match/);
  });

  it("does not flag the same address on different ports", () => {
    assert.deepEqual(
      egressWarnings(
        [
          rule("allow", "10.0.0.0/8", "443"),
          rule("allow", "10.0.0.0/8", "80"),
          rule("deny", "0.0.0.0/0"),
        ],
        "app",
      ),
      [],
    );
  });
});
