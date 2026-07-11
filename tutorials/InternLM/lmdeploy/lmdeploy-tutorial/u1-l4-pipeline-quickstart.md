# pipeline 推理快速上手

## 1. 本讲目标

学完本讲后，你应该能够：

- 用 `lmdeploy.pipeline(...)` 在几行代码内跑通一次文本推理（离线推理）。
- 分清 `Pipeline` 类对外暴露的三种推理方式：`__call__` / `infer`（一次性返回）、`stream_infer`（流式逐段返回）、`chat`（多轮会话），并知道 `batch_infer` 已被废弃。
- 看懂 `Pipeline.__init__` 里「默认后端选择逻辑」到底写在哪一行、按什么规则在 PyTorch 与 TurboMind 之间二选一。
- 理解推理返回值 `Response` 的关键字段（`text`、`generate_token_len`、`input_token_len`、`finish_reason`、`index`）。

本讲只解决一个问题：**用户调 `pipeline()` 之后，从函数入口到拿到一段生成文本，中间发生了什么。** 引擎内部如何调度、如何做 Paged Attention，留到 U4 再讲。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：推理是「填空」的循环。**
大语言模型本质是一个「下一个 token 预测器」。给它一段输入 token 序列，它输出下一个 token 的概率分布；把选中的 token 拼回输入，再预测下一个，如此循环，直到遇到结束符（EOS）或达到长度上限。一次推理因此分为两个阶段：

- **Prefill（预填充）**：一次性「读」完输入 prompt，算出第一个输出 token。这一步类似一次大矩阵乘，计算密集。
- **Decode（解码）**：每生成一个 token 就前进一步，这一步显存带宽密集。

`pipeline()` 不要求你理解这些细节，但理解「生成是逐步的」后，你就能明白为什么会有「流式」接口——它本质就是把每一步 decode 产生的 token 立刻交给你。

**直觉二：lmdeploy 有两套引擎，但对用户只露一个入口。**
正如 U1 前几讲所说，lmdeploy 内部并存两套推理引擎：

- **PyTorch 引擎**：纯 Python（`lmdeploy/pytorch/`），可读性强、上手快。
- **TurboMind 引擎**：C++/CUDA（`src/turbomind/`），追求极致性能。

用户不需要自己选，`pipeline()` 会根据模型自动判断（本讲 4.2 会讲清楚规则）。这就是「两条后端，一个 Pipeline」。

**直觉三：`pipeline()` 返回的对象既是「引擎」，也是「可调用对象」。**
拿到 `pipe` 后，既可以用 `pipe.infer(prompt)`，也可以直接 `pipe(prompt)`——后者通过 Python 的 `__call__` 魔术方法转调 `infer`。这点在 README 的示例里常见。

> 名词速查
> - **token**：模型处理的最小单元，一个汉字或词常被切成 1～2 个 token。
> - **prompt**：你给模型的输入文本。
> - **session（会话）**：一段多轮对话的上下文状态，用于 `chat` 多轮场景。
> - **EOS（End of Sequence）**：模型自带的「说完了」特殊 token。

## 3. 本讲源码地图

本讲只涉及两个核心文件，外加一个后端选择辅助文件与一个类型定义文件：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [lmdeploy/api.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/api.py) | 顶层工厂函数 `pipeline()` | `pipeline()` 如何转交给 `Pipeline` |
| [lmdeploy/pipeline.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py) | `Pipeline` 类的完整实现 | `__init__`、`infer`、`stream_infer` |
| [lmdeploy/archs.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py) | 架构识别与后端选择 | `autoget_backend_config`、`get_task` |
| [lmdeploy/messages.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py) | 核心数据类型 | `GenerationConfig`、`Response` |

公开 API 都在 [lmdeploy/__init__.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/__init__.py#L1-L13) 的 `__all__` 里登记：`pipeline`、`Pipeline`、`GenerationConfig`、`PytorchEngineConfig`、`TurbomindEngineConfig`、`ChatTemplateConfig` 等。

---

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：

1. **4.1 `api.pipeline` 工厂函数** —— 入口
2. **4.2 `Pipeline` 类与后端自动选择** —— 初始化
3. **4.3 `infer` 与 `stream_infer`：两种推理接口** —— 推理

### 4.1 `api.pipeline` 工厂函数

#### 4.1.1 概念说明

`lmdeploy.pipeline(...)` 是用户最常用的入口。它叫「工厂函数（factory function）」——本身不干活，只负责把参数原样转交给 `Pipeline` 类的构造函数，然后返回一个 `Pipeline` 实例。

为什么要多一层函数而不是直接让用户 `Pipeline(...)`？因为函数形式更短、更像「一句话起服务」的体验，也方便在文档里作为推荐写法。README 的 Quick Start 用的就是它：

```python
import lmdeploy
with lmdeploy.pipeline("internlm/internlm3-8b-instruct") as pipe:
    response = pipe(["Hi, pls intro yourself", "Shanghai is"])
    print(response)
```

#### 4.1.2 核心流程

```text
用户调 lmdeploy.pipeline(model_path, ...)
        │
        ▼
api.py 的 pipeline() 函数
        │  原样转发所有参数
        ▼
Pipeline(model_path, backend_config=..., ...)
        │
        ▼
返回一个 Pipeline 实例（pipe）
```

`pipeline()` 接受的关键参数（与 `Pipeline.__init__` 完全一致）：

| 参数 | 含义 |
| --- | --- |
| `model_path` | 模型路径：本地目录、HF model id（如 `Qwen/Qwen2.5-7B-Instruct`）、或量化模型 id |
| `backend_config` | 引擎配置（`PytorchEngineConfig` / `TurbomindEngineConfig`），默认 `None` 表示自动选 |
| `chat_template_config` | 对话模板配置，默认 `None` 表示从模型自带 tokenizer 推断 |
| `log_level` | 日志级别，默认 `'WARNING'` |
| `trust_remote_code` | 是否信任模型仓库里的远程代码，默认 `False` |
| `speculative_config` | 投机解码配置，默认 `None`（U9-l2 讲） |

#### 4.1.3 源码精读

工厂函数本体在 [lmdeploy/api.py:15-79](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/api.py#L15-L79)，开头是参数与长篇 docstring，真正「干活」的只有最后 8 行：

[lmdeploy/api.py:72-79](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/api.py#L72-L79) —— 把所有参数原样传给 `Pipeline` 并返回：

```python
return Pipeline(model_path,
                backend_config=backend_config,
                chat_template_config=chat_template_config,
                log_level=log_level,
                max_log_len=max_log_len,
                trust_remote_code=trust_remote_code,
                speculative_config=speculative_config,
                **kwargs)
```

> 注意：同文件里的 `serve()` 与 `client()` 两个函数已经被废弃，调用会直接抛 `NotImplementedError`（[api.py:82-102](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/api.py#L82-L102)、[api.py:105-123](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/api.py#L105-L123)）。它们只保留签名做兼容提示，正确做法是改用 CLI `lmdeploy serve api_server`（U8 讲）和 `from lmdeploy.serve import APIClient`。**不要在代码里再调用它们。**

#### 4.1.4 代码实践

**实践目标**：确认 `pipeline()` 与 `Pipeline()` 等价，并看清工厂函数的真实「厚度」。

**操作步骤**：

1. 在已安装 lmdeploy 的环境里，打开 Python 交互终端。
2. 执行下面的「源码阅读型」检查（不依赖 GPU，不需下载模型）：

```python
# 示例代码：仅做导入与签名检查，不真正创建引擎
import inspect
from lmdeploy import pipeline, Pipeline

# 1) 工厂函数就是一句话转发
print(inspect.getsource(pipeline).split('"""')[0])   # 只看签名部分

# 2) pipeline 的参数与 Pipeline.__init__ 完全一致
print('签名一致：',
      inspect.signature(pipeline) == inspect.signature(Pipeline.__init__))
```

**需要观察的现象**：第 2 步应打印 `签名一致： True`（去掉 `self` 后两者形参完全相同），证明工厂函数没有做任何额外处理。

**预期结果**：你确认 `pipeline(...)` ≡ `Pipeline(...)`，后续阅读可以直接聚焦 `Pipeline` 类。

#### 4.1.5 小练习与答案

**练习 1**：为什么 lmdeploy 同时提供 `pipeline()` 函数和 `Pipeline()` 类两种写法？只用其中一种行不行？

> **参考答案**：两者等价。提供函数形式是为了「短、好记、像一句话起服务」的用户体验（README 官方示例用它）；提供类形式是为了让高级用户能直接看到这是一个可被继承/扩展的对象。只用任何一种都完全可以，功能无差别。

**练习 2**：`api.py` 里 `serve()` 和 `client()` 现在的作用是什么？

> **参考答案**：它们已被废弃，函数体内只有 `raise NotImplementedError(...)`，仅保留签名用于给老代码一个「该改用了」的明确报错提示。应改用 CLI `lmdeploy serve api_server` 与 `from lmdeploy.serve import APIClient`。

---

### 4.2 `Pipeline` 类与后端自动选择

#### 4.2.1 概念说明

`Pipeline` 是「用户面 API 层」。它的职责有二：

1. **在初始化时**：决定走哪套引擎（PyTorch 还是 TurboMind），并把引擎实例拉起来。
2. **在推理时**：把用户的 prompt 包装成请求，喂给引擎，再把引擎产出的结果整理成 `Response` 还给用户。

本模块只讲第 1 个职责——**后端选择**，这是本讲实践任务的重点。第 2 个职责留到 4.3。

「后端选择」要解决的问题：当用户只给了一个 `model_path`（比如 `Qwen/Qwen2.5-7B-Instruct`），没说用哪套引擎时，lmdeploy 需要自动判断。判断依据只有两条：

- 这套模型 TurboMind 支不支持？
- TurboMind 编译扩展装没装好？

两个都满足 → TurboMind；否则 → PyTorch。

#### 4.2.2 核心流程

`Pipeline.__init__` 的后端选择分支可以用一段决策树概括：

```text
                  model_path 传入
                        │
            ┌───────────▼───────────┐
            │ 本地路径不存在？       │
            └───────┬───────────────┘
                是  │  get_model() 从 HF 下载
                    ▼
        autoget_backend_config(model_path, backend_config)
                    │
        ┌───────────▼───────────────────┐
        │ 用户已显式传 PytorchEngineConfig? │──是──▶ 强制 backend='pytorch'
        └───────────┬───────────────────┘
                  否 │
                    ▼
            autoget_backend(model_path):
              ┌─ TurboMind 支持且已安装 → 'turbomind'
              └─ 否则                   → 'pytorch'
                    │
                    ▼
        get_task(backend, model_path):
          ┌─ 多模态(VLM) → VLAsyncEngine
          └─ 纯文本      → AsyncEngine
                    │
                    ▼
        实例化 async_engine = pipeline_class(...)
        self.async_engine.start_loop(...)  # 启动内部事件循环线程
```

这里有个关键的「自动选择」可以形式化为一个简单逻辑表达式。设：

- \( S \)：TurboMind 支持该模型（`is_supported` 为真）
- \( I \)：TurboMind 扩展正确安装（未触发 `ImportError`）

则默认后端选择为：

\[
\text{backend} =
\begin{cases}
\text{turbomind}, & \text{若 } S \land I \\
\text{pytorch},   & \text{否则}
\end{cases}
\]

即只有当模型被 TurboMind 支持 **且** TurboMind 扩展装好时，才走 TurboMind；任一条件不满足都回退到 PyTorch。

#### 4.2.3 源码精读

整个 `Pipeline.__init__` 在 [lmdeploy/pipeline.py:35-90](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L35-L90)。其中与「默认后端选择」直接相关的就是这三步：

**第 1 步：本地路径不存在则下载模型** —— [pipeline.py:60-64](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L60-L64)

```python
# Download model if the path does not exist locally
if not os.path.exists(model_path):
    download_dir = backend_config.download_dir if backend_config else None
    revision = backend_config.revision if backend_config else None
    model_path = get_model(model_path, download_dir, revision)
```

这就是为什么你传一个 HF model id 也能跑——`get_model` 会从 HuggingFace 把模型拉到本地缓存，再把 `model_path` 替换为真实目录。

**第 2 步：自动选择后端与配置（本讲的核心所在行）** —— [pipeline.py:72-77](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L72-L77)

```python
# Create inference engine
backend, backend_config = autoget_backend_config(model_path, backend_config,
                                                 trust_remote_code=trust_remote_code)
_, pipeline_class = get_task(backend,
                             model_path,
                             trust_remote_code=trust_remote_code,
                             backend_config=backend_config)
```

这两行就是「默认后端选择逻辑」的位置。`autoget_backend_config` 返回 `(backend, backend_config)`，`get_task` 返回 `(task, pipeline_class)`——`pipeline_class` 是 `AsyncEngine`（纯文本）或 `VLAsyncEngine`（多模态）。

**第 3 步：实例化引擎并启动内部事件循环** —— [pipeline.py:78-90](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L78-L90)

```python
self.async_engine = pipeline_class(model_path, backend=backend, ...)
self.internal_thread = _EventLoopThread(daemon=True)
...
self.async_engine.start_loop(self.internal_thread.loop, use_async_api=False)
```

注意：`Pipeline` 内部其实是用一个**独立线程里的事件循环**（`_EventLoopThread`，定义在 [pipeline.py:415-472](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L415-L472)）来驱动异步引擎的。所以用户写的是同步代码 `pipe.infer(...)`，底层却跑在 asyncio 上——这是 4.3 推理接口能「并发处理批量请求」的基础。

---

那么 `autoget_backend_config` 内部到底怎么判定？看 [archs.py:56-92](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L56-L92)。它先做一次「短路」：如果用户已经显式传了 `PytorchEngineConfig`，就直接强制 PyTorch（[archs.py:74-75](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L74-L75)）；否则交给 `autoget_backend` 决定（[archs.py:77-78](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L77-L78)）。

`autoget_backend` 的判定核心在 [archs.py:52-53](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L52-L53)：

```python
backend = 'turbomind' if turbomind_has else 'pytorch'
return backend
```

其中 `turbomind_has` 来自 `is_supported_turbomind(model_path)`（[archs.py:36-37](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L36-L37)），并且只有当 `from lmdeploy.turbomind.supported_models import is_supported` 不抛 `ImportError` 时才有意义（也就是 TurboMind 扩展装好了）。如果连导入都失败，`is_turbomind_installed` 为假，`turbomind_has` 保持 `False`，自然回退 PyTorch，并打印一条 `Fallback to pytorch engine ...` 警告（[archs.py:46-50](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L46-L50)）。

> 结论一句话：**「默认后端选择逻辑」写在 `pipeline.py` 第 72–77 行，真正的判定函数是 `archs.py` 的 `autoget_backend`（第 52–53 行给出最终 `turbomind`/`pytorch`）。** 这正是实践任务要你定位的位置。

#### 4.2.4 代码实践

**实践目标**：定位「后端默认选择逻辑」所在行，并亲手触发一次自动选择（不真正加载权重）。

**操作步骤**：

```python
# 示例代码：只触发后端判定，不创建引擎（用 PytorchEngineConfig 强制短路，最省资源）
from lmdeploy.archs import autoget_backend, autoget_backend_config

# 1) 看清判定函数源码位置
import inspect
print(inspect.getsource(autoget_backend).split('Args:')[0])   # 关注最后的 return 行

# 2) 对一个本地 HF 模型目录调用自动后端判定（路径换成你本地的）
# backend = autoget_backend('/path/to/Qwen2.5-7B-Instruct')
# print('selected backend:', backend)
```

**需要观察的现象**：

- 第 1 步打印的源码里，你能清楚看到 `backend = 'turbomind' if turbomind_has else 'pytorch'` 这一行，以及它上面 `try/except ImportError` 的安装探测。
- 若 TurboMind 未装好，第 2 步（取消注释并传入真实路径后）会打印一条 `Fallback to pytorch engine ...` 警告，并返回 `'pytorch'`。

**预期结果**：你能在 `pipeline.py` 第 72 行找到 `autoget_backend_config` 的调用，并顺藤摸到 `archs.py` 第 52 行的最终判定。**是否真返回 `'turbomind'` 取决于本地是否编译了 TurboMind 及该模型是否在支持列表内——待本地验证。**

#### 4.2.5 小练习与答案

**练习 1**：如果用户既没传 `backend_config`，本地又没编译 TurboMind 扩展，`pipeline('Qwen/Qwen2.5-7B-Instruct')` 会走哪个引擎？为什么？

> **参考答案**：走 PyTorch 引擎。因为 `autoget_backend` 里 `from lmdeploy.turbomind.supported_models import is_supported` 会抛 `ImportError`，导致 `is_turbomind_installed=False`，于是 `turbomind_has` 保持 `False`，最终 `backend='pytorch'`，并打印一条回退警告。

**练习 2**：用户想无条件强制使用 PyTorch 引擎，最简单的写法是什么？

> **参考答案**：传入 `backend_config=PytorchEngineConfig()`。`autoget_backend_config` 在 [archs.py:74-75](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L74-L75) 看到 `isinstance(backend_config, PytorchEngineConfig)` 就直接 `return 'pytorch', backend_config`，连模型支持度都不再判断。

---

### 4.3 `infer` 与 `stream_infer`：两种推理接口

#### 4.3.1 概念说明

`Pipeline` 对外提供两种主要推理姿势：

| 方法 | 返回 | 适用场景 |
| --- | --- | --- |
| `pipe.infer(prompts)` 或 `pipe(prompts)` | `Response`（单条）或 `list[Response]`（批量） | 需要**一次性拿到完整结果** |
| `pipe.stream_infer(prompts)` | 一个迭代器，逐个 `yield Response` | 需要**边生成边输出**（打字机效果） |

此外还有：

- `pipe.chat(prompt, session=...)`：基于 `stream_infer` 实现，额外维护多轮对话的 session 历史（[pipeline.py:180-241](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L180-L241)）。本讲不展开，留给后续会话相关讲义。
- `pipe.batch_infer(...)`：**已废弃**，内部直接转调 `infer`（[pipeline.py:136-138](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L136-L138)），新代码请用 `infer`。

返回的 `Response`（[messages.py:536-565](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L536-L565)）是一个 dataclass，关键字段：

| 字段 | 含义 |
| --- | --- |
| `text` | 生成的文本 |
| `generate_token_len` | 生成的 token 数 |
| `input_token_len` | 输入 prompt 的 token 数（含 chat 模板部分） |
| `finish_reason` | 停止原因：`'stop'`（自然结束/命中停止词）或 `'length'`（达到 max_new_tokens） |
| `index` | 批量推理时该结果对应第几条 prompt |
| `token_ids` | 生成的 token id 列表 |

#### 4.3.2 核心流程

`infer` 的内部流程（同步外观，异步内核）：

```text
infer(prompts)
  │
  ├─ _is_single(prompts)           # 判断单条还是批量
  ├─ MultimodalProcessor.format_prompts(prompts)   # 统一成 list[list[dict]]
  ├─ _request_generator(...)       # 每条 prompt → 一个请求 dict
  │
  └─ _infer(requests, multiplex=False)   # 关键：提交到内部事件循环
        │
        │  在 _EventLoopThread 的事件循环里：
        ├─ 对每个请求 self.async_engine.generate(**req)  ← 真正的异步生成
        ├─ _sync_resp 把异步产出塞进一个 Queue
        └─ 主线程 iter(que.get, None) 同步取出
  │
  ▼
单条 → outputs[0]（Response）；批量 → outputs（list[Response]）
```

`stream_infer` 与 `infer` 的唯一结构差异是 `_infer` 的 `multiplex` 参数：

- `infer` 用 `multiplex=False`：每个请求独占一个结果队列，等它跑完再整体返回。
- `stream_infer` 用 `multiplex=True`（[pipeline.py:173](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L173)）：所有请求的增量结果混在同一个队列里，**来一个吐一个**，每条 `Response` 带 `index` 标明它属于哪个请求。

**流式输出的关键直觉**：`stream_infer` 每次 `yield` 的 `Response`，其 `.text` 是**这一步新产生的增量片段**，而不是从头累计的全文。所以官方文档与测试里都写成「把每段 `.text` 拼起来」：

```python
chunks = []
for chunk in pipe.stream_infer(prompt):
    chunks.append(chunk.text)
full_text = ''.join(chunks)
```

#### 4.3.3 源码精读

**`infer` 主体** —— [pipeline.py:92-134](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L92-L134)，核心三段：

[pipeline.py:113-115](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L113-L115) —— 判断单条/批量并把 prompts 规整成统一格式：

```python
is_single = self._is_single(prompts)
# format prompts to openai message format, which is a list of dicts
prompts = MultimodalProcessor.format_prompts(prompts)
```

[pipeline.py:119-129](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L119-L129) —— 生成请求、提交推理、把增量结果累计成完整 `Response`：

```python
requests = self._request_generator(prompts, ..., stream_response=False, **kwargs)
for g in self._infer(requests, multiplex=False, pbar=pbar):
    res = None
    for out in g:
        res = res.extend(out) if res else out   # 用 Response.extend 拼接增量
    outputs.append(res)
```

注意 `res.extend(out)` 调用的是 `Response.extend`（[messages.py:597](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L597)），它会把多段 `text`/`token_ids` 拼起来——这正是「非流式 = 把流式片段累计」的实现方式。

[pipeline.py:132-134](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L132-L134) —— 单条输入返回单个 `Response`，否则返回列表：

```python
if is_single:
    return outputs[0]
return outputs
```

**`stream_infer` 主体** —— [pipeline.py:140-173](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L140-L173)，结构与 `infer` 几乎一致，差别只在最后用 `multiplex=True`：

[pipeline.py:165-173](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L165-L173)：

```python
prompts = MultimodalProcessor.format_prompts(prompts)
requests = self._request_generator(prompts, sessions=sessions, ...,
                                   stream_response=stream_response, **kwargs)
return self._infer(requests, multiplex=True)
```

**`_infer` 与内部事件循环的桥接** —— [pipeline.py:365-401](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L365-L401)。这是「同步外观、异步内核」的关键：协程 `_infer()` 被 `asyncio.run_coroutine_threadsafe(...)` 提交到 `self.internal_thread.loop`（[pipeline.py:398-399](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L398-L399)），主线程则通过 `iter(que.get, None)`（[pipeline.py:401](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L401)）阻塞地取结果。线程间通信用标准库 `queue.Queue`。

**`__call__` 与上下文管理器** —— [pipeline.py:296-306](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L296-L306)：

```python
def __call__(self, prompts, gen_config=None, **kwargs):
    return self.infer(prompts, gen_config=gen_config, **kwargs)

def __enter__(self):
    return self

def __exit__(self, exc_type, exc_value, traceback):
    self.close()
```

这解释了 README 的写法：`with lmdeploy.pipeline(...) as pipe: response = pipe([...])`。`with` 退出时自动调 `close()`，干净地停止内部事件循环线程并释放引擎资源（`close` 见 [pipeline.py:175-178](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L175-L178)）。

**用法依据（来自仓库测试与文档）**：

- 单条非流式返回单个 `Response`：[tests/test_lmdeploy/test_pipeline.py:39-48](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/tests/test_lmdeploy/test_pipeline.py#L39-L48) 中 `response = pipe.infer(prompt)` 后断言 `isinstance(response, Response)`。
- 批量非流式返回 `list[Response]`：[test_pipeline.py:50-59](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/tests/test_lmdeploy/test_pipeline.py#L50-L59)。
- 流式逐段返回 `Response`：[test_pipeline.py:93-101](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/tests/test_lmdeploy/test_pipeline.py#L93-L101)，以及文档 [docs/zh_cn/advance/chat_template.md:83-84](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/docs/zh_cn/advance/chat_template.md#L83-L84) 的 `for response in pipe.stream_infer(messages): print(response.text, end='')`。

#### 4.3.4 代码实践

**实践目标**：用同一个 prompt 分别跑非流式与流式，观察 `Response` 字段与流式增量。

**操作步骤**（需 GPU 与已下载的模型；若本地无 GPU 则转为「阅读型实践」，见下方说明）：

```python
# 示例代码：非流式 + 流式各一次
from lmdeploy import pipeline, GenerationConfig

pipe = pipeline('Qwen/Qwen2.5-7B-Instruct')
prompt = '用一句话介绍 lmdeploy。'

# 1) 非流式：一次性拿到完整结果
resp = pipe(prompt, gen_config=GenerationConfig(max_new_tokens=64))
print('【非流式】', resp.text)
print('generate_token_len =', resp.generate_token_len,
      '| input_token_len =', resp.input_token_len,
      '| finish_reason =', resp.finish_reason)

# 2) 流式：逐段打印（打字机效果）
print('【流式】', end='', flush=True)
for chunk in pipe.stream_infer(prompt, gen_config=GenerationConfig(max_new_tokens=64)):
    print(chunk.text, end='', flush=True)   # 每个 chunk.text 是增量片段
print()

pipe.close()
```

**需要观察的现象**：

- 非流式：`print` 一次性打印完整句子；`generate_token_len` 约等于生成 token 数（≤ 64）。
- 流式：终端像打字机一样逐字蹦出；每个 `chunk.text` 通常只有一两个词。把所有 `chunk.text` 拼起来，应与非流式结果内容基本一致。

**预期结果**：你能清楚看到两种接口的「整段返回」vs「逐段返回」差异，并能从 `resp.finish_reason` 判断是自然结束（`'stop'`）还是被长度截断（`'length'`）。**具体生成内容与 token 数依赖本地模型与随机性——待本地验证。**

> 无 GPU 的「阅读型实践」替代方案：打开 [test_pipeline.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/tests/test_lmdeploy/test_pipeline.py)，对照 `test_infer_single_string`（第 39 行）与 `test_stream_infer_single`（第 93 行）的断言，说清楚「单条 `infer` 返回单个 `Response`」「`stream_infer` 迭代出的每个元素都是 `Response`」这两条行为契约即可。

#### 4.3.5 小练习与答案

**练习 1**：`pipe(prompts)` 和 `pipe.infer(prompts)` 有没有区别？批量输入时返回类型是什么？

> **参考答案**：没有区别。`__call__` 直接 `return self.infer(...)`（[pipeline.py:296-300](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L296-L300)）。批量输入（如 `['p1','p2']`）返回 `list[Response]`，单条输入返回单个 `Response`，由 `_is_single` 判定（[pipeline.py:317-321](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L317-L321)）并在 `infer` 末尾处理（[pipeline.py:132-134](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L132-L134)）。

**练习 2**：为什么 `stream_infer` 的循环里要写 `print(chunk.text, end='')`，而不是 `print(chunk.text)`？

> **参考答案**：因为每次 `yield` 的 `Response.text` 是**增量片段**，不是累计全文。用 `end=''` 不换行地把片段首尾相接，才能拼出完整、连续的句子；若每段都换行打印，输出会被切成很多碎行。要拿完整文本，应像 [test_mtp_guided_decoding.py:271-274](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/tests/test_lmdeploy/test_mtp_guided_decoding.py#L271-L274) 那样 `''.join(chunks)`。

**练习 3**：`batch_infer` 还能用吗？推荐用什么替代？

> **参考答案**：已被 `@deprecated` 标记，内部只是 `return self.infer(*args, **kwargs)`（[pipeline.py:136-138](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L136-L138)）。功能仍可用但会触发废弃警告，新代码请直接用 `infer`。

---

## 5. 综合实践

**任务**：编写一个脚本，用 `pipeline('Qwen/Qwen2.5-7B-Instruct')` 完成非流式与流式各一次推理，并打印每个增量片段；同时在脚本里**用代码自动定位「后端默认选择逻辑」的源码行号**。

**操作步骤**：

1. 准备环境（参考 U1-l3）：`pip install lmdeploy`，确保有可用的 GPU 与网络（首次会从 HuggingFace 下载模型；可用 `export LMDEPLOY_USE_MODELSCOPE=True` 改走 ModelScope）。
2. 新建 `quickstart.py`，写入下面的综合脚本：

```python
# 示例代码：综合实践
import inspect

from lmdeploy import pipeline, GenerationConfig
from lmdeploy.pipeline import Pipeline
from lmdeploy.archs import autoget_backend_config

MODEL = 'Qwen/Qwen2.5-7B-Instruct'
PROMPT = '用三句话向初学者解释什么是 Paged Attention。'

# ── 任务 A：自动定位「后端默认选择逻辑」所在行 ──
src = inspect.getsource(Pipeline.__init__)
for i, line in enumerate(src.splitlines(), start=1):
    if 'autoget_backend_config' in line:
        print(f'[定位] Pipeline.__init__ 里出现 autoget_backend_config 的源码行：\n    {line.strip()}')
        break
print('[定位] 最终 backend 判定在 archs.autoget_backend，可打印其源码确认：')
print(inspect.getsource(autoget_backend_config).split('Args:')[0].strip())

# ── 任务 B：非流式推理 ──
pipe = pipeline(MODEL)
resp = pipe(PROMPT, gen_config=GenerationConfig(max_new_tokens=128, do_sample=True, temperature=0.6))
print('\n[非流式] finish_reason =', resp.finish_reason,
      '| generate_token_len =', resp.generate_token_len,
      '| input_token_len =', resp.input_token_len)
print('[非流式] 正文：', resp.text)

# ── 任务 C：流式推理，逐段打印 ──
print('\n[流式] 正文：', end='', flush=True)
chunk_count = 0
for chunk in pipe.stream_infer(PROMPT, gen_config=GenerationConfig(max_new_tokens=128)):
    chunk_count += 1
    print(chunk.text, end='', flush=True)
print(f'\n[流式] 共收到 {chunk_count} 个增量片段')

pipe.close()
```

3. 运行 `python quickstart.py`。

**需要观察的现象**：

- 任务 A：脚本能打印出 `Pipeline.__init__` 中调用 `autoget_backend_config(...)` 的那一行，确认它对应源码 [pipeline.py:72](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L72)。
- 任务 B：一次性输出完整三句话，`finish_reason` 多半为 `'stop'`。
- 任务 C：打字机式逐字输出，`chunk_count` 明显大于 1。

**预期结果**：你既跑通了两种推理姿势，又亲手验证了「默认后端选择逻辑写在 `Pipeline.__init__` 第 72 行、最终判定在 `archs.autoget_backend` 第 52 行」。**运行耗时、生成内容、片段数量依赖本地硬件与模型——待本地验证。**

> 若本地无 GPU，可把任务 B/C 注释掉，只保留任务 A（纯静态源码分析，无需加载模型），同样能完成「定位后端选择逻辑」的学习目标。

## 6. 本讲小结

- `lmdeploy.pipeline(...)` 是个薄工厂函数，把参数原样转交给 `Pipeline`（[api.py:72-79](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/api.py#L72-L79)）；同文件 `serve`/`client` 已废弃，调用即报错。
- **默认后端选择逻辑写在 `Pipeline.__init__` 第 72–77 行**，调用 `autoget_backend_config` + `get_task`；最终 `turbomind`/`pytorch` 判定在 `archs.autoget_backend` 第 52 行，规则是「TurboMind 支持 且 已安装」。
- 传 `PytorchEngineConfig()` 可短路强制走 PyTorch；不传则自动判断（[archs.py:74-75](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L74-L75)）。
- `Pipeline` 是「同步外观 + 异步内核」：用户写同步 `infer`，底层靠 `_EventLoopThread` 的事件循环驱动 `async_engine.generate`，线程间用 `queue.Queue` 传结果。
- 两种推理姿势：`infer`/`__call__` 一次性返回 `Response` 或 `list[Response]`；`stream_infer` 逐段 `yield Response`，每段 `.text` 是增量片段，需拼接。`batch_infer` 已废弃，用 `infer` 替代。
- 推荐用 `with lmdeploy.pipeline(...) as pipe:` 上下文管理，退出时自动 `close()` 释放引擎与线程。

## 7. 下一步学习建议

本讲你只接触了 `Pipeline` 的「用户面」。接下来建议按依赖顺序深入：

1. **U2-l1 核心消息与响应类型 `messages.py`**：本讲只用了 `Response` 和 `GenerationConfig` 两个字段，U2 会把 `MessageStatus`、`EngineEvent`、`SchedulerSequence` 等贯穿全项目的类型一次讲清。
2. **U2-l3 引擎配置**：本讲 `backend_config` 一直默认 `None`，U2-l3 会讲透 `PytorchEngineConfig`/`TurbomindEngineConfig` 的 `tp`、`cache_max_entry_count`、`session_len` 等字段。
3. **U3-l1 Pipeline 如何选择并实例化后端**：本讲的 4.2 是「概览」，U3-l1 会完整追踪 `Pipeline → Engine → create_instance` 的调用链，进入引擎内部。
4. **若你更关心服务化**：可跳到 U8 的 `lmdeploy serve api_server`，把本讲的离线推理变成一个 OpenAI 兼容的 HTTP 服务。
