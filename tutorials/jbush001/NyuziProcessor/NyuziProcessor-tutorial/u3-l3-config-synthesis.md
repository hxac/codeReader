# 配置参数与可综合性

## 1. 本讲目标

上一讲（u3-l2）我们俯瞰了单核流水线的 14 个级模块。本讲要回答一个更基本的问题：**这一整套流水线到底由哪些数字「捏」出来的？如果我想换一个配置，能随便改吗？改完以后又怎么能保证它在真实 FPGA/ASIC 上还能综合出来？**

学完本讲你应该能够：

- 说清 `config.svh` 里每一个可配置参数（核数、线程数、各级缓存的「组数 / 路数」、TLB 项数）的含义与默认值。
- 列出这些参数之间的隐性约束（如「路数必须是 1/2/4/8」「`L1D_WAYS` 必须 ≥ 线程数」「`L1D_SETS` 必须 ≤ 64」），并能解释**为什么**有这些约束。
- 理解参数化的 SRAM / FIFO 模块如何通过一组 `ifdef` 在「仿真模型」「Altera 宏」「Xilinx 宏」「厂商存储编译器」之间切换，从而同时支持仿真与可综合实现。
- 知道 `SIMULATION` 宏的作用，以及它与可综合性的关系。

本讲覆盖三个最小模块：**配置参数**、**参数约束**、**SRAM 宏与可综合性**。

## 2. 前置知识

在进入源码前，先用通俗语言把几个概念讲清楚。

- **宏定义（\`define）与参数（parameter / localparam）**：SystemVerilog 里，`\`define NAME value` 是文本宏，在编译前由预处理器做替换，全局可见；`parameter` 则是模块参数，在「例化/精化（elaboration）」时确定。Nyuzi 把可配置项写成 `\`define`，放进 [config.svh](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh)，再由 [defines.svh](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh) `include` 并据此派生出全项目共享的类型与常量。改配置 = 改这些宏，然后**重新精化/重新编译**硬件模型。
- **组相联缓存（set-associative cache）**：缓存被分成若干「组（set）」，每组里有若干「路（way）」。一个地址落在哪一组由它的「索引位」决定；落在组内的哪一路则可自由替换。缓存的容量 = 组数 × 路数 × 缓存行大小。本讲里「组数」= `*_SETS`，「路数」= `*_WAYS`。
- **虚拟索引 / 物理标签（virtually indexed, physically tagged, VI/PT）**：L1 数据缓存用「虚拟地址」的若干位当索引去查组，但组里每一路存的「标签（tag）」是物理地址（由 TLB 翻译得到）。这种结构能提速，但要求「索引位」不能跨越页边界，否则同一个物理地址可能映到不同组，产生**别名（aliasing）**。这是后面 `L1D_SETS ≤ 64` 约束的根源。
- **可综合性（synthesizability）**：一段 SystemVerilog 能被综合工具（如 Quartus、Vivado、Design Compiler）转成真实的门电路/存储块，就叫「可综合」。仿真专用语法（如 `$random`、`$display`、`initial` 初始化数组）往往不可综合或综合效率很差，必须用厂商提供的「存储宏（megafunction / IP）」替换。Nyuzi 用同一套模块外壳 + `ifdef` 分支来兼顾两者。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `hardware/core/config.svh` | 全部可配置参数的单一来源（single source of truth），以及写在注释里的约束规则。 |
| `hardware/core/defines.svh` | `include config.svh`，并用这些宏派生出 `TOTAL_THREADS`、`core_id_t`、缓存地址类型（`l1d_addr_t` 等）。 |
| `hardware/core/cache_lru.sv` | 伪 LRU 替换模块，是「路数必须是 1/2/4/8」约束的直接来源，也解释了「路数 ≥ 线程数」的活锁规避。 |
| `hardware/core/dcache_tag_stage.sv` | L1D 标签级，注释里写明了 VI/PT 与「行大小×组数 ≤ 页大小」的别名规避原理。 |
| `hardware/core/sram_1r1w.sv` | 1 读 1 写块 SRAM 模块，用 `ifdef` 在仿真 / Altera / Xilinx / 存储编译器之间切换。 |
| `hardware/core/sram_2r1w.sv` | 2 读 1 写块 SRAM 模块，同样的切换机制。 |
| `hardware/core/sync_fifo.sv` | 同步 FIFO，第三种「参数化存储」模块，同样按厂商切换。 |
| `hardware/README.md` | 说明 `SIMULATION` 宏、参数化存储与厂商宏切换的整体策略。 |

> 阅读建议：先看 `config.svh` 的注释（约束全在这里）与参数表；再看 `defines.svh` 看参数如何派生；最后用 `sram_1r1w.sv` 体会「一套外壳、多个实现」的可综合性手法。

## 4. 核心概念与源码讲解

### 4.1 配置参数

#### 4.1.1 概念说明

Nyuzi 是一个**高度参数化**的处理器：核数、每核线程数、各级缓存的容量与相联度、TLB 规模都不是写死的，而是集中在 [config.svh](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh) 里的几个宏。这样设计的好处是：研究者可以快速调出「更大缓存」「更多核」或「更小面积」的不同变体来对比微架构取舍，而不需要改散落在各处的源码。

这些宏被 `defines.svh` 进一步派生成贯穿全项目的常量与类型，例如总线程数、核号位宽、缓存地址分解方式等。换句话说，`config.svh` 是「旋钮面板」，`defines.svh` 是「把旋钮的值接到电路里」的接线表。

#### 4.1.2 核心流程

参数从「定义」到「生效」的流程：

1. 修改 `config.svh` 中的某个宏（如 `\`define THREADS_PER_CORE 4`）。
2. 重新编译硬件模型（`make`，底层是 Verilator 把 SystemVerilog 精化成 C++ 再编译成 `nyuzi_vsim`）。**这是编译期决定，不能在运行时改。**
3. 精化时，`defines.svh` 用这些宏算出派生值（如 `TOTAL_THREADS`、`DCACHE_TAG_BITS`），所有模块例化时自动采用新值。
4. 模块内部用这些派生值定 SRAM 深度、位宽、地址译码逻辑。

#### 4.1.3 源码精读

先看配置面板本体，所有可配置项都在这里 [config.svh:40-57](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh#L40-L57)：

```systemverilog
`define NUM_CORES 1
`define THREADS_PER_CORE 4
`define L1D_WAYS 4
`define L1D_SETS 64        // 16k
`define L1I_WAYS 4
`define L1I_SETS 64        // 16k
`define L2_WAYS 8
`define L2_SETS 256        // 128k
`define AXI_DATA_WIDTH 32
`define ITLB_ENTRIES 64
`define DTLB_ENTRIES 64
`define TLB_WAYS 4
```

逐项含义：

| 宏 | 默认值 | 含义 |
| --- | --- | --- |
| `NUM_CORES` | 1 | 顶层实例化的核数（多核共享 L2）。 |
| `THREADS_PER_CORE` | 4 | 每核硬件线程数（多线程隐藏延迟）。 |
| `L1D_WAYS` / `L1D_SETS` | 4 / 64 | L1 数据缓存的「路数 / 组数」。 |
| `L1I_WAYS` / `L1I_SETS` | 4 / 64 | L1 指令缓存的「路数 / 组数」。 |
| `L2_WAYS` / `L2_SETS` | 8 / 256 | L2 统一缓存的「路数 / 组数」。 |
| `AXI_DATA_WIDTH` | 32 | L2 对外 AXI4 总线的数据位宽。 |
| `ITLB_ENTRIES` / `DTLB_ENTRIES` | 64 / 64 | 指令/数据 TLB 的表项总数。 |
| `TLB_WAYS` | 4 | TLB 的相联度（注意：TLB 的路数不受「1/2/4/8」约束，见 4.2）。 |

注释里还给出缓存容量公式 [config.svh:37-38](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh#L37-L38)：「The size of a cache is sets \* ways \* cache line size (64 bytes)」。验证默认值：

\[ V_{\text{L1D}} = N_{\text{sets}} \times N_{\text{ways}} \times L = 64 \times 4 \times 64\,\text{B} = 16384\,\text{B} = 16\,\text{KiB} \]

\[ V_{\text{L2}} = 256 \times 8 \times 64\,\text{B} = 131072\,\text{B} = 128\,\text{KiB} \]

与注释里的「16k」「128k」吻合。

再看派生层。`defines.svh` 用这些宏算出贯穿全局的常量与位宽 [defines.svh:42-44](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L42-L44)：

```systemverilog
parameter NUM_VECTOR_LANES = 16;
parameter NUM_REGISTERS = 32;
parameter TOTAL_THREADS = `THREADS_PER_CORE * `NUM_CORES;
```

注意 `NUM_VECTOR_LANES = 16` 是**写死**的（不是配置项），它决定了向量位宽（512 位）与缓存行大小（64 字节）。缓存相关派生在 [defines.svh:293-301](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L293-L301)：

```systemverilog
parameter PAGE_SIZE = 'h1000;                                  // 4 KiB
parameter CACHE_LINE_BYTES = NUM_VECTOR_LANES * 4;             // = 64
parameter CACHE_LINE_OFFSET_WIDTH = $clog2(CACHE_LINE_BYTES);  // = 6
parameter DCACHE_TAG_BITS = 32 - (CACHE_LINE_OFFSET_WIDTH + $clog2(`L1D_SETS));
```

这里出现了一条关键的「地址分解」：一个 32 位物理地址被切成三段——行内偏移（6 位）、组索引（$\log_2 N_{\text{sets}}$ 位）、标签（剩余位）。默认 `L1D_SETS=64` 时组索引占 6 位，于是标签位宽为：

\[ \text{DCACHE\_TAG\_BITS} = 32 - (6 + \log_2 64) = 32 - 12 = 20\,\text{位} \]

这些派生类型随后被封装成 `l1d_addr_t` 等 [defines.svh:316-324](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L316-L324)，供 `dcache_tag_stage` 等模块直接使用。改了 `L1D_SETS`，标签位宽与地址类型会自动跟着变。

#### 4.1.4 代码实践

**实践目标**：亲手确认「参数 → 派生值」的传递链。

**操作步骤**：

1. 打开 [config.svh:40-51](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh#L40-L51)，记下 `L1D_SETS`、`L1D_WAYS`、`L2_SETS`、`L2_WAYS` 的值。
2. 打开 [defines.svh:296-301](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L296-L301)，用计算器手算 `CACHE_LINE_BYTES`、`CACHE_LINE_OFFSET_WIDTH`、`DCACHE_TAG_BITS`、`ICACHE_TAG_BITS`。
3. 用公式 `sets × ways × 64 B` 算出 L1D、L1I、L2 三级缓存的容量。

**需要观察的现象 / 预期结果**：

- `CACHE_LINE_BYTES = 16 × 4 = 64`；`CACHE_LINE_OFFSET_WIDTH = 6`。
- `DCACHE_TAG_BITS = 20`，`ICACHE_TAG_BITS = 20`。
- L1D = L1I = 16 KiB，L2 = 128 KiB。

这些值无需运行即可手算，且必须与你阅读源码得到的派生式一致——若不一致，说明你漏看了某个宏的取值。

#### 4.1.5 小练习与答案

**练习 1**：如果只把 `L1D_SETS` 从 64 改成 128，`DCACHE_TAG_BITS` 会变成多少？L1D 容量变成多少？

**答案**：组索引位变成 $\log_2 128 = 7$，故 `DCACHE_TAG_BITS = 32 - (6 + 7) = 19`。容量 $= 128 \times 4 \times 64\,\text{B} = 32\,\text{KiB}$。注意：这个改动**违反了 4.2 要讲的别名约束**，不能直接用。

**练习 2**：`TOTAL_THREADS` 在默认配置下等于多少？它由哪两个宏决定？

**答案**：`TOTAL_THREADS = THREADS_PER_CORE × NUM_CORES = 4 × 1 = 4`，见 [defines.svh:44](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L44)。

---

### 4.2 参数约束

#### 4.2.1 概念说明

参数化设计的反面是**约束**：不是所有取值组合都能正常工作。Nyuzi 把这些约束以注释形式写在 `config.svh` 顶部，它们大多源于某个底层模块的实现细节或微架构正确性要求。理解这些约束，等于理解了「为什么不能随便改」。这些约束大体分三类：

- **结构性约束**：某个模块（如 `cache_lru`）的实现只支持特定取值。
- **正确性约束**：违反会导致活锁（livelock）或别名（aliasing）等功能错误。
- **配套修改约束**：改一处要同步改别处（如多核要加宽核号位宽）。

#### 4.2.2 核心流程

约束不是运行时检查（部分有 `assert`，部分完全没有），而是**给设计者的契约**。判断一个新配置是否合法的流程：

1. 列出所有受影响参数。
2. 逐一比对 `config.svh` 顶部注释里的约束清单。
3. 对「配套修改」类约束，定位到注释指出的另一个文件并同步修改。
4. （可选）对有 `assert` 的约束，靠仿真在精化/启动时报错来兜底。

#### 4.2.3 源码精读

约束总表写在 [config.svh:20-38](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh#L20-L38)，逐条解读：

| 约束 | 原因出处 | 含义 |
| --- | --- | --- |
| `THREADS_PER_CORE ≥ 2` | 微架构要求 | 单线程无法支撑多线程流水线的调度假设。 |
| 缓存路数 ∈ {1,2,4,8}（`TLB_WAYS` 例外） | `cache_lru` 模块 | 伪 LRU 树状编码只实现了这几种规模。 |
| `L1D_WAYS`/`L1I_WAYS` ≥ `THREADS_PER_CORE` | 活锁规避 | 否则多线程同时缺失会互相驱逐、永远无法前进。 |
| 缓存组数必须是 2 的幂 | 地址译码 | 组索引用地址位直接当索引，要求组数是 2 的幂。 |
| 改 `L2_WAYS` 要同步改 `testbench/soc_tb.sv` 的 `flush_l2_cache` | 测试台配套 | flush 函数按路数硬编码了遍历逻辑。 |
| `NUM_CORES` 1–16 | `core_id_t` 位宽 | 核号位宽写死为 4 位；更多核需加宽 `defines.svh` 里的类型。 |
| `L1D_SETS` ≤ 64 | VI/PT 别名 | 行大小 × 组数 ≤ 页大小，避免同一物理地址落到不同组。 |

**深入一：路数必须是 1/2/4/8。** 这是 `cache_lru` 模块的硬性限制。它用一棵「伪 LRU 树」记录每组各路的最近使用情况，树的位数随路数而定 [cache_lru.sv:68-72](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv#L68-L72)：

```systemverilog
localparam LRU_FLAG_BITS =
    NUM_WAYS == 1 ? 1 :
    NUM_WAYS == 2 ? 1 :
    NUM_WAYS == 4 ? 3 :
    7;    // NUM_WAYS = 8
```

对应地，`fill_way`（要替换的路）与 `update_flags`（更新后的树位）是用 `case (NUM_WAYS)` **硬编码**的 [cache_lru.sv:134-214](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv#L134-L214)，只覆盖了 1/2/4/8 四种情况，其它值会落入 `default` 分支并 `$finish` 报错。同时还有一个精化期断言兜底 [cache_lru.sv:91-95](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv#L91-L95)：`assert(NUM_WAYS <= 8 && (NUM_WAYS & (NUM_WAYS - 1)) == 0)`（即 ≤8 且为 2 的幂）。

**深入二：路数 ≥ 线程数，规避活锁。** `cache_lru` 顶部的注释点明了这一点 [cache_lru.sv:40-46](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv#L40-L46)：填充（fill）优先于访问（access）更新，「To avoid livelock where two threads evict each other's lines back and forth forever」。直觉是：若每个组至少有「线程数」那么多的路，则即便所有线程在同一组同时缺失，每个线程也能在组里保住自己的一条行，不会出现「A 驱逐 B、B 又驱逐 A」的死循环。这条是**正确性约束但没有 assert**，设计者必须自觉遵守。

**深入三：`L1D_SETS` ≤ 64，规避 VI/PT 别名。** L1D 是「虚拟索引 / 物理标签」结构，别名规避原理写在 [dcache_tag_stage.sv:29-37](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L29-L37)：要求 `cache line size × num sets ≤ page_size`。推导如下：页内偏移位宽为 $\log_2(\text{PAGE\_SIZE}) = \log_2(4096) = 12$；要保证「索引位 + 行内偏移位」全部落在页内偏移里（这样虚拟索引就等于物理索引，不会因翻译而变），需要：

\[ \underbrace{\log_2 L}_{\text{行偏移}=6} + \underbrace{\log_2 N_{\text{sets}}}_{\text{组索引}} \le \log_2(\text{PAGE\_SIZE}) = 12 \]

\[ \Rightarrow 6 + \log_2 N_{\text{sets}} \le 12 \Rightarrow \log_2 N_{\text{sets}} \le 6 \Rightarrow N_{\text{sets}} \le 64 \]

默认 `L1D_SETS = 64` 恰好取到上界，把组索引填满页内偏移的剩余 6 位。

**深入四：多核配套修改。** 核号类型 `core_id_t` 的位宽被**硬编码为 4 位**（上限 16 核），注释解释了这是绕过某些综合工具 bug 的妥协 [defines.svh:55-65](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L55-L65)：

```systemverilog
parameter CORE_ID_WIDTH = $clog2(`NUM_CORES);
// The width of core_id_t is hardcoded to work around some tool issues...
typedef logic[3:0] core_id_t;   // 限定最多 16 核
```

所以要综合超过 16 核，必须手动把这里的 `[3:0]` 加宽。

#### 4.2.4 代码实践（本讲核心实践任务）

**实践目标**：把 `THREADS_PER_CORE` 从 4 改成另一个合法值，重新构建，并解释「`L1D_WAYS` 必须 ≥ `THREADS_PER_CORE`」。

**操作步骤**：

1. 复制一份配置来实验（**不要**直接改主干；本实践只读，若真要改请在本地分支上做）。阅读 [config.svh:40-42](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh#L40-L42) 确认当前 `THREADS_PER_CORE=4`、`L1D_WAYS=4`。
2. 选一个**合法**的新值。最稳妥的选择是把 `THREADS_PER_CORE` 改为 `2`（此时 `L1D_WAYS=4 ≥ 2` 仍满足，且路数 4 仍在 {1,2,4,8} 内）。也可选 `8`，但那样必须同时把 `L1D_WAYS` 与 `L1I_WAYS` 都提到 `8`。
3. 在 `config.svh` 中改 `\`define THREADS_PER_CORE 2`，保存。
4. 在仓库根目录执行（参见 u1-l2 的构建流程）：

   ```bash
   cmake . && make
   ```

5. 构建产物在 `bin/nyuzi_vsim`（RTL 仿真模型，周期精确）。可用一个简单程序验证线程数变化，例如跑 `tests/core/isa` 下某个多线程相关测试。

**需要观察的现象 / 预期结果**：

- 改成 `THREADS_PER_CORE=2` 且 `L1D_WAYS=4` 不变时，构建应当成功；`TOTAL_THREADS` 派生为 `2 × NUM_CORES`，相关位宽（`local_thread_idx_t`、`local_thread_bitmap_t`，见 [defines.svh:48-49](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L48-L49)）会自动变窄。
- 实际构建输出与运行表现属于「**待本地验证**」——本讲无法替你跑机器，请如实记录 `make` 是否报错、`nyuzi_vsim` 是否生成。

**关于「`L1D_WAYS` 必须 ≥ `THREADS_PER_CORE`」的解释（请写入你的实验记录）**：

L1D 是组相联的，替换策略由 `cache_lru` 决定，且**填充优先于访问更新**（[cache_lru.sv:40-46](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv#L40-L46)）。设想「路数 < 线程数」的情况：若所有线程都在同一组上发生缺失，新填充的行会立刻驱逐掉别的线程刚填进来的行，于是被驱逐的线程再次缺失、再次填充、又驱逐别人……形成「互相驱逐、永不收敛」的**活锁**。保证「每组路数 ≥ 线程数」，相当于给每个线程在每一组里预留至少一条「立足之地」，从根上消除这种循环驱逐。这也是为什么默认配置刻意让 `L1D_WAYS=4` 等于 `THREADS_PER_CORE=4`。

> 反例预测（不要在生产配置里做）：若把 `THREADS_PER_CORE=8` 而 `L1D_WAYS` 仍为 4，构建**可能**通过（因为这条约束没有 assert），但运行多线程缓存密集程序时极易触发活锁——这正是「正确性约束但无运行时检查」的典型陷阱。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `L1D_SETS` 上限是 64，而 `L2_SETS` 却可以是 256？

**答案**：L1D 是虚拟索引/物理标签，索引位必须落在页内偏移（12 位）里，故 `行偏移(6) + 组索引 ≤ 12`，组数 ≤ 64。L2 是**物理索引/物理标签**（翻译已完成），不受页边界限制，所以组数可以更大（256 组仍只需 8 位索引）。

**练习 2**：如果要把 `NUM_CORES` 从 1 改成 20，除了改 `config.svh` 还要改哪里？为什么？

**答案**：要加宽 `defines.svh` 里 `core_id_t` 的位宽（[defines.svh:65](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L65)）。因为它被硬编码为 4 位，最多表示 16 个核；超过 16 核必须手动加宽（注释 [defines.svh:56-64](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L56-L64) 也说明了这一点）。

**练习 3**：`TLB_WAYS` 为什么不受「必须是 1/2/4/8」约束？

**答案**：TLB 的替换/查找不经过 `cache_lru` 模块（它是为缓存的伪 LRU 树硬编码的），TLB 用的是全相联 + 软件管理的表项结构，所以没有这个结构性限制（见 [config.svh:23-24](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh#L23-L24) 的注释）。

---

### 4.3 SRAM 宏与可综合性

#### 4.3.1 概念说明

处理器内部到处是「块存储」：缓存的数据阵列与标签阵列、LRU 位、各种队列（FIFO）、寄存器堆……这些存储在仿真里用最朴素的 SystemVerilog 数组就能写，但**真要综合到 FPGA/ASIC，必须换成厂商提供的存储宏**（Altera 的 ALTSYNCRAM、Xilinx 的 xpm_memory、或代工厂的存储编译器产出的网表）。原因是：

- 朴素数组（`logic data[SIZE]`）综合工具会推断成触发器堆，面积/功耗极其浪费；
- 厂商 FPGA 有专用的 Block RAM / M9K / M20K 资源，只有用对应宏才能映射上去；
- ASIC 流程要由独立的存储编译器生成与工艺绑定的 SRAM 实例。

Nyuzi 的解法是「**一套模块外壳 + 多个 `ifdef` 分支**」：同一个 `sram_1r1w` / `sram_2r1w` / `sync_fifo` 模块，根据预处理宏选择四种实现之一，于是上层代码完全不用关心存储到底怎么落地。这与 `SIMULATION` 宏配合，实现了「仿真用一份、综合用另一份」的可移植性。

#### 4.3.2 核心流程

存储模块的分支选择流程（以 `sram_1r1w` 为例，`sram_2r1w`、`sync_fifo` 同构）：

```text
预处理宏情况？
├─ VENDOR_ALTERA     → 例化 ALTSYNCRAM（Altera 双端口 RAM 宏）
├─ VENDOR_XILINX     → 例化 xpm_memory_sdpram（Xilinx XPM 宏）
├─ MEMORY_COMPILER   → `include "srams.inc"（由 make core/srams.inc 生成的厂商网表）
└─ 其它（仿真默认）   → 朴素 logic 数组 + $random 初始化（不可综合）
```

四条分支对外暴露的端口（读使能、读地址、读数据、写使能、写地址、写数据）完全一致，因此上层模块（如 `cache_lru`、`dcache_tag_stage`）用同一份例化代码即可在仿真与综合间无缝切换。

#### 4.3.3 源码精读

先看 `sram_1r1w` 的端口与参数——这就是「外壳」[sram_1r1w.sv:34-46](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/sram_1r1w.sv#L34-L46)：

```systemverilog
module sram_1r1w
    #(parameter DATA_WIDTH = 32,
    parameter SIZE = 1024,
    parameter READ_DURING_WRITE = "NEW_DATA",
    parameter ADDR_WIDTH = $clog2(SIZE))
    (input clk, input read_en, input [ADDR_WIDTH-1:0] read_addr,
     output logic[DATA_WIDTH-1:0] read_data,
     input write_en, input [ADDR_WIDTH-1:0] write_addr,
     input [DATA_WIDTH-1:0] write_data);
```

参数化的关键点：

- `DATA_WIDTH` / `SIZE` 让同一模块实例化出任意位宽、任意深度的块 RAM；`ADDR_WIDTH` 由 `SIZE` 自动派生（`$clog2`）。
- `READ_DURING_WRITE` 是个字符串参数，描述「同一地址同一周期既读又写时返回什么」[sram_1r1w.sv:26-31](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/sram_1r1w.sv#L26-L31)：`"NEW_DATA"` 表示返回新写入的值（read-after-write 旁路），`"DONT_CARE"` 表示不确定（可换取更高时钟频率）。

接着看四条分支。Altera 分支用 `ALTSYNCRAM` 双端口宏 [sram_1r1w.sv:48-94](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/sram_1r1w.sv#L48-L94)：

```systemverilog
`ifdef VENDOR_ALTERA
    ALTSYNCRAM #(
        .OPERATION_MODE("DUAL_PORT"),
        .WIDTH_A(DATA_WIDTH), .WIDTHAD_A(ADDR_WIDTH),
        ...
        .READ_DURING_WRITE_MIXED_PORTS("DONT_CARE")
    ) data0(...);
    generate
        if (READ_DURING_WRITE == "NEW_DATA") begin
            // 自己加一段旁路逻辑，弥补宏不支持 read-during-write 的缺陷
        end
    endgenerate
```

注意注释里的「踩坑经验」：并非所有 Altera 系列都支持 `READ_DURING_WRITE_MIXED_PORTS`，所以作者把它设成 `DONT_CARE`，再用一段 `generate` 里的旁路逻辑（`pass_thru_en`/`pass_thru_data`）自己实现 `NEW_DATA` 语义。Xilinx 分支同理，用 `xpm_memory_sdpram` 宏 [sram_1r1w.sv:95-164](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/sram_1r1w.sv#L95-L164)。

第三种是「存储编译器」分支，靠一个生成的包含文件 [sram_1r1w.sv:165-170](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/sram_1r1w.sv#L165-L170)：

```systemverilog
`elsif MEMORY_COMPILER
    generate
        `define _GENERATE_SRAM1R1W
        `include "srams.inc"
        `undef     _GENERATE_SRAM1R1W
    endgenerate
```

`srams.inc` 不是手写的，而是由 `make core/srams.inc` 配合 `tools/misc/extract_mems.py` 扫描全设计用到的所有存储规格后生成的（见 [hardware/README.md:35-38](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/README.md#L35-L38)）。这对应 ASIC 流程：先用独立存储编译器按每个尺寸生成 SRAM 实例，再插回设计。

最后是仿真默认分支——朴素数组 [sram_1r1w.sv:171-209](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/sram_1r1w.sv#L171-L209)：

```systemverilog
`else
    logic[DATA_WIDTH-1:0] data[SIZE];
    always @(posedge clk) begin
        if (write_en) data[write_addr] <= write_data;
        ...
        else read_data <= DATA_WIDTH'($random());   // 关键：$random 不可综合
    end
    initial begin
`ifndef VERILATOR
        for (int i = 0; i < SIZE; i++) data[i] = DATA_WIDTH'($random());
`endif
        ...
    end
```

这里特意在「不读」或 `DONT_CARE` 时返回 `$random()`，目的是让仿真真的暴露「不确定值」，逼上层逻辑把这类情况处理好（否则仿真碰巧读到 0 会掩盖 bug）。`$random`、`initial` 里给大数组赋值都是**仿真专用、不可综合**的写法，所以这条分支只在仿真时启用。

`sram_2r1w`（2 读 1 写）结构与 `sram_1r1w` 完全同构，只是多了一个读端口——Altera 分支例化两片 `ALTSYNCRAM`（[sram_2r1w.sv:54-96](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/sram_2r1w.sv#L54-L96)），Xilinx 分支例化两片 `xpm_memory_sdpram`（[sram_2r1w.sv:121-221](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/sram_2r1w.sv#L121-L221)），仿真分支用一个数组配两个读地址（[sram_2r1w.sv:248-298](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/sram_2r1w.sv#L248-L298)）。`sync_fifo` 同样如此：Altera 用 `SCFIFO`（[sync_fifo.sv:58-76](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/sync_fifo.sv#L58-L76)），存储编译器用 `srams.inc`（[sync_fifo.sv:77-82](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/sync_fifo.sv#L77-L82)），仿真用 head/tail 指针 + 数组（[sync_fifo.sv:83-142](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/sync_fifo.sv#L83-L142)）。

**`SIMULATION` 宏**是另一条贯穿全设计的可综合性开关。构建仿真模型时它会**被自动定义**（见 [hardware/README.md:21-24](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/README.md#L21-L24)），用来把「只在仿真里有意义、综合时要剔除」的代码包起来。一个典型例子在 `cache_lru`：一段只用于仿真的断言被 `ifdef SIMULATION` 包住 [cache_lru.sv:82-84](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv#L82-L84) 与 [cache_lru.sv:223-235](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv#L223-L235)：

```systemverilog
`ifdef SIMULATION
    always_ff @(posedge clk, posedge reset) begin
        if (reset) was_access <= 0;
        else begin
            assert(!(update_en && !was_access));   // 仿真期断言，综合时整段消失
            ...
```

这样断言（`assert`）等仿真专用结构就不会进入综合后的网表。**要点**：若你为别的工具链新建仿真工程，务必手动定义 `SIMULATION`，否则可能误把这些本该被剔除的代码送进综合。

#### 4.3.4 代码实践

**实践目标**：用源码阅读的方式，确认「同一存储模块的四种分支对外端口一致」，并理解 `extract_mems.py` 如何收集存储规格。

**操作步骤**：

1. 打开 `sram_1r1w.sv` 与 `sram_2r1w.sv`，对比四条 `ifdef` 分支。确认每条分支都驱动了同一组端口名（`read_data`/`read_en`/`read_addr`/`write_*`），这正是上层能无感切换的原因。
2. 在 [sram_1r1w.sv:206-207](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/sram_1r1w.sv#L206-L207) 与 [sram_2r1w.sv:295-296](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/sram_2r1w.sv#L295-L296) 找到 `$display("sram1r1w %d %d", ...)`，它会在带 `+dumpmems` 参数时把本模块的 `DATA_WIDTH` 与 `SIZE` 打到标准输出。
3. 阅读 [hardware/README.md:35-38](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/README.md#L35-L38) 关于 `make core/srams.inc` 与 `tools/misc/extract_mems.py` 的说明（也可直接打开 `tools/misc/extract_mems.py` 阅读其如何解析 `+dumpmems` 的输出）。

**需要观察的现象 / 预期结果**：

- 四条分支端口一致；只有「仿真」分支会出现 `$random`、`initial` 数组初始化这类不可综合结构。
- `+dumpmems` 触发的 `$display` 是 `extract_mems.py` 收集「全设计用到哪些尺寸的 SRAM」的数据源，收集结果写入 `srams.inc` 供 `MEMORY_COMPILER` 分支使用。
- 上述均为源码可读事实；若你想在本地实际跑 `+dumpmems` 收集输出，结果属于「**待本地验证**」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `sram_1r1w` 的 Altera 分支要「自己加一段旁路逻辑」来实现 `NEW_DATA`，而不是直接靠宏？

**答案**：因为并非所有 Altera FPGA 系列都支持 `READ_DURING_WRITE_MIXED_PORTS`（作者注释里明说「found out the hard way」），所以把宏的这一项设为 `DONT_CARE`，再用 `pass_thru_en`/`pass_thru_data` 这段 `generate` 逻辑自行实现「同址同周期读写返回新值」的语义（[sram_1r1w.sv:76-94](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/sram_1r1w.sv#L76-L94)）。

**练习 2**：如果把 `sram_1r1w` 的「仿真分支」代码（含 `$random`）拿去综合，会发生什么？

**答案**：`$random`、`initial` 里给大数组逐元素赋值都是不可综合结构，综合工具要么报错，要么把 `logic data[SIZE]` 推断成大量触发器，面积/功耗极差且可能时序不收敛。正因如此，Nyuzi 在综合时通过 `VENDOR_*` / `MEMORY_COMPILER` 宏切到厂商宏实现，**绝不**走这条分支。

## 5. 综合实践

把本讲三个模块串起来，设计一个「自定义 Nyuzi 配置」并自检其合法性与可综合性。

**任务**：假设目标是「一个面积更省的单核调试用配置」，请规划以下参数并逐项验证：

1. 把 `THREADS_PER_CORE` 从 4 降到 **2**。
2. 把 `L1D_WAYS` 与 `L1I_WAYS` 都降到 **2**（仍 ≥ 线程数 2，且 2 ∈ {1,2,4,8}）。
3. `L1D_SETS` / `L1I_SETS` 维持 **64**（不踩别名约束上限）。
4. `L2_SETS` / `L2_WAYS` 维持默认。

**要求你产出**：

- 用公式 `sets × ways × 64 B` 算出新配置下 L1D、L1I、L2 的容量，确认 L1D/L1I 从 16 KiB 降到 8 KiB。
- 列一张「约束自检表」，逐条对照 4.2 的七条约束，标注「满足 / 不满足 / 需配套修改」。
- 写出新配置下 `DCACHE_TAG_BITS` 的值（提示：组数没变，所以仍是 20）。
- 说明这个配置在「仿真」与「综合到 Altera FPGA」两种场景下，`sram_1r1w` 会分别走哪条 `ifdef` 分支；若改用 ASIC 流程，又该启用哪个宏、并先用哪条 `make` 目标生成 `srams.inc`。

**验收标准**：自检表全部为「满足」；能说清「仿真走 `else` 分支（朴素数组 + `$random`）、Altera 综合走 `VENDOR_ALTERA` 分支（ALTSYNCRAM）、ASIC 走 `MEMORY_COMPILER` 分支并需先生成 `srams.inc`」。实际重新构建并运行的行为属于「**待本地验证**」，请如实记录。

## 6. 本讲小结

- `config.svh` 是全部可配置参数的单一来源（核数、线程数、各级缓存的组数/路数、TLB 项数、AXI 位宽），`defines.svh` 据此派生出 `TOTAL_THREADS`、标签位宽、缓存地址类型等贯穿全局的量；缓存容量 = 组数 × 路数 × 64 字节。
- 参数有一组**隐性约束**：线程数 ≥ 2；缓存路数 ∈ {1,2,4,8}（源于 `cache_lru` 的硬编码伪 LRU 树）；`L1D_WAYS`/`L1I_WAYS` ≥ 线程数（规避活锁）；缓存组数为 2 的幂；`L1D_SETS` ≤ 64（源于 VI/PT 的别名规避：行大小×组数 ≤ 页大小）；`NUM_CORES` ≤ 16（源于 `core_id_t` 硬编码 4 位）。
- 别名约束可由地址位分解严格推出：行偏移 6 位 + 组索引 ≤ 页内偏移 12 位 ⟹ 组数 ≤ 64；默认 64 组恰好取满。
- Nyuzi 用「一套外壳 + 四条 `ifdef` 分支」让 `sram_1r1w`/`sram_2r1w`/`sync_fifo` 同时支持仿真（朴素数组 + `$random`，不可综合）、Altera（ALTSYNCRAM/SCFIFO）、Xilinx（xpm_memory）、ASIC 存储编译器（`srams.inc`，由 `extract_mems.py` 经 `+dumpmems` 生成）。
- `SIMULATION` 宏在仿真构建时自动定义，用于包住断言等仿真专用、综合时要剔除的代码；为别的工具链建仿真工程时必须手动定义它。
- 改配置是**编译期**行为，改完必须重新 `make` 重建硬件模型；部分正确性约束（如路数 ≥ 线程数）没有运行时检查，靠设计者自觉。

## 7. 下一步学习建议

本讲建立了「参数与可综合性」的全局视图，接下来可以：

- 沿**数据流向下**，进入取指与缓存实现细节：先读 [u4-l1 指令取指与 I-Cache](u4-l1-instruction-fetch.md)，再读 [u6-l1 L1 数据缓存](u6-l1-l1-dcache.md)，看本讲的缓存参数如何在真实的标签级 / 数据级里被使用。
- 想验证你对约束的理解，可直接打开 [cache_lru.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv) 与 [dcache_tag_stage.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv) 通读注释。
- 若对「综合到真实板子」感兴趣，可跳读 [hardware/fpga/de2-115/README.md](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/README.md)，看 `VENDOR_ALTERA` 宏在 DE2-115 板级工程里如何启用（对应大纲 u14 单元）。
- 想了解测试侧如何受配置影响（如改 `L2_WAYS` 要同步改 `flush_l2_cache`），可预习 [u15-1 测试框架与 CHECK 机制](u15-l1-test-harness.md)。
