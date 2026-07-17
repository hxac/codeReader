# L1 数据缓存

## 1. 本讲目标

本讲聚焦 Nyuzi 单核流水线中「数据怎么从内存进入寄存器」的第一道关口——L1 数据缓存（L1 Data Cache，简称 L1D）。学完本讲，你应该能够：

- 说清楚什么是「虚拟索引 / 物理标签（VI/PT）」缓存组织，以及它为什么能天然避免别名（alias）；
- 区分 L1D 的两拍流水线——标签级 `dcache_tag_stage` 与数据级 `dcache_data_stage`——各自负责什么；
- 理解伪 LRU 替换算法、store 队列的写合并与旁路（load-after-store forwarding）机制；
- 看懂多核场景下 L2 回填时对 L1D 的 snoop（监听）一致性处理。

本讲承接 [u3-l2 单核流水线总览] 中「操作数 fetch 之后分叉出访存路径 `dcache_tag → dcache_data`」这一结论，也用到 [u5-l1 操作数 fetch] 里「操作数如何读出并送往后级」的认知。本讲只讲 L1D 本身；缺失之后如何与 L2 交互、如何挂起线程，留给 [u6-l2 L1-L2 接口]。

## 2. 前置知识

在进入源码前，先用通俗语言建立几个概念。

**缓存（Cache）是什么。** CPU 比内存快几百倍，若每次取数都去内存，CPU 大部分时间都在干等。缓存是一块很小但极快的存储，把最近用过的数据留一份副本。程序访问有「局部性」：刚用过的数据很快会再用（时间局部性），用了某个地址附近的数据往往接着用相邻地址（空间局部性），所以小小一块缓存就能命中绝大多数访问。

**缓存行（Cache Line）。** 缓存不是按字节存的，而是按「行」存。Nyuzi 的一行是 64 字节，正好等于一个向量寄存器的宽度（16 通道 × 4 字节）。一次读内存会一次性搬回整行 64 字节，顺带把空间局部性利用起来。

**组相联（Set Associative）。** 一个地址该放在缓存的哪个位置？最灵活的是「任意位置都能放」，但这要全表比较，硬件代价大；最省的是「每个地址只能放一个固定位置」，但这容易冲突。折中是「组相联」：把缓存分成若干「组（set）」，每个地址先由地址算出归哪个组，组内有若干「路（way）」，组内任何一路都可以放。Nyuzi L1D 默认是 64 组、每组 4 路（4-way）。

**虚拟地址与物理地址。** 程序看到的是「虚拟地址」，而内存条上真正的编号是「物理地址」。两者由页表（MMU）翻译。翻译是按「页」进行的：一页 4KiB，页内的偏移量在翻译前后不变，只有页号变了。本讲会反复用到这一点——它是 Nyuzi L1D 避免别名的关键。

**TLB（Translation Lookaside Buffer）。** 每次访存都要翻译地址，而查页表很慢。TLB 是一张缓存近期翻译结果的小表，把「虚拟页号 → 物理页号」记下来。Nyuzi 的 TLB 是「软件管理」的：硬件只负责查表，查不到（TLB miss）就抛一个 trap 让软件把表项填进来，再重试。详见 [u7-l1]。

**别名（Alias）问题。** 如果缓存用虚拟地址来决定「放在哪一组」，但用物理地址来「比对标签」，那么同一个物理地址（被两个不同虚拟地址映射，即同义词 synonym）有可能被放进两组，造成两份不一致的副本——这就是别名。下面会看到 Nyuzi 如何用一个巧妙的设计规避它。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [hardware/core/dcache_tag_stage.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv) | L1D 第一拍：算地址、查 DTLB、并行读各路标签与 valid 位，并响应 snoop 读。 |
| [hardware/core/dcache_data_stage.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv) | L1D 第二拍：判命中、读数据、检测各类 fault、产生缺失/回滚/LRU 更新/性能事件。 |
| [hardware/core/cache_lru.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv) | 伪 LRU 替换算法，决定缺失时替换哪一路。 |
| [hardware/core/l1_store_queue.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv) | 每线程一个 store 槽，负责写合并、旁路与 sync。 |
| [hardware/core/l1_l2_interface.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv) | L1 与 L2 之间的接口，发起 snoop 并据结果回填/作废 L1D。 |
| [hardware/core/defines.svh](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh) | 缓存相关常量与类型（`l1d_addr_t`、`l1d_tag_t` 等）。 |
| [hardware/core/config.svh](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh) | 可配置参数（`L1D_WAYS`、`L1D_SETS`）及其约束说明。 |

## 4. 核心概念与源码讲解

### 4.1 VI/PT 组织：虚拟索引、物理标签

#### 4.1.1 概念说明

L1D 面对一个两难：

- 地址翻译（查 TLB）需要时间，如果等翻译完再用**物理地址**去查缓存，命中判定会晚一拍，拖慢流水线；
- 如果不等翻译、直接用**虚拟地址**去查缓存，可以更快，但同一个物理地址被多个虚拟地址映射时会出现「别名」——同一份数据进到两组，写一处另一处不变，造成不一致。

Nyuzi 采用业界经典折中：**虚拟索引 / 物理标签（Virtually Indexed, Physically Tagged，VI/PT）**。

- **索引（index，决定查哪一组）**用虚拟地址算——这一步不需要等翻译，可以和 TLB 查询**并行**进行，省时间；
- **标签（tag，决定这一路是不是我要的数据）**用物理地址——这一步等 TLB 翻译出物理页号后再比对，保证一致性。

这个组合能成立的**前提**是：用虚拟地址算出的「组号」必须和用物理地址算出的「组号」永远相同。下面用源码和位运算说明 Nyuzi 如何保证这一点。

#### 4.1.2 核心流程

先看地址是怎么被切成三段的。Nyuzi 把一个 32 位访存地址定义成结构体 `l1d_addr_t`：

```text
l1d_addr_t = { tag, set_idx, offset }
```

其中各字段位宽由 `defines.svh` 派生：

| 字段 | 含义 | 位宽（默认配置） |
|------|------|------------------|
| `offset` | 行内字节偏移 | `CACHE_LINE_OFFSET_WIDTH` = 6 位（64 字节一行） |
| `set_idx` | 组号 | `$clog2(L1D_SETS)` = 6 位（64 组） |
| `tag` | 物理标签 | `DCACHE_TAG_BITS` = 32 − 6 − 6 = 20 位 |

合计 6 + 6 + 20 = 32 位，正好铺满一个 32 位地址。

关键观察：`set_idx`（6 位）+ `offset`（6 位）= **12 位**，而一页大小 `PAGE_SIZE = 'h1000` = 4096 = \(2^{12}\)，页内偏移正好也是 12 位。也就是说：

\[ \text{组号所在位段} \subseteq \text{页内偏移} \]

地址翻译只改「页号」、不改「页内偏移」。所以对于落在同一物理页的所有虚拟地址，它们低 12 位完全相同 → 算出的 `set_idx` 也完全相同 → 同一个物理地址永远只会落到**同一个组**里，不可能分散到两组形成别名。这就是 VI/PT 避免别名的根本原理。

#### 4.1.3 源码精读

地址结构体定义在 [defines.svh:320-324](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L320-L324)，缓存常量定义在 [defines.svh:293-301](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L293-L301)：

```systemverilog
parameter PAGE_SIZE = 'h1000;                                  // 4KiB
parameter CACHE_LINE_BYTES = NUM_VECTOR_LANES * 4;             // 64 字节，等于向量宽度
parameter CACHE_LINE_OFFSET_WIDTH = $clog2(CACHE_LINE_BYTES);  // 6
parameter DCACHE_TAG_BITS = 32 - (CACHE_LINE_OFFSET_WIDTH + $clog2(`L1D_SETS)); // 20
```

这组常量体现了上一节说的「行偏移 6 位 + 组索引 6 位 = 12 位 = 页偏移」。文件顶部 `dcache_tag_stage.sv` 的注释把设计意图写得很清楚，[dcache_tag_stage.sv:30-37](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L30-L37)：

> The cache is virtually indexed and physically tagged... To avoid aliasing, the size of a way must be the same size or smaller than a virtual page (cache line size × num sets <= page_size).

这段注释翻译过来正是：**「一行的容量 × 组数 ≤ 页大小」**，等价于「组索引位 ≤ 页偏移位」。

为了从机制上守住这条约束，`dcache_tag_stage` 在仿真启动时放了一条断言，[dcache_tag_stage.sv:162-167](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L162-L167)：

```systemverilog
initial
begin
    // Cannot use more than 64 dcache sets
    assert(`L1D_SETS <= 64);
    assert((`L1D_SETS & (`L1D_SETS - 1)) == 0);   // 必须是 2 的幂
end
```

第二条断言要求组数是 2 的幂（这样 `set_idx` 正好是地址的连续位段）；第一条断言把组数上限钉死在 64。同样的约束也写在配置文件 [config.svh:33-37](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh#L33-L37) 的注释里，默认值 `L1D_SETS 64`（[config.svh:43](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh#L43)）正好踩在允许的上限。

> 提示：这条断言只在仿真（`SIMULATION` 构建）里生效。综合到真实 FPGA/ASIC 时它不会报错，所以改配置时务必自己核算——这正是本讲代码实践要练的。

#### 4.1.4 代码实践

**实践目标**：亲手算一遍「为什么组数不能超过 64」，把抽象约束变成具体数字。

**操作步骤**：

1. 打开 [defines.svh:293-301](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L293-L301)，确认默认值：`NUM_VECTOR_LANES = 16`，`L1D_SETS = 64`，`PAGE_SIZE = 'h1000`。
2. 推导：行内偏移位 \(= \log_2(16 \times 4) = 6\)；组索引位 \(= \log_2(64) = 6\)；两者之和 \(= 12\)；页偏移位 \(= \log_2(4096) = 12\)。两者相等，正好踩线。
3. 假想把 `L1D_SETS` 改成 128：组索引位变成 7，\(6 + 7 = 13 > 12\)，组索引会越界伸进「页号」区域。此时两个映射到同一物理页、但虚拟页号不同的虚拟地址，可能算出**不同**的组号 → 别名。
4. 打开 [dcache_tag_stage.sv:165](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L165)，确认 `assert(\`L1D_SETS <= 64)` 会拦截这种非法配置。

**需要观察的现象 / 预期结果**：

- 默认配置下，组索引 + 行偏移 = 12 位 = 页偏移，VI/PT 安全；
- 组数翻倍到 128 会破坏「组索引 ⊆ 页偏移」的不变量；
- 改完 `config.svh` 后若直接仿真构建，应在断言处看到失败信息（**待本地验证**：取决于仿真器是否默认开 `assert`）。

> 注意：请勿真的提交对源码的修改；本实践只需在脑中或草稿上完成推导。若要在本地验证断言，请在临时副本里改并运行 `make`，验证完毕还原。

#### 4.1.5 小练习与答案

**练习 1**：若把 `CACHE_LINE_BYTES`（行大小）改成 128 字节，在「不违反 VI/PT 约束」的前提下，`L1D_SETS` 最多能取多少？

**参考答案**：行偏移位变成 \( \log_2 128 = 7 \)，于是组索引位最多 \( 12 - 7 = 5 \)，`L1D_SETS` 最多 \( 2^5 = 32 \)。

**练习 2**：为什么 Nyuzi 强调 `CACHE_LINE_BYTES` 必须「等于向量宽度」？

**参考答案**：因为一条向量块访存指令（`MEM_BLOCK`）要一次性搬整个向量寄存器进出内存。让一行恰好等于一个向量，向量访存就对齐到缓存行，既能一次读完一行、又不必跨行拼装，是最快的连续搬运方式（见 [u2-l3]）。

---

### 4.2 标签级与数据级：两拍流水线的职责划分

#### 4.2.1 概念说明

L1D 在 Nyuzi 流水线里占两拍：`dcache_tag_stage`（标签级）和 `dcache_data_stage`（数据级）。为什么拆成两拍？因为「查标签」需要先访问一块 SRAM 读出标签和 valid 位，这一读本身有一拍延迟，必须等结果出来才能判定命中。于是设计成：

- **标签级**：用虚拟地址算出请求地址、查 DTLB 拿物理页号、并行读各路的标签与 valid 位，把这些结果锁存一拍；
- **数据级**：拿到上拍锁存的标签，比对出命中/缺失，命中则读数据 SRAM，同时检测对齐/权限等各类 fault，并产生缺失、回滚、LRU 更新等控制信号。

这种「先读元数据，再据元数据做决定」的两段式，是组相联缓存的常见结构。

#### 4.2.2 核心流程

一条 `load` 指令在 L1D 两拍的旅程：

```text
操作数 fetch 送来 of_operand1(基址)、immediate(偏移)、of_subcycle 等
        │
        ▼  dcache_tag_stage（第 1 拍）
  1. 算虚拟地址：addr = operand1[~subcycle] + immediate
     （scatter/gather 时用 subcycle 选 lane 的指针）
  2. 同时做两件并行的事：
       a. 查 DTLB：虚拟页号 → 物理页号 + 权限位（present/writable/supervisor）
       b. 并行读所有 way 的 tag 与 valid（用虚拟地址的 set_idx 索引）
  3. 若 MMU 关闭：TLB 查询旁路为恒等映射（物理=虚拟）
  4. 锁存：把虚拟地址、物理地址、各路 tag/valid、TLB 结果送下一拍
        │
        ▼  dcache_data_stage（第 2 拍）
  5. 命中判定：对每一路比较「物理 tag 相等 且 valid」→ way_hit_oh
     cache_hit = 有任一路命中 且（非 sync 或本线程已登记 sync）且 TLB 命中
  6. 命中 → 用 {命中路号, set_idx} 读数据 SRAM，送出 dd_load_data
  7. 缺失 → 发 dd_cache_miss，回滚取指（让线程重取这条 load），挂起线程
  8. 各类 fault 检查（对齐/权限/页缺失…），有 fault 则抛 trap 而非访存
  9. 命中且非 fault → 更新 LRU（把命中路挪到最近使用）
```

#### 4.2.3 源码精读

**标签级：算地址 + 并行读标签。** 地址计算在 [dcache_tag_stage.sv:134-135](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L134-L135)：

```systemverilog
assign scgath_lane = ~of_subcycle;
assign request_addr_nxt = of_operand1[scgath_lane] + of_instruction.immediate_value;
```

`~of_subcycle` 是「按位取反」：scatter/gather 指令重发 16 次（见 [u2-l3]），`subcycle` 每次递增，取反后用来挑出当前要访存的那个 lane 的指针（地址存在向量寄存器的对应通道里）。普通标量访存时 `subcycle=0`，取 `operand1[15]`——但标量值会被广播到所有通道（见 [u5-l1]），所以取哪条通道都一样。

`cache_load_en` 标记「这是一次真正的 load 访存」，[dcache_tag_stage.sv:130-133](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L130-L133)：排除了控制寄存器访问（`MEM_CONTROL_REG`，不走缓存）和非 load 的情形。

各路标签的并行读用一个 `generate` 循环为每路实例化一块 `sram_2r1w`，[dcache_tag_stage.sv:172-231](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L172-L231)。这块 SRAM 有两个读端口、一个写端口，正好对应三类访问：

```systemverilog
sram_2r1w #(.DATA_WIDTH($bits(l1d_tag_t)), .SIZE(`L1D_SETS), ...) sram_tags(
    .read1_en(cache_load_en),        .read1_addr(request_addr_nxt.set_idx), // 流水线用：虚拟索引
    .read2_en(l2i_snoop_en),         .read2_addr(l2i_snoop_set),            // snoop 用
    .write_en(l2i_dtag_update_en_oh[way_idx]),                             // L2 回填用
    .*);
```

注意 `read1_addr` 用的是**虚拟地址**的 `set_idx`（虚拟索引），而读出的标签稍后会和**物理**标签比对（物理标签）。这正是 VI/PT 的体现。`valid` 位没有放进 SRAM，而是用触发器 `line_valid` 单独存，因为复位时必须能一次性全部清零，[dcache_tag_stage.sv:178](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L178)。

> 一个精巧的旁路（bypass）：如果当前正在读某组、而同一拍 L2 恰好在写同一组（回填），代码直接采用写入的新值而不是过时的旧值，[dcache_tag_stage.sv:213-219](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L213-L219)，避免读到陈旧标签。

**DTLB 查询。** 标签级实例化了 DTLB，[dcache_tag_stage.sv:233-253](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L233-L253)，输入虚拟页号与当前 ASID，输出物理页号及权限位。当 MMU 关闭时（裸机程序，见 [u1-l4]），一组组合逻辑把 TLB 结果旁路成恒等映射，[dcache_tag_stage.sv:258-276](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L258-L276)：`dt_tlb_hit = 1`、`ppage_idx = 虚拟页号`。这就是为什么裸机 hello_world 不用填 TLB 也能跑。

**物理地址拼装。** 标签级最后把「物理页号 + 页内偏移」拼成物理地址交给下一拍，[dcache_tag_stage.sv:312-313](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L312-L313)：

```systemverilog
assign dt_request_paddr = {ppage_idx, fetched_addr[31 - PAGE_NUM_BITS:0]}; // 物理页号 + 低12位
assign dt_request_vaddr = fetched_addr;                                    // 虚拟地址（原样保留）
```

注意物理地址只换了高 20 位（页号），低 12 位（页内偏移）直接来自虚拟地址——再次印证「翻译不改页内偏移」。

**数据级：命中判定。** 数据级对每一路比较物理标签与 valid，[dcache_data_stage.sv:350-357](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L350-L357)：

```systemverilog
for (way_idx = 0; way_idx < `L1D_WAYS; way_idx++)
    assign way_hit_oh[way_idx] = dt_request_paddr.tag == dt_tag[way_idx]  // 物理标签比对
                                 && dt_valid[way_idx];
```

最终命中综合了「有任一路命中 + TLB 命中 + sync 特殊处理」，[dcache_data_stage.sv:361-363](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L361-L363)。sync load 第一次会被当作缺失（要去 L2 登记 LL/SC 监视，见 [u10-l1]），用 `dd_load_sync_pending` 跟踪是第一次还是重试。

**数据级：读数据 SRAM。** 命中的 load 才会读数据，地址是「命中路号 + 组号」，[dcache_data_stage.sv:475-489](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L475-L489)：

```systemverilog
sram_1r1w #(.DATA_WIDTH(CACHE_LINE_BITS), .SIZE(`L1D_WAYS * `L1D_SETS), ...) l1d_data(
    .read_en(cache_hit && cached_load_req),
    .read_addr({way_hit_idx, dt_request_paddr.set_idx}),
    .read_data(dd_load_data),
    .write_en(l2i_ddata_update_en),  // L2 回填写数据
    .*);
```

`L1D_WAYS * L1D_SETS` = 4 × 64 = 256 行，每行 64 字节，共 16 KiB——和默认容量一致。

**数据级：缺失与 near-miss。** 普通缺失发 `dd_cache_miss`，[dcache_data_stage.sv:505-509](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L505-L509)。但有一种「near miss」特例：数据其实不在缓存，但 L2 **这一拍正好在回填同一行**，[dcache_data_stage.sv:496-503](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L496-L503)。此时若挂起线程等缺失唤醒，会错过这个回填的唤醒；于是改用「回滚让线程重试」，不挂起，下一拍数据就填好了。

**数据级：fault 检查。** 数据级集中检查所有访存异常，[dcache_data_stage.sv:258-285](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L258-L285)，包括对齐（unaligned）、特权操作（supervisor 才能写控制寄存器/TLB）、页缺失（page not present）、supervisor 页越权、只读页写入。任一 fault 置 `any_fault`，后续所有副作用（读数据、store、更新 LRU）都被抑制，并在 [dcache_data_stage.sv:545-570](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L545-L570) 翻译成对应 trap 类型（`TT_TLB_MISS`/`TT_PAGE_FAULT`/`TT_UNALIGNED_ACCESS` 等）送写回级。注意顺序：先判 TLB miss，因为 TLB miss 时权限位无效。

**数据级：IO 访问分流。** 物理地址落在 `0xffff????` 区段的是外设寄存器（MMIO），不走缓存而走 IO 总线，[dcache_data_stage.sv:192](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L192) 与 [dcache_data_stage.sv:203-214](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L203-L214)。`printf` 输出文本就经这条路写到 UART 地址（见 [u1-l4]）。

#### 4.2.4 代码实践

**实践目标**：用模拟器跟踪输出对照两拍流水线的信号含义。

**操作步骤**：

1. 按 [u1-l4] 构建 `software/apps/hello_world`，得到可运行镜像。
2. 用 `-v`（verbose）模式运行模拟器（**待本地验证**：具体开关名以 `tools/emulator` 当前实现为准）。
3. 跟踪输出里某条 `load_32` 指令的一行：它会打印 PC、线程号、寄存器写回值。
4. 回到源码，在 [dcache_tag_stage.sv:135](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L135) 处心算这条 load 的请求地址 = 基址寄存器值 + 立即数偏移。
5. 对照 [dcache_data_stage.sv:354](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L354)：模拟器打印的「寄存器写回值」对应 `dd_load_data` 里命中的那一行的相应字。

**需要观察的现象 / 预期结果**：

- 命中的 load，模拟器只打印一次写回；
- 若某条 load 命中缺失，跟踪里会看到 PC 回退重取（模拟器层面表现为同一 PC 出现多次）。
- 这是「源码阅读型实践」，重在把 `-v` 输出与源码信号对应起来，不必强求运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `valid` 位用触发器存、而标签用 SRAM 存？

**参考答案**：复位时必须能在一个周期内把所有 valid 位同时清零（缓存初始全空），触发器支持异步/同步复位且可并行清零；SRAM 不支持这种整体复位。标签初始值无所谓（valid=0 时标签不会被比对），故可放 SRAM 省面积。

**练习 2**：`cache_hit` 的判定里为什么必须包含 `dt_tlb_hit`？

**参考答案**：标签是从 SRAM 读出的物理标签，只有 TLB 命中、物理地址有效时，这次比对才有意义。TLB miss 时物理标签无效，贸然判命中会读到错误数据，所以必须同时要求 TLB 命中；TLB miss 则走 trap 让软件填表项后重试。

---

### 4.3 LRU 与 store：替换策略与写缓冲

#### 4.3.1 概念说明

**LRU（Least Recently Used，最近最少使用）。** 一个组有 4 路，缺失时要填入新行，该把哪一路踢掉？直觉是「踢掉最久没用的那一路」——这就是 LRU。但严格的 LRU 要维护完整的访问顺序，硬件代价大。Nyuzi 用「伪 LRU（pseudo-LRU）」：用一棵小树记录「偏向」方向，近似 LRU 但实现简单。

**Store 队列。** 写操作（store）不直接写进 L1D 数据 SRAM，而是先进一个 store 队列，攒着再批量发给 L2。这样做有几个好处：

1. **写合并（write combining）**：连续多次写同一行可以合并成一次 L2 写请求，省带宽；
2. **写旁路（store bypass / forwarding）**：刚写完的数据紧接着读，可以从队列里直接返回，不必等回写完成；
3. **配合 sync 原语**：LL/SC 等同步操作需要等 L2 响应才能确认成功，store 队列提供了这个等待点。

#### 4.3.2 核心流程

**伪 LRU 的树结构**（以 4 路为例，3 个标志位 a/b/c 构成一棵树）：

```text
        b
      /   \
     a     c
    / \   / \
   0   1 2   3      ← 叶子代表 4 路
```

- 每个内部节点存 1 位，指向「最近较少使用的那一侧」；
- 找替换路：从根 b 开始，按标志位一路向下走到叶子，就是要踢掉的路；
- 访问某路后更新：把从根到该叶路径上的标志位都改成「指向另一侧」，表示「这一侧刚用过，另一侧更旧」。

这种近似 LRU 的好处是：一个节点从「最近使用」滑到「最久未用」至少要 2 个周期，不会立刻被踢，足够避免抖动。

**LRU 两个接口**：

- **Fill 接口**：缓存要填新行时拉 `fill_en`+`fill_set`，一拍后 `cache_lru` 给出 `fill_way`（要替换的路），并顺手把它挪到「最近使用」。
- **Access 接口**：访问某组时拉 `access_en`+`access_set`；若命中，一拍后拉 `update_en`+`update_way`，把命中路挪到「最近使用」。

**Store 队列写合并**：每线程一个 store 槽。新 store 到来时，若该线程槽里已有一笔**同地址**且尚未发出的 store，则把新数据合并进去（更新 mask 与 data）；否则占用空槽或回滚等待。

#### 4.3.3 源码精读

**伪 LRU 树的解释与实现。** [cache_lru.sv:97-113](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv#L97-L113) 的注释画了上面那棵树并解释原理。4 路 case 下，根据 3 位标志算出替换路的逻辑在 [cache_lru.sv:148-159](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv#L148-L159)：

```systemverilog
casez (lru_flags)
    3'b00?: fill_way = 0;
    3'b10?: fill_way = 1;
    3'b?10: fill_way = 2;
    3'b?11: fill_way = 3;
endcase
```

`?` 是无关位，体现「树」的层级选择。每组的标志位存在一块 SRAM `lru_data` 里，[cache_lru.sv:115-129](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv#L115-L129)。注意路数被硬编码为 1/2/4/8 四种（[cache_lru.sv:68-73](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv#L68-L73) 与断言 [cache_lru.sv:94](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv#L94)），这就是 [u3-l3] 提到「缓存路数必须 ∈ {1,2,4,8}」的根源。

**Fill 优先于 access。** 关键设计：当 `fill_en` 与 `access_en` 同一拍都拉起时，fill 优先，[cache_lru.sv:86-89](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv#L86-L89)：

```systemverilog
assign read_en = access_en || fill_en;
assign read_set = fill_en ? fill_set : access_set;   // fill 优先选 set
assign new_mru = was_fill ? fill_way : update_way;   // fill 优先更新
```

注释 [cache_lru.sv:40-46](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv#L40-L46) 说明原因：避免连续回填时互相踢出刚填的行（低效），更重要的是**避免两个线程互相驱逐对方刚填的行陷入活锁（livelock）**。

**LRU 与 dcache 的接线。** 标签级实例化 `cache_lru`，[dcache_tag_stage.sv:278-289](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L278-L289)：fill 来自 L2 接口（`l2i_dcache_lru_fill_*`），access 来自本级的有效访存，update_way 来自数据级（命中后回传 `dd_update_lru_way`）。数据级在命中且非 fault 时才更新 LRU，[dcache_data_stage.sv:514-515](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L514-L515)。

**Store 队列的写合并。** 每线程一个 `pending_stores` 槽，能否合并由 `can_write_combine` 判定，[l1_store_queue.sv:122-132](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv#L122-L132)：

```systemverilog
assign can_write_combine = pending_stores[thread_idx].valid
    && pending_stores[thread_idx].address == dd_store_addr   // 同一缓存行
    && !pending_stores[thread_idx].flush
    && !pending_stores[thread_idx].request_sent              // 还没发给 L2
    && !dd_store_sync ...;
```

合并条件核心是「同一地址（同一行）且尚未发出」。当 `update_store_entry && can_write_combine` 时，新 store 的字节掩码与数据并进已有条目（见 [l1_store_queue.sv:218](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv#L218) 附近的合并逻辑），把多笔小写拼成一笔整行写。

**Store 旁路（forwarding）。** 紧跟 store 的 load 若命中同一未发出的 store 条目，直接从队列返回数据，不必等回写，[l1_store_queue.sv:321-334](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv#L321-L334)：

```systemverilog
sq_store_bypass_data <= pending_stores[dd_store_bypass_thread_idx].data;
if (dd_store_bypass_addr == pending_stores[...].address && pending_stores[...].valid ...)
    sq_store_bypass_mask <= pending_stores[...].mask;   // 命中：返回未提交的写
else
    sq_store_bypass_mask <= 0;                          // 未命中：正常走缓存
```

`dd_store_bypass_*` 信号由数据级产生，[dcache_data_stage.sv:297-298](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L297-L298)。注意旁路用的是**物理**地址（`dt_request_paddr`），因为 store 队列以物理行地址为键。

#### 4.3.4 代码实践

**实践目标**：用伪 LRU 树手算一个替换序列，理解「近似 LRU」的行为。

**操作步骤**：

1. 假设某组初始标志 `lru_flags = 3'b000`，对应树 `{b=0, a=0, c=0}`。
2. 按 [cache_lru.sv:152-155](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv#L152-L155) 的 casez，此时 `fill_way = 0`（首次填到路 0）。
3. 假设随后依次访问路 0、路 1、路 2（每次用 [cache_lru.sv:163-168](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv#L163-L168) 的 update_flags 手算每步后标志值）。
4. 现在要填第 4 行（路 0/1/2/3 已分别被用过），问：`fill_way` 会指向哪一路？它是不是严格 LRU 意义下「最久未用」的那一路？

**需要观察的现象 / 预期结果**：

- 手算后会发现伪 LRU 选出的替换路与严格 LRU **偶尔不同**，但都保证「最近用过的路不会被立刻踢」。
- 这正是注释 [cache_lru.sv:111-113](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv#L111-L113) 所说「足够接近 LRU 以表现良好，但实现简单得多」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 store 不直接写 L1D 数据 SRAM，而要绕道 store 队列？

**参考答案**：Nyuzi 的 L1D 是「写不直达（write-back）」式，写只在本地缓冲，最终整行回写 L2。store 队列把多次小写合并成整行写、提供 load 旁路、并为 sync 原语提供等待点；若直接写 SRAM，既无法合并也无法表达「这笔写是否已被 L2 确认」。

**练习 2**：`fill_en` 与 `access_en` 同一拍都有效时为何 fill 优先？

**参考答案**：若 access 优先，回填时可能选中刚被 access「标记为最近使用」之外的路，连续回填会把彼此刚填的行踢掉，既浪费带宽，多线程下还可能两个线程互相驱逐陷入活锁。fill 优先保证「刚填入的路立刻成为最近使用，不会被马上踢」。

---

### 4.4 snoop 一致性：多核下的缓存监听

#### 4.4.1 概念说明

当系统有多个核（`NUM_CORES > 1`）共享 L2 时，会出现一致性问题：核 A 把某行放进自己的 L1D 并改写了（store 队列里），核 B 也缓存了同一物理行——B 手里的是旧数据。如何保证 B 不会读到陈旧值？

Nyuzi 采用「写使无效 + snoop（监听）」思路：

- L2 是所有核共享的一致性枢纽。当某核的写（或显式作废请求）经过 L2 时，L2 会向**所有核**广播这个物理地址；
- 每个核的 L1D 用 `l1_l2_interface` 的 snoop 通路监听这些广播，检查自己是否缓存了该地址；
- 若命中（snoop hit），就更新或作废（invalidate）对应行，从而保持一致。

即便单核（`NUM_CORES=1`），snoop 机制仍被复用来处理「同义词（synonym）」——两个虚拟地址映射到同一物理地址时，snoop 能让后一次访问命中已有行而不是新填一份。

#### 4.4.2 核心流程

snoop 是一个跨两拍的两段流水：

```text
L2 回填响应到达 l1_l2_interface（stage1）
        │  取出响应里的物理地址，拆出 {tag, set_idx}
        │  拉起 l2i_snoop_en + l2i_snoop_set
        ▼
dcache_tag_stage：用 snoop 读端口（sram_tags.read2）读出该组各路的 tag 与 valid
        │  返回 dt_snoop_tag[way] / dt_snoop_valid[way]
        ▼
l1_l2_interface（stage2）：比较 snoop_tag == 响应里的 tag 且 valid
        │  命中 → snoop_hit_way_oh
        ▼
决定回填/作废哪一路：
   - 命中已有行 → 更新该路（把新数据写进去，或作废它）
   - 未命中     → 填到 LRU 路
```

注意 snoop 用的是**物理地址**（`{dcache_tag_stage2, dcache_set_stage2}`），但 tag SRAM 是**虚拟索引**的。这又回到 4.1 的约束：因为「组索引 ⊆ 页偏移」，物理地址的组号 == 虚拟地址的组号，所以用物理地址的 `set_idx` 去读虚拟索引的 tag SRAM 是合法的，不会读错组。

#### 4.4.3 源码精读

**snoop 请求的产生。** 当 L2 回填响应有效且类型是 DCACHE 时，发起一次 snoop，[l1_l2_interface.sv:237-238](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L237-L238)：

```systemverilog
assign l2i_snoop_en = l2_response_valid && l2_response.cache_type == CT_DCACHE;
assign l2i_snoop_set = dcache_set_stage1;   // 响应地址的低 $clog2(L1D_SETS) 位
```

**标签级响应 snoop。** `dcache_tag_stage` 用 `sram_tags` 的第二个读端口服务 snoop，[dcache_tag_stage.sv:188-190](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L188-L190)，输出 `dt_snoop_tag` 与 `dt_snoop_valid`。同样有旁路处理「正在写同一组」的情形，[dcache_tag_stage.sv:222-228](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L222-L228)。

**snoop 命中比较。** 在 `l1_l2_interface` 的第二拍，把响应里的物理 tag 与各路 snoop 读出的 tag 比对，[l1_l2_interface.sv:271-278](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L271-L278)：

```systemverilog
for (way_idx = 0; way_idx < `L1D_WAYS; way_idx++)
    assign snoop_hit_way_oh[way_idx] = dt_snoop_tag[way_idx] == dcache_tag_stage2
                                       && dt_snoop_valid[way_idx];
```

**决定回填路。** snoop 命中则更新已有路，否则填 LRU 路，[l1_l2_interface.sv:289-295](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L289-L295)。注释 [l1_l2_interface.sv:284-287](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L284-L287) 点明这一招同时处理「写更新」和「同义词（cache synonyms）」。

**作废 vs 写入。** 回填时 valid 位由响应类型决定，[l1_l2_interface.sv:317](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L317)：

```systemverilog
assign l2i_dtag_update_valid = !response_dinvalidate;   // 作废请求时 valid=0
```

若是作废请求（`response_dinvalidate`，对应其他核的写使无效或本核的 `CACHE_DINVALIDATE`），就把该行 valid 清 0；否则把新数据写进去并置 valid=1。最终通过 `l2i_dtag_update_en_oh`（标签/valid）与 `l2i_ddata_update_*`（数据）两路写回 L1D，分别落到 [dcache_tag_stage.sv:191-194](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L191-L194) 的 tag SRAM 写端口与 [dcache_data_stage.sv:486-488](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L486-L488) 的 data SRAM 写端口。

> 设计要点：L1D 对软件暴露了 `CACHE_DFLUSH`（写回并作废）、`CACHE_DINVALIDATE`（作废）等显式缓存控制指令（见 [u2-l3]、[u2-l4]），它们在 [dcache_data_stage.sv:329-343](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L329-L343) 被识别并经 store 队列发给 L2，从而触发上述 snoop 链路，实现软件主动维护一致性。

#### 4.4.4 代码实践

**实践目标**：用源码确认 snoop 能正确处理「同义词」场景，并理解作废如何传播。

**操作步骤**：

1. 阅读 [l1_l2_interface.sv:284-295](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L284-L295)，确认：当 snoop 命中已有路时，回填**不会**新占一路，而是更新原路。这保证了同一物理行在 L1D 中至多一份，避免同义词产生两份副本。
2. 追踪作废路径：假设核 B 执行 `CACHE_DINVALIDATE`（或 L2 因核 A 的写而广播作废），顺着 [dcache_data_stage.sv:339-343](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L339-L343) → store 队列 → L2 → `l2i_snoop_en` → [l1_l2_interface.sv:317](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L317)（`valid <= 0`）的链条。
3. 断言 [l1_l2_interface.sv:373-375](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L373-L375) 保证「snoop 最多命中一路」，对应「同一物理行至多在一路」这一不变量。

**需要观察的现象 / 预期结果**：

- snoop 命中→更新原路，避免同义词重复；
- 作废请求把 valid 写 0，使下次访问该行必缺失、重新从 L2 取最新值；
- 单核默认配置下 snoop 主要服务同义词；多核（见 [u10-l3]）才真正用于核间一致。

#### 4.4.5 小练习与答案

**练习 1**：snoop 用物理地址的 `set_idx` 去读「虚拟索引」的 tag SRAM，为什么不会读错组？

**参考答案**：因为 VI/PT 约束保证「组索引位 ⊆ 页内偏移位」，而翻译不改页内偏移，所以同一物理地址在虚拟和物理两种地址下算出的 `set_idx` 永远相同。用物理 `set_idx` 读虚拟索引的 SRAM，读到的正是该物理行所在的那一组。

**练习 2**：多核下，核 A 写了某行后，核 B 缓存里的同一行如何被作废？

**参考答案**：核 A 的 store 经其 store 队列发往 L2；L2 作为一致性枢纽，向所有核广播该物理地址的作废（`response_dinvalidate`）。核 B 的 `l1_l2_interface` 收到后发起 snoop，在 L1D 中命中该行，通过 `l2i_dtag_update_valid = 0` 把该行 valid 清零。核 B 下次读该行便缺失，从 L2 取回核 A 写入的最新值。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成下面这个「源码阅读 + 推演」综合任务：

**场景**：单核默认配置下，一个线程执行如下伪代码（均为普通 cached 访问）：

```text
s0 = 0x1000           ; 基址寄存器，指向一个尚未访问过的缓存行
load_32 s1, [s0 + 0]  ; 第一次读该行 → 缺失
load_32 s2, [s0 + 4]  ; 紧接着再读同行下一个字
store_32 s3, [s0 + 8] ; 写同行另一个字
load_32 s4, [s0 + 8]  ; 立刻读回刚写的字
```

请按下列要求逐条分析，并标注所用源码位置：

1. **VI/PT 推演**：算出地址 `0x1000` 的 `{tag, set_idx, offset}` 三段值（提示：行偏移 6 位、组索引 6 位）。确认 `set_idx` 落在页内偏移内。
2. **两拍流水线**：第一次 `load_32` 在 `dcache_tag_stage` 算地址、查 DTLB（MMU 关闭走恒等映射）；在 `dcache_data_stage` 判定缺失（[dcache_data_stage.sv:505](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L505)），发 `dd_cache_miss`，回滚取指并挂起线程，等 L2 回填后唤醒（缺失→L2 的细节见 [u6-l2]）。
3. **LRU**：回填时 `cache_lru` 选出某路（初始全空，任选），填入数据并把它挪到「最近使用」。第二次 `load_32` 命中同行，更新 LRU（[dcache_data_stage.sv:514](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L514)）。
4. **store 旁路**：`store_32` 进 store 队列；紧跟的 `load_32` 命中 store 队列里未发出的同地址条目，经 `sq_store_bypass_*`（[l1_store_queue.sv:321-334](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv#L321-L334)）直接返回刚写的值，无需等回写。
5. **snoop（可选）**：若把场景改成多核，描述核 A 的 `store_32` 如何经 L2 广播使核 B 的副本作废（[l1_l2_interface.sv:317](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L317)）。

**预期结果**：你能用一条连贯的故事，把「地址切片 → 标签级 → 数据级命中/缺失 → LRU 选路 → store 队列合并与旁路 →（多核）snoop 作废」整条链路讲清楚，并且每一步都能指到具体源码行。这就达到了本讲的学习目标。

## 6. 本讲小结

- Nyuzi L1D 是**虚拟索引 / 物理标签（VI/PT）**的组相联缓存：用虚拟地址的 `set_idx` 索引（与 TLB 并行，省一拍），用物理 tag 比对（保证一致）。
- 避免「别名」的根因是约束 **组索引位 + 行偏移位 ≤ 页偏移位（12 位）**，所以 `L1D_SETS ≤ 64`；这条约束由仿真断言与配置注释双重守护。
- L1D 占两拍：**标签级** `dcache_tag_stage` 算地址、查 DTLB、并行读各路 tag/valid 并响应 snoop；**数据级** `dcache_data_stage` 判命中、读数据、检测 fault、发缺失/回滚/更新 LRU。
- 替换用 **伪 LRU 树**（`cache_lru`），路数限 1/2/4/8；fill 优先于 access 以避免活锁。
- 写操作经 **store 队列**：支持同地址写合并、load-after-store 旁路、sync 原语等待。
- 多核一致性靠 **snoop**：L2 广播物理地址，各核 L1D 监听并更新/作废命中行；单核下 snoop 也用于消除同义词。
- DTLB miss / page fault / 对齐错 / 越权访问等都在数据级汇聚成精确 trap；MMU 关闭时 TLB 旁路为恒等映射，裸机程序无需填 TLB。

## 7. 下一步学习建议

- 缺失之后到底发生了什么？线程如何挂起、L2 如何回填、又如何唤醒线程？请接着学 **[u6-l2 L1-L2 接口与队列]**，它会展开 `l1_load_miss_queue`、`l1_store_queue` 与 `l1_l2_interface` 的完整协作。
- 想理解 DTLB miss 之后软件如何填表项、TLB 表项长什么样？看 **[u7-l1 软件管理 TLB 与地址翻译]**，它详讲 `tlb.sv` 与 `tlb_entry_t`。
- 缺失与 fault 如何变成精确异常、如何回滚取指？看 **[u7-l3 Trap 处理与回滚]**，它会从写回级的视角把 trap 链路补全。
- 同步访存（LL/SC）与本讲的 store 队列、`dd_load_sync_pending` 紧密相关，深入细节见 **[u10-l1 同步内存操作 LL/SC 与 membar]**。
