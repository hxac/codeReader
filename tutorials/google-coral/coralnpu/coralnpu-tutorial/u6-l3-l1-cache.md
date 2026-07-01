# L1 指令/数据 Cache

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 CoralNPU 的 **L1 指令 Cache（L1I）** 的容量、相联度、行宽与地址划分（Tag/Index/Offset），并解释它如何用「CAM + 计数式伪 LRU」完成命中判定与替换。
- 说清 **L1 数据 Cache（L1D）** 的「双 bank、每 bank 8KB、4 路」结构，以及为什么双 bank 能在一次访问里同时读出相邻两行（被官方文档称为「next line prefetch」与「为 ML 外积引擎提供一半内存带宽」）。
- 复述 **D-cache 写回 + 脏行回收** 的状态机，以及 `fence.i` / `flushall` / `flushat` 三类刷写指令如何驱动 `IFlushIO`/`DFlushIO`，并理解「**刷写期间内核 stall 到完成**」这一契约。
- 读懂 `L1ICache.scala` 与 `L1DCache.scala` 两份 Chisel 源码，并能对照 `overview.md` 的 Cache 章节核对设计意图。

> ⚠️ 关于文档：本讲主题里的「microarch.md 的 Cache 章节」在实际仓库中**并不存在**。`doc/microarch/microarch.md` 只写了流水线与指令延迟表，没有 Cache 段落。Cache 的官方设计意图写在 [`doc/overview.md:74-91`](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/doc/overview.md#L74-L91)，本讲以此为准，并始终用源码核对。

## 2. 前置知识

在进入本讲前，请先建立几个直觉（这些在 u6-l2、u4-l2 已有铺垫，这里只做定位）。

**第一，CoralNPU 的主存储是 TCM，不是 Cache。** ITCM（8KB@`0x0`）放代码、DTCM（32KB@`0x10000`）放数据，都是单拍可访问、地址固定、无 tag 检查的「紧耦合存储」。run-to-completion 的 ML 负载追求**可预测的时序**，所以 TCM 才是主力（详见 u6-l2）。

**第二，Cache 是「溢出到外部存储时的开销配角」。** 官方原话是：

> Caches exists as a single layer between the core and the first level of shared SRAM. The L1 cache and scalar core frontend are an **overhead** to the rest of the backend compute pipeline and ideally are as small as possible.
> —— [overview.md:76-78](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/doc/overview.md#L76-L78)

也就是说：当程序/数据超过了 TCM 容量、必须放在外部 SRAM（经 AXI 访问）时，才需要 L1 Cache 来 amortize（摊薄）外部访问的高延迟。

**第三，先分清三个「I 侧缓存」概念，不要混淆：**

| 名称 | 位置 | 容量 | 是否在当前生产数据通路中 |
| --- | --- | --- | --- |
| L0ICache | `Fetch` 取指单元内部 | 1KB（`fetchCacheBytes`） | 否（生产 `enableFetchL0=False`，走 `UncachedFetch`） |
| **L1ICache** | `Fetch.ibus` 与 AXI 之间的独立模块 | **8KB** | **否（见下方说明）** |
| ITCM | SoC fabric 中 | 8KB | 是（主存） |

**第四，一个必须先说清的事实（本讲的诚实声明）。** `L1ICache.scala`、`L1DCache.scala` 这两个模块在当前 HEAD 是**独立可发射（emittable）的 RTL 模块**：它们各自有 `EmitL1ICache`/`EmitL1DCache` App 对象和 BUILD 目标（`l1icache_cc_library`、`l1dcache_cc_library`、`l1dcachebank_cc_library`），可以单独生成 SystemVerilog；但**它们没有被任何顶层（Core / SCore / CoralNPUChiselSubsystem）实例化进数据通路**。可以在全树搜索 `new L1ICache` / `new L1DCache` 验证：唯一命中是它们自身的 `apply` 工厂方法（[L1ICache.scala:24-28](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1ICache.scala#L24-L28)、[L1DCache.scala:26-36](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L26-L36)）。

> 结论：本讲讲解的是「**Cache 硬件本身如何实现**」——这是两份源码的全部价值所在。它们是 CoralNPU 为「需要外部 cached 存储的配置」准备的、经过独立发射/验证的 IP 块。至于「刷写语义」这一节，我们会看到对应的 ISA 与 LSU/SCore 联动（`fence.i`/`flushall`/`flushat`）是**在线生效**的：它当前驱动的是取指单元的 `IFlushIO`，而 `L1ICache`/`L1DCache` 实现了同一套 flush 接口，作为 cached 数据通路的参考实现。

**术语速查：**

- **Cache 行（line/block）**：cache 与外部存储之间一次搬移的最小单位。L1I/L1D 的行宽都是 256 位（32 字节）。
- **组相联（set-associative）**：把所有 slot 分成若干「组」，一个地址只可能落在某一组里的某一路（way）。
- **CAM（Content-Addressable Memory）**：这里指「拿地址当 key，并行比较所有 slot 的 tag」的查找方式。
- **伪 LRU**：用计数器近似「最近最少使用」的替换策略。
- **脏位（dirty）**：一个 cache 行被写过、与外部存储不一致时置 1；替换/刷写前必须写回。
- **stall**：内核停拍等待某操作完成。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [doc/overview.md](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/doc/overview.md#L74-L91) | Cache 章节：L1I/L1D 的容量、相联度、双 bank、刷写契约的**设计意图**（不是 microarch.md） |
| [hdl/chisel/src/coralnpu/Parameters.scala](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/Parameters.scala#L149-L161) | `l1islots`/`l1iassoc`/`l1dslots`/`l1dassoc` 与 AXI 接口位宽等 cache 参数 |
| [hdl/chisel/src/coralnpu/L1ICache.scala](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1ICache.scala) | L1 指令 Cache：只读、CAM 查找、计数式伪 LRU、AXI 读填充、整表刷写 |
| [hdl/chisel/src/coralnpu/L1DCache.scala](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala) | L1 数据 Cache：顶层 `L1DCache`（双 bank 仲裁/对齐）+ `L1DCacheBank`（单 bank 核心，含脏位/写回/刷写 FSM） |
| [hdl/chisel/src/coralnpu/scalar/Lsu.scala](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L822-L836) | `FlushCmd`、`LsuOp.FENCEI/FLUSHALL/FLUSHAT`、cached 标量访存标志 `sldst` |
| [hdl/chisel/src/coralnpu/scalar/SCore.scala](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/SCore.scala#L103-L114) | 把 LSU 的 flush 路由成 `dflush`（D 侧）或 `iflush`（I 侧 + 取指重定向） |
| [hdl/chisel/src/coralnpu/BUILD](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/BUILD#L793-L821) | `l1icache_cc_library` 等独立发射目标，证明它们是独立 IP 块 |

---

## 4. 核心概念与源码讲解

### 4.1 L1 指令 Cache：组织、查找与缺失填充

#### 4.1.1 概念说明

L1 指令 Cache（L1I）解决的问题是：「**取指请求落在 ITCM 之外、需要去外部 SRAM 取时，如何用一块小 SRAM 把最近用过的指令行缓存起来，避免每次都走慢速 AXI**」。

它是一个**只读、写分配不需要（指令不会被改写）、单事务在途**的简单 cache。源码顶部注释直接写明了它的规格：

[L1ICache.scala:30-34](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1ICache.scala#L30-L34) —— 「一个相对简单的 cache 块，同时只允许一个在途事务；`2^8 × 256 / 8 = 8KiB`，4 路，Tag[31,11]+Index[10,5]+Data[4,0]」。这段注释对应默认的 `fetchDataBits=256` 配置。

容量推导（默认 256 位行宽）：

\[ \text{容量} = \text{slots} \times \text{行宽} = 256 \times 256\text{b} = 256 \times 32\text{B} = 8192\text{B} = 8\,\text{KiB} \]

地址被切成三段（`setLsb = log2(fetchDataBits/8) = 5`，`sets = slots/assoc = 64`）：

\[ \underbrace{\text{Tag}}_{21\text{b}}\,\underbrace{\text{Index}}_{6\text{b}}\,\underbrace{\text{Offset}}_{5\text{b}} \quad\Rightarrow\quad \text{Tag}[31{:}11]+\text{Index}[10{:}5]+\text{Offset}[4{:}0] \]

#### 4.1.2 核心流程

每个取指请求 `io.ibus.addr` 到来时：

1. **组选择**：用 Index 位算出地址属于哪一组（共 64 组）。
2. **tag 比较（CAM）**：在该组的 4 路（way）里并行比较 tag；`valid && setMatch && tagMatch` 三者皆真即命中。
3. **命中**：从 SRAM 读出该 slot 的 256 位行，返回给 `io.ibus.rdata`；同时更新该组的 history 计数器（命中路置为「最近使用」）。
4. **缺失**：因为「同时只允许一个在途事务」，cache 先锁存地址，选一个替换 slot（history 最小的路），把该 slot 的 `valid` 暂时清 0；发一次 AXI 读把整行搬进来，写进 SRAM，再把 `valid` 置 1；随后命中返回。
5. **替换策略**：组内每路一个唯一计数器，计数器为 0 的那一路是被替换者；命中路计数器重置为最大值，其余按大小递减——这是计数式伪 LRU。
6. **整表刷写**：`io.flush.valid`（`IFlushIO`）一来，所有 slot 的 `valid` 一拍清零，`ready` 立即拉高——I-cache 刷写是单拍失效，不需要写回（指令只读）。

#### 4.1.3 源码精读

**容量与地址划分参数**（从 `Parameters` 注入）：

[Parameters.scala:149-154](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/Parameters.scala#L149-L154)：`l1islots = 256`、`l1iassoc = 4`、`axi0IdBits = 4`、`axi0AddrBits = 32`、`axi0DataBits = fetchDataBits`。注意 `axi0DataBits` 绑死到 `fetchDataBits`：默认 256，生产配置降到 128（见 4.1.4）。

[L1ICache.scala:36-43](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1ICache.scala#L36-L43) 把这些常量算成位段边界：`sets = slots/assoc`、`setLsb/setMsb/tagLsb/tagMsb`。紧接着 [L1ICache.scala:55-60](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1ICache.scala#L55-L60) 的一串 `assert` 把「assoc 与位段边界必须自洽」这一不变量焊死——例如 4 路时要求 `setLsb==5 && setMsb==10 && tagLsb==11`（256 位行）或 `setLsb==4 && setMsb==9 && tagLsb==10`（128 位行）。这是核对容量的「真相源」。

**存储体与 CAM 状态**：

[L1ICache.scala:62-80](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1ICache.scala#L62-L80)：声明了 BlackBox `Sram_1rw_256x256`（256 项 × 256 位 = 8KB 的数据阵列），以及三组寄存器状态——`valid`（每 slot 一个有效位）、`camaddr`（每 slot 存的地址/tag）、`history`（每组的每路一个计数器）。注意 `mem` 是按 slot 编址（`addr` 是 slot 编号），而不是按字节地址——这是 CAM 风格 cache 的特征。

**命中判定与替换选择**：

[L1ICache.scala:99-119](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1ICache.scala#L99-L119) 是查找核心。对每一 slot `i`：`matchSlotB(i) := valid(i) && matchSet(i) && matchAddr(index)`，其中 `matchSet` 用 Index 判定是否同组、`matchAddr` 用 Tag 判定是否同地址。结果被断言为 one-hot（`PopCount(matchSlot) <= 1`）。替换则用 `history(set)(index) === 0` 选出被淘汰路。

**计数式伪 LRU 更新**：

[L1ICache.scala:140-163](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1ICache.scala#L140-L163)：命中时把命中路计数器置为 `(assoc-1)`（最「新」），其余计数器若大于命中路原值则递减。复位时每组初始化为 `0,1,...,assoc-1` 的唯一值（[L1ICache.scala:168-174](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1ICache.scala#L168-L174)），保证任何时候「计数器为 0 的路唯一」。

**AXI 读填充状态机**：

[L1ICache.scala:197-242](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1ICache.scala#L197-L242) 用两个寄存器 `axivalid`（AR 通道在途）和 `axiready`（R 通道等待中）串起一次缺失填充：

- 缺失且无在途事务 → 锁存替换 slot（`replaceIdReg`），按行对齐地址 `axiaddr`，发 AR（`prot=2`、`id=0`）；
- AR 被接受 → 等 R；R 到来 → 把数据写进 SRAM（`mem.io.write := axiready`），`valid(replaceIdReg) := true.B`。

其中 [L1ICache.scala:229-235](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1ICache.scala#L229-L235) 同时把对齐地址写进 `camaddr(replaceId)`，让后续命中能匹配。`io.ibus.ready := found`（[L1ICache.scala:191](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1ICache.scala#L191)）保证缺失期间 ibus 反压。

**整表刷写**：

[L1ICache.scala:219-227](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1ICache.scala#L219-L227)：`io.flush.valid` 优先级最高，一拍把所有 `valid(i)` 清零；[L1ICache.scala:244](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1ICache.scala#L244) `io.flush.ready := true.B`——I-cache 刷写即时完成，因为指令只读、无需写回。

#### 4.1.4 代码实践

**实践目标**：手算 L1I 的地址划分，并核对源码注释与断言；再走一遍「缺失→填充」的控制流。

**操作步骤（源码阅读型）**：

1. 打开 [L1ICache.scala:30-60](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1ICache.scala#L30-L60)，分别对 `fetchDataBits=256` 和 `fetchDataBits=128` 计算 Tag/Index/Offset 的位段，并验证是否落入第 56–59 行的某一条 `assert`。
2. 追踪一次缺失：从 `io.ibus.valid && !io.ibus.ready` 开始，依次标注 `replaceIdReg`、`axivalid`、`axiready`、`mem.io.write`、`valid(replaceIdReg)` 这几个状态在第几拍变化。
3. （可选，待本地验证）尝试独立发射 L1I 的 SystemVerilog：`bazel build //hdl/chisel/src/coralnpu:l1icache_cc_library`（目标名见 [BUILD:813-821](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/BUILD#L813-L821)），在 `bazel-bin` 下找到生成的 `L1ICache.sv`，确认它是一个**顶层无父模块**的独立 IP。

**需要观察的现象**：

- 256 位配置下位段应为 Tag[31:11]/Index[10:5]/Offset[4:0]，与顶部注释一致；128 位配置下 Index 仍占 6 位（64 组不变），Offset 缩到 4 位。
- 一次缺失至少经历「发 AR → 接受 AR → 等 R → 写 SRAM」四拍控制信号翻转。

**预期结果**：手算位段与 `assert` 完全吻合；缺失填充控制流能用一张 4 拍时序表画出来。环境相关命令的运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**Q1**：为什么 L1I 的刷写可以「单拍完成、立即 ready」，而 L1D（4.3 节）不行？
**答**：L1I 是只读 cache，行内数据与外部存储始终一致（不会被核写过），没有「脏行」概念，失效即可；L1D 有写操作会产生脏行，刷脏行前必须先经 AXI 写回外部存储，所以要走一个多拍 FSM。

**Q2**：某组复位后 `history = [0,1,2,3]`（路 0..3，见 [L1ICache.scala:168-174](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1ICache.scala#L168-L174)）。若一次访问命中路 0（其计数器当前值 `matchValue=0`），更新后 `history` 变成什么？同组下一次缺失会替换哪一路？
**答**：命中路 0 置为 `assoc-1=3`；路 1/2/3 因为都 `> matchValue(0)` 各递减 1，变成 `0/1/2`。结果 `[3,0,1,2]`。计数器为 0 的路变成路 1，故下一次缺失替换路 1。（练习目的：体会「命中路置最大、比它小的递减」如何把刚用过的路推到最不容易被替换的位置。）

---

### 4.2 L1 数据 Cache：双 bank、对齐缓冲与脏行写回

#### 4.2.1 概念说明

L1 数据 Cache（L1D）解决的问题是：「**标量核做循环控制与地址生成、SIMD/RVV 后端做大批量数据搬运时，如何用一块 cached SRAM 桥接慢速外部存储**」。它比 L1I 复杂得多，因为：

- 它是**可读可写**的，需要脏位与写回（writeback）。
- 官方为它选了**双 bank**结构，每 bank 8KB、4 路，合计 16KB。
- 它还兼任**对齐缓冲**：标量与 SIMD 访存的位宽/对齐各异，cache 帮忙把数据摆齐，简化软件。

设计意图（官方原话，[overview.md:80-91](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/doc/overview.md#L80-L91)）：

> The L1Icache is 8KB (256b blocks × 256 slots) with 4-way set associativity. … The L1Dcache is 16KB (SIMD256b) with low set associativity of 4-way. The L1Dcache is implemented with a **dual bank architecture where each bank is 8KB** (similar to L1Icache). This property allows for a degree of **next line prefetch**. … the L1Dcache provides **half of the memory bandwidth to the ML outer-product engine** when only a single external memory port is provided. Line and all entry flushing is supported where **the core stalls until completion** to simplify the contract.

这段话里的三个关键点——「双 bank」「next line prefetch / 一半带宽」「刷写时 stall」——分别对应 4.2、4.2、4.3 三节。

#### 4.2.2 核心流程

L1D 分两层（见 [L1DCache.scala:38-51](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L38-L51)）：

- **顶层 `L1DCache`**：实例化 `bank0`、`bank1` 两个 `L1DCacheBank`，负责把一条 `dbus` 请求**拆/路由到两个 bank**、把两个 bank 的读数据**重新交织**回一条总线、并在两个 bank 之间**仲裁对外 AXI**。
- **`L1DCacheBank`**：单 bank 的 cache 核心，结构与 L1I 同构（CAM + 计数伪 LRU + AXI 填充），但增加了**脏位、写回、ECC、刷写 FSM**。

**双 bank 交织**：相邻两行轮流放在两个 bank——地址的 `linebit` 位（行地址最低位）为 0 的行进 bank0，为 1 的进 bank1。`BankInAddress` 把这一位摘掉得到 bank 内地址，`BankOutAddress` 对外 AXI 时再按 bank 号插回去（[L1DCache.scala:57-70](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L57-L70)）。

**一次访问两行**：`DBusIO` 同时携带 `addr` 和 `adrx`，源码断言 `adrx === addr + linebytes`（[L1DCache.scala:85](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L85)），即相邻下一行。由于相邻两行必然落在不同 bank，于是**一拍内两个 bank 各读一行**，相当于一拍吐出 2 倍单 bank 带宽——这就是官方所说的「next line prefetch」与「为 ML 引擎提供一半（额外）内存带宽」的物理基础。

**写回**：当一次缺失要替换一个**脏** slot 时，cache 不能直接覆盖，必须先把旧行读出来经 AXI 写回外部，再把新行读进来。`L1DCacheBank` 用 `ractive`（读填充在途）和 `wactive`（写回在途）两个标志串起这个「读旧→写回→填新」流程。

#### 4.2.3 源码精读

**顶层双 bank 与地址路由**：

[L1DCache.scala:53-82](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L53-L82) 是双 bank 的精髓。`lineend` 判定一次访问是否横跨两行；`dsel0`/`dsel1` 决定激活哪个 bank；`preread`（[L1DCache.scala:80](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L80)）在「4KB 范围内的读」时把另一个 bank 也预激活，呼应「next line prefetch」。

`addrA`/`addrB`（[L1DCache.scala:81-82](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L81-L82)）按 `addr(linebit)` 把 `addr`/`adrx` 分别派给两个 bank，保证主行进主 bank、次行进次 bank。[L1DCache.scala:99-115](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L99-L115) 把译好的总线信号接给两个 bank；写掩码 `wmaskSA`/`wmaskSB`（[L1DCache.scala:88-96](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L88-L96)）把一次写的字节选通切成两半，分别落到正确的 bank。

**读数据重新交织**：

[L1DCache.scala:120-143](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L120-L143)：两个 bank 各自读出一行（16/32 字节），`rsel` 逐字节标注每个字节来自哪个 bank，`RData()` 递归地把它们按正确顺序 `Cat` 回一条 `lsuDataBits` 宽的总线。这就是「对齐缓冲」的实现：无论访问如何跨行，输出永远是一段连续对齐的数据。

**对外 AXI 仲裁（读/写各一套）**：

[L1DCache.scala:163-185](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L163-L185)（读）和 [L1DCache.scala:189-245](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L189-L245)（写）：两个 bank 共享一条对外 AXI，用 **id 的最高位**区分响应归属（`rresp0`/`rresp1`、`wresp0`/`wresp1`），并在地址通道上轮流仲裁（`raxi0`/`waxi0` 优先 bank0）。`BankOutAddress` 在出门前把 bank 选位插回地址。

**单 bank 核心：ECC SRAM + 脏位**：

[L1DCache.scala:269-271](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L269-L271) 的注释写出 8KB/4 路的规格；[L1DCache.scala:320-327](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L320-L327) 的 `assert` 链用 `checkBit`（128 位时=4、256 位时=5）把 bank **内部地址**（去掉 bank 选位后的 31 位）的位段焊死：256 位时 `setLsb=5/setMsb=10/tagLsb=11`。顶部注释 `Tag[31,12]+Index[11,6]+Data[5,0]` 是在**完整 32 位地址**上表达的同一套划分——bit 5 是 bank 选位，`Data[5:0]` 把 bank 位与 5 位字节偏移打包在一起。注释与 assert 一致，只是所用地址空间不同（注释=完整地址，assert=bank 内地址）。

ECC：数据阵列是 `Sram_1rwm_256x288`（[L1DCache.scala:329-340](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L329-L340)），每字节 9 位（8 数据 + 1 校验），所以 256 位数据变成 288 位存储。`Mem8to9`/`Mem9to8`/`Mem9to1`（[L1DCache.scala:292-318](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L292-L318)）在写入/读出时做 8↔9 位转换，维持逐字节校验位——这与 u9-l3 的总线 SECDED 同源思想，只是作用在 cache SRAM 内部。

`L1DCacheBank` 比 L1I 多一个 `dirty` 寄存器向量（[L1DCache.scala:352-355](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L352-L355)）：每次写命中置脏（[L1DCache.scala:518-520](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L518-L520)）。

**脏行写回**：

[L1DCache.scala:500-516](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L500-L516)：缺失且替换 slot 脏时，置 `wactive`，发 `memwaddrEn`/`memwdataEn` 把脏行从 SRAM 读出。[L1DCache.scala:532-538](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L532-L538) 用 `Mem9to8`/`Mem9to1` 把它转成纯数据+选通，发一次单拍 AXI 写（`last=true`，[L1DCache.scala:567](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L567)）；等 BRESP 到来（[L1DCache.scala:548-550](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L548-L550)）才清 `wactive`。读填充（`ractive`）与之串行，体现「同时只允许一个在途事务」。

#### 4.2.4 代码实践

**实践目标**：吃透「双 bank 如何一次访问两行」与「脏行写回」两条数据通路。

**操作步骤（源码阅读型）**：

1. **双 bank 路由**：假设 `lsuDataBits=256`（`linebit=5`，`linebytes=32`），给定 `io.dbus.addr = 0x00000040`（行地址位 `addr(5)=0`）且 `adrx = 0x00000060`。打开 [L1DCache.scala:81-115](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L81-L115)，回答：`addrA`/`addrB` 分别送给 bank0 还是 bank1？`dsel0`/`dsel1` 各为何值？
2. **读交织**：继续追 [L1DCache.scala:120-143](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L120-L143) 的 `RData()`，说明两个 bank 各 32 字节的读数据如何拼成一条连续的 64 字节序列（即两行）。
3. **脏行写回**：在 [L1DCache.scala:500-550](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L500-L550) 里，按时间顺序列出 `ractive`、`wactive`、`memwaddrEn`、`axiwaddrvalid`、`axiwdatavalid`、`io.axi.write.resp` 这几个信号在「替换一个脏行」时的翻转顺序。
4. （可选，待本地验证）`bazel build //hdl/chisel/src/coralnpu:l1dcache_cc_library` 与 `:l1dcachebank_cc_library`（[BUILD:793-811](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/BUILD#L793-L811)），在生成的 `L1DCache.sv` 里确认顶层实例化了两个 `L1DCacheBank`。

**需要观察的现象**：相邻两行的请求被无冲突地分到两个 bank；脏行写回严格「先于」新行填充完成。

**预期结果**：能画出「一条 dbus 请求 → 两个 bank 并行读 → RData 交织」与「脏行：读 SRAM→AXI 写→BRESP→读填充」两张时序图。运行命令的产物路径**待本地验证**。

#### 4.2.5 小练习与答案

**Q1**：为什么把相邻行交替放在两个 bank（而不是 bank0 放低半地址、bank1 放高半地址）？
**答**：交替（按 `linebit` 交织）保证任意两个**相邻**行一定分属不同 bank，于是 `addr`+`adrx(=addr+行宽)` 这一对连续访问能在一拍内被两个 bank 并行服务，实现 2× 带宽。若按地址高低半划分，连续访问会落在同一 bank，带宽退化为 1×。

**Q2**：L1D 的数据 SRAM 为什么是 288 位宽而不是 256 位？
**答**：每字节多存 1 位校验（9 位/字节），256 位数据 × 9/8 = 288 位，用于检测存储内部的比特错误（ECC）。这也是 `Mem8to9`/`Mem9to8` 转换函数存在的原因。

**Q3**：`axiwdatavalid` 伴随的 `last` 为什么恒为 `true`（[L1DCache.scala:567](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L567)）？
**答**：一行刚好等于 AXI 数据位宽（256 位 = 一个 beat），所以一次写回只发一个数据 beat，`last` 自然恒真。

---

### 4.3 刷写语义与「内核 stall 到完成」契约

#### 4.3.1 概念说明

任何带写回 D-cache 的系统都必须提供「让软件主动把脏数据写回、并让取指看到最新数据」的手段。CoralNPU 在标量 ISA 里提供了三条 LSU 操作（[Lsu.scala:80-82、113-114](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L80-L82)）：

- **`FENCEI`**（`fence.i`）：同步「数据写」与「后续取指」。CoralNPU 的实现是——**失效整张 I-cache 并把取指引向 `pc+4`**。
- **`FLUSHALL`**：把整张 D-cache 的脏行全部写回（并按 `clean` 决定是否同时失效）。
- **`FLUSHAT`**：按地址刷写**指定行**（`all=false`，用 `dbus.addr` 指定）。

官方契约很直白：「**the core stalls until completion to simplify the contract**」——刷写期间内核停拍，直到刷写完成，软件无需自己轮询。

#### 4.3.2 核心流程

刷写由 LSU 发起、经 SCore 路由、最终落到 cache（或取指单元）：

1. 派发器只在「LSU 空闲、且在首槽」时发射 flush 指令（[Lsu.scala:854-856](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L854-L856) 注释）。
2. LSU 把指令翻译成 `FlushCmd`（[Lsu.scala:829-836](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L829-L836)）：`all := FENCEI|FLUSHALL`、`fencei := (op==FENCEI)`、`pcNext := pc+4`，并永远 `clean := true`。
3. SCore 按 `fencei` 标志二选一路由（[SCore.scala:104-114](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/SCore.scala#L104-L114)）：
   - `fencei` → `iflush.valid`（I 侧失效 + 取指重定向到 `pcNext`）；
   - 否则（`FLUSHALL`/`FLUSHAT`）→ `dflush.valid`（D 侧刷写）。
4. **LSU 的 `flush.ready` 取决于所选路径完成**：`Mux(fencei, fetch.io.iflush.ready, io.dflush.ready)`。在 ready 之前，flush 指令不退休，派发被屏障——这就是「内核 stall」。
5. D-cache 的 `L1DCacheBank` 用一个 8 态 FSM（`FlushState`）完成「逐脏行写回 + 按需失效」，直到全部 BRESP 收齐才置 `io.flush.ready`。

#### 4.3.3 源码精读

**LSU 侧的 flush 翻译**：

[Lsu.scala:822-836](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L822-L836)：`FlushCmd.apply` 把一条 LSU 指令转成 `{all, fencei, pcNext}`。注意 `all` 同时覆盖 `FENCEI` 和 `FLUSHALL`，而 `FLUSHAT`（按行）`all=false`。

[Lsu.scala:857-870](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L857-L870)：`flushCmd` 寄存器在「收到新 flush」与「flush 完成（`valid && ready`）」之间翻转。`io.flush.clean := true.B` 恒真，意味着 CoralNPU 的刷写总是「写回 + 失效」，不保留 clean-but-valid 行。

**SCore 侧的路由与 stall**：

[SCore.scala:104-114](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/SCore.scala#L104-L114)：

- `io.dflush.valid := lsu.io.flush.valid && !fencei`；
- `io.iflush.valid := lsu.io.flush.valid && fencei`，`io.iflush.pcNext := pcNext`；
- `lsu.io.flush.ready := Mux(fencei, fetch.io.iflush.ready, io.dflush.ready)`。

也就是说，`fence.i` **只**触发 I 侧（I-cache 失效 + 取指重定向），**不**触发 D-cache 刷写；`flushall/flushat` 只触发 D 侧。LSU 在所选路径 `ready` 之前一直被挂起，派发屏障天然形成「内核 stall」。

> 说明：在当前 HEAD 的生产数据通路里，`iflush` 实际驱动的是取指单元（`Fetch`/`UncachedFetch`）的 `IFlushIO`，使其冲刷内部指令缓冲并从 `pcNext` 重取；`L1ICache` 模块实现了同一个 `IFlushIO`（[L1ICache.scala:219-244](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1ICache.scala#L219-L244)），供 cached 配置使用。两套实现遵循同一契约。

**D-cache 的刷写状态机（`L1DCacheBank`）**：

[L1DCache.scala:468-473](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L468-L473) 定义状态枚举 `sNone→sCapture→sProcess→sMemwaddr→sMemwdata→sAxiready→（回 sProcess）…→sAxiresp→sEnd`。

[L1DCache.scala:595-667](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L595-L667) 是 FSM 主体：

- `sCapture`（[L1DCache.scala:603-611](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L603-L611)）：构建 `flush` 向量——`all` 时取所有脏 slot，否则只取命中的那一行。
- `sProcess`（[L1DCache.scala:613-621](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L613-L621)）：`flush` 向量空了就进 `sAxiresp`；否则取最低位脏 slot（`Ctz(flush)`，[L1DCache.scala:589](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L589)）进 `sMemwaddr`。
- `sMemwaddr/sMemwdata`（[L1DCache.scala:623-637](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L623-L637)）：读出脏行、清脏位、（`clean` 时）清有效位。
- `sAxiready`（[L1DCache.scala:639-644](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L639-L644)）：等这一行的 AXI 写地址+数据被接受，回 `sProcess` 处理下一脏行。
- `sAxiresp`（[L1DCache.scala:646-650](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L646-L650)）：等所有在途写响应计数 `wrespcnt` 归零。
- `sEnd`（[L1DCache.scala:652-666](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L652-L666)）：`clean && all` 时把全部 slot 失效；最后 `io.flush.ready` 才拉高（[L1DCache.scala:669](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L669)）。

**stall 契约的硬件体现**：

[L1DCache.scala:671](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L671) `assert(!(io.flush.valid && io.dbus.valid))`——刷写期间绝不允许新的 dbus 访存；配合 LSU/SCore 的派发屏障，整个核在刷写完成前彻底停拍，软件无需轮询，正是「simplify the contract」。

#### 4.3.4 代码实践

**实践目标**：把一条 `fence.i` 与一条 `flushall` 从 ISA 到 cache FSM 完整走通。

**操作步骤（源码阅读型）**：

1. 在 [Lsu.scala:80-82、829-836](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L80-L82) 确认三条 flush 指令的 `all`/`fencei` 取值。
2. 在 [SCore.scala:104-114](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/SCore.scala#L104-L114) 判定：`fence.i` 走哪条路径？`flushall` 走哪条？两者的 `ready` 分别依赖谁？
3. 画出 `L1DCacheBank` 的 `FlushState` 状态转移图（8 个状态），标注每条转移的条件，并指出「多脏行」时哪几个状态会循环。
4. （可选，待本地验证）写一段 C 伪代码：先向 DTCM/cached 区写若干字、执行 `flushall`、再改写同一片区域作为「新指令」、执行 `fence.i`、最后调用这些「新指令」。说明每一步硬件 stall 在哪里、何时恢复。

**需要观察的现象**：

- `fence.i` 不触发 D-cache FSM，只失效 I 侧并重定向 PC；`flushall` 触发 D-cache FSM，多脏行时 `sProcess↔sMemwaddr↔sMemwdata↔sAxiready` 会循环多次。
- 整个 flush 期间 `io.dbus.valid` 必须为 0（否则触发 [L1DCache.scala:671](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L671) 的断言失败）。

**预期结果**：能给出 `fence.i` 与 `flushall` 各自完整的「ISA→LSU→SCore→cache」信号链。实际仿真波形**待本地验证**。

#### 4.3.5 小练习与答案

**Q1**：为什么 `fence.i` 在 SCore 里**不**触发 `dflush`？
**答**：CoralNPU 的 `fence.i` 语义被实现为「失效 I-cache + 从 `pc+4` 重取」。它假设需要被取指看到的数据已经处在对取指可见的存储（如 ITCM 或一致性外部存储）中，故只需冲 I 侧。若软件把「未来指令」写在 cached 且写回的 D 区，则需先 `flushall` 再 `fence.i`——这是软件的责任。

**Q2**：`wrespcnt`（[L1DCache.scala:577-585](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L577-L585)）在刷写 FSM 里起什么作用？
**答**：它跟踪「已发出去但还没收到 BRESP」的 AXI 写事务数（发地址 +1，收响应 -1）。`sAxiresp` 要等它归零才能进 `sEnd`，保证所有脏行确实被外部存储接收后才宣告刷写完成。

**Q3**：`io.flush.clean` 恒为 `true`（[Lsu.scala:860](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L860)）意味着什么？
**答**：CoralNPU 的刷写永远是「写回 + 失效」（clean），不提供「只写回但保留有效」的选项。刷写完成后，被刷的行在 cache 里不再命中，下次访问必然重新从外部取——用「干净」换「简单」。

---

## 5. 综合实践

把本讲三节串起来，完成下面这张「**L1 Cache 全规格核对表**」（假设默认配置 `fetchDataBits=256`、`lsuDataBits=256`）：

| 项目 | L1I | L1D（每 bank / 合计） |
| --- | --- | --- |
| 总容量 | 8KB | 8KB / 16KB |
| 行宽 | 256 位（32B） | 256 位（32B，另 +32b ECC） |
| 相联度 | 4 路 | 4 路 |
| 组数 | 64 | 64 |
| Tag/Index/Offset 位段 | Tag[31:11]/Index[10:5]/Off[4:0] | 完整地址：Tag[31:12]/Index[11:6]/bank位[5]/Off[4:0]（等价于 bank 内 Tag[30:11]/Index[10:5]/Off[4:0]） |
| 替换策略 | 计数式伪 LRU | 计数式伪 LRU |
| 写策略 | 只读 | 写回（dirty 位） |
| 刷写 | 整表单拍失效 | 8 态 FSM，逐脏行写回 + 失效 |

任务：

1. 逐格用 [L1ICache.scala:30-60](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1ICache.scala#L30-L60) 与 [L1DCache.scala:269-327](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/L1DCache.scala#L269-L327) 的源码/断言验证（提示：L1D 顶部注释用完整地址表达 `Tag[31:12]/Index[11:6]/Data[5:0]`，bit 5 为 bank 选位；`assert` 用 bank 内 31 位地址表达，两者一致）。
2. 写两段时序伪代码（文字即可）：
   - **cached 标量 load 缺失且替换脏行**：标注 `ractive`/`wactive`/`memwaddrEn`/AXI 写/AXI 读/BRESP 的先后。
   - **`fence.i` 序列**：标注 LSU→SCore→`iflush`→取指重定向，以及内核在哪一拍 stall、哪一拍恢复。
3. 最后回答一个开放题：既然 `L1ICache`/`L1DCache` 当前未被实例化进生产 SoC，那 [overview.md:74-91](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/doc/overview.md#L74-L91) 描述的「为 ML 外积引擎提供一半内存带宽」在今天的硬件上是如何兑现的？（提示：回到 u6-l2 的 TCM 与 fabric 带宽，以及 u7 的 RVV 后端如何从 TCM 取数；L1D 的双 bank 设计是为「单外部端口 + cached」场景预留的能力。）

## 6. 本讲小结

- CoralNPU 的 **L1I = 8KB / 4 路 / 256b 行**，**L1D = 16KB（双 bank × 8KB）/ 4 路 / 256b 行**；容量与相联度的真相源是 `Parameters` 与两份源码里的 `assert` 链，而非源码顶部注释（后者含历史配置）。
- 两块 cache 都用「**CAM 查找 + 计数式伪 LRU 替换**」，且都遵守「**同时只允许一个在途事务**」的简化约束。
- L1D 的**双 bank 交织**让一次 `addr`+`adrx(=addr+行宽)` 访问在一拍内并行读出相邻两行，这就是官方说的「next line prefetch」与「为 ML 引擎提供一半（额外）内存带宽」的物理基础；它还兼任标量/SIMD 的**对齐缓冲**。
- L1D 用 `dirty` 位 + `ractive`/`wactive` 实现**写回**；脏行替换时先经 AXI 写回旧行、再读填充新行。
- 刷写分两条路：`fence.i` → I 侧整表失效 + 取指重定向；`flushall/flushat` → D 侧 8 态 FSM 逐脏行写回 + 失效。**刷写期间内核 stall 到完成**（`assert(!(flush.valid && dbus.valid))` + 派发屏障），软件无需轮询。
- **重要事实**：当前 HEAD 下 `L1ICache`/`L1DCache` 是**独立可发射的 IP 块**（BUILD 目标 `l1icache_cc_library`/`l1dcache_cc_library`/`l1dcachebank_cc_library`），**未被实例化进生产数据通路**；生产 SoC 用 `enableFetchL0=False`+`lsuDataBits=128` 并以 TCM 为主存。cache 的设计意图见 [overview.md:74-91](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/doc/overview.md#L74-L91)（**不是** microarch.md）。

## 7. 下一步学习建议

- **向外看总线保护**：本讲看到 L1D 的 SRAM 用了逐字节 ECC（9 位/字节），下一讲可进入 u9-l3，看 TileLink 总线层面的 `TlulIntegrity`/SECDED 如何与这一思想呼应。
- **向内看 RVV 后端如何消费带宽**：本讲反复提到「为 ML 外积引擎提供带宽」。建议进入单元 7（u7-l1 起），看 RVV 后端的向量访存如何经 `rvv2lsu`/`lsu2rvv` 与存储子系统交互，从而理解「双 bank 带宽」真正被谁吃掉。
- **若要做 cached 配置实验**：可尝试把 `L1ICache`/`L1DCache` 接进一个自定义顶层（实例化在 `Fetch.ibus`/LSU 的 ebus 与 AXI 之间），用 cocotb（u2-l4）写一个先写后 `flushall`+`fence.i` 再执行的测试，观察本讲描述的 stall 契约——这是一条很有价值的二次开发练习路径。
