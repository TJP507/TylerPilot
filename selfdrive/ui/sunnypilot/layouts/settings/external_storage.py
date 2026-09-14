"""
External storage management for developer settings.

Enumerates external (USB) drives and lets the user mount, unmount and format
them. Formatting is FAT32 only: vfat is the only filesystem this device can
create that Windows also supports natively (ext4 needs a third-party Windows
driver; exFAT/NTFS are unsupported by the 4.9 kernel and its tools).

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

from openpilot.selfdrive.ui.ui_state import ui_state, device
from openpilot.system.hardware import PC
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

# Filesystems the device kernel can mount for an external data drive. Anything
# else (exFAT, NTFS, unformatted) must be reformatted before it can be mounted.
MOUNTABLE_FS = {"vfat", "msdos", "ext2", "ext3", "ext4"}

TEXT_COLOR = rl.Color(255, 255, 255, 255)
SUBTEXT_COLOR = rl.Color(170, 170, 170, 255)
WARN_COLOR = rl.Color(255, 180, 120, 255)
GOOD_COLOR = rl.Color(140, 220, 140, 255)

# Operation state lives at module scope so it survives the panel being closed
# and reopened while a format/mount is still running in the background.
_OP_LOCK = threading.Lock()
_OP: dict = {"active": False, "title": "", "label": "", "result": "", "failed": False, "progress": 0.0, "revision": 0}


def operation_active() -> bool:
  with _OP_LOCK:
    return _OP["active"]


# Keep the screen awake while a mount/unmount/format is running, even if the panel was dismissed.
device.add_keep_awake_callback(operation_active)


def operation_status() -> dict:
  with _OP_LOCK:
    return dict(_OP)


def _set_progress(value: float) -> None:
  with _OP_LOCK:
    _OP["progress"] = max(0.0, min(1.0, value))


def _draw_spinner(cx: float, cy: float, radius: float, color: rl.Color) -> None:
  start = (rl.get_time() * 320.0) % 360.0
  rl.draw_ring(rl.Vector2(cx, cy), radius - 10, radius, start, start + 250.0, 40, color)


# The panel registers a single interactive-timeout callback so it closes like
# every other screen instead of lingering after the display wakes back up.
_ACTIVE_PANEL = None
_TIMEOUT_CB_REGISTERED = False


def _dismiss_active_panel() -> None:
  panel = _ACTIVE_PANEL
  if panel is None:
    return
  # Pop any confirm dialogs sitting above the panel, then the panel itself.
  for _ in range(4):
    top = gui_app.get_active_widget()
    if top is None or top is panel:
      break
    gui_app.pop_widget()
  if gui_app.get_active_widget() is panel:
    gui_app.pop_widget()


# Auto-mount: the OS automount rule targets /mnt/sdcard, which cannot exist on
# this read-only rootfs, so a supported drive plugged in at boot (or inserted
# later) would otherwise never be mounted. The UI process runs for the whole
# session, so watch for external drives here and mount each one once when it
# appears. Manual unmounts stay unmounted until the drive is replugged.
_AUTOMOUNT_STARTED = False


def _automount_loop() -> None:
  known: set = set()
  while True:
    try:
      present = set(list_external())
      for disk in present - known:
        entry = _drive_mount_entry(disk)
        if entry is not None and not entry.get("mountpoint"):
          mount_partition(entry)
      known = present
    except Exception:
      pass
    time.sleep(5)


def start_automount() -> None:
  global _AUTOMOUNT_STARTED
  if _AUTOMOUNT_STARTED or PC:
    return
  _AUTOMOUNT_STARTED = True
  threading.Thread(target=_automount_loop, daemon=True, name="external_automount").start()


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


def format_whole_drive(disk: str) -> tuple:
  """Wipe the partition table, create one GPT partition spanning the drive, format it as FAT32."""
  path = f"/dev/{disk}"
  if not DISK_RE.match(disk) or not _is_external_path(path):
    return False, "refusing to format non-external device"

  # Release every partition on the disk (mounts and swap) before repartitioning.
  _set_progress(0.05)
  parts = [e["name"] for e in _disk_entries(disk) if not e.get("is_disk")]
  for part in parts:
    _run(["sudo", "umount", f"/dev/{part}"])
    _run(["sudo", "swapoff", f"/dev/{part}"])
  _run(["sudo", "umount", path])
  _run(["sudo", "swapoff", path])

  _set_progress(0.15)
  if _run(["sudo", "wipefs", "-a", path], timeout=60).returncode != 0:
    return False, "wipefs failed"
  _set_progress(0.3)
  if _run(["sudo", "parted", "-s", path, "mklabel", "gpt"], timeout=60).returncode != 0:
    return False, "mklabel failed"
  _set_progress(0.45)
  r = _run(["sudo", "parted", "-s", "-a", "optimal", path, "mkpart", "primary", "fat32", "1MiB", "100%"], timeout=120)
  if r.returncode != 0:
    return False, (r.stderr or r.stdout).strip()

  _set_progress(0.65)
  _run(["sudo", "partprobe", path], timeout=30)
  new_part = _partition_name(disk, 1)
  for _ in range(24):
    if os.path.exists(new_part):
      break
    time.sleep(0.25)
  if not os.path.exists(new_part):
    return False, "new partition did not appear"

  _set_progress(0.85)
  r = _run(["sudo", "mkfs.vfat", "-F", "32", "-n", FORMAT_LABEL, new_part], timeout=300)
  _set_progress(1.0)
  return r.returncode == 0, (r.stderr or r.stdout).strip()


def _fs_type(dev: str) -> str:
  r = _run(["sudo", "blkid", "-o", "value", "-s", "TYPE", dev])
  return r.stdout.strip() if r.returncode == 0 else ""


def mount_partition(entry: dict) -> tuple:
  if not _is_external_path(entry["path"]):
    return False, "refusing to mount non-external device"
  mnt = os.path.join(MOUNT_ROOT, entry["name"])
  try:
    os.makedirs(mnt, exist_ok=True)
  except OSError as e:
    return False, str(e)

  # The UI process (user "comma") must be able to write to the mount, otherwise
  # exports fail with permission errors. FAT has no on-disk ownership, so map it
  # via mount options; real filesystems get chowned after mounting.
  uid, gid = os.getuid(), os.getgid()
  fstype = _fs_type(entry["path"]).lower()
  if fstype and fstype not in MOUNTABLE_FS:
    return False, f"unsupported filesystem {fstype}"
  opts = ["nodev", "nosuid"]
  if fstype in ("vfat", "msdos", "exfat"):
    opts += [f"uid={uid}", f"gid={gid}", "umask=000"]

  r = _run(["sudo", "mount", "-o", ",".join(opts), entry["path"], mnt])
  if r.returncode != 0:
    return False, (r.stderr or r.stdout).strip()
  if fstype not in ("vfat", "msdos", "exfat"):
    _run(["sudo", "chown", f"{uid}:{gid}", mnt])
  return True, ""


def unmount_partition(entry: dict) -> tuple:
  if not _is_external_path(entry["path"]):
    return False, "refusing to unmount non-external device"
  target = entry["mountpoint"] or entry["path"]
  r = _run(["sudo", "umount", target])
  if r.returncode != 0:
    r = _run(["sudo", "umount", entry["path"]])
  return r.returncode == 0, (r.stderr or r.stdout).strip()


def _drive_mount_entry(disk: str) -> dict:
  """Mount target for a drive: its first partition, or the disk if unpartitioned."""
  parts = [e for e in _disk_entries(disk) if not e.get("is_disk")]
  if parts:
    entry = dict(parts[0])
  else:
    entry = {"name": disk, "path": f"/dev/{disk}", "size": _disk_size(disk), "fstype": "",
             "label": "", "mountpoint": "", "is_disk": True}
  entry["name"] = disk  # mount at /data/external/<disk>
  entry["mountpoint"] = _mountpoint_of(entry["path"])
  return entry


def first_mounted_external():
  """Return the mount entry of the first mounted external drive, or None."""
  for disk in list_external():
    entry = _drive_mount_entry(disk)
    if entry is not None and entry.get("mountpoint"):
      return entry
  return None


class _DriveRow(Widget):
  """One row per external drive: mount/unmount and whole-drive format."""

  HEIGHT = 230
  BTN_W = 300
  BTN_H = 96
  GAP = 20

  def __init__(self, disk: str, size: int, entry: dict, panel: "ExternalStoragePanel"):
    super().__init__()
    self._disk = disk
    self._size = size
    self._entry = entry
    self._rect = rl.Rectangle(0, 0, 0, self.HEIGHT)
    self._font = gui_app.font(FontWeight.MEDIUM)
    self._small = gui_app.font(FontWeight.NORMAL)

    self._btn_toggle = self._child(Button(tr("Mount"), lambda: panel.toggle_mount(entry), font_size=40))
    self._btn_fat = self._child(Button(tr("Format Storage"), lambda: panel.confirm_format_drive(disk),
                                       font_size=40, button_style=ButtonStyle.DANGER))

  def set_parent_rect(self, parent_rect: rl.Rectangle) -> None:
    super().set_parent_rect(parent_rect)
    self._rect.width = parent_rect.width

  def _render(self, rect: rl.Rectangle) -> None:
    e = self._entry
    rl.draw_rectangle_rounded(rect, 0.08, 8, rl.Color(58, 40, 40, 255))

    mounted = bool(e["mountpoint"]) or os.path.ismount(os.path.join(MOUNT_ROOT, self._disk))
    fstype = (e.get("fstype") or "").lower()
    mountable = fstype in MOUNTABLE_FS
    title = f"/dev/{self._disk}   {_human_size(self._size)}"
    rl.draw_text_ex(self._font, title, rl.Vector2(rect.x + 40, rect.y + 26), 44, 0, TEXT_COLOR)

    if mounted:
      mp = e["mountpoint"] or os.path.join(MOUNT_ROOT, self._disk)
      rl.draw_text_ex(self._small, tr("Mounted at") + f" {mp}", rl.Vector2(rect.x + 40, rect.y + 92), 34, 0, GOOD_COLOR)
    elif mountable:
      rl.draw_text_ex(self._small, tr("Not mounted"), rl.Vector2(rect.x + 40, rect.y + 92), 34, 0, SUBTEXT_COLOR)
    else:
      hint = tr("Unsupported filesystem") + f" ({fstype or tr('unformatted')}) - " + tr("format the disk to use it")
      rl.draw_text_ex(self._small, hint, rl.Vector2(rect.x + 40, rect.y + 92), 34, 0, WARN_COLOR)

    self._btn_toggle.set_text(tr("Unmount") if mounted else tr("Mount"))
    busy = operation_active()
    n = 2
    total_w = n * self.BTN_W + (n - 1) * self.GAP
    x = rect.x + rect.width - total_w - 40
    y = rect.y + (rect.height - self.BTN_H) / 2
    for btn in (self._btn_toggle, self._btn_fat):
      if btn is self._btn_fat:
        # Formatting only makes sense on an unmounted drive.
        btn.set_enabled(not busy and not mounted)
      else:
        # Mount stays disabled until the drive has a filesystem we can actually mount.
        btn.set_enabled(not busy and (mounted or mountable))
      btn.render(rl.Rectangle(x, y, self.BTN_W, self.BTN_H))
      x += self.BTN_W + self.GAP


def _draw_progress_bar(rect: rl.Rectangle, frac: float, indeterminate: bool) -> None:
  rl.draw_rectangle_rounded(rect, 0.5, 8, rl.Color(60, 60, 60, 255))
  fill = rl.Color(80, 160, 255, 255)
  if indeterminate:
    seg = rect.width * 0.3
    span = rect.width - seg
    x = rect.x + span * ((rl.get_time() * 0.6) % 1.0)
    rl.draw_rectangle_rounded(rl.Rectangle(x, rect.y, seg, rect.height), 0.5, 8, fill)
  elif frac > 0:
    rl.draw_rectangle_rounded(rl.Rectangle(rect.x, rect.y, rect.width * frac, rect.height), 0.5, 8, fill)


class StorageProgressDialog(NavWidget):
  """Modal progress dialog shown while mounting or formatting a drive."""

  def __init__(self):
    super().__init__()
    self._font = gui_app.font(FontWeight.MEDIUM)
    self._small = gui_app.font(FontWeight.NORMAL)
    self._btn_close = self._child(Button(tr("Close"), lambda: self.dismiss(), font_size=44, button_style=ButtonStyle.PRIMARY))

  def _back_enabled(self) -> bool:
    return False

  def _render(self, rect: rl.Rectangle) -> None:
    status = operation_status()
    active = status["active"]
    rl.draw_rectangle_rec(rect, rl.Color(21, 21, 21, 255))

    title = status["title"] or tr("Working")
    rl.draw_text_ex(self._font, title, rl.Vector2(rect.x + 60, rect.y + 60), 56, 0, TEXT_COLOR)

    bar = rl.Rectangle(rect.x + 60, rect.y + 210, rect.width - 120, 46)
    _draw_progress_bar(bar, status["progress"], indeterminate=(active and status["progress"] <= 0.0))

    label = status["label"] if active else status["result"]
    color = WARN_COLOR if active else (rl.RED if status["failed"] else GOOD_COLOR)
    if label:
      rl.draw_text_ex(self._small, label, rl.Vector2(bar.x, bar.y + bar.height + 24), 36, 0, color)

    if not active:
      self._btn_close.render(rl.Rectangle(rect.x + rect.width - 60 - 300, rect.y + rect.height - 160, 300, 110))


class ExternalStoragePanel(NavWidget):
  """Developer panel to mount/unmount/format external (USB) drives."""

  def __init__(self):
    super().__init__()
    self._font = gui_app.font(FontWeight.MEDIUM)
    self._small = gui_app.font(FontWeight.NORMAL)
    self._scroller = Scroller([], spacing=16, line_separator=False, pad_end=True)
    self._rows: list = []
    self._reload_pending = False
    self._seen_revision = operation_status()["revision"]
    self._btn_back = self._child(Button(tr("Back"), lambda: self.dismiss(), font_size=40))
    self._btn_rescan = self._child(Button(tr("Rescan"), self._reload, font_size=40))

    global _TIMEOUT_CB_REGISTERED
    if not _TIMEOUT_CB_REGISTERED:
      device.add_interactive_timeout_callback(_dismiss_active_panel)
      _TIMEOUT_CB_REGISTERED = True

  # Back navigation is via the on-screen Back button only: swipe-to-dismiss
  # fights with list scrolling, so disable the NavWidget gesture entirely.
  def _back_enabled(self) -> bool:
    return False

  # ---- lifecycle ----
  def show_event(self) -> None:
    super().show_event()
    global _ACTIVE_PANEL
    _ACTIVE_PANEL = self
    self._reload()

  def hide_event(self) -> None:
    super().hide_event()
    global _ACTIVE_PANEL
    if _ACTIVE_PANEL is self:
      _ACTIVE_PANEL = None

  def _reload(self) -> None:
    rows: list = []
    for disk in list_external():
      rows.append(_DriveRow(disk, _disk_size(disk), _drive_mount_entry(disk), self))
    self._scroller = Scroller(rows, spacing=16, line_separator=False, pad_end=True)
    self._scroller.show_event()
    self._rows = rows
    self._seen_revision = operation_status()["revision"]

  # ---- actions ----
  def toggle_mount(self, entry: dict) -> None:
    if operation_active():
      return
    mounted = bool(entry["mountpoint"]) or os.path.ismount(os.path.join(MOUNT_ROOT, entry["name"]))
    if not mounted and (entry.get("fstype") or "").lower() not in MOUNTABLE_FS:
      return
    if mounted:
      self._run_action(unmount_partition, entry, tr("Unmounting") + f" {entry['path']}",
                       tr("Unmounted") + f" {entry['path']}", tr("Unmounting drive"))
    else:
      self._run_action(mount_partition, entry, tr("Mounting") + f" {entry['path']}",
                       tr("Mounted") + f" {entry['path']}", tr("Mounting drive"))

  def confirm_format_drive(self, disk: str) -> None:
    if operation_active():
      return

    def cb(result: int):
      if result == DialogResult.CONFIRM:
        self._run_action(format_whole_drive, disk, tr("Formatting") + f" /dev/{disk}",
                         tr("Formatted") + f" /dev/{disk}", tr("Formatting storage"))

    msg = tr("Are you sure you want to delete all of the data on this device?")
    gui_app.push_widget(ConfirmDialog(msg, tr("Format Storage"), callback=cb, confirm_style=ButtonStyle.DANGER))

  def _run_action(self, fn, arg, start_label: str, done_label: str, title: str, *args) -> None:
    with _OP_LOCK:
      if _OP["active"]:
        return
      _OP["active"] = True
      _OP["title"] = title
      _OP["label"] = start_label + "..."
      _OP["result"] = ""
      _OP["failed"] = False
      _OP["progress"] = 0.0
      self._seen_revision = _OP["revision"]

    def worker():
      try:
        ok, err = fn(arg, *args)
      except Exception as e:  # noqa: BLE001 - report, never crash the UI
        ok, err = False, str(e)
      with _OP_LOCK:
        _OP["active"] = False
        _OP["progress"] = 1.0
        _OP["result"] = done_label if ok else f"{tr('Failed')}: {err or 'unknown error'}"
        _OP["failed"] = not ok
        _OP["revision"] += 1

    threading.Thread(target=worker, daemon=True).start()
    gui_app.push_widget(StorageProgressDialog())

  # ---- render ----
  def _update_state(self) -> None:
    super()._update_state()
    if operation_status()["revision"] != self._seen_revision:
      self._reload_pending = True
    if self._reload_pending and not operation_active():
      self._reload_pending = False
      self._reload()

  def _render(self, rect: rl.Rectangle) -> None:
    if not ui_state.is_offroad():
      self._draw_center(rect, tr("External storage tools are only available while parked"))
      return

    self._btn_back.render(rl.Rectangle(rect.x, rect.y, 200, 84))
    self._btn_rescan.render(rl.Rectangle(rect.x + rect.width - 240, rect.y, 240, 84))
    rl.draw_text_ex(self._font, tr("External Storage"), rl.Vector2(rect.x + 240, rect.y + 8), 56, 0, TEXT_COLOR)

    status = operation_status()
    active, label, result, failed = status["active"], status["label"], status["result"], status["failed"]
    line = label if active else result
    if line:
      color = WARN_COLOR if active else (rl.RED if failed else GOOD_COLOR)
      text_x = rect.x
      if active:
        _draw_spinner(rect.x + 18, rect.y + 127, 18, color)
        text_x = rect.x + 52
      rl.draw_text_ex(self._small, line, rl.Vector2(text_x, rect.y + 110), 34, 0, color)

    list_y = rect.y + 170
    list_rect = rl.Rectangle(rect.x, list_y, rect.width, max(rect.height - (list_y - rect.y), 0))
    if self._rows:
      self._scroller.render(list_rect)
    else:
      self._draw_center(list_rect, tr("No external storage detected"))

  def _draw_center(self, rect: rl.Rectangle, text: str) -> None:
    size = measure_text_cached(self._font, text, 44)
    pos = rl.Vector2(rect.x + (rect.width - size.x) / 2, rect.y + (rect.height - size.y) / 2)
    rl.draw_text_ex(self._font, text, pos, 44, 0, rl.Color(200, 200, 200, 255))
