#!/usr/bin/env python3
"""Terminal-based macOS backup and restore manager for AWS S3."""

from __future__ import annotations

import curses
import datetime as dt
import json
import os
import pathlib
import shlex
import subprocess
import textwrap
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Callable, List, Optional

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except Exception:  # boto3 is optional at import time
    boto3 = None
    BotoCoreError = Exception
    ClientError = Exception

APP_DIR = pathlib.Path.home() / ".s3_backup_manager"
ROUTINES_FILE = APP_DIR / "routines.json"
STATE_FILE = APP_DIR / "state.json"
BUCKET_HELP_URL = "https://s3.console.aws.amazon.com/s3/buckets"


@dataclass
class BackupRoutine:
    id: str
    name: str
    source_path: str
    bucket: str
    prefix: str
    frequency: str
    interval_hours: int
    retention_count: int
    retention_days: int
    daemonized: bool = False


@dataclass
class AppState:
    quick_path: str = ""


class S3BackupApp:
    def __init__(self, stdscr) -> None:
        self.stdscr = stdscr
        self.routines: List[BackupRoutine] = self._load_routines()
        self.state = self._load_state()
        self.message = "Ready"

    def run(self) -> None:
        curses.curs_set(0)
        self.stdscr.keypad(True)
        while True:
            selected = self.menu(
                "S3 Backup Manager",
                [
                    "Create backup routine",
                    "Use preset routines (Monthly/Weekly/Daily)",
                    "Edit saved routines",
                    "Run backup now",
                    "Run restore",
                    "Quick path (paste anytime)",
                    "Exit",
                ],
            )
            if selected is None or selected == 6:
                break
            if selected == 0:
                self.create_routine()
            elif selected == 1:
                self.create_presets()
            elif selected == 2:
                self.edit_routine_menu()
            elif selected == 3:
                self.run_backup_menu()
            elif selected == 4:
                self.run_restore()
            elif selected == 5:
                self.set_quick_path()

    def menu(self, title: str, options: List[str]) -> Optional[int]:
        idx = 0
        while True:
            self.draw(title, options, idx)
            key = self.stdscr.getch()
            if key in (curses.KEY_UP, ord("k")):
                idx = (idx - 1) % len(options)
            elif key in (curses.KEY_DOWN, ord("j")):
                idx = (idx + 1) % len(options)
            elif key in (10, 13, curses.KEY_ENTER):
                return idx
            elif key == 27:  # ESC
                return None
            elif key in (ord("p"), ord("P")):
                self.set_quick_path()

    def draw(self, title: str, options: List[str], idx: int) -> None:
        self.stdscr.erase()
        height, width = self.stdscr.getmaxyx()
        self.stdscr.addstr(1, 2, title, curses.A_BOLD)
        help_line = "↑/↓ navigate  Enter select  ESC back  P paste quick path"
        self.stdscr.addstr(3, 2, help_line[: width - 4])
        if self.state.quick_path:
            qp = f"Quick path: {self.state.quick_path}"
            self.stdscr.addstr(4, 2, qp[: width - 4], curses.A_DIM)
        for i, option in enumerate(options):
            marker = "➜ " if i == idx else "  "
            style = curses.A_REVERSE if i == idx else curses.A_NORMAL
            self.stdscr.addstr(6 + i, 4, (marker + option)[: width - 8], style)
        self.stdscr.addstr(height - 2, 2, f"{self.message}"[: width - 4])
        self.stdscr.refresh()

    def prompt(self, label: str, default: str = "") -> Optional[str]:
        curses.echo()
        curses.curs_set(1)
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
        self.stdscr.addstr(2, 2, f"{label} (ESC to cancel)")
        if self.state.quick_path:
            self.stdscr.addstr(4, 2, f"Quick path: {self.state.quick_path}"[: w - 4], curses.A_DIM)
        self.stdscr.addstr(6, 2, f"Default: {default}"[: w - 4], curses.A_DIM)
        self.stdscr.addstr(8, 2, "> ")
        self.stdscr.refresh()
        buffer = default
        while True:
            ch = self.stdscr.getch()
            if ch == 27:
                curses.noecho()
                curses.curs_set(0)
                return None
            if ch in (10, 13, curses.KEY_ENTER):
                break
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                buffer = buffer[:-1]
            elif 32 <= ch <= 126:
                buffer += chr(ch)
            self.stdscr.addstr(8, 4, " " * (w - 6))
            self.stdscr.addstr(8, 4, buffer[: w - 6])
            self.stdscr.move(8, 4 + min(len(buffer), w - 7))
            self.stdscr.refresh()
        curses.noecho()
        curses.curs_set(0)
        return buffer.strip()

    def message_screen(self, title: str, content: str) -> None:
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
        self.stdscr.addstr(1, 2, title, curses.A_BOLD)
        y = 3
        for line in textwrap.wrap(content, width=max(20, w - 4)):
            if y >= h - 3:
                break
            self.stdscr.addstr(y, 2, line)
            y += 1
        self.stdscr.addstr(h - 2, 2, "Press any key to continue")
        self.stdscr.refresh()
        self.stdscr.getch()

    def set_quick_path(self) -> None:
        value = self.prompt("Paste a quick path to use in backup/restore", self.state.quick_path)
        if value is not None:
            self.state.quick_path = value
            self._save_state()
            self.message = "Quick path updated"

    def create_routine(self) -> None:
        routine = self.collect_routine_data()
        if routine:
            self.routines.append(routine)
            self._save_routines()
            self.message = f"Routine '{routine.name}' created"

    def create_presets(self) -> None:
        source = self.prompt("Source path for all preset routines", self.state.quick_path)
        if not source:
            return
        bucket = self.prompt("S3 bucket")
        if not bucket:
            return
        prefix = self.prompt("S3 prefix", "backups") or "backups"
        daemonized = self.prompt_yes_no("Create daemon scripts for presets?", True)
        presets = [
            ("Monthly", "monthly", 24 * 30, 12, 365),
            ("Weekly", "weekly", 24 * 7, 4, 60),
            ("Daily every 2 hours", "hourly", 2, 12, 2),
        ]
        for name, frequency, hours, count, days in presets:
            routine = BackupRoutine(
                id=str(uuid.uuid4()),
                name=name,
                source_path=source,
                bucket=bucket,
                prefix=f"{prefix}/{frequency}",
                frequency=frequency,
                interval_hours=hours,
                retention_count=count,
                retention_days=days,
                daemonized=daemonized,
            )
            self.routines.append(routine)
            if daemonized:
                self.create_daemon_files(routine)
        self._save_routines()
        self.message = "Preset routines created"

    def collect_routine_data(self, existing: Optional[BackupRoutine] = None) -> Optional[BackupRoutine]:
        current = existing or BackupRoutine(
            id=str(uuid.uuid4()),
            name="",
            source_path="",
            bucket="",
            prefix="backups",
            frequency="daily",
            interval_hours=24,
            retention_count=30,
            retention_days=30,
            daemonized=False,
        )
        name = self.prompt("Routine name", current.name)
        if not name:
            return None
        source = self.prompt("Source path to backup", current.source_path or self.state.quick_path)
        if not source:
            return None
        bucket = self.prompt("S3 bucket", current.bucket)
        if not bucket:
            return None
        if not self.bucket_exists(bucket):
            self.message_screen(
                "Bucket not found",
                f"Bucket '{bucket}' was not found or inaccessible. Check AWS credentials and bucket name. Manage buckets at: {BUCKET_HELP_URL}",
            )
            return None
        prefix = self.prompt("S3 prefix", current.prefix) or "backups"
        frequency = self.prompt_choice("Frequency", ["hourly", "daily", "weekly", "monthly"], current.frequency)
        if not frequency:
            return None
        interval_hours = self.prompt_int("Run every N hours", current.interval_hours)
        if interval_hours is None:
            return None
        retention_count = self.prompt_int("How many backups to keep", current.retention_count)
        if retention_count is None:
            return None
        retention_days = self.prompt_int("How many days to keep backups", current.retention_days)
        if retention_days is None:
            return None
        daemonized = self.prompt_yes_no("Convert into daemon script (launchd)?", current.daemonized)
        routine = BackupRoutine(
            id=current.id,
            name=name,
            source_path=source,
            bucket=bucket,
            prefix=prefix,
            frequency=frequency,
            interval_hours=interval_hours,
            retention_count=retention_count,
            retention_days=retention_days,
            daemonized=daemonized,
        )
        if daemonized:
            self.create_daemon_files(routine)
        return routine

    def edit_routine_menu(self) -> None:
        if not self.routines:
            self.message = "No routines to edit"
            return
        options = [f"{r.name} ({r.frequency}, keep {r.retention_count})" for r in self.routines] + ["Back"]
        idx = self.menu("Select routine to edit", options)
        if idx is None or idx == len(options) - 1:
            return
        routine = self.routines[idx]
        action = self.menu("Edit routine", ["Update", "Delete", "Back"])
        if action == 0:
            updated = self.collect_routine_data(routine)
            if updated:
                self.routines[idx] = updated
                self._save_routines()
                self.message = "Routine updated"
        elif action == 1:
            del self.routines[idx]
            self._save_routines()
            self.message = "Routine deleted"

    def run_backup_menu(self) -> None:
        if not self.routines:
            self.message = "No routines available"
            return
        options = [f"{r.name} -> s3://{r.bucket}/{r.prefix}" for r in self.routines] + ["Back"]
        idx = self.menu("Run backup now", options)
        if idx is None or idx == len(options) - 1:
            return
        routine = self.routines[idx]
        ok, msg = self.run_backup(routine)
        if ok:
            self.cleanup_retention(routine)
        self.message_screen("Backup result", msg)

    def run_restore(self) -> None:
        if not self.routines:
            self.message = "No routines available"
            return
        options = [f"{r.name} from s3://{r.bucket}/{r.prefix}" for r in self.routines] + ["Back"]
        idx = self.menu("Choose routine for restore", options)
        if idx is None or idx == len(options) - 1:
            return
        routine = self.routines[idx]
        target = self.prompt("Restore target path", self.state.quick_path or routine.source_path)
        if not target:
            return
        ok, msg = self.restore_latest(routine, target)
        self.message_screen("Restore result", msg)
        if ok:
            self.message = "Restore successful"

    def bucket_exists(self, bucket: str) -> bool:
        if boto3 is None:
            self.message = "boto3 not installed; skipping bucket validation"
            return True
        s3 = boto3.client("s3")
        try:
            s3.head_bucket(Bucket=bucket)
            return True
        except (BotoCoreError, ClientError):
            return False

    def run_backup(self, routine: BackupRoutine) -> tuple[bool, str]:
        if boto3 is None:
            return False, "boto3 not installed. Run: pip install boto3"
        s3 = boto3.client("s3")
        src = pathlib.Path(routine.source_path).expanduser()
        if not src.exists():
            return False, f"Source path not found: {src}"
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        base_prefix = f"{routine.prefix.rstrip('/')}/{routine.name.replace(' ', '_')}/{stamp}"
        try:
            for path in src.rglob("*"):
                if path.is_file():
                    relative = path.relative_to(src).as_posix()
                    key = f"{base_prefix}/{relative}"
                    s3.upload_file(str(path), routine.bucket, key)
            return True, f"Backup completed to s3://{routine.bucket}/{base_prefix}"
        except (BotoCoreError, ClientError) as exc:
            return False, f"Backup failed: {exc}"

    def restore_latest(self, routine: BackupRoutine, target: str) -> tuple[bool, str]:
        if boto3 is None:
            return False, "boto3 not installed. Run: pip install boto3"
        s3 = boto3.client("s3")
        prefix = f"{routine.prefix.rstrip('/')}/{routine.name.replace(' ', '_')}/"
        try:
            response = s3.list_objects_v2(Bucket=routine.bucket, Prefix=prefix)
            objects = response.get("Contents", [])
            if not objects:
                return False, "No backup objects found"
            latest_stamp = max(obj["Key"].split("/")[2] for obj in objects if len(obj["Key"].split("/")) > 2)
            for obj in objects:
                key = obj["Key"]
                parts = key.split("/")
                if len(parts) < 4 or parts[2] != latest_stamp:
                    continue
                relative = "/".join(parts[3:])
                destination = pathlib.Path(target).expanduser() / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                s3.download_file(routine.bucket, key, str(destination))
            return True, f"Restored backup {latest_stamp} to {target}"
        except (BotoCoreError, ClientError) as exc:
            return False, f"Restore failed: {exc}"

    def cleanup_retention(self, routine: BackupRoutine) -> None:
        if boto3 is None:
            return
        s3 = boto3.client("s3")
        prefix = f"{routine.prefix.rstrip('/')}/{routine.name.replace(' ', '_')}/"
        try:
            response = s3.list_objects_v2(Bucket=routine.bucket, Prefix=prefix)
            objects = response.get("Contents", [])
            grouped = {}
            now = dt.datetime.now(dt.timezone.utc)
            for obj in objects:
                parts = obj["Key"].split("/")
                if len(parts) < 4:
                    continue
                stamp = parts[2]
                grouped.setdefault(stamp, []).append(obj)
            sorted_stamps = sorted(grouped.keys(), reverse=True)
            keep = set(sorted_stamps[: routine.retention_count])
            for stamp in sorted_stamps:
                entries = grouped[stamp]
                modified = max(entry["LastModified"] for entry in entries)
                age_days = (now - modified).days
                if stamp in keep and age_days <= routine.retention_days:
                    continue
                for entry in entries:
                    s3.delete_object(Bucket=routine.bucket, Key=entry["Key"])
        except (BotoCoreError, ClientError):
            pass

    def create_daemon_files(self, routine: BackupRoutine) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        script_path = APP_DIR / f"run_{routine.id}.sh"
        plist_path = APP_DIR / f"com.s3backup.{routine.id}.plist"
        python_bin = shlex.quote(os.environ.get("PYTHON", "python3"))
        app_path = shlex.quote(str((pathlib.Path(__file__).resolve())))
        script = f"#!/bin/bash\n{python_bin} {app_path} --run-routine {routine.id}\n"
        script_path.write_text(script)
        os.chmod(script_path, 0o755)
        seconds = max(3600, routine.interval_hours * 3600)
        plist = f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\"><dict>
<key>Label</key><string>com.s3backup.{routine.id}</string>
<key>ProgramArguments</key><array><string>{script_path}</string></array>
<key>StartInterval</key><integer>{seconds}</integer>
<key>RunAtLoad</key><true/>
<key>StandardOutPath</key><string>{APP_DIR / 'daemon.log'}</string>
<key>StandardErrorPath</key><string>{APP_DIR / 'daemon.err'}</string>
</dict></plist>
"""
        plist_path.write_text(plist)
        self.message = f"Daemon files created at {plist_path}. Load with: launchctl load {plist_path}"

    def prompt_choice(self, title: str, options: List[str], default: str) -> Optional[str]:
        normalized = [o.lower() for o in options]
        if default.lower() in normalized:
            idx = normalized.index(default.lower())
        else:
            idx = 0
        while True:
            selection = self.menu(f"{title} (ESC cancel)", options)
            return None if selection is None else options[selection]

    def prompt_yes_no(self, question: str, default: bool) -> bool:
        default_text = "y" if default else "n"
        value = self.prompt(f"{question} [y/n]", default_text)
        if value is None:
            return default
        return value.lower() in {"y", "yes", "1", "true"}

    def prompt_int(self, label: str, default: int) -> Optional[int]:
        value = self.prompt(label, str(default))
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            self.message = f"Invalid number: {value}"
            return self.prompt_int(label, default)

    def _load_routines(self) -> List[BackupRoutine]:
        if not ROUTINES_FILE.exists():
            return []
        data = json.loads(ROUTINES_FILE.read_text())
        return [BackupRoutine(**item) for item in data]

    def _save_routines(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        ROUTINES_FILE.write_text(json.dumps([asdict(r) for r in self.routines], indent=2))

    def _load_state(self) -> AppState:
        if not STATE_FILE.exists():
            return AppState()
        data = json.loads(STATE_FILE.read_text())
        return AppState(**data)

    def _save_state(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(asdict(self.state), indent=2))


def run_single_routine(routine_id: str) -> int:
    app = S3BackupApp(curses.initscr())
    try:
        routine = next((r for r in app.routines if r.id == routine_id), None)
        if not routine:
            print(f"Routine not found: {routine_id}")
            return 1
        ok, msg = app.run_backup(routine)
        if ok:
            app.cleanup_retention(routine)
        print(msg)
        return 0 if ok else 1
    finally:
        curses.endwin()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="S3 backup manager")
    parser.add_argument("--run-routine", help="Run a routine by id without UI")
    args = parser.parse_args()

    if args.run_routine:
        return run_single_routine(args.run_routine)

    curses.wrapper(lambda stdscr: S3BackupApp(stdscr).run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
