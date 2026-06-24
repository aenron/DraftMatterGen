#!/bin/sh
set -eu

mkdir -p /data/uploads
chown -R appuser:appuser /data

exec gosu appuser "$@"
