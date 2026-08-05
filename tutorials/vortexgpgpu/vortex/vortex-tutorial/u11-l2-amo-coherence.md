# 原子内存操作与多缓存一致性

## 1. 本讲目标

本讲聚焦 Vortex 对 RISC-V「A」扩展（原子内存操作，AMO）的实现。读完后你应该能够：

- 说清 AMO（`AMOADD/AND/OR/XOR/MIN/MAX/SWAP` 与 `LR/SC`）在 Vortex 缓存层次中的「读-改-写」（RMW）是如何完成的，以及它为何被强制路由到末级缓存（LLC）的 bank 里解决。
- 理解多缓存（多核 L1、多 cluster L2、全局 L3）环境下，AMO 一致性靠什么保证，为什么 Vortex 选择 GPU 风格的「弱 / PULL」模型而不是目录式一致性。
- 把 SimX 的 `AmoUnit` 与 RTL 的 `VX_amo_unit` / `VX_cache_amo` 对应起来，理解二者如何维持 model parity，并知道当前已交付与尚未交付的部分分别在哪里。

本讲承接 [u8-l2（缓存标签、MSHR、替换与数据通路）](u8-l2-cache-internals.md)：你在那里建立的单级 cache「repl+tags+MSHR→数据阵列→提交」流水线，正是 AMO 挂载的宿主。

## 2. 前置知识

- **RMW（Read-Modify-Write）**：先读一个内存字的旧值，按操作（加、与、取大……）算出新值，再写回，且这三步对外不可分割。如果中间被别的 hart 插入一次写，结果就会错乱，所以 RMW 必须在一个「串行点」上完成。
- **LR/SC（Load-Reserved / Store-Conditional）**：RISC-V 的锁原语。`LR` 读一个字并留下一条「预留」，`SC` 只有在这条预留仍然有效时才写入并返回成功（0），否则返回失败（1）且不写入。它用来构造 CAS、自旋锁。
- **hart**：RISC-V 里「硬件线程」的称呼。Vortex 里一个 hart = 一个 thread lane，其全局编号 `hart_id = (cid · NUM_WARPS + wid) · NUM_THREADS + tid`。
- **LLC（Last-Level Cache）**：缓存层次最靠近 DRAM 的那一级（无 L2 时是 L1；有 L2 无 L3 时是 L2；有 L3 时是 L3）。
- **写直达（write-through）vs 写回（write-back）**：write-through 每次写都同步穿透到下一级；write-back 只在行被替换时才写回。AMO 要求其上方的所有缓存 write-through，原因见 4.1。
- **GPU 弱一致性**：GPU 的 L1 通常不硬件侦听（non-coherent），跨核的普通 store/load 可见性要靠程序员在同步点加 fence + invalidate。这与 CPU 的 MESI 目录式一致性不同。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [sim/simx/amo/amo_ops.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/amo/amo_ops.h) | SimX 的纯 RMW 计算内核 `amo_compute`，以及 line 内按字节偏移装/拆一个 word 的辅助函数。 |
| [sim/simx/amo/amo_unit.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/amo/amo_unit.h) / [amo_unit.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/amo/amo_unit.cpp) | SimX 的 `AmoUnit`：RMW 内核 + 按 hart 维护的预留表（reserve/check/invalidate/clear）。 |
| [sim/simx/mem/cache.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/cache.cpp) | SimX cache bank：`commitAmo`（LLC 提交）与 `AmoProbe`（非 LLC 透传）两条路径。 |
| [hw/rtl/cache/VX_amo_alu.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_amo_alu.sv) | RTL 纯组合 RMW 内核（`new_word`/`ret_word`）。 |
| [hw/rtl/cache/VX_amo_unit.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_amo_unit.sv) | RTL 的预留缓存（line 索引、BRAM 存 `{hart,tag}`、valid 位在寄存器），内含 ALU 实例。 |
| [hw/rtl/cache/VX_cache_amo.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache_amo.sv) | RTL 的 per-bank AMO 引擎：`IS_LLC=1` 提交角色（RMW + 合成写回 + 预留表）与 `IS_LLC=0` 透传角色（转发 + 结果回放）。 |
| [hw/rtl/Vortex.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/Vortex.sv) | 顶层：用 `STATIC_ASSERT` 强制 AMO 使能时其上方所有缓存 write-through。 |
| [docs/designs/atomic_memory_operations.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/atomic_memory_operations.md) | 单 LLC 原子设计文档（decode/LSU sideband/ALU/LLC 解决的奠基说明）。 |
| [docs/designs/multicache_amo_coherence.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/multicache_amo_coherence.md) | 多缓存 AMO 一致性设计文档（弱/PULL 模型、透传、自洽、年龄排序）。 |
| [tests/regression/amo/](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/amo/kernel.cpp) | 13 个 AMO 回归用例（含 `amoadd` 锤、`lrsc_counter`、`self_consistency`）。 |

> 关于 AMO 的功能开关：`VX_CFG_EXT_A_ENABLE`（默认 `false`，[VX_config.toml:30](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L30)）；使能后置 `MISA` bit 0。

---

## 4. 核心概念与源码讲解

### 4.1 AMO 基础与「在 LLC 解决」这条架构铁律

#### 4.1.1 概念说明

Vortex 处理 AMO 的总纲只有一句话，来自设计文档：

> **原子操作在末级缓存（LLC）的 bank 上解决。**

也就是说，无论一条 `amoadd` 是从哪个核、哪个 cluster 发出的，它最终都必须被送到 LLC 的某一个 bank，在那里由一个专门的 RMW ALU「读旧值→算新值→写回」，一拍内完成。中间的 L1/L2 不自行做 RMW，只负责把请求往下转发。

为什么必须集中在 LLC？因为**串行点只能有一个**。如果两个核各自的 L1 都能就地做 `amoadd`，那么对同一地址的两次原子加就可能在两个 L1 里各算一次、互相覆盖，最终只加了一而不是二。把所有原子都汇聚到 LLC 这个唯一的全局可见点，才能保证「每次 RMW 都看到上一次 RMW 的结果」。

为了让 LLC 真的「能看到每一次写」，Vortex 强制：**AMO 使能时，LLC 之上的每一级缓存都必须 write-through**。否则一个中间 write-back 缓存可能吸收掉某个 hart 的写，LLC 根本看不见，于是另一个 hart 的 `SC` 会假性成功——RISC-V 只允许「假性失败」，不允许「假性成功」。

#### 4.1.2 核心流程

一条 AMO 从译码到完成的端到端路径（参见 [atomic_memory_operations.md:148-161](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/atomic_memory_operations.md#L148)）：

```
decode(0x2F) ──► LSU 打包 amo_req sideband（强制 rw=0，走 load 通路）
            ──► coalescer/switch（AMO lane 永不合并；LMEM/IO AMO 被拒绝）
            ──► LLC bank 在 S1 提交：VX_amo_alu 做 RMW + VX_amo_unit 维护预留
            ──► 响应回 rd：ret_word（旧值）或 SC 的 0/1
            ──► miss 时：像 load 一样占一个 MSHR，fill 后 replay 提交
```

两个关键设计选择：

1. **AMO 走 load 通路**：即便 `SC` 本质是写，它的 `mem_req.rw` 也被强制为 0。这样 AMO 会分配一个 load 类的 MSHR 表项，并像 load 一样把一个值返回给 `rd`（旧值或成功标志）。真正的「写」发生在 LLC 的合成写回路径里，而不是普通 store 通路。
2. **AMO 永不合并**：`AMOADD` 不满足交换律/幂等性，coalescer 绝不能把同一 warp 内多个 lane 的 AMO 折叠成一次；它们必须各走各的 round-trip。

#### 4.1.3 源码精读

**write-through 强制（顶层断言）。** AMO 使能时，顶层根据实际 LLC 位置，静态断言其上方所有缓存为 write-through。当 L3 是 LLC 时，L1 与 L2 都必须 write-through：

[Vortex.sv:61-68](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/Vortex.sv#L61-L68) —— AMO 使能下的 write-through 静态断言：根据 LLC 是 L2 还是 L3，强制其上方缓存的 `WRITEBACK=0`。

注释直接点出原因：一个 write-back 的中间级可能吸收 hart-B 的写而不让 LLC 看到，导致 hart-A 后续的 `SC` 假性成功（[Vortex.sv:58-60](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/Vortex.sv#L58-L60)）。

**AMO 走 load 通路（rw=0）。** LSU slice 在构造内存请求时，只要 `amo_valid` 就把 `rw` 拉低：

[VX_lsu_slice.sv:182-189](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_slice.sv#L182-L189) —— `mem_req_rw = is_store && ~amo_valid`，注释明确：缺失行上的 `SC` 必须 miss-and-return-failure，绝不能 write-and-succeed。

**AMO 的 sideband 如何携带。** AMO 信息作为一个不透明的 per-lane 属性 `amo_req_t`（`{amo_valid, amo_op, amo_unsigned, hart_id}`）挂在内存总线的 attr 字段固定位置 `MEM_ATTR_AMO_OFFS=3`，于是共享的库 IP（`VX_mem_scheduler`/`VX_mem_coalescer`）对 AMO 完全无感：

[VX_gpu_pkg.sv:185-211](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_gpu_pkg.sv#L185-L211) —— 精简的 `amo_req_t` 与固定偏移的 `mem_bus_attr_t`，让仲裁器无需识别 AMO。

LSU 端把 `amo_valid/amo_op/amo_unsigned` 与 `hart_id = make_hart_id(cid,wid,tid)` 打包进这个 sideband：[VX_lsu_slice.sv:92-104](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_slice.sv#L92-L104)。

#### 4.1.4 代码实践

**实践目标**：确认「AMO 使能即强制 write-through」这条规则在你的配置里真的生效。

1. 在 build 目录运行 `../configure` 后，打开生成的 `config.mk`，找到 `VX_CFG_EXT_A_ENABLE` 与 `VX_CFG_DCACHE_WRITEBACK`/`VX_CFG_L2_WRITEBACK` 的取值。
2. 阅读 [Vortex.sv:61-68](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/Vortex.sv#L61-L68) 的 `STATIC_ASSERT`，回答：当 `L3_ENABLE=1` 且 `EXT_A_ENABLE=1` 时，哪两级缓存会被强制 write-through？
3. **预期结果**：L1（DCACHE）与 L2 都必须 `WRITEBACK=0`。如果你手动在 toml 里把 `L2_WRITEBACK` 设为非 0 又开了 AMO+L3，综合/elaborate 阶段应被静态断言拦下。

> 若本地未搭建综合环境，步骤 3 标注「待本地验证」；步骤 1–2 是纯源码阅读，可直接完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SC`（本质是写）也必须走 load 通路（`rw=0`）？
**答**：因为 `SC` 在预留失效时必须「miss 并返回失败」，而不是「写并成功」。如果走 store 通路，一个 cache miss 的 `SC` 可能直接把数据写下去并假装成功，这就构成了 RISC-V 禁止的「假性成功」。

**练习 2**：AMO lane 为什么绝不能被 coalescer 合并？
**答**：`AMOADD` 这类操作不满足交换律/幂等性。两个 lane 对同一字各做一次 +1，必须串行成两次 RMW（结果 +2）；若被合并成一次，只会 +1。

---

### 4.2 RMW 内核：原子读-改-写的纯计算

#### 4.2.1 概念说明

「在 LLC 解决」具体落到电路/代码上，就是一个**纯组合的 RMW 内核**：给定操作码、位宽（`.W`=32 位 / `.D`=64 位）、旧值 `old`、操作数 `rhs`，以及 MIN/MAX 的有/无符号选择，输出两个值：

- `new_word`：要写回 line 的新值（`LR` 不写）。
- `ret_word`：返回给 `rd` 的旧值（`SC` 的 0/1 由调用方另算，不经过内核）。

RTL 与 SimX 各有一份实现，语义逐位一致，是 model parity 的具体落点之一。

#### 4.2.2 核心流程

操作集合（9 个）与 RISC-V RVA 的对应（`MINU/MAXU` 折叠成 `MIN/MAX` + 一个无符号位）：

| op | new_word |
|---|---|
| LR | old（不写） |
| SC / SWAP | rhs |
| ADD | old + rhs |
| AND / OR / XOR | old 与 rhs 的位运算 |
| MIN / MAX | `(old < rhs) ? old : rhs`（按有/无符号比较） |

返回值 `ret_word` 始终是 `old`（旧值），LSU 再按位宽做符号扩展写回 `rd`。

#### 4.2.3 源码精读

**RTL 内核 `VX_amo_alu`** 是纯组合，`case` 直接把每种 op 翻译成一次加法/位运算/比较：

[VX_amo_alu.sv:58-75](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_amo_alu.sv#L58-L75) —— RMW 的 `case` 分支与 `.W` 截断：`MIN/MAX` 用 `is_unsigned` 在有符号（`_s`）与无符号（`_u`）比较间切换。

注意 [VX_amo_alu.sv:40-47](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_amo_alu.sv#L40-L47) 的精化：当 cache 字宽 ≤ 32 位时，整个 datapath 只可能是 `.W` 原子，`width` 参数被裁掉，加法器/比较器按 32 位综合以省面积。

**SimX 内核 `amo_compute`** 是同一个 case 的 C++ 镜像：

[amo_ops.h:52-74](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/amo/amo_ops.h#L52-L74) —— `switch (op)` 算 `newv`，`.W` 用 `mask_w` 截断，`MIN/MAX` 用 `unsigned_minmax` 选比较风格，最后返回 `{newv, retv}`。

`amo_ops.h` 还提供按字节偏移在一条 cache line 内装/拆一个 word 的辅助函数 `amo_load_word`/`amo_store_word`/`amo_byteen`（[amo_ops.h:78-97](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/amo/amo_ops.h#L78-L97)），它们被 cache bank 用来在 RMW 前后定位目标字。

#### 4.2.4 代码实践

**实践目标**：验证 RTL 与 SimX 两份内核对同一组输入产生相同的 `new_word`。

1. 任选一组输入，例如 `op=ADD, width=.W, old=10, rhs=1` → 两边都应得 `new_word=11, ret_word=10`。
2. 再选 `op=MIN, unsigned, old=5, rhs=8`（`.W`）→ 两边都应得 `new_word=5`。
3. 对照 [VX_amo_alu.sv:58-75](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_amo_alu.sv#L58-L75) 与 [amo_ops.h:52-74](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/amo/amo_ops.h#L52-L74)，逐 op 核对两边表达式是否一致。
4. **预期结果**：9 个 op、`.W`/`.D`、有/无符号 MIN/MAX 全部一一对应。若发现某 op 在两边的「枚举数值」不同（见 4.3 末尾的说明），那是编码差异，不是计算结果差异。

#### 4.2.5 小练习与答案

**练习 1**：`ret_word` 为什么直接取 `old`，而不用经过 ALU 的 `case`？
**答**：RVA 规定除 `SC` 外的所有 AMO 都返回「原子操作发生前的旧值」。`SC` 的返回值（0/1）由调用方根据预留表在内核之外决定（见 4.3），所以内核对返回值的处理统一为「旧值」即可。

**练习 2**：`.W` 与 `.D` 在 32 位字宽的 cache 里有什么区别？
**答**：没有区别——字宽 ≤ 32 位的 cache 只能承载 `.W` 原子，RTL 直接把 `width` 参数裁剪掉（[VX_amo_alu.sv:41-43](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_amo_alu.sv#L41-L43)）；只有 datapath 宽于 32 位时 `.D` 才有意义。

---

### 4.3 LR/SC 预留表与跨 hart 失效

#### 4.3.1 概念说明

`LR/SC` 的语义依赖一张「预留表」：`LR` 为 `(hart_id, line_addr)` 建立预留，`SC` 只有在对应预留仍有效时才成功。关键问题是：

- **预留归谁所有**：Vortex 按 **hart** 跟踪预留——每个 hart 至多一条预留，只可能被「对该 line 的一次已提交写」打破，而不会被另一个 hart 的 `LR` 挤掉。这匹配 RISC-V「预留是 hart 的属性」的定义。
- **如何打破别人的预留**：LLC 上任何一次对该 line 的已提交写（含 AMO 提交与普通 write-through）都会失效「除写者之外」的所有 hart 在该 line 上的预留。
- **前向进度（forward progress）**：按 hart 跟踪意味着每轮重试总有一个 hart 赢，于是 `LR/SC` 重试循环不会活锁。

#### 4.3.2 核心流程

预留表的生命周期（在 LLC bank 一拍内完成）：

```
LR  到达 LLC 且 hit  ──► reserve(hart, line)：安装/覆盖该 hart 的预留
SC  到达 LLC 且 hit  ──► check(hart, line) 决定成败；无论成败都 clear(hart, line)
任何写提交到 line    ──► invalidate(line, except=writer)：打破其他 hart 对该 line 的预留
```

`SC` 的成败判定为 `sc_fail = (op==SC) && !check(hart, line)`；成功则像普通 store-bearing AMO 一样写回并 `invalidate`，失败则只返回 1、不写。

> 关于预留表结构的一个**重要事实**：SimX 与 RTL 当前用了两种不同的结构（见 4.3.3），这是已知的实现差异，功能上都满足 RVA（允许假性失败）。

#### 4.3.3 源码精读

**SimX：按 hart 的 map。** `AmoUnit` 用一个 `unordered_map<hart_id, line_addr>` 维护预留，每个 hart 至多一项：

[amo_unit.cpp:30-39](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/amo/amo_unit.cpp#L30-L39) —— `reserve` 直接覆盖该 hart 的表项（不会碰到别的 hart），`check` 校验 `(hart, line)` 是否仍在。

失效与清除：

[amo_unit.cpp:41-57](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/amo/amo_unit.cpp#L41-L57) —— `invalidate(line, except)` 遍历删掉所有「行匹配且 hart≠except」的表项；`clear` 在 `SC` 时消费锁。类定义与不变量见 [amo_unit.h:33-70](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/amo/amo_unit.h#L33-L70)。

**RTL：line 索引的 BRAM 预留缓存。** RTL 的 `VX_amo_unit` 没有按 hart 建数组，而是用一组按「预留行低位地址」直接映像的站点（`RS_DEPTH = ≥NUM_RES_ENTRIES 的下一个 2 的幂`），`{hart, tag}` 存在同步 BRAM，valid 位存在可复位寄存器：

[VX_amo_unit.sv:77-93](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_amo_unit.sv#L77-L93) —— `RS_ADDRW/RS_DEPTH/RS_TAG_BITS` 把 line 地址切成 `{tag, index}`，按 index 寻址。

[VX_amo_unit.sv:99-117](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_amo_unit.sv#L99-L117) —— look-ahead BRAM `rs_store`（提前一拍读 `rs_idx_n`，使注册输出在 `SC` 提交拍正好就绪），LR 在 `rs_idx` 处写入 `{hart, tag}`。

命中判定与 clear：

[VX_amo_unit.sv:136-150](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_amo_unit.sv#L136-L150) —— `line_match`（槽位持有该 line）与 `own_match`（且就是该 hart 的）；`res_check = own_match`；`rs_clr` 在「任何对该 line 的写（`res_invalidate && line_match`）」或「`SC` 命中自身」时清 valid。

文件头注释（[VX_amo_unit.sv:16-35](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_amo_unit.sv#L16-L35)）明确说：这是一个**有界、固定容量的站点集（而非 per-hart 数组）**，冲突/容量驱逐是 RVA 合法的（`SC` 可因任何原因假性失败），前向进度是系统性质。站点容量由 `VX_CFG_AMO_RS_SIZE` 给出，默认按核/warp/bank 数推导（[VX_config.toml:128-130](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L128-L130)），注释亦写明「line-indexed in BRAM, eviction yields legal spurious SC failures」。

> **诚实标注：文档与已交付代码的差异。** [multicache_amo_coherence.md:133-140](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/multicache_amo_coherence.md#L133-L140) 的 §5.1 把 RTL 描述成「per-hart 直接索引数组（`NUM_HARTS` 个 `{valid, line_addr}` 槽）」，而上面读到的已交付 `VX_amo_unit.sv` 实际是 **line 索引的有界站点集**。SimX 侧（按 hart 的 map）与文档 §5.1 的 SimX 描述一致。也就是说，预留表的**结构**在 SimX（per-hart）与 RTL（line 索引）之间存在差异，但两者都 RVA 合法。这与 [atomic_memory_operations.md:181-186](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/atomic_memory_operations.md#L181-L186) §6.6 提到的「SimX↔RTL AMO 枚举值空间不同、trace parity 需要翻译层」是同类工具链缺口，不是正确性 bug。本讲一律以**已交付源码**为准。

#### 4.3.4 代码实践

**实践目标**：用 `tests/regression/amo` 的 `lrsc_counter` 用例理解预留表的「每轮必有一个赢家」。

1. 阅读 [kernel.cpp:115-136](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/amo/kernel.cpp#L115-L136) 的 `kernel_lrsc_counter`：每个 hart 用 `LR/SC` 重试循环把共享字 +1，`iters` 次。
2. 阅读 [testcases.h:208-224](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/amo/testcases.h#L208-L224) 的 `Test_LRSC_COUNTER::verify`：期望终值 `expected = num_harts * iters`。
3. 解释：即使多个 hart 竞争导致大量 `SC` 假性失败（被计入 `retries`），只要每轮至少一个 hart 成功，终值就一定正确。这正是「按 hart 跟踪 → 前向进度」的可观测后果。
4. **预期结果**：运行（见 §5 综合实践的命令）应打印 `lrsc_counter PASSED`，且 `retries` 缓冲里通常非零（说明确有竞争与假性失败被正确吸收）。

#### 4.3.5 小练习与答案

**练习 1**：为什么预留「按 hart」而非「一张全局 CAM」？
**答**：一张有界 CAM 在竞争 hart 数超过表容量时，某 hart 的预留可能在自己 `LR` 与 `SC` 之间被别的 hart 的 `LR` 挤掉，导致 `SC` 永不成功（活锁）。按 hart 跟踪则一个 hart 的预留只能被「对该 line 的写」打破，每轮重试至少一个 hart 赢，保证前向进度。

**练习 2**：`invalidate(line, except=writer)` 为什么要把写者自己排除？
**答**：写者（比如成功的 `SC` 或 `AMOADD`）的提交本身不应打破它自己刚建立的预留语义；且 `SC` 已通过 `clear` 显式消费了自己的预留。排除写者避免误清自身，其余 hart 对该 line 的预留才被打破。

---

### 4.4 多缓存一致性与 non-LLC 透传

#### 4.4.1 概念说明

当只有一级缓存（L1 即 LLC）时，「在 LLC 解决」就够了。一旦开了 L2（或 L2+L3），就冒出三个新问题（见 [multicache_amo_coherence.md:24-43](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/multicache_amo_coherence.md#L24-L43)）：

1. **LR/SC 在竞争下的前向进度**（4.3 已解决：按 hart 跟踪）。
2. **跨级原子正确性**：到达**非 LLC** bank 的原子必须被转发到 LLC（RMW 的真正拥有者），结果再路由回来，且不能留下陈旧/重复副本。
3. **发起者自洽**：一个 hart 发了原子后，再普通 load 同一地址，必须看到自己的更新——尽管它的 L1 是 write-through 且不硬件侦听。

Vortex 的整体一致性模型是 **GPU-弱 / PULL**（[multicache_amo_coherence.md:17-21](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/multicache_amo_coherence.md#L17-L21)）：原子在 LLC 解决，内层缓存 write-through 且不侦听；跨核的**普通数据**可见性由消费者在 acquire 点自行 invalidate 恢复，而非 LLC 主动回推（PUSH）。这与 NVIDIA / ARM Mali / PowerVR 一致。

核心不变量（[multicache_amo_coherence.md:96-112](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/multicache_amo_coherence.md#L96-L112)）：**原子绝不在任何内层（非 LLC）缓存留下副本。** 由此把行为分成两种 regime：

- **Regime A（仅原子共享）**：自旋锁、原子计数器。没有任何内层缓存持有该 line，每次原子在 LLC 串行。完全实现并验证。
- **Regime B（他核有普通 load 缓存的副本）**：那个副本在远端原子后变陈旧，由消费者的 acquire-invalidate 解决——**尚未实现**（见 [multicache_amo_coherence.md:260-269](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/multicache_amo_coherence.md#L260-L269)）。

#### 4.4.2 核心流程

同一个 cache 模块在每个层级被实例化，bank 内的 AMO 引擎按 `IS_LLC` 扮演两种互斥角色（[VX_cache_amo.sv:16-29](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache_amo.sv#L16-L29)）：

```
IS_LLC = 1（提交角色，仅 LLC bank）：
   在 S1 对驻留 line 做 RMW（4.2 的 ALU）+ 更新预留表（4.3）
   + 把结果作为「单在途的合成写回」注回 bank 流水线
   + commit_busy 关闭整个提交窗口，串行化不同 line 的 AMO

IS_LLC = 0（透传角色，所有非 LLC bank）：
   不本地做 RMW：把 AMO 非分配地转发到下游
   + 在下游 fill 时捕获结果字（ptw_word），不安装 line
   + 把结果作为 replay 路由回 core_rsp
   + 在入口做年龄排序，保证同 hart 同地址的程序序
```

非 LLC 透传的关键技巧：**复用既有 miss→fill→replay 路径**，而不是新建旁路。AMO 像普通 miss 一样占一个 MSHR 表项，只是用一个并行标志位 `amo_ptw_flag[]` 标记「这是个透传 AMO」，fill 到达时不安装 line、只把结果字存进 `ptw_word[]`，随后 replay 把结果送回请求者（[multicache_amo_coherence.md:151-167](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/multicache_amo_coherence.md#L151-L167)）。

发起者自洽：转发原子的同时，发起 cache 自失效本地副本，使下一次 load miss、从 LLC 重取，从而看到自己的更新（[multicache_amo_coherence.md:169-178](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/multicache_amo_coherence.md#L169-L178)）。

#### 4.4.3 源码精读

**RTL：两种角色的分派（`generate` 二选一）。** `VX_cache_amo` 用 `if (IS_LLC != 0)` 把提交 datapath 与透传 datapath 分开，综合器只保留被选中者：

[VX_cache_amo.sv:122](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache_amo.sv#L122) 起的 `g_commit` 块（`if (IS_LLC != 0)`）是 LLC 提交角色。其中的预留/写触发：

[VX_cache_amo.sv:304-318](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache_amo.sv#L304-L318) —— `sc_fail`、`do_store`、`res_reserve`(LR)/`res_clear`(SC)/`res_invalidate`（`do_store_st1 || do_write_st1`，即 AMO 提交与普通 write-through 都打破别人预留）。

[VX_cache_amo.sv:565-597](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache_amo.sv#L565-L597) —— `g_passthru` 块：`ptw_flag[]`/`ptw_wsel[]`/`ptw_word[]` 在分配时标记、在 fill 时捕获结果字并清标志。

入口年龄排序（保证同 hart 同地址程序序，使 AMO/load 互相不抢跑）：

[VX_cache_amo.sv:605-609](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache_amo.sv#L605-L609) —— `amo_input_defer`（AMO 等待同 line 的 load fill）与 `load_input_defer`（load 等待同 line 的 AMO 透传），合流为 `req_input_defer`。

bank 只在 `AMO_ENABLE` 时实例化该引擎，且提交角色的合成写回仅在 LLC 生效（[VX_cache_bank.sv:833](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/cache/VX_cache_bank.sv#L833) 实例化 `VX_cache_amo`，非 AMO 构建走 `:913` 的 `g_no_amo`）。

**SimX：两条对应路径。** 请求类型枚举里专门有 `AmoProbe`：

[cache.cpp:337-343](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/cache.cpp#L337-L343) —— `ReqType { None, Fill, Replay, Core, AmoProbe }`，`AmoProbe` 即非 LLC 透传。

LLC 提交 `commitAmo`（在 `config_.is_llc` 时调用）：

[cache.cpp:991-1010](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/cache.cpp#L991-L1010) —— 由 `bank_req.byteen` 的 popcount 推位宽、从 `bank_req.data` 取 `rhs`，算 `sc_fail`/`do_store`。

[cache.cpp:1036-1067](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/cache.cpp#L1036-L1067) —— `LR`→`reserve`、`SC`→`clear`；`do_store` 时把 `rmw.new_word` 按 `byte_off` 合并进 line，write-through 则额外发一笔 `ST` 到下游，最后 `invalidate(line, except=hid)` 打破他人预留。

非 LLC 透传 `AmoProbe` 处理：

[cache.cpp:1136-1142](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/cache.cpp#L1136-L1142) —— 断言「`AmoProbe` 不该出现在 LLC」；先 probe 本地 line（脏则写回、命中则失效该 sector），再把原始 AMO MemReq 转发到下游，响应回 `core_rsp_out` 但**不安装 fill**。

`AmoUnit` 仅在 `config_.is_llc` 的 bank 上活跃（构造见 [cache.cpp:615](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/cache.cpp#L615)），与 RTL「只在 LLC 实例化提交逻辑」对应。

#### 4.4.4 代码实践

**实践目标**：用 `self_consistency` 用例观察「发起者自洽」如何被透传路径保证。

1. 阅读 [kernel.cpp:250-258](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/amo/kernel.cpp#L250-L258)：每个 hart 在自己**私有**的 64B line 上做「普通 load（缓存该 line）→ 原子 +1 → 普通 load（必须看到 +1）」。
2. 阅读 [testcases.h:321-340](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/amo/testcases.h#L321-L340)：期望每个 hart 的 `per_hart[hid] == 1`；若非 LLC 缓存没在 AMO 转发时自失效，post-AMO 的 load 会命中陈旧副本、读到 0。
3. 在多核 + L2 配置下运行（见 §5），对照 [multicache_amo_coherence.md:169-178](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/multicache_amo_coherence.md#L169-L178) 解释：AMO 透传时发起 cache 自失效本地副本，使 post-AMO load miss → 从 LLC 重取到 +1 后的值。
4. **预期结果**：`self_consistency PASSED`。该用例正是设计文档 §9 点名用来「卡掉缺乏本地失效的设计」的回归。

#### 4.4.5 小练习与答案

**练习 1**：为什么非 LLC bank 复用「miss→fill→replay」路径，而不是新建一条 AMO 旁路？
**答**：复用既有 MSHR 机制能零新增状态机地拿到「分配表项→等下游响应→回放」的能力；只需一个并行标志位 `amo_ptw_flag` 区分「这个 fill 是透传 AMO 的结果，不要安装 line」。这最小化了改动，且天然与既有流水线时序对齐，利于 model parity。

**练习 2**：`atomic_critical` 用例为什么在多核下被跳过（[main.cpp:121-131](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/amo/main.cpp#L121-L131)）？
**答**：它在临界区里用了**非原子**的 load/store。锁本身（`amoswap`）跨核正确，但临界区内的普通计数器读写需要 L1↔L1 一致性——而 Vortex 的 L1 私有且不硬件侦听（Regime B 尚未实现），其他核会读到陈旧 L1 副本。所以该用例只在单核跑，多核跳过。

---

## 5. 综合实践

**任务**：在多核 + L2 配置下跑通 `tests/regression/amo`，并用本讲知识解释「一次 `amoadd` 在多缓存环境下为何最终正确」。

1. **运行**（在已 `configure` 的 build 目录，默认 SimX）：

   ```bash
   ./ci/blackbox.sh --driver=simx --app=amo --cores=4 --l2cache
   ```

   或直接在回归框架里跑（`tests/regression/amo` 的 `PROJECT:=amo`，默认每 hart 32 次迭代，见 [tests/regression/amo/Makefile:14](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/amo/Makefile#L14)）。命令与具体退出码**待本地验证**（取决于工具链是否就绪），但判据是主机打印每个用例 `PASSED`。

2. **跟踪一次 `amoadd`**（用 [kernel.cpp:20-25](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/amo/kernel.cpp#L20-L25) 的 `kernel_amoadd`）：所有 hart 对同一字 `__atomic_fetch_add(+1)`。画出它经过的路径：

   ```
   core 的 LSU（打包 amo_req，rw=0）
     → core 的 L1（非 LLC：AmoProbe 自失效本地副本 + 转发，不安装 line）
       → cluster 的 L2（LLC：commitAmo 做 RMW，写回 / write-through，invalidate 他人预留）
         → 响应旧值沿原路回 rd
   ```

3. **解释正确性**：因为每一次 `amoadd` 都被汇聚到 L2（LLC）的同一 bank 串行 RMW，且 L1 write-through、AMO 不在 L1 留副本，所以 N 个 hart 各 `iters` 次加一，终值必为 `num_harts * iters`（[testcases.h:66-74](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/amo/testcases.h#L66-L74)）。这正是 Regime A「零新增一致性流量」的体现。

4. **回答实践题的两问**：
   - **如何在多缓存下保持正确**：靠「在 LLC 解决 + 内层 write-through + 原子不留内层副本」三件事合力，串行点唯一。
   - **为何要路由到特定缓存点**：只有 LLC 是全局唯一可见的串行点；若任意 L1 就地 RMW，并发原子会互相覆盖。AMO 必须被送到 LLC（非 LLC 则透传），RMW 才有序。

> 若本地无法运行，本实践可退化为「源码阅读型」：只做第 2–4 步的调用链跟踪与推理，结论不变。

## 6. 本讲小结

- **总纲**：Vortex 的 AMO 一律在末级缓存（LLC）bank 上做单拍 RMW，靠一个纯组合 ALU 内核（RTL `VX_amo_alu` / SimX `amo_compute`）完成，9 个 op 语义逐位一致。
- **铁律**：AMO 使能即强制其上方所有缓存 write-through（顶层 `STATIC_ASSERT`），否则 LLC 看不全写、`SC` 可能假性成功；AMO 走 load 通路（`rw=0`），即便 `SC` 也是。
- **LR/SC 预留**：按 hart 跟踪，前向进度有保证；任何对该 line 的已提交写都打破他人预留。SimX 用 per-hart map，RTL 用 line 索引的有界 BRAM 站点集——两者都 RVA 合法，但结构存在差异（已知工具链缺口）。
- **多缓存一致性**：GPU 弱 / PULL 模型；非 LLC bank 透传（复用 MSHR miss→fill→replay，不安装 line），发起者靠自失效保证自洽；核心不变量是「原子绝不在内层缓存留副本」。
- **两种 regime**：Regime A（仅原子共享，如自旋锁/计数器）已完全实现验证；Regime B（他核普通 load 的陈旧副本，需 acquire-invalidate）尚未实现。
- **parity 与状态**：SimX 与 RTL 逐模块对应（`commitAmo`↔`g_commit`、`AmoProbe`↔`g_passthru`）；`tests/regression/amo` 在 1/L2/L3 多种配置下、SimX 与 rtlsim 均通过；xrt 多核签收与 bit-level trace parity 仍待补。

## 7. 下一步学习建议

- **横向读上层 API**：AMO 在设备侧的 C/C++ 入口是 GCC `__atomic_*` 内建与内联汇编（见 [kernel.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/amo/kernel.cpp)），它们被 RISC-V clang 编码成 `0x2F` 指令。可结合 [u4-l2（SIMT 控制指令）](u4-l2-kernel-intrinsics.md) 看这些指令如何流入 LSU。
- **纵向读 LSU 流水线**：AMO 的 sideband 由 [VX_lsu_slice.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_lsu_slice.sv) 打包，详见 [u8-l4（LSU 流水线设计）](u8-l4-lsu-pipeline.md)；建议把本讲的「AMO 走 load 通路」与 u8-l4 的 AGU/slice 对照阅读。
- **深入一致性取舍**：精读 [multicache_amo_coherence.md §3 与 §7](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/multicache_amo_coherence.md#L77)，理解为何拒绝了 PUSH+目录式方案，以及 Regime B acquire-invalidate 计划如何复用既有 flush walk。
- **RTL 验证方向**：[atomic_memory_operations.md §6](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/atomic_memory_operations.md#L164) 列出了若干「拟做未做」项（AMO unit testbench、性能计数器、rtlsim/FPGA 签收），是值得接手的二次开发切入点。
