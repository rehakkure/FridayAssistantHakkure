from __future__ import annotations

import ast
import ctypes
import datetime as dt
import difflib
import logging
import os
import re
import subprocess
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import pyautogui

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0

APP_NAME = "Friday Assistant"
ACTIVE_SECONDS = 5.0
WAKE_THRESHOLD = 0.80
WAKE_PARTIAL_THRESHOLD = 0.88

WAKE_ALIASES = (
    "пятница",
    "фрайдей",
    "фрэйдей",
    "фридей",
    "friday",
)
WAKE_STEMS = (
    "пятниц",
    "фрайд",
    "фрэйд",
    "фрид",
    "frid",
    "fray",
)

OPEN_HINTS = ("открой", "открыть", "запусти", "запустить", "включи", "включить")
CLOSE_HINTS = ("закрой", "закрыть", "выключи", "выключить", "останови", "остановить")
FILLERS = {"ну", "пожалуйста", "плиз", "пожалуйста", "эй", "слушай"}

VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
KEYEVENTF_KEYUP = 0x0002


@dataclass(frozen=True)
class CommandRule:
    name: str
    kind: str
    patterns: tuple[str, ...] = ()
    handler: str = ""
    data: dict[str, object] = field(default_factory=dict)
    threshold: float = 0.6


COMMAND_REGISTRY: tuple[CommandRule, ...] = (
    CommandRule(
        name="media_play_pause",
        kind="exact",
        patterns=("пауза", "воспроизведение", "play pause", "плей пауза"),
        handler="handle_vk",
        data={"vk": VK_MEDIA_PLAY_PAUSE, "reply": "Воспроизведение и пауза."},
    ),
    CommandRule(
        name="media_next",
        kind="exact",
        patterns=("следующий трек", "далее", "next track"),
        handler="handle_vk",
        data={"vk": VK_MEDIA_NEXT_TRACK, "reply": "Следующий трек."},
    ),
    CommandRule(
        name="media_previous",
        kind="exact",
        patterns=("предыдущий трек", "назад", "previous track"),
        handler="handle_vk",
        data={"vk": VK_MEDIA_PREV_TRACK, "reply": "Предыдущий трек."},
    ),
    CommandRule(
        name="close_tab",
        kind="exact",
        patterns=("закрой вкладку", "закрыть вкладку", "close tab"),
        handler="handle_hotkey",
        data={"hotkey": ("ctrl", "w"), "reply": "Закрываю вкладку."},
    ),
    CommandRule(
        name="new_tab",
        kind="exact",
        patterns=("новая вкладка", "открой вкладку", "открыть вкладку", "new tab"),
        handler="handle_hotkey",
        data={"hotkey": ("ctrl", "t"), "reply": "Открываю новую вкладку."},
    ),
    CommandRule(
        name="show_desktop",
        kind="exact",
        patterns=("покажи рабочий стол", "сверни всё", "сверни окна", "show desktop"),
        handler="handle_hotkey",
        data={"hotkey": ("winleft", "d"), "reply": "Показываю рабочий стол."},
    ),
    CommandRule(
        name="minimize_all_windows",
        kind="exact",
        patterns=("сверни все окна", "свернуть все окна", "сверни окна", "minimize all windows", "minimize windows"),
        handler="handle_hotkey",
        data={"hotkey": ("winleft", "m"), "reply": "Сворачиваю все окна."},
    ),
    CommandRule(
        name="browser_home",
        kind="exact",
        patterns=("открой браузер", "открыть браузер", "запусти браузер", "браузер"),
        handler="handle_open_url",
        data={"url": "https://www.google.com", "reply": "Открываю браузер."},
    ),
    CommandRule(
        name="youtube",
        kind="exact",
        patterns=("открой ютуб", "открыть ютуб", "youtube", "ютуб"),
        handler="handle_open_url",
        data={"url": "https://www.youtube.com", "reply": "Открываю YouTube."},
    ),
    CommandRule(
        name="mail",
        kind="exact",
        patterns=("открой почту", "почта", "mail"),
        handler="handle_open_url",
        data={"url": "https://mail.google.com", "reply": "Открываю почту."},
    ),
    CommandRule(
        name="google_search",
        kind="prefix",
        patterns=("поиск в google", "найди в google", "поиск google"),
        handler="handle_search",
        data={"engine": "google", "reply_prefix": "Ищу в Google"},
    ),
    CommandRule(
        name="youtube_search",
        kind="prefix",
        patterns=("поиск на ютубе", "найди на ютубе", "поиск youtube"),
        handler="handle_search",
        data={"engine": "youtube", "reply_prefix": "Ищу на YouTube"},
    ),
    CommandRule(
        name="help",
        kind="contains",
        patterns=("что ты умеешь", "помощь", "команды", "список команд"),
        handler="handle_help",
    ),
    CommandRule(
        name="exit",
        kind="contains",
        patterns=("выход", "выключись", "остановись", "стоп", "закройся", "пока"),
        handler="handle_exit",
    ),
    CommandRule(
        name="cancel_shutdown",
        kind="contains",
        patterns=("отмена выключения", "отмени выключение", "отменить выключение", "shutdown /a"),
        handler="handle_cancel_shutdown",
    ),
    CommandRule(
        name="shutdown",
        kind="contains",
        patterns=("выключи компьютер", "выключить компьютер", "выключи пк", "выключить пк"),
        handler="handle_shutdown",
    ),
    CommandRule(
        name="lock_pc",
        kind="contains",
        patterns=("заблокируй компьютер", "заблокировать компьютер", "lock screen", "lock pc"),
        handler="handle_lock",
    ),
    CommandRule(
        name="screenshot",
        kind="fuzzy",
        patterns=("сделай скриншот", "сфоткай экран", "снимок экрана", "скрин"),
        handler="handle_screenshot",
        threshold=0.62,
    ),
    CommandRule(
        name="volume_up",
        kind="fuzzy",
        patterns=("громче", "увеличь громкость", "прибавь громкость", "сделай громче"),
        handler="handle_volume",
        data={"vk": VK_VOLUME_UP, "reply": "Увеличиваю громкость."},
        threshold=0.60,
    ),
    CommandRule(
        name="volume_down",
        kind="fuzzy",
        patterns=("тише", "уменьши громкость", "убавь громкость", "сделай тише"),
        handler="handle_volume",
        data={"vk": VK_VOLUME_DOWN, "reply": "Уменьшаю громкость."},
        threshold=0.60,
    ),
    CommandRule(
        name="notepad",
        kind="fuzzy",
        patterns=("блокнот", "notepad", "заметки"),
        handler="handle_launch_app",
        data={"target": ["notepad.exe"], "reply": "Открываю Блокнот."},
    ),
    CommandRule(
        name="calculator",
        kind="fuzzy",
        patterns=("калькулятор", "calc", "calculator"),
        handler="handle_launch_app",
        data={"target": ["calc.exe"], "reply": "Открываю Калькулятор."},
    ),
    CommandRule(
        name="yandex_music",
        kind="fuzzy",
        patterns=("яндекс музыка", "музыка", "yandex music", "музыку"),
        handler="handle_launch_app",
        data={"target": "https://music.yandex.ru", "reply": "Открываю Яндекс Музыку."},
    ),
    CommandRule(
        name="explorer",
        kind="exact",
        patterns=("проводник", "открой проводник", "открыть проводник"),
        handler="handle_open_explorer",
    ),
    CommandRule(
        name="settings",
        kind="exact",
        patterns=("настройки", "открой настройки", "открыть настройки"),
        handler="handle_open_settings",
    ),
    CommandRule(
        name="time_date",
        kind="contains",
        patterns=("который час", "сколько времени", "текущее время", "какое сегодня число", "какая дата", "дата"),
        handler="handle_time_date",
    ),
)


NUMBER_REPLACEMENTS = (
    ("плюс", "+"),
    ("минус", "-"),
    ("умножить на", "*"),
    ("умножить", "*"),
    ("разделить на", "/"),
    ("разделить", "/"),
    ("делить на", "/"),
    ("делить", "/"),
    ("скобка открывается", "("),
    ("скобка закрывается", ")"),
)


class CommandProcessor:
    def __init__(self, speak: Callable[[str], None], logger: Optional[logging.Logger] = None):
        self.speak = speak
        self.logger = logger or logging.getLogger(__name__)
        self.active_until = 0.0

    def is_active_mode(self) -> bool:
        return time.monotonic() < self.active_until

    def enter_active_mode(self) -> None:
        self.active_until = time.monotonic() + ACTIVE_SECONDS

    def sleep_mode(self) -> None:
        self.active_until = 0.0

    def process_partial(self, partial: str) -> bool:
        normalized = normalize_text(partial)
        if not normalized:
            return False
        if has_wake_word(normalized, partial=True) and not self.is_active_mode():
            self.enter_active_mode()
            self.logger.debug("Wake word detected from partial input")
            return True
        return False

    def process_text(self, text: str) -> bool:
        normalized = normalize_text(text)
        if not normalized:
            return False

        if has_wake_word(normalized):
            self.enter_active_mode()
            rest = strip_wake_word(normalized)
            if not rest:
                self.speak("Слушаю.")
                return True
            handled = self.process_command_text(rest, armed=True)
            if handled:
                self.sleep_mode()
            return handled

        if not self.is_active_mode():
            return False

        handled = self.process_command_text(normalized, armed=True)
        if handled:
            self.sleep_mode()
        return handled

    def process_command_text(self, text: str, armed: bool) -> bool:
        normalized = normalize_text(text)
        if not normalized:
            return False

        if normalized.startswith("поиск в google") or normalized.startswith("поиск на ютубе"):
            return self.handle_command(normalized, armed=armed)

        for part in split_commands(normalized):
            segment = part.strip()
            if not segment:
                continue
            if len(segment.split()) == 1 and token_wake_score(segment) >= WAKE_THRESHOLD:
                continue
            handled = self.handle_command(segment, armed=armed)
            if handled:
                return True
            self.logger.debug("Unhandled command segment: %s", segment)
        return False

    def handle_command(self, command: str, armed: bool = False) -> bool:
        normalized = normalize_text(command)
        if not normalized:
            return False

        for rule in COMMAND_REGISTRY:
            matched = self._match_rule(rule, normalized)
            if matched is None:
                continue
            handler = getattr(self, rule.handler, None)
            if handler is None:
                self.logger.warning("No handler for command rule %s", rule.name)
                return False
            return bool(handler(normalized, matched, armed, rule))

        math_handled, answer = try_math(normalized)
        if math_handled:
            self.speak(answer or "Не удалось вычислить выражение.")
            return True

        return False

    def _match_rule(self, rule: CommandRule, text: str) -> Optional[str]:
        compact = compact_text(text)
        if rule.kind == "exact":
            if text in rule.patterns:
                return text
            text_words = {token for token in normalize_text(text).split() if token}
            for pattern in rule.patterns:
                pattern_words = [token for token in normalize_text(pattern).split() if token]
                if pattern_words and all(
                    any(p == w or p in w or w in p for w in text_words)
                    for p in pattern_words
                ):
                    return pattern
            return None
        if rule.kind == "prefix":
            for pattern in rule.patterns:
                if text.startswith(pattern):
                    return pattern
            return None
        if rule.kind == "contains":
            if any(pattern in text for pattern in rule.patterns):
                return next((pattern for pattern in rule.patterns if pattern in text), rule.patterns[0])
            return None
        if rule.kind == "fuzzy":
            if any(pattern in text for pattern in rule.patterns):
                return next(pattern for pattern in rule.patterns if pattern in text)
            for word in text.split():
                word_c = compact_text(word)
                if not word_c:
                    continue
                for pattern in rule.patterns:
                    pattern_c = compact_text(pattern)
                    if not pattern_c:
                        continue
                    ratio = difflib.SequenceMatcher(None, word_c, pattern_c).ratio()
                    if pattern_c in word_c or word_c in pattern_c or ratio >= rule.threshold:
                        return pattern
            return None
        return None

    def handle_vk(self, text: str, matched: str, armed: bool, rule: CommandRule) -> bool:
        vk = int(rule.data["vk"])
        reply = str(rule.data.get("reply", "Готово."))
        press_vk(vk)
        self.speak(reply)
        return True

    def handle_hotkey(self, text: str, matched: str, armed: bool, rule: CommandRule) -> bool:
        hotkey = tuple(str(item) for item in rule.data.get("hotkey", ()))
        reply = str(rule.data.get("reply", "Готово."))
        if hotkey:
            hotkey_vk(*hotkey)
            self.speak(reply)
            return True
        return False

    def handle_open_url(self, text: str, matched: str, armed: bool, rule: CommandRule) -> bool:
        url = str(rule.data.get("url", ""))
        reply = str(rule.data.get("reply", "Открываю."))
        if url:
            open_url(url)
            self.speak(reply)
            return True
        return False

    def handle_search(self, text: str, matched: str, armed: bool, rule: CommandRule) -> bool:
        engine = str(rule.data.get("engine", "google"))
        prefix = matched
        query = strip_prefix(text, prefix)
        if not query:
            if engine == "google":
                self.speak("Что искать в Google?")
            else:
                self.speak("Что искать на YouTube?")
            return True
        if engine == "google":
            open_google_search(query)
        else:
            open_youtube_search(query)
        reply_prefix = str(rule.data.get("reply_prefix", "Ищу"))
        self.speak(f"{reply_prefix}: {query}")
        return True

    def handle_help(self, text: str, matched: str, armed: bool, rule: CommandRule) -> bool:
        self.speak(
            "Я умею открывать браузер, YouTube, почту, Проводник и Настройки, "
            "управлять вкладками, громкостью, скриншотами, временем, выключением, "
            "а также искать в Google и YouTube."
        )
        return True

    def handle_exit(self, text: str, matched: str, armed: bool, rule: CommandRule) -> bool:
        self.speak("Выключаюсь.")
        raise SystemExit

    def handle_cancel_shutdown(self, text: str, matched: str, armed: bool, rule: CommandRule) -> bool:
        cancel_shutdown()
        self.speak("Отмена выключения выполнена.")
        return True

    def handle_shutdown(self, text: str, matched: str, armed: bool, rule: CommandRule) -> bool:
        shutdown_pc()
        self.speak("Компьютер выключится через 60 секунд.")
        return True

    def handle_lock(self, text: str, matched: str, armed: bool, rule: CommandRule) -> bool:
        lock_pc()
        self.speak("Компьютер заблокирован.")
        return True

    def handle_screenshot(self, text: str, matched: str, armed: bool, rule: CommandRule) -> bool:
        path = save_screenshot()
        if path is None:
            self.speak("Не удалось сделать скриншот.")
        else:
            self.speak(f"Скриншот сохранён: {path.name}")
        return True

    def handle_volume(self, text: str, matched: str, armed: bool, rule: CommandRule) -> bool:
        vk = int(rule.data["vk"])
        reply = str(rule.data.get("reply", "Готово."))
        press_vk(vk)
        self.speak(reply)
        return True

    def handle_launch_app(self, text: str, matched: str, armed: bool, rule: CommandRule) -> bool:
        target = rule.data.get("target")
        reply = str(rule.data.get("reply", "Открываю."))
        if not (armed or wants_open(text) or text in rule.patterns):
            return False
        if isinstance(target, str):
            if target.startswith("http://") or target.startswith("https://"):
                open_url(target)
            else:
                run_quiet([target])
        elif isinstance(target, (list, tuple)):
            run_quiet([str(part) for part in target])
        else:
            return False
        self.speak(reply)
        return True

    def handle_open_explorer(self, text: str, matched: str, armed: bool, rule: CommandRule) -> bool:
        open_explorer()
        self.speak("Открываю Проводник.")
        return True

    def handle_open_settings(self, text: str, matched: str, armed: bool, rule: CommandRule) -> bool:
        open_settings()
        self.speak("Открываю Настройки.")
        return True

    def handle_time_date(self, text: str, matched: str, armed: bool, rule: CommandRule) -> bool:
        wants_time = any_term(text, ("который час", "сколько времени", "текущее время", "сколько сейчас"))
        wants_date = any_term(text, ("какая дата", "какое сегодня число", "дата", "число"))
        now = dt.datetime.now()
        if wants_time and wants_date:
            self.speak(f"Сейчас {now:%H:%M}, дата {now:%d.%m.%Y}.")
        elif wants_time:
            self.speak(f"Сейчас {now:%H:%M}.")
        else:
            self.speak(f"Сегодня {now:%d.%m.%Y}.")
        return True


def normalize_text(text: str) -> str:
    text = str(text).lower().replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-я\s]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def compact_text(text: str) -> str:
    return re.sub(r"[^0-9a-zа-я]+", "", normalize_text(text))


def any_term(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def token_wake_score(token: str) -> float:
    token = compact_text(token)
    if not token:
        return 0.0
    candidates = WAKE_ALIASES + WAKE_STEMS
    return max(difflib.SequenceMatcher(None, token, compact_text(alias)).ratio() for alias in candidates)


def has_wake_word(text: str, partial: bool = False) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    compact = compact_text(normalized)
    if any(compact_text(alias) in compact for alias in WAKE_ALIASES):
        return True
    threshold = WAKE_PARTIAL_THRESHOLD if partial else WAKE_THRESHOLD
    for token in normalized.split():
        if token_wake_score(token) >= threshold:
            return True
    return False


def strip_leading_fillers(text: str) -> str:
    words = normalize_text(text).split()
    while words and words[0] in FILLERS:
        words.pop(0)
    return " ".join(words)


def strip_wake_word(text: str) -> str:
    normalized = strip_leading_fillers(text)
    words = normalized.split()
    if not words:
        return ""
    if token_wake_score(words[0]) >= WAKE_THRESHOLD:
        words = words[1:]
    return strip_leading_fillers(" ".join(words))


def split_commands(text: str) -> list[str]:
    text = normalize_text(text)
    if not text:
        return []
    parts = re.split(r"\s*(?:[,;]|\bи\b|\bпотом\b|\bзатем\b|\bthen\b)\s*", text)
    return [part.strip() for part in parts if part.strip()]


def strip_prefix(text: str, prefix: str) -> str:
    text = normalize_text(text)
    prefix = normalize_text(prefix)
    if text.startswith(prefix):
        return text[len(prefix):].strip()
    return re.sub(rf"^{re.escape(prefix)}\s*", "", text).strip()


def wants_open(text: str) -> bool:
    return any_term(normalize_text(text), OPEN_HINTS)


def press_vk(vk: int) -> None:
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def hotkey_vk(*keys: str) -> None:
    normalized = tuple("winleft" if key.lower() == "win" else key for key in keys)
    if not normalized:
        return
    time.sleep(0.05)
    for key in normalized:
        pyautogui.keyDown(key)
        time.sleep(0.05)
    for key in reversed(normalized):
        pyautogui.keyUp(key)
        time.sleep(0.05)


def open_url(url: str) -> None:
    webbrowser.open(url, new=2)


def open_google_search(query: str) -> None:
    open_url(f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}")


def open_youtube_search(query: str) -> None:
    open_url(f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(query)}")


def open_explorer() -> None:
    subprocess.Popen(["explorer.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def open_settings() -> None:
    subprocess.Popen(["cmd", "/c", "start", "", "ms-settings:"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def lock_pc() -> None:
    ctypes.windll.user32.LockWorkStation()


def shutdown_pc() -> None:
    subprocess.Popen(["shutdown", "/s", "/t", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cancel_shutdown() -> None:
    subprocess.Popen(["shutdown", "/a"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_quiet(command: list[str]) -> None:
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def find_desktop() -> Path:
    home = Path.home()
    candidates = [home / "Desktop", home / "OneDrive" / "Desktop"]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return home


def save_screenshot() -> Optional[Path]:
    try:
        desktop = find_desktop()
        folder = desktop / "FridayAssistantShots"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"screenshot_{dt.datetime.now():%Y%m%d_%H%M%S}.png"
        shot = pyautogui.screenshot()
        shot.save(path)
        return path
    except Exception:
        return None


def safe_eval(node: ast.AST):
    if isinstance(node, ast.Expression):
        return safe_eval(node.body)
    if isinstance(node, ast.BinOp) and type(node.op) in {ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow}:
        left = safe_eval(node.left)
        right = safe_eval(node.right)
        return eval(compile(ast.Expression(ast.BinOp(ast.Constant(left), node.op, ast.Constant(right))), "<math>", "eval"))
    if isinstance(node, ast.UnaryOp) and type(node.op) in {ast.UAdd, ast.USub}:
        operand = safe_eval(node.operand)
        return +operand if isinstance(node.op, ast.UAdd) else -operand
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    raise ValueError("Unsupported expression")


def words_to_digits(text: str) -> str:
    normalized = normalize_text(text)
    for src, dst in NUMBER_REPLACEMENTS:
        normalized = normalized.replace(src, dst)
    normalized = normalized.replace(",", ".")
    normalized = re.sub(r"\bсколько будет\b", "", normalized)
    normalized = re.sub(r"\bпосчитай\b", "", normalized)
    normalized = re.sub(r"\bвычисли\b", "", normalized)
    normalized = re.sub(r"\bскобка\b", "", normalized)
    return normalized.strip()


def try_math(text: str) -> tuple[bool, Optional[str]]:
    expr = words_to_digits(text)
    if not expr:
        return False, None
    if not re.search(r"[0-9]+|[\+\-\*/%()]", expr):
        return False, None
    try:
        tree = ast.parse(expr, mode="eval")
        value = safe_eval(tree)
    except Exception:
        return False, None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return True, str(value)
