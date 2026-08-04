# Independent / Basic / DataFree 管线

## 1. 本讲目标

本讲是「量化与校准管线」单元的最后一篇，聚焦 `oneshot` 默认管线 **IndependentPipeline**，以及它身后的两个常用子管线 **BasicPipeline** 与 **DataFreePipeline**。学完本讲你应当能够：

1. 说清楚 `IndependentPipeline` 作为「元管线（meta-pipeline）」是如何为 recipe 里的**每一个 modifier 单独推断并运行**最合适的子管线的。
2. 说清楚 `DataFreePipeline` 为什么在**完全不喂校准数据**的情况下，仍然能完成 RTN（round-to-nearest）权重量化。
3. 在 recipe 包含**多个 modifier** 时，预测独立管线与「强制单一管线」在「实际使用的管线」和「校准 epoch 数」上的差异。

本讲承接 u3-l4（校准管线选择与 `CalibrationPipeline` 注册表），不再重复注册机制，只钻进三种管线的 `__call__` 实现。

## 2. 前置知识

阅读本讲前，请确认你已经理解以下概念（均在 u2、u3 前序讲义中建立）：

- **校准管线（CalibrationPipeline）**：一段负责「喂校准数据 → 触发 modifier 生命周期钩子 → 让 modifier 完成压缩」的代码。所有管线都继承自 `CalibrationPipeline`，并通过 `@CalibrationPipeline.register(name)` 注册到全局表（见 u3-l4）。
- **四种管线与命名**：`independent`（元管线，oneshot 默认）、`sequential`（逐子图两遍前向、传播量化误差）、`basic`（整模型一遍前向、不传播误差）、`datafree`（无数据、只触发生命周期回调）。
- **`from_modifiers` 的推断规则**：`_infer_pipeline` 只看 `any(m.requires_calibration_data)`，需要数据返回 `sequential`，否则返回 `datafree`；`independent`/`basic` 永远不会作为「推断结果」出现，只能由用户显式指定。
- **modifier 的双生命周期钩子**：`on_calibration_start` → `on_sequential_epoch_end(modules)` → `on_calibration_end`（服务 PTQ，详见 u2-l3）。管线正是通过 `LifecycleCallbacks` 的三个语义方法去触发它们的。
- **RTN 取整发生点**：在 `on_sequential_epoch_end` 里调用 `observe(weight)` + `update_qparams(weight)` 完成权重的就近取整（详见 u3-l1、u3-l3）。

一个贯穿全讲的关键直觉：**管线本身不做量化数学**，它只决定「喂多少数据、喂几次、把哪些模块传给 `on_sequential_epoch_end`」。真正改权重的是 modifier 的钩子。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/llmcompressor/pipelines/registry.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/registry.py) | `CalibrationPipeline` 抽象基类、`from_modifiers`（含用户指定与推断的优先级）、`_infer_pipeline`（u3-l4 精读，本讲引用）。 |
| [src/llmcompressor/pipelines/independent/pipeline.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/independent/pipeline.py) | `IndependentPipeline`：oneshot 默认元管线，逐 modifier 委派子管线。 |
| [src/llmcompressor/pipelines/basic/pipeline.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/basic/pipeline.py) | `BasicPipeline`：整模型一遍前向、不传播误差；另含便捷函数 `run_calibration`。 |
| [src/llmcompressor/pipelines/data_free/pipeline.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/data_free/pipeline.py) | `DataFreePipeline`：无数据驱动四个生命周期钩子。 |
| [src/llmcompressor/pipelines/__init__.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/__init__.py) | 用 `import *` 副作用触发四种管线的 `@register`，填充注册表。 |
| [src/llmcompressor/core/session_functions.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session_functions.py) | `LifecycleCallbacks.calibration_start / sequential_epoch_end / calibration_end`：三种管线都靠这三个语义方法驱动 modifier。 |
| [src/llmcompressor/entrypoints/oneshot.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py) | `oneshot()` 的 `pipeline` 参数默认值是 `"independent"`，并在校准阶段调用 `from_modifiers`。 |

## 4. 核心概念与源码讲解

### 4.1 BasicPipeline：整模型一遍前向、不传播误差

#### 4.1.1 概念说明

`BasicPipeline` 是最朴素的「有数据」校准管线：把整个模型当成一个整体，对 dataloader 里的每一个 batch 做一次标准 `model(**batch)` 前向，前向过程中由 modifier 挂上的校准 hook 收集激活统计；全部 batch 跑完后，**只调用一次** `sequential_epoch_end`（传入模型的**所有模块**），让 modifier 一次性完成权重量化。

它的核心定位写在自己的 docstring 里：与 sequential 管线不同，它**不传播压缩误差**。也就是说，每一层在前向时都吃的是上一层「未量化」的全精度输出，量化误差不会在层间累积。这让 basic 适合「我只要简单收集一下统计、不关心误差跨层放大」的场景，但代价是对 GPTQ 这类依赖误差传播的算法不够精确。

#### 4.1.2 核心流程

```
calibration_start()                         # 触发各 modifier 的 on_calibration_start（挂 observer/hook）
with calibration_forward_context(model):    # 关梯度、关 KV cache、关 lm_head、关 hf kernel
  with DisableQuantization(model):          # 关掉 compressed-tensors 的量化前向（层间跑全精度）
    for batch in dataloader:                # tqdm 进度条显示 "Calibrating"
      state.current_batch_idx = batch_idx   # 告诉 modifier 现在是第几个 batch
      model(**batch)                        # hook 在前向中悄悄收集激活统计
sequential_epoch_end(所有模块)              # 一次性触发各 modifier 的 on_sequential_epoch_end（取整）
calibration_end()                           # 触发各 modifier 的 on_calibration_end（卸 hook、冻结）
```

两个关键点：

1. **`DisableQuantization`** 包住整个前向循环，意味着校准期间的「数值流」是全精度的，这是「不传播误差」的代码层面的直接原因；校准 hook 仍能在 `forward_pre_hook` 上捕获真实的全精度输入来算 scale。
2. **`sequential_epoch_end` 只调用一次**，且传入 `list(model.modules())`（全部模块），而不是像 sequential 那样「每个子图调一次、只传子图模块」。

#### 4.1.3 源码精读

注册与签名：[src/llmcompressor/pipelines/basic/pipeline.py:L22-L29](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/basic/pipeline.py#L22-L29) —— `@CalibrationPipeline.register("basic")` 把它登记为名字 `"basic"`，`__call__` 接收 `model`、`dataloader`、`dataset_args`（注意 `dataset_args` 允许为 `None`）。

前向主干：[src/llmcompressor/pipelines/basic/pipeline.py:L57-L73](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/basic/pipeline.py#L57-L73)。这段做了三件事：先 `calibration_start()`；然后用 `contextlib.ExitStack` 同时进入 `calibration_forward_context(model)`（统一关梯度/关 cache/关 lm_head）和 `DisableQuantization(model)`；最后在 `tqdm(dataloader, desc="Calibrating")` 里逐 batch 前向，并写入 `session.state.current_batch_idx`。

收尾三连：[src/llmcompressor/pipelines/basic/pipeline.py:L75-L76](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/basic/pipeline.py#L75-L76) —— `sequential_epoch_end(list(model.modules()))` 把**全部模块**交给 modifier 取整，紧接着 `calibration_end()` 收尾。

CPU 提示与 loss_mask：[src/llmcompressor/pipelines/basic/pipeline.py:L43-L55](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/basic/pipeline.py#L43-L55) —— 若执行设备是 CPU 会告警建议 `device_map='auto'`；`use_loss_mask`（AWQ 掩码支持）会把每个 batch 的 `loss_mask` 收集进 `session.state.loss_masks`。

便捷函数 `run_calibration`：[src/llmcompressor/pipelines/basic/pipeline.py:L79-L81](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/basic/pipeline.py#L79-L81) —— 一个极薄包装：`BasicPipeline()(model, dataloader, None)`。它被 SparseGPT 在内部复用来收集激活（见 [src/llmcompressor/modifiers/pruning/sparsegpt/sgpt_base.py:L231-L247](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/pruning/sparsegpt/sgpt_base.py#L231-L247)），说明 basic 管线不仅是 oneshot 的可选项，也是算法内部「跑一遍前向收激活」的通用工具。

#### 4.1.4 代码实践

**实践目标**：确认 `BasicPipeline` 只跑一遍 dataloader、只触发一次 `sequential_epoch_end`。

**操作步骤**：

1. 准备一个极小的因果语言模型（如本地 tiny 模型或 `hf-internal-testing/tiny-random-llama` 风格的小模型）和少量校准样本。
2. 用一条只含 `QuantizationModifier` 的 recipe，**强制** `pipeline="basic"` 跑 oneshot：

```python
# 示例代码：强制使用 basic 管线
from llmcompressor import oneshot
from llmcompressor.modifiers.quantization import QuantizationModifier

recipe = QuantizationModifier(targets="Linear", scheme="W8A8", ignore=["lm_head"])
oneshot(
    model="<你的小模型>",
    recipe=recipe,
    dataset="open_platypus",
    num_calibration_samples=4,
    pipeline="basic",          # 关键：强制 basic
    output_dir="./out-basic",
)
```

3. 观察终端日志。

**需要观察的现象**：

- 只出现**一个** `Calibrating` 进度条，长度等于 batch 数（4 个样本 / batch_size）。
- 不会出现 sequential 那种 `(1/N): Calibrating`、`(1/N): Propagating` 的子图两遍前向日志。

**预期结果**：日志显示单次整模型校准循环；若把日志级别调到 DEBUG，能看到 `sequential_epoch_end` 在循环结束后**只触发一次**。

> 待本地验证：上述日志现象依赖真实模型与 GPU/CPU 环境，请在本地运行后核对。

#### 4.1.5 小练习与答案

**练习 1**：`BasicPipeline` 为什么「不传播压缩误差」？请结合源码中的某个上下文管理器回答。

**参考答案**：因为前向循环被 `DisableQuantization(model)` 包住（basic/pipeline.py:L61），校准期间 compressed-tensors 的量化被关闭，每层都吃上一层全精度输出，量化误差不在层间累积。

**练习 2**：`BasicPipeline` 调用 `sequential_epoch_end` 时传入的模块列表是什么？这与 sequential 管线有何不同？

**参考答案**：传入 `list(model.modules())`，即模型的**全部模块**，且只调用一次；sequential 管线则是「每个子图调用一次」，每次只传入该子图的模块（`subgraph.submodules(model)`）。

---

### 4.2 DataFreePipeline：无数据也能驱动四个生命周期钩子

#### 4.2.1 概念说明

`DataFreePipeline` 服务于**完全不需要校准数据**的压缩方案，最典型的就是 RTN（就近取整）类的 `QuantizationModifier`，例如 `scheme="FP8_DYNAMIC"` 或 `"W4A16"`。这类方案的权重取整只依赖权重本身（`observe(weight)` + `update_qparams(weight)`），不需要任何激活统计。

但关键在于：modifier 的取整逻辑写在 `on_sequential_epoch_end` 钩子里（见 u3-l1）。要让这个钩子被触发，**必须有人去调用 `LifecycleCallbacks` 的三个语义方法**。`DataFreePipeline` 的全部工作，就是「不喂任何数据、把这三个回调按顺序各调一次」，从而把 modifier 的四个生命周期钩子（`on_initialize` 已在 `session.initialize` 阶段触发，再加 `on_calibration_start` / `on_sequential_epoch_end` / `on_calibration_end`）全部跑完。

#### 4.2.2 核心流程

```
onload_device = get_main_device()       # 取主设备（GPU 优先）
set_onload_device(model, onload_device) # 让 offload 的权重为 modifier 的运算上 GPU
calibration_start()                     # on_calibration_start
sequential_epoch_end(所有模块)          # on_sequential_epoch_end —— RTN 在此取整
calibration_end()                       # on_calibration_end —— 卸 hook、冻结
```

注意：这里**没有 for batch 循环**，dataloader 形参类型是 `Optional[DataLoader]`，即可以传 `None`，也根本不会被迭代。

#### 4.2.3 源码精读

完整实现：[src/llmcompressor/pipelines/data_free/pipeline.py:L17-L39](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/data_free/pipeline.py#L17-L39)。注册名 `"datafree"`，`__call__` 的 `dataloader` 是 `Optional[DataLoader]`（[L23](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/data_free/pipeline.py#L23)）。

为何要先设 onload 设备：[src/llmcompressor/pipelines/data_free/pipeline.py:L32-L35](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/data_free/pipeline.py#L32-L35)。注释写得很直白：「some ops are still performed on the model by modifiers / we want those ops to occur on the GPU」——即使没有数据前向，modifier 在 `on_sequential_epoch_end` 里仍要对权重做统计与取整（涉及大矩阵运算），把这些算子放到 GPU 上更快。`get_main_device()` 定义在 [src/llmcompressor/utils/dev.py:L157](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/utils/dev.py#L157)。

三个回调：[src/llmcompressor/pipelines/data_free/pipeline.py:L37-L39](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/data_free/pipeline.py#L37-L39)，分别对应 `calibration_start()`、`sequential_epoch_end(list(model.modules()))`、`calibration_end()`。和 basic 一样，`sequential_epoch_end` 也传**全部模块**、只调一次。

这三个语义方法的定义在 [src/llmcompressor/core/session_functions.py:L147-L175](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session_functions.py#L147-L175)：`calibration_start` → `CALIBRATION_START`、`sequential_epoch_end(modules)` → `SEQUENTIAL_EPOCH_END`、`calibration_end` → `CALIBRATION_END`，它们经 `active_session().event(...)` 广播给 recipe 中的每个 modifier（机制见 u2-l2）。

#### 4.2.4 代码实践

**实践目标**：确认 `DataFreePipeline` 在「不传 dataset」的情况下仍能完成 RTN 量化。

**操作步骤**：

```python
# 示例代码：无数据的 FP8 动态量化（RTN）
from llmcompressor import oneshot
from llmcompressor.modifiers.quantization import QuantizationModifier

recipe = QuantizationModifier(targets="Linear", scheme="FP8_DYNAMIC", ignore=["lm_head"])
oneshot(
    model="<你的小模型>",
    recipe=recipe,
    pipeline="datafree",      # 强制无数据管线
    output_dir="./out-datafree",
    # 注意：没有传 dataset
)
```

**需要观察的现象**：

- 终端**不会**出现 `Calibrating` 的 tqdm 数据循环（因为根本没有 for batch 循环）。
- 进程仍然正常结束，`./out-datafree/config.json` 里能看到 `quantization_config` 字段被写入。

**预期结果**：模型被成功量化并保存，证明 RTN 的取整只发生在 `sequential_epoch_end` 钩子里、与数据无关。

> 待本地验证：请在本地用真实小模型运行核对 config.json 的 `quantization_config`。

#### 4.2.5 小练习与答案

**练习 1**：既然 `DataFreePipeline` 不迭代 dataloader，为什么它的 `__call__` 还要接收一个 `dataloader` 参数？

**参考答案**：因为所有 `CalibrationPipeline.__call__` 共享同一个签名（`model, dataloader, dataset_args`，见 registry.py 的抽象方法），统一签名才能让 `from_modifiers` 返回的任意管线被同一行 `pipeline(model, dataloader, dataset_args)` 调用；datafree 把它声明为 `Optional` 并忽略不用。

**练习 2**：把一个 `requires_calibration_data=True` 的 modifier（例如 W8A8 Int8 静态激活量化）强制塞进 `pipeline="datafree"` 会发生什么？

**参考答案**：`from_modifiers` 会先打印告警「推荐用 sequential」但仍放行；运行时 datafree 不喂任何数据，该 modifier 的校准 hook 永远不触发、统计为空，`update_qparams` 拿不到有效统计，量化结果会是错的或报错。这是用户误用，靠告警提示。

---

### 4.3 IndependentPipeline：逐 modifier 委派的元管线

#### 4.3.1 概念说明

`IndependentPipeline` 是 **oneshot 的默认管线**（[oneshot.py:L341](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L341) 中 `pipeline: str | None = "independent"`）。它本身**不做任何校准**，而是一个「调度器 / 元管线（meta-pipeline）」：遍历 recipe 里的每一个 modifier，为它**单独推断**一个最合适的子管线，然后让那个子管线跑一个完整的校准 epoch。

为什么需要它？因为一条 recipe 经常包含多个性质不同的 modifier，例如：

- `[SmoothQuantModifier, QuantizationModifier(W8A8)]`：SmoothQuant 是变换类、需要数据 → 想用 sequential；
- `[AWQModifier, QuantizationModifier(W4A16)]`：AWQ 需要数据 → sequential，而 W4A16 是 RTN → datafree；
- `[QuantizationModifier(FP8_DYNAMIC)]`：单个 RTN → datafree。

如果强行用同一种管线跑所有 modifier，要么浪费（RTN 却跑了 sequential）、要么出错（把需要数据的 modifier 塞进 datafree）。IndependentPipeline 的做法是「**每个 modifier 各跑一个最适合它的子管线**」，代价是 N 个 modifier 就跑 N 个校准 epoch。

#### 4.3.2 核心流程

```
modifiers = session.lifecycle.recipe.modifiers        # 取出全部 modifier
with patch_attr(recipe, "modifiers", None):           # 临时把 recipe.modifiers 置空
  for modifier in modifiers:
    recipe.modifiers = [modifier]                      # 窗口缩到「只有当前这一个」
    sub = CalibrationPipeline.from_modifiers([modifier])  # 为它推断子管线（sequential/datafree）
    sub(model, dataloader, dataset_args)               # 跑一个完整校准 epoch
# with 退出：recipe.modifiers 恢复成完整列表（供后续 finalize/save 使用）
```

关键设计：通过 `patch_attr` **临时收窄** `recipe.modifiers`，让子管线在读取 `session.lifecycle.recipe.modifiers` 时只看到当前这一个 modifier——从而子管线触发的生命周期回调只广播给这一个 modifier。循环结束后恢复完整列表。

校准 epoch 数与 modifier 数的关系：

\[
\text{独立管线的校准 epoch 数} \;=\; N_{\text{modifiers}}
\]

而任何一种「强制单一管线」（basic/sequential/datafree）的校准 epoch 数恒为 1（sequential 的「子图数」是子图维度，不等于这里的「modifier 维度 epoch」）。

#### 4.3.3 源码精读

完整实现仅约 30 行：[src/llmcompressor/pipelines/independent/pipeline.py:L17-L48](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/independent/pipeline.py#L17-L48)。

取 modifier 与收窄窗口：[src/llmcompressor/pipelines/independent/pipeline.py:L36-L41](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/independent/pipeline.py#L36-L41)。`modifiers = session.lifecycle.recipe.modifiers` 先快照完整列表；`with patch_attr(session.lifecycle.recipe, "modifiers", None)` 进入时把 `recipe.modifiers` 临时设为 `None`，循环体内再赋成 `[modifier]`。`patch_attr` 来自 `compressed_tensors.utils`（[L4](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/independent/pipeline.py#L4)），是一个上下文管理器，退出时自动恢复原值。

逐个推断并运行：[src/llmcompressor/pipelines/independent/pipeline.py:L38-L45](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/independent/pipeline.py#L38-L45)。对每个 modifier：记录其类名 `mod_type`；把 recipe 收窄到 `[modifier]`；调用 `CalibrationPipeline.from_modifiers([modifier])` 推断子管线（这里**不传 `user`**，所以走纯推断：需要数据→sequential，否则→datafree）；用 `_logger.info(f"Inferred {pipeline_name} for {mod_type}")` 打印推断结果；最后 `pipeline(model, dataloader, dataset_args)` 跑这个子管线。

恢复注释：[src/llmcompressor/pipelines/independent/pipeline.py:L47](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/independent/pipeline.py#L47) ——「restore modifiers on exit so model can be compressed based on recipe」。`with` 退出后 `recipe.modifiers` 恢复完整列表，保证 oneshot 后续的 `session.finalize()` 与保存逻辑能看到所有 modifier。

`from_modifiers` 的推断与告警逻辑（u3-l4 已精读，这里只引用关键行）：[src/llmcompressor/pipelines/registry.py:L27-L53](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/registry.py#L27-L53)。注意 [L43-L44](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/registry.py#L43-L44) 的特殊分支：当 `user == "independent"` 时，把 `inferred` 也改写成 `"independent"`，从而**不触发「推荐不一致」的告警**——这正是 oneshot 默认传 `"independent"` 却不会刷屏告警的原因；而 IndependentPipeline 内部递归调用 `from_modifiers([modifier])` 时 `user=None`，走纯推断。

oneshot 如何调用管线：[src/llmcompressor/entrypoints/oneshot.py:L259-L268](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L259-L268) —— `user_pipeline = self.dataset_args.pipeline`（默认 `"independent"`），`pipeline = CalibrationPipeline.from_modifiers(recipe.modifiers, user=user_pipeline)`，然后 `pipeline(model, calibration_dataloader, dataset_args)`。

#### 4.3.4 代码实践

**实践目标**：看到 IndependentPipeline 为每个 modifier 打印的 `Inferred ... for ...` 日志，并数出校准 epoch 数。

**操作步骤**：

```python
# 示例代码：用默认（independent）管线跑一个含两个 modifier 的 recipe
from llmcompressor import oneshot
from llmcompressor.modifiers.quantization import QuantizationModifier

recipe = [
    QuantizationModifier(targets="Linear", scheme="FP8_DYNAMIC", ignore=["lm_head"]),
]
oneshot(
    model="<你的小模型>",
    recipe=recipe,
    # pipeline 不传 → 默认 "independent"
    output_dir="./out-independent",
)
```

**需要观察的现象**：

- 日志里出现一行类似 `Inferred DataFreePipeline for QuantizationModifier`（因为 `FP8_DYNAMIC` 的 `requires_calibration_data=False`，推断为 datafree）。
- 若把 recipe 扩成两个 modifier（例如再加一个 RTN 的 `QuantizationModifier` 指向不同 `targets`），会看到**两行** `Inferred ...`，对应**两个**校准 epoch。

**预期结果**：modifier 数 = `Inferred ...` 日志行数 = 校准 epoch 数。

> 待本地验证：日志文案与行数请在本地运行后核对。

#### 4.3.5 小练习与答案

**练习 1**：IndependentPipeline 为什么要用 `patch_attr` 临时把 `recipe.modifiers` 收窄到 `[modifier]`，而不是直接遍历调用？

**参考答案**：因为子管线（sequential/datafree/basic）在运行时会读取 `session.lifecycle.recipe.modifiers` 来决定把生命周期回调广播给谁。若不收窄，子管线会把回调广播给所有 modifier，导致每个 modifier 被重复校准 N 次。收窄到单个 modifier 后，每次只有当前 modifier 收到回调。

**练习 2**：oneshot 默认 `pipeline="independent"`，但 `from_modifiers` 的 `_infer_pipeline` 永不返回 `"independent"`。这两件事矛盾吗？

**参考答案**：不矛盾。`user="independent"` 是「用户指定」，绕过 `_infer_pipeline` 的推断结果直接从注册表取 `IndependentPipeline`；而 IndependentPipeline 内部对每个 modifier 调 `from_modifiers([m], user=None)` 时才走真正的推断（sequential/datafree）。`independent` 是元管线层的选择，`sequential`/`datafree` 是子管线层的推断，二者在不同层级。

---

### 4.4 三种管线对比与多 modifier 组合行为

#### 4.4.1 概念说明

把本讲三种管线与 sequential 放在一起对比，能看清「该用哪个」的决策依据。核心差异集中在三个维度：**是否需要 dataloader**、**`sequential_epoch_end` 调用几次/传哪些模块**、**是否传播量化误差**。

当 recipe 含多个 modifier 时，还出现一个只在 IndependentPipeline 才有的现象：「逐 modifier 多 epoch」。这是独立管线与「强制单一管线」最实用的区别。

#### 4.4.2 核心流程（对比表）

| 管线（注册名） | 需要 dataloader | 前向循环 | `sequential_epoch_end` 调用 | 误差传播 | 典型场景 |
| --- | --- | --- | --- | --- | --- |
| `independent` | 透传给子管线（本身不迭代） | 取决于子管线 | 取决于子管线 | 取决于子管线 | **oneshot 默认**、多 modifier recipe |
| `basic` | 是 | 整模型一遍（`for batch`） | **1 次**，传**全部模块** | 否 | 简单收集统计、SparseGPT 取激活 |
| `datafree` | 否（`Optional`） | **无** | **1 次**，传**全部模块** | 不适用 | RTN 无数据量化（FP8_DYNAMIC/W4A16） |
| `sequential`（u3-l5） | 是 | 每子图两遍 | **每子图 1 次**，传**子图模块** | 是 | GPTQ/W8A8/AWQ/SmoothQuant |

多 modifier 组合行为（学习目标 3 的核心）：

```
recipe = [Modifier_A, Modifier_B]            # 两个 modifier

pipeline="independent"（默认）:
  epoch 1: 为 A 推断子管线 → 跑一个完整校准
  epoch 2: 为 B 推断子管线 → 跑一个完整校准
  → 共 2 个校准 epoch，A、B 各自在最适合的子管线里跑

pipeline="sequential"（强制）:
  A、B 的 hook 同时挂载，在一个 sequential 流程里一起校准
  → 共 1 个校准流程（含子图维度），两 modifier 共享同一批前向

pipeline="basic"（强制）:
  A、B 在同一次整模型前向循环里一起收集统计、末尾一起取整
  → 共 1 个校准 epoch
```

#### 4.4.3 源码精读（差异点定位）

「全部模块 vs 子图模块」的差异，是 basic/datafree 与 sequential 的分水岭：

- basic 调一次、传全部：[src/llmcompressor/pipelines/basic/pipeline.py:L75](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/basic/pipeline.py#L75) —— `LifecycleCallbacks.sequential_epoch_end(list(model.modules()))`。
- datafree 调一次、传全部：[src/llmcompressor/pipelines/data_free/pipeline.py:L38](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/data_free/pipeline.py#L38) —— 同样 `list(model.modules())`。
- sequential 每子图调一次、只传子图：[src/llmcompressor/pipelines/sequential/pipeline.py:L160](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L160) —— `LifecycleCallbacks.sequential_epoch_end(subgraph.submodules(model))`。

「多 epoch」只发生在 independent：[src/llmcompressor/pipelines/independent/pipeline.py:L38-L45](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/independent/pipeline.py#L38-L45) 的 for 循环每轮都调一次 `pipeline(...)`（即一个完整子管线校准）。

注册填充：[src/llmcompressor/pipelines/__init__.py:L13-L17](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/__init__.py#L13-L17) —— `from .basic import *` / `.data_free` / `.independent` / `.sequential` 通过 `import *` 触发各文件顶部的 `@CalibrationPipeline.register(...)`，把四个名字填进 `RegistryMixin` 的注册表（机制见 u3-l4）。

#### 4.4.4 代码实践

**实践目标**：用同一条 recipe，对比「默认 independent」与「强制 datafree」在实际使用的管线与校准 epoch 数上的差异。

**操作步骤**：

1. 用一个**单 modifier、RTN** 的 recipe（推断为 datafree），分别用默认（independent）和强制 datafree 各跑一次：

```python
# 示例代码：对比 independent（默认）与 datafree（强制）
from llmcompressor import oneshot
from llmcompressor.modifiers.quantization import QuantizationModifier

recipe = QuantizationModifier(targets="Linear", scheme="W4A16", ignore=["lm_head"])

# 情形 A：默认 independent
oneshot(model="<小模型>", recipe=recipe, output_dir="./out-A")  # pipeline 默认 "independent"

# 情形 B：强制 datafree
oneshot(model="<小模型>", recipe=recipe, pipeline="datafree", output_dir="./out-B")
```

2. 把 recipe 扩成两个 modifier 再跑一次默认 independent，数 `Inferred ...` 日志行数。

**需要观察的现象**：

- 情形 A：日志出现 `Inferred DataFreePipeline for QuantizationModifier`，随后 datafree 子管线运行（无数据循环）。校准 epoch 数 = 1。
- 情形 B：日志**没有** `Inferred ...` 行（因为直接用了 datafree，未经 independent 调度），同样无数据循环。校准 epoch 数 = 1。
- 两 modifier 的情形 A：出现**两行** `Inferred ...`，校准 epoch 数 = 2。

**预期结果**：单 modifier 时 A、B 结果等价（都走 datafree），区别只在「是否多一层 independent 调度日志」；多 modifier 时只有 independent 会产生「逐 modifier 多 epoch」。

> 待本地验证：以上日志与 epoch 计数请在本地运行核对；若本地无 GPU，可仅阅读源码确认 `Inferred ...` 日志出自 independent/pipeline.py:L43。

#### 4.4.5 小练习与答案

**练习 1**：一条 `[AWQModifier, QuantizationModifier(W4A16)]` 的 recipe 用默认 independent 运行，会跑几个校准 epoch？分别用什么子管线？

**参考答案**：2 个 epoch。AWQModifier 需要校准数据（`requires_calibration_data=True`）→ 推断为 sequential；W4A16 的 QuantizationModifier 是 RTN → 推断为 datafree。所以是「先一个 sequential epoch 完成 AWQ 变换，再一个 datafree epoch 完成量化」。

**练习 2**：为什么说 `basic` 和 `datafree` 都把 `sequential_epoch_end` 传「全部模块」，而这样做不会让 modifier 把不相关的模块也量化了？

**参考答案**：因为传入的「全部模块」只是「候选集合」，每个 modifier 在 `on_sequential_epoch_end` 内部会用自己的 `resolved_targets` / `ignore` 过滤，只对真正命中的目标模块执行 `observe` + `update_qparams`（见 u3-l1、u3-l2）。管线只负责「把候选模块递过去」，筛选是 modifier 自己的事。

## 5. 综合实践

**任务**：用一条 recipe，把本讲的三种「非 sequential」管线串起来观察。

1. 选一个极小模型，构造 recipe = `[QuantizationModifier(scheme="W4A16", ignore=["lm_head"])]`（RTN，推断为 datafree）。
2. 分三次运行 `oneshot`，仅改变 `pipeline` 参数：
   - 不传（默认 `independent`）
   - `pipeline="datafree"`
   - `pipeline="basic"`（需要额外传 `dataset` 与 `num_calibration_samples`，因为 basic 会迭代 dataloader）
3. 每次运行记录：① 日志里出现的管线名（`Inferred ...` 或 `Calibrating` 进度条）；② 是否出现数据循环；③ 校准 epoch 数。
4. 把 recipe 改成两个 modifier（例如两个 `targets` 不同的 RTN `QuantizationModifier`），再用默认 independent 跑一次，确认 epoch 数变成 2。
5. 用一张表汇总「pipeline 参数 → 实际子管线 → epoch 数」的对应关系。

**预期结论**：

| pipeline 参数 | 实际运行管线 | 数据循环 | epoch 数 |
| --- | --- | --- | --- |
| 默认（independent） | independent → datafree（单 modifier） | 无 | 1 |
| `datafree` | datafree | 无 | 1 |
| `basic` | basic | 有（一遍） | 1 |
| 默认（independent）+ 双 modifier | independent → datafree ×2 | 无 | 2 |

> 待本地验证：表格中「epoch 数」依赖对日志的实际计数，请在本地运行后填充确认。

## 6. 本讲小结

- **管线不做量化数学**：四种管线只决定「喂多少数据、喂几次、把哪些模块交给 `on_sequential_epoch_end`」，真正改权重的是 modifier 的钩子。
- **BasicPipeline**：整模型一遍前向、`DisableQuantization` 下收集统计、末尾**一次** `sequential_epoch_end(全部模块)`，**不传播误差**；`run_calibration` 是它的薄包装，被 SparseGPT 复用来取激活。
- **DataFreePipeline**：不迭代 dataloader（形参 `Optional`），只按序调三个生命周期回调，让 RTN 类 modifier 在 `on_sequential_epoch_end` 完成取整；先 `set_onload_device` 把权重运算放到 GPU。
- **IndependentPipeline**：oneshot 默认的「元管线」，用 `patch_attr` 临时收窄 `recipe.modifiers`，**逐 modifier** 推断并运行最合适的子管线；modifier 数 = 校准 epoch 数。
- **「全部模块 vs 子图模块」** 是 basic/datafree 与 sequential 的分水岭：前者一次传全部模块，后者每子图传该子图模块。
- **多 modifier 组合**：只有 independent 产生「逐 modifier 多 epoch」；强制单一管线会把所有 modifier 塞进同一次校准流程。

## 7. 下一步学习建议

- 进入 **u5-l1（校准数据集与 DataLoader 构建）**，理解 oneshot 里传给上述管线的 `dataloader` 是如何由 `get_calibration_dataloader` 从数据集名 / `Dataset` / `DataLoader` 统一构造出来的。
- 若你关心「误差传播」的逐层细节，回头补 **u3-l5（SequentialPipeline 逐层校准深析）**，那里讲清了「两遍前向 + `IntermediatesCache`」如何让量化误差跨子图累积——这是 basic 故意省略、而 GPTQ 必须的能力。
- 到 **u6-l2（大模型内存策略）** 时你会再次遇到 `sequential_offload_device` 与 onloading；届时可结合本讲对管线结构的理解，看清内存旋钮作用在哪一层。
