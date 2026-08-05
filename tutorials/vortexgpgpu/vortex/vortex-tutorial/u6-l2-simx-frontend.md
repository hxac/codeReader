# 取指、解压、译码与微操作展开

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清一条指令在 SimX 前端走过的完整路径：**调度器选 warp → fetch 取指令字 →（可选）RVC 解压 → decode 译成 `Instr` → 写入 ibuffer**，并能指出每个阶段在 `core.cpp` 哪个函数里发生。
- 解释 **RVC（RISC-V 压缩扩展）** 的工作模型：为什么 icache 永远按 4 字节对齐取，而把 2 字节对齐的难题留给一个专门的 **`Decompressor`** 阶段；一条 16 位压缩指令是如何被 `rvc_decompress` 还原成 32 位标准编码的。
- 看懂 **`Decoder::decode`** 如何把一个 32 位指令字切出 opcode/funct/rd/rs1/rs2 等字段，填进一个 `Instr` 对象，并区分它属于哪个功能单元（ALU/FPU/LSU/SFU/TCU）。
- 解释 **宏指令（macro-op）与微操作（micro-op / uop）** 的区别：为什么一条 `WGMMA`（张量核矩阵乘）或一条 packed load 在译码阶段先被打成「一条宏指令」，再由 **`Sequencer`** 在发射阶段逐条展开成多个微操作送入流水线。

本讲是 SimX 核心流水线的第二站，紧接 u6-l1（warp 调度器与 CTA 派发）。u6-l1 讲清了「谁在本周期被发射」；本讲讲清「被发射的那条 warp 的 PC 指向的指令，是如何变成一个可执行对象的」。后续 u6-l3（发射、记分板与操作数收集）会消费本讲产出的 `instr_trace_t`。

## 2. 前置知识

在进入源码前，先建立四组直觉。RVC 部分在 `docs/designs/compressed_instruction_support.md` 有权威说明，宏/微操作部分与 u4-l2（SIMT 控制指令）的「指令即 custom0 槽位」一脉相承，这里只做承接。

**（1）RVC 是什么：用 16 位编码最常见的 32 位指令。** 标准 RISC-V 指令固定 32 位（4 字节）。但统计分析发现，程序里大量指令（如 `addi`、`lw`、`sw`、`beq`、`j`）只用到了很小的立即数、且寄存器集中在 `x8..x15` 这 8 个上。RVC（C 扩展）给这些「热指令」分配了 16 位（2 字节）的紧凑编码，从而提升指令密度、减少 icache 占用。代价是：取指现在必须支持「2 字节对齐」，因为一条指令可能从任意偶数地址开始。

判断一条指令是不是 RVC，靠的是指令字的**最低 2 位**：

- `low2 == 0b11` → 32 位标准指令。
- `low2 != 0b11` → 16 位压缩指令（三个「象限」0/1/2，分别对应 low2 = 0b00/01/10）。

Vortex 的 RVC 支持由 `VX_CFG_EXT_C_ENABLE` 开关控制（默认关闭）。**关闭时前端走直通路径，开启时多出一个 `Decompressor` 阶段**——这是本讲 4.1 节的核心。

**（2）`Instr` 对象：译码的产物。** SimX 不直接在位级上执行指令，而是先把 32 位指令字「翻译」成一个 C++ 对象 `Instr`。这个对象携带了执行所需的全部语义信息：属于哪个功能单元（`fu_type`）、具体哪种操作（`op_type`，如 `AluType::ADD`）、源/目的寄存器（`rsrc_`/`rdest_`）、立即数（`args_`），以及两个本讲的关键标志位：

- `is_macro_op_`：这是一条「需要被展开成多条微操作」的复合指令吗？
- `is_wstall_`：这条指令译码后是否要**暂停取指**（让前端停下来等它处理完）？

整个译码过程就是「按位切字段 → 填进 `Instr`」。

**（3）宏指令 vs 微操作：一条指令，多次执行。** 大多数指令（`add`、`lw`、`fmul`）是一对一的——译码出一条 `Instr`，发射一次，执行一次。但 Vortex 有几类「大」指令：

- **TCU 的 `WMMA` / `WGMMA`**（张量核分块矩阵乘，见 u9-l1）：一条 `WGMMA` 在硬件上对应几十次 MMA（乘加）节拍，每次读写不同的寄存器。
- **packed load**（`vx_packlb_f` / `vx_packlh_f`）：一条指令从内存读多个字节/半字，打包进一个浮点寄存器。
- **RTU 的窗口读**（`GETWF`/`GETW`，多槽）：一次读连续多个寄存器槽。

这些指令在译码阶段只产生**一条宏指令**（`is_macro_op_=true`），但在发射阶段必须被**展开成 N 条微操作**，每条微操作像普通指令一样独立经过记分板、操作数收集、派发到功能单元、写回。负责展开的就是 **`Sequencer`**（4.3 节）。

**（4）SimObject 与流水线级。** u5-l1 讲过，SimX 里每个模块都是 `SimObject`，靠 `on_tick` 每周期被驱动。`Core` 的每周期 `tick()` 顺序是 `schedule → fetch → decode → issue → ... → commit`（见 u6-l1）。本讲覆盖其中 `fetch` 和 `decode` 两级，以及 `issue` 级里 `Sequencer` 的接入点。`Decoder`、`Decompressor`、`Sequencer` 都是按 warp 或按 core 创建的 SimObject。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [sim/simx/decompressor.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/decompressor.h) | 定义 `DecompResult`（解压结果）、无状态函数 `rvc_decompress`、`Decompressor` SimObject 类及内部 `RvcSlot` 状态 |
| [sim/simx/decompressor.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/decompressor.cpp) | 本讲主角一：RVC 解压逻辑（`rvc_decompress` 的三象限展开）+ 跨字取指 FSM（`on_icache_rsp`/`pick_request`） |
| [sim/simx/decode.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/decode.h) | 定义无状态译码器 `Decoder` 类 |
| [sim/simx/decode.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/decode.cpp) | 本讲主角二：`Decoder::decode`——按 opcode 大 switch 把 32 位字译成 `Instr`，标记 RVC 与宏指令 |
| [sim/simx/sequencer.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sequencer.h) | 定义 `Sequencer` 类与内部 `State`（当前宏指令的展开进度） |
| [sim/simx/sequencer.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sequencer.cpp) | 本讲主角三：`Sequencer::get`/`advance`——按功能单元绑定 uop 生成器，逐条产出微操作 |
| [sim/simx/core.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp) | 把三者串起来的流水线宿主：`fetch()`、`decode()`、`issue()` 三级函数 |
| [sim/simx/tcu/tcu_unit.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp) | `TcuUopGen::uop_count` / `get`——WGMMA/WMMA 宏指令展开成多少微操作、每条微操作的寄存器布局 |
| [sim/simx/lsu_unit.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp) | `LsuUopGen::uop_count` / `get`——packed load 宏指令展开成 2 或 4 条单元素 load 微操作 |
| [docs/designs/compressed_instruction_support.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/compressed_instruction_support.md) | RVC 设计文档：RTL/SimX 双实现的对齐说明，是 4.1 节的权威依据 |

## 4. 核心概念与源码讲解

### 4.1 RVC 解压器（decompressor）—— 16 位如何还原成 32 位

#### 4.1.1 概念说明

RVC 解压要回答的核心问题是：**「前端取回来一个 4 字节的字，里面到底装了几条指令、从哪个字节开始？」**

最朴素的做法是让 icache 支持 2 字节对齐取指。但 Vortex 选择了一条更省事的路（设计文档称之为「word-aligned fetch + a decompressor stage」）：**icache 永远按 4 字节对齐响应**，把「这个 4 字节字里指令到底占 2 字节还是 4 字节、会不会跨到下一个 4 字节字」这个麻烦问题，交给 icache 响应与译码之间的一个专门阶段——`Decompressor`——来处理。

[设计文档 §1：word-aligned fetch + 专门的解压器阶段](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/compressed_instruction_support.md#L14-L24)

这样做的好处：icache 和它下游的数据通路完全不用改，只在「拿到 cache line 之后、译码之前」插一层逻辑。坏处是：当一个 32 位指令恰好横跨两个 4 字节字（即它的低半字在前一个字的高 16 位、高半字在下一个字的低 16 位）时，解压器必须能发起一次「补取（refetch）」。这个跨字（cross-word）情形是 4.1 节最微妙的部分。

#### 4.1.2 核心流程

`Decompressor` 拿到一条 icache 响应（一个 cache line + 对应的 trace）后，要分类处理。设 `PC` 是这条指令的地址，从 line 里取出地址对齐到的那个 4 字节字 `word`，则一个 4 字节字内可能出现的情形只有三种：

```
情形 A：低 2 位 == 0b11（PC 落在 4 字节边界，是完整 32 位指令）
  → trace->code = word，直接送译码。

情形 B：低 2 位 != 0b11（是 16 位 RVC，占半个字）
  → 看 PC[1] 决定取低半字还是高半字：
      PC[1]==0 → hword = word 的低 16 位
      PC[1]==1 → hword = word 的高 16 位
  → trace->code = hword 零扩展到 32 位，送译码（译码器自己再展开）。

情形 C：PC[1]==1 且低 2 位 == 0b11（一条 32 位指令从「高半字」开始 → 跨字！）
  → 当前的 word 只含这条指令的低 16 位（高半字），
    高 16 位在下一个 4 字节字里。
  → 缓存低半字，把 trace 排进 refetch 队列，发一次 PC+4 的补取；
    下一次响应回来，把新字的低 16 位拼上缓存，得到完整 32 位字。
```

注意一个关键设计：**解压器不自己调用 `rvc_decompress` 把 16 位展成 32 位后再往下传，而是把「16 位半字零扩展」原样放进 `trace->code` 往下送**，由下游的 `Decoder` 在译码入口处自己检测 `code[1:0]` 并调用 `rvc_decompress`。这样做让解压器只关心「把正确的原始比特放进 `trace->code`」，而「16→32 的语义展开」集中在译码器一处。下面这段代码注释把这个分工说得很清楚：

[on_icache_rsp 文档注释：解压器只放原始比特，RVC 展开由译码器内部做](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/decompressor.h#L67-L76)

`refetch_queue_` 的优先级也很重要：**跨字补取必须排在所有新取指之前**，否则一个等待补取的 warp 会被别的 warp 的新请求饿死。`pick_request` 实现了这个优先级。

#### 4.1.3 源码精读

先看解压的「纯函数」部分 `rvc_decompress`。它的输出是一个三元组：

[DecompResult 结构 — 展开后的 32 位编码、消耗字节数（2 或 4）、是否非法](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/decompressor.h#L26-L30)

`rvc_decompress` 的整体骨架是「先判是不是 32 位，否则按象限（quadrant）和 funct3 大 switch」。32 位快速通道在最前面：

[rvc_decompress 入口：low2==0b11 直接返回原字、size=4](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/decompressor.cpp#L71-L79)

RVC 展开的本质是「把 16 位编码里的零散比特，重新排列成等价的 32 位 RV32I 编码」。为此文件顶部定义了一组装配宏 `ENCI/ENCR/ENCS/ENCU/ENCUJ/ENCB`，分别对应 I 型、R 型、S 型、U 型、J 型、B 型的 32 位编码布局。它们就是把字段按 RV32I 的位位置拼起来：

[ENCI/ENCR/ENCS/ENCU —— 按位拼出 32 位 RV32I 编码的装配函数](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/decompressor.cpp#L34-L47)

还有一个 RVC 专属的寄存器映射：压缩指令用 3 位编码寄存器（`rd'`），它映射到 `x8..x15` 这 8 个「压缩寄存器」。`rcp(r3) = 8 + r3` 就是这个映射：

[rcp —— 压缩寄存器 rd'(3位) → x8..x15](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/decompressor.cpp#L30-L31)

举一个具体例子就能看懂整个 switch 的套路。象限 0、funct3=0b000 的 `C.ADDI4SPN`（给栈指针 `x2` 加一个非零立即数）。代码先从 16 位字里把分散的立即数位抠出来、按正确顺序拼成 `nzuimm`，再用 `ENCI` 拼成等价的 `ADDI rd', x2, nzuimm`：

[C.ADDI4SPN → ADDI rd', x2, nzimm 的展开（典型 RVC 套路）](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/decompressor.cpp#L93-L98)

整个三象限 switch（Q0/Q1/Q2）都是同一个套路：切比特、拼立即数、调装配宏。其中象限 2 的 `funct3=0b100` 是一组控制流指令（`C.JR`/`C.JALR`/`C.EBREAK`/`C.MV`/`C.ADD`），值得一看，因为它靠 `rd`、`rs2`、`bit[12]` 三个字段组合分叉：

[象限 2 funct3=0b100：C.JR/C.JALR/C.EBREAK/C.MV/C.ADD 的分叉](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/decompressor.cpp#L332-L361)

注意文件里有大量 `#ifdef VX_CFG_XLEN_64` —— 同一个 funct3 在 RV32C 和 RV64C 下含义不同（例如象限 1 funct3=0b001 在 RV64C 是 `C.ADDIW`，在 RV32C 是 `C.JAL`）。这是 RISC-V C 扩展本身的特性，不是 Vortex 的发明。

[XLEN 分叉示例：象限 1 funct3=0b001，RV64C=C.ADDIW / RV32C=C.JAL](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/decompressor.cpp#L178-L194)

如果 16 位模式保留或未实现，函数设 `illegal=true` 并打印告警（注意它**不 abort**，由调用方断言决定如何处理）：

[非法 16 位模式：设标志并打印告警](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/decompressor.cpp#L391-L394)

---

接下来看 `Decompressor` 这个 **SimObject**——它持有「跨字补取」的状态机。核心是每个 warp 一个 `RvcSlot`，记录这个 warp 是否正在等一个跨字 32 位指令的第二半：

[RvcSlot —— 每 warp 的跨字状态：是否需要第二半、缓存低半字、原始 PC](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/decompressor.h#L83-L87)

`on_icache_rsp` 是状态机的本体，对应 4.1.2 列出的三种情形。读它时先看「是不是补取回来」的分支（`rvc.needs_second`），再看普通情形里 `PC[1]` 的三分叉：

[on_icache_rsp —— 分类 icache 响应：RVC 半字 / 对齐 32 位 / 跨字补取](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/decompressor.cpp#L444-L483)

最关键的几行是跨字分支——把当前字的高半字缓存进 `rvc.low_half`、标记 `needs_second`、把 trace 推进 refetch 队列，并返回 `false`（表示「这次还不产出可译码的指令」）：

[跨字分支：缓存低半字 + 入 refetch 队列 + 返回 false](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/decompressor.cpp#L476-L482)

而 `pick_request` 实现了「补取优先于新取指」的调度——只要 refetch 队列非空，就先排空它；否则才取 `fetch_latch` 里调度器刚推进来的新 trace：

[pick_request —— refetch 队列优先，否则取 fetch_latch 头部 trace](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/decompressor.cpp#L414-L428)

最后，这套机制在 `core.cpp` 的 `fetch()` 里被驱动。看 `#ifdef VX_CFG_EXT_C_ENABLE` 分支：响应端调 `decompressor_->on_icache_rsp`，请求端调 `decompressor_->pick_request`，发送成功后调 `commit_request` 弹出 refetch 队列（新取指才弹 `fetch_latch`）：

[core.cpp fetch() —— RVC 开启时的请求/响应驱动](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L359-L404)

对照看 `#else` 分支（RVC 关闭）：没有解压器，直接从 line 里 `memcpy` 4 字节进 `trace->code`，因为指令一定是 4 字节对齐的 32 位：

[RVC 关闭时的直通路径：直接 memcpy 4 字节](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L364-L372)

#### 4.1.4 代码实践

**实践目标**：亲手验证「最低 2 位决定指令长度」这条 RVC 铁律，并观察一条 RVC 指令被展开成什么 32 位编码。

**操作步骤**：

1. 在仓库根目录找到 `sim/simx/decompressor.cpp`，阅读 `rvc_decompress` 中象限 0、funct3=0b010 的 `C.LW` 展开逻辑（约 L107-L113）。它把一条 `C.LW rd', offset(rs1')` 展开成 `LW rd', offset(rs1')`。

2. 现在手动模拟一个输入：假设有一条 `C.LW` 指令，其 16 位编码是 `0x4112`（这是示例值，用于练习读位布局，不是项目原有数据）。请你在纸上：
   - 算出 `quadrant = 0x4112 & 0x3`（应为 `0b10`？注意——实际上 `0x4112 & 0x3 = 0`，所以象限 0，你应据此判断它落在哪个 case）。
   - 算出 `funct3 = (0x4112 >> 13) & 0x7`。
   - 对照源码确认它命中哪一条 case，并写出展开后的 32 位 `ENCS`/`ENCI` 调用。

3. 若想看真实指令流，可在 `build/` 目录用 `--driver=simx` 跑一个 RVC 程序（需开启 `VX_CFG_EXT_C_ENABLE`），并用 `--debug` 生成 trace。设计文档指出 SimX 在 `decode.cpp` 入口检测 RVC 并展开：

[设计文档 §3：SimX 在 decode 入口检测 RVC、PC 按 +2/+4 推进](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/compressed_instruction_support.md#L57-L69)

**需要观察的现象**：trace 里相邻两条指令的 PC 增量可能是 2（RVC）也可能是 4（标准）。RVC 测试由 CI 的 `rvc()` job 覆盖（`run-simx-32c` / `run-rtlsim-32c`）。

**预期结果**：你能从一条 16 位编码手工算出它的 quadrant、funct3，并说出它展开成哪条 32 位指令。

**注意**：本实践以源码阅读和手工位运算为主；是否能在你的环境跑通 RVC 程序取决于工具链与配置，若无法运行，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Decompressor` 要把「补取」放在比「新取指」更高的优先级？如果反过来会怎样？

> **答案**：跨字 32 位指令的低半字已经缓存在 `RvcSlot` 里，warp 处于「卡住等第二半」的状态。如果不优先补取，别的 warp 的新请求会一直占着 icache 端口，这个等待的 warp 可能永远拿不到第二半，形成饥饿。优先补取保证跨字指令尽快完成、释放 warp。

**练习 2**：`rvc_decompress` 检测到非法 16 位模式时只设 `illegal=true` 并打印告警，没有 `abort`。这个「非法」最终在哪里被挡住？

> **答案**：在 `decode.cpp` 的译码入口。译码器调 `rvc_decompress` 后紧跟一句 `assert(!r.illegal && "illegal RVC encoding")`（见 4.2.3）。也就是说，解压器只负责「判定 + 上报」，译码器负责「断言拦截」。

---

### 4.2 RISC-V 译码器（decode）—— 32 位字如何变成 Instr

#### 4.2.1 概念说明

译码器要回答的核心问题是：**「这一个 32 位指令字，到底要做什么、需要哪些寄存器、由哪个功能单元执行？」**

译码器是一个**无状态**的翻译机——给它一个 32 位 `code` 和一个 `uuid`，它返回一个填好的 `Instr::Ptr`。无状态意味着同一个输入永远得到同一个输出，没有跨指令的依赖（跨指令的状态在 warp 状态机和 ibuffer 里，不在译码器里）。

[decode.h：无状态 ISA 译码器](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/decode.h#L22-L36)

译码器是 SimX 里「ISA 语义的归属地」。u5-l3 讲过，v3 架构取消了中央「上帝对象」，让功能语义与计时同居各单元；但**指令的「身份判定」**（它是 ALU 还是 LSU？读哪些寄存器？是不是宏指令？）集中在译码器。译码器不做执行，只做「分类 + 填字段」。

#### 4.2.2 核心流程

`Decoder::decode(code, uuid)` 的骨架是一个按 `Opcode`（指令低 7 位）组织的大 switch：

```
1. 先判 RVC：若 code 低 2 位 != 0b11，调 rvc_decompress 展开成 32 位。
2. 切出所有字段：opcode(7) / rd(5) / rs1,rs2,rs3(5) / funct2,3,5,6,7。
3. new 一个 Instr 对象（默认 fu_type=ALU）。
4. 大 switch (opcode)：
     LUI/AUIPC → ALU 的 LUI/AUIPC
     R/I       → ALU 算术逻辑 或 Mdv 乘除
     B/JAL/JALR→ 分支（标 is_rvc，设 wstall）
     L/FL/S/FS → LSU 的 LOAD/STORE
     FENCE/AMO → LSU
     SYS       → SFU 的 CSR 或 ALU 的 ECALL/MRET
     FCI/FMADD → FPU
     EXT1      → 自定义扩展（WCTL/WCTL、TCU、DXA、packed load）
     EXT2      → 自定义扩展（WGATHER、TEX、OM、RTU 窗口）
5. 返回填好的 Instr。
```

译码器在填字段时做三件影响后续流水线的事：

- **设 `fu_type`**：决定这条指令派发到哪个功能单元（ALU/FPU/LSU/SFU/TCU）。
- **设源/目的寄存器**：记分板（u6-l3）据此判断冒险。
- **设 `is_wstall` / `is_macro_op`**：`is_wstall=true` 会让 decode 阶段暂停该 warp 的取指；`is_macro_op=true` 会让发射阶段的 `Sequencer` 把它展开成多条微操作。

#### 4.2.3 源码精读

译码入口的前几行是本讲的「枢纽」之一：先做 RVC 检测与展开，再切字段。注意它**复用** 4.1 节的 `rvc_decompress`，把解压逻辑集中在那一处：

[decode 入口：检测 RVC（code&0x3 != 0x3）→ 调 rvc_decompress → 断言非非法 → 用展开后的 32 位继续](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/decode.cpp#L472-L479)

字段提取是一连串按位移位 + 掩码，对应 RISC-V 的标准编码布局（rd 在 bit[11:7]，rs1 在 bit[19:15]，rs2 在 bit[24:20] 等）：

[切出 opcode/funct/rd/rs1/rs2/rs3 等字段](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/decode.cpp#L480-L491)

看一个最经典的算术译码——`Opcode::R` / `Opcode::I`（寄存器型 / 立即数型 ALU 指令）。这段代码先区分「是不是乘除（funct7 低位为 1）」，再按 funct3 选 ADD/SUB/SLL/SLT/...。它会同时把 `rd` 设为目的寄存器、`rs1`（和 `rs2`，非立即数时）设为源寄存器：

[R/I 型 ALU 译码：区分乘除与算术逻辑，按 funct3 选具体操作](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/decode.cpp#L507-L573)

RVC 信息（`is_rvc`）被「搭车」塞进分支指令的参数里。这一点设计文档专门强调过：与其给每个流水线结构都加一个 size 字段，不如复用分支指令已有的 `op_args.br`，只在那里存一个 `is_rvc` 位。于是 `B`/`JAL`/`JALR` 的译码都把 `is_rvc` 传进 `IntrBrArgs`：

[B 型分支译码：is_rvc 搭车进 IntrBrArgs（链接地址后续按 +2/+4 算）](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/decode.cpp#L574-L586)

这个 `is_rvc` 会在 decode 阶段被用来推进 PC。回到 `core.cpp` 的 `decode()`：它从 `trace->code` 的低 2 位重新判一次 RVC，调 `advance_pc(trace, is_rvc ? 2 : 4)`，与 RTL「PC 在 decode 级按 is_rvc 推进」一一对应：

[core.cpp decode()：按 is_rvc 推进 PC +2/+4，非停顿指令立即 resume](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L482-L488)

`advance_pc` 本体在调度器里，除了 `warps_[wid].PC += inc`，还带一个 `trap_epoch` 守卫——丢弃在最近一次异步 trap 之前取的陈旧 trace，避免把 PC 越过 trap 设的 `mtvec`（u6-l1 已展开）：

[advance_pc：推进 warp PC，并用 trap_epoch 丢弃陈旧 post-trap fetch](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/scheduler.cpp#L281-L291)

---

译码器还承担「识别宏指令」的职责。看 `Opcode::EXT1`、`funct7=2`（TCU）这一支。当 `funct3=0` 时是 `WMMA`，`funct3=1`（在 `VX_CFG_TCU_WGMMA_ENABLE` 下）是 `WGMMA`。两者都调用 `instr->set_macro_op()` 标记为宏指令，并调用 `set_wstall(true)` 暂停取指：

[TCU 译码：WMMA/WGMMA 标记为宏指令 + wstall，留给 sequencer 展开](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/decode.cpp#L895-L932)

具体到 `WGMMA` 那一段，它从 `rd`/`rs1`/`rs2` 里抠出数据格式（`fmt_d`/`fmt_s`）、是否稀疏、`cd_nregs`（结果寄存器组数）、是否从共享内存取 A，填进 `IntrTcuArgs`，然后设宏指令：

[WGMMA 译码：抠出格式/稀疏/cd_nregs/is_a_smem，填 IntrTcuArgs，set_macro_op](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/decode.cpp#L906-L916)

另一类宏指令是 packed load（`funct7=4`）。它把一条「读 4 个字节 / 2 个半字并打包进一个浮点寄存器」的指令也标成宏指令，留给 LSU 的 uop 生成器展开：

[Load/Store Packing 译码：vx_packlb_f/vx_packlh_f 标记宏指令 + wstall](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/decode.cpp#L933-L951)

`set_wstall(true)` 的后果在 decode 阶段立刻显现：`trace->fetch_stall = instr->is_wstall()` 把它读出来，于是这条 warp 不会被 `resume`，取指暂停，直到宏指令在发射端被完全展开（见 4.3.3 的 resume 时机）：

[core.cpp decode()：trace->fetch_stall = instr->is_wstall()，停顿指令不立即 resume](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L475-L480)

最后，文件顶部的 `op_string` 函数（及其 `operator<<`）是 trace 打印的反向映射——把一个 `Instr` 的 `op_type` 变回人类可读的字符串（如 `FADD.S`、`WGMMA`、`TMC`）。它和 `decode` 是一对「正反映射」，调试时极其有用：

[operator<< — 把 Instr 反映射成可读字符串，用于 trace 打印](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/decode.cpp#L434-L462)

#### 4.2.4 代码实践

**实践目标**：跟踪一条普通算术指令在 `Decoder::decode` 里的字段填充路径，确认「译码器只填字段、不执行」。

**操作步骤**：

1. 打开 `sim/simx/decode.cpp`，定位 `case Opcode::R:` / `case Opcode::I:`（L507-L573）。

2. 假设译码器收到 `code = 0x002081b3`（示例值）。这是标准的 `add x3, x1, x2`（示例代码，非项目原有）。请你在纸上按源码的字段提取公式算出：
   - `opcode = (code >> 0) & 0x7F` → 应为 `0b0110011`（R 型）。
   - `rd = (code >> 7) & 0x1F` → `x3`。
   - `rs1 = (code >> 15) & 0x1F` → `x1`。
   - `rs2 = (code >> 20) & 0x1F` → `x2`。
   - `funct3 = (code >> 12) & 0x7` → `0`，`funct7` → `0`。
   - 对照源码确认它命中 `case 0: ... AluType::ADD`，且因为 `is_imm=false`，会 `set_src_reg(1, rs2, Integer)`。

3. 在 `build/` 目录用 `./ci/blackbox.sh --driver=simx --app=demo` 跑通后，用 `--debug` 打开 trace，在输出里搜 `Instr:` 开头的行（由 `core.cpp` decode 阶段的 `DP(1, "Instr: " ...)` 打印）。你会看到每条指令的译码字符串，验证你的手工计算。

**需要观察的现象**：trace 里每条 `Instr:` 行的助记符和寄存器编号，与你按位算出的一致。

**预期结果**：你能手工从 32 位编码算出 opcode/rd/rs1/rs2/funct3，并说出它命中 switch 的哪一支、最终 `op_type` 是什么。

#### 4.2.5 小练习与答案

**练习 1**：译码器为什么是「无状态」的？如果它有状态（比如缓存上一条指令），会破坏什么？

> **答案**：译码器只做「位 → 语义」的纯函数映射，没有跨指令依赖。无状态使它天然可重入、可被多个 warp 共享（Vortex 每个 core 只有一个 `Decoder`，被所有 warp 复用）。若有状态，不同 warp 的指令会互相污染，且无法与 RTL「组合逻辑译码」的语义对齐，破坏 model_parity。

**练习 2**：为什么 `is_rvc` 这个 1 位信息只塞进分支指令的 `IntrBrArgs`，而不是给 `instr_trace_t` 或 ibuffer 加一个独立字段？

> **答案**：因为 `is_rvc` 只在两个地方被消费——PC 推进（`+2`/`+4`，在 decode 级就处理完）和分支链接地址的计算（`to_fullPC(pc) + (is_rvc?2:4)`）。前者根本不需要传递，后者天然在分支指令上。给每个流水线结构都加 size 字段是过度设计——设计文档明确称这条路线「leaner」（更精简）。

**练习 3**：一条 `WGMMA` 在译码阶段被标了 `is_wstall(true)`。如果忘了设这个标志，发射端会发生什么异常？

> **答案**：`is_wstall` 控制取指是否暂停。若没设，宏指令进入 ibuffer 后，取指会继续往 ibuffer 塞后续指令；而 `Sequencer` 还在一条一条地展开这条 `WGMMA` 的微操作，ibuffer 头部被宏指令长期占据，后续指令无法发射，流水线会乱序甚至死锁。`set_wstall(true)` 保证宏指令展开期间前端停住。

---

### 4.3 微操作展开器（sequencer）—— 宏指令如何裂成多条 uop

#### 4.3.1 概念说明

`Sequencer` 要回答的核心问题是：**「译码给出的一条宏指令，如何变成 N 条可独立经过记分板、派发、写回的微操作？」**

为什么需要展开，而不是让功能单元「一口气」执行一条 `WGMMA`？因为 SimX 的执行模型是「每条微操作像普通指令一样独立流过流水线」——它要有自己的 uuid（供记分板追踪）、自己的源/目的寄存器（供冒险检测）、自己的写回节拍。一条 `WGMMA` 可能要写 8/16/32 个结果寄存器、读几十个源寄存器，如果当成一条指令，记分板和写回通路根本无法表达「部分寄存器已就绪」。展开成 N 条微操作后，每条只写一两个寄存器，整套冒险/写回机制可以原样复用。

`Sequencer` 的设计有三个要点：

- **按 warp 创建**：每个 warp 一个 `Sequencer` 实例（在 `core.cpp` 构造时创建），因为展开进度是 per-warp 的状态。
- **按功能单元绑定生成器**：宏指令的 `fu_type` 决定用哪个 uop 生成器（`LsuUopGen` / `TcuUopGen` / `RtuUopGen`）。
- **幂等的 `get` + 显式的 `advance`**：`get(trace)` 返回「当前该发射的微操作」，重复调返回同一个（缓存）；`advance()` 才推进到下一条。这让记分板检查和实际派发可以分别调 `get` 而不重复生成。

[sequencer.h：每 warp 微操作展开器，简单指令直接透传](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sequencer.h#L36-L47)

#### 4.3.2 核心流程

`Sequencer` 用一个 `State` 结构记录「当前宏指令展开到第几条」。每周期发射阶段对 ibuffer 头部指令的处理：

```
1. uop_trace = seq->get(trace)          // 取当前微操作（幂等）
2. scoreboard->in_use(uop_trace)?        // 检查这条微操作的冒险
     是 → stall，下周期再来（get 还是返回同一条）
     否 → ready
3. 仲裁选中一个 ready 的 warp，再 get 一次取微操作
4. 派发到操作数收集 → 记分板 reserve（若 wb）
5. seq->advance()                        // 推进到下一条微操作
     若返回 true（全部微操作发完）→ pop ibuffer、resume 取指
```

`State` 持有：是否在展开宏指令（`active`）、当前索引（`uop_index`）、总数（`uop_count`）、绑定的生成函数（`gen_fn`）、缓存的当前微操作（`current_uop`）：

[State 结构：展开进度（active/index/count）+ 生成函数 + 缓存的当前 uop](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sequencer.h#L64-L80)

#### 4.3.3 源码精读

`Sequencer::get` 是核心。读它时分三段：幂等返回（L42-43）、宏指令激活与生成器绑定（L46-80）、微操作生成与 trace 填充（L82-104）：

[Sequencer::get：幂等返回 / 激活宏指令并绑定生成器 / 生成微操作 trace](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sequencer.cpp#L40-L110)

关键的第一段——幂等性。只要 `state_.current_uop` 非空就直接返回它，保证记分板检查（core.cpp L530）和实际派发（L593）两次 `get` 拿到的是同一条微操作：

[get 的幂等性：缓存非空就直接返回当前 uop](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sequencer.cpp#L41-L44)

第二段——遇到宏指令时，按 `fu_type` 绑定生成器。注意它**静态派发**到三个生成器之一，未启用的扩展用 `#ifdef` 隔离。`uop_count` 由各生成器根据宏指令的参数算出（例如 WGMMA 的 count 取决于稀疏与否、`cd_nregs`、K 步数）：

[宏指令激活：按 fu_type 绑定 LsuUopGen/TcuUopGen/RtuUopGen，并问出 uop_count](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sequencer.cpp#L46-L80)

第三段——生成一条微操作 trace。它调 `gen_fn` 拿到微操作 `Instr`，再从 trace 池分配一个 `instr_trace_t`，把宏指令的 `cid/wid/PC` 等元数据复制过来，但 `tmask`/`fu_type`/`op_type`/寄存器都用**微操作自己**的值（因为微操作可能只写部分寄存器、有不同 tmask）：

[微操作 trace 填充：复制 wid/PC，但 op_type/寄存器用微操作的值](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sequencer.cpp#L82-L104)

`advance` 把 `current_uop` 清空（下次 `get` 会生成新的），并推进 `uop_index`；到头了就把 `active` 关掉，返回 `true` 表示「这条宏指令的全部微操作都已发射」：

[advance：清缓存 + 推进 index，到头则关闭 active 并返回 true](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sequencer.cpp#L112-L121)

回到 `core.cpp` 的 `issue()`，看这套机制如何收尾。当 `seq->advance()` 返回 `true` 时：因为宏指令当初用 `set_wstall(true)` 暂停了取指，现在要 `scheduler_->resume(wid)` 把取指重新放开；又因为宏指令本身**不会走到 commit**（只有它的微操作会），所以要在这里手动把它从 `pending_instrs_` 移除并归还 trace 池：

[issue 收尾：宏指令全部 uop 发完时 resume 取指、移除宏 trace（它不进 commit）](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L617-L627)

---

现在看「N 是多少、每条微操作长什么样」。以 TCU 的 `WGMMA` 为例。`TcuUopGen::uop_count` 先判是不是 WGMMA，然后按公式算总数：

\[ \text{uop\_count} = \text{mma\_uops} + \text{needs\_setup} \]

其中 \(\text{mma\_uops} = k\_count \times nrc\)，\(nrc \in \{8,16,32\}\) 由 `cd_nregs` 决定，\(k\_count\) 在稀疏时减半；`needs_setup` 是 fedp2K 寄存器预取路径多出的那条 setup 微操作：

[TcuUopGen::uop_count —— WGMMA/WMMA 展开条数公式](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L1404-L1436)

每条微操作由 `TcuUopGen::get(macro_instr, uop_index)` 现场计算。它把 `uop_index` 反推回三维循环下标 \((m, n, k)\)，算出这条微操作该读哪些 A/B/C 寄存器、写哪个结果寄存器，并标记 `first`/`last`（用于控制累加器的首条初始化与末条收尾）。寄存器布局经过精心编排，使 A/B/C 三个操作数每次都落在不同的寄存器堆 bank 上，做到「0 冲突、0 停顿」：

[TcuUopGen::get —— WGMMA 第 uop_index 条微操作的 (m,n,k) 反推与寄存器布局](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L1553-L1609)

对比看 LSU 的 packed load，它更简单：`LsuUopGen::uop_count` 按 `width` 返回 4（PACKLB，4 字节）或 2（PACKLH，2 半字）：

[LsuUopGen::uop_count —— PACKLB=4 条 / PACKLH=2 条](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp#L30-L35)

每条微操作是一次普通的 `LBU`/`LHU`（无符号 load，避免符号扩展），靠 `stride = uop_index` 让 AGU 算出 `addr = rs1 + uop_index*rs2`，并用 `bytesel` 把加载的数据挪到目的浮点寄存器的正确字节位置（写回时由 `OpcUnit` 按掩码 OR 合并）：

[LsuUopGen::get —— 每条微操作是一次 LBU/LHU，stride/bytesel 决定地址与字节位置](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp#L37-L69)

这条 packed load 路径解释了为什么 u8-l4（LSU 流水线）会专门讲 `LsuUopGen`——宏指令展开的微操作最终都进了 LSU 的正常流水线。

#### 4.3.4 代码实践

**实践目标**：理解「一条 `WGMMA` 在发射阶段裂成多少条微操作」，并验证 `get` 的幂等性。

**操作步骤**：

1. 打开 `sim/simx/tcu/tcu_unit.cpp` 的 `TcuUopGen::uop_count`（L1404-L1436）。设一个具体的 WGMMA 配置（示例参数，用于练习）：非稀疏（`is_sparse=false`）、`cd_nregs=0`（→ `nrc=8`）、`kFedp2K=false` 或 `is_a_smem=true`（→ `needs_setup=false`）、`wg_cfg::k_steps=4`（→ `k_count=4`）。
   - 按公式算：`mma_uops = k_count * nrc = 4 * 8 = 32`，`needs_setup = 0`，所以 `uop_count = 32`。
   - 即一条这样的 `WGMMA` 会被展开成 32 条微操作。

2. 打开 `sim/simx/sequencer.cpp`，阅读 `Sequencer::get`（L40-L110）。回答：core.cpp 在 `issue()` 里对同一条 ibuffer 头部指令调了两次 `seq->get(trace)`（L530 和 L593）。为什么第二次不会重新生成一条新微操作、导致跳过第一条？
   - 提示：看 L42-43 的 `if (state_.current_uop) return state_.current_uop;`。

3. （选做）在 `build/` 目录用 `./ci/blackbox.sh --driver=simx --app=sgemm_tcu_wg` 跑一个张量核 sgemm（若该测试在你的配置下可用），用 `--debug` 观察 trace：你会看到一条 `WGMMA` 宏指令之后跟着一串 `WGMMA` 微操作（同 `parent_uuid`，不同 uuid），它们的 \((m,n,k)\) 下标依次递增。

**需要观察的现象**：trace 中宏指令与微操作的 uuid 关系——微操作的 uuid 高位嵌入了 `uop_index`（见 `TcuUopGen::get` 里 `steps_shift` 的位运算），可据此把同一宏指令的微操作归组。

**预期结果**：你能用 `uop_count` 公式算出给定 WGMMA 配置的展开条数，并解释 `get` 的幂等缓存为何让两次调用拿到同一条微操作。

**注意**：`sgemm_tcu_wg` 是否可运行取决于 TCU/WGMMA 是否在 `VX_config.toml` 中启用；若不可运行，本实践以源码阅读和公式推演为准，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`Sequencer::get` 被设计成「幂等」（重复调返回同一微操作，直到 `advance`）。如果去掉这个幂等性——让每次 `get` 都推进 `uop_index`——会在 `core.cpp` 的 `issue()` 里引发什么 bug？

> **答案**：`issue()` 对同一条 ibuffer 头部指令调了两次 `get`：一次在记分板检查（L530），一次在实际派发（L593）。若每次 `get` 都推进，记分板检查的是第 i 条微操作、实际派发的却是第 i+1 条，两者不一致，记分板冒险检测就失效了。幂等性保证「检查的就是派发的」。

**练习 2**：为什么宏指令本身不走 commit，而要在 `issue()` 里手动从 `pending_instrs_` 移除？

> **答案**：宏指令只是一个「容器」，真正执行并产生结果的是它的微操作。微操作各自流过完整的 issue→execute→commit 路径。如果宏指令也进 commit，它会被重复计数、且它的寄存器字段（往往是聚合的）无法对应一次真实写回。所以在 `advance()` 返回 `true`（全部微操作发完）时，就把宏指令 trace 在 issue 级就地回收（core.cpp L624-L626）。

**练习 3**：一条 `WGMMA` 译码时设了 `set_wstall(true)`。这个「停顿」是在哪一行被解除的？

> **答案**：在 `core.cpp` 的 `issue()` 里，当 `seq->advance()` 返回 `true` 且 `trace->instr_ptr->is_macro_op()` 时，调用 `scheduler_->resume(trace->wid)`（core.cpp L620-L621）。即「宏指令的全部微操作都已发射完毕」是解除取指停顿的信号。

## 5. 综合实践

把本讲三个模块串起来，跟踪一条「跨字的 RVC 分支指令」从取回到发射的完整旅程。请在阅读源码后，按下面的步骤画出一张完整的时序图。

**场景**：假设一条 `C.BEQZ`（16 位 RVC 分支）恰好横跨两个 4 字节字——它的低半字在地址 `0x1FE` 所在字的高 16 位，高半字在地址 `0x202` 所在字的低 16 位（地址为示例值，仅用于练习）。

**任务**：

1. **取指与解压**（4.1）：在 `core.cpp` 的 `fetch()` 里画出这条指令经历的两次 icache 请求。
   - 第一次：`pick_request` 从 `fetch_latch` 取到 trace，`req_addr = PC & ~3 = 0x1FC`。响应回来后，`on_icache_rsp` 发现 `PC[1]==1` 且低 2 位是 `0b11`（跨字 32 位？注意——`C.BEQZ` 是 16 位，所以这里要思考：跨字只发生在 32 位指令上；16 位 RVC 不会跨字。请重新设定场景为一条 32 位 `BEQ` 指令跨字，重新走一遍）。
   - 修正场景为 32 位 `BEQ` 跨字后，写出 `RvcSlot.needs_second` 被置位、trace 入 `refetch_queue_`、`pick_request` 下周期优先返回补取请求 `req_addr = (PC & ~3) + 4 = 0x200`、第二次响应拼出完整 32 位字的过程。

2. **译码**（4.2）：在 `decode.cpp` 入口，`code & 0x3 == 0b11`（标准 32 位），不走 RVC 展开。它命中 `case Opcode::B:`，`is_rvc=false`（因为它不是 RVC），`IntrBrArgs.is_rvc=0`，设 `set_wstall(true)`（分支停顿）。然后在 `core.cpp` decode 阶段 `advance_pc(trace, 4)`。

3. **（对照）**再走一遍一条**真正的 16 位 `C.BEQZ`（不跨字）**的路径：`on_icache_rsp` 走情形 B（取半字、零扩展），decode 入口 `code & 0x3 != 0b11` → 调 `rvc_decompress` 展开成 `BEQ`，`is_rvc=true`，`advance_pc(trace, 2)`。

**产出**：一张包含「PC、req_addr、trace->code、is_rvc、advance_pc 增量、是否进 refetch 队列」六列的表格，分别填 32 位跨字 BEQ 与 16 位 C.BEQZ 两行。

**参考答案（关键列）**：

| 指令 | req_addr（首次/补取） | trace->code | is_rvc | advance_pc 增量 | 进 refetch 队列 |
|------|----------------------|-------------|--------|----------------|----------------|
| 32 位 BEQ（跨字） | `0x1FC` / `0x200` | 拼出的 32 位 | false | +4 | 是（一次） |
| 16 位 C.BEQZ（不跨字） | `0x1FC`（仅一次） | 半字零扩展到 32 位 | true | +2 | 否 |

这张表把本讲的三个最小模块——解压器（决定 code 与是否补取）、译码器（决定 is_rvc 与 op_type）、（后续）sequencer（本场景不触发，因为分支不是宏指令）——的协作浓缩在了一起。

## 6. 本讲小结

- SimX 前端是一条 **schedule → fetch → decode → ibuffer** 的流水线。`fetch` 从 icache 取 4 字节字，`decode` 把它译成 `Instr` 对象，两者都在 `core.cpp` 里。
- **RVC 解压**（`decompressor.cpp`）采用「word-aligned fetch + 专门的解压器阶段」策略：icache 永远按 4 字节取，解压器用 `on_icache_rsp` 的三路分类（对齐 32 位 / 半字 RVC / 跨字补取）处理 2 字节对齐，并把跨字补取排在最高优先级。
- 解压器只把**原始比特**放进 `trace->code`，真正的「16→32 语义展开」集中在 **`Decoder::decode`** 入口（`code & 0x3 != 0b11` 时调 `rvc_decompress`），译码器还把 `is_rvc` 搭车塞进分支指令的 `IntrBrArgs`，驱动 PC 的 `+2`/`+4` 推进。
- 译码器是**无状态**的「位 → `Instr`」翻译机，按 opcode 大 switch 填 `fu_type`/`op_type`/寄存器/立即数，并标记 `is_wstall`（停顿取指）与 `is_macro_op`（需展开）。
- **宏指令 / 微操作**机制：`WGMMA`/`WMMA`、packed load、多槽窗口读在译码时被标成宏指令，在发射阶段由 **`Sequencer`** 按 `fu_type` 绑定 `TcuUopGen`/`LsuUopGen`/`RtuUopGen`，用幂等的 `get` + 显式的 `advance` 逐条展开成独立流过流水线的微操作；全部展开完毕才 `resume` 取指。
- 一条 `WGMMA` 的展开条数由 `uop_count` 公式 \(\text{k\_count} \times \text{nrc} + \text{needs\_setup}\) 决定，每条微操作对应一个 \((m,n,k)\) 循环下标，寄存器布局保证 0 bank 冲突。

## 7. 下一步学习建议

- **u6-l3（发射、记分板与操作数收集）** 是本讲的直接后续：它消费 ibuffer 里的指令（或微操作），用 `scoreboard` 做冒险检测、`OpcUnit` 收集操作数。理解了本讲的「微操作 trace 如何生成」，你就能看懂记分板为何要按微操作的寄存器（而非宏指令的聚合寄存器）来追踪。
- **u6-l4（功能单元 ALU/FPU/LSU/SFU）** 讲微操作最终派发到的地方；其中 SFU 作为「分派器」路由到 WCTL/CSR/TEX/RASTER 等子单元，与本讲译码器设的 `fu_type` 直接对应。
- **u8-l4（LSU 流水线设计）** 会展开 `LsuUopGen` 与 packed load 的下游：本讲只讲了宏指令如何裂成 `LBU`/`LHU` 微操作，那条微操作进入 LSU 后的 AGU 地址生成、slice 处理在 u8-l4。
- **u9-l1（张量核 WGMMA 引擎）** 会从 kernel API（`vx_tensor.h`）和 TCU 硬件角度深入 `WGMMA`；本讲给出的 `uop_count` 公式与 \((m,n,k)\) 展开是那篇讲义的 SimX 侧前置。
- 若你对 RVC 的 RTL 实现好奇，可对照阅读 `hw/rtl/core/VX_decompressor.sv` 与 `docs/designs/compressed_instruction_support.md` 的 §2——SimX 与 RTL 的解压器是一对 model_parity 对象（u7-l4）。
