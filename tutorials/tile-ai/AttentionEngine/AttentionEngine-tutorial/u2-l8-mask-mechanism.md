# Mask 机制：mask_mod、block_mask 与 torch.fx 降级

## 1. 本讲目标

本讲是「降级三件套」之外的第四条降级线索——**遮蔽（mask）**。学完后你应当能够：

1. 说清 `mask_mod` 这个按下标返回布尔的 Python 函数，是如何被转换成两种产物的：**逐元素的 kernel 内代码** 与 **块级的稀疏跳块表 `block_mask`**。
2. 理解 `create_mask` / `create_block_mask` / `_convert_mask_to_block_mask` 这条「向量化 → 分块」的生成链。
3. 掌握 `is_causal_mask` 与 `is_less_causal_mask` 两个判定的数学含义，以及它们如何决定 `infer_mask` 在 **dense（稠密）模板 `TlAttnTemplate`** 与 **blocksparse（块稀疏）模板 `TlBlockAttnTemplate`** 之间做出选择。
4. 理解 `torch.fx` 的符号追踪（`symbolic_trace`）在把 `mask_mod` 降级成 kernel 代码时扮演的角色。

---

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自 u1-l4、u2-l7）：

- **`mask_mod` 的签名**：`mask_mod(b, h, q_idx, kv_idx) -> bool`，按下标返回布尔值，决定 `scores[b,h,q_idx,kv_idx]` 是否参与注意力。它与 `score_mod`（逐元素数值变换）是正交的两件事。
- **online 分块算法**：注意力按 KV 块（`block_N`）循环累加，因此 mask 的「块级」性质（整块是否全被遮蔽）直接决定了能否「跳过整块」而不进入循环。
- **降级（lowering）的四层架构**：transform（符号 IR）→ codegen（发射代码）→ lower（编排）→ template（Jinja2 渲染）。本讲的 `create_block_mask` 等落在 transform 层，`tl_codegen_from_torchfx` 落在 codegen 层，模板选择落在 lower 层。

一个关键直觉：**GPU kernel 不能在逐元素循环里调用一个 Python 函数**。所以 `mask_mod` 必须被「翻译」成两种 device 代码材料——这正是本讲要拆解的。

> 术语提示：源码中模板字段写作 `is_casual`（少了一个 `e`），它是 `is_causal` 的拼写笔误，但作为 Jinja2 占位符名字是真实存在的。本讲引用源码时保留原拼写，讲解时写「causal」。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [attention_engine/core/transform/core.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py) | `create_mask` / `create_block_mask` / `is_causal_mask` / `is_less_causal_mask` 等 mask 生成与判定的实现。 |
| [attention_engine/core/codegen/common.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/common.py) | `tl_codegen_from_torchfx` / `tl_codegen_from_torchNode`：把 `torch.fx` 图翻译成 TileLang 代码片段。 |
| [attention_engine/core/lower/lower.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py) | `lower_tl` 中的 mask 降级编排：trace mask_mod、`infer_mask` 分支、模板选择。 |
| [attention_engine/core/template/tl_template/attn/attn_tl.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py) | dense 模板：展示 `mask_mod_code` / `is_casual` / `loop_range` 如何被注入并应用。 |
| [attention_engine/core/template/attn_template.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/attn_template.py) / [blockattn_template.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/blockattn_template.py) | 两个模板类 `TlAttnTemplate`（dense）/ `TlBlockAttnTemplate`（blocksparse）。 |
| [attn_script/sparseattn.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/sparseattn.py) | 滑动窗口注意力示例（`infer_mask=True`）。 |
| [attn_script/blocksparseattn.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/blocksparseattn.py) | 外部 `block_mask` 示例（`extern_block_mask=True`）。 |
| [attention_engine/tests/test_blockmask.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/tests/test_blockmask.py) / [test_torchtrace.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/tests/test_torchtrace.py) | mask 与 torch.fx 的最小测试。 |

---

## 4. 核心概念与源码讲解

### 4.1 mask_mod → 布尔张量 → block_mask 的生成

#### 4.1.1 概念说明

用户写的 `mask_mod(b, h, q_idx, kv_idx) -> bool` 是一个**标量函数**：给定一组下标返回一个布尔值。但 kernel 关心的是两个**张量级**的问题：

1. **逐元素层面**：循环内每个 `(i, j)` 该不该遮蔽？这需要把 `mask_mod` 翻译成 kernel 内代码（见 4.3）。
2. **块级层面**：整个 `block_M × block_N` 的 KV 块是否**全部被遮蔽**？如果是，就可以整块跳过、根本不进入循环——这正是 blocksparse 加速的来源。

`block_mask` 就是块级层面的产物：把逐元素布尔张量压缩成「每块是否有任意有效元素」的块级掩码（`int8` 张量），形状为 `(B, H, Q//block_M, KV//block_N)`。

#### 4.1.2 核心流程

```
mask_mod (标量函数)
   │  create_mask       ← torch.vmap 四层向量化
   ▼
mask_tensor: (B, H, Q_LEN, KV_LEN) bool   逐元素布尔
   │  _convert_mask_to_block_mask   ← pad 对齐 + reshape + 块内求和
   ▼
block_mask: (B, H, Q//block_M, KV//block_N) int8   块级 (>0 即有有效元素)
```

关键点：`create_mask` 不解析 `mask_mod` 的源码，而是**真的把函数当函数调用一遍**——用 `torch.vmap` 让它能一次性吃下整个下标网格，返回完整的 4D 布尔张量。这是一种「以运行换静态」的策略：mask 在编译期是确定的，跑一次 `mask_mod` 就能得到整张表。

#### 4.1.3 源码精读

**`create_mask`：四层 `vmap` 把标量函数变 4D 张量**

[attention_engine/core/transform/core.py:352-398](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L352-L398) 中：

```python
b = torch.arange(0, B, device=device)   # (B,)
h = torch.arange(0, H, device=device)   # (H,)
m = torch.arange(0, Q_LEN, device=device)
n = torch.arange(0, KV_LEN, device=device)

dimensions = [
    (None, None, None, 0),   # 向量化第 4 个参数 n
    (None, None, 0, None),   # 向量化第 3 个参数 m
    (None, 0, None, None),   # 向量化第 2 个参数 h
    (0, None, None, None),   # 向量化第 1 个参数 b
]
for dim in dimensions:
    mod_fn = torch.vmap(mod_fn, in_dims=dim, out_dims=0)
mask = mod_fn(b, h, m, n)
```

这里 `in_dims` 中 `0` 表示「该参数是向量、对它做 batch」，`None` 表示「该参数广播为标量」。四层 `vmap` 叠加后，`mod_fn(b,h,m,n)` 把四个一维下标向量映射成形状 `(B, H, Q_LEN, KV_LEN)` 的布尔张量。注意顺序：最后向量化的是 `b`，因此它落在最外维（dim 0）。

**`_convert_mask_to_block_mask`：块内求和压缩成块级掩码**

[attention_engine/core/transform/core.py:410-453](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L410-L453) 的核心是三步变形：

```python
# 1. 对齐到块大小的整数倍（不足则 pad）
mask = torch.nn.functional.pad(mask, (0, pad_kv, 0, pad_q), ...)
# 2. 把 (Q, KV) 拆成 (Q//BM, BM, KV//BN, BN)
mask = mask.view(B, H, Q//Q_BLOCK_SIZE, Q_BLOCK_SIZE, KV//KV_BLOCK_SIZE, KV_BLOCK_SIZE)
mask = mask.permute(0, 1, 2, 4, 3, 5)   # 块坐标在前，块内坐标在后
# 3. 块内求和：>0 说明该块至少有一个 True
mask_block_sum = mask.sum(dim=[-2, -1])   # (B, H, Q//BM, KV//BN)
partial_blocks = mask_block_sum > 0
```

当 `separate_full_blocks=False`（`create_block_mask` 用的就是这个）时，只返回 `partial_blocks`（块内有任意有效元素即为 1），第二个返回值为 `None`。`>0` 的语义是「这块至少有一个未被遮蔽的元素，必须计算」。

**`create_block_mask`：串联两者**

[attention_engine/core/transform/core.py:492-506](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L492-L506)：

```python
BLOCK_SIZE = 128
def create_block_mask(mask_mod, B, H, QLen, KVLen, device, Q_BLOCK_SIZE=None, KV_BLOCK_SIZE=None):
    if Q_BLOCK_SIZE is None: Q_BLOCK_SIZE = BLOCK_SIZE   # 默认 128
    if KV_BLOCK_SIZE is None: KV_BLOCK_SIZE = BLOCK_SIZE
    mask_tensor = create_mask(mask_mod, B, H, QLen, KVLen, device)
    partial_block_mask, _ = _convert_mask_to_block_mask(mask_tensor, ...)
    return partial_block_mask
```

注意默认块大小 `_DEFAULT_SPARSE_BLOCK_SIZE = 128`（[core.py:400](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L400)），它通常与 kernel 的 `block_M`/`block_N` 对齐——只有块大小一致，「跳块」才等价于「跳过 kernel 的一个循环步」。

#### 4.1.4 代码实践

**实践目标**：直观看到一个 `mask_mod` 被压缩成 `block_mask` 后的样子。

**操作步骤**：`test_blockmask.py` 提供了无需 GPU 的最小验证（用 `"cpu"`）。你可以直接在仓库根目录参照它写一段：

```python
# 示例代码：基于 test_blockmask.py 的写法
from core.transform.core import create_block_mask, create_mask
import torch

def causal_mask(b, h, q_idx, kv_idx):
    return q_idx >= kv_idx

B, H, S = 2, 4, 512
Q_BLOCK_SIZE, K_BLOCK_SIZE = 128, 64

elem_mask = create_mask(causal_mask, B, H, S, S, "cpu")          # (2,4,512,512) bool
block_mask = create_block_mask(causal_mask, B, H, S, S, "cpu", Q_BLOCK_SIZE, K_BLOCK_SIZE)
print(elem_mask.shape, block_mask.shape)   # torch.Size([2,4,512,512]) torch.Size([2,4,4,8])
print(block_mask[0, 0])                     # 4x8 的 0/1 块级下三角
```

**需要观察的现象**：`block_mask[0,0]` 应呈现下三角为 1、上三角为 0 的形态（块大小 128×64 时，4×8 的网格里左下区域为 1）。

**预期结果**：块级掩码正确反映「该块内存在未遮蔽元素」。若运行环境无 `core` 包，需按 u1-l2 配置 `PYTHONPATH` 指向 `attention_engine/`。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `Q_BLOCK_SIZE` 设得比 `KV_BLOCK_SIZE` 大很多（如 256 vs 32），`block_mask` 的形状与「块粒度」会如何变化？

**答案**：形状变为 `(B, H, S//256, S//32)`，即 Q 方向块更少、KV 方向块更多；块粒度在 Q 方向更粗（一块覆盖 256 个 query），因此「跳块」的最小单位在 Q 方向变大，可能放过更多本可跳过的细碎区域。

**练习 2**：`_convert_mask_to_block_mask` 为什么用 `sum(dim=[-2,-1]) > 0` 而不是 `== Q_BLOCK_SIZE*KV_BLOCK_SIZE`？

**答案**：`>0` 判定的是「块内至少有一个有效元素」（partial block），这正是 kernel 必须计算该块的判据；`== full` 判定的是「整块全有效」（full block），用于另一种 `separate_full_blocks=True` 的稀疏编码，把 full 与 partial 分开存储以做不同优化。

---

### 4.2 causal 判定与 dense/blocksparse 模板选择

#### 4.2.1 概念说明

并非所有 mask 都值得用 blocksparse 模板。**标准因果（causal）掩码**有一个极好的性质：它的上三角完全为空。这意味着 kernel 不需要逐元素查 mask，也不需要块级跳块表——只要让 KV 循环的上界 `loop_range` 随 query 块 `bx` 收缩即可，整片上三角区域天然不会被遍历：

```python
loop_range = T.ceildiv((bx + 1) * block_M, block_N) if is_casual else T.ceildiv(seq_len, block_N)
```

这是最廉价、最快的路径（dense 模板 `TlAttnTemplate`）。只有当 mask **不是**标准因果（例如滑动窗口、任意稀疏模式）时，才值得引入 `block_mask` 作为运行期输入，用 blocksparse 模板 `TlBlockAttnTemplate` 按「块」跳过。

因此 `infer_mask` 的职责是：**用一个判定函数把 mask 分类，自动选择最合适的模板与循环策略**。

#### 4.2.2 核心流程

判定分两层，对应两个不同的用途：

| 判定函数 | 数学含义 | 控制的用途 |
| --- | --- | --- |
| `is_causal_mask` | block_mask 是否**精确等于**某个下三角因果模板 | 决定 **dense vs blocksparse 模板** |
| `is_less_causal_mask` | block_mask 在严格上三角区域是否**全为 0** | 决定 **`is_casual`（loop_range 截断）** |

`lower_tl` 中 `infer_mask=True` 时的三分支：

```
mask 是精确因果 (is_causal_mask=True)
   → dense 模板 TlAttnTemplate，丢弃 block_mask（=None）
   → is_casual=True（由 is_less_causal 推出），loop_range 截断上三角

mask 非精确因果 (is_causal_mask=False)
   → blocksparse 模板 TlBlockAttnTemplate，保留 block_mask 作运行期输入
   → is_casual 仍由 is_less_causal 决定（若上三角全空，仍可截断循环）

extern_block_mask=True（用户外部给 block_mask）
   → 强制 blocksparse 模板，mask_mod 可为 None
```

注意区分：**精确因果**走 dense（最快），**非精确因果但上三角全空**（如带前缀的因果）走 blocksparse 但 `is_casual` 仍可截断循环，**任意稀疏**走 blocksparse 且不截断。

#### 4.2.3 源码精读

**`is_causal_mask`：构造参考下三角模板并整体比对**

[attention_engine/core/transform/core.py:455-470](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L455-L470)：

```python
def is_causal_mask(mask_tensor, block_M, block_N):
    B, H, M, N = mask_tensor.shape
    q_idx = torch.arange(M).unsqueeze(-1)   # 列向量 (M,1)
    kv_idx = torch.arange(N).view(1,-1)     # 行向量 (1,N)
    mask = (q_idx+1)*block_M > kv_idx*block_N
    return torch.all(mask_tensor.bool() == mask.to(mask_tensor.device))
```

它构造参考模板：块 `(i,j)` 为 True 当且仅当

\[(i+1)\cdot \text{block\_M} > j\cdot \text{block\_N}\]

即「该 query 块的末端下标边界 `(i+1)·block_M` 超过该 kv 块的起点 `j·block_N`」。然后用 `torch.all(... == ...)` 判断**整张** block_mask 是否与该参考模板逐块相等。注意这是「精确等价」判定——只要有一块不一致就返回 False。

**`is_less_causal_mask`：上三角区域是否全空**

[attention_engine/core/transform/core.py:472-488](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L472-L488)：

```python
def is_less_causal_mask(mask_tensor, block_M, block_N):
    B, H, M, N = mask_tensor.shape
    q_idx = torch.arange(M).unsqueeze(-1)
    kv_idx = torch.arange(N).view(1,-1)
    mask = (q_idx+1)*block_M-1 < (kv_idx)*block_N     # 严格上三角区域
    filter_tensor = mask_tensor[...,mask]              # 取出这些位置的块
    is_all_zero = torch.all(filter_tensor == 0)
    return is_all_zero
```

它圈出「严格在因果上三角」的块：

\[(i+1)\cdot \text{block\_M} - 1 < j\cdot \text{block\_N}\]

并检查这些位置**是否全部为 0**（即全空）。这是一个比 `is_causal_mask` **弱**的条件——它只要求上三角没有有效块，不要求下三角填满。它正是「用 `loop_range` 截断上三角是否安全」的充要条件：只要上三角全空，截断循环就不会漏掉任何有效块。

**`lower_tl` 的 `infer_mask` 分支：模板选择**

[attention_engine/core/lower/lower.py:721-738](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L721-L738)：

```python
if infer_mask:
    ...
    if block_mask is not None:
        block_mask = create_block_mask(block_mask, Batch, head, seqlen, seqlen, ...)  # 真→块表
    if block_mask is not None:
        lower_output.is_casual = "True" if is_less_causal_mask(block_mask, ...) else "False"
    else:
        lower_output.is_casual = "False"
    if (block_mask is not None and not is_causal_mask(block_mask, ...)) or extern_block_mask:
        tlattn_template = TlBlockAttnTemplate           # 非精确因果 → blocksparse
        output_idx_list = [i+1 for i in output_idx_list]# block_mask 插到输出列表最前
        bwd_output_idx_list = [i+1 for i in bwd_output_idx_list]
    else:
        block_mask = None                                # 精确因果 → dense，丢弃块表
        tlattn_template = TlAttnTemplate
```

读法要点：

- 第 727 行把 `block_mask`（此时还是原始 `mask_mod` 函数）**重写**为真正的块级张量。注意在此之前（[lower.py:709-719](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L709-L719)）已经用原始函数生成了逐元素 `mask_mod_code`（见 4.3）。
- `is_casual`（loop 优化）来自 `is_less_causal_mask`；模板选择来自 `is_causal_mask`。两者来源不同。
- 精确因果时 `block_mask = None`——dense 模板不需要运行期块表，全靠 `loop_range` 截断。
- `output_idx_list = [i+1 ...]` 把 `block_mask` 作为 kernel 的**第一个输出张量**登记，因为 blocksparse 模板要把它作为运行期参数接收。

**`extern_block_mask` 分支**：当 `infer_mask=False` 但 `extern_block_mask=True` 时（[lower.py:758-765](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L758-L765)），强制走 blocksparse，`is_casual="False"`，`block_mask` 由用户在调用 `mod(q,k,v, block_mask=...)` 时传入（见 `blocksparseattn.py`）。

**模板内：`is_casual` 如何驱动 `loop_range` 与逐元素 mask**

在 dense 模板 [attn_tl.py:61](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L61) 声明 `is_casual = {{is_casual}}` 后，[attn_tl.py:113-135](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L113-L135) 是 mask 应用的核心：

```python
loop_range = (
    T.ceildiv((bx + 1) * block_M, block_N) if is_casual else T.ceildiv(seq_len, block_N)
)
for k in T.Pipelined(loop_range, num_stages=num_stages):
    ...
    if (is_casual or {{is_mask_mod_code}}) and {{is_inf_mask}}:
        for i, j in T.Parallel(block_M, block_N):
            {{q_idx}} = bx * block_M + i        # 由 4.3 的 torch.fx 注入
            {{kv_idx}} = k * block_N + j
            {{mask_mod_code | indent(28)}}       # 逐元素 mask 计算
            scores[i, j] = T.if_then_else(
                {{mask_output}}, 0, -T.infinity(scores.dtype)
            )
    else:
        T.clear(scores)
```

要点：

- `loop_range` 在 `is_casual=True` 时随 `bx` 收缩，整片上三角不进入循环——这是 causal 加速的根本。
- 即便 `is_casual=True`，对角线上的**边界块**仍含有需要遮蔽的元素，所以条件写成 `is_casual or is_mask_mod_code`：只要 causal 或存在自定义 mask，就跑逐元素 mask。
- `{{is_inf_mask}}`（[lower.py:637](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L637)）决定遮蔽值是 `-inf`（softmax 路径）还是 `0`：`is_inf_mask = "True" if block_mask is not None and mask_value == "-inf" else "False"`。被遮蔽位置在 softmax 时填 `-inf`（exp 后归零），在非 softmax（如 sigmoid）路径填 `0`。

> 小结：`is_casual` 是「**循环级**优化」，`mask_mod_code`/`is_mask_mod_code` 是「**元素级**补丁」，`block_mask` 是「**块级**跳过」。三者层层递进，由 `infer_mask` 自动选用。

#### 4.2.4 代码实践（本讲核心实践）

**实践目标**：自定义一个非因果的滑动窗口 `mask_mod`，**预测并验证** `lower_tl` 的 `infer_mask` 分支会如何判定它、并选择哪个模板。

**操作步骤**：

1. 先离线计算块表与判定（CPU 即可，参照 `test_blockmask.py`）：

```python
# 示例代码：预测模板选择
from core.transform.core import create_block_mask, is_causal_mask, is_less_causal_mask
import torch

window_size = 256
def sliding_window(b, h, q_idx, kv_idx):
    return torch.logical_and(q_idx >= kv_idx, q_idx < kv_idx + window_size)

B, H, S, BM, BN = 1, 32, 2048, 128, 128
block_mask = create_block_mask(sliding_window, B, H, S, S, "cpu", BM, BN)
print("is_causal_mask     :", is_causal_mask(block_mask, BM, BN))      # 预期 False
print("is_less_causal_mask:", is_less_causal_mask(block_mask, BM, BN)) # 预期 True
```

2. 据此**推断** `lower_tl` 的行为：
   - `is_causal_mask=False` → 进入 `if not is_causal_mask` 分支 → 选 `TlBlockAttnTemplate`（blocksparse）。
   - `is_less_causal_mask=True` → `is_casual="True"` → `loop_range` 仍按 query 块截断上三角（合理：滑动窗口上三角也是空的）。
   - `block_mask` 被保留作为运行期输入。
3. （需要 GPU + TileLang 环境）运行 `attn_script/sparseattn.py`，其中 `block_sparse_mask` 正是滑动窗口、`infer_mask=True`（[sparseattn.py:107](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/sparseattn.py#L107)）。可在 `attention_engine/attn_engine/attn_engine.py` 的 `_select_lower_template` → `lower_tl` 路径上加一行日志，确认走到了 `TlBlockAttnTemplate`。

**需要观察的现象**：步骤 1 打印 `is_causal_mask=False`、`is_less_causal_mask=True`，与步骤 2 的推断一致；步骤 3 生成的 kernel 接收 `block_mask` 作为参数。

**预期结果**：滑动窗口被正确归类为「非精确因果但上三角全空」，走 blocksparse 模板并保留 `is_casual` 循环截断。若无法运行 GPU 部分，步骤 1+2 的 CPU 判定已足以验证核心逻辑，步骤 3 标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `is_casual`（控制 `loop_range`）取自 `is_less_causal_mask` 而不是更严格的 `is_causal_mask`？

**答案**：`loop_range` 截断上三角的安全性只要求「上三角没有任何有效块」，这正是 `is_less_causal_mask` 的判定；它比精确因果弱，能让带前缀的因果（如 `q_idx-offset >= kv_idx`）也享受循环截断，扩大了优化覆盖面。

**练习 2**：标准因果 mask（`q_idx >= kv_idx`）经 `infer_mask` 后，`block_mask` 的最终取值是什么？为什么？

**答案**：`None`。因为 `is_causal_mask=True` 会进入 else 分支执行 `block_mask = None`——dense 模板完全靠 `loop_range` 截断 + 对角块的逐元素 mask 工作，不需要运行期块表。

---

### 4.3 torch.fx 符号追踪降级 mask_mod

#### 4.3.1 概念说明

`mask_mod` 是用户写的 Python 函数，但 kernel 内的逐元素循环需要一段**等价的 device 代码**来算「这个 `(i,j)` 该不该遮蔽」。AttentionEngine **不解析 `mask_mod` 的源码字符串**，而是借用 PyTorch 的 `torch.fx` 框架做**符号追踪（symbolic trace）**：

- `torch.fx.symbolic_trace(mask_mod)` 把函数记录成一张计算图 `fx.GraphModule`，图中的每个节点是一次函数调用（`call_function`）、占位输入（`placeholder`）或输出（`output`）。
- 然后逐节点把这张图翻译成 TileLang 代码片段 `mask_mod_code`，注入模板的逐元素循环里。

这与 score_mod 走「符号 IR（SymbolScalar）」是**两条不同的降级路线**：mask_mod 走 `torch.fx`，因为它的核心是「下标比较 + 逻辑运算」（`>=`、`<`、`logical_and`），用 `fx` 直接追踪比塞进符号 DAG 更自然。`torch.logical_and` 这类算子会被映射到 Python 标准库 `operator.and_`。

#### 4.3.2 核心流程

```
mask_mod (Python 函数)
   │  fx.symbolic_trace
   ▼
fx.GraphModule  (节点序列: placeholder×4 → call_function×N → output)
   │  tl_codegen_from_torchfx  逐节点翻译
   ▼
mask_mod_code (TileLang 代码片段, 形如 "_0 = operator.ge(q_idx, kv_idx)")
   │  lower.py 提取节点名: q_idx/kv_idx/batch_idx/head_idx/mask_output
   ▼
模板注入: 逐元素循环内先算 {{mask_mod_code}}, 再用 {{mask_output}} 做 T.if_then_else
```

#### 4.3.3 源码精读

**`tl_codegen_from_torchfx` 与 `tl_codegen_from_torchNode`**

[attention_engine/core/codegen/common.py:124-152](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/common.py#L124-L152)：

```python
torch_supported_ops = {
    torch.logical_and: "operator.and_",
}

def is_operator_func(func):
    return func in operator.__dict__.values()

def tl_codegen_from_torchNode(node: fx.Node) -> str:
    if node.op == "call_function":
        if is_operator_func(node.target):                       # operator.ge / operator.lt 等
            return f"{node} = operator.{node.target.__name__}({', '.join([str(arg) for arg in node.args])})"
        elif node.target in torch_supported_ops:                # torch.logical_and → operator.and_
            return f"{node} = {torch_supported_ops[node.target]}({', '.join([str(arg) for arg in node.args])})"
        else:
            raise NotImplementedError(f"Operator {node.target} is not supported")
    elif node.op == "placeholder":                              # 输入参数, 不生成代码
        return ""
    elif node.op == "output":                                   # 输出, 不生成代码
        return ""
    else:
        raise NotImplementedError(f"Operator {node.op} is not supported")

def tl_codegen_from_torchfx(mask_graph: fx.GraphModule)->IndentedCode:
    graph = mask_graph.graph
    mask_code = IndentedCode()
    for node in graph.nodes:                                    # 按拓扑序遍历
        mask_code.add_line(tl_codegen_from_torchNode(node))
    return mask_code
```

要点：

- 每个 `call_function` 节点被翻译成一行 `<node> = operator.xxx(args)`。例如 `q_idx >= kv_idx` 经 `fx` 变成 `operator.ge(q_idx, kv_idx)`，再翻译成 `_0 = operator.ge(q_idx, kv_idx)`。
- `torch.logical_and` 通过 `torch_supported_ops` 映射成 `operator.and_`——目前支持的算子集合很小（`torch.logical_and` 与所有 `operator.*`），其它会 `raise NotImplementedError`。这是 mask_mod 只能使用受限算子的根因。
- `placeholder`/`output` 节点返回空串：输入由模板循环里显式赋值（`{{q_idx}} = bx*block_M+i`），输出就是最后一个 `call_function` 的结果。

**`lower_tl` 提取节点名并生成 `mask_mod_code`**

[attention_engine/core/lower/lower.py:708-719](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L708-L719)：

```python
if block_mask is not None:                       # block_mask 此时仍是原始 mask_mod 函数
    mask_graph = fx.symbolic_trace(block_mask)
    node_list = [node for node in mask_graph.graph.nodes]
    lower_output.batch_idx   = node_list[0].name
    lower_output.head_idx    = node_list[1].name
    lower_output.q_idx       = node_list[2].name
    lower_output.kv_idx      = node_list[3].name
    lower_output.mask_output = node_list[-1].args[0].name   # 最后输出的来源节点名
    lower_output.mask_mod_code    = str(tl_codegen_from_torchfx(mask_graph))
    lower_output.is_mask_mod_code = "True"
```

注意这里取的是 `fx` 节点的**名字**（`node.name`，如 `"q_idx"`、`"_0"`）。前 4 个 `placeholder` 节点对应 `b/h/q_idx/kv_idx` 四个形参；最后一个 `output` 节点的 `args[0]` 指向真正产出布尔结果的中间节点，其名字即 `mask_output`。这些名字会被原样塞进模板的 `{{q_idx}}`、`{{mask_output}}` 等占位符，保证 `mask_mod_code` 里用到的变量名与循环里的赋值一致。

**模板注入：逐元素算 mask 并用 `T.if_then_else` 应用**

回到 [attn_tl.py:124-135](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L124-L135)（4.2.3 已贴）：循环内先给 `{{q_idx}}/{{kv_idx}}/{{batch_idx}}/{{head_idx}}` 赋当前块的下标，再执行 `{{mask_mod_code}}`（即 `fx` 翻译出的若干 `operator.*` 调用），最后用 `T.if_then_else({{mask_output}}, 0, -T.infinity(...))` 把被遮蔽位置清零/置 `-inf`。这样，用户的 Python `mask_mod` 就**等价地**变成了 kernel 内一段逐元素代码。

**最小验证**：`test_torchtrace.py` 演示了完整链路（[test_torchtrace.py:39-44](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/tests/test_torchtrace.py#L39-L44)），它对 `sliding_window_mask` 调 `fx.symbolic_trace` 后逐节点 `print`，可以直观看到图结构与翻译结果。

#### 4.3.4 代码实践

**实践目标**：把一个 `mask_mod` 经 `torch.fx` 追踪，亲眼看到它变成怎样的 TileLang 代码。

**操作步骤**：参照 `test_torchtrace.py` 写一段（CPU 即可）：

```python
# 示例代码
import torch.fx as fx
import operator
from core.transform.core import IndentedCode

def causal_mask(b, h, q_idx, kv_idx):
    return q_idx >= kv_idx

g = fx.symbolic_trace(causal_mask)
print(g.graph)                       # 打印节点: placeholder q_idx/kv_idx, call_function ge, output
for node in g.graph.nodes:
    print(node.op, node.name, node.target if node.op=="call_function" else "")

# 复刻 tl_codegen_from_torchNode 的翻译（或直接 import）
from core.codegen.common import tl_codegen_from_torchfx
print(tl_codegen_from_torchfx(g))    # 预期: _0 = operator.ge(q_idx, kv_idx)
```

**需要观察的现象**：图里有 4 个 `placeholder`（b/h/q_idx/kv_idx）、一个 `call_function`（`operator.ge`）、一个 `output`；翻译产物是一行 `_0 = operator.ge(q_idx, kv_idx)`。

**预期结果**：mask_mod 的下标比较被等价翻译为 `operator.ge` 调用，这正是模板 `{{mask_mod_code}}` 的内容、`{{mask_output}}` 取 `"_0"`。若把 `mask_mod` 改成含 `torch.logical_and`，产物会多一行 `operator.and_`。

#### 4.3.5 小练习与答案

**练习 1**：如果 `mask_mod` 里用了 `torch.logical_or`（注意不在 `torch_supported_ops` 里），降级会发生什么？

**答案**：`tl_codegen_from_torchNode` 会进入 `else: raise NotImplementedError`，编译报错。当前只支持 `operator.*` 与 `torch.logical_and`，新增逻辑算子需往 `torch_supported_ops`（[common.py:124-127](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/common.py#L124-L127)）补映射。

**练习 2**：为什么 `mask_output` 取的是 `node_list[-1].args[0].name` 而不是 `node_list[-1].name`？

**答案**：图的最后一个节点是 `output` 节点（`node.op == "output"`），它本身只是「返回」语义，真正的布尔结果是其 `args[0]` 指向的上游 `call_function` 节点；所以要取 `args[0].name` 才能拿到产出掩码值的那个中间变量名。

---

## 5. 综合实践

把三个最小模块串起来：**为一个自定义 mask 完整预测 AttentionEngine 会生成的 mask 相关代码与模板选择**。

1. 定义一个「带前缀偏移的因果 + 滑动窗口」混合 mask：
   ```python
   def hybrid_mask(b, h, q_idx, kv_idx):
       return torch.logical_and(q_idx >= kv_idx, q_idx - 64 < kv_idx)
   ```
2. **块表层**：用 `create_mask` 与 `create_block_mask` 画出它的逐元素 mask 与块级 mask，确认块级形态（上三角是否全空？是否精确等于因果模板？）。
3. **判定层**：调用 `is_causal_mask` 与 `is_less_causal_mask`，预测 `lower_tl` 会选 `TlAttnTemplate` 还是 `TlBlockAttnTemplate`，以及 `is_casual` 是 True 还是 False。
4. **降级层**：用 `fx.symbolic_trace` + `tl_codegen_from_torchfx` 打印 `mask_mod_code`，标注它会被注入模板的哪一段（`attn_tl.py` 的 `for i,j in T.Parallel(...)` 循环内）。
5. 把 2~4 的预测写成一表，再（若有 GPU）跑 `AttentionEngine(..., infer_mask=True)` 对照生成代码确认。无法运行 GPU 时，1~4 的 CPU 推理即为完成。

> 这一实践检验你是否能脱离「跑通」、单凭源码逻辑推断框架的编译决策——这是阅读编译式框架的核心能力。

---

## 6. 本讲小结

- **mask 降级产出两种材料**：块级 `block_mask`（跳块用）与逐元素 `mask_mod_code`（循环内补丁用），分别由 `create_block_mask` 与 `torch.fx` 链路生成。
- **`create_mask` 用四层 `torch.vmap`** 把标量 `mask_mod` 真正跑成 `(B,H,Q,KV)` 布尔张量，`_convert_mask_to_block_mask` 再用块内求和压缩成块级 `int8` 掩码。
- **两个判定用途不同**：`is_causal_mask`（精确下三角）决定 **dense vs blocksparse 模板**；`is_less_causal_mask`（上三角全空）决定 **`is_casual` 的 `loop_range` 截断**。
- **`infer_mask` 三分支**：精确因果→dense 且丢弃块表（最快）；非精确因果→blocksparse 保留块表；`extern_block_mask`→强制 blocksparse 由用户外部供块表。
- **`torch.fx` 是 mask 的降级引擎**：`symbolic_trace` 把 `mask_mod` 记成图，`tl_codegen_from_torchNode` 逐节点翻译成 `operator.*` 调用，注入模板后用 `T.if_then_else` 应用遮蔽；当前仅支持 `operator.*` 与 `torch.logical_and`。
- **三层优化叠加**：`is_casual`（循环级截断）+ `mask_mod_code`（元素级补丁）+ `block_mask`（块级跳过），由 `infer_mask` 自动选用。

---

## 7. 下一步学习建议

- 本讲完成了 `lower_tl` 的最后一块拼图（score_mod / online_func / custom_inputs / mask 四件套全齐）。下一讲 **u3-l1（Jinja2 模板渲染机制）** 会把本讲提到的 `{{mask_mod_code}}`、`{{is_casual}}`、`{{is_inf_mask}}` 等占位符与 `lower_output` 字段的对应关系系统讲清，建议对照 [attn_template.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/attn_template.py) 的 `TlAttnTemplate` 一并阅读。
- 若想理解 blocksparse 模板如何用 `block_mask` 真正「跳块」，可提前浏览 [blockattn_template.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/blockattn_template.py) 与 `blockattn_tl.py`。
- 想扩展 mask 算子支持的同学，可基于本讲 4.3 的 `torch_supported_ops` 入手，这是一个小而完整的二次开发切入点（见 u5-l7）。
