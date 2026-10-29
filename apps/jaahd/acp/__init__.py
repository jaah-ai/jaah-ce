"""The ACP host runtime: the node is an Agent Client Protocol CLIENT, speaking JSON-RPC 2.0 over the agent's stdio.

One host per run (`host.py`, D-HOST-1): it spawns the engine as its stdio child, opens one session, drives
turns, normalises `session/update` into schema-v1 events (`events.py`) and answers the agent's requests
(`requests.py`) — so a run is observed and steered through a typed protocol instead of a scraped TUI.
The daemon side (`runtime.py`, `session_store.py`) receives the events and serves the control lane.
"""
