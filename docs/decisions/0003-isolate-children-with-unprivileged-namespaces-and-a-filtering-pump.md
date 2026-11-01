# Isolate each child with unprivileged namespaces and a filtering network pump

**Status:** accepted
**Date:** 2026-10-30
**Deciders:** Liam Lee (design approved 2026-10-30)

## Context

appmux treats every child as potentially malicious, but all children share one
container and one OS user (uid 1000) with appmux. Measured on the Databricks
Apps container (Free Edition, 2026-10-29): same-uid processes read each other's
`/proc/<pid>/environ` (`ptrace_scope` 0), the platform's
`/databricks/runtime-secrets/runtime.cfg` is readable, 77 of 89 localhost ports
answer, there is no root, no `useradd`, no cgroup write and no `/dev/net/tun`.
User, mount, net and pid namespaces do work unprivileged through `unshare`, and
a veth pair with an `AF_PACKET` socket works. Children must keep normal
features (outbound network, dependencies, WebSocket) unless an admin narrows
them.

## Decision

Each child runs in its own user, mount, net and pid namespaces, created without
privilege by the sandbox launcher; stage 1 inside the namespaces brings up the
child's loopback and network pair, makes mounts private, hides `/proc`, `/tmp`,
`/var/tmp` and `/databricks/runtime-secrets` under `tmpfs`, binds the app
environment and the release overlay read-only, drops every capability, sets
`no_new_privs`, then execs the child's command ([design §6.1](../product/v1-design.md#61-sandbox)).

The child's only way out is `eth0` on a veth pair. An `AF_PACKET` socket opened
inside the namespace is passed to appmux over `SCM_RIGHTS`; one pump process per
child parses Ethernet and IPv4 headers, applies the egress rules (ordered,
first match wins, no match = allow, per-app rules set by a platform admin only)
and feeds `slirp4netns --netns-type=tapfd --disable-host-loopback`. Inbound
traffic reaches the child through a unix-socket relay. `self_check()` runs at
startup and after any sandbox change with a canary child; if the canary
succeeds at any forbidden action, no route opens ([design §6.2–6.4](../product/v1-design.md#62-network-path)).

## Alternatives considered

- **Flat supervisor, no namespaces** — rejected: the measurements above show a
  child can read siblings' and PID 1's environment and reach most localhost
  ports, which contradicts "assume a child is malware".
- **Shard children across several slots** — rejected for v1: it spends the
  scarce resource this repo exists to save and gives no isolation inside a slot.
  Kept as a later escape hatch.
- **TAP-based user-mode NAT (`slirp4netns` default, `pasta`)** — rejected:
  `/dev/net/tun` is absent, so it cannot start.
- **`--disable-host-loopback` alone** — rejected: blocks host loopback but not
  the container IP, appmux's port or `169.254.169.254`.
- **Blanket private-range deny** — rejected: it also blocked the workspace
  host, which resolves to a private address.

## Consequences

**Better:**
- A hostile child cannot read another child's or appmux's environment, see other
  pids, reach appmux's port or loopback, or write the packages other children
  import; each of these is a `self_check()` row.
- Children inherit the workspace's DNS and network path through `slirp4netns`
  at plain-container speed (measured 11–12 MB/s), so `pip` and API calls work
  unchanged.

**Worse:**
- No cgroups: CPU and process counts are unbounded, so a fork bomb is an
  accepted gap, and memory is held only by `RLIMIT_AS` plus a best-effort
  watchdog.
- Every outbound frame passes through a user-space pump in Python; throughput
  under saturation is not measured (spike 7).
- The mechanism rests on platform behaviour (unprivileged user namespaces,
  veth, `AF_PACKET`) that Databricks may change; measurements exist for Free
  Edition only.
- Egress defaults to pass-through, so exfiltration over allowed destinations or
  DNS tunnelling stays possible.
- Local AppArmor blocks `unshare` on this project's machines, so sandbox tests
  run only on the Databricks container.

**Must now be true:**
- Every child process starts through the sandbox launcher (`sandbox` module);
  no code path execs a child outside it.
- No route opens while `self_check()` has failed or has not run.
- The pump drops frames it cannot parse and all non-IPv4 frames (fail closed);
  the `AF_PACKET` descriptor is never inherited by the child.
- The app environment and every release overlay are mounted `MS_RDONLY` inside
  the sandbox, and `self_check()` proves a write fails with `EROFS`.
- The egress editor refuses an `allow` that overlaps `@container_ip`,
  `@appmux_port` or `169.254.0.0/16`.
- `slirp4netns` is fetched at install with a checksum and never committed (its
  licence is believed to be GPL-2.0; spike 9).

## Revisit if

`self_check()` fails on a healthy deploy because the platform removed user
namespaces, veth or `AF_PACKET`; the pump measures too slow for real load
(replace the pump, not the design); cgroup writes become available; or
Databricks offers per-child isolation natively.

## Spike results

**Spike 2, 2026-10-30 (pass; [log](../../specs/001-appmux-v1/spikes/README.md#spike-2--filesystem-exposure-2026-10-30)).**
The decision holds, and the Decision's "hides `/proc`, `/tmp`, `/var/tmp` and
`/databricks/runtime-secrets` under `tmpfs`" is replaced by an allow-list. On
the Free Edition container an unsandboxed child (uid 1000) could still read the
platform's `dynamic-secrets` and `runtime.cfg` (which names a client secret),
read **and write** the customer log and `/dev/termination-log`, write
`/home/app`, `/dev/shm` and the app directory (appmux's own source), read
`/sys/class/dmi`, and create a nested user namespace even after the capability
drop. Hiding paths one by one leaves the rest open, so stage 1 builds the
child's root instead: a fresh `tmpfs` root, `pivot_root` into it, detach the
old root, `chroot` into a subdirectory, then drop capabilities.

*Mount table (added).* Read-only: `/usr`, the app environment
(`.venv`), the release at `/srv/app`, and ten named `/etc` files. Read-write:
the per-child run directory at `/run/appmux`, and a private `tmpfs` each for
`/tmp` (64 MB), `/var/tmp` (16 MB), `/dev/shm` (64 MB) and `/home/app`
(64 MB). `/dev` holds `null`, `zero`, `full`, `random`, `urandom`; `/proc` holds
`cpuinfo`, `meminfo` and `/proc/self`, with `cgroup`, `mountinfo`, `mounts` and
`mountstats` overmounted by `/dev/null`. Everything else is absent. A read-only
bind must be remounted with the source mount's locked flags (`nosuid`, `nodev`,
`noexec`, `relatime`) or the remount fails. With this table 18 of 18 canaries
(platform files, sibling release, run directory and socket, app directory, home,
shared temp directories) answered `ENOENT`.

*Decision (added).* `unshare` takes `--ipc`, because SysV shared memory and
message queues stay visible without it. The `chroot` into a subdirectory is not
cosmetic: it makes `unshare(CLONE_NEWUSER)` fail with `EPERM`, which closes
nested user namespaces. Stage 1 closes every descriptor above 2 before `exec`:
the checker still found three inherited descriptors (two pipes and a socket)
open, so this is required, not yet shown to work.

*Consequences (added).* `self_check()` gains one row per path that mattered:
`runtime.cfg`, `dynamic-secrets`, the customer log, `termination-log`,
`/sys/class/dmi`, `/etc/hostname`, `/proc/self/cgroup` and `mountinfo`, the app
directory, a sibling's run directory, release and socket, SysV IPC, a nested
user namespace, and open descriptors above 2. Still open: a child can fill the
host-backed run directory (spike 7); fluent-bit listens on `0.0.0.0:7270`
(spike 6); the cgroup and UTS namespaces were not tried; measurements are Free
Edition only. The secret-name scan checked names, not content.

**Spike 6, 2026-10-30 (pass; [log](../../specs/001-appmux-v1/spikes/README.md#spike-6--egress-mechanics-2026-10-30)).**
The decision holds. Free Edition only.

*Consequences (added).*
- **The container address is the deny that matters.** It listens on 28 TCP
  ports, among them the App port (where the edge listens), fluent-bit on
  `0.0.0.0:7270` and platform services. A sandbox with no rule connected to
  all 28; with `deny @container_ip` it connected to none. This closes the open
  item "fluent-bit listens on `0.0.0.0:7270`": it is reachable without the
  deny and unreachable with it. `self_check()` tries every listening port, not
  only appmux's.
- **Reload is live, and a deny is silent.** The pump loads a changed rules file
  within 10 ms and a new connection follows the new rule within a fraction of
  a second. An open connection that crosses a deny is not reset: it stalls,
  and after the allow it resumes at the child's TCP retransmit timer (about
  25 s after a 30 s deny). A blocked connect is a timeout, never a refusal.
- **Fragments are dropped when port rules exist, which is conservative.** With
  fragments passed `slirp4netns` reassembles and a first fragment to a denied
  port is still dropped by the rule. The cost is that UDP datagrams over the
  MTU stop working for an app that has port rules.
- **IPv6 stays unsupported.** The pump drops it and `slirp4netns` runs without
  `--enable-ipv6`; a child has link-local addresses only. With IPv6 enabled
  the child got an address and a route, yet connects failed at once; IPv6 NAT
  on a host with a global address is untested.
- **Neighbouring pods are unreachable on Free Edition**, in both directions,
  whatever the rules say (the platform drops the traffic). The subnet is not
  added to the default list; a paid workspace is untested.

**Spike 7, 2026-10-30 (pass; [log](../../specs/001-appmux-v1/spikes/README.md#spike-7--throughput-hogs-forks-and-builds-2026-10-30)).**
The decision holds. Free Edition only. It replaces two "Worse" lines above:
"throughput under saturation is not measured" is now measured, and "CPU and
process counts are unbounded" is now bounded for processes and mitigated for CPU.

*Consequences (added).*
- **The pump is not the limit.** It carried 100 MB/s down and 59 MB/s up (124 %
  and 91 % of one CPU, about 12 ms of CPU per MB), and 28,000 small exchanges
  per second at 100 connections. The "pump measures too slow" condition does not
  fire. A child's bulk egress is CPU that every other app shares.
- **The container has a CPU quota of about 2 of its 4 visible CPUs, invisible
  from inside** (no cgroup file; `/proc/stat` shows idle time). One child with
  busy loops on every CPU makes every app, the edge included, stall for about
  50 ms at a time; no request fails, and `nice` does not help. A launcher that
  pins the sandbox to one CPU (nice 19) and refuses `sched_setaffinity` with
  seccomp keeps the hog from exhausting the quota (paced p95 1.35 ms, against
  50 ms); without the seccomp filter the sandbox widens its own pin. Children on
  the pinned CPU still compete by scheduler share (not measured). Whether to
  pin is the plan's decision. A paid workspace's quota is unknown.
- **Processes are bounded per sandbox.** `RLIMIT_NPROC` is counted in the
  sandbox's user namespace (298 of 300 forks, 34 other processes of the same user
  unaffected). Killing the namespace's init ends 2,001 processes in 0.6 s, but
  PID 1 of the container does not reap orphans, so **the launcher must be a
  subreaper** or each teardown leaves a zombie.
- **A sandbox needs a `resolv.conf`.** The mount table of spike 2 has none, so
  the launcher writes `nameserver 10.0.2.3` (slirp's resolver); with it a build
  that needs the index ran in 2.7 s.
- **Disk:** the `tmpfs` mounts are hard caps (`ENOSPC` at 64 MiB); the host-backed
  run directory is not (1 GiB in 1.5 s; `RLIMIT_FSIZE` is per file). It stays a
  known gap.
