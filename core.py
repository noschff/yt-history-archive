#!/usr/bin/env python3
"""
YouTube History Archiver
Saves every YouTube video you watch to a folder on your computer,
and optionally uploads it to cloud storage with rclone.

Engine and settings. app.py is the window; run this file directly for a single headless pass.
Run with --once to do a single pass with your saved settings (for cron / Task Scheduler).
"""
import sys

if sys.version_info < (3, 10):
    sys.exit("YouTube History Archiver needs Python 3.10 or newer. This is Python "
             + sys.version.split()[0] + ". Run setup.bat (Windows) or install a newer Python 3.")

import json
import os
import re
import platform
import queue
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

APP_NAME = "YouTube History Archiver"
CONFIG_DIR = Path.home() / ".yt_history_archiver"
CONFIG_FILE = CONFIG_DIR / "config.json"
ARCHIVE_FILE = CONFIG_DIR / "archive.txt"   # list of videos already saved
LOG_FILE = CONFIG_DIR / "archiver.log"
LIBRARY_FILE = CONFIG_DIR / "library.json"  # details of every saved video
STATE_FILE = CONFIG_DIR / "state.json"
VIDEO_EXT = {".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".opus", ".mov"}

DEFAULTS = {
    "browser": "firefox" if platform.system() == "Windows" else "chrome",
    "cookies_file": "",
    "dest": str(Path.home() / "YouTubeArchive"),
    "quality": "1080",
    "audio_only": False,
    "min_duration": 0,
    "check_count": 50,
    "check_all": True,
    "pace": "Normal",
    "speed_limit_mb": 0,
    "max_per_check": 0,
    "backoff": True,
    "backoff_hours": 3,
    "interval_min": 30,
    "by_channel": True,
    "rclone_remote": "",
    "rclone_mode": "copy",
    "ia_access": "",
    "ia_secret": "",
    "ia_mode": "Review first",
    "removed_check_daily": True,
}
BROWSERS = ["chrome", "firefox", "edge", "brave", "safari", "chromium", "opera", "vivaldi"]
# pace presets: (min wait between videos, max wait, wait between page requests, removal-check wait)
PACES = {
    "Gentle": (10, 25, 1.5, 1.5),
    "Normal": (2, 6, 0.5, 0.4),
    "Fast":   (0, 0, 0, 0.1),
}
RATE_SIGNS = ("429", "too many requests", "not a bot", "rate-limit", "rate limit",
              "rate-limited", "unusual traffic")


def pace(cfg):
    return PACES.get(cfg.get("pace"), PACES["Normal"])


QUALITIES = ["best", "2160", "1440", "1080", "720", "480", "360"]


# ---------------------------------------------------------------- settings
def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text()))
    except Exception:
        pass
    # settings from older versions
    cfg["ia_mode"] = {"Ask me": "Review first", "Upload automatically": "Skip review"}.get(cfg["ia_mode"], cfg["ia_mode"])
    return cfg


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def archive_ids():
    try:
        return {ln.split()[-1] for ln in ARCHIVE_FILE.read_text().splitlines() if ln.strip()}
    except FileNotFoundError:
        return set()



# ---------------------------------------------------------------- library of saved videos
_lib_lock = threading.RLock()
ID_IN_NAME = re.compile(r"\[([A-Za-z0-9_-]{11})\]\.[A-Za-z0-9]+$")


def load_library():
    with _lib_lock:
        try:
            return json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}


def save_library(lib):
    with _lib_lock:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = LIBRARY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(lib, indent=1), encoding="utf-8")
        os.replace(tmp, LIBRARY_FILE)


def update_entry(vid, **fields):
    with _lib_lock:
        lib = load_library()
        lib.setdefault(vid, {}).update(fields)
        save_library(lib)
        return lib[vid]


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(**fields):
    st = load_state()
    st.update(fields)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st))


def reindex(dest):
    """Add videos already in the save folder (e.g. from before this version) to the library."""
    dest = Path(dest).expanduser()
    if not dest.exists():
        return 0
    added = 0
    with _lib_lock:
        lib = load_library()
        for root, _, names in os.walk(dest):
            for n in names:
                p = Path(root) / n
                m = ID_IN_NAME.search(n)
                if not m or p.suffix.lower() not in VIDEO_EXT:
                    continue
                vid = m.group(1)
                rel = p.relative_to(dest).as_posix()
                e = lib.get(vid)
                if e is None:
                    title = p.stem[:p.stem.rfind("[")].strip()
                    if re.match(r"^\d{8} - ", title):
                        title = title[11:]
                    lib[vid] = {"title": title, "channel": p.parent.name if p.parent != dest else "",
                                "file": rel, "url": f"https://www.youtube.com/watch?v={vid}",
                                "upload_date": n[:8] if n[:8].isdigit() else "",
                                "audio_only": p.suffix.lower() in {".m4a", ".mp3", ".opus"}}
                    added += 1
                elif e.get("file") != rel:
                    e["file"] = rel
        save_library(lib)
    return added


def make_library_pp(dest, audio_only):
    """yt-dlp post-processor that records each finished download in the library."""
    from yt_dlp.postprocessor.common import PostProcessor
    dest = Path(dest).expanduser()

    class LibraryPP(PostProcessor):
        def run(self, info):
            try:
                fp = Path(info.get("filepath") or "")
                rel = fp.relative_to(dest).as_posix() if fp.is_absolute() else fp.as_posix()
                update_entry(info["id"],
                             title=info.get("title") or info["id"],
                             channel=info.get("channel") or info.get("uploader") or "",
                             channel_url=info.get("channel_url") or "",
                             upload_date=info.get("upload_date") or "",
                             description=(info.get("description") or "")[:20000],
                             url=info.get("webpage_url") or f"https://www.youtube.com/watch?v={info['id']}",
                             duration=info.get("duration"),
                             file=rel, audio_only=audio_only,
                             saved_at=datetime.now().isoformat(timespec="seconds"),
                             status="available")
            except Exception:
                pass
            return [], info

    return LibraryPP()


# ---------------------------------------------------------------- core engine
class _YDLLogger:
    def __init__(self, log, on_rate_limit=None):
        self.log = log
        self.on_rate_limit = on_rate_limit

    def _check_rate(self, msg):
        if self.on_rate_limit and any(s in msg.lower() for s in RATE_SIGNS):
            self.on_rate_limit()

    def debug(self, msg):
        if msg.startswith("[download] Destination:") or "has already been recorded" in msg:
            return  # too noisy
        if msg.startswith("[youtube] Extracting URL") or msg.startswith("[download] Downloading item"):
            self.log(msg)

    def info(self, msg):
        pass

    def warning(self, msg):
        self._check_rate(msg)
        if "nsig" in msg or "JavaScript runtime" in msg or "js runtime" in msg.lower():
            self.log("WARNING: " + msg + "  (install Deno - see README)")
        else:
            self.log("WARNING: " + msg)

    def error(self, msg):
        self._check_rate(msg)
        self.log("ERROR: " + msg)


class Archiver:
    def __init__(self, log):
        self.log = log
        self.lock = threading.Lock()
        self.rate_limited = False

    def _flag_rate_limit(self):
        if not self.rate_limited:
            self.rate_limited = True
            self.log("YouTube is slowing down requests. Stopping this check early to avoid a longer block.")

    def _base_opts(self, cfg):
        opts = {"quiet": True, "no_warnings": False, "ignoreerrors": True,
                "logger": _YDLLogger(self.log, self._flag_rate_limit)}
        if cfg.get("cookies_file"):
            opts["cookiefile"] = cfg["cookies_file"]
        else:
            opts["cookiesfrombrowser"] = (cfg["browser"],)
        return opts

    def preflight(self):
        try:
            import yt_dlp  # noqa: F401
        except ImportError:
            self.log("ERROR: yt-dlp is not installed. Run: pip install -U \"yt-dlp[default]\"")
            return False
        if not shutil.which("ffmpeg"):
            self.log("Note: ffmpeg not found - quality is limited to pre-merged formats (usually 720p max).")
        if not shutil.which("deno"):
            self.log("Note: Deno not found - YouTube downloads may fail or be limited. See README.")
        return True

    def run_once(self, cfg, stop_event):
        if not self.lock.acquire(blocking=False):
            self.log("A check is already running.")
            return
        try:
            self._run(cfg, stop_event)
        except Exception as e:
            self.log(f"ERROR: {e}")
        finally:
            self.lock.release()

    def _run(self, cfg, stop_event):
        if not self.preflight():
            return
        import yt_dlp
        self.rate_limited = False

        dest = Path(cfg["dest"]).expanduser()
        dest.mkdir(parents=True, exist_ok=True)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        has_ffmpeg = bool(shutil.which("ffmpeg"))
        before = len(archive_ids())

        q = cfg["quality"]
        if cfg["audio_only"]:
            fmt = "bestaudio/best"
        elif q == "best":
            fmt = "bv*+ba/b" if has_ffmpeg else "b"
        else:
            fmt = (f"bv*[height<={q}]+ba/b[height<={q}]/b" if has_ffmpeg
                   else f"b[height<={q}]/b")

        tmpl = "%(upload_date)s - %(title).150B [%(id)s].%(ext)s"
        if cfg["by_channel"]:
            tmpl = "%(channel,uploader|Unknown)s/" + tmpl

        min_dur = int(cfg.get("min_duration") or 0)

        def match_filter(info, *, incomplete=False):
            if stop_event.is_set():
                return "stopped by user"
            if self.rate_limited:
                return "paused: YouTube rate limit"
            dur = info.get("duration")
            if min_dur and dur is not None and dur < min_dur:
                return f"shorter than {min_dur}s"
            if info.get("is_live") or info.get("live_status") == "is_upcoming":
                return "live or upcoming"
            return None

        def hook(d):
            if d.get("status") == "finished":
                name = Path(d.get("filename", "")).name
                self.log(f"Saved: {name}")

        opts = self._base_opts(cfg)
        opts.update({
            "download_archive": str(ARCHIVE_FILE),
            "format": fmt,
            "outtmpl": str(dest / tmpl),
            "match_filter": match_filter,
            "progress_hooks": [hook],
            "noprogress": True,
            "windowsfilenames": True,
        })
        smin, smax, sreq, _ = pace(cfg)
        if smax:
            opts["sleep_interval"], opts["max_sleep_interval"] = smin, smax
        if sreq:
            opts["sleep_interval_requests"] = sreq
        if float(cfg.get("speed_limit_mb") or 0) > 0:
            opts["ratelimit"] = int(float(cfg["speed_limit_mb"]) * 1024 * 1024)
        if int(cfg.get("max_per_check") or 0) > 0:
            opts["max_downloads"] = int(cfg["max_per_check"])
        if not cfg.get("check_all"):
            opts["playlistend"] = int(cfg["check_count"])
        if has_ffmpeg and not cfg["audio_only"]:
            opts["merge_output_format"] = "mp4"
        if has_ffmpeg and cfg["audio_only"]:
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}]

        if cfg.get("check_all"):
            self.log("Checking your whole watch history. Videos already saved are skipped quickly.")
        else:
            self.log(f"Checking your last {cfg['check_count']} watched videos...")
        reindex(dest)
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.add_post_processor(make_library_pp(dest, cfg["audio_only"]), when="after_move")
            try:
                ydl.download([":ythistory"])
            except yt_dlp.utils.MaxDownloadsReached:
                self.log(f"Reached the limit of {cfg['max_per_check']} videos for this check. "
                         "The rest will be saved on later checks.")

        new = len(archive_ids()) - before
        self.log(f"Done. {new} new video(s) saved." if new else "Done. Nothing new to save.")

        if cfg.get("rclone_remote") and not stop_event.is_set():
            self.upload(cfg, dest)

    def upload(self, cfg, dest):
        if not shutil.which("rclone"):
            self.log("ERROR: rclone not found. Install it or clear the cloud destination.")
            return
        mode = cfg.get("rclone_mode", "copy")
        cmd = ["rclone", mode, str(dest), cfg["rclone_remote"],
               "--exclude", "*.part", "--exclude", "*.ytdl", "--exclude", "*.temp.*"]
        if mode == "move":
            cmd.append("--delete-empty-src-dirs")
        self.log(f"Uploading to {cfg['rclone_remote']} ({mode})...")
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            self.log("Upload finished.")
        else:
            tail = (r.stderr or r.stdout).strip().splitlines()[-3:]
            self.log("ERROR: upload failed: " + " | ".join(tail))

    def mark_history_seen(self, cfg):
        """Record current history as already saved so only future views download."""
        if not self.preflight():
            return
        import yt_dlp
        opts = self._base_opts(cfg)
        opts["extract_flat"] = "in_playlist"
        if not cfg.get("check_all"):
            opts["playlistend"] = int(cfg["check_count"])
        self.log("Reading your watch history...")
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(":ythistory", download=False) or {}
        ids = [e["id"] for e in (info.get("entries") or []) if e and e.get("id")]
        existing = archive_ids()
        fresh = [i for i in ids if i not in existing]
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with ARCHIVE_FILE.open("a") as f:
            for vid in fresh:
                f.write(f"youtube {vid}\n")
        self.log(f"Skipped {len(fresh)} video(s) already in your history. "
                 "Only videos you watch from now on will be saved.")


# ---------------------------------------------------------------- headless entry
def headless(argv):
    if "--check-removed" in argv:
        import lostmedia
        # No window here to review uploads, so this only uploads if "Skip review" is chosen in Settings.
        lostmedia.check_removed(load_config(), print)
        return
    if "--skip-history" in argv:
        Archiver(print).mark_history_seen(load_config())
    else:
        Archiver(lambda m: print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)) \
            .run_once(load_config(), threading.Event())


if __name__ == "__main__":
    headless(sys.argv)
