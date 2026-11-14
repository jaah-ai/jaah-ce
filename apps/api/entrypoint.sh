#!/bin/sh
# Jaah API launcher, and the single home for the Granian flags below. The ECS commands (api and console relay)
# and the local compose services all exec this script, so every server process starts the same way.
#
# ONE GRANIAN PROCESS, ONE WORKER, refused HERE before Granian starts. The reason: each task registers ONE peer
# address (`app/cp_peers.py`: `ip:port` names exactly one process to the other tasks, which forward node-bus
# commands to it), and the Postgres LISTEN fan-out is per process; a second worker behind the same port would
# take half the forwards and none of the sockets. Scale is more TASKS, never more workers. The same interpreter
# refuses a CPython that is not a free-threaded build: the CPU offload threads must run in parallel, and the
# GIL-off check after the app's imports (`assert_gil_off`, in both lifespans) only means something on one.
# See app/worker_guard.py, stdlib-only so this extra interpreter stays off the app's import graph.
#
# --interface asgi --loop asyncio --task-impl asyncio: the FastAPI app on the stdlib asyncio loop, so the dev
# slow-callback log (`API_ASYNCIO_DEBUG`, app/main.py) and every asyncio API behave as documented. `--task-impl
# asyncio` is LOAD-BEARING, not a default restated: each request then runs in its own asyncio task with its own copy
# of the context, which is what keeps one request's tenancy scope (`app/tenancy/seam.py`, a contextvar) out of every
# other request's. Granian's `rust` task implementation steps every request in one shared context.
#
# --backpressure 65536: how many requests and open WebSockets Granian lets this worker hold at once. Its implicit
# default is backlog/workers (1024), a cap no previous server had, which long-lived sockets (the node bus, the event
# stream, nginx's keepalive upstreams) could fill until /api/health itself queued. Stated far above any task's open
# sockets, so the one bound left is the file-descriptor limit; this epic adds no per-task connection cap.
#
# SHUTDOWN (--workers-kill-timeout 10): on SIGTERM Granian stops accepting and runs the lifespan teardown — incl.
# the 4504 sweep telling the fleet to redial the successor NOW, and the DBOS drain — on the worker. 10s sits below
# the container's stopTimeout = 15 (task definition) and covers in-flight HTTP behind the ALB's 10s
# deregistration_delay. On the free-threaded build the worker is a THREAD, which Granian cannot kill, so past the
# 10s it only stops waiting; ECS's SIGKILL at stopTimeout is the hard bound.
#
# --no-access-log (Granian's default, stated): nginx already writes each request's line to the SAME CloudWatch
# group, so a server access line is a verbatim duplicate (~4 req/s of node polling on a shared task), and the
# relay's would log a `?ticket=` capability. Startup, shutdown, lifespan and every ERROR/traceback still log.
#
# --ws: WebSockets on (the node bus, the console relay's legs). Granian sends no WebSocket pings and negotiates
# no permessage-deflate, so the keepalive is the app's own (`app/ws_keepalive.py`) and no hop compresses.
#
# Client addresses: Granian does not read X-Forwarded-For; `app/proxy_headers.py` (outermost in `create_app`)
# trusts nginx's loopback hop, plus `JAAH_TRUSTED_PROXY_CIDRS` where the load balancers dial this port directly.
set -e
python -c 'import sys; from app.worker_guard import assert_free_threaded_build, assert_single_api_worker; assert_free_threaded_build(); assert_single_api_worker(sys.argv[1:])' "$@"
exec granian --interface asgi --loop asyncio --task-impl asyncio --backpressure 65536 --workers 1 \
     --workers-kill-timeout 10 --no-access-log --ws "$@"
