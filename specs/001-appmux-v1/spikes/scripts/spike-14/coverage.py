"""Spike 14: subtract the App environment from each fixture's hashed requirements.

usage: coverage.py <environment-packages.txt> <name>.lock.txt ...
Writes, per lock, <name>.missing.txt (the entries the environment does not hold at the
locked version, hash lines kept) and prints one JSON object with the counts.
`same` = environment has name==version; `other` = environment has the name at another
version; `absent` = environment lacks the name.
"""
import json
import re
import sys


def canon(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def read_environment(path):
    return {canon(n): v for n, v in (line.strip().split("==") for line in open(path) if "==" in line)}


def read_lock(path):
    """-> list of (name, version, [lines of the entry])"""
    entries, current = [], None
    for line in open(path):
        m = re.match(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)", line)
        if m:
            current = [m.group(1), m.group(2), [line.rstrip("\n")]]
            entries.append(current)
        elif current is not None and line.strip().startswith("--hash="):
            current[2].append(line.rstrip("\n"))
        elif current is not None and not line.startswith(" "):
            current = None
    return entries


def main(env_path, lock_paths):
    env = read_environment(env_path)
    result = {}
    for lock in lock_paths:
        name = lock.split("/")[-1].split(".")[0]
        entries = read_lock(lock)
        groups = {"same": [], "other": [], "absent": []}
        missing_lines = []
        for pkg, ver, lines in entries:
            have = env.get(canon(pkg))
            kind = "absent" if have is None else "same" if have == ver else "other"
            groups[kind].append(f"{pkg}=={ver}" + (f" (env {have})" if kind == "other" else ""))
            if kind != "same":
                missing_lines += lines
        open(f"{name}.missing.txt", "w").write("\n".join(missing_lines) + "\n")
        result[name] = {"entries": len(entries), **{k: len(v) for k, v in groups.items()}, "detail": groups}
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
