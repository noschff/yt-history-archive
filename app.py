#!/usr/bin/env python3
"""YouTube History Archiver - desktop window.

Run with --once for a single headless pass (cron / Task Scheduler).
"""
import os
import platform
import queue
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402
from core import (Archiver, load_config, save_config, archive_ids, load_library, load_state,
                  BROWSERS, QUALITIES, CONFIG_DIR, LOG_FILE, VIDEO_EXT)

if "--once" in sys.argv or "--skip-history" in sys.argv:
    core.headless(sys.argv)
    sys.exit(0)

try:
    import tkinter as tk
    import customtkinter as ctk
except ImportError:
    print('customtkinter is missing. Run:  pip install -U customtkinter "yt-dlp[default]"')
    sys.exit(1)

import lostmedia  # noqa: E402

# ---------------------------------------------------------------- look
# (light, dark) pairs. Cool blue-slate base; the red is a "recording" light,
# used only for the one thing that matters: whether it's capturing.
C = {
    "bg":       ("#EEF1F5", "#141A24"),
    "side":     ("#E2E7EE", "#18202C"),
    "surface":  ("#FFFFFF", "#1F2836"),
    "line":     ("#D3DAE3", "#2C3646"),
    "text":     ("#141A24", "#E9EDF2"),
    "muted":    ("#5B6677", "#8794A8"),
    "rec":      ("#D3363C", "#EF4B50"),
    "rec_hov":  ("#B42C31", "#D33D42"),
    "amber":    ("#B7791F", "#E3A33B"),
    "idle":     ("#9AA5B4", "#566276"),
    "navsel":   ("#D3DAE3", "#243042"),
}
SYS = platform.system()
FAMILY = {"Windows": "Segoe UI", "Darwin": "Helvetica Neue"}.get(SYS)


def font(size, weight="normal"):
    return ctk.CTkFont(family=FAMILY, size=size, weight=weight) if FAMILY else ctk.CTkFont(size=size, weight=weight)


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024




# ---------------------------------------------------------------- app
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("system")
        self.title("YouTube History Archiver")
        self.geometry("980x660")
        self.minsize(860, 580)
        self.configure(fg_color=C["bg"])

        self.cfg = load_config()
        self.msgs = queue.Queue()
        self.engine = Archiver(self.log)
        self.stop_event = threading.Event()
        self.watching = False
        self.busy = False
        self.busy_label = ""
        self.last_check = None
        self.next_check = None
        self.stats = {"count": len(archive_ids()), "size": None, "recent": []}
        self._stats_running = False
        self._blink = False
        self.rm_busy = False
        self.rm_progress = (0, 0)
        self.backoff_until = None
        self.gone_total = 0
        self.gone_pending = 0

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._sidebar()
        self.pages = {"Home": self._home(), "Removed": self._removed(), "Settings": self._settings(),
                      "Activity": self._activity()}
        self.show("Home")

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(150, self._drain_log)
        self.after(400, self._tick)
        self.refresh_stats()
        self.log(f"Ready. Settings and log are in {CONFIG_DIR}")

    # ------------------------------------------------ sidebar
    def _sidebar(self):
        side = ctk.CTkFrame(self, fg_color=C["side"], corner_radius=0, width=210)
        side.grid(row=0, column=0, sticky="nsw")
        side.grid_propagate(False)

        ctk.CTkLabel(side, text="History\nArchiver", justify="left", font=font(22, "bold"),
                     text_color=C["text"]).pack(anchor="w", padx=22, pady=(26, 4))
        ctk.CTkLabel(side, text="Every video you watch, kept.", font=font(12),
                     text_color=C["muted"]).pack(anchor="w", padx=22, pady=(0, 26))

        self.nav = {}
        for name in ("Home", "Removed", "Settings", "Activity"):
            b = ctk.CTkButton(side, text=name, anchor="w", height=38, corner_radius=8, font=font(14),
                              fg_color="transparent", hover_color=C["navsel"], text_color=C["text"],
                              command=lambda n=name: self.show(n))
            b.pack(fill="x", padx=12, pady=2)
            self.nav[name] = b

        foot = ctk.CTkFrame(side, fg_color="transparent")
        foot.pack(side="bottom", fill="x", padx=22, pady=20)
        self.side_dot = ctk.CTkLabel(foot, text="●", font=font(14), text_color=C["idle"])
        self.side_dot.pack(side="left")
        self.side_state = ctk.CTkLabel(foot, text="Paused", font=font(12), text_color=C["muted"])
        self.side_state.pack(side="left", padx=6)

    def show(self, name):
        for n, page in self.pages.items():
            page.grid_forget()
            self.nav[n].configure(fg_color="transparent", font=font(14))
        self.pages[name].grid(row=0, column=1, sticky="nsew", padx=34, pady=28)
        self.nav[name].configure(fg_color=C["navsel"], font=font(14, "bold"))

    # ------------------------------------------------ home
    def _home(self):
        page = ctk.CTkFrame(self, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(4, weight=1)

        # status hero: the record light
        hero = ctk.CTkFrame(page, fg_color="transparent")
        hero.grid(row=0, column=0, sticky="ew")
        self.dot = ctk.CTkLabel(hero, text="●", font=font(30), text_color=C["idle"])
        self.dot.pack(side="left", padx=(0, 12))
        txt = ctk.CTkFrame(hero, fg_color="transparent")
        txt.pack(side="left")
        self.status_big = ctk.CTkLabel(txt, text="Paused", font=font(30, "bold"), text_color=C["text"])
        self.status_big.pack(anchor="w")
        self.status_small = ctk.CTkLabel(txt, text="Start watching to save new videos as you view them.",
                                         font=font(13), text_color=C["muted"])
        self.status_small.pack(anchor="w")

        btns = ctk.CTkFrame(page, fg_color="transparent")
        btns.grid(row=1, column=0, sticky="w", pady=(22, 26))
        self.toggle_btn = ctk.CTkButton(btns, text="Start watching", height=44, width=180, corner_radius=22,
                                        font=font(15, "bold"), fg_color=C["rec"], hover_color=C["rec_hov"],
                                        text_color="#FFFFFF", command=self.toggle)
        self.toggle_btn.pack(side="left")
        self.check_btn = self._ghost(btns, "Check now", self.check_now)
        self.check_btn.pack(side="left", padx=10)
        self._ghost(btns, "Open folder", self.open_folder).pack(side="left")

        # banner shown when saved videos have disappeared from YouTube
        self.banner = ctk.CTkFrame(page, fg_color=C["surface"], corner_radius=14, border_width=1,
                                   border_color=C["rec"])
        self.banner.grid_columnconfigure(0, weight=1)
        self.banner_text = ctk.CTkLabel(self.banner, text="", font=font(14), text_color=C["text"], anchor="w")
        self.banner_text.grid(row=0, column=0, sticky="w", padx=20, pady=14)
        self._ghost_small(self.banner, "Review", lambda: self.show("Removed")).grid(row=0, column=1, padx=16)
        self._banner_shown = False

        # stats band: one surface, three columns with hairline dividers
        band = ctk.CTkFrame(page, fg_color=C["surface"], corner_radius=14, border_width=1, border_color=C["line"])
        band.grid(row=3, column=0, sticky="ew")
        self.stat_vals = {}
        for i, (key, label) in enumerate((("count", "Videos saved"), ("size", "Space used"), ("last", "Last check"))):
            band.grid_columnconfigure(i * 2, weight=1)
            cell = ctk.CTkFrame(band, fg_color="transparent")
            cell.grid(row=0, column=i * 2, sticky="w", padx=24, pady=18)
            v = ctk.CTkLabel(cell, text="–", font=font(26, "bold"), text_color=C["text"])
            v.pack(anchor="w")
            ctk.CTkLabel(cell, text=label, font=font(12), text_color=C["muted"]).pack(anchor="w")
            self.stat_vals[key] = v
            if i < 2:
                ctk.CTkFrame(band, width=1, fg_color=C["line"]).grid(row=0, column=i * 2 + 1, sticky="ns", pady=16)

        # recent saves
        rec = ctk.CTkFrame(page, fg_color="transparent")
        rec.grid(row=4, column=0, sticky="nsew", pady=(26, 0))
        rec.grid_columnconfigure(0, weight=1)
        rec.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(rec, text="Recently saved", font=font(16, "bold"), text_color=C["text"]).grid(row=0, column=0, sticky="w")
        self.recent_box = ctk.CTkScrollableFrame(rec, fg_color="transparent")
        self.recent_box.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self.recent_box.grid_columnconfigure(0, weight=1)
        return page

    def _ghost(self, parent, text, cmd):
        return ctk.CTkButton(parent, text=text, height=44, corner_radius=22, font=font(14), command=cmd,
                             fg_color="transparent", border_width=1, border_color=C["line"],
                             hover_color=C["navsel"], text_color=C["text"])

    def _render_recent(self):
        for w in self.recent_box.winfo_children():
            w.destroy()
        items = self.stats["recent"]
        if not items:
            ctk.CTkLabel(self.recent_box, text="Nothing yet. Videos appear here after they're saved.",
                         font=font(13), text_color=C["muted"]).grid(row=0, column=0, sticky="w", pady=6)
            return
        for i, (title, channel, when) in enumerate(items):
            row = ctk.CTkFrame(self.recent_box, fg_color="transparent")
            row.grid(row=i * 2, column=0, sticky="ew", pady=7)
            row.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(row, text=title, font=font(14), text_color=C["text"], anchor="w",
                         wraplength=520, justify="left").grid(row=0, column=0, sticky="w")
            ctk.CTkLabel(row, text=when, font=font(12), text_color=C["muted"]).grid(row=0, column=1, sticky="e", padx=(12, 4))
            if channel:
                ctk.CTkLabel(row, text=channel, font=font(12), text_color=C["muted"], anchor="w").grid(row=1, column=0, sticky="w")
            if i < len(items) - 1:
                ctk.CTkFrame(self.recent_box, height=1, fg_color=C["line"]).grid(row=i * 2 + 1, column=0, sticky="ew")

    # ------------------------------------------------ removed from YouTube
    def _removed(self):
        page = ctk.CTkFrame(self, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(3, weight=1)
        head = ctk.CTkFrame(page, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(head, text="Removed from YouTube", font=font(30, "bold"), text_color=C["text"]).pack(side="left")
        self.rm_btn = ctk.CTkButton(head, text="Check saved videos", height=38, corner_radius=19, font=font(14),
                                    fg_color="transparent", border_width=1, border_color=C["line"],
                                    hover_color=C["navsel"], text_color=C["text"], command=self.check_removed)
        self.rm_btn.pack(side="right")
        ctk.CTkLabel(page, text="Videos you saved that are no longer on YouTube. Your copy may be one of the few left.",
                     font=font(13), text_color=C["muted"]).grid(row=1, column=0, sticky="w", pady=(4, 0))
        prog = ctk.CTkFrame(page, fg_color="transparent")
        prog.grid(row=2, column=0, sticky="ew", pady=(14, 6))
        self.rm_bar = ctk.CTkProgressBar(prog, height=6, progress_color=C["rec"], fg_color=C["line"])
        self.rm_bar.set(0)
        self.rm_bar.pack(side="left", fill="x", expand=True)
        self.rm_note = ctk.CTkLabel(prog, text="", font=font(12), text_color=C["muted"], width=150, anchor="e")
        self.rm_note.pack(side="right", padx=(12, 0))
        self.rm_list = ctk.CTkScrollableFrame(page, fg_color="transparent")
        self.rm_list.grid(row=3, column=0, sticky="nsew", pady=(8, 0))
        self.rm_list.grid_columnconfigure(0, weight=1)
        return page

    def _render_removed(self):
        for w in self.rm_list.winfo_children():
            w.destroy()
        lib = load_library()
        gone = sorted(((v, e) for v, e in lib.items() if e.get("status") in lostmedia.GONE),
                      key=lambda x: x[1].get("checked_at", ""), reverse=True)
        if not gone:
            ctk.CTkLabel(self.rm_list, text="None found. Check your saved videos to look for removals.",
                         font=font(13), text_color=C["muted"]).grid(row=0, column=0, sticky="w", pady=6)
            return
        for i, (vid, e) in enumerate(gone):
            row = ctk.CTkFrame(self.rm_list, fg_color=C["surface"], corner_radius=12, border_width=1,
                               border_color=C["line"])
            row.grid(row=i, column=0, sticky="ew", pady=5, padx=(0, 6))
            row.grid_columnconfigure(0, weight=1)
            info = ctk.CTkFrame(row, fg_color="transparent")
            info.grid(row=0, column=0, sticky="w", padx=18, pady=12)
            ctk.CTkLabel(info, text=e.get("title", vid), font=font(14, "bold"), text_color=C["text"],
                         anchor="w", wraplength=440, justify="left").pack(anchor="w")
            sub = e.get("channel") or "Unknown channel"
            ctk.CTkLabel(info, text=sub, font=font(12), text_color=C["muted"], anchor="w").pack(anchor="w")
            ia_status = e.get("ia_status")
            note = e.get("reason") or "Removed"
            if ia_status == "failed" and e.get("ia_error"):
                note += f". Last upload failed: {e['ia_error'][:120]}"
            ctk.CTkLabel(info, text=note, font=font(12), anchor="w", wraplength=440, justify="left",
                         text_color=C["rec"] if ia_status == "failed" else C["muted"]).pack(anchor="w")

            acts = ctk.CTkFrame(row, fg_color="transparent")
            acts.grid(row=0, column=1, sticky="e", padx=14)
            if ia_status == "uploaded":
                self._ghost_small(acts, "View on archive.org",
                                  lambda v=vid: webbrowser.open(lostmedia.ia_url(v))).pack(side="left", padx=4)
            elif ia_status == "uploading":
                ctk.CTkButton(acts, text="Uploading...", state="disabled", width=150, height=34, corner_radius=8,
                              font=font(13), fg_color=C["line"], text_color=C["muted"]).pack(side="left", padx=4)
            else:
                ctk.CTkButton(acts, text="Retry upload" if ia_status == "failed" else "Upload to Internet Archive",
                              height=34, corner_radius=8, font=font(13, "bold"), fg_color=C["rec"],
                              hover_color=C["rec_hov"], text_color="#FFFFFF",
                              command=lambda v=vid: self.upload_one(v)).pack(side="left", padx=4)
            self._ghost_small(acts, "Show file", lambda v=vid: self.show_file(v)).pack(side="left", padx=4)

    def _removed_check_due(self):
        st = load_state().get("last_removed_check")
        if not st:
            return True
        return datetime.now() - datetime.fromisoformat(st) > timedelta(hours=24)

    def _run_removed_check(self, cfg, stop):
        self.rm_busy, self.rm_progress = True, (0, 0)
        try:
            found = lostmedia.check_removed(cfg, self.log, progress=self._set_rm_progress, stop=stop)
        except Exception as e:
            found = []
            self.log(f"ERROR: {e}")
        finally:
            self.rm_busy = False
            self.after(0, self.refresh_stats)
        return found

    def _set_rm_progress(self, i, n):
        self.rm_progress = (i, n)

    def check_removed(self):
        if self.rm_busy:
            return
        cfg = self._collect(quiet=True)
        if cfg:
            threading.Thread(target=self._run_removed_check, args=(cfg, threading.Event()), daemon=True).start()

    def upload_one(self, vid):
        from tkinter import messagebox
        cfg = self._collect(quiet=True)
        if not cfg:
            return
        if not cfg.get("ia_access") or not cfg.get("ia_secret"):
            self.show("Settings")
            self.save_note.configure(text="Add your Internet Archive keys first.", text_color=C["rec"])
            return
        title = load_library().get(vid, {}).get("title", vid)
        if not messagebox.askyesno("Upload to Internet Archive",
                                   f"Upload \"{title}\" to Internet Archive?\n\n"
                                   f"It will be public at {lostmedia.ia_url(vid)}\n\n"
                                   "Only upload videos you have the right to share."):
            return

        def go():
            try:
                lostmedia.upload_to_ia(vid, cfg, self.log)
            except Exception as e:
                self.log(f"ERROR: {e}")
            self.after(0, self.refresh_stats)
        threading.Thread(target=go, daemon=True).start()
        self.after(400, self._render_removed)

    def show_file(self, vid):
        rel = load_library().get(vid, {}).get("file", "")
        p = Path(self.cfg["dest"]).expanduser() / rel
        if rel and p.exists():
            if SYS == "Windows":
                subprocess.Popen(["explorer", "/select,", str(p)])
            elif SYS == "Darwin":
                subprocess.Popen(["open", "-R", str(p)])
            else:
                self._open(p.parent)
        else:
            self.log("That file isn't on this computer. It may only be in your cloud storage.")
            self.show("Activity")

    # ------------------------------------------------ settings
    def _settings(self):
        page = ctk.CTkFrame(self, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(page, text="Settings", font=font(30, "bold"), text_color=C["text"]).grid(row=0, column=0, sticky="w")

        body = ctk.CTkScrollableFrame(page, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", pady=(16, 0))
        body.grid_columnconfigure(0, weight=1)
        self.v = {}
        self._r = 0

        def var(key, kind=tk.StringVar):
            val = self.cfg[key]
            self.v[key] = kind(value=str(val) if kind is tk.StringVar else val)
            return self.v[key]

        def section(title):
            box = ctk.CTkFrame(body, fg_color=C["surface"], corner_radius=14, border_width=1, border_color=C["line"])
            box.grid(row=self._r, column=0, sticky="ew", pady=(0, 16), padx=(0, 8))
            box.grid_columnconfigure(0, weight=1)
            self._r += 1
            ctk.CTkLabel(box, text=title, font=font(16, "bold"), text_color=C["text"]).grid(
                row=0, column=0, columnspan=2, sticky="w", padx=20, pady=(16, 4))
            box._n = 1
            return box

        def field(box, label, hint, widget_fn):
            r = box._n
            box._n += 1
            t = ctk.CTkFrame(box, fg_color="transparent")
            t.grid(row=r, column=0, sticky="w", padx=20, pady=10)
            ctk.CTkLabel(t, text=label, font=font(14), text_color=C["text"]).pack(anchor="w")
            if hint:
                ctk.CTkLabel(t, text=hint, font=font(12), text_color=C["muted"], wraplength=380, justify="left").pack(anchor="w")
            w = widget_fn(box)
            w.grid(row=r, column=1, sticky="e", padx=20, pady=10)

        def entry(key, width=260, ph=""):
            return lambda p: ctk.CTkEntry(p, textvariable=var(key), width=width, placeholder_text=ph,
                                          border_color=C["line"], height=34)

        def path_entry(key, pick):
            def make(p):
                f = ctk.CTkFrame(p, fg_color="transparent")
                ctk.CTkEntry(f, textvariable=var(key), width=230, border_color=C["line"], height=34).pack(side="left")
                ctk.CTkButton(f, text="Browse", width=76, height=34, corner_radius=8, font=font(13),
                              fg_color="transparent", border_width=1, border_color=C["line"],
                              hover_color=C["navsel"], text_color=C["text"],
                              command=lambda: pick(key)).pack(side="left", padx=(8, 0))
                return f
            return make

        def switch(key):
            return lambda p: ctk.CTkSwitch(p, text="", variable=var(key, tk.BooleanVar), onvalue=True, offvalue=False,
                                           progress_color=C["rec"])

        s = section("Account")
        field(s, "Browser", "The browser you're signed into YouTube with. On Windows, Firefox works most reliably.",
              lambda p: ctk.CTkOptionMenu(p, values=BROWSERS, variable=var("browser"), width=160))
        field(s, "Cookies file", "Optional. Use a cookies.txt export if reading the browser fails.",
              path_entry("cookies_file", self._pick_file))

        s = section("Saving")
        field(s, "Save to", None, path_entry("dest", self._pick_dir))
        field(s, "Max quality", None,
              lambda p: ctk.CTkSegmentedButton(p, values=QUALITIES, variable=var("quality"),
                                               selected_color=C["rec"], selected_hover_color=C["rec_hov"]))
        field(s, "Audio only", "Saves just the sound as .m4a. Much smaller files.", switch("audio_only"))
        field(s, "Folder per channel", "Groups videos into a folder for each channel.", switch("by_channel"))
        field(s, "Skip short videos", "Seconds. Set to 120 to leave out most Shorts. 0 saves everything.",
              entry("min_duration", 90))

        s = section("Schedule")
        field(s, "Check every", "Minutes between checks while watching.", entry("interval_min", 90))
        field(s, "Check my whole history", "Looks through every video you've ever watched, not only recent ones. "
              "Already-saved videos are skipped, but the first run can take hours.", switch("check_all"))
        field(s, "Videos to look at", "Only used when whole history is off.", entry("check_count", 90))

        s = section("Speed and limits")
        field(s, "Pace", "How long to wait between videos. Gentle is least likely to trigger YouTube's limits; "
              "Fast has no waits.",
              lambda p: ctk.CTkSegmentedButton(p, values=["Gentle", "Normal", "Fast"], variable=var("pace"),
                                               selected_color=C["rec"], selected_hover_color=C["rec_hov"]))
        field(s, "Download speed limit", "MB per second. 0 means no limit. Useful if downloads slow down your internet.",
              entry("speed_limit_mb", 90))
        field(s, "Max videos per check", "0 means no limit. Spreads a big backlog over several checks.",
              entry("max_per_check", 90))
        field(s, "Back off when YouTube slows you down", "Stops the check and waits before trying again.",
              switch("backoff"))
        field(s, "Wait time", "Hours to wait after YouTube slows you down.", entry("backoff_hours", 90))

        s = section("Cloud upload")
        field(s, "rclone destination", "Leave blank to keep videos on this computer only. Example: gdrive:YouTube",
              entry("rclone_remote", 260, "gdrive:YouTube"))
        field(s, "After uploading", "Move deletes the local copy once it's in the cloud.",
              lambda p: ctk.CTkSegmentedButton(p, values=["copy", "move"], variable=var("rclone_mode"),
                                               selected_color=C["rec"], selected_hover_color=C["rec_hov"]))

        s = section("Internet Archive")
        field(s, "Access key", "From your archive.org S3 keys page.", entry("ia_access", 260))
        field(s, "Secret key", None,
              lambda p: ctk.CTkEntry(p, textvariable=var("ia_secret"), width=260, show="•",
                                     border_color=C["line"], height=34))
        field(s, "Get your keys", "Sign in to archive.org, then copy both keys from this page.",
              lambda p: self._ghost_small(p, "Open archive.org",
                                          lambda: webbrowser.open("https://archive.org/account/s3.php")))
        field(s, "When a saved video is removed", "Uploads are public. Only upload videos you have the right to share.",
              lambda p: ctk.CTkSegmentedButton(p, values=["Ask me", "Upload automatically"], variable=var("ia_mode"),
                                               selected_color=C["rec"], selected_hover_color=C["rec_hov"]))
        field(s, "Check for removed videos", "Once a day while watching.", switch("removed_check_daily"))

        s = section("Maintenance")
        field(s, "Skip existing history", "Marks videos already in your history as saved, so only new ones download.",
              lambda p: self._ghost_small(p, "Skip now", self.skip_history))
        field(s, "Update yt-dlp", "Fixes most download errors after YouTube changes something.",
              lambda p: self._ghost_small(p, "Update", self.update_ytdlp))

        foot = ctk.CTkFrame(page, fg_color="transparent")
        foot.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        ctk.CTkButton(foot, text="Save settings", height=40, width=150, corner_radius=20, font=font(14, "bold"),
                      fg_color=C["text"], hover_color=C["muted"], text_color=C["bg"],
                      command=self.save_settings).pack(side="left")
        self.save_note = ctk.CTkLabel(foot, text="", font=font(13), text_color=C["muted"])
        self.save_note.pack(side="left", padx=14)
        return page

    def _ghost_small(self, parent, text, cmd):
        return ctk.CTkButton(parent, text=text, width=110, height=34, corner_radius=8, font=font(13), command=cmd,
                             fg_color="transparent", border_width=1, border_color=C["line"],
                             hover_color=C["navsel"], text_color=C["text"])

    # ------------------------------------------------ activity
    def _activity(self):
        page = ctk.CTkFrame(self, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)
        head = ctk.CTkFrame(page, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(head, text="Activity", font=font(30, "bold"), text_color=C["text"]).pack(side="left")
        self._ghost_small(head, "Open log file", self.open_log).pack(side="right")
        self.logbox = ctk.CTkTextbox(page, font=font(13), fg_color=C["surface"], border_width=1,
                                     border_color=C["line"], corner_radius=14, text_color=C["text"], wrap="word")
        self.logbox.grid(row=1, column=0, sticky="nsew", pady=(16, 0))
        self.logbox.configure(state="disabled")
        return page

    # ------------------------------------------------ settings helpers
    def _pick_dir(self, key):
        from tkinter import filedialog
        d = filedialog.askdirectory(initialdir=self.v[key].get() or str(Path.home()))
        if d:
            self.v[key].set(d)

    def _pick_file(self, key):
        from tkinter import filedialog
        f = filedialog.askopenfilename(filetypes=[("Cookies", "*.txt"), ("All files", "*")])
        if f:
            self.v[key].set(f)

    def _collect(self, quiet=False):
        new = dict(self.cfg)
        for k, var in self.v.items():
            new[k] = var.get()
        try:
            mb = float(str(new.get("speed_limit_mb", 0)).strip() or 0)
            if not 0 <= mb <= 1000:
                raise ValueError
            new["speed_limit_mb"] = mb
        except ValueError:
            self.show("Settings")
            self.save_note.configure(text="Enter a speed limit from 0 to 1000 MB per second.", text_color=C["rec"])
            return None
        for k, lo, hi in (("min_duration", 0, 86400), ("interval_min", 5, 1440), ("check_count", 1, 1000),
                          ("max_per_check", 0, 100000), ("backoff_hours", 1, 72)):
            try:
                n = int(str(new[k]).strip() or 0)
                if not lo <= n <= hi:
                    raise ValueError
                new[k] = n
            except ValueError:
                self.show("Settings")
                self.save_note.configure(text=f"Enter a whole number from {lo} to {hi} for that field.",
                                         text_color=C["rec"])
                return None
        if not str(new["dest"]).strip():
            self.show("Settings")
            self.save_note.configure(text="Choose a folder to save videos to.", text_color=C["rec"])
            return None
        self.cfg = new
        save_config(new)
        if not quiet:
            self.save_note.configure(text=f"Saved at {datetime.now():%H:%M}", text_color=C["muted"])
        return dict(new)

    def save_settings(self):
        if self._collect():
            self.refresh_stats()

    # ------------------------------------------------ actions
    def _run(self, cfg, label):
        self.busy, self.busy_label = True, label
        try:
            self.engine.run_once(cfg, self.stop_event if self.watching else threading.Event())
        finally:
            self.busy = False
            self.last_check = datetime.now()
            self.after(0, self.refresh_stats)

    def _confirm_full(self, cfg):
        """Warn before a whole-history download when nothing has been saved yet."""
        if not cfg.get("check_all") or len(archive_ids()) > 0:
            return True
        from tkinter import messagebox
        return messagebox.askyesno(
            "Download your whole history?",
            "This will save every video in your watch history. For most people that's thousands of "
            "videos and hundreds of GB, and the first run can take hours or days.\n\n"
            "To save only videos you watch from now on, choose No, then use Skip existing history in Settings.\n\n"
            "Download everything?")

    def toggle(self):
        if self.watching:
            self.watching = False
            self.stop_event.set()
            self.next_check = None
            self.log("Stopping after the current video.")
            return
        cfg = self._collect(quiet=True)
        if not cfg or not self._confirm_full(cfg):
            return
        self.stop_event = threading.Event()
        self.watching = True
        threading.Thread(target=self._loop, args=(self.stop_event,), daemon=True).start()

    def _loop(self, stop):
        while not stop.is_set():
            cfg = load_config()
            self._run(cfg, "Checking your history")
            if cfg.get("removed_check_daily") and not stop.is_set() and self._removed_check_due():
                self._run_removed_check(cfg, stop)
            mins = int(cfg["interval_min"])
            if self.engine.rate_limited and cfg.get("backoff"):
                mins = max(mins, int(cfg["backoff_hours"]) * 60)
                self.backoff_until = datetime.now() + timedelta(minutes=mins)
                self.log(f"Waiting until {self.backoff_until:%H:%M} before checking again.")
            else:
                self.backoff_until = None
            self.next_check = datetime.now() + timedelta(minutes=mins)
            if stop.wait(mins * 60):
                break
        self.log("Stopped watching.")

    def check_now(self):
        if self.busy:
            return
        cfg = self._collect(quiet=True)
        if cfg and self._confirm_full(cfg):
            threading.Thread(target=self._run, args=(cfg, "Checking your history"), daemon=True).start()

    def skip_history(self):
        cfg = self._collect(quiet=True)
        if not cfg or self.busy:
            return
        def go():
            self.busy, self.busy_label = True, "Reading your history"
            try:
                self.engine.mark_history_seen(cfg)
            except Exception as e:
                self.log(f"ERROR: {e}")
            finally:
                self.busy = False
                self.after(0, self.refresh_stats)
        threading.Thread(target=go, daemon=True).start()
        self.show("Activity")

    def update_ytdlp(self):
        def go():
            self.log("Updating yt-dlp...")
            r = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default]"],
                               capture_output=True, text=True)
            self.log("yt-dlp updated. Restart the app to use the new version." if r.returncode == 0
                     else "ERROR: update failed: " + r.stderr.strip()[-300:])
        threading.Thread(target=go, daemon=True).start()
        self.show("Activity")

    def _open(self, path):
        if SYS == "Windows":
            os.startfile(str(path))
        elif SYS == "Darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def open_folder(self):
        d = Path(self.cfg["dest"]).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        self._open(d)

    def open_log(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        LOG_FILE.touch()
        self._open(LOG_FILE)

    # ------------------------------------------------ stats (background)
    def refresh_stats(self):
        if self._stats_running:
            return
        self._stats_running = True
        dest = Path(self.cfg["dest"]).expanduser()

        def work():
            total, files = 0, []
            try:
                for root, _, names in os.walk(dest):
                    for n in names:
                        p = Path(root) / n
                        if p.suffix.lower() in VIDEO_EXT:
                            st = p.stat()
                            total += st.st_size
                            files.append((st.st_mtime, p))
            except Exception:
                pass
            files.sort(reverse=True)
            recent = []
            for mtime, p in files[:12]:
                title = p.stem
                if " - " in title[:12]:
                    title = title.split(" - ", 1)[1]
                if title.endswith("]") and "[" in title:
                    title = title[:title.rfind("[")].strip()
                channel = p.parent.name if p.parent != dest else ""
                recent.append((title, channel, self._ago(datetime.fromtimestamp(mtime))))
            lib = load_library()
            gone = [e for e in lib.values() if e.get("status") in lostmedia.GONE]
            self.gone_total = len(gone)
            self.gone_pending = sum(1 for e in gone if e.get("ia_status") != "uploaded")
            self.stats = {"count": len(archive_ids()), "size": total, "recent": recent}
            self._stats_running = False
            self.after(0, self._render_stats)

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _ago(t):
        s = (datetime.now() - t).total_seconds()
        if s < 60:
            return "Just now"
        if s < 3600:
            return f"{int(s // 60)} min ago"
        if s < 86400:
            return f"{int(s // 3600)} h ago"
        return t.strftime("%b %d")

    def _render_stats(self):
        self.stat_vals["count"].configure(text=f"{self.stats['count']:,}")
        self.stat_vals["size"].configure(text=human_size(self.stats["size"] or 0))
        self._render_recent()
        self._render_removed()
        self.nav["Removed"].configure(text=f"Removed   {self.gone_total}" if self.gone_total else "Removed")
        want = self.gone_pending > 0
        if want:
            n = self.gone_pending
            self.banner_text.configure(text=f"{n} saved video{'s are' if n != 1 else ' is'} no longer on YouTube "
                                            f"and not yet on Internet Archive.")
        if want != self._banner_shown:
            if want:
                self.banner.grid(row=2, column=0, sticky="ew", pady=(0, 16))
            else:
                self.banner.grid_forget()
            self._banner_shown = want

    # ------------------------------------------------ live status
    def _tick(self):
        self._blink = not self._blink
        if self.busy:
            color, big = C["amber"], self.busy_label
            small = "Saving any new videos. This can take a few minutes."
        elif self.watching:
            color = C["rec"] if self._blink else C["rec_hov"]
            if self.backoff_until and self.backoff_until > datetime.now():
                big = "Waiting"
                small = (f"YouTube slowed down requests, so the app is giving it a break. "
                         f"Next check at {self.backoff_until:%H:%M}.")
            else:
                big = "Watching"
                nxt = f"Next check at {self.next_check:%H:%M}." if self.next_check else ""
                small = f"New videos you watch are saved automatically. {nxt}".strip()
        else:
            color, big = C["idle"], "Paused"
            small = "Start watching to save new videos as you view them."
        self.dot.configure(text_color=color)
        self.side_dot.configure(text_color=color)
        self.status_big.configure(text=big)
        self.side_state.configure(text=big if len(big) < 14 else "Checking")
        self.status_small.configure(text=small)
        self.toggle_btn.configure(text="Stop watching" if self.watching else "Start watching")
        self.check_btn.configure(state="disabled" if self.busy else "normal")
        self.stat_vals["last"].configure(text=self._ago(self.last_check) if self.last_check else "Never")
        if self.rm_busy:
            i, n = self.rm_progress
            self.rm_bar.set(i / n if n else 0)
            self.rm_note.configure(text=f"Checked {i} of {n}" if n else "Starting...")
            self.rm_btn.configure(state="disabled", text="Checking...")
        else:
            self.rm_btn.configure(state="normal", text="Check saved videos")
            st = load_state().get("last_removed_check")
            self.rm_note.configure(text=("Last checked " + self._ago(datetime.fromisoformat(st))) if st
                                   else "Not checked yet")
        self.after(700, self._tick)

    # ------------------------------------------------ log
    def log(self, msg):
        line = f"{datetime.now():%H:%M:%S}  {msg}"
        self.msgs.put(line)
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d} {line}\n")
        except Exception:
            pass

    def _drain_log(self):
        if not self.msgs.empty():
            self.logbox.configure(state="normal")
            while not self.msgs.empty():
                self.logbox.insert("end", self.msgs.get() + "\n")
            self.logbox.see("end")
            self.logbox.configure(state="disabled")
        self.after(150, self._drain_log)

    def _on_close(self):
        if self.watching or self.busy or self.rm_busy:
            from tkinter import messagebox
            if not messagebox.askyesno("Quit", "Quit and stop saving new videos?"):
                return
        self.stop_event.set()
        self.destroy()


if __name__ == "__main__":
    try:
        App().mainloop()
    except Exception:
        # When started from the desktop shortcut there's no console, so record the crash somewhere visible.
        import traceback
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        (CONFIG_DIR / "crash.log").write_text(traceback.format_exc(), encoding="utf-8")
        try:
            from tkinter import messagebox
            messagebox.showerror("YouTube History Archiver",
                                 f"The app hit an error and closed. Details are in {CONFIG_DIR / 'crash.log'}")
        except Exception:
            pass
        raise
