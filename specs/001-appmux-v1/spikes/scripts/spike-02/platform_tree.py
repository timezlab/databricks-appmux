import os, stat, json
def tree(root, depth, out, maxn=40):
    n = 0
    for dp, dn, fn in os.walk(root, followlinks=False):
        d = dp[len(root):].count("/")
        if d >= depth: dn[:] = []
        for name in sorted(dn + fn):
            p = os.path.join(dp, name)
            try: st = os.lstat(p)
            except OSError: continue
            kind = "d" if stat.S_ISDIR(st.st_mode) else "l" if stat.S_ISLNK(st.st_mode) else "f"
            ok = os.access(p, os.R_OK)
            out.append(f"{kind} {oct(st.st_mode & 0o777)[2:]:>3} uid={st.st_uid} {st.st_size:>8} r={int(ok)} {p}")
            n += 1
            if n >= maxn: return
o = []
tree("/databricks", 4, o, 60)
print("\n".join(o))
for r in ("/app/deployments", "/app/lha-runtime", "/app/scanner", "/app/static", "/app/.uv-python", "/home/app", "/var/lib/python", "/etc/databricks"):
    o = []
    if os.path.isdir(r): tree(r, 1, o, 12)
    print("==", r, len(o)); print("\n".join(o))
st = os.stat("/dev/termination-log"); print("termination-log", oct(st.st_mode), st.st_uid, st.st_size)
