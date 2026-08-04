# 剪枝算法 SparseGPT / Wanda / Magnitude / REAP

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出「剪枝（pruning）」与「量化（quantization）」的根本区别，并理解稀疏度（sparsity）、结构化/非结构化、N:M 掩码等基本术语。
- 看懂 `SparsityModifierBase` 这条 SparseGPT/Wanda 共享的生命周期骨架，知道「挂 hook 累积统计 → `on_sequential_epoch_end` 改权重 → 卸 hook」三步是怎么串起来的。
- 区分 Wanda（按激活幅度打分）与 SparseGPT（用 Hessian 做最优误差补偿）在「决定剪谁」上的数学差异。
- 理解 `MagnitudePruningModifier` 走的是另一套「训练事件链 + 稀疏度调度器」的无数据剪枝。
- 理解 `REAPPruningModifier` 如何用「路由权重 × 专家输出范数」的 saliency 对 MoE 模型做结构化专家剪枝。

本讲承接 [u2-l3 Modifier 基类生命周期](u2-l3-modifier-base-lifecycle.md)：所有剪枝 modifier 都是 `Modifier` 的子类，复用同一套 `on_initialize` / 校准链 / 训练链钩子。如果你对「校准链（`on_calibration_start` → `on_sequential_epoch_end` → `on_calibration_end`）」和「训练链（`on_start` → `on_update` → `on_end`）」两条互斥链还不熟，请先读 u2-l3。

## 2. 前置知识

### 剪枝 vs 量化

前面几讲讲的 GPTQ/AWQ/SmoothQuant 都是**量化**：用更少的位数（int4/fp8）表示权重，权重依然稠密。**剪枝**则是把一部分权重**直接置零**（甚至整列整块移除），让权重变「稀疏」。量化的产物是低位张量，剪枝的产物是含大量零的张量（或更小的模块）。

### 稀疏度 sparsity

稀疏度指「零元素占比」。

\[ \text{sparsity} = \frac{\#\text{零元素}}{\#\text{总元素}} \]

`sparsity=0.5` 表示把一半权重置零。

### 非结构化 vs 结构化 vs N:M

- **非结构化（unstructured）**：在整张权重里任意位置置零，对应 `mask_structure="0:0"`。零值分布随机，压缩比好但硬件加速需要专门稀疏内核。
- **N:M 结构化**：在每连续 \(M\) 个权重里只保留 \(N\) 个非零（其余置零）。最常见的是 `2:4`：每 4 个里留 2 个，正好契合 NVIDIA Ampere+ 的稀疏张量核。
- **MoE 专家级结构化**：直接删掉整个专家模块（REAP），属于「块级/模块级」结构化。

llm-compressor 的剪枝 modifier 通过 `mask_structure` 字段控制：值为 `"N:M"` 字符串，`0:0` 即非结构化。

### saliency（显著性）

「剪谁」需要一个打分标准——**越不重要的权重越该剪**。不同算法的区别几乎全在这个打分函数上：

- Magnitude：\(\text{score}=|w|\)，越小越剪（纯看权重大小）。
- Wanda：\(\text{score}=|w|\cdot \sqrt{s}\)，\(s\) 是输入激活的列范数平方（权重小 **且** 激活小的才剪）。
- SparseGPT：剪掉后用 Hessian 逆矩阵把误差最优地补偿到剩余权重，使输出变化最小。
- REAP：\(\text{score}_j = \text{mean}(g_j\cdot \|f_j\|_2)\)，对「专家」打分而非对单个权重。

### 关键约束（来自前置讲义）

本讲不再重复 Modifier 基类细节。只需记住：`on_*` 钩子由生命周期按事件类型分发；`register_hook`/`remove_hooks` 来自 `HooksMixin`，用于在校准时挂载/卸载统计用的 forward hook；`update_offload_parameter` 用于把改好的权重写回模块（兼容磁盘 offload）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/llmcompressor/modifiers/pruning/sparsegpt/sgpt_base.py` | `SparsityModifierBase`：SparseGPT 与 Wanda 共享的骨架，统一字段与生命周期三步。 |
| `src/llmcompressor/modifiers/pruning/sparsegpt/base.py` | `SparseGPTModifier`：累积 Hessian、最优补偿稀疏化。 |
| `src/llmcompressor/modifiers/pruning/sparsegpt/sgpt_sparsify.py` | SparseGPT 的纯算法：`accumulate_hessian` / `sparsify_weight`。 |
| `src/llmcompressor/modifiers/pruning/wanda/base.py` | `WandaPruningModifier`：累积激活列范数、按幅度×范数打分。 |
| `src/llmcompressor/modifiers/pruning/wanda/wanda_sparsify.py` | Wanda 的纯算法：`accumulate_row_scalars` / `sparsify_weight`。 |
| `src/llmcompressor/modifiers/pruning/magnitude/base.py` | `MagnitudePruningModifier`：无数据幅度剪枝，走训练事件链。 |
| `src/llmcompressor/modifiers/pruning/helpers.py` | 稀疏度调度器工厂（linear/cubic/...），服务 Magnitude。 |
| `src/llmcompressor/modifiers/pruning/reap/base.py` | `REAPPruningModifier`：MoE 专家剪枝生命周期。 |
| `src/llmcompressor/modifiers/pruning/reap/utils.py` | REAP 工具：MoE 探测、saliency 累积、结构化裁剪专家。 |

## 4. 核心概念与源码讲解

### 4.1 共享骨架 SparsityModifierBase（SparseGPT / Wanda 的地基）

#### 4.1.1 概念说明

`SparseGPTModifier` 和 `WandaPruningModifier` 的**生命周期完全一样**，区别只在「校准时累积什么统计量」和「最终用什么公式决定剪谁」。于是项目把公共部分抽到抽象基类 `SparsityModifierBase`，它规定了三个钩子点，子类只需实现两个抽象方法：

- `calibrate_module(module, args, _output)`：每个 forward batch 调一次，负责累积统计（Hessian 或激活范数）。
- `compress_modules()`：校准 epoch 结束时调一次，负责用统计量真正改权重。

`SparsityModifierBase` 声明为 `requires_calibration_data = True`，意味着 oneshot 会为它选择 **sequential** 校准管线（回顾 u3-l4/l5：逐子图两遍前向）。

#### 4.1.2 核心流程

```text
on_initialize          推断 sequential_targets、命中目标层、解析 mask_structure → (prune_n, prune_m)
    │
on_calibration_start   对每个命中的可剪层 (Linear/Conv) 注册 forward hook = calibrate_module
    │   (校准管线逐子图前向, 每个 batch 触发 calibrate_module 累积统计)
on_sequential_epoch_end → compress_modules()
    │   对每个已校准模块: 调子类的 sparsify_weight 改权重 → update_offload_parameter 写回
on_calibration_end     remove_hooks()
```

注意一个易错点：基类注释里写的 `on_sequential_batch_end` 已是历史说法，真实代码里改权重发生在 **`on_sequential_epoch_end`**（子图一轮校准完成时），见下方源码。

#### 4.1.3 源码精读

**类定义与共享字段**——`SparsityModifierBase` 继承 `Modifier`，强制需要校准数据：

[src/.../pruning/sparsegpt/sgpt_base.py:21-39](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/sparsegpt/sgpt_base.py#L21-L39) —— 定义共享字段 `sparsity` / `mask_structure` / `targets`（默认 `["Linear"]`）/ `ignore`，并声明 `requires_calibration_data: bool = True`。其中 `sequential_update=False` 已被弃用（设了会告警并强制改回 `True`）。

`mask_structure` 解析成整数对：

[src/.../pruning/sparsegpt/sgpt_base.py:88](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/sparsegpt/sgpt_base.py#L88) —— 在 `model_validator(mode="after")` 里把 `"2:4"` 拆成 `_prune_n=2, _prune_m=4`，`"0:0"` 拆成 `0,0`（非结构化）。

**`on_initialize`**——推断目标层与 sequential 切分点：

[src/.../pruning/sparsegpt/sgpt_base.py:105-142](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/sparsegpt/sgpt_base.py#L105-L142) —— `infer_sequential_targets` 决定子图切分（回顾 u3-l5），`match_named_modules(model, self.targets)` 找出含目标层的模块；若 `sparsity_profile=="owl"` 则跑 OWL（逐层自适应稀疏度）推断。

**`on_calibration_start`**——在命中的可剪子模块上挂校准 hook：

[src/.../pruning/sparsegpt/sgpt_base.py:144-174](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/sparsegpt/sgpt_base.py#L144-L174) —— 关键是遍历 `PRUNABLE_LAYER_TYPES = ["Linear", "Conv1d", "Conv2d", "Conv3d"]`，对每个未在 `ignore` 里的子模块调用 `self.register_hook(module, self.calibrate_module, "forward")`。注意 `lm_head` 不再自动忽略，会打告警提示你显式加进 `ignore`。

**改权重与卸 hook 的两个钩子**：

[src/.../pruning/sparsegpt/sgpt_base.py:176-180](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/sparsegpt/sgpt_base.py#L176-L180) —— `on_sequential_epoch_end` 直接调 `self.compress_modules()`（子类实现），`on_calibration_end` 调 `self.remove_hooks()`。`calibrate_module` 与 `compress_modules` 都是 `@abstractmethod`（见 [L92-103](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/sparsegpt/sgpt_base.py#L92-L103)），由 SparseGPT/Wanda 各自实现。

#### 4.1.4 代码实践

**实践目标**：确认基类的「挂 hook → 改权重 → 卸 hook」三步确实按校准链顺序触发。

**操作步骤**：

1. 打开 `sgpt_base.py`，定位 `on_calibration_start`、`on_sequential_epoch_end`、`on_calibration_end` 三个方法。
2. 用以下「源码阅读型」脚本（**示例代码**，不依赖真实模型）在脑中走一遍事件分发：当 sequential 管线对一个子图完成校准时，lifecycle 会依次发 `CALIBRATION_START` → 多次 batch → `SEQUENTIAL_EPOCH_END` → `CALIBRATION_END`（回顾 u3-l5）。
3. 对照 u2-l3 的 `update_event` 分发逻辑，确认这三个钩子都在「校准链」上。

**需要观察的现象**：`compress_modules` 只在 `SEQUENTIAL_EPOCH_END`（每子图一次）被调用，而不是每个 batch 调用。

**预期结果**：每个可剪模块恰好在它所在子图校准完毕时被稀疏化，保证误差能逐子图传播。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SparsityModifierBase` 要把「改权重」放在 `on_sequential_epoch_end` 而不是 `calibrate_module` 里？

> 参考答案：`calibrate_module` 是 forward hook，每个 batch 触发一次，此时统计量还在累积中、不完整；必须等一个子图的所有校准 batch 跑完（`SEQUENTIAL_EPOCH_END`），统计量才完整，才能一次性算出最优掩码并改权重。

**练习 2**：`mask_structure="2:4"` 会被解析成哪两个私有字段？含义是什么？

> 参考答案：`_prune_n=2, _prune_m=4`，表示每连续 4 个权重里保留 2 个、置零 2 个（2:4 结构化稀疏）。

---

### 4.2 Wanda：按「权重幅度 × 激活范数」打分

#### 4.2.1 概念说明

Wanda（**W**eights **and** activations）来自 [论文 arXiv:2306.11695](https://arxiv.org/abs/2306.11695)。它的核心洞察极其简单：**一个权重重不重要，不只看它自己有多大，还要看喂给它的激活有多大**。如果一个权重的绝对值小，且对应的输入激活也很小，那它对输出的贡献微乎其微，剪掉无损。

Wanda 不需要像 SparseGPT 那样求 Hessian 逆，只需求输入激活每列的 L2 范数平方，因此**比 SparseGPT 快得多**，且在不少任务上效果接近。

#### 4.2.2 核心流程与数学

对每个 `Linear`（权重 \(W\in\mathbb{R}^{\text{out}\times\text{in}}\)），Wanda 的打分为：

\[ W_{\text{metric},ij} = |W_{ij}|\cdot \sqrt{s_j},\qquad s_j = \frac{1}{N}\sum_{t} \|X_{\cdot,j}^{(t)}\|_2^2 \]

其中 \(s_j\) 是输入激活第 \(j\) 列（对应权重第 \(j\) 个输入通道）的范数平方均值，\(N\) 是样本数。然后把每个**输出行**里打分最小的 \(\text{sparsity}\) 比例的权重置零（非结构化），或按 N:M 块选最小的（结构化）。

执行流程：

```text
calibrate_module (forward hook, 每 batch)
    累积 row_scalars s_j（激活列范数平方的增量平均）
on_sequential_epoch_end → compress_modules
    对每个模块: W_metric = |W| * sqrt(s)
    按 sparsity 选最小的置零 → 写回
```

#### 4.2.3 源码精读

**`WandaPruningModifier` 继承共享基类**：

[src/.../pruning/wanda/base.py:22](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/wanda/base.py#L22) —— `class WandaPruningModifier(SparsityModifierBase)`，只需实现 `calibrate_module` 与 `compress_modules`。

**累积统计——`calibrate_module`**：

[src/.../pruning/wanda/base.py:70-99](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/wanda/base.py#L70-L99) —— 每个 batch 调 `accumulate_row_scalars` 增量更新 `self._row_scalars[module]`。

**真正的数学在纯算法文件里**：

[src/.../pruning/wanda/wanda_sparsify.py:16-51](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/wanda/wanda_sparsify.py#L16-L51) —— `accumulate_row_scalars`：先把历史 `row_scalars` 按样本数加权缩放（`*= num_samples/(num_samples+num_added)`，做增量平均），再加上新样本的列范数平方。关键一行：

```python
row_scalars += torch.norm(inp, p=2, dim=1) ** 2 / num_samples
```

即上文 \(s_j\) 的增量累积。

**打分与置零——`sparsify_weight`**：

[src/.../pruning/wanda/wanda_sparsify.py:54-106](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/wanda/wanda_sparsify.py#L54-L106) —— 关键三步：

```python
W_metric = torch.abs(W) * torch.sqrt(S.reshape((1, -1)))   # L80 打分
# 非结构化: 每行排序取最小的 sparsity 比例 (L95-97)
W[W_mask] = 0.0                                             # L99 置零
```

`prune_n != 0` 分支（L84-93）则是 N:M 结构化：每 `prune_m` 列里选 `prune_n` 个最小的置零。

**`compress_modules` 把结果写回模块**：

[src/.../pruning/wanda/base.py:101-127](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/wanda/base.py#L101-L127) —— 调 `sparsify_weight` 得到稀疏化后的权重，用 `update_offload_parameter(module, "weight", sparsified_weight)` 写回（兼容磁盘 offload）。注意 `_row_scalars[module]` 已在 `sparsify_weight` 内部被 `del`，这里只删 `_num_samples`。

#### 4.2.4 代码实践

**实践目标**：用 Wanda 对一个稠密小模型做 50% 非结构化剪枝，并验证零值比例。

**操作步骤**：

1. 选一个极小模型（如 `HuggingFaceTB/SmolLM2-135M` 或本地小模型）。
2. 准备少量校准数据（Wanda 需要校准数据来算激活范数）。
3. 运行以下脚本（**示例代码**）：

```python
from transformers import AutoModelForCausalLM
from datasets import load_dataset
from llmcompressor import oneshot
from llmcompressor.modifiers.pruning import WandaPruningModifier

model = AutoModelForCausalLM.from_pretrained("HuggingFaceTB/SmolLM2-135M")

recipe = WandaPruningModifier(
    sparsity=0.5,            # 剪掉 50%
    mask_structure="0:0",    # 非结构化
    targets=["Linear"],
    ignore=["re:.*lm_head"], # 显式忽略 lm_head
)

oneshot(
    model=model,
    dataset="HuggingFaceFW/fineweb-edu",  # 任选文本数据集
    recipe=recipe,
    num_calibration_samples=64,
    max_seq_length=2048,
    output_dir="./smollm2-wanda-50",
)

# 检查零值比例
import torch
total, zeros = 0, 0
for name, p in model.named_parameters():
    if "weight" in name:
        total += p.numel()
        zeros += (p == 0).sum().item()
print(f"零值比例: {zeros/total:.2%}")  # 预期接近 50%
```

**需要观察的现象**：日志里能看到「Sparsifying ... using N samples」逐层出现；保存后 `model.named_parameters()` 的零值比例应接近 0.5（`lm_head` 等被忽略的层不计入会更准）。

**预期结果**：非结构化下零值比例约 50%；若改 `mask_structure="2:4"`，零值比例约为 50% 但呈 2:4 规律分布。**待本地验证**：实际零值比例取决于命中 `Linear` 的层数与被 `ignore` 的层占比。

#### 4.2.5 小练习与答案

**练习 1**：Wanda 的 `row_scalars` 形状是 `[num_columns]`，对应权重的哪个维度？为什么不是方阵？

> 参考答案：对应权重的输入维度（`weight.shape[1]`，即 `in_features`）。因为 Wanda 只需每列（每个输入通道）的激活范数标量 \(s_j\)，不像 SparseGPT 需要 \(\text{in}\times\text{in}\) 的 Hessian 方阵，所以更省内存。

**练习 2**：如果把 `sparsity` 设为 `0.5` 但 `mask_structure="2:4"`，实际稀疏度和纯非结构化 `0:0` 相比有何不同？

> 参考答案：两者零值比例都约 50%，但 `2:4` 强制零值出现在每 4 连续元素中的固定 2 个位置上（结构化），适合稀疏硬件；`0:0` 则零值位置任意（非结构化）。`sparsity` 字段在 N:M 模式下仅用于推算应保留比例，掩码由 N:M 规则决定。

---

### 4.3 SparseGPT：用 Hessian 做最优误差补偿

#### 4.3.1 概念说明

SparseGPT 的核心思想比 Wanda 更深一层（其逐列最优补偿的数学源自 OBQ/Optimal Brain Surgeon 推导，源码注释明确指向 [arXiv:2203.07259 第 3.4 节](https://arxiv.org/abs/2203.07259)）：**剪掉某个权重后，并不是简单置零了事，而是利用校准数据算出的 Hessian 信息，把这一列的误差「最优地」补偿到尚未处理的剩余权重上**，从而让整层输出在剪枝后变化最小。

这与 GPTQ 量化（u4-l1）共享同一套 Hessian 数学，区别只在于：GPTQ 用它来「选最优低位整数」，SparseGPT 用它来「选最优零位」。事实上 SparseGPT 求逆失败时会**退化成 RTN 式的简单置零**。

#### 4.3.2 核心流程与数学

对权重 \(W\)，给定校准输入 \(X\)，定义：

\[ H = 2X^\top X \]

\(H\) 是该层重建损失 \(\|WX - Y\|^2\) 关于 \(W\) 的 Hessian（差一个常数）。SparseGPT 按列分块（`block_size`，默认 128）逐块处理：

1. 给 \(H\) 加阻尼（`dampening_frac`，默认 0.01）防数值不稳，用 Cholesky 求逆得 \(H_{\text{inv}}\)。
2. 在当前块内按「\(w_{ij}^2 / H_{\text{inv},jj}^2\)」最小的位置置零（选对误差影响最小的）。
3. 把置零带来的误差 \((w-q)/d\) 用 \(H_{\text{inv}}\) 补偿到**右侧尚未处理的列**：

\[ W_{\cdot, j:} \leftarrow W_{\cdot, j:} - \text{err}\cdot H_{\text{inv}}[j, j:] \]

这正是 [论文 3.4 节](https://arxiv.org/abs/2203.07259) 的 OBQ 闭式解。求逆失败时退化为单位阵（即不补偿，等价于按幅度简单置零）。

#### 4.3.3 源码精读

**`SparseGPTModifier` 的特有字段**：

[src/.../pruning/sparsegpt/base.py:74-82](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/sparsegpt/base.py#L74-L82) —— `block_size=128`、`dampening_frac=0.01`、`preserve_sparsity_mask=False`、`offload_hessians=False`，以及两个私有统计字典 `_hessians` / `_num_samples`。

**累积 Hessian——`calibrate_module`**：

[src/.../pruning/sparsegpt/base.py:84-114](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/sparsegpt/base.py#L84-L114) —— 每批（batch）调用 `accumulate_hessian`，并可通过 `_maybe_onload_hessian` 上下文管理器（[L148-158](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/sparsegpt/base.py#L148-L158)）把 Hessian 在 CPU/GPU 间搬运以省显存（`offload_hessians=True` 时）。

**Hessian 数学——`accumulate_hessian`**：

[src/.../pruning/sparsegpt/sgpt_sparsify.py:19-55](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/sparsegpt/sgpt_sparsify.py#L19-L55) —— 关键两行：

```python
inp = math.sqrt(2 / num_samples) * inp   # L52 缩放
H += inp.matmul(inp.t())                 # L53 累积 H = 2 X^T X
```

注意它和 GPTQ 的 `accumulate_hessian` 数学一致（回顾 u4-l1）。

**最优补偿稀疏化——`sparsify_weight`**：

[src/.../pruning/sparsegpt/sgpt_sparsify.py:58-213](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/sparsegpt/sgpt_sparsify.py#L58-L213) —— 重点三段：

- **Cholesky 求逆**（[L103-117](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/sparsegpt/sgpt_sparsify.py#L103-L117)）：加阻尼 `damp = dampening_frac * mean(diag(H))`，Cholesky 分解后 `cholesky_inverse` 得到 `Hinv`；若抛 `LinAlgError` 则 `Hinv = I`（退化为不补偿）。
- **分块逐列循环**（[L140-206](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/sparsegpt/sgpt_sparsify.py#L140-L206)）：核心是 `Err1.matmul(Hinv1[i1:i2, i2:])` 把当前块的误差补偿到右侧剩余列（即上文公式），对应注释「See section 3.4 of ...」。
- **掩码选择**（[L150-165](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/sparsegpt/sgpt_sparsify.py#L150-L165)）：非结构化时按 `W1**2 / diag(Hinv1)**2` 最小者置零。

**`compress_modules` 与收尾**：

[src/.../pruning/sparsegpt/base.py:116-173](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/sparsegpt/base.py#L116-L173) —— `sparsify_weight` 返回 `(loss, W)`，`loss` 经 `CompressionLogger` 记录；`on_finalize` 会检查是否还有未压缩模块（有则报错），并清空所有统计字典。

#### 4.3.4 代码实践

**实践目标**：感受 `dampening_frac` 对数值稳定性的影响。

**操作步骤**：

1. 复用 4.2.4 的脚本，把 modifier 换成 `SparseGPTModifier`：

```python
from llmcompressor.modifiers.pruning import SparseGPTModifier

recipe = SparseGPTModifier(
    sparsity=0.5,
    mask_structure="0:0",
    targets=["Linear"],
    ignore=["re:.*lm_head"],
    dampening_frac=0.01,   # 先用默认值
    block_size=128,
)
```

2. 再把 `dampening_frac` 调到极小（如 `1e-8`）重跑一次。

**需要观察的现象**：`dampening_frac` 过小时日志可能出现 `Failed to invert hessian due to numerical instability`，并提示增大 `dampening_frac` 或增加校准样本。

**预期结果**：阻尼过小→Hessian 病态→退化成单位阵（不补偿），剪枝质量下降；适度阻尼→稳定求逆→误差补偿生效。**待本地验证**：是否触发警告取决于模型与校准数据。

#### 4.3.5 小练习与答案

**练习 1**：SparseGPT 的 `sparsify_weight` 里 `W[:, i2:] -= Err1.matmul(Hinv[i1:i2, i2:])` 这行在做什么？

> 参考答案：把当前块 \([i1, i2)\) 内因置零产生的列向误差 `Err1`，通过逆 Hessian 块 `Hinv[i1:i2, i2:]` 补偿到右侧尚未处理的列 \([i2, \text{end})\)，使整层输出在最小二乘意义下尽量不变。

**练习 2**：SparseGPT 与 GPTQ（u4-l1）都用 Hessian，两者对 Hessian 的用途有什么不同？

> 参考答案：GPTQ 用 Hessian 指导「把权重舍入到最优低位整数」（量化），SparseGPT 用 Hessian 指导「把哪些权重置零并最优补偿误差」（剪枝）。两者共享 `accumulate_hessian` 这套 \(H=2X^\top X\) 数学，只是后续优化目标不同。

---

### 4.4 Magnitude：无数据幅度剪枝（走训练事件链）

#### 4.4.1 概念说明

前面三个 modifier 都依赖校准数据（Wanda/SparseGPT 算 Hessian 或激活范数，REAP 算路由 saliency）。`MagnitudePruningModifier` 则是**完全不需要校准数据**的剪枝：它只看权重自身的绝对值 \(|w|\)，把最小的置零。这正是最古老的「幅度剪枝」思想。

更特别的是，它**不走校准链**，而是走 **训练事件链**（`on_start` / `on_update` / `on_end`），配合一个**稀疏度调度器**（scheduler），让稀疏度从 `init_sparsity` 渐进升到 `final_sparsity`。这让它既能用于一次性 PTQ，也能用于训练式逐步剪枝。

#### 4.4.2 核心流程

```text
on_initialize   创建调度器(从 init_sparsity 到 final_sparsity)
                创建掩码生成器(mask_structure)
                build_parameterized_layers + add_mask 给目标参数挂掩码
on_start        按 current sparsity 算掩码: score = |param|，小的置零 → enable_masks
on_update       BATCH_START 时若调度出的 sparsity 变了就重算掩码
on_end          disable_masks（保留置零结果）
on_finalize     remove_mask
```

打分函数恒为权重绝对值：

\[ \text{score} = |w|,\qquad \text{剪掉最小的 sparsity 比例} \]

#### 4.4.3 源码精读

**类定义与字段**：

[src/.../pruning/magnitude/base.py:24-39](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/magnitude/base.py#L24-L39) —— `class MagnitudePruningModifier(Modifier, LayerParamMasking)`：注意它**不继承 `SparsityModifierBase`**，而是多重继承 `LayerParamMasking`（提供掩码应用机制），用 `init_sparsity`/`final_sparsity` + `update_scheduler`（默认 `"cubic"`）+ `mask_structure`。

**`on_initialize`——建调度器与掩码生成器**：

[src/.../pruning/magnitude/base.py:51-90](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/magnitude/base.py#L51-L90) —— `PruningSchedulerFactory.create_scheduler` 按 `update_scheduler` 名字（linear/cubic/polynomial/multi_step/自定义 `calc(...)`）造调度函数；`build_parameterized_layers` 把 `targets` 命中的参数收集起来，`add_mask` 给每个参数挂上掩码。

**调度器实现**——以 cubic 为例：

[src/.../pruning/helpers.py:116-142](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/helpers.py#L116-L142) —— `cubic_scheduler` 实际委托 `polynomial_decay_scheduler`，按 `event.current_index` 在 `[start, end]` 区间内把稀疏度从 `init_sparsity` 用指数衰减曲线升到 `final_sparsity`。

**`on_start`——按幅度算掩码并启用**：

[src/.../pruning/magnitude/base.py:98-112](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/magnitude/base.py#L98-L112) —— 关键是 `scores=parameterized_layer.param.data.abs()`（纯幅度打分），`mask_creator_function_` 据此选最小的置零，`enable_masks()` 把掩码应用到权重。

**`on_update` 与 `on_end`**：

[src/.../pruning/magnitude/base.py:114-136](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/magnitude/base.py#L114-L136) —— `on_update` 在 `BATCH_START` 且调度出的稀疏度变化时重算掩码；`on_end` 调 `disable_masks()`（移除掩码钩子但保留已置零的权重值）。

#### 4.4.4 代码实践

**实践目标**：验证 Magnitude 不需要校准数据，且稀疏度按调度器变化。

**操作步骤**（**示例代码**）：

1. 构造一个 `MagnitudePruningModifier`，设 `init_sparsity=0.0, final_sparsity=0.5`：

```python
from llmcompressor.modifiers.pruning import MagnitudePruningModifier
recipe = MagnitudePruningModifier(
    targets="__ALL__",
    init_sparsity=0.0,
    final_sparsity=0.5,
    update_scheduler="cubic",
    mask_structure="unstructured",
    start=0, end=1, update=1,
)
```

2. 在 oneshot 里**不传 `dataset`** 运行。

**需要观察的现象**：与 Wanda/SparseGPT 不同，日志不会出现逐层「Sparsifying using N samples」（因为它不挂 forward hook、不累积校准统计），而是在 `on_start`/`on_update` 时一次性按幅度置零。

**预期结果**：最终权重零值比例约 `final_sparsity`（0.5）。**待本地验证**：Magnitude 在纯 oneshot（无训练 step）场景下的行为依赖调度器在单次触发时的取值。

#### 4.4.5 小练习与答案

**练习 1**：Magnitude 与 Wanda 打分函数只差一个因子，差在哪？为什么 Magnitude 可以无数据？

> 参考答案：Wanda 是 \(|w|\cdot\sqrt{s}\)（含激活列范数 \(s\)），Magnitude 是纯 \(|w|\)。Magnitude 不需要激活统计，因此无需校准数据，但它忽略了「权重重要性还取决于输入」这一事实，精度通常不如 Wanda/SparseGPT。

**练习 2**：Magnitude 走的是哪条生命周期链？为什么不是校准链？

> 参考答案：训练链（`on_start`/`on_update`/`on_end`），配合 `BATCH_START` 事件与稀疏度调度器，支持「逐步加深稀疏度」的训练式剪枝；它不需要校准数据累积，故不走校准链。

---

### 4.5 REAP：面向 MoE 的专家剪枝

#### 4.5.1 概念说明

REAP（**R**outer-weighted **E**xpert **A**ctivation **P**runing）来自 [论文 arXiv:2510.13999](https://arxiv.org/abs/2510.13999)，是本讲唯一**结构化**剪枝：它不针对单个权重，而是针对 MoE（混合专家）模型里的**整个专家**。对每个 MoE 层，REAP 计算每个专家的「显著性」，剪掉显著性最低的若干个专家，并同步裁剪路由器（router）权重、更新模型 config。

前三者剪「权重值」，REAP 剪「模块数量」——剪完模型真的变小了。

#### 4.5.2 核心流程与数学

每个专家 \(j\) 的显著性定义为（对其收到的 token 取平均）：

\[ S_j = \text{mean}_{t\to j}\bigl(g_j^{(t)}\cdot \|f_j^{(t)}\|_2\bigr) \]

其中 \(g_j\) 是路由器分给专家 \(j\) 的门控权重（融合专家输出时的系数），\(f_j\) 是专家对该 token 的输出激活，\(\|f_j\|_2\) 是其范数。直觉：**一个专家既被路由器经常选中（\(g_j\) 大）、又输出强响应（\(\|f_j\|\) 大），才重要**。显著性最低的专家被删除。

执行流程（跑在 sequential 管线上）：

```text
on_initialize         get_moe_attrs 探测 MoE 结构; 算每层要删 _n_experts_to_drop = num_experts*sparsity
                      fail-fast: 删完后剩余可达专家数必须 ≥ top_k, 否则报错
on_calibration_start  给每个专家挂 forward hook(记录输出范数 f_j)
                      给 experts 块挂 forward hook(取路由 top_k 索引与权重 g_j, 更新 saliency)
(校准逐子图前向累积 saliency)
on_sequential_epoch_end  compute_retained_experts(留高 saliency 的) → prune_moe_layer(切片专家 + 裁 router)
on_calibration_end    remove_hooks()
on_finalize           update_model_config(把 num_experts 改成新值)
```

#### 4.5.3 源码精读

**类定义与校验**：

[src/.../pruning/reap/base.py:30-78](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/reap/base.py#L30-L78) —— `class REAPPruningModifier(Modifier)`，`requires_calibration_data=True`，必填字段 `sparsity`（取值开区间 (0,1)，`_validate_sparsity` 在 [L74-78](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/reap/base.py#L74-L78) 强制）。

**`on_initialize`——探测 MoE 与 fail-fast**：

[src/.../pruning/reap/base.py:80-154](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/reap/base.py#L80-L154) —— 关键三步：`get_moe_attrs` 探测结构；`_n_experts_to_drop = int(num_experts * sparsity)`（[L85](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/reap/base.py#L85)）；剪完后「可达专家数 < top_k」时直接抛错（[L117-124](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/reap/base.py#L117-L124)），避免剪完路由器选不到足够专家。

**挂专家 hook——`on_calibration_start`**：

[src/.../pruning/reap/base.py:156-205](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/reap/base.py#L156-L205) —— 对每个专家挂 `_expert_hook`（记录输出范数），对 experts 块挂 `_experts_block_hook`（取路由 `top_k_indices`/`top_k_weights` 更新 saliency）。这里还强制 **REAP 必须是 recipe 里唯一的 modifier**（[L161-167](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/reap/base.py#L161-L167)），否则抛错（建议配合 independent 管线）。

**两个 hook 的实现**：

[src/.../pruning/reap/base.py:253-291](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/reap/base.py#L253-L291) —— `_expert_hook` 算 `torch.linalg.norm(output.float(), dim=-1)` 存入 `_norm_buffers`；`_experts_block_hook` 从 `args[1]=top_k_indices, args[2]=top_k_weights` 取路由决策，调 `tracker.update(...)`。

**saliency 累积——`REAPSaliencyTracker.update`**：

[src/.../pruning/reap/utils.py:231-290](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/reap/utils.py#L231-L290) —— 向量化累积 `sum_saliency[j] += g_j * ||f_j||` 与 `count[j]`，`mean_saliency`（[L326-332](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/reap/utils.py#L326-L332)）即 \(S_j=\text{sum}/\text{count}\)。

**决定保留谁——`compute_retained_experts`**：

[src/.../pruning/reap/utils.py:292-318](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/reap/utils.py#L292-L318) —— 用 `torch.topk(saliency, n_to_drop, largest=False)` 选最不重要的删，其余保留；分组路由（DeepSeek 式）则每组分别删。

**真正的结构化裁剪——`prune_moe_layer` + `_prune_router`**：

[src/.../pruning/reap/utils.py:340-414](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/reap/utils.py#L340-L414) —— `prune_moe_layer` 重建专家 `ModuleList`（只留 retained 索引），`_prune_router` 切片路由器 `weight`/`bias`/`e_score_correction_bias`（分组路由的分数修正缓冲区），保证路由器与新专家数对齐。

**更新 config——`update_model_config`**：

[src/.../pruning/reap/utils.py:417-429](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/reap/utils.py#L417-L429) —— 把 `config.num_experts`（或 `num_local_experts`/`moe_num_experts`）改成新值，使保存后的 checkpoint 自洽。

#### 4.5.4 代码实践

**实践目标**：用 REAP 对一个 MoE 小模型剪枝，验证专家数确实减少。

**操作步骤**（**示例代码**，基于 `examples/reap_expert_pruning/`）：

```python
from transformers import AutoModelForCausalLM, AutoTokenizer
from llmcompressor import oneshot
from llmcompressor.modifiers.pruning import REAPPruningModifier

model_id = "MoonshotAI/Moonlight-16B-A3B-Instruct"  # 或任一 LinearExperts2D 格式的 MoE 模型
model = AutoModelForCausalLM.from_pretrained(model_id)

recipe = REAPPruningModifier(sparsity=0.25)  # 每层剪掉 25% 专家

oneshot(
    model=model,
    dataset="HuggingFaceH4/ultrachat_200k",
    recipe=recipe,
    num_calibration_samples=512,
    max_seq_length=2048,
    moe_calibrate_all_experts=False,
)

print("剪枝后 num_experts:", model.config.num_experts)
```

**需要观察的现象**：日志先打印 `REAP initialized: N MoE layers, M experts/layer, will drop K`，校准后每层打印 `Updated ...num_experts: M -> M-K`。

**预期结果**：`model.config.num_experts` 减少，模型体积下降。**待本地验证**：REAP 当前仅支持 `LinearExperts2D` 格式的专家，`GraniteMoeLinearExperts` 与 `Llama4LinearExperts` 会被跳过（见 [reap/utils.py:155-169](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/reap/utils.py#L155-L169)）。若无合适 MoE 模型，可改为阅读 `tests/llmcompressor/modifiers/pruning/reap/test_base.py` 里的 `test_reap_full_lifecycle`，它用合成的 `FakeMoEModel` 验证完整流程。

#### 4.5.5 小练习与答案

**练习 1**：REAP 的 `on_initialize` 里那段 `if top_k > available: raise` 在防什么？

> 参考答案：防止剪得太狠，导致路由器为每个 token 选 top_k 个专家时「可达专家数 < top_k」。比如 8 个专家、top_k=5，若剪掉 6 个只剩 2 个，路由器就选不到 5 个了，模型会崩。这是 fail-fast，在开始校准前就报错。

**练习 2**：为什么 REAP 要求「recipe 里必须是唯一 modifier」？

> 参考答案：其他 modifier（如量化）会改变专家的输出或挂自己的 hook，干扰 REAP 的 saliency 累积与结构化裁剪。正确做法是把 REAP 与量化放在不同 stage，或用 independent 管线让它们各自独立跑一次校准。

**练习 3**：REAP 剪完后为什么要调 `update_model_config`？

> 参考答案：结构化裁剪真的删掉了专家模块，模型 `config.num_experts` 必须同步更新，否则保存的 checkpoint 与实际模块数不一致，加载时会出错。

---

## 5. 综合实践

**任务**：给同一个稠密小模型，分别用 **Wanda** 和 **SparseGPT** 做 50% 非结构化剪枝，比较两者的剪枝过程与结果。

**步骤**：

1. 选一个小模型（如 `HuggingFaceTB/SmolLM2-135M`），准备同一份校准数据。
2. 分别用以下两个 recipe 跑 oneshot：

```python
# (a) Wanda
from llmcompressor.modifiers.pruning import WandaPruningModifier
recipe_a = WandaPruningModifier(sparsity=0.5, mask_structure="0:0",
                                targets=["Linear"], ignore=["re:.*lm_head"])

# (b) SparseGPT
from llmcompressor.modifiers.pruning import SparseGPTModifier
recipe_b = SparseGPTModifier(sparsity=0.5, mask_structure="0:0",
                             targets=["Linear"], ignore=["re:.*lm_head"],
                             block_size=128, dampening_frac=0.01)
```

3. 对两个产物分别统计：全局零值比例、单层零值分布是否均匀、跑一段生成文本观察质量差异。
4. （可选）把 SparseGPT 的 `dampening_frac` 调到极小，观察是否触发 Hessian 求逆警告，并对比剪枝质量。

**观察要点**：

- 两者零值比例都应接近 50%，但 SparseGPT 日志会打印逐层 `loss`（重建误差），Wanda 不会。
- SparseGPT 因有误差补偿，通常在相同稀疏度下生成质量略优于 Wanda，但更慢、更耗内存（要存 \( \text{in}\times\text{in} \) 的 Hessian）。
- 两者都走 sequential 管线（`requires_calibration_data=True`），日志里能看到逐子图校准。

## 6. 本讲小结

- **剪枝 vs 量化**：剪枝把权重置零（或删模块）产生稀疏，量化用低位表示稠密权重；两者可组合但本讲只讲剪枝。
- **共享骨架 `SparsityModifierBase`**：SparseGPT 与 Wanda 共用「挂 hook 累积统计 → `on_sequential_epoch_end` 改权重 → 卸 hook」三步，差异只在两个抽象方法 `calibrate_module`/`compress_modules`。
- **Wanda**：打分 \(|w|\sqrt{s}\)，\(s\) 为激活列范数平方；轻量快速，只需 `[num_columns]` 向量统计。
- **SparseGPT**：用 \(H=2X^\top X\) 的逆做最优误差补偿，逐块逐列处理；求逆失败退化为简单置零；与 GPTQ 共享 Hessian 数学。
- **Magnitude**：纯幅度打分 \(|w|\)，无数据，走**训练事件链**与稀疏度调度器，支持渐进剪枝。
- **REAP**：MoE 专家级结构化剪枝，saliency \(S_j=\text{mean}(g_j\cdot\|f_j\|_2)\)，剪完真的删模块、裁路由器、改 config。

## 7. 下一步学习建议

- **对比量化**：回到 [u4-l1 GPTQ](u4-l1-gptq-algorithm.md)，对比 GPTQ 与 SparseGPT 如何复用同一套 Hessian 数学做不同目标的最优化。
- **扩展开发**：学完 [u6-l4 自定义 Modifier](u6-l4-custom-modifier.md) 后，可尝试仿照 `SparsityModifierBase` 实现一个自定义打分函数的剪枝 modifier（如结合 Wanda 与幅度的混合打分）。
- **规模化**：剪枝大模型同样受显存制约，可继续读 [u6-l2 大模型内存策略](u6-l2-big-model-memory.md) 了解 sequential onloading 与 Hessian offloading（`offload_hessians`）。
- **MoE 全链路**：REAP 依赖 MoE 线性化与专家建模，建议接着读 [u5-l2 MoE 建模与线性化](u5-l2-moe-modeling.md) 理解 `LinearExperts2D` 与 `moe_calibration_context`。
