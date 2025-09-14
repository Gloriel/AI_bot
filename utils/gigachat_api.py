import os
import httpx
import asyncio
from cachetools import TTLCache
from datetime import datetime

# Кэш для access_token (живет ~29 минут — буфер перед истечением)
token_cache = TTLCache(maxsize=1, ttl=1740)  # 29 минут

async def get_gigachat_token() -> str:
    """
    Получает актуальный access_token для GigaChat API.
    Использует кэш, чтобы не запрашивать токен каждый раз.
    """
    cached_token = token_cache.get('access_token')
    if cached_token:
        return cached_token

    client_id = os.getenv('GIGACHAT_CLIENT_ID')
    auth_key = os.getenv('GIGACHAT_AUTHORIZATION_KEY')
    scope = os.getenv('GIGACHAT_SCOPE', 'GIGACHAT_API_PERS')

    if not auth_key:
        raise ValueError("GIGACHAT_AUTHORIZATION_KEY not found in environment variables")

    url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Accept': 'application/json',
        'RqUID': client_id or "default-uuid",
        'Authorization': f'Basic {auth_key}'
    }
    data = {'scope': scope}

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
                response = await client.post(url, headers=headers, data=data)
                response.raise_for_status()
                token_data = response.json()
                access_token = token_data.get('access_token')
                if access_token:
                    token_cache['access_token'] = access_token
                    print(f"✅ [GigaChat] Токен получен и закэширован ({datetime.now().strftime('%H:%M:%S')})")
                    return access_token
                else:
                    raise Exception("Access token not found in response")

        except (httpx.NetworkError, httpx.TimeoutException) as e:
            if attempt == 2:
                raise Exception("Сеть: не удалось получить токен после 3 попыток") from e
            await asyncio.sleep(1 + attempt)

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                # Сбрасываем кэш при ошибке авторизации
                token_cache.pop('access_token', None)
            if attempt == 2:
                raise Exception(f"HTTP {e.response.status_code}: {e.response.text[:200]}") from e
            await asyncio.sleep(1 + attempt)

        except Exception as e:
            if attempt == 2:
                raise Exception(f"Неизвестная ошибка при получении токена: {str(e)}") from e
            await asyncio.sleep(1 + attempt)

    raise Exception("Недостижимо")

async def gigachat_request(messages: list, temperature: float = 0.7, max_tokens: int = 200) -> str:
    """
    Отправляет запрос к GigaChat API и возвращает ответ.
    Поддерживает retry и валидацию ответа.
    """
    for attempt in range(3):
        try:
            access_token = await get_gigachat_token()
            url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

            headers = {
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'Authorization': f'Bearer {access_token}'
            }

            payload = {
                "model": "GigaChat",
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }

            async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
                response = await client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()

                # Валидация структуры
                if not isinstance(data, dict) or 'choices' not in data or not data['choices']:
                    raise ValueError("Некорректная структура ответа от GigaChat")

                choice = data['choices'][0]
                msg = choice.get('message') or {}
                content = (msg.get('content') or "").strip()
                if not content or len(content) < 5:
                    raise ValueError("Ответ слишком короткий")

                print(f"✅ [GigaChat] Успешный ответ получен (попытка {attempt + 1})")
                return content

        except (httpx.NetworkError, httpx.TimeoutException, ValueError) as e:
            if attempt == 2:
                raise Exception(f"Ошибка генерации: {str(e)}") from e
            await asyncio.sleep(2 ** attempt)  # экспоненциальная задержка: 1, 2, 4 сек

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                # Токен мог протухнуть — сбросим
                token_cache.pop('access_token', None)
            if attempt == 2:
                raise Exception(f"API ошибка {e.response.status_code}: {e.response.text[:200]}") from e
            await asyncio.sleep(2 ** attempt)

        except Exception as e:
            if attempt == 2:
                raise Exception(f"Необработанная ошибка: {str(e)}") from e
            await asyncio.sleep(2 ** attempt)

    raise Exception("Недостижимо")
