"""
视频音频提取器 - 手机网页版
在电脑上启动后，手机浏览器打开 http://<电脑IP>:8088 即可使用
"""

import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from fastapi import FastAPI, File, UploadFile, Form, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
import uvicorn

# ── 配置 ───────────────────────────────────────────────────
HOST = "0.0.0.0"
# 云端 Render 注入 PORT 环境变量，本地默认 8088
PORT = int(os.environ.get("PORT", 8088))
IS_CLOUD = bool(os.environ.get("RENDER") or os.environ.get("PORT"))

# 工作目录：云端用 /tmp，本地用用户目录
if IS_CLOUD:
    WORK_DIR = "/tmp/audio_extractor_web"
else:
    WORK_DIR = os.path.join(os.path.expanduser("~"), ".audio_extractor_web")
os.makedirs(WORK_DIR, exist_ok=True)

# ── ffmpeg 路径 ────────────────────────────────────────────
def find_ffmpeg():
    # 云端 Docker 已通过 apt 安装 ffmpeg
    path = shutil.which("ffmpeg")
    if path:
        return path
    # 本地：桌面版打包的 ffmpeg
    local = os.path.join(os.path.expanduser("~"), ".audio_extractor", "ffmpeg", "ffmpeg.exe")
    if os.path.isfile(local):
        return local
    return None

FFMPEG_PATH = find_ffmpeg()

# ── 任务管理 ───────────────────────────────────────────────
tasks = {}  # task_id -> {status, log, output_path, filename, ...}
tasks_lock = threading.Lock()

FORMAT_MAP = {
    "MP3": {"ext": ".mp3", "args": ["-vn", "-acodec", "libmp3lame", "-q:a", "2"]},
    "WAV": {"ext": ".wav", "args": ["-vn", "-acodec", "pcm_s16le"]},
    "FLAC": {"ext": ".flac", "args": ["-vn", "-acodec", "flac"]},
    "AAC": {"ext": ".aac", "args": ["-vn", "-acodec", "aac", "-b:a", "192k"]},
}

# ── FastAPI 应用 ───────────────────────────────────────────
app = FastAPI(title="视频音频提取器")

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/api/ip")
async def get_ip():
    """返回本机局域网 IP"""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "127.0.0.1"
    return {"ip": ip, "port": PORT}


@app.post("/api/extract-url")
async def extract_url(url: str = Form(...), fmt: str = Form("MP3"),
                       save_video: bool = Form(False)):
    task_id = str(uuid.uuid4())[:8]
    with tasks_lock:
        tasks[task_id] = {
            "status": "processing", "log": [], "output_path": None,
            "filename": None, "video_path": None, "video_filename": None,
        }
    threading.Thread(target=_process_url, args=(task_id, url, fmt, save_video),
                      daemon=True).start()
    return {"task_id": task_id}


@app.post("/api/extract-file")
async def extract_file(file: UploadFile = File(...), fmt: str = Form("MP3")):
    task_id = str(uuid.uuid4())[:8]
    # 保存上传文件
    ext = os.path.splitext(file.filename)[1] or ".mp4"
    save_path = os.path.join(WORK_DIR, f"{task_id}_input{ext}")
    content = await file.read()
    with open(save_path, "wb") as f:
        f.write(content)

    with tasks_lock:
        tasks[task_id] = {
            "status": "processing", "log": [], "output_path": None,
            "filename": None, "video_path": None, "video_filename": None,
        }
    threading.Thread(target=_process_file, args=(task_id, save_path, file.filename, fmt),
                      daemon=True).start()
    return {"task_id": task_id}


@app.get("/api/status/{task_id}")
async def task_status(task_id: str):
    with tasks_lock:
        t = tasks.get(task_id)
    if not t:
        return JSONResponse({"error": "任务不存在"}, status_code=404)
    return {
        "status": t["status"],
        "log": t["log"],
        "filename": t.get("filename"),
        "video_filename": t.get("video_filename"),
    }


@app.get("/api/download/{task_id}")
async def download(task_id: str, type: str = "audio"):
    with tasks_lock:
        t = tasks.get(task_id)
    if not t:
        return JSONResponse({"error": "任务不存在"}, status_code=404)
    if type == "video" and t.get("video_path") and os.path.exists(t["video_path"]):
        return FileResponse(t["video_path"], filename=t["video_filename"],
                             media_type="application/octet-stream")
    if t["output_path"] and os.path.exists(t["output_path"]):
        return FileResponse(t["output_path"], filename=t["filename"],
                             media_type="application/octet-stream")
    return JSONResponse({"error": "文件未就绪"}, status_code=404)


# ── 处理逻辑 ───────────────────────────────────────────────
def _log(task_id, msg):
    with tasks_lock:
        if task_id in tasks:
            ts = time.strftime("%H:%M:%S")
            tasks[task_id]["log"].append(f"[{ts}] {msg}")


def _run_ffmpeg_cancelable(cmd, task_id):
    CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=CREATE_NO_WINDOW)
    while proc.poll() is None:
        time.sleep(0.3)
    stderr = proc.stderr.read().decode("utf-8", errors="replace")
    return proc.returncode == 0, stderr[-300:] if stderr else ""


def _process_file(task_id, file_path, original_name, fmt):
    try:
        cfg = FORMAT_MAP.get(fmt, FORMAT_MAP["MP3"])
        name = os.path.splitext(original_name)[0]
        out_path = os.path.join(WORK_DIR, f"{task_id}_out{cfg['ext']}")

        _log(task_id, f"开始提取: {original_name}")
        _log(task_id, f"输出格式: {fmt}")

        cmd = [FFMPEG_PATH, "-y", "-i", file_path] + cfg["args"] + [out_path]
        ok, err = _run_ffmpeg_cancelable(cmd, task_id)

        if ok:
            size = os.path.getsize(out_path) / (1024 * 1024)
            _log(task_id, f"提取成功 ({size:.1f}MB)")
            with tasks_lock:
                tasks[task_id]["status"] = "done"
                tasks[task_id]["output_path"] = out_path
                tasks[task_id]["filename"] = f"{name}{cfg['ext']}"
        else:
            _log(task_id, f"提取失败: {err}")
            with tasks_lock:
                tasks[task_id]["status"] = "error"

    except Exception as e:
        _log(task_id, f"异常: {e}")
        with tasks_lock:
            tasks[task_id]["status"] = "error"
    finally:
        try:
            os.remove(file_path)
        except OSError:
            pass


def _process_url(task_id, url, fmt, save_video):
    try:
        import yt_dlp

        cfg = FORMAT_MAP.get(fmt, FORMAT_MAP["MP3"])

        # 获取视频信息
        _log(task_id, "正在解析链接...")
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
                info = ydl.extract_info(url, download=False)
                title = info.get("title", "download")
        except Exception:
            title = "download"

        safe_title = re.sub(r'[\\/:*?"<>|]', '_', title)[:100]
        _log(task_id, f"标题: {safe_title}")

        if save_video:
            # ── 下载视频 + 提取音频 ──
            _log(task_id, "模式: 下载原视频 + 提取音频")

            video_path = os.path.join(WORK_DIR, f"{task_id}_video.mp4")
            video_opts = {
                "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "outtmpl": video_path,
                "quiet": True, "no_warnings": True,
                "noplaylist": True, "merge_output_format": "mp4",
                "progress_hooks": [lambda d: _check_cancel(task_id, d)],
            }
            if FFMPEG_PATH:
                video_opts["ffmpeg_location"] = os.path.dirname(FFMPEG_PATH)

            _log(task_id, "正在下载原视频...")
            try:
                with yt_dlp.YoutubeDL(video_opts) as ydl:
                    ydl.download([url])
            except _CancelExc:
                _log(task_id, "已取消")
                with tasks_lock:
                    tasks[task_id]["status"] = "error"
                return

            if os.path.exists(video_path):
                vsize = os.path.getsize(video_path) / (1024 * 1024)
                _log(task_id, f"原视频已下载 ({vsize:.1f}MB)")

                # 提取音频
                out_path = os.path.join(WORK_DIR, f"{task_id}_out{cfg['ext']}")
                _log(task_id, f"正在提取音频 -> {fmt}...")
                cmd = [FFMPEG_PATH, "-y", "-i", video_path] + cfg["args"] + [out_path]
                ok, err = _run_ffmpeg_cancelable(cmd, task_id)

                if ok:
                    asize = os.path.getsize(out_path) / (1024 * 1024)
                    _log(task_id, f"音频提取成功 ({asize:.1f}MB)")
                    with tasks_lock:
                        tasks[task_id]["status"] = "done"
                        tasks[task_id]["output_path"] = out_path
                        tasks[task_id]["filename"] = f"{safe_title}{cfg['ext']}"
                        tasks[task_id]["video_path"] = video_path
                        tasks[task_id]["video_filename"] = f"{safe_title}.mp4"
                else:
                    _log(task_id, f"音频提取失败: {err}")
                    with tasks_lock:
                        tasks[task_id]["status"] = "error"
            else:
                _log(task_id, "视频下载失败")
                with tasks_lock:
                    tasks[task_id]["status"] = "error"

        else:
            # ── 仅提取音频 ──
            _log(task_id, "模式: 仅提取音频")

            out_path = os.path.join(WORK_DIR, f"{task_id}_out{cfg['ext']}")

            if fmt == "MP3":
                pp = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
            elif fmt == "WAV":
                pp = [{"key": "FFmpegExtractAudio", "preferredcodec": "wav"}]
            elif fmt == "FLAC":
                pp = [{"key": "FFmpegExtractAudio", "preferredcodec": "flac"}]
            else:
                pp = [{"key": "FFmpegExtractAudio", "preferredcodec": "aac", "preferredquality": "192"}]

            ydl_opts = {
                "format": "bestaudio/best",
                "postprocessors": pp,
                "outtmpl": os.path.join(WORK_DIR, f"{task_id}_dl.%(ext)s"),
                "quiet": True, "no_warnings": True,
                "noplaylist": True,
                "progress_hooks": [lambda d: _check_cancel(task_id, d)],
            }
            if FFMPEG_PATH:
                ydl_opts["ffmpeg_location"] = os.path.dirname(FFMPEG_PATH)

            _log(task_id, "正在下载并提取音频...")
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
            except _CancelExc:
                _log(task_id, "已取消")
                with tasks_lock:
                    tasks[task_id]["status"] = "error"
                return

            # 查找输出
            expected = os.path.join(WORK_DIR, f"{task_id}_dl{cfg['ext']}")
            if os.path.exists(expected):
                size = os.path.getsize(expected) / (1024 * 1024)
                _log(task_id, f"提取成功 ({size:.1f}MB)")
                with tasks_lock:
                    tasks[task_id]["status"] = "done"
                    tasks[task_id]["output_path"] = expected
                    tasks[task_id]["filename"] = f"{safe_title}{cfg['ext']}"
            else:
                # 兜底查找
                for f in glob.glob(os.path.join(WORK_DIR, f"{task_id}_dl*")):
                    if f.endswith(cfg["ext"]):
                        size = os.path.getsize(f) / (1024 * 1024)
                        _log(task_id, f"提取成功 ({size:.1f}MB)")
                        with tasks_lock:
                            tasks[task_id]["status"] = "done"
                            tasks[task_id]["output_path"] = f
                            tasks[task_id]["filename"] = f"{safe_title}{cfg['ext']}"
                        return
                _log(task_id, "未找到输出文件")
                with tasks_lock:
                    tasks[task_id]["status"] = "error"

    except Exception as e:
        _log(task_id, f"失败: {e}")
        with tasks_lock:
            tasks[task_id]["status"] = "error"


class _CancelExc(Exception):
    pass

def _check_cancel(task_id, d):
    with tasks_lock:
        t = tasks.get(task_id)
        if t and t.get("status") == "cancelled":
            raise _CancelExc()


# ── 启动 ───────────────────────────────────────────────────
if __name__ == "__main__":
    if IS_CLOUD:
        print(f"""
╔══════════════════════════════════════════════╗
║       视频音频提取器 · 云端服务已启动         ║
╠══════════════════════════════════════════════╣
║  端口: {PORT}                                  ║
║  ffmpeg: {'OK' if FFMPEG_PATH else 'NOT FOUND'}                                  ║
╚══════════════════════════════════════════════╝
""")
    else:
        import socket
        def get_ip():
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
                s.close()
                return ip
            except Exception:
                return "127.0.0.1"
        local_ip = get_ip()
        print(f"""
╔══════════════════════════════════════════════╗
║         视频音频提取器 · 手机网页版           ║
╠══════════════════════════════════════════════╣
║                                              ║
║  电脑访问: http://localhost:{PORT}             ║
║  手机访问: http://{local_ip:<20s}:{PORT} ║
║                                              ║
║  确保手机和电脑连接同一个 WiFi               ║
║                                              ║
╚══════════════════════════════════════════════╝
""")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
