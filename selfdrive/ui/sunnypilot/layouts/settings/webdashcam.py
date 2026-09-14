"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

Developer submenu for the local dash cam web server: enable it, read the
generated password and address, and regenerate the password. The server itself
is started/stopped by the process manager from sunnypilot/webdashcam/config.py.
"""
from __future__ import annotations

import pyray as rl

from openpilot.selfdrive.ui.ui_state import device
from openpilot.sunnypilot.webdashcam import config as webdashcam
from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.widgets import DialogResult
from openpilot.system.ui.widgets.button import Button
from openpilot.system.ui.widgets.confirm_dialog import ConfirmDialog
from openpilot.system.ui.widgets.list_view import text_item
from openpilot.system.ui.widgets.nav_widget import NavWidget
from openpilot.system.ui.widgets.scroller_tici import Scroller
from openpilot.system.ui.sunnypilot.widgets.list_view import button_item_sp, toggle_item_sp

TEXT_COLOR = rl.Color(255, 255, 255, 255)

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


class WebDashcamPanel(NavWidget):
  """Enable the dash cam web server and manage its password."""

  def __init__(self):
    super().__init__()
    self._font = gui_app.font(FontWeight.MEDIUM)
    self._btn_back = self._child(Button(tr("Back"), lambda: self.dismiss(), font_size=40))

    self._toggle = toggle_item_sp(
      lambda: tr("Dash Cam Web Server"),
      self._description,
      initial_state=webdashcam.get_enabled(),
      callback=self._on_toggle,
    )
    self._password = text_item(lambda: tr("Web Password"), lambda: webdashcam.get_password() or tr("(disabled)"))
    self._address = text_item(lambda: tr("Web Address"), lambda: webdashcam.url() if webdashcam.get_enabled() else tr("(disabled)"))
    self._regen = button_item_sp(
      lambda: tr("Regenerate Web Password"),
      lambda: tr("RESET"),
      lambda: tr("Generate a new password. Devices already signed in will need the new one."),
      callback=self._on_regen,
    )
    self._scroller = Scroller([self._toggle, self._password, self._address, self._regen],
                              spacing=0, line_separator=True, pad_end=True)

    global _TIMEOUT_CB_REGISTERED
    if not _TIMEOUT_CB_REGISTERED:
      device.add_interactive_timeout_callback(_dismiss_active_panel)
      _TIMEOUT_CB_REGISTERED = True

  # Back navigation is via the on-screen Back button only.
  def _back_enabled(self) -> bool:
    return False

  def show_event(self) -> None:
    super().show_event()
    global _ACTIVE_PANEL
    _ACTIVE_PANEL = self
    self._scroller.show_event()

  def hide_event(self) -> None:
    super().hide_event()
    global _ACTIVE_PANEL
    if _ACTIVE_PANEL is self:
      _ACTIVE_PANEL = None

  def _update_state(self) -> None:
    super()._update_state()
    self._toggle.action_item.set_state(webdashcam.get_enabled())

  @staticmethod
  def _description() -> str:
    return tr("Browse and download dash cam clips from a phone or laptop on the same Wi-Fi.\n" +
              "The server only runs while parked and requires the password shown below.")

  @staticmethod
  def _on_toggle(state: bool) -> None:
    webdashcam.set_enabled(state)

  @staticmethod
  def _on_regen() -> None:
    def _do(result: int):
      if result == DialogResult.CONFIRM:
        webdashcam.regenerate_password()

    gui_app.push_widget(ConfirmDialog(
      tr("Generate a new web password? Devices already signed in will need the new one."),
      tr("Regenerate"), callback=_do))

  def _render(self, rect: rl.Rectangle) -> None:
    self._btn_back.render(rl.Rectangle(rect.x, rect.y, 200, 84))
    rl.draw_text_ex(self._font, tr("Dash Cam Web Server"), rl.Vector2(rect.x + 240, rect.y + 8), 56, 0, TEXT_COLOR)
    list_rect = rl.Rectangle(rect.x, rect.y + 150, rect.width, max(rect.height - 150, 0))
    self._scroller.render(list_rect)
