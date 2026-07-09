# tpu_forward 寄存器时序与完成轮询

## 1. 本讲目标

本讲是「推理执行与结果读取」单元的第一讲，承接 u4-l2 已经建立的 EEPTPU_SA 类与 TPU 寄存器协议，把视线从「有哪些寄存器」推进到「**一次推理在时间轴上到底发生了什么**」。

读完本讲你应该能够：

- 说出 `tpu_forward()` 在寄存器层面的完整时序：先写哪几个寄存器、写什么值启动、轮询哪个寄存器的哪一位判定完成。
- 精确掌握启动寄存器 `STARTUP = 0x11` 与完成状态位 `STATUS & 0x80000000` 的含义。
- 区分两套计时：软件 `XTime`（ARM 全局定时器，量的是墙钟时间）与硬件 `EEPTPU_RUNTIMER_REG`（TPU 内部计时器，量的是纯计算时间）。
- 解释为什么 `tused_forward` 通常大于 `EEPTPU_RUNTIMER_REG`，并能指出哪个更贴近「纯 TPU 计算耗时」。

## 2. 前置知识

本讲假设你已掌握 u4-l1（裸机工程结构与 `main.cc` 菜单）、u4-l2（EEPTPU_SA 类与 TPU 寄存器协议）、u4-l3（`EEP_INTERFACE` 的 AXI 读写与 `register_wait`）的内容。下面只做最简回顾，不展开。

- **裸机驱动模型**：在裸机（无操作系统）下，TPU IP 的寄存器被映射成一段物理地址，ARM CPU 把它当普通内存读写。寄存器块基址为 `0xA0000000`（控制通路），权重/输入/输出张量在 DDR 上（数据通路，基址 `0x31000000`）。
- **寄存器协议一句话**：写若干「基地址寄存器」告诉 TPU 张量在哪 → 写「算法地址寄存器」告诉它调度表在哪 → 写「启动寄存器」踢一脚 → 轮询「状态寄存器」的完成位直到 TPU 干完。
- **关键术语**：
  - `volatile`：告诉编译器「这个内存地址是硬件寄存器，别优化掉我对它的读写」，轮询循环必须用它。
  - 忙等（busy-wait）/ 自旋（spin）：CPU 不睡眠、不阻塞，反复读寄存器直到条件满足。裸机下没有操作系统调度，忙等是最简单的等待方式。
  - `XTime`：Xilinx BSP 提供的 64 位 ARM 全局定时器类型，用来做软件侧高精度计时。

## 3. 本讲源码地图

本讲聚焦三个文件：

| 文件 | 作用 |
| --- | --- |
| `sdk/standalone/src/main.cc` | 裸机主程序。包含**实际被菜单调用**的 `tpu_forward()`（全局函数）以及 case `'2'`/`'5'` 里围绕它的软件计时 `XTime` 与硬件计时 `RUNTIMER` 读取。 |
| `sdk/standalone/src/config.h` | 把 TPU 寄存器偏移定义为 `volatile` 宏（`EEPTPU_STARTUP_REG`、`EEPTPU_STATUS_REG`、`EEPTPU_RUNTIMER_REG` 等）。 |
| `sdk/standalone/src/eeptpu/eeptpu_sa.cpp` | EEPTPU_SA 类的实现。提供**等价但更规整**的协议封装 `eep_tpu_start_work()` / `eep_tpu_wait_done()` / `forward()`，便于理解 `bin_type` 分支。 |

> 重要事实：本工程里菜单（case `'2'` / `'5'`）实际调用的是 `main.cc::tpu_forward()`，**不是** `EEPTPU_SA::forward()`（后者在当前 `main.cc` 中没有被调用，但它是同一段协议的面向对象写法，读它有助于理解 `bin_type` 分支）。本讲两者都讲，并明确区分。

## 4. 核心概念与源码讲解

### 4.1 启动寄存器写入

#### 4.1.1 概念说明

TPU 不是一条指令一条指令地被驱动的，而是「**配置 + 触发**」式（kick-off）的协处理器：软件先把这次推理用到的四类 DDR 内存段的基地址、调度表地址写进控制寄存器，最后往「启动寄存器」写一个魔数，TPU 看到这个魔数才真正开始算。

这四类内存段对应 u3-l3 讲过的 `bin_type=2`（pub）的四段：

- `par`（hwbase0）：网络参数/权重区。
- `in`（hwbase1）：输入张量区。
- `tmp`（hwbase2）：中间张量（临时）区。
- `out`（hwbase3）：输出张量区。

外加 `addr_alg`：算法调度表地址（告诉 TPU 「按这张表里的算子顺序与配置执行」）。

#### 4.1.2 核心流程

启动一次推理的寄存器写入时序（`bin_type=2` 情形）：

```text
1. BASEADDR0(0x50) ← hwbase0   (par  段基地址)
2. BASEADDR1(0x54) ← hwbase1   (in   段基地址)
3. BASEADDR2(0x58) ← hwbase2   (tmp  段基地址)
4. BASEADDR3(0x5C) ← hwbase3   (out  段基地址)
5. ALGOADDR (0x30) ← addr_alg  (算法调度表地址)
6. STARTUP  (0x34) ← 0x11      (踢一脚：开始执行)
```

写成伪代码：

```
write_reg(0x50, hwbase0)
write_reg(0x54, hwbase1)
write_reg(0x58, hwbase2)
write_reg(0x5C, hwbase3)
write_reg(0x30, addr_alg)
write_reg(0x34, 0x11)          // ← 启动触发
```

> 第 6 步的 `0x11` 是「启动一次推理」的命令值；其具体位语义由加密 TPU 硬件定义，软件侧只需照写（待确认其逐位含义）。

#### 4.1.3 源码精读

寄存器宏定义，全部以基址 `0xA0000000` 加偏移、并声明为 `volatile`：

[config.h:28-35](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L28-L35) —— 定义 `EEPTPU_RUNTIMER_REG`、`EEPTPU_BASEADDR0~3_REG`、`EEPTPU_ALGOADDR_REG`、`EEPTPU_STARTUP_REG`、`EEPTPU_STATUS_REG`。`volatile` 保证每次访问都真正落到硬件。

实际被菜单调用的 `tpu_forward()`，固定写四个基地址（pub 风格）：

[main.cc:195-220](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L195-L220) —— 依次写 `BASEADDR0~3`、`ALGOADDR`，最后 `EEPTPU_STARTUP_REG=0x11` 触发。每个基地址取自 `eeptpu_init()` 解析出的 `hwbase0~3` / `addr_alg`（见 main.cc:298-302 的赋值）。

类方法 `eep_tpu_start_work()` 是同一段时序的面向对象版本，并按 `bin_type` 分支：

[eeptpu_sa.cpp:255-283](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L255-L283) —— `bin_type==1`(enc) 只写 2 个基址；`bin_type==2`(pub) 写 4 个基址（par/in/tmp/out）；随后统一写 `ALGOADDR(0x30)` 与 `STARTUP(0x34)=0x11`。

注意两种实现的差异：`main.cc::tpu_forward()` 无条件写 4 个基地址（因为本工程固定用 yolov4-tiny 的 pub bin），而类方法 `eep_tpu_start_work()` 会根据 `bin_type` 自适应。这就是为什么读类方法能帮你理解「enc 与 pub 的区别」，而读 `tpu_forward()` 能帮你理解「当前菜单实际跑的是什么」。

#### 4.1.4 代码实践

**实践目标**：搞清一次推理启动时，软件到底往 TPU 写了哪几个寄存器、写的值从哪来。

**操作步骤（源码阅读型）**：

1. 打开 [main.cc:298-302](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L298-L302)，确认 `waddr / sd_input_addr / tpu_hwbase2 / tpu_hwbase3 / tpu_algbase` 分别来自 `eepsa.hwbase0~3` 和 `eepsa.addr_alg`。
2. 对照 [main.cc:195-220](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L195-L220)，把每个寄存器宏替换成 config.h 里的偏移，列出「偏移 ← 变量 ← hwbaseN」三列对照表。
3. 在 [eeptpu_sa.cpp:255-283](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L255-L283) 中找到 `0x11` 这个魔数，确认它与 `main.cc` 写的是同一个值。

**需要观察的现象 / 预期结果**：你会得到一张 6 行的表，前 4 行是基地址、第 5 行是 algaddr、第 6 行是 `0x11` 启动触发。如果开启 `EEP_DEBUG_INFO`，[main.cc:196-219](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L196-L219) 的回读打印会逐个回显这些值（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`STARTUP` 写 `0x11` 之前，那 6 个基地址/算法地址寄存器已经写好了；能不能调换顺序，先写 `STARTUP=0x11` 再写基地址？为什么？

> **答案**：不能。TPU 在看到 `STARTUP` 触发时会锁存（latch）当前基地址寄存器与 algaddr 的值并开始执行。若先触发再写地址，TPU 拿到的是旧/未定义的地址，会读到错误数据甚至越界访问 DDR。所以「先配地址、最后写触发」是必须的时序。

**练习 2**：`main.cc::tpu_forward()` 写了 4 个基地址，而 `EEPTPU_SA::eep_tpu_start_work()` 在 `bin_type==1` 时只写 2 个。这说明什么？

> **答案**：enc（`bin_type=1`）与 pub（`bin_type=2`）两种 bin 的内存布局不同——enc 把多段内存合并成 2 个基址管理，pub 拆成 par/in/tmp/out 共 4 段。`main.cc` 写死 4 段是因为它只服务于当前 pub 型的 yolov4-tiny；类方法则更通用，能同时兼容两种 bin。

---

### 4.2 完成状态轮询

#### 4.2.1 概念说明

写完 `STARTUP=0x11` 后，TPU 开始在硬件上并行执行网络，CPU 这边没有任何「完成中断」可用（本工程唯一的中断被 DP 显示占用了，见 u4-l1），所以软件用**忙等轮询**：反复读状态寄存器 `STATUS`，直到它的最高位（bit31）被 TPU 置 1，表示「这次推理完成」。

bit31（掩码 `0x80000000`）就是完成标志位。

#### 4.2.2 核心流程

```
do:
    rd_val = read_reg(STATUS)        // 0x0C
while (rd_val & 0x80000000) != 0x80000000
```

判据是：

\[
(rval\ \&\ 0x80000000)\ ==\ 0x80000000
\]

即只看最高位，其余位忽略。一旦为真就跳出循环，认为 TPU 完成。注意这里**没有超时、没有 sleep**——CPU 全速自旋。这与 u4-l3 讲过的 `register_wait(addr, mask, want_val)`（判据 `(rval & mask) == want_val`）完全一致：这里 `mask = want_val = 0x80000000`。

#### 4.2.3 源码精读

`tpu_forward()` 的轮询段：

[main.cc:222-227](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L222-L227) —— 先读一次 `STATUS`，再 `while` 循环反复读，直到 bit31 为 1。循环体内的 `ret = 0;` 只是个赋值，不影响判定。

类方法版本通过 `eepreg_wait` 复用通用的位字段等待原语：

[eeptpu_sa.cpp:285-292](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L285-L292) —— `eep_tpu_wait_done()` 调 `eepreg_wait(0x0C, 0x80000000, 0x80000000)`，即「轮询 STATUS(0x0C)，直到 `(值 & 0x80000000) == 0x80000000`」。

`STATUS` 寄存器本身的偏移定义：

[config.h:35](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L35) —— `EEPTPU_STATUS_REG` 在偏移 `0xC`，`volatile unsigned int`。

> 关于 bit31 在启动前是否需要软件清零：源码里 `tpu_forward()` 在写 `STARTUP` 后直接轮询，并未显式清 STATUS。推测 TPU 硬件在收到 `STARTUP` 时会自动清掉上一次的完成位、完成后再置位；但该清零机制属硬件行为，**待确认**。

#### 4.2.4 代码实践

**实践目标**：理解忙等轮询为什么必须用 `volatile`，并体会「无超时」带来的风险。

**操作步骤（源码阅读型）**：

1. 看 [main.cc:222-227](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L222-L227) 的循环条件 `(rd_val & 0x80000000) != 0x80000000`，写出它等价的 `eepreg_wait` 调用参数。
2. 假设把 `EEPTPU_STATUS_REG` 宏里的 `volatile` 去掉（**只在脑中模拟，不要真改源码**），想一想编译器可能怎么优化这个 `while` 循环。
3. 在 [eeptpu_sa.cpp:285-292](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L285-L292) 确认 `eep_tpu_wait_done` 没有任何 timeout 退出条件。

**需要观察的现象 / 预期结果**：

- 去掉 `volatile` 后，编译器可能认定「循环体内 `rd_val` 的赋值来源（内存映射寄存器）不会被外部改变」，从而把整段轮询优化成只读一次的死循环或直接跳过——这正是内存映射 I/O 必须加 `volatile` 的根本原因。
- 由于无超时，若 TPU 因配置错误（如基地址写错）永不置位，CPU 会永远卡在这个循环里，串口再无输出——这是裸机调试时「跑着跑着就死了」的常见根因之一（待本地验证：可故意写错一个基地址观察现象）。

#### 4.2.5 小练习与答案

**练习 1**：轮询判据是 `(rd_val & 0x80000000) == 0x80000000`。如果改成 `rd_val == 0x80000000` 会有什么问题？

> **答案**：`==` 要求 STATUS 整个 32 位都等于 `0x80000000`。但 STATUS 的低位可能还承载其它状态信息（如运行/错误标志位），只要这些位非 0，`==` 就永不成立，导致死循环。用「`& mask == mask`」只关心完成位、忽略其它位，才是正确写法。

**练习 2**：`tpu_forward()` 的 `while` 循环里有一句 `ret = 0;`，它起什么作用？去掉会怎样？

> **答案**：它对完成判定**没有任何作用**，只是一个多余的赋值（很可能是历史遗留或为了不被警告「ret 未使用」）。去掉它不影响功能；但它也从侧面说明这个轮询循环是纯忙等，循环体本身不做任何有意义的工作。

---

### 4.3 硬件运行计时

#### 4.3.1 概念说明

TPU 内部有一个**运行计时寄存器 `EEPTPU_RUNTIMER_REG`**（偏移 `0x24`），它在 TPU 执行推理期间计数，推理结束（STATUS 置位）后保持其值。软件在确认完成后读它，就得到「TPU 自己眼里这次计算花了多少」。

这与软件 `XTime` 形成关键对比：`RUNTIMER` 由硬件在 TPU 时钟域里维护，**不包含**软件写寄存器、读寄存器、轮询的 CPU/AXI 总线开销，因此更贴近「纯 TPU 计算耗时」。这是评估 TPU 算力、对比不同网络/精度时的首选指标。

#### 4.3.2 核心流程

```
tpu_forward()                    // 启动 + 忙等完成
runtimer = EEPTPU_RUNTIMER_REG   // 完成后读硬件计时器
```

`RUNTIMER` 的精确单位（时钟周期数还是固定时基折算值）由加密 TPU 硬件定义，**待确认**；但其语义是「硬件执行期间的计数」，与软件墙钟时间量纲不同，比较时以「相对大小」为准。

#### 4.3.3 源码精读

寄存器定义：

[config.h:28](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L28) —— `EEPTPU_RUNTIMER_REG` 在偏移 `0x24`，`volatile unsigned int`。

它的读取被包在 `EEP_DEBUG_INFO` 宏里：

[main.cc:391-394](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L391-L394) —— 在 case `'2'` 的 `tpu_forward()` 之后，**仅当编译时定义了 `EEP_DEBUG_INFO`** 才读取 `RUNTIMER` 并以十六进制打印。case `'5'` 的实时循环里也有对称的一段：[main.cc:574-577](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L574-L577)。

存放它的全局变量：

[main.cc:71](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L71) —— `u32 runtimer;`。注意它只有 `#ifdef EEP_DEBUG_INFO` 下才会被赋值/打印；在默认（非 debug）编译下这个读取根本不存在。

#### 4.3.4 代码实践

**实践目标**：让 `RUNTIMER` 真正打印出来，并理解它只在 debug 编译下可见。

**操作步骤**：

1. 打开 [config.h:55](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L55)，把 `//#define EEP_DEBUG_INFO` 取消注释（即启用 `#define EEP_DEBUG_INFO`）。
2. 重新编译裸机工程并上板运行（待本地验证：此步需 Vitis 工具链与硬件）。
3. 串口菜单选 `2`（Forward Result），观察输出中是否出现 `EEPTPU Run timer value: 0x........`。

**需要观察的现象 / 预期结果**：启用 `EEP_DEBUG_INFO` 后，`tpu_forward()` 内部的回读打印（[main.cc:196-219](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L196-L219)）和完成后的 `RUNTIMER` 值都会打印；不启用时则什么都不打印。**结论**：要对比软件/硬件两套计时，必须先开 debug。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `RUNTIMER` 的读取要放在 `EEP_DEBUG_INFO` 里、而不是和 `tused_forward` 一样无条件执行？

> **答案**：`RUNTIMER` 主要用于调试与性能剖析，正式发布时多一次寄存器读（一次 AXI 控制通路往返）是额外开销，且其值对最终推理结果无影响。`tused_forward` 虽然也偏调试性质，但它后续会被 `xil_printf("forward time is %d us", tused_forward)` 打印（见 4.4），属于 demo 对用户的常规输出，故无条件保留。

**练习 2**：`RUNTIMER` 是 32 位无符号（`u32`）。如果某次推理硬件计数溢出（超过 \(2^{32}-1\)），会发生什么？

> **答案**：32 位会回绕（wrap-around）到 0 继续计，软件直接读到的就是一个被截断的小值。对于本工程毫秒级推理、TPU 时钟域计数周期不长，一般不会溢出；但若用它评估超长任务，需注意回绕风险（待确认 RUNTIMER 的实际计数频率以估算上限）。

---

### 4.4 软件耗时计量

#### 4.4.1 概念说明

软件侧用 Xilinx BSP 的 `XTime`（ARM 全局定时器，64 位）做墙钟计时：在 `tpu_forward()` 调用前后各取一次时间戳，差值换算成微秒。这个值 `tused_forward` 量的是「**从 CPU 视角看，这次 forward 调用占用了 CPU 多长时间**」，包含三部分：

1. 写 6 个寄存器的 AXI 控制通路往返（启动开销）；
2. TPU 真正计算的时间；
3. CPU 忙等轮询 STATUS 的时间（含轮询颗粒度带来的「完成到 CPU 察觉」之间的延迟）。

因此 `tused_forward` 通常**大于** `RUNTIMER`，多出来的就是第 1、3 项的软件/总线开销。

#### 4.4.2 核心流程

```
XTime_GetTime(&tBegin)
tpu_forward()
XTime_GetTime(&tEnd)
tused_forward = ((tEnd - tBegin) * 1000000) / COUNTS_PER_SECOND
```

微秒换算公式：

\[
t_{us} \;=\; \frac{(t_{end}-t_{begin})\times 10^{6}}{\text{COUNTS\_PER\_SECOND}}
\]

其中 `COUNTS_PER_SECOND` 是 BSP 头文件 `xtime_l.h` 给出的「全局定时器每秒滴答数」（由 ZynqMP 的定时器时钟决定，具体数值待本地确认）。无论该常数是多少，公式都把滴答差正确折算成微秒。

#### 4.4.3 源码精读

计时变量声明：

[main.cc:64-67](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L64-L67) —— `XTime tBegin,tEnd;` 与 `u32 tused_forward;`（还有 `tused`、`tused_det_out`）。`XTime` 类型来自 [main.cc:41](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L41) 的 `#include "xtime_l.h"`（Xilinx BSP）。

case `'2'` 里的计时块：

[main.cc:382-385](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L382-L385) —— `tBegin` 取样 → `tpu_forward()` → `tEnd` 取样 → 换算成微秒存 `tused_forward`。注意 `RUNTIMER` 的读取（[main.cc:391-394](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L391-L394)）在 `tEnd` **之后**，所以 `tused_forward` 不包含读 `RUNTIMER` 本身的耗时。

这套计时是 BSP 通用写法，摄像头采集也用同一套：

- 采集耗时 `tused`：[camera.c:458-464](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/camera.c#L458-L464) —— 与 forward 完全相同的 `XTime` 三段式。
- yolo3 后处理耗时 `tused_det_out`：[main.cc:417-420](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L417-L420) —— 同一套写法。

最终这些值会打印给用户，例如 [main.cc:433-434](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L433-L434) 的 `forward time is %d us` 与 `detection output time is %d us`。

> 一个影响计时可比性的细节：进入 forward 之前工程关闭了 D-cache（[main.cc:279](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L279) `Xil_DCacheDisable()`），所以轮询 STATUS 时每次读都是未缓存的 AXI 往返，这会让 `tused_forward` 里的「轮询开销」相对偏大；而 `RUNTIMER` 在硬件侧计数，不受 D-cache 影响。这也是两者会有差异的原因之一。

#### 4.4.4 代码实践

**实践目标**：搞清 `tused_forward` 量了什么、没量什么，并定位它在代码里的完整生命周期。

**操作步骤（源码阅读型）**：

1. 在 [main.cc:382-385](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L382-L385) 圈出 `tBegin`/`tEnd` 的取样位置，确认 `tused_forward` 覆盖的是**整个** `tpu_forward()`（含 6 次寄存器写 + 忙等轮询）。
2. 顺 [main.cc:433](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L433) 找到它的打印处，确认 `tused_forward` 不包含 `read_forward_result()`（输出读取）和后处理时间。
3. 对比 [camera.c:458-464](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/camera.c#L458-L464)，体会这是全工程统一的 BSP 计时范式。

**需要观察的现象 / 预期结果**：`tused_forward` 是「CPU 视角的 forward 墙钟时间」，边界是 `tpu_forward()` 的入口与出口；它**不含**输出读取与后处理。因此串口看到的 `forward time` 只是「启动 TPU + 等 TPU 算完」这一段，不等于整条推理管线的端到端延迟。

#### 4.4.5 小练习与答案

**练习 1**：`tused_forward` 是 `u32`（32 位无符号），单位微秒。它能表示的最大时长是多少？对毫秒级推理够用吗？

> **答案**：\(2^{32}-1 \approx 4.29\times10^{9}\) 微秒 ≈ 4290 秒 ≈ 71 分钟。对毫秒级推理绰绰有余。但要留意「一小时停机限制」（u1-l1）：若 demo 长时间连续运行，靠累计 `tused_forward` 估算总时长会受限于该限制，而非 `u32` 溢出。

**练习 2**：为什么说 `tused_forward` 不等于「纯 TPU 计算时间」？请列出它额外包含的两类开销。

> **答案**：(1) 启动阶段写 6 个寄存器的 AXI 控制通路往返；(2) 完成阶段的忙等轮询开销（含「TPU 实际完成」与「CPU 下一次读 STATUS 察觉到完成」之间的颗粒度延迟，尤其在 D-cache 关闭、每次读都走 AXI 时偏大）。这两类都是 CPU/总线开销，TPU 内部的 `RUNTIMER` 不含它们。

---

## 5. 综合实践

**任务**：对比软件 `tused_forward` 与硬件 `EEPTPU_RUNTIMER_REG`，解释二者差异，并判断哪个更反映「纯 TPU 计算耗时」。

**操作步骤**：

1. **开启 debug**：在 [config.h:55](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L55) 启用 `#define EEP_DEBUG_INFO`（否则 `RUNTIMER` 不会被读取，见 [main.cc:391-394](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L391-L394)）。
2. **重建并运行**：用 Vitis 重新编译、上板运行，串口菜单选 `2`（待本地验证，需硬件）。预期会同时看到两类输出：
   - `EEPTPU Run timer value: 0x........`（硬件 `RUNTIMER`，十六进制）；
   - `forward time is N us`（软件 `tused_forward`，十进制微秒）。
3. **读源码定位两者来源**：
   - `tused_forward`：[main.cc:382-385](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L382-L385)，ARM `XTime` 取样 `tpu_forward()` 前后。
   - `runtimer`：[main.cc:391-394](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L391-L394)，完成后再读硬件寄存器 `0x24`。
4. **解释差异**：填下表（数值列标「待本地验证」即可）：

| 指标 | 来源 | 量程范围 | 是否含启动寄存器写开销 | 是否含轮询开销 | 反映纯 TPU 计算？ |
| --- | --- | --- | --- | --- | --- |
| `tused_forward`（软件 XTime） | ARM 全局定时器墙钟 | `tpu_forward()` 入口→出口 | 是 | 是 | 否（偏大） |
| `EEPTPU_RUNTIMER_REG`（硬件） | TPU 内部计时器 `0x24` | TPU 执行期间 | 否 | 否 | **是** |

**结论要点**：

- `tused_forward` > `RUNTIMER`（折算到同一时间单位后），差额来自 6 次启动寄存器写的 AXI 往返 + 完成轮询的 CPU 自旋（D-cache 关闭时尤其明显）。
- **`EEPTPU_RUNTIMER_REG` 更反映纯 TPU 计算耗时**，因为它在 TPU 时钟域内计数、不含 CPU/总线开销。评估 TPU 算力、对比 FP16/INT8 或不同网络时，应以它为准；评估端到端管线延迟（含启动与读取）时，才看 `tused_forward` 这类墙钟指标。

> 注意 `RUNTIMER` 的精确时间单位由加密 TPU 硬件定义（待确认），所以严格比较时应先在同一基准上标定其计数频率；本实践以「相对大小关系」与「哪个更贴近纯计算」为主要结论。

## 6. 本讲小结

- 一次推理的寄存器时序是：写 `BASEADDR0~3`(0x50/54/58/5C) → 写 `ALGOADDR`(0x30) → 写 `STARTUP`(0x34)=`0x11` 触发 → 轮询 `STATUS`(0x0C) 的 bit31。
- 完成判定靠忙等轮询 `STATUS & 0x80000000`，无中断、无超时、无 sleep；`volatile` 是轮询不被编译器优化的关键。
- 工程里有两套等价实现：菜单实际调用 `main.cc::tpu_forward()`（固定 pub 的 4 基址），类方法 `EEPTPU_SA::forward()`（经 `eep_tpu_start_work`/`eep_tpu_wait_done`，按 `bin_type` 分支）当前未被 `main.cc` 调用。
- 软件计时 `tused_forward` 用 ARM `XTime` 量墙钟，含启动写 + 轮询的 CPU/总线开销；硬件计时 `EEPTPU_RUNTIMER_REG`(0x24) 在 TPU 内部计数，更贴近纯计算。
- `RUNTIMER` 只在 `EEP_DEBUG_INFO` 下读取；要对比两套计时，必须先开 debug。
- 评估纯 TPU 算力看 `RUNTIMER`，评估端到端延迟看 `tused_forward`。

## 7. 下一步学习建议

本讲把「forward 的寄存器时序与计时」讲透了，但还遗留两个问题留给后续讲义：

- **输出怎么读出来**：`tpu_forward()` 完成后，结果张量以 TPU 特有的「16 通道分组、32 字节步长、定点(exp)」格式躺在 DDR 的 `out` 段。下一讲 **u5-l2「输出读取与 epmat→ncnn::Mat 转换」** 将讲解 `read_forward_result()` 与 `epmat2nmat()` 如何反量化还原成浮点 `ncnn::Mat`。
- **端到端延迟的其余组成**：本讲的 `tused_forward` 只覆盖 forward 本身；采集（`tused`）、后处理（`tused_det_out`）的计时已在代码里就位，可在 u6（后处理算法）单元串成完整管线。

建议继续阅读 [eeptpu_sa.cpp:364-387](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L364-L387)（`read_forward_result`）与 [eeptpu_sa.cpp:309-361](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L309-L361)（`epmat_get_size` / `epmat2nmat`），为 u5-l2 做准备。
