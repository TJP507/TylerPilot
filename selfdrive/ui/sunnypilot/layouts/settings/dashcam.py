"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

On-device dash cam browser and simple video player.

Recordings live in Paths.log_root() (/data/media/0/realdata) as one directory per
60 second segment (<route>--<segment>). Each segment contains raw HEVC elementary
streams (fcamera.hevc / ecamera.hevc / dcamera.hevc) plus an H.264/TS cabin stream
(qcamera.ts).

Raw HEVC has no container timestamps, so seeking is done through openpilot's HEVC
index (frame -> byte offset). On comma 3X the Qualcomm msm_vidc hardware decoder
(tools/dashcam/hwdec) is used for real-time playback; a software PyAV decoder is
kept as a fallback when the helper is unavailable.

Playback is only permitted while the device is offroad.
"""
import bisect
import fcntl
import io
import os
import select
import shutil
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

import numpy as np
import pyray as rl

from openpilot.common.basedir import BASEDIR
from openpilot.system.hardware.hw import Paths
from openpilot.selfdrive.ui.ui_state import ui_state, device
from openpilot.selfdrive.ui.sunnypilot.layouts.settings.external_storage import first_mounted_external
from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets import Widget, DialogResult
from openpilot.system.ui.widgets.button import Button, ButtonStyle
from openpilot.system.ui.widgets.confirm_dialog import ConfirmDialog
from openpilot.system.ui.widgets.nav_widget import NavWidget
from openpilot.system.ui.widgets.scroller_tici import Scroller

_DBG = os.getenv("DASHCAM_DEBUG") is not None


def _dbg(*args) -> None:
  if _DBG:
    print("[dashcam]", *args, file=sys.stderr, flush=True)

HWDEC_PATH = os.path.join(BASEDIR, "tools", "dashcam", "hwdec")

# GLES YUV -> RGB shader so NV12 frames are color-converted on the GPU.
YUV_VERTEX_SHADER = """
#version 300 es
in vec3 vertexPosition;
in vec2 vertexTexCoord;
in vec3 vertexNormal;
in vec4 vertexColor;
uniform mat4 mvp;
out vec2 fragTexCoord;
out vec4 fragColor;
void main() {
  fragTexCoord = vertexTexCoord;
  fragColor = vertexColor;
  gl_Position = mvp * vec4(vertexPosition, 1.0);
}
"""

YUV_FRAGMENT_SHADER = """
#version 300 es
precision mediump float;
in vec2 fragTexCoord;
uniform sampler2D texture0;
uniform sampler2D texture1;
out vec4 fragColor;
void main() {
  float y = texture(texture0, fragTexCoord).r;
  vec2 uv = texture(texture1, fragTexCoord).ra - 0.5;
  fragColor = vec4(y + 1.402 * uv.y, y - 0.344 * uv.x - 0.714 * uv.y, y + 1.772 * uv.x, 1.0);
}
"""

# (display name, file name inside each segment)
CAMERAS = [
  ("Front", "fcamera.hevc"),
  ("Wide", "ecamera.hevc"),
  ("Driver", "dcamera.hevc"),
  ("Cabin", "qcamera.ts"),
]

DISPLAY_W = 960
DISPLAY_H = 600
PLAYER_FPS = 20.0  # openpilot records 1200 frames per 60s segment

PANEL_BG = rl.Color(41, 41, 41, 255)
ROW_BG = rl.Color(41, 41, 41, 255)
ROW_BG_PRESSED = rl.Color(74, 74, 74, 255)
SUBTEXT_COLOR = rl.Color(170, 170, 170, 255)

_ACTIVE_PLAYER = None  # set while a DashCamPlayer is on the nav stack
_ACTIVE_DIALOG = None  # set while the export progress dialog is on the nav stack
_TIMEOUT_CB_REGISTERED = False


def _dismiss_active_player() -> None:
  # The settings menu closes itself on the interactive timeout, but a pushed
  # player or export dialog would otherwise be left on screen.
  if _ACTIVE_DIALOG is not None:
    _ACTIVE_DIALOG.dismiss()
  elif _ACTIVE_PLAYER is not None:
    _ACTIVE_PLAYER.graceful_close()


@dataclass
class Clip:
  path: str
  name: str
  segment: int
  mtime: float

  @property
  def date_text(self) -> str:
    return time.strftime("%b %d, %Y", time.localtime(self.mtime))

  @property
  def time_text(self) -> str:
    return time.strftime("%I:%M:%S %p", time.localtime(self.mtime))


def _fmt_time(seconds: float) -> str:
  s = max(0, int(seconds))
  return f"{s // 60:02d}:{s % 60:02d}"


def list_clips(camera_file: str) -> list[Clip]:
  """Enumerate finished segments that contain the requested camera stream, newest first."""
  clips: list[Clip] = []
  root = Paths.log_root()
  try:
    names = os.listdir(root)
  except OSError:
    return clips

  for name in names:
    seg_dir = os.path.join(root, name)
    if not os.path.isdir(seg_dir):
      continue
    # Skip segments still being written
    if os.path.exists(os.path.join(seg_dir, "rlog.lock")):
      continue
    path = os.path.join(seg_dir, camera_file)
    if not os.path.isfile(path):
      continue
    try:
      mtime = os.path.getmtime(path)
    except OSError:
      continue
    _, _, seg = name.rpartition("--")
    clips.append(Clip(path, name, int(seg) if seg.isdigit() else 0, mtime))

  clips.sort(key=lambda c: c.mtime, reverse=True)
  return clips


# ---------------------------------------------------------------------------
# Export: remux a segment's cameras to .mp4 on a mounted USB drive.
# ---------------------------------------------------------------------------
FFMPEG = "/usr/local/venv/bin/ffmpeg"
EXPORT_DIR_NAME = "tylerpilot"
SEGMENT_SECONDS = 60.0
# camera file -> export subfolder on the USB drive
CAMERA_FOLDER = {"fcamera.hevc": "front", "ecamera.hevc": "wide", "dcamera.hevc": "driver", "qcamera.ts": "cabin"}

_EXPORT_LOCK = threading.Lock()
_EXPORT: dict = {"active": False, "done": 0, "total": 0, "fraction": 0.0, "current": "",
                 "cancel": False, "result": "", "failed": False, "revision": 0}


def export_status() -> dict:
  with _EXPORT_LOCK:
    return dict(_EXPORT)


def export_active() -> bool:
  with _EXPORT_LOCK:
    return _EXPORT["active"]


# Keep the screen awake while an export is running, even if the dialog was dismissed.
device.add_keep_awake_callback(export_active)


def request_export_cancel() -> None:
  with _EXPORT_LOCK:
    _EXPORT["cancel"] = True


_MOUNT_CACHE = {"t": -10.0, "mp": None}


def export_mountpoint():
  """Mountpoint of the first mounted external drive, cached briefly."""
  now = time.monotonic()
  if now - _MOUNT_CACHE["t"] > 2.0:
    _MOUNT_CACHE["t"] = now
    entry = first_mounted_external()
    _MOUNT_CACHE["mp"] = entry.get("mountpoint") if entry else None
  return _MOUNT_CACHE["mp"]


def segment_cameras(seg_dir: str) -> list:
  return [(CAMERA_FOLDER[file], os.path.join(seg_dir, file)) for _, file in CAMERAS
          if os.path.isfile(os.path.join(seg_dir, file))]


def segment_mtime(seg_dir: str) -> float:
  mt = 0.0
  for _, file in CAMERAS:
    path = os.path.join(seg_dir, file)
    if os.path.isfile(path):
      try:
        mt = max(mt, os.path.getmtime(path))
      except OSError:
        pass
  if mt == 0.0:
    try:
      mt = os.path.getmtime(seg_dir)
    except OSError:
      mt = 0.0
  return mt


def segments_on_date(date: str) -> list:
  """Every finished segment directory (any camera) recorded on `date`."""
  out: list = []
  root = Paths.log_root()
  try:
    names = os.listdir(root)
  except OSError:
    return out
  for name in names:
    seg_dir = os.path.join(root, name)
    if "--" not in name or not os.path.isdir(seg_dir):
      continue
    if os.path.exists(os.path.join(seg_dir, "rlog.lock")):
      continue
    mt = segment_mtime(seg_dir)
    if mt and time.strftime("%Y-%m-%d", time.localtime(mt)) == date:
      out.append(seg_dir)
  return out


def list_date_counts() -> dict:
  """Number of finished segments recorded on each date, across all cameras."""
  counts: dict = {}
  root = Paths.log_root()
  try:
    names = os.listdir(root)
  except OSError:
    return counts
  for name in names:
    seg_dir = os.path.join(root, name)
    if "--" not in name or not os.path.isdir(seg_dir):
      continue
    if os.path.exists(os.path.join(seg_dir, "rlog.lock")):
      continue
    mt = segment_mtime(seg_dir)
    if mt:
      date = time.strftime("%Y-%m-%d", time.localtime(mt))
      counts[date] = counts.get(date, 0) + 1
  return counts


def delete_segments(seg_dirs: list) -> tuple:
  """Delete segment directories under the log root. Returns (deleted, errors)."""
  root = os.path.realpath(Paths.log_root())
  deleted = 0
  errors = 0
  for seg_dir in seg_dirs:
    real = os.path.realpath(seg_dir)
    if not real.startswith(root + os.sep) or "--" not in os.path.basename(real) or not os.path.isdir(real):
      errors += 1
      continue
    if os.path.exists(os.path.join(real, "rlog.lock")):
      errors += 1
      continue
    try:
      shutil.rmtree(real)
      deleted += 1
    except OSError:
      errors += 1
  return deleted, errors


def _remux_one(src: str, dst: str) -> tuple:
  """Stream-copy src to an mp4 at dst via ffmpeg. Returns (ok, cancelled, error)."""
  cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-y"]
  if not src.endswith(".ts"):
    # Raw HEVC carries no container timestamps; generate them at the record rate.
    cmd += ["-fflags", "+genpts", "-r", str(int(PLAYER_FPS))]
  cmd += ["-i", src, "-c", "copy", dst]
  try:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
  except OSError as e:
    return False, False, str(e)

  cancelled = False
  if proc.stdout is not None:
    for line in proc.stdout:
      line = line.strip()
      if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
        try:
          us = int(line.split("=", 1)[1])
        except ValueError:
          continue
        with _EXPORT_LOCK:
          _EXPORT["fraction"] = min((us / 1e6) / SEGMENT_SECONDS, 1.0)
          cancel = _EXPORT["cancel"]
        if cancel and not cancelled:
          cancelled = True
          proc.terminate()
      elif line.startswith("progress=end"):
        break
  err = ""
  proc.wait()
  if cancelled or proc.returncode != 0:
    if proc.stderr is not None:
      try:
        err = proc.stderr.read().strip()
      except Exception:
        err = ""
    try:
      os.remove(dst)
    except OSError:
      pass
    return False, cancelled, err
  return True, False, ""


def _run_export(segments: list, mountpoint: str) -> None:
  """segments is a list of (seg_dir, seg_mtime). Writes into <usb>/tylerpilot/."""
  plan = []
  total = 0
  for seg_dir, mtime in segments:
    cams = segment_cameras(seg_dir)
    if cams:
      plan.append((seg_dir, mtime, cams))
      total += len(cams)

  with _EXPORT_LOCK:
    _EXPORT.update(done=0, total=total, fraction=0.0, current="", cancel=False)

  done = 0
  errors = 0
  cancelled = False
  last_error = ""
  for _seg_dir, mtime, cams in plan:
    date = time.strftime("%Y-%m-%d", time.localtime(mtime))
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(mtime))
    for folder, src in cams:
      if cancelled:
        break
      out_dir = os.path.join(mountpoint, EXPORT_DIR_NAME, date, folder)
      try:
        os.makedirs(out_dir, exist_ok=True)
      except OSError as e:
        errors += 1
        done += 1
        last_error = str(e)
        continue
      dst = os.path.join(out_dir, f"{stamp}_{folder}.mp4")
      with _EXPORT_LOCK:
        _EXPORT["current"] = os.path.basename(dst)
        _EXPORT["fraction"] = 0.0
      ok, was_cancelled, err = _remux_one(src, dst)
      if was_cancelled:
        cancelled = True
      else:
        done += 1
        if not ok:
          errors += 1
          last_error = err or last_error
      with _EXPORT_LOCK:
        _EXPORT["done"] = done
        _EXPORT["fraction"] = 0.0
    if cancelled:
      break

  root = os.path.join(mountpoint, EXPORT_DIR_NAME)
  if cancelled:
    result = tr("Cancelled") + f". {done}/{total} " + tr("file(s) exported to") + f" {root}"
  elif errors:
    reason = f": {last_error[:100]}" if last_error else ""
    result = f"{done - errors} " + tr("exported,") + f" {errors} " + tr("failed") + reason + f" -> {root}"
  else:
    result = f"{done} " + tr("file(s) exported to") + f" {root}"
  with _EXPORT_LOCK:
    _EXPORT["result"] = result
    _EXPORT["failed"] = errors > 0


def start_export(segments: list, mountpoint: str) -> bool:
  with _EXPORT_LOCK:
    if _EXPORT["active"]:
      return False
    _EXPORT.update(active=True, cancel=False, done=0, total=0, fraction=0.0, current="", result="", failed=False)

  def worker():
    try:
      _run_export(segments, mountpoint)
    except Exception as e:
      with _EXPORT_LOCK:
        _EXPORT["result"] = tr("Export failed") + f": {e}"
        _EXPORT["failed"] = True
    finally:
      with _EXPORT_LOCK:
        _EXPORT["active"] = False
        _EXPORT["revision"] += 1

  threading.Thread(target=worker, daemon=True, name="dashcam_export").start()
  return True


class _SoftwareDecoder(threading.Thread):
  """Background software HEVC/TS decoder. Produces RGBA frames at a fixed size."""

  def __init__(self, path: str, out_w: int, out_h: int):
    super().__init__(daemon=True)
    self._path = path
    self._w = out_w
    self._h = out_h

    self._stop_ev = threading.Event()
    self._play_ev = threading.Event()
    self._play_ev.set()
    self._lock = threading.Lock()

    self._frame: np.ndarray | None = None
    self._frame_w = 0
    self._frame_h = 0
    self._seq = 0
    self._fidx = 0
    self._total = 0
    self._eof = False
    self._error: str | None = None
    self._seek_to: int | None = None

  # ---- thread control (called from UI thread) ----
  def stop(self) -> None:
    self._stop_ev.set()
    self._play_ev.set()

  def set_playing(self, playing: bool) -> None:
    if playing:
      self._play_ev.set()
    else:
      self._play_ev.clear()

  def request_seek(self, frame_idx: int) -> None:
    with self._lock:
      self._seek_to = max(frame_idx, 0)

  def snapshot(self):
    with self._lock:
      return self._frame, self._frame_w, self._frame_h, self._seq, self._fidx, self._total, self._eof, self._error

  # ---- helpers ----
  def _wait_if_paused(self) -> None:
    while not self._play_ev.is_set() and not self._stop_ev.is_set():
      time.sleep(0.02)

  def _wait_for_seek_or_stop(self) -> None:
    while not self._stop_ev.is_set():
      with self._lock:
        if self._seek_to is not None:
          return
      time.sleep(0.05)

  def _take_seek(self) -> int | None:
    with self._lock:
      seek = self._seek_to
      self._seek_to = None
      return seek

  def _has_pending_seek(self) -> bool:
    with self._lock:
      return self._seek_to is not None

  def _publish(self, frame, fidx: int) -> None:
    img = frame.reformat(format="nv12")
    arr = np.ascontiguousarray(img.to_ndarray()).reshape(-1)
    with self._lock:
      self._frame = arr
      self._frame_w = img.width
      self._frame_h = img.height
      self._seq += 1
      self._fidx = fidx

  # ---- thread body ----
  def run(self) -> None:
    try:
      if self._path.endswith(".hevc"):
        self._run_hevc()
      else:
        self._run_ts()
    except Exception as e:
      with self._lock:
        self._error = str(e) or type(e).__name__
        self._eof = True

  def _run_hevc(self) -> None:
    import av
    from openpilot.tools.lib.vidindex import hevc_index

    frames, file_total, prefix = hevc_index(self._path)
    if len(frames) < 2:
      raise RuntimeError("no decodable frames")

    keyframes = [i for i, (slice_type, _) in enumerate(frames) if slice_type == 2]
    if not keyframes:
      keyframes = [0]
    gop_starts = keyframes + [len(frames)]

    with self._lock:
      self._total = len(frames)

    gop_idx = 0
    while not self._stop_ev.is_set():
      seek = self._take_seek()
      if seek is not None:
        gop_idx = max(bisect.bisect_right(keyframes, seek) - 1, 0)
        skip = seek - keyframes[gop_idx]
      else:
        skip = 0

      if gop_idx >= len(keyframes):
        with self._lock:
          self._eof = True
        self._wait_for_seek_or_stop()
        with self._lock:
          self._eof = False
        continue

      frame_base = keyframes[gop_idx]
      frame_end = gop_starts[gop_idx + 1]
      off_b = frames[frame_base][1]
      off_e = frames[frame_end][1] if frame_end < len(frames) else file_total

      with open(self._path, "rb") as fh:
        fh.seek(off_b)
        data = prefix + fh.read(off_e - off_b)

      container = av.open(io.BytesIO(data), format="hevc")
      stream = container.streams.video[0]
      stream.thread_type = "FRAME"
      try:
        stream.thread_count = 8
      except Exception:
        pass

      emitted = 0
      seeking = seek is not None
      for frame in container.decode(stream):
        if self._stop_ev.is_set():
          container.close()
          return
        if emitted < skip:
          emitted += 1
          continue
        if self._has_pending_seek():
          break
        if not seeking:
          self._wait_if_paused()
          if self._stop_ev.is_set():
            container.close()
            return
        self._publish(frame, frame_base + emitted)
        emitted += 1
        seeking = False
      container.close()

      if self._has_pending_seek():
        continue
      gop_idx += 1

  def _run_ts(self) -> None:
    import av

    container = av.open(self._path)
    stream = container.streams.video[0]
    stream.thread_type = "FRAME"
    try:
      stream.thread_count = 8
    except Exception:
      pass
    fps = float(stream.average_rate) if stream.average_rate else PLAYER_FPS
    if container.duration:
      with self._lock:
        self._total = int(container.duration / av.time_base * fps)

    while not self._stop_ev.is_set():
      seek = self._take_seek()
      if seek is not None:
        try:
          container.seek(int(seek / fps * av.time_base), backward=True)
        except Exception:
          pass

      seeking = seek is not None
      for frame in container.decode(stream):
        if self._stop_ev.is_set():
          container.close()
          return
        if self._has_pending_seek():
          break
        if not seeking:
          self._wait_if_paused()
          if self._stop_ev.is_set():
            container.close()
            return
        t = frame.time if frame.time is not None else 0.0
        self._publish(frame, int(t * fps))
        seeking = False

      if self._has_pending_seek():
        continue
      with self._lock:
        self._eof = True
      self._wait_for_seek_or_stop()
      with self._lock:
        self._eof = False

    container.close()


class _HardwareDecoder(threading.Thread):
  """Background decoder that drives the Qualcomm msm_vidc helper (tools/dashcam/hwdec)."""

  def __init__(self, path: str, out_w: int, out_h: int):
    super().__init__(daemon=True)
    self._path = path
    self._w = out_w
    self._h = out_h

    self._stop_ev = threading.Event()
    self._play_ev = threading.Event()
    self._play_ev.set()
    self._lock = threading.Lock()

    self._frame: np.ndarray | None = None
    self._frame_w = 0
    self._frame_h = 0
    self._seq = 0
    self._fidx = 0
    self._total = 0
    self._eof = False
    self._error: str | None = None
    self._seek_to: int | None = None
    self._flush_target = -1

    self._img_w = 0
    self._img_h = 0
    self._files: list = []
    self._prefix = b""

  # ---- thread control (called from UI thread) ----
  def stop(self) -> None:
    self._stop_ev.set()
    self._play_ev.set()

  def set_playing(self, playing: bool) -> None:
    if playing:
      self._play_ev.set()
    else:
      self._play_ev.clear()

  def request_seek(self, frame_idx: int) -> None:
    with self._lock:
      self._seek_to = max(frame_idx, 0)

  def snapshot(self):
    with self._lock:
      return self._frame, self._frame_w, self._frame_h, self._seq, self._fidx, self._total, self._eof, self._error

  # ---- helpers ----
  @staticmethod
  def _read_exact(stream, n: int) -> bytes | None:
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
      chunk = stream.read(n - got)
      if not chunk:
        return None
      view[got:got + len(chunk)] = chunk
      got += len(chunk)
    return bytes(buf)

  def _take_seek(self) -> int | None:
    with self._lock:
      seek = self._seek_to
      self._seek_to = None
      return seek

  # ---- session: one hwdec process fed from start_idx ----
  def _session(self, start_idx: int) -> None:
    try:
      proc = subprocess.Popen([HWDEC_PATH, str(self._img_w), str(self._img_h)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as e:
      with self._lock:
        self._error = f"hardware decoder unavailable: {e}"
      return
    session_stop = threading.Event()
    frames = self._files

    # Writes must never block: while paused the decoder's input pipe fills up,
    # and a blocking write would strand the feed thread where it could never
    # observe a pending seek. A non-blocking pipe lets us poll stop/seek.
    stdin_fd = proc.stdin.fileno()
    try:
      fl = fcntl.fcntl(stdin_fd, fcntl.F_GETFL)
      fcntl.fcntl(stdin_fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
    except OSError:
      pass

    def write_all(data: bytes) -> bool:
      view = memoryview(data)
      off = 0
      while off < len(view):
        if self._stop_ev.is_set() or session_stop.is_set():
          return False
        with self._lock:
          if self._seek_to is not None:
            return False
        try:
          off += os.write(stdin_fd, view[off:])
        except BlockingIOError:
          time.sleep(0.005)
        except OSError:
          return False
      return True

    def feed():
      i = start_idx
      normal = False
      try:
        # HEVC parameter sets (VPS/SPS/PPS) only appear once, at the top of the
        # file. A session that starts mid-file must re-send them before the
        # first IDR, otherwise msm_vidc emits blank frames.
        if start_idx > 0 and self._prefix:
          if not write_all(struct.pack("<I", len(self._prefix)) + self._prefix):
            return
        with open(self._path, "rb") as fh:
          while i < len(frames) and not self._stop_ev.is_set():
            with self._lock:
              if self._seek_to is not None:
                return
            _key, pos, size = frames[i]
            fh.seek(pos)
            data = fh.read(size)
            if not write_all(struct.pack("<I", len(data)) + data):
              return
            i += 1
          normal = i >= len(frames) and not self._stop_ev.is_set()
      finally:
        _dbg("feed end at", i, "normal", normal)
        # On natural end-of-clip, close stdin so the decoder flushes (EOS) and
        # exits. Any other exit means the session is being torn down, so make
        # sure the reader loop stops waiting on it.
        if normal:
          try:
            if proc.stdin is not None:
              proc.stdin.close()
          except OSError:
            pass
        else:
          session_stop.set()

    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    _dbg("session start", start_idx, "img", self._img_w, self._img_h)

    frames_read = 0
    next_frame_time = 0.0
    while not session_stop.is_set() and not self._stop_ev.is_set():
      playing = self._play_ev.is_set()
      with self._lock:
        flush_target = self._flush_target
      # While paused (and not flushing toward a seek target) hold the last
      # frame instead of draining the decoder's ~2s pipeline.
      if not playing and flush_target < 0:
        time.sleep(0.02)
        continue

      if playing:
        # Honor an absolute per-frame deadline. Clamping a stale deadline means
        # a pause never builds a backlog that would fast-forward on resume.
        now = time.monotonic()
        next_frame_time = max(next_frame_time, now)
        delay = next_frame_time - now
        if delay > 0:
          time.sleep(delay)

      ready, _, _ = select.select([proc.stdout], [], [], 0.05)
      if not ready:
        continue
      hdr = self._read_exact(proc.stdout, 12)
      if hdr is None:
        _dbg("reader EOF after", frames_read)
        break
      fw, fh_, ln = struct.unpack("<III", hdr)
      if fw == 0:
        with self._lock:
          self._error = "hardware decoder error"
        _dbg("decoder error")
        break
      data = self._read_exact(proc.stdout, ln)
      if data is None:
        _dbg("payload EOF after", frames_read)
        break
      arr = np.frombuffer(data, dtype=np.uint8)
      with self._lock:
        self._frame = arr
        self._frame_w = fw
        self._frame_h = fh_
        self._seq += 1
        self._fidx = start_idx + frames_read
      frames_read += 1
      if frames_read % 60 == 0:
        _dbg("frames_read", frames_read)

      if playing:
        # Advance the deadline by exactly one frame period.
        next_frame_time += 1.0 / PLAYER_FPS
      else:
        # Paused seek: once the requested frame is on screen, hold it.
        with self._lock:
          if self._flush_target >= 0 and self._fidx >= self._flush_target:
            self._flush_target = -1

    session_stop.set()
    _dbg("session end", start_idx, "read", frames_read, "writer_alive", writer.is_alive())

    # Drain stdout first so the decoder can't block on a full output pipe, then
    # send an explicit abort so it tears down (STREAMOFF + free ION) and exits.
    def _drain():
      try:
        while proc.stdout is not None and proc.stdout.read(65536):
          pass
      except (OSError, ValueError):
        pass

    drain_thread = threading.Thread(target=_drain, daemon=True)
    drain_thread.start()

    try:
      os.write(stdin_fd, struct.pack("<I", 0))
    except OSError:
      pass
    try:
      if proc.stdin is not None:
        proc.stdin.close()
    except OSError:
      pass

    try:
      proc.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
      proc.kill()
      proc.wait()
    drain_thread.join(timeout=1.0)
    writer.join(timeout=1.0)

  def run(self) -> None:
    try:
      import av

      container = av.open(self._path, format="hevc")
      stream = container.streams.video[0]
      self._img_w, self._img_h = stream.width, stream.height

      # Demux once to record each access unit's byte range and keyframe flag.
      # (A raw slice-only split would drop the in-band parameter sets that
      # precede IDR frames, which the decoder needs after a port reconfig.)
      packets: list = []
      offset = 0
      for pkt in container.demux(stream):
        if pkt.size == 0:
          continue
        pos = pkt.pos if pkt.pos is not None and pkt.pos >= 0 else offset
        packets.append((bool(pkt.is_keyframe), int(pos), int(pkt.size)))
        offset = pos + pkt.size
      container.close()

      if len(packets) < 2:
        raise RuntimeError("no decodable frames")

      self._files = packets
      with self._lock:
        self._total = len(packets)
      self._prefix = self._extract_param_sets(self._path)
    except Exception as e:
      with self._lock:
        self._error = str(e) or type(e).__name__
        self._eof = True
      return

    start = 0
    while not self._stop_ev.is_set():
      seek = self._take_seek()
      if seek is not None:
        start = self._snap_to_gop(seek)
        # A seek while paused should still show its target frame: let the reader
        # consume frames up to the target, then hold.
        with self._lock:
          self._flush_target = seek if not self._play_ev.is_set() else -1
      elif not self._play_ev.is_set():
        # Paused with nothing to do; wait for play or a new seek.
        self._stop_ev.wait(0.05)
        continue
      self._session(start)
      with self._lock:
        self._flush_target = -1
      if self._stop_ev.is_set():
        break
      with self._lock:
        another_seek = self._seek_to is not None
      if another_seek:
        continue
      if not self._play_ev.is_set():
        continue
      with self._lock:
        self._eof = True
      while not self._stop_ev.is_set():
        with self._lock:
          if self._seek_to is not None:
            break
        if not self._play_ev.is_set():
          break
        time.sleep(0.05)
      with self._lock:
        self._eof = False

  @staticmethod
  def _extract_param_sets(path: str) -> bytes:
    """Return the leading VPS/SPS/PPS bytes (everything before the first VCL NAL)."""
    try:
      with open(path, "rb") as fh:
        raw = fh.read(1 << 20)
    except OSError:
      return b""
    i, n = 0, len(raw)
    while i < n - 4:
      if raw[i] == 0 and raw[i + 1] == 0 and (raw[i + 2] == 1 or (raw[i + 2] == 0 and raw[i + 3] == 1)):
        sc = 4 if raw[i + 2] == 0 else 3
        if i + sc < n and ((raw[i + sc] >> 1) & 0x3F) <= 31:
          return raw[:i]
        i += sc
      else:
        i += 1
    return b""

  def _snap_to_gop(self, frame_idx: int) -> int:
    keyframes = [i for i, (is_key, _, _) in enumerate(self._files) if is_key]
    if not keyframes:
      return 0
    pos = bisect.bisect_right(keyframes, frame_idx) - 1
    return keyframes[max(pos, 0)]


def _create_decoder(path: str, out_w: int, out_h: int):
  if path.endswith(".hevc") and os.path.isfile(HWDEC_PATH) and os.access(HWDEC_PATH, os.X_OK):
    return _HardwareDecoder(path, out_w, out_h)
  return _SoftwareDecoder(path, out_w, out_h)


class _IconButton(Button):
  """Button with a centered icon and no text."""

  def __init__(self, icon_path: str, callback, icon_size: int = 84, button_style: ButtonStyle = ButtonStyle.NORMAL):
    super().__init__("", callback, font_size=44, button_style=button_style)
    self._icon_path = icon_path
    self._icon_size = icon_size
    self._icon_tex: rl.Texture | None = None

  def set_icon(self, icon_path: str) -> None:
    if icon_path != self._icon_path:
      self._icon_path = icon_path
      self._icon_tex = None

  def _render(self, rect: rl.Rectangle) -> None:
    super()._render(rect)
    if self._icon_tex is None:
      self._icon_tex = gui_app.texture(self._icon_path, self._icon_size, self._icon_size, keep_aspect_ratio=True)
    if self.is_pressed:
      color = rl.Color(200, 200, 200, 255)
    elif not self.enabled:
      color = rl.Color(255, 255, 255, 90)
    else:
      color = rl.WHITE
    x = rect.x + (rect.width - self._icon_tex.width) / 2
    y = rect.y + (rect.height - self._icon_tex.height) / 2
    rl.draw_texture_ex(self._icon_tex, rl.Vector2(x, y), 0.0, 1.0, color)


class DashCamPlayer(NavWidget):
  """Full screen, offroad-only player with play/pause, clip skip and 10s seek."""

  def __init__(self, clips: list[Clip], camera_file: str, index: int):
    super().__init__()
    self._clips = clips
    self._camera_file = camera_file
    self._index = index
    self._decoder: _SoftwareDecoder | _HardwareDecoder | None = None
    self._tex_y: rl.Texture | None = None
    self._tex_uv: rl.Texture | None = None
    self._shader: rl.Shader | None = None
    self._tex1_loc = 0
    self._frame_w = 0
    self._frame_h = 0
    self._last_seq = -1
    self._playing = True
    self._error: str | None = None

    self._font = gui_app.font(FontWeight.MEDIUM)
    self._small_font = gui_app.font(FontWeight.NORMAL)

    self._btn_prev = self._child(_IconButton("icons/previous.png", self._prev_clip))
    self._btn_back10 = self._child(_IconButton("icons/seek-back-10.png", lambda: self._seek_rel(-10)))
    self._btn_play = self._child(_IconButton("icons/pause.png", self._toggle_play, button_style=ButtonStyle.PRIMARY))
    self._btn_fwd10 = self._child(_IconButton("icons/seek-forward-10.png", lambda: self._seek_rel(10)))
    self._btn_next = self._child(_IconButton("icons/next.png", self._next_clip))
    self._btn_close = self._child(_IconButton("icons/close2.png", lambda: self.dismiss(), button_style=ButtonStyle.DANGER))
    self._btn_export = self._child(Button(tr("Export"), self._export_current, font_size=40, button_style=ButtonStyle.PRIMARY))

    global _TIMEOUT_CB_REGISTERED
    if not _TIMEOUT_CB_REGISTERED:
      device.add_interactive_timeout_callback(_dismiss_active_player)
      _TIMEOUT_CB_REGISTERED = True

  # ---- lifecycle ----
  def show_event(self) -> None:
    super().show_event()
    global _ACTIVE_PLAYER
    _ACTIVE_PLAYER = self
    if self._shader is None:
      self._shader = rl.load_shader_from_memory(YUV_VERTEX_SHADER, YUV_FRAGMENT_SHADER)
      self._tex1_loc = rl.get_shader_location(self._shader, "texture1")
    self._load_clip(self._index)

  def hide_event(self) -> None:
    super().hide_event()
    global _ACTIVE_PLAYER
    if _ACTIVE_PLAYER is self:
      _ACTIVE_PLAYER = None
    self._stop_decoder()
    self._unload_textures()

  def graceful_close(self) -> None:
    """Pause and animate the player away (used by the interactive timeout)."""
    self._playing = False
    if self._decoder is not None:
      self._decoder.set_playing(False)
    self._btn_play.set_icon("icons/play.png")
    self.dismiss()

  def _unload_textures(self) -> None:
    if self._tex_y is not None:
      rl.unload_texture(self._tex_y)
      self._tex_y = None
    if self._tex_uv is not None:
      rl.unload_texture(self._tex_uv)
      self._tex_uv = None
    self._frame_w = 0
    self._frame_h = 0

  def _ensure_textures(self, fw: int, fh: int) -> None:
    if self._tex_y is not None and self._frame_w == fw and self._frame_h == fh:
      return
    self._unload_textures()
    img_y = rl.Image(None, fw, fh, 1, rl.PixelFormat.PIXELFORMAT_UNCOMPRESSED_GRAYSCALE)
    self._tex_y = rl.load_texture_from_image(img_y)
    rl.set_texture_filter(self._tex_y, rl.TextureFilter.TEXTURE_FILTER_BILINEAR)
    img_uv = rl.Image(None, fw // 2, fh // 2, 1, rl.PixelFormat.PIXELFORMAT_UNCOMPRESSED_GRAY_ALPHA)
    self._tex_uv = rl.load_texture_from_image(img_uv)
    rl.set_texture_filter(self._tex_uv, rl.TextureFilter.TEXTURE_FILTER_BILINEAR)
    self._frame_w = fw
    self._frame_h = fh

  # ---- playback controls ----
  def _load_clip(self, index: int) -> None:
    self._stop_decoder()
    if not self._clips:
      self._error = tr("No recordings found")
      return
    self._index = max(0, min(index, len(self._clips) - 1))
    self._error = None
    self._last_seq = -1
    self._playing = True
    self._btn_play.set_icon("icons/pause.png")
    clip = self._clips[self._index]
    self._decoder = _create_decoder(clip.path, DISPLAY_W, DISPLAY_H)
    self._decoder.start()

  def _stop_decoder(self) -> None:
    if self._decoder is not None:
      self._decoder.stop()
      self._decoder.join(timeout=2.0)
      self._decoder = None

  def _toggle_play(self) -> None:
    self._playing = not self._playing
    if self._decoder is not None:
      self._decoder.set_playing(self._playing)
    self._btn_play.set_icon("icons/pause.png" if self._playing else "icons/play.png")

  def _seek_rel(self, seconds: float) -> None:
    if self._decoder is None:
      return
    _, _, _, _, fidx, _, _, _ = self._decoder.snapshot()
    self._decoder.request_seek(max(0, fidx + int(seconds * PLAYER_FPS)))

  def _prev_clip(self) -> None:
    if self._index > 0:
      self._load_clip(self._index - 1)

  def _next_clip(self) -> None:
    if self._index < len(self._clips) - 1:
      self._load_clip(self._index + 1)

  def _export_current(self) -> None:
    if export_active() or not self._clips:
      return
    mnt = export_mountpoint()
    if not mnt:
      return
    clip = self._clips[self._index]
    start_export([(os.path.dirname(clip.path), clip.mtime)], mnt)
    gui_app.push_widget(ExportProgressDialog())

  def _update_state(self) -> None:
    super()._update_state()
    # Never allow playback while driving
    if ui_state.started:
      self._playing = False
      self.dismiss()
      return
    # Keep the screen awake (and the settings open) while a video is actively
    # playing. Once paused, let the normal interactive timeout close the player.
    if self._playing:
      eof = self._decoder is not None and self._decoder.snapshot()[6]
      if not eof:
        device._reset_interactive_timeout()

  # ---- rendering ----
  def _render(self, rect: rl.Rectangle) -> None:
    cur_fidx = 0
    total = 0
    if self._decoder is not None:
      frame, fw, fh, seq, cur_fidx, total, _eof, err = self._decoder.snapshot()
      if err:
        self._error = err
      if frame is not None and seq != self._last_seq and fw > 0 and fh > 0:
        self._ensure_textures(fw, fh)
        y_plane = frame[:fw * fh]
        uv_plane = frame[fw * fh:]
        if self._tex_y is not None:
          rl.update_texture(self._tex_y, rl.ffi.cast("void *", y_plane.ctypes.data))
        if self._tex_uv is not None:
          rl.update_texture(self._tex_uv, rl.ffi.cast("void *", uv_plane.ctypes.data))
        self._last_seq = seq

    video_rect = rl.Rectangle(rect.x + 40, rect.y + 90, rect.width - 80, rect.height - 300)

    if self._tex_y is not None and self._frame_w > 0 and self._frame_h > 0 and self._shader is not None:
      scale = min(video_rect.width / self._frame_w, video_rect.height / self._frame_h)
      dw, dh = self._frame_w * scale, self._frame_h * scale
      dst = rl.Rectangle(video_rect.x + (video_rect.width - dw) / 2,
                         video_rect.y + (video_rect.height - dh) / 2, dw, dh)
      rl.begin_shader_mode(self._shader)
      rl.set_shader_value_texture(self._shader, self._tex1_loc, self._tex_uv)
      rl.draw_texture_pro(self._tex_y, rl.Rectangle(0, 0, self._frame_w, self._frame_h),
                          dst, rl.Vector2(0, 0), 0.0, rl.WHITE)
      rl.end_shader_mode()

    if self._clips:
      clip = self._clips[self._index]
      title = f"{clip.date_text}   {clip.time_text}   -   segment {clip.segment}"
      rl.draw_text_ex(self._font, title, rl.Vector2(rect.x + 40, rect.y + 30), 40, 0, rl.WHITE)
      export_w = 220
      export_rect = rl.Rectangle(rect.x + rect.width - 40 - export_w, rect.y + 14, export_w, 80)
      self._btn_export.set_enabled(export_mountpoint() is not None and not export_active())
      self._btn_export.render(export_rect)
      if total > 0:
        label = f"{_fmt_time(cur_fidx / PLAYER_FPS)} / {_fmt_time(total / PLAYER_FPS)}"
        size = measure_text_cached(self._font, label, 40)
        rl.draw_text_ex(self._font, label, rl.Vector2(export_rect.x - 30 - size.x, rect.y + 30), 40, 0, rl.WHITE)

    if self._error:
      rl.draw_text_ex(self._font, self._error, rl.Vector2(video_rect.x + 20, video_rect.y + 20), 36, 0, rl.RED)

    controls = [self._btn_prev, self._btn_back10, self._btn_play, self._btn_fwd10, self._btn_next, self._btn_close]
    n = len(controls)
    gap = 20
    btn_h = 110
    btn_w = (rect.width - 160 - gap * (n - 1)) / n
    x = rect.x + 80
    y = rect.y + rect.height - 150
    for btn in controls:
      btn.render(rl.Rectangle(x, y, btn_w, btn_h))
      x += btn_w + gap


def _draw_text_button(rect: rl.Rectangle, text: str, font, enabled: bool = True, color=None) -> None:
  base = color if color is not None else rl.Color(32, 60, 96, 255)
  bg = base if enabled else rl.Color(52, 52, 52, 255)
  rl.draw_rectangle_rounded(rect, 0.15, 8, bg)
  size = measure_text_cached(font, text, 36)
  pos = rl.Vector2(rect.x + (rect.width - size.x) / 2, rect.y + (rect.height - size.y) / 2)
  rl.draw_text_ex(font, text, pos, 36, 0, rl.WHITE if enabled else rl.Color(150, 150, 150, 255))


class _ClipRow(Widget):
  """A single clip: tap the checkbox to select it, tap the row to play, Delete removes it."""

  HEIGHT = 150
  CHECKBOX_ZONE = 150
  DELETE_W = 210

  def __init__(self, clip: Clip, selected: bool, on_open, on_toggle, on_delete):
    super().__init__()
    self._clip = clip
    self._selected = selected
    self._on_open = on_open
    self._on_toggle = on_toggle
    self._on_delete = on_delete
    self._rect = rl.Rectangle(0, 0, 0, self.HEIGHT)
    self._font = gui_app.font(FontWeight.MEDIUM)
    self._small = gui_app.font(FontWeight.NORMAL)

  def set_parent_rect(self, parent_rect: rl.Rectangle) -> None:
    super().set_parent_rect(parent_rect)
    self._rect.width = parent_rect.width

  def _delete_rect(self, rect: rl.Rectangle) -> rl.Rectangle:
    return rl.Rectangle(rect.x + rect.width - self.DELETE_W - 40, rect.y + (rect.height - 92) / 2, self.DELETE_W, 92)

  def _handle_mouse_release(self, mouse_pos) -> None:
    if rl.check_collision_point_rec(mouse_pos, self._delete_rect(self._rect)):
      self._on_delete(self._clip)
    elif mouse_pos.x - self._rect.x < self.CHECKBOX_ZONE:
      self._selected = not self._selected
      self._on_toggle(self._clip, self._selected)
    else:
      self._on_open(self._clip)

  def _render(self, rect: rl.Rectangle) -> None:
    bg = ROW_BG_PRESSED if self.is_pressed else ROW_BG
    rl.draw_rectangle_rounded(rect, 0.15, 8, bg)

    box = rl.Rectangle(rect.x + 45, rect.y + (rect.height - 58) / 2, 58, 58)
    rl.draw_rectangle_rounded(box, 0.25, 6, rl.Color(25, 25, 25, 255))
    rl.draw_rectangle_rounded_lines_ex(box, 0.25, 6, 3, rl.Color(200, 200, 200, 255))
    if self._selected:
      inner = rl.Rectangle(box.x + 9, box.y + 9, box.width - 18, box.height - 18)
      rl.draw_rectangle_rounded(inner, 0.3, 6, rl.Color(80, 160, 255, 255))

    text_x = rect.x + self.CHECKBOX_ZONE + 20
    rl.draw_text_ex(self._font, self._clip.time_text, rl.Vector2(text_x, rect.y + 28), 46, 0, rl.WHITE)
    rl.draw_text_ex(self._small, tr("segment") + f" {self._clip.segment}", rl.Vector2(text_x, rect.y + 88), 34, 0, SUBTEXT_COLOR)
    _draw_text_button(self._delete_rect(rect), tr("Delete"), self._small, color=rl.Color(150, 45, 45, 255))


class _DateRow(Widget):
  """A day's folder: tap to open its clips, or use Export day / Delete day."""

  HEIGHT = 160
  EXPORT_W = 210
  DELETE_W = 210
  GAP = 16

  def __init__(self, date: str, count: int, can_export: bool, on_open, on_export, on_delete):
    super().__init__()
    self._date = date
    self._count = count
    self._can_export = can_export
    self._on_open = on_open
    self._on_export = on_export
    self._on_delete = on_delete
    self._rect = rl.Rectangle(0, 0, 0, self.HEIGHT)
    self._font = gui_app.font(FontWeight.MEDIUM)
    self._small = gui_app.font(FontWeight.NORMAL)

  def set_parent_rect(self, parent_rect: rl.Rectangle) -> None:
    super().set_parent_rect(parent_rect)
    self._rect.width = parent_rect.width

  def _delete_rect(self, rect: rl.Rectangle) -> rl.Rectangle:
    return rl.Rectangle(rect.x + rect.width - self.DELETE_W - 40, rect.y + (rect.height - 92) / 2, self.DELETE_W, 92)

  def _export_rect(self, rect: rl.Rectangle) -> rl.Rectangle:
    x = rect.x + rect.width - self.DELETE_W - self.GAP - self.EXPORT_W - 40
    return rl.Rectangle(x, rect.y + (rect.height - 92) / 2, self.EXPORT_W, 92)

  def _handle_mouse_release(self, mouse_pos) -> None:
    if rl.check_collision_point_rec(mouse_pos, self._delete_rect(self._rect)):
      self._on_delete(self._date)
    elif rl.check_collision_point_rec(mouse_pos, self._export_rect(self._rect)):
      if self._can_export:
        self._on_export()
      # A disabled Export button must not fall through to opening the folder.
    else:
      self._on_open()

  def _render(self, rect: rl.Rectangle) -> None:
    bg = ROW_BG_PRESSED if self.is_pressed else ROW_BG
    rl.draw_rectangle_rounded(rect, 0.15, 8, bg)
    rl.draw_text_ex(self._font, self._date, rl.Vector2(rect.x + 40, rect.y + 30), 52, 0, rl.WHITE)
    rl.draw_text_ex(self._small, f"{self._count} " + tr("clip(s)"), rl.Vector2(rect.x + 40, rect.y + 96), 34, 0, SUBTEXT_COLOR)
    _draw_text_button(self._export_rect(rect), tr("Export day"), self._small, self._can_export)
    _draw_text_button(self._delete_rect(rect), tr("Delete day"), self._small, color=rl.Color(150, 45, 45, 255))


class ExportProgressDialog(NavWidget):
  """Modal progress dialog shown while an export runs."""

  def __init__(self):
    super().__init__()
    self._font = gui_app.font(FontWeight.MEDIUM)
    self._small = gui_app.font(FontWeight.NORMAL)
    self._btn_cancel = self._child(Button(tr("Cancel"), request_export_cancel, font_size=44, button_style=ButtonStyle.DANGER))
    self._btn_close = self._child(Button(tr("Close"), lambda: self.dismiss(), font_size=44, button_style=ButtonStyle.PRIMARY))

    global _TIMEOUT_CB_REGISTERED
    if not _TIMEOUT_CB_REGISTERED:
      device.add_interactive_timeout_callback(_dismiss_active_player)
      _TIMEOUT_CB_REGISTERED = True

  def show_event(self) -> None:
    super().show_event()
    global _ACTIVE_DIALOG
    _ACTIVE_DIALOG = self

  def hide_event(self) -> None:
    super().hide_event()
    global _ACTIVE_DIALOG
    if _ACTIVE_DIALOG is self:
      _ACTIVE_DIALOG = None

  def _back_enabled(self) -> bool:
    return False

  def _render(self, rect: rl.Rectangle) -> None:
    st = export_status()
    rl.draw_rectangle_rec(rect, rl.Color(21, 21, 21, 255))
    total = max(st["total"], 1)
    frac = min((st["done"] + st["fraction"]) / total, 1.0)

    if st["active"]:
      title = tr("Exporting to USB drive")
    elif st["failed"]:
      title = tr("Export finished with errors")
    else:
      title = tr("Export complete")
    rl.draw_text_ex(self._font, title, rl.Vector2(rect.x + 60, rect.y + 60), 56, 0, rl.WHITE)

    bar_x, bar_w, bar_y, bar_h = rect.x + 60, rect.width - 120, rect.y + 210, 46
    rl.draw_rectangle_rounded(rl.Rectangle(bar_x, bar_y, bar_w, bar_h), 0.5, 8, rl.Color(60, 60, 60, 255))
    if frac > 0:
      rl.draw_rectangle_rounded(rl.Rectangle(bar_x, bar_y, bar_w * frac, bar_h), 0.5, 8, rl.Color(80, 160, 255, 255))

    pct = f"{int(frac * 100)}%"
    rl.draw_text_ex(self._font, pct, rl.Vector2(bar_x, bar_y + bar_h + 24), 44, 0, rl.WHITE)
    counts = f"{st['done']}/{st['total']} " + tr("files")
    csize = measure_text_cached(self._font, counts, 44)
    rl.draw_text_ex(self._font, counts, rl.Vector2(bar_x + bar_w - csize.x, bar_y + bar_h + 24), 44, 0, rl.WHITE)

    if st["active"] and st["current"]:
      rl.draw_text_ex(self._small, st["current"], rl.Vector2(bar_x, bar_y + bar_h + 92), 36, 0, SUBTEXT_COLOR)
    if st["result"]:
      rl.draw_text_ex(self._small, st["result"], rl.Vector2(bar_x, bar_y + bar_h + 92), 36, 0, SUBTEXT_COLOR)

    y = rect.y + rect.height - 160
    if st["active"]:
      self._btn_cancel.render(rl.Rectangle(rect.x + rect.width - 60 - 300, y, 300, 110))
    else:
      self._btn_close.render(rl.Rectangle(rect.x + rect.width - 60 - 300, y, 300, 110))


class DashCamLayout(Widget):
  """Offroad-only settings panel: browse dates, play clips and export to USB."""

  def __init__(self):
    super().__init__()
    self._camera_idx = 0
    self._clips: list[Clip] = []
    self._selected_date: str | None = None
    self._selected: set[str] = set()
    self._has_rows = False
    self._scroller = Scroller([], spacing=12, line_separator=False, pad_end=True)
    self._loaded = False
    self._font = gui_app.font(FontWeight.MEDIUM)
    self._small = gui_app.font(FontWeight.NORMAL)

    self._picker: list[Button] = []
    for i, (label, _) in enumerate(CAMERAS):
      self._picker.append(self._child(Button(label, lambda idx=i: self._set_camera(idx), font_size=40)))
    self._btn_back = self._child(Button(tr("< Dates"), self._back_to_dates, font_size=40))
    self._btn_select_all = self._child(Button(tr("Select all"), self._select_all, font_size=40))
    self._btn_export = self._child(Button(tr("Export"), self._export_selected, font_size=40, button_style=ButtonStyle.PRIMARY))

  # ---- navigation ----
  def _set_camera(self, idx: int) -> None:
    if idx == self._camera_idx and self._loaded:
      return
    self._camera_idx = idx
    self._selected.clear()
    # The picker only exists inside a date, so stay in the current date.
    self._reload()

  def _back_to_dates(self) -> None:
    self._selected_date = None
    self._selected.clear()
    self._reload()

  def _open_date(self, date: str) -> None:
    self._selected_date = date
    self._selected.clear()
    # Default to a camera that actually has clips that day.
    if not any(time.strftime("%Y-%m-%d", time.localtime(c.mtime)) == date for c in self._clips):
      for idx, (_, camera_file) in enumerate(CAMERAS):
        if idx == self._camera_idx:
          continue
        if any(time.strftime("%Y-%m-%d", time.localtime(c.mtime)) == date for c in list_clips(camera_file)):
          self._camera_idx = idx
          break
    self._reload()

  def _toggle(self, clip: Clip, selected: bool) -> None:
    if selected:
      self._selected.add(clip.name)
    else:
      self._selected.discard(clip.name)

  def _clips_on_date(self, date: str) -> list[Clip]:
    return [c for c in self._clips if time.strftime("%Y-%m-%d", time.localtime(c.mtime)) == date]

  def _visible_clips(self) -> list[Clip]:
    return self._clips_on_date(self._selected_date) if self._selected_date else list(self._clips)

  def _select_all(self) -> None:
    names = {c.name for c in self._visible_clips()}
    if names and names.issubset(self._selected):
      self._selected -= names
    else:
      self._selected |= names
    self._reload()

  # ---- export ----
  def _start_export_segments(self, seg_dirs: list) -> None:
    if export_active() or not seg_dirs:
      return
    mnt = export_mountpoint()
    if not mnt:
      return
    segs = {d: segment_mtime(d) for d in seg_dirs}
    if start_export(list(segs.items()), mnt):
      gui_app.push_widget(ExportProgressDialog())

  def _export_selected(self) -> None:
    dirs = sorted({os.path.dirname(c.path) for c in self._clips if c.name in self._selected})
    self._start_export_segments(dirs)

  def _export_date(self, date: str) -> None:
    self._start_export_segments(segments_on_date(date))

  # ---- delete ----
  def _confirm_delete(self, text: str, action) -> None:
    def cb(result: int):
      if result == DialogResult.CONFIRM:
        action()
    gui_app.push_widget(ConfirmDialog(text, tr("Delete"), callback=cb))

  def _delete_clip(self, clip: Clip) -> None:
    msg = tr("Delete this recording?") + f"  {clip.time_text}  -  " + tr("segment") + f" {clip.segment}"

    def do_delete():
      delete_segments([os.path.dirname(clip.path)])
      self._selected.discard(clip.name)
      self._reload()
      if self._selected_date and not self._clips_on_date(self._selected_date):
        self._selected_date = None
        self._reload()

    self._confirm_delete(msg, do_delete)

  def _delete_date(self, date: str) -> None:
    segs = segments_on_date(date)
    msg = tr("Delete all recordings from") + f" {date} ({len(segs)})?"

    def do_delete():
      delete_segments(segs)
      if self._selected_date == date:
        self._selected_date = None
      self._reload()

    self._confirm_delete(msg, do_delete)

  # ---- list ----
  def _reload(self) -> None:
    self._clips = list_clips(CAMERAS[self._camera_idx][1])
    self._selected &= {c.name for c in self._clips}
    can_export = export_mountpoint() is not None and not export_active()

    rows: list = []
    if self._selected_date is None:
      # Root: only dates, counted across every camera.
      counts = list_date_counts()
      for d in sorted(counts.keys(), reverse=True):
        rows.append(_DateRow(d, counts[d], can_export, lambda d=d: self._open_date(d),
                             lambda d=d: self._export_date(d), self._delete_date))
    else:
      for c in self._clips_on_date(self._selected_date):
        rows.append(_ClipRow(c, c.name in self._selected, self._open_clip, self._toggle, self._delete_clip))

    self._scroller = Scroller(rows, spacing=12, line_separator=False, pad_end=True)
    self._scroller.show_event()
    self._has_rows = bool(rows)
    self._loaded = True

  def _open_clip(self, clip: Clip) -> None:
    view = self._visible_clips()
    try:
      idx = view.index(clip)
    except ValueError:
      idx = 0
    gui_app.push_widget(DashCamPlayer(view, CAMERAS[self._camera_idx][1], idx))

  def show_event(self) -> None:
    super().show_event()
    # Always reopen on the date list, not the folder last viewed.
    self._selected_date = None
    self._selected.clear()
    self._loaded = False

  def _draw_message(self, rect: rl.Rectangle, text: str) -> None:
    size = measure_text_cached(self._font, text, 44)
    pos = rl.Vector2(rect.x + (rect.width - size.x) / 2, rect.y + (rect.height - size.y) / 2)
    rl.draw_text_ex(self._font, text, pos, 44, 0, rl.Color(200, 200, 200, 255))

  def _render(self, rect: rl.Rectangle) -> None:
    if not ui_state.is_offroad():
      self._draw_message(rect, tr("Dash cam playback is only available while parked"))
      return

    if not self._loaded:
      self._reload()

    can_export = export_mountpoint() is not None and not export_active()
    date_label = self._selected_date if isinstance(self._selected_date, str) else ""

    if not can_export:
      warn = tr("No USB drive mounted")
      wsize = measure_text_cached(self._small, warn, 34)
      rl.draw_text_ex(self._small, warn, rl.Vector2(rect.x + rect.width - wsize.x, rect.y), 34, 0, rl.Color(255, 180, 120, 255))

    if not date_label:
      # Root: dates only - no camera picker until a date is opened.
      if can_export:
        rl.draw_text_ex(self._small, tr("Select a date"), rl.Vector2(rect.x, rect.y), 34, 0, SUBTEXT_COLOR)
      list_y = rect.y + 50
    else:
      py = rect.y + 50
      pw, ph, gap = 220, 90, 16
      x = rect.x
      for i, btn in enumerate(self._picker):
        btn.set_button_style(ButtonStyle.PRIMARY if i == self._camera_idx else ButtonStyle.NORMAL)
        btn.render(rl.Rectangle(x, py, pw, ph))
        x += pw + gap

      ctrl_y = py + ph + 18
      self._btn_back.render(rl.Rectangle(rect.x, ctrl_y, 200, 84))
      rl.draw_text_ex(self._font, date_label, rl.Vector2(rect.x + 220, ctrl_y + 12), 48, 0, rl.WHITE)
      self._btn_select_all.render(rl.Rectangle(rect.x + rect.width - 580, ctrl_y, 240, 84))
      self._btn_export.set_enabled(can_export and bool(self._selected) and not export_active())
      self._btn_export.render(rl.Rectangle(rect.x + rect.width - 320, ctrl_y, 320, 84))
      list_y = ctrl_y + 84 + 22

    list_rect = rl.Rectangle(rect.x, list_y, rect.width, rect.height - (list_y - rect.y))
    if self._has_rows:
      self._scroller.render(list_rect)
    else:
      self._draw_message(list_rect, tr("No recordings found"))

