"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import json
import os

from enum import IntEnum

import pyray as rl

from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.lib.application import gui_app, FontWeight, MousePos
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.widgets.scroller_tici import Scroller
from openpilot.system.ui.sunnypilot.widgets.list_view import option_item_sp
from openpilot.system.ui.widgets.list_view import ListItem, ButtonAction, text_item, ItemAction
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


class OffroadBrightnessControl(ItemAction):
  def __init__(self, min_value=0, max_value=22, value_change_step=1,
               value_map=None, label_callback=None):
    super().__init__(enabled=True)
    self.min_value = min_value
    self.max_value = max_value
    self.value_change_step = value_change_step
    self.value_map = value_map
    self.label_callback = label_callback
    self.current_value = _read_offroad_cfg().get("brightness", 0)
    self._font = gui_app.font(FontWeight.MEDIUM)

  def _render(self, rect: rl.Rectangle):
    self._rect.width = 400
    minus_enabled = self.current_value > self.min_value
    plus_enabled = self.current_value < self.max_value
    btn_w, btn_h = 60, 60
    gap = 20
    total_w = btn_w * 2 + gap + 120
    x = rect.x + rect.width - total_w - 20
    y = rect.y + (rect.height - btn_h) / 2

    minus_rect = rl.Rectangle(x, y, btn_w, btn_h)
    plus_rect = rl.Rectangle(x + btn_w + gap + 120, y, btn_w, btn_h)
    label_rect = rl.Rectangle(x + btn_w + gap, y, 120, btn_h)

    for r, text, enabled in [(minus_rect, "-", minus_enabled), (plus_rect, "+", plus_enabled)]:
      color = rl.Color(74, 74, 74, 255) if enabled else rl.Color(57, 57, 57, 100)
      rl.draw_rectangle_rounded(r, 1.0, 10, color)
      ts = self._font
      tw = rl.measure_text_ex(ts, text, 60, 0).x
      th = rl.measure_text_ex(ts, text, 60, 0).y
      rl.draw_text_ex(ts, text, rl.Vector2(r.x + (r.width - tw) / 2, r.y + (r.height - th) / 2), 60, 0, rl.WHITE)

    label = self._get_label()
    ts = self._font
    tw = rl.measure_text_ex(ts, label, 40, 0).x
    th = rl.measure_text_ex(ts, label, 40, 0).y
    rl.draw_text_ex(ts, label, rl.Vector2(label_rect.x + (label_rect.width - tw) / 2, label_rect.y + (label_rect.height - th) / 2), 40, 0, rl.WHITE)

  def _get_label(self):
    v = self.current_value
    if callable(self.label_callback):
      if self.value_map and v in self.value_map:
        return self.label_callback(self.value_map[v])
      return self.label_callback(v)
    if self.value_map and v in self.value_map:
      return str(self.value_map[v])
    return str(v)

  def set_value(self, value):
    if self.min_value <= value <= self.max_value and value != self.current_value:
      self.current_value = value
      cfg = _read_offroad_cfg()
      cfg["brightness"] = value
      _write_offroad_cfg(cfg)

  def _handle_mouse_release(self, mouse_pos):
    x = self._rect.x + self._rect.width - 400 - 20
    y = self._rect.y + (self._rect.height - 60) / 2
    btn_w, btn_h, gap = 60, 60, 20
    minus_rect = rl.Rectangle(x, y, btn_w, btn_h)
    plus_rect = rl.Rectangle(x + btn_w + gap + 120, y, btn_w, btn_h)

    if rl.check_collision_point_rec(mouse_pos, minus_rect) and self.current_value > self.min_value:
      self.set_value(self.current_value - self.value_change_step)
    elif rl.check_collision_point_rec(mouse_pos, plus_rect) and self.current_value < self.max_value:
      self.set_value(self.current_value + self.value_change_step)


class OffroadTimerControl(ItemAction):
  def __init__(self, min_value=0, max_value=15, value_change_step=1,
               value_map=None, label_callback=None):
    super().__init__(enabled=True)
    self.min_value = min_value
    self.max_value = max_value
    self.value_change_step = value_change_step
    self.value_map = value_map
    self.label_callback = label_callback
    self.current_value = _read_offroad_cfg().get("timer", 0)
    self._font = gui_app.font(FontWeight.MEDIUM)

  def _render(self, rect: rl.Rectangle):
    self._rect.width = 400
    minus_enabled = self.current_value > self.min_value
    plus_enabled = self.current_value < self.max_value
    btn_w, btn_h = 60, 60
    gap = 20
    total_w = btn_w * 2 + gap + 120
    x = rect.x + rect.width - total_w - 20
    y = rect.y + (rect.height - btn_h) / 2

    minus_rect = rl.Rectangle(x, y, btn_w, btn_h)
    plus_rect = rl.Rectangle(x + btn_w + gap + 120, y, btn_w, btn_h)
    label_rect = rl.Rectangle(x + btn_w + gap, y, 120, btn_h)

    for r, text, enabled in [(minus_rect, "-", minus_enabled), (plus_rect, "+", plus_enabled)]:
      color = rl.Color(74, 74, 74, 255) if enabled else rl.Color(57, 57, 57, 100)
      rl.draw_rectangle_rounded(r, 1.0, 10, color)
      ts = self._font
      tw = rl.measure_text_ex(ts, text, 60, 0).x
      th = rl.measure_text_ex(ts, text, 60, 0).y
      rl.draw_text_ex(ts, text, rl.Vector2(r.x + (r.width - tw) / 2, r.y + (r.height - th) / 2), 60, 0, rl.WHITE)

    label = self._get_label()
    ts = self._font
    tw = rl.measure_text_ex(ts, label, 40, 0).x
    th = rl.measure_text_ex(ts, label, 40, 0).y
    rl.draw_text_ex(ts, label, rl.Vector2(label_rect.x + (label_rect.width - tw) / 2, label_rect.y + (label_rect.height - th) / 2), 40, 0, rl.WHITE)

  def _get_label(self):
    v = self.current_value
    if callable(self.label_callback):
      if self.value_map and v in self.value_map:
        return self.label_callback(self.value_map[v])
      return self.label_callback(v)
    if self.value_map and v in self.value_map:
      return str(self.value_map[v])
    return str(v)

  def set_value(self, value):
    if self.min_value <= value <= self.max_value and value != self.current_value:
      self.current_value = value
      cfg = _read_offroad_cfg()
      cfg["timer"] = value
      _write_offroad_cfg(cfg)

  def _handle_mouse_release(self, mouse_pos):
    x = self._rect.x + self._rect.width - 400 - 20
    y = self._rect.y + (self._rect.height - 60) / 2
    btn_w, btn_h, gap = 60, 60, 20
    minus_rect = rl.Rectangle(x, y, btn_w, btn_h)
    plus_rect = rl.Rectangle(x + btn_w + gap + 120, y, btn_w, btn_h)

    if rl.check_collision_point_rec(mouse_pos, minus_rect) and self.current_value > self.min_value:
      self.set_value(self.current_value - self.value_change_step)
    elif rl.check_collision_point_rec(mouse_pos, plus_rect) and self.current_value < self.max_value:
      self.set_value(self.current_value + self.value_change_step)


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
    self._offroad_control = OffroadBrightnessControl(
      min_value=0, max_value=22, value_change_step=1,
      label_callback=lambda value: self.update_offroad_brightness(value),
    )
    self._offroad_brightness = ListItem(
      title=lambda: tr("Offroad Brightness"),
      action_item=self._offroad_control,
    )
    self._offroad_timer_control = OffroadTimerControl(
      min_value=0, max_value=15, value_change_step=1,
      value_map=OFFROAD_BRIGHTNESS_TIMER_VALUES,
      label_callback=lambda value: f"{value} s" if value < 60 else f"{int(value/60)} m",
    )
    self._offroad_brightness_timer = ListItem(
      title=lambda: tr("Offroad Brightness Delay"),
      action_item=self._offroad_timer_control,
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
    offroad_val = self._offroad_control.current_value
    self._offroad_timer_control.set_enabled(offroad_val not in (OnroadBrightness.AUTO, OnroadBrightness.AUTO_DARK))

  def _render(self, rect):
    self._scroller.render(rect)

  def show_event(self):
    self._scroller.show_event()
