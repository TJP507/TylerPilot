"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

Owns the Tailscale daemon on the device.

Runs as an openpilot manager process. Each tick it keeps tailscaled alive (when
enabled), drives an interactive login when one is needed, publishes a small
status snapshot to /data/tailscale/status.json for the settings UI, and executes
commands the UI drops in /data/tailscale/command.json.

The daemon needs root for the TUN device and its netfilter rules, so every
invocation goes through passwordless sudo. tailscaled is detached from this
process so it keeps running across manager restarts.

Kernel WireGuard is unavailable on this hardware (4.9 kernel, no loadable
modules), so tailscaled automatically falls back to its userspace
wireguard-go implementation.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import time
import urllib.request

from openpilot.common.swaglog import cloudlog
from openpilot.sunnypilot.tailscale import config as ts_config

DOWNLOAD_ROOT = "https://pkgs.tailscale.com/stable"
PINNED_TARBALL = "tailscale_1.102.4_arm64.tgz"

TICK = 1.0
LONG_ACTION_TIMEOUT = 10.0


def _sudo(args: list, timeout: float = 20.0) -> subprocess.CompletedProcess:
  try:
    return subprocess.run(["sudo", "-n", *args], capture_output=True, text=True, timeout=timeout)
  except (OSError, subprocess.SubprocessError) as e:
    return subprocess.CompletedProcess(args, 1, "", str(e))


def _cli(args: list, timeout: float = 20.0) -> subprocess.CompletedProcess:
  return _sudo([ts_config.TAILSCALE_BIN, f"--socket={ts_config.TS_SOCKET}", *args], timeout=timeout)


def _spawn(args: list) -> bool:
  """Run a command detached from this process, appending its output to the daemon log."""
  os.makedirs(ts_config.TS_DIR, exist_ok=True)
  try:
    log = open(ts_config.TS_LOG, "ab", buffering=0)
  except OSError:
    return False
  try:
    subprocess.Popen(["sudo", "-n", "-b", *args], stdout=log, stderr=log,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    return True
  except OSError:
    return False
  finally:
    log.close()


def installed() -> bool:
  return (os.path.isfile(ts_config.TAILSCALE_BIN) and os.access(ts_config.TAILSCALE_BIN, os.X_OK) and
          os.path.isfile(ts_config.TAILSCALED_BIN) and os.access(ts_config.TAILSCALED_BIN, os.X_OK))


def _socket_present() -> bool:
  return os.path.exists(ts_config.TS_SOCKET)


def status_json() -> dict | None:
  if not _socket_present():
    return None
  r = _cli(["status", "--json"], timeout=8.0)
  if r.returncode != 0:
    return None
  try:
    data = json.loads(r.stdout)
  except (ValueError, TypeError):
    return None
  return data if isinstance(data, dict) else None


def daemon_running() -> bool:
  return status_json() is not None


def start_daemon() -> bool:
  if daemon_running():
    return True
  started = _spawn([ts_config.TAILSCALED_BIN, f"--state={ts_config.TS_STATE}", f"--socket={ts_config.TS_SOCKET}",
                    "--tun=tailscale0", "--port=41641"])
  if not started:
    return False
  for _ in range(20):
    if daemon_running():
      return True
    time.sleep(0.25)
  return daemon_running()


def stop_daemon() -> None:
  # Persist WantRunning=false so a restart does not immediately reconnect, then
  # stop the process (SIGTERM lets it remove its socket cleanly).
  _cli(["down"], timeout=LONG_ACTION_TIMEOUT)
  _sudo(["pkill", "-x", "tailscaled"], timeout=LONG_ACTION_TIMEOUT)


def _up_in_flight() -> bool:
  """True while an interactive `tailscale up` is still waiting for login."""
  for pid in os.listdir("/proc"):
    if not pid.isdigit():
      continue
    try:
      with open(os.path.join("/proc", pid, "cmdline"), "rb") as f:
        raw = f.read()
    except OSError:
      continue
    parts = [p.decode(errors="ignore") for p in raw.split(b"\x00") if p]
    if parts and os.path.basename(parts[0]) == "tailscale" and "up" in parts:
      return True
  return False


def start_login(hostname: str) -> None:
  if _up_in_flight():
    return
  _spawn([ts_config.TAILSCALE_BIN, f"--socket={ts_config.TS_SOCKET}", "up",
          "--accept-dns=false", f"--hostname={hostname}"])


def logout() -> None:
  _cli(["logout"], timeout=LONG_ACTION_TIMEOUT)


def down() -> None:
  _cli(["down"], timeout=LONG_ACTION_TIMEOUT)


def _latest_arm64_url() -> str:
  try:
    with urllib.request.urlopen(f"{DOWNLOAD_ROOT}/?mode=json", timeout=20) as r:
      data = json.load(r)
    name = (data.get("Tarballs") or {}).get("arm64")
    if name:
      return f"{DOWNLOAD_ROOT}/{name}"
  except Exception:
    cloudlog.exception("tailscale: failed to resolve latest tarball, using pinned version")
  return f"{DOWNLOAD_ROOT}/{PINNED_TARBALL}"


def install() -> tuple[bool, str]:
  """Download the upstream static build and place the two binaries in /data/tailscale/bin."""
  os.makedirs(ts_config.TS_BIN_DIR, exist_ok=True)
  url = _latest_arm64_url()
  archive = os.path.join(ts_config.TS_DIR, "tailscale.tgz")
  try:
    with urllib.request.urlopen(url, timeout=120) as r, open(archive, "wb") as f:
      shutil.copyfileobj(r, f)
  except Exception as e:
    return False, f"download failed: {e}"

  try:
    with tarfile.open(archive) as tf:
      for member in tf.getmembers():
        base = os.path.basename(member.name)
        if base not in ("tailscale", "tailscaled") or not member.isfile():
          continue
        src = tf.extractfile(member)
        if src is None:
          continue
        dest = os.path.join(ts_config.TS_BIN_DIR, base)
        with open(dest, "wb") as out:
          shutil.copyfileobj(src, out)
        os.chmod(dest, 0o755)
  except (tarfile.TarError, OSError) as e:
    return False, f"extract failed: {e}"
  finally:
    try:
      os.remove(archive)
    except OSError:
      pass

  return (True, "") if installed() else (False, "binaries missing after extract")


def request_command(name: str) -> None:
  """Queue a one-shot action for the manager to run (called from the UI process)."""
  os.makedirs(ts_config.TS_DIR, exist_ok=True)
  tmp = ts_config.TS_COMMAND + ".tmp"
  try:
    with open(tmp, "w") as f:
      json.dump({"name": name, "ts": time.monotonic()}, f)
    os.replace(tmp, ts_config.TS_COMMAND)
  except OSError:
    pass


def _take_command() -> str:
  try:
    with open(ts_config.TS_COMMAND) as f:
      data = json.load(f)
  except (OSError, ValueError):
    return ""
  try:
    os.remove(ts_config.TS_COMMAND)
  except OSError:
    pass
  return str(data.get("name") or "") if isinstance(data, dict) else ""


def write_status(state: dict) -> None:
  tmp = ts_config.TS_STATUS + ".tmp"
  try:
    os.makedirs(ts_config.TS_DIR, exist_ok=True)
    with open(tmp, "w") as f:
      json.dump(state, f)
    os.replace(tmp, ts_config.TS_STATUS)
  except OSError:
    pass


def read_status() -> dict:
  """Last published snapshot, for the settings UI. Never blocks."""
  try:
    with open(ts_config.TS_STATUS) as f:
      data = json.load(f)
  except (OSError, ValueError):
    data = {}
  if not isinstance(data, dict):
    data = {}
  return data


def _empty_state(enabled: bool) -> dict:
  return {
    "installed": False,
    "enabled": enabled,
    "daemon": False,
    "backend_state": "Unknown",
    "auth_url": "",
    "hostname": "",
    "dns_name": "",
    "ips": [],
    "error": "",
    "updated": time.monotonic(),
  }


def _handle_command(state: dict, enabled: bool, hostname: str) -> None:
  command = _take_command()
  if not command:
    return
  if command == "install":
    ok, err = install()
    state["error"] = "" if ok else err
  elif command == "up":
    if enabled and installed():
      start_login(hostname)
  elif command == "down":
    down()
  elif command == "logout":
    logout()


def tick() -> None:
  cfg = ts_config.load()
  enabled, hostname = cfg["enabled"], cfg["hostname"]
  state = _empty_state(enabled)
  _handle_command(state, enabled, hostname)

  state["installed"] = installed()
  if not state["installed"]:
    write_status(state)
    return

  st = status_json()
  if st is None and enabled:
    start_daemon()
    st = status_json()

  if st is not None:
    state["daemon"] = True
    self_node = st.get("Self") or {}
    state["backend_state"] = str(st.get("BackendState") or "Unknown")
    state["auth_url"] = str(st.get("AuthURL") or "")
    state["hostname"] = str(self_node.get("HostName") or "")
    state["dns_name"] = str(self_node.get("DNSName") or "")
    state["ips"] = [ip for ip in (st.get("TailscaleIPs") or []) if ip]

    if enabled and not state["auth_url"] and not _up_in_flight() and state["backend_state"] in ("Stopped", "NoState", "NeedsLogin"):
      start_login(hostname)
  elif not enabled:
    # Disabled and the daemon is not reachable: make sure it is fully stopped.
    if _socket_present():
      stop_daemon()

  write_status(state)


def main() -> None:
  while True:
    try:
      tick()
    except Exception:
      cloudlog.exception("tailscale manager tick failed")
    time.sleep(TICK)
