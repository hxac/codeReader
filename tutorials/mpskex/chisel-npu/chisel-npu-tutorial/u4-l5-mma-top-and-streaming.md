# MMALU 顶层集成与流式归约

## 1. 本讲目标

本讲是「矩阵乘法引擎 MMALU」单元（U4）的收口篇。前面四讲我们已经分别拆解了 PE（u4-l1）、脉动阵列（u4-l2）、数据馈送器/收集器（u4-l3）、控制单元（u4-l4）。本讲要做的事是：**把这些零件用螺丝拧在一起，看它们如何在 `MMALU` 这个顶层类里连成一条完整的乘累加流水线**。

学完本讲你应该能够：

1. 说出 `MMALU` 如何实例化并连线 `SystolicArray2D` + `n×n` 个 PE + `DataFeeder` + `DataCollector` + `ControlUnit` 五大组件。
2. 解释为什么 `mma.scala` 里要插入 `pipe_a` / `pipe_b` / `pipe_ctrl` 等「流水寄存器」，以及它们带来「延迟从 3n−2 变为 3n−1」的代价。
3. 掌握 `ctrl.keep = true` 持续拉高时，K×K 阵列原生支持 **M×K 流式归约**、每 K 拍输出一个累积部分和的机制。

---

## 2. 前置知识

本讲依赖前面四讲建立的概念，先用三句话回顾：

- **PE 与 keep**（u4-l1）：每个 PE 有一个永久累加器寄存器 `res`；`keep=true` 时 `res := res + a*b`（累加），`keep=false` 时 `res := a*b`（覆盖/重置）。
- **脉动阵列与波前对齐**（u4-l2）：`SystolicArray2D` 用水平/垂直移位寄存器把输入逐拍送到每个 PE，反对角线（i+j 相同）的 PE 在同一拍处理同一个「波前」，整条链路最长传播 n−1 拍。
- **馈送器/收集器**（u4-l3）：`DataFeeder` 用阶梯延迟（源码称 chainsaw）把逐列喂入的向量扭曲成波前；`DataCollector` 用一个模 n 计数器把 n×n 个 PE 输出逐列回收到对齐的 n 元结果。
- **控制单元**（u4-l4）：`ControlUnit` 用一条深度 2n−1 的一维移位寄存器让控制信号也「脉动」，使控制延迟量等于数据波前延迟量；并用 OR 门把所有在飞的 `keep` 信号汇总成收集使能 `dat_clct`。

如果对其中任何一条还不确定，建议先回到对应讲义。本讲的关键术语只有两个新词：

- **流水寄存器（pipeline register）**：故意插在两个组合逻辑模块之间的 1 拍寄存器，目的是切断过长的组合路径、让电路跑得更高频率。
- **流式归约（streaming reduction）**：不把归约长度钉死在 K，而是让 `keep=true` 一直拉高 M 拍（M 可以远大于 K），让 PE 持续累加。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|:---|:---|
| `src/main/scala/alu/mma/mma.scala` | **本讲主角**。`MMALU` 顶层类，实例化并连线五大组件，插入流水寄存器。 |
| `src/main/scala/alu/pe/procElem.scala` | `MMPE`：实际使用的 PE，`keep` 控制累加/覆盖。 |
| `src/main/scala/alu/mma/cu/controlUnit.scala` | `ControlUnit`：控制脉动移位 + OR 门汇总 `dat_clct`。 |
| `src/main/scala/alu/mma/sa/dataFeeder.scala` | `DataFeeder`：阶梯扭曲输入向量。 |
| `src/main/scala/alu/mma/sa/dataCollector.scala` | `DataCollector`：模 n 计数器回收 PE 输出。 |
| `src/main/scala/isa/micro_op/MMALUMicroCode.scala` | `NCoreMMALUCtrlBundle`：keep/use_accum/busy 三位控制包。 |
| `src/test/scala/alu/mma/MMALUStreamReduceSpec.scala` | 流式归约的端到端验证（M=2K/3K/5K 等）。 |
| `docs/implementations/SystolicArray.md` | 架构文档，含 M×K 流式归约章节与波形。 |

---

## 4. 核心概念与源码讲解

### 4.1 MMALU 顶层集成：把五个组件连成一条流水线

#### 4.1.1 概念说明

前面四讲我们看的是「零件图」，本讲看「装配图」。`MMALU` 是一个通用的矩阵乘法 ALU，它的职责只有一句话：

> 给我两个矩阵的逐列输入（`in_a`、`in_b`），一个可选的偏置（`in_accum`），以及一串控制信号（`ctrl`），我逐拍吐出结果矩阵 `out`，并在算完时拉高 `clct`（collect）。

功能模型上，一个 K×K 的「喂入窗口」计算的是：

\[
Y=\text{Clip}\Big(\text{Clip}(B^{\mathsf T}A)+C\Big)
\]

其中 \(A, B\) 是输入矩阵，\(C\) 是偏置（`in_accum`）。这只是 M=K 的特例——本讲第 3 节会把它推广到任意 M。

#### 4.1.2 核心流程

从输入到输出，数据流经五个站点，可以用下面的伪流水线表示：

```
in_a / in_b  ──►  DataFeeder       ──►  (阶梯扭曲成波前)
                                          │
                                  SystolicArray2D  ──►  (逐拍送到 n×n 个 PE)
                                          │
in_accum ──►  DataFeeder(长延迟 2n−1) ──► DataCollector
                                          │
ctrl ──►  ControlUnit  ──►  (对角线广播 keep/use_accum 给 PE)
                                          │
                      n×n PE (in_a * in_b 累加) ──► DataCollector ──► out
                                                                    └─► clct(算完)
```

关键连线关系：

- `DataFeeder` 的三路输入（a/b/accum）分别来自 `io.in_a` / `io.in_b` / `io.in_accum`。
- `SystolicArray2D` 的两路输入来自 `DataFeeder` 的 `reg_a_out` / `reg_b_out`。
- `n×n` 个 PE 的 `in_a` / `in_b` 来自阵列输出，`ctrl` 来自 `ControlUnit` 的对角线广播。
- PE 的 `out` 全部喂给 `DataCollector` 的 `reg_in`。
- `ControlUnit` 产生 `dat_clct` / `use_accum` 告诉收集器「何时收集」「是否加偏置」，产生 `clct` 告诉外界「整批算完」。

#### 4.1.3 源码精读

**顶层类与 IO 接口。** `MMALU` 是一个参数化类，类型参数 `[T <: BasePE]` 和按名参数 `pe_gen` 让 PE 可被替换（u4-l1 已讲）。默认 `n=8`（阵列边长），`nbits=8`（数据位宽），`accum_nbits=32`（累加位宽）：

[src/main/scala/alu/mma/mma.scala:23-31](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L23-L31) — 定义 `MMALU` 类与五路 IO：`in_a`/`in_b` 是 `Vec(n, SInt(nbits.W))`（n 个有符号数），`in_accum` 是 `Vec(n, SInt(accum_nbits.W))`（宽累加偏置），`ctrl` 是三位控制包，`out` 是 n 个 32 位结果，`clct` 是完成标志。

**实例化五大组件。** 注意 PE 是用 `Seq.fill(n*n){Module(pe_gen).io}` 摆成 n² 个的一维向量（用 `n*i+j` 寻址）：

[src/main/scala/alu/mma/mma.scala:33-45](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L33-L45) — 实例化 `n*n` 个 PE、`DataFeeder`、`DataCollector`、`ControlUnit`；并把 `io.in_a/in_b/in_accum` 接到 feeder 输入，`io.ctrl` 接到 ControlUnit 输入。

**SA 与输出连线。** 阵列输入直接来自 feeder，输出经流水寄存器后送 PE（见 4.2），PE 输出送 collector，collector 输出就是 `io.out`：

[src/main/scala/alu/mma/mma.scala:68-84](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L68-L84) — `sarray.io.vec_a/vec_b` 接 feeder 输出；在双层 `for (i,j)` 循环里把每个 PE 的 `in_a`/`in_b`/`ctrl` 接到 `pipe_a`/`pipe_b`/`pipe_ctrl`，并把 PE 输出接到 `dclct.io.reg_in`。

> **顶层入口提示**：仓库根目录 `top.scala` 当前只 elaborate 出 `MMALU`（`new MMALU(new MMPE(), 32, 8, 32)`），即一个 32×32、INT8 数据、INT32 累加的阵列——所以 `make build` 产出的 `top.sv` 就是这个 MMALU。完整后端 `NCoreBackend` 留待 U6。

#### 4.1.4 代码实践

**实践目标**：通过阅读连线，画出 MMALU 的完整数据通路框图，确认每个组件的输入来自哪里。

**操作步骤**：

1. 打开 `src/main/scala/alu/mma/mma.scala`。
2. 从 `io`（24-31 行）出发，逐个追踪 `in_a`、`in_b`、`in_accum`、`ctrl` 各自流向哪个子模块。
3. 画出一张包含 `DataFeeder`、`SystolicArray2D`、`n×n PE`、`ControlUnit`、`DataCollector` 五个方块的图，用箭头标出每个箭头携带的信号名（如 `reg_a_out`、`cbus_out`、`reg_in`）。

**需要观察的现象**：你会注意到 `in_accum` 并没有走 `SystolicArray2D`，而是经 feeder 长延迟后直接进 `DataCollector`——这是因为偏置是「加到结果上」而不是「参与乘法」。

**预期结果**：得到一张与 4.1.2 伪流水线一致的框图，且能说出 `io.out` 的数据来自 `dclct.io.reg_out`（72 行）。

---

### 4.2 流水寄存器 pipe_*：为 200MHz 时序收敛付出 +1 拍代价

#### 4.2.1 概念说明

在数字电路里，一个时钟周期能完成多少计算，取决于「最长的组合逻辑路径」（critical path，关键路径）。路径越长，信号在一个周期内「跑不完」，电路就达不到目标频率。

`MMALU` 上板（Kintex-7 xc7k480t）目标 200 MHz 时遇到了这个问题。源码头部有一段非常诚实的时间注释，记录了这次时序收敛的过程。解决办法不是重写算法，而是**在两个模块之间插入一拍寄存器**（pipeline register），把一条长路径切成两条短路径。代价是：**总延迟多 1 拍**。

这是 NPU 设计中典型的「用面积/延迟换频率」的取舍——多一拍延迟换来能否跑上目标频率，几乎总是划算的。

#### 4.2.2 核心流程

经典 n×n 脉动阵列的延迟是 **3n−2** 拍（从输入第一个元素到输出最后一个元素，见架构文档）。插入流水寄存器后变成 **3n−1** 拍。

具体到本设计，关键路径出在：

```
SystolicArray2D 的水平移位寄存器(reg_h) ──13 级逻辑(8×CARRY4 + 5×LUT)──► PE 的乘累加链
```

这条路径在 200 MHz 下 WNS（Worst Negative Slack，最差负裕量）= −0.151 ns，即差 0.151 ns 才能满足时序。

修复办法：在「阵列输出」与「PE 输入」之间，以及「控制单元」与「收集器」之间，各插一拍寄存器，把 13 级逻辑切成两段各 6–7 级：

```
[reg_h ──6~7级──►] pipe_a/pipe_b [──6~7级──► PE MAC]
```

> **WNS 是什么**：时序分析工具对每条路径计算「要求时间 − 到达时间 = 裕量（slack）」。slack ≥ 0 表示满足时序；WNS 是所有路径里最差（最小）的那个。WNS = −0.151 ns 意味着最差路径慢了 0.151 ns。

#### 4.2.3 源码精读

**时序修复的「施工记录」注释。** 这段注释本身就是最好的讲义，建议逐字读：

[src/main/scala/alu/mma/mma.scala:13-22](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L13-L22) — 记录 200 MHz 关键路径（reg_h → PE MAC，13 级逻辑，WNS = −0.151 ns），以及修复方案：在 SA→PE 与 CU→Collector 之间插流水寄存器，延迟从 3n−2 变为 3n−1。

**六个流水寄存器的声明。** 注意数据路径（`pipe_a`/`pipe_b`/`pipe_ctrl` 是 n² 个，对应每个 PE）与控制/偏置路径（`pipe_accum` 是 n 个，`pipe_dat_clct`/`pipe_use_accum`/`pipe_clct` 各 1 个）全部都加了 1 拍，保证「数据和控制整体同步后移 1 拍」：

[src/main/scala/alu/mma/mma.scala:52-58](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L52-L58) — 用 `RegInit`（数据/控制）与 `RegNext`（单 bit 控制标志）声明六个流水寄存器。注释强调「+1 拍延迟在数据与控制路径上一致」。

**寄存器的写入与读出在同一循环里。** 这是理解「+1 拍」的关键 Chisel 细节：在 74–84 行的循环中，`pipe_a(...) := sarray.io.out_a(...)` 是「下一拍写入」，而 `pe_io(...).in_a := pipe_a(...)` 读的是「当前拍（旧）值」——寄存器的读总是拿上一拍写入的值，所以阵列输出比 PE 输入**早 1 拍**被采样：

[src/main/scala/alu/mma/mma.scala:74-84](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L74-L84) — 先写 `pipe_a/pipe_b/pipe_ctrl`（下拍生效），再用它们驱动 PE 的 `in_a/in_b/ctrl`（读当前值），这正是 +1 拍延迟的来源。

**控制/偏置路径的后移。** `pipe_accum` 把 feeder 的 accum 输出延 1 拍再送 collector；`pipe_dat_clct`/`pipe_use_accum`/`pipe_clct` 把 ControlUnit 的三个标志延 1 拍：

[src/main/scala/alu/mma/mma.scala:60-66](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L60-L66) — `pipe_accum(i)` 接 `dfeed.io.reg_accum_out(i)`，再喂 `dclct.io.accum_in`；`pipe_dat_clct/pipe_use_accum` 接 collector 的 `dat_clct/use_accum`；`pipe_clct` 接 `io.clct`。

[src/main/scala/alu/mma/mma.scala:86-88](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L86-88) — ControlUnit 的 `cbus_dat_clct/cbus_use_accum/clct` 经 `RegNext`/`RegInit` 后移 1 拍。

> **旁注：另一处时序修复**。`DataFeeder` 内部还有一处独立的时序修复（与 MMALU 这处不同）：偏置原本用一根 `Pipe(Vec(n,...), 2n-1)` 长延迟线，扇出极大（n=32 时达 1025），250 MHz 下违例。改成「每 lane 一根独立 Pipe」让 Vivado 能把每路摆在消费者附近，网络延迟降约 6 倍。见 [src/main/scala/alu/mma/sa/dataFeeder.scala:24-28](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/dataFeeder.scala#L24-L28)。两处修复目标频率不同（200/250 MHz）、对象不同，但思路一致：切断长路径/大扇出。

#### 4.2.4 代码实践

**实践目标**：理解「读寄存器=旧值」如何造成 +1 拍，并用延迟公式验证。

**操作步骤**：

1. 在 `mma.scala` 的 74–84 行循环里，找到 `pipe_a(n*i+j) := sarray.io.out_a(n*i+j)`（写）和 `pe_io(n*i+j).in_a := pipe_a(n*i+j)`（读）这两行。
2. 回答：如果某个数据在第 T 拍出现在 `sarray.io.out_a`，它在第几拍出现在 PE 的 `in_a`？
3. 用架构文档的经典公式 3n−2，加上这 1 拍流水寄存器，写出插入后的总延迟。

**需要观察的现象**：写入与读出虽然在同一循环、看似「同时」，但因为 `pipe_a` 是寄存器，读出永远比写入晚 1 拍。

**预期结果**：第 T 拍的数据，第 T+1 拍才到 PE；总延迟 = (3n−2) + 1 = **3n−1**，与头部注释第 20–21 行一致。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `pipe_a`/`pipe_b`/`pipe_ctrl` 全部删掉（直接 `pe_io.in_a := sarray.io.out_a`），延迟会变成多少？时序会怎样？

> **参考答案**：延迟回到 3n−2（少 1 拍），但关键路径重新变成 13 级逻辑，200 MHz 下 WNS 回到 −0.151 ns，时序违例、上板跑不到目标频率。

**练习 2**：为什么 `pipe_accum`（n 个）也要跟着加 1 拍，而不是保持原样？

> **参考答案**：因为偏置要和「PE 输出被收集」的那一拍对齐。数据路径整体后移了 1 拍，偏置路径若不随后移，就会差一拍加错位置。注释第 51 行明确说「+1 拍延迟在数据与控制路径上一致」。

---

### 4.3 ctrl.keep 流式归约：一条 keep=true 实现 M×K 归约

#### 4.3.1 概念说明

前面所有讨论都假设「一次喂 K 拍、算一个 K×K 结果」。但真实的 GEMM（通用矩阵乘）里，归约维 M 往往远大于 K——比如 K=32 但 M=4096。难道要发 4096/32 = 128 条独立的 `mma` 指令、每条算一个 32×32 子块再软件累加吗？

不需要。`MMALU` 的 K×K 阵列本质上是一个 **M×K 归约引擎**：只要把 `ctrl.keep` 持续拉高 M 拍（M 任意），PE 的 `res` 就一直累加，阵列就在算一个 M×K 的归约。**唯一把归约长度钉死在 K 的，是旧协议里那条手动插入的 `keep=false` 复位脉冲**——删掉它，就是 M×K 流式归约。

这个能力不是新加的硬件，而是 PE（永久累加器）、ControlUnit（OR 门汇总）、DataCollector（模 K 计数器）三者的自然结果。本节就是把这个「自然结果」讲清楚。

#### 4.3.2 核心流程

**M×K 归约的数学定义。** 对 M 拍连续喂入（`keep=true` 全程拉高），PE(i,j) 的累加器最终值为：

\[
\text{PE}(i,j).\text{res} \;=\; \sum_{m=0}^{M-1} A_{m,\,i}\cdot B_{m,\,j}
\]

即阵列算出 \(C = B^{\mathsf T}A\)，其中 \(A, B \in \mathbb{Z}^{M\times K}\)。令 M=K 就回到 K×K 功能模型。

**阶梯式输出帧（staircased frames）。** 关键在于：收集器不是只在最后吐一次结果，而是在喂入过程中**每 K 拍吐出一个 K×K 的累积部分和帧**。定义第 f 帧（\(f = 1, 2, \ldots, \lceil M/K\rceil\)）为累积到 f·K 行的部分和：

\[
\text{frame}_f[i, j] \;=\; \sum_{m=0}^{f \cdot K - 1} A_{m,\,j}\cdot B_{m,\,i}
\]

第 1 帧是前 K 行之和，第 2 帧是前 2K 行之和（在第 1 帧基础上再加 K 行）……最后一帧才是完整 M 行之和。中间帧是「免费的」运行中部分和，可读可不读。

**为什么是每 K 拍一帧？** 三个机制叠加：

1. **PE 持续累加**（`keep=true`）：`res` 不断增长，每拍都持有「到目前为止」的累积和。
2. **ControlUnit 的 OR 门**：`dat_clct` = 所有在飞 `keep` 的 OR。`keep` 全程拉高 ⇒ `dat_clct` 全程为真 ⇒ 收集器一直被使能。
3. **DataCollector 的模 K 计数器**：`cnt` 在 `dat_clct` 为真时每拍 +1、模 K 回绕，每回绕一轮就完整输出一个 K×K 帧。

**两种协议的差别只有一拍 `keep=false`。** 这是最美的结论：

| 协议 | 用法 | 结果 |
|:---|:---|:---|
| K-burst（旧） | 每 K 拍插一拍 `keep=false` | 每 K 拍得到一个**独立** K×K 结果（复位清零） |
| M×K 流式（本节） | `keep=true` 连续拉高 M 拍 | 每 K 拍得到一个**累积** K×K 部分和，最后一帧是完整 M 和 |

> 流式归约只需一条 `keep=true`（持续 M 拍），无需任何清零指令——这正是本讲标题「一条 keep=true 实现 M×K 归约」的含义。

#### 4.3.3 源码精读

**PE：keep 的累加/覆盖语义。** 这是流式归约的物理根基——只要 keep 不掉，就一直加：

[src/main/scala/alu/pe/procElem.scala:12-18](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/pe/procElem.scala#L12-L18) — `MMPE` 在 `keep` 为真时 `res := res + in_a*in_b`，否则 `res := in_a*in_b`（覆盖/复位）。`keep=true` 持续 M 拍 ⇒ `res` 累加 M 项。

**ControlUnit：OR 门产生持续的 dat_clct。** `keep` 全程拉高时，深度 2n−1 移位寄存器里每一级都是真，OR 之后 `dat_clct` 全程为真：

[src/main/scala/alu/mma/cu/controlUnit.scala:20-33](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/cu/controlUnit.scala#L20-L33) — `reg` 是深度 2n−1 的一维移位寄存器；`or_g` 把 `io.cbus_in.keep` 与 `reg(0..2n-3).keep` 共 2n−1 路 OR 得到 `cbus_dat_clct`。`keep` 持续拉高 ⇒ 移位寄存器逐级填满真 ⇒ `cbus_dat_clct` 持续为真。

**DataCollector：模 K 计数器每 K 拍回绕一次。** 这就是「每 K 拍一帧」的来源：

[src/main/scala/alu/mma/sa/dataCollector.scala:21-42](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/sa/dataCollector.scala#L21-L42) — `Counter(0 until n, ...)` 在 `dat_clct` 为真时每拍自增、模 n 回绕；`col = (cnt - i) % n` 配合 chainsaw buffer 把 n×n 个 PE 输出按列对齐回收。计数器每回绕一轮 = 输出一个完整 K×K 帧。

**架构文档对 M×K 流式归约的完整论述。** 文档专门有一节讲这个能力，含公式、协议对比表和波形：

[docs/implementations/SystolicArray.md:103-123](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/SystolicArray.md#L103-L123) — 「M×K Streaming Reduction」章节，明确指出「PE/DataFeeder/ControlUnit 里没有任何东西强制 K 拍粒度，唯一限制 K 粒度的是旧协议手动插的那拍 `keep=false`」，并给出广义公式 \(C = B^{\mathsf T}A,\ A,B\in\mathbb{Z}^{M\times K}\)。

[docs/implementations/SystolicArray.md:125-147](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/SystolicArray.md#L125-L147) — 「Staircased output frames」给出帧 f 的累积部分和公式与时间窗口定义。

**端到端验证测试。** `MMALUStreamReduceSpec` 用四个用例验证上述机制，是本节结论的可执行「裁判」：

[src/test/scala/alu/mma/MMALUStreamReduceSpec.scala:100-140](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/mma/MMALUStreamReduceSpec.scala#L100-L140) — `runStream`：喂入循环里 M 拍全程 `keep=true`、`busy=true`（见 127–129 行），之后排空；逐拍捕获 `io.out`。这就是「持续拉高 keep」的合约。

[src/test/scala/alu/mma/MMALUStreamReduceSpec.scala:150-175](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/mma/MMALUStreamReduceSpec.scala#L150-L175) — `checkFrame`：第 f 帧落在 i_tick 区间 `[(f+1)K−1, (f+2)K−2]`（`base = (f+1)*K - 1`，共 K 拍），对照 Scala 参考矩阵逐 lane 比对。注意这个 +1 偏移正是 4.2 节那拍 SA→PE 流水寄存器造成的。

测试用例覆盖：M=2K（帧 1 的 K 部分和 + 帧 2 的 2K 全和）、M=3K（三帧）、M=5K（五帧，模 K 多次回绕），以及最关键的**对照实验**——在 M=2K 的中间插一拍 `keep=false`，验证帧 2 塌缩成「第二个 K-only 和」而非「2K 全和」，从而**证明 `keep=false` 是 K 粒度的唯一成因**：

[src/test/scala/alu/mma/MMALUStreamReduceSpec.scala:290-342](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/mma/MMALUStreamReduceSpec.scala#L290-L342) — Test 4：注入一拍 `keep=false`，断言帧 2 不等于 2K 全和（336–340 行的对比断言），证明复位脉冲是 K 粒度的唯一来源。

#### 4.3.4 代码实践

**实践目标**：运行流式归约测试，亲眼看「持续 keep=true → 每 K 拍一个累积部分和帧」。

**操作步骤**：

1. 在容器里运行单测（`tool/test-specific-spec.sh` 是 `sbt "testOnly ..."` 的薄封装）：

   ```bash
   make container
   # 进入容器后：
   bash tool/test-specific-spec.sh alu.mma.MMALUStreamReduceSpec
   ```

   或直接在宿主机（已配好 sbt 环境）：

   ```bash
   sbt "testOnly alu.mma.MMALUStreamReduceSpec"
   ```

2. 打开 `src/test/scala/alu/mma/MMALUStreamReduceSpec.scala`，对照 4.3.2 的帧公式阅读 Test 1（M=2K）：
   - `expK = refSum(matA, matB, K, K)` 是第 1 帧期望值（前 K 行之和）。
   - `exp2K = refSum(matA, matB, K, 2*K)` 是第 2 帧期望值（前 2K 行之和）。
   - `checkFrame(capture, K, f=1, ...)` 与 `checkFrame(..., f=2, ...)` 分别核对两帧。
3. 如果想让失败信息可见，可在 `runStream` 后手动打印 `capture`（测试在失败时已会打印全部 capture，见 202–206 行）。

**需要观察的现象**：

- 测试通过，说明一次 M=2K 的连续喂入，**确实在同一批数据里**既输出了 K 部分和帧（帧 1），又输出了 2K 全和帧（帧 2）。
- Test 4 用同样的数据但插了一拍 `keep=false`，帧 2 变成「第二个 K-only 和」——**同一硬件、同一数据，仅凭一拍 keep 的差别就产生不同结果**。

**预期结果**：四个用例全部 PASS。然后用一句话回答本节标题的问题。

**那句话答案**：流式归约只需一条 `keep=true` 持续 M 拍，是因为 PE 的永久累加器在 `keep=true` 下永不复位、持续累加 M 项，而收集器的模 K 计数器在 `dat_clct`（= 全体在飞 keep 的 OR）持续为真时每 K 拍自然回绕一次、吐出一个累积帧——没有任何额外指令或清零动作参与。

> **诚实提示（软件派发尚未就绪）**：流式归约是阵列的**架构能力**，已由 `MMALUStreamReduceSpec` 端到端验证。但当前 ISA/后端路径还无法直接派发它——`MMA_LAST` 译码会把最后一拍 `keep` 置假（导致末拍覆盖、丢失 M 和），且 MMA 的寄存器堆地址尚未从 `rs1/rs2/rd` 驱动。文档 [docs/implementations/SystolicArray.md:221-242](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/SystolicArray.md#L221-L242) 的「Gaps for software dispatch」一节列出了这两个待修复点。本讲陈述的是已验证的硬件能力，而非已接通的 ISA 路径。

#### 4.3.5 小练习与答案

**练习 1**：M=5K 时，阵列在喂入过程中会吐出几个 K×K 帧？每个帧分别累积了多少行？

> **参考答案**：\(\lceil M/K\rceil = 5\) 个帧。第 f 帧累积 f·K 行：帧 1 = K 行、帧 2 = 2K 行、帧 3 = 3K 行、帧 4 = 4K 行、帧 5 = 5K 行（完整 M 和）。对应 Test 3（M=5K）。

**练习 2**：如果在 M=2K 的喂入中，第 K−1 拍（即第 K 拍数据的前一拍）插入一拍 `keep=false`，帧 2 会变成什么？

> **参考答案**：帧 2 不再是 2K 全和，而是「第二个 K 行的独立 K×K 和」。因为那拍 `keep=false` 把 PE 累加器复位了，第二个 K 窗口从零开始累加。这正是 Test 4 验证的现象，也证明 `keep=false` 复位脉冲是 K 粒度的唯一成因。

**练习 3**：为什么说「每 K 拍输出一个累积帧」是免费的，不需要额外硬件？

> **参考答案**：因为 PE 的 `res` 本来就每拍持有「至今为止」的累积和（keep=true 持续累加），DataCollector 的模 K 计数器本来就在 `dat_clct` 为真时每 K 拍回绕一次。两者本来就在工作，流式归约只是「让 keep 一直为真」，不新增任何逻辑。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「读图 + 推时序」任务：

**任务**：取 n=4（与测试一致），回答下列问题，全部基于源码与文档，不要凭空猜。

1. **画连线图**：参照 4.1，画出 `MMALU` 五大组件的连线图，并在图上标出 4.2 讲的六个流水寄存器（`pipe_a/pipe_b/pipe_ctrl/pipe_accum/pipe_dat_clct/pipe_use_accum`，外加 `pipe_clct`）插在什么位置。
2. **算延迟**：用公式算出 n=4 时插入流水寄存器后的总延迟是多少拍（应为 3·4−1 = 11）。
3. **推帧时序**：对照 `checkFrame` 的 `base = (f+1)*K - 1`，写出 n=4、M=8（即 2K）时第 1 帧和第 2 帧各自落在哪几个 i_tick（应分别为 7..10 与 11..14，与文档「Timing diagram — streaming case」一致）。
4. **运行验证**：执行 `sbt "testOnly alu.mma.MMALUStreamReduceSpec"`，确认 Test 1（M=2K）通过，印证你推的第 1、2 帧时序与累积和正确。

**验收标准**：第 2、3 问的数字与公式/源码完全对得上；第 4 问测试 PASS。若无法本地运行，明确标注「待本地验证」并至少完成 1–3 问的纸面推导。

---

## 6. 本讲小结

- `MMALU` 在顶层把 `DataFeeder` + `SystolicArray2D` + `n×n` 个 PE + `ControlUnit` + `DataCollector` 五大组件连成一条「输入扭曲 → 脉动传播 → 乘累加 → 列回收」的完整流水线，`io.out` 来自 collector，`io.clct` 标志整批完成。
- 为修 200 MHz 关键路径（reg_h → PE MAC，13 级逻辑，WNS = −0.151 ns），在 SA→PE 与 CU→Collector 之间插入六个流水寄存器，把长路径切成两段 6–7 级，代价是总延迟从 **3n−2 变为 3n−1**。
- 这六个寄存器（`pipe_a/pipe_b/pipe_ctrl/pipe_accum/pipe_dat_clct/pipe_use_accum/pipe_clct`）让数据与控制路径整体同步后移 1 拍，保证偏置、收集使能与数据对齐。
- K×K 阵列本质是 **M×K 归约引擎**：`ctrl.keep` 持续拉高 M 拍，PE 永久累加器持续累加，每 K 拍自然输出一个**累积部分和帧**。
- K-burst（每 K 拍插 `keep=false`）与 M×K 流式（`keep=true` 连续 M 拍）两种协议的差别**只有一拍 keep**——`keep=false` 复位脉冲是 K 粒度的唯一成因。
- 流式归约是 PE 永久累加器 + ControlUnit OR 门 + DataCollector 模 K 计数器的自然结果，无需额外硬件；但 ISA/后端派发路径尚有两个待修复点（`MMA_LAST` 的 keep、MMA 寄存器堆地址）才能让前端真正发出流式归约。

---

## 7. 下一步学习建议

本讲完成了 MMALU 的全部内部机制（U4 单元收口）。接下来推荐：

- **横向进入 VALU（U5）**：学完矩阵引擎后，去学 K 通道的向量 ALU（`alu/vec/vec.scala`），它是另一个核心计算单元，负责算术/逻辑/激活/浮点/归约，与 MMALU 在后端并行。
- **纵向进入后端集成（U6）**：看 `backend/SimpleBackend.scala` 如何把译码器、多宽度寄存器堆、MMALU、VALU 连成一个完整后端，以及 MMALU 的 3n−1 拍长流水如何与 VALU 的 1～2 拍重叠执行。
- **若对流式归约的软件派发感兴趣**：阅读 `docs/implementations/SystolicArray.md` 的「Gaps for software dispatch」一节，思考如何修 `MMA_LAST` 译码与寄存器堆地址，让前端能真正发出一条流式归约指令。
- **继续阅读源码**：`src/main/scala/alu/mma/mma.scala` 全文仅 89 行，是理解「组件如何装配」的最佳范本；建议对照本讲再通读一遍，确认每个连线都能对上。
