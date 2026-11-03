import assert from "node:assert/strict";
import { describe, it } from "vitest";

import type { PlatformView } from "@/lib/api/schemas";
import { SettingKey } from "@/lib/api/schemas";
import {
  SETTING_DESCRIPTORS,
  formatSettingValue,
  isSettingChanged,
  parseSettingDraft,
  settingDraft,
  settingSaveWarning,
} from "@/lib/platform/settings";

const SETTINGS: PlatformView["settings"] = {
  memory_reserve_mb: 1024,
  default_memory_budget_mb: 512,
  package_mode: "wheelhouse",
  default_wheel_sources: ["/Workspace/Shared/appmux/wheelhouse"],
  shared_sp_delegation_allowed: false,
  max_releases_per_app: 10,
  max_locked_releases: 3,
  request_timeout_s: 120,
  max_request_body_mb: 100,
  max_concurrent_requests: 64,
  build_timeout_s: 600,
  max_processes_per_child: 256,
  child_cpu_pin: true,
  attach_check_warehouse_id: null,
};

function valueOf(key: SettingKey, draft: string) {
  const result = parseSettingDraft(key, draft, SETTINGS);
  assert.equal(result.ok, true, JSON.stringify(result));
  return result.ok ? result.value.value : undefined;
}

function errorOf(key: SettingKey, draft: string) {
  const result = parseSettingDraft(key, draft, SETTINGS);
  assert.equal(result.ok, false);
  return result.ok ? undefined : result.errors.value;
}

describe("descriptors", () => {
  it("describe every key the PUT route accepts, each with a level of effect", () => {
    for (const key of SettingKey.options) {
      const descriptor = SETTING_DESCRIPTORS[key];
      assert.equal(descriptor.key, key);
      assert.ok(descriptor.meaning.length > 0, key);
      assert.equal(descriptor.levelOfEffect, "All children", key);
    }
  });
});

describe("settingDraft", () => {
  it("writes each value the way its editor takes it", () => {
    assert.equal(settingDraft("memory_reserve_mb", SETTINGS), "1024");
    assert.equal(settingDraft("default_memory_budget_mb", SETTINGS), "512");
    assert.equal(settingDraft("child_cpu_pin", SETTINGS), "true");
    assert.equal(
      settingDraft("shared_sp_delegation_allowed", SETTINGS),
      "false",
    );
    assert.equal(settingDraft("package_mode", SETTINGS), "wheelhouse");
    assert.equal(
      settingDraft("default_wheel_sources", {
        ...SETTINGS,
        default_wheel_sources: ["/a", "/b"],
      }),
      "/a\n/b",
    );
    assert.equal(settingDraft("attach_check_warehouse_id", SETTINGS), "");
  });
});

describe("parseSettingDraft", () => {
  it("reads a whole number", () => {
    assert.equal(valueOf("memory_reserve_mb", " 2048 "), 2048);
    assert.equal(valueOf("max_concurrent_requests", "0"), 0);
  });

  it("refuses a blank or a typo instead of guessing", () => {
    assert.equal(errorOf("memory_reserve_mb", ""), "is required");
    assert.equal(errorOf("memory_reserve_mb", "1e3"), "must be a whole number");
    assert.equal(errorOf("memory_reserve_mb", "-5"), "must be a whole number");
    assert.equal(
      errorOf("memory_reserve_mb", "10.5"),
      "must be a whole number",
    );
  });

  it("keeps the default memory budget in the bounds of one app's budget, 64 to 16,384 MB", () => {
    assert.equal(valueOf("default_memory_budget_mb", "64"), 64);
    assert.equal(valueOf("default_memory_budget_mb", " 768 "), 768);
    assert.equal(valueOf("default_memory_budget_mb", "16384"), 16384);
    assert.equal(
      errorOf("default_memory_budget_mb", "63"),
      "must be at least 64",
    );
    assert.equal(
      errorOf("default_memory_budget_mb", "16385"),
      "must be at most 16,384",
    );
    assert.equal(errorOf("default_memory_budget_mb", ""), "is required");
    assert.equal(
      errorOf("default_memory_budget_mb", "5e2"),
      "must be a whole number",
    );
  });

  it("keeps max_locked_releases at most max_releases_per_app minus 4", () => {
    assert.equal(valueOf("max_locked_releases", "6"), 6);
    assert.equal(
      errorOf("max_locked_releases", "7"),
      "must be at most 6 (max_releases_per_app is 10, minus 4)",
    );
  });

  it("holds max_releases_per_app to the same rule from the other side", () => {
    assert.equal(valueOf("max_releases_per_app", "7"), 7);
    assert.equal(
      errorOf("max_releases_per_app", "6"),
      "must be at least 7 (max_locked_releases is 3, plus 4)",
    );
  });

  it("caps request_timeout_s at 290, below the platform's 300 seconds", () => {
    assert.equal(valueOf("request_timeout_s", "290"), 290);
    assert.equal(errorOf("request_timeout_s", "291"), "must be at most 290");
  });

  it("reads a switch", () => {
    assert.equal(valueOf("child_cpu_pin", "true"), true);
    assert.equal(valueOf("child_cpu_pin", "false"), false);
  });

  it("accepts only the package modes", () => {
    assert.equal(
      valueOf("package_mode", "wheelhouse+index"),
      "wheelhouse+index",
    );
    assert.equal(
      errorOf("package_mode", "pypi"),
      "must be one of index, wheelhouse, wheelhouse+index",
    );
  });

  it("reads a list as one entry per line, dropping blank lines and spaces", () => {
    assert.deepEqual(
      valueOf("default_wheel_sources", " /Workspace/a \n\n  \n/Workspace/b\n"),
      ["/Workspace/a", "/Workspace/b"],
    );
    assert.deepEqual(valueOf("default_wheel_sources", ""), []);
  });

  it("clears an optional text with an empty box, and trims it otherwise", () => {
    assert.equal(valueOf("attach_check_warehouse_id", "  "), null);
    assert.equal(valueOf("attach_check_warehouse_id", " abc123 "), "abc123");
  });

  it("files every error under value", () => {
    const result = parseSettingDraft("memory_reserve_mb", "x", SETTINGS);
    assert.deepEqual(result, {
      ok: false,
      errors: { value: "must be a whole number" },
    });
  });
});

describe("isSettingChanged", () => {
  it("is false for the current value, however it is written", () => {
    assert.equal(
      isSettingChanged("memory_reserve_mb", "1024", SETTINGS),
      false,
    );
    assert.equal(
      isSettingChanged("memory_reserve_mb", " 1024 ", SETTINGS),
      false,
    );
    assert.equal(
      isSettingChanged(
        "default_wheel_sources",
        "/Workspace/Shared/appmux/wheelhouse\n\n",
        SETTINGS,
      ),
      false,
    );
    assert.equal(
      isSettingChanged("attach_check_warehouse_id", "  ", SETTINGS),
      false,
    );
  });

  it("is true for another value, and for text that is not valid yet", () => {
    assert.equal(isSettingChanged("memory_reserve_mb", "2048", SETTINGS), true);
    assert.equal(isSettingChanged("memory_reserve_mb", "20x", SETTINGS), true);
    assert.equal(isSettingChanged("child_cpu_pin", "false", SETTINGS), true);
  });
});

describe("settingSaveWarning", () => {
  it("asks for care only when the shared-SP master switch goes on", () => {
    assert.match(
      settingSaveWarning("shared_sp_delegation_allowed", true) ?? "",
      /service principal/i,
    );
    assert.equal(
      settingSaveWarning("shared_sp_delegation_allowed", false),
      null,
    );
    assert.equal(settingSaveWarning("child_cpu_pin", true), null);
  });
});

describe("formatSettingValue", () => {
  it("writes a value with its unit", () => {
    assert.equal(formatSettingValue("memory_reserve_mb", SETTINGS), "1,024 MB");
    assert.equal(
      formatSettingValue("default_memory_budget_mb", SETTINGS),
      "512 MB",
    );
    assert.equal(formatSettingValue("request_timeout_s", SETTINGS), "120 s");
    assert.equal(formatSettingValue("max_concurrent_requests", SETTINGS), "64");
  });

  it("writes a switch, a choice, a list and an unset text in words", () => {
    assert.equal(formatSettingValue("child_cpu_pin", SETTINGS), "On");
    assert.equal(
      formatSettingValue("shared_sp_delegation_allowed", SETTINGS),
      "Off",
    );
    assert.equal(formatSettingValue("package_mode", SETTINGS), "wheelhouse");
    assert.equal(
      formatSettingValue("default_wheel_sources", SETTINGS),
      "/Workspace/Shared/appmux/wheelhouse",
    );
    assert.equal(
      formatSettingValue("default_wheel_sources", {
        ...SETTINGS,
        default_wheel_sources: [],
      }),
      "none",
    );
    assert.equal(
      formatSettingValue("attach_check_warehouse_id", SETTINGS),
      "not set",
    );
  });
});
