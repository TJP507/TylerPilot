"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import os
import threading
import time
import pyray as rl

from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.widgets.button import Button, ButtonStyle
from openpilot.system.ui.widgets.network import NetworkUI, PanelType

# Dropped to ask the modem process (system/hardware/tici/modem.py) to tear down
# and reinitialize so it re-reads the SIM/APN/roaming.
MODEM_REINIT_FILE = "/data/community/modem_reinit"


class NetworkUISP(NetworkUI):
  def __init__(self, wifi_manager):
    super().__init__(wifi_manager)

    self.scan_button = Button(tr("Scan"), self._scan_clicked, button_style=ButtonStyle.NORMAL, font_size=60, border_radius=30)
    self.scan_button.set_rect(rl.Rectangle(0, 0, 400, 100))

    self.reinit_modem_button = Button(tr("Reinitialize Modem"), self._reinit_modem, button_style=ButtonStyle.NORMAL, font_size=48, border_radius=30)
    self.reinit_modem_button.set_rect(rl.Rectangle(0, 0, 560, 100))

    self._scanning = False
    self._wifi_manager.add_callbacks(networks_updated=self._on_networks_updated)

  def _scan_clicked(self):
    self._scanning = True
    self.scan_button.set_text(tr("Scanning..."))
    self.scan_button.set_enabled(False)

    threading.Thread(target=self._wifi_manager._update_networks, daemon=True).start()
    self._wifi_manager._request_scan()
    self._wifi_manager._last_network_update = time.monotonic()

  def _on_networks_updated(self, networks):
    if self._scanning:
      self._scanning = False
      self.scan_button.set_text(tr("Scan"))
      self.scan_button.set_enabled(True)

  def _reinit_modem(self):
    try:
      os.makedirs(os.path.dirname(MODEM_REINIT_FILE), exist_ok=True)
      with open(MODEM_REINIT_FILE, "w"):
        pass
    except OSError:
      pass

  def _render(self, _):
    super()._render(_)

    if self._current_panel == PanelType.WIFI:
      self.scan_button.set_position(self._rect.x + self._back_btn.rect.width + 20, self._rect.y + 20)
      self.scan_button.render()
    elif self._current_panel == PanelType.CELLULAR:
      # Reinitialize Modem, top-right, aligned with the Back button.
      self.reinit_modem_button.set_position(self._rect.x + self._rect.width - self.reinit_modem_button.rect.width,
                                            self._rect.y + 20)
      self.reinit_modem_button.render()
