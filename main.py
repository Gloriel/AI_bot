import os
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional
from collections import defaultdict
from enum import Enum
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot, Message
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# Тексты и генератор промптов
import texts
from utils.database import (
    check_subscription,
    is_user_banned,
    init_user_data,
    allow_request_token_bucket,
)
from utils.moderation import moderate_text
from utils.prompt_engine import generate_enhanced_prompt

# ========================
# Инициализация окружения
# ========================
load_dotenv()

# Состояния диалога
SELECTING_CATEGORY, TYPING_PROMPT = range(2)

# Логи и директории
os.makedirs('logs', exist_ok=True)
os.makedirs('data', exist_ok=True)
os.makedirs('images', exist_ok=True)
init_user_data()

# Настройка логирования
bot_logger = logging.getLogger('bot_errors')
bot_logger.setLevel(logging.ERROR)
bot_handler = logging.FileHandler('logs/bot_errors.log', encoding='utf-8')
bot_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
bot_logger.addHandler(bot_handler)

user_logger = logging.getLogger('user_requests')
user_logger.setLevel(logging.INFO)
user_handler = logging.FileHandler('logs/user_requests.log', encoding='utf-8')
user_handler.setFormatter(logging.Formatter('%(asctime)s - User %(message)s'))
user_logger.addHandler(user_handler)

# Мягкая защита от дабл-кликов
user_last_request: Dict[int, datetime] = defaultdict(lambda: datetime.min)

# ========================
# Вспомогательные элементы
# ========================
class Stage(str, Enum):
    COURSE = "course"
    STAGE1 = "stage1_prompt"
    STAGE2 = "stage2_book"
    STAGE3 = "stage3_code"
    STAGE4 = "stage4_image"
    STAGE5 = "stage5_video"

# Соответствие этапов и картинок
STAGE_IMAGES = {
    Stage.COURSE: 'images/prompt6.jpg',
    Stage.STAGE1: 'images/prompt1.jpg',
    Stage.STAGE2: 'images/prompt2.jpg',
    Stage.STAGE3: 'images/prompt3.jpg',
    Stage.STAGE4: 'images/prompt4.jpg',
    Stage.STAGE5: 'images/prompt5.jpg'
}

def check_environment_variables():
    """Проверка обязательных переменных окружения"""
    required_vars = ['BOT_TOKEN', 'GIGACHAT_AUTHORIZATION_KEY', 'CHANNEL_ID']
    missing = [v for v in required_vars if not os.getenv(v)]
    if missing:
        raise EnvironmentError(f"Missing env vars: {', '.join(missing)}")
    
    try:
        int(os.getenv('CHANNEL_ID'))
    except (ValueError, TypeError):
        raise EnvironmentError("CHANNEL_ID must be a valid integer")

def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Быстрый старт для Этапа 1: выбор типа запроса"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(texts.BTN_QUESTION, callback_data='category_question'),
            InlineKeyboardButton(texts.BTN_EVENT, callback_data='category_event'),
            InlineKeyboardButton(texts.BTN_ADVICE, callback_data='category_advice')
        ],
        [
            InlineKeyboardButton(texts.BTN_BOOK, callback_data='open_stage2'),
            InlineKeyboardButton(texts.BTN_CREATE_BOT, callback_data='open_stage3')
        ],
        [InlineKeyboardButton("🏠 В меню курса", callback_data=Stage.COURSE.value)]
    ])

def get_stage1_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для этапа 1"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(texts.BTN_QUESTION, callback_data='category_question'),
            InlineKeyboardButton(texts.BTN_EVENT, callback_data='category_event'),
            InlineKeyboardButton(texts.BTN_ADVICE, callback_data='category_advice')
        ],
        [InlineKeyboardButton("🏠 В меню курса", callback_data=Stage.COURSE.value)]
    ])

def get_course_menu_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура главного меню курса"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1️⃣ Превращаем запрос в идеальный промпт", callback_data=Stage.STAGE1.value)],
        [InlineKeyboardButton("2️⃣ Книга о себе", callback_data=Stage.STAGE2.value)],
        [InlineKeyboardButton("3️⃣ Конструктор кода", callback_data=Stage.STAGE3.value)],
        [InlineKeyboardButton("4️⃣ Картинки: идеальный промпт", callback_data=Stage.STAGE4.value)],
        [InlineKeyboardButton("5️⃣ Видео: сториборд-промпт", callback_data=Stage.STAGE5.value)],
    ])

def get_after_result_keyboard(next_stage: Optional[Stage] = None) -> InlineKeyboardMarkup:
    """Клавиатура после генерации результата"""
    rows = []
    if next_stage:
        rows.append([InlineKeyboardButton("➡️ К следующему этапу", callback_data=next_stage.value)])
    rows.append([InlineKeyboardButton("🏠 В меню курса", callback_data=Stage.COURSE.value)])
    rows.append([InlineKeyboardButton(texts.BTN_NEW, callback_data='new_prompt')])
    return InlineKeyboardMarkup(rows)

def get_book_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для этапа книги"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Запустить", callback_data='start_book')],
        [InlineKeyboardButton("🏠 В меню курса", callback_data=Stage.COURSE.value)]
    ])

def get_bot_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для этапа создания бота"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Запустить конструктор кода", callback_data='create_bot')],
        [InlineKeyboardButton("🏠 В меню курса", callback_data=Stage.COURSE.value)]
    ])

@asynccontextmanager
async def typing_action(app: Application, chat_id: int):
    """Контекстный менеджер для показа действия печати"""
    try:
        await app.bot.send_chat_action(chat_id, action=ChatAction.TYPING)
        yield
    finally:
        pass

# ========================
# Класс бота
# ========================
class PromptBot:
    def __init__(self):
        self.token = os.getenv('BOT_TOKEN')
        self.channel_id = int(os.getenv('CHANNEL_ID'))
        self.application = Application.builder().token(self.token).build()
        self.bot_instance = Bot(token=self.token)

    async def send_html_message(self, chat_id: int, text: str, reply_markup=None, **kwargs) -> Optional[Message]:
        """Унифицированная отправка HTML сообщений с fallback"""
        try:
            return await self.application.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode='HTML',
                **kwargs
            )
        except Exception as e:
            bot_logger.error(f"HTML send error for chat {chat_id}: {type(e).__name__}")
            # Fallback: убираем HTML разметку
            clean_text = text.replace('<b>', '').replace('</b>', '').replace('<code>', '').replace('</code>', '')
            clean_text = clean_text.replace('<i>', '').replace('</i>', '').replace('<a href="', '').replace('">', '').replace('</a>', '')
            
            try:
                return await self.application.bot.send_message(
                    chat_id=chat_id,
                    text=clean_text[:4096],
                    reply_markup=reply_markup,
                    **kwargs
                )
            except Exception:
                bot_logger.error("Fallback send also failed")
                return None

    async def send_html_with_photo(self, chat_id: int, photo_path: str, caption: str, reply_markup=None, **kwargs) -> Optional[Message]:
        """Отправка сообщения с картинкой"""
        try:
            with open(photo_path, 'rb') as photo:
                return await self.application.bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=caption[:1024],
                    reply_markup=reply_markup,
                    parse_mode='HTML',
                    **kwargs
                )
        except FileNotFoundError:
            return await self.send_html_message(chat_id, caption, reply_markup, **kwargs)
        except Exception as e:
            bot_logger.error(f"Photo send error: {type(e).__name__}")
            return await self.send_html_message(chat_id, caption, reply_markup, **kwargs)

    async def show_generation_status(self, chat_id: int, message: str = "⏳ Генерируем ваш промпт...") -> Optional[Message]:
        """Показать статус генерации"""
        return await self.send_html_message(chat_id, message)

    async def send_prompt_template(self, user_id: int, prompt_text: str, footer: Optional[str] = None, 
                                 category: Optional[str] = None, next_stage: Optional[Stage] = None):
        """Отправка сгенерированного промпта"""
        full_message = texts.SUCCESSFUL_GENERATION_TEMPLATE.format(prompt_text)
        full_message += (footer or texts.GENERATION_FOOTER)
        full_message += texts.CONTINUE_FOOTER

        if len(full_message) > 4096:
            full_message = full_message[:4090] + " [...]"

        await self.send_html_message(user_id, full_message, get_after_result_keyboard(next_stage))

    async def check_user_access(self, user_id: int) -> bool:
        """Проверка доступа пользователя"""
        if is_user_banned(user_id):
            await self.send_html_message(user_id, "Доступ к боту ограничен.")
            return False
        
        if not await check_subscription(user_id, self.channel_id, self.bot_instance):
            keyboard = [[InlineKeyboardButton("✅ Я подписался", callback_data="check_subscription")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await self.send_html_with_photo(user_id, STAGE_IMAGES[Stage.COURSE], 
                                          texts.NOT_SUBSCRIBED_MESSAGE, reply_markup)
            return False
        
        return True

    async def check_rate_limit(self, user_id: int) -> bool:
        """Проверка ограничения запросов"""
        allowed, wait_sec = allow_request_token_bucket(
            user_id=user_id,
            capacity=float(os.getenv("TB_CAPACITY", 30)),
            refill_per_sec=float(os.getenv("TB_REFILL_PER_SEC", 0.5)),
            cost=1.0
        )
        
        if not allowed:
            await self.send_html_message(user_id, f"🚦 Много запросов подряд. Повторите через ~{int(wait_sec)} сек.")
            return False
        
        return True

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Обработчик команды /start"""
        try:
            user_id = update.effective_user.id

            if not await self.check_user_access(user_id):
                return ConversationHandler.END

            context.user_data.clear()
            await self.send_html_with_photo(user_id, STAGE_IMAGES[Stage.COURSE], 
                                          texts.COURSE_WELCOME, get_course_menu_keyboard())
            return SELECTING_CATEGORY

        except Exception as e:
            bot_logger.error(f"Start error: {type(e).__name__} - {str(e)[:200]}")
            await self.send_html_message(update.effective_chat.id, "Ошибка. Попробуйте позже.")
            return ConversationHandler.END

    async def menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Обработчик команды /menu"""
        user_id = update.effective_user.id
        await self.send_html_with_photo(user_id, STAGE_IMAGES[Stage.COURSE], 
                                      texts.COURSE_WELCOME, get_course_menu_keyboard())
        return SELECTING_CATEGORY

    async def handle_category_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Обработка выбора категории Этапа 1 и прямых запусков"""
        try:
            query = update.callback_query
            await query.answer()
            user_id = query.from_user.id

            if not await self.check_user_access(user_id):
                return SELECTING_CATEGORY

            if not await self.check_rate_limit(user_id):
                return SELECTING_CATEGORY

            # Карта категорий
            category_map = {
                'category_question': ('question', texts.QUESTION_PROMPT),
                'category_event': ('event', texts.EVENT_PROMPT),
                'category_advice': ('advice', texts.ADVICE_PROMPT),
                'start_book': ('book', "📖 Готовим промпт для вашей книги..."),
                'create_bot': ('bot', "🤖 Готовим инструкцию по созданию бота...")
            }

            # Прямой запуск книга/бот
            if query.data in ['start_book', 'create_bot']:
                stage = Stage.STAGE2 if query.data == 'start_book' else Stage.STAGE3
                context.user_data['stage'] = stage
                category, _ = category_map[query.data]
                context.user_data['category'] = category

                status_msg = await self.show_generation_status(
                    user_id,
                    "🧠 <b>Принял ваш запрос!</b>\n\n"
                    "Идет генерация идеального промпта... Это займет 5–10 секунд.\n"
                    "Пока ждете, можете подумать, как будете использовать результат! 💡"
                )

                try:
                    async with typing_action(self.application, user_id):
                        enhanced_prompt = await generate_enhanced_prompt(category=category, user_input="")

                    if "Не удалось сгенерировать" in enhanced_prompt or len(enhanced_prompt.strip()) < 10:
                        await self.send_html_message(user_id, texts.API_ERROR_MESSAGE)
                        return SELECTING_CATEGORY

                    next_stage = Stage.STAGE3 if category == 'book' else Stage.STAGE4
                    footer = texts.BOOK_GENERATION_FOOTER if category == 'book' else texts.GENERATION_FOOTER
                    await self.send_prompt_template(user_id, enhanced_prompt, footer, category, next_stage)

                    if status_msg:
                        try:
                            await context.bot.delete_message(chat_id=user_id, message_id=status_msg.message_id)
                        except Exception:
                            pass

                except Exception as e:
                    bot_logger.error(f"Direct generation error: {type(e).__name__} - {str(e)[:200]}")
                    await self.send_html_message(user_id, texts.API_ERROR_MESSAGE)

                return SELECTING_CATEGORY

            # Этап 1: выбор подкатегории (вопрос/событие/совет)
            elif query.data in ['category_question', 'category_event', 'category_advice']:
                context.user_data['stage'] = Stage.STAGE1
                category, prompt_text = category_map[query.data]
                context.user_data['category'] = category
                
                await self.send_html_with_photo(user_id, STAGE_IMAGES[Stage.STAGE1], 
                                              texts.STAGE_INSTRUCTIONS["stage1_prompt"], get_stage1_keyboard())
                await self.send_html_message(user_id, prompt_text)
                return TYPING_PROMPT

            return SELECTING_CATEGORY

        except Exception as e:
            bot_logger.error(f"Category error: {type(e).__name__}")
            await self.send_html_message(update.effective_chat.id, "Ошибка. Попробуйте снова.")
            return SELECTING_CATEGORY

    async def handle_button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Обработка callback кнопок (навигация по меню)"""
        try:
            query = update.callback_query
            await query.answer()
            user_id = query.from_user.id
            data = query.data

            if data == "check_subscription":
                is_subscribed = await check_subscription(user_id, self.channel_id, self.bot_instance)
                if is_subscribed:
                    context.user_data.clear()
                    await query.delete_message()
                    await self.send_html_with_photo(user_id, STAGE_IMAGES[Stage.COURSE], 
                                                  texts.COURSE_WELCOME, get_course_menu_keyboard())
                    return SELECTING_CATEGORY
                else:
                    keyboard = [[InlineKeyboardButton("✅ Я подписался", callback_data="check_subscription")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await query.edit_message_caption(
                        caption=texts.NOT_SUBSCRIBED_MESSAGE,
                        reply_markup=reply_markup,
                        parse_mode='HTML'
                    )
                    return SELECTING_CATEGORY

            elif data == 'copy_prompt':
                await self.send_html_message(user_id, "👉 Нажмите на текст выше → «Копировать». Так работает Telegram!")
                return SELECTING_CATEGORY

            elif data == 'new_prompt':
                # Мягкая защита от быстрых кликов
                now = datetime.now()
                if now - user_last_request[user_id] < timedelta(seconds=1):
                    await self.send_html_message(user_id, "⏳ Подождите 1 секунду между запросами.")
                    return TYPING_PROMPT
                user_last_request[user_id] = now

                context.user_data.clear()
                context.user_data['stage'] = Stage.STAGE1
                await self.send_html_with_photo(user_id, STAGE_IMAGES[Stage.STAGE1], 
                                              texts.WELCOME_MESSAGE, get_main_menu_keyboard())
                return TYPING_PROMPT

            elif data == Stage.COURSE.value:
                context.user_data.clear()
                await query.delete_message()
                await self.send_html_with_photo(user_id, STAGE_IMAGES[Stage.COURSE], 
                                              texts.COURSE_WELCOME, get_course_menu_keyboard())
                return SELECTING_CATEGORY

            elif data == Stage.STAGE1.value:
                context.user_data['stage'] = Stage.STAGE1
                await query.delete_message()
                await self.send_html_with_photo(user_id, STAGE_IMAGES[Stage.STAGE1], 
                                              texts.STAGE_INSTRUCTIONS["stage1_prompt"], get_stage1_keyboard())
                await self.send_html_message(user_id, texts.QUESTION_PROMPT)
                return TYPING_PROMPT

            elif data == 'open_stage2' or data == Stage.STAGE2.value:
                context.user_data['stage'] = Stage.STAGE2
                await query.delete_message()
                await self.send_html_with_photo(user_id, STAGE_IMAGES[Stage.STAGE2], 
                                              texts.STAGE_INSTRUCTIONS["stage2_book"], get_book_keyboard())
                return SELECTING_CATEGORY

            elif data == 'open_stage3' or data == Stage.STAGE3.value:
                context.user_data['stage'] = Stage.STAGE3
                await query.delete_message()
                await self.send_html_with_photo(user_id, STAGE_IMAGES[Stage.STAGE3], 
                                              texts.STAGE_INSTRUCTIONS["stage3_code"], get_bot_keyboard())
                return SELECTING_CATEGORY

            elif data == Stage.STAGE4.value:
                context.user_data['stage'] = Stage.STAGE4
                await query.delete_message()
                await self.send_html_with_photo(user_id, STAGE_IMAGES[Stage.STAGE4], 
                                              texts.STAGE_INSTRUCTIONS["stage4_image"] + "\n\n📝 Опишите сцену одной фразой:")
                return TYPING_PROMPT

            elif data == Stage.STAGE5.value:
                context.user_data['stage'] = Stage.STAGE5
                await query.delete_message()
                await self.send_html_with_photo(user_id, STAGE_IMAGES[Stage.STAGE5], 
                                              texts.STAGE_INSTRUCTIONS["stage5_video"] + "\n\n📝 Опишите идею ролика одной фразой:")
                return TYPING_PROMPT

            return SELECTING_CATEGORY

        except Exception as e:
            bot_logger.error(f"Callback error: {type(e).__name__}")
            await self.send_html_message(user_id, "Ошибка. Попробуйте снова.")
            return SELECTING_CATEGORY

    async def handle_user_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Обработка текстового ввода пользователя"""
        try:
            user_id = update.message.from_user.id
            user_input = (update.message.text or "").strip()

            # Проверка быстрых запросов
            now = datetime.now()
            if now - user_last_request[user_id] < timedelta(seconds=1):
                await self.send_html_message(user_id, "⏳ Подождите 1 секунду между запросами.")
                return TYPING_PROMPT
            user_last_request[user_id] = now

            # Валидация ввода
            if not user_input:
                await self.send_html_message(user_id, "Введите текст запроса.")
                return TYPING_PROMPT
            if len(user_input) > 500:
                await self.send_html_message(user_id, "Слишком длинный запрос. Макс. 500 симв.")
                return TYPING_PROMPT

            user_logger.info(f"{user_id}: {user_input[:100]}...")

            # Проверка доступа
            if not await self.check_user_access(user_id):
                return SELECTING_CATEGORY

            if not await self.check_rate_limit(user_id):
                return TYPING_PROMPT

            # Модерация текста
            if not await moderate_text(text=user_input):
                await self.send_html_message(user_id, texts.MODERATION_FAILED_MESSAGE)
                return TYPING_PROMPT

            # Статус генерации
            status_msg = await self.show_generation_status(
                user_id,
                "🧠 <b>Принял ваш запрос!</b>\n\n"
                "Идет генерация идеального промпта... Это займет 5–10 секунд.\n"
                "Пока ждете, можете подумать, как будете использовать результат! 💡"
            )

            # Определение стадии и категории
            stage: Stage = context.user_data.get('stage', Stage.STAGE1)
            category = context.user_data.get('category', 'question')
            
            if stage == Stage.STAGE4:
                category = 'image'
            elif stage == Stage.STAGE5:
                category = 'video'

            # Генерация промпта
            async with typing_action(self.application, user_id):
                enhanced_prompt = await generate_enhanced_prompt(category=category, user_input=user_input)

            if "Не удалось сгенерировать" in enhanced_prompt or len(enhanced_prompt.strip()) < 10:
                await self.send_html_message(user_id, texts.API_ERROR_MESSAGE)
                return SELECTING_CATEGORY

            # Определение следующего этапа
            next_stage_map = {
                Stage.STAGE1: Stage.STAGE2,
                Stage.STAGE2: Stage.STAGE3,
                Stage.STAGE3: Stage.STAGE4,
                Stage.STAGE4: Stage.STAGE5
            }
            next_stage = next_stage_map.get(stage)

            # Отправка результата
            footer = texts.BOOK_GENERATION_FOOTER if category == 'book' else texts.GENERATION_FOOTER
            await self.send_prompt_template(user_id, enhanced_prompt, footer, category, next_stage)

            # Удаление статусного сообщения
            if status_msg:
                try:
                    await context.bot.delete_message(chat_id=user_id, message_id=status_msg.message_id)
                except Exception:
                    pass

            return SELECTING_CATEGORY

        except Exception as e:
            bot_logger.error(f"Input error: {type(e).__name__} - {str(e)[:200]}")
            await self.send_html_message(update.effective_chat.id, texts.API_ERROR_MESSAGE)
            return SELECTING_CATEGORY

    async def handle_non_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка не текстовых сообщений"""
        await self.send_html_message(update.effective_chat.id, "Пожалуйста, введите текст.")
        return TYPING_PROMPT

    def setup_handlers(self):
        """Настройка обработчиков"""
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler('start', self.start), CommandHandler('menu', self.menu)],
            states={
                SELECTING_CATEGORY: [
                    # Обрабатываем категории Этапа 1 и прямые запуски в отдельном обработчике
                    CallbackQueryHandler(self.handle_category_selection, pattern=r'^(category_(question|event|advice)|start_book|create_bot)$'),
                    # Все остальные callback-и обрабатываем здесь
                    CallbackQueryHandler(self.handle_button_callback),
                ],
                TYPING_PROMPT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_user_input),
                    CallbackQueryHandler(self.handle_button_callback),
                ],
            },
            fallbacks=[CommandHandler('start', self.start), CommandHandler('menu', self.menu)],
            allow_reentry=True,
            per_message=False,
            per_chat=True
        )

        self.application.add_handler(conv_handler)
        self.application.add_handler(MessageHandler(~filters.TEXT, self.handle_non_text))

    def run(self):
        """Запуск бота"""
        self.setup_handlers()
        print("✅ Бот запущен...")
        print("🔗 GigaChat API: активен")
        print(f"🤖 @{os.getenv('BOT_USERNAME', 'unknown_bot')}")
        self.application.run_polling()

if __name__ == '__main__':
    try:
        check_environment_variables()
        bot = PromptBot()
        bot.run()
    except EnvironmentError as e:
        print(f"❌ Ошибка окружения: {e}")
    except Exception as e:
        print(f"❌ Критическая ошибка: {type(e).__name__}")
        bot_logger.error(f"CRITICAL: {type(e).__name__} - {str(e)[:300]}")