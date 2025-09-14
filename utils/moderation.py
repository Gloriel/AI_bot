# Модерация отключена/упрощена для GigaChat, так как отдельного API нет.
# Можно добавить простую фильтрацию по запрещенным словам при необходимости.

async def moderate_text(text: str, **kwargs) -> bool:
    """
    Упрощенная проверка текста.
    Всегда возвращает True (текст прошел проверку),
    кроме очевидных чёрных слов из списка.
    """
    simple_blacklist = ['спам', 'мошенничество']  # расширяй при необходимости
    lower_text = (text or "").lower()
    for word in simple_blacklist:
        if word in lower_text:
            print(f"⚠️ Text blocked by simple filter: {text[:50]}...")
            return False
    return True
