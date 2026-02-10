#!/usr/bin/env bash
# /workspace/pipeline/bin/stageP_proxy_mp4.sh
set -uo pipefail

MID="${MEDIA_ID:?MEDIA_ID env var required}"
CATALOG_ROOT="${CATALOG_ROOT:-/workspace/catalog}"
PROXY_HEIGHT="${PROXY_HEIGHT:-720}"

MANIFEST="${CATALOG_ROOT}/media_manifest/${MID}.json"
OUTDIR="${CATALOG_ROOT}/proxies"
OUT="${OUTDIR}/${MID}.mp4"
TMP="${OUTDIR}/${MID}.tmp.mp4"

if [[ ! -f "$MANIFEST" ]]; then
  echo "FAIL: missing manifest: $MANIFEST" >&2
  exit 2
fi

SRC="$(MANIFEST_PATH="$MANIFEST" python3 - <<'PY'
import json, os
mp = os.environ.get("MANIFEST_PATH","")
with open(mp, "r", encoding="utf-8") as f:
    m = json.load(f)
print(m.get("source_path","") or "")
PY
)"

if [[ -z "$SRC" ]]; then
  echo "FAIL: manifest has no source_path: $MANIFEST" >&2
  exit 2
fi
if [[ ! -f "$SRC" ]]; then
  echo "FAIL: source_path missing on disk: $SRC" >&2
  exit 2
fi

mkdir -p "$OUTDIR"

if [[ -s "$OUT" ]]; then
  echo "OK: proxy exists: $OUT" >&2
  exit 0
fi

FPS="$(ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate -of default=nw=1:nk=1 "$SRC" 2>/dev/null | head -n 1 || true)"
if [[ -z "$FPS" ]]; then
  FPS="25/1"
fi

GOP="$(FPS="$FPS" python3 - <<'PY'
import os
fps=os.environ.get("FPS","25/1")
try:
    num,den=fps.split("/",1)
    f=float(num)/float(den)
except Exception:
    f=25.0
g=int(round(f*2.0))
if g < 24:
    g = 24
print(g)
PY
)"

ROTATE="$(ffprobe -v error -select_streams v:0 -show_entries stream_tags=rotate -of default=nw=1:nk=1 "$SRC" 2>/dev/null | head -n 1 || true)"
ROTATE="${ROTATE:-}"
VF=""
if [[ "$ROTATE" = "90" ]]; then
  VF="transpose=1,"
elif [[ "$ROTATE" = "270" ]]; then
  VF="transpose=2,"
elif [[ "$ROTATE" = "-90" ]]; then
  VF="transpose=2,"
fi
VF="${VF}scale=-2:${PROXY_HEIGHT}:flags=bicubic"

echo "PROXY: MID=$MID" >&2
echo "PROXY: SRC=$SRC" >&2
echo "PROXY: OUT=$OUT" >&2
echo "PROXY: PROXY_HEIGHT=$PROXY_HEIGHT" >&2
echo "PROXY: FPS=$FPS GOP=$GOP ROTATE=${ROTATE:-none}" >&2

# Strict stream mapping: only first video + first audio, ignore iPhone data tracks.
# Also ignore input metadata mapping to reduce noisy diagnostics.
ffmpeg -hide_banner -nostats -loglevel error -y \
  -i "$SRC" \
  -map "0:v:0" \
  -map "0:a:0?" \
  -dn -sn \
  -vf "$VF" \
  -c:v libx264 -profile:v baseline -level 3.1 -pix_fmt yuv420p \
  -preset veryfast -crf 23 -maxrate 2500k -bufsize 5000k \
  -g "$GOP" -keyint_min "$GOP" -sc_threshold 0 \
  -c:a aac -b:a 96k -ac 2 \
  -movflags +faststart \
  "$TMP" \
  1>&2


rc=$?
if [[ "$rc" -ne 0 ]]; then
  echo "FAIL: ffmpeg rc=$rc" >&2
  rm -f "$TMP" >/dev/null 2>&1 || true
  exit "$rc"
fi

if [[ ! -s "$TMP" ]]; then
  echo "FAIL: proxy output missing/empty: $TMP" >&2
  rm -f "$TMP" >/dev/null 2>&1 || true
  exit 3
fi

mv -f "$TMP" "$OUT"
echo "OK: wrote proxy: $OUT"
