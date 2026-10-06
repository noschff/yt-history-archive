# YouTube History Archiver

Saves every YouTube video you watch (on any device signed into your account) to a folder, and can upload it to the cloud.

## Setup

**Windows:** double-click `setup.bat`. It:
1. Finds Python 3.10 or newer (via the `py -3` launcher), or installs Python 3.12 if you don't have it
2. Installs the Python packages (customtkinter, yt-dlp, internetarchive)
3. Installs ffmpeg and Deno
4. Optionally installs rclone and walks you through connecting a cloud account
5. Puts a **YouTube History Archiver** shortcut on your desktop

After that, open the app from the desktop shortcut or `Start YouTube Archiver.bat`. Run `setup.bat` again any time to repair or update.

Automatic installs use winget, which comes with Windows 10/11. If it's missing, get "App Installer" from the Microsoft Store, or install Python from python.org (tick "Add python.exe to PATH").

**Mac/Linux:** install Python 3.10+, ffmpeg and Deno (`brew install python ffmpeg deno` on Mac), then run `./run_mac_linux.sh`.

Also make sure **YouTube watch history is on** in your Google account.

## Using it

1. Pick the browser you're logged into YouTube with.
2. Pick a save folder and quality.
3. First time only: click **Skip existing history** if you don't want your existing history downloaded.
4. Click **Start watching**. Leave the window open (minimise it).

## Whole history vs. recent videos

By default the app checks your **entire** watch history on every run, so nothing is missed (including bursts of Shorts). Videos that are already saved are skipped without being downloaded again, so later checks are much faster than the first one.

The first run downloads everything you've ever watched, which can be thousands of videos and hundreds of GB. If you only want videos from now on, click **Skip existing history** before you start. To go back to checking only recent videos, turn off **Check my whole history** in Settings.

## Speed and limits

In Settings > Speed and limits:
- **Pace**: Gentle waits 10–25 s between videos, Normal 2–6 s, Fast doesn't wait. Use Gentle for a big first download.
- **Download speed limit**: caps bandwidth in MB per second (0 = unlimited).
- **Max videos per check**: spreads a large backlog across several checks (0 = unlimited).
- **Back off when YouTube slows you down**: if YouTube starts refusing requests ("not a bot" checks, error 429), the app stops that check and waits the number of hours you set before trying again.

## Videos removed from YouTube

The **Removed** page checks every video you've saved and lists the ones YouTube no longer has (deleted, terminated channel, copyright strike, or made private). Each one gets an **Upload to Internet Archive** button.

To set it up:
1. Make a free account at archive.org.
2. In the app, go to Settings > Internet Archive > **Open archive.org**, and copy your access key and secret key into the app.

**Review before uploading.** Clicking **Review and upload** opens a window showing a still from the video, a button to play the whole thing, and every detail that will be public: title, creator, date, tags and description. You can edit any of it or cancel. Nothing uploads until you tick "I've watched this and I have the right to share it publicly" and click **Upload publicly**. Your edits are kept if you need to retry.

If you set **Before uploading** to **Skip review**, removed videos upload automatically with no review window. The headless `--check-removed` mode only uploads when Skip review is chosen.

Uploads go to `archive.org/details/youtube-<video id>`, with the original title, channel, upload date, description and link. Before every upload the app looks for existing copies in three places: the standard `youtube-<id>` address, a search of all archive.org items for the video ID (catching uploads by other people under any name), and the Wayback Machine. If the standard address exists, it links to it and doesn't upload. If other copies turn up, the review window lists them so you can decide whether yours is worth adding. **Find copies** on the Removed page runs the same search without uploading. In Skip review mode, videos that someone else already archived are skipped rather than duplicated. If you use rclone "move" mode, the app downloads the file back from your cloud before uploading.

Uploads are public. Only upload videos you have the right to share; archive.org may remove copyrighted material. Your keys are stored in plain text in `~/.yt_history_archiver/config.json`.

Headless: `python core.py --check-removed` checks for removals (and uploads them only if Skip review is on).

## Tips and fixes

- **Windows + Chrome/Edge:** Chrome locks its cookies, so reading them often fails. Use Firefox, or export a `cookies.txt` with a "Get cookies.txt LOCALLY" extension and pick that file in the app.
- **Downloads suddenly fail:** YouTube changes often. Click **Update yt-dlp** and restart.
- **Run without the window (cron / Task Scheduler):** `python core.py` (or `python app.py --once`) does one pass with your saved settings.
  Example cron (every 30 min): `*/30 * * * * /usr/bin/python3 /path/to/core.py`
- Settings, the "already saved" list and a log live in `~/.yt_history_archiver/`. Delete `archive.txt` there to re-download everything.
- Storage: roughly 0.5–1 GB per hour of 1080p video. Use 720p, audio-only or "skip shorter than" to cut it down.

Downloading YouTube videos goes against YouTube's Terms of Service; use it for your own personal archive and keep the check interval reasonable.
