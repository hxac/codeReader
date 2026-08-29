# USB CDC Shell：命令系统与跨线程执行（u5-l1）

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 NanoVNA 如何通过 USB CDC-ACM 把自己变成一个「虚拟串口」，以及 `SDU1` 为什么能被当作一个通用字符流（`BaseSequentialStream`）来读写。
2. 逐行读懂自研 shell 的两步实现：`VNAShell_readLine()` 的单字节读取与回显、`VNAShell_executeLine()` 的「原地」参数切分与查表分发。
3. 理解 `commands[]` 命令表这种数据驱动设计，以及 chprintf 裁剪版为 VNA 场景做的定制（`%q` 频率格式化、`%F` 自动 SI 前缀浮点、`plot_printf`）。
4. 掌握 `CMD_WAIT_MUTEX` 标志的跨线程执行机制：主线程只「递交」一个函数指针，真正的命令体在 sweep 线程里执行，从而在**没有互斥量**的前提下避免并发冲突。
5. 独立完成综合实践：仿照现有命令新增 `uptime` 命令，并注册进命令表。

本讲是第 2 单元「Thread1 主循环与并发协作」（u2-l5）的延伸：u2-l5 讲的是 sweep 线程内部怎么跑，本讲讲的是**另一条线程**（main/USB shell 线程）如何与它安全地共享一台仪器。

## 2. 前置知识

### 2.1 USB CDC-ACM：虚拟串口是什么

- **CDC**（Communications Device Class）是 USB 的一个设备类标准，**ACM**（Abstract Control Model）是其中最常用的子模型。实现了 CDC-ACM 的设备插到电脑上，操作系统会加载内置串口驱动（Linux 的 `cdc_acm`、Windows 的 `usbser.sys`），呈现出一个新的串口（Linux 下通常是 `/dev/ttyACM0`）。
- 它**不是真的 UART**：波特率、校验位等参数对 CDC 设备通常没有实际意义（设备按 USB 全速 12Mbps 收发），只是惯例上大家仍用 115200 8N1 打开它。
- USB 通信靠**端点（Endpoint, EP）**：每个端点是一个有方向（IN=设备→主机，OUT=主机→设备）和类型（bulk/批量、interrupt/中断、control/控制）的管道。CDC-ACM 典型布局是：一对 bulk IN/OUT 端点跑数据，一个 interrupt IN 端点发状态通知。
- 主机识别设备靠**描述符（descriptor）**：设备描述符（我是谁，VID/PID）、配置描述符（我有哪些接口和端点）、字符串描述符（厂商名、产品名）。这套数据在**枚举**阶段由主机读取。

### 2.2 BaseSequentialStream：ChibiOS 的「流接口」

ChibiOS 用 C 结构体里的函数指针模拟面向对象：`BaseSequentialStream` 定义了 `streamRead()` / `streamPut()` 等「虚方法」。任何驱动只要把自己的结构体头部摆成这个接口的形状（驱动作者已替你做好），就能被当成通用流使用。本讲的 `SDU1`（USB 串口）就是这样一个对象，而 `chvprintf()` 只认 `BaseSequentialStream *`，不关心底层是 USB 还是内存缓冲。

### 2.3 双线程模型回顾（来自 u2-l5）

- **main 线程**：`main()` 跑完初始化后，进入死循环专门伺候 USB shell。
- **sweep 线程（Thread1）**：`chThdCreateStatic()` 创建，循环执行 `sweep(true)`（测量）→ `ui_process()`（按键/触摸）→ `plot_into_index()` + `draw_all()`（绘图）。
- 两个线程共享大量状态：`measured[]` 测量结果、`frequencies[]` 频点表、si5351 寄存器、LCD 和 `spi_buffer`。**谁能在什么时候碰这些数据，就是本讲 CMD_WAIT_MUTEX 要解决的问题。**

### 2.4 为什么没有互斥量可用

NanoVNA 的 STM32F072 只有 16KB RAM，作者把 ChibiOS 裁剪到了骨头：互斥量和信号量都被关闭（见 4.4 的源码证据）。所以这里的跨线程同步是「手搓」的——一个 `volatile` 函数指针加轮询，我们在 u2-l5 见过类似思路，本讲看它的完整形态。

### 2.5 终端行基础

串口终端逐字符收发。你在键盘上敲一个键，终端立刻把该字符发给设备；设备把字符**原样发回来**（回显），你才能在屏幕上看到自己输入了什么。Enter 键发送 `\r`（CR，回车），退格键发送 `0x08` 或 `0x7f`——这些细节都会在 `readLine` 源码里出现。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [main.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c) | 固件主体 | shell 全部实现（L32-L59 声明区、L2231-L2312 解析、L2143-L2222 命令表、L2432-L2454 主循环）、Thread1 的延迟执行点（L121-L126）、`cmd_capture`（L727-L745） |
| [usbcfg.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/usbcfg.c) | USB CDC 配置 | 设备/配置描述符、端点初始化、`SDU1` 定义、`serusbcfg` 接线 |
| [usbcfg.h](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/usbcfg.h) | 对外声明 | `extern SDU1` 等三个符号（L20-L22） |
| [chprintf.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chprintf.c) | 裁剪增强版 printf | `%q` 频率格式化、`%F` 自动前缀浮点、`plot_printf` |
| [ili9341.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c) | LCD 驱动 | `ili9341_read_memory()` 读显存（L475-L517，capture 命令的底层） |
| [chconf.h](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chconf.h) | RTOS 内核配置 | `CH_CFG_ST_FREQUENCY`（L51）、互斥量/信号量关闭（L167/L186） |
| [Makefile](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile) | 构建脚本 | 自研 shell 的证据（L109）、源文件清单（L117-L126）、`make flash`（L228-L229） |

## 4. 核心概念与源码讲解

### 4.1 SDU1：USB CDC 虚拟串口驱动

#### 4.1.1 概念说明

`SDU1` 是一个 `SerialUSBDriver`（ChibiOS 的 Serial-over-USB 驱动实例），它把「USB 包收发」包装成「逐字节读写」的串口接口。要让它工作，固件必须向 USB 主机（电脑）交代清楚三件事：

1. **我是谁**——设备描述符：VID=0x0483（STMicroelectronics）、PID=0x5740、设备类 0x02（CDC）。
2. **我怎么通信**——配置描述符：两个接口（ACM 控制接口 + 数据接口）、一对 64 字节 bulk 端点、一个 8 字节 interrupt 端点。
3. **事件来了怎么办**——回调：主机完成枚举（`USB_EVENT_CONFIGURED`）时初始化端点；每 1ms 的 SOF（帧起始）中断里做流量维护。

shell 拿到的只是一个指针：`shell_stream = (BaseSequentialStream *)&SDU1;`，之后所有 `streamRead` / `streamPut` / `chvprintf` 都面向这个抽象接口，完全不知道 USB 的存在。

#### 4.1.2 核心流程

```text
上电
 ├─ sduObjectInit(&SDU1)          # SDU1 对象清零、初始化
 ├─ sduStart(&SDU1, &serusbcfg)   # 绑定 USB 驱动 USBD1 和 3 个端点号
 ├─ usbDisconnectBus(...)         # 先断开（拉低 D+），确保主机重新枚举
 ├─ 等 100ms
 ├─ usbStart(USBD1, &usbcfg)      # 注册回调：usb_event / get_descriptor / sof_handler
 └─ usbConnectBus(...)            # D+ 上拉 → 主机看到设备，开始枚举

枚举阶段（主机发起，固件被动应答）
 ├─ 主机读设备/配置/字符串描述符  → get_descriptor() 返回静态表
 ├─ 主机下发配置                  → usb_event(USB_EVENT_CONFIGURED)
 │    ├─ usbInitEndpointI(EP1, bulk IN/OUT 64B)
 │    ├─ usbInitEndpointI(EP2, interrupt IN 8B)
 │    └─ sduConfigureHookI(&SDU1)   # CDC 子系统复位
 └─ 主机加载 cdc_acm 驱动 → 出现 /dev/ttyACM0

运行阶段
 ├─ 主机每 1ms 发 SOF → sof_handler() → sduSOFHookI()（超时/流量管理）
 ├─ 主机→设备数据落在 EP1 OUT → sduDataReceived → SDU1 输入队列
 └─ shell 线程看到 state == USB_ACTIVE，开始打印提示符、读行
```

#### 4.1.3 源码精读

**SDU1 实例与端点编号**。[usbcfg.c:L20-L27](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/usbcfg.c#L20-L27) 定义了全局的 `SerialUSBDriver SDU1;`，并规定：数据收发都走 EP1（一对 bulk IN/OUT），中断通知走 EP2。

**设备描述符**。[usbcfg.c:L32-L45](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/usbcfg.c#L32-L45) 用 `USB_DESC_DEVICE` 宏拼出 18 字节标准设备描述符：bcdUSB 1.1、设备类 CDC（0x02）、最大包 0x40（64 字节）、`idVendor=0x0483`、`idProduct=0x5740`。主机就是靠这对 VID/PID 决定加载哪个驱动。

**配置描述符（CDC 的核心）**。[usbcfg.c:L56-L130](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/usbcfg.c#L56-L130) 是一棵 67 字节的描述符树：

- 接口 0：CDC 通信/ACM 控制接口（[usbcfg.c:L65-L75](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/usbcfg.c#L65-L75)），后面跟着 Header/Call Management/ACM/Union 四个功能描述符，声明「我是一个虚拟串口」；
- 接口 0 的中断端点：EP2 IN、8 字节（[usbcfg.c:L104-L108](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/usbcfg.c#L104-L108)）；
- 接口 1：CDC 数据接口，带一对 64 字节 bulk 端点（OUT 在 [usbcfg.c:L120-L124](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/usbcfg.c#L120-L124)，IN 在 [usbcfg.c:L125-L129](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/usbcfg.c#L125-L129)）——shell 的命令输入和所有输出都走这一对端点。

**描述符分发**。[usbcfg.c:L197-L214](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/usbcfg.c#L197-L214)：主机 `GET_DESCRIPTOR` 请求到来时按类型返回对应的静态表（设备/配置/字符串）。

**端点配置结构体**。[usbcfg.c:L219-L261](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/usbcfg.c#L219-L261)：`ep1config` 把 EP1 配成 bulk、收发缓冲 64 字节，并挂上 `sduDataTransmitted` / `sduDataReceived` 两个回调（它们是 ChibiOS SerialUSB 驱动的一部分，负责把包搬进/搬出 SDU1 的队列）；`ep2config` 把 EP2 配成 interrupt。

**事件回调**。[usbcfg.c:L266-L302](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/usbcfg.c#L266-L302)：关键的 `USB_EVENT_CONFIGURED` 分支（L274-L287）在中断上下文里（所以函数名都带 `I` 后缀）初始化两个端点并调用 `sduConfigureHookI` 复位 CDC 状态。`USB_EVENT_SUSPEND` 分支则调用 `sduDisconnectI` 通知断开。

**SOF 回调与总配置**。[usbcfg.c:L307-L324](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/usbcfg.c#L307-L324)：`sof_handler` 每 1ms 调 `sduSOFHookI`；`usbcfg` 把四个回调打包成 `USBConfig`。最后 [usbcfg.c:L329-L334](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/usbcfg.c#L329-L334)：`serusbcfg` 把 `USBD1` 与三个端点号接给 SerialUSB 驱动，`sduStart(&SDU1, &serusbcfg)` 用的就是它。

**初始化顺序与主循环**。[main.c:L2385-L2395](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2385-L2395) 严格按 4.1.2 的顺序初始化（注意 `usbDisconnectBus` + 100ms 延时：复位后先假意断开，主机才会重新走一遍枚举）。[main.c:L2432-L2454](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2432-L2454)：主线程死循环，只有当 `SDU1.config->usbp->state == USB_ACTIVE`（枚举完成、串口被主机打开）才进入 shell；否则每秒醒来查一次。把 `SDU1` 转型成流的那一行在 [main.c:L39](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L39)。

#### 4.1.4 代码实践：亲眼看一次枚举

1. **实践目标**：验证固件描述符与主机行为，建立「源码 ↔ 系统」的对应感。
2. **操作步骤**（Linux 为例，Windows 可用设备管理器 + USBView）：
   - 把 NanoVNA 用 USB 连到电脑：`lsusb | grep 0483:5740`；
   - 查看完整描述符：`lsusb -d 0483:5740 -v | head -60`，找 CDC 接口和两个 bulk 端点；
   - `dmesg | tail -20`，确认内核加载 `cdc_acm` 并创建了 `/dev/ttyACM0`；
   - 打开串口：`screen /dev/ttyACM0 115200`（或 `picocom`），按一次回车，应看到 `ch> ` 提示符。
3. **需要观察的现象**：`lsusb -v` 输出里的 `bInterfaceClass 2 Communications` / `bInterfaceClass 10 CDC Data`、`wMaxPacketSize 64` 的 bulk 端点——对应 [usbcfg.c:L56-L130](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/usbcfg.c#L56-L130) 的静态表。
4. **预期结果**：`cdc_acm 1-1:1.0 ttyACM0: USB Serial device registered` 之类的内核日志。
5. 以上均为「待本地验证」：我无法替你运行这些命令，若现象不符（例如某些系统把该 VID/PID 识别为其他设备），以你本机输出为准。

#### 4.1.5 小练习与答案

**练习 1**：shell 是通过哪个函数判断「主机已经打开了我的虚拟串口」的？
答案：主循环里的 `SDU1.config->usbp->state == USB_ACTIVE`（[main.c:L2433](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2433)）。`USB_ACTIVE` 表示 USB 驱动已完成枚举且配置生效。

**练习 2**：为什么初始化时要先 `usbDisconnectBus()` 再等 100ms 才 `usbStart()` + `usbConnectBus()`？
答案：见 [main.c:L2387-L2391](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2387-L2391) 的注释：复位后若 D+ 一直保持上拉，主机会认为设备没变过、不重新枚举；先断开再连接，强制主机重走一遍枚举流程（这正是插拔 USB 线的软件等价物）。

**练习 3**：`streamPut(shell_stream, c)` 最终落到哪个端点？
答案：EP1 bulk IN。`streamPut` 经 `BaseSequentialStream` 虚方法表进入 SerialUSB 驱动，数据从 EP1 IN（[usbcfg.c:L125-L129](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/usbcfg.c#L125-L129) 声明的 64 字节 bulk 端点）发给主机。

### 4.2 VNAShell 命令解析：readLine 与 executeLine

#### 4.2.1 概念说明

ChibiOS 其实自带一个 shell（`os/various/shell/`），但本工程**没有用它**——证据在 [Makefile:L109](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile#L109)：`#include $(CHIBIOS)/os/various/shell/shell.mk` 被注释掉了；[Makefile:L207](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile#L207) 还定义 `-DSHELL_CMD_TEST_ENABLED=FALSE`（防呆）。[main.c:L35-L37](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L35-L37) 的注释说明原因：shell 默认在 main 线程里跑；若要挪到独立线程（`VNA_SHELL_THREAD`），要多占一块线程栈，还得缩小 `spi_buffer`。16KB RAM 的机器上，一个约 90 行、零动态内存的自研 shell 是更划算的选择。

自研 shell 的全部家当（[main.c:L39-L59](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L39-L59)）：

- 一个流指针 `shell_stream`；
- 四个配置宏：换行 `"\r\n"`、提示符 `"ch> "`、最多 4 个参数、命令行最长 48 字符；
- 命令函数类型 `vna_shellcmd_t` 和定义宏 `VNA_SHELL_FUNCTION`；
- 三个静态变量：行缓冲 `shell_line[48]`、参数指针数组 `shell_args[5]`、参数个数 `shell_nargs`，以及一个 `volatile` 函数指针 `shell_function`（4.4 的主角）。

#### 4.2.2 核心流程

**读一行（readLine）**：

```text
循环:
  streamRead(shell_stream, &c, 1)   # 阻塞读 1 字节；返回 0 说明流断了 → 返回 0
  c == 8 或 0x7f ?                  # 退格/删除
      若行缓冲非空: 回显 {0x08,0x20,0x08}（左移+覆盖+再左移），ptr--
  c == '\r' ?                       # 回车 = 行结束
      回显 CRLF，*ptr = 0（写字符串结束符），返回 1
  c < 0x20 ?                        # 其他控制字符（含 \n、方向键转义序列的 ESC）
      直接丢弃
  否则:
      若还有空间: 回显该字符并存入缓冲；没空间则静默丢弃
```

**执行一行（executeLine）**：

```text
lp = 行首
while *lp:
  跳过前导空格/Tab
  若参数以 " 开头 → 找下一个 " 作为结束；否则找空格/Tab 作为结束
  shell_args[nargs++] = lp          # 记录参数起始
  找不到结束符 → 已是最后一个参数，跳出
  nargs > 4 ? → 打印 "too many arguments" 返回
  *lp = 0                           # 把分隔符原地改成 NUL，参数即成为独立字符串
在 commands[] 里线性 strcmp 查 shell_args[0]
  命中且 flags 含 CMD_WAIT_MUTEX → 递交给 sweep 线程（见 4.4）
  命中且无标志 → 直接调用 cmd_xxx(nargs-1, &shell_args[1])
  没命中 → 打印 "命令名?"
```

**原地切分**是这段代码最漂亮的地方：不需要 malloc、不需要复制，参数字符串就「长」在原缓冲区里——把分隔符字节直接改写成 `'\0'` 即可。以输入 `scan 1000000 900000000 101 15` 为例：

```text
切分前 shell_line:  scan 1000000 900000000 101 15\0
切分后 shell_line:  scan\0 1000000\0 900000000\0 101\0 15\0
shell_args[0..4] →  指向上述 5 个字符串的首地址，shell_nargs = 5
命令收到:          argc = 5-1 = 4, argv[0..3] = "1000000","900000000","101","15"
```

#### 4.2.3 源码精读

**shell 配置与状态**。[main.c:L39-L48](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L39-L48)：提示符、长度上限、参数上限；[main.c:L51-L59](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L51-L59)：`vna_shellcmd_t` 类型、`VNA_SHELL_FUNCTION(name)` 宏（展开为 `static void name(int argc, char *argv[])`）以及四个静态变量。注意 `shell_args` 的大小是 `VNA_SHELL_MAX_ARGUMENTS + 1`——命令名也占一格。

**readLine 退格处理**。[main.c:L2241-L2248](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2241-L2248)：`static const char backspace[] = {0x08, 0x20, 0x08, 0x00};` 回显三个字符——退格（光标左移一格）、空格（把残留字符覆盖成空白）、再退格（光标回到原位）。这是无状态终端下删字符的标准技巧，最后补的 `0x00` 只是让数组能当字符串传给 `shell_printf`。

**readLine 行结束与回显**。[main.c:L2250-L2262](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2250-L2262)：只有 `\r` 被认为是回车；其他控制字符（包括 `\n`）一律跳过——所以终端必须配成「发送 CR」而不是「发送 LF」。存入与回显在同一分支里（[main.c:L2259-L2262](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2259-L2262)），且以 `ptr < line + max_size - 1` 为界：超过 47 个字符后输入会被**静默**丢弃（不回显、不警告），这是 48 字节缓冲的硬保护。

**executeLine 切分器**。[main.c:L2275-L2293](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2275-L2293)：`ep = (*lp == '"') ? strpbrk(++lp, "\"") : strpbrk(lp, " \t");` 一行处理两种定界——带引号的参数（可含空格）找闭引号，普通参数找空白。随后 `*lp++ = 0` 把分隔符改成 NUL，实现 4.2.2 的原地切分。参数上限检查在 [main.c:L2286-L2290](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2286-L2290)：命令名 + 4 个参数是极限，第 5 个参数触发 `too many arguments`。

**executeLine 分发**。[main.c:L2296-L2311](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2296-L2311)：线性 `strcmp` 扫描命令表；未命中时打印 `%s?`（如输入 `foo` 会回 `foo?`）。`CMD_WAIT_MUTEX` 分支（L2299-L2304）留到 4.4 精读。

**主循环把它们串起来**。[main.c:L2432-L2454](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2432-L2454)：`USB_ACTIVE` 后打印横幅 `NanoVNA Shell`，然后 `do { 打印 "ch> "; readLine; executeLine } while (USB_ACTIVE)`。`readLine` 返回 0（USB 断开）时睡 200ms 再查；外层每秒轮询等待重新插上。

#### 4.2.4 代码实践：与解析器对话

1. **实践目标**：亲手触发解析器的每条分支，把源码行为变成肌肉记忆。
2. **操作步骤**（接好 USB 串口终端后）：
   - 敲 `versio` 然后按退格补成 `version` 回车——观察退格回显；
   - 输入 `help` 回车；
   - 输入 `scan 1 2 3 4 5`（命令名 + 5 个参数）回车；
   - 输入 `xyz` 回车。
3. **需要观察的现象**：
   - 退格时屏幕上字符真的消失（对应 L2243 的三字符回显）；
   - `scan 1 2 3 4 5` 回 `too many arguments, max 4`（对应 [main.c:L2286-L2290](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2286-L2290)）；
   - `xyz` 回 `xyz?`（对应 [main.c:L2311](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2311)）。
4. **预期结果**：如上三条；另外试想：把终端的「回车映射」改成 LF 会发生什么？——`\n` 被 `c < 0x20` 分支丢弃，命令永远发不出去。
5. 本实践「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `shell_args` 定义为 5 个元素，而 `VNA_SHELL_MAX_ARGUMENTS` 是 4？
答案：见 [main.c:L57](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L57)：`shell_args[VNA_SHELL_MAX_ARGUMENTS + 1]`——第 0 格存命令名，后 4 格才是参数。命令函数拿到的 `argc = shell_nargs - 1`、`argv = &shell_args[1]`（[main.c:L2306](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2306)），与 C 标准 `main` 的习惯一致。

**练习 2**：引号参数有什么用？举一个本固件的使用场景。
答案：`"..."` 让一个参数里可以包含空格（[main.c:L2280](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2280)）。例如 `trace 0 "some name"` 这类带空格的名字可作为一个整体传入；对上位机批量发命令也有用（可避免自行转义空格）。

**练习 3**：如果命令行敲了 60 个字符，会发生什么？
答案：前 47 个字符被回显并保存，之后输入全部静默丢弃（[main.c:L2259-L2262](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2259-L2262) 的边界检查）。缓冲区不会溢出，用户会看到「敲了字但屏幕没反应」的现象。

### 4.3 commands 命令表与 chprintf 裁剪

#### 4.3.1 概念说明

**命令表是数据驱动设计**：每条命令只是 `{名字, 函数, 标志}` 三元组，shell 引擎（readLine/executeLine）完全不认识任何具体命令。加一条命令 = 写一个函数 + 表里加一行，两处改动，零引擎改动。这正是综合实践要利用的性质。

**chprintf 是 ChibiOS 的迷你 printf**：只认 `%c %s %d %u %x %o` 和有限标志，输出目标不是 stdout 而是 `BaseSequentialStream`。本仓库根目录的 [chprintf.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chprintf.c) 是一份**裁剪增强版**，针对 VNA 场景做了三件典型的事：

1. **砍**：删掉 `%l/%L` 长度修饰符解析（M0 上 int 与 long 都是 32 位，没必要）；把 `chprintf()` / `chsnprintf()` 用 `#if 0` 关掉（省 flash，工程统一走自己的 `shell_printf` / `plot_printf` 包装）。
2. **加**：`%q`——把频率（Hz 数）格式化成带 SI 前缀的易读形式（k/M/G，三位一组、首组分隔符变小数点），内部用**移位实现除以 10** 避免 Cortex-M0 没有硬件除法器的惩罚；
3. **加**：`%F`——浮点数自动 SI 前缀（大到 T、小到 y），配合 `%f` 精度控制，用于屏幕上画时延（s）、距离（m）等量纲跨度极大的数。

> **构建来源说明（待确认）**：[Makefile:L124](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile#L124) 通过 `$(STREAMSSRC)` 引入的 chprintf.c 来自 ChibiOS 子模块（[Makefile:L93](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile#L93)），而根目录这份定制版并未列入 [Makefile:L117-L126](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile#L117-L126) 的 CSRC；同时 `plot_printf` 只在根目录这份里定义（[chprintf.c:L577](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chprintf.c#L577)），却声明于 [nanovna.h:L488](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L488) 并被 plot.c/ui.c 大量调用。子模块固定指针上的 streams/chprintf.c 是 ChibiOS 原版（无 `%q`/`%F`/`plot_printf`）。因此「固件里实际生效的 chvprintf 是哪一份」取决于本地 ChibiOS 检出内容——推测作者本地子模块的该文件已被同内容替换，根目录这份是改动留档。验证办法见 4.3.4 实践 2。**阅读格式化行为时以根目录这份为准**，因为 `%q`/`%F`/`plot_printf` 只存在于这里。

#### 4.3.2 核心流程

```text
命令注册（编译期）
  commands[] = { {"version", cmd_version, 0}, ..., {NULL, NULL, 0} }
  条件宏 ENABLE_* 决定部分条目是否存在

命令输出（运行期）
  cmd_xxx → shell_printf(fmt, ...)
          → chvprintf(shell_stream, fmt, ap)      # 解析 % 占位符
          → 逐字符 streamPut → USB EP1 bulk IN → 主机终端

屏幕输出（sweep 线程，另一条路）
  plot.c/ui.c → plot_printf(buf, size, fmt, ...)  # 写入内存缓冲，不碰 USB
              → chvprintf((BaseSequentialStream*)&printStream, ...)
```

`shell_printf` 本体只有 8 行（[main.c:L280-L288](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L280-L288)）：把可变参数打包成 `va_list` 转交 `chvprintf`，目标流固定为 `shell_stream`。

#### 4.3.3 源码精读

**命令结构体与标志定义**。[main.c:L2143-L2152](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2143-L2152)：`VNAShellCommand` 三个字段——名字指针、函数指针、`uint16_t` 标志；`#pragma pack(push, 2)` 把结构体压到 2 字节对齐（省 ROM，与 u4 讲过的位打包同一思路）。`CMD_WAIT_MUTEX` 定义处的注释直接点题：**有些命令只能在 sweep 线程执行，不能在主循环执行**。

**命令表本体**。[main.c:L2153-L2208](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2153-L2208)：默认编译下共 36 条命令（33 条无条件 + `vbat_offset`/`info`/`color` 三条由 [main.c:L67-L71](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L67-L71) 的宏开启），表尾以 `{NULL, NULL, 0}` 哨兵结束。带 `CMD_WAIT_MUTEX` 的共 8 条：`freq`、`data`、`scan`、`touchcal`、`touchtest`、`cal`、`recall`、`capture`（4.4 会解释为什么恰好是它们）。注意 `time`/`dump`/`threads` 三条被注释掉的宏（[main.c:L61-L65](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L61-L65)）裁掉了对应命令——ROM 里的代码也随之消失，这是编译期裁剪。

**cmd_help：表驱动的直接受益者**。[main.c:L2210-L2222](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2210-L2222)：`help` 命令自己只是从表头走到哨兵、把每个 `sc_name` 打出来。新增命令后 `help` 自动包含它，无需修改。注意 [main.c:L2141](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2141) 有一行前置声明——`cmd_help` 定义在表之后，但表里要用它。

**shell_printf**。[main.c:L280-L288](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L280-L288)：`va_start`/`chvprintf`/`va_end` 三步，返回格式化字节数（返回值在工程里几乎都被忽略）。

**`%q`：频率格式化 ulong_freq**。[chprintf.c:L370-L373](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chprintf.c#L370-L373) 是入口，实现体在 [chprintf.c:L86-L159](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chprintf.c#L86-L159)。算法要点：

- 从个位开始逐位取出十进制数字，每 3 位插一个分隔空格，同时数出「组数 s」——组数直接决定 SI 前缀（`bigPrefix[]` 的 `k/M/G/...`，[chprintf.c:L50-L55](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chprintf.c#L50-L55)）；
- 复制阶段把**第一个**分隔空格替换成小数点（`FREQ_PSET` 标志控制，[chprintf.c:L142-L153](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chprintf.c#L142-L153)），于是 `900000000` 会呈现为 `900.xxx` + 前缀 `M` 的形式；
- 精度（如 `%.9q`）限制总位数，超限时丢掉分隔空格（`FREQ_NO_SPACE`）。
- 除以 10 用移位近似完成（[chprintf.c:L101-L121](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chprintf.c#L101-L121)）：\( q \approx \lfloor x \cdot 0.8 \rfloor \) 再右移 3 位等价于 \( \lfloor x/10 \rfloor \)，余数用减法修正——因为 Cortex-M0 **没有硬件除法指令**，`%10`/`/10` 会调入软件除法库，又慢又占空间。plot.c 里大量 `"%qHz"`（如 [plot.c:L1601](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1601)）都走这条路。

**`%F`：自动前缀浮点 ftoaS**。[chprintf.c:L374-L394](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chprintf.c#L374-L394) 中小写 `%f` 走定点小数 `ftoa`（[chprintf.c:L162-L183](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chprintf.c#L162-L183)，注意它用 `(num-l)*multi+0.5` 自己做四舍五入，`INFINITY` 输出为 `0x19` 特殊字符），大写 `%F` 走 `ftoaS`（[chprintf.c:L185-L208](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chprintf.c#L185-L208)）：大于 1000 就除以 1000 并把前缀从 `bigPrefix` 里右移，小于 1 就乘 1000 从 `smallPrefix` 里取——例如 \( 145\times10^{-9} \) 秒会被折算成 `145.x` + `n` 前缀，屏幕上即「ns」量级的时延读数。`CHPRINTF_FORCE_TRAILING_ZEROS`（[chprintf.c:L40](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chprintf.c#L40)）让小数位数固定，避免读数宽度跳动。

**裁剪痕迹**：长度修饰符解析被整段注释（[chprintf.c:L324-L333](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chprintf.c#L324-L333)），所有整数按 32 位取参；`chprintf()`（[chprintf.c:L476-L487](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chprintf.c#L476-L487)）与 `chsnprintf()`（[chprintf.c:L516-L546](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chprintf.c#L516-L546)）双双 `#if 0`。

**plot_printf：只写缓冲的迷你流**。[chprintf.c:L551-L592](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chprintf.c#L551-L592)：定义了一个只实现 `put` 方法的 `printStream`（比 ChibiOS 的 MemoryStream 更省），`plot_printf` 把格式化结果写进调用者给的字符缓冲——屏幕上每个数字（标记频率、时延、阻抗）都是先用它格式化成字符串，再交给 u4-l1 讲过的字模渲染画上去。

#### 4.3.4 代码实践

**实践 1（阅读型）：数一数命令表，对照 help 输出**

1. **实践目标**：验证「命令表 = help 的唯一事实来源」。
2. **操作步骤**：在源码里数 [main.c:L2155-L2207](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2155-L2207) 的条目数（记得排除被关闭的 `#ifdef ENABLED_DUMP/ENABLE_THREADS_COMMAND/ENABLE_TIME_COMMAND` 三条）；接上终端敲 `help`，数输出单词数。
3. **需要观察的现象**：`help` 的输出与表内容一一对应。
4. **预期结果**：默认编译下 36 个命令名（本讲按源码手数所得，待本地验证）。

**实践 2（构建考古型）：确认固件里链接的是哪份 chprintf**

1. **实践目标**：解决 4.3.1 提出的「两份 chprintf.c」疑问。
2. **操作步骤**：`make` 构建后查看 `build/ch.map`（链接器符号表），执行 `grep -A2 chvprintf build/ch.map`，看该符号来自哪个 `.o` 文件（`build/chprintf.o` 的源路径会注明是根目录还是 ChibiOS 子模块）；也可以 `arm-none-eabi-nm build/ch.elf | grep plot_printf` 确认定制符号存在。
3. **需要观察的现象**：`chvprintf` 所在的目标文件路径。
4. **预期结果**：待本地验证——如果链接的是子模块原版，`plot_printf` 会报未定义符号而链接失败；能成功构建则说明本地子模块的 chprintf.c 已是定制版（或构建环境另有覆盖）。

#### 4.3.5 小练习与答案

**练习 1**：`%q` 为什么要手写「移位除以 10」？
答案：Cortex-M0 没有硬件除法指令，`/10` 和 `%10` 会调用编译器的软件除法库（慢、体积大）。[chprintf.c:L101-L121](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chprintf.c#L101-L121) 用移位+加法+减法自造 \( \lfloor x/10 \rfloor \)，在频繁刷新的屏幕渲染路径上省下可观的周期与 flash。

**练习 2**：`shell_printf` 与 `plot_printf` 有何异同？
答案：同：都最终调 `chvprintf`，共享同一套占位符语义。异：`shell_printf`（[main.c:L280-L288](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L280-L288)）写 USB 流、给 shell 命令用；`plot_printf`（[chprintf.c:L577-L592](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chprintf.c#L577-L592)）写字符缓冲、给屏幕渲染用，且用只含 `put` 方法的迷你流对象省掉了 MemoryStream 的开销。

**练习 3**：想让 `threads` 命令出现在 `help` 里，最少改几处？
答案：一处——把 [main.c:L63](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L63) 的 `//#define ENABLE_THREADS_COMMAND` 取消注释重新编译即可（命令函数与表项都已存在，只是被宏裁掉了）。

### 4.4 CMD_WAIT_MUTEX：跨线程命令执行

#### 4.4.1 概念说明

shell 命令在 **main 线程**里被解析和调用，但命令要做的事往往触碰 **sweep 线程**的地盘：

- `scan`/`freq` 要改频点表并跑一次测量（写 `frequencies[]`、操作 si5351、填 `measured[]`）；
- `cal`/`recall` 要重放校准流程（同样要跑测量）；
- `touchcal`/`touchtest` 要独占触摸屏交互；
- `capture` 要独占 LCD 的 SPI 总线和 `spi_buffer` 回读显存。

若 main 线程直接执行这些操作，而 sweep 线程正同时在 `sweep()` 里操作 si5351、在 `draw_all()` 里刷 LCD，就会数据竞争：测出乱码、花屏、甚至配置丢失。

标准解法是互斥量，但本工程的 ChibiOS 配置把互斥量和信号量**都关了**（[chconf.h:L167](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chconf.h#L167)、[chconf.h:L186](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chconf.h#L186) 均为 `FALSE`，为的是省 RAM）。于是作者用了最朴素也最可靠的方案：**把命令「递交」给 sweep 线程去执行**——main 线程设置函数指针后就地等待，sweep 线程在自己的循环间隙看到指针非空，调用它、清空指针。命令与测量天然串行，无需任何锁。`CMD_WAIT_MUTEX` 这个名字的含义是「此命令要等到拿到 sweep 线程的独占权（事实上是借用 sweep 线程本身）才执行」。

#### 4.4.2 核心流程

```text
main 线程                                sweep 线程（Thread1）
──────────                               ───────────────────
解析出 scan 需要 WAIT_MUTEX
shell_function = cmd_scan  ──────────┐
shell_args/shell_nargs 已就绪        │  while(1):
do {                                 │    若 sweep 使能: sweep(true)   ← 本轮测量
  sleep(100ms)                       │    否则:        __WFI()        ← 睡到中断
} while (shell_function)             │    if (shell_function):        ← 看到递交
                                     │      shell_function(argc, &argv[1])  ← 执行命令
                                     │      shell_function = 0        ← 交还
                                     │      sleep(10ms); continue
shell_function 变 0，继续读下一行      │    ui_process() / plot / draw_all()
```

几个设计要点：

1. **单写者单读者**：`shell_function` 只有 main 线程置位、只有 sweep 线程清零，且两侧都有睡眠间隔，不需要原子操作以外的任何保护；`volatile`（[main.c:L59](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L59)）阻止编译器把它缓存进寄存器。
2. **参数的生命周期**：命令用的 `shell_args[]` 指向 main 线程的 `shell_line` 缓冲。安全的原因是 main 线程在 `do-while` 里**阻塞等待**，不会去读下一行、不会覆盖缓冲——「忙等」在这里反而成了正确性的一部分。
3. **等待粒度**：main 线程每 100ms 查一次；sweep 线程每轮循环（一趟 sweep 或一次唤醒）检查一次。命令延迟约为一趟 sweep 的时间（101 点典型为几十到几百毫秒量级，与带宽设置有关）。
4. **暂停扫描时为什么还能执行**：`pause` 后 sweep 线程走进 `__WFI()` 睡眠（[main.c:L117-L119](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L117-L119)），但 USB 主机每 1ms 发来 SOF 中断，任何中断都会唤醒 `__WFI`（u2-l5 讲过这个性质），线程醒来后重新检查 `shell_function`。所以「暂停扫描 → capture 截屏」依然可用。
5. **它不是通用 RPC**：一次只能挂一个命令（`shell_function` 是单变量），好在 main 线程会等它清零才处理下一行，天然串行。

#### 4.4.3 源码精读

**递交与等待（main 线程侧）**。[main.c:L2299-L2304](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2299-L2304)：命中命令且 `flags & CMD_WAIT_MUTEX` 时，先把函数指针赋给 `shell_function`，然后 `do { osalThreadSleepMilliseconds(100); } while (shell_function);`——睡 100ms、查一次，直到 sweep 线程清零。对照无标志分支 [main.c:L2305-L2307](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2305-L2307)：直接在 main 线程调用 `scp->sc_function(shell_nargs - 1, &shell_args[1])`。

**执行点（sweep 线程侧）**。[main.c:L112-L128](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L112-L128)：Thread1 每轮先做 sweep（或 WFI），随后第 121-126 行检查 `shell_function`——非空就调用、清零、睡 10ms（给 main 线程留出观察到清零的时间窗，也避免刚执行完立刻又开始测量）、`continue` 跳过本轮 UI/绘图。注意执行时机在 `sweep(true)` **之后**：递交的命令永远与测量串行。

**标志定义与注释**。[main.c:L2151-L2152](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2151-L2152)：`// Some commands can executed only in sweep thread, not in main cycle`——作者一句话说明白了这个机制的存在理由。

**为什么恰好是那 8 条**：对照 [main.c:L2157](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2157)（freq）、[L2165](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2165)（data）、[L2177](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2177)（scan）、[L2180-L2181](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2180-L2181)（touchcal/touchtest）、[L2184](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2184)（cal）、[L2186](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2186)（recall）、[L2190](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2190)（capture）——它们全部要动 sweep 线程正在用的共享资源。反例是 `pause`/`resume`（[main.c:L2182-L2183](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2182-L2183)，标志 0）：它们只做一次原子的位操作（[main.c:L151-L161](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L151-L161) 的 `sweep_mode &= ~SWEEP_ENABLE`），不需要延迟执行。

**一个完整例子：cmd_scan**。[main.c:L899-L940](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L899-L940)：参数校验（L904-L921）→ `set_frequencies()` 重排频点表（L923）→ 校准插值（L924-L925）→ `pause_sweep()` 停连续扫描（L926）→ `sweep(false)` 手动跑一趟完整测量（L927）→ 按掩码把 `frequencies[]` 和 `measured[]` 用 `shell_printf` 吐给主机（L929-L939）。频点表、si5351、测量缓冲全被触碰——这就是它必须挂 `CMD_WAIT_MUTEX` 的原因，也是 u5-l2 的 Python 上位机批量取数的协议基础。

#### 4.4.4 代码实践

1. **实践目标**：体感「延迟执行」的时序，并从并发角度分析去掉标志的后果。
2. **操作步骤**：
   - 终端敲 `pause`（立即返回），再敲 `capture`——观察 capture 需要等一下才有输出（递交 + sweep 线程唤醒 + 120 次显存读取）；
   - 恢复：`resume`；
   - 阅读实验：假设把 [main.c:L2177](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2177) 里 scan 的 `CMD_WAIT_MUTEX` 改成 0，分析会发生什么（不必真烧录）。
3. **需要观察的现象**：`pause` 后 `capture` 依然成功返回 153600 字节左右的数据（`ls -l` 保存的文件可核对，见 4.5.4）；改标志为 0 后（思想实验），main 线程的 `sweep(false)` 会与 Thread1 的 `sweep(true)` 并发操作同一硬件，可能产生交错配置的频点与错误数据。
4. **预期结果**：暂停扫描不阻碍延迟命令执行（SOF 中断唤醒 `__WFI`）。待本地验证。
5. 思想实验部分答案：并发 `sweep()` 没有互斥保护，测量结果与频点表可能错位；这正是该标志存在的意义。

#### 4.4.5 小练习与答案

**练习 1**：`shell_function` 为什么必须声明为 `volatile`？
答案：[main.c:L59](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L59)。两个线程都会访问它；没有 `volatile`，main 线程的 `while (shell_function)` 可能被优化成「读一次、死循环」，永远看不到 sweep 线程的清零。

**练习 2**：main 线程等待时为什么选择「睡 100ms 轮询」而不是自旋？
答案：[main.c:L2302-L2304](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2302-L2304)。自旋会在只有 48MHz 的 M0 上烧掉全部 CPU，还可能影响中断响应；命令延迟本来就有百毫秒量级（一趟 sweep），100ms 的轮询粒度足够。这是「忙等待 + 睡眠」在资源受限系统上的经典折中。

**练习 3**：如果一条延迟命令执行期间 USB 被拔掉，会发生什么？
答案：命令照常执行完（它在 sweep 线程里，与 USB 状态无关），`shell_function` 被清零；main 线程醒来后发现 `shell_function == 0` 退出等待，随后 `executeLine` 返回、主循环的 `do-while` 条件 `USB_ACTIVE` 不成立而退出，回到每秒轮询等待重连（[main.c:L2443-L2453](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2443-L2453)）。过程中 `streamPut` 写入已失效的流会被 SerialUSB 驱动安全丢弃。

### 4.5 capture 命令：读显存与二进制流输出

#### 4.5.1 概念说明

`capture` 把当前屏幕内容（320×240 像素）原样回读并发给主机，上位机（如 nanovna-saver）用它实现「截图」。难点有两个：

1. **LCD 是写多读少**的器件：正常路径是固件往 GRAM（显存）写 RGB565；回读要走另一条命令路径，且 ILI9341 返回的是 **18 位**（每像素 3 字节 R/G/B），必须转换。
2. **整屏 \( 320 \times 240 \times 3 + 1 \approx 230\,\text{KB} \)**，而共享 SPI 缓冲 `spi_buffer` 远没这么大——所以按每次 2 行（640 像素）分 120 批回读。这也再次解释了它为什么挂 `CMD_WAIT_MUTEX`：`spi_buffer` 和 SPI 总线是 sweep 线程绘图正在用的资源。

#### 4.5.2 核心流程

```text
cmd_capture（运行在 sweep 线程，经 CMD_WAIT_MUTEX 递交）
for y = 0, 2, 4, ... 238:            # 120 批
  ili9341_read_memory(0, y, 320, 2, 640, spi_buffer)
    ├─ 设置列/页地址窗口（2 行高）
    ├─ 发 MEMORY_READ 命令
    ├─ 清空 SPI 接收缓冲
    ├─ 启动双 DMA：dummy 发送（只产生时钟）+ 接收到 rgbbuf
    ├─ 等待 DMA 完成
    └─ 跳过 1 个 dummy 字节，把 18bit RGB → RGB565 写回 out（原地）
  for i in 0 .. 4*320:               # 2 行 × 320 像素 × 2 字节
    streamPut(shell_stream, *buf++)  # 原始字节流发给主机
总计输出 320×240×2 = 153600 字节二进制
```

缓冲需求：读取 `len` 个像素需要 \( 3 \times len + 1 \) 字节（每像素 3 字节 + 1 个 dummy），`len = 640` 时即 1921 字节——代码用一个编译期断言保护这个约束。

#### 4.5.3 源码精读

**cmd_capture 本体**。[main.c:L727-L745](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L727-L745)：120 批循环、每批读完把 `spi_buffer` 里的 1280 字节（`4*320`）逐字节 `streamPut`。注意输出是**裸二进制**，没有任何帧头/长度字段——主机端必须自己数够 153600 字节。编译期保护在 [main.c:L733-L735](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L733-L735)：`#if SPI_BUFFER_SIZE < (3*320 + 1)` → `#error`，若有人改小缓冲区，编译直接失败而不是运行时花屏。

**ili9341_read_memory（DMA 版）**。[ili9341.c:L473-L517](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L473-L517)（工程定义了 `__USE_DISPLAY_DMA__`，因此生效的是这份；L396 起还有一份非 DMA 版被宏隔离）：

- [ili9341.c:L479](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L479)：`data_size = len * 3 + 1`——18 位色 + 1 个 dummy 字节的由来；L474 的注释明确警告缓冲必须大于 `3*len + 1` 字节；
- [ili9341.c:L480-L486](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L480-L486)：设窗口、发 `ILI9341_MEMORY_READ`、排空接收 FIFO（丢弃残留）；
- [ili9341.c:L488-L502](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L488-L502)：**双 DMA**——SPI 是全双工，读必须有时钟，时钟只能靠「发送点什么」产生，于是用一个指向单字节 `dummy_tx` 的 DMA 通道专门产生时钟（不递增内存指针），另一个通道接收数据；CPU 在此期间只等待；
- [ili9341.c:L505-L516](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L505-L516)：先 `rgbbuf++` 跳过第一个 dummy 字节，然后每 3 字节 R/G/B 合成一个 RGB565 写回 `out` 数组——注意读缓冲和输出缓冲是**同一块内存**（`rgbbuf` 是 `out` 的字节视图），转换是从后往前原地进行的，不会覆盖未读数据。

#### 4.5.4 代码实践

1. **实践目标**：完整收下一帧屏幕数据，验证字节数与分批机制。
2. **操作步骤**：接好串口，在 shell 里执行（把设备换成你的）：
   ```bash
   # 示例代码（主机侧，非固件代码）
   stty -F /dev/ttyACM0 raw -echo 115200
   cat /dev/ttyACM0 > /tmp/screen.raw &      # 后台收流
   printf 'capture\r' > /dev/ttyACM0          # 发命令
   sleep 8; kill %1                            # 停止接收
   ls -l /tmp/screen.raw                       # 数字节
   ```
3. **需要观察的现象**：文件大小应接近 \( 320 \times 240 \times 2 = 153600 \) 字节（开头会混入 `ch> ` 提示符与回显的十几个字节）。
4. **预期结果**：字节数吻合即证明 120 批 × 1280 字节的搬运链路完整；进一步用 Python（`numpy.frombuffer(..., '<u2')` + PIL 的 Image.frombuffer，RGB565 需手动展开）可渲染成 PNG，具体字节序约定可在 u5-l2 与上位机源码对照后确认。待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么每批只读 2 行，而不是 1 行或 10 行？
答案：受 `spi_buffer` 容量约束：一批 `len` 像素需要 \( 3len+1 \) 字节缓冲（[ili9341.c:L474](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L474)），2 行 = 640 像素 = 1921 字节；[main.c:L733-L735](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L733-L735) 的编译期检查锁死了这个上限。批数越多与主机往返越少越好，于是在缓冲允许内取最大批量。

**练习 2**：接收 DMA 已经在收数据了，为什么还要一个「发送 dummy」的 DMA？
答案：SPI 全双工，时钟由主机端（这里是 STM32 的 TX 线）驱动；想读 N 字节就必须同时「发」N 字节制造 N 个时钟（[ili9341.c:L492-L495](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L492-L495)）。dummy 通道的源是单个字节且不开内存递增，等于反复发同一字节。这也是 u2-l3 I2S 采集之外另一处「DMA 换 CPU」的例子。

**练习 3**：capture 输出为什么不用 `shell_printf("%d", ...)` 逐像素打印？
答案：shell_printf 走格式化 + ASCII 文本，153600 字节的二进制若转十进制文本会膨胀数倍且无法还原精确字节；capture 用最底层的 `streamPut` 直吐原始字节（[main.c:L741-L743](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L741-L743)），主机按固定长度解析。文本协议（命令行）与二进制协议（数据流）在同一条虚拟串口上按上下文切换。

## 5. 综合实践：新增 uptime 命令

这是本讲的「毕业操作」：从零给 shell 加一条 `uptime` 命令——读取系统运行秒数并打印。它综合了命令函数写法（4.2/4.3）、命令表注册与 flags 选择（4.3/4.4）、以及 USB 串口验证（4.1）。

**任务**：仿照 `cmd_vbat` 在 main.c 中新增 `uptime`：读取 `chVTGetSystemTimeX()` 换算为秒并用 `shell_printf` 输出；注册进 `commands[]`（自己判断是否需要 `CMD_WAIT_MUTEX`）；烧录后通过 USB 串口终端敲 `uptime` 与 `help` 验证。

### 步骤 1：写命令函数

仿照最简单的 [cmd_vbat（main.c:L2031-L2036）](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2031-L2036)，在它后面加（放在命令表 L2153 之前即可）：

```c
// 示例代码：新增 uptime 命令（仿照 cmd_vbat）
VNA_SHELL_FUNCTION(cmd_uptime)
{
  (void)argc;
  (void)argv;
  // CH_CFG_ST_FREQUENCY = 10000（chconf.h L51），1 tick = 100µs
  uint32_t sec = chVTGetSystemTimeX() / CH_CFG_ST_FREQUENCY;
  shell_printf("uptime: %u s\r\n", sec);
}
```

要点：

- `VNA_SHELL_FUNCTION` 宏会展开成 `static void cmd_uptime(int argc, char *argv[])`（[main.c:L51-L53](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L51-L53)），所以不需要再写函数头。
- `chVTGetSystemTimeX()` 是 ChibiOS 的非阻塞系统时间读取，单位是 tick。本工程 [chconf.h:L51](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chconf.h#L51) 设 \( f_{ST} = 10\,\text{kHz} \)，即 \( 1\,\text{tick} = 100\,\mu s \)，秒数 = tick 数 / 10000。
- 用 `%u` 而不是 `%d`：tick 计数与秒数都是无符号数；裁剪版 chprintf 的整数全部按 32 位取参（[chprintf.c:L400-L413](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chprintf.c#L400-L413)），`uint32_t` 正好匹配。
- 附带知识：tick 计数是 32 位，\( 2^{32} / 10^{4} \approx 429497\,s \approx 119.3 \) 小时后回绕——对一台手持仪器足够了。

### 步骤 2：注册命令并选好 flags

在 [commands[] 表（main.c:L2153-L2208）](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2153-L2208)里加一行，比如放在 `vbat` 旁边：

```c
// 示例代码：命令表新条目
    {"vbat"       , cmd_vbat        , 0},
    {"uptime"     , cmd_uptime      , 0},   // ← 新增
```

**flags 选 0（不需要 CMD_WAIT_MUTEX）的理由**：这条命令只读系统 tick 计数器再打印，不触碰 `measured[]`、`frequencies[]`、si5351、LCD 中任何一个 sweep 线程正在使用的资源——与 `vbat`（只读 ADC 结果）、`version`（打印常量）同类。对照 4.4 的判据：**凡是会让 main 线程与 sweep 线程同时操作同一资源的命令才需要标志**。选错了会怎样？挂上 `CMD_WAIT_MUTEX` 也能工作，只是平白多等一趟 sweep 的时间。

### 步骤 3：编译与烧录

按 u1-l2 的流程（工具链：`make`；烧录：`make flash` 走 dfu-util，见 [Makefile:L228-L229](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile#L228-L229)，或用 ST-Link/OpenOCD）。编译期就会帮你检查两处低级错误：忘了写函数（链接错 undefined reference to `cmd_uptime`）、表项放在 `#ifdef` 外导致格式错误（编译错）。

### 步骤 4：验证

1. USB 连接电脑，打开 `/dev/ttyACM0`（115200 8N1，CDC 不真的使用波特率）；
2. 敲 `help`——应出现 `uptime`（cmd_help 自动收录，见 4.3.3）；
3. 敲 `uptime`——应输出 `uptime: N s`；
4. 等 10 秒再敲一次——两次读数差应约为 10（允许 ±1，粒度是 1 秒）；
5. 顺手验证 flags 的选择：敲 `pause` 再敲 `uptime`，应与平时一样**立即**返回（若当时误选了 `CMD_WAIT_MUTEX`，会感觉到约百毫秒的额外延迟）。

以上运行现象均**待本地验证**。

### 常见坑

- 函数定义放在了 `commands[]` **之后**且忘了前置声明——照抄 L2141 对 `cmd_help` 的做法补一行 `VNAShellCommand` 前的声明即可。
- 用 `shell_printf` 之外的方式输出（如直接 `chprintf`）——本工程的 `chprintf()` 已被 `#if 0` 关闭（4.3.1），只能走 `shell_printf`。
- 命令行缓冲只有 48 字节：别设计 `uptime --format=...` 这种长参数风格。

## 6. 本讲小结

- **一条虚拟串口的诞生**：`usbcfg.c` 用静态描述符（VID 0x0483/PID 0x5740、双接口 CDC、EP1 bulk 64B + EP2 interrupt）+ 四个回调把 STM32 变成 `/dev/ttyACM0`；`SDU1` 以 `BaseSequentialStream` 的面目被 shell 使用，shell 逻辑与 USB 完全解耦。
- **90 行的自研 shell**：`VNAShell_readLine` 单字节读 + 回显 + 退格三连击（`0x08 0x20 0x08`）；`VNAShell_executeLine` 用「分隔符原地改 NUL」零拷贝地切出 argv，再线性查 `commands[]` 表分发——数据驱动，加命令只需函数 + 表项各一行。
- **chprintf 的裁剪哲学**：砍掉长度修饰和两个入口函数省 flash，新增 `%q`（移位除法的 SI 前缀频率格式化）与 `%F`（自动前缀浮点）服务 VNA 显示；`shell_printf` 走 USB，`plot_printf` 走内存缓冲，同一个 `chvprintf` 内核两用。
- **CMD_WAIT_MUTEX 是「借线程」而非加锁**：互斥量/信号量在 chconf.h 里被整体关闭，于是 main 线程递交 `volatile` 函数指针并 100ms 轮询，sweep 线程在测量间隙执行并清零——命令与测量天然串行，`__WFI` 的中断唤醒保证暂停扫描时命令照常服务。
- **文本协议与二进制协议共用一条串口**：命令行走可读文本（`%s?`、`too many arguments`），`capture` 用 `streamPut` 直吐 153600 字节 RGB565 裸流，120 批 DMA 回读依赖 `spi_buffer` 的 `3*len+1` 字节约束（编译期 `#error` 保护）。

## 7. 下一步学习建议

- **下一讲（u5-l2）**：Python 上位机——本讲的直接消费者。`scan` 的 `outmask` 参数（[main.c:L929-L939](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L929-L939)）怎么被解析成 numpy 数组、`capture` 的 153600 字节怎么变成 PNG、`%q` 格式的频率在主机侧如何还原成数值，都将在那一讲展开。
- **u5-l3（RTOS 资源约束）** 会继续深挖本讲反复出现的主题：为什么关掉互斥量、`#pragma pack`、`#if 0` 裁函数这些「抠门」手法能在一个 16KB RAM / 128KB flash 的芯片上塞下整个 VNA。
- **动手向**：综合实践的 `uptime` 只用了 `%u`；试着把 4.3 的 `%q` 用起来（比如打印「运行 xx.x ks」这类带前缀的时间），对比输出，体会定制格式符的便利。
- **源码向**：把 [usbcfg.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/usbcfg.c) 与 ChibiOS 子模块里的 `os/hal/lib/streams`（SerialUSB 驱动 `sduDataReceived`/`sduDataTransmitted` 的队列实现）对照阅读，弄清「端点上的一个包」如何变成 shell 读到的「一个字节」；顺带用 4.3.4 的 map 文件方法确认你本地固件链接的 chprintf 版本。
