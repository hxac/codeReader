# 项目总览与快速上手

## 1. 本讲目标

本讲是 nano-vllm 学习手册的第一篇，面向「完全没接触过这个项目」的读者。读完本讲，你应该能够：

1. 说清楚 **nano-vllm 是什么**、它和工业级推理框架 vLLM 是什么关系、它解决了什么问题。
2. 理解它的 **四大核心特性**：离线推理（offline inference）、Prefix Caching、张量并行（Tensor Parallelism）、CUDA Graph。
3. 完成 **环境安装** 和 **模型下载**，并跑通 `example.py`，看懂终端输出的进度条与最终生成文本。
4. 掌握两个最基础的概念模块：**`LLM`**（推理引擎入口）和 **`SamplingParams`**（采样参数），并能动手修改它们观察生成行为的变化。

本讲不要求你懂 GPU 内部细节，只要会基本的 Python 和命令行操作即可。

---

## 2. 前置知识

在开始前，先用大白话建立几个概念，后面读源码就不会卡。

### 2.1 什么是「LLM 推理」

训练好的大语言模型（LLM）是一组权重文件。**推理（inference）** 就是给模型输入一段文字（prompt），让它输出一段续写文字。例如：

> 输入：「列出 100 以内的所有质数」
> 输出：「2, 3, 5, 7, 11, ...」

模型一次只「吐」出一个 token（token 可以粗略理解为一个词或字片段），然后把它拼回输入，再预测下一个，循环往复，直到结束。这个「逐个生成」的过程叫 **decode**；而第一次处理整段 prompt 的过程叫 **prefill**。

### 2.2 什么是「推理引擎」

直接用 PyTorch 跑 LLM 推理会很慢，因为有很多可以优化的地方：显存怎么管、多个请求怎么凑批、缓存怎么复用……一个 **推理引擎**（如 vLLM）就是把这些优化做好、对外暴露简洁 API 的工具。nano-vllm 是 vLLM 的一个 **极简复刻**，代码量只有约 1200 行（[README.md:L9-L17](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/README.md#L9-L17) 里写到「Clean implementation in ~ 1,200 lines of Python code」），目的是让人能读懂，而不是追求功能全。

### 2.3 离线推理 vs 在线服务

- **离线推理（offline inference）**：你一次性给一批 prompt，引擎全部算完再返回结果。适合批量处理、评测、实验。nano-vllm 聚焦的就是这一类。
- **在线服务（online serving）**：像一个 HTTP 服务，随时接收新请求、流式返回。vLLM 完整版两者都支持，nano-vllm 只做离线。

### 2.4 关键术语速查

| 术语 | 含义 |
|------|------|
| token | 模型处理的最小文本单元 |
| prefill | 处理整段 prompt 的阶段，一次算很多 token |
| decode | 逐个生成新 token 的阶段，一次算一个（或一批序列各一个） |
| KV Cache | 把注意力机制里的 Key/Value 缓存起来，避免重复计算 |
| 张量并行（TP） | 把同一层权重切到多张 GPU 上一起算 |

---

## 3. 本讲源码地图

本讲只涉及最外层的「用户视角」文件，不深入引擎内部。后续讲义才会逐层拆解。

| 文件 | 作用 | 本讲用到的地方 |
|------|------|----------------|
| [README.md](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/README.md) | 项目说明、安装方式、特性列表、基准测试结果 | 了解定位与安装 |
| [example.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/example.py) | 最小可运行示例，跑通第一次推理 | 动手实践的主入口 |
| [pyproject.toml](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/pyproject.toml) | 包元数据与依赖声明 | 确认依赖与 Python 版本 |
| [nanovllm/__init__.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/__init__.py) | 对外导出 `LLM` 和 `SamplingParams` | 理解 `from nanovllm import ...` |
| [nanovllm/llm.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/llm.py) | `LLM` 类定义（极薄一层） | 理解入口类 |
| [nanovllm/sampling_params.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/sampling_params.py) | `SamplingParams` 数据类 | 调整采样行为 |
| [nanovllm/engine/llm_engine.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py) | `LLMEngine`，真正的引擎实现 | 理解 `LLM` 的构造与 `generate` |

> 提示：本讲的「源码精读」会引用上面这些文件的具体行号，建议你边读边在仓库里点开对照。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先认识项目本身（4.1），再学采样参数（4.2），最后学引擎入口 `LLM`（4.3）。

### 4.1 nano-vllm 是什么：定位、特性与安装

#### 4.1.1 概念说明

nano-vllm 用一句话概括：**一个从零手写、只有约 1200 行的 vLLM 极简复刻，用来说明一个现代 LLM 推理引擎是怎么运转的。**

它和完整版 vLLM 的关系类似「教科书示例」与「工业产品」：

- vLLM：功能全、支持几十种模型、有在线服务、有分布式调度，代码量大。
- nano-vllm：只保留核心机制，代码可读完，足够理解 vLLM 的关键设计。

它对外声明了 **四大特性**（见 [README.md:L13-L17](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/README.md#L13-L17)）：

1. **快速离线推理**：吞吐与 vLLM 接近（README 给出了在 RTX 4070 Laptop 上 nano-vllm 1434 tok/s、vLLM 1361 tok/s 的对比）。
2. **可读的代码库**：约 1200 行 Python。
3. **优化套件**：包含 Prefix Caching（前缀缓存）、Tensor Parallelism（张量并行）、Torch compilation（torch.compile 算子融合）、CUDA Graph（计算图捕获）。

这几个特性的工作原理会在后面的讲义里逐一展开，本讲你只需要知道「有这些东西」即可。

#### 4.1.2 核心流程

从零到跑通推理的流程：

```text
1. 安装 nano-vllm 包（pip install）
2. 下载一个 HuggingFace 模型权重（默认用 Qwen3-0.6B）
3. 写脚本：LLM(model_path) 构造引擎 → generate(prompts, sampling_params) 生成
4. 程序返回 [{"text": ..., "token_ids": ...}, ...]
```

安装方式见 [README.md:L19-L23](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/README.md#L19-L23)：

```bash
pip install git+https://github.com/GeeeekExplorer/nano-vllm.git
```

模型下载见 [README.md:L25-L32](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/README.md#L25-L32)：

```bash
huggingface-cli download --resume-download Qwen/Qwen3-0.6B \
  --local-dir ~/huggingface/Qwen3-0.6B/ \
  --local-dir-use-symlinks False
```

#### 4.1.3 源码精读

依赖与 Python 版本要求在 [pyproject.toml:L13-L20](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/pyproject.toml#L13-L20)，这段代码声明了运行前提：

- 要求 Python `>=3.10,<3.13`；
- 核心依赖包括 `torch>=2.4.0`、`triton>=3.0.0`、`transformers>=4.51.0`、`flash-attn`、`xxhash`。

也就是说，nano-vllm 依赖 **flash-attn**（高效注意力实现）和 **triton**（写 GPU kernel 的语言），这两者是后面优化特性的基石，并且安装时通常需要 GPU 环境。

对外导出的两个名字非常简单，[nanovllm/__init__.py:L1-L2](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/__init__.py#L1-L2) 只是把 `LLM` 和 `SamplingParams` 暴露出来：

```python
from nanovllm.llm import LLM
from nanovllm.sampling_params import SamplingParams
```

这也是为什么 example.py 里可以直接 `from nanovllm import LLM, SamplingParams`。

#### 4.1.4 代码实践

**目标**：在你的机器上跑通第一次推理。

**操作步骤**：

1. 克隆仓库并安装（需要 CUDA 环境；若无 GPU，本步骤可能失败，标注为「待本地验证」）：
   ```bash
   git clone https://github.com/GeeeekExplorer/nano-vllm.git
   cd nano-vllm
   pip install -e .
   ```
2. 下载模型权重（约 1～2 GB）：
   ```bash
   huggingface-cli download --resume-download Qwen/Qwen3-0.6B \
     --local-dir ~/huggingface/Qwen3-0.6B/ \
     --local-dir-use-symlinks False
   ```
3. 运行示例：
   ```bash
   python example.py
   ```

**需要观察的现象**：

- 程序启动后会先有一段权重加载和预热（warmup）时间，期间没有输出。
- 接着终端出现一个 tqdm 进度条，标题是 `Generating`，右侧会带 `Prefill=…tok/s` 和 `Decode=…tok/s` 的实时后缀。
- 两条 prompt（「introduce yourself」「list all prime numbers within 100」）各打印一段 `Prompt:` 与 `Completion:`。

**预期结果**：`Completion:` 后面是 Qwen3-0.6B 生成的回答文本。具体文字内容「待本地验证」（取决于模型版本与采样随机性），但结构一定是先 `Prompt:` 再 `Completion:`。

> 如果安装失败，最常见原因是 `flash-attn` 编译。这是已知的环境门槛，不影响你继续读源码。

#### 4.1.5 小练习与答案

**练习 1**：nano-vllm 的代码量大约是多少行？它的定位是什么？
**答案**：约 1200 行 Python；定位是 vLLM 的极简、可读复刻，用于讲清楚推理引擎的核心机制。

**练习 2**：nano-vllm 的依赖里，哪两个库和「GPU 加速」直接相关？
**答案**：`flash-attn`（高效注意力）和 `triton`（GPU kernel DSL）。

**练习 3**：README 报告的基准测试中，nano-vllm 的吞吐大约是多少？
**答案**：在 RTX 4070 Laptop、Qwen3-0.6B 上约 1434 tokens/s（见 [README.md:L57-L61](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/README.md#L57-L61)）。

---

### 4.2 SamplingParams：采样参数模块

#### 4.2.1 概念说明

模型在每一步会输出一组「下一个 token 的概率分布」。**采样参数**就是决定「如何从这个分布里挑 token」的旋钮。nano-vllm 用一个数据类 `SamplingParams` 来表达，只有三个字段（[nanovllm/sampling_params.py:L4-L11](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/sampling_params.py#L4-L11)）：

| 字段 | 默认值 | 含义 |
|------|--------|------|
| `temperature` | `1.0` | 温度，控制采样的随机性 |
| `max_tokens` | `64` | 最多生成多少个 token |
| `ignore_eos` | `False` | 是否忽略「结束符」，强行生成到 max_tokens |

**温度的直觉**：在采样前，模型的 logits 会被温度 T 缩放，再过 softmax 得到概率。概率公式为：

\[ p_i = \frac{\exp(z_i / T)}{\sum_j \exp(z_j / T)} \]

- T 越小（如 0.1）：分布越「尖锐」，更倾向选最高概率的 token，输出更确定、更保守；
- T 越大（如 1.5）：分布越「平坦」，选择更随机，输出更有创造性、也更容易跑题。

example.py 用的是 `temperature=0.6`（[example.py:L11](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/example.py#L11)），属于「偏确定但保留一点随机」的常用值。

**一个重要的设计选择**：nano-vllm **不允许 greedy 采样**（即温度为 0、永远选最大概率）。代码里有一行断言：

```python
assert self.temperature > 1e-10, "greedy sampling is not permitted"
```

这是因为它的采样器实现走的是「指数分布采样」路线（后续 u5-l2 会讲），这条路本身就需要非零温度。所以你不能传 `temperature=0`，想要接近确定的结果就传一个很小的正数（如 `1e-5`）。

#### 4.2.2 核心流程

`SamplingParams` 的生命周期很简单：

```text
1. SamplingParams(temperature=…, max_tokens=…) 创建对象
2. __post_init__ 校验 temperature > 1e-10，否则抛 AssertionError
3. 对象随请求一起传给 LLM.generate / add_request
4. 引擎在生成循环里读取 max_tokens（何时停）、ignore_eos（是否提前停）
5. 最终由采样器（Sampler）按 temperature 实际挑 token
```

它本身只「携带」配置，不做计算——真正的采样逻辑在 [nanovllm/layers/sampler.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/sampler.py)，本讲先不展开。

#### 4.2.3 源码精读

完整的 `SamplingParams` 定义在 [nanovllm/sampling_params.py:L1-L11](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/sampling_params.py#L1-L11)：

```python
from dataclasses import dataclass

@dataclass(slots=True)
class SamplingParams:
    temperature: float = 1.0
    max_tokens: int = 64
    ignore_eos: bool = False

    def __post_init__(self):
        assert self.temperature > 1e-10, "greedy sampling is not permitted"
```

要点：

- `@dataclass(slots=True)`：用 Python 数据类自动生成 `__init__`，`slots=True` 能省内存、加快属性访问。
- 三个字段都有默认值，所以 `SamplingParams()` 不传参也能用，默认是 `temperature=1.0, max_tokens=64`。bench.py 里就用 `SamplingParams()` 做了一次预热（[bench.py:L22](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/bench.py#L22)）。
- `__post_init__` 是数据类提供的钩子，在 `__init__` 之后自动调用，用来做参数校验。

注意 bench.py 还演示了 `ignore_eos=True` 的用法（[bench.py:L18](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/bench.py#L18)）：做基准测试时希望每条都生成满，不要因为偶然生成结束符就提前停，所以打开它。

#### 4.2.4 代码实践

**目标**：直观感受 `temperature` 和 `max_tokens` 对生成的影响。

**操作步骤**（基于 example.py 改造）：

1. 把 [example.py:L11](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/example.py#L11) 改成：
   ```python
   sampling_params = SamplingParams(temperature=0.6, max_tokens=32)
   ```
   运行，观察 `Completion:` 的长度明显变短。
2. 再改成 `max_tokens=512`，运行，观察输出更长（可能因为生成结束符而提前停，这是正常现象）。
3. 试着传 `temperature=0`：
   ```python
   SamplingParams(temperature=0, max_tokens=64)
   ```
   运行，观察程序抛出 `AssertionError: greedy sampling is not permitted`。
4. 用同一个 prompt、同一个低温度（如 `temperature=1e-5`）连跑两次，对比输出是否几乎一致。

**需要观察的现象**：

- `max_tokens` 越大，可能生成的文本越长（上限受结束符或 max_tokens 制约）。
- 温度为 0 会直接报错；极小温度下两次输出高度相似。

**预期结果**：步骤 3 必然抛出断言错误（这是代码保证的）；其余文字内容「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么不传任何参数时 `SamplingParams()` 默认只会生成 64 个 token？
**答案**：因为 `max_tokens` 默认值是 64（见源码字段定义）。

**练习 2**：如果你希望模型「把回答写满 200 个 token，中途别因为说完就停」，该怎么设置？
**答案**：`SamplingParams(max_tokens=200, ignore_eos=True)`。

**练习 3**：用户传 `temperature=0` 会发生什么？为什么？
**答案**：会触发 `__post_init__` 里的断言抛错，因为 nano-vllm 不允许 greedy 采样，其采样器需要非零温度。

---

### 4.3 LLM：推理引擎统一入口

#### 4.3.1 概念说明

`LLM` 是用户与引擎打交道的 **唯一入口**。但有意思的是，它本身几乎没有代码——它只是 `LLMEngine` 的子类，且方法体为空（[nanovllm/llm.py:L1-L5](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/llm.py#L1-L5)）：

```python
from nanovllm.engine.llm_engine import LLMEngine

class LLM(LLMEngine):
    pass
```

为什么这样设计？因为 README 明确说「API mirrors vLLM's interface」（[README.md:L34-L44](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/README.md#L34-L44)）——vLLM 里 `LLM` 是面向用户的类，`LLMEngine` 是更底层的引擎。nano-vllm 复刻了这个命名习惯：对外叫 `LLM`，实现细节都在 `LLMEngine`。这样从 vLLM 迁移过来的用户会感到熟悉。

所以理解 `LLM`，本质就是理解 `LLMEngine` 的 **构造函数** 和 **`generate` 方法**。

#### 4.3.2 核心流程

调用 `LLM` 推理的整体流程：

```text
LLM(model, enforce_eager=…, tensor_parallel_size=…)
   ├── 读取模型配置（Config）
   ├── 拉起 worker 进程（张量并行时）
   ├── 创建 ModelRunner（实际跑模型）
   └── 创建 Tokenizer + Scheduler

llm.generate(prompts, sampling_params)
   ├── 把每条 prompt 加成请求（add_request）
   └── while not finished:
         step() → 调度 → 前向 → 后处理
   最终返回 [{"text": str, "token_ids": list[int]}, ...]
```

关键点：`generate` 返回的是 **字典列表**，每个字典有两个键 `text`（解码后的字符串）和 `token_ids`（token id 列表）。example.py 里正是用 `output['text']` 取文本（[example.py:L29](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/example.py#L29)）。

#### 4.3.3 源码精读

**构造函数** 在 [nanovllm/engine/llm_engine.py:L17-L35](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L17-L35)。最值得注意的一个设计：它用 `**kwargs` 接收任意关键字参数，然后只挑出属于 `Config` 字段的那些来构造配置：

```python
def __init__(self, model, **kwargs):
    config_fields = {field.name for field in fields(Config)}
    config_kwargs = {k: v for k, v in kwargs.items() if k in config_fields}
    config = Config(model, **config_kwargs)
    ...
```

这意味着 `LLM(path, enforce_eager=True, tensor_parallel_size=1)` 里的 `enforce_eager`、`tensor_parallel_size` 必须是 `Config` 的字段名，否则会被静默忽略。哪些字段合法？看 [nanovllm/config.py:L7-L18](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/config.py#L7-L18)，常用的有：

| 参数 | 默认值 | 含义 |
|------|--------|------|
| `max_model_len` | `4096` | 模型最大上下文长度 |
| `tensor_parallel_size` | `1` | 张量并行 GPU 数（1～8） |
| `enforce_eager` | `False` | 是否强制不用 CUDA Graph（True=更省显存/更慢启动，False=用图加速） |
| `gpu_memory_utilization` | `0.9` | KV cache 可用显存比例 |
| `max_num_seqs` | `512` | 最多同时调度多少条序列 |
| `max_num_batched_tokens` | `16384` | 单次 prefill 最多处理的 token 数 |

example.py 里设了 `enforce_eager=True, tensor_parallel_size=1`（[example.py:L9](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/example.py#L9)）——单卡、关闭 CUDA Graph，是最稳妥的「先跑通」配置；bench.py 为了性能设了 `enforce_eager=False`（[bench.py:L15](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/bench.py#L15)）。

**`generate` 方法** 在 [nanovllm/engine/llm_engine.py:L60-L90](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L60-L90)。它的签名是：

```python
def generate(self, prompts, sampling_params, use_tqdm=True) -> list[str]:
```

- `prompts` 可以是字符串列表 `list[str]`，也可以是 **token id 列表的列表** `list[list[int]]`（bench.py 就直接传了随机 token id）。
- `sampling_params` 可以是单个对象（所有 prompt 共用），也可以是列表（每条 prompt 一个）。
- `use_tqdm` 控制是否显示进度条。

方法内部会先 `add_request` 把所有请求入队，然后在一个 `while not self.is_finished()` 循环里反复调用 `step()` 推进。进度条的后缀 `Prefill/Decode` 是在 [llm_engine.py:L72-L83](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L72-L83) 算出来的——根据 `step()` 返回的 `num_tokens` 正负判断当前是 prefill 还是 decode，再除以耗时得到吞吐。最后把所有 token id 解码成文本返回（[llm_engine.py:L89-L90](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L89-L90)）。

#### 4.3.4 代码实践

**目标**：验证 `LLM` 的返回结构，并读懂终端进度条的含义。

**操作步骤**：

1. 运行原始 `example.py`。
2. 在 [example.py:L24](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/example.py#L24) 之后加一行（**示例代码**，用于查看返回结构）：
   ```python
   print("type:", type(outputs), "len:", len(outputs))
   print("keys:", list(outputs[0].keys()))
   print("token_ids[:10]:", outputs[0]["token_ids"][:10])
   ```
3. 观察终端：进度条先显示一个较大的 `Prefill=…tok/s`（处理 prompt 阶段，吞吐高），随后切换为 `Decode=…tok/s`（逐 token 生成，吞吐较低）。

**需要观察的现象**：

- `outputs` 是 `list`，长度等于 `prompts` 的条数；
- 每个元素是 `dict`，键为 `text` 和 `token_ids`；
- `token_ids` 是一串整数，长度约为生成的 token 数；
- 进度条后缀在 prefill 和 decode 之间切换。

**预期结果**：`keys: ['text', 'token_ids']` 是确定的；具体吞吐数值与生成内容「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`LLM` 类自己定义了 `generate` 方法吗？为什么我们还能调用它？
**答案**：没有。`LLM` 是 `LLMEngine` 的空子类，`generate` 继承自 `LLMEngine`。

**练习 2**：`LLM(path, enforce_eager=True, tensor_parallel_size=1)` 里，如果误写成 `LLM(path, enforce_eage=True)`（拼错），会发生什么？
**答案**：不会报错，但 `enforce_eage` 不是 `Config` 字段，会被 `config_kwargs` 过滤掉静默忽略，`enforce_eager` 保持默认值 `False`。

**练习 3**：`generate` 既能接受字符串 prompt，也能接受 token id 列表。这个能力是在哪里实现的？
**答案**：在 `add_request` 里（[llm_engine.py:L43-L47](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L43-L47)），它判断 `prompt` 是否为字符串，是就先 `tokenizer.encode`，否则直接当 token id 用。

---

## 5. 综合实践

把本讲的三个模块串成一个完整任务：**把 example.py 改成一个可对比的迷你实验脚本**。

**任务背景**：你想直观感受「温度与长度」如何影响生成，并验证你对返回结构的理解。

**操作步骤**：

1. 复制 `example.py` 为 `my_first_run.py`（**示例代码**，自己创建，不要改原文件）。
2. 把 prompts 改成 3 条不同风格的问题，例如：
   ```python
   raw_prompts = [
       "introduce yourself",
       "list all prime numbers within 100",
       "write a one-sentence greeting",
   ]
   ```
3. 用 `apply_chat_template` 包成 chat 格式（沿用 [example.py:L16-L23](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/example.py#L16-L23) 的写法）。
4. 跑两组对比：
   - 组 A：`SamplingParams(temperature=0.6, max_tokens=128)`
   - 组 B：`SamplingParams(temperature=1e-5, max_tokens=128)`（接近确定输出）
5. 对每条输出，打印：prompt、`len(output["token_ids"])`、`output["text"]` 的前 80 个字符。

**需要观察并记录的现象**：

- 进度条的 `Prefill` 与 `Decode` 吞吐数值各是多少？
- 组 A 与组 B 相比，哪组的输出更稳定、重复性更高？
- 不同 prompt 的 `token_ids` 长度是否相同？为什么？

**预期结果**：组 B（低温）多次运行结果应几乎一致；各 prompt 生成长度通常不同，因为它们在不同位置生成了结束符或达到语义结束。具体数值「待本地验证」。

**交付物**：一张小表格，记录每条 prompt 在组 A/组 B 下的生成 token 数与首句文本。这会让你真切体会到 `SamplingParams` 的作用。

---

## 6. 本讲小结

- nano-vllm 是一个约 **1200 行** 的 vLLM 极简复刻，聚焦 **离线推理**，主打可读性。
- 四大特性：**Prefix Caching、张量并行、torch.compile、CUDA Graph**，这些会在后续讲义展开。
- 对外只导出两个名字：`LLM` 和 `SamplingParams`（见 `__init__.py`）。
- `SamplingParams` 只有 `temperature / max_tokens / ignore_eos` 三个字段，且 **不允许 greedy（temperature=0）** 采样。
- `LLM` 是 `LLMEngine` 的空子类；构造参数通过 `**kwargs` 过滤后交给 `Config`，常用的是 `enforce_eager`、`tensor_parallel_size`、`max_model_len`。
- `generate` 返回 `[{"text": str, "token_ids": list[int]}, ...]`，终端进度条用 `Prefill/Decode` 后缀展示两类阶段的吞吐。

---

## 7. 下一步学习建议

你已经能跑通推理并看懂输出了，接下来建议：

1. **下一讲 u1-l2《目录结构与代码入口》**：系统梳理 `nanovllm` 包的 `engine / layers / models / utils` 四个子包，画出从 `LLM.generate` 到模型前向的完整模块依赖图，建立全局代码地图。
2. **再往后 u1-l3《从 generate 到推理主循环》**：深入 `LLMEngine.step()`，理解「调度 → 前向 → 后处理」的循环细节。
3. **阅读建议**：在进入下一讲前，可以先把 [nanovllm/engine/llm_engine.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py) 通读一遍（它只有 90 行），你会对引擎骨架有整体印象，学起来更顺。
