from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from modules.terminal_theme import BLUE, CYAN, GRAY, GREEN, MAGENTA, RED, YELLOW, paint
from modules.utils import resolve_path


WHATSAPP_PACKAGE = "com.whatsapp"
GOPAY_PACKAGE = "com.gojek.gopay"


@dataclass
class AdbDeviceInfo:
    serial: str
    state: str
    model: str = ""
    android: str = ""
    size: str = ""
    has_whatsapp: bool = False
    has_gopay: bool = False
    current_package: str = ""


def parse_env_lines(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return parse_env_lines(path.read_text(encoding="utf-8").splitlines())


def write_env_updates(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    output: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _value = stripped.split("=", 1)
            key = key.strip()
            if key in updates:
                output.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        output.append(raw)
    missing = [key for key in updates if key not in seen]
    if missing:
        if output and output[-1].strip():
            output.append("")
        output.append("# ADB 设备绑定向导新增")
        for key in missing:
            output.append(f"{key}={updates[key]}")
    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def resolve_adb_path(env: dict[str, str]) -> Path | str:
    configured = (env.get("WHATSAPP_ADB_PATH") or env.get("GOPAY_ADB_PATH") or env.get("ADB_PATH") or "").strip()
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else resolve_path(path)
    bundled = resolve_path("tools/adb/adb.exe")
    if bundled.exists():
        return bundled
    found = shutil.which("adb")
    return found or "adb"


def run_command(command: list[str | Path], timeout: int = 12) -> tuple[int, str]:
    completed = subprocess.run(
        [str(item) for item in command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=timeout,
    )
    text = "\n".join(part for part in [completed.stdout, completed.stderr] if part).strip()
    return completed.returncode, text


def adb(adb_path: Path | str, *args: str, timeout: int = 12) -> str:
    code, text = run_command([adb_path, *args], timeout=timeout)
    if code != 0:
        raise RuntimeError(text or f"adb 返回非 0: {code}")
    return text


def parse_devices(output: str) -> list[AdbDeviceInfo]:
    devices: list[AdbDeviceInfo] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("list of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        devices.append(AdbDeviceInfo(serial=parts[0], state=parts[1]))
    return devices


def current_package(window_text: str) -> str:
    match = re.search(r"(?:mCurrentFocus|mFocusedApp)=.*?\s([a-zA-Z0-9_.]+)/(?:[a-zA-Z0-9_.$]+)", window_text)
    return match.group(1) if match else ""


def inspect_device(adb_path: Path | str, serial: str, state: str) -> AdbDeviceInfo:
    info = AdbDeviceInfo(serial=serial, state=state)
    if state != "device":
        return info
    try:
        info.model = adb(adb_path, "-s", serial, "shell", "getprop", "ro.product.model", timeout=8).strip()
    except Exception:
        pass
    try:
        info.android = adb(adb_path, "-s", serial, "shell", "getprop", "ro.build.version.release", timeout=8).strip()
    except Exception:
        pass
    try:
        size_text = adb(adb_path, "-s", serial, "shell", "wm", "size", timeout=8)
        info.size = size_text.split(":", 1)[-1].strip()
    except Exception:
        pass
    try:
        packages = adb(adb_path, "-s", serial, "shell", "pm", "list", "packages", timeout=12)
        info.has_whatsapp = f"package:{WHATSAPP_PACKAGE}" in packages
        info.has_gopay = f"package:{GOPAY_PACKAGE}" in packages
    except Exception:
        pass
    try:
        info.current_package = current_package(adb(adb_path, "-s", serial, "shell", "dumpsys", "window", timeout=12))
    except Exception:
        pass
    return info


def scan_devices(adb_path: Path | str) -> list[AdbDeviceInfo]:
    output = adb(adb_path, "devices", "-l", timeout=12)
    return [inspect_device(adb_path, item.serial, item.state) for item in parse_devices(output)]


def yes_no(value: bool) -> str:
    return paint("已安装", GREEN, bold=True) if value else paint("未安装", RED, bold=True)


def render_table(devices: list[AdbDeviceInfo]) -> None:
    headers = ["序号", "设备ID", "状态", "型号", "Android", "分辨率", "WhatsApp", "GoPay", "当前前台"]
    rows = [
        [
            str(index),
            item.serial,
            item.state,
            item.model or "-",
            item.android or "-",
            item.size or "-",
            yes_no(item.has_whatsapp),
            yes_no(item.has_gopay),
            item.current_package or "-",
        ]
        for index, item in enumerate(devices, start=1)
    ]
    widths = [len(strip_ansi(value)) for value in headers]
    for row in rows:
        for i, value in enumerate(row):
            widths[i] = max(widths[i], len(strip_ansi(value)))
    print("  ".join(paint(headers[i].ljust(widths[i]), CYAN, bold=True) for i in range(len(headers))))
    print(paint("  ".join("-" * width for width in widths), MAGENTA))
    for row in rows:
        print("  ".join(row[i].ljust(widths[i] + len(row[i]) - len(strip_ansi(row[i]))) for i in range(len(row))))


def strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", str(value))


def existing_bound_devices(env: dict[str, str]) -> set[str]:
    devices: set[str] = set()
    for key, value in env.items():
        if re.match(r"^(WHATSAPP_ADB_DEVICE|GOPAY_ADB_DEVICE)_?\d+$", key) and value.strip():
            devices.add(value.strip())
    return devices


def used_worker_indexes(env: dict[str, str]) -> set[int]:
    indexes: set[int] = set()
    for key in env:
        match = re.match(r"^(?:WHATSAPP_ADB_DEVICE|GOPAY_ADB_DEVICE)_?(\d+)$", key)
        if match:
            indexes.add(int(match.group(1)))
    return indexes


def first_free_worker(env: dict[str, str], start: int = 1) -> int:
    index = start
    while env.get(f"WHATSAPP_ADB_DEVICE_{index}") or env.get(f"GOPAY_ADB_DEVICE_{index}"):
        index += 1
    return index


def bind_device_updates(worker_id: int, device: AdbDeviceInfo) -> dict[str, str]:
    updates = {
        f"WHATSAPP_ENABLED_{worker_id}": "true",
        f"WHATSAPP_ADB_DEVICE_{worker_id}": device.serial,
        f"WHATSAPP_PACKAGE_{worker_id}": WHATSAPP_PACKAGE,
        f"GOPAY_UNLINK_ENABLED_{worker_id}": "true",
        f"GOPAY_ADB_DEVICE_{worker_id}": device.serial,
    }
    return updates


def append_phone(env: dict[str, str], updates: dict[str, str], phone: str) -> None:
    phone = re.sub(r"\D+", "", phone or "")
    if not phone:
        return
    values = [
        item.strip()
        for item in (updates.get("GOPAY_PHONES") or env.get("GOPAY_PHONES") or "").split(",")
        if item.strip()
    ]
    if phone not in values:
        values.append(phone)
    updates["GOPAY_PHONES"] = ",".join(values)


def set_worker_phone(updates: dict[str, str], worker_id: int, phone: str) -> None:
    phone = re.sub(r"\D+", "", phone or "")
    if phone:
        updates[f"GOPAY_PHONE_{worker_id}"] = phone


def prompt_phone(worker_id: int, serial: str, env: dict[str, str], updates: dict[str, str]) -> None:
    raw = input(f"worker-{worker_id} / {serial} 对应 GoPay 手机号（直接回车跳过）：").strip()
    set_worker_phone(updates, worker_id, raw)
    append_phone(env, updates, raw)


def bind_devices(env_path: Path, devices: list[AdbDeviceInfo], *, only_new: bool, overwrite: bool) -> None:
    env = read_env(env_path)
    online = [item for item in devices if item.state == "device"]
    if only_new:
        bound = existing_bound_devices(env)
        targets = [item for item in online if item.serial not in bound]
    else:
        targets = online
    if not targets:
        print(paint("没有需要绑定的新设备。", YELLOW, bold=True))
        input("按 Enter 返回。")
        return
    updates: dict[str, str] = {}
    next_worker = 1
    for position, device in enumerate(targets, start=1):
        if overwrite:
            worker_id = position
        else:
            merged_env = {**env, **updates}
            worker_id = first_free_worker(merged_env, next_worker)
            next_worker = worker_id + 1
        updates.update(bind_device_updates(worker_id, device))
        prompt_phone(worker_id, device.serial, env, updates)
    print()
    print(paint("准备写入以下绑定：", MAGENTA, bold=True))
    for key, value in updates.items():
        shown = "***" if key == "GOPAY_PHONES" else value
        print(f"  {key}={shown}")
    answer = input("确认写入 .env？[y/N]: ").strip().lower()
    if answer not in {"y", "yes", "是"}:
        print(paint("已取消写入。", YELLOW, bold=True))
        input("按 Enter 返回。")
        return
    write_env_updates(env_path, updates)
    print(paint("已写入 .env。", GREEN, bold=True))
    input("按 Enter 返回。")


def manual_bind(env_path: Path, devices: list[AdbDeviceInfo]) -> None:
    if not any(item.state == "device" for item in devices):
        print(paint("没有在线设备。", RED, bold=True))
        input("按 Enter 返回。")
        return
    raw_device = input("请输入设备序号：").strip()
    raw_worker = input("请输入绑定到 worker 几：").strip()
    try:
        device = devices[int(raw_device) - 1]
        worker_id = int(raw_worker)
    except Exception:
        print(paint("输入无效。", RED, bold=True))
        input("按 Enter 返回。")
        return
    if device.state != "device":
        print(paint("该设备不是在线 device 状态，不能绑定。", RED, bold=True))
        input("按 Enter 返回。")
        return
    env = read_env(env_path)
    updates = bind_device_updates(worker_id, device)
    prompt_phone(worker_id, device.serial, env, updates)
    write_env_updates(env_path, updates)
    print(paint(f"已绑定 {device.serial} 到 worker-{worker_id}。", GREEN, bold=True))
    input("按 Enter 返回。")


def test_device(adb_path: Path | str, devices: list[AdbDeviceInfo]) -> None:
    if not any(item.state == "device" for item in devices):
        print(paint("没有在线设备。", RED, bold=True))
        input("按 Enter 返回。")
        return
    raw = input("请输入要测试的设备序号：").strip()
    try:
        device = devices[int(raw) - 1]
    except Exception:
        print(paint("输入无效。", RED, bold=True))
        input("按 Enter 返回。")
        return
    if device.state != "device":
        print(paint("该设备不是在线 device 状态，不能测试。", RED, bold=True))
        input("按 Enter 返回。")
        return
    print(paint(f"测试设备: {device.serial}", MAGENTA, bold=True))
    for package in [WHATSAPP_PACKAGE, GOPAY_PACKAGE]:
        print(paint(f"打开 {package} ...", CYAN, bold=True))
        try:
            adb(adb_path, "-s", device.serial, "shell", "monkey", "-p", package, "1", timeout=10)
            focus = current_package(adb(adb_path, "-s", device.serial, "shell", "dumpsys", "window", timeout=10))
            ok = focus == package
            print((paint("[OK]", GREEN, bold=True) if ok else paint("[WARN]", YELLOW, bold=True)) + f" 当前前台: {focus or '-'}")
        except Exception as exc:
            print(paint("[FAIL]", RED, bold=True) + f" {package}: {exc}")
    input("按 Enter 返回。")


def adb_binding_wizard(env_path: str | Path = ".env") -> None:
    path = resolve_path(env_path)
    while True:
        env = read_env(path)
        adb_path = resolve_adb_path(env)
        print()
        print(paint("ADB 设备绑定向导", MAGENTA, bold=True))
        print(paint(f"ADB: {adb_path}", GRAY))
        try:
            devices = scan_devices(adb_path)
        except Exception as exc:
            print(paint(f"扫描失败: {exc}", RED, bold=True))
            input("按 Enter 返回。")
            return
        if devices:
            render_table(devices)
        else:
            print(paint("未检测到 ADB 在线设备。", YELLOW, bold=True))
        print()
        print(paint("1. 只绑定新设备，不覆盖已有 worker", GREEN, bold=True))
        print("2. 按当前设备顺序覆盖绑定 worker-1/2/3...")
        print("3. 手动选择设备绑定到指定 worker")
        print("4. 测试某台设备 WhatsApp / GoPay")
        print("5. 重新扫描")
        print("0. 返回设置")
        choice = input("请输入选项 [0-5]: ").strip()
        if choice == "1":
            bind_devices(path, devices, only_new=True, overwrite=False)
        elif choice == "2":
            bind_devices(path, devices, only_new=False, overwrite=True)
        elif choice == "3":
            manual_bind(path, devices)
        elif choice == "4":
            test_device(adb_path, devices)
        elif choice == "5":
            continue
        elif choice in {"0", "q", "Q"}:
            return
        else:
            print("请输入有效选项。")
