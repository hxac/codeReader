# 性能计数器与 profiling

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 Nyuzi **硬件性能事件**从哪里产生、如何在 `core.sv` 里聚合成一条事件总线、又如何被计数模块累加。
- 用**控制寄存器**（`getcr`/`setcr`）选择某个事件并读出 64 位计数，从而统计 I-Cache/D-Cache 缺失等指标。
- 理解 **`+profile` 采样剖析**的工作原理，并用 `profile.py` 把采样到的 PC 流转成「每个函数占多少时间」的热点报告。
- 区分 Nyuzi 里的**两套性能接口**：真正接线的「控制寄存器路径」与遗留未接线的「libos MMIO 路径」，避免踩坑。

本讲是「调试与性能」单元的一环，承接 u7-l2（控制寄存器与中断）与 u9-l2（libos 并行执行）。

## 2. 前置知识

- **控制寄存器 `getcr`/`setcr`**（见 u2-l4、u7-l2）：读写处理器内部状态，需 supervisor 权限，不走缓存。本讲要频繁写「事件选择寄存器」、读「计数寄存器」。
- **内存映射 I/O（MMIO）**（见 u1-l4、u8-2）：访问 `0xffff0000` 以上的外设寄存器。本讲会对比它与控制寄存器两条路径的差异。
- **流水线与多核信号命名**（见 u3-l2）：Nyuzi 用信号前缀（`ix_`/`dd_`/`ifd_`/`ts_`/`wb_`/`l2i_`）标识来源级，本讲的性能脉冲就来自这些级。
- **周期精确仿真 vs 指令集模拟器**（见 u1-l2）：`+profile` 只在 Verilator 周期精确模型里可用，C 模拟器里没有。
- **两类性能测量思路**：
  - **事件计数（counting）**：用一个不断累加的计数器统计某类事件发生了多少次（如「D-Cache 缺失次数」）。
  - **采样剖析（sampling）**：周期性地「拍快照」记录当前正在执行的 PC，用样本分布近似时间分布。Nyuzi 两种都支持。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [hardware/core/performance_counters.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/performance_counters.sv) | 通用的「按索引累加」计数模块，被 core 实例化。 |
| [hardware/core/core.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv) | 把各流水级的性能脉冲聚合成 14 位 `perf_events` 总线，并实例化计数模块。 |
| [hardware/core/control_registers.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv) | 提供「事件选择」与「计数读出」两组控制寄存器，是软件与计数器之间的桥梁。 |
| [hardware/core/defines.svh](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh) | 定义 `CORE_PERF_EVENTS=14`、`CR_PERF_*` 控制寄存器编号。 |
| [hardware/testbench/soc_tb.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/testbench/soc_tb.sv) | 实现 `+profile=<文件名>`：周期性地把采样到的 PC 写入文件。 |
| [tools/misc/profile.py](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/misc/profile.py) | 读 PC 采样文件 + 符号表，输出每个函数的采样计数与占比。 |
| [software/libs/libos/performance_counters.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/performance_counters.h) 与 [software/libs/libos/bare-metal/performance_counters.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/performance_counters.c) | libos 提供的 C 语言性能计数 API（**遗留 MMIO 接口，见 4.2 的提醒**）。 |
| [tests/unit/test_control_registers.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/unit/test_control_registers.sv) | 对控制寄存器（含性能计数）的单元测试，可作为行为参考。 |

## 4. 核心概念与源码讲解

### 4.1 性能事件聚合

#### 4.1.1 概念说明

「性能事件」是处理器内部某个**有趣瞬间**拉起的一个 1 比特脉冲，例如「这一拍发生了一次 D-Cache 缺失」「这一拍退休了一条指令」。单个脉冲没有意义，但如果我们把某一类脉冲**逐拍累加**，就得到了「这类事件发生了多少次」——这就是事件计数的本质。

Nyuzi 的设计很朴素：

- 各流水级在自己感兴趣的时机拉高一根 `xxx_perf_yyy` 信号（「事件源」）。
- `core.sv` 把这些信号**拼成一条位宽为 14 的事件总线** `perf_events`，每一位代表一类事件。
- 一个**通用计数模块** `performance_counters` 按软件选定的位索引，对总线上该位为 1 的拍计数。

这样，「在哪里产生事件」与「怎么计数」被解耦：流水级只管拉脉冲，计数逻辑完全通用。

#### 4.1.2 核心流程

```
ifetch_data   ifd_perf_icache_miss ─┐
dcache_data   dd_perf_dcache_miss  ─┤
int_execute   ix_perf_cond_branch  ─┤  core.sv 把 14 根脉冲
thread_select ts_perf_instruction_ ─┤  按 MSB-first 拼接
writeback     wb_perf_instruction_ ─┤  ─►  perf_events[13:0]
l1_l2_iface   l2i_perf_store       ─┘
                                          │
                          performance_counters 模块
                          （按 perf_event_select[i] 选一位，
                            每拍若该位=1 则 count[i]++）
                                          │
                                    perf_event_count[0..1] (64 位)
```

关键点：事件总线是**逐拍（per-cycle）**的组合逻辑聚合；计数模块在每个时钟上升沿检查选定那一位是否为 1，是则加 1。注意 `perf_events` 是各源信号的**或汇总**，同一拍里多位可能同时为 1（不同事件互不影响）。

#### 4.1.3 源码精读

**事件总线的拼接**在 `core.sv`，注释明确要求位宽必须与 `CORE_PERF_EVENTS` 对齐：

[hardware/core/core.sv:403-418](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L403-L418) ——把 14 个性能脉冲按 **MSB-first** 拼成 `perf_events`（拼接在最前面的信号是最高位 bit 13，最后面的 `wb_perf_interrupt` 是 bit 0）。

位宽常量定义在 `defines.svh`：

[hardware/core/defines.svh:67-71](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L67-L71) ——`CORE_PERF_EVENTS = 14`，并注明该值必须与 `core.sv` 里拼接的信号数一致（`L2_PERF_EVENTS = 3` 是 L2 自己的、独立的一组，见下文提醒）。

**通用计数模块** `performance_counters.sv` 全文很短，核心就是一个参数化的累加器：

[hardware/core/performance_counters.sv:34-49](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/performance_counters.sv#L34-L49) ——`NUM_COUNTERS` 个独立计数器，第 `i` 个每拍检查 `perf_events[perf_event_select[i]]`，为 1 则 `perf_event_count[i] <= ... + 1`。复位清零。

`core.sv` 的实例化把 `NUM_EVENTS` 设为 14、`NUM_COUNTERS` 设为 2：

[hardware/core/core.sv:420-424](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L420-L424) ——`performance_counters #(.NUM_EVENTS(CORE_PERF_EVENTS), .NUM_COUNTERS(2))`。

> **关于 L2 的事件**：`l2_cache.sv` 也输出一组 3 位的 `l2_perf_events`（[hardware/core/l2_cache.sv:55-56](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache.sv#L55-L56) 与 [:116](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache.sv#L116)），描述 L2 写回/缺失/命中。但这组信号**没有**进入 `core.sv` 的 14 位 `perf_events`，核内的 `performance_counters` 实例计数不到它们。请记住这一点，它解释了 4.2 里软件枚举与硬件事件表的错位。

#### 4.1.4 代码实践

**实践目标**：确认每个性能事件「在什么条件下」拉高，建立对事件语义的直观认识。

**操作步骤**（源码阅读型）：

1. 对照下表，挑两个事件，例如「指令发射」(bit 4, `ts_perf_instruction_issue`) 与「D-Cache 缺失」(bit 8, `dd_perf_dcache_miss`)。
2. 用 `Grep` 在 `hardware/core/` 下分别搜索 `ts_perf_instruction_issue` 与 `dd_perf_dcache_miss` 的赋值点，看它们各自在什么逻辑里被置 1。
3. 思考：为什么「指令发射」与「指令退休」是两个不同事件？（提示：发射 ≠ 退休，分支回滚会让已发射的指令作废。）

**需要观察的现象**：`ts_perf_instruction_issue` 一般在线程选择级真的发射了一条指令的拍拉高；`dd_perf_dcache_miss` 在数据缓存判定缺失的拍拉高。

**预期结果**：你会看到这些脉冲都是「事件发生那一拍为 1、其余拍为 0」的单比特信号，正是计数模块累加的对象。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `perf_events` 用一条位总线，而不是给每个事件单独接一个计数器？
**答案**：单独计数器会占用大量寄存器资源（14 事件 × 64 位）。Nyuzi 只设 2 个物理计数器，用「选择位」让软件按需把这 2 个计数器**复用**到任意事件上，资源省而灵活。

**练习 2**：同一拍内，`icache_miss` 与 `dcache_miss` 能否同时为 1？计数会互相干扰吗？
**答案**：能。二者是不同位，互不影响；只要它们各自被某个计数器选中，两个计数器会各加各的。

---

### 4.2 事件选择

#### 4.2.1 概念说明

上一节看到有 14 类事件、却只有 2 个物理计数器。软件需要一种机制来告诉硬件：「计数器 0 去数第 5 号事件，计数器 1 去数第 8 号事件」。这就是**事件选择**，在 Nyuzi 里通过两个控制寄存器实现：

- `CR_PERF_EVENT_SELECT0`（编号 22）选计数器 0 的事件索引；
- `CR_PERF_EVENT_SELECT1`（编号 23）选计数器 1 的事件索引。

索引位宽为 `EVENT_IDX_WIDTH = $clog2(14) = 4` 位。每个计数器是 64 位（避免短时间溢出），但 Nyuzi 的控制寄存器读返回值是 32 位标量，所以 64 位计数被拆成**低/高两个半字**，由 4 个只读寄存器读出（24–27 号）。

> **重要提醒（两条软件接口，只有一条真正可用）**
>
> Nyuzi 源码里存在**两套**「性能计数软件接口」，初学者很容易混淆：
>
> 1. **控制寄存器路径（真正接线、可用）**：用 `getcr`/`setcr`（C 里即 `__builtin_nyuzi_read_control_reg` / `__builtin_nyuzi_write_control_reg`）访问 22–27 号控制寄存器。这是 `core.sv` 里 `performance_counters` 模块的真实出口，本讲的动手实践应走这一条。
> 2. **libos MMIO 路径（遗留、未接线）**：[software/libs/libos/bare-metal/performance_counters.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/performance_counters.c) 与 [performance_counters.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/performance_counters.h) 暴露了 `set_perf_counter_event` / `read_perf_counter`，它们写/读的是 MMIO 寄存器 `REG_PERF0_SEL`/`REG_PERF0_VAL`（地址 `0xffff0200`，见 [registers.h:45-52](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/registers.h#L45-L52)）。但模拟器的 `device.c`（[tools/emulator/device.c:42-71](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/device.c#L42-L71)）**并不处理**这个地址段，GPGPU 核心的硬件也没有把它们映射到真实的计数器——它们更像是为某类 SoC 外设预留、却未在当前核上落地的遗留接口。内核的 `SYS_set_perf_counter`/`SYS_read_perf_counter` 系统调用同样转发到这些 MMIO 寄存器（[software/kernel/syscall.c:111-123](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/syscall.c#L111-L123)），因此也存在同样的问题。
>
> 此外，`performance_counters.h` 里的事件枚举顺序（`PERF_L2_WRITEBACK=0, …, PERF_COND_BRANCH_NOT_TAKEN=16`，共 17 项）与下表「硬件真实事件位索引」**完全不一致**。这进一步说明该 MMIO 接口并非当前核上 `performance_counters` 模块的对应物。本讲后面会教你**用控制寄存器路径**完成等价工作。

#### 4.2.2 核心流程

读写一次性能计数的完整流程：

```
1. （可选）复位后计数器为 0；如需重置可重新写选择寄存器后再读基线
2. 软件写 CR_PERF_EVENT_SELECT0(22) = 事件索引  →  选定计数器0 的事件
   软件写 CR_PERF_EVENT_SELECT1(23) = 事件索引  →  选定计数器1 的事件
3. ……运行被测程序（计数器每拍自动累加）……
4. 读 CR_PERF_EVENT_COUNT0_L(24) 与 CR_PERF_EVENT_COUNT0_H(25)
      → 拼成 64 位 = counter0 的计数值
   读 CR_PERF_EVENT_COUNT1_L(26) 与 CR_PERF_EVENT_COUNT1_H(27)
      → 拼成 64 位 = counter1 的计数值
```

辅助指标：`CR_CYCLE_COUNT`（编号 6）是一个每拍自增的自由运行周期计数器，可用来算「事件数 / 周期数」等比率（注意它是 32 位标量，到 \(2^{32}\) 周期会回绕）。

#### 4.2.3 源码精读

**硬件事件表**（由 [core.sv:403-418](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L403-L418) 的 MSB-first 拼接派生，选择值即位索引）：

| 选择值 | 事件 | 来源信号 | 来源流水级 |
|---|---|---|---|
| 0 | 中断 | `wb_perf_interrupt` | writeback |
| 1 | store 回滚 | `wb_perf_store_rollback` | writeback |
| 2 | store 发送 | `l2i_perf_store` | l1_l2_interface |
| 3 | 指令退休 | `wb_perf_instruction_retire` | writeback |
| 4 | 指令发射 | `ts_perf_instruction_issue` | thread_select |
| 5 | I-Cache 缺失 | `ifd_perf_icache_miss` | ifetch_data |
| 6 | I-Cache 命中 | `ifd_perf_icache_hit` | ifetch_data |
| 7 | ITLB 缺失 | `ifd_perf_itlb_miss` | ifetch_data |
| 8 | D-Cache 缺失 | `dd_perf_dcache_miss` | dcache_data |
| 9 | D-Cache 命中 | `dd_perf_dcache_hit` | dcache_data |
| 10 | DTLB 缺失 | `dd_perf_dtlb_miss` | dcache_data |
| 11 | 无条件分支 | `ix_perf_uncond_branch` | int_execute |
| 12 | 条件分支 taken | `ix_perf_cond_branch_taken` | int_execute |
| 13 | 条件分支 not taken | `ix_perf_cond_branch_not_taken` | int_execute |

例如想数 D-Cache 缺失，就把选择值 `8` 写进 `CR_PERF_EVENT_SELECT0`。

**事件选择寄存器的写入**在 `control_registers.sv` 的写逻辑里：

[hardware/core/control_registers.sv:210-211](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L210-L211) ——`setcr` 写 22/23 号时，把值的低 `EVENT_IDX_WIDTH` 位锁存到 `cr_perf_event_select0/1`，送给计数模块。

**计数值的读出**在同一模块的读逻辑里，64 位拆成 4 个半字：

[hardware/core/control_registers.sv:304-307](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L304-L307) ——`getcr` 读 24/25 号返回 `perf_event_count0` 的低/高 32 位，26/27 号对应 `perf_event_count1`。

控制寄存器编号定义在 `defines.svh`：

[hardware/core/defines.svh:188-193](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L188-L193) ——`CR_PERF_EVENT_SELECT0=22`、`SELECT1=23`、`COUNT0_L=24`、`COUNT0_H=25`、`COUNT1_L=26`、`COUNT1_H=27`。

**周期计数器**每拍自增：

[hardware/core/control_registers.sv:159](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L159) ——`cycle_count <= cycle_count + 1`，经 `CR_CYCLE_COUNT`(6) 读出（见 [:292](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L292)）。

**行为参考**：单元测试 [tests/unit/test_control_registers.sv:48-49](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/unit/test_control_registers.sv#L48-L49) 把 `control_registers` 的 `NUM_PERF_EVENTS` 设为 `CORE_PERF_EVENTS`、计数器数设为 2，可作为「这些寄存器确实如此工作」的佐证。

#### 4.2.4 代码实践

**实践目标**：用控制寄存器路径（真实可用）编程统计 I-Cache 缺失与 D-Cache 缺失，并算出「每条退休指令的缺失数」。

下面是**示例代码**（不是仓库原有文件），展示正确的用法：用 `__builtin_nyuzi_write_control_reg` 选事件、`__builtin_nyuzi_read_control_reg` 读计数。

```c
// 示例代码：统计 I-Cache miss / D-Cache miss（走控制寄存器路径）
#include <stdint.h>

#define CR_PERF_EVENT_SELECT0   22
#define CR_PERF_EVENT_SELECT1   23
#define CR_PERF_EVENT_COUNT0_L  24
#define CR_PERF_EVENT_COUNT0_H  25
#define CR_PERF_EVENT_COUNT1_L  26
#define CR_PERF_EVENT_COUNT1_H  27
#define CR_CYCLE_COUNT          6

// 硬件事件索引（见 4.2.3 表格）
#define EV_ICACHE_MISS   5
#define EV_DCACHE_MISS   8
#define EV_INST_RETIRE   3

static inline uint64_t read_counter(int low_cr, int high_cr) {
    uint32_t lo = __builtin_nyuzi_read_control_reg(low_cr);
    uint32_t hi = __builtin_nyuzi_read_control_reg(high_cr);
    return ((uint64_t)hi << 32) | lo;
}

void measure(void) {
    // 计数器0 数 I-Cache miss；计数器1 数 D-Cache miss
    __builtin_nyuzi_write_control_reg(CR_PERF_EVENT_SELECT0, EV_ICACHE_MISS);
    __builtin_nyuzi_write_control_reg(CR_PERF_EVENT_SELECT1, EV_DCACHE_MISS);

    uint64_t ic_miss = read_counter(CR_PERF_EVENT_COUNT0_L, CR_PERF_EVENT_COUNT0_H);
    uint64_t dc_miss = read_counter(CR_PERF_EVENT_COUNT1_L, CR_PERF_EVENT_COUNT1_H);
    uint32_t cycles0 = __builtin_nyuzi_read_control_reg(CR_CYCLE_COUNT);

    run_workload();   // 你要测量的计算，例如一段循环或渲染

    ic_miss = read_counter(CR_PERF_EVENT_COUNT0_L, CR_PERF_EVENT_COUNT0_H) - ic_miss;
    dc_miss = read_counter(CR_PERF_EVENT_COUNT1_L, CR_PERF_EVENT_COUNT1_H) - dc_miss;
    uint32_t cycles = __builtin_nyuzi_read_control_reg(CR_CYCLE_COUNT) - cycles0;

    printf("icache_miss=%llu dcache_miss=%llu cycles=%u\n",
           (unsigned long long)ic_miss, (unsigned long long)dc_miss, cycles);
}
```

**操作步骤**：

1. 把 `measure()` 的 `run_workload()` 换成一段有意义的计算（例如拷贝一块内存、或调用 librender 渲染一帧）。
2. 用 Nyuzi 工具链编译、`elf2hex` 转镜像，放进 `run_verilator` 跑（计数器只在周期精确模型里有意义）。
3. 记录测量前后两次读数之差。

**需要观察的现象 / 预期结果**：D-Cache 缺失数会随「访问不同缓存行」的次数增加；增大工作集超过 L1D 容量（默认 16 KiB）时缺失率应明显上升。由于只有 2 个计数器，要同时得到 I-Cache、D-Cache、退休指令、周期四项，需要分多次运行（每次换两个事件）。

> **待本地验证**：上述计数绝对值取决于工作负载与缓存配置，请在你的环境实跑后记录真实数字。注意：若误用 libos 的 `set_perf_counter_event()`/`read_perf_counter()`（MMIO 路径），在当前核/模拟器上**读不到真实计数**，这就是本实践改用控制寄存器路径的原因。

#### 4.2.5 小练习与答案

**练习 1**：为什么 64 位计数要拆成 `_L`/`_H` 两个寄存器读？
**答案**：控制寄存器的读返回值是 32 位标量 `scalar_t`，一次只能传 32 位；64 位计数必须分两次读再由软件拼合。

**练习 2**：两次读 `_L` 与 `_H` 之间，计数器可能又涨了，会出错吗？如何减小误差？
**答案**：理论上有「撕裂」风险（低半字进位而高半字尚未更新）。减小误差的办法是尽量在读前后包裹一段静止区，或用周期计数对齐；对长跑的大计数，1 拍误差通常可忽略。

**练习 3**：想同时统计 4 个事件怎么办？
**答案**：只有 2 个物理计数器，需分两轮运行（每轮选 2 个事件），前提是被测负载是确定性的、可重复运行。

---

### 4.3 profile 剖析

#### 4.3.1 概念说明

事件计数回答「某类事发生了多少次」，但回答不了「**时间都花在哪个函数里**」。后者靠**采样剖析**：周期性地记录当前 PC，样本越多的函数，占用时间越多。这是一种统计近似——样本数 ≈ 占用周期占比。

Nyuzi 的采样剖析由两部分组成：

- **采样器（testbench）**：Verilator 仿真时，每个时钟周期以 1/64 的概率随机挑一个线程，把它的「下一 PC」写进文件。
- **分析器（profile.py）**：拿符号表把每个 PC 归属到函数，统计每个函数被采到多少次。

#### 4.3.2 核心流程

```
仿真启动时：  +profile=pc.txt  →  soc_tb 打开 pc.txt
每个时钟沿：  以 1/64 概率，随机选一个线程，
              写 next_program_counter[thread]（十六进制）一行到 pc.txt
仿真结束：    关闭 pc.txt

线下分析：
  1. llvm-objdump -t program.elf  > symbols.txt   （取符号表）
  2. python3 tools/misc/profile.py symbols.txt pc.txt
     → profile.py 把每个 PC 二分查找归到某函数，累加计数，
        按计数降序打印「计数  占比  函数名」
```

采样源是取指级的 `next_program_counter`（即「即将执行的下一条指令地址」），所以采样到的就是「CPU 正准备执行的指令」，符合「时间分布」的直觉。

#### 4.3.3 源码精读

**开启采样**在 testbench 的初始化里，靠 `+profile=<文件名>` plusarg 触发：

[hardware/testbench/soc_tb.sv:438-444](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/testbench/soc_tb.sv#L438-L444) ——传了 `profile=%s` 就置 `profile_en=1` 并 `$fopen` 文件，否则不采样。

**采样动作**在每个时钟沿的块里：

[hardware/testbench/soc_tb.sv:541-543](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/testbench/soc_tb.sv#L541-L543) ——条件 `($random() & 63) == 0` 即每拍约 1/64 命中；命中时随机挑一个线程，写 `CORE0.ifetch_tag_stage.next_program_counter[$random % THREADS_PER_CORE]` 的十六进制到文件。

文件关闭见 [hardware/testbench/soc_tb.sv:497-498](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/testbench/soc_tb.sv#L497-L498)。

**分析器** `profile.py`：用正则从 `llvm-objdump -t` 的输出里抠出全局函数符号：

[tools/misc/profile.py:32-33](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/misc/profile.py#L32-L33) ——匹配形如 `地址 g  F .text 大小 名字` 的行。

**PC → 函数**用二分查找（符号按地址排序后，找最后一个起始地址 ≤ PC 的符号）：

[tools/misc/profile.py:48-60](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/misc/profile.py#L48-L60) ——`find_function` 实现该查找。

**统计与输出**：逐行读 PC 文件归函数并计数，最后按计数降序打印：

[tools/misc/profile.py:79-83](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/misc/profile.py#L79-L83) 与 [:91-95](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/misc/profile.py#L91-L95) ——输出 `计数  占比%  函数名`。

`+profile` 的用法也在 [hardware/README.md:54](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/README.md#L54) 列出（「Periodically write the program counters to a file. Use with tools/misc/profile.py」）。

#### 4.3.4 代码实践

**实践目标**：对一个程序生成热点报告，找出最耗时的函数。

**操作步骤**（运行型，命令的具体写法**待本地验证**）：

1. 构建一个有点计算量的程序（例如 `software/apps/` 下的某个示例，或自带一段循环的小程序），得到其 ELF。
2. 导出符号表：
   ```bash
   /usr/local/llvm-nyuzi/bin/llvm-objdump -t program.elf > symbols.txt
   ```
3. 用 Verilator 模型跑，并带 `+profile` 采样：
   ```bash
   ./run_verilator program.hex +profile=pc.txt
   ```
4. 生成报告：
   ```bash
   python3 tools/misc/profile.py symbols.txt pc.txt
   ```

**需要观察的现象**：终端按计数降序打印若干行 `计数 占比 函数名`；排在前面的就是占用周期最多的热点函数。

**预期结果**：计算密集函数（如渲染/拷贝循环）占比高；启动/收尾代码占比低。占比是相对采样总数 `total_cycles`（即所有被采到的 PC 数，并非真实周期数）算的。

> **注意**：采样剖析**只在 Verilator 周期精确模型里可用**，C 模拟器（`nyuzi_emulator`）没有 `+profile`。样本量取决于运行时长与 1/64 命中率，程序太短样本会偏少、统计噪声大。

#### 4.3.5 小练习与答案

**练习 1**：采样率是 1/64，为什么不定成「每拍都采」？
**答案**：每拍都采会产生巨大文件、且与「样本近似时间分布」的目标相悖；1/64 既够稀疏（文件小）又能保证统计意义。每拍都采等价于周期精确 trace，已不是「采样」。

**练习 2**：`profile.py` 报告里的 `total_cycles` 是真实周期数吗？
**答案**：不是。它是被采到的 PC 行数（≈ 总周期数 / 64），仅用于算各函数占比；真实周期数应读 `CR_CYCLE_COUNT`。

**练习 3**：采样挑的是随机线程，多线程程序下报告还准吗？
**答案**：仍具统计意义——各线程被等概率采样，占比反映「所有线程合在一起的时间分布」。但要分析单线程热点需结合事件计数或单线程运行。

---

## 5. 综合实践

把「采样剖析」与「事件计数」串起来，定位并量化一个性能问题：

1. **找热点**：挑一个程序（建议 `sceneview` 或一段自写的向量循环），按 4.3.4 用 `+profile` + `profile.py` 生成报告，记下占用周期最多的 1–2 个函数。
2. **量化缓存行为**：针对热点函数所在的代码段，用 4.2.4 的控制寄存器方法，分轮统计 I-Cache miss、D-Cache miss、退休指令数、周期数。
3. **算比率**：计算
   - 每千条退休指令的 D-Cache 缺失数 \(\;= \frac{\text{dcache\_miss}}{\text{inst\_retire}} \times 1000\)；
   - 平均每条退休指令周期数 \(\;\text{CPI} = \frac{\text{cycles}}{\text{inst\_retire}}\)。
4. **改一处、再测一次**：例如把热点循环的访存模式从「跨步」改成「顺序」、或展开循环以减少指令数，重新跑 1–3 步，观察 miss 数与 CPI 的变化。

**预期**：顺序访存应显著降低 D-Cache miss；CPI 随 miss 下降而下降。把「改前/改后」两组数字对照记录下来，就是一份最小的性能调优报告。

> 事件索引请严格使用 4.2.3 表格里的硬件位索引（5=icache_miss, 8=dcache_miss, 3=inst_retire），**不要**使用 libos `performance_counters.h` 枚举里的值——后者对应的是未接线的 MMIO 接口。

## 6. 本讲小结

- Nyuzi 的性能事件由各流水级产生单比特脉冲，在 [core.sv:403-418](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L403-L418) 聚合成 14 位 `perf_events` 总线，由通用的 `performance_counters` 模块逐拍累加。
- 只有 **2 个物理计数器**，每个用 4 位索引从 14 个事件中选 1 个；事件选择走控制寄存器 `CR_PERF_EVENT_SELECT0/1`(22/23)，64 位计数经 `CR_PERF_EVENT_COUNT0/1_L/H`(24–27) 分半字读出。
- 周期计数 `CR_CYCLE_COUNT`(6) 每拍自增，可用来算 CPI、事件/周期等比率。
- **关键避坑**：仓库里 libos 的 `performance_counters.c/.h` 走 MMIO `REG_PERF*`，但模拟器与 GPGPU 核都**未接线**，事件枚举也与硬件位索引不一致；真正可用的是控制寄存器路径。
- 采样剖析由 testbench 的 `+profile=<file>`（每拍 1/64 概率写一个线程的 `next_program_counter`）与 `profile.py`（二分归函数、统计占比）配合完成，**仅 Verilator 模型可用**。
- 事件计数答「发生了多少次」，采样剖析答「时间花在哪」，二者互补；综合实践把它们组合成「定位热点 → 量化缓存 → 改进 → 复测」的调优闭环。

## 7. 下一步学习建议

- 想深入「事件从哪一拍产生」：阅读 [int_execute_stage.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv)、[dcache_data_stage.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv) 中各 `*_perf_*` 信号的赋值条件。
- 想理解多核下的性能观察：结合 u10-l3（多核与 L2 仲裁），思考「为什么 L2 事件没有进入核内计数器」以及如何在 L2 层做统计。
- 调试方向：继续 u11-l3（GDB 远程调试），把「断点单步」与「性能计数」组合使用。
- 验证方向：阅读 [tests/unit/test_control_registers.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/unit/test_control_registers.sv)，学习如何为控制寄存器/性能计数写周期精确单元测试（承接 u15）。
