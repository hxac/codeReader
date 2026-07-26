# 融合派发：expand / reduce / mapping

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 MoE（专家混合）推理里「派发（dispatch）」要解决的数据搬运问题，以及为什么朴素 scatter/gather 会让显存带宽崩塌。
- 解释「fused 布局（expert-major 连续布局）」是什么、它如何把同一个专家的 token 排成连续一段，从而让每个专家的 GEMM 能连续访存。
- 读懂 `get_fused_mapping`：它如何只靠 warp shuffle + `__match_any_sync`，无锁地把 `(token, topk)` 对排进按专家分段的连续区间，并产出 `token_topk_to_pos` / `pos_to_expert` 等一系列双向映射。
- 读懂 `expand_to_fused` 与 `reduce_fused` 这对「互逆」算子：前者把 token-major 的激活 scatter 到 fused 布局，后者把专家输出加权 reduce 回 token-major。
- 读懂 `inplace_unique_group_indices`：用 128 位位图就地给每行的 group 下标去重。

本讲承接 u5-l1（打分函数）与 u5-l2/u5-l3（top-k 选取与分组路由）。打分与选取的产物是 `topk_idx`（每个 token 选中的 `num_topk` 个专家下标），本讲就从 `topk_idx` 出发，讲清「选完之后，数据该怎么搬进专家、专家算完又该怎么搬回来」。

## 2. 前置知识

- **MoE 路由的输入输出**：模型对每个 token 打出一组 logits，路由器选出 `num_topk` 个专家（`topk_idx`）和对应权重（`topk_weights`）。这一步在 u5-l1~u5-l3 已经讲过。
- **scatter / gather**：scatter 是「按一张索引表，把源数据写到目标的不同位置」；gather 是反过来「按索引表把分散的数据读回来」。MoE 派发的本质就是一对 scatter+gather。
- **连续访存（coalesced access）与 GEMM**：GPU 读显存时，一个 warp 里相邻线程访问相邻地址才算「合并访存」，带宽才高；否则访存被拆成多次零碎事务，带宽暴跌。专家计算是一次分组矩阵乘（grouped GEMM），它的输入行必须连续才能高效分块。
- **TileLang 基础**：`@tilelang.jit` + `@T.prim_func` 骨架、`T.dynamic` 运行时符号、`T.alloc_fragment` / `T.alloc_local` / `T.alloc_shared` 三级存储、`T.Parallel` / `T.serial` / `T.unroll` 循环（见 u2-l1~u2-l3）。
- **warp 与 shuffle**：一个 warp = 32 个 lane，`__match_any_sync` 是 CUDA warp 级原语，返回「与本 lane 取值相同的所有 lane」的位掩码。

## 3. 本讲源码地图

本讲涉及四个底层 kernel，外加它们的 torch 参考实现与测试：

| 文件 | 作用 |
| --- | --- |
| [tile_kernels/moe/get_fused_mapping_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/get_fused_mapping_kernel.py) | **映射构造**：从 `topk_idx` 算出 fused 布局所需的全部双向映射（最复杂的那个）。 |
| [tile_kernels/moe/expand_to_fused_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/expand_to_fused_kernel.py) | **前向 scatter**：把 token 激活按映射复制进 fused 布局。 |
| [tile_kernels/moe/reduce_fused_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/reduce_fused_kernel.py) | **反向 gather / 加权归约**：把专家输出加权求和还原成 token 输出。 |
| [tile_kernels/moe/inplace_unique_group_indices_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/inplace_unique_group_indices_kernel.py) | **组去重工具**：按行把重复的 group 下标就地改写成 `-1`。 |
| [tile_kernels/torch/expand_to_fused.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/expand_to_fused.py) | `expand_to_fused` 的纯 PyTorch 参考实现，测试对拍用。 |
| [tile_kernels/torch/reduce_fused.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/reduce_fused.py) | `reduce_fused` 的纯 PyTorch 参考实现。 |
| [tile_kernels/torch/moe.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/moe.py) | `inplace_unique_group_indices` 的纯 PyTorch 参考实现。 |
| tests/moe/test_get_fused_mapping.py 等 | 四个 kernel 各有对应测试，用 torch 参考对拍验证正确性。 |

这四个 kernel 都通过 [tile_kernels/moe/__init__.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/__init__.py) 再导出，调用入口是 `tile_kernels.moe.<wrapper>`（见 u1-l3）。

## 4. 核心概念与源码讲解

先建立一张「派发全流程」的全局图，再逐个模块精读。

一次 MoE 前向的派发数据流（自上而下）：

```
topk_idx (num_tokens, num_topk)        ← u5-l1~u5-l3 的产物
        │
        ▼  get_fused_mapping
fused 映射表（pos_to_expert / token_topk_to_pos / expert_start …）
        │
        ▼  expand_to_fused
expanded_x (num_expanded_tokens, hidden)   ← expert-major 连续布局
        │
        ▼  每个 expert 对自己的连续段做 GEMM（外部，不在本讲）
expanded_y (num_expanded_tokens, hidden)
        │
        ▼  reduce_fused
out (num_tokens, hidden)                ← token-major，加权求和还原
```

`expand_to_fused` 和 `reduce_fused` 是一对互逆算子：前者把数据按专家重排（scatter），后者按 token 重排（gather）。两者都依赖 `get_fused_mapping` 预先算好的映射表。`inplace_unique_group_indices` 则是路由阶段的一个独立小工具，不属于这条主链。

---

### 4.1 get_fused_mapping_kernel：构造 fused 派发映射

#### 4.1.1 概念说明

路由选完之后，每个 token 要被送给 `num_topk` 个专家。把所有 `(token, topk)` 对展开，总共有 `num_tokens * num_topk` 个「待处理单元」。问题是：这些单元该按什么顺序排进给专家用的显存缓冲？

- **朴素做法**：按 `(token, topk)` 的自然顺序排。于是「要送给专家 e 的那些 token」会均匀散布在整个缓冲里。专家 e 算 GEMM 时必须跨大段显存去 gather 自己的行，访存完全不连续，带宽利用率极差。
- **fused 布局**：把单元**按专家下标排序**，让专家 0 的所有单元连续排在最前，专家 1 排在后面……这样专家 e 只需要读缓冲里连续的一段 `[expert_start[e], expert_end[e])`，GEMM 可以正常分块、用 TMA、合并访存。

`get_fused_mapping` 的任务，就是给定 `topk_idx`，算出这套 fused 布局所需的**全部双向映射**。它要回答两类问题：

- 正向：第 `i` 个 `(token, topk)` 单元，该落到 fused 缓冲的哪个位置 `pos`？（→ `token_topk_to_pos`）
- 反向：fused 缓冲的第 `pos` 行，来自哪个专家、哪个 token、哪个 topk 槽？（→ `pos_to_expert` / `pos_to_token` / `pos_to_token_topk`）

外加每个专家的连续段范围 `expert_start` / `expert_end`，以及每段的对齐计数 `num_tokens_per_expert`。

为了让每个专家的段在 GEMM 里能整齐分块，每个专家的段长要**向上对齐**到一个 `alignment`（如 16 或 128），不足的尾部用 `pos_to_expert = -1` 的 padding 行补齐（这些行会被 expand 填 0，被 GEMM 当哑行）。

#### 4.1.2 核心流程

整个 kernel 是「两遍扫描」结构，全程几乎不用原子（只在第一遍用 warp 内对共享内存的 `atomic_add` 做局部直方图）：

```
预备：把 numel = num_tokens*num_topk 个单元，用 divide_task 切分到各 global warp

第一遍（计数 / 算分段范围）：
  1. 每个 warp 扫自己负责的单元，对每个 expert 做局部直方图计数
     （atomic_add 到 experts_sum_per_warp_shared[warp, expert]）
  2. warp 间前缀求和 → 得到每个 expert 的总数
  3. 对每个 expert 的总数向上 align(alignment) → 算 expert_start/expert_end
  4. cumsum 得到各 expert 的排前专家的累计偏移 exclusive_prefix

第二遍（scatter / 写映射）：
  5. 再扫一遍同一批单元，用 __match_any_sync 找出 warp 内同 expert 的 lane
  6. popcount(mask & lane_mask) 得到本 lane 在同 expert 中的序号
  7. pos = prefix_count - 序号 → 同 expert 的单元自动落到连续位置
  8. 写出 token_topk_to_pos[i]=pos 及三张反向表
```

关键直觉：**同一 warp 内、同一个专家的 lane，靠 `__match_any_sync` 就能算出彼此的相对序号，从而无需任何全局锁就落到连续位置**。跨 warp 的衔接由第一遍算好的 prefix 偏移保证。

#### 4.1.3 源码精读

**任务切分宏 `divide_task`**：把 `length` 个单元均分给 `num_tasks` 个 warp，每段大小先 `ceildiv` 再 `align(.., 32)`，第 `task_id` 段为 `[start, end)`。

```python
@T.macro
def divide_task(length, num_tasks, task_id, start, end):
    length_per_task = align(T.ceildiv(length, num_tasks), 32)
    start = task_id * length_per_task
    end = T.min(start + length_per_task, length)
```

参见 [get_fused_mapping_kernel.py:L10-L14](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/get_fused_mapping_kernel.py#L10-L14)。`align(.., 32)` 是为了让每段起点是 warp 大小的倍数，使第二遍的 `match_any_sync` 落在整齐的 warp 边界上。

**网格与线程**：grid 用 `num_sms` 个 block（每个 SM 一个 block，硬件感知，见 u10-l1），每 block `num_threads` 个线程（≥ num_experts，向上取到 2 的幂）。先派生每个线程的 `(warp_idx, lane_idx, global_thread_idx, global_warp_idx)`，见 [L55-L60](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/get_fused_mapping_kernel.py#L55-L60)。

**第一遍：局部直方图 + warp 间前缀和**。每个线程步进扫描全局单元，对命中的 expert 在本 warp 的共享直方图里 `atomic_add` 计数：

```python
for i in T.serial(start + lane_idx, end, warp_size):
    expert_idx = topk_idx_1d[i]
    if expert_idx != -1:
        T.atomic_add(experts_sum_per_warp_shared[warp_idx, expert_idx], 1)
```

参见 [L90-L95](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/get_fused_mapping_kernel.py#L90-L95)。注意 `atomic_add` 只打到**块内共享内存**且**每 warp 一份**，竞争远小于打全局原子；之后再做 warp 间前缀和（[L99-L102](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/get_fused_mapping_kernel.py#L99-L102)），每个 SM 还要把「排在前面的 SM」对每个 expert 的计数累加进来（`num_experts_per_sm` 的逐 SM 求和，[L113-L119](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/get_fused_mapping_kernel.py#L113-L119)）。

**分段范围**：每个 expert 的总数 `align(alignment)` 后做 `cumsum`，得到对齐后的段起点 `expert_start` 与终点 `expert_end`：

```python
expert_num_elements_aligned = align(expert_num_elements, alignment)
cumsum_shared[thread_idx] = expert_num_elements_aligned
...
exclusive_prefix = cumsum_shared[thread_idx] - expert_num_elements_aligned
...
expert_start[thread_idx] = exclusive_prefix
expert_end[thread_idx] = exclusive_prefix + expert_num_elements_aligned
```

参见 [L120-L137](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/get_fused_mapping_kernel.py#L120-L137)。`align` 造成的 padding 段，其 `pos_to_expert` 保持初始化时的 `-1`，后续 expand 会把它们填 0。

**第二遍：无锁 scatter**。这是全 kernel 最精巧的一段。对每个单元，用 CUDA warp 原语 `__match_any_sync` 拿到「与本 lane 同 expert 的所有 lane」位掩码，再用 `popcount(mask & lane_mask)`（`lane_mask` 是「lane 下标 ≤ 本 lane」的掩码）算出本 lane 在同 expert 中的序号，进而得到连续位置：

```python
mask = T.call_extern(T.uint32, '__match_any_sync', 0xFFFFFFFF, expert_idx)
count = T.popcount(mask & lane_mask)
...
prefix_count = experts_sum_per_warp_shared[warp_idx, expert_idx]
pos = prefix_count - count
...
token_topk_to_pos_1d[i] = pos
pos_to_expert[pos] = expert_idx
pos_to_token[pos] = i // num_topk
pos_to_token_topk[pos] = i
```

参见 [L147-L160](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/get_fused_mapping_kernel.py#L147-L160)。其中 `lane_mask` 由位运算 `(1<<lane_idx)+(1<<lane_idx)-1` 构造（[L142](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/get_fused_mapping_kernel.py#L142)），即「lane 0..lane_idx」的掩码。同 expert 的若干 lane 各自得到不同 `count`，于是各写不同 `pos`，自然连续、无需原子。每个 expert 在本 warp 的「最后一个 lane」负责把滚动计数器更新成最新 `pos`，供后续 warp 接力（[L154-L155](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/get_fused_mapping_kernel.py#L154-L155)）。`pos_to_token_topk = i`（即 `token*num_topk + topk` 的扁平下标）方便后续反查。

> 注意：这种 match-any 写法保证的是「同 expert 单元连续」这一关键不变量；同一 expert 段内单元的相对顺序并非严格稳定排序——但 GEMM 不关心段内顺序，只关心连续，所以这里舍弃稳定换来了零原子。

**wrapper `get_fused_mapping`**：当调用方给 `num_expanded_tokens=0` 时，先估算一个上界（每个 expert 最多 padding `alignment-1` 行），跑完 kernel 再用一次 `.tolist()` 把实际段长取回主机、裁掉多余 padding：

```python
num_expanded_tokens = (num_tokens * num_topk + (alignment - 1) * num_experts) // alignment * alignment
...
num_tokens_per_expert_list = num_tokens_per_expert.tolist()
num_expanded_tokens = sum(num_tokens_per_expert_list)
pos_to_expert = pos_to_expert[:num_expanded_tokens]
```

参见 [L201-L240](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/get_fused_mapping_kernel.py#L201-L240)。这次 host sync（`.tolist()`）会引入一次 CPU 同步；想避免可传 `force_no_sync=True` 走上界不裁剪。`num_sms` 来自 `get_num_sms()`（硬件探测，见 [L207](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/get_fused_mapping_kernel.py#L207)）。

#### 4.1.4 代码实践

**目标**：用一个极小例子，人工验证 `get_fused_mapping` 产出的映射满足「同专家连续」。

**步骤**（源码阅读型实践，无需 GPU 也能推理）：

1. 假设 `num_tokens=3`、`num_topk=2`、`num_experts=3`、`alignment=4`，且
   ```
   topk_idx = [[0, 2],   # token0 → expert0, expert2
                [1, 0],   # token1 → expert1, expert0
                [2, 1]]   # token2 → expert2, expert1
   ```
   即扁平化后 6 个单元的 expert 序列为 `[0,2,1,0,2,1]`。
2. 手算每个专家的单元数：expert0 有 2 个、expert1 有 2 个、expert2 有 2 个；各 `align(4)` 后都是 4，所以 `expert_start=[0,4,8]`、`expert_end=[4,8,12]`，`num_expanded_tokens=12`。
3. 推断 `pos_to_expert` 应为：位置 0~3 全是 0（2 个真实 + 2 个 padding 的 -1），位置 4~7 全是 1，位置 8~11 全是 2。
4. 在有 GPU 的环境里运行（待本地验证）：

   ```python
   import torch, tile_kernels
   topk_idx = torch.tensor([[0,2],[1,0],[2,1]], dtype=torch.int64, device='cuda')
   pos_to_expert, pos_to_token, pos_to_token_topk, token_topk_to_pos, \
       expert_start, expert_end, n_per, n_per_list = tile_kernels.moe.get_fused_mapping(
           topk_idx, num_experts=3, num_expanded_tokens=0, alignment=4)
   print(pos_to_expert.tolist())
   print(expert_start.tolist(), expert_end.tolist(), n_per_list)
   ```

**需要观察的现象**：`pos_to_expert` 按「0,0,-1,-1, 1,1,-1,-1, 2,2,-1,-1」这种分段形式出现（padding 位置为 -1）；`expert_start/expert_end` 与手算一致；`token_topk_to_pos` 中每个 `(token,topk)` 指向的位置，其 `pos_to_expert` 恰好等于 `topk_idx[token,topk]`。

**预期结果**：同专家单元连续成段，padding 行为 -1，正反两张表互为逆映射（测试 [tests/moe/test_get_fused_mapping.py:L54-L78](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/moe/test_get_fused_mapping.py#L54-L78) 正是检查这些不变量）。若段内顺序与手算略有出入属正常，因 match-any 不保证段内稳定。

#### 4.1.5 小练习与答案

**练习 1**：为什么每个专家的段长要 `align(alignment)`，而不是直接用真实计数？

**答案**：分组 GEMM 按固定块（如 16/128 行）切分；段长不对齐会让最后一块只含几行，既浪费线程又破坏 TMA/向量化对齐。宁可多 padding 几行 `-1`（被 expand 填 0、GEMM 当哑行），换整齐分块。

**练习 2**：第二遍 scatter 里，如果同一 warp 内有 3 个 lane 的 `expert_idx` 都是 5，它们的 `count` 分别是多少？位置会怎样？

**答案**：从低 lane 到高 lane，`count` 依次为 1、2、3（`popcount(mask & lane_mask)` 含本 lane）。由 `pos = prefix_count - count`，三者的 pos 递减、彼此相邻，于是这 3 个单元连续落在 expert5 段内的 3 个位置。

**练习 3**：wrapper 里 `.tolist()` 那一步为什么可能成为性能隐患？怎样避免？

**答案**：`.tolist()` 强制 GPU→CPU 同步并阻塞后续 kernel 提交。如果调用方能容忍多分配一点 padding，可传 `force_no_sync=True`，跳过同步、直接用估算的上界 `num_expanded_tokens`。

---

### 4.2 expand_to_fused_kernel：前向 scatter

#### 4.2.1 概念说明

有了映射表，`expand_to_fused` 把 token-major 的输入 `x`（形状 `(num_tokens, hidden)`）**复制**进 fused 布局缓冲 `expanded_x`（形状 `(num_expanded_tokens, hidden)`）。

复制规则很直观：对每个 `(token, topk)`，查 `token_topk_to_pos[token, topk]` 得到目标位置 `pos`，把整行 `x[token, :]` 写到 `expanded_x[pos, :]`。这样同一个 token 被复制到它要去的每个专家段里。

两个边界情况：

- `token_topk_to_pos[token, topk] = -1`：该路由被丢弃（如 EP/TP 掩码置 -1，见 u5-l5），跳过不写。
- `pos_to_expert[pos] < 0`：该 `pos` 是 padding 行，要主动写 0（否则缓冲里是未初始化数据，会污染专家 GEMM）。

`expand_to_fused_with_sf` 是它的带量化缩放因子（SF）版本，把 `(x, x_sf)` 一起复制，SF 行的布局还要支持 row-major / col-major / packed ue8m0 三种（SF 的概念见 u4-l1~u4-l3）。

#### 4.2.2 核心流程

```
对 pid_token in [0, num_expanded_tokens)（grid 覆盖整个 fused 缓冲）：
  1. 若 pos_to_expert[pid_token] < 0：把 expanded_x[pid_token] 整行写 0（padding）
  2. 若 pid_token >= num_tokens：return（这些 block 只负责给 padding 清零）
  3. 把 x[pid_token, :] 整行 T.copy 进 fragment
  4. for k in serial(num_topk):
       pos = token_topk_to_pos[pid_token, k]
       if pos >= 0:
           expanded_x[pos, :] = fragment     # T.Parallel 覆盖整行
```

注意第 2 步：grid 维度是 `num_blocks = max(num_tokens, num_expanded_tokens)`，所以会启动比 token 数更多的 block，多出来的 block 专门负责给 padding 行清零，清完即 `thread_return` 退出。

#### 4.2.3 源码精读

**grid 覆盖 padding**：`num_blocks = T.max(num_tokens, num_expanded_tokens)`，使 grid 既能覆盖所有真实 token，也能覆盖所有 padding 行（[L40](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/expand_to_fused_kernel.py#L40)、[L53](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/expand_to_fused_kernel.py#L53)）。

**padding 清零 + 越界早退**：

```python
if pid_token < num_expanded_tokens:
    if pos_to_expert[pid_token] < 0:
        for i in T.Parallel(hidden_aligned):
            expanded_x[pid_token, i] = 0
        ...
if pid_token >= num_tokens:
    T.thread_return()
T.assume(pid_token < num_tokens)
```

参见 [L56-L69](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/expand_to_fused_kernel.py#L56-L69)。`hidden_aligned = align(hidden, num_threads)` 让一行按线程数对齐，`T.Parallel` 把整行分摊给 64 个线程向量化写。

**整行加载 + 散播**：先用 `T.copy` 把一整行读到 fragment（集体加载，见 u2-l2），再在 serial 的 topk 循环里把同一行 fragment 重复写到各个目标位置：

```python
T.copy(token_topk_to_pos[pid_token, 0], pos_local)
T.copy(x[pid_token, :], x_fragment[0:hidden])
...
for k in T.serial(num_topk):
    T.assume(pos_local[k] < num_expanded_tokens)
    if pos_local[k] >= 0:
        for i in T.Parallel(hidden_aligned):
            expanded_x[pos_local[k], i] = x_fragment[i]
```

参见 [L74-L89](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/expand_to_fused_kernel.py#L74-L89)。这是「读一次、写 `num_topk` 次」的典型 scatter 模式；`hidden` 维度用 `T.Parallel` 并行，`num_topk` 维度用 `T.serial`（因为几次写互不依赖，但都要复用同一份 fragment，serial 更易让编译器合并地址计算）。

**带 SF 的变体**：`expand_to_fused_with_sf` 多写一份 `expanded_x_sf`，且 SF 行在 col-major / packed ue8m0 时索引方式不同，kernel 内用 `use_tma_aligned_col_major_sf` 分支处理（[L60-L65](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/expand_to_fused_kernel.py#L60-L65) 与 [L84-L89](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/expand_to_fused_kernel.py#L84-L89)），SF 输出张量声明为 `T.StridedTensor` 以承载 col-major 的跨步（[L49](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/expand_to_fused_kernel.py#L49)）。SF 相关的 `hidden_sf`、`use_packed_ue8m0` 等派生逻辑见 [L28-L34](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/expand_to_fused_kernel.py#L28-L34)。

**wrapper `expand_to_fused`**：标准四步——校验、`get_expand_to_fused_kernel` 触发 JIT、`num_tokens>0` 时启动。注意它把不带 SF 的调用以 `None` 占位喂给带 SF 形参的 prim_func（[L115-L128](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/expand_to_fused_kernel.py#L115-L128)）。

**torch 参考**：逻辑等价的实现是「先全填 0，再按有效 pos scatter」：

```python
out = torch.zeros((num_expanded_tokens, hidden), ...)
pos_flat = token_topk_to_pos.reshape(-1)
mask = pos_flat >= 0
x_repeated = x.unsqueeze(1).expand(-1, num_topk, -1).reshape(-1, hidden)
out[pos_flat[mask]] = x_repeated[mask]
```

参见 [torch/expand_to_fused.py:L30-L37](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/expand_to_fused.py#L30-L37)。先 `zeros` 对应 kernel 里给 padding 行写 0；`expand` 后按 mask gather-write 对应「复制 token 到每个目标 pos」。

#### 4.2.4 代码实践

**目标**：亲手写一个简化版 torch `expand`，与 `tile_kernels.moe.expand_to_fused` 对拍，并解释 fused 布局为何利于连续访存。

**步骤**：

1. 用 `get_fused_mapping` 造一组映射，再分别用「自己写的 torch 版」和「kernel 版」展开：

   ```python
   import torch, tile_kernels
   from tile_kernels.testing.generator import generate_topk_idx

   params = {'num_tokens': 64, 'num_experts': 8, 'num_ep_ranks': 1,
             'num_topk': 4, 'num_groups': 0, 'num_ep_ranks': 1, 'num_send_tokens': 0}
   topk_idx = generate_topk_idx(params)
   num_tokens, hidden = topk_idx.shape[0], 256
   x = torch.randn(num_tokens, hidden, dtype=torch.bfloat16, device='cuda')

   pos_to_expert, _, _, token_topk_to_pos, *_ = tile_kernels.moe.get_fused_mapping(
       topk_idx, num_experts=8, num_expanded_tokens=0, alignment=16)

   # 你的简化版 torch expand（按 token_topk_to_pos 复制）
   def my_expand(x, tt2p, p2e):
       out = torch.zeros((p2e.shape[0], x.shape[1]), dtype=x.dtype, device=x.device)
       for t in range(x.shape[0]):
           for k in range(tt2p.shape[1]):
               pos = tt2p[t, k].item()
               if pos >= 0:
                   out[pos] = x[t]
       return out

   ref = my_expand(x, token_topk_to_pos, pos_to_expert)
   got = tile_kernels.moe.expand_to_fused(x, token_topk_to_pos, pos_to_expert)
   print(torch.equal(ref, got))
   ```

2. 对照 `pos_to_expert`，确认 `got` 的行顺序是按专家分段的（专家 0 的行在最前）。

**需要观察的现象**：`torch.equal` 返回 `True`；`pos_to_expert` 显示连续分段。

**解释 fused 布局为何利于连续访存**：专家 e 做 GEMM 时只需读 `expanded_x[expert_start[e]:expert_end[e]]`，这是显存里一段**物理连续**的行；一个 warp 里相邻线程读相邻行 → 合并访存，带宽接近峰值。若用朴素布局，专家 e 的行散布在 `num_tokens*num_topk` 范围内，warp 里相邻线程读到的地址相差 `num_topk` 行，完全不合并，带宽利用率崩塌。

**预期结果**：与 torch 参考位精确一致（expand 无浮点运算，可位精确，对拍方式同 [tests/moe/test_expand_to_fused.py:L37-L44](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/moe/test_expand_to_fused.py#L37-L44)）。

#### 4.2.5 小练习与答案

**练习 1**：grid 维度为什么是 `max(num_tokens, num_expanded_tokens)` 而不是 `num_tokens`？

**答案**：padding 行（`pos_to_expert<0`）也必须被清零，它们的下标可能 ≥ `num_tokens`。grid 必须覆盖到所有 padding 行，所以取 `max`；超出 `num_tokens` 的 block 清完零用 `thread_return()` 退出。

**练习 2**：为什么 `hidden` 维度用 `T.Parallel`，`num_topk` 维度却用 `T.serial`？

**答案**：`hidden` 维度的各元素互相独立，用 `Parallel` 把整行分给 64 线程向量化写，吃满带宽；`num_topk` 维度的几次写虽互不依赖，但都复用同一份已加载的 fragment，serial 让编译器更易合并地址计算，且 `num_topk` 通常只有个位数，无需并行。

**练习 3**：带 SF 版本里，为什么 SF 输出要声明成 `T.StridedTensor`？

**答案**：col-major 布局下 SF 的行步长不等于 1（运行时才确定，用 `sf_stride` 符号表达），`T.StridedTensor` 能携带这个跨步信息，使 kernel 主体在不感知物理布局的情况下正确寻址（见 u2-l1 对 `T.StridedTensor` 的说明）。

---

### 4.3 reduce_fused_kernel：反向 gather / 加权归约

#### 4.3.1 概念说明

专家 GEMM 算完，得到 fused 布局的输出 `expanded_y`（`(num_expanded_tokens, hidden)`）。现在要把它**还原**回 token-major：每个 token 的最终输出 = 它被派发到的 `num_topk` 个专家的输出，按路由权重加权求和。

`reduce_fused` 正是 `expand_to_fused` 的逆方向：对每个 token，查 `token_topk_to_pos[token, :]` 找到它的 `num_topk` 个位置，把 `expanded_y[pos, :]` 取出来、乘以权重 `topk_weights[token, k]`，累加到 `out[token, :]`。

\[ \mathrm{out}[t, :] = \sum_{k=0}^{\text{num\_topk}-1} w_{t,k} \cdot Y[\text{pos}(t,k), :] \]

其中 \(\text{pos}(t,k) = \text{token\_topk\_to\_pos}[t,k]\)，权重 \(w_{t,k}\) 由 `topk_weights` 提供（取不带 bias 的 scores，见 u5-l2）。`-1` 位置（被丢弃的路由）跳过。

#### 4.3.2 核心流程

```
对 pid_token in [0, num_tokens)（grid 按 token）：
  1. reduced_fragment 清 0
  2. 把 token_topk_to_pos[pid_token, :]、topk_weights[pid_token, :] 读进 fragment
  3. for k in unroll(num_topk):     # 几次累加，展开
       pos = topk_to_pos_local[k]
       if pos >= 0:
         s = topk_weights_local[k]（可选） * x_sf[pos]（可选）
         for i in Parallel(hidden): reduced_fragment[i] += x[pos,i] * s
  4. for i in Parallel(hidden): out[pid_token,i] = reduced_fragment[i]（可选乘 sf）
```

和 expand 相反：expand 是「读一行、写多处」，reduce 是「读多处、加权累加到一行」。

#### 4.3.3 源码精读

**累加器与输入装载**：

```python
reduced_fragment = T.alloc_fragment((hidden,), T.float32)
...
T.clear(reduced_fragment)
if with_sf:
    sf_var = sf[0]
if with_weights:
    T.copy(topk_weights[pid_token, :], topk_weights_local)
T.copy(token_topk_to_pos[pid_token, :], topk_to_pos_local)
```

参见 [L38-L48](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/reduce_fused_kernel.py#L38-L48)。累加在 `float32` fragment 里做，避免低精度累加误差；`with_sf` / `with_weights` / `with_x_sf` 三个编译期布尔分别特化「是否乘输出 SF」「是否乘路由权重」「是否乘每 token 的 SF」（见 [L19-L22](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/reduce_fused_kernel.py#L19-L22)）。

**加权累加主体**：

```python
for k in T.unroll(num_topk):
    pos = topk_to_pos_local[k]
    T.assume(pos < num_expanded_tokens)
    if pos >= 0:
        s = T.alloc_var(T.float32)
        s = 1
        if with_weights:
            s = topk_weights_local[k]
        if with_x_sf:
            s *= x_sf[pos]
        for i in T.Parallel(hidden):
            reduced_fragment[i] += x[pos, i] * s
```

参见 [L50-L62](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/reduce_fused_kernel.py#L50-L62)。`num_topk` 用 `unroll`（次数小、固定），`hidden` 用 `Parallel`（吃带宽）。三个乘法因子用 `s` 合并成一次乘法（factor 合并思想，见 u4-l2），减少浮点指令。

**写出与可选 SF**：

```python
for i in T.Parallel(hidden):
    out[pid_token, i] = T.Select(with_sf, reduced_fragment[i] * sf_var, reduced_fragment[i])
```

参见 [L64-L65](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/reduce_fused_kernel.py#L64-L65)。当输出要量化成 FP8 时（`fp8_format='e4m3'`），`with_sf=True`，在写出时乘一个标量 SF。

**wrapper `reduce_fused`**：根据输入是否为 `QuantTensor`（带 `x_sf`）、是否有 `topk_weights`、是否有 `sf`，把三个布尔传给 JIT 构造器特化出不同产物（[L121-L129](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/reduce_fused_kernel.py#L121-L129)）。注意 `assert hidden % 256 == 0`（[L99](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/reduce_fused_kernel.py#L99)），要求 hidden 是 256 的倍数，方便分块。

**torch 参考**：逐 k 累加，用 `torch.where` 屏蔽 `-1` 槽，并用 `@torch.compile` 的 `elementwise_fma` 强制融合成 FMA（乘加）指令以匹配 kernel 的精度：

```python
for k in range(num_topk):
    safe_pos = pos_k.clamp(min=0)
    rows = x[safe_pos].float()
    s = ...  # ones / topk_weights / * x_sf
    result = elementwise_fma(rows, s.unsqueeze(1), reduced)
    reduced = torch.where(mask_k.unsqueeze(1), result, reduced)
```

参见 [torch/reduce_fused.py:L48-L74](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/reduce_fused.py#L48-L74)。FMA 融合的注释（[L7-L8](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/reduce_fused.py#L7-L8)）点明这是为了和 kernel 做位精确对拍——否则浮点累加顺序差异会让结果有 1 ULP 出入。

#### 4.3.4 代码实践

**目标**：构造 expand → reduce 往返，验证 `reduce_fused` 把 expand 的结果正确还原（恒等权重下应近似还原原 token）。

**步骤**：

```python
import torch, tile_kernels
from tile_kernels.testing.generator import generate_topk_idx

params = {'num_tokens': 32, 'num_experts': 4, 'num_ep_ranks': 1,
          'num_topk': 2, 'num_groups': 0, 'num_send_tokens': 0}
topk_idx = generate_topk_idx(params)
num_tokens, hidden = topk_idx.shape[0], 256
x = torch.randn(num_tokens, hidden, dtype=torch.bfloat16, device='cuda')

pos_to_expert, _, _, token_topk_to_pos, *_ = tile_kernels.moe.get_fused_mapping(
    topk_idx, num_experts=4, num_expanded_tokens=0, alignment=16)

# 1. expand：token → fused
expanded = tile_kernels.moe.expand_to_fused(x, token_topk_to_pos, pos_to_expert)

# 2. 假装「专家」是恒等映射：expanded_y = expanded
# 3. reduce：fused → token，权重全 1 / num_topk（归一化）
w = torch.full((num_tokens, 2), 0.5, dtype=torch.float32, device='cuda')
out = tile_kernels.moe.reduce_fused(expanded, w, token_topk_to_pos)

# 每个被保留路由的 token，out[t] ≈ x[t]*0.5*2 = x[t]（被丢弃路由的 token 会偏小）
print((out - x).abs().mean())
```

**需要观察的现象**：对没有被 `-1` 路由的 token，`out[t]` 近似等于 `x[t]`（权重 0.5×2=1，专家恒等）；有路由被丢弃的 token 则 `out[t]` 偏小（少加了一份）。

**预期结果**：与 torch 参考 `tile_kernels.torch.reduce_fused(expanded, w, token_topk_to_pos)` 位精确一致（见 [tests/moe/test_reduce_fused.py:L63-L78](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/moe/test_reduce_fused.py#L63-L78) 的对拍断言）。

#### 4.3.5 小练习与答案

**练习 1**：为什么累加用 `float32` fragment，而不是直接在 `bfloat16` 上累加？

**答案**：MoE 一个 token 要累加 `num_topk`（如 4~8）个专家输出，低精度累加会引入不可忽略的舍入误差，且与 torch 参考无法位精确对拍。在 `float32` 累加、最后再转回目标 dtype，是兼顾精度与带宽的常规做法。

**练习 2**：`with_weights`、`with_sf`、`with_x_sf` 为什么设计成编译期布尔，而不是运行时分支？

**答案**：它们在大多数调用里取值固定（如有无路由权重、是否 FP8 输出）。做成编译期布尔，JIT 会为每种组合特化一份产物，省掉 kernel 内的运行时分支与无用的乘法/装载指令（见 u2-l1「编译期 vs 运行时」）。

**练习 3**：torch 参考里为何用 `@torch.compile` 的 `elementwise_fma` 而非直接 `rows*s + reduced`？

**答案**：`a*b+c` 在 PyTorch 默认可能拆成两次独立运算（先乘后加），而 kernel 走的是硬件 FMA（一次乘加、单次舍入）。用 `torch.compile` 强制融合成 FMA，使参考与 kernel 的浮点舍入一致，从而能做严格的位精确对拍。

---

### 4.4 inplace_unique_group_indices_kernel：组级去重工具

#### 4.4.1 概念说明

这个 kernel 不在 expand/reduce 主链上，而是路由阶段的一个小工具，和 u5-l3 的「分组路由」配套。

DeepSeek-MoE 的 group-limited routing 会先按「组」粗筛：每个 token 选若干组，再在组内选专家。问题是——一个 token 可能同时在两个组里都选到了**同一个专家**（因为同组的多个专家会被分别考虑）。这会造成重复派发。

`inplace_unique_group_indices` 解决的就是这种重复：给定每行的 group 下标（形状 `(num_tokens, num_topk)`），把**重复出现的 group** 就地改写成 `-1`，每行只保留每个 group 的第一次出现。下标 `< 0` 视为已失效，不参与去重。

约束：`num_groups ≤ 128`（用两个 64 位字位图覆盖）。

#### 4.4.2 核心流程

```
对每个 token（多 block 步进扫描）：
  group_sel = [0, 0]   # 两个 uint64，共 128 位，每位代表一个 group 是否已见过
  for j in unroll(num_topk):
    g = group_indices[token, j]
    bit = (g>=0) ? (1 << (g%64)) : 0
    lo/hi = 根据 g<64 或 g>=64 选 group_sel[0] 或 group_sel[1]
    if (对应位已置 1):   # 之前见过 → 重复
        group_indices[token, j] = -1
    else:
        group_sel[对应字] |= bit   # 标记为已见过
```

用一个 128 位的「见过的 group」位图，O(num_topk) 时间完成一行去重，无需排序、无需原子（每行由单线程处理，天然无竞争）。

#### 4.4.3 源码精读

**kernel 主体**：每个线程处理若干 token（grid_x*num_threads 步进），用 `group_sel[2]` 两个 `uint64` 当 128 位位图：

```python
for i in T.serial(global_thread_idx, num_tokens, grid_x * num_threads):
    for j in T.unroll(num_groups_aligned // 64):
        group_sel[j] = 0
    for j in T.unroll(num_topk):
        group_idx = group_indices[i, j]
        mask = T.Select(group_idx >= 0, T.uint64(1) << (group_idx % 64), T.uint64(0))
        lo_mask = T.Select(group_idx < 64, mask, T.uint64(0))
        hi_mask = T.Select(group_idx >= 64, mask, T.uint64(0))
        found = (lo_mask & group_sel[0]) | (hi_mask & group_sel[1])
        group_sel[0] |= lo_mask
        group_sel[1] |= hi_mask
        if found:
            group_indices[i, j] = -1
```

参见 [inplace_unique_group_indices_kernel.py:L32-L46](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/inplace_unique_group_indices_kernel.py#L32-L46)。要点：

- `group_idx % 64` 算出在 64 位字内的位号，`group_idx < 64` 决定落 `group_sel[0]` 还是 `group_sel[1]`（[L39-L41](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/inplace_unique_group_indices_kernel.py#L39-L41)）。
- `found` 用「位与」检测该 group 是否已见过；无论是否重复，都把位 OR 进位图（[L42-L44](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/inplace_unique_group_indices_kernel.py#L42-L44)）。
- `group_idx < 0` 时 `mask=0`，既不算重复也不污染位图（[L39](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/inplace_unique_group_indices_kernel.py#L39)）。

**JIT 开关**：关掉 warp specialized、打开 `TL_ENABLE_LOWER_LDGSTG_PREDICATED`（让 `cp.async` 带谓词，减少分支开销，[L10-L15](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/inplace_unique_group_indices_kernel.py#L10-L15)）。

**wrapper**：`num_groups` 向上对齐到 64 的倍数（决定清零几个字），`assert num_groups <= 128`，就地修改输入（[L51-L69](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/inplace_unique_group_indices_kernel.py#L51-L69)）。

**torch 参考**：用 `stable=True` 排序后找相邻相等来定位重复，再 scatter 回原位置改 `-1`：

```python
vals, idx = torch.sort(group_indices, dim=1, stable=True)
first_in_sorted[:, 1:] = vals[:, 1:] != vals[:, :-1]
dup_in_sorted = ~first_in_sorted
dup_in_orig.scatter_(1, idx, dup_in_sorted)
group_indices[dup_in_orig] = -1
```

参见 [torch/moe.py:L96-L120](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/moe.py#L96-L120)。两种实现语义一致：每行保留每个 group 的最左出现，其余改 `-1`。

#### 4.4.4 代码实践

**目标**：手算一行去重结果，再用 torch 参考与 kernel 对拍。

**步骤**：

```python
import torch, tile_kernels
# 一行: group = [3, 1, 3, 2, 1] → 保留 [3,1,-1,2,-1]
gi = torch.tensor([[3,1,3,2,1]], dtype=torch.int64, device='cuda')
gi_ref = gi.clone()
tile_kernels.torch.inplace_unique_group_indices(gi_ref, num_groups=8)

gi_k = gi.clone()
tile_kernels.moe.inplace_unique_group_indices(gi_k, num_groups=8)
print(gi_ref.tolist(), gi_k.tolist(), torch.equal(gi_ref, gi_k))
```

**需要观察的现象**：两者都变成 `[[3, 1, -1, 2, -1]]`，仅保留每个 group 的首次出现。

**预期结果**：`torch.equal` 为 `True`，与 [tests/moe/test_inplace_unique_group_indices.py:L38-L52](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/moe/test_inplace_unique_group_indices.py#L38-L52) 的断言一致。

#### 4.4.5 小练习与答案

**练习 1**：为什么用两个 `uint64` 而不是一个更大的数组做位图？

**答案**：`num_groups ≤ 128`，两个 64 位字正好覆盖 128 位；把它们放在线程私有寄存器（`alloc_local`）里，访问零延迟、无需同步。若 group 数超过 128 则需扩成更多字（当前实现的硬上限）。

**练习 2**：去重为何只需「保留首次出现」，而不是全部丢弃重复项？

**答案**：group 只是路由粗筛的中间量，每个被选中的 group 仍要参与后续选专家；重复的 group 会让同一组被计算两次、扭曲权重，所以只去掉多余的副本、保留一个有效记录。

**练习 3**：为什么每行用一个线程处理就够了，不需要并行？

**答案**：每行只有 `num_topk`（个位数）个元素，单线程顺序扫一遍远快于启动并行的开销；且单线程处理使位图的读改写天然无竞争，避免了原子或同步。

---

## 5. 综合实践

把本讲四个模块串起来，跑一遍完整的「派发往返」，并用 torch 参考交叉验证。

**任务**：实现一个最小的 MoE 派发模拟。

1. 用 `generate_topk_idx` 造 `topk_idx`，用 `get_fused_mapping` 得到 `pos_to_expert` 与 `token_topk_to_pos`。
2. 造输入 `x (num_tokens, hidden)`，用 `expand_to_fused` 得到 fused 布局的 `expanded_x`。
3. **自检 1**：逐专家检查 `expanded_x[expert_start[e]:expert_end[e]]` 里非 padding 行的内容，是否恰好等于被路由到专家 e 的那些 token 的 `x`（用 `pos_to_token` 反查）。
4. **模拟专家**：让每个专家做一次简单变换（如乘以 `(e+1)` 的标量），得到 `expanded_y`。
5. 用 `reduce_fused` 配上随机 `topk_weights`，把 `expanded_y` 还原成 `out (num_tokens, hidden)`。
6. **自检 2**：用 `tile_kernels.torch.reduce_fused` 做同样的还原，断言 `torch.equal(out, out_ref)` 或浮点近似（注意 FMA 精度对齐）。
7. **解释**：在第 3 步里，说明为什么「按 `expert_start/expert_end` 切片就能拿到专家 e 的全部输入」正是 fused 布局带来的连续访存收益；如果把 `expanded_x` 换成朴素 `(num_tokens, num_topk, hidden)` 布局，专家 e 要怎样零散地 gather，带宽会如何劣化。

**验收标准**：第 3 步切片内容与反查一致；第 6 步与 torch 参考位精确（或近位精确）一致；第 7 步能说清「连续 vs 散乱」对 GEMM 访存的影响。

> 提示：完整测试范式（参数生成、`assert_equal`、`count_bytes` 算带宽）见 [tests/moe/test_expand_to_fused.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/moe/test_expand_to_fused.py) 与 [tests/moe/test_reduce_fused.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/moe/test_reduce_fused.py)，可照搬其 fixture 写法（见 u3-l2、u9-l1）。

## 6. 本讲小结

- **fused 布局**把 `(token, topk)` 单元按专家排序成连续段，让每个专家的 GEMM 能合并访存，这是 MoE 高效派发的核心。
- **`get_fused_mapping`** 用「第一遍局部直方图 + 前缀和算分段、第二遍 `__match_any_sync` 无锁 scatter」产出全套双向映射，几乎不依赖原子。
- **`expand_to_fused`** 是「读一行、写多处」的 scatter：把 token 激活复制进 fused 缓冲，并给 padding 行清零。
- **`reduce_fused`** 是 `expand` 的逆：把专家输出按 `token_topk_to_pos` 加权累加还原成 token 输出，累加在 float32 fragment、用编译期布尔特化。
- **`inplace_unique_group_indices`** 用 128 位寄存器位图，O(num_topk) 就地给每行的 group 下标去重，配合 group-limited 路由防重复派发。
- 全部算子都有等价的 torch 参考做对拍，正确性测试遵循「参数生成 → 数据生成 → kernel 与参考 `assert_equal`」三件套（见 u3-l2、u9-l1）。

## 7. 下一步学习建议

- **u5-l5**（权重归一化、TP 掩码、aux 负载）会把这些派发算子放回完整路由流水线，看 `top2_sum_gate` 如何串起打分→分组→掩码→派出 `topk_idx`，正是本讲 `get_fused_mapping` 的输入来源。
- **u9-l1**（测试数据生成与数值对拍）深入讲解 `generate_moe_params` / `assert_equal` / `calc_diff`，理解本讲测试里那些 fixture 的底层原理。
- **u10-l1**（硬件感知调优）展开 `get_num_sms`、`set_num_sms` 与持久化 kernel 占用估算——本讲 `get_fused_mapping` 的 `num_sms` grid 正是硬件感知调度的实例。
- 想做端到端练习，可参考 **u10-l3**（新增算子四件套综合实战），把本讲的 expand/reduce 当作「wrapper + torch 参考 + 测试」范式的模板。
