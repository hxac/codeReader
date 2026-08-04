# 安装与第一次量化实践

## 1. 本讲目标

上一讲（u1-l1）我们建立了对 llm-compressor 的全局认识：它是一个面向 vLLM 部署的大模型压缩库，输入「HuggingFace 模型 + recipe（配方）」，输出 `compressed-tensors` 格式的可部署 checkpoint。本讲我们要**亲手把它跑起来**。

学完本讲，你应该能够：

1. 完成 `llmcompressor` 的安装，并确认 Python、PyTorch、Transformers 等关键依赖到位。
2. 跑通 README Quick Tour 与 `examples/quantization_w8a8_fp8` 里的 FP8 量化最小示例，把一个小模型量化并保存到磁盘。
3. 理解 `oneshot(model=..., recipe=...)` 这一最小调用形态背后发生了什么。
4. 知道如何用 vLLM（或离线检查）验证量化产物的正确性。

本讲覆盖两个最小模块：`llmcompressor`（这个包本身、如何安装与导入）和 `llmcompressor.entrypoints`（`oneshot` 入口函数）。

---

## 2. 前置知识

承接 u1-l1，你已经知道三个核心概念：**modifier**（单个压缩动作）、**recipe**（modifier 的有序集合）、**oneshot**（一次校准完成压缩的主入口）。本讲会把它们用到真实代码里。再补充几个本讲要用到的基础术语：

- **PTQ（Post-Training Quantization，训练后量化）**：模型训练好之后、不重新训练，直接把权重/激活从高精度（如 bf16）转成低精度（如 fp8/int4）。llm-compressor 主要面向 PTQ。
- **RTN（Round-to-Nearest，就近取整）**：最朴素的量化算法——直接把浮点权重按 scale 缩放后四舍五入到目标精度。它的好处是**不需要校准数据**就能完成权重量化。
- **FP8 动态量化**：权重静态量化（一次性算好 scale 固定下来），激活在**推理时**按每个 token 动态计算 scale。由于激活是动态算的，量化阶段同样不需要校准数据。
- **compressed-tensors 格式**：llm-compressor 保存量化模型的统一格式，vLLM 原生支持。保存后模型的 `config.json` 里会出现一个 `quantization_config` 字段描述量化方案。
- **HuggingFace 加载流程**：用 `AutoModelForCausalLM.from_pretrained(...)` 把模型权重加载成 PyTorch 的 `nn.Module`，用 `AutoTokenizer.from_pretrained(...)` 加载分词器。`oneshot` 接收的就是这个加载好的模型对象。

> 一句话直觉：本讲我们要做的，就是「加载一个模型 → 用一行 `QuantizationModifier` 声明量化方案 → 调用 `oneshot` 让它执行 → 保存」。RTN + FP8 动态量化是最省事的组合，因为它**跳过了校准数据准备**这一步。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目门面，给出 `pip install` 命令与 Quick Tour 端到端示例 |
| `docs/getting-started/install.md` | 详细安装文档：系统要求、Python 版本、多种安装方式 |
| `examples/quantization_w8a8_fp8/README.md` | W8A8 FP8 量化示例的代码走读（Load → Quantize → Evaluate） |
| `examples/quantization_w8a8_fp8/llama3_example.py` | 上面示例对应的可运行脚本，本讲最小示例的模板 |
| `src/llmcompressor/__init__.py` | 包入口，暴露 `oneshot`、`Oneshot`、`model_free_ptq` 等 API |
| `src/llmcompressor/entrypoints/oneshot.py` | `oneshot()` 函数本体与 `Oneshot` 类的完整实现 |
| `setup.py` | 声明依赖（torch/transformers/compressed-tensors 等）与 `python_requires` |

---

## 4. 核心概念与源码讲解

### 4.1 安装与环境确认

#### 4.1.1 概念说明

`llmcompressor` 是一个标准的 Python 包，发布在 PyPI 上，包名就是 `llmcompressor`（注意：仓库名是 `llm-compressor` 带连字符，但 `pip install` 的包名没有连字符）。安装它会把一整套依赖一起拉下来，其中最关键的是：

- **PyTorch（torch）**：所有张量运算的底座。
- **Transformers**：加载 HuggingFace 模型与分词器。
- **compressed-tensors**：定义量化的数据结构与保存格式（llm-compressor 与 vLLM 共用的「共同语言」）。
- **accelerate / datasets**：分布式/设备搬运与校准数据集加载。

#### 4.1.2 核心流程

1. 确认操作系统与 Python 版本满足要求（Linux 推荐，Python ≥ 3.10）。
2. `pip install llmcompressor` 拉取稳定版。
3. 在 Python 里 `import llmcompressor` 并打印版本号，确认安装成功、`oneshot` 可用。

#### 4.1.3 源码精读

安装文档明确列出前置条件——Linux、Python 3.10 或更新：

[docs/getting-started/install.md:L5-L16](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/getting-started/install.md#L5-L16) —— 这里说明推荐 Linux（为了 GPU 支持）以及 Python 与 pip 的版本要求。

最简单的安装方式就是一行命令：

[docs/getting-started/install.md:L19-L25](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/getting-started/install.md#L19-L25) —— 从 PyPI 安装最新稳定版 `pip install llmcompressor`。

README 的安装小节也是同样的命令：

[README.md:L99-L103](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/README.md#L99-L103) —— 项目首页给出的安装命令。

这些依赖具体是哪些版本、Python 要求是多少，全都在 `setup.py` 里写死。我们重点看几行：

[setup.py:L112-L148](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/setup.py#L112-L148) —— `install_requires` 列表，可以看到核心依赖：`torch>=2.10.0`、`transformers>=5.9.0`、`datasets>=4.8.4`、`compressed-tensors>=0.17.2a2`、`accelerate>=1.6.0`。

[setup.py:L186](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/setup.py#L186) —— `python_requires=">=3.10"`，Python 必须 3.10 及以上。

装好之后，`import llmcompressor` 时会发生什么？看包入口：

[src/llmcompressor/\_\_init\_\_.py:L23-L29](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/__init__.py#L23-L29) —— 这几行从子模块里把 `oneshot`、`Oneshot`、`model_free_ptq` 以及一组全局会话函数（`active_session`、`create_session` 等）导出到顶层命名空间。所以你才能直接 `from llmcompressor import oneshot`。

#### 4.1.4 代码实践

**实践目标**：完成安装并确认 `oneshot` 可导入、依赖版本符合预期。

**操作步骤**：

```bash
# 1) 确认 Python 版本 ≥ 3.10
python --version

# 2) 安装（在虚拟环境里执行更稳妥）
pip install llmcompressor
```

```python
# 3) 在 Python 里验证
import llmcompressor
from llmcompressor import oneshot, Oneshot, model_free_ptq
import torch, transformers
import compressed_tensors

print("llmcompressor:", llmcompressor.__version__)
print("torch:", torch.__version__)
print("transformers:", transformers.__version__)
print("oneshot 可用:", callable(oneshot))
```

**需要观察的现象**：三个库的版本号打印出来，且 `oneshot 可用: True`。

**预期结果**：`torch` ≥ 2.10.0、`transformers` ≥ 5.9.0（与 `setup.py` 的下限一致），`oneshot` 可调用。如果你的 `transformers` 版本偏低，`pip` 会自动升级到满足约束的版本。

> 待本地验证：受你的网络与 GPU 驱动影响，`torch` 的 CUDA 版本是否匹配需要自行确认；如果只做权重量化（产出 checkpoint），不一定需要可用 GPU（见 4.2.1 的说明）。

#### 4.1.5 小练习与答案

**练习 1**：如何安装最新的开发版（main 分支）？

**参考答案**：用 git 安装：`pip install git+https://github.com/vllm-project/llm-compressor.git`（见 [install.md:L39-L47](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/getting-started/install.md#L39-L47)）。

**练习 2**：想改源码并立刻生效，应该用哪种安装方式？

**参考答案**：本地克隆后用可编辑模式安装 `pip install -e .[dev]`（见 [install.md:L57-L63](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/getting-started/install.md#L57-L63)），`[dev]` 额外装上 pytest/ruff/mypy 等开发工具。

---

### 4.2 FP8 量化的最小示例

#### 4.2.1 概念说明

我们要跑的示例是 **W8A8 FP8 动态量化**：权重（W）量化到 8 位 fp8、激活（A）也是 8 位 fp8，且激活在推理时动态计算 scale。它用 `QuantizationModifier` 配合 `scheme="FP8_DYNAMIC"` 表达：

- **targets="Linear"**：对所有 `Linear`（线性层）做量化。
- **scheme="FP8_DYNAMIC"**：权重静态 per-channel 量化，激活动态 per-token 量化。
- **ignore=["lm_head"]**：跳过输出投影层 `lm_head`（它对精度敏感，通常不量化）。

为什么这个组合最适合作为「第一次量化」？因为它走的是 **RTN**，**不需要任何校准数据集**——权重直接就近取整到 fp8，激活留到推理时动态算。所以我们的最小示例里**不传 `dataset` 参数**。

关于显存的直觉：把一个权重从 bf16（每个元素 2 字节）量化到 fp8（每个元素 1 字节），体积大约减半：

\[ \text{体积}_{\text{fp8}} \approx \frac{1}{2}\,\text{体积}_{\text{bf16}} \]

实际节省会略小于 50%，因为 `lm_head`、embedding、LayerNorm 等不量化，还会为每层保存少量 scale 元数据。

> 关于硬件：[examples/quantization_w8a8_fp8/README.md](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/examples/quantization_w8a8_fp8/README.md) 指出 fp8 **计算**需要 NVIDIA 计算能力 > 8.9（Ada/Hopper 及以后）的 GPU。注意区分：**产出量化 checkpoint**（本讲要做的事）在普通 CPU/GPU 上即可完成；只有**用 vLLM 跑 fp8 推理**才需要 Ada/Hopper 级别的 GPU。

#### 4.2.2 核心流程

最小示例分四步（伪代码）：

```
1. 加载模型与分词器       model  = AutoModelForCausalLM.from_pretrained(MODEL_ID)
                          tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
2. 用一个 modifier 声明方案 recipe = QuantizationModifier(targets="Linear",
                                                      scheme="FP8_DYNAMIC",
                                                      ignore=["lm_head"])
3. 执行一次性量化          oneshot(model=model, recipe=recipe)
4. 保存为 compressed-tensors model.save_pretrained(SAVE_DIR)
                          tokenizer.save_pretrained(SAVE_DIR)
```

注意：第 2 步里 `recipe` 其实就是一个 `QuantizationModifier` **实例**——`oneshot` 的 `recipe` 参数既接受 YAML 文件路径、也接受 modifier 实例或 Recipe 对象。本讲用「直接传 modifier 实例」这种最直白的形式（recipe 的完整语法见 u2-l5）。

#### 4.2.3 源码精读

官方 `w8a8_fp8` 示例的代码走读把这步讲得很清楚：

[examples/quantization_w8a8_fp8/README.md:L47-L72](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/examples/quantization_w8a8_fp8/README.md#L47-L72) —— 这里说明：simple PTQ 不需要数据（权重 RTN、激活动态），并给出 `QuantizationModifier(targets="Linear", scheme="FP8_DYNAMIC", ignore=["lm_head"])` + `oneshot(model=model, recipe=recipe)` + `save_pretrained` 的完整三段。

对应的可运行脚本就是这几行：

[examples/quantization_w8a8_fp8/llama3_example.py:L17-L22](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/examples/quantization_w8a8_fp8/llama3_example.py#L17-L22) —— 构造 `recipe = QuantizationModifier(targets="Linear", scheme="FP8_DYNAMIC", ignore=["lm_head"])`，然后 `oneshot(model=model, recipe=recipe)`。

`QuantizationModifier` 的三个关键字段的文档定义在它的基类里：

[src/llmcompressor/modifiers/quantization/quantization/base.py:L34-L45](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/base.py#L34-L45) —— 文档说明 `targets`（要量化的层名，默认 `Linear`）、`ignore`（即使命中 target 也要跳过的模块）、`scheme`（单个量化方案，可用预设名如 `FP8_DYNAMIC`）。`scheme` 的完整预设清单与 `config_groups` 的高级用法留到 u3-l1 详解。

README 首页的 Quick Tour 用的是同一种思路，只是把方案换成了 `FP8_BLOCK`（权重按 block 量化），并演示了量化后直接 `model.generate` 采样验证：

[README.md:L152-L200](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/README.md#L152-L200) —— Quick Tour 端到端示例：加载 `Qwen3-30B-A3B`、构造 `QuantizationModifier(targets="Linear", scheme="FP8_BLOCK", ignore=[...])`、调用 `oneshot(model=model, recipe=recipe)`、`dispatch_model` 后采样、`save_pretrained`。

#### 4.2.4 代码实践

**实践目标**：把一个**极小模型**量化为 FP8 并保存到磁盘，对比压缩前后体积。

> 为什么用极小模型？官方示例用的是 8B/30B 级别的大模型，第一次跑既慢又吃资源。我们换成 tiny 级别的模型，让任何人都能在普通机器上跑通，重点是**走通流程**。

**操作步骤**（新建 `first_fp8.py`，示例代码）：

```python
# 示例代码：第一次 FP8 量化（极小模型）
from transformers import AutoModelForCausalLM, AutoTokenizer
from llmcompressor import oneshot
from llmcompressor.modifiers.quantization import QuantizationModifier

# 选一个极小的模型，便于快速跑通流程
MODEL_ID = "sshleifer/tiny-gpt2"   # 待本地验证：需能从 HF 下载

model = AutoModelForCausalLM.from_pretrained(MODEL_ID)
tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)

# RTN + FP8 动态量化，不需要校准数据
recipe = QuantizationModifier(
    targets="Linear",
    scheme="FP8_DYNAMIC",
    ignore=["lm_head"],
)

# 执行一次性量化（注意：没有传 dataset 参数）
oneshot(model=model, recipe=recipe)

# 保存为 compressed-tensors 格式
SAVE_DIR = "tiny-gpt2-FP8-Dynamic"
model.save_pretrained(SAVE_DIR)
tokenizer.save_pretrained(SAVE_DIR)
print("saved to", SAVE_DIR)
```

```bash
# 量化前后体积对比
python first_fp8.py

# 查看保存目录的总体积
du -sh tiny-gpt2-FP8-Dynamic
```

**需要观察的现象**：

1. 运行过程中日志会打印 `oneshot` 的各阶段信息（加载数据、初始化会话、运行校准管线等），即使没有校准数据也会有一条「不需要校准数据 / datafree」相关的流程。
2. 保存目录里出现 `config.json`、`*.safetensors`、`tokenizer*` 等文件。
3. 打开 `tiny-gpt2-FP8-Dynamic/config.json`，应能看到一个 `quantization_config` 字段，其中 `quant_method` 为 `compressed-tensors`，并描述了 `FP8_DYNAMIC` 方案与 `Linear` 目标。

**预期结果**：模型成功量化保存。由于 tiny-gpt2 本身体积极小、且 `lm_head`/embedding 未量化，体积下降幅度可能不到 50%，但 `config.json` 里一定带有 `quantization_config`，证明量化已生效。

> 待本地验证：`sshleifer/tiny-gpt2` 是否能在你的网络环境下载、以及 tiny 模型的 `lm_head` 是否被 `ignore` 命中（不同架构层名不同），需要实际运行确认。若下载失败，可换成任意你能访问的小模型（如 `facebook/opt-125m`，待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么不传 `dataset` 参数也能完成这次量化？

**参考答案**：因为 `scheme="FP8_DYNAMIC"` 走的是 RTN——权重直接就近取整、激活在推理时动态量化，两者都不需要校准数据来统计 scale。

**练习 2**：把 `ignore=["lm_head"]` 去掉会发生什么？

**参考答案**：`lm_head`（输出投影层）也会被量化，通常会让生成质量明显下降，所以一般保留在 `ignore` 里。这也是 README Quick Tour 里 `ignore` 还会加上 `re:.*mlp.gate$`（MoE 路由门）这类对精度敏感的层的原因。

---

### 4.3 oneshot(model=..., recipe=...) 的最小调用形态

#### 4.3.1 概念说明

`oneshot` 是 llm-compressor 最常用的入口函数。它的最小调用形态只要两个必填参数：

- **model**：模型 ID 字符串或已加载的 `PreTrainedModel`。
- **recipe**：压缩配方（YAML 路径 / modifier 实例 / Recipe 对象）。

其余参数都有默认值。本节我们只关心这套「最小形态」背后 `oneshot` 实际做了什么，为后续 u1-l4（三阶段生命周期）打基础。

#### 4.3.2 核心流程

`oneshot(...)` 函数本身很薄，它把所有参数打包交给 `Oneshot` 类，然后调用它。`Oneshot` 的生命周期分三步：

```
oneshot(model, recipe, ...)
   └─► Oneshot(**args).__init__()      # 阶段1 预处理
   │       parse_args(**kwargs)  →  拆成 model_args/dataset_args/recipe_args/output_dir
   │       pre_process(...)      →  初始化模型与 tokenizer/processor、打补丁
   │
   └─► Oneshot.__call__()               # 阶2 校准/压缩
   │       get_calibration_dataloader(...)
   │       apply_recipe_modifiers(...)  →  经全局 CompressionSession 执行 modifiers
   │       post_process(...)            # 阶段3 后处理（若给了 output_dir 则保存）
   │
   └─► return model                      # 返回压缩后的模型
```

两个保存选择（都合法）：

- **显式保存**（本讲示例与官方示例用的方式）：不传 `output_dir`，自己调 `model.save_pretrained(SAVE_DIR)`。
- **自动保存**：传 `output_dir="./out"`，`post_process` 会自动把模型和 tokenizer 存进去。

#### 4.3.3 源码精读

先看 `oneshot` 函数签名（节选关键参数）：

[src/llmcompressor/entrypoints/oneshot.py:L306-L365](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L306-L365) —— `def oneshot(...)`，可以看到 `model` 与 `recipe` 之外，还有 `dataset`（校准数据）、`num_calibration_samples`、`pipeline`（默认 `"independent"`）、`output_dir` 等参数。本讲的最小示例只用了 `model` 与 `recipe`，其余走默认值。

函数体的实现极其简洁——把参数打包、构造 `Oneshot`、调用它、返回模型：

[src/llmcompressor/entrypoints/oneshot.py:L464-L471](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L464-L471) —— `one_shot = Oneshot(**local_args, **kwargs)`、`one_shot()`、`return one_shot.model`。真正的活儿都在 `Oneshot` 类里。

预处理阶段：`__init__` 用 `parse_args` 把 kwargs 拆成四组，再调用 `pre_process` 初始化模型：

[src/llmcompressor/entrypoints/oneshot.py:L170-L183](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L170-L183) —— `parse_args(**kwargs)` 拆出 `model_args/dataset_args/recipe_args/output_dir`，`pre_process(...)` 初始化模型与处理器，并把模型/recipe 挂到实例属性上。

校准/压缩阶段：`__call__` 构造校准 dataloader、应用 recipe 里的 modifiers、再做后处理：

[src/llmcompressor/entrypoints/oneshot.py:L187-L209](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L187-L209) —— `__call__` 里依次：`get_calibration_dataloader(...)` → `apply_recipe_modifiers(...)` → `post_process(...)`。这就是「三阶段生命周期」的雏形（u1-l4 会逐行精读）。

自动保存逻辑在 `post_process` 里——只有传了 `output_dir` 才保存：

[src/llmcompressor/entrypoints/utils.py:L115-L127](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/utils.py#L115-L127) —— `if output_dir is not None: model_args.model.save_pretrained(output_dir, save_compressed=...)`。这解释了为什么「不传 `output_dir` 时必须自己 `save_pretrained`」。

`Oneshot`、`oneshot`、`post_process`、`pre_process` 都从 entrypoints 子包导出：

[src/llmcompressor/entrypoints/\_\_init\_\_.py:L10-L12](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/__init__.py#L10-L12) —— `from .oneshot import Oneshot, oneshot`、`from .utils import post_process, pre_process`。

#### 4.3.4 代码实践

**实践目标**：体会「显式保存 vs 自动保存」两种最小形态的差异。

**操作步骤**：在 4.2.4 脚本基础上，分别试两种写法。

写法 A（显式保存，省略 `output_dir`）：

```python
oneshot(model=model, recipe=recipe)          # 不会自动保存
model.save_pretrained("out-A")               # 必须自己保存
```

写法 B（自动保存，传入 `output_dir`）：

```python
oneshot(model=model, recipe=recipe, output_dir="out-B")  # 自动保存到 out-B
# 无需再调 save_pretrained
```

**需要观察的现象**：写法 A 若注释掉 `save_pretrained`，磁盘上不会出现任何模型目录；写法 B 运行结束后 `out-B` 目录里直接有 `config.json` 与 `*.safetensors`。

**预期结果**：两种写法产出的 `config.json` 里 `quantization_config` 一致（都是 `compressed-tensors` / `FP8_DYNAMIC`）。区别只在「谁负责落盘」。

> 待本地验证：写法 B 是否同时保存了 tokenizer，取决于 model_args 里是否带 processor/tokenizer，建议实际运行确认（本讲示例里 tokenizer 用的是显式保存）。

#### 4.3.5 小练习与答案

**练习 1**：`oneshot` 的 `recipe` 参数可以接受哪几种形式的输入？

**参考答案**：接受 YAML 文件路径（或路径列表）、`Modifier` 实例（或列表）、`Recipe` 对象（或列表），见 [oneshot.py:L390-L392](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L390-L392) 的参数文档。本讲用的是「modifier 实例」这一最直白的形式。

**练习 2**：为什么不传 `output_dir` 时模型没有被保存？

**参考答案**：因为保存逻辑在 `post_process` 里，而它只有当 `output_dir is not None` 时才执行 `save_pretrained`（见 [utils.py:L115-L127](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/utils.py#L115-L127)）。

---

## 5. 综合实践

把本讲的知识串起来：完成一次「量化 → 体积对比 → 产物校验 → 推理验证」的完整小任务。

**任务**：用极小模型跑 RTN FP8 量化，并验证产物是一个合规的 compressed-tensors checkpoint。

1. 按 4.2.4 跑通量化脚本，得到保存目录（如 `tiny-gpt2-FP8-Dynamic`）。
2. **体积对比**：用 `du -sh` 对比「原始模型缓存目录」与「量化保存目录」的大小，记录下降比例，并与理论值（约 50%，因部分层未量化而偏低）对照。
3. **产物校验**：打开 `config.json`，找到 `quantization_config` 字段，逐项确认：
   - `quant_method` 是否为 `compressed-tensors`；
   - `config_groups` 里是否包含你的 `Linear` 目标与 `FP8_DYNAMIC`（或 `num_bits: 8, type: float`）方案；
   - `ignore` 列表里是否包含 `lm_head`。
4. **推理验证（可选）**：如果环境允许（有 Ada/Hopper GPU 且装了 vLLM），用 vLLM 加载量化目录并生成一句话：

   ```bash
   pip install vllm
   ```
   ```python
   from vllm import LLM
   llm = LLM("./tiny-gpt2-FP8-Dynamic")     # 待本地验证：需 Ada/Hopper GPU
   print(llm.generate("Hello"))
   ```

   若没有合适的 GPU，可跳过 vLLM，改用第 3 步的 `config.json` 检查作为产物正确性的证据。

**验收标准**：能说出量化前后体积变化、能解释 `config.json` 中 `quantization_config` 每个关键字段的含义、能说明为什么本次没有用到校准数据。

---

## 6. 本讲小结

- `llmcompressor` 通过 `pip install llmcompressor` 安装，要求 Python ≥ 3.10，核心依赖是 torch、transformers、compressed-tensors、accelerate、datasets（见 `setup.py`）。
- 第一次量化推荐用 **RTN + FP8 动态量化**（`scheme="FP8_DYNAMIC"`）：它不需要校准数据，最小示例只传 `model` 和 `recipe` 两个参数。
- `recipe` 可以直接传一个 `QuantizationModifier(targets="Linear", scheme="FP8_DYNAMIC", ignore=["lm_head"])` 实例，`oneshot` 会把它应用到模型上。
- `oneshot()` 是一个薄包装，内部由 `Oneshot` 类完成「预处理（`pre_process`）→ 校准压缩（`apply_recipe_modifiers`）→ 后处理（`post_process`）」三阶段，详细生命周期见 u1-l4。
- 保存有两种等价方式：不传 `output_dir` 时自己调 `model.save_pretrained(...)`；传 `output_dir` 时由 `post_process` 自动保存。
- 量化产物是 `compressed-tensors` 格式，`config.json` 里的 `quantization_config` 是判断量化是否生效的关键证据，vLLM 可直接加载该格式推理。

---

## 7. 下一步学习建议

- **u1-l3（目录结构与库入口）**：本讲你只碰到了 `oneshot`。建议先读 `src/llmcompressor/__init__.py` 与 entrypoints，建立全局目录印象，知道每个功能大致在哪个子包。
- **u1-l4（oneshot 入口与三阶段生命周期）**：本讲我们把 `pre_process / apply_recipe_modifiers / post_process` 当作黑盒。下一讲会逐行精读这三阶段，并引入 `CompressionSession` 与 `CalibrationPipeline`。
- **想加深量化理解**：可以提前翻 `docs/steps/choosing-scheme.md`（u1-l1 提到过），对照本讲产物 `config.json` 里的 scheme 字段，理解不同精度方案（W8A8 / W4A16 / NVFP4）的差异。
- **想立刻试更进一步的算法**：等学完 u3（量化与校准管线）后，再回到本讲的小模型，把 `recipe` 换成 GPTQ（需要校准数据），体会「需要校准数据」与「不需要校准数据（RTN）」的区别。
