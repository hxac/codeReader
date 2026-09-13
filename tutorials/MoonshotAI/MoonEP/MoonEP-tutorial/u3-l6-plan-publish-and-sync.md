# 计划发布：组播广播、Phase D 暂存与同步原语

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 **rank0 的规划结果如何"回家"**：哪部分数据走 `multimem.st` 组播一次性扇出到所有 rank 的 PLAN 区（发布的数据范围与时机），哪部分由各 rank 用 `cp.async.bulk` 从 rank0 的 chunk 单播拉取（Phase D 暂存），以及为什么要这样分工。
2. 掌握 **两种自复位屏障**：库内单 kernel 多 CTA 用的软件栅格屏障 `grid_sync`（计数器 + 哨兵位翻转），和跨 NVLink 的 `cross_rank_barrier`（双相位槽 + 加减轮转），并能解释**为什么它们都不需要每轮清零**——sentinel 位翻转 / 相位加减抵消如何做到这一点。
3. 理解 **`inter_rank_sync` 内核为什么放在 planning 之前**：它对齐的是什么、防的是什么。
4. 能写出两个屏障的到达/离开协议伪代码，并用纯 Python 模拟验证"永不清零"性质。

本讲是规划器单元（单元 3）的收官：u3-l2 算出了迁移矩阵 `z`，u3-l3 展开成 `alloc` 与 padded 段布局，u3-l4 算出了 `dst` 与 `src_info`。但这些结果目前只躺在 **rank0 的 chunk** 里（或只在 rank0 的寄存器里算完）。本讲回答最后一个问题：**这些结果如何安全、高效地出现在每个 rank 需要它们的地方**。

## 2. 前置知识

### 2.1 回顾：meta_buf 的 PLAN 区与三张查找表

u2-l4 讲过 meta_buf 的七段布局，本讲反复用到其中两段：

- **BARRIER 区**（偏移 `BARRIER_OFF`）：每 rank chunk 内 3 个 int32 的屏障槽；
- **PLAN 区**（偏移 `PLAN_OFF`）：rank0 写规划结果的区域，内部再切成子区，见 [moonep/planning.py:546-553](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L546-L553)：

```python
ALLOC_SUB = 0
TPE_SUB = E * R
EOFF_SUB = 2 * E * R
CU_SUB = 3 * E * R
ZFR_SUB = CU_SUB + R * (E + B)
ETC_SUB = ZFR_SUB + 2 * R * (E + B)
STATS_SUB = ETC_SUB + R * B
```

其中 **ALLOC（alloc_cumsum，形状 [E,R]）、TPE（tpe_cumsum，[R,E]）、EOFF（expert_offsets，[R,E]）** 是三张"人人都要查全量"的查找表——u3-l4 的 passB 里，每个 rank 都要对任意 `(expert, rank)` 组合做二分查找与取基址。而 **CU（cu_seqlens，[R,E+B]）、ZFR（zero_fill，[R,(E+B)*2]）** 是按目的 rank 切行的表，每个 rank 只消费自己那一行；**ETC（experts_to_copy，[R,B]）** 则作为 `MoonEPCommPlan.experts_to_copy` 字段，每个 rank 都要持有**全量**副本（u3-l1 讲过它的形状契约）。

还有两个视角要分清（u2-l2/u2-l4）：

- `meta`：**单播合并视图**，`[R * meta_stride]`，第 `r` 段物理上驻留在 rank r 的 GPU 上，任何 rank 都能经 NVLink 读写全部段；
- `mc`：**组播虚拟地址**（`ctx['meta_mc']`，由 [moonep/api.py:362](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L362) 的 `create_nvl_dist_multicast_tensor` 创建），向它写一次，NVSwitch 硬件把这次写**扇出到所有 rank chunk 的同一偏移**。

### 2.2 release / acquire：不用锁的"写完了吗"

本讲源码里到处是成对的原语，它们的含义用一句话说：

- **release 写**（`atom.add.release.gpu` / `red.release.sys`）：我这条原子写之前发出的所有写，对看到这条写的读者**一并可见**——"写完再发布"；
- **acquire 读**（`ld.acquire.gpu` / `ld.acquire.sys`）：我看到你的 release 写之后，我后续的读都能拿到你发布前的一切——"确认收到再往下走"。

后缀 `.gpu` 与 `.sys` 是可见范围：`.gpu` 限本 GPU（grid_sync 用），`.sys` 覆盖整个系统、可跨 NVLink（cross_rank_barrier 用）。一对 release/acquire 构成一个** happens-before 边**，这是所有屏障正确性的基石。

### 2.3 CUDA 的"代理"与 fence.proxy（通俗版）

GPU 上访问同一块显存的指令分属不同"代理"（proxy）：普通 load/store 走 **generic 代理**；TMA（`cp.async.bulk`）走 **async 代理**；写组播地址（`multimem.st`）走 **alias 代理**。不同代理之间的写**默认不保证互相可见**——即使物理地址相同。

`fence.proxy.*` 就是代理之间的桥：

- `fence.proxy.alias`：让我之前经**组播地址**写的内存，对之后经**单播别名**访问它的读者一致化；
- `fence.proxy.async.global`：让我刚**获取到**的数据（generic 代理视角），对我随后发出的 **TMA** 访问可见。

本讲会看到 cross_rank_barrier 的入口/出口恰好各放一座桥，位置与 planning 的组播发布、Phase D 的 TMA 拉取严丝合缝。

### 2.4 mbarrier 与 cp.async.bulk（最小补课）

- `cp.async.bulk`（G2S 方向）：**单线程**发起的 DMA 式批量拷贝，全局内存 → 共享内存，要求源、目的地址 16 字节对齐且字节数是 16 的倍数；完成后自动在指定的 **mbarrier** 上"交易到达"（transaction arrive）`size` 字节。
- `mbarrier_arrive_and_expect_tx(mbar, bytes)`：声明"我预计将有 `bytes` 字节到达"，同时到达一次；
- `mbarrier_wait(mbar, 0)`：等所有声明的到达凑齐。

详细展开在 u4-l1，本讲只把它当作"带完成通知的异步 memcpy"。

## 3. 本讲源码地图

| 文件 | 本讲涉及的部分 | 作用 |
|------|----------------|------|
| `moonep/planning.py` | `multimem_st_v4`、发布循环、`_pd_*` 四个 helper、Phase D smem 布局、G2S 发起与写回、三处 `cross_rank_barrier` 调用 | 组播发布的写入侧；Phase D 暂存的全部逻辑；屏障的使用方 |
| `moonep/_common.py` | `GRID_SYNC_TAG`/`BARRIER_TIMEOUT_CYCLES` 常量、`grid_sync`、`cross_rank_barrier`、`cp_async_bulk_g2s`、若干原子/访存 PTX 封装 | 本讲主角：两种自复位屏障与 TMA 封装 |
| `moonep/inter_rank_sync.py` | `InterRankSyncKernel`、`_num_threads_for_ranks`、`launch_inter_rank_sync` | planning 之前的预同步小内核 |
| `moonep/api.py` | `grid_sync_bar` 的分配注释、`_dispatch_impl` 里 `launch_inter_rank_sync` → `launch_planning` 的顺序 | 屏障计数器的宿主侧分配与调用顺序证据 |

## 4. 核心概念与源码讲解

### 4.1 组播发布：rank0 的一次写入、全 rank 落盘

#### 4.1.1 概念说明

Phase A/B/C 全部跑在 rank0 上，产出的三张查找表（ALLOC/TPE/EOFF）此刻只在 rank0 chunk 的 PLAN 区里。而 passB 在**每个 rank** 上都要执行（u3-l4），且每次二分查找读的都是任意 `(expert, rank)` 组合——**每个 rank 都需要这三张表的全量副本**。

分发方式有两种朴素选择：

1. 每 rank 用 NVLink 从 rank0 chunk 单播拉全量：每 rank 拉 \(3ER\) 个 int32，总共 \(R \cdot 3ER\) 个字的 NVLink 流量，且这些读全部压在 rank0 一端；
2. rank0 执行 R 次远程写，每个 peer 一份：R 次 NVLink 单播。

MoonEP 选第三种：**组播**。rank0 往 `mc`（组播虚拟地址）写一次，NVSwitch 在硬件层面把这次写扇出到所有 R 个 chunk 的同一偏移。发布成本是 \(nvec = 3ER/4\) 条向量指令，**与 R 无关**。

而 CU/ZFR/ETC 这些"各取所需"的切片**不走组播**：组播会强行给每个 rank 都写一份它不需要的别人的行，纯浪费；它们由 Phase D（4.2 节）各自单播拉取。这就是"**人人要的全量表用组播，各取所需的切片用单播拉**"的分工。

直觉比喻：校长（rank0）算完全校分班方案后，"全校教师对照表"（三张查找表）贴到每个班的公告栏（组播）；而每个班只去教务处复印**自己班的名单**（Phase D 单播拉取）。

#### 4.1.2 核心流程

发布发生在 rank0 完成全部 Phase C 计算之后、其它 rank 尚在跑排序（run_c1）的同时：

```
rank0：Phase A → Phase B → Phase C 写满自己 chunk 的 PLAN 区
        │
        ├─ grid_sync                       # 等 rank0 上所有 CTA 的 Phase C 写完成
        ├─ for i in [0, nvec):             # nvec = 3ER // 4
        │     读 meta[PLAN_OFF + i*4 .. +3]   # 本地 chunk 的 4 个 int32
        │     multimem_st_v4(mc + PLAN_OFF + i*4, ...)  # 一次写扇出到所有 rank
        └─ 继续走 src_info 清零 → cross_rank_barrier(979 行)  # 见 4.4 节的可见性
```

关键时序点：**发布本身没有任何紧跟的屏障**。其它 rank 之所以不会读到"还没写完"的数据，是因为它们要过 979 行的 `cross_rank_barrier` 之后才进入 passB 读本地 PLAN 副本——可见性由该屏障的 release/acquire 链条传递（4.4.2 详述）。

#### 4.1.3 源码精读

向量化的组播写指令封装在 [moonep/planning.py:148-158](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L148-L158)：

```python
@dsl_user_op
def multimem_st_v4(addr_i64, x, y, z, w, *, loc=None, ip=None) -> None:
    llvm.inline_asm(
        None,
        [addr_i64, ...],
        "{\n\t.reg .u64 g;\n\t cvta.to.global.u64 g, $0;\n\t"
        "multimem.st.relaxed.sys.global.v4.f32 [g], {$1, $2, $3, $4};\n\t}",
        ...)
```

这段内联 PTX 把 4 个 int32 按位当作 `v4.f32` 向量写入组播地址：`.f32` 只是位型（数据是 int32，按位搬运语义不变），`.v4` 让一次写覆盖 16 字节、NVLink 事务数除以 4，`.sys` 域保证跨 rank 可见性顺序。

发布循环本体在 [moonep/planning.py:954-960](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L954-L960)，位于 `if rank == 0:` 块的末尾：

```python
grid_sync(bar_p, num_sms, tid)
nb = 3 * E * R; nvec = nb // 4
for i in cutlass.range(pid * num_threads + tid, nvec, num_sms * num_threads):
    a0 = meta[PB + i * 4 + 0]; a1 = meta[PB + i * 4 + 1]
    a2 = meta[PB + i * 4 + 2]; a3 = meta[PB + i * 4 + 3]
    addr = (mc.iterator + (PLAN_OFF + i * 4)).toint()
    multimem_st_v4(addr.ir_value(), a0, a1, a2, a3)
```

这段代码做的事情：前面的 `grid_sync` 先等 rank0 上所有 CTA 把 Phase C 的结果写完（发布不能抢跑）；随后 grid-stride 循环把 PLAN 区前 \(nb = 3ER\) 个 int32——恰好是 ALLOC/TPE/EOFF 三个子区（[moonep/planning.py:546-548](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L546-L548)，`EOFF_SUB = 2 * E * R` 是第三个子区的起点，`CU_SUB = 3 * E * R` 紧随其后）——每 4 个一组从本地 chunk 读出、经 `mc` 组播地址写回。**一次写在硬件层扇出到所有 R 个 chunk 的 `PLAN_OFF + i*4` 偏移**，包括 rank0 自己（写回相同值，无害）。

下游的读法在 [moonep/planning.py:986-1001](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L986-L1001)：每个 rank 把 passB 要用的三个视图建立在 `plo = rank * ms + PLAN_OFF`，即**自己 chunk 的组播副本**上：

```python
plo = rank * ms + PLAN_OFF
tpe_cumsum_view = cute.make_tensor(meta.iterator + (plo + TPE_SUB), ...)
alloc_cumsum_view = cute.make_tensor(meta.iterator + (plo + ALLOC_SUB), ...)
expert_off_view = cute.make_tensor(meta.iterator + (plo + EOFF_SUB), ...)
```

因此 passB（[moonep/planning.py:1048-1073](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1048-L1073)，u3-l4 已精读）里的每一次二分查找与基址读取都是**本地显存访问**，不产生任何 NVLink 流量。

#### 4.1.4 代码实践

**实践目标**：量化"组播大矩阵 + 单播拉切片"这笔账，并确认发布范围。

**操作步骤**（纯 Python，无需 GPU）：

1. 写脚本 `plan_publish_calc.py`，对 `E=256, R=8, B=E//R=32` 的配置计算：`nb`、`nvec`、发布总字节数；
2. 再按 4.2 节将讲的 Phase D 单播拉取量（本 rank 的 CU 行 `E+B`、ZFR 行 `2*(E+B)`、ETC 全量 `R*B` 个 int32）求和，与"不组播、每 rank 全量拉 PLAN"（至少 \(3ER\) 个 int32）对比；
3. 打开 [moonep/planning.py:546-549](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L546-L549) 核对 `nb = 3 * E * R` 恰好覆盖 ALLOC+TPE+EOFF、止于 `CU_SUB`。

```python
# 示例代码：plan_publish_calc.py
E, R, B = 256, 8, 32
nb = 3 * E * R            # 组播发布的 int32 数（ALLOC+TPE+EOFF）
nvec = nb // 4            # multimem_st_v4 指令条数
unicast_pull = (E + B) + 2 * (E + B) + R * B   # Phase D 每 rank 单播拉取
print(f"组播: {nvec} 条 v4 指令, {nb*4} 字节, 与 R 无关")
print(f"若不组播, 每 rank 还需额外单播拉 {nb} 个 int32")
print(f"Phase D 每 rank 实际单播拉取 {unicast_pull} 个 int32")
```

**需要观察的现象**：`nb = 6144`，是每 rank 单播拉取量（1120）的 5.5 倍——组播把这部分最大的流量变成了与 R 无关的一次硬件扇出。

**预期结果**：脚本输出上述数字；`nb` 恰等于 `EOFF_SUB 之后到 CU_SUB` 的子区总长。本实践为纯算术，无需 GPU，可直接本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 CU（cu_seqlens）和 ZFR 不组播，而 ETC 要每个 rank 拉全量 `R*B`？

**答案**：CU/ZFR 是按目的 rank 切行的表，每 rank 只消费自己那一行，组播会把别的 rank 的行强行写进每个 chunk，纯属浪费；而 `experts_to_copy` 是 `MoonEPCommPlan` 的 `(R, B)` 全量字段（u3-l1 的形状契约），dispatch 与用户侧需要知道所有 rank 各自预取了哪些专家，所以每 rank 都要完整副本。**数据走哪条通道由下游的消费范围决定**。

**练习 2**：发布循环为什么用 `v4`（128 位）而不是标量 store？

**答案**：一条 `multimem.st.v4` 搬 16 字节，NVLink/组播事务数除以 4；且 PLAN 区起点按 16 字节对齐（u2-l4 的区间对齐规则），`i*4` 的元素偏移保证每条向量指令地址都是 16 字节对齐。

**练习 3**：R=1 时这段组播代码还有意义吗？

**答案**：仍然执行，但退化为"rank0 写给自己的 chunk"（扇出集合只有自己），值与它 Phase A/B/C 直接写的相同。统一路径避免了为 R=1 单写分支；此时性能无关紧要，因为只有一个 chunk。

### 4.2 Phase D 暂存：用 TMA 把自己的切片拉回家

#### 4.2.1 概念说明

组播解决了三张大表，剩下的**本 rank 切片**（自己的 cu_seqlens 行、自己的 zero_fill 行、全量 experts_to_copy、自己的 remote_stats）还躺在 rank0 chunk 的 PLAN 区里。Phase D 的任务就是把它们搬进本 rank 的**输出张量**（`cu_seqlens`、`zero_fill_ranges`、`experts_to_copy`、`remote_stats`，即 u3-l1 讲过的 `allocate_planning_outputs` 分配的那批）。

搬运工具选了 TMA（`cp.async.bulk` G2S）而不是逐元素 load/store，动机有两个：

1. **异步重叠**：G2S 发起后立刻返回，拷贝由 TMA 引擎执行。Phase D 把拷贝**在 passB 之前发起**、在 passB 与 dst 规范化都跑完之后才 `mbarrier_wait` 收割——搬运时间几乎完全被计算覆盖；
2. **合并访问**：连续段一次 DMA 搬完，比逐 int32 的散访问省事务。

难点是对齐：`cp.async.bulk` 要求源、目的 16 字节对齐、字节数 16 的倍数，而逻辑切片的起点（如 `CU_SUB + rank*(E+B)`）不保证是 4 个 int32 的倍数。解法是**信封对齐**：从 `floor(src/4)*4` 开始拷，多带的头尾 int 落在共享内存 stage 的 padding 里，写回时再按偏差重新对齐。

#### 4.2.2 核心流程

```
（每个 rank，非 rank0 也在做；与 rank0 的组播发布、自己的 run_c1 并行）
1. 过 cross_rank_barrier(979 行)              # rank0 的 Phase C 输出对 TMA 可见（4.4 节）
2. _pd_cta_slice：把 [0, E+B) 与 [0, R*B) 切成每 CTA 一段（32 对齐）
3. tid==0：mbarrier_init + arrive_and_expect_tx(三段信封总字节)
   并发出三条 cp.async.bulk G2S：
      cu  段：rank0 chunk 的 PLAN 区 → smem stage（信封对齐）
      zfr 段：同上（长度 ×2）
      etc 段：全量 R*B
4. 所有线程去跑 passB（计算 dst、远程写 src_info）+ dst 规范化
5. mbarrier_wait(pd_mbar, 0)                  # TMA 完成，数据在 smem stage 里
6. 写回：cu_seqlens / zero_fill_ranges / experts_to_copy ← stage[stage_bias + 局部下标]
        pid==0 顺带直接 load 本 rank 的 remote_stats 两个数
```

#### 4.2.3 源码精读

**CTA 分段**：[moonep/planning.py:305-315](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L305-L315) 把总量按 32 对齐切成每 CTA 连续一段，`copy_begin` 的 clamp 只处理 pid 越过有效段的空 CTA：

```python
per_cta = cute.round_up(cute.ceil_div(total, num_sms), tile)
begin = pid * per_cta
end = cutlass.min(begin + per_cta, total)
copy_begin = cutlass.min(begin, total)
```

该函数每段连续分配、保持 gmem 写回合并（coalesced），注释明说"Phase D outputs are small"——输出小，所以按段切而非按元素跨步。

**信封三件套**：[moonep/planning.py:318-334](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L318-L334) 定义偏差与对齐长度——`_pd_stage_bias` 取逻辑起点在 16 字节信封内的偏差（`src_begin & 3`），`_pd_aligned_ints` 把"偏差 + 逻辑长度"向上取整到 4 的倍数：

```python
@cute.jit
def _pd_stage_bias(src_begin):
    return src_begin & 3

@cute.jit
def _pd_aligned_ints(src_begin, logical_count):
    aligned_ints = 0
    if logical_count > 0:
        aligned_ints = cute.round_up(_pd_stage_bias(src_begin) + logical_count, 4)
    return aligned_ints
```

**发起 G2S**：[moonep/planning.py:337-350](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L337-L350) 从 16 字节对齐的源地址（`src_begin - stage_bias`）发起 bulk 拷贝，字节数 `aligned_ints * 4`，完成时自动在 mbarrier 上交易到达：

```python
src_aligned = src_begin - _pd_stage_bias(src_begin)
aligned_ints = _pd_aligned_ints(src_begin, logical_count)
cp_async_bulk_g2s(
    smem_stage.iterator.toint().ir_value(),
    (meta.iterator + src_aligned).toint().ir_value(),
    Int32(aligned_ints * 4).ir_value(),
    mbar.toint().ir_value(),
)
```

`cp_async_bulk_g2s` 本体是 [moonep/_common.py:414-435](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L414-L435) 对 `cp.async.bulk.shared::cluster.global.mbarrier::complete_tx::bytes` 的内联 PTX 封装——单线程指令，完成自动向 mbarrier 报 `size` 字节。

**共享内存 stage 布局**：[moonep/planning.py:569-585](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L569-L585) 为三个 stage 预留空间，每段额外 `+4` 个 int 容纳信封可能多带的头/尾元素，再各自对齐到 4：

```python
PHASE_D_TILE = 32
PHASE_D_GROUPS_PER_CTA = cutlass.const_expr(
    align_up((E + B + num_sms - 1) // num_sms, PHASE_D_TILE))
PD_CU_OFF = 0
PD_CU_LEN = cutlass.const_expr(align_up(PHASE_D_GROUPS_PER_CTA + 4, 4))
PD_ZFR_OFF = cutlass.const_expr(PD_CU_OFF + PD_CU_LEN)
PD_ZFR_LEN = cutlass.const_expr(align_up(2 * PHASE_D_GROUPS_PER_CTA + 4, 4))
PD_ETC_OFF = cutlass.const_expr(PD_ZFR_OFF + PD_ZFR_LEN)
PD_ETC_LEN = cutlass.const_expr(align_up(PHASE_D_ETC_PER_CTA + 4, 4))
```

**源偏移的选择**：[moonep/planning.py:1025-1028](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1025-L1028) 决定每段从 rank0 chunk（无 `rank * ms` 项，行选择用矩阵内的行偏移）拉什么：

```python
cu_src_begin = PB + CU_SUB + rank * pd_group_count + group_copy_begin
zfr_src_begin = PB + ZFR_SUB + (rank * pd_group_count + group_copy_begin) * 2
etc_src_begin = PB + ETC_SUB + etc_copy_begin     # 注意：无 rank 项，拉全量 R*B
zfr_copy_count = group_copy_count * 2
```

**发起与收割**：[moonep/planning.py:1030-1047](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1030-L1047) 中 tid==0 初始化 mbarrier、一次性声明三段信封的总字节数、连发三条 G2S——都发生在 passB 之前：

```python
if tid == 0:
    pd_stage_bytes = (
        _pd_aligned_ints(cu_src_begin, group_copy_count)
        + _pd_aligned_ints(zfr_src_begin, zfr_copy_count)
        + _pd_aligned_ints(etc_src_begin, etc_copy_count)
    ) * 4
    cute.arch.mbarrier_arrive_and_expect_tx(pd_mbar, pd_stage_bytes)
    _pd_issue_g2s(meta, s_pd_cu_stage, cu_src_begin, group_copy_count, pd_mbar)
    _pd_issue_g2s(meta, s_pd_zfr_stage, zfr_src_begin, zfr_copy_count, pd_mbar)
    _pd_issue_g2s(meta, s_pd_etc_stage, etc_src_begin, etc_copy_count, pd_mbar)
```

随后所有线程先去跑 passB（[1048-1073](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1048-L1073)）与 dst 规范化（[1083-1113](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1083-L1113)），最后 [moonep/planning.py:1114-1130](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1114-L1130) 才等 mbarrier 并按 stage_bias 写回：

```python
cute.arch.mbarrier_wait(pd_mbar, 0)
cu_stage_bias = _pd_stage_bias(cu_src_begin)
...
for group_idx in cutlass.range(group_begin + tid, group_end, num_threads):
    local_group = group_idx - group_begin
    cu_seqlens[group_idx] = s_pd_cu_stage[cu_stage_bias + local_group]
    zfr[group_idx * 2] = s_pd_zfr_stage[zfr_stage_bias + local_group * 2]
    zfr[group_idx * 2 + 1] = s_pd_zfr_stage[zfr_stage_bias + local_group * 2 + 1]
for etc_idx in cutlass.range(etc_begin + tid, etc_end, num_threads):
    experts_to_copy[etc_idx] = s_pd_etc_stage[etc_stage_bias + (etc_idx - etc_begin)]
```

写回下标从 `stage_bias + 局部下标` 读——正是信封头部的偏差修正。`remote_stats` 只有两个 int32，不值得走流水线，由 pid==0 用普通 load 直接从 rank0 chunk 的 STATS 子区取（[1120 行附近](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1118-L1120)）。

#### 4.2.4 代码实践

**实践目标**：亲手验证信封对齐公式的正确性（16 字节约束）。

**操作步骤**（纯 Python）：

```python
# 示例代码：pd_envelope_check.py
def stage_bias(src_begin):        # 对应 _pd_stage_bias
    return src_begin & 3

def aligned_ints(src_begin, count):   # 对应 _pd_aligned_ints
    if count <= 0:
        return 0
    return (stage_bias(src_begin) + count + 3) // 4 * 4

for src, cnt in [(7, 5), (12, 6), (0, 33), (289, 32)]:
    b = stage_bias(src)
    a = aligned_ints(src, cnt)
    src_aligned = src - b
    assert src_aligned % 4 == 0                 # 源地址 4 对齐 → 字节 16 对齐
    assert a % 4 == 0                           # 字节数是 16 的倍数
    assert b + cnt <= a                         # 信封装得下逻辑数据
    print(f"src={src:4d} cnt={cnt:3d} → 从 {src_aligned} 拷 {a} ints "
          f"({a*4}B), 逻辑段 [{src}, {src}+{cnt}) 落在 stage[{b}:{b+cnt}]")
```

**需要观察的现象**：例如 `src=7, cnt=5` 时从 4 拷 8 个 int32（32 字节），逻辑数据落在 stage 下标 `[3, 8)`。

**预期结果**：所有断言通过；打印出每段"物理信封 vs 逻辑区间"的对应关系。纯算术，可本地验证。

#### 4.2.5 小练习与答案

**练习 1**：三段拷贝为什么共用一个 mbarrier，而不是各配一个？

**答案**：`arrive_and_expect_tx` 一次性声明三段信封的总字节数，三条 `cp.async.bulk` 各自向同一 mbarrier 交易到达自己的字节数；一次 `mbarrier_wait` 同时覆盖三段。少两个 mbarrier 的 smem 与初始化开销，等待逻辑也只需一遍。

**练习 2**：如果把 G2S 发起挪到 passB 之后，会怎样？

**答案**：功能仍正确，但失去重叠——passB 期间 TMA 引擎闲置，写回阶段要全程等待拷贝完成。当前"先发起、后收割"的顺序正是为了让 NVLink 拉取与 passB 的二分查找计算**在时间上叠在一起**。

**练习 3**：`PD_CU_LEN = align_up(GROUPS_PER_CTA + 4, 4)` 里那个 `+4` 是干什么的？

**答案**：信封从 16 字节对齐边界起拷，头最多多带 3 个 int、尾因字节数取整最多多带 3 个 int，合计不超过 6；`+4` 提供的是头部 padding 的保守余量，配合外层 `align_up(...,4)` 保证 stage 尺寸自身也 16 字节对齐（`_pd_aligned_ints` 的公式保证实际拷贝量不超过 stage 容量）。

### 4.3 软件栅格屏障 grid_sync：计数器 + 哨兵位翻转

#### 4.3.1 概念说明

planning 内核是一个 cooperative launch：`num_sms` 个 CTA 一起上 GPU，Phase A/B/C/D 之间必须反复"全员到齐才能下一步"。CUDA 没有免费的 grid 级屏障（`cudaLaunchCooperativeKernel` 的 group sync 需要宿主侧 workspace），MoonEP 自己实现了一个，就是 [moonep/_common.py:249-271](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L249-L271) 的 `grid_sync`——文档字符串注明参考了 DeepGEMM 的同名实现。

它的精妙之处在于**自复位**：一个 32 位计数器，每轮全员到达后**恰好回到"低 31 位为 0、最高位翻转"的状态**，不需要任何清零操作。这意味着同一个计数器可以被 planning、dispatch、combine、inter_rank_sync 轮流使用而永不维护——宿主侧只需在构造时 `torch.zeros` 一次（[moonep/api.py:380-384](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L380-L384)，注释明说"allocated once and shared by planning/dispatch/combine/inter_rank, with no per-launch zeroing needed"）：

```python
# Global arrive counter (1 int32) for the software grid barrier: the
# equivalent of cudaLaunchCooperative's grid workspace. Self-resetting
# (sentinel-bit flip), allocated once and shared by planning/dispatch/
# combine/inter_rank, with no per-launch zeroing needed.
grid_sync_bar = torch.zeros(1, dtype=torch.int32, device=dev)
```

#### 4.3.2 核心流程

到达/离开协议（`bar[0]` 是全局计数器，`nsm` 个 SM）：

```
grid_sync(bar, nsm, tid):
  到达：sync_threads()                       # CTA 内先到齐
        if tid == 0:
            pid == 0 ?  inc = 0x80000000 - (nsm - 1)   # SM0 补大头
                    :  inc = 1                          # 其余各 +1
            old = atom_add.release.gpu(bar, inc)        # 到达并取旧值
            自旋: new = ld.acquire.gpu(bar)
                  完成 ⇔ (new ⊕ old) & 0x80000000 ≠ 0
  离开：sync_threads()                       # 广播给全 CTA
```

自复位的数学：每轮全体增量之和为

\[ \big(2^{31} - (n_{sm} - 1)\big) + (n_{sm} - 1) \cdot 1 = 2^{31} \equiv 0 \pmod{2^{31}} \]

即低 31 位每轮**归零**，最高位每轮**翻转**一次。等待条件比较的是自己 arrive 前后读值的异或在哨兵位（`GRID_SYNC_TAG = 0x80000000`，[moonep/_common.py:30-31](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L30-L31)）上的差异：本席位从 0 翻到 1（或 1 翻到 0）当且仅当本轮所有人（合计 \(+2^{31}\)）都到齐——**模 \(2^{32}\) 环绕与跨轮累积都不影响这个判据**，这正是它可被多个内核无限复用的原因。

内存序：跨 SM 的顺序由 release 原子 / acquire 读这对 happens-before 边保证，函数内的 fence 只是 `bar.sync`（文档字符串说明"cross-SM ordering is guaranteed by release/acquire atomics"）。

#### 4.3.3 源码精读

本体 [moonep/_common.py:249-271](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L249-L271)：

```python
@cute.jit
def grid_sync(bar_ptr, nsm: Int32, tid: Int32):
    cute.arch.sync_threads()
    if tid == 0:
        b0 = bar_ptr.toint().ir_value()
        pid = cute.arch.block_idx()[0]
        inc = Int32(1)
        if pid == 0:
            inc = Int32(GRID_SYNC_TAG) - (nsm - 1)
        old = atom_add_release_gpu(b0, inc)
        done = cutlass.Boolean(False)
        while not done:
            new = ld_acquire_gpu_s32(b0)
            done = ((new ^ old) & GRID_SYNC_TAG) != 0
    cute.arch.sync_threads()
```

逐行看：CTA 内 `sync_threads` 保证本块全部线程的先行写完成；只有 0 号线程参与计数（每 CTA 一票）；SM0 加 `TAG-(nsm-1)`、其余加 1，合计恰好 `+0x80000000`；`old` 是自己加之前的值，自旋读 `new` 直到异或出的哨兵位非零。planning 内核里 Phase A/B/C 之间的一连串 `grid_sync(bar_p, num_sms, tid)` 调用（如 [623](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L623)、[670](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L670)、[702](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L702)、[798](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L798)、[833](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L833)、[954](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L954) 行）用的都是它；内核末尾的注释（[1131-1133 行](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1131-L1133)）再次总结："the counter's low 31 bits always return to 0 and the top bit alternates between two phases, so no cleanup zeroing is needed"。

`atom_add_release_gpu` 与 `ld_acquire_gpu_s32` 的 PTX 封装见 [moonep/_common.py:74-88](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L74-L88) 与 [198-212](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L198-L212)，分别对应 `atom.add.release.gpu.global.s32` 与 `ld.acquire.gpu.global.s32`。

**前提**：cooperative launch 保证所有 CTA **同时驻留**（planning 的启动参数里带 `cooperative=True`，[moonep/planning.py:391-393](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L391-L393)），否则先到的 CTA 自旋占着 SM、后到的永远排不上队，就是死锁。

#### 4.3.4 代码实践

**实践目标**：用纯 Python 模拟 grid_sync 计数器，验证"低 31 位归零 + 哨兵位翻转 + 跨轮可复用"。

**操作步骤**：

```python
# 示例代码：grid_sync_sim.py
MASK, TAG = 0xFFFFFFFF, 0x80000000

def grid_sync_round(counter: int, nsm: int) -> int:
    """模拟一轮：SM0 加 TAG-(nsm-1)，其余各加 1（int32 环绕）。"""
    for pid in range(nsm):
        inc = (TAG - (nsm - 1)) if pid == 0 else 1
        counter = (counter + inc) & MASK
    return counter

nsm, c = 148, 0
for rnd in range(6):
    old_tag = c & TAG
    c = grid_sync_round(c, nsm)
    assert c & 0x7FFFFFFF == 0, "低 31 位必须归零"
    assert (c & TAG) != old_tag, "哨兵位必须翻转"
    print(f"round {rnd}: counter = {c:#010x}  低31位=0, TAG 翻转 ✓")
```

**需要观察的现象**：6 轮模拟中计数器在 `0x80000000` 与 `0x00000000` 之间交替，低 31 位恒为 0。

**预期结果**：所有断言通过——这就是"从不清零却永远干净"的直接证据。可再故意把 SM0 的增量改成 1，观察低 31 位残留 `nsm`、下一轮判据错乱，体会"补大头"设计的必要性。纯 Python，可本地验证。

#### 4.3.5 小练习与答案

**练习 1**：nsm=148 时 SM0 每轮加多少？

**答案**：\(2^{31} - 147 = 2147483501\)（十六进制 `0x7FFFFF6D`，仍是正的 int32）。关键是它与另外 147 个 SM 各自的 `+1` 相加后模 \(2^{32}\) 恰为 \(2^{31}\)，即 `0x80000000`。

**练习 2**：为什么等待条件用 `(new ^ old) & TAG` 而不是 `new == 某个固定值`？

**答案**：计数器跨轮累积且每轮最高位翻转、还会模 \(2^{32}\) 环绕，任何"固定阈值"都会在第二轮失效；异或哨兵位只关心"从自己 arrive 到现在这一位翻没翻"，天然免疫绝对值、轮数与环绕。

**练习 3**：planning 和 dispatch 是两个先后启动的内核，共用 `grid_sync_bar` 为什么不串扰？

**答案**：每轮结束计数器回到"低 31 位为 0"的规范态，唯一跨轮变化的是哨兵位相位——而判据恰恰只依赖相位翻转。下一个内核从任意相位出发都成立，无需知道、也无需复位任何状态。

### 4.4 跨 rank 屏障 cross_rank_barrier：双槽相位轮转

#### 4.4.1 概念说明

`grid_sync` 对齐的是一个 kernel 内的 CTA；planning 还需要对齐**不同 GPU 上的 rank**——Phase A 的 tpe 远程写要等所有人写完才能算、src_info 清零要等所有人清完才能发布、src_info 写要等所有人写完才能被 dispatch 读。这就是 [moonep/_common.py:274-346](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L274-L346) 的 `cross_rank_barrier`（参考 DeepGEMM 的 `nvlink_barrier`），信号槽放在 **meta_buf 的 BARRIER 区**——对称内存上的一小块，所有 rank 经 NVLink 可达。

它同样**自复位**，但机制与 grid_sync 不同（信号是别人写进我 chunk 的，不能用"补大头"）：**双相位槽 + 加减交替**。每 rank chunk 有 3 个 int32：`+0`、`+1` 是两个相位信号槽（同伴们往里投 ±1），`+2` 是本地状态字（低 2 位有效：bit0 = 该用哪个槽，bit1 = 这轮投正还是投负）。文档字符串概括为"A single atomic with alternating phase/sign self-resets, so an all-zero initial state is already correct"——**初始全零即可用，永远不需要清理**。

另外它还身兼两座**代理桥**（2.3 节）：入口 `fence.proxy.alias` 把本线程先前的 `multimem`（组播 VA）写与单播别名一致化后才发布；出口 `fence.proxy.async.global` 把屏障获取到的数据桥接到本线程随后的 TMA 访问。这两座桥的位置不是巧合——正是 planning"组播发布（走 mc VA）→ 屏障 → Phase D TMA 拉取（走单播 VA）"这条数据通路需要的（见 4.4.2 的时序分析）。

#### 4.4.2 核心流程

到达/离开协议（每 rank 执行，`ms = meta_stride`）：

```
cross_rank_barrier():
  到达：fence.proxy.alias                      # 桥 1：组播写 → 单播别名
        grid_sync(...)                         # 本 rank 全部 CTA 到齐
        if blockIdx == 0:
            status = bar[+2] & 3;  phase = status & 1;  sign = status >> 1
            if tid < R:                        # 前 R 个线程各负责一个 rank 的信箱
                red.release.sys(peer_rank_tid 的 bar[+phase] += (sign ? -1 : +1))
            sync_threads
            if tid == 0:
                bar[+2] += 1 (gpu scope)       # 推进本地状态字
                target = sign ? 0 : R
                自旋: cur = ld.acquire.sys(bar[+phase])
                      完成 ⇔ cur == target     # 超时则 printf + trap
  离开：grid_sync(...)
        fence.proxy.async.global               # 桥 2：generic → TMA
```

状态机四轮一循环（`status = bar[+2] & 3`，每轮 +1）：

| 轮 | status（读到） | phase（用哪个槽） | sign（投什么） | 等待条件 | 轮后槽值 |
|----|----|----|----|----|----|
| 0 | 0 | 槽 +0 | +1 | 槽 +0 == R | 槽 +0 = R |
| 1 | 1 | 槽 +1 | +1 | 槽 +1 == R | 槽 +1 = R |
| 2 | 2 | 槽 +0 | **-1** | 槽 +0 == 0 | 槽 +0 = 0 |
| 3 | 3 | 槽 +1 | **-1** | 槽 +1 == 0 | 槽 +1 = 0 |

每轮 R 个 rank 各向**所有 R 个信箱**投一张卡（含自己），所以一轮里每个信箱恰好收到 R 份 ±1：投 +1 的轮次凑到 R，投 -1 的轮次归回 0。**两个槽轮换、正负号轮换，每个槽两轮一个完整周期 \(R \to 0\)**——这就是它版本的自复位。状态字 `+2` 本身只增不减（读时 `& 3` 截取低 2 位），溢出高位也无所谓。

planning 里三处调用的语义各不相同：

1. [609 行](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L601-L609)：发布 Phase A 的远程写——各 rank 的 tpe 直方图写进 rank0 chunk 的 TPE 区、rank0 把 topk/tpe 写进 rank1 chunk（u3-l4 的"排序外包"），rank0 的 Phase A 计算与 rank1 的 run_c1 都依赖这些数据；
2. [979 行](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L973-L979)：发布 src_info 清零（"所有人清完自己的槽"先于"任何人往别人槽里写"，防止写被清掉）；**同时**它也是组播发布与 Phase C 输出的可见性边界——rank0 的 `multimem` 写先过入口的 `fence.proxy.alias` 一致化、再经 release 发布，其它 rank acquire 之后才进 passB 读本地 PLAN 副本、才发起 Phase D 的 TMA 拉取（出口的 `fence.proxy.async.global` 恰好为此铺设）；
3. [1077 行](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1074-L1077)：发布 src_info 写——注释写明"makes all peer src_info writes visible before any rank's fresh dispatch builder reads its local src_info slice"，即 planning 返回后 dispatch 可以各自立刻开跑的前提。

#### 4.4.3 源码精读

本体 [moonep/_common.py:274-346](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L274-L346)。先是契约断言与桥 1（[304-311 行](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L304-L311)）：

```python
assert num_threads >= num_ranks, (
    "cross_rank_barrier requires blockDim.x >= num_ranks: ...")
cute.arch.fence_proxy("alias")
grid_sync(bar_ptr, nsm, tid)
```

断言 `blockDim.x >= num_ranks` 是硬约束：block0 的前 R 个线程每人负责一个 peer 的信箱，planning 里 `BLOCK_DIM_P2 = 512`，因此 R ≤ 512。

信号发送（[312-322 行](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L312-L322)）——block0 的线程 `tid` 往 rank `tid` 的当前相位槽投 ±1：

```python
if cute.arch.block_idx()[0] == 0:
    status = meta_buf[rank * meta_stride + barrier_off + 2] & 3
    phase = status & 1
    sign = status >> 1
    if tid < num_ranks:
        peer = (meta_buf.iterator + (tid * meta_stride + barrier_off + phase)).toint()
        delta = Int32(1)
        if sign != 0:
            delta = Int32(-1)
        red_add_release_sys(peer.ir_value(), delta)
```

`red.release.sys`（[moonep/_common.py:215-228](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L215-L228)，PTX `red.release.sys.global.add.s32`）是"只写不返回"的 release 原子加，投卡动作本身就是发布点。

等待与状态推进（[323-341 行](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L323-L341)）：

```python
if tid == 0:
    cute.arch.atomic_add(meta_buf.iterator + (rank * meta_stride + barrier_off + 2),
                         Int32(1), scope="gpu")
    target = Int32(num_ranks)
    if sign != 0:
        target = Int32(0)
    own = (meta_buf.iterator + (rank * meta_stride + barrier_off + phase)).toint()
    done = cutlass.Boolean(False)
    start = clock64()
    while not done:
        cur = ld_acquire_sys_s32(own.ir_value())
        done = cur == target
        if (clock64() - start) >= Int64(BARRIER_TIMEOUT_CYCLES):
            cute.printf("MoonEP cross_rank_barrier timeout (100s), trapping: ...")
            device_trap()
```

状态字 +1 推进轮次；`ld.acquire.sys` 自旋等自己的信箱凑到 target；超时上限 `BARRIER_TIMEOUT_CYCLES = 100 * 2_000_000_000`（[moonep/_common.py:36-37](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L36-L37)，按 ≈2 GHz SM 时钟约合 100 秒）后 `printf` 诊断信息并 `device_trap()`（[58-71 行](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L58-L71)）——注释强调 fail-fast 是为了**避免继续污染屏障状态**：若挂掉的 rank 永远不来投卡，同伴们无限自旋只会把日志刷爆、把状态搅乱。

离开侧（[342-346 行](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L342-L346)）：再一次 `grid_sync` 让全 CTA 一起过线，随后桥 2 `fence.proxy.async.global` 把刚获取的数据铺给后续 TMA。

#### 4.4.4 代码实践

**实践目标**：模拟 cross_rank_barrier 的状态机，验证双槽轮转 + 加减抵消的"永不清零"；写出协议伪代码。

**操作步骤**：

```python
# 示例代码：cross_rank_sim.py
def cross_rank_round(state, slots, R):
    """一轮：status 推进；R 个 rank 各向所有信箱投 ±1。"""
    status = state["status"] & 3
    phase, sign = status & 1, status >> 1
    delta = -1 if sign else 1
    for _ in range(R):                 # 每个 rank 给每个信箱投一张卡
        for p in range(R):
            slots[p][phase] += delta
    state["status"] += 1
    target = 0 if sign else R
    assert all(slots[r][phase] == target for r in range(R)), "本轮未对齐"

R = 8
state, slots = {"status": 0}, [[0, 0] for _ in range(R)]
for rnd in range(8):
    cross_rank_round(state, slots, R)
    print(f"round {rnd}: status={state['status'] & 3}  slots={[s[:] for s in slots[:3]]}...")
assert all(s == [0, 0] for s in slots), "8 轮后应回到全零"
```

**需要观察的现象**：槽值按 4.4.2 的表格轮转（槽 +0 在轮 0 结束为 R、轮 2 结束回 0……），status 低 2 位循环 0→1→2→3→0，8 轮（两个完整周期）后两槽同时归零。

**预期结果**：断言全部通过；对照 4.4.2 的协议伪代码检查自己的实现覆盖了"到达（投卡）—推进（状态 +1）—等待（凑 target）—离开（grid_sync + fence）"四步。纯 Python，可本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么跨 rank 信号要被 `grid_sync` 前后包住，而不是所有 CTA 都去投卡？

**答案**：只有 block0 的前 R 个线程投卡，"rank 级到达"的定义才能保持一票一 rank；前面的 `grid_sync` 保证本 rank 全部 CTA 都到达后才投卡（不然别的 CTA 还在写共享数据，卡就投早了），后面的 `grid_sync` 保证拿到全员信号后整个 kernel（所有 CTA）一起过线。若所有 CTA 投卡，卡数变成 `num_sms * R`，target 也要随之变，白白多出 `num_sms` 倍的 NVLink 原子流量。

**练习 2**：去掉入口的 `fence.proxy.alias` 会出什么问题？

**答案**：rank0 的组播发布走 mc（组播 VA）写，passB 与后续读者经单播别名访问**同一块物理内存**；两个访问分属不同代理，默认不保证互相一致。没有这座桥，release 发布的可能是"别名视角下的旧值"，其它 rank 过了屏障仍可能读到未更新的 PLAN 副本。文档字符串明确写了这座桥服务于该场景（"so peers may read the same physical memory through the unicast alias afterwards"）。

**练习 3**：第 5 轮（`status` 回到 0）开始时，两个槽的值各是多少？

**答案**：都是 0——轮 3 结束时槽 +1 被 -1 归零、槽 +0 早在轮 2 归零。所以第 5 轮与第 1 轮完全同构，状态机可无限循环，这就是"an all-zero initial state is already correct"的含义。

### 4.5 inter_rank_sync：planning 之前的发令枪

#### 4.5.1 概念说明

planning 是个集体动作：内部有跨 rank 屏障、组播发布、相互写 src_info。如果各 rank **启动** planning 的时刻参差不齐——CPU 侧launch 抖动、上游计算流长短不一——先到的 rank 会在第一道 `cross_rank_barrier` 上干等后到的，白白把"planning 耗时"拉长且不稳定（木桶效应在时间维度的重演）。

[moonep/inter_rank_sync.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/inter_rank_sync.py) 的解决方式朴素而有效：在 planning 之前插一个**极小的独立内核**，里面只做一次 `cross_rank_barrier`。模块文档字符串（[1-7 行](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/inter_rank_sync.py#L1-L7)）说得很直白：

> This is used to align EP ranks immediately before launching the planning kernel, so planning/communication timings are less affected by CPU-side or upstream stream skew.

直觉比喻：4×100 米接力前先把各赛道选手叫上起跑线，发令枪响后大家一起冲——省得有人抢跑空等、有人迟到拖累。对齐的成本是所有 rank 在这里等最慢的——但**等待被显式地记在"同步"账上，而不是稀释进 planning 的测量与流水**；planning 启动后立刻就能过第一道屏障。

自复位屏障在这里兑现了第二个红利：这个内核与 planning **共用同一组 BARRIER 槽与同一个 `grid_sync_bar` 计数器**（[149-154 行](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/inter_rank_sync.py#L149-L154) 传入的正是 `ctx['grid_sync_bar']`），它推进了 phase/sign 与计数器相位，但因为是自复位的，**对 planning 随后的屏障调用零影响、零清理**。

#### 4.5.2 核心流程

```
api._dispatch_impl（每个 rank 的通信流上）:
    if inter_rank_sync:
        launch_inter_rank_sync(ctx)      # ← 发令枪：单 CTA，只做一次 cross_rank_barrier
    launch_planning(ctx, topk, tpe, cu_seqlens, plan)   # 全员已对齐后启动
    launch_dispatch(...)
    ...
```

内核几何刻意做到最小：`grid=(1,1,1)`、`smem=0`、线程数只求盖住 R（见下），启动开销近乎只有一次内核发射。

#### 4.5.3 源码精读

调用顺序的证据在 [moonep/api.py:631-636](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L631-L636)——`_dispatch_impl` 的第一件事就是可选预同步，然后才规划：

```python
if inter_rank_sync:
    launch_inter_rank_sync(ctx)

if planning_args is not None:
    topk_flat, tokens_per_expert, cu_seqlens = planning_args
    launch_planning(ctx, topk_flat, tokens_per_expert, cu_seqlens, plan)
```

`dispatch` 的参数文档（[moonep/api.py:751-752](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L751-L752)）同样注明"inter_rank_sync: run a CuTe DSL rank sync before planning (default True)"。`combine` 侧也有对称调用（[678-679 行](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L678-L679)，注释说明保持其历史位置，combine 入口自有屏障负责发布暂存写）。

内核本体 [moonep/inter_rank_sync.py:61-83](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/inter_rank_sync.py#L61-L83) 只有一行实质内容——复用 `cross_rank_barrier`：

```python
@cute.kernel
def kernel(self, meta_tensor, bar_tensor, rank: Int32, barrier_off: Int32):
    meta_stride = cutlass.const_expr(self.meta_stride)
    num_ranks = cutlass.const_expr(self.R)
    tid = cute.arch.thread_idx()[0]
    cross_rank_barrier(
        meta_tensor, meta_stride, barrier_off, rank, num_ranks,
        bar_tensor.iterator, Int32(1), cutlass.const_expr(self.num_threads), tid,
    )
```

注意传入的 `nsm = 1`：单 CTA 下内部的 `grid_sync` 退化为两次 `bar.sync` 加一次原子，正当地便宜。启动配置在 [30-59 行](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/inter_rank_sync.py#L30-L59) 的 `__call__`：`grid=(1,1,1)`、`smem=0`、`cooperative=True`（与库内其它使用 `grid_sync` 的内核保持一致的启动约定）。

线程数由 [86-89 行](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/inter_rank_sync.py#L86-L89) 决定：

```python
def _num_threads_for_ranks(R: int) -> int:
    assert R > 0, f"R must be positive, got R={R}"
    assert R <= 1024, f"inter_rank_sync supports at most 1024 ranks, got R={R}"
    return max(32, ((R + 31) // 32) * 32)
```

向上取整到 warp 的倍数且至少 32，同时天然满足 `cross_rank_barrier` 的 `num_threads >= num_ranks` 断言。宿主入口 [119-155 行](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/inter_rank_sync.py#L119-L155)：校验 meta_buf、按 `(R, meta_stride, num_threads, device)` 做 `lru_cache` 编译缓存、把 `ctx['meta_buf']` 与 `ctx['grid_sync_bar']` 的指针连同 `ctx['BARRIER_OFF']` 一起喂给已编译内核——与 planning 同一块信号区、同一个计数器。

#### 4.5.4 代码实践

**实践目标**：说清 inter_rank_sync 的位置与理由（本讲规格指定的实践）。

**操作步骤**：

1. `Grep "launch_inter_rank_sync"` 找到 api.py 中的全部调用点（`_dispatch_impl` 与 `_combine_impl` 各一处）；
2. 读 [moonep/inter_rank_sync.py:1-7](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/inter_rank_sync.py#L1-L7) 的文档字符串与 [moonep/api.py:631-636](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L631-L636) 的调用顺序；
3. 用文本画出以下两个时序并对比（脚本输出即可）：

```python
# 示例代码：sync_position.py（打印文本时序）
no_presync = """
rank0: [CPU抖动大, 晚启动] ──────────▶ planning ──▶ barrier#1 等待...
rank1: [早启动] planning ──▶ barrier#1 自旋空等 ═══════▶ (rank0 到达才放行)
                            └── 空等时间被计入 planning/通信耗时
"""
presync = """
rank0: ──▶ inter_rank_sync ──┐(全员在此对齐, 等待记在同步账上)
rank1: ──▶ inter_rank_sync ──┘
        ▶ planning ──▶ barrier#1 几乎立刻通过（全员同时在场）
"""
print(no_presync); print(presync)
```

**需要观察的现象**：两种时序里 planning 内部第一道屏障的等待时长差异——预同步把 skew 消化在专门的小内核里。

**预期结果**：能写出约 200 字的说明，覆盖三点：(a) planning 是含跨 rank 屏障的集体内核；(b) 各 rank 启动时刻受 CPU/上游流影响有 skew，快者在屏障上空等；(c) 预同步内核单 CTA、零 smem、复用自复位屏障（与 planning 共用信号区且无需清理），把对齐成本显式化并稳定后续计时。此为源码阅读型实践，无需 GPU。

#### 4.5.5 小练习与答案

**练习 1**：`_num_threads_for_ranks(8)` 返回什么？为什么不能直接用 8？

**答案**：返回 32。线程数须是 warp（32 线程）的整数倍才是合法/高效的 block 尺寸；同时必须 ≥ R 才能满足 `cross_rank_barrier` 里"前 R 个线程各管一个信箱"的断言，`max(32, ceil(R/32)*32)` 一举满足两者。

**练习 2**：什么场景下合理的把 `inter_rank_sync=False` 传给 dispatch？

**答案**：上游已经保证各 rank 同时到达（例如宿主刚做过 `torch.distributed.barrier()` 或前后算子在各 rank 完全对称）、逐层 MoE 调试想省一次内核发射、或测量 planning 本体耗时想剥离同步开销时。默认 True 说明作者认为常规训练里 skew 是常态。

**练习 3**：这个内核推进了 BARRIER 区的 phase/sign 状态，planning 会因此读错槽吗？

**答案**：不会。phase/sign 是**本地状态字派生的**：每个 rank 读自己 `+2` 槽的低 2 位决定本轮用哪个槽、投什么符号，只要所有 rank 经历的屏障轮数相同（它们必然相同——每次 cross_rank_barrier 是全员参与的集体操作），状态字就同步推进。inter_rank_sync 多消耗一轮，等于全体一起往前转了一格状态机，随后 planning 的第一道屏障自然落在下一个正确的相位上——自复位设计让"共用"与"复用"都不需要任何簿记。

## 5. 综合实践

把本讲三块内容串成一个"屏障协议档案 + 状态机验证器"（纯 Python，无需 GPU），产出一份 `barrier_protocol_doc.md` 草稿：

1. **协议伪代码**：为 `grid_sync` 与 `cross_rank_barrier` 各写一份"到达—离开"四段伪代码（到达动作 / 等待判据 / 离开动作 / 自复位机制），格式对照 4.3.2 与 4.4.2，逐行标注对应的源码行号链接；
2. **模拟验证**：把 4.3.4 的 `grid_sync_sim.py` 与 4.4.4 的 `cross_rank_sim.py` 合并成一个脚本，随机化各 SM/各 rank 的到达顺序（`random.shuffle` 后逐个投增量），跑 100 轮，断言：(a) grid_sync 每轮结束低 31 位为 0、哨兵位翻转；(b) cross_rank_barrier 每 4 轮槽值回到全零、状态字低 2 位循环——**乱序到达不破坏正确性**（release/acquire 只约束可见性，不约束到达顺序）；
3. **图文说明**：写一段"inter_rank_sync 为什么在 planning 之前"的分析，引用 `moonep/inter_rank_sync.py` 文档字符串与 `moonep/api.py:631-636` 的调用顺序作为证据，并解释共用 `grid_sync_bar` 为何安全（自复位）；
4. **（可选，需多卡 NVLink 环境）**运行 `torchrun --nproc_per_node=8 -m pytest tests/test_planning.py`，对比 `inter_rank_sync` 默认开启下 planning 的正确性；若暂时关闭（调用侧传 `inter_rank_sync=False`，需改示例脚本而非库源码），验证结果不变、仅时序不同。此步**待本地验证**。

预期：伪代码与模拟互相印证——两份"永不清零"的数学（模 \(2^{31}\) 的哨兵翻转、双槽加减抵消）在乱序模拟下依然成立。

## 6. 本讲小结

- **发布分工**：rank0 规划结果中"人人要全量"的三张查找表（ALLOC/TPE/EOFF，共 \(3ER\) 个 int32）经 `multimem.st.v4` **组播**一次写入扇出到所有 rank 的 PLAN 区，发布成本与 R 无关；"各取所需"的切片（本 rank 的 cu_seqlens/zero_fill 行、全量 experts_to_copy）由各 rank 在 Phase D 用 `cp.async.bulk` 从 rank0 chunk **单播拉取**。
- **Phase D 暂存**：G2S 拷贝在 passB 之前发起、计算完成后 `mbarrier_wait` 收割，搬运与计算重叠；16 字节信封对齐（`_pd_stage_bias`/`_pd_aligned_ints`）化解"逻辑起点不对齐"与 TMA 硬性对齐要求的矛盾，写回时按偏差重定位。
- **grid_sync**：软件栅格屏障，SM0 补 \(2^{31}-(n_{sm}-1)\)、其余各 +1，每轮合计 \(+2^{31}\)——低 31 位归零、哨兵位翻转，**从不清零**，因此一个计数器被 planning/dispatch/combine/inter_rank_sync 终身共用。
- **cross_rank_barrier**：跨 NVLink 屏障落在 meta_buf 的 BARRIER 区，双相位槽 ±1 轮转（每槽两轮一周期 \(R \to 0\)）实现自复位；入口 `fence.proxy.alias` 桥接组播写 → 单播读，出口 `fence.proxy.async.global` 桥接屏障获取 → TMA 访问，两座桥与 planning 的数据通路严丝合缝；100 秒超时 fail-fast 防 state 污染。
- **inter_rank_sync**：planning 之前单 CTA、零 smem 的预同步发令枪，把 CPU/上游流 skew 的等待显式化，使 planning 启动后立刻通过第一道跨 rank 屏障；靠屏障自复位与 planning 共用信号区，零清理、零簿记。

## 7. 下一步学习建议

至此单元 3（在线规划器）完整收官：从 tpe 汇聚、surplus/deficit 平衡、专家分配与 top-B 选择、dst 槽位计算与去重编码，到本讲的发布与同步，你已经能独立读通 `moonep/planning.py` 的每一个阶段。接下来两条路：

1. **进入单元 4（通信内核）**：建议从 u4-l1（CuTe DSL 与 PTX 基础设施）开始——本讲已经预习过 `_common.py` 里的原子、TMA 封装与屏障，u4-l1 把它们正式铺开；随后 u4-l2 的 dispatch 内核会**作为消费者**用上本讲的 src_info、PLAN 区与退出屏障。
2. **横向巩固**：带着本讲的"自复位"视角重读 [moonep/dispatch.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py) 与 [moonep/combine.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py) 中所有 `cross_rank_barrier`/`cross_warp_sync` 调用点，问自己同一个问题："这道屏障发布的是什么数据、给谁读？"——这道问题贯穿 MoonEP 全部内核的并发正确性。
