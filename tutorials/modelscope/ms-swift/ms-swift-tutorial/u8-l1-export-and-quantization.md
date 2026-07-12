# 模型导出与量化

## 1. 本讲目标

训练结束之后，模型往往以「基座 + LoRA adapter」的形态躺在 `output/vx-xxx/checkpoint-xxx` 目录里，还带着一份 `args.json`。这样的产物既不能直接给 vLLM 这类推理引擎用，体积也偏大。本讲要解决的正是「如何把训练产物变成可部署的最终模型」。

学完本讲你应该能够：

- 理解 `swift export` 的 `SwiftExport` 管道如何用一个 `run()` 方法以「分支式 if/elif」串联 merge_lora、量化、ollama、hf/mcore 转换、push_to_hub 等多条出口。
- 掌握 `merge_lora` 如何把 LoRA 增量并入基座得到一个独立完整模型，以及它为何要在「未量化」的原始模型上做合并。
- 掌握 `quantize_model` 如何用 GPTQ/AWQ/FP8/BNB 四种方法把权重量化到低比特，理解校准数据集在其中的作用。
- 了解 `export_to_ollama` 如何生成 Ollama 的 `Modelfile`，以及 hf↔mcore 权重互转如何接入 Megatron 生态。

## 2. 前置知识

在进入本讲前，你需要具备以下认知（这些都在前置讲义中建立）：

- **LoRA adapter 是什么**：训练时只外挂一个低秩增量 \(\Delta W = \frac{\alpha}{r}BA\)，基座权重冻结。推理时既可以把 adapter「挂」回基座，也可以把它「并入」基座（u5-l3）。
- **args.json 回载机制**：swift 训练时会把所有参数落盘成 `args.json`，推理/导出时按 `force_load_keys > data_keys > load_keys` 三档优先级自动回载，所以 `--adapters output/vx-xxx/checkpoint-xxx` 一条命令就够了，无需重复指定 `--model`、`--template`、`--system`（u1-l5、u2-l2）。
- **prepare_model_template**：上层管道通用的「加载模型 + 实例化模板 + 挂载 adapter」入口，返回 `(model, template)`（u5-l4）。
- **量化的直觉**：把高精度浮点权重（如 fp16）映射到低比特整数（如 int4），用「缩放因子 scale + 零点 zero_point」描述一段权重区间，从而压缩体积、降低显存。代价是引入数值误差，需要一批校准数据来估计每段权重的范围。

本讲不涉及训练流程本身，只关心「产物加工」这一段。

## 3. 本讲源码地图

本讲涉及的关键文件都集中在 `swift/pipelines/export/` 目录下，是一个职责非常内聚的小模块：

| 文件 | 作用 |
| --- | --- |
| [swift/pipelines/export/export.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/export/export.py) | `SwiftExport` 管道与 `export_main` 入口，用分支式 `run()` 派发到各导出动作。 |
| [swift/pipelines/export/merge_lora.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/export/merge_lora.py) | `merge_lora`：把 LoRA/LLaMAPro 等增量并入基座并保存为完整模型。 |
| [swift/pipelines/export/quant.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/export/quant.py) | `QuantEngine` 与 `quantize_model`：实现 AWQ/GPTQ/GPTQ_v2/BNB/FP8 量化。 |
| [swift/pipelines/export/ollama.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/export/ollama.py) | `export_to_ollama`：把对话模板翻译成 Ollama 的 `Modelfile`。 |
| [swift/arguments/export_args.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/export_args.py) | `ExportArguments`：声明 `merge_lora`/`quant_method`/`to_ollama`/`to_mcore`/`to_hf`/`push_to_hub` 等所有导出开关。 |
| [swift/megatron/convert.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/convert.py) | `convert_hf2mcore`/`convert_mcore2hf`：HF 与 Megatron-Core 权重互转（本讲只到调用入口，内部细节留到 u9-l3）。 |

> 提示：`SwiftPipeline` 基类的 `main()` 负责解析参数、设种子、计时，业务逻辑由子类的 `run()` 实现（u5-l4 已讲过模板方法模式）。本讲聚焦 `SwiftExport.run()`。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：先看 `SwiftExport` 的分支式总流程，再分别深入 `merge_lora`、`quantize_model`，最后看 ollama 与 hf/mcore 两种格式转换。

### 4.1 SwiftExport 分支式 run 流程

#### 4.1.1 概念说明

`swift export` 的设计哲学是「一个命令，多个出口」。同一条 `swift export` 命令，依据你打开的开关不同，可以：合并 LoRA、量化、生成 Ollama 配置、转 Megatron 格式、推送 hub，甚至把这些动作串起来（先合并再量化）。

`SwiftExport` 继承自 `SwiftPipeline`，其 `args_class` 绑定到 `ExportArguments`。真正干活的是它覆写的 `run()` 方法——这是一个非常扁平的、用一连串 `if/elif` 写成的派发器，没有复杂的状态机，读起来几乎像一份配置清单。

#### 4.1.2 核心流程

`run()` 的执行顺序可以概括为三段：

1. **格式兼容**（可选）：若 `--to_peft_format`，先把 swift 后端 checkpoint 转成 peft 通用格式（兼容老用法）。
2. **合并 LoRA**（可选，但可叠加）：若 `--merge_lora true`，调用 `merge_lora(args)` 把增量并入基座。合并完后会把 `args.model`/`args.model_dir` 指向合并产物，清空 `args.adapters`，使后续步骤直接消费完整模型。
3. **单一出口**（互斥多选一）：以下分支按 `if/elif` 顺序取第一个命中的执行：
   - `args.quant_method` → `quantize_model(args)`
   - `args.to_ollama` → `export_to_ollama(args)`
   - `args.to_cached_dataset` → `export_cached_dataset(args)`
   - `args.to_hf or (args.mcore_adapter and args.to_mcore)` → `convert_mcore2hf(args)`
   - `args.to_mcore` → `convert_hf2mcore(args)`
   - `args.push_to_hub` → `args.hub.push_to_hub(...)`

注意第 2 段和第 3 段的关系：合并是「可叠加的前置步骤」，量化/ollama/转换是「互斥的最终出口」。所以「合并 + 量化」「合并 + ollama」「合并 + push」都能组合，但「量化 + ollama」不能同时走。

#### 4.1.3 源码精读

整个 `run()` 只有 30 行，但信息量很大：

[swift/pipelines/export/export.py:20-50](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/export/export.py#L20-L50) — `SwiftExport.run()` 的全部分支派发逻辑，注释指出每个 `if/elif` 对应一种导出动作。

重点看合并段的「临时清空 output_dir」技巧：

```python
if args.merge_lora:
    output_dir = args.output_dir
    if args.to_peft_format or args.quant_method or args.to_ollama or args.push_to_hub:
        args.output_dir = None   # 让 merge_lora 自己生成中间目录
    merge_lora(args)
    args.output_dir = output_dir  # recover
```

当合并只是中间步骤（后面还要量化/转 ollama/push）时，把 `output_dir` 临时置 `None`，让 `merge_lora` 内部按默认规则生成一个 `xxx-merged` 临时目录，避免和最终输出目录冲突；合并完再恢复 `output_dir` 给后续步骤用。这是一个很值得学习的「临时改参数 + 事后恢复」模式。

而入口 `export_main` 则是 `SwiftPipeline` 的标准单行：

[swift/pipelines/export/export.py:53-54](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/export/export.py#L53-L54) — `export_main` 实例化 `SwiftExport` 并调 `main()`，`main()` 由基类提供（解析参数→`run()`）。

参数开关都定义在 `ExportArguments`，它多继承 `MergeArguments`（提供 `merge_lora`/`safe_serialization`/`max_shard_size`）与 `BaseArguments`：

[swift/arguments/export_args.py:51-83](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/export_args.py#L51-L83) — 声明了 `quant_method`/`to_ollama`/`to_mcore`/`to_hf`/`push_to_hub` 等所有分支开关字段，`quant_method` 被约束为 `Literal['awq', 'gptq', 'bnb', 'fp8', 'gptq_v2']`。

`__post_init__` 里还有两条关键校验：量化必须配数据集（gptq/awq 需要校准）；`to_mcore`/`to_hf` 不支持 `merge_lora`，会自动关掉并打 warning（LoRA 增量导出要用 `megatron export`）：

[swift/arguments/export_args.py:120-150](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/export_args.py#L120-L150) — 参数后置校验：quant_bits 与 quant_method 互校、gptq/awq 必须有 dataset、to_mcore/to_hf 关闭 merge_lora、to_cached_dataset 关闭 packing。

#### 4.1.4 代码实践

**实践目标**：不真正跑训练，仅通过阅读源码画出 `swift export` 的分支决策树，并验证「合并 + 量化」可以叠加而「量化 + ollama」不能。

**操作步骤**：

1. 打开 [swift/pipelines/export/export.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/export/export.py)，在 `run()` 的每个 `if/elif` 旁标注它对应的 `--xxx` 参数。
2. 构造两条「假想」命令，预测它们会依次命中哪些分支：
   - `swift export --adapters ckpt --merge_lora true --quant_method gptq --quant_bits 4 --dataset ...`
   - `swift export --model Qwen/... --quant_method gptq --to_ollama true`
3. 对照源码验证你的预测。

**需要观察的现象**：

- 第一条命令：先走 `merge_lora`（`output_dir` 被临时置 `None`），合并后 `args.model` 指向 `xxx-merged`、`args.adapters=[]`，再走 `quant_method` 分支量化合并后的模型。两个动作叠加。
- 第二条命令：`merge_lora` 段跳过，进入 `if args.quant_method` 命中量化并 `return`（elif 链终止），`to_ollama` 永远不会被触发。

**预期结果**：「合并 + 量化」是一条合法且常见的两步流水线；「量化 + ollama」中 ollama 会被量化分支短路，二者不可兼得。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `run()` 里合并段用的是独立 `if`，而后续出口用的是 `if/elif` 链？

**参考答案**：合并是「可叠加的前置加工」，需要和量化/ollama/push 等组合使用，所以用独立 `if` 不排斥后续分支；而量化、ollama、cached_dataset、mcore 转换、push 这些是「互斥的最终出口」，一次导出只选一个，用 `elif` 保证只命中第一个。

**练习 2**：若用户同时传了 `--merge_lora true --to_mcore true`，会发生什么？

**参考答案**：在 `ExportArguments.__post_init__` 中检测到 `to_mcore` 为真，会把 `merge_lora` 强制改回 `False` 并打 warning 提示「to_mcore/to_hf 不支持 merge_lora，导出 LoRA 增量请用 `megatron export`」。所以 `run()` 里合并段会被跳过，直接走 `convert_hf2mcore`。

### 4.2 merge_lora：把 LoRA 增量并入基座

#### 4.2.1 概念说明

训练得到的 LoRA adapter 是「外挂增量」：推理时基座权重 \(W\) 不变，前向时额外算 \(\Delta W x = \frac{\alpha}{r}BAx\)。这种形态好处是体积小、可插拔，坏处是很多推理引擎（尤其加速引擎）更愿意直接吃一个完整模型。

`merge_lora` 解决的就是「把增量焊死进基座」：

\[
W' = W + \Delta W = W + \frac{\alpha}{r}BA
\]

合并后得到的新权重 \(W'\) 与原基座同形状、同精度，但已经吸收了微调效果，可以像普通模型一样直接部署。合并产物只保留基座权重，不再有 adapter 文件。

#### 4.2.2 核心流程

`merge_lora(args)` 的执行步骤：

1. **决定输出目录**：若 `args.output_dir` 已被上层置 `None`（说明合并不是最终步骤），则按默认规则 `f'{args.adapters[0]}-merged'` 生成中间目录。
2. **短路复用**：若目标目录已存在且未指定 `replace_if_exists`，跳过保存（幂等）。
3. **关键安全措施**：`args.quant_method = None`——在未量化的原始模型上做合并。
4. **加载模型 + 挂 adapter**：`prepare_model_template(args)` 返回挂好 LoRA 的 `PeftModel`/`SwiftModel`。
5. **修正 tie_word_embeddings**：`check_tie_word_embeddings` 处理输入/输出 embedding 被 `ModulesToSaveWrapper` 包裹时的 tying 状态。
6. **合并并卸载**：`Swift.merge_and_unload(model)` 把增量算回基座权重，返回纯 `nn.Module`。
7. **保存**：`save_checkpoint` 写出完整权重与 processor。
8. **改写 args 指向合并产物**：`args.model`/`args.model_dir = output_dir`，`args.adapters = []`，使后续量化等步骤消费合并后的完整模型。

#### 4.2.3 源码精读

最关键的一行是第 37 行——合并必须在未量化模型上进行：

[swift/pipelines/export/merge_lora.py:27-61](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/export/merge_lora.py#L27-L61) — `merge_lora` 全流程：默认目录生成、幂等跳过、置空 quant_method、加载挂载、合并卸载、保存、改写 args 指向合并产物。

```python
# If the model is quantized, perform the merge on the original (unquantized) model.
# https://github.com/huggingface/peft/issues/2321
args.quant_method = None
```

注释引用了 peft 的 issue #2321：在已量化模型上合并 LoRA 会出错，所以这里强制先把 `quant_method` 抹掉，让 `prepare_model_template` 加载原始 fp16 权重再合并。这也是为什么「合并 + 量化」的组合里，合并一定在前、量化一定在后——合并时还没量化，量化作用于已合并的完整模型。

合并动作本身委托给 `Swift.merge_and_unload`，它按模型后端分流：

[swift/tuners/base.py:723-742](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/tuners/base.py#L723-L742) — peft 后端调 `PeftModel.merge_and_unload()`，swift 后端对每个 LoRA adapter 调 `LoRA.unpatch_lora` 把增量回写基座。

合并完 `model = model.model` 剥掉外层 wrapper，拿到纯基座 `nn.Module`，再交给 `save_checkpoint` 落盘。最后三行把 `args` 的「模型身份」整体迁移到合并产物：

```python
args.model = output_dir
args.model_dir = output_dir
args.adapters = []   # adapter 已并入，清空
```

这一步是「合并 → 量化」能无缝衔接的关键：后续 `quantize_model(args)` 读到的就是一个没有 adapter 的完整模型路径。

> 补充：`check_tie_word_embeddings` 处理的是「tie_word_embeddings=True 但 embedding 被 `ModulesToSaveWrapper` 包装」的边角情况——此时 tying 已不成立，需把 config 里的 `tie_word_embeddings` 改回 `False`，否则保存/加载会错位。
> [swift/pipelines/export/merge_lora.py:13-24](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/export/merge_lora.py#L13-L24)

#### 4.2.4 代码实践

**实践目标**：对一个已有的 LoRA adapter 执行合并，得到一个可独立部署的完整模型，并验证合并前后推理输出一致。

**操作步骤**：

1. 准备一个训练产物目录（来自 u1-l5 的 LoRA 微调，或任何 `output/vx-xxx/checkpoint-xxx`）。若手头没有，可先用最小命令训练一个：参考 [examples/train/lora_sft.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/train/lora_sft.sh)。
2. 执行合并（与官方示例一致）：

   ```bash
   swift export \
       --adapters output/vx-xxx/checkpoint-xxx \
       --merge_lora true
   ```

   命令对应 [examples/export/merge_lora.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/export/merge_lora.sh)。因 checkpoint 含 `args.json`，无需显式 `--model`/`--system`。
3. 合并完成后，分别用「基座 + adapter」与「合并后的完整模型」两种方式推理同一条 prompt：

   ```bash
   # 方式 A：挂 adapter 推理
   swift infer --adapters output/vx-xxx/checkpoint-xxx
   # 方式 B：用合并后的完整模型推理
   swift infer --model output/vx-xxx/checkpoint-xxx-merged
   ```

**需要观察的现象**：

- 合并目录 `checkpoint-xxx-merged` 下是完整的 `*.safetensors`（不再是 `adapter_model.safetensors`），且体积接近原基座。
- 两种推理方式对同一 prompt 的输出文本应当一致（允许极小浮点误差）。

**预期结果**：合并产物与「基座+adapter」推理结果一致，证明增量已正确焊入基座权重。若两者输出差异明显，优先排查 `check_tie_word_embeddings` 是否误判、或 adapter 是否训练步数过少。

> 若无 GPU 环境，「待本地验证」推理一致性这一步；但合并命令本身可在 CPU 上跑通（较慢）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `merge_lora` 要在函数开头把 `args.quant_method = None`？

**参考答案**：因为 peft 在已量化模型上合并 LoRA 会报错（peft issue #2321）。抹掉 `quant_method` 可确保 `prepare_model_template` 加载原始未量化的权重再做合并。这也意味着「合并 + 量化」必须合并在前。

**练习 2**：合并完成后为什么要执行 `args.adapters = []` 和 `args.model_dir = output_dir`？

**参考答案**：合并后 adapter 已并入基座，不再存在独立的增量文件；把 `args.adapters` 清空、把 `model_dir` 指向合并产物，是为了让 `run()` 中后续步骤（如量化）把合并产物当作一个普通完整模型来消费，而不是再去挂载已不存在的 adapter。

### 4.3 quantize_model：GPTQ/AWQ/FP8/BNB 量化

#### 4.3.1 概念说明

量化是把权重从高比特浮点压到低比特整数，例如 fp16→int4 可把模型体积压到约 1/4，显存占用大幅下降，int4 还能加速推理。代价是精度损失，需要一批**校准数据（calibration set）**来估计每段权重的数值范围，从而选出合适的缩放因子。

ms-swift 通过 `QuantEngine` 支持四种主流量化方法：

| 方法 | 是否需要校准数据 | 原理要点 | 典型用途 |
| --- | --- | --- | --- |
| **GPTQ / GPTQ_v2** | 需要 | 逐层用校准数据计算 Hessian，按列量化权重并补偿误差 | 通用 int4 量化 |
| **AWQ** | 需要 | 保留「重要」权重精度（基于激活幅度），其余量化 | 高精度 int4 |
| **BNB** | 不需要 | bitsandbytes 运行时量化（nf4/fp4），无需校准 | 快速、低成本 |
| **FP8** | 不需要 | 直接把权重存为 8-bit 浮点（e4m3 等），无校准 | vLLM/硬件 FP8 加速 |

量化的核心数学是「缩放 + 取整」：

\[
\hat{W} = \mathrm{round}\!\left(\frac{W}{s}\right),\qquad
\tilde{W} = s \cdot \hat{W}
\]

其中 \(s\) 是缩放因子（scale），GPTQ/AWQ 的核心差异就在于如何用校准数据为每一组权重选出一个让误差最小的 \(s\)。

#### 4.3.2 核心流程

`quantize_model(args)` 只有一行——`QuantEngine(args).quantize()`，所有逻辑在 `QuantEngine` 里：

1. **`__init__`**：加载模型与模板，把模板切到 `train` 模式、关 `use_cache`、保存 `args.json`。若是 AWQ，把 `AutoAWQForCausalLM` 作为 `auto_model_cls` 透传给 `prepare_model_template`。
2. **`quantize()` 入口校验**：非 fp8 方法必须指定 `quant_bits`。
3. **按方法分流**：
   - AWQ：`awq_model_quantize()` → `self.model.quantize(...)` → `save_quantized`。
   - GPTQ/GPTQ_v2：`gptq_model_quantize(v2=...)` → `GPTQQuantizer.quantize_model` → `gptq_quantizer.save`。
   - BNB/FP8：直接 `self.model.save_pretrained(...)`（量化在加载时已完成或 FP8 无需校准）。
4. **统一收尾**：`save_checkpoint` 保存 processor 与 `additional_saved_files`，打印参数信息。

校准数据集由 `_get_quant_dataset` 准备：从 `args.dataset` 取训练集，编码成 token，按 `max_length` 切块，取前 `quant_n_samples` 条。

#### 4.3.3 源码精读

分流逻辑一目了然：

[swift/pipelines/export/quant.py:36-71](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/export/quant.py#L36-L71) — `QuantEngine.quantize()`：按 `quant_method` 分流到 AWQ/GPTQ/BNB+FP8 三类保存路径，末尾统一 `save_checkpoint`。

```python
if args.quant_method == 'awq':
    self.template.model = self.model.model
    self.awq_model_quantize()
    self.model.save_quantized(args.output_dir, ...)
elif args.quant_method in {'gptq', 'gptq_v2'}:
    self.template.model = self.model
    gptq_quantizer = self.gptq_model_quantize(v2=(args.quant_method == 'gptq_v2'))
    ...
    gptq_quantizer.save(self.model, args.output_dir, ...)
elif args.quant_method in {'bnb', 'fp8'}:
    self.model.save_pretrained(args.output_dir, ...)
```

注意 BNB/FP8 分支没有「量化」调用——BNB 的量化发生在模型加载阶段（`prepare_model_template` 时按 `bnb_4bit_*` 参数已量化），FP8 则是把权重以 fp8 dtype 直接保存，二者都不需要 GPTQ/AWQ 那样的逐层校准，所以直接 `save_pretrained` 即可。

校准数据集的准备是 GPTQ/AWQ 的关键。`_get_quant_dataset` 用 monkey-patch 替换 optimum/awq 原生的 `get_calib_dataset`，注入 ms-swift 自己用 `load_dataset` + `template.encode` 准备的数据：

[swift/pipelines/export/quant.py:84-130](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/export/quant.py#L84-L130) — `_get_quant_dataset`：加载训练集、逐条 `template.encode`、按 `max_length` 拼接切块，供 GPTQ/AWQ 校准；多模态模型走分支保留完整 inputs。

```python
for data in dataset:
    try:
        inputs = template.encode(data)
    except MaxLengthError:
        continue
    ...
    samples += input_ids
    i += 1
    if i == n_samples:
        break
# now concatenate all samples and split according to block size
n_split = max(len(samples) // block_size, 1)
```

这里把多条样本的 token 拼成一条长序列，再按 `block_size`（= `max_length`）切块，每块作为一个校准样本——这是 GPTQ/AWQ 校准的常规做法，让每个校准块尽量长以覆盖更多激活模式。

`_patch_gptq` / `awq_model_quantize` 用 `contextmanager` 临时替换 optimum/awq 的内部函数，结束后恢复：

[swift/pipelines/export/quant.py:148-168](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/export/quant.py#L148-L168) — AWQ 量化：替换 `quantizer.get_calib_dataset` 为自家实现，构造 `quant_config`（`w_bit`/`q_group_size`/`zero_point`/`version`），MoE 模型追加 `modules_to_not_convert`。

[swift/pipelines/export/quant.py:262-288](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/export/quant.py#L262-L288) — GPTQ 量化：先决出 `block_name_to_quantize` 与 MoE 的 `modules_in_block_to_quantize`，再 `GPTQQuantizer(...).quantize_model(...)`。

入口 `quantize_model` 是个薄封装：

[swift/pipelines/export/quant.py:291-292](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/export/quant.py#L291-L292) — `quantize_model(args)` 仅 `QuantEngine(args).quantize()`。

#### 4.3.4 代码实践

**实践目标**：对 4.2 合并得到的完整模型（或直接对一个基座）做 FP8 量化，对比量化前后的模型体积与推理效果。

**操作步骤**：

1. 先确保已得到一个完整模型（4.2 的合并产物，或直接用基座）。FP8 无需校准数据，命令最简：

   ```bash
   CUDA_VISIBLE_DEVICES=0 \
   swift export \
       --model Qwen/Qwen2.5-3B-Instruct \
       --quant_method fp8 \
       --output_dir Qwen2.5-3B-Instruct-FP8
   ```

   命令对应 [examples/export/quantize/fp8.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/export/quantize/fp8.sh)。
2. 若想体验「合并 + FP8 量化」叠加，把 `--model` 换成 `--adapters`：

   ```bash
   swift export \
       --adapters output/vx-xxx/checkpoint-xxx \
       --merge_lora true \
       --quant_method fp8 \
       --output_dir my-model-FP8
   ```
3. 对比体积：用 `du -sh` 分别查看原始模型目录与 FP8 目录。
4. 对比推理效果：

   ```bash
   swift infer --model Qwen2.5-3B-Instruct-FP8 --infer_backend vllm
   ```

**需要观察的现象**：

- FP8 目录体积约为原始 fp16 模型的一半（fp16→fp8 理论 2:1 压缩）。
- 量化前后对常见 prompt 的回答语义应基本一致，长文本生成可能略有差异。
- 日志会打印 `model_parameter_info`，可观察量化后参数的 dtype/分布。

**预期结果**：FP8 模型体积减半，推理质量几乎无损，且 vLLM 能直接加载（FP8 是 vLLM 友好格式）。若用 GPTQ/AWQ，则需额外提供 `--dataset` 校准集，耗时更长但 int4 压缩比更高。

> FP8 量化对硬件有要求（需 Ampere 及以上 GPU）；MoE 模型的 FP8 量化在 `transformers>=5.0` 下结构有变，需改用 `megatron export`（见 fp8.sh 顶部注释）。若无合适硬件，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 BNB 和 FP8 分支里没有像 GPTQ/AWQ 那样的「量化」函数调用，而是直接 `save_pretrained`？

**参考答案**：BNB 的量化是「加载时量化」——`prepare_model_template` 阶段已按 `bnb_4bit_quant_type` 等参数把模型量化好；FP8 则是把权重直接以 fp8 dtype 保存。二者都不需要 GPTQ/AWQ 那种逐层用校准数据算 Hessian 的过程，所以 `quantize()` 里只需把已就绪的模型保存下来。

**练习 2**：GPTQ 校准时为什么要把多条样本的 token 拼成长序列再按 `max_length` 切块，而不是每条样本单独作为一个校准样本？

**参考答案**：拼接后切块能让每个校准块尽量接近 `max_length` 长度，覆盖更长的上下文与更多激活模式，使 Hessian 估计更充分、量化误差更小；若每条短样本单独校准，激活覆盖不足，量化精度会下降。

### 4.4 ollama 与 hf/mcore 格式转换

#### 4.4.1 概念说明

除了合并与量化，`swift export` 还支持两类「格式翻译」：

- **Ollama 导出**：Ollama 是一个本地大模型运行时，它不直接读 HF 格式，而是用一个 `Modelfile`（类似 Dockerfile）描述模型路径、对话模板、停止词、采样参数。`export_to_ollama` 的职责就是把 ms-swift 的 `TemplateMeta`（对话格式配方）翻译成 Ollama 的 `Modelfile` 语法。
- **HF ↔ mcore 互转**：Megatron-Core（mcore）是另一套权重布局，常用于大规模 TP/PP 训练。`convert_hf2mcore` 把 HF 权重转成 mcore 格式，`convert_mcore2hf` 反向转换。ms-swift 借 `mcore-bridge` 让这层转换对上层尽量透明。

这两类转换都不改变模型能力，只改变「存放格式」。

#### 4.4.2 核心流程

**Ollama 导出**流程：

1. `args.device_map = 'meta'`：用 meta 设备加载，只读结构不占显存，加速加载。
2. 构造 `TransformersEngine`，拿到 `template_meta`。
3. 写 `Modelfile`：
   - `FROM <model_dir>`：指定模型路径。
   - `TEMPLATE """..."""`：把 `system_prefix`/`prefix`/`prompt`/`suffix` 翻译成 Ollama 的 Go 模板语法（`{{ .System }}`/`{{ .Prompt }}`/`{{ .Response }}`）。
   - `PARAMETER stop/temperature/top_k/top_p/repeat_penalty`：从 `generation_config` 抽取采样参数与停止词。

**hf/mcore 互转**流程（本讲只到 dispatch 层）：

- `to_mcore` 单独为真 → `convert_hf2mcore(args)`：HF→mcore。
- `to_hf` 为真，或（`mcore_adapter` 且 `to_mcore`）→ `convert_mcore2hf(args)`：mcore→HF（后者表示先有 mcore adapter 要转成 hf 中间态再继续）。

#### 4.4.3 源码精读

`export.py` 里 mcore 转换的分支条件值得细读（注意 Python 运算符优先级，`and` 高于 `or`）：

[swift/pipelines/export/export.py:36-41](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/export/export.py#L36-L41) — mcore 转换分发：`to_hf or (mcore_adapter and to_mcore)` 走 mcore→hf，单独 `to_mcore` 走 hf→mcore。

```python
elif args.to_hf or args.mcore_adapter and args.to_mcore:
    from swift.megatron import convert_mcore2hf
    convert_mcore2hf(args)
elif args.to_mcore:
    from swift.megatron import convert_hf2mcore
    convert_hf2mcore(args)
```

`megatron` 子模块按需导入（`from swift.megatron import ...`），保护未装 megatron 的用户——这与全项目「重型依赖懒加载」的约定一致（u1-l3）。

`convert_hf2mcore` 的关键是用 `bridge.load_weights` 把 HF 权重灌进 mcore 模型结构：

[swift/megatron/convert.py:32-64](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/convert.py#L32-L64) — `convert_hf2mcore`：按参数量自动算 `thread_count`（控制分片数，单分片 <10GB），建 mcore 模型后用 `bridge.load_weights` 迁移权重，可选 `test_convert_precision` 校验精度。

[swift/megatron/convert.py:67-112](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/convert.py#L67-L112) — `convert_mcore2hf`：反向，加载 mcore checkpoint（可含 mcore_adapter 并 merge），`to_hf` 时用 `bridge.save_weights` 写成 HF 格式，`to_mcore` 时保存为 mcore。

Ollama 导出的核心是 `replace_and_concat`——把 `TemplateMeta` 里的 token 占位（如 `bos_token_id`/`eos_token_id`）和文本模板翻译成 Ollama 的字符串：

[swift/pipelines/export/ollama.py:33-73](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/export/ollama.py#L33-L73) — `export_to_ollama`：meta 设备加载、写 `FROM`/`TEMPLATE`/`PARAMETER`，把 `template_meta` 的 prefix/prompt/suffix 翻译成 Ollama Go 模板语法，并从 `generation_config` 抽采样参数。

```python
args.device_map = 'meta'  # Accelerate load speed.
...
with open(os.path.join(args.output_dir, 'Modelfile'), 'w', ...) as f:
    f.write(f'FROM {engine.model_dir}\n')
    f.write(f'TEMPLATE """{{{{ if .System }}}}...')  # 翻译 system_prefix/prefix/prompt/suffix
    ...
    f.write(f'PARAMETER stop "{...}"\n')
    f.write(f'PARAMETER temperature {generation_config.temperature}\n')
```

注意 `TEMPLATE` 块里用 `{{ .System }}`/`{{ .Prompt }}`/`{{ .Response }}` 这些 Ollama 占位符替换 ms-swift 模板里的 `{{SYSTEM}}`/`{{QUERY}}`，而 `eos_token_id` 这类 token 占位则被 `replace_and_concat` 解码成真实字符串。这正是 u3-l3 讲过的 `TemplateMeta` 格式配方在这里被消费的实例。

#### 4.4.4 代码实践

**实践目标**：为一个模型生成 Ollama 的 `Modelfile`，并理解模板翻译过程。

**操作步骤**：

1. 执行 Ollama 导出（无需 GPU 量化，只生成配置）：

   ```bash
   swift export \
       --model Qwen/Qwen2.5-1.5B-Instruct \
       --to_ollama true \
       --output_dir Qwen2.5-1.5B-Instruct-ollama
   ```

   命令对应 [examples/export/ollama.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/export/ollama.sh)。
2. 打开生成的 `Qwen2.5-1.5B-Instruct-ollama/Modelfile`，对照 [swift/template/template_meta.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/template_meta.py) 里的 Qwen 模板字段，逐行核对 `FROM`/`TEMPLATE`/`PARAMETER`。
3. 若本地装了 ollama，可按日志提示尝试：

   ```bash
   ollama create my-custom-model -f Qwen2.5-1.5B-Instruct-ollama/Modelfile
   ollama run my-custom-model
   ```

**需要观察的现象**：

- `Modelfile` 的 `TEMPLATE` 段里出现 `{{ .System }}`、`{{ .Prompt }}`、`{{ .Response }}`，且 `<|im_start|>`/`<|im_end|>` 等 Qwen 特殊 token 已被正确写入。
- `PARAMETER stop` 列出了模板的 suffix 与 `generation_config` 的 stop_words。

**预期结果**：生成的 `Modelfile` 能被 ollama 正确解析并还原与 `swift infer` 一致的对话格式。若 ollama 未安装，至少能通过阅读 `Modelfile` 验证模板翻译正确性。

> hf↔mcore 互转需要安装 megatron 相关依赖，且通常配合多卡分布式环境，本讲不展开实操，留到 u9-l3 实践。

#### 4.4.5 小练习与答案

**练习 1**：`export_to_ollama` 为什么要把 `args.device_map` 设成 `'meta'`？

**参考答案**：导出 Ollama 只需要读模型结构来生成 `Modelfile`，不需要把权重真正加载进显存。`'meta'` 设备只实例化 tensor 的形状与元信息、不分配实际显存，从而加速加载、降低资源占用。

**练习 2**：表达式 `args.to_hf or args.mcore_adapter and args.to_mcore` 中，若 `to_hf=False`、`mcore_adapter='path'`、`to_mcore=True`，会走哪个分支？为什么？

**参考答案**：会走 `convert_mcore2hf`。因为 `and` 优先级高于 `or`，表达式等价于 `to_hf or (mcore_adapter and to_mcore)`，即 `False or (True and True)` = `True`，命中 mcore→hf 分支。语义上：当你手里有一个 mcore 格式的 LoRA adapter、又想把整个东西导出为 mcore 模型时，需要先把 mcore adapter 转换/合并回 hf 中间态，所以走 `convert_mcore2hf`。

## 5. 综合实践

把本讲的合并与量化串成一个完整流水线，模拟一次真实的「训练产物 → 可部署模型」加工：

**任务**：对一个 LoRA 微调产物，依次完成「合并 LoRA → FP8 量化 → 推理验证」，并对比三个阶段产物的体积与效果。

**步骤**：

1. **准备产物**：用 u1-l5 的 LoRA 微调得到 `output/vx-xxx/checkpoint-xxx`（含 `args.json` 与 `adapter_model.safetensors`）。
2. **阶段 A：合并 + FP8 一步到位**：

   ```bash
   swift export \
       --adapters output/vx-xxx/checkpoint-xxx \
       --merge_lora true \
       --quant_method fp8 \
       --output_dir my-model-merged-fp8
   ```

   对照 [swift/pipelines/export/export.py:24-31](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/export/export.py#L24-L31) 解释：先 `merge_lora`（`output_dir` 临时置 `None` 生成 `xxx-merged` 中间目录，合并后 `args.adapters=[]`、`args.model_dir` 指向合并产物），再 `quantize_model` 对合并产物做 FP8 量化，最终输出到 `my-model-merged-fp8`。
3. **阶段 B：体积对比**：用 `du -sh` 分别测量：
   - 原始基座模型目录
   - `checkpoint-xxx`（adapter，应很小）
   - `my-model-merged-fp8`（合并+量化后，应约为基座的 1/2）
4. **阶段 C：推理验证**：

   ```bash
   swift infer --model my-model-merged-fp8 --infer_backend vllm
   swift infer --adapters output/vx-xxx/checkpoint-xxx   # 对照组：基座+adapter
   ```

   对同一组 prompt 比较两者输出。

**预期结果**：

- 「合并+FP8」产物体积约为原始基座的一半，远小于「基座+adapter」双文件部署。
- 推理输出与「基座+adapter」对照组语义一致（允许 FP8 微小数值差异），证明合并与量化都正确。
- 若输出差异明显，按本讲排查要点定位：合并阶段查 `check_tie_word_embeddings` 与 adapter 训练质量；量化阶段查硬件是否支持 FP8、MoE 是否需走 `megatron export`。

> 这是一个需要 GPU 且耗时较长的综合任务。若无条件完整跑通，建议至少完成阶段 A 的命令拼装与阶段 B 的体积对比（可在 CPU 上慢速完成合并，FP8 量化与 vLLM 推理则标注「待本地验证」）。

## 6. 本讲小结

- `SwiftExport.run()` 用「合并段（独立 if，可叠加）+ 出口段（if/elif 链，互斥）」的分支结构，把 merge_lora、量化、ollama、mcore 转换、push_to_hub 串成一条流水线，合并可前置叠加在量化/ollama/push 之前。
- `merge_lora` 把 LoRA 增量 \(\Delta W\) 焊入基座得到完整模型；关键是在**未量化**的原始模型上合并（`args.quant_method = None`），合并后清空 `args.adapters` 并把 `model_dir` 指向合并产物，使后续步骤无缝衔接。
- `quantize_model` 通过 `QuantEngine` 分流：GPTQ/AWQ 需校准数据（`_get_quant_dataset` 拼 token 切块、monkey-patch 注入 optimum/awq），BNB/FP8 无需校准直接 `save_pretrained`；统一由 `save_checkpoint` 收尾。
- `export_to_ollama` 把 `TemplateMeta` 翻译成 Ollama `Modelfile`（`FROM`/`TEMPLATE`/`PARAMETER`），用 `device_map='meta'` 加速；hf↔mcore 互转经 `convert_hf2mcore`/`convert_mcore2hf` 派发，megatron 按需懒加载。
- 所有导出开关集中在 `ExportArguments`，`__post_init__` 做关键校验（quant_bits 与 quant_method 互校、gptq/awq 必须有 dataset、to_mcore/to_hf 关闭 merge_lora），是排错的第一站。

## 7. 下一步学习建议

- **部署与服务化**：本讲产出的量化/合并模型如何真正跑成服务？下一讲 u8-l2「部署与服务化」讲解 `swift deploy` 如何基于 vLLM/sglang 启动 OpenAI 兼容服务，正是消费本讲产物的下游。
- **模型评测**：量化必然带来精度损失，如何量化评估？u8-l3「模型评测」讲解 `swift eval` 以 EvalScope 为后端评测模型，建议用本讲的 FP8 模型做一次评测对比。
- **Megatron 深入**：若你需要 mcore 转换背后的 TP/PP/SP 细节，跳到 u9-l3「Megatron-SWIFT 架构总览」与 u9-l4「Megatron 训练流程」，那里详解 `convert.py` 用的 `bridge` 与并行策略。
- **延伸阅读源码**：[swift/pipelines/export/cached_dataset.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/export/cached_dataset.py) 实现了 `to_cached_dataset` 分支（提前 tokenize 数据集缓存到磁盘），虽未在本讲展开，但它是理解「数据预处理加速」的好材料，可对照 u4-l3 的编码与 packing 机制阅读。
