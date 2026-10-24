from __future__ import annotations

import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import shutil
import sys
import threading
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if os.name == "nt":
    try:
        ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
    except Exception:
        pass

import pyaudio
import pystray
import pyttsx3
from PIL import Image, ImageDraw
from vosk import KaldiRecognizer, Model

import commands

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
DEFAULT_CONFIG: dict[str, Any] = {
    "assistant_name": "Пятница",
    "app_name": "Friday Assistant",
    "model_path": "models/vosk-model-small-ru-0.22",
    "model_urls": [
        "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip",
        "https://github.com/alphacep/vosk-api/releases/download/v0.3.45/vosk-model-small-ru-0.22.zip",
    ],
    "log_file": "friday.log",
    "log_level": "INFO",
    "max_log_bytes": 1_048_576,
    "backup_count": 3,
    "sample_rate": 16000,
    "audio_chunk": 4000,
    "tts_rate": 180,
    "tts_volume": 1.0,
    "input_device_index": None,
}


def load_config() -> dict[str, Any]:
    config: dict[str, Any] = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as fp:
                loaded = json.load(fp)
            if isinstance(loaded, dict):
                config.update(loaded)
        except Exception as exc:
            print(f"Failed to load config.json: {exc}", file=sys.stderr)
    return config


def resolve_path(value: str | os.PathLike[str] | None, default: Path) -> Path:
    if value in (None, ""):
        return default
    path = Path(value)
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


CONFIG = load_config()
APP_NAME = str(CONFIG.get("app_name", DEFAULT_CONFIG["app_name"]))
ASSISTANT_NAME = str(CONFIG.get("assistant_name", DEFAULT_CONFIG["assistant_name"]))
MODEL_PATH = resolve_path(CONFIG.get("model_path"), BASE_DIR / str(DEFAULT_CONFIG["model_path"]))
MODEL_ZIP = MODEL_PATH.parent / f"{MODEL_PATH.name}.zip"
MODEL_URLS = tuple(str(url) for url in CONFIG.get("model_urls", DEFAULT_CONFIG["model_urls"]))
LOG_FILE = resolve_path(CONFIG.get("log_file"), BASE_DIR / str(DEFAULT_CONFIG["log_file"]))
LOG_LEVEL = str(CONFIG.get("log_level", DEFAULT_CONFIG["log_level"]))
MAX_LOG_BYTES = int(CONFIG.get("max_log_bytes", DEFAULT_CONFIG["max_log_bytes"]))
BACKUP_COUNT = int(CONFIG.get("backup_count", DEFAULT_CONFIG["backup_count"]))
SAMPLE_RATE = int(CONFIG.get("sample_rate", DEFAULT_CONFIG["sample_rate"]))
AUDIO_CHUNK = int(CONFIG.get("audio_chunk", DEFAULT_CONFIG["audio_chunk"]))
TTS_RATE = int(CONFIG.get("tts_rate", DEFAULT_CONFIG["tts_rate"]))
TTS_VOLUME = float(CONFIG.get("tts_volume", DEFAULT_CONFIG["tts_volume"]))
INPUT_DEVICE_INDEX = CONFIG.get("input_device_index", DEFAULT_CONFIG["input_device_index"])


@dataclass
class RuntimeState:
    shutdown: threading.Event = field(default_factory=threading.Event)
    paused: threading.Event = field(default_factory=threading.Event)
    icon: pystray.Icon | None = None
    worker: threading.Thread | None = None


state = RuntimeState()


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("friday")
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=MAX_LOG_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    logger.addHandler(console)
    logger.addHandler(file_handler)
    logger.propagate = False
    return logger


logger = setup_logging()


def init_tts() -> pyttsx3.Engine | None:
    try:
        engine = pyttsx3.init()
        engine.setProperty("rate", TTS_RATE)
        engine.setProperty("volume", TTS_VOLUME)
        for voice in engine.getProperty("voices") or []:
            voice_id = str(getattr(voice, "id", "")).lower()
            voice_name = str(getattr(voice, "name", "")).lower()
            if any(token in f"{voice_id} {voice_name}" for token in ("ru", "russian", "russia", "russ")):
                try:
                    engine.setProperty("voice", voice.id)
                    break
                except Exception:
                    pass
        return engine
    except Exception:
        logger.exception("TTS initialization failed")
        return None


engine = init_tts()


def speak(text: str) -> None:
    text = str(text).strip()
    if not text:
        return
    logger.info("%s: %s", APP_NAME, text)
    if engine is None:
        return
    try:
        engine.say(text)
        engine.runAndWait()
    except Exception:
        logger.exception("TTS error")


def list_microphones(audio: pyaudio.PyAudio) -> list[int]:
    found: list[int] = []
    logger.info("Available audio input devices:")
    for index in range(audio.get_device_count()):
        try:
            info = audio.get_device_info_by_index(index)
        except Exception:
            continue
        if int(info.get("maxInputChannels", 0)) > 0:
            found.append(index)
            logger.info(
                "[%s] %s | input=%s | rate=%s",
                index,
                info.get("name", "Unknown"),
                info.get("maxInputChannels"),
                info.get("defaultSampleRate", "?"),
            )
    if not found:
        logger.warning("No microphone inputs found")
    return found


def select_input_device(audio: pyaudio.PyAudio, available: list[int]) -> int | None:
    configured = INPUT_DEVICE_INDEX
    if configured is not None:
        try:
            configured_index = int(configured)
            if configured_index in available:
                try:
                    info = audio.get_device_info_by_index(configured_index)
                    logger.info("Using configured input device: [%s] %s", configured_index, info.get("name", ""))
                except Exception:
                    logger.info("Using configured input device: [%s]", configured_index)
                return configured_index
            logger.warning("Configured input device %s is unavailable", configured_index)
        except Exception:
            logger.warning("Invalid configured input device index: %r", configured)

    try:
        default_info = audio.get_default_input_device_info()
        default_index = int(default_info["index"])
        if default_index in available:
            logger.info("Using default input device: [%s] %s", default_index, default_info.get("name", ""))
            return default_index
    except Exception:
        pass

    if available:
        first = available[0]
        try:
            info = audio.get_device_info_by_index(first)
            logger.info("Using first available input device: [%s] %s", first, info.get("name", ""))
        except Exception:
            logger.info("Using first available input device: [%s]", first)
        return first

    return None


def find_model_dir() -> Path | None:
    if MODEL_PATH.exists() and MODEL_PATH.is_dir():
        return MODEL_PATH
    return None


def download_model_zip(dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not MODEL_URLS:
        raise RuntimeError("No Vosk model URLs configured")
    for url in MODEL_URLS:
        try:
            logger.info("Downloading Vosk model from %s", url)
            with urllib.request.urlopen(url) as response, dest.open("wb") as fp:
                shutil.copyfileobj(response, fp)
            return dest
        except Exception as exc:
            logger.warning("Download failed from %s: %s", url, exc)
    raise RuntimeError("Unable to download Vosk model")


def extract_model(zip_path: Path, target_dir: Path) -> Path:
    """Extract only regular archive entries contained under target_dir.parent."""
    logger.info("Extracting model archive")
    extraction_root = target_dir.parent.resolve()
    extraction_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as archive:
        for member in archive.infolist():
            # Resolve each path before writing: ZIP entries may contain ../ or
            # absolute paths and otherwise overwrite files outside the model dir.
            destination = (extraction_root / member.filename).resolve()
            if destination != extraction_root and extraction_root not in destination.parents:
                raise ValueError(f"Unsafe path in model archive: {member.filename!r}")
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member, "r") as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)
    return target_dir


def ensure_model() -> Path:
    model_dir = find_model_dir()
    if model_dir is not None:
        return model_dir
    if MODEL_ZIP.exists():
        return extract_model(MODEL_ZIP, MODEL_PATH)
    zip_path = download_model_zip(MODEL_ZIP)
    return extract_model(zip_path, MODEL_PATH)


def create_recognizer(model: Model, sample_rate: int) -> KaldiRecognizer:
    recognizer = KaldiRecognizer(model, sample_rate)
    recognizer.SetWords(False)
    return recognizer


def create_tray_image() -> Image.Image:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, 56, 56), radius=12, fill=(33, 115, 255, 255))
    draw.ellipse((20, 20, 44, 44), fill=(255, 255, 255, 255))
    return image


def status_text(_item: pystray.MenuItem) -> str:
    return f"Статус: {'Пауза' if state.paused.is_set() else 'Работает'}"


def pause_text(_item: pystray.MenuItem) -> str:
    return "Продолжить" if state.paused.is_set() else "Пауза"


def request_exit(icon: pystray.Icon | None = None, _item: Any = None) -> None:
    state.shutdown.set()
    tray_icon = icon or state.icon
    if tray_icon is not None:
        try:
            tray_icon.stop()
        except Exception:
            pass


def toggle_pause(_icon: pystray.Icon, _item: pystray.MenuItem) -> None:
    if state.paused.is_set():
        state.paused.clear()
        logger.info("Processing resumed")
    else:
        state.paused.set()
        logger.info("Processing paused")


def audio_worker() -> None:
    audio = pyaudio.PyAudio()
    stream = None
    try:
        available = list_microphones(audio)
        device_index = select_input_device(audio, available)
        if device_index is None:
            logger.error("No microphone input device found")
            speak("Микрофон не найден.")
            request_exit()
            return

        model_dir = ensure_model()
        logger.info("Using Vosk model: %s", model_dir)
        model = Model(str(model_dir))
        processor = commands.CommandProcessor(speak, logger=logger)

        try:
            device_info = audio.get_device_info_by_index(device_index)
            sample_rate = int(CONFIG.get("sample_rate", device_info.get("defaultSampleRate", SAMPLE_RATE)))
        except Exception:
            sample_rate = SAMPLE_RATE
        chunk = AUDIO_CHUNK

        stream = audio.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=sample_rate,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=chunk,
        )
        stream.start_stream()

        speak(f"{ASSISTANT_NAME} запущен.")
        speak("Слушаю.")

        recognizer = create_recognizer(model, sample_rate)
        was_paused = False

        while not state.shutdown.is_set():
            try:
                data = stream.read(chunk, exception_on_overflow=False)
            except Exception as exc:
                if state.shutdown.is_set():
                    break
                logger.debug("Audio read error: %s", exc)
                time.sleep(0.05)
                continue

            if state.paused.is_set():
                was_paused = True
                continue

            if was_paused:
                recognizer = create_recognizer(model, sample_rate)
                was_paused = False

            try:
                if recognizer.AcceptWaveform(data):
                    payload = json.loads(recognizer.Result())
                    text = str(payload.get("text", "")).strip()
                    if text:
                        logger.info("Vosk: %s", text)
                        try:
                            processor.process_text(text)
                        except SystemExit:
                            request_exit()
                            break
                        except Exception:
                            logger.exception("Command processing failed")
                            speak("Произошла ошибка при обработке команды.")
                else:
                    payload = json.loads(recognizer.PartialResult())
                    partial = str(payload.get("partial", "")).strip()
                    if partial:
                        try:
                            processor.process_partial(partial)
                        except SystemExit:
                            request_exit()
                            break
            except SystemExit:
                request_exit()
                break
            except Exception:
                logger.exception("Recognition loop error")
    except KeyboardInterrupt:
        request_exit()
    except SystemExit:
        request_exit()
    except Exception:
        logger.exception("Fatal assistant error")
        speak("Произошла ошибка при запуске ассистента.")
    finally:
        try:
            if stream is not None:
                stream.stop_stream()
                stream.close()
        except Exception:
            pass
        try:
            audio.terminate()
        except Exception:
            pass
        logger.info("Audio worker finished")


def build_tray_icon() -> pystray.Icon:
    menu = pystray.Menu(
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.MenuItem(pause_text, toggle_pause),
        pystray.MenuItem("Выход", request_exit),
    )
    icon = pystray.Icon("friday_assistant", create_tray_image(), APP_NAME, menu)
    state.icon = icon
    return icon


def main() -> None:
    logger.info("%s is starting", APP_NAME)
    worker = threading.Thread(target=audio_worker, name="FridayAudio", daemon=True)
    state.worker = worker
    worker.start()

    icon = build_tray_icon()
    try:
        icon.run()
    finally:
        request_exit(icon)
        worker.join(timeout=5.0)
        state.worker = None
        state.icon = None
        logger.info("%s stopped", APP_NAME)


if __name__ == "__main__":
    main()
