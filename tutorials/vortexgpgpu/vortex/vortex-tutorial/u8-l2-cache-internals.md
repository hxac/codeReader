# 缓存标签、MSHR、替换与数据通路

## 1. 本讲目标

上一讲（u8-l1）我们从外部看清了 Vortex 的内存层次：LSU→L1→L2→L3→DRAM 这条梯子，以及「层次即共享边界」「line/sector/word 三种粒度解耦」等总原则。本讲要**钻进单级缓存的内部**，把一个 cache 拆成零件，并在 RTL（`hw/rtl/cache/`）与 SimX（`sim/simx/mem/cache.cpp`）两套实现之间逐项对照。

学完后你应当能够：

- 说清一个 Vortex cache 的数据通路：请求如何经 crossbar 分发到 bank、bank 内部流水线如何串起 tags / MSHR / data array、miss 如何变成 fill 请求再回到 bank。
- 解释 **tag store** 如何用单数组 BRAM 完成 line/sector 级命中判定，以及「read-first 旁路」为何能保证 fill 后紧跟的 replay 不出错。
- 解释 **MSHR** 如何用「链式合并（coalescing）」让同一行的多个 miss 共享一次 fill，并在 RTL 与 SimX 中分别如何承载未命中请求与 line 数据。
- 区分 **FIFO / PLRU / Random** 三种替换策略在两套实现里的具体编码。
- 对比 RTL 与 SimX 的 cache 实现，理解它们为何能在 `model_parity` 下保持一致。

## 2. 前置知识

- **set / way / line / sector / word**：cache 用地址位分成「组（set）」、组内多「路（way）」；一个 tag 覆盖一条「行（line）」；行再切成若干「扇区（sector）」，sector 是 fill/eviction/访存的最小单位；「字（word）」是一次请求的粒度。u8-l1 已讲过 Vortex 把这三种粒度解耦。
- **命中与未命中（hit / miss）**：tag 匹配且对应 sector 有效 → 命中；否则未命中，需要向下一级发 `fill` 请求把 sector 取回来。
- **MSHR（Miss Status Holding Register，又叫 Miss Handling Queue）**：保存「正在等 fill 回来」的未命中请求的表项。有了它，cache 在一个 miss 还没回来时就能继续服务别的请求（**非阻塞**），而不是停下来干等。
- **合并 / 链式（coalescing / chaining）**：多个 miss 落在同一行（Vortex 里更精确：同一 set/tag/sector）时，不重复发 fill，而是「挂」在第一个 miss 的表项上，等同一个 fill 回来一起服务。
- **fill forwarding（填充前递）**：fill 数据回来时，不等它写回数据阵列再读，而是直接拿它服务挂在 MSHR 上的等待者，省掉「写完再读」的一来回。
- **write-through / write-back**：写命中时，写穿透（wt）每笔都同步写到下一级；写回（wb）只在 cache 里改、打脏位，驱逐时才写回。u8-l1 讲过 Vortex 按一致性角色自动推导。
- **`shared_ptr<mem_block_t>`**：SimX 里一条 sector 的数据用一个引用计数智能指针承载，多处在途请求可以共享同一份 line 数据而无需拷贝（u5-l3 已建立这条认知）。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| `hw/rtl/cache/VX_cache.sv` | RTL **顶层**：把 NUM_REQS 个核心请求口经 crossbar 分发到 NUM_BANKS 个 bank，再经 crossbar/仲裁汇聚到 MEM_PORTS 个访存口。 |
| `hw/rtl/cache/VX_cache_bank.sv` | RTL **单 bank 流水线**：输入仲裁 → S0 查找（repl+tags+MSHR 分配）→ S1（MSHR finalize）→ 数据阵列 → stC 提交（响应/访存请求）。 |
| `hw/rtl/cache/VX_cache_tags.sv` | RTL **标签存储**：单数组 BRAM，per-way 写使能，line/sector 级命中判定 + read-first 旁路。 |
| `hw/rtl/cache/VX_cache_mshr.sv` | RTL **MSHR**：链式合并、allocate/finalize/fill/dequeue 四阶段、AMO 不合并。 |
| `hw/rtl/cache/VX_cache_repl.sv` | RTL **替换策略**：PLRU（树形）/ FIFO（计数器）/ Random（LFSR）三选一。 |
| `sim/simx/mem/cache.cpp` | SimX **cache 全部**：`Cache::Impl` 做拓扑（crossbar+仲裁+bank），`CacheBank` 做 bank 流水线，`MSHR` 做未命中合并，`set_t` 做 tags/替换。 |
| `sim/simx/mem/cache.h` | SimX cache 的 `Config`（C/L/S/W/A/B 等几何参数）与 `ReplPolicy` 枚举。 |
| `docs/designs/cache_subsystem.md` | 缓存子系统设计文档，是本讲的权威说明。 |

> 一句话总览：RTL 把 cache 拆成「顶层 `VX_cache` + 每 bank `VX_cache_bank` + 子模块 tags/mshr/repl/data」五件套；SimX 把它们压缩进 `cache.cpp` 一个文件里的 `Cache::Impl` / `CacheBank` / `MSHR` / `set_t` 四个类。**两边是同一架构的两种实现**，这是 `model_parity` 的物理基础。

## 4. 核心概念与源码讲解

### 4.1 顶层与数据通路骨架：crossbar × bank × 仲裁

#### 4.1.1 概念说明

一个 Vortex cache 对外暴露两组端口：向上（核心侧）有 `NUM_REQS` 个请求口，向下（访存侧）有 `MEM_PORTS` 个访存口；内部则是 `NUM_BANKS` 个并行 bank。请求需要被**按地址分发**到对应的 bank（bank 间以 line 为粒度交织），bank 的响应要**汇聚**回发请求的那个口，bank 的 miss 请求要**仲裁**到有限的访存口。

于是顶层天然是「两个 crossbar + 一个仲裁器」的拓扑：

- 请求 dispatch crossbar：`NUM_REQS → NUM_BANKS`，按 bank-id 选路，冲突用轮转（R）仲裁。
- 响应 merge crossbar：`NUM_BANKS → NUM_REQS`，把 bank 的响应送回原请求口。
- 访存 mux/demux：`NUM_BANKS → MEM_PORTS` 的请求仲裁 + `MEM_PORTS → NUM_BANKS` 的响应分发。

SimX 的 `Cache::Impl` 用完全相同的拓扑，只是把 RTL 的 `VX_stream_xbar` / `VX_stream_omega` 换成了 `MemCrossBar` / `MemArbiter` 这两个 SimObject。

#### 4.1.2 核心流程

```text
                 RTL VX_cache.sv / SimX Cache::Impl
   core_req[0..NUM_REQS-1]
            │  (cache_init / Core 在 SimX)
            ▼
   ┌─────────────────────── dispatch crossbar (按 bank_id 选路, RR) ──┐
   │                                                                 │
   ▼              ▼             ▼              ▼
 bank0         bank1   ...   bank_i        ... bank_{NUM_BANKS-1}
   │   内部流水线: S0(repl+tags+MSHR分配) → S1(MSHR finalize)        │
   │              → 数据阵列 → stC(核心响应 / 访存请求)                │
   │              每个非阻塞 bank 自带一个 MSHR                       │
   ▼              ▼             ▼              ▼
   └────────────────────── merge crossbar ─→ core_rsp[0..NUM_REQS-1]
            │
   mem_req_xbar (NUM_BANKS → MEM_PORTS, RR) ─→ mem_req[0..MEM_PORTS-1]
   mem_rsp 反向分发 (MEM_PORTS → NUM_BANKS)
```

几何参数把地址切成五段（从低位到高位）：word 选择、bank 选择、set 选择、tag。下式给出每段的来源（`C=Σlog2容量, L=log2行, S=log2扇区, W=log2字, A=log2路, B=log2bank 数`）：

\[ \text{index\_bits} = C - (L + A + B),\quad \text{offset\_bits} = L - W,\quad \text{sector\_bits} = L - S \]

#### 4.1.3 源码精读

RTL 顶层先声明所有几何与策略参数（注意 `MSHR_SIZE`、`REPL_POLICY`、`IS_LLC`、`AMO_ENABLE` 都是 per-cache 旋钮）：

[VX_cache.sv:L16-L53](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache.sv#L16-L53) —— 顶层参数，`NUM_BANKS`/`NUM_WAYS`/`MSHR_SIZE`/`WRITEBACK`/`REPL_POLICY`/`IS_LLC` 决定 cache 的全部形状。

请求 dispatch crossbar（轮转仲裁 `ARBITER="R"`，按 `core_req_bid` 即 bank-id 选路）：

```systemverilog
VX_stream_xbar #(
    .NUM_INPUTS  (NUM_REQS), .NUM_OUTPUTS (NUM_BANKS),
    .ARBITER     ("R"),      .OUT_BUF     (REQ_XBAR_BUF)
) core_req_xbar (...);   // core_req_valid → per_bank_core_req_valid，按 bank-id 分发
```

[VX_cache.sv:L272-L295](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache.sv#L272-L295) —— 请求分发 crossbar；同一拍多个请求映射到同一 bank 时由轮转仲裁器裁决。

随后是一个 `for` 循环实例化 `NUM_BANKS` 个 `VX_cache_bank`，每个 bank 自带一个 MSHR：

[VX_cache.sv:L307-L372](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache.sv#L307-L372) —— bank 实例化循环，把 `MSHR_SIZE`、`REPL_POLICY`、`IS_LLC`、`AMO_ENABLE` 透传给每个 bank。

访存侧的请求仲裁 crossbar（`NUM_BANKS → MEM_PORTS`）：

[VX_cache.sv:L427-L445](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache.sv#L427-L445) —— `mem_req_xbar`，bank 的 miss 请求汇聚到访存口。

SimX 这边，`Cache::Impl` 的构造函数用 `MemCrossBar` + `MemArbiter` 搭出**同样的拓扑**：

```cpp
bank_core_xbar_ = MemCrossBar::Create(..., [&](const ReqType &req){
    return params_.addr_bank_id(req.addr);   // 按 bank-id 选路，与 RTL 同
});
auto bank_mem_arb = MemArbiter::Create(..., ArbiterType::RoundRobin, num_banks, config_.mem_ports);
for (...) {
    banks_.at(i) = CacheBank::Create(sname, config, params_, i);
    bank_core_xbar_->ReqOut.at(i).bind(&banks_.at(i)->core_req_in);
    banks_.at(i)->mem_req_out.bind(&bank_mem_arb->ReqIn.at(i));
}
```

[cache.cpp:L1725-L1744](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/cache.cpp#L1725-L1744) —— SimX 顶层拓扑：core xbar（按 `addr_bank_id` 选路）→ banks → bank mem arb（RR）。

地址译码（bank/set/sector/word/tag 各占哪几位）在 SimX 的 `params_t` 构造里集中完成：

[cache.cpp:L52-L87](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/cache.cpp#L52-L87) —— `params_t` 把地址切成 word/bank/set/tag 段，sector 占行内偏移的高位。

#### 4.1.4 代码实践

**实践目标**：确认「RTL 顶层 crossbar 拓扑 = SimX `Cache::Impl` 拓扑」。

**操作步骤**：

1. 打开 `VX_cache.sv`，统计它实例化了哪几个 `VX_stream_xbar` / `VX_stream_omega`（dispatch、merge、mem_req 各一个），记录每个的 `NUM_INPUTS/NUM_OUTPUTS/ARBITER`。
2. 打开 `cache.cpp` 的 `Cache::Impl` 构造，找到对应的 `MemCrossBar` / `MemArbiter`，记录它们的输入输出数与仲裁类型（`RoundRobin`）。
3. 对照两份清单。

**需要观察的现象**：两边都是「请求 RR 仲裁分发 + 响应汇聚 + 访存 RR 仲裁」的三件套；bank 选择都按地址的 bank 段。

**预期结果**：拓扑一一对应，差别只在「RTL 用硬件总线接口 `*_bus_if`、SimX 用带类型的 `SimChannel<MemReq/MemRsp>`」（这条差别在 u5-l1、u7-l1 已建立）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 dispatch crossbar 用轮转（R）仲裁，而不是固定优先级？
**答**：多个请求口可能同拍映射到同一 bank，固定优先级会让低优先级口饥饿；轮转保证公平，且确定性可复现（每个 bank 每拍只服务一个请求），这是 `model_parity` 要求的。

**练习 2**：`NUM_BANKS` 与 `MEM_PORTS` 谁大谁小？为什么 `VX_cache.sv` 里有断言 `NUM_BANKS >= MEM_PORTS`？
**答**：通常 `NUM_BANKS >= MEM_PORTS`（见 [VX_cache.sv:L65](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache.sv#L65)）。多个 bank 的 miss 要仲裁到较少的访存口；若 bank 数少于访存口，多余访存口无法被利用。

---

### 4.2 标签存储 tags：line/sector 命中判定

#### 4.2.1 概念说明

tag store 要回答两个问题：（1）这一行在不在 cache 里？（2）在的话，我要的那个 sector 有效吗？Vortex 把 line 与 sector 解耦——一个 tag 覆盖整条 line，但 line 里的每个 sector 有独立的 valid/dirty 位。所以「命中」分两层：

- **line present（行驻留）**：tag 匹配且至少一个 sector 有效 → 这一行在 cache。
- **sector hit（扇区命中）**：tag 匹配**且被请求的那个 sector 有效** → 这次访问命中。

一个 line present 但 sector 无效的访问走 miss 路径，但**只 refill 缺的那个 sector**（sector refill），不必驱逐整行、不必换路。这正是 L2/L3 分段行（sectored line）的意义：用一条 tag 覆盖更大的行（省 tag 数），fill 仍按 sector 走（省 fill 带宽）。

#### 4.2.2 核心流程

```text
addr → {tag, set, sector, word}
读 set → 取出所有 way 的 {tag, valid[SEC]}
for each way:
    raw_hit    = (line_tag == read_tag) && read_valid[sector]      // 扇区命中
    line_present = (line_tag == read_tag) && (| read_valid)        // 行驻留
tag_matches = raw_hit  (per-way 向量)
↓
有一个 way raw_hit → 命中，用该 way
所有 way 都不 line_present → 行 miss → 选 victim way 换路
line_present 但无 sector hit → sector refill → 用驻留的 way（不换路）
```

RTL 的实现关键：把所有 way 的 `{valid[SEC], tag}` 打包进**一个 BRAM 字**，同地址读出后并行比较，写时用 per-way 写使能单独更新某一路。SimX 则是把 set 存成 `std::vector<line_t>`，软件循环比较。

#### 4.2.3 源码精读

RTL 标签存储的头部注释直白说明了「单数组 + per-way 写使能」的设计动机：

[VX_cache_tags.sv:L16-L21](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache_tags.sv#L16-L21) —— 单数组 tag store：所有 way 共用一个 BRAM，per-way 写使能免去读-改-写。

每一路的命中判定（注意 `raw_hit` 要 tag 匹配**且 sector 有效**）：

```systemverilog
wire raw_hit = read_valid[i][sector_idx] && (line_tag == read_tag);
...
assign tag_matches[i] = raw_hit;
assign line_present[i] = (line_tag == read_tag[i]) && (| read_valid[i]);  // 任一 sector 有效
```

[VX_cache_tags.sv:L102-L106](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache_tags.sv#L102-L106) 与 [VX_cache_tags.sv:L197-L200](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache_tags.sv#L197-L200) —— 区分扇区命中（`tag_matches`）与行驻留（`line_present`），后者驱动 sector refill vs 换路的决策。

sector valid 的合并写入：fill 到驻留行时把新 sector「或」进现有 valid 向量，fill 到新 victim 时只装这一个 sector：

[VX_cache_tags.sv:L121-L135](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache_tags.sv#L121-L135) —— per-sector valid 合并，init/flush 清零、inval 清单 sector、fill 时按是否行驻留选择 OR 或覆盖。

「read-first 旁路」是 tag store 最精巧的一点：一条 fill 在上一拍刚提交进 BRAM，本拍还没出现在读出端；如果不处理，紧跟 fill 的 replay 会假 miss。RTL 用一组 `BUFFER_EX`（带 stall 保持）把上一拍的 fill 折进读出：

[VX_cache_tags.sv:L137-L166](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache_tags.sv#L137-L166) —— read-first 旁路：按 set 与 way 精确替换刚 fill 的内容，并在流水线 stall 时保持，避免多拍写回期间旁路过期。

tag 存储本体是一个 `VX_dp_ram`（read-first 模式 `"R"`）：

[VX_cache_tags.sv:L203-L222](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache_tags.sv#L203-L222) —— 单数组 tag BRAM，look-ahead 读（`raddr=line_idx_n`）、read-first 写。

SimX 这边，tags 是 `set_t` 里的 `std::vector<line_t>`，命中判定在 `tag_match()` 里——它返回**行驻留**的 way（注意：不在此处判定 sector 有效，留给调用者）：

[cache.cpp:L216-L264](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/cache.cpp#L216-L264) —— `set_t::tag_match`：纯查表不改状态，返回行驻留 way 与候选 victim/free，并按策略预算 victim。

SimX 的数据结构 `sector_t` / `line_t` 把 valid/dirty/data 放在 sector 粒度（`data` 就是 `shared_ptr<mem_block_t>`）：

[cache.cpp:L138-L170](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/cache.cpp#L138-L170) —— `sector_t`（valid/dirty/dirty_mask/data）与 `line_t`（tag/lru_ctr/sectors），`any_valid()`/`any_dirty()` 对应 RTL 的 `(| read_valid)`。

调用方（如 `processRequests` 的 `Core` 分支）在 `tag_match` 之后再检查 sector 是否有效，把「行驻留」细化成「扇区命中」：

```cpp
int present_id = set.tag_match(addr_tag, config_.repl_policy, rand_ctr_, &free_id, &repl_id);
int hit_id = (present_id != -1 && set.lines.at(present_id).sectors.at(sector_id).valid) ? present_id : -1;
```

[cache.cpp:L1394-L1406](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/cache.cpp#L1394-L1406) —— 两层命中判定的 SimX 写法：先 `tag_match` 找行驻留，再查 sector valid。

#### 4.2.4 代码实践

**实践目标**：理解 sector refill 为何不驱逐整行。

**操作步骤**：

1. 在 `VX_cache_bank.sv` 找到 `line_present_any_st0` 与 `fill_way_st0`（约 [L488-L498](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache_bank.sv#L488-L498)）。
2. 追踪：当 fill 的行已驻留时，`fill_way_st0 = present_way_st0`（驻留路），否则 `= victim_way`（新换路）。
3. 在 `cache.cpp` 的 `Fill` 分支找 `find_resident` / `is_refill`（[L1226-L1232](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/cache.cpp#L1226-L1232)），确认 refill 时不走 `select_victim`、不写回。

**需要观察的现象**：行已驻留时，fill 只补 sector、`wb_count=0`（无写回）；行不驻留时才选 victim、写回脏 sector。

**预期结果**：两边语义一致——sector refill 是「往驻留路里补一个 sector」，不触发驱逐。

#### 4.2.5 小练习与答案

**练习 1**：`SECTOR_SIZE == LINE_SIZE`（即每行 1 个 sector）时，`line_present` 与 `tag_matches` 是什么关系？
**答**：相等。此时「行驻留」与「扇区有效」退化为同一件事，没有 sector refill，fill 永远换新路。L1（icache/dcache）就是这种不分段的形态。

**练习 2**：为什么 SimX 的 `tag_match` 注释强调「调用者必须在所有 stall 检查通过**之后**再调 `update_lru()`」？
**答**：PLRU 的年龄计数器是状态。如果在 stall 重试路径上也更新，同一个访问会被重复计入，导致 PLRU 计数漂移、选错 victim。所以查表（不改状态）和更新（改状态）必须分开，更新只在访问确定提交后做一次。

---

### 4.3 MSHR：多重未命中的链式合并与回放

#### 4.3.1 概念说明

MSHR 是非阻塞缓存的核心。它做三件事：

1. **登记**：每次 miss 分配一个表项，记录请求的地址、读写、数据、tag 等。
2. **合并**：新 miss 如果命中一个已在等同一 set/tag/sector 的表项，**不另发 fill**，而是挂到那条链上——所有等待者共享一次 fill。
3. **回放（replay）**：fill 回来时，把这条链上的所有等待者按到达顺序逐个重放（此时已是命中），各自拿到数据 / 完成写回。

这一节是本讲的核心，也是实践任务的重点。要特别注意「**请求**和「**line 数据**」在 MSHR 里是两条不同的载体：

- **未命中请求**：RTL 里每个 MSHR 表项存请求元数据（地址、tag、byteen、写数据等），用 `next_index`/`next_table` 串成链表；SimX 里 `MSHR` 类存 `mshr_entry_t`（bank_req + set/tag/sector + seq），合并靠 `lookup()` 匹配、回放靠 `replay()` 把匹配项标成 `Replay` 再 `dequeue()` 按 `seq` 出队。
- **line/sector 数据**：RTL 里 fill 数据**不进 MSHR**，而是进 bank 的 fill 暂存寄存器 `fbuf_data_r`，在数据阵列写回的同时直接前递；SimX 里 fill 数据是 `MemRsp::data`（一个 `shared_ptr<mem_block_t>`），`Fill` 分支把它直接装进 `sector.data`，replay 时直接读 `sector.data`。

#### 4.3.2 核心流程

RTL MSHR 的四类端口（按时序）：

```text
allocate（S0, 命中判定后）:
   每个未命中 core 请求都先分配一个槽 id
   查 addr_matches：是否有同 (line, sector) 的有效表项？
     有 → allocate_pending=1，并返回链尾 previd
     无 → allocate_pending=0（这是链头，要发 fill）
finalize（S1, 紧跟一拍）:
   hit   → release（释放该槽）
   miss  → pending（把这个槽链到 previd 后面，等 fill）
fill（mem 响应到来）:
   用 fill_id 指向链头，开始 dequeue
dequeue（回放）:
   弹出链头 → 重新进流水线（这次命中）
   若 next_table[id] 还有后继 → 继续 dequeue，否则停
```

SimX 的等价流程在 `CacheBank::processInputs` + `processRequests`：

```text
admission（processInputs）:  新 core 请求若 lookup() 命中已有链 → 直接 enqueue 挂链（合并）
core miss（processRequests::Core）: 首个 miss 发 fill 请求、enqueue；后续同链 miss 不重发
fill（processRequests::Fill）: 把 sector 数据装进 set，调 replay(mshr_id) 标记链上所有项为 Replay
drain（processInputs 优先级 1）: dequeue() 按 seq 最小（最老）出队 → 作为 Replay 进流水线 → 命中读 sector.data
```

#### 4.3.3 源码精读

RTL MSHR 的设计意图写在文件头部注释里——分配在命中前、fill 时触发整链回放、链表按到达顺序出队：

[VX_cache_mshr.sv:L16-L43](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache_mshr.sv#L16-L43) —— MSHR 工作模型：每项有指向同链下一项的指针，fill 时 dequeue 整条链。

合并的匹配逻辑：同 `(line_addr, sector)` 才算同一条链；AMO 在 passthrough 模式下被排除（永不合并）；正在 dequeue 的链尾也排除（避免孤儿）：

```systemverilog
assign addr_matches[i] = valid_table[i] && (addr_table[i] == allocate_addr)
                      && (sector_table[i] == allocate_sector) && ~amo_mask[i]
                      && ~(dequeue_fire && (dequeue_id == MSHR_ADDR_WIDTH'(i)));
```

[VX_cache_mshr.sv:L144-L154](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache_mshr.sv#L144-L154) —— 合并匹配：per-entry 比较 line+sector，排除 AMO 与正在排空的链尾。

四阶段状态机（fill 触发 dequeue、dequeue 走 `next_index`、finalize 释放或挂链、allocate 占槽）：

[VX_cache_mshr.sv:L175-L213](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache_mshr.sv#L175-L213) —— MSHR 组合/时序核心：`valid_table`/`next_table`/`next_index` 维护链表，fill→dequeue→finalize→allocate 四动作同拍协调。

写穿透模式下写请求**不**算 pending（写穿透的写已经直接下发到下一级，不必等 fill）：

[VX_cache_mshr.sv:L323-L328](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache_mshr.sv#L323-L328) —— `allocate_pending` 在 write-back 下取 `|addr_matches`，写穿透下排除写请求。

bank 把 MSHR 与 tag/repl 接在一起（allocate 在 S0、finalize 在 S1，恰好隔一拍——注释解释了为何不能延后，否则合并链会孤儿死锁）：

[VX_cache_bank.sv:L751-L804](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache_bank.sv#L751-L804) —— MSHR 实例：`fill_valid=mem_rsp_fire`、`dequeue`→replay、`allocate`@S0、`finalize`@S1。

fill 的 **line 数据**通路在 RTL 里走 fill 暂存寄存器，不进 MSHR（`fbuf_data_r` 在 fill accept 时锁存，喂给数据阵列写口与前递响应）：

[VX_cache_bank.sv:L963-L1004](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache_bank.sv#L963-L1004) —— fill forwarding：fill sector 暂存后，链上连续的纯读请求直接从暂存数据出响应、不走流水线，遇到第一个写/AMO 才关闭前递窗口。

SimX 的 MSHR 是一个类，合并用 `lookup()`、回放用 `replay()` + `dequeue()`（按 `seq` 最老优先，保证程序序）：

```cpp
bool lookup(set_id, addr_tag, sector_id, uint32_t *root_id=nullptr) const;  // 合并探测
mshr_entry_t &replay(uint32_t id);   // fill 回来后，把同 (set,tag,sector) 的所有 Core 项标成 Replay
void dequeue(bank_req_t *out);       // 按 seq 最小（最老）取一个 Replay 出队
```

[cache.cpp:L434-L598](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/cache.cpp#L434-L598) —— SimX `MSHR` 类：`lookup`/`enqueue`/`replay`/`dequeue`，`seq` 是入队序号，注释解释了为何必须按到达序回放（store-then-load 与 load-then-store 都要对）。

SimX 的 fill 数据处理（`processRequests::Fill`）——这是「line 数据如何承载」的 SimX 答案：fill 响应里的 `shared_ptr` 直接装进 sector，然后 `replay()` 唤醒链上等待者，回放时从 `sector.data` 读：

```cpp
auto &sec = line.sectors.at(sector_id);
sec.valid = true; sec.dirty = false; sec.data = bank_req.data;  // bank_req.data 来自 mem_rsp.data
mshr_.replay(bank_req.mshr_id);                                 // 唤醒同链
```

[cache.cpp:L1215-L1290](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/cache.cpp#L1215-L1290) —— `Fill` 分支：选 victim、写回脏 sector、把 fill 的 `shared_ptr` 装入新 sector、`replay()` 唤醒等待链。

admission 阶段的合并（`processInputs`）：新请求若命中已有链，直接 `enqueue` 挂链、若链已在排空则 `defer_to_replay`，不发新 fill：

[cache.cpp:L860-L900](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/cache.cpp#L860-L900) —— 入口合并：探测已有同链则直接挂上，避免请求穿越流水线后才发现该合并。

#### 4.3.4 代码实践

**实践目标**：对比 RTL 与 SimX 的 MSHR 如何分别承载「未命中请求」与「line 数据」。

**操作步骤**：

1. 在 `VX_cache_mshr.sv` 找 `addr_table`/`next_index`/`mshr_store`（请求与写数据的存储）与 `VX_dp_ram mshr_store`（[L250-L265](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache_mshr.sv#L250-L265)）。
2. 在 `VX_cache_bank.sv` 找 fill 暂存 `fbuf_data_r`（[L208-L209](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache_bank.sv#L208-L209)）——确认 fill 的 line 数据存在这里、**不在 MSHR**。
3. 在 `cache.cpp` 看 `mshr_entry_t`（[L411-L432](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/cache.cpp#L411-L432)，只存请求元数据）与 `Fill` 分支里 `sec.data = bank_req.data`（数据直接进 sector）。

**需要观察的现象**：
- 两侧 MSHR 都**只存请求元数据 + 合并链**，不存 line 的读数据。
- line/sector 数据：RTL 走 `fbuf_data_r` 暂存 → 数据阵列；SimX 走 `MemRsp::data`（`shared_ptr`）→ `sector.data`。
- 两侧合并键都是 `(set/line, tag, sector)`，回放都按到达序。

**预期结果**：能画出两张并列的「miss → 合并 → fill → 回放」时序图，并标注数据载体分别是 `fbuf_data_r`（RTL）与 `sector.data`/`shared_ptr`（SimX）。

#### 4.3.5 小练习与答案

**练习 1**：为什么写穿透 cache 里写 miss 不算 `pending`，而写回 cache 里算？
**答**：写穿透的写在 miss 时就已直接下发下一级并响应核心，不必等 fill 回来再回放，所以不挂链；写回的写要先等 fill 把行取回、才能把写数据并进去，所以要挂链（pending）。

**练习 2**：SimX 的 `dequeue()` 为什么要按 `seq`（入队序）而不是按 MSHR 表项 id 出队？
**答**：合并链上的访问必须按程序序回放——同地址的 store 必须先于后继 load 合并自己的数据，否则 load 会读到 fill 带回的旧值。表项 id 是分配先后，但中途挂链的请求 id 不一定单调，所以用专门的入队序号 `seq` 保证语义。见 [cache.cpp:L563-L584](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/cache.cpp#L563-L584) 的注释。

---

### 4.4 替换策略：FIFO / PLRU / Random 三选一

#### 4.4.1 概念说明

cache 满了要驱逐谁？Vortex 支持三种策略（编译期由 `REPL_POLICY` 选，默认 FIFO）：

- **FIFO（先入先出）**：每个 set 一个循环计数器，按入路顺序换。最省资源（每 set 一个计数器），流式 GPU 负载下命中率与 PLRU 相差无几。
- **PLRU（伪 LRU）**：近似「最久未用」。RTL 用**树形位**（每 set `NUM_WAYS-1` 个位）；SimX 用**每路年龄计数器**（`lru_ctr`）。两者都是 LRU 的近似，不保证逐位一致。
- **Random（随机）**：RTL 用 LFSR 伪随机；SimX 用 `rand_ctr_` 计数器取模。最省逻辑，对最坏访问模式退化得最优雅。

策略宏在 [VX_cache_define.vh:L95-L97](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache_define.vh#L95-L97)：`CS_REPL_RANDOM=0, CS_REPL_FIFO=1, CS_REPL_PLRU=2`，与 SimX `cache.h` 的 `ReplPolicy` 枚举（`RANDOM=0, FIFO=1, PLRU=2`）一一对应。

#### 4.4.2 核心流程

替换发生在**行 miss 且要换路**时（sector refill 不换路，不触发替换）。流程：

```text
行 miss（无 line_present）:
   RTL: VX_cache_repl 读 repl_store（按 repl_line_n look-ahead）→ repl_way
        PLRU: plru_encoder 把树位译成 way
        FIFO: 读 fifo_ptr
        Random: LFSR 状态 ^ 行号
   SimX: set.select_victim(policy, rand_idx) → 选 free 优先，否则按策略选 victim
命中或 refill: 更新替换状态
        PLRU: 命中路 lru_ctr=0，其余 ++  (SimX)
        PLRU: 命中路经 plru_decoder 更新树位 (RTL)
        FIFO/RANDOM: 无命中更新（FIFO 只在换路时 ++）
```

注意：两边的替换状态都按 set 索引存储，且都用「look-ahead 读 + read-first」对齐流水线（RTL 的 `VX_dp_ram` 与 SimX 的 set 数组）。

#### 4.4.3 源码精读

RTL PLRU 用树形位 + BaseJump STL 的 `plru_decoder`/`plru_encoder`：

```systemverilog
VX_dp_ram #(...) plru_store (
    .read  (~stall), .write (init || (lookup_valid && lookup_hit)),
    .waddr (lookup_line), .raddr (repl_line_n),   // look-ahead + read-first
    .wdata (init ? '0 : plru_wdata), .rdata (plru_rdata)
);
plru_decoder #(.NUM_WAYS(NUM_WAYS)) plru_dec (.way_idx(lookup_way), .lru_data(plru_wdata), .lru_mask(plru_wmask));
plru_encoder #(.NUM_WAYS(NUM_WAYS)) plru_enc (.lru_in(plru_rdata),  .way_idx(repl_way));
```

[VX_cache_repl.sv:L108-L153](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache_repl.sv#L108-L153) —— PLRU：树形位存 BRAM、命中时译码更新、换路时编码出 victim way。

RTL FIFO 是每 set 一个递增计数器（`fifo_wdata = fifo_rdata + 1`）：

[VX_cache_repl.sv:L154-L183](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache_repl.sv#L154-L183) —— FIFO：单计数器循环，仅换路（`repl_valid`）时递增。

RTL Random 用 LFSR（`xnor` 最高两位做反馈），并与行号异或使不同行有不同序列：

[VX_cache_repl.sv:L184-L208](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache_repl.sv#L184-L208) —— Random：LFSR 状态取低若干位异或行号得到 victim way。

SimX 的 victim 选择在 `set_t::select_victim`（free 优先；否则 PLRU 取 `lru_ctr` 最大者，FIFO 取 `fifo_ptr`，RANDOM 取 `rand_idx`）：

[cache.cpp:L282-L321](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/cache.cpp#L282-L321) —— `select_victim`：纯查表不改状态；有空路优先用空路。

SimX 的 PLRU 年龄更新（命中路清零、其余自增）：

```cpp
void update_lru(int hit_line_id) {
    for (...) {
        if ((int)i == hit_line_id) line.lru_ctr = 0;
        else                        ++line.lru_ctr;
    }
}
```

[cache.cpp:L268-L279](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/cache.cpp#L268-L279) —— SimX PLRU 更新：年龄计数器法。

> **parity 细节**：默认 `fifo`，两边都是循环计数器、选路一致。PLRU 时 RTL 用树形位、SimX 用年龄计数器，是两种**不同的 LRU 近似**，可能选不同 victim；但只要功能正确（不丢数据），不影响退休指令一致性。这正是不该把 `model_parity` 容差当成「万能抹布」的原因——差异要么对齐，要么记 known_issue（见 u7-l4）。

#### 4.4.4 代码实践

**实践目标**：把替换策略旋钮与代码分支对应起来。

**操作步骤**：

1. 在 `VX_config.toml` 搜 `REPL_POLICY`，记录各缓存的默认值（应是 `fifo`）。或在 `docs/designs/cache_subsystem.md` 的 `*_REPL_POLICY` 行确认。
2. 在 `VX_cache_repl.sv` 找到 `if (REPL_POLICY == ...)` 的三段，确认它们互斥。
3. 在 `cache.cpp` 的 `select_victim` 与 `Fill` 分支找到 `switch (policy)` 与 `set.fifo_ptr = (set.fifo_ptr + 1) % ...`（[L1247-L1251](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/cache.cpp#L1247-L1251)）。
4. （选做）用 `CONFIGS=-DVX_CFG_L2_REPL_POLICY=2` 重新 configure 跑一个回归，对比 FIFO/PLRU 下的命中率（`--perf=1`，见 u13-l3）。

**需要观察的现象**：策略是**编译期**参数，三段代码只有一段被综合/编译进去；改策略要重新 configure（u1-l3、u2-l1 纪律）。

**预期结果**：默认 FIFO 下，RTL 与 SimX 都用每 set 单计数器、行为一致。

#### 4.4.5 小练习与答案

**练习 1**：替换策略模块的「输入」有哪些？它在什么时候被触发？
**答**：输入主要是 `lookup_valid/lookup_hit/lookup_line/lookup_way`（命中更新状态）与 `repl_valid/repl_line`（换路时读 victim）。触发于行 miss 换路（`repl_valid`，对应 `do_fill_st0`）。sector refill 不换路，所以不触发。

**练习 2**：为什么 `select_victim` 优先返回空路（`free_line_id`）而不是直接用策略选的 victim？
**答**：set 还没满时（有空路），直接用空路免去无谓驱逐与写回，既省带宽也保住更多有效数据；只有 set 满了才按策略在有效路里挑 victim。

---

## 5. 综合实践

**任务**：完成实践任务要求的「对比 RTL 与 SimX 的 cache，解释 MSHR 在两个实现中如何承载未命中请求与 line 数据，并说明替换策略模块的职责」。

请用一张大表把本讲的四个维度填完（这就是你把知识串起来的产物）：

| 维度 | RTL 实现 | SimX 实现 | 关键代码位置 |
|---|---|---|---|
| 顶层/数据通路 | `VX_cache`：stream xbar+omega | `Cache::Impl`：MemCrossBar/Arbiter | VX_cache.sv / cache.cpp Impl |
| 标签命中 | 单数组 BRAM + read-first 旁路 | `set_t::tag_match` + sector valid 检查 | VX_cache_tags.sv / cache.cpp |
| MSHR（请求） | 链表 `next_index` + addr_matches 合并 | `MSHR::lookup/replay/dequeue` 按 seq | VX_cache_mshr.sv / cache.cpp MSHR |
| MSHR（line 数据） | fill 暂存 `fbuf_data_r`（不进 MSHR） | `sector.data = shared_ptr`（fill 响应直接装入） | VX_cache_bank.sv / cache.cpp Fill |
| 替换策略 | 树位 PLRU / 计数器 FIFO / LFSR Rand | 年龄计数器 PLRU / fifo_ptr / rand_ctr_ | VX_cache_repl.sv / cache.cpp |

具体步骤：

1. **画 miss 全链路时序图（两张并排）**：一个 load miss、紧接同 sector 的第二个 load（应合并）、然后 fill 回来、两个 load 先后回放。RTL 版标注 `fbuf_data_r`、SimX 版标注 `shared_ptr`。
2. **验证合并键**：确认两侧合并键都是 `(set, tag, sector)`；解释为什么「同 line 不同 sector」不合并（各发各的 fill）。
3. **验证 fill forwarding**：在 RTL 找 `fwd_active_r`/`fwd_head`/`fwd_fire`，在 SimX 找 `fwd_active_`/`processForward`，说明「第一个写/AMO 关闭前递窗口、回退到正常 replay」的动机（保程序序）。
4. **替换策略职责小结**：用一句话写出替换模块的三项职责——(a) 行 miss 时选 victim way、(b) 命中时更新命中序（仅 PLRU）、(c) 用 look-ahead + read-first 与流水线对齐。

如果手头有可运行的构建树，可进一步：用 `./ci/blackbox.sh --driver=simx --app=sgemm --l2cache`（旋钮名以本地 `ci/blackbox.sh --help` 为准，待本地验证）开 L2，再用 `--perf=1` 读 `read_misses`/`mshr_stalls`，把 MSHR 占用与 miss 数对上（参见 u13-l3）。

## 6. 本讲小结

- Vortex cache 的**顶层拓扑**在 RTL（`VX_cache` 的 stream xbar/omega）与 SimX（`Cache::Impl` 的 MemCrossBar/Arbiter）里一一对应：dispatch/merge 两个 crossbar + 一个访存仲裁。
- **tag store** 用单数组 BRAM + per-way 写使能，区分「行驻留（line_present）」与「扇区命中（tag_matches）」，从而支持 sector refill（往驻留路补 sector，不驱逐整行）；read-first 旁路保证 fill 后紧跟的 replay 不假 miss。
- **MSHR** 让缓存非阻塞：用链式合并让同一 set/tag/sector 的多个 miss 共享一次 fill，fill 回来后按到达序回放。**请求**在两侧都用链/表项存（RTL 的 `next_index` 链表 vs SimX 的 `MSHR` 类按 seq 出队），而 **line 数据**在两侧都不存在 MSHR 里——RTL 走 fill 暂存 `fbuf_data_r`，SimX 走 `shared_ptr` 装入 `sector.data`。
- **fill forwarding** 在两侧都存在：fill 数据回来时直接服务链上连续纯读，省掉「写完再读」，遇到第一个写/AMO 关闭窗口、退回正常回放以保程序序。
- **替换策略**三选一（默认 FIFO），RTL 用树位/计数器/LFSR，SimX 用年龄计数器/计数器/`rand_ctr_`；FIFO 下两侧行为一致，PLRU 是两种 LRU 近似。
- 贯穿全讲的纪律：RTL 与 SimX 是同一架构的两种实现，任何改动都要两侧同步，否则破坏 `model_parity`（承接 u7-l4）。

## 7. 下一步学习建议

- 下一讲 **u8-l3（访存合并、本地内存与 DRAM 模型）** 会讲 cache **上游**的 `mem_coalescer`（warp 内多线程请求如何合并成更少的 line 请求）和 **下游**的 `memory.cpp`（ramulator2 建模的 DRAM）。本讲的 cache 是承上启下的中段。
- 若想看 cache 如何被实例化成 L1/L2/L3 不同形态，回到 `VX_cache_cluster.sv`（L1 共享）与 `VX_cluster.sv`/`VX_socket.sv`（u7-l1），对照 `VX_config.toml` 的各级参数。
- 若对 AMO 在 cache 里的特殊路径（本讲多次提到 `amo_mask`、`probe_pending_ld/amo`、`AmoProbe`）感兴趣，先读 `docs/designs/atomic_memory_operations.md`，再在 u11-l2 系统展开。
- 调试建议：用 `--debug` 生成 trace 后（u13-l2），在 trace 里搜 `mshr`/`fill-rsp`/`replay`/`fwd-rsp` 关键字，把本讲的时序图与真实运行轨迹对上。
