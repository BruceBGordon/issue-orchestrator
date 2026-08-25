"""Platform kernel adapters for collision-resistant process identity."""

from __future__ import annotations

import ctypes
import errno
import sys
from dataclasses import dataclass
from pathlib import Path

from ..domain.process_group import (
    ProcessBirthIdentity,
    ProcessIdentityAbsent,
    ProcessIdentityObservation,
    ProcessIdentityPermissionDenied,
    ProcessIdentityPresent,
)


class KernelProcessIdentityError(RuntimeError):
    """The platform identity source could not provide trustworthy evidence."""


class _DarwinProcBsdInfo(ctypes.Structure):
    _fields_ = (
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    )


class DarwinKernelProcessIdentityObserver:
    """Read process birth timeval and group from macOS ``proc_pidinfo``."""

    _PROC_PIDTBSDINFO = 3

    def __init__(self, libproc_path: Path) -> None:
        if not libproc_path.is_absolute():
            raise ValueError(
                "DarwinKernelProcessIdentityObserver.libproc_path must be absolute"
            )
        try:
            library = ctypes.CDLL(str(libproc_path), use_errno=True)
        except OSError as exc:
            raise KernelProcessIdentityError(
                f"could not load macOS process identity library {libproc_path}"
            ) from exc
        function = library.proc_pidinfo
        function.argtypes = (
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        )
        function.restype = ctypes.c_int
        self._library = library
        self._proc_pidinfo = function

    def observe_process(self, process_id: int) -> ProcessIdentityObservation:
        _require_process_id(process_id)
        info = _DarwinProcBsdInfo()
        size = ctypes.sizeof(info)
        ctypes.set_errno(0)
        returned = self._proc_pidinfo(
            process_id,
            self._PROC_PIDTBSDINFO,
            0,
            ctypes.byref(info),
            size,
        )
        if returned == size:
            if info.pbi_pid != process_id:
                raise KernelProcessIdentityError(
                    "proc_pidinfo returned a different process identity: "
                    f"requested={process_id} observed={info.pbi_pid}"
                )
            return ProcessIdentityPresent(
                ProcessBirthIdentity(
                    f"darwin-timeval:{info.pbi_start_tvsec}:"
                    f"{info.pbi_start_tvusec}"
                ),
                int(info.pbi_pgid),
            )
        error_number = ctypes.get_errno()
        if returned == 0 and error_number in (0, errno.ESRCH):
            return ProcessIdentityAbsent()
        if error_number in (errno.EPERM, errno.EACCES):
            return ProcessIdentityPermissionDenied(
                f"proc_pidinfo errno={error_number}"
            )
        raise KernelProcessIdentityError(
            "proc_pidinfo returned an incomplete process identity: "
            f"pid={process_id} bytes={returned} expected={size} errno={error_number}"
        )


@dataclass(frozen=True, slots=True)
class LinuxProcProcessIdentityObserver:
    """Read process birth ticks and group from one Linux procfs record."""

    proc_root: Path

    def __post_init__(self) -> None:
        if not self.proc_root.is_absolute():
            raise ValueError(
                "LinuxProcProcessIdentityObserver.proc_root must be absolute"
            )

    def observe_process(self, process_id: int) -> ProcessIdentityObservation:
        _require_process_id(process_id)
        stat_path = self.proc_root / str(process_id) / "stat"
        try:
            raw_stat = stat_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ProcessIdentityAbsent()
        except PermissionError as exc:
            return ProcessIdentityPermissionDenied(repr(exc))
        except OSError as exc:
            raise KernelProcessIdentityError(
                f"could not read Linux process identity at {stat_path}"
            ) from exc
        closing_parenthesis = raw_stat.rfind(")")
        if closing_parenthesis < 0:
            raise KernelProcessIdentityError(
                f"malformed Linux process identity at {stat_path}"
            )
        fields = raw_stat[closing_parenthesis + 1 :].split()
        if len(fields) <= 19:
            raise KernelProcessIdentityError(
                f"incomplete Linux process identity at {stat_path}"
            )
        try:
            process_group_id = int(fields[2])
            start_ticks = int(fields[19])
        except ValueError as exc:
            raise KernelProcessIdentityError(
                f"malformed Linux process identity at {stat_path}"
            ) from exc
        return ProcessIdentityPresent(
            ProcessBirthIdentity(f"linux-boot-ticks:{start_ticks}"),
            process_group_id,
        )


def build_kernel_process_identity_observer() -> (
    DarwinKernelProcessIdentityObserver | LinuxProcProcessIdentityObserver
):
    """Select the one exact kernel identity source supported by this host."""
    if sys.platform == "darwin":
        return DarwinKernelProcessIdentityObserver(Path("/usr/lib/libproc.dylib"))
    if sys.platform.startswith("linux"):
        return LinuxProcProcessIdentityObserver(Path("/proc"))
    raise RuntimeError(
        f"exact process birth identity is unsupported on {sys.platform!r}"
    )


def _require_process_id(process_id: int) -> None:
    if type(process_id) is not int or process_id <= 1:
        raise ValueError("process_id must be an integer above 1")
