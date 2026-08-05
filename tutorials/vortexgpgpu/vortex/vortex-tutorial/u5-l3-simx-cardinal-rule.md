# 基数规则与模块分解

## 1. 本讲目标

本讲是 SimX 模拟器框架的「宪法课」。学完后你应该能够：

- 说清楚 SimX 的**基数规则（Cardinal Rule）**是什么，以及它为什么不可违反；
- 理解 SimX v3 的**模块分解原则**——为什么「ISA 语义」和「时序模型」必须住在同一个模块里，而没有一个大一统的 `Emulator`；
- 认识 `MemReq` / `MemRsp` 这两个核心数据结构，明白为什么一次 LOAD 的响应**必须**把整条 cache line 当作载荷带回来，而不是让某个单元走后门去读 DRAM；
- 把「模块只通过 channel 通信」这条规则，与上一讲（u5-l2）的 `SimPlatform` 时序轮、`SimChannel` 连线图联系起来。

本讲承接 u5-l1（三大基元）与 u5-l2（处理器层次与启动），回答一个贯穿全栈的问题：**凭什么 SimX 能当 RTL 的预言机（oracle）？** 答案就藏在「基数规则 + 模块分解」这两件事里。

---

## 2. 前置知识

阅读本讲前，请先建立以下直觉（均在 u5-l1、u5-l2 中讲过）：

- **SimObject / SimChannel / SimPlatform 三基元**：每个硬件模块是一个 `SimObject`，模块之间用带类型、带延迟、带背压的 `SimChannel` 连接，`SimPlatform` 是驱动全局 `tick()` 的时序引擎。
- **channel 就是流水线**：一条 `delay=1` 的 channel 扮演级间寄存器，流水线单元不需要自己维护内部 stage 队列。
- **处理器层次**：`Processor → Cluster → Socket → Core`，`Core` 拥有每核流水线，`Processor` 拥有全局唯一的 `Memory`（DRAM）。
- **所有权即层级**：父对象构造并拥有子对象，这种 parent→child 的拥有关系是为「生命周期/构造」服务的。

如果你对「为什么 SimX 必须和 RTL 保持功能与时序一致（model_parity）」还不熟悉，可以把本讲看作那条纪律在代码层面的根因。

> 术语提示：
> - **TLM（Transaction-Level Modeling）**：把一次访存看作一笔「事务」（带地址、数据、标签的包），而不是一根根比特线。SimX 的 `MemReq`/`MemRsp` 就是 TLM 风格的载荷。
> - **后门（back door）**：绕过被建模的硬件通路（缓存、总线），直接读写底层存储的捷径。本讲的核心论点之一就是「SimX 不允许后门」。
> - **预言机（oracle）**：调试 RTL 时，用一个被信任的参考模型给出「正确答案」来比对。SimX 之所以能当 oracle，正是因为它忠实建模了每一级延迟。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [docs/simobject.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/simobject.md) | 框架参考文档，**基数规则**的权威出处（§The Cardinal Rule）。 |
| [docs/designs/simx_simulator_architecture.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/simx_simulator_architecture.md) | SimX 架构设计文档，讲清 v3 模型（§1）、框架（§2）、组件清单（§3）。 |
| [sim/simx/types.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/types.h) | SimX 的核心类型定义，`MemReq`/`MemRsp`/`mem_block_t` 都在这里。 |
| [sim/simx/lsu_unit.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp) | LSU 单元，展示「从 `MemRsp::data` 取回 load 数据」的正确做法（不是后门）。 |
| [sim/simx/processor.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/processor.cpp) | 顶层 `Processor`，证明 `mem_read/mem_write` 如今只是性能计数器标签，不再是数据后门。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **基数规则**——模块只通过 channel 通信（对应 `simobject.md`）。
2. **v3 模块分解**——语义与时序同居一处（对应 `simx_simulator_architecture.md` §1–3）。
3. **MemReq/MemRsp 数据载荷**——把「数据当作真实载荷流过层级」这条规则落到具体结构（对应 `types.h`）。

### 4.1 基数规则：模块只通过 channel 通信

#### 4.1.1 概念说明

基数规则只有一句话：

> **Modules communicate *only* through channels.**（模块**只**通过 channel 通信。）

一个 `SimObject` 想观察或改变另一个模块的状态，**唯一的合法途径**是它被接线时分配到的那些 channel 端口（如 `MemReq`/`MemRsp`、`result_if` 等）。它**绝不允许**跨越所有权层级去直接触碰别的对象。

为什么这条规则被称作「基数（cardinal）」——即根本性的、不可妥协的？文档给出了三个理由：

1. **Channel 就是连线。** `SimChannel` 构成的图，就是 SimX 对芯片真实连线的建模。一个模块通往系统其余部分的唯一路径，就是它被接上的那组端口。绕开它们，等于在建模一块「真实硬件里根本不存在的电路」。
2. **保住时序/功能保真度与 SimX↔RTL 一致性。** 走后门读底层存储的单元，可能读到一个「在真实硅片上还卡在缓存层级里、正在传输中」的值，从而产生 RTL 永远不会产生的结果。走 channel 路径，时序模型和功能效果才一致——这正是 SimX 能当 RTL 忠实 oracle 的原因。
3. **层级是「所有权」，不是「调用图」。** `Core` 拥有它的各单元，`Processor` 拥有 `Memory`；这种父→子的拥有关系是为生命周期/构造服务的，**不能**被向上走（`child->parent()->…`）或横向走（调用兄弟模块的内部）来当作调用图用。

#### 4.1.2 核心流程

把基数规则翻译成「一个访存请求该怎么走」，就是下面这条对照：

```text
【错误：后门】                      【正确：走 channel】
LSU 单元                            LSU 单元
  │ 直接向上爬                           │ 把请求塞进自己的输出 channel
  ▼                                     ▼
core_->processor()->memsim()        out_req.try_send(MemReq{...})
  │ 直接 write_bytes 到 DRAM             │
  ▼                                     ▼  请求流过：
（绕过了 coalescer / cache / NoC）    mem_coalescer → L1 Cache → L2/L3 → Memory
                                      （和 RTL 里的连线完全一致）
```

关键在于：**「正确」路径里，请求要经过的每一级延迟，都被 channel 的 `send` 延迟和各级队列如实建模。** 而「错误」路径里，数据瞬间就落进了 DRAM，既没有缓存未命中 penalty，也没有总线仲裁延迟——这是一个 SimX-only 的幻觉，RTL 永远复现不出来。

> 一句话直觉：**在 SimX 里，「连线」就是「channel」，「绕线」就是「后门」。RTL 里你怎么布线，SimX 里就怎么 bind channel。**

#### 4.1.3 源码精读

基数规则的权威定义在 `simobject.md`，文档直接用一段「错 vs 对」的代码对比来说明（这段是本讲的「宪法条文」）：

[docs/simobject.md:L17-L34](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/simobject.md#L17-L34) — 这里先点明「模块只通过 channel 通信」，然后给出反例：一个叶子单元向上爬 `core_->processor()->memsim()` 去抓全局 `Memory`、直接 `write_bytes`，绕过了被建模的缓存路径；正例则是只驱动自己的输出 channel。

紧接着文档列出「为什么不可妥协」的三条理由：

[docs/simobject.md:L36-L50](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/simobject.md#L36-L50) — 三个 bullet 分别讲：channel 即连线、保住时序/功能保真与 SimX↔RTL 一致性、层级是所有权而非调用图。

注意反例里出现的 `core_->processor()->memsim()` 这种「向上爬」写法，正是规则第三条要禁止的：`Processor` 拥有 `Memory` 是为了构造和生命周期，**不是**给叶子单元当取数捷径用的。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是让你亲手在仓库里验证「没有叶子单元走后门」。

1. **实践目标**：确认 SimX 的访存单元不会出现「向上爬所有权层级去抓 `Memory`」的后门写法。
2. **操作步骤**：
   - 在仓库根目录执行（只读检索）：
     ```bash
     grep -rn "processor()->memsim" sim/simx/
     grep -rn "->memsim()" sim/simx/ | grep -v "processor"
     ```
   - 再检索 LSU / 功能单元里是否出现直接写 DRAM 的调用：
     ```bash
     grep -rn "write_bytes\|read_bytes" sim/simx/ | grep -E "lsu|alu|fpu|sfu"
     ```
3. **需要观察的现象**：前两条命令在叶子单元（`lsu_unit`、`alu_unit` 等）里应当**没有命中**——也就是说，没有任何一个流水线单元去向上抓 `Memory`。第三条命令同样应为空，说明没有单元绕开 channel 直接读写 DRAM 字节。
4. **预期结果**：命中集中在 `processor.cpp` / `memory.cpp` 等「合法拥有 `Memory`」的顶层模块（如果有的话），而非叶子单元。（本机检索结果待本地验证，但根据基数规则，叶子单元不应出现此类调用。）
5. **结论**：如果检索印证了「叶子单元零后门」，你就亲眼看到了基数规则在代码里的体现——它不是一句口号，而是一处可被 grep 检验的事实。

#### 4.1.5 小练习与答案

**练习 1**：假设某个新加的加速器单元为了让仿真「跑快点」，直接调用了 `processor_->memsim()->write_bytes(...)` 把结果写回 DRAM。这会破坏什么？为什么 model_parity 会失败？

> **答案**：它绕过了 coalescer / L1 / L2 / L3 与总线仲裁，因此这次写操作在 SimX 里是「零延迟落地」的；而 RTL 里同一笔写要经过完整的缓存层级和 NoC，有真实的周期代价。于是 SimX 会比 RTL 早若干周期看到这份数据，退休指令虽可能一致，但周期数会分叉，model_parity 的周期容差检查会失败。更糟的是，如果它读的是一个「还在缓存里传输中」的值，连功能结果都可能和 RTL 不同。

**练习 2**：规则说「层级是所有权，不是调用图」。请用自己的话解释 `Core` 拥有 `alu_unit`、`Processor` 拥有 `Memory`，这两种「拥有」分别是干什么用的。

> **答案**：`Core` 拥有 `alu_unit` 是为了在构造 `Core` 时一并构造并销毁它的功能单元、管理它们的生命周期；`Processor` 拥有 `Memory` 同理，是为了让全局唯一的 DRAM 与 `Processor` 同生共死。这种拥有关系只服务于「谁负责构造/析构谁」，**不**意味着子对象可以反过来 `parent()->...` 把父对象当工具调用。

---

### 4.2 v3 模块分解：语义与时序同居一处

#### 4.2.1 概念说明

基数规则回答了「模块之间怎么通信」，而 v3 模块分解回答了「模块**内部**该装什么」。

SimX v3 的定义性特征是：**没有一个中央 `Emulator`（仿真执行器）**。换句话说，不存在一个「上帝对象」负责执行所有指令的语义、再把结果告诉各个时序模块。相反，**ISA 语义就住在那个负责建模对应硬件块时序的同一个模块里**：

- ALU 和 FPU 各自拥有私有的 `execute()` 方法——运算「算什么」和「算多久」写在一起；
- SFU 是一个分派器，把指令路由到 WCTL / CSR / TEX / RASTER / DXA / OM 等子单元；
- warp / CTA / barrier 的状态住在 `Scheduler` 里；
- 寄存器堆住在 `OpcUnit` 里；
- 译码住在 `Decoder` 里。

数据则像真实载荷一样流过存储层级——cache 和 DRAM 都**带着整行数据（line data）**，**没有** `core->mem_read/mem_write` 这种后门（这两个名字如今只作为性能计数器的标签存活）。

这条原则的直接后果：**SimX 成了 RTL 的逐模块孪生体（module-by-module twin）**，因此它能担任周期级一致性调试的 oracle。

#### 4.2.2 核心流程

把 v3 模型画成一张「语义与时序同居」的对应表：

| RISC-V 指令语义 | 住在哪个 SimX 模块 | 同时建模的时序 |
| --- | --- | --- |
| 整数/分支运算 | `alu_unit` 私有 `execute()` | ALU 流水线延迟 |
| 浮点运算 | `fpu_unit` 私有 `execute()` | FPU 流水线延迟 |
| 访存 LD/ST/AMO | `lsu_unit`（从 `MemRsp::data` 取数） | LSU + 缓存层级延迟 |
| warp 控制 TMC/WSPAWN/SPLIT/JOIN/BAR | `wctl_unit` → `Scheduler` | warp 调度周期 |
| CSR 读写 | `csr_unit` | CSR 访问时序 |
| 张量核 WMMA/WGMMA | `tcu_unit`（经 sequencer 展开为多 uop） | TCU 流水线 |

整条流水线的流向（来自架构文档 §4）：

```text
Fetch (Scheduler PC + I-cache)
  → Decompress (RVC)
  → Decode
  → Sequence (uop 展开)
  → I-buffer
  → Scoreboard 发射门控
  → Operands / OpcUnit 寄存器读取
  → Dispatcher
  → FuncUnit lanes (ALU / FPU / LSU / SFU{...} / TCU)
  → commit 仲裁
  → OpcUnit 写回

访存支路：LSU → mem_coalescer → L1 Cache → L2/L3 cache_cluster → Memory(DRAM)
```

注意访存支路里**每一级都搬运真实数据载荷**，这正是下一个模块（`MemReq`/`MemRsp`）要展开的内容。

#### 4.2.3 源码精读

v3 模型的权威表述在架构文档 §1：

[docs/designs/simx_simulator_architecture.md:L15-L29](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/simx_simulator_architecture.md#L15-L29) — 点明「没有中央 `Emulator`」，ISA 语义与对应硬件块的时序住在同一模块；并明确「数据作为真实载荷流过存储层级，不存在 `core->mem_read/mem_write` 后门（这两个名字仅作为性能计数器标签存活）」。

「`mem_read/mem_write` 只是计数器标签」这句话很容易被误读，我们到 `processor.cpp` 里验证它。`Processor` 在内存 channel 上挂了一个 `tx_callback`（关于 `tx_callback` 见 u5-l1 §3），它**只是数读写笔数**，并不搬运数据：

[sim/simx/processor.cpp:L119-L123](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/processor.cpp#L119-L123) — `memsim_->mem_req_in.at(i).tx_callback(...)` 里只做 `perf_mem_reads_ += !req.is_write(); perf_mem_writes_ += req.is_write();`，纯粹是窥探总线、累加计数器。

这些计数器随后作为性能统计暴露：

[sim/simx/processor.cpp:L330-L335](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/processor.cpp#L330-L335) — `perf.mem_reads = perf_mem_reads_; perf.mem_writes = perf_mem_writes_;`，证实它们是给性能报告用的标签，而不是数据通路。

组件清单（§3）则把「每个语义住在哪个文件」逐一列清，是后续讲义（u6 系列）的导览：

[docs/designs/simx_simulator_architecture.md:L51-L104](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/simx_simulator_architecture.md#L51-L104) — 按「编排 / 每核流水线 / 功能单元 / 存储」四类列出所有组件，并指出 `MemReq`/`MemRsp` 携带 `shared_ptr<mem_block_t> data` + `byteen`，LOAD 响应必须带行载荷。

#### 4.2.4 代码实践

1. **实践目标**：用 `tx_callback` 这个框架钩子，体会在「不插入新模块、不走后门」的前提下，如何观察一条 channel 上的流量。
2. **操作步骤**：
   - 阅读 `simobject.md` §3 里 `tx_callback` 的三个示例（计数读写、记录 trace、测量延迟）。
   - 在 `processor.cpp` 的 `tx_callback` 处（如上引用）加一行临时日志，例如打印每次访存请求的 `op` 与 `addr`（**仅在本地学习时修改，勿提交；这属于读源码型实践**）。
3. **需要观察的现象**：程序运行后，你会看到一笔笔 `MemReq` 流经顶层内存 channel，印证「数据是作为载荷流过 channel 的，而不是被某个单元直接读写」。
4. **预期结果**：日志里出现的都是带 `op`/`addr`/`tag` 的完整事务包，而**不会**出现任何模块直接 `write_bytes` 到 DRAM 的调用。
5. **结论**：`tx_callback` 是框架提供的「合法窥探口」——你可以观察流量，但观察行为本身不构成数据通路，也不会破坏时序模型。这正是「模块只通过 channel 通信」之下，框架留给你的合规 instrumentation 手段。

#### 4.2.5 小练习与答案

**练习 1**：为什么 v3 要刻意**消灭**中央 `Emulator`？把它和「基数规则」放在一起解释。

> **答案**：如果有一个中央 `Emulator` 负责算所有指令语义，那它必然要绕过各模块的 channel、直接读写寄存器堆和内存——这正是基数规则禁止的「后门」。把语义下放到各功能单元、让数据和状态作为载荷与通道事件流过层级，每个单元就只通过自己的 channel 端口与外界交互，基数规则得以成立。两者其实是同一件事的两面：模块分解决定了「语义住哪」，基数规则决定了「模块之间怎么连」。

**练习 2**：架构文档说 `mem_read/mem_write` 这两个名字「仅作为性能计数器标签存活」。请到源码里确认它们今天到底干什么。

> **答案**：在 `processor.cpp` 里，`perf_mem_reads_` / `perf_mem_writes_` 是通过内存 channel 上的 `tx_callback` 累加的计数器（L119–L123），并在 `perf_stats()` 里作为 `perf.mem_reads` / `perf.mem_writes` 暴露（L332–L333）。它们**不**搬运任何数据——数据搬运由 `MemReq`/`MemRsp` 的载荷完成。所以这两个名字是「历史遗留的标签」，不再是数据后门。

---

### 4.3 MemReq/MemRsp：作为真实载荷的数据通路

#### 4.3.1 概念说明

前两个模块讲了「规则」，这个模块讲「规则在数据结构上的落地」。`MemReq`（访存请求）和 `MemRsp`（访存响应）是 SimX 存储层级的**通用事务包**，它们在 `types.h` 里定义，被 LSU、coalescer、各级 cache、DRAM 反复搬运。

理解它们的关窍只有一个：**数据是载荷（payload），不是后门查询。**

- 一次 **STORE**：LSU 把要写的数据塞进 `MemReq::data`，这个包一路流到 DRAM，沿途每一级都看到这份数据。
- 一次 **LOAD**：LSU 发出一个**不含数据**的 `MemReq`；请求流到 DRAM 后，DRAM 把整条 cache line 塞进 `MemRsp::data`，这个响应包再一路流回 LSU。LSU 从响应载荷里 `memcpy` 出自己需要的字节。

**为什么 LOAD 响应必须把整行数据带回来，而不是让 LSU 走 `core->mem_read(addr)` 后门去现读？** 因为后门会绕开「请求 → 缓存未命中 → MSHR → 总线 → DRAM → 返回」这一整条被建模的延迟链。一旦走了后门，这次 load 在 SimX 里就是零延迟的，而 RTL 里同一笔 load 要付完整的命中/未命中代价——model_parity 立刻崩坏。把数据装进 `MemRsp` 当载荷送回，延迟就被「请求发出的 cycle」到「响应到达的 cycle」之间的 channel 事件如实记录了。

#### 4.3.2 核心流程

一次 LOAD 在存储层级里的往返，可用下面的时序描述（cycle 编号为示意）：

```text
cycle c0 : LSU 发出 MemReq{op=LD, addr=A, tag=T}        ← 不含 data
            经 mem_coalescer → L1 → (miss) → L2 → ... → Memory
cycle c0..ck: 请求在各级 channel 上传播，每级付 delay
cycle ck  : Memory(DRAM) 收到请求，读出整行，发出
            MemRsp{tag=T, data=<整行字节>}
cycle ck..cn: 响应沿原路返回，data 载荷随行
cycle cn  : LSU 收到 MemRsp，从 data 里 memcpy 出所需字节
```

LOAD 取回字节时的块内偏移用一个简单的按位与计算：

\[
\text{off} = \text{addr}\ \&\ ( \text{VX\_CFG\_MEM\_BLOCK\_SIZE} - 1 )
\]

这里 `VX_CFG_MEM_BLOCK_SIZE` 是一个 cache line（传输块）的字节数，且是 2 的幂，所以 `& (size-1)` 等价于「取模得块内偏移」。

> 关于 `shared_ptr<mem_block_t>` 的设计巧思：`mem_block_t` 是 `std::array<uint8_t, VX_CFG_MEM_BLOCK_SIZE>`，而请求/响应里存的是它的 `shared_ptr`。这样当多个 MSHR 合并的请求命中同一个 fill buffer 时，它们可以**共享同一份行数据而无需拷贝**——既省内存又省时间，注释里写得很明白（见源码精读）。

#### 4.3.3 源码精读

先看「一个传输块」的定义，它是所有数据载荷的载体：

[sim/simx/types.h:L50-L53](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/types.h#L50-L53) — `using mem_block_t = std::array<uint8_t, VX_CFG_MEM_BLOCK_SIZE>;`，注释说明它由 `MemReq`/`MemRsp` 在 TLM 数据通路模式下携带，用 `shared_ptr` 是为了让 MSHR 合并的重放共享同一个 fill buffer 而不拷贝。

再看 `MemReq`——注意它**既有 `data` 也有 `byteen`**，故既能承载写数据，也能精确描述哪些字节有效：

[sim/simx/types.h:L1226-L1265](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/types.h#L1226-L1265) — `struct MemReq`，字段依次为 `op`（`MemOp` 枚举：LD/ST/FLUSH/各 AMO）、`addr`、`data`（`shared_ptr<mem_block_t>`）、`byteen`（字节使能掩码）、`tag`、`hart_id`、`uuid`、`flags`。一个 LD 请求发出时 `data` 可为空，等响应回来才有数据。

然后是 `MemRsp`——它的关键字段就是 `data`：

[sim/simx/types.h:L1269-L1290](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/types.h#L1269-L1290) — `struct MemRsp`，含 `tag`、`hart_id`、`uuid`、`data`（`shared_ptr<mem_block_t>`）。响应靠 `tag` 与请求配对，靠 `data` 把整行字节送回请求方。

`MemOp` 枚举把「访存做什么」压缩到一个字节里，方便在请求包里携带：

[sim/simx/types.h:L393-L410](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/types.h#L393-L410) — `enum class MemOp`，低值是始终存在的 LD/ST/FLUSH，3..11 是原子族（仅当 `VX_CFG_EXT_A_ENABLE` 时有意义）。

最后是「规则被代码强制」的实锤——LSU 在消费 LOAD 响应时，**断言**响应必须带载荷，然后直接从载荷 `memcpy`：

[sim/simx/lsu_unit.cpp:L227-L234](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/lsu_unit.cpp#L227-L234) — 第 231 行 `assert(lsu_rsp.data.at(lane) && "LOAD response must carry line payload");`，第 232 行算块内偏移 `off = lane_info.addr & (VX_CFG_MEM_BLOCK_SIZE - 1);`，第 234 行 `std::memcpy(&read_data, lsu_rsp.data.at(lane)->data() + off, data_bytes);`。LSU 全程没有调任何 `mem_read` 后门，它只从 channel 送来的响应载荷里取字节。

> 把这条引用和 4.1 的基数规则放在一起看：第 231 行的 `assert` 不是防御性编程，而是**基数规则的运行时契约**——如果有人某天把 LOAD 改成走后门、响应不再带 `data`，这条断言会立刻在仿真时炸掉，把违规暴露出来。

#### 4.3.4 代码实践（本讲主实践）

1. **实践目标**：在 `types.h` 中定位 `MemReq` / `MemRsp` / `mem_block_t`，并结合 `lsu_unit.cpp` 解释「为什么 LOAD 响应必须携带 line 数据载荷，而不是通过 `core->mem_read` 后门」。
2. **操作步骤**：
   - 打开 `sim/simx/types.h`，找到三处定义：
     - `mem_block_t`（L50–L53）：一行 `std::array<uint8_t, VX_CFG_MEM_BLOCK_SIZE>`。
     - `MemReq`（L1226–L1265）：确认它有 `data` 与 `byteen` 字段。
     - `MemRsp`（L1269–L1290）：确认它**只有** `tag/hart_id/uuid/data`，`data` 是唯一的「数据出口」。
   - 打开 `sim/simx/lsu_unit.cpp` 第 227–234 行，看清 LSU 取数的全过程：先 `assert` 载荷非空，再算块内偏移，再 `memcpy`。
   - 对照阅读架构文档 §1（L15–L29）那句「没有 `core->mem_read/mem_write` 后门」。
3. **需要观察的现象**：`MemRsp` 里没有任何「请对方来读我」的回调或指针，只有被装好的 `data`；`MemReq` 的 LD 分支也不携带写数据。数据流向是**单向的载荷搬运**，而非双向的方法调用。
4. **预期结果**：你能用一句话回答主实践问题——
   > 「因为后门会绕开被建模的缓存/总线延迟，让 SimX 的 load 变成零延迟，从而破坏 SimX↔RTL 的周期一致性（model_parity）；把整行数据装进 `MemRsp` 当载荷送回，延迟就由 channel 事件如实记录，LSU 只需 `memcpy`（L234），并由 `assert`（L231）强制载荷存在。」
5. **若想进一步验证**（待本地验证）：在一个已配好的 build 目录里，用 `--driver=simx` 跑一个简单 load 测试（如 `tests/regression/demo`），观察 trace 里一笔 LD 的 `MemReq`（无 data）与对应的 `MemRsp`（带 data）的 cycle 差，体会「载荷往返 = 真实延迟」。

#### 4.3.5 小练习与答案

**练习 1**：`MemReq` 和 `MemRsp` 都用 `shared_ptr<mem_block_t>` 而不是直接存 `mem_block_t`（值拷贝）。除了省去大块拷贝，还有什么建模上的好处？

> **答案**：当一个 cache miss 触发 MSHR、且多个 LSU 请求合并到同一 fill buffer 时，它们的重放可以共享同一份 `mem_block_t`——`shared_ptr` 让「同一行数据被多个消费者读取」变成零拷贝的引用共享。这既贴近真实硬件里「一个 fill buffer 被多个等待者同时唤醒」的语义，又避免了逐字节复制整条 cache line 的开销。

**练习 2**：假如把 `MemRsp::data` 删掉，改让 LSU 在收到响应后调用 `core_->memory()->read(addr)` 去取数，从基数规则和 model_parity 两个角度分别说会出什么问题。

> **答案**：① 违反基数规则——LSU 向上爬所有权层级去调用兄弟模块 `Memory` 的内部方法，属于明令禁止的后门。② 破坏 model_parity——这次 `read` 是同步的、零延迟的函数调用，而 RTL 里同一笔 load 的数据要经过缓存命中/未命中、MSHR、总线仲裁才能回到 LSU；于是 SimX 的周期数会偏小，与 RTL 的退休周期不再吻合，容差检查失败。

**练习 3**：`MemReq::byteen`（字节使能）存在的意义是什么？提示：联系 4.3.2 里的块内偏移。

> **答案**：一次访存未必覆盖整条 cache line——可能只写一个字节、半字或一个字。`byteen` 用位掩码标出 `data` 里**哪些字节是有效的**，让 STORE、AMO、部分写都能在一行之内精确表达，而 `off = addr & (BLOCK_SIZE-1)` 则定位这些字节在行内的起点。两者配合，才让「一行数据 + 一次事务」足以表达任意宽度的访存。

---

## 5. 综合实践

把本讲三个模块串起来，做一次**全链路追踪**。

**任务**：追踪一次 LOAD 从 LSU 发出到数据写回寄存器的完整路径，证明沿途**没有任何一个模块走后门**，数据始终作为载荷在 channel 上流动。

**建议步骤**：

1. **起点**：在 `sim/simx/lsu_unit.cpp`（L227–L234）确认 LSU 从 `lsu_rsp.data` 取数；向上找它把 `LsuReq` 发出的位置（LSU 的输出 channel）。
2. **中段**：沿架构文档 §4（L107–L115）给出的访存支路 `LSU → mem_coalescer → L1 Cache → L2/L3 cache_cluster → Memory`，在 `sim/simx/mem/` 下找到对应的 `mem_coalescer.cpp`、`cache.cpp`、`cache_cluster.cpp`、`memory.cpp`，确认每一级都接收 `MemReq`、回送 `MemRsp`，且 `MemRsp` 带 `data`。
3. **终点**：回到 LSU，确认它收到 `MemRsp` 后用 `memcpy`（L234）取字节并写回寄存器堆。
4. **验证基数规则**：在你追踪的每个文件里 grep `processor()->` / `->memsim()` / `write_bytes`，确认它们都没有向上爬所有权层级。
5. **产出**：画一张时序图，横轴是 cycle，纵轴是 `LSU → coalescer → L1 → L2 → Memory → (原路返回) → LSU`，标注 `MemReq`（去程，无 data）与 `MemRsp`（回程，带 data）在哪些 channel 上传播、各级付了多少 delay。

**预期收获**：你会直观看到——SimX 之所以能当 RTL 的 oracle，不是因为它「算得对」，而是因为它**连得对**：数据走过的每一级、付出的每一拍延迟，都和 RTL 的连线一一对应。这正是基数规则与 v3 模块分解共同守护的东西。

---

## 6. 本讲小结

- **基数规则**：模块**只**通过 `SimChannel` 通信，绝不跨越所有权层级走后门；channel 就是连线，绕线就是后门。
- **走后门的代价**：会读/写「还在传输中」的值，让 SimX 产生 RTL 复现不出来的结果，直接破坏 model_parity。
- **v3 模块分解**：没有中央 `Emulator`，ISA 语义住在建模对应硬件块时序的**同一个模块**里（ALU/FPU 私有 `execute()`、warp 状态在 `Scheduler`、寄存器堆在 `OpcUnit`）。
- **历史标签**：`mem_read/mem_write` 如今只是 `processor.cpp` 里由 `tx_callback` 累加的性能计数器，不再是数据后门。
- **数据即载荷**：`MemReq`/`MemRsp` 用 `shared_ptr<mem_block_t>` 携带整行数据，LOAD 响应必须带 `data`——`lsu_unit.cpp:231` 的 `assert` 把这条规则变成了运行时契约。
- **合规窥探**：需要观察流量时，用 `tx_callback`，而不是插入新模块或开后门。

---

## 7. 下一步学习建议

本讲确立了「模块只通过 channel 通信」这条总纲，接下来可以沿着两条线深入：

- **往下看流水线内部**：进入 u6-l1（Warp 调度器、CTA 派发与屏障），你会看到 `Scheduler`/`CtaDispatcher`/`BarrierUnit` 如何严格遵守基数规则，通过 channel 驱动 warp 生命周期。建议同时翻架构文档 §3 的「Per-core pipeline」清单，把每个组件对应到即将讲的模块。
- **往深看存储层级**：进入 u8 系列（内存层次与缓存子系统），重点读 `sim/simx/mem/cache.cpp` 与 `memory.cpp`，看 cache 的 tags/MSHR/替换策略如何把 `MemReq` 变成 `MemRsp`、把整行数据装进响应载荷。

如果你对「SimX 如何被 RTL 用来做一致性门控」感兴趣，可以先跳到 u7-l4（SimX↔RTL model parity），再回头看本讲，你会更深刻地理解「为什么基数规则不可妥协」。
