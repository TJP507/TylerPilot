"""
External storage management for developer settings.

Enumerates external (USB) block devices and lets the user mount, unmount and
format their partitions. Only ext4 and FAT32 are offerable: those are the only
filesystems the device's kernel and mkfs tools support (no exfat).

External is detected from the sysfs bus path rather than the "removable" flag,
which is 0 for every block device on this hardware. Internal storage lives on
the UFS controller named by `androidboot.bootdevice`; anything on a USB bus is
user-attached. Formatting refuses anything that is not positively identified as
external, and every write action goes through sudo.
"""
import json
import os
import re
import subprocess
import threading
import time

import pyray as rl

from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets import Widget, DialogResult
from openpilot.system.ui.widgets.button import Button, ButtonStyle
from openpilot.system.ui.widgets.confirm_dialog import ConfirmDialog
from openpilot.system.ui.widgets.nav_widget import NavWidget
from openpilot.system.ui.widgets.scroller_tici import Scroller

MOUNT_ROOT = "/data/external"
FORMAT_LABEL = "EXTDATA"

# Safe, tightly-scoped device name patterns. Nothing is ever passed to a shell.
DISK_RE = re.compile(r"^(?:sd[a-z]+|mmcblk\d+|nvme\d+n\d+)$")
PARTITION_RE = re.compile(r"^(?:sd[a-z]+\d+|mmcblk\d+p\d+|nvme\d+n\d+p\d+)$")

TEXT_COLOR = rl.Color(255, 255, 255, 255)
SUBTEXT_COLOR = rl.Color(170, 170, 170, 255)
ROW_BG = rl.Color(41, 41, 41, 255)
WARN_COLOR = rl.Color(255, 180, 120, 255)
GOOD_COLOR = rl.Color(140, 220, 140, 255)


def _run(cmd: list, timeout: float = 20.0) -> subprocess.CompletedProcess:
  try:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
  except (OSError, subprocess.SubprocessError) as e:
    return subprocess.CompletedProcess(cmd, 1, "", str(e))


def _boot_device() -> str:
  try:
    with open("/proc/cmdline") as f:
      for tok in f.read().split():
        if tok.startswith("androidboot.bootdevice="):
          return tok.split("=", 1)[1]
  except OSError:
    pass
  return ""


def _is_external_disk(name: str) -> bool:
  """True only if the disk sits on a USB bus (or a removable SD card)."""
  try:
    real = os.path.realpath(f"/sys/class/block/{name}")
  except OSError:
    return False
  if "usb" in real.lower():
    return True
  if name.startswith("mmcblk"):
    try:
      with open(f"/sys/class/block/{name}/removable") as f:
        return f.read().strip() == "1"
    except OSError:
      return False
  return False


def _mountpoint_of(dev: str) -> str:
  r = _run(["findmnt", "-rn", "-S", dev, "-o", "TARGET"])
  if r.returncode == 0 and r.stdout.strip():
    return r.stdout.strip().splitlines()[0]
  return ""


def _parent_disk(dev_name: str):
  m = re.match(r"^(?:sd[a-z]+|mmcblk\d+|nvme\d+n\d+)", dev_name)
  return m.group(0) if m else None


def _is_external_path(dev_path: str) -> bool:
  """Hard safety check: the device must be on a USB bus and not the boot device."""
  name = os.path.basename(dev_path)
  disk = _parent_disk(name)
  if disk is None or not re.match(r"^(?:sd[a-z]+(?:\d+)?|mmcblk\d+(?:p\d+)?|nvme\d+n\d+(?:p\d+)?)$", name):
    return False
  try:
    real = os.path.realpath(f"/sys/class/block/{name}")
  except OSError:
    return False
  boot = _boot_device()
  if boot and boot in real:
    return False
  return _is_external_disk(disk)


def _entry(name: str, bd: dict, is_disk: bool) -> dict:
  size = int(bd.get("size") or 0)
  return {
    "name": name,
    "path": f"/dev/{name}",
    "size": size,
    "fstype": bd.get("fstype") or "",
    "label": bd.get("label") or "",
    "mountpoint": bd.get("mountpoint") or "",
    "is_disk": is_disk,
  }


def _disk_entries(disk: str) -> list:
  """Partitions of an external disk (or the whole disk if it is unpartitioned)."""
  r = _run(["lsblk", "-J", "-b", "-o", "NAME,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINT", f"/dev/{disk}"])
  if r.returncode != 0:
    return []
  try:
    data = json.loads(r.stdout)
  except json.JSONDecodeError:
    return []

  entries: list = []
  for bd in data.get("blockdevices", []):
    children = bd.get("children") or []
    if not children:
      entries.append(_entry(bd.get("name", disk), bd, True))
      continue
    for ch in children:
      if ch.get("type") == "part" and PARTITION_RE.match(ch.get("name", "")):
        entries.append(_entry(ch["name"], ch, False))
  return entries


def list_external() -> list:
  """All external disks and their partitions, as presented in the UI."""
  boot = _boot_device()
  try:
    names = os.listdir("/sys/class/block")
  except OSError:
    return []

  disks: list = []
  for name in sorted(names):
    if not DISK_RE.match(name):
      continue
    if os.path.exists(f"/sys/class/block/{name}/partition"):
      continue
    real = os.path.realpath(f"/sys/class/block/{name}")
    if boot and boot in real:
      continue
    if not _is_external_disk(name):
      continue
    disks.append(name)
  return sorted(set(disks))


def _human_size(n: int) -> str:
  if n <= 0:
    return "?"
  for unit in ("B", "K", "M", "G", "T"):
    if n < 1024 or unit == "T":
      return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
    n /= 1024.0
  return f"{n:.1f}T"


def _disk_size(disk: str) -> int:
  try:
    with open(f"/sys/class/block/{disk}/size") as f:
      return int(f.read().strip()) * 512
  except (OSError, ValueError):
    return 0


def _partition_name(disk: str, idx: int) -> str:
  if disk.startswith("mmcblk") or disk.startswith("nvme"):
    return f"/dev/{disk}p{idx}"
  return f"/dev/{disk}{idx}"


def format_whole_drive(disk: str, fs: str) -> tuple:
  """Wipe the partition table, create one GPT partition spanning the drive, format it."""
  path = f"/dev/{disk}"
  if not DISK_RE.match(disk) or not _is_external_path(path):
    return False, "refusing to format non-external device"

  # Release every partition on the disk (mounts and swap) before repartitioning.
  parts = [e["name"] for e in _disk_entries(disk) if not e.get("is_disk")]
  for part in parts:
    _run(["sudo", "umount", f"/dev/{part}"])
    _run(["sudo", "swapoff", f"/dev/{part}"])
  _run(["sudo", "umount", path])
  _run(["sudo", "swapoff", path])

  if _run(["sudo", "wipefs", "-a", path], timeout=60).returncode != 0:
    return False, "wipefs failed"
  if _run(["sudo", "parted", "-s", path, "mklabel", "gpt"], timeout=60).returncode != 0:
    return False, "mklabel failed"
  ptype = "ext4" if fs == "ext4" else "fat32"
  r = _run(["sudo", "parted", "-s", "-a", "optimal", path, "mkpart", "primary", ptype, "1MiB", "100%"], timeout=120)
  if r.returncode != 0:
    return False, (r.stderr or r.stdout).strip()

  _run(["sudo", "partprobe", path], timeout=30)
  new_part = _partition_name(disk, 1)
  for _ in range(24):
    if os.path.exists(new_part):
      break
    time.sleep(0.25)
  if not os.path.exists(new_part):
    return False, "new partition did not appear"

  if fs == "ext4":
    r = _run(["sudo", "mkfs.ext4", "-F", "-L", FORMAT_LABEL, new_part], timeout=300)
  else:
    r = _run(["sudo", "mkfs.vfat", "-F", "32", "-n", FORMAT_LABEL, new_part], timeout=300)
  return r.returncode == 0, (r.stderr or r.stdout).strip()


def mount_partition(entry: dict) -> tuple:
  if not _is_external_path(entry["path"]):
    return False, "refusing to mount non-external device"
  mnt = os.path.join(MOUNT_ROOT, entry["name"])
  try:
    os.makedirs(mnt, exist_ok=True)
  except OSError as e:
    return False, str(e)
  r = _run(["sudo", "mount", entry["path"], mnt])
  return r.returncode == 0, (r.stderr or r.stdout).strip()


def unmount_partition(entry: dict) -> tuple:
  if not _is_external_path(entry["path"]):
    return False, "refusing to unmount non-external device"
  target = entry["mountpoint"] or entry["path"]
  r = _run(["sudo", "umount", target])
  if r.returncode != 0:
    r = _run(["sudo", "umount", entry["path"]])
  return r.returncode == 0, (r.stderr or r.stdout).strip()


def format_partition(entry: dict, fs: str) -> tuple:
  """Unmount, then create a fresh filesystem. ext4 or vfat only."""
  if not _is_external_path(entry["path"]):
    return False, "refusing to format non-external device"
  if not (PARTITION_RE.match(entry["name"]) or entry["is_disk"]):
    return False, "refusing to format unexpected device"
  _run(["sudo", "umount", entry["mountpoint"] or entry["path"]])
  if fs == "ext4":
    cmd = ["sudo", "mkfs.ext4", "-F", "-L", FORMAT_LABEL, entry["path"]]
  elif fs == "vfat":
    cmd = ["sudo", "mkfs.vfat", "-F", "32", "-n", FORMAT_LABEL, entry["path"]]
  else:
    return False, f"unsupported filesystem {fs}"
  r = _run(cmd, timeout=180.0)
  return r.returncode == 0, (r.stderr or r.stdout).strip()


class _DriveRow(Widget):
  """Whole-drive row: wipes the partition table and creates one partition."""

  HEIGHT = 210
  BTN_W = 330
  BTN_H = 96
  GAP = 20

  def __init__(self, disk: str, size: int, panel: "ExternalStoragePanel"):
    super().__init__()
    self._disk = disk
    self._size = size
    self._rect = rl.Rectangle(0, 0, 0, self.HEIGHT)
    self._font = gui_app.font(FontWeight.MEDIUM)
    self._small = gui_app.font(FontWeight.NORMAL)

    self._btn_ext4 = self._child(Button(tr("Format drive ext4"), lambda: panel.confirm_format_drive(disk, "ext4"),
                                        font_size=40, button_style=ButtonStyle.DANGER))
    self._btn_fat = self._child(Button(tr("Format drive FAT32"), lambda: panel.confirm_format_drive(disk, "vfat"),
                                       font_size=40, button_style=ButtonStyle.DANGER))

  def set_parent_rect(self, parent_rect: rl.Rectangle) -> None:
    super().set_parent_rect(parent_rect)
    self._rect.width = parent_rect.width

  def _render(self, rect: rl.Rectangle) -> None:
    rl.draw_rectangle_rounded(rect, 0.08, 8, rl.Color(58, 40, 40, 255))
    title = f"/dev/{self._disk}   {_human_size(self._size)}   -   " + tr("entire drive")
    rl.draw_text_ex(self._font, title, rl.Vector2(rect.x + 40, rect.y + 30), 44, 0, TEXT_COLOR)
    rl.draw_text_ex(self._small, tr("Erases the partition table and all partitions on this drive."),
                    rl.Vector2(rect.x + 40, rect.y + 96), 34, 0, WARN_COLOR)

    n = 2
    total_w = n * self.BTN_W + (n - 1) * self.GAP
    x = rect.x + rect.width - total_w - 40
    y = rect.y + (rect.height - self.BTN_H) / 2
    for btn in (self._btn_ext4, self._btn_fat):
      btn.render(rl.Rectangle(x, y, self.BTN_W, self.BTN_H))
      x += self.BTN_W + self.GAP


class _PartitionRow(Widget):
  HEIGHT = 250
  BTN_W = 260
  BTN_H = 96
  GAP = 20

  def __init__(self, entry: dict, panel: "ExternalStoragePanel"):
    super().__init__()
    self._entry = entry
    self._panel = panel
    self._rect = rl.Rectangle(0, 0, 0, self.HEIGHT)
    self._font = gui_app.font(FontWeight.MEDIUM)
    self._small = gui_app.font(FontWeight.NORMAL)

    self._btn_toggle = self._child(Button(tr("Mount"), lambda: panel.toggle_mount(entry), font_size=40))
    self._btn_ext4 = self._child(Button(tr("Format ext4"), lambda: panel.confirm_format(entry, "ext4"),
                                        font_size=40, button_style=ButtonStyle.DANGER))
    self._btn_fat = self._child(Button(tr("Format FAT32"), lambda: panel.confirm_format(entry, "vfat"),
                                       font_size=40, button_style=ButtonStyle.DANGER))

  def set_parent_rect(self, parent_rect: rl.Rectangle) -> None:
    super().set_parent_rect(parent_rect)
    self._rect.width = parent_rect.width

  def _render(self, rect: rl.Rectangle) -> None:
    e = self._entry
    rl.draw_rectangle_rounded(rect, 0.08, 8, ROW_BG)

    mounted = bool(e["mountpoint"]) or os.path.ismount(os.path.join(MOUNT_ROOT, e["name"]))
    title = f"{e['path']}   {_human_size(e['size'])}   {e['fstype'] or tr('unformatted')}"
    if e["label"]:
      title += f"   [{e['label']}]"
    rl.draw_text_ex(self._font, title, rl.Vector2(rect.x + 40, rect.y + 26), 44, 0, TEXT_COLOR)

    if mounted:
      mp = e["mountpoint"] or os.path.join(MOUNT_ROOT, e["name"])
      rl.draw_text_ex(self._small, tr("Mounted at") + f" {mp}", rl.Vector2(rect.x + 40, rect.y + 92), 34, 0, GOOD_COLOR)
    else:
      rl.draw_text_ex(self._small, tr("Not mounted"), rl.Vector2(rect.x + 40, rect.y + 92), 34, 0, SUBTEXT_COLOR)
    if e["is_disk"]:
      rl.draw_text_ex(self._small, tr("whole disk (no partition table)"), rl.Vector2(rect.x + 40, rect.y + 138), 30, 0, WARN_COLOR)

    self._btn_toggle.set_text(tr("Unmount") if mounted else tr("Mount"))
    n = 3
    total_w = n * self.BTN_W + (n - 1) * self.GAP
    x = rect.x + rect.width - total_w - 40
    y = rect.y + (rect.height - self.BTN_H) / 2
    for btn in (self._btn_toggle, self._btn_ext4, self._btn_fat):
      btn.render(rl.Rectangle(x, y, self.BTN_W, self.BTN_H))
      x += self.BTN_W + self.GAP


class ExternalStoragePanel(NavWidget):
  """Developer panel to mount/unmount/format external (USB) drives."""

  def __init__(self):
    super().__init__()
    self._font = gui_app.font(FontWeight.MEDIUM)
    self._small = gui_app.font(FontWeight.NORMAL)
    self._scroller = Scroller([], spacing=16, line_separator=False, pad_end=True)
    self._rows: list = []
    self._status = ""
    self._reload_pending = False
    self._busy = False
    self._lock = threading.Lock()
    self._btn_rescan = self._child(Button(tr("Rescan"), self._reload, font_size=40))

  # ---- lifecycle ----
  def show_event(self) -> None:
    super().show_event()
    self._reload()

  def _reload(self) -> None:
    rows: list = []
    for disk in list_external():
      rows.append(_DriveRow(disk, _disk_size(disk), self))
      for entry in _disk_entries(disk):
        entry["mountpoint"] = entry["mountpoint"] or _mountpoint_of(entry["path"])
        rows.append(_PartitionRow(entry, self))
    self._scroller = Scroller(rows, spacing=16, line_separator=False, pad_end=True)
    self._scroller.show_event()
    self._rows = rows

  def _back_enabled(self) -> bool:
    # Only swipe-away when the list is scrolled to the top, otherwise a
    # downward drag must scroll the list instead of dismissing the panel.
    try:
      return self._scroller.scroll_panel.offset >= -20
    except Exception:
      return True

  def _set_status(self, text: str, failed: bool = False) -> None:
    with self._lock:
      self._status = ("! " if failed else "") + text

  # ---- actions ----
  def toggle_mount(self, entry: dict) -> None:
    if self._busy:
      return
    mounted = bool(entry["mountpoint"]) or os.path.ismount(os.path.join(MOUNT_ROOT, entry["name"]))
    self._run_action(unmount_partition if mounted else mount_partition, entry,
                     tr("Unmounted") + f" {entry['path']}" if mounted else tr("Mounted") + f" {entry['path']}")

  def confirm_format(self, entry: dict, fs: str) -> None:
    if self._busy:
      return

    def cb(result: int):
      if result == DialogResult.CONFIRM:
        self._run_action(format_partition, entry, tr("Formatted") + f" {entry['path']} ({fs})", fs)

    msg = tr("Erase ALL data on") + f" {entry['path']} " + tr("and format it as") + f" {fs}?"
    gui_app.push_widget(ConfirmDialog(msg, tr("Format"), callback=cb))

  def confirm_format_drive(self, disk: str, fs: str) -> None:
    if self._busy:
      return

    def cb(result: int):
      if result == DialogResult.CONFIRM:
        self._run_action(format_whole_drive, disk, tr("Formatted drive") + f" /dev/{disk} ({fs})", fs)

    msg = (tr("Erase ALL partitions and data on") + f" /dev/{disk} " +
           tr("and format the entire drive as") + f" {fs}?")
    gui_app.push_widget(ConfirmDialog(msg, tr("Format drive"), callback=cb))

  def _run_action(self, fn, entry: dict, ok_msg: str, *args) -> None:
    self._busy = True
    self._set_status(tr("Working..."))

    def worker():
      try:
        ok, err = fn(entry, *args)
        self._set_status(ok_msg if ok else f"{tr('Failed')}: {err or 'unknown error'}", failed=not ok)
      finally:
        self._busy = False
        self._reload_pending = True

    threading.Thread(target=worker, daemon=True).start()

  # ---- render ----
  def _update_state(self) -> None:
    super()._update_state()
    if self._reload_pending and not self._busy:
      self._reload_pending = False
      self._reload()

  def _render(self, rect: rl.Rectangle) -> None:
    if not ui_state.is_offroad():
      self._draw_center(rect, tr("External storage tools are only available while parked"))
      return

    rl.draw_text_ex(self._font, tr("External Storage"), rl.Vector2(rect.x, rect.y), 56, 0, TEXT_COLOR)

    info = tr("USB drives are mounted under") + f" {MOUNT_ROOT}/<device>."
    rl.draw_text_ex(self._small, info, rl.Vector2(rect.x, rect.y + 68), 34, 0, SUBTEXT_COLOR)
    warn = tr("Formatting permanently erases data. ext4 is recommended; FAT32 is limited to 4 GB files.")
    rl.draw_text_ex(self._small, warn, rl.Vector2(rect.x, rect.y + 110), 34, 0, WARN_COLOR)
    with self._lock:
      status = self._status
    if status:
      rl.draw_text_ex(self._small, status, rl.Vector2(rect.x, rect.y + 152), 34, 0, TEXT_COLOR)

    self._btn_rescan.render(rl.Rectangle(rect.x + rect.width - 240, rect.y - 4, 240, 90))

    list_y = rect.y + 200
    list_rect = rl.Rectangle(rect.x, list_y, rect.width, max(rect.height - (list_y - rect.y), 0))
    if self._rows:
      self._scroller.render(list_rect)
    else:
      self._draw_center(list_rect, tr("No external storage detected"))

  def _draw_center(self, rect: rl.Rectangle, text: str) -> None:
    size = measure_text_cached(self._font, text, 44)
    pos = rl.Vector2(rect.x + (rect.width - size.x) / 2, rect.y + (rect.height - size.y) / 2)
    rl.draw_text_ex(self._font, text, pos, 44, 0, rl.Color(200, 200, 200, 255))
