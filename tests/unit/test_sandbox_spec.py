"""The sandbox specification of design 6.1: what the launcher asks of the kernel, as data.

Nothing here needs a container. The ``unshare`` command line, the child's root as an ordered
plan of mounts, and the one switch that weakens a layer (``omit_layers``) are all values the
launcher computes before it touches the system, so they are checked as values. What the kernel
does with them (that the namespaces exist, that the mounts hold) is the canary's job, in a
container (``tests/integration``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from appmux.sandbox.launcher import (
    UNSHARE_FLAGS,
    Layer,
    SandboxSpec,
    child_path,
    child_python_path,
    root_layout_for,
    unshare_command,
)
from appmux.sandbox.rootfs import (
    BUILD_OUTPUT_PATH,
    SHM_PATH,
    TMP_PATH,
    VAR_TMP_PATH,
    BindMount,
    HostFile,
    Overmount,
    ProcSelf,
    RootPlan,
    Symlink,
    TmpfsMount,
    WrittenFile,
    overlay_path,
    plan_rootfs,
    tmpfs_mount_data,
)
from appmux.sandbox.run_dir import RUN_DIR_MOUNT_PATH, RUN_DIR_SIZE_MB

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "appmux"

HOST_APPMUX = Path(TMP_PATH) / "appmux"
APP_ENVIRONMENT = Path("/app/python/source_code/.venv")
RELEASE = HOST_APPMUX / "releases/shop-1"
OVERLAY_A = HOST_APPMUX / "overlays/aaaa"
OVERLAY_B = HOST_APPMUX / "overlays/bbbb"
ETC_FILES = {
    "ld.so.cache",
    "ld.so.conf",
    "ld.so.conf.d",
    "ssl",
    "ca-certificates",
    "nsswitch.conf",
    "passwd",
    "group",
    "localtime",
    "os-release",
}


def make_spec(**overrides: Any) -> SandboxSpec:
    fields: dict[str, Any] = {
        "slug": "shop",
        "command": ("python", "-m", "uvicorn", "app:app"),
        "environment": {"DATABRICKS_APP_PORT": "8000", "SECRET_TOKEN": "s3cret-value"},
        "app_environment": APP_ENVIRONMENT,
        "release_dir": RELEASE,
        "scratch_dir": HOST_APPMUX / "run/shop",
        "log_path": HOST_APPMUX / "run/shop/child.log",
        "rules_path": HOST_APPMUX / "run/shop/rules.json",
    }
    fields.update(overrides)
    return SandboxSpec(**fields)


# ---- the unshare command line ------------------------------------------------------------------


def test_unshare_command_names_every_namespace_and_the_three_launcher_flags() -> None:
    command = unshare_command(["/usr/bin/python3", "-I", "-c", "stage1"])

    flags = command[1 : command.index("--")]
    for namespace in ("--user", "--mount", "--net", "--pid", "--ipc"):
        assert namespace in flags
    # --ipc hides SysV shared memory and queues (spike 2); --fork is required for the pid
    # namespace; --kill-child ends the sandbox when the launcher dies.
    for launcher_flag in ("--map-root-user", "--fork", "--kill-child"):
        assert launcher_flag in flags


def test_unshare_command_runs_the_stage_one_program_after_the_separator() -> None:
    stage1 = ["/usr/bin/python3", "-I", "-c", "stage one source"]

    command = unshare_command(stage1)

    assert command[0] == "unshare"
    assert command[command.index("--") + 1 :] == stage1
    assert command.count("--") == 1


def test_unshare_command_has_no_flag_that_is_not_a_fixed_namespace_or_launcher_flag() -> None:
    # A flag added by accident (--cgroup, --uts, --map-current-user) was not measured: the
    # design keeps the sequence that spike 2 and 6 ran.
    command = unshare_command(["stage1"])

    assert tuple(command[1 : command.index("--")]) == UNSHARE_FLAGS
    assert set(UNSHARE_FLAGS) == {
        "--user",
        "--map-root-user",
        "--net",
        "--mount",
        "--pid",
        "--ipc",
        "--fork",
        "--kill-child",
    }


def test_unshare_command_is_an_argument_list_and_does_not_interpret_its_arguments() -> None:
    hostile = "; rm -rf / #"

    command = unshare_command(["python", hostile])

    assert command[-1] == hostile
    assert all(isinstance(part, str) for part in command)


# ---- the spec as a value -----------------------------------------------------------------------


def test_a_spec_survives_the_json_hop_into_stage_one() -> None:
    spec = make_spec(
        overlays=(OVERLAY_A, OVERLAY_B),
        build_output=HOST_APPMUX / "overlays/cccc",
        relay_socket_path=HOST_APPMUX / "run/shop/relay.sock",
        child_port=8000,
        max_processes=64,
        memory_budget_mb=256,
        pinned_cpu=3,
        wall_clock_limit_s=600.0,
        omit_layers=frozenset({Layer.CAPABILITY_DROP}),
    )

    assert SandboxSpec.from_json(spec.to_json()) == spec


def test_a_spec_never_prints_the_values_of_its_environment() -> None:
    spec = make_spec()

    text = f"{spec!r} {spec}"

    assert "s3cret-value" not in text
    assert "SECRET_TOKEN" in text  # the names are fine to show


def test_a_spec_refuses_a_command_that_is_empty() -> None:
    with pytest.raises(ValueError, match="command"):
        make_spec(command=())


def test_a_spec_refuses_a_cpu_pin_below_zero_and_a_process_limit_below_one() -> None:
    with pytest.raises(ValueError, match="pinned_cpu"):
        make_spec(pinned_cpu=-1)
    with pytest.raises(ValueError, match="max_processes"):
        make_spec(max_processes=0)


# ---- omit_layers: a constructor argument and nothing else --------------------------------------


def test_omit_layers_defaults_to_every_layer_in_force() -> None:
    assert make_spec().omit_layers == frozenset()


def test_the_environment_cannot_weaken_a_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("APPMUX_OMIT_LAYERS", "OMIT_LAYERS", "APPMUX_OMIT_LAYER"):
        monkeypatch.setenv(name, ",".join(layer.value for layer in Layer))

    assert make_spec().omit_layers == frozenset()


def test_the_json_hop_is_the_only_other_way_in_and_it_carries_exactly_what_was_given() -> None:
    document = json.loads(make_spec().to_json())
    assert document["omit_layers"] == []

    document["omit_layers"] = ["not-a-layer"]
    with pytest.raises(ValueError, match="omit_layers"):
        SandboxSpec.from_json(json.dumps(document))


def test_only_the_launcher_reads_omit_layers() -> None:
    # "nothing else reads it": no registry, settings or reconciler code can turn a layer off.
    mentions = {
        path.relative_to(SOURCE_ROOT).as_posix()
        for path in SOURCE_ROOT.rglob("*.py")
        if re.search(r"\bomit_layers\b", path.read_text(encoding="utf-8"))
    }

    assert mentions == {"sandbox/launcher.py"}


# ---- the child's root: the allow-list as data --------------------------------------------------


def plan_for(**overrides: Any) -> RootPlan:
    return plan_rootfs(root_layout_for(make_spec(**overrides)))


def test_exactly_five_directories_are_writable() -> None:
    plan = plan_for()

    assert plan.writable_directories() == {
        RUN_DIR_MOUNT_PATH,
        TMP_PATH,
        VAR_TMP_PATH,
        SHM_PATH,
        "/home/app",
    }


def test_the_private_temp_directories_are_capped_tmpfs() -> None:
    sizes = {step.target: step.size_mb for step in plan_for().steps if isinstance(step, TmpfsMount)}

    assert sizes[TMP_PATH] == 64
    assert sizes[VAR_TMP_PATH] == 16
    assert sizes[SHM_PATH] == 64
    assert sizes["/home/app"] == 64
    assert sizes[RUN_DIR_MOUNT_PATH] == RUN_DIR_SIZE_MB


def test_every_writable_tmpfs_of_the_plan_is_limited_in_files_as_well_as_in_bytes() -> None:
    limits = {
        step.target: tmpfs_mount_data(step)
        for step in plan_for().steps
        if isinstance(step, TmpfsMount) and step.writable
    }

    assert {target: _file_limit_of(data) for target, data in limits.items()} == {
        RUN_DIR_MOUNT_PATH: 16384,
        TMP_PATH: 16384,
        VAR_TMP_PATH: 4096,
        SHM_PATH: 16384,
        "/home/app": 16384,
    }
    # The set of writable directories above is the set that has a limit: a new writable mount
    # without one fails here.
    assert set(limits) == plan_for().writable_directories()


def _file_limit_of(mount_data: str) -> int | None:
    options = dict(option.split("=", 1) for option in mount_data.split(","))
    return int(options["nr_inodes"]) if "nr_inodes" in options else None


def test_the_run_directory_is_a_host_directory_only_when_one_is_given() -> None:
    host_run_dir = HOST_APPMUX / "run/shop/rundir"

    capped = [s for s in plan_for().steps if s.target == RUN_DIR_MOUNT_PATH]
    host_backed = [
        s for s in plan_for(host_run_dir=host_run_dir).steps if s.target == RUN_DIR_MOUNT_PATH
    ]

    assert [type(s) for s in capped] == [TmpfsMount]
    assert host_backed == [BindMount(source=host_run_dir, target=RUN_DIR_MOUNT_PATH, writable=True)]


def test_the_package_environment_every_overlay_and_the_release_are_read_only() -> None:
    plan = plan_for(overlays=(OVERLAY_A, OVERLAY_B))
    binds = {s.target: s for s in plan.steps if isinstance(s, BindMount)}

    assert binds["/usr"] == BindMount(Path("/usr"), "/usr")
    assert binds[str(APP_ENVIRONMENT)] == BindMount(APP_ENVIRONMENT, str(APP_ENVIRONMENT))
    # An overlay lives under /srv/overlay, not at its host path (which is under the host's /tmp,
    # where the child's own writable /tmp would swallow it).
    assert binds[overlay_path(0)] == BindMount(OVERLAY_A, overlay_path(0))
    assert binds[overlay_path(1)] == BindMount(OVERLAY_B, overlay_path(1))
    assert binds["/srv/app"] == BindMount(RELEASE, "/srv/app")
    for target in ("/usr", str(APP_ENVIRONMENT), overlay_path(0), overlay_path(1), "/srv/app"):
        assert binds[target].writable is False


def test_a_build_gets_one_writable_output_directory_and_nothing_else_writable_from_the_host() -> (
    None
):
    output = HOST_APPMUX / "overlays/cccc"

    plan = plan_for(build_output=output)

    assert BindMount(output, BUILD_OUTPUT_PATH, writable=True) in plan.steps
    host_writable = {s.target for s in plan.steps if isinstance(s, BindMount) and s.writable}
    assert host_writable == {BUILD_OUTPUT_PATH}


def test_usr_symlinks_bin_sbin_lib_and_lib64() -> None:
    links = {s.target: s.destination for s in plan_for().steps if isinstance(s, Symlink)}

    assert links["/bin"] == "usr/bin"
    assert links["/sbin"] == "usr/sbin"
    assert links["/lib"] == "usr/lib"
    assert links["/lib64"] == "usr/lib64"


def test_etc_holds_the_ten_listed_files_and_two_files_the_launcher_writes() -> None:
    steps = plan_for().steps
    etc_binds = {
        s.target.removeprefix("/etc/"): s
        for s in steps
        if isinstance(s, BindMount) and s.target.startswith("/etc/")
    }
    etc_written = {s.target: s.content for s in steps if isinstance(s, WrittenFile)}

    assert set(etc_binds) == ETC_FILES
    assert all(not s.writable for s in etc_binds.values())
    # slirp's resolver; without this line a name lookup fails (spike 7).
    assert etc_written["/etc/resolv.conf"] == "nameserver 10.0.2.3\n"
    # A constant line with no host information: "localhost" must resolve for apps that use the name.
    assert etc_written["/etc/hosts"] == "127.0.0.1 localhost\n"


def test_dev_has_only_five_device_nodes_and_the_standard_descriptor_links() -> None:
    steps = plan_for().steps

    assert {
        s.target for s in steps if isinstance(s, HostFile) and s.target.startswith("/dev/")
    } == {
        "/dev/null",
        "/dev/zero",
        "/dev/full",
        "/dev/random",
        "/dev/urandom",
    }
    links = {s.target: s.destination for s in steps if isinstance(s, Symlink)}
    assert links["/dev/stdin"] == "/proc/self/fd/0"
    assert links["/dev/stdout"] == "/proc/self/fd/1"
    assert links["/dev/stderr"] == "/proc/self/fd/2"
    assert links["/dev/fd"] == "/proc/self/fd"


def test_proc_shows_cpuinfo_meminfo_and_self_and_hides_the_files_that_name_the_host() -> None:
    steps = plan_for().steps
    proc_binds = {
        s.target for s in steps if isinstance(s, HostFile) and s.target.startswith("/proc/")
    }
    overmounted = {s.target for s in steps if isinstance(s, Overmount)}

    assert ProcSelf("/proc/self") in steps
    assert proc_binds == {"/proc/cpuinfo", "/proc/meminfo"}
    assert overmounted == {
        "/proc/self/cgroup",
        "/proc/self/mountinfo",
        "/proc/self/mounts",
        "/proc/self/mountstats",
    }


def test_omitting_the_proc_overmount_leaves_those_files_visible_and_changes_nothing_else() -> None:
    full = plan_for()
    weakened = plan_for(omit_layers=frozenset({Layer.PROC_OVERMOUNT}))

    assert not [s for s in weakened.steps if isinstance(s, Overmount)]
    kept = [s for s in full.steps if not isinstance(s, Overmount)]
    assert list(weakened.steps) == kept


def test_nothing_of_the_platform_exists_in_the_root() -> None:
    # The app environment lives inside the platform's app directory, and a virtual environment
    # only works at its own path (its scripts name the interpreter by absolute path). So that one
    # mount is the single thing under /app; the app's source and everything else there is absent.
    targets = [
        s.target
        for s in plan_for().steps
        if hasattr(s, "target") and s.target != str(APP_ENVIRONMENT)
    ]

    for forbidden in ("/databricks", "/sys", "/mnt", "/etc/hostname", "/app", "/root"):
        assert not [t for t in targets if t == forbidden or t.startswith(forbidden + "/")], (
            forbidden
        )
    sources = [str(s.source) for s in plan_for().steps if isinstance(s, BindMount)]
    assert not [source for source in sources if source.startswith("/databricks")]


def test_a_mount_point_is_always_mounted_after_its_parent() -> None:
    steps = plan_for(overlays=(OVERLAY_A,), build_output=HOST_APPMUX / "overlays/cccc").steps
    assert [s for s in steps if hasattr(s, "target") and s.target.startswith(TMP_PATH + "/")] == []
    mounted = [(i, s.target) for i, s in enumerate(steps) if hasattr(s, "target")]

    for index, target in mounted:
        for parent_index, parent in mounted:
            if target.startswith(parent.rstrip("/") + "/"):
                assert parent_index < index, f"{parent} must come before {target}"


def test_a_target_is_planned_once() -> None:
    steps = plan_for(overlays=(OVERLAY_A,)).steps
    targets = [s.target for s in steps if hasattr(s, "target")]

    assert len(targets) == len(set(targets))


# ---- the child's PATH and PYTHONPATH -----------------------------------------------------------


def test_the_overlays_come_first_on_both_paths_and_in_the_order_given() -> None:
    spec = make_spec(overlays=(OVERLAY_A, OVERLAY_B))

    assert child_python_path(spec) == f"{overlay_path(0)}:{overlay_path(1)}"
    assert child_path(spec).split(":") == [
        f"{overlay_path(0)}/bin",
        f"{overlay_path(1)}/bin",
        f"{APP_ENVIRONMENT}/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]


def test_a_release_without_an_overlay_has_no_pythonpath() -> None:
    spec = make_spec()

    assert child_python_path(spec) is None
    assert child_path(spec).split(":")[0] == f"{APP_ENVIRONMENT}/bin"
