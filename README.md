# Offline Downloader

Offline Downloader is a Linux Mint GTK app for maintaining offline backup copies of GitHub repositories, direct-download files, and the latest Linux Mint Cinnamon 64-bit ISO.

The app is download-only. It does not install packages, run downloaded files, compile code, configure services, start containers, update Linux, or change system settings.

## Download

Clone the repo:

```bash
git clone https://github.com/TimAnderson1992/downloader.git
cd downloader
```

## Files

- `offline_downloader_gui.py` - GTK GUI application.
- `offline_downloader.sh` - launcher script.
- `offline-downloader.desktop` - desktop launcher.

## Features

- Add GitHub repository URLs.
- Add direct file URLs.
- Enable, disable, and remove downloads.
- Choose the main save folder.
- Configure a monthly schedule by day of month and time of day.
- Prompt on launch when the saved monthly schedule is due.
- Start downloads manually with Download Now.
- Preview work with Dry Run without changing files.
- Cancel the queue after the current item finishes.
- Enable or disable an optional systemd user timer for monthly auto-checks.
- Show clear status messages.
- Download only one item at a time.

## Linux Mint ISO Handling

The Linux Mint item uses `https://linuxmint.com/download_all.php` as a webpage to scan. The app does not save `download_all.php`.

It finds the newest Linux Mint Cinnamon 64-bit ISO filename, downloads only the real `.iso` file, saves it under `isos/linuxmint/`, and keeps only the newest ISO plus the previous ISO after a successful new download.

## Run

```bash
./offline_downloader.sh
```

## Config

User settings are stored locally at:

```text
~/.config/offline-downloader/config.json
```

That config file is not part of this repository.

The config also stores `last_scheduled_run_date`, which prevents the scheduled check from running more than once for the same monthly due date.

## Optional Systemd User Timer

The GUI buttons `Enable Monthly Auto-Check` and `Disable Monthly Auto-Check` manage user-level systemd files only:

```text
~/.config/systemd/user/offline-downloader.service
~/.config/systemd/user/offline-downloader.timer
```

No `sudo` is used. The timer periodically launches:

```bash
python3 offline_downloader_gui.py --scheduled-check
```

That headless check reads the saved monthly schedule and only starts downloads when the schedule is due and `last_scheduled_run_date` is not today. Downloads still run one item at a time.
