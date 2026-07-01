# CLINT 与 PLIC 中断

## 1. 本讲目标

本讲讲解 CoralNPU SoC 中两类「标准 RISC-V 中断控制器」：**CLINT**（Core-Local Interruptor，机器定时器与软件中断）与 **PLIC**（Platform-Level Interrupt Controller，外部中断）。学完本讲你应该能够：

1. 在 `Clint.scala` 中定位 `msip` / `mtime` / `mtimecmp` 三个寄存器，并解释 `mtip` 定时器中断如何由 `mtime >= mtimecmp` 产生。
2. 在 `Plic.scala` 中追踪一个外部中断源从 `io.srcs` 触发、经优先级仲裁、`claim`/`complete` 到 `io.irq` 拉高的完整流程，并说清电平触发与边沿触发两种网关（gateway）行为的差异。
3. 说出 CLINT/PLIC 的输出如何挂到 SoC 总线、又如何最终驱动标量核进入 trap（`mcause` / `mie` / `mstatus.MIE`）。
4. 理解在 CoralNPU「run-to-completion」标量核模型下，中断扮演的是「让核能在 `wfi` 中睡眠、被事件唤醒」的角色。

---

## 2. 前置知识

阅读本讲前，建议你已建立以下认知（见依赖讲义 u1-l1、u3-l1、u3-l4、u5-l3）：

- **TL-UL 总线**：CLINT 和 PLIC 都是挂在 SoC 内部 TileLink-UL（TL-UL）总线上的**从机（device）**，CPU 用普通的 `Get`/`PutFullData` 事务读写它们的寄存器。TL-UL 只有两个通道 A（请求）与 D（响应），经 `valid`/`ready` 握手。
- **RISC-V 机器模式中断**：机器模式下有三类本地中断，对应 `mip`/`mie` CSR 的三个位：
  - **MSIP / MSIE**（bit 3）：机器软件中断——由软件写 CLINT 的 `msip` 触发。
  - **MTIP / MTIE**（bit 7）：机器定时器中断——由 CLINT 的 `mtime >= mtimecmp` 触发。
  - **MEIP / MEIE**（bit 11）：机器外部中断——由 PLIC 汇总所有外部源后拉高 `irq` 触发。
- **trap 与 `mtvec`/`mcause`**：当 `mstatus.MIE`=1 且某类中断的 `mie` 位使能、且其 pending 条件成立时，核在指令边界进入 trap：PC 跳到 `mtvec`，`mcause` 写入中断原因码（最高位 1 表示中断），如 MEI=`0x8000000B`、MSI=`0x80000003`、MTI=`0x80000007`。
- **CoralNPU 标量核的 run-to-completion 模型**：典型 ML 负载是一条「加载→计算→halt」的程序。即便如此，SoC 仍按 RISC-V 标准配备了 CLINT/PLIC，使核能在 `wfi`（Wait For Interrupt）中停跑省电、被中断唤醒后再继续——这就是本讲要讲的硬件基础。

> 关键直觉：CLINT 管「核自己产生的两类本地中断」（软件、定时器），PLIC 管「核外一堆外设引脚汇成一根外部中断线」。它们都是「内存映射的 TL-UL 从机 + 一根/几根中断输出线」的简单结构。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [hdl/chisel/src/bus/Clint.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Clint.scala) | CLINT：实现 `msip`/`mtime`/`mtimecmp` 寄存器，输出 `mtip`/`msip` 两根中断线。 |
| [hdl/chisel/src/bus/Plic.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Plic.scala) | PLIC：31 个外部中断源的优先级/使能/网关/claim-complete 状态机，输出单根 `irq`。 |
| [hdl/chisel/src/soc/SoCChiselConfig.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/SoCChiselConfig.scala) | 把 `clint`、`plic` 声明为 SoC 模块，并把 PLIC 的 `io.srcs` 提升为顶层外部端口 `ext_intrs`。 |
| [hdl/chisel/src/soc/CrossbarConfig.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CrossbarConfig.scala) | 给出 CLINT/PLIC 的总线地址区间（CLINT `0x0200_0000`，PLIC `0x0C00_0000`）。 |
| [hdl/chisel/src/soc/CoralNPUChiselSubsystem.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUChiselSubsystem.scala) | 实例化 CLINT/PLIC，并把 `mtip`/`msip`/`irq` 三根线手工接到标量核。 |
| [hdl/chisel/src/coralnpu/scalar/Csr.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala) | 在核内把三根中断线与 `mie`/`mip`/`mstatus` 结合，决定是否进入 trap。 |
| [tests/cocotb/plic_test.cc](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/plic_test.cc) | 跑在核上的 C++ 中断处理演示：配置 PLIC、写 ISR、`wfi` 等中断。 |
| [tests/cocotb/tlul/test_subsystem.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tlul/test_subsystem.py) | cocotb 测试台：用 TL-UL 主机写 CLINT 的 `msip`、驱动 PLIC 的 `ext_intrs` 引脚。 |

---

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：**4.1 CLINT**（定时器 + 软件中断）、**4.2 PLIC**（外部中断仲裁）、**4.3 中断通路**（三根线如何到达内核 trap）。

### 4.1 CLINT：机器定时器与软件中断

#### 4.1.1 概念说明

CLINT（Core-Local Interruptor）是 SiFive/RISC-V 平台的标准部件，负责两类**本地**中断：

- **MSIP**（Machine Software Interrupt Pending）：一个 1 位的软件中断位。任何能写 CLINT 内存的总线主机（CPU 自己、或外部主机）写 `msip`=1，就在本地核上挂起一次软件中断。常用于多核间 IPI（核间中断），在 CoralNPU 单核场景里则用于「外部主机把核从 `wfi` 中踢醒」。
- **MTIP**（Machine Timer Interrupt Pending）：一个 64 位自由计数器 `mtime` 每周期自增，当 `mtime >= mtimecmp` 时挂起定时器中断。软件把 `mtimecmp` 写成「未来某一刻」即可定时唤醒。

CLINT 本身**不做仲裁、不向 trap**，它只是两根组合输出线 `mtip`/`msip`，最终由核的 CSR 模块决定是否真正进 trap（见 4.3）。

#### 4.1.2 核心流程

CLINT 的数据流非常简单：

```
        TL-UL 主机 (CPU/外部)                CLINT 内部                  标量核 CSR
   Get/Put ───────────────►  msip(32)   ──msip(0)──►  io.msip ──►  software_irq
   Get/Put ───────────────►  mtimecmp(64)            io.mtip ──►  timer_irq
   Get    ───────────────►  mtime(64)   (每周期 +1)        ▲
                                                  mtime>=mtimecmp
```

定时中断的产生是一个无符号比较：当且仅当 `mtime >= mtimecmp` 时 `mtip` 拉高。设时钟频率为 \(f\)，当前 `mtime` 为 \(t_0\)，要延迟 \(T\) 秒触发，则应写入

\[
\text{mtimecmp} = t_0 + f \cdot T
\]

注意 `mtimecmp` 的复位值是全 1（`0xFFFF...FF`），所以上电后 `mtime < mtimecmp` 永远成立之前，`mtip` 不会误触发——软件必须显式写一个「未来值」才会定时。

#### 4.1.3 源码精读

寄存器偏移用 `ChiselEnum` 集中定义，采用 SiFive 标准 CLINT 布局：

[Clint.scala:21-27](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Clint.scala#L21-L27) —— 定义 CLINT 寄存器偏移：`MSIP`=`0x0000`、`MTIMECMP`=`0x4000`/`0x4004`（低/高 32 位）、`MTIME`=`0xBFF8`/`0xBFFC`。结合 CLINT 基址 `0x0200_0000`（见 [CrossbarConfig.scala:114](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CrossbarConfig.scala#L114)），`MSIP` 的绝对地址就是 `0x0200_0000`，与 cocotb 测试里的 `CLINT_MSIP = 0x02000000` 完全一致。

[Clint.scala:29-46](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Clint.scala#L29-L46) —— 模块 IO 与中断输出。CLINT 一边是 TL-UL 从机端口 `io.tl`，另一边是两根输出 `mtip`/`msip`；三个寄存器 `msip`（32 位）、`mtime`（64 位）、`mtimecmp`（64 位，复位全 1）。两根中断线是**纯组合**逻辑：

```scala
io.mtip := mtime >= mtimecmp   // 定时器中断：自由计数器追上比较值
io.msip := msip(0)             // 软件中断：仅看 msip 的 bit0
```

[Clint.scala:68-77](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Clint.scala#L68-L77) —— `mtime` 的「自增 + 可写覆盖」逻辑，是 CLINT 里最值得品味的细节。默认每周期 `mtime + 1.U`，但用 `MuxCase` 给出三类覆盖：

```scala
mtime := MuxCase(mtime + 1.U, Seq(
  // 写 MTIME_LO：替换低 32 位，保留高 32 位
  (write MTIME_LO) -> Cat(mtime(63,32), data),
  // 写 MTIME_HI：替换高 32 位，保留低 32 位
  (write MTIME_HI) -> Cat(data, mtime(31,0)),
  // 写 MTIMECMP 期间冻结自增（避免比较窗口里 mtime 漏跳）
  (write MTIMECMP_LO/HI) -> mtime
))
```

这里有一个**刻意的设计**：当软件正在写 `mtimecmp` 时，`mtime` 暂停自增一拍。否则软件读到的「旧 `mtime`」与写下的「新 `mtimecmp`」之间可能跨过若干周期，导致定时偏差甚至错过中断。冻结一拍换来了「读 `mtime`→算差值→写 `mtimecmp`」这个标准序列的可预测性。

[Clint.scala:79-91](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Clint.scala#L79-L91) —— `mtimecmp` 也是按 32 位高低半字分别写入，因为 TL-UL 数据总线是 32 位的，64 位比较值必须分两次写。

[Clint.scala:93-124](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Clint.scala#L93-L124) —— 读数据选择与 TL-UL 响应：`read_data` 用 `MuxLookup` 按地址返回对应寄存器（`msip`、`mtimecmp` 高低半字、`mtime` 高低半字）；响应遵循 TL-UL「写回 `AccessAck`、读回 `AccessAckData`」的契约，且每次只允许一拍在途（`tl_a.ready := !tl_d_valid`），是一个最简的单拍从机。

#### 4.1.4 代码实践

**实践目标**：亲手用 cocotb 触发一次 CLINT 软件中断，观察核从 `wfi` 醒来并跑完程序。

**操作步骤**：

1. 阅读现成的 cocotb 用例 `test_software_interrupt`：[test_subsystem.py:791-862](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tlul/test_subsystem.py#L791-L862)。它加载 `software_interrupt_test.elf`（程序里 `wfi` 自旋），用 TL-UL 主机写 `0x02000000`（`CLINT_MSIP`）值为 1。
2. 找到触发语句：[test_subsystem.py:842-848](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tlul/test_subsystem.py#L842-L848)。
3. 在仓库根目录尝试运行该测试（命令需待本地验证，标签请参考 `tests/cocotb/tlul/BUILD`）：
   ```bash
   bazel test //tests/cocotb/tlul:core_mini_axi_sim_cocotb \
     --test_filter=test_software_interrupt --test_output=streamed
   ```
4. 阅读被测程序：找到 `software_interrupt_test.cc`（与 `plic_test.cc` 同目录），确认它设置了 `mtvec`、使能 `mie.MSIE`(bit3) 与 `mstatus.MIE`(bit3)、然后 `wfi`。

**需要观察的现象**：

- 写 `CLINT_MSIP`=1 之后若干周期，`io_external_ports_halted` 最终变 1（程序跑完），且 `io_external_ports_fault`=0（无错误），即 ISR 正确响应了软件中断。

**预期结果**：测试打印 `Software interrupt test passed.`。

> 命令的确切 bazel 目标名请以 `tests/cocotb/tlul/BUILD` 里的 `cocotb_test`/`coco_tb` 规则定义为准；若本地无法跑通，退化为「源码阅读型实践」：对照 [Clint.scala:45-46](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Clint.scala#L45-L46) 解释「为什么写 `msip` 的 bit0 就能让核进 MSI trap」，并标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`mtimecmp` 的复位值为什么是全 1 而不是 0？

> **答案**：若复位为 0，则上电时 `mtime(0) >= mtimecmp(0)` 立即成立，会在软件还没来得及设置中断向量前就误触发定时器中断。全 1 让 `mtime` 必须跑很久（\(2^{64}-1\) 拍）才会自然追上，等于「默认不产生定时中断」，由软件显式写入未来值才启用。

**练习 2**：为什么写 `mtimecmp` 的那个周期，`mtime` 要暂停自增？

> **答案**：软件通常先读 `mtime`、算出目标值、再写 `mtimecmp`。如果写 `mtimecmp` 的同时 `mtime` 还在往前跳，那么软件基于「旧 `mtime`」算出的比较值可能与实际不符，引入定时误差；暂停一拍让写入原子地落在某个确定的 `mtime` 上，序列可预测。

---

### 4.2 PLIC：外部中断的优先级仲裁与 claim/complete

#### 4.2.1 概念说明

PLIC（Platform-Level Interrupt Controller）把**一堆外部中断源**（外设引脚）汇成**一根**外部中断线 `irq` 送给核。它要做四件事：

1. **网关（gateway）**：把每个原始引脚 `srcs(i)` 按「电平 / 边沿」转换成一个内部 `pending` 位，屏蔽毛刺、保证一次触发只挂起一次。
2. **优先级仲裁**：在所有「pending 且 enable」的源里选出优先级最高的一个（HPPI，Highest Priority Pending Interrupt）。
3. **阈值过滤**：仅当 `最高优先级 > threshold` 时才拉高 `irq`。
4. **claim / complete**：核响应中断后，先 **claim**（读 `CLAIM` 寄存器）拿走中断源 ID（同时清 pending），处理完再 **complete**（写 `CLAIM`）告知 PLIC 已处理完毕——这对电平触发源尤其关键，它决定「电平还挂着时是否允许再次中断」。

> CoralNPU 的 PLIC 是**单上下文（单目标）**精简版：只有一组 `enable`/`threshold`/`claim`，对应核的机器外部中断（MEI）。标准 PLIC 是多目标、每目标一套上下文的；这里裁剪到「一个核用一根线」。

#### 4.2.2 核心流程

一个外部中断从触发到核响应的完整生命：

```
外设引脚 srcs(i) ──gateway──► pending(i)
                                 │  (pending && enable ? priority(i) : 0)
                                 ▼
                 HPPI 仲裁 = max(优先级) ──► max_id / max_prio
                                 │
                        max_prio > threshold ?
                                 │ yes
                                 ▼
                          io.irq = 1  ──► 核进 MEI trap (mcause=0x8000000B)
                                 │
                          ISR 里读 CLAIM
                                 ▼
                   返回 max_id，并清 pending(max_id)
                          （电平源：置 waiting_for_complete）
                                 │
                          ISR 处理完，写 CLAIM=id
                                 ▼
                   complete：清 waiting_for_complete(id)
                   （若电平源仍高，下一拍可再次 pending）
```

优先级仲裁的数学很直接。设有 \(n\) 个源，每个源的有效优先级

\[
p_i = \begin{cases} \text{priority}(i) & \text{pending}(i) \land \text{enable}(i) \\ 0 & \text{otherwise} \end{cases}
\]

则被选中的源

\[
\text{id}^* = \arg\max_{i} p_i
\]

实现用严格大于 `>` 折叠，因此**优先级相同时取 ID 最小者**（先到先得）。输出中断线

\[
\text{irq} = (\max_i p_i) > \text{threshold}
\]

注意是严格大于：`priority` 写 0 的源视为「永不中断」（与阈值 0 比较为假）。

#### 4.2.3 源码精读

[Plic.scala:21-27](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Plic.scala#L21-L27) —— PLIC 寄存器偏移。注意它用的是 24 位地址 `address(23,0)`，所以偏移跨度大：优先级区 `0x000000`~`0x000FFC`、`PENDING`=`0x001000`、`LE`=`0x001080`、`ENABLE`=`0x002000`、`THRESHOLD`=`0x200000`、`CLAIM`=`0x200004`。结合 PLIC 基址 `0x0C00_0000`（[CrossbarConfig.scala:115](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CrossbarConfig.scala#L115)），`CLAIM` 绝对地址 = `0x0C00_0000 + 0x200004` = `0x0C20_0004`，正是 `plic_test.cc` 里硬编码的 `0x0C200004`。

[Plic.scala:29-40](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Plic.scala#L29-L40) —— 模块参数与 IO。`numInterrupts=31`、`priorityWidth=3`（来自 [SoCChiselConfig.scala:242](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/SoCChiselConfig.scala#L242)）。IO 三件：TL-UL 从机 `tl`、`srcs`（31 位原始引脚输入）、`irq`（单根汇总输出）。`require` 限定源数 1~31、优先级宽 1~32。

[Plic.scala:42-56](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Plic.scala#L42-L56) —— 寄存器组。注意所有位宽都是 `numInterrupts+1`：**源 0 是保留的「空源」**，永远不中断。`pending`/`le`/`enable`/`waiting_for_complete` 的 bit0 恒为 0。`src_q = RegNext(io.srcs)` 是上一拍的引脚值，用于边沿检测。

[Plic.scala:67-80](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Plic.scala#L67-L80) —— HPPI 仲裁。先算每个源的有效优先级（`pending && enable` 才取 `priority`，否则 0），再用 `foldLeft` 从源 1 到 31 折叠出 `(max_prio, max_id)`：

```scala
val p_greater = p > max_p
(Mux(p_greater, p, max_p), Mux(p_greater, idx.U, max_i))
```

严格 `>` 保证「同优先级取小 ID」。源 0 的 `active_priorities(0)` 恒为 0，不参与。

[Plic.scala:82-86](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Plic.scala#L82-L86) —— claim/complete 信号。`actual_claim_id` 在「读 CLAIM 那拍」等于 `max_id`（否则 0）；`complete_id` 在「写 CLAIM 那拍」等于写入数据里的 ID。二者驱动下面的 pending/waiting 状态机。

[Plic.scala:90-103](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Plic.scala#L90-L103) —— **网关核心**：`pending` 位的置位/清零。对每个源 \(i\ge 1\)：

```scala
val s            = io.srcs(i-1)              // 当前引脚电平
val sq           = src_q(i-1)                // 上一拍电平
val edge_trigger = s && !sq                  // 上升沿
val level_trigger= s && !waiting_for_complete(i) // 电平高 且 未在"已claim等待complete"
val set_p  = Mux(le(i), edge_trigger, level_trigger) // le:1=边沿,0=电平
val clear_p= (actual_claim_id === i.U)       // 被claim时清
pending(i) := clear_p ? 0 : (set_p ? 1 : pending(i))
```

- **边沿源**：`le(i)=1`，仅在上升沿置 pending，claim 后清；与 `waiting_for_complete` 无关。
- **电平源**：`le(i)=0`，只要引脚为高且「没在 claim 后等待 complete」就置 pending。

[Plic.scala:105-114](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Plic.scala#L105-L114) —— `waiting_for_complete`：**电平源的防抖状态**。claim 电平源时置位，complete 时清位。它保证：一个持续高电平的源，在被 claim 后不会立刻重新 pending，直到软件 complete；若 complete 后引脚仍高，下一拍 `level_trigger` 再次为真，重新 pending——这正是标准 PLIC 对电平源的语义（「处理完了源头还在？那就再中断一次」）。

[Plic.scala:116-140](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Plic.scala#L116-L140) —— 优先级、使能、阈值、LE 的写入。两处细节值得注意：

```scala
// enable / le 写入都强制把 bit0 清零
Cat(tl_a.bits.data(numInterrupts, 1), 0.U(1.W))
```

`Cat(data(numInterrupts,1), 0)` 把数据位 [numInterrupts:1] 放到结果的 [numInterrupts:1]、结果 bit0 恒 0——即**源 0 永远不可使能**，符合 PLIC 规范。优先级寄存器按 `addr` 的字下标 `addr(...,2)` 索引（[Plic.scala:146](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Plic.scala#L146)），源 0 的优先级被显式跳过（`i.U =/= 0.U`）。

[Plic.scala:142-157](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Plic.scala#L142-L157) —— 输出与读数据。`io.irq := max_prio > threshold`（严格大于）。读 `CLAIM` 返回 `max_id`（[Plic.scala:155](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Plic.scala#L155)），即「读动作本身就完成 claim」。响应时序与 CLINT 同构（单拍在途、`AccessAck`/`AccessAckData`）。

#### 4.2.4 代码实践

**实践目标**：跑通 `test_plic`，亲眼看到 PLIC 同时处理一个电平源和一个边沿源。

**操作步骤**：

1. 阅读核上程序 [plic_test.cc:96-117](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/plic_test.cc#L96-L117) 的 `main`：
   - 设 `mtvec` 指向 `plic_isr_wrapper`；
   - `PLIC_PRIO(1)=5`、`PLIC_PRIO(2)=10`、`PLIC_LE=(1<<2)`（源 2 边沿）、`ENABLE=(1<<1)|(1<<2)`、`THRESHOLD=0`；
   - `csrs mie, (1<<11)` 使能 MEIE、`csrs mstatus, (1<<3)` 开全局 MIE；
   - `while(intr_count < 2) wfi;` 阻塞等两次中断。
2. 阅读 ISR [plic_test.cc:45-92](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/plic_test.cc#L45-L92)：判断 `mcause==0x8000000B`，读 `CLAIM` 拿 id、`intr_count++`、经 UART1 打印、写 `CLAIM` complete。
3. 阅读 cocotb 驱动 [test_subsystem.py:942-956](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tlul/test_subsystem.py#L942-L956)：
   - `ext_intrs=1`（bit0→源 1，**电平**）；
   - `ext_intrs=3` 再 `=1`（bit1 由 0→1→0，源 2 **上升沿**）。
4. 运行（目标名以 `tests/cocotb/tlul/BUILD` 为准，**待本地验证**）：
   ```bash
   bazel test //tests/cocotb/tlul:core_mini_axi_sim_cocotb \
     --test_filter=test_plic --test_output=streamed
   ```

**需要观察的现象**：

- UART 输出里应出现 `R`（程序启动）、两次 `I` + hex（两次中断各打印一个被 claim 的 id，应为 `00000001` 与 `00000002`）、最后 `D`（`intr_count` 到 2 退出循环）。
- `ext_intrs` 拉高源 1 后，即便它保持高电平，因为电平网关的 `waiting_for_complete`，**不会**在 complete 之前反复中断。

**预期结果**：测试打印 `PLIC test passed.`，且 `io_external_ports_halted`=1、`fault`=0。

**进阶（修改观察）**：把 [plic_test.cc:103](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/plic_test.cc#L103) 的 `PLIC_THRESHOLD` 改成 `6`，重编重跑——此时源 1（优先级 5）被阈值过滤，只有源 2（优先级 10）能中断，`intr_count` 将卡在 1 永不退出（除非你再触发一次源 2）。借此验证「`max_prio > threshold` 是严格大于」。

#### 4.2.5 小练习与答案

**练习 1**：`plic_test.cc` 里源 1 是电平触发、源 2 是边沿触发，依据是哪条配置？为什么源 1 一直保持高电平却不会无限触发中断？

> **答案**：依据 `PLIC_LE = (1u << 2)`——`le` 的 bit2=1 表示源 2 是边沿，其余（含源 1）为电平。源 1 不会无限触发，是因为电平网关在 claim 后会置 `waiting_for_complete(1)`，使 `level_trigger = s && !waiting = 0`，pending 不再重置；只有软件 complete（写 `CLAIM=1`）清掉 `waiting` 后，若引脚仍高才会再次 pending。

**练习 2**：若两个源同时 pending、优先级都等于 `threshold`，`io.irq` 会拉高吗？

> **答案**：不会。`io.irq := max_prio > threshold` 是**严格大于**。优先级等于阈值时不触发；且若两源优先级为 0（默认复位值），`active_priorities` 全 0，`max_prio=0`，与阈值 0 比较为假，故复位态下 PLIC 永不拉 `irq`——必须软件显式给源写非零优先级、并设合理阈值。

**练习 3**：为什么 claim 用「读」、complete 用「写」同一个 `CLAIM` 地址？

> **答案**：这是 RISC-V PLIC 的标准协议。读 `CLAIM` 是原子的「取走当前最高优先级源的 ID 并清其 pending」，让 ISR 知道该分派给哪个设备；处理完设备后必须写同一地址回送 ID 完成 complete，否则电平源的 `waiting_for_complete` 永不清、该源此后不再产生中断。读写复用地址省了一组寄存器，靠 opcode（Get vs Put）区分语义。

---

### 4.3 中断通路：从 CLINT/PLIC 输出到内核 trap

#### 4.3.1 概念说明

CLINT 的两根线（`mtip`/`msip`）和 PLIC 的一根线（`irq`）本身只是组合输出，**不会自动让核进 trap**。要让中断真正生效，需要三步接线：① SoC 顶层把这三根线连到核的 `timer_irq`/`software_irq`/`irq` 输入；② 核边界把它们寄存一拍打拍；③ CSR 模块用 `mie`/`mstatus.MIE` 做最终裁决，决定是否真的进 trap。本模块把这整条通路串起来——它解释了「4.1/4.2 的硬件输出如何变成一次真实的 trap」。

#### 4.3.2 核心流程

```
CLINT.mtip ─────────────────────────────────┐
CLINT.msip ────────────────────────────┐    │     CoralNPUChiselSubsystem
PLIC.irq  ───────────────────────┐     │    │     (手工三根线)
                                  ▼     ▼    ▼
   rvv_core.io.irq / timer_irq / software_irq
                                  │
                          CoreAxi 边界 RegNext 打拍
                                  ▼
                       Core → SCore → Csr.io.{irq,timer_irq,software_irq}
                                  │
                  meip = irq      & mie(11)
                  mtip = timer_irq& mie(7)
                  msip = software & mie(3)
                                  │
        interrupt_pending = (meip|mtip|msip) & mstatus.MIE & !in_debug
                                  │  (在指令边界由 BRU 提交)
                                  ▼
                        进 trap：PC←mtvec, mcause←0x8000000{B,7,3}
```

裁决优先级（同时 pending 时）由 `mcause` 选择顺序决定：**MEI > MSI > MTI**。

#### 4.3.3 源码精读

[SoCChiselConfig.scala:232-247](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/SoCChiselConfig.scala#L232-L247) —— CLINT/PLIC 的 SoC 声明。二者都以 `deviceConnections = Map("io.tl" -> ...)` 挂到总线成为 TL-UL 从机；PLIC 额外用 `ExternalPort("ext_intrs", Logic(31), In, "io.srcs")` 把 31 根原始中断引脚提升到顶层（这就是 cocotb 里 `io_external_ports_ext_intrs` 的由来）。CLINT 无外部端口——它的 `mtip`/`msip` 只在片内连到核。

[CoralNPUChiselSubsystem.scala:171-181](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUChiselSubsystem.scala#L171-L181) —— 实例化 CLINT/PLIC。二者都用一份 `lsuDataBits=32` 的本地 `Parameters`（因为 TL-UL 数据宽 32 位）。

[CoralNPUChiselSubsystem.scala:273-286](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUChiselSubsystem.scala#L273-L286) —— **三根中断线的手工连接**（CLINT/PLIC 的输出不属于总线，通用连线循环管不到，需特例）：

```scala
coreTimerIrq    := modulePorts("clint.io.mtip")   // mtip  -> rvv_core.io.timer_irq
coreSoftwareIrq := modulePorts("clint.io.msip")   // msip  -> rvv_core.io.software_irq
coreIrq         := modulePorts("plic.io.irq")     // irq   -> rvv_core.io.irq
```

[CoreAxi.scala:99-110](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L99-L110) —— 边界打拍 + 时钟门控唤醒。三根中断线先过 `RegNext` 寄存一拍，「to break timing paths to ibus」——避免长组合路径拖慢取指时序。更关键的是时钟门控：

```scala
cg.io.enable := irq_reg || timer_irq_reg || software_irq_reg
               || (!csr.io.cg && !core.io.wfi) || dm.io.haltreq(0)
```

即「有待处理中断就把钟打开」。这意味着核可以停钟进入低功耗，任一中断 pending 会自动唤醒时钟——这就是 `wfi` 省电模式的硬件基础。注意 `core.io.irq := irq_reg || dm.io.haltreq(0)`：调试模块的 halt 请求被 OR 进外部中断路径（用于调试器强停核）。

[Csr.scala:391-394](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L391-L394) —— 在 CSR 内把三根线与 `mie` 结合：

```scala
val mtip_pending = io.timer_irq    && mie(7)   // MTIE
val meip_pending = io.irq          && mie(11)  // MEIE
val msip_pending = io.software_irq && mie(3)   // MSIE
wfi := Mux(wfi, !(mtip_pending||meip_pending||msip_pending||io.dm.debug_req), io.bru.in.wfi)
```

`wfi` 在「无任何可唤醒中断」时保持为真（继续睡），任一 pending 立即清零（醒来）。

[Csr.scala:412](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L412) —— 读 `mip` CSR 时把三根线拼回标准位位置：bit11=`io.irq`、bit7=`io.timer_irq`、bit3=`io.software_irq`，软件可 `csrr mip` 查询 pending。

[Csr.scala:595-602](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L595-L602) —— **最终裁决与 `mcause` 编码**：

```scala
val interrupt_pending = (mtip_pending || meip_pending || msip_pending) && mstatus_mie && !in_debug
io.bru.out.interrupt := interrupt_pending
io.bru.out.interrupt_cause := MuxCase(0.U, Seq(
  meip_pending -> "x8000000B".U,   // MEI
  msip_pending -> "x80000003".U,   // MSI
  mtip_pending -> "x80000007".U,   // MTI
))
```

需要同时满足：① 某类 pending 成立；② `mstatus.MIE`=1（全局中断使能）；③ 不在 debug 模式。`interrupt` 信号交给 BRU 在指令边界提交（trap 也是一种控制流，复用派发屏障机制，见 u5-l1）。`MuxCase` 的顺序即优先级：**MEI > MSI > MTI**。

#### 4.3.4 代码实践

**实践目标**：把「软件写一个寄存器 → 硬件进 trap」的整条链路在源码里走一遍，画出完整调用链。

**操作步骤**（源码阅读型实践，无需运行）：

1. 从「写 `CLINT_MSIP`=1」出发：在 [Clint.scala:66](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Clint.scala#L66) 确认 `msip` 被置 1 → [Clint.scala:46](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Clint.scala#L46) `io.msip` 拉高。
2. 经 [CoralNPUChiselSubsystem.scala:279-281](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUChiselSubsystem.scala#L279-L281) 传到 `rvv_core.io.software_irq`。
3. 经 [CoreAxi.scala:102](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L102) 的 `RegNext` 打拍、[CoreAxi.scala:104](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L104) 唤醒时钟、[CoreAxi.scala:110](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L110) 传入 Core。
4. 在 [Csr.scala:393](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L393) 与 `mie(3)`（MSIE）相与得 `msip_pending`。
5. 在 [Csr.scala:595-601](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L595-L601) 与 `mstatus.MIE` 结合产生 `interrupt`，`mcause`=`0x80000003`。

**需要观察的现象**：把上述 5 步整理成一张「信号名 → 文件:行号」的链路表。

**预期结果**：你能用一句话说清「为什么 `plic_test.cc` 里 `csrs mie,(1<<11)` 和 `csrs mstatus,(1<<3)` 两条指令缺一不可」——前者打开 MEIE（`mie(11)`），后者打开全局 MIE（`mstatus.MIE`），任缺一个 `interrupt_pending` 都为假。

#### 4.3.5 小练习与答案

**练习 1**：核在 `wfi` 时钟停了，外部中断来了，核怎么「醒过来」？

> **答案**：[CoreAxi.scala:104](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L104) 的时钟门控 `cg.io.enable` 把「任一中断 reg 为高」作为开钟条件之一。中断线一到，门控打开、时钟恢复，CSR 模块随即在下一拍算出 `interrupt_pending` 并把 `wfi` 清零，核跳出 `wfi`、在指令边界进 trap。

**练习 2**：三个中断同时 pending，`mcause` 会是哪个？为什么 MEI 优先级最高？

> **答案**：`mcause`=`0x8000000B`（MEI）。因为 [Csr.scala:598-602](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L598-L602) 的 `MuxCase` 把 `meip_pending` 排在最前。MEI 优先最高是 RISC-V 惯例：外部中断通常对应最需要及时响应的外设，软件/定时器中断可稍延后。

---

## 5. 综合实践

**任务**：仿照 `plic_test.cc`，自己编写一个最小的「定时器中断」演示程序 `timer_test.cc`，并配一个 cocotb 测试台驱动它，验证 CLINT 定时器能在给定周期后唤醒 `wfi` 中的核。

要求与提示：

1. **程序侧（参照 [plic_test.cc:96-117](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/plic_test.cc#L96-L117) 的结构）**：
   - 设 `mtvec` 指向你的 `timer_isr`；使能 `mie.MTIE`(bit7) 与 `mstatus.MIE`(bit3)；进入 `wfi` 循环。
   - 注意 CLINT 是 64 位 `mtimecmp`、TL-UL 数据 32 位，需分两次写：先写 `MTIMECMP_LO`(`0x02004000`)、再写 `MTIMECMP_HI`(`0x02004004`)。可先读 `MTIME_LO`(`0x0200BFF8`)/`MTIME_HI`(`0x0200BFFC`) 算出「当前值 + 延迟」。
   - ISR 里把一个全局 `volatile int fired` 置 1，然后 `mret`。
2. **测试台侧（参照 [test_subsystem.py:865-970](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tlul/test_subsystem.py#L865-L970) 的 `test_plic`）**：
   - 用 TL-UL 主机加载 ELF、写 `PC_START`(0x30004)、`RESET_CONTROL`(0x30000) 启动核；
   - 等若干周期让核跑到 `wfi`；可由核自己在 `main` 里写 `mtimecmp`（更简单），或由测试台代写。
3. **验收**：程序在定时器到期后退出 `wfi`、`fired==1`、`halted`=1 且 `fault`=0。

> 本任务是「示例代码」（你新写的 `.cc`/`.py` 非仓库原有文件），请标注清楚；编译与运行命令需**待本地验证**（参考 `tests/cocotb/tlul/BUILD` 与 `rules/coco_tb.bzl` 把新文件接入 cocotb 目标）。重点是借此把 CLINT 的 `mtime/mtimecmp`、CSR 的 `mie/mstatus`、核的 `wfi` 唤醒这一整条链路亲手打通。

---

## 6. 本讲小结

- **CLINT** 是两个本地中断源：`msip`（软件，看 bit0）与 `mtime>=mtimecmp`（定时器，自由计数器每周期 +1），输出 `mtip`/`msip` 两根组合线；`mtimecmp` 复位全 1 避免误触发，写 `mtimecmp` 时冻结 `mtime` 一拍保证定时可预测。
- **PLIC** 把 31 个外部源汇成一根 `irq`：网关按电平/边沿转 `pending`，`foldLeft` 选最高优先级（同优先级取小 ID），`max_prio > threshold`（严格大于）决定是否拉 `irq`；`claim`（读）取 ID 清 pending，`complete`（写）清电平源的 `waiting_for_complete`。
- **电平 vs 边沿网关**：电平源靠 `waiting_for_complete` 防止持续高电平无限触发，complete 后源头仍在才会再次中断；边沿源仅上升沿触发。源 0 永远保留不可使能。
- **三根中断线**（`mtip`/`msip`/`irq`）经 SoC 顶层手工连到核，在 `CoreAxi` 边界打拍并参与时钟门控（中断可唤醒停钟的核），最终在 CSR 由 `mie`/`mstatus.MIE` 裁决，产生 `mcause`（MEI>MSI>MTI）并复用 BRU 的派发屏障进 trap。
- **地址映射**：CLINT `0x0200_0000`（MSIP=`0x0200_0000`、MTIMECMP=`0x0200_4000`、MTIME=`0x0200_BFF8`），PLIC `0x0C00_0000`（CLAIM/COMPLETE=`0x0C20_0004`），均为 32 位 TL-UL 从机。
- **run-to-completion 视角**：CoralNPU 典型 ML 负载不依赖中断，但 CLINT/PLIC 的存在让核支持 `wfi` 低功耗睡眠与标准 RISC-V 中断语义，是 SoC 完整性与可编程性的基础。

---

## 7. 下一步学习建议

- **中断处理的软件侧**：本讲聚焦硬件，ISR 写法、`mtvec` 直连模式、上下文保存/恢复等软件细节可结合 [toolchain/crt/coralnpu_gloss.cc](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/toolchain/crt/coralnpu_gloss.cc) 与 `plic_test.cc` 的裸机汇编进一步练习。
- **总线挂载细节**：CLINT/PLIC 是典型「TL-UL 从机」，其单拍响应时序可对照 [TileLinkUL.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TileLinkUL.scala) 与 u3-l3 复习 A/D 通道握手。
- **新增外设的中断接入**：当你学完 u8-l5（PeripheralInterface 与 GPIO）后，可尝试把一个新外设的中断输出连到 PLIC 的 `io.srcs` 某一位，跑通「外设→PLIC→核 trap」的端到端流程。
- **调试与中断的交互**：[Csr.scala:594](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L594) 里 `!in_debug` 表明 debug 模式下中断被屏蔽——这为 u9-l1（RISC-V Debug 模块）埋下伏笔，后续可深入调试与中断的优先级关系。
