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

CHECK_EVERY = 1  # секунд между проверками
VERIFICATION_ATTEMPTS = 3
SUCCESS_THRESHOLD = 2
ATTEMPT_DELAY = 2

# 🤖 ПАРАМЕТРЫ АВТОБРОНИРОВАНИЯ
AUTO_BOOKING_WINBOX = True  # Для WinBox падел
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
        "email": "shvarevftsha@mail.ru",
        "password": "Arl1kino",
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
        url="https://winboxmsk.ru/schedule?types=padel",
        dates=["27", "28", "29", "30"],
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
        url="https://winboxmsk.ru/schedule?types=padel",
        dates=["27", "28", "29", "30"],
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
        url="https://n1594888.yclients.com/company/1434507/activity/select?orderStatus=successed&o=act2025-08-25",
        dates=["28", "29", "30", "31"],
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
    "недоступно для бронирования",
    "нет мест"  # Добавили ключевое стоп-слово
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
    """📬 Рассылка уведомлений всем пользователям ТОЛЬКО о слотах и бронированиях"""
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

# ─── УЛУЧШЕННЫЕ ФУНКЦИИ БРОНИРОВАНИЯ ДЛЯ WINBOX ──────────────────────────────────────────

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

async def find_clickable_time_slots(page, date: str) -> List[dict]:
    """🔍 УЛУЧШЕННЫЙ поиск кликабельных временных слотов"""
    print(f"        🔍 Ищем кликабельные временные слоты для даты {date}...")
    
    try:
        # Ждем загрузки контента
        await page.wait_for_timeout(2000)
        
        # Расширенные селекторы для поиска временных слотов
        time_slot_selectors = [
            # Основные селекторы для временных слотов
            'div:has-text(":")',
            'button:has-text(":")',
            'span:has-text(":")',
            '[class*="time"]:has-text(":")',
            '[class*="slot"]:has-text(":")',
            '[class*="schedule"]:has-text(":")',
            '[class*="court"]:has-text(":")',
            
            # Селекторы для структуры расписания
            '[class*="time-slot"]',
            '[class*="schedule-item"]',
            '[class*="booking-slot"]',
            '[class*="court-time"]',
            
            # Универсальные селекторы
            'div[role="button"]:has-text(":")',
            'a:has-text(":")',
            '*[onclick*="time"]:has-text(":")',
            '*[data-time]',
            '*[data-slot]',
        ]
        
        all_slots = []
        
        for selector in time_slot_selectors:
            try:
                elements = await page.query_selector_all(selector)
                
                for element in elements:
                    try:
                        # Получаем текст элемента
                        element_text = await element.text_content()
                        if not element_text:
                            continue
                            
                        element_text = element_text.strip()
                        
                        # Проверяем что это временной слот (содержит время)
                        time_pattern = r'\b([0-2]?[0-9]):([0-5][0-9])\b'
                        time_matches = re.findall(time_pattern, element_text)
                        
                        if not time_matches:
                            continue
                            
                        # Проверяем доступность элемента
                        is_available = await element.evaluate('''
                            el => {
                                const rect = el.getBoundingClientRect();
                                const style = getComputedStyle(el);
                                
                                // Проверяем видимость
                                if (rect.width <= 0 || rect.height <= 0) return false;
                                if (style.display === 'none' || style.visibility === 'hidden') return false;
                                if (parseFloat(style.opacity) < 0.1) return false;
                                
                                // Проверяем что элемент не заблокирован
                                if (el.disabled) return false;
                                if (style.pointerEvents === 'none') return false;
                                
                                // Проверяем классы, указывающие на недоступность
                                const classList = el.classList;
                                const unavailableClasses = [
                                    'disabled', 'booked', 'unavailable', 'occupied', 
                                    'reserved', 'blocked', 'inactive', 'past'
                                ];
                                
                                for (const cls of unavailableClasses) {
                                    if (classList.contains(cls)) return false;
                                }
                                
                                return true;
                            }
                        ''')
                        
                        if not is_available:
                            continue
                            
                        # Проверяем что элемент кликабелен
                        is_clickable = await element.evaluate('''
                            el => {
                                // Проверяем есть ли обработчики событий
                                const hasClickHandler = el.onclick !== null || 
                                                      el.addEventListener !== undefined ||
                                                      el.getAttribute('onclick') !== null ||
                                                      el.getAttribute('href') !== null;
                                
                                // Проверяем что это кнопка, ссылка или кликабельный элемент
                                const tagName = el.tagName.toLowerCase();
                                const isClickableTag = ['button', 'a', 'input'].includes(tagName);
                                
                                const role = el.getAttribute('role');
                                const isClickableRole = role === 'button' || role === 'link';
                                
                                const cursor = getComputedStyle(el).cursor;
                                const hasPointerCursor = cursor === 'pointer';
                                
                                return hasClickHandler || isClickableTag || isClickableRole || hasPointerCursor;
                            }
                        ''')
                        
                        # Добавляем элемент в список, даже если он формально не кликабелен
                        # (иногда на сайтах обработчики добавляются динамически)
                        slot_info = {
                            'element': element,
                            'text': element_text,
                            'is_clickable': is_clickable,
                            'selector_used': selector
                        }
                        
                        all_slots.append(slot_info)
                        
                        print(f"          ✅ Найден слот: '{element_text}' (кликабелен: {is_clickable}, селектор: {selector})")
                        
                    except Exception as e:
                        continue
                        
            except Exception as e:
                continue
        
        # Убираем дубликаты по тексту
        unique_slots = {}
        for slot in all_slots:
            text = slot['text']
            if text not in unique_slots or slot['is_clickable']:
                unique_slots[text] = slot
        
        final_slots = list(unique_slots.values())
        
        print(f"        📊 Итого найдено уникальных слотов: {len(final_slots)}")
        
        # Сортируем слоты по времени
        def extract_first_time(slot_info):
            text = slot_info['text']
            time_match = re.search(r'(\d{1,2}):(\d{2})', text)
            if time_match:
                return (int(time_match.group(1)), int(time_match.group(2)))
            return (0, 0)
        
        final_slots.sort(key=extract_first_time)
        
        return final_slots
        
    except Exception as e:
        print(f"        ❌ Ошибка поиска слотов: {e}")
        return []

async def try_click_slot_multiple_ways(page, slot_info: dict) -> bool:
    """🎯 МНОЖЕСТВЕННЫЕ способы клика по слоту"""
    element = slot_info['element']
    text = slot_info['text']
    
    print(f"            🎯 Пытаемся кликнуть по слоту: '{text}'")
    
    # Способ 1: Обычный клик
    try:
        await element.click()
        await page.wait_for_timeout(1000)
        print(f"            ✅ Способ 1 (обычный клик) - выполнен")
        return True
    except Exception as e:
        print(f"            ❌ Способ 1 не сработал: {e}")
    
    # Способ 2: Клик с force
    try:
        await element.click(force=True)
        await page.wait_for_timeout(1000)
        print(f"            ✅ Способ 2 (force клик) - выполнен")
        return True
    except Exception as e:
        print(f"            ❌ Способ 2 не сработал: {e}")
    
    # Способ 3: Фокус + Enter
    try:
        await element.focus()
        await page.keyboard.press('Enter')
        await page.wait_for_timeout(1000)
        print(f"            ✅ Способ 3 (фокус + Enter) - выполнен")
        return True
    except Exception as e:
        print(f"            ❌ Способ 3 не сработал: {e}")
    
    # Способ 4: JavaScript клик
    try:
        await element.evaluate('el => el.click()')
        await page.wait_for_timeout(1000)
        print(f"            ✅ Способ 4 (JavaScript клик) - выполнен")
        return True
    except Exception as e:
        print(f"            ❌ Способ 4 не сработал: {e}")
    
    # Способ 5: Эмуляция событий
    try:
        await element.evaluate('''
            el => {
                const event = new MouseEvent('click', {
                    bubbles: true,
                    cancelable: true,
                    view: window
                });
                el.dispatchEvent(event);
            }
        ''')
        await page.wait_for_timeout(1000)
        print(f"            ✅ Способ 5 (эмуляция событий) - выполнен")
        return True
    except Exception as e:
        print(f"            ❌ Способ 5 не сработал: {e}")
    
    # Способ 6: Поиск родительского кликабельного элемента
    try:
        parent_clickable = await element.evaluate('''
            el => {
                let current = el;
                while (current && current !== document.body) {
                    if (current.onclick || current.getAttribute('onclick') || 
                        current.tagName.toLowerCase() === 'button' ||
                        current.tagName.toLowerCase() === 'a') {
                        return current;
                    }
                    current = current.parentElement;
                }
                return null;
            }
        ''')
        
        if parent_clickable:
            await page.evaluate('el => el.click()', parent_clickable)
            await page.wait_for_timeout(1000)
            print(f"            ✅ Способ 6 (клик по родителю) - выполнен")
            return True
    except Exception as e:
        print(f"            ❌ Способ 6 не сработал: {e}")
    
    print(f"            ❌ ВСЕ СПОСОБЫ КЛИКА НЕ СРАБОТАЛИ для слота: '{text}'")
    return False

async def wait_for_booking_modal(page) -> bool:
    """⏳ Ожидание появления модального окна бронирования"""
    print(f"            ⏳ Ожидаем модальное окно бронирования...")
    
    modal_selectors = [
        '[class*="modal"]',
        '[class*="popup"]',
        '[class*="dialog"]',
        '[class*="booking"]',
        '[role="dialog"]',
        '.overlay',
        '[class*="overlay"]'
    ]
    
    try:
        for selector in modal_selectors:
            try:
                modal = await page.wait_for_selector(selector, timeout=3000)
                if modal:
                    is_visible = await modal.evaluate('el => el.offsetHeight > 0 && el.offsetWidth > 0')
                    if is_visible:
                        print(f"            ✅ Модальное окно найдено: {selector}")
                        return True
            except:
                continue
        
        # Альтернативный способ - проверяем изменение URL или появление новых элементов
        await page.wait_for_timeout(2000)
        
        # Ищем кнопки бронирования
        booking_button_selectors = [
            'button:has-text("Забронировать")',
            'button:has-text("ЗАБРОНИРОВАТЬ")',
            'button:has-text("Подтвердить")',
            'button:has-text("Записаться")',
            'button[class*="book"]',
            'button[class*="confirm"]'
        ]
        
        for selector in booking_button_selectors:
            try:
                button = await page.query_selector(selector)
                if button and await button.is_visible():
                    print(f"            ✅ Найдена кнопка бронирования: {selector}")
                    return True
            except:
                continue
        
        print(f"            ❌ Модальное окно не найдено")
        return False
        
    except Exception as e:
        print(f"            ❌ Ошибка ожидания модального окна: {e}")
        return False

async def try_book_winbox_slot_improved(page, slot_info: dict, config: SiteConfig) -> bool:
    """🎯 УЛУЧШЕННАЯ попытка забронировать конкретный слот"""
    text = slot_info['text']
    
    try:
        print(f"            🎯 БРОНИРОВАНИЕ СЛОТА: '{text}'")
        
        # Шаг 1: Кликаем по слоту разными способами
        click_success = await try_click_slot_multiple_ways(page, slot_info)
        
        if not click_success:
            print(f"            ❌ Не удалось кликнуть по слоту: '{text}'")
            return False
        
        # Шаг 2: Ждем появления модального окна или формы бронирования
        modal_appeared = await wait_for_booking_modal(page)
        
        if not modal_appeared:
            print(f"            ⚠️ Модальное окно не появилось, но продолжаем поиск кнопки бронирования")
        
        # Шаг 3: Ищем и нажимаем кнопку подтверждения бронирования
        book_button_selectors = [
            'button:has-text("Забронировать")',
            'button:has-text("ЗАБРОНИРОВАТЬ")',
            'button:has-text("Подтвердить")',
            'button:has-text("ПОДТВЕРДИТЬ")',
            'button:has-text("Записаться")',
            'button:has-text("ЗАПИСАТЬСЯ")',
            'button:has-text("Оплатить")',
            'button:has-text("ОПЛАТИТЬ")',
            'button[class*="book"]',
            'button[class*="confirm"]',
            'button[class*="submit"]',
            'input[type="submit"]',
            'button[type="submit"]',
            'form button',
            '.btn:has-text("Забронировать")',
            '.button:has-text("Забронировать")'
        ]
        
        book_button = None
        book_button_text = ""
        
        # Ищем кнопку бронирования
        for selector in book_button_selectors:
            try:
                buttons = await page.query_selector_all(selector)
                for button in buttons:
                    if await button.is_visible():
                        is_enabled = await button.evaluate('''
                            el => !el.disabled && 
                                  !el.classList.contains("disabled") &&
                                  getComputedStyle(el).pointerEvents !== "none"
                        ''')
                        if is_enabled:
                            book_button = button
                            book_button_text = await button.text_content()
                            print(f"            👆 Найдена кнопка бронирования: '{book_button_text.strip()}'")
                            break
                if book_button:
                    break
            except:
                continue
        
        if not book_button:
            print(f"            ❌ Кнопка бронирования не найдена для слота: '{text}'")
            return False
        
        # Шаг 4: Нажимаем кнопку бронирования
        try:
            await book_button.click()
            print(f"            👆 Нажали кнопку: '{book_button_text.strip()}'")
        except:
            # Пытаемся JavaScript клик
            await book_button.evaluate('el => el.click()')
            print(f"            👆 JavaScript клик по кнопке: '{book_button_text.strip()}'")
        
        # Шаг 5: Ждем результата
        await page.wait_for_timeout(5000)
        
        # Шаг 6: Проверяем успешность бронирования
        success_indicators = [
            ':has-text("успешно")',
            ':has-text("Успешно")',
            ':has-text("УСПЕШНО")',
            ':has-text("забронирован")',
            ':has-text("Забронирован")',
            ':has-text("подтверждено")',
            ':has-text("Подтверждено")',
            ':has-text("записан")',
            ':has-text("Записан")',
            ':has-text("оплачено")',
            ':has-text("Оплачено")',
            '[class*="success"]',
            '[class*="confirmed"]',
            '[class*="booked"]'
        ]
        
        for indicator in success_indicators:
            try:
                success_element = await page.wait_for_selector(indicator, timeout=3000)
                if success_element and await success_element.is_visible():
                    success_text = await success_element.text_content()
                    print(f"            🎉 БРОНИРОВАНИЕ УСПЕШНО! Слот: '{text}' - {success_text.strip()}")
                    logger.info(f"УСПЕШНОЕ БРОНИРОВАНИЕ {config.sport_type}: {text} - {success_text}")
                    
                    # Отправляем уведомление о УСПЕШНОМ БРОНИРОВАНИИ
                    booking_notification = (
                        f"🎉 СЛОТ ЗАБРОНИРОВАН!\n\n"
                        f"🏢 {config.name}\n"
                        f"⏰ Время: {text}\n"
                        f"🎾 Вид спорта: {config.sport_type}\n"
                        f"✅ Статус: {success_text.strip()}"
                    )
                    await send_notification(booking_notification)
                    
                    return True
            except:
                continue
        
        # Шаг 7: Проверяем ошибки бронирования
        error_indicators = [
            ':has-text("ошибка")',
            ':has-text("Ошибка")',
            ':has-text("ОШИБКА")',
            ':has-text("недоступно")',
            ':has-text("Недоступно")',
            ':has-text("занято")',
            ':has-text("Занято")',
            ':has-text("не удалось")',
            ':has-text("неудачно")',
            '[class*="error"]',
            '[class*="failed"]'
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
        
        # Если явных индикаторов нет, считаем что бронирование прошло успешно
        print(f"            ✅ Попытка бронирования завершена (предположительно успешно)")
        
        # Отправляем уведомление о предполагаемом успешном бронировании
        booking_notification = (
            f"✅ СЛОТ ВОЗМОЖНО ЗАБРОНИРОВАН\n\n"
            f"🏢 {config.name}\n"
            f"⏰ Время: {text}\n"
            f"🎾 Вид спорта: {config.sport_type}\n"
            f"📝 Статус: Бронирование выполнено (требует проверки)"
        )
        await send_notification(booking_notification)
        
        return True
        
    except Exception as e:
        print(f"            ❌ Критическая ошибка бронирования слота '{text}': {e}")
        logger.error(f"Критическая ошибка бронирования {config.sport_type} '{text}': {e}")
        return False

async def book_winbox_slots_improved(page, date: str, config: SiteConfig) -> List[str]:
    """🤖 УЛУЧШЕННОЕ автобронирование слотов на WinBox"""
    if not config.enable_booking:
        print(f"        📵 Автобронирование отключено для {config.name}")
        return []
    
    print(f"        🤖 УЛУЧШЕННОЕ автобронирование {config.sport_type} для даты {date}...")
    print(f"        🎯 Цель: забронировать {config.slots_to_book} слот(ов)")
    
    booked_slots = []
    booking_attempts = 0
    successful_bookings = 0
    
    try:
        # Основной цикл бронирования
        while successful_bookings < config.slots_to_book and booking_attempts < MAX_BOOKING_ATTEMPTS:
            print(f"        🔄 Попытка бронирования {booking_attempts + 1}/{MAX_BOOKING_ATTEMPTS}")
            
            # Ищем доступные слоты
            available_slots = await find_clickable_time_slots(page, date)
            
            if not available_slots:
                print(f"        ❌ Не найдено доступных слотов для бронирования")
                break
            
            print(f"        📅 Найдено {len(available_slots)} потенциальных слотов")
            
            # Сортируем слоты по времени
            def extract_first_time(slot_info):
                text = slot_info['text']
                time_match = re.search(r'(\d{1,2}):(\d{2})', text)
                if time_match:
                    return (int(time_match.group(1)), int(time_match.group(2)))
                return (0, 0)
            
            # Сортировка: от поздних к ранним или наоборот
            if BOOK_FROM_LATE:
                available_slots.sort(key=extract_first_time, reverse=True)
                print(f"        ⏰ Порядок бронирования: от поздних времен к ранним")
            else:
                available_slots.sort(key=extract_first_time)
                print(f"        ⏰ Порядок бронирования: от ранних времен к поздним")
            
            # Пытаемся забронировать первый доступный слот
            slot_booked = False
            for slot_info in available_slots:
                if await try_book_winbox_slot_improved(page, slot_info, config):
                    booked_slots.append(slot_info['text'])
                    successful_bookings += 1
                    slot_booked = True
                    print(f"        🎉 УСПЕШНО забронирован слот {successful_bookings}/{config.slots_to_book}: {slot_info['text']}")
                    
                    # Закрываем модальное окно после успешного бронирования
                    await close_modal_and_return(page)
                    
                    # Перезагружаем страницу для следующего бронирования
                    if successful_bookings < config.slots_to_book:
                        print(f"        🔄 Перезагружаем страницу для следующего бронирования...")
                        await reload_and_reselect_date(page, config, date)
                    
                    break
                else:
                    print(f"        ❌ Не удалось забронировать слот: {slot_info['text']}")
                    await close_modal_and_return(page)
            
            booking_attempts += 1
            
            if not slot_booked:
                print(f"        ❌ В попытке {booking_attempts} не удалось забронировать ни одного слота")
                # Делаем паузу перед следующей попыткой
                await page.wait_for_timeout(3000)
            
            # Если достигли цели - выходим
            if successful_bookings >= config.slots_to_book:
                print(f"        🎉 ЦЕЛЬ ДОСТИГНУТА! Успешно забронировано {successful_bookings} слотов")
                break
        
        if successful_bookings > 0:
            print(f"        ✅ ИТОГО забронировано {successful_bookings} слотов: {', '.join(booked_slots)}")
        else:
            print(f"        ❌ НЕ УДАЛОСЬ забронировать ни одного слота")
        
        return booked_slots
        
    except Exception as e:
        print(f"        ❌ Критическая ошибка улучшенного автобронирования: {e}")
        logger.error(f"Критическая ошибка улучшенного автобронирования {config.sport_type}: {e}")
        return booked_slots

# ─── ФУНКЦИИ МОНИТОРИНГА ZONA PADELA ──────────────────────────────────────────

async def determine_slot_availability(text: str, container) -> str:
    """Определяет доступность слота по тексту и элементам"""
    text_lower = text.lower()
    
    # ✅ НОВАЯ ЛОГИКА: проверяем "Осталось X мест" ПЕРВЫМ
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
            print(f"          ❌ Найден индикатор недоступности: '{indicator}'")
            return "unavailable"
    
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
    """🔍 УЛУЧШЕННЫЙ анализ слотов на Zona Padela с проверкой "Осталось 1-8 мест"""
    print(f"      🔍 Детальный анализ слотов для даты {date}...")
    logger.info(f"Начинаем детальный анализ слотов для даты {date}")
    
    try:
        # ✅ Получаем весь текст страницы для анализа
        page_text = await page.evaluate('() => document.body.innerText')
        
        # ✅ ПЕРВИЧНАЯ ПРОВЕРКА: есть ли критические стоп-слова
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
        
        # ✅ НОВАЯ ЛОГИКА: ищем конкретно "Осталось X мест" где X от 1 до 8
        available_slots = []
        
        # Ищем паттерн "Осталось X мест" с числом от 1 до 8
        remains_pattern = r'осталось\s+([1-8])\s+мест[оа]?'
        remains_matches = re.finditer(remains_pattern, page_text.lower())
        
        print(f"      🔍 Ищем паттерн 'Осталось 1-8 мест' в тексте...")
        
        for match in remains_matches:
            places_count = int(match.group(1))
            # Ищем время рядом с этим текстом
            context_start = max(0, match.start() - 100)
            context_end = min(len(page_text), match.end() + 100)
            context = page_text[context_start:context_end]
            
            # Ищем время в контексте
            time_matches = re.findall(r'(\d{1,2}:\d{2})', context)
            
            for time_text in time_matches:
                if time_text not in available_slots:
                    available_slots.append(time_text)
                    print(f"        ✅ Найден доступный слот: {time_text} (осталось {places_count} мест)")
        
        # ✅ ДОПОЛНИТЕЛЬНАЯ ПРОВЕРКА: ищем элементы с "осталось"
        try:
            remains_elements = await page.query_selector_all('*:has-text("осталось")')
            
            for element in remains_elements:
                try:
                    element_text = await element.text_content()
                    if element_text and 'осталось' in element_text.lower():
                        # Проверяем есть ли число от 1 до 8
                        number_match = re.search(r'осталось\s+([1-8])\s+мест?', element_text.lower())
                        if number_match:
                            places_count = int(number_match.group(1))
                            
                            # Ищем время в тексте элемента
                            time_match = re.search(r'(\d{1,2}:\d{2})', element_text)
                            if time_match:
                                time_text = time_match.group(1)
                                if time_text not in available_slots:
                                    available_slots.append(time_text)
                                    print(f"        ✅ Найден доступный слот из элемента: {time_text} (осталось {places_count} мест)")
                except:
                    continue
        except:
            print("      ⚠️ Не удалось найти элементы с 'осталось'")
        
        if available_slots:
            print(f"      🎉 НАЙДЕНЫ ДОСТУПНЫЕ СЛОТЫ с местами от 1 до 8:")
            for slot in available_slots:
                print(f"        ✅ {slot}")
                logger.info(f"Доступный слот Zona Padela: {slot}")
        else:
            print(f"      ❌ НЕТ доступных слотов с 1-8 местами для даты {date}")
        
        return available_slots
        
    except Exception as e:
        print(f"      ❌ Ошибка анализа слотов: {e}")
        logger.error(f"Ошибка анализа слотов для {date}: {e}")
        return []

async def check_zona_padela_date_single(page, date: str, config: SiteConfig) -> List[str]:
    """📅 Проверка даты на Zona Padela с улучшенной логикой"""
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

async def check_winbox_date_single_improved(page, date: str, config: SiteConfig) -> Dict:
    """📅 УЛУЧШЕННАЯ проверка даты на WinBox с новым механизмом бронирования"""
    print(f"    📅 УЛУЧШЕННАЯ проверка даты {date} для {config.sport_type} ({config.name})...")
    
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
            
            # Проверяем наличие временных слотов и стоп-слов
            has_time_slots = has_time_slots_in_text(page_text)
            has_stop_words = check_critical_stop_words(page_text, STOP_WORDS_WINBOX)
            
            print(f"      🕐 Временные слоты найдены: {'ДА' if has_time_slots else 'НЕТ'}")
            print(f"      🛑 Стоп-слова найдены: {'ДА' if has_stop_words else 'НЕТ'}")
            
            # Если есть временные слоты - ищем кликабельные элементы
            if has_time_slots:
                available_slots_info = await find_clickable_time_slots(page, date)
                
                if available_slots_info:
                    print(f"      ✅ Дата {date}: найдено {len(available_slots_info)} потенциально кликабельных слотов")
                    
                    # Пытаемся забронировать слоты если включено автобронирование
                    booked_slots = []
                    if config.enable_booking:
                        booked_slots = await book_winbox_slots_improved(page, date, config)
                        if booked_slots:
                            print(f"      🎉 Забронированы {config.sport_type} слоты: {', '.join(booked_slots)}")
                            logger.info(f"Забронированы {config.sport_type} слоты {date}: {', '.join(booked_slots)}")
                    
                    # Извлекаем тексты слотов для уведомления
                    slot_texts = [slot_info['text'] for slot_info in available_slots_info]
                    
                    return {
                        "status": "available",
                        "slots": slot_texts,
                        "booked_slots": booked_slots,
                        "message": f"Доступно {len(available_slots_info)} потенциально кликабельных слотов",
                        "has_stop_words": has_stop_words
                    }
                else:
                    # Есть временные слоты в тексте, но нет кликабельных элементов
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
                        "status": "time_slots_detected",
                        "slots": unique_times,
                        "message": f"Обнаружены временные слоты в тексте ({len(unique_times)} шт.), но элементы не кликабельны",
                        "has_stop_words": has_stop_words
                    }
            
            # Если нет временных слотов
            if has_stop_words:
                return {
                    "status": "no_slots",
                    "message": "Найдены стоп-слова, временных слотов нет",
                    "has_stop_words": True
                }
            else:
                return {
                    "status": "no_time_detected",
                    "message": "Временные слоты не обнаружены",
                    "has_stop_words": False
                }
            
        except Exception as e:
            print(f"      ⚠️ Не удалось получить текст страницы: {e}")
        
        print(f"      ⚠️ Дата {date}: не удалось определить статус слотов")
        return {"status": "unknown", "message": "Не удалось определить статус"}
        
    except Exception as e:
        print(f"      ❌ Ошибка проверки даты {date}: {e}")
        logger.error(f"Ошибка улучшенной проверки WinBox {date}: {e}")
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
                # Для WinBox - используем улучшенную проверку
                result = await check_winbox_date_single_improved(page, date, config)
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
                    print(f"      ✅ Попытка {attempt}: НАЙДЕНО {len(slots)} доступных слотов")
                else:
                    print(f"      ❌ Попытка {attempt}: НЕ НАЙДЕНО")
            
            # Небольшая пауза между попытками
            if attempt < VERIFICATION_ATTEMPTS:
                await page.wait_for_timeout(ATTEMPT_DELAY * 1000)
                
        except Exception as e:
            print(f"      ⚠️ Ошибка в попытке {attempt}: {e}")
            logger.error(f"Ошибка проверки {config.sport_type} {date}, попытка {attempt}: {e}")
    
    # Анализируем результаты
    final_slots = list(all_found_slots)
    
    print(f"  📊 ИТОГИ ТРОЙНОЙ ПРОВЕРКИ для {date}:")
    print(f"    ✅ Успешных проверок: {successful_checks}/{VERIFICATION_ATTEMPTS}")
    print(f"    🕐 Временные слоты обнаружены: {'ДА' if time_slots_detected else 'НЕТ'}")
    print(f"    📝 Уникальных слотов найдено: {len(final_slots)}")
    
    # ✅ НОВАЯ ЛОГИКА: возвращаем слоты если есть достаточно успешных проверок ИЛИ обнаружены временные слоты
    if successful_checks >= SUCCESS_THRESHOLD or (time_slots_detected and len(final_slots) > 0):
        print(f"    🎉 ДАТА {date} ПРИЗНАНА ДОСТУПНОЙ!")
        logger.info(f"Дата {date} {config.sport_type} признана доступной: {len(final_slots)} слотов")
        return final_slots
    else:
        print(f"    ❌ Дата {date} недоступна (недостаточно успешных проверок)")
        return []

# ─── ОСНОВНЫЕ ФУНКЦИИ МОНИТОРИНГА ─────────────────────────────────────────────

async def check_site_comprehensive(config: SiteConfig) -> Dict:
    """🔍 КОМПЛЕКСНАЯ проверка сайта с авторизацией и мониторингом"""
    print(f"\n🔍 ПРОВЕРЯЕМ: {config.name}")
    logger.info(f"Начинаем проверку {config.name}")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled',
                '--disable-features=VizDisplayCompositor'
            ]
        )
        
        try:
            context = await browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                viewport={'width': 1920, 'height': 1080}
            )
            
            page = await context.new_page()
            
            # Переходим на сайт
            try:
                await page.goto(config.url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
                print(f"  ✅ Страница загружена: {config.url}")
            except Exception as e:
                print(f"  ❌ Ошибка загрузки страницы: {e}")
                return {"status": "error", "message": f"Не удалось загрузить страницу: {e}"}
            
            # Авторизация если требуется
            if config.needs_auth and config.account_index is not None:
                print(f"  🔐 Выполняем авторизацию...")
                auth_success = await login_to_winbox(page, config.account_index)
                if not auth_success:
                    print(f"  ❌ Авторизация не удалась")
                    return {"status": "auth_failed", "message": "Авторизация не удалась"}
                
                # После авторизации переходим обратно на нужную страницу
                await page.goto(config.url, wait_until="domcontentloaded")
                print(f"  ✅ Перешли на страницу расписания после авторизации")
            
            # Переключение на месяц если нужно
            if config.month_tab:
                try:
                    await page.wait_for_selector(f"text={config.month_tab}", timeout=ELEMENT_WAIT_TIMEOUT)
                    await page.click(f"text={config.month_tab}")
                    await page.wait_for_timeout(DEFAULT_WAIT)
                    print(f"  ✅ Переключились на месяц: {config.month_tab}")
                except PlaywrightTimeoutError:
                    print(f"  ⚠️ Не найден месяц {config.month_tab}, продолжаем без переключения")
            
            # Проверяем каждую дату
            all_available_dates = {}
            total_slots_found = 0
            
            for date in config.dates:
                print(f"\n  📅 Проверяем дату {date}...")
                
                try:
                    available_slots = await verify_date_multiple_times(page, date, config)
                    
                    if available_slots:
                        all_available_dates[date] = available_slots
                        total_slots_found += len(available_slots)
                        print(f"  🎉 Дата {date}: найдено {len(available_slots)} слотов")
                        logger.info(f"{config.name} - дата {date}: {len(available_slots)} доступных слотов")
                    else:
                        print(f"  ❌ Дата {date}: слоты не найдены")
                        
                except Exception as e:
                    print(f"  ❌ Ошибка проверки даты {date}: {e}")
                    logger.error(f"Ошибка проверки даты {date} на {config.name}: {e}")
            
            # Формируем итоговый результат
            if all_available_dates:
                return {
                    "status": "success",
                    "available_dates": all_available_dates,
                    "total_slots": total_slots_found,
                    "message": f"Найдено {total_slots_found} доступных слотов"
                }
            else:
                return {
                    "status": "no_slots",
                    "message": "Доступные слоты не найдены"
                }
                
        except Exception as e:
            print(f"  ❌ Критическая ошибка: {e}")
            logger.error(f"Критическая ошибка проверки {config.name}: {e}")
            return {"status": "error", "message": f"Критическая ошибка: {e}"}
        
        finally:
            await browser.close()

async def format_and_send_notifications(site_results: Dict[str, Dict]):
    """📬 ФОРМАТИРОВАНИЕ и отправка уведомлений о найденных слотах"""
    
    # Группируем результаты по типам
    successful_sites = []
    failed_sites = []
    
    for site_name, result in site_results.items():
        if result.get("status") == "success":
            successful_sites.append((site_name, result))
        else:
            failed_sites.append((site_name, result))
    
    # Отправляем уведомления только о НАЙДЕННЫХ слотах
    if successful_sites:
        for site_name, result in successful_sites:
            available_dates = result.get("available_dates", {})
            total_slots = result.get("total_slots", 0)
            
            # Формируем подробное уведомление
            notification_parts = [
                f"🎾 НАЙДЕНЫ СЛОТЫ! 🎾",
                f"",
                f"🏢 Сайт: {site_name}",
                f"📊 Всего слотов: {total_slots}",
                f""
            ]
            
            # Добавляем детали по датам
            for date, slots in available_dates.items():
                notification_parts.append(f"📅 {date} августа:")
                for slot in slots[:10]:  # Максимум 10 слотов на дату
                    notification_parts.append(f"   ⏰ {slot}")
                if len(slots) > 10:
                    notification_parts.append(f"   ... и еще {len(slots) - 10} слотов")
                notification_parts.append("")
            
            # Добавляем временную метку
            current_time = datetime.now().strftime("%H:%M:%S")
            notification_parts.append(f"🕐 Время проверки: {current_time}")
            
            notification_text = "\n".join(notification_parts)
            
            # Отправляем уведомление
            try:
                await send_notification(notification_text)
                print(f"📬 Отправлено уведомление для {site_name}")
            except Exception as e:
                print(f"❌ Ошибка отправки уведомления для {site_name}: {e}")
    else:
        print("📵 Нет доступных слотов для уведомлений")

async def main_monitoring_loop():
    """🔄 ОСНОВНОЙ ЦИКЛ МОНИТОРИНГА"""
    print(f"🚀 ЗАПУСК СИСТЕМЫ МОНИТОРИНГА СПОРТИВНЫХ ПЛОЩАДОК")
    print(f"⏱️  Интервал проверки: {CHECK_EVERY} секунд")
    print(f"🎯 Мониторим {len(SITES)} сайтов:")
    
    for i, site in enumerate(SITES, 1):
        print(f"  {i}. {site.name} - {site.sport_type}")
        print(f"     📅 Даты: {', '.join(site.dates)}")
        if site.enable_booking:
            print(f"     🤖 Автобронирование: ВКЛЮЧЕНО ({site.slots_to_book} слотов)")
        else:
            print(f"     👀 Режим: только мониторинг")
    
    print(f"\n" + "="*80)
    
    iteration = 0
    
    while True:
        iteration += 1
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        print(f"\n🔄 ИТЕРАЦИЯ {iteration} - {current_time}")
        print("="*80)
        
        # Результаты проверки всех сайтов
        all_results = {}
        
        # Проверяем каждый сайт
        for site in SITES:
            try:
                result = await check_site_comprehensive(site)
                all_results[site.name] = result
                
                # Выводим краткий результат
                if result.get("status") == "success":
                    total_slots = result.get("total_slots", 0)
                    print(f"✅ {site.name}: {total_slots} слотов найдено")
                else:
                    status_msg = result.get("message", "неизвестная ошибка")
                    print(f"❌ {site.name}: {status_msg}")
                    
            except Exception as e:
                print(f"❌ {site.name}: критическая ошибка - {e}")
                logger.error(f"Критическая ошибка проверки {site.name}: {e}")
                all_results[site.name] = {"status": "error", "message": str(e)}
        
        # Отправляем уведомления о найденных слотах
        try:
            await format_and_send_notifications(all_results)
        except Exception as e:
            print(f"❌ Ошибка отправки уведомлений: {e}")
            logger.error(f"Ошибка отправки уведомлений: {e}")
        
        # Выводим итоги итерации
        successful_sites = sum(1 for r in all_results.values() if r.get("status") == "success")
        total_slots = sum(r.get("total_slots", 0) for r in all_results.values() if r.get("status") == "success")
        
        print(f"\n📊 ИТОГИ ИТЕРАЦИИ {iteration}:")
        print(f"  ✅ Успешных проверок: {successful_sites}/{len(SITES)}")
        print(f"  🎯 Всего слотов найдено: {total_slots}")
        print(f"  ⏰ Следующая проверка через {CHECK_EVERY} секунд...")
        
        # Ожидание до следующей итерации
        await asyncio.sleep(CHECK_EVERY)

if __name__ == "__main__":
    try:
        asyncio.run(main_monitoring_loop())
    except KeyboardInterrupt:
        print(f"\n⏹️  Мониторинг остановлен пользователем")
    except Exception as e:
        print(f"\n💥 Критическая ошибка системы: {e}")
        logger.critical(f"Критическая ошибка системы: {e}")