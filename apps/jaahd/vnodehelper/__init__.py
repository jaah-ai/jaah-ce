"""vnodehelper — the root helper that builds, starts, stops and destroys a host's virtual nodes.

A virtual node is another jaahd on one host, isolated by its own uid/gid set, trees, network namespace
and tailnet identity. Only the host's jaahd asks, over a `0660 root:jaahd` socket, for the
operations in `protocol.OPERATIONS`; every name, number and path is derived by `layout.py` (the single
home of the per-vnode layout) from the slot and tag in the helper's own store.

Isolation is a uid + namespace boundary on one shared kernel: a kernel escalation or a compromise of any
shared root component (this helper included) reaches every vnode. It is not VM-equivalent.

Stdlib only, like the `rootlib` primitives it reuses.
"""
