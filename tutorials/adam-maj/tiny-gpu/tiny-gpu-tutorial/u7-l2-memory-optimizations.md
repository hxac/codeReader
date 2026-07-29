# 内存优化与缓存

## 1. 本讲目标

本讲是 Unit 7 的第二篇，承接 [u3-l2 内存控制器：带宽节流](u3-l2-memory-controller.md)，把视角从「控制器怎么节流」拉升到「真实 GPU 如何系统地缓解内存瓶颈」。

读完本讲，你应当能够：

- 说清 **缓存（cache）** 解决什么问题、命中率（hit rate）为什么是核心指标；
- 理解 **多层缓存（L1/L2）** 的层次结构与平均访问时间（AMAT）公式；
- 掌握 **内存合并（memory coalescing）** 的原理，并能用文字 + 伪代码画出一个合并器草图；
- 理解 **共享内存（shared memory）** 与 **屏障同步（barrier）** 的作用，以及 tiny-gpu 为什么两者都没有；
- 用 `NUM_CONSUMERS / NUM_CHANNELS` 公式定量分析 tiny-gpu 控制器的带宽瓶颈，并解释它如何拖长 scheduler 的 WAIT 阶段。

本讲会反复对照「真实 GPU 怎么做」与「tiny-gpu 实际有什么」，因此会多次回到源码核对，**不轻信 README 的描述性文字**。

## 2. 前置知识

本讲默认你已掌握以下内容（来自前置讲义）：

- **多通道 valid/ready 握手**：请求类信号（valid/address/data）由 GPU 驱出，应答类信号（ready/read_data）由外部驱动回（u3-l1）。
- **控制器五态状态机** `IDLE / READ_WAITING / WRITE_WAITING / READ_RELAYING / WRITE_RELAYING`，每条通道一拍只拾取一个消费者（u3-l2）。
- **scheduler 的七阶段**与 **WAIT 阶段**：WAIT 用 `for`+`break` 轮询所有 LSU，任一未完成就原地等待，因此「最慢的线程」决定整条指令的耗时（u4-l2）。
- **SIMD 单指令流**：同一拍内所有使能线程执行同一条指令（如同一条 `LDR`），它们会在同一拍向控制器发起请求（u4-l1）。

几个通俗概念先建立直觉：

- **SRAM vs DRAM**：SRAM（静态随机存储）速度快、容量小、贵，适合做片上缓存；DRAM（动态随机存储，即「全局内存 / 显存」）容量大、速度慢、便宜。缓存的本质是「用小块快速 SRAM 挡在慢速 DRAM 前面」。
- **局部性（locality）**：程序倾向于反复访问同一块数据（时间局部性）和访问地址相近的数据（空间局部性），这正是缓存能命中的根因。
- **带宽（bandwidth）**：单位时间能搬运的数据量，单位通常是「事务数/周期」或「字节/秒」。带宽有限，是 GPU 的头号瓶颈。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用它做什么 |
|------|------|----------------|
| [src/controller.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv) | 内存控制器：消费者 ↔ 外部内存的中继仲裁器 | 分析 `NUM_CHANNELS` 节流逻辑、定位合并器的插入点 |
| [src/gpu.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv) | 顶层：实例化两个控制器与多个 core | 核对控制器实例化参数（8→4、2→1 的过订阅比） |
| [src/decoder.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv) | 指令译码器，定义全部操作码 | 证明 ISA 没有任何 barrier/sync 指令 |
| README.md | 项目说明 | 核对 README 对 cache/shared memory/coalescing/barrier 的「设想」与源码实现的差距 |

> 提醒：本讲会引用 README 的「Advanced Functionality」章节，但 README 在缓存一段的措辞与源码不符，我们会在 4.1.3 用源码纠正它。

## 4. 核心概念与源码讲解

### 4.1 单层 cache 现状：README 的设想 vs 源码的真实

#### 4.1.1 概念说明

全局内存（DRAM）很慢且带宽有限。如果同一段数据被反复读取，每次都去外部内存搬一遍，既浪费带宽又拖慢执行。**缓存（cache）** 的思路是：把最近从外部内存取回来的数据存一份在片上 SRAM 里，下次再要时直接从 SRAM 取，不再占用宝贵的外部带宽。

衡量缓存好坏的核心指标是 **命中率（hit rate）**：

\[ h = \frac{\text{命中次数}}{\text{总访问次数}} \]

命中率越高，平均访问延迟越低，外部带宽越省。正因为缓存对 GPU 性能至关重要，README 把它列为 GPU 的基本组成单元之一。

#### 4.1.2 核心流程

一次带缓存的读取，流程是：

1. 计算单元（LSU）发起读请求；
2. 先查缓存：用地址匹配缓存里的标签（tag）；
3. **命中（hit）**：直接从缓存返回数据，不触碰外部内存；
4. **未命中（miss）**：向外部内存发请求，数据返回后 **填入缓存**，再返回给计算单元。

伪代码：

```
def cached_read(address):
    if address in cache:          # tag match
        return cache[address]     # hit: 快速路径
    data = global_mem.read(address)   # miss: 走外部
    cache.fill(address, data)     # 填充，供下次命中
    return data
```

关键直觉：**miss 越少，缓存越值**。所有缓存优化（替换策略、预取、合并）本质上都在压低 miss 率或摊薄 miss 代价。

#### 4.1.3 源码精读

README 在两处描述了缓存。先把「GPU 组成单元」列出来：

> GPU itself consists of: … 5. Cache —— 见 [README.md:75-82](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L75-L82)

以及专门的 WIP 小节，解释缓存的作用与动机：

[README.md:121-125](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L121-L125) —— 标题就叫 `### Cache (WIP)`，说明这是「待实现」的设想，并解释「重复访问全局内存很贵，所以把数据缓存到片上 SRAM」。

**但是源码里根本没有缓存。** 我们用三个证据交叉验证：

1. **没有 cache 模块文件**。`src/` 下只有 12 个 `.sv` 文件（`alu/lsu/registers/core/fetcher/dcr/decoder/controller/pc/gpu/scheduler/dispatch`），其中没有 `cache.sv`。
2. **顶层没有实例化任何缓存**。`gpu.sv` 只实例化了 `dcr`、两个 `controller`、`dispatch` 和 `core`，数据通路是「LSU → 控制器 → 外部内存」直达，中间没有缓存环节。
3. **控制器本身只做中继，不做缓存**。控制器的请求路径是 `consumer_*` 进、`mem_*` 出，没有任何「先查本地缓存」的逻辑——它原样转发每个请求：

[src/controller.sv:97-113](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L97-L113) —— `READ_WAITING` 等到外部内存应答后，直接把 `mem_read_data` 中继回 `consumer_read_data`，全程无缓存查找。

这与 README「Advanced Functionality」里的一句措辞冲突：

[README.md:338-346](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L338-L346) —— 原文称「tiny-gpu implements only one cache layer … which stores recent cached data」，即声称已实现一层缓存。

**结论（已用源码纠正）**：tiny-gpu 实际实现了 **零层缓存**。README 这句是前瞻性描述（aspirational），与源码不符。「Cache (WIP)」的 WIP 标注才是真实状态；Next Steps 里也把缓存列为待办：

[README.md:384-388](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L384-L388) —— `- [ ] Add a simple cache for instructions`、`- [ ] Add basic memory coalescing` 都还是未勾选的 TODO。

> 这正是本讲反复强调的方法论：**README 的描述要回源码验证**。读开源硬件项目时，「文档说有」≠「代码里有」。

#### 4.1.4 代码实践

**实践目标**：亲手确认「tiny-gpu 没有缓存实现」，并定位缓存未来该插在哪一层。

**操作步骤**：

1. 在项目根目录确认 `src/` 下没有缓存文件：用 `Glob` 搜索 `src/*.sv`，核对清单里无 `cache.sv`。
2. 用 `Grep` 在 `src/` 内搜 `cache`（不区分大小写），预期返回「无匹配」。
3. 打开 [src/gpu.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv)，在数据控制器实例化处（`data_memory_controller`，约 86–112 行）画出当前的请求路径：`core 的 LSU → lsu_* 信号 → data_memory_controller → data_mem_* → 外部`。
4. 在图上标出一个「data cache」方块应插入的位置（例如夹在 `lsu_*` 与控制器之间，或夹在控制器与 `data_mem_*` 之间）。

**需要观察的现象**：第 2 步 grep 应当无任何输出，证明源码里既没有 cache 模块、也没有任何变量名带 cache。

**预期结果**：确认「零层缓存」属实；并得出插入点建议——最简单的指令缓存可挂在 fetcher 与 program 控制器之间（呼应 README「Add a simple cache for **instructions**」），数据缓存则挂在 LSU 与 data 控制器之间。

#### 4.1.5 小练习与答案

**练习 1**：为什么 README 把缓存归为 GPU 的「基本单元」，而源码里却没有？这矛盾吗？

> **答案**：不矛盾。「基本单元」是从架构教学角度划分的逻辑组成（一台 GPU「应当」有缓存），而源码处于 WIP 阶段，缓存尚未落地。文档描述的是设计蓝图，代码描述的是当前实现，两者时间维度不同。

**练习 2**：如果只能加一个缓存，加「指令缓存」还是「数据缓存」性价比更高？结合 scheduler 行为说明。

> **答案**：通常加指令缓存更划算。一个 block 内所有线程跑同一段代码、PC 从 0 递增，指令地址高度重复（时间 + 空间局部性都极强），命中率接近 100%；而数据地址随线程变化，命中率较低。README 的 TODO 也是先列「cache for instructions」。

---

### 4.2 多层缓存设计

#### 4.2.1 概念说明

一层缓存不够时，真实 GPU 用 **多层缓存（multi-layered cache）**：越靠近计算单元的层越小越快（L1），越远的层越大越慢（L2、L3，最终是全局内存/显存）。典型布局：

- **L1 缓存**：每个 core 私有，容量小、延迟最低，紧贴 ALU/LSU；
- **L2 缓存**：多个 core 共享，容量更大、延迟略高；
- **全局内存**：所有 core 共享的 DRAM，容量最大、延迟最高。

多层缓存的收益来自 **局部性的分层利用**：最热的数据留在 L1，次热的留在 L2，冷数据才落回 DRAM。

#### 4.2.2 核心流程

一次读取在多层结构里逐级下探：

```
L1 命中? ──是──> 返回（最快）
   │否
L2 命中? ──是──> 填充 L1，返回
   │否
全局内存 ──> 填充 L2、L1，返回（最慢）
```

衡量多层缓存的经典指标是 **平均内存访问时间（AMAT, Average Memory Access Time）**，其递推公式为：

\[ \text{AMAT} = T_{L1\_hit} + \text{MissRate}_{L1} \cdot \text{MissPenalty}_{L1} \]

其中第一层的 miss 代价又递推到下一层：

\[ \text{MissPenalty}_{L1} = T_{L2\_hit} + \text{MissRate}_{L2} \cdot T_{\text{global}} \]

直觉：只要每层命中率足够高，AMAT 就会逼近最快的 L1 延迟，外部带宽压力随之大幅下降。

#### 4.2.3 源码精读

tiny-gpu 目前是「零层缓存」，所以多层结构纯属设计展望。但我们可以对照源码说清每一层未来该挂在哪里：

- **L1（per-core 私有）** 应挂在每个 core 内部、LSU 与控制器之间。当前 core 内每线程有一份 LSU（见 [src/gpu.sv:206-213](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L206-L213) 的 `data_mem_*` 端口），L1 数据缓存可放在这条通路上。
- **L2（多核共享）** 应挂在控制器与外部内存之间，即 [src/controller.sv:28-36](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L28-L36) 的 `mem_*` 接口那一侧，所有 core 经控制器统一访问。

README 的「Multi-layered Cache & Shared Memory」一节正是这个方向：

[README.md:338-346](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L338-L346) —— 指出多层缓存让「频繁访问的数据离使用点更近」，并强调「不同的缓存替换算法是优化内存访问的关键维度」。这与 AMAT 公式里「压低 miss 率」完全对应。

#### 4.2.4 代码实践

**实践目标**：用 AMAT 公式量化「加一层缓存」的收益。

**操作步骤**：假设给 tiny-gpu 加一层 L1 数据缓存，参数为：

- L1 命中延迟 \( T_{L1\_hit} = 1 \) 周期；
- L1 命中率 \( h_1 = 0.8 \)；
- 当前无缓存时，每次访存走外部内存，延迟 \( T_{\text{global}} = 5 \) 周期（控制器中继约 3 拍 + 外部往返，估计值）。

**需要观察的现象**：分别计算「无缓存」与「有 L1」两种情况的 AMAT，并算出加速比。

**预期结果（手工计算）**：

- 无缓存：\(\text{AMAT}_{\text{无}} = T_{\text{global}} = 5\) 周期。
- 有 L1：\(\text{AMAT}_{\text{L1}} = 1 + (1 - 0.8) \times 5 = 1 + 1 = 2\) 周期。
- 加速比 \( 5 / 2 = 2.5\times \)。

> 说明：上述延迟是便于计算的假设值；控制器中继的精确拍数待本地仿真验证（见 4.5）。

#### 4.2.5 小练习与答案

**练习 1**：若把 L1 命中率从 0.8 提升到 0.9，AMAT 从 2 降到多少？这说明什么？

> **答案**：\(\text{AMAT} = 1 + 0.1 \times 5 = 1.5\)。命中率每提高 10 个百分点，miss 代价被进一步摊薄，说明「缓存替换算法」是高杠杆优化点——这正是 README 强调的「关键维度」。

**练习 2**：为什么 L1 设计成「每核私有」而 L2 设计成「多核共享」？

> **答案**：私有 L1 贴近计算单元、延迟最低，但容量受限；共享 L2 容量大、能缓存更多数据并让多核复用同一份热数据（例如矩阵分块），用「稍高延迟换大容量」。这是延迟与容量的分层折中。

---

### 4.3 内存合并（memory coalescing）

#### 4.3.1 概念说明

SIMD 模型下，同一拍内所有线程执行同一条访存指令。一个非常常见的访问模式是 **顺序访问**：线程 0 读 `A[0]`、线程 1 读 `A[1]`、线程 2 读 `A[2]`…… 例如矩阵加法里每个线程读 `A[i]`。

如果这些「相邻地址」的请求被一个个单独发到外部内存，就得发起 N 次独立事务、做 N 次寻址，浪费严重。**内存合并（memory coalescing）** 的思路是：在发往外部内存之前，先分析排队中的请求，把 **落在同一「合并行」里的相邻请求拼成一次事务**，一次搬回一段连续数据，再拆给各个线程。

#### 4.3.2 核心流程

合并器（coalescer）逻辑上分两步：

1. **合并（发出方向）**：收集本拍所有 pending 读请求，按「合并行」分组。同一行内的多个请求合成一次外部事务，附带一个掩码说明要哪些字节。
2. **分发（返回方向）**：宽事务数据返回后，按掩码把对应字节拆回给每个原始请求者。

```
# 合并：每拍在控制器拾取请求「之前」运行
def coalesce(pending_reads):              # pending_reads: [(consumer_id, address)]
    groups = {}                            # line_base -> [(consumer_id, address)]
    for (cid, addr) in pending_reads:
        line_base = addr & ~0b11           # 4 字节对齐的合并行（需外部内存支持更宽/突发访问）
        groups.setdefault(line_base, []).append((cid, addr))
    txns = []
    for line_base, members in groups.items():
        mask = sum(1 << (a & 0b11) for (_, a) in members)   # 字节使能掩码
        txns.append(Txn(base=line_base, mask=mask, members=members))
    return txns                            # 每个 Txn 占用「一条」外部通道

# 分发：宽数据返回后拆回
def demux(line_data, txn):                 # line_data: 4 字节
    for (cid, addr) in txn.members:
        consumer_read_data[cid] = line_data[addr & 0b11]
        consumer_read_ready[cid] = 1
```

直觉：N 个相邻请求从「N 次事务」压成「1 次事务」（若 N ≤ 合并行宽度），外部带宽占用直接除以 N。

#### 4.3.3 源码精读

合并器之所以有价值，根源在控制器的 **「一通道一拍一请求」** 节流规则。看 IDLE 状态如何拾取请求：

[src/controller.sv:68-96](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L68-L96) —— 每条通道在 IDLE 里用 `for j` 扫描消费者，一旦发现 `consumer_read_valid[j]`（且未被别的通道领走），就 `break` 停下，**这一拍这条通道只服务这一个消费者**。

把这条规则代入 SIMD 场景：8 个 LSU 在同一拍（REQUEST 阶段）同时拉高 `read_valid`、地址分别是 `baseA+0 … baseA+7`（连续），但 data 控制器只有 4 条通道，于是：

- 第 1 波：通道 0–3 分别领走地址 `+0, +1, +2, +3`；
- 地址 `+4 … +7` 的 4 个请求只能排队等通道空闲；
- 这 4 个相邻请求本可以合并成 1 次「读 4 字节」事务，却因为无合并逻辑而各占一条通道、各发一次外部事务。

README 的「Memory Coalescing」一节描述的正是这个优化：

[README.md:348-352](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L348-L352) —— 「分析排队中的请求，把相邻请求合并成单次事务，减少寻址开销」。它也仍是 TODO：

[README.md:384-388](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L384-L388) —— `- [ ] Add basic memory coalescing`。

> **一个必须说清的约束**：tiny-gpu 当前外部数据内存是「8 位地址、8 位数据、一次一事一字节」。合并若要真正减少事务数，**必须先改造外部内存接口**——要么加宽数据通路（例如一条通道返回 4 字节），要么引入突发（burst）传输模式。否则「合并 4 个字节请求」在字节级接口下仍是 4 次寻址，省不下事务数。合并器是必要条件，外部接口支持宽/突发事务才是充分条件。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：基于 `controller.sv` 的 `NUM_CHANNELS` 节流逻辑，设计一个「内存合并」改进草图，说明多个 LSU 请求连续地址时如何合并成一次外部事务。

**操作步骤**：

1. **阅读现状**：再读 [src/controller.sv:68-96](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L68-L96)，确认 IDLE 的「一通道一拍一请求 + break」规则。
2. **定位插入点**：合并器应插在 `consumer_*`（消费者侧）与现有 IDLE 拾取逻辑之间——先把 8 个 LSU 的请求合并成若干「行事务」，再让通道去拾取「行事务」而非「单字节请求」。
3. **写出合并器伪代码**（已在 4.3.2 给出 `coalesce` / `demux` 骨架），并补充以下三件事：
   - **行宽选择**：假设合并行 = 4 字节（与 4 通道对应），地址 `addr & ~0b11` 得到行基址；
   - **回退策略**：若两个请求落在不同行、或同一行但超过行宽，则不合并，各自走独立事务（功能正确性优先）；
   - **接口改造声明**：明确写出「需要把 `mem_read_data` 加宽到 32 位 / 或新增 `mem_read_burst` 信号」，否则合并无收益。
4. **估算收益**：对「8 个 LSU 读连续地址」的场景，比较改造前后占用通道数与外部事务数。

**需要观察的现象（设计层面）**：改造前 8 个连续地址需要 8 次外部事务、分两波占满 4 条通道；改造后（行宽 4）只需 2 次行事务、一波即可发完。

**预期结果**：

- 改造前：外部事务数 = 8，通道占用 = 2 波；
- 改造后：外部事务数 = 2（地址 0–3 一行、4–7 一行），通道占用 = 1 波；
- 事务数降为 1/4，data 控制器的排队压力相应下降，scheduler 的 WAIT 阶段随之缩短（定量见 4.5）。

**待本地验证**：精确的拍数节省取决于合并器自身延迟与外部内存是否真支持宽事务，需在仿真中加合并器后跑 `make test_matadd` 比对 cycle 数。

#### 4.3.5 小练习与答案

**练习 1**：如果 8 个线程读的地址是 `0, 1, 2, …, 7`（连续）vs `0, 16, 32, …, 112`（步长 16），合并器收益有何不同？

> **答案**：连续地址落在 2 个合并行内（`0–3`、`4–7`），可压成 2 次事务；步长 16 的地址各落在不同行，无法合并，仍是 8 次事务。合并只对「空间局部性强」的访问模式有效——这也是 GPU 编程要讲究「合并访存（coalesced access）」的原因。

**练习 2**：为什么说「合并器是必要条件，宽/突发接口是充分条件」？

> **答案**：合并器只负责把请求分组、减少「逻辑事务数」；但若外部接口仍是「一字节一事」，物理上仍要发多次。只有接口支持「一次返回一行」，逻辑合并才能转化为物理事务减少。两者缺一不可。

---

### 4.4 共享内存与 barrier 同步

#### 4.4.1 概念说明

很多算法里，同一 block 的线程需要 **交换中间结果**（例如矩阵乘法里共享一整行/列）。若每次交换都走全局内存，要绕一大圈慢路径。**共享内存（shared memory）** 是一块 **block 内线程共享的片上 SRAM**，延迟远低于全局内存，专为线程间数据交换设计。

但共享带来 **竞态**：线程 A 写的数据，线程 B 什么时候能安全读到？这就需要 **屏障同步（barrier）**：一个屏障点要求 block 内所有线程都到达后，才允许任何一个继续。典型用法：

```
# 线程 A（生产者）写共享内存
shared[tid] = compute(tid)
barrier()              # 等所有线程都写完
# 线程 B（消费者）此刻能安全读到别人的 shared[*]
x = shared[other]
```

#### 4.4.2 核心流程

带共享内存与屏障的协作流程：

1. 各线程把各自那份结果写入共享内存；
2. 每个线程执行 `barrier`，硬件挂起已到达的线程；
3. 当 block 内 **所有线程** 都到达屏障，硬件统一放行；
4. 放行后线程再读共享内存，此时数据已就绪。

关键点：屏障把「所有线程同步」从软件约定变成 **硬件保证**，否则快线程会读到慢线程还没写的脏数据。

#### 4.4.3 源码精读

tiny-gpu **既没有共享内存，也没有 barrier 指令**。两个证据：

1. **没有共享内存资源**。每个线程只有自己私有的寄存器堆，线程之间无共享 SRAM：

[README.md:152-155](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L152-L155) —— 「Each thread has it's own dedicated set of register files」。`src/` 12 个文件里也没有任何 shared-memory 模块；线程间若要交换数据，只能经 LSU → 控制器 → 全局内存绕一圈，代价高昂。

2. **ISA 没有任何 barrier/sync 指令**。decoder 的操作码表穷举了全部 11 条指令，其中没有同步指令：

[src/decoder.sv:34-44](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv#L34-L44) —— 操作码 `NOP/BRnzp/CMP/ADD/SUB/MUL/DIV/LDR/STR/CONST/RET`，无 `SYNC`、`BARRIER`、`FENCE` 之类。注意 `1010–1110` 几个编码虽未被使用（可扩展），但当前没有任何屏障语义。

README 把这两项都列为「真实 GPU 有、tiny-gpu 没做」的高级功能：

[README.md:338-346](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L338-L346) —— 共享内存：threads within the same block 共享一块内存空间；
[README.md:374-378](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L374-L378) —— 屏障：让 block 内线程互相等待到齐再继续。

**这就解释了为什么示例内核能跑通**：`matadd` 和 `matmul` 的每个线程完全独立、彼此不交换数据，因此不依赖共享内存与屏障。一旦你想写「线程间协作」的内核（如归约求和、转置），就会撞上 tiny-gpu 的这道天花板。

#### 4.4.4 代码实践

**实践目标**：理解「无共享内存 + 无屏障」如何限制内核设计。

**操作步骤**：

1. 打开 [README.md:236-260](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L236-L260) 的 `matadd.asm`，确认每个线程只读写「属于自己的」`A[i]`、`B[i]`、`C[i]`，不读别人的数据。
2. 设想一个「8 个线程求和」内核：每个线程算出一个值，最终要累加到一个全局地址。在不引入共享内存和屏障的前提下，写出你认为可行的策略（提示：只能让线程各自 `STR` 到不同地址，再由某个线程串行 `LDR` 累加，或由 host 端 Python 汇总）。
3. 对照思考：如果有了共享内存 + barrier，这个求和可以怎样用「树形归约」并行加速？

**需要观察的现象**：第 2 步你会发现，没有屏障时「谁先写、谁后读」无法保证，只能靠把读写 **按线程职责拆到不同阶段、用不同地址** 来回避竞态——这正是 tiny-gpu 示例内核的设计哲学。

**预期结果**：得出结论——tiny-gpu 的内核必须保证「线程间无数据依赖」，任何需要协作的算法都得退化成串行或 host 端处理；这是比分支分歧（见 u7-l1）更直接的功能边界。

**待本地验证**：尝试用 ISA 写一个「两线程交换数据」的内核（线程 0 写地址 X、线程 1 读地址 X），跑仿真观察读到的值是否稳定——预期不稳定，因为没有同步保证。

#### 4.4.5 小练习与答案

**练习 1**：共享内存与 L1 缓存都是「片上 SRAM」，它们有何本质区别？

> **答案**：L1 缓存对程序员透明、由硬件自动管理（缓存全局内存的副本）；共享内存由程序员显式管理（`STR`/`LDR` 到一块命名空间），可控、可预测。真实 GPU 常把同一块 SRAM 配置成「一部分 L1 + 一部分 shared」，在可控性与自动缓存之间权衡。

**练习 2**：如果在 decoder 里新增一个 `SYNC` 操作码（占用空闲编码 `1010`），最小实现需要哪些硬件配合？

> **答案**：至少需要 (a) 一个「已到达屏障」的线程掩码寄存器，记录 block 内哪些线程到齐；(b) 当使能线程全部到齐才放行 PC，否则 scheduler 停在 SYNC 指令不动；(c) decoder 译出 `SYNC` 时拉高一个 `sync_enable` 信号给 scheduler。本质上是在 scheduler 的状态机里插一个「全员到齐」的等待条件。

---

### 4.5 控制器 NUM_CHANNELS 带宽瓶颈分析

#### 4.5.1 概念说明

控制器的根本约束是 **消费者多于通道**。把过订阅比定义为：

\[ R = \frac{\text{NUM\_CONSUMERS}}{\text{NUM\_CHANNELS}} \]

每条通道一次只能服务一个未完成请求，因此控制器的稳态吞吐约为：

\[ \text{Throughput} \approx \frac{\text{NUM\_CHANNELS}}{T_{\text{svc}}} \quad \text{（请求/周期）} \]

其中 \( T_{\text{svc}} \) 是一条通道服务一个请求占用的周期数。当一拍内涌入的请求数（K）远大于通道数时，排队不可避免，排队时延 ≈ \( \lceil K / \text{NUM\_CHANNELS} \rceil \cdot T_{\text{svc}} \)。这个排队时延直接灌进 scheduler 的 WAIT 阶段。

#### 4.5.2 核心流程

把控制器的五态机（u3-l2）映射到「通道占用时长」：

1. **IDLE → 拾取**：通道在 IDLE 扫到一个 pending 请求（约 1 拍过渡）；
2. **WAITING**：等外部内存应答（仿真里外部零延迟，约 1 拍）；
3. **RELAYING**：等消费者撤回 valid（约 1 拍）；
4. 回到 IDLE，可服务下一个。

粗估 \( T_{\text{svc}} \approx 3 \) 拍/请求/通道。于是 SIMD 一拍涌入 8 个读请求、4 条通道时：第 1 波 4 个、第 2 波 4 个，排空约 \( 2 \times 3 = 6 \) 拍；其中最慢线程（第 2 波）决定 scheduler 的 WAIT 时长。

#### 4.5.3 源码精读

先看控制器模块自身的默认参数：

[src/controller.sv:8-13](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L8-L13) —— 默认 `NUM_CONSUMERS = 4`、`NUM_CHANNELS = 1`。注意这只是模块默认值，真正生效的是顶层实例化时的覆盖。

顶层为两块内存分别实例化控制器，参数决定了过订阅比：

- **数据控制器**：消费者 = 全部 LSU，通道 = 数据内存通道。

[src/gpu.sv:86-91](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L86-L91) —— `.NUM_CONSUMERS(NUM_LSUS)`、`.NUM_CHANNELS(DATA_MEM_NUM_CHANNELS)`。其中 `NUM_LSUS = NUM_CORES × THREADS_PER_BLOCK`：

[src/gpu.sv:58](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L58) —— `localparam NUM_LSUS = NUM_CORES * THREADS_PER_BLOCK;`（默认 2×4 = 8）；
通道数来自 [src/gpu.sv:13](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L13) —— `DATA_MEM_NUM_CHANNELS = 4`。
→ 过订阅比 \( R_{\text{data}} = 8/4 = 2 \)。

- **程序控制器**：消费者 = 全部 fetcher，通道 = 程序内存通道，且只读。

[src/gpu.sv:115-120](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L115-L120) —— `.NUM_CONSUMERS(NUM_FETCHERS)`、`.NUM_CHANNELS(PROGRAM_MEM_NUM_CHANNELS)`、`.WRITE_ENABLE(0)`。`NUM_FETCHERS = NUM_CORES`：

[src/gpu.sv:69](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L69) —— `localparam NUM_FETCHERS = NUM_CORES;`（默认 2），通道数 [src/gpu.sv:16](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L16) `PROGRAM_MEM_NUM_CHANNELS = 1`。
→ 过订阅比 \( R_{\text{prog}} = 2/1 = 2 \)。

两个控制器都是 **2 倍过订阅**，这是 README「Memory Controllers」一节描述的节流动机的量化体现：

[README.md:113-119](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L113-L119) —— 「incoming requests … far more than the external memory is actually able to handle」，控制器据此限流并把应答送回正确资源。

再回到 IDLE 的拾取规则确认「一波能服务多少」：

[src/controller.sv:68-96](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L68-L96) —— 4 条通道并发，每条 `break` 后只领一个请求；`channel_serving_consumer`（阻塞赋值）保证同一消费者不被两条通道重复领取。因此一波最多服务 `min(K, NUM_CHANNELS)` 个请求。

#### 4.5.4 代码实践

**实践目标**：手算 SIMD 访存 burst 的排空时延，体会带宽瓶颈如何拖长 WAIT。

**操作步骤**：默认参数下（`NUM_CORES=2`、`THREADS_PER_BLOCK=4`、`DATA_MEM_NUM_CHANNELS=4`），假设 8 个线程在同一拍执行 `LDR`，地址各不相同、且为连续地址。

1. 计算一波能服务多少请求（= 通道数 = 4）；
2. 用 \( T_{\text{svc}} \approx 3 \) 拍/请求，估算排空 8 个请求的总拍数；
3. 指出 scheduler 的 WAIT 阶段会被拖到约多少拍，并解释「最慢线程决定整体」；
4. 进一步估算：若把通道数翻倍到 8（消除过订阅），WAIT 缩短多少？若启用 4.3 的合并（行宽 4），外部事务降到 2 次，WAIT 又如何变？

**需要观察的现象**：把「通道数」「合并」「单请求服务时延」三个变量对 WAIT 的影响排成一张表。

**预期结果（手工估算）**：

| 方案 | 一波服务数 | 总波数（8 请求） | WAIT 估算 |
|------|-----------|-----------------|-----------|
| 现状（4 通道，无合并） | 4 | 2 | \( 2 \times 3 = 6 \) 拍 |
| 通道翻倍（8 通道，无合并） | 8 | 1 | \( 1 \times 3 = 3 \) 拍 |
| 合并（4 通道，行宽 4，事务降到 2） | 2（仅 2 个行事务） | 1 | \( 1 \times 3 = 3 \) 拍 |

> 结论：**加通道** 与 **内存合并** 都能把 WAIT 砍半；前者烧硬件面积，后者烧控制逻辑但要配合接口改造。

**待本地验证**：\( T_{\text{svc}} \approx 3 \) 是握手时序的估计值。可在 `test/logs` 里数一条 `LDR` 指令从 REQUEST 到 UPDATE 的实际 cycle 数（参照 u6-l2 的轨迹阅读方法），用真实拍数替换上表的 3。

#### 4.5.5 小练习与答案

**练习 1**：把 `NUM_CORES` 从 2 加到 4（其他不变），数据控制器的过订阅比变成多少？对 WAIT 有何影响？

> **答案**：`NUM_LSUS = 4×4 = 16`，通道仍 4，\( R = 16/4 = 4 \)。一波仍只能服务 4 个，排空 16 个请求要 4 波，WAIT 显著变长。说明「加核」若不配套「加通道/合并」，内存带宽会变成更严重的瓶颈。

**练习 2**：为什么程序控制器设成 `WRITE_ENABLE=0`、且只 1 通道，而过订阅比仍是 2？

> **答案**：程序内存只读（内核代码不能改写），故关闭写通道省硬件；fetcher 每核一个（默认 2 核 → 2 个消费者），程序内存只配 1 通道，所以 \( 2/1 = 2 \)。指令取指的局部性极强，未来加一层指令缓存（4.2）几乎能消除这个过订阅压力，因此程序侧的带宽瓶颈比数据侧好缓解。

---

## 5. 综合实践

**任务**：为 tiny-gpu 画两张「内存层次图」——「现状图」与「改进目标图」，并用带宽公式量化改进对 `matmul` 内核的影响。

**步骤**：

1. **现状图**：画出当前数据通路 `每线程 LSU → lsu_* → data_memory_controller(4 通道) → 外部 DRAM`，标注「零层缓存、无合并器、无共享内存、无 barrier」。在控制器旁注明过订阅比 \( R_{\text{data}} = 2 \)。
2. **改进目标图**：在同一张图上叠加三类优化：
   - L1 数据缓存（夹在 LSU 与控制器之间）；
   - 合并器（夹在 LSU 与控制器之间、缓存未命中路径上），并标出「需外部接口支持宽/突发事务」；
   - 共享内存 + barrier（core 内新增一块 block 级 SRAM，并在 ISA 增 `SYNC` 指令）。
3. **量化**：参照 4.5.4 的方法，估算 `matmul` 内核（每个线程在循环里多次 `LDR` A、B）在「现状」「加 L1（命中率 0.8）」「加合并（连续地址）」三种配置下的 WAIT 拍数，列出对比表。
4. **结论**：写一段话说明，对 `matmul` 这类 **内存密集型** 内核，哪一项优化的杠杆最大、为什么。

**预期产出**：两张图 + 一张对比表 + 一段结论。重点不是画出能综合的电路，而是理清「每项优化插在哪一层、解决哪个矛盾、受什么约束」。

## 6. 本讲小结

- **缓存现状被高估**：README 称已实现一层缓存，但源码里 **零层缓存**——无 `cache.sv`、顶层无实例化、控制器直达外部内存；这是典型的「文档 vs 源码」落差，读硬件项目务必回源码核实。
- **多层缓存** 用 L1（每核私有、快）+ L2（多核共享、大）分层压低 AMAT，收益由命中率主导：\(\text{AMAT} = T_{L1} + (1-h_1)\cdot\text{MissPenalty}\)。
- **内存合并** 把 SIMD 同拍内的相邻地址请求拼成一次行事务，前提是外部内存支持宽/突发访问；合并器是必要条件，接口改造是充分条件。
- **共享内存 + barrier** 支撑线程间协作；tiny-gpu 两者皆无（无私有 SRAM、ISA 无 `SYNC` 指令，见 `decoder.sv:34-44`），故内核必须保证「线程间无数据依赖」。
- **带宽瓶颈可量化**：data 控制器 8 LSU / 4 通道、program 控制器 2 fetcher / 1 通道，均为 2 倍过订阅；SIMD burst 排空时延 \( \approx \lceil K/\text{NUM\_CHANNELS}\rceil \cdot T_{\text{svc}} \)，直接灌进 scheduler 的 WAIT。
- **三条缓解路径**：加通道（烧面积）、加缓存（提命中率）、加合并（需改接口）——对内存密集型内核，缓存与合并的杠杆通常最大。

## 7. 下一步学习建议

本讲把「内存优化」的图景铺开了，但每一项都停留在「设计与估算」层面。建议接着做：

1. **动手验证**：按 u6-l2 的方法读 `test/logs` 里的轨迹，数出一条 `LDR` 的真实 \( T_{\text{svc}} \)，把本讲的估算换成实测值。
2. **试写协作型内核**：尝试用现有 ISA 写一个需要线程间交换数据的内核，亲历「无共享内存/无屏障」的墙，从而理解为何 u7-l1 的分支分歧与本题的协作同步是两条独立的简化线。
3. **对照真实 GPU**：选一个开源 GPU（README 提到的 Miaow / VeriGPU），找它的缓存层次与合并单元，对比 tiny-gpu 的「零层」设计，体会「教学简化」砍掉了什么。
4. **回到控制器源码做改造练习**：在 `controller.sv` 的 IDLE 之前试加一个最小合并器（仅合并完全连续的 2 个请求），跑 `make test_matadd` 确认功能不破坏、并观察 cycle 数变化——这是把本讲从「纸上」推向「能综合」的第一步。
