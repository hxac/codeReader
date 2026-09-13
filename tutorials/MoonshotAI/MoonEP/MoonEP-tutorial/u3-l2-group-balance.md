# 群组级均衡：surplus/deficit 平衡算法

## 1. 本讲目标

本讲拆解在线规划器的 **Phase A**——整个规划内核的第一步，也是 MoonEP「完美均衡」承诺的算法核心。读完本讲你应该能够：

1. 说清 `tpe`（tokens_per_expert）是什么、它如何从每个 rank 汇聚到 rank 0，以及 `tpe_cumsum` 的用途。
2. 理解 `group_tokens` 如何把逐专家的 token 计数归并到「home group（宿主群组）」粒度。
3. 逐行读懂 surplus/deficit 贪心平衡循环，并能证明它产出的迁移矩阵 `z` 满足三条不变量：守恒、完美均衡、每个目的 rank 至多从**一个**远程 home group 接收 token。
4. 脱离 GPU 环境，只用 PyTorch 在 CPU 上独立复现这套算法，并与 `tests/planning_reference.py` 的参考实现对拍。

本讲对应源码：`moonep/planning.py` 的 Phase A 段（L601-702）与 `tests/planning_reference.py` 的平衡段（L57-97）。Phase B/C/D 留给后续讲义。

## 2. 前置知识

### 2.1 回顾符号

| 符号 | 含义 | 来源 |
| --- | --- | --- |
| S | 每个 rank 的 token 数 | Buffer 构造参数 |
| K | 每个 token 的 top-k 路由数 | Buffer 构造参数 |
| E | 专家总数，均分到 R 个 rank | Buffer 构造参数 |
| R | EP 世界大小（rank 数） | Buffer 构造参数 |
| epn | 每个 rank 拥有的专家数，epn = E / R | 派生量 |
| N | 每个 rank 发出的路由条目数，N = S×K | 派生量 |
| CAP | 每个目的 rank 的逻辑接收容量，CAP = NvS_capacity = S×K | [moonep/api.py:277](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L277) |
| NvS | 物理 slot 数，NvS = CAP + (token_padding−1)×2×epn | [moonep/api.py:287-288](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L287-L288) |

**home group（宿主群组）**：专家 e 的「家」是编号为 e // epn 的 rank（群组），即该专家的参数权重物理上驻留在那个 rank（u1-l1 讲过：每 rank 持有 E/R 个 home 专家）。本讲的「群组级均衡」就是在 R 个群组之间搬运 token 计数。

### 2.2 tpe：每个 rank 的路由直方图

`tpe`（tokens_per_expert）是形状为 `[E]` 的 int32 张量，记录**本 rank** 的 S×K 条 topk 路由中落到每个专家的条数。它是用户在调用 `buffer.dispatch(...)` 时与 `topk_experts_sk` 一起传入的（[moonep/api.py:740-743](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L740-L743)：`tokens_per_expert: [E] int32 local token count per expert`）。它必须与 topk 严格一致——每行之和为 S×K，这是规划器一切推导的前提。

### 2.3 meta_buf：rank 间共享的便签纸

回顾 u2-l4：`meta_buf` 是一块 `[R × meta_chunk_padded]` 的 int32 对称内存，第 r 段物理上驻留在 rank r 的 GPU 上，但所有 rank 都能直接读写。其中 `[NvS, NvS + R*E)` 这一段是 **TPE 汇聚区**，文档明确写着「only rank 0 is the read/write target; symmetric reserve」（[moonep/api.py:240-251](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L240-L251)）——每个 rank 都有这段的「保留副本」，但只有 rank 0 那份真正被使用。本讲会反复用到这个布局。

### 2.4 贪心平衡的直觉

想象 R 个水桶，总水量恰好是 R×CAP（每桶目标水位 CAP）。有的桶高于 CAP（surplus，过剩），有的低于（deficit，缺口）。最朴素的均衡法：每轮挑**水位最高**的桶和**水位最低**的桶，从高桶往低桶倒水，一次把低桶**倒满到 CAP**；重复到所有桶都恰好在 CAP。这就是 MoonEP 的群组级平衡算法——唯一不朴素的点在于：倒水顺序和平局裁决是**确定性的**，这让 GPU 内核和 CPU 参考实现能逐元素对拍相等。

## 3. 本讲源码地图

| 文件 | 本讲涉及范围 | 作用 |
| --- | --- | --- |
| [moonep/planning.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L601-L702) | L601-702（Phase A） | CuTe DSL 规划内核：tpe 汇聚、tpe_cumsum/group_tokens 统计、贪心平衡循环 |
| [moonep/planning.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L197-L302) | L197-224、L257-280、L283-302 | Phase A 用到的底层助手：warp argmax/argmin 两段式归约、寄存器扫描、`copy_v4_remote` 向量化远程拷贝 |
| [tests/planning_reference.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L25-L131) | L25-131 | PyTorch 参考实现：tpe 汇聚、balance/z 贪心循环、守恒断言 |
| [moonep/api.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L274-L288) | L274-288、L376-378 | CAP/NvS 的定义、「至多一个远程 home group」注释、`z`/`group_tokens`/`alloc` 临时缓冲的分配 |
| [moonep/_common.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L249-L285) | L249-285 | `grid_sync`（CTA 间）与 `cross_rank_barrier`（rank 间）屏障，Phase A 的同步骨架（详见 u3-l6） |

注意：`z`（[R×R]）、`group_tokens`（[R]）、`alloc`（[E×R]）都是 Buffer 构造期分配的**普通本地临时缓冲**（[moonep/api.py:376-378](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L376-L378)），不是对称内存；Phase A 只使用 rank 0 上的那一份，规划结果最终经由 PLAN 区组播分发（u3-l6）。

## 4. 核心概念与源码讲解

先给 Phase A 的全局流程，再逐模块精读：

```text
Phase A（规划内核开头，只在 rank 0 上产出结果）
  A0  所有 rank：把本地 tpe[E] 远程写入 rank0 chunk 的 TPE 区（偏移 TPE_OFF + rank*E）
      （R>1 时 rank0 顺带把 topk/tpe 备份进 rank1 chunk，供 Phase C 的镜像排序，u3-l4 讲）
      cross_rank_barrier —— 保证 rank0 读汇聚矩阵前所有写已可见
  A1  rank0：清零 z[R×R] 与 group_tokens[R]，grid_sync
  A2  rank0：按 32 列 tile 分块计算 tpe_cumsum[R,E]（按源 rank 前缀和），
      并把每专家的跨 rank 总数原子累加进 group_tokens[e // epn]，grid_sync
  A3  rank0（仅 CTA0 的单个 warp）：surplus/deficit 贪心循环，写 z[h,u]，grid_sync
  A4  进入 Phase B（z 展开为逐专家 alloc，u3-l3 讲）
```

### 4.1 tpe 汇聚：把全网格路由统计搬到 rank 0

#### 4.1.1 概念说明

规划是**全局决策**：要决定「哪里的 token 发给谁」，必须先知道整个网格上每个专家有多热。但每个 rank 只有自己那份 `[E]` 直方图。Phase A 的第一步就是把 R 份 tpe 拼成一张 `[R, E]` 的汇聚矩阵 `tpe_gather`，其中第 r 行就是 rank r 的 tpe。

这步**不走任何集合通信 API**（没有 all_gather），而是利用 u2 建好的对称内存：每个 rank 直接把自己的 tpe 写进 rank 0 拥有的物理显存里第 r 行的位置。写远端显存走 NVLink，由 `copy_v4_remote` 完成 128 位向量化，省带宽也省一次「device→host→device」的往返。

#### 4.1.2 核心流程

1. 所有 rank 并行执行：`copy_v4_remote(meta, TPE_OFF + rank*E, tpe, E, ...)`——本 rank 的 E 个 int32 写进 rank0 chunk 的 TPE 区第 rank 行。
2. （仅 R>1）rank 0 额外把 `topk`（N 个 int32）和自己的 `tpe` 拷进 **rank1 chunk** 的 TOPK0/TPE 区——这是给后面 Phase C 的「rank1 替 rank0 做镜像排序」准备的原料，本讲不展开（u3-l4 精读）。
3. `cross_rank_barrier`：全员到达后才放行。没有这道屏障，rank 0 可能读到别的 rank 还没写完的行。

地址计算的关键：TPE 区起点 `TPE_OFF = _align_up(NvS, 4)`（[moonep/api.py:309](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L309)），按 4 个 int32（16 字节）对齐；rank r 的行落在 `TPE_OFF + r*E`。

#### 4.1.3 源码精读

Phase A 的汇聚三连——本 rank tpe 进 rank0 chunk、（R>1 时）rank0 备份 topk/tpe 进 rank1 chunk、跨 rank 屏障：

- [moonep/planning.py:601-609](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L601-L609)：`copy_v4_remote(meta, TPE_OFF + rank * E, tpe, E, ...)` 由**所有 rank** 执行，每个 rank 写 rank0 chunk 中属于自己的那一行；`if R > 1 / if rank == 0` 块里的两次拷贝目标是 `ms + TOPK0_OFF` 与 `ms + TPE_OFF`（`ms` 是 meta_stride，即 rank1 chunk 的起点），为 u3-l4 的镜像排序发布原料；最后 `cross_rank_barrier` 保证可见性。

向量化远程拷贝的实现——头部标量对齐、128 位主体、尾部标量：

- [moonep/planning.py:283-302](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L283-L302)：`copy_v4_remote` 先算 `head = (-dst_off) & 3`，把目标偏移补齐到 4 个 int32 的倍数，主体用 `st_global_v4_s32`（一次写 4 个 int32，NVLink 事务数 ÷4），头尾各留一小段标量循环兜底；循环以 `pid*nth+tid` 为步长做 grid-stride，全网格线程分摊。

为什么 `dst_off` 可能不对齐？`TPE_OFF` 本身 16B 对齐，但 `rank*E` 不一定是 4 的倍数——例如 E=6、r=2 时偏移多出 12 个 int32，`head` 就会是 0，但下一行 E×r 落在模 4 余 2 的位置，`head=2`。这个助手函数把对齐脏活全部封装掉，调用方不用关心。

#### 4.1.4 代码实践（源码阅读 + 算术验证，无需 GPU）

1. **实践目标**：亲手验证 `copy_v4_remote` 的 head/body/tail 切分逻辑，理解「目标偏移任意对齐」时它如何保证 128 位写不越界。
2. **操作步骤**：写一个纯 Python 小脚本（示例代码），对几组 `(NvS, E, R, r)` 计算第 r 行的目标偏移 `dst_off = TPE_OFF + r*E`（`TPE_OFF = ceil(NvS/4)*4`），再按 L290-291 的公式算 `head = (-dst_off) & 3`、`nv = (E - head) >> 2`、尾部元素数 `n - head - nv*4`。
3. **观察现象**：当 E ≡ 0 (mod 4) 时所有行 head=0、tail=0；当 E ≡ 1..3 (mod 4) 时 head 随 r 变化。
4. **预期结果**：任何输入下都有 `head ∈ [0,3]`、`head + nv*4 + tail == E`。待本地验证（纯算术，CPU 即可）。

```python
# tpe_offset_check.py（示例代码）
def split(NvS, E, R, r):
    TPE_OFF = (NvS + 3) // 4 * 4            # api.py: _align_up(NvS, 4)
    dst_off = TPE_OFF + r * E               # 第 r 行在 rank0 chunk TPE 区的偏移
    head = (-dst_off) & 3                   # planning.py: L290
    nv = (E - head) >> 2                    # planning.py: L291
    tail = E - head - nv * 4
    assert 0 <= head <= 3 and head + nv * 4 + tail == E
    return dst_off, head, nv, tail

for E in (4, 6, 7, 256):                    # E=256: 全部行 16B 对齐
    for r in range(8):
        print(E, r, split(4096 * 8 + 2 * (256 // 8), E, 8, r))
```

#### 4.1.5 小练习与答案

**练习 1**：为什么 tpe 要汇聚到 rank 0 的 chunk，而不是各 rank 写自己的 chunk 再由 rank 0 逐个读？

**答案**：写自己 chunk 再读需要两个方向的数据移动（各 rank 写 + rank0 逐段拉取），且 rank0 拉取时还要额外屏障逐 rank 同步；直接写 rank0 chunk 只有一次单向移动，rank0 只需等一道全员屏障就能看到完整矩阵。MoonEP 选择「数据向决策者集中」，代价是每次 dispatch 的规划阶段有一次 O(R×E) 的 NVLink 写入——相比 token payload 本身微不足道。

**练习 2**：`copy_v4_remote` 为什么要专门处理 head/tail 标量段，而不是直接循环写单个 int32？

**答案**：`st_global_v4_s32` 一次写 16 字节，NVLink 事务数是标量写的 1/4（planning.py L137 的注释：「NVLink transactions /4」），对 R×E 规模的汇聚能显著省带宽；但 128 位写要求地址 16B 对齐且不能越界，所以头部先标量补齐到对齐边界、尾部标量收掉不足 4 个的余数。这是「向量化收益」与「任意对齐输入」的折中。

**练习 3**：如果删掉 L609 的 `cross_rank_barrier`，最坏会发生什么？

**答案**：rank 0 可能在其它 rank 的 tpe 行尚未落盘（写还在飞行中或未发射）时就开始统计 group_tokens，读到过期/残缺数据，规划出的 z 完全失真——而且这种错误是静默的：内核不会崩溃，只会产出错误的计划。跨 rank 屏障是 Phase A 正确性的硬前提（其自复位实现见 u3-l6）。

### 4.2 tpe_cumsum 与 group_tokens：从专家计数到群组负载

#### 4.2.1 概念说明

拿到 `tpe_gather[R, E]` 后，rank 0 计算两个量：

1. **tpe_cumsum[r, e]**：对每个专家 e，沿**源 rank 维**做包含式前缀和。它记录的是「专家 e 的 token 全局序」：dispatch 时专家 e 的槽位按「rank 0 的 token 在前、rank 1 次之……」拼接成全局序列，第 r 行的前缀和恰好把「rank r 上该专家的第 i 个 token」映射为全局第 i' 个（参考实现的注释见 [tests/planning_reference.py:57-61](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L57-L61)）。本讲只负责把它算出来存好，真正的消费方是 Phase C 的 `passB`（u3-l4）。
2. **group_tokens[h]**：专家 e 的跨 rank 总数（即 tpe_cumsum 最后一行）按 home group 归并——`group_tokens[h] = Σ_{e∈group h} expert_count[e]`。这是「群组级均衡」的输入：它回答「如果不出迁移动，每个 rank 要接收多少 token」。

数学上：

\[ \mathrm{tpe\_cumsum}[r, e] = \sum_{r'=0}^{r} \mathrm{tpe}[r', e], \qquad \mathrm{expert\_count}[e] = \mathrm{tpe\_cumsum}[R-1, e] \]

\[ \mathrm{group\_tokens}[h] = \sum_{e=h\cdot \mathrm{epn}}^{(h+1)\cdot \mathrm{epn}-1} \mathrm{expert\_count}[e] \]

#### 4.2.2 核心流程

rank 0 上的并行化方案（专家维被切成 32 列的 tile，分给各 CTA）：

1. 清零 `z[R×R]`、`group_tokens[R]`（grid-stride，L615-622），`grid_sync`。
2. 每个 CTA 认领若干个 32 列 tile，把 `tpe_gather` 的 `[R, 32]` 块拷进共享内存 `s_tpe`。
3. 一个 warp（`tid < S1_COLS`，每 lane 负责一列/一个专家）对该列做**串行 R 步**前缀和：`run += s_tpe[r, tid]; s_tpe[r, tid] = run`。R 步做完，`run` 恰好是该专家的跨 rank 总数。
4. 用设备级原子加 `atomic_add(group_tokens[e // epn], run)` 把总数累进 home group 计数器（多 CTA 并发写同一 [R] 数组，必须原子）。
5. 全体线程把前缀和写回 meta 的 PLAN 区（`PB + TPE_SUB`，`TPE_SUB = E*R`，L547）——这份 tpe_cumsum 之后会随 PLAN 区一起组播给所有 rank（u3-l6）。
6. `grid_sync`，进入贪心平衡循环。

#### 4.2.3 源码精读

零初始化与 tile 划分：

- [moonep/planning.py:610-628](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L610-L628)：rank 0 先把 `z`、`group_tokens` 清零并 `grid_sync`；专家维按 `seg_raw = ceil(E/num_sms)` 向上取整到 `S1_TILE=32` 对齐后切给各 CTA——「S1_TILE alignment keeps each round processing a fixed number of columns」，保证每轮处理的列数是编译期常量。
- [moonep/planning.py:629-640](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L629-L640)：建立三个视图——`tpe_gather`（读 TPE 区的 `[R, E]` 汇聚矩阵）、`tpe_cumsum`（写 PLAN 区 `PB + TPE_SUB` 的 `[R, E]`）、`s_tpe`（共享内存里的 `[R, 32]` tile）。

每列前缀和 + 原子累加 group_tokens：

- [moonep/planning.py:641-669](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L641-L669)：L643-648 全线程把 tile 从 gmem 搬进 `s_tpe`；L652-660 中 `tid < 32` 的 lane 各管一列，`for r in range_constexpr(R)` 完全展开做包含式前缀和，注释点明「the final run accumulates the total tokens of the target rank group」，随后 `atomic_add(group_tokens.iterator + expert_idx // epn, run, scope="gpu")` 把总数累进 home group；L663-667 把前缀和写回 `tpe_cumsum`。每步之间用 `cute.arch.barrier()` 隔开共享内存的读写阶段。

注意一个工程细节：多 CTA 对 `group_tokens` 的原子加顺序不确定，但**整数加法可交换**，最终结果与串行累加完全一致——确定性没有被打乱。真正的确定性要求出现在下一节的 argmax/argmin 平局规则里。

#### 4.2.4 代码实践（CPU，PyTorch）

1. **实践目标**：用 PyTorch 验证 `tpe_cumsum` / `expert_count` / `group_tokens` 的计算，并向量化重写群组归并。
2. **操作步骤**：随机生成一个行和为 S×K 的 `tpe[R, E]`（保证 `tpe.sum(dim=1) == S*K`），先照参考实现 L61-66 的写法（cumsum + for 循环）算一遍，再用 `expert_count.view(R, epn).sum(dim=1)` 向量化算一遍（示例代码）。
3. **观察现象**：两种写法结果逐元素相等；偏斜的 tpe 下 `group_tokens` 偏离 CAP 很远。
4. **预期结果**：`torch.equal` 通过；`group_tokens.sum() == R * S * K`（总量守恒）。待本地验证。

```python
# group_tokens_check.py（示例代码）
import torch

def make_tpe(R, E, S, K, skew=0.0, seed=0):
    """行和恒为 S*K 的随机 tpe；skew>0 时后面的 group 更热。"""
    g = torch.Generator().manual_seed(seed)
    epn = E // R
    logits = torch.zeros(E)
    for h in range(R):
        logits[h * epn:(h + 1) * epn] = h * skew
    ids = torch.multinomial(torch.softmax(logits, 0), S * K,
                            replacement=True, generator=g)
    row = torch.bincount(ids, minlength=E)
    return row.unsqueeze(0).expand(R, -1).contiguous()  # 先用同一行，实践里可每 rank 独立采样

tpe = make_tpe(R=8, E=32, S=128, K=4, skew=1.0)
tpe_cumsum = tpe.cumsum(dim=0)                  # 对应参考实现 L61
expert_count = tpe_cumsum[-1]                   # 对应参考实现 L62
R, epn = 8, 4
gt_loop = torch.zeros(R, dtype=torch.long)      # 参考实现 L64-66 的循环版
for h in range(R):
    gt_loop[h] = expert_count[h * epn:(h + 1) * epn].sum()
gt_vec = expert_count.view(R, epn).sum(dim=1)   # 向量化版
assert torch.equal(gt_loop, gt_vec)
assert gt_vec.sum() == R * 128 * 4              # 总量 = R * S * K
print("group_tokens =", gt_vec.tolist(), " CAP =", 128 * 4)
```

#### 4.2.5 小练习与答案

**练习 1**：为什么 group_tokens 按 `e // epn` 归组，而不是按 `e % R` 或别的映射？

**答案**：MoonEP 的专家分片是**连续分块**：rank h 拥有专家 `[h*epn, (h+1)*epn)`。这与 Megatron 等框架的连续分片约定一致（u1-l4 讲过权重张量 `[E+B, H, H']` 的前 E 行按此顺序经对称内存映射到各 rank）。所以 `e // epn` 就是专家 e 的 home group；`e % R` 对应的是交错分片，会与权重布局错位。

**练习 2**：tpe_cumsum 为什么沿源 rank 维做前缀和，而不是沿专家维？

**答案**：它服务的是 Phase C 的「全局序号」计算：专家 e 的接收槽位按源 rank 顺序拼接（rank 0 的该专家 token 在最前）。把本 rank 在专家 e 上的第 i 个 token 转成全局序号需要「排在它前面的所有 rank 的该专家 token 数」，正是 `tpe_cumsum[rank-1, e]`（u3-l4 的 `global_rank = prev + local_cnt`）。沿专家维的前缀和没有任何消费者。

**练习 3**：这一步为什么用 `atomic_add` 而不是让每个 CTA 各自算一份 group_tokens 再归约？

**答案**：group_tokens 只有 R 个元素、又是后续单 warp 循环的直接输入，让每个 CTA 持有副本再做树形归约会引入多余的共享内存和一次显式 reduce 阶段；直接对 [R] 计数器做设备级原子加，代码最短、同步最少，且整数加法可交换，结果依然确定。

### 4.3 surplus/deficit 贪心平衡：生成迁移矩阵 z

#### 4.3.1 概念说明

现在 rank 0 手里有 `group_tokens[R]`——「不迁移时每个 rank 的接收量」。定义：

\[ \mathrm{balance}[h] = \mathrm{group\_tokens}[h] - \mathrm{CAP}, \qquad \mathrm{CAP} = S \times K \]

由于每个 rank 恰好发出 S×K 条路由，`Σ_h group_tokens[h] = R·S·K = R·CAP`，所以 **balance 之和恒为 0**：正数是过剩群组（surplus，必须送走 token），负数是有余量的群组（deficit，可以接收 token）。

贪心策略（内核注释 L684-686 原文：「surplus takes max (larger balance first, smaller rank on ties); deficit takes min (larger shortfall first, smaller rank on ties)」）：

```text
循环：
  h ← balance 的 argmax（平局取编号小的 rank）   # 最过剩的群组
  u ← balance 的 argmin（平局取编号小的 rank）   # 缺口最大的群组
  若 balance[h] ≤ 0（等价地 balance[u] ≥ 0）：结束
  move ← -balance[u]                # 一次把 u 填满到 CAP
  z[h, u] ← move
  balance[h] ← balance[h] − move    # h 可能因此变负（超调），无妨
  balance[u] ← 0
```

产出物 `z[h, u]`（[R×R] int32）读作：**home group h 要把 move 个 token 迁到目的 rank u**。它满足三条不变量：

1. **守恒与完美均衡**：循环结束时 balance 全 0，即对每个 rank d：
   \[ \mathrm{group\_tokens}[d] - \sum_u z[d, u] + \sum_h z[h, d] = \mathrm{CAP} \]
   每个 rank 最终恰好接收 CAP = S×K 个 token——这就是 u1-l1 承诺的「完美均衡」，静态形状与零拷贝的全部前提。
2. **每列至多一个非零**：u 被填满后 `balance[u] = 0`，而 0 永远不会成为 argmin（只要还有负数）也不会成为 argmax（只要还有正数），所以**每个 rank 至多当一次接收者**——它至多从**一个**远程 home group 收 token（自己的 group 除外）。这不是巧合而是设计：api.py 的 NvS 容量公式正依赖它（见下）。
3. **对角线为 0、元素非负**：h > 0 > u 保证 h ≠ u；move > 0。

不变量 2 直接决定了 NvS 上界（[moonep/api.py:279-288](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L279-L288) 的注释原文：「The current constructive planner lets each destination rank receive tokens from at most one remote home group」）：每个目的 rank 至多承载 epn 个本地专家段 + epn 个远程专家段（各至多补 token_padding−1 行 padding），所以 NvS = S×K + (token_padding−1)·2·epn 就够。**算法结构与内存布局在这一个公式里锁死在一起**——这是本讲最值得记住的一点。

#### 4.3.2 核心流程

内核里的执行配置非常克制——**整个循环只占一个 warp**（CTA 0 的前 32 个线程）：

1. `balance` 不放共享内存也不放全局内存，而是放**寄存器**：lane j 持有 `balance[j] = group_tokens[lane + j*32] - CAP`（CHUNK = ceil(R/32)，[planning.py:676-681](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L676-L681)）。R ≤ 64 时每个 lane 一个寄存器就装下。
2. 每轮用**两段式 warp 归约**找 argmax/argmin：第一段 `warp_redux_sync` 归约值，第二段把「值等于极值的 lane 才贡献自己的下标、其余贡献哨兵」再归约一次下标——平局天然取到最小（或最大）下标，且结果对全 warp 广播。
3. `while keep_balancing` 是 CuTe DSL 的**动态循环**（区别于编译期展开的 `range_constexpr`）——轮数是数据相关的，至多 R−1 轮（每个 rank 至多当一次 deficit 接收者）。
4. 更新只在寄存器里发生；每轮只有 lane 0 往 gmem 写一个 `z[h, u] = move`（值全 warp 一致，单点写入即可，且因为每个 u 至多被写一次，用 `=` 而非 `+=` 是安全的）。
5. 循环前后的 `grid_sync` 让其它 CTA 在 Phase B 里能读到完整的 z。

为什么单 warp 就够？循环体是 O(R) 寄存器扫描 × 至多 R−1 轮，R=8 时微秒级；而瓶颈本来就不在这—— centralized 规划的设计取舍是「rank 0 串行决策、换全网确定性」。

#### 4.3.3 源码精读

两段式 argmax/argmin 原语（平局规则在这里定死）：

- [moonep/planning.py:197-224](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L197-L224)：注释说明不把 value/idx 打包成一个 int32，而是跑两遍归约——先 `warp_redux_sync(v, "max")` 得极值 m，再 `warp_redux_sync(i if v == m else 2147483647, "min")` 得最小下标（哨兵 int32 最大值永远不会赢，因为合法下标都 ≥ 0）；四个变体分别对应 max/min × 平局取小/大下标。
- [moonep/planning.py:257-263](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L257-L263) 与 [moonep/planning.py:274-280](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L274-L280)：`reg_scan_argmax_min_idx` / `reg_scan_argmin_min_idx` 把扫描从「warp 分块访存」升级为「纯寄存器」——lane 直接遍历自己寄存器里的 CHUNK 段取局部极值，再交给上面的两段式归约，全程不碰 smem。

贪心循环本体：

- [moonep/planning.py:671-701](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L671-L701)：`if pid == 0 / if tid < 32` 限定单 CTA 单 warp；L676-681 初始化寄存器版的 balance（注释「balance stays in registers throughout」）；L687-688 每轮取 `(surplus, surplus_rank) = reg_scan_argmax_min_idx(...)` 与 `(deficit, deficit_rank) = reg_scan_argmin_min_idx(...)`；L689-690 `if surplus <= 0 or deficit >= 0` 结束——由于 balance 和恒为 0，两个条件实际同时触发（max ≤ 0 ⟺ 全零 ⟺ min ≥ 0），写两个是双保险；L692-694 注释点明策略「The move amount is limited by the receiver's shortfall; refill deficit_rank back to CAP in one shot」，`move_tokens = -deficit`；L695-698 只更新 `surplus_rank`/`deficit_rank` 两个 lane 的寄存器；L699-700 lane 0 写 `z_tensor[surplus_rank, deficit_rank] = move_tokens`。
- z 的 [R, R] 视图在 [planning.py:611-614](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L611-L614) 建立，清零在 L615-618；`CAP` 是编译期常量 `NvS_capacity`（[planning.py:535](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L535)），由宿主侧 `NvS_capacity = S * K`（api.py:277）传入。
- 循环结束后的 `grid_sync`（[planning.py:702](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L702)）放行所有 CTA 进入 Phase B。

「超调」是正确且必要的：设初始 balance = [3, −5, 2]。第 1 轮 h=0、u=1，move=5，z[0,1]=5，balance 变 [−2, 0, 2]——rank 0 从过剩方**翻转**成了缺口方；第 2 轮 h=2、u=0，move=2，z[2,0]=2，balance 归零。z 非零列仍各只有一个。参考实现与之逐元素一致。

#### 4.3.4 代码实践（CPU，手工推演）

1. **实践目标**：在最小用例上手工走一遍贪心循环，验证三条不变量与「超调」行为。
2. **操作步骤**：取 R=3、CAP=100，`group_tokens = [103, 95, 102]` → balance = [3, −5, 2]。在纸上（或用 10 行 Python，示例代码）按 4.3.1 的伪代码逐轮记录 (h, u, move) 并更新 balance。
3. **观察现象**：第 1 轮后 rank 0 的 balance 变负（超调），第 2 轮它反过来当接收者；总共 2 = R−1 轮。
4. **预期结果**：`z = [[0,5,0],[0,0,0],[2,0,0]]`；每列至多一个非零；`group_tokens − z.sum(dim=1) + z.sum(dim=0) == [100,100,100]`。待本地验证。

```python
# overshoot_walkthrough.py（示例代码）
balance = [3, -5, 2]            # R=3, CAP=100
z = [[0] * 3 for _ in range(3)]
while True:
    h = max(range(3), key=lambda i: balance[i])   # 平局取小下标（max 的第一语义差异，见下）
    u = min(range(3), key=lambda i: balance[i])
    if balance[h] <= 0:
        break
    move = -balance[u]
    z[h][u] = move
    balance[h] -= move
    balance[u] = 0
    print(f"h={h} u={u} move={move} balance={balance}")
# 注意：Python 的 max(range,key=...) 恰好也是平局取先出现（小）下标，与内核一致；
# 但 torch.argmax 同样返回首个最大值——三种实现平局语义统一，对拍才能逐元素相等。
```

#### 4.3.5 小练习与答案

**练习 1**：把 `move = -balance[u]`（一次填满）换成 `move = min(balance[h], -balance[u])`（不超调的温和转移），算法仍能终结于完美均衡吗？会破坏什么？

**答案**：仍能均衡（两种都是合法的装箱策略），但会破坏「每列至多一个非零」：温和转移下 u 可能没被填满，后续轮次再从别的 h' 接收，z[:, u] 出现多个非零 → 一个目的 rank 要从多个远程 home group 收 token → NvS 上界从 (token_padding−1)·2·epn 膨胀到接近 (token_padding−1)·(E+B)，静态形状契约破产。「一次填满」是刻意选择，不是疏忽。

**练习 2**：为什么内核写 `z[h,u]` 用赋值 `=` 而不是累加 `+=`？

**答案**：`balance[u] = 0` 之后 u 永远不会再被选为 deficit（0 不是最小值，只要还有负数），所以每个 u 至多被写一次，`=` 天然安全。反过来若用 `+=` 反而要担心重复加——这里省掉一次「读-改-写」也是单 warp 单写者的直接收益。

**练习 3**：surplus 用「平局取小 rank」、deficit 也用「平局取小 rank」，这个平局规则是性能优化还是正确性要求？

**答案**：正确性要求。GPU 内核与 `tests/planning_reference.py` 的 PyTorch 循环要在 18 个 KernelCase 上**逐元素相等**（见 4.4），而 PyTorch 的 `argmax`/`argmin` 返回首个（即最小下标的）极值。只要任何一侧的平局裁决不同，某些随机输入（平局在离散计数问题里很常见，比如很多 group 同为 0）就会产出不同的 z，进而不同的 dst/cu_seqlens，测试无法通过。确定性是 MoonEP 测试方法论的基石（u6-l4 展开）。

### 4.4 PyTorch 参考实现与 GPU 内核对拍

#### 4.4.1 概念说明

`tests/planning_reference.py` 是规划内核的**黄金参考**：同一个数学问题用纯 PyTorch 重写，跑在 CPU 上，供 `tests/test_planning.py` 与 GPU 内核输出逐张量比对。它分为两段：L45-97 是「汇聚 + 群组平衡」（本讲的镜像），L99-237 是「专家级展开 + 槽位映射 + 去重」（u3-l3/u3-l4/u3-l5 的镜像）。

参考实现与内核的对应关系：

| 步骤 | 内核（planning.py） | 参考（planning_reference.py） |
| --- | --- | --- |
| tpe 汇聚 | 对称内存远程写 `copy_v4_remote`（L603） | `dist.all_gather`（L46-53） |
| tpe_cumsum | warp 逐列前缀和写 PLAN 区（L652-667） | `tpe.cumsum(dim=0)`（L61） |
| group_tokens | 原子累加 `e//epn`（L660） | 切片求和循环（L64-66） |
| balance | 寄存器 `group_tokens[k] − CAP`（L678-681） | `group_tokens − CAP`（L71），`CAP = ctx["NvS_capacity"]`（L40） |
| 贪心循环 | 单 warp 两段式归约（L683-701） | `argmax`/`argmin` while 循环（L85-97） |
| 结果去向 | z 留在 rank0 scratch，供 Phase B | z 就地展开为 alloc（L99-122） |

两个值得体会的差异：(1) 汇聚用集合通信还是远程写，只是传输方式不同，语义等价；(2) 内核把 z 留作中间量，参考实现立即消费它——因为参考不需要跨内核传递数据，而 GPU 侧 z 要喂给同一次 launch 里的 Phase B。

#### 4.4.2 核心流程

参考实现平衡段的流程（全部在 CPU 张量上）：

1. `tpe = torch.stack(gathered)` 得 [R, E]（R>1 时）。
2. `tpe_cumsum = tpe.cumsum(dim=0)`；`expert_count = tpe_cumsum[R-1]`。
3. `group_tokens[h] = expert_count[h*epn:(h+1)*epn].sum()`。
4. `balance = group_tokens - CAP`。
5. while 循环：`h = balance.argmax()`（首个最大 = 平局取小下标）、`u = balance.argmin()`；`balance[h] <= 0` 则停；`move = -balance[u]`；`z[h,u] = move`；`balance[h] -= move`；`balance[u] = 0`。
6. 随后立刻做两条断言（L124-127）：逐专家守恒 `alloc.sum(dim=1) == expert_count`、逐 rank 容量 `alloc.sum(dim=0) <= CAP`——这是对 z 及其展开的双重自检。

#### 4.4.3 源码精读

- [tests/planning_reference.py:45-55](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L45-L55)：输入整备——`topk_experts` 展平、`tokens_per_expert` 若为一维则 `dist.all_gather` 成 [R, E]，再搬到 CPU。这就是本讲实践的「输入定义」。
- [tests/planning_reference.py:57-71](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L57-L71)：`tpe_cumsum`/`expert_count`/`group_tokens`/`balance`——与内核 4.2/4.3 节逐式对应；注释「sum(balance) = 0; positive means the home group is overloaded, negative means the destination rank has room」正是我们推导过的守恒式。
- [tests/planning_reference.py:73-97](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L73-L97)：`alloc` 先按 home group 放置初值（L78-80，`alloc[e, e//epn] = expert_count[e]`），再跑贪心循环产出 z（L85-97）；注释「the policy fills receivers fully, so chosen ones become balance[u] = 0」点破一次填满策略。alloc 的展开（L99-122）属下一讲。
- [tests/planning_reference.py:124-127](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L124-L127)：守恒与容量两条断言——z 的质量在参考实现内部就被自检，坏输入（例如行和不为 S×K 的 tpe）大概率在这里炸出 `AssertionError` 而不是静默产出错计划。

测试如何消费参考：

- [tests/test_planning.py:260-308](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_planning.py#L260-L308)：`test_planning_matches_reference_and_invariants` 先跑 GPU 规划内核，再跑参考实现，对 `cu_seqlens`/`zero_fill_ranges`/`experts_to_copy`/`remote_stats`/`dst` 五个输出**逐元素相等**断言，最后再查一组规划不变量。能这样比，靠的正是 4.3 讲的确定性平局规则。

#### 4.4.4 代码实践（本讲主实践：独立复现 + 对拍）

1. **实践目标**：不看 `planning_reference.py` L64-97 的循环体（只用 L45-55 的输入定义），独立用 PyTorch 实现 `tpe_cumsum → group_tokens → balance → z`，再与参考实现对拍。
2. **操作步骤**：
   - 写 `my_plan_balance(tpe, CAP)`：输入 [R, E] int64 张量，返回 `(z, group_tokens)`，策略按 4.3.1 的伪代码（含平局取小下标）。
   - 写 `ref_plan_balance(tpe, CAP)`：把 planning_reference.py L57-97 的逻辑原样抄成函数（这是对拍基准）。
   - 生成随机 tpe：对多组 `(R, E)`（如 (4,32)、(8,256)、(2,8)）与多档偏斜 skew，用 4.2.4 的 `make_tpe` 思路保证每行和为 S×K；每个配置跑 ≥ 20 个种子。
   - 断言 `torch.equal(my_z, ref_z)`，并检查三条不变量（守恒均衡式、每列至多一个非零、对角线为 0 且元素非负）。
3. **观察现象**：偏斜越大，z 中非零项越多、单次 move 越大；极度偏斜时（所有 token 挤进一个 group）会出现明显超调——那个 group 先大额送出、自身 balance 翻负后再接收。
4. **预期结果**：所有种子上 `torch.equal` 通过；三条不变量全部成立。若不通过，优先排查平局处理（是否用了会打破「首个极值」语义的向量化写法）。待本地验证。

```python
# balance_z_crosscheck.py（示例代码）
import torch

def make_tpe(R, E, S, K, skew, seed):
    g = torch.Generator().manual_seed(seed)
    epn = E // R
    logits = torch.zeros(E)
    for h in range(R):
        logits[h * epn:(h + 1) * epn] = h * skew
    ids = torch.multinomial(torch.softmax(logits, 0), S * K,
                            replacement=True, generator=g)
    return torch.bincount(ids, minlength=E).unsqueeze(0).expand(R, -1).contiguous()

def ref_plan_balance(tpe, CAP):
    """对拍基准：planning_reference.py L61-97 的忠实抄写。"""
    R, E = tpe.shape
    epn = E // R
    tpe_cumsum = tpe.cumsum(dim=0)
    expert_count = tpe_cumsum[R - 1]
    group_tokens = torch.stack([expert_count[h*epn:(h+1)*epn].sum() for h in range(R)])
    balance = group_tokens - CAP
    z = torch.zeros(R, R, dtype=torch.int64)
    while True:
        h, u = balance.argmax(), balance.argmin()   # 首个极值 = 平局取小下标
        if balance[h] <= 0:
            break
        move = -balance[u]
        z[h, u] = move
        balance[h] -= move
        balance[u] = 0
    return z, group_tokens

def my_plan_balance(tpe, CAP):
    """独立实现：不看参考循环体，按 4.3.1 伪代码自己写。"""
    R, E = tpe.shape
    epn = E // R
    expert_count = tpe.sum(dim=0)                          # 全局每专家计数
    group_tokens = expert_count.view(R, epn).sum(dim=1)    # 按 home group 归并
    balance = group_tokens - CAP
    z = torch.zeros(R, R, dtype=torch.int64)
    while balance.max() > 0:                               # balance 和恒为 0
        h = int(balance.argmax())
        u = int(balance.argmin())
        move = -int(balance[u])
        z[h, u] = move
        balance[h] -= move
        balance[u] = 0
    return z, group_tokens

def check_invariants(z, group_tokens, CAP):
    R = z.shape[0]
    assert (z >= 0).all() and z.diagonal().eq(0).all()     # 非负、不自迁
    assert (z > 0).sum(dim=0).max() <= 1                   # 每列至多一个非零
    load = group_tokens - z.sum(dim=1) + z.sum(dim=0)      # 每 rank 最终接收量
    assert load.eq(CAP).all(), load                        # 完美均衡

for R, E, S, K in [(4, 32, 256, 8), (8, 256, 512, 4), (2, 8, 64, 2)]:
    for skew in (0.0, 0.5, 1.5, 3.0):
        for seed in range(20):
            tpe = make_tpe(R, E, S, K, skew, seed)
            CAP = S * K
            z_ref, gt = ref_plan_balance(tpe, CAP)
            z_my, gt_my = my_plan_balance(tpe, CAP)
            assert torch.equal(gt, gt_my)
            assert torch.equal(z_ref, z_my), (R, E, skew, seed)
            check_invariants(z_ref, gt, CAP)
print("all cross-checks passed")
```

#### 4.4.5 小练习与答案

**练习 1**：参考实现 L91 只检查 `balance[h] <= 0`，内核 L689 检查 `surplus <= 0 or deficit >= 0`。证明两者等价。

**答案**：整个循环保持 `sum(balance) == 0`（h 减 move、u 加 move，净变化为 0）。若 max ≤ 0，则全部元素 ≤ 0 且和为 0，故全部恰为 0，min = 0 ≥ 0 也成立；反之若 max > 0，和为 0 强制 min < 0，两个条件都不触发。所以「最大值非正」「最小值非负」「全零」三者等价，写法差异只是防御风格。

**练习 2**：如果把参考实现的 `u = balance.argmin()` 换成「缺口绝对值最大的 rank」（`balance.abs().argmax()` 且要求为负），对拍还会通过吗？

**答案**：通常不会。`argmin(balance)` 与「绝对值最大的负数」在多数情况一致，但当**正的最大值超过负的绝对值**时（如 balance = [5, −3, −2]），`abs().argmax()` 会选中 +5（不是合法接收者）或需要额外过滤；即使过滤成正数之外，平局裁决也可能与 `argmin` 不同（argmin 平局取小下标，abs-argmax 平局也取小下标但比较的值不同）。对拍逐元素相等要求两套实现**在每个输入上**走完全相同的 (h, u) 序列——任何策略细节偏离都会在某组随机种子上分叉。

**练习 3**：为什么参考实现要在 L124-127 断言「逐专家守恒」和「逐 rank 容量」两条，而不是只断言 z 正确？

**答案**：z 只是中间产物，下游真正消费的是它展开成的 alloc。守恒断言（`alloc.sum(dim=1) == expert_count`）保证每个专家的 token 一个不少地被分配出去；容量断言（`alloc.sum(dim=0) <= CAP`）保证没有 rank 超收。这两条是 Phase B 展开正确性的直接检验，等价于对 z 的使用方式做了端到端验证——比单查 z 更贴近真实故障模式（比如 L99-122 的展开循环写错索引时，z 本身看起来仍然是对的）。

## 5. 综合实践

把本讲所有内容串成一个「maxvio 归零器」脚本（纯 CPU，无需 GPU）：

1. 用 `make_tpe`（4.4.4）生成一份**强偏斜**的 tpe：让一半群组极热、一半极冷，模拟 u1-l1 讲过的路由不均衡场景。
2. 计算规划前的「若不迁移」每 rank 接收量 `group_tokens`，并按 u1-l1 的定义算 maxvio：
   \[ \mathrm{maxvio} = \max_h\left(\frac{\mathrm{group\_tokens}[h]}{\mathrm{CAP}}\right) - 1 \]
3. 跑 `my_plan_balance` 得到 z，再算规划后的每 rank 接收量 `load = group_tokens − z.sum(dim=1) + z.sum(dim=0)`，重算 maxvio。
4. 断言并打印报告：规划前 maxvio 明显大于 0（偏斜越强越大），规划后恒等于 0；同时验证「每个目的 rank 至多从一个远程 home group 接收」（检查 z 每列非零数 ≤ 1），并写出该性质如何支撑 `NvS = S*K + (token_padding−1)*2*epn`（对照 [moonep/api.py:279-288](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L279-L288) 的注释逐句核对）。
5. 进阶（可选）：把 `my_plan_balance` 改成 4.3.4 的「温和转移」版本，重跑第 3-4 步，亲眼看到均衡仍达成但「每列至多一个非零」被破坏——从而理解为什么 MoonEP 必须选「一次填满」。

预期结果：无论偏斜多强，规划后每个 rank 的接收量都恰为 S×K，maxvio = 0。待本地验证。

## 6. 本讲小结

- Phase A 的数据流是「本地直方图 → 全网汇聚矩阵 → 群组负载 → 迁移决策」：各 rank 把 tpe 经对称内存远程写进 rank0 chunk 的 TPE 区（`copy_v4_remote`，128 位向量化），一道 `cross_rank_barrier` 后 rank0 独自完成全部规划计算。
- `tpe_cumsum[R,E]` 是沿**源 rank 维**的包含式前缀和，为 Phase C 的全局槽位序号预备；`group_tokens[h]` 按 `e // epn` 把逐专家计数归并到 home group，是群组级均衡的输入。
- 贪心循环每轮把「最过剩群组」配「缺口最大群组」，`move = -balance[u]` **一次填满**接收方；balance 之和恒为 0 保证终止时全零——每个 rank 恰收 CAP = S×K，maxvio 归零。
- 「一次填满」带来关键结构性质：z 每列至多一个非零 → 每个目的 rank 至多从**一个**远程 home group 接收 token → `NvS = S*K + (token_padding−1)*2*epn` 的容量上界成立。算法选择直接决定了内存布局契约。
- 超调（balance[h] 被减成负数）是正常现象：该 rank 后续轮次转为接收者；总轮数不超过 R−1。
- 确定性（含 argmax/argmin 平局取小下标）让单 warp 的 GPU 内核与 PyTorch 循环在 18 个 KernelCase 上逐元素相等——这是 MoonEP「参考实现对拍」测试方法学的根基。

## 7. 下一步学习建议

下一讲 **u3-l3《专家级分配与 top-B 预取专家选择》**接住本讲的产物：Phase B 把群组级的 `z[h,u]` 展开为逐专家的 `alloc[e,d]` 矩阵（「最大配额 × 最热本地专家」的贪心配对，planning.py L717-798），随后 Phase C 生成 top-B 的 `experts_to_copy` 与 padded 段布局。建议先带着两个问题去读：① `move` 个 token 具体从 group h 的**哪些专家**里扣？② 为什么展开后仍保证 `alloc.sum(dim=0) ≤ CAP`？读完可回到 `tests/planning_reference.py:99-131` 对照参考实现的展开循环与两条守恒断言。若想先补齐同步原语细节（`grid_sync` 的自复位计数器、`cross_rank_barrier` 的 sentinel 位翻转），可提前跳读 u3-l6。
