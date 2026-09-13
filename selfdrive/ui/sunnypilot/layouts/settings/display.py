"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import json
import os

from enum import IntEnum

from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.widgets.scroller_tici import Scroller
from openpilot.system.ui.sunnypilot.widgets.list_view import option_item_sp
from openpilot.system.ui.sunnypilot.widgets.option_control import OptionControlSP
from openpilot.system.ui.sunnypilot.widgets.list_view import ListItemSP
from openpilot.sunnypilot.system.params_migration import ONROAD_BRIGHTNESS_TIMER_VALUES, OFFROAD_BRIGHTNESS_TIMER_VALUES

OFFROAD_CFG_PATH = "/data/community/offroad_brightness.json"


def _read_offroad_cfg():
  try:
    with open(OFFROAD_CFG_PATH) as f:
      return json.load(f)
  except (FileNotFoundError, json.JSONDecodeError):
    return {"brightness": 0, "timer": 0}


def _write_offroad_cfg(cfg: dict):
  os.makedirs(os.path.dirname(OFFROAD_CFG_PATH), exist_ok=True)
  with open(OFFROAD_CFG_PATH, "w") as f:
    json.dump(cfg, f)


class OnroadBrightness(IntEnum):
  AUTO = 0
  AUTO_DARK = 1
  SCREEN_OFF = 2


class OffroadBrightnessControlSP(OptionControlSP):
  def _read_param(self):
    return _read_offroad_cfg().get("brightness", 0)

  def _write_param(self, value):
    cfg = _read_offroad_cfg()
    cfg["brightness"] = value
    _write_offroad_cfg(cfg)


class OffroadTimerControlSP(OptionControlSP):
  def _read_param(self):
    return _read_offroad_cfg().get("timer", 0)

  def _write_param(self, value):
    cfg = _read_offroad_cfg()
    cfg["timer"] = value
    _write_offroad_cfg(cfg)


def offroad_option_item(title, control_cls, min_value=0, max_value=22,
                        value_change_step=1, value_map=None, label_callback=None):
  action = control_cls(
    param="", min_value=min_value, max_value=max_value,
    value_change_step=value_change_step, value_map=value_map,
    label_callback=label_callback,
  )
  return ListItemSP(title=title, action_item=action)


class DisplayLayout(Widget):
  def __init__(self):
    super().__init__()

    items = self._initialize_items()
    self._scroller = Scroller(items, line_separator=True, spacing=0)

  def _initialize_items(self):
    self._onroad_brightness = option_item_sp(
      param="OnroadScreenOffBrightness",
      title=lambda: tr("Onroad Brightness"),
      description="",
      min_value=0,
      max_value=22,
      value_change_step=1,
      label_callback=lambda value: self.update_onroad_brightness(value),
      inline=True
    )
    self._onroad_brightness_timer = option_item_sp(
      param="OnroadScreenOffTimer",
      title=lambda: tr("Onroad Brightness Delay"),
      description="",
      min_value=0,
      max_value=15,
      value_change_step=1,
      value_map=ONROAD_BRIGHTNESS_TIMER_VALUES,
      label_callback=lambda value: f"{value} s" if value < 60 else f"{int(value/60)} m",
      inline=True
    )
    self._interactivity_timeout = option_item_sp(
      param="InteractivityTimeout",
      title=lambda: tr("Interactivity Timeout"),
      description=lambda: tr("Apply a custom timeout for settings UI." +
                             "<br>This is the time after which settings UI closes automatically " +
                             "if user is not interacting with the screen."),
      min_value=0,
      max_value=120,
      value_change_step=10,
      label_callback=lambda value: (tr("Default") if not value or value == 0 else
                                    f"{value} s" if value < 60 else f"{int(value/60)} m"),
      inline=True
    )
    self._offroad_brightness = offroad_option_item(
      title=lambda: tr("Offroad Brightness"),
      control_cls=OffroadBrightnessControlSP,
      min_value=0, max_value=22, value_change_step=1,
      label_callback=lambda value: self.update_offroad_brightness(value),
    )
    self._offroad_brightness_timer = offroad_option_item(
      title=lambda: tr("Offroad Brightness Delay"),
      control_cls=OffroadTimerControlSP,
      min_value=0, max_value=15, value_change_step=1,
      value_map=OFFROAD_BRIGHTNESS_TIMER_VALUES,
      label_callback=lambda value: f"{value} s" if value < 60 else f"{int(value/60)} m",
    )
    items = [
      self._onroad_brightness,
      self._onroad_brightness_timer,
      self._offroad_brightness,
      self._offroad_brightness_timer,
      self._interactivity_timeout,
    ]
    return items

  @staticmethod
  def update_onroad_brightness(val):
    if val == OnroadBrightness.AUTO:
      return tr("Auto (Default)")
    if val == OnroadBrightness.AUTO_DARK:
      return tr("Auto (Dark)")
    if val == OnroadBrightness.SCREEN_OFF:
      return tr("Screen Off")
    return f"{(val - 2) * 5} %"

  @staticmethod
  def update_offroad_brightness(val):
    if val == OnroadBrightness.AUTO:
      return tr("Default")
    if val == OnroadBrightness.SCREEN_OFF:
      return tr("Screen Off")
    return f"{(val - 2) * 5} %"

  def _update_state(self):
    super()._update_state()
    brightness_val = self._onroad_brightness.action_item.current_value
    self._onroad_brightness_timer.action_item.set_enabled(brightness_val not in (OnroadBrightness.AUTO, OnroadBrightness.AUTO_DARK))
    offroad_val = self._offroad_brightness.action_item.current_value
    self._offroad_brightness_timer.action_item.set_enabled(offroad_val not in (OnroadBrightness.AUTO, OnroadBrightness.AUTO_DARK))

  def _render(self, rect):
    self._scroller.render(rect)

  def show_event(self):
    self._scroller.show_event()
