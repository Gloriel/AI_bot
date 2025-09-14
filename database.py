import json
import os
import asyncio
import logging
from datetime import datetime
from telegram import Bot

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

# --- Проверка подписки — ИСПРАВЛЕННАЯ ВЕРСИЯ ---
async def check_subscription(user_id: int, channel_id, bot: Bot) -> bool:
    """
    Возвращает True, если пользователь подписан на канал.
    
    Поддерживает:
    - Числовой ID канала (например: -100123456789)
    - Юзернейм канала (например: "@mychannel")
    
    При любых ошибках (нет доступа, канал удалён, бот не админ и т.д.) — возвращает False.
    Это безопасный fail-closed — гарантирует, что только подписчики получают доступ.
    """
    try:
        # Если channel_id — это строка и начинается с '@', преобразуем в числовой ID
        if isinstance(channel_id, str) and channel_id.startswith('@'):
            chat = await bot.get_chat(channel_id)
            channel_id = chat.id

        # Теперь channel_id должен быть int
        if not isinstance(channel_id, int):
            logger.warning(f"Invalid channel_id type: {type(channel_id)}, value: {channel_id}")
            return False

        # Получаем статус пользователя в канале
        member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        return member.status in ['member', 'administrator', 'creator']

    except Exception as e:
        # Любая ошибка: бот не может проверить подписку → считаем, что пользователь НЕ подписан
        logger.warning(f"Subscription check failed for user {user_id} in channel {channel_id}: {type(e).__name__}: {e}")
        return False  # 🔴 FAIL-CLOSED — безопасно!

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
) -> tuple[bool, float]:
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