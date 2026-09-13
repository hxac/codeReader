# 块选择细节与可视化：当前块必选与 top-k 平局处理

## 1. 本讲目标

上一讲（u2-l1）我们走通了 `moba_attn_varlen_naive` 的五步流水线。本讲放慢镜头，专攻其中最容易一眼扫过、却暗藏玄机的一段——**从 gate 打分矩阵到最终块选择掩码 `need_attend`**。读完本讲，你应该能够：

1. 逐行解释「当前块 `+inf`、未来块 `-inf`」这条因果修正规则的三段区间语义，以及为什么两条赋值语句的书写顺序不可交换。
2. 说清楚阈值筛选法（`gate >= gate_top_k_val`）在两种 corner case 下会出错：**有限值平局导致多选**、**`-inf` 阈值导致全选**。
3. 解释 `scatter_` 构造的 `gate_idx_mask` 如何把选择结果精确收敛到「恰好 k 个块」，以及为什么 token 级下三角掩码（`tril`）能兜住 `-inf` 阈值情形的正确性。
4. 动手把 `need_attend` 掩码导出并用 matplotlib 画成热力图，肉眼观察 chunk_size / topk 对块选择模式的影响，并识别图中的「虚假选中」像素。

## 2. 前置知识

### 2.1 承接 u2-l1：我们在流水线的哪一步

回忆 [moba/moba_naive.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py) 的五步流水线：分块求 `key_gate_weight` → 算 gate → 因果修正与 top-k 选块 → 掩码展开到 token 级 → 带掩码 softmax 注意力。本讲的战场是第 3、4 步，即函数中 L58-L81 这二十来行代码。gate 是形状为 `[H, S, N]` 的打分矩阵（H 个头、S 个 query 位置、N 个 KV 块），它回答的问题是：**「第 s 个 query 应该注意哪些块？」**

### 2.2 IEEE 754 中 `±inf` 的比较规则

上一讲已经铺垫过，这里给出精确规则表，本讲会反复用到：

| 比较 | 结果 | 在本讲中的含义 |
| --- | --- | --- |
| `+inf > 任何有限值` | True | 当前块在 top-k 中永远是最大值，必然入选 |
| `-inf < 任何有限值` | True | 未来块在正常情况下永远竞争不过过去块 |
| `-inf >= -inf` | True | **陷阱**：阈值恰为 `-inf` 时，`>=` 筛选会放过所有 `-inf` 块 |
| `topk(largest=True)` | 把 `+inf` 排最前、`-inf` 排最后 | 因果性主要靠这条规则「免费」获得 |

### 2.3 `torch.topk` 与 `scatter_` 的最小知识

- `torch.topk(x, k, dim=-1, largest=True)` 返回 `(values, indices)`：前 k 大的值和它们的下标。**下标互不相同、个数恒为 k**；但当有并列值时，「并列者中具体选中哪几个下标」文档并未保证，属于实现细节。
- `Tensor.scatter_(dim, index, value)`：沿 `dim` 维，把 `index` 中给出的位置写成 `value`。本讲中用它把 top-k 下标「抄」成一个布尔掩码——即一份**精确名单**。
- 布尔掩码高级索引：`gate[need_attend] = 0` 把 True 位置赋 0，`gate[~need_attend] = -float("inf")` 把 False 位置赋 `-inf`，合起来把打分矩阵改写成 0/-inf 的**加性掩码**。

### 2.4 可视化工具

matplotlib 的 `imshow` 可以把二维布尔矩阵画成热力图（True=深色、False=浅色），`origin="lower"` 让纵轴从下往上。本讲第 4.4 节会给出完整脚本。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [moba/moba_naive.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py) | 教学用 naive 实现 | L58-L81：因果修正、top-k、`gate_idx_mask`、掩码展开 |
| [README.md](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/README.md) | 项目说明 | L60-L62：官方明确说 naive 实现「可保存并可视化注意力掩码来观察块选择过程」 |
| [tests/test_moba_attn.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py) | 正确性测试 | L37-L42 参数化网格、L83-L88 容忍度断言：解释「恰好 k 个」为何重要 |

## 4. 核心概念与源码讲解

### 4.1 gate 因果修正：当前块 `+inf` 与未来块 `-inf`

#### 4.1.1 概念说明

gate 的原始分数来自 `einsum("shd,nhd->hsn", q_, key_gate_weight)`（见 [moba/moba_naive.py:53-55](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L53-L55)），它只反映「query 与块代表向量的相似度」，**完全不知道因果性**：如果不加处理，第 10 个 token 可能选中只包含第 500 个 token 的块——这在自回归语言模型里属于「偷看未来」，是绝对禁止的。

MoBA 在这里的修正是两句话：

1. **因果性**：query 位置 s 看不到块起点在 s 之后的块——把这些位置的分数设为 `-inf`，使它们在 top-k 竞选中天然落败。
2. **当前块必选**：query 位置 s 所在的块（第 \(b = \lfloor s / c \rfloor\) 块，c 为 chunk_size）分数设为 `+inf`，保证它无论如何都排进 top-k。这是 MoBA 的设计决策：**自己的块永远可见**，同时保证每个 query 的注意力至少有一个「保底」的注意范围（自己所在的块），从根源上避免出现整行 `-inf`、softmax 产生 NaN 的可能。

#### 4.1.2 核心流程

对块 i 的那一「列」gate 分数（`gate[:, :, i]`）做两段写入后，按 query 位置分成三段：

| query 位置 s 所在区间 | 块 i 的 gate 分数 | 语义 |
| --- | --- | --- |
| \(s < i \cdot c\) | `-inf` | 块 i 全部在 s 的未来 → 不可选 |
| \(i \cdot c \le s < (i+1) \cdot c\) | `+inf` | 块 i 是 s 的当前块 → 必选 |
| \(s \ge (i+1) \cdot c\) | 保留原始分数 | 块 i 是 s 的过去块 → 凭分数竞选 top-k |

注意两段写入是**有重叠**的：第一段先把 `[0, (i+1)·c)` 全部压成 `-inf`，第二段再把其中 `[i·c, (i+1)·c)` 抬成 `+inf`。**顺序不可交换**——若先写 `+inf` 再写 `-inf`，当前块会被错误地压成 `-inf`。

#### 4.1.3 源码精读

```python
for i in range(num_block):
    # select the future Qs that can attend to KV chunk i
    gate[:, : (i + 1) * moba_chunk_size, i] = float("-inf")
    gate[:, i * moba_chunk_size : (i + 1) * moba_chunk_size, i] = float("inf")
```

这是 [moba/moba_naive.py:58-61](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L58-L61)，逐行说明：

- 第 60 行：对块 i 这一列，把 query 下标小于 `(i+1)·chunk` 的分数全部写成 `-inf`。源码注释里的 "future Qs" 读起来容易反直觉，其含义是「把块 i 视为未来信息的那些 query」，即位于块 i 之前的 query。
- 第 61 行：紧跟着把当前块区间 `[i·chunk, (i+1)·chunk)` 覆写成 `+inf`。由于它执行在后，当前块最终是 `+inf` 而不是 `-inf`。
- 循环对每一列（每个块）独立执行一遍，复杂度 \(O(N)\) 次切片写入，N 为块数。

顺带留意一个细节：这段修正发生在 gate 已被转为 fp32 之后（[moba/moba_naive.py:52-54](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L52-L54)），所以 `±inf` 写入的是 fp32 的 IEEE 754 无穷值，语义精确，不受 bf16 精度影响。

#### 4.1.4 代码实践

**实践目标**：脱离完整注意力函数，单独验证三段区间表格。

操作步骤（示例代码，非仓库自带；纯 CPU 可运行）：

```python
import torch

S, chunk = 8, 4
num_block = (S + chunk - 1) // chunk  # = 2
gate = torch.arange(100., 108.).reshape(1, S, 1).repeat(1, 1, num_block)  # 原始分数 100..107

for i in range(num_block):
    gate[:, : (i + 1) * chunk, i] = float("-inf")
    gate[:, i * chunk : (i + 1) * chunk, i] = float("inf")

print(gate[0])
```

需要观察的现象：

- 第 0 列（块 0）：`[inf]*4 + [104,105,106,107]`——query 0-3 的当前块是 `+inf`，query 4-7 把块 0 当过去块，保留原始分数。
- 第 1 列（块 1）：`[-inf]*4 + [inf]*4`——query 0-3 看不到块 1（未来），query 4-7 的当前块是块 1。

预期结果：与 4.1.2 的表格逐格一致。再把两行赋值语句交换顺序运行一次，观察第 0 列变成 `[-inf]*4 + [...]`，当前块被错误屏蔽——直观体会书写顺序的重要性。

#### 4.1.5 小练习与答案

**练习 1**：设 `chunk_size=3`、序列长 8，query s=5 的当前块是哪一块？它能凭「原始分数」竞选的块有哪些？

答案：\(b = \lfloor 5/3 \rfloor = 1\)（覆盖位置 3-5）。块 1 是 `+inf` 必选；块 2 覆盖位置 6-8，起点 6 > 5，是未来块，`-inf` 不可选；只有块 0（位置 0-2）凭原始分数竞选。所以 s=5 的可选块集合 = {块 0（凭分数），块 1（必选）}，共 \(b+1 = 2\) 个。

**练习 2**：为什么「当前块必选」能防止整行 `-inf` 导致的 NaN？

答案：query s 所在块 b 恒为 `+inf`，是全行最大值，top-k（largest=True）必然选中它；掩码展开后 token 级至少保留 s 自己（`tril` 对角线），因此 softmax 每行的分母至少有一个非 `-inf` 项对应的 \(e^0 = 1\)，不会出现 0/0。

**练习 3**：如果把「未来块 `-inf`」改成「未来块乘以一个大负数（如 -1e4）」，会有什么风险？

答案：过去块的原始分数如果比 -1e4 还小（fp32 下完全可能，内积量级随 head_dim 增长），top-k 可能选 future 块而不是 past 块，破坏因果性。`-inf` 是 IEEE 754 中严格小于一切有限值的哨兵值，不存在被「压过」的问题，这是源码选择 `±inf` 而非大数的原因。

### 4.2 gate_top_k_val 最小值筛选：阈值法的两个坑

#### 4.2.1 概念说明

有了修正后的 gate，接下来要「每行选出最大的 k 个块」。源码采用的是**阈值法**：先 `topk` 拿到入选的 k 个值，取其中的最小值作为门槛 `gate_top_k_val`（直觉上就是「第 k 大的值」），再用 `gate >= 门槛` 筛出所有过线者。

这个方法简洁，但有两个 corner case：

- **坑 1：有限值平局**。若第 k 大的值出现并列（多个块分数完全相等），`>=` 会把并列者全部放过，选出**多于 k 个**块。这会真正改变注意力结果——多选的块也参与了 softmax——偏离「每个 query 恰好注意 k 个块」的 MoBA 语义。
- **坑 2：`-inf` 阈值**。位于块 b 的 query，因果可选块只有 \(b+1\) 个；若 \(k > b+1\)，`topk` 被迫把 `-inf` 的未来块也选进来凑数，门槛变成 `-inf`，而 `-inf >= -inf` 为 True，于是 `>=` 把**所有**块（包括未来块）都标成可注意。

坑 2 并非杞人忧天：测试参数化网格（[tests/test_moba_attn.py:39-42](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L39-L42)）里就有 `seqlen=512, moba_chunk_size=128, moba_topk=4` 的组合——此时块 0 的每个 query 可选块只有 1 个，远小于 k=4，坑 2 在**每次测试运行中都会真实发生**。

#### 4.2.2 核心流程

```text
gate [H, S, N]（已含 ±inf 修正）
   │ torch.topk(k = min(moba_topk, N), largest=True)
   ├── gate_top_k_val  [H, S, K]   # 入选的 k 个值
   └── gate_top_k_idx  [H, S, K]   # 它们的下标（精确名单）
   │ gate_top_k_val.min(dim=-1)
   ▼
门槛 [H, S]
   │ gate >= 门槛.unsqueeze(-1)
   ▼
need_attend [H, S, N]  ← 坑 1、坑 2 在这一步暴露
```

其中 \(k = \min(\text{moba\_topk}, N)\) 的 `min` 防止块总数不足 topk 时 `topk` 报错——此时退化为全选。

#### 4.2.3 源码精读

```python
# gate_top_k_idx = gate_top_k_val = [ H S K ]
gate_top_k_val, gate_top_k_idx = torch.topk(
    gate, k=min(moba_topk, num_block), dim=-1, largest=True, sorted=False
)
gate_top_k_val, _ = gate_top_k_val.min(dim=-1)  # [ H, S ]
need_attend = gate >= gate_top_k_val.unsqueeze(-1)
```

这是 [moba/moba_naive.py:62-67](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L62-L67)。四行分别做：取 top-k 的值与下标（`sorted=False` 省去排序，因为后续只需要最小值和下标集合）；对 k 个值取 `min` 得到门槛（`[H,S,K] → [H,S]`）；广播比较得到布尔掩码 `need_attend`。注意 `topk` 沿 `dim=-1`（块维度）进行，**每个头独立选块**——不同头可以为同一个 query 选择完全不同的块集合，这正是 MoE「专家路由」的分散性。

#### 4.2.4 代码实践

**实践目标**：在最小例子里亲手复现两个坑（示例代码，非仓库自带；纯 CPU 可运行）。

```python
import torch

# 坑 1：有限值平局 → 多选
g = torch.tensor([[3., 3., 3., 1.]])
val, idx = torch.topk(g, k=2, dim=-1)
thr = val.min(dim=-1).values
print(g >= thr)   # 预期 [True, True, True, False] —— 3 个过线，超过 k=2

# 坑 2：-inf 阈值 → 全选
g = torch.tensor([[float("inf"), float("-inf"), float("-inf")]])
val, idx = torch.topk(g, k=3, dim=-1)
thr = val.min(dim=-1).values          # = -inf
print(g >= thr)                       # 预期 [True, True, True] —— 未来块也被放过
```

需要观察的现象：

- 坑 1 中 `g >= thr` 有 3 个 True，而 topk 的名额只有 2 个。
- 坑 2 中门槛是 `-inf`，连 `-inf` 的块都满足 `>= -inf`，筛选完全失效。
- `idx` 的具体取值（平局时选中哪几个并列者）由 topk 的实现决定，文档未保证，待本地验证；但 `idx` 的形状恒为 `[1, 2]` / `[1, 3]`、元素互不相同，这是确定的。

预期结果：如上两条注释。若把 `>=` 改成 `>`，坑 1 变成「漏选并列的第 k 名」——同样错误，说明比较符的选择救不了阈值法，必须引入下一节的精确名单。

#### 4.2.5 小练习与答案

**练习 1**：seqlen=512、chunk_size=128、topk=4 时，哪些 query 会触发坑 2？

答案：块 0 的全部 query（位置 0-127）。它们 \(b+1 = 1 < k = 4\)。块 1 的 query（128-255）有 \(b+1 = 2 < 4\)，同样触发；块 2 的 query（256-383）\(b+1 = 3 < 4\)，也触发；只有块 3 的 query（384-511）\(b+1 = 4 = k\)，门槛是有限值，不触发。

**练习 2**：为什么说坑 1 会改变注意力输出，而坑 2「侥幸」不会？（提示：想想后续的 token 级 `tril`）

答案：坑 1 多选的是**过去**的块，`tril` 不会屏蔽它们的 token，softmax 真的多算了这些块，输出改变。坑 2 多选的是**未来**块，而未来块的所有 token 位置都大于 query 位置 s，会在 token 级被 `tril` 全部压成 `-inf`（见 4.3 节），对 softmax 的贡献恰好为零，输出不变。这也是为什么测试中坑 2 天天发生却没人察觉——它只污染「块级掩码」这一中间产物，不污染最终结果。

**练习 3**：`min(moba_topk, num_block)` 里如果去掉 `min`，什么配置会崩？

答案：当 `moba_topk > num_block` 时（例如 seqlen=300、chunk_size=128 → num_block=3，topk=4），`torch.topk` 会因 k 超过维度大小直接抛出运行时错误。`min` 让这种配置退化为「全选所有块」，配合 chunk_size ≥ 序列长的情形即退化为全量因果注意力（u2-l1 的结论）。

### 4.3 gate_idx_mask：`scatter_` 精确名单兜底

#### 4.3.1 概念说明

阈值法的病根是「用值反推名单」——值可能并列。而 `topk` 返回的 `gate_top_k_idx` 本身就是一份**互不相同的 k 个下标**的精确名单。`gate_idx_mask` 的思路就是：把这份名单用 `scatter_` 抄成一个布尔矩阵，再与阈值结果求交集，把选择结果**硬性收敛到恰好 k 个**。

源码注释写得很直白："add gate_idx_mask in case of there is cornercases of same topk val been selected"（防止相同 topk 值被重复选中的 corner case）。

#### 4.3.2 核心流程

```text
gate_top_k_idx [H, S, K] （topk 的精确下标）
   │ zeros([H,S,N], bool).scatter_(dim=-1, index=idx, value=True)
   ▼
gate_idx_mask [H, S, N]   # 每行恰好 K 个 True
   │ need_attend = logical_and(阈值掩码, 名单掩码)
   ▼
need_attend [H, S, N]     # 恒等于名单掩码：恰好 K 个 True
   │ gate[need_attend]=0, gate[~need_attend]=-inf
   ▼
0/-inf 加性掩码（块级）
```

一个值得证明的事实（见练习 3）：阈值掩码永远是名单掩码的**超集**——topk 返回的每个值都 ≥ 它们自身的最小值（即门槛），所以名单里的下标在阈值掩码中必为 True。因此交集恒等于名单掩码本身。换句话说，从纯逻辑上讲只保留 `scatter_` 名单一步结果完全等价；保留「阈值 + 交集」两步是防御式写法，也让代码意图（「按值筛选、再按名单校正」）更贴近作者思路。

#### 4.3.3 源码精读

```python
# add gate_idx_mask in case of there is cornercases of same topk val been selected
gate_idx_mask = torch.zeros(
    need_attend.shape, dtype=torch.bool, device=q.device
)
gate_idx_mask = gate_idx_mask.scatter_(dim=-1, index=gate_top_k_idx, value=True)
need_attend = torch.logical_and(need_attend, gate_idx_mask)
```

这是 [moba/moba_naive.py:68-73](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L68-L73)。要点：

- `scatter_` 要求 `index` 与目标张量同维数、除 `dim` 外形状相同（`[H,S,K]` 对 `[H,S,N]`，沿最后一维散射），且 dtype 为 int64——`topk` 返回的下标天然满足。
- `scatter_` 是原地操作并返回自身，所以第 72 行的赋值是风格性写法。
- 交集之后，`need_attend` 每行恰好 k 个 True。这一性质是与高效实现对齐的**硬要求**：`moba_efficient` 的块选择恰好是 k 个（u3 系列会看到），测试用 `torch.allclose`（[tests/test_moba_attn.py:85-88](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L85-L88)）逐元素比对两者输出，naive 若在平局时多算块，误差会直接体现为断言失败。

紧接着的两行把布尔选择改写为加性掩码（[moba/moba_naive.py:74-75](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L74-L75)）：`gate[need_attend] = 0`、`gate[~need_attend] = -float("inf")`。再经 `repeat_interleave` 从块级展开到 token 级并截断到序列长（[moba/moba_naive.py:76-78](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L76-L78)），最后叠加 token 级因果下三角：

```python
gate.masked_fill_(
    torch.ones_like(gate, dtype=torch.bool).tril().logical_not(), -float("inf")
)
```

这是 [moba/moba_naive.py:79-81](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L79-L81)。`ones_like(...).tril()` 是下三角为 True 的矩阵，取 `logical_not()` 得到**严格上三角**（j > s 的位置），将这些位置填 `-inf`。它处理的是块内因果——当前块虽然必选，但块内排在 query 之后的 token 依然不可见。**正是这一步兜住了坑 2**：块级掩码错选的未来块，其 token 全部位于上三角，被强制屏蔽，对 softmax 贡献为零。

#### 4.3.4 代码实践

**实践目标**：验证「交集结果恒等于 scatter 名单」（示例代码，非仓库自带；纯 CPU 可运行）。

```python
import torch

torch.manual_seed(0)
for trial in range(1000):
    N, k = 6, 3
    if trial % 2 == 0:
        g = torch.randn(1, N)                 # 随机分数，几乎无平局
    else:
        g = torch.randint(0, 2, (1, N)).float()  # 只有 0/1 两种值，平局大量出现
        g[0, torch.randint(0, N, (1,)).item()] = float("inf")  # 模拟当前块
    val, idx = torch.topk(g, k=k, dim=-1)
    thr = val.min(dim=-1).values
    thresh_mask = g >= thr.unsqueeze(-1)
    idx_mask = torch.zeros_like(thresh_mask).scatter_(-1, idx, True)
    assert torch.equal(torch.logical_and(thresh_mask, idx_mask), idx_mask), trial
    assert idx_mask.sum() == k
print("1000 次试验全部通过：交集 == 名单，且每行恰好 k 个 True")
```

需要观察的现象：无论分数是连续随机还是大量平局（甚至含 `inf`），断言均不触发；`thresh_mask` 的 True 个数在平局 trial 中经常大于 k。

预期结果：打印最终通过信息。若把断言换成 `torch.equal(thresh_mask, idx_mask)`，平局 trial 会失败——直观展示「阈值掩码是超集」。

#### 4.3.5 小练习与答案

**练习 1**：`scatter_` 为什么能保证「每行恰好 k 个 True」？

答案：`topk` 返回的下标在同一行内互不相同（同一下标不可能被选两次），`scatter_` 把这 k 个不同位置各写一次 True，其余位置保持初始 False，因此每行 True 的个数恰为 k。

**练习 2**：如果不加 `gate_idx_mask`（只用阈值掩码），坑 2 会让最终注意力输出算错吗？

答案：不会。坑 2 错选的都是未来块，token 级 `tril` 会把它们的全部 token 压成 `-inf`，softmax 贡献为零（见 4.2 练习 2）。但如果有人基于 `need_attend` 做下游优化——例如「只对选中的块调度计算」——坑 2 就会白算未来块、坑 1 会多算块，`gate_idx_mask` 把掩码收敛到精确名单后这类优化才是安全的。

**练习 3**：证明「阈值掩码 ⊇ 名单掩码」。

答案：设某行 topk 返回值集合 \(V = \{v_1, \dots, v_k\}\)，门槛 \(t = \min_j v_j\)。名单中的任一下标对应某个 \(v_i \in V\)，有 \(v_i \ge \min_j v_j = t\)，即 `gate[i] >= t` 成立，故该下标在阈值掩码中为 True。因此名单掩码的每个 True 都同时是阈值掩码的 True，超集关系成立，交集等于名单掩码。

### 4.4 掩码可视化方法：把 need_attend 画出来

#### 4.4.1 概念说明

README 在 Implementation Details 一节明确写道：naive 实现 "designed to help understand how MoBA selects corresponding chunks. You may save and visualize the attention masks to see the block selection process"（见 [README.md:60-62](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/README.md#L60-L62)）——**可视化块选择过程是 naive 实现存在的官方理由之一**。论文的 `figures/running_example.png`（README 第 17 行引用）展示的就是这种块选择示意。

有两个层面的掩码可以可视化：

| 层面 | 形状 | 含义 | 观察点 |
| --- | --- | --- | --- |
| 块级 `need_attend` | `[H, S, N]` | 每个 query 选中哪些块 | 阶梯对角带、上三角空洞、坑 2 的虚假像素 |
| token 级 gate 掩码 | `[H, S, S]` | 展开加 `tril` 后每个 query 选中哪些 token | 行内只保留 ≤s 且属于选中块的列 |

块级图信息密度高、最能说明问题，是本讲的主角；token 级图用于验证 4.3 的「tril 兜底」结论。

#### 4.4.2 核心流程

```text
复制 moba_attn_varlen_naive 到独立脚本
   │ 在 need_attend = logical_and(...) 之后插桩：masks.append(need_attend.cpu())
   │ 在 tril 之后插桩：token_masks.append(gate.cpu())（可选）
   ▼
batch=1 构造数据（纯 CPU，fp32）
   ▼
m = masks[0][head]           # [S, N]
   ▼
plt.imshow(m.T, origin="lower") + 轴标签 + savefig
```

为什么用「复制改造」而不是直接调用：原函数不返回中间量，而本讲的角色约束是不修改源码，把函数体抄进脚本加两行插桩是最小侵入的做法（u2-l1 的实践也是这么做的）。

#### 4.4.3 源码精读

插桩点选取依据（关键代码位置回顾）：

- 块级掩码在 [moba/moba_naive.py:73](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L73) 的 `need_attend = torch.logical_and(...)` 之后定型——这是插桩的第一处。
- 随后 [moba/moba_naive.py:74-75](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L74-L75) 把它改写成 0/-inf，原始布尔信息将被覆盖，所以必须在改写前留存。
- token 级掩码在 [moba/moba_naive.py:76-81](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L76-L81) 的 `repeat_interleave` + `masked_fill_` 之后定型——这是插桩的第二处。

#### 4.4.4 代码实践

**实践目标**：画出 head 0 的块选择热力图（横轴 query 位置、纵轴块编号），识别三种图案。

操作步骤（示例代码，非仓库自带；纯 CPU 可运行，需 `pip install matplotlib`）：

```python
# viz_moba_mask.py —— 复制自 moba/moba_naive.py 的 moba_attn_varlen_naive，
# 仅增加两处插桩（标有 "<-- 插桩"），其余逐行保持一致。
import math
import torch
import matplotlib.pyplot as plt


def moba_naive_with_mask(q, k, v, cu_seqlens, max_seqlen, moba_chunk_size, moba_topk):
    batch = cu_seqlens.numel() - 1
    masks, token_masks = [], []                     # <-- 插桩：收集中间掩码
    o = torch.zeros_like(q)
    for batch_idx in range(batch):
        batch_start = cu_seqlens[batch_idx].item()
        batch_end = cu_seqlens[batch_idx + 1].item()
        q_, k_, v_ = q[batch_start:batch_end], k[batch_start:batch_end], v[batch_start:batch_end]
        o_ = o[batch_start:batch_end]
        batch_size = batch_end - batch_start
        num_block = math.ceil(batch_size / moba_chunk_size)
        key_gate_weight = torch.cat(
            [k_[i * moba_chunk_size: min(batch_size, (i + 1) * moba_chunk_size)]
             .mean(dim=0, keepdim=True) for i in range(num_block)], dim=0)
        q_ = q_.type(torch.float32)
        key_gate_weight = key_gate_weight.type(torch.float32)
        gate = torch.einsum("shd,nhd->hsn", q_, key_gate_weight)
        for i in range(num_block):
            gate[:, : (i + 1) * moba_chunk_size, i] = float("-inf")
            gate[:, i * moba_chunk_size: (i + 1) * moba_chunk_size, i] = float("inf")
        gate_top_k_val, gate_top_k_idx = torch.topk(
            gate, k=min(moba_topk, num_block), dim=-1, largest=True, sorted=False)
        gate_top_k_val, _ = gate_top_k_val.min(dim=-1)
        need_attend = gate >= gate_top_k_val.unsqueeze(-1)
        gate_idx_mask = torch.zeros(need_attend.shape, dtype=torch.bool, device=q.device)
        gate_idx_mask = gate_idx_mask.scatter_(dim=-1, index=gate_top_k_idx, value=True)
        need_attend = torch.logical_and(need_attend, gate_idx_mask)
        masks.append(need_attend.detach().cpu())    # <-- 插桩 1：块级掩码
        gate[need_attend] = 0
        gate[~need_attend] = -float("inf")
        gate = gate.repeat_interleave(moba_chunk_size, dim=-1)[:, :, :batch_size]
        gate.masked_fill_(torch.ones_like(gate, dtype=torch.bool).tril().logical_not(), -float("inf"))
        token_masks.append(gate.detach().cpu())     # <-- 插桩 2：token 级掩码
        q_ = q_.type(torch.float32); k_ = k_.type(torch.float32); v_ = v_.type(torch.float32)
        qk = torch.einsum("xhd,yhd->hxy", q_, k_)
        qk += gate.to(q_.device)
        qk *= q.shape[-1] ** (-0.5)
        o_ += torch.einsum("hxy,yhd->xhd", qk.softmax(dim=-1), v_)
        o = o.type_as(q)
    return o, masks, token_masks


if __name__ == "__main__":
    S, H, D = 512, 2, 128
    chunk, topk = 128, 3
    torch.manual_seed(0)
    q = torch.randn(S, H, D); k = torch.randn(S, H, D); v = torch.randn(S, H, D)
    cu = torch.tensor([0, S], dtype=torch.int32)

    o, masks, token_masks = moba_naive_with_mask(q, k, v, cu, S, chunk, topk)
    m = masks[0][0]                                  # head 0，[S, N]
    print("每行选中块数 =", m.sum(-1).unique().tolist())   # 预期恒为 [3]

    plt.figure(figsize=(7, 3))
    plt.imshow(m.T, aspect="auto", cmap="Blues", origin="lower")
    plt.xlabel("query 位置 s"); plt.ylabel("块编号 n")
    plt.title(f"need_attend[head=0], chunk={chunk}, topk={topk}")
    plt.colorbar(label="选中=1")
    plt.tight_layout(); plt.savefig("moba_block_mask.png", dpi=150)
```

需要观察的现象（对照热力图）：

1. **阶梯状对角带**：query s 所在的块 \(\lfloor s/c \rfloor\) 恒为 True——因为当前块被强制 `+inf` 必选（4.1）。这就是「对角线附近总是被选中」的原因。
2. **右上三角空洞**：块编号大于 query 所在块的格子基本为 False——未来块 `-inf`（4.1）。
3. **左上角的虚假像素**：块 0、块 1 的 query 行里会出现选中未来块的 True 像素（本配置下 topk=3 > 块 0 query 的 1 个因果块，坑 2 触发）。它们不影响最终注意力——可另存 `token_masks[0][0]` 并检查任意行 s 的非 `-inf` 列恰好是 `0..s` 中属于因果选中块的部分来验证（待本地验证具体像素位置，取决于 topk 平局打破规则）。

预期结果：`每行选中块数` 打印 `[3]`（= min(topk, num_block)，`gate_idx_mask` 的功劳）；图上呈现上述三种图案。注意 naive 实现不依赖 flash-attn，CPU 即可运行；若 matplotlib 不在环境中，先 `pip install matplotlib`。

#### 4.4.5 小练习与答案

**练习 1**：把 `topk` 从 3 改成 4（其余不变），`m.sum(-1).unique()` 会输出什么？图中最大的变化是什么？

答案：输出 `[4]`。图中每行多一个 True；同时坑 2 的触发范围扩大——块 2 的 query（\(b+1=3<4\)）也出现虚假像素，左上角「实心」区域更高。

**练习 2**：把 `chunk` 从 128 改成 512（≥ S），会发生什么？

答案：num_block=1，`k = min(topk, 1) = 1`，热力图退化为单列全 True——MoBA 退化为全量因果注意力（与 u2-l1 的结论呼应：chunk_size 不小于序列长时失去稀疏性）。

**练习 3**：为什么可视化要选在 `logical_and` 之后、`gate[need_attend] = 0` 之前插桩？

答案：`logical_and` 之后 `need_attend` 才是最终的块选择（恰好 k 个）；而 `gate[need_attend] = 0` 会立即把布尔信息改写成数值掩码，布尔矩阵本体并没有被存下来，且后续 `repeat_interleave` 还会改变形状。此插桩点捕获的是语义最完整、形状最干净 `[H, S, N]` 的中间产物。

## 5. 综合实践

**任务：一张参数网格图 + 一次「tril 兜底」验证**，把本讲四个模块串起来。

1. **准备**：运行 4.4.4 的脚本确认基线图无异常。
2. **参数扫描**：对 `(chunk, topk) ∈ {(128,2), (128,4), (256,2), (256,4), (512,1)}` 循环调用 `moba_naive_with_mask`，把五张热力图画成 `2×3` 子图（`plt.subplots`），统一色标。记录三件事：每行 True 数（应恒等于 `min(topk, num_block)`）、对角带宽度（= chunk）、虚假像素区域的高度（= 块编号 < topk-1 的 query 范围）。
3. **解释对角线**：在图旁写一段话，说明对角带来自 4.1 的「当前块 `+inf` 必选」，它同时是防止整行 `-inf` NaN 的保底机制。
4. **验证 tril 兜底**：取 `(128, 4)` 那组，检查 `token_masks[0][0]`（token 级 0/-inf 掩码）第 10 行：非 `-inf` 的列应恰好是 0..10，即使块级掩码为该行选中了未来块。再检查任意一行 s，非 `-inf` 列数应等于 s+1 与「因果选中块覆盖范围」的交集大小。
5. **对照测试**：打开 [tests/test_moba_attn.py:37-42](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L37-L42)，找出参数网格中哪些组合一定触发坑 2（判据：`moba_topk > 块编号+1` 对某些 query 成立，即 `moba_topk ≥ 2` 且序列内存在第 0 块时全部触发），体会「测试全绿」与「中间掩码失真」并存的原因。

预期成果：五张热力图 + 一段对角线解释 + 第 10 行 token 掩码的验证输出。全程 CPU 可完成。

## 6. 本讲小结

- 因果修正（[L58-61](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L58-L61)）把每列 gate 分成三段：未来块 `-inf` 不可选、当前块 `+inf` 必选、过去块凭原始分数竞选；两条赋值的书写顺序不可交换。
- 阈值筛选（[L62-67](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L62-L67)）有两个坑：有限值平局会选出多于 k 个块（改变输出）；`k > b+1` 时门槛退化为 `-inf`、`-inf >= -inf` 放行所有块（测试网格中真实发生）。
- `gate_idx_mask`（[L68-73](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L68-L73)）用 `scatter_` 把 topk 下标抄成精确名单，交集后选择恒为「恰好 k 个」；阈值掩码恒为名单的超集。
- token 级 `tril`（[L79-81](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L79-L81)）兜住坑 2：错选的未来块 token 全在上三角，softmax 贡献为零，最终输出依然正确。
- 「恰好 k 个」是与高效实现对齐并通过 `torch.allclose` 测试的硬约束；可视化时看到的虚假选中像素是坑 2 的直接证据，不是 bug。

## 7. 下一步学习建议

下一讲进入第三单元：**u3-l1「moba_attn_varlen 总览」**，看高效实现如何用纯向量化操作（无 Python 循环、无 `±inf` 哨兵）实现与本讲相同的 gate 语义——特别是 `gate_chunk_end_mask` 与 `gate_batch_end_mask` 如何用一次广播比较替代本讲的逐列写入。之后 **u3-l3「gate 的向量化计算与跨 batch 因果掩码」** 会逐行对照两版 gate 实现，届时建议把本讲 4.4 的可视化脚本改成同时打印两版的 `need_attend`，验证它们逐格一致。若想先巩固本讲，可重做 4.2 练习 1 的「触发范围」分析，推广到 varlen 多 batch 情形（提示：每个 batch 独立分块、独立计数 `b+1`）。
