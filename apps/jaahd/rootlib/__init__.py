"""rootlib — shared root-side primitives for the privileged helpers: `vnodehelper` and the GHA runner
installer.

Stdlib only and no import-time side effects: install.py is a standalone host script with no virtualenv,
and the vnode helper's boot rebuild imports these before anything else on the node exists.
"""
