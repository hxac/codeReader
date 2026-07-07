# BlockInfo：分块与有效范围计算

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `tile_m` / `tile_n` 这两个分块维度在 FA4 kernel 里代表什么，以及 `BlockInfo` 这个数据类保存了哪些字段。
- 在给定 Q tile 下，手算出这个 tile 在因果（causal）、滑窗（local）掩码下「真正需要遍历的 K/V block 范围」`[n_block_min, n_block_max)`，并理解它是如何被推导出来的。
- 理解 SplitKV 场景下，这段 `[n_block_min, n_block_max)` 范围是如何被进一步切成 `num_splits` 段、分给不同 thread block 并行处理的。
- 读懂 `flash_attn/cute/block_info.py` 与 `flash_attn/cute/seqlen_info.py` 的真实代码，并能把它们和 kernel 主循环对应起来。

本讲**不**进入 softmax 数值核心（那是 u4-l1），也**不**讨论元素级掩码 `apply_mask` 的细节（那是 u3-l1）。本讲只关心一个问题：**一个 Q tile 到底要从第几块 K/V 算到第几块？**

## 2. 前置知识

- **分块（tiling）**：FA 把序列切成固定大小的「瓦片」（tile），Q 切成 `tile_m` 一行块、K/V 切成 `tile_n` 一列块（见 u1-l1）。
- **因果掩码（causal mask）**：在第 \(i\) 个 query 位置，只允许关注第 \(j \le i\) 个 key，把 \(j>i\) 的注意力分数置为 \(-\infty\)。FA4 采用 **end-aligned** 约定：当 `seqlen_q != seqlen_k` 时，让最后一条 Q 对齐最后一条 K，即允许条件为 \(j \le i + (\text{seqlen\_k} - \text{seqlen\_q})\)（见 u3-l1）。
- **滑窗掩码（local / sliding window）**：query 只关注一个窗口内的 key，常用 `window_size_left`（往左看多远）和 `window_size_right`（往右看多远）描述。
- **块级跳过 vs 元素级掩码**：这是理解本讲的关键。`BlockInfo` 负责**块级跳过**——如果整个 K tile 都被掩掉，就根本不进主循环；而 tile **内部**那些被掩掉的单个元素（边界块里部分非法位置），则交给 u3-l1 讲过的 `apply_mask` 在元素级别处理。两者协同才能既省算力又保证精确。
- **`cutlass.Constexpr` 与 `const_expr`**：编译期常量与编译期分支。`BlockInfo` 的 `is_causal`、`tile_n` 等都是 `Constexpr`，`const_expr(...)` 包起来的 `if` 在编译期就被求值并裁剪掉，于是「带因果的 kernel」和「不带因果的 kernel」会被特化成两份不同的 PTX（详见 u2-l2、u11-l2）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [flash_attn/cute/block_info.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/block_info.py) | 定义 `BlockInfo` 数据类，提供 `get_n_block_min_max` / `get_m_block_min_max` 等方法，是本讲的主角。 |
| [flash_attn/cute/seqlen_info.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/seqlen_info.py) | 定义 `SeqlenInfoQK`，集中保存 `seqlen_q`、`seqlen_k` 等序列长度信息，并**预计算** `num_n_blocks`，供 `BlockInfo` 消费。 |
| [flash_attn/cute/flash_fwd.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py) | Ampere 前向 kernel，在主循环前构造 `BlockInfo` 并调用 `get_n_block_min_max` 决定 K/V 遍历范围（`is_split_kv=False` 的典型用法）。 |
| [flash_attn/cute/flash_fwd_sm100.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm100.py) | Blackwell 前向 kernel，在 SplitKV 场景下把 `split_idx, num_splits` 传进 `get_n_block_min_max`。 |

---

## 4. 核心概念与源码讲解

### 4.1 tile 维度与 BlockInfo 字段

#### 4.1.1 概念说明

FA4 的前向 kernel 把 Q 矩阵按行切成若干个 `tile_m` 行的块，把 K/V 按行切成若干个 `tile_n` 行的块。整个序列的 Q 块总数是 `ceil(seqlen_q / tile_m)`，K/V 块总数是 `ceil(seqlen_k / tile_n)`。

主循环的外层枚举 **Q 块**（用 `m_block` 编号），内层枚举该 Q 块需要访问的 **K/V 块**（用 `n_block` 编号）。如果不做任何掩码，那么每个 Q 块都要遍历**所有** K/V 块；但有了因果/滑窗掩码，很多 K/V 块整块都是 \(-\infty\)，遍历它们纯属浪费。

`BlockInfo` 就是为了回答这个枚举范围问题而存在的小工具：它**只读**地保存分块维度和掩码开关，然后提供几个 `@cute.jit` 方法，给定一个 `m_block`（或 `n_block`），算出对应的合法 `n_block`（或 `m_block`）范围。

#### 4.1.2 核心流程

```
kernel 主循环开始
   ├─ 从 tile scheduler 拿到本 thread block 负责的 m_block
   ├─ 构造 SeqlenInfoQK（一次性读出 seqlen_q / seqlen_k / num_n_blocks）
   ├─ 构造 BlockInfo（保存 tile_m, tile_n, is_causal, is_local, window_size...）
   ├─ n_block_min, n_block_max = block_info.get_n_block_min_max(seqlen, m_block)
   └─ for n_block in range(n_block_max-1, n_block_min-1, -1):   # 从右往左遍历
          加载第 n_block 个 K/V tile → MMA → 在线 softmax 累加
```

注意主循环是**从 `n_block_max-1` 往 `n_block_min` 倒着遍历**（见 [flash_fwd.py:809](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L809) 的 `n_block = cutlass.max(n_block_max - 1, 0)`）。所以 `get_n_block_min_max` 返回的是一个**半开区间** `[n_block_min, n_block_max)`：包含 `n_block_min`，不包含 `n_block_max`。

#### 4.1.3 源码精读

`BlockInfo` 本身是一个冻结的数据类，字段几乎都是 `cutlass.Constexpr`（编译期常量），意味着它们进编译缓存键，改了就会触发重编译：

[flash_attn/cute/block_info.py:L12-L21](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/block_info.py#L12-L21) —— `BlockInfo` 的字段定义：

- `tile_m` / `tile_n`：Q 块的行数与 K/V 块的行数。
- `is_causal` / `is_local`：两个掩码开关（`Constexpr[bool]`）。
- `is_split_kv`：是否处于 SplitKV 模式（下面 4.3 详述）。
- `window_size_left` / `window_size_right`：滑窗左右边界（运行期 `Int32`，**不**是 Constexpr，所以改窗口大小不重编译，见 u2-l1）。
- `qhead_per_kvhead_packgqa`：pack_gqa 模式下每个 KV 头对应的 Q 头数（详见 u7-l1）；大于 1 时需要把 m_idx 折算回 KV 头视角的索引。

那么 `seqlen_q`、`seqlen_k` 这些**运行期**信息从哪来？来自 `SeqlenInfoQK`。它的模块注释点明了设计意图——把所有序列长度相关的 gmem 读取**在每个 tile 开头一次性做完**，避免在算 `n_block_min`、`n_block_max` 时反复读全局内存：

[flash_attn/cute/seqlen_info.py:L10-L14](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/seqlen_info.py#L10-L14) —— 模块注释，解释为何要把序列长度信息集中到一个对象里。

特别地，`SeqlenInfoQK.create` 还顺手**预计算**了 `num_n_blocks`（即 `ceil(seqlen_k / tile_n)`）和 `block_idx_offset`，这样 `BlockInfo` 里就不用每次重算：

[flash_attn/cute/seqlen_info.py:L124-L130](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/seqlen_info.py#L124-L130) —— 预计算 `num_n_blocks = (seqlen_k + tile_n - 1) // tile_n`。

最后看 kernel 里如何把它们拼起来。Ampere 前向 kernel 在进入主循环前这样构造 `BlockInfo` 与 `SeqlenInfoQK`，并立刻求出 n_block 范围：

[flash_attn/cute/flash_fwd.py:L785-L809](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L785-L809) —— 构造 `BlockInfo`（注意这里第 790 行 `is_split_kv` 写死为 `False`，因为 Sm80 不支持 SplitKV），构造 `seqlen`，然后 `n_block_min, n_block_max = block_info.get_n_block_min_max(seqlen, m_block)`。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：在真实 kernel 里确认「`BlockInfo` 的字段是从哪些 kernel 属性来的」。
2. **操作步骤**：打开 [flash_fwd.py:L785-L794](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L785-L794)，对照 [block_info.py:L12-L21](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/block_info.py#L12-L21) 的字段顺序，逐个写出 `tile_m, tile_n, is_causal, is_local, is_split_kv, window_size_left, window_size_right` 这 7 个实参分别来自 `self.xxx` 还是局部变量 `window_size_xxx`。
3. **需要观察的现象**：你会发现 `is_split_kv` 这一栏在 Sm80 路径里恒为 `False`（字面量），而 `window_size_left/right` 来自函数局部变量（由 u3-l1 讲过的 `_resolve_causal_local_window` 归一化得到）。
4. **预期结果**：列出一张 7 行的字段来源对照表。
5. 本步骤为纯阅读，不运行，无「待本地验证」项。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `is_causal` 是 `Constexpr[bool]`，而 `window_size_left` 只是普通 `Optional[Int32]`？

**参考答案**：`is_causal` 决定 kernel 是否包含「因果上界裁剪」这整段逻辑，应在编译期裁剪分支、特化出更精简的 PTX，所以用 `Constexpr` 进编译键；而窗口的具体大小只是在已编译好的「带 local」kernel 内部改一个运行期数值，不需要重编译，所以用普通 `Int32`。这与 u2-l1 讲过的「`causal` 改值会重编译、窗口具体大小不重编译」一致。

---

### 4.2 n_block_min_max 因果/滑窗裁剪

#### 4.2.1 概念说明

`get_n_block_min_max` 是 `BlockInfo` 最核心的方法：给定一个 `m_block`，求出该 Q 块需要遍历的 K/V 块半开区间 `[n_block_min, n_block_max)`。

它的推导基于一个核心量——**「对角线」K 索引 `n_idx`**。在 end-aligned 约定下，Q 索引 `m_idx` 对应的「对角」K 索引是：

\[
\text{n\_idx} = \text{m\_idx} + (\text{seqlen\_k} - \text{seqlen\_q})
\]

当 `seqlen_q == seqlen_k` 时它就退化成 `n_idx = m_idx`。各种掩码都围绕这个对角索引表达：

- **因果上界**：Q 在 `m_idx` 处只能看 \(j \le \text{n\_idx}\) 的 key，所以 `n_idx_right = n_idx`。
- **滑窗右界**：再允许往右多看 `window_size_right`，`n_idx_right = n_idx + window_size_right`。
- **滑窗左界**：Q 只允许看 \(j \ge \text{n\_idx} - \text{window\_size\_left}\) 的 key，所以 `n_idx_left = n_idx - window_size_left`。

把这些「最大允许 K 索引」「最小允许 K 索引」换算成块号，就得到块级范围。注意换算时的方向不同：

- 上界 `n_idx_right` 是**允许的最大 K 索引**，包含它的那一整块都要算 → 用 `ceil_div`（向上取整）。
- 下界 `n_idx_left` 是**允许的最小 K 索引**，包含它的那一块也要算（块内更小的非法位置交给 `apply_mask` 元素级处理）→ 用向下整除 `//`。

这正是「BlockInfo 做块级跳过、apply_mask 做元素级掩码」协同的体现：块级范围是**保守的包络**，宁可多算一两个边界块，也不能漏算。

#### 4.2.2 核心流程

```
输入: m_block, seqlen_q, seqlen_k, tile_m, tile_n, 掩码开关与窗口

# 1) 上界 n_block_max（因果 / 滑窗右界）
n_block_max = ceil_div(seqlen_k, tile_n)          # 默认全部 K 块
if is_causal or (is_local and window_size_right is not None):
    m_idx_max = (m_block + 1) * tile_m             # 本 Q 块的最后一行（排他上界）
    n_idx      = m_idx_max + seqlen_k - seqlen_q   # 对角 K 索引
    n_idx_right= n_idx            if is_causal
                 else n_idx + window_size_right
    n_block_max = min(n_block_max, ceil_div(n_idx_right, tile_n))

# 2) 下界 n_block_min（滑窗左界；纯因果时为 0）
n_block_min = 0
if is_local and window_size_left is not None:
    m_idx_min = m_block * tile_m                   # 本 Q 块的第一行
    n_idx      = m_idx_min + seqlen_k - seqlen_q
    n_idx_left = n_idx - window_size_left
    n_block_min = max(n_idx_left // tile_n, 0)

# 3) SplitKV 再切分（见 4.3）
return n_block_min, n_block_max
```

注意一个细节：上界用 `m_idx_max = (m_block + 1) * tile_m`（本块的**末**行对角），下界用 `m_idx_min = m_block * tile_m`（本块的**首**行对角）。这是因为上界关心「本 Q 块里能看到最远 key 的那一行」，下界关心「本 Q 块里限制最严（窗口左边界最高）的那一行」。两者都用本块内的极端行，才能保证块级包络不漏。

#### 4.2.3 源码精读

[flash_attn/cute/block_info.py:L23-L55](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/block_info.py#L23-L55) —— `get_n_block_min_max` 完整方法。

其中计算**上界**的关键几行：

[flash_attn/cute/block_info.py:L31-L38](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/block_info.py#L31-L38) —— 先 `n_block_max = ceil_div(seqlen_k, tile_n)`，再在因果/滑窗右界分支里用 `ceil_div(n_idx_right, tile_n)` 把它收紧。`qhead_per_kvhead_packgqa > 1` 时把 `m_idx_max` 除以该系数，把 Q 头视角的行号折算回 KV 头视角（u7-l1 pack_gqa 的副作用，本讲可先忽略）。

计算**下界**的关键几行：

[flash_attn/cute/block_info.py:L40-L46](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/block_info.py#L40-L46) —— 纯因果时 `n_block_min` 保持 0；只有 local 掩码时才用 `n_idx_left // tile_n`（向下整除）抬升下界，并用 `cutlass.max(..., 0)` 防止负数。

> **对称视角（反向用）**：`get_m_block_min_max` 是它的「转置」——给定一个 `n_block`（K 块），反求哪些 `m_block`（Q 块）会用到它，用于反向 kernel 决定遍历范围：
> [flash_attn/cute/block_info.py:L57-L71](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/block_info.py#L57-L71)。逻辑完全镜像：把 `seqlen_q/seqlen_k` 互换、`m/n` 互换、`ceil_div` 与 `//` 的位置对应翻转即可，本讲不展开。

#### 4.2.4 代码实践（本讲主实践）

1. **实践目标**：手算 + 脚本核对 `get_n_block_min_max` 在 `tile_m=tile_n=128, seqlen_q=seqlen_k=1024, causal=True` 时各 `m_block` 的返回值。
2. **手算 `m_block=3`**：
   - `n_block_max = ceil(1024/128) = 8`。
   - `m_idx_max = (3+1)*128 = 512`；`n_idx = 512 + 1024 - 1024 = 512`；因果下 `n_idx_right = 512`。
   - `n_block_max = min(8, ceil(512/128)) = min(8, 4) = 4`。
   - 纯因果 → `n_block_min = 0`。
   - 结果：`(n_block_min, n_block_max) = (0, 4)`。即第 3 个 Q 块（Q 行 384..511）只遍历 K 块 0、1、2、3。
3. **操作步骤**：把下面的示例脚本（这是作者按源码逻辑写的纯 Python 复刻，**非项目原有代码**）保存运行，逐个 `m_block` 打印，并和你手算的对照。

```python
# 示例代码：get_n_block_min_max 的纯 Python 复刻（仅用于理解，非项目代码）
def ceil_div(a, b):
    return (a + b - 1) // b

def get_n_block_min_max(seqlen_q, seqlen_k, tile_m, tile_n, m_block,
                        is_causal, is_local, window_size_left, window_size_right,
                        qhead_per_kvhead_packgqa=1,
                        is_split_kv=False, split_idx=0, num_splits=1):
    n_block_max = ceil_div(seqlen_k, tile_n)
    if is_causal or (is_local and window_size_right is not None):
        m_idx_max = (m_block + 1) * tile_m
        if qhead_per_kvhead_packgqa > 1:
            m_idx_max = ceil_div(m_idx_max, qhead_per_kvhead_packgqa)
        n_idx = m_idx_max + seqlen_k - seqlen_q
        n_idx_right = n_idx if is_causal else n_idx + window_size_right
        n_block_max = min(n_block_max, ceil_div(n_idx_right, tile_n))
    n_block_min = 0
    if is_local and window_size_left is not None:
        m_idx_min = m_block * tile_m
        if qhead_per_kvhead_packgqa > 1:
            m_idx_min = m_idx_min // qhead_per_kvhead_packgqa
        n_idx = m_idx_min + seqlen_k - seqlen_q
        n_idx_left = n_idx - window_size_left
        n_block_min = max(n_idx_left // tile_n, 0)
    if is_split_kv:
        per_split = (0 if n_block_max <= n_block_min
                     else (n_block_max - n_block_min + num_splits - 1) // num_splits)
        n_block_min = n_block_min + split_idx * per_split
        n_block_max = min(n_block_min + per_split, n_block_max)
    return n_block_min, n_block_max

for mb in range(8):  # seqlen_q=1024, tile_m=128 -> 8 个 Q 块
    print(f"m_block={mb}: {get_n_block_min_max(1024, 1024, 128, 128, mb, True, False, None, None)}")
```

4. **需要观察的现象**：`m_block` 从 0 到 7，`n_block_max` 应依次为 `1,1,2,3,4,5,6,7,8` 中递增；具体地 `m_block=0→(0,1)`、`m_block=3→(0,4)`、`m_block=7→(0,8)`。
5. **预期结果**：第 3 块打印 `(0, 4)`，与手算一致；第 0 块 `(0,1)`（首块只看 K 块 0），第 7 块 `(0,8)`（末块看全部 K 块）。这正是因果掩码「上三角被跳过」带来的工作量节省——越靠后的 Q 块要算的 K 块越多。
6. （可选，纯 CPU 脚本，无需 GPU，可本地直接运行验证。）

#### 4.2.5 小练习与答案

**练习 1**：把上面的脚本改成滑窗掩码（`is_causal=False, is_local=True, window_size_left=256, window_size_right=0`，仍 `seqlen_q=seqlen_k=1024`），求 `m_block=4` 的范围，并解释含义。

**参考答案**：
- 上界分支（`is_local and window_size_right is not None`）：`m_idx_max=5*128=640`，`n_idx=640`，`window_size_right=0` → `n_idx_right=640`，`n_block_max=min(8, ceil(640/128))=min(8,5)=5`。
- 下界分支：`m_idx_min=4*128=512`，`n_idx=512`，`n_idx_left=512-256=256`，`n_block_min=max(256//128, 0)=2`。
- 结果 `(2, 5)`。含义：第 4 个 Q 块（Q 行 512..639）只看 K 块 2、3、4（K 行 256..639），即「往左最多看 256 个 key」。

**练习 2**：为什么上界用 `ceil_div` 而下界用 `//`？如果下界也用 `ceil_div` 会出什么问题？

**参考答案**：上界 `n_idx_right` 是「允许的最大 K 索引」，包含它的块必须算，所以向上取整把该块纳入；下界 `n_idx_left` 是「允许的最小 K 索引」，包含它的块也必须算（块内更小的非法位置由 `apply_mask` 元素级处理），所以向下取整。若下界也用 `ceil_div`，当 `n_idx_left` 落在某块中间时会**跳过那个仍含合法 key 的块**，导致漏算、结果错误。

---

### 4.3 split_kv 切分逻辑

#### 4.3.1 概念说明

在长上下文或解码场景，单个 thread block 顺序遍历所有 K/V 块会成为瓶颈。SplitKV 的做法是：把同一个 `(batch, head, m_block)` 任务对应的 K/V 块范围，**横向切成 `num_splits` 段**，分给 `num_splits` 个 thread block 并行算，每段各自产出一份「部分输出 O 和部分 logsumexp LSE」，最后由专门的 combine kernel 用 log-sum-exp 合并（合并的数学与 combine kernel 见 u7-l2，本讲只看「切」这一步）。

`BlockInfo` 通过 `is_split_kv` 开关 + `split_idx` / `num_splits` 两个运行期参数支持这种切分。注意 `is_split_kv` 是 `Constexpr[bool]`——是否启用 SplitKV 会特化不同的 kernel（带 SplitKV 的 kernel 还要写部分 O/LSE、走 combine），而 `split_idx` / `num_splits` 是运行期值，改它们不重编译。

#### 4.3.2 核心流程

切分发生在「已经算好完整范围 `[n_block_min, n_block_max)` 之后」：

\[
\text{per\_split} = \left\lceil \frac{\text{n\_block\_max} - \text{n\_block\_min}}{\text{num\_splits}} \right\rceil
\]

第 `split_idx` 段（从 0 开始）拿到：

\[
[\,\text{n\_block\_min} + \text{split\_idx} \cdot \text{per\_split},\ \min(\text{n\_block\_min} + (\text{split\_idx}+1)\cdot \text{per\_split},\ \text{n\_block\_max})\,)
\]

也就是把块**按 n_block 从小到大连续等分**（最后一段可能更短）。一个边界情况：当 `n_block_max <= n_block_min`（本 Q 块压根没有 K 块要算，比如 varlen 里的空序列），`per_split = 0`，每段都拿到空范围，对应的 thread block 通过 `n_block_min < n_block_max` 判断后直接跳过。

#### 4.3.3 源码精读

[flash_attn/cute/block_info.py:L47-L54](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/block_info.py#L47-L54) —— SplitKV 切分逻辑。注意第 47 行用 `cutlass.const_expr(self.is_split_kv)` 把整段切分代码包成编译期分支：非 SplitKV kernel 编译时这段直接消失，零运行期开销。

调用侧：Blackwell 前向 kernel 在 SplitKV 模式下，从 tile scheduler 拿到本 CTA 的 `split_idx, num_splits`，传进 `get_n_block_min_max`，并用 `n_block_min < n_block_max` 判断本 split 是否真的有活干：

[flash_attn/cute/flash_fwd_sm100.py:L1473-L1476](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm100.py#L1473-L1476) —— `block_info.get_n_block_min_max(seqlen, m_block, split_idx, num_splits)`，随后 `if const_expr(not self.is_split_kv) or n_block_min < n_block_max:` 决定是否跳过空 split。

（作为对照，Ampere 路径 [flash_fwd.py:L790](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L790) 把 `is_split_kv` 写死为 `False`，且 `interface.py` 里明确 `assert not is_split_kv, "SplitKV not supported on SM 8.0"`——SplitKV 是 Sm100 起才有的能力。）

#### 4.3.4 代码实践

1. **实践目标**：用 4.2.4 的脚本体验 SplitKV 切分，观察 `num_splits=4` 时各 `split_idx` 拿到的子范围恰好拼回完整范围。
2. **操作步骤**：在脚本里设 `seqlen_q=seqlen_k=1024, tile_m=tile_n=128, m_block=7`（末块，无掩码，完整范围 `(0,8)`），再设 `is_split_kv=True, num_splits=4`，对 `split_idx=0,1,2,3` 分别打印。

```python
# 示例代码：SplitKV 切分演示（非项目代码，复用上面的 get_n_block_min_max）
full = get_n_block_min_max(1024, 1024, 128, 128, 7, False, False, None, None)
print("完整范围:", full)                       # 预期 (0, 8)
covered = []
for s in range(4):
    lo, hi = get_n_block_min_max(1024, 1024, 128, 128, 7, False, False, None, None,
                                 is_split_kv=True, split_idx=s, num_splits=4)
    print(f"split_idx={s}: ({lo}, {hi})")
    covered.extend(range(lo, hi))
print("四段并集:", sorted(set(covered)))        # 预期 [0,1,2,3,4,5,6,7]
```

3. **需要观察的现象**：`per_split = ceil((8-0)/4) = 2`，四段依次是 `(0,2),(2,4),(4,6),(6,8)`。
4. **预期结果**：四段 n_block 集合并集恰好是 `{0,1,...,7}`，与完整范围 `(0,8)` 一致——说明切分是无遗漏、无重叠的。
5. 再试 `num_splits=3`：`per_split = ceil(8/3) = 3`，前三段 `(0,3),(3,6),(6,8)`，最后一段较短（`min(6+3,8)=8`），体现「最后一段可能更短」。
6. （纯 CPU 脚本，可本地直接运行验证。）

#### 4.3.5 小练习与答案

**练习 1**：为什么切分要放在「算好完整范围之后」，而不是直接按 n_block 总数均分？

**参考答案**：因为完整范围 `[n_block_min, n_block_max)` 本身已经经过了因果/滑窗裁剪——某些 Q 块的合法范围很短（比如首块因果下只有 1 个 K 块）。先裁剪再切分，能保证每个 split 拿到的都是「真正需要算」的块，避免把宝贵的并行度浪费在被掩掉的块上；同时也让 `n_block_min < n_block_max` 这个「空 split 跳过」判断天然成立。

**练习 2**：`is_split_kv` 是 `Constexpr[bool]`，而 `split_idx` 是普通 `Int32`。这给重编译行为带来什么影响？

**参考答案**：切换「是否启用 SplitKV」（`num_splits` 从 1 变 >1）会改变 `is_split_kv` 这个编译期常量，触发重编译出一份带切分逻辑、写部分 O/LSE 的 kernel；而在已编译好的 SplitKV kernel 内部，每个 CTA 用不同的 `split_idx` 运行，只是运行期数值不同，不重编译。这与 u2-l1 的「compile_key 由 Constexpr 字段决定」一致。

---

## 5. 综合实践

把 4.2 和 4.3 串起来，做一个「因果 + SplitKV 联合」的范围推演任务。

**任务**：设 `seqlen_q=seqlen_k=2048, tile_m=tile_n=128, causal=True, num_splits=4`。

1. 先用 4.2.4 的脚本（不带 split）求出每个 `m_block ∈ [0, 15]` 的完整因果范围 `[n_block_min, n_block_max)`。
2. 挑 `m_block=15`（末块，范围最大），用 4.3.4 的方式把它切成 4 段，列出每段 `split_idx` 的 `[n_block_min, n_block_max)`。
3. 计算「因果掩码让全部 16 个 Q 块总共省下了多少个 K 块的计算」（相对不做掩码的 `16 × 16 = 256` 块·次）。
4. 把你的 K 块节省比例写成一句话，体会因果掩码对工作量的影响。

**参考答案要点**（请先自己算再对照）：

- 各 `m_block` 的 `n_block_max` 依次为 `1,1,2,...,16`（即 `m_block+1` 上取整后的块数），`n_block_min` 恒为 0。
- `m_block=15` 完整范围 `(0, 16)`，`num_splits=4` → `per_split=4`，四段 `(0,4),(4,8),(8,12),(12,16)`。
- 因果下总 K 块·次 \(=\sum_{m=0}^{15}(m+1) = \frac{16 \times 17}{2} = 136\)，相比无掩码的 \(16\times16=256\)，节省了 \(256-136=120\) 块·次，约 **46.9%**。这正是因果掩码 + 块级跳过的收益，也是 `get_n_block_min_max` 存在的意义。

---

## 6. 本讲小结

- `BlockInfo` 是一个只读的冻结数据类，保存 `tile_m/tile_n` 与各掩码开关（多为 `Constexpr`），负责回答「一个 Q tile 要遍历哪些 K/V tile」。
- 核心方法 `get_n_block_min_max` 围绕对角索引 `n_idx = m_idx + seqlen_k - seqlen_q` 推导因果/滑窗范围：上界用 `ceil_div`、下界用 `//`，得到的是**保守的块级包络**，边界块内的非法位置交给 u3-l1 的 `apply_mask` 元素级处理。
- `SeqlenInfoQK` 把序列长度相关的 gmem 读取**在每个 tile 开头一次性做完**并预计算 `num_n_blocks`，`BlockInfo` 只是它的消费者。
- SplitKV 在完整范围算好后，把它**连续等分**成 `num_splits` 段（最后一段可能更短），`is_split_kv` 是 `Constexpr`、`split_idx/num_splits` 是运行期值；空 split 用 `n_block_min < n_block_max` 跳过。
- `get_m_block_min_max` 是前者的「转置」，给定 K 块反求 Q 块范围，供反向 kernel 使用。
- 主循环从 `n_block_max-1` 倒着遍历到 `n_block_min`，故返回值是半开区间 `[n_block_min, n_block_max)`。

## 7. 下一步学习建议

- **u3-l3（SeqlenInfo 变长）**：深入 `SeqlenInfoQK` 的另一半——`cu_seqlens` 打包、`offset` 与 `offset_padded` 的对齐含义，理解变长序列下 `seqlen_q/seqlen_k` 是如何被读出来的。
- **u4-l1（在线 Softmax）**：本讲只决定了「算哪些块」，接下来该看「每块算完后如何用 row_max/row_sum/rescale 把分块结果精确拼回完整 softmax」。
- **u7-l2（SplitKV 与 Combine）**：本讲的切分只产出「部分 O + 部分 LSE」，下一步看 combine kernel 如何用 log-sum-exp 把它们合并成最终输出。
- **延伸阅读**：对照 [block_info.py:L104-L156](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/block_info.py#L104-L156) 的 `get_n_block_min_causal_local_mask` / `get_n_block_max_for_m_block`，理解主循环为何要把「需要元素级掩码的边界块」和「无需掩码的内部块」分成两段分别迭代。
