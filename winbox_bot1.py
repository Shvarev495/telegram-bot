import asyncio
import re
import logging
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from telegram import Bot
from dataclasses import dataclass
from typing import List, Optional

# === ЛОГИРОВАНИЕ ===
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

# === ПАРАМЕТРЫ ===
TELEGRAM_TOKEN = "8195557403:AAGGWvI04F0jxswbDsk6gVQohz6KChBAho0"
CHAT_IDS = [
    "1599566837"
]

CHECK_EVERY = 1  # секунд между циклами проверки
VERIFICATION_ATTEMPTS = 3  # число попыток проверки каждой даты
SUCCESS_THRESHOLD = 2      # минимум успешных попыток для подтверждения наличия слотов
ATTEMPT_DELAY = 1          # пауза (с) между попытками в тройной проверке

# 🤖 АВТОБРОНИРОВАНИЕ (для WinBox)
AUTO_BOOKING_WINBOX = True      # включено ли автобронирование на WinBox
BOOK_FROM_LATE = True           # бронировать начиная с поздних слотов (True) или с ранних (False)
MAX_BOOKING_ATTEMPTS = 5        # макс. попыток бронирования слотов за один поиск
BOOKING_DELAY = 1               # пауза (с) между попытками бронирования

# 🎾 КОЛИЧЕСТВО СЛОТОВ ДЛЯ БРОНИРОВАНИЯ
PADEL_SLOTS_TO_BOOK = 2  # на WinBox (падел)
ZONA_SLOTS_TO_BOOK = 1   # на Zona Padela (автобронирование отключено, используется только для информации)

# 🔐 ДАННЫЕ ДЛЯ АВТОРИЗАЦИИ WINBOX (замените на свои реальные учетные данные)
WINBOX_PHONE = "shvarev03@gmail.com"
WINBOX_PASSWORD = "7538tuti"

# ⏰ ТАЙМАУТЫ
PAGE_LOAD_TIMEOUT = 60000     # максимум 60 сек на загрузку страницы
ELEMENT_WAIT_TIMEOUT = 45000  # максимум 45 сек на появление элемента
DEFAULT_WAIT = 1500           # стандартная короткая пауза (мс)

@dataclass
class SiteConfig:
    name: str
    url: str
    dates: List[str]
    month_tab: Optional[str] = None
    check_type: str = "slots"    # "slots" для WinBox, "clickable" для Zona Padela
    sport_type: str = "padel"
    slots_to_book: int = 1
    needs_auth: bool = False
    enable_booking: bool = False
    date_in_url: bool = False

# 📋 КОНФИГУРАЦИЯ САЙТОВ ДЛЯ МОНИТОРИНГА
SITES = [
    SiteConfig(
        name="Winbox Падел",
        url="https://winboxmsk.ru/schedule?types=padel",
        dates=["21", "22", "23", "24"],   # укажите интересующие даты (дни месяца)
        month_tab="АВГУСТ",               # название вкладки месяца (если требуется переключение)
        check_type="slots",
        sport_type="padel",
        slots_to_book=PADEL_SLOTS_TO_BOOK,
        needs_auth=True,
        enable_booking=True,
        date_in_url=False
    ),
    SiteConfig(
        name="Zona Padela",
        url="https://n1594888.yclients.com/company/1434507/activity/select?o=act",  # базовый URL (дата будет добавлена параметром)
        dates=["21", "22", "23", "24"],
        month_tab="Август",               # название месяца (если нужно выбрать вручную)
        check_type="clickable",
        sport_type="padel",
        slots_to_book=ZONA_SLOTS_TO_BOOK,
        needs_auth=False,
        enable_booking=False,
        date_in_url=True
    )
]

# ✅ СТОП-СЛОВА ДЛЯ ПРОВЕРКИ СТРАНИЦ
STOP_WORDS_WINBOX = [
    "бронирование недоступно",
    "win box отдыхает",
    "winbox отдыхает",
    "вернёмся скоро",
    "нет мест",
    "недоступно",
    "отдыхает",
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

# === ФУНКЦИИ АВТОРИЗАЦИИ WINBOX ===
async def login_to_winbox(page) -> bool:
    """Авторизация на WinBox с улучшенным поиском полей ввода и кнопок."""
    print("    🔐 Проверяем авторизацию на WinBox...")
    try:
        # Проверяем, не выполнен ли вход уже
        profile_indicators = [
            ':has-text("Личный кабинет")',
            ':has-text("Мой профиль")',
            ':has-text("Выйти")',
            '[class*="profile"]',
            '[class*="user"]',
            ':has-text("Мои записи")'
        ]
        for selector in profile_indicators:
            try:
                element = await page.query_selector(selector)
                if element and await element.is_visible():
                    print("    ✅ Пользователь уже авторизован на WinBox")
                    return True
            except:
                continue

        # Ищем и открываем форму входа (если она не открыта сразу)
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
        email_field = None
        password_field = None

        # Пытаемся сразу найти поле email на странице
        email_selectors = [
            'input[type="email"]',
            'input[placeholder*="почт"]',
            'input[placeholder*="mail"]',
            'input[name*="email"]',
            'input[id*="email"]',
            'input[autocomplete="email"]',
            'input[autocomplete="username"]'
        ]
        for selector in email_selectors:
            try:
                field = await page.query_selector(selector)
                if field and await field.is_visible():
                    email_field = field
                    print("    📧 Поле для email обнаружено")
                    break
            except:
                continue

        # Если поля не найдены, кликаем по кнопке входа, чтобы отобразить форму авторизации
        if not email_field:
            for trigger in auth_triggers:
                try:
                    buttons = await page.query_selector_all(trigger)
                    for btn in buttons:
                        if await btn.is_visible():
                            btn_text = (await btn.text_content() or "").strip()
                            print(f'    🔘 Кликаем по элементу входа: "{btn_text}"')
                            await btn.click()
                            await page.wait_for_timeout(1000)
                            break
                    if email_field:
                        break
                except:
                    continue
            # После попытки открыть форму, снова ищем поле email
            for selector in email_selectors:
                try:
                    field = await page.query_selector(selector)
                    if field and await field.is_visible():
                        email_field = field
                        print("    📧 Поле email появилось после открытия формы")
                        break
                except:
                    continue

        if not email_field:
            print("    ❌ Не удалось найти поле ввода email на странице")
            return False

        # Ищем поле ввода пароля
        password_selectors = [
            'input[type="password"]',
            'input[placeholder*="ароль"]',  # учитываем слова "Пароль", "пароль"
            'input[name*="password"]',
            'input[id*="password"]',
            'input[autocomplete="current-password"]'
        ]
        for selector in password_selectors:
            try:
                field = await page.query_selector(selector)
                if field and await field.is_visible():
                    password_field = field
                    print("    🔑 Поле для пароля обнаружено")
                    break
            except:
                continue

        if not password_field:
            print("    ❌ Не удалось найти поле ввода пароля")
            return False

        # Вводим учетные данные
        print(f"    📧 Вводим email: {WINBOX_PHONE}")
        await email_field.click()
        await email_field.fill("")  # очищаем поле на всякий случай
        await page.wait_for_timeout(200)
        await email_field.type(WINBOX_PHONE, delay=100)
        await page.wait_for_timeout(300)

        print("    🔑 Вводим пароль")
        await password_field.click()
        await password_field.fill("")
        await page.wait_for_timeout(200)
        await password_field.type(WINBOX_PASSWORD, delay=100)
        await page.wait_for_timeout(300)

        # Ищем и нажимаем кнопку подтверждения входа
        submit_selectors = [
            'button:has-text("Войти")',
            'button:has-text("ВОЙТИ")',
            'button[type="submit"]',
            'input[type="submit"]'
        ]
        submit_button = None
        for selector in submit_selectors:
            try:
                candidates = await page.query_selector_all(selector)
                for btn in candidates:
                    if await btn.is_visible():
                        enabled = await btn.evaluate('el => !el.disabled')
                        if enabled:
                            submit_button = btn
                            break
                if submit_button:
                    break
            except:
                continue

        if submit_button:
            print("    👆 Нажимаем кнопку Войти")
            await submit_button.click()
        else:
            print("    ⌨️ Нажимаем Enter для подтверждения входа")
            await password_field.press('Enter')

        # Ожидаем завершения авторизации
        await page.wait_for_timeout(5000)
        for selector in profile_indicators:
            try:
                element = await page.query_selector(selector)
                if element and await element.is_visible():
                    print("    ✅ Авторизация прошла успешно (обнаружены элементы профиля)")
                    logger.info("Успешный вход в аккаунт WinBox")
                    return True
            except:
                continue

        # Проверяем возможные сообщения об ошибке авторизации
        error_indicators = [
            ':has-text("Неверный")',
            ':has-text("ошибка")',
            ':has-text("неправильн")',
            '[class*="error"]',
            '[role="alert"]'
        ]
        for selector in error_indicators:
            try:
                err_elem = await page.query_selector(selector)
                if err_elem and await err_elem.is_visible():
                    err_text = (await err_elem.text_content() or "").strip()
                    print(f"    ❌ Ошибка при входе: {err_text}")
                    logger.error(f"Ошибка авторизации WinBox: {err_text}")
                    return False
            except:
                continue

        # Если ни успех, ни ошибка явно не определились
        print("    ⚠️ Результат входа не определён однозначно, продолжаем как будто авторизация успешна")
        return True

    except Exception as e:
        print(f"    ❌ Исключение при авторизации WinBox: {e}")
        logger.error(f"Исключение во время авторизации WinBox: {e}")
        return False

# === ФУНКЦИИ ДЛЯ ОТПРАВКИ УВЕДОМЛЕНИЙ ===
async def send_notification(text: str):
    """Отправляет текстовое сообщение всем указанным чатам в Telegram."""
    bot = Bot(TELEGRAM_TOKEN)
    for chat_id in CHAT_IDS:
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            print(f"📬 Уведомление отправлено (чат {chat_id})")
            logger.info(f"Уведомление отправлено в чат {chat_id}")
        except Exception as e:
            print(f"❌ Ошибка отправки уведомления для {chat_id}: {e}")
            logger.error(f"Ошибка отправки уведомления для {chat_id}: {e}")

def check_critical_stop_words(text: str, stop_words: List[str]) -> bool:
    """Проверяет наличие любых стоп-слов в тексте (без учёта регистра). Возвращает True, если найдено."""
    if not text:
        return False
    text_lower = text.lower()
    for word in stop_words:
        if word.lower() in text_lower:
            print(f"    🛑 Обнаружено стоп-слово: \"{word}\"")
            logger.warning(f"Найдено критическое слово на странице: {word}")
            return True
    return False

def parse_time_slot(slot_text: str) -> tuple:
    """Парсит время слота в формате (часы, минуты). Например, '9:30' -> (9, 30)."""
    match = re.search(r'(\d{1,2}):(\d{2})', slot_text)
    if match:
        return int(match.group(1)), int(match.group(2))
    return (0, 0)

# === ФУНКЦИИ АНАЛИЗА СЛОТОВ (ZONA PADELA) ===
async def determine_slot_availability(text: str) -> str:
    """Анализирует текст слота и возвращает 'available' или 'unavailable'."""
    t = text.lower()
    # Явные признаки недоступности
    for marker in ["нет мест", "недоступно", "занято", "забронировано", "закрыто", "заблокировано", "осталось: 0", "мест: 0"]:
        if marker in t:
            return "unavailable"
    # Обработка шаблонов "осталось X"
    match = re.search(r'осталось\s+(\d+)', t)
    if match:
        count = int(match.group(1))
        return "available" if count > 0 else "unavailable"
    # Другие указания количества мест
    match2 = re.search(r'(\d+)\s*мест', t)
    if match2:
        count = int(match2.group(1))
        return "available" if count > 0 else "unavailable"
    # Явные слова доступности
    for marker in ["записаться", "забронировать", "доступно", "свободн", "есть места"]:
        if marker in t:
            return "available"
    # По умолчанию считаем недоступным
    return "unavailable"

async def analyze_available_slots(page, date: str) -> List[str]:
    """Проверяет страницу Zona Padela на наличие доступных слотов, возвращает список доступных времен (чч:мм)."""
    print(f"      🔍 Анализируем наличие слотов на дату {date}...")
    try:
        page_text = await page.evaluate('() => document.body.innerText')  # получаем весь текст страницы
        # Проверяем критические фразы об отсутствии событий
        date_specific_stops = [
            f"{date} августа — нет событий",
            f"{date} августа нет событий",
            f"на {date} августа нет событий"
        ]
        if check_critical_stop_words(page_text, CRITICAL_STOP_WORDS_ZONA + date_specific_stops):
            return []  # критическое отсутствие слотов/событий

        available_times = []
        # Ищем элементы, содержащие слово "осталось" (признак слота с количеством мест)
        elements = await page.query_selector_all('*:has-text("осталось")')
        for elem in elements:
            text = (await elem.text_content() or "")
            if "осталось" in text.lower():
                time_match = re.search(r'(\d{1,2}:\d{2})', text)
                status = await determine_slot_availability(text)
                if time_match and status == "available":
                    available_times.append(time_match.group(1))
                    print(f"        ✅ Найден свободный слот: {time_match.group(1)}")
        # Удаляем возможные дубликаты и сортируем времена
        available_times = sorted(set(available_times))
        if available_times:
            print(f"      🎾 Доступные слоты на {date}: {', '.join(available_times)}")
        else:
            print(f"      ❌ На {date} свободных слотов не обнаружено")
        return available_times
    except Exception as e:
        print(f"      ❌ Ошибка при анализе слотов (Zona Padela): {e}")
        logger.error(f"Zona Padela - ошибка анализа слотов на дату {date}: {e}")
        return []

# === ФУНКЦИИ ДЛЯ БРОНИРОВАНИЯ СЛОТОВ (WINBOX) ===
async def close_modal_and_return(page):
    """Закрывает модальное окно бронирования (если открыто) и возвращается к расписанию."""
    try:
        selectors = [
            'button[aria-label="Close"]',
            'button:has-text("Закрыть")',
            'button:has-text("✕")',
            'button:has-text("×")',
            'button:has-text("X")'
        ]
        for sel in selectors:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    await page.wait_for_timeout(500)
                    print("        🔐 Закрыли всплывающее окно бронирования")
                    return True
            except:
                continue
        # Попробуем нажать Escape, если прямой кнопки закрытия не нашли
        await page.keyboard.press("Escape")
        return True
    except Exception:
        return False

async def reload_page_for_date(page, config: SiteConfig, date: str):
    """Перезагружает страницу расписания WinBox и выбирает нужный месяц и дату заново."""
    try:
        await page.goto(config.url, wait_until="networkidle")
        await page.wait_for_timeout(1000)
        if config.month_tab:
            try:
                await page.click(f"text={config.month_tab}")
                await page.wait_for_timeout(500)
                print(f"          🔄 Переключились на месяц {config.month_tab} (после обновления)")
            except:
                pass
        # Кликаем необходимую дату на обновлённой странице
        selectors = [
            f'button:has-text("{date}")',
            f'div:has-text("{date}")',
            f'[data-date="{date}"]',
            f'[data-day="{date}"]',
            f'td:has-text("{date}")'
        ]
        for sel in selectors:
            try:
                elements = await page.query_selector_all(sel)
                for el in elements:
                    text = (await el.text_content() or "").strip()
                    if text == date and await el.is_visible():
                        await el.click()
                        await page.wait_for_timeout(500)
                        print(f"          🔄 После перезагрузки снова выбрана дата {date}")
                        return True
            except:
                continue
        return False
    except Exception as e:
        print(f"          ❌ Ошибка при перезагрузке страницы WinBox: {e}")
        return False

async def try_book_winbox_slot(page, slot_element, slot_text: str) -> bool:
    """Пытается выполнить бронирование выбранного слота на WinBox."""
    try:
        print(f"            🏸 Кликаем по слоту {slot_text} для бронирования...")
        await slot_element.click()
        await page.wait_for_timeout(500)
        # Ищем кнопку подтверждения бронирования (в модальном окне)
        confirm_selectors = [
            'button:has-text("ЗАБРОНИРОВАТЬ")',
            'button:has-text("Подтвердить")',
            'button:has-text("Записаться")',
            'button[type="submit"]'
        ]
        confirm_btn = None
        for sel in confirm_selectors:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    confirm_btn = btn
                    break
            except:
                continue
        if not confirm_btn:
            print("            ❌ Не найдена кнопка подтверждения в окне бронирования")
            return False

        print("            👆 Подтверждаем запись/бронирование слота")
        await confirm_btn.click()
        await page.wait_for_timeout(1000)

        # Проверяем, появилось ли подтверждение успешного бронирования
        success_indicators = [
            ':has-text("успешно")',
            ':has-text("подтвержден")',
            ':has-text("Запись подтверждена")',
            '[class*="success"]'
        ]
        for sel in success_indicators:
            try:
                success_elem = await page.query_selector(sel)
                if success_elem and await success_elem.is_visible():
                    print("            ✅ Бронирование подтверждено (обнаружено сообщение о успехе)")
                    return True
            except:
                continue

        # Если явного сообщения нет, проверяем исчезновение самого слота из списка
        await page.wait_for_timeout(500)
        still_available = await page.query_selector(f'button:has-text("{slot_text}")')
        if not still_available:
            print("            ✅ Слот исчез из расписания (считаем, что забронирован успешно)")
            return True

        print("            ⚠️ Не удалось подтвердить бронирование слота")
        return False
    except Exception as e:
        print(f"            ❌ Ошибка при бронировании слота {slot_text}: {e}")
        return False

async def book_winbox_slots(page, date: str, config: SiteConfig) -> List[str]:
    """Находит все доступные слоты на WinBox и пытается забронировать заданное количество."""
    booked_slots = []
    try:
        # Находим все доступные слоты на странице
        time_selectors = [
            'button:has-text(":")',
            '[class*="slot"]:has-text(":")',
            '[data-time]'
        ]
        available_slots = []
        for sel in time_selectors:
            try:
                slot_elements = await page.query_selector_all(sel)
                for slot in slot_elements:
                    text = (await slot.text_content() or "").strip()
                    if text and ':' in text and len(text) < 6:
                        # Проверяем, что слот свободен (не задизейблен и не помечен как занятый)
                        is_disabled = await slot.evaluate('el => el.disabled || el.classList.contains("disabled") or el.classList.contains("booked") or el.classList.contains("unavailable")')
                        if not is_disabled:
                            available_slots.append((slot, text))
            except:
                continue

        if not available_slots:
            return []  # нет доступных слотов для бронирования

        # Сортируем слоты по времени в заданном порядке (поздние -> ранние или наоборот)
        available_slots.sort(key=lambda x: parse_time_slot(x[1]), reverse=BOOK_FROM_LATE)
        print(f"        ⏳ Найдено свободных слотов: {len(available_slots)}. Начинаем бронирование ({'с поздних' if BOOK_FROM_LATE else 'с ранних'}).")

        attempts = 0
        success = 0
        # Перебираем найденные свободные слоты в указанном порядке
        for slot_elem, slot_time in available_slots:
            if attempts >= MAX_BOOKING_ATTEMPTS or success >= config.slots_to_book:
                break
            attempts += 1
            if await try_book_winbox_slot(page, slot_elem, slot_time):
                booked_slots.append(slot_time)
                success += 1
                logger.info(f"WinBox: слот {slot_time} забронирован")
                # Закрываем модальное окно и обновляем расписание для актуализации статуса слотов
                await close_modal_and_return(page)
                await reload_page_for_date(page, config, date)
            # Пауза между бронированиями (если будем пробовать следующий слот)
            await asyncio.sleep(BOOKING_DELAY)

        if success:
            print(f"        🎉 Успешно забронировано слотов: {success}")
        return booked_slots
    except Exception as e:
        print(f"        ❌ Ошибка процесса бронирования слотов: {e}")
        logger.error(f"Ошибка автобронирования на WinBox: {e}")
        return booked_slots

# === ФУНКЦИИ ПРОВЕРКИ (ОДНОКРАТНОЙ) ДЛЯ КАЖДОГО САЙТА ===
async def check_winbox_date_once(page, date: str, config: SiteConfig) -> (bool, List[str], List[str]):
    """Проверяет наличие свободных слотов на WinBox для одной даты (одна попытка). Возвращает флаг, список слотов и список забронированных слотов."""
    print(f"    📅 Проверяем дату {date} (WinBox)...")
    try:
        # Если нужно, переключаемся на нужный месяц
        if config.month_tab:
            try:
                await page.click(f"text={config.month_tab}")
                await page.wait_for_timeout(500)
                print(f"      📆 Выбрали вкладку месяца: {config.month_tab}")
            except:
                pass

        # Кликаем по дате на календаре расписания
        date_element = None
        selectors = [
            f'button:has-text("{date}")',
            f'div:has-text("{date}")',
            f'[data-date="{date}"]',
            f'td:has-text("{date}")'
        ]
        for sel in selectors:
            try:
                elements = await page.query_selector_all(sel)
                for el in elements:
                    txt = (await el.text_content() or "").strip()
                    if txt == date and await el.is_visible():
                        date_element = el
                        break
                if date_element:
                    break
            except:
                continue

        if not date_element:
            print(f"      ❌ Дата {date} не доступна для выбора на WinBox")
            return (False, [], [])
        await date_element.click()
        await page.wait_for_timeout(500)

        # Проверяем страницу на наличие стоп-слов (технические перерывы, отсутствие доступа и т.п.)
        page_text = await page.evaluate('() => document.body.innerText')
        if check_critical_stop_words(page_text, STOP_WORDS_WINBOX):
            return (False, [], [])

        # Собираем все доступные слоты времени на странице (только текст, без бронирования)
        available_slots = []
        slot_buttons = await page.query_selector_all('button:has-text(":")')
        for btn in slot_buttons:
            text = (await btn.text_content() or "").strip()
            if text and ':' in text and len(text) < 6:
                # Проверяем, что слот не помечен как занятый
                disabled = await btn.evaluate('el => el.disabled || el.classList.contains("disabled") || el.classList.contains("booked") || el.classList.contains("unavailable")')
                if not disabled:
                    available_slots.append(text)
        if available_slots:
            print(f"      ✅ Найдены свободные времена: {', '.join(available_slots)}")
        else:
            print("      ⛔ Свободных слотов нет")

        # Если слоты есть и автобронирование включено – пробуем бронировать
        booked = []
        if available_slots and config.enable_booking and AUTO_BOOKING_WINBOX:
            print("      🤖 Автобронирование включено, начинаем бронировать доступные слоты...")
            booked = await book_winbox_slots(page, date, config)
            if booked:
                print(f"      🤖 Автобронирование: забронированы слоты {', '.join(booked)}")

        return (len(available_slots) > 0, available_slots, booked)
    except Exception as e:
        print(f"      ❌ Ошибка при проверке даты {date} на WinBox: {e}")
        logger.error(f"WinBox: ошибка проверки {date}: {e}")
        return (False, [], [])

async def check_zona_date_once(page, date: str, config: SiteConfig) -> (bool, List[str]):
    """Проверяет наличие слотов на Zona Padela для одной даты (одна попытка). Возвращает флаг и список доступных времен."""
    print(f"    📅 Проверяем дату {date} (Zona Padela)...")
    try:
        if config.date_in_url:
            # Формируем URL с параметром даты (YYYY-MM-DD) и загружаем его
            full_date = f"2025-08-{str(date).zfill(2)}"
            # Убираем из базового URL существующий параметр date, если есть
            base_url = re.sub(r'(\?|&)date=[0-9\-]+', '', config.url)
            joiner = '&' if '?' in base_url else '?'
            target_url = f"{base_url}{joiner}date={full_date}"
            await page.goto(target_url, wait_until="networkidle")
            await page.wait_for_timeout(1000)
            print(f"      🔗 Открыта страница расписания на дату {full_date}")
        else:
            # Загружаем базовую страницу расписания и переключаемся на нужный месяц, если указано
            await page.goto(config.url, wait_until="domcontentloaded")
            await page.wait_for_timeout(1000)
            if config.month_tab:
                try:
                    await page.click(f"text={config.month_tab}")
                    await page.wait_for_timeout(500)
                    print(f"      📆 Переключились на месяц {config.month_tab}")
                except:
                    pass
            # Выбираем дату на календаре
            date_element = None
            date_selectors = [
                f'td:has-text("{date}"):not([class*="other-month"]):not([class*="disabled"])',
                f'button:has-text("{date}"):not([disabled])',
                f'div:has-text("{date}"):not([class*="disabled"])'
            ]
            for sel in date_selectors:
                try:
                    elements = await page.query_selector_all(sel)
                    for el in elements:
                        txt = (await el.text_content() or "").strip()
                        # Убираем лидирующий ноль (например, '08' -> '8') для сравнения текста
                        if txt == str(int(date)):
                            date_element = el
                            break
                    if date_element:
                        break
                except:
                    continue

            if not date_element:
                print(f"      ❌ Дата {date} не активна или не найдена на календаре Zona Padela")
                return (False, [])
            await date_element.click()
            await page.wait_for_timeout(1000)
            print(f"      📆 Нажали на дату {date} на календаре")
        # Анализируем страницу на наличие свободных слотов
        available_times = await analyze_available_slots(page, date)
        return (len(available_times) > 0, available_times)
    except Exception as e:
        print(f"      ❌ Ошибка при проверке даты {date} на Zona Padela: {e}")
        logger.error(f"Zona Padela: ошибка проверки {date}: {e}")
        return (False, [])

# === МНОГОКРАТНАЯ ПРОВЕРКА (ТРОЙНАЯ) ДЛЯ НАДЁЖНОСТИ ===
async def verify_winbox_date(page, date: str, config: SiteConfig) -> dict:
    """Выполняет тройную проверку даты на WinBox для подтверждения наличия слотов."""
    success_count = 0
    any_slots = []
    all_booked = []
    for attempt in range(1, VERIFICATION_ATTEMPTS + 1):
        print(f"  🔄 WinBox {date}: попытка {attempt} из {VERIFICATION_ATTEMPTS}...")
        found, slots, booked = await check_winbox_date_once(page, date, config)
        if found:
            success_count += 1
            if not any_slots:  # запоминаем список слотов с первой успешной попытки
                any_slots = slots
            if booked:
                all_booked.extend(booked)  # добавляем забронированные слоты к общему списку
            print("  ✅ На этой попытке слоты обнаружены")
        else:
            print("  ❌ На этой попытке слоты НЕ обнаружены")
        if attempt < VERIFICATION_ATTEMPTS:
            await asyncio.sleep(ATTEMPT_DELAY)

    confirmed = (success_count >= SUCCESS_THRESHOLD) or (len(all_booked) > 0)
    if confirmed:
        print(f"  🎉 Результат: дата {date} - наличие слотов ПОДТВЕРЖДЕНО ({success_count}/{VERIFICATION_ATTEMPTS} успешных проверок)")
    else:
        print(f"  ⛔ Результат: дата {date} - свободных слотов НЕ НАЙДЕНО надёжно ({success_count}/{VERIFICATION_ATTEMPTS} успешных проверок)")

    return {
        "status": "available" if confirmed else "no_slots",
        "slots": any_slots,
        "booked_slots": all_booked
    }

async def verify_zona_date(page, date: str, config: SiteConfig) -> dict:
    """Выполняет тройную проверку даты на Zona Padela для подтверждения наличия слотов."""
    success_count = 0
    any_slots = []
    for attempt in range(1, VERIFICATION_ATTEMPTS + 1):
        print(f"  🔄 Zona Padela {date}: попытка {attempt} из {VERIFICATION_ATTEMPTS}...")
        found, slots = await check_zona_date_once(page, date, config)
        if found:
            success_count += 1
            if not any_slots:
                any_slots = slots
            print("  ✅ На этой попытке слоты обнаружены")
        else:
            print("  ❌ На этой попытке слоты НЕ обнаружены")
        if attempt < VERIFICATION_ATTEMPTS:
            await asyncio.sleep(ATTEMPT_DELAY)

    confirmed = (success_count >= SUCCESS_THRESHOLD)
    if confirmed:
        print(f"  🎉 Результат: дата {date} - наличие слотов ПОДТВЕРЖДЕНО ({success_count}/{VERIFICATION_ATTEMPTS} успешных проверок)")
    else:
        print(f"  ⛔ Результат: дата {date} - свободных слотов НЕ НАЙДЕНО надёжно ({success_count}/{VERIFICATION_ATTEMPTS} успешных проверок)")

    return {
        "status": "available" if confirmed else "no_slots",
        "slots": any_slots,
        "booked_slots": []  # на Zona Padela бронирование не выполняется
    }

# === ГЛАВНАЯ ФУНКЦИЯ МОНИТОРИНГА ===
async def monitor_sites():
    print("🚀 Запуск мониторинга сайтов...")
    logger.info("Старт мониторинга всех сайтов")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,  # установите True, если не нужен видимый браузер
            args=['--no-sandbox', '--disable-blink-features=AutomationControlled']
        )
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/100.0.0.0 Safari/537.36'
        )
        page = await context.new_page()
        previous_states: dict = {}  # хранение предыдущих статусов для каждого site+date

        try:
            while True:
                print(f"\n=== Новая проверка: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
                for site in SITES:
                    print(f"\n🏢 Сайт: {site.name} | URL: {site.url}")
                    # Загружаем главную страницу сайта
                    try:
                        await page.goto(site.url, wait_until="networkidle", timeout=PAGE_LOAD_TIMEOUT)
                        await page.wait_for_timeout(1000)
                        if site.needs_auth:
                            # Авторизация WinBox при необходимости
                            if not await login_to_winbox(page):
                                print(f"❌ {site.name}: пропускаем (не удалось авторизоваться)")
                                logger.error(f"{site.name}: авторизация не выполнена, сайт пропущен")
                                continue
                    except Exception as e:
                        print(f"❌ Ошибка загрузки {site.name}: {e}")
                        logger.error(f"{site.name}: ошибка при загрузке страницы - {e}")
                        continue

                    # Проверяем все даты из списка для текущего сайта
                    for date in site.dates:
                        site_date_key = f"{site.name}_{date}"
                        if "winbox" in site.url.lower():
                            result = await verify_winbox_date(page, date, site)
                        else:
                            result = await verify_zona_date(page, date, site)

                        prev_status = previous_states.get(site_date_key, {}).get("status")
                        # Если на этой дате слоты сейчас доступны, а раньше не были (или это первая проверка)
                        if result["status"] == "available" and prev_status != "available":
                            # Формируем текст уведомления
                            slots_info = ""
                            if result.get("slots") and result["slots"]:
                                # берем до 3 времен для предварительного просмотра
                                preview = ", ".join(result["slots"][:3])
                                if len(result["slots"]) > 3:
                                    preview += "..."
                                slots_info = f"⏰ Доступные слоты: {preview}"
                            message = (
                                f"🎾 *{site.name}* – появились свободные слоты!\n"
                                f"📅 Дата: {date} августа\n"
                                f"{slots_info}\n"
                                f"🔗 [Открыть расписание]({site.url})"
                            )
                            # Если что-то было автоматически забронировано
                            if result.get("booked_slots"):
                                booked_list = ", ".join(result["booked_slots"])
                                message += f"\n🤖 *Автобронирование:* забронированы слоты {booked_list}"
                            print(f"🔔 Отправляем уведомление: найдены новые слоты на {date} число")
                            await send_notification(message)
                        # Обновляем предыдущий статус данной даты
                        previous_states[site_date_key] = result
                        await asyncio.sleep(1)  # небольшая задержка между проверками разных дат
                print(f"\n⏳ Ожидание {CHECK_EVERY} сек. до следующего цикла проверки...")
                await asyncio.sleep(CHECK_EVERY)
        except Exception as e:
            print(f"💥 Критическая ошибка мониторинга: {e}")
            logger.error(f"Критическая ошибка мониторинга: {e}")
            await send_notification(f"💥 *Мониторинг остановлен из-за ошибки*:\n`{e}`")
        finally:
            await context.close()
            await browser.close()

if __name__ == "__main__":
    try:
        asyncio.run(monitor_sites())
    except KeyboardInterrupt:
        print("🛑 Мониторинг прерван пользователем")
        logger.info("Мониторинг остановлен пользователем")
