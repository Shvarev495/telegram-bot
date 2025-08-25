import asyncio
import re
import logging
from datetime import datetime
from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)
from telegram import Bot
from dataclasses import dataclass
from typing import List, Optional, Dict
import json
import os

# ─── НАСТРОЙКА ЛОГИРОВАНИЯ ─────────────────────────────────────────────────────
log_filename = f'multi_sport_monitor_{datetime.now().strftime("%Y%m%d")}.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_filename, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ─── ПАРАМЕТРЫ ────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = "8195557403:AAGGWvI04F0jxswbDsk6gVQohz6KChBAho0"
CHAT_IDS = [
    "1599566837",
    "1422173650"
]

CHECK_EVERY = 30  # секунд между проверками
VERIFICATION_ATTEMPTS = 3
SUCCESS_THRESHOLD = 2
ATTEMPT_DELAY = 2

# 🤖 ПАРАМЕТРЫ АВТОБРОНИРОВАНИЯ
AUTO_BOOKING_WINBOX = True  # Только для WinBox падел
BOOK_FROM_LATE = True  # True = поздние→ранние, False = ранние→поздние
MAX_BOOKING_ATTEMPTS = 5
BOOKING_DELAY = 2

# 🏀 ПАРАМЕТРЫ ДЛЯ ПАДЕЛ
PADEL_SLOTS_TO_BOOK = 2
ZONA_SLOTS_TO_BOOK = 1  # Не используется, только для уведомлений

# 🔐 ДАННЫЕ ДЛЯ АВТОРИЗАЦИИ WINBOX - ДВА АККАУНТА
WINBOX_ACCOUNTS = [
    {
        "email": "shvarev03@gmail.com",
        "password": "7538tuti",
        "name": "Аккаунт 1"
    },
    {
        "email": "shvarevftsha@mail.ru",  # ⚠️ ЗАМЕНИТЕ НА ВТОРОЙ EMAIL!
        "password": "Arl1kino",      # ⚠️ ЗАМЕНИТЕ НА ВТОРОЙ ПАРОЛЬ!
        "name": "Аккаунт 2"
    }
]

# ⏰ ТАЙМАУТЫ
PAGE_LOAD_TIMEOUT = 60000
ELEMENT_WAIT_TIMEOUT = 45000
DEFAULT_WAIT = 3000

@dataclass
class SiteConfig:
    name: str
    url: str
    dates: List[str]
    month_tab: Optional[str] = None
    check_type: str = "slots"
    sport_type: str = "padel"
    slots_to_book: int = 1
    needs_auth: bool = False
    enable_booking: bool = False
    account_index: Optional[int] = None  # Индекс аккаунта для авторизации

# 📋 КОНФИГУРАЦИЯ САЙТОВ - ПАДЕЛ С ДВУМЯ АККАУНТАМИ
SITES = [
    SiteConfig(
        name="Winbox Падел (Аккаунт 1)",
        url="https://winboxmsk.ru/schedule?types=basketball",
        dates=["21", "22", "23", "24"],
        month_tab="АВГУСТ",
        check_type="slots",
        sport_type="padel",
        slots_to_book=PADEL_SLOTS_TO_BOOK,
        needs_auth=True,
        enable_booking=True,
        account_index=0  # Первый аккаунт
    ),
    SiteConfig(
        name="Winbox Падел (Аккаунт 2)",
        url="https://winboxmsk.ru/schedule?types=basketball",
        dates=["21", "22", "23", "24"],
        month_tab="АВГУСТ",
        check_type="slots",
        sport_type="padel",
        slots_to_book=PADEL_SLOTS_TO_BOOK,
        needs_auth=True,
        enable_booking=True,
        account_index=1  # Второй аккаунт
    ),
    SiteConfig(
        name="Zona Padela",
        url="https://n1594888.yclients.com/company/1434507/activity/select?o=act",
        dates=["21", "22", "23", "24"],
        month_tab="Август",
        check_type="clickable",
        sport_type="padel", 
        slots_to_book=ZONA_SLOTS_TO_BOOK,
        needs_auth=False,
        enable_booking=False,
        account_index=None
    )
]

# ✅ СТОП-СЛОВА ДЛЯ ВИНБОКСА (КОГДА НЕ УВЕДОМЛЯТЬ)
STOP_WORDS_WINBOX = [
    "бронирование недоступно",
    "win box отдыхает", 
    "winbox отдыхает",
    "недоступно",
    "отдыхает",
    "вернёмся скоро",
    "закрыто",
    "техническое обслуживание"
]

CRITICAL_STOP_WORDS_ZONA = [
    "нет событий",
    "события отсутствуют", 
    "попробуйте изменить дату",
    "сбросить фильтры",
    "закрыто на техническое обслуживание",
    "недоступно для бронирования"
]

# ─── УЛУЧШЕННЫЕ ФУНКЦИИ АВТОРИЗАЦИИ ───────────────────────────────────────────

async def login_to_winbox(page, account_index: int) -> bool:
    """🔐 УЛУЧШЕННАЯ авторизация на WinBox с поддержкой нескольких аккаунтов"""
    
    if account_index >= len(WINBOX_ACCOUNTS):
        print(f"    ❌ Неверный индекс аккаунта: {account_index}")
        return False
    
    account = WINBOX_ACCOUNTS[account_index]
    print(f"    🔐 Авторизация на WinBox - {account['name']} ({account['email']})")
    
    try:
        # Проверяем, не авторизованы ли уже
        profile_indicators = [
            ':has-text("Личный кабинет")',
            ':has-text("Мой профиль")', 
            ':has-text("Выйти")',
            '[class*="profile"]',
            '[class*="user"]',
            ':has-text("Мои записи")'
        ]
        
        for indicator in profile_indicators:
            try:
                element = await page.query_selector(indicator)
                if element and await element.is_visible():
                    print(f"    ✅ {account['name']} уже авторизован")
                    return True
            except:
                continue
        
        # Если уже авторизованы под другим аккаунтом - выходим
        logout_selectors = [
            'button:has-text("Выйти")',
            'a:has-text("Выйти")',
            'button:has-text("ВЫЙТИ")',
            'a:has-text("ВЫЙТИ")',
            '[class*="logout"]'
        ]
        
        for selector in logout_selectors:
            try:
                logout_btn = await page.query_selector(selector)
                if logout_btn and await logout_btn.is_visible():
                    print("    🚪 Выходим из текущего аккаунта...")
                    await logout_btn.click()
                    await page.wait_for_timeout(3000)
                    break
            except:
                continue
        
        # Ждем загрузки страницы
        await page.wait_for_timeout(3000)
        
        # Ищем форму авторизации или кнопку "ВХОД"
        auth_triggers = [
            'button:has-text("ВХОД")',
            'button:has-text("АВТОРИЗОВАТЬСЯ")',
            'button:has-text("Войти")',
            'a:has-text("ВХОД")',
            'a:has-text("АВТОРИЗОВАТЬСЯ")',
            'a:has-text("Войти")',
            '[class*="auth"]',
            '[class*="login"]'
        ]
        
        # Сначала пытаемся найти уже открытую форму
        email_field = None
        password_field = None
        
        # Расширенные селекторы для поля email
        email_selectors = [
            'input[type="email"]',
            'input[placeholder*="почт"]', 
            'input[placeholder*="email"]',
            'input[placeholder*="Email"]',
            'input[placeholder*="EMAIL"]',
            'input[name*="email"]',
            'input[id*="email"]',
            'input[autocomplete="email"]',
            'input[autocomplete="username"]'
        ]
        
        # Ищем поле email
        for selector in email_selectors:
            try:
                field = await page.query_selector(selector)
                if field and await field.is_visible():
                    email_field = field
                    print("    📧 Найдено поле для email")
                    break
            except:
                continue
        
        # Если поля не найдены, пытаемся открыть форму авторизации
        if not email_field:
            print("    🔍 Поля авторизации не найдены, ищем кнопку входа...")
            
            for trigger_selector in auth_triggers:
                try:
                    triggers = await page.query_selector_all(trigger_selector)
                    for trigger in triggers:
                        if await trigger.is_visible():
                            trigger_text = await trigger.text_content()
                            print(f"    🎯 Найдена кнопка авторизации: '{trigger_text}'")
                            await trigger.click()
                            await page.wait_for_timeout(3000)
                            break
                    if email_field:
                        break
                except Exception as e:
                    continue
            
            # Повторно ищем поля после открытия формы
            for selector in email_selectors:
                try:
                    field = await page.wait_for_selector(selector, timeout=10000)
                    if field and await field.is_visible():
                        email_field = field
                        print("    📧 Найдено поле для email после открытия формы")
                        break
                except:
                    continue
        
        if not email_field:
            print("    ❌ Поле для email не найдено")
            return False
        
        # Расширенные селекторы для поля пароля
        password_selectors = [
            'input[type="password"]',
            'input[placeholder*="пароль"]',
            'input[placeholder*="Пароль"]',
            'input[name*="password"]',
            'input[id*="password"]',
            'input[autocomplete="current-password"]'
        ]
        
        # Ищем поле пароля
        for selector in password_selectors:
            try:
                field = await page.query_selector(selector)
                if field and await field.is_visible():
                    password_field = field
                    print("    🔑 Найдено поле для пароля")
                    break
            except:
                continue
        
        if not password_field:
            print("    ❌ Поле для пароля не найдено")
            return False
        
        # Медленно и аккуратно вводим данные
        print(f"    📧 Вводим email: {account['email']}")
        
        # Фокусируемся и очищаем поле email
        await email_field.click()
        await page.wait_for_timeout(500)
        await email_field.evaluate('el => el.value = ""')
        await email_field.fill("")
        await page.wait_for_timeout(500)
        
        # Медленно печатаем email
        await email_field.type(account['email'], delay=150)
        await page.wait_for_timeout(1000)
        
        print("    🔑 Вводим пароль...")
        
        # Фокусируемся и очищаем поле пароля
        await password_field.click()
        await page.wait_for_timeout(500)
        await password_field.evaluate('el => el.value = ""')
        await password_field.fill("")
        await page.wait_for_timeout(500)
        
        # Медленно печатаем пароль
        await password_field.type(account['password'], delay=150)
        await page.wait_for_timeout(1000)
        
        # Расширенные селекторы для кнопки входа
        submit_selectors = [
            'button:has-text("ВОЙТИ")',
            'button:has-text("Войти")',
            'button:has-text("ВХОД")',
            'button[type="submit"]',
            'input[type="submit"]',
            'form button'
        ]
        
        submit_button = None
        for selector in submit_selectors:
            try:
                buttons = await page.query_selector_all(selector)
                for button in buttons:
                    if await button.is_visible():
                        is_enabled = await button.evaluate('el => !el.disabled && !el.classList.contains("disabled")')
                        if is_enabled:
                            submit_button = button
                            button_text = await button.text_content()
                            print(f"    🎯 Найдена кнопка входа: '{button_text.strip()}'")
                            break
                if submit_button:
                    break
            except:
                continue
        
        # Пытаемся войти
        if submit_button:
            await submit_button.click()
            print("    👆 Нажали кнопку входа")
        else:
            print("    ❌ Кнопка входа не найдена, пробуем Enter")
            try:
                await password_field.press('Enter')
                print("    ⌨️ Нажали Enter в поле пароля")
            except:
                print("    ❌ Не удалось нажать Enter")
                return False
        
        # Ждем результата авторизации
        print("    ⏳ Ожидаем результат авторизации...")
        await page.wait_for_timeout(7000)
        
        # Проверяем успешность авторизации
        success_indicators = [
            ':has-text("Личный кабинет")',
            ':has-text("Профиль")',
            ':has-text("Выйти")',
            ':has-text("Мои записи")',
            '[class*="profile"]',
            '[class*="logout"]'
        ]
        
        for indicator in success_indicators:
            try:
                success_element = await page.wait_for_selector(indicator, timeout=3000)
                if success_element and await success_element.is_visible():
                    success_text = await success_element.text_content()
                    print(f"    ✅ Авторизация {account['name']} успешна! Найден индикатор: '{success_text.strip()}'")
                    logger.info(f"Успешная авторизация на WinBox - {account['name']}")
                    return True
            except:
                continue
        
        # Проверяем ошибки авторизации
        error_indicators = [
            ':has-text("Неверный")',
            ':has-text("Ошибка")',
            ':has-text("неправильн")',
            '[class*="error"]'
        ]
        
        for indicator in error_indicators:
            try:
                error_element = await page.query_selector(indicator)
                if error_element and await error_element.is_visible():
                    error_text = await error_element.text_content()
                    print(f"    ❌ Ошибка авторизации {account['name']}: {error_text.strip()}")
                    logger.error(f"Ошибка авторизации WinBox {account['name']}: {error_text}")
                    return False
            except:
                continue
        
        print(f"    ✅ Авторизация {account['name']} завершена (предположительно успешно)")
        return True
        
    except Exception as e:
        print(f"    ❌ Критическая ошибка авторизации {account['name']}: {e}")
        logger.error(f"Критическая ошибка авторизации WinBox {account['name']}: {e}")
        return False

# ─── УЛУЧШЕННЫЕ ФУНКЦИИ МОНИТОРИНГА ───────────────────────────────────────────

async def send_notification(text: str) -> None:
    """📬 Рассылка уведомлений всем пользователям"""
    bot = Bot(TELEGRAM_TOKEN)
    for chat_id in CHAT_IDS:
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            print(f"📬 Уведомление отправлено → {chat_id}")
            logger.info(f"Уведомление отправлено {chat_id}")
        except Exception as e:
            print(f"❌ Ошибка отправки для {chat_id}: {e}")
            logger.error(f"Ошибка отправки {chat_id}: {e}")

def check_critical_stop_words(text: str, stop_words: List[str]) -> bool:
    """🛑 Проверяет наличие критических стоп-слов"""
    if not text:
        return False
    
    text_lower = text.lower()
    for stop_word in stop_words:
        if stop_word.lower() in text_lower:
            print(f"    🛑 КРИТИЧЕСКОЕ СТОП-СЛОВО: '{stop_word}' в тексте")
            logger.warning(f"Найдено критическое стоп-слово: {stop_word}")
            return True
    return False

def has_time_slots_in_text(text: str) -> bool:
    """🕐 Проверяет наличие временных слотов в тексте (формат ЧЧ:ММ)"""
    if not text:
        return False
    
    # Ищем паттерны времени: 10:00, 15:30, и т.д.
    time_pattern = r'\b([0-2]?[0-9]):([0-5][0-9])\b'
    time_matches = re.findall(time_pattern, text)
    
    # Фильтруем валидные времена (00:00 - 23:59)
    valid_times = []
    for hour, minute in time_matches:
        hour_int = int(hour)
        minute_int = int(minute)
        if 0 <= hour_int <= 23 and 0 <= minute_int <= 59:
            valid_times.append(f"{hour}:{minute}")
    
    if valid_times:
        print(f"    🕐 Найдены временные слоты: {', '.join(valid_times[:5])}")
        return True
    
    return False

def parse_time_slot(slot_text: str) -> tuple:
    """🕐 Парсит время из текста слота"""
    try:
        time_match = re.search(r'(\d{1,2}):(\d{2})', slot_text)
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2))
            return (hour, minute)
        return (0, 0)
    except:
        return (0, 0)

# ─── ФУНКЦИИ БРОНИРОВАНИЯ ДЛЯ WINBOX ──────────────────────────────────────────

async def close_modal_and_return(page):
    """❌ Закрывает модальное окно и возвращается к расписанию"""
    try:
        close_selectors = [
            'button[aria-label="Close"]',
            'button[class*="close"]',
            'button:has-text("✕")',
            'button:has-text("×")',
            'button:has-text("X")',
            'button:has-text("Закрыть")',
            'button:has-text("Отмена")',
            '.modal-close',
            '[data-dismiss="modal"]'
        ]
        
        for selector in close_selectors:
            try:
                close_button = await page.query_selector(selector)
                if close_button and await close_button.is_visible():
                    await close_button.click()
                    print("        ✅ Закрыли модальное окно")
                    await page.wait_for_timeout(1000)
                    return True
            except:
                continue
        
        # Попытка закрыть нажатием Escape
        try:
            await page.keyboard.press('Escape')
            print("        ✅ Закрыли модальное окно через Escape")
            await page.wait_for_timeout(1000)
            return True
        except:
            pass
            
        return False
    except Exception as e:
        print(f"        ⚠️ Ошибка закрытия модального окна: {e}")
        return False

async def reload_and_reselect_date(page, config: SiteConfig, date: str):
    """🔄 Перезагружает страницу и заново выбирает дату"""
    print("        🔄 Перезагружаем страницу и заново выбираем дату...")
    try:
        # Переходим на страницу расписания
        await page.goto(config.url, wait_until="networkidle")
        await page.wait_for_timeout(2000)
        
        # Переключаемся на нужный месяц
        if config.month_tab:
            try:
                await page.wait_for_selector(f"text={config.month_tab}", timeout=ELEMENT_WAIT_TIMEOUT)
                await page.click(f"text={config.month_tab}")
                await page.wait_for_timeout(DEFAULT_WAIT)
                print(f"          ✅ Переключились на {config.month_tab}")
            except PlaywrightTimeoutError:
                print(f"          ⚠️ Не найден месяц {config.month_tab}")
        
        # Выбираем дату заново
        date_selectors = [
            f'button:has-text("{date}")',
            f'div:has-text("{date}")',
            f'*:has-text("{date}")',
            f'[data-date="{date}"]',
            f'[data-day="{date}"]',
            f'td:has-text("{date}")',
            f'span:has-text("{date}")'
        ]
        
        date_element = None
        for selector in date_selectors:
            try:
                elements = await page.query_selector_all(selector)
                for element in elements:
                    text_content = await element.text_content()
                    if text_content and text_content.strip() == date:
                        is_clickable = await element.evaluate('''
                            el => {
                                const style = getComputedStyle(el);
                                const rect = el.getBoundingClientRect();
                                return !el.disabled && 
                                       style.pointerEvents !== "none" && 
                                       rect.width > 0 && rect.height > 0;
                            }
                        ''')
                        if is_clickable:
                            date_element = element
                            break
                if date_element:
                    break
            except:
                continue
        
        if date_element:
            await date_element.click()
            await page.wait_for_timeout(DEFAULT_WAIT)
            print(f"          ✅ Повторно выбрали дату {date}")
            return True
        else:
            print(f"          ❌ Не удалось повторно выбрать дату {date}")
            return False
            
    except Exception as e:
        print(f"          ❌ Ошибка перезагрузки и выбора даты: {e}")
        return False

async def try_book_winbox_slot(page, slot_element, slot_text: str, config: SiteConfig) -> bool:
    """🎯 Попытка забронировать конкретный слот"""
    try:
        print(f"            🎯 Пытаемся забронировать слот: {slot_text}")
        
        # Кликаем на слот
        await slot_element.click()
        await page.wait_for_timeout(BOOKING_DELAY * 1000)
        
        # Ищем кнопку подтверждения бронирования
        book_button_selectors = [
            'button:has-text("Забронировать")',
            'button:has-text("ЗАБРОНИРОВАТЬ")',
            'button:has-text("Подтвердить")',
            'button:has-text("ПОДТВЕРДИТЬ")',
            'button:has-text("Записаться")',
            'button:has-text("ЗАПИСАТЬСЯ")',
            'button[class*="book"]',
            'button[class*="confirm"]',
            'input[type="submit"]',
            'button[type="submit"]'
        ]
        
        book_button = None
        for selector in book_button_selectors:
            try:
                buttons = await page.query_selector_all(selector)
                for button in buttons:
                    if await button.is_visible():
                        is_enabled = await button.evaluate('el => !el.disabled && !el.classList.contains("disabled")')
                        if is_enabled:
                            book_button = button
                            break
                if book_button:
                    break
            except:
                continue
        
        if book_button:
            button_text = await book_button.text_content()
            print(f"            👆 Нажимаем кнопку: '{button_text.strip()}'")
            await book_button.click()
            await page.wait_for_timeout(3000)
            
            # Проверяем успешность бронирования
            success_indicators = [
                ':has-text("успешно")',
                ':has-text("Успешно")',
                ':has-text("забронирован")',
                ':has-text("Забронирован")',
                ':has-text("подтверждено")',
                ':has-text("Подтверждено")',
                '[class*="success"]',
                '[class*="confirmed"]'
            ]
            
            for indicator in success_indicators:
                try:
                    success_element = await page.wait_for_selector(indicator, timeout=5000)
                    if success_element and await success_element.is_visible():
                        success_text = await success_element.text_content()
                        print(f"            ✅ БРОНИРОВАНИЕ УСПЕШНО! {slot_text} - {success_text}")
                        logger.info(f"УСПЕШНОЕ БРОНИРОВАНИЕ падел: {slot_text} - {success_text}")
                        return True
                except:
                    continue
            
            # Проверяем ошибки бронирования
            error_indicators = [
                ':has-text("ошибка")',
                ':has-text("Ошибка")',
                ':has-text("недоступно")',
                ':has-text("занято")',
                '[class*="error"]'
            ]
            
            for indicator in error_indicators:
                try:
                    error_element = await page.query_selector(indicator)
                    if error_element and await error_element.is_visible():
                        error_text = await error_element.text_content()
                        print(f"            ❌ Ошибка бронирования: {error_text.strip()}")
                        return False
                except:
                    continue
            
            print(f"            ✅ Попытка бронирования завершена (предположительно успешно)")
            return True
        else:
            print(f"            ❌ Кнопка бронирования не найдена для слота: {slot_text}")
            return False
        
    except Exception as e:
        print(f"            ❌ Ошибка бронирования слота {slot_text}: {e}")
        return False

async def book_winbox_slots(page, date: str, config: SiteConfig) -> List[str]:
    """🤖 Автобронирование слотов на WinBox"""
    if not config.enable_booking:
        print(f"        📵 Автобронирование отключено для {config.name}")
        return []
    
    print(f"        🤖 Начинаем автобронирование {config.sport_type} для даты {date}...")
    print(f"        🎯 Цель: забронировать {config.slots_to_book} слот(ов)")
    
    booked_slots = []
    
    try:
        # Ищем все временные слоты
        time_selectors = [
            'button:has-text(":")',
            '[class*="time"]:has-text(":")',
            '[class*="slot"]:has-text(":")',
            'button[class*="available"]:has-text(":")',
            'div[class*="time"]:has-text(":")',
            '[data-time]',
            '[class*="schedule-slot"]',
            '[class*="time-slot"]',
            '.slot:has-text(":")',
            'button[class*="slot"]:has-text(":")'
        ]
        
        all_slots = []
        
        for selector in time_selectors:
            try:
                slots = await page.query_selector_all(selector)
                for slot in slots:
                    slot_text = await slot.text_content()
                    if slot_text and ':' in slot_text and len(slot_text.strip()) < 20:
                        # Проверяем что слот кликабельный и не заблокирован
                        is_available = await slot.evaluate('''
                            el => {
                                const style = getComputedStyle(el);
                                const rect = el.getBoundingClientRect();
                                
                                return !el.disabled && 
                                       style.pointerEvents !== "none" && 
                                       style.opacity !== "0.5" &&
                                       rect.width > 0 && rect.height > 0 &&
                                       !el.classList.contains('disabled') &&
                                       !el.classList.contains('booked') &&
                                       !el.classList.contains('unavailable') &&
                                       !el.classList.contains('occupied') &&
                                       !el.classList.contains('reserved');
                            }
                        ''')
                        
                        if is_available:
                            all_slots.append((slot, slot_text.strip()))
            except:
                continue
        
        if not all_slots:
            print(f"        ❌ Не найдено доступных слотов для бронирования на {date}")
            return []
        
        print(f"        📅 Найдено {len(all_slots)} доступных слотов на {date}")
        
        # Сортируем слоты по времени
        slots_with_time = []
        for slot_element, slot_text in all_slots:
            hour, minute = parse_time_slot(slot_text)
            slots_with_time.append((slot_element, slot_text, hour, minute))
        
        # Сортировка: от поздних к ранним или наоборот
        if BOOK_FROM_LATE:
            slots_with_time.sort(key=lambda x: (x[2], x[3]), reverse=True)
            print(f"        ⏰ Порядок бронирования: от поздних времен к ранним")
        else:
            slots_with_time.sort(key=lambda x: (x[2], x[3]))
            print(f"        ⏰ Порядок бронирования: от ранних времен к поздним")
        
        # Пытаемся забронировать слоты по порядку
        booking_attempts = 0
        successful_bookings = 0
        
        for slot_element, slot_text, hour, minute in slots_with_time:
            if booking_attempts >= MAX_BOOKING_ATTEMPTS:
                print(f"        🛑 Достигнут лимит попыток бронирования ({MAX_BOOKING_ATTEMPTS})")
                break
                
            if successful_bookings >= config.slots_to_book:
                print(f"        🎉 Успешно забронировано {successful_bookings} слотов - цель достигнута!")
                break
            
            booking_attempts += 1
            
            try:
                if await try_book_winbox_slot(page, slot_element, slot_text, config):
                    booked_slots.append(f"{slot_text} ({hour:02d}:{minute:02d})")
                    successful_bookings += 1
                    print(f"        ✅ УСПЕШНО забронирован слот {successful_bookings}/{config.slots_to_book}: {slot_text}")
                    logger.info(f"Забронирован {config.sport_type} слот: {slot_text}")
                    
                    # Закрываем модальное окно после успешного бронирования
                    await close_modal_and_return(page)
                    
                    # Небольшая пауза перед следующим бронированием
                    await page.wait_for_timeout(2000)
                    
                    # Перезагружаем страницу и заново выбираем дату для следующего бронирования
                    if successful_bookings < config.slots_to_book:
                        await reload_and_reselect_date(page, config, date)
                        # Нужно пересобрать список слотов после перезагрузки
                        break
                else:
                    print(f"        ❌ Не удалось забронировать слот: {slot_text}")
            except Exception as e:
                print(f"        ❌ Ошибка при попытке бронирования слота {slot_text}: {e}")
                continue
        
        return booked_slots
        
    except Exception as e:
        print(f"        ❌ Критическая ошибка автобронирования: {e}")
        logger.error(f"Критическая ошибка автобронирования {config.sport_type}: {e}")
        return []

# ─── ФУНКЦИИ МОНИТОРИНГА ZONA PADELA ──────────────────────────────────────────

async def determine_slot_availability(text: str, container) -> str:
    """Определяет доступность слота по тексту и элементам"""
    text_lower = text.lower()
    
    # ✅ ЯВНЫЕ ИНДИКАТОРЫ НЕДОСТУПНОСТИ (высший приоритет)
    unavailable_indicators = [
        'нет мест',
        'недоступно',
        'занято',
        'забронировано',
        'закрыто',
        'заблокировано',
        'полностью занято',
        'мест: 0',
        'осталось: 0',
        'свободно: 0',
        'места закончились',
        'запись невозможна'
    ]
    
    for indicator in unavailable_indicators:
        if indicator in text_lower:
            return "unavailable"
    
    # ✅ КЛЮЧЕВАЯ ПРОВЕРКА: "Осталось X место/мест"
    remains_patterns = [
        r'осталось\s+(\d+)\s+мест[оа]?',
        r'осталось\s+(\d+)',
        r'остается\s+(\d+)\s+мест[оа]?',
    ]
    
    for pattern in remains_patterns:
        remains_match = re.search(pattern, text_lower)
        if remains_match:
            places_count = int(remains_match.group(1))
            print(f"          🎯 НАЙДЕН КЛЮЧЕВОЙ ИНДИКАТОР: 'Осталось {places_count} место/мест'")
            return "available" if places_count > 0 else "unavailable"
    
    # ✅ ПРОВЕРЯЕМ ДРУГИЕ ПАТТЕРНЫ КОЛИЧЕСТВА ДОСТУПНЫХ МЕСТ
    places_patterns = [
        r'(\d+)\s*свободн',
        r'(\d+)\s*доступн',
        r'свободно:\s*(\d+)',
        r'мест:\s*(\d+)',
        r'доступно\s*(\d+)',
        r'(\d+)\s*мест.*доступн',
    ]
    
    for pattern in places_patterns:
        places_match = re.search(pattern, text_lower)
        if places_match:
            places_count = int(places_match.group(1))
            print(f"          📊 Найдено мест: {places_count}")
            return "available" if places_count > 0 else "unavailable"
    
    # ✅ ЯВНЫЕ ИНДИКАТОРЫ ДОСТУПНОСТИ
    available_indicators = [
        'записаться',
        'забронировать',
        'выбрать время',
        'доступно для записи',
        'свободные места',
        'можно записаться',
        'есть места',
        'аренда корта'
    ]
    
    for indicator in available_indicators:
        if indicator in text_lower:
            print(f"          ✅ Найден индикатор доступности: '{indicator}'")
            return "available"
    
    # Логика по умолчанию: недоступен
    print(f"          ❓ Неопределенный статус слота, по умолчанию: НЕДОСТУПЕН")
    return "unavailable"

async def analyze_available_slots(page, date: str) -> List[str]:
    """🔍 УЛУЧШЕННЫЙ анализ слотов на Zona Padela"""
    print(f"      🔍 Детальный анализ слотов для даты {date}...")
    logger.info(f"Начинаем детальный анализ слотов для даты {date}")
    
    try:
        # ✅ Проверяем критические стоп-сообщения
        page_text = await page.evaluate('() => document.body.innerText')
        
        date_specific_stops = [
            f"{date} августа — нет событий",
            f"{date} августа нет событий",
            f"на {date} августа нет событий",
            f"{date}.08 - нет событий",
        ]
        
        all_critical_stops = CRITICAL_STOP_WORDS_ZONA + date_specific_stops
        
        if check_critical_stop_words(page_text, all_critical_stops):
            print(f"      ❌ Найдены критические стоп-слова для даты {date}")
            return []
        
        # ✅ Ищем карточки с "Осталось X место"
        remains_elements = await page.query_selector_all('*:has-text("осталось")')
        available_slots = []
        
        for element in remains_elements:
            try:
                element_text = await element.text_content()
                if element_text and 'осталось' in element_text.lower():
                    # Ищем время в тексте
                    time_match = re.search(r'(\d{1,2}:\d{2})', element_text)
                    if time_match:
                        time_text = time_match.group(1)
                        availability = await determine_slot_availability(element_text, element)
                        
                        if availability == "available":
                            available_slots.append(time_text)
                            print(f"        ✅ Найден доступный слот: {time_text}")
            except:
                continue
        
        if available_slots:
            print(f"      🎉 НАЙДЕНЫ ТОЧНО ДОСТУПНЫЕ СЛОТЫ:")
            for slot in available_slots:
                print(f"        ✅ {slot}")
                logger.info(f"Доступный слот: {slot}")
        else:
            print(f"      ❌ НЕТ точно доступных слотов для даты {date}")
        
        return available_slots
        
    except Exception as e:
        print(f"      ❌ Ошибка анализа слотов: {e}")
        logger.error(f"Ошибка анализа слотов для {date}: {e}")
        return []

async def check_zona_padela_date_single(page, date: str, config: SiteConfig) -> List[str]:
    """📅 Проверка даты на Zona Padela"""
    print(f"    📅 Проверяем дату {date}...")
    logger.info(f"Проверяем дату {date} на Zona Padela")

    try:
        # Переходим на страницу
        await page.goto(config.url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
        await page.wait_for_timeout(DEFAULT_WAIT * 2)

        # Если нужен выбор месяца — выполняем (например, Август)
        if config.month_tab:
            month_found = False
            selectors = [
                f'button:has-text("{config.month_tab}")',
                f'div:has-text("{config.month_tab}")',
                f'span:has-text("{config.month_tab}")',
            ]
            for selector in selectors:
                try:
                    elem = await page.query_selector(selector)
                    if elem:
                        await elem.click()
                        await page.wait_for_timeout(DEFAULT_WAIT)
                        print(f"      ✅ Клик по месяцу {config.month_tab}")
                        month_found = True
                        break
                except Exception:
                    continue

        # Собираем варианты селекторов для дня
        calendar_selectors = [
            f'td:has-text("{date}"):not([class*="other-month"]):not([class*="disabled"])',
            f'button:has-text("{date}"):not([disabled])',
            f'div:has-text("{date}"):not([class*="disabled"])',
            f'span:has-text("{date}"):not([class*="disabled"])'
        ]

        # Ищем элемент дня. Он может быть недоступен (серый/disabled).
        target_elem = None
        for selector in calendar_selectors:
            elems = await page.query_selector_all(selector)
            for el in elems:
                try:
                    text = (await el.text_content() or '').strip()
                    is_disabled = await el.get_attribute('disabled')
                    class_attr = await el.get_attribute('class') or ''
                    if text == str(int(date)):  # для 08 → 8
                        if not is_disabled and "disabled" not in class_attr:
                            target_elem = el
                            break
                except Exception:
                    continue
            if target_elem:
                break

        if not target_elem:
            print(f"      ❌ Дата {date}: не найдена или недоступна для выбора")
            return []

        await target_elem.click()
        print(f"      ✅ Клик по дате {date} успешно выполнен")
        await page.wait_for_timeout(DEFAULT_WAIT * 2)

        # Анализируем доступные слоты
        return await analyze_available_slots(page, date)

    except Exception as e:
        print(f"      ❌ Ошибка при проверке даты {date}: {e}")
        logger.error(f"Ошибка проверки даты {date}: {e}")
        return []

# ─── УЛУЧШЕННЫЕ ФУНКЦИИ ПРОВЕРКИ WINBOX ───────────────────────────────────────

async def check_winbox_date_single(page, date: str, config: SiteConfig) -> Dict:
    """📅 Улучшенная проверка даты на WinBox с уведомлениями о временных слотах"""
    print(f"    📅 Проверяем дату {date} для {config.sport_type} ({config.name})...")
    
    try:
        # Ищем и кликаем по дате
        date_selectors = [
            f'button:has-text("{date}")',
            f'div:has-text("{date}")',
            f'*:has-text("{date}")',
            f'[data-date="{date}"]',
            f'[data-day="{date}"]',
            f'td:has-text("{date}")',
            f'span:has-text("{date}")'
        ]
        
        date_element = None
        for selector in date_selectors:
            try:
                elements = await page.query_selector_all(selector)
                for element in elements:
                    text_content = await element.text_content()
                    if text_content and text_content.strip() == date:
                        # Проверяем что дата кликабельна
                        is_clickable = await element.evaluate('''
                            el => {
                                const style = getComputedStyle(el);
                                const rect = el.getBoundingClientRect();
                                return !el.disabled && 
                                       style.pointerEvents !== "none" && 
                                       rect.width > 0 && rect.height > 0;
                            }
                        ''')
                        if is_clickable:
                            date_element = element
                            break
                if date_element:
                    break
            except:
                continue
        
        if not date_element:
            print(f"      ❌ Не найдена кликабельная дата {date}")
            return {"status": "date_not_found", "message": f"Дата {date} недоступна"}
        
        await date_element.click()
        await page.wait_for_timeout(DEFAULT_WAIT)
        
        # Получаем весь текст страницы для проверки
        try:
            page_text = await page.evaluate('() => document.body.innerText')
            print(f"      📄 Получен текст страницы для даты {date}")
            
            # ✅ НОВАЯ ЛОГИКА: Проверяем наличие временных слотов ПЕРЕД проверкой стоп-слов
            has_time_slots = has_time_slots_in_text(page_text)
            has_stop_words = check_critical_stop_words(page_text, STOP_WORDS_WINBOX)
            
            print(f"      🕐 Временные слоты найдены: {'ДА' if has_time_slots else 'НЕТ'}")
            print(f"      🛑 Стоп-слова найдены: {'ДА' if has_stop_words else 'НЕТ'}")
            
            # ✅ ЕСЛИ ЕСТЬ ВРЕМЕННЫЕ СЛОТЫ И НЕТ СТОП-СЛОВ - это хорошая новость!
            if has_time_slots and not has_stop_words:
                print(f"      🎉 Найдены временные слоты БЕЗ стоп-слов! Это потенциально доступные слоты.")
                
                # Извлекаем все временные слоты из текста
                time_pattern = r'\b([0-2]?[0-9]):([0-5][0-9])\b'
                time_matches = re.findall(time_pattern, page_text)
                found_times = []
                
                for hour, minute in time_matches:
                    hour_int = int(hour)
                    minute_int = int(minute)
                    if 0 <= hour_int <= 23 and 0 <= minute_int <= 59:
                        found_times.append(f"{hour}:{minute}")
                
                # Убираем дубликаты
                unique_times = list(set(found_times))
                unique_times.sort()
                
                print(f"      ⏰ Найденные временные слоты: {', '.join(unique_times[:10])}")
                
                return {
                    "status": "time_slots_detected",
                    "slots": unique_times,
                    "message": f"Обнаружены временные слоты ({len(unique_times)} шт.)",
                    "has_stop_words": False
                }
            
            # ✅ ЕСЛИ ЕСТЬ СТОП-СЛОВА - проверяем есть ли всё-таки временные слоты
            if has_stop_words:
                if has_time_slots:
                    print(f"      ⚠️ Есть и стоп-слова, и временные слоты - противоречивая ситуация")
                    
                    # Извлекаем временные слоты даже при наличии стоп-слов
                    time_pattern = r'\b([0-2]?[0-9]):([0-5][0-9])\b'
                    time_matches = re.findall(time_pattern, page_text)
                    found_times = []
                    
                    for hour, minute in time_matches:
                        hour_int = int(hour)
                        minute_int = int(minute)
                        if 0 <= hour_int <= 23 and 0 <= minute_int <= 59:
                            found_times.append(f"{hour}:{minute}")
                    
                    unique_times = list(set(found_times))
                    unique_times.sort()
                    
                    return {
                        "status": "time_slots_with_stop_words",
                        "slots": unique_times,
                        "message": f"Временные слоты найдены, но есть стоп-слова",
                        "has_stop_words": True
                    }
                else:
                    print(f"      🛑 Найдены стоп-слова БЕЗ временных слотов")
                    return {
                        "status": "no_slots",
                        "message": "Найдены стоп-слова, временных слотов нет",
                        "has_stop_words": True
                    }
            
        except Exception as e:
            print(f"      ⚠️ Не удалось получить текст страницы: {e}")
        
        # ✅ Старая логика: ищем кликабельные временные слоты
        time_selectors = [
            'button:has-text(":")',
            '[class*="time"]',
            '[class*="slot"]',
            'button[class*="available"]',
            'div[class*="time"]:has-text(":")',
            '[data-time]'
        ]
        
        available_slots = []
        for selector in time_selectors:
            try:
                slots = await page.query_selector_all(selector)
                for slot in slots:
                    slot_text = await slot.text_content()
                    if slot_text and ':' in slot_text and len(slot_text.strip()) < 20:
                        # Проверяем доступность слота
                        is_available = await slot.evaluate('''
                            el => {
                                const style = getComputedStyle(el);
                                return !el.disabled && 
                                       style.pointerEvents !== "none" && 
                                       style.opacity !== "0.5" &&
                                       !el.classList.contains('disabled') &&
                                       !el.classList.contains('booked');
                            }
                        ''')
                        if is_available:
                            available_slots.append(slot_text.strip())
            except:
                continue
        
        if available_slots:
            print(f"      ✅ Дата {date}: найдено {len(available_slots)} КЛИКАБЕЛЬНЫХ {config.sport_type} слотов")
            
            # Пытаемся забронировать слоты если включено автобронирование
            booked_slots = []
            if config.enable_booking:
                booked_slots = await book_winbox_slots(page, date, config)
                if booked_slots:
                    print(f"      🎉 Забронированы {config.sport_type} слоты: {', '.join(booked_slots)}")
                    logger.info(f"Забронированы {config.sport_type} слоты {date}: {', '.join(booked_slots)}")
            
            return {
                "status": "available",
                "slots": available_slots,
                "booked_slots": booked_slots,
                "message": f"Доступно {len(available_slots)} кликабельных слотов"
            }
        
        print(f"      ⚠️ Дата {date}: кликабельные {config.sport_type} слоты не найдены")
        return {"status": "no_clickable_slots", "message": "Нет кликабельных слотов"}
        
    except Exception as e:
        print(f"      ❌ Ошибка проверки даты {date}: {e}")
        logger.error(f"Ошибка проверки WinBox {date}: {e}")
        return {"status": "error", "message": f"Техническая ошибка: {e}"}

# ─── ФУНКЦИИ ТРОЙНОЙ ПРОВЕРКИ ─────────────────────────────────────────────────

async def verify_date_multiple_times(page, date: str, config: SiteConfig) -> List[str]:
    """🔄 ТРОЙНАЯ ПРОВЕРКА даты с объединением результатов"""
    print(f"  🔄 ТРОЙНАЯ ПРОВЕРКА даты {date} для {config.sport_type} ({VERIFICATION_ATTEMPTS} попыток)...")
    logger.info(f"Начинаем тройную проверку даты {date} {config.sport_type}")
    
    all_found_slots = set()  # Используем set для уникальных слотов
    successful_checks = 0
    time_slots_detected = False  # Флаг для обнаружения временных слотов
    
    for attempt in range(1, VERIFICATION_ATTEMPTS + 1):
        print(f"    🔍 Попытка {attempt}/{VERIFICATION_ATTEMPTS} для даты {date}...")
        
        try:
            if config.check_type == "slots":
                # Для WinBox - проверяем наличие слотов
                result = await check_winbox_date_single(page, date, config)
                status = result.get("status")
                
                # ✅ НОВАЯ ЛОГИКА: учитываем временные слоты
                if status == "available":
                    successful_checks += 1
                    slots = result.get("slots", [])
                    all_found_slots.update(slots)
                    print(f"      ✅ Попытка {attempt}: НАЙДЕНО {len(slots)} КЛИКАБЕЛЬНЫХ слотов")
                elif status == "time_slots_detected":
                    successful_checks += 1
                    time_slots_detected = True
                    slots = result.get("slots", [])
                    all_found_slots.update(slots)
                    print(f"      🕐 Попытка {attempt}: НАЙДЕНЫ ВРЕМЕННЫЕ СЛОТЫ ({len(slots)} шт.)")
                elif status == "time_slots_with_stop_words":
                    # Частичный успех - есть временные слоты, но есть и стоп-слова
                    time_slots_detected = True
                    slots = result.get("slots", [])
                    all_found_slots.update(slots)
                    print(f"      ⚠️ Попытка {attempt}: ВРЕМЕННЫЕ СЛОТЫ + СТОП-СЛОВА ({len(slots)} шт.)")
                else:
                    print(f"      ❌ Попытка {attempt}: НЕ НАЙДЕНО")
                    
            elif config.check_type == "clickable":
                # Для Zona Padela - получаем список слотов
                slots = await check_zona_padela_date_single(page, date, config)
                if slots:
                    successful_checks += 1
                    all_found_slots.update(slots)
                    print(f"      ✅ Попытка {attempt}: НАЙДЕНО {len(slots)} слотов")
                else:
                    print(f"      ❌ Попытка {attempt}: НЕ НАЙДЕНО")
            else:
                print(f"      ❌ Попытка {attempt}: НЕИЗВЕСТНЫЙ ТИП ПРОВЕРКИ")
                
            # Пауза между попытками (кроме последней)
            if attempt < VERIFICATION_ATTEMPTS:
                await asyncio.sleep(ATTEMPT_DELAY)
                
        except Exception as e:
            print(f"      ❌ Попытка {attempt}: ОШИБКА - {e}")
            logger.error(f"Ошибка проверки {date}, попытка {attempt}: {e}")
    
    # Анализируем результаты
    success_rate = successful_checks / VERIFICATION_ATTEMPTS
    print(f"  📊 РЕЗУЛЬТАТ для даты {date}: {successful_checks}/{VERIFICATION_ATTEMPTS} успешных проверок ({success_rate*100:.1f}%)")
    print(f"  📋 Найдено уникальных слотов: {len(all_found_slots)}")
    
    # ✅ НОВАЯ ЛОГИКА: возвращаем результат даже при обнаружении только временных слотов
    if successful_checks >= SUCCESS_THRESHOLD or time_slots_detected:
        print(f"  🎉 Дата {date}: ПОДТВЕРЖДЕНА ({successful_checks} >= {SUCCESS_THRESHOLD} или найдены временные слоты)")
        logger.info(f"Дата {date} {config.sport_type} ПОДТВЕРЖДЕНА: {successful_checks}/{VERIFICATION_ATTEMPTS}")
        return list(all_found_slots)
    else:
        print(f"  ❌ Дата {date}: НЕ ПОДТВЕРЖДЕНА ({successful_checks} < {SUCCESS_THRESHOLD})")
        return []

# ─── ГЛАВНАЯ ФУНКЦИЯ МОНИТОРИНГА ──────────────────────────────────────────────

async def check_site_comprehensive(config: SiteConfig) -> Dict[str, any]:
    """🏢 Комплексная проверка сайта на все даты с тройной верификацией"""
    print(f"🔍 Комплексная проверка {config.name}...")
    logger.info(f"Начинаем комплексную проверку {config.name}")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-accelerated-2d-canvas',
                '--no-first-run',
                '--no-zygote',
                '--disable-gpu'
            ]
        )
        
        context = await browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        )
        
        page = await context.new_page()
        page.set_default_timeout(ELEMENT_WAIT_TIMEOUT)
        
        try:
            print(f"🔥 Загружаем {config.name}: {config.url}")
            logger.info(f"Загружаем {config.name}: {config.url}")
            
            await page.goto(config.url, wait_until="networkidle", timeout=PAGE_LOAD_TIMEOUT)
            await page.wait_for_timeout(DEFAULT_WAIT * 2)
            
            # ✅ АВТОРИЗАЦИЯ НА WINBOX (если требуется)
            if config.needs_auth and 'winbox' in config.url.lower() and config.account_index is not None:
                if not await login_to_winbox(page, config.account_index):
                    print(f"❌ {config.name}: не удалось авторизоваться")
                    logger.error(f"{config.name}: ошибка авторизации")
                    return {"status": "auth_failed", "dates": {}, "message": "Ошибка авторизации"}
                
                # Переходим на нужный спорт после авторизации
                await page.goto(config.url, wait_until="networkidle")
                print(f"    🔄 Перешли на раздел {config.sport_type}")
                await page.wait_for_timeout(2000)
            
            # Переключаемся на нужный месяц
            if config.month_tab:
                try:
                    await page.wait_for_selector(f"text={config.month_tab}", timeout=ELEMENT_WAIT_TIMEOUT)
                    await page.click(f"text={config.month_tab}")
                    await page.wait_for_timeout(DEFAULT_WAIT)
                    print(f"  ✅ Переключились на {config.month_tab}")
                except PlaywrightTimeoutError:
                    print(f"  ⚠️ Не найден месяц {config.month_tab}")
            
            # Результаты проверки по датам
            date_results = {}
            
            for date in config.dates:
                try:
                    # ✅ ТРОЙНАЯ ПРОВЕРКА каждой даты
                    slots = await verify_date_multiple_times(page, date, config)
                    if slots:
                        date_results[date] = {
                            "available": True,
                            "slots": slots,
                            "count": len(slots)
                        }
                    else:
                        date_results[date] = {
                            "available": False,
                            "slots": [],
                            "count": 0
                        }
                except Exception as e:
                    print(f"  ❌ Критическая ошибка проверки даты {date}: {e}")
                    logger.error(f"Критическая ошибка проверки {config.name} {date}: {e}")
                    date_results[date] = {
                        "available": False,
                        "slots": [],
                        "count": 0,
                        "error": str(e)
                    }
            
            # Подводим итоги
            available_dates = [date for date, result in date_results.items() if result["available"]]
            total_slots = sum(result["count"] for result in date_results.values())
            
            if available_dates:
                print(f"🎉 {config.name}: НАЙДЕНЫ СЛОТЫ!")
                print(f"📅 Доступные даты: {', '.join(available_dates)}")
                print(f"⏰ Всего слотов: {total_slots}")
                
                return {
                    "status": "available",
                    "dates": date_results,
                    "available_dates": available_dates,
                    "total_slots": total_slots,
                    "message": f"Найдены слоты на {len(available_dates)} дат"
                }
            else:
                print(f"⚠️ {config.name}: слоты не найдены")
                return {
                    "status": "no_slots",
                    "dates": date_results,
                    "available_dates": [],
                    "total_slots": 0,
                    "message": "Слоты не найдены"
                }
            
        except Exception as e:
            print(f"❌ Ошибка при проверке {config.name}: {e}")
            logger.error(f"Ошибка проверки {config.name}: {e}")
            return {
                "status": "error",
                "dates": {},
                "message": f"Техническая ошибка: {e}"
            }
            
        finally:
            await context.close()
            await browser.close()

async def monitor_sites():
    """🔄 Главная функция мониторинга всех сайтов"""
    print("🚀 Запуск объединенного мониторинга с автобронированием v4.0!")
    print("🆕 ВОЗМОЖНОСТИ ВЕРСИИ 4.0:")
    print("   👥 Поддержка двух аккаунтов WinBox")
    print("   🕐 Уведомления о временных слотах даже если они не кликабельны")
    print("   🤖 Автобронирование падел слотов на WinBox")
    print("   📱 Уведомления о появлении слотов на обоих сайтах")
    print("   🔄 Тройная проверка для надежности")
    print("   📊 Детальная статистика и логирование")
    print()
    
    logger.info("=== ЗАПУСК ОБЪЕДИНЕННОГО МОНИТОРИНГА v4.0 ===")
    logger.info(f"Файл логов: {log_filename}")
    
    # ⚠️ ВАЖНОЕ ПРЕДУПРЕЖДЕНИЕ ОБ АККАУНТАХ
    print("✅ НАСТРОЕННЫЕ АККАУНТЫ WINBOX:")
    for i, account in enumerate(WINBOX_ACCOUNTS):
        print(f"   {i+1}. {account['name']}: {account['email']} (пароль: {'*' * len(account['password'])})")
    
    if WINBOX_ACCOUNTS[1]['email'] == "второй_email@gmail.com":
        print("🚨 ВНИМАНИЕ! Не забудьте заменить данные второго аккаунта!")
        print("   📧 WINBOX_ACCOUNTS[1]['email']: замените на реальный email")
        print("   🔑 WINBOX_ACCOUNTS[1]['password']: замените на реальный пароль")
    
    # Отправляем уведомление о запуске
    startup_message = (
        "🚀 МОНИТОРИНГ ЗАПУЩЕН! v4.0\n\n"
        f"📋 Отслеживаемые сайты:\n"
    )
    
    for site in SITES:
        booking_status = "🤖 АВТОБРОНИРОВАНИЕ" if site.enable_booking else "📢 ТОЛЬКО УВЕДОМЛЕНИЯ"
        account_info = ""
        if site.account_index is not None:
            account_info = f" ({WINBOX_ACCOUNTS[site.account_index]['name']})"
        startup_message += f"• {site.name}{account_info} - {booking_status}\n"
    
    startup_message += f"\n⏰ Проверка каждые {CHECK_EVERY} секунд"
    startup_message += f"\n🔍 Тройная проверка ({VERIFICATION_ATTEMPTS} попыток)"
    startup_message += f"\n🆕 Уведомления о временных слотах даже без кликабельности"
    await send_notification(startup_message)
    
    # Словарь для отслеживания предыдущих состояний
    previous_states = {}
    
    try:
        while True:
            print(f"\n{'='*80}")
            print(f"🕐 ЦИКЛ ПРОВЕРКИ В {datetime.now().strftime('%H:%M:%S')}")
            print(f"{'='*80}")
            
            for site_config in SITES:
                print(f"\n🏢 {site_config.name}")
                print(f"🔗 {site_config.url}")
                print(f"🎾 Спорт: {site_config.sport_type}")
                booking_info = "🤖 АВТОБРОНИРОВАНИЕ" if site_config.enable_booking else "📢 ТОЛЬКО УВЕДОМЛЕНИЯ"
                print(f"📋 Режим: {booking_info}")
                
                if site_config.account_index is not None:
                    account_info = WINBOX_ACCOUNTS[site_config.account_index]
                    print(f"👤 Аккаунт: {account_info['name']} ({account_info['email']})")
                
                # Проверяем сайт комплексно
                site_result = await check_site_comprehensive(site_config)
                
                # Сравниваем с предыдущим состоянием по каждой дате
                for date in site_config.dates:
                    site_date_key = f"{site_config.name}_{date}"
                    
                    current_date_result = site_result.get("dates", {}).get(date, {})
                    previous_date_result = previous_states.get(site_date_key, {})
                    
                    current_available = current_date_result.get("available", False)
                    previous_available = previous_date_result.get("available", False)
                    
                    current_slots = current_date_result.get("slots", [])
                    previous_slots = previous_date_result.get("slots", [])
                    
                    # Логика уведомлений
                    should_notify = False
                    notification_text = ""
                    
                    # Случай 1: Появились новые слоты (раньше не было, теперь есть)
                    if current_available and not previous_available:
                        should_notify = True
                        slots_preview = ", ".join(current_slots[:5])
                        if len(current_slots) > 5:
                            slots_preview += "..."
                        
                        # Определяем тип слотов для WinBox
                        slot_type_info = ""
                        if 'winbox' in site_config.url.lower():
                            # Проверяем, есть ли среди слотов реальные времена (проверяем кликабельность)
                            clickable_times = [slot for slot in current_slots if len(slot) <= 8 and ':' in slot]
                            if clickable_times:
                                slot_type_info = f"\n🎯 Тип: КЛИКАБЕЛЬНЫЕ слоты ({len(clickable_times)} шт.)"
                            else:
                                slot_type_info = f"\n🕐 Тип: ВРЕМЕННЫЕ слоты (возможно не кликабельны)"
                        
                        notification_text = (
                            f"🎯 НОВЫЕ СЛОТЫ ПОЯВИЛИСЬ!\n\n"
                            f"🏢 {site_config.name}\n"
                            f"📅 Дата: {date}\n"
                            f"🎾 Вид спорта: {site_config.sport_type}\n"
                            f"⏰ Доступные слоты ({len(current_slots)}): {slots_preview}{slot_type_info}\n\n"
                            f"🔗 {site_config.url}"
                        )
                        
                        # Информация об автобронировании для WinBox
                        if site_config.enable_booking and 'winbox' in site_config.url.lower():
                            notification_text += f"\n\n🤖 АВТОБРОНИРОВАНИЕ: включено (цель: {site_config.slots_to_book} слота)"
                            notification_text += f"\n⏰ Приоритет: {'поздние→ранние' if BOOK_FROM_LATE else 'ранние→поздние'}"
                            notification_text += f"\n👤 Аккаунт: {WINBOX_ACCOUNTS[site_config.account_index]['name']}"
                        
                        print(f"  🎉 НОВЫЕ СЛОТЫ на дату {date}! Отправляем уведомление...")
                    
                    # Случай 2: Добавились новые слоты к уже существующим
                    elif current_available and previous_available:
                        new_slots = set(current_slots) - set(previous_slots)
                        if new_slots:
                            should_notify = True
                            new_slots_text = ", ".join(list(new_slots)[:5])
                            if len(new_slots) > 5:
                                new_slots_text += "..."
                            
                            notification_text = (
                                f"➕ ДОБАВИЛИСЬ НОВЫЕ СЛОТЫ!\n\n"
                                f"🏢 {site_config.name}\n"
                                f"📅 Дата: {date}\n"
                                f"🎾 Вид спорта: {site_config.sport_type}\n"
                                f"🆕 Новые слоты ({len(new_slots)}): {new_slots_text}\n"
                                f"📊 Всего доступно: {len(current_slots)}\n\n"
                                f"🔗 {site_config.url}"
                            )
                            
                            if site_config.account_index is not None:
                                notification_text += f"\n👤 Аккаунт: {WINBOX_ACCOUNTS[site_config.account_index]['name']}"
                            
                            print(f"  ➕ ДОБАВИЛИСЬ слоты на дату {date}! Отправляем уведомление...")
                    
                    # Случай 3: Слоты исчезли
                    elif not current_available and previous_available:
                        should_notify = True
                        notification_text = (
                            f"❌ СЛОТЫ БОЛЬШЕ НЕ ДОСТУПНЫ\n\n"
                            f"🏢 {site_config.name}\n"
                            f"📅 Дата: {date}\n"
                            f"🎾 Вид спорта: {site_config.sport_type}\n"
                            f"💭 Возможно, слоты были забронированы"
                        )
                        
                        if site_config.account_index is not None:
                            notification_text += f"\n👤 Аккаунт: {WINBOX_ACCOUNTS[site_config.account_index]['name']}"
                        
                        print(f"  ❌ Слоты исчезли на дату {date}")
                    
                    # Отправляем уведомление
                    if should_notify and notification_text:
                        await send_notification(notification_text)
                    
                    # Сохраняем текущее состояние
                    previous_states[site_date_key] = current_date_result
                    
                    # Логирование состояния
                    if current_available:
                        logger.info(f"{site_config.name} {date}: {len(current_slots)} слотов доступно")
                    else:
                        logger.info(f"{site_config.name} {date}: слоты недоступны")
                
                # Пауза между сайтами
                await asyncio.sleep(3)
            
            print(f"\n⏳ Следующая проверка через {CHECK_EVERY} секунд...")
            print(f"📊 Отслеживается состояний: {len(previous_states)}")
            await asyncio.sleep(CHECK_EVERY)
            
    except KeyboardInterrupt:
        print("\n👋 Остановка мониторинга по запросу пользователя")
        await send_notification("⏹️ Мониторинг остановлен администратором")
    except Exception as e:
        print(f"\n❌ Критическая ошибка мониторинга: {e}")
        logger.error(f"Критическая ошибка: {e}")
        await send_notification(f"💥 КРИТИЧЕСКАЯ ОШИБКА МОНИТОРИНГА:\n{e}")

# ─── ТОЧКА ВХОДА ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("🎾 ОБЪЕДИНЕННЫЙ МУЛЬТИСПОРТИВНЫЙ МОНИТОРИНГ И АВТОБРОНИРОВАНИЕ v4.0")
    print("=" * 80)
    print("🔥 НОВЫЕ ФУНКЦИИ v4.0:")
    print("   👥 Поддержка двух аккаунтов WinBox")
    print("   🕐 Уведомления о временных слотах даже без кликабельности")
    print("   🔍 Мониторинг слотов на WinBox и Zona Padela")
    print("   🤖 Автобронирование падел слотов на WinBox")
    print("   📱 Telegram уведомления о новых слотах")
    print("   🔄 Тройная проверка для надежности")
    print("   📊 Подробное логирование")
    print("=" * 80)
    
    # Проверяем настройки аккаунтов
    print("\n📋 ПРОВЕРКА НАСТРОЕК АККАУНТОВ:")
    for i, account in enumerate(WINBOX_ACCOUNTS):
        status = "✅ НАСТРОЕН" if account['email'] != "второй_email@gmail.com" else "❌ ТРЕБУЕТ НАСТРОЙКИ"
        print(f"   {i+1}. {account['name']}: {account['email']} - {status}")
    
    if WINBOX_ACCOUNTS[1]['email'] == "второй_email@gmail.com":
        print("\n🚨 ВНИМАНИЕ: Второй аккаунт не настроен!")
        print("   Отредактируйте WINBOX_ACCOUNTS[1] в коде перед запуском")
    
    print(f"\n📊 СТАТИСТИКА:")
    print(f"   🏢 Сайтов для мониторинга: {len(SITES)}")
    print(f"   📅 Дат для проверки: {len(SITES[0].dates) if SITES else 0}")
    print(f"   ⏰ Интервал проверки: {CHECK_EVERY} секунд")
    print(f"   🔄 Попыток верификации: {VERIFICATION_ATTEMPTS}")
    
    try:
        asyncio.run(monitor_sites())
    except KeyboardInterrupt:
        print("\n👋 Программа завершена пользователем")
    except Exception as e:
        print(f"\n💥 Критическая ошибка запуска: {e}")
        logger.error(f"Критическая ошибка запуска: {e}")