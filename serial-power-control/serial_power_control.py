"""
Serial Power Control Tool (Standalone)

通过MCU串口对板子进行上下电控制。
支持绿幕板(VLMU)、XCCP-A/B、EVB、FPU等板型。
可自动通过ADB获取板型号，也可手动选择。

依赖: pyserial (pip install pyserial)

Usage:
    python serial_power_control.py                    # 交互式模式
    python serial_power_control.py --port COM58 on    # 直接上电
    python serial_power_control.py --port COM58 off   # 直接下电
    python serial_power_control.py --port COM58 --board vlmu on  # 指定板型上电

Exit Codes:
    0 - Success
    1 - Power control failed
    2 - Invalid arguments / missing dependencies
"""

__version__ = "1.0.0"

import argparse
import json
import logging
import os
import platform
import subprocess
import sys
import time
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, List, Callable

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("ERROR: pyserial is required. Install with: pip install pyserial")
    sys.exit(2)


# ============================================================
# Logging Setup
# ============================================================

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


def setup_logging(log_to_file: bool = True, verbose: bool = False) -> logging.Logger:
    """配置日志"""
    log = logging.getLogger("serial_power")
    log.setLevel(logging.DEBUG if verbose else logging.INFO)

    # 避免重复 handler
    if log.handlers:
        return log

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_fmt = logging.Formatter("  [%(levelname)s] %(message)s")
    console_handler.setFormatter(console_fmt)
    log.addHandler(console_handler)

    # File handler
    if log_to_file:
        os.makedirs(LOG_DIR, exist_ok=True)
        log_file = os.path.join(
            LOG_DIR,
            f"power_control_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        )
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_fmt = logging.Formatter(
            "[%(asctime)s] %(levelname)-8s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        file_handler.setFormatter(file_fmt)
        log.addHandler(file_handler)
        log.info(f"Log file: {log_file}")

    log.propagate = False
    return log


log = setup_logging(log_to_file=False)  # 延迟到 main() 时再配置文件日志


# ============================================================
# Board Configurations
# ============================================================

@dataclass
class BoardConfig:
    """板型电源控制配置"""
    name: str
    poweroff_cmd: str
    poweron_cmd: str
    poweroff_success_flag: str
    poweroff_already_flag: str
    poweron_success_flag: str
    poweron_already_flag: str
    login_prompt: str
    shell_prompt: str
    password_prompt: str = "Password"
    username: str = "xiaopeng"
    password: str = "xiaopeng@123.com"
    power_timeout: int = 30


# 各板型配置
BOARD_CONFIGS = {
    "vlmu": BoardConfig(
        name="VLMU (绿幕板)",
        poweroff_cmd="poweroff force",
        poweron_cmd="poweron",
        poweroff_success_flag="PWRDOWN_SEQ_COMPLETE       is done",
        poweroff_already_flag="has been powered off",
        poweron_success_flag="PWRUP_SEQ_COMPLETE         is done",
        poweron_already_flag="has been powered on",
        login_prompt="XpShell_Login>",
        shell_prompt="XpShell>",
    ),
    "xccp-a": BoardConfig(
        name="XCCP-A",
        poweroff_cmd="poweroff force",
        poweron_cmd="poweron",
        poweroff_success_flag="PWRDOWN_SEQ_COMPLETE is done !",
        poweroff_already_flag="has been powered off",
        poweron_success_flag="PWRUP_SEQ_COMPLETE",
        poweron_already_flag="has been powered on",
        login_prompt="XpShell_Login>",
        shell_prompt="XpShell>",
    ),
    "xccp-b": BoardConfig(
        name="XCCP-B",
        poweroff_cmd="poweroffb force",
        poweron_cmd="poweronb",
        poweroff_success_flag="PWRDOWN_SEQ_COMPLETE is done !",
        poweroff_already_flag="has been powered off",
        poweron_success_flag="PWRUP_SEQ_COMPLETE",
        poweron_already_flag="has been powered on",
        login_prompt="XpShell_Login>",
        shell_prompt="XpShell>",
    ),
    "evb": BoardConfig(
        name="EVB",
        poweroff_cmd="pm power sys off",
        poweron_cmd="pm power sys on",
        poweroff_success_flag="power down system end",
        poweroff_already_flag="power is already off",
        poweron_success_flag="power up system end",
        poweron_already_flag="power is already on",
        login_prompt="uart_Login$",
        shell_prompt="uart:~$",
    ),
    "fpu": BoardConfig(
        name="FPU",
        poweroff_cmd="poweroff force",
        poweron_cmd="poweron",
        poweroff_success_flag="PWRDOWN_SEQ_COMPLETE is done !",
        poweroff_already_flag="has been powered off",
        poweron_success_flag="PWRUP_SEQ_COMPLETE",
        poweron_already_flag="has been powered on",
        login_prompt="XpShell_Login>",
        shell_prompt="XpShell>",
    ),
}

# kernel_dts -> board key 映射
KERNEL_DTS_MAP = {
    "xp5_evb": "evb",
    "xp5_xccp-a": "xccp-a",
    "xp5_xccp-b": "xccp-b",
    "xp5_xccp-vlm": "vlmu",
    "xp5_xccp-fpu": "fpu",
}


# ============================================================
# ADB Board Detection
# ============================================================

def find_adb_tool() -> Optional[str]:
    """查找ADB工具路径"""
    # 1. 检查项目 tools 目录
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)

    if sys.platform == 'win32':
        tool_name = 'adb.exe'
        tool_subdir = 'win'
    else:
        tool_name = 'adb'
        tool_subdir = 'linux'

    project_adb = os.path.join(project_root, 'tools', tool_subdir, tool_name)
    if os.path.exists(project_adb):
        return project_adb

    # 2. 检查 PATH
    import shutil
    adb_in_path = shutil.which('adb')
    if adb_in_path:
        return adb_in_path

    return None


def get_adb_devices(adb_path: str) -> List[str]:
    """获取ADB设备列表"""
    try:
        kwargs = {}
        if sys.platform == 'win32':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            kwargs['startupinfo'] = startupinfo
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW

        result = subprocess.run(
            [adb_path, 'devices'],
            capture_output=True, text=True, timeout=10, **kwargs
        )
        devices = []
        for line in result.stdout.strip().split('\n'):
            line = line.strip()
            if line and 'List of devices' not in line:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == 'device':
                    devices.append(parts[0])
        return devices
    except Exception as e:
        log.warning(f"Failed to get ADB devices: {e}")
        return []


def detect_board_type_via_adb(adb_path: str, device_id: str) -> Optional[str]:
    """通过ADB读取kernel_dts来检测板型"""
    try:
        kwargs = {}
        if sys.platform == 'win32':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            kwargs['startupinfo'] = startupinfo
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW

        result = subprocess.run(
            [adb_path, '-s', device_id, 'shell',
             'cat /proc/device-tree/board_config/xp_kernel_dts'],
            capture_output=True, text=True, timeout=10, **kwargs
        )
        kernel_dts = result.stdout.strip().strip('\x00').strip()
        if not kernel_dts:
            return None

        # 精确匹配
        if kernel_dts in KERNEL_DTS_MAP:
            return KERNEL_DTS_MAP[kernel_dts]

        # 前缀匹配 (e.g. xp5_evb_xxx)
        for prefix, board_key in KERNEL_DTS_MAP.items():
            if kernel_dts.startswith(prefix):
                return board_key

        log.warning(f"Unknown kernel_dts: {kernel_dts}")
        return None
    except Exception as e:
        log.warning(f"Failed to detect board type for {device_id}: {e}")
        return None


def auto_detect_board() -> Optional[str]:
    """自动检测板型"""
    adb_path = find_adb_tool()
    if not adb_path:
        log.info("ADB tool not found, skipping auto-detection")
        return None

    log.info(f"Using ADB: {adb_path}")
    devices = get_adb_devices(adb_path)
    if not devices:
        log.info("No ADB devices found")
        return None

    log.info(f"Found {len(devices)} ADB device(s): {', '.join(devices)}")

    # 尝试检测第一个设备
    for device_id in devices:
        board_key = detect_board_type_via_adb(adb_path, device_id)
        if board_key:
            log.info(f"Detected board: {BOARD_CONFIGS[board_key].name} (device: {device_id})")
            return board_key

    return None


# ============================================================
# Serial MCU Controller
# ============================================================

class MCUSerialController:
    """MCU串口控制器，负责串口通信、登录、电源控制"""

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 1,
                 on_output: Callable[[str], None] = None):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser: Optional[serial.Serial] = None
        self._lock = threading.Lock()
        self._on_output = on_output  # 外部回调（用于集成到其他系统）

    def _log(self, msg: str, level: str = "info"):
        """统一日志输出"""
        getattr(log, level)(msg)
        if self._on_output:
            self._on_output(f"[{level.upper()}] {msg}")

    def connect(self) -> bool:
        """连接串口"""
        try:
            if self.ser and self.ser.is_open:
                self._log(f"Serial port {self.port} already open")
                return True

            self.ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=self.timeout,
                write_timeout=5,
            )
            if self.ser.is_open:
                self._log(f"Connected to {self.port} @ {self.baudrate}")
                return True
            else:
                self._log(f"Failed to open {self.port}", "error")
                return False
        except serial.SerialException as e:
            self._log(f"Cannot connect to {self.port}: {e}", "error")
            return False

    def disconnect(self):
        """断开串口"""
        if self.ser and self.ser.is_open:
            self.ser.close()
            self._log(f"Disconnected from {self.port}")

    def clear_buffer(self):
        """清空缓冲区"""
        if self.ser and self.ser.is_open:
            with self._lock:
                self.ser.reset_input_buffer()
                self.ser.reset_output_buffer()

    def write(self, data: str, add_newline: bool = True):
        """写入数据"""
        if not self.ser or not self.ser.is_open:
            raise RuntimeError("Serial port not connected")
        with self._lock:
            if add_newline and not data.endswith('\n'):
                data += '\n'
            self.ser.write(data.encode('utf-8'))
            self.ser.flush()

    def read_all(self) -> str:
        """读取所有可用数据"""
        if not self.ser or not self.ser.is_open:
            return ""
        with self._lock:
            size = self.ser.in_waiting
            if size == 0:
                return ""
            data = self.ser.read(size)
            return data.decode('utf-8', errors='ignore')

    def in_waiting(self) -> int:
        """缓冲区等待字节数"""
        if not self.ser or not self.ser.is_open:
            return 0
        with self._lock:
            return self.ser.in_waiting

    def login(self, config: BoardConfig) -> bool:
        """
        MCU串口登录流程

        Returns:
            True if login successful, False otherwise
        """
        self._log("Starting MCU login...")

        for attempt in range(3):
            self.clear_buffer()

            # 发送回车唤醒
            for _ in range(3):
                self.write("\n", add_newline=False)
                time.sleep(0.5)

            if self.in_waiting() > 0:
                data = self.read_all()
                log.debug(f"Login response: {data[:200].strip()}")
                # 如果已在shell中，无需登录
                if config.shell_prompt in data:
                    self._log("Already logged in (shell prompt detected)")
                    return True
                # 如果看到登录提示
                if config.login_prompt in data:
                    # 发送用户名
                    self._log("Sending username...")
                    self.write(config.username)
                    time.sleep(0.5)

                    # 等待密码提示
                    for _ in range(4):
                        data = self.read_all()
                        if data and config.password_prompt in data:
                            break
                        time.sleep(0.5)
                    else:
                        self._log(f"Password prompt not received (attempt {attempt + 1})", "warning")
                        continue

                    # 发送密码
                    self.clear_buffer()
                    self._log("Sending password...")
                    self.write(config.password)
                    time.sleep(0.5)

                    # 验证登录成功
                    for _ in range(4):
                        data = self.read_all()
                        if data and config.shell_prompt in data:
                            self._log("Login successful")
                            return True
                        time.sleep(0.5)

                    self._log(f"Login failed (attempt {attempt + 1})", "warning")
                else:
                    self._log(f"Unexpected response: {data[:100].strip()}", "warning")
            else:
                self._log(f"No response from serial port (attempt {attempt + 1})", "warning")

        self._log("Login failed after 3 attempts", "error")
        return False

    def execute_power_command(self, command: str, success_flag: str,
                              already_flag: str, timeout: int = 30) -> bool:
        """
        执行电源控制命令并检测结果

        Args:
            command: 电源命令
            success_flag: 成功标识
            already_flag: 已经处于目标状态的标识
            timeout: 超时时间(秒)

        Returns:
            True if command successful, False otherwise
        """
        self.clear_buffer()
        self._log(f"Sending command: {command}")
        self.write(command)

        full_output = ""
        end_time = time.time() + timeout

        while time.time() < end_time:
            if self.in_waiting() > 0:
                data = self.read_all()
                if data:
                    full_output += data
                    # 实时输出（去掉首尾空白，按行显示）
                    for line in data.splitlines():
                        line = line.strip()
                        if line:
                            log.debug(f"[MCU] {line}")

                    # 检查成功标识
                    if success_flag in full_output:
                        return True
                    if already_flag in full_output:
                        return True
            else:
                time.sleep(0.1)

        self._log(f"Command timeout ({timeout}s)", "warning")
        log.debug(f"Output so far: {full_output[:500]}")
        return False

    def power_off(self, config: BoardConfig) -> bool:
        """执行下电"""
        self._log(f"=== POWER OFF ({config.name}) ===")

        result = self.execute_power_command(
            command=config.poweroff_cmd,
            success_flag=config.poweroff_success_flag,
            already_flag=config.poweroff_already_flag,
            timeout=config.power_timeout,
        )

        if result:
            self._log("Power OFF successful!")
        else:
            self._log("Power OFF failed!", "error")
        return result

    def power_on(self, config: BoardConfig) -> bool:
        """执行上电"""
        self._log(f"=== POWER ON ({config.name}) ===")

        result = self.execute_power_command(
            command=config.poweron_cmd,
            success_flag=config.poweron_success_flag,
            already_flag=config.poweron_already_flag,
            timeout=config.power_timeout,
        )

        if result:
            self._log("Power ON successful!")
        else:
            self._log("Power ON failed!", "error")
        return result


# ============================================================
# Interactive Helpers
# ============================================================

def list_serial_ports():
    """列出所有可用串口"""
    ports = serial.tools.list_ports.comports()
    if not ports:
        print("  No serial ports found!")
        return []

    print("\n  Available serial ports:")
    print(f"  {'No.':<5} {'Port':<12} {'Description'}")
    print(f"  {'-'*5} {'-'*12} {'-'*40}")
    for i, port in enumerate(ports, 1):
        print(f"  {i:<5} {port.device:<12} {port.description}")
    return ports


def select_serial_port() -> Optional[str]:
    """交互式选择串口"""
    ports = list_serial_ports()
    if not ports:
        return None

    while True:
        choice = input("\n  Enter port number or port name (e.g. 1 or COM58): ").strip()
        if not choice:
            return None

        # 数字选择
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(ports):
                return ports[idx].device
            else:
                print(f"  Invalid number. Please enter 1-{len(ports)}")
                continue
        except ValueError:
            pass

        # 直接输入端口名
        port_name = choice.upper() if sys.platform == 'win32' else choice
        # 验证端口是否存在
        available = [p.device for p in ports]
        if port_name in available:
            return port_name
        # 宽松匹配 (允许输入 com58 匹配 COM58)
        for p in available:
            if p.upper() == port_name.upper():
                return p
        print(f"  Port '{choice}' not found in available ports. Enter anyway? (y/n): ", end="")
        if input().strip().lower() == 'y':
            return choice


def select_board_type() -> Optional[str]:
    """交互式选择板型"""
    print("\n  Auto-detecting board type via ADB...")
    detected = auto_detect_board()

    print(f"\n  Available board types:")
    keys = list(BOARD_CONFIGS.keys())
    for i, key in enumerate(keys, 1):
        cfg = BOARD_CONFIGS[key]
        marker = " <-- auto-detected" if key == detected else ""
        print(f"  {i}. {cfg.name}{marker}")

    default_hint = ""
    if detected:
        default_idx = keys.index(detected) + 1
        default_hint = f" [default: {default_idx}]"

    while True:
        choice = input(f"\n  Select board type{default_hint}: ").strip()

        # 默认值
        if not choice and detected:
            return detected

        # 数字选择
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(keys):
                return keys[idx]
            else:
                print(f"  Invalid. Please enter 1-{len(keys)}")
        except ValueError:
            # 名称匹配
            choice_lower = choice.lower()
            if choice_lower in BOARD_CONFIGS:
                return choice_lower
            print(f"  Unknown board type: {choice}")


def select_action() -> Optional[str]:
    """交互式选择操作"""
    print("\n  Actions:")
    print("  1. Power ON  (上电)")
    print("  2. Power OFF (下电)")
    print("  3. Reboot    (重启: 下电 -> 等待 -> 上电)")
    print("  0. Exit")

    while True:
        choice = input("\n  Select action: ").strip()
        if choice == '1':
            return 'on'
        elif choice == '2':
            return 'off'
        elif choice == '3':
            return 'reboot'
        elif choice == '0' or choice.lower() in ('q', 'quit', 'exit'):
            return None
        else:
            print("  Invalid choice. Please enter 0-3")


# ============================================================
# Main Logic
# ============================================================

@dataclass
class PowerResult:
    """电源操作结果"""
    success: bool
    action: str
    board: str
    port: str
    message: str
    duration: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    def __str__(self):
        status = "SUCCESS" if self.success else "FAILED"
        return f"[{status}] {self.action} on {self.board} via {self.port} - {self.message} ({self.duration:.1f}s)"


def run_power_action(port: str, baudrate: int, board_key: str, action: str,
                     on_output: Callable[[str], None] = None) -> PowerResult:
    """
    执行电源控制操作

    Args:
        port: 串口号
        baudrate: 波特率
        board_key: 板型key
        action: 'on', 'off', 'reboot'
        on_output: 可选回调函数，用于实时输出日志（集成到其他系统时使用）

    Returns:
        PowerResult 操作结果对象
    """
    start_time = time.time()
    config = BOARD_CONFIGS.get(board_key)
    if not config:
        return PowerResult(False, action, board_key, port, f"Unknown board type: {board_key}")

    log.info(f"Port: {port} | Baudrate: {baudrate} | Board: {config.name} | Action: {action}")

    ctrl = MCUSerialController(port=port, baudrate=baudrate, on_output=on_output)

    try:
        if not ctrl.connect():
            return PowerResult(False, action, config.name, port, "Failed to connect serial port")

        # 登录
        if not ctrl.login(config):
            return PowerResult(False, action, config.name, port, "MCU login failed")

        # 执行操作
        if action == 'off':
            ok = ctrl.power_off(config)
            msg = "Power OFF successful" if ok else "Power OFF failed"
        elif action == 'on':
            ok = ctrl.power_on(config)
            msg = "Power ON successful" if ok else "Power ON failed"
        elif action == 'reboot':
            off_ok = ctrl.power_off(config)
            if not off_ok:
                return PowerResult(False, action, config.name, port,
                                   "Reboot aborted: power off failed",
                                   time.time() - start_time)

            wait_sec = 5
            log.info(f"Waiting {wait_sec}s before power on...")
            time.sleep(wait_sec)

            on_ok = ctrl.power_on(config)
            ok = on_ok
            msg = "Reboot successful" if ok else "Reboot failed: power on failed"
        else:
            return PowerResult(False, action, config.name, port, f"Unknown action: {action}")

        duration = time.time() - start_time
        return PowerResult(ok, action, config.name, port, msg, duration)
    except Exception as e:
        duration = time.time() - start_time
        log.error(f"Unexpected error: {e}")
        return PowerResult(False, action, config.name, port, f"Error: {e}", duration)
    finally:
        ctrl.disconnect()


def interactive_mode():
    """交互式模式"""
    print("=" * 55)
    print("  Serial Power Control Tool")
    print("  MCU串口上下电控制工具")
    print("=" * 55)

    # 选择串口
    port = select_serial_port()
    if not port:
        print("  No port selected, exiting.")
        return

    # 选择波特率
    baud_input = input(f"\n  Enter baudrate [default: 115200]: ").strip()
    baudrate = int(baud_input) if baud_input else 115200

    # 选择板型
    board_key = select_board_type()
    if not board_key:
        print("  No board type selected, exiting.")
        return

    # 循环操作
    while True:
        action = select_action()
        if not action:
            print("\n  Bye!")
            break

        result = run_power_action(port, baudrate, board_key, action)
        print(f"\n  Result: {result}")
        print(f"  {'='*50}")


def main():
    parser = argparse.ArgumentParser(
        description="Serial Power Control Tool - MCU串口上下电控制",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                    Interactive mode
  %(prog)s --port COM58 on                    Power on via COM58
  %(prog)s --port COM58 off                   Power off via COM58
  %(prog)s --port COM58 reboot                Reboot via COM58
  %(prog)s --port COM58 --board vlmu on       Specify board type
  %(prog)s --port COM58 --baud 9600 off       Custom baudrate
  %(prog)s --list                             List serial ports
        """
    )
    parser.add_argument('action', nargs='?', choices=['on', 'off', 'reboot'],
                        help='Power action: on, off, reboot')
    parser.add_argument('--port', '-p', type=str, help='Serial port (e.g. COM58, /dev/ttyUSB0)')
    parser.add_argument('--baud', '-b', type=int, default=115200, help='Baudrate (default: 115200)')
    parser.add_argument('--board', type=str, choices=list(BOARD_CONFIGS.keys()),
                        help='Board type (default: auto-detect or vlmu)')
    parser.add_argument('--list', '-l', action='store_true', help='List available serial ports')

    parser.add_argument('--verbose', '-v', action='store_true', help='Enable verbose/debug output')
    parser.add_argument('--no-log', action='store_true', help='Disable file logging')
    parser.add_argument('--version', '-V', action='version', version=f'%(prog)s {__version__}')

    args = parser.parse_args()

    # 重新配置日志（启用文件日志）
    global log
    log = setup_logging(log_to_file=not args.no_log, verbose=args.verbose)

    # 列出串口
    if args.list:
        list_serial_ports()
        return

    # 交互模式
    if not args.action:
        interactive_mode()
        return

    # 命令行模式
    if not args.port:
        log.error("--port is required for command-line mode")
        parser.print_help()
        sys.exit(2)

    # 板型：命令行指定 > 自动检测 > 默认vlmu
    board_key = args.board
    if not board_key:
        log.info("Auto-detecting board type...")
        board_key = auto_detect_board()
        if not board_key:
            board_key = 'vlmu'
            log.info(f"Using default board type: {BOARD_CONFIGS[board_key].name}")

    result = run_power_action(args.port, args.baud, board_key, args.action)
    log.info(str(result))
    sys.exit(0 if result.success else 1)


if __name__ == '__main__':
    main()
