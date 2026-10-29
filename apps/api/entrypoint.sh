#!/bin/sh
# Jaah API launcher, and the single home for the uvicorn flags below. The ECS command execs this
# script; the local stack does NOT (its compose file overrides `command:`).
#
# SINGLE WORKER, refused HERE in the process that becomes the
# uvicorn master. The reason: each task registers ONE peer address (`app/cp_peers.py`: `ip:port` names
# exactly one process to the other tasks, which forward node-bus commands to it), and the Postgres
# LISTEN fan-out is per process; a second worker behind the same port would take half the forwards
# and none of the sockets. Scale is more TASKS, never more workers. Refused here because under
# `--workers N` the app is imported only inside workers the supervisor respawns forever, so the in-app
# guard would crash-loop rather than stop; this also sees a bare `--workers N` on argv.
# See app/worker_guard.py, stdlib-only so this extra interpreter stays off the app's import graph.
#
# WS KEEPALIVE 20/20 mirrors `apps/jaahd/node_bus.py`, so both ends notice a dead (half-open) peer on
# the same schedule; a library default change would silently lengthen that window.
#
# GRACEFUL SHUTDOWN 10s: uvicorn otherwise waits indefinitely on SIGTERM. 10s covers in-flight HTTP
# behind the ALB's 10s deregistration_delay and stays below the container's stopTimeout = 15 (task definition),
# because the lifespan teardown — incl. the 4504 sweep telling the fleet to redial the successor NOW —
# runs AFTER uvicorn stops serving, and ECS must allow both phases before SIGKILL.
#
# --no-access-log: nginx already writes each request's line to the SAME CloudWatch group, so uvicorn's
# is a verbatim duplicate (~4 req/s of node polling on a shared task). It suppresses ONLY per-request
# access lines — startup, shutdown, lifespan and every ERROR/traceback still log.
set -e
python -c 'import sys; from app.worker_guard import assert_single_api_worker; assert_single_api_worker(sys.argv[1:])' "$@"
# --forwarded-allow-ips states the library default (uvicorn 0.30.6 resolves "127.0.0.1") because
# `app/routes/public.py::check_rate_limit` keys on `request.client.host`: trusting nginx's loopback hop
# makes that the RIGHTMOST X-Forwarded-For entry (the ALB's), i.e. per real client IP. A changed default
# would make it one global bucket — a public-login lockout of everyone.
exec uvicorn --ws-ping-interval 20 --ws-ping-timeout 20 --timeout-graceful-shutdown 10 \
     --forwarded-allow-ips 127.0.0.1 --no-access-log "$@"
