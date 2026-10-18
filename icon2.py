"""
Распознавание речи через OpenAI API (gpt-4o-mini-transcribe).
Push-to-talk по Left Alt или Left Ctrl + мониторинг папки для аудиофайлов.

Запуск:
    python3 -m pip install -r requirements.txt
    python3 icon2.py
"""

import asyncio
import fcntl
import io
import itertools
import json
import math
import queue
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque

import numpy as np
import sounddevice as sd
import soundfile as sf
from openai import (
    APIConnectionError, APIError, APIStatusError, APITimeoutError,
    AuthenticationError, OpenAI, RateLimitError,
)
from evdev import InputDevice, ecodes, list_devices
from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_property, method
from dbus_fast.service import signal as dbus_signal
from Xlib import X, Xatom
from Xlib import display as xdisplay
from Xlib.protocol import event as xevent

SAMPLE_RATE = 16000
CHANNELS = 1
MIN_DURATION_SECONDS = 1.0
TRANSCRIBE_QUEUE_SIZE = 8
MODEL = "gpt-4o-mini-transcribe"
MODEL_SETTING = "ALTALK_MODEL"
TRANSCRIBE_MODELS = ["gpt-4o-mini-transcribe", "gpt-4o-transcribe", "gpt-transcribe", "whisper-1"]
HOTKEY_SETTING = "ALTALK_HOTKEY"
HOTKEYS = {"alt": ("Left Alt", ecodes.KEY_LEFTALT), "ctrl": ("Left Ctrl", ecodes.KEY_LEFTCTRL)}
DEFAULT_HOTKEY = "alt"
PROMPT = "Задача в Jira. Деплой через docker compose. CI/CD pipeline, phpstan, cs-fixer, дейлик, services.yaml, deprecated."
APP_DIR = os.path.dirname(os.path.abspath(__file__))  # source checkout or the PyInstaller bundle
ASSETS_DIR = os.path.join(APP_DIR, "assets")
CONFIG_HOME = os.getenv("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
CONFIG_DIR = os.path.join(CONFIG_HOME, "altalk")
ENV_PATHS = [os.path.join(APP_DIR, ".env"), os.path.join(CONFIG_DIR, ".env")]
VOCABULARY_PATHS = [os.path.join(APP_DIR, "vocabulary.json"), os.path.join(CONFIG_DIR, "vocabulary.json")]
AUTOSTART_PATH = os.path.join(CONFIG_HOME, "autostart", "altalk.desktop")
LOG_PATH = os.path.join(
    os.getenv("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "altalk", "altalk.log"
)
ICON_PATH = os.path.join(ASSETS_DIR, "altalk-256.png")
# GTK 4 + libadwaita come with Ubuntu's GNOME, but only for the system Python.
SETTINGS_DIALOG = ["/usr/bin/python3", os.path.join(APP_DIR, "settings_dialog.py")]
SETTINGS_DIALOG_UNAVAILABLE = 3
TRAY_ICON_NAME = "altalk-tray"
COPIED_ICON_NAME = "altalk-copied"
FAILED_ICON_NAME = "altalk-failed"
BUSY_ICON_NAMES = ["altalk-busy-1", "altalk-busy-2", "altalk-busy-3"]  # typing dots, cycled
BUSY_FRAME_SECONDS = 0.4
COPIED_FLASH_SECONDS = 1.2
FAILED_FLASH_SECONDS = 3.0
HISTORY_SIZE = 5
HISTORY_PREVIEW_CHARS = 45
HISTORY_ICONS = {
    "done": "edit-copy-symbolic",
    "failed": "dialog-error-symbolic",
    "empty": "dialog-information-symbolic",
}
LOCK_PATH = os.path.join(os.getenv("XDG_RUNTIME_DIR", tempfile.gettempdir()), "altalk.lock")
SNI_WATCHER = "org.kde.StatusNotifierWatcher"
DESKTOP_ENTRY = """\
[Desktop Entry]
Type=Application
Name=Altalk
Comment=Hold Left Alt, speak, get the text in the clipboard
Exec={exec}
Icon=altalk
Terminal=false
Categories=Utility;
StartupNotify=false
StartupWMClass=Altalk
"""
DEFAULT_WATCH_DIRECTORY = "~/Downloads/icon_audio_inbox"
WATCH_DIRECTORIES = [os.getenv("ICON_WATCH_DIR", DEFAULT_WATCH_DIRECTORY)]
FILE_POLL_INTERVAL_SECONDS = 1.0
FILE_STABLE_PASSES = 1
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_CHUNK_DURATION_SECONDS = 480.0
CHUNK_OVERLAP_SECONDS = 4.0
TRANSCODE_BITRATE = "64k"
REQUEST_TIMEOUT_SECONDS = 120.0
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 1.0

CORRECTIONS = {
    r"\bджира\b": "Jira",
    r"\bдокер компоуз\b": "docker compose",
    r"\bдокер-компоуз\b": "docker compose",
    r"\bдокер\b": "docker",
    r"\bгит хаб\b": "GitHub",
    r"\bгитхаб\b": "GitHub",
    r"\bпайплайн\b": "pipeline",
    r"\bдевопс\b": "DevOps",
    r"\bдепрекейтед\b": "deprecated",
}

SUPPORTED_AUDIO_EXTENSIONS = {
    ".mp3",
    ".wav",
    ".m4a",
    ".flac",
    ".ogg",
    ".aac",
    ".webm",
}


def load_vocabulary(paths=VOCABULARY_PATHS):
    global PROMPT
    path = next((p for p in paths if os.path.isfile(p)), None)
    if path is None:
        return
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    PROMPT = data.get("prompt", PROMPT)
    CORRECTIONS.update(data.get("corrections", {}))


load_vocabulary()


def read_env_file(path):
    values = {}
    if os.path.isfile(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    values[key.strip()] = value.strip()
    return values


def read_settings():
    # The process environment is only a fallback; the first .env in ENV_PATHS wins over the rest.
    settings = {
        key: os.environ[key] for key in ("OPENAI_API_KEY", MODEL_SETTING, HOTKEY_SETTING) if os.environ.get(key)
    }
    for path in reversed(ENV_PATHS):
        settings.update(read_env_file(path))
    return settings


def read_hotkey():
    hotkey = read_settings().get(HOTKEY_SETTING, "").lower()
    return hotkey if hotkey in HOTKEYS else DEFAULT_HOTKEY


def settings_path():
    return next((p for p in ENV_PATHS if os.path.isfile(p)), ENV_PATHS[-1])


def settings_dialog_input():
    settings = read_settings()
    return json.dumps({
        "key": settings.get("OPENAI_API_KEY", ""),
        "model": settings.get(MODEL_SETTING) or MODEL,
        "models": TRANSCRIBE_MODELS,
        "hotkey": read_hotkey(),
        "hotkeys": [[hotkey, name] for hotkey, (name, _keycode) in HOTKEYS.items()],
        "autostart": autostart_enabled(),
        "path": settings_path().replace(os.path.expanduser("~"), "~", 1),
    }).encode()


def save_settings(values):
    # Update the .env the app actually reads; realpath keeps a symlinked .env a symlink.
    path = os.path.realpath(settings_path())
    lines = []
    if os.path.isfile(path):
        with open(path) as f:
            lines = f.read().splitlines()
    pending = dict(values)
    for i, line in enumerate(lines):
        key = line.partition("=")[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in pending:
            lines[i] = f"{key}={pending.pop(key)}"
    lines.extend(f"{key}={value}" for key, value in pending.items())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp_path, path)


def load_api_key():
    api_key = read_settings().get("OPENAI_API_KEY")
    if not api_key or api_key == "sk-your-key-here":
        raise RuntimeError(
            f"OPENAI_API_KEY not set. Put your key in {ENV_PATHS[-1]}:\n"
            "  OPENAI_API_KEY=sk-..."
        )
    return api_key


async def _notify(title, message):
    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    try:
        await bus.call(
            Message(
                destination="org.freedesktop.Notifications",
                path="/org/freedesktop/Notifications",
                interface="org.freedesktop.Notifications",
                member="Notify",
                signature="susssasa{sv}i",
                body=["Altalk", 0, ICON_PATH, title, message, [], {}, -1],
            )
        )
    finally:
        bus.disconnect()


def send_notification(title, message):
    try:
        asyncio.run(_notify(title, message))
    except Exception:
        pass


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")


def system_env():
    # A PyInstaller build puts its bundled libraries first in LD_LIBRARY_PATH;
    # system programs (ffmpeg, the GTK settings dialog) must get the original value.
    env = {key: value for key, value in os.environ.items() if not key.startswith("_PYI_")}
    if "LD_LIBRARY_PATH_ORIG" in env:
        env["LD_LIBRARY_PATH"] = env.pop("LD_LIBRARY_PATH_ORIG")
    elif getattr(sys, "frozen", False):
        env.pop("LD_LIBRARY_PATH", None)
    return env


class X11Clipboard:
    # Owns the CLIPBOARD selection the way xclip does; GNOME hands it over to Wayland apps.
    def __init__(self):
        self.display = xdisplay.Display()
        self.window = self.display.screen().root.create_window(0, 0, 1, 1, 0, X.CopyFromParent)
        self.selection = self.display.intern_atom("CLIPBOARD")
        self.targets = self.display.intern_atom("TARGETS")
        self.text_targets = [
            self.display.intern_atom("UTF8_STRING"),
            self.display.intern_atom("text/plain;charset=utf-8"),
        ]
        # Without the INCR protocol one property write must fit into a single X request.
        self.max_bytes = self.display.display.info.max_request_length * 4 - 64
        self.data = b""
        self.wakeup_read, self.wakeup_write = os.pipe()
        threading.Thread(target=self._serve, daemon=True).start()

    def copy(self, text):
        data = text.encode("utf-8")
        if len(data) > self.max_bytes:
            log(f"Text is too long for the clipboard ({len(data)} bytes), not copied")
            return
        self.data = data
        os.write(self.wakeup_write, b"\0")

    def _serve(self):
        # All X calls stay on this thread; copy() only wakes it up through the pipe.
        while True:
            readable, _, _ = select.select([self.display, self.wakeup_read], [], [])
            if self.wakeup_read in readable:
                os.read(self.wakeup_read, 64)
                self.window.set_selection_owner(self.selection, X.CurrentTime)
                self.display.flush()
            while self.display.pending_events():
                event = self.display.next_event()
                if event.type == X.SelectionRequest:
                    self._answer(event)

    def _answer(self, request):
        prop = request.property or request.target
        if request.target == self.targets:
            request.requestor.change_property(
                prop, Xatom.ATOM, 32, [self.targets, *self.text_targets]
            )
        elif request.target in self.text_targets:
            request.requestor.change_property(prop, request.target, 8, self.data)
        else:
            prop = X.NONE
        request.requestor.send_event(
            xevent.SelectionNotify(
                time=request.time,
                requestor=request.requestor,
                selection=request.selection,
                target=request.target,
                property=prop,
            )
        )
        self.display.flush()


_clipboard = None
_clipboard_lock = threading.Lock()


def copy_to_clipboard(text):
    global _clipboard
    with _clipboard_lock:
        if _clipboard is None:
            _clipboard = X11Clipboard()
    _clipboard.copy(text)


def apply_corrections(text: str) -> str:
    for pattern, replacement in CORRECTIONS.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


def get_audio_duration(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path,
        ],
        capture_output=True, text=True, check=True, env=system_env(),
    )
    return float(result.stdout.strip())


def transcode_for_upload(src, dst):
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", src, "-vn", "-ac", "1", "-ar", "16000",
            "-b:a", TRANSCODE_BITRATE, dst,
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        env=system_env(),
    )


def split_audio(src_mp3, duration, max_bytes, max_chunk_seconds, overlap, out_dir):
    size = os.path.getsize(src_mp3)
    chunks_by_size = math.ceil(size / max_bytes * 1.1)
    chunks_by_duration = math.ceil(duration / max_chunk_seconds)
    num_chunks = max(2, chunks_by_size, chunks_by_duration)
    base_chunk = duration / num_chunks
    chunks = []
    for i in range(num_chunks):
        start = max(0.0, i * base_chunk - (overlap if i > 0 else 0.0))
        if i == num_chunks - 1:
            length = max(0.1, duration - start + 1.0)
        else:
            length = base_chunk + (overlap if i > 0 else 0.0)
        chunk_path = os.path.join(out_dir, f"chunk_{i:03d}.mp3")
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{start:.3f}", "-t", f"{length:.3f}",
                "-i", src_mp3, "-c", "copy", chunk_path,
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            env=system_env(),
        )
        chunks.append(chunk_path)
    return chunks


def _normalize_word(word: str) -> str:
    return word.lower().strip(".,!?;:—–\"'()[]«»")


def _find_overlap_words(prev_tail_words, next_head_words, max_k=25, min_k=3):
    limit = min(len(prev_tail_words), len(next_head_words), max_k)
    for k in range(limit, min_k - 1, -1):
        if [_normalize_word(w) for w in prev_tail_words[-k:]] == [
            _normalize_word(w) for w in next_head_words[:k]
        ]:
            return k
    return 0


def stitch_transcripts(parts):
    cleaned = [p.strip() for p in parts if p and p.strip()]
    if not cleaned:
        return ""
    result_words = cleaned[0].split()
    for nxt in cleaned[1:]:
        nxt_words = nxt.split()
        tail = result_words[-60:]
        k = _find_overlap_words(tail, nxt_words)
        result_words.extend(nxt_words[k:])
    return " ".join(result_words)


class MissingApiKeyError(RuntimeError):
    pass


def is_retryable(error):
    # The same rule the OpenAI SDK uses for its own retries.
    if isinstance(error, APIConnectionError):
        return True
    if not isinstance(error, APIStatusError):
        return False
    should_retry = error.response.headers.get("x-should-retry")
    if should_retry in ("true", "false"):
        return should_retry == "true"
    return error.status_code in (408, 409, 429) or error.status_code >= 500


def describe_error(error):
    # A few words for the tray menu; the log keeps the full message.
    if isinstance(error, MissingApiKeyError):
        return "нет ключа OpenAI"
    if isinstance(error, APITimeoutError):
        return "таймаут"
    if isinstance(error, APIConnectionError):
        return "нет связи"
    if isinstance(error, AuthenticationError):
        return "неверный ключ"
    if isinstance(error, RateLimitError):
        return "нет денег на счёте" if error.code == "insufficient_quota" else "лимит запросов"
    if isinstance(error, APIStatusError):
        return f"OpenAI ответил {error.status_code}"
    return preview_text(str(error) or type(error).__name__, 30)


class Transcription:
    # A recording or audio file on its way through the queue; the tray menu shows its state.
    def __init__(self, source_type, source_data):
        self.source_type = source_type
        self.source_data = source_data
        self.stamp = time.strftime("%H:%M")
        self.state = "queued"  # queued → running → done | failed | empty
        self.attempt = 0
        self.error = ""
        self.text = ""


class OpenAITranscriber:
    def __init__(self, api_key, model=None):
        self.configure(api_key, model)

    def configure(self, api_key, model=None):
        # Also called by the tray after the settings window saved a new key or model.
        self.model = model or read_settings().get(MODEL_SETTING) or MODEL
        if not api_key:
            self.client = None
            log(f"OpenAI transcriber waiting for an API key (model: {self.model})")
            return
        # Retries are ours (see _request), not the SDK's, so the tray can show the attempt.
        self.client = OpenAI(
            api_key=api_key,
            timeout=REQUEST_TIMEOUT_SECONDS,
            max_retries=0,
        )
        log(
            f"OpenAI transcriber ready (model: {self.model}, "
            f"timeout: {REQUEST_TIMEOUT_SECONDS:.0f}s, retries: {MAX_RETRIES})"
        )

    def _require_client(self):
        if self.client is None:
            log("OPENAI_API_KEY not set, skipping transcription")
            send_notification("Altalk", "Нет ключа OpenAI — открой «Настройки…» в меню трея")
            raise MissingApiKeyError("OPENAI_API_KEY not set")

    def _audio_to_wav_bytes(self, audio_array):
        buf = io.BytesIO()
        sf.write(buf, audio_array, SAMPLE_RATE, format="WAV", subtype="PCM_16")
        buf.seek(0)
        buf.name = "audio.wav"
        return buf

    def _request(self, audio_file, on_attempt=None):
        # on_attempt(attempt, previous_error) runs before every request, starting from 1.
        error = None
        for attempt in range(1, MAX_RETRIES + 2):
            if on_attempt:
                on_attempt(attempt, error)
            audio_file.seek(0)
            started = time.monotonic()
            try:
                response = self.client.audio.transcriptions.create(
                    model=self.model,
                    file=audio_file,
                    language="ru",
                    prompt=PROMPT,
                )
            except APIError as e:
                if attempt > MAX_RETRIES or not is_retryable(e):
                    raise
                error = e
                delay = RETRY_DELAY_SECONDS * 2 ** (attempt - 1)
                log(
                    f"  Attempt {attempt} failed after {time.monotonic() - started:.1f}s: {e}; "
                    f"retrying in {delay:.0f}s"
                )
                time.sleep(delay)
                continue
            log(f"  API call took {time.monotonic() - started:.1f}s")
            return response.text

    def transcribe_array(self, audio_data, on_attempt=None):
        self._require_client()
        text = self._request(self._audio_to_wav_bytes(audio_data), on_attempt)
        return apply_corrections(text.strip())

    def _transcribe_path(self, path, on_attempt=None):
        with open(path, "rb") as f:
            return self._request(f, on_attempt)

    def transcribe_file(self, audio_path, on_attempt=None):
        self._require_client()
        size_mb = os.path.getsize(audio_path) / (1024 * 1024)
        log(f"Processing {audio_path} ({size_mb:.1f} MB)")
        with tempfile.TemporaryDirectory(prefix="icon2_") as tmpdir:
            transcoded = os.path.join(tmpdir, "audio.mp3")
            transcode_for_upload(audio_path, transcoded)
            transcoded_size = os.path.getsize(transcoded)
            log(f"Transcoded to mono 16kHz mp3: {transcoded_size / (1024*1024):.1f} MB")

            duration = get_audio_duration(transcoded)
            log(f"Duration: {duration:.1f}s")

            if (
                transcoded_size <= MAX_UPLOAD_BYTES
                and duration <= MAX_CHUNK_DURATION_SECONDS
            ):
                log("Single-request upload")
                return apply_corrections(self._transcribe_path(transcoded, on_attempt).strip())

            chunks = split_audio(
                transcoded, duration, MAX_UPLOAD_BYTES,
                MAX_CHUNK_DURATION_SECONDS, CHUNK_OVERLAP_SECONDS, tmpdir,
            )
            log(
                f"Split into {len(chunks)} chunks "
                f"(overlap {CHUNK_OVERLAP_SECONDS:.0f}s)"
            )
            parts = []
            for idx, chunk in enumerate(chunks, 1):
                chunk_mb = os.path.getsize(chunk) / (1024 * 1024)
                log(f"Chunk {idx}/{len(chunks)} ({chunk_mb:.1f} MB)")
                parts.append(self._transcribe_path(chunk, on_attempt))
            stitched = stitch_transcripts(parts)
            return apply_corrections(stitched.strip())


class PushToTalkRecorder:
    def __init__(self, submit, hotkey):
        self.submit = submit
        self.recording = False
        self.aborted = False
        self.audio_buffer = []
        self.buffer_lock = threading.Lock()
        self.set_hotkey(hotkey)

    def set_hotkey(self, hotkey):
        # Also called by the tray after the settings window saved another key.
        self.key_name, self.keycode = HOTKEYS[hotkey]
        self.recording = False

    def callback(self, indata, frames, time_info, status):
        if status:
            log(status)
        if self.recording and not self.aborted:
            with self.buffer_lock:
                self.audio_buffer.append(indata.copy())

    def record_audio(self):
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            callback=self.callback,
            blocksize=1024,
        ):
            while True:
                sd.sleep(100)

    def flush_recording(self):
        with self.buffer_lock:
            chunks = self.audio_buffer
            self.audio_buffer = []

        if not chunks:
            return

        audio_data = np.concatenate(chunks, axis=0).reshape(-1).astype(np.float32)
        duration = len(audio_data) / SAMPLE_RATE
        log(f"Recording duration: {duration:.2f} seconds")

        if duration < MIN_DURATION_SECONDS:
            log("Recording duration less than 1 second. Discarding recording.")
            return

        # Checked here, not in the worker: no need to wait behind a slow request to say so.
        rms = np.sqrt(np.mean(audio_data ** 2))
        if rms < 0.0025:
            log(f"Recording too quiet (rms={rms:.4f}). Skipping.")
            send_notification("Altalk", "Слишком тихо — проверьте микрофон/громкость")
            return

        try:
            self.submit(Transcription("array", audio_data))
        except queue.Full:
            log("Transcription queue is full. Dropping recording.")

    def on_press(self, keycode):
        if keycode == self.keycode:
            if not self.recording:
                self.recording = True
                self.aborted = False
                with self.buffer_lock:
                    self.audio_buffer = []
                log("Recording started")
        else:
            self.cancel(f"Other key pressed (code {keycode})")

    def cancel(self, reason):
        # The hotkey was used as a modifier (Ctrl+C, Alt+Tab, Ctrl+click), not to talk.
        if self.recording and not self.aborted:
            self.aborted = True
            with self.buffer_lock:
                self.audio_buffer = []
            log(f"{reason} — recording aborted")

    def on_release(self, keycode):
        if keycode == self.keycode and self.recording:
            self.recording = False
            if self.aborted:
                log(f"Recording discarded (other keys were pressed while holding {self.key_name})")
                with self.buffer_lock:
                    self.audio_buffer = []
            else:
                log("Recording stopped")
                threading.Thread(target=self.flush_recording, daemon=True).start()


def find_input_devices():
    # Keyboards for the hotkey; mice and touchpads so that a click or a scroll cancels the recording.
    devices = []
    for path in list_devices():
        try:
            d = InputDevice(path)
        except (OSError, PermissionError):
            continue
        keys = d.capabilities().get(ecodes.EV_KEY, [])
        if any(code in keys for code in (ecodes.KEY_LEFTALT, ecodes.KEY_LEFTCTRL, ecodes.BTN_LEFT)):
            devices.append(d)
    return devices


def listen_device(device, recorder):
    try:
        for event in device.read_loop():
            if event.type == ecodes.EV_REL and event.code in (ecodes.REL_WHEEL, ecodes.REL_HWHEEL):
                recorder.cancel("Mouse wheel scrolled")
            if event.type != ecodes.EV_KEY:
                continue
            if ecodes.BTN_DIGI <= event.code <= ecodes.BTN_TOOL_QUADTAP:
                continue  # a finger touching the touchpad, not a click
            if event.value == 1:
                recorder.on_press(event.code)
            elif event.value == 0:
                recorder.on_release(event.code)
    except OSError as e:
        log(f"Input listener for {device.path} stopped: {e}")
    finally:
        try:
            device.close()
        except Exception:
            pass


INPUT_RESCAN_INTERVAL_SECONDS = 2.0


def input_supervisor(recorder):
    active = {}
    while True:
        for path in list(active):
            thread, _ = active[path]
            if not thread.is_alive():
                del active[path]
                log(f"Input listener for {path} gone, will rescan")

        try:
            current = find_input_devices()
        except Exception as e:
            log(f"Input rescan error: {e}")
            current = []

        for device in current:
            if device.path in active:
                try:
                    device.close()
                except Exception:
                    pass
                continue
            t = threading.Thread(
                target=listen_device, args=(device, recorder), daemon=True
            )
            t.start()
            active[device.path] = (t, device)
            log(f"Listening on {device.path} ({device.name})")

        time.sleep(INPUT_RESCAN_INTERVAL_SECONDS)


class DirectoryAudioEnqueuer:
    def __init__(self, submit, watch_directories=None):
        self.submit = submit
        directories = watch_directories or WATCH_DIRECTORIES
        self.watch_directories = [
            os.path.abspath(os.path.expanduser(path)) for path in directories
        ]
        self.file_sizes = {}
        self.stable_pass_counts = {}
        self.processed_signatures = set()
        self._ensure_directories_exist()
        self._prime_existing_files()

    def run(self):
        watched = ", ".join(self.watch_directories)
        log(f"Watching directories for new audio: {watched}")
        while True:
            try:
                self._enqueue_new_files()
            except Exception as e:
                log(f"Directory watcher error: {e}")
            time.sleep(FILE_POLL_INTERVAL_SECONDS)

    def _ensure_directories_exist(self):
        for directory in self.watch_directories:
            os.makedirs(directory, exist_ok=True)

    def _prime_existing_files(self):
        for directory in self.watch_directories:
            if not os.path.isdir(directory):
                continue
            for entry in os.scandir(directory):
                if not entry.is_file():
                    continue
                extension = os.path.splitext(entry.path)[1].lower()
                if extension not in SUPPORTED_AUDIO_EXTENSIONS:
                    continue
                file_stat = entry.stat()
                signature = (entry.path, file_stat.st_mtime_ns, file_stat.st_size)
                self.processed_signatures.add(signature)

    def _enqueue_new_files(self):
        for directory in self.watch_directories:
            if not os.path.isdir(directory):
                continue

            for entry in os.scandir(directory):
                if not entry.is_file():
                    continue

                audio_path = entry.path
                extension = os.path.splitext(audio_path)[1].lower()
                if extension not in SUPPORTED_AUDIO_EXTENSIONS:
                    continue

                file_stat = entry.stat()
                signature = (audio_path, file_stat.st_mtime_ns, file_stat.st_size)
                if signature in self.processed_signatures:
                    continue

                current_size = file_stat.st_size
                previous_size = self.file_sizes.get(audio_path)
                if previous_size != current_size:
                    self.file_sizes[audio_path] = current_size
                    self.stable_pass_counts[audio_path] = 0
                    continue

                self.stable_pass_counts[audio_path] = (
                    self.stable_pass_counts.get(audio_path, 0) + 1
                )
                if self.stable_pass_counts[audio_path] < FILE_STABLE_PASSES:
                    continue

                try:
                    self.submit(Transcription("file", audio_path))
                    self.processed_signatures.add(signature)
                    self.file_sizes.pop(audio_path, None)
                    self.stable_pass_counts.pop(audio_path, None)
                    log(f"Queued file for transcription: {audio_path}")
                except queue.Full:
                    log("Transcription queue is full. Dropping file path.")


def transcription_worker(transcriber, in_queue, on_update=lambda job: None):
    # on_update(job) runs in this thread after every change of job.state or job.attempt.
    while True:
        job = in_queue.get()

        def on_attempt(attempt, error):
            job.attempt = attempt
            job.error = describe_error(error) if error else ""
            on_update(job)

        try:
            job.state, job.attempt = "running", 0
            on_update(job)
            if job.source_type == "array":
                text = transcriber.transcribe_array(job.source_data, on_attempt)
            else:
                text = transcriber.transcribe_file(job.source_data, on_attempt)

            if text:
                job.state, job.text = "done", text
                on_update(job)
                copy_to_clipboard(text)
                if job.source_type == "file":
                    txt_path = os.path.splitext(job.source_data)[0] + ".txt"
                    with open(txt_path, "w", encoding="utf-8") as f:
                        f.write(text)
                    log(f"Transcription saved: {txt_path}")
                log("Transcribed text copied to clipboard.")
                stripped = text.strip()
                preview = stripped[:40] + "..." if len(stripped) > 40 else stripped
                send_notification("Transcription Complete", preview)
            else:
                job.state = "empty"
                on_update(job)
                log("Transcription returned empty text.")
        except Exception as e:
            log(f"Transcription failed: {e}")
            if job.state != "done":
                job.state, job.error = "failed", describe_error(e)
                on_update(job)
        finally:
            in_queue.task_done()


def job_status(job):
    # The part of a tray history entry after the time.
    if job.state == "done":
        return preview_text(job.text)
    if job.state == "failed":
        status = f"Ошибка: {job.error} — повторить"
    elif job.state == "empty":
        status = "ничего не распознано"
    elif job.state == "queued":
        status = "в очереди"
    elif job.attempt > 1:
        status = f"попытка {job.attempt} из {MAX_RETRIES + 1}: {job.error}"
    else:
        status = "распознаю…"
    if job.source_type == "file":
        return f"{os.path.basename(job.source_data)} — {status}"
    return status


def preview_text(text, limit=HISTORY_PREVIEW_CHARS):
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[:limit].rsplit(" ", 1)[0].rstrip(".,;:—–-") + "…"


class StatusNotifierItem(ServiceInterface):
    # The tray icon itself (StatusNotifierItem spec); GNOME shows it via the AppIndicator extension.
    def __init__(self):
        super().__init__("org.kde.StatusNotifierItem")
        self.icon_name = TRAY_ICON_NAME
        self.hotkey_name = HOTKEYS[read_hotkey()][0]

    @dbus_property(access=PropertyAccess.READ)
    def Category(self) -> "s":
        return "ApplicationStatus"

    @dbus_property(access=PropertyAccess.READ)
    def Id(self) -> "s":
        return "altalk"

    @dbus_property(access=PropertyAccess.READ)
    def Title(self) -> "s":
        return "Altalk"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "Active"

    @dbus_property(access=PropertyAccess.READ)
    def IconName(self) -> "s":
        return self.icon_name

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> "s":
        return ASSETS_DIR

    @dbus_property(access=PropertyAccess.READ)
    def ToolTip(self) -> "(sa(iiay)ss)":
        return ["", [], "Altalk", f"Зажми {self.hotkey_name} и говори"]

    @dbus_property(access=PropertyAccess.READ)
    def ItemIsMenu(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def Menu(self) -> "o":
        return "/MenuBar"

    @method()
    def Activate(self, x: "i", y: "i"):
        pass

    @method()
    def SecondaryActivate(self, x: "i", y: "i"):
        pass

    @method()
    def ContextMenu(self, x: "i", y: "i"):
        pass

    @method()
    def Scroll(self, delta: "i", orientation: "s"):
        pass

    @dbus_signal()
    def NewIcon(self):
        pass

    @dbus_signal()
    def NewToolTip(self):
        pass


class DBusMenu(ServiceInterface):
    # The tray menu (com.canonical.dbusmenu); the tray host renders it, items come from TrayIcon.
    def __init__(self, tray):
        super().__init__("com.canonical.dbusmenu")
        self.tray = tray
        self.revision = 1

    @dbus_property(access=PropertyAccess.READ)
    def Version(self) -> "u":
        return 3

    @dbus_property(access=PropertyAccess.READ)
    def TextDirection(self) -> "s":
        return "ltr"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "normal"

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> "as":
        return []

    @method()
    def GetLayout(self, parent_id: "i", recursion_depth: "i", property_names: "as") -> "u(ia{sv}av)":
        items = self.tray.menu_items()
        if parent_id != 0:
            return [self.revision, [parent_id, dict(items).get(parent_id, {}), []]]
        children = [Variant("(ia{sv}av)", [item_id, props, []]) for item_id, props in items]
        return [self.revision, [0, {"children-display": Variant("s", "submenu")}, children]]

    @method()
    def GetGroupProperties(self, ids: "ai", property_names: "as") -> "a(ia{sv})":
        items = dict(self.tray.menu_items())
        return [[item_id, items[item_id]] for item_id in ids if item_id in items]

    @method()
    def GetProperty(self, item_id: "i", name: "s") -> "v":
        return dict(self.tray.menu_items()).get(item_id, {}).get(name, Variant("s", ""))

    @method()
    def Event(self, item_id: "i", event_id: "s", data: "v", timestamp: "u"):
        if event_id == "clicked":
            self.tray.on_click(item_id)

    @method()
    def EventGroup(self, events: "a(isvu)") -> "ai":
        for item_id, event_id, _data, _timestamp in events:
            if event_id == "clicked":
                self.tray.on_click(item_id)
        return []

    @method()
    def AboutToShow(self, item_id: "i") -> "b":
        return False

    @method()
    def AboutToShowGroup(self, ids: "ai") -> "aiai":
        return [[], []]

    @dbus_signal()
    def LayoutUpdated(self) -> "ui":
        return [self.revision, 0]


class TrayIcon:
    QUIT_ID = 1
    EMPTY_ID = 2
    SEPARATOR_ID = 3
    SETTINGS_ID = 4

    def __init__(self, transcribe_queue, on_settings_saved=None):
        self.transcribe_queue = transcribe_queue
        self.history = deque(maxlen=HISTORY_SIZE)
        self.history_lock = threading.Lock()
        self.next_entry_id = 100
        self.item = StatusNotifierItem()
        self.menu = DBusMenu(self)
        self.loop = None
        self.quit_event = None
        self.restore_icon_handle = None
        self.busy_task = None
        self.on_settings_saved = on_settings_saved
        self.settings_task = None
        self.settings_process = None

    def submit(self, job):
        # Queues a new recording or file and lists it; raises queue.Full like put_nowait.
        self.transcribe_queue.put_nowait(job)
        with self.history_lock:
            self.history.appendleft((self.next_entry_id, job))
            self.next_entry_id += 1
        self.update(job)

    def update(self, job):
        # Called from worker threads; D-Bus objects live on the asyncio loop.
        if self.loop:
            self.loop.call_soon_threadsafe(self._job_changed, job.state)

    def _job_changed(self, state):
        self._layout_changed()
        if state == "done":
            self._flash(COPIED_ICON_NAME, COPIED_FLASH_SECONDS)
        elif state == "failed":
            self._flash(FAILED_ICON_NAME, FAILED_FLASH_SECONDS)
        else:
            self._refresh_icon()

    def _retry(self, job):
        job.state = "queued"
        try:
            self.transcribe_queue.put_nowait(job)
        except queue.Full:
            job.state = "failed"
            log("Transcription queue is full. Not retrying.")
            return
        log(f"Retrying transcription from {job.stamp}")
        self._job_changed(job.state)

    def _layout_changed(self):
        self.menu.revision += 1
        self.menu.LayoutUpdated()

    def menu_items(self):
        with self.history_lock:
            history = list(self.history)
        items = []
        if not history:
            items.append(
                (self.EMPTY_ID, {
                    "label": Variant("s", "Пока нет транскрипций"),
                    "enabled": Variant("b", False),
                })
            )
        for entry_id, job in history:
            # DBusMenu treats "_" as a mnemonic marker; "__" is a literal underscore.
            label = f"{job.stamp}  {job_status(job)}".replace("_", "__")
            items.append(
                (entry_id, {
                    "label": Variant("s", label),
                    "icon-name": Variant("s", HISTORY_ICONS.get(job.state, "content-loading-symbolic")),
                    # Done copies the text again, failed retries; the rest is still in work.
                    "enabled": Variant("b", job.state in ("done", "failed")),
                })
            )
        items.append((self.SEPARATOR_ID, {"type": Variant("s", "separator")}))
        items.append((self.SETTINGS_ID, {"label": Variant("s", "Настройки…")}))
        items.append((self.QUIT_ID, {"label": Variant("s", "Выйти")}))
        return items

    def on_click(self, item_id):
        if item_id == self.QUIT_ID:
            self.quit_event.set()
            return
        if item_id == self.SETTINGS_ID:
            self.open_settings()
            return
        with self.history_lock:
            job = next((j for entry_id, j in self.history if entry_id == item_id), None)
        if job and job.state == "failed":
            self._retry(job)
            return
        if not job or job.state != "done":
            return
        text = job.text
        copy_to_clipboard(text)
        log(f"Copied from history: {preview_text(text)}")
        self._flash(COPIED_ICON_NAME, COPIED_FLASH_SECONDS)

    def _flash(self, name, seconds):
        # Shown for a moment instead of the busy or idle icon.
        self._stop_busy_animation()
        if self.restore_icon_handle:
            self.restore_icon_handle.cancel()
        self._set_icon(name)
        self.restore_icon_handle = self.loop.call_later(seconds, self._end_flash)

    def _end_flash(self):
        self.restore_icon_handle = None
        self._refresh_icon()

    def _refresh_icon(self):
        # Busy while anything is queued or being transcribed, idle (ready) otherwise.
        if self.restore_icon_handle:
            return  # _end_flash comes back here
        with self.history_lock:
            busy = any(job.state in ("queued", "running") for _, job in self.history)
        if busy and not self.busy_task:
            self.busy_task = self.loop.create_task(self._animate_busy())
        elif not busy:
            self._stop_busy_animation()
            self._set_icon(TRAY_ICON_NAME)

    def _stop_busy_animation(self):
        if self.busy_task:
            self.busy_task.cancel()
            self.busy_task = None

    async def _animate_busy(self):
        for name in itertools.cycle(BUSY_ICON_NAMES):
            self._set_icon(name)
            await asyncio.sleep(BUSY_FRAME_SECONDS)

    def _set_icon(self, name):
        if self.item.icon_name != name:
            self.item.icon_name = name
            self.item.NewIcon()

    def open_settings(self):
        if self.settings_task and not self.settings_task.done():
            return
        self.settings_task = self.loop.create_task(self._run_settings())

    async def _run_settings(self):
        # settings_dialog.py gets the current values on stdin and prints the saved ones.
        try:
            self.settings_process = await asyncio.create_subprocess_exec(
                *SETTINGS_DIALOG,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                # Local-only GIO: GTK otherwise calls gvfsd on startup and waits out
                # its 25 s D-Bus timeout whenever gvfsd hangs (e.g. on a flaky USB disk).
                # Software rendering: a two-row window gains nothing from OpenGL, and a
                # broken GL driver would otherwise crash or hang it.
                env={**system_env(), "GIO_USE_VFS": "local", "GSK_RENDERER": "cairo"},
            )
        except OSError:
            code, output = SETTINGS_DIALOG_UNAVAILABLE, b""
        else:
            output, _ = await self.settings_process.communicate(settings_dialog_input())
            code = self.settings_process.returncode
            self.settings_process = None
        if code == 0:
            values = json.loads(output)
            save_settings({
                "OPENAI_API_KEY": values["key"],
                MODEL_SETTING: values["model"],
                HOTKEY_SETTING: values["hotkey"],
            })
            log(f"Settings saved to {settings_path()}")
            set_autostart(values["autostart"])
            if self.on_settings_saved:
                self.on_settings_saved()
            self.item.hotkey_name = HOTKEYS[values["hotkey"]][0]
            self.item.NewToolTip()
        elif code == SETTINGS_DIALOG_UNAVAILABLE:
            log("Settings dialog needs /usr/bin/python3 with python3-gi and gir1.2-adw-1")
            try:
                await _notify("Altalk", f"Для окна настроек нужен GTK 4 (gir1.2-adw-1). Ключ можно вписать в {settings_path()}")
            except Exception:
                pass

    async def run(self, open_settings=False):
        self.loop = asyncio.get_running_loop()
        self.quit_event = asyncio.Event()
        try:
            bus = await MessageBus(bus_type=BusType.SESSION).connect()
            bus.export("/StatusNotifierItem", self.item)
            bus.export("/MenuBar", self.menu)
            name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
            await bus.request_name(name)

            def on_message(message):
                # The tray host restarted (e.g. GNOME Shell reloaded the extension): register again.
                if message.member == "NameOwnerChanged" and message.body[0] == SNI_WATCHER and message.body[2]:
                    self.loop.create_task(self._register(bus, name))

            bus.add_message_handler(on_message)
            await bus.call(
                Message(
                    destination="org.freedesktop.DBus",
                    path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus",
                    member="AddMatch",
                    signature="s",
                    body=[f"type='signal',member='NameOwnerChanged',arg0='{SNI_WATCHER}'"],
                )
            )
            await self._register(bus, name)
        except Exception as e:
            log(f"Tray icon unavailable: {e}")
        if open_settings:
            self.open_settings()
        await self.quit_event.wait()
        if self.settings_process and self.settings_process.returncode is None:
            self.settings_process.terminate()

    async def _register(self, bus, name):
        reply = await bus.call(
            Message(
                destination=SNI_WATCHER,
                path="/StatusNotifierWatcher",
                interface=SNI_WATCHER,
                member="RegisterStatusNotifierItem",
                signature="s",
                body=[name],
            )
        )
        if reply.message_type == MessageType.ERROR:
            log(f"No tray host ({reply.error_name}); on GNOME enable the AppIndicator extension")
        else:
            log("Tray icon registered")


def app_command():
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, os.path.abspath(__file__)]


def desktop_entry():
    return DESKTOP_ENTRY.format(exec=" ".join(app_command()))


def autostart_enabled():
    # Session managers skip an entry that is hidden or switched off (e.g. by GNOME Tweaks).
    if not os.path.isfile(AUTOSTART_PATH):
        return False
    entry = read_env_file(AUTOSTART_PATH)
    return entry.get("Hidden") != "true" and entry.get("X-GNOME-Autostart-enabled") != "false"


def set_autostart(enabled):
    # XDG autostart: GNOME, KDE and the rest start ~/.config/autostart/*.desktop at login.
    if enabled == autostart_enabled():
        return
    if enabled:
        os.makedirs(os.path.dirname(AUTOSTART_PATH), exist_ok=True)
        with open(AUTOSTART_PATH, "w") as f:
            f.write(desktop_entry())
        log(f"Autostart enabled: {AUTOSTART_PATH}")
    else:
        os.remove(AUTOSTART_PATH)
        log("Autostart disabled")


def install_desktop_entry():
    data_home = os.getenv("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    icon_dir = os.path.join(data_home, "icons", "hicolor", "256x256", "apps")
    apps_dir = os.path.join(data_home, "applications")
    os.makedirs(icon_dir, exist_ok=True)
    os.makedirs(apps_dir, exist_ok=True)
    shutil.copyfile(ICON_PATH, os.path.join(icon_dir, "altalk.png"))
    entry_path = os.path.join(apps_dir, "altalk.desktop")
    with open(entry_path, "w") as f:
        f.write(desktop_entry())
    print(f"Installed {entry_path}")


def redirect_output_to_log():
    # Started from the app menu there is no terminal, so keep the log in the XDG state dir.
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    log_file = open(LOG_PATH, "a")
    os.dup2(log_file.fileno(), sys.stdout.fileno())
    os.dup2(log_file.fileno(), sys.stderr.fileno())
    sys.stdout.reconfigure(line_buffering=True)


def acquire_instance_lock():
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        send_notification("Altalk", "Уже запущен — иконка в трее")
        raise SystemExit("Altalk is already running")
    return lock_file


def main():
    if sys.argv[1:] == ["--install"]:
        install_desktop_entry()
        return
    if getattr(sys, "frozen", False) and not sys.stdout.isatty():
        redirect_output_to_log()

    instance_lock = acquire_instance_lock()  # held until the process exits
    try:
        api_key = load_api_key()
    except RuntimeError:
        api_key = None
        log("OPENAI_API_KEY not set: opening settings")
    transcriber = OpenAITranscriber(api_key)
    transcribe_queue = queue.Queue(maxsize=TRANSCRIBE_QUEUE_SIZE)

    def reload_settings():
        try:
            transcriber.configure(load_api_key())
        except RuntimeError:
            transcriber.configure(None)
        recorder.set_hotkey(read_hotkey())

    tray = TrayIcon(transcribe_queue, on_settings_saved=reload_settings)
    recorder = PushToTalkRecorder(tray.submit, read_hotkey())

    threading.Thread(
        target=transcription_worker,
        args=(transcriber, transcribe_queue, tray.update),
        daemon=True,
    ).start()

    threading.Thread(target=recorder.record_audio, daemon=True).start()
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        file_enqueuer = DirectoryAudioEnqueuer(tray.submit)
        threading.Thread(target=file_enqueuer.run, daemon=True).start()
        log(f"To add external audio while running, put file into: {DEFAULT_WATCH_DIRECTORY}")
    else:
        log("ffmpeg/ffprobe not found: audio-file inbox disabled")

    initial = find_input_devices()
    if not initial:
        send_notification(
            "Altalk", "Нет доступа к клавиатуре: sudo usermod -aG input $USER и перелогиниться"
        )
        raise SystemExit(
            "No accessible keyboard devices found in /dev/input/. "
            "Ensure your user is in the 'input' group: sudo usermod -aG input $USER"
        )
    for d in initial:
        try:
            d.close()
        except Exception:
            pass

    threading.Thread(target=input_supervisor, args=(recorder,), daemon=True).start()
    log(f"Ready. Hold {recorder.key_name} to record.")
    asyncio.run(tray.run(open_settings=api_key is None))
    log("Quit from tray")
    instance_lock.close()


if __name__ == "__main__":
    main()
