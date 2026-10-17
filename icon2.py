"""
Распознавание речи через OpenAI API (gpt-4o-mini-transcribe).
Push-to-talk по Left Alt + мониторинг папки для аудиофайлов.

Запуск:
    python3 -m pip install -r requirements.txt
    python3 icon2.py
"""

import io
import json
import math
import queue
import os
import re
import subprocess
import tempfile
import threading
import time

import numpy as np
import pyperclip
import sounddevice as sd
import soundfile as sf
from openai import OpenAI
from evdev import InputDevice, ecodes, list_devices

SAMPLE_RATE = 16000
CHANNELS = 1
MIN_DURATION_SECONDS = 1.0
TRANSCRIBE_QUEUE_SIZE = 8
MODEL = "gpt-4o-mini-transcribe"
PROMPT = "Задача в Jira. Деплой через docker compose. CI/CD pipeline, phpstan, cs-fixer, дейлик, services.yaml, deprecated."
VOCABULARY_PATH = os.path.join(os.path.dirname(__file__), "vocabulary.json")
ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icon.png")
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


def load_vocabulary(path=VOCABULARY_PATH):
    global PROMPT
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    PROMPT = data.get("prompt", PROMPT)
    CORRECTIONS.update(data.get("corrections", {}))


load_vocabulary()


def load_api_key():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.isfile(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key or api_key == "sk-your-key-here":
        raise RuntimeError(
            "OPENAI_API_KEY not set. Put your key in .env file:\n"
            "  OPENAI_API_KEY=sk-..."
        )
    return api_key


def send_notification(title, message):
    try:
        subprocess.run(
            ["notify-send", "-i", ICON_PATH, title, message],
            check=False,
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")


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
    def __init__(self, api_key):
        self.client = OpenAI(
            api_key=api_key,
            timeout=REQUEST_TIMEOUT_SECONDS,
            max_retries=MAX_RETRIES,
        )
        log(
            f"OpenAI transcriber ready (model: {MODEL}, "
            f"timeout: {REQUEST_TIMEOUT_SECONDS:.0f}s, retries: {MAX_RETRIES})"
        )

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
            send_notification("icon2", "Слишком тихо — проверьте микрофон/громкость")
            return ""

        wav_buf = self._audio_to_wav_bytes(audio_data)
        started = time.monotonic()
        response = self.client.audio.transcriptions.create(
            model=MODEL,
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
                model=MODEL,
                file=f,
                language="ru",
                prompt=PROMPT,
            )
        log(f"  API call took {time.monotonic() - started:.1f}s")
        return response.text

    def transcribe_file(self, audio_path):
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


def transcription_worker(transcriber, in_queue):
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
                pyperclip.copy(text)
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


def main():
    api_key = load_api_key()
    transcriber = OpenAITranscriber(api_key)
    transcribe_queue = queue.Queue(maxsize=TRANSCRIBE_QUEUE_SIZE)
    recorder = PushToTalkRecorder(transcribe_queue)
    file_enqueuer = DirectoryAudioEnqueuer(transcribe_queue)

    threading.Thread(
        target=transcription_worker,
        args=(transcriber, transcribe_queue),
        daemon=True,
    ).start()

    threading.Thread(target=recorder.record_audio, daemon=True).start()
    threading.Thread(target=file_enqueuer.run, daemon=True).start()

    initial = find_keyboard_devices()
    if not initial:
        raise SystemExit(
            "No accessible keyboard devices found in /dev/input/. "
            "Ensure your user is in the 'input' group: sudo usermod -aG input $USER"
        )
    for d in initial:
        try:
            d.close()
        except Exception:
            pass

    log("Ready. Hold Left Alt to record.")
    log(f"To add external audio while running, put file into: {DEFAULT_WATCH_DIRECTORY}")

    keyboard_supervisor(recorder)


if __name__ == "__main__":
    main()
