"""
Shared dash cam filesystem logic.

Pure logic only: no GUI/raylib imports, so it can be reused by both the
on-device Dash Cam settings panel and the web dash cam server.

Recordings live in Paths.log_root() (/data/media/0/realdata) as one directory
per 60 second segment (<route>--<segment>). Each segment contains raw HEVC
elementary streams (fcamera.hevc / ecamera.hevc / dcamera.hevc), which have no
container timestamps and must be remuxed before a browser will play them.
"""
from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass

from openpilot.system.hardware.hw import Paths

FFMPEG = "/usr/local/venv/bin/ffmpeg"
SEGMENT_SECONDS = 60.0
PLAYER_FPS = 20.0  # openpilot records 1200 frames per 60s segment

# (display name, file name inside each segment)
CAMERAS = [
  ("Front", "fcamera.hevc"),
  ("Wide", "ecamera.hevc"),
  ("Driver", "dcamera.hevc"),
]
CAMERA_FILES = [file for _, file in CAMERAS]
# camera file <-> short slug used by the export layout and the web UI
CAMERA_FOLDER = {"fcamera.hevc": "front", "ecamera.hevc": "wide", "dcamera.hevc": "driver"}
FOLDER_CAMERA = {folder: file for file, folder in CAMERA_FOLDER.items()}


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


def date_of(mtime: float) -> str:
  return time.strftime("%Y-%m-%d", time.localtime(mtime))


def _has_video_data(path: str) -> bool:
  """A camera file is usable only if it exists and actually contains data."""
  try:
    return os.path.isfile(path) and os.path.getsize(path) > 0
  except OSError:
    return False


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


def segment_cameras(seg_dir: str) -> list:
  return [(CAMERA_FOLDER[file], os.path.join(seg_dir, file)) for _, file in CAMERAS
          if _has_video_data(os.path.join(seg_dir, file))]


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


def list_segments() -> list:
  """Every finished segment directory as (name, path, mtime)."""
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
    if mt:
      out.append((name, seg_dir, mt))
  return out


def segments_on_date(date: str) -> list:
  """Every finished segment directory (any camera) recorded on `date`."""
  return [seg_dir for _name, seg_dir, mt in list_segments() if date_of(mt) == date]


def list_date_counts() -> dict:
  """Number of finished segments recorded on each date, across all cameras."""
  counts: dict = {}
  for _name, _seg_dir, mt in list_segments():
    date = date_of(mt)
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


def segment_dir(name: str) -> str | None:
  """Resolve a segment directory name to a path that is a direct child of the log root.

  Returns None for anything that is not a plain <route>--<segment> directory name,
  so user input can never escape the log root.
  """
  if not name or "--" not in name or "/" in name or "\\" in name or name in (".", ".."):
    return None
  root = os.path.realpath(Paths.log_root())
  path = os.path.realpath(os.path.join(root, name))
  if os.path.dirname(path) != root or not os.path.isdir(path):
    return None
  return path


def camera_path(seg_name: str, camera: str) -> str | None:
  """Resolve <segment>/<camera-slug> to a real, non-empty file inside the log root."""
  seg = segment_dir(seg_name)
  if seg is None:
    return None
  file = FOLDER_CAMERA.get(camera)
  if file is None:
    return None
  path = os.path.join(seg, file)
  return path if _has_video_data(path) else None


def segment_info(name: str, seg_dir: str, mtime: float) -> dict:
  """Metadata for one segment, including which cameras exist and their sizes."""
  cameras = []
  for _label, file in CAMERAS:
    path = os.path.join(seg_dir, file)
    if _has_video_data(path):
      try:
        size = os.path.getsize(path)
      except OSError:
        size = 0
      cameras.append({"slug": CAMERA_FOLDER[file], "file": file, "size": size})
  return {
    "seg": name,
    "mtime": mtime,
    "time": time.strftime("%I:%M:%S %p", time.localtime(mtime)),
    "date": date_of(mtime),
    "cameras": cameras,
  }


def mp4_stream_command(src: str) -> list[str]:
  """ffmpeg argv that remuxes `src` to a fragmented MP4 on stdout (no temp file).

  Fragmented output is required: the result is streamed straight to the client,
  so it must be playable before the muxer can seek back and finalise a header.
  """
  cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostats"]
  if not src.endswith(".ts"):
    # Raw HEVC carries no container timestamps; generate them at the record rate.
    cmd += ["-fflags", "+genpts", "-r", str(int(PLAYER_FPS))]
  cmd += ["-i", src, "-c", "copy",
          "-movflags", "frag_keyframe+empty_moov+default_base_moof",
          "-f", "mp4", "pipe:1"]
  return cmd
