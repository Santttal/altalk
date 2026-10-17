"""
Распознавание речи через OpenAI API (gpt-4o-mini-transcribe).
Push-to-talk по Left Alt + мониторинг папки для аудиофайлов.

Запуск:
    python3 -m pip install -r requirements.txt
    python3 icon2.py
"""

import asyncio
import fcntl
import io
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
from openai import APIConnectionError, AuthenticationError, NotFoundError, OpenAI, PermissionDeniedError
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
PROMPT = "Задача в Jira. Деплой через docker compose. CI/CD pipeline, phpstan, cs-fixer, дейлик, services.yaml, deprecated."
APP_DIR = os.path.dirname(os.path.abspath(__file__))  # source checkout or the PyInstaller bundle
ASSETS_DIR = os.path.join(APP_DIR, "assets")
CONFIG_DIR = os.path.join(os.getenv("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "altalk")
ENV_PATHS = [os.path.join(APP_DIR, ".env"), os.path.join(CONFIG_DIR, ".env")]
VOCABULARY_PATHS = [os.path.join(APP_DIR, "vocabulary.json"), os.path.join(CONFIG_DIR, "vocabulary.json")]
LOG_PATH = os.path.join(
    os.getenv("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "altalk", "altalk.log"
)
ICON_PATH = os.path.join(ASSETS_DIR, "altalk-256.png")
TRAY_ICON_NAME = "altalk-tray"
COPIED_ICON_NAME = "altalk-copied"
COPIED_FLASH_SECONDS = 1.2
HISTORY_SIZE = 5
HISTORY_PREVIEW_CHARS = 45
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
        key: os.environ[key] for key in ("OPENAI_API_KEY", MODEL_SETTING) if os.environ.get(key)
    }
    for path in reversed(ENV_PATHS):
        settings.update(read_env_file(path))
    return settings


def save_settings(values):
    # Update the .env the app actually reads; realpath keeps a symlinked .env a symlink.
    path = os.path.realpath(next((p for p in ENV_PATHS if os.path.isfile(p)), ENV_PATHS[-1]))
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
        capture_output=True, text=True, check=True,
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
        self.client = OpenAI(
            api_key=api_key,
            timeout=REQUEST_TIMEOUT_SECONDS,
            max_retries=MAX_RETRIES,
        )
        log(
            f"OpenAI transcriber ready (model: {self.model}, "
            f"timeout: {REQUEST_TIMEOUT_SECONDS:.0f}s, retries: {MAX_RETRIES})"
        )

    def _has_client(self):
        if self.client is None:
            log("OPENAI_API_KEY not set, skipping transcription")
            send_notification("Altalk", "Нет ключа OpenAI — открой «Настройки…» в меню трея")
        return self.client is not None

    def _audio_to_wav_bytes(self, audio_array):
        buf = io.BytesIO()
        sf.write(buf, audio_array, SAMPLE_RATE, format="WAV", subtype="PCM_16")
        buf.seek(0)
        buf.name = "audio.wav"
        return buf

    def transcribe_array(self, audio_data):
        rms = np.sqrt(np.mean(audio_data ** 2))
        if rms < 0.0025:
            log(f"Recording too quiet (rms={rms:.4f}). Skipping.")
            send_notification("Altalk", "Слишком тихо — проверьте микрофон/громкость")
            return ""
        if not self._has_client():
            return ""

        wav_buf = self._audio_to_wav_bytes(audio_data)
        started = time.monotonic()
        response = self.client.audio.transcriptions.create(
            model=self.model,
            file=wav_buf,
            language="ru",
            prompt=PROMPT,
        )
        log(f"  API call took {time.monotonic() - started:.1f}s")
        return apply_corrections(response.text.strip())

    def _transcribe_path(self, path):
        started = time.monotonic()
        with open(path, "rb") as f:
            response = self.client.audio.transcriptions.create(
                model=self.model,
                file=f,
                language="ru",
                prompt=PROMPT,
            )
        log(f"  API call took {time.monotonic() - started:.1f}s")
        return response.text

    def transcribe_file(self, audio_path):
        if not self._has_client():
            return ""
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
                return apply_corrections(self._transcribe_path(transcoded).strip())

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
                parts.append(self._transcribe_path(chunk))
            stitched = stitch_transcripts(parts)
            return apply_corrections(stitched.strip())


class PushToTalkRecorder:
    def __init__(self, out_queue):
        self.out_queue = out_queue
        self.recording = False
        self.aborted = False
        self.audio_buffer = []
        self.buffer_lock = threading.Lock()

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

        try:
            self.out_queue.put_nowait(("array", audio_data))
        except queue.Full:
            log("Transcription queue is full. Dropping recording.")

    def on_press(self, keycode):
        if keycode == ecodes.KEY_LEFTALT:
            if not self.recording:
                self.recording = True
                self.aborted = False
                with self.buffer_lock:
                    self.audio_buffer = []
                log("Recording started")
        elif self.recording and not self.aborted:
            self.aborted = True
            with self.buffer_lock:
                self.audio_buffer = []
            log(f"Other key pressed (code {keycode}) — recording aborted")

    def on_release(self, keycode):
        if keycode == ecodes.KEY_LEFTALT and self.recording:
            self.recording = False
            if self.aborted:
                log("Recording discarded (other keys were pressed while holding Alt)")
                with self.buffer_lock:
                    self.audio_buffer = []
            else:
                log("Recording stopped")
                threading.Thread(target=self.flush_recording, daemon=True).start()


def find_keyboard_devices():
    devices = []
    for path in list_devices():
        try:
            d = InputDevice(path)
        except (OSError, PermissionError):
            continue
        caps = d.capabilities()
        if ecodes.EV_KEY in caps and ecodes.KEY_LEFTALT in caps[ecodes.EV_KEY]:
            devices.append(d)
    return devices


def listen_keyboard(device, recorder):
    try:
        for event in device.read_loop():
            if event.type != ecodes.EV_KEY:
                continue
            if event.value == 1:
                recorder.on_press(event.code)
            elif event.value == 0:
                recorder.on_release(event.code)
    except OSError as e:
        log(f"Keyboard listener for {device.path} stopped: {e}")
    finally:
        try:
            device.close()
        except Exception:
            pass


KEYBOARD_RESCAN_INTERVAL_SECONDS = 2.0


def keyboard_supervisor(recorder):
    active = {}
    while True:
        for path in list(active):
            thread, _ = active[path]
            if not thread.is_alive():
                del active[path]
                log(f"Keyboard listener for {path} gone, will rescan")

        try:
            current = find_keyboard_devices()
        except Exception as e:
            log(f"Keyboard rescan error: {e}")
            current = []

        for device in current:
            if device.path in active:
                try:
                    device.close()
                except Exception:
                    pass
                continue
            t = threading.Thread(
                target=listen_keyboard, args=(device, recorder), daemon=True
            )
            t.start()
            active[device.path] = (t, device)
            log(f"Listening on {device.path} ({device.name})")

        time.sleep(KEYBOARD_RESCAN_INTERVAL_SECONDS)


class DirectoryAudioEnqueuer:
    def __init__(self, out_queue, watch_directories=None):
        self.out_queue = out_queue
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
                    self.out_queue.put_nowait(("file", audio_path))
                    self.processed_signatures.add(signature)
                    self.file_sizes.pop(audio_path, None)
                    self.stable_pass_counts.pop(audio_path, None)
                    log(f"Queued file for transcription: {audio_path}")
                except queue.Full:
                    log("Transcription queue is full. Dropping file path.")


def transcription_worker(transcriber, in_queue, on_transcript=None):
    while True:
        payload = in_queue.get()
        try:
            source_type, source_data = payload
            if source_type == "array":
                text = transcriber.transcribe_array(source_data)
            elif source_type == "file":
                text = transcriber.transcribe_file(source_data)
            else:
                log(f"Unknown payload type: {source_type}")
                text = ""

            if text:
                copy_to_clipboard(text)
                if on_transcript:
                    on_transcript(text)
                if source_type == "file":
                    txt_path = os.path.splitext(source_data)[0] + ".txt"
                    with open(txt_path, "w", encoding="utf-8") as f:
                        f.write(text)
                    log(f"Transcription saved: {txt_path}")
                log("Transcribed text copied to clipboard.")
                stripped = text.strip()
                preview = stripped[:40] + "..." if len(stripped) > 40 else stripped
                send_notification("Transcription Complete", preview)
            else:
                log("Transcription returned empty text.")
        except Exception as e:
            log(f"Transcription failed: {e}")
        finally:
            in_queue.task_done()


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
        return ["", [], "Altalk", "Зажми Left Alt и говори"]

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

    def __init__(self, on_settings_saved=None):
        self.history = deque(maxlen=HISTORY_SIZE)
        self.history_lock = threading.Lock()
        self.next_entry_id = 100
        self.item = StatusNotifierItem()
        self.menu = DBusMenu(self)
        self.loop = None
        self.quit_event = None
        self.restore_icon_handle = None
        self.on_settings_saved = on_settings_saved
        self.settings_task = None
        self.settings_process = None

    def add_transcript(self, text):
        # Called from the transcription worker thread; D-Bus objects live on the asyncio loop.
        with self.history_lock:
            self.history.appendleft((self.next_entry_id, time.strftime("%H:%M"), text))
            self.next_entry_id += 1
        if self.loop:
            self.loop.call_soon_threadsafe(self._layout_changed)

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
        for entry_id, stamp, text in history:
            # DBusMenu treats "_" as a mnemonic marker; "__" is a literal underscore.
            label = f"{stamp}  {preview_text(text)}".replace("_", "__")
            items.append(
                (entry_id, {
                    "label": Variant("s", label),
                    "icon-name": Variant("s", "edit-copy-symbolic"),
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
            text = next((t for entry_id, _, t in self.history if entry_id == item_id), None)
        if text is None:
            return
        copy_to_clipboard(text)
        log(f"Copied from history: {preview_text(text)}")
        self._set_icon(COPIED_ICON_NAME)
        if self.restore_icon_handle:
            self.restore_icon_handle.cancel()
        self.restore_icon_handle = self.loop.call_later(
            COPIED_FLASH_SECONDS, self._set_icon, TRAY_ICON_NAME
        )

    def _set_icon(self, name):
        self.item.icon_name = name
        self.item.NewIcon()

    def open_settings(self):
        if self.settings_task and not self.settings_task.done():
            return
        self.settings_task = self.loop.create_task(self._run_settings())

    async def _run_settings(self):
        # A separate process keeps Tk out of the tray app; exit code 0 means "saved".
        self.settings_process = await asyncio.create_subprocess_exec(*app_command(), "--settings")
        code = await self.settings_process.wait()
        self.settings_process = None
        if code == 0 and self.on_settings_saved:
            self.on_settings_saved()

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


def install_desktop_entry():
    data_home = os.getenv("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    icon_dir = os.path.join(data_home, "icons", "hicolor", "256x256", "apps")
    apps_dir = os.path.join(data_home, "applications")
    os.makedirs(icon_dir, exist_ok=True)
    os.makedirs(apps_dir, exist_ok=True)
    shutil.copyfile(ICON_PATH, os.path.join(icon_dir, "altalk.png"))
    entry_path = os.path.join(apps_dir, "altalk.desktop")
    with open(entry_path, "w") as f:
        f.write(DESKTOP_ENTRY.format(exec=" ".join(app_command())))
    print(f"Installed {entry_path}")


def check_api_key(api_key, model):
    if not api_key:
        return "Впиши ключ OpenAI"
    client = OpenAI(api_key=api_key, timeout=15, max_retries=0)
    try:
        client.models.retrieve(model)
    except AuthenticationError:
        return "OpenAI не принял ключ — проверь, что он скопирован целиком"
    except (NotFoundError, PermissionDeniedError):
        return f"Модель {model} недоступна для этого ключа"
    except APIConnectionError:
        return None  # offline: save without the check rather than block the user
    return None


def run_settings_window():
    # Imported here: only the settings process needs Tk, not the tray app or /refinement.
    import tkinter as tk
    from tkinter import font as tkfont
    from tkinter import ttk

    settings = read_settings()
    root = tk.Tk(className="altalk")
    root.title("Altalk — настройки")
    root.resizable(False, False)
    root.iconphoto(True, tk.PhotoImage(file=ICON_PATH))
    ttk.Style(root).theme_use("clam")
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
        tkfont.nametofont(name).configure(size=11)

    form = ttk.Frame(root, padding=20)
    form.grid(sticky="nsew")

    ttk.Label(form, text="Ключ OpenAI").grid(row=0, column=0, columnspan=2, sticky="w")
    key = tk.StringVar(value=settings.get("OPENAI_API_KEY", ""))
    key_entry = ttk.Entry(form, textvariable=key, show="•", width=44)
    key_entry.grid(row=1, column=0, sticky="we", pady=(4, 14))
    shown = tk.BooleanVar(value=False)
    ttk.Checkbutton(
        form,
        text="Показать",
        variable=shown,
        command=lambda: key_entry.configure(show="" if shown.get() else "•"),
    ).grid(row=1, column=1, padx=(10, 0), pady=(4, 14))

    ttk.Label(form, text="Модель распознавания").grid(row=2, column=0, columnspan=2, sticky="w")
    current_model = settings.get(MODEL_SETTING) or MODEL
    models = TRANSCRIBE_MODELS if current_model in TRANSCRIBE_MODELS else [current_model, *TRANSCRIBE_MODELS]
    model = tk.StringVar(value=current_model)
    ttk.Combobox(form, textvariable=model, values=models, state="readonly").grid(
        row=3, column=0, columnspan=2, sticky="we", pady=(4, 6)
    )

    status = ttk.Label(form, text="", wraplength=430)
    status.grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))

    buttons = ttk.Frame(form)
    buttons.grid(row=5, column=0, columnspan=2, sticky="e", pady=(14, 0))
    result = {"code": 1}

    def save(_event=None):
        status.configure(text="Проверяю ключ…", foreground="#555555")
        root.update_idletasks()
        error = check_api_key(key.get().strip(), model.get())
        if error:
            status.configure(text=error, foreground="#c0392b")
            return
        save_settings({"OPENAI_API_KEY": key.get().strip(), MODEL_SETTING: model.get()})
        result["code"] = 0
        root.destroy()

    ttk.Button(buttons, text="Отмена", command=root.destroy).grid(row=0, column=0, padx=(0, 8))
    ttk.Button(buttons, text="Сохранить", command=save).grid(row=0, column=1)
    root.bind("<Return>", save)
    root.bind("<Escape>", lambda _event: root.destroy())
    key_entry.focus_set()
    root.eval("tk::PlaceWindow . center")
    root.mainloop()
    return result["code"]


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
    if sys.argv[1:] == ["--settings"]:
        raise SystemExit(run_settings_window())
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
    recorder = PushToTalkRecorder(transcribe_queue)

    def reload_settings():
        try:
            transcriber.configure(load_api_key())
        except RuntimeError:
            transcriber.configure(None)

    tray = TrayIcon(on_settings_saved=reload_settings)

    threading.Thread(
        target=transcription_worker,
        args=(transcriber, transcribe_queue, tray.add_transcript),
        daemon=True,
    ).start()

    threading.Thread(target=recorder.record_audio, daemon=True).start()
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        file_enqueuer = DirectoryAudioEnqueuer(transcribe_queue)
        threading.Thread(target=file_enqueuer.run, daemon=True).start()
        log(f"To add external audio while running, put file into: {DEFAULT_WATCH_DIRECTORY}")
    else:
        log("ffmpeg/ffprobe not found: audio-file inbox disabled")

    initial = find_keyboard_devices()
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

    threading.Thread(target=keyboard_supervisor, args=(recorder,), daemon=True).start()
    log("Ready. Hold Left Alt to record.")
    asyncio.run(tray.run(open_settings=api_key is None))
    log("Quit from tray")
    instance_lock.close()


if __name__ == "__main__":
    main()
