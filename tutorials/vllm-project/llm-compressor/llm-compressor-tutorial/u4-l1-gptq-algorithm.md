# GPTQ 算法实现

## 1. 本讲目标

本讲精读 llm-compressor 中 **GPTQ**（一种基于激活校准的权重量化算法）的完整实现。学完后你应当能够：

- 说清楚 GPTQ 为什么需要校准数据、它的核心数学直觉（Hessian 矩阵 + 逐列量化 + 误差补偿）是什么。
- 跟着 `GPTQModifier` 的四个生命周期钩子，讲明白「挂量化方案与 observer → 累积 Hessian → 逐列量化权重 → 冻结」这条主链路。
- 独立阅读 `quantize_weight` 函数，讲明白 `block_size`、`dampening_frac`、`actorder` 三个参数分别控制了什么。
- 理解 `GPTQModifier` 与第三单元学过的 `QuantizationMixin` 如何分工协作。

本讲是第四单元（压缩算法实现）的第一篇，承接 u3-l2（QuantizationMixin）与 u3-l5（SequentialPipeline）。建议先复习这两篇里「量化开关 / 校准 hook」与「逐子图两遍前向」的概念。

---

## 2. 前置知识

### 2.1 RTN 的局限

在 u3-l1 我们讲过 `QuantizationModifier` 本身只做 **RTN（round-to-nearest，就近取整）**：直接把每个权重值映射成最近的低位整数。RTN 把每个权重当作「同等重要」来取舍。

问题在于：权重的重要性并不相同。对一个线性层 \( y = W x \)，有些输入维度（\(x\) 的某些分量）在校准数据里出现得很多、幅值很大，这些维度对应的权重列如果量化误差大，输出 \(y\) 的误差就大；反之一些几乎不被激活的列，量化误差对输出几乎无影响。

**GPTQ 的核心思想就是：用校准数据算出每个输入维度的「重要性」，再按重要性顺序逐列量化，并把当前列的量化误差「补偿」到还没量化的列上**，从而让整体输出误差最小。

### 2.2 二阶信息：Hessian 矩阵

衡量「输入维度重要性」的数学工具是 **Hessian 矩阵**。对线性层的重构误差目标：

\[
\min_{\hat W} \lVert W X - \hat W X \rVert_F^2
\]

其中 \(X\) 是校准激活（形状 `num_samples × in_features`），\(W\) 是权重（`out_features × in_features`）。这个目标关于 \(\hat W\) 的第 \(j\) 列的二阶导数矩阵正是：

\[
H = 2 X^\top X
\]

\(H\) 是 `in_features × in_features` 的矩阵，对角线 \(H_{ii}\) 越大，说明第 \(i\) 个输入维度越「活跃 / 重要」。GPTQ 就用 \(H\) 来决定量化顺序和误差补偿权重。本讲稍后会看到，源码里 `H` 正是用校准输入 \(X\) 累积出来的。

### 2.3 必要的矩阵知识

- **Cholesky 分解 / 求逆**：GPTQ 需要用到 \(H^{-1}\)（逆 Hessian）。由于 \(H = X^\top X\) 是半正定的，源码用 Cholesky 分解来稳定地求逆。
- **g_idx（group index）**：分组量化（如 int4 group=128）时，需要一张「权重列号 → 分组号」的映射表，让推理时知道每一列用哪组 scale/zero_point。这张表在 GPTQ 里叫 `g_idx`。

如果你对 scale / zero_point / group_size 等量化基础概念还不熟，请先回看 u3-l1 与 u3-l2。

---

## 3. 本讲源码地图

本讲只涉及两个文件，它们正是本讲的两个最小模块：

| 文件 | 职责 |
| --- | --- |
| [`src/llmcompressor/modifiers/gptq/base.py`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py) | `GPTQModifier` 类：声明参数（`block_size`/`dampening_frac`/`actorder`/`offload_hessians`），编排四个生命周期钩子，用 `calibrate_module` hook 累积 Hessian，在 epoch 末调 `quantize_weight` 完成逐列量化。 |
| [`src/llmcompressor/modifiers/gptq/gptq_quantize.py`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/gptq_quantize.py) | GPTQ 的纯算法实现：`make_empty_hessian`（建空 H）、`accumulate_hessian`（累积 H）、`quantize_weight`（逐列量化主循环）、`_apply_activation_ordering`（激活排序）。 |

此外会少量引用协作文件（不展开精读）：

- `src/llmcompressor/modifiers/quantization/quantization/mixin.py` —— `QuantizationMixin`，挂方案、observer、校准 hook 的脚手架（u3-l2 已讲）。
- `src/llmcompressor/modifiers/quantization/calibration.py` —— `observe` / `update_qparams`，把 observer 统计换成 scale/zero_point（u3-l3 已讲）。
- `tests/llmcompressor/modifiers/gptq/test_gptq_quantize.py` —— 单元测试，是理解 `quantize_weight` 行为的最佳脚手架。

---

## 4. 核心概念与源码讲解

### 4.1 GPTQModifier 全景：双继承与生命周期协作

#### 4.1.1 概念说明

`GPTQModifier` 是一个「需要校准数据的权重量化 modifier」。它的特殊之处在于 **双继承**：

```python
class GPTQModifier(Modifier, QuantizationMixin):
```

- 继承 `Modifier`：拿到生命周期骨架（u2-l3 讲过的 `initialize` / `update_event` / `finalize` 模板方法，以及 `on_*` 钩子）。
- 混入 `QuantizationMixin`：拿到「把量化方案挂到模块、挂 observer、挂校准 hook、开关量化」这一整套脚手架（u3-l2）。

这种分工非常重要：**`QuantizationMixin` 自己不会去更新 scale/zero-point**，它只负责「搭台」（挂方案、挂 hook、控制量化开关）；真正「唱戏」（怎么算出更好的权重）由子类 `GPTQModifier` 自己实现。所以 GPTQ 的算法逻辑几乎全在 `base.py` 的几个钩子里，而 RTN（`QuantizationModifier`）只是 mixin 的最简单用法。

#### 4.1.2 核心流程

GPTQModifier 把压缩组织成一条与校准生命周期严格对齐的链路。结合 u3-l5 的 SequentialPipeline（逐子图两遍前向），整个过程是：

```text
on_initialize
  └─ QuantizationMixin.initialize_quantization：给目标模块挂 quantization_scheme（含 weight_observer），并临时关闭量化
     （GPTQ 自己额外记录「模块→名字」的映射 _module_names）

on_calibration_start        ← 由 SequentialPipeline 在子图第一遍前向前触发
  └─ QuantizationMixin.start_calibration：按 scheme 挂 observer 与输入/输出校准 hook，开启量化
  └─ GPTQ 额外：给每个待量化模块挂 calibrate_module hook（用来累积 Hessian）

  【校准前向逐 batch 运行】
     calibrate_module hook 触发 → accumulate_hessian 把本 batch 输入 X 累加进 _hessians[module]

on_sequential_epoch_end     ← 子图第一遍前向结束后触发，传入本轮处理的 modules
  └─ observe(weight) + update_qparams：先用 observer 算出权重的 scale/zero_point（供 quantize_weight 内部的 fake_quantize 使用）
  └─ compress_modules → quantize_weight：用累积好的 Hessian，逐列量化权重，写回 weight/weight_scale/...

on_calibration_end
  └─ QuantizationMixin.end_calibration：卸载校准 hook、删 observer、冻结量化参数
  └─ GPTQ 自己：remove_hooks() 卸载 calibrate_module hook
```

关键点：GPTQ 的权重量化 **精确发生在 `on_sequential_epoch_end`** 钩子里，而不是 `on_initialize` 或 `on_calibration_start`。前两个钩子只负责「把量化方案与 Hessian 累积器挂上去」，真正改权重的数学计算留到「这一层校准数据跑完、Hessian 累积完毕」之后才做。

#### 4.1.3 源码精读

**类定义与参数字段** —— GPTQ 特有的四个旋钮：

[src/llmcompressor/modifiers/gptq/base.py:47-47](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L47-L47) —— 双继承声明：`Modifier`（生命周期）+ `QuantizationMixin`（量化脚手架）。

[src/llmcompressor/modifiers/gptq/base.py:124-130](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L124-L130) —— 四个核心参数：

- `requires_calibration_data: bool = True`：**硬编码为 True**，覆盖 `QuantizationMixin` 的自动推断。这表示 GPTQ **永远需要校准数据**（要算 Hessian），因此 oneshot 一定会为它选 `sequential` 管线（回顾 u3-l4 的 `_infer_pipeline`）。
- `block_size: int = 128`：GPTQ 不是一列一列、也不是整个矩阵一次量化，而是 **每次处理一个块（128 列）**，块内逐列、块间整体补偿。这是速度与精度的折衷。
- `dampening_frac: float | None = 0.01`：Hessian 对角线阻尼系数（代码里叫 `percdamp`），用来让 H 可逆、数值稳定。默认 0.01。
- `actorder: ... = Sentinel("static")`：权重量化的列顺序，默认 `static`（按 Hessian 对角线大小排序）。
- `offload_hessians: bool = False`：是否把 Hessian 卸到 CPU 省显存（用时间换空间）。

[src/llmcompressor/modifiers/gptq/base.py:133-137](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L133-L137) —— 三个私有运行时字典：`_module_names`（模块→名字，用于日志）、`_hessians`（模块→累积的 H）、`_num_samples`（模块→已累积的样本数）。带下划线说明是 pydantic 的 `PrivateAttr`，不参与序列化，只在校准期间存活。

**`on_initialize`** —— 挂方案、记录模块名：

[src/llmcompressor/modifiers/gptq/base.py:196-214](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L196-L214)。注意它先把 `QuantizationMixin.initialize_quantization` 委派出去（挂 scheme、关量化），这印证了「mixin 搭台、子类唱戏」。注意它用 `self.resolved_targets`（而不是 `self.targets`）来匹配模块——这是 u3-l2 强调过的「真实命中目标」。

**`on_calibration_start`** —— 挂校准 hook 与 GPTQ 专属 hook：

[src/llmcompressor/modifiers/gptq/base.py:216-238](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L216-L238)。先调 `QuantizationMixin.start_calibration`（挂输入/输出校准 hook、开量化），再给每个已挂上 `quantization_scheme.weights` 的模块额外挂一个 `calibrate_module`（forward）hook。如果没找到任何可量化模块，直接抛错——这是 GPTQ 的常见报错点（`config_groups`/`targets` 配错）。注意它特意排除了 `torch.nn.Embedding`。

**`on_sequential_epoch_end`** —— 真正量化的入口：

[src/llmcompressor/modifiers/gptq/base.py:240-247](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L240-L247)。这 8 行浓缩了 GPTQ 与量化基础设施的协作：

1. `observe(modules, base_name="weight")`：让每个模块的 `weight_observer` 去观测自己的权重，累积统计（min/max 等）。
2. `self.sync_obs_act_stats(modules)`：DDP 下跨 rank 同步激活统计（单卡是 no-op，u6-l1 详讲）。
3. `update_qparams(modules, ACTIVATION_OBS, ...)`：从 observer 统计算出 `weight_scale` / `weight_zero_point` 写回模块——**这些 scale/zero_point 之后会被 `quantize_weight` 内部的 `fake_quantize` 复用**。
4. `self.compress_modules()`：进入 GPTQ 自己的逐模块量化。

**`on_calibration_end`** —— 收尾卸载：

[src/llmcompressor/modifiers/gptq/base.py:249-254](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L249-L254)。把 mixin 的校准 hook 和 GPTQ 自己的 `calibrate_module` hook 都卸掉。

#### 4.1.4 代码实践

**实践目标**：用日志确认 GPTQ 的生命周期顺序与「逐模块量化」行为。

**操作步骤**：

1. 阅读上面的四个钩子源码，在纸上写出它们被 SequentialPipeline 触发的先后顺序。
2. 运行下面这个最小 oneshot（需要一个可联网拉取或本地的小模型，如 `TinyLlama` 之类的 tiny 变体；若没有 GPU 可设 `device_map` 适配）：

```python
# 示例代码：跟踪 GPTQ 生命周期（需要校准数据，故会跑 sequential 管线）
from llmcompressor import oneshot
from llmcompressor.modifiers.gptq import GPTQModifier

recipe = GPTQModifier(targets="Linear", scheme="W4A16", ignore=["lm_head"])

oneshot(
    model="<一个小模型 id>",          # 例如 "meta-llama/..." 的 tiny 版或本地路径
    dataset="open_platypus",
    recipe=recipe,
    num_calibration_samples=8,         # 演示用，正式量化建议 512+
    max_seq_length=2048,
    output_dir="./gptq-trace-out",
)
```

**需要观察的现象**：

- 日志里应出现 `Quantizing <layer name> using <N> samples`（这条日志在 `compress_module_list`，见 [src/llmcompressor/modifiers/gptq/base.py:326-326](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L326-L326)），每个 Linear 模块各一条，确认了「逐模块量化」。
- 日志里能看到 `SequentialPipeline` 把模型切成多个子图、每子图两遍前向（回顾 u3-l5）。

**预期结果**：每个命中的 Linear 都打印一次 `Quantizing ...`，且发生时机在子图校准前向结束之后。**待本地验证**：若你的环境拉不到模型或没有足够显存，可改用下文 4.3 的纯 `quantize_weight` 单元实践，它不需要完整模型。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `GPTQModifier` 要硬编码 `requires_calibration_data = True`，而不像 `QuantizationModifier` 那样让 scheme 自动推断？

> **答案**：GPTQ 的算法本质依赖校准激活来构造 Hessian 矩阵 \(H = 2X^\top X\)，没有校准数据就根本没有 \(H\)，算法无从谈起。而 RTN 类方案（如 `FP8_DYNAMIC`、`W4A16` 纯权重）不需要二阶信息，可以无数据取整。硬编码为 True 也确保 oneshot 一定为 GPTQ 选 `sequential` 管线（带校准前向）。

**练习 2**：`on_sequential_epoch_end` 里为什么先调 `observe`+`update_qparams` 再调 `compress_modules`？

> **答案**：`quantize_weight` 内部对每一列做 `fake_quantize` 时需要用到 scale/zero_point。`observe`+`update_qparams` 负责从 `weight_observer` 算出这些量化参数并写回模块，这样 `quantize_weight` 才能通过 `module.weight_observer.get_qparams()` 拿到它们（见 4.3 源码）。

---

### 4.2 Hessian 累积：用输入激活构造二阶信息

#### 4.2.1 概念说明

GPTQ 的「校准」与 RTN 的「校准」含义不同：RTN 的校准（如果需要）只是为了统计激活 min/max 算 scale；**GPTQ 的校准是为了累积 Hessian 矩阵 \(H\)**。

\(H\) 是 `in_features × in_features` 的方阵，每个元素编码两个输入维度之间的联合活跃度。它的构造方式很直接：对每个校准样本的输入向量 \(x\)，累加 \(2 x x^\top\)：

\[
H \mathrel{+}= 2\, x x^\top
\]

所有样本累加完，就得到 \(H = 2 X^\top X\)。源码里这个累加由 `calibrate_module` 这个 forward hook 在每个 batch 自动触发。

#### 4.2.2 核心流程

```text
SequentialPipeline 子图第一遍前向，逐 batch：
  模块 forward 时，calibrate_module(module, args, output) 被触发
    └─ inp = args[0]                       # 取第一个输入张量作为 X
    └─ 若 _hessians[module] 不存在：
         make_empty_hessian(module)        # 建一个全零的 in_features×in_features 矩阵
         _num_samples[module] = 0
    └─ accumulate_hessian(inp, module, H, num_samples)
         └─ 把 inp 整形成 (num_tokens, in_features)
         └─ inp = sqrt(2) * inp            # 乘 sqrt(2)，使 H += 2 X^T X
         └─ H += inp @ inp.T
         └─ num_samples += 本 batch token 数
```

注意一个细节：`H` 的累积是 **按 token 而不是按 batch** 累加的。一个 batch 里如果有 `seq_len` 个 token，它们都会贡献到 \(H\)，所以 `_num_samples` 最终是「总 token 数」而非「样本数」（日志里的 `using N samples` 实指这个数）。

#### 4.2.3 源码精读

**`make_empty_hessian` —— 建空 H**：

[src/llmcompressor/modifiers/gptq/gptq_quantize.py:19-25](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/gptq_quantize.py#L19-L25) —— 用 `weight.shape[1]`（即 `in_features`）建一个 `in_features × in_features` 的全零矩阵，dtype 固定为 `float32`（`GPTQ_PRECISION`），保证累加与求逆的数值精度。

**`accumulate_hessian` —— 累积核心**：

[src/llmcompressor/modifiers/gptq/gptq_quantize.py:28-64](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/gptq_quantize.py#L28-L64)。关键三步在末尾：

[src/llmcompressor/modifiers/gptq/gptq_quantize.py:60-62](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/gptq_quantize.py#L60-L62) —— 这就是 \(H \mathrel{+}= 2 X^\top X\) 的实现：`inp = sqrt(2) * inp; H += inp.matmul(inp.t())`。前面 42–58 行的 `match module` 是对不同模块类型做输入整形：`Linear`/`Conv1D` 把 `(batch, seq, feat)` 摊平成 `(token, feat)` 再转置；`Conv2d` 则用 `Unfold` 把卷积滑窗展平成等效的「token×通道」矩阵——这是为了让卷积层也能套用同一套 GPTQ 公式。

**`calibrate_module` —— 触发累积的 hook**：

[src/llmcompressor/modifiers/gptq/base.py:256-290](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L256-L290)。它做三件事：

1. 取 `args[0]` 作为输入（[L271](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L271-L271)）。
2. 惰性初始化 `_hessians[module]` 与 `_num_samples[module]`，初始化设备由 `offload_hessians` 决定（CPU 或模块所在 GPU，[L274-L281](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L274-L281)）。
3. 在 `_maybe_onload_hessian` 上下文里调 `accumulate_hessian`（[L284-L290](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L284-L290)）。

**`_maybe_onload_hessian` —— offload 机制**：

[src/llmcompressor/modifiers/gptq/base.py:389-399](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L389-L399)。若 `offload_hessians=True`，进入上下文前把 H 搬到 GPU 算，退出后搬回 CPU。这就是 `offload_hessians` 参数的用时间换空间——大模型里每个 Linear 都有一个 `in_features²` 的 float32 H，显存压力很大。

#### 4.2.4 代码实践

**实践目标**：在脱离完整模型的情况下，亲手累积一个 Hessian，直观看到 \(H = 2X^\top X\)。

**操作步骤**：把下面脚本存为 `acc_hessian_demo.py` 并运行（仅依赖 torch 与 llmcompressor）：

```python
# 示例代码
import torch
from llmcompressor.modifiers.gptq.gptq_quantize import make_empty_hessian, accumulate_hessian

torch.manual_seed(0)
module = torch.nn.Linear(4, 3, bias=False)   # in_features=4
H = make_empty_hessian(module)               # 4x4 全零
num_samples = torch.zeros(())

# 喂两个「batch」，每个 batch 2 个 token，共 4 个 token
inp1 = torch.randn(2, 4)
inp2 = torch.randn(2, 4)
H, num_samples = accumulate_hessian(inp1, module, H, num_samples)
H, num_samples = accumulate_hessian(inp2, module, H, num_samples)

print("num_samples =", int(num_samples))     # 预期 4
X = torch.cat([inp1, inp2], dim=0)           # (4,4)
expected = 2 * X.t() @ X                      # 手算 2 X^T X
print("matches 2X^TX:", torch.allclose(H, expected, atol=1e-5))
```

**需要观察的现象**：`num_samples` 等于 4（token 总数），`matches 2X^TX` 为 `True`。

**预期结果**：验证了「每 token 累加 \(2xx^\top\)」的数学实现。若你修改 `inp` 的 shape 为 3 维 `(batch, seq, feat)`，函数会自动摊平，结果仍等于 `2 X^T X`（X 为摊平后的 token×feat）。

#### 4.2.5 小练习与答案

**练习 1**：`accumulate_hessian` 里为什么要乘 `sqrt(2)`？

> **答案**：为了让累加结果正好是 \(H = 2 X^\top X\)。因为 \((\sqrt{2}\,x)(\sqrt{2}\,x)^\top = 2 x x^\top\)，所以 `H += (sqrt2*inp) @ (sqrt2*inp).T` 等价于 \(H \mathrel{+}= 2 X^\top X\)，与重构误差目标 \(\lVert WX - \hat{W}X\rVert^2\) 的二阶导数一致。

**练习 2**：把 `offload_hessians` 从 False 改成 True，会改变最终量化结果吗？为什么？

> **答案**：不会改变结果，只改变显存与速度。`_maybe_onload_hessian` 在计算前把 H 搬到 GPU、计算后搬回 CPU，数值上等价；代价是每次 hook 触发多一次 CPU↔GPU 拷贝，变慢但省显存。

---

### 4.3 quantize_weight：逐列量化、阻尼与逆 Hessian

#### 4.3.1 概念说明

累积好 \(H\) 后，`quantize_weight` 才是 GPTQ 真正「改权重」的核心。它的目标：在给定量化参数（scale/zero_point，由前面的 `update_qparams` 算好）的前提下，逐列量化权重 \(W\)，并 **把当前列的量化误差补偿到剩余列**，使整体重构误差最小。

GPTQ 的核心更新公式（量化第 \(i\) 列时）：

\[
\hat{w}_i = \mathrm{quantize}(w_i),\qquad
W_{:, i+1:} \mathrel{-}= \frac{(w_i - \hat{w}_i)}{H^{-1}_{ii}} \cdot H^{-1}_{i,\, i+1:}
\]

直觉：当前列量化后产生误差 \((w_i - \hat{w}_i)\)，按逆 Hessian 的对应行把它「分摊」到尚未量化的列上，让后续列预先吸收这部分误差。\(H^{-1}_{ii}\)（代码里的 `d`）起归一化作用。

**阻尼（dampening）**：\(H = X^\top X\) 可能奇异或病态（某些输入维度从未被激活），无法稳定求逆。GPTQ 给对角线加一个小扰动：

\[
H \mathrel{+}= \lambda \cdot \mathrm{mean}(\mathrm{diag}(H)) \cdot I
\]

\(\lambda\) 就是 `dampening_frac`（代码里叫 `percdamp`）。阻尼越大，\(H\) 越接近单位矩阵、求逆越稳，但补偿越「迟钝」（趋近 RTN）；阻尼越小，补偿越精细，但数值风险越高。

#### 4.3.2 核心流程

`quantize_weight` 主流程（[src/llmcompressor/modifiers/gptq/gptq_quantize.py:67-264](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/gptq_quantize.py#L67-L264)）：

```text
1. 准备：W = weight.clone() 转 float32，H = 累积好的 Hessian（已除以 num_samples）
2. （可选）actorder：按 diag(H) 重排 W 与 H 的列顺序
3. （可选）g_idx：分组量化时建立「列号→分组号」映射
4. 从 observer 取 scale / zero_point / global_scale
5. 处理「死列」：diag(H)==0 的列（从未激活）直接置零、跳过
6. 阻尼 + Cholesky 求 Hinv：
     damp = percdamp * mean(diag(H))
     H[diag,diag] += damp
     Hinv = cholesky_upper( cholesky_inverse( cholesky(H) ) )
   若数值失败 → 退化为单位矩阵（等价 RTN）
7. 按 block_size 分块，逐块：
     块内逐列：fake_quantize 当前列 → 记误差 → 用 Hinv 把误差分摊到块内剩余列
     块结束后：把整块的误差用 Hinv 分摊到所有后续列
8. （可选）恢复原始列顺序，reshape 回原形状
9. 返回 loss 与 {weight, weight_scale, weight_zero_point, [weight_global_scale], [weight_g_idx]}
```

注意第 6 步的三次 Cholesky：先 `cholesky`（下三角）、再 `cholesky_inverse`、再 `cholesky(upper=True)`，得到的是一个上三角形式的 \(H^{-1}\)。这是 GPTQ 官方实现的稳定求逆套路。

#### 4.3.3 源码精读

**死列处理**：

[src/llmcompressor/modifiers/gptq/gptq_quantize.py:143-146](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/gptq_quantize.py#L143-L146) —— `diag(H)==0` 的列说明该输入维度在所有校准样本里都没出现（死维度），把 H 对应行列置 1、权重列置 0，避免除零与无意义量化。

**阻尼 + Cholesky 求逆（含 RTN 兜底）**：

[src/llmcompressor/modifiers/gptq/gptq_quantize.py:148-164](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/gptq_quantize.py#L148-L164) —— 这就是上面阻尼公式与三次 Cholesky 的实现。`try/except torch._C._LinAlgError` 很关键：一旦 Hessian 数值不稳定（阻尼太小、样本太少），就退化成单位矩阵 `Hinv = eye(...)`，此时 GPTQ 补偿失效、等价于 RTN。报错信息明确建议「增大 `dampening_frac`、增加校准样本、或打乱数据」——这正是阻尼参数的调试方向。

**逐列量化 + 误差补偿（块内）**：

[src/llmcompressor/modifiers/gptq/gptq_quantize.py:177-238](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/gptq_quantize.py#L177-L238)。逐列看：

- [L178-L180](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/gptq_quantize.py#L178-L180)：`w = W1[:, i]`（当前列），`d = Hinv1[i,i]`（归一化因子）。
- [L182-L229](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/gptq_quantize.py#L182-L229)：按 `strategy`（TENSOR/CHANNEL/GROUP/TENSOR_GROUP/BLOCK）选对应 scale/zero_point 切片，调 `fake_quantize` 得到量化后的列 `q`。
- [L235-L237](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/gptq_quantize.py#L235-L237)：核心补偿——`err1 = (w - q) / d`，`W1[:, i:] -= err1.unsqueeze(1) @ Hinv1[i, i:].unsqueeze(0)`，即把误差按逆 Hessian 行分摊到块内剩余列。

**块间误差分摊**：

[src/llmcompressor/modifiers/gptq/gptq_quantize.py:240-245](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/gptq_quantize.py#L240-L245) —— 一个 block 处理完后，把整块累积的误差 `Err1` 用 `Hinv[i1:i2, i2:]` 分摊到所有后续列。这就是 `block_size` 的意义：块内逐列精细补偿，块间批量补偿，兼顾精度与速度。源码注释指明参考了 OBQ 论文第 3.4 节（arXiv 2203.07259）。

**返回量化结果**：

[src/llmcompressor/modifiers/gptq/gptq_quantize.py:255-264](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/gptq_quantize.py#L255-L264) —— 返回 `loss`（重构误差，越小越好）与一个字典，含 `weight`（量化后权重）、`weight_scale`、`weight_zero_point`，以及可选的 `weight_global_scale`（NVFP4 等需要）与 `weight_g_idx`（分组 actorder 需要）。这个字典在 `compress_module_list` 里被 `update_offload_parameter` 写回模块（[base.py:342-343](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L342-L343)）。

**`compress_module_list` —— 调用 quantize_weight 并写回**：

[src/llmcompressor/modifiers/gptq/base.py:320-343](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L320-L343)。注意 [L336](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L336-L336)：传给 `quantize_weight` 的 `hessian` 是 `_hessians[module] / _num_samples[module]`——H 被总 token 数归一化。`blocksize=self.block_size`、`percdamp=self.dampening_frac` 在这里完成参数映射。

#### 4.3.4 代码实践

**实践目标**：直接调用 `quantize_weight`，对比不同 `dampening_frac`（即 `percdamp`）对重构误差 `loss` 的影响。这个实践不需要完整模型或校准数据，是理解阻尼最直接的方式。

**操作步骤**：参考官方单元测试 [`tests/llmcompressor/modifiers/gptq/test_gptq_quantize.py`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/modifiers/gptq/test_gptq_quantize.py) 的写法，运行下面脚本：

```python
# 示例代码：对比不同 dampening_frac 的 GPTQ 重构误差
import torch
from compressed_tensors.quantization import QuantizationArgs, QuantizationScheme
from llmcompressor.modifiers.gptq.gptq_quantize import make_empty_hessian, quantize_weight
from llmcompressor.modifiers.quantization.calibration import initialize_observer, observe

torch.manual_seed(0)
module = torch.nn.Linear(64, 32, bias=False)        # in_features=64
quant_args = QuantizationArgs(num_bits=4, symmetric=True,
                              strategy="group", group_size=128, actorder=None)
module.quantization_scheme = QuantizationScheme(targets=["Linear"], weights=quant_args)
initialize_observer(module, "weight")
observe(module, "weight")                            # 算 scale/zero_point

# 构造一个非平凡但可逆的 Hessian（对角占优 + 随机扰动）
H = make_empty_hessian(module)
H += torch.diag(torch.linspace(1, 2, steps=H.shape[0]))
H += 0.01 * torch.randn_like(H)

for percdamp in [1e-6, 0.01, 0.5, 10.0]:            # 从极小到极大
    loss, _ = quantize_weight(module=module, quant_args=quant_args,
                              hessian=H.clone(), percdamp=percdamp)
    print(f"dampening_frac={percdamp:<8} -> GPTQ loss={loss:.6f}")
```

**需要观察的现象**：

- `dampening_frac` 过小（如 `1e-6`）时，可能触发 [L158-L163](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/gptq_quantize.py#L157-L164) 的 `LinAlgError` 兜底，打印警告并退化为 RTN，`loss` 偏大。
- `dampening_frac` 在一个适中区间（如默认 `0.01`）时，`loss` 最小（补偿生效）。
- `dampening_frac` 过大（如 `10.0`）时，\(H\) 被阻尼拉向单位矩阵，补偿失效，`loss` 再次增大、趋近 RTN。

**预期结果**：得到一条「U 形」的 dampening_frac–loss 曲线，中间存在较优区间。这解释了为什么默认值是 `0.01`，以及调试 GPTQ 数值不稳定时「调大 dampening_frac」为何有效。**待本地验证**：由于 Hessian 是手工构造的，绝对数值会随 seed 变化，关注相对趋势即可。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `quantize_weight` 在求逆失败时要退化为单位矩阵（`Hinv = eye(...)`）？退化后 GPTQ 退化成了什么算法？

> **答案**：当 Hessian 奇异或病态时无法可靠求逆，强行求逆会得到 NaN/Inf。退化为单位矩阵后，误差补偿项 \((w-q)/d \cdot H^{-1}_{i,i:}\) 中 \(H^{-1}\) 只有对角为 1、其余为 0，于是补偿只作用于当前列、不再分摊到其他列——即不再做跨列补偿，等价于每列独立地 RTN 取整。

**练习 2**：`block_size` 设得很大（比如等于 `in_features`）和设得很小（比如 1）分别会怎样？

> **答案**：`block_size` 越大，块内逐列补偿越多、块间批量补偿越少，理论上精度越好，但块内循环更长、速度略慢；`block_size=1` 退化为纯逐列（块内只有一列、块间每次补偿一列），仍正确但循环开销大。128 是经验上的良好折衷，精度损失可忽略而速度可观。

---

### 4.4 actorder 激活顺序与 g_idx 分组映射

#### 4.4.1 概念说明

`actorder`（activation ordering，激活顺序）控制 **权重的列以什么顺序被量化**。直觉：先量化「重要」的列（Hessian 对角线大、即对输出影响大的列），让它们的误差被后续大量列充分补偿；最后量化「不重要」的列，它们就算误差没人补偿也无所谓。

按 \(H\) 对角线从大到小排序列号，就是激活排序：

\[
\mathrm{perm} = \mathrm{argsort}(\mathrm{diag}(H), \mathrm{descending}=\mathrm{True})
\]

llm-compressor 默认 `actorder="static"`（静态排序）。它的妙处在于：**排序只是改变了「量化时处理的列顺序」，量化完成后会把权重恢复成原始列顺序**，因此保存出来的模型在推理时无需任何额外开销（不需要保存 perm，也不影响 vLLM 加载）。这正注释里说的「best accuracy recovery with no runtime cost」。

> 历史参数提示：`"group"` / `"dynamic"` 已废弃，源码里仍有兼容处理（见下方）。日常使用保持默认 `static` 即可。

`g_idx`（group index）只在 **分组量化**（strategy 为 `GROUP`/`TENSOR_GROUP`/`BLOCK`）时出现：它是一张「权重列号 → 该列所属分组号」的表，长度等于 `in_features`。GPTQ 重排列顺序后，原生的列号到分组号的映射会乱掉，需要 `g_idx` 告诉推理端每一列到底用哪组 scale/zero_point。

#### 4.4.2 核心流程

```text
quantize_weight 开头：
  若 actorder（非 None）：
    _apply_activation_ordering(W, H)
      └─ perm = argsort(diag(H), desc=True)
      └─ W = W[:, perm]；H = H[perm][:, perm]   # 同步重排 W 与 H 的列/行
  ...（求 Hinv、逐块量化，期间 W 一直保持重排后的列顺序）...
quantize_weight 结尾：
  若 actorder：
    invperm = argsort(perm)
    W = W[:, invperm]                            # 恢复原始列顺序
  若 actorder == GROUP：保存 weight_g_idx = g_idx[invperm]
```

`actorder` 的取值与含义（来自 `compressed_tensors.quantization.ActivationOrdering`）：

| 取值 | 含义 | 是否产生 g_idx |
| --- | --- | --- |
| `None` | 不排序，按原始列顺序量化 | 否 |
| `STATIC`（默认） | 按 diag(H) 排序，量化后恢复原序 | 否（推理零开销） |
| `WEIGHT` | 按权重幅值排序（同样恢复原序） | 否 |
| `GROUP`（已废弃） | 分组级动态排序，需保存 g_idx | 是 |

#### 4.4.3 源码精读

**`_apply_activation_ordering`**：

[src/llmcompressor/modifiers/gptq/gptq_quantize.py:267-278](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/gptq_quantize.py#L267-L278) —— 就是上面的 `perm = argsort(diag(H), descending=True)`，然后对 W 按列重排、对 H 做行列双重重排（`H[perm][:, perm]`，因为 H 同时牵涉输入维度的行与列）。

**actorder 的应用点**：

[src/llmcompressor/modifiers/gptq/gptq_quantize.py:106-114](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/gptq_quantize.py#L106-L114) —— 进入量化前若有 actorder 就重排；若为 `GROUP` 还要 `observer.delete_statistics` 后用重排权重重新 observe，以便拿到正确的分组 scale。

**g_idx 的构造**：

[src/llmcompressor/modifiers/gptq/gptq_quantize.py:117-133](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/gptq_quantize.py#L117-L133) —— 对分组策略，`g_idx = arange(num_columns) // divisor`（`divisor` 即 group_size 或 block 宽度）。逐列量化时，[L200-L215](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/gptq_quantize.py#L200-L215) 用 `g_idx[column_idx]` 查出当前列属于哪个分组，取对应 scale/zero_point 切片做 `fake_quantize`。

**恢复原序与导出 g_idx**：

[src/llmcompressor/modifiers/gptq/gptq_quantize.py:247-264](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/gptq_quantize.py#L247-L264) —— `invperm = argsort(perm)` 把权重恢复成原始列顺序；只有 `actorder==GROUP` 时才把 `weight_g_idx = g_idx[invperm]` 放进返回字典。

**`resolve_actorder` —— 默认值解析**：

[src/llmcompressor/modifiers/gptq/base.py:139-194](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L139-L194)。GPTQModifier 把 `actorder` 默认设为 `Sentinel("static")`（一个哨兵，[L129](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L129-L129)），`resolve_actorder`（[L142-L158](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L142-L158)）负责把哨兵解析成真正的 `ActivationOrdering.STATIC`，并处理「modifier 级 actorder 与 scheme 内 actorder 冲突」的报错。它还校验 `GROUP` 只能配 `GROUP`/`TENSOR_GROUP` 策略，否则回退为 `None`（[L180-L192](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L180-L192)）。

#### 4.4.4 代码实践

**实践目标**：用单元测试同款脚手架，对比不同 `actorder` 的输出差异，验证「只有 GROUP 才产生 g_idx」。

**操作步骤**：以下改编自官方测试 [`test_quantize_weight_group_strategy_actorder`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/modifiers/gptq/test_gptq_quantize.py#L21-L60)：

```python
# 示例代码
import torch
from compressed_tensors.quantization import ActivationOrdering, QuantizationArgs, QuantizationScheme
from llmcompressor.modifiers.gptq.gptq_quantize import make_empty_hessian, quantize_weight
from llmcompressor.modifiers.quantization.calibration import initialize_observer, observe

for actorder in [None, ActivationOrdering.STATIC, ActivationOrdering.GROUP]:
    module = torch.nn.Linear(8, 6, bias=False)
    quant_args = QuantizationArgs(num_bits=4, symmetric=True,
                                  strategy="group", group_size=2, actorder=actorder)
    module.quantization_scheme = QuantizationScheme(targets=["Linear"], weights=quant_args)
    initialize_observer(module, "weight"); observe(module, "weight")

    H = make_empty_hessian(module)
    H += torch.diag(torch.arange(1, H.shape[0] + 1, dtype=H.dtype))   # 非平凡对角，确保排序生效
    loss, q = quantize_weight(module=module, quant_args=quant_args, hessian=H.clone())
    print(f"actorder={str(actorder):<24} loss={loss:.5f} g_idx? {'weight_g_idx' in q}")
```

**需要观察的现象**：

- 只有 `actorder=GROUP` 时输出 `g_idx? True`（与 [test_gptq_quantize.py:59-60](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/modifiers/gptq/test_gptq_quantize.py#L59-L60) 的断言一致）。
- `None` 与 `STATIC` 的 `loss` 通常不同，`STATIC` 因优先量化重要列而更小。
- 任意 actorder 下，`q["weight"].shape` 都等于 `module.weight.shape`（因为最终都恢复了原始列顺序）。

**预期结果**：直观印证「static 排序能降低重构误差且不引入 g_idx 开销」。**待本地验证**：绝对 loss 随实现细节变化。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `actorder=static` 能做到「no runtime cost」？

> **答案**：因为 static 只是在量化过程中临时改变列处理顺序，量化结束后 [L247-L250](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/gptq_quantize.py#L247-L250) 用 `invperm` 把权重恢复成了原始列顺序，且不保存 perm、也不产生 g_idx。保存出来的模型与「不排序」的模型结构完全一致，vLLM 加载与推理时无需任何额外查找或重排，故零运行时开销。

**练习 2**：在 `strategy="channel"`（逐通道）下，`actorder=GROUP` 会发生什么？

> **答案**：会被回退成 `None`。`GROUP` 排序依赖「分组」结构才有意义，而 channel 策略每个输出通道自成一组、没有可排序的分组概念。[L96-L104](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/gptq_quantize.py#L96-L104) 会打印警告并 `actorder = None`，[test_quantize_weight_channel_actorder_weight](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/modifiers/gptq/test_gptq_quantize.py#L100-L132) 也印证了 channel 下不产生 g_idx。

---

## 5. 综合实践

把本讲的四个最小模块串起来，完成一个完整的 GPTQ 量化对比实验，对应规格里的实践任务。

**任务**：用 `GPTQModifier` 对一个小模型做 int4（group=128）量化，分别设置 `dampening_frac` 高低两档，比较量化后的权重重构误差或下游指标变化。

**操作步骤**：

1. 参考 [`examples/quantization_w4a16/llama3_example.py`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/examples/quantization_w4a16/llama3_example.py) 与 [`examples/quantization_w4a16/README.md`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/examples/quantization_w4a16/README.md) 的流程（加载模型 → 准备校准数据 → `GPTQModifier(targets="Linear", scheme="W4A16", ignore=["lm_head"])` → oneshot → save）。

2. 跑两遍，仅改 `dampening_frac`：

```python
# 示例代码（节选关键差异）
from llmcompressor import oneshot
from llmcompressor.modifiers.gptq import GPTQModifier

for damp in [0.01, 1.0]:                       # 低档（默认）vs 高档
    recipe = GPTQModifier(targets="Linear", scheme="W4A16",
                          ignore=["lm_head"], dampening_frac=damp)
    oneshot(
        model="<小模型 id>",
        dataset="open_platypus",
        recipe=recipe,
        num_calibration_samples=64,            # 正式量化建议 512+
        max_seq_length=2048,
        output_dir=f"./gptq-w4a16-damp{damp}",
    )
```

3. **比较指标**（任选其一，按你的环境能力）：
   - **轻量**：对每个 Linear，比较量化前后权重的相对误差 \(\lVert W - \hat W\rVert / \lVert W\rVert\) 的均值，两档对比。
   - **重量**：用 vLLM 加载两个 checkpoint，跑 lm_eval 的 gsm8k（参考 `examples/quantization_w4a16/README.md` 第 4 步命令），对比准确率。

**需要观察的现象**：

- 日志中每个 Linear 的 `Quantizing <name> using <N> samples`（[base.py:326](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L326-L326)）。
- 打开 `output_dir/config.json`，确认 `quantization_config` 里是 W4A16、group_size=128、actorder=static（印证 4.4）。
- `dampening_frac=1.0`（过大）那档，权重误差/下游指标应略差于 `0.01`（印证 4.3 的 U 形结论）。

**预期结果**：低档阻尼（0.01）整体优于高档（1.0），证明 GPTQ 的逆 Hessian 补偿在合适阻尼下生效。**待本地验证**：绝对数值依赖模型与样本，重点是两档的相对差异与 `config.json` 的量化字段是否正确。若环境无 GPU/无模型，可退回 4.3 的纯 `quantize_weight` 单元实践获得相同结论。

---

## 6. 本讲小结

- `GPTQModifier(Modifier, QuantizationMixin)` 双继承：`QuantizationMixin` 负责「搭台」（挂量化方案、observer、校准 hook、开关量化），GPTQ 自己负责「唱戏」（用 Hessian 校准权重）。
- GPTQ **永远需要校准数据**（`requires_calibration_data=True`），因为算法依赖校准激活累积 Hessian 矩阵 \(H = 2X^\top X\)；这使 oneshot 必为它选 sequential 管线。
- 校准阶段通过 `calibrate_module` forward hook，逐 batch 把输入 \(X\) 累加进 `_hessians[module]`（`accumulate_hessian`），真正的权重量化发生在 `on_sequential_epoch_end` 调用的 `quantize_weight`。
- `quantize_weight` 的核心是「阻尼 + Cholesky 求逆 → 按 `block_size` 分块逐列量化 → 用逆 Hessian 把每列/每块误差补偿到剩余列」，求逆失败时安全退化为 RTN。
- `dampening_frac`（`percdamp`）控制 Hessian 阻尼，过小数值不稳、过大补偿失效，默认 `0.01` 是经验最优区间；`actorder`（默认 `static`）按 diag(H) 排序能降低重构误差且推理零开销。
- 分组量化（GROUP/TENSOR_GROUP/BLOCK）下会生成 `g_idx`（列→分组映射），仅 `actorder=GROUP` 会把它写入最终模型。

## 7. 下一步学习建议

- **横向对比算法**：本讲只覆盖 GPTQ。建议接着读 u4-l2（AWQ 缩放变换）与 u4-l4（AutoRound），体会「先变换后量化」「逐层轻量优化」与「Hessian 补偿」三种思路的区别——它们都复用了第三单元的 `QuantizationMixin`。
- **深挖分布式与显存**：本讲刻意略过了 `compress_modules` 里的 DDP 分支（`greedy_bin_packing` / `broadcast_qparams_and_cleanup`）与 `offload_hessians` 的工程细节，它们在 u6-l1（DDP 分布式量化）与 u6-l2（大模型内存策略）详讲。
- **阅读论文与测试**：算法层面建议读 GPTQ 原文（arXiv 2210.17323）与 OBQ（arXiv 2203.07259 第 3.4 节，即源码注释引用处）；代码层面把 [`tests/llmcompressor/modifiers/gptq/test_gptq_quantize.py`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/modifiers/gptq/test_gptq_quantize.py) 与 [`tests/llmcompressor/transformers/gptq/test_gptq_oneshot.py`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/transformers/gptq/test_gptq_oneshot.py) 当作行为规格说明书，它们覆盖了各 strategy/actorder 组合的断言。
- **动手扩展**：学完 u6-l4（自定义 Modifier）后，可以尝试基于 `GPTQModifier` 派生一个自定义变体（例如自定义 Hessian 累积策略），体会双继承的扩展点。
