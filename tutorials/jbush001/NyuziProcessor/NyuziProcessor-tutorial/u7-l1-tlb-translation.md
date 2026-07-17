# 软件管理 TLB 与地址翻译

## 1. 本讲目标

本讲是「虚拟内存、TLB 与异常」单元的第一讲，聚焦于 Nyuzi 如何把**虚拟地址翻译成物理地址**。

学完后你应当能够：

- 说出 `tlb_entry_t` 表项里每一个标志位的含义，以及它在 SRAM 里是如何存放的。
- 描述一次 TLB 查询的两级流水过程，以及「命中 / 缺失」是如何判定的。
- 追踪一次 DTLB 缺失：从 `dcache` 检测到 `!dt_tlb_hit`，到产生 `TT_TLB_MISS` trap，到写回级把 PC 回滚到 TLB miss handler，再到软件用 `CACHE_DTLB_INSERT` 插入表项并 `eret` 返回重试访问的完整链路。
- 理解 ASID（地址空间标识）与全局映射（global）如何配合页目录，让多个进程共享同一个 TLB 而互不干扰。

本讲依赖 u6-l1（L1 数据缓存的虚拟索引 / 物理标签结构）。如果你还不清楚「为什么 L1D 的组数必须 ≤ 64」，建议先回顾那一讲。

## 2. 前置知识

### 虚拟地址与物理地址

程序使用的是**虚拟地址**（virtual address），而内存芯片只认**物理地址**（physical address）。两者之间需要一张「翻译表」。Nyuzi 是 32 位机，采用 4 KiB 页：

- 页内偏移（page offset）占低位 12 位。
- 虚拟页号（virtual page index）占高位 20 位。

即一个 32 位虚拟地址被切成两段：

\[ \text{虚拟地址} = \underbrace{\text{虚拟页号 (20 位)}}_{\text{page\_index\_t}} \,\|\, \underbrace{\text{页内偏移 (12 位)}}_{\text{offset}} \]

翻译只替换「页号」这一段，页内偏移原样保留。

### 什么是 TLB

页表通常很大、放在主存里。如果每次访存都要先去主存读页表，速度会慢一两个数量级。**TLB（Translation Lookaside Buffer，地址翻译旁路缓冲）** 就是页表项的一个小而快的硬件缓存：它记住「最近用过的虚拟页号 → 物理页号」映射，让翻译在流水线里一拍完成。

### 软件管理 TLB（software-managed TLB）

这是本讲最关键的设计取舍。不同体系结构处理 TLB 缺失的方式不同：

| 模型 | 谁来填充 TLB | 代表架构 |
|---|---|---|
| 硬件页表漫游（hardware page walker） | 硬件自动读多级页表并填充 | x86、ARM |
| 软件管理 TLB | 硬件只报缺失 trap，由操作系统读页表、执行专门指令把表项塞进 TLB | MIPS、Nyuzi |

Nyuzi 选择**软件管理**模型：TLB 硬件只负责「查」和「按命令改」，不会自己去主存里走页表。一旦查不到，它就抛出一个 `TT_TLB_MISS` 异常，剩下的工作（读页目录、读页表、组装表项、插入 TLB）全部由内核里的汇编 handler 完成。这样做的好处是硬件简单、页表格式完全由软件决定；代价是每一次 TLB 缺失都要付出一次 trap + 软件漫游的代价。

> 如果你学过 u2-l4，就知道 `eret` 用于从 trap 返回；本讲会反复用到它。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|---|---|
| `hardware/core/tlb.sv` | TLB 硬件本体：两级流水的组相联查找 / 更新 / 失效。ITLB 和 DTLB 都用它实例化。 |
| `hardware/core/defines.svh` | 定义 `tlb_entry_t` 表项结构、`page_index_t`、`PAGE_SIZE`/`ASID_WIDTH`、TLB 相关 cache 操作码 `cache_op_t` 与 trap 类型 `trap_type_t`、控制寄存器编号。 |
| `hardware/core/dcache_tag_stage.sv` | L1D 标签级：实例化 DTLB、发起查询、判定命中，并把 `dt_tlb_hit` 等结果交给下一级。 |
| `hardware/core/dcache_data_stage.sv` | L1D 数据级：根据 `dt_tlb_hit` 判定 `tlb_miss`，并把它翻译成 `TT_TLB_MISS` trap。 |
| `hardware/core/writeback_stage.sv` | 写回级：把 TLB miss trap 转成 PC 回滚，目标指向 `CR_TLB_MISS_HANDLER`。 |
| `hardware/core/control_registers.sv` | 控制寄存器：维护每线程 ASID、页目录基址、TLB miss handler 地址。 |
| `hardware/core/ifetch_tag_stage.sv` | 取指标签级：用同一个 `tlb` 模块实例化 ITLB。 |
| `hardware/core/config.svh` | 配置 TLB 表项数与相联度。 |
| `software/kernel/trap_entry.S` | 内核里的 TLB miss handler：手工走页表并执行 `dtlbinsert` / `itlbinsert`。 |
| `software/kernel/asm.h` | 内核侧的控制寄存器编号与 trap 类型常量（与硬件一一对应）。 |
| `tools/emulator/instruction-set.h` | 模拟器侧的 ISA 定义，与硬件共用同一套编码。 |

## 4. 核心概念与源码讲解

### 4.1 TLB 表项

#### 4.1.1 概念说明

TLB 是一个小的**组相联缓存**。它存放的每一行（表项）记录一条翻译：

- **虚拟页号**（要翻译谁）；
- **物理页号**（翻译成谁）；
- 一组**权限 / 属性位**（present、writable、executable、supervisor、global）；
- 一个 **ASID**（这条翻译属于哪个地址空间）。

在 Nyuzi 里，软件要往 TLB 里塞东西时，不是逐字段写入，而是**把整条表项打包成一个 32 位标量**，作为一条「缓存控制指令」的 store 值一次性提交。这个打包格式就是 `tlb_entry_t`。

#### 4.1.2 核心流程

`tlb` 模块是一个参数化的组相联结构：

- 容量 `NUM_ENTRIES`（默认 64），相联度 `NUM_WAYS`（默认 4），于是组数 `NUM_SETS = 16`。
- 它有三种操作，由输入信号二选一/三选一（同一周期只能做一件，有断言保护）：
  1. **lookup（查询）**：`lookup_en`，给一个虚拟页号 + ASID，下一周期返回是否命中、物理页号和权限位。
  2. **update（插入 / 更新）**：`update_en`，写入一条新表项；若该组已有同虚拟页号 + 同 ASID 的表项就覆盖它，否则按轮询指针挑一路写入。
  3. **invalidate（失效）**：`invalidate_en` 清掉命中的那一路；`invalidate_all_en` 清空所有表项。

模块内部是两级流水：**Stage 1 用虚拟页号的低位选组、并行读各路 SRAM**；**Stage 2 用锁存的请求做命中比对并输出结果**。这种「本周期给地址、下周期出结果」的一拍延迟，恰好和 L1D 标签 SRAM 的读取对齐，可以并行进行（详见 u6-l1）。

#### 4.1.3 源码精读

先看模块的端口，注意它对 ITLB 和 DTLB 是中性的——`exe_writable` 对指令缓存意味着「可执行」，对数据缓存意味着「可写」：

[hardware/core/tlb.sv:26-52](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/tlb.sv#L26-L52) —— `tlb` 模块的命令 / 响应端口：`lookup_en`/`update_en`/`invalidate_en`/`invalidate_all_en` 四种操作，请求带虚拟页号与 ASID，响应带物理页号与四个权限位。

再看表项打包格式。软件把整条表项塞进一个标量寄存器，其位域由 `tlb_entry_t` 定义：

[hardware/core/defines.svh:306-314](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L306-L314) —— `tlb_entry_t`：`ppage_idx`（物理页号）+ `unused` 填充 + `global_map`/`supervisor`/`executable`/`writable`/`present` 五个属性位。软件组装出这个 32 位值后，通过缓存控制指令提交。

辅助常量（页大小、页号位宽、ASID 宽度）在这里定义：

[hardware/core/defines.svh:293-304](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L293-L304) —— `PAGE_SIZE = 'h1000`（4 KiB），`PAGE_NUM_BITS = 32 - 12 = 20`（页号位宽），`ASID_WIDTH = 8`，以及 `page_index_t`（20 位页号类型）。

表项的存储用一块 `sram_1r1w`，每个 way 一块。注意它的数据宽度把「虚拟页号 + ASID + 物理页号 + 4 个属性位」打包在一起：

[hardware/core/tlb.sv:96-119](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/tlb.sv#L96-L119) —— 每 way 一块 SRAM，数据宽度 `PAGE_NUM_BITS*2 + 4 + ASID_WIDTH`，把虚拟页号、ASID、物理页号、present/exe_writable/supervisor/global 一起存。读端口用请求组号索引，写端口在更新时写入。

配置参数（容量、相联度）在 `config.svh` 里，ITLB 与 DTLB 各 64 项、4 路：

[hardware/core/config.svh:49-51](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh#L49-L51) —— `ITLB_ENTRIES = 64`、`DTLB_ENTRIES = 64`、`TLB_WAYS = 4`，故 ITLB/DTLB 各为 16 组 × 4 路的组相联结构。

> 小细节：TLB 的**组索引用的是虚拟页号的低位**（`request_vpage_idx[SET_INDEX_WIDTH-1:0]`），而不是字节地址——因为 TLB 表项本身就是按页粒度组织的。这与 L1D 用虚拟地址低位索引是两回事。

#### 4.1.4 代码实践

**目标**：弄清一条 TLB 表项在内存里到底长什么样，为后面理解软件 handler 做铺垫。

**步骤**：

1. 打开 `hardware/core/defines.svh` 第 306–314 行，把 `tlb_entry_t` 的每个字段从高位到低位列出来，算出它占多少位。
2. 打开 `tools/emulator/instruction-set.h`，找到模拟器侧的等价定义：

   [tools/emulator/instruction-set.h:23-27](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/instruction-set.h#L23-L27) —— 模拟器用一组位掩码（`TLB_PRESENT=1`、`TLB_WRITE_ENABLE=2`、`TLB_EXECUTABLE=4`、`TLB_SUPERVISOR=8`、`TLB_GLOBAL=16`）描述同一个表项，与硬件 `tlb_entry_t` 完全对应，这是协同仿真的基础。

3. 对照两份定义，回答：物理页号放在哪几位？`present` 位是最低位吗？

**预期结果**：你能画出一张 32 位的位域图，标注出 `ppage_idx`、五个属性位和填充位的位置，并确认硬件与模拟器两套定义指向同一种布局。

#### 4.1.5 小练习与答案

**练习 1**：`tlb_entry_t` 里的 `unused` 字段宽度是多少？为什么需要它？

**答案**：`PAGE_NUM_BITS = 20`，故 `unused` 宽度为 `32 - (20 + 5) - 1 = 6` 位。它存在是为了把 `ppage_idx` 和五个属性位凑齐放进一个 32 位标量寄存器（`store_value`），让软件能一次提交整条表项。

**练习 2**：如果把 `DTLB_ENTRIES` 从 64 改成 128，`NUM_SETS` 会变成多少？需要同时满足什么约束？

**答案**：`NUM_WAYS = 4` 不变时，`NUM_SETS = 128 / 4 = 32`，组索引位宽从 4 变成 5。约束是表项数和路数都必须让 `NUM_ENTRIES / NUM_WAYS` 为 2 的幂（因为 SRAM 地址宽度由 `$clog2` 决定），且这是编译期参数，改完要重新 `make`。

---

### 4.2 查询与缺失

#### 4.2.1 概念说明

TLB 的查询发生在 L1 数据缓存的**标签级**（`dcache_tag_stage`）和指令缓存的**标签级**（`ifetch_tag_stage`）。它们用虚拟页号去查 DTLB/ITLB，拿到物理页号后再去比对缓存标签（详见 u6-l1 的「虚拟索引 / 物理标签」）。

查询的结果只有两种走向：

- **命中**：拿到物理页号和权限位，访存正常进行。
- **缺失**：硬件**不会**自己去走页表，而是把这次访存标记为发生了 `TT_TLB_MISS` trap，让流水线回滚到 TLB miss handler，由软件来填表项。

注意区分两个概念：**TLB 缺失（`TT_TLB_MISS`）** 表示「TLB 里没有这条翻译，请软件补上」；**页缺失（`TT_PAGE_FAULT`）** 表示「TLB 命中了，但表项的 `present` 位为 0，即这一页根本没映射」。两者的处理路径不同。

#### 4.2.2 核心流程

一次 DTLB 缺失的全链路：

```text
dcache_tag_stage          用虚拟页号查 DTLB（与 L1D 标签并行读取）
        │  下一周期产出 dt_tlb_hit = 0
        ▼
dcache_data_stage         tlb_miss = tlb_read && !dt_tlb_hit
        │  组装 dd_trap_cause = {1'b1, store?, TT_TLB_MISS}
        │  dd_trap = any_fault || tlb_miss
        ▼
writeback_stage           看到 dd_trap 且类型为 TT_TLB_MISS
        │  wb_rollback_pc = cr_tlb_miss_handler   ← 不走通用 trap handler
        │  wb_rollback_en = 1   刷新流水线
        ▼
取指级                    从 TLB miss handler 重新取指（软件接管）
        │  软件：读页目录 → 读页表 → 组装 tlb_entry_t → dtlbinsert
        │  eret
        ▼
取指级                    回到触发缺失的那条指令重新执行，这次 DTLB 命中
```

关键点：TLB miss 是**精确异常**。回滚时，`wb_trap_pc` 记录了触发缺失的指令地址，`eret` 之后 CPU 会重新执行这条指令——这次表项已经填好，就能正常访问了。

#### 4.2.3 源码精读

**(1) TLB 内部的命中判定。** 每 way 独立比较，再 OR 起来：

[hardware/core/tlb.sv:150-153](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/tlb.sv#L150-L153) —— 单路命中条件：表项有效 && 虚拟页号相等 && (ASID 相等 || 全局映射)。三个条件必须同时满足。

[hardware/core/tlb.sv:189-207](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/tlb.sv#L189-L207) —— `lookup_hit = |way_hit_oh`（任意一路命中即命中）；用「使能多路 OR」而非优先编码器选出命中路的物理页号和权限位。

**(2) DTLB 在 `dcache_tag_stage` 里的实例化与查询发起。**

[hardware/core/dcache_tag_stage.sv:152-156](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L152-L156) —— `tlb_lookup_en`：当指令是真正的内存访问（非控制寄存器访问、且不是本周期正在插表项 / 失效）时才发起查询。

[hardware/core/dcache_tag_stage.sv:233-253](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L233-L253) —— DTLB 实例化：请求虚拟页号取自 `request_addr_nxt[31-:PAGE_NUM_BITS]`，请求 ASID 取自当前线程的 `cr_current_asid`，更新数据来自软件提交的 `new_tlb_value`。

**(3) MMU 关闭时的恒等映射。** 裸机程序（u1-l4 的 hello_world）不开 MMU，这时 TLB 查询被完全旁路：

[hardware/core/dcache_tag_stage.sv:258-276](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L258-L276) —— 当 `cr_mmu_en` 为 0 时，直接令 `dt_tlb_hit=1`、`dt_tlb_present=1`、物理页号 = 虚拟页号（恒等映射），所以不开 MMU 时永远不会有 TLB miss。

**(4) 在数据级把缺失翻译成 trap。**

[hardware/core/dcache_data_stage.sv:253](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L253) —— `tlb_miss = tlb_read && !dt_tlb_hit`：只要这次访问需要翻译且没命中，就是缺失。

[hardware/core/dcache_data_stage.sv:555-569](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L555-L569) —— trap 优先级仲裁：**TLB miss 排在最前**（因为缺失时权限位无效，必须先判 miss），其次才是 page fault、supervisor、对齐等。

[hardware/core/dcache_data_stage.sv:611](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L611) —— `dd_trap <= any_fault || tlb_miss`：把缺失信号送往写回级。

**(5) 写回级把 TLB miss 路由到专用 handler。**

[hardware/core/writeback_stage.sv:200-214](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L200-L214) —— 关键分支：如果 `dd_trap_cause.trap_type == TT_TLB_MISS`，回滚目标是 `cr_tlb_miss_handler`（而不是通用的 `cr_trap_handler`）；同时记录 `wb_trap_pc`（触发指令地址）和 `wb_trap_access_vaddr`（触发访问的虚拟地址）。

**(6) 软件侧：TLB miss handler。**

[software/kernel/trap_entry.S:197-236](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap_entry.S#L197-L236) —— `tlb_miss_handler`：先把 s0/s1 存进 scratchpad 寄存器；从 `CR_TRAP_ADDR` 取触发虚拟地址，按「页目录 → 页表」两级手工查；读出 PTE 后，用 `dtlbinsert`/`itlbinsert` 插入 TLB，最后 `eret` 返回。

> handler 里 `getcr s1, CR_TRAP_CAUSE; and s1, s1, 0x20` 是在判断这次缺失是 DTLB 还是 ITLB（`trap_cause` 的 bit5 区分），从而决定插哪个 TLB。

#### 4.2.4 代码实践

**目标**：把「一次 DTLB 缺失如何变成 `TT_TLB_MISS` trap、跳到 handler、软件插表项后返回重试」的全过程走一遍。

**步骤**：

1. 从 `dcache_data_stage.sv:253` 出发，确认 `tlb_miss` 的定义；再跳到第 555–569 行，确认 TLB miss 是 trap 优先级里的第一名。
2. 跟着 `dd_trap`（第 611 行）进入 `writeback_stage.sv:200`，确认对 `TT_TLB_MISS` 特殊处理：回滚 PC 取自 `cr_tlb_miss_handler`。
3. 打开 `software/kernel/trap_entry.S` 的 `tlb_miss_handler`（第 197 行起），逐行读懂它如何用 `CR_TRAP_ADDR` + `CR_PAGE_DIR_BASE` 走两级页表。
4. 找到 `update_tlb:` 标签（第 223 行），看清它如何根据 `trap_cause` 的 bit5 在 `fill_dltb`（`dtlbinsert`）和 `fill_itlb`（`itlbinsert`）之间二选一。
5. **可选运行**：若你已按 u1-l2 构建好环境，构建并运行内核（`software/kernel`），用模拟器 `-v` 跟踪，搜索首次出现的 TLB miss trap 与随后的 `eret`。运行结果**待本地验证**。

**需要观察的现象**：

- 硬件侧：一次普通 load 指令在 DTLB 未命中时，`dt_tlb_hit` 为 0，写回级产生 `TT_TLB_MISS` 并回滚到 `cr_tlb_miss_handler`。
- 软件侧：handler 读到 PTE 后执行 `dtlbinsert`，`eret` 后 CPU 重新执行同一条 load，这次命中。
- 因此同一条 load 指令会在 trace 里出现两次（第一次触发缺失，第二次成功）。

**预期结果**：你能画出 4.2.2 节那张流程图，并标出硬件负责哪些步骤（查 TLB、判缺失、回滚）、软件负责哪些步骤（走页表、插表项、返回）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 trap 优先级里 `TT_TLB_MISS` 必须排在 `TT_PAGE_FAULT` 前面？

**答案**：因为一旦 TLB 缺失，表项里的 `present` 等权限位是**无效的随机值**（SRAM 里那一路根本没命中）。如果先判 page fault，会拿无效的 `present` 位误判。所以必须先确认「命中」了，才能信任权限位；缺失时直接报 `TT_TLB_MISS` 让软件补表项。

**练习 2**：裸机的 hello_world（u1-l4）从不配置页表，为什么不会触发 TLB miss？

**答案**：因为它没有置位 `CR_FLAGS` 里的 `FLAG_MMU_EN`。`dcache_tag_stage` 在 `cr_mmu_en == 0` 时直接旁路 TLB（`dt_tlb_hit=1`、物理页号 = 虚拟页号），恒等映射，自然永远不缺失。

---

### 4.3 ASID 与全局映射

#### 4.3.1 概念说明

TLB 是全核共享的小缓存。当多个进程（地址空间）交替运行时，它们的虚拟页号会重叠：进程 A 的虚拟页 0x00100 和进程 B 的虚拟页 0x00100 指向完全不同的物理页。如果 TLB 不区分它们，切换进程时就必须把整个 TLB 清空，代价很大。

**ASID（Address Space ID，地址空间标识）** 解决这个问题：每条 TLB 表项在插入时都打上「当时所在地址空间」的 ASID 标签，查询时要求表项的 ASID 与当前进程的 ASID 相等才算命中。这样多个进程的表项可以共存在 TLB 里，切换进程只需换一个 ASID（写 `CR_CURRENT_ASID`），不必刷 TLB。

**全局映射（global）** 是 ASID 的例外：内核代码、内核数据这种所有进程共享的页，被打上 `global_map` 位，查询时**无论 ASID 是多少都算命中**。这样内核页只需在 TLB 里存一份。

Nyuzi 的 ASID 宽度是 8 位，但内核常量定义 `MAX_ASIDS = 64`（见 asm.h），即软件层面最多管理 64 个地址空间。

#### 4.3.2 核心流程

ASID 在三个环节起作用：

1. **维护**：每个硬件线程有自己的当前 ASID，存在 `control_registers` 模块里（`cr_current_asid[thread]`），由内核在切换地址空间时写 `CR_CURRENT_ASID`。
2. **查询**：`dcache_tag_stage` / `ifetch_tag_stage` 把当前线程的 ASID 一起送给 TLB；TLB 在命中判定里比较 ASID（或接受 global 表项）。
3. **插入**：插入表项时，TLB 自动把「当时的请求 ASID」连同表项一起写进 SRAM。

页目录（page directory）则与 ASID 配合提供「从哪开始走页表」的入口：内核把每个地址空间的页目录物理地址写进 `CR_PAGE_DIR`，TLB miss handler 读它来定位第一级页表。

#### 4.3.3 源码精读

**(1) 命中判定里的 ASID / global 比较。** 这是最核心的一行逻辑：

[hardware/core/tlb.sv:150-153](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/tlb.sv#L150-L153) —— 命中要求 `way_asid == request_asid || way_global`：要么 ASID 匹配，要么这条是全局页。第三个条件 `update_en_latched && update_global_latched` 是一个边角：在「插入一条 global 表项」的同一拍，让并发的查询也能命中它。

**(2) 查询时把当前线程 ASID 送给 TLB。**

[hardware/core/dcache_tag_stage.sv:233-247](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_tag_stage.sv#L233-L247) —— `.request_asid(cr_current_asid[of_thread_idx])`：DTLB 查询用的是「发起访问那个线程」的 ASID；`.update_global(new_tlb_value.global_map)` 把软件组装的 global 位写入。

**(3) 每线程独立 ASID 的维护。**

[hardware/core/control_registers.sv:199-204](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L199-L204) —— 写 `CR_CURRENT_ASID` 更新当前线程的 ASID，写 `CR_PAGE_DIR` 更新当前线程的页目录基址；二者都是按线程分体的。

[hardware/core/control_registers.sv:41](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L41) —— `cr_current_asid[THREADS_PER_CORE]`：输出是按线程索引的数组，每个硬件线程一个 ASID。

**(4) 内核侧对应的常量与 handler 用法。**

[software/kernel/asm.h:27-30](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/asm.h#L27-L30) —— 内核侧定义 `CR_TLB_MISS_HANDLER = 7`、`CR_CURRENT_ASID = 9`、`CR_PAGE_DIR_BASE = 10`，与硬件 `control_register_t`（`CR_PAGE_DIR = 10`）一一对应。

[software/kernel/trap_entry.S:201-220](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap_entry.S#L201-L220) —— handler 用 `CR_PAGE_DIR_BASE` 找到当前地址空间的页目录物理地址，再按虚拟地址的高 10 位 / 中 10 位走两级页表。注意它不查 ASID——ASID 只在 TLB 查询时用，软件走页表时页目录本身已经隐含了地址空间。

**(5) ITLB 与 DTLB 共用同一套 ASID 机制。**

[hardware/core/ifetch_tag_stage.sv:226-247](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L226-L247) —— ITLB 用同一个 `tlb` 模块实例化，查询时同样带 `cr_current_asid`；插入 ITLB 表项的信号（`dt_update_itlb_*`）由 `dcache_tag_stage` 转发过来（因为缓存控制指令都走数据通路）。

#### 4.3.4 代码实践

**目标**：验证「切进程只换 ASID、不刷 TLB」是如何落到代码里的。

**步骤**：

1. 在 `tlb.sv:150-153` 确认：两条虚拟页号相同、但 ASID 不同的表项，可以同时存在 TLB 的不同路里而互不命中。
2. 在 `control_registers.sv` 找到 `CR_CURRENT_ASID` 的写路径（第 203 行），确认它是「按当前线程」更新的——这意味着同一核上的不同硬件线程可以属于不同地址空间。
3. 思考：内核页（global）为什么不需要在每次切进程时重新插入？结合命中条件里 `way_global` 那一项回答。
4. **可选**：阅读内核里切换地址空间的代码（如 `vm_address_space.c` 中设置 `CR_CURRENT_ASID` 与 `CR_PAGE_DIR` 的地方），看它是否在切进程时调用了 `CACHE_TLB_INVAL_ALL`。运行结果**待本地验证**。

**预期结果**：你能解释清楚——切换进程只需写 `CR_CURRENT_ASID`，TLB 里旧进程的非全局表项因 ASID 不匹配自动「失效」（查询不命中），而 global 表项继续有效；只有当 ASID 这 8 位被复用（64 个地址空间轮满）时，软件才需要主动刷 TLB。

#### 4.3.5 小练习与答案

**练习 1**：设进程 A（ASID=1）和进程 B（ASID=2）都映射了虚拟页 `0x12345`，但物理页不同。这两条表项能在 TLB 里共存吗？查询时会不会串？

**答案**：能共存。它们会落在同一个组（组索引来自虚拟页号低位）、不同路。查询进程 A 时，`request_asid=1`，只有 ASID=1 那一路命中；进程 B 的那一路因 `way_asid(2) != request_asid(1)` 且非 global 而不命中。不会串。

**练习 2**：什么情况下软件必须执行 `CACHE_TLB_INVAL_ALL`（清空整个 TLB）？

**答案**：当 ASID 被复用时——即一个旧的地址空间退出，它的 ASID 被分配给新的地址空间，此时 TLB 里残留的旧 ASID 表项会被误判为属于新进程。所以在重新分配一个已用过的 ASID 之前，软件要清空 TLB（至少清掉该 ASID 的表项，但 Nyuzi 的 `CACHE_TLB_INVAL` 是按虚拟地址失效，故实践中常用 `INVAL_ALL`）。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「端到端」的 TLB 缺失分析。

**任务**：假设一个用户态程序执行了 `load_32 s0, (s1)`，而 `s1` 指向的虚拟页在 DTLB 里没有表项、但页表里确实存在合法映射。请画出从这条 load 第一次执行失败、到第二次执行成功的**完整时序图**，要求：

1. 标出**硬件**承担的每一步，并给出对应的源码位置（`dcache_tag_stage` 查询 → `dcache_data_stage` 判缺失、组 trap → `writeback_stage` 回滚到 `cr_tlb_miss_handler`）。
2. 标出**软件** handler 承担的每一步（读 `CR_TRAP_ADDR` / `CR_PAGE_DIR_BASE` → 走两级页表 → 组装 `tlb_entry_t` → `dtlbinsert` → `eret`），对应 `trap_entry.S` 的行号。
3. 在图上标注 ASID 在哪两处被使用（查询时、插入时），以及为什么这条映射**不是** global。
4. 解释为什么这条 load 会在 trace 里出现两次，且第二次不再触发 trap。

**进阶（可选）**：对比 ITLB 缺失与 DTLB 缺失在处理上的两点差异——（a）trap 怎么区分两者（`trap_cause` 的 bit5）；（b）取指级发生 ITLB miss 时，回滚的「指令地址」是那条未取到的指令本身的地址（见 `writeback_stage` 对 `ix` 路径的处理 `wb_trap_access_vaddr = ix_instruction.pc`）。

> 运行验证**待本地环境**：若已构建内核与模拟器，可运行一个启用 MMU 的程序并用 `-v` 抓取 trace，核对你的时序图与实际事件是否一致。

## 6. 本讲小结

- Nyuzi 采用**软件管理 TLB**：硬件只负责查、改、失效，缺失时抛 `TT_TLB_MISS` trap，由内核 handler 走页表并插入表项。
- TLB 表项格式是硬件 / 模拟器 / 软件三方共享的 `tlb_entry_t`（物理页号 + present/writable/executable/supervisor/global 五个属性位），软件把它打包成一个标量一次性提交。
- `tlb.sv` 是一个 16 组 × 4 路的组相联结构，两级流水：本周期给虚拟页号 + ASID 选组读 SRAM，下周期做命中比对（页号相等 && (ASID 相等 || global)）。
- DTLB / ITLB 用同一个 `tlb` 模块实例化；查询在 `dcache_tag_stage` / `ifetch_tag_stage` 与缓存标签读取并行进行；MMU 关闭时旁路成恒等映射。
- 缺失经 `dcache_data_stage`（判 `tlb_miss`、组 `TT_TLB_MISS` trap）→ `writeback_stage`（回滚到 `CR_TLB_MISS_HANDLER`）→ 软件 handler（`trap_entry.S`）→ `eret` 重试，是精确异常。
- ASID 让多地址空间共享 TLB 而不互相干扰，global 表项跨地址空间命中；切进程只换 `CR_CURRENT_ASID`，ASID 复用时才需刷 TLB。

## 7. 下一步学习建议

- **下一步讲义 u7-l2（控制寄存器与中断）**：本讲多次提到 `CR_FLAGS`、`CR_CURRENT_ASID`、`CR_TLB_MISS_HANDLER`，下一讲会系统地讲控制寄存器存储、trap 级别嵌套与中断时序，帮你补齐「TLB miss trap 与普通 trap / 中断如何排队」的全貌。
- **u7-l3（Trap 处理与回滚）**：如果你对 `writeback_stage` 如何实现精确异常、`TT_PAGE_FAULT` 与 `TT_TLB_MISS` 的区别感兴趣，下一讲会深入回滚机制。
- **继续阅读源码**：想看清软件侧完整页表格式与缺页处理，可直接读 `software/kernel/trap_entry.S`（TLB miss handler）与 `software/kernel/vm_address_space.c`（`handle_page_fault`，处理 `present=0` 的真正页缺失）。
