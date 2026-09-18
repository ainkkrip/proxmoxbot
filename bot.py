import asyncio
import yaml
import re
import os
import html
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession

import sys_manager
import logging

logging.basicConfig(level=logging.INFO)

# --- Загрузка конфигурации ---
CONFIG_FILE = "config.yaml"
if not os.path.exists(CONFIG_FILE):
    print(f"Файл {CONFIG_FILE} не найден. Переименуйте шаблон и настройте его.")
    exit(1)

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

ADMINS = config['bot']['admin_ids']
BOT_TOKEN = config['bot']['token']
PROXY_URL = config['bot'].get('proxy')
VMS = {str(vm['id']): vm for vm in config['vms']}

# Гостевые пользователи с персональным доступом к ВМ (Telegram ID -> VM ID)
GUESTS = {}
for guest in config['bot'].get('guest_ids', []):
    GUESTS[guest['id']] = str(guest['vm'])

if not BOT_TOKEN:
    print("Внимание: токен бота не указан в config.yaml (bot.token). Укажите токен перед запуском.")

# Инициализация бота
session = AiohttpSession(proxy=PROXY_URL) if PROXY_URL else None
bot = Bot(token=BOT_TOKEN or "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11", session=session, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

def is_admin(user_id):
    return user_id in ADMINS

def get_guest_vm_id(user_id):
    return GUESTS.get(user_id)

def get_allowed_vm(user_id, target_vm_id=None):
    """
    Проверяет права доступа пользователя к VM.
    Возвращает кортеж: (vm_dict, error_message).
    """
    if is_admin(user_id):
        if target_vm_id:
            vm = VMS.get(str(target_vm_id))
            if not vm:
                return None, f"VM '{target_vm_id}' не найдена в конфиге."
            return vm, None
        return None, "Не указан ID виртуальной машины."
        
    guest_vmid = get_guest_vm_id(user_id)
    if guest_vmid:
        if target_vm_id and str(target_vm_id) != str(guest_vmid):
            return None, f"Доступ запрещен. У вас есть доступ только к VM: {guest_vmid}"
        vm = VMS.get(str(guest_vmid))
        if not vm:
            return None, f"Назначенная VM '{guest_vmid}' не найдена в конфиге."
        return vm, None

    return None, "Доступ запрещен. Ваш ID не зарегистрирован."

async def notify_admins(text: str):
    """Отправка уведомления всем администраторам бота"""
    for admin_id in ADMINS:
        try:
            await bot.send_message(admin_id, text)
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление администратору {admin_id}: {e}")

# --- Клавиатуры ---
def get_main_keyboard():
    buttons = []
    # Кнопки для каждой VM
    for vm_id, vm in VMS.items():
        buttons.append([InlineKeyboardButton(text=f"Управление: {vm['name']}", callback_data=f"vm_{vm_id}")])
    # Кнопки для Ноды
    buttons.append([
        InlineKeyboardButton(text="Reboot Node", callback_data="node_reboot"),
        InlineKeyboardButton(text="Shutdown Node", callback_data="node_shutdown")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_vm_keyboard(vm_id):
    buttons = [
        [InlineKeyboardButton(text="Reboot VM", callback_data=f"vmreboot_{vm_id}"),
         InlineKeyboardButton(text="Shutdown VM", callback_data=f"vmshutdown_{vm_id}")],
        [InlineKeyboardButton(text="Назад к Node", callback_data="back_to_main")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_guest_vm_keyboard(vm_id):
    buttons = [
        [InlineKeyboardButton(text="Обновить статус", callback_data=f"vmrefresh_{vm_id}")],
        [InlineKeyboardButton(text="Reboot VM", callback_data=f"vmreboot_{vm_id}"),
         InlineKeyboardButton(text="Shutdown VM", callback_data=f"vmshutdown_{vm_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- Хэндлеры Главного меню ---
@dp.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id
    
    # 1. Если администратор - полный доступ
    if is_admin(user_id):
        stats = sys_manager.get_local_stats()
        vm_statuses = await sys_manager.get_all_vms_status(VMS)
        text = f"<b>Главная Нода ({config['node']['name']})</b>\n\n{stats}\n{vm_statuses}"
        return await message.answer(text, reply_markup=get_main_keyboard())
    
    # 2. Если друг (гость с доступом к конкретной ВМ)
    guest_vm_id = get_guest_vm_id(user_id)
    if guest_vm_id:
        vm = VMS.get(guest_vm_id)
        if not vm:
            return await message.answer(f"Назначенная VM '{guest_vm_id}' не найдена в конфиге.")
            
        wait_msg = await message.answer(f"<i>Получаю данные {vm['name']}...</i>")
        info = await sys_manager.get_vm_full_info(vm)
        info += (
            "\n\n💡 <b>Консоль (терминал):</b>\n"
            "Отправьте команду прямо в чат:\n"
            "<code>.console &lt;команда&gt;</code>\n"
            "<i>(например: <code>.console systemctl status nginx</code>)</i>"
        )
        return await wait_msg.edit_text(info, reply_markup=get_guest_vm_keyboard(guest_vm_id))
    
    # 3. Неизвестный пользователь
    await message.answer("Доступ запрещен. Ваш ID не зарегистрирован в системе.")

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Доступ запрещен.", show_alert=True)
    stats = sys_manager.get_local_stats()
    vm_statuses = await sys_manager.get_all_vms_status(VMS)
    text = f"<b>Главная Нода ({config['node']['name']})</b>\n\n{stats}\n{vm_statuses}"
    await callback.message.edit_text(text, reply_markup=get_main_keyboard())

# --- Хэндлеры просмотра и обновления VM ---
@dp.callback_query(F.data.startswith("vm_") | F.data.startswith("vmrefresh_"))
async def show_or_refresh_vm(callback: CallbackQuery):
    user_id = callback.from_user.id
    action = "vmrefresh_" if callback.data.startswith("vmrefresh_") else "vm_"
    vm_id = callback.data.replace(action, "")
    
    vm, err = get_allowed_vm(user_id, vm_id)
    if err:
        return await callback.answer(err, show_alert=True)
        
    await callback.answer("Обновление данных...")
    info = await sys_manager.get_vm_full_info(vm)
    
    if is_admin(user_id):
        kb = get_vm_keyboard(vm_id)
    else:
        info += (
            "\n\n <b>Консоль (терминал):</b>\n"
            "Отправьте команду прямо в чат:\n"
            "<code>.console &lt;команда&gt;</code>\n"
            "<i>(например: <code>.console systemctl status nginx</code>)</i>"
        )
        kb = get_guest_vm_keyboard(vm_id)
        
    await callback.message.edit_text(info, reply_markup=kb)

# --- Действия: Reboot / Shutdown Ноды ---
@dp.callback_query(F.data.startswith("node_"))
async def node_actions(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Доступ запрещен.", show_alert=True)
        
    action = callback.data
    if action == "node_reboot":
        await callback.message.answer("Выполняется перезагрузка главной ноды...")
        await sys_manager.reboot_node()
    elif action == "node_shutdown":
        await callback.message.answer("Выполняется выключение главной ноды...")
        await sys_manager.shutdown_node()
    await callback.answer()

# --- Действия: Reboot / Shutdown VM ---
@dp.callback_query(F.data.startswith("vmreboot_") | F.data.startswith("vmshutdown_"))
async def vm_actions(callback: CallbackQuery):
    user_id = callback.from_user.id
    if callback.data.startswith("vmreboot_"):
        vm_id = callback.data.replace("vmreboot_", "")
        action = "reboot"
    else:
        vm_id = callback.data.replace("vmshutdown_", "")
        action = "shutdown"
        
    vm, err = get_allowed_vm(user_id, vm_id)
    if err:
        return await callback.answer(err, show_alert=True)
        
    if action == "reboot":
        await callback.message.answer(f"Выполняется перезагрузка {vm['name']}...")
        res = await sys_manager.reboot_vm(vm)
        if res: await callback.message.answer(f"Результат: {res}")
    else:
        await callback.message.answer(f"Выключение {vm['name']}...")
        res = await sys_manager.shutdown_vm(vm)
        if res: await callback.message.answer(f"Результат: {res}")
    await callback.answer()

    # Уведомление для администратора, если действие совершил гость
    if not is_admin(user_id):
        user_info = callback.from_user.full_name
        if callback.from_user.username:
            user_info += f" (@{callback.from_user.username})"
        user_info += f" [ID: <code>{user_id}</code>]"
        action_name = "Перезагрузка" if action == "reboot" else "Выключение"
        notify_text = (
            f"<b>Действие</b>\n"
            f"<b>Пользователь:</b> {user_info}\n"
            f"<b>VM:</b> {vm['name']} (<code>{vm['id']}</code>)\n"
            f"<b>Действие:</b> {action_name} VM\n"
            f"<b>Результат:</b> {html.escape(res or 'Команда отправлена')}"
        )
        await notify_admins(notify_text)

# --- Консоль (.console) ---
@dp.message(F.text.startswith(".console"))
async def console_command(message: Message):
    user_id = message.from_user.id
    is_adm = is_admin(user_id)
    guest_vm_id = get_guest_vm_id(user_id)
    
    if not is_adm and not guest_vm_id:
        return await message.answer("Доступ запрещен.")

    text = message.text
    match = re.match(r'^\.console(?:\s+(?:[-—–]+vm\s+(\w+)\s+)?(.+)|$)', text, re.DOTALL)
    if not match or not match.group(2) or not match.group(2).strip():
        if is_adm:
            return await message.answer(
                "Использование:\n"
                "<code>.console &lt;command&gt;</code> — на Ноде\n"
                "<code>.console --vm &lt;vm_id&gt; &lt;command&gt;</code> — на конкретной ВМ"
            )
        else:
            return await message.answer(
                "Использование:\n"
                "<code>.console &lt;command&gt;</code> — выполнение на вашей ВМ"
            )
    
    specified_vm_id = match.group(1)
    command = match.group(2).strip()
    
    if is_adm:
        if specified_vm_id:
            vm = VMS.get(specified_vm_id)
            if not vm:
                ids = ", ".join(VMS.keys())
                return await message.answer(f"VM '{specified_vm_id}' не найдена. Доступные ID: {ids}")
            wait_msg = await message.answer(f"[QEMU] Выполняю на <b>{vm['name']}</b>:\n<code>{html.escape(command)}</code>")
            result = await sys_manager.run_qm_guest_exec(vm['id'], command)
        else:
            wait_msg = await message.answer(f"[Local] Выполняю на <b>Главной Ноде</b>:\n<code>{html.escape(command)}</code>")
            result = sys_manager.run_local_command(command)
    else:
        # Для друга: команда ВСЕГДА направляется на его разрешенную ВМ
        if specified_vm_id and str(specified_vm_id) != str(guest_vm_id):
            return await message.answer(f"Доступ запрещен. У вас есть доступ только к VM: <code>{guest_vm_id}</code>")
            
        vm = VMS.get(guest_vm_id)
        if not vm:
            return await message.answer(f"Назначенная VM '{guest_vm_id}' не найдена в конфигурации.")
            
        wait_msg = await message.answer(f"[QEMU] Выполняю на <b>{vm['name']}</b>:\n<code>{html.escape(command)}</code>")
        result = await sys_manager.run_qm_guest_exec(vm['id'], command)
        
    if not result:
        result = "[Успешно, вывода нет]"
        
    # Telegram разрешает до 4096 символов. Режем, если слишком длинный вывод.
    if len(result) > 4000:
        result = result[:3900] + "\n...[Слишком длинный вывод, обрезан]"
        
    await wait_msg.edit_text(f"<pre>{html.escape(result)}</pre>")

    # Если команду выполнял гость — отправляем уведомление главному администратору
    if not is_adm:
        user_info = message.from_user.full_name
        if message.from_user.username:
            user_info += f" (@{message.from_user.username})"
        user_info += f" [ID: <code>{user_id}</code>]"
        
        preview_res = result if len(result) <= 1500 else result[:1500] + "\n...[Обрезано для уведомления]"
        notify_text = (
            f"<b>Консоль:выполнена команда</b>\n"
            f"<b>Пользователь:</b> {user_info}\n"
            f"<b>VM:</b> {vm['name']} (<code>{vm['id']}</code>)\n"
            f"<b>Команда:</b>\n<code>{html.escape(command)}</code>\n\n"
            f"<b>Результат:</b>\n<pre>{html.escape(preview_res)}</pre>"
        )
        await notify_admins(notify_text)

async def main():
    print("Бот успешно запущен. Ожидание команд...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
