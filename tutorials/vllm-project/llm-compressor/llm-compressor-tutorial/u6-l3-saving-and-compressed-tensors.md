# 保存与 compressed-tensors 格式

## 1. 本讲目标

前面三讲（u3 量化与校准、u4 算法、u6-l1/u6-l2 规模化与内存）都在回答「怎么把权重改掉」。本讲回答收尾的最后一个问题：**改完之后，这些被量化/压缩过的权重是怎么落盘、又怎么被 vLLM 加载的？**

具体来说，学完本讲你应当掌握：

- 能说清 `post_process` 这一行调用背后，`model.save_pretrained` 是如何被「换成压缩版」的，以及 `pre_process` 在校准**之前**就提前埋下的伏笔。
- 能讲清 `modify_save_pretrained` 包装出来的 `save_pretrained` 做了哪几件事（建 compressor、可选压缩权重、re-tie embeddings、转 accelerate、写结构、更新 config、落 recipe、复制 python/mtp 文件、分布式只让 source 进程写）。
- 理解 `save_compressed` 与 `quantization_format` 两个参数的真实含义，以及为何「稀疏压缩」已不再支持。
- 能打开一个量化模型的 `config.json`，逐字段读懂 `quantization_config`，并据此判断它能否被 vLLM 加载。

本讲只讲「保存与序列化格式」这一件事，不再涉及量化数学本身。

## 2. 前置知识

本讲假设你已经读过：

- **u1-l4 oneshot 入口与三阶段生命周期**：知道 `oneshot()` 是薄包装，内部 `Oneshot` 类走 `pre_process → apply_recipe_modifiers → post_process` 三阶段；知道 `parse_args` 把 kwargs 路由成 `ModelArguments / DatasetArguments / RecipeArguments`。
- **u2-l1 Session 与 State**：知道全局 `CompressionSession` 持有 `lifecycle`，而 `recipe` 住在 `lifecycle` 里——这点在本讲「落 recipe」时要用到。
- **u3-l1 QuantizationModifier 与量化方案**：知道 `QuantizationModifier` 的核心字段是 `targets / ignore / scheme / config_groups / kv_cache_scheme`，以及 scheme（如 `FP8_DYNAMIC`、`W4A16`）会归一化成 `config_groups`。本讲会看到这些字段如何原样出现在 `config.json` 的 `quantization_config` 里。

几个需要先建立的直觉：

1. **`save_pretrained` 不是 transformers 的原版了**。在 oneshot 流程里，它在校准开始前就被 llmcompressor 偷偷换成了一个「带压缩能力的包装版本」。你以为你在调 `model.save_pretrained(...)`，实际跑的是包装函数。
2. **llmcompressor 自己不发明磁盘格式**。量化和稀疏权重在磁盘上长什么样、`config.json` 里的 `quantization_config` 字段怎么写，全部由外部库 [`compressed-tensors`](https://github.com/vllm-project/compressed-tensors) 决定。llmcompressor 的职责只是「把 transformers 的 `save_pretrained` 接到 compressed-tensors 的序列化引擎上」。
3. **`config.json` 里的 `quantization_config` 是「量化是否生效」的唯一证据**。vLLM 加载模型时就是读这个字段来决定要不要按压缩格式解释权重。所以「保存」的核心产物，与其说是 `.safetensors` 文件，不如说是写进 `config.json` 的那段配置。
4. **压缩 = 用更少字节存同一个权重**。一个 fp16 的 `Linear` 权重若量化成 int4，磁盘上可以打包（pack）成紧凑的字节流；也可以不打包，仍以全精度存着、只是打个「已被量化」的标记。`save_compressed` 开关控制的正是「要不要真的打包」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/llmcompressor/entrypoints/oneshot.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py) | `Oneshot` 类：在 `__init__` 里调 `pre_process`（埋保存伏笔），在 `__call__` 末尾调 `post_process`（真正落盘）。 |
| [src/llmcompressor/entrypoints/utils.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/utils.py) | `pre_process`（加载模型、打补丁、调用 `modify_save_pretrained`）与 `post_process`（调用 `model.save_pretrained`、保存 processor、`reset_session`）。 |
| [src/llmcompressor/transformers/compression/compressed_tensors_utils.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/transformers/compression/compressed_tensors_utils.py) | 本讲主角：`modify_save_pretrained`（包装 `save_pretrained`）、`_retie_embeddings`、`update_and_save_recipe`、`suspend_distributed_timeout`。 |
| [src/llmcompressor/args/model_arguments.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/args/model_arguments.py) | `ModelArguments.save_compressed` 字段的定义处（默认 `True`）。 |
| [src/llmcompressor/transformers/utils/helpers.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/transformers/utils/helpers.py) | `is_model_ct_quantized_from_path`（判断输入模型是否已是 compressed-tensors 量化）、`infer_recipe_from_model_path`（保存时找旧 recipe）、`RECIPE_FILE_NAME`。 |
| [docs/guides/saving_a_model.md](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/guides/saving_a_model.md) | 官方保存指南：解释 `quantization_format` / `save_compressed` 两个附加参数与 vLLM 加载约定。 |
| [examples/quantization_w8a8_fp8/fp8_block_example.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/examples/quantization_w8a8_fp8/fp8_block_example.py) | 一个完整可参考的「量化 → `save_pretrained`」示例。 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1 保存的全景**：`pre_process` 埋伏笔、`post_process` 落盘，讲清两阶段与 `save_pretrained` 的接驳点（`entrypoints.utils`）。
- **4.2 `modify_save_pretrained`：给 `save_pretrained` 装上压缩引擎**：包装机制、`ModelCompressor`、`save_compressed` / `quantization_format` 两个旋钮（`compressed_tensors_utils`）。
- **4.3 落盘的四个附加动作**：re-tie embeddings、accelerate 转换、写 recipe、复制 python/mtp 文件，以及分布式下「只让 source 进程写」（`compressed_tensors_utils`）。
- **4.4 compressed-tensors 格式与 vLLM 加载约定**：`quantization_config` 字段语义、格式推断、稀疏压缩的废弃。

### 4.1 保存的全景：从 oneshot 三阶段到落盘

#### 4.1.1 概念说明

回顾 u1-l4：`oneshot()` 只是把参数塞进 `Oneshot` 实例并调用它。`Oneshot.__init__` 跑「预处理」，`Oneshot.__call__` 跑「校准 + 后处理」。**保存发生在后处理阶段**，但保存所需的「装备」却在预处理阶段就装好了。这两步分别对应 `entrypoints/utils.py` 里的 `pre_process` 与 `post_process`。

理解本模块的关键是建立这张「谁在什么时候干了什么」的对照表：

| 阶段 | 函数 | 与保存相关的动作 |
|------|------|----------------|
| 预处理 | `pre_process` | 加载模型；把 accelerate offload 转 CT offload；**调用 `modify_save_pretrained(model)` 把 `save_pretrained` 换成压缩版** |
| 校准 | `apply_recipe_modifiers` | 改权重（与保存无关，但产出了待保存的量化权重） |
| 后处理 | `post_process` | **调用 `model.save_pretrained(output_dir, save_compressed=...)`**；保存 processor；`reset_session()` |

注意：`modify_save_pretrained`（换 `save_pretrained`）发生在校准**之前**，但被换上去的压缩能力要到后处理阶段调用 `save_pretrained` 时才真正触发。这正是「提前埋伏笔」的意思。

#### 4.1.2 核心流程

`Oneshot.__call__` 的尾部把后处理串起来：

```python
# oneshot.py（节选）
def __call__(self):
    calibration_dataloader = get_calibration_dataloader(self.dataset_args, self.processor)
    self.apply_recipe_modifiers(...)
    post_process(
        model_args=self.model_args,
        recipe_args=self.recipe_args,
        output_dir=self.output_dir,
    )
```

`post_process` 本身非常薄，核心就一句 `save_pretrained`：

```python
# entrypoints/utils.py post_process（节选）
if model_args is not None and output_dir is not None:
    if recipe_args is not None and getattr(recipe_args, "stage", None) is not None:
        output_dir = os.path.join(output_dir, recipe_args.stage)  # 多 stage 分目录
        os.makedirs(output_dir, exist_ok=True)

    model_args.model.save_pretrained(
        output_dir, save_compressed=model_args.save_compressed
    )
    if model_args.processor is not None:
        model_args.processor.save_pretrained(output_dir)

if recipe_args is not None and recipe_args.clear_sparse_session:
    reset_session()
```

这里有两个细节值得记住：

1. **`save_compressed` 从 `ModelArguments` 流到 `save_pretrained`**。`oneshot(save_compressed=...)` 这个参数（见 `oneshot.py` 的 `save_compressed: bool = True`）经 `parse_args` 进了 `ModelArguments`，又在这里原样传给 `save_pretrained`。
2. **多 stage 会分子目录**：如果 recipe 指定了 `stage`，保存目录会变成 `output_dir/<stage>`，便于分阶段产出多个 checkpoint。
3. **`reset_session()` 默认不触发**：只有显式传 `clear_sparse_session=True` 才会清掉这次一次性会话。

而预处理阶段的关键一行是：

```python
# entrypoints/utils.py pre_process（节选）
# if the model was loaded with accelerate offloading, convert to CT offloading
if hasattr(model_args.model, "hf_device_map"):
    from_accelerate(model_args.model)

# wrap model.save_pretrained
modify_save_pretrained(model_args.model)
```

这两行就是本讲与 u6-l2（大模型内存）的交汇点：如果模型是用 HuggingFace accelerate 的 offload 加载的（`hf_device_map`），先 `from_accelerate` 把它转成 compressed-tensors 自己的 offload 表示；然后 `modify_save_pretrained` 给 `save_pretrained` 打补丁。

#### 4.1.3 源码精读

[entrypoints/utils.py:99-131](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/utils.py#L99-L131) —— `post_process` 的完整实现。注意 `save_pretrained(output_dir, save_compressed=model_args.save_compressed)`（L122-L124）是后处理阶段唯一真正写权重的地方；processor 的保存紧跟其后（L126-L127）。

[entrypoints/utils.py:86-96](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/utils.py#L86-L96) —— 预处理里先 `from_accelerate`（L92-L93）再 `modify_save_pretrained`（L96）。`modify_save_pretrained` 这一行就是「把 transformers 原版 `save_pretrained` 换成压缩版」的总开关。

[entrypoints/oneshot.py:205-209](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L205-L209) —— `__call__` 末尾对 `post_process` 的调用，证明保存是三阶段的最后一步。

[args/model_arguments.py:69-73](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/args/model_arguments.py#L69-L73) —— `save_compressed: bool = True` 字段定义，注释为「Whether to compress models during save」。这是 `save_compressed` 默认值的源头。

#### 4.1.4 代码实践：跟踪一次 oneshot 的保存路径

1. **实践目标**：把「预处理埋伏笔 → 后处理落盘」这条链路在源码里走一遍，确认保存不是凭空发生的。
2. **操作步骤**：
   - 打开 [oneshot.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py)，在 `__init__`（L178）找到 `pre_process(model_args, dataset_args, output_dir)`；在 `__call__`（L205-L209）找到 `post_process(...)`。
   - 打开 [entrypoints/utils.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/utils.py)，确认 `pre_process` 里 L96 的 `modify_save_pretrained` 与 `post_process` 里 L122-L124 的 `save_pretrained` 是「装」与「用」的关系。
3. **需要观察的现象**：`modify_save_pretrained` 改变的是 `model.save_pretrained` 这个方法本身；`post_process` 只是调用它。
4. **预期结果**：能用自己的话说出「为什么保存能力要在校准前装好、却在校准后才生效」——因为方法替换是幂等的准备工作，真正的写盘动作必须等权重改完。
5. 本实践为源码阅读型，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：如果在 `oneshot` 里既不传 `output_dir`、事后也不手动调 `model.save_pretrained`，模型会落盘吗？

> **答案**：不会。`post_process` 只在 `model_args is not None and output_dir is not None` 时才调 `save_pretrained`（见 L115）。不传 `output_dir` 时校准照常进行、模型权重在内存里被改写并返回，但磁盘上什么都不写。

**练习 2**：`save_compressed` 这个参数从用户传入 `oneshot()` 到最终被 `save_pretrained` 使用，中间经过了哪几个「容器」？

> **答案**：`oneshot(save_compressed=...)` → `parse_args` 路由进 `ModelArguments.save_compressed` 字段 → `post_process` 读取 `model_args.save_compressed` → 作为 `save_compressed=` 关键字传给 `model.save_pretrained(...)`。

---

### 4.2 modify_save_pretrained：给 save_pretrained 装上压缩引擎

#### 4.2.1 概念说明

`modify_save_pretrained` 是本讲真正的「主角」。它做的事情用一句话概括：**用闭包把模型原有的 `save_pretrained` 方法包一层，换成 `save_pretrained_wrapper`**。换上去之后，`model.save_pretrained(...)` 不再只是「把 state_dict 写成 safetensors」，而是会：

1. 用 compressed-tensors 的 `ModelCompressor` 推断/接受一个磁盘序列化格式（`quantization_format`）；
2. 可选地把权重按该格式真正压缩（打包）；
3. 调用 transformers 原版的 `save_pretrained` 写出结构（`config.json`、`*.safetensors`、tokenizer 等）；
4. 用 compressor 回写 `quantization_config` 到 `config.json`；
5. 顺手做几件附加事（4.3 节展开）。

这里有几个关键设计要分清：

- **`quantization_format`（量化格式）≠ scheme（量化方案）**。scheme（如 `W4A16`、`FP8_DYNAMIC`）决定「用几位、什么类型、激活怎么算」——它在 u3-l1 里被解析成 `config_groups`，存进模型的 quantization scheme。`quantization_format` 决定的是「这些量化权重在磁盘上用什么字节布局存」——比如 int 权重是打包成紧凑字节（`pack-quantized`）还是用浮点格式（`float-quantized`）。前者是「量化什么」，后者是「怎么存」。
- **`save_compressed` 控制要不要真的打包**。`True`（默认）：权重按 `quantization_format` 压缩打包，体积变小、`quantization_status` 标为 `compressed`。`False`：权重保持全精度字节、只是挂着 scheme，状态留在 `FROZEN`——适合「还想继续改」的中间产物。
- **格式可以自动推断**。`quantization_format=None`（默认）时，由 `ModelCompressor.from_pretrained_model` 根据模型当前的 quantization scheme 推断合适的磁盘格式；想强制控制就显式传（如文档示例里的 `QuantizationFormat.pack_quantized`）。

#### 4.2.2 核心流程

包装函数 `save_pretrained_wrapper` 的执行流程（伪代码）：

```python
def save_pretrained_wrapper(save_directory, quantization_format=None, save_compressed=True, **kwargs):
    kwargs.setdefault("max_shard_size", "20GB")

    # 1. 由模型当前的 quantization scheme 建一个 compressor（quantization_format 可覆盖）
    compressor = ModelCompressor.from_pretrained_model(model, quantization_format=quantization_format)

    # 2. 可选：把权重按格式真正压缩打包
    if save_compressed:
        compressor.compress_model(model, skip_compressed=True)

    # 3. 保存前先 re-tie 一次相等的 embeddings（4.3 详解）
    _retie_embeddings(model)

    # 4. 转成 accelerate 表示，让 transformers 的保存更顺
    to_accelerate(model)

    with suspend_distributed_timeout():           # 分布式下放宽超时
        if is_source_process():                   # 只让 source 进程真正写盘
            original_save_fn(...)(save_dir, ...)   # 写结构：config.json / safetensors
            compressor.update_config(save_dir)     # 回写 quantization_config
            update_and_save_recipe(...)            # 落 recipe.yaml
            copy_python_files_from_model_cache(...)# 复制 modeling python 文件
            if num_mtp_layers > 0:
                save_mtp_tensors_to_checkpoint(...)# 复制 MTP 权重并更新 config

    from_accelerate(model)                         # 转回原表示，恢复模型
```

注意三个「保持不变量」的设计：

1. **幂等包装**：函数头先检查 `_overridden` 标志（L94-L96、L168、L172），如果 `save_pretrained` 已被换过就直接返回，避免重复 oneshot 或多次 `modify_save_pretrained` 把包装层层套娃。
2. **弱引用模型**：用 `weakref.ref` 和未绑定的 `original_save_fn = save_pretrained_method.__func__`（L100-L103）保留对原始方法和模型类的引用，既不阻止模型被回收，又能在 wrapper 内部用 `original_save_fn.__get__(model, model_class)(...)` 调回 transformers 原版 `save_pretrained`。
3. **`to_accelerate` / `from_accelerate` 配对**：保存前转 accelerate、保存后转回来（L145、L166），保证模型在保存前后表示一致，只是借用 accelerate 的保存路径。

#### 4.2.3 源码精读

[compressed_tensors_utils.py:82-173](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/transformers/compression/compressed_tensors_utils.py#L82-L173) —— `modify_save_pretrained` 的完整定义。L172-L173 是「未换过才换」的入口；真正的包装函数 `save_pretrained_compressed` 定义闭包 `save_pretrained_wrapper`。

[compressed_tensors_utils.py:106-124](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/transformers/compression/compressed_tensors_utils.py#L106-L124) —— `save_pretrained_wrapper` 的签名与 docstring。两个附加参数都在这里：`quantization_format: str | None = None` 与 `save_compressed: bool = True`。docstring 明确说明 `save_compressed=False` 时「weights will remain in full precision in the 'FROZEN' state」。

[compressed_tensors_utils.py:130-134](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/transformers/compression/compressed_tensors_utils.py#L130-L134) —— 建 compressor 与可选压缩。`ModelCompressor.from_pretrained_model(model, quantization_format=quantization_format)` 负责格式推断；`compressor.compress_model(model, skip_compressed=True)` 真正打包权重。

[compressed_tensors_utils.py:147-166](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/transformers/compression/compressed_tensors_utils.py#L147-L166) —— 保存的真正发生处。`original_save_fn.__get__(model, model_class)(save_dir, **kwargs)`（L150）调 transformers 原版写结构；`compressor.update_config(save_dir)`（L153）把 `quantization_config` 写回 `config.json`——这一行就是「config.json 里出现 quantization_config 字段」的源头。

[compressed_tensors_utils.py:176-220](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/transformers/compression/compressed_tensors_utils.py#L176-L220) —— 已废弃的 `get_model_compressor`（标了 `@deprecated`）。它揭示了两个事实：其一，现在统一走 `ModelCompressor.from_pretrained_model`；其二，**稀疏压缩（sparse compression）已被 compressed-tensors 移除**，函数体里只要碰到任何稀疏相关参数就只打一条警告（L198-L205）。这与官方文档的废弃说明一致。

> 关于 `ModelCompressor` 的内部实现（格式推断规则、`compress_model` 如何打包、`update_config` 写哪些字段）：它们属于外部库 `compressed_tensors`，不在本项目源码内。本讲只讲 llmcompressor 如何调用它们；想深究打包字节布局请去 compressed-tensors 仓库。链接格式以本项目 HEAD 为准，external 库行为标注为「依赖外部库」。

#### 4.2.4 代码实践：对比 save_compressed 的两种取值

1. **实践目标**：直观感受 `save_compressed` 对产物体积与 `quantization_config.quantization_status` 的影响。
2. **操作步骤**：用 `QuantizationModifier(targets="Linear", scheme="FP8_DYNAMIC", ignore=["lm_head"])` 对一个小模型做两次 oneshot，分别用 `save_compressed=True`（默认）和 `save_compressed=False`，保存到两个目录。
   ```python
   # 示例代码（基于 fp8_block_example.py 改写）
   from transformers import AutoModelForCausalLM
   from llmcompressor import oneshot
   from llmcompressor.modifiers.quantization import QuantizationModifier

   MODEL_ID = "<一个小模型，如 tiny 系列>"  # 待本地确认可用的小模型
   for sc, tag in [(True, "compressed"), (False, "frozen")]:
       model = AutoModelForCausalLM.from_pretrained(MODEL_ID)
       oneshot(
           model=model,
           recipe=QuantizationModifier(targets="Linear", scheme="FP8_DYNAMIC", ignore=["lm_head"]),
           output_dir=f"out-{tag}",
           save_compressed=sc,
       )
   ```
3. **需要观察的现象**：比较两个目录里 `model.safetensors` 的文件大小，以及各自 `config.json` 中 `quantization_config.quantization_status` 字段。
4. **预期结果**：`save_compressed=True` 的产物体积更小、`quantization_status` 为 `compressed`；`save_compressed=False` 体积接近原模型、`quantization_status` 为 `frozen`（权重未打包但仍带 scheme）。**具体数值待本地验证**（取决于模型与磁盘格式）。
5. 若无 GPU，可只读源码确认 `save_compressed` 的分流在 L133-L134。

#### 4.2.5 小练习与答案

**练习 1**：`quantization_format` 和 `scheme` 有什么区别？分别回答「决定什么」。

> **答案**：`scheme`（如 `W4A16`、`FP8_DYNAMIC`）决定「量化什么」——权重/激活各几位、什么数值类型、分组策略，它在 u3-l1 里被解析成 `config_groups` 写进模型 scheme。`quantization_format` 决定「怎么存」——这些量化权重在磁盘上用什么字节布局打包（如 `pack-quantized`/`float-quantized`），不传时由 compressor 从 scheme 自动推断。

**练习 2**：为什么 `save_pretrained_wrapper` 开头要检查 `_overridden` 标志？

> **答案**：防止重复包装。`modify_save_pretrained` 可能被多次调用（多次 oneshot 或显式调用），没有这个幂等检查就会把 wrapper 一层层套在原方法外面，导致保存逻辑被重复执行。标志 `_overridden=True` 标记「已换过」，命中即直接返回原方法。

**练习 3**：`save_compressed=False` 时，`config.json` 里还会有 `quantization_config` 吗？

> **答案**：会。`compressor.update_config(save_dir)`（L153）在 `is_source_process()` 分支里无条件执行，与 `save_compressed` 无关。区别只在于 `quantization_status`（`frozen` vs `compressed`）和权重是否真的打包。

---

### 4.3 落盘的四个附加动作：re-tie、accelerate、recipe、python/mtp

#### 4.3.1 概念说明

除了「写结构 + 写 quantization_config」，`save_pretrained_wrapper` 在 `is_source_process()` 分支里还做了几件容易忽略但很重要的事。本模块逐个拆解：

1. **re-tie 相等的 embeddings（`_retie_embeddings`）**：校准期间输入/输出词向量常被「解绑」分别量化（见 u6-l1/l2），或被 offload 拆成两个独立参数。如果两者张量完全相等，保存前就把它们重新指向同一份存储，避免把同一张表写两遍、浪费磁盘。
2. **accelerate 转换（`to_accelerate` / `from_accelerate`）**：保存前转成 accelerate 表示以走 transformers 最优的保存路径，保存后再转回，保证模型状态前后一致。
3. **写 recipe（`update_and_save_recipe`）**：把「这次压缩用了什么 recipe」落成 `recipe.yaml`，并与原模型自带的旧 recipe 合并，让产物可复现。
4. **复制 python 文件与 MTP 权重**：`copy_python_files_from_model_cache` 把模型自带的 `modeling_*.py` 等自定义代码一并复制（`trust_remote_code` 模型需要）；若模型带 MTP（multi-token prediction）层，`save_mtp_tensors_to_checkpoint` 把 transformers 不会自动加载的 MTP 权重也拷过去并更新 config。

此外还有一个**分布式语义**贯穿全程：`is_source_process()` 保证只有 source 进程（DDP 下通常是 rank 0）真正写盘，其他进程通过 `suspend_distributed_timeout` 放宽的 barrier 等待，避免多进程同时写同一文件。

#### 4.3.2 核心流程

**re-tie embeddings** 的判定逻辑（伪代码）：

```python
def _retie_embeddings(model):
    input_embed, output_embed = get_embeddings(model)
    if 任一为 None 或 本就同一个对象:
        return
    if 两者的命名张量(参数+buffer)键集合不同 或 任一张量值不相等(torch.equal):
        return                      # 量化方式不同或本就发散，不动
    # 否则：把 output 的各张量指向 input 的存储
    with OffloadCache.disable_onloading():
        for name in input_tensors:
            setattr(output_embed, name, getattr(input_embed, name))
    text_config.tie_word_embeddings = True
```

关键在于「**比较张量值本身**」而不是比较压缩格式——因为 packed int、fp8、dense 三种格式下的张量只要数值相等就视为同一张表，逻辑对格式无关。

**写 recipe** 的逻辑：

```python
def update_and_save_recipe(model_stub, save_directory):
    existing_recipe = infer_recipe_from_model_path(model_stub)   # 找原模型自带的 recipe.yaml
    recipe = active_session().lifecycle.recipe                    # 本次实际跑的 recipe
    recipe.yaml(file_path=..., existing_recipe_path=existing_recipe)  # 合并写出
```

注意 recipe 来自 `active_session().lifecycle.recipe`（u2-l1 讲过 recipe 住在 lifecycle 里），这就是为什么保存需要依赖一个仍存活的 session——这也是 `post_process` 最后才 `reset_session()` 的原因。

**分布式保存** 的骨架：

```python
with suspend_distributed_timeout():      # 把 world 超时放宽到 3 小时
    if is_source_process():              # 只有 source 进程写盘
        original_save_fn(...)            # 其余进程在这里的 barrier 上等待
        ...
from_accelerate(model)                   # 所有进程一起恢复模型表示
```

#### 4.3.3 源码精读

[compressed_tensors_utils.py:37-79](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/transformers/compression/compressed_tensors_utils.py#L37-L79) —— `_retie_embeddings`。L54-L63 是「是否相等」的判定（键集合相同且每个张量 `torch.equal`）；L70-L72 在 `OffloadCache.disable_onloading()` 上下文里把 output 的张量指向 input 的存储（注释 L66-L69 说明这是为绕过 compressed-tensors 当前 `__setitem__` 会拷贝的临时手段）。

[compressed_tensors_utils.py:26-34](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/transformers/compression/compressed_tensors_utils.py#L26-L34) —— `_named_tensors`：取模块自身的参数与 buffer（跳过 `None`，例如无 bias 的 `lm_head`），这是 re-tie 比较的基础。

[compressed_tensors_utils.py:223-237](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/transformers/compression/compressed_tensors_utils.py#L223-L237) —— `update_and_save_recipe`。`infer_recipe_from_model_path`（L232）找旧 recipe，`active_session().lifecycle.recipe`（L234）取本次 recipe，`recipe.yaml(...)`（L237）合并写出 `recipe.yaml`。

[helpers.py:50-98](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/transformers/utils/helpers.py#L50-L98) —— `infer_recipe_from_model_path`：按「本地目录/文件 → HF 缓存 → 联网下载」三级查找原模型的 `recipe.yaml`，找不到返回 `None`。文件名常量 `RECIPE_FILE_NAME = "recipe.yaml"`（[helpers.py:28](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/transformers/utils/helpers.py#L28)）。

[compressed_tensors_utils.py:240-272](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/transformers/compression/compressed_tensors_utils.py#L240-L272) —— `suspend_distributed_timeout`：未初始化分布式时直接 `yield`（L258-L260）；否则新建一个 3 小时超时的 `gloo` 临时进程组，前后各加 barrier 同步（L264-L272）。这是「保存大模型可能很久、别让别的进程超时」的保险。

[compressed_tensors_utils.py:147-166](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/transformers/compression/compressed_tensors_utils.py#L147-L166) —— `is_source_process()` 把写盘、`update_config`、`update_and_save_recipe`、`copy_python_files_from_model_cache`、`save_mtp_tensors_to_checkpoint` 全部圈在 source 进程内（L148-L163），保证多卡只写一份。`to_accelerate`（L145）/`from_accelerate`（L166）在 barrier 之外、所有进程都执行。

#### 4.3.4 代码实践：观察保存产物里多了哪些文件

1. **实践目标**：验证保存不仅写 `config.json` + `safetensors`，还会落 `recipe.yaml`（及可能的 modeling python 文件）。
2. **操作步骤**：对一个小模型跑一次 oneshot 并保存到 `output_dir`；之后用 `ls` 列出该目录，并用文本编辑器打开 `recipe.yaml`。
   ```bash
   ls -la out-compressed/
   cat out-compressed/recipe.yaml
   ```
3. **需要观察的现象**：目录里除 `config.json`、`*.safetensors`、tokenizer 文件外，应出现 `recipe.yaml`；其内容应是你传入的 recipe（或合并了原模型 recipe 后的结果）。
4. **预期结果**：`recipe.yaml` 存在且可被 `Recipe.create_instance(file_path=...)`（u2-l5）重新解析。若原模型带自定义代码，还会看到 `modeling_*.py`。
5. 若本地无可量化的小模型，可改为源码阅读：在 `save_pretrained_wrapper` 里标注 L156（recipe）、L159（python 文件）、L161-L163（MTP）三处附加动作的代码行。**文件是否真的出现待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`_retie_embeddings` 为什么用 `torch.equal` 比较张量，而不是比较两个模块的类名或 quantization scheme？

> **答案**：因为 offload 会把一个 tied 权重拆成两个独立参数、校准会把它解绑分别量化，但只要两者张量值相等就代表同一张表，应只写一份。比较「值」而不是「格式/类名」让这个判断对 packed int、fp8、dense 三种格式都成立——格式不同但值相同仍判为同一表，值不同（被量化成不同的 scale）则保留两份。

**练习 2**：为什么 `update_and_save_recipe` 能在保存阶段拿到本次 recipe？如果 `post_process` 里 `reset_session()` 在 `save_pretrained` 之前调用会怎样？

> **答案**：本次 recipe 存在 `active_session().lifecycle.recipe` 里，只要 session 还活着就能取到。`post_process` 里 `reset_session()` 在 `save_pretrained` **之后**（L122 先于 L130），所以保存时 recipe 还在；若顺序反过来，session 被 reset 后 recipe 丢失，`update_and_save_recipe` 就拿不到本次压缩指令了。

**练习 3**：多卡（DDP）保存时，会不会每个 rank 都写一份 `config.json`？

> **答案**：不会。写盘相关的动作（`original_save_fn`、`update_config`、`update_and_save_recipe`、复制文件）都被 `is_source_process()` 圈住（L148），只有 source 进程写；其余进程在 `suspend_distributed_timeout` 的 barrier 上等待，之后所有进程一起执行 `from_accelerate` 恢复模型。

---

### 4.4 compressed-tensors 格式与 vLLM 加载约定

#### 4.4.1 概念说明

前面三个模块讲的是「怎么保存」。本模块讲「保存出来的东西长什么样、vLLM 怎么认」。核心结论：**compressed-tensors 格式 = safetensors 权重 + `config.json` 里的 `quantization_config` 字段**，其中 `quantization_config` 是 vLLM 识别压缩模型的唯一入口。

一个被 compressed-tensors 量化的模型，其 `config.json` 里会有类似下面这段（**示例**，具体字段值随 scheme 变化）：

```json
{
  "quantization_config": {
    "quant_method": "compressed-tensors",
    "format": "float-quantized",
    "quantization_status": "compressed",
    "kv_cache_scheme": null,
    "config_groups": {
      "group_0": {
        "targets": ["Linear"],
        "weights":   { "num_bits": 8, "type": "float", "strategy": "channel", ... },
        "input_activations":  { "num_bits": 8, "type": "float", "strategy": "token", ... },
        "output_activations": null
      }
    },
    "ignore": ["lm_head"]
  }
}
```

逐字段含义：

| 字段 | 含义 | 来源 |
|------|------|------|
| `quant_method` | 固定 `"compressed-tensors"`，是 vLLM/transformers 分发到正确加载器的钥匙 | compressed-tensors 常量 `QUANTIZATION_METHOD` |
| `format` | 磁盘字节布局，如 `pack-quantized`/`int-quantized`/`float-quantized`/`dense` | `quantization_format` 推断或显式指定 |
| `quantization_status` | `compressed`（已打包）或 `frozen`（挂 scheme 未打包） | `save_compressed` 取值 |
| `config_groups` | 量化方案本体：每个 group 的 targets + weights/activations 的位数/类型/策略 | u3-l1 的 scheme → `config_groups` 归一化结果 |
| `ignore` | 不量化的模块名列表 | `QuantizationModifier(ignore=...)` |
| `kv_cache_scheme` | KV cache 的量化方案，无则 `null` | `QuantizationModifier(kv_cache_scheme=...)` |

注意 `config_groups` / `ignore` / `kv_cache_scheme` 三个字段，正好对应 u3-l1 里 `QuantizationModifier` 的核心字段——**保存阶段把它们原样写进 config**，所以你能在产物里看到自己当初设的方案。

#### 4.4.2 核心流程：vLLM 如何识别并加载

加载侧的契约很简单：

1. vLLM（或 transformers）读 `config.json`，看到 `quantization_config.quant_method == "compressed-tensors"`，就分发到 compressed-tensors 加载器。
2. 加载器按 `format` 解释 `.safetensors` 里的字节（比如 `pack-quantized` 要解包 int 权重），按 `config_groups` 还原 scale/zero-point，重建量化层。
3. 若 `quant_method` 不是 `compressed-tensors`（如 `gptq`、`awq`），llmcompressor 的 `validate_model` 会直接报错（见下）——这是「想对别的格式量化模型再 oneshot」时的拦路检查。

官方文档给的 vLLM 加载方式就是当作普通模型加载：

```python
from vllm import LLM
model = LLM("./your-model-compressed")
```

#### 4.4.3 源码精读

[helpers.py:31-47](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/transformers/utils/helpers.py#L31-L47) —— `is_model_ct_quantized_from_path`：判断输入模型是否已是 compressed-tensors 量化，依据就是 `config.quantization_config["quant_method"] == "compressed-tensors"`（L42-L46）。这正是「vLLM 用 quant_method 分发」的同一条判据。

[entrypoints/utils.py:164-168](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/utils.py#L164-L168) —— `initialize_model_from_path`：若输入模型已是 CT 量化，加载时传 `CompressedTensorsConfig(run_compressed=False)`，即**以解压（全精度）形式加载**再进行 oneshot——因为压缩态权重无法直接再量化。

[entrypoints/oneshot.py:272-303](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L272-L303) —— `validate_model`：读 `config.quantization_config.quant_method`（L279-L282）；若是别的量化格式直接 `raise ValueError`（L300-L303），若是 compressed-tensors 则只警告（L293-L298）。这条逻辑印证了「llmcompressor 的产物和输入都围绕 compressed-tensors 这一种格式」。

[docs/guides/saving_a_model.md:80-91](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/guides/saving_a_model.md#L80-L91) —— 官方「Notes」：明确「`from_pretrained` 会自动检测压缩格式」「vLLM 直接当普通模型加载」，并给出 `LLM("./your-model-compressed")` 示例。同时 [L83-L84](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/guides/saving_a_model.md#L83-L84) 警告稀疏压缩（含 2:4）因缺乏硬件支持已不再支持。

[compressed_tensors_utils.py:198-205](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/transformers/compression/compressed_tensors_utils.py#L198-L205) —— 废弃的 `get_model_compressor` 里对任何稀疏参数只打「Sparse compression is no longer supported」的警告，是上一条文档说明的代码佐证。

#### 4.4.4 代码实践：逐字段解读 quantization_config 并验证可加载

1. **实践目标**：把一个真实量化产物的 `quantization_config` 字段逐个对上「我当初设的 scheme」，并确认它可被（按 compressed-tensors 逻辑）加载。
2. **操作步骤**：
   - 参考 [fp8_block_example.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/examples/quantization_w8a8_fp8/fp8_block_example.py) 跑一次 FP8_BLOCK 量化并保存（L24-L38 的 `oneshot` + `model.save_pretrained(SAVE_DIR)`）。
   - 打开 `<SAVE_DIR>/config.json`，定位 `quantization_config`，按本模块的表格逐字段解释。
   - 用 transformers 验证加载器能识别：
     ```python
     # 示例代码
     from transformers import AutoConfig
     cfg = AutoConfig.from_pretrained("<SAVE_DIR>")
     qc = cfg.quantization_config
     print(qc["quant_method"])   # 期望: compressed-tensors
     print(qc["format"])         # 期望: 由 FP8_BLOCK 推断出的 format
     print(qc["config_groups"])  # 期望: 含 weights=fp8 / input_activations=fp8 的 group
     ```
3. **需要观察的现象**：`quant_method` 恒为 `compressed-tensors`；`config_groups` 里 `targets` 含 `Linear`、`ignore` 含 `lm_head`（及你显式 ignore 的模块）；权重/激活的 `num_bits`/`type` 与 `FP8_BLOCK` 方案一致。
4. **预期结果**：能在自己的 `config.json` 里找到 `QuantizationModifier` 当初设的 `targets`/`ignore`/`scheme` 的痕迹；若有 vLLM 环境，`LLM("<SAVE_DIR>")` 应能正常加载。**FP8 推理与具体 format 待本地验证**。
5. 若无 GPU 跑量化，可去 HuggingFace 任意一个带 `quant_method: compressed-tensors` 的模型仓库（如 NeuralMagic 的量化模型）下载其 `config.json` 做字段阅读练习。

#### 4.4.5 小练习与答案

**练习 1**：vLLM 是凭什么判断一个模型是 compressed-tensors 量化模型的？

> **答案**：凭 `config.json` 里 `quantization_config.quant_method == "compressed-tensors"`。这是 compressed-tensors 加载器的分发钥匙，也是 `is_model_ct_quantized_from_path` 的判据。

**练习 2**：`quantization_config` 里的 `config_groups` / `ignore` / `kv_cache_scheme` 与 `QuantizationModifier` 的字段是什么关系？

> **答案**：一一对应。`QuantizationModifier` 的 `config_groups`（由 `scheme` 归一化而来）、`ignore`、`kv_cache_scheme` 在保存阶段经 `compressor.update_config` 原样写进 `config.json` 的 `quantization_config`。所以产物里的这几个字段就是你当初设的方案。

**练习 3**：为什么对已经是 compressed-tensors 量化的模型再 oneshot，加载时要传 `run_compressed=False`？

> **答案**：压缩态权重（已打包的 int/fp8）无法直接再被量化算法处理。`run_compressed=False`（见 `initialize_model_from_path` L164-L168）让模型以解压的全精度形式加载，才能继续新一轮校准；同时 `validate_model` 会对「再次量化已量化层」给出警告。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「量化 → 保存 → 解读产物 → 验证可加载」的完整闭环。

**任务**：选一个小模型，用 `QuantizationModifier` 做一次 FP8 动态量化，保存到磁盘，然后回答下列问题（每问对应一个模块）：

1. **（4.1）** 在不写 `model.save_pretrained(...)` 的情况下，仅靠 `oneshot(output_dir=...)` 也能落盘吗？为什么？→ 提示：看 `post_process` 的 `output_dir` 判定。
2. **（4.2）** 同一个模型分别用 `save_compressed=True` 和 `save_compressed=False` 保存，比较两者 `.safetensors` 体积与 `quantization_status`，解释差异来源。
3. **（4.3）** 打开产物目录，确认除权重外是否还有 `recipe.yaml`；用 `Recipe.create_instance(file_path=".../recipe.yaml")`（u2-l5）重新解析它，看能否还原你传入的 modifier。
4. **（4.4）** 打开 `config.json`，把 `quantization_config` 的每个字段对上你当初在 `QuantizationModifier` 里设的参数；最后用 transformers 的 `AutoConfig.from_pretrained` 确认 `quant_method`，若有 vLLM 环境则 `LLM("<SAVE_DIR>")` 验证可加载。

**参考脚本骨架**（基于 `examples/quantization_w8a8_fp8/fp8_block_example.py` 改写）：

```python
# 示例代码
from transformers import AutoModelForCausalLM, AutoTokenizer
from llmcompressor import oneshot
from llmcompressor.modifiers.quantization import QuantizationModifier

MODEL_ID = "<一个小模型>"          # 待本地确认
model = AutoModelForCausalLM.from_pretrained(MODEL_ID)
tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)

recipe = QuantizationModifier(
    targets="Linear",
    scheme="FP8_DYNAMIC",          # 动态激活、无需校准数据
    ignore=["lm_head"],
)

oneshot(
    model=model,
    recipe=recipe,
    output_dir="my-fp8-model",     # 触发 post_process 自动保存
    save_compressed=True,
)
# 产物：my-fp8-model/{config.json, *.safetensors, recipe.yaml, tokenizer 文件}
```

> **运行结果说明**：本讲所有量化/保存的可观察数值（文件体积、`quantization_status`、`format` 具体取值）均与模型及压缩方案强相关，**待本地验证**；脚本骨架的源码依据来自 [fp8_block_example.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/examples/quantization_w8a8_fp8/fp8_block_example.py) 与 [saving_a_model.md](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/guides/saving_a_model.md)。

---

## 6. 本讲小结

- **保存是三阶段的最后一步，但装备在校准前就装好了**：`pre_process` 调 `modify_save_pretrained` 把 `save_pretrained` 换成压缩版（伏笔），`post_process` 调 `save_pretrained` 才真正落盘。
- **`modify_save_pretrained` 用闭包包装原方法**：弱引用保留原方法、`_overridden` 标志保证幂等，换上去的 `save_pretrained_wrapper` 接上了 compressed-tensors 的 `ModelCompressor` 序列化引擎。
- **两个旋钮分清**：`quantization_format`（怎么存，可从 scheme 推断）控制磁盘字节布局；`save_compressed`（要不要真打包）控制权重是 `compressed` 还是全精度 `frozen`。
- **落盘不止写权重**：还会 re-tie 相等 embeddings、转 accelerate、回写 `quantization_config`、落 `recipe.yaml`、复制 modeling python / MTP 权重；分布式下只让 source 进程写盘。
- **compressed-tensors 格式 = safetensors 权重 + `config.json.quantization_config`**，`quant_method: "compressed-tensors"` 是 vLLM 分发加载器的唯一钥匙，`config_groups`/`ignore`/`kv_cache_scheme` 直接对应 `QuantizationModifier` 的字段。
- **稀疏压缩（含 2:4）已被 compressed-tensors 移除**，任何稀疏相关参数现在只会得到一条废弃警告。

## 7. 下一步学习建议

- **u6-l4 自定义 Modifier** 与 **u6-l5 自定义 Observer**：本讲看到「保存阶段会把 recipe 落盘、把 scheme 写进 config」，下一讲会教你如何写出能被 `ModifierFactory` 发现、进而能被这套保存流程正确序列化的自定义压缩算子。
- **u6-l6 测试与贡献流程**：若你想为本讲的保存逻辑（如 re-tie embeddings、recipe 合并）补测试，可参考该讲的 pytest marker 体系。
- **深入 compressed-tensors**：本讲把「字节布局怎么打包、`update_config` 写哪些字段」标记为外部库行为。若要做 vLLM 侧兼容性开发，建议直接阅读 `compressed_tensors` 仓库的 `ModelCompressor`、`QuantizationFormat`、`CompressionFormat` 与 `save_compressed` 的实现。
