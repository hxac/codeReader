# AutoRound 量化

## 1. 本讲目标

本讲聚焦 `AutoRoundModifier`——llm-compressor 中接入 [AutoRound](https://aclanthology.org/2024.findings-emnlp.662.pdf) 算法的「变换/量化」类 modifier。学完本讲你应该能够：

1. 说清 AutoRound 与最朴素的 RTN（round-to-nearest）的本质区别：它把「如何取整」「如何裁剪」从贪心决策改成**少量步数的逐层优化**。
2. 掌握 `AutoRoundModifier` 的关键字段（`iters`/`batch_size`/`lr`/`device_ids`/`enable_torch_compile` 等）以及它如何接管逐层校准，把整条调用链交给外部的 `auto_round` 库。
3. 理解在 `SequentialPipeline` 中，检测到 `AutoRoundModifier` 时会被**强制关闭量化误差传播**（`propagate_error=False`）这一特殊处理的原因。

## 2. 前置知识

阅读本讲前，建议你已经掌握以下内容（对应前置讲义）：

- **量化基础（u1-l1）**：量化用 `scale` 和 `zero_point` 把高精度值映射为低位整数，误差主要来自 round（取整）。
- **RTN 与 QuantizationModifier（u3-l1）**：RTN 就是「就近取整」，`QuantizationModifier` 自身只做 RTN；取整发生在 `on_sequential_epoch_end` 钩子里。
- **QuantizationMixin（u3-l2）**：mixin 只负责给模块挂 `quantization_scheme`、开关量化、挂卸校准 hook，**不自己算 scale/zero_point**，真正取整由子类在 `on_sequential_epoch_end` 显式完成。本讲的 `AutoRoundModifier` 正是混入 `QuantizationMixin` 的子类。
- **Modifier 基类生命周期（u2-l3）**：校准链 `on_calibration_start` → `on_sequential_epoch_end` → `on_calibration_end`。
- **SequentialPipeline（u3-l5）**：把模型切成子图，每个子图跑两遍前向——第一遍 Calibrating 收集统计、触发钩子做量化；第二遍 Propagating（仅当 `propagate_error=True`）把量化后输出喂给下一层，使误差跨层累积。

一个贯穿本讲的关键事实：**AutoRound 算法本身（V/α/β 三个可学习参数 + SignSGD 优化）实现在外部 Python 包 [`auto_round`](https://github.com/intel/auto-round) 中**。llm-compressor 的 `AutoRoundModifier` 只是一个「适配器」——它在 llmc 的生命周期里准备好逐层的输入，把每一层交给 `auto_round` 去优化，再把产出的量化参数按 llmc 的命名约定回写。因此你会看到本讲的源码大量是「包装 / 映射 / 设备搬运」，而不是量化数学本身。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/llmcompressor/modifiers/autoround/base.py` | `AutoRoundModifier` 主体：字段、四钩子生命周期、`apply_autoround` 逐层优化、方案映射、量化参数回写。本讲核心。 |
| `src/llmcompressor/modifiers/autoround/utils.py` | GPU 分组分布式初始化（`init_gpu_group_dist`）与 attention mask 归一化（`fix_attention_mask`）。 |
| `src/llmcompressor/pipelines/sequential/pipeline.py` | `SequentialPipeline` 中检测到 `AutoRoundModifier` 时强制 `propagate_error=False` 的特殊分支（学习目标 3）。 |
| `examples/autoround/quantization_wNa16/llama3_example.py` | 官方 W4A16 AutoRound 量化示例，本讲代码实践的依据。 |
| `tests/llmcompressor/modifiers/autoround/test_base.py` | 单元测试，帮助理解方案映射、`apply_autoround` 调用形态。 |

---

## 4. 核心概念与源码讲解

### 4.1 AutoRound 的核心思想与 AutoRoundModifier 总览

#### 4.1.1 概念说明

先回顾 RTN 的局限。对一个权重张量 \(W\)，RTN 这样量化：

\[
\text{scale} = \frac{\max(W)-\min(W)}{2^b-1}, \qquad
W_q = \text{round}(W / \text{scale})
\]

这里的 `round`（向上还是向下取整）是**每个权重各自贪心地就近**，`scale` 由全局 min/max 决定。这种「局部最优」在低比特（尤其是 sub-4-bit）下误差会迅速放大，因为少数离群值会把 `scale` 撑大、拖累大多数普通权重。

AutoRound 的核心思想是把上面两个「拍脑袋」的决定**改成可学习的**：

- **取整权重 V**：每个权重学一个连续的「取整倾向」，决定它更该向上还是向下。
- **裁剪范围 α、β**：学一个 clip 的上下界，主动牺牲少数离群值来换取更紧的 `scale`，降低大多数权重的量化误差。

这三类参数（V、α、β）用 **SignSGD（带符号梯度下降）** 在**少量步数**（默认 `iters=200`）内、以**单个解码层的块级重建误差**为损失做优化：

\[
\mathcal{L} = \big\|\, f_\ell(X) - f_\ell^{\,q}\!\left(X;\, V,\alpha,\beta\right) \big\|^{2}
\]

其中 \(f_\ell(X)\) 是该层全精度输出，\(f_\ell^{\,q}\) 是量化权重后的输出。参数更新只取梯度的符号：

\[
\theta \leftarrow \theta - \eta \cdot \mathrm{sign}\!\left(\nabla_\theta \mathcal{L}\right)
\]

优化结束后，再把 V、α、β 烘焙（bake）成最终的整数权重和 scale。这样 AutoRound 就「用极少量的逐层优化，换来了远好于 RTN 的低比特精度」——它本质仍是**训练后量化（PTQ）**，但给取整与裁剪做了一点「微调」。

> 通俗类比：RTN 像「每个学生自己估分四舍五入」，AutoRound 像「老师用整卷总分当目标，微调每个学生的进位方向，让总分最接近真实分」。

#### 4.1.2 核心流程

`AutoRoundModifier` 把上面的算法挂到 llmc 的校准链上，整体流程是：

1. **初始化**：给命中模块挂 `quantization_scheme`（复用 mixin），冻结模型全部参数（因为只优化 V/α/β 这几个新增参数，不动原权重），推断 `sequential_targets`。
2. **校准开始**：给每个解码层挂一个「输入捕获 hook」，在第一遍前向时把该层的输入缓存起来。
3. **逐层优化**：每处理完一个子图（解码层），用缓存好的输入，调用 `auto_round` 的 `quantize_block` 对这一层跑 `iters` 步优化，得到量化权重。
4. **校准结束**：卸 hook、冻结量化参数、清理缓存。

关键点：**第 2 步缓存的是「该解码层在校准前向里的真实输入」**，这正是 `auto_round` 重建损失所需的数据；llmc 用自己的 sequential 管线来生产这些输入，`AutoRoundModifier` 只负责把它们收集好、再喂给 `auto_round`。

#### 4.1.3 源码精读

类声明——双继承 `Modifier`（生命周期）与 `QuantizationMixin`（量化字段与方案解析），与 `GPTQModifier`、`QuantizationModifier` 同构：

> [base.py:104-104](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/autoround/base.py#L104-L104) — `class AutoRoundModifier(Modifier, QuantizationMixin)`，声明该 modifier 既是生命周期参与者，又具备量化能力。

类 docstring 直接点明算法与生命周期，是本讲最权威的「设计意图」出处：

> [base.py:106-165](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/autoround/base.py#L106-L165) — docstring：算法为「SignSGD + block-wise loss 优化 rounding 与 clipping，几步即可」，并明确列出四个钩子各自干什么（`on_initialize` 应用配置、`on_calibration_start` 挂输入捕获 hook、`on_sequential_epoch_end` 跑 `apply_autoround`、`on_calibration_end` 卸 hook + 冻结）。

`requires_calibration_data` 被硬编码为 `True`——AutoRound 必须有校准数据来算重建损失，因此 oneshot 会为它选 `sequential` 管线：

> [base.py:167-167](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/autoround/base.py#L167-L167) — `requires_calibration_data: bool = True`，决定管线推断结果（参见 u3-l4）。

AutoRound 专有参数（区别于普通量化 modifier 的部分）：

> [base.py:169-175](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/autoround/base.py#L169-L175) — `iters=200`（每层调优步数）、`enable_torch_compile=True`（用 `torch.compile` 加速调优循环）、`batch_size=8`、`lr=None`（为 `None` 时由 `auto_round` 按 `1.0/iters` 自动设）、`device_ids=None`（多卡层分发）、`disable_opt_rtn=False`。

此外它仍继承 mixin 的 `targets/ignore/scheme/config_groups/kv_cache_scheme` 五个量化字段（u3-l1 已讲），以及一个私有缓存字段：

> [base.py:178-179](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/autoround/base.py#L178-L179) — `_all_module_input`（各解码层被捕获的输入）与 `_q_input`（跨层传递的「量化后输入」，见 4.2）。

#### 4.1.4 代码实践

**实践目标**：熟悉 `AutoRoundModifier` 的参数空间，并把它与官方推荐配置对上。

**操作步骤**：

1. 打开 [examples/autoround/README.md](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/examples/autoround/README.md)，找到「Key Parameters」与「Quantization Configurations」两张表。
2. 对照 [base.py:169-175](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/autoround/base.py#L169-L175) 的默认值，把 README 里 `default / best / light / fast` 四种模式各自对应的 `iters`、`batch_size`、`lr` 填进下表（这里给出骨架，请自己补全）：

   | 模式 | iters | batch_size | lr | 校准样本数 | 适用场景 |
   |------|-------|------------|----|-----------|---------|
   | default | 200 | 8 | Auto | 128 | 通用 |
   | best | ? | ? | ? | ? | 生产、精度优先 |
   | light | ? | ? | ? | ? | 快速迭代 |
   | fast | ? | ? | ? | ? | 显存受限 |

**需要观察的现象**：`iters` 越大、`batch_size` 越大、校准样本越多，精度越好但越慢越耗显存。

**预期结果**：能说出「精度优先选 `best`，速度优先选 `light/fast`」并解释每个参数对应的源码字段。本实践为源码阅读型，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `AutoRoundModifier.requires_calibration_data` 必须是 `True`，而 `QuantizationModifier` 做 `FP8_DYNAMIC` 时却可以是 `False`？

**参考答案**：AutoRound 的重建损失必须有真实校准样本计算 \(f_\ell(X)\)，没有数据就无法优化 V/α/β；而 `FP8_DYNAMIC` 属于 RTN（权重静态取整、激活动态算 scale），不需要校准数据，故 `requires_calibration_data` 由 scheme 自动推断为 `False`（见 u3-l1）。

**练习 2**：AutoRound 优化的是模型本身的权重吗？

**参考答案**：不是。`on_initialize` 里会把模型全部参数 `requires_grad_(False)`（[base.py:203-204](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/autoround/base.py#L203-L204)）。被优化的是 `auto_round` 内部新增的 V/α/β；优化完再烘焙成整数权重。

---

### 4.2 apply_autoround：逐层调起 auto_round 优化

#### 4.2.1 概念说明

`auto_round` 库的 `AutoRound` 类期望接收一个「完整的 HuggingFace 风格模型」、并逐个 `model.layers[i]`（解码层）做调优。但 llmc 的 sequential 管线一次只把**一个子图（通常一个解码层）**交给 modifier。于是 `AutoRoundModifier.apply_autoround` 做了三件适配：

1. **包装**：把单个解码层塞进一个伪造的「`model.layers = [layer]`」结构（`_wrap_decoding_layer`），骗过 `auto_round` 的模型假设。
2. **喂缓存输入**：把校准期捕获的该层输入喂给 `ar.quantize_block`，让 `auto_round` 在这些输入上跑 `iters` 步 SignSGD。
3. **回写**：`auto_round` 产出的量化参数用的是它自己的命名（`scale`/`act_scale`/`weight_global_scale`/`act_max`），llmc 需要把它们映射回 compressed-tensors 的命名（`weight_scale`/`input_scale`/`weight_global_scale`/`input_global_scale`），否则后续保存与 vLLM 加载会失败。

此外，`auto_round` 支持**跨层传递「量化后输入」**（`q_input`）：第 \(\ell\) 层优化完后，它把量化后的输出作为第 \(\ell+1\) 层的输入提示，使每层的重建误差更贴近「上一层已被量化」的真实分布。这层「层间耦合」是 `auto_round` 自己管的，**与 llmc 的 `propagate_error` 是两回事**（见 4.3）。

#### 4.2.2 核心流程

完整逐层优化流程（伪代码）：

```
on_calibration_start:                          # 校准开始
    start_calibration(model)                   #   挂 scheme + 开量化（注意：不挂 observer！）
    for each decoding_layer:                   #   给每个解码层挂「输入捕获 hook」
        register_hook(forward_pre, input_capture_hook)

# ---- sequential 管线第一遍前向发生在这里 ----
# 每个 batch 前向时，input_capture_hook 把 decoding_layer 的输入追加到 _all_module_input[name]

on_sequential_epoch_end(modules):              # 每个子图（解码层）前向完
    apply_autoround(state, modules):
        layer = 唯一的 decoding_layer
        wrapped = _wrap_decoding_layer(layer)          # 包成伪模型
        scheme  = _mapping_config_to_autoround()       # llmc scheme -> auto_round scheme
        ar = AutoRound(model=wrapped, scheme=scheme, iters=..., ...)
        q_input, _ = ar.quantize_block(                # 真正跑 iters 步优化的地方
            block=layer, inputs=captured_inputs, q_input=self._q_input, ...
        )
        self._q_input = q_input                        # 传给下一层
        layer = _unwrapper_quantized_layer(layer)      # 拆掉 auto_round 的 wrapper
        _postprocess_qparams(layer, ...)               # 命名映射 + 回写 llmc 量化参数
    post_autoround_cleanup(): _all_module_input.clear()
```

注意一个细节：`start_calibration` 被**重写**了，它**跳过了 observer 的注册**——因为 AutoRound 不用 llmc 的 observer/min-max 统计那一套，它自己用重建损失来调。它只做 `apply_calibration_status`（打状态标记）和 `enable_quantization`（开量化）。

#### 4.2.3 源码精读

`input_capture_hook`——把每个解码层每次前向的 `(args, kwargs)` 追加缓存，这就是喂给 `auto_round` 的校准数据来源：

> [base.py:227-230](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/autoround/base.py#L227-L230) — `input_capture_hook`：按 `_tmp_name`（临时名）分桶缓存输入。

`on_calibration_start`——先 `start_calibration`，再给所有「解码层」挂输入捕获 hook（`forward_pre`，即前向前抓输入）：

> [base.py:232-241](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/autoround/base.py#L232-L241) — 判断解码层用 `_is_decoding_layer`（类名在 `sequential_targets` 里）。

`start_calibration` 的重写——显式注释「skip register observers for auto-round」，并直接 `enable_quantization`：

> [base.py:211-225](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/autoround/base.py#L211-L225) — 与 mixin 默认实现不同：不挂 observer、只打标记 + 开量化。

`on_sequential_epoch_end`——极薄，转交 `apply_autoround` + 清理：

> [base.py:243-247](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/autoround/base.py#L243-L247) — 注意 `modules` 是管线透传来的「本子图模块」，**不做任何 `is_module_quantized` 过滤**（见 4.2.5 练习的回归测试背景）。

`apply_autoround` 的算法直觉写在 docstring 里，是理解整段实现的最佳入口：

> [base.py:253-264](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/autoround/base.py#L253-L264) — 用 `for iter in range(iters)` 的伪代码点明：前向算量化输出 → MSE 损失 → 反传 → `optimizer.step()` → 记录最佳参数。

把单个解码层包成伪模型，让 `auto_round` 误以为收到一个完整模型：

> [base.py:59-64](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/autoround/base.py#L59-L64) — `_wrap_decoding_layer` 构造 `_PretrainModelWrapper(model=_LLModelWrapper(layers=[layer]))`。

真正跑优化的核心调用——`ar.quantize_block`，并把跨层 `q_input` 接力：

> [base.py:336-343](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/autoround/base.py#L336-L343) — `q_input, _ = ar.quantize_block(block=decoding_layer, inputs=ar_inputs, q_input=self._q_input, ...)`；返回的新 `q_input` 存入 `self._q_input` 供下一层用。

方案映射入口——把 llmc 的 `scheme` 翻译成 `auto_round` 能识别的形式：

> [base.py:642-654](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/autoround/base.py#L642-L654) — `_mapping_config_to_autoround`：若 `scheme` 大写名在 `auto_round` 的预设表 `AR_PRESET_SCHEMES`（如 `W4A16`）中，直接返回该字符串；否则（如 `W7A16`，`auto_round` 无此预设）退化为显式构造 `ARQuantizationScheme`。

量化参数命名的映射表——`auto_round` 名 → llmc 名：

> [base.py:470-477](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/autoround/base.py#L470-L477) — `{"scale": "weight_scale", "act_scale": "input_scale", "weight_global_scale": "weight_global_scale", "act_max": "input_global_scale"}`。`_postprocess_qparams` 据此把 `auto_round` 产出的张量按 llmc 名重新注册为 `nn.Parameter`，并对 `MXFP4/MXFP8`（log2 scale 还原）与 `NVFP4`（`act_max` 特殊换算）做特判。

#### 4.2.4 代码实践

**实践目标**：用 `AutoRoundModifier` 做一次 W4A16 量化，并与相同方案下的 `GPTQModifier` 对比量化误差，体会「逐层优化」相对「Hessian 校准」的差异。

**操作步骤**（以官方示例 [examples/autoround/quantization_wNa16/llama3_example.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/examples/autoround/quantization_wNa16/llama3_example.py) 为模板）：

1. 选一个**小模型**（显存有限时推荐 `TinyLlama/TinyLlama-1.1B-Chat-v1.0`，测试用例也用它）。安装 `auto_round`（`pip install llmcompressor` 已含）。
2. 准备校准数据（示例用 `auto_round.calib_dataset.get_dataset`，llmc 也支持传 `dataset="ultrachat_200k"` 等内置名）：

   ```python
   from auto_round.calib_dataset import get_dataset
   ds = get_dataset(tokenizer=tokenizer, seqlen=2048, nsamples=128)
   ```

3. **AutoRound** 量化（注意 `iters` 调小以省时）：

   ```python
   from llmcompressor import oneshot
   from llmcompressor.modifiers.autoround import AutoRoundModifier

   recipe = AutoRoundModifier(
       targets="Linear", scheme="W4A16", ignore=["lm_head"], iters=200,
   )
   oneshot(model=model, dataset=ds, recipe=recipe,
           max_seq_length=2048, num_calibration_samples=128,
           output_dir="./TinyLlama-W4A16-AutoRound")
   ```

4. **GPTQ** 量化相同方案作对比：

   ```python
   from llmcompressor.modifiers import GPTQModifier

   recipe = GPTQModifier(
       targets="Linear", scheme="W4A16", ignore=["lm_head"],
   )
   oneshot(model=model, dataset=ds, recipe=recipe,
           max_seq_length=2048, num_calibration_samples=128,
           output_dir="./TinyLlama-W4A16-GPTQ")
   ```

5. 比较两者（任选其一）：
   - **权重误差**：加载两个 checkpoint，取某层 `q_proj` 的 `weight` 反量化后与原模型算 MSE。
   - **简单指标**：用相同 prompt 跑 `model.generate`，肉眼看生成质量；或跑一个轻量困惑度（perplexity）。

**需要观察的现象**：日志里会看到逐层打印 `Applying AutoRound on layer <name>`（来自 [base.py:277](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/autoround/base.py#L277-L277)）；GPTQ 则打印 Hessian 累积相关信息。两者最终 `config.json` 的 `quantization_config` 都应是 W4A16、group_size=128。

**预期结果**：在 1B 量级、4bit 下，AutoRound 与 GPTQ 精度接近；若把 scheme 换成 `W2A16`（sub-4-bit），AutoRound 通常明显更优（README 的「When to Use」一节有数据）。

**如果无法确定运行结果**：本实践依赖 GPU 与 `auto_round` 环境，若无条件运行，请明确标注「待本地验证」，并可退化为**源码阅读型实践**——阅读 [test_base.py:168-219](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/modifiers/autoround/test_base.py#L168-L219)，说明 `apply_autoround` 是如何把缓存输入 `inputs` 透传给 `quantize_block` 的（断言 `quantize_inputs[0][0][0][0].device` 与设备一致）。

#### 4.2.5 小练习与答案

**练习 1**：`self._q_input` 在层与层之间传递的作用是什么？为什么 llmc 关掉了 `propagate_error`（见 4.3），AutoRound 却还能让「上一层量化影响下一层」？

**参考答案**：`_q_input` 是 `auto_round` 内部的「量化后输入」接力（[base.py:339-343](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/autoround/base.py#L339-L343)）。llmc 的 `propagate_error=False` 关的是「sequential 管线层面的两遍前向误差传播」；而 `auto_round` 在自己的 `quantize_block` 内部用 `q_input` 独立做了层间耦合，二者互不冲突。

**练习 2**：构造 `AutoRoundModifier(scheme="W7A16")` 时，`_mapping_config_to_autoround` 走的是哪条分支？为什么？

**参考答案**：走「显式构造 `ARQuantizationScheme`」的 fallback 分支（[base.py:650-654](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/autoround/base.py#L650-L654)）。因为 `W7A16` 不在 `auto_round` 的 `AR_PRESET_SCHEMES` 里（[test_base.py:89-90](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/modifiers/autoround/test_base.py#L89-L90) 断言 `"W7A16" not in AR_PRESET_SCHEMES`），故 llmc 用兼容映射自行把 `num_bits=7` 等字段翻译过去。

**练习 3**：[test_base.py:31-48](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/modifiers/autoround/test_base.py#L31-L48) 这个回归测试在防什么 bug？

**参考答案**：防止 `on_sequential_epoch_end` 用 `is_module_quantized` 过滤掉解码层、导致 `apply_autoround` 收不到任何层而**静默变成 no-op**。现在 `on_sequential_epoch_end` 不做过滤、直接把全部 `modules` 透传给 `apply_autoround`（[base.py:243-247](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/autoround/base.py#L243-L247)）。

---

### 4.3 SequentialPipeline 对 AutoRound 的不传播误差处理

#### 4.3.1 概念说明

回顾 u3-l5：`SequentialPipeline` 默认对每个子图跑**两遍前向**——第一遍 Calibrating（开校准 hook，触发钩子完成量化），第二遍 Propagating（仅当 `propagate_error=True`，关 hook，把量化后输出喂给下一层，使误差跨层累积）。这套机制对 GPTQ、W8A8、SmoothQuant 等算法很关键，因为它们需要「上一层的真实量化误差」作为下一层校准的输入。

但 AutoRound 不一样：**它每层都用自己的缓存输入做独立优化**（`quantize_block` 内部会自行迭代到收敛，并通过 `q_input` 做层间耦合）。如果再叠加 llmc 的 `propagate_error`，就会造成「双重层间耦合」——既多余又可能互相干扰。因此 sequential 管线在**检测到 recipe 里含 `AutoRoundModifier`** 时，会**强制把 `propagate_error` 关掉**。

#### 4.3.2 核心流程

在 `SequentialPipeline.__call__` 开头（切子图之前）：

```python
modifiers = session.lifecycle.recipe.modifiers
if any(type(m).__name__ == "AutoRoundModifier" for m in modifiers):
    dataset_args.propagate_error = False
```

关掉之后，主循环里只剩第一遍 Calibrating 前向（`propagate_error` 为假的分支），不再进入第二遍 Propagating 前向。也就是说：**AutoRound 下，每个子图只跑一遍前向**（用来触发输入捕获 hook 和 `on_sequential_epoch_end`），省时省显存，层间耦合交给 `auto_round` 的 `q_input`。

#### 4.3.3 源码精读

强制关闭 `propagate_error` 的判定与注释（解释了「为什么」）：

> [sequential/pipeline.py:89-94](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L89-L94) — 注释明确：「AutoRoundModifier 逐层独立优化、用自己的前向，因此校准阶段不应在层间传播量化误差」。

主循环里 `propagate_error` 为假时的分支（更新缓存、删消费完的中间值，**不跑第二遍**）：

> [sequential/pipeline.py:155-160](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L155-L160) — `if not dataset_args.propagate_error:` 把输出写回缓存，并在 `subgraph_index < num_subgraphs - 1` 时删掉已消费的中间激活。

对照：`propagate_error` 为真时才会进入的第二遍前向（AutoRound 永远不会走到这里）：

> [sequential/pipeline.py:162-165](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L162-L165) — `if dataset_args.propagate_error:` 这段用 `HooksMixin.disable_hooks()` 关掉钩子，纯粹为捕获压缩模块的量化后输出。

#### 4.3.4 代码实践

**实践目标**：理解「含 AutoRound 的 recipe 会让 sequential 管线少跑一遍前向」。

**操作步骤**（源码阅读型）：

1. 打开 [sequential/pipeline.py:89-94](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L89-L94)，确认判定用的是 `type(m).__name__ == "AutoRoundModifier"`（按类名字符串匹配，而非 `isinstance`——因为 `AutoRoundModifier` 不一定被 import 到该模块作用域）。
2. 对照 [sequential/pipeline.py:136-174](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L136-L174) 的主循环，画出 `propagate_error=False` 与 `=True` 时各自执行的代码路径。

**需要观察的现象**：`propagate_error=False` 时只执行 `Calibrating` 这一遍前向 + `sequential_epoch_end`；`propagate_error=True` 时额外多一遍 `Propagating` 前向。

**预期结果**：能回答「AutoRound 量化时，每个子图只前向一遍」并指出对应代码行。

#### 4.3.5 小练习与答案

**练习 1**：如果有人强行绕过这段判定、让 AutoRound 在 `propagate_error=True` 下运行，会出现什么问题？

**参考答案**：会多跑一遍无用的 Propagating 前向（关 hook 的那遍），浪费时间与显存；同时与 `auto_round` 自己的 `q_input` 层间耦合叠加，可能导致每层优化的输入分布与最终推理不一致，反而损害精度。因此源码用「检测到就强制关」来保护用户。

**练习 2**：为什么这里用 `type(m).__name__ == "AutoRoundModifier"` 而不是 `isinstance(m, AutoRoundModifier)`？

**参考答案**：避免把 `AutoRoundModifier` import 进 `pipelines/sequential/pipeline.py`（防止反向依赖、循环导入）。按类名字符串匹配是 llmc 里跨包「松耦合识别」的常见手法。

---

### 4.4 utils：GPU 分组与 attention mask 归一化

#### 4.4.1 概念说明

`autoround/utils.py` 解决两类「让 `auto_round` 在 llmc 生态里跑得起来」的杂项问题：

1. **GPU 分组（rank-local GPU grouping）**：标准 DDP 是「一个进程绑一张卡」。但 AutoRound 调优一个解码层时，可以让**一组 GPU 协作**（`LLMCOMPRESSOR_GPUS_PER_GROUP` 环境变量控制组大小，默认 1）。这时进程组要绑到「`LOCAL_RANK * gpus_per_group`」起始的若干张卡上，而不是默认的 `LOCAL_RANK` 单卡，于是需要专门的 `init_gpu_group_dist`。
2. **attention mask 归一化**：`auto_round` 对自定义校准数据的 attention mask 有特殊要求——当 mask **全 1（没有任何位置被遮蔽）**时，它要求至少把**最后一个位置置 0**。否则其重建损失的 mask 逻辑会失效。`fix_attention_mask` 负责把各种形状（1D/2D/3D/4D，含因果 mask）的输入归一化成 `[batch, seq_len]` 并满足「至少一个被遮蔽」。

#### 4.4.2 核心流程

`init_gpu_group_dist` 的分支逻辑：

- `gpus_per_group == 1`：退化为标准 `init_dist()`，返回 `(rank, world_size, local_rank)`。
- `gpus_per_group > 1`：要求 `torchrun` 环境（`TORCHELASTIC_RUN_ID` 必须存在），把进程绑到 `main_gpu = local_rank * gpus_per_group`，按加速器类型选后端（cuda→nccl、xpu→xccl、其它→gloo），调 `dist.init_process_group` 后 `barrier`，返回 `(rank, world_size, main_gpu)`。

`fix_attention_mask` 的归一化逻辑：

1. 若是 4D 且形状为 `[b,1,1,s]`，先 squeeze 成 2D。
2. 若是 3D/4D（因果 mask），调 `_collapse_causal_attention_mask` 压成 `[b, s]`（按最后一维取 amax>global_min）。
3. 对 1D/2D：找出「全 1」的行（所有 token 都有效），把这类行的**最后一位**置 0，保证至少一个被遮蔽位置。

#### 4.4.3 源码精读

GPU 组大小读取（默认 1）：

> [utils.py:14-15](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/autoround/utils.py#L14-L15) — `get_local_gpu_group_size` 读 `LLMCOMPRESSOR_GPUS_PER_GROUP`。

rank-local 绑定的分布式初始化：

> [utils.py:18-76](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/autoround/utils.py#L18-L76) — `init_gpu_group_dist`：注释点明「AutoRound 可用一个 rank-local GPU 组调优单个解码块，故进程组须从 `LOCAL_RANK * gpus_per_group` 起始初始化」。

因果 mask 压缩：

> [utils.py:79-104](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/autoround/utils.py#L79-L104) — `_collapse_causal_attention_mask`：把高维因果 mask 压成 per-token 有效位。

attention mask 归一化入口：

> [utils.py:107-146](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/autoround/utils.py#L107-L146) — `fix_attention_mask`：核心规则是「全 1 的行/向量，把最后位置 0」；并在 [utils.py:116](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/autoround/utils.py#L116-L116) 给出了上游 `auto_round` 对应代码的参考链接。

#### 4.4.4 代码实践

**实践目标**：体会 `LLMCOMPRESSOR_GPUS_PER_GROUP` 如何影响 AutoRound 的设备分配（源码阅读型）。

**操作步骤**：

1. 阅读 [base.py:374-392](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/autoround/base.py#L374-L392) 的 `_update_device_map_for_dp`：在 DDP 已初始化、且用户没显式设 `device_ids` 时，若 `gpus_per_group > 1` 则把 `device_map` 设为「`local_rank` 起的连续若干卡」，否则设为当前进程那张卡。
2. 对照 [test_base.py:148-165](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/modifiers/autoround/test_base.py#L148-L165)：`gpus_per_group=1` 且 `current_device_index=1` 时，`device_map` 被断言为 `"cuda:1"`。

**需要观察的现象**：组大小决定 `auto_round` 的 `device_map` 字符串，进而决定它把解码层搬到哪些卡上调优。

**预期结果**：能解释「`LLMCOMPRESSOR_GPUS_PER_GROUP=2` + `LOCAL_RANK=1` → `device_map="2,3"`」的推导。本实践为源码阅读型，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `fix_attention_mask` 在「mask 全 1」时非要置掉最后一个位置？

**参考答案**：`auto_round` 用 attention mask 把被 padding 的 query 位置从重建损失里排除；当 mask 全 1（没有 padding）时，它的内部逻辑会失效（参考 [utils.py:116](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/autoround/utils.py#L116-L116) 给出的上游链接）。置掉最后一个 token 是「至少要有一个被遮蔽位置」的最低代价满足方式。

**练习 2**：`gpus_per_group > 1` 时为什么必须用 `torchrun`？

**参考答案**：rank-local 绑定需要从环境变量读 `RANK`/`LOCAL_RANK`/`WORLD_SIZE` 并自行 `init_process_group`，这套环境只有 `torchrun`（`TORCHELASTIC_RUN_ID`）才会提供。代码在 [utils.py:40-44](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/autoround/utils.py#L40-L44) 显式检查并报错。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来——用 `AutoRoundModifier` 量化一个小模型，并验证「逐层优化 + 不传播误差 + 参数回写」整条链确实生效。

**步骤**：

1. 用 `TinyLlama/TinyLlama-1.1B-Chat-v1.0` + `AutoRoundModifier(scheme="W4A16", iters=10)`（调小 `iters` 省时）跑一次 oneshot，`output_dir="./tiny-autoround"`。
2. **验证 4.1（参数生效）**：打开 `./tiny-autoround/config.json`，确认 `quantization_config` 是 W4A16、group_size=128、`lm_head` 在 `ignore` 里（参考 [test_autoround_oneshot.py:122-141](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/transformers/autoround/test_autoround_oneshot.py#L122-L141) 的断言）。
3. **验证 4.2（逐层优化）**：在运行日志里数 `Applying AutoRound on layer ...` 的行数，它应≈解码层数（TinyLlama 是 22 层）。
4. **验证 4.3（不传播误差）**：在 sequential 管线那段（[pipeline.py:89-94](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L89-L94)）临时加一行 `print(propagate_error)`（**仅用于观察，验证后删除，勿提交**），确认打印 `False`；并对比日志里只有 `Calibrating` 没有 `Propagating`。
5. **验证 4.4（参数回写正确）**：加载量化后模型，确认 `model.layers[2].self_attn.q_proj` 有 `quantization_scheme` 与 `weight_scale` 参数（说明 `_postprocess_qparams` 的命名映射生效）。

**预期结果**：四步全部通过，证明 AutoRound 通过 llmc 生命周期正确完成「逐层调优 → 回写 compressed-tensors 量化参数 → 可被 vLLM 加载」的全流程。

**如果无法确定运行结果**：步骤 2 与 5 的断言已由官方测试覆盖（`test_autoround_oneshot.py`），可直接阅读该测试理解预期；步骤 3、4 标注「待本地验证」。

## 6. 本讲小结

- AutoRound 把 RTN 里「贪心取整 + 全局 min/max 裁剪」改成**学习 V/α/β 三个参数**，用 **SignSGD + 块级重建损失** 在少量步数（默认 `iters=200`）内逐层优化，在 sub-4-bit 等低比特场景显著优于 RTN。
- `AutoRoundModifier(Modifier, QuantizationMixin)` 是把外部 `auto_round` 库接入 llmc 的**适配器**：它在 `on_calibration_start` 给解码层挂输入捕获 hook、在 `on_sequential_epoch_end` 调 `ar.quantize_block` 逐层优化、在 `on_calibration_end` 卸 hook 冻结。
- 它**重写了 `start_calibration`**：不挂 observer（AutoRound 不用 min-max 统计那套），只打标记 + 开量化；并冻结模型全部参数（只优化 V/α/β）。
- **方案映射**：`_mapping_config_to_autoround` 把 llmc scheme 翻译成 `auto_round` scheme（预设名直返、如 `W7A16` 无预设则显式构造）；**参数回写** `_postprocess_qparams` 把 `auto_round` 命名（`scale` 等）映射回 compressed-tensors 命名（`weight_scale` 等），并对 MXFP4/MXFP8/NVFP4 做特判。
- **SequentialPipeline 对 AutoRound 的特殊处理**：检测到 recipe 含 `AutoRoundModifier` 就**强制 `propagate_error=False`**，因为 AutoRound 逐层独立优化且自带 `q_input` 层间耦合，无需 llmc 再做两遍前向误差传播。
- `autoround/utils.py` 提供 GPU 分组分布式初始化（`LLMCOMPRESSOR_GPUS_PER_GROUP`）与 attention mask 归一化（全 1 时置末位 0），让 `auto_round` 在多卡与自定义数据下正确运行。

## 7. 下一步学习建议

- **对比阅读剪枝类 modifier（u4-l5）**：`SparseGPTModifier`/`WandaModifier` 同样依赖校准数据逐层处理，可与 AutoRound 对照体会「不同算法如何复用同一条 sequential 校准管线」。注意 `BasicPipeline` 经 `run_calibration` 被 SparseGPT 复用来取激活（u3-l6），这是另一条值得追踪的调用链。
- **进入规模化与工程化（u6）**：AutoRound 的 DDP/GPU 分组（本讲 4.4）是 u6-l1「DDP 分布式量化」的一个具体实例；大模型下的 `suspend_offloading`、`device_ids` 多卡层分发则通向 u6-l2「大模型内存策略」。建议接着读 u6-l1，把本讲的 `init_gpu_group_dist` 放回分布式全景里理解。
- **自定义扩展（u6-l4）**：若你想写一个「逐层优化型」的自定义 modifier，`AutoRoundModifier` 是比 `QuantizationModifier` 更完整的模板——它演示了「重写 `start_calibration`、缓存输入、调外部库、回写参数」的完整套路，可作为 u6-l4 自定义 Modifier 的范例。
