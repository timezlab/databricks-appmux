"""The seccomp filter that stops a child from undoing its one-CPU pin (design 6.4, spike 7).

The container has a CPU quota it cannot see; pinning the sandbox to one CPU at nice 19 keeps a
busy-loop child from exhausting it. Spike 7 measured that the pin alone is not enough: a child
that calls ``sched_setaffinity`` widens its own pin and is back at 50 ms stalls for everyone. So
the launcher also loads a filter that makes that one system call fail with ``EPERM`` and lets
every other call through. A seccomp filter is inherited across ``fork`` and ``exec`` and cannot
be removed, so it covers the child and everything it starts.

The program is classic BPF over ``struct seccomp_data``: load the architecture word and kill the
process on anything but x86-64 (the call numbers below are that ABI's, and guessing on another
would silently filter the wrong call), load the call number, return ``EPERM`` for the named call
and ``ALLOW`` for the rest. Installing it needs ``no_new_privs`` first (the launcher sets it when
it drops capabilities), or a capability the child no longer has.

Ported from ``op_deny_syscall`` in ``specs/001-appmux-v1/spikes/scripts/spike-07/load_ops.py``.
"""

from __future__ import annotations

import ctypes
import errno
import struct
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

SYSCALL_SCHED_SETAFFINITY_X86_64: Final = 203  # asm/unistd_64.h
AUDIT_ARCH_X86_64: Final = 0xC000003E

SECCOMP_RET_ALLOW: Final = 0x7FFF0000
SECCOMP_RET_KILL_PROCESS: Final = 0x80000000
SECCOMP_RET_ERRNO: Final = 0x00050000

PR_SET_SECCOMP: Final = 22
SECCOMP_MODE_FILTER: Final = 2

# Classic BPF opcodes: load a 32-bit word at an absolute offset, jump if equal to a constant,
# return a constant.
BPF_LD_W_ABS: Final = 0x20
BPF_JEQ_K: Final = 0x15
BPF_RET_K: Final = 0x06

# Offsets into ``struct seccomp_data``.
SECCOMP_DATA_NR_OFFSET: Final = 0
SECCOMP_DATA_ARCH_OFFSET: Final = 4

_SOCK_FILTER_FORMAT: Final = "<HBBI"  # struct sock_filter: code, jt, jf, k


class SeccompError(OSError):
    """The kernel refused the filter. The sandbox must not start without it."""


@dataclass(frozen=True, slots=True)
class BpfInstruction:
    """One ``struct sock_filter``."""

    code: int
    jump_true: int
    jump_false: int
    operand: int

    def pack(self) -> bytes:
        return struct.pack(
            _SOCK_FILTER_FORMAT, self.code, self.jump_true, self.jump_false, self.operand
        )


def deny_syscall_program(
    syscall_number: int, *, errno_value: int = errno.EPERM
) -> tuple[BpfInstruction, ...]:
    """A program that fails ``syscall_number`` with ``errno_value`` and allows every other call."""
    return (
        BpfInstruction(BPF_LD_W_ABS, 0, 0, SECCOMP_DATA_ARCH_OFFSET),
        # x86-64: skip the kill. Any other architecture falls through to it.
        BpfInstruction(BPF_JEQ_K, 1, 0, AUDIT_ARCH_X86_64),
        BpfInstruction(BPF_RET_K, 0, 0, SECCOMP_RET_KILL_PROCESS),
        BpfInstruction(BPF_LD_W_ABS, 0, 0, SECCOMP_DATA_NR_OFFSET),
        # The named call falls through to the refusal; any other call skips it.
        BpfInstruction(BPF_JEQ_K, 0, 1, syscall_number),
        BpfInstruction(BPF_RET_K, 0, 0, SECCOMP_RET_ERRNO | errno_value),
        BpfInstruction(BPF_RET_K, 0, 0, SECCOMP_RET_ALLOW),
    )


def deny_sched_setaffinity() -> tuple[BpfInstruction, ...]:
    """The filter of design 6.4: ``sched_setaffinity`` fails with ``EPERM``, nothing else does."""
    return deny_syscall_program(SYSCALL_SCHED_SETAFFINITY_X86_64)


class _SockFilter(ctypes.Structure):
    _fields_ = (
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint),
    )


class SockFprog(ctypes.Structure):
    _fields_ = (("length", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter)))


def install_filter(packed_program: bytes) -> None:
    """``prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER)`` for this process and its future children."""
    count = len(packed_program) // struct.calcsize(_SOCK_FILTER_FORMAT)
    instructions = (_SockFilter * count).from_buffer_copy(packed_program)
    program = SockFprog(count, instructions)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(program), 0, 0) != 0:
        code = ctypes.get_errno()
        raise OSError(code, f"prctl(PR_SET_SECCOMP) failed: {errno.errorcode.get(code, code)}")


def load_filter(
    program: Sequence[BpfInstruction],
    *,
    install: Callable[[bytes], None] = install_filter,
) -> None:
    """Load ``program`` into this process; raises :class:`SeccompError` when the kernel refuses.

    ``install`` is the seam for the system call, so a test can check what would be sent without
    filtering the test runner.
    """
    try:
        install(b"".join(instruction.pack() for instruction in program))
    except OSError as exc:
        raise SeccompError(
            exc.errno,
            "the kernel refused the seccomp filter; set no_new_privs before loading it "
            f"({exc.strerror or exc})",
        ) from exc
