# 无模型定义量化 model_free_ptq

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `model_free_ptq` 与 `oneshot` 的**根本区别**：为什么它不需要 HuggingFace 模型定义、不需要把整个模型实例化成 `torch.nn.Module`。
- 描述 `model_free_ptq` 对 safetensors 权重**逐文件、逐张量**做权重量化（RTN）的六个阶段流水线。
- 精读 `process.py` 中「初始化 → 校准 → 压缩 → 保存」的**单张量四步法**，并理解 `lifecycle.py` 如何复用与 `oneshot` 完全相同的校准函数。
- 理解 microscale 方案（NVFP4/MXFP4）为何需要**跨分片融合**，以及 `reindex_fused_weights` 为什么被废弃。
- 用一个小模型 checkpoint 跑通一次 FP8 权重量化，并对比它与 `oneshot` 产物在 `config` 与权重上的异同。

## 2. 前置知识

在进入本讲前，请确认你已经理解以下概念（均来自前置讲义 u3-l1 与第三单元）：

- **PTQ（Post-Training Quantization，训练后量化）**：模型训练完成后，直接对权重做量化，不再反传梯度。
- **RTN（Round-To-Nearest，就近取整）**：最朴素的权重量化方式。它只需要权重本身，**不需要任何校准数据**。给定一个权重张量 \(W\)，量化把它映射成低位整数，核心是算出缩放因子 scale 与零点 zero-point：

  \[
  \text{scale} = \frac{\max(W) - \min(W)}{q_{\max} - q_{\min}}, \quad
  q(W) = \mathrm{round}\!\left(\frac{W}{\text{scale}}\right) + \text{zero\_point}
  \]

  误差全部来自 `round`。**合理选 scale 是各种算法的核心权衡**（见 u1-l1、u3-l1）。
- **scheme（量化方案）**：决定量化位数与格式，如 `FP8_DYNAMIC`、`FP8_BLOCK`、`NVFP4A16`。命名中 `W` 指权重、`A` 指激活。`model_free_ptq` 只做**仅权重量化（weight-only）**。
- **compressed-tensors 格式**：量化结果写进 `config.json` 里的 `quantization_config` 字段，vLLM 原生可加载。
- **oneshot 的三阶段生命周期**（u1-l4、u2-l1）：`pre_process → apply_recipe_modifiers → post_process`，背后是 `CompressionSession` + `CompressionLifecycle` + `CalibrationPipeline` 一整套引擎。

本讲要讲的是一个**绕开整套引擎**的「快车道」入口。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `src/llmcompressor/entrypoints/model_free/` 子包下：

| 文件 | 作用 |
|------|------|
| [`__init__.py`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/__init__.py#L41-L110) | 定义 `model_free_ptq` 主入口，编排六个阶段。 |
| [`process.py`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/process.py#L74-L125) | **核心**：`process_file` 逐张量执行「初始化 → 校准 → 压缩 → 保存」四步；另有 microscale 版本与 MoE 拆分函数。 |
| [`lifecycle.py`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/lifecycle.py#L38-L56) | **核心**：`initialize_quantized_linear` / `calibrate_weight` / `validate_weight_for_quantization`，把单个张量包成一个可量化的 Linear。 |
| [`validate.py`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/validate.py#L19-L55) | `validate_scheme`（只允许 data-free 方案）、`validate_safetensors_index`。 |
| [`microscale.py`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/microscale.py#L27-L66) | NVFP4/MXFP4 的融合分组定义、`is_microscale_scheme`、跨分片 inverse weight map 构建。 |
| [`save_utils.py`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/save_utils.py#L24-L67) | `update_config`：把量化方案写回 `config.json` 的 `quantization_config`。 |
| [`reindex_fused_weights.py`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/reindex_fused_weights.py#L21-L45) | 已**废弃**的预处理脚本，现在不再需要。 |

> 顶层 `llmcompressor/__init__.py:29` 把 `model_free_ptq` 与 `oneshot`、`Oneshot` 一起提升为顶层 API，所以你可以直接 `from llmcompressor import model_free_ptq`。

## 4. 核心概念与源码讲解

### 4.1 model_free_ptq 是什么：为什么不需要模型定义

#### 4.1.1 概念说明

`oneshot` 的前提是「能拿到一个 HuggingFace 模型定义」：它要 `transformers.AutoModelForCausalLM.from_pretrained(model)` 把整个模型实例化成一个有完整层结构的 `torch.nn.Module`，再让校准管线（SequentialPipeline 等）按层切子图、跑前向、触发 modifier 钩子。这套机制强大，但有两个前提会经常不满足：

1. **模型没有 transformers 定义**——例如刚发布、`transformers` 还没收录的新架构，或社区自训练的模型。
2. **模型太大，根本装不进内存**——实例化完整的 `torch.nn.Module` 树本身就要吃下全部权重。

`model_free_ptq` 解决的就是这两个场景。它的关键思想是：**既然只做仅权重的 RTN 量化，而 RTN 只需要一个权重张量就能算出 scale/zero-point，那就不必实例化整个模型，直接对磁盘上的 safetensors 张量逐个处理即可。**

具体而言，它完全不接触 `CompressionSession` / `CompressionLifecycle` / `Recipe` / `Modifier` / `CalibrationPipeline` 这套引擎，而是直接调用底层 `compressed_tensors` 库的量化函数。代价是：**它只能做无需校准数据的方案（data-free weight-only quantization）**。

| 维度 | `oneshot` | `model_free_ptq` |
|------|-----------|------------------|
| 需要 HF 模型定义 | 是 | 否 |
| 实例化完整 `torch.nn.Module` 模型 | 是 | 否（只对每个张量临时建一个 Linear） |
| 支持 GPTQ/AWQ/SmoothQuant 等需数据算法 | 是 | 否 |
| 引擎（session/lifecycle/pipeline） | 全套 | 无 |
| 适用场景 | 任意压缩 | 仅权重的 data-free 量化、超大模型、无定义模型 |

文档明确列出了三类使用时机：方案是 data-free（FP8 动态/块、NVFP4A16、MXFP4/MXFP8）、模型没有 transformers 定义、或 `oneshot` 失败时。

参见 [docs/guides/entrypoints/model-free-ptq.md:3-13](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/guides/entrypoints/model-free-ptq.md#L3-L13) 的「When to Use」与「How It Works」说明。

#### 4.1.2 核心流程

入口函数签名非常薄：

```python
def model_free_ptq(
    model_stub, save_directory, scheme,
    ignore=tuple(), max_workers=1, device=None, converter=None,
): ...
```

七个参数，没有一个叫 `recipe` 或 `dataset`——这是它和 `oneshot` 最直观的区别：**没有压缩配方、没有校准数据**，只有一个 `scheme`（量化方案）。

入口做的第一件事是 `validate_scheme`，它强制约束了「必须 data-free」。核心约束有三条：

1. **必须有权重量化方案**（`scheme.weights is not None`）——这是仅权重量化。
2. **激活量化必须是动态的（dynamic）**——因为没有任何校准数据去算静态激活的 scale。一旦方案要求静态激活，直接报错并提示「请改用 `oneshot`」。
3. **weight observer 自动改写为 static 版本**——因为权重是静态值，需要在一次观测里就把统计算完（参见 u3-l3 中 static 与 memoryless observer 的区别）。

#### 4.1.3 源码精读

入口签名与文档：[`entrypoints/model_free/__init__.py:41-70`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/__init__.py#L41-L70)。注意参数表里没有 `recipe` / `dataset`，只有 `scheme` 与 `ignore`。

scheme 校验的三条约束在 [`validate.py:19-55`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/validate.py#L19-L55)。其中关键的两段（这是判断方案是否合法的「门神」）：

```python
# weight quantization must be provided
if scheme.weights is None:
    raise ValueError("Must provide a weights quantization scheme ...")

# activation quantization must be dynamic
input_dynamic = getattr_chain(scheme, "input_activations.dynamic", True)
output_dynamic = getattr_chain(scheme, "output_activations.dynamic", True)
if input_dynamic is not True or output_dynamic is not True:
    raise ValueError(
        "Model Free PTQ cannot calibrate activations. Please use `oneshot` instead."
    )
```

这段代码就是「data-only weight-only」边界的硬编码：你只要尝试传一个需要静态激活的方案，会在量化开始前就被拦下。

#### 4.1.4 代码实践

**实践目标**：体会 `model_free_ptq` 的参数边界。

**操作步骤**：

1. 打开 [`__init__.py:41-49`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/__init__.py#L41-L49) 的签名，与 `oneshot` 的签名（见 u1-l4）逐字对比。
2. 在 Python 里构造一个需要静态激活的 `QuantizationScheme`（例如手动设置 `input_activations.dynamic=False`），调用 `validate_scheme`，观察报错信息。

```python
# 示例代码（仅演示调用，非项目原有代码）
from compressed_tensors.quantization import preset_name_to_scheme
from llmcompressor.entrypoints.model_free.validate import validate_scheme

# FP8_BLOCK 是合法的 data-free 方案
name, scheme = validate_scheme("FP8_BLOCK")
print(name, "OK")
```

**预期结果**：合法方案正常返回 `(scheme_name, scheme)`；非法方案抛出 `ValueError` 并提示改用 `oneshot`。若你无法本地构造非法 scheme，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `model_free_ptq` 不能做 GPTQ？

> **参考答案**：GPTQ 需要用校准激活去累积 Hessian 矩阵 \(H=2X^\top X\)（见 u4-l1），而 `model_free_ptq` 不加载模型、不跑前向、没有校准数据，根本拿不到激活 \(X\)。它只能做仅依赖权重本身的 RTN。`validate_scheme` 也通过强制「激活必须 dynamic」把这类方案挡在门外。

**练习 2**：`model_free_ptq` 的参数表里为什么没有 `recipe`？

> **参考答案**：它不使用 Recipe/Modifier 引擎，压缩动作是固定的一种（对该方案做 RTN 取整），不需要一个有序的 modifier 列表，所以只需要一个 `scheme` 字符串就够了。

---

### 4.2 总体流水线：六个阶段

#### 4.2.1 概念说明

`model_free_ptq` 的主体是一个**面向文件的批处理调度器**：它把一个 checkpoint 目录看成「一堆 safetensors 分片文件 + 一些配置文件」，然后对每个分片文件派发一个独立的「量化作业」。作业之间互不依赖，可以多线程并行（`max_workers`），也可以多 GPU 轮询（`device` 列表）。

这和 `oneshot` 那种「把模型整体加载、按层切子图、逐子图两遍前向」的 SequentialPipeline 截然不同——后者是**模型级**的编排，前者是**文件级**的编排。

#### 4.2.2 核心流程

主入口串起六个阶段：

```
1. 校验参数      get_checkpoint_files + validate_scheme + validate_safetensors_index
2. 复制非权重文件  把 config.json / tokenizer 等（非 .safetensors）原样拷到输出目录
3. 构建作业       _build_jobs：为每个分片预计算 inverse_weight_map，并按 GPU 轮询分配
4. 预校验作业      validate_file：在 meta device 上 fail-fast 检查每个可量化张量
5. 执行量化作业    process_file：逐张量 init→calibrate→compress→save
6. 更新元信息      update_config（写 quantization_config）+ update_safetensors_index（重建索引）
```

其中阶段 4 的「预校验」是一个重要的工程细节：**在跑漫长量化之前，先在 meta device（不占真实显存的虚拟设备）上把每个张量试一遍**，如果某个张量形状不对（不是 2D）会立刻失败，避免量化跑到一半才崩。`validate_file` 里显式写着 `device = torch.device("meta")`（[process.py:64](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/process.py#L64)）。

作业分配采用 **round-robin（轮询）**：`device = devices[i % len(devices)]`，第 i 个分片落到第 `i mod N` 张卡上，多卡时各卡分摊分片。

#### 4.2.3 源码精读

主入口的六个阶段：[`__init__.py:71-110`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/__init__.py#L71-L110)。其中复制非权重文件与构建作业两步（这两步定义了「文件级」调度的边界）：

```python
# 阶段2：复制非 safetensors 文件（configs, tokenizers, etc.）
for file_path, resolved_path in model_files.items():
    if not file_path.endswith("safetensors"):
        ...  # 原样 copyfile
# 阶段3：构建量化作业
jobs = _build_jobs(model_files, save_directory, scheme, ignore, resolved_devices, converter)
# 阶段4：fail-fast 预校验
validate_jobs = [(validate_file, *job[1:]) for job in jobs]
exec_jobs(validate_jobs, max_workers, desc="Validating")
# 阶段5：真正量化
quantize_results = exec_jobs(jobs, max_workers, desc="Quantizing")
```

作业构建与轮询分配：[`_build_jobs: __init__.py:137-201`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/__init__.py#L137-L201)。它根据是否是 microscale 方案选择不同的处理函数与不同的 inverse weight map 构建器：

```python
if is_microscale_scheme(scheme):
    job_fn = process_file_microscale_scheme
    build_inverse_weight_maps_fn = build_microscale_inverse_weight_maps
else:
    job_fn = process_file
    build_inverse_weight_maps_fn = build_inverse_weight_maps   # 来自 compressed_tensors
```

设备解析（无 GPU 时退回 CPU）：[`_resolve_devices: __init__.py:113-134`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/__init__.py#L113-L134)。

#### 4.2.4 代码实践

**实践目标**：理解「文件级」批处理调度。

**操作步骤**：

1. 在 [`__init__.py:89-110`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/__init__.py#L89-L110) 中，用注释把六段代码分别标上「阶段1~6」。
2. 找到 `device = devices[i % len(devices)]`（在 `_build_jobs` 内），解释当 `devices` 有 2 张卡、共 5 个分片时，每个分片分别落到哪张卡。

**预期结果**：分片 0/2/4 落到 cuda:0，分片 1/3 落到 cuda:1（round-robin）。

#### 4.2.5 小练习与答案

**练习**：阶段 4 的预校验为什么要在 `meta` device 上做，而不是真实 GPU？

> **参考答案**：校验只需要检查张量的**形状与可量化性**（是否 2D、能否被 `flatten_for_calibration`），不需要真实的数值计算。meta device 是 PyTorch 提供的「虚拟设备」，张量在上面只有形状、不占显存，因此可以用几乎零成本提前发现错误（fail-fast），避免量化跑了几十分钟才因为某个张量形状问题崩溃。

---

### 4.3 单张量 RTN 四步法（process.py + lifecycle.py）

> 这是本讲的两个核心最小模块，也是 `model_free_ptq` 真正「做量化」的地方。

#### 4.3.1 概念说明

不管分片有多少个、调度多复杂，最终落到每一个权重张量上的动作是固定的四步，定义在 `process_file` 里。它对分片内每一个「可量化张量」（由 `match_quantizable_tensors` 按名字与 `ignore`/`targets` 过滤得到）执行：

1. **初始化**：把这个 2D 权重张量临时包成一个最小化的 `torch.nn.Linear`，并挂上量化方案。
2. **校准**：从权重本身算出 scale/zero-point。
3. **压缩**：调用 `compressed_tensors` 的 `compress_module`，把权重按算好的 qparams 打包成低位整数。
4. **保存**：把压缩后的张量写回字典，搬回 CPU。

关键洞察是第 2 步的 `calibrate_weight`：它调用的 `initialize_observer / observe / update_qparams / freeze_module_quantization` **正是 `oneshot` 中 `QuantizationMixin` 子类在 `on_sequential_epoch_end` 里调用的同一组函数**（见 u3-l2、u3-l3）。也就是说，`model_free_ptq` 没有重复造轮子，而是把引擎里「取整三连」那一层直接抽出来复用——区别只在于：`oneshot` 通过生命周期钩子在子图末尾触发它，`model_free_ptq` 在一个朴素的 `for` 循环里对每个张量直接调用它。

这就是为什么两者的 RTN 量化结果（在相同 scheme 下）本质等价：**底层用的是同一套取整函数**。

#### 4.3.2 核心流程

```
for 每个可量化张量 name in 分片:
    1. initialize_quantized_linear(weight, scheme, device)
         → 新建 Linear(in, out, bias=False)，copy 权重，挂 scheme
    2. calibrate_weight(module)
         → initialize_observer(weight) → apply_calibration_status
         → observe(module) → update_qparams(module) → freeze
         此时 scale/zero_point 已写入模块
    3. compress_module(module)
         → 用 qparams 把权重打包成低位表示（如 FP8/INT4）
    4. del tensors[name]; 把 module.state_dict() 的每个 key 搬回 CPU 写回字典
save_file(tensors, save_path)  # 写出整个分片
```

RTN 的数学就发生在第 2 步的 `observe` + `update_qparams`：observer 收集权重的 min/max（对静态 weight observer 而言，一次观测即可），然后由 `compressed_tensors.calculate_qparams` 反解出 scale 与 zero-point（公式见第 2 节）。第 3 步 `compress_module` 再用这组参数把浮点权重真正量化成低位整数。

#### 4.3.3 源码精读

四步法的主循环：[`process.py:104-120`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/process.py#L104-L120)。这是本讲最该精读的代码段：

```python
for module_name, name in match_quantizable_tensors(tensors, ignore, scheme.targets):
    validate_weight_for_quantization(tensors[name], scheme, name)

    # 1. initialize module with qparams (on device)
    module = initialize_quantized_linear(tensors[name], scheme, device)
    # 2. calibrate weight qparams
    calibrate_weight(module)
    # 3. compress module using qparams
    compress_module(module)
    # 4. save compressed data (on cpu)
    del tensors[name]
    prefix = module_name + "."
    for key, value in module.state_dict(prefix=prefix).items():
        tensors[key] = value.to("cpu")
```

三个辅助函数在 `lifecycle.py`：

- `validate_weight_for_quantization`：[`lifecycle.py:23-35`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/lifecycle.py#L23-L35) —— 校验权重是 2D 且能被压平校准。
- `initialize_quantized_linear`：[`lifecycle.py:38-48`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/lifecycle.py#L38-L48) —— 新建无 bias 的 Linear、copy 权重、调用 `compressed_tensors.initialize_module_for_quantization` 挂方案：

  ```python
  module = torch.nn.Linear(in_features, out_features, bias=False, device=device, dtype=weight.dtype)
  module.weight.data.copy_(weight)
  initialize_module_for_quantization(module, scheme, force_zero_point=False)
  ```

- `calibrate_weight`：[`lifecycle.py:51-56`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/lifecycle.py#L51-L56) —— 这一组函数全部 import 自 `llmcompressor.modifiers.quantization.calibration`（即 u3-l3 精读的那个文件），是与 `oneshot` 共享的取整三连：

  ```python
  def calibrate_weight(module):
      initialize_observer(module, "weight")
      apply_calibration_status(module)
      observe(module, base_name="weight")
      update_qparams(module, base_name="weight")
      freeze_module_quantization(module)
  ```

  对比 u3-l3 中 `update_qparams` 必须紧跟 `observe`、且 observe 后统计被删除的结论，这里把五步严格按顺序串起来，正是一个模块级 RTN 的完整流程。

`match_quantizable_tensors` 来自 `compressed_tensors`，它按 `ignore`（含自动忽略名字以 `norm` 结尾的模块）与 `scheme.targets`（默认 `Linear`，见 [validate.py:53-54](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/validate.py#L53-L54)）过滤出要量化的张量。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：用一个已有的小模型 checkpoint 跑 `model_free_ptq` 做 FP8 权重量化，并对比它与 `oneshot` 产物在 `config` 与权重上的异同。

**操作步骤**：

1. 准备一个**极小的本地 safetensors 模型目录**（例如用项目自带的 tiny 模型生成工具，或任意一个只有几层的小模型，如 `HuggingFaceTB/smollm2-135M` 这类小尺寸模型）。记其本地目录为 `tiny_model/`。
2. 调用 `model_free_ptq` 做 FP8 块量化：

   ```python
   from llmcompressor import model_free_ptq

   model_free_ptq(
       model_stub="tiny_model",                # 本地 checkpoint 目录
       save_directory="tiny_model_fp8_mfptq",  # 输出目录
       scheme="FP8_BLOCK",
       ignore=["lm_head"],
       device="cuda:0",                        # 无 GPU 改成 "cpu"
   )
   ```
3. 再用 `oneshot` 对同一个原始模型做**相同 scheme** 的量化，存到 `tiny_model_fp8_oneshot/`（参见 u1-l2 的最小示例，用 `QuantizationModifier(targets="Linear", scheme="FP8_BLOCK", ignore=["lm_head"])`）。
4. 对比两个输出目录：
   - 打开各自的 `config.json`，找到 `quantization_config` 字段，对比 `config_groups`、`quant_method`、`format` 是否一致。
   - 用 `safetensors.safe_open` 各读一个 Linear 层（如 `model.layers.0.self_attn.q_proj.weight`），看是否都从 `float16/bfloat16` 变成了 `float8_e4m3fn`（FP8 dtype），并比较 scale 张量 `...weight_scale` 的数值是否接近。

**需要观察的现象**：

- 两者的 `quantization_config` 在 scheme 层面应当**高度相似**（因为底层用同一套 `compressed_tensors` 取整函数）。
- 权重 dtype 都应变成 FP8，体积大约减半。
- 由于实现路径不同（一个逐张量、一个走生命周期），scale 数值可能存在**微小差异**（数值精度/运算顺序），但量级一致。

**预期结果**：两个 checkpoint 都能被 vLLM（或 vLLM 加载逻辑）按 compressed-tensors 格式加载，行为基本等价。若本地无 GPU 或无小模型，把步骤 2、3 标注「待本地验证」，并改为**源码阅读型实践**：在 [`process.py:104-120`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/process.py#L104-L120) 标注四步，对照 u3-l2 中 `QuantizationMixin.on_sequential_epoch_end` 的取整调用，确认二者调用的是同名函数。

#### 4.3.5 小练习与答案

**练习 1**：`model_free_ptq` 的 `calibrate_weight` 和 `oneshot` 里 RTN 的取整，为什么用的是同一组函数？

> **参考答案**：因为两者做的都是「仅依赖权重」的 RTN：算 scale/zero-point 只需要权重张量的 min/max。这组函数（`observe`+`update_qparams`+`freeze`）定义在 `llmcompressor.modifiers.quantization.calibration` 中，`lifecycle.py` 直接 import 它们。`oneshot` 通过生命周期钩子触发，`model_free_ptq` 通过 `for` 循环直接调用，殊途同归。

**练习 2**：为什么 `initialize_quantized_linear` 要把权重包成一个 `Linear(bias=False)`，而不是直接量化原始张量？

> **参考答案**：因为取整三连与 `compressed_tensors.compress_module` 都是面向 `torch.nn.Module` 的——它们要从模块的 `quantization_scheme` 属性读方案、向模块的 `state_dict` 写入 `weight_scale`/`weight_quantized` 等键。包成 Linear 是为了复用这套「以模块为单位」的接口，否则就要重新实现一遍 qparams 的读写逻辑。

---

### 4.4 microscale 方案、跨分片融合与 reindex 的废弃

#### 4.4.1 概念说明

前面三节讲的都是「普通」方案（如 `FP8_BLOCK`），每个权重张量独立量化即可。但有一类**microscale 方案**（NVFP4、MXFP4）特殊：它们除了每个小块的局部 scale，还需要一个**跨多个相关权重共享的 global scale**。这些相关权重通常是**融合的**：

- 注意力的 q/k/v 投影（`q_proj` / `k_proj` / `v_proj`）共享一个 global scale；
- MLP 的 gate/up 投影（`gate_proj` / `up_proj`）共享一个 global scale。

难点在于：在大模型里，这些融合的伙伴张量常常**被切到不同的 safetensors 分片文件**里（比如 `q_proj` 在 shard0、`k_proj` 在 shard1）。要算 global scale 就必须把它们凑齐。

判断一个方案是否是 microscale，依据是它的 weight 策略为 `TENSOR_GROUP`（见 [`is_microscale_scheme: microscale.py:64-66`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/microscale.py#L64-L66)）。

**reindex 的废弃**：在旧版本里，处理 microscale 需要先用一个叫 `reindex_fused_weights` 的预处理脚本，把分散在各分片的融合张量重新排到同一个文件里。但最近的提交已经把这一步废弃了——`model_free_ptq` 现在能直接处理跨分片融合，不再需要 reindex。

#### 4.4.2 核心流程

新机制的核心是**预计算的 inverse_weight_map**：在量化开始前（`_build_jobs` 阶段），为每个输出分片预先算好「需要从哪些源文件、读取哪些张量」的清单。对于拥有「主」张量（如 `q_proj`、`gate_proj`）的分片，清单里会额外包含它的「伙伴」张量（`k_proj`/`v_proj`、`up_proj`），即使伙伴在别的分片。这样：

1. 每个融合集合**只被读取一次**——由拥有主张量的分片负责拉取所有伙伴，避免重复读。
2. 伙伴张量被读进来后，会和主张量一起**重新保存到主分片的输出**里，索引随后由调用方更新。
3. 量化时，对一组融合模块用 `FusionHandler.fuse` 合并 observer、算出共享的 global scale（与 u3-l3 中 TENSOR_GROUP 的 FusionHandler 是同一套机制）。

主/伙伴的配对规则在 `DEFAULT_FUSED_MAPPINGS` 里以正则模板定义（主模式 → 伙伴模板列表）。

#### 4.4.3 源码精读

融合配对规则：[`DEFAULT_FUSED_MAPPINGS: microscale.py:27-46`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/microscale.py#L27-L46)。例如 q/k/v 与 gate/up 配对：

```python
# Attention q/k/v fusion: q_proj is primary
r"^(?P<prefix>.+?)\.(?P<attn>attn|...)\.q_proj\.weight$": [
    r"{prefix}.{attn}.k_proj.weight",
    r"{prefix}.{attn}.v_proj.weight",
],
# MLP gate/up fusion: gate_proj is primary
r"^(?P<prefix>.+?)\.(?P<mlp>mlp|feed_forward)\.gate_proj\.weight$": [
    r"{prefix}.{mlp}.up_proj.weight",
],
```

跨分片 inverse_weight_map 构建：[`build_microscale_inverse_weight_maps: microscale.py:82-125`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/microscale.py#L82-L125)。内部定义了一个 `MicroscaleConverter`，它的 `get_dependencies` 用上面的模板算出每个权重依赖哪些伙伴，再交给 `compressed_tensors.build_inverse_weight_maps` 生成每个输出分片的精确读取清单。

microscale 的处理函数：[`process_file_microscale_scheme: process.py:128-235`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/process.py#L128-L235)。它的关键差异在于：普通权重照常四步处理，而融合权重先收集起来，最后统一用共享 global scale 压缩（共享 scale 的核心三步）：

```python
# Compress fused modules with shared global scale
for named_modules in fused_modules.values():
    FusionHandler.fuse([(mod.weight_observer, mod) for mod in named_modules.values()])
    observe(named_modules.values(), base_name="weight")
    update_qparams(named_modules.values(), base_name="weight")
    for name, module in named_modules.items():
        freeze_module_quantization(module)
        compress_module(module)
        ...
```

`reindex_fused_weights` 的废弃声明：[`reindex_fused_weights.py:21-45`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/reindex_fused_weights.py#L21-L45)，函数被 `@deprecated` 装饰、函数体只发一条「不再需要」的警告。

#### 4.4.4 代码实践

**实践目标**：理解 microscale 的跨分片融合与 reindex 的废弃。

**操作步骤**：

1. 读 [`microscale.py:27-46`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/microscale.py#L27-L46)，画出 `DEFAULT_FUSED_MAPPINGS` 中四组「主 → 伙伴」的配对表。
2. 读 [`process.py:181-228`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/process.py#L181-L228)，用一句话解释：为什么融合权重要单独收集、最后统一压缩，而不是像普通权重那样在循环里直接压？

**预期结果**：因为融合权重共享一个 global scale，必须等一组伙伴（q+k+v 或 gate+up）凑齐后一起 `FusionHandler.fuse` + `observe`，才能算出这个共享 scale；普通权重没有伙伴，可以独立压。

#### 4.4.5 小练习与答案

**练习 1**：怎么从代码判断一个 scheme 是否需要走 microscale 流程？

> **参考答案**：看 `is_microscale_scheme(scheme)`——它检查 `scheme.weights.strategy == QuantizationStrategy.TENSOR_GROUP`。`_build_jobs` 据此在 `process_file`（普通）与 `process_file_microscale_scheme`（microscale）之间二选一。

**练习 2**：现在的版本还需要在 `model_free_ptq` 之前跑 `reindex_fused_weights` 吗？为什么？

> **参考答案**：不需要。`reindex_fused_weights` 已被标记为 `@deprecated` 且函数体只发警告、什么都不做。因为 `_build_jobs` 现在会通过 `build_microscale_inverse_weight_maps` 预计算每个分片需要从哪些源文件读哪些张量（含跨分片的伙伴），由主分片负责拉取伙伴，无需事先把融合张量重排到同一文件。

---

### 4.5（补充）config 更新：让产物可被 vLLM 加载

量化完所有分片、写回权重后，最后一步 `update_config`（[`save_utils.py:24-67`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/save_utils.py#L24-L67)）把量化元信息写进 `config.json` 的 `quantization_config`，再由 `update_safetensors_index`（来自 `compressed_tensors`）重建分片索引。`create_or_update_quant_config`（[`save_utils.py:70-129`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/save_utils.py#L70-L129)）处理三种情况：从 converter 新建、追加到已有的 compressed-tensors 配置、或从零创建——最终都把状态置为 `COMPRESSED`。这一步是产物能被 vLLM 当作 compressed-tensors 模型加载的「凭证」，与 `oneshot` 的 `post_process` 保存阶段等价。

## 5. 综合实践

把本讲知识串起来，完成一个「对比两条路径」的任务：

**任务**：选定一个能在本地加载的小模型（如 `HuggingFaceTB/SmolLM2-135M` 或自造 tiny 模型），分别用 `oneshot`（recipe 含 `QuantizationModifier(scheme="FP8_BLOCK")`）和 `model_free_ptq(scheme="FP8_BLOCK")` 做量化，保存到两个目录。

**要求**：

1. 在源码层面先定位：`oneshot` 路径里 RTN 取整发生在 `QuantizationModifier.on_sequential_epoch_end`（u3-l1），而 `model_free_ptq` 路径发生在 [`process.py:104-120`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/process.py#L104-L120) 的 `calibrate_weight`。确认两者最终都调用 `llmcompressor.modifiers.quantization.calibration` 里的同名函数。
2. 对比两个产物目录的 `config.json` 中 `quantization_config`，列出相同字段与不同字段。
3. 用 `safetensors.safe_open` 各读一个 Linear 的 `weight` 与 `weight_scale`，比较 dtype 与 scale 量级。
4. 写一段总结：什么场景你会优先选 `model_free_ptq`（提示：无 transformers 定义 / 超大模型 / 只需仅权重量化）。

**预期结果**：两条路径的产物在 compressed-tensors 格式上等价、权重 dtype 一致；config 主要差异可能在 `config_groups` 的命名（`model_free_ptq` 用 scheme 名或 `config_group_0`，见 [validate.py:21-24](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/validate.py#L21-L24)）。若无环境，则改为纯源码阅读型实践，并标注「待本地验证」。

## 6. 本讲小结

- `model_free_ptq` 是**绕开整套引擎**的快车道入口：不实例化完整模型、不用 session/lifecycle/recipe/pipeline，直接对 safetensors 权重逐文件、逐张量做仅权重的 RTN 量化。
- 它的边界由 `validate_scheme` 硬编码：**必须 data-free**（激活必须 dynamic），需要校准数据的算法（GPTQ/AWQ/SmoothQuant）一律不支持，会被拦下并提示改用 `oneshot`。
- 主体是**文件级批处理**：复制配置 → 构建作业（含 round-robin 多卡分配）→ meta device 预校验 → 并行量化 → 更新 config 与索引，共六阶段。
- 真正的量化在 `process_file` 的**单张量四步法**（初始化→校准→压缩→保存），`lifecycle.py` 把单个 2D 权重包成 Linear 后，复用 `oneshot` 同款取整三连函数，因此 RTN 结果与 `oneshot` 本质等价。
- microscale 方案（NVFP4/MXFP4）需要跨融合伙伴（q/k/v、gate/up）共享 global scale，靠预计算的 inverse_weight_map 处理跨分片伙伴，**旧的 `reindex_fused_weights` 预处理已废弃**。
- 最终 `update_config` 写出 `quantization_config`，使产物可被 vLLM 按 compressed-tensors 格式加载。

## 7. 下一步学习建议

- 若你想理解 `model_free_ptq` 复用的取整三连函数（`observe`/`update_qparams`/`freeze`）的内部细节，回到 **u3-l3（校准、Observers 与 Hooks）** 精读 `calibration.py`。
- 若你想理解 microscale 共享 global scale 的 `FusionHandler`，回到 **u3-l3** 的 TENSOR_GROUP 部分。
- 若你想理解写出的 `quantization_config` 各字段含义，参考 **u6-l3（保存与 compressed-tensors 格式）**。
- 若你想看另一条「需要校准数据」的完整压缩路径，对照 **u1-l4（oneshot 三阶段生命周期）** 与 **u3-l5（SequentialPipeline）**，体会两种编排粒度（文件级 vs 模型级）的差异。
