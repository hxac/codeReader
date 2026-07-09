# 项目总览与定位：什么是 Koala-GPGPU

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标是让你在还没有读任何 RTL 细节之前，先在脑子里建立一张「全局地图」。读完本讲你应该能够：

- 用一句话说清楚 Koala-GPGPU 是什么、模仿了谁的架构。
- 解释 SM（streaming multiprocessor）、warp（线程束）、寄存器堆、取指这几个基本概念。
- 对照 README 给出的架构树，指出顶层 `gpgpu_top`、核心 `sm_core` 以及 9 个子模块各自的位置与职责。
- 看懂顶层模块 `gpgpu_top` 的对外接口（代码存储器接口 + host 接口）是如何向下传递给 `sm_core` 的。

本讲只讲「顶层封装」和「SM 核心骨架」两个最小模块，**不展开**任何一个子模块的内部实现——那是后续讲义的任务。

## 2. 前置知识

本讲面向零基础读者，但有几个名词先解释清楚会更顺：

- **GPGPU**：General-Purpose computation on GPU，把原本只用来画图的 GPU 用来做通用计算（矩阵、科学计算等）。
- **Fermi**：NVIDIA 在 2010 年前后推出的 GPU 微架构，是现代 CUDA GPU 的起点之一。Koala-GPGPU 就是从 Fermi 开始模仿。
- **SM（Streaming Multiprocessor，流式多处理器）**：GPU 里真正执行计算的核心单元。一块 GPU 有很多个 SM，本项目只实现**一个** SM。
- **warp（线程束）**：GPU 里调度的最小单位。NVIDIA GPU 中一个 warp 通常是 32 个线程，硬件以 warp 为单位取指、发射、执行。Koala-GPGPU 支持同时存在 8 个 warp。
- **流水线（pipeline）**：把一条指令的执行拆成「取指 → 译码 → 读操作数 → 执行 → 写回」等多个阶段，每个时钟周期各阶段并行处理不同指令，从而提高吞吐。
- **valid/ready 握手**：模块间传递数据时的协议。发送方拉高 `valid` 表示「我手上数据有效」，接收方拉高 `ready` 表示「我准备好收了」，两边同时为高时这一拍才完成一次传输。本项目的模块间通信几乎都用这个协议。
- **SystemVerilog（.sv 文件）**：Verilog 的增强版硬件描述语言，本项目的 RTL 全部用它写成。

如果上面某些词还觉得抽象，不用担心，后面结合源码会再讲一遍。

## 3. 本讲源码地图

本讲只涉及很少几个文件，但它们是整个项目的「骨架」：

| 文件 | 作用 |
|------|------|
| `README.md` | 项目说明，包含架构树、关键参数、支持的指令、ISA 编码格式。建立全局认知的主要依据。 |
| `rtl/gpgpu_top.sv` | 顶层模块，把对外接口（代码存储器 + host）转接给内部的 `sm_core`。 |
| `rtl/sm_core/sm_core.sv` | SM 核心，例化了全部 9 个子模块并用 wire 把它们连成流水线。是后续所有讲义的中心。 |
| `rtl/common/define.sv` | 全局参数定义（warp 数、寄存器数、位宽等），README 里的「Key Parameters」就是来自这里。 |

## 4. 核心概念与源码讲解

### 4.1 gpgpu_top：顶层封装与对外接口

#### 4.1.1 概念说明

`gpgpu_top` 是整个 GPU 的「外壳」。它的职责非常单一：

- 向外（也就是向仿真测试台或 FPGA 顶层）暴露两套接口：一套用来读**代码存储器**（kernel 指令放在里面），一套用来和 **host**（发起 kernel 的主机）通信。
- 向内只做一件事：把这两套接口原封不动地接给唯一的子模块 `sm_core`。

换句话说，`gpgpu_top` 本身**不做任何计算逻辑**，它只是个「接线盒」。这样设计的好处是：对外接口固定，而 SM 内部怎么实现可以独立演进，不会牵动顶层。

#### 4.1.2 核心流程

`gpgpu_top` 的数据流可以概括为三条线：

1. **时钟与复位**：`clk`（系统时钟）和 `rst_n`（低有效的复位）贯穿全片。
2. **代码存储器接口**（取指通路）：
   - 输出：`code_rd_req_*`（读请求：valid / 地址 / warp id）→ 向外部存储器要指令。
   - 输入：`code_rd_rsp_*`（读响应：valid / 地址 / warp id / 64 位指令数据）← 存储器把指令送回来。
3. **host 接口**（kernel 启动与完成通路）：
   - host → GPU：`host_req_*`（valid + 起始地址）表示「请从这个地址开始跑一个 kernel」。
   - GPU → host：`host_rsp_*`（valid + warp id）表示「某个 warp 跑完了（遇到 EXIT）」。

这三条线在 `gpgpu_top` 里被直接接到 `sm_core` 的同名端口上（host 接口在 `sm_core` 内部叫 `tpc` 接口，名字不同但信号一一对应）。

#### 4.1.3 源码精读

先看 `gpgpu_top` 的模块端口定义，它列出了全部对外信号，每行都带英文注释说明含义：

[rtl/gpgpu_top.sv:L5-L28](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/gpgpu_top.sv#L5-L28) —— 定义 `gpgpu_top` 的对外接口：时钟/复位、代码存储器读写接口、host 请求/响应接口。

端口里反复出现的位宽宏 `CODE_MEM_ADDR_WIDTH`、`DEPTH_WARP`、`CODE_MEM_DATA_WIDTH` 都来自 `define.sv`，下面会讲到。

接着看模块体——整个 `gpgpu_top` 只例化了**一个**子模块 `sm_core`，把对外信号映射过去：

[rtl/gpgpu_top.sv:L31-L48](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/gpgpu_top.sv#L31-L48) —— 例化 `sm_core U_sm_core`，把 `gpgpu_top` 的代码存储器接口和 host 接口转接给 SM 核心。

注意一个细节：`gpgpu_top` 的 `host_req_*` / `host_rsp_*` 端口，接到 `sm_core` 时被改名为 `tpc_req_*` / `tpc_rsp_*`。在真实 Fermi 架构里，SM 之上还有 GPC/TPC（纹理处理簇）层级，这里用 `tpc` 命名是保留了那层概念，但在本项目中 `gpgpu_top` 直接充当了 host 与 SM 之间的桥。这是后续阅读 `sm_core` 时容易困惑的第一个点，先记住。

#### 4.1.4 代码实践

**实践目标**：亲手确认「`gpgpu_top` 只是个接线盒，没有逻辑」。

**操作步骤**：

1. 打开 [rtl/gpgpu_top.sv](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/gpgpu_top.sv#L1-L52)。
2. 通读整个文件（只有 52 行）。
3. 数一数：除了 `module`/`endmodule` 和那一个 `sm_core U_sm_core (...)` 例化块之外，文件里有没有任何 `assign`、`always`、`reg`、`wire`？

**需要观察的现象**：

- 你应该发现：`gpgpu_top` 里**没有任何** `assign`、`always`、`reg` 或独立的 `wire` 声明，所有端口都被直接连进了 `sm_core` 的同名端口。

**预期结果**：整个模块体只有一个例化语句，纯做端口转接。这验证了「顶层外壳不含逻辑」的判断。

#### 4.1.5 小练习与答案

**练习 1**：`gpgpu_top` 的复位信号叫什么？是高有效还是低有效？

**参考答案**：叫 `rst_n`，从命名后缀 `_n` 和注释「reset signal to the system, negative active」可知是**低有效**（低电平时复位）。

**练习 2**：`gpgpu_top` 的 `host_req_start_addr_i` 在 `sm_core` 内部对应哪个端口？

**参考答案**：对应 `sm_core` 的 `tpc_req_start_addr_i`（见 [rtl/gpgpu_top.sv:L44](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/gpgpu_top.sv#L44)），二者在例化时连到同一根线。

---

### 4.2 sm_core：SM 核心与九大子模块

#### 4.2.1 概念说明

`sm_core` 才是 GPU 真正的「大脑」。它把一条指令从取到写回的全过程，拆成了 9 个分工明确的子模块，再用 wire 把它们串成一条流水线。这 9 个子模块就是 README 架构树里列出的那一组：

[README.md:L9-L21](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/README.md#L9-L21) —— README 给出的架构树：`gpgpu_top → sm_core`，下挂 9 个子模块，每个后面跟一句英文职责说明。

把这 9 个模块的职责翻成中文（后续讲义会逐个深入）：

| 子模块 | 中文职责 |
|--------|----------|
| `sm_warp_scheduler` | 轮询式（round-robin）为 kernel 启动分配 warp 槽位 |
| `sm_fetch` | 跟踪每个 warp 的 PC，仲裁对代码存储器的取指请求 |
| `sm_decode` | 从 64 位 Fermi 指令中抽取各个字段 |
| `sm_inst_buffer` | 每个 warp 一个 2 项的指令 FIFO，缓存已译码指令 |
| `sm_score_board` | 通过跟踪在飞（in-flight）的寄存器，检测 RAW（写后读）冒险 |
| `sm_operand_collect` | 按 warp 读寄存器堆并选择操作数 |
| `sm_issue` | 把指令派发到执行单元 |
| `sm_int_alu` | 整数算术/搬移执行单元 |
| `sm_writeback` | 把结果写回寄存器堆 |

这 9 个模块在 `sm_core.sv` 里被一一例化，下文会给出对应行号。

#### 4.2.2 核心流程

把 9 个模块按数据流向画出来，就是 SM 的主流水线（箭头表示数据/控制信号传递方向）：

```
host 启动 kernel
   │  (tpc_req: start_addr)
   ▼
sm_warp_scheduler ──(sm_warp_req: wid)──▶ sm_fetch ──(code_rd_req)──▶ 代码存储器
   ▲                                          ▲                          │
   │                                          │                  (code_rd_rsp: 指令+wid)
   │ (sm_warp_rsp: 完成的 wid)                │                          ▼
   │                                          │                     sm_decode
sm_issue ◀──(EXIT 时回送完成)                  │                          │
   ▲                                          │                          ▼
   │                                   (ibuffer_avail,            sm_inst_buffer
   │                                    ready_warps)                   │
   │                                          ▲                          ▼
   │                                          │                     sm_score_board
   │                                          │                    (检测 RAW 冒险,
   │                                          │                     输出 stalled_warps)
   │                                          │                          │
   │                                          │                          ▼
   │                                          │                  sm_operand_collect
   │                                          │                   (读寄存器,选操作数)◀──┐
   │                                          │                          │            │
   │                                          │                          ▼            │
   │                                          └──────────────────── sm_issue          │
   │                                                                     │            │
   │                                                                     ▼            │
   │                                                                  sm_int_alu       │
   │                                                                     │            │
   │                                                                     ▼            │
   │                                                                sm_writeback ─────┘
   │                                                                (wb_valid/wid/dst/data)
   └──────────────────────────── (EXIT 完成) ───────────────────────────┘
```

整个流水线的关键特点：

1. **以 warp 为中心**：几乎每条信号都带一个 `wid`（warp id），因为 8 个 warp 共用同一套硬件，靠 wid 区分上下文。
2. **取指是请求-响应式**：`sm_fetch` 发请求，外部存储器异步回数据，因此中间需要 `sm_inst_buffer` 缓存，避免取指空泡卡住后续。
3. **有两条反馈回路**：
   - `sm_writeback` 的写回结果会同时回送给 `sm_score_board`（用来清除该寄存器的在飞标记，解除冒险）和 `sm_operand_collect`（用来做数据前递/旁路）。
   - `sm_score_board` 输出的 `stalled_warps` 回送给 `sm_fetch`，让它在对应 warp 冒险时停止取指。
4. **EXIT 走回程**：当某 warp 执行到 `EXIT` 指令，`sm_issue` 会产生 `sm_warp_rsp_valid/wid`，经 `sm_warp_scheduler` 转成 `tpc_rsp`，最终从 `gpgpu_top` 的 `host_rsp_*` 输出，告诉 host「这个 warp 跑完了」。

#### 4.2.3 源码精读

**（1）关键参数来自 `define.sv`**

README 的「Key Parameters」表里那些数字，源头就在这里：

[rtl/common/define.sv:L7-L16](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/common/define.sv#L7-L16) —— 定义代码存储器位宽（地址 32 位、数据 64 位）、warp 数（8 个）、寄存器数（64 个，数据 32 位）等全局参数。

对照 README 的参数表：

| README 参数 | 对应宏 | 值 |
|-------------|--------|----|
| Concurrent warps = 8 | `NUM_WARP` | 8 |
| Registers per warp = 64 | `NUM_REG` | 64 |
| Instruction width = 64 bits | `CODE_MEM_DATA_WIDTH` | 64 |
| Code address width = 32 bits | `CODE_MEM_ADDR_WIDTH` | 32 |
| Register data width = 32 bits | `REG_DATA_WIDTH` | 32 |

注意 `DEPTH_WARP = $clog2(NUM_WARP)` = `$clog2(8)` = 3，所以 warp id 是 3 位；同理 `DEPTH_REG` = 6，寄存器号是 6 位——这正好对应后面 `sm_decode` 抽取出的 6 位 `dst`/`src1` 字段。

**（2）`sm_core` 的端口与内部连线**

`sm_core` 的对外端口和 `gpgpu_top` 几乎一模一样（只是 host 接口改名为 tpc）：

[rtl/sm_core/sm_core.sv:L5-L28](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/sm_core/sm_core.sv#L5-L28) —— `sm_core` 模块端口：代码存储器接口 + tpc（host）接口。

端口之后是一大片 `wire` 声明，用来在 9 个子模块之间传递信号：

[rtl/sm_core/sm_core.sv:L30-L107](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/sm_core/sm_core.sv#L30-L107) —— 子模块间所有内部连线，每根线都以「源模块名_信号名」命名（如 `decode_signals_*`、`sb_signals_*`），便于追踪数据来源。

你能看到一类典型的「译码信号束」，例如 `decode_signals_inst`、`decode_signals_dst`、`decode_signals_src1`、`decode_signals_immea` 等，它们对应 README 里 ISA 编码的各个字段，被一整束地从 `sm_decode` 传到 `sm_inst_buffer` 再传到 `sm_score_board`。

中间还有一行把外部读回的指令数据直接命名给 `inst_to_decode`：

[rtl/sm_core/sm_core.sv:L109](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/sm_core/sm_core.sv#L109) —— `assign inst_to_decode = code_rd_rsp_data_i;` 把代码存储器返回的 64 位数据接到译码器输入。

**（3）九个子模块的例化位置**

下面按流水线顺序给出每个子模块在 `sm_core.sv` 中的例化行号，方便你后续逐个点开阅读：

| 子模块 | 例化行号 | 永久链接 |
|--------|----------|----------|
| `sm_warp_scheduler` | L111–L126 | [rtl/sm_core/sm_core.sv:L111-L126](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/sm_core/sm_core.sv#L111-L126) |
| `sm_fetch` | L128–L147 | [rtl/sm_core/sm_core.sv:L128-L147](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/sm_core/sm_core.sv#L128-L147) |
| `sm_decode` | L149–L168 | [rtl/sm_core/sm_core.sv:L149-L168](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/sm_core/sm_core.sv#L149-L168) |
| `sm_inst_buffer` | L171–L206 | [rtl/sm_core/sm_core.sv:L171-L206](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/sm_core/sm_core.sv#L171-L206) |
| `sm_score_board` | L213–L248 | [rtl/sm_core/sm_core.sv:L213-L248](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/sm_core/sm_core.sv#L213-L248) |
| `sm_operand_collect` | L249–L284 | [rtl/sm_core/sm_core.sv:L249-L284](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/sm_core/sm_core.sv#L249-L284) |
| `sm_issue` | L285–L313 | [rtl/sm_core/sm_core.sv:L285-L313](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/sm_core/sm_core.sv#L285-L313) |
| `sm_int_alu` | L315–L331 | [rtl/sm_core/sm_core.sv:L315-L331](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/sm_core/sm_core.sv#L315-L331) |
| `sm_writeback` | L333–L346 | [rtl/sm_core/sm_core.sv:L333-L346](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/sm_core/sm_core.sv#L333-L346) |

重点观察 `sm_writeback` 的输出 `wb_valid/wb_wid/wb_dst/wb_data` 被同时连到了两个地方——既回送给 `sm_score_board`（清除在飞标记），又回送给 `sm_operand_collect`（数据前递），这正是 4.2.2 里说的反馈回路：

- 写回到记分牌：[rtl/sm_core/sm_core.sv:L230-L232](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/sm_core/sm_core.sv#L230-L232)
- 写回到操作数收集：[rtl/sm_core/sm_core.sv:L266-L269](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/sm_core/sm_core.sv#L266-L269)

而 `sm_score_board` 输出的 `stalled_warps` 回送给 `sm_fetch`（输入侧 `stalled_warps_i` 在 L136，输出侧 `stalled_warps_o` 在 L247），构成「冒险→停取指」的反馈。

> 说明：本讲只看「谁连到谁」，**不**展开任一子模块内部逻辑。每个子模块的内部实现都在后续单独的讲义里讲解。

#### 4.2.4 代码实践

**实践目标**：按本讲的实践任务，亲手把 `gpgpu_top → sm_core → 9 个子模块` 的结构树画出来，并标注中文职责，同时验证一条关键反馈连线。

**操作步骤**：

1. 打开 [README.md:L9-L21](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/README.md#L9-L21) 的架构树。
2. 打开 [rtl/sm_core/sm_core.sv](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/sm_core/sm_core.sv#L111-L346)，对照 L111–L346 的 9 个例化块，确认 README 列出的 9 个模块与源码一一对应（数量、名字都对得上）。
3. 在纸上或文本里画出本讲 4.2.2 的流水线示意图，给每个模块补一句中文职责（参考 4.2.1 的表格）。
4. **追踪一条反馈线**：在 `sm_core.sv` 里找到 `sm_writeback` 的输出 `wb_valid_o` / `wb_wid_o` / `wb_dst_o` / `wb_data_o`（L342–L345），再分别找到它们在 `sm_score_board`（L230–L232）和 `sm_operand_collect`（L266–L269）例化时的同名输入端口，确认同一组 `wb_*` wire 被连到了两个模块。

**需要观察的现象**：

- README 架构树的 9 个模块名，在 `sm_core.sv` 中都能找到对应 `U_sm_xxx` 例化，数量正好 9 个。
- `wb_valid` / `wb_wid` / `wb_dst` / `wb_data` 这 4 根 wire 同时出现在 `sm_score_board` 和 `sm_operand_collect` 的端口连接里，说明写回结果被两个下游模块共享。

**预期结果**：你得到一张标注完整的结构树，并且验证了「写回结果同时驱动记分牌清冒险 + 操作数前递」这条关键连线。**是否能在仿真里跑出该结果，待本地验证**（运行仿真的方法在下一讲 u1-l2 讲解）。

#### 4.2.5 小练习与答案

**练习 1**：`NUM_WARP = 8`，那么 `DEPTH_WARP` 等于多少？它代表什么？

**参考答案**：`DEPTH_WARP = $clog2(8) = 3`，代表 warp id 的位宽（3 位能编码 0~7 共 8 个 warp）。这也解释了为什么端口里 `code_rd_req_wid_o` 等信号的位宽是 `[`DEPTH_WARP-1:0]`。

**练习 2**：在本项目的 9 个子模块里，哪一个直接负责和外部代码存储器打交道（发取指请求）？

**参考答案**：`sm_fetch`。它在 [rtl/sm_core/sm_core.sv:L140-L142](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/sm_core/sm_core.sv#L140-L142) 输出 `code_rd_req_valid_o/addr_o/wid_o`，是唯一连到外部代码存储器请求侧的模块。

**练习 3**：当一条 `EXIT` 指令被执行，完成信号是沿着哪条路径回到 host 的？

**参考答案**：`sm_issue` 产生 `sm_warp_rsp_valid/wid`（[L311-L312](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/sm_core/sm_core.sv#L311-L312)）→ `sm_warp_scheduler` 转成 `tpc_rsp_valid_o/tpc_rsp_wid_o`（[L118-L119](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/sm_core/sm_core.sv#L118-L119)）→ `gpgpu_top` 转成 `host_rsp_valid_o/host_rsp_wid_o`（[L46-L47](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/gpgpu_top.sv#L46-L47)）送给 host。

## 5. 综合实践

把本讲学到的「全局地图」用起来：在 `Koala-GPGPU-tutorial/` 目录下（或你自己的笔记里）新建一个文本文件，画一张**完整的数据通路结构树**，要求：

1. 顶层写出 `gpgpu_top`，标出它的两套对外接口（代码存储器、host）。
2. 下一层写出 `sm_core`，并注明 host 接口在它内部叫 `tpc` 接口。
3. 把 9 个子模块按本讲 4.2.2 的流水线顺序排列，每个模块后面写一句中文职责。
4. 在图上用箭头标出**两条反馈回路**：
   - `sm_writeback → sm_score_board`（清冒险）
   - `sm_writeback → sm_operand_collect`（数据前递）
5. 在图上标出 **EXIT 完成回程**：`sm_issue → sm_warp_scheduler → (tpc_rsp) → host`。

完成后，你应该能不查源码就回答：「指令从哪里进来、在哪里译码、在哪里冒险检测、在哪里执行、结果写回哪里、完成信号怎么出去」。这张图将作为你阅读后续每一篇讲义时的「定位地图」——每学一个子模块，就把它在图上的位置点亮。

## 6. 本讲小结

- Koala-GPGPU 是一个用 SystemVerilog 模仿 NVIDIA Fermi 架构的 GPGPU，实现单个 SM 与基于 warp 的执行模型。
- `gpgpu_top` 是纯外壳，只把代码存储器接口和 host 接口转接给内部唯一的 `sm_core`，自身不含任何逻辑。
- `sm_core` 例化了 9 个子模块，按 `取指→译码→指令缓冲→冒险检测→操作数收集→发射→执行→写回` 的顺序串成流水线。
- 全局参数（8 warp、64 寄存器、64 位指令、32 位地址）都定义在 `rtl/common/define.sv`，README 的参数表即来源于此。
- 写回结果有两条反馈回路：回送记分牌清冒险、回送操作数收集做前递；EXIT 完成信号经 `sm_issue → sm_warp_scheduler → tpc → host` 回送 host。
- 模块间几乎全部用 valid/ready 握手协议通信，且每条数据都带 `wid` 以区分 8 个 warp 的上下文。

## 7. 下一步学习建议

本讲只建立了「骨架地图」，还没有让项目真正跑起来。建议按以下顺序继续：

1. **下一讲 u1-l2「构建系统与运行测试」**：学习 `make test_integer` 如何用 iverilog + vvp + cocotb 把仿真跑起来，亲手在 `build/` 和 `logs/` 里看到寄存器验证结果。这是后续所有「观察现象」类实践的前提。
2. **u1-l3「仓库目录结构导览」**：浏览 `rtl/common`、`rtl/sm_core`、`test`、`project/altera` 四个目录，明确公共基础设施、核心流水线、测试与 FPGA 工程的边界。
3. 在进入任何子模块讲义之前，**先回头确认本讲 4.2.2 的流水线图你已画熟**——后续每篇讲义都会假设你脑子里有这张图。
4. 想提前感受「真实跑起来是什么样」，可以跳到第 7 单元的 Cocotb 测试讲义，但更推荐先走完第 2、3 单元打好基础。
