from __future__ import annotations

import json
import logging
import os
import re
import datetime
import random
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cardinal import Cardinal

from FunPayAPI.updater.events import NewMessageEvent
from FunPayAPI.common.enums import MessageTypes
from FunPayAPI.common.utils import RegularExpressions
from FunPayAPI.types import Order

from tg_bot import CBT as _CBT, static_keyboards as skb
from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B, Message, CallbackQuery

NAME = "Auto Response Order"
VERSION = "1.0.0"
DESCRIPTION = "Плагин добавляет новую функцию автоматическая отправка сообщения покупателю после оплаты заказа."
CREDITS = "@kewanmov"
UUID = "d63d1dff-843b-4c7f-b4bd-24c352b710b2"
SETTINGS_PAGE = True

logger = logging.getLogger("FPC.AutoResponseOrder")

CBT_MAIN_MENU = "ARO_Main"
CBT_SWITCH = "ARO_Switch"
CBT_TEXT_SHOW = "ARO_ShowText"
CBT_TEXT_EDIT = "ARO_EditText"
CBT_TEXT_EDITED = "ARO_TextEdited"

_STORAGE_PATH = os.path.join(os.path.dirname(__file__), "..", "storage", "plugins", "auto_response_order")
os.makedirs(_STORAGE_PATH, exist_ok=True)
_SETTINGS_FILE = os.path.join(_STORAGE_PATH, "settings.json")

_RE = RegularExpressions()

_MONTHS = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
           "июля", "августа", "сентября", "октября", "ноября", "декабря"]

_lock = threading.Lock()


class Settings:
    def __init__(self):
        self.enabled: bool = False
        self.watermark: bool = False
        self.message_text: str = (
            "Привет, $username!\n"
            "Спасибо за заказ #$order_id.\n"
            "Товар: $order_title\n"
            "Сумма: $price $currency\n"
            "Скоро выдам!"
        )
        self.processed_orders: list[str] = []

    def save(self):
        try:
            self.processed_orders = self.processed_orders[-500:]
            with open(_SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.__dict__, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"Ошибка сохранения настроек: {e}")

    def load(self):
        if not os.path.exists(_SETTINGS_FILE):
            return
        try:
            with open(_SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.enabled = data.get("enabled", self.enabled)
            self.watermark = data.get("watermark", self.watermark)
            self.message_text = data.get("message_text", self.message_text)
            self.processed_orders = data.get("processed_orders", [])
        except Exception as e:
            logger.error(f"Ошибка загрузки настроек: {e}")


SETTINGS = Settings()
SETTINGS.load()


def _safe_attr(obj, attr: str, default: str = "") -> str:
    val = getattr(obj, attr, None)
    if val is None:
        return default
    return str(val)


def _build_replacements(username: str, order_id: str, order: Order) -> dict[str, str]:
    now = datetime.datetime.now()

    subcat = getattr(order, "subcategory", None)
    game_name = subcat.category.name if subcat and hasattr(subcat, "category") else ""
    subcat_name = subcat.name if subcat else ""
    subcat_fullname = getattr(subcat, "fullname", "") if subcat else ""

    lot_params_text = getattr(order, "lot_params_text", None) or ""
    currency_str = str(order.currency) if hasattr(order, "currency") else ""

    return {
        "$date": now.strftime("%d.%m.%Y"),
        "$date_text": f"{now.day} {_MONTHS[now.month]}",
        "$time": now.strftime("%H:%M"),
        "$full_time": now.strftime("%H:%M:%S"),
        "$username": username,
        "$order_id": order_id,
        "$order_link": f"https://funpay.com/orders/{order_id}/",
        "$order_title": _safe_attr(order, "short_description"),
        "$order_desc": _safe_attr(order, "full_description"),
        "$order_params": lot_params_text,
        "$order_desc_or_params": _safe_attr(order, "full_description") or lot_params_text,
        "$buyer": _safe_attr(order, "buyer_username"),
        "$seller": _safe_attr(order, "seller_username"),
        "$game": game_name,
        "$category": f"{subcat_name} {game_name}".strip(),
        "$category_full": subcat_fullname,
        "$price": str(order.sum) if hasattr(order, "sum") and order.sum is not None else "",
        "$currency": currency_str,
        "$amount": str(order.amount) if hasattr(order, "amount") else "1",
    }


def process_text(raw_text: str, username: str, order_id: str, order: Order) -> str:
    def spin(match):
        return random.choice(match.group(1).split("|"))

    text = re.sub(r"\{([^{}]+)}", spin, raw_text)

    replacements = _build_replacements(username, order_id, order)
    for key, value in replacements.items():
        text = text.replace(key, value)

    return text


def message_hook(cardinal: Cardinal, event: NewMessageEvent):
    if not SETTINGS.enabled:
        return

    if event.message.type != MessageTypes.ORDER_PURCHASED:
        return

    if event.message.i_am_buyer:
        return

    order_ids = _RE.ORDER_ID.findall(str(event.message))
    if not order_ids:
        return
    order_id = order_ids[0][1:]

    with _lock:
        if order_id in SETTINGS.processed_orders:
            return
        SETTINGS.processed_orders.append(order_id)
        SETTINGS.save()

    raw_text = SETTINGS.message_text
    if not raw_text or not raw_text.strip():
        return

    chat_id = event.message.chat_id
    chat_name = event.message.chat_name

    def worker():
        try:
            import time as _time

            order = None
            for attempt in range(3):
                try:
                    order = cardinal.account.get_order(order_id)
                    if order:
                        break
                except Exception as e:
                    logger.warning(f"Попытка {attempt + 1}/3 получения заказа #{order_id}: {e}")
                    _time.sleep(2)

            if not order:
                logger.error(f"Не удалось получить данные заказа #{order_id} после 3 попыток.")
                return

            username = chat_name or _safe_attr(order, "buyer_username", "Покупатель")
            text = process_text(raw_text, username, order_id, order)

            if not text.strip():
                return

            result = cardinal.send_message(
                chat_id,
                text,
                chat_name,
                watermark=SETTINGS.watermark
            )

            if result:
                logger.info(f"Авто-ответ отправлен для заказа #{order_id} в чат {chat_id}")
            else:
                logger.warning(f"Не удалось отправить авто-ответ для заказа #{order_id}")

        except Exception as e:
            logger.error(f"Ошибка обработки заказа #{order_id}: {e}")
            logger.debug("TRACEBACK", exc_info=True)

    threading.Thread(target=worker, daemon=True).start()


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _main_text() -> str:
    header = (
        f"⚙️ <b>Настройки авто-ответа после оплаты</b>\n\n"
        f"∟ Авто-ответ: {'🟢 Включен' if SETTINGS.enabled else '🔴 Выключен'}\n"
        f"∟ Водяной знак: {'🟢 Да' if SETTINGS.watermark else '🔴 Нет'}\n\n"
    )
    if SETTINGS.message_text and SETTINGS.message_text.strip():
        header += f"📝 Текст сообщения:\n<code>{_escape_html(SETTINGS.message_text)}</code>"
    else:
        header += "❌ Текст сообщения не установлен."
    return header


def _main_kb() -> K:
    kb = K()
    kb.add(B(
        f"{'🟢' if SETTINGS.enabled else '🔴'} Авто-ответ",
        callback_data=f"{CBT_SWITCH}:enabled"
    ))
    kb.add(B(
        f"{'🟢' if SETTINGS.watermark else '🔴'} Водяной знак",
        callback_data=f"{CBT_SWITCH}:watermark"
    ))
    kb.add(B("📝 Изменить текст", callback_data=CBT_TEXT_SHOW))
    kb.add(B("◀️ Назад", callback_data=f"{_CBT.EDIT_PLUGIN}:{UUID}:0"))
    return kb


def _variables_help_text() -> str:
    variables = {
        "$username": "никнейм покупателя",
        "$order_id": "ID заказа (без #)",
        "$order_link": "ссылка на страницу заказа",
        "$order_title": "краткое описание (название) заказа",
        "$order_desc": "полное описание заказа",
        "$order_params": "параметры лота",
        "$order_desc_or_params": "описание заказа или параметры лота",
        "$buyer": "никнейм покупателя",
        "$seller": "никнейм продавца",
        "$game": "название игры",
        "$category": "подкатегория + игра",
        "$category_full": "полное название подкатегории",
        "$price": "сумма заказа",
        "$currency": "валюта заказа (₽, $ или €)",
        "$amount": "количество товара",
        "$date": "дата (ДД.ММ.ГГГГ)",
        "$date_text": "дата (1 января)",
        "$time": "время (ЧЧ:ММ)",
        "$full_time": "время (ЧЧ:ММ:СС)",
        "$photo=XXXX": "отправить изображение с указанным ID",
        "$sleep=5": "задержка в секундах перед следующей частью",
        "{вариант1|вариант2}": "случайный выбор из вариантов",
    }
    lines = [f"<code>{k}</code> — {v}" for k, v in variables.items()]
    return "\n".join(lines)


def init_commands(cardinal: Cardinal, *args):
    if not cardinal.telegram:
        return

    tg = cardinal.telegram
    bot = tg.bot

    def open_settings(call: CallbackQuery):
        try:
            bot.edit_message_text(
                _main_text(),
                call.message.chat.id,
                call.message.id,
                reply_markup=_main_kb(),
                parse_mode="HTML"
            )
            bot.answer_callback_query(call.id)
        except Exception:
            pass

    def switch(call: CallbackQuery):
        param = call.data.split(":")[-1]
        if param == "enabled":
            SETTINGS.enabled = not SETTINGS.enabled
        elif param == "watermark":
            SETTINGS.watermark = not SETTINGS.watermark
        SETTINGS.save()
        open_settings(call)

    def show_text(call: CallbackQuery):
        kb = K()
        kb.row(
            B("◀️ Назад", callback_data=CBT_MAIN_MENU),
            B("✏️ Изменить", callback_data=CBT_TEXT_EDIT)
        )

        if SETTINGS.message_text and SETTINGS.message_text.strip():
            text = f"📝 <b>Текст сообщения:</b>\n\n<code>{_escape_html(SETTINGS.message_text)}</code>"
        else:
            text = "❌ Текст сообщения не установлен."

        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.id,
            reply_markup=kb,
            parse_mode="HTML"
        )
        bot.answer_callback_query(call.id)

    def edit_text_start(call: CallbackQuery):
        text = (
            "✏️ <b>Введите новый текст сообщения.</b>\n\n"
            "Отправьте <code>-</code> чтобы очистить текст.\n\n"
            f"📋 <b>Доступные переменные:</b>\n{_variables_help_text()}"
        )
        result = bot.send_message(
            call.message.chat.id,
            text,
            reply_markup=skb.CLEAR_STATE_BTN(),
            parse_mode="HTML"
        )
        tg.set_state(call.message.chat.id, result.id, call.from_user.id, CBT_TEXT_EDITED, {})
        bot.answer_callback_query(call.id)

    def edit_text_finish(message: Message):
        tg.clear_state(message.chat.id, message.from_user.id, True)

        if message.text and message.text.strip() == "-":
            SETTINGS.message_text = ""
        else:
            SETTINGS.message_text = message.text or ""

        SETTINGS.save()

        try:
            bot.delete_message(message.chat.id, message.id)
        except Exception:
            pass

        kb = K()
        kb.row(
            B("◀️ Назад", callback_data=CBT_MAIN_MENU),
            B("📝 Посмотреть", callback_data=CBT_TEXT_SHOW)
        )
        bot.send_message(
            message.chat.id,
            "✅ Текст сообщения успешно обновлён!",
            reply_markup=kb
        )

    def open_menu_command(m: Message):
        bot.send_message(
            m.chat.id,
            _main_text(),
            reply_markup=_main_kb(),
            parse_mode="HTML"
        )

    tg.cbq_handler(open_settings, lambda c: f"{_CBT.PLUGIN_SETTINGS}:{UUID}" in c.data)
    tg.cbq_handler(open_settings, lambda c: c.data == CBT_MAIN_MENU)
    tg.cbq_handler(switch, lambda c: c.data.startswith(f"{CBT_SWITCH}:"))
    tg.cbq_handler(show_text, lambda c: c.data == CBT_TEXT_SHOW)
    tg.cbq_handler(edit_text_start, lambda c: c.data == CBT_TEXT_EDIT)
    tg.msg_handler(edit_text_finish,
                   func=lambda m: tg.check_state(m.chat.id, m.from_user.id, CBT_TEXT_EDITED))
    tg.msg_handler(open_menu_command, commands=["auto_response_order"])
    cardinal.add_telegram_commands(UUID, [
        ("auto_response_order", "открыть настройки авто-ответа после оплаты", True)
    ])


BIND_TO_PRE_INIT = [init_commands]
BIND_TO_NEW_MESSAGE = [message_hook]
BIND_TO_DELETE = None
