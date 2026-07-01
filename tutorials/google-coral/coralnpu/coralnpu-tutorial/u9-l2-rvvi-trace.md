# RVVI 指令追踪与仿真观测

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 **RVVI（RISC-V Instruction Trace Interface）** 是什么、为什么 CoralNPU 需要一个专门的指令追踪接口。
- 读懂 `hdl/chisel/src/coralnpu/RvviTrace.scala`，列出它对外暴露的每一类追踪字段（PC、指令、order、trap、GPR/FPR/VPR 写、CSR 写）。
- 理解这些字段的数据来源：退休缓冲（ROB）的 debug 端口与 CSR 模块的 trace 端口。
- 说明 RVVI 信号在仿真中如何被 UVM monitor 采样，并如何与 ISA 参考模型（Spike）做协同验证（co-verification）。
- 自己设计一个最小的「RTL 行为 vs 参考模型」一致性检查点。

本讲属于「调试、可观测性与总线完整性」单元，依赖 [u7-l1 RVV 后端总览与 Chisel 桥接](u7-l1-rvv-backend-overview.md)——你需要知道 CoralNPU 同时拥有标量寄存器（GPR）、浮点寄存器（FPR）和向量寄存器（VPR）三类寄存器堆。

## 2. 前置知识

### 2.1 什么叫「可观测性」

一段 RTL 代码在仿真器里跑得飞快，但你很难直接看到「它究竟执行了哪条指令、写入了哪个寄存器、值是多少」。可观测性（observability）就是给 RTL 打一扇窗：把内部状态以标准化的方式引到模块边界，让仿真环境（甚至真实调试器）能像看日志一样看到每条指令的执行结果。

### 2.2 为什么要按「退休」而非「派发」来追踪

回顾 [u4-l4](u4-l4-dispatch-scoreboard-retire.md)：CoralNPU 标量核是「按序派发、乱序完成、按序退休」。一条指令**派发**之后可能因为 cache miss、除法延迟等原因很久才完成；但它只有在**退休（retire）**那一刻，才会真正把结果写进架构寄存器堆、对外可见。

所以追踪必须挂在「退休」这一级——只有退休的指令才算数。被冲刷掉的推测指令、半途而废的执行都不应该出现在追踪里。RVVI 就是挂在 CoralNPU 退休缓冲（Retirement Buffer，ROB）输出上的。

### 2.3 什么是协同验证（co-verification / co-simulation）

即使 RTL 跑出了「正确」的波形，你也无法仅凭波形判断「这条 `add` 算对了没有」——除非你有一个**参照物**。协同验证的做法是：用一个可信的 ISA 仿真器（参考模型，CoralNPU 用的是 **Spike**，即 `riscv-isa-sim`）跑同一份程序，把它提交的每一条指令（PC、写哪个寄存器、写什么值）记录成日志，再让 RTL 仿真环境拿着 RVVI 提供的真实退休信息逐条比对。一旦 RTL 与参考模型不一致，就立刻报错。这是处理器验证的核心手段之一。

### 2.4 几个关键术语

| 术语 | 含义 |
|------|------|
| RVVI | RISC-V 官方定义的指令追踪接口，一组标准信号，描述「哪条指令在哪一拍退休、改了哪些架构状态」 |
| GPR / FPR / VPR | 通用（整数）/ 浮点 / 向量 寄存器堆 |
| ROB | Retirement Buffer，退休缓冲，按序提交指令 |
| `order` | RVVI 里一个单调递增的全局退休序号，保证指令可被外部排序 |
| one-hot 位掩码 | 用 N 位中恰好一位置 1 来表示「选中了第几个」的编码方式 |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `hdl/chisel/src/coralnpu/RvviTrace.scala` | **本讲主角**。把 ROB 与 CSR 的内部状态翻译成标准 RVVI 信号，并生成一个 SystemVerilog BlackBox 包装 |
| `hdl/chisel/src/coralnpu/RetirementBuffer.scala` | 退休缓冲。其 `debug` 端口向 RVVI 提供每条退休指令的 PC/指令/写回数据/trap |
| `hdl/chisel/src/coralnpu/Interfaces.scala` | 定义 `RetirementBufferDebugIO` 与 `CsrTraceIO` 两个数据结构 |
| `hdl/chisel/src/coralnpu/scalar/SCore.scala` | 标量核顶层。在这里把 `RvviTrace` 实例化并接上 ROB/CSR |
| `hdl/chisel/src/coralnpu/scalar/Csr.scala` | CSR 模块。其 `io.trace` 端口向 RVVI 提供 CSR 写信息 |
| `hdl/chisel/src/coralnpu/Parameters.scala` | 提供 `retirementBufferSize`、各类寄存器堆基址等参数 |
| `tests/uvm/tb/coralnpu_tb_top.sv` | UVM 顶层 testbench。抓取 RTL 内部的 `rvviTraceBlackBox.rvvi` 句柄交给验证环境 |
| `tests/uvm/common/rvvi_agent/coralnpu_rvvi_monitor.sv` | UVM monitor。每拍采样 RVVI、解析指令、驱动覆盖率 |
| `tests/uvm/common/cosim/coralnpu_cosim_checker_pkg.sv` + `spike_cosim_checker.sv` | 协同验证检查器。用 Spike 参考模型逐条比对 RTL 退休结果 |

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：(1) RVVI 是什么、CoralNPU 怎么实现它；(2) RVVI 对外暴露的字段全景；(3) 字段的数据来源（ROB debug 端口）；(4) 字段的数据来源（CSR trace 端口）；(5) RVVI 在协同验证中的消费。

### 4.1 RVVI 接口与 CoralNPU 的实现策略

#### 4.1.1 概念说明

RVVI 是 RISC-V 国际标准组织定义的一套**观察接口**。它的核心思想是：处理器每退休一条指令，就在一个标准化的信号组里告诉你「序号、PC、指令编码、是否 trap、写了哪些寄存器、写了什么值、CSR 有没有变」。这套接口是只读的（对 RTL 而言是输出），不改变处理器行为，因此也叫**非侵入式（non-intrusive）**观测。

CoralNPU 没有手写 SystemVerilog 去拼这些信号，而是采用「Chisel 聚合 + SV BlackBox 出接口」的两段式策略：

1. 用 Chisel 写一个普通 `Module`——`RvviTrace`，它接收 Chisel 侧的 `RetirementBufferDebugIO` 和 `CsrTraceIO`，做必要的译码与重组，再把结果喂给一个 BlackBox。
2. 这个 BlackBox（`RvviTraceBlackBox`）用 `HasBlackBoxInline` 在编译期**自动生成**一段 SystemVerilog 包装代码，内部实例化标准的 `rvviTrace` 接口。这样 RTL 边界对外暴露的就是标准 RVVI 接口，仿真环境可以直接用 `virtual rvviTrace #(...)` 句柄来抓。

这种做法的好处是：RVVI 字段数量巨大（后面会看到），但绝大部分只是「按通道复制 + 位宽拼接」，用 Chisel 的 `Vec`/`asUInt` 描述比手写 SV 干净得多；而真正要符合标准的接口声明留给 SV。

#### 4.1.2 核心流程

```
           (每拍) ROB 退休槽 0..7 ──┐
                                  ├──► RvviTrace (Chisel 模块)
           CSR 模块 io.trace ──────┘            │
                                                 │ 译码/重组：
                                                 │  - order 计数
                                                 │  - GPR/FPR/VPR 写译码
                                                 │  - CSR 写展开
                                                 ▼
                                     RvviTraceBlackBox (BlackBox)
                                                 │
                                    自动生成 RvviTraceBlackBox.sv
                                    + 外部资源 rvviTrace.sv
                                                 │
                                                 ▼
                                     标准 rvviTrace 接口 (到 RTL 边界)
                                                 │
                                     仿真环境用 virtual rvviTrace 抓取
```

`RvviTrace` 这个 Chisel 模块本身**只在 `p.useRetirementBuffer` 为真时才被实例化**（即验证模式开启时），这是为了在裁剪配置下省掉这部分逻辑。

#### 4.1.3 源码精读

先看模块的整体骨架与 IO 定义：

`RvviTrace` 模块的入口：输入只有两路——来自 ROB 的 `rb` 与来自 CSR 的 `csr`。
[RvviTrace.scala:142-146](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RvviTrace.scala#L142-L146) —— 定义 `RvviTrace` 的 IO：`rb` 是退休缓冲 debug 端口，`csr` 是 CSR 追踪端口，两者共同决定本拍 RVVI 输出什么。

实例化并接线 BlackBox（接时钟与各类宽位字段）：
[RvviTrace.scala:159-173](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RvviTrace.scala#L159-L173) —— 实例化 `RvviTraceBlackBox`，把时钟接上，并把每个退休通道的 `x/f/v_wdata`、`x/f/v_wb`、`csr`、`csr_wb` 等「打包后的宽位信号」灌进 BlackBox。

BlackBox 本体的声明与「内联生成 + 外部资源」两条出文件途径：
[RvviTrace.scala:119-140](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RvviTrace.scala#L119-L140) —— `RvviTraceBlackBox` 继承 `BlackBox with HasBlackBoxInline with HasBlackBoxResource`：`addResource` 引入标准的 `rvviTrace.sv`（外部依赖，构建期获取），`setInline` 用 `GenerateRvviTraceSource(p)` 现场生成 `RvviTraceBlackBox.sv` 包装层。

`SCore` 顶层把 `RvviTrace` 接到 ROB 与 CSR：
[SCore.scala:561-566](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L561-L566) —— `debug.rb := robDebug` 把 ROB debug 端口交给上层 debug 结构；随后 `if (p.useRetirementBuffer)` 才实例化 `RvviTrace`，并把 `rvvi.io.rb := robDebug`、`rvvi.io.csr := csr.io.trace`。这说明 RVVI 是验证模式专属的可观测通路。

`useRetirementBuffer` 的定义（验证模式开关）：
[Parameters.scala:99](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L99) —— `useRetirementBuffer` 直接等于 `enableVerification`。注释（同文件 84-87 行）说明：生成 riscv-dv 协同仿真指令轨迹需要完整 ROB，但完整 ROB 模式存在 hang bug，故把 `enableVerification` 与 `exposeDebugPorts` 解耦。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认 RVVI 的「生成 + 接线」是验证模式专属。
2. **步骤**：
   - 打开 [SCore.scala:562-566](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L562-L566)，确认 `RvviTrace` 在 `if (p.useRetirementBuffer)` 块内。
   - 打开 [Parameters.scala:92-99](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L92-L99)，读 `shouldExposeDebugPorts` 与 `useRetirementBuffer` 两个定义，记录它们各自依赖哪个开关。
3. **观察现象**：在 SoC 生产配置（`enableVerification=false`）下，`debug.rb` 端口（`Option.when(p.shouldExposeDebugPorts)`）也会消失，`RvviTrace` 根本不会被综合出来。
4. **预期结果**：RVVI 是一条「验证专享」通路；流片用的裁剪配置里没有它。
5. 运行结果标注：**待本地验证**（取决于具体 SoC 配置）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `RvviTrace` 用「Chisel 模块 + BlackBox」两段式，而不是直接在 RTL 里手写 RVVI 信号？

**参考答案**：RVVI 字段数量庞大（8 个退休通道 × 数十种信号 × 4096 路 CSR 解码），用 Chisel 的 `Vec`/`for` 循环描述可以一次写好、随参数扩展；而真正要符合标准的 SV 接口声明（`rvviTrace.sv`）由 BlackBox 的 `addResource` 引入。这样「聚合逻辑用 Chisel、接口标准用 SV」各取所长，且 BlackBox 包装层还能用 `GenerateRvviTraceSource` 按参数自动生成，避免手写易错。

**练习 2**：`RvviTrace` 与 ROB 的 `debug` 端口共享同一个 `robDebug` 信号（`debug.rb := robDebug` 与 `rvvi.io.rb := robDebug`），这说明它们是什么关系？

**参考答案**：二者是「同一份退休信息、两种用途」的并行消费者。`debug.rb` 供 Chisel 侧的 `DebugIO`（开发期波形观测）使用，`rvvi.io.rb` 供标准 RVVI 接口（验证期协同比对）使用。它们都来自同一个 `robDebug`，保证看到的是同一份退休事实。

### 4.2 RVVI 对外暴露的字段全景

#### 4.2.1 概念说明

RVVI 的核心信息粒度是「**每个退休通道、每拍**」。CoralNPU 的 `retirementBufferSize`（退休缓冲深度）是 8，所以每拍最多有 8 条指令同时退休，对应 RVVI 的 8 个通道。每个通道携带一组字段。

需要特别说明「通道」与 `order` 的关系：虽然一拍里可能有多条指令退休，但它们有严格的程序顺序——通道 0 是最早的、通道 7 是最晚的。RVVI 用一个全局单调递增的 `order` 给每条退休指令编号，保证外部能把它排成一条无歧义的指令流。

#### 4.2.2 字段表

下表列出每个退休通道 `i`（`0..7`）暴露的字段、含义与 CoralNPU 中的来源。`x_wb`/`f_wb`/`v_wb` 是 32 位的 **one-hot 位掩码**：哪一位置 1，表示该类寄存器堆的第几个寄存器被写；对应的 `x_wdata`/`f_wdata`/`v_wdata` 是把 32 个寄存器的写值「拼成一个大数」后按寄存器号切片读取。

| RVVI 字段 | 位宽（单通道） | 含义 | 来源 |
|-----------|------------|------|------|
| `valid` | 1 | 本通道本拍是否真的退休了一条指令 | ROB debug `inst(i).valid` |
| `order` | 64 | 全局退休序号（单调递增） | 内部 `count` 计数器 + 通道偏移 |
| `insn` | 32 | 退休指令的 32 位编码 | ROB debug `inst(i).bits.inst` |
| `pc_rdata` | 32 | 该指令的 PC | ROB debug `inst(i).bits.pc` |
| `trap` | 1 | 该指令是否触发了异常/trap | ROB debug `inst(i).bits.trap` |
| `debug_mode` | 1 | 是否处于调试模式 | **恒为 false（未实现，TODO）** |
| `x_wb` / `x_wdata` | 32 / 1024 | GPR 写掩码 / 32×32 拼接写值 | ROB 写回 idx/data |
| `f_wb` / `f_wdata` | 32 / 1024 | FPR 写掩码 / 32×32 拼接写值 | ROB 写回 idx/data（基址 32） |
| `v_wb` / `v_wdata` | 32 / 4096 | VPR 写掩码 / 32×128 拼接写值 | ROB 向量写 `vecWrites`（基址 64） |
| `csr_wb` / `csr` | 4096 / 4096×32 | CSR 写掩码（哪号 CSR 变了）/ 各 CSR 的值 | CSR 模块 `io.trace` |

关于位宽：`v_wdata` 单通道是 `32 × 128 = 4096` 位（因为 VLEN=128），所以 8 个通道拼接后是 32768 位——这正是「字段数量巨大」、适合用 Chisel 自动生成的原因之一。

#### 4.2.3 核心流程：`order` 计数器

`order` 是 RVVI 区分指令先后、把多通道退休还原成单条流的关键。CoralNPU 用一个 64 位寄存器 `count` 累计「历史上已退休的指令数」，本拍第 `i` 通道退休的指令，其 `order = count + i`（因为同拍内通道 `i` 比通道 0 晚 `i` 条）。

```text
count（历史已退休总数）
   │
   ├─ 本拍 valid 通道数 = PopCount(io.rb.inst.map(_.valid))
   │
   ▼
count_next = count + 本拍退休数      ← 下一拍更新
   │
   └─ 通道 i 的 order = count + i    ← 用 MuxOR(valid, count + i.U) 输出
```

这里用 `MuxOR(valid, x)` 是 CoralNPU 的惯用写法：当 `valid` 为假时把输出清零，避免无效通道泄漏垃圾数据。

#### 4.2.4 源码精读

`order` 计数器与单通道字段提取：
[RvviTrace.scala:156-194](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RvviTrace.scala#L156-L194) —— `count` 寄存器每拍加上 `PopCount(io.rb.inst.map(_.valid))`（本拍实际退休的指令数）；随后对每个通道 `i`，用 `MuxOR(valid, ...)` 输出 `order = count + i.U`、`insn`、`pc_rdata`、`trap`。`debug_mode_i` 被注释标注为「TODO: 一般不追踪」并硬接 `false.B`。

`retirementBufferSize` 等关键参数：
[Parameters.scala:117-121](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L117-L121) —— `retirementBufferSize = 8`，即 RVVI 有 8 个退休通道；`retirementBufferIdxWidth` 由「标量+浮点+向量+2」的寄存器总数取对数得到，说明 ROB 用一个**统一的索引空间**区分写的是 GPR/FPR/VPR。

BlackBox IO 的位宽定义（印证上表）：
[RvviTrace.scala:121-137](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RvviTrace.scala#L121-L137) —— BlackBox 的 `io` 声明：`x_wdata_i` 为 `Vec(8, UInt(1024.W))`（32 个 32 位 GPR 拼接）、`v_wdata_i` 为 `Vec(8, UInt(4096.W))`（32 个 128 位 VPR 拼接）、`csr_wb_i` 为 `Vec(8, UInt(4096.W))`（4096 路 CSR 掩码）。

`GenerateRvviTraceSource` 自动生成 SV 包装：它把上述 8 通道 × 各字段展开成 SV `input` 端口，再 `assign` 到标准 `rvviTrace` 接口上。
[RvviTrace.scala:20-84](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RvviTrace.scala#L20-L84) —— 用 Scala 字符串拼接生成 `module RvviTraceBlackBox(...)` 的端口表，并实例化 `rvviTrace #(.ILEN(32),.XLEN(32),.FLEN(32),.VLEN(128),.NHART(1),.RETIRE(8)) rvvi();`，再用 `assign rvvi.xxx[0][i] = ...` 把每个通道接到标准接口。注意 `NHART=1`、`RETIRE=8` 与上文参数一致。

#### 4.2.5 代码实践（源码阅读型）

1. **目标**：亲手核验 RVVI 字段表与位宽。
2. **步骤**：
   - 打开 [RvviTrace.scala:121-137](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RvviTrace.scala#L121-L137)，对每个 `Input(...)` 字段记录位宽，填进上面的字段表。
   - 打开 [RvviTrace.scala:75-84](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RvviTrace.scala#L75-L84)，确认生成的 `rvviTrace` 接口参数 `ILEN/XLEN/FLEN/VLEN/NHART/RETIRE` 取值。
3. **观察现象**：`v_wdata_i` 单通道 4096 位 = 32 × 128，正好是「32 个向量寄存器 × VLEN=128」。
4. **预期结果**：字段表与源码完全吻合；`RETIRE=8` 与 `retirementBufferSize=8` 一致。
5. 运行结果标注：**待本地验证**。

#### 4.2.6 小练习与答案

**练习 1**：为什么 `x_wb` 用 32 位 one-hot 掩码，而不是直接给一个 5 位的寄存器号？

**参考答案**：因为理论上一条指令在极端情况下可能写多个寄存器（向量场景尤其如此），用位掩码能自然表达「这一拍写了哪些寄存器」。同时，下游检查器可以用 `$clog2(x_wb)` 反推第一个被写的寄存器号，也可以用位运算快速判断「RTL 写了某寄存器、而参考模型没写」这种对称性错误（见 4.5 节）。

**练习 2**：`order` 为什么是 64 位？

**参考答案**：`order` 是全局退休计数，长程序会退休海量指令，32 位（约 42 亿）可能在大型回归测试中溢出。64 位提供几乎无尽的序号空间，保证协同验证里「RTL 序号」与「Spike commit 日志序号」一一对应时不至于回绕。

### 4.3 字段数据来源之一：ROB 的 debug 端口

#### 4.3.1 概念说明

`RvviTrace` 的绝大多数字段（valid/order/insn/pc/trap/GPR/FPR/VPR 写）全部来自一个数据结构：`RetirementBufferDebugIO`。它是 ROB（`RetirementBuffer`）在验证模式下额外暴露的「退休快照」端口——把每拍真正退休的指令（注意是 `deqReady` 范围内的、按序出队的）的完整信息打包送出。

这里要特别注意「退休才输出」的语义：ROB 内部 `instBuffer`/`resultBuffer` 可能同时装着多条已完成但还没轮到退休的指令；只有队首连续 ready 的前缀（`deqReady`）才会在这一拍退休并被送进 debug 端口。被 trap 截断之后的指令不会出现。这就保证了 RVVI 永远只看到「真正生效的架构状态变更」。

#### 4.3.2 核心流程：统一索引空间

ROB 用一个统一的 `idx` 字段记录「这条指令写的是哪类寄存器堆的几号」：

- GPR：`idx` 直接是寄存器号 `1..31`（0 号 `x0` 不写）。
- FPR：`idx = fAddr + floatRegfileBaseAddr`（基址 32），即 `32..63`。
- VPR：`idx = vAddr + rvvRegfileBaseAddr`（基址 64），即 `64..95`。

`RvviTrace` 据此把同一个 `idx` 分流到 `x_wb`/`f_wb`/`v_wb` 三套掩码。此外还有两个特殊值：`noWriteRegIdx`（不写寄存器，如很多分支）、`storeRegIdx`（store 指令，用特殊标记占位）。

#### 4.3.3 源码精读

`RetirementBufferDebugIO` 数据结构定义：
[Interfaces.scala:146-158](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Interfaces.scala#L146-L158) —— 每个退休槽是一个 `Valid` 包裹的 Bundle：`pc`、`inst`、`idx`（统一索引）、`data`（写回值，标量时 32 位、RVV 时为 `rvvVlen` 位）、可选的 `vecWrites`（8 路向量写）、`trap`。

寄存器堆基址定义（决定 idx 分流边界）：
[Parameters.scala:109-117](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L109-L117) —— `floatRegfileBaseAddr = 32`、`rvvRegfileBaseAddr = 64`、三类寄存器堆各 32 个，构成 0..95 的统一索引空间。

ROB 在退休时填充 debug 端口（关键：用 `deqReady` 限定「真退休」）：
[RetirementBuffer.scala:455-474](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RetirementBuffer.scala#L455-L474) —— `valid := (i.U < instBuffer.io.deqReady)`，即只有前 `deqReady` 条按序退休的指令才置 valid；`pc/inst/data/idx/trap` 都用 `MuxOR(valid, ...)` 包裹，并额外处理 trap 时屏蔽写回（`idx` 改成 `noWriteRegIdx`），保证 trapping 指令不在 RVVI 里显示写寄存器。

`RvviTrace` 侧把 `idx` 分流到三类寄存器堆：
[RvviTrace.scala:196-218](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RvviTrace.scala#L196-L218) —— 对每个 GPR/FPR 寄存器号 `j`，判断 `wb_idx === j`（GPR）或 `wb_idx === j + baseAddr`（FPR）来置对应掩码位；向量部分在 `enableRvv` 时从 `vecWrites`（8 路累加器）用 `PriorityMux` 选出命中数据，否则同样按基址分流。

debug 端口的「存在性」受开关控制：
[RetirementBuffer.scala:39](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RetirementBuffer.scala#L39) —— `debug = Option.when(p.shouldExposeDebugPorts)(...)`，与 4.1 节的 `useRetirementBuffer` 一起，构成「验证模式才暴露」的双重门控。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：跟踪一条 `addi a0, a0, 1` 的退休信息如何走完 ROB→RVVI 链路。
2. **步骤**：
   - 读 [RetirementBuffer.scala:455-474](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RetirementBuffer.scala#L455-L474)：确认 `addi` 退休时 `valid` 由 `deqReady` 决定、`idx` = `a0` 的寄存器号（10）、`data` = 计算结果。
   - 读 [RvviTrace.scala:196-199](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RvviTrace.scala#L196-L199)：确认 `wb_idx===10` 会使 `x_wb(i)(10)=1`、`x_wdata(i)(10)=data`。
3. **观察现象**：一个标量指令只在 `x_wb` 里置一位，`f_wb`/`v_wb` 全零。
4. **预期结果**：RVVI 单通道输出 `valid=1, insn=0x..., pc=..., x_wb 的 bit10=1, x_wdata 的 [351:320]=结果`。
5. 运行结果标注：**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：一条 store 指令（如 `sw`）退休时，RVVI 的 `x_wb` 会是什么样？

**参考答案**：`sw` 不写任何架构寄存器。在 ROB 里它的 `idx` 被设成 `storeRegIdx`（一个特殊值），既不等于任何 GPR 号，也不等于 FPR/VPR 基址范围，所以 `RvviTrace` 里 `x_wb`/`f_wb`/`v_wb` 三套掩码全零——store 在 RVVI 里表现为「退休了一条指令（valid=1），但没有寄存器写」。这正是协同验证期望的行为。

**练习 2**：为什么向量写要用 `vecWrites`（8 路）而不是单个 `idx/data`？

**参考答案**：向量指令可能一次写多个向量寄存器（LMUL>1），且 CoralNPU 的向量写会经累加器分多拍汇齐（`vectorWriteAccumulator`），所以 debug 端口提供 `Vec(8, Valid(...))` 容纳一组写。`RvviTrace` 用 `PriorityMux` 在命中项里挑数据。这与标量「最多写一个寄存器」的语义不同。

### 4.4 字段数据来源之二：CSR trace 端口

#### 4.4.1 概念说明

RVVI 还要追踪 **CSR（控制状态寄存器）** 的变化——比如 `mstatus`、`mepc`、`vl`（向量长度）等被 `csrrw/csrrs` 改写的情况。这部分数据不是 ROB 管的（ROB 只管寄存器堆写与 trap），而是由专门的 CSR 模块通过 `io.trace`（类型 `CsrTraceIO`）提供。

`CsrTraceIO` 很简洁：一个 `valid`、一个 12 位 `addr`（CSR 地址空间共 4096 个）、一个 `data`。`RvviTrace` 把这个「单点」信息展开成 RVVI 要求的「4096 路」：对每个 CSR 地址 `j`，判断本拍退休的指令是否写了这个 CSR，是则置 `csr_wb(j)` 并填 `csr(j)`。

#### 4.4.2 核心流程

```
CSR 模块执行 csrrw mstatus, x5, x5
        │
        ▼
io.trace.valid = 1   (注意：纯读 CSRRS/CSRRC 且 rs1==0 时 valid=0)
io.trace.addr  = mstatus 的地址 (0x300)
io.trace.data  = 写入的新值
        │
        ▼  RvviTrace 对每个退休通道 i、每个 CSR 地址 j：
csr_wb(i)(j) = valid_i && csr.valid && (csr.addr === j)
csr(i)(j)    = MuxOR(csr_wb, csr.data)
```

注意一个精妙之处：CSR 写在整个核里是「单点」（一拍最多一条 CSR 指令退休），但 RVVI 把它广播到所有 8 个退休通道，由 `valid_i` 限定——只有真正退休那条 CSR 指令的通道才会置 `csr_wb`。

#### 4.4.3 源码精读

`CsrTraceIO` 数据结构：
[Interfaces.scala:279-283](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Interfaces.scala#L279-L283) —— `valid: Bool`、`addr: UInt(12.W)`、`data: UInt(xlen.W)`，三字段刻画「写了一个 CSR」。

CSR 模块对外提供 trace 端口：
[Csr.scala:227](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L227) —— 在 CSR 模块 IO 里声明 `trace = Output(new CsrTraceIO(p))`。

CSR trace 的填充逻辑（关键：屏蔽「伪写」）：
[Csr.scala:641-643](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L641-L643) —— `trace.valid` 仅在「真写」时拉高：`req.valid && !(op 是 CSRRS/CSRRC 且 rs1==0)`。因为 `csrrs x0, csr, x0` 这种「读不改写」不应算作 CSR 写。`addr` 与 `data` 直接取自请求。

`RvviTrace` 把 CSR 单点展开成 4096 路：
[RvviTrace.scala:220-224](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RvviTrace.scala#L220-L224) —— 对每个 CSR 地址 `j`（0..4095），`csr_wb_valid = valid && io.csr.valid && (io.csr.addr === j)`，命中则置掩码并填值。这一段把一个 12 位地址译码成 4096 路掩码，硬件上等价于一个大的译码器。

`SCore` 把 CSR trace 接进 RVVI：
[SCore.scala:565](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L565) —— `rvvi.io.csr := csr.io.trace`，完成 CSR trace 的接入。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：理解「读不改写」的 CSR 访问为何不出现在 RVVI 里。
2. **步骤**：
   - 读 [Csr.scala:641-643](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L641-L643)，记录 `trace.valid` 的判定条件。
   - 对照 RISC-V 规范：`csrrs`/`csrrc` 当 `rs1==x0` 时定义为「不写 CSR，仅读」。
3. **观察现象**：执行 `csrr t0, mcycle`（实为 `csrrs t0, mcycle, x0`）时，RVVI 的 `csr_wb` 全零——没有 CSR 写事件，只有 GPR `t0` 的写。
4. **预期结果**：纯读 CSR 在 RVVI 里只体现为 GPR 写，不产生 CSR 变更记录。
5. 运行结果标注：**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 CSR trace 只用一个 `addr/data` 单点，而 GPR/FPR/VPR 用位掩码？

**参考答案**：CSR 在一拍内最多被一条指令改写（CSR 指令在派发时被限定为「仅首槽、当作控制流」，见 u5-l3），所以单点即可唯一描述。而寄存器堆理论上可能一拍多写（尤其向量），位掩码更通用。`RvviTrace` 再把 CSR 单点广播+译码成 4096 路，纯粹是为了凑齐 RVVI 标准接口的字段形状。

**练习 2**：如果 `io.csr.valid` 为假（本拍无 CSR 写），[RvviTrace.scala:220-224](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RvviTrace.scala#L220-L224) 会输出什么？

**参考答案**：对所有 `j`，`csr_wb_valid = valid && false && ... = false`，于是 `csr_wb(i)` 全零、`csr(i)` 全零。即 RVVI 报告「本拍没有 CSR 变更」，符合预期。

### 4.5 RVVI 在协同验证中的消费

#### 4.5.1 概念说明

到这里为止，RVVI 还只是 RTL 内部的一组信号。它的真正价值在仿真环境里被消费。CoralNPU 的 UVM 验证平台用 RVVI 做两件事：

1. **功能覆盖率（functional coverage）**：`coralnpu_rvvi_monitor` 每拍采样 RVVI，把退休指令解码成 RV32I/V/F/M/Zicsr/Zbb/Zifencei 等 ISA 分组，喂给覆盖率组（`coralnpu_cov.sv`），统计「哪些指令、哪些寻址模式、哪些 hazard 组合被测过」。
2. **协同验证（co-simulation）**：`spike_cosim_checker` 拿 RVVI 的退休信息，与 Spike 参考模型的 commit 日志逐条比对——同样的 PC 下，RTL 写了哪些寄存器、值是多少，必须和 Spike 完全一致（且双向一致：RTL 写了 Spike 没写也算错）。

#### 4.5.2 核心流程：监控与比对

```
RTL 每拍退休
   │
   ▼
rvviTrace 接口 (valid/order/pc/insn/x_wb/x_wdata/...)
   │
   ├──► coralnpu_rvvi_monitor
   │       ├─ 对每个 valid 通道建一个 transaction
   │       ├─ 用 $clog2(x_wb) 反推被写的 GPR 号，从 x_wdata 取值
   │       ├─ decode() 把 insn 分到 RV32I/V/F/M/Zicsr/Zbb/Zifencei
   │       └─ 写各 analysis_port → covergroup 采样
   │
   └──► spike_cosim_checker
           ├─ 按 PC 把 Spike commit 日志同步到当前退休指令
           ├─ 比对 GPR：Spike 写了 rd → RTL 的 x_wb[rd] 必须为 1 且值相等
           ├─ 比对 FPR / VPR（同理）
           └─ 反向比对：RTL 写了某寄存器 → Spike 必须也写了（防止 RTL 多写）
```

`tb_top` 怎么拿到这组信号？它直接穿透 RTL 层级，把 `u_dut.core.score.rvvi.rvviTraceBlackBox.rvvi`（即 BlackBox 内实例化的标准接口）作为 `virtual rvviTrace` 句柄，通过 `uvm_config_db` 分发给 monitor 与 cosim checker。

#### 4.5.3 源码精读

tb_top 抓取 RVVI 句柄并分发：
[coralnpu_tb_top.sv:317-336](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/uvm/tb/coralnpu_tb_top.sv#L317-L336) —— `rvvi_vif = u_dut.core.score.rvvi.rvviTraceBlackBox.rvvi;` 直接拿到标准接口，再 `uvm_config_db#(...)::set` 分别交给 `m_cosim_checker` 与 `m_rvvi_agent`。接口参数 `ILEN=32, XLEN=32, FLEN=32, VLEN=128, NHART=1, RETIRE=8` 与 RTL 生成端完全一致。

monitor 每拍采样并反推寄存器号：
[coralnpu_rvvi_monitor.sv:100-135](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/uvm/common/rvvi_agent/coralnpu_rvvi_monitor.sv#L100-L135) —— `for (int i=0; i<RETIRE; i++) if (rvvi_vif.valid[0][i])` 逐通道处理；当 `x_wb>0` 时 `gpr_reg = $clog2(x_wb)` 反推寄存器号，再 `x_wdata >> (gpr_reg*XLEN)` 取出该寄存器的写值，维护一份 `gpr_reg_val[]` 影子寄存器堆。

cosim checker 的 GPR/FPR/VPR 值比对（双向）：
[spike_cosim_checker.sv:104-170](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/uvm/common/cosim/spike_cosim_checker.sv#L104-L170) —— 对每条 Spike 事务：若 Spike 写了 GPR `rd`，则要求 RTL 的 `x_wb[rd]` 为 1 且 `rvvi_vif.x_wdata[0][retire_index][rd]` 等于 Spike 的值，否则 `uvm_fatal`；FPR/VPR 同理。随后第 4 步反向遍历 32 个寄存器：若 RTL 的掩码位置 1 而 Spike 没写，也 `uvm_fatal`——保证一一对应。

`retired_instr_info_s` 结构与 retire_index：
[coralnpu_cosim_checker_pkg.sv:40-47](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/uvm/common/cosim/coralnpu_cosim_checker_pkg.sv#L40-L47) —— 比对信息结构体含 `pc`、`x_wb/f_wb/v_wb` 掩码、`retire_index`（0..7 的通道号）。因为 `x_wdata` 是按通道索引的，必须记住指令从哪个通道退休，才能正确取值。

collect 阶段把 RVVI 掩码装进结构体：
[coralnpu_cosim_checker_pkg.sv:224-238](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/uvm/common/cosim/coralnpu_cosim_checker_pkg.sv#L224-L238) —— `info.x_wb = rvvi_vif.x_wb[0][i]`、`info.retire_index = i`，把每拍各通道的掩码与通道号收集成队列，供后续 `step_and_compare` 与 Spike 比对。

#### 4.5.4 代码实践（设计型：最小一致性检查点）

这是本讲的综合实践，目标是用 RVVI 字段设计一个最小的「RTL vs 参考模型」一致性检查器。

1. **目标**：不依赖完整 Spike，只用 RVVI 的 `valid/order/pc/x_wb/x_wdata` 五个字段，设计一个能抓住「RTL 多写/少写/写错值」三类错误的检查点。
2. **操作步骤**（伪代码，标注为「示例代码」，非项目原有）：

   ```python
   # 示例代码：最小 RVVI 一致性检查器（概念演示，非仓库内实现）
   ref_model = {}            # 参考模型的架构寄存器堆快照（由你信任的来源喂入）
   for cycle in rvvi_stream:
       for ch in range(RETIRE):            # RETIRE = 8
           if not ch.valid: continue
           order = ch.order                 # 用来排序，保证按程序序处理
           pc   = ch.pc
           mask = ch.x_wb                   # 32 位 one-hot
           data = ch.x_wdata                # 32×32 拼接，按 reg 号切片
           regs_written = bits_set(mask)    # 置 1 的位集合
           # 检查 1：标量指令最多写一个 GPR
           assert len(regs_written) <= 1, f"{pc:#x}: 标量指令写了多个 GPR"
           # 检查 2：与参考模型比对
           exp = ref_model.retire(order)    # 参考模型同 order 的写
           if exp.reg is None:
               assert not regs_written, f"{pc:#x}: 参考模型未写，RTL 却写了"
           else:
               assert regs_written == {exp.reg}, f"{pc:#x}: 写错寄存器"
               rtl_val = slice(data, exp.reg)   # 取该寄存器号对应的 32 位
               assert rtl_val == exp.val, f"{pc:#x}: 值不一致 rtl={rtl_val:#x} exp={exp.val:#x}"
   ```
3. **需要观察的现象**：
   - 若 RTL 多写一个寄存器（`len(regs_written) > 1` 或 `regs_written != {exp.reg}`），检查 1/2 报错——对应 `spike_cosim_checker.sv` 第 151-170 行的「反向比对」思想。
   - 若值不一致，检查 2 报错——对应第 114-118 行的 `SPIKE_VAL_MISMATCH`。
4. **预期结果**：在正常程序上检查器全程静默；若你故意在 RTL 里把某条 `add` 的结果改错，检查器应在该指令退休的那一拍立即报错。
5. 运行结果标注：**待本地验证**（需要先有一份参考模型输出与一份 RVVI 采样流；可借助项目的 cocotb/UVM 流程获取）。

> 想跑真实流程：可参考 [u11-l2 VCS 与 UVM 验证](u11-l2-vcs-uvm.md) 用 `--config=vcs` 启动 UVM，并设置 `current_spike_log` 指向 Spike 日志（见 [coralnpu_cosim_checker_pkg.sv:359-363](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/uvm/common/cosim/coralnpu_cosim_checker_pkg.sv#L359-L363)），即可让项目自带的 `spike_cosim_checker` 做完整三向（RTL / Spike / 覆盖率）协同验证。

#### 4.5.5 小练习与答案

**练习 1**：`spike_cosim_checker` 为什么要做「反向比对」（第 151-170 行），而不只是「Spike 写了什么，RTL 也得写什么」？

**参考答案**：单向比对只能抓「RTL 少写/写错」，抓不到「RTL 多写」（即 RTL 偷偷写了一个参考模型没写的寄存器）。反向遍历 32 个寄存器、检查「RTL 掩码置 1 的位 Spike 是否也写了」，能补上这一类错误，保证 RTL 与参考模型的状态更新严格一一对应。

**练习 2**：`retire_index`（通道号）在 cosim 里为什么必不可少？

**参考答案**：`x_wdata` 是 `x_wdata[0][retire_index][reg]` 三维索引——第一维 hart、第二维通道、第三维寄存器号。一拍可能有多条指令退休，它们各占一个通道，写值分别存在各自通道的拼接数据里。只有记住 `retire_index`，才能从正确的通道切片取值，否则会取到别的指令的写值。

## 5. 综合实践

把本讲知识串起来，完成下面这个端到端的「读 RVVI 接线图」任务。

**任务**：画出从「ROB/CSR 内部状态」到「UVM cosim 报错」的完整数据通路，并在图上标注每一处对应的源码位置。

**建议步骤**：

1. 从 [RetirementBuffer.scala:455-474](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RetirementBuffer.scala#L455-L474) 与 [Csr.scala:641-643](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L641-L643) 出发，画出两个数据源（`robDebug`、`csr.io.trace`）。
2. 经 [SCore.scala:562-566](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L562-L566) 进入 `RvviTrace`，再经 [RvviTrace.scala:159-224](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RvviTrace.scala#L159-L224) 聚合成 RVVI 字段，过 BlackBox（[RvviTrace.scala:119-140](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RvviTrace.scala#L119-L140)）出到标准 `rvviTrace` 接口。
3. 经 [coralnpu_tb_top.sv:319-336](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/uvm/tb/coralnpu_tb_top.sv#L319-L336) 的句柄抓取，分发到 monitor（[coralnpu_rvvi_monitor.sv:100-135](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/uvm/common/rvvi_agent/coralnpu_rvvi_monitor.sv#L100-L135)）与 cosim checker（[spike_cosim_checker.sv:104-170](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/uvm/common/cosim/spike_cosim_checker.sv#L104-L170)）。
4. 在图上用不同颜色标出三类信息流：**退休指令元信息**（valid/order/pc/insn/trap）、**寄存器写**（x/f/v_wb + wdata）、**CSR 写**（csr_wb + csr）。
5. 最后在图末端写出三类可能的报错（少写、多写、值错）分别对应检查器的哪段代码。

**交付物**：一张数据通路图 + 一张「错误类型 ↔ 检查代码行」对照表。

## 6. 本讲小结

- RVVI 是 RISC-V 标准的**非侵入式指令追踪接口**，挂在 CoralNPU 退休缓冲输出上，每拍报告「哪些通道退休了指令、改了哪些架构状态」。
- CoralNPU 用 **Chisel 聚合（`RvviTrace`）+ SV BlackBox（`RvviTraceBlackBox`，由 `GenerateRvviTraceSource` 自动生成）** 的两段式实现，对外暴露标准 `rvviTrace` 接口（`ILEN=32, XLEN=32, FLEN=32, VLEN=128, NHART=1, RETIRE=8`）。
- RVVI 字段分四类：**退休元信息**（valid/order/insn/pc_rdata/trap/debug_mode）、**GPR 写**（x_wb/x_wdata）、**FPR 写**（f_wb/f_wdata）、**VPR 写**（v_wb/v_wdata）、**CSR 写**（csr_wb/csr）；其中 `order` 由内部 `count` 计数器 + 通道偏移生成，保证多通道退休可被还原成单条流。
- 数据来源有两个：ROB 的 `RetirementBufferDebugIO`（用统一索引空间区分 GPR/FPR/VPR，基址 32/64）与 CSR 模块的 `CsrTraceIO`（单点，屏蔽「读不改写」）。
- RVVI 是**验证模式专属**：受 `useRetirementBuffer`/`shouldExposeDebugPorts` 门控，生产裁剪配置里不存在。
- RVVI 在仿真中被两类消费者使用：UVM monitor 驱动功能覆盖率，`spike_cosim_checker` 用 Spike 参考模型做双向（值 + 写/不写对称）逐条比对，是 RTL 正确性的关键护栏。

## 7. 下一步学习建议

- 想看 RVVI 信号在仿真里真正流动起来：继续学习 [u11-l2 VCS 与 UVM 验证](u11-l2-vcs-uvm.md)，了解 `--config=vcs` 如何启用 UVM 平台与覆盖率回归，以及 `rvv_backend_tb` 的 agent/scoreboard/coverage 三件套。
- 想理解「为什么 RVVI 要按退休而非派发追踪」的更深层原因：回顾 [u4-l4 派发规则、记分板与退休](u4-l4-dispatch-scoreboard-retire.md) 中 RetirementBuffer 的 `deqReady`/trap 截断逻辑。
- 想看另一条「总线层」的可观测性通路：阅读 [u9-l3 总线完整性与 SECDED](u9-l3-secded-integrity.md)，对比「指令级追踪（RVVI）」与「数据级完整性（ECC/SECDED）」这两种互补的观测/防护手段。
- 建议继续精读的源码：`tests/uvm/common/rvvi_agent/coralnpu_cov.sv`（覆盖率如何采样 RVVI 解码结果）、`tests/uvm/common/cosim/coralnpu_cosim_checker_pkg.sv`（`step_and_compare` 的完整比对时序）。
