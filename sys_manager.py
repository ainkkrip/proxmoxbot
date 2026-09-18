import psutil
import asyncio
import subprocess
import sys
import json
import shlex
import html

def get_local_stats():
    """Получает статистику главной ноды (там, где запущен бот - Proxmox)"""
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    # Определяем путь к корню диска в зависимости от ОС (для совместимости при тесте на Windows)
    path = '/' if sys.platform != 'win32' else 'C:\\'
    disk = psutil.disk_usage(path)
    
    return (f"<b>CPU:</b> {cpu}%\n"
            f"<b>RAM:</b> {mem.used // (1024**2)}MB / {mem.total // (1024**2)}MB ({mem.percent}%)\n"
            f"<b>Disk:</b> {disk.used // (1024**3)}GB / {disk.total // (1024**3)}GB ({disk.percent}%)")

def format_uptime(seconds):
    if not seconds: return "0m"
    try:
        seconds = int(seconds)
    except:
        return "0m"
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    m = (seconds % 3600) // 60
    parts = []
    if d > 0: parts.append(f"{d}d")
    if h > 0: parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)

async def get_single_vm_status(vm_config):
    """Возвращает статус конкретной ВМ: (status_text, is_running)"""
    vmid = vm_config['id']
    proc = await asyncio.create_subprocess_shell(
        f"qm status {vmid} --verbose",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    out, _ = await proc.communicate()
    out = out.decode()
    
    status = "unknown"
    uptime = 0
    for line in out.splitlines():
        if line.startswith("status:"):
            status = line.split(":", 1)[1].strip()
        elif line.startswith("uptime:"):
            uptime = line.split(":", 1)[1].strip()
            
    if status == "running":
        up_str = format_uptime(uptime)
        return f"Запущена (Uptime: {up_str})", True
    elif status == "stopped":
        return "Выключена", False
    else:
        return status, False

async def get_all_vms_status(vms_config_dict):
    """Возвращает строку со статусами всех ВМ"""
    text_lines = ["\n<b>Статус виртуальных машин:</b>"]
    for vm_id, vm in vms_config_dict.items():
        status_text, _ = await get_single_vm_status(vm)
        text_lines.append(f"<b>{vm['name']}</b>: {status_text}")
    return "\n".join(text_lines)

async def get_vm_full_info(vm_config):
    """Сводная информация о конкретной ВМ: статус + метрики CPU/RAM/Диск"""
    status_text, is_running = await get_single_vm_status(vm_config)
    lines = [
        f"<b>Виртуальная машина:</b> {vm_config['name']}",
        f"<b>Статус:</b> {status_text}\n"
    ]
    if is_running:
        stats = await get_vm_stats(vm_config)
        lines.append(stats)
    else:
        lines.append("<i>ВМ выключена. Метрики недоступны.</i>")
    return "\n".join(lines)


async def run_qm_guest_exec(vmid, command):
    """Асинхронное выполнение команды внутри VM через qm guest exec"""
    try:
        # Экранируем команду для безопасной передачи в bash -c
        safe_cmd = shlex.quote(command)
        cmd = f"qm guest exec {vmid} -- bash -c {safe_cmd}"
        
        proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        
        if proc.returncode != 0:
            return f"Ошибка qm guest exec: {err.decode().strip()} {out.decode().strip()}"
        
        try:
            res = json.loads(out.decode())
        except Exception as e:
            return f"Ошибка парсинга JSON: {out.decode().strip()}"
            
        if "exited" in res:
            out_data = res.get("out-data", "")
            err_data = res.get("err-data", "")
            if out_data:
                return out_data
            elif err_data:
                return err_data
            else:
                return "[Команда ничего не вывела]"
                
        pid = res.get("pid")
        if not pid:
            return f"Не удалось получить PID команды: {out.decode().strip()}"
            
        # Polling: ждем завершения команды и получаем ее вывод
        for _ in range(30):
            await asyncio.sleep(1)
            status_cmd = f"qm guest exec-status {vmid} {pid}"
            sproc = await asyncio.create_subprocess_shell(status_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            sout, serr = await sproc.communicate()
            
            if sproc.returncode != 0:
                continue
                
            try:
                status_res = json.loads(sout.decode())
            except:
                continue
                
            if status_res.get("exited") == 1:
                out_data = status_res.get("out-data", "")
                err_data = status_res.get("err-data", "")
                if out_data:
                    return out_data
                elif err_data:
                    return err_data
                else:
                    return "[Команда ничего не вывела]"
                    
        return "Таймаут выполнения команды (30 сек)"
    except Exception as e:
        return f"Ошибка qm exec: {str(e)}"

async def get_vm_stats(vm_config):
    """Сбор статистики с Ubuntu VM через qm guest exec"""
    vmid = vm_config['id']
    script = """
    cpu=$(grep 'cpu ' /proc/stat | awk '{usage=($2+$4)*100/($2+$4+$5)} END {printf "%.1f", usage}')
    mem_used=$(free -m | awk 'NR==2{print $3}')
    mem_total=$(free -m | awk 'NR==2{print $2}')
    if [ -n "$mem_total" ] && [ "$mem_total" -gt 0 ]; then
      mem_pct=$(awk "BEGIN {printf \\"%.1f\\", ($mem_used/$mem_total)*100}")
    else
      mem_pct="0.0"
    fi
    read -r disk_used disk_total disk_pct <<< $(df -h / | awk 'NR==2{print $3, $2, $5}')
    echo "${cpu}%"
    echo "${mem_used}MB / ${mem_total}MB (${mem_pct}%)"
    echo "${disk_used} / ${disk_total} (${disk_pct})"
    """
    res = await run_qm_guest_exec(vmid, script)
    
    if "Ошибка" in res or "Таймаут" in res:
        return html.escape(res)
        
    lines = res.strip().split('\n')
    if len(lines) >= 3:
        return (f"<b>CPU:</b> {html.escape(lines[0])}\n"
                f"<b>RAM:</b> {html.escape(lines[1])}\n"
                f"<b>Disk:</b> {html.escape(lines[2])}")
    return f"Не удалось распарсить метрики:\n{html.escape(res)}"

async def reboot_node():
    subprocess.run("reboot", shell=True)

async def shutdown_node():
    if sys.platform != 'win32':
        subprocess.run("shutdown -h now", shell=True)
    else:
        subprocess.run("shutdown /s /t 0", shell=True)

async def reboot_vm(vm_config):
    vmid = vm_config['id']
    proc = await asyncio.create_subprocess_shell(f"qm reboot {vmid}", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    return out.decode().strip() if proc.returncode == 0 else err.decode().strip()

async def shutdown_vm(vm_config):
    vmid = vm_config['id']
    proc = await asyncio.create_subprocess_shell(f"qm shutdown {vmid}", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    return out.decode().strip() if proc.returncode == 0 else err.decode().strip()

def run_local_command(command):
    """Запуск локальной консольной команды на ноде"""
    try:
        res = subprocess.run(command, shell=True, capture_output=True, text=True, errors="replace")
        out = res.stdout if res.stdout else res.stderr
        return out.strip() if out else "[Команда ничего не вывела]"
    except Exception as e:
        return str(e)
