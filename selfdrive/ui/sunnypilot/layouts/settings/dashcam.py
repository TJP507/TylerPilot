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
index (frame -> byte offset) and decoding is done one GOP at a time. Playback is
software decoded, so it runs slower than real time; that is acceptable for review.

Playback is only permitted while the device is offroad.
"""
import bisect
import io
import os
import threading
import time
from dataclasses import dataclass

import numpy as np
import pyray as rl

from openpilot.system.hardware.hw import Paths
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.widgets.button import Button, ButtonStyle
from openpilot.system.ui.widgets.nav_widget import NavWidget
from openpilot.system.ui.widgets.scroller_tici import Scroller

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


class _Decoder(threading.Thread):
  """Background software decoder. Produces RGBA frames at a fixed size."""

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
      return self._frame, self._seq, self._fidx, self._total, self._eof, self._error

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
    img = frame.reformat(width=self._w, height=self._h, format="rgba")
    arr = np.ascontiguousarray(img.to_ndarray())
    with self._lock:
      self._frame = arr
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
    self._decoder: _Decoder | None = None
    self._texture: rl.Texture | None = None
    self._last_seq = -1
    self._tex_w = DISPLAY_W
    self._tex_h = DISPLAY_H
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

  # ---- lifecycle ----
  def show_event(self) -> None:
    super().show_event()
    if self._texture is None:
      img = rl.gen_image_color(self._tex_w, self._tex_h, rl.BLACK)
      self._texture = rl.load_texture_from_image(img)
      rl.set_texture_filter(self._texture, rl.TextureFilter.TEXTURE_FILTER_BILINEAR)
      rl.unload_image(img)
    self._load_clip(self._index)

  def hide_event(self) -> None:
    super().hide_event()
    self._stop_decoder()

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
    self._decoder = _Decoder(clip.path, self._tex_w, self._tex_h)
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
    _, _, fidx, _, _, _ = self._decoder.snapshot()
    self._decoder.request_seek(max(0, fidx + int(seconds * PLAYER_FPS)))

  def _prev_clip(self) -> None:
    if self._index > 0:
      self._load_clip(self._index - 1)

  def _next_clip(self) -> None:
    if self._index < len(self._clips) - 1:
      self._load_clip(self._index + 1)

  def _update_state(self) -> None:
    super()._update_state()
    # Never allow playback while driving
    if ui_state.started:
      self._playing = False
      self.dismiss()

  # ---- rendering ----
  def _render(self, rect: rl.Rectangle) -> None:
    if self._decoder is not None:
      frame, seq, _, _, _eof, err = self._decoder.snapshot()
      if err:
        self._error = err
      if frame is not None and seq != self._last_seq and self._texture is not None:
        rl.update_texture(self._texture, rl.ffi.cast("void *", frame.ctypes.data))
        self._last_seq = seq

    video_rect = rl.Rectangle(rect.x + 40, rect.y + 90, rect.width - 80, rect.height - 300)

    if self._texture is not None:
      scale = min(video_rect.width / self._tex_w, video_rect.height / self._tex_h)
      dw, dh = self._tex_w * scale, self._tex_h * scale
      dst = rl.Rectangle(video_rect.x + (video_rect.width - dw) / 2,
                         video_rect.y + (video_rect.height - dh) / 2, dw, dh)
      rl.draw_texture_pro(self._texture, rl.Rectangle(0, 0, self._texture.width, self._texture.height),
                          dst, rl.Vector2(0, 0), 0.0, rl.WHITE)

    if self._clips:
      clip = self._clips[self._index]
      title = f"{self._camera_file}  ·  {clip.date_text}  {clip.time_text}"
      rl.draw_text_ex(self._font, title, rl.Vector2(rect.x + 40, rect.y + 30), 40, 0, rl.WHITE)

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


class _ClipRow(Widget):
  def __init__(self, clip: Clip, callback):
    super().__init__()
    self._clip = clip
    self._rect = rl.Rectangle(0, 0, 0, 150)
    self.set_click_callback(lambda: callback(self._clip))
    self._font = gui_app.font(FontWeight.MEDIUM)
    self._small_font = gui_app.font(FontWeight.NORMAL)

  def set_parent_rect(self, parent_rect: rl.Rectangle) -> None:
    super().set_parent_rect(parent_rect)
    self._rect.width = parent_rect.width

  def _render(self, rect: rl.Rectangle) -> None:
    bg = ROW_BG_PRESSED if self.is_pressed else ROW_BG
    rl.draw_rectangle_rounded(rect, 0.15, 8, bg)
    rl.draw_text_ex(self._font, self._clip.date_text, rl.Vector2(rect.x + 40, rect.y + 28), 46, 0, rl.WHITE)
    subtitle = f"{self._clip.time_text}   ·   segment {self._clip.segment}"
    rl.draw_text_ex(self._small_font, subtitle, rl.Vector2(rect.x + 40, rect.y + 88), 34, 0, SUBTEXT_COLOR)


class DashCamLayout(Widget):
  """Offroad-only settings panel: browse and play recorded dash cam clips."""

  def __init__(self):
    super().__init__()
    self._camera_idx = 0
    self._clips: list[Clip] = []
    self._scroller = Scroller([], spacing=12, line_separator=False, pad_end=True)
    self._loaded = False
    self._font = gui_app.font(FontWeight.MEDIUM)

    self._picker: list[Button] = []
    for i, (label, _) in enumerate(CAMERAS):
      btn = self._child(Button(label, lambda idx=i: self._set_camera(idx), font_size=40))
      self._picker.append(btn)

  def _set_camera(self, idx: int) -> None:
    if idx == self._camera_idx and self._loaded:
      return
    self._camera_idx = idx
    self._reload()

  def _reload(self) -> None:
    camera_file = CAMERAS[self._camera_idx][1]
    self._clips = list_clips(camera_file)
    rows = [_ClipRow(clip, self._open_clip) for clip in self._clips]
    self._scroller = Scroller(rows, spacing=12, line_separator=False, pad_end=True)
    self._scroller.show_event()
    self._loaded = True

  def _open_clip(self, clip: Clip) -> None:
    try:
      idx = self._clips.index(clip)
    except ValueError:
      idx = 0
    camera_file = CAMERAS[self._camera_idx][1]
    gui_app.push_widget(DashCamPlayer(self._clips, camera_file, idx))

  def show_event(self) -> None:
    super().show_event()
    # Refresh the list every time the panel is opened
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

    rl.draw_text_ex(self._font, tr("Dash Cam"), rl.Vector2(rect.x, rect.y), 56, 0, rl.WHITE)

    py = rect.y + 90
    pw, ph, gap = 220, 90, 16
    x = rect.x
    for i, btn in enumerate(self._picker):
      btn.set_button_style(ButtonStyle.PRIMARY if i == self._camera_idx else ButtonStyle.NORMAL)
      btn.render(rl.Rectangle(x, py, pw, ph))
      x += pw + gap

    list_y = py + ph + 30
    list_rect = rl.Rectangle(rect.x, list_y, rect.width, rect.height - (list_y - rect.y))
    if self._clips:
      self._scroller.render(list_rect)
    else:
      self._draw_message(list_rect, tr("No recordings found"))
