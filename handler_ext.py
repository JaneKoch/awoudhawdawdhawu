"""
Extended handler for runpod/worker-comfyui (installed by boot.sh).

Jobs with input.workflow go to the upstream handler unchanged.
Volume management ops (relative paths resolve against /runpod-volume):

  {"input": {"op": "ls", "path": "/runpod-volume", "depth": 3, "models_only": false}}
  {"input": {"op": "df"}}
  {"input": {"op": "models"}}            # model files per folder as ComfyUI resolves them
  {"input": {"op": "download", "files": [{"url": "...", "path": "models/loras/x.safetensors"}],
             "hf_token": "...", "civitai_token": "..."}}
  {"input": {"op": "clone", "repo": "https://github.com/owner/pack", "ref": "main"}}   # into custom_nodes/
  {"input": {"op": "rm", "path": "models/loras/x.safetensors", "recursive": false}}
  {"input": {"op": "stage", "path": "runpod-slim/ComfyUI/input/x.png", "name": "x.png"}}  # copy into /comfyui/input
  {"input": {"op": "put", "path": "models/loras/x.safetensors", "data_b64": "...", "append": true}}  # chunked upload
  {"input": {"op": "cat", "path": "/sys/fs/cgroup/memory.max"}}   # read a small text file (debugging)

Between workflow jobs that reference a different set of model files, ComfyUI is told to unload models
and clear its cache (POST /free). Serverless containers have a memory cgroup limit far below host RAM,
and ComfyUI's RAM-pressure cache only looks at host RAM, so offloaded weights of several model families
would otherwise pile up until the kernel OOM-kills ComfyUI. Set FREE_ON_MODEL_CHANGE=0 to disable.
"""
import os
import sys
import time
import base64
import shutil
import hashlib
import subprocess

import requests
import runpod

sys.path.insert(0, "/")
import handler as upstream  # noqa: E402  (upstream worker-comfyui /handler.py)

VOLUME = "/runpod-volume"
NODE_ROOT = os.environ.get("CUSTOM_NODES_DIR", os.path.join(VOLUME, "custom_nodes"))
MODEL_EXT = (".safetensors", ".gguf", ".ckpt", ".pt", ".pth", ".bin", ".onnx", ".sft")
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".uv-cache", "site-packages", ".cache"}


def _abs(path):
    path = path or VOLUME
    if not os.path.isabs(path):
        path = os.path.join(VOLUME, path)
    return os.path.normpath(path)


def op_ls(inp):
    root = _abs(inp.get("path"))
    depth = int(inp.get("depth", 3))
    only_models = bool(inp.get("models_only", False))
    if not os.path.exists(root):
        return {"error": f"{root} does not exist", "volume_mounted": os.path.isdir(VOLUME)}
    entries = []
    base_depth = root.rstrip("/").count("/")
    for dirpath, dirnames, filenames in os.walk(root):
        cur = dirpath.count("/") - base_depth
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        if cur >= depth:
            for d in dirnames:
                entries.append({"path": os.path.join(dirpath, d) + "/ (not expanded)", "size": None})
            dirnames[:] = []
        for f in sorted(filenames):
            if only_models and not f.lower().endswith(MODEL_EXT):
                continue
            p = os.path.join(dirpath, f)
            try:
                size = os.path.getsize(p)
            except OSError:
                size = -1
            entries.append({"path": p, "size": size})
        if len(entries) > 4000:
            entries.append({"path": "... truncated ...", "size": None})
            break
    return {"root": root, "count": len(entries), "entries": entries}


def op_df(inp):
    out = {}
    for p in (VOLUME, "/", "/comfyui"):
        try:
            t, u, f = shutil.disk_usage(p)
            out[p] = {"total_gb": round(t / 1e9, 1), "used_gb": round(u / 1e9, 1), "free_gb": round(f / 1e9, 1)}
        except Exception as e:  # noqa: BLE001
            out[p] = {"error": str(e)}
    return out


def _download_one(url, dest, hf_token=None, civitai_token=None, want_sha=False):
    dest = _abs(dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    headers = {"User-Agent": "comfy-runpod/1.0"}
    if "huggingface.co" in url and hf_token:
        headers["Authorization"] = f"Bearer {hf_token}"
    if "civitai.com" in url and civitai_token:
        headers["Authorization"] = f"Bearer {civitai_token}"
        url = f"{url}{'&' if '?' in url else '?'}token={civitai_token}"
    t0 = time.time()
    with requests.get(url, headers=headers, stream=True, timeout=(30, 120), allow_redirects=True) as r:
        if r.status_code != 200:
            return {"path": dest, "ok": False, "status": r.status_code, "body": r.text[:300]}
        total = int(r.headers.get("content-length") or 0)
        if os.path.exists(dest) and total and os.path.getsize(dest) == total:
            return {"path": dest, "ok": True, "skipped": "already present with same size", "size": total}
        tmp = dest + ".part"
        h = hashlib.sha256() if want_sha else None
        n = 0
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(chunk_size=8 << 20):
                if chunk:
                    fh.write(chunk)
                    n += len(chunk)
                    if h:
                        h.update(chunk)
        if total and n != total:
            os.remove(tmp)
            return {"path": dest, "ok": False, "error": f"short read {n}/{total}"}
        os.replace(tmp, dest)
    res = {"path": dest, "ok": True, "size": n, "seconds": round(time.time() - t0, 1),
           "mb_s": round(n / 1e6 / max(time.time() - t0, 0.01), 1)}
    if h:
        res["sha256"] = h.hexdigest()
    return res


def op_download(inp):
    results = []
    for f in inp.get("files") or []:
        try:
            results.append(_download_one(f["url"], f["path"], inp.get("hf_token"), inp.get("civitai_token"), f.get("sha256", False)))
        except Exception as e:  # noqa: BLE001
            results.append({"path": f.get("path"), "ok": False, "error": repr(e)})
    return {"results": results}


def op_clone(inp):
    """git clone a custom node pack into the volume's custom_nodes directory."""
    repo = inp.get("repo") or ""
    if not repo.startswith("https://github.com/"):
        return {"error": "repo must be an https://github.com/ URL"}
    name = inp.get("name") or repo.rstrip("/").split("/")[-1].removesuffix(".git")
    dest = os.path.join(NODE_ROOT, name)
    os.makedirs(NODE_ROOT, exist_ok=True)
    if os.path.isdir(dest):
        if not inp.get("update"):
            return {"path": dest, "skipped": "already present (pass update=true to git pull)"}
        p = subprocess.run(["git", "-C", dest, "pull", "--ff-only"], capture_output=True, text=True, timeout=600)
    else:
        cmd = ["git", "clone", "--depth", "1"]
        if inp.get("ref"):
            cmd += ["--branch", str(inp["ref"])]
        p = subprocess.run(cmd + [repo, dest], capture_output=True, text=True, timeout=900)
    return {"path": dest, "rc": p.returncode, "stdout": p.stdout[-4000:], "stderr": p.stderr[-4000:]}


def op_rm(inp):
    p = _abs(inp.get("path"))
    if not p.startswith(VOLUME + "/"):
        return {"error": "refusing to delete outside the network volume"}
    if os.path.isdir(p):
        if not inp.get("recursive"):
            return {"error": "is a directory; pass recursive=true"}
        shutil.rmtree(p)
    elif os.path.exists(p):
        os.remove(p)
    else:
        return {"error": "not found", "path": p}
    return {"removed": p}


def op_models(inp):
    """Model files per folder as ComfyUI resolves them (folder_paths + extra_model_paths.yaml)."""
    sys.path.insert(0, "/comfyui")
    cwd = os.getcwd()
    os.chdir("/comfyui")
    try:
        import folder_paths  # noqa: E402
        import yaml  # noqa: E402
        with open("/comfyui/extra_model_paths.yaml") as fh:
            cfg = yaml.safe_load(fh) or {}
        for _, c in cfg.items():
            if not isinstance(c, dict):
                continue
            base = c.pop("base_path", "")
            for k, v in c.items():
                if k == "is_default":
                    continue
                for sub in str(v).split("\n"):
                    if sub.strip():
                        folder_paths.add_model_folder_path(k, os.path.join(base, sub.strip()))
        out = {}
        folders = inp.get("folders") or ["checkpoints", "diffusion_models", "text_encoders", "vae", "loras",
                                         "upscale_models", "controlnet", "clip_vision", "ultralytics", "sams", "custom_nodes"]
        for folder in folders:
            try:
                out[folder] = {"paths": folder_paths.get_folder_paths(folder), "files": folder_paths.get_filename_list(folder)}
            except Exception as e:  # noqa: BLE001
                out[folder] = {"error": repr(e)}
        return out
    finally:
        os.chdir(cwd)


def op_cat(inp):
    p = inp.get("path") or ""
    if not os.path.isfile(p):
        return {"error": "not a file", "path": p}
    with open(p, "rb") as fh:
        data = fh.read(int(inp.get("max_bytes", 65536)))
    return {"path": p, "text": data.decode("utf-8", "replace")}


def _cgroup_mem():
    out = {}
    for name in ("memory.max", "memory.current", "memory.events"):
        try:
            with open(f"/sys/fs/cgroup/{name}") as fh:
                out[name] = fh.read().strip()
        except OSError:
            pass
    return out


def op_stage(inp):
    """Copy a file from the volume into ComfyUI's input directory so LoadImage can reference it by name."""
    src = _abs(inp.get("path"))
    if not os.path.isfile(src):
        return {"error": "not found", "path": src}
    name = inp.get("name") or os.path.basename(src)
    dest = os.path.join("/comfyui/input", os.path.basename(name))
    shutil.copyfile(src, dest)
    return {"staged": dest, "size": os.path.getsize(dest)}


def op_put(inp):
    """Write base64 data to a volume path; with append=true, chunks can be sent in several requests."""
    dest = _abs(inp.get("path"))
    if not dest.startswith(VOLUME + "/"):
        return {"error": "path must be on the network volume"}
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    data = base64.b64decode(inp.get("data_b64") or "")
    mode = "ab" if inp.get("append") else "wb"
    with open(dest, mode) as fh:
        fh.write(data)
    return {"path": dest, "written": len(data), "size": os.path.getsize(dest)}


OPS = {"stage": op_stage, "put": op_put, "cat": op_cat, "ls": op_ls, "df": op_df, "download": op_download, "clone": op_clone, "rm": op_rm, "models": op_models}


_last_models = None


def _models_in(workflow):
    names = set()
    for node in (workflow or {}).values():
        for v in (node.get("inputs") or {}).values():
            if isinstance(v, str) and v.lower().endswith(MODEL_EXT):
                names.add(v)
            elif isinstance(v, dict) and isinstance(v.get("lora"), str):
                names.add(v["lora"])
    return names


def _comfy_free():
    try:
        r = requests.post("http://127.0.0.1:8188/free", json={"unload_models": True, "free_memory": True}, timeout=30)
        print(f"comfy-runpod - asked ComfyUI to unload models / free cache: HTTP {r.status_code}; cgroup {_cgroup_mem()}")
    except Exception as e:  # noqa: BLE001
        print(f"comfy-runpod - /free failed: {e!r}")


def handler(job):
    global _last_models
    inp = job.get("input") or {}
    op = inp.get("op")
    if not op and isinstance(inp.get("workflow"), dict) and os.environ.get("FREE_ON_MODEL_CHANGE", "1") != "0":
        models = _models_in(inp["workflow"])
        if _last_models is not None and models != _last_models:
            _comfy_free()
        _last_models = models
    if op:
        fn = OPS.get(op)
        if not fn:
            return {"error": f"unknown op {op!r}; known: {sorted(OPS)}"}
        try:
            return fn(inp)
        except Exception as e:  # noqa: BLE001
            return {"error": repr(e)}
    return upstream.handler(job)


if __name__ == "__main__":
    print("worker-comfyui (comfy-runpod ext) - Starting handler...")
    runpod.serverless.start({"handler": handler})
