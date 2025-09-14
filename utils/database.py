import json
import os
import asyncio
import logging
import time
from datetime import datetime
from typing import Optional, Tuple, Dict, Tuple as Tup
from telegram import Bot
from telegram.error import Forbidden, BadRequest, TimedOut, NetworkError

# Настройка логгера — согласован с основным ботом
logger = logging.getLogger('database')
logger.setLevel(logging.WARNING)
if not logger.handlers:
    handler = logging.FileHandler('logs/database_errors.log', encoding='utf-8')
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

# Асинхронная блокировка для файловых операций
file_lock = asyncio.Lock()

DATA_DIR = 'data'
CREDITS_FILE = os.path.join(DATA_DIR, 'user_credits.json')  # оставлен для совместимости (не используется)
BANNED_FILE = os.path.join(DATA_DIR, 'banned_users.txt')
RATE_FILE = os.path.join(DATA_DIR, 'rate_limits.json')     # токен-бакет

def ensure_data_files():
    """Создает все необходимые файлы данных если их нет"""
    os.makedirs(DATA_DIR, exist_ok=True)

    if not os.path.exists(CREDITS_FILE):
        try:
            with open(CREDITS_FILE, 'w', encoding='utf-8') as f:
                json.dump({}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Init file error {CREDITS_FILE}: {e}")

    if not os.path.exists(BANNED_FILE):
        try:
            with open(BANNED_FILE, 'w', encoding='utf-8') as f:
                pass
        except Exception as e:
            logger.warning(f"Init file error {BANNED_FILE}: {e}")

    if not os.path.exists(RATE_FILE):
        try:
            with open(RATE_FILE, 'w', encoding='utf-8') as f:
                json.dump({}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Init file error {RATE_FILE}: {e}")

def init_user_data():
    """Инициализация данных пользователя"""
    ensure_data_files()
    logger.info("📁 Data files initialized")

async def load_json_data(filename: str, default=None):
    try:
        ensure_data_files()
        async with file_lock:
            if os.path.exists(filename) and os.path.getsize(filename) > 0:
                with open(filename, 'r', encoding='utf-8') as f:
                    return json.load(f)
    except (json.JSONDecodeError, Exception) as e:
        logger.warning(f"Load error {filename}: {e}")
        async with file_lock:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(default or {}, f, ensure_ascii=False, indent=2)
    return default or {}

async def save_json_data(filename: str, data):
    try:
        ensure_data_files()
        async with file_lock:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Save error {filename}: {e}")

# --- Кредиты (оставлено для совместимости с ранними версиями) ---
def get_user_credits(user_id: int) -> int:
    """Совместимость: возвращает фиктивные кредиты (не используются ботом)."""
    ensure_data_files()
    try:
        with open(CREDITS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    return int(data.get(str(user_id), {}).get("credits", 999999))

def update_user_credits(user_id: int, credits: int):
    """Совместимость: записывает, но бот лимиты больше не читает из кредитов."""
    ensure_data_files()
    try:
        with open(CREDITS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data[str(user_id)] = {"credits": max(0, int(credits)), "last_reset": datetime.now().isoformat()}
    try:
        with open(CREDITS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Update credits error: {e}")

# --- Бан-лист ---
def is_user_banned(user_id: int) -> bool:
    ensure_data_files()
    try:
        with open(BANNED_FILE, 'r', encoding='utf-8') as f:
            banned_users = [line.strip() for line in f if line.strip()]
            return str(user_id) in banned_users
    except Exception as e:
        logger.warning(f"Banned check error: {e}")
    return False

def add_banned_user(user_id: int):
    ensure_data_files()
    try:
        with open(BANNED_FILE, 'a', encoding='utf-8') as f:
            f.write(f"{user_id}\n")
    except Exception as e:
        logger.warning(f"Ban error: {e}")

# --- Проверка подписки — кэш с TTL ---
ALLOWED_STATUSES = {'member', 'administrator', 'creator', 'restricted'}

def _env_flag(name: str, default: bool = False) -> bool:
    v = os.getenv(name, str(default)).strip().lower()
    return v in ('1', 'true', 'yes', 'y', 'on')

FAIL_OPEN = _env_flag('SUBSCRIPTION_FAIL_OPEN', False)
SUB_CACHE_TTL = int(os.getenv('SUBSCRIPTION_CACHE_TTL', '600'))  # сек, по умолчанию 10 минут

# (user_id, chat_id) -> (status_bool, timestamp)
_SUB_CACHE: Dict[Tup[int, int], Tup[bool, float]] = {}
# кэш для резолвинга @username -> chat_id
_CHAT_RESOLVE_CACHE: Dict[str, int] = {}

async def _resolve_chat_id(bot: Bot, channel_id) -> Optional[int]:
    """Возвращает числовой chat_id по @username или числу; None при ошибке."""
    try:
        if isinstance(channel_id, str) and channel_id.startswith('@'):
            # кэшируем резолв
            if channel_id in _CHAT_RESOLVE_CACHE:
                return _CHAT_RESOLVE_CACHE[channel_id]
            chat = await bot.get_chat(channel_id)
            _CHAT_RESOLVE_CACHE[channel_id] = chat.id
            return chat.id
        if isinstance(channel_id, str):
            channel_id = int(channel_id)
        return int(channel_id)
    except Exception as e:
        logger.warning(f"resolve chat_id failed for {channel_id}: {type(e).__name__}: {e}")
        return None

def _cache_get(user_id: int, chat_id: int) -> Optional[bool]:
    key = (user_id, chat_id)
    item = _SUB_CACHE.get(key)
    if not item:
        return None
    status, ts = item
    if time.time() - ts <= SUB_CACHE_TTL:
        return status
    # просрочен
    _SUB_CACHE.pop(key, None)
    return None

def _cache_set(user_id: int, chat_id: int, status: bool):
    _SUB_CACHE[(user_id, chat_id)] = (status, time.time())

async def check_subscription(user_id: int, channel_id, bot: Bot) -> bool:
    """
    True, если пользователь подписан на канал.
    Поддерживает channel_id как '-100…' так и '@username'.
    Кэширует положительные статусы на SUBSCRIPTION_CACHE_TTL (по умолчанию 10 минут).
    Если ранее было False или кэш истёк — делаем живую проверку.
    """
    chat_id = await _resolve_chat_id(bot, channel_id)
    if chat_id is None:
        # Конфиг битый — при FAIL_OPEN=True не ломаем UX
        return True if FAIL_OPEN else False

    # 1) быстрый хит из кэша: если True — сразу возвращаем
    cached = _cache_get(user_id, chat_id)
    if cached is True:
        return True
    # Если cached False/None — идём в Telegram

    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        ok = getattr(member, "status", None) in ALLOWED_STATUSES
        _cache_set(user_id, chat_id, ok)
        return ok

    except Forbidden as e:
        logger.warning(f"check_subscription Forbidden for {chat_id}: {e}")
        return True if FAIL_OPEN else False

    except (TimedOut, NetworkError) as e:
        logger.warning(f"check_subscription network issue: {type(e).__name__}: {e}")
        return True if FAIL_OPEN else False

    except BadRequest as e:
        logger.warning(f"check_subscription BadRequest for {chat_id}: {e}")
        return False

    except Exception as e:
        logger.warning(f"check_subscription unexpected {type(e).__name__}: {e}")
        return False

# --- Token Bucket per user (персистентный) ---
def _load_rate_state() -> dict:
    ensure_data_files()
    try:
        with open(RATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def _save_rate_state(state: dict):
    ensure_data_files()
    try:
        with open(RATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Save error {RATE_FILE}: {e}")

def allow_request_token_bucket(
    user_id: int,
    capacity: float = 30.0,          # burst: сколько можно «залить» подряд
    refill_per_sec: float = 0.5,     # скорость пополнения: токенов/сек (≈ 30 ток/мин)
    cost: float = 1.0
) -> Tuple[bool, float]:
    """
    Возвращает (allowed, wait_seconds).
    Если allowed=False — через сколько секунд попытаться снова.
    Хранит состояние в data/rate_limits.json.
    """
    state = _load_rate_state()
    user_key = str(user_id)
    now = time.time()

    user_state = state.get(user_key, {"tokens": capacity, "updated": now})
    tokens = float(user_state.get("tokens", capacity))
    updated = float(user_state.get("updated", now))

    # Пополнение
    delta = max(0.0, now - updated)
    tokens = min(capacity, tokens + delta * refill_per_sec)

    if tokens >= cost:
        tokens -= cost
        state[user_key] = {"tokens": tokens, "updated": now}
        _save_rate_state(state)
        return True, 0.0
    else:
        deficit = cost - tokens
        wait = deficit / refill_per_sec if refill_per_sec > 0 else 5.0
        state[user_key] = {"tokens": tokens, "updated": now}
        _save_rate_state(state)
        return False, wait
