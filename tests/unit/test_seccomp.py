"""The seccomp filter that refuses ``sched_setaffinity`` (design 6.4, spike 7).

The one-CPU pin only holds if the child cannot undo it, so the launcher loads a filter that fails
that single system call. These tests run the filter program through a small interpreter of the
classic BPF instructions it uses (which proves what it decides), and load it into a throw-away
child process of this machine (which proves the kernel accepts it and enforces it).
"""

from __future__ import annotations

import errno
import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest

from appmux.sandbox import seccomp
from appmux.sandbox.seccomp import (
    AUDIT_ARCH_X86_64,
    BPF_JEQ_K,
    BPF_LD_W_ABS,
    BPF_RET_K,
    SECCOMP_RET_ALLOW,
    SECCOMP_RET_ERRNO,
    SECCOMP_RET_KILL_PROCESS,
    SYSCALL_SCHED_SETAFFINITY_X86_64,
    BpfInstruction,
    SeccompError,
    deny_sched_setaffinity,
    deny_syscall_program,
    load_filter,
)

PACKAGE_PARENT = Path(seccomp.__file__).resolve().parents[2]
SECCOMP_DATA_ARCH_OFFSET = 4
SECCOMP_DATA_NR_OFFSET = 0
GETPID_X86_64 = 39
READ_X86_64 = 0


def run_program(program: tuple[BpfInstruction, ...], *, number: int, arch: int) -> int:
    """Interpret the three classic-BPF opcodes the filter uses, as the kernel would."""
    data = {SECCOMP_DATA_NR_OFFSET: number, SECCOMP_DATA_ARCH_OFFSET: arch}
    accumulator = 0
    counter = 0
    while True:
        instruction = program[counter]
        counter += 1
        if instruction.code == BPF_LD_W_ABS:
            accumulator = data[instruction.operand]
        elif instruction.code == BPF_JEQ_K:
            counter += (
                instruction.jump_true
                if accumulator == instruction.operand
                else instruction.jump_false
            )
        elif instruction.code == BPF_RET_K:
            return instruction.operand
        else:  # pragma: no cover - a test guard: the filter must use only these three opcodes
            raise AssertionError(f"unexpected opcode {instruction.code:#x}")


def test_the_named_call_fails_with_eperm() -> None:
    program = deny_syscall_program(SYSCALL_SCHED_SETAFFINITY_X86_64)

    verdict = run_program(program, number=SYSCALL_SCHED_SETAFFINITY_X86_64, arch=AUDIT_ARCH_X86_64)

    assert verdict == SECCOMP_RET_ERRNO | errno.EPERM


@pytest.mark.parametrize("number", [GETPID_X86_64, READ_X86_64, 202, 204])
def test_every_other_call_is_allowed_including_the_neighbours_of_the_named_one(number: int) -> None:
    program = deny_sched_setaffinity()

    verdict = run_program(program, number=number, arch=AUDIT_ARCH_X86_64)

    assert verdict == SECCOMP_RET_ALLOW


def test_another_architecture_is_killed_because_its_numbers_differ() -> None:
    program = deny_sched_setaffinity()
    aarch64 = 0xC00000B7

    verdict = run_program(program, number=GETPID_X86_64, arch=aarch64)

    assert verdict == SECCOMP_RET_KILL_PROCESS


def test_an_instruction_packs_to_the_eight_bytes_of_struct_sock_filter() -> None:
    packed = BpfInstruction(code=0x15, jump_true=1, jump_false=2, operand=0xC000003E).pack()

    assert packed == bytes([0x15, 0x00, 1, 2, 0x3E, 0x00, 0x00, 0xC0])
    assert all(len(i.pack()) == 8 for i in deny_sched_setaffinity())


def test_the_errno_of_the_refusal_can_be_chosen() -> None:
    program = deny_syscall_program(SYSCALL_SCHED_SETAFFINITY_X86_64, errno_value=errno.EACCES)

    verdict = run_program(program, number=SYSCALL_SCHED_SETAFFINITY_X86_64, arch=AUDIT_ARCH_X86_64)

    assert verdict == SECCOMP_RET_ERRNO | errno.EACCES


def test_load_filter_hands_the_kernel_the_whole_program_in_order() -> None:
    installed: list[bytes] = []
    program = deny_sched_setaffinity()

    load_filter(program, install=installed.append)

    assert installed == [b"".join(i.pack() for i in program)]


def test_a_kernel_refusal_is_raised_and_names_the_cause() -> None:
    def refuse(_: bytes) -> None:
        raise OSError(errno.EACCES, "Permission denied")

    with pytest.raises(SeccompError, match="no_new_privs") as raised:
        load_filter(deny_sched_setaffinity(), install=refuse)

    assert isinstance(raised.value.__cause__, OSError)


KERNEL_CHECK = r"""
import ctypes, errno, os, sys
libc = ctypes.CDLL(None, use_errno=True)
assert libc.prctl(38, 1, 0, 0, 0) == 0  # PR_SET_NO_NEW_PRIVS: needed without CAP_SYS_ADMIN
from appmux.sandbox.seccomp import deny_sched_setaffinity, load_filter
load_filter(deny_sched_setaffinity())
try:
    os.sched_setaffinity(0, {sorted(os.sched_getaffinity(0))[0]})
except PermissionError:
    refused = True
else:
    refused = False
child = os.fork()
if child == 0:
    try:
        os.sched_setaffinity(0, {sorted(os.sched_getaffinity(0))[0]})
    except PermissionError:
        os._exit(0)
    os._exit(1)
_, status = os.waitpid(child, 0)
child_refused = os.WEXITSTATUS(status) == 0
print("refused" if refused else "allowed", "child-refused" if child_refused else "child-allowed")
print(os.getpid() > 0, os.getppid() > 0)  # other calls still work
"""


@pytest.mark.skipif(platform.machine() != "x86_64", reason="the filter checks for the x86-64 ABI")
def test_the_kernel_enforces_the_filter_for_the_process_and_its_children() -> None:
    result = subprocess.run(
        [sys.executable, "-c", KERNEL_CHECK],
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONPATH": str(PACKAGE_PARENT), "PATH": os.environ.get("PATH", "")},
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["refused child-refused", "True True"]
