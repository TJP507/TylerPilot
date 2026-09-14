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
import time

import psutil

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


# Interfaces that must never be advertised: the cellular modem changes address
# constantly and is not reachable from the LAN or the tailnet.
CELLULAR_INTERFACE_PREFIXES = ("ppp", "rmnet", "wwan")
TAILSCALE_STATUS_PATH = "/data/tailscale/status.json"

_HOSTS_CACHE = {"at": -1.0, "hosts": []}
_HOSTS_TTL = 2.0


def tailscale_info() -> dict:
  try:
    with open(TAILSCALE_STATUS_PATH) as f:
      data = json.load(f)
  except (OSError, ValueError):
    return {}
  return data if isinstance(data, dict) else {}


def _ipv4s_for(predicate) -> list:
  out: list = []
  try:
    interfaces = psutil.net_if_addrs()
  except Exception:
    return out
  for name, addrs in interfaces.items():
    if not predicate(name):
      continue
    for addr in addrs:
      if addr.family == socket.AF_INET and addr.address and not addr.address.startswith("127.") and addr.address != "0.0.0.0":
        out.append(addr.address)
  return out


def lan_ips() -> list:
  """IPv4 addresses of user-facing interfaces (Wi-Fi, tether, USB Ethernet)."""
  return _ipv4s_for(lambda n: not n.startswith(("lo", "tailscale", *CELLULAR_INTERFACE_PREFIXES)))


def tailscale_ips() -> list:
  ips = _ipv4s_for(lambda n: n.startswith("tailscale"))
  for ip in (tailscale_info().get("ips") or []):
    if isinstance(ip, str) and ":" not in ip and ip not in ips:
      ips.append(ip)
  return ips


def tailscale_names() -> list:
  info = tailscale_info()
  names: list = []
  for name in (str(info.get("hostname") or "").strip(), str(info.get("dns_name") or "").strip().rstrip(".")):
    if name and name not in names:
      names.append(name)
  return names


def hosts() -> list:
  """Hosts the web UI can be reached at, most robust first. Cellular is never included.

  The Tailscale identity is preferred because it works from anywhere the device
  is connected; a LAN address follows for clients that are on the same network.
  """
  now = time.monotonic()
  if now - _HOSTS_CACHE["at"] > _HOSTS_TTL:
    seen: set = set()
    ordered: list = []
    for host in tailscale_names() + tailscale_ips() + lan_ips():
      if host and host not in seen:
        seen.add(host)
        ordered.append(host)
    _HOSTS_CACHE.update(at=now, hosts=ordered)
  return list(_HOSTS_CACHE["hosts"])


def urls() -> list:
  return [f"https://{host}:{PORT}" for host in hosts()]


def url() -> str:
  options = urls()
  return options[0] if options else f"https://localhost:{PORT}"


def lan_url() -> str | None:
  ips = lan_ips()
  return f"https://{ips[0]}:{PORT}" if ips else None
