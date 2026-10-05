"""
Finds saved videos that are no longer on YouTube, and uploads your copy
to the Internet Archive (archive.org) when you choose to.
"""
import html
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
        auto_upload = cfg.get("ia_mode") == "Upload automatically"
    if auto_upload and found:
        for vid in found:
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


def _metadata(vid, e):
    d = e.get("upload_date") or ""
    date = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else ""
    channel = e.get("channel") or "Unknown"
    url = e.get("url") or f"https://www.youtube.com/watch?v={vid}"
    desc = html.escape(e.get("description") or "").replace("\n", "<br>")
    intro = (f"Originally published on YouTube by {html.escape(channel)}"
             f"{' on ' + date if date else ''}. "
             f"No longer available on YouTube ({html.escape(e.get('reason') or 'removed')}).<br>"
             f"Original URL: {url}")
    audio = bool(e.get("audio_only"))
    md = {
        "title": e.get("title") or vid,
        "mediatype": "audio" if audio else "movies",
        "collection": "opensource_audio" if audio else "opensource_movies",
        "creator": channel,
        "description": intro + (f"<br><br>Original description:<br>{desc}" if desc else ""),
        "subject": ["youtube", channel],
        "originalurl": url,
        "source": url,
        "youtube-id": vid,
    }
    if date:
        md["date"] = date
    return md


def upload_to_ia(vid, cfg, log):
    """Uploads the saved copy of one video. Returns True on success."""
    try:
        import internetarchive as ia
    except ImportError:
        log('ERROR: the internetarchive package is missing. Run: pip install -U internetarchive')
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
        title = e.get("title", vid)
        try:
            if ia.get_item(ident).exists:
                update_entry(vid, ia_status="uploaded", ia_identifier=ident)
                log(f"Already on Internet Archive: {title}  {ia_url(vid)}")
                return True
        except Exception:
            pass  # network hiccup; the upload itself will tell us

        rel = e.get("file") or ""
        path = Path(cfg["dest"]).expanduser() / rel
        temp = None
        if not rel or not path.exists():
            temp = _fetch_from_cloud(cfg, rel, log) if rel else None
            if not temp:
                update_entry(vid, ia_status="failed", ia_error="Saved file not found")
                log(f"ERROR: can't find the saved file for {title}.")
                return False
            path = temp

        update_entry(vid, ia_status="uploading", ia_error="")
        log(f"Uploading to Internet Archive: {title} ({path.stat().st_size / 1e6:.0f} MB)...")
        try:
            responses = ia.upload(ident, files=[str(path)], metadata=_metadata(vid, e),
                                  access_key=ak, secret_key=sk, retries=5, retries_sleep=30,
                                  verbose=False)
            ok = bool(responses) and all(getattr(r, "status_code", 0) == 200 for r in responses)
        except Exception as ex:
            ok, err = False, str(ex)[:300]
        else:
            err = "" if ok else f"Archive.org returned {[getattr(r, 'status_code', '?') for r in responses]}"
        finally:
            if temp:
                shutil.rmtree(temp.parent, ignore_errors=True)

        if ok:
            update_entry(vid, ia_status="uploaded", ia_identifier=ident,
                         ia_uploaded_at=datetime.now().isoformat(timespec="seconds"))
            log(f"Uploaded. It can take an hour or so to appear: {ia_url(vid)}")
        else:
            update_entry(vid, ia_status="failed", ia_error=err)
            log(f"ERROR: upload failed for {title}: {err}")
        return ok
