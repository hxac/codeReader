# Verilator 测试框架与组件测试台

## 1. 本讲目标

本讲带你深入 `sim/verilator` 目录，理解 ZipCPU 是如何用 **Verilator** 把 Verilog RTL 编译成 C++ 模型，再用一套 C++ 测试台（testbench）去驱动它的。读完本讲，你应当能够：

- 说清楚「一份 RTL → Verilator 模型 → 可执行模拟器」这条链路上的每一段由谁负责。
- 读懂 `zipcpu_tb.cpp` 的主流程：参数解析、ELF 加载、复位、放行、`tick()` 循环、成功/失败判定。
- 区分三种运行模式（`-a` 自动 / `-s` 单步 / 交互）以及它们对应的 Makefile 目标 `atest` / `stest` / `itest` / `test`。
- 理解 HALT 与 BUSY 在测试约定中分别代表什么，以及为什么组件测试台（`div_tb` / `mpy_tb` / `pfcache_tb`）要脱离整颗 CPU 单独跑。

本讲是「调试、验证、工具链」单元的验证篇，承接入门单元的 [u1-l4](u1-l4-first-simulation.md)（那里你已经跑通过 hello 程序），并把视角从「跑通」提升到「理解测试台内部机制，并能自己加测试用例」。

## 2. 前置知识

- **Verilator 是什么**：一个把 Verilog/SystemVerilog 源码「翻译」成 C++（或 SystemC）的开源工具。它不像商业仿真器那样解释执行 RTL，而是先编译成可链接的 C++ 类（如 `Vzipsystem`），再由你写的 C++ 主程序去驱动。结果是仿真速度快、但只能仿真**可综合**风格的行为（它对时序、X 值的处理比商业仿真器简单）。
- **测试台（testbench）**：在硬件仿真里，被测设计叫 DUT（Design Under Test）；测试台是包裹在 DUT 外面、负责喂时钟、喂激励、检查输出的程序。Verilator 模型本身不带时钟，时钟必须由测试台用 C++ 代码翻转。
- **ELF 与节（section）**：编译器/链接器把程序分成 `.text`（代码）、`.data`（已初始化数据）等「节」，每节有起始地址和长度。加载程序就是把每个节的内容填到模拟内存的对应地址。
- **调试端口**：在 [u5-l1](u5-l5-debug-interface-port.md) 之前你可能还没细读，本讲只需知道：ZipSystem/ZipBones 暴露了一组独立的调试从端口，测试台通过写它的命令寄存器（地址 0）来 HALT / STEP / RESET / GO 这颗 CPU。详见后续 [u5-l1](u5-l5-debug-interface-port.md)。
- **Wishbone 总线握手**：`cyc`（周期）/ `stb`（选通）/ `ack`（应答）/ `stall`（反压）/ `err`（错误）这组信号。模拟器里那块 RAM 就是一个最简的 Wishbone 从设备。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `sim/verilator/Makefile` | 编译入口。定义 `all` 构建哪些可执行模拟器，并提供 `stest`/`itest`/`test` 等测试目标。 |
| `sim/verilator/zipcpu_tb.cpp` | **整 CPU 测试台**。同一份源码靠 `-DZIPBONES` 宏编译出 `zipsys_tb`（ZipSystem）和 `zipbones_tb`（ZipBones）。 |
| `sim/verilator/zipaxil_tb.cpp` | AXI-Lite 顶层 `zipaxil` 的测试台，结构类似但独立。 |
| `sim/verilator/testb.h` | 测试台**基类模板** `TESTB<VA>`：负责 new 出模型、生成时钟 `tick()`、驱动复位、管理 VCD 波形。 |
| `sim/verilator/memsim.h` / `memsim.cpp` | 一个最简的 Wishbone RAM 模型，整 CPU 测试台里唯一的「外设」。 |
| `sim/verilator/div_tb.cpp` | **组件测试台**：脱离 CPU，单独验证除法单元 `div.v`。 |
| `sim/verilator/mpy_tb.cpp` | 组件测试台：单独验证乘法（在 `cpuops.v` 即 ALU 内）。 |
| `sim/verilator/pfcache_tb.cpp` | 组件测试台：单独验证指令缓存 `pfcache.v`。 |
| `bench/asm/simtest.s` | 默认的回归测试程序，`stest`/`itest`/`test` 加载的就是它编译出的 ELF。 |

## 4. 核心概念与源码讲解

### 4.1 从 RTL 到可执行模拟器：构建链路全景

#### 4.1.1 概念说明

要跑一个 Verilator 仿真，需要三类东西拼到一起：

1. **RTL 模型**：由 `rtl/` 的 Makefile 调用 Verilator，把 Verilog 编译成 `rtl/obj_dir/` 下的 C++ 库，如 `Vzipsystem__ALL.a`、`Vdiv__ALL.a`、`Vpfcache__ALL.a`。这是「设计的 C++化身」。
2. **测试台（C++）**：你写的驱动程序，负责时钟、激励、检查。它 `#include "Vzipsystem.h"` 拿到模型类。
3. **设备模型与胶水**：如 `memsim`（RAM）、`zipelf`（ELF 加载）、`testb.h`（基类）。

`sim/verilator/Makefile` 的工作就是把这三者编译链接成最终的可执行文件。

#### 4.1.2 核心流程

```
rtl/Makefile                    sim/verilator/Makefile
Verilog ──Verilator──> obj_dir/*.a  ──g++──> zipsys_tb / zipbones_tb / div_tb ...
                         ▲                          ▲
                         │                          │
                  (RTL 模型库)            testb.h + memsim + zipcpu_tb.cpp
```

- 顶层 `all` 目标列出了所有要产出的模拟器：

  [sim/verilator/Makefile:122](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/Makefile#L122) —— 一行 `all` 同时构建整 CPU 测试台和三个组件测试台。

- 测试台链接时，整 CPU 版需要 `Vzipsystem__ALL.a`（以及 ncurses、libelf），组件版只需对应的单模块库：

  [sim/verilator/Makefile:214-215](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/Makefile#L214-L215) —— `div_tb` 只链接 `Vdiv__ALL.a`，不依赖整颗 CPU。这正是「组件测试」的精髓：把一个模块从系统里摘出来单独验证。

- 关键约定（HALT 与 BUSY）写在 Makefile 的注释里，是整个测试体系的语义基础：

  [sim/verilator/Makefile:38-40](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/Makefile#L38-L40) —— 「测试在遇到 HALT 或 BUSY 指令时结束；HALT 表示成功，BUSY 表示失败」。

#### 4.1.3 源码精读：TESTB 基类模板

所有测试台都继承自 `TESTB<VA>`（`VA` 是 Verilator 模型类，如 `Vdiv`、`Vzipsystem`）。它把「生成时钟」这件最繁琐的事封装好：

[testb.h:56-67](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/testb.h#L56-L67) —— 构造时 `new VA` 创建模型，`CLOCK=0`，并调用 `eval()` 让组合逻辑先算出初值。

[testb.h:94-113](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/testb.h#L94-L113) —— `tick()` 是整个仿真的心跳。它先 `eval()`（让输入变化传播），再把 `CLOCK` 拉高 `eval()`、拉低 `eval()`，并在四个时刻把状态 dump 进 VCD 波形文件。一次 `tick()` = 一个时钟周期。

注意 `CLOCK` 与 `RESET` 是宏，默认展开成 `i_clk` / `i_reset`：

[testb.h:48-54](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/testb.h#L48-L54) —— 定义了 AXI 时钟名（`S_AXI_ACLK`）与默认时钟名（`i_clk`）的切换。

子类（如 `ZIPCPU_TB`、`DIV_TB`）只需重写 `tick()`，在调用 `TESTB<>::tick()` 之前喂激励、之后检查输出即可。

### 4.2 zipcpu_tb 测试台主流程

#### 4.2.1 概念说明

`zipcpu_tb.cpp` 是整颗 CPU 的测试台。它做了三件事：

1. 把一个 ELF 程序加载进模拟 RAM。
2. 通过调试端口让 CPU 复位、设好 PC、然后放行（GO）。
3. 不断 `tick()`，直到判定「成功」或「失败」。

一份源码同时服务 ZipSystem 和 ZipBones 两种顶层，靠编译宏切换。

#### 4.2.2 核心流程（ELF 加载 → 复位 → 放行 → tick 判定）

```
main()
 ├─ 解析命令行（-a 自动 / -s 单步 / 文件名）
 ├─ 若是 ELF：elfread() 取出各节 → tb->m_mem.load() 灌进 RAM
 ├─ reset()（TESTB 基类：拉一拍 i_reset）
 ├─ wb_write(CMD_REG, CMD_HALT|CMD_RESET|CMD_CATCH)   通过调试端口复位并暂停
 ├─ wb_write(CPU_sPC, entry)                           设 PC = ELF 入口
 ├─ wb_write(CMD_REG, CMD_GO|CMD_CATCH)                放行（或 STEP）
 └─ while(!done) { tb->tick(); done = success||failure||signalled; }
```

判定逻辑由 `test_success()` / `test_failure()` 给出，最终的 `SUCCESS!` / `TEST BOMBED` / `TEST FAILED` 在 `main` 末尾打印。

#### 4.2.3 源码精读

**① 一份源码，两种顶层。** `ZIPBONES` 宏决定包含哪个头文件、`SIMCLASS` 是哪个类：

[sim/verilator/zipcpu_tb.cpp:60-79](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L60-L79) —— 默认（无 `ZIPBONES`）走 `Vzipsystem`；Makefile 在编译 `zipbones_tb.o` 时加 `-DZIPBONES`（见 [Makefile:186-188](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/Makefile#L186-L188)），于是改成 `Vzipbones`。这样调试寄存器/外设相关的差异就能用 `#ifdef ZIPSYSTEM` 在同一份代码里处理。

**② 调试端口命令寄存器的位定义。** 整个测试台对 CPU 的控制都落到这几个常量上：

[sim/verilator/zipcpu_tb.cpp:89-102](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L89-L102) —— `CMD_REG`(0x00) 是控制段地址；`CMD_HALT`(bit0)、`CMD_STEP`(bit2)、`CMD_RESET`(bit3)、`CMD_GO`(0) 是写入命令，`CMD_GIE`(bit9) 等是读回的状态位。`CPU_sPC`(0x80+(15<<2)) 是写 supervisor PC 的地址。这些与 spec 的调试端口规范一一对应（后续 [u5-l1](u5-l5-debug-interface-port.md) 详讲）。

**③ RAM 布局。** 测试台只挂了一块 RAM，地址范围固定：

[sim/verilator/zipcpu_tb.cpp:205-208](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L205-L208) —— `RAMBASE = 1<<28 = 0x10000000`，即 256 MiB 处；程序必须链接到这里（回顾 [u1-l4](u1-l4-first-simulation.md) 的 `board.ld` 约定）。

**④ ELF 加载。** `main` 识别 ELF 文件后，逐节灌入内存：

[sim/verilator/zipcpu_tb.cpp:2251-2267](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L2251-L2267) —— `elfread()` 返回若干 `ELFSECTION`，每节带 `m_start`/`m_len`/`m_data`；三个 `assert` 保证节落在 `[RAMBASE, RAMBASE+RAMWORDS)` 内且长度按字对齐。`m_mem.load()` 把字节流填进 RAM 模型。

> 说明：如果传入的不是 ELF（例如旧式原始二进制），会走 `usage()` 提示。当前回归测试用的 `simtest` 是 ELF。

**⑤ tick() —— 仿真心跳与总线驱动。** 这是测试台最核心的方法，子类 `ZIPCPU_TB::tick()` 先喂总线激励，再调基类 `tick()` 推进时钟，最后做检查与日志：

[sim/verilator/zipcpu_tb.cpp:1320-1329](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L1320-L1329) —— 每拍先算出 CPU 想访问的字地址；若地址不在 RAM 段内，就拉 `i_wb_ack`+`i_wb_err` 并把 `m_bomb` 置位（这是「CPU 跑飞访问非法地址」的主要失败检测）。

[sim/verilator/zipcpu_tb.cpp:1464-1468](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L1464-L1468) —— 把 CPU 的 Wishbone 主端口信号（`o_wb_cyc/stb/we/addr/data/sel`）喂给 `MEMSIM`，并把 RAM 回送的 `i_wb_ack/stall/data` 接回模型；随后才调用 `TESTB<SIMCLASS>::tick()` 推进一拍。

[sim/verilator/zipcpu_tb.cpp:1470-1472](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L1470-L1472) —— 若 CPU 执行了 `SIM` 指令（`cpu_sim` 有效），调用 `execsim()` 解释它的立即数。这是程序「主动通知测试台」的带外通道。

**⑥ SIM 指令 = 程序与测试台的约定。** `execsim()` 解析立即数，识别多种「SIM Exit」编码：

[sim/verilator/zipcpu_tb.cpp:1986-1993](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L1986-L1993) —— `SIM Exit(0)` 编码把 `m_exit=true`、`m_rcode=0`；其它变体（带寄存器号、带立即数）从对应寄存器取返回码。这就是 u1-l4 里说的「`_exit → NEXIT/HALT → SUCCESS`」链路在测试台一侧的落点。

**⑦ 成功 / 失败判定。**

[sim/verilator/zipcpu_tb.cpp:1633-1640](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L1633-L1640) —— `test_success()` 为真有两种情况：(a) 程序执行了 `SIM Exit(0)`（`m_exit && rcode==0`）；(b) CPU 处于监管态且已睡眠（`!r_gie && r_sleep`）。情况 (b) 正是程序执行 **HALT** 指令的结果——HALT 让 CPU 睡眠，故 HALT = 成功。

[sim/verilator/zipcpu_tb.cpp:1730-1738](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L1730-L1738) —— `test_failure()` 仅在 `SIM Exit(非0)` 时为真。

把这套判定和 Makefile 的约定对照：**HALT → r_sleep → test_success → "SUCCESS!"**；**BUSY** 指令则让 CPU 在原地空转、永不睡眠，测试台既等不到成功也（在没有触发 m_bomb 时）不会自然结束——因此在测试设计上 BUSY 被用来表示「卡死/失败」，配合非法访问触发的 `m_bomb` 一起，构成失败出口。

**⑧ 最终结论。** `main` 末尾根据状态打印结论：

[sim/verilator/zipcpu_tb.cpp:2527-2536](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L2527-L2536) —— `m_bomb` 真 → `TEST BOMBED`（rc=-1）；`test_success` → `SUCCESS!`；`test_failure` → `TEST FAILED`（rc=-2）；否则 `User quit`。注意脚本调用方正是靠这个返回码（0=成功，非 0=失败）做 CI 判定。

**⑨ IPC 报告。** ZipSystem 配置下，`main` 还会从性能计数器读出时钟数与指令数，算出 IPC：

[sim/verilator/zipcpu_tb.cpp:2517-2523](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L2517-L2523) —— 读 `mtc_data`（master 时钟计数器）与 `mic_data`（master 指令计数器，回顾 [u4-l5](u4-l5-peripherals-timer-counter-irq.md) 的 zipcounter）。

\[ \mathrm{IPC} = \frac{\text{mic\_data（已执行指令数）}}{\text{mtc\_data（已用时钟数）}} \]

IPC 越接近 1 说明流水线越顺畅、停顿越少（回顾 [u3-l7](u3-l7-hazards-writeback-stalls.md) 的冒险与停顿）。这是 Dhrystone 等基准评测的关键指标。

#### 4.2.4 代码实践：阅读 `wb_write`，理解调试端口写时序

1. **实践目标**：搞清楚「测试台写一个调试端口寄存器」在 C++ 层面到底做了哪些 Wishbone 握手。
2. **操作步骤**：
   - 打开 [sim/verilator/zipcpu_tb.cpp:1740-1775](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L1740-L1775)。
   - 逐行跟踪：置 `i_dbg_cyc=1, i_dbg_stb=1, i_dbg_we=1, i_dbg_addr=(a>>2), i_dbg_data=v`；然后 `while(o_dbg_stall) tick()` 等从设备不忙；再 `tick()` 锁存；接着 `i_dbg_stb=0`，`while(!o_dbg_ack) tick()` 等应答；最后撤下 `cyc/stb` 再 `tick()` 释放总线。
3. **需要观察的现象**：写一个寄存器要消耗多拍（至少请求一拍 + 等应答数拍），期间 CPU 在「真实」地走时钟。
4. **预期结果**：你能解释为什么 `reset()` 之后那串 `wb_write(CMD_REG, ...)` 本身就要花掉不少 tick——它们不是瞬时完成的，而是真正的总线交易。
5. **待本地验证**：若要看到精确拍数，可在 `wb_write` 内对 `errcount` 计数并打印。

#### 4.2.5 小练习与答案

**练习 1**：`test_success()` 里 `(!m_core->r_gie) && m_core->r_sleep` 这一项，为什么要求 `r_gie==0`（必须在监管态）？

**参考答案**：HALT 是一条特权/监管指令，执行后 CPU 停在监管态并睡眠；若用户态也能睡眠，一个用户程序就能把整机挂死。因此成功判定要求「监管态 + 睡眠」同时成立，避免把用户态的 SLEEP 误判为测试成功。

**练习 2**：为什么 `simtest` 失败时常用 BUSY 指令，而不是直接 `SIM Exit(1)`？

**参考答案**：BUSY 让 CPU 原地空转，既不睡眠也不退出，测试台永远等不到 `test_success`，运行会「卡住」而非干净返回——这是早期测试约定的失败信号。现代程序更推荐用 `SIM Exit(非0)`（会被 `test_failure()` 捕获并干净返回非 0 码），但历史测试和 Makefile 注释仍沿用 HALT/BUSY 的二分约定。

### 4.3 三种运行模式与 Makefile 的 stest / itest / test 目标

#### 4.3.1 概念说明

同一份 `zipsys_tb` 可执行文件支持三种「驱动 CPU」的方式，由命令行参数选择：

| 参数 | 模式 | 含义 |
|------|------|------|
| `-a` | autorun（自动） | 复位后 GO，让 CPU 全速跑，每拍 `tick()` 判定终止。 |
| `-s` | autostep（单步） | 不全速跑，而是每轮都通过调试端口写 `CMD_STEP` 推进一条指令。 |
| （无） | interactive（交互） | 进入 ncurses 全屏界面，用键盘 h/g/s/r/q 等键手动控制，适合调试。 |

`-s` 模式模拟的是「CPU 被装进一个真实设备、外部调试器一条一条单步」的使用场景；`-a` 模式追求快、用于回归测试。

#### 4.3.2 核心流程

三种模式的代码在 `main` 里是三个并列分支：

- **autorun**（`-a`）：[zipcpu_tb.cpp:2276-2305](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L2276-L2305) —— 写 `CMD_HALT|CMD_RESET` → 写 PC → 等复位释放（`while(CMD_RESET)`）→ 写 `CMD_GO` 放行 → 循环 `tick()` 直到 `done`。
- **autostep**（`-s`）：[zipcpu_tb.cpp:2306-2327](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L2306-L2327) —— 同样复位 + 设 PC + GO，但主循环里每轮额外写一次 `CMD_STEP|CMD_CATCH`，由 `step()` 方法发出（[zipcpu_tb.cpp:306-309](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L306-L309)）。
- **interactive**（无）：[zipcpu_tb.cpp:2328-2487](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L2328-L2487) —— `initscr()` 进 ncurses，键盘事件分发：`h`=HALT、`g/G`=GO、`s`=step、`t`=单 tick、`r`=reset、`q`=quit。

#### 4.3.3 源码精读：Makefile 目标

Makefile 把上面三种调用方式封装成目标，默认测试程序是 `bench/asm/simtest` 编译出的 ELF：

[sim/verilator/Makefile:175](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/Makefile#L175) —— `TESTF := ../../bench/asm/simtest` 指向回归程序。

[sim/verilator/Makefile:229-238](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/Makefile#L229-L238) —— `atest`/`stest`/`itest` 分别用 `-a`/`-s`/无参数调用 `zipsys_tb $(TESTF)`。注意 `stest` 跑的就是「单步模式回归」。

[sim/verilator/Makefile:264-265](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/Makefile#L264-L265) —— `test` 目标把 ZipSystem、ZipBones、ZipAXILite 三种顶层 × atest+stest 全部串起来跑一遍，是最完整的回归入口。

> 补充：`simtest` 程序本身在 `bench/asm/Makefile` 里构建，因为源文件含 `#define/#ifdef`，需要先过 C 预处理器再汇编（[bench/asm/Makefile:120-126](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/asm/Makefile#L120-L126)）。所以 `make stest` 前，`simtest` 这个 ELF 必须先存在。

#### 4.3.4 代码实践：运行 `make stest` 并解释 HALT / BUSY

1. **实践目标**：亲手跑一次单步回归，并用自己的话讲清 HALT 与 BUSY 的语义。
2. **操作步骤**：
   1. 确保已按 [INSTALL.md](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/INSTALL.md) 装好 Verilator，并已完成 `make rtl`（产出 `rtl/obj_dir/`）。
   2. 先到 `bench/asm` 执行 `make simtest` 生成 `simtest` ELF（`make stest` 不会自动帮你建它）。
   3. 回到 `sim/verilator` 执行 `make zipsys_tb`（构建模拟器）再 `make stest`。
   4. 观察终端输出，关注结尾的 `SUCCESS!` / `TEST BOMBED` / `TEST FAILED` 与返回码。
3. **需要观察的现象**：单步模式下，测试台每推进一条指令都要走一次完整的调试端口写（`CMD_STEP`）；因此 `stest` 比 `atest` 慢很多。运行结束时 `Clocks used` / `Instructions Issued` / `Instructions / Clock` 三行会打印（仅 ZipSystem）。
4. **预期结果**：
   - 程序正常跑完 → 末行 `SUCCESS!`，进程返回码 0；
   - HALT 的作用 = 让 CPU 在监管态睡眠 → `test_success()` 真 → 打印 `SUCCESS!`；
   - BUSY 的作用 = 让 CPU 原地空转、永不睡眠，表示失败/卡死（脚本里靠「没有 SUCCESS」即判定失败）。
5. **待本地验证**：若 `make stest` 报找不到 `simtest`，说明上一步 `bench/asm` 的 ELF 未生成；若运行卡住不退出，多半是触发了 BUSY 空转，可用 Ctrl-C 中断（测试台注册了 SIGINT 处理，[zipcpu_tb.cpp:2210-2214](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L2210-L2214)）。

#### 4.3.5 小练习与答案

**练习 1**：`stest`（单步）和 `atest`（自动）跑同一个 `simtest`，最终判定结果会不同吗？为什么我们两种都要？

**参考答案**：功能正确时二者结果一致（都应 `SUCCESS!`）。两种都要，是因为它们走的是不同的调试路径：`atest` 让 CPU 全速跑（验证正常运行），`stest` 每条指令都经调试端口单步（验证调试接口的 STEP 机制本身）。一个 bug 可能让全速跑正常、单步异常，反之亦然，故需双重覆盖。

**练习 2**：`make test`（[Makefile:264-265](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/Makefile#L264-L265)）依赖了哪些可执行文件？为什么要把三种顶层都跑一遍？

**参考答案**：依赖 `zipsys_tb`、`zipbones_tb`、`zipaxil_tb` 三个模拟器，并对每个跑 `atest`+`stest`。三种顶层封装（回顾 [u1-l3](u1-l3-rtl-top-wrappers.md)）包裹同一个内核但总线/外设配置不同，三者都通过才能保证内核在各种封装下都正确。

### 4.4 组件测试台：以 div_tb 为例

#### 4.4.1 概念说明

整 CPU 测试台（`zipsys_tb`）能验证「端到端」的正确性，但它把太多东西搅在一起：一旦失败，你不知道是除法器、乘法器、缓存还是流水线冒险的锅。所以 ZipCPU 还为关键模块各写了一个**组件测试台**，把模块单独摘出来、用大量边界值狂轰滥炸：

- `div_tb` → 除法单元 `div.v`（[u3-l5](u3-l5-multiply-divide.md)）
- `mpy_tb` → 乘法（在 ALU `cpuops.v` 内）
- `pfcache_tb` → 指令缓存 `pfcache.v`（[u3-l2](u3-l2-prefetch-family.md)）

它们不加载 ELF、不走调试端口，而是直接在 C++ 里设置输入、读输出、用 `assert` 卡死式地检查正确性。

#### 4.4.2 核心流程（div_tb 的一次除法测试）

```
divtest(n, d, ans, issigned):
 ├─ 断言进来时 o_busy==0（必须空闲）
 ├─ 设置 i_wr=1, i_signed, i_numerator=n, i_denominator=d
 ├─ tick()（锁存请求）
 ├─ 清输入；断言 o_busy 已升起、o_valid 为 0
 ├─ while(!o_valid) { 断言 o_busy；tick(); }   等结果
 ├─ 断言 o_busy 已落下
 └─ 检查：
     · d==0 → 必须有 o_err（除零）
     · 否则 → o_quotient 必须等于 ans
     · Z 标志位必须与「商是否为 0」一致
```

#### 4.4.3 源码精读

**① 只链接单模块库。** Makefile 里 `div_tb` 的链接行只引用 `Vdiv__ALL.a`：

[sim/verilator/Makefile:214-215](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/Makefile#L214-L215) —— 不需要整颗 CPU，编译快、定位问题精准。`mpy_tb`（[Makefile:211-212](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/Makefile#L211-L212)）链接 `Vcpuops`、`pfcache_tb`（[Makefile:220-222](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/Makefile#L220-L222)）链接 `Vpfcache`，模式一致。

**② 请求 → 忙 → 有效 的握手。** `divtest` 把 [u3-l5](u3-l5-multiply-divide.md) 讲的「`i_stb/i_wr` → `o_busy` → `o_valid`」三阶段握手原样翻译成断言：

[sim/verilator/div_tb.cpp:146-153](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/div_tb.cpp#L146-L153) —— 拉高 `i_wr` 并给出操作数，`tick()` 一拍让请求被锁存。

[sim/verilator/div_tb.cpp:173-186](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/div_tb.cpp#L173-L186) —— 等待循环：在 `o_valid` 拉起前，每拍都断言 `o_busy` 必须为真（模块要么在算、要么该给结果，不允许「既不忙也无效」的中间态）。

**③ 结果校验，含除零。** 出结果后分情况检查：

[sim/verilator/div_tb.cpp:207-233](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/div_tb.cpp#L207-L233) —— 除数为 0 时必须报 `o_err`；否则商必须等于预期 `ans`；任何错都会先 `closetrace()`（把最后一拍写进 VCD）再 `assert` 崩溃，方便事后看波形。

[sim/verilator/div_tb.cpp:235-239](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/div_tb.cpp#L235-L239) —— 额外检查 Z 标志：商为 0 时 `o_flags` 的 Z 位必须为 1，反之必须为 0。

**④ 期望值由 C 原生除法算出。** `divs`/`divu` 用宿主机的 `/` 算出 `ans` 再传入，等于拿 C 编译器的除法当「黄金参考」：

[sim/verilator/div_tb.cpp:243-265](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/div_tb.cpp#L243-L265) —— 注意 C 的整数除法向零截断（`-15/4 == -3`），与 ZipCPU 硬件除法语义一致，故可直接对比。

**⑤ 大规模边界扫描。** `main` 里先跑一批「温和 + 边界」用例，再用三个 `for` 循环各跑 32768 次：

[sim/verilator/div_tb.cpp:300-343](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/div_tb.cpp#L300-L343) —— 含 `(1u<<31)`（最大无符号/最小有符号）、除零、`±15/±1`、`±15/±4`，以及上万次扫描。全部通过才打印 `SUCCESS!`（[div_tb.cpp:365](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/div_tb.cpp#L365)）。

**⑥ pfcache_tb 的对比。** 取指缓存的测试台思路类似但激励不同：它用 `/dev/urandom` 随机填充内存，然后组合「顺序取指 / 跳转 / 跳过（模拟 CIS）/ 随机混合」四种模式，并断言每条取回的指令都等于内存里对应地址的内容：

[sim/verilator/pfcache_tb.cpp:158-171](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/pfcache_tb.cpp#L158-L171) —— 每次 `o_valid` 时拿 `o_pc/o_insn` 与 `m_mem[pc>>2]` 比对，不一致即崩溃。它还检查「只读、不写、地址合法」等契约（[pfcache_tb.cpp:134-145](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/pfcache_tb.cpp#L134-L145)）。

#### 4.4.4 代码实践：为 div_tb 设计并加入一个新测试用例

1. **实践目标**：在不破坏现有测试的前提下，给 `div_tb` 增加一个有针对性的边界用例，体验「加测试」的完整流程。
2. **操作步骤**：
   1. 选一个现有用例没覆盖到的组合。建议验证**有符号除法的向零截断**与**无符号的大数除法**：
      - 有符号：`-100 / 7`，C 语言 `-100 / 7 == -14`（向零截断，不是 -15）。
      - 无符号：`100 / 7 == 14`，`0xFFFFFFFF / 10 == 429496729`。
   2. 打开 [sim/verilator/div_tb.cpp:300-318](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/div_tb.cpp#L300-L318)，在「Some other gentle tests」区段插入两行（每个用例之间用 `tb->tick();` 隔开，与现有风格一致）：

      ```cpp
      // 示例代码：新增的有符号截断与大数除法用例
      tb->divs(-100, 7);  tb->tick();   // 预期商 = -14（向零截断）
      tb->divu(100u, 7u); tb->tick();   // 预期商 = 14
      tb->divu(0xFFFFFFFFu, 10u); tb->tick(); // 预期商 = 429496729
      ```

      说明：`divs(n,d)` 内部已用 C 的 `n/d` 算好期望值并传给 `divtest`（[div_tb.cpp:243-249](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/div_tb.cpp#L243-L249)），所以你只需给出输入，无需手写期望商。
   3. 重新编译运行：`make div_tb && ./div_tb`。
3. **需要观察的现象**：若硬件除法实现正确，程序照常走到末尾打印 `SUCCESS!`；若你故意把期望搞错（例如改成 `tb->divu(100u,7u)` 但在 `divu` 里写错期望——不建议真改 `divu`），`assert` 会先 `closetrace()` 再崩溃，并留下 `div_tb.vcd` 波形。
4. **预期结果**：新增用例不影响 `SUCCESS!`；若想观察失败长什么样，可临时把 `-100/7` 改成一个会截断方向相反的「错误期望」来触发断言（验证完记得改回）。
5. **待本地验证**：实际除法周期数与 `div.v` 的 `OPT_DIV` 实现有关（回顾 [u3-l5](u3-l5-multiply-divide.md)），本实践只验证功能正确性，不验证周期数。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `div_tb` 用 C 的 `/` 算期望值是安全的？万一 C 编译器的除法和 ZipCPU 硬件除法语义不同怎么办？

**参考答案**：二者都遵循「向零截断（truncation toward zero）」的整数除法语义，这是 C 标准与 ZipCPU spec 共同约定的，所以可直接对比。真正需要小心的是「余数符号」「INT_MIN / -1 溢出」等极端情形——`div_tb` 的边界扫描（含 `1u<<31`、除零）正是为了覆盖这些。若语义不一致，对比就会失败，这本身就是测试的价值。

**练习 2**：组件测试台和整 CPU 测试台是「替代关系」还是「互补关系」？

**参考答案**：互补。组件测试台（`div_tb` 等）覆盖面窄但深——能对单模块做上万次边界扫描，定位精准；整 CPU 测试台（`zipsys_tb`）覆盖面广但浅——验证模块在真实流水线、真实总线下的协作。前者抓「模块本身错」，后者抓「集成/交互错」，缺一不可。

## 5. 综合实践

把本讲的三条主线串起来，完成下面的「读 + 跑 + 改」小任务：

1. **读**：从 `make stest` 出发，沿着 `Makefile` 的 `stest` 目标（[Makefile:232-234](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/Makefile#L232-L234)）→ `zipsys_tb -s simtest` → `main` 的 autostep 分支（[zipcpu_tb.cpp:2306-2327](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L2306-L2327)）→ `step()`（[zipcpu_tb.cpp:306-309](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L306-L309)）→ `wb_write`（[zipcpu_tb.cpp:1740-1775](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L1740-L1775)）→ `tick()`（[zipcpu_tb.cpp:1311-1631](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L1311-L1631)）→ `test_success`（[zipcpu_tb.cpp:1633-1640](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L1633-L1640)），画出一张「一次单步回归的调用链」时序草图，标注每一步走的是调试端口还是主总线。
2. **跑**：依次执行 `make zipsys_tb`、`make div_tb`、（在 `bench/asm` 里）`make simtest`、（回 `sim/verilator`）`make stest`、`./div_tb`。记录每个命令的产物与结尾结论行。
3. **改**：按 4.4.4 给 `div_tb` 加一个 `divs(-100, 7)` 用例，重编译运行，确认仍是 `SUCCESS!`；再打开 `div_tb.vcd`（用 GTKWave 或文本查看）找到这次除法对应的 `o_busy`→`o_valid` 时段，估算它花了多少个时钟周期，与 [u3-l5](u3-l5-multiply-divide.md) 给出的「无符号约 33 拍、有符号约 33–35 拍」对照。

> 若无法本地构建（缺 Verilator 等），请把第 2、3 步标注为「待本地验证」，重点完成第 1 步的源码阅读与草图。

## 6. 本讲小结

- Verilator 把 Verilog RTL 编译成 C++ 库（`rtl/obj_dir/V*__ALL.a`），测试台（`sim/verilator/*.cpp`）链接这些库得到可执行模拟器；`Makefile` 的 `all` 目标列出全部产物。
- `zipcpu_tb.cpp` 一份源码靠 `-DZIPBONES` 宏同时服务 ZipSystem 与 ZipBones；主流程是「ELF 加载 → 调试端口复位/设 PC/放行 → `tick()` 循环 → 成功/失败判定」。
- 三种模式 `-a`/`-s`/交互 对应 Makefile 的 `atest`/`stest`/`itest`；`test` 目标把三种顶层 × 两种自动模式全跑一遍做完整回归。
- 约定上 **HALT = 成功**（CPU 在监管态睡眠，`test_success()` 真）、**BUSY = 失败**（原地空转永不睡眠）；非法访问触发 `m_bomb` 也是一种失败出口。
- 组件测试台（`div_tb`/`mpy_tb`/`pfcache_tb`）只链接单模块库，用大量边界值 + `assert` 把单个模块摘出来深测，与整 CPU 测试台互补。
- ZipSystem 配置下测试台还会打印 IPC（指令数/时钟数），是评估流水线效率的关键指标。

## 7. 下一步学习建议

- 想了解「调试端口命令寄存器」的完整规范与 `zipdbg` 调试器，继续读 [u5-l1 调试接口与调试端口寄存器](u5-l1-debug-interface-port.md)——本讲的 `CMD_REG`/`CMD_HALT`/`CMD_STEP` 都在那里有权威定义。
- 想了解「不基于 RTL、纯 C++ 解释指令」的另一套模拟器，读 [u5-l4 C++ 指令级模拟器（ISS）](u5-l4-cpp-iss-simulator.md)，对比它和 Verilator 模拟器在速度与保真度上的取舍。
- 想了解「不跑输入、用数学证明验证模块」的方法，读 [u5-l2 形式化验证体系（SymbiYosys）](u5-l2-formal-verification.md)——那里的 `fwb_master/slave` 与本讲的组件测试台是互补的两种验证哲学。
- 对被测模块本身（除法/乘法/取指缓存）的算法细节感兴趣，回看 [u3-l2](u3-l2-prefetch-family.md)、[u3-l5](u3-l5-multiply-divide.md)。
