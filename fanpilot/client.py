# SPDX-License-Identifier: GPL-3.0-or-later
"""Tiny client for the fanpilotd socket."""
from __future__ import annotations

import json
import socket

from . import SOCKET_PATH


class DaemonError(Exception):
    pass


class Client:
    def __init__(self, path: str = SOCKET_PATH, timeout: float = 3.0):
        self.path = path
        self.timeout = timeout
        self._sock = None
        self._file = None

    def _connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(self.path)
        except OSError as e:
            s.close()
            raise DaemonError(f"FanPilot service is not running ({e})") from e
        self._sock, self._file = s, s.makefile("rwb")

    def close(self):
        for o in (self._file, self._sock):
            try:
                if o:
                    o.close()
            except OSError:
                pass
        self._sock = self._file = None

    def call(self, cmd: str, **kw) -> dict:
        req = json.dumps({"cmd": cmd, **kw}).encode() + b"\n"
        for attempt in (0, 1):
            sent = False
            try:
                if self._sock is None:
                    self._connect()
                self._file.write(req)
                self._file.flush()
                sent = True
                line = self._file.readline()
                if not line:
                    raise OSError("connection closed")
                resp = json.loads(line)
                break
            except DaemonError:
                raise
            except (OSError, ValueError) as e:
                self.close()
                if attempt or (sent and isinstance(e, TimeoutError)):
                    # a timeout after sending means the daemon may already have acted on it: don't send
                    # it twice. A closed connection (daemon restarted) never processed it: retry once.
                    raise DaemonError(f"lost connection to FanPilot service ({e})") from e
        if not resp.get("ok"):
            raise DaemonError(resp.get("error", "unknown error"))
        return resp
