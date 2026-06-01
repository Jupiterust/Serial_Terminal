# Serial Debug CLI Tool 😎🐙

轻量跨平台串口调试终端，专注终端交互、历史输入、HEX/ASCII 双向收发、多编码显示和带时间戳日志。

## 项目结构

```text
Serial_Terminal/
├── pyproject.toml
├── README.md
├── setup.py
├── src/
│   └── serial_debug_cli/
│       ├── __init__.py
│       ├── app.py              # 主交互循环、命令处理、快捷键状态切换
│       ├── codec.py            # 编码/解码与 HEX 转换
│       ├── config.py           # 串口配置模型
│       ├── logger.py           # 带时间戳日志
│       ├── main.py             # CLI 入口
│       ├── mock_serial.py      # --demo 内建虚拟 MCU/Loopback
│       ├── serial_worker.py    # 串口读线程与写入封装
│       ├── stm32_isp.py        # STM32 AN3155 系统 Bootloader 基础烧录
│       ├── terminal.py         # prompt_toolkit 输入缓冲、历史、快捷键
│       └── transfer.py         # Ymodem/Xmodem-1K 状态机、CRC16、进度模型
└── scripts/
    └── virtual_loopback.py     # Linux/macOS pty 虚拟串口单片机仿真
```

## 安装

建议使用虚拟环境：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Windows PowerShell：

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

Linux 串口权限通常需要加入 `dialout` 组：

```bash
sudo usermod -aG dialout "$USER"
```

执行后重新登录。macOS 常见串口名为 `/dev/tty.usbserial-*` 或 `/dev/tty.usbmodem-*`，Windows 常见串口名为 `COM3`、`COM4`。

## 运行

```bash
serial-debug
```

指定参数启动：

```bash
serial-debug --port COM3 --baudrate 115200 --encoding gbk
serial-debug --port /dev/tty.usbserial-110 --baudrate 9600 --hex
serial-debug --port COM3 --baudrate 115200 --transfer-timeout 3 --transfer-retries 5
serial-debug --demo
serial-debug --port COM3 --raw-stream
```

## 启动参数速查

```text
--port PORT                         串口名，如 COM3、/dev/ttyUSB0
--baudrate 115200 / --baud 115200   波特率
--bytesize 5|6|7|8                  数据位
--parity N|E|O|M|S                  校验位
--stopbits 1|1.5|2                  停止位
--encoding utf-8|gbk|gb2312|ascii   文本收发编码
--hex                               以 HEX 模式启动
--demo                              使用内建 mock MCU，不打开真实串口
--raw-stream                        关闭 RX 行缓冲，按接收 chunk 原样输出
--transfer-timeout 3                协议 ACK 等待超时，单位秒
--transfer-retries 5                单包最大重试次数
--packet-delay 2                    协议包间延时，单位毫秒
--verbose-protocol                  将协议层 Tx/Rx HEX 报文写入日志
--board generic|atomic|wildfire|inverted
--dtr-polarity normal|inverted      覆盖 DTR/BOOT0 极性
--rts-polarity normal|inverted      覆盖 RTS/RESET 极性
--reset-pulse 100                   RESET 脉冲宽度，单位毫秒
--reset-settle 200                  复位释放后等待时间，单位毫秒
```

## 快捷键

| 快捷键 | 作用 |
| --- | --- |
| `↑` / `↓` | 调用历史输入 |
| `Ctrl+H` | 切换 ASCII/HEX 显示与发送模式 |
| `Ctrl+L` | 清屏 |
| `Ctrl+E` | 切换发送结束符：`\r\n` / `\n` / 空 |
| `Ctrl+C` | 优雅退出 |
| `Ctrl+D` | 优雅退出 |

说明：`prompt_toolkit` 会统一不同平台的键盘事件。Windows Terminal、PowerShell、cmd、macOS Terminal、iTerm2、Linux 终端均使用同一套绑定；`Ctrl+V` 保留为终端/系统默认粘贴行为，粘贴后的文本进入当前输入缓冲区。

## 内置命令

所有命令都在输入行中以冒号开头：

```text
:help
:ports
:open COM3
:open 1
:close
:config
:baud 115200
:encoding utf-8
:encoding gbk
:hex
:ascii
:term crlf
:term lf
:term none
:log start logs/session.log
:log stop
:ymodem build/firmware.bin
:xmodem build/firmware.bin
:stm32-reset
:stm32-sync
:stm32-flash 0x08000000 build/firmware.bin
:clear
:quit
```

普通输入会发送到串口。HEX 模式下输入示例：

```text
AA BB CC 0D 0A
AA:BB:CC
AABBCC
```

ASCII 模式下会按当前编码发送文本，并根据当前结束符自动追加 `\r\n`、`\n` 或不追加。

## RX 显示策略

默认启用行缓冲渲染：文本模式下，收到的数据会先缓存在当前 RX 行中，遇到 `\n` / `\r\n` 后以完整行输出；如果 50 ms 内没有新数据，也会自动刷新当前半行。这样可以避免 MCU 分段发送一行文本时被多个时间戳切碎。

如果需要观察完全原始的接收 chunk，可使用：

```bash
serial-debug --port COM3 --raw-stream
```

HEX 模式始终按接收 chunk 显示，避免二进制数据被文本行规则误处理。终端输出会清洗 XML/终端非法控制字符，例如 `\x00`、`\x1B`、孤立 surrogate 会显示成可见转义文本，防止 `prompt_toolkit.HTML()` 因非法 XML token 崩溃。

## 固件文件传输

### Ymodem-1K / Xmodem-1K

工具内置标准 CRC16 模式的 Ymodem-1K 和 Xmodem-1K 发送端。进入传输命令后，普通串口 Rx 显示会暂停，协议状态机独占串口读取单片机 Bootloader 发出的控制字节：

- `C`：接收端请求 CRC16 模式并启动传输
- `ACK`：当前包确认
- `NAK`：当前包重传
- `CAN`：接收端取消

Ymodem 每包 1024 字节数据，元数据包和结束空包使用 128 字节包；每包格式为：

```text
STX/SOH + packet_no + ~packet_no + payload + CRC16_H + CRC16_L
```

发送 `.bin` 或 `.hex` 文件：

```text
:ymodem build/app.bin
:ymodem build/app.hex
:xmodem build/app.bin
```

传输过程中会显示进度条、百分比、已发送字节、实时 KiB/s、ETA、当前包号和重试次数。单包等待超时和最大重试次数可在启动时配置：

```bash
serial-debug --port COM3 --baudrate 115200 --transfer-timeout 3 --transfer-retries 5 --packet-delay 2
```

如果硬件链路有噪声或 Bootloader 处理较慢，`--packet-delay 2` 会在协议包之间插入 2 ms 延时。`--verbose-protocol` 会把底层报文写入日志，例如 `[Tx] 01 00 FF ...` 和 `[Rx] 06`：

```bash
serial-debug --port COM3 --verbose-protocol
:log start logs/protocol.log
:ymodem build/app.bin
```

### STM32 官方系统 ISP

已实现 STM32 AN3155 UART Bootloader 的基础能力：

- `:stm32-reset`：DTR 控制 BOOT0，RTS 控制 RESET，并按配置极性脉冲复位
- `:stm32-sync`：发送 `0x7F` 自动波特率识别字节，并读取芯片 ID
- `:stm32-flash ADDRESS FILE`：`0x7F` 同步、Extended Mass Erase、Write Memory 分块写入 `.bin`

示例：

```text
:stm32-reset
:stm32-sync
:stm32-flash 0x08000000 build/app.bin
```

不同 USB-UART 模块的 DTR/RTS 电平可能经过反相电路，可用启动参数柔性适配：

```bash
serial-debug --port COM3 --board atomic
serial-debug --port COM3 --board wildfire
serial-debug --port COM3 --dtr-polarity inverted --rts-polarity normal
serial-debug --port COM3 --reset-pulse 120 --reset-settle 300
```

可用预设包括 `generic`、`atomic`、`wildfire`、`inverted`。如果板卡版本或 USB-UART 芯片反相逻辑不同，以 `--dtr-polarity` / `--rts-polarity` 覆盖预设。

## 虚拟仿真调试

内建 Demo 模式不需要真实串口：

```bash
serial-debug --demo
```

它会连接一个进程内 mock MCU：周期发送中文、HEX 字节和长字符串，并把 CLI 发出的内容原样回显，用于验证输入缓冲保护、历史输入、HEX/ASCII 切换和编码处理。

Linux/macOS 也可以启动 pty 虚拟串口对：

```bash
scripts/virtual_loopback.py
```

脚本会打印一个 slave 设备名，例如 `/dev/ttys012`，然后在另一个终端连接：

```bash
serial-debug --port /dev/ttys012 --baudrate 115200
```

Windows 没有 POSIX pty。推荐直接使用 `serial-debug --demo`；如果需要两个真实 COM 口形态，可安装 com0com 创建 `COM10 <-> COM11` 这类虚拟串口对。

## 架构说明

- 串口读写分离：`SerialWorker` 使用后台读线程阻塞读取串口，主线程负责终端交互和写入。
- 协议传输独占：Ymodem/Xmodem/STM32 ISP 执行时会临时停止后台读线程，避免 ACK/NAK/CAN 被普通 Rx 消费。
- 输入缓冲保护：`prompt_toolkit.patch_stdout()` 会在 Rx 输出前临时让出当前输入行，输出完成后恢复未发送文本，避免串口数据冲散用户正在输入的命令。
- 行缓冲渲染：文本 RX 默认按行输出，遇到换行或 50 ms 空闲再打印完整行；`--raw-stream` 可恢复 chunk 级输出。
- 编码解耦：`codec.py` 将字符编码转换和 HEX 转换独立出来，并使用增量解码缓存 UTF-8/GBK 半个字符，避免拆包乱码。
- 输出安全：串口乱码中的非法 XML/控制字符会被转换为可见转义文本，避免 `prompt_toolkit.HTML()` 解析崩溃。
- 异步日志：`logger.py` 使用后台队列写盘，避免高波特率 Rx 时磁盘 I/O 阻塞串口读取。
- 热插拔保护：读线程捕获 `SerialException`/`OSError` 后关闭端口并通知主循环，CLI 进入等待重连状态。
- 固件传输：`transfer.py` 实现 CRC16、1024 字节分包、超时、重试和取消状态；`stm32_isp.py` 实现 AN3155 的同步、擦除和写内存命令。
- 安全退出：独占串口会话使用 context manager，协议异常退出时恢复 timeout、重启普通接收线程，并释放 DTR/RTS。
- 优雅退出：退出时先停止读线程，再关闭串口，并恢复终端状态。

## 常见问题

- Windows 打不开串口：确认串口未被其它软件占用，端口名使用 `COM3` 这类完整名称。
- Linux 权限不足：加入 `dialout` 组并重新登录，或临时使用 `sudo`。
- 中文乱码：尝试 `:encoding gbk` 或 `:encoding gb2312`。
- `Ctrl+H` 被终端当作退格：部分终端会把 `Ctrl+H` 与 Backspace 映射为同一码位，可改用命令 `:hex` / `:ascii`。
