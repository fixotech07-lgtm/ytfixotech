"""
FIXOTOOLS — Media Downloader · LITE Edition
Super fast · Direct download · Artist playlist builder
"""
from __future__ import annotations

import json, os, re, shutil, sys
import threading, time, uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, Response, jsonify, request, send_file
import yt_dlp

BASE_DIR     = Path(__file__).resolve().parent
DATA_DIR     = BASE_DIR / "data"
DOWNLOAD_DIR = BASE_DIR / "downloads"
JOBS_DIR     = DOWNLOAD_DIR / "jobs"
HISTORY_FILE = DATA_DIR / "history.json"

for d in (DATA_DIR, DOWNLOAD_DIR, JOBS_DIR):
    d.mkdir(parents=True, exist_ok=True)

MAX_BATCH      = 250
PARALLEL_JOBS  = 12
SEARCH_RESULTS = 15
ARTIST_LIMIT   = 50

app          = Flask(__name__)
_LOCK        = threading.RLock()
JOBS:  Dict[str, Dict[str, Any]] = {}
QUEUE: List[str] = []
_WORKER_ON   = False
CANCELLED:    set[str] = set()
GROUPS: Dict[str, Dict[str, Any]] = {}
SEARCH_CACHE: Dict[str, tuple[float, List[Dict]]] = {}
ARTIST_CACHE: Dict[str, tuple[float, List[Dict]]] = {}
CACHE_TTL = 600


def now_ms() -> int: return int(time.time() * 1000)
def ts() -> str: return time.strftime("%Y-%m-%d %H:%M:%S")

def human_bytes(n: int) -> str:
    size = float(n or 0)
    for u in ("B","KB","MB","GB","TB"):
        if size < 1024: return f"{size:.1f} {u}"
        size /= 1024
    return f"{size:.1f} PB"

def human_duration(secs: int) -> str:
    if not secs: return ""
    secs = int(secs)
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    if h: return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

def sanitize(name: str, fallback: str = "download") -> str:
    name = re.sub(r'[\\/:*?"<>|]+', " ", (name or fallback).strip())
    return re.sub(r"\s+", " ", name).strip()[:150] or fallback

def is_url(text: str) -> bool:
    return bool(re.match(r"^https?://", (text or "").strip(), re.I))

def safe_str(val: Any, allowed: set, default: str) -> str:
    v = str(val or default).lower()
    return v if v in allowed else default

def safe_int(val: Any, default: int, lo: int, hi: int) -> int:
    try: n = int(val)
    except Exception: return default
    return max(lo, min(hi, n))

def ffmpeg_ok() -> bool: return shutil.which("ffmpeg") is not None

def load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists(): return json.loads(path.read_text("utf-8"))
    except Exception: pass
    return default

def save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def _read_history() -> List[Dict[str, Any]]:
    d = load_json(HISTORY_FILE, [])
    return d if isinstance(d, list) else []

def add_history(item: Dict[str, Any]) -> None:
    h = _read_history(); h.insert(0, item); save_json(HISTORY_FILE, h[:500])

def delete_history_item(hid: str) -> bool:
    h = _read_history()
    new = [x for x in h if x.get("id") != hid]
    if len(new) == len(h): return False
    save_json(HISTORY_FILE, new); return True


def yt_search(query: str, n: int = SEARCH_RESULTS) -> List[Dict[str, Any]]:
    key = f"{query.lower()}::{n}"
    cached = SEARCH_CACHE.get(key)
    if cached and (time.time() - cached[0] < CACHE_TTL):
        return cached[1]
    opts = {
        "quiet": True, "no_warnings": True,
        "extract_flat": True, "skip_download": True,
        "default_search": f"ytsearch{n}",
        "ignoreerrors": True, "no_color": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
        if not isinstance(info, dict): return []
        results = []
        for e in (info.get("entries") or []):
            if not e: continue
            vid = e.get("id") or ""
            url = e.get("url") or e.get("webpage_url")
            if not url and vid:
                url = f"https://www.youtube.com/watch?v={vid}"
            if not url: continue
            thumb = e.get("thumbnail") or (f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if vid else "")
            results.append({
                "id": vid, "url": url,
                "title": e.get("title", "Untitled"),
                "uploader": e.get("uploader") or e.get("channel") or "",
                "duration": human_duration(e.get("duration") or 0),
                "thumbnail": thumb,
                "views": e.get("view_count") or 0,
            })
        SEARCH_CACHE[key] = (time.time(), results)
        if len(SEARCH_CACHE) > 80:
            cutoff = time.time() - CACHE_TTL
            for k in [k for k, v in SEARCH_CACHE.items() if v[0] < cutoff]:
                SEARCH_CACHE.pop(k, None)
        return results
    except Exception as exc:
        print(f"Search error: {exc}")
        return []


def yt_artist_songs(artist: str, limit: int = ARTIST_LIMIT) -> List[Dict[str, Any]]:
    key = artist.lower()
    cached = ARTIST_CACHE.get(key)
    if cached and (time.time() - cached[0] < CACHE_TTL):
        return cached[1]
    query = f"{artist} songs"
    opts = {
        "quiet": True, "no_warnings": True,
        "extract_flat": True, "skip_download": True,
        "default_search": f"ytsearch{limit}",
        "ignoreerrors": True, "no_color": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        if not isinstance(info, dict): return []
        results = []
        artist_lower = artist.lower()
        for e in (info.get("entries") or []):
            if not e: continue
            vid = e.get("id") or ""
            url = e.get("url") or (f"https://www.youtube.com/watch?v={vid}" if vid else "")
            if not url: continue
            title = e.get("title", "")
            uploader = (e.get("uploader") or e.get("channel") or "").lower()
            if (artist_lower in title.lower() or artist_lower in uploader):
                thumb = e.get("thumbnail") or (f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if vid else "")
                results.append({
                    "id": vid, "url": url, "title": title,
                    "uploader": e.get("uploader") or e.get("channel") or "",
                    "duration": human_duration(e.get("duration") or 0),
                    "thumbnail": thumb,
                })
        ARTIST_CACHE[key] = (time.time(), results)
        return results
    except Exception as exc:
        print(f"Artist search error: {exc}")
        return []


def _job_update(jid: str, **kw) -> None:
    with _LOCK:
        if jid in JOBS:
            JOBS[jid].update(kw)
            JOBS[jid]["updated_at"] = now_ms()

def _job_get(jid: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        j = JOBS.get(jid)
        return dict(j) if j else None

def make_job(payload: Dict[str, Any], title: str = "", group_id: str = "",
             thumbnail: str = "") -> Dict[str, Any]:
    jid = uuid.uuid4().hex
    jpath = JOBS_DIR / jid
    jpath.mkdir(parents=True, exist_ok=True)
    job = {
        "id": jid, "title": title or "Download",
        "thumbnail": thumbnail, "status": "queued",
        "progress": 0, "message": "Ne radhe...",
        "created_at": now_ms(), "updated_at": now_ms(),
        "payload": payload, "path": str(jpath),
        "group_id": group_id, "filename": None,
        "download_url": None, "size_human": None,
        "speed_human": None,
    }
    with _LOCK:
        JOBS[jid] = job
        QUEUE.append(jid)
    return job


def _progress_hook(jid: str):
    last = {"t": 0.0}
    def hook(d: Dict[str, Any]) -> None:
        if jid in CANCELLED:
            raise yt_dlp.utils.DownloadError("Anulua.")
        status = d.get("status")
        now = time.time()
        if status == "downloading":
            if now - last["t"] < 0.5: return
            last["t"] = now
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done  = d.get("downloaded_bytes") or 0
            pct   = min(90, max(2, int(done * 100 / total))) if total else 5
            spd   = d.get("speed") or 0
            _job_update(jid, status="downloading", progress=pct,
                        message=f"Shkarkim {pct}%",
                        speed_human=f"{spd/1024/1024:.1f} MB/s" if spd else None)
        elif status == "finished":
            _job_update(jid, progress=93, message="Konvertim...")
    return hook


def _build_opts(jid: str, payload: Dict, folder: Path) -> Dict[str, Any]:
    mode = payload["mode"]
    tmpl = str(folder / "%(title).140s.%(ext)s")
    opts: Dict[str, Any] = {
        "outtmpl": tmpl, "quiet": True, "no_warnings": True,
        "ignoreerrors": True, "noplaylist": True,
        "windowsfilenames": True, "continuedl": True,
        "retries": 2, "fragment_retries": 2,
        "concurrent_fragment_downloads": 16,
        "http_chunk_size": 10485760,
        "progress_hooks": [_progress_hook(jid)],
        "no_color": True,
    }
    if mode == "audio":
        fmt = payload.get("audio_format", "mp3")
        pp: Dict[str, Any] = {"key": "FFmpegExtractAudio", "preferredcodec": fmt}
        if fmt in {"mp3", "wav"}:
            pp["preferredquality"] = payload.get("quality", "192")
        opts.update({"format": "bestaudio/best", "postprocessors": [pp]})
    else:
        vq = payload.get("video_quality", "720p")
        if vq == "best":
            sel = "bv*+ba/best"
        else:
            h = vq.replace("p", "")
            sel = f"bv*[height<={h}]+ba/b[height<={h}]/best[height<={h}]"
        opts.update({"format": sel, "merge_output_format": "mp4"})
    return opts


def run_download(jid: str) -> None:
    job = _job_get(jid)
    if not job: return
    payload = job["payload"]
    folder  = Path(job["path"])
    try:
        if jid in CANCELLED:
            _job_update(jid, status="cancelled", progress=0, message="U anulua.")
            return
        if not ffmpeg_ok():
            raise RuntimeError("FFmpeg nuk u gjet.")
        _job_update(jid, status="starting", progress=2, message="Fillim...")
        opts = _build_opts(jid, payload, folder)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(payload["url"], download=True)
        if jid in CANCELLED:
            _job_update(jid, status="cancelled", progress=0, message="U anulua.")
            return
        title = sanitize(info.get("title", "download")) if isinstance(info, dict) else "download"
        ext_filter = payload.get("audio_format", "mp3") if payload["mode"] == "audio" else "mp4"
        files = sorted(p for p in folder.rglob(f"*.{ext_filter}") if p.is_file())
        if not files:
            files = sorted(p for p in folder.rglob("*")
                          if p.is_file() and p.suffix.lower() not in {".part", ".ytdl", ".jpg", ".webp"})
        if not files:
            raise RuntimeError("File nuk u gjet.")
        final = files[0]
        fsize = final.stat().st_size
        _job_update(
            jid, status="done", progress=100, message="Gati!",
            filename=final.name, download_url=f"/download/{jid}",
            size_human=human_bytes(fsize), title=title,
        )
        add_history({
            "id": jid, "time": ts(), "title": title,
            "thumbnail": job.get("thumbnail", ""),
            "mode": payload["mode"], "format": ext_filter,
            "filename": final.name, "size": fsize,
            "size_human": human_bytes(fsize),
        })
        gid = job.get("group_id")
        if gid: _check_group_complete(gid)
    except Exception as exc:
        if jid in CANCELLED:
            _job_update(jid, status="cancelled", progress=0, message="U anulua.")
        else:
            _job_update(jid, status="error", progress=0,
                       message=f"Gabim: {str(exc)[:80]}")
            print(f"Job {jid} error: {exc}")
            gid = job.get("group_id")
            if gid: _check_group_complete(gid)


def _check_group_complete(gid: str) -> None:
    with _LOCK:
        group = GROUPS.get(gid)
        if not group: return
        jids = group.get("job_ids", [])
        statuses = [JOBS.get(j, {}).get("status") for j in jids]
        all_done = all(s in {"done", "error", "cancelled"} for s in statuses)
        if all_done and not group.get("zipped"):
            group["zipped"] = True
            threading.Thread(target=_zip_group, args=(gid,), daemon=True).start()


def _zip_group(gid: str) -> None:
    import zipfile
    try:
        with _LOCK:
            group = GROUPS.get(gid)
            if not group: return
            jids = list(group.get("job_ids", []))
            group_title = group.get("title", "playlist")
        zip_folder = JOBS_DIR / f"group_{gid}"
        zip_folder.mkdir(parents=True, exist_ok=True)
        zip_path = zip_folder / f"{sanitize(group_title)}.zip"
        added = 0; total_size = 0
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            for jid in jids:
                job = _job_get(jid)
                if not job or job.get("status") != "done": continue
                fname = job.get("filename")
                if not fname: continue
                fpath = Path(job["path"]) / fname
                if fpath.exists():
                    zf.write(fpath, arcname=fname)
                    added += 1
                    total_size += fpath.stat().st_size
        if added > 0:
            with _LOCK:
                group["zip_path"] = str(zip_path)
                group["zip_ready"] = True
                group["count"] = added
                group["size_human"] = human_bytes(total_size)
    except Exception as exc:
        print(f"ZIP error: {exc}")


def _worker() -> None:
    global _WORKER_ON
    with _LOCK:
        if _WORKER_ON: return
        _WORKER_ON = True
    try:
        with ThreadPoolExecutor(max_workers=PARALLEL_JOBS) as pool:
            active = set()
            while True:
                while len(active) < PARALLEL_JOBS:
                    with _LOCK:
                        jid = QUEUE.pop(0) if QUEUE else None
                    if not jid: break
                    fut = pool.submit(run_download, jid)
                    active.add(fut)
                if not active: break
                done = {f for f in active if f.done()}
                if done: active -= done
                else: time.sleep(0.1)
    finally:
        with _LOCK: _WORKER_ON = False

def _start_worker() -> None:
    threading.Thread(target=_worker, daemon=True).start()


def normalize_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "url": (data.get("url") or "").strip(),
        "mode": safe_str(data.get("mode"), {"audio","video"}, "audio"),
        "audio_format": safe_str(data.get("audio_format"), {"mp3","m4a","opus"}, "mp3"),
        "quality": safe_str(data.get("quality"), {"128","192","320"}, "192"),
        "video_quality": safe_str(data.get("video_quality"),{"360p","720p","1080p","best"}, "720p"),
    }


def _extract_playlist_entries(url: str, limit: int) -> List[Dict[str, Any]]:
    opts = {
        "quiet": True, "no_warnings": True,
        "extract_flat": "in_playlist", "skip_download": True,
        "playlistend": limit, "ignoreerrors": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not isinstance(info, dict): return []
    entries = info.get("entries", []) or []
    result = []
    for e in entries:
        if not e: continue
        u = e.get("url") or e.get("webpage_url") or e.get("id")
        if u and not u.startswith("http"):
            u = f"https://www.youtube.com/watch?v={u}"
        if u:
            vid = e.get("id", "")
            result.append({
                "url": u, "title": e.get("title", "Track"),
                "thumbnail": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if vid else "",
            })
    return result[:limit]


@app.route("/")
def index() -> Response:
    return Response(UI, mimetype="text/html")

@app.route("/api/health")
def api_health():
    return jsonify({
        "ffmpeg": ffmpeg_ok(),
        "jobs_active": sum(1 for j in JOBS.values() if j["status"] in ("queued","downloading","starting")),
    })

@app.route("/api/search", methods=["POST"])
def api_search():
    data = request.get_json(silent=True) or {}
    q = (data.get("q") or "").strip()
    if not q: return jsonify({"results": []})
    if is_url(q):
        return jsonify({"results": [], "is_url": True, "url": q})
    return jsonify({"results": yt_search(q, SEARCH_RESULTS)})

@app.route("/api/artist", methods=["POST"])
def api_artist():
    data = request.get_json(silent=True) or {}
    artist = (data.get("artist") or "").strip()
    if not artist: return jsonify({"songs": []})
    limit = safe_int(data.get("limit"), ARTIST_LIMIT, 5, 100)
    songs = yt_artist_songs(artist, limit)
    return jsonify({"songs": songs, "artist": artist, "count": len(songs)})

@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(silent=True) or {}
    payload = normalize_payload(data)
    if not is_url(payload["url"]):
        return jsonify({"error": "URL e pavlefshme."}), 400
    title = (data.get("title") or "").strip()
    thumb = (data.get("thumbnail") or "").strip()
    job = make_job(payload, title=title, thumbnail=thumb)
    _start_worker()
    return jsonify({"job_id": job["id"]})

@app.route("/api/start-playlist", methods=["POST"])
def api_start_playlist():
    data = request.get_json(silent=True) or {}
    payload = normalize_payload(data)
    if not is_url(payload["url"]):
        return jsonify({"error": "URL e pavlefshme."}), 400
    try:
        entries = _extract_playlist_entries(payload["url"], 250)
        if not entries:
            return jsonify({"error": "Playlist bosh."}), 400
    except Exception as exc:
        return jsonify({"error": f"Gabim: {exc}"}), 500
    gid = uuid.uuid4().hex
    job_ids = []
    for entry in entries:
        ep = {**payload, "url": entry["url"]}
        j = make_job(ep, title=entry["title"], group_id=gid,
                    thumbnail=entry.get("thumbnail", ""))
        job_ids.append(j["id"])
    with _LOCK:
        GROUPS[gid] = {
            "id": gid, "title": f"Playlist ({len(entries)})",
            "job_ids": job_ids, "count": len(entries),
            "created_at": now_ms(), "zipped": False, "zip_ready": False,
        }
    _start_worker()
    return jsonify({"group_id": gid, "count": len(entries)})

@app.route("/api/start-bulk", methods=["POST"])
def api_start_bulk():
    data = request.get_json(silent=True) or {}
    songs = data.get("songs") or []
    if not songs: return jsonify({"error": "Asnje keng."}), 400
    title = (data.get("title") or "playlist").strip()
    payload_base = normalize_payload(data)
    gid = uuid.uuid4().hex
    job_ids = []
    for s in songs[:250]:
        url = s.get("url")
        if not url or not is_url(url): continue
        p = {**payload_base, "url": url}
        j = make_job(p, title=s.get("title", "Track"),
                    group_id=gid, thumbnail=s.get("thumbnail", ""))
        job_ids.append(j["id"])
    with _LOCK:
        GROUPS[gid] = {
            "id": gid, "title": sanitize(title),
            "job_ids": job_ids, "count": len(job_ids),
            "created_at": now_ms(), "zipped": False, "zip_ready": False,
        }
    _start_worker()
    return jsonify({"group_id": gid, "count": len(job_ids)})

@app.route("/api/group/<gid>")
def api_group(gid: str):
    with _LOCK:
        group = GROUPS.get(gid)
        if not group:
            return jsonify({"error": "Grupi nuk u gjet."}), 404
        jids = list(group.get("job_ids", []))
        jobs_info = []
        for j in jids:
            job = JOBS.get(j)
            if job:
                jobs_info.append({
                    "id": j, "title": job.get("title", ""),
                    "thumbnail": job.get("thumbnail", ""),
                    "status": job.get("status"),
                    "progress": job.get("progress", 0),
                    "size_human": job.get("size_human"),
                    "download_url": job.get("download_url"),
                })
        statuses = [JOBS.get(j, {}).get("status") for j in jids]
        done = sum(1 for s in statuses if s == "done")
        error = sum(1 for s in statuses if s == "error")
        active = sum(1 for s in statuses if s in {"downloading", "starting", "queued"})
        progress_total = sum(JOBS.get(j, {}).get("progress", 0) for j in jids)
        progress_avg = int(progress_total / len(jids)) if jids else 0
        result = {
            "id": gid, "title": group.get("title"), "count": group.get("count"),
            "done": done, "error": error, "active": active,
            "progress": progress_avg, "zip_ready": group.get("zip_ready", False),
            "jobs": jobs_info,
        }
        if group.get("zip_ready"):
            result["download_url"] = f"/group-download/{gid}"
            result["size_human"] = group.get("size_human")
        return jsonify(result)

@app.route("/group-download/<gid>")
def group_download(gid: str):
    with _LOCK:
        group = GROUPS.get(gid)
        if not group or not group.get("zip_ready"):
            return jsonify({"error": "ZIP nuk eshte gati."}), 400
        zip_path = group.get("zip_path")
    if not zip_path or not Path(zip_path).exists():
        return jsonify({"error": "ZIP nuk ekziston."}), 404
    return send_file(zip_path, as_attachment=True,
                    download_name=Path(zip_path).name,
                    mimetype="application/zip")

@app.route("/api/status/<jid>")
def api_status(jid: str):
    job = _job_get(jid)
    if not job: return jsonify({"error":"Jo i gjetur."}), 404
    return jsonify({k: v for k, v in job.items() if k != "path"})

@app.route("/api/cancel/<jid>", methods=["POST"])
def api_cancel(jid: str):
    with _LOCK:
        if jid not in JOBS: return jsonify({"error":"Jo i gjetur."}), 404
        CANCELLED.add(jid)
        if jid in QUEUE:
            QUEUE.remove(jid)
            JOBS[jid].update({"status":"cancelled","message":"U anulua.","progress":0})
    return jsonify({"ok": True})

@app.route("/download/<jid>")
def download(jid: str):
    job = _job_get(jid)
    if not job: return jsonify({"error":"Jo i gjetur."}), 404
    if job.get("status") != "done": return jsonify({"error":"Jo gati."}), 400
    folder = Path(job["path"])
    fname  = job.get("filename")
    target = folder / fname if fname else None
    if not target or not target.exists():
        files = [p for p in folder.rglob("*") if p.is_file()]
        if not files: return jsonify({"error":"File jo i gjetur."}), 404
        target = files[0]
    return send_file(target, as_attachment=True, download_name=target.name,
                    mimetype="application/octet-stream")

@app.route("/api/history")
def api_history():
    return jsonify(_read_history())

@app.route("/api/history/<hid>", methods=["DELETE"])
def api_history_delete(hid: str):
    return jsonify({"ok": delete_history_item(hid)})

@app.route("/api/history/clear", methods=["POST"])
def api_history_clear():
    save_json(HISTORY_FILE, [])
    return jsonify({"ok": True})


UI = r"""<!DOCTYPE html>
<html lang="sq">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FixoTools — Lite</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root {
  --navy-abyss: #050d1f;
  --navy-deep:  #0a1a3a;
  --navy:       #0f2557;
  --navy-soft:  #5a73ab;
  --gold:       #d4b572;
  --gold-bright:#e8c987;
  --white:      #ffffff;
  --cream:      #fbfaf6;
  --ivory:      #f4f1e9;
  --paper:      #ebe7da;
  --paper-deep: #d6cfbe;
  --success:    #2d8a5f;
  --danger:     #c44545;
  --font: 'Inter', system-ui, -apple-system, sans-serif;
}
* { margin:0; padding:0; box-sizing:border-box; }
html, body {
  background: var(--cream); color: var(--navy-deep);
  font-family: var(--font); font-size: 14px;
  min-height: 100vh;
}
body { padding: 24px 16px 60px; }
.wrap { max-width: 1080px; margin: 0 auto; }

.header {
  display: flex; justify-content: space-between; align-items: center;
  padding-bottom: 18px; margin-bottom: 24px;
  border-bottom: 1px solid var(--paper-deep);
}
.brand { display: flex; align-items: center; gap: 12px; }
.brand-icon {
  width: 40px; height: 40px;
  background: var(--navy-abyss); color: var(--gold);
  border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
  font-weight: 800; font-size: 16px;
}
.brand h1 { font-size: 18px; font-weight: 800; color: var(--navy-abyss); }
.brand .sub { font-size: 11px; color: var(--navy-soft); font-weight: 500; }
.stat-mini {
  font-size: 12px; color: var(--navy-soft); font-weight: 600;
  padding: 6px 12px; background: var(--white);
  border: 1px solid var(--paper-deep); border-radius: 6px;
}
.stat-mini strong { color: var(--navy-abyss); }

.tabs {
  display: flex; gap: 4px; padding: 4px;
  background: var(--white); border: 1px solid var(--paper-deep);
  border-radius: 8px; margin-bottom: 20px;
}
.tab {
  flex: 1; padding: 10px 16px;
  font-family: inherit; font-size: 13px; font-weight: 600;
  color: var(--navy-soft); background: transparent;
  border: none; border-radius: 5px; cursor: pointer;
  transition: all 0.15s ease;
}
.tab:hover { color: var(--navy); background: var(--ivory); }
.tab.active { background: var(--navy-abyss); color: var(--cream); }

.panel {
  background: var(--white);
  border: 1px solid var(--paper-deep);
  border-radius: 10px; padding: 24px;
}
.panel.hidden { display: none; }
.panel-title { font-size: 20px; font-weight: 700; color: var(--navy-abyss); margin-bottom: 4px; }
.panel-sub { font-size: 13px; color: var(--navy-soft); margin-bottom: 20px; }

.search-box { position: relative; margin-bottom: 16px; }
.search-input {
  width: 100%; padding: 14px 50px 14px 44px;
  background: var(--cream);
  border: 1.5px solid var(--paper-deep);
  border-radius: 8px;
  font-family: inherit; font-size: 15px; font-weight: 500;
  color: var(--navy-abyss);
}
.search-input:focus { outline: none; border-color: var(--navy); background: var(--white); }
.search-icon {
  position: absolute; left: 14px; top: 50%;
  transform: translateY(-50%); color: var(--navy-soft);
}
.search-spin {
  position: absolute; right: 14px; top: 50%;
  transform: translateY(-50%); display: none;
  width: 18px; height: 18px;
  border: 2px solid var(--paper);
  border-top-color: var(--navy);
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
}
.search-spin.show { display: block; }
@keyframes spin { to { transform: translateY(-50%) rotate(360deg); } }

.opts { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }
.opt {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 6px 10px;
  background: var(--cream);
  border: 1px solid var(--paper-deep);
  border-radius: 6px; font-size: 12px;
}
.opt label { color: var(--navy-soft); font-weight: 500; }
.opt select {
  padding: 2px 4px; background: transparent; border: none;
  font-family: inherit; font-size: 12px; font-weight: 600;
  color: var(--navy-abyss); cursor: pointer;
}
.opt select:focus { outline: none; }

.results { display: grid; gap: 8px; }
.result {
  display: grid; grid-template-columns: 100px 1fr auto;
  gap: 14px; align-items: center;
  padding: 10px;
  background: var(--cream);
  border: 1px solid var(--paper-deep);
  border-radius: 8px;
}
.result:hover { border-color: var(--navy-soft); background: var(--white); }
.result-thumb {
  width: 100px; height: 60px;
  background: var(--navy-deep);
  border-radius: 6px; overflow: hidden; position: relative;
}
.result-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.result-duration {
  position: absolute; bottom: 3px; right: 3px;
  background: rgba(5,13,31,0.92); color: var(--cream);
  font-size: 10px; font-weight: 600;
  padding: 1px 5px; border-radius: 3px;
}
.result-body { min-width: 0; }
.result-title {
  font-size: 14px; font-weight: 600;
  color: var(--navy-abyss); line-height: 1.3;
  display: -webkit-box; -webkit-line-clamp: 2;
  -webkit-box-orient: vertical; overflow: hidden;
  margin-bottom: 4px;
}
.result-meta {
  font-size: 12px; color: var(--navy-soft);
  display: flex; gap: 8px; flex-wrap: wrap; align-items: center;
}
.uploader-link {
  color: var(--navy); font-weight: 600; cursor: pointer;
  text-decoration: underline; text-decoration-color: transparent;
}
.uploader-link:hover { text-decoration-color: var(--navy); }
.result-actions { display: flex; flex-direction: column; gap: 4px; }

.btn {
  padding: 9px 14px;
  font-family: inherit; font-size: 12px; font-weight: 700;
  border: none; border-radius: 6px; cursor: pointer;
  display: inline-flex; align-items: center; gap: 6px;
  white-space: nowrap;
}
.btn-primary { background: var(--navy-abyss); color: var(--cream); }
.btn-primary:hover { background: var(--navy); }
.btn-primary:disabled { background: var(--paper-deep); color: var(--navy-soft); cursor: not-allowed; }
.btn-gold { background: var(--gold); color: var(--navy-abyss); }
.btn-gold:hover { background: var(--gold-bright); }
.btn-success { background: var(--success); color: var(--white); }
.btn-success:hover { background: #246b4a; }
.btn-ghost { background: var(--white); color: var(--navy); border: 1px solid var(--paper-deep); }
.btn-ghost:hover { background: var(--paper); }
.btn-danger { background: var(--danger); color: var(--white); }
.btn-danger:hover { background: #a83838; }
.btn-mini { padding: 6px 10px; font-size: 11px; }

.empty { padding: 40px 20px; text-align: center; color: var(--navy-soft); font-size: 13px; }
.empty-big { font-size: 32px; margin-bottom: 10px; opacity: 0.5; }

.dl-tracker {
  position: fixed; bottom: 16px; right: 16px;
  width: 340px; max-width: calc(100vw - 32px);
  z-index: 100; display: flex; flex-direction: column;
  gap: 6px; pointer-events: none;
}
.dl-item {
  background: var(--white);
  border: 1px solid var(--paper-deep);
  border-radius: 8px; padding: 10px 12px;
  box-shadow: 0 8px 24px rgba(15,30,61,0.12);
  pointer-events: auto; position: relative;
}
.dl-item.done { border-color: var(--success); }
.dl-item.error { border-color: var(--danger); }
.dl-row { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
.dl-thumb {
  width: 36px; height: 36px;
  background: var(--navy-deep);
  border-radius: 4px; overflow: hidden; flex-shrink: 0;
}
.dl-thumb img { width: 100%; height: 100%; object-fit: cover; }
.dl-info { flex: 1; min-width: 0; }
.dl-title {
  font-size: 12px; font-weight: 600;
  color: var(--navy-abyss);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.dl-status { font-size: 11px; color: var(--navy-soft); margin-top: 1px; }
.dl-prog { height: 3px; background: var(--paper); border-radius: 2px; overflow: hidden; }
.dl-prog-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--navy), var(--gold));
  transition: width 0.3s ease;
}
.dl-close {
  position: absolute; top: 4px; right: 4px;
  width: 18px; height: 18px;
  background: transparent; border: none; cursor: pointer;
  color: var(--navy-soft); font-size: 14px;
}
.dl-actions { display: flex; gap: 4px; margin-top: 6px; }

.group-status {
  margin-top: 16px; padding: 16px;
  background: var(--cream); border: 1px solid var(--paper-deep);
  border-radius: 8px; display: none;
}
.group-status.show { display: block; }
.group-header {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 12px; flex-wrap: wrap; gap: 10px;
}
.group-title { font-size: 14px; font-weight: 700; color: var(--navy-abyss); }
.group-counts { display: flex; gap: 6px; flex-wrap: wrap; }
.chip {
  font-size: 11px; font-weight: 600;
  padding: 3px 8px; border-radius: 10px;
  background: var(--paper); color: var(--navy);
}
.chip.done { background: var(--success); color: var(--white); }
.chip.active { background: var(--navy); color: var(--cream); }
.chip.error { background: var(--danger); color: var(--white); }
.group-bar { height: 5px; background: var(--paper); border-radius: 3px; overflow: hidden; margin-bottom: 12px; }
.group-bar-fill {
  height: 100%; width: 0%;
  background: linear-gradient(90deg, var(--navy), var(--gold));
  transition: width 0.4s ease;
}
.sub-jobs { max-height: 260px; overflow-y: auto; display: grid; gap: 4px; }
.sub-job {
  display: grid; grid-template-columns: 14px 1fr 50px;
  align-items: center; gap: 8px;
  padding: 5px 8px;
  background: var(--white);
  border: 1px solid var(--paper);
  border-radius: 4px; font-size: 11px;
}
.sub-job.done { border-color: var(--success); }
.sub-job.active { border-color: var(--navy); }
.sub-job.error { border-color: var(--danger); }
.sub-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--paper-deep); }
.sub-dot.starting, .sub-dot.downloading { background: var(--navy); }
.sub-dot.done { background: var(--success); }
.sub-dot.error { background: var(--danger); }
.sub-name { color: var(--navy-deep); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.sub-pct { font-weight: 600; color: var(--navy); text-align: right; }

.modal {
  position: fixed; inset: 0;
  background: rgba(5,13,31,0.5);
  z-index: 200; display: none;
  align-items: center; justify-content: center;
  padding: 20px;
}
.modal.show { display: flex; }
.modal-content {
  background: var(--white); border-radius: 12px;
  max-width: 700px; width: 100%; max-height: 80vh;
  display: flex; flex-direction: column; overflow: hidden;
}
.modal-header {
  padding: 18px 20px;
  border-bottom: 1px solid var(--paper-deep);
  display: flex; justify-content: space-between; align-items: center;
}
.modal-title { font-size: 18px; font-weight: 700; color: var(--navy-abyss); }
.modal-title strong { color: var(--gold); }
.modal-close {
  width: 32px; height: 32px;
  background: transparent;
  border: 1px solid var(--paper-deep);
  border-radius: 6px; font-size: 18px;
  cursor: pointer; color: var(--navy-soft);
}
.modal-body { flex: 1; overflow-y: auto; padding: 16px 20px; }
.modal-body .empty-state { padding: 60px 20px; text-align: center; color: var(--navy-soft); }
.song-pick {
  display: grid; grid-template-columns: 24px 80px 1fr 60px;
  gap: 10px; align-items: center;
  padding: 8px; border-radius: 6px;
  border: 1px solid var(--paper);
  margin-bottom: 4px;
}
.song-pick:hover { background: var(--cream); }
.song-pick input[type="checkbox"] { width: 16px; height: 16px; accent-color: var(--navy); cursor: pointer; }
.song-pick-thumb { width: 80px; height: 48px; background: var(--navy-deep); border-radius: 4px; overflow: hidden; }
.song-pick-thumb img { width: 100%; height: 100%; object-fit: cover; }
.song-pick-title { font-size: 13px; font-weight: 600; color: var(--navy-abyss); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.song-pick-meta { font-size: 11px; color: var(--navy-soft); }
.song-pick-dur { font-size: 11px; color: var(--navy-soft); text-align: right; }
.modal-footer {
  padding: 14px 20px;
  border-top: 1px solid var(--paper-deep);
  display: flex; justify-content: space-between; align-items: center;
  gap: 10px; flex-wrap: wrap;
}
.modal-select-info { font-size: 13px; color: var(--navy); font-weight: 600; }
.modal-actions { display: flex; gap: 8px; }
.modal-spin {
  width: 24px; height: 24px;
  border: 3px solid var(--paper);
  border-top-color: var(--navy);
  border-radius: 50%;
  animation: spin2 0.7s linear infinite;
  margin: 0 auto;
}
@keyframes spin2 { to { transform: rotate(360deg); } }

.textarea {
  width: 100%; padding: 12px 14px;
  background: var(--cream);
  border: 1.5px solid var(--paper-deep);
  border-radius: 8px;
  font-family: 'JetBrains Mono', monospace; font-size: 12px;
  color: var(--navy-abyss);
  min-height: 160px; resize: vertical; line-height: 1.6;
}
.textarea:focus { outline: none; border-color: var(--navy); background: var(--white); }
.field-label {
  display: flex; justify-content: space-between; align-items: center;
  font-size: 12px; font-weight: 600;
  color: var(--navy-soft); margin-bottom: 6px;
}

.hist-list { display: grid; gap: 6px; }
.hist {
  display: grid; grid-template-columns: 70px 1fr auto auto;
  align-items: center; gap: 12px;
  padding: 8px 12px;
  background: var(--cream); border: 1px solid var(--paper-deep);
  border-radius: 6px;
}
.hist-thumb { width: 70px; height: 42px; background: var(--navy-deep); border-radius: 4px; overflow: hidden; }
.hist-thumb img { width: 100%; height: 100%; object-fit: cover; }
.hist-name { font-size: 13px; font-weight: 600; color: var(--navy-abyss); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.hist-meta { font-size: 11px; color: var(--navy-soft); margin-top: 2px; }
.hist-size { font-size: 11px; color: var(--navy); font-weight: 600; }
.hist-del { width: 28px; height: 28px; border-radius: 4px; background: transparent; border: 1px solid var(--paper-deep); color: var(--danger); cursor: pointer; }
.hist-del:hover { background: var(--danger); color: var(--white); }

.footer { margin-top: 24px; padding-top: 16px; border-top: 1px solid var(--paper-deep); text-align: center; font-size: 11px; color: var(--navy-soft); }

@media (max-width: 700px) {
  body { padding: 16px 12px; }
  .panel { padding: 18px 14px; }
  .result { grid-template-columns: 80px 1fr; }
  .result-actions { grid-column: 1 / -1; flex-direction: row; margin-top: 4px; }
  .dl-tracker { width: calc(100vw - 24px); right: 12px; bottom: 12px; }
  .song-pick { grid-template-columns: 24px 60px 1fr; }
  .song-pick-dur { display: none; }
}
</style>
</head>
<body>
<div class="wrap">
  <header class="header">
    <div class="brand">
      <div class="brand-icon">FX</div>
      <div>
        <h1>FIXOTOOLS</h1>
        <div class="sub">Search · Download · Lite</div>
      </div>
    </div>
    <div class="stat-mini">Aktive: <strong id="statActive">0</strong></div>
  </header>

  <div class="tabs">
    <button class="tab active" data-tab="search">Kerko &amp; Shkarko</button>
    <button class="tab" data-tab="batch">Playlist / Batch</button>
    <button class="tab" data-tab="history">Historiku</button>
  </div>

  <section class="panel" id="panel-search">
    <div class="panel-title">Kerko nje kenge</div>
    <div class="panel-sub">Shkruaj artist ose titull. Kliko "Shkarko" per shkarkim direkt.</div>
    <div class="search-box">
      <svg class="search-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
        <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
      </svg>
      <input type="text" class="search-input" id="searchInput" placeholder="Dua Lipa, Imagine Dragons, ose URL..." spellcheck="false" autofocus>
      <div class="search-spin" id="searchSpin"></div>
    </div>
    <div class="opts">
      <div class="opt"><label>Format</label>
        <select id="optMode"><option value="audio">MP3</option><option value="video">MP4</option></select>
      </div>
      <div class="opt"><label>Cilesia</label>
        <select id="optQuality"><option value="128">128 kbps</option><option value="192" selected>192 kbps</option><option value="320">320 kbps</option></select>
      </div>
      <div class="opt" id="optVideoBox" style="display:none"><label>Rezolucioni</label>
        <select id="optVideo"><option value="360p">360p</option><option value="720p" selected>720p</option><option value="1080p">1080p</option></select>
      </div>
    </div>
    <div class="results" id="results">
      <div class="empty">
        <div class="empty-big">&#128270;</div>
        <p>Shkruaj emrin e nje kenge per te filluar</p>
        <p style="margin-top:8px; font-size:11px">Te gjithe artistet kane butonin "Bej Playlist" anash &#127925;</p>
      </div>
    </div>
  </section>

  <section class="panel hidden" id="panel-batch">
    <div class="panel-title">Playlist ose Shume URL</div>
    <div class="panel-sub">Vendos nje URL playlist-i, ose shume URL kengesh (nje per rresht). Sistemi e dallon automatikisht.</div>
    <div>
      <div class="field-label">
        <span>URL</span>
        <span id="batchInfo" style="color:var(--navy)"></span>
      </div>
      <textarea class="textarea" id="batchLinks" placeholder="https://youtube.com/playlist?list=...&#10;ose shume linke:&#10;https://youtube.com/watch?v=..." spellcheck="false"></textarea>
    </div>
    <div class="opts" style="margin-top:12px">
      <div class="opt"><label>Format</label>
        <select id="batchMode"><option value="audio" selected>MP3</option><option value="video">MP4</option></select>
      </div>
      <div class="opt"><label>Cilesia</label>
        <select id="batchQuality"><option value="128">128 kbps</option><option value="192" selected>192 kbps</option><option value="320">320 kbps</option></select>
      </div>
    </div>
    <div style="display:flex; gap:8px; margin-top:14px">
      <button class="btn btn-primary" id="batchBtn"><span id="batchBtnText">Fillo shkarkimin</span></button>
      <button class="btn btn-ghost" id="clearBatchBtn">Pastro</button>
    </div>
    <div class="group-status" id="groupStatus">
      <div class="group-header">
        <div class="group-title" id="groupTitle">Processing</div>
        <div class="group-counts" id="groupCounts"></div>
      </div>
      <div class="group-bar"><div class="group-bar-fill" id="groupBar"></div></div>
      <div class="sub-jobs" id="subJobs"></div>
    </div>
    <div id="groupDlBox" style="margin-top:12px; display:none">
      <a class="btn btn-success" id="groupDlLink" href="#" download>&darr; Shkarko ZIP <span id="groupDlInfo"></span></a>
    </div>
  </section>

  <section class="panel hidden" id="panel-history">
    <div class="panel-title">Historiku</div>
    <div class="panel-sub">Te gjitha shkarkimet e fundit</div>
    <div style="display:flex; gap:8px; margin-bottom:14px">
      <button class="btn btn-ghost btn-mini" onclick="loadHistory()">Rifresko</button>
      <button class="btn btn-danger btn-mini" id="clearHistBtn">Pastro</button>
    </div>
    <div class="hist-list" id="histList"><div class="empty"><p>Ende bosh</p></div></div>
  </section>

  <footer class="footer">Vetem per perdorim vetjak &middot; Respekto te drejtat e autorit</footer>
</div>

<div class="dl-tracker" id="dlTracker"></div>

<div class="modal" id="artistModal">
  <div class="modal-content">
    <div class="modal-header">
      <div class="modal-title">Krijo Playlist nga <strong id="artistName">Artisti</strong></div>
      <button class="modal-close" onclick="closeArtistModal()">&times;</button>
    </div>
    <div class="modal-body" id="artistBody">
      <div class="empty-state"><div class="modal-spin"></div><p style="margin-top:12px">Duke mbledhur kenget...</p></div>
    </div>
    <div class="modal-footer">
      <div class="modal-select-info" id="selectInfo">0 kenge te zgjedhura</div>
      <div class="modal-actions">
        <button class="btn btn-ghost btn-mini" onclick="toggleAllSongs()">Zgjidh/Hiq</button>
        <button class="btn btn-primary" id="downloadPlaylistBtn" onclick="downloadArtistPlaylist()">&darr; Shkarko Playlist</button>
      </div>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s||'').replace(/[<>&"]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
const active = new Map();
let lastResults = [];
let artistSongs = [];

async function api(path, method='GET', body=null) {
  const opts = { method, headers: { 'Content-Type':'application/json' } };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || 'Gabim');
  return data;
}

document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.add('hidden'));
    tab.classList.add('active');
    $('panel-' + tab.dataset.tab).classList.remove('hidden');
    if (tab.dataset.tab === 'history') loadHistory();
  });
});

$('optMode').addEventListener('change', () => {
  $('optVideoBox').style.display = $('optMode').value === 'video' ? '' : 'none';
});

let _searchTimer = null;
$('searchInput').addEventListener('input', e => {
  const q = e.target.value.trim();
  if (_searchTimer) clearTimeout(_searchTimer);
  if (!q) {
    $('results').innerHTML = '<div class="empty"><div class="empty-big">&#128270;</div><p>Shkruaj emrin e nje kenge per te filluar</p></div>';
    $('searchSpin').classList.remove('show');
    return;
  }
  _searchTimer = setTimeout(() => doSearch(q), 350);
});
$('searchInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') {
    if (_searchTimer) clearTimeout(_searchTimer);
    const q = e.target.value.trim();
    if (q) doSearch(q);
  }
});

async function doSearch(q) {
  $('searchSpin').classList.add('show');
  try {
    const r = await api('/api/search', 'POST', { q });
    if (r.is_url) {
      $('results').innerHTML = '<div class="empty"><p>URL e detektuar - shkarkimi po fillon...</p></div>';
      startDownload({ url: r.url, title: 'Download', thumbnail: '' });
      $('searchInput').value = '';
      return;
    }
    renderResults(r.results || []);
  } catch (e) {
    $('results').innerHTML = '<div class="empty"><p>Gabim: ' + esc(e.message) + '</p></div>';
  } finally {
    $('searchSpin').classList.remove('show');
  }
}

function formatViews(n) {
  if (n >= 1e9) return (n/1e9).toFixed(1) + 'B';
  if (n >= 1e6) return (n/1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n/1e3).toFixed(1) + 'K';
  return String(n);
}

function renderResults(results) {
  lastResults = results;
  if (!results.length) {
    $('results').innerHTML = '<div class="empty"><div class="empty-big">&#128542;</div><p>Asnje rezultat</p></div>';
    return;
  }
  $('results').innerHTML = results.map((r, i) => {
    const views = r.views ? formatViews(r.views) + ' views' : '';
    const uploader = r.uploader ?
      '<span class="uploader-link" onclick="openArtist(\'' + esc(r.uploader).replace(/'/g, "\\'") + '\')">' + esc(r.uploader) + '</span>' : '';
    return '<div class="result">' +
      '<div class="result-thumb">' +
        (r.thumbnail ? '<img src="' + esc(r.thumbnail) + '" alt="" loading="lazy">' : '') +
        (r.duration ? '<div class="result-duration">' + esc(r.duration) + '</div>' : '') +
      '</div>' +
      '<div class="result-body">' +
        '<div class="result-title">' + esc(r.title) + '</div>' +
        '<div class="result-meta">' + uploader + (views ? '<span>&middot; ' + views + '</span>' : '') + '</div>' +
      '</div>' +
      '<div class="result-actions">' +
        '<button class="btn btn-primary btn-mini" onclick="downloadResult(' + i + ')">&darr; Shkarko</button>' +
        (r.uploader ? '<button class="btn btn-gold btn-mini" onclick="openArtist(\'' + esc(r.uploader).replace(/'/g, "\\'") + '\')">&#127925; Playlist</button>' : '') +
      '</div>' +
    '</div>';
  }).join('');
}

window.downloadResult = function(idx) {
  const r = lastResults[idx];
  if (!r) return;
  startDownload({ url: r.url, title: r.title, thumbnail: r.thumbnail });
};

async function startDownload(item) {
  const tempId = 'tmp-' + Date.now();
  active.set(tempId, {
    id: tempId, title: item.title, thumbnail: item.thumbnail,
    status: 'queued', progress: 0, message: 'Duke filluar...',
  });
  renderTracker();
  try {
    const r = await api('/api/start', 'POST', {
      url: item.url, title: item.title, thumbnail: item.thumbnail,
      mode: $('optMode').value, audio_format: 'mp3',
      quality: $('optQuality').value, video_quality: $('optVideo').value,
    });
    if (r.job_id) {
      const it = active.get(tempId);
      active.delete(tempId);
      it.id = r.job_id;
      active.set(r.job_id, it);
      renderTracker();
      watchJob(r.job_id);
    }
  } catch (e) {
    active.delete(tempId);
    renderTracker();
    alert(e.message);
  }
}

function renderTracker() {
  const items = Array.from(active.values()).slice(-5);
  if (!items.length) { $('dlTracker').innerHTML = ''; return; }
  $('dlTracker').innerHTML = items.map(it => {
    const cls = it.status === 'done' ? 'done' : it.status === 'error' ? 'error' : '';
    const dl = it.download_url ? '<a class="btn btn-success btn-mini" href="' + esc(it.download_url) + '" download>&darr; Shkarko</a>' : '';
    const cancel = !['done','error','cancelled'].includes(it.status) ?
      '<button class="btn btn-ghost btn-mini" onclick="cancelDl(\'' + it.id + '\')">Anulo</button>' : '';
    return '<div class="dl-item ' + cls + '">' +
      '<button class="dl-close" onclick="closeTracker(\'' + it.id + '\')">&times;</button>' +
      '<div class="dl-row">' +
        '<div class="dl-thumb">' + (it.thumbnail ? '<img src="' + esc(it.thumbnail) + '" alt="">' : '') + '</div>' +
        '<div class="dl-info">' +
          '<div class="dl-title">' + esc(it.title) + '</div>' +
          '<div class="dl-status">' + esc(it.message || it.status) + (it.speed_human ? ' &middot; ' + esc(it.speed_human) : '') + '</div>' +
        '</div>' +
      '</div>' +
      '<div class="dl-prog"><div class="dl-prog-fill" style="width:' + (it.progress||0) + '%"></div></div>' +
      (dl || cancel ? '<div class="dl-actions">' + dl + cancel + '</div>' : '') +
    '</div>';
  }).join('');
}

window.closeTracker = function(jid) { active.delete(jid); renderTracker(); };
window.cancelDl = async function(jid) {
  try { await api('/api/cancel/' + jid, 'POST', {}); } catch (e) {}
};

async function watchJob(jid) {
  const interval = setInterval(async () => {
    try {
      const j = await api('/api/status/' + jid);
      if (active.has(jid)) {
        Object.assign(active.get(jid), j);
        renderTracker();
      }
      if (['done','error','cancelled'].includes(j.status)) {
        clearInterval(interval);
        if (j.status === 'done' && j.download_url) {
          setTimeout(() => {
            const a = document.createElement('a');
            a.href = j.download_url; a.download = j.filename || 'download';
            document.body.appendChild(a); a.click(); document.body.removeChild(a);
          }, 300);
        }
        loadHealth();
      }
    } catch (e) { clearInterval(interval); }
  }, 700);
}

window.openArtist = async function(artist) {
  $('artistModal').classList.add('show');
  $('artistName').textContent = artist;
  $('artistBody').innerHTML = '<div class="empty-state"><div class="modal-spin"></div><p style="margin-top:12px">Duke mbledhur kenget e ' + esc(artist) + '...</p></div>';
  $('selectInfo').textContent = '0 kenge te zgjedhura';
  try {
    const r = await api('/api/artist', 'POST', { artist, limit: 50 });
    artistSongs = r.songs || [];
    if (!artistSongs.length) {
      $('artistBody').innerHTML = '<div class="empty-state"><p>Asnje keng e gjetur per ' + esc(artist) + '.</p></div>';
      return;
    }
    renderArtistSongs();
  } catch (e) {
    $('artistBody').innerHTML = '<div class="empty-state"><p>Gabim: ' + esc(e.message) + '</p></div>';
  }
};

function renderArtistSongs() {
  $('artistBody').innerHTML = artistSongs.map((s, i) =>
    '<label class="song-pick">' +
      '<input type="checkbox" checked data-idx="' + i + '" onchange="updateSelectCount()">' +
      '<div class="song-pick-thumb">' + (s.thumbnail ? '<img src="' + esc(s.thumbnail) + '" alt="" loading="lazy">' : '') + '</div>' +
      '<div><div class="song-pick-title">' + esc(s.title) + '</div><div class="song-pick-meta">' + esc(s.uploader || '') + '</div></div>' +
      '<div class="song-pick-dur">' + esc(s.duration || '') + '</div>' +
    '</label>'
  ).join('');
  updateSelectCount();
}

window.updateSelectCount = function() {
  const n = document.querySelectorAll('#artistBody input[type="checkbox"]:checked').length;
  $('selectInfo').textContent = n + ' kenge te zgjedhura';
};

window.toggleAllSongs = function() {
  const boxes = document.querySelectorAll('#artistBody input[type="checkbox"]');
  const anyUnchecked = Array.from(boxes).some(b => !b.checked);
  boxes.forEach(b => b.checked = anyUnchecked);
  updateSelectCount();
};

window.closeArtistModal = function() { $('artistModal').classList.remove('show'); };

window.downloadArtistPlaylist = async function() {
  const checked = Array.from(document.querySelectorAll('#artistBody input[type="checkbox"]:checked'));
  if (!checked.length) { alert('Zgjidh te pakten 1 keng.'); return; }
  const selected = checked.map(b => artistSongs[+b.dataset.idx]).filter(Boolean);
  const artist = $('artistName').textContent;
  try {
    $('downloadPlaylistBtn').disabled = true;
    const r = await api('/api/start-bulk', 'POST', {
      songs: selected, title: artist + ' - Playlist',
      mode: $('optMode').value, audio_format: 'mp3', quality: $('optQuality').value,
    });
    closeArtistModal();
    document.querySelector('[data-tab="batch"]').click();
    watchGroup(r.group_id, r.count);
  } catch (e) { alert(e.message); }
  finally { $('downloadPlaylistBtn').disabled = false; }
};

function isPlaylistUrl(url) {
  return /[?&]list=[A-Za-z0-9_-]+/.test(url) || /\/playlist\?/.test(url);
}

function parseBatchUrls() {
  return $('batchLinks').value.split('\n').map(l => l.trim()).filter(l => l && l.startsWith('http'));
}

function detectBatchMode() {
  const urls = parseBatchUrls();
  if (!urls.length) return 'empty';
  if (urls.length === 1 && isPlaylistUrl(urls[0])) return 'playlist';
  if (urls.length === 1) return 'single';
  return 'batch';
}

function updateBatchInfo() {
  const urls = parseBatchUrls();
  const mode = detectBatchMode();
  const info = $('batchInfo');
  const btnText = $('batchBtnText');
  if (mode === 'playlist') {
    info.textContent = '\u{1F3B5} PLAYLIST e detektuar';
    info.style.color = 'var(--gold)';
    btnText.textContent = 'Shkarko Playlist';
  } else if (mode === 'batch') {
    info.textContent = urls.length + ' URL';
    info.style.color = urls.length > 250 ? 'var(--danger)' : 'var(--navy)';
    btnText.textContent = 'Shkarko ' + urls.length + ' file';
  } else if (mode === 'single') {
    info.textContent = '1 URL';
    info.style.color = 'var(--navy)';
    btnText.textContent = 'Shkarko';
  } else {
    info.textContent = '';
    btnText.textContent = 'Fillo shkarkimin';
  }
}
$('batchLinks').addEventListener('input', updateBatchInfo);

$('clearBatchBtn').onclick = () => {
  $('batchLinks').value = '';
  updateBatchInfo();
};

$('batchBtn').onclick = async () => {
  const urls = parseBatchUrls();
  if (!urls.length) return;
  if (urls.length > 250) { alert('Maks 250 URL.'); return; }
  const mode = detectBatchMode();
  $('batchBtn').disabled = true;
  $('groupDlBox').style.display = 'none';
  try {
    if (mode === 'playlist') {
      $('groupStatus').classList.add('show');
      $('groupTitle').textContent = 'Analize playlist-i...';
      $('groupCounts').innerHTML = '<span class="chip">Duke lexuar...</span>';
      const r = await api('/api/start-playlist', 'POST', {
        url: urls[0], mode: $('batchMode').value, quality: $('batchQuality').value,
      });
      watchGroup(r.group_id, r.count);
    } else if (mode === 'single') {
      const r = await api('/api/start', 'POST', {
        url: urls[0], mode: $('batchMode').value, quality: $('batchQuality').value,
      });
      $('batchLinks').value = '';
      updateBatchInfo();
      $('batchBtn').disabled = false;
      if (r.job_id) {
        addToTracker(r.job_id, { title: 'Download', thumbnail: '' });
        watchJob(r.job_id);
      }
    } else {
      const r = await api('/api/start-bulk', 'POST', {
        songs: urls.map(u => ({ url: u, title: 'Track', thumbnail: '' })),
        title: 'Batch ' + new Date().toISOString().slice(0, 10),
        mode: $('batchMode').value, quality: $('batchQuality').value,
      });
      watchGroup(r.group_id, r.count);
    }
  } catch (e) {
    alert(e.message);
    $('batchBtn').disabled = false;
    $('groupStatus').classList.remove('show');
  }
};

function addToTracker(jid, item) {
  active.set(jid, {
    id: jid, title: item.title || 'Download',
    thumbnail: item.thumbnail || '', status: 'queued', progress: 0,
  });
  renderTracker();
}

function watchGroup(gid, expected) {
  $('groupStatus').classList.add('show');
  $('groupTitle').textContent = 'Processing (' + expected + ' file)';
  const interval = setInterval(async () => {
    try {
      const g = await api('/api/group/' + gid);
      $('groupTitle').textContent = g.title;
      $('groupCounts').innerHTML =
        '<span class="chip active">' + g.active + ' aktive</span>' +
        '<span class="chip done">' + g.done + ' done</span>' +
        (g.error > 0 ? '<span class="chip error">' + g.error + ' err</span>' : '');
      $('groupBar').style.width = g.progress + '%';
      $('subJobs').innerHTML = g.jobs.slice(0, 60).map(s => {
        const cls = s.status === 'done' ? 'done' :
                    s.status === 'error' ? 'error' :
                    ['downloading','starting'].includes(s.status) ? 'active' : '';
        return '<div class="sub-job ' + cls + '">' +
          '<div class="sub-dot ' + s.status + '"></div>' +
          '<div class="sub-name">' + esc(s.title || '...') + '</div>' +
          '<div class="sub-pct">' + (s.progress||0) + '%</div>' +
        '</div>';
      }).join('');
      if (g.zip_ready) {
        clearInterval(interval);
        $('batchBtn').disabled = false;
        $('groupDlLink').href = g.download_url;
        $('groupDlInfo').textContent = '(' + g.done + ' file &middot; ' + (g.size_human||'') + ')';
        $('groupDlBox').style.display = 'block';
        setTimeout(() => {
          const a = document.createElement('a');
          a.href = g.download_url;
          document.body.appendChild(a); a.click(); document.body.removeChild(a);
        }, 500);
        loadHistory();
      }
    } catch (e) { clearInterval(interval); }
  }, 1000);
}

async function loadHistory() {
  try {
    const hist = await api('/api/history');
    if (!hist.length) {
      $('histList').innerHTML = '<div class="empty"><p>Ende bosh</p></div>';
      return;
    }
    $('histList').innerHTML = hist.slice(0, 100).map(x =>
      '<div class="hist">' +
        '<div class="hist-thumb">' + (x.thumbnail ? '<img src="' + esc(x.thumbnail) + '" alt="" loading="lazy">' : '') + '</div>' +
        '<div><div class="hist-name">' + esc(x.title || 'Download') + '</div><div class="hist-meta">' + esc(x.time || '') + ' &middot; ' + esc(x.format || '') + '</div></div>' +
        '<div class="hist-size">' + esc(x.size_human || '') + '</div>' +
        '<button class="hist-del" onclick="deleteHist(\'' + x.id + '\')">&times;</button>' +
      '</div>'
    ).join('');
  } catch (e) {}
}

window.deleteHist = async function(hid) {
  try { await api('/api/history/' + hid, 'DELETE'); loadHistory(); } catch (e) {}
};

$('clearHistBtn').onclick = async () => {
  if (!confirm('Fshi gjithe historikun?')) return;
  try { await api('/api/history/clear', 'POST', {}); loadHistory(); } catch (e) {}
};

async function loadHealth() {
  try {
    const h = await api('/api/health');
    $('statActive').textContent = h.jobs_active || 0;
  } catch (e) {}
}

$('artistModal').addEventListener('click', e => {
  if (e.target === $('artistModal')) closeArtistModal();
});

setInterval(loadHealth, 3000);
loadHealth();
updateBatchInfo();
</script>
</body>
</html>"""


if __name__ == "__main__":
    print("=" * 60)
    print("  FIXOTOOLS - LITE Edition")
    print(f"  Paralel: {PARALLEL_JOBS}x  |  Max batch: {MAX_BATCH}")
    print(f"  -> http://127.0.0.1:5000")
    print("=" * 60)
    if not ffmpeg_ok():
        print("  WARNING: FFmpeg jo i instaluar!")
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), threaded=True)
