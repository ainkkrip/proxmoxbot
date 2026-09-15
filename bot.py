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

# Инициализация бота
session = AiohttpSession(proxy=PROXY_URL) if PROXY_URL else None
bot = Bot(token=BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

def is_admin(user_id):
    return user_id in ADMINS

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

# --- Хэндлеры Главного меню ---
@dp.message(CommandStart())
async def cmd_start(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("Доступ запрещен. Ваш ID не в списке администраторов.")
    
    stats = sys_manager.get_local_stats()
    vm_statuses = await sys_manager.get_all_vms_status(VMS)
    text = f"<b>Главная Нода ({config['node']['name']})</b>\n\n{stats}\n{vm_statuses}"
    await message.answer(text, reply_markup=get_main_keyboard())

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    stats = sys_manager.get_local_stats()
    vm_statuses = await sys_manager.get_all_vms_status(VMS)
    text = f"<b>Главная Нода ({config['node']['name']})</b>\n\n{stats}\n{vm_statuses}"
    await callback.message.edit_text(text, reply_markup=get_main_keyboard())

# --- Хэндлеры управления VM ---
@dp.callback_query(F.data.startswith("vm_"))
async def show_vm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
        
    vm_id = callback.data.replace("vm_", "")
    vm = VMS.get(vm_id)
    if not vm:
        return await callback.answer("VM не найдена в конфиге", show_alert=True)
    
    await callback.message.edit_text(f"<i>Получаю данные {vm['name']}...</i>")
    stats = await sys_manager.get_vm_stats(vm)
    text = f"<b>Виртуальная машина: {vm['name']}</b>\n\n{stats}"
    await callback.message.edit_text(text, reply_markup=get_vm_keyboard(vm_id))

# --- Действия: Reboot / Shutdown ---
@dp.callback_query(F.data.startswith("node_"))
async def node_actions(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
        
    action = callback.data
    if action == "node_reboot":
        await callback.message.answer("Выполняется перезагрузка главной ноды...")
        await sys_manager.reboot_node()
    elif action == "node_shutdown":
        await callback.message.answer(f"Выключение {vm['name']}...")
        await sys_manager.shutdown_node()
    await callback.answer()

@dp.callback_query(F.data.startswith("vmreboot_") | F.data.startswith("vmshutdown_"))
async def vm_actions(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    
    if callback.data.startswith("vmreboot_"):
        vm_id = callback.data.replace("vmreboot_", "")
        action = "reboot"
    else:
        vm_id = callback.data.replace("vmshutdown_", "")
        action = "shutdown"
        
    vm = VMS.get(vm_id)
    if not vm:
        return await callback.answer("VM не найдена", show_alert=True)
        
    if action == "reboot":
        await callback.message.answer(f"Выполняется перезагрузка {vm['name']}...")
        res = await sys_manager.reboot_vm(vm)
        if res: await callback.message.answer(f"Результат: {res}")
    else:
        await callback.message.answer(f"Выключение {vm['name']}...")
        res = await sys_manager.shutdown_vm(vm)
        if res: await callback.message.answer(f"Результат: {res}")
    await callback.answer()

# --- Консоль (.console) ---
@dp.message(F.text.startswith(".console"))
async def console_command(message: Message):
    if not is_admin(message.from_user.id):
        return

    text = message.text
    # Регулярка для извлечения флага --vm (если есть) и самой команды, с учетом тире
    match = re.match(r'^\.console\s+(?:[-—–]+vm\s+(\w+)\s+)?(.+)$', text, re.DOTALL)
    if not match:
        return await message.answer("Использование:\n<code>.console &lt;command&gt;</code>\n<code>.console --vm &lt;vm_id&gt; &lt;command&gt;</code>")
    
    vm_id = match.group(1)
    command = match.group(2)
    
    if vm_id:
        vm = VMS.get(vm_id)
        if not vm:
            ids = ", ".join(VMS.keys())
            return await message.answer(f"VM '{vm_id}' не найдена. Доступные ID: {ids}")
            
        wait_msg = await message.answer(f"[QEMU] Выполняю на <b>{vm['name']}</b>:\n<code>{command}</code>")
        result = await sys_manager.run_qm_guest_exec(vm['id'], command)
    else:
        wait_msg = await message.answer(f"[Local] Выполняю на <b>Главной Ноде</b>:\n<code>{command}</code>")
        result = sys_manager.run_local_command(command)
        
    if not result:
        result = "[Успешно, вывода нет]"
        
    # Telegram разрешает до 4096 символов. Режем, если слишком длинный вывод.
    if len(result) > 4000:
        result = result[:3900] + "\n...[Слишком длинный вывод, обрезан]"
        
    await wait_msg.edit_text(f"<pre>{html.escape(result)}</pre>")

async def main():
    print("Бот успешно запущен. Ожидание команд...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
