# 指令取指与 I-Cache

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 Nyuzi 单核「取指」这两个流水级（`ifetch_tag_stage` 与 `ifetch_data_stage`）各自负责什么。
- 描述一条指令的 PC 是如何生成、如何被翻译成物理地址的（ITLB 查询流程）。
- 解释 I-Cache「虚拟索引 / 物理标签（VI/PT）」的命中判定是怎么做的。
- 跟踪一次 I-Cache 缺失：线程如何被挂起、请求如何发往 L2、数据回填后线程又如何被唤醒恢复取指。

本讲是 u3-l2（单核流水线总览）的延续，把流水线最前端的「取指」两拍放大讲透。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**取指要解决什么问题。** 流水线每个周期都想给后级送去一条「有效指令」。但指令放在内存里，访问内存很慢，所以处理器内部有一块小而快的「指令缓存（I-Cache）」用来缓存最近用过的指令。取指阶段的核心任务就是：拿一个 PC（program counter，程序计数器），去 I-Cache 里找，找到了（命中）就把指令读出来送下去；找不到（缺失）就去下一级缓存（L2）搬一整行回来，同时把发起这次取指的线程先「挂起」，等数据到了再「唤醒」它。

**为什么取指要分两级。** Nyuzi 把取指拆成 `ifetch_tag_stage`（标签级）和 `ifetch_data_stage`（数据级）两个周期。标签级负责「选线程、算 PC、读缓存标签、查 ITLB」；数据级负责「拿上一级读到的标签去比对、判定命中、读出真正的指令数据」。拆成两级是因为 SRAM 读、TLB 查询都需要一个周期的延迟，单周期做不完。

**虚拟地址与物理地址。** 程序里看到的是「虚拟地址（virtual address, vaddr）」，而内存和缓存底层用「物理地址（physical address, paddr）」。两者由「页表」做翻译，翻译结果会被缓存在「TLB（Translation Lookaside Buffer，旁路翻译缓冲）」里，避免每次都去查页表。指令取指用的 TLB 叫 ITLB。本讲假设你已知这些概念；地址翻译的完整机制在 u7-l1（软件管理 TLB）详讲。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `hardware/core/ifetch_tag_stage.sv` | 取指标签级：选线程、生成 PC、读 I-Cache 标签、查 ITLB、管理「等待缺失」的线程位图。 |
| `hardware/core/ifetch_data_stage.sv` | 取指数据级：比对标签判定命中、读出指令、检测各类 fault、产生 cache miss 信号。 |
| `hardware/core/tlb.sv` | 通用 TLB 模块，在取指阶段被实例化为 ITLB，做虚拟页号→物理页号翻译。 |
| `hardware/core/cache_lru.sv` | 伪 LRU（最近最少使用）模块，决定缺失回填时替换哪一路。 |
| `hardware/core/l1_l2_interface.sv` | L1↔L2 接口：把 I-Cache 缺失请求排队发往 L2，并在 L2 响应回填标签/数据、唤醒线程。 |
| `hardware/core/defines.svh` | 缓存几何常量与类型（`l1i_addr_t`、`l1i_tag_t`、缓存行宽度等）。 |
| `hardware/core/config.svh` | 可配置参数（`L1I_WAYS`、`L1I_SETS`、`ITLB_ENTRIES` 等）。 |

默认配置（`config.svh`）：`L1I_WAYS = 4`、`L1I_SETS = 64`、`ITLB_ENTRIES = 64`、`TLB_WAYS = 4`、`THREADS_PER_CORE = 4`。配合 `CACHE_LINE_BYTES = 64`（16 向量通道 × 4 字节），I-Cache 总容量为 \( 64 \times 4 \times 64 = 16384 \) 字节 = 16 KiB。

## 4. 核心概念与源码讲解

### 4.1 PC 生成与线程选择

#### 4.1.1 概念说明

Nyuzi 每个核有 4 个硬件线程（`THREADS_PER_CORE = 4`），每个线程维护自己独立的 PC。取指阶段每周期只能为一个线程服务，于是需要两件事：

1. **选线程**：从「当前可以取指的线程」里挑一个。挑选时尽量轮转，让不同线程交错取指，这样可以隐藏单线程的缓存缺失延迟（一个线程在等内存时，流水线还可以去取别的线程的指令）。
2. **生成 PC**：被选中的线程用它的 PC 去取指，并在下一拍把它的 PC 加 4（Nyuzi 指令定长 32 位 = 4 字节）。

此外还要处理两类「撤销已取指」的情况：分支解析后发现预测/顺序取指错了，要回滚（rollback）；以及本讲的重点——缓存缺失时要把多加的那次 4 字节扣回来。

#### 4.1.2 核心流程

取指标签级每周期大致做这些事（组合逻辑 + 寄存器更新）：

```
1. 计算「可取指线程位图」 can_fetch = ts_fetch_en & ~icache_wait_threads
   （被流水线后级允许取指，且不在「等缺失」状态里的线程）
2. 若本周期正在更新/失效 TLB，或被片上调试器挂起，则本周期不取指
3. 用轮询仲裁器 rr_arbiter 从 can_fetch 里选一个线程（独热码 selected_thread_oh）
4. pc_to_fetch = 该线程的 next_program_counter（虚拟地址）
5. 用 pc_to_fetch 的 set_idx 去读各路的标签 SRAM（4 路）
6. 同时把 PC 更新逻辑写入寄存器：
     reset              -> RESET_PC
     回滚该线程          -> wb_rollback_pc
     上一拍选中的线程缺失 -> PC - 4（把刚才那次取指的 +4 撤回，重试）
     本周期选中且可取指   -> PC + 4
```

注意第 5 步用的是**虚拟地址**的 set_idx，而最终命中比对用的是**物理地址**的 tag——这正是本讲后面要讲的「虚拟索引 / 物理标签」结构。

#### 4.1.3 源码精读

先看「可取指线程位图」与「本周期是否真的取指」：

[ifetch_tag_stage.sv:121-136](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L121-L136) 用中文说明：这段先注释了「挑线程时不预先排除正被回滚的线程」（回滚信号组合路径长，是时钟关键路径，所以宁可选中后用 `ift_instruction_requested` 把这条取指作废，也不在选线程时判断），随后定义 `can_fetch_thread_bitmap`（去掉等缺失的线程）和 `cache_fetch_en`（正在更新/失效 TLB 或被调试器 halt 时本周期不取指）。

线程选择靠一个轮询仲裁器：

[ifetch_tag_stage.sv:138-146](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L138-L146) 说明：`rr_arbiter` 按 `can_fetch_thread_bitmap` 做轮询仲裁，输出独热的 `selected_thread_oh`，再用 `oh_to_idx` 转成线程号 `selected_thread_idx`。`update_lru(cache_fetch_en)` 让仲裁器记住「上次选了谁」，从而公平轮转。

PC 更新逻辑是理解「缺失重试」的关键，逐线程实例化：

[ifetch_tag_stage.sv:151-167](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L151-L167) 说明：这是每个线程一份的 PC 寄存器 `next_program_counter`，优先级从高到低为 reset、回滚、缺失/近似缺失回退 4、正常 +4。其中「缺失回退 4」用 `last_selected_thread_oh`（上一拍被选中的线程）来定位是哪个线程的取指在数据级被判了缺失，从而把它在上一拍已经 +4 的 PC 扣回来，等唤醒后重取同一条指令。

最后，本周期真正去读缓存的地址：

[ifetch_tag_stage.sv:169](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L169) 说明：`pc_to_fetch` 选用被仲裁选中的线程的 PC（调试 halt 时改用 `ocd_thread`），它的 `.set_idx` 会被用来读 I-Cache 标签。

#### 4.1.4 代码实践

**实践目标**：在源码层面跟踪「PC +4 与缺失 −4 是如何配对」的，理解为何缺失后不会丢指令。

**操作步骤**：

1. 打开 [ifetch_tag_stage.sv:151-167](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L151-L167)。
2. 假设线程 0 的 PC 初始为 `0x100`，第 N 拍被选中取指：
   - 第 N 拍：`selected_thread_oh[0]=1 && cache_fetch_en=1` → `next_program_counter[0]` 由 `0x100` 变为 `0x104`。
   - 第 N+1 拍：数据级发现这次取指缺失（`ifd_cache_miss=1`），且 `last_selected_thread_oh[0]=1` → 命中「缺失回退」分支，`next_program_counter[0]` 由 `0x104` 减回 `0x100`。
3. 观察：缺失让线程 0 进入 `icache_wait_threads`（见 4.4），等回填唤醒后它会重新从 `0x100` 取指。

**需要观察的现象 / 预期结果**：PC 在缺失时回到原值，被唤醒后重取的是同一条指令，没有跳过。这是纯源码阅读型实践，无需运行（若要在波形中确认，可在仿真里观察 `next_program_counter[0]` 与 `ifd_cache_miss` 的时序关系）。

#### 4.1.5 小练习与答案

**练习 1**：为什么选线程时不直接排除「本拍正在被回滚」的线程？

**参考答案**：回滚信号（来自写回级 `wb_rollback_*`）要穿过很长的组合逻辑才能传到取指级，是时钟频率的关键路径。若在选线程时就判断回滚，会拖慢整个时钟。设计者选择「照常选线程，但在同周期用 `ift_instruction_requested` 把这条取指作废」，代价只是偶尔浪费一拍，但换来了更短的时钟周期。

**练习 2**：`next_program_counter` 在「缺失」与「回滚」两种情况下都会被改写，它们的来源信号分别是什么？

**参考答案**：回滚时写 `wb_rollback_pc`（来自写回级 `writeback_stage`），缺失/近似缺失时写「当前值 − 4」（把上一拍误加的 4 撤回）。

---

### 4.2 ITLB 查询与地址翻译

#### 4.2.1 概念说明

I-Cache 用**物理地址**的 tag 来判定命中，但程序给出的是**虚拟地址**。取指标签级在用虚拟地址读缓存标签的同时，要并行地把虚拟页号翻译成物理页号，这件事交给 ITLB。

Nyuzi 的 TLB 是「软件管理」的：硬件只负责查表和报告「缺失」，真正的页表遍历由软件 trap 处理程序完成（详见 u7-l1）。当 MMU 关闭（`cr_mmu_en=0`，比如裸机启动时），取指直接用「恒等映射（identity mapping）」，即虚拟地址 = 物理地址。

ITLB 也是组相联结构：默认 64 项、4 路，即 16 组、每组 4 路。

#### 4.2.2 核心流程

```
1. 取虚拟 PC 的高位作为「虚拟页号」request_vpage_idx，取当前线程的 ASID
2. 用 (vpage_idx 的低位作 set 索引) 在 ITLB 各路里查：
     命中条件 = 表项有效 && 虚拟页号相等 && (ASID 相等 || 全局位 global)
3. ITLB 输出：物理页号 ppage_idx、命中 hit、present、executable、supervisor
4. 取指标签级根据 cr_mmu_en 选择：
     MMU 开 -> 用 ITLB 结果
     MMU 关 -> 恒等映射，tlb_hit/present/executable 全部置 1
5. 物理地址 paddr = {ppage_idx, 虚拟地址的页内偏移[11:0]}
```

由于 ITLB 内部有一拍延迟，取指标签级在「这一拍组合输出」给数据级时，用的输入其实都是上一拍已经打寄存器的值（见源码注释）。

#### 4.2.3 源码精读

先看 ITLB 的输入如何构造：

[ifetch_tag_stage.sv:221-233](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L221-L233) 说明：当本周期要取指时，`request_vpage_idx` 取自 `pc_to_fetch` 的高位（虚拟页号），`request_asid` 取自被选中线程的 `cr_current_asid`；否则把输入让给「正在更新 ITLB」的写请求（因为读和写共用 TLB 端口，见 tlb.sv）。

ITLB 实例化：

[ifetch_tag_stage.sv:235-253](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L235-L253) 说明：用通用 `tlb` 模块实例化出 `itlb`，参数为 `ITLB_ENTRIES=64`、`TLB_WAYS=4`；`lookup_en=cache_fetch_en`，更新口接 `dt_update_itlb_*`（由数据缓存的 `CACHE_DTLB_INSERT` 操作驱动，软件填表项）。注意 `update_exe_writable` 在 ITLB 语境下表示「可执行」。

输出选择（MMU 开关）：

[ifetch_tag_stage.sv:257-276](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L257-L276) 说明：若该线程 `cr_mmu_en=1`，把 ITLB 的命中/present/executable/supervisor 与物理页号透传给数据级；否则强制恒等映射（命中、present、executable 全 1，物理页号直接取虚拟地址的页号），即不翻译。

物理地址拼装：

[ifetch_tag_stage.sv:331](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L331) 说明：`ift_pc_paddr = {ppage_idx, last_selected_pc[11:0]}`，即「物理页号 + 虚拟地址的页内偏移」。页内偏移 12 位恰好覆盖「6 位行内偏移 + 6 位组索引」，这正是下一节 VI/PT 结构成立的前提。

再看 ITLB 内部如何查表（通用 tlb 模块）：

[tlb.sv:150-153](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/tlb.sv#L150-L153) 说明：每一路的命中条件是「表项有效 && 虚拟页号相等 && (ASID 相等 || 全局位 global || 正在写全局项)」。`global` 位让多个进程共享同一段映射而不必各自建表项。

#### 4.2.4 代码实践

**实践目标**：确认「MMU 关闭时取指走恒等映射」，从而理解裸机程序为何能从地址 0 直接取指执行。

**操作步骤**：

1. 阅读 [ifetch_tag_stage.sv:267-275](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L267-L275)。
2. 回顾 u1-l4：裸机 hello_world 由 `elf2hex` 转成镜像后从地址 0 启动，此时内核尚未启用 MMU（`cr_mmu_en=0`）。
3. 因此 `ift_tlb_hit=1`、`ift_tlb_present=1`、`ift_tlb_executable=1`，`ppage_idx` 直接等于虚拟页号——程序地址就是物理地址。

**预期结果**：你能用一句话解释「为什么裸机程序不用管 TLB 也能跑」——因为取指标签级在 MMU 关闭时硬连线成恒等映射。

#### 4.2.5 小练习与答案

**练习 1**：ITLB 缺失（`tlb_hit=0`）时，取指会立刻去 L2 搬数据吗？

**参考答案**：不会。看 4.3 的 `cache_hit = |way_hit_oh && ift_tlb_hit`，TLB 未命中时不会判为缓存命中，但 `ifd_cache_miss` 的条件里要求 `ift_tlb_hit`（见 4.3），所以 TLB 缺失既不算命中也不算缓存缺失，而是产生 `ifd_tlb_miss` fault，由后级触发 `TT_TLB_MISS` trap，交给软件填充 TLB 后重试（详见 u7-l1）。

**练习 2**：ITLB 的「全局位 global」解决了什么问题？

**参考答案**：让一段对所有进程都相同的映射（例如内核代码）只存一份表项，查询时忽略 ASID 即可命中，省去每个进程各自维护一份相同映射。

---

### 4.3 I-Cache 命中判定（标签级与数据级）

#### 4.3.1 概念说明

I-Cache 是一个「组相联」结构：默认 64 组、每组 4 路，每行 64 字节（恰好等于一条向量，也等于一个缓存行）。一次取指的判定分两拍：

- **标签级（ifetch_tag_stage）**：用虚拟地址的 `set_idx` 并行读出该组 4 路的 tag 和 valid 位（从 SRAM/flop 读，结果下一拍才到）。
- **数据级（ifetch_data_stage）**：拿到物理地址的 tag，与上一拍读出的 4 路 tag 逐路比对，若某路 tag 相等且 valid，则命中；命中后用「路号 + 组号」去读数据 SRAM，取出这一拍要的 4 字节指令。

关键设计：**虚拟索引 / 物理标签（VI/PT）**。用虚拟地址的低位（组索引 + 行内偏移）去索引 SRAM，用物理地址的高位（tag）去比对。这之所以合法，是因为 \( \text{L1I\_SETS} \times \text{CACHE\_LINE\_BYTES} = 64 \times 64 = 4096 = \text{PAGE\_SIZE} \)，即「6 位行内偏移 + 6 位组索引 = 12 位 = 页内偏移位数」，而页内偏移在虚拟↔物理翻译中是不变的。于是同一物理地址在不同虚拟页号下也会落到同一组，避免了缓存「别名（synonym）」问题。这也是 u3-l3 提到的 `L1I_SETS ≤ 64` 约束的来源。

#### 4.3.2 核心流程

```
[标签级] 读标签：
  for way in 0..L1I_WAYS-1:
      ift_tag[way]   = SRAM_tags[way][pc_to_fetch.set_idx]   // 虚拟 set 索引
      ift_valid[way] = line_valid_flop[way][pc_to_fetch.set_idx]
  （若本拍恰好有 L2 回填在写同一组，则走旁路 bypass，用新值）

[数据级] 判命中：
  for way in 0..L1I_WAYS-1:
      way_hit_oh[way] = (ift_pc_paddr.tag == ift_tag[way]) && ift_valid[way]
  cache_hit = |way_hit_oh && ift_tlb_hit

  若 cache_hit：
      读数据 SRAM：地址 = {way_hit_idx, ift_pc_paddr.set_idx}
      从 64 字节缓存行里按 PC 低 6 位取出本拍 4 字节指令
      置 ifd_instruction_valid = 1
  否则（TLB 命中但缓存未命中）：
      置 ifd_cache_miss = 1，把缺失物理地址交给 L2 接口
```

注意一种「近似缺失（near miss）」：本拍 I-Cache 缺失，但这拍恰好有 L2 回填在更新同一组同一 tag 的标签（数据要到下一拍才写进去）。这种情况既不能当命中（数据还没到），也不能当缺失（会重复请求、且唤醒信号本拍就来了会卡死），所以专门用 `ifd_near_miss` 标记，回退 PC 让取指级下一拍重试，那时数据已就位。

#### 4.3.3 源码精读

标签级读各路标签与 valid（每路一份）：

[ifetch_tag_stage.sv:174-218](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L174-L218) 说明：对每一路实例化一个 `sram_1r1w` 存 tag，用 `pc_to_fetch.set_idx`（虚拟索引）做读地址；valid 位用 flop 存（便于复位时整体清零）。其中 [ifetch_tag_stage.sv:209-216](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L209-L216) 是回填旁路：若本拍 L2 正在写「与读取相同的组」，则 `ift_valid` 直接取新值，保证刚回填的行能被立即看到。

数据级逐路比对、判命中：

[ifetch_data_stage.sv:112-128](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_data_stage.sv#L112-L128) 说明：每路 `way_hit_oh[way] = (ift_pc_paddr.tag == ift_tag[way]) && ift_valid[way]`，即「物理 tag 相等且有效」；`cache_hit = |way_hit_oh && ift_tlb_hit`，必须 TLB 也命中才算命中。

近似缺失与真缺失的区分：

[ifetch_data_stage.sv:130-149](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_data_stage.sv#L130-L149) 说明：`ifd_near_miss` 是「未命中 + TLB 命中 + 本拍 L2 正在写同组同 tag」；`ifd_cache_miss` 是「未命中 + TLB 命中 + 已请求取指 + 非近似缺失 + 非回滚撤销」。`ifd_cache_miss_paddr = {tag, set_idx}` 是要向 L2 请求的物理缓存行地址，`ifd_cache_miss_thread_idx` 记录是哪个线程发起的缺失。

命中后读数据 SRAM：

[ifetch_data_stage.sv:155-172](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_data_stage.sv#L155-L172) 说明：`sram_l1i_data` 用「路号 + 组号」做地址，命中时读出整行 512 位；再按 PC 的行内偏移选出本拍 4 字节。注意 [ifetch_data_stage.sv:170-172](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_data_stage.sv#L170-L172) 做了字节序交换（小端拼接），把 SRAM 里的大端字拼回 Nyuzi 的小端指令字。

各类 fault 与性能计数：

[ifetch_data_stage.sv:222-253](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_data_stage.sv#L222-L253) 说明：在非调试、非回滚撤销时，依次判定 `ifd_instruction_valid`、对齐错（PC 低 2 位非 0）、supervisor 错（用户态取了特权页）、TLB 缺失、page fault（页不在内存）、executable fault（页不可执行）；同时累加 `ifd_perf_icache_hit/miss`、`ifd_perf_itlb_miss` 给性能计数器。这些 fault 互斥（代码用多个 `assert` 保证）。

VI/PT 的几何依据可对照类型定义：

[defines.svh:293-301](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L293-L301) 说明：`PAGE_SIZE='h1000`（4 KiB），`CACHE_LINE_BYTES = NUM_VECTOR_LANES*4 = 64`，`CACHE_LINE_OFFSET_WIDTH = 6`，`ICACHE_TAG_BITS = 32 - (6 + log2(L1I_SETS))`。`CACHE_LINE_OFFSET_WIDTH + log2(L1I_SETS) = 6 + 6 = 12 = log2(PAGE_SIZE)`，正好落在页内偏移内。

#### 4.3.4 代码实践

**实践目标**：亲手验证「虚拟索引 + 物理标签」不会产生别名。

**操作步骤**：

1. 读 [defines.svh:330-334](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L330-L334)，确认 `l1i_addr_t = {tag, set_idx, offset}`，其中 `offset` 6 位、`set_idx` 6 位（`$clog2(64)`）。
2. 假设有两个不同虚拟页号 `V1`、`V2` 都映射到同一物理页 `P`（即别名）。
3. 因为 `set_idx` 取自地址的第 [11:6] 位，而 V1、V2 在第 [11:0] 位（页内偏移）上与 P 完全一致（翻译不动页内偏移），所以 V1、V2 会落到**同一个组**。
4. 又因为 tag 取自物理地址 `P`，两份别名会写入同一组的同一 tag——要么复用同一路，要么被 `onehot0` 断言拦下，不会出现「同一物理数据藏在两个不同组里」的别名。

**需要观察的现象 / 预期结果**：你能解释为何 `L1I_SETS` 若超过 64 就会破坏这一不变量（组索引位会越过页内偏移，虚拟/物理的组索引不再恒等，别名随之产生）。这是源码阅读型实践，结论即上述推理。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `cache_hit` 要同时满足 `|way_hit_oh` 和 `ift_tlb_hit`？

**参考答案**：tag 比对用的物理 tag 来自 ITLB 翻译。若 ITLB 未命中，物理地址不可信，比对出的「相等」没有意义，所以必须 TLB 也命中才算缓存命中。TLB 未命中会走另一条路（产生 `ifd_tlb_miss`），不进缓存缺失流程。

**练习 2**：`ifd_near_miss` 为什么不能简单当成 `ifd_cache_miss` 处理？

**参考答案**：回填的标签和数据是分两拍写的（标签先、数据后），本拍标签已更新但数据未到。若当缺失处理，会向 L2 重复请求同一行；同时唤醒信号在本拍就到来，把线程挂起会立刻死锁。所以专门标记 near_miss，只回退 PC 让取指级下一拍重取，那时数据已就位即可命中。

---

### 4.4 缺失唤醒机制

#### 4.4.1 概念说明

取指最常遇到的「慢路径」就是 I-Cache 缺失。Nyuzi 的策略是：**缺失的线程立刻被挂起，不再参与取指仲裁；缺失请求进入 L1↔L2 接口的队列发往 L2；L2 把整行回填进 I-Cache（先写标签、再写数据）后，通过一个「唤醒位图」把对应线程的位清掉，线程重新回到可取指集合，重取同一条指令（这次命中）。**

这套机制让「等内存」不再阻塞流水线——因为其他 3 个线程还能继续取指执行，这正是多线程隐藏延迟的价值（见 u3-l2、u10-l2）。

挂起用一个位图 `icache_wait_threads`（每线程一比特）维护；唤醒用 L2 接口送来的 `l2i_icache_wake_bitmap`。两者在同一拍里做「加入挂起 / 清除唤醒」的集合运算。

#### 4.4.2 核心流程

```
[数据级] 检测到 ifd_cache_miss（线程 T，地址 A）
   -> 把 {T} 传回标签级：ifd_cache_miss_thread_idx, ifd_cache_miss

[标签级] 维护挂起位图：
   cache_miss_thread_oh = onehot(ifd_cache_miss_thread_idx)
   thread_sleep_mask   = cache_miss_thread_oh & {L1I{ifd_cache_miss}}
   icache_wait_threads_next =
        (icache_wait_threads | thread_sleep_mask)   // 新缺失的线程置 1
        & ~l2i_icache_wake_bitmap                    // 被唤醒的线程清 0
   => T 进入等待集合，不再被 rr_arbiter 选中（can_fetch 把它排除）

[L1↔L2 接口] 把缺失请求排队：
   ifd_cache_miss -> l1_load_miss_queue(icache) -> 发 L2 请求
   L2 响应回来：
        - 选定替换路（cache_lru 给出 ift_fill_lru）
        - 写标签：l2i_itag_update_* (valid=1)
        - 写数据：l2i_idata_update_*（晚一拍）
        - 唤醒：l2i_icache_wake_bitmap[T] = 1

[标签级] 下一拍 T 的位被清，T 回到 can_fetch，重取 A（命中）
```

#### 4.4.3 源码精读

挂起位图的集合运算（本讲最核心的一段）：

[ifetch_tag_stage.sv:291-303](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L291-L303) 说明：先把缺失线程号转成独热 `cache_miss_thread_oh`，再与 `ifd_cache_miss` 与一下得到 `thread_sleep_mask_oh`（只有真缺失时才置位）；`icache_wait_threads_nxt` 在「现有等待集合 ∪ 新缺失线程」之后，再用唤醒位图按位清零。注释还强调：即便线程在等待期间发生回滚，也要等这次缺失填完再唤醒，以避免响应随后到达造成的竞态。

把位图写入寄存器，并生成「本拍是否真的请求了取指」：

[ifetch_tag_stage.sv:305-322](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L305-L322) 说明：`icache_wait_threads` 在每拍更新为 `icache_wait_threads_nxt`；`ift_instruction_requested` 仅在「本拍取指 且 选中线程没被判缺失/近似缺失 且 没被同拍回滚」时才置 1，告诉数据级「这次的标签/TLB 结果是有效的，可以用来判命中」。

缺失请求排队与唤醒的 L2 侧：

[l1_l2_interface.sv:211-229](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L211-L229) 说明：为 I-Cache 实例化了一个 `l1_load_miss_queue`，输入是 `ifd_cache_miss / ifd_cache_miss_paddr / ifd_cache_miss_thread_idx`；它在内部排队、向 L2 发请求，并在 L2 响应到达时输出 `wake_bitmap = l2i_icache_wake_bitmap`，这个位图正是上一段用来清挂起位的来源。请求的 `id` 字段（`icache_dequeue_idx`）让 L2 响应能找回当初是哪个 miss 队列条目，从而唤醒正确的线程。

回填时选替换路与写标签：

[l1_l2_interface.sv:242-244](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L242-L244) 说明：当 L2 响应是「本核的 I-Cache 回填」时，置 `l2i_icache_lru_fill_en` 并给出组号；标签级据此调用 `cache_lru` 算出要替换的路 `ift_fill_lru`（见下）。

[ifetch_tag_stage.sv:278-289](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L278-L289) 说明：`cache_lru` 实例在 fill 时返回最久未用的路 `ift_fill_lru` 作为替换目标，并把该路移到「最近使用」位置。

[l1_l2_interface.sv:323-331](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L323-L331) 说明：把替换路、物理 tag、组号、valid=1 通过 `l2i_itag_update_*` 写回 I-Cache 标签；若是指令失效（`L2RSP_IINVALIDATE_ACK`），则把该组所有路置为无效。

回填数据（比标签晚一拍）：

[l1_l2_interface.sv:356-358](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L356-L358) 与 [l1_l2_interface.sv:390](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L390) 说明：`l2i_idata_update_*` 把整行数据连同路号、组号写进 I-Cache 数据 SRAM；它在标签更新后一拍才有效（`l2i_idata_update_en` 由 `icache_update_en` 打一拍寄存器），这正是 4.3 里 near_miss 存在的根因。

伪 LRU 算法（替换路选择）：

[cache_lru.sv:96-113](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/cache_lru.sv#L96-L113) 说明：用一棵树形伪 LRU（4 路用 3 个标志位）记录「指向最久未用路的路径」，比严格 LRU 简单很多，但效果接近。注释还指出 fill 与 access 同拍发生时 fill 优先，以避免「两个线程互相把对方的行反复驱逐」的活锁——这也是 u3-l3 里「`L1I_WAYS/L1D_WAYS ≥ THREADS_PER_CORE`」约束的配套保障。

#### 4.4.4 代码实践

**实践目标**：完整跟踪一次 I-Cache 缺失，定位「缺失信号、缺失地址、挂起、回填、唤醒」分别在哪个文件哪一行产生。

**操作步骤**：

1. **缺失产生**：在 [ifetch_data_stage.sv:143-149](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_data_stage.sv#L143-L149) 找到 `ifd_cache_miss` 与 `ifd_cache_miss_paddr = {ift_pc_paddr.tag, ift_pc_paddr.set_idx}`、`ifd_cache_miss_thread_idx = ift_thread_idx`。这就是缺失信号与缺失（物理）地址的产生位置。
2. **线程挂起**：在 [ifetch_tag_stage.sv:297-303](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L297-L303) 找到 `cache_miss_thread_oh`、`thread_sleep_mask_oh`、`icache_wait_threads_nxt`，确认缺失线程位被置入 `icache_wait_threads`，从而被 `can_fetch_thread_bitmap` 排除（[ifetch_tag_stage.sv:130](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L130)）。
3. **请求排队发往 L2**：在 [l1_l2_interface.sv:211-229](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L211-L229) 找到 `l1_load_miss_queue` 的 icache 实例，确认它消费 `ifd_cache_miss*` 并向 L2 发请求。
4. **回填标签/数据**：在 [l1_l2_interface.sv:325-331](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L325-L331)（标签）与 [l1_l2_interface.sv:390](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L390)（数据）找到 `l2i_itag_update_*` 与 `l2i_idata_update_*`。
5. **唤醒**：在 [ifetch_tag_stage.sv:302-303](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L302-L303) 找到 `& ~l2i_icache_wake_bitmap`，确认唤醒位图把线程位清掉；线程回到 `can_fetch_thread_bitmap`，重取同一条指令（这次命中）。

**需要观察的现象 / 预期结果**：你能画出一张时序图：第 N 拍取指 → 第 N+1 拍判缺失并挂起 → 若干拍后 L2 响应回填标签（第 K 拍）、回填数据（第 K+1 拍）、同拍唤醒 → 第 K+1 拍线程被重新选中、PC 仍是原值 → 第 K+2 拍命中取到指令。本实践为源码阅读型，无需运行；若想验证，可运行一个会让 I-Cache 缺失的小程序并用 `+trace` 或波形观察 `icache_wait_threads` 的位变化。

#### 4.4.5 小练习与答案

**练习 1**：为什么线程被挂起后，其他线程还能继续取指？这依赖哪个信号？

**参考答案**：因为挂起只把该线程的位从 `can_fetch_thread_bitmap` 里去掉（`can_fetch = ts_fetch_en & ~icache_wait_threads`），仲裁器仍能从其余线程里轮转选一个。这依赖 `icache_wait_threads` 位图与 `rr_arbiter` 的配合——这正是多线程隐藏内存延迟的关键。

**练习 2**：如果在「线程正等缺失」期间，该线程又因为分支回滚需要换 PC，会发生什么？

**参考答案**：看 [ifetch_tag_stage.sv:291-296](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L291-L296) 的注释：设计上要求「即便在等待期间发生回滚，也要等这次缺失被 L2 填完再唤醒」。这样避免「回滚后 L2 响应才到、却唤醒了已经不该取那个地址的线程」的竞态。回滚会改写该线程的 `next_program_counter`，但唤醒后它会按新 PC 重新取指。

---

## 5. 综合实践

把本讲四个模块串起来，做一次「端到端取指追踪」。

**任务**：任选一段 Nyuzi 程序（例如 `software/apps/hello_world`），在模拟器里用 `-v`（详细跟踪）或波形中观察最开始几条指令的取指过程，回答下列问题，并尽量在源码里给出依据行号。

1. **PC 来源**：第一条指令的 PC 是多少？它由 [ifetch_tag_stage.sv:155-158](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L155-L158) 的 `RESET_PC` 决定（默认 0）。
2. **翻译**：启动时 MMU 关闭，取指走 [ifetch_tag_stage.sv:267-275](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L267-L275) 的恒等映射，物理地址 = 虚拟地址。
3. **首次取指必然 I-Cache 缺失**：冷启动 I-Cache 全空，所以 `ifd_cache_miss=1`（[ifetch_data_stage.sv:143-147](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_data_stage.sv#L143-L147)），线程 0 进入 `icache_wait_threads`（[ifetch_tag_stage.sv:302-303](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L302-L303)）。
4. **回填与唤醒**：L2 把第 0 行（地址 0 的那一行，64 字节，含 16 条指令）回填，唤醒线程 0；线程 0 重取 PC=0，命中，后续 15 条指令也在同一行里连续命中。
5. **观察连续命中**：从第 2 条到第 16 条指令，`ifd_perf_icache_hit` 持续为 1（[ifetch_data_stage.sv:248](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_data_stage.sv#L248)），直到跨入下一行才再次缺失。

**预期结果**：你能用「一次缺失换来回填后连续 16 次命中」来解释 I-Cache 的命中率来源，并把每一步都对应到具体源码行。若本地无法运行波形，可标注「待本地验证」并仅依据源码完成推理。

## 6. 本讲小结

- 取指分两拍：`ifetch_tag_stage` 选线程、算 PC、读 I-Cache 标签、查 ITLB；`ifetch_data_stage` 判命中、读数据、产生缺失信号与各类 fault。
- PC 由「reset / 回滚 / 缺失回退 −4 / 正常 +4」四级优先级维护，缺失时把误加的 4 扣回，保证唤醒后重取同一条指令。
- ITLB 是软件管理的：硬件只查表与报缺失，MMU 关闭时取指标签级硬连线成恒等映射，所以裸机程序无需 TLB 即可运行。
- I-Cache 是「虚拟索引 / 物理标签」：靠 `L1I_SETS × CACHE_LINE_BYTES = PAGE_SIZE` 让组索引落在页内偏移内，天然避免别名。
- 缺失用 `icache_wait_threads` 位图挂起线程、用 `l1_load_miss_queue` 排队发往 L2、用 `l2i_icache_wake_bitmap` 唤醒；标签与数据分两拍回填，由此引出 near_miss 特例。
- 多线程让「等内存」不阻塞流水线：一个线程等缺失时，其余线程仍被 `rr_arbiter` 轮选取指。

## 7. 下一步学习建议

- **指令解码**：取指送出的 `ifd_instruction` 下一拍进入解码级，下一讲 u4-l2（指令解码）讲 32 位指令如何被填成 `decoded_instruction_t`。
- **线程选择与记分牌**：本讲的 `ts_fetch_en` 来自 `thread_select_stage`，u4-l3 会讲它如何用记分牌规避数据冒险、并与本讲的取指仲裁配合。
- **TLB 与虚拟内存**：本讲只用了 ITLB 的查询接口，完整的「软件管理 TLB、缺失 trap、`CACHE_DTLB_INSERT` 填表」在 u7-l1。
- **L1↔L2 接口与 L2 缓存**：本讲的缺失请求经 `l1_l2_interface` 进入 L2，u6-l2 / u6-l3 会讲 miss 队列、store 队列与 L2 四阶段流水线的细节。
