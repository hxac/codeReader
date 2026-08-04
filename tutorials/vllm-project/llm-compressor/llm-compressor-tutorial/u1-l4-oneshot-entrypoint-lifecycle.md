# oneshot 入口与三阶段生命周期

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `oneshot()` 这个用户最常用的入口函数，与它内部真正干活的 `Oneshot` 类是什么关系；
- 画出一次压缩调用的完整生命周期：**预处理（pre_process）→ 校准（apply_recipe_modifiers）→ 后处理（post_process）** 三个阶段分别在做什么；
- 知道 `oneshot()` 的一长串参数（`model` / `recipe` / `dataset` / `pipeline` / `num_calibration_samples` / `output_dir` 等）分别落在哪一组参数类里；
- 理解 `Oneshot` 是如何借助全局会话（`active_session`）和校准管线（`CalibrationPipeline`）来驱动各个 modifier 完成压缩的。

本讲只聚焦「入口与生命周期」这条主线，modifier 内部如何量化、管线内部如何逐层校准，会在 u3 单元展开。

## 2. 前置知识

本讲承接 u1-l2（你已经能跑通一次最小 `oneshot` 调用）和 u1-l3（你已知道 `oneshot` 真正定义在 `src/llmcompressor/entrypoints/oneshot.py`）。在进入源码前，先建立三个概念直觉。

### 2.1 会话（Session）：压缩过程的「工作台」

llm-compressor 把一次压缩任务放进一个全局的 **CompressionSession** 里。会话里保存了两样东西：

- **state**：当前要压缩的模型、校准数据、硬件信息等所有「状态」；
- **lifecycle**：当前压缩进行到哪一步、有哪些 modifier 处于激活状态。

你可以把它理解成「一次手术的工作台」：state 是病人（模型）和器械（数据），lifecycle 是手术流程单。`oneshot` 内部就是通过 `active_session()` 拿到这个全局工作台来推进流程的。

### 2.2 生命周期（Lifecycle）三阶段

`oneshot` 把工作分成三个阶段：

1. **预处理**：把模型、processor 准备好，给 `save_pretrained` 打补丁；
2. **校准**：把 recipe 里的 modifier 注册进会话，跑前向收集统计、算量化参数；
3. **后处理**：把压缩后的模型按 compressed-tensors 格式保存到磁盘。

「生命周期」就是这三个阶段被有序触发的过程，每个 modifier 会在合适的阶段收到对应钩子（如 `on_initialize`、`on_calibration_end`）。modifier 与钩子的细节属于 u2 单元，本讲只看 `Oneshot` 是怎么按下这些按钮的。

### 2.3 校准管线（Calibration Pipeline）：决定「怎么跑前向」

同样是「校准」，但不同算法需要的前向方式不同：

- **sequential**：逐层校准，省显存，适合需要校准数据的算法（如 GPTQ）；
- **datafree**：不跑前向，适合纯权重量化（如 RTN/FP8_DYNAMIC）；
- **independent**：每个 modifier 自己管理前向（`oneshot` 的默认值）；
- **basic**：所有 modifier 共享一组前向。

`oneshot` 会根据 recipe 里 modifier 的特征**自动推断**用哪条管线，也允许你用 `pipeline=` 参数覆盖。管线的内部实现属于 u3 单元，本讲只看 `Oneshot` 是如何「选择并运行」它的。

> 一句话总结直觉：`oneshot()` 是**薄包装**，真正的工作由 `Oneshot` 类按 **pre_process → 校准 → post_process** 三步完成，校准阶段靠**全局会话**驱动 modifier、靠**校准管线**决定前向方式。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/llmcompressor/entrypoints/oneshot.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py) | 定义用户入口 `oneshot()` 函数和真正干活的 `Oneshot` 类，是本讲的主角 |
| [src/llmcompressor/entrypoints/utils.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/utils.py) | 提供 `pre_process`（预处理）和 `post_process`（后处理）两个阶段的具体实现 |
| [docs/guides/entrypoints/oneshot.md](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/guides/entrypoints/oneshot.md) | 官方使用文档，列出参数表与示例，便于对照源码 |

辅助理解（非本讲主角，但会被调用）：

| 文件 | 作用 |
|------|------|
| [src/llmcompressor/args/utils.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/args/utils.py) | `parse_args`：把 kwargs 拆分成三组参数类 |
| [src/llmcompressor/pipelines/registry.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/registry.py) | `CalibrationPipeline.from_modifiers`：自动选择校准管线 |
| [src/llmcompressor/core/session.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session.py) | `CompressionSession.initialize/finalize`：会话的初始化与收尾 |

## 4. 核心概念与源码讲解

### 4.1 oneshot() 函数：从用户调用到 Oneshot 实例

#### 4.1.1 概念说明

你在 u1-l2 里写的 `oneshot(model=..., recipe=...)` 调用的，就是这个模块级的 `oneshot()` 函数。它本身**几乎不做任何压缩逻辑**，它的职责只有一个：把用户传进来的所有参数原样转发给 `Oneshot` 类，然后调用一次 `Oneshot` 实例（触发 `__call__`），最后把压缩好的模型返回。

这种「函数包一层类」的设计有两个好处：

- 用户可以用最简单的函数式语法 `oneshot(...)`；
- 真正的生命周期逻辑被封装在 `Oneshot` 类里，状态（model、recipe 等）作为实例属性保留，便于在 `__init__` 和 `__call__` 之间传递，也便于测试。

#### 4.1.2 核心流程

```text
oneshot(model=..., recipe=..., dataset=..., **kwargs)
   │
   │  ① 收集所有局部变量（locals）为字典
   ▼
Oneshot(**local_args, **kwargs)      # ② 构造实例 → 触发 __init__（= 预处理阶段）
   │
   │  ③ 调用实例 → 触发 __call__（= 校准 + 后处理阶段）
   ▼
return one_shot.model                # ④ 返回压缩后的模型
```

关键是第 ② 步：`Oneshot.__init__` 一旦执行，预处理就已经完成了；第 ③ 步的 `__call__` 才真正跑校准和保存。

#### 4.1.3 源码精读

`oneshot` 函数定义在 [src/llmcompressor/entrypoints/oneshot.py:306-365](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L306-L365)，它的签名就是一份完整的「参数清单」——分成模型参数、recipe 参数、数据集参数、杂项参数四大段，这正是 u1-l3 提到的入口暴露面。

真正体现「薄包装」的是函数末尾这几行 [src/llmcompressor/entrypoints/oneshot.py:464-471](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L464-L471)：

```python
# pass all args directly into Oneshot
local_args = {
    k: v for k, v in locals().items() if k not in ("local_args", "kwargs")
}
one_shot = Oneshot(**local_args, **kwargs)
one_shot()

return one_shot.model
```

这几行做了三件事：把所有命名参数收集成字典、丢给 `Oneshot` 构造并调用、返回模型。注意它还额外透传了 `**kwargs`——也就是说 `oneshot()` 签名里没有列出的参数，也会原样传给 `Oneshot`，再由 `parse_args` 决定如何安置（见 4.2）。

`Oneshot` 类的整体结构与三阶段生命周期，写在它的 docstring 里 [src/llmcompressor/entrypoints/oneshot.py:68-82](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L68-L82)：预处理、校准、后处理。这就是本讲的主线。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认 `oneshot()` 只是薄包装。

**步骤**：

1. 打开 [src/llmcompressor/entrypoints/oneshot.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py)。
2. 找到 `def oneshot(` 与文件末尾的 `return one_shot.model`。
3. 数一下：函数体内除了参数收集，是否还有任何「真正压缩模型」的代码？

**预期结果**：函数体里没有任何量化/剪枝逻辑，只有构造 `Oneshot`、调用它、返回模型三步。

#### 4.1.5 小练习与答案

**练习 1**：如果用户调用 `oneshot(model=m, recipe=r)` 后想拿到压缩后的模型，应该用谁的属性？
**答案**：用 `oneshot()` 的返回值（即 `Oneshot` 实例的 `.model` 属性），如 `model = oneshot(model=m, recipe=r)`。

**练习 2**：为什么 `oneshot()` 末尾要把 `locals()` 收集成字典再传给 `Oneshot`，而不是逐个手写参数？
**答案**：因为参数数量很多，逐个手写既冗长又易漏；用 `locals()` 自动收集能保证函数签名里新增的参数被自动透传，且额外留了 `**kwargs` 兜底签名外的参数。

---

### 4.2 预处理阶段：Oneshot.__init__ 与 pre_process

#### 4.2.1 概念说明

预处理阶段的目标是：**在真正压缩之前，把模型和 processor 准备到一个「可被压缩」的状态**。这一阶段在 `Oneshot.__init__` 里完成，它做两件事——

1. 用 `parse_args` 把用户传的一堆 kwargs 分类装进三组参数类；
2. 调用 `pre_process` 真正加载模型、初始化 processor、解绑词嵌入、给 `save_pretrained` 打补丁。

三组参数类是理解 `oneshot` 参数体系的钥匙：

| 参数类 | 承载内容 | 典型字段 |
|--------|----------|----------|
| `ModelArguments` | 模型加载与保存 | `model`、`processor`、`precision`、`save_compressed` |
| `DatasetArguments` | 数据集与校准管线 | `dataset`、`num_calibration_samples`、`pipeline`、`sequential_targets` |
| `RecipeArguments` | 压缩配方 | `recipe`、`recipe_args`、`stage` |

#### 4.2.2 核心流程

```text
Oneshot.__init__(**kwargs)
   │
   ├─ 关闭 tokenizer 并行ism、配置日志
   ├─ parse_args(**kwargs) → (model_args, dataset_args, recipe_args, output_dir)
   ├─ 挂到 self.model_args / dataset_args / recipe_args / output_dir
   ├─ pre_process(model_args, dataset_args, output_dir)   ← 预处理主体
   │     ├─ 若 model 是路径 → initialize_model_from_path 真正加载
   │     ├─ 若 processor 是路径/None → initialize_processor_from_path
   │     ├─ 若 tie_word_embeddings=False → untie_word_embeddings
   │     ├─ 若模型用了 accelerate offload → from_accelerate 转换
   │     └─ modify_save_pretrained：给 save_pretrained 打补丁
   ├─ self.model / self.processor / self.recipe 取值
   └─ validate_model(self.model)：检查模型是否已被量化
```

#### 4.2.3 源码精读

`Oneshot.__init__` 的核心在 [src/llmcompressor/entrypoints/oneshot.py:170-185](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L170-L185)：

```python
model_args, dataset_args, recipe_args, output_dir = parse_args(**kwargs)

self.model_args = model_args
self.dataset_args = dataset_args
self.recipe_args = recipe_args
self.output_dir = output_dir

# initialize the model and processor
pre_process(model_args, dataset_args, output_dir)

# Set instance attributes
self.model = self.model_args.model
self.processor = self.model_args.processor
self.recipe = self.recipe_args.recipe

self.validate_model(self.model)
```

注意一个关键点：`pre_process` 是**就地修改** `model_args.model` 的——如果传入的是字符串路径，`pre_process` 会把真正加载好的模型对象写回 `model_args.model`，所以下面 `self.model = self.model_args.model` 取到的已经是加载完成的模型对象。

`parse_args` 的拆分逻辑在 [src/llmcompressor/args/utils.py:44-50](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/args/utils.py#L44-L50)：

```python
output_dir = kwargs.pop("output_dir", None)
parser_args = (ModelArguments, DatasetArguments, RecipeArguments)
parser = HfArgumentParser(parser_args)
parsed_args = parser.parse_dict(kwargs)
model_args, dataset_args, recipe_args = parsed_args
```

它借助 `transformers` 的 `HfArgumentParser`，按字段名把 kwargs 自动归类到三个 dataclass——这就是为什么你传 `num_calibration_samples=...` 会自动落到 `DatasetArguments`、传 `save_compressed=...` 会落到 `ModelArguments`。

`pre_process` 的主体在 [src/llmcompressor/entrypoints/utils.py:41-96](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/utils.py#L41-L96)，其中加载模型的关键是 [src/llmcompressor/entrypoints/utils.py:61-63](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/utils.py#L61-L63)：

```python
if isinstance(model_args.model, (str, PosixPath)):
    model = initialize_model_from_path(model_args)
    model_args.model = model
```

也就是说：`model` 参数既可以是 HuggingFace 模型 ID / 本地路径（字符串），也可以是已经加载好的模型对象。`pre_process` 只在它是字符串时才触发真正的 `AutoModelForCausalLM.from_pretrained`（见 [initialize_model_from_path](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/utils.py#L134-L174)）。最后 [modify_save_pretrained](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/utils.py#L96) 给模型的 `save_pretrained` 打补丁，使其支持 compressed-tensors 序列化——这是后处理阶段能正确保存的前提。

`validate_model` 在 [src/llmcompressor/entrypoints/oneshot.py:272-303](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L272-L303) 做一道安全检查：如果输入模型**已经是量化模型**，且量化格式不是 compressed-tensors，就直接报错；如果是 compressed-tensors 格式则只警告。

#### 4.2.4 代码实践（源码阅读型）

**目标**：搞清某个参数最终落在哪一组参数类。

**步骤**：

1. 打开 `oneshot()` 签名，挑三个参数：`save_compressed`、`num_calibration_samples`、`stage`。
2. 打开 [src/llmcompressor/args/dataset_arguments.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/args/dataset_arguments.py)，确认 `num_calibration_samples` 的默认值与所属类。
3. 判断这三个参数分别属于 `ModelArguments` / `DatasetArguments` / `RecipeArguments` 哪一组。

**预期结果**：`save_compressed` 属于 ModelArguments；`num_calibration_samples`（默认 512）属于 DatasetArguments；`stage` 属于 RecipeArguments。

#### 4.2.5 小练习与答案

**练习 1**：如果用户传给 `oneshot` 的 `model` 是一个已经加载好的模型对象，`pre_process` 还会调用 `initialize_model_from_path` 吗？
**答案**：不会。`pre_process` 只在 `model_args.model` 是 `str` 或 `PosixPath` 时才加载，对象类型会跳过加载步骤。

**练习 2**：为什么预处理阶段要调用 `modify_save_pretrained`？
**答案**：因为压缩后的模型要按 compressed-tensors 格式序列化（写出 `quantization_config`、压缩保存稀疏权重等），原版 `save_pretrained` 不支持这些，所以需要提前打补丁，给后处理阶段的保存铺路。

---

### 4.3 校准阶段：__call__、dataloader 与 apply_recipe_modifiers

#### 4.3.1 概念说明

校准阶段是三阶段里最核心的一步，在 `Oneshot.__call__` 里完成。它做三件事：

1. 用数据集参数构建一个**校准 dataloader**（即便没有校准数据，也会构建一个空的）；
2. 调用 `apply_recipe_modifiers`：把 recipe 里的 modifier 注册进全局会话，**选择并运行校准管线**；
3. 调用 `post_process` 保存（这一步名义上属于后处理，但代码上紧接在校准之后）。

`apply_recipe_modifiers` 是把「recipe / 会话 / 管线」三者串起来的枢纽：它先 `session.initialize`（让每个 modifier 执行各自的初始化钩子），再用 `CalibrationPipeline.from_modifiers` 选管线，最后 `pipeline(...)` 跑前向，结束后 `session.finalize`。

#### 4.3.2 核心流程

```text
Oneshot.__call__()
   │
   ├─ calibration_dataloader = get_calibration_dataloader(dataset_args, processor)
   ├─ apply_recipe_modifiers(calibration_dataloader, recipe_stage)
   │     ├─ session = active_session(); session.reset()
   │     ├─ 若存在未线性化的 MoE → linearize_moe（并警告）
   │     ├─ with ExitStack():
   │     │     ├─ norm_calibration_context(model)        # norm 平滑上下文
   │     │     ├─ [可选] moe_calibration_context()        # MoE 全专家校准
   │     │     ├─ session.initialize(model, recipe, calib_data, ...)
   │     │     ├─ pipeline = CalibrationPipeline.from_modifiers(modifiers, user=...)
   │     │     └─ pipeline(model, dataloader, dataset_args)   # 真正跑前向/校准
   │     └─ session.finalize()
   └─ post_process(model_args, recipe_args, output_dir)   # 保存
```

#### 4.3.3 源码精读

`__call__` 非常短，见 [src/llmcompressor/entrypoints/oneshot.py:187-209](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L187-L209)：

```python
calibration_dataloader = get_calibration_dataloader(
    self.dataset_args, self.processor
)
self.apply_recipe_modifiers(
    calibration_dataloader=calibration_dataloader,
    recipe_stage=self.recipe_args.stage,
)
post_process(
    model_args=self.model_args,
    recipe_args=self.recipe_args,
    output_dir=self.output_dir,
)
```

校准的真正编排都在 `apply_recipe_modifiers` 里 [src/llmcompressor/entrypoints/oneshot.py:211-270](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L211-L270)。先拿全局会话并重置 [src/llmcompressor/entrypoints/oneshot.py:229-230](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L229-L230)：

```python
session = active_session()
session.reset()
```

接着是对 MoE 模型的保护：如果发现还有没线性化的 MoE，就警告并就地线性化 [src/llmcompressor/entrypoints/oneshot.py:232-238](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L232-L238)（MoE 线性化细节在 u5-l2）。

然后用 `ExitStack` 同时挂载两个校准上下文，并在其中完成「初始化 → 选管线 → 跑管线」[src/llmcompressor/entrypoints/oneshot.py:242-268](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L242-L268)：

```python
with ExitStack() as stack:
    stack.enter_context(norm_calibration_context(self.model))
    if self.dataset_args.moe_calibrate_all_experts:
        stack.enter_context(moe_calibration_context())

    session.initialize(
        model=self.model,
        start=-1,
        recipe=self.recipe,
        recipe_stage=recipe_stage,
        recipe_args=self.recipe_args.recipe_args,
        calib_data=calibration_dataloader,
        sequential_targets=self.dataset_args.sequential_targets,
    )

    session.state.enable_compile = self.dataset_args.enable_compile

    user_pipeline = self.dataset_args.pipeline
    pipeline = CalibrationPipeline.from_modifiers(
        session.lifecycle.recipe.modifiers, user=user_pipeline
    )

    pipeline(self.model, calibration_dataloader, self.dataset_args)

session.finalize()
```

三个要点：

1. **`session.initialize`** 让会话编译 recipe、对每个 modifier 调用其初始化钩子，并把模型、校准数据、`sequential_targets` 都存进 state（详见 [session.py 的 initialize](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session.py#L74-L145)）。注意它用的是 `start=-1`（表示从最早已开始训练步骤算起）。
2. **选管线**用的是 `session.lifecycle.recipe.modifiers`——也就是 `initialize` 之后、由 recipe 编译出来的实际 modifier 列表（不是用户原始 recipe）。
3. **`pipeline(...)`** 才是真正跑前向、收集统计、算量化参数的地方；`session.finalize()` 在最后让每个 modifier 执行收尾钩子。

管线如何自动选择？看 [CalibrationPipeline.from_modifiers](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/registry.py#L27-L53) 与推断函数 [_infer_pipeline](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/registry.py#L55-L60)：

```python
@staticmethod
def _infer_pipeline(modifiers: list[Modifier]) -> str:
    if any(modifier.requires_calibration_data for modifier in modifiers):
        return "sequential"
    else:
        return "datafree"
```

规则很简单：**只要有一个 modifier 声明需要校准数据，就推断为 `sequential`；否则用 `datafree`**。用户通过 `pipeline=` 传的值可以覆盖推断结果，但如果和推断不一致会打印一条建议告警。注意 `oneshot` 的默认 `pipeline="independent"`——这是一种「强制让每个 modifier 独立管理前向」的特殊值，`from_modifiers` 会对它做单独处理（详见 u3-l4/l6）。

#### 4.3.4 代码实践（源码阅读型）

**目标**：标注校准阶段的关键代码行，理解「初始化→选管线→跑管线→收尾」的顺序。

**步骤**：

1. 在 [src/llmcompressor/entrypoints/oneshot.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py) 的 `apply_recipe_modifiers` 中标出四行：`session.initialize(`（约 247 行）、`CalibrationPipeline.from_modifiers(`（约 260 行）、`pipeline(self.model, ...)`（约 264 行）、`session.finalize()`（约 270 行）。
2. 思考：为什么 `session.initialize` 要放在 `pipeline(...)` 之前？
3. 思考：为什么 `CalibrationPipeline.from_modifiers` 用的是 `session.lifecycle.recipe.modifiers` 而不是 `self.recipe`？

**预期结果**：因为 `initialize` 会编译 recipe（把 YAML/字符串解析成真实的 modifier 对象列表），只有初始化后 `session.lifecycle.recipe.modifiers` 才是可执行的 modifier；`pipeline(...)` 必须在这些 modifier 已注册、已初始化的状态下才能正确驱动它们。

#### 4.3.5 小练习与答案

**练习 1**：一个只含 `QuantizationModifier(scheme="FP8_DYNAMIC")`（无需校准数据）的 recipe，`_infer_pipeline` 会推断成什么？
**答案**：`datafree`，因为没有任何 modifier 的 `requires_calibration_data` 为 True。但 `oneshot` 默认传 `pipeline="independent"`，所以实际跑的是 independent 管线（见 u3-l6）。

**练习 2**：`apply_recipe_modifiers` 开头为什么要先 `session.reset()`？
**答案**：全局会话是「一次性的」，每次 `oneshot` 调用前都要把上一次残留的 lifecycle/状态清掉，避免上一次的 modifier 或状态污染本次压缩。

**练习 3**：`ExitStack` 在这里的作用是什么？
**答案**：用它一次性挂载多个校准上下文（`norm_calibration_context`、可选的 `moe_calibration_context`），保证无论中间是否抛异常，这些上下文都能在退出 `with` 块时被正确清理。

---

### 4.4 后处理阶段：post_process 与保存

#### 4.4.1 概念说明

后处理阶段的任务很纯粹：**把压缩好的模型、processor（或 tokenizer）按 compressed-tensors 格式写到 `output_dir`**。它由 `post_process` 实现，在 `__call__` 的最后被调用。如果用户没传 `output_dir`，则什么都不保存——这时你需要自己用返回的 `model.save_pretrained(...)` 手动保存（u1-l2 已演示过这种用法）。

#### 4.4.2 核心流程

```text
post_process(model_args, recipe_args, output_dir)
   │
   ├─ if model_args is not None and output_dir is not None:
   │     ├─ 若指定了 stage → output_dir 追加 stage 子目录
   │     ├─ model.save_pretrained(output_dir, save_compressed=...)
   │     └─ 若有 processor → processor.save_pretrained(output_dir)
   └─ 若 clear_sparse_session → reset_session()   # 清理会话
```

#### 4.4.3 源码精读

`post_process` 在 [src/llmcompressor/entrypoints/utils.py:99-131](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/utils.py#L99-L131)，核心保存逻辑如下：

```python
if model_args is not None and output_dir is not None:
    if recipe_args is not None and getattr(recipe_args, "stage", None) is not None:
        output_dir = os.path.join(output_dir, recipe_args.stage)
        os.makedirs(output_dir, exist_ok=True)

    model_args.model.save_pretrained(
        output_dir, save_compressed=model_args.save_compressed
    )

    if model_args.processor is not None:
        model_args.processor.save_pretrained(output_dir)

if recipe_args is not None and recipe_args.clear_sparse_session:
    reset_session()
```

两个要点：

1. **`save_compressed`** 来自 `ModelArguments`（默认 `True`），它决定稀疏权重是否压缩保存——这正是预处理阶段 `modify_save_pretrained` 打补丁后新增的能力。
2. 保存出的 `config.json` 里会包含 `quantization_config` 字段，这就是模型「已被量化、可被 vLLM 加载」的证据（u1-l2 已验证过）。

`save_pretrained` 之所以能写出 compressed-tensors 格式，是因为预处理阶段已经调用 `modify_save_pretrained` 替换/增强了它。所以**预处理与后处理是一对配合**：预处理铺路，后处理落地。

#### 4.4.4 代码实践（运行型，待本地验证）

**目标**：观察 `output_dir` 是否提供，对保存行为的影响。

**步骤**：

```python
from transformers import AutoModelForCausalLM
from llmcompressor import oneshot
from llmcompressor.modifiers.quantization import QuantizationModifier

model = AutoModelForCausalLM.from_pretrained("HuggingFaceTB/SmolLM2-135M")
recipe = QuantizationModifier(targets="Linear", scheme="FP8_DYNAMIC", ignore=["lm_head"])

# 方式 A：传 output_dir，由 post_process 自动保存
oneshot(model=model, recipe=recipe, output_dir="./smol-fp8-A")

# 方式 B：不传 output_dir，自己保存
compressed = oneshot(model=model, recipe=recipe)
compressed.save_pretrained("./smol-fp8-B", save_compressed=True)
```

**需要观察的现象**：方式 A 运行结束后 `./smol-fp8-A` 目录下是否生成 `config.json`、`*.safetensors`；打开 `config.json` 是否能找到 `quantization_config` 字段。

**预期结果**：两种方式产物等价，都生成带 `quantization_config` 的 checkpoint。具体文件大小与字段内容**待本地验证**（取决于本地环境与模型版本）。

#### 4.4.5 小练习与答案

**练习 1**：如果不传 `output_dir`，`post_process` 会保存模型吗？
**答案**：不会。`post_process` 只在 `output_dir is not None` 时才保存；此时需用 `oneshot()` 的返回值手动调用 `model.save_pretrained(...)`。

**练习 2**：`save_compressed=True` 主要影响什么？
**答案**：它影响稀疏（已剪枝）权重的保存——压缩存储可以显著减小稀疏模型的磁盘体积。对纯量化（非稀疏）模型影响较小。

---

## 5. 综合实践

本任务把三阶段串起来，既读源码又跑脚本，体会「不同 dataset / num_calibration_samples 如何改变校准阶段的日志」。

### 实践目标

- 在源码里精确标出 `pre_process` / `apply_recipe_modifiers` / `post_process` 三阶段的入口行号；
- 通过脚本对比不同校准样本数下的日志差异，直观感受「校准阶段」确实被触发。

### 操作步骤

**第 1 步：标注三阶段（源码阅读）**

在 [src/llmcompressor/entrypoints/oneshot.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py) 中找到并记录：

| 阶段 | 所在方法 | 关键调用行 |
|------|----------|-----------|
| 预处理 | `Oneshot.__init__` | `pre_process(...)`（约 178 行） |
| 校准 | `Oneshot.__call__` → `apply_recipe_modifiers` | `session.initialize(...)`（约 247 行）、`pipeline(...)`（约 264 行） |
| 后处理 | `Oneshot.__call__` | `post_process(...)`（约 205 行） |

**第 2 步：对比脚本（运行型，待本地验证）**

```python
from transformers import AutoModelForCausalLM
from llmcompressor import oneshot
from llmcompressor.modifiers.quantization import QuantizationModifier

recipe = QuantizationModifier(targets="Linear", scheme="W8A8", ignore=["lm_head"])

# 场景 1：少量校准样本
model1 = AutoModelForCausalLM.from_pretrained("HuggingFaceTB/SmolLM2-135M")
oneshot(
    model=model1,
    dataset="HuggingFaceH4/ultrachat_200k",
    splits="train_sft[:1%]",
    recipe=recipe,
    num_calibration_samples=8,
    max_seq_length=512,
    output_dir="./smol-w8a8-8",
)

# 场景 2：更多校准样本
model2 = AutoModelForCausalLM.from_pretrained("HuggingFaceTB/SmolLM2-135M")
oneshot(
    model=model2,
    dataset="HuggingFaceH4/ultrachat_200k",
    splits="train_sft[:1%]",
    recipe=recipe,
    num_calibration_samples=64,
    max_seq_length=512,
    output_dir="./smol-w8a8-64",
)
```

### 需要观察的现象

- 日志中关于数据集加载、样本数（`8` vs `64`）、校准前向批次数量的差异；
- 两个 `output_dir` 下 `config.json` 的 `quantization_config` 是否都生成；
- 校准样本数变化对运行耗时的影响。

### 预期结果

- 两次都会完整经历 pre_process → 校准 → post_process 三阶段；
- `num_calibration_samples` 越大，校准阶段前向次数越多、耗时越长，但 `quantization_config` 都会正确写入。
- 具体日志行与耗时数值**待本地验证**（取决于硬件与网络）。

> 提示：如果你没有 GPU 或下载大模型受限，可把模型换成更小的本地模型、把 `num_calibration_samples` 调到个位数，重点是观察日志中「校准阶段被触发」这一现象，而非追求量化精度。

## 6. 本讲小结

- `oneshot()` 是**薄包装**：收集参数 → 构造 `Oneshot` → 调用它 → 返回模型，本身不含压缩逻辑。
- `Oneshot` 把工作组织成**三阶段生命周期**：预处理（`__init__` + `pre_process`）、校准（`__call__` + `apply_recipe_modifiers`）、后处理（`post_process`）。
- `parse_args` 借助 `HfArgumentParser`，按字段名把 kwargs 自动归类到 `ModelArguments` / `DatasetArguments` / `RecipeArguments` 三组。
- `pre_process` 负责「加载模型 + 初始化 processor + 解绑词嵌入 + 给 `save_pretrained` 打补丁」，是后处理能正确保存的前提。
- 校准阶段是核心：`session.initialize` 编译 recipe 并初始化各 modifier，`CalibrationPipeline.from_modifiers` 按需选择管线，`pipeline(...)` 跑前向，`session.finalize` 收尾。
- 管线自动推断规则很简明：有 modifier 需要校准数据→`sequential`，否则→`datafree`；`oneshot` 默认 `pipeline="independent"`。

## 7. 下一步学习建议

- 想搞清 `active_session` / `create_session` / `session.initialize` 到底如何管理状态与生命周期，进入 **u2-l1（CompressionSession 与 State）** 和 **u2-l2（CompressionLifecycle 与事件系统）**。
- 想理解 recipe 是怎么从 YAML/Modifier 编译成 modifier 列表的，进入 **u2-l5（Recipe 编码压缩指令）**。
- 想深入校准管线内部（sequential 如何逐层切图、四种管线差异），进入 **u3-l4（CalibrationPipeline 注册与选择）** 与 **u3-l5（SequentialPipeline 逐层校准）**。
- 建议顺手通读 [docs/guides/entrypoints/oneshot.md](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/guides/entrypoints/oneshot.md) 的参数表，把本讲的参数类归属与官方文档相互印证。
