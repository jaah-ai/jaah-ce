"""Start the `control` MCP server (server.py, beside this file) on the node's own interpreter.

The plugin manifest (`.mcp.json`) is shared by every OS and the agent CLI spawns it with a bare `python3` — on Ubuntu
22.04 the distro's 3.10, too old for server.py. So the manifest starts THIS file, which must parse under any
python3 a supported distro ships (Python 3.6 grammar: the test compiles it with `feature_version=(3, 6)`), and
which execs `$JAAH_PYTHON`, else the `jaah-python` package's interpreter where it is installed, on server.py.
Neither present (macOS, Windows, a source checkout): server.py runs in this process."""
import os
import runpy
import sys

JAAH_PYTHON = "/usr/lib/jaah-python/bin/python3"
SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")


def interpreter():
    """The interpreter to exec server.py under, or None to run it here."""
    chosen = (os.environ.get("JAAH_PYTHON") or "").strip()
    # Never a worktree-relative program in place of the control server, nor one that cannot run.
    if not (os.path.isabs(chosen) and os.access(chosen, os.X_OK)):
        chosen = ""
    if not chosen and os.access(JAAH_PYTHON, os.X_OK):
        chosen = JAAH_PYTHON
    if not chosen or os.path.realpath(chosen) == os.path.realpath(sys.executable):
        return None
    return chosen


def main():
    python = interpreter()
    if python:
        # `-E -s`: no PYTHON* variable or user site steers what the control server imports.
        os.execv(python, [python, "-E", "-s", SERVER] + sys.argv[1:])
    sys.argv[0] = SERVER
    runpy.run_path(SERVER, run_name="__main__")


if __name__ == "__main__":
    main()
