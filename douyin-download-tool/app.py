#!/usr/bin/env python3
"""
Local Douyin video downloader UI.

Use only for videos you own or have permission to save. The downloader shells out
to yt-dlp and keeps all output inside this sandbox directory by default.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from email.parser import BytesParser
from email.policy import default
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
DOWNLOADS = Path.home() / "Downloads"
MERGE_UPLOADS = ROOT / "merge_uploads"
YTDLP = shutil.which("yt-dlp")
FFMPEG = shutil.which("ffmpeg")
JOBS: dict[str, dict] = {}
MERGE_JOBS: dict[str, dict] = {}
MEDIA_JOBS: dict[str, dict] = {}
COOKIE_RE = re.compile(r"^(none|safari|firefox|chrome(?::[A-Za-z0-9_. -]+)?)$")


def safe_text(value: object, limit: int = 4000) -> str:
    text = "" if value is None else str(value)
    return text[:limit]


def append_cookie_args(cmd: list[str], source: str) -> None:
    if COOKIE_RE.fullmatch(source) and source != "none":
        cmd += ["--cookies-from-browser", source]


def chrome_profiles() -> list[dict[str, str]]:
    chrome_root = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    profiles: list[dict[str, str]] = []
    for path in sorted(chrome_root.glob("*")):
        if not path.is_dir() or not ((path.name == "Default") or path.name.startswith("Profile ")):
            continue
        label = path.name
        prefs = path / "Preferences"
        try:
            data = json.loads(prefs.read_text(encoding="utf-8"))
            profile_name = data.get("profile", {}).get("name")
            if profile_name and profile_name != path.name:
                label = f"{profile_name} ({path.name})"
        except Exception:
            pass
        profiles.append({"value": f"chrome:{path.name}", "label": f"Chrome - {label}"})
    return profiles


def build_command(url: str, kind: str = "video", cookies: str = "none") -> list[str]:
    output = str(DOWNLOADS / "%(extractor)s-%(id)s-%(title).120B.%(ext)s")
    cmd = [
        YTDLP or "yt-dlp",
        "--no-playlist",
        "--force-ipv4",
        "--socket-timeout",
        "25",
        "--retries",
        "3",
        "--fragment-retries",
        "3",
        "--restrict-filenames",
        "--windows-filenames",
        "--newline",
        "-o",
        output,
    ]
    append_cookie_args(cmd, cookies)

    if kind == "audio":
        cmd += ["-x", "--audio-format", "mp3"]
    else:
        cmd += ["-f", "bv*+ba/best"]

    cmd.append(url)
    return cmd


def run_job(job_id: str, url: str, kind: str, cookies: str) -> None:
    DOWNLOADS.mkdir(exist_ok=True)
    started = time.time()
    JOBS[job_id].update(status="running", log="", files=[])

    cmd = build_command(url, kind, cookies)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        lines: list[str] = []
        before = {p.name for p in DOWNLOADS.glob("*")}
        for line in proc.stdout:
            lines.append(line.rstrip())
            JOBS[job_id]["log"] = "\n".join(lines[-200:])
        code = proc.wait()
        after_paths = sorted(
            [p for p in DOWNLOADS.glob("*") if p.name not in before and p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        JOBS[job_id].update(
            status="done" if code == 0 else "failed",
            returncode=code,
            duration=round(time.time() - started, 1),
            error=None if code == 0 else user_facing_error("\n".join(lines[-40:])),
            files=[file_payload(p) for p in after_paths[:8]],
        )
    except Exception as exc:
        JOBS[job_id].update(status="failed", error=safe_text(exc))


def file_payload(path: Path) -> dict:
    return {
        "name": path.name,
        "size": path.stat().st_size,
        "url": f"/file?name={quote(path.name)}",
    }


def clean_filename(name: str) -> str:
    stem = Path(name).name
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", stem)
    return stem[:160] or "upload.bin"


def parse_multipart(content_type: str, body: bytes) -> dict[str, tuple[str, bytes]]:
    raw = (
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
        + body
    )
    message = BytesParser(policy=default).parsebytes(raw)
    files: dict[str, tuple[str, bytes]] = {}
    if not message.is_multipart():
        return files
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        if not name or not filename:
            continue
        payload = part.get_payload(decode=True) or b""
        files[name] = (clean_filename(filename), payload)
    return files


def merge_media(video_file: tuple[str, bytes], audio_file: tuple[str, bytes]) -> dict:
    if not FFMPEG:
        raise RuntimeError("没有找到 ffmpeg，无法合并音视频")
    DOWNLOADS.mkdir(exist_ok=True)
    MERGE_UPLOADS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    work = MERGE_UPLOADS / f"merge-{stamp}-{uuid.uuid4().hex[:8]}"
    work.mkdir(exist_ok=True)

    video_path = work / video_file[0]
    audio_path = work / audio_file[0]
    output = DOWNLOADS / f"merged-{stamp}.mp4"
    video_path.write_bytes(video_file[1])
    audio_path.write_bytes(audio_file[1])

    cmd = [
        FFMPEG,
        "-y",
        "-i",
        str(video_path),
        "-i",
        str(audio_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-shortest",
        str(output),
    ]
    result = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout or "ffmpeg 合并失败")
    return file_payload(output)


def download_url_to_file(url: str, path: Path) -> None:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
            ),
            "Referer": "https://www.douyin.com/",
        },
    )
    with urlopen(request, timeout=60) as response:
        with path.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)


def merge_media_urls(video_url: str, audio_url: str, title: str = "merged") -> dict:
    if not FFMPEG:
        raise RuntimeError("没有找到 ffmpeg，无法合并音视频")
    DOWNLOADS.mkdir(exist_ok=True)
    MERGE_UPLOADS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe_title = re.sub(r"[^A-Za-z0-9._ -]+", "_", title)[:80] or "merged"
    work = MERGE_UPLOADS / f"merge-url-{stamp}-{uuid.uuid4().hex[:8]}"
    work.mkdir(exist_ok=True)

    video_path = work / "video.mp4"
    audio_path = work / "audio.mp4"
    output = DOWNLOADS / f"{safe_title}-{stamp}.mp4"
    download_url_to_file(video_url, video_path)
    download_url_to_file(audio_url, audio_path)

    cmd = [
        FFMPEG,
        "-y",
        "-i",
        str(video_path),
        "-i",
        str(audio_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-shortest",
        str(output),
    ]
    result = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, timeout=240)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout or "ffmpeg 合并失败")
    return file_payload(output)


def download_media_url(url: str, title: str = "download.mp4") -> dict:
    DOWNLOADS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe_title = clean_filename(title or "download.mp4")
    suffix = Path(safe_title).suffix or ".mp4"
    output = DOWNLOADS / f"{Path(safe_title).stem}-{stamp}{suffix}"
    download_url_to_file(url, output)
    return file_payload(output)


def run_merge_url_job(job_id: str, video_url: str, audio_url: str, title: str) -> None:
    MERGE_JOBS[job_id].update(status="running")
    try:
        merged = merge_media_urls(video_url, audio_url, title)
        MERGE_JOBS[job_id].update(status="done", file=merged, message=f"合并完成：{merged['name']}")
    except Exception as exc:
        MERGE_JOBS[job_id].update(status="failed", error=safe_text(exc))


def run_media_url_job(job_id: str, media_url: str, title: str) -> None:
    MEDIA_JOBS[job_id].update(status="running")
    try:
        media = download_media_url(media_url, title)
        MEDIA_JOBS[job_id].update(status="done", file=media, message=f"下载完成：{media['name']}")
    except Exception as exc:
        MEDIA_JOBS[job_id].update(status="failed", error=safe_text(exc))


def latest_files() -> list[dict]:
    DOWNLOADS.mkdir(exist_ok=True)
    paths = sorted(
        [p for p in DOWNLOADS.glob("*") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return [file_payload(p) for p in paths[:20]]


def extract_urls(text: str) -> list[str]:
    return [normalize_url(url) for url in re.findall(r"https?://[^\s，,。]+", text)]


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc.endswith("douyin.com") and re.fullmatch(r"/video/\d+/?", parsed.path):
        return parsed._replace(query="", fragment="").geturl()
    return url


def user_facing_error(error: object) -> str:
    text = safe_text(error)
    if "timed out" in text.lower() or "timeout" in text.lower():
        return (
            "当前链接诊断超时。通常是抖音网页请求卡住、Cookie 读取被占用，或该视频网页端限制较严。"
            "先完全退出 Chrome，再重新打开工具试一次。"
        )
    if "fresh cookies" in text.lower() or "cookies are needed" in text.lower():
        return (
            "这条视频在浏览器里能播放，但抖音没有接受 yt-dlp 这条解析路线的登录态。"
            "这通常不是你操作错，而是网页端接口/反爬限制导致。继续反复诊断意义不大；"
            "建议改用浏览器播放后的学习流程，例如录屏、手动保存片段，或把链接交给转写/笔记流程。"
        )
    if "could not copy chrome cookie database" in text.lower() or "permission" in text.lower():
        return "读取浏览器 Cookie 失败。可以先退出 Chrome 再试，或换 Safari/Firefox 对应登录态。"
    return text


def merge_status_html(job_id: str) -> str:
    safe_job_id = escape(job_id, quote=True)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>合并下载进度</title>
  <style>
    :root {{
      --ink: #1f1c16;
      --paper: #f7f2e7;
      --line: #2d281f;
      --green: #2f8f46;
      --red: #b84a3c;
      --yellow: #f4c84f;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      color: var(--ink);
      background:
        linear-gradient(90deg, rgba(45,40,31,.05) 1px, transparent 1px) 0 0 / 32px 32px,
        linear-gradient(0deg, rgba(45,40,31,.04) 1px, transparent 1px) 0 0 / 32px 32px,
        var(--paper);
      font-family: "Avenir Next", "PingFang SC", "Noto Sans CJK SC", sans-serif;
    }}
    main {{
      width: min(720px, calc(100vw - 32px));
      border: 3px solid var(--line);
      border-radius: 8px;
      background: #fffaf0;
      box-shadow: 8px 8px 0 var(--line);
      padding: 28px;
    }}
    h1 {{ margin: 0 0 10px; font-size: 34px; letter-spacing: 0; }}
    p {{ margin: 10px 0; color: #625845; font-size: 17px; line-height: 1.55; }}
    .bar {{
      height: 18px;
      margin: 22px 0;
      border: 2px solid var(--line);
      border-radius: 999px;
      overflow: hidden;
      background: #ebe4d2;
    }}
    .fill {{
      width: 42%;
      height: 100%;
      background: repeating-linear-gradient(45deg, var(--yellow), var(--yellow) 12px, #ffe58b 12px, #ffe58b 24px);
      animation: pulse 1.1s linear infinite;
    }}
    @keyframes pulse {{ from {{ transform: translateX(-40%); }} to {{ transform: translateX(180%); }} }}
    .done .fill {{
      width: 100%;
      transform: none;
      animation: none;
      background: var(--green);
    }}
    .failed .fill {{
      width: 100%;
      transform: none;
      animation: none;
      background: var(--red);
    }}
    a, button {{
      display: inline-flex;
      min-height: 44px;
      align-items: center;
      justify-content: center;
      border: 2px solid var(--line);
      border-radius: 6px;
      padding: 0 18px;
      background: var(--green);
      color: white;
      font: inherit;
      font-weight: 800;
      text-decoration: none;
      cursor: pointer;
      box-shadow: 3px 3px 0 var(--line);
    }}
    code {{ word-break: break-all; }}
    .error {{ color: var(--red); white-space: pre-wrap; }}
  </style>
</head>
<body>
  <main id="card">
    <h1 id="title">正在合并音频和视频</h1>
    <p id="message">这个页面会一直检查后台任务。合并完成后，会自动交给 Chrome 下载到你的“下载”文件夹。</p>
    <div class="bar"><div class="fill"></div></div>
    <p id="detail">任务 ID：<code>{safe_job_id}</code></p>
    <p id="action"></p>
  </main>
  <script>
    const jobId = {json.dumps(job_id)};
    const card = document.getElementById("card");
    const title = document.getElementById("title");
    const message = document.getElementById("message");
    const detail = document.getElementById("detail");
    const action = document.getElementById("action");
    let downloaded = false;

    function bytesText(bytes) {{
      if (!bytes) return "";
      return (bytes / 1024 / 1024).toFixed(2) + " MB";
    }}

    function triggerDownload(file) {{
      if (downloaded || !file || !file.url) return;
      downloaded = true;
      const url = file.url.startsWith("http") ? file.url : location.origin + file.url;
      const a = document.createElement("a");
      a.href = url;
      a.download = file.name || "";
      document.body.appendChild(a);
      a.click();
      a.remove();
      action.innerHTML = `<a href="${{url}}">再次下载</a>`;
    }}

    async function poll() {{
      try {{
        const response = await fetch("/api/merge-job?id=" + encodeURIComponent(jobId), {{ cache: "no-store" }});
        const data = await response.json();
        if (data.status === "done") {{
          card.className = "done";
          title.textContent = "合并完成，正在启动 Chrome 下载";
          message.textContent = "如果浏览器没有自动开始下载，可以点下面的“再次下载”。";
          const file = data.file || {{}};
          detail.textContent = file.name ? `${{file.name}} · ${{bytesText(file.size)}}` : "合并完成";
          triggerDownload(file);
          return;
        }}
        if (data.status === "failed") {{
          card.className = "failed";
          title.textContent = "合并失败";
          message.textContent = "这通常是临时链接失效，回到抖音页面重新播放一下，再重新点 Merge A+V。";
          detail.className = "error";
          detail.textContent = data.error || "未知错误";
          return;
        }}
        title.textContent = data.status === "queued" ? "等待开始合并" : "正在合并音频和视频";
        message.textContent = "页面可以放在一边；完成后会自动触发 Chrome 下载。";
        detail.textContent = "状态：" + (data.status || "checking");
        setTimeout(poll, 1500);
      }} catch (err) {{
        message.textContent = "暂时没有连上本地服务，稍后自动重试。";
        detail.textContent = err.message;
        setTimeout(poll, 2500);
      }}
    }}
    poll();
  </script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        return

    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def send_text(self, text: str, content_type: str = "text/html; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8") or "{}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_text(INDEX_HTML)
            return
        if parsed.path == "/api/status":
            self.send_json(
                {
                    "ytDlp": bool(YTDLP),
                    "ytDlpPath": YTDLP,
                    "ffmpeg": bool(FFMPEG),
                    "ffmpegPath": FFMPEG,
                    "files": latest_files(),
                    "chromeProfiles": chrome_profiles(),
                }
            )
            return
        if parsed.path == "/api/job":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            self.send_json(JOBS.get(job_id, {"status": "missing"}))
            return
        if parsed.path == "/api/merge-job":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            self.send_json(MERGE_JOBS.get(job_id, {"status": "missing"}))
            return
        if parsed.path == "/api/media-job":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            self.send_json(MEDIA_JOBS.get(job_id, {"status": "missing"}))
            return
        if parsed.path == "/merge-status":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            self.send_text(merge_status_html(job_id))
            return
        if parsed.path == "/file":
            name = parse_qs(parsed.query).get("name", [""])[0]
            path = (DOWNLOADS / name).resolve()
            if not str(path).startswith(str(DOWNLOADS.resolve())) or not path.exists():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if self.path == "/api/info":
            payload = self.read_json()
            url = extract_urls(safe_text(payload.get("url"), 2000))
            cookies = safe_text(payload.get("cookies"), 80)
            if not url:
                self.send_json({"ok": False, "error": "没有识别到有效链接"}, HTTPStatus.BAD_REQUEST)
                return
            cmd = [
                YTDLP or "yt-dlp",
                "--dump-single-json",
                "--no-playlist",
                "--skip-download",
                "--force-ipv4",
                "--socket-timeout",
                "25",
                "--retries",
                "2",
            ]
            append_cookie_args(cmd, cookies)
            cmd.append(url[0])
            try:
                result = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, timeout=120)
                if result.returncode != 0:
                    self.send_json(
                        {"ok": False, "error": user_facing_error(result.stderr or result.stdout)},
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                info = json.loads(result.stdout)
                self.send_json(
                    {
                        "ok": True,
                        "title": info.get("title"),
                        "duration": info.get("duration"),
                        "thumbnail": info.get("thumbnail"),
                        "extractor": info.get("extractor"),
                        "uploader": info.get("uploader"),
                    }
                )
            except Exception as exc:
                self.send_json({"ok": False, "error": user_facing_error(exc)}, HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/api/cookie-check":
            payload = self.read_json()
            cookies = safe_text(payload.get("cookies"), 80)
            urls = extract_urls(safe_text(payload.get("url"), 2000))
            if not COOKIE_RE.fullmatch(cookies) or cookies == "none":
                self.send_json({"ok": False, "error": "请先选择一个具体的浏览器登录态来源"}, HTTPStatus.BAD_REQUEST)
                return
            if not urls:
                self.send_json({"ok": False, "error": "请先粘贴要诊断的抖音视频链接"}, HTTPStatus.BAD_REQUEST)
                return
            cmd = [
                YTDLP or "yt-dlp",
                "--dump-single-json",
                "--no-playlist",
                "--skip-download",
                "--force-ipv4",
                "--socket-timeout",
                "15",
                "--retries",
                "1",
            ]
            append_cookie_args(cmd, cookies)
            cmd.append(urls[0])
            try:
                result = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, timeout=25)
                combined = result.stderr or result.stdout
                if result.returncode != 0:
                    self.send_json({"ok": False, "error": user_facing_error(combined)}, HTTPStatus.BAD_REQUEST)
                    return
                info = json.loads(result.stdout)
                title = info.get("title") or "未命名视频"
                self.send_json({"ok": True, "message": f"登录态对当前链接有效：{title}"})
            except Exception as exc:
                self.send_json({"ok": False, "error": user_facing_error(exc)}, HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/api/download":
            payload = self.read_json()
            urls = extract_urls(safe_text(payload.get("url"), 2000))
            kind = "audio" if payload.get("kind") == "audio" else "video"
            cookies = safe_text(payload.get("cookies"), 80)
            if not urls:
                self.send_json({"ok": False, "error": "没有识别到有效链接"}, HTTPStatus.BAD_REQUEST)
                return
            if not YTDLP:
                self.send_json({"ok": False, "error": "没有找到 yt-dlp"}, HTTPStatus.BAD_REQUEST)
                return
            job_id = uuid.uuid4().hex
            JOBS[job_id] = {"id": job_id, "status": "queued", "url": urls[0], "kind": kind}
            thread = threading.Thread(target=run_job, args=(job_id, urls[0], kind, cookies), daemon=True)
            thread.start()
            self.send_json({"ok": True, "jobId": job_id})
            return

        if self.path == "/api/merge":
            try:
                content_type = self.headers.get("Content-Type", "")
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                files = parse_multipart(content_type, body)
                if "video" not in files or "audio" not in files:
                    self.send_json({"ok": False, "error": "请同时选择视频文件和音频文件"}, HTTPStatus.BAD_REQUEST)
                    return
                merged = merge_media(files["video"], files["audio"])
                self.send_json({"ok": True, "file": merged})
            except Exception as exc:
                self.send_json({"ok": False, "error": safe_text(exc)}, HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/api/merge-urls":
            try:
                payload = self.read_json()
                video_url = safe_text(payload.get("videoUrl"), 8000)
                audio_url = safe_text(payload.get("audioUrl"), 8000)
                title = safe_text(payload.get("title"), 160)
                if not video_url or not audio_url:
                    self.send_json({"ok": False, "error": "缺少视频流或音频流 URL"}, HTTPStatus.BAD_REQUEST)
                    return
                merged = merge_media_urls(video_url, audio_url, title)
                self.send_json({"ok": True, "file": merged, "message": f"合并完成：{merged['name']}"})
            except Exception as exc:
                self.send_json({"ok": False, "error": safe_text(exc)}, HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/api/merge-urls-start":
            try:
                payload = self.read_json()
                video_url = safe_text(payload.get("videoUrl"), 8000)
                audio_url = safe_text(payload.get("audioUrl"), 8000)
                title = safe_text(payload.get("title"), 160)
                if not video_url or not audio_url:
                    self.send_json({"ok": False, "error": "缺少视频流或音频流 URL"}, HTTPStatus.BAD_REQUEST)
                    return
                job_id = uuid.uuid4().hex
                MERGE_JOBS[job_id] = {"id": job_id, "status": "queued", "title": title}
                thread = threading.Thread(
                    target=run_merge_url_job,
                    args=(job_id, video_url, audio_url, title),
                    daemon=True,
                )
                thread.start()
                self.send_json(
                    {
                        "ok": True,
                        "jobId": job_id,
                        "statusUrl": f"/merge-status?id={job_id}",
                        "apiStatusUrl": f"/api/merge-job?id={job_id}",
                    }
                )
            except Exception as exc:
                self.send_json({"ok": False, "error": safe_text(exc)}, HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/api/media-url-start":
            try:
                payload = self.read_json()
                media_url = safe_text(payload.get("mediaUrl"), 8000)
                title = safe_text(payload.get("title"), 180)
                if not media_url:
                    self.send_json({"ok": False, "error": "缺少媒体流 URL"}, HTTPStatus.BAD_REQUEST)
                    return
                job_id = uuid.uuid4().hex
                MEDIA_JOBS[job_id] = {"id": job_id, "status": "queued", "title": title}
                thread = threading.Thread(
                    target=run_media_url_job,
                    args=(job_id, media_url, title),
                    daemon=True,
                )
                thread.start()
                self.send_json({"ok": True, "jobId": job_id, "apiStatusUrl": f"/api/media-job?id={job_id}"})
            except Exception as exc:
                self.send_json({"ok": False, "error": safe_text(exc)}, HTTPStatus.BAD_REQUEST)
            return

        self.send_error(HTTPStatus.NOT_FOUND)


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>授权视频备份器</title>
  <style>
    :root {
      --ink: #171612;
      --paper: #f7f4ec;
      --line: #29251c;
      --green: #4f8a56;
      --blue: #315f9b;
      --red: #c6533f;
      --yellow: #f4c84f;
      --soft: #ebe4d2;
      --panel: #fffaf0;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--ink);
      background:
        linear-gradient(90deg, rgba(23,22,18,.05) 1px, transparent 1px) 0 0 / 32px 32px,
        linear-gradient(0deg, rgba(23,22,18,.04) 1px, transparent 1px) 0 0 / 32px 32px,
        var(--paper);
      font-family: "Avenir Next", "PingFang SC", "Noto Sans CJK SC", sans-serif;
    }
    .shell { width: min(1120px, calc(100vw - 32px)); margin: 0 auto; padding: 28px 0 44px; }
    header {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 20px;
      align-items: end;
      border-bottom: 3px solid var(--line);
      padding-bottom: 18px;
    }
    .brand { display: flex; gap: 16px; align-items: center; }
    .mark {
      width: 62px; height: 62px; border: 3px solid var(--line); border-radius: 8px;
      background: conic-gradient(from 20deg, var(--yellow), #f6efe0, var(--green), #f6efe0, var(--blue), var(--yellow));
      box-shadow: 5px 5px 0 var(--line);
    }
    h1 { margin: 0; font-size: clamp(30px, 5vw, 58px); line-height: .95; letter-spacing: 0; }
    .subtitle { margin: 8px 0 0; font-size: 15px; color: #5a5344; max-width: 720px; }
    .status-pill {
      border: 2px solid var(--line); border-radius: 999px; padding: 9px 14px;
      background: var(--panel); font-size: 14px; box-shadow: 3px 3px 0 var(--line);
      white-space: nowrap;
    }
    main { display: grid; grid-template-columns: minmax(0, 1.1fr) 360px; gap: 22px; margin-top: 26px; }
    .board, .side {
      border: 3px solid var(--line);
      background: var(--panel);
      box-shadow: 8px 8px 0 var(--line);
      border-radius: 6px;
    }
    .board { padding: 22px; }
    .side { padding: 18px; align-self: start; }
    label { display: block; font-weight: 800; margin-bottom: 10px; }
    textarea {
      width: 100%; min-height: 168px; resize: vertical; border: 2px solid var(--line);
      border-radius: 4px; background: #fff; padding: 16px; font: inherit; font-size: 18px; line-height: 1.45;
      outline: none;
    }
    textarea:focus { box-shadow: 0 0 0 4px rgba(244,200,79,.45); }
    .controls { display: grid; grid-template-columns: 1fr auto auto; gap: 12px; align-items: center; margin-top: 16px; }
    .option-row { display: grid; grid-template-columns: 180px 1fr auto; gap: 12px; align-items: center; margin-top: 12px; }
    .segmented { display: inline-grid; grid-template-columns: 1fr 1fr; border: 2px solid var(--line); border-radius: 4px; overflow: hidden; background: var(--soft); }
    .segmented button { border: 0; padding: 12px 16px; background: transparent; font: inherit; font-weight: 800; cursor: pointer; }
    .segmented button.active { background: var(--yellow); }
    select {
      width: 100%; min-height: 44px; border: 2px solid var(--line); border-radius: 4px;
      background: #fff; padding: 0 12px; font: inherit; font-weight: 800;
    }
    input[type="file"] {
      width: 100%; min-height: 44px; border: 2px solid var(--line); border-radius: 4px;
      background: #fff; padding: 9px 12px; font: inherit;
    }
    .btn {
      border: 2px solid var(--line); border-radius: 4px; padding: 12px 18px; font: inherit; font-weight: 900;
      cursor: pointer; background: #fff; color: var(--ink); box-shadow: 3px 3px 0 var(--line);
      min-height: 48px;
    }
    .btn.primary { background: var(--green); color: white; }
    .btn.blue { background: var(--blue); color: white; }
    .btn:disabled { opacity: .55; cursor: not-allowed; transform: none; }
    .btn:not(:disabled):active { transform: translate(2px, 2px); box-shadow: 1px 1px 0 var(--line); }
    .preview {
      margin-top: 18px; border: 2px dashed var(--line); border-radius: 4px; padding: 16px;
      background: #fdf8e9; min-height: 96px;
    }
    .preview h2 { margin: 0 0 8px; font-size: 21px; }
    .meta { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
    .tag { border: 1px solid var(--line); border-radius: 999px; padding: 4px 9px; background: #fff; font-size: 13px; }
    .log {
      margin-top: 18px; min-height: 190px; max-height: 320px; overflow: auto; white-space: pre-wrap;
      border: 2px solid var(--line); border-radius: 4px; background: #171612; color: #f6efe0;
      padding: 14px; font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }
    .side h2 { margin: 0 0 14px; font-size: 22px; }
    .file-list { display: grid; gap: 10px; }
    .file {
      border: 2px solid var(--line); border-radius: 4px; padding: 12px; background: #fff;
      display: grid; gap: 7px;
    }
    .file a { color: var(--blue); font-weight: 900; text-decoration: none; word-break: break-all; }
    .small { color: #675e4d; font-size: 13px; }
    .notice { margin-top: 16px; padding: 12px; border-left: 6px solid var(--red); background: #fff; font-size: 14px; line-height: 1.55; }
    .merge-panel {
      margin-top: 18px;
      padding-top: 18px;
      border-top: 3px solid var(--line);
      display: grid;
      gap: 12px;
    }
    .merge-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    @media (max-width: 860px) {
      header, main, .controls { grid-template-columns: 1fr; }
      .option-row { grid-template-columns: 1fr; }
      .merge-grid { grid-template-columns: 1fr; }
      .status-pill { justify-self: start; white-space: normal; }
      .controls .btn { width: 100%; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <header>
      <div class="brand">
        <div class="mark" aria-hidden="true"></div>
        <div>
          <h1>授权视频备份器</h1>
          <p class="subtitle">粘贴抖音分享口令或链接，保存你自己发布、已获授权、或允许离线备份的视频。</p>
        </div>
      </div>
      <div class="status-pill" id="runtime">检查 yt-dlp...</div>
    </header>

    <main>
      <section class="board">
        <label for="url">视频分享内容</label>
        <textarea id="url" placeholder="把抖音复制出来的一整段分享文案贴到这里，工具会自动识别其中的链接。"></textarea>
        <div class="controls">
          <div class="segmented" aria-label="下载类型">
            <button id="videoMode" class="active" type="button">视频</button>
            <button id="audioMode" type="button">音频</button>
          </div>
          <button class="btn" id="infoBtn" type="button">解析</button>
          <button class="btn primary" id="downloadBtn" type="button">下载</button>
        </div>
        <div class="option-row">
          <label for="cookies" style="margin:0">登录态来源</label>
          <select id="cookies">
            <option value="none">不使用浏览器 Cookie</option>
            <option value="chrome">Chrome 默认 Profile（旧方式）</option>
            <option value="safari">Safari（我已确认有权保存）</option>
            <option value="firefox">Firefox（我已确认有权保存）</option>
          </select>
          <button class="btn" id="cookieBtn" type="button">诊断当前链接</button>
        </div>
        <div class="preview" id="preview">
          <h2>等待链接</h2>
          <div class="small">解析结果会显示在这里；解析只是预览，失败时也可以直接点下载。</div>
        </div>
        <div class="log" id="log">准备就绪。</div>
        <div class="merge-panel">
          <h2 style="margin:0">本地合并</h2>
          <div class="small">把插件下载出来的“视频文件”和“音频文件”放进来，合并成一个完整 MP4。</div>
          <div class="merge-grid">
            <div>
              <label for="mergeVideo">视频文件</label>
              <input id="mergeVideo" type="file" accept="video/*,.mp4,.m4v,.webm" />
            </div>
            <div>
              <label for="mergeAudio">音频文件</label>
              <input id="mergeAudio" type="file" accept="audio/*,.mp4,.m4a,.aac,.webm" />
            </div>
          </div>
          <button class="btn blue" id="mergeBtn" type="button">合并为 MP4</button>
        </div>
      </section>

      <aside class="side">
        <h2>最近文件</h2>
        <div class="file-list" id="files"></div>
        <div class="notice">
          如果浏览器能播放但本工具仍失败，通常说明抖音拒绝 yt-dlp 解析路线，不是你没登录。此时别反复诊断，改用浏览器播放后的学习流程。
        </div>
      </aside>
    </main>
  </div>

  <script>
    const $ = (id) => document.getElementById(id);
    let kind = "video";
    let timer = null;

    function fmtSize(bytes) {
      if (!bytes) return "0 B";
      const units = ["B", "KB", "MB", "GB"];
      let n = bytes, i = 0;
      while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
      return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
    }

    function setMode(next) {
      kind = next;
      $("videoMode").classList.toggle("active", kind === "video");
      $("audioMode").classList.toggle("active", kind === "audio");
    }

    async function api(path, body) {
      const res = await fetch(path, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body)
      });
      const data = await res.json();
      if (!res.ok || data.ok === false) throw new Error(data.error || "请求失败");
      return data;
    }

    function renderFiles(files) {
      $("files").innerHTML = files.length ? files.map(file => `
        <div class="file">
          <a href="${file.url}">${file.name}</a>
          <div class="small">${fmtSize(file.size)}</div>
        </div>
      `).join("") : `<div class="small">还没有下载文件。</div>`;
    }

    async function refreshStatus() {
      const res = await fetch("/api/status");
      const data = await res.json();
      $("runtime").textContent = data.ffmpeg ? `ffmpeg 已就绪` : "未找到 ffmpeg";
      if (data.chromeProfiles && data.chromeProfiles.length) {
        const existing = new Set([...$("cookies").options].map(option => option.value));
        data.chromeProfiles.forEach(profile => {
          if (!existing.has(profile.value)) {
            const option = document.createElement("option");
            option.value = profile.value;
            option.textContent = `${profile.label}（我已确认有权保存）`;
            $("cookies").appendChild(option);
          }
        });
      }
      renderFiles(data.files || []);
    }

    async function checkCookies() {
      $("log").textContent = "正在诊断当前链接的登录态，最多等待 25 秒...";
      try {
        const data = await api("/api/cookie-check", {url: $("url").value, cookies: $("cookies").value});
        $("log").textContent = data.message || "登录态对当前链接有效。";
      } catch (err) {
        $("log").textContent = String(err.message);
      }
    }

    async function parseInfo() {
      $("preview").innerHTML = `<h2>解析中...</h2><div class="small">正在向 yt-dlp 请求视频信息。</div>`;
      $("log").textContent = "解析中...";
      try {
        const data = await api("/api/info", {url: $("url").value, cookies: $("cookies").value});
        const duration = data.duration ? `${Math.floor(data.duration / 60)}:${String(data.duration % 60).padStart(2, "0")}` : "未知时长";
        $("preview").innerHTML = `
          <h2>${data.title || "未命名视频"}</h2>
          <div class="meta">
            <span class="tag">${data.extractor || "未知平台"}</span>
            <span class="tag">${duration}</span>
            <span class="tag">${data.uploader || "未知作者"}</span>
          </div>
        `;
        $("log").textContent = "解析完成。";
      } catch (err) {
        $("preview").innerHTML = `<h2>解析失败</h2><div class="small">${String(err.message).slice(0, 600)}<br><br>可以跳过解析，直接点「下载」试一次。</div>`;
        $("log").textContent = String(err.message);
      }
    }

    async function startDownload() {
      $("downloadBtn").disabled = true;
      $("infoBtn").disabled = true;
      $("log").textContent = "创建下载任务...";
      try {
        const data = await api("/api/download", {url: $("url").value, kind, cookies: $("cookies").value});
        pollJob(data.jobId);
      } catch (err) {
        $("log").textContent = String(err.message);
        $("downloadBtn").disabled = false;
        $("infoBtn").disabled = false;
      }
    }

    async function mergeLocalFiles() {
      const video = $("mergeVideo").files[0];
      const audio = $("mergeAudio").files[0];
      if (!video || !audio) {
        $("log").textContent = "请同时选择视频文件和音频文件。";
        return;
      }
      $("mergeBtn").disabled = true;
      $("log").textContent = "正在合并，本地处理可能需要几十秒...";
      try {
        const form = new FormData();
        form.append("video", video);
        form.append("audio", audio);
        const res = await fetch("/api/merge", {method: "POST", body: form});
        const data = await res.json();
        if (!res.ok || data.ok === false) throw new Error(data.error || "合并失败");
        $("log").textContent = `合并完成：${data.file.name}`;
        renderFiles([data.file]);
        await refreshStatus();
      } catch (err) {
        $("log").textContent = String(err.message);
      } finally {
        $("mergeBtn").disabled = false;
      }
    }

    async function pollJob(jobId) {
      clearInterval(timer);
      timer = setInterval(async () => {
        const res = await fetch(`/api/job?id=${jobId}`);
        const job = await res.json();
        $("log").textContent = job.error || job.log || job.status;
        if (job.status === "done" || job.status === "failed") {
          clearInterval(timer);
          $("downloadBtn").disabled = false;
          $("infoBtn").disabled = false;
          await refreshStatus();
          if (job.files && job.files.length) renderFiles(job.files);
        }
      }, 900);
    }

    $("videoMode").onclick = () => setMode("video");
    $("audioMode").onclick = () => setMode("audio");
    $("infoBtn").onclick = parseInfo;
    $("downloadBtn").onclick = startDownload;
    $("cookieBtn").onclick = checkCookies;
    $("mergeBtn").onclick = mergeLocalFiles;
    refreshStatus();
  </script>
</body>
</html>
"""


def main() -> None:
    port = int(os.environ.get("PORT", "8765"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Open http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
