"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

Developer submenu for the Tailscale remote-access integration. Shows connection
state and, while the device is unregistered, a QR code (and the raw URL) that
opens Tailscale's login page. All privileged work happens in the manager
process; this panel only reads the published status and queues commands.
"""
from __future__ import annotations

import os

import pyray as rl
import qrcode
import numpy as np

from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.ui.ui_state import device
from openpilot.sunnypilot.tailscale import config as ts_config
from openpilot.sunnypilot.tailscale import manager as ts_manager
from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets.button import Button, ButtonStyle
from openpilot.system.ui.widgets.nav_widget import NavWidget

TEXT_COLOR = rl.Color(255, 255, 255, 255)
SUBTEXT_COLOR = rl.Color(170, 170, 170, 255)
GOOD_COLOR = rl.Color(140, 220, 140, 255)
WARN_COLOR = rl.Color(255, 180, 120, 255)

QR_REFRESH_INTERVAL = 300.0

_ACTIVE_PANEL = None
_TIMEOUT_CB_REGISTERED = False


def _dismiss_active_panel() -> None:
  panel = _ACTIVE_PANEL
  if panel is None:
    return
  for _ in range(4):
    top = gui_app.get_active_widget()
    if top is None or top is panel:
      break
    gui_app.pop_widget()
  if gui_app.get_active_widget() is panel:
    gui_app.pop_widget()


class TailscalePanel(NavWidget):
  """Tailscale status, registration QR code, and connect/disconnect controls."""

  def __init__(self):
    super().__init__()
    self._font = gui_app.font(FontWeight.BOLD)
    self._small = gui_app.font(FontWeight.NORMAL)
    self._field = gui_app.font(FontWeight.MEDIUM)

    self._status: dict = {}
    self._status_mtime = -1.0
    self._qr_texture: rl.Texture | None = None
    self._qr_array = None
    self._qr_url = ""
    self._qr_generated = float("-inf")

    self._btn_back = self._child(Button(tr("Back"), lambda: self.dismiss(), font_size=40))
    self._btn_primary = self._child(Button(tr("Connect"), lambda: self._connect(), font_size=40, button_style=ButtonStyle.PRIMARY))
    self._btn_secondary = self._child(Button(tr("Disable"), lambda: self._disable(), font_size=40))
    self._btn_logout = self._child(Button(tr("Log out"), lambda: self._log_out(), font_size=40))

    global _TIMEOUT_CB_REGISTERED
    if not _TIMEOUT_CB_REGISTERED:
      device.add_interactive_timeout_callback(_dismiss_active_panel)
      _TIMEOUT_CB_REGISTERED = True

  # Back navigation is via the on-screen Back button only.
  def _back_enabled(self) -> bool:
    return False

  # ---- lifecycle ----
  def show_event(self) -> None:
    super().show_event()
    global _ACTIVE_PANEL
    _ACTIVE_PANEL = self

  def hide_event(self) -> None:
    super().hide_event()
    global _ACTIVE_PANEL
    if _ACTIVE_PANEL is self:
      _ACTIVE_PANEL = None

  def _update_state(self) -> None:
    super()._update_state()
    self._read_status()
    url = str(self._status.get("auth_url") or "")
    stale = rl.get_time() - self._qr_generated > QR_REFRESH_INTERVAL
    if url != self._qr_url or (url and stale):
      self._generate_qr(url)

  def _read_status(self) -> None:
    try:
      mtime = os.path.getmtime(ts_config.TS_STATUS)
    except OSError:
      mtime = -1.0
    if mtime != self._status_mtime:
      self._status = ts_manager.read_status()
      self._status_mtime = mtime

  # ---- actions ----
  def _install(self) -> None:
    ts_manager.request_command("install")

  def _enable(self) -> None:
    ts_config.set_enabled(True)

  def _disable(self) -> None:
    ts_config.set_enabled(False)

  def _connect(self) -> None:
    ts_manager.request_command("up")

  def _disconnect(self) -> None:
    ts_manager.request_command("down")

  def _log_out(self) -> None:
    ts_manager.request_command("logout")

  # ---- QR code ----
  def _generate_qr(self, url: str) -> None:
    self._qr_url = url
    self._qr_generated = rl.get_time()
    if self._qr_texture is not None and self._qr_texture.id != 0:
      rl.unload_texture(self._qr_texture)
    self._qr_texture = None
    self._qr_array = None
    if not url:
      return
    try:
      qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
      qr.add_data(url)
      qr.make(fit=True)
      img = qr.make_image(fill_color="black", back_color="white").convert("RGBA")
      arr = np.array(img, dtype=np.uint8)

      rl_image = rl.Image()
      rl_image.data = rl.ffi.cast("void *", arr.ctypes.data)
      rl_image.width = img.width
      rl_image.height = img.height
      rl_image.mipmaps = 1
      rl_image.format = rl.PixelFormat.PIXELFORMAT_UNCOMPRESSED_R8G8B8A8

      self._qr_texture = rl.load_texture_from_image(rl_image)
      self._qr_array = arr
    except Exception:
      cloudlog.exception("tailscale QR generation failed")
      self._qr_texture = None

  # ---- render ----
  def _render(self, rect: rl.Rectangle) -> None:
    st = self._status
    installed = bool(st.get("installed")) or ts_manager.installed()
    enabled = bool(st.get("enabled", ts_config.get_enabled()))
    backend = str(st.get("backend_state") or "Unknown")
    auth_url = str(st.get("auth_url") or "")
    running = backend == "Running"

    self._btn_back.render(rl.Rectangle(rect.x, rect.y, 200, 84))
    rl.draw_text_ex(self._font, tr("Tailscale"), rl.Vector2(rect.x + 240, rect.y + 8), 56, 0, TEXT_COLOR)

    label, color = self._status_line(installed, enabled, backend, auth_url)
    rl.draw_text_ex(self._small, label, rl.Vector2(rect.x + 240, rect.y + 86), 34, 0, color)

    content = rl.Rectangle(rect.x, rect.y + 160, rect.width, max(rect.height - 340, 0))
    if not installed:
      self._draw_center(content, tr("Tailscale is not installed"), tr("Install the upstream static build to get started."))
    elif not enabled:
      self._draw_center(content, tr("Tailscale is off"), tr("Enable it to connect this device to your tailnet."))
    elif running:
      self._render_connected(content)
    elif auth_url:
      self._render_login(content, auth_url)
    else:
      self._render_waiting(content)

    self._render_controls(rect, installed, enabled, running)

  def _status_line(self, installed: bool, enabled: bool, backend: str, auth_url: str) -> tuple[str, rl.Color]:
    if not installed:
      return tr("Not installed"), WARN_COLOR
    if not enabled:
      return tr("Off"), SUBTEXT_COLOR
    if backend == "Running":
      ips = self._status.get("ips") or []
      suffix = f"  ·  {ips[0]}" if ips else ""
      return tr("Connected") + suffix, GOOD_COLOR
    if auth_url:
      return tr("Waiting for login"), WARN_COLOR
    if backend == "Starting":
      return tr("Starting..."), SUBTEXT_COLOR
    return tr("Connecting..."), SUBTEXT_COLOR

  def _render_login(self, rect: rl.Rectangle, auth_url: str) -> None:
    half = rect.width // 2
    left = rl.Rectangle(rect.x, rect.y, half - 40, rect.height)
    right = rl.Rectangle(rect.x + half + 40, rect.y, half - 40, rect.height)

    y = left.y + 10
    steps = [
      tr("Scan this QR code with your phone"),
      tr("Sign in with your Tailscale account"),
      tr("The device joins your tailnet automatically"),
    ]
    for i, text in enumerate(steps):
      radius = 24
      cx = left.x + radius + 10
      text_x = left.x + radius * 2 + 30
      wrapped = self._wrap(text, left.width - (radius * 2 + 30), 42)
      text_h = len(wrapped) * 42
      cy = y + text_h // 2
      rl.draw_circle(int(cx), int(cy), radius, rl.Color(70, 70, 70, 255))
      num = str(i + 1)
      ns = measure_text_cached(self._small, num, 28)
      rl.draw_text_ex(self._small, num, rl.Vector2(int(cx - ns.x // 2), int(cy - ns.y // 2)), 28, 0, TEXT_COLOR)
      rl.draw_text_ex(self._small, "\n".join(wrapped), rl.Vector2(text_x, y), 42, 0, TEXT_COLOR)
      y += text_h + 36

    y += 10
    rl.draw_text_ex(self._small, tr("Or open this link:"), rl.Vector2(left.x, y), 34, 0, SUBTEXT_COLOR)
    y += 46
    rl.draw_text_ex(self._small, auth_url, rl.Vector2(left.x, y), 34, 0, TEXT_COLOR)

    size = min(right.width, right.height) - 20
    qr_rect = rl.Rectangle(right.x + (right.width - size) / 2, right.y, size, size)
    if self._qr_texture is not None and self._qr_texture.id != 0:
      rl.draw_rectangle_rounded(rl.Rectangle(qr_rect.x - 12, qr_rect.y - 12, qr_rect.width + 24, qr_rect.height + 24), 0.05, 8, rl.WHITE)
      rl.draw_texture_pro(self._qr_texture, rl.Rectangle(0, 0, self._qr_texture.width, self._qr_texture.height),
                          qr_rect, rl.Vector2(0, 0), 0, rl.WHITE)
    else:
      rl.draw_rectangle_rounded(qr_rect, 0.05, 8, rl.Color(60, 60, 60, 255))
      self._draw_center(qr_rect, tr("Generating QR code..."), "")

  def _render_connected(self, rect: rl.Rectangle) -> None:
    st = self._status
    rows = [
      (tr("Device name"), str(st.get("hostname") or "-")),
      (tr("Tailscale IP"), ", ".join(st.get("ips") or []) or "-"),
      (tr("Tailnet name"), str(st.get("dns_name") or "-")),
    ]
    y = rect.y + 30
    for name, value in rows:
      rl.draw_text_ex(self._small, name, rl.Vector2(rect.x, y), 38, 0, SUBTEXT_COLOR)
      rl.draw_text_ex(self._field, value, rl.Vector2(rect.x, y + 48), 46, 0, TEXT_COLOR)
      y += 150

  def _render_waiting(self, rect: rl.Rectangle) -> None:
    cx, cy = rect.x + rect.width / 2, rect.y + rect.height / 2
    start = (rl.get_time() * 320.0) % 360.0
    rl.draw_ring(rl.Vector2(cx, cy - 60), 26, 32, start, start + 250.0, 40, WARN_COLOR)
    size = measure_text_cached(self._small, tr("Connecting to Tailscale..."), 40)
    rl.draw_text_ex(self._small, tr("Connecting to Tailscale..."), rl.Vector2(cx - size.x / 2, cy + 10), 40, 0, SUBTEXT_COLOR)

  # ---- controls ----
  def _render_controls(self, rect: rl.Rectangle, installed: bool, enabled: bool, running: bool) -> None:
    w, h, gap = 340, 110, 24
    y = rect.y + rect.height - 150
    buttons: list[tuple[Button, str, object, ButtonStyle, bool]] = []

    if not installed:
      buttons.append((self._btn_primary, tr("Install"), self._install, ButtonStyle.PRIMARY, True))
    elif not enabled:
      buttons.append((self._btn_primary, tr("Enable"), self._enable, ButtonStyle.PRIMARY, True))
    else:
      if running:
        buttons.append((self._btn_logout, tr("Log out"), self._log_out, ButtonStyle.NORMAL, True))
      buttons.append((self._btn_secondary, tr("Disable"), self._disable, ButtonStyle.NORMAL, True))
      if running:
        buttons.append((self._btn_primary, tr("Disconnect"), self._disconnect, ButtonStyle.NORMAL, True))
      else:
        buttons.append((self._btn_primary, tr("Connect"), self._connect, ButtonStyle.PRIMARY, True))

    x = rect.x + rect.width
    for btn, text, callback, style, active in reversed(buttons):
      x -= w
      btn.set_text(text)
      btn.set_button_style(style)
      btn.set_click_callback(callback)
      btn.set_enabled(active)
      btn.render(rl.Rectangle(x, y, w, h))
      x -= gap

  def _wrap(self, text: str, max_width: float, font_size: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
      candidate = f"{current} {word}".strip()
      if measure_text_cached(self._small, candidate, font_size).x <= max_width or not current:
        current = candidate
      else:
        lines.append(current)
        current = word
    if current:
      lines.append(current)
    return lines

  def _draw_center(self, rect: rl.Rectangle, title: str, subtitle: str) -> None:
    size = measure_text_cached(self._font, title, 50)
    y = rect.y + (rect.height - size.y) / 2
    rl.draw_text_ex(self._font, title, rl.Vector2(rect.x + (rect.width - size.x) / 2, y), 50, 0, TEXT_COLOR)
    if subtitle:
      ssize = measure_text_cached(self._small, subtitle, 36)
      rl.draw_text_ex(self._small, subtitle, rl.Vector2(rect.x + (rect.width - ssize.x) / 2, y + size.y + 20), 36, 0, SUBTEXT_COLOR)

  def __del__(self):
    if self._qr_texture is not None and self._qr_texture.id != 0:
      rl.unload_texture(self._qr_texture)
