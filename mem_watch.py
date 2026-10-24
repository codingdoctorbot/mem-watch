#!/usr/bin/env python3
"""mem-watch: system tray monitor for RAM, swap, GPU-driver-held memory and crashes.

Built for the Intel Arc (xe) failed-suspend leak, where the driver keeps
~VRAM-sized chunks of system RAM after a suspend fails. Shows a tray icon,
a small status window, and sends a notification once per new problem.
"""

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time

from PyQt6.QtCore import QRectF, Qt, QTimer
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QMenu, QMessageBox, QProgressBar,
    QPushButton, QSystemTrayIcon, QVBoxLayout, QWidget,
)

APP_NAME = "mem-watch"
FAST_INTERVAL_MS = 10_000   # /proc reads: cheap
SLOW_INTERVAL_MS = 60_000   # journal / coredump / process scans

# Thresholds
GPU_WARN_GB = 2.0           # GPUActive is ~50 MB normally
GPU_RENOTIFY_STEP_GB = 2.0  # notify again if it grows this much more
AVAIL_WARN_PCT = 15
AVAIL_CRIT_PCT = 7
SWAP_WARN_PCT = 70
SWAP_CRIT_PCT = 90
STALE_REPORTER_SECS = 3600

OK, WARN, CRIT = 0, 1, 2
LEVEL_COLORS = {OK: "#3fb950", WARN: "#d29922", CRIT: "#f85149"}
LEVEL_NAMES = {OK: "OK", WARN: "Warning", CRIT: "Critical"}

GIB = 1024 ** 3


# ---------------------------------------------------------------- data

def read_meminfo():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, rest = line.split(":", 1)
            info[key] = int(rest.split()[0]) * 1024  # kB -> bytes
    return info


def read_zram():
    """Return (original data bytes, compressed RAM bytes) across zram devices."""
    data = used = 0
    base = "/sys/block"
    for dev in os.listdir(base):
        if not dev.startswith("zram"):
            continue
        try:
            with open(f"{base}/{dev}/mm_stat") as f:
                fields = f.read().split()
            data += int(fields[0])
            used += int(fields[2])
        except (OSError, IndexError, ValueError):
            pass
    return data, used


def boot_time():
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("btime"):
                return int(line.split()[1])
    return 0


def run(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def xe_suspend_failures():
    out = run(["journalctl", "-k", "-b", "-o", "short-iso", "--no-pager",
               "--grep", r"xe .*failed to suspend"])
    return [l for l in out.splitlines() if l and not l.startswith("--")]


def coredumps_since(since_epoch):
    out = run(["coredumpctl", "list", "--no-pager", "--json=short",
               "--since", f"@{int(since_epoch)}"])
    try:
        entries = json.loads(out) if out.strip() else []
    except json.JSONDecodeError:
        return []
    return [
        {"time": e["time"] / 1e6, "pid": e["pid"],
         "exe": os.path.basename(e.get("exe") or "?"), "sig": e.get("sig")}
        for e in entries
    ]


def stale_crash_reporters(btime):
    """drkonqi processes (crash dialogs/launchers) that have been sitting around."""
    clk = os.sysconf("SC_CLK_TCK")
    now = time.time()
    stale = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm") as f:
                comm = f.read().strip()
            if not comm.startswith("drkonqi"):
                continue
            with open(f"/proc/{pid}/stat") as f:
                stat = f.read()
            start_ticks = int(stat.rsplit(")", 1)[1].split()[19])
            age = now - (btime + start_ticks / clk)
            if age > STALE_REPORTER_SECS and os.stat(f"/proc/{pid}").st_uid == os.getuid():
                stale.append((int(pid), comm, age))
        except (OSError, ValueError, IndexError):
            continue
    return stale


def fmt_gb(b):
    return f"{b / GIB:.1f} GB"


def fmt_age(secs):
    h, m = divmod(int(secs) // 60, 60)
    return f"{h}h {m}m" if h else f"{m}m"


# ---------------------------------------------------------------- state

class Monitor:
    def __init__(self, fake_gpu_gb=None, fake_suspend_fail=False):
        self.fake_gpu_gb = fake_gpu_gb
        self.fake_suspend_fail = fake_suspend_fail
        self.btime = boot_time()
        self.started = time.time()
        self.mem = {}
        self.zram = (0, 0)
        self.suspend_failures = []
        self.crashes = []
        self.stale = []

    def refresh_fast(self):
        self.mem = read_meminfo()
        if self.fake_gpu_gb is not None:
            self.mem["GPUActive"] = int(self.fake_gpu_gb * GIB)
        self.zram = read_zram()

    def refresh_slow(self):
        self.suspend_failures = xe_suspend_failures()
        if self.fake_suspend_fail:
            self.suspend_failures.append("(simulated) xe: PM: failed to suspend async: error -22")
        self.crashes = coredumps_since(self.btime)
        self.stale = stale_crash_reporters(self.btime)

    # derived values
    @property
    def total(self): return self.mem.get("MemTotal", 1)
    @property
    def avail(self): return self.mem.get("MemAvailable", 0)
    @property
    def used(self): return self.total - self.avail
    @property
    def swap_total(self): return self.mem.get("SwapTotal", 0)
    @property
    def swap_used(self): return self.swap_total - self.mem.get("SwapFree", 0)
    @property
    def gpu(self): return self.mem.get("GPUActive", 0)

    def pct(self, part, whole):
        return 100 * part / whole if whole else 0

    def issues(self):
        """List of (key, level, title, detail). Keys identify an issue for de-duplicating notifications."""
        out = []
        gpu_gb = self.gpu / GIB
        if gpu_gb >= GPU_WARN_GB:
            why = " after a failed suspend" if self.suspend_failures else ""
            out.append(("gpu", CRIT, f"GPU driver holding {gpu_gb:.1f} GB of RAM{why}",
                        "This memory is not owned by any app and won't come back on its own. "
                        "Reboot recommended (logging out may also free it)."))
        elif self.suspend_failures:
            out.append(("suspend", WARN, "Arc GPU failed to suspend this boot",
                        "No memory held yet, but sleep is unreliable. Consider disabling auto-sleep."))

        avail_pct = self.pct(self.avail, self.total)
        if avail_pct < AVAIL_CRIT_PCT:
            out.append(("ram", CRIT, f"RAM nearly full: {fmt_gb(self.avail)} available",
                        "Close some programs or reboot."))
        elif avail_pct < AVAIL_WARN_PCT:
            out.append(("ram", WARN, f"RAM low: {fmt_gb(self.avail)} available", ""))

        swap_pct = self.pct(self.swap_used, self.swap_total)
        if swap_pct > SWAP_CRIT_PCT:
            out.append(("swap", CRIT, f"Swap {swap_pct:.0f}% full", "System will slow down or start killing programs."))
        elif swap_pct > SWAP_WARN_PCT:
            out.append(("swap", WARN, f"Swap {swap_pct:.0f}% full", ""))

        if self.stale:
            out.append(("stale", WARN, f"{len(self.stale)} stale crash reporter(s)",
                        "Leftover drkonqi processes from earlier crashes. Safe to clean up."))
        return out

    def level(self):
        return max((lvl for _, lvl, _, _ in self.issues()), default=OK)

    def reboot_recommended(self):
        keys = {k: lvl for k, lvl, _, _ in self.issues()}
        return "gpu" in keys or (keys.get("ram") == CRIT and keys.get("swap") == CRIT)


# ---------------------------------------------------------------- UI

def make_icon(fill_frac, level):
    """Rounded 'memory chip' whose fill shows RAM used, colored by status."""
    size = 64
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    color = QColor(LEVEL_COLORS[level])
    outer = QRectF(6, 4, 52, 56)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(40, 40, 40, 220))
    p.drawRoundedRect(outer, 10, 10)
    inner_h = 48 * max(0.0, min(1.0, fill_frac))
    p.setBrush(color)
    p.drawRoundedRect(QRectF(11, 8 + 48 - inner_h, 42, inner_h), 6, 6)
    if level != OK:
        p.setPen(QColor("white"))
        f = QFont()
        f.setBold(True)
        f.setPixelSize(40)
        p.setFont(f)
        p.drawText(outer, Qt.AlignmentFlag.AlignCenter, "!")
    p.end()
    return QIcon(pm)


class StatusWindow(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setWindowTitle("Memory Watch")
        self.setWindowFlag(Qt.WindowType.Tool)
        self.setMinimumWidth(380)

        lay = QVBoxLayout(self)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.status)

        self.ram_bar, self.ram_lbl = self._bar_row(lay, "RAM")
        self.swap_bar, self.swap_lbl = self._bar_row(lay, "Swap")
        self.gpu_lbl = QLabel()
        self.zram_lbl = QLabel()
        lay.addWidget(self.gpu_lbl)
        lay.addWidget(self.zram_lbl)

        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.details)

        btns = QHBoxLayout()
        self.cleanup_btn = QPushButton("Clean up crash reporters")
        self.cleanup_btn.clicked.connect(app.cleanup_reporters)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(lambda: app.tick(slow=True))
        self.logout_btn = QPushButton("Log out…")
        self.logout_btn.clicked.connect(lambda: app.kde_prompt("promptLogout"))
        self.reboot_btn = QPushButton("Reboot…")
        self.reboot_btn.clicked.connect(lambda: app.kde_prompt("promptReboot"))
        for b in (self.cleanup_btn, refresh_btn, self.logout_btn, self.reboot_btn):
            btns.addWidget(b)
        lay.addLayout(btns)

    def _bar_row(self, lay, name):
        row = QHBoxLayout()
        row.addWidget(QLabel(f"<b>{name}</b>"), 0)
        bar = QProgressBar()
        bar.setRange(0, 1000)
        bar.setTextVisible(False)
        bar.setFixedHeight(12)
        row.addWidget(bar, 1)
        lbl = QLabel()
        lbl.setMinimumWidth(150)
        lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(lbl, 0)
        lay.addLayout(row)
        return bar, lbl

    @staticmethod
    def _set_bar(bar, pct, level):
        bar.setValue(int(pct * 10))
        bar.setStyleSheet(f"QProgressBar::chunk {{ background: {LEVEL_COLORS[level]}; }}")

    def update_view(self, m: Monitor):
        issues = m.issues()
        level = m.level()
        color = LEVEL_COLORS[level]
        if issues:
            items = "".join(
                f"<li><b style='color:{LEVEL_COLORS[lvl]}'>{t}</b>"
                + (f"<br><small>{d}</small>" if d else "") + "</li>"
                for _, lvl, t, d in issues)
            head = ("<b>Reboot recommended.</b>" if m.reboot_recommended()
                    else f"<b style='color:{color}'>{LEVEL_NAMES[level]}</b>")
            self.status.setText(f"{head}<ul style='margin:4px 0'>{items}</ul>")
        else:
            self.status.setText(f"<b style='color:{color}'>All good.</b>")

        ram_pct = m.pct(m.used, m.total)
        lvl_of = {k: l for k, l, _, _ in issues}
        self._set_bar(self.ram_bar, ram_pct, lvl_of.get("ram", OK))
        self.ram_lbl.setText(f"{fmt_gb(m.used)} / {fmt_gb(m.total)} ({fmt_gb(m.avail)} free)")
        swap_pct = m.pct(m.swap_used, m.swap_total)
        self._set_bar(self.swap_bar, swap_pct, lvl_of.get("swap", OK))
        self.swap_lbl.setText(f"{fmt_gb(m.swap_used)} / {fmt_gb(m.swap_total)}")

        gpu_color = LEVEL_COLORS[lvl_of.get("gpu", OK)]
        self.gpu_lbl.setText(
            f"<b>GPU driver RAM:</b> <span style='color:{gpu_color}'>{m.gpu / 1024**2:,.0f} MB</span>"
            "  <small>(normal: under ~200 MB)</small>")
        zdata, zused = m.zram
        self.zram_lbl.setText(f"<b>zram:</b> {fmt_gb(zdata)} swapped, using {fmt_gb(zused)} RAM")

        parts = []
        if m.suspend_failures:
            parts.append(f"<b>Failed GPU suspends this boot:</b> {len(m.suspend_failures)}")
        if m.crashes:
            rows = "".join(
                f"<li>{time.strftime('%a %H:%M', time.localtime(c['time']))} — {c['exe']} (signal {c['sig']})</li>"
                for c in m.crashes[-6:])
            parts.append(f"<b>Crashes this boot:</b> {len(m.crashes)}<ul style='margin:2px 0'>{rows}</ul>")
        if m.stale:
            parts.append("<b>Stale crash reporters:</b> " + ", ".join(
                f"{comm} ({fmt_age(age)})" for _, comm, age in m.stale))
        uptime = time.time() - m.btime
        parts.append(f"<small>Up {fmt_age(uptime)} · checks every {FAST_INTERVAL_MS // 1000}s</small>")
        self.details.setText("<br>".join(parts))
        self.cleanup_btn.setEnabled(bool(m.stale))
        self.adjustSize()


class TrayApp:
    def __init__(self, qapp, monitor: Monitor):
        self.qapp = qapp
        self.m = monitor
        self.notified = {}       # issue key -> level last notified
        self.gpu_notified_gb = 0.0
        self.seen_crashes = None  # pids already reported

        self.tray = QSystemTrayIcon()
        self.window = StatusWindow(self)

        menu = QMenu()
        show = QAction("Show status", menu)
        show.triggered.connect(self.show_window)
        refresh = QAction("Refresh now", menu)
        refresh.triggered.connect(lambda: self.tick(slow=True))
        quit_ = QAction("Quit", menu)
        quit_.triggered.connect(qapp.quit)
        for a in (show, refresh):
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction(quit_)
        self.menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.on_activated)

        self.tick(slow=True)
        self.tray.show()

        self.fast = QTimer()
        self.fast.timeout.connect(lambda: self.tick(slow=False))
        self.fast.start(FAST_INTERVAL_MS)
        self.slow = QTimer()
        self.slow.timeout.connect(lambda: self.tick(slow=True))
        self.slow.start(SLOW_INTERVAL_MS)

    def on_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.window.isVisible():
                self.window.hide()
            else:
                self.show_window()

    def show_window(self):
        self.window.update_view(self.m)
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def tick(self, slow):
        self.m.refresh_fast()
        if slow:
            self.m.refresh_slow()
            self.check_crashes()
        self.check_issues()
        level = self.m.level()
        self.tray.setIcon(make_icon(self.m.pct(self.m.used, self.m.total) / 100, level))
        tip = (f"RAM {fmt_gb(self.m.used)} / {fmt_gb(self.m.total)}\n"
               f"Swap {fmt_gb(self.m.swap_used)} / {fmt_gb(self.m.swap_total)}\n"
               f"GPU driver {self.m.gpu / 1024**2:,.0f} MB")
        if self.m.reboot_recommended():
            tip += "\nReboot recommended"
        self.tray.setToolTip(tip)
        if self.window.isVisible():
            self.window.update_view(self.m)

    def check_issues(self):
        current = {}
        for key, level, title, detail in self.m.issues():
            current[key] = level
            prev = self.notified.get(key)
            renotify = prev is None or level > prev
            if key == "gpu":
                gb = self.m.gpu / GIB
                if gb - self.gpu_notified_gb >= GPU_RENOTIFY_STEP_GB:
                    renotify = True
                if renotify:
                    self.gpu_notified_gb = gb
            if renotify:
                body = detail
                if self.m.reboot_recommended() and "Reboot" not in detail:
                    body = (detail + " " if detail else "") + "Reboot recommended."
                self.notify(title, body, level)
        # forget resolved issues so they notify again if they come back
        self.notified = current
        if "gpu" not in current:
            self.gpu_notified_gb = 0.0

    def check_crashes(self):
        pids = {c["pid"] for c in self.m.crashes}
        if self.seen_crashes is None:  # don't spam about crashes from before we started
            self.seen_crashes = pids
            return
        new = [c for c in self.m.crashes if c["pid"] not in self.seen_crashes]
        self.seen_crashes |= pids
        if new:
            names = ", ".join(sorted({c["exe"] for c in new}))
            self.notify(f"{len(new)} program(s) crashed", names, WARN)

    def notify(self, title, body, level):
        urgency = {OK: "low", WARN: "normal", CRIT: "critical"}[level]
        icon = {OK: "dialog-information", WARN: "dialog-warning", CRIT: "dialog-error"}[level]
        try:
            subprocess.Popen(["notify-send", "-a", "Memory Watch", "-u", urgency, "-i", icon,
                              title, body or ""])
        except OSError:
            mi = {OK: QSystemTrayIcon.MessageIcon.Information,
                  WARN: QSystemTrayIcon.MessageIcon.Warning,
                  CRIT: QSystemTrayIcon.MessageIcon.Critical}[level]
            self.tray.showMessage(title, body, mi)

    def cleanup_reporters(self):
        stale = self.m.stale
        if not stale:
            return
        ans = QMessageBox.question(
            self.window, "Clean up crash reporters",
            f"Close {len(stale)} leftover crash reporter process(es)?\n"
            "The crash records stay available via coredumpctl.")
        if ans != QMessageBox.StandardButton.Yes:
            return
        for pid, _, _ in stale:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
        QTimer.singleShot(1000, lambda: self.tick(slow=True))

    def kde_prompt(self, method):
        """Opens KDE's own confirmation dialog; nothing happens without the user confirming there."""
        for qdbus in ("qdbus6", "qdbus"):
            try:
                subprocess.Popen([qdbus, "org.kde.LogoutPrompt", "/LogoutPrompt", method])
                return
            except OSError:
                continue


def single_instance_lock():
    path = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), f"{APP_NAME}.lock")
    fd = open(path, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return fd


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fake-gpu-gb", type=float, help="pretend GPUActive is this many GB (testing)")
    ap.add_argument("--fake-suspend-fail", action="store_true", help="pretend an xe suspend failed (testing)")
    ap.add_argument("--show", action="store_true", help="open the status window on start")
    args = ap.parse_args()

    lock = single_instance_lock()
    if lock is None:
        print(f"{APP_NAME} is already running", file=sys.stderr)
        return 1

    qapp = QApplication(sys.argv)
    qapp.setApplicationName(APP_NAME)
    qapp.setDesktopFileName(APP_NAME)
    qapp.setQuitOnLastWindowClosed(False)

    # The tray may not be up yet right after login; wait for it.
    for _ in range(30):
        if QSystemTrayIcon.isSystemTrayAvailable():
            break
        time.sleep(1)

    app = TrayApp(qapp, Monitor(args.fake_gpu_gb, args.fake_suspend_fail))
    if args.show:
        app.show_window()
    return qapp.exec()


if __name__ == "__main__":
    sys.exit(main())
