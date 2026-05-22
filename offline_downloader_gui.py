#!/usr/bin/env python3

import datetime
import email.utils
import filecmp
import gzip
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk


APP_NAME = "Offline Downloader"
APP_VERSION = "1.0.2"
APP_REPO_URL = "https://github.com/TimAnderson1992/downloader"
APP_RELEASES_URL = f"{APP_REPO_URL}/releases"
APP_LATEST_RELEASE_API = "https://api.github.com/repos/TimAnderson1992/downloader/releases/latest"
CONFIG_DIR = Path.home() / ".config" / "offline-downloader"
CONFIG_FILE = CONFIG_DIR / "config.json"
SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
SYSTEMD_SERVICE = SYSTEMD_USER_DIR / "offline-downloader.service"
SYSTEMD_TIMER = SYSTEMD_USER_DIR / "offline-downloader.timer"

DEFAULT_REPOS = [
    "https://github.com/TimAnderson1992/LinuxMintTaskManager.git",
    "https://github.com/pyMC-dev/pyMC_Repeater.git",
    "https://github.com/OpenCPN/OpenCPN.git",
    "https://github.com/SignalK/signalk-server.git",
    "https://github.com/openplotter/openplotter.git",
    "https://github.com/kiwix/kiwix-tools.git",
    "https://github.com/open-webui/open-webui.git",
    "https://github.com/syncthing/syncthing.git",
    "https://github.com/meshtastic/firmware.git",
    "https://github.com/meshtastic/python.git",
    "https://github.com/AvaloniaUI/Avalonia.git",
    "https://github.com/MODSetter/SurfSense.git",
]

DEFAULT_DIRECT_URLS = [
    "https://code.visualstudio.com/sha/download?build=stable&os=linux-deb-x64",
    "https://downloads.raspberrypi.org/imager/imager_latest_amd64.deb",
    "https://download.kiwix.org/release/kiwix-desktop/",
]

DEFAULT_GITHUB_RELEASES = [
    {
        "name": "Linux Mint Task Manager DEB",
        "url": "https://github.com/TimAnderson1992/LinuxMintTaskManager/releases/tag/Release",
    }
]


def default_items():
    items = []
    for url in DEFAULT_REPOS:
        items.append({"enabled": True, "type": "github", "url": url})
    for url in DEFAULT_DIRECT_URLS:
        items.append({"enabled": True, "type": "direct", "url": url})
    for release in DEFAULT_GITHUB_RELEASES:
        items.append(
            {
                "enabled": True,
                "name": release["name"],
                "type": "github_release",
                "url": release["url"],
            }
        )
    items.append(
        {
            "enabled": True,
            "type": "linuxmint_iso",
            "url": "https://linuxmint.com/download_all.php",
        }
    )
    return items


def default_config():
    items = default_items()

    return {
        "save_root": str(Path.home() / "OfflineDownloads"),
        "monthly_schedule": {
            "day": 15,
            "hour": 2,
            "minute": 0,
        },
        "last_scheduled_run_date": "",
        "items": items,
    }


def merge_default_items(config):
    existing = {
        (item.get("type"), item.get("url"))
        for item in config.get("items", [])
    }
    added = False
    for item in default_items():
        key = (item.get("type"), item.get("url"))
        if key not in existing:
            config.setdefault("items", []).append(dict(item))
            existing.add(key)
            added = True
    return added


def github_url_without_git(url):
    return url[:-4] if url.endswith(".git") else url


def github_url_variants(url):
    base = github_url_without_git(url)
    return {base, f"{base}.git"}


def normalize_default_github_items(config):
    defaults_by_base = {
        github_url_without_git(item["url"]): item
        for item in default_items()
        if item.get("type") == "github"
    }
    existing_by_base = {}
    for item in config.get("items", []):
        if item.get("type") == "github" and "github.com" in item.get("url", ""):
            existing_by_base[github_url_without_git(item["url"])] = item

    changed = False
    for base, default in defaults_by_base.items():
        existing = existing_by_base.get(base)
        if existing:
            if existing.get("enabled") is not True:
                existing["enabled"] = True
                changed = True
            if existing.get("url") != default["url"]:
                existing["url"] = default["url"]
                changed = True
        else:
            config.setdefault("items", []).append(dict(default))
            changed = True
    return changed


def ensure_download_dirs(save_root):
    root = Path(save_root).expanduser()
    for name in [
        "github",
        "packages",
        "isos",
        "isos/linuxmint",
        "vscode",
        "firmware",
        "appimages",
        "zim",
    ]:
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def load_config():
    if not CONFIG_FILE.exists():
        data = default_config()
        save_config(data)
        return data
    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default_config()

    defaults = default_config()
    data.setdefault("save_root", defaults["save_root"])
    data.setdefault("monthly_schedule", defaults["monthly_schedule"])
    data.setdefault("last_scheduled_run_date", "")
    data.setdefault("items", defaults["items"])
    changed = merge_default_items(data)
    if normalize_default_github_items(data):
        changed = True
    if data.pop("scheduled_days", None) is not None:
        changed = True
    if changed:
        save_config(data)
    return data


def clamp(value, low, high, fallback):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def save_config(config):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with CONFIG_FILE.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)


def scheduled_download_due(config, now=None):
    if now is None:
        now = datetime.datetime.now()

    schedule = config.get("monthly_schedule", {})
    day = clamp(schedule.get("day"), 1, 28, 15)
    hour = clamp(schedule.get("hour"), 0, 23, 2)
    minute = clamp(schedule.get("minute"), 0, 59, 0)
    today = now.date().isoformat()

    if config.get("last_scheduled_run_date") == today:
        return False
    if now.day != day:
        return False

    scheduled_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return now >= scheduled_time


def mark_scheduled_run_complete(config):
    config["last_scheduled_run_date"] = datetime.date.today().isoformat()
    save_config(config)


def run_systemctl_user(args):
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def app_base_dir():
    return Path(__file__).resolve().parent


def enable_user_timer():
    script_path = Path(__file__).resolve()
    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    SYSTEMD_SERVICE.write_text(
        "\n".join(
            [
                "[Unit]",
                "Description=Offline Downloader monthly scheduled check",
                "",
                "[Service]",
                "Type=oneshot",
                f"ExecStart=/usr/bin/python3 {script_path} --scheduled-check",
                "",
            ]
        ),
        encoding="utf-8",
    )
    SYSTEMD_TIMER.write_text(
        "\n".join(
            [
                "[Unit]",
                "Description=Run Offline Downloader scheduled check",
                "",
                "[Timer]",
                "OnCalendar=hourly",
                "Persistent=true",
                "Unit=offline-downloader.service",
                "",
                "[Install]",
                "WantedBy=timers.target",
                "",
            ]
        ),
        encoding="utf-8",
    )

    daemon = run_systemctl_user(["daemon-reload"])
    if daemon.returncode != 0:
        raise RuntimeError((daemon.stderr or daemon.stdout).strip())

    enabled = run_systemctl_user(["enable", "--now", "offline-downloader.timer"])
    if enabled.returncode != 0:
        raise RuntimeError((enabled.stderr or enabled.stdout).strip())


def git_app_output(args):
    return subprocess.run(
        ["git", *args],
        cwd=str(app_base_dir()),
        capture_output=True,
        text=True,
        check=False,
    )


def app_running_from_git_clone():
    result = git_app_output(["rev-parse", "--is-inside-work-tree"])
    return result.returncode == 0 and result.stdout.strip() == "true"


def app_git_update_status():
    if not app_running_from_git_clone():
        return {"git_clone": False}

    remote_result = git_app_output(["remote", "get-url", "origin"])
    remote_url = remote_result.stdout.strip() if remote_result.returncode == 0 else ""
    if "github.com" not in remote_url or "TimAnderson1992/downloader" not in remote_url:
        return {"git_clone": True, "wrong_remote": remote_url}

    fetch_result = git_app_output(["fetch", "origin", "main"])
    if fetch_result.returncode != 0:
        raise RuntimeError((fetch_result.stderr or fetch_result.stdout).strip())

    local_result = git_app_output(["rev-parse", "HEAD"])
    remote_head_result = git_app_output(["rev-parse", "origin/main"])
    if local_result.returncode != 0 or remote_head_result.returncode != 0:
        raise RuntimeError("Could not compare local app version with origin/main")

    local_head = local_result.stdout.strip()
    remote_head = remote_head_result.stdout.strip()
    return {
        "git_clone": True,
        "remote_url": remote_url,
        "local_head": local_head,
        "remote_head": remote_head,
        "update_available": local_head != remote_head,
    }


def pull_app_update():
    pull_result = git_app_output(["pull", "--ff-only", "origin", "main"])
    if pull_result.returncode != 0:
        raise RuntimeError((pull_result.stderr or pull_result.stdout).strip())
    return pull_result.stdout.strip()


def version_key(version):
    version = version.lstrip("v")
    return tuple(int(part) if part.isdigit() else 0 for part in re.split(r"[.-]", version))


def latest_deb_release_status():
    request = urllib.request.Request(APP_LATEST_RELEASE_API, headers={"User-Agent": APP_NAME})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.loads(response.read().decode("utf-8"))

    tag = data.get("tag_name", "")
    release_version = tag.lstrip("v")
    assets = data.get("assets", [])
    deb_assets = [
        asset for asset in assets
        if asset.get("name", "").endswith("_amd64.deb") and asset.get("browser_download_url")
    ]
    deb_asset = sorted(deb_assets, key=lambda item: natural_key(item["name"]))[-1] if deb_assets else None

    return {
        "release_url": data.get("html_url", APP_RELEASES_URL),
        "tag": tag,
        "version": release_version,
        "newer": version_key(release_version) > version_key(APP_VERSION),
        "asset_name": deb_asset.get("name") if deb_asset else "",
        "asset_url": deb_asset.get("browser_download_url") if deb_asset else "",
    }


def disable_user_timer():
    stopped = run_systemctl_user(["disable", "--now", "offline-downloader.timer"])
    if stopped.returncode != 0 and "does not exist" not in (stopped.stderr or ""):
        raise RuntimeError((stopped.stderr or stopped.stdout).strip())

    SYSTEMD_TIMER.unlink(missing_ok=True)
    SYSTEMD_SERVICE.unlink(missing_ok=True)

    daemon = run_systemctl_user(["daemon-reload"])
    if daemon.returncode != 0:
        raise RuntimeError((daemon.stderr or daemon.stdout).strip())


def repo_name(repo_url):
    name = repo_url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name or "repository"


def direct_filename(url, headers=None):
    if "visualstudio.com/sha/download" in url:
        return "vscode-stable-linux-deb-x64.deb"

    if headers:
        disposition = headers.get("Content-Disposition", "")
        match = re.search(r'filename="?([^";]+)"?', disposition)
        if match:
            return match.group(1)

    parsed = urllib.parse.urlparse(url)
    name = Path(parsed.path).name
    if name:
        return name

    return "downloaded-file"


def estimated_direct_filename(url):
    if is_kiwix_desktop_page(url):
        return "kiwix-desktop.AppImage"
    if "visualstudio.com/sha/download" in url:
        return "vscode-stable-linux-deb-x64.deb"
    parsed = urllib.parse.urlparse(url)
    name = Path(parsed.path).name
    return name or "downloaded-file"


def clean_folder_name(name):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-._")
    return cleaned or "download"


def direct_category_and_name(url, filename):
    lower = f"{url} {filename}".lower()
    if "visualstudio.com" in lower or "vscode" in lower:
        return "vscode", "vscode-stable"
    if filename.lower().endswith(".appimage") or "kiwix-desktop" in lower:
        if "kiwix" in lower:
            return "appimages", "kiwix"
        return "appimages", clean_folder_name(Path(filename).stem)
    if "firmware" in lower:
        return "firmware", clean_folder_name(Path(filename).stem)
    if filename.lower().endswith(".iso"):
        return "isos", clean_folder_name(Path(filename).stem)
    if filename.lower().endswith(".zim"):
        return "zim", clean_folder_name(Path(filename).stem)
    if "raspberrypi.org/imager" in lower or "raspberry-pi-imager" in lower or "imager_latest" in lower:
        return "packages", "raspberry-pi-imager"
    return "packages", clean_folder_name(Path(filename).stem)


def github_release_folder(save_root, url):
    if "TimAnderson1992/LinuxMintTaskManager" in url:
        return Path(save_root) / "packages" / "linux-mint-task-manager"
    return Path(save_root) / "packages" / clean_folder_name(Path(urllib.parse.urlparse(url).path).parts[-3])


def direct_folder(save_root, url, filename):
    category, download_name = direct_category_and_name(url, filename)
    return Path(save_root) / category / download_name


def item_destination_folder(save_root, item_type, url):
    root = Path(save_root).expanduser()
    if item_type == "github":
        return root / "github" / repo_name(url)
    if item_type == "github_release":
        return github_release_folder(root, url)
    if item_type == "linuxmint_iso" or is_linuxmint_download_page(url):
        return root / "isos" / "linuxmint"
    return direct_folder(root, url, estimated_direct_filename(url))


def nearest_existing_folder(path):
    path = Path(path).expanduser()
    current = path if path.is_dir() else path.parent
    while current and current != current.parent:
        if current.exists() and current.is_dir():
            return current
        current = current.parent
    return current if current.exists() and current.is_dir() else None


def legacy_direct_folder(save_root, url, filename):
    category, _download_name = direct_category_and_name(url, filename)
    return Path(save_root) / category


def migrate_legacy_direct_file(save_root, url, filename, new_dir):
    legacy_path = legacy_direct_folder(save_root, url, filename) / filename
    new_path = new_dir / filename
    if new_path.exists() or not legacy_path.exists() or legacy_path.parent == new_dir:
        return
    new_dir.mkdir(parents=True, exist_ok=True)
    legacy_path.replace(new_path)


def is_linuxmint_download_page(url):
    parsed = urllib.parse.urlparse(url)
    return parsed.netloc in {"linuxmint.com", "www.linuxmint.com"} and parsed.path == "/download_all.php"


def is_kiwix_desktop_page(url):
    return url.rstrip("/") == "https://download.kiwix.org/release/kiwix-desktop"


def natural_key(text):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"([0-9]+)", text)]


COL_ENABLED = 0
COL_NAME = 1
COL_TYPE = 2
COL_URL = 3
COL_STATUS = 4
COL_PROGRESS_VALUE = 5
COL_PROGRESS_TEXT = 6

STATUS_PREFIXES = (
    "Waiting",
    "Checking",
    "Skipped — already current",
    "Downloading — missing",
    "Updating — newer version found",
    "Complete",
    "Failed —",
    "Cancel pending",
)


def item_display_name(item):
    item_type = item.get("type", "direct")
    url = item.get("url", "")
    if item.get("name"):
        return item["name"]
    if item_type == "github":
        return repo_name(url)
    if item_type == "github_release":
        if "TimAnderson1992/LinuxMintTaskManager" in url:
            return "Linux Mint Task Manager DEB"
        return f"{repo_name(url)} release"
    if item_type == "linuxmint_iso" or is_linuxmint_download_page(url):
        return "Linux Mint Cinnamon ISO"
    if "visualstudio.com/sha/download" in url:
        return "vscode-stable"
    if "raspberrypi.org/imager" in url or "imager_latest" in url:
        return "raspberry-pi-imager"
    if "kiwix" in url.lower():
        return "kiwix"
    parsed = urllib.parse.urlparse(url)
    name = Path(parsed.path).stem or parsed.netloc or "download"
    return clean_folder_name(name)


def progress_text(fraction):
    if fraction is None:
        return ""
    return f"{int(max(0, min(1, fraction)) * 100)}%"


def is_row_status(text):
    return text.startswith(STATUS_PREFIXES)


class Downloader:
    def __init__(self, save_root, status_cb, progress_cb, dry_run=False):
        self.save_root = Path(save_root).expanduser()
        if not dry_run:
            self.save_root = ensure_download_dirs(save_root)
        self.status_cb = status_cb
        self.progress_cb = progress_cb
        self.dry_run = dry_run

    def status(self, text):
        self.status_cb(text)

    def progress(self, fraction=None, pulse=False, detail=""):
        self.progress_cb(fraction, pulse, detail)

    def run_item(self, item):
        item_type = item["type"]
        if item_type == "github":
            if self.dry_run:
                self.dry_run_github(item["url"])
            else:
                self.sync_github(item["url"])
        elif item_type == "direct":
            if self.dry_run:
                self.dry_run_direct(item["url"])
            else:
                self.download_direct(item["url"])
        elif item_type == "linuxmint_iso":
            if self.dry_run:
                self.dry_run_linuxmint_iso()
            else:
                self.download_linuxmint_iso()
        elif item_type == "github_release":
            if self.dry_run:
                self.dry_run_github_release(item["url"])
            else:
                self.download_github_release(item["url"])

    def sync_github(self, url):
        target = self.save_root / "github" / repo_name(url)
        self.status(f"GitHub item: {target.name}")
        self.status(f"GitHub repo URL: {url}")
        self.status(f"GitHub destination folder: {target}")
        if not (target / ".git").exists():
            self.status(f"GitHub action: clone {target.name}")
            self.status(f"Downloading — missing: {target.name}")
            self.progress(pulse=True)
            try:
                self.run_git(["git", "clone", url, str(target)], cwd=self.save_root)
            except Exception as exc:
                self.status(f"Failed — GitHub clone failed for {target.name}: {exc}")
                raise
            self.status(f"Complete: {target.name}")
            self.status(f"GitHub success: cloned {target.name}")
            return

        self.status(f"Checking {target.name}")
        self.status(f"GitHub action: fetch/pull check {target.name}")
        self.progress(pulse=True)
        try:
            self.run_git(["git", "fetch"], cwd=target)
        except Exception as exc:
            self.status(f"Failed — GitHub fetch failed for {target.name}: {exc}")
            raise

        local = self.git_output(["git", "rev-parse", "HEAD"], cwd=target)
        upstream = self.git_output(["git", "rev-parse", "@{u}"], cwd=target)

        if local and upstream and local == upstream:
            self.status(f"Skipped — already current: {target.name}")
            self.status(f"GitHub success: already current {target.name}")
            return

        self.status(f"Updating — newer version found: {target.name}")
        self.status(f"GitHub action: pull {target.name}")
        try:
            self.run_git(["git", "pull", "--ff-only"], cwd=target)
        except Exception as exc:
            self.status(f"Failed — GitHub pull failed for {target.name}: {exc}")
            raise
        self.status(f"Complete: {target.name}")
        self.status(f"GitHub success: updated {target.name}")

    def dry_run_github(self, url):
        target = self.save_root / "github" / repo_name(url)
        self.status(f"GitHub item: {target.name}")
        self.status(f"GitHub repo URL: {url}")
        self.status(f"GitHub destination folder: {target}")
        if not (target / ".git").exists():
            self.status(f"GitHub dry run: would clone {url} to {target}")
            self.status(f"Downloading — missing: {target.name}")
            return

        self.status(f"GitHub dry run: would check/pull {target}")
        local = self.git_output(["git", "rev-parse", "HEAD"], cwd=target)
        remote = self.git_output(["git", "ls-remote", url, "HEAD"], cwd=self.save_root)
        remote_head = remote.split()[0] if remote else ""

        if local and remote_head and local == remote_head:
            self.status(f"Skipped — already current: {target.name}")
        elif remote_head:
            self.status(f"GitHub dry run: would pull newer commits for {target.name}")
            self.status(f"Updating — newer version found: {target.name}")
        else:
            self.status(f"Failed — could not check remote HEAD: {target.name}")

    def run_git(self, args, cwd):
        process = subprocess.Popen(
            args,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert process.stdout is not None
        for line in process.stdout:
            line = line.strip()
            if line:
                self.status(line)
                self.progress(pulse=True)
        code = process.wait()
        if code != 0:
            raise RuntimeError(f"Command failed: {' '.join(args)}")

    def git_output(self, args, cwd):
        result = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    def download_direct(self, url):
        if is_linuxmint_download_page(url):
            self.download_linuxmint_iso()
            return

        if is_kiwix_desktop_page(url):
            url = self.latest_kiwix_appimage(url)
        elif url.endswith("/"):
            url = self.latest_kiwix_appimage(url)

        request = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
        with urllib.request.urlopen(request, timeout=60) as response:
            filename = direct_filename(url, response.headers)
            out_dir = direct_folder(self.save_root, url, filename)
            out_dir.mkdir(parents=True, exist_ok=True)
            migrate_legacy_direct_file(self.save_root, url, filename, out_dir)
            final_path = out_dir / filename
            if final_path.exists():
                self.status(f"Updating — newer version found: {filename}")
            else:
                self.status(f"Downloading — missing: {filename}")
            changed = self.download_to_temp_and_compare(response, final_path)

        if changed:
            self.status(f"Complete: {filename}")
            self.trim_versions(out_dir, filename, keep=1)
        else:
            self.status(f"Skipped — already current: {filename}")

    def dry_run_direct(self, url):
        if is_linuxmint_download_page(url):
            self.dry_run_linuxmint_iso()
            return

        if is_kiwix_desktop_page(url):
            url = self.latest_kiwix_appimage(url)
        elif url.endswith("/"):
            url = self.latest_kiwix_appimage(url)

        request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": APP_NAME})
        try:
            response = urllib.request.urlopen(request, timeout=60)
        except Exception:
            request = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
            response = urllib.request.urlopen(request, timeout=60)

        with response:
            filename = direct_filename(url, response.headers)
            out_dir = direct_folder(self.save_root, url, filename)
            final_path = out_dir / filename
            legacy_path = legacy_direct_folder(self.save_root, url, filename) / filename
            if not final_path.exists():
                if legacy_path.exists():
                    self.status(f"Skipped — already current: {filename} exists in old location")
                    return
                self.status(f"Downloading — missing: {filename}")
                return

            check_path = final_path
            remote_size = response.headers.get("Content-Length")
            local_size = check_path.stat().st_size
            if remote_size and remote_size.isdigit() and int(remote_size) != local_size:
                self.status(f"Updating — newer version found: {filename}")
                return

            remote_modified = response.headers.get("Last-Modified")
            if remote_modified:
                parsed = email.utils.parsedate(remote_modified)
                if parsed:
                    remote_dt = datetime.datetime(*parsed[:6])
                    local_dt = datetime.datetime.fromtimestamp(check_path.stat().st_mtime)
                    if remote_dt > local_dt:
                        self.status(f"Updating — newer version found: {filename}")
                        return

            self.status(f"Skipped — already current: {filename}")

    def download_github_release(self, url):
        asset = self.github_release_deb_asset(url)
        filename = asset["name"]
        out_dir = github_release_folder(self.save_root, url)
        out_dir.mkdir(parents=True, exist_ok=True)
        final_path = out_dir / filename

        if final_path.exists():
            self.status(f"Updating — newer version found: {filename}")
        else:
            self.status(f"Downloading — missing: {filename}")

        request = urllib.request.Request(asset["download_url"], headers={"User-Agent": APP_NAME})
        with urllib.request.urlopen(request, timeout=60) as response:
            changed = self.download_to_temp_and_compare(response, final_path)

        if changed:
            self.status(f"Complete: {filename}")
            self.trim_versions(out_dir, "*.deb", keep=1)
        else:
            self.status(f"Skipped — already current: {filename}")

    def dry_run_github_release(self, url):
        asset = self.github_release_deb_asset(url)
        filename = asset["name"]
        final_path = github_release_folder(self.save_root, url) / filename
        if final_path.exists():
            self.status(f"Skipped — already current: {filename}")
        else:
            self.status(f"Downloading — missing: {filename}")

    def github_release_deb_asset(self, release_url):
        api_url = self.github_release_api_url(release_url)
        try:
            request = urllib.request.Request(api_url, headers={"User-Agent": APP_NAME})
            with urllib.request.urlopen(request, timeout=60) as response:
                data = json.loads(response.read().decode("utf-8"))
            assets = data.get("assets", [])
            deb_assets = [
                asset for asset in assets
                if asset.get("name", "").endswith(".deb") and asset.get("browser_download_url")
            ]
            if deb_assets:
                asset = sorted(deb_assets, key=lambda item: natural_key(item["name"]))[-1]
                self.status(f"Found GitHub release asset: {asset['name']}")
                return {"name": asset["name"], "download_url": asset["browser_download_url"]}
        except Exception:
            pass

        html = self.read_url(release_url)
        matches = re.findall(
            r'href=["\']([^"\']+/releases/download/[^"\']+\.deb)["\']',
            html,
        )
        if not matches:
            raise RuntimeError("Could not detect a .deb release asset")
        asset_url = urllib.parse.urljoin(release_url, sorted(set(matches), key=natural_key)[-1])
        filename = Path(urllib.parse.urlparse(asset_url).path).name
        self.status(f"Found GitHub release asset: {filename}")
        return {"name": filename, "download_url": asset_url}

    def github_release_api_url(self, release_url):
        parsed = urllib.parse.urlparse(release_url)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 5 and parts[2] == "releases" and parts[3] == "tag":
            owner, repo, tag = parts[0], parts[1], parts[4]
            return f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"
        if len(parts) >= 2:
            owner, repo = parts[0], parts[1]
            return f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
        raise RuntimeError("Invalid GitHub release URL")

    def download_to_temp_and_compare(self, response, final_path):
        total = response.headers.get("Content-Length")
        total_size = int(total) if total and total.isdigit() else 0
        downloaded = 0

        with tempfile.NamedTemporaryFile(delete=False, dir=str(final_path.parent)) as tmp:
            tmp_path = Path(tmp.name)
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                tmp.write(chunk)
                downloaded += len(chunk)
                if total_size:
                    fraction = min(downloaded / total_size, 1.0)
                    percent = int(fraction * 100)
                    detail = f"{percent}% — {downloaded / 1048576:.1f} MB / {total_size / 1048576:.1f} MB"
                    self.progress(fraction, detail=detail)
                else:
                    self.progress(pulse=True)

        if final_path.exists() and filecmp.cmp(tmp_path, final_path, shallow=False):
            tmp_path.unlink(missing_ok=True)
            return False

        tmp_path.replace(final_path)
        return True

    def latest_kiwix_appimage(self, page_url):
        scan_page = page_url.rstrip("/") + "/"
        self.status(f"Scanning Kiwix page: {scan_page}")
        try:
            html = self.read_url(scan_page)
            hrefs = re.findall(r'href=["\']([^"\']+\.AppImage)["\']', html, flags=re.IGNORECASE)
            names = re.findall(r'kiwix-desktop[^"\'<> ]*x86_64[^"\'<> ]*\.AppImage', html, flags=re.IGNORECASE)
            candidates = list(hrefs) + names
            candidates = [
                candidate for candidate in candidates
                if "x86_64" in candidate.lower() and candidate.lower().endswith(".appimage")
            ]
            if not candidates:
                raise RuntimeError("no Linux x86_64 AppImage link found")

            selected = sorted(set(candidates), key=natural_key)[-1]
            filename = Path(urllib.parse.urlparse(selected).path).name or selected
            final_url = urllib.parse.urljoin(scan_page, selected)
            self.status(f"Found Kiwix AppImage: {filename}")
            self.status(f"Selected Kiwix URL: {final_url}")
            return final_url
        except Exception as exc:
            raise RuntimeError(f"Kiwix scan failed; page scanned: {scan_page}; reason: {exc}") from exc

    def download_linuxmint_iso(self):
        page_url = "https://linuxmint.com/download_all.php"
        out_dir = self.save_root / "isos" / "linuxmint"
        out_dir.mkdir(parents=True, exist_ok=True)

        resolved = self.resolve_linuxmint_iso(page_url)
        iso_name = resolved["filename"]
        iso_url = resolved["url"]
        final_path = out_dir / iso_name

        if final_path.exists():
            self.status(f"Skipped — already current: {iso_name}")
            return

        self.status(f"Downloading — missing: {iso_name}")
        try:
            self.download_url_to_path(iso_url, final_path)
        except Exception as exc:
            raise RuntimeError(
                "Linux Mint download failed; "
                f"page scanned: {page_url}; filename found: {iso_name}; "
                f"final URL selected: {iso_url}; reason: {exc}"
            ) from exc
        self.status(f"Complete: {iso_name}")
        self.trim_versions(out_dir, "linuxmint-*-cinnamon-64bit.iso", keep=2)

    def dry_run_linuxmint_iso(self):
        page_url = "https://linuxmint.com/download_all.php"
        out_dir = self.save_root / "isos" / "linuxmint"

        resolved = self.resolve_linuxmint_iso(page_url)
        iso_name = resolved["filename"]
        if (out_dir / iso_name).exists():
            self.status(f"Skipped — already current: {iso_name}")
        else:
            self.status(f"Downloading — missing: {iso_name}")

    def resolve_linuxmint_iso(self, page_url):
        self.status(f"Scanning Linux Mint page: {page_url}")
        iso_name = "not found"
        iso_url = ""
        try:
            html = self.read_url(page_url)
            matches = re.findall(r'linuxmint-[0-9][^"\'<> ]+-cinnamon-64bit\.iso', html)
            if matches:
                iso_name = sorted(set(matches), key=natural_key)[-1]
                self.status(f"Found Linux Mint ISO: {iso_name}")

                iso_url = self.find_iso_url_in_html(page_url, html, iso_name)
                if not iso_url:
                    linked_pages = self.linuxmint_candidate_pages(page_url, html)
                    for linked_page in linked_pages:
                        self.status(f"Scanning Linux Mint linked page: {linked_page}")
                        linked_html = self.read_url(linked_page)
                        iso_url = self.find_iso_url_in_html(linked_page, linked_html, iso_name)
                        if iso_url:
                            break
            else:
                self.status("Linux Mint download_all.php did not expose an ISO filename; scanning official mirror index")
                mirror = self.resolve_linuxmint_iso_from_official_mirror()
                iso_name = mirror["filename"]
                iso_url = mirror["url"]

            if not iso_url:
                raise RuntimeError("no real .iso mirror URL found")

            self.status(f"Selected Linux Mint ISO URL: {iso_url}")
            return {"filename": iso_name, "url": iso_url}
        except Exception as exc:
            raise RuntimeError(
                "Linux Mint scan failed; "
                f"page scanned: {page_url}; "
                f"filename found: {iso_name}; "
                f"final URL selected: {iso_url or 'not selected'}; "
                f"reason: {exc}"
            ) from exc

    def resolve_linuxmint_iso_from_official_mirror(self):
        root_url = "https://pub.linuxmint.io/stable/"
        self.status(f"Scanning Linux Mint page: {root_url}")
        root_html = self.read_url(root_url)
        versions = re.findall(r'href=["\']([0-9][0-9.]+/)["\']', root_html)
        versions = sorted(set(version.strip("/") for version in versions), key=natural_key, reverse=True)
        for version in versions:
            version_url = urllib.parse.urljoin(root_url, f"{version}/")
            self.status(f"Scanning Linux Mint linked page: {version_url}")
            html = self.read_url(version_url)
            matches = re.findall(r'linuxmint-[0-9][^"\'<> ]+-cinnamon-64bit\.iso', html)
            if not matches:
                continue
            iso_name = sorted(set(matches), key=natural_key)[-1]
            iso_url = urllib.parse.urljoin(version_url, iso_name)
            self.status(f"Found Linux Mint ISO: {iso_name}")
            return {"filename": iso_name, "url": iso_url}
        raise RuntimeError(f"no Cinnamon 64-bit ISO filename found in {root_url}")

    def find_iso_url_in_html(self, base_url, html, iso_name):
        absolute_matches = re.findall(
            r'https?://[^"\'<> ]+/' + re.escape(iso_name),
            html,
        )
        if absolute_matches:
            return sorted(set(absolute_matches), key=natural_key)[-1]

        relative_matches = re.findall(
            r'href=["\']([^"\']*' + re.escape(iso_name) + r')["\']',
            html,
        )
        if relative_matches:
            return urllib.parse.urljoin(base_url, sorted(set(relative_matches), key=natural_key)[-1])
        return ""

    def linuxmint_candidate_pages(self, base_url, html):
        hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, flags=re.IGNORECASE)
        candidates = []
        for href in hrefs:
            lower = href.lower()
            if "edition.php" in lower or "download.php" in lower or "mirrors" in lower:
                url = urllib.parse.urljoin(base_url, href)
                if url not in candidates:
                    candidates.append(url)
        return candidates

    def read_url(self, url):
        request = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read()
            if response.headers.get("Content-Encoding") == "gzip" or data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            return data.decode("utf-8", errors="replace")

    def download_url_to_path(self, url, final_path):
        request = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
        with urllib.request.urlopen(request, timeout=60) as response:
            self.download_to_temp_and_compare(response, final_path)

    def trim_versions(self, out_dir, pattern, keep):
        files = sorted(out_dir.glob(pattern), key=lambda path: path.name)
        old_files = files[:-keep] if keep else files
        for path in old_files:
            path.unlink(missing_ok=True)
            self.status(f"Removed old file: {path.name}")


def run_scheduled_check():
    config = load_config()
    if not scheduled_download_due(config):
        print("Skipped — already current: scheduled download is not due")
        return 0

    items = [item for item in config.get("items", []) if item.get("enabled")]
    if not items:
        print("Complete: no enabled downloads")
        mark_scheduled_run_complete(config)
        return 0

    downloader = Downloader(
        config["save_root"],
        lambda text: print(text, flush=True),
        lambda _fraction=None, pulse=False, detail="": None,
        dry_run=False,
    )

    for index, item in enumerate(items, start=1):
        print(f"Item {index} of {len(items)}: {item['type']} {item['url']}", flush=True)
        try:
            downloader.run_item(item)
        except Exception as exc:
            print(f"Failed — {exc}", flush=True)

    mark_scheduled_run_complete(config)
    print("Complete", flush=True)
    return 0


class OfflineDownloaderApp(Gtk.Window):
    def __init__(self):
        super().__init__(title=APP_NAME)
        self.set_default_size(980, 620)
        self.config = load_config()
        self.worker = None
        self.cancel_requested = threading.Event()
        self.active_row_index = None

        self.store = Gtk.ListStore(bool, str, str, str, str, int, str)
        self.build_ui()
        self.load_items()
        self.load_schedule()
        self.connect("destroy", Gtk.main_quit)
        GLib.idle_add(self.validate_download_items)
        GLib.idle_add(self.show_schedule_prompt)

    def build_ui(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.set_border_width(10)
        self.add(outer)

        folder_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        outer.pack_start(folder_row, False, False, 0)

        folder_row.pack_start(Gtk.Label(label="Save folder"), False, False, 0)
        self.folder_button = Gtk.FileChooserButton(title="Choose save folder")
        self.folder_button.set_action(Gtk.FileChooserAction.SELECT_FOLDER)
        self.folder_button.set_filename(str(Path(self.config["save_root"]).expanduser()))
        self.folder_button.connect("file-set", self.on_folder_changed)
        folder_row.pack_start(self.folder_button, True, True, 0)

        schedule_frame = Gtk.Frame(label="Monthly schedule")
        outer.pack_start(schedule_frame, False, False, 0)
        schedule_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        schedule_box.set_border_width(8)
        schedule_frame.add(schedule_box)

        schedule_box.pack_start(Gtk.Label(label="Day"), False, False, 0)
        self.schedule_day = Gtk.SpinButton()
        self.schedule_day.set_adjustment(Gtk.Adjustment(15, 1, 28, 1, 5, 0))
        self.schedule_day.set_numeric(True)
        self.schedule_day.connect("value-changed", self.on_schedule_changed)
        schedule_box.pack_start(self.schedule_day, False, False, 0)

        schedule_box.pack_start(Gtk.Label(label="Time"), False, False, 0)
        self.schedule_hour = Gtk.SpinButton()
        self.schedule_hour.set_adjustment(Gtk.Adjustment(2, 0, 23, 1, 4, 0))
        self.schedule_hour.set_numeric(True)
        self.schedule_hour.connect("value-changed", self.on_schedule_changed)
        schedule_box.pack_start(self.schedule_hour, False, False, 0)

        schedule_box.pack_start(Gtk.Label(label=":"), False, False, 0)
        self.schedule_minute = Gtk.SpinButton()
        self.schedule_minute.set_adjustment(Gtk.Adjustment(0, 0, 59, 1, 10, 0))
        self.schedule_minute.set_numeric(True)
        self.schedule_minute.connect("value-changed", self.on_schedule_changed)
        schedule_box.pack_start(self.schedule_minute, False, False, 0)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        outer.pack_start(toolbar, False, False, 0)

        timer_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        outer.pack_start(timer_row, False, False, 0)

        self.enable_timer_button = Gtk.Button(label="Enable Monthly Auto-Check")
        self.enable_timer_button.connect("clicked", self.enable_monthly_auto_check)
        timer_row.pack_start(self.enable_timer_button, False, False, 0)

        self.disable_timer_button = Gtk.Button(label="Disable Monthly Auto-Check")
        self.disable_timer_button.connect("clicked", self.disable_monthly_auto_check)
        timer_row.pack_start(self.disable_timer_button, False, False, 0)

        self.check_app_updates_button = Gtk.Button(label="Check for App Updates")
        self.check_app_updates_button.connect("clicked", self.check_for_app_updates)
        timer_row.pack_start(self.check_app_updates_button, False, False, 0)

        self.add_github_button = Gtk.Button(label="Add GitHub Repo")
        self.add_github_button.connect("clicked", self.add_github)
        toolbar.pack_start(self.add_github_button, False, False, 0)

        self.add_direct_button = Gtk.Button(label="Add Direct File URL")
        self.add_direct_button.connect("clicked", self.add_direct)
        toolbar.pack_start(self.add_direct_button, False, False, 0)

        self.remove_button = Gtk.Button(label="Remove Selected")
        self.remove_button.connect("clicked", self.remove_selected)
        toolbar.pack_start(self.remove_button, False, False, 0)

        self.open_folder_button = Gtk.Button(label="Open Folder")
        self.open_folder_button.connect("clicked", self.open_selected_folder)
        toolbar.pack_start(self.open_folder_button, False, False, 0)

        self.dry_run_button = Gtk.Button(label="Dry Run")
        self.dry_run_button.connect("clicked", self.start_dry_run)
        toolbar.pack_end(self.dry_run_button, False, False, 0)

        self.cancel_button = Gtk.Button(label="Cancel")
        self.cancel_button.connect("clicked", self.cancel_downloads)
        self.cancel_button.set_sensitive(False)
        toolbar.pack_end(self.cancel_button, False, False, 0)

        self.download_button = Gtk.Button(label="Download Now")
        self.download_button.connect("clicked", self.start_downloads)
        toolbar.pack_end(self.download_button, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        outer.pack_start(scroller, True, True, 0)

        self.tree = Gtk.TreeView(model=self.store)
        self.tree.connect("button-press-event", self.on_tree_button_press)
        scroller.add(self.tree)

        enabled_renderer = Gtk.CellRendererToggle()
        enabled_renderer.connect("toggled", self.on_enabled_toggled)
        enabled_col = Gtk.TreeViewColumn("Enabled", enabled_renderer, active=COL_ENABLED)
        self.tree.append_column(enabled_col)

        name_renderer = Gtk.CellRendererText()
        name_col = Gtk.TreeViewColumn("Item", name_renderer, text=COL_NAME)
        self.tree.append_column(name_col)

        type_renderer = Gtk.CellRendererText()
        type_col = Gtk.TreeViewColumn("Type", type_renderer, text=COL_TYPE)
        self.tree.append_column(type_col)

        url_renderer = Gtk.CellRendererText()
        url_renderer.set_property("ellipsize", 3)
        url_col = Gtk.TreeViewColumn("URL", url_renderer, text=COL_URL)
        url_col.set_expand(True)
        self.tree.append_column(url_col)

        status_renderer = Gtk.CellRendererText()
        status_col = Gtk.TreeViewColumn("Status", status_renderer, text=COL_STATUS)
        status_col.set_min_width(220)
        self.tree.append_column(status_col)

        row_progress_renderer = Gtk.CellRendererProgress()
        row_progress_col = Gtk.TreeViewColumn(
            "Progress",
            row_progress_renderer,
            value=COL_PROGRESS_VALUE,
            text=COL_PROGRESS_TEXT,
        )
        row_progress_col.set_min_width(240)
        self.tree.append_column(row_progress_col)

        status_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.pack_start(status_box, False, False, 0)

        self.overall_label = Gtk.Label(label="Overall: 0 of 0 complete")
        self.overall_label.set_xalign(0)
        status_box.pack_start(self.overall_label, False, False, 0)

        self.overall_progress_bar = Gtk.ProgressBar()
        self.overall_progress_bar.set_show_text(True)
        status_box.pack_start(self.overall_progress_bar, False, False, 0)

        log_scroller = Gtk.ScrolledWindow()
        log_scroller.set_size_request(-1, 120)
        status_box.pack_start(log_scroller, False, True, 0)

        self.status_log = Gtk.TextView()
        self.status_log.set_editable(False)
        self.status_log.set_cursor_visible(False)
        self.status_log.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        log_scroller.add(self.status_log)

    def load_items(self):
        self.store.clear()
        for item in self.config["items"]:
            self.store.append(
                [
                    bool(item.get("enabled", True)),
                    item_display_name(item),
                    item.get("type", "direct"),
                    item.get("url", ""),
                    "Waiting",
                    0,
                    "",
                ]
            )

    def load_schedule(self):
        schedule = self.config.get("monthly_schedule", {})
        self.schedule_day.set_value(clamp(schedule.get("day"), 1, 28, 15))
        self.schedule_hour.set_value(clamp(schedule.get("hour"), 0, 23, 2))
        self.schedule_minute.set_value(clamp(schedule.get("minute"), 0, 59, 0))

    def config_from_ui(self):
        items = []
        for row in self.store:
            item = {"enabled": bool(row[COL_ENABLED]), "type": row[COL_TYPE], "url": row[COL_URL]}
            if row[COL_TYPE] == "github_release":
                item["name"] = row[COL_NAME]
            items.append(item)
        self.config["items"] = items
        filename = self.folder_button.get_filename()
        if filename:
            self.config["save_root"] = filename
        self.config["monthly_schedule"] = {
            "day": int(self.schedule_day.get_value()),
            "hour": int(self.schedule_hour.get_value()),
            "minute": int(self.schedule_minute.get_value()),
        }
        return self.config

    def save_from_ui(self):
        save_config(self.config_from_ui())

    def on_folder_changed(self, _button):
        self.save_from_ui()

    def on_schedule_changed(self, _button):
        self.save_from_ui()

    def on_enabled_toggled(self, _renderer, path):
        self.store[path][COL_ENABLED] = not self.store[path][COL_ENABLED]
        self.save_from_ui()

    def add_github(self, _button):
        self.add_url_dialog("github", "Add GitHub repository URL")

    def add_direct(self, _button):
        self.add_url_dialog("direct", "Add direct file URL")

    def add_url_dialog(self, item_type, title):
        dialog = Gtk.Dialog(title=title, transient_for=self, flags=0)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Add", Gtk.ResponseType.OK)
        entry = Gtk.Entry()
        entry.set_activates_default(True)
        entry.set_hexpand(True)
        box = dialog.get_content_area()
        box.set_spacing(8)
        box.set_border_width(10)
        box.add(entry)
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.show_all()

        response = dialog.run()
        url = entry.get_text().strip()
        dialog.destroy()

        if response == Gtk.ResponseType.OK and url:
            item = {"enabled": True, "type": item_type, "url": url}
            self.store.append([True, item_display_name(item), item_type, url, "Waiting", 0, ""])
            self.save_from_ui()

    def remove_selected(self, _button):
        selection = self.tree.get_selection()
        model, iterator = selection.get_selected()
        if iterator:
            model.remove(iterator)
            self.save_from_ui()

    def open_selected_folder(self, _button):
        selection = self.tree.get_selection()
        model, iterator = selection.get_selected()
        if not iterator:
            self.set_status("Folder not found: no download item selected")
            return
        self.open_folder_for_row(model[iterator])

    def on_tree_button_press(self, tree, event):
        if event.button != 3:
            return False
        path_info = tree.get_path_at_pos(int(event.x), int(event.y))
        if path_info:
            path, _column, _cell_x, _cell_y = path_info
            tree.get_selection().select_path(path)
        menu = Gtk.Menu()
        open_item = Gtk.MenuItem(label="Open Folder")
        open_item.connect("activate", self.open_selected_folder)
        menu.append(open_item)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def open_folder_for_row(self, row):
        target = item_destination_folder(self.config["save_root"], row[COL_TYPE], row[COL_URL])
        folder = nearest_existing_folder(target)
        if not folder:
            self.set_status(f"Folder not found: {target}")
            return
        self.set_status(f"Opening folder: {folder}")
        try:
            self.launch_folder(folder)
        except Exception as exc:
            self.set_status(f"Failed — could not open folder {folder}: {exc}")

    def launch_folder(self, folder):
        if sys.platform.startswith("win"):
            cmd = ["explorer", str(folder)]
        elif sys.platform == "darwin":
            cmd = ["open", str(folder)]
        else:
            cmd = ["xdg-open", str(folder)]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def start_downloads(self, _button):
        self.start_queue(dry_run=False)

    def start_dry_run(self, _button):
        self.start_queue(dry_run=True)

    def start_queue(self, dry_run, mark_scheduled=False):
        if self.worker and self.worker.is_alive():
            self.set_status("Downloads are already running")
            return

        self.save_from_ui()
        items = []
        for row_index, row in enumerate(self.store):
            row[COL_STATUS] = "Waiting"
            row[COL_PROGRESS_VALUE] = 0
            row[COL_PROGRESS_TEXT] = ""
            if row[COL_ENABLED]:
                items.append(
                    {
                        "enabled": True,
                        "name": row[COL_NAME],
                        "type": row[COL_TYPE],
                        "url": row[COL_URL],
                        "row_index": row_index,
                    }
                )
        if not items:
            self.set_status("No enabled downloads")
            return

        self.cancel_requested.clear()
        self.set_controls_sensitive(False)
        self.cancel_button.set_sensitive(True)
        self.completed_items = 0
        self.total_items = len(items)
        self.update_overall_progress(0, self.total_items)
        self.clear_status_log()
        mode = "dry run" if dry_run else "download"
        self.set_status(f"Starting {mode}")
        self.worker = threading.Thread(
            target=self.download_worker,
            args=(items, dry_run, mark_scheduled),
            daemon=True,
        )
        self.worker.start()

    def cancel_downloads(self, _button):
        self.cancel_requested.set()
        self.set_status("Cancel pending")
        for row in self.store:
            if row[COL_ENABLED] and row[COL_STATUS] == "Waiting":
                row[COL_STATUS] = "Cancel pending"
        if self.valid_row_index(self.active_row_index):
            self.store[self.active_row_index][COL_STATUS] = "Cancel pending"

    def download_worker(self, items, dry_run, mark_scheduled):
        current = {"row_index": None}
        downloader = Downloader(
            self.config["save_root"],
            lambda text: GLib.idle_add(self.set_item_status, current["row_index"], text),
            lambda fraction, pulse=False, detail="": GLib.idle_add(
                self.set_item_progress,
                current["row_index"],
                fraction,
                pulse,
                detail,
                dry_run,
            ),
            dry_run=dry_run,
        )

        total = len(items)
        for index, item in enumerate(items, start=1):
            if self.cancel_requested.is_set():
                GLib.idle_add(self.set_status, "Complete: canceled after current item")
                break
            current["row_index"] = item["row_index"]
            try:
                GLib.idle_add(
                    self.start_item_progress,
                    item["row_index"],
                    item["name"],
                    index,
                    total,
                    dry_run,
                )
                downloader.run_item(item)
                GLib.idle_add(self.finish_item_progress, item["row_index"], index, total)
            except Exception as exc:
                GLib.idle_add(self.set_item_status, item["row_index"], f"Failed — {exc}")
                GLib.idle_add(self.finish_item_progress, item["row_index"], index, total)

        GLib.idle_add(self.finish_downloads, mark_scheduled)

    def set_status(self, text):
        self.append_status_log(text)
        return False

    def append_status_log(self, text):
        if not hasattr(self, "status_log"):
            return
        buffer = self.status_log.get_buffer()
        end_iter = buffer.get_end_iter()
        buffer.insert(end_iter, f"{text}\n")
        mark = buffer.create_mark(None, buffer.get_end_iter(), False)
        self.status_log.scroll_mark_onscreen(mark)

    def clear_status_log(self):
        self.status_log.get_buffer().set_text("")

    def validate_download_items(self):
        for row in self.store:
            item_type = row[COL_TYPE]
            url = row[COL_URL]
            if "github.com" in url and item_type not in {"github", "github_release"}:
                self.set_status(
                    f"Warning — GitHub URL has unexpected type '{item_type}': {url}"
                )
        return False

    def start_item_progress(self, row_index, name, index, total, dry_run):
        self.update_overall_progress(index - 1, total)
        if self.valid_row_index(row_index):
            self.store[row_index][COL_STATUS] = "Checking"
            self.store[row_index][COL_PROGRESS_VALUE] = 0
            self.store[row_index][COL_PROGRESS_TEXT] = "" if dry_run else "Checking"
        self.active_row_index = row_index
        self.set_status(f"Checking: {name}")
        return False

    def set_item_status(self, row_index, text):
        self.append_status_log(text)
        if self.valid_row_index(row_index) and is_row_status(text):
            self.store[row_index][COL_STATUS] = text
            if text.startswith("Complete") or text.startswith("Skipped"):
                self.store[row_index][COL_PROGRESS_VALUE] = 100
                self.store[row_index][COL_PROGRESS_TEXT] = "Done" if text.startswith("Complete") else "Current"
            elif text.startswith("Failed"):
                self.store[row_index][COL_PROGRESS_TEXT] = "Failed"
            elif text.startswith("Downloading"):
                self.store[row_index][COL_PROGRESS_VALUE] = max(self.store[row_index][COL_PROGRESS_VALUE], 5)
                self.store[row_index][COL_PROGRESS_TEXT] = "Downloading"
            elif text.startswith("Updating"):
                self.store[row_index][COL_PROGRESS_VALUE] = max(self.store[row_index][COL_PROGRESS_VALUE], 5)
                self.store[row_index][COL_PROGRESS_TEXT] = "Updating"
            elif text.startswith("Checking"):
                self.store[row_index][COL_PROGRESS_TEXT] = "Checking"
        return False

    def set_item_progress(self, row_index, fraction, pulse=False, detail="", dry_run=False):
        if dry_run:
            return False
        if pulse:
            if self.valid_row_index(row_index):
                status = self.store[row_index][COL_STATUS]
                if str(status).startswith("Downloading"):
                    self.store[row_index][COL_PROGRESS_TEXT] = "Downloading"
                elif str(status).startswith("Updating"):
                    self.store[row_index][COL_PROGRESS_TEXT] = "Updating"
                else:
                    self.store[row_index][COL_PROGRESS_TEXT] = "Checking"
        elif fraction is not None:
            fraction = max(0.0, min(1.0, float(fraction)))
            percent = int(fraction * 100)
            text = detail or f"{percent}%"
            if self.valid_row_index(row_index):
                self.store[row_index][COL_PROGRESS_VALUE] = percent
                self.store[row_index][COL_PROGRESS_TEXT] = text
        return False

    def finish_item_progress(self, row_index, index, total):
        if self.valid_row_index(row_index):
            status = self.store[row_index][COL_STATUS]
            if not str(status).startswith("Failed"):
                self.store[row_index][COL_PROGRESS_VALUE] = 100
                self.store[row_index][COL_PROGRESS_TEXT] = "Done"
        self.update_overall_progress(index, total)
        return False

    def update_overall_progress(self, complete, total):
        self.overall_label.set_text(f"Overall: {complete} of {total} complete")
        self.overall_progress_bar.set_fraction((complete / total) if total else 0)
        self.overall_progress_bar.set_text(f"{complete} / {total}" if total else "")
        return False

    def valid_row_index(self, row_index):
        return row_index is not None and 0 <= row_index < len(self.store)

    def set_progress(self, fraction, pulse=False, detail=""):
        return False

    def finish_downloads(self, mark_scheduled=False):
        if mark_scheduled:
            self.config["last_scheduled_run_date"] = datetime.date.today().isoformat()
            save_config(self.config)
        self.set_controls_sensitive(True)
        self.cancel_button.set_sensitive(False)
        self.active_row_index = None
        self.set_status("Complete")
        return False

    def set_controls_sensitive(self, sensitive):
        for widget in [
            self.folder_button,
            self.schedule_day,
            self.schedule_hour,
            self.schedule_minute,
            self.enable_timer_button,
            self.disable_timer_button,
            self.check_app_updates_button,
            self.add_github_button,
            self.add_direct_button,
            self.remove_button,
            self.open_folder_button,
            self.download_button,
            self.dry_run_button,
            self.tree,
        ]:
            widget.set_sensitive(sensitive)

    def enable_monthly_auto_check(self, _button):
        try:
            self.save_from_ui()
            enable_user_timer()
            self.set_status("Complete: monthly auto-check enabled")
        except Exception as exc:
            self.set_status(f"Failed — {exc}")

    def disable_monthly_auto_check(self, _button):
        try:
            disable_user_timer()
            self.set_status("Complete: monthly auto-check disabled")
        except Exception as exc:
            self.set_status(f"Failed — {exc}")

    def check_for_app_updates(self, _button):
        try:
            status = app_git_update_status()
            if not status.get("git_clone"):
                self.check_deb_app_updates()
                return

            if status.get("wrong_remote"):
                self.show_info_dialog(
                    "App update check",
                    f"This git checkout does not use the expected app remote:\n{status['wrong_remote']}",
                )
                self.set_status("Failed — app git remote is not the Offline Downloader repo")
                return

            if not status.get("update_available"):
                self.show_info_dialog("App update check", "Offline Downloader is already current.")
                self.set_status("Offline Downloader is already current.")
                return

            if self.ask_yes_no("Update available", "Update Offline Downloader from origin/main now?"):
                output = pull_app_update()
                self.show_info_dialog("App update complete", output or "Offline Downloader was updated.")
                self.set_status("Complete: app update applied")
            else:
                self.set_status("Complete: app update skipped")
        except Exception as exc:
            self.show_info_dialog("App update failed", str(exc))
            self.set_status(f"Failed — {exc}")

    def check_deb_app_updates(self):
        release = latest_deb_release_status()
        base_message = (
            "This copy was installed from a .deb package. App updates are checked from GitHub Releases, not git pull."
        )
        if not release["newer"]:
            self.show_info_dialog(
                "App update check",
                f"{base_message}\n\nOffline Downloader is already current.\n\nInstalled version: {APP_VERSION}\nLatest release: {release['tag'] or 'unknown'}\n{release['release_url']}",
            )
            self.set_status("Offline Downloader is already current.")
            return

        asset_line = release["asset_url"] or release["release_url"]
        message = (
            f"{base_message}\n\n"
            "A new .deb package is available.\n\n"
            f"Installed version: {APP_VERSION}\n"
            f"Latest release: {release['tag']}\n"
            f"{asset_line}\n\n"
            "Download the newer .deb into packages/offline-downloader/ under your selected save folder?"
        )
        if self.ask_yes_no("A new .deb package is available.", message, ok_label="Download"):
            if not release["asset_url"]:
                self.show_info_dialog("App update check", f"No .deb asset was found.\n\n{release['release_url']}")
                self.set_status("Failed — no .deb release asset found")
                return
            self.download_app_deb_release(release["asset_url"], release["asset_name"])
        else:
            self.set_status("Complete: app .deb update skipped")

    def download_app_deb_release(self, asset_url, asset_name):
        self.save_from_ui()
        out_dir = Path(self.config["save_root"]).expanduser() / "packages" / "offline-downloader"
        out_dir.mkdir(parents=True, exist_ok=True)
        final_path = out_dir / asset_name
        downloader = Downloader(
            self.config["save_root"],
            lambda text: GLib.idle_add(self.set_status, text),
            lambda _fraction=None, pulse=False, detail="": None,
            dry_run=False,
        )
        request = urllib.request.Request(asset_url, headers={"User-Agent": APP_NAME})
        self.set_status(f"Downloading — missing: {asset_name}")
        with urllib.request.urlopen(request, timeout=60) as response:
            changed = downloader.download_to_temp_and_compare(response, final_path)
        if changed:
            downloader.trim_versions(out_dir, "*.deb", keep=1)
            self.set_status(f"Complete: downloaded {final_path}")
        else:
            self.set_status(f"Skipped — already current: {asset_name}")

    def ask_yes_no(self, title, message, ok_label="Update"):
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text=title,
        )
        dialog.format_secondary_text(message)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button(ok_label, Gtk.ResponseType.OK)
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.OK

    def show_info_dialog(self, title, message):
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text=title,
        )
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()

    def show_schedule_prompt(self):
        if not scheduled_download_due(self.config):
            return False

        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text="Scheduled download is due. Start downloads now?",
        )
        dialog.add_button("Not Now", Gtk.ResponseType.CANCEL)
        dialog.add_button("Start Downloads", Gtk.ResponseType.OK)
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.OK:
            self.start_queue(dry_run=False, mark_scheduled=True)
        return False


if __name__ == "__main__":
    if "--scheduled-check" in sys.argv:
        raise SystemExit(run_scheduled_check())

    app = OfflineDownloaderApp()
    app.show_all()
    Gtk.main()
