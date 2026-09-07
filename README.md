# comfy-runpod

Boot script + extended handler for RunPod's stock `runpod/worker-comfyui` serverless image.
No custom Docker image: the endpoint's start command downloads `boot.sh` from this repo at container
start. The script wires the attached network volume (models + custom nodes) into ComfyUI and installs
`handler_ext.py`, which adds volume-management ops (ls / df / models / download / clone / rm) next to
the normal workflow handler.

Endpoint settings:

- Start command: `bash -c "wget -qO /boot.sh $BOOT_REPO_RAW/boot.sh && bash /boot.sh"`
- Environment: `BOOT_REPO_RAW=https://raw.githubusercontent.com/<user>/<repo>/<commit-sha>`
  (pin a commit: GitHub's CDN serves `main` stale for minutes after a push)

No secrets live here; tokens are passed per request.

Notes
- Custom nodes live in `/runpod-volume/custom_nodes`; their `requirements.txt` are installed into the
  image venv at boot (about 40 s for Impact Pack). Keep the uv cache on local disk: a cache on the
  network volume takes many minutes.
- ComfyUI rejects `LoadImage` paths that resolve outside its input directory, so volume files cannot be
  symlinked in; upload images with the request (`input.images`) or use the `stage` op for tests.
