# 目录结构与代码组织

## 1. 本讲目标

上一篇（u1-l1）我们明白了 SGLang-Omni 「是什么」——一个面向 omni / 语音 / TTS 模型的多阶段推理运行时，主链路是：

```text
HTTP API -> Client -> Coordinator -> Stage -> Scheduler -> ModelRunner -> model forward
```

本讲要回答的问题是：**这七个抽象层，在磁盘上分别住在哪个目录里？我打开仓库后，该去哪里找它？**

学完本讲，你应该能够：

1. 读懂 `docs/developer_reference/main.md` 中的 Directory Layout，并把每个子目录对应到主链路的某一层。
2. 看懂 `sglang_omni/__init__.py` 的「惰性导出（lazy export）」机制，知道为什么 `import sglang_omni` 不会立刻加载全部重量级依赖。
3. 掌握「模型目录约定」，知道一个新模型家族的代码该放在 `sglang_omni/models/<model>/` 下的哪个文件里。
4. 当同事说「控制平面消息出了点问题」时，你能立刻定位到正确的子目录，而不是在仓库里乱翻。

## 2. 前置知识

本讲假设你已经读过 u1-l1，了解下面两个概念：

- **多阶段运行时（multi-stage runtime）**：一次生成被拆成 preprocessing / encoders / AR engines / talkers / decoders / vocoder / aggregators 等异构阶段接力完成。
- **分层主链路**：HTTP → Client → Coordinator → Stage → Scheduler → ModelRunner → model forward，每一层只负责一件事。

另外，你需要知道一个 Python 工程的小常识：一个包（package）就是一个含 `__init__.py` 的目录。SGLang-Omni 把它自己发布为一个名为 `sglang_omni` 的 Python 包，**所有运行时代码都在 `sglang_omni/` 这一个顶层目录下**。这点很重要：仓库根目录下虽然还有 `docs/`、`docker/`、`benchmarks/`、`examples/` 等，但真正「跑起来」的代码只有 `sglang_omni/`（以及一个旁路的 `sglang_omni_router/`，我们放在 u7 再讲）。

## 3. 本讲源码地图

本讲涉及的关键文件很少，因为讲的就是「地图」本身：

| 文件 | 作用 |
| --- | --- |
| [sglang_omni/__init__.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/__init__.py) | 包的入口。用惰性导出把公开符号映射到它们真正所在的子模块。 |
| [docs/developer_reference/main.md](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/main.md) | 架构总览文档。包含 System Overview 表、Directory Layout、Model Directory Convention 三节。 |
| `sglang_omni/` 下各子目录 | 本讲的核心「观察对象」，我们会在第 4 节逐个走访。 |

> 提示：本讲大量内容是「读目录」和「读文档」，而不是「读函数实现」。这是故意的——在深入任何一层的源码之前，先建立一张全局地图，后续每一篇讲义才不会迷路。

---

## 4. 核心概念与源码讲解

本讲的三个最小模块：

1. **包结构**：`sglang_omni` 包如何组织，惰性导出如何工作。
2. **子包职责**：每个子目录对应主链路的哪一层。
3. **模型目录约定**：`models/<model>/` 下文件该怎么放。

### 4.1 包结构与惰性导出

#### 4.1.1 概念说明

很多大型 Python 包都会遇到一个矛盾：用户想 `import sglang_omni` 就能用 `Client`、`Coordinator` 这些高层符号；但包内部又依赖 torch、CUDA、各种调度器，这些导入很慢、很重，甚至在没有 GPU 的机器上会直接报错。

SGLang-Omni 的解法是 **惰性导出（lazy export）**：

- `__init__.py` 里**不**直接 `from ... import ...`，而是只维护一张「名字 → (模块路径, 属性名)」的映射表。
- 当用户**第一次**访问某个名字时（例如 `sglang_omni.Client`），Python 才会去真正 import 那个子模块，并把结果缓存起来。
- 如果用户从头到尾没用到某个符号，那个重量级子模块就永远不会被加载。

这样一来，`import sglang_omni` 本身是廉价的；只有你真正用到的部分才会付出加载代价。

#### 4.1.2 核心流程

惰性导出的核心是 PEP 562 引入的模块级 `__getattr__`。流程如下：

```text
用户写 sglang_omni.Client
        │
        ▼
Python 在 __all__ / 命名空间里找不到 Client
        │
        ▼
触发模块级 __getattr__("Client")
        │
        ▼
查 _EXPORTS["Client"]  →  ("sglang_omni.client.client", "Client")
        │
        ▼
import_module("sglang_omni.client.client")   # 这一刻才真正加载
        │
        ▼
getattr(模块, "Client") 取出符号
        │
        ▼
globals()["Client"] = 结果   # 缓存，下次直接命中，不再 import
```

关键不变量：**第一次访问要付 import 成本，之后访问命中 `globals()` 缓存，零成本。**

#### 4.1.3 源码精读

包入口的文件头说明了这个设计意图——保持顶层导入轻量，让调用方能单独 import 子包而不立刻拖入整个 pipeline 运行时：

[sglang_omni/__init__.py:2-7](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/__init__.py#L2-L7) —— 这段 docstring 是惰性导出的「设计合同」，中文翻译就是：不要在顶层 `__init__.py` 里放重导入。

接着是导出映射表 `_EXPORTS`，它是一张字典，键是公开名字，值是 `(模块路径, 属性名)` 二元组。注意它按子包分组，正好对应主链路的几个层：

[sglang_omni/__init__.py:15-38](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/__init__.py#L15-L38) —— 例如 `"Client": ("sglang_omni.client.client", "Client")`、`"Coordinator": ("sglang_omni.pipeline.coordinator", "Coordinator")`、`"Stage": ("sglang_omni.pipeline.stage.runtime", "Stage")`。这张表本身就是一张「公开 API → 源码位置」的索引，**遇到不认识的符号，先来这里查它住在哪个子模块**。

`__all__` 由 `__version__` 加上所有键构成：

[sglang_omni/__init__.py:40-40](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/__init__.py#L40-L40) —— 决定了 `from sglang_omni import *` 会导出哪些名字。

真正的惰性加载逻辑在模块级 `__getattr__`：

[sglang_omni/__init__.py:43-51](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/__init__.py#L43-L51) —— 它做三件事：①查 `_EXPORTS`，查不到就抛 `AttributeError`（带友好提示）；②`import_module` 真正加载子模块并取属性；③`globals()[name] = value` 把结果写回模块命名空间，实现「首次加载、后续命中缓存」。

#### 4.1.4 代码实践

**实践目标**：亲手验证惰性导出的「按需加载」行为。

**操作步骤**（在装好包的环境里，参见 u1-l2）：

1. 进入 Python 解释器。
2. 先只 `import sglang_omni`，**不**访问任何子符号。
3. 用 `import sys` 查看 `sys.modules` 里有没有 `sglang_omni.client.client`。
4. 接着访问 `sglang_omni.Client`。
5. 再次查看 `sys.modules`。

**预期现象 / 预期结果**：

- 第 3 步：`sglang_omni.client.client` 应该**不在** `sys.modules` 里（还没被加载）。
- 第 4 步：访问 `sglang_omni.Client` 后，会触发一次 import。
- 第 5 步：此时 `sys.modules` 里**出现** `sglang_omni.client.client`，证明它是被「第一次访问」时才加载的。

一个最小验证脚本（**示例代码**，非仓库原有）：

```python
# verify_lazy.py
import sys
import sglang_omni

before = "sglang_omni.client.client" in sys.modules
_ = sglang_omni.Client          # 触发惰性加载
after = "sglang_omni.client.client" in sys.modules
print(f"访问前已加载: {before}")   # 预期 False
print(f"访问后已加载: {after}")    # 预期 True
```

> 如果当前环境没有 GPU / 没装齐重型依赖，访问某个符号可能会在 `import_module` 阶段报错——这恰好反过来说明惰性导出的价值：你不碰它，它就不会炸。运行结果请以本地为准（**待本地验证**）。

#### 4.1.5 小练习与答案

**练习 1**：为什么不在 `__init__.py` 里直接写 `from sglang_omni.pipeline.coordinator import Coordinator`？

**参考答案**：那样会让任何 `import sglang_omni` 都立刻拖入 `pipeline.coordinator` 及其全部传递依赖（torch、调度器、relay 后端等），既慢又可能在无 GPU 机器上直接 import 失败。惰性导出把加载推迟到「真正用到」的那一刻。

**练习 2**：如果我想知道 `AggregatedInput` 这个公开符号定义在哪个文件，最快的办法是什么？

**参考答案**：直接查 `__init__.py` 里的 `_EXPORTS` 字典。`_EXPORTS["AggregatedInput"]` 的值是 `("sglang_omni.pipeline.stage.input", "AggregatedInput")`，所以它定义在 `sglang_omni/pipeline/stage/input.py`。

---

### 4.2 子包职责：把目录对应到主链路

#### 4.2.1 概念说明

`docs/developer_reference/main.md` 给出了一张精简的 Directory Layout，把 `sglang_omni/` 划成 9 个核心子目录，每个目录对应主链路的一个或几个职责。但**真实的仓库比文档列的更丰富**：除了文档里的 9 个，还有 `cli/`、`comm/`、`preprocessing/`、`profiler/`、`sampling/`、`http/`、`utils/`、`vendor/` 等若干子包，以及一个顶层模块 `quantization.py`。

这很正常——文档给的是「概念地图」，仓库是「真实地形」。本节我们既讲文档的精简版，也补上真实地形，让你拿到的是一张能对得上号的地图。

#### 4.2.2 核心流程：两层视图

我们先看文档给出的「概念层 → 目录」对应关系，这是骨架：

| 主链路层 / 职责 | 对应目录 | 说明 |
| --- | --- | --- |
| HTTP API、OpenAI 兼容接口 | `serve/` | FastAPI 路由、SSE 流式 |
| 内部 Client | `client/` | GenerateRequest↔OmniRequest 转换、结果聚合 |
| Coordinator + Stage | `pipeline/` | 跨阶段编排、阶段生命周期、多进程 |
| Scheduler | `scheduling/` | 每阶段执行循环、inbox/outbox 消息 |
| ModelRunner | `model_runner/` | AR 阶段共享的前向抽象 |
| 模型族代码 | `models/` | 各模型 config / stages / 模块 |
| 配置 | `config/` | PipelineConfig / StageConfig / topology |
| 数据传输后端 | `relay/` | cuda_ipc / shm / nccl / nixl / mooncake |
| 控制平面消息类型 | `proto/` | request / payload / stage / 消息 dataclass |

然后看「真实地形」补全（文档没列出，但仓库里有）：

| 目录 | 职责（按文件名推断 + 文档佐证） |
| --- | --- |
| `cli/` | `sgl-omni` 命令行入口，含 `serve.py`、`config.py`、`__main__.py`（u1-l2 / u1-l4） |
| `comm/` | 通信路由层：`router.py` / `engine.py` / `stage_io.py` / `data_ref.py`，决定一条阶段边用哪种传输（u6-l1） |
| `preprocessing/` | 多模态输入规范化：`audio.py` / `image.py` / `video.py` / `text.py` / `resource_connector.py` / `cache_key.py` / `base.py`（u5-l4） |
| `profiler/` | 请求级事件记录与 torch profiler：`event_recorder.py` / `views.py` / `torch_profiler.py`（u6-l3） |
| `sampling/` | 采样相关工具，如 `seed.py` |
| `http/` | HTTP 侧辅助：`admin_auth.py`（admin 鉴权）、`favicon.py` |
| `utils/` | 通用工具：`gpu_memory.py`、`hf.py`、`checkpoint.py`、`audio.py`、`imports.py` 等 |
| `vendor/` | 第三方 vendored 代码 |
| `quantization.py` | 顶层模块：量化策略选择（u6-l2） |

> 一个容易混淆的点：`comm/`、`relay/`、`proto/` 都和「阶段间通信」有关，但分工不同。简单记：**`proto/` 定义消息长什么样，`relay/` 是搬数据的底层后端，`comm/` 是决定「这条边用哪个后端」的路由层**。`pipeline/control_plane.py` 则是搬运这些控制消息的 ZMQ 通道（后续 u3 详讲）。

#### 4.2.3 源码精读

文档的 Directory Layout 原文如下，这是最权威的「骨架」描述：

[docs/developer_reference/main.md:27-40](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/main.md#L27-L40) —— 注意它列了 9 个目录，并各自用一句话点明职责，例如 `relay/  # Data transfer backends`、`proto/  # Request, payload, stage, and control-plane message types`。

主链路表则把目录和「职责层」连起来：

[docs/developer_reference/main.md:14-25](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/main.md#L14-L25) —— 例如 Stage 这一行写着 "Control-plane IO, relay IO, fan-in, stream routing, scheduler inbox/outbox bridging"，对应 `pipeline/` 下的 stage 代码。

为了让你对「控制平面消息」和「数据传输后端」有具象证据，各看一处真实代码。

控制平面消息——`proto/messages.py` 里用 `@dataclass` 定义了一批消息类型，例如 `DataReadyMessage`：

[sglang_omni/proto/messages.py:13-14](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/proto/messages.py#L13-L14) —— 这是「数据已就绪」控制消息的起点。同文件里还有 `SubmitMessage`、`CompleteMessage`、`StreamMessage`、`AbortMessage`、`ShutdownMessage` 等，构成控制平面的全部消息种类。

数据传输后端——`relay/base.py` 维护了一个全局注册表，把后端名字映射到实现类：

[sglang_omni/relay/base.py:11-12](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/base.py#L11-L12) —— `RELAY_REGISTRY` 是 `Dict[str, Type[Relay]]`，每个后端（nccl / nixl / mooncake / cuda_ipc / shm）注册进来。`relay/__init__.py` 对外暴露的也是 `Relay`、`NixlRelay`、`MooncakeRelay` 这些具体后端，正好印证「`relay/` = 数据传输后端」。

#### 4.2.4 代码实践

这正是本讲指定的实践任务：**在 `sglang_omni/` 下找出「控制平面消息」与「数据传输后端」分别对应哪两个子目录，并各举一个真实文件名作为证据。**

**操作步骤**：

1. 在仓库根目录列出子目录：`ls sglang_omni/`（或用编辑器展开该目录）。
2. 对照本节的「概念层 → 目录」表，定位两个目标目录。
3. 进入这两个目录，各挑一个文件作为证据。

**需要观察的现象 / 预期结果**：

- **控制平面消息 → `proto/` 目录**。证据文件：`sglang_omni/proto/messages.py`（里面定义 `DataReadyMessage`、`SubmitMessage`、`AbortMessage` 等控制消息类型）。
- **数据传输后端 → `relay/` 目录**。证据文件：`sglang_omni/relay/cuda_ipc.py`（或 `relay/nixl.py`、`relay/mooncake.py`、`relay/shm.py`、`relay/nccl.py` 任选其一，都是具体后端实现；`relay/base.py` 是它们的公共基类与注册表）。

> 自查：如果你选了 `comm/`，说明你把「路由层」当成了「后端」。记住——`comm/` 是决定用哪个后端的路由，`relay/` 才是后端本身。控制消息的**类型**住在 `proto/`，搬运它们的 **ZMQ 通道**住在 `pipeline/control_plane.py`（u3 详讲）。

#### 4.2.5 小练习与答案

**练习 1**：同事说「我想改一下 OpenAI 兼容的 `/v1/chat/completions` 路由」，应该去哪个目录？

**参考答案**：去 `sglang_omni/serve/`。HTTP API 与 OpenAI 兼容适配都住在 `serve/`，路由通常在 `serve/openai_api.py`（u2-l2 详讲）。

**练习 2**：`comm/` 和 `relay/` 有什么区别？用一句话说清。

**参考答案**：`relay/` 是「具体的数据传输后端」（cuda_ipc / nixl / mooncake …），`comm/` 是「为每条阶段边选择并使用哪个后端」的路由与打包解包层（`router.py` / `engine.py` / `stage_io.py`）。前者是零件，后者是装配。

**练习 3**：文档的 Directory Layout 没有列出 `profiler/`，但这不影响它存在。这说明文档和仓库是什么关系？

**参考答案**：文档给的是「概念骨架」（核心 9 个目录），仓库是「完整地形」。看骨架建立心智模型，看地形找具体文件；两者冲突时以仓库实际文件为准，并顺手补一条文档 issue。

---

### 4.3 模型目录约定

#### 4.3.1 概念说明

SGLang-Omni 区分「框架层」和「模型层」：

- **框架层**：`Stage`、`Coordinator`、各种 scheduler、`model_runner` 基类、relay、runtime 准备逻辑等。这些是所有模型共用的「脚手架」，不应该被某个具体模型污染。
- **模型层**：某个具体模型家族（如 Qwen3-Omni、Qwen3-TTS、Higgs Audio）专属的配置、阶段工厂、请求构造、网络模块、处理器、vocoder 等。

约定很明确：**只有模型专属的代码才进 `sglang_omni/models/<model>/`，框架逻辑留在各自的框架目录里。** 这样接入新模型时，你只在 `models/` 下加东西，几乎不动框架。

#### 4.3.2 核心流程：一个模型目录的标准布局

文档给出的推荐布局（“应当长这样”）：

```text
models/<model>/
|-- config.py             # PipelineConfig 子类 + StageConfig 列表
|-- stages.py             # 阶段工厂（构造各 stage）
|-- routing.py            # 可选：数据驱动的路由辅助
|-- request_builders.py   # 阶段间 payload 转换
|-- payload_types.py      # 模型专属、有类型的 payload 状态
|-- callbacks.py          # 可选：反馈回调或策略
`-- components/           # 模型模块、处理器、vocoder、adapter
```

注意 `config.py` 和 `stages.py` 几乎是「必选项」——它们定义了这个模型的「管线拓扑」和「阶段如何被构造」。其余文件按需出现。

真实仓库里，`models/` 下已有十几个模型家族（`qwen3_omni`、`qwen3_tts`、`qwen3_asr`、`higgs_tts`、`fun_asr`、`whisper_asr`、`moss_transcribe_diarize` 等），外加两个框架级文件：

- `sglang_omni/models/registry.py` —— 模型注册表，自动发现各 `<model>/`（u5-l1）。
- `sglang_omni/models/model_capabilities.py` —— 模型能力声明（u5-l1）。
- `sglang_omni/models/weight_loader.py` —— 顶层权值加载辅助。

#### 4.3.3 源码精读

文档的 Model Directory Convention 原文：

[docs/developer_reference/main.md:42-57](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/main.md#L42-L57) —— 它明确写出推荐布局，并在结尾强调："Only model-local behavior belongs here."（只有模型局部行为才放这里），框架层仍由 `Stage`、`Coordinator`、scheduler、model-runner 基类等拥有。

我们拿真实的 `qwen3_omni` 目录来对照（这是 u1-l1 提到的 omni 代表模型）。它的实际文件有：

```text
models/qwen3_omni/
|-- config.py              # 对应推荐布局的 config.py
|-- stages.py              # 对应 stages.py
|-- request_builders.py    # 对应 request_builders.py
|-- payload_types.py       # 对应 payload_types.py
|-- components/            # 对应 components/（thinker / talker / 编码器 / code2wav 等）
|-- bootstrap.py           # 模型专属的启动钩子
|-- placement.py           # 模型专属放置规则
|-- talker_model_runner.py # talker 阶段专属 runner
|-- talker_scheduler.py    # talker 阶段专属 scheduler
|-- merge.py, hf_config.py, pending_text_queue.py ...
```

可以看到：**推荐布局里的核心文件（config / stages / request_builders / payload_types / components）确实都在**；同时真实模型会按需多出 `bootstrap.py`、`placement.py`、`talker_scheduler.py` 这类模型专属文件——这正说明约定是「骨架 + 按需扩展」，不是死板清单。

> `config.py` 和 `stages.py` 里的具体内容（如 `architecture` 声明、stage 列表）属于 u5 的范围，本讲只需记住：「想看 Qwen3-Omni 的拓扑定义，去 `models/qwen3_omni/config.py`；想看各 stage 怎么构造，去 `stages.py`」。

#### 4.3.4 代码实践

**实践目标**：用「模型目录约定」去定位一个真实模型的某个文件，验证约定的可预测性。

**操作步骤**：

1. 打开 `sglang_omni/models/qwen3_omni/`。
2. 根据约定回答：这个模型的「阶段工厂」在哪个文件？「阶段间 payload 转换」在哪个文件？「模型模块（如 thinker / talker / image_encoder）」在哪个子目录？
3. 进入 `components/`，确认它确实放着 `thinker.py`、`talker.py`、`image_encoder.py`、`audio_encoder.py` 这类模块。

**预期结果**：

- 阶段工厂 → `stages.py`。
- 阶段间 payload 转换 → `request_builders.py`。
- 模型模块 → `components/` 子目录，里面是 `thinker.py` / `talker.py` / `image_encoder.py` / `audio_encoder.py` / `code2wav_scheduler.py` / `streaming_detokenizer.py` 等（**待本地用编辑器或 `ls` 确认完整清单**）。

> 这一步的价值在于：一旦你记住约定，以后面对任何一个陌生的 `models/<model>/`，都能不看 README 就猜对 80% 的文件位置。

#### 4.3.5 小练习与答案

**练习 1**：假设你要给一个新 TTS 模型写「流水线拓扑定义」，应该新建哪个文件？放在哪？

**参考答案**：`sglang_omni/models/<新模型名>/config.py`。`config.py` 负责定义该模型的 `PipelineConfig` 子类和 `StageConfig` 列表。

**练习 2**：thinker 的网络前向实现，属于「框架层」还是「模型层」？应该放在哪？

**参考答案**：属于「模型层」。具体网络模块放在 `sglang_omni/models/<model>/components/`（例如 `qwen3_omni/components/thinker.py`）。框架层只提供共用的 `model_runner` 基类，不包含任何具体模型的前向实现。

**练习 3**：为什么 `registry.py` 和 `model_capabilities.py` 放在 `models/` 顶层，而不是某个 `<model>/` 里？

**参考答案**：因为它们是**跨所有模型**的框架级机制——注册表负责自动发现所有 `<model>/` 子目录，能力声明是统一的静态描述格式。它们服务于「所有模型」，不属于任何单个模型，所以放在 `models/` 顶层而非某个模型子目录下。

---

## 5. 综合实践

把本讲三块知识串起来，完成一个「从公开 API 到源码落点」的寻路任务。

**任务**：选定 `__init__.py` 里导出的某个名字，把它一路追到磁盘上的源码文件，再判断它属于「框架层」还是「模型层」。

**操作步骤**：

1. 在 [sglang_omni/__init__.py:15-38](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/__init__.py#L15-L38) 的 `_EXPORTS` 里挑一个名字，例如 `Coordinator` 或 `Stage`。
2. 读出它的 `(模块路径, 属性名)`，定位到真实文件（如 `Coordinator` → `sglang_omni/pipeline/coordinator.py`）。
3. 用本节的「概念层 → 目录」表，判断这个文件属于主链路的哪一层（Coordinator 属于 `pipeline/`，即 Coordinator+Stage 编排层）。
4. 再挑一个明显属于模型层的例子对比：去 `sglang_omni/models/qwen3_omni/config.py`，确认它是「模型专属拓扑」。
5. 用一句话写下你的结论：**框架层的代码住在 `pipeline/`、`scheduling/`、`model_runner/`、`config/`、`relay/`、`proto/` 等公共目录；模型层的代码住在 `models/<model>/` 下；公开 API 名字到源码的映射，统一记录在 `__init__.py` 的 `_EXPORTS` 里。**

**预期产出**：一张属于你自己的「名字 → 目录 → 层」对照表（至少 4 行），以及一句对「框架层 vs 模型层」边界的口头总结。这一步做完，你就拥有了在后续每一篇讲义里快速定位源码的能力。

## 6. 本讲小结

- SGLang-Omni 的全部运行时代码集中在 `sglang_omni/` 一个包下；仓库根目录的 `docs/`、`docker/`、`benchmarks/` 等是辅助资产。
- `__init__.py` 用模块级 `__getattr__` 实现**惰性导出**：公开名字到源码的映射存在 `_EXPORTS` 字典里，第一次访问才 import，后续命中 `globals()` 缓存——这让 `import sglang_omni` 保持轻量。
- 文档的 Directory Layout 给出 9 个核心目录的「概念骨架」，真实仓库还多了 `cli/`、`comm/`、`preprocessing/`、`profiler/`、`sampling/`、`http/`、`utils/`、`vendor/` 与顶层模块 `quantization.py`。
- 「阶段间通信」三件套要分清：`proto/` 定义消息**类型**，`relay/` 是数据传输**后端**，`comm/` 是选后端的**路由层**，搬运控制消息的 ZMQ 通道在 `pipeline/control_plane.py`。
- 模型专属代码一律放 `sglang_omni/models/<model>/`，核心是 `config.py`（拓扑）与 `stages.py`（阶段工厂），模型模块放 `components/`；框架级代码不进任何 `<model>/`。
- 遇到不认识的公开符号，先查 `_EXPORTS` 定位源码文件——这是本讲留给你最实用的工具。

## 7. 下一步学习建议

本讲建立的是「地图」。接下来：

- **想真正把服务跑起来**：进入 u1-l4「启动 API Server 与第一次请求」，用 `sgl-omni serve` 发出第一个请求，把地图和真实运行连起来。
- **想先看看配置长什么样**：进入 u1-l5「配置查看、导出与 YAML 结构」，你会用到本讲提到的 `cli/config.py` 和 `config/` 目录。
- **想深入某一层的源码**：本讲出现的每个子目录，后续都有一篇专题讲义——`pipeline/`（u3）、`scheduling/` + `model_runner/`（u4）、`models/`（u5）、`comm/` + `relay/`（u6）。建议按依赖顺序推进，先 u1-l4，再 u2。
