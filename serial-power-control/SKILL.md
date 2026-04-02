---
name: serial-power-control
description: MCU串口上下电控制工具，通过串口与MCU通信，实现对板子的电源控制（上电/下电/重启）。支持多板型、ADB自动检测、交互式和命令行模式。
---

## 功能特性

- 多板型支持：VLMU (绿幕板)、XCCP-A、XCCP-B、EVB、FPU
- ADB自动检测：通过ADB读取kernel_dts自动识别板型
- MCU串口自动登录
- 成功/失败判断
- 交互式 & 命令行模式
- 日志记录

## 使用方法

### 命令行模式

```bash
# 上电
python script/serial_power_control.py --port /dev/ttyUSB0 on

# 下电
python script/serial_power_control.py --port /dev/ttyUSB0 off

# 重启
python script/serial_power_control.py --port /dev/ttyUSB0 reboot

# 指定板型
python script/serial_power_control.py --port /dev/ttyUSB0 --board vlmu on

# 列出可用串口
python script/serial_power_control.py --list
```

### 作为库使用

```python
from serial_power_control import run_power_action

result = run_power_action(
    port="/dev/ttyUSB0",
    board_key="vlmu",
    action="on"
)
print(result.success)
```