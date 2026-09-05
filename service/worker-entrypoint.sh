#!/bin/sh
set -eu

# Keep xvfb-run as a child process. Its Xvfb readiness signal is not handled
# correctly when xvfb-run itself is PID 1 in a container.
xvfb-run -a video-edit-worker "$@" &
worker_pid=$!
trap 'kill -TERM "$worker_pid" 2>/dev/null || true; wait "$worker_pid" 2>/dev/null || true; exit 143' INT TERM
wait "$worker_pid"
