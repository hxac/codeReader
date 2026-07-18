# cache 子系统深入

> 本讲是专家层（第 5 单元）的一篇，承接 [u3-l1 命名空间包模式](u3-l1-namespace-package-pattern.md) 与 [u3-l3 片上 RAM 抽象：ocram 家族](u3-l3-ocram-memory-abstraction.md)。在阅读本讲前，你应当已经理解 PoC 的「命名空间根包 + `.files` 编译清单」组织方式，以及片上 RAM 的抽象思路。本讲会把这些机制汇聚到一个真实、可综合的硬件缓存子系统上。

## 1. 本讲目标

学完本讲，你应当能够：

- 画出 PoC `cache` 命名空间各实体的**真实分层与实例化关系**，并能指出「顶层缓存」「标签单元」「替换策略」「控制器」分别由哪个实体承担。
- 读懂 `cache_par` 顶层缓存的数据通路，说清楚一次**命中读请求**从 `Request` 拉高到 `CacheLineOut` 有效的完整时序，以及各输出的延迟周期数。
- 区分**并行标签单元**（`cache_tagunit_par`）与**顺序标签单元**（`cache_tagunit_seq`）的设计差异，并理解全相联（FA）、直接映射（DM）、组相联（SA）三种结构在 `generate` 中如何分别展开。
- 解释 `cache_cpu` 控制器的有限状态机（FSM）如何在「命中即返回 / 未命中访问存储」之间切换，并说出它的写策略。
- 说明 `cache_replacement_policy` 如何把字符串 `"LRU"` 映射到 `sort_lru_cache` 这一硬件 LRU 实现，以及替换策略如何接入组相联缓存的每一个 cache-set。

## 2. 前置知识

### 2.1 为什么需要缓存

CPU 的运算速度远高于主存。若每次取数都直接访问主存，CPU 流水线会长时间「空转」等待数据。缓存的思路是：在 CPU 与主存之间放一块容量小但速度快（通常是片上 SRAM/BRAM）的存储，把最近用过的数据连同行（cache line）缓存起来。其背后的**局部性原理**：

- 时间局部性：刚被访问过的地址，短期内很可能再次被访问。
- 空间局部性：被访问地址的邻居，很可能很快也被访问（所以一次搬一整行）。

缓存要回答的核心问题只有两个：**「这次访问的数据在不在缓存里？」（命中 / 缺失）**，以及**「缓存满了之后，腾出谁的行？」（替换策略）**。本讲涉及的每个实体，都是在硬件上回答这两个问题。

### 2.2 缓存的三种映射结构

设缓存共有 \(N\) 行（cache lines），每行存一个数据块与一个「标签（tag）」用来标记它对应主存的哪个地址。

- **直接映射（Direct-Mapped，DM）**：主存每个地址只能落在缓存里的**唯一**一行。地址被切成 `tag | index`，`index` 直接决定落到哪一行。优点：硬件简单、比较器少；缺点：多个地址争用同一行，容易抖动（thrashing）。
- **全相联（Fully-Associative，FA）**：主存每个地址可以放在缓存**任意**一行。需要把地址与**所有**行的标签同时比较。优点：命中率最高；缺点：比较器数量等于行数，面积大。
- **组相联（Set-Associative，SA）**：折中。缓存分成若干「组（set）」，每个组有 \(A\) 行（\(A\) 路相联）。地址切成 `tag | index | ...`，`index` 选定一个组，组内全相联。地址位宽拆分为：

  \[
  \text{INDEX\_BITS} = \lceil \log_2(\text{CACHE\_SETS}) \rceil,\quad
  \text{CACHE\_SETS} = N / A
  \]

  \[
  \text{TAG\_BITS} = \text{ADDRESS\_BITS} - \text{INDEX\_BITS},\quad
  \text{WAY\_BITS} = \lceil \log_2 A \rceil
  \]

> 公式里用到的 \(\lceil \log_2(\cdot) \rceil\) 正是 [u2-l2](u2-l2-utils-package.md) 讲过的 `log2ceil`；PoC 在这里用的是它的「非零」变体 `log2ceilnz`，定义在 `utils.vhdl` 中。

### 2.3 读优先 / 写优先（read-first / write-first）

当对同一存储地址「同一拍既读又写」时，读端口返回的是旧值还是新值？返回旧值叫 **read-first（写时返回旧数据）**，返回新值叫 **write-first**。这个「读-写冲突行为」会决定综合器能不能把一段数组推断成 BRAM——这正是 [u3-l3](u3-l3-ocram-memory-abstraction.md) 里强调的点，也是本讲 `cache_par` 选择存储模型时的关键约束。

## 3. 本讲源码地图

本讲涉及的文件全部位于 `src/cache/` 与 `src/sort/` 下。先用一张表建立空间地图：

| 文件 | 角色 | 关键内容 |
| --- | --- | --- |
| [src/cache/cache.pkg.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache.pkg.vhdl) | 命名空间根包 | 目前非常精简，只定义命中/缺失枚举 `T_CACHE_RESULT` 与转换函数 |
| [src/cache/cache_par.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_par.vhdl) | **顶层缓存（并行）** | 把并行标签单元与数据存储并排组合，是本讲主轴 |
| [src/cache/cache_tagunit_par.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_tagunit_par.vhdl) | 并行标签单元 | FA/DM/SA 三种结构用 `generate` 分别实现，全并行比较标签 |
| [src/cache/cache_tagunit_seq.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_tagunit_seq.vhdl) | 顺序标签单元 | 按 chunk（块）逐拍串行比较标签的另一种设计，仅 FA 实现 |
| [src/cache/cache_replacement_policy.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_replacement_policy.vhdl) | 替换策略封装 | 用字符串选策略，LRU 委托给 `sort_lru_cache` |
| [src/sort/sort_lru_cache.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sort/sort_lru_cache.vhdl) | LRU 硬件实现 | 只存「键（cache 行索引）」的优化 LRU 列表 |
| [src/cache/cache_cpu.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_cpu.vhdl) | **缓存控制器（CPU 侧）** | 在缓存外加状态机，处理命中 / 未命中 / 写直达 |
| [src/cache/cache_mem.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_mem.vhdl) | 存储侧包装 | 在 CPU 与主存之间插入缓存，支持多个未完成请求 |

> **先记一张「实例化树」**，这是本讲最重要的事实，后续所有细节都挂在这棵树上：
>
> ```
> cache_mem  ──包含──>  cache_cpu  ──包含──>  cache_par2 ──包含──>  cache_tagunit_par
>                                                                  └─ cache_replacement_policy ──> sort_lru_cache
> ```
>
> 注意：本讲精读的 `cache_par`（不带 2）是「并行标签 + 数据存储」的最小完整缓存原型，它实例化的是 `cache_tagunit_par`；而真正被控制器使用的版本是 `cache_par2`（接口略不同）。两者共用同一套标签单元与替换策略。本讲以 `cache_par` 为切入点讲清机制，再用 `cache_cpu` / `cache_mem` 讲清「控制器如何套在缓存外」。

## 4. 核心概念与源码讲解

### 4.1 缓存命名空间总览与顶层结构

#### 4.1.1 概念说明

`PoC.cache` 命名空间提供「不同映射结构 + 不同标签比较方式 + 不同集成形态」的缓存实现组合。从 [src/cache/README.md](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/README.md) 可以看到它对外暴露 7 个实体。理解它们的关键不是逐个背诵，而是分清**三层职责**：

1. **标签单元（tag unit）**：回答「在不在缓存里」。`cache_tagunit_par`（并行比较）与 `cache_tagunit_seq`（顺序比较）是两种实现。
2. **替换策略（replacement policy）**：回答「满了腾谁」。`cache_replacement_policy` 是统一入口，内部按字符串分发。
3. **缓存主体与控制器**：把标签单元、数据存储、替换策略组装成一个可用缓存，并（可选地）在外面套一层控制状态机，对接 CPU 流水线或存储总线。

`cache_par` 正是第 3 层里**最朴素**的一个：它把并行标签单元与一个数据存储「并排」放在一起，对外暴露请求/替换命令，但不负责「未命中时去主存搬数据」——那件事由更上层的控制器 `cache_cpu` 完成。

#### 4.1.2 核心流程

`cache_par` 把一次缓存操作抽象成一张「命令真值表」（见文件头注释），由 4 个控制信号组合出 6 种命令：

| Request | ReadWrite | Invalidate | Replace | 命令 |
| --- | --- | --- | --- | --- |
| 0 | 0 | 0 | 0 | 无操作 |
| 1 | 0 | 0 | 0 | 读缓存行 |
| 1 | 1 | 0 | 0 | 更新（写）缓存行 |
| 1 | 0 | 1 | 0 | 读并丢弃（失效）该行 |
| 1 | 1 | 1 | 0 | 写并丢弃该行 |
| 0 | — | 0 | 1 | 替换缓存行（从主存搬入新行） |

整体流程（伪代码）：

```
每个时钟上升沿：
  1. 把 Request/ReadWrite/Invalidate/Replace 与 Address 送给标签单元
  2. 标签单元（组合逻辑）算出：
       - TagHit / TagMiss  （在不在）
       - LineIndex          （在的话，数据存在哪一行）
       - ReplaceLineIndex   （要替换的话，腾哪一行）
  3. 数据存储用一个二选一选出本次访问的行号：
       MemoryIndex = Request ? LineIndex : ReplaceLineIndex
  4. 命中写或替换时，把 CacheLineIn 写入 CacheMemory(MemoryIndex)
  5. 组合读出 CacheMemory(MemoryIndex)，寄存一拍后输出到 CacheLineOut
  6. TagHit/TagMiss 也寄存一拍后输出到 CacheHit/CacheMiss
```

关键时序结论（来自文件头注释）：**每条命令在 1 个时钟周期内完成，但所有输出都有 1 拍延迟**。

#### 4.1.3 源码精读

先看顶层 `cache_par` 的 generic 与 port，这是理解整个子系统的入口：

[src/cache/cache_par.vhdl:L91-L115](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_par.vhdl#L91-L115) — 顶层实体声明。generic 用 `CACHE_LINES`（行数）、`ASSOCIATIVITY`（相联度）、`ADDRESS_BITS`、`DATA_BITS` 描述缓存几何；端口就是上面真值表里的 4 个控制信号加地址、数据与命中/缺失输出。注意 `LINE_INDEX_BITS` 由 `log2ceilnz(CACHE_LINES)` 推导（见下一段）。

接着是架构里的关键常量与内部信号：

[src/cache/cache_par.vhdl:L118-L136](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_par.vhdl#L118-L136) — 行索引位宽 `LINE_INDEX_BITS = log2ceilnz(CACHE_LINES)`；数据存储 `CacheMemory` 是一个以行为元素的数组；与标签单元之间的握手信号（`TU_LineIndex`、`TU_TagHit`、`TU_TagMiss`、`TU_ReplaceLineIndex`、`TU_OldAddress`）。

`cache_par` 把标签比较整体委托给一个并行标签单元实例：

[src/cache/cache_par.vhdl:L141-L163](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_par.vhdl#L141-L163) — 直接实体例化 `PoC.cache_tagunit_par`，把同样的 `REPLACEMENT_POLICY/CACHE_LINES/ASSOCIATIVITY/ADDRESS_BITS` 透传，并把请求/替换命令与地址送给它，回取 `LineIndex/TagHit/TagMiss` 与替换用的 `ReplaceLineIndex/OldAddress`。

> **事实更正**：本讲的规划里提到了 `cache_tagunit_seq`，但 `cache_par` 在源码里**实际**例化的是 `cache_tagunit_par`（并行版）。`cache_par.files` 清单也只编译了 `cache_tagunit_par`。顺序版 `cache_tagunit_seq` 是同一职责的另一种设计，4.2 节会专门对比。

数据存储用一个简单的二选一决定本次访问哪一行，再用一个钟控进程完成「按需写入 + 组合读出」：

[src/cache/cache_par.vhdl:L165-L191](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_par.vhdl#L165-L191) — 重点三句：
- 行号二选一：`Request='1'` 时用命中行号 `TU_LineIndex`，否则用替换行号 `TU_ReplaceLineIndex`。
- 写条件：`(Request and TU_TagHit and ReadWrite) or Replace`——即「命中写」或「替换搬入」时才更新数据。
- 读与控制输出：`CacheLineOut`、`CacheHit`、`CacheMiss`、`OldAddress` 全部在这个 `rising_edge` 进程里寄存，所以都是 1 拍延迟。

文件头注释明确点出：这里推断的是**单端口、读优先（read-first）**存储（`"single-port memory with read before write"`），因此**不能**映射到 [u3-l3](u3-l3-ocram-memory-abstraction.md) 讲过的 `ocram_sdp`（简单双端口）。这种存储（如 LUT-RAM）并非所有器件都有，所以综合可能退化为大量触发器加多路选择器，效率很低——这也是 README 推荐 `cache_par2` 的原因。

#### 4.1.4 代码实践

**实践目标**：在本地用 Python（cocotb）跑通 `cache_par` 的现成测试台，或做一次源码阅读型实践。

**操作步骤（仿真型）**：

1. 浏览测试台 [tb/cache/cache_par_cocotb.py](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/cache/cache_par_cocotb.py) 与 [tb/cache/cache_par_tb.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/cache/cache_par_tb.files)，注意它**只支持 Cocotb（Python）测试台**，并用 `tb/common/lru_dict.py` 作为软件参考模型做记分板比对。
2. 若本地已装好 cocotb 与某款仿真器（如 GHDL/Icarus/ModelSim），按 pyIPCMI 流程（见 [u5-l1](u5-l1-pyipcmi-infrastructure.md)）运行该测试台。
3. 若无法运行，转为下面的源码阅读型实践。

**操作步骤（源码阅读型，推荐）**：定位 [cache_par.vhdl 第 166–191 行](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_par.vhdl#L166-L191)，回答两个问题：
- 数据存储的写入条件 `(Request and TU_TagHit and ReadWrite) or Replace` 里，为什么必须同时要求 `TU_TagHit='1'`？如果去掉它会怎样？
- 为什么 `CacheHit/CacheMiss/CacheLineOut` 必须在 `rising_edge(Clock)` 进程里寄存，而不能直接把 `TU_TagHit` 组合输出？

**需要观察的现象 / 预期结果**：
- 若能跑仿真，测试台应在多种随机地址序列下 PASS（因为它用 LRU 参考模型逐拍比对）。
- 源码层面应得出结论：「命中写」要求命中才写，是为了避免把数据写到错的行；输出寄存是为了让数据与控制信号**同拍到达**（延迟一致），便于上层流水线对齐。**待本地验证**仿真结果。

#### 4.1.5 小练习与答案

**练习 1**：`cache_par` 的数据存储为什么不能直接换成 `ocram_sdp`？
**参考答案**：`cache_par` 注释明确要求「单端口、读优先（read before write）」行为——写时返回旧数据。`ocram_sdp` 是简单双端口存储，混合端口读-写冲突行为不同（[u3-l3](u3-l3-ocram-memory-abstraction.md) 讲过它用 `ramstyle="no_rw_check"`），无法保证这里的 read-first 语义，因此注释里直接写明不能映射。

**练习 2**：把 `CACHE_LINES=32, ASSOCIATIVITY=32, ADDRESS_BITS=8, DATA_BITS=8` 代入，这个缓存最多能存多少比特数据？它属于哪种映射结构？
**参考答案**：数据容量 \(32 \times 8 = 256\) 比特。因为 `CACHE_LINES == ASSOCIATIVITY`，属于**全相联（FA）**结构。

---

### 4.2 标签单元：并行比较与顺序比较

#### 4.2.1 概念说明

标签单元是缓存里「回答命中/缺失」的部件。给定一个地址，它要做两件事：

1. 拿这个地址（的 tag 部分）去和缓存里每一行存的 tag 比较；
2. 如果有任意一行匹配且该行有效（valid），就报告命中，并给出这一行的索引。

「比较」可以有两种硬件做法：

- **并行比较（parallel）**：一拍内同时和**所有**行的 tag 比较，用一组并行比较器 + 一个 `onehot2bin`（独热转二进制）编码器得到命中行号。速度快（组合逻辑，当拍出结果），但比较器数量 = 行数/相联度，面积大。对应 `cache_tagunit_par`。
- **顺序比较（sequential）**：把 tag 切成多个小块（chunk），逐拍送入、逐拍比较，多拍才出结果。比较器只需 1 个，面积小，但延迟大、吞吐低。对应 `cache_tagunit_seq`。

PoC 的做法是用 `generate` 在**同一份实体**里按 `CACHE_LINES` 与 `ASSOCIATIVITY` 的关系自动选择 FA/DM/SA 三种结构，体现了「一份源码、多形态」的设计。

#### 4.2.2 核心流程（以并行版 FA 为例）

```
组合逻辑（当拍）：
  for 每一行 i in 0..CACHE_LINES-1:
      TagHits(i) = (TagMemory(i) == Address) and (ValidMemory(i) == '1')
  HitWay      = onehot2bin(TagHits)      # 独热向量 → 行号
  TagHit      = (or(TagHits)) and Request
  TagMiss     = not (or(TagHits)) and Request

时序逻辑（上升沿）：
  if Replace:        把 Address 写入 TagMemory(ReplaceWay)，置 Valid
  if Invalidate命中: 清掉对应 Valid 位
```

DM 与 SA 的差别在「地址怎么切」与「比较器有几组」：

- DM（`ASSOCIATIVITY=1`）：地址切 `tag | index`，按 `index` 读**一行**的 tag 来比，只要 1 个比较器。
- SA：地址切 `tag | index`，`index` 选定一个 set，set 内 \(A\) 路全相联——所以要 \(A\) 个并行比较器，且每个 cache-set 有自己的替换策略实例。

#### 4.2.3 源码精读

**并行标签单元** `cache_tagunit_par` 的实体声明：

[src/cache/cache_tagunit_par.vhdl:L114-L137](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_tagunit_par.vhdl#L114-L137) — 注意它有两组接口：`Replace`/`ReplaceLineIndex`/`OldAddress` 负责「腾行」，`Request`/`ReadWrite`/`Invalidate`/`Address`/`LineIndex`/`TagHit`/`TagMiss` 负责「查询」。文件头注释强调：查询结果是**组合逻辑当拍输出**（`"indicate ... immediately (combinational)"`），而 valid/tag 的更新在时钟沿。

全相联分支 `genFA`（`CACHE_LINES = ASSOCIATIVITY`）的核心是「一组并行比较器 + 独热转二进制」：

[src/cache/cache_tagunit_par.vhdl:L185-L194](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_tagunit_par.vhdl#L185-L194) — 用一个 `for` 循环把 `Address` 与每行的 `TagMemory(i)` 比较并乘上 `ValidMemory(i)`，得到独热命中向量 `hits`；再用 `onehot2bin(hits, 0)` 得到命中行号 `HitWay`。`onehot2bin` 来自公共包 `vectors`（[u2-l4](u2-l4-physical-strings-vectors-math.md)）。

命中/缺失信号就是「独热向量有没有任何一位置 1」：

[src/cache/cache_tagunit_par.vhdl:L216-L217](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_tagunit_par.vhdl#L216-L217) — `TagHit_i = slv_or(TagHits) and Request`，`TagMiss_i = not slv_or(TagHits) and Request`。`slv_or` 是「把整个向量或起来」的辅助函数。两者互斥且都受 `Request` 门控，没有请求时既不报命中也不报缺失。

直接映射分支 `genDM`（`ASSOCIATIVITY=1`）演示地址如何切片：

[src/cache/cache_tagunit_par.vhdl:L295-L304](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_tagunit_par.vhdl#L295-L304) — `Address_Tag` 取地址高位、`Address_Index` 取地址低位（共 `INDEX_BITS = log2ceilnz(CACHE_LINES)` 位）；按 `Address_Index` 读出一行的 `Tag` 与 `Valid`，比较得到 `DM_TagHit`。注释还提醒：若 `Address` 来自寄存器，Xilinx 上必须把 `TagMemory` 实现为分布式 RAM（`ram_style="distributed"`）才能得到正确的混合端口读-写冲突行为。

组相联分支 `genSA` 则把「每路一套 tag 存储 + 比较器」用 `for ... generate` 复制 \(A\) 份，再为每个 cache-set 单独例化一个替换策略：

[src/cache/cache_tagunit_par.vhdl:L385-L438](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_tagunit_par.vhdl#L385-L438) — `genWay` 循环为每一路生成独立的 `TagMemory`/`ValidMemory` 与比较器；`TagHits(way)` 汇总各路命中。

[src/cache/cache_tagunit_par.vhdl:L485-L504](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_tagunit_par.vhdl#L485-L504) — `genSet` 循环为每个 cache-set 例化一个 `cache_replacement_policy`，把替换决策**下放到组级别**。这正是「组相联 = 每组一个全相联 + 每组一个替换策略」的硬件体现。

**顺序标签单元** `cache_tagunit_seq` 走的是另一条路。从端口就能看出它把 tag 当作「逐块流入」的数据流：

[src/cache/cache_tagunit_seq.vhdl:L50-L72](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_tagunit_seq.vhdl#L50-L72) — 端口里有 `Request_Tag_rst/rev/nxt` 与 `Request_Tag_Data(CHUNK_BITS)`、`Replace_NewTag_*` 一整套「块流」握手信号，说明 tag 是被切成 `CHUNK_BITS` 位的块、由外部逐拍喂进来的。

[src/cache/cache_tagunit_seq.vhdl:L218-L291](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_tagunit_seq.vhdl#L218-L291) — 请求处理是一个三态状态机 `ST_IDLE → ST_COMPARE → ST_READ`：每拍送入一个 chunk，和所有行的对应 chunk 比，用「部分命中向量」逐拍收敛（`TagHits_nxt <= TagHits_r and PartialTagHits`），直到所有 chunk 比完。

> **重要事实**：`cache_tagunit_seq` 目前**只实现了全相联**（`genFA`），而 `genDM`（[L429-L499](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_tagunit_seq.vhdl#L429-L499)）与 `genSA`（[L503-L574](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_tagunit_seq.vhdl#L503-L574)）的内部实现仍是注释掉的 TODO。并且 `cache_par` 实际例化的是 `_par` 而非 `_seq`。所以本讲的实践与流程图都以并行版为准。

#### 4.2.4 代码实践

**实践目标**：用 `generate` 的分支条件，反推给定参数下会走哪一条实现路径。

**操作步骤**：
1. 打开 [cache_tagunit_par.vhdl 第 161、252、337 行](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_tagunit_par.vhdl#L161) 三条 `generate` 守卫：`genFA`（`CACHE_LINES = ASSOCIATIVITY`）、`genDM`（`ASSOCIATIVITY = 1`）、`genSA`（`ASSOCIATIVITY > 1 and SETS > 1`）。
2. 对下面三组参数，判断走哪条分支、`INDEX_BITS`/`TAG_BITS`/`WAY_BITS` 各是多少：
   - (a) `CACHE_LINES=256, ASSOCIATIVITY=1, ADDRESS_BITS=32`
   - (b) `CACHE_LINES=16, ASSOCIATIVITY=16, ADDRESS_BITS=32`
   - (c) `CACHE_LINES=64, ASSOCIATIVITY=4, ADDRESS_BITS=32`

**预期结果**：
- (a) DM：`INDEX_BITS=log2ceilnz(256)=8`，`TAG_BITS=24`，无 `WAY_BITS`。
- (b) FA：`TAG_BITS=32`（整地址当 tag），`WAY_BITS=log2ceilnz(16)=4`。
- (c) SA：`CACHE_SETS=16`，`INDEX_BITS=4`，`TAG_BITS=28`，`WAY_BITS=2`，且会例化 16 个 `cache_replacement_policy`。

**待本地验证**：用 GHDL elaboration 或一段简单 VHDL 测试台打印上述常量确认。

#### 4.2.5 小练习与答案

**练习 1**：并行标签单元的命中信号 `TagHits` 为什么要把 `ValidMemory(i)` 一起乘进去？
**参考答案**：只有「有效且 tag 匹配」才算命中。复位后所有 valid 位为 0，即使 tag 存储里残留旧值，也不会误报命中。

**练习 2**：组相联结构里，为什么替换策略要为每个 cache-set 各例化一个，而不是全库共用一个？
**参考答案**：LRU 的「最近最少使用」是在**同一组内**比较各路（way）的新旧程度。不同 set 之间互不相干，共用一个 LRU 既无意义也会让逻辑（选择 set 的多路复用）变得复杂且更慢。每个 set 一个策略实例，面积换时序，也最符合组相联的语义。

---

### 4.3 数据通路：CPU 侧接口与存储侧接口

#### 4.3.1 概念说明

`cache_par` 只是一个「裸缓存」——它知道命中/缺失，但**缺失时不会自己去主存搬数据**。要把缓存真正用起来，需要在外面套一层**控制器**，它负责：

- 收 CPU 的请求；
- 命中：直接用缓存数据应答；
- 缺失：去主存读一整行回来，替换进缓存，再应答；
- 写：按写策略处理（PoC 这里用 **write-through, no-write-allocate**，即写时同时更新缓存与主存，且写缺失不把数据搬进缓存）。

PoC 提供两个层次的控制器：

- **`cache_cpu`**：面向 CPU 流水线的缓存控制器。CPU 侧是「改版 PoC.Mem 接口」（带 `cpu_got` 应答），存储侧是标准 PoC.Mem 接口。它只支持 **1 个未完成请求**。
- **`cache_mem`**：在 `cache_cpu` 基础上再加一层，使 CPU 侧也是标准 PoC.Mem 接口（带 `cpu_rdy`/`cpu_rstb`），并可缓冲 **多个未完成请求**（用 `fifo_glue` 或 `fifo_cc_got`）。

这正好把 [u3-l4 FIFO 家族](u3-l4-fifo-family.md) 用上了——`cache_mem` 用 FIFO 来缓冲未完成的 CPU 请求。

> 注意一个易混点：`cache_cpu` 实际例化的是 `cache_par2`（推荐版本，接口多了 `WriteMask` 与 `HIT_MISS_REG`），**不是**本讲精读的 `cache_par`。但二者机制一致，理解了 `cache_par` 就能读懂 `cache_par2`。

#### 4.3.2 核心流程（cache_cpu 控制器）

`cache_cpu` 的核心是一个 4 态 FSM：

```
READY:       有新请求？
               读命中 / 无请求  -> 直接 cpu_got，留在 READY
               写命中（write-through）或读缺失 -> ACCESS_MEM
ACCESS_MEM:  向主存发 mem_req
               主存就绪 mem_rdy：
                 写 -> 完成，cpu_got，回 READY
                 读 -> READING_MEM
READING_MEM: 等 mem_rstb（读数据到位）
               数据到位 -> cache_Replace（搬入新行），cpu_got，回 READY
UNKNOWN:     非法态兜底（输出 'X'）
```

CPU 流水线的停机条件是文档给出的关键公式：

\[
\text{pipeline\_enable} \leftarrow (\text{not } \text{cpu\_req})\ \text{or}\ \text{cpu\_got}
\]

即「没有请求，或请求已被缓存应答」时流水线才前进。

#### 4.3.3 源码精读

先看 `cache_cpu` 如何把内部缓存实例化出来，以及「CPU 数据宽度 < 缓存行宽度」时如何处理：

[src/cache/cache_cpu.vhdl:L209-L214](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_cpu.vhdl#L209-L214) — `RATIO = MEM_DATA_BITS/CPU_DATA_BITS` 是缓存行（= 存储字）宽度与 CPU 字宽度之比；`LOWER_ADDR_BITS = log2ceil(RATIO)` 是用来在一条缓存行里选中某个 CPU 字的低位地址位数。

[src/cache/cache_cpu.vhdl:L243-L264](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_cpu.vhdl#L243-L264) — 例化 `work.cache_par2`（注意 `HIT_MISS_REG => false`），把它与控制器信号 `cache_Request/cache_Hit/...` 连起来。

[src/cache/cache_cpu.vhdl:L268-L306](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_cpu.vhdl#L268-L306) — `gEqual`（`RATIO=1`，CPU 字宽 = 行宽）与 `gWider`（`RATIO>1`，行宽更大）两个分支。`gWider` 用 `for i in 0 to RATIO-1 generate` 把一条缓存行拆成 `RATIO` 个 CPU 字，写时按 `lower_addr` 选中要写的字、读时用一个流水线寄存器 `lower_addr_r` 选中要读的字——这就是「空间局部性」在硬件上的落地：一次搬一整行，CPU 再按小字宽挑。

控制 FSM 是本实体的核心：

[src/cache/cache_cpu.vhdl:L322-L406](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_cpu.vhdl#L322-L406) — 组合进程实现上面的 4 态机。特别注意 `READY` 态的分支条件 `(cache_Hit and cpu_write) or cache_Miss`：**写命中也走 ACCESS_MEM**，这正是 write-through（写直达，同时写主存）的体现；而写缺失不会把数据搬进缓存（no-write-allocate）。`READING_MEM` 态在数据到位时拉高 `cache_Replace`，触发缓存搬入新行。`UNKNOWN` 态把所有控制位置 `'X'`，用于在仿真里捕获非法输入。

`cache_mem` 则把 `cache_cpu` 包了一层，并按 `OUTSTANDING_REQ` 用三种 FIFO 方案缓冲请求：

[src/cache/cache_mem.vhdl:L185-L210](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_mem.vhdl#L185-L210) — 例化内部 `cache_cpu`。

[src/cache/cache_mem.vhdl:L218-L347](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_mem.vhdl#L218-L347) — 三个 `generate` 分支：
- `OUTSTANDING_REQ = 1`：单个寄存器做 1 项缓冲（`g1`）。
- `OUTSTANDING_REQ = 2`：用 [fifo_glue](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_glue.vhdl)（2 项深度的解耦 FIFO，见 [u3-l4](u3-l4-fifo-family.md)）。
- `OUTSTANDING_REQ > 2`：用 [fifo_cc_got](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_cc_got.vhdl)，深度向上取整到合适的 2 的幂。请求被压成一位宽的位串 `din = cpu_wmask & cpu_wdata & cpu_addr & cpu_write` 整体入队。

文件头注释（[cache_mem.vhdl:L60-L76](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_mem.vhdl#L60-L76)）还给出了一条重要的面积/性能权衡：`OUTSTANDING_REQ=2` 是「不损失性能且面积最小」的甜点设置。

#### 4.3.4 代码实践

**实践目标**：追踪一次「读缺失」在控制器里的完整状态转移。

**操作步骤**：
1. 在 [cache_cpu.vhdl 第 339–405 行](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_cpu.vhdl#L339-L405) 的 `case fsm_cs` 里，从 `READY` 态开始，假设 `cpu_req=1, cpu_write=0`（读请求）且 `cache_Miss=1`（缺失）。
2. 逐拍记录 `fsm_cs`、`cache_Request`、`mem_req`、`cache_Replace`、`cpu_got` 的取值，直到回到 `READY`。
3. 对照 [L324–L327](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_cpu.vhdl#L324-L327) 的默认值（每拍先把控制信号复位为 `'0'`/`'-'`）理解「Mealy 型」输出风格。

**需要观察的现象 / 预期结果**：

| 拍 | fsm_cs | 触发条件 | 关键输出 |
| --- | --- | --- | --- |
| T0 | READY | `cpu_req=1, cache_Miss=1` | `cache_Request=1`，`fsm_ns=ACCESS_MEM` |
| T1 | ACCESS_MEM | `mem_rdy=1`（读） | `mem_req=1`，`fsm_ns=READING_MEM` |
| T2 | READING_MEM | `mem_rstb=0` | 等待 |
| T3 | READING_MEM | `mem_rstb=1` | `cache_Replace=1`，`cpu_got=1`，`fsm_ns=READY` |
| T4 | READY | — | 新数据已在缓存，下一次同地址访问会命中 |

**待本地验证**：用 cocotb 测试台 [tb/cache/cache_cpu_tb.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/cache/cache_cpu_tb.vhdl) 实跑确认每拍输出。

#### 4.3.5 小练习与答案

**练习 1**：`cache_cpu` 的写策略是 write-through, no-write-allocate。请从 FSM 里指出是哪条分支体现了「写缺失不搬入缓存」。
**参考答案**：`READY` 态里 `(cache_Hit and cpu_write) or cache_Miss` 控制是否进入 `ACCESS_MEM`。对写请求（`cpu_write=1`）：若**命中**（`cache_Hit=1`）才进 `ACCESS_MEM` 去写主存（write-through）；若写**缺失**（`cache_Miss=1`... 但此时 `cache_Hit=0`），条件为假，不会触发 `cache_Replace`，即不把数据搬进缓存（no-write-allocate）。

**练习 2**：为什么 `cache_mem` 在 `OUTSTANDING_REQ=1` 时「吞吐降为每 2 拍 1 个请求」？
**参考答案**：`g1` 分支用单个寄存器缓冲 1 个请求，且为了给 `cpu_rdy` 一个短的 clock-to-output 延迟，故意不让 `cpu_rdy` 依赖传播延迟大的 `int_got`（见 [L228-L229](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_mem.vhdl#L228-L229) 注释）。这造成「存请求」与「被应答」错拍，吞吐折半。改用 `OUTSTANDING_REQ=2`（fifo_glue）即可恢复每拍 1 个请求。

---

### 4.4 替换策略：cache_replacement_policy 与 LRU

#### 4.4.1 概念说明

缓存满了之后要腾出一行放新数据，腾哪一行由**替换策略**决定。常见策略：

- **RR（round robin，轮转）**：按固定顺序循环替换，最简单。
- **RAND（随机）**：随机选一行。
- **LRU（least recently used，最近最少使用）**：替换「最久没被访问过」的那一行，命中率通常最好。
- **LFU（least frequently used）**：替换「访问次数最少」的那一行。
- **CLOCK**：RR 的近似，用「使用位」实现。

PoC 的 `cache_replacement_policy` 用一个字符串 generic（`"LRU"`/`"RR"`/...）在 elaboration 时选择实现。从文件头注释的「支持策略表」可以看到：**目前只有 LRU 真正实现了**，RR/RAND/CLOCK/LFU 都标注「not yet」。

LRU 的硬件实现并不简单——要动态维护「所有行按访问时间排序」的顺序。PoC 把这件事交给 `src/sort/` 下的 `sort_lru_cache`：一个**只为缓存优化过的 LRU 列表**，只存「键（cache 行索引）」，初始就装好 `0..ELEMENTS-1`，输出端 `KeyOut` 永远给出当前「最久未用」的索引。

#### 4.4.2 核心流程

`cache_replacement_policy` 对外是一个统一的命令接口（见其文件头真值表）：

```
TagAccess=1, Invalidate=0 : 命中访问某行 -> 把该行标记为「最近用过」（Insert）
TagAccess=1, Invalidate=1 : 命中但失效该行 -> 把该行标记为「最久未用」（Free）
Replace=1                  : 替换 -> 用 ReplaceWay 指定的行装入新数据
```

它把这些命令翻译成底层 `sort_lru_cache` 的 `Insert/Free/KeyIn`：

```
LRU_Insert     = (TagAccess and not Invalidate) or Replace
LRU_Invalidate = TagAccess and Invalidate
KeyIn          = Replace ? LRU_Key : HitWay     # 替换时写入待替换行，否则写入命中行
ReplaceWay     = LRU_Key                          # 输出：下次该替换的行
```

#### 4.4.3 源码精读

`cache_replacement_policy` 的实体很简洁——一边是替换接口，一边是「缓存行使用更新」接口：

[src/cache/cache_replacement_policy.vhdl:L85-L104](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_replacement_policy.vhdl#L85-L104) — `CACHE_WAYS` 是相联度（注意：在组相联里，每个 cache-set 的策略实例的 `CACHE_WAYS = ASSOCIATIVITY`）；`HitWay` 是本次命中的路号，`ReplaceWay` 是建议替换的路号，位宽都是 `log2ceilnz(CACHE_WAYS)`。

开头用 `assert` 在 elaboration 期校验策略字符串：

[src/cache/cache_replacement_policy.vhdl:L114-L117](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_replacement_policy.vhdl#L114-L117) — 只接受 `"RR"` 或 `"LRU"`，否则报 `error`。这是 PoC 一贯的「未覆盖就 assert 兜底」风格（见 [u3-l2](u3-l2-vendor-selection-portability.md)）。

`genRR` 分支是「占位」——整个实现都被注释掉了：

[src/cache/cache_replacement_policy.vhdl:L123-L174](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_replacement_policy.vhdl#L123-L174) — 这里能看到 RR 的设计草图（`Pointer_us` 循环 +1、valid 位），但进程体是注释，所以选 `"RR"` 实际上不会产生有效硬件。这与文件头「RR: not yet」一致。

真正干活的是 `genLRU`：

[src/cache/cache_replacement_policy.vhdl:L179-L209](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_replacement_policy.vhdl#L179-L209) — 命令解码得到 `LRU_Insert/LRU_Invalidate`，把 `KeyIn` 在「替换（写待替换行）」与「命中访问（写命中行）」间二选一，输出 `ReplaceWay = LRU_Key`，并例化 `PoC.sort_lru_cache`：

[src/cache/cache_replacement_policy.vhdl:L195-L208](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_replacement_policy.vhdl#L195-L208) — `ELEMENTS => CACHE_WAYS`，把 `Insert/Free/KeyIn/KeyOut` 接上。注意 `Free` 接的是 `LRU_Invalidate`——失效一行就是把它「降到最久未用」。

底层 `sort_lru_cache` 的设计哲学在它的文件头里说得很清楚：

[src/sort/sort_lru_cache.vhdl:L9-L38](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sort/sort_lru_cache.vhdl#L9-L38) — 它是 `sort_lru_list` 的缓存专用优化版，「只存键」、键就是 cache 行索引；列表初始即含 `0..ELEMENTS-1`；`KeyOut` 永远有效，且**第一次**输出的最久未用索引是 `ELEMENTS-1`。

#### 4.4.4 代码实践

**实践目标**：手工模拟一个小 LRU，验证 `sort_lru_cache` 的 `KeyOut` 序列与 `cache_replacement_policy` 的命令解码一致。

**操作步骤**：
1. 设 `CACHE_WAYS=4`（即 `ELEMENTS=4`），初始 LRU 顺序（从最久未用到最近用过）为 `[3, 2, 1, 0]`，所以首次 `KeyOut=3`。
2. 按下面事件序列，逐拍写出 `KeyOut`（= 下一次会替换的路）：
   - 拍 0：上电，无访问。
   - 拍 1：`Insert` key=0（命中路 0）。
   - 拍 2：`Insert` key=2。
   - 拍 3：`Insert` key=0。
   - 拍 4：`Replace`（用 `KeyOut` 指定的路装入新数据）。
3. 把你的手算结果与 `cache_replacement_policy` 在 `Replace=1` 时 `KeyIn <= LRU_Key` 的语义对照（替换时写入的就是将被替换的行号，等价于「新行刚装入，立刻成为最近用过」）。

**预期结果**（一种自洽的演化，**待本地验证**具体实现细节）：

| 拍 | 事件 | LRU 顺序（旧→新） | KeyOut |
| --- | --- | --- | --- |
| 0 | 上电 | 3,2,1,0 | 3 |
| 1 | Insert 0 | 3,2,1,0 → 3,2,1,(0) | 3 |
| 2 | Insert 2 | 3,1,0,(2) | 3 |
| 3 | Insert 0 | 3,1,2,(0) | 3 |
| 4 | Replace | 装入路 3 → 1,2,0,(3) | 1 |

关键观察：连续 Insert「最近用过」的行会被搬到队尾，`KeyOut`（队首）总是「最该被替换」的行；这正是 LRU 命中率高的原因。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `REPLACEMENT_POLICY` 设成 `"LFU"`，综合/仿真会发生什么？
**参考答案**：[L114-L117](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_replacement_policy.vhdl#L114-L117) 的 `assert` 只放过 `"RR"` 与 `"LRU"`，`"LFU"` 会触发 `severity error` 的报告；同时 `genRR` 与 `genLRU` 两个 `generate` 都不展开，实体内没有任何逻辑，综合不出有效硬件。

**练习 2**：在组相联缓存里，`cache_replacement_policy` 的 `HitWay` 输入从哪里来？
**参考答案**：来自标签单元的命中路号。在 [cache_tagunit_par.vhdl 的 genSA](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_tagunit_par.vhdl#L440) 里，`HitWay = onehot2bin(TagHits, 0)`，它被接到每个 set 的策略实例的 `HitWay` 端口（[L502](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_tagunit_par.vhdl#L502)），让 LRU 知道「本次命中的是哪一路」。

## 5. 综合实践

**任务**：把本讲四个模块串起来，画出 `cache_par` 一次「读命中」的完整数据通路，并把它放到整个实例化树里理解。

**要求**：

1. **画出真实的实例化树**（取代任务描述里那条不符合源码的 `cpu→tagunit→mem` 链）。请基于源码确认并绘制：

   ```
   cache_mem ─┬─(g1/g2/gt2: fifo_glue / fifo_cc_got / 寄存器)─ 缓冲 CPU 请求
              └─ cache_cpu ─┬─ FSM(READY/ACCESS_MEM/READING_MEM)
                            └─ cache_par2 ─┬─ cache_tagunit_par ─┬─ genFA/genDM/genSA
                                           │                     └─ cache_replacement_policy ─ sort_lru_cache
                                           └─ 数据存储（单端口 read-first）
   ```

   并在图中标注 `cache_par`（本讲精读版）与 `cache_par2`（控制器实际使用版）的关系。

2. **追踪一次读命中**（在 `cache_par` 层面）。设 `Request=1, ReadWrite=0, Address=A`，且 `A` 命中。请按下面的检查表逐项在源码里找到对应行：
   - 命中如何被算出：[cache_tagunit_par.vhdl L185-L194](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_tagunit_par.vhdl#L185-L194)（比较器 + `onehot2bin`）与 [L216-L217](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_tagunit_par.vhdl#L216-L217)（`TagHit`）。
   - 命中行号如何回传给数据存储：[cache_par.vhdl L160-L163](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_par.vhdl#L160-L163)（`LineIndex => TU_LineIndex`）。
   - 行号二选一：[cache_par.vhdl L166-L167](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_par.vhdl#L166-L167)（`Request='1'` 选 `TU_LineIndex`）。
   - 数据读出与寄存：[cache_par.vhdl L178](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_par.vhdl#L178)（`CacheLineOut <= CacheMemory(...)`）。
   - 命中信号寄存：[cache_par.vhdl L181-L187](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_par.vhdl#L181-L187)（`CacheHit <= TU_TagHit`）。

3. **时序图**：画出 `Clock`、`Request`、`Address`、内部 `TU_TagHit`、`TU_LineIndex`、`CacheHit`、`CacheLineOut` 的波形，标注「命中比较是当拍组合结果，而 `CacheHit`/`CacheLineOut` 下一拍才有效」这一 1 拍延迟。

4. **扩展思考**：如果这次是「读缺失」，`cache_par` 自己不会去取数据。请说明这个「去主存搬数据」的动作由谁、在哪个 FSM 态完成（答案在 4.3 节 `cache_cpu` 的 `READING_MEM` 态：拉高 `cache_Replace`）。

**预期结果**：得到一张实例化树图 + 一张读命中时序图 + 一段说明，能清楚区分「标签单元负责命中判定（当拍组合）」「数据存储负责读出（1 拍延迟）」「控制器负责缺失处理（多拍 FSM）」三者职责。

## 6. 本讲小结

- PoC 的 `cache` 命名空间分三层职责：**标签单元**（命中判定）、**替换策略**（满了腾谁）、**缓存主体与控制器**（组装 + 对接 CPU/存储）。真实实例化树是 `cache_mem → cache_cpu → cache_par2 → cache_tagunit_par + cache_replacement_policy → sort_lru_cache`。
- `cache_par` 把并行标签单元与一个「单端口、读优先」数据存储并排组合；所有命令 1 拍完成，但 `CacheHit/CacheMiss/CacheLineOut` 等**输出统一延迟 1 拍**；这种 read-first 存储不能映射到 `ocram_sdp`，故综合效率低，README 推荐 `cache_par2`。
- 标签单元用 `generate` 在一份实体里按 `CACHE_LINES`/`ASSOCIATIVITY` 自动展开 FA/DM/SA 三种结构；并行版 `cache_tagunit_par` 当拍组合出命中结果，顺序版 `cache_tagunit_seq` 逐 chunk 串行比较（目前仅 FA 实现，且未被 `cache_par` 使用）。
- `cache_cpu` 控制器用一个 4 态 FSM（`READY/ACCESS_MEM/READING_MEM/UNKNOWN`）把裸缓存套上「缺失自动搬数据」能力，写策略为 write-through、no-write-allocate；`cache_mem` 在外层用 `fifo_glue`/`fifo_cc_got` 支持多个未完成请求。
- 替换策略 `cache_replacement_policy` 用字符串 generic 在 elaboration 期选实现，**目前只有 LRU 真正实现**（委托给 `sort_lru_cache`），RR 为注释草图、其余未实现；组相联里每个 cache-set 各一个策略实例。

## 7. 下一步学习建议

- **横向对比**：阅读 [src/cache/cache_par2.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/cache/cache_par2.vhdl)，找出它与 `cache_par` 的接口差异（`WriteMask`、`HIT_MISS_REG`），理解为什么控制器实际使用的是它。
- **深入 LRU 硬件**：精读 [src/sort/sort_lru_cache.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sort/sort_lru_cache.vhdl) 与 `sort_lru_list.vhdl`，弄清「只存键」的 LRU 列表如何用移位/交换维护顺序。
- **接上总线**：本讲的 `cache_mem` 暴露的是 PoC.Mem 接口。建议接着学 [u5-l4 总线与流式协议](u5-l4-bus-stream-protocols.md)，看缓存如何挂到 Wishbone/AXI-Stream 风格的总线上。
- **测试台视角**：阅读 [tb/cache/cache_par_cocotb.py](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/cache/cache_par_cocotb.py) 与 `tb/common/lru_dict.py`，学习如何用一个软件 LRU 字典作为参考模型给硬件缓存做记分板验证——这会把本讲的 4.4 节 LRU 演化真正跑起来。
