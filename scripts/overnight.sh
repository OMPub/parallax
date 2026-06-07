#!/usr/bin/env bash
# parallax overnight runner.
#
# Rotates `survey` across the given target repos until a wall-clock deadline, then
# runs LOCAL introspection on each. Surveys run SEQUENTIALLY on purpose: the local
# LLM box is single-request-at-a-time, so concurrent surveys would only contend.
#
# Usage: scripts/overnight.sh <hours> <target-repo-path> [more paths ...]
# Example: scripts/overnight.sh 9 ~/projects/ompub/doc-chain ~/projects/ompub/RSO
set -u

HOURS="${1:?usage: overnight.sh <hours> <target-path> [target-path ...]}"; shift
TARGETS=("$@")
[ "${#TARGETS[@]}" -ge 1 ] || { echo "need at least one target repo path"; exit 1; }

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
END=$(( $(date +%s) + HOURS * 3600 ))
LOG="$ROOT/overnight-$(date +%Y%m%d-%H%M).log"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

log "parallax overnight: ${HOURS}h, targets=[${TARGETS[*]}], deadline=$(date -r "$END" 2>/dev/null || echo +${HOURS}h)"
i=0
while [ "$(date +%s)" -lt "$END" ]; do
  i=$((i + 1))
  for T in "${TARGETS[@]}"; do
    [ "$(date +%s)" -ge "$END" ] && break
    log "loop $i — survey $(basename "$T")"
    python3 -m parallax survey "$T" --mode archaeology >> "$LOG" 2>&1 \
      || log "  (survey $(basename "$T") errored; continuing)"
  done
done
log "surveys done after $i loops; running LOCAL introspection (no upstream contribution)"
for T in "${TARGETS[@]}"; do
  log "introspect $(basename "$T")"
  python3 -m parallax introspect "$T" --act >> "$LOG" 2>&1 || true
done
log "overnight complete: $i loops across ${#TARGETS[@]} target(s). Review: $LOG"
