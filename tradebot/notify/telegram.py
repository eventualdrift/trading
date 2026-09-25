"""Notifications: Telegram (with remote commands) and console."""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable

import requests

log = logging.getLogger(__name__)


class ConsoleNotifier:
    def __init__(self, printer: Callable[[str], None] = print):
        self.printer = printer

    def send(self, text: str) -> None:
        plain = re.sub(r"<[^>]+>", "", text).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        self.printer(plain + "\n")


class MemoryNotifier:
    """Collects messages (tests, demo)."""

    def __init__(self):
        self.messages: list[str] = []

    def send(self, text: str) -> None:
        self.messages.append(text)


class MultiNotifier:
    def __init__(self, *notifiers):
        self.notifiers = [n for n in notifiers if n is not None]

    def send(self, text: str) -> None:
        for n in self.notifiers:
            n.send(text)

    def start_listener(self, handler):
        for n in self.notifiers:
            if hasattr(n, "start_listener"):
                return n.start_listener(handler)
        return None


class TelegramNotifier:
    API = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, token: str, chat_id: str | int, session: requests.Session | None = None):
        self.token = token
        self.chat_id = str(chat_id)
        self.session = session or requests.Session()
        self._offset = 0
        self._stop = threading.Event()

    def _call(self, method: str, http_timeout: float = 15, **payload):
        url = self.API.format(token=self.token, method=method)
        resp = self.session.post(url, json=payload, timeout=http_timeout)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
        return data["result"]

    def send(self, text: str) -> None:
        for chunk in _chunks(text, 4000):
            for attempt in range(3):
                try:
                    self._call("sendMessage", chat_id=self.chat_id, text=chunk,
                               parse_mode="HTML", disable_web_page_preview=True)
                    break
                except Exception as exc:  # never let a notification crash the bot
                    log.warning("Telegram send failed (attempt %d): %s", attempt + 1, exc)
                    time.sleep(2 ** attempt)

    def poll_once(self, handler: Callable[[str, list[str]], str], timeout: int = 25) -> None:
        updates = self._call("getUpdates", http_timeout=timeout + 10, offset=self._offset,
                             timeout=timeout, allowed_updates=["message"])
        for u in updates:
            self._offset = u["update_id"] + 1
            msg = u.get("message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            text = (msg.get("text") or "").strip()
            if not text.startswith("/"):
                continue
            if chat != self.chat_id:
                log.warning("ignoring command from unauthorised chat %s", chat)
                continue
            parts = text.split()
            cmd = parts[0][1:].split("@")[0].lower()
            try:
                reply = handler(cmd, parts[1:])
            except Exception as exc:
                log.exception("command /%s failed", cmd)
                reply = f"Command failed: {exc}"
            if reply:
                self.send(reply)

    def start_listener(self, handler: Callable[[str, list[str]], str]) -> threading.Thread:
        def loop():
            while not self._stop.is_set():
                try:
                    self.poll_once(handler)
                except Exception as exc:
                    log.warning("Telegram polling error: %s", exc)
                    self._stop.wait(5)

        t = threading.Thread(target=loop, name="telegram-commands", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()


def _chunks(text: str, size: int):
    while text:
        yield text[:size]
        text = text[size:]
