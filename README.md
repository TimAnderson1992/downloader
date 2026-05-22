# Offline Downloader

Offline Downloader is a Linux Mint GTK app for maintaining offline backup copies of GitHub repositories, direct-download files, and the latest Linux Mint Cinnamon 64-bit ISO.

The app is download-only. It does not install packages, run downloaded files, compile code, configure services, start containers, update Linux, or change system settings.

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
