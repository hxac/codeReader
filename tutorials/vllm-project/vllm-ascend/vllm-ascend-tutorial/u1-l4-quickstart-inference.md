# 快速上手：运行第一个推理

## 1. 本讲目标

学完本讲后，你应该能够：

1. 看懂 `examples/offline_inference_npu.py`，并能用自己的 prompt 跑通一次离线生成。
2. 说清楚示例里 `LLM` / `SamplingParams` / `generate()` 三者的关系，以及输出对象的结构。
3. 解释为什么几乎每个 NPU 示例开头都要设置 `VLLM_WORKER_MULTIPROC_METHOD=spawn` 和 `VLLM_USE_MODELSCOPE=True`。
4. 说出 `examples/` 目录覆盖的几类典型场景（张量并行、Embedding、睡眠模式、指标采集），并知道各自对应哪个示例文件。

## 2. 前置知识

本讲假设你已经完成 **u1-l1（项目定位）** 与 **u1-l3（环境准备与安装构建）**，知道：

- `vllm-ascend` 是一个**硬件插件**，它本身不改变你调用 vLLM 的方式，而是被 vLLM 通过 `entry points` 自动发现并加载（这点在 **u1-l5** 会详细展开）。
- 真正运行这些示例需要一台装有昇腾 NPU、CANN、torch-npu 的机器。本讲的代码实践**不要求你有 NPU**——我们只让你读懂并改写代码、预测输出。

这里补三个本讲会用到的通用概念：

- **离线推理（offline inference）**：你写一个 Python 脚本，用 `LLM` 类直接在前端进程里把一批 prompt 一次性生成完，结果直接拿到内存里打印。它和「在线服务」相对——在线服务是用 `vllm serve` 起一个常驻 HTTP 服务，客户端通过 OpenAI 兼容 API 反复发请求。
- **采样参数（SamplingParams）**：控制「怎么从模型概率分布里挑下一个 token」的配置，例如最多生成多少 token、温度多高、是否贪心。它和「模型本身」是解耦的两件事。
- **张量并行（Tensor Parallelism, TP）**：把模型的权重矩阵按列或行切到多张卡上，每张卡算一部分，再通过通信原语（这里是 HCCL）把结果合并。`tensor_parallel_size=2` 表示用 2 张 NPU。

## 3. 本讲源码地图

本讲围绕 `examples/` 目录下的几个最小示例展开：

| 文件 | 一句话职责 |
| --- | --- |
| [examples/offline_inference_npu.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/examples/offline_inference_npu.py) | 单卡离线生成的「hello world」，本讲主线。 |
| [examples/offline_inference_npu_tp2.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/examples/offline_inference_npu_tp2.py) | 双卡张量并行版，演示 `tensor_parallel_size=2`。 |
| [examples/offline_inference_sleep_mode_npu.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/examples/offline_inference_sleep_mode_npu.py) | 睡眠/唤醒示例：`sleep` 后释放显存，`wake_up` 后恢复继续推理。 |
| [examples/offline_embed.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/examples/offline_embed.py) | Embedding 示例，用 `runner="pooling"` 跑句向量并算相似度。 |
| [examples/offline_inference_metrics.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/examples/offline_inference_metrics.py) | 演示如何拿到请求级 `metrics`（首 token 时间等）。 |

> 一个贯穿全讲的观察：**这些示例里没有任何一行 `import vllm_ascend`**。这就是「硬件插件」的精髓——从用户视角，代码和上游 vLLM 完全一样；插件由 vLLM 在启动时按 entry points 自动加载。这条加载链路我们在 **u1-l5** 会拆开讲。

## 4. 核心概念与源码讲解

### 4.1 离线推理的最小骨架

#### 4.1.1 概念说明

vLLM 把「模型」和「怎么调用模型」拆成了两个对象：

- `LLM`：封装了模型加载、KV cache 管理、调度、前向执行、采样这一整条流水线。你 `LLM(model=...)` 时它就把模型拉到 NPU 上、把显存规划好。
- `SamplingParams`：纯粹的「采样策略」配置，不碰模型权重。

一次离线生成的最小步骤是：**建采样参数 → 建 LLM → 调 `generate()` → 解析输出**。`generate()` 接收一批 prompt（list），返回一个同样顺序的输出列表。

#### 4.1.2 核心流程

```
SamplingParams(max_tokens=100, temperature=0.0)
        │
        ▼
LLM(model="Qwen/Qwen2.5-0.5B-Instruct")
   ├─ vLLM 扫描 entry points → 加载 vllm-ascend 插件
   ├─ 选中 NPUPlatform（决定用 NPU 算子/注意力后端/通信器）
   ├─ 拉取并加载权重到 NPU
   └─ 分配 KV cache、初始化 worker 子进程
        │
        ▼
outputs = llm.generate(prompts, sampling_params)
   └─ 每个 output 含 .prompt 和 .outputs[0].text
```

注意 `temperature=0.0` 表示**贪心解码（greedy）**——每步取概率最大的 token，结果基本确定可复现，非常适合用来验证环境是否跑通。

#### 4.1.3 源码精读

示例开头先设两个环境变量（4.2 节详讲），然后导入 vLLM 的两个核心类：

[examples/offline_inference_npu.py:23-26](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/examples/offline_inference_npu.py#L23-L26) — 设置 `VLLM_USE_MODELSCOPE` 与 `VLLM_WORKER_MULTIPROC_METHOD`，再 `from vllm import LLM, SamplingParams`。

主流程非常短，四步走：

[examples/offline_inference_npu.py:30-46](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/examples/offline_inference_npu.py#L30-L46) — 准备 4 条 prompt，建 `SamplingParams(max_tokens=100, temperature=0.0)`，建 `LLM(model="Qwen/Qwen2.5-0.5B-Instruct")`，然后 `llm.generate(prompts, sampling_params)`。

输出对象的取值方式是固定的——每个 `output` 有 `output.prompt`（原 prompt）和 `output.outputs[0].text`（生成的文本）：

[examples/offline_inference_npu.py:47-50](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/examples/offline_inference_npu.py#L47-L50) — 遍历结果并用 f-string 打印 `Prompt` 与 `Generated text`。

> 为何是 `.outputs[0]`？因为一次 `generate` 默认每个 prompt 只采一条（`n=1`），但 vLLM 支持 `SamplingParams(n=k)` 一次采多条，所以输出是一个列表，取第 0 条是最常见的取法。

#### 4.1.4 代码实践

**实践目标**：把主线示例改成「读你自己的 prompt 并打印生成结果」。本实践无需 NPU，只需写代码与预测输出。

**操作步骤**：

1. 复制 `examples/offline_inference_npu.py` 为 `my_inference.py`（放在任意位置，**不要**放进仓库的 `examples/`，避免污染项目）。
2. 把 `prompts` 列表替换成你自己的内容。
3. 保留环境变量设置与导入不变。
4. 写下你预期的输出形态。

下面是改写后的完整脚本（**示例代码**，非项目原有文件）：

```python
# 示例代码：my_inference.py
# isort: skip_file
import os

os.environ["VLLM_USE_MODELSCOPE"] = "True"
os.environ["VLLM_WORKER_MULTIPROC_METHOD"] = "spawn"

from vllm import LLM, SamplingParams


def main():
    prompts = [
        "请用一句话解释什么是 KV cache。",
    ]

    sampling_params = SamplingParams(max_tokens=64, temperature=0.0)
    llm = LLM(model="Qwen/Qwen2.5-0.5B-Instruct")

    outputs = llm.generate(prompts, sampling_params)
    for output in outputs:
        print(f"Prompt: {output.prompt!r}")
        print(f"Generated: {output.outputs[0].text!r}")


if __name__ == "__main__":
    main()
```

**需要观察的现象**：
- 脚本启动时会打印权重下载/加载日志、KV cache 块数、worker 子进程启动等信息。
- 第一条 prompt 对应一条生成文本。

**预期结果**（**待本地验证**）：因为你没有 NPU，这段代码无法在普通 CPU 机器上真正运行（`vllm-ascend` 需要 torch-npu）。预期它是「能在 NPU 环境下跑通」的目标态；在无 NPU 机器上，导入阶段就会因缺少 torch-npu 而报错，这正是下一节要解释的环境前提。请你把「这条 prompt 大概会生成什么内容」作为预测写下来即可。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `temperature=0.0` 改成 `temperature=0.8`，同一个 prompt 多次运行，生成结果会怎样？

> **参考答案**：`temperature>0` 会引入随机采样，同一 prompt 多次运行大概率得到不同文本；`temperature=0` 是贪心解码，结果几乎确定可复现。

**练习 2**：`output.outputs[0]` 里的 `[0]` 对应 `SamplingParams` 的哪个参数？

> **参考答案**：对应 `n`（一次采样条数）。默认 `n=1`，所以列表只有一条；若设 `SamplingParams(n=3)`，则 `output.outputs` 会有 3 条。

**练习 3**：示例里为什么没有 `import vllm_ascend`，推理却用的是 NPU？

> **参考答案**：因为 `vllm-ascend` 通过 entry points 注册成硬件插件，vLLM 启动时自动发现并加载它，从而选中 `NPUPlatform`。用户代码层面完全感知不到插件的存在——这正是「可插拔硬件」的设计目标。

---

### 4.2 两个关键环境变量：modelscope 与 spawn

#### 4.2.1 概念说明

几乎每个 NPU 示例的前两行都是这两个赋值，它们直接影响「模型从哪下」和「worker 子进程怎么起」：

```python
os.environ["VLLM_USE_MODELSCOPE"] = "True"
os.environ["VLLM_WORKER_MULTIPROC_METHOD"] = "spawn"
```

- **`VLLM_USE_MODELSCOPE=True`**：让 vLLM 从 **ModelScope（魔搭社区）** 拉模型，而不是默认的 Hugging Face。vllm-ascend 的主要用户群体在国内，ModelScope 对这些模型有镜像、下载更快更稳。它必须在 `from vllm import ...` **之前**设置，因为这是导入时即生效的环境变量。
- **`VLLM_WORKER_MULTIPROC_METHOD=spawn`**：把 vLLM 的 worker 子进程启动方式设为 `spawn`，而不是 Python 默认的 `fork`。

#### 4.2.2 核心流程

为什么 NPU 必须用 `spawn`？关键在于设备运行时（CANN/torch-npu/HCCL）对**进程复制**的容忍度。

Python 多进程有两种主要启动方式：

| 方式 | 行为 | 对设备运行时 |
| --- | --- | --- |
| `fork`（默认） | 子进程复制父进程的整块内存与运行时状态，不重新执行模块顶层代码 | 子进程会**继承**父进程里已初始化的 CANN/HCCL 设备句柄和线程，这些句柄无法被安全复制 |
| `spawn` | 子进程启动一个全新的解释器，**重新 import** 所有模块、重新初始化运行时 | 每个 worker 干净地从头初始化自己的设备上下文，互不污染 |

vLLM 为模型前向开 worker 子进程（多卡时每卡一个）。在 NPU 上若用 `fork`，子进程会带着父进程「半初始化」的设备状态启动，容易导致 HCCL 通信初始化失败或设备句柄错乱；改用 `spawn` 后每个 worker 重新、独立地初始化 CANN/HCCL，问题随之消失。所以这是 vllm-ascend 在 NPU 场景下**强约束**的设置。

#### 4.2.3 源码精读

主线示例与 tp2 示例开头都一模一样地设置了这两个变量，且都在导入 vLLM **之前**：

[examples/offline_inference_npu.py:21-26](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/examples/offline_inference_npu.py#L21-L26) — `os` 导入后立刻设两个环境变量，再 `from vllm import LLM, SamplingParams`。注意注释 `# isort: skip_file`：它告诉 isort 不要重排 import 顺序，从而保证「先设环境变量、再导入 vLLM」的顺序不被破坏。

[examples/offline_inference_npu_tp2.py:20-26](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/examples/offline_inference_npu_tp2.py#L20-L26) — tp2 示例的开头完全相同，说明这是所有多卡/单卡 NPU 示例的通用前置。

#### 4.2.4 代码实践

**实践目标**：亲手验证「顺序很重要」——证明把环境变量设到导入之后会失效（逻辑层面的推理，无需 NPU）。

**操作步骤**：

1. 想象两段代码，仅顺序不同：

   ```python
   # A：先设后导入（正确）
   os.environ["VLLM_WORKER_MULTIPROC_METHOD"] = "spawn"
   from vllm import LLM
   ```

   ```python
   # B：先导入后设（错误）
   from vllm import LLM
   os.environ["VLLM_WORKER_MULTIPROC_METHOD"] = "spawn"
   ```

2. 解释为什么 B 中的设置不会生效。

**需要观察的现象**：B 中虽然赋了值，但 vLLM 在导入时（构建进程池相关代码）已经读取过这个变量的默认值，赋值太晚。

**预期结果**：A 能让 worker 用 `spawn` 启动；B 大概率仍走默认 `fork`（**待本地验证**：具体取决于 vLLM 读取该变量的时机，但「导入前设置」是社区公认的安全做法，因此示例才用 `isort: skip_file` 锁住顺序）。

#### 4.2.5 小练习与答案

**练习 1**：`# isort: skip_file` 这条注释在示例里解决什么问题？

> **参考答案**：阻止 isort 把 `import os`、`os.environ[...]`、`from vllm import ...` 按字母序重排，从而保住「先设环境变量再导入 vLLM」的关键顺序。

**练习 2**：用一句话说明 `fork` 与 `spawn` 在设备运行时上的本质区别。

> **参考答案**：`fork` 复制父进程的设备句柄（CANN/HCCL 状态被一起继承，不安全），`spawn` 让子进程全新启动并重新初始化设备上下文（安全）。

---

### 4.3 多卡张量并行示例

#### 4.3.1 概念说明

当单张 NPU 装不下一个模型，或想加速时，就用多卡。最常见的是**张量并行（TP）**：把每一层的权重切到多张卡上。`offline_inference_npu_tp2.py` 演示的就是 2 卡 TP。它的主流程和单卡版几乎一样，区别全在 `LLM(...)` 的构造参数上。

#### 4.3.2 核心流程

```
LLM(model=..., tensor_parallel_size=2, ...)
        │
        ├─ 启动 2 个 worker 子进程（每个绑定一张 NPU）
        ├─ init_ascend_model_parallel：建立 TP=2 的并行组（u7-l1 详讲）
        ├─ 各卡加载自己那一半权重
        └─ 前向时各卡算一部分，再用 HCCL 通信合并
```

#### 4.3.3 源码精读

tp2 版的构造多了几个参数：

[examples/offline_inference_npu_tp2.py:40-46](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/examples/offline_inference_npu_tp2.py#L40-L46) — `LLM(model="deepseek-ai/DeepSeek-V2-Lite", tensor_parallel_size=2, enforce_eager=True, trust_remote_code=True, max_model_len=1024)`。

逐个参数说明：

- `tensor_parallel_size=2`：用 2 张 NPU 做张量并行，这是本示例的核心。
- `enforce_eager=True`：关闭图捕获（NPU 上的 ACL Graph，见 **u8-l3**），强制走 eager 逐算子执行。调试时常用，能避开图编译的复杂性。
- `trust_remote_code=True`：允许执行模型仓库里自带的 Python 代码（DeepSeek 等模型需要）。
- `max_model_len=1024`：把最大上下文长度压到 1024，省显存、加快启动，适合演示。

#### 4.3.4 代码实践

**实践目标**：对比单卡版与 tp2 版的差异，理解每个新增参数的取舍。

**操作步骤**：

1. 打开 `examples/offline_inference_npu.py` 与 `examples/offline_inference_npu_tp2.py` 并排看。
2. 列出 tp2 版相对单卡版**新增**的 4 个 `LLM` 参数。
3. 对每个参数写一句话：「关掉它会怎样」。

**需要观察的现象**：单卡版只传了 `model=`，tp2 版多传了 TP、eager、trust_remote_code、max_model_len。

**预期结果**（**待本地验证**）：

| 参数 | 关掉/改掉的后果 |
| --- | --- |
| `tensor_parallel_size=2` → 默认 1 | 只用单卡，大模型可能 OOM |
| `enforce_eager=True` → False | 启用 ACL Graph 图捕获，启动更慢但推理更快 |
| `trust_remote_code=True` → False | 拒绝执行模型自带代码，加载失败 |
| `max_model_len=1024` → 默认 | 用模型原始长上下文，占显存更多 |

#### 4.3.5 小练习与答案

**练习 1**：为什么演示脚本要加 `enforce_eager=True`？

> **参考答案**：为了关闭图捕获、走 eager 模式，启动更快、更易调试，避免图编译引入的额外复杂度和时间，适合「快速跑通」的演示目的。

**练习 2**：`tensor_parallel_size=2` 要求几张 NPU？

> **参考答案**：至少 2 张可用 NPU；否则 vLLM 在建立并行组时会因卡数不足而报错。

---

### 4.4 特性示例巡览：睡眠模式、Embedding、指标

#### 4.4.1 概念说明

`examples/` 目录还覆盖了几个典型场景，它们展示了 vLLM API 的不同侧面。这一节带你快速浏览，建立「遇到需求知道找哪个示例」的索引。

- **睡眠模式（sleep mode）**：让常驻服务在不服务请求时**主动释放显存**（把权重/KV cache 卸到内存或丢弃），来支持「多模型分时复用同一批卡」。`llm.sleep()` 释放，`llm.wake_up()` 恢复。
- **Embedding**：不是生成文本，而是把文本映射成向量，用于检索/相似度。要告诉 vLLM「我用 pooling runner 而不是 generate runner」。
- **请求级指标（metrics）**：拿到每条请求的首 token 时间、完成时间等，用于性能分析。

#### 4.4.2 核心流程

睡眠模式的生命周期：

```
LLM(enable_sleep_mode=True)
   ├─ generate() → 正常推理
   ├─ sleep(level=1)   → 释放显存（断言占用 < 权重大小）
   ├─ wake_up()        → 恢复权重/KV
   └─ generate() again → 推理结果应与上次一致（断言相等）
```

Embedding 的调用链则换了个入口：`LLM(runner="pooling")` + `model.embed(...)`，而不是 `generate`。

#### 4.4.3 源码精读

**睡眠模式**——构造时开启，然后用 `sleep`/`wake_up` 配对：

[examples/offline_inference_sleep_mode_npu.py:36-52](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/examples/offline_inference_sleep_mode_npu.py#L36-L52) — `LLM(..., enable_sleep_mode=True)`，生成后 `llm.sleep(level=1)`；用 `torch.npu.mem_get_info()` 比较睡眠前后的空闲显存，断言睡眠后占用小于 1 GiB（0.5B 模型权重约 1 GiB）；再 `llm.wake_up()` 重新生成，并断言两次输出文本完全一致。这里用 `torch.npu.mem_get_info()` 读取 NPU 显存——这是 torch-npu 提供的 NPU 版显存查询接口。

**Embedding**——换 runner、换方法：

[examples/offline_embed.py:49-55](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/examples/offline_embed.py#L49-L55) — `LLM(model="Qwen/Qwen3-Embedding-0.6B", runner="pooling")`，然后 `model.embed(input_texts)` 得到句向量，用矩阵乘 `embeddings[:2] @ embeddings[2:].T` 算 query 与 document 的相似度分数。

**指标采集**——默认是关的，要显式打开：

[examples/offline_inference_metrics.py:48-67](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/examples/offline_inference_metrics.py#L48-L67) — `LLM(model=..., disable_log_stats=False)` 才能让 `output.metrics` 非 `None`，从而打印 `arrival_time`、`first_token_time`、`finished_time` 等。脚本顶部 docstring 明确指出：默认 `disable_log_stats=True`，`output.metrics` 会是 `None`，对应 issue #5027。

#### 4.4.4 代码实践

**实践目标**：建立「需求 → 示例文件」的映射表（源码阅读型实践，无需 NPU）。

**操作步骤**：

1. 通读本节引用的三个示例。
2. 完成下表的「适用场景」与「关键 API」两列。

**需要观察的现象**：三个示例分别用 `generate`/`sleep+wake_up`、`embed`、`generate + metrics`，体现了同一套 vLLM API 的不同用法。

**预期结果**（参考答案）：

| 需求 | 示例文件 | 关键 API |
| --- | --- | --- |
| 多模型分时复用显存 | `offline_inference_sleep_mode_npu.py` | `enable_sleep_mode=True`、`llm.sleep(level=1)`、`llm.wake_up()` |
| 文本检索/相似度 | `offline_embed.py` | `LLM(..., runner="pooling")`、`model.embed(...)` |
| 分析首 token 延迟 | `offline_inference_metrics.py` | `disable_log_stats=False`、`output.metrics` |

#### 4.4.5 小练习与答案

**练习 1**：睡眠模式示例里，为什么睡眠后断言「显存占用 < 1 GiB」就能说明 sleep 生效？

> **参考答案**：0.5B 模型权重本身约 1 GiB，睡眠后权重被卸载/释放，若剩余占用小于权重本身，说明显存确被释放，sleep 生效。

**练习 2**：`offline_inference_metrics.py` 不加 `disable_log_stats=False` 会怎样？

> **参考答案**：`output.metrics` 为 `None`，无法读取首 token 时间等指标；脚本因此特地在分支里提示 `Metrics: None (set disable_log_stats=False to enable)`。

**练习 3**：Embedding 示例为什么用 `runner="pooling"` 而不是默认的 generate runner？

> **参考答案**：Embedding 任务不是逐 token 生成，而是对整段输入做一次聚合（pooling）得到向量，所以要切到 pooling runner，并改用 `.embed()` 入口。

---

## 5. 综合实践

**任务**：写一份「NPU 离线生成速查卡」，把本讲内容串起来。

要求你产出一份 Markdown 文档（**示例文档**，可写在你的本地笔记里，**不要**写进仓库），包含：

1. **最小可运行脚本**：基于 `offline_inference_npu.py`，但换成你自己的一条中文 prompt，并把 `max_tokens` 改成 50。
2. **两个必设环境变量**及一句话理由（modelscope / spawn）。
3. **一张参数速查表**：列出 `tensor_parallel_size`、`enforce_eager`、`trust_remote_code`、`max_model_len`、`enable_sleep_mode`、`runner`、`disable_log_stats` 各自的作用，并标注「来自哪个示例」。
4. **一行预期输出**：写出你那条 prompt 生成结果的大致形态（**待本地验证**）。

完成这份速查卡后，你应该能在拿到 NPU 机器时，立刻知道：装好 u1-l3 的依赖 → 复制脚本 → 设两个环境变量 → `python my_inference.py` 跑通第一次推理。

> 关于**在线服务**：本仓库的 `examples/` 聚焦离线脚本；起在线 OpenAI 兼容服务用的是上游 vLLM 的 `vllm serve <model>` 命令。它在启动时同样会通过 entry points 自动加载 vllm-ascend 插件，所以服务侧无需额外配置——这也再次印证了「插件对用户透明」。

## 6. 本讲小结

- 离线推理的最小骨架是 `SamplingParams` → `LLM(model=...)` → `llm.generate()` → 解析 `output.outputs[0].text`。
- 所有 NPU 示例开头都要设 `VLLM_USE_MODELSCOPE=True`（从魔搭下模型）和 `VLLM_WORKER_MULTIPROC_METHOD=spawn`（让 worker 子进程干净初始化 CANN/HCCL），且必须在导入 vLLM **之前**设置。
- 这些示例里**没有** `import vllm_ascend`——插件由 vLLM 按 entry points 自动加载，对用户完全透明，这正是「可插拔硬件」的体现。
- `offline_inference_npu_tp2.py` 用 `tensor_parallel_size=2` 演示多卡 TP，并用 `enforce_eager`/`max_model_len` 等参数权衡启动速度与显存。
- `examples/` 还覆盖睡眠模式（`sleep`/`wake_up`）、Embedding（`runner="pooling"` + `embed`）、请求级指标（`disable_log_stats=False`）等典型场景。

## 7. 下一步学习建议

- 想知道「插件到底是怎么被自动发现并选中 `NPUPlatform` 的」，进入 **u1-l5（插件入口：注册机制与发现流程）**，拆解 `vllm_ascend/__init__.py` 的 `register()`。
- 想理解 `NPUPlatform` 选中后做了哪些关键改写，进入 **u2-l1（NPUPlatform：平台核心能力）**。
- 想深入睡眠模式的显存释放细节（`device_allocator`），可先记下，留到 **u10-l3（KV 卸载与睡眠模式）**。
- 建议在本机对照阅读：`examples/offline_inference_npu.py`、`examples/offline_inference_npu_tp2.py`、`examples/offline_embed.py`，把本讲的「需求 → 示例」映射亲手走一遍。
