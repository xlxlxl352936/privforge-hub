import os
import sys
import multiprocessing

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

multiprocessing.freeze_support()

def get_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

BASE_DIR = get_base_dir()

LOG_PATH = os.path.join(os.path.expanduser("~"), "privforge_hub_debug.log")

def dlog(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            import datetime
            f.write(f"[{datetime.datetime.now():%H:%M:%S}] {msg}\n")
    except Exception:
        pass

dlog("=== PrivForge Hub 起動 ===")
dlog(f"frozen={getattr(sys, 'frozen', False)}, BASE_DIR={BASE_DIR}")

import threading
import uvicorn
import customtkinter as ctk
import ctypes
import hashlib
import subprocess
import shutil
import json
import uuid
import shlex
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware

# ── パス定数 ──────────────────────────────────────────────────
BASE_SECRET_PATH   = os.path.join(os.environ["LOCALAPPDATA"], "PrivForgeData")
SECRET_DIR         = os.path.join(BASE_SECRET_PATH, ".privforge_vault")
AUDIO_DIR          = os.path.join(SECRET_DIR, "audio")
IMAGE_DIR          = os.path.join(SECRET_DIR, "images")
PASS_FILE          = os.path.join(BASE_SECRET_PATH, "vault.dat")
TAGS_FILE          = os.path.join(BASE_SECRET_PATH, "tags.json")
THUMB_DIR          = os.path.join(BASE_SECRET_PATH, ".thumbs")
TOOLS_FILE         = os.path.join(BASE_SECRET_PATH, "tools.json")
LADA_CLI_PATH      = os.path.join(BASE_DIR, "tools", "lada", "lada-cli.exe")
JASNA_CLI_PATH     = os.path.join(BASE_DIR, "tools", "jasna", "jasna-cli.exe")
JASNA_PRESETS_FILE = os.path.join(BASE_SECRET_PATH, "jasna_presets.json")
FFMPEG_PATH        = os.path.join(BASE_DIR, "ffmpeg.exe")

# 旧 XL Press 時代のパス（初回起動時にデータを自動マイグレーション）
_OLD_BASE       = os.path.join(os.environ["LOCALAPPDATA"], "SystemNetworkData")
_OLD_SECRET_DIR = os.path.join(_OLD_BASE, ".xl_vault_content")

# ── 機能フラグ ────────────────────────────────────────────────
ENABLE_LADA  = False   # True で有効
ENABLE_JASNA = False   # True で有効
# ─────────────────────────────────────────────────────────────

dlog(f"LADA_CLI_PATH={LADA_CLI_PATH}")
dlog(f"FFMPEG_PATH={FFMPEG_PATH}")
dlog(f"TOOLS_FILE={TOOLS_FILE}")


def setup_secure_env():
    # 旧 XL Press データを新パスへ自動マイグレーション
    if os.path.exists(_OLD_SECRET_DIR) and not os.path.exists(SECRET_DIR):
        dlog(f"[migrate] {_OLD_SECRET_DIR} → {SECRET_DIR}")
        try:
            os.makedirs(BASE_SECRET_PATH, exist_ok=True)
            shutil.copytree(_OLD_SECRET_DIR, SECRET_DIR)
            dlog("[migrate] video files migrated OK")
        except Exception as e:
            dlog(f"[migrate] ERROR: {e}")
    for src_name, dst_path in [
        ("vault.dat",          PASS_FILE),
        ("tags.json",          TAGS_FILE),
        ("tools.json",         TOOLS_FILE),
        ("jasna_presets.json", JASNA_PRESETS_FILE),
    ]:
        old_f = os.path.join(_OLD_BASE, src_name)
        if os.path.exists(old_f) and not os.path.exists(dst_path):
            try:
                os.makedirs(BASE_SECRET_PATH, exist_ok=True)
                shutil.copy2(old_f, dst_path)
                dlog(f"[migrate] {src_name} OK")
            except Exception as e:
                dlog(f"[migrate] {src_name} ERROR: {e}")

    for d in [BASE_SECRET_PATH, SECRET_DIR, AUDIO_DIR, IMAGE_DIR, THUMB_DIR]:
        if not os.path.exists(d):
            os.makedirs(d)
    ctypes.windll.kernel32.SetFileAttributesW(SECRET_DIR, 0x02 | 0x04)

try:
    setup_secure_env()
    dlog("setup_secure_env OK")
except Exception as e:
    dlog(f"setup_secure_env ERROR: {e}")


# ── GPU 検出 ─────────────────────────────────────────────────
def detect_encoding_args():
    """GPU優先順位: NVIDIA → AMD → Intel → CPU fallback"""
    try:
        result = subprocess.run(["nvidia-smi"], capture_output=True, timeout=5)
        if result.returncode == 0:
            dlog("[gpu] NVIDIA検出")
            return ["--encoding-preset", "hevc-nvidia-gpu-hq", "--fp16"], "NVIDIA GPU"
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["wmic", "path", "win32_VideoController", "get", "name"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5
        )
        gpu_info = result.stdout.upper()
        dlog(f"[gpu] wmic: {result.stdout.strip()}")
        if "AMD" in gpu_info or "RADEON" in gpu_info:
            return ["--encoder", "libx265",
                    "--encoder-options", "-crf 26 -preset fast -x265-params log_level=error"], "AMD GPU (CPU encode)"
        if "INTEL" in gpu_info or "UHD" in gpu_info or "IRIS" in gpu_info:
            return ["--encoder", "libx265",
                    "--encoder-options", "-crf 26 -preset fast -x265-params log_level=error"], "Intel GPU (CPU encode)"
    except Exception as e:
        dlog(f"[gpu] wmic失敗: {e}")
    dlog("[gpu] CPU fallback")
    return ["--encoder", "libx265",
            "--encoder-options", "-crf 26 -preset fast -x265-params log_level=error"], "CPU"


# ── データ読み書き ────────────────────────────────────────────
def load_tags():
    if not os.path.exists(TAGS_FILE): return {}
    try:
        with open(TAGS_FILE, "r", encoding="utf-8") as f: return json.load(f)
    except: return {}

def save_tags(tags):
    with open(TAGS_FILE, "w", encoding="utf-8") as f:
        json.dump(tags, f, ensure_ascii=False, indent=2)

def load_jasna_presets():
    if not os.path.exists(JASNA_PRESETS_FILE): return []
    try:
        with open(JASNA_PRESETS_FILE, "r", encoding="utf-8") as f: return json.load(f)
    except: return []

def save_jasna_presets(presets):
    with open(JASNA_PRESETS_FILE, "w", encoding="utf-8") as f:
        json.dump(presets, f, ensure_ascii=False, indent=2)

def load_tool_profiles():
    """登録済み汎用ツール一覧を返す"""
    if not os.path.exists(TOOLS_FILE): return []
    try:
        with open(TOOLS_FILE, "r", encoding="utf-8") as f: return json.load(f)
    except: return []

def save_tool_profiles(tools):
    with open(TOOLS_FILE, "w", encoding="utf-8") as f:
        json.dump(tools, f, ensure_ascii=False, indent=2)


# ── FastAPI ──────────────────────────────────────────────────
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.state.unlocked = False

lada_jobs  = {}
jasna_jobs = {}
tool_jobs  = {}   # 汎用ツールジョブ

# ── 基本エンドポイント ────────────────────────────────────────
_VIDEO_EXTS = ('.mp4', '.mkv', '.mov', '.avi', '.ts')
_AUDIO_EXTS = ('.mp3', '.wav', '.m4a', '.aac', '.ogg', '.flac')
_IMAGE_EXTS = ('.jpg', '.jpeg', '.png')

@app.get("/videos")
def list_videos():
    if not app.state.unlocked: return {"videos": []}
    return {"videos": [f for f in os.listdir(SECRET_DIR)
                       if f.lower().endswith(_VIDEO_EXTS)]}

@app.get("/stream/{video_name}")
async def stream_video(video_name: str, request: Request):
    if not app.state.unlocked: raise HTTPException(status_code=403)
    path = os.path.join(SECRET_DIR, video_name)
    if not os.path.exists(path): raise HTTPException(status_code=404)
    file_size = os.stat(path).st_size
    range_header = request.headers.get("range")
    start, end = 0, file_size - 1
    if range_header:
        parts = range_header.replace("bytes=", "").split("-")
        start = int(parts[0])
        end = int(parts[1]) if parts[1] else file_size - 1
    def get_chunk():
        with open(path, "rb") as f:
            f.seek(start)
            yield f.read(end - start + 1)
    return StreamingResponse(get_chunk(), status_code=206, headers={
        "Content-Range":  f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges":  "bytes",
        "Content-Length": str(end - start + 1),
        "Content-Type":   "video/mp4",
    })

@app.post("/upload")
async def upload_file(request: Request):
    """動画・音声・画像を拡張子で自動振り分けて保存する"""
    if not app.state.unlocked: raise HTTPException(status_code=403)
    form = await request.form()
    file = form.get("file")
    if not file: raise HTTPException(status_code=400, detail="No file provided")
    ext = os.path.splitext(file.filename)[1].lower()
    if ext in _AUDIO_EXTS:
        save_dir = AUDIO_DIR
    elif ext in _IMAGE_EXTS:
        save_dir = IMAGE_DIR
    else:
        save_dir = SECRET_DIR   # 動画 / その他
    save_path = os.path.join(save_dir, file.filename)
    with open(save_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"status": "ok", "filename": file.filename, "type":
            "audio" if ext in _AUDIO_EXTS else "image" if ext in _IMAGE_EXTS else "video"}

@app.get("/tags")
def get_tags():
    if not app.state.unlocked: raise HTTPException(status_code=403)
    return load_tags()

@app.post("/tags/{video_name}")
async def set_tags(video_name: str, request: Request):
    if not app.state.unlocked: raise HTTPException(status_code=403)
    body = await request.json()
    tags = load_tags()
    tags[video_name] = body.get("tags", [])
    save_tags(tags)
    return {"status": "ok"}

@app.get("/audio")
def list_audio():
    """保存済み音声ファイル一覧"""
    if not app.state.unlocked: return {"audio": []}
    if not os.path.exists(AUDIO_DIR): return {"audio": []}
    files = [f for f in os.listdir(AUDIO_DIR) if f.lower().endswith(_AUDIO_EXTS)]
    result = []
    for f in files:
        fp = os.path.join(AUDIO_DIR, f)
        result.append({"name": f, "size": os.path.getsize(fp)})
    return {"audio": result}

@app.get("/audio/{audio_name}")
async def stream_audio(audio_name: str, request: Request):
    """音声ファイルをレンジリクエスト対応でストリーム配信"""
    if not app.state.unlocked: raise HTTPException(status_code=403)
    path = os.path.join(AUDIO_DIR, audio_name)
    if not os.path.exists(path): raise HTTPException(status_code=404)
    ext = os.path.splitext(audio_name)[1].lower()
    mime_map = {'.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.m4a': 'audio/mp4',
                '.aac': 'audio/aac', '.ogg': 'audio/ogg', '.flac': 'audio/flac'}
    mime = mime_map.get(ext, 'audio/mpeg')
    file_size = os.stat(path).st_size
    range_header = request.headers.get("range")
    start, end = 0, file_size - 1
    if range_header:
        parts = range_header.replace("bytes=", "").split("-")
        start = int(parts[0])
        end = int(parts[1]) if parts[1] else file_size - 1
    def get_chunk():
        with open(path, "rb") as f:
            f.seek(start)
            yield f.read(end - start + 1)
    return StreamingResponse(get_chunk(), status_code=206, headers={
        "Content-Range":  f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges":  "bytes",
        "Content-Length": str(end - start + 1),
        "Content-Type":   mime,
    })

@app.delete("/audio/{audio_name}")
def delete_audio(audio_name: str):
    """音声ファイルを削除"""
    if not app.state.unlocked: raise HTTPException(status_code=403)
    path = os.path.join(AUDIO_DIR, audio_name)
    if not os.path.exists(path): raise HTTPException(status_code=404)
    os.remove(path)
    return {"status": "ok"}

@app.get("/images")
def list_images():
    """保存済み画像ファイル一覧"""
    if not app.state.unlocked: return {"images": []}
    if not os.path.exists(IMAGE_DIR): return {"images": []}
    files = [f for f in os.listdir(IMAGE_DIR) if f.lower().endswith(_IMAGE_EXTS)]
    result = []
    for f in files:
        fp = os.path.join(IMAGE_DIR, f)
        result.append({"name": f, "size": os.path.getsize(fp)})
    return {"images": result}

@app.get("/images/{image_name}")
def serve_image(image_name: str):
    """画像ファイルを返す"""
    if not app.state.unlocked: raise HTTPException(status_code=403)
    path = os.path.join(IMAGE_DIR, image_name)
    if not os.path.exists(path): raise HTTPException(status_code=404)
    ext = os.path.splitext(image_name)[1].lower()
    mime = "image/png" if ext == ".png" else "image/jpeg"
    return FileResponse(path, media_type=mime)

@app.delete("/images/{image_name}")
def delete_image(image_name: str):
    """画像ファイルを削除"""
    if not app.state.unlocked: raise HTTPException(status_code=403)
    path = os.path.join(IMAGE_DIR, image_name)
    if not os.path.exists(path): raise HTTPException(status_code=404)
    os.remove(path)
    return {"status": "ok"}

@app.delete("/videos/{video_name}")
def delete_video(video_name: str):
    """動画ファイルを削除"""
    if not app.state.unlocked: raise HTTPException(status_code=403)
    path = os.path.join(SECRET_DIR, video_name)
    if not os.path.exists(path): raise HTTPException(status_code=404)
    os.remove(path)
    return {"status": "ok"}

@app.get("/thumb/{video_name}")
def get_thumbnail(video_name: str):
    if not app.state.unlocked: raise HTTPException(status_code=403)
    thumb_path = os.path.join(THUMB_DIR, video_name + ".jpg")
    if not os.path.exists(thumb_path):
        video_path = os.path.join(SECRET_DIR, video_name)
        if not os.path.exists(video_path): raise HTTPException(status_code=404)
        try:
            ffmpeg = FFMPEG_PATH if os.path.exists(FFMPEG_PATH) else "ffmpeg"
            subprocess.run([
                ffmpeg, "-i", video_path,
                "-ss", "00:00:01", "-vframes", "1",
                "-vf", "scale=320:-1", "-q:v", "5", thumb_path
            ], capture_output=True, timeout=30)
        except: raise HTTPException(status_code=500)
    if not os.path.exists(thumb_path): raise HTTPException(status_code=404)
    return FileResponse(thumb_path, media_type="image/jpeg")


# ── LADA エンドポイント（ENABLE_LADA=True で有効） ────────────
@app.get("/lada/status")
def lada_status():
    if not ENABLE_LADA: raise HTTPException(status_code=403, detail="disabled")
    if not app.state.unlocked: raise HTTPException(status_code=403)
    return {"available": os.path.exists(LADA_CLI_PATH), "jobs": lada_jobs}

@app.post("/lada/process/{video_name}")
async def lada_process(video_name: str):
    if not ENABLE_LADA: raise HTTPException(status_code=403, detail="disabled")
    if not app.state.unlocked: raise HTTPException(status_code=403)
    input_path = os.path.join(SECRET_DIR, video_name)
    if not os.path.exists(input_path): raise HTTPException(status_code=404)
    if not os.path.exists(LADA_CLI_PATH): raise HTTPException(status_code=500)
    import time
    job_id = f"{video_name}_{int(time.time())}"
    output_name = video_name.rsplit(".", 1)[0] + ".restored.mp4"
    output_path = os.path.join(SECRET_DIR, output_name)
    lada_jobs[job_id] = {"status": "processing", "input": video_name,
                         "output": None, "progress": 0, "error": None}
    def run_lada():
        try:
            enc_args, _ = detect_encoding_args()
            process = subprocess.Popen(
                [LADA_CLI_PATH, "--input", input_path, "--output", output_path] + enc_args,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                cwd=os.path.dirname(LADA_CLI_PATH)
            )
            lines = []
            for line in process.stdout:
                line = line.strip(); lines.append(line)
                if "%" in line:
                    try: lada_jobs[job_id]["progress"] = float(line.split("%")[0].split()[-1])
                    except: pass
            process.wait()
            if process.returncode == 0 and os.path.exists(output_path):
                lada_jobs[job_id].update({"status": "done", "output": output_name, "progress": 100})
            else:
                lada_jobs[job_id].update({"status": "error", "error": "\n".join(lines[-5:])[:200]})
        except Exception as e:
            lada_jobs[job_id].update({"status": "error", "error": str(e)})
    threading.Thread(target=run_lada, daemon=True).start()
    return {"job_id": job_id, "status": "started"}

@app.get("/lada/job/{job_id}")
def lada_job_status(job_id: str):
    if not ENABLE_LADA: raise HTTPException(status_code=403, detail="disabled")
    if not app.state.unlocked: raise HTTPException(status_code=403)
    if job_id not in lada_jobs: raise HTTPException(status_code=404)
    return lada_jobs[job_id]


# ── JASNA エンドポイント（ENABLE_JASNA=True で有効） ──────────
@app.get("/jasna/status")
def jasna_status():
    if not ENABLE_JASNA: raise HTTPException(status_code=403, detail="disabled")
    if not app.state.unlocked: raise HTTPException(status_code=403)
    return {"available": os.path.exists(JASNA_CLI_PATH), "jobs": jasna_jobs}

@app.get("/jasna/presets")
def jasna_presets_get():
    if not ENABLE_JASNA: raise HTTPException(status_code=403, detail="disabled")
    if not app.state.unlocked: raise HTTPException(status_code=403)
    ps = load_jasna_presets()
    return {"presets": [{"id": i+1, "name": str(i+1), **p} for i, p in enumerate(ps)]}

@app.post("/jasna/presets")
async def jasna_save_preset(request: Request):
    if not ENABLE_JASNA: raise HTTPException(status_code=403, detail="disabled")
    if not app.state.unlocked: raise HTTPException(status_code=403)
    body = await request.json()
    ps = load_jasna_presets(); ps.append(body); save_jasna_presets(ps)
    return {"status": "ok", "id": len(ps)}

@app.delete("/jasna/presets/{preset_id}")
def jasna_delete_preset(preset_id: int):
    if not ENABLE_JASNA: raise HTTPException(status_code=403, detail="disabled")
    if not app.state.unlocked: raise HTTPException(status_code=403)
    ps = load_jasna_presets()
    if preset_id < 1 or preset_id > len(ps): raise HTTPException(status_code=404)
    ps.pop(preset_id - 1); save_jasna_presets(ps)
    return {"status": "ok"}

@app.post("/jasna/process/{video_name}")
async def jasna_process(video_name: str, request: Request):
    if not ENABLE_JASNA: raise HTTPException(status_code=403, detail="disabled")
    if not app.state.unlocked: raise HTTPException(status_code=403)
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    preset_id = body.get("preset_id") if body else None
    input_path = os.path.join(SECRET_DIR, video_name)
    if not os.path.exists(input_path): raise HTTPException(status_code=404)
    if not os.path.exists(JASNA_CLI_PATH): raise HTTPException(status_code=500)
    import time
    job_id = f"{video_name}_{int(time.time())}"
    output_name = video_name.rsplit(".", 1)[0] + ".jasna.mp4"
    output_path = os.path.join(SECRET_DIR, output_name)
    jasna_jobs[job_id] = {"status": "processing", "input": video_name,
                          "output": None, "progress": 0, "error": None}
    def run_jasna():
        try:
            _, enc_label = detect_encoding_args()
            has_nvidia = "NVIDIA" in enc_label
            jasna_args = ["--fp16"] if has_nvidia else []
            if preset_id:
                ps = load_jasna_presets()
                idx = preset_id - 1
                if 0 <= idx < len(ps):
                    p = ps[idx]
                    if p.get("fp16") and has_nvidia: jasna_args += ["--fp16"]
                    if p.get("denoise", "none") != "none": jasna_args += ["--denoise", p["denoise"]]
            process = subprocess.Popen(
                [JASNA_CLI_PATH, "--input", input_path, "--output", output_path] + jasna_args,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                cwd=os.path.dirname(JASNA_CLI_PATH)
            )
            lines = []
            for line in process.stdout:
                line = line.strip(); lines.append(line)
                if "%" in line:
                    try: jasna_jobs[job_id]["progress"] = float(line.split("%")[0].split()[-1])
                    except: pass
            process.wait()
            if process.returncode == 0 and os.path.exists(output_path):
                jasna_jobs[job_id].update({"status": "done", "output": output_name, "progress": 100})
            else:
                jasna_jobs[job_id].update({"status": "error", "error": "\n".join(lines[-5:])[:200]})
        except Exception as e:
            jasna_jobs[job_id].update({"status": "error", "error": str(e)})
    threading.Thread(target=run_jasna, daemon=True).start()
    return {"job_id": job_id, "status": "started"}

@app.get("/jasna/job/{job_id}")
def jasna_job_status(job_id: str):
    if not ENABLE_JASNA: raise HTTPException(status_code=403, detail="disabled")
    if not app.state.unlocked: raise HTTPException(status_code=403)
    if job_id not in jasna_jobs: raise HTTPException(status_code=404)
    return jasna_jobs[job_id]


# ════════════════════════════════════════════════════════════════
#  汎用 CLIツール コネクター API  (/tools)
# ════════════════════════════════════════════════════════════════

def _build_command(exe_path: str, template: str,
                   input_path: str, output_path: str,
                   params: dict) -> list:
    """
    コマンドテンプレートから実行コマンドリストを生成する。

    テンプレート例:
      --input {input} --output {output} --scale {scale} --quality {quality}

    {input}  → input_path に置換
    {output} → output_path に置換
    {key}    → params[key] の値に置換
    """
    # テンプレートをトークン分割（引用符を尊重）
    try:
        parts = shlex.split(template, posix=False)
    except Exception:
        parts = template.split()

    result = []
    for part in parts:
        part = part.replace("{input}",  input_path)
        part = part.replace("{output}", output_path)
        for key, value in params.items():
            part = part.replace(f"{{{key}}}", str(value))
        # 引用符を除去（shlex が付ける場合）
        if len(part) >= 2 and part[0] == part[-1] and part[0] in ('"', "'"):
            part = part[1:-1]
        result.append(part)
    return [exe_path] + result


@app.get("/tools")
def list_tools():
    """
    登録済みツール一覧を返す。
    EXE が存在するツールのみ available=True になる。
    """
    if not app.state.unlocked: raise HTTPException(status_code=403)
    profiles = load_tool_profiles()
    result = []
    for t in profiles:
        exe = t.get("exe_path", "")
        result.append({
            "id":          t.get("id", ""),
            "name":        t.get("name", ""),
            "description": t.get("description", ""),
            "available":   os.path.exists(exe),
            "params":      t.get("params", []),
        })
    return {"tools": result}


@app.post("/tools/{tool_id}/process/{video_name}")
async def tool_process(tool_id: str, video_name: str, request: Request):
    """
    指定ツールで動画を処理するジョブを開始する。
    リクエストボディ: パラメーターのキー/値 JSON
    """
    if not app.state.unlocked: raise HTTPException(status_code=403)

    # ツールプロファイルを検索
    profiles = load_tool_profiles()
    tool = next((t for t in profiles if t.get("id") == tool_id), None)
    if tool is None:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_id}' not registered")

    exe_path = tool.get("exe_path", "")
    if not os.path.exists(exe_path):
        raise HTTPException(status_code=500, detail=f"EXE not found: {exe_path}")

    input_path = os.path.join(SECRET_DIR, video_name)
    if not os.path.exists(input_path):
        raise HTTPException(status_code=404, detail="Video not found")

    # リクエストボディからパラメーターを取得
    try:
        params = await request.json()
    except Exception:
        params = {}
    if not isinstance(params, dict):
        params = {}

    # 出力ファイル名: input_stem.tool_id.mp4
    stem = video_name.rsplit(".", 1)[0]
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in tool_id)
    output_name = f"{stem}.{safe_id}.mp4"
    output_path = os.path.join(SECRET_DIR, output_name)

    import time
    job_id = f"{tool_id}_{video_name}_{int(time.time())}"
    tool_jobs[job_id] = {
        "status":    "processing",
        "tool_id":   tool_id,
        "tool_name": tool.get("name", tool_id),
        "input":     video_name,
        "output":    None,
        "progress":  0,
        "error":     None,
    }

    template = tool.get("command_template", "--input {input} --output {output}")

    def run_tool():
        try:
            cmd = _build_command(exe_path, template, input_path, output_path, params)
            dlog(f"[tool:{tool_id}] cmd={cmd}")
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                cwd=os.path.dirname(exe_path),
            )
            lines = []
            for line in process.stdout:
                line = line.strip()
                lines.append(line)
                dlog(f"[tool:{tool_id}] {line}")
                if "%" in line:
                    try:
                        pct = float(line.split("%")[0].split()[-1])
                        tool_jobs[job_id]["progress"] = pct
                    except Exception:
                        pass
            process.wait()
            dlog(f"[tool:{tool_id}] returncode={process.returncode}")
            if process.returncode == 0 and os.path.exists(output_path):
                tool_jobs[job_id].update({
                    "status":   "done",
                    "output":   output_name,
                    "progress": 100,
                })
            else:
                tool_jobs[job_id].update({
                    "status": "error",
                    "error":  "\n".join(lines[-10:])[:300],
                })
        except Exception as e:
            import traceback
            dlog(f"[tool:{tool_id}] EXCEPTION: {e}\n{traceback.format_exc()}")
            tool_jobs[job_id].update({"status": "error", "error": str(e)})

    threading.Thread(target=run_tool, daemon=True).start()
    return {"job_id": job_id, "status": "started"}


@app.get("/tools/job/{job_id}")
def tool_job_status(job_id: str):
    if not app.state.unlocked: raise HTTPException(status_code=403)
    if job_id not in tool_jobs: raise HTTPException(status_code=404)
    return tool_jobs[job_id]


# ════════════════════════════════════════════════════════════════
#  GUI
# ════════════════════════════════════════════════════════════════
ctk.set_appearance_mode("dark")
BG_MAIN, BG_CARD, BG_INPUT = "#080C18", "#0D1929", "#0A1220"
ACCENT, ACCENT_DIM = "#4A9EFF", "#1E3A5F"
TEXT_PRI, TEXT_SEC = "#E8F4FF", "#4A6080"
DANGER, SUCCESS = "#FF4A4A", "#4AFF9E"

APP_VERSION = "v1.1.0"

def get_detection_models():
    default_models = ["rfdetr-v5", "lada-yolo-v4"]
    weights_dir = os.path.join(BASE_DIR, "tools", "jasna", "model_weights")
    if not os.path.exists(weights_dir): return default_models
    found = []
    for f in os.listdir(weights_dir):
        name = f[:-5] if f.endswith(".onnx") else (f[:-3] if f.endswith(".pt") else None)
        if name and name not in found: found.append(name)
    return found if found else default_models


class PrivForgeHub(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("PrivForge Hub")
        self.geometry("560x820")
        self.configure(fg_color=BG_MAIN)
        self.resizable(False, False)
        self.is_first_time = not os.path.exists(PASS_FILE)
        self._server_running = False
        self._server = None
        self._build_ui()

    # ── UI 構築 ──────────────────────────────────────────────
    def _build_ui(self):
        # ヘッダー
        header = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=0, height=80)
        header.pack(fill="x")
        header.pack_propagate(False)
        ctk.CTkLabel(header, text="PrivForge", font=("Arial Black", 24),
                     text_color=TEXT_PRI).place(x=24, y=16)
        ctk.CTkLabel(header, text="Hub", font=("Arial", 18),
                     text_color=ACCENT).place(x=162, y=24)
        ctk.CTkLabel(header, text=APP_VERSION, font=("Arial", 11),
                     text_color=TEXT_SEC).place(x=24, y=52)

        _, encoder_label = detect_encoding_args()
        ctk.CTkLabel(header, text=f"⚡ {encoder_label}", font=("Arial", 10),
                     text_color=SUCCESS if "NVIDIA" in encoder_label else ACCENT).place(x=90, y=52)

        ctk.CTkFrame(self, fg_color=ACCENT_DIM, height=1, corner_radius=0).pack(fill="x")
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.pack(fill="both", expand=True, padx=24, pady=20)

        # 認証カード
        auth_card = ctk.CTkFrame(main, fg_color=BG_CARD, corner_radius=12)
        auth_card.pack(fill="x", pady=(0, 16))
        ctk.CTkLabel(auth_card, text="AUTHENTICATION", font=("Arial", 11),
                     text_color=TEXT_SEC).pack(anchor="w", padx=20, pady=(16, 4))
        self.guide_label = ctk.CTkLabel(auth_card,
                                        text="INITIALIZE" if self.is_first_time else "LOCKED",
                                        font=("Arial Black", 14),
                                        text_color=ACCENT if self.is_first_time else DANGER)
        self.guide_label.pack(anchor="w", padx=20, pady=(0, 12))
        self.pass_entry = ctk.CTkEntry(auth_card, placeholder_text="Password", show="*",
                                       height=44, corner_radius=8,
                                       fg_color=BG_INPUT, border_color=ACCENT_DIM, text_color=TEXT_PRI)
        self.pass_entry.pack(fill="x", padx=20, pady=(0, 12))
        self.pass_entry.bind("<Return>", lambda e: self.handle_password())
        self.action_btn = ctk.CTkButton(auth_card,
                                        text="INITIALIZE" if self.is_first_time else "UNLOCK",
                                        command=self.handle_password,
                                        height=44, corner_radius=8,
                                        fg_color=ACCENT, text_color="#000000",
                                        font=("Arial Black", 14))
        self.action_btn.pack(fill="x", padx=20, pady=(0, 16))

        # ステータスカード
        status_card = ctk.CTkFrame(main, fg_color=BG_CARD, corner_radius=12)
        status_card.pack(fill="x", pady=(0, 16))
        self.status_label = ctk.CTkLabel(status_card, text="● STANDBY",
                                         font=("Arial Black", 14), text_color=TEXT_SEC)
        self.status_label.pack(anchor="w", padx=20, pady=16)

        # ファイルリストカード
        list_card = ctk.CTkFrame(main, fg_color=BG_CARD, corner_radius=12)
        list_card.pack(fill="x", pady=(0, 16))
        self.file_list = ctk.CTkTextbox(list_card, height=180, corner_radius=8,
                                        fg_color=BG_INPUT, border_color=ACCENT_DIM,
                                        font=("Consolas", 12), text_color=TEXT_PRI)
        self.file_list.pack(fill="x", padx=20, pady=16)
        self.file_list.insert("0.0", "  —  LOCKED  —")
        self.file_list.configure(state="disabled")
        # 後方互換: 旧コードが video_list を参照している箇所のため alias を設定
        self.video_list = self.file_list

        # ボタン行
        btn_row = ctk.CTkFrame(main, fg_color="transparent")
        btn_row.pack(fill="x", pady=(0, 16))
        self.qr_btn = ctk.CTkButton(btn_row, text="Show QR", command=self._show_qr,
                                    state="disabled", height=40, corner_radius=8,
                                    fg_color=ACCENT_DIM, hover_color="#2A4A6F",
                                    text_color=TEXT_SEC, font=("Arial", 13))
        self.qr_btn.grid(row=0, column=0, padx=(0, 4), sticky="ew")
        self.open_btn = ctk.CTkButton(btn_row, text="Open Folder", command=self.open_folder,
                                      state="disabled", height=40, corner_radius=8,
                                      fg_color=ACCENT_DIM, text_color=TEXT_SEC, font=("Arial", 13))
        self.open_btn.grid(row=0, column=1, padx=4, sticky="ew")
        self.refresh_btn = ctk.CTkButton(btn_row, text="Refresh", command=self.refresh_list,
                                         state="disabled", height=40, corner_radius=8,
                                         fg_color=ACCENT_DIM, text_color=TEXT_SEC, font=("Arial", 13))
        self.refresh_btn.grid(row=0, column=2, padx=4, sticky="ew")
        self.change_dir_btn = ctk.CTkButton(btn_row, text="Change Dir", command=self.change_vault_dir,
                                            state="disabled", height=40, corner_radius=8,
                                            fg_color=ACCENT_DIM, text_color=TEXT_SEC, font=("Arial", 13))
        self.change_dir_btn.grid(row=0, column=3, padx=(4, 0), sticky="ew")
        for i in range(4): btn_row.columnconfigure(i, weight=1)

        # START SERVER ボタン
        self.start_btn = ctk.CTkButton(main, text="START SERVER", command=self.start_server,
                                       state="disabled", height=60, corner_radius=10,
                                       fg_color=ACCENT_DIM, text_color=TEXT_SEC,
                                       font=("Arial Black", 18))
        self.start_btn.pack(fill="x")

        # ── Tool Manager ボタン（常時表示） ──
        self.tool_btn = ctk.CTkButton(main, text="⚙  Tool Manager",
                                      command=self._open_tool_manager,
                                      height=44, corner_radius=8,
                                      fg_color=ACCENT_DIM, text_color=TEXT_SEC,
                                      font=("Arial", 13), state="disabled")
        self.tool_btn.pack(fill="x", pady=(8, 0))

        # JASNA プリセット管理（ENABLE_JASNA=True かつ jasna-cli が存在する場合のみ）
        if ENABLE_JASNA and os.path.exists(JASNA_CLI_PATH):
            self.preset_btn = ctk.CTkButton(main, text="Jasna Preset Manager",
                                            command=self._open_preset_manager,
                                            height=40, corner_radius=8,
                                            fg_color=ACCENT_DIM, text_color=TEXT_SEC,
                                            font=("Arial", 13), state="disabled")
            self.preset_btn.pack(fill="x", pady=(8, 0))
        else:
            self.preset_btn = None

        self.protocol("WM_DELETE_WINDOW", lambda: os._exit(0))

    # ── 認証 ────────────────────────────────────────────────
    def hash_password(self, pwd):
        return hashlib.sha256(pwd.encode()).hexdigest()

    def handle_password(self):
        pwd = self.pass_entry.get()
        if not pwd: return
        if self.is_first_time:
            with open(PASS_FILE, "w") as f: f.write(self.hash_password(pwd))
            self.is_first_time = False
            self.guide_label.configure(text="RE-ENTER PASSWORD", text_color=ACCENT)
            self.action_btn.configure(text="UNLOCK")
            self.pass_entry.delete(0, "end")
        else:
            with open(PASS_FILE, "r") as f: saved = f.read()
            if self.hash_password(pwd) == saved:
                self.unlock_success()
            else:
                self.status_label.configure(text="● AUTHENTICATION FAILED", text_color=DANGER)

    def unlock_success(self):
        app.state.unlocked = True
        self.guide_label.configure(text="ACCESS GRANTED", text_color=SUCCESS)
        self.status_label.configure(text="● READY", text_color=ACCENT)
        self.pass_entry.configure(state="disabled")
        self.action_btn.configure(state="disabled", fg_color=ACCENT_DIM)
        for b in [self.open_btn, self.refresh_btn, self.change_dir_btn,
                  self.qr_btn, self.tool_btn]:
            b.configure(state="normal", text_color=TEXT_PRI)
        self.start_btn.configure(state="normal", fg_color=ACCENT, text_color="#000000")
        if self.preset_btn:
            self.preset_btn.configure(state="normal", text_color=TEXT_PRI)
        self.refresh_list()

    # ── ファイル操作 ─────────────────────────────────────────
    def open_folder(self):
        if not app.state.unlocked: return
        # サブメニューで開くフォルダを選択
        import tkinter as tk
        menu = tk.Menu(self, tearoff=0, bg="#0D1929", fg="#E8F4FF",
                       activebackground="#1E3A5F", activeforeground="#E8F4FF",
                       font=("Arial", 11))
        menu.add_command(label="▶  Videos",
                         command=lambda: subprocess.Popen(f'explorer "{SECRET_DIR}"'))
        menu.add_command(label="♪  Audio",
                         command=lambda: subprocess.Popen(f'explorer "{AUDIO_DIR}"'))
        menu.add_command(label="🖼  Images",
                         command=lambda: subprocess.Popen(f'explorer "{IMAGE_DIR}"'))
        try:
            menu.tk_popup(self.open_btn.winfo_rootx(),
                          self.open_btn.winfo_rooty() + self.open_btn.winfo_height())
        finally:
            menu.grab_release()

    def change_vault_dir(self):
        from tkinter import filedialog
        new_dir = filedialog.askdirectory()
        if new_dir:
            global SECRET_DIR
            SECRET_DIR = new_dir
            self.refresh_list()

    def refresh_list(self):
        if not app.state.unlocked: return
        videos = sorted([f for f in os.listdir(SECRET_DIR)
                         if f.lower().endswith(_VIDEO_EXTS)])
        audios = sorted([f for f in (os.listdir(AUDIO_DIR) if os.path.exists(AUDIO_DIR) else [])
                         if f.lower().endswith(_AUDIO_EXTS)])
        images = sorted([f for f in (os.listdir(IMAGE_DIR) if os.path.exists(IMAGE_DIR) else [])
                         if f.lower().endswith(_IMAGE_EXTS)])

        self.file_list.configure(state="normal")
        self.file_list.delete("0.0", "end")

        def section(icon, label, files):
            if not files: return
            self.file_list.insert("end", f" {icon} {label} ({len(files)})\n")
            for f in files:
                self.file_list.insert("end", f"    {f}\n")

        total = len(videos) + len(audios) + len(images)
        if total == 0:
            self.file_list.insert("end", "  No files in vault")
        else:
            section("▶", "Videos", videos)
            section("♪", "Audio",  audios)
            section("🖼", "Images", images)

        self.file_list.configure(state="disabled")

    # ── サーバー起動 ─────────────────────────────────────────
    def start_server(self):
        if self._server_running: return
        self._server_running = True
        dlog("start_server called")
        self.status_label.configure(text="● BROADCASTING  —  0.0.0.0:8000",
                                    text_color=SUCCESS)
        self.start_btn.configure(state="disabled", text="SERVER ACTIVE",
                                 fg_color="#0A2A10", text_color=SUCCESS)
        def run_uvicorn():
            try:
                config = uvicorn.Config(app=app, host="0.0.0.0", port=8000,
                                        log_level="warning", log_config=None, loop="asyncio")
                self._server = uvicorn.Server(config)
                self._server.run()
            except Exception as e:
                import traceback
                dlog(f"uvicorn ERROR: {e}\n{traceback.format_exc()}")
        threading.Thread(target=run_uvicorn, daemon=True).start()
        self._show_qr()

    def _get_local_ip(self):
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    # ════════════════════════════════════════════════════════
    #  Tool Manager ウィンドウ
    # ════════════════════════════════════════════════════════
    def _open_tool_manager(self):
        win = ctk.CTkToplevel(self)
        win.title("Tool Manager")
        win.geometry("680x780")
        win.configure(fg_color=BG_MAIN)
        win.grab_set()

        # ── ヘッダー ──
        hdr = ctk.CTkFrame(win, fg_color=BG_CARD, corner_radius=0, height=50)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        ctk.CTkLabel(hdr, text="⚙  Tool Manager", font=("Arial Black", 15),
                     text_color=TEXT_PRI).pack(side="left", padx=20, pady=12)

        pane = ctk.CTkFrame(win, fg_color="transparent")
        pane.pack(fill="both", expand=True, padx=20, pady=16)

        # ── 登録済みツール一覧 ──────────────────────────────
        def sec_label(text):
            ctk.CTkLabel(pane, text=text, font=("Arial Black", 12),
                         text_color=ACCENT).pack(anchor="w", pady=(8, 4))
            ctk.CTkFrame(pane, fg_color=ACCENT_DIM, height=1).pack(fill="x")

        sec_label("Registered Tools")
        list_frame = ctk.CTkFrame(pane, fg_color=BG_CARD, corner_radius=8)
        list_frame.pack(fill="x", pady=(4, 12))

        def refresh_tool_list():
            for w in list_frame.winfo_children():
                w.destroy()
            tools = load_tool_profiles()
            if not tools:
                ctk.CTkLabel(list_frame, text="  No tools registered.",
                             text_color=TEXT_SEC, font=("Arial", 12)).pack(pady=12)
                return
            for i, t in enumerate(tools):
                row = ctk.CTkFrame(list_frame, fg_color="transparent")
                row.pack(fill="x", padx=10, pady=4)
                exe_ok = os.path.exists(t.get("exe_path", ""))
                dot_color = SUCCESS if exe_ok else DANGER
                ctk.CTkLabel(row, text="●", text_color=dot_color,
                             font=("Arial", 11)).pack(side="left", padx=(0, 6))
                name_desc = t.get("name", "?")
                if t.get("description"):
                    name_desc += f"  —  {t['description'][:40]}"
                ctk.CTkLabel(row, text=name_desc, text_color=TEXT_PRI,
                             font=("Arial", 12), anchor="w").pack(side="left", fill="x", expand=True)

                def delete_tool(idx=i):
                    ts = load_tool_profiles()
                    ts.pop(idx)
                    save_tool_profiles(ts)
                    refresh_tool_list()

                ctk.CTkButton(row, text="Delete", width=60, height=24,
                              fg_color=DANGER, text_color="white",
                              font=("Arial", 11), command=delete_tool).pack(side="right")

        refresh_tool_list()

        # ── 新規登録フォーム ────────────────────────────────
        sec_label("Register New Tool")

        scroll = ctk.CTkScrollableFrame(pane, fg_color=BG_CARD, corner_radius=8, height=350)
        scroll.pack(fill="x", pady=(4, 0))

        def field(parent, label, placeholder="", width=None, height=None):
            ctk.CTkLabel(parent, text=label, text_color=TEXT_PRI,
                         font=("Arial", 12), anchor="w").pack(anchor="w", pady=(8, 2))
            kw = dict(fg_color=BG_INPUT, border_color=ACCENT_DIM, text_color=TEXT_PRI)
            if height:
                w = ctk.CTkTextbox(parent, height=height, corner_radius=6, **kw)
                w.pack(fill="x")
            else:
                w = ctk.CTkEntry(parent, placeholder_text=placeholder,
                                 corner_radius=6, height=36, **kw)
                w.pack(fill="x")
            return w

        v_name     = field(scroll, "Tool Name *",        "e.g. AI Upscaler 4x")
        v_desc     = field(scroll, "Description",        "e.g. Upscales video 4x with AI")
        v_exe      = field(scroll, "EXE Path *",         r"e.g. C:\tools\mytool-cli.exe")

        # EXE Browse ボタン
        def browse_exe():
            from tkinter import filedialog
            p = filedialog.askopenfilename(filetypes=[("Executable", "*.exe"), ("All", "*.*")])
            if p:
                v_exe.delete(0, "end")
                v_exe.insert(0, p)

        ctk.CTkButton(scroll, text="Browse...", command=browse_exe,
                      height=30, corner_radius=6, fg_color=ACCENT_DIM,
                      text_color=TEXT_PRI, font=("Arial", 11)).pack(anchor="w", pady=(2, 0))

        v_cmd = field(scroll, "Command Template *",
                      "--input {input} --output {output} --scale {scale}")

        # テンプレート説明
        ctk.CTkLabel(scroll,
                     text="  {input} and {output} are reserved. Other {key} become params.",
                     text_color=TEXT_SEC, font=("Arial", 10), anchor="w").pack(anchor="w")

        # Params JSON
        v_params = field(scroll, "Params (JSON array, optional)", height=100)
        PARAMS_EXAMPLE = (
            '[\n'
            '  {"key":"scale","label":"Scale","type":"select","default":"4","options":["2","4"]},\n'
            '  {"key":"quality","label":"Quality","type":"number","default":80,"hint":"0-100"}\n'
            ']'
        )
        v_params.insert("0.0", PARAMS_EXAMPLE)

        # エラー/成功メッセージ用ラベル
        msg_lbl = ctk.CTkLabel(pane, text="", font=("Arial", 11))
        msg_lbl.pack(pady=(8, 0))

        def save_new_tool():
            name    = v_name.get().strip()
            desc    = v_desc.get().strip()
            exe     = v_exe.get().strip()
            cmd_tpl = v_cmd.get().strip()
            params_raw = v_params.get("0.0", "end").strip()

            # バリデーション
            if not name:
                msg_lbl.configure(text="✗ Name is required.", text_color=DANGER); return
            if not exe:
                msg_lbl.configure(text="✗ EXE Path is required.", text_color=DANGER); return
            if not cmd_tpl:
                msg_lbl.configure(text="✗ Command Template is required.", text_color=DANGER); return
            if not os.path.exists(exe):
                msg_lbl.configure(text=f"✗ EXE not found: {exe}", text_color=DANGER); return

            # Params JSON パース
            params_list = []
            if params_raw and params_raw != PARAMS_EXAMPLE:
                try:
                    parsed = json.loads(params_raw)
                    if not isinstance(parsed, list):
                        raise ValueError("Params must be a JSON array")
                    params_list = parsed
                except Exception as ex:
                    msg_lbl.configure(text=f"✗ Params JSON error: {ex}", text_color=DANGER)
                    return

            # 保存
            tool_id = str(uuid.uuid4())[:8]
            new_tool = {
                "id":               tool_id,
                "name":             name,
                "description":      desc,
                "exe_path":         exe,
                "command_template": cmd_tpl,
                "params":           params_list,
            }
            ts = load_tool_profiles()
            ts.append(new_tool)
            save_tool_profiles(ts)

            # フォームリセット
            v_name.delete(0, "end")
            v_desc.delete(0, "end")
            v_exe.delete(0, "end")
            v_cmd.delete(0, "end")
            v_params.delete("0.0", "end")
            v_params.insert("0.0", PARAMS_EXAMPLE)
            msg_lbl.configure(text=f"✓ '{name}' registered (id: {tool_id})", text_color=SUCCESS)
            refresh_tool_list()

        ctk.CTkButton(pane, text="Register Tool", command=save_new_tool,
                      height=44, corner_radius=8,
                      fg_color=ACCENT, text_color="#000000",
                      font=("Arial Black", 13)).pack(fill="x", pady=(12, 0))

    # ════════════════════════════════════════════════════════
    #  Jasna Preset Manager（ENABLE_JASNA 用、変更なし）
    # ════════════════════════════════════════════════════════
    def _open_preset_manager(self):
        import tkinter as tk
        win = ctk.CTkToplevel(self)
        win.title("Jasna Preset Manager")
        win.geometry("700x860")
        win.configure(fg_color=BG_MAIN)
        win.grab_set()

        scroll = ctk.CTkScrollableFrame(win, fg_color=BG_MAIN)
        scroll.pack(fill="both", expand=True, padx=16, pady=16)

        detection_models = get_detection_models()
        vars = {}
        vars["fp16"]                      = ctk.BooleanVar(value=True)
        vars["denoise"]                   = ctk.StringVar(value="none")
        vars["denoise_step"]              = ctk.StringVar(value="after_primary")
        vars["compile_basicvsrpp"]        = ctk.BooleanVar(value=True)
        vars["enable_crossfade"]          = ctk.BooleanVar(value=True)
        vars["max_clip_size"]             = ctk.StringVar(value="90")
        vars["temporal_overlap"]          = ctk.StringVar(value="8")
        vars["batch_size"]                = ctk.StringVar(value="")
        vars["secondary_restoration"]     = ctk.StringVar(value="none")
        vars["rtx_scale"]                 = ctk.StringVar(value="4")
        vars["rtx_quality"]               = ctk.StringVar(value="high")
        vars["rtx_denoise"]               = ctk.StringVar(value="medium")
        vars["rtx_deblur"]                = ctk.StringVar(value="none")
        vars["tvai_model"]                = ctk.StringVar(value="iris-2")
        vars["tvai_scale"]                = ctk.StringVar(value="4")
        vars["tvai_ffmpeg_path"]          = ctk.StringVar(value="")
        vars["detection_model"]           = ctk.StringVar(
            value=detection_models[0] if detection_models else "rfdetr-v5")
        vars["detection_score_threshold"] = ctk.StringVar(value="0.25")

        def section(parent, title):
            ctk.CTkLabel(parent, text=title, font=("Arial Black", 12),
                         text_color=ACCENT).pack(anchor="w", pady=(12, 4))
            ctk.CTkFrame(parent, fg_color=ACCENT_DIM, height=1).pack(fill="x")
        def row(parent, label, widget_fn):
            f = ctk.CTkFrame(parent, fg_color="transparent")
            f.pack(fill="x", pady=3)
            ctk.CTkLabel(f, text=label, text_color=TEXT_PRI, width=220, anchor="w").pack(side="left")
            widget_fn(f)
        def combo(parent, var, values):
            ctk.CTkComboBox(parent, variable=var, values=values, width=160,
                            fg_color=BG_INPUT, border_color=ACCENT_DIM,
                            button_color=ACCENT_DIM, text_color=TEXT_PRI).pack(side="left")
        def check(parent, var):
            ctk.CTkCheckBox(parent, variable=var, text="", fg_color=ACCENT, width=30).pack(side="left")
        def entry(parent, var, width=160):
            ctk.CTkEntry(parent, textvariable=var, width=width,
                         fg_color=BG_INPUT, border_color=ACCENT_DIM, text_color=TEXT_PRI).pack(side="left")

        section(scroll, "Basic")
        row(scroll, "FP16 (NVIDIA recommended)", lambda p: check(p, vars["fp16"]))
        row(scroll, "Batch Size (blank=default)", lambda p: entry(p, vars["batch_size"]))
        section(scroll, "Restoration")
        row(scroll, "Denoise",            lambda p: combo(p, vars["denoise"], ["none","low","medium","high"]))
        row(scroll, "Denoise Step",       lambda p: combo(p, vars["denoise_step"], ["after_primary","after_secondary"]))
        row(scroll, "Compile BasicVSR++", lambda p: check(p, vars["compile_basicvsrpp"]))
        row(scroll, "Enable Crossfade",   lambda p: check(p, vars["enable_crossfade"]))
        row(scroll, "Max Clip Size",      lambda p: entry(p, vars["max_clip_size"], 100))
        row(scroll, "Temporal Overlap",   lambda p: entry(p, vars["temporal_overlap"], 100))
        section(scroll, "2nd Restoration")
        row(scroll, "Secondary Restoration", lambda p: combo(p, vars["secondary_restoration"],
                                             ["none","unet-4x","tvai","rtx-super-res"]))
        section(scroll, "RTX Super Res")
        row(scroll, "RTX Scale",   lambda p: combo(p, vars["rtx_scale"], ["2","4"]))
        row(scroll, "RTX Quality", lambda p: combo(p, vars["rtx_quality"], ["low","medium","high","ultra"]))
        row(scroll, "RTX Denoise", lambda p: combo(p, vars["rtx_denoise"], ["none","low","medium","high","ultra"]))
        row(scroll, "RTX Deblur",  lambda p: combo(p, vars["rtx_deblur"], ["none","low","medium","high","ultra"]))
        section(scroll, "Topaz Video (tvai)")
        row(scroll, "TVAI Model",       lambda p: entry(p, vars["tvai_model"]))
        row(scroll, "TVAI Scale",       lambda p: combo(p, vars["tvai_scale"], ["1","2","4"]))
        row(scroll, "TVAI FFmpeg Path", lambda p: entry(p, vars["tvai_ffmpeg_path"], 300))
        section(scroll, "Detection")
        row(scroll, "Detection Model",           lambda p: combo(p, vars["detection_model"], detection_models))
        row(scroll, "Detection Score Threshold", lambda p: entry(p, vars["detection_score_threshold"], 100))

        section(scroll, "Saved Presets")
        preset_frame = ctk.CTkFrame(scroll, fg_color=BG_CARD, corner_radius=8)
        preset_frame.pack(fill="x", pady=4)

        def load_vars_from_preset(preset):
            for key, var in vars.items():
                if key not in preset: continue
                val = preset[key]
                var.set(bool(val) if isinstance(var, ctk.BooleanVar) else (str(val) if val is not None else ""))

        def refresh_preset_list():
            for w in preset_frame.winfo_children(): w.destroy()
            ps = load_jasna_presets()
            if not ps:
                ctk.CTkLabel(preset_frame, text="No presets", text_color=TEXT_SEC).pack(pady=8)
                return
            for i, p in enumerate(ps):
                pf = ctk.CTkFrame(preset_frame, fg_color="transparent")
                pf.pack(fill="x", padx=8, pady=2)
                ctk.CTkLabel(pf, text=f"Preset {i+1}", text_color=TEXT_PRI, width=100).pack(side="left")
                ctk.CTkLabel(pf, text=f"denoise={p.get('denoise','none')}  2nd={p.get('secondary_restoration','none')}",
                             text_color=TEXT_SEC, font=("Consolas", 11)).pack(side="left", padx=8)
                ctk.CTkButton(pf, text="Load", width=50, height=24, fg_color=ACCENT_DIM,
                              text_color=TEXT_PRI, command=lambda idx=i: load_vars_from_preset(load_jasna_presets()[idx])).pack(side="right", padx=(0,4))
                ctk.CTkButton(pf, text="Del", width=50, height=24, fg_color=DANGER,
                              text_color="white",
                              command=lambda idx=i: [load_jasna_presets().pop(idx), save_jasna_presets(load_jasna_presets()), refresh_preset_list()]).pack(side="right")

        refresh_preset_list()

        def save_preset():
            preset = {k: v.get() for k, v in vars.items()}
            for key in ["max_clip_size","temporal_overlap","batch_size","detection_score_threshold","rtx_scale","tvai_scale"]:
                val = preset.get(key, "")
                try:
                    preset[key] = (int(val) if key in ["max_clip_size","temporal_overlap","batch_size","rtx_scale","tvai_scale"] else float(val)) if val else None
                except: preset[key] = None
            ps = load_jasna_presets(); ps.append(preset); save_jasna_presets(ps)
            refresh_preset_list()
            ctk.CTkLabel(scroll, text=f"✓ Preset {len(ps)} saved", text_color=SUCCESS).pack(pady=4)

        ctk.CTkButton(scroll, text="Save Current Settings as Preset",
                      command=save_preset, height=44, corner_radius=8,
                      fg_color=ACCENT, text_color="#000000",
                      font=("Arial Black", 13)).pack(fill="x", pady=(16, 0))

    # ── QRコードウィンドウ ───────────────────────────────────
    def _show_qr(self):
        import qrcode
        from PIL import Image, ImageTk
        import tkinter as tk

        ip  = self._get_local_ip()
        url = f"http://{ip}:8000"

        qr = qrcode.QRCode(version=1, box_size=6, border=3)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#4A9EFF", back_color="#0D1929")
        img = img.resize((200, 200), Image.LANCZOS)

        qr_window = tk.Toplevel(self)
        qr_window.title("QR Code")
        qr_window.configure(bg="#0D1929")
        qr_window.resizable(False, False)

        tk_img = ImageTk.PhotoImage(img)
        tk.Label(qr_window, text="Scan with your phone to connect",
                 font=("Arial", 13, "bold"), fg="#4A9EFF", bg="#0D1929").pack(pady=(16, 4))
        tk.Label(qr_window, image=tk_img, bg="#0D1929").pack(padx=20)
        qr_window.tk_img = tk_img
        tk.Label(qr_window, text=url, font=("Consolas", 13, "bold"),
                 fg="#A6E22E", bg="#0D1929").pack(pady=(8, 4))
        tk.Label(qr_window, text="Open PrivForge app → PC tab to scan",
                 font=("Arial", 10), fg="#4A6080", bg="#0D1929").pack(pady=(0, 8))
        tk.Button(qr_window, text="Close", command=qr_window.destroy,
                  bg="#333333", fg="white", font=("Arial", 11),
                  relief="flat", padx=20, pady=6).pack(pady=(0, 16))


if __name__ == "__main__":
    gui = PrivForgeHub()
    gui.mainloop()
