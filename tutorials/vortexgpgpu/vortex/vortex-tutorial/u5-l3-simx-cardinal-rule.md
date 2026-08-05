# 基数规则与模块分解

## 1. 本讲目标

本讲是 SimX 系列的第三篇。在 u5-l1 我们学了 `SimObject`/`SimChannel`/`SimPlatform` 三大基元，在 u5-l2 我们看到这些零件如何组装成一台完整 GPU 并按下启动键。本讲要回答一个更上位的问题：**这些零件拼装时，必须遵守什么样的纪律，才能让 SimX 仿真器成为 RTL 的「预言机」（oracle）？**

学完本讲你应该能够：

1. 说清楚**基数规则（The Cardinal Rule）**——「模块只通过 channel 通信」——是什么，以及它为什么不可违反。
2. 理解 **SimX v3 模型**的核心设计决定：取消中央 `Emulator`，让一条指令的功能语义与它在硬件里的时序住在**同一个模块**里，数据以真实载荷形式流过缓存层次。
3. 在 `types.h` 中定位 `MemReq`/`MemRsp` 结构，并解释**为什么一次 LOAD 响应必须把整条 cache line 的数据随身带回来**，而不能靠 `core->mem_read` 这种「后门」去读。

这三点共同回答了贯穿整个 SimX 系列的主线：**SimX 之所以能逐模块地与 RTL 对齐（model_parity），正是因为模块边界、数据通路都被严格约束成「和真实连线一一对应」。**

## 2. 前置知识

阅读本讲前，你应当已经具备（来自 u5-l1、u5-l2）：

- **`SimChannel<Pkt>` 的基本概念**：它是一条带类型、带延迟、带背压的连线；生产者 `send(pkt, delay)` 后，包会在若干周期后被消费者收到。`delay=1` 的 channel 充当流水线寄存器，`delay=0` 充当组合逻辑直通。
- **`SimObject<Impl>` 的概念**：CRTP 模块基类，通过 `on_tick()`/`on_reset()` 钩子被每周期调用；纯「管道/门面」模块自动跳过、零开销。
- **GPU 的实例化层次**：`Processor → Cluster → Socket → Core`，以及各级缓存的共享边界（socket 共享 L1、cluster 共享 L2、全局共享 L3/DRAM）。
- **TLM（Transaction-Level Modeling）直觉**：一次访存不是一次函数调用，而是一个「请求包 → 若干周期后 → 响应包」的事务。

一个关键直觉，本讲会反复用到：**在 SimX 里，channel 图就是芯片的连线图。** 你在 RTL 里看到一根线连接两个模块，在 SimX 里就对应一条 `SimChannel`；你在 RTL 里看到数据流过 L1→L2→L3→DRAM，在 SimX 里就对应 `MemReq`/`MemRsp` 包沿 channel 链逐级流动。

> 术语提示：
> - **后门（back door）**：绕过被建模的硬件通路（coalescer/缓存/NoC），直接读写底层存储的捷径。本讲的核心论点之一就是「SimX 不允许后门」。
> - **预言机（oracle）**：调试 RTL 时，用一个被信任的参考模型给出「正确答案」来比对。SimX 之所以能当 RTL 的 oracle，正是因为它逐模块地忠实建模了每一级延迟。

## 3. 本讲源码地图

本讲涉及的文件不多，但每个都很关键：

| 文件 | 作用 |
| --- | --- |
| [`docs/simobject.md`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/simobject.md) | 框架参考手册，开篇的 **The Cardinal Rule** 一节是本讲的灵魂。 |
| [`docs/designs/simx_simulator_architecture.md`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/simx_simulator_architecture.md) | SimX 架构设计文档。§1 讲 v3 模型，§2 复述框架，§3 给出全模块清单。 |
| [`sim/simx/types.h`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/types.h) | 全 SimX 共享的类型定义。`MemReq`/`MemRsp`/`mem_block_t`/`MemOp` 都在这里。 |
| [`sim/simx/lsu_unit.cpp`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp) | 访存单元，演示「LOAD 数据从 `MemRsp::data` 取回」的真实用法。 |
| [`sim/simx/processor.cpp`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/processor.cpp) | 顶层处理器，演示 `mem_reads`/`mem_writes` 如今只是性能计数器标签。 |

## 4. 核心概念与源码讲解

### 4.1 基数规则：模块只通过 channel 通信

#### 4.1.1 概念说明

「基数规则」（The Cardinal Rule）是 SimX 框架唯一一条被冠以「不可商量」（non-negotiable）的纪律：

> **Modules communicate *only* through channels.**（模块之间只能通过 channel 通信。）

一个 `SimObject` 想观察或改变另一个模块的状态，**唯一**合法的途径是经由它被接线（bind）到的 channel 端口——比如 `MemReq`/`MemRsp`、`result_if` 之类。它**绝不允许**跨所有权层级去直接戳另一个对象的内部。

为什么要立这样一条规矩？因为 channel 不是普通的通信工具，它就是「线」。一根 channel 对应芯片上一根真实的连线。如果一个模块绕过 channel 直接去读 DRAM 后备存储，那它建模的就是**真实硬件里根本不存在的数据通路**。

#### 4.1.2 核心流程

基数规则可以用一个「错」与「对」的对照来理解。设想一个位于 `Core` 内部的叶单元，需要完成一次 store：

```text
【错误写法】叶单元沿所有权树向上爬：
   leaf unit → core_ → processor() → memsim()   // 攀到全局 Memory
   然后 gmem->write_bytes(&data, addr, size);     // 直接写 DRAM 后备存储
   结果：绕过了 coalescer / cache / NoC 的整条被建模的缓存路径。

【正确写法】叶单元只驱动自己的输出 channel：
   out_req.try_send(MemReq{ .addr=addr, .op=MemOp::STORE, ... });
   结果：请求包像真实连线一样流过 coalescer → L1 → L2 → L3 → DRAM。
```

这两条路径的区别不是「风格」问题，而是**建模正确性**问题。错误写法里，模块能读到一个值，但在真实硅片上这个值此刻还正在缓存层次里「在途（in flight）」——于是 SimX 会产生 RTL 永远跑不出来的结果，SimX 作为 RTL 预言机的资格就破产了。

把这条规则展开成三条「为什么」：

1. **channel 即连线**：`SimChannel` 图就是芯片连线的 SimX 模型。一个模块连到系统其余部分的唯一通路，就是它接线时被赋予的那组端口；绕过它们等于凭空造出硬件里不存在的线。
2. **保住时序/功能保真度与 SimX↔RTL parity**：走 channel 路径，时序模型和功能效果才能保持一致——这正是 SimX 能当 RTL 忠实预言机的根本原因。
3. **层级是所有权，不是调用图**：`Core` *拥有* 它的单元，`Processor` *拥有* `Memory`；这种父→子的所有权关系只是为生命周期/构造服务，**绝不能**沿它向上爬（`child->parent()->…`）或横向去调用兄弟模块的内部。

#### 4.1.3 源码精读

基数规则的权威表述在 `docs/simobject.md` 开篇。整节标题就叫 **The Cardinal Rule**，紧接着给出 WRONG/RIGHT 对照：

> [docs/simobject.md:L17-L50](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/simobject.md#L17-L50) ——「The Cardinal Rule」整节：先点明「模块只通过 channel 通信」，再用一段注释为 `WRONG` / `RIGHT` 的代码对照说明，最后用三条 bullet 解释为何不可商量。

其中 WRONG 分支的注释一针见血：「a leaf unit climbs `Core -> Processor` to grab the global Memory and read/write its DRAM backing store, bypassing the modeled cache path」（一个叶单元顺着 `Core → Processor` 攀到全局 Memory，去读写它的 DRAM 后备存储，绕过了被建模的缓存路径）。这正是基数规则要禁止的典型反模式。

RIGHT 分支只有一行：

```cpp
// 示例代码（摘自 docs/simobject.md 的 RIGHT 分支）
out_req.try_send(MemReq{ .addr = e.addr, .op = MemOp::STORE, ... });
```

驱动自己的输出 channel，让请求像真实连线一样流过去。注意这里出现的 `MemReq` 与 `MemOp::STORE` 正是我们在 4.3 要精读的载荷类型——基数规则不是抽象口号，它落在具体的包结构上。

#### 4.1.4 代码实践

**实践目标**：在真实源码里找一处「遵守基数规则」的 channel 接线，确认它没有跨层级调用。

**操作步骤**：

1. 打开 `sim/simx/lsu_unit.cpp`，定位到处理访存响应的 `on_tick` 分支（搜索 `mem-rsp`）。
2. 观察它如何拿到数据：它**只**从输入 channel 推上来的 `lsu_rsp` 里读，没有任何 `core_->processor()->memsim()` 之类的向上攀爬。
3. 用 Grep 在 `sim/simx/lsu_unit.cpp` 内搜索 `memsim` 或 `processor()`，确认**零命中**。

**需要观察的现象**：LSU 取回 load 数据完全依赖 `lsu_rsp.data.at(lane)`（详见 4.3.3），它的世界里只有「我自己的输入/输出 channel」。

**预期结果**：LSU 模块内不存在任何向上所有权树或横向兄弟模块的直接调用，数据 100% 经 channel 到达。

#### 4.1.5 小练习与答案

**练习 1**：假设有人为了「加速仿真」，在某个功能单元里加了 `core_->processor()->memsim()->read_bytes(...)` 去直接读 DRAM。这条改动会破坏哪两件事？

> **答案**：① 破坏了基数规则（模块绕过 channel、跨所有权层级直接戳兄弟模块内部）；② 破坏了 SimX↔RTL parity——该单元可能读到真实硅片上仍在缓存层次中「在途」的值，产生 RTL 跑不出来的结果，SimX 不再是忠实预言机。

**练习 2**：基数规则里的「channel 即连线」与 u5-l1 讲过的「channel 就是流水线」是矛盾的吗？

> **答案**：不矛盾。前者从**拓扑**角度说：channel 图对应芯片连线图，模块只能通过被接线的端口与外界交互；后者从**时序**角度说：带 `delay=1` 的 channel 充当流水线寄存器，使流水线单元无需内部 stage deque。两者是同一事物的两个视角。

---

### 4.2 SimX v3 模型：语义与时序同居一处

#### 4.2.1 概念说明

理解了基数规则，我们就能看懂 SimX v3 最根本的一条架构决定。所谓「v3」是相对于已被废弃的旧设计而言的。旧 SimX 里有一个中央 `Emulator`「上帝对象」：所有指令的功能语义集中在它身上，各模块只负责报时序。v3 彻底删掉了这个中央 `Emulator`。

v3 的定义性特征是：**一条指令的功能语义，住在「负责给这条指令计时的那个硬件模块」里。**

- ALU、FPU 各自拥有私有的 `execute()` 方法，ISA 语义归它们自己。
- SFU 是一个路由器，把指令派发到它的子单元；CSR/WCTL 的语义住在各自子单元类里。
- warp/CTA/barrier 的状态住在 `Scheduler`；寄存器堆住在 `OpcUnit`；译码住在 `Decoder`。
- 数据以**真实载荷**的形式流过内存层次：cache 和 DRAM 都随身携带 line 数据。

这一点与基数规则是配套的。如果数据要靠 `core->mem_read/mem_write` 这种后门去取，那就必然要跨所有权层级直接调用——直接违反基数规则。所以 v3 把数据通路彻底改成 TLM 载荷流。

#### 4.2.2 核心流程

把 v3 模型画成「所有权 vs 语义归属」的对照：

```text
硬件块          谁拥有它的时序模型      谁拥有它的功能语义 (v3)
──────────────────────────────────────────────────────────
ALU/FPU         alu_unit / fpu_unit     各自的私有 execute()
SFU             sfu_unit                路由到 WCTL/CSR/TEX/RASTER/DXA/OM 子单元
warp/CTA/barrier scheduler              Scheduler 内部状态
寄存器堆         opc_unit                OpcUnit (每 warp 一份)
译码            decode (Decoder)         Decoder
内存数据         cache/memory            以 MemReq/MemRsp 载荷沿 channel 链流动
```

关键结论：**没有中央 `Emulator`，也没有 `core->mem_read/mem_write` 后门**——这两个名字如今只作为性能计数器的标签幸存（见 4.3.3 的 `perf_mem_reads_`）。

这样的设计有一个直接后果：SimX 成了 RTL 的「逐模块孪生体（module-by-module twin）」。RTL 里 `VX_alu_unit.sv` 负责什么，SimX 里 `alu_unit` 就负责什么；二者一一对应、各自封闭。这正是 SimX 能用于「周期级 parity 调试」的原因——它服务为 RTL 的预言机（serves as the RTL oracle for cycle-parity debugging）。

#### 4.2.3 源码精读

v3 模型的定义在 `docs/designs/simx_simulator_architecture.md` §1，简短而关键：

> [docs/designs/simx_simulator_architecture.md:L15-L29](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/simx_simulator_architecture.md#L15-L29) —— §1「The v3 model: functional + timing in one place」。开篇即点明「there is no central `Emulator`」，随后逐项列出 ISA 语义的归属，并以「there is no `core->mem_read/mem_write` back door」收尾，落点在「makes SimX a faithful, module-by-module twin of the RTL」。

紧接着 §2 用三个要点复述了 u5-l1 已学过的框架（SimObject/SimChannel/SimPlatform），并再次强调「The channel is the pipeline」——这条结论正是基数规则得以成立的物理基础：

> [docs/designs/simx_simulator_architecture.md:L32-L48](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/simx_simulator_architecture.md#L32-L48) —— §2「Framework」，重申 channel = pipeline、pipelined units 无需内部 stage deque。

而被 v3 废弃的旧方向，文档 §6 明确记录在案，以防死灰复燃——其中就包括中央 `Emulator` 上帝对象和 `core->mem_read` 数据通路：

> [docs/designs/simx_simulator_architecture.md:L159-L164](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/simx_simulator_architecture.md#L159-L164) —— §6「Superseded directions」：中央 `Emulator` 与 `MemBackend`/`core->mem_read` 数据通路已被删除（「semantics now live on the units, data lives in the hierarchy」）。

#### 4.2.4 代码实践

**实践目标**：在源码里验证「语义与时序同居一处」——找一个功能单元的私有 `execute()`，确认 ISA 语义确实写在它自己内部。

**操作步骤**：

1. 打开 `sim/simx/alu_unit.cpp`，搜索 `execute`。
2. 确认 `execute()` 是 `private` 成员（v3 文档明确指出：曾把 `execute()` 设为 public，后又改回 private）。
3. 观察它内部对 `AluType` 的 switch——ADD/SUB/SLL……每种算术操作的真实语义就在这里。

**需要观察的现象**：ALU 的时序（`on_tick` 里的 channel 收发）和功能（`execute` 里的运算）写在同一个 `.cpp`、同一个类里。

**预期结果**：找不到一个「中央解释器」在替 ALU 算 ADD；运算是 ALU 自己的事。若无法本地编译，可仅做源码阅读，结论一致。

#### 4.2.5 小练习与答案

**练习 1**：为什么 v3 要把 `execute()` 设成 `private`，而不是让外部模块直接调用 `alu->execute(instr)`？

> **答案**：如果 `execute()` 是 public，外部就能跨模块直接调用 ALU 的运算逻辑——这等于开了一条绕过 channel 的后门，违反基数规则，也破坏时序模型（调用者会在零周期内拿到结果，而真实 ALU 有流水线延迟）。设为 private 后，ALU 的运算只能由它自己的 `on_tick` 在正确周期、经 channel 触发。

**练习 2**：v3 文档说 `core->mem_read/mem_write` 后门的名字「survive only as perf-counter labels」。请结合 4.3.3 说明这是什么意思。

> **答案**：这两个名字如今不再是「去读内存」的方法，而只是 `Processor` 里 `perf_mem_reads_` / `perf_mem_writes_` 这两个性能计数器字段的命名残留。读内存的数据通路已被 `MemReq`/`MemRsp` 载荷流取代（详见 4.3）。

---

### 4.3 模块清单与 MemReq/MemRsp 数据载荷

#### 4.3.1 概念说明

前两节讲了「纪律」（基数规则）和「架构」（v3 模型）。这一节落到**具体的数据结构**：一次访存事务，到底长什么样？

在 v3 里，访存数据是**以载荷形式**沿 channel 链流动的真实数据，而不是靠后门去读的。承载这些载荷的，就是 `types.h` 里的 `MemReq`（请求）和 `MemRsp`（响应）。理解它们的字段，是理解「LOAD 响应为什么必须带 line 数据」的前提。

与之配套的还有几个关键类型：

- **`mem_block_t`**：一个内存块（一条 cache line / DRAM 传输单元），即 `std::array<uint8_t, VX_CFG_MEM_BLOCK_SIZE>`。它是访存载荷的「最小集装箱」。
- **`MemOp`**：访存操作类型枚举（LD/ST/FLUSH 及一系列原子操作）。
- **`MemFlags`**：访存请求的标志位（是否 IO、是否本地内存、原子符号性等）。

`simx_simulator_architecture.md` §3 的全模块清单（main/processor/cluster/socket/core、scheduler、decode、scoreboard、opc_unit、各 FuncUnit、内存子系统等）则告诉我们：这些 `MemReq`/`MemRsp` 包在哪些模块之间流动。本节聚焦载荷本身，模块逐一展开留给后续 U6/U8。

#### 4.3.2 核心流程

一次 LOAD 从 LSU 到 DRAM 再回来的完整 TLM 流程：

```text
LSU 生成 MemReq{ op=LD, addr, tag, ... }            // 请求里没有 data（load 不写）
   │  (data 字段为 nullptr)
   ▼
mem_coalescer ──► L1 Cache ──► L2/L3 cache_cluster ──► Memory (DRAM)
   │                                                        │
   │  每一级只转发/修改 MemReq，不靠后门读真实数据             │
   │                                                        ▼
   │                                          Memory 命中 RAM，构造
   │                                          MemRsp{ tag, data=line 载荷 }
   ◄────────────── MemRsp 沿原路 channel 链返回 ◄─────────────┘
   │  MemRsp.data 携带整条 line
   ▼
LSU 从 MemRsp.data 按地址偏移切出所需字节，写回寄存器堆
```

关键点在于：**LOAD 的请求方向不携带数据，但响应方向必须携带整条 line 的数据。** 因为 LSU 取回数据时，只能从「推到我输入 channel 上的 `MemRsp`」里读——它没有任何别的合法途径拿到内存值（否则就违反基数规则）。

#### 4.3.3 源码精读

先看载荷的最小集装箱 `mem_block_t`，注意它的注释直接点明了设计意图：

> [sim/simx/types.h:L50-L53](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/types.h#L50-L53) —— `mem_block_t` 定义。注释说明：一个内存块由 `MemReq`/`MemRsp` 在 TLM 数据通路模式下携带；用 `shared_ptr<mem_block_t>` 是为了让 MSHR 合并后的重放共享同一个 fill buffer 而无需拷贝。

```cpp
// 一个内存块（cache line / DRAM 传输单元）。TLM 数据通路模式下由 MemReq/MemRsp 携带。
// 用 shared_ptr 是为了让 MSHR 合并后的重放共享同一个 fill buffer 而无需拷贝。
using mem_block_t = std::array<uint8_t, VX_CFG_MEM_BLOCK_SIZE>;
```

接着是 `MemOp` 枚举，注意原子族（AMO_*）是在 `VX_CFG_EXT_A_ENABLE` 下才有意义、但枚举值位置固定：

> [sim/simx/types.h:L393-L410](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/types.h#L393-L410) —— `MemOp` 枚举：LD=0、ST=1、FLUSH=2 始终存在；AMO_LR/SC/SWAP/ADD/AND/OR/XOR/MIN/MAX 占据 3..11 的连续区间。

`MemFlags` 是个 32 位联合体，按位解释各项语义（含 DXA 完成边带）：

> [sim/simx/types.h:L424-L461](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/types.h#L424-L461) —— `MemFlags`：`strsp`/`io`/`local`/`amo_unsigned` 及可选的 DXA 通知边带，并有 `static_assert(sizeof(MemFlags)==4)` 保证它压在 32 位里。

**重头戏是 `MemReq`**。请特别留意它的 `data` 字段：

> [sim/simx/types.h:L1226-L1265](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/types.h#L1226-L1265) —— `MemReq` 结构。字段依次为 `op`、`addr`、`data`（`shared_ptr<mem_block_t>`）、`byteen`、`tag`、`hart_id`、`uuid`、`flags`。对 LOAD，`data` 默认为 `nullptr`（构造函数默认值）；对 STORE/AMO，`data` 装着要写入的 line。

```cpp
struct MemReq {
  MemOp    op;
  uint64_t addr;
  std::shared_ptr<mem_block_t> data;   // LOAD 时为 nullptr；STORE/AMO 时为写入的 line
  uint64_t byteen = 0;
  uint32_t tag;
  uint32_t hart_id;
  uint64_t uuid;
  MemFlags flags;
  // ... 构造函数、is_write()、addr_type()、operator<<
};
```

**对应的 `MemRsp`**：

> [sim/simx/types.h:L1269-L1290](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/types.h#L1269-L1290) —— `MemRsp` 结构。字段为 `tag`、`hart_id`、`uuid`、`data`（`shared_ptr<mem_block_t>`）。注意响应里**没有** `op`/`addr`——只有 `tag` 用来与请求配对，外加一个 `data`。

```cpp
struct MemRsp {
  uint64_t tag;
  uint32_t hart_id;
  uint64_t uuid;
  std::shared_ptr<mem_block_t> data;   // LOAD 响应里：整条 line 的数据载荷
  // ...
};
```

`simx_simulator_architecture.md` §3 对这两个结构有一句点睛之笔：

> [docs/designs/simx_simulator_architecture.md:L101-L103](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/simx_simulator_architecture.md#L101-L103) —— 「`MemReq`/`MemRsp` carry `shared_ptr<mem_block_t> data` + `byteen`; a LOAD response must carry a line payload.」

现在回答本讲核心问题：**为什么 LOAD 响应必须携带 line 数据载荷？** 答案在 LSU 取数据的现场。LSU 收到 `LsuRsp`（其 `data` 是 `vector<shared_ptr<mem_block_t>>`，逐 lane 一份，源自沿 channel 链返回的 `MemRsp`），第一行就是一条硬断言：

> [sim/simx/lsu_unit.cpp:L227-L234](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp#L227-L234) —— LSU 处理 load 响应：对每个活跃 lane，先断言 `lsu_rsp.data.at(lane) && "LOAD response must carry line payload"`，再用 `std::memcpy` 从该 line 按 `off` 偏移切出 `data_bytes` 字节。

```cpp
// 示例代码（摘自 sim/simx/lsu_unit.cpp，load 响应处理）
for (uint32_t lane = 0; lane < lsu_rsp.mask.size(); ++lane) {
    if (!lsu_rsp.mask.test(lane)) continue;
    const auto& lane_info = entry.lanes.at(lane);
    assert(lsu_rsp.data.at(lane) && "LOAD response must carry line payload");  // 硬断言
    uint32_t off = lane_info.addr & (VX_CFG_MEM_BLOCK_SIZE - 1);
    uint64_t read_data = 0;
    std::memcpy(&read_data, lsu_rsp.data.at(lane)->data() + off, data_bytes);  // 唯一数据来源
    // ... 按 RISC-V load 语义格式化 read_data 并写回寄存器堆
}
```

这条断言的字符串 `"LOAD response must carry line payload"` 正是本讲实践任务的标题。它的存在说明：LSU **完全依赖**响应里的 `data`，没有任何「兜底后门」可走。如果哪个 cache 层或 Memory 忘了在 `MemRsp` 里填 `data`，仿真会在这一行直接 assert 失败。

最后，验证 4.2.5 练习 2 的结论——`mem_reads`/`mem_writes` 如今确实只是性能计数器标签。`Processor` 通过给内存请求 channel 注册一个 `tx_callback`，在包到达时顺手计数，从不真正「读内存」：

> [sim/simx/processor.cpp:L119-L124](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/processor.cpp#L119-L124) —— `memsim_->mem_req_in.at(i).tx_callback(...)`：在请求包到达内存入口 channel 的交付周期，按 `req.is_write()` 分别累加 `perf_mem_reads_` / `perf_mem_writes_`。

> [sim/simx/processor.cpp:L330-L335](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/processor.cpp#L330-L335) —— `perf_stats()` 把 `perf_mem_reads_` / `perf_mem_writes_` 填进性能统计结构。这就是「`mem_read` 名字仅作为计数器标签幸存」的全部含义。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：在 `types.h` 中找到 `MemReq`/`MemRsp`，并结合 `lsu_unit.cpp` 的断言，解释为何 LOAD 响应必须携带 line 数据载荷（而非通过 `core->mem_read` 后门）。

**操作步骤**：

1. 打开 [`sim/simx/types.h`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/types.h)（约 L1226），抄下 `MemReq` 的字段表，标注每个字段在 LOAD 请求时的取值（特别留意 `data=nullptr`）。
2. 紧接着看 `MemRsp`（约 L1269），确认响应唯一的「数据」字段就是 `data`。
3. 打开 [`sim/simx/lsu_unit.cpp`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp)（约 L231），找到那条断言，确认 LSU 读 load 数据的**唯一**来源是 `lsu_rsp.data.at(lane)`。
4. 用 Grep 在 `sim/simx/lsu_unit.cpp` 里搜 `core_->processor()` 或 `memsim`，确认零命中——LSU 没有任何向上攀爬。

**需要观察的现象**：

- `MemReq` 的 `data` 字段在 LOAD 时为空；响应方向 `MemRsp.data` 必须非空，否则断言失败。
- LSU 取数据的全部代码路径里，没有任何一处调用 `Memory::read_bytes` 之类的后门。

**预期结果 / 解释**：

LOAD 响应必须携带 line 数据，是因为**基数规则要求访存数据只能以载荷形式沿 channel 链流动**。LSU 这个叶单元被接线时只拿到了「内存响应」这条输入 channel，它的世界里只有这条 channel；它没有任何合法的、能跨所有权层级去戳 `Processor::memsim()` 的通路（那条路就是被 v3 删除的 `core->mem_read` 后门）。因此整条 line 必须由 DRAM/Memory 装进 `MemRsp.data`，逐级 channel 原路送回，LSU 才能在自己的输入端口上 `memcpy` 到所需字节。

> 备注：若你尚未配置可运行的 SimX 构建环境，步骤 1–4 可作为纯源码阅读实践完成，结论完全一致；断言字符串本身就是文档级的证据。如需运行验证，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`MemReq` 和 `MemRsp` 为什么都用 `shared_ptr<mem_block_t>` 而不是直接持有 `mem_block_t` 值？

> **答案**：两个原因。① 避免在 channel 链每一级拷贝整条 line（line 可能几十字节，每周期大量包会很贵）；② `types.h:L50-L52` 的注释指出：MSHR 合并后的重放要共享同一个 fill buffer——多个等待同一 cache line 的请求，在 fill 时共享同一个 `shared_ptr`，无需重复拷贝。

**练习 2**：`MemRsp` 里没有 `addr` 字段，LSU 怎么知道返回的数据对应哪个地址？

> **答案**：靠 `tag`（以及 `hart_id`/`uuid`）与原始请求配对。LSU 在发出 `MemReq` 时记录 `tag → 请求上下文`，收到 `MemRsp` 时按 `tag` 查回上下文（包括当初的地址偏移 `lane_info.addr`），再从 `data` 里按 `addr & (BLOCK_SIZE-1)` 的偏移切字节（见 `lsu_unit.cpp:L232-L234`）。这正是 TLM「事务靠 tag 配对、而非靠地址」的特征。

**练习 3**：假如某个 cache 层在返回 `MemRsp` 时忘了填 `data`，仿真会怎样？

> **答案**：会在 `lsu_unit.cpp:L231` 的断言 `assert(lsu_rsp.data.at(lane) && "LOAD response must carry line payload")` 处直接失败终止。这条断言就是「LOAD 响应必须携带 line 数据」这条不变量的运行时守卫。

## 5. 综合实践

把本讲三节串起来，完成一个「反模式审查」小任务：

**任务**：假设一位新同学为了「让某个自定义加速器更快拿到内存数据」，提交了如下改动——在加速器单元里新增一行 `auto* gmem = this->core()->processor()->memsim(); gmem->read_bytes(buf, addr, size);`。请你作为 reviewer，用本讲的三条要点写一段 review 意见，并给出正确的修改方向。

**参考要点**：

1. **指出违反的纪律**：这行代码是 `docs/simobject.md` Cardinal Rule 节里 WRONG 分支的翻版——叶单元沿 `core() → processor()` 攀到全局 `Memory`，绕过了被建模的 coalescer/cache/NoC 路径。
2. **指出后果**：它破坏了 SimX↔RTL parity——加速器可能读到真实硅片上仍在缓存层次「在途」的值，产生 RTL 跑不出来的结果，SimX 不再是忠实预言机。
3. **给出正确方向**：让加速器驱动自己的 `SimChannel<MemReq>` 输出端口（如 `out_req.try_send(MemReq{ .addr=..., .op=MemOp::LD, ... })`），请求包沿 channel 链流到 Memory，响应以 `MemRsp.data` 携带 line 原路返回，加速器从输入 channel 上读数据——与 LSU 的做法（`lsu_unit.cpp:L231-L234`）完全一致。
4. **延伸**：如果担心忘记，可建议团队引入 v3 文档 §6 提到的「可选 CI lint：拒绝新的跨模块方法调用」，从机械层面守住基数规则（该 lint 尚未实现，是 documented but not yet implemented 项）。

## 6. 本讲小结

- **基数规则**是 SimX 唯一「不可商量」的纪律：模块之间只能通过 channel 通信，绝不能跨所有权层级直接戳别的对象内部。
- **三条理由**：channel 即连线；保住时序/功能保真度与 SimX↔RTL parity；层级是所有权而非调用图。
- **v3 模型**取消了中央 `Emulator`，让指令的功能语义与计时住在同一个模块里；数据以真实载荷流过内存层次。
- **`core->mem_read/mem_write` 后门已被删除**，这两个名字如今只作为 `Processor` 的性能计数器标签（`perf_mem_reads_`/`perf_mem_writes_`）幸存。
- **`MemReq`/`MemRsp`** 是访存事务的载荷容器；LOAD 请求不带 `data`，但 LOAD 响应必须携带整条 line 的 `data`。
- **LOAD 响应必须带 line 数据**，是因为 LSU 作为叶单元只能从自己的输入 channel 读数据——`lsu_unit.cpp:L231` 的断言 `"LOAD response must carry line payload"` 是这条不变量的运行时守卫。

## 7. 下一步学习建议

本讲把「SimX 为何能当 RTL 预言机」的纪律与架构讲清楚了。接下来有三条可选路径：

1. **沿流水线下行**：进入 U6《GPU 执行模型与核心流水线（SimX 视角）》，从 u6-l1 的 warp 调度器开始，看 `MemReq`/`MemRsp` 之外的指令与寄存器载荷如何沿 channel 流动。
2. **横向对照 RTL**：跳到 u7-l4《SimX↔RTL 模型一致性（model parity）》，看本讲的「逐模块孪生」如何被 CI 门控机械验证。
3. **深入内存**：进入 u8《内存层次与缓存子系统》，看 `MemReq` 沿 coalescer → L1 → L2 → L3 → DRAM 流动时，每一级如何转发/修改这个包、MSHR 如何让多个请求共享同一个 `shared_ptr<mem_block_t>` fill buffer。

继续阅读建议：[`docs/designs/simx_simulator_architecture.md`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/simx_simulator_architecture.md) §3 的完整模块清单，以及 [`docs/simobject.md`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/simobject.md) §3 关于 `tx_callback`（本讲看到的内存计数器就是用它实现的）的说明。
