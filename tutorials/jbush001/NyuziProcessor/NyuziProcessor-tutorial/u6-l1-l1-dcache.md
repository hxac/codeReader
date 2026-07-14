# L1 数据缓存

## 1. 本讲目标

本讲放大讲解 Nyuzi 单核流水线中「访存路径」最关键的一级——L1 数据缓存（L1 Data Cache，简称 L1D）。学完后你应当能够：

- 说清 L1D 为什么是「虚拟索引 / 物理标签（Virtually Indexed, Physically Tagged，VI/PT）」结构，以及它如何在不增加别名（alias）的前提下省掉一次翻译延迟。
- 用 `L1D_SETS ≤ 64` 这个约束推导出 VI/PT 不产生别名的数学条件。
- 区分「标签级 `dcache_tag_stage`」和「数据级 `dcache_data_stage`」两拍各自的职责，并说清 DTLB 在其中的位置。
- 讲清伪 LRU（pseudo-LRU）替换策略、store 经由 store 队列的合并与旁路，以及多核/多线程下的 snoop 一致性。

本讲承接 u3-l2（单核流水线总览）与 u5-l1（操作数 fetch）：访存指令在操作数 fetch 之后进入 `PIPE_MEM` 通路，其基址（`of_operand1`）与立即数相加得到访存地址，再交给本讲的两级缓存。

## 2. 前置知识

在进入源码前，先用三个直觉建立心智模型。

**为什么要缓存？** 内存比核心慢几十到上百倍。缓存把最近用到的数据行（cache line）放在核心旁边的小 SRAM 里，让多数访存一拍命中。Nyuzi 的缓存行恰好等于一个向量宽度：

\[ \text{CACHE\_LINE\_BYTES} = \text{NUM\_VECTOR\_LANES} \times 4 = 16 \times 4 = 64 \text{ 字节} \]

这样一条向量块访存（`MEM_BLOCK`）正好搬动一整行，地址天然对齐。

**组相联（set-associative）。** 地址被切成三段：`tag | set_idx | offset`。访存时先用 `set_idx` 选出一个「组」，组里有若干「路（way）」，再比较各路的 `tag` 判断是否命中。Nyuzi L1D 默认 4 路 64 组，容量为：

\[ \text{容量} = \text{SETS} \times \text{WAYS} \times \text{CACHE\_LINE\_BYTES} = 64 \times 4 \times 64 = 16384 \text{ 字节} = 16\,\text{KiB} \]

**虚拟地址 vs 物理地址。** 程序用的是虚拟地址（VA），内存里存的是物理地址（PA）。两者由 TLB 翻译。缓存「用哪个地址来索引和比较」决定了它的设计难度——这正是本讲 VI/PT 要解决的核心问题。表 2-1 列出后续会反复出现的地址位宽。

| 符号 | 含义 | 默认值 |
|---|---|---|
| `PAGE_SIZE` | 虚拟页大小 | 4096（12 位页内偏移） |
| `CACHE_LINE_BYTES` | 缓存行大小 | 64（6 位行偏移） |
| `L1D_SETS` | L1D 组数 | 64（6 位组索引） |
| `L1D_WAYS` | L1D 路数 | 4 |
| `PAGE_NUM_BITS` | 页号位宽 | 20 |
| `DCACHE_TAG_BITS` | L1D 标签位宽 | 20 |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [hardware/core/config.svh](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh) | 可配置参数 `L1D_WAYS`/`L1D_SETS` 及其约束注释 |
| [hardware/core/defines.svh](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh) | 派生常量（页大小、缓存行、标签位宽）与 `l1d_addr_t` 地址结构体 |
| [hardware/core/dcache_tag_stage.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv) | L1D 标签级：读标签、查 DTLB、算物理地址、维护 LRU、响应 snoop |
| [hardware/core/dcache_data_stage.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv) | L1D 数据级：命中判定、读数据、检测缺失/故障、驱动 store |
| [hardware/core/cache_lru.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv) | 伪 LRU 替换算法，决定新行填入哪一路 |
| [hardware/core/l1_store_queue.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv) | store 缓冲：写合并、写旁路、同步访存（辅助理解 store 路径） |
| [hardware/core/l1_l2_interface.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv) | L1 与 L2 之间的接口，含 snoop 三级流水（辅助理解一致性） |

---

## 4. 核心概念与源码讲解

### 4.1 虚拟索引 / 物理标签（VI/PT）的组织

#### 4.1.1 概念说明

缓存若用**虚拟地址**索引（VI）和比较，可以与 TLB 并行、甚至不查 TLB，速度最快，但同一个物理地址可能被两个虚拟地址映射到不同组，产生「别名」——同一份数据在缓存里出现两份，写出不一致结果。若用**物理地址**索引和比较（PI/PT），则没有别名问题，但必须等 TLB 翻译完才能开始查缓存，延迟高。

VI/PT 是两者的折中：**用虚拟地址的低若干位（组索引）索引，用物理地址的高位（标签）比较**。它的妙处在于：只要「组索引 + 行偏移」的总位数不超过「页内偏移」位数，那么被索引的位就落在 TLB 不翻译的页内偏移里，于是「虚拟索引 == 物理索引」，既快又不会别名。

#### 4.1.2 核心流程

L1D 地址（`l1d_addr_t`）被切成三段，对应虚拟地址的位段：

\[ \underbrace{\text{va}[31{:}12]}_{\text{tag, 20 位}} \; \underbrace{\text{va}[11{:}6]}_{\text{set\_idx, 6 位}} \; \underbrace{\text{va}[5{:}0]}_{\text{offset, 6 位}} \]

由于页大小为 \(2^{12}=4096\)，页内偏移正好是低 12 位（`va[11:0]`）。组索引（6 位）加行偏移（6 位）合计 12 位，恰好填满页内偏移。TLB 只翻译高 20 位页号，因此：

\[ \text{va}[11{:}0] = \text{pa}[11{:}0] \quad\Longrightarrow\quad \text{虚拟组索引} = \text{物理组索引} \]

VI/PT 不产生别名的充要条件是「一路的容量不超过一页」：

\[ \text{L1D\_SETS} \times \text{CACHE\_LINE\_BYTES} \le \text{PAGE\_SIZE} \]

代入默认值：\(64 \times 64 = 4096 = \text{PAGE\_SIZE}\)，刚好满足。若把 `L1D_SETS` 翻倍到 128，组索引变成 7 位，会用到 `va[12]`，而这一位属于被翻译的页号——同一物理页里的数据可能落到两个不同组，别名就出现了。

#### 4.1.3 源码精读

派生常量集中定义在 defines.svh，注意它们之间的位数关系：

[defines.svh:293-301](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L293-L301) —— 这段定义了 `PAGE_SIZE`、`CACHE_LINE_BYTES`、`CACHE_LINE_OFFSET_WIDTH` 与 `DCACHE_TAG_BITS`。其中 `DCACHE_TAG_BITS = 32 - (CACHE_LINE_OFFSET_WIDTH + $clog2(L1D_SETS)) = 32 - 6 - 6 = 20`，与页号位宽一致，说明标签存的就是物理页号。

[defines.svh:320-324](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L320-L324) —— `l1d_addr_t` 结构体把地址显式拆成 `tag / set_idx / offset` 三段，正是上面公式里那三段。

config.svh 顶部用注释写明了这条硬约束：

[config.svh:33-43](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh#L33-L43) —— 注释明确写出「L1D_SETS must be 64 or fewer (page size / cache line size)」，目的是「避免 VI/PT 缓存中的别名」，并给出默认 `L1D_SETS 64 // 16k`。同一段还约束了 `L1D_WAYS ≥ THREADS_PER_CORE`（见 4.3 节）。

dcache_tag_stage.sv 顶部注释把整套设计意图说得很清楚：

[dcache_tag_stage.sv:29-37](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L29-L37) —— 说明 L1D 是「virtually indexed and physically tagged」，标签存的是 TLB 翻译出的物理地址；并指出 snoop 用物理地址索引但标签阵列是虚拟索引，为避免别名必须保证 `cache line size * num sets <= page_size`。

这条约束在仿真期还被编译成断言强制检查：

[dcache_tag_stage.sv:162-167](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L162-L167) —— `assert(L1D_SETS <= 64)` 且组数必须是 2 的幂；若配置违反，仿真会立即报错终止。

物理地址在本级由 TLB 翻译结果拼装而成。注意「页内偏移直接透传，页号取自 TLB」：

[dcache_tag_stage.sv:312-313](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L312-L313) —— `dt_request_paddr = {ppage_idx, fetched_addr[31 - PAGE_NUM_BITS:0]}`：高位是物理页号 `ppage_idx`（来自 TLB），低位 12 位直接取自（未翻译的）虚拟地址 `fetched_addr`。这正是「虚拟索引==物理索引」在代码里的体现。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：亲手算出 VI/PT 不别名所需的位数条件，并在源码里找到对应代码。
2. **步骤**：
   - 在 defines.svh 找到 `PAGE_SIZE`、`CACHE_LINE_OFFSET_WIDTH`，计算页内偏移位数（应为 12）与行偏移位数（应为 6）。
   - 在 config.svh 确认 `L1D_SETS = 64`，计算组索引位数（应为 6）。
   - 验证 `组索引位数 + 行偏移位数 = 6 + 6 = 12 = 页内偏移位数`。
   - 在 dcache_tag_stage.sv 找到 `assert(L1D_SETS <= 64)`（约 165 行）。
3. **现象与预期结果**：假设把 `L1D_SETS` 改为 128，组索引变 7 位，会越过页内偏移的最高位 `va[12]`。该位属于页号、会被 TLB 翻译，于是同一物理地址可能映射到两个不同组——这就是别名。源码里那条断言会在仿真启动时直接失败，阻止这种配置。
4. 关于在本机真正触发断言失败的具体构建命令：**待本地验证**（可参考 u1-l2 的 `cmake . && make` 流程后，修改 config.svh 重新构建仿真目标）。

#### 4.1.5 小练习与答案

**练习 1**：若把 `CACHE_LINE_BYTES` 减半到 32（保持向量宽度不变只是假设），`L1D_SETS` 的上限应调整为多少才能继续满足 VI/PT 不别名？

**答案**：新约束为 `L1D_SETS × 32 ≤ 4096`，即 `L1D_SETS ≤ 128`。注意真实项目里 `CACHE_LINE_BYTES` 必须等于向量宽度，此处仅为练习位数推导。

**练习 2**：为什么标签位宽 `DCACHE_TAG_BITS` 恰好等于页号位宽 `PAGE_NUM_BITS`？

**答案**：因为标签存的是物理页号，而组索引与行偏移合起来正好占满页内偏移，所以标签就等于整个页号。

---

### 4.2 标签级与数据级两阶段

#### 4.2.1 概念说明

L1D 查询分两拍：

- **标签级 `dcache_tag_stage`**：用虚拟地址的组索引读出所有路的标签与 valid 位，并行查 DTLB 得到物理页号与权限位；把「物理地址、各路标签、TLB 结果」一齐推到下一拍。
- **数据级 `dcache_data_stage`**：用物理标签与各路标签比较判定命中，命中则读数据 SRAM；同时做对齐、故障检测、IO/控制寄存器分流，并在缺失时通知 L2。

之所以拆成两拍，是因为 SRAM 读本身有一拍延迟。把「读标签/翻译」和「比较/读数据」分别放在两拍，能让标签读取与 TLB 查询并行进行。

#### 4.2.2 核心流程

```
of_operand1[lane] + immediate          ← 操作数 fetch 送来的基址+偏移（虚拟地址）
        │
 [标签级] 读标签 SRAM(按虚拟组索引) ──┐
        查 DTLB(按虚拟页号) ──────────┤  并行
        读 valid 位、维护 LRU          │
        └──────────────────────────────┴──→ 推到下一拍：物理地址 + 各路标签
                                                    │
 [数据级] 物理标签 vs 各路标签比较 ──→ 命中?
              是 → 读数据 SRAM, 输出 dd_load_data, 更新 LRU
              否 → cache_near_miss? 否则 dd_cache_miss → 挂起线程、回滚、向 L2 请求
```

#### 4.2.3 源码精读

**标签级。** 地址计算与请求判定：

[dcache_tag_stage.sv:130-135](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L130-L135) —— `cache_load_en` 仅在「访存类、非控制寄存器、是 load」时拉高；`request_addr_nxt = of_operand1[scgath_lane] + immediate_value` 是访存虚拟地址（scatter/gather 时按 `scgath_lane` 选 lane）。

标签 SRAM 每路一块，**两个读口**：一个服务流水线 load（`read1`，按虚拟组索引），一个服务 snoop（`read2`，按物理组索引，见 4.4 节）：

[dcache_tag_stage.sv:180-194](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L180-L194) —— 关键看 `read1_addr(request_addr_nxt.set_idx)`（虚拟组索引）与 `read2_en(l2i_snoop_en)`（snoop）。valid 位用触发器而非 SRAM，因为复位时需全部清零；同周期写入时还做了旁路（bypass）保证读到最新值。

[dcache_tag_stage.sv:210-219](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L210-L219) —— 流水线读 valid 位，若本周期恰好在同一组写入，则直接取新值（`dt_valid[way] <= l2i_dtag_update_valid`），避免读到陈旧状态。

DTLB 实例化与 MMU 关闭时的恒等映射：

[dcache_tag_stage.sv:233-253](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L233-L253) —— DTLB 用 `request_addr_nxt` 的高位（虚拟页号）与当前 ASID 查询，输出物理页号与 present/writable/supervisor 等权限位。

[dcache_tag_stage.sv:258-276](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L258-L276) —— 当 `cr_mmu_en` 为 0（裸机未开 MMU），TLB 结果被强制为「命中、可写、present、用户态」，物理页号直接等于虚拟页号。这就是 hello_world 这类裸机程序无需配置 TLB 即可访存的原因（承接 u1-l4）。

**数据级。** 先按地址区域把请求分类（普通缓存访问 / IO 区域 / 控制寄存器 / cache control）：

[dcache_data_stage.sv:192-206](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L192-L206) —— `addr_in_io_region` 用模式匹配 `0xffff????` 识别 MMIO；由此把请求分成 `cached_access_req`（走 L1D）、`io_access_req`（走 IO 队列）等。注意「store 掩码为 0 时不视为访存请求」这一细节，它让被掩码的 scatter store 不触发故障（见 185-187 行 `lane_enabled`）。

命中判定是整个 L1D 的核心——**用物理标签比较**：

[dcache_data_stage.sv:350-357](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L350-L357) —— `way_hit_oh[way] = (dt_request_paddr.tag == dt_tag[way]) && dt_valid[way]`。`dt_request_paddr.tag` 是物理标签（TLB 翻译得到），`dt_tag[way]` 是标签级读出的（物理）标签。两者都是物理的，所以比较合法且无别名问题。

[dcache_data_stage.sv:361-363](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L361-L363) —— `cache_hit` 还要求 `dt_tlb_hit`（TLB 命中）；同步 load 首次访问会被刻意视为未命中（需先到 L2 登记，见 359-360 行注释）。

命中时读数据 SRAM，地址由「命中路号 + 物理组索引」拼接：

[dcache_data_stage.sv:475-489](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L475-L489) —— `l1d_data` 是一块 `L1D_WAYS * L1D_SETS` 项的 SRAM，读地址 `{way_hit_idx, dt_request_paddr.set_idx}`。写口只接 L2 回填（`l2i_ddata_update_*`），说明 **store 不直接写数据 SRAM**（见 4.3 节）。

缺失与「near miss」特例：

[dcache_data_stage.sv:496-509](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L496-L509) —— `cache_near_miss` 指「此刻同一行正在被 L2 回填」的情况：若挂起线程将永远收不到唤醒，于是改为回滚重试而非挂起；普通缺失则拉高 `dd_cache_miss`，由下游 miss 队列向 L2 请求并挂起当前线程。

故障检测与精确顺序：

[dcache_data_stage.sv:258-285](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L258-L285) —— 依次检测对齐故障、特权操作、页不存在（page fault）、supervisor 越权、只读页写入。注意 page/supervisor/write fault 都先要求 `dt_tlb_hit`，因为 TLB 缺失时权限位无效。这些故障汇总为 `any_fault`，会抑制该指令的全部副作用，并最终送写回级翻译成对应 trap（承接 u7-l3）。

性能事件：

[dcache_data_stage.sv:614-618](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L614-L618) —— 产出 `dd_perf_dcache_hit/miss/dtlb_miss` 三个脉冲，汇入 core.sv 的 `perf_events`（承接 u11-l2）。

#### 4.2.4 代码实践（源码阅读 + 单元测试）

1. **目标**：验证「命中看物理标签、读数据用物理组索引」这条数据通路。
2. **步骤**：
   - 阅读 [test_dcache_data_stage.sv:21-70](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/unit/test_dcache_data_stage.sv#L21-L70)（单元测试驱动），看它如何驱动 `dt_request_paddr`、`dt_tag`、`dt_valid` 并观察 `dd_load_data`、`dd_cache_miss`。
   - 注意测试里 `NORMAL_ADDR = 'h80000020`（缓存区）与 `IO_ADDR = 'hffff0010`（IO 区）两个常量，对应数据级 192-206 行的分类逻辑。
   - 在 `tests/unit/` 下用 `runtest.py` 运行该单元测试（具体命令格式 **待本地验证**，框架会单独用 Verilator 编译该模块并在输出里查找 `PASS`）。
3. **现象与预期结果**：当 `dt_tag[way]` 等于 `dt_request_paddr.tag` 且 `dt_valid[way]` 为 1 时，应观察到 `dd_cache_miss` 为 0、`dd_load_data` 出现有效数据；故意改 `dt_tag` 使其不等，则 `dd_cache_miss` 拉高、`dd_suspend_thread` 拉高。

#### 4.2.5 小练习与答案

**练习 1**：为什么命中判定 `way_hit_oh` 用 `dt_request_paddr.tag` 而不是虚拟地址的 tag？

**答案**：标签阵列里存的是物理标签（TLB 翻译后写入），所以必须用物理标签比较；用虚拟 tag 比较会把别名误判并破坏一致性。

**练习 2**：`cache_near_miss` 为什么不能像普通缺失那样挂起线程？

**答案**：因为目标行此刻正被 L2 回填，挂起后该线程收不到额外的唤醒信号，会永久睡眠；所以改为回滚取指、下拍重试，重试时数据已就位。

---

### 4.3 LRU 替换与 store 合并

#### 4.3.1 概念说明

**替换策略。** 缺失时需要选一路填入新行。理想是 LRU（最近最少使用），但严格 LRU 在 4 路以上实现昂贵。Nyuzi 用「伪 LRU」：用一个二叉树状的少量标志位近似追踪「最久未用」的路，硬件代价小得多。

**store 路径。** L1D 的数据 SRAM 写口只接 L2 回填——也就是说 **store 不直接落入 L1D 数据阵列**。store 先进入 `l1_store_queue`（每线程一个表项），由队列合并（write-combine）后发往 L2，L2 的 `STORE_ACK` 响应再通过 snoop 把更新后的整行回填进 L1D（若该行已在缓存）。期间，后续对同一地址的 load 可经「写旁路（store bypass）」直接读到尚未下发的 store 数据。

**为什么路数要 ≥ 线程数？** 多线程并发访存时，若路数太少，多个线程可能互相把对方的行挤出去形成「乒乓」，导致活锁（livelock）。伪 LRU 在「连续回填」时给 fill 最高优先级，配合 `L1D_WAYS ≥ THREADS_PER_CORE` 共同规避这一问题。

#### 4.3.2 核心流程

```
缺失回填:  l2i_dcache_lru_fill_en → cache_lru 算出 fill_way(LRU 路) → 填入并提到 MRU
正常命中:  dd_update_lru_en/way   → cache_lru 把命中路提到 MRU
并发规则:  fill 与 access 同周期 → fill 优先(避免回填被立即挤出 / 避免活锁)

store:  dd_store_en → l1_store_queue(每线程一项)
            ├─ 同地址 → can_write_combine 合并掩码
            ├─ 下一拍 dequeue → 发往 L2
            └─ 同地址 load → sq_store_bypass 旁路读出
        L2 STORE_ACK → snoop 命中 → 回填整行到 L1D
```

#### 4.3.3 源码精读

cache_lru.sv 顶部注释完整解释了伪 LRU 的二叉树编码与「fill 优先」的两个理由：

[cache_lru.sv:40-46](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv#L40-L46) —— 「fill 与 access 同周期时 fill 优先」：一是避免连续回填把刚填入的行又挤掉（低效），二是**避免两个线程互相驱逐对方的行而陷入活锁**。

[cache_lru.sv:96-113](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv#L96-L113) —— 4 路情形用 3 个标志位构成一棵树（根 `b`、左右 `a`/`c`，叶子 0–3 对应 4 路）。每个内部节点存「指向较不常用一侧」的方向位；访问某路时把路径上的方向位翻向相反方向，使其至少两拍后才可能再次成为 LRU。注释指出这比严格 LRU（需三拍）略宽松但实现简单得多。

标志位宽度随路数变化，且路数被限制为 1/2/4/8：

[cache_lru.sv:68-72](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv#L68-L72) 与 [cache_lru.sv:91-95](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv#L91-L95) —— `LRU_FLAG_BITS` 在 1/2/4/8 路时分别是 1/1/3/7 位；断言强制 `NUM_WAYS ∈ {1,2,4,8}`，这正是 u3-l3 提到「缓存路数必须是 1/2/4/8」的根源。

4 路 fill 选择与更新逻辑用 `casez` 硬编码：

[cache_lru.sv:148-171](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv#L148-L171) —— `fill_way` 由当前 `lru_flags` 经 `casez` 选出要替换的 LRU 路；`update_flags` 按新 MRU 路翻转相应标志位。

标签级把命中信息接给 cache_lru，把回填请求也接给它：

[dcache_tag_stage.sv:278-289](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L278-L289) —— `access_en = instruction_valid`、`access_set = request_addr_nxt.set_idx`（本周期访问的组），`update_en/update_way` 来自数据级下一拍的 `dd_update_lru_*`（命中才更新），`fill_en/fill_set` 来自 L2 回填。

数据级在命中且为缓存访问时驱动 LRU 更新：

[dcache_data_stage.sv:514-515](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L514-L515) —— `dd_update_lru_en = cache_hit && cached_access_req && !any_fault`，`dd_update_lru_way = way_hit_idx`。注意只有命中的 load/store 才会更新 LRU，缺失由 fill 路径更新。

store 合并（write-combine）逻辑在 l1_store_queue.sv，每线程一个表项，同地址的连续 store 在掩码上「或」起来合并成一次下发：

[l1_store_queue.sv:122-132](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv#L122-L132) —— `can_write_combine` 要求「表项有效、地址相同、非 flush/invalidate、非 sync、尚未下发」。

[l1_store_queue.sv:208-222](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv#L208-L222) —— 命中合并条件时 `mask <= mask | dd_store_mask`（按字节或），并把新数据覆盖到对应字节；否则写入新表项。这样对同一缓存行的多次 store 只产生一次 L2 请求。

写旁路让同地址 load 不必等 L2：

[l1_store_queue.sv:319-335](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv#L319-L335) —— 当 load 地址与某未下发的 store 表项地址相同，给出 `sq_store_bypass_mask` 与 `sq_store_bypass_data`，由写回级把旁路数据与 L1D 读出数据按掩码合并。

`L1D_WAYS ≥ THREADS_PER_CORE` 约束写在 config.svh 注释里（见 4.1.3 引用的 25-37 行）：路数少于线程数时多线程可能互相驱逐缓存行导致活锁，配合 cache_lru 的 fill 优先共同规避。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：理解一次 store 是如何「不写 L1D 数据阵列」却能让随后的 load 读到新值的。
2. **步骤**：
   - 在 dcache_data_stage.sv 找到 `dd_store_en`（约 291-293 行），确认它只是把 `dd_store_addr/mask/data` 发往 l1_l2_interface，并没有写 `l1d_data` 的写口。
   - 在 l1_store_queue.sv 跟踪 `can_write_combine` 与 `pending_stores[thread_idx].mask` 的「或」合并（218-219 行）。
   - 在 l1_store_queue.sv 跟踪 `sq_store_bypass_*`（319-335 行），理解后续 load 如何读到未下发的 store。
3. **现象与预期结果**：连续两个 `store_32` 写同一缓存行的不同字，应只产生一次 L2 `L2REQ_STORE`；中间插入的 `load_32` 同地址读，应通过 bypass 拿到最新值而不会读到旧 L1D 行。
4. 若要在仿真中观察波形，需借助 unit test 框架或 cosimulation，具体命令 **待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `cache_lru` 在 fill 与 access 同周期时让 fill 优先？

**答案**：连续回填时若 access 抢先更新 LRU，刚填入的行可能立刻被判为 LRU 被下一次 fill 挤出，既低效又在多线程下会演变成互相驱逐的活锁；fill 优先保证新填入的行至少留在 MRU 一拍。

**练习 2**：把 `L1D_WAYS` 设为 2（小于 `THREADS_PER_CORE=4`）会怎样？

**答案**：违反 config.svh 注释中的约束。4 个线程争抢 2 路，容易出现线程 A 把 B 的热行挤出、B 再把 A 的热行挤出的乒乓，最坏形成活锁；代码层面虽不一定立即报错，但功能上不可靠。

---

### 4.4 snoop 缓存一致性

#### 4.4.1 概念说明

多核与 store 缓冲都要求 L1D 能感知「别处对同一物理行的写入」。Nyuzi 用 **snoop（侦听）** 实现：每当 L2 给本核返回一个涉及 D-cache 的响应（如 `STORE_ACK`、`LOAD_ACK`、`DINVALIDATE_ACK`），L1D 都用该响应携带的物理地址去查自己的标签阵列，判断这一行是否在本地缓存：在则更新或失效，不在则忽略。

snoop 的难点正是 4.1 节那条约束：snoop 地址是物理地址，但标签阵列是按（虚拟）组索引组织的。因为「一路容量 ≤ 一页」，物理组索引 == 虚拟组索引，snoop 才能直接用物理地址的低位索引标签阵列。

#### 4.4.2 核心流程

```
L2 响应(物理地址, CT_DCACHE)
   │  [stage1] l2i_snoop_en=1, l2i_snoop_set=响应地址低位 → dcache_tag_stage read2 读标签
   │  [stage2] 物理标签 vs 各路 snoop 标签比较 → snoop_hit_way_oh
   │           命中 → 复用原路(dupdate_way=snoop_hit); 否则 → LRU 路(dt_fill_lru)
   │  [stage3] 按更新类型写标签/数据; DINVALIDATE 则置 valid=0
```

注意这是 l1_l2_interface 里的「三级响应流水」：先发地址给标签级 snoop（stage1），下一拍收 snoop 结果并决定更新哪一路（stage2），再下一拍才写数据 SRAM（stage3）。标签比数据先写一拍，是为了避免流水线 load 在两拍之间看到「标签已更新但数据未更新」的竞态。

#### 4.4.3 源码精读

标签级为 snoop 预留了第二个读口（已在 4.2.3 见过 read2）：

[dcache_tag_stage.sv:188-190](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L188-L190) —— `read2_en(l2i_snoop_en)`、`read2_addr(l2i_snoop_set)`、`read2_data(dt_snoop_tag[way])`。`l2i_snoop_set` 是 L2 响应地址的低位（物理组索引）。

[dcache_tag_stage.sv:222-228](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L222-L228) —— snoop 读 valid 位，同样有同周期写入旁路。`dt_snoop_valid` 与 `dt_snoop_tag` 一并回送 l1_l2_interface。

l1_l2_interface.sv 顶部注释解释了三级响应流水的设计意图：

[l1_l2_interface.sv:32-40](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L32-L40) —— 三级：① 把 store 响应地址送 L1D 标签级 snoop；② 检查 snoop 结果，选更新路；③ 更新数据，且必须比标签晚一拍以避免竞态。

stage1：判断是否需要 snoop 并取出组索引：

[l1_l2_interface.sv:235-238](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L235-L238) —— `l2i_snoop_en = l2_response_valid && l2_response.cache_type == CT_DCACHE`；`l2i_snoop_set = l2_response.address[$clog2(L1D_SETS)-1:0]`（物理地址低位当组索引）。

stage2：用物理标签比较，并决定更新哪一路：

[l1_l2_interface.sv:271-278](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L271-L278) —— `snoop_hit_way_oh[way] = (dt_snoop_tag[way] == dcache_tag_stage2) && dt_snoop_valid[way]`。`dcache_tag_stage2` 是响应地址的物理标签。

[l1_l2_interface.sv:284-295](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L284-L295) —— 关键：「若该数据已在某一路，就更新那一路；否则填到 LRU 路」。注释指出这同时处理「写更新」和「缓存同义（cache synonyms，两个虚拟地址指向同一物理地址）」。这就是为什么 4.1 节即便理论上无别名，工程上仍用 snoop 来兜底处理同义写回。

stage2/3：决定是否真正写标签与数据，以及失效语义：

[l1_l2_interface.sv:310-317](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L310-L317) —— `dcache_update_en` 在「本核 LOAD_ACK/STORE_ACK」或「DINVALIDATE 且 snoop 命中」时成立；`l2i_dtag_update_valid = !response_dinvalidate`，即失效响应把 valid 写 0，其余写 1。

[l1_l2_interface.sv:386-387](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L386-L387) —— 数据更新使能在 STORE_ACK 且 snoop 命中时也拉高，于是 store 经 L2 后把更新后的整行回填进 L1D（呼应 4.3 节「store 不直接写数据阵列」）。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：跟踪一次「本核 store → L2 → STORE_ACK → snoop 回填 L1D」的完整路径。
2. **步骤**：
   - 从 dcache_data_stage 的 `dd_store_en`（291 行）出发，到 l1_store_queue（下发 `L2REQ_STORE`，见 l1_l2_interface 444-445 行）。
   - 再看 l1_l2_interface stage1（235-238 行）如何把响应地址送 snoop，stage2（271-295 行）如何判定 snoop 命中并选路，stage3（386-387 行）如何写数据。
   - 对照 4.1 节约束，解释为什么 snoop 用物理地址的低位索引标签阵列是安全的。
3. **现象与预期结果**：store 之后紧接着 load 同一地址，若该行原本在 L1D，则 LOAD/STORE_ACK 的 snoop 会命中并更新原路；若原本不在 L1D，则填入 LRU 路。无论哪种，后续 load 都能命中并读到新值。
4. 多核 snoop 的端到端验证可用 `tests/stress/atomic` 与 `tests/core/multicore`，具体运行命令 **待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 snoop 用物理地址索引标签阵列却不会出错？

**答案**：因为 VI/PT 保证组索引落在页内偏移内，物理组索引 == 虚拟组索引，所以物理地址的低位可以直接用来索引按虚拟地址组织的标签阵列。

**练习 2**：snoop 命中与未命中分别如何选择更新路？

**答案**：命中时更新已有数据所在的那一路（保持单一副本、处理同义）；未命中时填入 `dt_fill_lru` 给出的 LRU 路（见 l1_l2_interface 289-295 行）。

---

## 5. 综合实践

把本讲四个模块串起来，做一次「地址的一生」跟踪。设有一条 `load_32 s1, 0(s0)`，其中 `s0 = 0x00001240`，且该地址尚未缓存、MMU 已开启、对应虚拟页已映射到物理页 `0x0009`。

1. **组索引与标签拆分**（4.1）：虚拟地址 `0x00001240` 的低 12 位页内偏移是 `0x240`，其中行偏移 `[5:0]=0`（64 字节对齐），组索引 `[11:6] = 0x240>>6 = 9`，虚拟页号 `[31:12]=0x00001`。
2. **标签级**（4.2）：DTLB 把虚拟页号 `0x00001` 翻译成物理页号 `0x00009`；标签 SRAM 用组索引 9 读出 4 路标签与 valid；物理地址被拼成 `{0x00009, 0x240}`。
3. **数据级缺失**（4.2）：物理标签 `0x00009` 与各路标签都不等，`cache_hit=0`；非 near miss，于是 `dd_cache_miss=1`、`dd_suspend_thread=1`、线程回滚挂起，miss 队列向 L2 发请求。
4. **回填与 LRU**（4.3）：L2 取回整行，`l2i_dcache_lru_fill_en=1`，cache_lru 选出组 9 的 LRU 路 `fill_way`（设为 2），新行填入第 2 路，并把第 2 路提到 MRU；标签写成 `0x00009`、valid=1；随后唤醒位图恢复该线程。
5. **重试命中**（4.2 + 4.4）：线程被唤醒后重新执行该 load，这次物理标签 `0x00009` 与第 2 路标签相等，`cache_hit=1`，从 `l1d_data` 读出整行并取出字 0；同时 `dd_update_lru_en` 把第 2 路再次提到 MRU。

**交付物**：一张标注了「组索引=9、tag=0x00009、way=2、LRU 变化、snoop 口未启用」的时序草图，并用一句话说明为什么整条路径里虚拟组索引与物理组索引始终相同。

---

## 6. 本讲小结

- L1D 是**虚拟索引 / 物理标签（VI/PT）**结构：组索引落在页内偏移内，使「虚拟索引==物理索引」，兼顾速度与无别名。
- 不别名的充要条件是 `L1D_SETS × CACHE_LINE_BYTES ≤ PAGE_SIZE`，默认 `64×64=4096` 刚好满足；`L1D_SETS ≤ 64` 被断言强制。
- 查询分**标签级**（读标签 + 查 DTLB + 算物理地址）与**数据级**（物理标签比较命中 + 读数据 + 检测缺失/故障）两拍；MMU 关闭时标签级做恒等映射。
- 替换用**伪 LRU**（二叉树标志位），fill 优先于 access 以避免低效与多线程活锁；`L1D_WAYS ≥ THREADS_PER_CORE` 是配套约束。
- **store 不直接写数据阵列**，而是进 `l1_store_queue` 做写合并与写旁路，最终由 L2 的 `STORE_ACK` 经 snoop 回填整行。
- **snoop** 用 L2 响应的物理地址查标签阵列，命中则更新原路（含同义处理），未命中填 LRU 路；`DINVALIDATE` 把 valid 置 0。三级响应流水让标签比数据先写一拍以避免竞态。

## 7. 下一步学习建议

- **u6-l2 L1-L2 接口与队列**：本讲反复出现的 `dd_cache_miss`、store 下发、snoop 回填都汇集到 `l1_l2_interface`，下一讲会完整讲解 miss 队列、store 队列与响应分发。
- **u6-l3 L2 缓存**：L1D 缺失最终去向是 L2，建议接着读 `l2_cache.sv` 的物理索引/物理标签四阶段流水。
- **u7-l1 TLB 与翻译**：本讲的 DTLB 只用了查询接口，TLB 表项结构、缺失 trap、ASID 与全局映射将在虚拟内存专题展开。
- **u10-l1 同步访存**：本讲刻意略过的 `MEM_SYNC`（LL/SC）在数据级的特殊处理（首次访问视为缺失、`dd_load_sync_pending`），留到并发同步专题细讲。
