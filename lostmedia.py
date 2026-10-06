"""
Finds saved videos that are no longer on YouTube, and uploads your copy
to the Internet Archive (archive.org) when you choose to.
"""
import html
import json
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from core import load_library, update_entry, reindex, save_state, pace

GONE = ("removed", "private")
UA = "Mozilla/5.0 (YouTube History Archiver)"
_upload_lock = threading.Lock()


# ---------------------------------------------------------------- availability
def _oembed(vid):
    """Fast check. Returns HTTP status code, or None on network trouble."""
    target = urllib.parse.quote(f"https://www.youtube.com/watch?v={vid}", safe="")
    req = urllib.request.Request(f"https://www.youtube.com/oembed?format=json&url={target}",
                                 headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


class _Quiet:
    def debug(self, m): pass
    def info(self, m): pass
    def warning(self, m): pass
    def error(self, m): pass


def video_status(vid):
    """Returns (status, reason). status is available, removed, private or unknown."""
    code = _oembed(vid)
    if code == 200:
        return "available", ""
    if code is None:
        return "unknown", "Network error"

    # oEmbed can also fail for videos that exist but block embedding, so confirm with yt-dlp.
    import yt_dlp
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                               "logger": _Quiet()}) as ydl:
            ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False, process=False)
        return "available", ""
    except Exception as e:
        msg = str(e)
        low = msg.lower()
        reason = msg.split(":", 2)[-1].strip().splitlines()[0][:160] if ":" in msg else msg[:160]
        if "private video" in low:
            return "private", "Made private"
        if "terminated" in low:
            return "removed", "Channel terminated"
        if "copyright" in low:
            return "removed", "Removed for copyright"
        if "community guidelines" in low or "violating" in low:
            return "removed", "Removed for policy violation"
        if "removed by the uploader" in low or "deleted" in low:
            return "removed", "Deleted by uploader"
        if any(k in low for k in ("confirm you’re not a bot", "confirm you're not a bot", "too many requests", "429")):
            return "unknown", "YouTube is rate-limiting checks. Try again later."
        if any(k in low for k in ("confirm your age", "members-only", "join this channel", "not available in your country")):
            return "available", ""
        if any(k in low for k in ("video unavailable", "no longer available", "does not exist", "has been removed")):
            return "removed", reason or "Removed"
        return "unknown", reason or "Couldn't tell"


def check_removed(cfg, log, progress=None, stop=None, auto_upload=None):
    """Checks every saved video that isn't already known to be gone. Returns newly-gone ids."""
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        log("ERROR: yt-dlp is not installed.")
        return []
    reindex(cfg["dest"])
    lib = load_library()
    ids = [v for v, e in lib.items() if e.get("status") not in GONE]
    log(f"Checking {len(ids)} saved video(s) for removals...")
    found, unknown = [], 0
    for i, vid in enumerate(ids, 1):
        if stop is not None and stop.is_set():
            log("Removal check stopped.")
            break
        status, reason = video_status(vid)
        update_entry(vid, status=status, reason=reason,
                     checked_at=datetime.now().isoformat(timespec="seconds"))
        if status in GONE:
            found.append(vid)
            log(f"No longer on YouTube: {lib[vid].get('title', vid)} ({reason})")
        elif status == "unknown":
            unknown += 1
            if "rate-limiting" in reason:
                log(reason)
                break
        if progress:
            progress(i, len(ids))
        time.sleep(pace(cfg)[3])
    save_state(last_removed_check=datetime.now().isoformat(timespec="seconds"))
    msg = f"Removal check done. {len(found)} newly removed."
    if unknown:
        msg += f" {unknown} couldn't be checked."
    log(msg)

    if auto_upload is None:
        auto_upload = ia_auto_upload(cfg)
    if auto_upload and found:
        for vid in found:
            title = load_library().get(vid, {}).get("title", vid)
            copies = find_existing_copies(vid)
            others = save_copies(vid, copies)
            if copies.get("exact"):
                log(f"Already on Internet Archive: {title}  {ia_url(vid)}")
            elif others:
                # Unattended mode: don't create a duplicate. The user can still upload from the Removed page.
                log(f"Skipped upload, already archived by someone else: {title}  {others[0]['url']}")
            else:
                upload_to_ia(vid, cfg, log)
    return found


# ---------------------------------------------------------------- Internet Archive
def ia_identifier(vid):
    return f"youtube-{vid}"


def ia_url(vid):
    return f"https://archive.org/details/{ia_identifier(vid)}"


def _fetch_from_cloud(cfg, rel, log):
    remote = (cfg.get("rclone_remote") or "").rstrip("/")
    if not remote or not shutil.which("rclone"):
        return None
    tmpdir = Path(tempfile.mkdtemp(prefix="yt-ia-"))
    target = tmpdir / Path(rel).name
    log(f"Local copy not found, downloading it back from {remote}...")
    r = subprocess.run(["rclone", "copyto", f"{remote}/{rel}", str(target)], capture_output=True, text=True)
    return target if r.returncode == 0 and target.exists() else None


def ia_auto_upload(cfg):
    """True when the user chose to upload without reviewing each video."""
    return cfg.get("ia_mode") in ("Skip review", "Upload automatically")


def draft_metadata(vid, e):
    """Plain-text metadata the user can review and edit before anything is published."""
    if e.get("ia_draft"):
        return dict(e["ia_draft"])
    d = e.get("upload_date") or ""
    date = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else ""
    channel = e.get("channel") or "Unknown"
    url = e.get("url") or f"https://www.youtube.com/watch?v={vid}"
    intro = (f"Originally published on YouTube by {channel}{' on ' + date if date else ''}. "
             f"No longer available on YouTube ({e.get('reason') or 'removed'}).\n"
             f"Original URL: {url}")
    desc = (e.get("description") or "").strip()
    audio = bool(e.get("audio_only"))
    return {
        "title": e.get("title") or vid,
        "creator": channel,
        "date": date,
        "description": intro + (f"\n\nOriginal description:\n{desc}" if desc else ""),
        "subject": ["youtube", channel],
        "mediatype": "audio" if audio else "movies",
        "collection": "opensource_audio" if audio else "opensource_movies",
        "originalurl": url,
    }


def to_ia_metadata(vid, draft):
    """Turns the reviewed draft into the fields archive.org expects."""
    md = {
        "title": draft.get("title") or vid,
        "mediatype": draft.get("mediatype", "movies"),
        "collection": draft.get("collection", "opensource_movies"),
        "creator": draft.get("creator") or "Unknown",
        "description": html.escape(draft.get("description") or "").replace("\n", "<br>"),
        "subject": [t for t in draft.get("subject", []) if t],
        "originalurl": draft.get("originalurl", ""),
        "source": draft.get("originalurl", ""),
        "youtube-id": vid,
    }
    if draft.get("date"):
        md["date"] = draft["date"]
    return md


def _metadata(vid, e):
    return to_ia_metadata(vid, draft_metadata(vid, e))


def locate_file(vid, cfg, log):
    """Returns (path, is_temporary) for the saved copy, fetching it back from the cloud if needed."""
    e = load_library().get(vid) or {}
    rel = e.get("file") or ""
    path = Path(cfg["dest"]).expanduser() / rel
    if rel and path.exists():
        return path, False
    temp = _fetch_from_cloud(cfg, rel, log) if rel else None
    return (temp, True) if temp else (None, False)


def discard_temp(path):
    if path:
        shutil.rmtree(Path(path).parent, ignore_errors=True)


def already_on_ia(vid):
    """True if archive.org already has this video. None if it couldn't be checked."""
    try:
        import internetarchive as ia
        return bool(ia.get_item(ia_identifier(vid)).exists)
    except Exception:
        return None


def _get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "null")


def find_existing_copies(vid):
    """Looks for copies of a video already archived anywhere on archive.org.

    Returns {"exact": True/False/None, "items": [{"identifier", "title", "url"}],
             "wayback": url or None, "errors": [...]}. None means it couldn't be checked."""
    out = {"exact": None, "items": [], "wayback": None, "errors": []}

    # 1. The standard address most tools (and this app) upload to
    out["exact"] = already_on_ia(vid)

    # 2. Search every item's metadata (title, description, links, tags) for the video ID
    q = f'"{vid}" OR youtube-id:"{vid}" OR originalurl:"{vid}"'
    params = urllib.parse.urlencode([("q", q), ("fl[]", "identifier"), ("fl[]", "title"),
                                     ("rows", "25"), ("output", "json")])
    try:
        data = _get_json(f"https://archive.org/advancedsearch.php?{params}")
        for d in (data or {}).get("response", {}).get("docs", []):
            ident = d.get("identifier")
            if not ident:
                continue
            title = d.get("title")
            title = title[0] if isinstance(title, list) and title else (title or ident)
            out["items"].append({"identifier": ident, "title": title,
                                 "url": f"https://archive.org/details/{ident}"})
        if any(i["identifier"] == ia_identifier(vid) for i in out["items"]):
            out["exact"] = True
    except Exception:
        out["errors"].append("archive.org search")

    # 3. Was the YouTube page saved in the Wayback Machine?
    target = f"youtube.com/watch?v={vid}"
    params = urllib.parse.urlencode({"url": target, "output": "json", "limit": "1",
                                     "filter": "statuscode:200", "fl": "timestamp"})
    try:
        rows = _get_json(f"https://web.archive.org/cdx/search/cdx?{params}") or []
        if len(rows) > 1 and rows[1]:
            out["wayback"] = f"https://web.archive.org/web/{rows[1][0]}/https://www.youtube.com/watch?v={vid}"
    except Exception:
        out["errors"].append("Wayback Machine")
    return out


def save_copies(vid, found):
    """Remembers what was found so the Removed page can show it."""
    others = [i for i in found["items"] if i["identifier"] != ia_identifier(vid)]
    update_entry(vid, ia_found=others[:10], wayback=found.get("wayback"),
                 copies_checked_at=datetime.now().isoformat(timespec="seconds"))
    if found.get("exact"):
        update_entry(vid, ia_status="uploaded", ia_identifier=ia_identifier(vid))
    return others


def preview_frame(path, duration=None, width=480):
    """Grabs one still from the video for the review window. Returns a temp .jpg path or None."""
    if not shutil.which("ffmpeg") or not path or Path(path).suffix.lower() in {".m4a", ".mp3", ".opus"}:
        return None
    at = max(1, int((duration or 30) * 0.15))
    out = Path(tempfile.mkdtemp(prefix="yt-frame-")) / "frame.jpg"
    for seek in (at, 0):
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(seek), "-i", str(path),
                            "-frames:v", "1", "-vf", f"scale={width}:-2", str(out)],
                           capture_output=True)
        if r.returncode == 0 and out.exists():
            return out
    return None


def upload_to_ia(vid, cfg, log, draft=None, file_path=None):
    """Uploads the saved copy of one video. Returns True on success.

    draft is the reviewed metadata from the preview window. file_path is a copy the
    caller already located (the caller then cleans up any temporary copy)."""
    try:
        import internetarchive as ia
    except ImportError:
        log('ERROR: the internetarchive package is missing. Run setup.bat again or: pip install -U internetarchive')
        return False
    ak, sk = (cfg.get("ia_access") or "").strip(), (cfg.get("ia_secret") or "").strip()
    if not ak or not sk:
        log("ERROR: add your Internet Archive keys in Settings first.")
        return False

    with _upload_lock:
        e = load_library().get(vid)
        if not e:
            log(f"ERROR: {vid} isn't in your library.")
            return False
        ident = ia_identifier(vid)
        draft = draft or draft_metadata(vid, e)
        title = draft.get("title") or vid

        if already_on_ia(vid):
            update_entry(vid, ia_status="uploaded", ia_identifier=ident)
            log(f"Already on Internet Archive: {title}  {ia_url(vid)}")
            return True

        temp = None
        path = Path(file_path) if file_path else None
        if path is None:
            path, is_temp = locate_file(vid, cfg, log)
            temp = path if is_temp else None
        if not path or not path.exists():
            update_entry(vid, ia_status="failed", ia_error="Saved file not found")
            log(f"ERROR: can't find the saved file for {title}.")
            return False

        update_entry(vid, ia_status="uploading", ia_error="")
        log(f"Uploading to Internet Archive: {title} ({path.stat().st_size / 1e6:.0f} MB)...")
        try:
            responses = ia.upload(ident, files=[str(path)], metadata=to_ia_metadata(vid, draft),
                                  access_key=ak, secret_key=sk, retries=5, retries_sleep=30,
                                  verbose=False)
            ok = bool(responses) and all(getattr(r, "status_code", 0) == 200 for r in responses)
            err = "" if ok else f"Archive.org returned {[getattr(r, 'status_code', '?') for r in responses]}"
        except Exception as ex:
            ok, err = False, str(ex)[:300]
        finally:
            if temp:
                discard_temp(temp)

        if ok:
            update_entry(vid, ia_status="uploaded", ia_identifier=ident,
                         ia_uploaded_at=datetime.now().isoformat(timespec="seconds"))
            log(f"Uploaded. It can take an hour or so to appear: {ia_url(vid)}")
        else:
            update_entry(vid, ia_status="failed", ia_error=err)
            log(f"ERROR: upload failed for {title}: {err}")
        return ok
