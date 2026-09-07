#!/usr/bin/env bash
# Boot script for the stock runpod/worker-comfyui image.
# The endpoint start command downloads this file from the repo and runs it; the repo URL is
# passed via the BOOT_REPO_RAW environment variable on the endpoint, e.g.
#   BOOT_REPO_RAW=https://raw.githubusercontent.com/<user>/<repo>/main
#
# Steps (idempotent, run on every cold start):
#   1. Add the network volume's model and custom_nodes folders to ComfyUI's search paths.
#   2. Install python requirements of custom nodes found on the volume (into the image venv, local cache).
#   3. Install the extended handler (volume management ops next to the normal workflow handler).
#   4. Hand over to the image's normal /start.sh.
set -u
V=/runpod-volume
# the image ships wget but not curl
fetch() { wget -qO "$2" "$1" 2>/dev/null || curl -fsSL "$1" -o "$2"; }
REPO_RAW=${BOOT_REPO_RAW:?set BOOT_REPO_RAW to the raw GitHub URL of this repo}
echo "[boot] $(date -u +%FT%TZ) starting; volume mounted: $([ -d $V ] && echo yes || echo NO)"

# --- 1. model / custom node paths -------------------------------------------------------
NODE_ROOT=${CUSTOM_NODES_DIR:-$V/custom_nodes}
if [ -d "$V" ]; then
  mkdir -p "$NODE_ROOT"
  if fetch "$REPO_RAW/extra_model_paths.append.yaml" /tmp/extra.yaml; then
    sed -i '/^# >>> comfy-runpod boot/,$d' /comfyui/extra_model_paths.yaml
    { echo "# >>> comfy-runpod boot"; sed "s#__NODE_ROOT__#${NODE_ROOT}#g" /tmp/extra.yaml; } >> /comfyui/extra_model_paths.yaml
    echo "[boot] extra_model_paths.yaml updated (custom_nodes -> $NODE_ROOT)"
  fi
fi

# --- 1b. expose a volume folder as ComfyUI input subfolder "vol" (LoadImage: "vol/<file>") -------
VOL_INPUT=${VOLUME_INPUT_DIR:-$V/runpod-slim/ComfyUI/input}
if [ -d "$VOL_INPUT" ]; then
  mkdir -p /comfyui/input && ln -sfn "$VOL_INPUT" /comfyui/input/vol && echo "[boot] /comfyui/input/vol -> $VOL_INPUT"
fi

# --- 2. custom node python deps ---------------------------------------------------------
if [ -d "$NODE_ROOT" ] && [ "${SKIP_NODE_DEPS:-0}" != "1" ]; then
  # cache stays on the container's local disk: a cache on the network volume is far too slow
  t0=$(date +%s)
  for req in "$NODE_ROOT"/*/requirements.txt; do
    [ -f "$req" ] || continue
    [ -f "$(dirname "$req")/.disabled" ] && continue
    echo "[boot] deps: $req"
    uv pip install --no-progress -r "$req" 2>&1 | tail -n 3 || true
  done
  echo "[boot] custom node deps checked in $(( $(date +%s) - t0 ))s"
fi

# --- 3. extended handler ----------------------------------------------------------------
if fetch "$REPO_RAW/handler_ext.py" /handler_ext.py; then
  sed -i 's#/handler.py#/handler_ext.py#g' /start.sh
  echo "[boot] handler_ext installed"
else
  echo "[boot] WARN: could not fetch handler_ext.py, using stock handler"
fi

echo "[boot] done -> /start.sh"
exec /start.sh
