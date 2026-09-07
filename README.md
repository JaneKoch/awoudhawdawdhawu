# comfy-runpod

Boot script + extended handler for RunPod's stock `runpod/worker-comfyui` serverless image.
No custom Docker image: the endpoint's start command downloads `boot.sh` from this repo at container
start. The script wires the attached network volume (models + custom nodes) into ComfyUI and installs
`handler_ext.py`, which adds volume-management ops (ls / df / models / download / clone / rm) next to
the normal workflow handler.

Endpoint settings:

- Start command: `bash -c "curl -fsSL $BOOT_REPO_RAW/boot.sh -o /boot.sh && bash /boot.sh"`
- Environment: `BOOT_REPO_RAW=https://raw.githubusercontent.com/<user>/<repo>/main`

No secrets live here; tokens are passed per request.
