"""Third-party credential support — the type registry that `app/routes/credentials/` is built on.

A package rather than a module because the connection testers (a later block) land beside the registry
here, not inside the router: the router's job is CRUD over stored rows, the testers' job is outbound
HTTP to an operator-supplied host, and the two want separate review lanes.
"""
