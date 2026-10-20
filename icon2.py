"""
Распознавание речи через OpenAI API (gpt-4o-mini-transcribe).
Push-to-talk по Left Alt или Left Ctrl + мониторинг папки для аудиофайлов.
Всё системное (клавиатура, трей, буфер обмена, автозапуск) — в linux_desktop.py, windows_desktop.py
и macos_desktop.py.

Запуск:
    python3 -m pip install -r requirements.txt
    python3 icon2.py
"""

import asyncio
import io
import itertools
import json
import math
import queue
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

import numpy as np
import sounddevice as sd
import soundfile as sf
from openai import (
    APIConnectionError, APIError, APIStatusError, APITimeoutError,
    AuthenticationError, OpenAI, RateLimitError,
)

SAMPLE_RATE = 16000
CHANNELS = 1
MIN_DURATION_SECONDS = 1.0
TRANSCRIBE_QUEUE_SIZE = 8
MODEL = "gpt-4o-mini-transcribe"
MODEL_SETTING = "ALTALK_MODEL"
TRANSCRIBE_MODELS = ["gpt-4o-mini-transcribe", "gpt-4o-transcribe", "gpt-transcribe", "whisper-1"]
HOTKEY_SETTING = "ALTALK_HOTKEY"
if sys.platform == "darwin":
    HOTKEYS = {"alt": "Left Option", "ctrl": "Left Control"}
else:
    HOTKEYS = {"alt": "Left Alt", "ctrl": "Left Ctrl"}
DEFAULT_HOTKEY = "alt"
SPEECH_LANGUAGE_SETTING = "ALTALK_LANGUAGE"
# What OpenAI transcribes (its speech-to-text docs): ISO 639-1 code → the language's name in
# itself, ordered by that name.
SPEECH_LANGUAGES = {
    "af": "Afrikaans", "az": "Azərbaycan dili", "id": "Bahasa Indonesia", "ms": "Bahasa Melayu",
    "bs": "Bosanski", "ca": "Català", "cs": "Čeština", "cy": "Cymraeg", "da": "Dansk",
    "de": "Deutsch", "et": "Eesti", "en": "English", "es": "Español", "fr": "Français",
    "gl": "Galego", "hr": "Hrvatski", "is": "Íslenska", "it": "Italiano", "sw": "Kiswahili",
    "lv": "Latviešu", "lt": "Lietuvių", "hu": "Magyar", "mi": "Māori", "nl": "Nederlands",
    "no": "Norsk", "pl": "Polski", "pt": "Português", "ro": "Română", "sk": "Slovenčina",
    "sl": "Slovenščina", "fi": "Suomi", "sv": "Svenska", "tl": "Tagalog", "vi": "Tiếng Việt",
    "tr": "Türkçe", "el": "Ελληνικά", "be": "Беларуская", "bg": "Български", "mk": "Македонски",
    "ru": "Русский", "sr": "Српски", "uk": "Українська", "kk": "Қазақ тілі", "hy": "Հայերեն",
    "he": "עברית", "ur": "اردو", "ar": "العربية", "fa": "فارسی", "ne": "नेपाली", "mr": "मराठी",
    "hi": "हिन्दी", "ta": "தமிழ்", "kn": "ಕನ್ನಡ", "th": "ไทย", "ko": "한국어", "zh": "中文",
    "ja": "日本語",
}
DEFAULT_PROMPTS = {
    "ru": "Задача в Jira. Деплой через docker compose. CI/CD pipeline, phpstan, cs-fixer, дейлик, services.yaml, deprecated.",
    "en": "Jira task. Deploy with docker compose. CI/CD pipeline, phpstan, cs-fixer, daily standup, services.yaml, deprecated.",
}
PROMPT = None  # vocabulary.json's "prompt"; without one, DEFAULT_PROMPTS for the speech language
APP_DIR = os.path.dirname(os.path.abspath(__file__))  # source checkout or the PyInstaller bundle
ASSETS_DIR = os.path.join(APP_DIR, "assets")
if sys.platform == "win32":
    CONFIG_DIR = os.path.join(os.getenv("APPDATA") or os.path.expanduser("~"), "Altalk")
    STATE_DIR = os.path.join(os.getenv("LOCALAPPDATA") or os.path.expanduser("~"), "Altalk")
elif sys.platform == "darwin":
    CONFIG_DIR = os.path.expanduser("~/Library/Application Support/Altalk")
    STATE_DIR = os.path.expanduser("~/Library/Logs/Altalk")
else:
    CONFIG_DIR = os.path.join(os.getenv("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "altalk")
    STATE_DIR = os.path.join(os.getenv("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "altalk")
ENV_PATHS = [os.path.join(APP_DIR, ".env"), os.path.join(CONFIG_DIR, ".env")]
VOCABULARY_PATHS = [os.path.join(APP_DIR, "vocabulary.json"), os.path.join(CONFIG_DIR, "vocabulary.json")]
LOG_PATH = os.path.join(STATE_DIR, "altalk.log")
ICON_PATH = os.path.join(ASSETS_DIR, "altalk-256.png")
TRAY_ICON_NAME = "altalk-tray"
COPIED_ICON_NAME = "altalk-copied"
FAILED_ICON_NAME = "altalk-failed"
# Speech bubbles with dots, cycled: white while the hotkey is held, yellow while transcribing.
LISTENING_ICON_NAMES = ["altalk-listening-1", "altalk-listening-2", "altalk-listening-3"]
BUSY_ICON_NAMES = ["altalk-busy-1", "altalk-busy-2", "altalk-busy-3"]
ANIMATION_FRAME_SECONDS = 0.4
# Alt+Tab, Ctrl+C and the like press the hotkey too: the listening icon waits this long to skip them.
LISTENING_ICON_DELAY_SECONDS = 0.3
COPIED_FLASH_SECONDS = 1.2
FAILED_FLASH_SECONDS = 3.0
HISTORY_SIZE = 5
HISTORY_PREVIEW_CHARS = 45
HISTORY_ICONS = {
    "done": "edit-copy-symbolic",
    "failed": "dialog-error-symbolic",
    "empty": "dialog-information-symbolic",
}
DEFAULT_WATCH_DIRECTORY = "~/Downloads/icon_audio_inbox"
WATCH_DIRECTORIES = [os.getenv("ICON_WATCH_DIR", DEFAULT_WATCH_DIRECTORY)]
FILE_POLL_INTERVAL_SECONDS = 1.0
FILE_STABLE_PASSES = 1
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_CHUNK_DURATION_SECONDS = 480.0
CHUNK_OVERLAP_SECONDS = 4.0
TRANSCODE_BITRATE = "64k"
# Without it every ffmpeg run from the windowed Windows app flashes a console window.
NO_CONSOLE_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
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

UI_TEXTS = {
    "ru": {
        "no_history": "Пока нет транскрипций",
        "settings": "Настройки…",
        "quit": "Выйти",
        "status_failed": "Ошибка: {error} — повторить",
        "status_empty": "ничего не распознано",
        "status_queued": "в очереди",
        "status_retry": "попытка {attempt} из {total}: {error}",
        "status_running": "распознаю…",
        "error_no_key": "нет ключа OpenAI",
        "error_timeout": "таймаут",
        "error_connection": "нет связи",
        "error_bad_key": "неверный ключ",
        "error_no_credit": "нет денег на счёте",
        "error_rate_limit": "лимит запросов",
        "error_status": "OpenAI ответил {code}",
        "hint": "Зажми {key} и говори",
        "notify_done": "Распознано",
        "notify_no_key": "Нет ключа OpenAI — открой «Настройки…» в меню трея",
        "notify_too_quiet": "Слишком тихо — проверьте микрофон/громкость",
        "notify_no_keyboard": "Нет доступа к клавиатуре: sudo usermod -aG input $USER и перелогиниться",
        "notify_no_input_monitoring": "Разреши Altalk «Мониторинг ввода»: Системные настройки → Конфиденциальность и безопасность",
        "notify_running": "Уже запущен — иконка в трее",
        "notify_no_gtk": "Для окна настроек нужен GTK 4 (gir1.2-adw-1). Ключ можно вписать в {path}",
        "dialog_title": "Настройки Altalk",
        "cancel": "Отмена",
        "save": "Сохранить",
        "checking": "Проверяю…",
        "stored_in": "Хранится в {path}",
        "api_key": "Ключ API",
        "show_key": "Показать ключ",
        "model": "Модель распознавания",
        "speech_language": "Язык речи",
        "general": "Общие",
        "hotkey": "Клавиша записи",
        "hotkey_hint": "Зажми её и говори",
        "autostart": "Добавить в автозапуск",
        "autostart_hint": "Altalk будет запускаться при входе в систему",
        "enter_key": "Впиши ключ OpenAI",
        "key_rejected": "OpenAI не принял ключ — проверь, что он скопирован целиком",
        "model_unavailable": "Модель {model} недоступна для этого ключа",
    },
    "en": {
        "no_history": "No transcriptions yet",
        "settings": "Settings…",
        "quit": "Quit",
        "status_failed": "Error: {error} — retry",
        "status_empty": "nothing recognized",
        "status_queued": "queued",
        "status_retry": "attempt {attempt} of {total}: {error}",
        "status_running": "transcribing…",
        "error_no_key": "no OpenAI key",
        "error_timeout": "timed out",
        "error_connection": "no connection",
        "error_bad_key": "invalid key",
        "error_no_credit": "out of credit",
        "error_rate_limit": "rate limited",
        "error_status": "OpenAI returned {code}",
        "hint": "Hold {key} and speak",
        "notify_done": "Transcription Complete",
        "notify_no_key": "No OpenAI key — open Settings… in the tray menu",
        "notify_too_quiet": "Too quiet — check the microphone and its volume",
        "notify_no_keyboard": "No access to the keyboard: run sudo usermod -aG input $USER and log in again",
        "notify_no_input_monitoring": "Allow Altalk in System Settings → Privacy & Security → Input Monitoring",
        "notify_running": "Already running — see the tray icon",
        "notify_no_gtk": "The settings window needs GTK 4 (gir1.2-adw-1). The key can also go into {path}",
        "dialog_title": "Altalk Settings",
        "cancel": "Cancel",
        "save": "Save",
        "checking": "Checking…",
        "stored_in": "Stored in {path}",
        "api_key": "API key",
        "show_key": "Show key",
        "model": "Transcription model",
        "speech_language": "Speech language",
        "general": "General",
        "hotkey": "Record key",
        "hotkey_hint": "Hold it and speak",
        "autostart": "Start at login",
        "autostart_hint": "Altalk starts when you log in",
        "enter_key": "Enter your OpenAI key",
        "key_rejected": "OpenAI rejected the key — check that it was copied in full",
        "model_unavailable": "Model {model} is not available for this key",
    },
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


def system_language():
    # The system's language as a code like "ru" or "de" ("" when it is unknown, e.g. LANG=C).
    if sys.platform == "win32":
        import ctypes
        import locale
        name = locale.windows_locale.get(ctypes.windll.kernel32.GetUserDefaultUILanguage(), "")
    elif sys.platform == "darwin":
        # An app started from Finder gets no LANG; macOS keeps the languages in its own settings.
        from Foundation import NSLocale
        name = str((NSLocale.preferredLanguages() or [""])[0])
    else:
        name = next((os.environ[n] for n in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG") if os.environ.get(n)), "")
    code = re.split(r"[_.@:-]", name)[0].lower()
    return {"nb": "no", "nn": "no"}.get(code, code)  # OpenAI knows Norwegian only as "no"


SYSTEM_LANGUAGE = system_language()
UI_LANGUAGE = SYSTEM_LANGUAGE if SYSTEM_LANGUAGE in UI_TEXTS else "en"  # the interface: Russian or English


def tr(key, /, **values):
    return UI_TEXTS[UI_LANGUAGE][key].format(**values)


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
        key: os.environ[key]
        for key in ("OPENAI_API_KEY", MODEL_SETTING, HOTKEY_SETTING, SPEECH_LANGUAGE_SETTING)
        if os.environ.get(key)
    }
    for path in reversed(ENV_PATHS):
        settings.update(read_env_file(path))
    return settings


def read_hotkey():
    hotkey = read_settings().get(HOTKEY_SETTING, "").lower()
    return hotkey if hotkey in HOTKEYS else DEFAULT_HOTKEY


def read_speech_language():
    # Unset, it follows the system language; a Russian speaker on an English system sets it.
    language = read_settings().get(SPEECH_LANGUAGE_SETTING, "").lower()
    if language in SPEECH_LANGUAGES:
        return language
    return SYSTEM_LANGUAGE if SYSTEM_LANGUAGE in SPEECH_LANGUAGES else UI_LANGUAGE


def check_api_key(api_key, model):
    # What the settings windows run on «Save»; settings_dialog.py (a separate process) has a copy.
    if not api_key:
        return tr("enter_key")
    request = urllib.request.Request(
        f"https://api.openai.com/v1/models/{urllib.parse.quote(model)}",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        urllib.request.urlopen(request, timeout=15).close()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return tr("key_rejected")
        if e.code in (403, 404):
            return tr("model_unavailable", model=model)
    except OSError:
        pass  # offline: save without the check rather than block the user
    return None


def settings_path():
    return next((p for p in ENV_PATHS if os.path.isfile(p)), ENV_PATHS[-1])


def settings_dialog_values():
    settings = read_settings()
    return {
        "key": settings.get("OPENAI_API_KEY", ""),
        "model": settings.get(MODEL_SETTING) or MODEL,
        "models": TRANSCRIBE_MODELS,
        "language": read_speech_language(),
        "languages": [[language, name] for language, name in SPEECH_LANGUAGES.items()],
        "hotkey": read_hotkey(),
        "hotkeys": [[hotkey, name] for hotkey, name in HOTKEYS.items()],
        "autostart": desktop.autostart_enabled(),
        "path": settings_path().replace(os.path.expanduser("~"), "~", 1),
        "texts": UI_TEXTS[UI_LANGUAGE],
    }


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


desktop = None  # linux_desktop or windows_desktop, set by main()


def load_desktop():
    # Imported here, not at the top: other scripts import this file just for OpenAITranscriber.
    if sys.platform == "win32":
        import windows_desktop as module
    elif sys.platform == "darwin":
        import macos_desktop as module
    else:
        import linux_desktop as module
    module.app = sys.modules[__name__]
    return module


def send_notification(title, message):
    if desktop:
        desktop.notify(title, message)


def copy_to_clipboard(text):
    desktop.copy_to_clipboard(text)


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
        creationflags=NO_CONSOLE_WINDOW,
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
        creationflags=NO_CONSOLE_WINDOW,
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
            creationflags=NO_CONSOLE_WINDOW,
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
        return tr("error_no_key")
    if isinstance(error, APITimeoutError):
        return tr("error_timeout")
    if isinstance(error, APIConnectionError):
        return tr("error_connection")
    if isinstance(error, AuthenticationError):
        return tr("error_bad_key")
    if isinstance(error, RateLimitError):
        return tr("error_no_credit") if error.code == "insufficient_quota" else tr("error_rate_limit")
    if isinstance(error, APIStatusError):
        return tr("error_status", code=error.status_code)
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
        # Also called by the tray after the settings window saved a new key, model or language.
        self.model = model or read_settings().get(MODEL_SETTING) or MODEL
        self.language = read_speech_language()
        if not api_key:
            self.client = None
            log(f"OpenAI transcriber waiting for an API key (model: {self.model}, language: {self.language})")
            return
        # Retries are ours (see _request), not the SDK's, so the tray can show the attempt.
        self.client = OpenAI(
            api_key=api_key,
            timeout=REQUEST_TIMEOUT_SECONDS,
            max_retries=0,
        )
        log(
            f"OpenAI transcriber ready (model: {self.model}, language: {self.language}, "
            f"timeout: {REQUEST_TIMEOUT_SECONDS:.0f}s, retries: {MAX_RETRIES})"
        )

    def _require_client(self):
        if self.client is None:
            log("OPENAI_API_KEY not set, skipping transcription")
            send_notification("Altalk", tr("notify_no_key"))
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
        options = {"language": self.language}
        prompt = PROMPT or DEFAULT_PROMPTS.get(self.language)  # no default for most languages
        if prompt:
            options["prompt"] = prompt
        for attempt in range(1, MAX_RETRIES + 2):
            if on_attempt:
                on_attempt(attempt, error)
            audio_file.seek(0)
            started = time.monotonic()
            try:
                response = self.client.audio.transcriptions.create(
                    model=self.model,
                    file=audio_file,
                    **options,
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
    def __init__(self, submit, hotkey, on_listening=lambda listening: None):
        self.submit = submit
        self.on_listening = on_listening  # called from input threads
        self.recording = False
        self.aborted = False
        self.audio_buffer = []
        self.buffer_lock = threading.Lock()
        self.microphone_wanted = threading.Event()
        self.set_hotkey(hotkey)

    def set_hotkey(self, hotkey):
        # Also called by the tray after the settings window saved another key.
        self.key_name, self.keycode = HOTKEYS[hotkey], desktop.HOTKEY_CODES[hotkey]
        self.recording = False
        self.on_listening(False)

    def callback(self, indata, frames, time_info, status):
        if status:
            log(status)
        if self.recording and not self.aborted:
            with self.buffer_lock:
                self.audio_buffer.append(indata.copy())

    def open_microphone(self):
        return sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            callback=self.callback,
            blocksize=1024,
        )

    def record_audio(self):
        if desktop.KEEP_MIC_OPEN:
            with self.open_microphone():
                while True:
                    sd.sleep(100)
        # Windows shows «microphone in use» for as long as the stream is open,
        # so there it is open only while the hotkey is held.
        while True:
            self.microphone_wanted.wait()
            self.microphone_wanted.clear()
            try:
                with self.open_microphone():
                    while self.recording and not self.aborted:
                        sd.sleep(20)
            except sd.PortAudioError as e:
                log(f"Microphone unavailable: {e}")

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
            send_notification("Altalk", tr("notify_too_quiet"))
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
                self.microphone_wanted.set()
                self.on_listening(True)
                log("Recording started")
        else:
            self.cancel(f"Other key pressed (code {keycode})")

    def cancel(self, reason):
        # The hotkey was used as a modifier (Ctrl+C, Alt+Tab), not to talk.
        if self.recording and not self.aborted:
            self.aborted = True
            with self.buffer_lock:
                self.audio_buffer = []
            self.on_listening(False)
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
                threading.Thread(target=self._finish_recording, daemon=True).start()

    def _finish_recording(self):
        # Listening ends once the recording is queued or discarded, so the tray goes straight
        # from the listening icon to the busy one.
        try:
            self.flush_recording()
        finally:
            if not self.recording:  # unless the next recording has already started
                self.on_listening(False)


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
                send_notification(tr("notify_done"), preview)
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
        status = tr("status_failed", error=job.error)
    elif job.state == "empty":
        status = tr("status_empty")
    elif job.state == "queued":
        status = tr("status_queued")
    elif job.attempt > 1:
        status = tr("status_retry", attempt=job.attempt, total=MAX_RETRIES + 1, error=job.error)
    else:
        status = tr("status_running")
    if job.source_type == "file":
        return f"{os.path.basename(job.source_data)} — {status}"
    return status


def preview_text(text, limit=HISTORY_PREVIEW_CHARS):
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[:limit].rsplit(" ", 1)[0].rstrip(".,;:—–-") + "…"


def hotkey_hint(hotkey):
    return tr("hint", key=HOTKEYS[hotkey])


class TrayIcon:
    # What the tray shows and does; desktop.Tray puts it on screen (D-Bus on Linux, Win32 on Windows).
    QUIT_ID = 1
    EMPTY_ID = 2
    SEPARATOR_ID = 3
    SETTINGS_ID = 4

    def __init__(self, transcribe_queue, on_settings_saved=None):
        self.transcribe_queue = transcribe_queue
        self.history = deque(maxlen=HISTORY_SIZE)
        self.history_lock = threading.Lock()
        self.next_entry_id = 100
        self.icon_name = TRAY_ICON_NAME
        self.hint = hotkey_hint(read_hotkey())
        self.backend = desktop.Tray(self)
        self.loop = None
        self.quit_event = None
        self.restore_icon_handle = None
        self.listening = False
        self.listening_handle = None
        self.animation_task = None
        self.animation_frames = None
        self.on_settings_saved = on_settings_saved
        self.settings_task = None

    def submit(self, job):
        # Queues a new recording or file and lists it; raises queue.Full like put_nowait.
        self.transcribe_queue.put_nowait(job)
        with self.history_lock:
            self.history.appendleft((self.next_entry_id, job))
            self.next_entry_id += 1
        self.update(job)

    def update(self, job):
        # Called from worker threads; the tray lives on the asyncio loop.
        if self.loop:
            self.loop.call_soon_threadsafe(self._job_changed, job.state)

    def _job_changed(self, state):
        self.backend.menu_changed()
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

    def menu_items(self):
        # (id, properties) with DBusMenu's property names; called from the tray backend's thread.
        with self.history_lock:
            history = list(self.history)
        items = []
        if not history:
            items.append((self.EMPTY_ID, {"label": tr("no_history"), "enabled": False}))
        for entry_id, job in history:
            items.append(
                (entry_id, {
                    "label": f"{job.stamp}  {job_status(job)}",
                    "icon-name": HISTORY_ICONS.get(job.state, "content-loading-symbolic"),
                    # Done copies the text again, failed retries; the rest is still in work.
                    "enabled": job.state in ("done", "failed"),
                })
            )
        items.append((self.SEPARATOR_ID, {"type": "separator"}))
        items.append((self.SETTINGS_ID, {"label": tr("settings")}))
        items.append((self.QUIT_ID, {"label": tr("quit")}))
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

    def set_listening(self, listening):
        # Called by the recorder from input threads; the tray lives on the asyncio loop.
        if self.loop:
            self.loop.call_soon_threadsafe(self._listening_changed, listening)

    def _listening_changed(self, listening):
        if self.listening_handle:
            self.listening_handle.cancel()
            self.listening_handle = None
        if listening:
            self.listening_handle = self.loop.call_later(LISTENING_ICON_DELAY_SECONDS, self._show_listening)
        elif self.listening:
            self.listening = False
            self._refresh_icon()

    def _show_listening(self):
        self.listening_handle = None
        self.listening = True
        if self.restore_icon_handle:  # the check or cross still shown gives way
            self.restore_icon_handle.cancel()
            self.restore_icon_handle = None
        self._refresh_icon()

    def _flash(self, name, seconds):
        # Shown for a moment instead of the busy or idle icon, but not while listening.
        if self.listening:
            return
        self._stop_animation()
        if self.restore_icon_handle:
            self.restore_icon_handle.cancel()
        self._set_icon(name)
        self.restore_icon_handle = self.loop.call_later(seconds, self._end_flash)

    def _end_flash(self):
        self.restore_icon_handle = None
        self._refresh_icon()

    def _refresh_icon(self):
        # Listening while the hotkey is held, busy while anything is queued or being transcribed,
        # idle (ready) otherwise.
        if self.restore_icon_handle:
            return  # _end_flash comes back here
        with self.history_lock:
            busy = any(job.state in ("queued", "running") for _, job in self.history)
        frames = LISTENING_ICON_NAMES if self.listening else BUSY_ICON_NAMES if busy else None
        if frames is None:
            self._stop_animation()
            self._set_icon(TRAY_ICON_NAME)
        elif frames is not self.animation_frames:
            self._stop_animation()
            self.animation_frames = frames
            self.animation_task = self.loop.create_task(self._animate(frames))

    def _stop_animation(self):
        if self.animation_task:
            self.animation_task.cancel()
            self.animation_task = None
            self.animation_frames = None

    async def _animate(self, frames):
        for name in itertools.cycle(frames):
            self._set_icon(name)
            await asyncio.sleep(ANIMATION_FRAME_SECONDS)

    def _set_icon(self, name):
        if self.icon_name != name:
            self.icon_name = name
            self.backend.icon_changed()

    def open_settings(self):
        if self.settings_task and not self.settings_task.done():
            return
        self.settings_task = self.loop.create_task(self._run_settings())

    async def _run_settings(self):
        values = await self.backend.edit_settings(settings_dialog_values())
        if values is None:
            return
        save_settings({
            "OPENAI_API_KEY": values["key"],
            MODEL_SETTING: values["model"],
            SPEECH_LANGUAGE_SETTING: values["language"],
            HOTKEY_SETTING: values["hotkey"],
        })
        log(f"Settings saved to {settings_path()}")
        desktop.set_autostart(values["autostart"])
        if self.on_settings_saved:
            self.on_settings_saved()
        self.hint = hotkey_hint(values["hotkey"])
        self.backend.hint_changed()

    async def run(self, open_settings=False):
        self.loop = asyncio.get_running_loop()
        self.quit_event = asyncio.Event()
        try:
            await self.backend.start()
        except Exception as e:
            log(f"Tray icon unavailable: {e}")
        if open_settings:
            self.open_settings()
        await self.quit_event.wait()
        self.backend.stop()


def app_command():
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, os.path.abspath(__file__)]


def redirect_output_to_log():
    # Started from the app menu there is no terminal, so keep the log in the state dir.
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    log_file = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = sys.stderr = log_file  # a windowed .exe or .app has no console streams at all
        return
    os.dup2(log_file.fileno(), sys.stdout.fileno())
    os.dup2(log_file.fileno(), sys.stderr.fileno())
    sys.stdout.reconfigure(line_buffering=True)


def main():
    global desktop
    desktop = load_desktop()
    if sys.argv[1:] == ["--install"] and sys.platform != "win32":
        desktop.install_desktop_entry()
        return
    if getattr(sys, "frozen", False) and (sys.stdout is None or not sys.stdout.isatty()):
        redirect_output_to_log()

    instance_lock = desktop.acquire_instance_lock()  # held until the process exits
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
    recorder = PushToTalkRecorder(tray.submit, read_hotkey(), on_listening=tray.set_listening)

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

    desktop.watch_input(recorder)
    log(f"Ready. Hold {recorder.key_name} to record.")
    desktop.run(tray.run(open_settings=api_key is None))
    log("Quit from tray")
    instance_lock.close()


if __name__ == "__main__":
    main()
