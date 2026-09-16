# u5-l1 权重缓冲区布局与预取内核

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `[E+B, H, H']` 权重张量每一行的语义：前 `E` 行是对称内存映射的全组专家，后 `B` 行是本地预取槽；理解训练为何必须 `B = E/R`、推理为何推荐 `B = 3–4`。
2. 读懂 `PrefetchKernel` 的完整设计：把 3D 权重视为 2D 矩阵、128×128 tile 划分、持久化 CTA（persistent CTA）、warp 0 生产 / warp 1 消费的多阶段 TMA 流水线，以及「延迟一个 store 组再释放 stage」的带宽考量。
3. 读懂 `launch_prefetch` 宿主封装：类型白名单、形状/连续性契约、`lru_cache` 编译缓存与 `Buffer.prefetch_weight` 的切分逻辑。
4. 理解预取槽物理内存「进程级池共享」的成本模型：额外开销是每投影 `B` 个专家权重的物理内存总量，而不是乘以层数。

本讲是第五单元（权重预取与梯度归约）的第一讲，只聚焦**权重侧**；`[E+B]` 梯度缓冲与 `reduce_grad` 留到 u5-l3。

## 2. 前置知识

本讲承接前面几讲的关键结论，先做一分钟回顾：

- **u3-l3（专家级分配与 top-B 选择）**：在线规划器的 Phase C 会为每个 rank 挑出最热的 `B` 个远程专家，写入 `plan.experts_to_copy`（形状 `[R, B]` 的 int32 表，空槽填 `-1`）。同一张表还产出 `cu_seqlens[E+B]`——被复制专家的 token 段被指向预取槽行。
- **u3-l2（群组级均衡）**：迁移矩阵 `z` 满足「每个目的 rank 至多从一个远程 home group 接收 token」的不变量，因此每个 rank 需要复制的远程专家数上限是 `E/R`——这正是训练取 `B = E/R` 的算法根基。
- **u2-l2（对称内存）**：所有 rank 通过 CUDA VMM 把彼此的物理显存映射成一段布局一致的连续虚拟地址，远程读走 NVLink。因此「远程专家权重」本来就可以直接读——预取不是为了「能读」，而是为了「读得快」。
- **u4-l1（CuTe DSL 与 PTX 基础）**：TMA 的两种拷贝模式——G2S（global→shared，mbarrier 事务计数确认到达）与 S2G（shared→global，`bulk_group` 编组 + `cp_async_bulk_wait_group` 等待完成）；`const_expr` 把形状烧成编译期常量；`lru_cache` 按形状缓存 cubin。本讲的流水线原语 `pipeline.PipelineTmaAsync` 是对这些手工原语的官方封装，语义完全一致。

一个提醒：本讲会大量使用「槽位（slot）」「tile」「stage」三个词。**槽位**是 `experts_to_copy` / 权重张量第 0 维的意义单位（一个专家的完整权重）；**tile** 是 128×128 元素的搬运单位；**stage** 是 smem 里流水线缓冲的份数。三者层次从大到小。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `moonep/prefetch.py` | 预取内核全部实现 | `PrefetchKernel`（设备内核）、`launch_prefetch`（宿主封装）、`_get_compiled`（编译缓存） |
| `moonep/api.py` | 顶层 `Buffer` API | `prefetch_weight` 入口、`_launch_full_weight_prefetches` 切分逻辑 |
| `README.md` | 项目说明 | Weight buffer 一节：`[E+B]` 行语义与 `B` 的取值规则 |
| `tests/test_prefetch.py` | 预取正确性测试 | `run_case` 的逐槽校验逻辑、单 owner 对称张量搭建方式 |
| `benchmarks/bench_vs_deepep.py` | 对比基准 | `[E+B]` 复合张量的真实组装示例 |

## 4. 核心概念与源码讲解

### 4.1 权重缓冲区 `[E+B, H, H']`：行语义、B 的取值与成本模型

#### 4.1.1 概念说明

回顾 u1-l1 的核心设计：MoonEP 用「动态冗余专家」实现完美均衡——每个 rank 除了自己的 `E/R` 个 home 专家，还要临时接收其他 rank 的 token。处理这些 token 需要对应远程专家的**权重**。

MoonEP 与训练/推理框架的契约是：**每个专家投影（gate/up/down）持有一个连续的对称内存张量 `[E+B, H, H']`，外加规划器产出的 `cu_seqlens[E+B]`**。分组 GEMM（VM group GEMM）只按行号寻址，所以连续性是硬性要求。行语义分两段：

- **行 `[0, E)`：全组所有 rank 的本地专家**，每 rank 占 `E/R` 行。这些行不是拷贝——每一段物理上*就是*宿主 rank 的参数显存，经 u2-l2 的对称内存映射到所有 rank。也就是说，即使不做任何预取，远程专家权重也可经 NVLink 直接读取。
- **行 `[E, E+B)`：本地预取槽**。由 `buffer.prefetch_weight` 按规划结果填充；规划器通过 `cu_seqlens` 把被复制专家的 token 段指向这些行，使分组 GEMM 的所有读取都命中本地（NVLink 域内的）内存。

`B` 的取值规则（README 原文）：

- **训练必须 `B = E/R`**：u3-l2 证明了每个 rank 至多从一个远程 home group 复制专家，而上限恰好是 `E/R` 个（一个 home group 的全部专家）。取满才能保证分组 GEMM 触碰的每个专家都是「本地」的——梯度侧的 reduce 缓冲（u5-l3）也按这个布局镜像。
- **推理允许 `B < E/R`，推荐 `B = 3–4`**：路由通常高度集中，少数热门专家覆盖绝大多数 token。若某 rank 需要的不同远程专家数超过 `B`，分组 GEMM 会经对称映射**直接从 home rank 远程读取**溢出权重——慢一点，但完全不影响正确性。这是一个优雅的降级路径：预取是优化项，不是正确性依赖项。

**成本模型**：预取槽的物理内存来自一个**进程级共享池**——所有层共用，因此额外开销是「每投影 `B` 个专家权重」的总量，而不是「每层 `B` 个」乘以层数。其物理基础正是 u2-l2 的 VMM「虚拟地址固定、物理映射可换」能力：每层 `[E+B]` 的虚拟布局不变，而 `B` 个槽行背后的物理页可以随训练步逐层重映射。池的组装由集成方（训练框架）负责——MoonEP 自身的契约只是那个连续张量。

#### 4.1.2 核心流程

一次前向中权重侧的完整流转：

```text
dispatch(hidden, ...)                     # 在线规划 + token 派发
    └─> plan.experts_to_copy[R, B]        # 每 rank 的 top-B 远程专家（-1 为空槽）
    └─> cu_seqlens[E+B]                   # token 段 → 专家行的映射（含预取槽行）

prefetch_weight(plan, full_*_weight)      # 本讲主角
    └─> 对 gate/up/down 三个投影各跑一次 launch_prefetch：
          remote_expert    = full_weight[0:E]     # 源：对称内存里的全组专家
          prefetch_buffers = full_weight[E:E+B]   # 目的：本地预取槽
          experts          = experts_to_copy[rank]

分组 GEMM(cu_seqlens, full_weight)        # token 段 × 对应专家行（全部本地命中）
```

搬运总量（字节数）：

\[
\text{bytes} = n_{\text{active}} \times H \times H' \times b_{\text{elem}}, \qquad n_{\text{active}} = \#\{b : \text{experts}[b] \ge 0\}
\]

其中 \(b_{\text{elem}}\) 是每个元素的字节数（bf16 为 2，int8/uint8 为 1）。

#### 4.1.3 源码精读

README 定义了整个契约——「一个连续对称内存权重张量 + 一个 `cu_seqlens`」，并给出两段行语义与 `B` 的取值规则：

- [README.md:L45-L59](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L45-L59)：权重缓冲契约。行 `[0,E)` 是各 rank 本地专家（每段物理上就是 home rank 的参数显存）；行 `[E,E+B)` 是预取槽，物理内存来自进程级共享池；训练必须 `B=E/R`，推理推荐 `B=3–4`，溢出经对称映射直读 home rank。

`Buffer.prefetch_weight` 是用户入口，其 docstring 写明了张量契约（三个投影必须一起给、形状 `[E+B,H,H']`、行约定、量化时的 scale 约定），并说明了它独立于 `dispatch` 存在的理由——plan 复用路径（combine bwd）可以跳过重新预取：

- [moonep/api.py:L860-L896](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L860-L896)：`prefetch_weight` 签名与契约文档。
- [moonep/api.py:L899-L917](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L899-L917)：契约断言——三个权重张量必须同时提供、dtype 必须在 `_ELEM_TYPES` 白名单内、必须连续、`ndim==3` 且第 0 维恰为 `E+B`；scale 张量同理。

真正的切分发生在 `_launch_full_weight_prefetches`——注意 `full_weight[:E]` 与 `full_weight[E:]` 这两刀，它们把一个张量的两段分别当作 `launch_prefetch` 的源和目的：

- [moonep/api.py:L158-L182](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L158-L182)：对 gate/up/down 各调用一次 `launch_prefetch(full_weight[:E], full_weight[E:], experts_to_copy, num_sms)`；scale 张量先经 `retile_for_prefetch` 重排再走同一路径（u5-l2 专门讲）。
- [moonep/api.py:L706-L718](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L706-L718)：`_run_prefetch_weight_on_current_stream` 用 `experts_to_copy[int(ctx['rank'])]` 取出 `[R,B]` 表中**本 rank 的那一行**——预取计划是每 rank 独立的。

`bench_vs_deepep.py` 里有一个真实的 `[E+B]` 组装示例，能帮助理解「前 E 行是全组池、后 B 行是本 rank 自己的」：R 个 rank 各贡献一个 `[epn, H, Hp]` 专家 chunk（稠密全局池，远程行走 NVLink），本 rank 自己的 `[epn]` 缓冲 chunk 拼在最后，以 `world_size=R+1` 做一次 `nvl_dist_map` 得到 `[E+B, H, Hp]`：

- [benchmarks/bench_vs_deepep.py:L154-L167](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py#L154-L167)：复合 `[E+B]` 布局的组装说明（该基准假设 `B == epn == E/R`，与训练规则一致）。

测试侧的对应物是 `make_single_owner_experts`：为每个物理 owner GPU 建一个「单 owner」VMM 张量（物理内存在 owner 上、所有 rank 可读写），本 rank 取**远程 owner**的那份当 `remote_expert`：

- [moonep/buffer.py:L338-L351](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L338-L351)：`create_nvl_single_owner_tensor` 的文档——shape 必须先经 `pad_dim0_for_alignment` 对齐到 VMM 粒度。
- [tests/test_prefetch.py:L244-L264](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_prefetch.py#L244-L264)：owner rank 填随机权重（尾部 padding 清零），每个 rank 都拿到 `mapped[:E]` 作为远程专家源。

#### 4.1.4 代码实践

**实践目标**：把「预取槽的显存成本」算清楚，直观体会进程级池共享的收益。

**操作步骤**（示例代码，纯 CPU 可运行）：

```python
# prefetch_cost.py —— 预取槽显存成本核算（示例代码）
def fmt(nb):  # 字节数转可读单位
    for u in ("B", "KiB", "MiB", "GiB"):
        if nb < 1024 or u == "GiB":
            return f"{nb:.1f} {u}"
        nb /= 1024

E, R, H, Hp, L = 256, 8, 7168, 3072, 80   # 生产级 MoE 配置，80 层
B = E // R                                 # 训练规则：B = E/R = 32
per_expert = H * Hp * 2                    # bf16

per_proj = E * per_expert                  # 一个投影的参数量
slots_per_proj = B * per_expert            # 一个投影的预取槽显存
print("单投影参数        :", fmt(per_proj))       # ≈ 10.9 GiB
print("单投影预取槽(池化) :", fmt(slots_per_proj)) # ≈ 1.4 GiB（全层共享）
print("若每层独立分配    :", fmt(L * slots_per_proj))  # 无池化：×L

total = 3 * (per_proj + slots_per_proj)    # gate/up/down 三投影
print("三投影权重总量    :", fmt(total))
```

**需要观察的现象**：无池化时预取开销随层数线性膨胀（80 层 ≈ 109 GiB，不可接受）；池化后它是与层数无关的常量。

**预期结果**：单投影参数 ≈ 10.93 GiB，池化预取槽 ≈ 1.37 GiB，三投影合计 ≈ 36.9 GiB。若你的 `fmt` 输出与这些数值有出入，先检查单位换算。

#### 4.1.5 小练习与答案

**练习 1**：为什么训练必须 `B = E/R`，而推理可以更小？

**答案**：训练时每个 rank 至多从一个远程 home group 复制专家（u3-l2 的 `z` 矩阵不变量），上限是一个 home group 的全部 `E/R` 个专家；取 `B=E/R` 保证分组 GEMM 只碰本地行，同时梯度侧的 reduce 缓冲也按该布局镜像（u5-l3）。推理没有梯度约束，路由又高度集中，`B=3–4` 即可覆盖多数热门专家；溢出部分经对称映射直读 home rank，慢但正确。

**练习 2**：某推理部署取 `B=2`，某步规划器给一个 rank 选出了 3 个远程专家，会发生什么？

**答案**：前 2 个（token 数最多的）进预取槽，第 3 个专家的 token 段由 `cu_seqlens` 指向它在 `[0,E)` 内的本名行，分组 GEMM 经 NVLink 远程读取 home rank 的权重。正确性不变，该段 GEMM 变慢。

**练习 3**：`full_weight[:E]` 作为源传给内核，其中包含了本 rank 自己的 `E/R` 个本地专家行。这会出问题吗？

**答案**：不会。`experts_to_copy` 只包含远程专家（u3-l3 的 top-B 选择只考虑远程专家），内核只读取表里点名的行，本地行永远不被读；传整个 `[0,E)` 只是为了让「源 = 对称内存里的全组专家」这一定义完整。

### 4.2 PrefetchKernel：持久化 2D TMA 流水线

#### 4.2.1 概念说明

预取本质上是一个**纯带宽问题**：把 \(n_{\text{active}} \times H \times H'\) 个元素从 NVLink 对端搬进本地显存，没有计算、没有跨 rank 同步（每个 rank 按自己的计划独立launch，源端甚至不知道有人在读——对称内存的读是单边的）。`PrefetchKernel` 的模块文档开宗明义：**persistent, warp-specialized 2D TMA pipeline**——三个关键词：

1. **2D TMA**：把 3D 张量 `[E, H, H']` 看成 2D 行主序矩阵 `[E*H, H']`，每个 128×128 元素的 tile 就是一次 TMA 搬运单位。行坐标按专家主序编码：`remote row = expert_id * H + h`，`buffer row = buffer_id * H + h`。tile 形状固定 128×128 是第一版实现的简化选择，代价是 `H` 与 `H'` 必须是 128 的倍数（`launch_prefetch` 会断言）。
2. **warp 特化**：64 线程 = 2 个 warp。warp 0 是生产者（GMEM→SMEM 的 2D TMA load），warp 1 是消费者（SMEM→GMEM 的 2D TMA store）。两个 warp 通过 `PipelineTmaAsync` 的多 stage 环形缓冲衔接——正是 u4-l2 dispatch 内核「按行 TMA + mbarrier 流水线」的同款模式，但这里搬运的是权重 tile 而非 token 行。
3. **持久化 CTA**：grid 恒为 `num_sms`，每个 CTA 用 `for tile in range(bidx, total_tiles, num_sms)` 的 grid-stride 循环认领 flat tile 序列，而不是为每个 tile 重新启动 CTA。省去启动开销，也让 tile→CTA 的分配天然均衡（活跃槽数变化时无需改 grid）。

另一个关键设计是**计划压缩**：`experts_to_copy` 可能有很多 `-1` 空洞。若 tile 循环直接遍历 `B * TILES_PER_EXPERT` 个索引再逐个跳过空槽，每个 CTA 都要空转很多次；内核开头先把稀疏表压缩成紧凑的 `exp_tab`（专家号）/`slot_tab`（槽号）两张 smem 表，循环只访问活跃条目。

#### 4.2.2 核心流程

设备内核的完整执行逻辑（伪代码）：

```text
kernel(tma_g2s, tma_src, tma_s2g, tma_dst, experts):        # grid=num_sms, block=64 线程
    # —— 阶段 0：smem 布局 ——
    smem: load_mbar[2*stages] + exp_tab[B] + slot_tab[B] + tile[128,128,stages]

    # —— 阶段 1：计划压缩（全部 64 线程执行同样的扫描）——
    n_active = 0
    for b in 0..B:                    # B 是编译期常量
        e = experts[b]                # 从 gmem 读
        if e >= 0:
            exp_tab[n_active], slot_tab[n_active] = e, b
            n_active += 1
    total_tiles = n_active * (H/128) * (Hp/128)

    # —— 阶段 2：warp 分裂 ——
    if warp_idx == 0:                                 # 生产者
        for tile in range(bidx, total_tiles, num_sms):
            i = tile // TILES_PER_EXPERT              # 第 i 个活跃条目
            (mt, nt) = (tile % TILES_PER_EXPERT) 拆成 (行块, 列块)
            expert = exp_tab[i]
            load_pipe.producer_acquire(state)         # 等有空闲 stage
            src_mt = expert * MTILES + mt             # 源 2D 视图里的行块号
            TMA G2S: tma_src[(src_mt, nt), :] -> smem[state.index]
            state.advance()

    elif warp_idx == 1:                               # 消费者
        for tile in range(bidx, total_tiles, num_sms):
            i, (mt, nt) 同上
            b = slot_tab[i]
            load_pipe.consumer_wait(use_state)        # 等 stage 填满（tx 计数归零）
            dst_mt = b * MTILES + mt                  # 目的 2D 视图里的行块号
            TMA S2G: smem[use_state.index] -> tma_dst[(dst_mt, nt), :]
            cp_async_bulk_commit_group()
            use_state.advance(); issued += 1
            if issued >= 2:                           # 延迟释放（见下文）
                cp_async_bulk_wait_group(1, read=True)
                load_pipe.consumer_release(rel_state)
                rel_state.advance()
        cp_async_bulk_wait_group(0, read=True)        # 收尾：等全部 store 排空
```

几个值得咀嚼的细节：

- **tile 坐标的两套行号**：源侧行块号是 `expert * MTILES + mt`，目的侧是 `slot * MTILES + mt`——同一个 `(mt, nt)` tile 在源和目的的第 0 维落点不同，这正是「把专家 e 的权重搬进槽 b」的全部地址逻辑。
- **TILES_PER_EXPERT**：\(\text{TILES\_PER\_EXPERT} = \frac{H}{128} \times \frac{H'}{128}\)。例如生产形态 `H=7168, Hp=3072` 时为 \(56 \times 24 = 1344\) 个 tile/专家。
- **延迟释放的带宽账**：`consumer_release` 归还 stage 给生产者，但必须等 S2G 已经**读完** smem。若消费者攒满 `stages-1` 个 store 组才释放（朴素的滞后策略），生产者实际上只能看到约 1 个空闲 stage，在途 load 数趋近 1，单 SM 带宽退化为 \(\text{tile\_bytes}/\text{RTT}\)（RTT 为 NVLink 远程读往返延迟）——延迟完全无法被摊销。代码改为 `issued >= 2` 后就 `wait_group(1)`（等至多 1 组在途）再释放：释放仅滞后发行 1 组，生产者始终有 `stages-2` 个 stage 可提前填充，多个在途 load 重叠，延迟被流水线摊掉。源码注释原话：「Lagging by stages-1 groups instead would keep stages-1 slots hostage and cap the producer at ~1 in-flight load」。
- **空计划**：全 `-1` 时 `total_tiles == 0`，两个循环体零次执行，内核只是穿过 warp 分裂和收尾的 `wait_group(0)`——测试专门有一个 `empty_plan` 用例验证哨兵一个都不变。
- **64 位寻址**：2D 布局的列 stride 以 `cutlass.Int64` 构造。生产形态下 `expert_id * H * Hp` 在专家号 ≥ 98 时越过 \(2^{31}\) 元素，任何 32 位中间量都会回绕——测试的 `i64_offset_7168x3072` 用例专门抓这类错误。

#### 4.2.3 源码精读

模块文档即设计总纲（2D TMA、warp 0/1 分工、128×128 tile、scale 张量的重排提示）：

- [moonep/prefetch.py:L1-L16](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L1-L16)：内核设计文档。

类常量固定了启动几何与 tile 形状：

- [moonep/prefetch.py:L39-L46](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L39-L46)：`NUM_THREADS=64`、`PRODUCER_WARP=0`、`CONSUMER_WARP=1`、`M_BLOCK=N_BLOCK=128`。

构造期按设备 smem 预算自选流水线深度（与 u4-l2 dispatch 的 `_pick_stages` 同款思路）：

- [moonep/prefetch.py:L48-L72](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L48-L72)：`__init__` 调 `_pick_stages`，候选 `(6,5,4,3,2)`，全放不下则抛错。
- [moonep/prefetch.py:L74-L84](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L74-L84)：`_smem_bytes`——stage tile 区（对齐 128）+ mbarrier 区（`stages*2*8` 字节，对齐 16）+ 压缩表区（`2*B*4` 字节，对齐 16）+ 256 字节余量。
- [moonep/prefetch.py:L86-L90](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L86-L90)：从 6 往下试，返回第一个满足预算的深度。

宿主入口 `__call__`（`@cute.jit`）：形状全部 `const_expr` 特化；**关键一步是把 3D 视为 2D**——注意 `Hp64 = cutlass.Int64(Hp)` 与行主序布局 `(src_rows, Hp), stride=(Hp64, 1)`：

- [moonep/prefetch.py:L92-L121](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L92-L121)：`src_rows=E*H`、`dst_rows=B*H`，两个 `make_tensor` 把源/目的都建成 `[rows, Hp]` 的 2D 张量，列 stride 强制 64 位。
- [moonep/prefetch.py:L124-L140](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L124-L140)：`make_ordered_layout((M_BLOCK,N_BLOCK), order=(1,0))` 是 smem tile 的 swizzle 布局；`make_tiled_tma_atom` 分别为 G2S 与 S2G 构建 TMA 描述符。
- [moonep/prefetch.py:L142-L149](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L142-L149)：启动几何 `grid=(num_sms,1,1)`、`block=(64,1,1)`、`smem=smem_bytes`——持久化 CTA + 2 warp。

设备内核的 smem 分配与流水线构建：

- [moonep/prefetch.py:L151-L187](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L151-L187)：`SmemAllocator` 依次放 mbarrier 存储、`exp_tab`/`slot_tab` 两张 `Int32[B]` 表、以及 `[128,128,stages]` 的 tile 缓冲（对齐 128）。
- [moonep/prefetch.py:L189-L206](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L189-L206)：`zipped_divide` 把 2D 张量按 cta_tiler 切块；`tma_partition` 以 `make_layout(1)` 声明单参与者（TMA 由单线程发射，u4-l1 的约束）。
- [moonep/prefetch.py:L208-L214](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L208-L214)：`PipelineTmaAsync.create(..., tx_count=TILE_BYTES)`——每个 stage 的 mbarrier 预期收到恰好一个 tile 的字节数，TMA 完成时事务计数归零，`consumer_wait` 据此感知数据就绪。

计划压缩——注意注释解释了为什么压缩后不需要块同步：

- [moonep/prefetch.py:L216-L228](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L216-L228)：所有线程跑同样的扫描、写同样的值，各自读回的就是自己写的，warp 分裂前无需 barrier；`n_active * TILES_PER_EXPERT` 得到 flat tile 总数。

生产者 warp：

- [moonep/prefetch.py:L230-L251](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L230-L251)：`range(bidx, total_tiles, num_sms)` 认领 tile；`producer_acquire` 等空闲 stage；`src_mt = expert * MTILES + mt` 定位源行块；`cute.copy(tma_g2s, ...)` 发射 G2S，`tma_bar_ptr` 让 TMA 硬件在完成时敲 mbarrier。

消费者 warp 与延迟释放：

- [moonep/prefetch.py:L253-L291](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L253-L291)：`consumer_wait` 等 tx 归零；`dst_mt = b * MTILES + mt` 定位目的行块；S2G 后 `commit_group`，`issued>=2` 起 `wait_group(1, read=True)` + `consumer_release`。
- [moonep/prefetch.py:L281-L289](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L281-L289)：延迟释放的注释原文——滞后 `stages-1` 组会让生产者只有约 1 个在途 load，单 SM 带宽退化为 `tile_bytes / remote latency`。
- [moonep/prefetch.py:L291](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L291)：循环外 `cp_async_bulk_wait_group(0, read=True)` 排空全部在途 store 后内核才退出。

#### 4.2.4 代码实践

**实践目标**：不依赖 GPU，用纸笔 + 脚本推演内核的启动参数——smem 用量、stage 深度、tile 划分与 CTA 认领，把 4.2.2 的流程落到具体数字上。

**操作步骤**（示例代码）：

```python
# prefetch_geometry.py —— 复刻 _smem_bytes/_pick_stages 与 tile 几何（示例代码）
def round_up(n, a): return (n + a - 1) // a * a

M_BLOCK = N_BLOCK = 128
def smem_bytes(stages, elem_bytes, B):
    tile = M_BLOCK * N_BLOCK * elem_bytes
    return (round_up(stages * tile, 128) + round_up(stages * 2 * 8, 16)
            + round_up(2 * B * 4, 16) + 256)

def pick_stages(budget, elem_bytes, B):
    for s in (6, 5, 4, 3, 2):
        if smem_bytes(s, elem_bytes, B) <= budget:
            return s
    return 0

# 设备预算：torch.cuda.get_device_properties(i).shared_memory_per_block_optin - 1024
# 典型值（待本地验证）：sm_90 = 232448, sm_80 = 166912
for name, budget in [("sm_90?", 232448 - 1024), ("sm_80?", 166912 - 1024)]:
    print(name, "bf16 B=32 ->",
          pick_stages(budget, 2, 32), "stages,",
          "int8 B=32 ->", pick_stages(budget, 1, 32), "stages")

# tile 几何：测试的 pipeline_wraparound 用例
H, Hp, num_sms = 512, 384, 2
experts = [3, 15, 3, 0, 8, 15, 7, 1]           # E=16, 全部 >= 0
MT, NT = H // M_BLOCK, Hp // N_BLOCK
TPE = MT * NT
n_active = sum(e >= 0 for e in experts)
total = n_active * TPE
print(f"MTILES={MT} NTILES={NT} TILES_PER_EXPERT={TPE} "
      f"n_active={n_active} total_tiles={total}")
print("CTA0 认领", len(range(0, total, num_sms)), "个 tile；",
      "CTA1 认领", len(range(1, total, num_sms)), "个 tile")

# 64 位寻址的必要性：生产形态
E, H2, Hp2 = 104, 7168, 3072
per_expert_elems = H2 * Hp2                      # = 22_020_096
for e in (97, 98):
    print(f"expert {e} 起始元素号 = {e * per_expert_elems}",
          "(> 2^31)" if e * per_expert_elems > 2**31 else "(< 2^31)")
```

**需要观察的现象**：`pipeline_wraparound` 用例算出的 `total_tiles` 应为 96（8 个活跃条目 × 12 tile），两 CTA 各 48 个，远超 stage 上限 6——这正是该用例名「流水线回绕」的含义：`producer_acquire`/`consumer_release` 的 stage 环形要在一次 launch 里转很多圈。`i64` 部分应显示专家 97 起始元素号小于 \(2^{31}\)、专家 98 越过——与测试注释「expert ids >= 98 at 7168x3072」一致。

**预期结果**：bf16 B=32 在 231424 预算下选 6 stages（约 197 KiB），在 165888 预算下选 5；`MTILES=4, NTILES=3, TILES_PER_EXPERT=12, n_active=8, total_tiles=96`；CTA0/CTA1 各 48。设备属性值属「待本地验证」——请以 `torch.cuda.get_device_properties(...).shared_memory_per_block_optin` 的实际返回为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `H` 和 `H'` 必须是 128 的倍数？这个约束从哪来？

**答案**：tile 形状在类里固定为 `M_BLOCK=N_BLOCK=128`，且内核没有实现边界 tile 的处理（`MTILES=H//M_BLOCK`、`NTILES=Hp//N_BLOCK` 直接整除）。模块文档明说这是「first implementation」的简化；`launch_prefetch` 用断言 `H % 128 == 0 and Hp % 128 == 0` 把约束前移到宿主侧。scale 张量尾维是 `K/32`（几乎不可能是 128 的倍数），所以要用 `retile_for_prefetch` 重排成 `[N,128,X]` 再进内核（u5-l2）。

**练习 2**：把消费者改成「攒满 `stages-1` 个 store 组再统一释放」，功能上还正确吗？性能上会怎样？

**答案**：功能正确——release 只是归还 stage，晚归还不改变数据。但性能会崩：生产者的 `producer_acquire` 长期无 stage 可用，在途 load 被压到约 1 个，NVLink 远程读的往返延迟无法被任何并发摊销，单 SM 带宽退化为 `tile_bytes / RTT`。这就是源码注释 L281-L289 解释的取舍。

**练习 3**：dispatch 内核出口有 `cross_rank_barrier`，prefetch 内核为什么没有？

**答案**：dispatch 直写**远端**接收区，必须显式发布数据可见性并等所有 rank 到齐；prefetch 是**单边本地计划拷贝**——目的内存（预取槽）只属于本 rank，源端只被远程读不被动，读放没有任何需要同步的写入方。测试文档明确说明「prefetch is a one-sided local-plan copy with no cross-rank sync and no state that survives a launch」。内核唯一的同步是 warp 间流水线与出口的 `wait_group(0)`（本地 store 排空）。

### 4.3 launch_prefetch 封装：契约校验、编译缓存与测试验证

#### 4.3.1 概念说明

`launch_prefetch` 是预取内核唯一的宿主入口（`Buffer.prefetch_weight` 经 `_launch_full_weight_prefetches` 间接调用它，基准测试 `bench_vs_deepep.py` 也直接用它）。它承担三件事：

1. **契约校验**：dtype 白名单、连续性、秩、形状一致性、`H/Hp` 整除 128、`experts_to_copy` 的 int32/长度/同设备。把所有非法输入挡在 JIT 编译之前，报错信息可读。
2. **编译缓存**：`_get_compiled` 用 `functools.lru_cache` 以 `(E, H, Hp, B, num_sms, device_index, dtype)` 为键缓存 cubin——指针**不在**键里：几何在编译期烧死（u4-l1），运行期只换 `data_ptr`。同一形状的三个投影（gate/up/down）只编译一次。
3. **零拷贝直发**：校验通过后构造三个 `make_ptr`（源、目的、专家表），取 `torch.cuda.current_stream()` 直接发射。它在**当前流**上运行——异步语义由上层 `Buffer.prefetch_weight(async_finish=True)` 切到 comm stream 实现。

类型白名单 `_ELEM_TYPES` 是 dtype 支持的唯一登记处：bf16（`BFloat16`, 2 字节）、int8、uint8。拷贝是**类型无关**的纯字节搬运，所以 MXFP4 打包权重（uint8，e2m1 每字节两个值）和 ue8m0 scale 走同一条路径——这是 u5-l2 的主题，本讲只需知道映射表的存在。

测试侧，`tests/test_prefetch.py` 的校验逻辑（规格里称 `assert_prefetched`，实际是 `run_case` 内联的检查，没有独立同名的辅助函数）非常值得精读，它是「预取语义」的可执行规格：

- 槽 `b` 的计划 `e >= 0`：`torch.equal(prefetch_buffers[b], remote_expert[e])` —— **逐位相等**，没有容差（纯拷贝不容许任何差异）。
- 槽 `b` 的计划 `e == -1`：该槽必须**原封不动**——测试预填哨兵值（bf16 用 `-123.0`、uint8 用 `0xAB`），事后检查哨兵未变，证明内核不写未使用的槽。
- 全组聚合：`ok` 标志经 `all_reduce(MIN)` 汇总，任何 rank 失败都让全体断言失败（u6-l4 会展开这套方法论）。

#### 4.3.2 核心流程

```text
launch_prefetch(remote_expert[E,H,H'], prefetch_buffers[B,H,H'], experts[B], num_sms)
    ├─ prefetch_buffers.numel()==0 或 experts.numel()==0 → 直接 return（空计划免发射）
    ├─ 断言：dtype ∈ {bf16, int8, uint8}
    ├─ 断言：两张量连续、rank-3、H/H' 一致；experts 是连续 int32 且长度 == B
    ├─ 断言：H % 128 == 0 且 Hp % 128 == 0；num_sms 为正整数
    ├─ 断言：CUDA 张量且 experts 与目的同设备
    ├─ compiled = _get_compiled(E, H, Hp, B, num_sms, device_index, dtype)   # lru_cache
    └─ compiled(src_ptr, dst_ptr, experts_ptr, CUstream(current_stream))
```

`Buffer.prefetch_weight` 的异步路径（承接 u6-l1 的主/通信流握手模式，此处先见实例）：

```text
async_finish=True:
    _record_streams(所有输入张量, comm)     # 生命周期保护
    comm.wait_event(main_stream.record_event())
    with torch.cuda.stream(comm):
        _run_prefetch_weight_on_current_stream(...)   # 切到 comm 流后再取 current_stream
        done = comm.record_event()
    return done
```

#### 4.3.3 源码精读

- [moonep/prefetch.py:L32-L36](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L32-L36)：`_ELEM_TYPES`——`torch.dtype → (cutlass 类型, 字节数)` 的登记表，当前三项：bf16/int8/uint8。

编译缓存两件套：

- [moonep/prefetch.py:L294-L296](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L294-L296)：`_max_smem_per_block_optin`——设备 smem 预算也按设备号缓存。
- [moonep/prefetch.py:L299-L332](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L299-L332)：`_get_compiled` 的键是 `(E,H,Hp,B,num_sms,device_index,dtype)`；`smem_budget = optin - 1024`；占位指针（`data_ptr=0`）只用于编译特化，真指针在调用时传入——u4-l1「运行期只换指针不换几何」的又一次体现。

宿主封装本体：

- [moonep/prefetch.py:L357-L374](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L357-L374)：docstring 写明全部契约——`experts_to_copy` 是 `[0,E)` 内的专家号或 `-1`，**未使用的槽不会被内核写入**。
- [moonep/prefetch.py:L375-L376](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L375-L376)：空张量早退。
- [moonep/prefetch.py:L378-L404](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L378-L404)：dtype/连续性/秩/形状/整除/num_sms 全套断言。
- [moonep/prefetch.py:L406-L434](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L406-L434)：设备一致性检查、`_get_compiled` 取 cubin、三个 `make_ptr`（`assumed_align=16` 与权重张量的 16 字节对齐约定呼应）、以当前流发射。

上层入口的同步/异步两条路径：

- [moonep/api.py:L919-L926](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L919-L926)：同步模式直接在当前流跑，返回 `None`。
- [moonep/api.py:L928-L948](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L928-L948)：异步模式——`_record_streams` 保护输入生命周期、事件握手切 comm 流、`with torch.cuda.stream(comm)` 内发射并记录 `done` 事件返回。

测试的校验逻辑（本讲实践的对象）：

- [tests/test_prefetch.py:L1-L19](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_prefetch.py#L1-L19)：测试文档——覆盖轴包括单 tile/多 tile/矩形 tile/生产形态（>2^31 偏移）、手工计划（空洞/重复/极值）/空计划/随机计划、以及 tile 数远超 stage 数的流水线回绕；并说明为何没有 barrier/重复 launch 用例（无跨 rank 同步、无跨 launch 状态）。
- [tests/test_prefetch.py:L274-L299](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_prefetch.py#L274-L299)：`run_case` 搭建——建单 owner 专家张量、取远程 owner、构造 `experts_to_copy`、预填哨兵、launch 后同步。
- [tests/test_prefetch.py:L301-L324](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_prefetch.py#L301-L324)：**逐槽校验**——`e<0` 则槽必须仍是哨兵；`e>=0` 则 `torch.equal(prefetch_buffers[b], remote_expert[e])` 逐位相等；失败打印槽号/专家号/owner/最大差；`all_reduce(MIN)` 全组聚合。
- [tests/test_prefetch.py:L102-L125](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_prefetch.py#L102-L125)：两个关键用例——`pipeline_wraparound`（96 tile / 2 CTA，注释解释回绕动机）与 `i64_offset_7168x3072`（专家号 ≥98 越过 \(2^{31}\)，抓 32 位寻址回绕）。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：把 `run_case` 的校验逻辑读成「可执行的规格」，然后写一个脚本：给定任意 `experts_to_copy`，推导每个预取槽**应该**等于哪个源专家行，并用 PyTorch 模拟内核语义自检——这一步之后，如果有多卡环境，你可以直接把同一推导用于核对真实内核输出。

**操作步骤**：

1. 精读 [tests/test_prefetch.py:L301-L324](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_prefetch.py#L301-L324)，注意三个要点：`e<0` 的槽以「哨兵未变」为通过条件；`e>=0` 用 `torch.equal`（逐位、无容差）；结果经 `all_reduce(MIN)` 变成全组断言。
2. 编写 `prefetch_expectation.py`（示例代码，单进程单卡即可跑，不需要 torchrun）：

```python
# prefetch_expectation.py —— 预取槽期望值推导 + 内核语义模拟（示例代码）
import torch

def derive_expectation(remote: torch.Tensor, experts: list[int], sentinel):
    """对给定 experts_to_copy 推导每个预取槽的期望内容。
    返回 (expected, touched) —— touched[b] 为 False 表示该槽必须保持哨兵。"""
    B = len(experts)
    exp = torch.full_like(remote[:B], sentinel)   # 预填哨兵
    touched = []
    for b, e in enumerate(experts):
        if e >= 0:
            exp[b] = remote[e]                    # 槽 b ← 专家 e 的整行
            touched.append(True)
        else:
            touched.append(False)                 # 空槽：内核不写
    return exp, torch.tensor(touched)

def simulate_kernel(remote, experts, sentinel, num_sms):
    """按 PrefetchKernel 语义模拟：逐 (i, mt, nt) tile 从源拷到目的。
    这里直接按槽级模拟（tile 级与槽级结果等价——同一专家的所有 tile
    连续落在同一槽内）。"""
    B = len(experts)
    out = torch.full_like(remote[:B], sentinel)
    n_active = sum(e >= 0 for e in experts)
    MT, NT = remote.shape[1] // 128, remote.shape[2] // 128
    total_tiles = n_active * MT * NT
    # flat tile -> (i, mt, nt) 的分解与 CTA 认领（与内核一致）
    for tile in range(total_tiles):
        cta = tile % num_sms                       # range(bidx, total, num_sms)
        i, rem = divmod(tile, MT * NT)
        mt, nt = divmod(rem, NT)
        b = [x for x in experts if x >= 0][i]      # 仅演示：slot_tab 逆查
        # 真正的字节搬运，这里用切片等价表达
        out[i] = remote[b]                          # 占位：见下方说明
    return out

if __name__ == "__main__":
    torch.manual_seed(0)
    E, H, Hp, B = 8, 256, 384, 5
    remote = torch.randn(E, H, Hp, dtype=torch.bfloat16)
    experts = [7, 0, 4, 4, -1]                     # 含空洞与重复专家
    exp, touched = derive_expectation(remote, experts, sentinel=-123.0)

    assert torch.equal(exp[0], remote[7])
    assert torch.equal(exp[3], remote[4])           # 重复专家：两个槽各得一份
    assert torch.equal(exp[4], torch.full_like(exp[4], -123.0)), "空槽必须保持哨兵"
    print("期望推导自检通过：",
          f"n_active={touched.sum().item()}/{B}，重复专家 4 同时进槽 2 和槽 3")
```

3. 把 `simulate_kernel` 里标了「占位」的行改为**真正的 tile 级模拟**：对每个 `(i, mt, nt)`，执行 `out[slot, mt*128:(mt+1)*128, nt*128:(nt+1)*128] = remote[expert, mt*128:(mt+1)*128, nt*128:(nt+1)*128]`（`slot` 用紧凑表的逆映射 `[b for b,e in enumerate(experts) if e>=0][i]`），然后断言 `torch.equal(simulate_kernel(...), derive_expectation(...)[0])`。

**需要观察的现象**：期望推导对重复专家（示例中的 4 号同时进槽 2 和槽 3）会得到**两份独立的拷贝**——内核读同一个源两次、写两个不同的目的槽；空槽的期望值就是哨兵本身；tile 级模拟与槽级推导在任意 `experts`（含全 `-1`）下逐位一致。

**预期结果**：脚本打印「期望推导自检通过」。第 3 步的 tile 级等价断言在随机数据上全部通过。若要在真实内核上验证同一推导，需 `torchrun --nproc_per_node=8 -m pytest -s tests/test_prefetch.py`（NVLink 互联的多卡），本环境无法替你运行——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`_get_compiled` 的缓存键里为什么没有 `data_ptr`？gate/up/down 三个投影会导致三次 JIT 编译吗？

**答案**：`const_expr` 把 `E/H/Hp/B/num_sms` 烧进 cubin，运行期只换指针（u4-l1 的编译模型）；三个投影形状相同，命中同一个 `lru_cache` 键 `(E,H,Hp,B,num_sms,device_index,dtype)`，只编译一次、发射三次。

**练习 2**：把 `float16` 的权重张量传给 `prefetch_weight` 会发生什么？若要支持，最少要改哪里？

**答案**：`launch_prefetch` 在 `assert dtype in _ELEM_TYPES` 处报错（`Buffer.prefetch_weight` 的断言会更早一步拦下）。最小改动是在 `_ELEM_TYPES` 加 `torch.float16: (Float16, 2)`（拷贝本身类型无关），再同步检查 `_smem_bytes` 的 `elem_bytes` 分支与相关测试——这正是 u6-l6 扩展练习的主题。

**练习 3**：`empty_plan` 用例（全 `-1`）里内核实际执行了什么？为什么它不会挂起？

**答案**：压缩后 `n_active=0`、`total_tiles=0`，两个 warp 的 `for tile in range(bidx, 0, num_sms)` 零次迭代，消费者直接走到循环外的 `cp_async_bulk_wait_group(0, read=True)`——没有在途组，立即通过，内核退出。不会挂起是因为流水线的等待都发生在循环体内（`acquire`/`wait` 都不会被零次循环触达），哨兵一个字节都不被写。

## 5. 综合实践

**任务：为一次真实配置写出「预取可行性核对单」。** 把本讲三个模块串起来，写一个脚本 `prefetch_checklist.py`（示例代码，纯 CPU），输入 `(E, R, H, Hp, dtype, B, experts_to_copy, num_sms, 层数 L)`，输出并自检以下五项：

1. **布局合法性**：`B` 是否等于 `E/R`（训练模式报警告，推理模式放行）；`H`、`Hp` 是否是 128 的倍数；`len(experts_to_copy) == B`。
2. **显存账**：单投影参数量、池化预取槽开销（`B` 行）、无池化对照（`L × B` 行），复用 4.1.4 的公式。
3. **搬运量**：`n_active × H × Hp × elem_bytes`，并换算成「按 400 GB/s 有效 NVLink 带宽估算的秒数」（数量级估算即可）。
4. **内核几何**：`MTILES/NTILES/TILES_PER_EXPERT/total_tiles`、每 CTA 认领的 tile 数（复用 4.2.4 的函数）、给定 smem 预算下选中的 `stages`。
5. **期望状态**：对每个槽输出「槽号 → 源专家行」的映射表（复用 4.3.4 的 `derive_expectation`），空槽标注 `-1 (untouched)`。

用两组数据自检：`(E=256, R=8, H=7168, Hp=3072, bf16, B=32, L=80)` 的生产形态，与 `(E=8, H=256, Hp=384, B=5, experts=[7,0,4,4,-1])` 的小形态（后者应与 4.3.4 的输出一致）。如果有多卡 NVLink 环境，再把小形态放进 `tests/test_prefetch.py` 的 `CASES` 里跑一遍真实内核做终极对拍——**待本地验证**。

这个脚本之后就是你排查预取问题的第一工具：任何「预取结果不对」的 bug，先跑核对单分清是**计划错了**（experts_to_copy 内容）、**布局错了**（形状/对齐）、还是**内核错了**（逐位不等但前两者都对）。

## 6. 本讲小结

- 权重契约是**每个投影一个连续 `[E+B, H, H']` 对称内存张量**：行 `[0,E)` 物理上就是各 home rank 的参数显存（可直读），行 `[E,E+B)` 是本地预取槽；分组 GEMM 只按行号 + `cu_seqlens` 寻址。
- `B` 的规则：训练必须 `E/R`（`z` 矩阵每列至多一个非零的算法保证，且梯度侧按此镜像）；推理推荐 `3–4`，溢出经对称映射远程直读 home rank——预取是带宽优化项而非正确性依赖项。
- 预取槽物理内存来自**进程级共享池**，额外成本是每投影 `B` 个专家权重的总量、与层数无关；池的组装靠 VMM 重映射，由集成框架负责（`bench_vs_deepep.py` 展示了一种 `world_size=R+1` 的拼法）。
- `PrefetchKernel` 是**持久化 2 warp CTA + 多 stage TMA 流水线**：3D 权重视为 2D 行主序矩阵，128×128 tile，warp 0 G2S 生产 / warp 1 S2G 消费，`PipelineTmaAsync` 以 `tx_count=TILE_BYTES` 衔接；计划先压缩成 `exp_tab/slot_tab` 紧凑表再遍历。
- 消费者**延迟一个 store 组就释放 stage**（`wait_group(1)` 而非攒满 `stages-1` 组），保住生产者的在途 load 数，避免单 SM 带宽退化到 `tile_bytes/RTT`。
- `launch_prefetch` 是契约守门人（dtype 白名单/连续性/128 整除/`-1` 空槽不写）+ `lru_cache` 编译缓存（指针不进键）；测试用**逐位相等 + 哨兵不变**定义预取语义。

## 7. 下一步学习建议

- **u5-l2（MXFP4 量化权重与 scale 重排）**：本讲刻意绕过的 `retile_for_prefetch`/`prefetch_retile_nbytes` 在那里展开——为什么 `K/32` 宽的 scale 张量必须重排成 `[N,128,X]` 的 uint8 视图才能进本内核。
- **u5-l3（梯度缓冲与 reduce_grad）**：`[E+B]` 布局在 fp32 梯度侧的镜像，以及预取槽梯度为何要走独立的 `[R,B,H,H']` reduce 缓冲。
- 想先看性能数字的话，读 `benchmarks/bench_prefetch.py`：它度量的正是本讲内核的带宽口径，可与 4.2.4 的理论搬运量对照。
- 复习锚点：预取计划从哪来（u3-l3 的 Phase C）、流水线原语为什么长这样（u4-l1/u4-l2）、源内存为什么可直读（u2-l2）。
