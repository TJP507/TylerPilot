"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

Persistent state for the Tailscale remote-access integration.

Everything the daemon needs lives under /data so it survives AGNOS A/B updates:
the rootfs is read-only and /var is a tmpfs, so nothing outside /data persists.
The binaries are the upstream static arm64 build (userspace wireguard-go, since
this 4.9 kernel has no WireGuard module and cannot load one).
"""
from __future__ import annotations

import json
import os
import threading

from openpilot.system.hardware import PC
from openpilot.system.hardware.hw import Paths

TS_DIR = "/data/tailscale"
TS_BIN_DIR = os.path.join(TS_DIR, "bin")
TAILSCALE_BIN = os.path.join(TS_BIN_DIR, "tailscale")
TAILSCALED_BIN = os.path.join(TS_BIN_DIR, "tailscaled")
TS_STATE = os.path.join(TS_DIR, "tailscaled.state")
TS_SOCKET = os.path.join(TS_DIR, "tailscaled.sock")
TS_LOG = os.path.join(TS_DIR, "tailscaled.log")

# The manager process owns these; the settings UI only reads the status file and
# writes command files, so the UI thread never spawns sudo or blocks on the CLI.
TS_STATUS = os.path.join(TS_DIR, "status.json")
TS_COMMAND = os.path.join(TS_DIR, "command.json")

# Config is a small JSON file, not a Params key: Params only accepts keys
# compiled into the prebuilt params library, and this branch ships no build
# system to recompile it.
STATE_DIR = Paths.comma_home() if PC else "/data/community"
CONFIG_PATH = os.path.join(STATE_DIR, "tailscale.json")

DEFAULT_HOSTNAME = "tylerpilot"
_LOCK = threading.Lock()


def load() -> dict:
  try:
    with open(CONFIG_PATH) as f:
      data = json.load(f)
  except (OSError, ValueError):
    data = {}
  if not isinstance(data, dict):
    data = {}
  return {
    "enabled": bool(data.get("enabled", False)),
    "hostname": str(data.get("hostname") or DEFAULT_HOSTNAME),
  }


def _save(state: dict) -> None:
  os.makedirs(STATE_DIR, exist_ok=True)
  tmp = CONFIG_PATH + ".tmp"
  with open(tmp, "w") as f:
    json.dump(state, f)
  os.replace(tmp, CONFIG_PATH)


def get_enabled() -> bool:
  return load()["enabled"]


def get_hostname() -> str:
  return load()["hostname"]


def set_enabled(enabled: bool) -> dict:
  with _LOCK:
    state = load()
    state["enabled"] = bool(enabled)
    _save(state)
    return state


def set_hostname(hostname: str) -> dict:
  with _LOCK:
    state = load()
    state["hostname"] = hostname or DEFAULT_HOSTNAME
    _save(state)
    return state
