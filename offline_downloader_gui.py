#!/usr/bin/env python3

import datetime
import email.utils
import filecmp
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
]

DEFAULT_DIRECT_URLS = [
    "https://code.visualstudio.com/sha/download?build=stable&os=linux-deb-x64",
    "https://downloads.raspberrypi.org/imager/imager_latest_amd64.deb",
    "https://download.kiwix.org/release/kiwix-desktop/",
]


def default_config():
    items = []
    for url in DEFAULT_REPOS:
        items.append({"enabled": True, "type": "github", "url": url})
    for url in DEFAULT_DIRECT_URLS:
        items.append({"enabled": True, "type": "direct", "url": url})
    items.append(
        {
            "enabled": True,
            "type": "linuxmint_iso",
            "url": "https://linuxmint.com/download_all.php",
        }
    )

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
    ]:
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def load_config():
    if not CONFIG_FILE.exists():
        return default_config()
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
    data.pop("scheduled_days", None)
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


def direct_folder(save_root, url, filename):
    lower = f"{url} {filename}".lower()
    root = Path(save_root)
    if "visualstudio.com" in lower or "vscode" in lower:
        return root / "vscode"
    if filename.lower().endswith(".appimage") or "kiwix-desktop" in lower:
        return root / "appimages"
    if "firmware" in lower:
        return root / "firmware"
    if filename.lower().endswith(".iso"):
        return root / "isos"
    return root / "packages"


def is_linuxmint_download_page(url):
    parsed = urllib.parse.urlparse(url)
    return parsed.netloc in {"linuxmint.com", "www.linuxmint.com"} and parsed.path == "/download_all.php"


def natural_key(text):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"([0-9]+)", text)]


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

    def progress(self, fraction=None, pulse=False):
        self.progress_cb(fraction, pulse)

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

    def sync_github(self, url):
        target = self.save_root / "github" / repo_name(url)
        if not (target / ".git").exists():
            self.status(f"Downloading — missing: {target.name}")
            self.progress(pulse=True)
            self.run_git(["git", "clone", url, str(target)], cwd=self.save_root)
            self.status(f"Complete: {target.name}")
            return

        self.status(f"Checking {target.name}")
        self.progress(pulse=True)
        self.run_git(["git", "fetch"], cwd=target)

        local = self.git_output(["git", "rev-parse", "HEAD"], cwd=target)
        upstream = self.git_output(["git", "rev-parse", "@{u}"], cwd=target)

        if local and upstream and local == upstream:
            self.status(f"Skipped — already current: {target.name}")
            return

        self.status(f"Updating — newer version found: {target.name}")
        self.run_git(["git", "pull", "--ff-only"], cwd=target)
        self.status(f"Complete: {target.name}")

    def dry_run_github(self, url):
        target = self.save_root / "github" / repo_name(url)
        if not (target / ".git").exists():
            self.status(f"Downloading — missing: {target.name}")
            return

        local = self.git_output(["git", "rev-parse", "HEAD"], cwd=target)
        remote = self.git_output(["git", "ls-remote", url, "HEAD"], cwd=self.save_root)
        remote_head = remote.split()[0] if remote else ""

        if local and remote_head and local == remote_head:
            self.status(f"Skipped — already current: {target.name}")
        elif remote_head:
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

        if url.rstrip("/") == "https://download.kiwix.org/release/kiwix-desktop":
            url = url + "/"

        if url.endswith("/"):
            url = self.latest_kiwix_appimage(url)

        request = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
        with urllib.request.urlopen(request, timeout=60) as response:
            filename = direct_filename(url, response.headers)
            out_dir = direct_folder(self.save_root, url, filename)
            out_dir.mkdir(parents=True, exist_ok=True)
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

        if url.rstrip("/") == "https://download.kiwix.org/release/kiwix-desktop":
            url = url + "/"

        if url.endswith("/"):
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
            if not final_path.exists():
                self.status(f"Downloading — missing: {filename}")
                return

            remote_size = response.headers.get("Content-Length")
            local_size = final_path.stat().st_size
            if remote_size and remote_size.isdigit() and int(remote_size) != local_size:
                self.status(f"Updating — newer version found: {filename}")
                return

            remote_modified = response.headers.get("Last-Modified")
            if remote_modified:
                parsed = email.utils.parsedate(remote_modified)
                if parsed:
                    remote_dt = datetime.datetime(*parsed[:6])
                    local_dt = datetime.datetime.fromtimestamp(final_path.stat().st_mtime)
                    if remote_dt > local_dt:
                        self.status(f"Updating — newer version found: {filename}")
                        return

            self.status(f"Skipped — already current: {filename}")

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
                    self.progress(min(downloaded / total_size, 1.0))
                else:
                    self.progress(pulse=True)

        if final_path.exists() and filecmp.cmp(tmp_path, final_path, shallow=False):
            tmp_path.unlink(missing_ok=True)
            return False

        tmp_path.replace(final_path)
        return True

    def latest_kiwix_appimage(self, page_url):
        self.status("Checking newest Kiwix AppImage")
        html = self.read_url(page_url)
        matches = re.findall(r'kiwix-desktop[^"<> ]*x86_64\.AppImage', html)
        if not matches:
            raise RuntimeError("Could not detect Kiwix AppImage filename")
        name = sorted(set(matches))[-1]
        return urllib.parse.urljoin(page_url, name)

    def download_linuxmint_iso(self):
        page_url = "https://linuxmint.com/download_all.php"
        out_dir = self.save_root / "isos" / "linuxmint"
        out_dir.mkdir(parents=True, exist_ok=True)

        self.status("Checking newest Linux Mint Cinnamon 64-bit ISO")
        html = self.read_url(page_url)
        matches = re.findall(r'linuxmint-[0-9][^"\'<> ]+-cinnamon-64bit\.iso', html)
        if not matches:
            raise RuntimeError("Could not detect Linux Mint Cinnamon 64-bit ISO filename")

        iso_name = sorted(set(matches), key=natural_key)[-1]
        self.status(f"Found Linux Mint ISO: {iso_name}")
        final_path = out_dir / iso_name

        if final_path.exists():
            self.status(f"Skipped — already current: {iso_name}")
            return

        url_matches = re.findall(
            r'https?://[^"\'<> ]+/linuxmint-[0-9][^"\'<> ]+-cinnamon-64bit\.iso',
            html,
        )
        iso_url = next((url for url in url_matches if url.endswith(iso_name)), "")
        if not iso_url:
            relative_matches = re.findall(
                r'href=["\']([^"\']*linuxmint-[0-9][^"\']+-cinnamon-64bit\.iso)["\']',
                html,
            )
            iso_url = next(
                (
                    urllib.parse.urljoin(page_url, url)
                    for url in relative_matches
                    if url.endswith(iso_name)
                ),
                "",
            )

        if not iso_url:
            raise RuntimeError("Could not detect a Linux Mint ISO download URL on the page")

        self.status(f"Downloading — missing: {iso_name}")
        self.download_url_to_path(iso_url, final_path)
        self.status(f"Complete: {iso_name}")
        self.trim_versions(out_dir, "linuxmint-*-cinnamon-64bit.iso", keep=2)

    def dry_run_linuxmint_iso(self):
        page_url = "https://linuxmint.com/download_all.php"
        out_dir = self.save_root / "isos" / "linuxmint"

        self.status("Checking newest Linux Mint Cinnamon 64-bit ISO")
        html = self.read_url(page_url)
        matches = re.findall(r'linuxmint-[0-9][^"\'<> ]+-cinnamon-64bit\.iso', html)
        if not matches:
            raise RuntimeError("Could not detect Linux Mint Cinnamon 64-bit ISO filename")

        iso_name = sorted(set(matches), key=natural_key)[-1]
        self.status(f"Found Linux Mint ISO: {iso_name}")
        if (out_dir / iso_name).exists():
            self.status(f"Skipped — already current: {iso_name}")
        else:
            self.status(f"Downloading — missing: {iso_name}")

    def read_url(self, url):
        request = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8", errors="replace")

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
        lambda _fraction=None, pulse=False: None,
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

        self.store = Gtk.ListStore(bool, str, str)
        self.build_ui()
        self.load_items()
        self.load_schedule()
        self.connect("destroy", Gtk.main_quit)
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

        self.add_github_button = Gtk.Button(label="Add GitHub Repo")
        self.add_github_button.connect("clicked", self.add_github)
        toolbar.pack_start(self.add_github_button, False, False, 0)

        self.add_direct_button = Gtk.Button(label="Add Direct File URL")
        self.add_direct_button.connect("clicked", self.add_direct)
        toolbar.pack_start(self.add_direct_button, False, False, 0)

        self.remove_button = Gtk.Button(label="Remove Selected")
        self.remove_button.connect("clicked", self.remove_selected)
        toolbar.pack_start(self.remove_button, False, False, 0)

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
        scroller.add(self.tree)

        enabled_renderer = Gtk.CellRendererToggle()
        enabled_renderer.connect("toggled", self.on_enabled_toggled)
        enabled_col = Gtk.TreeViewColumn("Enabled", enabled_renderer, active=0)
        self.tree.append_column(enabled_col)

        type_renderer = Gtk.CellRendererText()
        type_col = Gtk.TreeViewColumn("Type", type_renderer, text=1)
        self.tree.append_column(type_col)

        url_renderer = Gtk.CellRendererText()
        url_renderer.set_property("ellipsize", 3)
        url_col = Gtk.TreeViewColumn("URL", url_renderer, text=2)
        url_col.set_expand(True)
        self.tree.append_column(url_col)

        status_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.pack_start(status_box, False, False, 0)

        self.progress_bar = Gtk.ProgressBar()
        status_box.pack_start(self.progress_bar, False, False, 0)

        self.status_label = Gtk.Label(label="Ready")
        self.status_label.set_xalign(0)
        status_box.pack_start(self.status_label, False, False, 0)

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
            self.store.append([bool(item.get("enabled", True)), item.get("type", "direct"), item.get("url", "")])

    def load_schedule(self):
        schedule = self.config.get("monthly_schedule", {})
        self.schedule_day.set_value(clamp(schedule.get("day"), 1, 28, 15))
        self.schedule_hour.set_value(clamp(schedule.get("hour"), 0, 23, 2))
        self.schedule_minute.set_value(clamp(schedule.get("minute"), 0, 59, 0))

    def config_from_ui(self):
        items = []
        for row in self.store:
            items.append({"enabled": bool(row[0]), "type": row[1], "url": row[2]})
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
        self.store[path][0] = not self.store[path][0]
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
            self.store.append([True, item_type, url])
            self.save_from_ui()

    def remove_selected(self, _button):
        selection = self.tree.get_selection()
        model, iterator = selection.get_selected()
        if iterator:
            model.remove(iterator)
            self.save_from_ui()

    def start_downloads(self, _button):
        self.start_queue(dry_run=False)

    def start_dry_run(self, _button):
        self.start_queue(dry_run=True)

    def start_queue(self, dry_run, mark_scheduled=False):
        if self.worker and self.worker.is_alive():
            self.set_status("Downloads are already running")
            return

        self.save_from_ui()
        items = [item for item in self.config["items"] if item.get("enabled")]
        if not items:
            self.set_status("No enabled downloads")
            return

        self.cancel_requested.clear()
        self.set_controls_sensitive(False)
        self.cancel_button.set_sensitive(True)
        self.progress_bar.set_fraction(0)
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
        self.set_status("Cancel requested — current item will finish first")

    def download_worker(self, items, dry_run, mark_scheduled):
        downloader = Downloader(
            self.config["save_root"],
            lambda text: GLib.idle_add(self.set_status, text),
            lambda fraction, pulse=False: GLib.idle_add(self.set_progress, fraction, pulse),
            dry_run=dry_run,
        )

        total = len(items)
        for index, item in enumerate(items, start=1):
            if self.cancel_requested.is_set():
                GLib.idle_add(self.set_status, "Complete: canceled after current item")
                break
            try:
                GLib.idle_add(
                    self.set_status,
                    f"Item {index} of {total}: {item['type']} {item['url']}",
                )
                downloader.run_item(item)
            except Exception as exc:
                GLib.idle_add(self.set_status, f"Failed — {exc}")

        GLib.idle_add(self.finish_downloads, mark_scheduled)

    def set_status(self, text):
        self.status_label.set_text(text)
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

    def set_progress(self, fraction, pulse=False):
        if pulse:
            self.progress_bar.pulse()
        elif fraction is not None:
            self.progress_bar.set_fraction(float(fraction))
        return False

    def finish_downloads(self, mark_scheduled=False):
        if mark_scheduled:
            self.config["last_scheduled_run_date"] = datetime.date.today().isoformat()
            save_config(self.config)
        self.progress_bar.set_fraction(1)
        self.set_controls_sensitive(True)
        self.cancel_button.set_sensitive(False)
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
            self.add_github_button,
            self.add_direct_button,
            self.remove_button,
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
