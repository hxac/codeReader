# 串口启动与上板流程

## 1. 本讲目标

本讲是「FPGA SoC 与外设」单元的收尾篇。前面两讲（u14-1、u14-2）已经把 Nyuzi SoC 在 DE2-115 开发板上的「静态结构」讲清楚了：AXI 互连怎么连、外设控制器怎么挂。本讲则回答一个「动态」问题：

> **一段编译好的程序，到底是怎么从你的电脑进入 FPGA 上的内存，并开始执行的？**

读完本讲，你应当能够：

1. 说清 `serial_boot` 宿主工具与 `bootrom` 第一级引导程序之间的**串口下载协议**（握手、分块、校验、执行）。
2. 画出从**按下复位键**到**跳转到用户程序**的完整启动流程，并解释「为什么程序要加载到地址 0、而引导 ROM 在高地址」。
3. 区分 Nyuzi 的三种运行环境——**C 模拟器、Verilator 周期精确仿真、FPGA 实物上板**——在加载方式、时钟、外设保真度上的差异，知道什么时候该用哪一个。

本讲是「纯软件 + 纯硬件」的接缝：`serial_boot` 跑在你的 x86/Linux 电脑上，`bootrom` 跑在 FPGA 里的 Nyuzi 核上，两者靠一根串口线（通常是 USB 转串口）和一个自定的字节协议对话。理解这条接缝，就理解了「上板」这件事的全部。

## 2. 前置知识

本讲默认你已经掌握以下概念（来自前置讲义）：

- **MMIO（内存映射 I/O）**：外设寄存器被映射到高地址（`0xffff0000` 起的一段），读写这些地址就是读写外设，而不是读写内存。`bootrom` 就是通过写 UART 的 MMIO 寄存器来收发字节的（见 u9-l1、u14-2）。
- **UART**：最朴素的串口，一次收发一个字节。本讲里它既用来下载程序，也用来在程序跑起来后当作控制台（console）。
- **`$readmemh` 与 hex 内存镜像**：Verilog 的 `$readmemh` 系统任务能按「每行一个 32 位十六进制数」的格式把文件读进内存数组；`elf2hex` 工具把 ELF 转成这种格式（见 u1-l4、u9-l1）。`serial_boot` 下载的就是这种 hex 文件。
- **复位 PC 与取指**：Nyuzi 核复位后从一个可配置的 `RESET_PC` 开始取指（见 u4-l1）。
- **控制寄存器与线程号**：`getcr s0, 0` 能读出当前线程号（见 u2-l4）。本讲的 `bootrom` 用它区分「线程 0（要跑引导程序）」和「其它线程（直接跳到用户程序）」。

一个需要建立的关键直觉：**「下载」和「执行」是两件事**。FPGA 上电后，SDRAM 里并没有你的程序；必须有人（`serial_boot`）通过串口把程序字节一点点灌进去，然后发一条「开始执行」的命令，引导 ROM 才会跳过去。这和「模拟器里 `+bin=xxx.hex` 一启动就把整个文件载入内存并从 0 执行」是完全不同的机制。

## 3. 本讲源码地图

本讲涉及的关键文件分两类：**宿主侧（你的电脑上）** 和 **目标侧（FPGA 上的 Nyuzi 核里）**。

| 文件 | 侧 | 作用 |
| --- | --- | --- |
| [tools/serial_boot/serial_boot.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/serial_boot/serial_boot.c) | 宿主 | 串口下载工具主体：打开串口、握手、分块发送 hex 镜像、校验、发执行命令、进入控制台 |
| [software/bootrom/protocol.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/bootrom/protocol.h) | 双方共享 | 串口协议的命令字节定义（`LOAD_MEMORY_REQ` 等），宿主与目标**两边都 include**，保证编码一致 |
| [software/bootrom/boot.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/bootrom/boot.c) | 目标 | 第一级引导程序：轮询串口命令，把收到的字节写进内存、回校验、收到执行命令后返回 |
| [software/bootrom/start.S](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/bootrom/start.S) | 目标 | 引导 ROM 的入口：线程 0 调 `boot.c`，结束后跳到地址 0 |
| [software/bootrom/boot.ld](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/bootrom/boot.ld) | 目标 | 链接脚本，把引导程序固定到高地址 `0xfffee000` |
| [hardware/fpga/de2-115/de2_115_top.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv) | 目标（硬件） | DE2-115 顶层：把引导 ROM 挂成 AXI 从设备，并设置 `RESET_PC = 0xfffee000` |
| [hardware/fpga/de2-115/README.md](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/README.md) | 文档 | 上板操作步骤、串口波特率、`run_fpga` 用法 |
| [hardware/README.md](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/README.md) | 文档 | Verilator 仿真的 `+bin` 等参数说明 |
| [cmake/nyuzi.cmake](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/cmake/nyuzi.cmake) | 构建 | 自动生成 `run_emulator` / `run_verilator` / `run_fpga` 三个脚本，是三种环境差异的「事实来源」 |

记住一个对应关系：**宿主侧的 `serial_boot.c` 与目标侧的 `boot.c` 是一对镜像**，它们实现同一个协议的两端；`protocol.h` 是两者共享的「合同」。

## 4. 核心概念与源码讲解

### 4.1 串口下载协议

#### 4.1.1 概念说明

`serial_boot` 解决的问题是：**FPGA 上的 SDRAM 是易失的**——掉电就空，上电后里面什么也没有，更没有「读文件」的能力。所以必须有一个外部 agent（你的电脑）把程序字节喂进去。

整个下载过程是典型的「**宿主 + 目标**」二段式设计：

- **宿主（`tools/serial_boot/serial_boot.c`）**：跑在你的电脑上，负责读 hex 文件、打开串口设备（如 `/dev/ttyUSB0`）、驱动整个握手流程。
- **目标（`software/bootrom/boot.c`）**：这是一段被综合进 FPGA 里 ROM 的小程序，复位后就在 Nyuzi 核上跑，死循环地「等串口命令、执行命令、回应」。

两者通过一个**极简的字节协议**对话，协议常量定义在共享头文件 `protocol.h` 里。这个协议有四类命令：

| 命令字节 | 含义 | 谁发 |
| --- | --- | --- |
| `PING_REQ` / `PING_ACK` | 探活：你在吗？/ 在。 | 宿主发 REQ，目标回 ACK |
| `LOAD_MEMORY_REQ` / `LOAD_MEMORY_ACK` | 把一块字节写入指定内存地址 | 宿主发数据，目标回 ACK + 校验和 |
| `CLEAR_MEMORY_REQ` / `CLEAR_MEMORY_ACK` | 把一段内存清零（优化全零块） | 宿主发，目标回 ACK |
| `EXECUTE_REQ` / `EXECUTE_ACK` | 跳转到下载的程序执行 | 宿主发，目标回 ACK 后跳出引导循环 |
| `BAD_COMMAND` | 收到无法识别的命令 | 目标发，用于错误恢复 |

注意：**这套协议不是通用的 XMODEM/ZMODEM，而是 Nyuzi 自定义的**，简单到只有 9 个常量。它特意为「可靠的程序加载」做了两件事：① 每块数据都回一个 **FNV-1a 校验和**供宿主比对；② 有握手探活和连接修复机制。代价是协议很「专一」，只能用来下程序。

#### 4.1.2 核心流程

`serial_boot` 的 `main` 函数把这些步骤串起来，整体流程是：

```
1. 读取 hex 镜像（解析成若干「地址 + 数据」段）
2. （可选）读取 ramdisk 二进制镜像
3. 打开串口设备，配置 921600 波特率
4. ping_target：反复发 PING_REQ，直到目标回 PING_ACK（最多 20 次）
5. send_segments：对每一段，按 1024 字节切块逐块发送
     - 若块全零 → 发 CLEAR_MEMORY_REQ（省带宽）
     - 否则     → 发 LOAD_MEMORY_REQ（地址+长度+数据），等 ACK，比对 FNV-1a 校验和
     - 若校验失败 → fix_connection 重新同步后重发该块
6. （可选）把 ramdisk 发到固定地址 0x4000000
7. send_execute_command：发 EXECUTE_REQ，目标回 EXECUTE_ACK
8. do_console_mode：把串口变成「终端 ↔ 串口」双向桥，你可以和跑起来的程序交互
```

整个握手可用下面的时序示意（一次成功的 `LOAD_MEMORY`）：

```
宿主 serial_boot                 目标 boot.c (FPGA)
   | --- LOAD_MEMORY_REQ -----------> |
   | --- address (4 字节, LSB 先) ---> |
   | --- length  (4 字节, LSB 先) ---> |
   | --- data[length]  ------------> |  (逐字节写内存并累加 FNV-1a)
   | <--- LOAD_MEMORY_ACK ----------- |
   | <--- checksum (4 字节) -------- |  (目标算出的 FNV-1a)
   |   (宿主本地也算一份，两者比对)     |
   |   相等 → 本块成功，下一块         |
```

一个 32 位整数在串口线上是**低字节先发（小端字节序）**的：发送端把最低字节先写，接收端用「每次右移 8 位、把新字节塞进最高位」的方式还原。两端约定一致，所以能正确还原出原值。

FNV-1a 校验和是关键可靠性保障。对一块 `length` 字节的数据，初始化 `hash = 2166136261`，然后对每个字节 `b` 执行 `hash = (hash ^ b) * 16777619`（常数 16777619 = 0x0119，是 FNV 质数）。宿主和目标用**完全相同**的公式各算一遍，再比对，以此发现串口传输中的丢字节或错位。

#### 4.1.3 源码精读

**协议合同 —— `protocol.h`**。宿主和目标都 include 这个头文件，靠枚举值保证两边的命令字节编码完全一致。这正是不编造接口的关键：协议不是哪一方「自定义」的，而是双方共享的合同。

[software/bootrom/protocol.h:20-31](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/bootrom/protocol.h#L20-L31) 定义了全部 9 个命令常量，从 `LOAD_MEMORY_REQ = 0xc0` 开始递增。

**宿主入口 —— `serial_boot.c` 的 `main`**。它严格按 4.1.2 的顺序调用各步骤，命令行用法是 `serial_boot <串口设备> <hex 文件> [ramdisk]`。

[tools/serial_boot/serial_boot.c:709-758](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/serial_boot/serial_boot.c#L709-L758) 是 `main` 主体：读 hex、开串口、ping、发段、发 ramdisk、发执行命令、进控制台。注意 `argv[1]` 是串口设备路径、`argv[2]` 是 hex 文件、`argv[3]` 是可选 ramdisk。

**握手探活 —— `ping_target`**。复位后目标可能还没准备好，宿主反复发 `PING_REQ`、最多重试 20 次，每次等 250ms，直到收到 `PING_ACK` 才认为目标在线。

[tools/serial_boot/serial_boot.c:238-267](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/serial_boot/serial_boot.c#L238-L267) 实现了带重试的探活循环。

**核心数据传输 —— `load_memory`**。这是「发一块数据并校验」的函数，对应时序图里的主体。

[tools/serial_boot/serial_boot.c:160-213](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/serial_boot/serial_boot.c#L160-L213)：先发 `LOAD_MEMORY_REQ`、地址、长度，再一次性 `write` 整块数据；然后读一个字节等 `LOAD_MEMORY_ACK`（超时 15 秒）；接着**本地计算 FNV-1a 校验和**（第 195-197 行的循环），读回目标算的校验和（第 199 行）并比对（第 205 行）。比对失败即返回 false，由上层重发。

**全零块优化与重发 —— `send_segment`**。一段数据按 1024 字节切块；若某块全是 0，就发更便宜的 `CLEAR_MEMORY_REQ`（目标只需 `memset` 清零，不必回传校验和）；否则 `load_memory`；若失败，调 `fix_connection` 重新同步并重发该块。

[tools/serial_boot/serial_boot.c:657-693](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/serial_boot/serial_boot.c#L657-L693) 是分块发送循环，`is_empty` 判定（第 669 行）决定走 clear 还是 load 分支，`fix_connection`（第 679 行）负责错误恢复。

**目标侧镜像 —— `boot.c` 的 `main`**。这段是 `load_memory` 的「对端」：收命令、按命令把字节写进内存、回 ACK 和校验和。因为运行在 ROM 里，它**不能用全局变量**（注释里特意说明），所有状态都是局部变量。

[software/bootrom/boot.c:86-137](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/bootrom/boot.c#L86-L137) 是一个 `for(;;)` 大 switch：`LOAD_MEMORY_REQ` 分支（第 96-111 行）读地址、长度，逐字节读串口、写内存、累加 FNV-1a，最后回 `LOAD_MEMORY_ACK` 和校验和——与宿主的 `load_memory` 逐字段对应。`EXECUTE_REQ` 分支（第 122-127 行）回 ACK 后 `return 0` 跳出循环。

**32 位字的串口编解码**。两端用一致的「低字节先发」约定。宿主的发送：

[tools/serial_boot/serial_boot.c:141-158](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/serial_boot/serial_boot.c#L141-L158) 按 LSB→MSB 顺序写 4 个字节。目标的接收用「右移 + 新字节塞最高位」还原：

[software/bootrom/boot.c:69-76](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/bootrom/boot.c#L69-L76) 的 `read_serial_long` 与宿主的 [serial_boot.c:112-128](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/serial_boot/serial_boot.c#L112-L128) 用的是同一个「右移还原」算法，保证两端对齐。

#### 4.1.4 代码实践

> **实践目标**：不依赖真实硬件，仅通过阅读源码「演算」一次 `LOAD_MEMORY` 往返，确认你对协议字段顺序和校验和的理解。

**操作步骤**：

1. 打开 [serial_boot.c 的 `load_memory`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/serial_boot/serial_boot.c#L160-L213)，按发送顺序写下宿主往串口写了哪些字节（命令、地址、长度、数据）。
2. 打开 [boot.c 的 `LOAD_MEMORY_REQ` 分支](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/bootrom/boot.c#L96-L111)，确认目标按相同顺序读取了这些字段。
3. 手算一个最小例子：假设要加载地址 `0x00001000`、内容是两个字节 `0x01 0x02`。用 FNV-1a 公式（初值 `2166136261`，每步 `hash=(hash^b)*16777619`）算出校验和。

**需要观察的现象**：

- 宿主发的地址 `0x00001000` 在串口线上出现的字节序应当是 `0x00 0x10 0x00 0x00`（LSB 先发），而不是 `0x00 0x00 0x10 0x00`。
- 宿主本地算出的校验和与目标回传的校验和应**逐位相等**；只要差一位，`load_memory` 第 205 行就返回 false。

**预期结果**：

- 你能在纸上画出一次完整 `LOAD_MEMORY` 的双向字节流。
- 对 `0x01 0x02` 两字节，FNV-1a 结果是一个确定的 32 位值（待本地验证：可写一段 5 行 Python 用同样的公式核对，注意 Python 整数需 `& 0xffffffff` 截断到 32 位）。

> 本实践为「源码阅读型实践」，不需要 FPGA 硬件。若你有 DE2-115 板子，可进一步按 4.3.4 的步骤实跑 `run_fpga`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `send_segment` 要对「全零块」单独走 `CLEAR_MEMORY_REQ`，而不是统一用 `LOAD_MEMORY_REQ`？

**参考答案**：全零块里没有任何信息，发过去纯属浪费串口带宽（1024 字节）和校验往返。`CLEAR_MEMORY_REQ` 只发地址和长度，目标用 `memset` 本地清零即可，回一个 ACK 就行，省掉了 1024 字节的传输和校验和比对。这是针对「hex 镜像里常有大量零间隙（BSS 段等）」的务实优化。

**练习 2**：如果某一块的 FNV-1a 校验和比对失败，`serial_boot` 会直接退出吗？

**参考答案**：不会。`send_segment` 在 `load_memory` 失败时会调 `fix_connection` 重新与目标同步（清除 `BAD_COMMAND`、重新 ping），同步成功后**重发同一块**（`offset` 不前进，见第 685-688 行的 `if (copied_correctly)` 守卫）。只有 `fix_connection` 自己失败（连续 40 次没收到 ping）才会真正放弃。

**练习 3**：`boot.c` 为什么强调「不能用全局变量」？

**参考答案**：因为它被综合成 ROM（见 `boot.ld` 链接到 `0xfffee000`），ROM 是只读的，可写的数据段（`.data`/`.bss`）没有有效存储位置。任何全局变量都没有可写的落脚点，所以全部状态都用栈上的局部变量。

---

### 4.2 从复位到跳转：启动流程

#### 4.2.1 概念说明

4.1 讲清了「字节怎么进来」，本节回答「**整个板子从上电到跑你的程序，先后发生了什么**」。这里有一个关键的空间布局问题需要先建立直觉：

- **引导 ROM 在高地址** `0xfffee000`（接近 `0xffff0000` 的外设区，但更低），它是只读的、综合进 FPGA 的。
- **你的程序要被加载到低地址** `0x00000000` 起的 SDRAM 里。

为什么这么分？因为 **`serial_boot` 把 hex 镜像的第一段写到地址 0**（裸机程序的链接基址就是 0，见 u9-l1 的 crt0），而引导 ROM 必须在复位时就在某个固定位置等着——这个位置由硬件参数 `RESET_PC` 决定。所以让引导 ROM 占高地址、用户程序占低地址 0，两者互不打架。

启动流程的「主角」是 `start.S`：它只有几行，却定义了「**线程 0 跑引导，其它线程直接跳 0**」的关键分流逻辑。这样设计的好处是：用户程序之后用 `CR_RESUME_THREAD` 唤醒的其它线程，也会从同一个复位入口进来，但它们跳过引导、直接进入已经加载好的用户程序（地址 0），无需重新下载。

#### 4.2.2 核心流程

从按下复位键（DE2-115 的 KEY0）到你的程序跑起来，时序如下：

```
上电 / 按 KEY0 复位
   │  Nyuzi 核从 RESET_PC = 0xfffee000 取指
   ▼
start.S: _start
   │  getcr s0, 0           读线程号
   ├─ 线程 0 ─────────────► li sp, 0x400000   设临时栈
   │                        call main        调 boot.c 的引导主循环
   │                           (此时 serial_boot 在另一端下程序)
   │                        ◄── main 在 EXECUTE_REQ 后 return
   ├─ 线程 1..N ──────────► (bnz 直接跳过 loader)
   ▼
jump_to_zero:  move s0, 0 ; b s0     无条件跳到地址 0
   │
   ▼
用户程序（crt0._start → main）在 SDRAM 地址 0 开始执行
```

两个要点：

1. **只有线程 0 跑引导**。复位时硬件只使能线程 0（见 u10-l2 的 `thread_en` 复位值）。即便其它线程以后被唤醒，它们也走 `bnz s0, jump_to_zero` 直接跳到 0。
2. **「跳到 0」就是启动用户程序的唯一动作**。引导 ROM 不「调用」用户程序，而是用一个无条件分支 `b s0`（s0=0）把 PC 设成 0，从此用户程序接管。引导 ROM 的使命到此结束。

注意 `li sp, 0x400000`：引导程序用一个位于 4MiB 处的**临时栈**，这跟用户程序的栈区（通常在高地址，见 u9-l1）不冲突，因为引导阶段用户程序还没加载。

#### 4.2.3 源码精读

**复位地址的硬件接线 —— `de2_115_top.sv`**。`RESET_PC` 参数告诉 Nyuzi 核「复位后从哪里取第一条指令」，这里被设成引导 ROM 的基址。

[hardware/fpga/de2-115/de2_115_top.sv:73](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv#L73) 定义 `BOOT_ROM_BASE = 32'hfffee000`；[第 111 行](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv#L111) `nyuzi #(.RESET_PC(BOOT_ROM_BASE))` 把它传给核；[第 140 行](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv#L140) `axi_rom boot_rom` 把编译好的 `boot.hex` 挂成 AXI 从设备。这正是 u14-1 讲过的「ROM 是 AXI 从设备、地址译码由 `M1_BASE_ADDRESS` 完成」。

**引导程序的链接位置 —— `boot.ld`**。链接脚本把 `.text` 固定到 `0xfffee000`，与硬件的 `BOOT_ROM_BASE` 对齐，这样综合出来的 ROM 字节恰好落在 `RESET_PC` 处。

[software/bootrom/boot.ld:3](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/bootrom/boot.ld#L3) `. = 0xfffee000;`。

**入口分流 —— `start.S`**。这是整个启动流程最浓缩的几行。

[software/bootrom/start.S:32-39](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/bootrom/start.S#L32-L39)：`_start` 用 `getcr s0, 0` 读线程号，`bnz s0, jump_to_zero` 让非 0 线程跳过引导；线程 0 设栈、`call main` 进引导循环；`main` 返回后（即收到 `EXECUTE_REQ`）落到 `jump_to_zero`，`move s0, 0; b s0` 跳到地址 0。文件顶部[注释（17-25 行）](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/bootrom/start.S#L17-L25)清楚说明了「下载完跳 0、其它线程也跳 0 但跳过 loader」的设计意图。

**触发返回的命令 —— `boot.c` 的 `EXECUTE_REQ`**。引导循环什么时候结束？当宿主发来 `EXECUTE_REQ`。

[software/bootrom/boot.c:122-127](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/bootrom/boot.c#L122-L127)：关掉 LED、回 `EXECUTE_ACK`、`return 0`。这一 `return` 让控制权回到 `start.S` 的 `call main` 之后，从而走到 `jump_to_zero`。

**「活着」的信号 —— LED 闪烁**。引导程序在等串口字节时会让绿色 LED 闪烁，告诉操作者「我还在等你下程序」。

[software/bootrom/boot.c:42-59](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/bootrom/boot.c#L42-L59) 的 `read_serial_byte` 在轮询 UART 状态时，每计数到 `BLINK_DELAY` 就翻转一次 `REG_GREEN_LED`。DE2-115 README 里说的「复位后 LED 0 闪烁，表示引导程序在等程序」就是这个。

#### 4.2.4 代码实践

> **实践目标**：把 FPGA 上的地址空间布局画出来，搞清楚「引导 ROM、用户程序、临时栈、外设」各占哪段。

**操作步骤**：

1. 从本讲源码里收集四个地址：引导 ROM 基址（`boot.ld` / `de2_115_top.sv`）、用户程序基址（地址 0）、引导临时栈顶（`start.S` 的 `li sp`）、外设区基址（`boot.c` 的 `REGISTERS`）。
2. 画一条从 `0x0` 到 `0xffffffff` 的地址轴，标出这四段。
3. 标出复位后 PC 的第一个值，以及收到 `EXECUTE_REQ` 后 PC 跳到的值。

**需要观察的现象**：

- 引导 ROM（`0xfffee000`）与外设区（`0xffff0000`）相距很近但不重叠——这正是 u14-1 讲的 AXI 地址译码要把 ROM 地址减基地址转本地偏移的原因。
- 用户程序（`0x0`）和引导 ROM 在地址轴两端，互不干扰。

**预期结果**：得到一张清晰的 FPGA 启动地址布局图，能回答「复位 PC 是多少、下载完跳到哪、临时栈在哪、外设在哪」四个问题。

#### 4.2.5 小练习与答案

**练习 1**：假如用户程序的链接基址不是 0 而是 `0x10000`，`serial_boot` + `bootrom` 这套机制还能直接工作吗？

**参考答案**：不能直接工作。`start.S` 的 `jump_to_zero` 是硬编码跳到地址 0 的（`move s0, 0; b s0`）。若用户程序链接在 `0x10000`，跳到 0 就会取到错误指令。要让程序加载到 `0x10000`，需要同时改：① 用 `elf2hex -b 0x10000` 让 hex 第一段落在 `0x10000`；② 改 `start.S` 的跳转目标。这也解释了为何裸机程序约定链接到地址 0——它和引导 ROM 的「跳 0」约定是配套的。

**练习 2**：为什么让「线程 0 跑引导、其它线程跳过」？如果让所有线程都跑引导会怎样？

**参考答案**：如果所有线程都跑引导，会有多个线程同时通过同一个 UART 收发字节，互相抢数据，协议必然错乱。让唯一被复位的线程 0 独占串口引导，其它线程以后被唤醒时直接跳到已加载好的用户程序（地址 0），既避免了串口竞争，又不需要重复下载。

**练习 3**：`boot.c` 的 `main` 收到 `EXECUTE_REQ` 后只做了「关 LED + 回 ACK + return」。它有没有显式「跳转到用户程序的 main」？

**参考答案**：没有。它只是 `return 0` 回到 `start.S`，由 `start.S` 的 `jump_to_zero` 用 `b s0`（s0=0）完成跳转。引导程序对「用户程序长什么样、入口在哪」一无所知，它只负责把字节写到地址 0 然后跳 0。用户程序的入口（crt0 的 `_start` → `main`）是因为被链接到地址 0 才「恰好在 0」。

---

### 4.3 上板与仿真：三种运行环境的差异

#### 4.3.1 概念说明

Nyuzi 同一个程序可以在**三种环境**里跑，它们保真度从低到高、成本从低到高：

| 环境 | 是什么 | 程序怎么进内存 | 周期精确？ | 真实外设？ | 用什么脚本 |
| --- | --- | --- | --- | --- | --- |
| **C 模拟器** `nyuzi_emulator` | 用 C 写的指令集模拟器（ISS），只建模架构状态（见 u8-l1） | 直接读 hex 文件进内存数组 | ❌ 否 | 软件仿真 | `run_emulator` |
| **Verilator 仿真** `nyuzi_vsim` | 把 SystemVerilog RTL 编译成 C++ 跑，建模完整流水线与缓存 | `+bin=xxx.hex` 启动时载入地址 0 | ✅ 是 | testbench 假外设 | `run_verilator` |
| **FPGA 实物** DE2-115 | 真硬件，50MHz | `serial_boot` 经串口下程序 | ✅（就是真硬件） | 真实 SDRAM/VGA/UART/SD/PS2 | `run_fpga` |

这三种环境的**最大区别就在「程序怎么进内存」和「外设真不真」**：

- 模拟器和 Verilator 都是在**进程启动时**把整个 hex 文件一次性载入内存，然后从 0 执行——不需要引导 ROM、不需要串口、不需要握手。
- FPGA 实物的 SDRAM 上电是空的，**必须**靠 `serial_boot` + `bootrom` 这套串口协议把程序「喂」进去，然后才跳转执行。

这三套脚本都不是手写的，而是由 `cmake/nyuzi.cmake` 的 `add_nyuzi_executable` 宏在构建时用 `file(GENERATE)` **自动生成**的——所以三者参数对齐、可对比。

#### 4.3.2 核心流程

`add_nyuzi_executable` 为每个 Nyuzi 程序生成三个脚本，核心差异如下：

```
run_emulator:  <nyuzi_emulator> [显示/ramdisk/内存参数] <name>.hex
                 → 模拟器 main.c 用 read_hex_file 直接载入内存，从 0 执行

run_verilator: <nyuzi_vsim> [+block=fsimage] +bin=<name>.hex
                 → 仿真器读 +bin 参数，$readmemh 载入，从 RESET_PC(=0) 执行

run_fpga:      <serial_boot> $SERIAL_PORT <name>.hex [fsimage]
                 → 先 make synthesize + make program 把比特流烧进 FPGA
                 → 按复位键，引导 ROM 跑起来，LED 闪烁
                 → serial_boot 经串口下程序、校验、发 EXECUTE
                 → 程序在真硬件上跑，串口变成控制台
```

注意三个细节：

1. **`run_fpga` 依赖环境变量 `SERIAL_PORT`**（串口设备路径，如 `/dev/ttyUSB0`），因为它要把这个路径传给 `serial_boot`。前两个脚本不需要。
2. **ramdisk 的地址不一样**。`serial_boot` 把可选的 ramdisk 镜像下到固定地址 `0x4000000`（`RAMDISK_BASE`）；模拟器/Verilator 则用各自的 `-b` / `+block` 把文件挂成虚拟 SD/MMC 块设备（见 u8-l2）。这是两种完全不同的「附加数据」机制。
3. **FPGA 流程多了「综合 + 烧录」两步**，且每次改硬件配置（如核数）都要重新综合；而改软件程序只需重新 `run_fpga`，不必重烧比特流（只要板子不断电）。

#### 4.3.3 源码精读

**三个脚本的同源生成 —— `nyuzi.cmake`**。这是「三种环境差异」最权威的事实来源，三段 `file(GENERATE)` 写在一起，便于对比。

[cmake/nyuzi.cmake:91-92](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/cmake/nyuzi.cmake#L91-L92) 生成 `run_emulator`（直接喂 hex）；[第 103-104 行](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/cmake/nyuzi.cmake#L103-L104) 生成 `run_verilator`（用 `+bin`）；[第 115-116 行](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/cmake/nyuzi.cmake#L115-L116) 生成 `run_fpga`（`serial_boot $SERIAL_PORT name.hex [fsimage]`）。注意 `run_fpga` 的内容里 `$SERIAL_PORT` 是被原样写进脚本的 shell 变量引用，运行时才取值。

**仿真侧的载入方式 —— `hardware/README.md`**。Verilator 用 `+bin` 参数在启动时载入 hex。

[hardware/README.md:47](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/README.md#L47) 说明 `+bin=hexfile` 把文件载入地址 0；[第 64-65 行](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/README.md#L64-L65) 说明仿真器在「所有线程停机」时退出。这跟 FPGA 上「程序写控制寄存器停机」是同一个机制（见 u1-l4），只是退出动作发生在仿真进程里。

**FPGA 侧的上板步骤 —— `de2-115/README.md`**。这是「上板」的标准操作手册。

[hardware/fpga/de2-115/README.md:81-88](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/README.md#L81-L88) 给出 `make synthesize` + `make program` 两步；[第 94-101 行](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/README.md#L94-L101) 说明按 KEY0 复位、LED 闪烁后用 `run_fpga` 下程序；[第 105-108 行](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/README.md#L105-L108) 说明重载程序只需复位 + 再跑 `run_fpga`，不必重烧比特流。

**串口波特率约定**。`serial_boot` 与 `bootrom` 必须用同一个波特率，默认 921600，改的话要两边一起改。

[hardware/fpga/de2-115/README.md:31-38](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/README.md#L31-L38) 明确指出波特率定义在 `software/bootrom/boot.c` 与 `tools/serial_boot/serial_boot.c` 两个文件里，改后需重新综合设计并重建工具——这正是「FPGA 流程更重」的体现（改宿主代码要重编译工具，改目标代码要重新综合 ROM）。

**FPGA 的真实约束 —— 50MHz 与 USB 转串口**。

[hardware/fpga/de2-115/README.md:10](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/README.md#L10) 指出核跑在 50MHz（远低于仿真的 1GHz 时标）；[第 64-75 行](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/README.md#L64-L75) 脚注特别提醒：廉价 USB 转串口线用的 Prolific 芯片在大数据量传输时经常挂起，建议用 FTDI 芯片的线——这是真实硬件才有的「物理世界麻烦」，模拟器里完全不存在。

#### 4.3.4 代码实践

> **实践目标**：对比三种环境的运行脚本与前置条件，建立「什么时候用哪个」的判断。

**操作步骤（无需硬件）**：

1. 找一个已构建的 Nyuzi 程序目录（如 `tests/fpga/blinky` 的构建目录，或任意 `apps/hello_world` 构建目录），用编辑器打开自动生成的 `run_emulator`、`run_verilator`、`run_fpga` 三个脚本，对比它们的内容。若没有现成构建产物，直接阅读 [nyuzi.cmake:91-116](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/cmake/nyuzi.cmake#L91-L116) 推断三者内容。
2. 列一张表：每种环境需要哪些前置条件（环境变量、工具、硬件）、程序如何进内存、退出条件。

**操作步骤（若有 DE2-115 板子）**：

3. 按 [de2-115/README.md](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/README.md#L81-L101) 的步骤：`export SERIAL_PORT=...`、`make synthesize`、`make program`、按 KEY0 复位（观察 LED 0 闪烁）、`cd tests/fpga/blinky && run_fpga`。

**需要观察的现象**：

- 三个脚本里只有 `run_fpga` 引用了 `$SERIAL_PORT`，且只有它调用 `serial_boot`。
- 上板时 `serial_boot` 会打印进度条（`_loading [===...]`）和 `ping target...`，这正是 4.1 协议的可视化。
- 程序跑起来后 `serial_boot` 进入 `do_console_mode`，你的键盘输入会经串口发到 FPGA、FPGA 的串口输出会显示在终端——这就是 [serial_boot.c:338-398](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/serial_boot/serial_boot.c#L338-L398) 的双向桥（按 Ctrl-D 退出）。

**预期结果**：

- 能说清：日常开发先用 `run_emulator`（最快）跑通逻辑，再用 `run_verilator` 验证周期精确行为（缓存、流水线），最后才用 `run_fpga` 上真硬件验证外设与时序。
- 实跑 `run_fpga` 应看到 blinky 的 LED 闪烁效果。

> 若无硬件，本实践止步于步骤 1-2 的脚本对比，属「源码阅读型实践」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `run_emulator` 和 `run_verilator` 都不需要 `bootrom` 和 `serial_boot`，而 `run_fpga` 必需？

**参考答案**：模拟器和 Verilator 都是「宿主进程启动时就把整个 hex 文件一次性载入自己的内存空间」——前者用 C 的 `read_hex_file`，后者用 `$readmemh`。载入后直接从地址 0（或 `RESET_PC`）执行，内存「天生就有内容」，不需要引导。而 FPGA 的 SDRAM 上电是空的、且没有文件系统，只能靠 `serial_boot` 经串口、配合 `bootrom` 把字节一点点灌进去。引导 ROM 是「真实易失内存 + 无文件系统」环境下的必需品。

**练习 2**：`run_fpga` 脚本里 `$SERIAL_PORT` 为什么写成 shell 变量引用，而 hex 文件名是写死的绝对路径？

**参考答案**：hex 文件名在构建时就知道（`${CMAKE_CURRENT_BINARY_DIR}/${name}.hex`），所以直接内联；而串口设备路径因机器而异（`/dev/ttyUSB0`、`/dev/cu.usbserial` 等），构建时无法预知，所以留作运行时 shell 变量，由用户 `export SERIAL_PORT=...` 提供。这是把「与构建相关」和「与运行环境相关」的参数分开的常见做法。

**练习 3**：同一个程序在三种环境下的「停机」机制一样吗？

**参考答案**：软件层面的停机机制是一样的——都是程序（crt0 在 `main` 返回后）向控制寄存器 `CR_SUSPEND_THREAD`（编号 20）写 -1，清空 `thread_enable_mask`（见 u1-l4、u8-l1）。区别在于「所有线程停机后谁来观测并退出」：模拟器主循环发现没有使能线程就退出进程；Verilator 仿真器 likewise 退出；FPGA 上核会真的停下来（进入低功耗/挂起），但 `serial_boot` 不会自动退出——它停在 `do_console_mode` 等你按 Ctrl-D，因为宿主无法感知目标核已停机。

---

## 5. 综合实践

> **综合任务**：完整复述「一段 blinky 程序从你的电脑到 DE2-115 上 LED 闪烁」的全过程，把本讲三个模块串起来。

请按下列顺序，用自己的话写出每一步「谁在干什么、数据流向哪里」，并标注对应源码位置：

1. **构建阶段**：`elf2hex` 把 blinky 的 ELF 转成 `$readmemh` 格式的 hex 镜像；`add_nyuzi_executable` 生成 `run_fpga` 脚本（引用 [nyuzi.cmake:115-116](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/cmake/nyuzi.cmake#L115-L116)）。
2. **上板准备**：`make synthesize` 把含 `bootrom` 的设计综合成比特流，`make program` 烧进 FPGA（引用 [de2-115/README.md:81-88](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/README.md#L81-L88)）。
3. **复位启动**：按 KEY0，Nyuzi 核从 `RESET_PC = 0xfffee000` 取指，执行 `start.S`，线程 0 进 `boot.c` 的引导循环，LED 闪烁（引用 [start.S:32-39](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/bootrom/start.S#L32-L39)、[boot.c:42-59](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/bootrom/boot.c#L42-L59)）。
4. **串口下载**：`run_fpga` 调 `serial_boot`，ping 握手 → 把 hex 分块（全零块用 CLEAR、其余用 LOAD + FNV-1a 校验）→ 全部写入地址 0（引用 [serial_boot.c:709-758](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/serial_boot/serial_boot.c#L709-L758)、[boot.c:96-111](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/bootrom/boot.c#L96-L111)）。
5. **跳转执行**：`serial_boot` 发 `EXECUTE_REQ`，`boot.c` 回 ACK 后 return，`start.S` 跳到地址 0，blinky 的 crt0 → main 接管，控制 GPIO 点亮 LED（引用 [boot.c:122-127](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/bootrom/boot.c#L122-L127)）。
6. **控制台**：`serial_boot` 进入 `do_console_mode`，串口变成双向终端。

**验收标准**：你能不看讲义，对着一张白纸画出「宿主 serial_boot ↔ 串口 ↔ bootrom/FPGA」的数据流图，并指出 hex 字节在哪一步、以什么校验、写到哪个地址、最后靠哪条指令跳过去。如果某一步你只能含糊带过，就回到对应模块的「源码精读」重读。

## 6. 本讲小结

- **串口下载**是一对镜像：宿主侧 `serial_boot.c` 和目标侧 `boot.c` 用 `protocol.h` 共享的 9 个命令字节对话，靠 **ping 握手 + 分块（1024 字节）+ FNV-1a 校验和 + EXECUTE** 把 hex 镜像可靠地灌进 FPGA 内存；全零块用更便宜的 `CLEAR_MEMORY_REQ`，失败块靠 `fix_connection` 重同步后重发。
- **启动流程**的空间布局是「引导 ROM 在高地址 `0xfffee000`、用户程序在地址 0」；复位 PC = `BOOT_ROM_BASE`，`start.S` 让**线程 0 独占串口引导**、其它线程直接跳 0，引导结束后用 `b s0`（s0=0）跳进用户程序。
- **三种运行环境**（C 模拟器 / Verilator 仿真 / FPGA 实物）的根本差异是「程序怎么进内存」：前两者启动时一次性载入、不需要引导；FPGA 的 SDRAM 上电为空，**必须**靠 `serial_boot` + `bootrom` 串口下载。
- 三套运行脚本（`run_emulator`/`run_verilator`/`run_fpga`）由 `cmake/nyuzi.cmake` 同源自动生成，是三种环境差异的权威对照表；其中只有 `run_fpga` 依赖 `$SERIAL_PORT` 环境变量并调用 `serial_boot`。
- FPGA 流程比仿真「重」：改目标侧波特率需重新综合 ROM，改硬件配置（核数等）需重新综合比特流；真实硬件还有 50MHz 主频、USB 转串口芯片稳定性等物理约束，这些在模拟器里都不存在。
- `serial_boot` 在程序跑起来后进入 `do_console_mode`，把串口变成宿主终端与 FPGA 之间的双向桥（Ctrl-D 退出），这就是上板后与程序交互的通道。

## 7. 下一步学习建议

本讲讲完「上板」，FPGA SoC 与外设单元（u14）就完整了。接下来可以：

1. **回到验证体系（u15）**：现在你已理解三种运行环境，正好进入 u15-l1「测试框架与 CHECK 机制」，看 `test_harness.py` 如何让同一个测试在 emulator 与 verilator 两个目标上自校验——你会发现它本质上是把本讲的 `run_emulator`/`run_verilator` 脚本自动化、参数化了。
2. **深入调试链路**：本讲的串口既是下载通道也是控制台。如果想看「另一种」宿主↔目标通道，可读 u11-l1（JTAG 片上调试器）和 u11-l3（GDB 远程调试），对比串口下载与 JTAG 注入两种「从外部控制 Nyuzi 核」的方式。
3. **动手扩展（可选）**：若你有 DE2-115，尝试写一个最小程序（仅向 `REG_GREEN_LED` 写值），用 `run_fpga` 下进去验证；再尝试用 `serial_boot` 的可选 ramdisk 参数（地址 `0x4000000`）下一块数据，写程序读出来打印，体会「程序 + 附加数据」的下载方式。
4. **源码延伸阅读**：对比 [tools/emulator 的 `read_hex_file`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/)（模拟器侧的 hex 载入）与 [serial_boot.c 的 `read_hex_file`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/serial_boot/serial_boot.c#L428-L588)——两者都解析 `$readmemh` 格式，但一个把结果塞进内存数组、一个塞进待发送的段链表，正好印证「载入」与「下载」的分工。
