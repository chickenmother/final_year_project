#!/usr/bin/env bash
# scripts/inject_latency.sh — Chaos Engineering: inject / reset tc netem latency
#
# Purpose
# -------
# Adds or removes artificial network delay on the container interfaces of
# Cloud X (172.20.0.10) and Middle Y (172.20.0.20) using ``tc netem``.
# This lets you observe dynamic tier migration as the measured RTT crosses
# the thresholds defined in shared/config.py.
#
# Prerequisites
# -------------
# - Docker Compose must be running:  ``docker compose up -d``
# - Each container **must** have ``cap_add: [NET_ADMIN]`` in docker-compose.yml
#   otherwise ``tc`` will fail with "Operation not permitted".
# - Container names default to ``cloud_x`` and ``middle_y``.  Override with
#   environment variables ``CLOUD_X_CONTAINER`` and ``MIDDLE_Y_CONTAINER``.
#
# Usage
# -----
#   bash scripts/inject_latency.sh inject <delay_ms>   # add delay to X and Y
#   bash scripts/inject_latency.sh reset                # clear all tc rules
#
# Examples
# --------
#   bash scripts/inject_latency.sh inject 100   # 100 ms artificial latency
#   bash scripts/inject_latency.sh inject 250   # force local-fallback threshold
#   bash scripts/inject_latency.sh reset        # restore clean network

set -euo pipefail

# -- container names (overridable) -------------------------------------------------
CLOUD_X="${CLOUD_X_CONTAINER:-cloud_x}"
MIDDLE_Y="${MIDDLE_Y_CONTAINER:-middle_y}"
END_Z="${END_Z_CONTAINER:-end_z}"
CONTAINERS=("$CLOUD_X" "$MIDDLE_Y" "$END_Z")

# -- colour helpers -----------------------------------------------------------------
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'  # No Colour

log_ok()   { printf "${GREEN}  ✓ %s${NC}\n" "$*"; }
log_fail() { printf "${RED}  ✗ %s${NC}\n" "$*"; }
log_warn() { printf "${YELLOW}  ⚠ %s${NC}\n" "$*"; }

# -- usage --------------------------------------------------------------------------
usage() {
    echo "Usage:  $(basename "$0") inject <delay_ms>"
    echo "        $(basename "$0") reset"
    exit 1
}
# ---------------------------------------------------------------------------
# inject
# ---------------------------------------------------------------------------
do_inject() {
    local delay="$1"
    local ok=0 fail=0

    echo "Injecting ${delay} ms artificial latency …"
    for c in "${CONTAINERS[@]}"; do
        if ! docker inspect --format='{{.State.Running}}' "$c" 2>/dev/null | grep -q true; then
            log_warn "Container '$c' is not running — skipping"
            ((fail++))
            continue
        fi
        if docker exec "$c" tc qdisc add dev eth0 root netem delay "${delay}"ms 2>/dev/null; then
            log_ok "$c ← ${delay} ms delay"
            ((ok++))
        else
            log_fail "$c — tc failed (is NET_ADMIN cap present?)"
            ((fail++))
        fi
    done
    echo ""
    printf "Done: %d ok, %d skipped/failed\n" "$ok" "$fail"
}

# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------
do_reset() {
    local ok=0 fail=0

    echo "Resetting traffic-control rules …"
    for c in "${CONTAINERS[@]}"; do
        if ! docker inspect --format='{{.State.Running}}' "$c" 2>/dev/null | grep -q true; then
            log_warn "Container '$c' is not running — skipping"
            ((fail++))
            continue
        fi
        # Delete root qdisc — may fail if no rules exist, which is harmless.
        docker exec "$c" tc qdisc del dev eth0 root 2>/dev/null || true
        log_ok "$c — cleared"
        ((ok++))
    done
    echo ""
    printf "Done: %d ok, %d skipped\n" "$ok" "$fail"
}

# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------
case "${1:-}" in
    inject)
        delay="${2:-}"
        [[ -n "$delay" ]] && [[ "$delay" =~ ^[0-9]+$ ]] || usage
        do_inject "$delay"
        ;;
    reset)
        do_reset
        ;;
    *)
        usage
        ;;
esac