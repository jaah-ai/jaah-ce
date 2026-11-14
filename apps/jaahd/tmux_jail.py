#!/usr/bin/env python3
"""tmux_jail.py — exec a tmux COMMAND client that cannot touch the filesystem (Linux Landlock, macOS Seatbelt).
Stdlib-only.

Why: tmux does a command client's file I/O in the CLIENT process (`MSG_READ_OPEN`/`MSG_WRITE_OPEN`). On a
fleet server the session user can reach (a non-isolated node, or the pre-isolation server still draining), a
runner pane can set a hook — `set-hook -g after-list-windows 'save-buffer -a <path>'`, or `load-buffer <path>`
to read — that fires with the daemon's next command client as its client: the file is then written or read as
`jaahd`. Reproduced on tmux 3.3a and 3.6 (`tests/test_tmux_jail.py`).

A runner can also bind a server of its own at a socket path it owns, and a "server" may answer `MSG_EXEC`: the
client then `exec`s a shell command. So `session_user.tmux_cmd` runs such a client as `python3 -I -S
tmux_jail.py tmux …`: this script restricts itself with Landlock and execs tmux. Granted: read under the
tmux binary's prefix and the system library roots, plus the three `/etc` files the loader and libc read; EXECUTE
of the tmux binary and its ELF interpreter only. Denied: every write, create and bind; executing anything else;
TCP (ABI 4+); signals and abstract sockets outside the client (ABI 6+). A `connect()` to the server's pathname
socket is not a Landlock right, so the client still works — which is also why no other program may run. Already-
open descriptors (stdin/stdout) are untouched, so `-` streams work.

On macOS (`sandbox-exec`, the Seatbelt profile `darwin_profile`, on Apple's `system.sb` baseline): the same grants — read under the tmux prefix and
the system roots, metadata anywhere, read/write of `/dev/null` only, exec of the tmux binary only, connect to its
`-S` socket only, no TCP, signals to itself — and everything else denied. The tmux must be self-contained under
its prefix, as the build macOS nodes ship is (`packaging/platform-pins.toml`): a Homebrew tmux, whose libraries
live in other kegs, fails closed under it.

On Windows (`run_windows_jailed`): no exec, so this script stays as the client's parent — the client runs AS THE
RUNNER ACCOUNT (logged on from the installer's `--logon` file), so it reaches only what the runner already could,
in a job capped at one process (its section says why not a restricted token, an AppContainer or low integrity).
Where no jail can run (a kernel without Landlock, a Windows node without the logon file)
`session_user.jail_gap` names the gap and `tmux_jail_health` reports it as a node Health finding.

Usage: `tmux_jail.py [--logon <file>] <tmux> [args…]` (the file on Windows only). Exits 126 without running
tmux when the jail cannot be applied: fail closed,
never an unjailed client. `available()` is the daemon's check, so it only wraps where this can succeed.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import struct
import subprocess
import sys

# Syscall numbers: Landlock postdates the syscall-table unification, so they are the same on every architecture.
_SYS_CREATE_RULESET, _SYS_ADD_RULE, _SYS_RESTRICT_SELF = 444, 445, 446
_CREATE_RULESET_VERSION = 1
_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

# `LANDLOCK_ACCESS_FS_*`.
_EXECUTE, _WRITE_FILE, _READ_FILE, _READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
_ABI1_ALL = (1 << 13) - 1  # EXECUTE … MAKE_SYM
_REFER, _TRUNCATE, _IOCTL_DEV = 1 << 13, 1 << 14, 1 << 15  # ABI 2, 3, 5
_NET_BIND_TCP, _NET_CONNECT_TCP = 1 << 0, 1 << 1  # `LANDLOCK_ACCESS_NET_*`, ABI 4
_SCOPE_ABSTRACT_UNIX_SOCKET, _SCOPE_SIGNAL = 1 << 0, 1 << 1  # `LANDLOCK_SCOPE_*`, ABI 6

# Read roots: where the dynamic loader, shared libraries, libc's locale data and gconv modules live.
_LIBRARY_ROOTS = ("/usr", "/lib", "/lib32", "/lib64", "/bin", "/sbin")
# The only `/etc` files a command client reads (strace of 3.3a and 3.6). Nothing else under `/etc`: a node's
# own secrets may live there.
_ETC_FILES = ("/etc/ld.so.cache", "/etc/ld.so.preload", "/etc/localtime")


# `struct landlock_ruleset_attr` — `handled_access_fs`, `handled_access_net` (ABI 4), `scoped` (ABI 6); an older
# kernel takes the shorter struct — and the packed `struct landlock_path_beneath_attr`, in native byte order.
_PATH_BENEATH_ATTR = "=Qi"


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def abi() -> int:
    """The kernel's Landlock ABI version; 0 when the platform, kernel or LSM list has none."""
    if not sys.platform.startswith("linux"):
        return 0
    try:
        version = _libc().syscall(ctypes.c_long(_SYS_CREATE_RULESET), None, ctypes.c_size_t(0),
                                  ctypes.c_uint32(_CREATE_RULESET_VERSION))
    except (OSError, AttributeError):
        return 0
    return max(int(version), 0)


SANDBOX_EXEC = "/usr/bin/sandbox-exec"
_AVAILABLE: list[bool] = []


def _supported() -> bool:
    if sys.platform == "darwin":
        return os.access(SANDBOX_EXEC, os.X_OK)
    if sys.platform == "win32":
        try:  # read, not stat: `os.access` ignores a Windows access list
            read_logon(windows_logon_file())
        except (OSError, ValueError):
            return False
        return True
    return abi() >= 1


def available() -> bool:
    """Whether a jailed client can run here. Cached: the kernel's answer does not change while it runs."""
    if not _AVAILABLE:
        _AVAILABLE.append(_supported())
    return _AVAILABLE[0]


def unavailable_reason() -> str:
    """Why `available()` is False here, in words for a node's Health finding."""
    if sys.platform == "darwin":
        return f"{SANDBOX_EXEC} is missing"
    if sys.platform.startswith("linux"):
        return "the kernel has no Landlock (5.13+, with landlock in the lsm= list)"
    if sys.platform == "win32":
        return (f"{windows_logon_file()} (the runner account's logon, which the installer writes) is unreadable — "
                "run the Jaah installer again")
    return f"{sys.platform} has no tmux client jail"


def windows_logon_file() -> str:
    """The runner account's logon file a Windows daemon's jailed clients run as (`run_windows_jailed`)."""
    from host_platform import windows  # noqa: PLC0415 — Windows-only, and the daemon's import path

    return windows.runner_logon_file()


def _handled(version: int) -> int:
    mask = _ABI1_ALL
    if version >= 2:
        mask |= _REFER
    if version >= 3:
        mask |= _TRUNCATE
    if version >= 5:
        mask |= _IOCTL_DEV
    return mask


def _check(rc: int, what: str) -> int:
    if rc < 0:
        err = ctypes.get_errno()
        raise OSError(err, f"{what}: {os.strerror(err)}")
    return rc


def read_roots(tmux: str) -> list[str]:
    """The directories a jailed client may read under: the tmux binary's prefix (`/usr` for `/usr/bin/tmux`; a
    private build's own tree, its libraries included) and `_LIBRARY_ROOTS`. The prefix only for a `<prefix>/bin/`
    binary, and never `/`: a `/bin/tmux` on a host without merged `/usr` would otherwise open the whole
    filesystem to a hook's reads."""
    binary_dir = os.path.dirname(os.path.realpath(tmux))
    prefix = os.path.dirname(binary_dir)
    own = [prefix] if os.path.basename(binary_dir) == "bin" and prefix != "/" else []
    return [*own, *_LIBRARY_ROOTS]


_PT_INTERP = 3


def elf_interpreter(path: str) -> str | None:
    """PATH's ELF interpreter (`PT_INTERP`, e.g. `/lib64/ld-linux-x86-64.so.2`), or None for a static or non-ELF
    file. The kernel execs it beside the binary, so it needs EXECUTE too."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(64)
            if len(head) < 52 or head[:4] != b"\x7fELF" or head[4] not in (1, 2) or head[5] not in (1, 2):
                return None
            order = "<" if head[5] == 1 else ">"
            if head[4] == 2:  # ELFCLASS64
                phoff, entsize, count = (struct.unpack_from(order + "Q", head, 0x20)[0],
                                         *struct.unpack_from(order + "HH", head, 0x36))
                at_offset, at_size = order + "Q", order + "Q"
                offset_pos, size_pos = 0x08, 0x20
            else:  # ELFCLASS32
                phoff, entsize, count = (struct.unpack_from(order + "I", head, 0x1C)[0],
                                         *struct.unpack_from(order + "HH", head, 0x2A))
                at_offset, at_size = order + "I", order + "I"
                offset_pos, size_pos = 0x04, 0x10
            for index in range(count):
                handle.seek(phoff + index * entsize)
                entry = handle.read(entsize)
                if struct.unpack_from(order + "I", entry, 0)[0] != _PT_INTERP:
                    continue
                handle.seek(struct.unpack_from(at_offset, entry, offset_pos)[0])
                name = handle.read(struct.unpack_from(at_size, entry, size_pos)[0]).split(b"\0", 1)[0]
                return os.path.realpath(name.decode())
    except (OSError, struct.error, UnicodeDecodeError):
        return None
    return None


def _ruleset_attr(version: int) -> bytes:
    """The ruleset to create: every filesystem right this ABI knows, then TCP and the scopes where it knows them."""
    if version >= 6:
        return struct.pack("=QQQ", _handled(version), _NET_BIND_TCP | _NET_CONNECT_TCP,
                           _SCOPE_ABSTRACT_UNIX_SOCKET | _SCOPE_SIGNAL)
    if version >= 4:
        return struct.pack("=QQ", _handled(version), _NET_BIND_TCP | _NET_CONNECT_TCP)
    return struct.pack("=Q", _handled(version))


def restrict(tmux: str) -> None:
    """Landlock this process for a client of the TMUX binary: read beneath `read_roots`, read of `_ETC_FILES`,
    execute of TMUX and its interpreter, nothing else. Missing paths are skipped. Raises `OSError` when
    Landlock is unavailable or refuses."""
    version = abi()
    if version < 1:
        raise OSError(0, "Landlock is not available on this kernel")
    libc = _libc()
    attr = _ruleset_attr(version)
    ruleset = _check(libc.syscall(ctypes.c_long(_SYS_CREATE_RULESET), attr, ctypes.c_size_t(len(attr)),
                                  ctypes.c_uint32(0)),
                     "landlock_create_ruleset")
    try:
        rules = [(path, _READ_FILE | _READ_DIR) for path in read_roots(tmux)]
        rules += [(path, _READ_FILE) for path in _ETC_FILES]
        rules += [(path, _EXECUTE | _READ_FILE) for path in (os.path.realpath(tmux), elf_interpreter(tmux)) if path]
        for path, access in rules:
            try:
                fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except OSError:
                continue
            try:
                if not os.path.isdir(path):
                    access &= _EXECUTE | _READ_FILE  # a file rule takes file rights only
                beneath = struct.pack(_PATH_BENEATH_ATTR, access, fd)
                _check(libc.syscall(ctypes.c_long(_SYS_ADD_RULE), ctypes.c_int(ruleset),
                                    ctypes.c_int(_RULE_PATH_BENEATH), beneath, ctypes.c_uint32(0)),
                       f"landlock_add_rule({path})")
            finally:
                os.close(fd)
        _check(libc.prctl(ctypes.c_int(_PR_SET_NO_NEW_PRIVS), *(ctypes.c_ulong(v) for v in (1, 0, 0, 0))),
               "prctl(PR_SET_NO_NEW_PRIVS)")
        _check(libc.syscall(ctypes.c_long(_SYS_RESTRICT_SELF), ctypes.c_int(ruleset), ctypes.c_uint32(0)),
               "landlock_restrict_self")
    finally:
        os.close(ruleset)


def _sbpl(path: str) -> str:
    """PATH as an SBPL string literal."""
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def darwin_profile(tmux: str, socket: str) -> str:
    """The Seatbelt profile for a client of the TMUX binary on SOCKET (its `-S`), the one unix socket it may connect
    to. Paths are resolved first: Seatbelt matches the vnode's real path (`/private/var/…`, not `/var/…`)."""
    binary = os.path.realpath(tmux)
    reads = [root for root in read_roots(binary) if root not in _LIBRARY_ROOTS] or [binary]
    return "\n".join((
        "(version 1)",
        "(deny default)",
        # Apple's baseline for system daemons: dyld and its shared cache, libSystem, read of the system roots
        # (`/System`, `/usr/lib`, `/usr/share`, zone data) and a few `/etc` files; writes only to `/dev/null`,
        # `/dev/zero` and new core files under `/cores`; outbound only to syslog. No exec, no TCP.
        '(import "system.sb")',
        f"(allow process-exec (literal {_sbpl(binary)}))",
        "(allow file-read* " + " ".join(f"(subpath {_sbpl(root)})" for root in reads) + ")",
        "(allow file-read-metadata)",
        '(allow file-read* file-write-data (literal "/dev/null"))',
        f"(allow network-outbound (remote unix-socket (path-literal {_sbpl(os.path.realpath(socket))})))",
        "(allow sysctl-read)",
        "(allow signal (target self))",
    ))


def socket_arg(argv: list[str]) -> str:
    """The `-S <socket>` a tmux ARGV names, or "" (a jailed client is always given one: `session_user.tmux_cmd`)."""
    for index, arg in enumerate(argv[1:-1], start=1):
        if arg == "-S":
            return argv[index + 1]
        if not arg.startswith("-"):
            break
    return ""


# ── Windows: the client runs AS THE RUNNER ACCOUNT, in a one-process job ──────────────────────────────────────
# Not a restricted copy of the daemon's token: the MSYS runtime opens its own per-user objects (shared memory, the
# signal pipe) under DACLs naming only the token's user SID, so a token that must also pass a restricting SID dies
# at start ("couldn't create signal pipe, Win32 error 5"); an AppContainer blocks the loopback TCP an MSYS unix
# socket rides on; low integrity blocks writes but not reads. So the client logs on as the runner (`--logon`, the
# file the installer writes beside node.env, readable by the daemon alone) and is started with that token: it can
# open, read, write and run only what the runner itself already can. The job (one active process, killed with
# this script) is the second line: `MSG_EXEC`'s child cannot start at all. It runs on a desktop of its own
# (`_private_desktop`), on a window station of its own where this account may make one, else on the daemon's.

_LOGON32_LOGON_SERVICE, _LOGON32_PROVIDER_DEFAULT = 5, 0
_JOB_EXTENDED_LIMITS = 9
_LIMIT_ACTIVE_PROCESS, _LIMIT_DIE_ON_UNHANDLED_EXCEPTION, _LIMIT_KILL_ON_JOB_CLOSE = 0x8, 0x400, 0x2000
_CREATE_SUSPENDED, _CREATE_UNICODE_ENVIRONMENT = 0x4, 0x400
_STARTF_USESTDHANDLES = 0x100
_TOKEN_QUERY, _TOKEN_USER = 0x8, 1
_CWF_CREATE_ONLY, _WINSTA_ALL_ACCESS, _DESKTOP_ALL_ACCESS = 0x1, 0x37F, 0x1FF
_ERROR_ACCESS_DENIED, _ERROR_ALREADY_EXISTS, _UOI_NAME = 5, 183, 2
_SE_WINDOW_OBJECT, _DACL_SECURITY_INFORMATION, _SET_ACCESS, _TRUSTEE_IS_SID, _TRUSTEE_IS_USER = 7, 4, 2, 0, 1
# What the runner needs of the daemon's own window station to start a process on it: every station right and
# READ_CONTROL. Measured as NT SERVICE\jaahd in session 0 (pr-checks `tmux-jail-windows`): without EXITWINDOWS and
# READSCREEN together (each alone untested), or without READ_CONTROL, its user32 fails to initialise (0xC0000142).
# Never WRITE_DAC or WRITE_OWNER.
_RUNNER_STATION_ACCESS = _WINSTA_ALL_ACCESS | 0x20000
_INFINITE = 0xFFFFFFFF
# What the runner-account client inherits of this process's environment; nothing else (the daemon's own
# variables are not the runner's to read). PATH finds the MSYS runtime; MSYS carries `noglob`.
WINDOWS_CLIENT_ENV = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "MSYS", "TERM", "LANG",
                      "LC_ALL", "LC_CTYPE", "TZ", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE")


def logon_arg(argv: list[str]) -> tuple[str, list[str]]:
    """`(path, rest)` from a leading `--logon <file>`, or `("", argv)` when ARGV has none."""
    if len(argv) >= 2 and argv[0] == "--logon":
        return argv[1], argv[2:]
    return "", argv


def read_logon(path: str) -> tuple[str, str, str]:
    """`(domain, user, password)` from the logon file: the account (`.\\jaah` or `jaah`) on its first line, the
    password on its second. Raises `OSError` when it is unreadable or malformed."""
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    if len(lines) < 2 or not lines[0].strip() or not lines[1]:
        raise OSError(0, f"{path} does not name an account and its password")
    domain, _, user = lines[0].strip().rpartition("\\")
    return domain or ".", user, lines[1]


def windows_client_env(environ: dict[str, str]) -> str:
    """The client's environment block (`CREATE_UNICODE_ENVIRONMENT`): `WINDOWS_CLIENT_ENV` from ENVIRON only."""
    keep = {name.upper(): name for name in WINDOWS_CLIENT_ENV}
    # Sorted ignoring case, as `CreateProcess*` requires of a block.
    pairs = sorted(((key, value) for key, value in environ.items() if key.upper() in keep),
                   key=lambda pair: pair[0].upper())
    return "".join(f"{key}={value}\0" for key, value in pairs) + "\0"


def _win_api():
    """kernel32/advapi32 with their prototypes; Windows only."""
    from ctypes import wintypes as w

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class StartupInfo(ctypes.Structure):
        _fields_ = [("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR), ("lpTitle", w.LPWSTR),
                    ("dwX", w.DWORD), ("dwY", w.DWORD), ("dwXSize", w.DWORD), ("dwYSize", w.DWORD),
                    ("dwXCountChars", w.DWORD), ("dwYCountChars", w.DWORD), ("dwFillAttribute", w.DWORD),
                    ("dwFlags", w.DWORD), ("wShowWindow", w.WORD), ("cbReserved2", w.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", w.HANDLE), ("hStdOutput", w.HANDLE),
                    ("hStdError", w.HANDLE)]

    class Trustee(ctypes.Structure):
        _fields_ = [("pMultipleTrustee", ctypes.c_void_p), ("MultipleTrusteeOperation", ctypes.c_int),
                    ("TrusteeForm", ctypes.c_int), ("TrusteeType", ctypes.c_int), ("ptstrName", ctypes.c_void_p)]

    class ExplicitAccess(ctypes.Structure):
        _fields_ = [("grfAccessPermissions", w.DWORD), ("grfAccessMode", ctypes.c_int),
                    ("grfInheritance", w.DWORD), ("Trustee", Trustee)]

    class ProcessInformation(ctypes.Structure):
        _fields_ = [("hProcess", w.HANDLE), ("hThread", w.HANDLE), ("dwProcessId", w.DWORD),
                    ("dwThreadId", w.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]  # Windows-only
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]  # Windows-only
    user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]  # Windows-only
    proto = (
        (kernel32.GetCurrentProcess, w.HANDLE, []),
        (kernel32.LocalFree, ctypes.c_void_p, [ctypes.c_void_p]),
        (kernel32.CloseHandle, w.BOOL, [w.HANDLE]),
        (advapi32.OpenProcessToken, w.BOOL, [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]),
        (advapi32.GetTokenInformation, w.BOOL, [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD,
                                                ctypes.POINTER(w.DWORD)]),
        (advapi32.ConvertSidToStringSidW, w.BOOL, [ctypes.c_void_p, ctypes.POINTER(w.LPWSTR)]),
        (advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW, w.BOOL,
         [w.LPCWSTR, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]),
        (user32.CreateWindowStationW, w.HANDLE, [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p]),
        (user32.CloseWindowStation, w.BOOL, [w.HANDLE]),
        (user32.GetProcessWindowStation, w.HANDLE, []),
        (user32.SetProcessWindowStation, w.BOOL, [w.HANDLE]),
        (user32.CreateDesktopW, w.HANDLE, [w.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, w.DWORD, w.DWORD,
                                           ctypes.c_void_p]),
        (user32.CloseDesktop, w.BOOL, [w.HANDLE]),
        (user32.GetUserObjectInformationW, w.BOOL, [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD,
                                                    ctypes.POINTER(w.DWORD)]),
        (advapi32.GetSecurityInfo, w.DWORD, [w.HANDLE, ctypes.c_int, w.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                                             ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                             ctypes.POINTER(ctypes.c_void_p)]),
        (advapi32.SetEntriesInAclW, w.DWORD, [w.ULONG, ctypes.POINTER(ExplicitAccess), ctypes.c_void_p,
                                              ctypes.POINTER(ctypes.c_void_p)]),
        (advapi32.SetSecurityInfo, w.DWORD, [w.HANDLE, ctypes.c_int, w.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                                             ctypes.c_void_p, ctypes.c_void_p]),
        (kernel32.CreateJobObjectW, w.HANDLE, [ctypes.c_void_p, w.LPCWSTR]),
        (kernel32.SetInformationJobObject, w.BOOL, [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]),
        (kernel32.AssignProcessToJobObject, w.BOOL, [w.HANDLE, w.HANDLE]),
        (kernel32.ResumeThread, w.DWORD, [w.HANDLE]),
        (kernel32.TerminateProcess, w.BOOL, [w.HANDLE, ctypes.c_uint]),
        (kernel32.WaitForSingleObject, w.DWORD, [w.HANDLE, w.DWORD]),
        (kernel32.GetExitCodeProcess, w.BOOL, [w.HANDLE, ctypes.POINTER(w.DWORD)]),
        (advapi32.LogonUserW, w.BOOL, [w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, w.DWORD, w.DWORD, ctypes.POINTER(w.HANDLE)]),
        (advapi32.CreateProcessWithTokenW, w.BOOL, [w.HANDLE, w.DWORD, w.LPCWSTR, w.LPWSTR, w.DWORD,
                                                    ctypes.c_void_p, w.LPCWSTR, ctypes.POINTER(StartupInfo),
                                                    ctypes.POINTER(ProcessInformation)]),
    )
    for fn, restype, argtypes in proto:
        fn.restype, fn.argtypes = restype, argtypes
    return kernel32, advapi32, user32, {"ExtendedLimits": ExtendedLimits, "StartupInfo": StartupInfo,
                                        "ProcessInformation": ProcessInformation, "ExplicitAccess": ExplicitAccess,
                                        "Trustee": Trustee}


def _win_ok(ok: int, what: str) -> None:
    if not ok:
        err = ctypes.get_last_error()  # type: ignore[attr-defined]  # Windows-only
        raise OSError(0, f"{what}: {ctypes.FormatError(err).strip()} ({err})")  # type: ignore[attr-defined]


def _token_user_info(advapi32, token):
    """TOKEN's `TOKEN_USER`; its first field is the user SID's pointer, valid while the buffer lives."""
    from ctypes import wintypes as w

    size = w.DWORD(0)
    advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
    buf = ctypes.create_string_buffer(max(size.value, 1))
    _win_ok(advapi32.GetTokenInformation(token, _TOKEN_USER, buf, size, ctypes.byref(size)), "GetTokenInformation")
    return buf


def _token_user(kernel32, advapi32, token) -> str:
    """TOKEN's user SID, as a string."""
    from ctypes import wintypes as w

    buf = _token_user_info(advapi32, token)
    text = w.LPWSTR()
    _win_ok(advapi32.ConvertSidToStringSidW(ctypes.c_void_p.from_buffer(buf).value, ctypes.byref(text)),
            "ConvertSidToStringSid")
    try:
        return text.value or ""
    finally:
        kernel32.LocalFree(text)


def windows_desktop_sddl(daemon_sid: str, runner_sid: str) -> str:
    """The private window station's and desktop's access list: SYSTEM, this daemon and the runner only."""
    return f"D:P(A;;GA;;;SY)(A;;GA;;;{daemon_sid})(A;;GA;;;{runner_sid})"


def is_service_station(name: str) -> bool:
    """Whether NAME is a service logon session's own non-interactive window station (`Service-0x0-3e5$`), never
    `WinSta0` or another a person's session shows."""
    return name.startswith("Service-0x") and name.endswith("$")


def _object_name(user32, handle) -> str:
    """A window station's name (`Service-0x0-…$` for a service's own)."""
    from ctypes import wintypes as w

    buf = ctypes.create_unicode_buffer(256)
    _win_ok(user32.GetUserObjectInformationW(handle, _UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(w.DWORD())),
            "GetUserObjectInformation")
    return buf.value


def _grant_runner_station(kernel32, advapi32, types, station, runner_token) -> None:
    """Set the runner's entry on STATION's access list to `_RUNNER_STATION_ACCESS` — SET, not added: every call
    leaves the same one entry."""
    info = _token_user_info(advapi32, runner_token)
    dacl, descriptor, merged = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    rc = advapi32.GetSecurityInfo(station, _SE_WINDOW_OBJECT, _DACL_SECURITY_INFORMATION, None, None,
                                  ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if rc:
        raise OSError(0, f"GetSecurityInfo(window station): {ctypes.FormatError(rc).strip()} ({rc})")  # type: ignore[attr-defined]
    try:
        trustee = types["Trustee"](None, 0, _TRUSTEE_IS_SID, _TRUSTEE_IS_USER, ctypes.c_void_p.from_buffer(info).value)
        entry = types["ExplicitAccess"](_RUNNER_STATION_ACCESS, _SET_ACCESS, 0, trustee)
        rc = advapi32.SetEntriesInAclW(1, ctypes.byref(entry), dacl, ctypes.byref(merged))
        if rc:
            raise OSError(0, f"SetEntriesInAcl: {ctypes.FormatError(rc).strip()} ({rc})")  # type: ignore[attr-defined]
        rc = advapi32.SetSecurityInfo(station, _SE_WINDOW_OBJECT, _DACL_SECURITY_INFORMATION, None, None, merged,
                                      None)
        if rc:
            raise OSError(0, f"SetSecurityInfo(window station): {ctypes.FormatError(rc).strip()} ({rc})")  # type: ignore[attr-defined]
    finally:
        kernel32.LocalFree(merged)
        kernel32.LocalFree(descriptor)


def _private_desktop(kernel32, advapi32, user32, types, runner_token, owned_ui: list) -> str:
    """Make a desktop of this call's own that the runner may use; return `<station>\\<desktop>`. The runner's process
    cannot start on a station or desktop it may not open, and granting it the daemon's desktop would let runner code
    reach the daemon's windows. Its station: a fresh one (create-only, so a station a runner made first is never
    used) where this account may create one — SYSTEM, an administrator. A service's virtual account may not
    (NT SERVICE\\jaahd: access denied), so there the desktop goes on the daemon's own non-interactive station, the runner
    granted `_RUNNER_STATION_ACCESS` on it. Gone when its last handle closes."""
    from ctypes import wintypes as w

    own = w.HANDLE()
    _win_ok(advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(own)),
            "OpenProcessToken")
    try:
        sddl = windows_desktop_sddl(_token_user(kernel32, advapi32, own), _token_user(kernel32, advapi32, runner_token))
    finally:
        kernel32.CloseHandle(own)
    descriptor = ctypes.c_void_p()
    _win_ok(advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None),
            "ConvertStringSecurityDescriptor")

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("nLength", w.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", w.BOOL)]

    attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)

    def create_desktop(name: str) -> None:  # on the calling process's station
        ctypes.set_last_error(0)  # type: ignore[attr-defined]  # Windows-only
        desktop = user32.CreateDesktopW(name, None, None, 0, _DESKTOP_ALL_ACCESS, ctypes.byref(attributes))
        _win_ok(desktop, "CreateDesktop")
        owned_ui.append(("desktop", desktop))
        # An existing desktop is opened, its own access list kept: never run on one somebody else made.
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:  # type: ignore[attr-defined]  # Windows-only
            raise OSError(0, f"CreateDesktop: {name} already exists")

    try:
        name = f"jaah-tmux-{os.getpid()}-{os.urandom(4).hex()}"
        station = user32.CreateWindowStationW(name, _CWF_CREATE_ONLY, _WINSTA_ALL_ACCESS, ctypes.byref(attributes))
        if not station:
            if ctypes.get_last_error() != _ERROR_ACCESS_DENIED:  # type: ignore[attr-defined]  # Windows-only
                _win_ok(0, "CreateWindowStation")
            daemons = user32.GetProcessWindowStation()
            _win_ok(daemons, "GetProcessWindowStation")
            station_name = _object_name(user32, daemons)
            if not is_service_station(station_name):  # never grant the runner an interactive user's station
                raise OSError(0, f"CreateWindowStation: access denied, and {station_name} is not a service's own")
            _grant_runner_station(kernel32, advapi32, types, daemons, runner_token)
            create_desktop(name)
            return f"{station_name}\\{name}"
        owned_ui.append(("station", station))
        previous = user32.GetProcessWindowStation()
        _win_ok(user32.SetProcessWindowStation(station), "SetProcessWindowStation")
        try:
            create_desktop("Default")
        finally:
            user32.SetProcessWindowStation(previous)
        return f"{name}\\Default"
    finally:
        kernel32.LocalFree(descriptor)


def run_windows_jailed(tmux: str, argv: list[str], logon: str) -> int:
    """Run TMUX with ARGV's arguments as the account LOGON names, in the one-process job; return its exit code.
    Raises `OSError` when the jail cannot be built (nothing ran) or the client cannot be waited on (killed with the
    job). Each call is one logon and one Secondary Logon round trip — tens of milliseconds beside Python's start."""
    import msvcrt
    from ctypes import wintypes as w

    domain, user, password = read_logon(logon)
    kernel32, advapi32, user32, types = _win_api()
    owned: list = []  # handles to close
    owned_ui: list = []  # the private window station and desktop
    process = None
    try:
        token = w.HANDLE()
        _win_ok(advapi32.LogonUserW(user, domain, password, _LOGON32_LOGON_SERVICE, _LOGON32_PROVIDER_DEFAULT,
                                    ctypes.byref(token)), f"LogonUser({domain}\\{user})")
        owned.append(token)
        job = kernel32.CreateJobObjectW(None, None)
        _win_ok(job, "CreateJobObject")
        owned.append(job)
        limits = types["ExtendedLimits"]()
        limits.BasicLimitInformation.LimitFlags = (_LIMIT_ACTIVE_PROCESS | _LIMIT_KILL_ON_JOB_CLOSE
                                                   | _LIMIT_DIE_ON_UNHANDLED_EXCEPTION)
        limits.BasicLimitInformation.ActiveProcessLimit = 1
        _win_ok(kernel32.SetInformationJobObject(job, _JOB_EXTENDED_LIMITS, ctypes.byref(limits),
                                                 ctypes.sizeof(limits)), "SetInformationJobObject")

        startup = types["StartupInfo"]()
        startup.cb = ctypes.sizeof(startup)
        desktop = ctypes.create_unicode_buffer(_private_desktop(kernel32, advapi32, user32, types, token, owned_ui))
        startup.lpDesktop = ctypes.cast(desktop, w.LPWSTR)
        startup.dwFlags = _STARTF_USESTDHANDLES
        for fd, field in ((0, "hStdInput"), (1, "hStdOutput"), (2, "hStdError")):
            try:
                setattr(startup, field, msvcrt.get_osfhandle(fd))  # type: ignore[attr-defined]  # Windows-only
            except OSError:
                continue
        info = types["ProcessInformation"]()
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline([tmux, *argv[1:]]))
        environment = ctypes.create_unicode_buffer(windows_client_env(dict(os.environ)))
        # The cwd is the binary's folder: the daemon's own may be closed to the runner.
        _win_ok(advapi32.CreateProcessWithTokenW(token, 0, tmux, command,
                                                 _CREATE_SUSPENDED | _CREATE_UNICODE_ENVIRONMENT, environment,
                                                 os.path.dirname(tmux), ctypes.byref(startup), ctypes.byref(info)),
                "CreateProcessWithToken")
        process = info.hProcess
        owned += [info.hThread, info.hProcess]
        _win_ok(kernel32.AssignProcessToJobObject(job, process), "AssignProcessToJobObject")
        if kernel32.ResumeThread(info.hThread) == 0xFFFFFFFF:
            _win_ok(0, "ResumeThread")
        process = None  # running: from here its exit code is the answer
        if kernel32.WaitForSingleObject(info.hProcess, _INFINITE) != 0:  # WAIT_OBJECT_0
            _win_ok(0, "WaitForSingleObject")
        code = w.DWORD()
        _win_ok(kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code)), "GetExitCodeProcess")
        return code.value
    finally:
        if process is not None:
            kernel32.TerminateProcess(process, 126)  # built but never resumed: it ran nothing
        for handle in reversed(owned):
            kernel32.CloseHandle(handle)
        for kind, handle in reversed(owned_ui):
            (user32.CloseDesktop if kind == "desktop" else user32.CloseWindowStation)(handle)


def main(argv: list[str]) -> int:
    logon, argv = logon_arg(argv) if sys.platform == "win32" else ("", argv)
    if not argv:
        print("tmux_jail: usage: tmux_jail.py [--logon <file>] <tmux> [args…]", file=sys.stderr)
        return 2
    tmux = shutil.which(argv[0])
    if not tmux:
        print(f"tmux_jail: {argv[0]!r} is not on PATH", file=sys.stderr)
        return 127
    if sys.platform == "darwin":
        socket = socket_arg(argv)
        if not os.access(SANDBOX_EXEC, os.X_OK) or not socket:
            why = f"{SANDBOX_EXEC} is missing" if socket else "no -S socket to confine it to"
            print(f"tmux_jail: refusing to run an unjailed tmux client — {why}", file=sys.stderr)
            return 126
        os.execv(SANDBOX_EXEC, [SANDBOX_EXEC, "-p", darwin_profile(tmux, socket), tmux, *argv[1:]])
    if sys.platform == "win32":
        # UTF-8, as the daemon decodes it: `-I` drops the PYTHONUTF8 the service sets, leaving the ANSI code page.
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        try:
            if not logon:
                raise OSError(0, "no --logon account to run it as")
            return run_windows_jailed(tmux, argv, logon)
        except (OSError, ValueError) as exc:  # ValueError: a logon file that is not UTF-8
            print(f"tmux_jail: refusing to run an unjailed tmux client — {exc.strerror or exc}", file=sys.stderr)
            return 126
    try:
        restrict(tmux)
    except OSError as exc:
        print(f"tmux_jail: refusing to run an unjailed tmux client — {exc}", file=sys.stderr)
        return 126
    os.execv(tmux, argv)
    return 127  # unreachable: execv replaces this process or raises


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
