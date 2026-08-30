# 与接收机对话：USB CDC Shell 与 Python 控制工具

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 CentSDR 的控制通道是如何搭建的：USB 如何枚举成一个"虚拟串口"，ChibiOS shell 线程如何跑在这个虚拟串口之上。
2. 熟练使用 `tune` / `mode` / `gain` / `agc` / `channel` / `show` / `stat` / `data` 等常用命令，知道每个命令的参数范围和它背后真正调用的函数。
3. 理解 `ShellCommand` 表驱动注册机制：为什么加一条命令只需要"写一个函数 + 在表里加一行"。
4. 读懂 `python/centsdr.py` 如何把文本命令封装成 Python 方法和命令行工具，特别是它如何用 `ch>` 提示符来判断"一条命令的输出结束了"。
5. 独立完成：给 shell 新增一条 `hello` 命令，并用 Python 工具验证。

承接上一讲（u1-l3）：我们已经知道 `main()` 在初始化序列的什么位置启动了 USB 和 shell。本讲把这条"人机对话"链路从硬件层到脚本层完整拆开。

## 2. 前置知识

### 2.1 USB CDC 与虚拟串口

- **CDC**（Communications Device Class）是 USB 官方定义的"通信设备类"，其 **ACM**（Abstract Control Model）子类就是"USB 转串口"的标准做法。
- 设备插入后，操作系统识别出 CDC 设备并加载内置驱动：Linux 下是 `cdc_acm`，生成 `/dev/ttyACM*` 设备节点；macOS 下生成 `/dev/cu.usbmodem*`。
- 应用程序之后就像操作普通串口一样 `read`/`write` 字节流。**波特率、校验位等参数对 USB CDC 没有意义**——数据实际走的是 USB 批量传输，串口参数只是被"礼貌性地忽略"。

### 2.2 几个 USB 术语

| 术语 | 含义 | 在本讲中的角色 |
|------|------|----------------|
| 描述符 (descriptor) | 设备向主机自我介绍的一串结构化数据 | `usbcfg.c` 里那几张 `const uint8_t` 数组 |
| 端点 (endpoint, EP) | USB 数据传输的"端口"，有方向（IN=设备发往主机，OUT=主机发往设备） | CDC 用 1 对批量端点传数据 + 1 个中断端点传通知 |
| VID / PID | 厂商 ID / 产品 ID，设备的第一张"名片" | `0x0483`（ST）/ `0x5740` |
| 批量传输 (bulk) | 有错误校验、无固定带宽保证的传输类型 | shell 命令与输出都走它 |

### 2.3 Shell 是什么

Shell 就是"读一行 → 查表 → 执行 → 打提示符"的循环。ChibiOS 在 `os/various/shell/`（子模块内，由 [Makefile:105](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L105) 的 `shell.mk` 引入构建）提供了一个通用 shell：行编辑、按空格切分参数、在应用提供的命令表里查找分发。CentSDR 自己一个字的 shell 代码都没写，只提供了命令表。

### 2.4 串口终端工具

想手动敲命令，需要一个终端模拟器，例如 `screen /dev/ttyACM0`、`picocom /dev/ttyACM0` 或 `minicom`。连上后按回车，出现 `ch>` 提示符即表示 shell 就绪。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| `usbcfg.c` | USB CDC 全部配置 | 设备/配置/字符串描述符、端点配置、事件回调 |
| `usbcfg.h` | 对外导出 | `usbcfg`、`serusbcfg`、`SDU1` 三个符号 |
| `main.c` | 应用主体 | 27 条 `cmd_*` 命令函数、`commands` 注册表、shell 线程孵化循环 |
| `nanosdr.h` | 全局共享头 | 命令所操作的数据结构 `uistat_t`、`modulation_t` |
| `python/centsdr.py` | Python 控制工具 | 模块类 `CentSDR` + 命令行入口 `run_as_command()` |
| `python/README.md` | 工具使用说明（日文） | 设备指定方法、缓冲区编号含义 |
| `ChibiOS/os/various/shell/` | 通用 shell（子模块，本环境未检出） | 行读取与命令分发；具体行号待确认 |

> 提示：ChibiOS 是 git 子模块（指向 `edy5555/ChibiOS`，见 [.gitmodules:1-3](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/.gitmodules#L1-L3)），当前分析环境中目录为空，因此涉及 shell 内部实现的行号本文标注"待确认"，只依据可观察行为与调用接口描述。

## 4. 核心概念与源码讲解

### 4.1 USB CDC：把 USB 口变成串口

#### 4.1.1 概念说明

CentSDR 没有把物理 UART 引出，**USB 口就是它唯一的文本控制通道**。要让它"说话"，需要三层配合：

1. **USB 硬件层**：STM32F303 的 USB 设备控制器，由 ChibiOS 的 `USBDriver`（实例名 `USBD1`）驱动。
2. **CDC 协议层**：向主机声明"我是一个虚拟串口"，由 `usbcfg.c` 里的描述符和 `SerialUSBDriver`（实例名 `SDU1`）完成。
3. **流抽象层**：`SDU1` 被包装成 `BaseSequentialStream*`，于是 ChibiOS shell、`chprintf` 都能把它当成普通串口写——同一份命令代码将来换到物理 UART 上也能跑。

`usbcfg.h` 把这三样东西导出给 `main.c`：

```c
extern const USBConfig usbcfg;
extern SerialUSBConfig serusbcfg;
extern SerialUSBDriver SDU1;
```

见 [usbcfg.h:16-23](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/usbcfg.h#L16-L23)，这三行就是本模块的全部公共接口。

#### 4.1.2 核心流程

从插上 USB 到出现 `/dev/ttyACM0`，再到 shell 可用，完整时序如下：

```
设备上电
  │
  ▼
main(): sduObjectInit(&SDU1) → sduStart(&SDU1, &serusbcfg)     # 初始化 CDC 驱动
  │
  ▼
main(): usbDisconnectBus → usbStart(usbp, &usbcfg) → usbConnectBus
  │        # 先断开再连接 D+ 上拉，强制主机重新枚举
  ▼
主机枚举：
  ① 读设备描述符        → 得知 VID=0x0483 PID=0x5740，CDC 类
  ② 读配置描述符树      → 得知 2 个接口：CDC 控制 + CDC 数据
  ③ SET_CONFIGURATION  → 触发 usb_event(USB_EVENT_CONFIGURED)
                          → usbInitEndpointI() 激活 EP1(批量)/EP2(中断)
                          → sduConfigureHookI() 复位 CDC 子系统
  ④ 主机加载 cdc-acm 驱动 → 生成 /dev/ttyACM*
  ▼
main() 主循环检测到 USB_ACTIVE → 创建 shell 线程 → shell 在 SDU1 上
打印 "ch> "，等待输入
```

之后你在电脑上向 `/dev/ttyACM0` 写的每个字节，都经 EP1（OUT 方向）进入设备；设备的应答经 EP1（IN 方向）回来。

#### 4.1.3 源码精读

**（1）驱动实例与端点号**。[usbcfg.c:21](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/usbcfg.c#L21) 定义全局 `SerialUSBDriver SDU1`；[usbcfg.c:26-28](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/usbcfg.c#L26-L28) 用宏约定：EP1 承担数据收发（IN/OUT 各一个 64 字节批量端点），EP2 承担 CDC 中断通知（8 字节，仅 IN）。

**（2）设备描述符——第一张名片**。[usbcfg.c:33-46](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/usbcfg.c#L33-L46) 声明这是一个 USB 1.1、CDC 类（`bDeviceClass=0x02`）设备，`idVendor=0x0483`（ST 官方 VID）、`idProduct=0x5740`（ST 虚拟 COM 口惯例 PID），最大包 64 字节。Linux 的 `cdc_acm` 驱动正是靠类信息（而非 VID/PID 白名单）自动绑定它。

**（3）配置描述符——两个接口的"族谱"**。[usbcfg.c:57-131](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/usbcfg.c#L57-L131) 这 67 字节是本模块最长的表，结构为：

- 接口 0（通信类）：挂 4 个 CDC 功能描述符（Header / Call Management / ACM / Union），并声明中断端点 EP2 IN；
- 接口 1（数据类）：声明一对批量端点 EP1 OUT + EP1 IN（[usbcfg.c:122-130](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/usbcfg.c#L122-L130)）。

`Union` 功能描述符（[usbcfg.c:98-104](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/usbcfg.c#L98-L104)）告诉主机"接口 0 是主、接口 1 是它的数据通道"，这是 CDC 枚举的关键一环。

**（4）端点配置结构**。[usbcfg.c:230-241](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/usbcfg.c#L230-L241) 的 `ep1config` 把 EP1 配成批量、收发各 64 字节，回调直接挂 ChibiOS SDU 驱动自带的 `sduDataTransmitted` / `sduDataReceived`；[usbcfg.c:251-262](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/usbcfg.c#L251-L262) 的 `ep2config` 把 EP2 配成中断端点。**注意这两张表里没有任何应用代码——数据到了端点回调就完全交给 SerialUSB 驱动缓冲成字节流。**

**（5）事件回调**。[usbcfg.c:267-303](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/usbcfg.c#L267-L303) 的 `usb_event()` 处理主机事件，最关键的是 `USB_EVENT_CONFIGURED` 分支：在中断上下文里用 I 类函数激活两个端点并调用 `sduConfigureHookI` 复位 CDC 状态（[usbcfg.c:275-288](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/usbcfg.c#L275-L288)）。[usbcfg.c:308-315](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/usbcfg.c#L308-L315) 的 `sof_handler()` 在每帧 SOF（Start of Frame）中断里调用 `sduSOFHookI`，用于 USB 挂起时的超时计时。

**（6）两份配置的组装**。[usbcfg.c:320-325](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/usbcfg.c#L320-L325) 的 `usbcfg`（事件回调 + 描述符获取 + `sduRequestsHook` 处理 CDC 类请求）和 [usbcfg.c:330-335](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/usbcfg.c#L330-L335) 的 `serusbcfg`（把 `SDU1` 绑到 `USBD1` 和三个端点号上）。

**（7）main() 侧的启动顺序**（上一讲已走读，这里只看 USB 段）：

- [main.c:990-991](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L990-L991)：`sduObjectInit(&SDU1); sduStart(&SDU1, &serusbcfg);`
- [main.c:998-1001](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L998-L1001)：先 `usbDisconnectBus` 把 D+ 上拉断开、`usbStart` 装载配置、再 `usbConnectBus` 重新连上。这个"先断后连"的顺序保证复位后主机一定会重新枚举，避免电脑缓存了旧的设备状态。

#### 4.1.4 代码实践

**实践 A（需要硬件，待本地验证）**：观察一次真实枚举。

1. 实践目标：把 `usbcfg.c` 里的描述符字段和操作系统看到的信息对上号。
2. 操作步骤：
   ```bash
   # 插入设备后
   dmesg | tail -20          # 应看到 cdc_acm 绑定并生成 /dev/ttyACMn
   lsusb | grep 0483:5740    # 确认 VID:PID
   lsusb -d 0483:5740 -v | grep -A2 bDeviceClass
   ```
3. 需要观察的现象：`dmesg` 中出现 `cdc_acm` 字样和 `ttyACM` 设备名；`lsusb -v` 输出的 `bDeviceClass` 为 2（CDC）。
4. 预期结果：与 [usbcfg.c:33-46](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/usbcfg.c#L33-L46) 中声明的值一一对应。

**实践 B（无硬件可做）**：纯源码问答。只读 `usbcfg.c`，回答：

1. 数据端点每方向一次最多传多少字节？（看 `ep1config` 的两个 `0x0040`）
2. 设备自供电还是总线供电？最大电流多少？（看配置描述符的 `bmAttributes=0xC0` 与 `bMaxPower=50`，单位 2mA）
3. 为什么产品字符串显示为 "ChibiOS/RT Virtual COM Port" 而不是 "CentSDR"？（看 [usbcfg.c:164-171](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/usbcfg.c#L164-L171) 的 `vcom_string2`——这是从 ChibiOS 例程继承下来的默认字符串）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `usb_event()` 里要用 `chSysLockFromISR()` / `usbInitEndpointI()` 这类带 I 后缀的函数？

**答案**：`USB_EVENT_CONFIGURED` 回调运行在中断服务程序上下文，此时不能使用会睡眠的普通内核 API。I 类（I-Class）函数是 ChibiOS 专门为中断上下文准备的"不允许睡眠、在锁内完成"版本，此处用 `chSysLockFromISR()` 进入临界区后调用它们是 ChibiOS 的标准写法。

**练习 2**：串口终端里设置的波特率 115200 会影响 CentSDR 的 shell 吗？

**答案**：不会。USB CDC 的数据走批量传输，波特率参数不参与实际传输；`serusbcfg` 里也没有任何波特率字段。终端里设多少都能正常通信。

**练习 3**：`get_descriptor()`（[usbcfg.c:198-215](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/usbcfg.c#L198-L215)）支持哪几类描述符？字符串描述符最多有几个？

**答案**：设备描述符、配置描述符、字符串描述符三类；字符串索引 `dindex < 4`，即最多 4 个（语言 ID、厂商、产品、序列号——序列号内容是 ChibiOS 内核版本号拼出来的，见 [usbcfg.c:176-182](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/usbcfg.c#L176-L182)）。

### 4.2 Shell 命令机制：一张表驱动 27 条命令

#### 4.2.1 概念说明

ChibiOS shell 的设计哲学是**框架与命令分离**：框架负责读行、切词、分发、打印提示符；应用只需提供一张"命令名 → 函数指针"的表。CentSDR 的这张表就是 `main.c` 里的 `commands`，共 **27 条命令 + 一个 NULL 结尾哨兵**。

每条命令都是一个统一签名的 C 函数：

```c
static void cmd_xxx(BaseSequentialStream *chp, int argc, char *argv[]);
```

- `chp`：输出流。shell 把 `SDU1` 强转成 `BaseSequentialStream*` 传进来，命令用 `chprintf(chp, ...)` 打印——`chprintf` 是 ChibiOS 的 printf 精简版。
- `argc`/`argv`：命令名之后的参数（不含命令名本身），与 C 的 main 参数约定一致。

这个签名是**唯一的扩展契约**：只要你写出这个签名的函数并注册进表，shell 就能调用它，不需要改框架任何代码。

#### 4.2.2 核心流程

shell 线程的生命周期与一条命令的执行路径：

```
main 线程末尾的孵化循环（每秒检查一次）:
    if (SDU1.config->usbp->state == USB_ACTIVE):
        从堆上创建 shell 线程（栈 2KB，优先级 NORMALPRIO+1，入口 shellThread）
        chThdWait() 阻塞等待 shell 线程退出（USB 断开时退出）
    else:
        睡 1 秒再查

shell 线程内部（ChibiOS 实现，路径 os/various/shell/shell.c，行号待确认）:
    loop:
        打印提示符 "ch> "
        从流读一行（带回显）
        按空白切分成 token
        token[0] 与 commands[] 中各项的 sc_name 逐个 strcmp
        命中 → 调用 sc_function(chp, 剩余token数, 剩余token数组)
        回到 loop
```

注意一个细节：shell 线程**不是常驻的**。USB 断开后 `shellThread` 退出，`chThdWait` 收尸，main 循环继续轮询，等 USB 再次 ACTIVE 时重新孵化。这就是为什么拔插 USB 后 shell 总是干净的初始状态。

#### 4.2.3 源码精读

**（1）命令注册表**。[main.c:874-904](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L874-L904)：

```c
static const ShellCommand commands[] =
{
    { "reset", cmd_reset },
    { "freq", cmd_freq },
    { "tune", cmd_tune },
    ...
    { "lcd", cmd_lcd },
    { NULL, NULL }          /* 哨兵：shell 靠它知道表到这里结束 */
};
```

这张表就是全部注册机制——没有宏、没有注册函数、没有初始化代码。加命令 = 写函数 + 在这里加一行。整个表是 `static const`，被放进 Flash，不占 RAM。

**（2）shell 配置与栈大小**。[main.c:934-940](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L934-L940)：

```c
#define SHELL_WA_SIZE THD_WORKING_AREA_SIZE(2048)

static const ShellConfig shell_cfg1 =
{
    (BaseSequentialStream *)&SDU1,   /* shell 的输入输出流 */
    commands                          /* 命令表 */
};
```

`ShellConfig` 只有两项：流和命令表。`(BaseSequentialStream *)&SDU1` 这个强转正是 4.1 节说的"流抽象层"落地点。

**（3）孵化循环**。[main.c:1058-1066](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L1058-L1066)：`main` 线程在所有初始化完成后的唯一工作，就是上面流程图中的轮询 + 孵化 + 等待。shell 线程优先级 `NORMALPRIO + 1`（[main.c:1060-1062](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L1060-L1062)），略高于 Thread1/Thread2，人机交互响应优先。

**（4）一个标准命令的样子**。以 `cmd_show` 为例（[main.c:728-753](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L728-L753)）：先检查 `argc`，参数不合法就打印 usage 并 return；合法则读全局状态、`chprintf` 输出。27 条命令全部遵循"校验参数 → 调用底层函数 → 更新 uistat → disp_update() 刷新屏幕"的套路。

#### 4.2.4 代码实践

**实践目标**：在不改一行代码的前提下，通过阅读建立"命令 → 底层动作"的映射表。

**操作步骤**：

1. 打开 [main.c:874-904](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L874-L904) 的 `commands` 表，数一遍命令数量（应为 27）。
2. 把 27 条命令分成三类抄成清单：
   - **设置类**（改变接收机状态，如 `tune`/`mode`/`gain`）；
   - **查询类**（只读输出，如 `show`/`stat`/`power`/`data`）;
   - **维护类**（`save`/`clearconfig`/`reset`/`lcd` 等）。
3. 对每条设置类命令，跳转到它的 `cmd_*` 函数，记下它最终调用的那个底层函数（如 `cmd_tune` → `set_tune()` → `si5351_set_frequency()`）。

**需要观察的现象**：分类完成后你会发现设置类命令几乎都以下面三步收尾——更新 `uistat`、可选地调用 `disp_update()`、可选地落盘 `config_save()`。

**预期结果**：得到一张三列清单（命令名 / 底层函数 / 是否更新 uistat）。它是 4.3 节的速查表雏形，也是单元五"二次开发"时判断"在哪一层插入自己的逻辑"的基础。

**待本地验证**部分：如果手头有固件和串口终端，可连接后只按回车，确认出现 `ch>` 提示符，再敲一条不存在的命令名，观察 shell 对未知命令的报错格式（该行为由 ChibiOS shell 决定，具体输出待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `commands` 表要 `{ NULL, NULL }` 结尾？去掉会怎样？

**答案**：shell 框架靠遍历表项直到名字为 NULL 来确定表长（C 语言数组不携带长度信息）。去掉哨兵后 shell 会越过数组边界继续读内存，轻则匹配到垃圾命令，重则 HardFault。这是 C 里"表驱动 + 哨兵结尾"的惯用法——与 4.3 节将看到的 TLV320AIC3204 寄存器配置表是同一手法。

**练习 2**：shell 线程的栈为什么从堆上分配（`chThdCreateFromHeap`），而 Thread1/Thread2 用静态栈（`THD_WORKING_AREA`）？

**答案**：Thread1/Thread2 是常驻线程，生命周期与固件相同，静态分配最省事且无碎片风险。shell 线程随 USB 插拔创建销毁，且只有 USB 连着时才需要那 2KB 内存，从堆分配可以在拔线后归还内存。这也解释了 128KB Flash / 40KB RAM 的小内存系统里"按需分配"的价值。

**练习 3**：如果我想要一个不需要 USB、跑在物理 UART 上的 shell，`main.c` 要改哪里？

**答案**：只需把 `shell_cfg1` 的第一个成员从 `(BaseSequentialStream *)&SDU1` 换成物理串口驱动（如 `&SD1`，需先 `sdStart`），命令表和所有 `cmd_*` 函数一行都不用动——因为命令只依赖 `BaseSequentialStream*` 抽象。这正是流抽象带来的可移植性。

### 4.3 常用命令精读：调谐、模式、增益与状态查询

#### 4.3.1 概念说明

这一节把最常用的命令按"操作对象"分组精读。它们操作的核心数据结构是全局 `uistat_t`（[nanosdr.h:256-274](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L256-L274)）——记录整机当前状态（频率、模式、增益、音量、AGC、显示档位等），以及 `mod_table`（解调模式表，[main.c:165-177](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L165-L177)）——把每种调制映射为"解调函数指针 + 频偏 + 采样率 + 名字"。

#### 4.3.2 核心流程

一条典型设置命令（`mode`）的完整链路：

```
mode fm
  └─ cmd_mode(): strncmp 前缀匹配 "fm"
       └─ set_modulation(MOD_FM)
            ├─ uistat.fs = mod_table[MOD_FM].fs (=192)   # 该模式的标准采样率
            ├─ set_fs(192)                               # 切换 I2S/编解码器采样率
            ├─ signal_process = mod_table[MOD_FM].demod_func  # 换解调函数指针
            ├─ mode_freq_offset = mod_table[MOD_FM].freq_offset (=0)
            ├─ uistat.modulation = MOD_FM                # 记入全局状态
            └─ disp_update()                             # 请求屏幕刷新
```

关键洞察：**解调模式的热切换就是换一个函数指针**。I2S 回调每 5ms 调一次 `(*signal_process)(p, q, n)`（[main.c:258-276](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L258-L276)），`mode` 命令换掉这个指针，下一拍起就走新算法，不需要重启任何数据流。这个机制在单元三会反复用到。

#### 4.3.3 源码精读

**（1）`tune` 与 `freq`：一对容易混淆的命令**。

- [main.c:83-96](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L83-L96) `cmd_tune`：参数是**你想接收的信号频率**。它调用 `set_tune(freq)`，后者（[main.c:196-201](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L196-L201)）先减去当前模式的频偏 `mode_freq_offset`（AM/CW 为 `AM_FREQ_OFFSET`，SSB/FM 为 0），再乘 4 交给 SI5351（四倍频正交本振）。随后更新 `uistat.freq`、把 UI 档位切到 `FREQ`、请求刷屏。这是**用户级**命令。
- [main.c:72-81](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L72-L81) `cmd_freq`：参数是**SI5351 的实际输出频率**，不经偏移、不乘 4、不更新 uistat。这是**工程级/调试级**命令，用来直接驱读本振。日常收台用 `tune`，做硬件实验时才用 `freq`。

**（2）`mode`：前缀匹配与判断顺序的陷阱**。[main.c:657-679](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L657-L679)：

```c
if (strncmp(cmd, "am", 1) == 0)        /* 只比第 1 个字符：'a' 即命中 am */
...
else if (strncmp(cmd, "fms", 3) == 0)   /* 必须先于 fm 判断 */
    set_modulation(MOD_FM_STEREO);
else if (strncmp(cmd, "fm", 1) == 0)    /* 否则 "fms" 会先被 'f' 匹配成 fm */
    set_modulation(MOD_FM);
```

两个值得记住的细节：多数分支只比较 1 个字符（`mode a` 也能切 AM）；`fms` 的 3 字符比较**必须排在** `fm` 的 1 字符比较之前，否则立体声永远选不到。写自己的命令时要么用 `strcmp` 全匹配，要么想清楚前缀长度和排列顺序。

**（3）`gain` / `volume` / `agc`：落到编解码器的三组增益**。

- [main.c:481-504](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L481-L504) `cmd_gain`：1~3 个参数。第一参数是 PGA 增益（0-95，0.5dB 步进），可叠加数字增益（-24~40）和左右声道微调，最后写 `uistat.rfgain` 并刷屏。
- [main.c:542-554](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L542-L554) `cmd_volume`：耳机音量（-7~29）。
- [main.c:580-628](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L580-L628) `cmd_agc`：一个"命令里的命令"，子命令 `manual/slow/mid/fast/enable/disable/level/hysteresis/attack/decay/maxgain`。快捷档位由 [main.c:630-655](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L630-L655) 的 `set_agc_mode()` 实现——本质是改写 `config.agc` 的 decay/decay_scale 再整体下发（如 SLOW = decay 31 / scale 4，FAST = 0/0）。这些最终都变成对 TLV320AIC3204 的 I2C 寄存器写入，细节留到 u2-l2。

**（4）`show` 与 `stat`：两个查询命令的分工**。[main.c:728-753](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L728-L753) `cmd_show` 查询**用户设置**（频率/音量/模式/增益/信道/AGC，直接读 `uistat`）；[main.c:423-459](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L423-L459) `cmd_stat` 查询**运行测量值**（RMS/最小最大值、回调计数、DSP 负载、fps、ADC 溢出、AGC 实际增益、立体声 PLL 内部状态、温度/电池/基准电压）。其中 DSP 负载一行值得注意：

\[ \text{load} = \frac{\text{busy\_cycles}}{\text{interval\_cycles}} \times 100\% \]

`busy_cycles` 是 I2S 回调里执行解调耗费的周期数、`interval_cycles` 是两次回调的间隔周期数（[main.c:269-272](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L269-L272) 由 DWT 周期计数器测得）。load 接近 100% 意味着解调快算不过来了——这个指标在 u5-l1 会深入使用。

**（5）`data`：把内部缓冲区十六进制转储出来**。[main.c:315-349](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L315-L349)：参数选择缓冲区，每行 16 个值、`%04x` 格式打印 480 个 int16：

```c
switch (atoi(argv[0])) {
case 0: break;               /* rx_buffer：I2S 采集的交织 IQ 原始数据 */
case 1: buf = tx_buffer;     break;   /* 送往 DAC 的音频输出 */
case 2: buf = buffer[0];     break;   /* 解调中间缓冲 1 */
case 3: buf = buffer2[0];    break;   /* 解调中间缓冲 2 */
}
```

注意这个编号与 `buffers_table`（[main.c:102-107](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L102-L107)）的下标顺序**不一致**（表里顺序是 rx、buffer、buffer2、tx）——查表时别搞混。`data` 是"用电脑当示波器"的出口，Python 侧靠它抓波形（见 4.4）。转储期间解调仍在跑（代码里 `i2sStopExchange` 被注释掉了），所以抓的是"活"数据，但 hex 输出很慢，抓一次约 480 个值 × 6 字符。

**（6）`channel` 与 `save`：信道与持久化**。[main.c:755-795](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L755-L795) `cmd_channel` 支持三形态：数字（切换到 n 号信道并 `recall_channel`）、`save [n]`（把当前频率/模式写入信道）、`list`（列出所有非空信道）。[main.c:819-828](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L819-L828) `cmd_save` 把整个 `uistat` 快照进 `config` 并调 `config_save()` 落 Flash——机制在 u4-l5 展开。

**（7）速查表**。

| 命令 | 参数 | 行为 | 主要落点 |
|------|------|------|----------|
| `tune` | 频率 Hz | 设接收频率（含模式频偏补偿），更新 uistat | `set_tune()` → `si5351_set_frequency()` |
| `freq` | 频率 Hz | 直接设 SI5351 输出频率（调试用） | `si5351_set_frequency()` |
| `mode` | cw/lsb/usb/am/fm/fms | 换解调函数指针 + 采样率 | `set_modulation()` → `mod_table` |
| `gain` | 0-95 [-24~40] [adj] | PGA/数字增益 | `tlv320aic3204_set_gain()` |
| `volume` | -7~29 | 耳机音量 | `tlv320aic3204_set_volume()` |
| `agc` | 子命令若干 | AGC 模式与参数 | `set_agc_mode()` / `tlv320aic3204_agc_config()` |
| `fs` | 48/96/192 | 强制采样率 | `set_fs()` |
| `cwtone` | Hz（空参=读回） | CW 侧音频率 | `update_cwtone()` |
| `channel` | n / save / list | 信道切换、保存、列表 | `recall_channel()` / `config` |
| `show` | [all/tune/volume/mode/gain/channel/agc] | 查询用户设置 | 读 `uistat` |
| `stat` | 无 | 查询运行统计（rms/load/fps/overflow…） | 读 `stat` |
| `power` | 无 | 查询功率（dBm，定点 8.8） | 读 `measured_power_dbm` |
| `data` | [0-3] | 十六进制转储内部缓冲区 | `rx_buffer`/`tx_buffer`/`buffer`/`buffer2` |
| `save` | 无 | 保存配置到 Flash | `config_save()` |

#### 4.3.4 代码实践

**实践目标**：不改代码，用"推理 + 源码核对"的方式吃透命令语义；有硬件时再用真机验证。

**操作步骤**：

1. 先在纸上作答：依次执行下面序列后，`show` 的输出是什么？
   ```
   tune 7100000
   mode lsb
   gain 30
   agc slow
   ```
2. 逐条对照源码核对：`tune` 后 `uistat.freq=7100000`（`cmd_tune` 写入）；`mode lsb` 后 `show mode` 输出 `lsb`（`mod_table[MOD_LSB].name`，且 `mod_table` 的 fs=48 会被 `set_modulation` 设进 `uistat.fs`，但 `show` 不显示 fs）；`gain 30` 后 `uistat.rfgain=30`；`agc slow` 后 `agcmode_table[AGC_SLOW]="slow"`。
3. 再预测一个陷阱题：执行 `mode a` 会怎样？（对照 [main.c:666](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L666) 的 1 字符前缀匹配，答案是切到 AM。）
4. 有硬件时（待本地验证）：用 `screen /dev/ttyACM0` 连接，逐条执行上述命令并 `show` 核对，再用 `stat` 记录一次 load 值。

**需要观察的现象**：纸上预测与源码核对完全一致；真机上 `show` 输出与预测一致。

**预期结果**：确认"命令 = 改 uistat + 调底层函数 + 刷屏"的心智模型成立；`mode a` 的行为印证前缀匹配特性。

#### 4.3.5 小练习与答案

**练习 1**：`tune 7100000`（当前模式 AM）与 `tune 7100000`（当前模式 LSB）设给 SI5351 的频率相同吗？

**答案**：不同。`set_tune()` 是 `center_frequency = hz - mode_freq_offset; si5351_set_frequency(center_frequency * 4)`。AM 模式 `mode_freq_offset = AM_FREQ_OFFSET`（10000），所以 SI5351 得到 `(7100000-10000)*4`；LSB 模式偏移为 0，得到 `7100000*4`。模式切换会改变 `mode_freq_offset`，同一 `tune` 值在不同模式下对应不同本振频率。

**练习 2**：为什么 `cmd_stat` 里 `load` 的计算要放在 Thread1 而不是 I2S 回调里做除法打印？

**答案**：I2S 回调是硬实时上下文（5ms 周期），只做"记录起止周期数"（[main.c:260](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L260)、[main.c:269-272](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L269-L272)）；除法和 `chprintf` 属于慢速工作，放在 Thread1 的 100ms 循环里按需计算打印。中断里只留计数、耗时的处理放线程，是嵌入式的基本纪律。

**练习 3**：`data 2` 和 `data 3` 抓的 `buffer`/`buffer2` 在解调链路里是什么角色？

**答案**：它们是解调的中间级缓冲（`buffers_table` 里标注为 `BT_IQ`，见 [main.c:102-107](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L102-L107)）——`signal_process` 各阶段的中间结果会写到这两个缓冲。抓取它们可以在 PC 上观察算法中间级信号（例如频移后、滤波后的波形），是单元三调试解调算法时的主要手段。

### 4.4 Python 控制工具：centsdr.py

#### 4.4.1 概念说明

`python/centsdr.py` 是一个**双重身份**的脚本：既可以 `import` 当模块用（类 `CentSDR`），也可以直接当命令行工具跑（`run_as_command()`，[python/centsdr.py:112-186](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/centsdr.py#L112-L186)）。

它解决三个问题：

1. **免去手敲**：把 shell 命令封装成方法（`set_tune()` → `tune %d\r`）。
2. **同步问题**：文本协议里怎么知道一条命令的输出结束了？答案是拿 `ch>` 提示符当"完成信号"。
3. **数据落地**：把 `data` 命令的十六进制文本解析成 numpy 数组并绘图。

注意它是 **Python 2.7 代码**（`print` 语句、`decode('hex')`，[python/README.md:18-26](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/README.md#L18-L26) 明确标注运行要求），在 Python 3 下不能直接运行。

设备节点的确定顺序：`-d` 参数 > 环境变量 `CENTSDR_DEVICE` > 脚本里的 `DEFAULT_DEVICE`（[python/centsdr.py:11-12](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/centsdr.py#L11-L12)），与 [python/README.md:28-34](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/README.md#L28-L34) 的说明一致。

#### 4.4.2 核心流程

一次 `read_status()` 的完整时序：

```
Python                          固件 (shell 线程)
──────                          ─────────────────
write("show \r")        ──────► 收到一行，回显，执行 cmd_show
readline()  # 丢弃回显行 ◄────── "show \r\n"（回显）
逐字符 read()           ◄────── "tune: 7100000\r\nvolume: 10\r\n..."
                              "ch> "                 ← 结束信号！
line.endswith('ch>') → break
返回多行字符串
```

协议设计要点：**固件侧没有任何"包格式"或长度前缀，脚本的同步完全依赖提示符 `ch>`**。这也是所有自制文本 shell 协议最常见的做法——简单可靠，代价是输出里不能出现以 `ch>` 结尾的行。

抓波形（`fetch_array`）在此基础上多一步解析：

```
send_command("data 2\r")
  → fetch_data() 收齐全部十六进制行（直到 ch>）
  → 每行按空格 split
  → 每个四位十六进制 → bytes → struct.unpack('>h') → 有符号 int16
  → 拼成 numpy 数组返回
```

#### 4.4.3 源码精读

**（1）连接管理**。[python/centsdr.py:10-29](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/centsdr.py#L10-L29)：构造函数只记设备名不打开串口（懒连接），`open()`/`close()` 幂等，并实现了 `__enter__`/`__exit__` 支持 `with` 上下文写法。注意 `serial.Serial(self.dev)` 没有设置超时——设备不应答时 `fetch_data()` 会永久阻塞，写自动化脚本时要意识到这一点。

**（2）命令发送**。[python/centsdr.py:31-34](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/centsdr.py#L31-L34)：

```python
def send_command(self, cmd):
    self.open()
    self.serial.write(cmd)
    self.serial.readline() # discard empty line
```

所有命令以 `\r` 结尾发出，紧跟的 `readline()` 丢掉回显产生的第一行。**每个 setter 方法都是一行模板**（[python/centsdr.py:36-58](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/centsdr.py#L36-L58)）：`set_mode(mode)` → `"mode %s\r"`、`set_fs(fs)` → `"fs %d\r"`……加新命令的 Python 封装就是在这一段加一个方法。

**（3）提示符同步**。[python/centsdr.py:60-75](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/centsdr.py#L60-L75)：

```python
def fetch_data(self):
    result = ''
    line = ''
    while True:
        c = self.serial.read()
        if c == chr(13):
            next # ignore CR        ← 注意：next 在这里是内建函数名当表达式用，
        line += c                    ← 并不是 continue！CR 实际仍被拼进 line
        if c == chr(10):
            result += line
            line = ''
            next                     ← 同样是无效语句
        if line.endswith('ch>'):
            # stop on prompt
            break
    return result
```

这里有个**真实的代码彩蛋**：作者显然想把 `next` 当 `continue` 用，但 `next` 单独成句只是引用了 Python 内建函数、什么都不做，所以 CR 其实**没有**被忽略、仍拼进了 `line`。这不影响正确性——判断条件是 `endswith('ch>')`，行尾多一个 CR 也照样匹配。读开源代码时能发现这种"意图与实现不符但无伤大雅"的细节，说明你真的逐字符读进去了。

**（4）十六进制解析**。[python/centsdr.py:77-86](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/centsdr.py#L77-L86)：`fetch_array()` 把每行按空格切开，`struct.unpack('>h', h.decode('hex'))` 把 `"ffff"` 这类文本还原成有符号 16 位数（-1）——与固件 `chprintf(chp, "%04x ", 0xffff & (int)buf[i])` 的补码输出严格互补。

**（5）一个不同步的方法**。[python/centsdr.py:88-96](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/centsdr.py#L88-L96) 的 `data()` 按**十进制**复数对解析（`float(d[0])+float(d[1])*1.j`），而当前固件 `cmd_data` 输出的是**十六进制**——两者格式不匹配（疑似旧版固件输出十进制的历史遗留）。实际可用的抓取路径是 `fetch_array()`，命令行的 `-p` 绘图走的也是它。使用前先读一眼，别踩坑。

**（6）命令行入口**。[python/centsdr.py:112-186](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/centsdr.py#L112-L186)：`optparse` 定义 `-F/-G/-M/-V/-C/-A`（设置类，按序调用 setter）、`-s/-S`（`read_status`）、`-P`（`read_power`，用正则 `power: ([\d.-]+)dBm` 从文本里抠出数值，[python/centsdr.py:98-102](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/centsdr.py#L98-L102)）、`-p <buffer>`（`fetch_array` + pylab 绘图，缓冲区 0 时按交织 IQ 重组成复数序列，[python/centsdr.py:159-161](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/centsdr.py#L159-L161)）、`-l`（循环刷新，注册 SIGINT 优雅退出）。缓冲区编号 0~3 的含义（采集/音频/中间 1/中间 2）见 [python/README.md:118-131](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/README.md#L118-L131)，与 4.3 节 `cmd_data` 的 switch 一一对应。

**（7）脚本用法**（[python/README.md:145-155](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/README.md#L145-L155)）：

```python
from centsdr import CentSDR
sdr = CentSDR()
sdr.set_tune(27500000)
sdr.set_mode('fm')
sdr.set_volume(20)
```

#### 4.4.4 代码实践

**实践目标**：在没有任何硬件的 PC 上，验证自己对"十六进制文本 → int16 数组"解析逻辑的理解。

**操作步骤**：

1. 写一个 10 行左右的 Python 2 脚本 `parse_test.py`（示例代码）：

   ```python
   # -*- coding: utf-8 -*-
   # 示例代码：验证 centsdr.py 的 hex16 解析逻辑
   import struct

   def hex16(h):
       return struct.unpack('>h', h.decode('hex'))[0]

   # 模拟固件 chprintf(chp, "%04x ", 0xffff & (int)buf[i]) 的输出
   samples = [32767, 1, 0, -1, -32768, -1234]
   line = ' '.join('%04x' % (0xffff & s) for s in samples)
   print('firmware line: %s' % line)

   parsed = [hex16(tok) for tok in line.strip().split(' ')]
   print('parsed:        %s' % parsed)
   assert parsed == samples, 'mismatch!'
   print('OK')
   ```

2. 用 `python2 parse_test.py` 运行（只需标准库，不需要 pyserial/numpy；若机器上只有 Python 3，可把 `h.decode('hex')` 改成 `bytes.fromhex(h)` 后运行，解析逻辑本身与版本无关）。

**需要观察的现象**：`firmware line` 打印出 `7fff 0001 0000 ffff 8000 fb2e`；`parsed` 恢复出原始带符号整数。

**预期结果**：断言通过，输出 `OK`。这证明"固件 `%04x` 补码输出 + Python `>h` 大端有符号解析"是一对严丝合缝的编解码组合，为以后写自己的抓取脚本打底。

（有硬件时的进阶验证，待本地验证：`./centsdr.py -p 0` 应弹出 I/Q 两路波形图，静止按窗口关闭键退出；`-p 0 -l` 连续刷新、Ctrl-C 退出。）

#### 4.4.5 小练习与答案

**练习 1**：为什么 `fetch_data()` 必须逐字符 `read()`，而不是按行 `readline()`？

**答案**：因为结束条件不是"某一行结束"而是"行尾出现 `ch>` 提示符"。提示符后面没有换行符（shell 在等用户输入），`readline()` 会一直等 `\n` 而永远读不到，只能逐字符读、自己拼接并检查 `endswith('ch>')`。

**练习 2**：如果固件某条命令的输出恰好有一行以 `ch>` 结尾，会发生什么？

**答案**：`fetch_data()` 会提前判定命令结束，后续输出被丢进下一条命令的读取里，造成数据错位。文本提示符协议的固有弱点。修改固件输出时（包括你以后自己加的命令）要避免任何行以 `ch>` 结尾。

**练习 3**：给 `centsdr.py` 加一个 `set_cwtone(freq)` 方法该怎么写？

**答案**：在 [python/centsdr.py:36-58](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/centsdr.py#L36-L58) 的 setter 区按模板加一行：

```python
def set_cwtone(self, freq):
    self.send_command("cwtone %d\r" % freq)
```

对应固件命令 `cwtone`（[main.c:681-698](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L681-L698)）。这也演示了 Python 侧与固件侧命令是"一行对一行"的镜像关系。

## 5. 综合实践

**任务：给 shell 新增一条 `hello` 命令，走通"固件改动 → 编译 → 验证"闭环。**

这条命令打印当前频率和调制模式，效果类似：

```
ch> hello
freq: 7100000 mode: lsb
ch>
```

**步骤 1：写命令函数**。在 [main.c:728-753](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L728-L753) 的 `cmd_show` 附近（任何 `cmd_*` 旁都可以）加入（示例代码——非项目原有代码）：

```c
static void cmd_hello(BaseSequentialStream *chp, int argc, char *argv[])
{
    (void)argc;
    (void)argv;
    chprintf(chp, "freq: %d mode: %s\r\n",
             uistat.freq, mod_table[uistat.modulation].name);
}
```

写法完全模仿 `cmd_show`：`uistat.freq` 是 `uint32_t`（[nanosdr.h:262](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L262)），`cmd_show` 也用 `%d` 打印它；模式名直接查 `mod_table`（[main.c:169](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L169) 的 `name` 字段），与 [main.c:733](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L733) 同款写法。

**步骤 2：注册**。在 [main.c:874-904](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L874-L904) 的 `commands` 表里任意两条之间加一行：

```c
    { "hello", cmd_hello },
```

就这两处，共约 6 行——这就是表驱动注册机制的全部代价。

**步骤 3：编译**。按 u1-l2 的方法：

```bash
make          # 产出 build/ch.elf
arm-none-eabi-size build/ch.elf   # 对比改动前后 text 段增量（预期 +几十字节）
```

**步骤 4：验证**（需要硬件，待本地验证）：

1. 烧录：`make flash`（或 OpenOCD/Nucleo 方式，见 u1-l2）。
2. 手动验证：`screen /dev/ttyACM0`，回车出现 `ch>` 后敲 `hello`。
3. 脚本验证（本实践的指定环节）：

   ```bash
   cd python
   ./centsdr.py -F 7100000 -M lsb     # 设置频率和模式
   ./centsdr.py -s                     # 读回状态
   ```

   `-s` 输出（[python/README.md:99-109](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/README.md#L99-L109) 有示例格式）中 `tune:` 与 `mode:` 两行应与你刚设置的 `7100000`/`lsb` 一致；串口终端里 `hello` 打印的值应与 `show` 完全相同——两条路径读的是同一个 `uistat`。

**无硬件替代验证**：只完成步骤 1-3，确认 `make` 无警告无错误、`ch.elf` 正常产出；同时说明如果上机，你预计在哪里看到 `hello` 的输出（`ch>` 提示符后一行、以 `\r\n` 结尾、之后 shell 重新打印提示符）。

**思考题（附答案）**：如果把 `hello` 写成与 `he` 开头的其他命令前缀冲突的名字会怎样？——shell 分发用的是命令表里的名字做精确/前缀匹配（框架行为），而本讲 4.3 里 `cmd_mode` 的前缀匹配是**命令函数自己**做的 `strncmp`，两者层次不同：前者决定"哪条命令被调用"，后者决定"命令内部怎么解释参数"。理解这两层边界，加命令时就不会踩坑。

## 6. 本讲小结

- CentSDR 的控制通道是 `usbcfg.c` 配置的 USB CDC 虚拟串口：设备/配置/字符串三类描述符向主机声明身份，EP1 批量对传数据、EP2 中断传通知，`usb_event` 在 `USB_EVENT_CONFIGURED` 时激活端点，电脑侧即得 `/dev/ttyACM*`。
- Shell 是 ChibiOS 提供的框架，应用侧的全部工作是 [main.c:874-904](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L874-L904) 那张 27 项、NULL 哨兵结尾的 `commands` 表；命令统一签名 `(BaseSequentialStream*, argc, argv)`，输出用 `chprintf`。
- 命令语义的通用套路是"校验参数 → 调底层函数 → 更新 `uistat` → `disp_update()`"；`tune` 与 `freq` 的区别（信号频率 vs 本振实际频率）、`mode` 的前缀匹配顺序、`data` 的缓冲区编号是与代码语义一一对应的三个易错点。
- 解调模式切换的本质是替换 `signal_process` 函数指针，`stat` 里的 `load` 用 \( \text{busy}/\text{interval} \times 100\% \) 量化 DSP 实时余量。
- `python/centsdr.py` 是 Python 2 的模块兼 CLI，靠 `ch>` 提示符做命令完成同步、靠 `"%04x"`↔`'>h'` 的编解码对抓取内部缓冲区波形；注意 `data()` 方法与现行固件输出格式不匹配，绘图请走 `fetch_array()`。
- 扩展成本极低：固件侧加命令 = 一个函数 + 表里一行；Python 侧加封装 = 一个 send_command 模板方法。

## 7. 下一步学习建议

- **下一单元（u2）**将进入四个外设驱动：`gain`/`volume`/`agc` 命令最终落到的 TLV320AIC3204 编解码器（u2-l2）、`tune` 背后的 SI5351 本振（u2-l1）、承载 `data` 命令数据的 I2S 双缓冲流（u2-l3），以及屏幕（u2-l4）。届时本讲的命令表将成为你验证驱动理解的"遥控器"。
- 想深挖 shell 框架本身，可检出 ChibiOS 子模块后阅读 `os/various/shell/shell.c`（由 [Makefile:105](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L105) 引入），对照本讲 4.2 的分发流程图验证。
- `stat` 命令的 `load`/`fps`/`overflow` 三个指标是后续评估实时性的抓手，u5-l1 会用它们做并发与负载分析；`data` 抓取的缓冲区将在 u4-l1 中被送进 FFT 变成屏幕上的频谱。
- 如果本讲的 `hello` 命令你顺利完成，可以预习 u5-l4 的"新增解调模式"——那是同一套扩展思想在算法层的放大版。
