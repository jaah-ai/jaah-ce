"""The ONE way a root helper runs a privileged command — a fixed argv list, and NOTHING reaches a shell.

The helper constrains the CONTENTS and the caller names nothing: every command is a program a
table names plus arguments derived from the helper's own root-only store, `shell=False`, argv as a list —
never `shell=True`, `os.system`, a string command, or an argv element that arrived over the socket. `run()`
asserts the shape rather than trusting the caller. This table holds what `rootlib` itself calls;
`vnodehelper/runner.py` widens it with the vnode helper's own programs.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

log = logging.getLogger("jaah.rootlib.run")

# Absolute paths, so the helper's PATH — whatever systemd or an operator's shell hands it — cannot
# decide which binary root executes. A program is looked up here or it is not run.
PROGRAMS = {
    "ip": "/usr/sbin/ip",
    "umount": "/usr/bin/umount",
    # iptables, for ONE job that nftables cannot do: `DOCKER-USER`. Rootful dockerd sets
    # `-P FORWARD DROP`, and a vnode's egress is forwarded traffic that matches none of its
    # bridge rules — so the nft table's own accept (which means "continue", not "stop") reaches that
    # policy and dies. `DOCKER-USER` is the chain docker documents as user-owned and does not flush;
    # writing it with the `iptables` binary rather than with `nft` is what keeps it legible to
    # `iptables -S` on a node, whichever backend the alternatives link resolves to.
    "iptables": "/usr/sbin/iptables",
}
# NO `install` ROW, and it is not an oversight: `install -o <user> -m <mode> <src> <dst>` is a general
# "write any file as root with any owner and any mode" primitive — strictly larger than every program
# above it, and the shortest route back to re-creating a sudoers file. The table IS the boundary, so it
# holds only what is actually called; `tests/test_rootlib_runner.py` pins that every key has a caller.


class CommandFailed(Exception):
    def __init__(self, argv: list[str], returncode: int, stderr: str) -> None:
        super().__init__(f"{argv[0]} exited {returncode}: {stderr.strip()[:400]}")
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str
    stderr: str


def run(program: str, *args: str, check: bool = True, timeout: float = 60.0,
        stdin_text: str | None = None, programs: dict[str, str] | None = None) -> Result:
    """Run one enumerated program with a fixed argv. `program` is a KEY into PROGRAMS, never a path.

    `stdin_text` carries input too large for a command line (`nft -f -`) so it still goes through here; a
    second `subprocess` site would skip the argv assertions (`tests/test_rootlib_runner.py` pins none
    exists). `programs` is the vnode helper's wider table; `rootlib`'s own calls never pass it.
    """
    table = PROGRAMS if programs is None else programs
    if program not in table:
        raise ValueError(f"{program!r} is not an enumerated program")
    argv = [table[program], *args]
    for element in argv:
        if not isinstance(element, str):
            raise TypeError("argv elements must be str")
    log.debug("run %s", argv)
    proc = subprocess.run(  # noqa: S603 — argv list, shell=False, program from PROGRAMS
        argv,
        shell=False,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    result = Result(proc.returncode, proc.stdout, proc.stderr)
    if check and proc.returncode != 0:
        raise CommandFailed(argv, proc.returncode, proc.stderr)
    return result
