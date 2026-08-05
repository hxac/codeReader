# 功能单元 ALU/FPU/LSU/SFU

## 1. 本讲目标

本讲是「GPU 执行模型与核心流水线（SimX 视角）」的最后一站——流水线的 **Execute（执行）级**。前面 u6-l3 讲完了「指令能不能发射」，本讲回答「发射出去的指令由谁执行、怎么执行、结果怎么写回」。

学完后你应该能够：

1. 读懂 `FuncUnit<NUM_BLOCKS>` 这套 CRTP 模板基类，说清 `Inputs`/`Outputs` 两条 channel 数组与 `on_tick`/`on_reset` 钩子的契约。
2. 解释 ALU 与 FPU 为何是「纯 channel 延迟」单元——内部几乎不持有状态，全部计算延迟由 `output.send(trace, delay)` 的 channel 延迟来建模。
3. 理解 LSU 为何是「带内部状态」的例外——访存延迟是数据相关的（命中/缺失），必须用 `pending_reqs` 表追踪在途请求。
4. 说清 SFU 为何不是一个执行单元而是「单端口分派器」：它按 `op_type` 把指令路由到 WCTL/CSR/DXA/TEX/OM/RASTER/RTU 等子单元，再把结果汇聚回同一个输出端口。
5. 有一份「新增一个 FuncUnit」的设计清单。

## 2. 前置知识

在进入本讲前，请确认你已经掌握以下前置讲义的概念：

- **u5-l1（SimObject/SimChannel/SimPlatform）**：本讲会反复用到「channel 就是流水线」这条结论——一个带 `delay=N` 的 `SimChannel` 就是 N 级寄存器，单元内部不必再手写 stage deque。
- **u6-l2（取指/译码）**：译码器把每条指令填好 `fu_type`（`FUType::ALU/LSU/FPU/SFU/TCU`）和 `op_type`，宏指令由 sequencer 展开成微操作（uop）。
- **u6-l3（发射/记分板/操作数收集）**：指令在 Issue 级读好操作数、占好目的寄存器、通过记分板冒险检测，然后被 Dispatcher 拆成 SIMD packet，送到本讲的 FuncUnit 输入端。

补两个本讲直接用到、但前面没展开的小概念：

- **`instr_trace_t`（指令影子）**：一条指令在流水线里流动的「影子对象」。本讲最关心的字段是 `fu_type`（去哪个 FU）、`op_type`（FU 内部具体哪个子操作）、`tmask`（发射时快照的线程激活掩码）、`src_data`（已读好的源操作数，按 `[寄存器序号][线程号]` 组织）、`dst_data`（FU 要填的结果，按 `[线程号]` 组织）。它的构造见 [sim/simx/instr_trace.h:86-111](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/instr_trace.h#L86-L111)。
- **`FUType` 枚举**：`{ALU, LSU, FPU, SFU, TCU}`，它既是「指令去哪个 FU」的路由键，又被直接当作数组下标（`func_units_[(int)fu_type]`、`dispatchers_[(int)fu_type]`），定义见 [sim/simx/types.h:176-185](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/types.h#L176-L185)。

## 3. 本讲源码地图

本讲围绕 5 个源码文件展开，它们恰好构成「1 个基类 + 4 个派生单元」的对称结构：

| 文件 | 角色 |
|------|------|
| [sim/simx/func_unit.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/func_unit.h) | `FuncUnitBase` 类型擦除基类 + `FuncUnit<NUM_BLOCKS>` CRTP 模板。定义 Inputs/Outputs channel 与 `on_tick` 钩子。 |
| [sim/simx/alu_unit.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/alu_unit.cpp) / [.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/alu_unit.h) | ALU：整数运算、分支、乘除、投票/洗牌。**同步、定长延迟**的典型。 |
| [sim/simx/fpu_unit.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/fpu_unit.cpp) / [.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/fpu_unit.h) | FPU：浮点加减乘除、FMA、类型转换。同样是同步定长，但延迟更高。 |
| [sim/simx/lsu_unit.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp) / [.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.h) | LSU：访存。**带内部状态**的例外（AGU、在途请求表、fence 控制器）。 |
| [sim/simx/sfu_unit.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sfu_unit.cpp) / [.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sfu_unit.h) | SFU：特殊功能单元。**单端口分派器**，路由到一堆子单元。 |

辅助理解的接线代码在 [sim/simx/core.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp) 与 [sim/simx/dispatcher.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dispatcher.cpp)。

## 4. 核心概念与源码讲解

### 4.1 FuncUnit CRTP 基类与 I/O channel 模型

#### 4.1.1 概念说明

Vortex 的 Core 有 5 类执行单元（ALU/FPU/LSU/SFU/TCU），它们体量不同、延迟不同、有的还有内部状态。如果让 Core 直接持有 5 个不同类型的成员，代码会非常凌乱。SimX 的解法是：

1. 抽象出一个 **`FuncUnitBase`** 接口（类型擦除），让 Core 用一个 `vector<shared_ptr<FuncUnitBase>>` 把异构单元装进同一个容器，并能统一地「按 block 编号取输入/输出 channel」。
2. 再用一个 **`FuncUnit<NUM_BLOCKS>`** 模板，把「每个单元有 `NUM_BLOCKS` 条物理 lane、每条 lane 一对 `Inputs[b]/Outputs[b]` channel、每拍 `on_tick` 驱动一次」这套共性固化下来。具体单元（AluUnit 等）只需继承它、实现 `on_tick`。

为什么是 **CRTP**（Curiously Recurring Template Pattern，奇特递归模板模式，即 `class AluUnit : public FuncUnit<...>` 把自己当模板参数传给父类的 `SimObject<FuncUnit<N>>`）？因为 u5-l1 讲过：`SimObject<Impl>` 用 CRTP 在「被动模块零开销」检测里避免虚函数调用——每周期被平台 tick 上千次的单元，虚调用开销不可忽视。

#### 4.1.2 核心流程

一个 FuncUnit 在每拍 `on_tick()` 里做的事情，可以用下面这段统一的「形状」概括（ALU/FPU 严格遵循，LSU/SFU 有变形，后两节展开）：

```
on_tick():
  for b in 0..NUM_BLOCKS:              # 遍历每条物理 lane
    if Inputs[b].empty():   continue    # 没活干
    if Outputs[b].full():   continue    # 下游背压，停顿
    trace = Inputs[b].peek()            # 窥视队头，不弹出
    execute(trace)                      # 派生类私有：算结果填进 dst_data
    delay = latency_of(trace)           # 派生类私有：这条指令算多久
    Outputs[b].send(trace, delay)       # 经 channel 延迟送到下游
    Inputs[b].pop()                     # 真正弹出
```

关键设计点：

- **`NUM_BLOCKS` 是物理 lane 数，不是 `VX_CFG_ISSUE_WIDTH`**。Core 在更上游用 Dispatcher 把 `VX_CFG_ISSUE_WIDTH` 个发射通道**汇聚**到 `NUM_BLOCKS` 个执行端口，commit 阶段再用 `trace->wid` 扇出回去。
- **延迟由 channel 承载**。`execute()` 同步算完结果后，`send(trace, delay)` 把 trace 排程到 `delay` 拍后到达输出端。这正是 u5-l1「channel 就是流水线」的直接体现——ALU/FPU 单元内部**没有**任何多级寄存器或 stage 队列，整条流水线的延迟就是这条 channel 的 delay。
- **背压靠 `Outputs[b].full()`**。下游 commit 仲裁器没腾出位置时，本单元这一拍就停住、不 pop 输入，下拍重试。

#### 4.1.3 源码精读

先看类型擦除基类 `FuncUnitBase`。它只有三个纯虚函数，目的是让 Core 能对异构单元「按 block 取 channel、问它有几条 lane」：

[func_unit.h:26-32 — FuncUnitBase：类型擦除接口](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/func_unit.h#L26-L32) —— 这段定义了 `input(b)`/`output(b)`/`num_blocks()` 三个访问器，让 Core 的 `vector<shared_ptr<FuncUnitBase>>` 可以统一操作所有单元。

接着是模板本体 `FuncUnit<NUM_BLOCKS>`，它把「每条 lane 一对 channel」固化下来：

[func_unit.h:38-58 — FuncUnit 模板：Inputs/Outputs 数组与 FuncUnitBase 实现](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/func_unit.h#L38-L58) —— 注意第 43–44 行 `std::array<SimChannel<instr_trace_t*>, NUM_BLOCKS> Inputs/Outputs;` 是核心数据成员；第 56–58 行把这三个数组实现成 `FuncUnitBase` 的虚函数。第 41 行 `kNumBlocks = NUM_BLOCKS` 把模板参数暴露成编译期常量。

再看「钩子」是如何被注入的。这里有一个不显然的 C++ 技巧，值得停下读懂：

[func_unit.h:60-69 — 通过新增虚函数让派生类能重写 on_tick](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/func_unit.h#L60-L69) —— 注释说得很直白：`SimObject<FuncUnit<N>>::on_tick` 本身是个非虚空操作；为了让 AluUnit 这类派生类能 override，这里**额外声明**了 `virtual void on_tick() = 0;`，由 CRTP 的 `do_tick()` 转发穿透。于是「被动模块零开销」的 SimObject 机制与「派生类各自实现执行逻辑」两者兼得。`on_reset()` 默认空实现（第 64 行），只有 LSU 这种带状态的单元才去 override。

最后看 Core 如何实例化这 5 个单元并装进同一个容器——这段代码直接印证了「FUType 当数组下标」的设计：

[core.cpp:234-242 — 实例化 ALU/FPU/LSU/SFU 四类单元](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L234-L242) —— 每个单元用 `create_object<XxxUnit>` 创建，按 `(int)FUType::XXX` 的下标塞进 `func_units_`。容器声明在 [core.cpp:937](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L937)：`std::vector<std::shared_ptr<FuncUnitBase>> func_units_;`。

> 关于 TCU：它走同一套 `FuncUnitBase` 接口，但实现复杂得多（张量核），本讲不讲，留给 u9-l1。

#### 4.1.4 代码实践

**实践目标**：亲手把 `FuncUnit` 基类的「契约」抄一遍，建立新增单元的肌肉记忆。

**操作步骤**：

1. 打开 [sim/simx/func_unit.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/func_unit.h)，逐行读完 72 行。
2. 打开 [sim/simx/alu_unit.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/alu_unit.h)，对比 AluUnit 的声明：它继承了 `FuncUnit<VX_CFG_NUM_ALU_BLOCKS>`，只 override 了 `on_tick()`，并私有声明 `execute()` 与 `latency_of()`。
3. 用纸笔或注释形式，写出「一个全新的 `MyUnit` 要实现什么」：
   - 继承 `FuncUnit<VX_CFG_NUM_MY_BLOCKS>`；
   - 构造函数转发给 `FuncUnit(...)`；
   - override `on_tick()`：内部循环 `NUM_BLOCKS` 条 lane，做 peek → execute → latency → send → pop；
   - 私有 `execute(instr_trace_t*)`：读 `trace->src_data`、写 `trace->dst_data`；
   - 私有 `latency_of(const instr_trace_t*)`：返回该指令的延迟拍数。

**需要观察的现象**：你会确认「新增一个同步定长单元」其实只需写两个私有函数 + 一个 on_tick 模板循环，模板本身已经把 channel、背压、类型擦除全部搞定。

**预期结果**：写出的清单与 AluUnit/FpuUnit 的结构一一对应。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `FuncUnit` 要同时继承 `FuncUnitBase` 和 `SimObject<FuncUnit<NUM_BLOCKS>>`？去掉 `FuncUnitBase` 会怎样？

**答案**：`SimObject<...>` 让它获得 `on_tick` 生命周期、能被 `SimPlatform` 调度；`FuncUnitBase` 提供类型擦除，让 Core 能把 `AluUnit<4>`、`LsuUnit<2>` 这些**不同模板实例**（不同 `NUM_BLOCKS`、不同派生类型）装进同一个 `vector<shared_ptr<FuncUnitBase>>` 并统一取 channel。去掉 `FuncUnitBase`，Core 就无法用统一容器持有异构单元。

**练习 2**：`NUM_BLOCKS` 和 `VX_CFG_ISSUE_WIDTH` 哪个大？它们如何配合？

**答案**：二者独立。`VX_CFG_ISSUE_WIDTH` 是每拍发射通道数，`NUM_*_BLOCKS` 是该 FU 的物理 lane 数。上游 Dispatcher 把 `ISSUE_WIDTH` 个通道**汇聚**到 `NUM_BLOCKS` 个执行端口（见 [dispatcher.cpp:19-32](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/dispatcher.cpp#L19-L32)），commit 阶段再用 `trace->wid` 扇出回去。默认配置下 `ISSUE_WIDTH` 往往等于各 `NUM_*_BLOCKS`，呈直通形态。

---

### 4.2 ALU 与 FPU：同步执行与可变延迟

#### 4.2.1 概念说明

ALU 和 FPU 是「最守规矩」的两个单元——它们完全符合 4.1.2 的模板形状：无内部状态、计算结果一次性算完、延迟由 channel 承载。

- **ALU** 承载整数与控制语义：算术逻辑（ADD/SUB/AND/OR…）、整乘整除（MUL/DIV/REM）、分支（BR/JAL/JALR/SYS）、以及 warp 级 SIMT 专用操作——投票（VoteType：ALL/ANY/UNI/BAL）、跨 lane 洗牌（ShflType：UP/DOWN/BFLY/IDX）、聚集（WgatherType）。注意分支也由 ALU 处理，因为它会**直接改 `warp.PC`**。
- **FPU** 承载浮点语义：加减乘除、FMA、比较、符号注入、最值、整型⇄浮点转换等。它把真正复杂的 IEEE-754 计算委托给一个软浮点库 `rvfloats`（`rv_fadd_s`、`rv_fmadd_d` 等），自己只负责「按线程循环 + NaN-boxing + 收集异常标志」。

两者最大的区别是**延迟量级**：

| 单元 | 典型操作 | 延迟（拍） |
|------|----------|-----------|
| ALU | ADD/SUB/逻辑/分支/乘法 | 2 |
| ALU | DIV/REM（迭代除法） | \( \text{XLEN} + 2 \)（如 64 位时为 66） |
| FPU | 比较/符号/移动 | 4（`2+delay`） |
| FPU | FADD/FMUL/FMA | \( \text{FMA\_LATENCY} + 2 \) |
| FPU | FDIV / FSQRT | \( \text{FDIV\_LATENCY}+2 \) / \( \text{FSQRT\_LATENCY}+2 \) |

#### 4.2.2 核心流程

以 ALU 为例，`on_tick` 严格遵循 4.1.2 的模板。`execute()` 内部则是一个按 `trace->op_type` 的 `variant` 分发的大 switch：先取出每线程的源操作数 `rs1/rs2/rs3`，遍历 `tmask` 中活跃的线程，逐线程算结果写进 `rd_data[t]`。

一个贯穿所有单元的关键不变量（代码里有显式注释强调）：

> **用 `trace->tmask`（发射时快照），而不是 `warp.tmask`（实时状态）**。

因为发射之后、执行之前，warp 可能因分支发散改变了实时 tmask；而流水线后续的 commit/writeback 都按 `trace->tmask` 工作，两者不一致就会让某些 lane 拿到陈旧结果。

FPU 的流程几乎一样，只是 `execute()` 把整数算术换成了对 `rvfloats` 库的调用，并多了 NaN-boxing 处理（RISC-V 规定 32 位浮点寄存器在 64 位 FPR 里要「装箱」——高位全 1）。

#### 4.2.3 源码精读

先看 ALU 的 `on_tick`——它就是 4.1.2 模板的逐字实现：

[alu_unit.cpp:524-538 — AluUnit::on_tick：模板化的取指-执行-延迟-发送循环](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/alu_unit.cpp#L524-L538) —— 第 527–528 行处理输入空（跳过）；第 530–531 行处理输出满（停顿）；第 532 行 peek；第 533 行调私有 `execute`；第 534–535 行用 `latency_of` 的返回值作为 channel delay 发送；第 536 行 pop。这就是「channel 即流水线」。

接着看 `latency_of` 如何体现「可变延迟」：

[alu_unit.cpp:31-87 — AluUnit::latency_of：按 op_type 查延迟表](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/alu_unit.cpp#L31-L87) —— 大多数整数操作返回 `2`（第 48 行）；但 DIV/DIVU/REM/REMU 返回 `VX_CFG_XLEN+2`（第 81 行），因为硬件除法是迭代的，位宽越宽周期越多。这正是「定长延迟」单元里仍可以有「按操作可变延迟」的体现——延迟值随指令走，但每条指令的延迟是**编译期可确定、与数据无关**的常量。

再看 `execute()` 里一个有代表性的分支——整数 ADD（含 RV64 的 `.w` 后缀处理）：

[alu_unit.cpp:144-154 — AluUnit::execute 的 ADD 分支：逐线程计算 + is_w 32 位窄化](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/alu_unit.cpp#L144-L154) —— `for (t ...)` 遍历线程，`if (!tmask.test(t)) continue;` 跳过非活跃线程；`aluArgs.is_imm` 区分立即数还是寄存器源；`is_w_enabled && aluArgs.is_w` 分支做 32 位运算后用 `sext(..., 32)` 符号扩展回 64 位（RV64W 语义）。这套「遍历线程 + tmask 门控」是所有 FU execute 的统一范式。`is_w_enabled` 由 [alu_unit.cpp:117-120](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/alu_unit.cpp#L117-L120) 根据 `VX_CFG_XLEN_64` 宏在编译期决定。

ALU 还承担分支——它会改 `warp.PC`：

[alu_unit.cpp:336-352 — BR 分支：用最后一个活跃线程的比较结果决定是否跳转](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/alu_unit.cpp#L336-L352) —— 注意它用 `thread_last`（最后一个活跃线程）来判定 `curr_taken`，然后 `warp.PC = trace->PC + offset;`。这就是「PC 是 warp 级」的体现——同一个 warp 只有一个 PC，分支判定取一个统一结果。

FPU 的结构与 ALU 同构，看两个有代表性的点即可。首先是延迟表：

[fpu_unit.cpp:46-76 — FpuUnit::latency_of：浮点延迟由配置常量驱动](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/fpu_unit.cpp#L46-L76) —— 第 48 行 `const uint32_t delay = 2;` 是一个固定的「额外跳数」，叠加在操作本身的计算延迟上。所以 FCMP 这类返回 `2+delay=4`，FMA 返回 `VX_CFG_FMA_LATENCY+delay`，FDIV 返回 `VX_CFG_FDIV_LATENCY+delay`。这些 `*_LATENCY` 都来自 `VX_config.toml`，可与 RTL 对齐。

然后是 NaN-boxing 辅助函数与 FADD 分支：

[fpu_unit.cpp:28-44 — NaN-boxing 辅助：把 32 位浮点装进 64 位 FPR](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/fpu_unit.cpp#L28-L44) —— `nan_box` 把低 32 位浮点值高位填 `0xffffffff`；`check_boxing` 检测若未合法装箱则替换成 NaN。这是 RISC-V NaN-boxing 不变量的软实现。

[fpu_unit.cpp:102-114 — FpuUnit::execute 的 FADD 分支：调用软浮点库 + 收集异常标志](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/fpu_unit.cpp#L102-L114) —— `is_f64` 区分双精度（直接调 `rv_fadd_d`）与单精度（调 `rv_fadd_s` 且结果 NaN-box）；`emu.get_fpu_rm` 取每线程的舍入模式（来自 CSR/FCSR）；`emu.update_fcrs` 把 `fflags`（NX/UF/OF/DZ/NV 五个 IEEE-754 异常标志）写回 CSR。FPU 的 `on_tick` 见 [fpu_unit.cpp:375-389](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/fpu_unit.cpp#L375-L389)，与 ALU 同构。

#### 4.2.4 代码实践

**实践目标**：跟踪一条 `mul` 指令在 ALU 里的延迟路径，并验证「除法比乘法慢得多」。

**操作步骤**：

1. 在 [alu_unit.cpp:388-518](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/alu_unit.cpp#L388-L518) 找到 `MdvType` 分支，确认 `MUL` 走第 392–402 行、`DIV` 走第 427–450 行。
2. 回到 [alu_unit.cpp:69-84](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/alu_unit.cpp#L69-L84) 的 `latency_of`：`MUL` 返回 2，`DIV` 返回 `VX_CFG_XLEN+2`。
3. 查 `VX_config.toml`（或用 `gen_config.py --format cflags`，见 u2-l2）确认当前 `XLEN`（32 或 64），算出 DIV 的实际拍数。

**需要观察的现象**：MUL 与 ADD 同为 2 拍（单周期乘法器在现代工艺下常见），而 DIV 在 64 位下要 66 拍——这正是为什么编译器会把 `x/常量` 优化成乘以倒数。

**预期结果**：能口算出「在 `--xlen=64` 下，一条 `div` 指令占用 ALU 输出 channel 66 拍」，并解释这 66 拍里该 lane 不能接受新指令（因为 trace 还在 channel 里飞，`Outputs[b]` 未被消费）。

> 待本地验证：具体拍数取决于你构建树里 `VX_CFG_XLEN` 的实际取值。

#### 4.2.5 小练习与答案

**练习 1**：为什么 FPU 的 `latency_of` 里所有返回值都带一个 `+delay`（`delay=2`）？

**答案**：这个 `delay=2` 是除「操作本身计算延迟」之外的固定 channel 跳数（execute 的输出送到 commit 还要经过若干级 channel）。把它单独命名，便于把「算法延迟」（如 `FMA_LATENCY`）与「互连/调度开销」分开配置，对齐 RTL。

**练习 2**：FPU 的 `execute` 里每个 case 都调用 `emu.update_fcrs(fflags, wid, t)`，为什么必须每线程都调？

**答案**：IEEE-754 异常标志（NV 比如除零、OF 上溢…）是**粘性（sticky）**的——任何一个线程产生的标志都应累积进 `fcsr.fflags`。因为每线程的输入数据不同（有的线程可能除零、有的不会），必须逐线程检查并按位或累加，不能只看一个线程。

---

### 4.3 LSU：AGU、流水化请求/响应与 fence

#### 4.3.1 概念说明

LSU 是 4 个单元里**唯一打破「无内部状态、纯 channel 延迟」形状**的单元。原因很根本：**访存延迟是数据相关的**——一次 load 命中 L1 只要几拍，未命中要等几十上百拍，而且 warp 内不同线程的地址可能命中/缺失各不相同。你没法用一个编译期常量 `latency_of` 来描述它。

所以 LSU 必须维护内部状态：

- **AGU（地址生成单元）**：对每条访存指令，按 `addr = rs1 + stride*rs2 + offset` 为每个线程算地址。
- **在途请求表 `pending_reqs`**：发出 load 后，把「这条请求来自哪个 trace、对应哪些 lane」记下来，等响应回来时才能把数据拼回正确的寄存器。
- **fence 控制器**：实现 `fence` 与 barrier 的「先排空再继续」语义。

此外，LSU 还要处理两个特例：**AMO（原子操作）**（也走 LSU，但它有读-改-写回返）和 **packed load 宏指令**（`PACKLB.F`/`PACKLH.F`，由 `LsuUopGen` 展开成多个 uop）。

#### 4.3.2 核心流程

LSU 每个 block 的状态机（`lsu_state_t`）里有三段流水，`on_tick` 按固定顺序调用：

```
on_tick():
  for b in 0..NUM_LSU_BLOCKS:
    process_response_step(b)   # 1. 先收响应：把 cache 返回的数据拼回 dst_data
    process_request_step(b)    # 2. 再发请求：从 req_queue 取队头发内存请求
    ingest_inputs(b)           # 3. 最后吃输入：Inputs[b] → req_queue（造 1 拍延迟）
```

这个「先消费、再生产、最后摄取」的顺序不是随意的——它让 `req_queue` 成为一个**真实的 1 拍流水级**，而不是同拍直通（注释在 [lsu_unit.cpp:524-527](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp#L524-L527) 明确写了「A while-loop here would fabricate write bandwidth」）。

一条 load 的完整生命周期：

1. trace 进 `Inputs[b]`，下拍被 `ingest_inputs` 推进 `req_queue`。
2. `process_request_step` 取队头，调 `compute_addrs` 算地址，按每拍 `NUM_LSU_LANES` 条为一拍（beat）发往 `lmem_switch`（通往 local mem / dcache）；同时为这条请求在 `pending_reqs` 里**分配一个 tag**，记下 trace、lane 信息、eop。
3. warp 内线程较多时，一条指令会被拆成多个 beat 发送；最后一个 beat 标记 `eop`（end of packet）。
4. cache 处理完后，响应（带整条 cache line）经 `lmem_switch->RspOut` 回来，`process_response_step` 按 tag 找回 trace，把 line 数据按宽度格式化（LB 符号扩展、LW、NaN-box…）写进对应 lane 的 `dst_data`。
5. 当一个 trace 的所有分包（`count` 减到 0）都回来，且是 `eop`，才 `output.send(trace, 1)` 把 trace 送往 commit——此时结果才齐。

store 更简单：它是「直写（direct-commit）」——发完请求即可 `output.send(trace)`，不必等响应。

#### 4.3.3 源码精读

先看 LSU 的状态结构，理解它持有哪些「硬件子块」：

[lsu_unit.h:128-148 — lsu_state_t：每个 block 的内部状态](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.h#L128-L148) —— `req_queue`（输入暂存，深度 `VX_CFG_LSU_QUEUE_IN_SIZE`）、`pending_reqs`（在途请求表，深度 `VX_CFG_LSU_PENDING_SIZE`，注释明确说它「不是 MSHR——cache 有自己的 MSHR」）、`fence`（fence 控制器）、`addr_list`/`remain_addrs`（AGU 产出的地址列表与剩余未发数）。整个 LSU 有 `NUM_LSU_BLOCKS` 个这样的状态，见 [lsu_unit.h:150](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.h#L150)。

接着看 AGU 的地址公式——这是理解访存最关键的一行：

[lsu_unit.cpp:110-173 — LsuUnit::compute_addrs：AGU 地址生成](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp#L110-L173) —— 第 157 行 `e.addr = Word(rs1_data[t].i + (uint64_t)stride * rs2_data[t].u + offset);` 就是 AGU 公式 \(\text{addr} = \text{rs1} + \text{stride}\cdot\text{rs2} + \text{offset}\)。第 159–163 行：store/AMO 还要把 `rs2` 作为数据或 RMW 操作数带上。注释（第 140–146 行）解释了一个精细的硬件保真点：**slot 索引必须等于 tid（tid-stable），不能把活跃线程紧凑到低 lane**，否则同一线程连续两次访问会因 tmask 变化而落到不同 lane，破坏 per-bank 仲裁的顺序保证。

然后看请求发送——「按 beat 发、分配 tag、记 pending」：

[lsu_unit.cpp:427-501 — process_request_step 的发送段：组 LsuReq、分配 tag、送 lmem_switch](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp#L427-L501) —— 第 433 行构造 `LsuReq`；第 439–440 行按是否 AMO 决定操作码（`amo_to_memop` 把 AMO 家族映射到 MemOp，或 load 取 `LD`、store 取 `ST`）；第 447–473 行遍历本 beat 的 lane，填地址、（store/AMO 时）把数据打包成 mem_block 并设 byteen；第 480–494 行：load/AMO 需要返回值，所以在 `pending_reqs` 分配 tag 并存 entry（store 不需要返回，跳过）；第 500 行真正 `ReqIn.send(lsu_req)`。

再看响应处理——「按 tag 找 trace、格式化数据、分包计数」：

[lsu_unit.cpp:218-269 — process_response_step：响应拼回与分包计数](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp#L218-L269) —— 第 231 行 `assert(lsu_rsp.data.at(lane) && "LOAD response must carry line payload");` 正是 u5-3 讲的那条不变量的运行时守卫——LOAD 响应必须带回整条 cache line。第 237–254 行按 `width`（LB/LH/LW/LD/LBU/LHU/LWU）做符号扩展或 NaN-box；第 256–258 行按 bytesel 把数据移到目的寄存器的正确字节位置。第 261–267 行：`entry.count -= mask.count()`，减到 0 才 `release(tag)` 并（若 eop）`output.send(trace, 1)`——这就是「分包计数释放」，呼应 u6-3 讲的 commit 用 `num_pkts` 延迟记分板释放。

最后看 fence 控制器——它实现 barrier 的「排空 LSU」语义：

[lsu_unit.h:46-74 — FenceController：engage/try_release 的锁存式 fence](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.h#L46-L74) —— `engage` 上锁、`try_release` 在「在途表空 且 输出能接受」时才解锁并把 trace 送出。LSU 的 `drained()`（[lsu_unit.cpp:87-101](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp#L87-L101)）报告所有 block 的队列与在途表都空；Core 的 `lsu_drained()`（[core.cpp:819-821](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L819-L821)）转发它，正是 SFU 里 `BAR` 指令判定「能否继续」的依据（见 4.4.3）。

> 关于 packed load：`PACKLB.F`/`PACKLH.F` 这类宏指令在译码时只产生一条，由 sequencer 用 `LsuUopGen`（[lsu_unit.cpp:30-72](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp#L30-L72)）展开成多个「无符号单字节/半字 load」uop，各自带不同的 `bytesel`，最后由 OpcUnit 的 writeback 按掩码 OR 合并——本讲只点到为止，详细见 u6-2/u6-3。

#### 4.3.4 代码实践

**实践目标**：跟踪一次 4 线程 warp 的 load，理解「分包」与「tag」如何配合。

**操作步骤**：

1. 设 `VX_CFG_NUM_THREADS=32`、`VX_CFG_NUM_LSU_LANES=4`（具体值查你的 `VX_config.toml`，或用 u2-l2 的 `gen_config.py --format cflags` 查看 `VX_CFG_NUM_LSU_LANES`）。
2. 在 [lsu_unit.cpp:402-415](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp#L402-L415) 看 beat 大小：`beat_n = min(NUM_LSU_LANES, remain_addrs)`。
3. 推演：32 个活跃线程的 load 会被切成多少个 beat？最后一个 beat 的 `is_eop` 何时为真？
4. 在 [lsu_unit.cpp:493](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp#L493) 看：每个 beat 都会 `pending_reqs.allocate(...)` 一个 entry，这些 entry 共享同一个 `trace` 指针，但 `count`/`eop` 不同。

**需要观察的现象**：一条 load 指令在 LSU 内部会**多次**进出 `pending_reqs`（每个 beat 一次），只有最后一个 beat 的响应回来（`entry.count==0 && entry.eop`）才触发 `output.send`。这解释了为什么 commit 必须用 `num_pkts` 来延迟记分板释放——缓存响应可能乱序到达。

**预期结果**：能画出「1 条 load → N 个 beat → N 个 pending entry → N 个响应 → 凑齐后才送 commit」的时序图。

#### 4.3.5 小练习与答案

**练习 1**：store 走的是「direct-commit」，不等响应。那 store 的 trace 是什么时候被 `output.send` 的？

**答案**：在 `process_request_step` 末尾，当 `state.remain_addrs == 0`（所有 beat 都发出）且 `direct_commit` 为真时，直接 `Outputs[b].send(trace)`（[lsu_unit.cpp:512-517](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp#L512-L517)）。store 不分配 pending entry，因为它不需要把数据读回来。

**练习 2**：为什么 `process_response_step` 要排在 `process_request_step` 之前，而 `ingest_inputs` 排在最后？

**答案**：为了保证 `req_queue` 是「1 拍流水级」而非同拍直通。如果先 ingest 再 dispatch，一条刚进来的 trace 就能在同一拍被发出去，等于凭空多出写带宽、把延迟塌缩成 0，破坏与 RTL 的 cycle 级 parity。先消费响应、再发请求、最后摄取，让每个阶段都跨拍。

---

### 4.4 SFU：单端口分派器与子单元路由

#### 4.4.1 概念说明

SFU（Special Function Unit）是 4 个单元里最特殊的一个。从外面看，它和 ALU/FPU 一样：一个 `FuncUnit<VX_CFG_NUM_SFU_BLOCKS>`，有 `Inputs`/`Outputs`。但进去一看，它的 `on_tick` **根本没有统一的 `execute()` + `latency_of()`** ——它是一个**单端口分派器（dispatch router）**：每拍从 `Inputs[b]` peek 一条 trace，看它的 `op_type`，把它路由到对应的子单元（sub-unit），子单元算完后再把 trace 汇聚回 `Outputs[b]`。

为什么这么设计？因为挂到 SFU 上的操作**异质性极大**：

- **WCTL**（warp 控制）：`WSYNC`（等之前指令退休）、`BAR`（屏障，要先排空 LSU）。
- **CSR**：读写控制状态寄存器。
- **DXA**：异步 DMA 拷贝（fire-and-wait）。
- **TEX**：纹理采样（异步，要走纹理流水线）。
- **OM**：输出合并（图形固定功能）。
- **RASTER**：光栅化（实际上是 push 模式，由 SFU 主动拉取）。
- **RTU**：光线追踪（异步，带回调陷阱）。

这些操作的延迟、同步性、是否要返回值都完全不同，硬塞进一个 `execute()` 会让代码不可维护。所以 SFU 选择「我只是一个路由器，真正的活由我拥有的子单元干」。子单元（`WctlUnit`、`CsrUnit`、`DxaUnit`、`TexUnit`、`OmUnit`、`RtuUnit`）是**普通的非 SimObject 辅助类**，由 SFU 用 `unique_ptr` 持有（见 [sfu_unit.h:123-185](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sfu_unit.h#L123-L185)）。

#### 4.4.2 核心流程

SFU 的 `on_tick` 分两大阶段：

```
on_tick():
  # 阶段 A：先排空各个【异步】子单元的响应通道
  drain rtu_rsp_in   (RTU 终止/回调响应)
  drain tex_rsp_in   (TEX 完成响应)
  drain raster_rsp_in 并主动拉取 fragment wave (RASTER push)

  # 阶段 B：PE switch —— 遍历每个 block，按 op_type 路由队头 trace
  for b in 0..NUM_SFU_BLOCKS:
    trace = Inputs[b].peek()
    switch (trace->op_type):
      case TEX:   提交到 tex_unit_->process()        # 异步，不立即送 output
      case OM:    多子像素循环提交到 om_unit_         # 异步
      case RTU*:  trace2/wait2/cb_ret/getw* 等        # 多为异步
      case WCTL:  wctl_unit_->process() → 设 resume_warp  # 同步
      case CSR:   csr_unit_->process()                # 同步
      case DXA:   dxa_unit_->process()                # 半异步
      default:    output.send(trace, latency_of=4); input.pop()
```

三类操作的完成路径不同，这是理解 SFU 的关键：

1. **同步操作**（WCTL/CSR/SETW 等）：当场 `process()` 算完，`output.send(trace, 4)`（SFU 的 `latency_of` 恒返回 4，见 [sfu_unit.cpp:72-74](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sfu_unit.cpp#L72-L74)）。
2. **异步 fire-and-wait 操作**（TEX/RTU/DXA）：trace 被**移交**给子单元（或它的协处理器核心），SFU **不立即** `output.send`，而是等响应通道（`tex_rsp_in`/`rtu_rsp_in`）回包后，在阶段 A 把结果填进 `trace->dst_data` 再 `output.send`。期间这条 trace 的输入槽位可能被「挂起」（如 RTU WAIT 会 park）。
3. **RASTER 是 push 模式**：根本没有 kernel 侧的 raster 操作码，SFU 主动向 `raster_req_out` 发请求拉取 covered-quad wave，攒满一个 warp 后由 scheduler 启动 fragment warp。

#### 4.4.3 源码精读

先看 SFU 头文件里那段点睛注释，它直接回答了「为什么 SFU 是分派器」：

[sfu_unit.h:46-54 — SFU 设计说明：单端口扇出再汇聚](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sfu_unit.h#L46-L54) —— 「SFU has a single dispatch port that fans out to per-op sub-units ... then gathers their results back to a single result port. Sub-units are plain non-SimObject helpers owned here.」并专门指出 TEX 走 fire-and-wait 路径：trace 交给 TexCore，直到 `tex_rsp_in` 回来才 forward 到 writeback。

接着看 SFU 持有的「对外 channel」——这些 channel 才是异步子单元与协处理器核心的通信通道（区别于 FuncUnit 固有的 Inputs/Outputs）：

[sfu_unit.h:61-108 — SFU 的对外请求/响应 channel](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sfu_unit.h#L61-L108) —— `dxa_req_out`、`tex_req_out`/`tex_rsp_in`、`om_req_out`、`raster_req_out`/`raster_rsp_in`、`rtu_req_out`/`rtu_rsp_in`，全部由 Cluster 绑定到对应的总线仲裁器（fan-in 到 TexCore/RasterCore/RtuCore）。这些 channel 都用 `VX_CFG_EXT_*_ENABLE` 条件编译——没启用图形/光追时根本不存在。

现在看 `on_tick` 的 PE switch 主循环——这是「按 op_type 路由」的实体：

[sfu_unit.cpp:317-323 — PE switch 主循环：peek 后按 op_type 分发](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sfu_unit.cpp#L317-L323) —— 注释「route to the matching sub-unit (WCTL / CSR / DXA / TEX / OM / RASTER) by op_type, gather to the single result port」一句话概括了 SFU 的全部职责。随后是一长串 `if (std::get_if<XxxType>(&trace->op_type))`（[sfu_unit.cpp:327](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sfu_unit.cpp#L327)、[390](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sfu_unit.cpp#L390)、[460](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sfu_unit.cpp#L460)…），每个分支就是一种 op_type 的路由。

看一个**同步**路由的例子——WCTL，它展示了 SFU 如何处理 warp 控制与 barrier 的特殊语义：

[sfu_unit.cpp:548-583 — WCTL/CSR/DXA 的同步路由 + warp 释放决策](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sfu_unit.cpp#L548-L583) —— 第 548–558 行是关键的结构性门控：`WSYNC` 要等「该 warp 之前的指令都退休」（`core_->has_pending_instrs`），`BAR` 要等「LSU 排空」（`core_->lsu_drained()`，正是 4.3.3 的 `drained()`）。第 561–573 行按 op_type 调用对应子单元的 `process()`。第 575–576 行同步发送（`latency_of` 恒为 4）。第 581 行 `trace->resume_warp = release_warp;` —— SFU 通过这个字段告诉 commit「这条 warp 控制指令是否要释放 warp」（屏障未到齐、wspawn 未完成、自禁用 warp 都不释放，由对应机制后续释放）。commit 侧的处理见 [core.cpp:707-709](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L707-L709)。

再看一个**异步**路由的例子——TEX 的 fire-and-wait：

[sfu_unit.cpp:378-382 — TEX 异步提交：不立即 send，等 tex_rsp_in 回来再 retire](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sfu_unit.cpp#L378-L382) —— `tex_unit_->process(trace, b)` 若返回 false 表示背压，`continue`（不 pop，下拍重试）；成功则 `input.pop()` 但**不** `output.send`——trace 被 TexCore 持有。它的写回发生在阶段 A 的 `drain tex_rsp_in`（[sfu_unit.cpp:202-235](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sfu_unit.cpp#L202-L235)），第 219 行把纹素填进 `trace->dst_data`，第 231 行 `output.send(trace, 2)`。这种「提交点 ≠ 写回点」是异步单元的标志，与 ALU/FPU 的「send 即完成」形成鲜明对比。

#### 4.4.4 代码实践

**实践目标**：回答本讲的核心设问——**SFU 为何是分派器而非单一执行单元**，并验证一个同步路由。

**操作步骤**：

1. 打开 [sfu_unit.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sfu_unit.cpp)，在 `on_tick` 的 PE switch 里数一下共有多少个 `if (std::get_if<...>(&trace->op_type))` 分支，每个分支对应哪类操作。
2. 对比 ALU 的 `execute()`（[alu_unit.cpp:89](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/alu_unit.cpp#L89)）——它是一个集中计算函数；而 SFU 的「计算」散落在各个 `*_unit_->process()` 子单元里。
3. 写一段话解释：如果硬要把 TEX（异步、要访问纹理缓存、可能几十拍）和 CSR（同步、1 拍）塞进同一个 `execute()`，会遇到什么麻烦？
4. （可选，待本地验证）用一个跑得过 `tests/regression` 的 kernel，开 `--debug=3`，在 trace 里找一条 CSR 写指令，观察它从 SFU input 到 output 是否间隔约 4 拍（`latency_of` 的返回值）。

**需要观察的现象**：SFU 的 `on_tick` 既要做「路由」，又要做「响应排空」（阶段 A），还要做「warp 释放决策」——三件事挤在一起，这正是分派器的复杂度所在，而非单一执行单元。

**预期结果**：能用一句话说清——「SFU 的本质是一个按 op_type 扇出到异构子单元、再汇聚回单端口的分派器；它把同步/异步、计算/控制、本核/跨核操作的差异，封装在各自子单元里」。

#### 4.4.5 小练习与答案

**练习 1**：SFU 的 `latency_of` 恒返回 4（[sfu_unit.cpp:72-74](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sfu_unit.cpp#L72-L74)）。这个 4 对 TEX/RTU 这类异步操作有意义吗？

**答案**：只对**同步路径**（WCTL/CSR/SETW/DXA 的提交确认）有意义——它们的 `output.send(trace, latency_of(trace))` 用到这个 4。对 TEX/RTU 这类异步操作，trace 的写回发生在阶段 A 的响应排空里，那里用的是 `output.send(trace, 2)`（如 [sfu_unit.cpp:231](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sfu_unit.cpp#L231) 与 [187](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sfu_unit.cpp#L187)），真正的耗时由协处理器核心（TexCore/RtuCore）决定，与这个 4 无关。

**练习 2**：`BAR`（屏障）指令为什么在 SFU 里要先检查 `core_->lsu_drained()`？

**答案**：`__syncthreads()` / `barrier(CLK_LOCAL_MEM_FENCE)` 的语义要求「屏障前的所有访存都对后续可见」。如果 barrier 在 LSU 还有在途 load/store 时就放行，后续线程可能读到旧值。所以 `BAR` 必须等 LSU 完全排空（队列空 + 在途表空）才允许继续——这正是 u4-3 讲的 barrier 语义在硬件上的落实点。

---

## 5. 综合实践

**任务**：设计一个「新增一个自定义同步 FuncUnit」的完整方案，把本讲四节串起来。

假设你要加一个极简的 `CryptoUnit`，实现一条 `xorhash rd, rs1`（对 rs1 的每个线程做 `rd = rs1 ^ (rs1>>>5) ^ 0x9e3779b9`，固定 3 拍延迟）。请按下列四层给出改动清单（**纸面设计，不真正改源码**）：

1. **SimX 层**（对应 4.1）：
   - 新增 `crypto_unit.h/.cpp`，让 `CryptoUnit : public FuncUnit<VX_CFG_NUM_CRYPTO_BLOCKS>`；
   - 实现 `on_tick`（套用 4.1.2 模板：peek → execute → `latency_of` → send → pop）；
   - 私有 `execute()`：遍历线程、tmask 门控、写 `dst_data`；
   - 私有 `latency_of()` 恒返回 3。
   - 在 `types.h` 的 `FUType` 枚举加 `CRYPTO`（参考 [types.h:176-185](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/types.h#L176-L185)）。

2. **Core 接线层**（对应 4.1.3）：
   - 在 [core.cpp:234-242](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L234-L242) 旁加一行 `func_units_.at((int)FUType::CRYPTO) = create_object<CryptoUnit>(...)`；
   - 同样为它建一个 Dispatcher（参考 [core.cpp:226-229](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L226-L229)）；
   - 注意 `func_units_`、`dispatchers_`、`fu_credits_`、`commit_arbs_` 的维度都依赖 `FUType::Count`，加枚举后这些容器会自动变宽——检查是否会引入未处理的 `case`（如 [core.cpp:672-681](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L672-L681) 的 stall 统计 switch）。

3. **译码层**（对应 u6-2，本讲前置）：
   - 在 `decode.cpp` 让新指令的 `fu_type = FUType::CRYPTO`、`op_type = CryptoType::XORHASH`（参考现有 [decode.cpp:493/613](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/decode.cpp#L493) 的写法）。

4. **配置层**（对应 u2-1/u2-2）：
   - 在 `VX_config.toml` 加 `NUM_CRYPTO_BLOCKS`、`EXT_CRYPTO_ENABLE` 等键；
   - 因为改了 toml，按 u1-l3/u2-l1 的纪律**必须重新 `configure`**。

**交付物**：一份四层改动清单 + 一段「为什么我的 CryptoUnit 像 ALU 而不像 LSU/SFU」的说明（答：定长延迟、无内部状态、同步完成 → 纯 channel 延迟模型，与 ALU 同构；不像 LSU 因为无在途状态，不像 SFU 因为不分派）。

> 提示：真实场景下的自定义加速器扩展，详见 u14-l3。

## 6. 本讲小结

- **FuncUnit 是统一骨架**：`FuncUnitBase` 做类型擦除、`FuncUnit<NUM_BLOCKS>` CRTP 模板固化「每 lane 一对 Inputs/Outputs channel + `on_tick` 钩子」，Core 用 `func_units_[(int)fu_type]` 统一持有 ALU/FPU/LSU/SFU/TCU 五类异构单元。
- **ALU/FPU 是「纯 channel 延迟」单元**：内部无状态，计算结果一次性算完，延迟由 `output.send(trace, latency_of)` 的 channel delay 承载——这正是 u5-l1「channel 就是流水线」的落地。
- **延迟可变但数据无关**：ALU 的 DIV 是 \( \text{XLEN}+2 \) 拍，FPU 的 FMA 是 \( \text{FMA\_LATENCY}+2 \) 拍，延迟随操作类型变，但对每条具体指令是编译期可定的常量。
- **LSU 是带内部状态的例外**：访存延迟数据相关，必须用 AGU 算地址 + `pending_reqs` 在途请求表追踪分包响应，store 直写、load/AMO 等响应；fence 控制器实现 barrier 的排空语义。
- **SFU 是分派器不是执行单元**：单端口按 `op_type` 扇出到 WCTL/CSR/DXA/TEX/OM/RASTER/RTU 等子单元再汇聚；同步操作当场 send，异步操作（TEX/RTU）fire-and-wait，响应回包后才写回。
- **`trace->tmask`（发射快照）是所有 FU 的统一依据**：execute 与 writeback 都按它工作，不能用实时 `warp.tmask`，否则发散控制流会让 lane 拿到陈旧结果。

## 7. 下一步学习建议

本讲完成了 SimX 核心流水线 Execute 级的讲解。接下来推荐：

- **u7-1 / u7-2（RTL 顶层与核心流水线各级）**：到 `hw/rtl/core` 里找到 `VX_alu_unit.sv`、`VX_lsu_unit.sv`、`VX_sfu_unit.sv`，对照本讲看 RTL 如何实现同样的 ALU/LSU/SFU——你会看到 SimX 的 `latency_of` 在 RTL 里变成真实的多级流水线寄存器。
- **u7-4（SimX↔RTL model parity）**：本讲反复强调的「channel delay 必须与 RTL 周期对齐」「LSU 1 拍流水级不能塌缩」正是 model_parity 门控的具体落实点，建议接着读。
- **u8-3 / u8-4（访存合并与 LSU 流水线）**：本讲的 LSU 只讲到 AGU 与在途表；coalescer 如何把 warp 内多线程的 load 合并成更少的 cache line 请求，以及 RTL 的 `VX_lsu_agu.sv`/`VX_lsu_slice.sv` 细节，在 U8 展开。
- **u9-l1（张量核 TCU）**：第 5 类单元 TCU 本讲只点到，它的 `tcu_unit.cpp` 同样继承 `FuncUnitBase` 但有自己的 sequencer（`TcuUopGen`），值得专门一讲。
