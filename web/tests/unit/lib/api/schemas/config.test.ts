/**
 * Boundary tests of the limits and rules that the request schemas enforce.
 * Every number below is written as a literal from admin-api.md on purpose: a
 * test that reads the schema's own constant would follow a mutation of it.
 */
import { describe, it } from "vitest";

import * as api from "@/lib/api";
import {
  readExampleObject,
  without,
} from "../../../../support/contract-examples";
import {
  accepts,
  rejects,
  variant,
} from "../../../../support/schema-assertions";

describe("configuration", () => {
  const config = readExampleObject("UpdateConfigRequest");

  it("health deadline: an integer from 5 to 600", () => {
    for (const good of [5, 600]) {
      accepts(
        api.UpdateConfigRequest,
        variant(config, { health_deadline_s: good }),
      );
    }
    for (const bad of [4, 601, 5.5, "5"]) {
      rejects(
        api.UpdateConfigRequest,
        variant(config, { health_deadline_s: bad }),
        String(bad),
      );
    }
  });

  it("health path: starts with /", () => {
    accepts(api.UpdateConfigRequest, variant(config, { health_path: "/" }));
    accepts(
      api.UpdateConfigRequest,
      variant(config, { health_path: "/healthz" }),
    );
    for (const bad of ["", "healthz", "http://x/", " /x"]) {
      rejects(
        api.UpdateConfigRequest,
        variant(config, { health_path: bad }),
        bad,
      );
    }
  });

  it("own-SP keys: both null, or both resource keys", () => {
    const keys = (
      own_sp_client_id_key: string | null,
      own_sp_client_secret_key: string | null,
    ) => variant(config, { own_sp_client_id_key, own_sp_client_secret_key });
    accepts(api.UpdateConfigRequest, keys(null, null));
    accepts(api.UpdateConfigRequest, keys("sp-id", "sp-secret"));
    rejects(api.UpdateConfigRequest, keys("sp-id", null), "only the id");
    rejects(
      api.UpdateConfigRequest,
      keys(null, "sp-secret"),
      "only the secret",
    );
    rejects(api.UpdateConfigRequest, keys("SP ID", "sp-secret"), "bad key");
    rejects(api.UpdateConfigRequest, keys("sp-id", "a".repeat(64)), "long key");
  });

  it("env entries: strict, exactly one of value and value_from, value_from is a resource key", () => {
    const env = (entry: Record<string, unknown>) =>
      variant(config, { env: [entry] });
    accepts(api.UpdateConfigRequest, env({ name: "A", value: "1" }));
    accepts(
      api.UpdateConfigRequest,
      env({ name: "A", value_from: "warehouse" }),
    );
    rejects(api.UpdateConfigRequest, env({ name: "A" }), "neither");
    rejects(
      api.UpdateConfigRequest,
      env({ name: "A", value: "1", value_from: "k" }),
      "both",
    );
    rejects(
      api.UpdateConfigRequest,
      env({ name: "A", value: "1", is_admin: true }),
      "an unlisted field in a nested entry",
    );
    rejects(
      api.UpdateConfigRequest,
      env({ name: "A", value_from: "Not A Key" }),
      "value_from is a resource key",
    );
  });

  it("the response form of an env entry keeps ignoring fields it does not list", () => {
    accepts(api.EnvEntry, { name: "A", value: "1", added_later: true });
  });
});

describe("attaching a resource", () => {
  const warehouse = readExampleObject("AttachResourceRequest");
  const secret = {
    type: "secret",
    target: "sales-secrets/openai-api-key",
    permission: "READ",
  };

  it("needs access_as for every type but secret", () => {
    accepts(api.AttachResourceRequest, warehouse);
    rejects(
      api.AttachResourceRequest,
      without(warehouse, "access_as"),
      "access_as omitted",
    );
    rejects(
      api.AttachResourceRequest,
      variant(warehouse, { access_as: null }),
      "access_as null",
    );
    rejects(api.AttachResourceRequest, variant(warehouse, { access_as: "me" }));
  });

  it("takes no access_as for a secret, and a secret is a scope/key reference with READ", () => {
    accepts(api.AttachResourceRequest, secret);
    accepts(api.AttachResourceRequest, { ...secret, access_as: null });
    rejects(api.AttachResourceRequest, { ...secret, access_as: "obo" });
    rejects(api.AttachResourceRequest, { ...secret, permission: "WRITE" });
    for (const bad of ["no-slash", "/key", "scope/", "a/b/c"]) {
      rejects(api.AttachResourceRequest, { ...secret, target: bad }, bad);
    }
  });
});
