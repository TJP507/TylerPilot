"""
Persistent state for the web dash cam server.

The server is opt-in and password protected. Its state (on/off, username and a
randomly generated password) lives in a small JSON file rather than a Params
key: Params only accepts keys compiled into the prebuilt params library, and
this branch ships no build system to recompile it.

The file is the single source of truth shared by the settings UI (which reads
and writes it) and the server process (which reads it for credentials).
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import string
import threading

from openpilot.system.hardware import PC
from openpilot.system.hardware.hw import Paths

STATE_DIR = Paths.comma_home() if PC else "/data/community"
STATE_PATH = os.path.join(STATE_DIR, "webdashcam.json")
CERT_PATH = os.path.join(STATE_DIR, "webdashcam.crt")
KEY_PATH = os.path.join(STATE_DIR, "webdashcam.key")
DEFAULT_USER = "admin"
PORT = 8443

PASSWORD_LENGTH = 8
# Letters and numbers only. Ambiguous characters (0/O, 1/l/I) are dropped so the
# password can be read off the device screen and typed into a phone without mistakes.
_ALPHABET = "".join(c for c in string.ascii_letters + string.digits if c not in "0O1lI")
_LOCK = threading.Lock()


def _new_password() -> str:
  return "".join(secrets.choice(_ALPHABET) for _ in range(PASSWORD_LENGTH))


def load() -> dict:
  try:
    with open(STATE_PATH) as f:
      data = json.load(f)
  except (OSError, ValueError):
    data = {}
  if not isinstance(data, dict):
    data = {}
  return {
    "enabled": bool(data.get("enabled", False)),
    "user": str(data.get("user") or DEFAULT_USER),
    "password": str(data.get("password") or ""),
  }


def _save(state: dict) -> None:
  os.makedirs(STATE_DIR, exist_ok=True)
  tmp = STATE_PATH + ".tmp"
  with open(tmp, "w") as f:
    json.dump(state, f)
  os.chmod(tmp, 0o600)
  os.replace(tmp, STATE_PATH)


def get_enabled() -> bool:
  return load()["enabled"]


def get_password() -> str:
  return load()["password"]


def get_user() -> str:
  return load()["user"]


def credentials() -> tuple[str, str] | None:
  """(user, password) when the server is enabled and has a password, else None."""
  state = load()
  if state["enabled"] and state["password"]:
    return state["user"], state["password"]
  return None


def set_enabled(enabled: bool) -> dict:
  """Enable or disable the server. Enabling for the first time mints a password."""
  with _LOCK:
    state = load()
    state["enabled"] = bool(enabled)
    if enabled and not state["password"]:
      state["password"] = _new_password()
    _save(state)
    return state


def regenerate_password() -> str:
  with _LOCK:
    state = load()
    state["password"] = _new_password()
    _save(state)
    return state["password"]


def local_ip() -> str:
  """Best-effort local network address, for display in the settings UI."""
  s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  try:
    s.connect(("8.8.8.8", 80))
    return s.getsockname()[0]
  except OSError:
    return "0.0.0.0"
  finally:
    s.close()


def url() -> str:
  return f"https://{local_ip()}:{PORT}"
