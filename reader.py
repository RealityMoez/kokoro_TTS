from __future__ import annotations

import argparse
import atexit
import ctypes
import json
import logging
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import winsound
from dataclasses import dataclass
from pathlib import Path
from typing import IO, List
from urllib.parse import urlparse

from ctypes import wintypes

import pyperclip
import requests
import tkinter as tk
from PIL import Image, ImageDraw
from pystray import Icon, Menu, MenuItem
from tkinter import messagebox, ttk
import uvicorn


if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "reader_config.json"

DEFAULT_ENDPOINT = "http://127.0.0.1:8000"
DEFAULT_HOTKEY = "ctrl+alt+s"
DEFAULT_VOICE = "af_heart"
DEFAULT_SPEED = 1.0


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("kokoro_reader")


WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
HOTKEY_ID = 1

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_C = 0x43
VK_INSERT = 0x2D

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


@dataclass
class ReaderConfig:
    endpoint: str = DEFAULT_ENDPOINT
    hotkey: str = DEFAULT_HOTKEY
    voice: str = DEFAULT_VOICE
    speed: float = DEFAULT_SPEED


class KokoroReader:
    def __init__(self) -> None:
        self.config = self._load_config()
        self.voices: List[str] = [self.config.voice]
        self.http = requests.Session()

        self.hotkey_thread: threading.Thread | None = None
        self.hotkey_thread_id: int | None = None
        self.hotkey_ready = threading.Event()
        self.hotkey_register_ok = False
        self.hotkey_register_error = ""
        self.hotkey_active = False
        self.icon: Icon | None = None
        self.settings_window: tk.Toplevel | None = None
        self.speak_lock = threading.Lock()

        self.server_process: subprocess.Popen[str] | None = None
        self.server_log_thread: threading.Thread | None = None

        self._temp_files: List[Path] = []
        atexit.register(self._cleanup_temp_files)
        atexit.register(self.http.close)

        self.root = tk.Tk()
        self.root.withdraw()

        logger.info("Reader initialized. Endpoint=%s Hotkey=%s", self.config.endpoint, self.config.hotkey)

    def _load_config(self) -> ReaderConfig:
        if not CONFIG_PATH.exists():
            return ReaderConfig()

        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to read config, using defaults: %s", exc)
            return ReaderConfig()

        endpoint = str(raw.get("endpoint", DEFAULT_ENDPOINT)).strip() or DEFAULT_ENDPOINT
        hotkey = str(raw.get("hotkey", DEFAULT_HOTKEY)).strip() or DEFAULT_HOTKEY
        voice = str(raw.get("voice", DEFAULT_VOICE)).strip() or DEFAULT_VOICE
        try:
            speed = float(raw.get("speed", DEFAULT_SPEED))
        except (TypeError, ValueError):
            speed = DEFAULT_SPEED
        speed = min(2.0, max(0.5, speed))
        return ReaderConfig(endpoint=endpoint.rstrip("/"), hotkey=hotkey.lower(), voice=voice, speed=speed)

    def _save_config(self) -> None:
        CONFIG_PATH.write_text(json.dumps(self.config.__dict__, indent=2), encoding="utf-8")
        logger.info("Settings saved.")

    def _notify(self, title: str, message: str) -> None:
        logger.info("%s: %s", title, message)
        if self.icon is None:
            return
        try:
            self.icon.notify(message, title=title)
        except Exception:
            pass

    def _fetch_voices(self) -> List[str]:
        try:
            response = self.http.get(f"{self.config.endpoint}/voices", timeout=(2, 5))
            response.raise_for_status()
            data = response.json()
            voices = data.get("voices", [])
            if isinstance(voices, list) and voices:
                self.voices = [str(v) for v in voices if isinstance(v, str)]
                logger.info("Loaded %d voices.", len(self.voices))
        except Exception as exc:
            logger.warning("Could not load voices from server: %s", exc)
            if not self.voices:
                self.voices = [self.config.voice]
        return self.voices

    def _parse_hotkey(self, hotkey: str) -> tuple[int, int]:
        parts = [p.strip().lower() for p in hotkey.split("+") if p.strip()]
        if not parts:
            raise ValueError("Hotkey is empty")

        modifiers = MOD_NOREPEAT
        key_token = ""
        for token in parts:
            if token in {"ctrl", "control"}:
                modifiers |= MOD_CONTROL
            elif token in {"alt"}:
                modifiers |= MOD_ALT
            elif token in {"shift"}:
                modifiers |= MOD_SHIFT
            elif token in {"win", "windows", "meta"}:
                modifiers |= MOD_WIN
            else:
                key_token = token

        if not key_token:
            raise ValueError("Hotkey must include a non-modifier key")

        named_keys = {
            "space": 0x20,
            "tab": 0x09,
            "enter": 0x0D,
            "return": 0x0D,
            "esc": 0x1B,
            "escape": 0x1B,
            "insert": 0x2D,
            "delete": 0x2E,
            "home": 0x24,
            "end": 0x23,
            "pageup": 0x21,
            "pagedown": 0x22,
            "up": 0x26,
            "down": 0x28,
            "left": 0x25,
            "right": 0x27,
        }

        if len(key_token) == 1 and key_token.isalpha():
            vk = ord(key_token.upper())
        elif len(key_token) == 1 and key_token.isdigit():
            vk = ord(key_token)
        elif key_token in named_keys:
            vk = named_keys[key_token]
        elif key_token.startswith("f") and key_token[1:].isdigit():
            fn = int(key_token[1:])
            if not 1 <= fn <= 24:
                raise ValueError("Function key must be F1..F24")
            vk = 0x70 + fn - 1
        else:
            raise ValueError(f"Unsupported hotkey key: {key_token}")

        return modifiers, vk

    def _hotkey_loop(self, hotkey: str) -> None:
        self.hotkey_thread_id = kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()

        modifiers = 0
        vk = 0
        try:
            modifiers, vk = self._parse_hotkey(hotkey)
            ok = user32.RegisterHotKey(None, HOTKEY_ID, modifiers, vk)
            if not ok:
                error_code = ctypes.get_last_error()
                if error_code == 1409:
                    self.hotkey_register_error = (
                        f"Hotkey '{hotkey}' is already registered by another app. "
                        "Try another combination."
                    )
                else:
                    self.hotkey_register_error = str(ctypes.WinError(error_code))
                self.hotkey_register_ok = False
                logger.error("RegisterHotKey failed for %s: %s", hotkey, self.hotkey_register_error)
                self.hotkey_ready.set()
                return

            self.hotkey_register_ok = True
            self.hotkey_register_error = ""
            logger.info("Hotkey registered (WinAPI): %s", hotkey)
            self.hotkey_ready.set()

            while True:
                res = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if res == 0:
                    break
                if res == -1:
                    logger.error("GetMessageW returned error in hotkey loop.")
                    break
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    self._on_hotkey()
        except Exception as exc:
            self.hotkey_register_error = str(exc)
            self.hotkey_register_ok = False
            logger.error("Hotkey loop failed: %s", exc)
            self.hotkey_ready.set()
        finally:
            if modifiers and vk:
                user32.UnregisterHotKey(None, HOTKEY_ID)
            self.hotkey_thread_id = None
            logger.info("Hotkey listener stopped.")

    def _stop_hotkey_listener(self) -> None:
        if self.hotkey_thread is not None and self.hotkey_thread.is_alive() and self.hotkey_thread_id is not None:
            user32.PostThreadMessageW(self.hotkey_thread_id, WM_QUIT, 0, 0)
            self.hotkey_thread.join(timeout=2)

        self.hotkey_thread = None
        self.hotkey_thread_id = None
        self.hotkey_active = False

    def _register_hotkey(self) -> None:
        self._stop_hotkey_listener()
        self.hotkey_ready.clear()
        self.hotkey_register_ok = False
        self.hotkey_register_error = ""

        self.hotkey_thread = threading.Thread(target=self._hotkey_loop, args=(self.config.hotkey,), daemon=True)
        self.hotkey_thread.start()

        if not self.hotkey_ready.wait(timeout=2):
            raise RuntimeError("Hotkey listener did not initialize in time")
        if not self.hotkey_register_ok:
            self._notify("Kokoro Reader", f"Hotkey registration failed: {self.hotkey_register_error}")
            raise RuntimeError(self.hotkey_register_error or "Failed to register hotkey")
        self.hotkey_active = True

    def _on_hotkey(self) -> None:
        logger.info("Hotkey triggered: %s", self.config.hotkey)
        threading.Thread(target=self.speak_selected_text, daemon=True).start()

    def _capture_selected_text(self) -> str:
        t0 = time.perf_counter()
        marker = f"__kokoro_reader_marker_{time.time_ns()}__"
        previous_text = ""

        try:
            previous_text = pyperclip.paste()
        except Exception as exc:
            logger.warning("Clipboard read failed before capture: %s", exc)

        copied = marker
        try:
            pyperclip.copy(marker)
            marker_seq = user32.GetClipboardSequenceNumber()
            time.sleep(0.01)

            # Wait briefly for hotkey modifiers to release before sending Ctrl+C.
            time.sleep(0.02)
            self._press_ctrl_combo(VK_C)

            for _ in range(24):
                time.sleep(0.01)
                if user32.GetClipboardSequenceNumber() != marker_seq:
                    copied = pyperclip.paste() or ""
                    if copied != marker:
                        break

            if copied == marker:
                logger.info("Primary copy attempt failed, trying Ctrl+Insert fallback.")
                self._press_ctrl_combo(VK_INSERT)
                for _ in range(24):
                    time.sleep(0.01)
                    if user32.GetClipboardSequenceNumber() != marker_seq:
                        copied = pyperclip.paste() or ""
                        if copied != marker:
                            break
        except Exception as exc:
            logger.error("Selection capture failed: %s", exc)
            return ""
        finally:
            try:
                pyperclip.copy(previous_text)
            except Exception:
                pass

        if copied == marker:
            logger.info("Selection capture failed after %.0f ms", (time.perf_counter() - t0) * 1000)
            return ""

        text = copied.strip()
        logger.info(
            "Captured selected text length: %d (%.0f ms)",
            len(text),
            (time.perf_counter() - t0) * 1000,
        )
        return text

    def _press_ctrl_combo(self, key_vk: int) -> None:
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        time.sleep(0.004)
        user32.keybd_event(key_vk, 0, 0, 0)
        time.sleep(0.004)
        user32.keybd_event(key_vk, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.004)
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)

    def _synthesize(self, text: str) -> bytes:
        payload = {
            "text": text,
            "voice": self.config.voice,
            "speed": self.config.speed,
            "format": "wav",
        }
        t0 = time.perf_counter()
        logger.info("Sending TTS request. chars=%d voice=%s speed=%.2f", len(text), self.config.voice, self.config.speed)
        response = self.http.post(f"{self.config.endpoint}/tts", json=payload, timeout=(5, 120))
        response.raise_for_status()
        logger.info("TTS response received in %.0f ms", (time.perf_counter() - t0) * 1000)
        return response.content

    def _play_wav(self, audio_data: bytes) -> None:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        tmp.write(audio_data)
        tmp.close()

        path = Path(tmp.name)
        self._temp_files.append(path)

        winsound.PlaySound(None, winsound.SND_PURGE)
        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
        logger.info("Playback started: %s", path)

    def _cleanup_temp_files(self) -> None:
        for path in self._temp_files:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

    def _get_local_server_target(self) -> tuple[str, int] | None:
        parsed = urlparse(self.config.endpoint)
        host = parsed.hostname
        port = parsed.port
        if host is None:
            return None
        if host.lower() not in {"127.0.0.1", "localhost"}:
            return None
        if port is None:
            if parsed.scheme == "https":
                port = 443
            else:
                port = 80
        return host, port

    def _health_check(self) -> bool:
        try:
            response = self.http.get(f"{self.config.endpoint}/health", timeout=(1, 2))
            if response.ok:
                return True
        except Exception:
            return False
        return False

    def _read_server_output(self, stream: IO[str]) -> None:
        try:
            for line in stream:
                text = line.rstrip()
                if text:
                    logger.info("SERVER | %s", text)
        except Exception as exc:
            logger.warning("Server output reader stopped: %s", exc)

    def _start_server(self, notify_when_already: bool = True) -> None:
        if self.server_process is not None and self.server_process.poll() is None:
            if notify_when_already:
                self._notify("Kokoro Reader", "Server is already running.")
            else:
                logger.info("Server already running (managed process).")
            return

        target = self._get_local_server_target()
        if target is None:
            self._notify("Kokoro Reader", "Endpoint is not local. Cannot auto-start server.")
            return

        host, port = target
        if self._health_check():
            if notify_when_already:
                self._notify("Kokoro Reader", "Server is already running.")
            else:
                logger.info("Server already reachable on %s:%s.", host, port)
            return

        venv_python = APP_DIR / ".venv" / "Scripts" / "python.exe"
        if getattr(sys, "frozen", False):
            command = [
                sys.executable,
                "--run-server",
                "--host",
                host,
                "--port",
                str(port),
            ]
        else:
            python_exe = str(venv_python if venv_python.exists() else Path(sys.executable))
            command = [
                python_exe,
                str(APP_DIR / "reader.py"),
                "--run-server",
                "--host",
                host,
                "--port",
                str(port),
            ]

        logger.info("Starting Kokoro server: %s", " ".join(command))
        try:
            self.server_process = subprocess.Popen(
                command,
                cwd=str(APP_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            self.server_process = None
            self._notify("Kokoro Reader", f"Failed to start server: {exc}")
            return

        if self.server_process.stdout is not None:
            self.server_log_thread = threading.Thread(
                target=self._read_server_output,
                args=(self.server_process.stdout,),
                daemon=True,
            )
            self.server_log_thread.start()

        for _ in range(30):
            if self._health_check():
                self._notify("Kokoro Reader", f"Server started on {host}:{port}.")
                return
            if self.server_process.poll() is not None:
                break
            time.sleep(0.2)

        code = self.server_process.poll() if self.server_process is not None else None
        self._notify("Kokoro Reader", f"Server did not become ready (exit={code}).")

    def _stop_server(self) -> None:
        if self.server_process is None or self.server_process.poll() is not None:
            self._notify("Kokoro Reader", "No server process managed by Reader.")
            self.server_process = None
            return

        logger.info("Stopping Kokoro server process.")
        self.server_process.terminate()
        try:
            self.server_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.server_process.kill()
            self.server_process.wait(timeout=5)

        self.server_process = None
        self._notify("Kokoro Reader", "Server stopped.")

    def speak_selected_text(self) -> None:
        t0 = time.perf_counter()
        text = self._capture_selected_text()
        if not text:
            self._notify("Kokoro Reader", "No selected text found.")
            return
        logger.info("Selected-text flow to synth start: %.0f ms", (time.perf_counter() - t0) * 1000)
        self.speak_text(text)

    def speak_clipboard_text(self) -> None:
        try:
            text = (pyperclip.paste() or "").strip()
        except Exception as exc:
            logger.error("Clipboard read failed: %s", exc)
            text = ""
        if not text:
            self._notify("Kokoro Reader", "Clipboard is empty.")
            return
        logger.info("Using clipboard text length: %d", len(text))
        self.speak_text(text)

    def speak_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            return

        if not self.speak_lock.acquire(blocking=False):
            self._notify("Kokoro Reader", "Already generating audio.")
            return

        def run() -> None:
            try:
                audio_data = self._synthesize(text)
                self._play_wav(audio_data)
                self._notify("Kokoro Reader", "Speaking text.")
            except requests.RequestException as exc:
                self._notify("Kokoro Reader", f"Server error: {exc}")
            except Exception as exc:
                self._notify("Kokoro Reader", f"Error: {exc}")
            finally:
                self.speak_lock.release()

        threading.Thread(target=run, daemon=True).start()

    def _build_icon_image(self) -> Image.Image:
        img = Image.new("RGB", (64, 64), color=(33, 38, 45))
        draw = ImageDraw.Draw(img)
        draw.ellipse((8, 8, 56, 56), fill=(241, 196, 96))
        draw.text((25, 19), "K", fill=(33, 38, 45))
        return img

    def _open_settings_window(self) -> None:
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.deiconify()
            self.settings_window.lift()
            self.settings_window.focus_force()
            return

        self._fetch_voices()

        win = tk.Toplevel(self.root)
        win.title("Kokoro Reader Settings")
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", win.withdraw)
        self.settings_window = win

        frame = ttk.Frame(win, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")

        endpoint_var = tk.StringVar(value=self.config.endpoint)
        hotkey_var = tk.StringVar(value=self.config.hotkey)
        voice_var = tk.StringVar(value=self.config.voice)
        speed_var = tk.StringVar(value=f"{self.config.speed:.1f}")
        sample_var = tk.StringVar(value="Hello from Kokoro Reader.")
        server_status_var = tk.StringVar(value="running" if self._health_check() else "stopped")
        hotkey_status_var = tk.StringVar(value="active" if self.hotkey_active else "inactive")

        ttk.Label(frame, text="Server Endpoint").grid(row=0, column=0, sticky="w")
        endpoint_entry = ttk.Entry(frame, textvariable=endpoint_var, width=34)
        endpoint_entry.grid(row=1, column=0, sticky="ew", pady=(0, 8))

        ttk.Label(frame, text="Global Hotkey").grid(row=2, column=0, sticky="w")
        hotkey_entry = ttk.Entry(frame, textvariable=hotkey_var, width=34)
        hotkey_entry.grid(row=3, column=0, sticky="ew", pady=(0, 8))

        ttk.Label(frame, text="Voice").grid(row=4, column=0, sticky="w")
        voice_box = ttk.Combobox(frame, textvariable=voice_var, values=self.voices, state="readonly", width=31)
        voice_box.grid(row=5, column=0, sticky="ew", pady=(0, 8))

        ttk.Label(frame, text="Speed (0.5 - 2.0)").grid(row=6, column=0, sticky="w")
        speed_entry = ttk.Entry(frame, textvariable=speed_var, width=34)
        speed_entry.grid(row=7, column=0, sticky="ew", pady=(0, 8))

        ttk.Label(frame, text="Test Text").grid(row=8, column=0, sticky="w")
        sample_entry = ttk.Entry(frame, textvariable=sample_var, width=34)
        sample_entry.grid(row=9, column=0, sticky="ew", pady=(0, 8))

        ttk.Label(frame, text="Server Status").grid(row=10, column=0, sticky="w")
        ttk.Label(frame, textvariable=server_status_var).grid(row=11, column=0, sticky="w", pady=(0, 10))

        ttk.Label(frame, text="Hotkey Status").grid(row=12, column=0, sticky="w")
        ttk.Label(frame, textvariable=hotkey_status_var).grid(row=13, column=0, sticky="w", pady=(0, 10))

        button_frame = ttk.Frame(frame)
        button_frame.grid(row=14, column=0, sticky="ew")

        def refresh_voices() -> None:
            self._fetch_voices()
            voice_box.configure(values=self.voices)
            if voice_var.get() not in self.voices and self.voices:
                voice_var.set(self.voices[0])

        def save_settings() -> None:
            endpoint = endpoint_var.get().strip().rstrip("/")
            hotkey = hotkey_var.get().strip().lower()
            voice = voice_var.get().strip()

            if not endpoint:
                messagebox.showerror("Kokoro Reader", "Server endpoint is required.")
                return
            if not hotkey:
                messagebox.showerror("Kokoro Reader", "Global hotkey is required.")
                return
            if not voice:
                messagebox.showerror("Kokoro Reader", "Voice is required.")
                return

            try:
                speed = float(speed_var.get().strip())
            except ValueError:
                messagebox.showerror("Kokoro Reader", "Speed must be a number.")
                return

            if not 0.5 <= speed <= 2.0:
                messagebox.showerror("Kokoro Reader", "Speed must be between 0.5 and 2.0.")
                return

            old_hotkey = self.config.hotkey
            self.config.endpoint = endpoint
            self.config.hotkey = hotkey
            self.config.voice = voice
            self.config.speed = speed

            try:
                self._register_hotkey()
            except Exception as exc:
                self.config.hotkey = old_hotkey
                self._register_hotkey()
                messagebox.showerror("Kokoro Reader", f"Hotkey error: {exc}")
                return

            self._save_config()
            server_status_var.set("running" if self._health_check() else "stopped")
            hotkey_status_var.set("active" if self.hotkey_active else "inactive")
            messagebox.showinfo("Kokoro Reader", "Settings saved.")

        def start_server() -> None:
            if (self.server_process is not None and self.server_process.poll() is None) or self._health_check():
                messagebox.showinfo("Kokoro Reader", "Server is already running.")
                server_status_var.set("running")
                return
            self._start_server()
            server_status_var.set("running" if self._health_check() else "stopped")

        def stop_server() -> None:
            if not messagebox.askyesno("Kokoro Reader", "Stop Kokoro server now?"):
                return
            self._stop_server()
            server_status_var.set("running" if self._health_check() else "stopped")

        ttk.Button(button_frame, text="Refresh Voices", command=refresh_voices).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(button_frame, text="Start Server", command=start_server).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(button_frame, text="Stop Server", command=stop_server).grid(row=0, column=2, padx=(0, 6))
        ttk.Button(button_frame, text="Speak Test", command=lambda: self.speak_text(sample_var.get())).grid(
            row=0,
            column=3,
            padx=(0, 6),
        )
        ttk.Button(button_frame, text="Save", command=save_settings).grid(row=0, column=4)

    def _show_settings(self, icon: Icon | None = None, item: MenuItem | None = None) -> None:
        self.root.after(0, self._open_settings_window)

    def _open_web_ui(self, icon: Icon | None = None, item: MenuItem | None = None) -> None:
        webbrowser.open(self.config.endpoint)

    def _speak_selected_menu(self, icon: Icon | None = None, item: MenuItem | None = None) -> None:
        threading.Thread(target=self.speak_selected_text, daemon=True).start()

    def _speak_clipboard_menu(self, icon: Icon | None = None, item: MenuItem | None = None) -> None:
        threading.Thread(target=self.speak_clipboard_text, daemon=True).start()

    def _start_server_menu(self, icon: Icon | None = None, item: MenuItem | None = None) -> None:
        threading.Thread(target=self._start_server, daemon=True).start()

    def _stop_server_menu(self, icon: Icon | None = None, item: MenuItem | None = None) -> None:
        self.root.after(0, self._confirm_stop_server_from_tray)

    def _confirm_stop_server_from_tray(self) -> None:
        if messagebox.askyesno("Kokoro Reader", "Stop Kokoro server now?"):
            threading.Thread(target=self._stop_server, daemon=True).start()

    def _quit(self, icon: Icon | None = None, item: MenuItem | None = None) -> None:
        self._stop_hotkey_listener()

        winsound.PlaySound(None, winsound.SND_PURGE)
        self._stop_server()
        self._cleanup_temp_files()

        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass

        self.root.after(0, self.root.quit)

    def run(self) -> None:
        self._fetch_voices()

        if self.config.voice not in self.voices:
            self.config.voice = self.voices[0]
            self._save_config()

        self._register_hotkey()
        logger.info("Hotkey listener active. Press %s to test.", self.config.hotkey)

        self._start_server(notify_when_already=False)

        menu = Menu(
            MenuItem("Speak Selected", self._speak_selected_menu),
            MenuItem("Speak Clipboard", self._speak_clipboard_menu),
            MenuItem("Start Server", self._start_server_menu),
            MenuItem("Stop Server", self._stop_server_menu),
            MenuItem("Settings", self._show_settings),
            MenuItem("Open Kokoro UI", self._open_web_ui),
            MenuItem("Quit", self._quit),
        )

        self.icon = Icon("kokoro-reader", self._build_icon_image(), "Kokoro Reader", menu)
        self.icon.run_detached()

        self._notify("Kokoro Reader", f"Ready. Use {self.config.hotkey} to read selected text.")
        self.root.mainloop()


def run_server_mode(host: str, port: int) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logger.info("Starting server mode on %s:%s", host, port)
    from server import app as fastapi_app

    uvicorn.run(fastapi_app, host=host, port=port, log_level="info")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kokoro Reader")
    parser.add_argument("--run-server", action="store_true", help="Run only Kokoro server")
    parser.add_argument("--host", default="127.0.0.1", help="Server host when using --run-server")
    parser.add_argument("--port", type=int, default=8000, help="Server port when using --run-server")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.run_server:
        run_server_mode(args.host, args.port)
        return

    app = KokoroReader()
    app.run()


if __name__ == "__main__":
    main()
