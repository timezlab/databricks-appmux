import json, os, re
base = "/databricks/dynamic-secrets"
TOKEN = re.compile(r"[A-Za-z0-9_.=+/-]{40,}")
def shape(v):
    if isinstance(v, dict): return {k: shape(x) for k, x in v.items()}
    if isinstance(v, list): return [shape(x) for x in v[:3]]
    if isinstance(v, str):
        tl = ' TOKEN-LIKE' if TOKEN.fullmatch(v) else ''
        return "<str len=%d%s>" % (len(v), tl)
    return v
for p in ("shared/deployment-statuses.json", "shared/deployment-unit-info.json", "shared/configs/deployment.json", "shared/configs/effective-scopes.json", "shared/configs/telemetry-table-names.json"):
    try: print(p, json.dumps(shape(json.load(open(f"{base}/{p}"))))[:400])
    except Exception as e: print(p, type(e).__name__)
for p in ("reserved-platform/system-metadata/system", "reserved-platform/app-metadata/app", "reserved-platform/app-metadata/app-instance", "shared/metadata/app-name", "shared/metadata/spec-version", "shared/configs/fluentbit-version", "shared/configs/telemetry-enabled"):
    s = open(f"{base}/{p}").read()
    print(p, f"<len={len(s)}>", "".join(c if c.isalpha() else "#" if c.isdigit() else c for c in s)[:50].replace("\n", "\\n"))
print(open(f"{base}/shared/configs/fluent-bit.yaml.TEMPLATE").read()[:350])
# runtime.cfg: keys only
print("runtime.cfg keys:", [l.split("=")[0].split(":")[0].strip() for l in open("/databricks/runtime-secrets/runtime.cfg") if l.strip() and not l.startswith("#")][:20])
