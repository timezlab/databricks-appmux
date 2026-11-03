import assert from "node:assert/strict";
import { describe, it } from "vitest";

import * as api from "@/lib/api";
import {
  SECRET_MASK,
  environmentJson,
  filterPackages,
  filterVariables,
  packagesCopyText,
  runtimeRows,
  variableSourceText,
  variableValueText,
  variablesCopyText,
} from "@/lib/app/environment";
import { readExample } from "../../../support/contract-examples";

const EXAMPLE = api.AppEnvironment.parse(readExample("AppEnvironment"));

type Variable = api.AppEnvironment["variables"][number];

function variable(overrides: Partial<Variable>): Variable {
  return {
    name: "LOG_LEVEL",
    value: "info",
    source: "config",
    resource_key: null,
    is_secret: false,
    ...overrides,
  };
}

describe("variableValueText", () => {
  it("shows a value as it is", () => {
    assert.equal(variableValueText(variable({})), "info");
  });

  it("shows a secret as a mask, even if a value came with it", () => {
    assert.equal(
      variableValueText(variable({ is_secret: true, value: "hunter2" })),
      SECRET_MASK,
    );
  });

  it("says a resource key the app does not have is not available", () => {
    assert.equal(
      variableValueText(
        variable({ source: "resource", resource_key: "db", value: null }),
      ),
      "not available",
    );
  });
});

describe("variableSourceText", () => {
  it("names the resource key a variable comes from", () => {
    assert.equal(
      variableSourceText(
        variable({ source: "resource", resource_key: "db-password" }),
      ),
      "resource key db-password",
    );
  });

  it("names config and platform variables by their source", () => {
    assert.equal(variableSourceText(variable({})), "config");
    assert.equal(
      variableSourceText(variable({ source: "platform" })),
      "platform",
    );
  });
});

describe("filterVariables", () => {
  it("keeps every variable for an empty search", () => {
    assert.equal(filterVariables(EXAMPLE.variables, "  ").length, 3);
  });

  it("matches the name, the value or the resource key, in any case", () => {
    assert.deepEqual(
      filterVariables(EXAMPLE.variables, "log").map((item) => item.name),
      ["LOG_LEVEL"],
    );
    assert.deepEqual(
      filterVariables(EXAMPLE.variables, "/SALES").map((item) => item.name),
      ["APPMUX_BASE_PATH"],
    );
    assert.deepEqual(
      filterVariables(EXAMPLE.variables, "db-pass").map((item) => item.name),
      ["DB_PASSWORD"],
    );
  });

  it("never matches a secret by a value it should not have", () => {
    const leaked = [
      variable({ name: "TOKEN", is_secret: true, value: "s3cr3t" }),
    ];
    assert.deepEqual(filterVariables(leaked, "s3cr3t"), []);
  });
});

describe("filterPackages", () => {
  it("matches the name and the version", () => {
    assert.deepEqual(
      filterPackages(EXAMPLE.packages, "fast", "all").map((item) => item.name),
      ["fastapi"],
    );
    assert.deepEqual(
      filterPackages(EXAMPLE.packages, "2.11", "all").map((item) => item.name),
      ["pydantic"],
    );
  });

  it("keeps only the packages of one source", () => {
    assert.deepEqual(
      filterPackages(EXAMPLE.packages, "", "wheel").map((item) => item.name),
      ["acme-tools"],
    );
  });
});

describe("runtimeRows", () => {
  it("lists the Python and Node versions", () => {
    assert.deepEqual(runtimeRows(EXAMPLE.runtime), [
      { label: "Python", value: "3.11.9" },
      { label: "Node", value: "22.22.1" },
    ]);
  });

  it("says when a version was not detected", () => {
    assert.deepEqual(
      runtimeRows({
        kind: "unknown",
        python_version: null,
        node_version: null,
      }),
      [
        { label: "Python", value: "Not detected" },
        { label: "Node", value: "Not detected" },
      ],
    );
  });
});

describe("the copied and JSON texts", () => {
  const withLeak: api.AppEnvironment = {
    ...EXAMPLE,
    variables: [
      ...EXAMPLE.variables,
      variable({ name: "API_TOKEN", is_secret: true, value: "s3cr3t-value" }),
    ],
  };

  it("never carry a secret's value", () => {
    assert.ok(!environmentJson(withLeak).includes("s3cr3t-value"));
    assert.ok(!variablesCopyText(withLeak.variables).includes("s3cr3t-value"));
  });

  it("show the same data as the structured view, as JSON", () => {
    const parsed: unknown = JSON.parse(environmentJson(EXAMPLE));
    assert.deepEqual(parsed, EXAMPLE);
  });

  it("copy variables as NAME=value lines with the secret masked", () => {
    assert.equal(
      variablesCopyText(EXAMPLE.variables),
      `LOG_LEVEL=info\nDB_PASSWORD=${SECRET_MASK}\nAPPMUX_BASE_PATH=/sales`,
    );
  });

  it("copy packages as name, version and source", () => {
    assert.equal(
      packagesCopyText(EXAMPLE.packages),
      "fastapi ==0.115.0 (release)\nacme-tools 1.2.0 (wheel)\npydantic 2.11.7 (platform)",
    );
  });
});
