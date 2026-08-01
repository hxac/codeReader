# 核心配置参数 define.v

## 1. 本讲目标

学完本讲后，你应当能够：

- 说出 `NUM_SM`、`NUM_WARP`、`NUM_THREAD`、`NUM_LANE` 这一组规模参数各自的含义，以及它们之间「谁是根参数、谁由谁派生」的关系。
- 把 `DCACHE_*` 与 `L2CACHE_*` 两组 cache 参数（组数、路数、块字数、MSHR 条目数等）和 cache 的物理结构对应起来，能手算出一个地址的 tag/set/offset 划分。
- 读懂 `define.v` 里三大类「编码宏」的组织方式：指令位模式（用 `?` 做通配）、操作类型 `FN_*`、CSR 地址，并大致知道 TileLink 操作码放在哪。
- 解释「为什么仿真前必须先确认 `NUM_THREAD`」——这是 README 反复强调、新手最容易踩的坑。

承接上一讲（u1-l2）建立的「`src/define` 是全项目配置总开关」的认知，本讲带你把这个总开关一格一格拆开来看。

## 2. 前置知识

- **Verilog 宏（`` `define ``）**：编译期文本替换，例如 `` `define NUM_THREAD 4 `` 之后，源码里所有 `` `NUM_THREAD `` 都会被替换成 `4`。它不是变量，不能在运行时改。
- **`$clog2(n)`**：Verilog 内建系统函数，返回「表示 \(n-1\) 这个数所需的最小位数」，即向上取整的对数。例如 `$clog2(4)=2`，`$clog2(5)=3`。在本项目里它大量用于「由数量推导位宽」。
- **cache 的组相联结构**：一个 cache 被分成若干「组（set）」，每组有若干「路（way）」。一个内存地址按位切成三段：`tag | set-index | block-offset`，先用 set-index 选组，再在该组的所有路里比对 tag。
- **MSHR（Miss Status Holding Register）**：cache 缺失状态保持寄存器。当多个请求同时 miss 到内存，MSHR 用来「登记在途的缺失」并把后来的同地址请求合并，避免重复发内存请求。
- **warp / thread / lane**（上一讲已建立）：一条向量指令广播给一个 warp 的所有线程并行执行，每个线程占一条 lane（数据通路）。本项目里 `NUM_THREAD`（每 warp 线程数）等于 lane 数。

> 提示：cache 的命中/缺失控制状态机、MSHR 的内部实现会在单元 6（u6-l1）详讲；TileLink 协议与 source 编码会在单元 7（u7-l1）详讲。本讲你只需要理解**这些参数是怎么定义、怎么互相派生的**。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用来讲解什么 |
| --- | --- | --- |
| [src/define/define.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v) | 全局配置宏（共 1270 行） | 规模参数、cache 参数、编码宏的逐组定义 |
| [src/define/undefine.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/undefine.v) | 对每个宏做 `` `undef `` 的配套文件 | 为什么需要「先反定义再定义」 |
| [README.md](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/README.md) | 综合指标与仿真入口 | 默认配置与综合配置的对比，以及「仿真前确认 NUM_THREAD」的提醒 |

`define.v` 文件本身是按「段落」组织的，大致顺序是：规模参数 → 基本位宽 → DCACHE 参数 → 共享内存参数 → L2CACHE 参数 → CTA 资源表参数 → 张量核维度 → AXI 位宽 → TileLink 操作码 → 指令位模式 → `FN_*` 操作码 → 舍入模式 → CSR 地址。本讲按最小模块挑其中最核心的几段来讲，而不是逐行背诵 1270 行。

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：

1. **4.1 规模参数族**：`NUM_CLUSTER` / `NUM_SM` / `NUM_WARP` / `NUM_THREAD` / `NUM_LANE` 及其派生关系（覆盖最小模块 NUM_THREAD、NUM_WARP、NUM_SM）。
2. **4.2 L1 数据缓存参数族**：`DCACHE_*`（覆盖最小模块 DCACHE 参数）。
3. **4.3 L2 缓存参数族**：`L2CACHE_*`（覆盖最小模块 L2CACHE 参数）。
4. **4.4 寄存器堆与执行单元规模**：`NUM_VGPR/SGPR`、`NUM_SFU`、`NUMBER_ALU/MUL/FPU`、共享内存参数。
5. **4.5 编码宏的组织方式**：指令位模式、`FN_*`、CSR 地址、TileLink 操作码。

### 4.1 规模参数族：NUM_THREAD / NUM_WARP / NUM_SM

#### 4.1.1 概念说明

这一族参数回答一个最基本的问题：「这片 GPGPU 有几个核？每个核能同时跑几个 warp？每个 warp 有几条线程？」它们是全项目的「根参数」——几乎所有位宽、深度、并行度都是从它们派生出来的。

四个核心概念：

- **NUM_CLUSTER（簇数）**：把若干 SM 编成一个簇（cluster），簇是 L2 互联的基本单位。默认为 1。
- **NUM_SM（核数 / CU 数）**：整个 GPGPU 里有几个 SM 核。默认为 2。
- **NUM_WARP（每核 warp 数）**：一个 SM 同时容纳的 warp 数量。默认为 8（写作 `4'b1000`）。
- **NUM_THREAD（每 warp 线程数 = lane 数）**：一个 warp 里有几条线程，也就是一条向量指令并行处理几个数据。默认为 **4**（仿真用的小配置）。

派生关系一览（默认值下）：

| 宏 | 定义 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `NUM_SM_IN_CLUSTER` | `NUM_SM/NUM_CLUSTER` | 2 | 每个簇里几个 SM |
| `NUM_LANE` | `NUM_THREAD` | 4 | lane 数 = 线程数 |
| `NUM_BLOCK` | `NUM_WARP` | 8 | 同时在跑的 workgroup(block) 数，不超过 warp 数 |
| `NUM_COLLECTORUNIT` | `NUM_WARP` | 8 | 操作数采集单元数 |
| `DEPTH_WARP` | `$clog2(NUM_WARP)` | 3 | warp 编号的位宽（0..7） |
| `DEPTH_THREAD` | `$clog2(NUM_THREAD)` | 2 | thread 编号的位宽（0..3） |
| `NUM_SFU` | `NUM_THREAD >> 2` | 1 | SFU 单元数 = 线程数/4 |

注意一个关键点：**`NUM_THREAD` 是这一族里「牵一发动全身」的根**。改它，lane 数、SFU 数、地址位宽全跟着变。

#### 4.1.2 核心流程

规模参数的「使用流程」是在编译期完成的：

1. 用户编辑 `define.v`，确定 `NUM_SM` / `NUM_WARP` / `NUM_THREAD`。
2. 编译时，各模块用 `generate` 语句按这些宏「展开」出对应数量的硬件。例如「有 `NUM_LANE` 条 lane，就生成 `NUM_LANE` 套 ALU」。
3. 派生宏（如 `DEPTH_THREAD`）自动算出对应位宽，喂给接口信号声明。

换句话说，这些宏不是「运行时可调的旋钮」，而是「重新综合/重新仿真前要敲定的图纸尺寸」。

#### 4.1.3 源码精读

规模参数集中在文件最开头：

- [src/define/define.v:3-17](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L3-L17)：定义 `NUM_CLUSTER`、`NUM_SM`、`NUM_SM_IN_CLUSTER`、`NUM_WARP`、`NUM_THREAD`、`NUM_LANE`、`NUM_BLOCK` 等根参数。这里能看到 `NUM_SM_IN_CLUSTER` 直接由 `NUM_SM/NUM_CLUSTER` 算出，`NUM_LANE` 直接等于 `NUM_THREAD`。
- [src/define/define.v:37](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L37)：`` `define NUM_SFU (`NUM_THREAD >> 2) ``——SFU（特殊功能单元，做除法/开方等慢运算）的数量是线程数的四分之一，因为这类运算慢，不需要每条 lane 配一个。
- [src/define/define.v:43-45](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L43-L45)：`DEPTH_WARP`、`DEPTH_THREAD` 用 `$clog2` 由数量推出编号位宽。

值得对比的是 README 给出的**综合配置**与本仓库**默认配置**：

- [README.md:25-33](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/README.md#L25-L33)：DC 综合（tsmc 28nm、620MHz、3.908mm²）用的是 `NUM_THREAD=32`、`NUM_SM=2`、`NUM_WARP=8`、`DCACHE_BLOCKWORDS=2`。
- 而 `define.v` 里默认 `NUM_THREAD=4`，是为快速功能仿真（`4w4t` 目标）准备的小配置。

这说明：**同一个 RTL 通过改 `define.v` 就能在「小而快（仿真）」和「大而真（综合）」之间切换**，这正是参数化设计的威力。

#### 4.1.4 代码实践（源码阅读 + 手算）

实践目标：验证你理解了「派生」这件事。

操作步骤：

1. 打开 [src/define/define.v:11-13](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L11-L13)，确认默认 `NUM_THREAD=4`。
2. 默算默认配置下这几个派生宏的值：`NUM_LANE`、`DEPTH_THREAD`、`NUM_SFU`。
3. 对照 [define.v:37](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L37) 和 [define.v:45](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L45) 核对。

预期结果（默认 `NUM_THREAD=4`）：

- `NUM_LANE = 4`
- `DEPTH_THREAD = $clog2(4) = 2`
- `NUM_SFU = 4 >> 2 = 1`

现象解释：`DEPTH_THREAD=2` 意味着 thread 编号只需要 2 位（00、01、10、11 共 4 个），刚好够给 4 条线程编号。如果 `NUM_THREAD` 不是 2 的幂，`$clog2` 仍能向上取整，但本项目约定它取 2 的幂。

#### 4.1.5 小练习与答案

**练习 1**：若把 `NUM_THREAD` 改成 16，`DEPTH_THREAD` 和 `NUM_SFU` 分别变成多少？

答案：`DEPTH_THREAD = $clog2(16) = 4`；`NUM_SFU = 16 >> 2 = 4`。

**练习 2**：`NUM_LANE` 为什么直接等于 `NUM_THREAD` 而不是另一个独立参数？

答案：因为本 GPGPU 是「一线程一 lane」的 SIMT 结构，一条向量指令需要并行处理 `NUM_THREAD` 个数据，就必须有 `NUM_THREAD` 条数据通路（lane）。所以 lane 数与线程数恒相等，没有必要拆成两个参数。

### 4.2 L1 数据缓存参数族 DCACHE_*

#### 4.2.1 概念说明

`DCACHE_*` 这一组参数描述 SM 内部的 L1 数据缓存（同时也被 L1 指令缓存复用 `BLOCKWORDS`）。它是一个**组相联**结构。理解这一组参数，等于理解了 L1 cache 的「几何尺寸」。

关键参数（默认值）：

| 宏 | 默认值 | 含义 |
| --- | --- | --- |
| `DCACHE_NSETS` | 32 | 组（set）数 |
| `DCACHE_NWAYS` | 2 | 每组的路（way）数 |
| `DCACHE_BLOCKWORDS` | 2 | 每个块（block）含几个字，L1D 与 L1I 共用 |
| `BYTESOFWORD` | 4 | 一个字 4 字节（32 位） |
| `DCACHE_MSHRENTRY` | 4 | MSHR 主表项数（能并行跟踪几个不同 set 的缺失） |
| `DCACHE_MSHRSUBENTRY` | 2 | 每个 MSHR 项的子项数（同 set 缺失的合并深度） |
| `DCACHE_WSHR_ENTRY` | 4 | 写缓冲（WSHR）项数 |
| `DCACHE_NLANES` | `NUM_THREAD` | cache 能一次服务的 lane 数 |

#### 4.2.2 核心流程

cache 用一个 32 位地址的位段来定位数据。地址按位从低到高切成：

\[ \text{地址} = \underbrace{\text{tag}}_{\text{DCACHE\_TAGBITS}} \;\big|\; \underbrace{\text{set-index}}_{\text{DCACHE\_SETIDXBITS}} \;\big|\; \underbrace{\text{block-offset}}_{\text{DCACHE\_BLOCKOFFSETBITS}} \;\big|\; \underbrace{\text{word-offset}}_{\text{DCACHE\_WORDOFFSETBITS}} \]

各段位宽由参数派生（默认值）：

- `DCACHE_WORDOFFSETBITS = $clog2(BYTESOFWORD) = $clog2(4) = 2`（字内选字节）
- `DCACHE_BLOCKOFFSETBITS = $clog2(DCACHE_BLOCKWORDS) = $clog2(2) = 1`（块内选字）
- `DCACHE_SETIDXBITS = $clog2(DCACHE_NSETS) = $clog2(32) = 5`（选组）
- `DCACHE_TAGBITS = XLEN - (SETIDXBITS + BLOCKOFFSETBITS + WORDOFFSETBITS) = 32 - (5+1+2) = 24`

所以默认配置下一个地址的划分是：`tag[31:8] | set[7:3] | block[2] | word[1:0]`，正好 32 位。

整个 L1 D-cache 的数据容量为：

\[ \text{容量} = \text{NSETS} \times \text{NWAYS} \times \text{BLOCKWORDS} \times 4\text{B} = 32 \times 2 \times 2 \times 4 = 512\text{B} \]

这个容量看起来很小——这是因为它只是默认仿真配置；真正面向综合/部署时会调大。重点是理解**参数之间如何拼出结构**，而非记住某个绝对数字。

#### 4.2.3 源码精读

DCACHE 参数集中在一个连续段落：

- [src/define/define.v:69-91](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L69-L91)：定义 `DCACHE_NSETS`、`DCACHE_NWAYS`、`DCACHE_BLOCKWORDS`、`DCACHE_WSHR_ENTRY`、`DCACHE_MSHRENTRY`、`DCACHE_MSHRSUBENTRY` 等结构参数。注释 `Both L1$D and L1$I use this parameter`（[L73](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L73)）说明 `BLOCKWORDS` 被指令与数据缓存共用。
- [src/define/define.v:77-87](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L77-L87)：用 `$clog2` 派生出各段位宽，最后 `DCACHE_TAGBITS` 用减法把剩余位全留给 tag——这正是 4.2.2 里地址划分的依据。
- [src/define/define.v:89-91](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L89-L91)：`DCACHE_MSHRENTRY=4`、`DCACHE_MSHRSUBENTRY=2`，决定了 L1 能并行处理多少个缺失。

#### 4.2.4 代码实践（手算地址划分）

实践目标：用默认参数手算一个地址的 tag/set/offset 划分，验证你读懂了参数。

操作步骤：

1. 默写出 `DCACHE_WORDOFFSETBITS`、`DCACHE_BLOCKOFFSETBITS`、`DCACHE_SETIDXBITS`、`DCACHE_TAGBITS` 的默认值。
2. 取一个示例地址，比如 `0x0000_0104`（= 0000...0100 0100），按上面的位段切开。

预期结果：

- `WORDOFFSETBITS=2`、`BLOCKOFFSETBITS=1`、`SETIDXBITS=5`、`TAGBITS=24`。
- 地址 `0x104` = 二进制 `...0001 0000 0100`，最低 2 位 `00` 是 word-offset；接下来 1 位 `1` 是 block-offset；再 5 位 `00001` 是 set-index（=1）；高位是 tag。

现象解释：同一个 set（set=1）里的两个 way 都会被 tag 比对；命中则返回数据，缺失则分配一个 MSHR 项去向 L2 发请求（详见 u6-l1）。

#### 4.2.5 小练习与答案

**练习 1**：把 `DCACHE_NWAYS` 从 2 改成 4，`DCACHE_TAGBITS` 会变大还是变小？为什么？

答案：变小。因为 `WAYIDXBITS` 不参与 tag 计算，但 `NWAYS` 加大通常意味着总容量变大；若保持 `NSETS` 不变，tag 位宽不变。正确推导：`TAGBITS = 32 - (SETIDXBITS + BLOCKOFFSETBITS + WORDOFFSETBITS)`，只依赖 set/offset，所以**单改 `NWAYS` 不影响 tag 位宽**。这是一个常见陷阱——路数影响的是「每组几路」，不进地址位段。

**练习 2**：默认配置下，L1 D-cache 同时能跟踪几个不同 set 的缺失？

答案：`DCACHE_MSHRENTRY = 4` 个主表项，意味着最多 4 个不同 set 的缺失可并行在途；每个 set 还能用 `DCACHE_MSHRSUBENTRY = 2` 个子项合并同 set 的后续缺失。

### 4.3 L2 缓存参数族 L2CACHE_*

#### 4.3.1 概念说明

L2 cache 基于 SiFive 的 block-inclusivecache（包含式缓存，L2 的内容包含所有 L1 的内容）。`L2CACHE_*` 这一组参数描述它的结构与对外接口宽度。

关键参数（默认值）：

| 宏 | 默认值 | 含义 |
| --- | --- | --- |
| `L2CACHE_NSETS` | 2 | 组数 |
| `L2CACHE_NWAYS` | 4 | 路数 |
| `L2CACHE_BLOCKWORDS` | `DCACHE_BLOCKWORDS` = 2 | 每块字数，跟随 L1 |
| `L2CACHE_WRITEBYTES` | 1 | 最小可写粒度（字节） |
| `L2CACHE_MEMCYCLES` | 4 | 向下访问内存的周期数（影响 MSHR 数量） |
| `L2CACHE_PORTFACTOR` | 2 | 端口因子 |

派生量：

- `L2CACHE_BLOCKBYTES = BLOCKWORDS × 4 = 8`
- `L2CACHE_BEATBYTES = BLOCKWORDS × 4 = 8`（一个 TileLink beat 的字节数）
- `L2CACHE_BLOCKS = NWAYS × NSETS = 8`
- `DATA_BITS = BEATBYTES × 8 = 64`（数据通道 64 位，恰好等于 `AXI_DATA_WIDTH`）
- `SOURCE_BITS = 12`（来源编码宽度，用于响应路由，见 u7-l1）

#### 4.3.2 核心流程

L2 的地址划分与 L1 类似，但多了一个 `L2C_BITS`（选择哪个 L2 实例，默认只有 1 个 L2，所以为 0）：

\[ \text{TAG\_BITS} = \text{ADDRESS\_BITS} - \text{SET\_BITS} - \text{OFFSET\_BITS} - \text{L2C\_BITS} = 32 - 1 - 3 - 0 = 28 \]

L2 的并行度由内存访问延迟决定，用一个公式算出需要多少 MSHR：

\[ \text{MSHRS} = \left\lceil \frac{\text{L2CACHE\_MEMCYCLES}}{\text{L2CACHE\_BLOCKBEATS}} \right\rceil = \left\lceil \frac{4}{1} \right\rceil = 4 \]

这表示：为了在 4 周期的内存延迟下不丢吞吐，L2 至少要能同时持有 4 个在途事务。

#### 4.3.3 源码精读

L2 参数分两段定义：上半段是「顶层用户可调」的，下半段是「由前者派生给 SiFive 代码用」的。

- [src/define/define.v:135-145](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L135-L145)：用户可调的 `L2CACHE_NSETS/NWAYS/BLOCKWORDS/WRITEBYTES/MEMCYCLES/PORTFACTOR`。
- [src/define/define.v:311-319](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L311-L319)：派生出 `L2CACHE_BLOCKBYTES`、`L2CACHE_BEATBYTES`、`L2CACHE_BLOCKS`、`L2CACHE_SIZEBYTES`。
- [src/define/define.v:333](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L333)：`SOURCE_BITS` 的定义式，把「来源（SM 号、cache 号、MSHR 项号、set 号）」打包进一个定宽字段，是 TileLink 响应路由的关键（详见 u7-l1）。
- [src/define/define.v:345-347](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L345-L347)：由 `MEMCYCLES` 派生 `MSHRS` / `SECONDARY`。

注意 `L2CACHE_SIZEBYTES` 默认只有 `8 × 8 = 64` 字节——这是 SiFive block-inclusivecache 在最小默认参数下的结果，仅够跑通功能仿真，不代表真实部署容量。这再次印证「define.v 默认值是仿真最小集」。

#### 4.3.4 代码实践（手算接口宽度）

实践目标：验证 `DATA_BITS` 与 `SOURCE_BITS` 的派生。

操作步骤：

1. 用 [define.v:313](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L313) 的 `L2CACHE_BEATBYTES = 8`，算 `DATA_BITS`。
2. 用 [define.v:333](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L333) 的公式算 `SOURCE_BITS`。

预期结果：

- `DATA_BITS = 8 × 8 = 64`，与 [define.v:228](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L228) 的 `AXI_DATA_WIDTH = 64` 一致——L2 的 TileLink 数据宽度与对外 AXI 数据宽度对齐，避免拼接。
- `SOURCE_BITS = 3 + $clog2(4) + $clog2(32) + $clog2(2) + $clog2(1) + 1 = 3+2+5+1+0+1 = 12`。

现象解释：L2 数据通道是 64 位，所以它和 AXI4 适配器（u7-l4）天然对齐；source 字段 12 位，刚好够编码「一个请求来自哪个 SM、哪个 cache、哪个 MSHR 项、哪个 set」，使 L2 能把响应原路送回。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `L2CACHE_BLOCKWORDS` 不独立设值，而是 `= DCACHE_BLOCKWORDS`？

答案：L2 是包含式缓存，L1 的一个块在 L2 里也有一份。让两者的块字数一致，保证 L1 和 L2 之间按块交换数据时无需拆分/拼接，简化控制逻辑。

**练习 2**：`MSHRS` 的公式里为什么要对 `L2CACHE_MEMCYCLES / BLOCKBEATS` 向上取整？

答案：内存延迟（`MEMCYCLES`）内可能到来多个请求，每个未完成请求都要占一个 MSHR 项。向上取整保证在最坏情况下（延迟完全填满）也有足够的项容纳在途事务，避免丢请求或 stall。

### 4.4 寄存器堆与执行单元规模

#### 4.4.1 概念说明

这一族参数回答：「每个 SM 有多少寄存器？多少执行单元？共享内存多大？」

关键参数（默认值）：

| 宏 | 默认值 | 含义 |
| --- | --- | --- |
| `NUM_VGPR` | 1024 | 向量通用寄存器（VGPR）数 |
| `NUM_SGPR` | 1024 | 标量通用寄存器（SGPR）数 |
| `NUM_BANK` | 4 | 寄存器堆分 bank 数 |
| `DEPTH_REGBANK` | `$clog2(NUM_VGPR/NUM_BANK)` = 8 | 每个 bank 的深度位宽 |
| `NUMBER_ALU` / `NUMBER_MUL` / `NUMBER_FPU` | `NUM_THREAD` = 4 | ALU/乘法/FPU 单元数 = lane 数 |
| `NUM_SFU` | `NUM_THREAD>>2` = 1 | SFU 单元数 |
| `SHAREDMEM_DEPTH` | 128 | 共享内存（LDS）深度（块数） |
| `SHAREMEM_SIZE` | `DEPTH × BLOCKWORDS × 4` = 1024B | 共享内存总容量 |
| `TC_DIM_M/N/K` | 2/2/2 | 张量核 M/N/K 维度 |

#### 4.4.2 核心流程

寄存器堆采用**多 bank 分体**结构以提升端口带宽：

\[ \text{每个 bank 深度} = \frac{\text{NUM\_VGPR}}{\text{NUM\_BANK}} = \frac{1024}{4} = 256 \quad\Rightarrow\quad \text{DEPTH\_REGBANK} = clog2(256) = 8 \]

把 1024 个寄存器均匀切成 4 个 bank，每个 bank 256 项。这样多个 lane 可以同时访问不同 bank 而不冲突（详见 u4-l1 操作数采集）。

执行单元数量与 lane 数绑定：`NUMBER_ALU = NUMBER_MUL = NUMBER_FPU = NUM_THREAD`，意思是「每条 lane 配一个 ALU、一个乘法器、一个 FPU」，所以一条向量运算指令能在所有 lane 上同时执行。SFU 因为慢，只配 `NUM_THREAD/4` 个，多条 lane 共享。

#### 4.4.3 源码精读

- [src/define/define.v:27-49](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L27-L49)：`NUM_BANK`、`NUM_VGPR`、`NUM_SGPR`、`NUM_SFU` 以及 `DEPTH_REGBANK` 的定义。
- [src/define/define.v:117-127](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L117-L127)：共享内存参数。`SHAREMEM_SIZE = DEPTH × BLOCKWORDS × 4`（[L123](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L123)），`SHAREMEM_NBANKS = DCACHE_BLOCKWORDS`（[L127](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L127)）。
- [src/define/define.v:219-223](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L219-L223)：张量核维度 `TC_DIM_M/N/K = 2/2/2`（详见 u5-l4）。
- [src/define/define.v:263-268](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L263-L268)：`NUMBER_ALU/MUL/FPU` 都等于 `NUM_THREAD`。

#### 4.4.4 代码实践（手算容量与 bank）

实践目标：手算共享内存容量与寄存器堆 bank 深度。

操作步骤：

1. 用 [define.v:123](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L123) 算 `SHAREMEM_SIZE`。
2. 用 [define.v:49](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L49) 算 `DEPTH_REGBANK`。

预期结果：

- `SHAREMEM_SIZE = 128 × 2 × 4 = 1024` 字节（1KB）。
- `DEPTH_REGBANK = $clog2(1024/4) = $clog2(256) = 8`。

现象解释：若把 `NUM_VGPR` 加大到 4096，`DEPTH_REGBANK` 会变成 `$clog2(1024)=10`，bank 地址位宽随之变宽——这就是为什么改寄存器堆规模时，相关接口位宽会自动跟着变。

> 待本地验证：上述 1024 字节是默认仿真值；CTA 调度器视角下每个 CU 可分配的 LDS 资源是 `NUMBER_LDS_SLOTS = 131072`（128kB，[define.v:157](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L157)），两者口径不同（一个是单 SM 共享内存实例容量，一个是调度器账本上的资源上限），实际部署值以本地综合配置为准。

#### 4.4.5 小练习与答案

**练习 1**：`NUMBER_ALU` 为什么等于 `NUM_THREAD` 而不是 1？

答案：因为向量 ALU 要在所有 lane 上并行执行同一条指令，每条 lane 需要一个独立的 ALU，所以 ALU 数 = lane 数 = `NUM_THREAD`。若只有 1 个 ALU，就只能串行处理，丧失 SIMT 并行优势。

**练习 2**：`NUM_SFU = NUM_THREAD >> 2` 体现了什么设计取舍？

答案：SFU 处理除法、开方、指数等高延迟运算，单元面积大、使用频率低。给每条 lane 都配一个 SFU 既浪费面积又难以提升吞吐（运算本身慢），因此用 4 条 lane 共享 1 个 SFU，在面积与性能之间折中。

### 4.5 编码宏的组织方式：指令位模式 / FN_* / CSR / TileLink

#### 4.5.1 概念说明

除了「规模」参数，`define.v` 还承担另一个重任：定义**指令与接口的编码常量**。这部分占了文件一半以上篇幅，但组织很有规律，分四大类：

1. **指令位模式**：每条指令一个 32 位的「带通配的模板」，用 `?` 表示「任意位」。译码器拿到的 32 位指令去和这些模板匹配，匹配上就知道是哪条指令。
2. **操作类型 `FN_*`**：6 位的功能码，告诉执行单元「这条指令具体做什么运算」（加、减、乘、比较……）。注意整数、浮点、SFU 各有一套 `FN_*`，**编码空间是复用的**（例如 `FN_ADD=0` 和 `FN_FADD=0` 不冲突，因为分别由 ALU/FPU 使用）。
3. **CSR 地址**：12 位的 CSR 寄存器地址，既有 RISC-V 标准机器态 CSR，也有 Ventus 自定义 CSR（如 `CSR_WG_ID`、`CSR_RPC`）。
4. **TileLink 操作码**：3 位的通道操作码（`GET`、`PUTFULLDATA`、`ACQUIREBLOCK`……），分 L1 用（`TLAOP_*`）和 L2 用两套。

#### 4.5.2 核心流程

编码宏在译码与执行中的流转：

1. **译码**：`decodeUnit`（u3-l3）把 32 位指令与「指令位模式」逐条比对，识别出指令名，再据字段译出该指令的 `FN_*`、立即数类型、操作数来源等控制信号。
2. **执行**：执行单元（ALU/FPU/SFU）拿到 `FN_*` 功能码，在一个大 `case` 里选择对应的运算。
3. **CSR 访问**：`csrexe`（u5-l2）用 CSR 地址定位寄存器，做读/写/置位/清位。
4. **总线交互**：cache 用 `TLAOP_*` / TileLink 操作码组装请求、解析响应。

#### 4.5.3 源码精读

- **指令位模式**：[src/define/define.v:562-1182](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L562-L1182)。例如 [L563](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L563) 的 `ADDI` 模板 `32'b?????????????????000?????0010011`，末 7 位 `0010011` 是 ADDI 的 opcode，中间 `000` 是 funct3，其余 `?` 是任意的立即数和寄存器号字段；[L717](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L717) 的 `VADD_VV` 模板同理。
- **整数 `FN_*`**：[src/define/define.v:469-498](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L469-L498)，`FN_ADD=6'd0`、`FN_SUB=6'd10`、`FN_MUL=6'd20` 等。
- **浮点 `FN_*`**：[src/define/define.v:513-542](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L513-L542) 与 [L1184-1209](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L1184-L1209)（浮点功能码重新完整定义了一遍，含 `FN_F2I`/`FN_I2F` 等类型转换）。
- **SFU `FN_*`**：[src/define/define.v:544-555](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L544-L555)，`FN_DIV`、`FN_FSQRT`、`FN_EXP` 等，编码与整数 `FN_*` 复用（`FN_DIV=0` 与 `FN_ADD=0` 同值，但分别由 SFU/ALU 解读）。
- **CSR 地址**：[src/define/define.v:1218-1270](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L1218-L1270)。RISC-V 标准如 `CSR_MSTATUS=12'h300`（[L1252](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L1252)）；Ventus 自定义如 `CSR_WG_ID=12'h804`（[L1226](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L1226)）、`CSR_KNL_BASE=12'h803`（[L1225](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L1225)）、`CSR_RPC=12'h80c`（[L1234](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L1234)）、`CSR_PDS_BASEADDR=12'h807`（[L1229](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L1229)）。
- **TileLink 操作码**：[src/define/define.v:375-414](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L375-L414)（A/D 通道，`PUTFULLDATA=0`、`GET=4`、`ACQUIREBLOCK=6` 等）；L1 专用 `TLAOP_*` 见 [L271-305](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L271-L305)（`TLAOP_GET=4`、`TLAOP_PUTFULL=0`、`TLAOP_FLUSH=5`）。

> 补充：[src/define/undefine.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/undefine.v) 把 `define.v` 里**每一个宏**都对应写了一条 `` `undef ``。它被 [model_list:186](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/model_list#L186) 放在清单最后一行，与首行 [model_list:1](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/model_list#L1) 的 `define.v` 首尾呼应。其用途是：当需要在一次编译里切换配置（先 undef 旧值、再 define 新值）或防止宏被重复定义时报错时，提供一个干净的「重置」入口。

#### 4.5.4 代码实践（读模板与查码）

实践目标：学会读指令位模式、查 `FN_*` 与 CSR 地址。

操作步骤：

1. 读 [define.v:563](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L563) 的 `ADDI` 模板，说出它的 opcode（末 7 位）和 funct3。
2. 查 `FN_ADD`、`FN_MUL` 的编码值（[L471](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L471)、[L491](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L491)）。
3. 查自定义 CSR `CSR_WG_ID`、`CSR_RPC` 的地址。

预期结果：

- `ADDI` opcode = `0010011`，funct3 = `000`。
- `FN_ADD = 6'd0`，`FN_MUL = 6'd20`。
- `CSR_WG_ID = 12'h804`，`CSR_RPC = 12'h80c`。

现象解释：译码器只要把指令末 7 位匹配上 `0010011`、funct3 匹配上 `000`，就能判定这是 `ADDI`；而自定义 CSR 用 `0x8xx` 段地址，与标准机器态 CSR（`0x3xx`）区分开，专供 Ventus 的派发与执行流程使用。

#### 4.5.5 小练习与答案

**练习 1**：`FN_ADD`（整数加）和 `FN_FADD`（浮点加）都是 `6'd0`，为什么不会冲突？

答案：因为它们分别送给不同的执行单元——`FN_ADD` 进 ALU，`FN_FADD` 进 FPU。两个执行单元各自有自己的 `case` 表，互不可见，所以编码空间可以复用。这是硬件设计中常见的「按执行单元分命名空间」做法。

**练习 2**：`ADDI` 的位模式里有大量 `?`，它代表什么？译码器如何利用它？

答案：`?` 表示该位「0 或 1 都行」，对应指令里可变的字段（立即数、源/目的寄存器号）。译码器在匹配时只关心固定为 0/1 的位（opcode、funct3），忽略 `?` 位；匹配成功后，再单独从这些 `?` 位置提取出立即数和寄存器号。

## 5. 综合实践

把本讲五个最小模块串起来，完成 README 强调的「仿真前确认 `NUM_THREAD`」全流程。

### 实践目标

通过把 `NUM_THREAD` 在 4 / 8 / 16 / 32 之间切换，亲眼看到「一个根参数如何牵动整片派生宏」，并理解为什么仿真前必须让它与测试程序匹配。

### 操作步骤

1. 打开 [src/define/define.v:11](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L11)，把 `` `define NUM_THREAD 4 `` 依次改成 8、16、32。
2. 每改一次，记录下列派生宏的值（按 [4.1.3](#413-源码精读) 的定义式手算）：
   - `NUM_LANE`（[L13](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L13)）
   - `DEPTH_THREAD`（[L45](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L45)）
   - `NUM_SFU`（[L37](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L37)）
   - `DCACHE_NLANES`（[L93](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L93)）
   - `NUMBER_ALU`（[L264](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L264)）
3. 填写下表（先自己算，再对照下方答案）：

| `NUM_THREAD` | `NUM_LANE` | `DEPTH_THREAD` | `NUM_SFU` | `DCACHE_NLANES` | `NUMBER_ALU` |
| --- | --- | --- | --- | --- | --- |
| 4 | 4 | 2 | 1 | 4 | 4 |
| 8 | ? | ? | ? | ? | ? |
| 16 | ? | ? | ? | ? | ? |
| 32 | ? | ? | ? | ? | ? |

4. 进入 `testcase/test_gpgpu_axi_top/tc_gaussian`，按 README（[L35-44](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/README.md#L35-L44)）执行 `make run-vcs-4w4t`，观察 `PASSED`/`FAILED`。

### 预期结果

第 3 步表格答案：

| `NUM_THREAD` | `NUM_LANE` | `DEPTH_THREAD` | `NUM_SFU` | `DCACHE_NLANES` | `NUMBER_ALU` |
| --- | --- | --- | --- | --- | --- |
| 8 | 8 | 3 | 2 | 8 | 8 |
| 16 | 16 | 4 | 4 | 16 | 16 |
| 32 | 32 | 5 | 8 | 32 | 32 |

### 需要观察的现象与解释

- **派生量随 `NUM_THREAD` 单调变化**：`NUM_LANE`、`DCACHE_NLANES`、`NUMBER_ALU` 与 `NUM_THREAD` 严格相等；`NUM_SFU = NUM_THREAD/4`；`DEPTH_THREAD = $clog2(NUM_THREAD)`。改一行，这一列全变——这就是「参数化设计」的核心。
- **为什么仿真前必须确认 `NUM_THREAD`**（README [L39](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/README.md#L39) 反复强调）：
  1. 测试用的 kernel 程序（二进制）是按某个向量长度 VLEN 编译的，而 VLEN = `NUM_THREAD × 32`。若 RTL 的 `NUM_THREAD` 与程序期望的不一致，向量寄存器的划分、活跃线程掩码的语义全对不上，结果必然错误。
  2. Makefile 目标名（如 `run-vcs-4w4t`）暗示了「4 warp × 4 thread」，配套的测试数据就是为该配置准备的。`define.v` 必须与之一致。
  3. 默认 `define.v` 是 `NUM_THREAD=4` 的小配置（快速仿真），而 README 综合指标用的是 `NUM_THREAD=32`（[L28](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/README.md#L28)）。两者不能混用。

> 待本地验证：第 4 步的实际仿真输出（PASSED/FAILED 与周期数）取决于本地 VCS/Verdi 环境。建议先在默认 `4w4t` 下跑通，再尝试切换 `NUM_THREAD` 并换用对应的 kernel 二进制，观察结果是否仍 PASSED。若仅改 `NUM_THREAD` 而不换配套程序，通常会 FAILED——这恰好印证了上面的解释。

## 6. 本讲小结

- `define.v` 是全项目「配置 + 编码」总开关：既管规模（核数/warp/线程/cache），也管编码（指令位模式/`FN_*`/CSR/TileLink）。
- 规模参数族以 `NUM_THREAD` 为核心根参数，派生出 `NUM_LANE`、`DEPTH_THREAD`、`NUM_SFU`、`DCACHE_NLANES`、`NUMBER_ALU/MUL/FPU` 等，牵一发动全身。
- `DCACHE_*` 与 `L2CACHE_*` 用「组数 × 路数 × 块字数」描述 cache 几何，地址的 tag/set/offset 划分全部由这些参数用 `$clog2` 和减法派生。
- 寄存器堆采用多 bank 分体（`NUM_VGPR/NUM_BANK` → `DEPTH_REGBANK`），执行单元数与 lane 数绑定，共享内存与张量核维度也在此定义。
- 编码宏按四类组织：指令位模式（`?` 通配）、`FN_*` 功能码（按执行单元分命名空间、可复用编码）、CSR 地址（标准 + 自定义 `0x8xx`）、TileLink 操作码（L1 `TLAOP_*` 与 L2 两套）。
- `undefine.v` 与 `define.v` 首尾呼应，提供宏的「重置」入口；README 默认 `NUM_THREAD=4` 用于仿真，综合用 `NUM_THREAD=32`，仿真前必须让二者与测试程序一致。

## 7. 下一步学习建议

- 想看「规模参数如何生成实际硬件」：进入 [u3-l1 SM 流水线总览 pipe.v](u3-l1-sm-pipeline-overview.md)，观察 `NUM_LANE` 如何驱动 generate 生成 lane 阵列。
- 想看「cache 参数如何驱动命中/缺失」：阅读 [u6-l1 数据缓存 dcache 与 MSHR](u6-l1-dcache-and-mshr.md)，对照本讲的 `DCACHE_*` 理解控制状态机。
- 想看「编码宏如何被译码使用」：阅读 [u3-l3 指令缓冲 ibuffer 与译码 decodeUnit](u3-l3-ibuffer-and-decode.md)，看 `decodeUnit` 如何用本讲的指令位模式与 `FN_*`。
- 想看「自定义 CSR 如何参与派发」：阅读 [u5-l2 CSR 寄存器与分支](u5-l2-csr-and-branch.md)，对照本讲的 `CSR_WG_ID`、`CSR_RPC` 等。
- 想看「TileLink 操作码与 source 编码」：阅读 [u7-l1 TileLink 协议基础](u7-l1-tilelink-protocol.md)，深入 `SOURCE_BITS` 的逐段含义。
