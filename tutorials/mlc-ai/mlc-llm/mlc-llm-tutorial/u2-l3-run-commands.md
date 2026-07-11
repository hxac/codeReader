# 运行入口：chat / serve / package

## 1. 本讲目标

本讲承接 [u2-l1（CLI 总入口与子命令分发）](u2-l1-cli-entrypoint.md)，专门拆解 `mlc_llm` 里三条**运行期/部署期**子命令：`chat`、`serve`、`package`。学完后你应当能够：

- 用 `mlc_llm chat` 与模型交互，并熟练使用 `/set`、`/stats`、`/metrics`、`/reset`、`/exit`、`/help` 等特殊指令；
- 用 `mlc_llm serve` 启动一个 OpenAI 兼容的 REST 服务器，理解 `--mode`、`--host`、`--port`、`--overrides`、`--api-key` 等关键参数；
- 说清楚 `mlc_llm package` 如何把模型库 + 权重 + 配置打包成 iOS / Android / Mac Catalyst 可用的静态库产物；
- 在脑中区分三条命令各自落在「运行期快路径」「HTTP 服务」「移动端打包」哪一个阶段。

> 提醒：本讲默认你已经读过 u1-l3（三种运行入口与 mode）和 u1-l4（三类产物：MLC 权重 / model lib / mlc-chat-config.json）。命令的「产物」概念不会在此重复。

---

## 2. 前置知识

### 2.1 cli/ 与 interface/ 的两层结构

u2-l1 已经建立：每条子命令都由两层构成——

- `cli/<cmd>.py`：**命令行入口层**，只负责解析 `argv`、做必要的 `detect_*` 翻译，然后把结构化参数交给下层；
- `interface/<cmd>.py`：**接口层**，包含真正的实现逻辑，既能被 CLI 调用，也能被你的 Python 代码直接 `import` 调用。

本讲的三条命令都遵循这个模式：

| 子命令 | CLI 入口 | 接口实现 |
| --- | --- | --- |
| chat | `cli/chat.py` | `interface/chat.py` |
| serve | `cli/serve.py` | `interface/serve.py` |
| package | `cli/package.py` | `interface/package.py` |

### 2.2 三种 engine mode 回顾

u1-l3 讲过，运行期有三种 preset mode，它决定了 `max_num_sequence`、`max_total_seq_length`、`prefill_chunk_size` 的默认推断方式：

- `interactive`：最多 1 个并发请求（chat CLI 用它）；
- `local`：低并发本地部署，`max_num_sequence` 默认为 4（serve 的默认 mode）；
- `server`：高并发服务器，自动推断尽量大的 batch 与序列长度。

本讲会看到 chat 默认锁定 `interactive`，而 serve 允许你在三者间选择。

### 2.3 同一套引擎，不同的「壳」

无论 chat 还是 serve，背后驱动的是**同一个 C++ ThreadedEngine**（见 [u1-l3](u1-l3-install-and-quickstart.md) 的「两扇门」）。chat 是一个终端 REPL 把请求喂给引擎；serve 是一个 HTTP 服务把请求喂给引擎。理解这一点，你就明白为什么两者的「生成参数」语义完全一致——它们最终走的是同一条 OpenAI 兼容的请求路径。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/mlc_llm/cli/chat.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/chat.py) | chat CLI 入口，解析 4 个参数后调 `interface.chat` |
| [python/mlc_llm/interface/chat.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/chat.py) | chat 真正实现：`ChatState` REPL、特殊指令、流式生成、`/stats` 统计 |
| [python/mlc_llm/cli/serve.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/serve.py) | serve CLI 入口，解析大量参数与 `EngineConfigOverride` |
| [python/mlc_llm/interface/serve.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/serve.py) | serve 真正实现：创建 `AsyncMLCEngine`、FastAPI app、挂载路由、`uvicorn.run` |
| [python/mlc_llm/serve/entrypoints/openai_entrypoints.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/openai_entrypoints.py) | OpenAI 兼容路由 `/v1/chat/completions` 等 |
| [python/mlc_llm/cli/package.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/package.py) | package CLI 入口，解析配置 JSON / 源码目录 / 输出目录 |
| [python/mlc_llm/interface/package.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/package.py) | package 真正实现：构建模型库、打包权重、合并静态库、构建各平台 binding |
| [python/mlc_llm/interface/help.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/help.py) | `HELP` 字典，统一存放所有 `--help` 文案（如 `mode_serve`） |
| [ios/MLCChat/mlc-package-config.json](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/ios/MLCChat/mlc-package-config.json) | package 输入配置的真实示例 |

---

## 4. 核心概念与源码讲解

### 4.1 chat CLI 与特殊指令

#### 4.1.1 概念说明

`mlc_llm chat` 是一个**交互式终端对话程序**（REPL，Read-Eval-Print Loop）。它是最直接的「跑一下模型看看」入口：起一个引擎，进入循环，每读一行输入就生成一段回复。

它和 u2-l2 讲的三条**编译期**命令（convert_weight / gen_config / compile）完全不同——那些是「把模型变成产物」，而 chat 是「拿产物来跑」。chat 不产出任何文件，只读模型权重 + model lib + mlc-chat-config.json，然后开始对话。

chat 的特别之处在于它内置了一套**特殊指令**（以 `/` 开头），让你在不退出程序的前提下改生成参数、看速度统计、重置历史。这些指令是 chat CLI 独有的便利层，serve 模式下没有。

#### 4.1.2 核心流程

chat 的执行链非常短：

```
__main__.py 识别 "chat" 子命令
        │  (sys.argv[2:] 传入)
        ▼
cli/chat.py main()        ── 解析 4 个参数
        │
        ▼
interface/chat.py chat()  ── 创建 JSONFFIEngine(mode="interactive")
        │
        ▼
ChatState(engine).chat()  ── 进入交互循环
        │
        ▼  读一行 prompt
   ┌────┴────────────────────────────────────┐
   │ 以 / 开头？                              │
   │   是 → 前缀匹配 /set /stats /metrics ... │
   │   否 → generate(prompt) 流式生成          │
   └─────────────────────────────────────────┘
```

`__main__.py` 对 chat 的分发就一行（其余子命令同构）：

```python
elif parsed.subcommand == "chat":
    from mlc_llm.cli import chat as cli
    cli.main(sys.argv[2:])
```

参见 [python/mlc_llm/__main__.py:42-45](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/__main__.py#L42-L45)（中文说明：把 `chat` 子命令的剩余参数交给 `cli/chat.py`，import 写在分支内部，符合 u2-l1 讲的「懒加载」）。

#### 4.1.3 源码精读

**① CLI 入口：仅 4 个参数**

[python/mlc_llm/cli/chat.py:8-41](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/chat.py#L8-L41)（中文说明：chat 的 CLI 层极薄，只声明 `model`（位置参数）和 `--device` / `--model-lib` / `--overrides` 三个可选项，然后原样转给 `interface.chat`）：

```python
parser.add_argument("model", type=str, ...)                 # 模型路径或 HF:// 链接
parser.add_argument("--device", default="auto", ...)        # 部署设备
parser.add_argument("--model-lib", default=None, ...)       # 模型库；不给就 JIT
parser.add_argument("--overrides", type=ModelConfigOverride.from_str, ...)
chat(model=..., device=..., model_lib=..., overrides=...)
```

注意 `--overrides` 这里用的是 `ModelConfigOverride`（覆盖 **model config**，如 `context_window_size`），它和 REPL 里 `/set` 用的 `ChatCompletionOverride`（覆盖 **生成参数**，如 `temperature`）不是一回事——一个在启动时定模型结构参数，一个在运行时改采样参数。

**② 接口层：固定用 interactive 模式 + JSONFFIEngine**

[python/mlc_llm/interface/chat.py:285-311](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/chat.py#L285-L311)（中文说明：chat 入口固定创建一个 `mode="interactive"` 的 `JSONFFIEngine`，把 `--overrides` 里的 model config 字段塞进 `EngineConfig`，最后用 `try/finally` 保证退出时 `engine.terminate()`）：

```python
engine = JSONFFIEngine(
    model, device, model_lib=model_lib,
    mode="interactive",                          # ← chat 锁定单并发
    engine_config=EngineConfig(
        max_single_sequence_length=overrides.context_window_size,
        prefill_chunk_size=overrides.prefill_chunk_size,
        ...),
)
try:
    ChatState(engine).chat()
finally:
    engine.terminate()
```

为什么固定 `interactive`？因为终端对话天然只有「一个用户、一段对话」，无需并发批处理，单并发还能把全部显存让给更长的上下文。

**③ 特殊指令清单**

[python/mlc_llm/interface/chat.py:18-30](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/chat.py#L18-L30)（中文说明：启动时打印的帮助文本，列出了 chat 支持的全部特殊指令）：

```
/help               print the special commands
/exit               quit the cli
/stats              print out stats of last request (token/sec)
/metrics            print out full engine metrics
/reset              restart a fresh chat
/set [overrides]    override settings in the generation config. For example,
                    `/set temperature=0.5;top_p=0.8;seed=23;max_tokens=100;stop=str1,str2`
```

**④ 交互循环：前缀分发**

[python/mlc_llm/interface/chat.py:249-282](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/chat.py#L249-L282)（中文说明：`ChatState.chat()` 是整个 REPL 的主循环，用 `prompt_toolkit` 读输入，再用一串 `if/elif` 按「前缀」把 `/xxx` 指令和普通对话分流）：

```python
while True:
    prompt = get_prompt(">>> ", key_bindings=kb, multiline=True)
    if prompt[:4] == "/set":        # 解析并合并生成参数覆盖
        overrides = ChatCompletionOverride.from_str(prompt.split()[1])
        for key, value in dataclasses.asdict(overrides).items():
            if value is not None:
                setattr(self.overrides, key, value)
    elif prompt[:6] == "/stats":    self.stats()
    elif prompt[:8] == "/metrics":  self.metrics()
    elif prompt[:6] == "/reset":    self.reset()
    elif prompt[:5] == "/exit":     break
    elif prompt[:5] == "/help":     _print_help_str()
    else:                           self.generate(prompt)
```

两个细节值得注意：

- `/set` 取的是 `prompt.split()[1]`，即**第一个空白分隔后的整段**。所以多个覆盖项必须用 `;` 连在一段里、**中间不能有空格**（如 `temperature=0.5;top_p=0.8`），否则空格后的部分会被丢掉。
- 分发用 `prompt[:N] == "/xxx"` 做前缀比较，顺序上 `/set`（4 字符）排在最前，因此指令彼此不会误匹配。

**⑤ `/set` 如何解析**

[python/mlc_llm/interface/chat.py:59-79](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/chat.py#L59-L79)（中文说明：`ChatCompletionOverride.from_str` 把 `temperature=0.5;top_p=0.8` 这种串按 `;` 切开，前缀加 `--` 喂给一个临时 argparse，得到结构化的采样参数；`stop` 额外按 `,` 拆成列表）：

```python
results = parser.parse_args([f"--{i}" for i in source.split(";") if i])
# "temperature=0.5;top_p=0.8"  →  ["--temperature=0.5", "--top_p=0.8"]
```

**⑥ 流式生成与 `/stats` 数据来源**

[python/mlc_llm/interface/chat.py:183-220](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/chat.py#L183-L220)（中文说明：`generate()` 以 OpenAI 风格 `stream=True` 调引擎，逐块累加 `delta.content` 并实时打印；当某块带 `usage` 时把它存为「最近一次请求的用量」，这正是 `/stats` 的数据来源）。

[python/mlc_llm/interface/chat.py:222-238](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/chat.py#L222-L238)（中文说明：`stats()` 从 `last_finished_request_usage.extra` 里取 `prefill_tokens_per_s` 与 `decode_tokens_per_s` 打印——和 u1-l3 讲过的 `usage.extra` 同源）：

```python
prefill_speed = last_finished_request.get("prefill_tokens_per_s", None)
decode_speed  = last_finished_request.get("decode_tokens_per_s", None)
```

#### 4.1.4 代码实践

**目标**：在 chat CLI 中用 `/set` 实时覆盖生成参数，并用 `/stats` 观察速度。

**操作步骤**：

1. 选一个本地已有的 MLC 模型目录（含 `mlc-chat-config.json`），或用 `HF://` 远程模型：

   ```bash
   mlc_llm chat HF://mlc-ai/Llama-3.2-1B-Instruct-q4f16_1-MLC --device auto
   ```

2. 进入 `>>> ` 提示符后，先正常问一句（如 `你好`），观察流式输出。
3. 输入 `/stats`，记录 prefill / decode 速度。
4. 输入 `/set temperature=0.5;top_p=0.8;max_tokens=50` 覆盖采样参数。
5. 再问同一个问题，观察输出风格（温度降低后应更确定、更短）。
6. 输入 `/reset` 清空历史，再 `/exit` 退出。

**需要观察的现象**：

- `/set` 之后**没有**重新加载模型，覆盖立即对下一次生成生效；
- `/stats` 打印形如 `prefill: 1234.5 tok/s, decode: 56.7 tok/s`；
- `temperature=0.5` 后回答趋于保守。

**预期结果**：覆盖生效，`/stats` 给出非 N/A 的数字。若 `last_finished_request_usage` 尚未产生（还没生成过），`/stats` 会打印 `N/A`。

> 待本地验证：具体 tok/s 数值依赖你的硬件；本实践需要可用的 GPU/CPU 后端与已下载的模型。

#### 4.1.5 小练习与答案

**练习 1**：为什么 chat 默认锁定 `mode="interactive"` 而不允许 `server`？

**参考答案**：交互式终端一次只有一个人在对话，天然单并发；`interactive` 模式把 `max_num_sequence` 设为 1，能把显存尽量留给更长的上下文窗口，且免去不必要的批调度开销。

**练习 2**：`/set temperature=0.7; max_tokens=100`（注意分号后有空格）能否正确设置 `max_tokens`？

**参考答案**：不能。`prompt.split()[1]` 只取第一个空白分隔的 token，遇到空格就会截断，`max_tokens=100` 会被丢弃。正确写法是不加空格：`/set temperature=0.7;max_tokens=100`。

**练习 3**：`/stats` 显示的 `decode_tokens_per_s` 来自哪里？

**参考答案**：来自上一次生成结束时引擎回传的 `usage.extra["decode_tokens_per_s"]`，由 `ChatState.generate` 存入 `last_finished_request_usage`，`/stats` 只是把它打印出来。

---

### 4.2 serve 启动参数

#### 4.2.1 概念说明

`mlc_llm serve` 把一个（或多个）模型变成一个 **OpenAI 兼容的 HTTP 服务器**。任何会用 `openai` Python SDK 或 `curl` 的客户端都能直接对接——这是 MLC LLM 对外提供服务的标准方式。

serve 的核心是「编排」：它把若干组件按正确顺序串起来——异步引擎 `AsyncMLCEngine`、可选的 embedding 引擎、`ServerContext`（多模型注册中心）、FastAPI 应用、CORS 中间件、各路由（OpenAI / metrics / microserving / debug）、异常处理，最后交给 `uvicorn` 跑起来。

和 chat 相比，serve 多了一层 HTTP/异步，但**请求语义不变**——同样是 OpenAI 兼容的 chat completion。

#### 4.2.2 核心流程

```
cli/serve.py main()
   │  ① 解析 ~20 个参数
   │  ② EngineConfigOverride.from_str 解析 --overrides 串
   │  ③ additional_models 拆出 (path, lib) 元组
   ▼
interface/serve.py serve()
   │  ① AsyncMLCEngine(model, mode, engine_config=...)
   │  ② （可选）AsyncEmbeddingEngine
   │  ③ with ServerContext(): 注册模型 / embedding / api_key
   │  ④ FastAPI app + CORSMiddleware
   │  ⑤ include_router(openai / metrics / microserving [/debug])
   │  ⑥ 注册 BadRequestError 异常处理
   ▼
uvicorn.run(app, host, port)
```

#### 4.2.3 源码精读

**① CLI 参数概览**

[python/mlc_llm/cli/serve.py:106-219](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/serve.py#L106-L219)（中文说明：serve 的 CLI 层比 chat 厚得多，声明了模型、设备、mode、调试、附加模型、embedding、推测解码、前缀缓存、prefill 模式、overrides、tracing、host/port、CORS、api-key 等一系列参数）。几个最常用的：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `model`（位置参数） | — | 模型目录 / `mlc-chat-config.json` / `HF://` 链接 |
| `--device` | `auto` | 部署设备 |
| `--model-lib` | `None` | 模型库；不给则 JIT |
| `--mode` | `local` | `local` / `interactive` / `server`，决定并发与显存推断策略 |
| `--host` / `--port` | `127.0.0.1` / `8000` | 监听地址 |
| `--api-key` | `None` | 不给则关闭鉴权 |
| `--overrides` | `""` | 覆盖 EngineConfig（见下） |
| `--enable-debug` | `False` | 额外挂载 debug 路由与请求级 debug_config |

`--mode` 的完整语义在 HELP 里写得很清楚，见 [python/mlc_llm/interface/help.py:177-194](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/help.py#L177-L194)（中文说明：`local`=低并发默认 batch 4；`interactive`=单并发；`server`=高并发、自动榨干显存）。三者都可用 `--overrides` 手动覆盖自动推断值。

**② EngineConfigOverride：把分号串翻成结构化对象**

[python/mlc_llm/cli/serve.py:14-34](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/serve.py#L14-L34)（中文说明：一个 dataclass，枚举所有可在 serve 启动时覆盖的 EngineConfig 字段——并发数、序列长度、prefill chunk、显存利用率、推测解码、前缀缓存、张量/流水线并行等）。它的 `from_str` 解析方式和 chat 的 `/set` 完全同构（按 `;` 切、加 `--` 喂 argparse），见 [python/mlc_llm/cli/serve.py:64-103](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/serve.py#L64-L103)。

注意一个**命名映射**的小坑：CLI override 字段叫 `context_window_size`，但传给 `serve()` 时它变成了参数 `max_single_sequence_length`——见 [python/mlc_llm/cli/serve.py:246](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/serve.py#L246)（中文说明：把用户写的 `context_window_size` 覆盖映射成引擎的 `max_single_sequence_length` 形参）。

**③ additional-models 的解析**

[python/mlc_llm/cli/serve.py:221-228](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/serve.py#L221-L228)（中文说明：`--additional-models` 接收多个 token，每个支持 `path` 或 `path,lib` 两种写法，用于推测解码场景下挂载额外的 draft 模型）：

```python
for additional_model in parsed.additional_models:
    splits = additional_model.split(",", maxsplit=1)
    if len(splits) == 2:
        additional_models.append((splits[0], splits[1]))   # (path, lib)
    else:
        additional_models.append(splits[0])                 # 仅 path
```

**④ 接口层：编排引擎 + FastAPI + uvicorn**

[python/mlc_llm/interface/serve.py:24-131](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/serve.py#L24-L131)（中文说明：`serve()` 是整个服务器的编排函数）。关键几段：

创建异步引擎并把所有覆盖项塞进 `EngineConfig`：

```python
async_engine = engine.AsyncMLCEngine(
    model=model, device=device, model_lib=model_lib, mode=mode,
    engine_config=engine.EngineConfig(
        additional_models=additional_models,
        max_num_sequence=max_num_sequence,
        speculative_mode=speculative_mode,
        prefix_cache_mode=prefix_cache_mode,
        ...),
    enable_tracing=enable_tracing,
)
```

可选的 embedding 引擎（启用 `/v1/embeddings`）：[python/mlc_llm/interface/serve.py:90-101](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/serve.py#L90-L101)（中文说明：指定 `--embedding-model` 时必须同时给 `--embedding-model-lib`，否则报错）。

注册到 `ServerContext` 并挂载路由：

```python
with ServerContext() as server_context:
    server_context.add_model(model, async_engine)
    if emb_engine is not None:
        server_context.add_embedding_engine(embedding_model, emb_engine)
    server_context.api_key = api_key

    app = fastapi.FastAPI()
    app.add_middleware(CORSMiddleware, ...)              # CORS
    app.include_router(openai_entrypoints.app)          # /v1/*
    app.include_router(metrics_entrypoints.app)         # metrics
    app.include_router(microserving_entrypoints.app)    # 微服务
    if enable_debug:
        app.include_router(debug_entrypoints.app)       # 仅调试时
    app.exception_handler(error_protocol.BadRequestError)(...)
    uvicorn.run(app, host=host, port=port, log_level="info")
```

**⑤ OpenAI 兼容路由**

[python/mlc_llm/serve/entrypoints/openai_entrypoints.py:39](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/openai_entrypoints.py#L39)（中文说明：所有 OpenAI 路由挂在一个带 `verify_api_key` 依赖的 `APIRouter` 上，鉴权由它统一拦截）。具体端点：

| 方法 | 路径 | 行号 | 作用 |
| --- | --- | --- | --- |
| POST | `/v1/embeddings` | [L45](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/openai_entrypoints.py#L45) | 文本向量（需 embedding 模型） |
| GET | `/v1/models` | [L120](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/openai_entrypoints.py#L120) | 列出已加载模型 |
| POST | `/v1/completions` | [L132](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/openai_entrypoints.py#L132) | 文本补全 |
| POST | `/v1/chat/completions` | [L236](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/openai_entrypoints.py#L236) | 对话补全（本讲实践用） |

#### 4.2.4 代码实践

**目标**：启动 serve，用 `curl` 跑通 `/v1/chat/completions`，并验证 `--mode` 与 `--overrides` 的作用。

**操作步骤**：

1. 启动服务器（默认 `127.0.0.1:8000`，mode=`local`）：

   ```bash
   mlc_llm serve HF://mlc-ai/Llama-3.2-1B-Instruct-q4f16_1-MLC \
       --device auto --port 8000
   ```

2. 另开一个终端，列出模型：

   ```bash
   curl http://127.0.0.1:8000/v1/models
   ```

3. 发一次（非流式）chat completion：

   ```bash
   curl http://127.0.0.1:8000/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{
       "model": "Llama-3.2-1B-Instruct-q4f16_1-MLC",
       "messages": [{"role":"user","content":"用一句话介绍你自己"}],
       "temperature": 0.5,
       "max_tokens": 64
     }'
   ```

4. 启用鉴权重试：停掉服务器，加 `--api-key sk-test` 重启，再用带 key 的请求访问：

   ```bash
   curl http://127.0.0.1:8000/v1/chat/completions \
     -H "Authorization: Bearer sk-test" \
     -H "Content-Type: application/json" -d '{...}'
   ```

**需要观察的现象**：

- `/v1/models` 返回一个 JSON 数组，含你加载的模型 id；
- `/v1/chat/completions` 返回结构与 OpenAI 官方一致（`choices[0].message.content`）；
- 不带 key 访问带 `--api-key` 的服务器时，被 `verify_api_key` 拦截返回 401/错误。

**预期结果**：三个端点均可返回合法 JSON。流式响应把 `"stream": true` 加入请求体即可（响应是 SSE 的 `data:` 行序列）。

> 待本地验证：模型 id 需与 `/v1/models` 返回值一致；本实践需要可用的模型与后端。

#### 4.2.5 小练习与答案

**练习 1**：`--mode server` 与 `--mode local` 的核心区别是什么？

**参考答案**：`server` 面向高并发，自动推断尽量大的 `max_num_sequence` 与 `max_total_seq_length`，尽量用满显存；`local` 面向低并发本地部署，`max_num_sequence` 默认为 4，序列/prefill 长度默认取上下文窗口大小。

**练习 2**：默认监听地址为什么是 `127.0.0.1` 而不是 `0.0.0.0`？

**参考答案**：出于安全默认只绑定本机回环，避免误把无鉴权的模型服务暴露到公网。若要对局域网开放，需显式 `--host 0.0.0.0` 并建议配合 `--api-key`。

**练习 3**：`--overrides "max_num_sequence=32;context_window_size=4096"` 中，`context_window_size` 最终落到 EngineConfig 的哪个字段？

**参考答案**：落到 `max_single_sequence_length`。CLI 层在 [cli/serve.py:246](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/serve.py#L246) 把 override 的 `context_window_size` 作为 `max_single_sequence_length` 传给 `serve()`。

---

### 4.3 package 打包流程

#### 4.3.1 概念说明

`mlc_llm package` 是一条**部署期**命令：把「模型库 + 权重 + 配置」打包成 **iOS / Android / Mac Catalyst（macabi）** 原生 App 可加载的静态库产物。它服务的对象不是服务器，而是手机/平板上的 MLC Chat App——这正是 u1-l1 讲的「让 LLM 部署到手机」落地的最后一公里。

package 与 chat/serve 有本质不同：

- chat/serve 是「跑模型」，package 是「造 App 用的库」；
- chat/serve 面向通用后端（CUDA/Vulkan/…），package 只面向 `SUPPORTED_DEVICES = ["iphone", "macabi", "android"]` 三种移动/苹果平台；
- package 会把多个模型**合并**成一个静态库（`libmodel_iphone.a` / `libmodel_android.a`），并生成一份 `mlc-app-config.json` 供 App 在运行期枚举可用模型。

#### 4.3.2 核心流程

```
cli/package.py main()
   │  ① --package-config (默认 mlc-package-config.json)
   │  ② --mlc-llm-source-dir (默认 $MLC_LLM_SOURCE_DIR)
   │  ③ --output (默认 dist)
   ▼
interface/package.py package()
   │  ① 读配置 JSON，校验 device ∈ SUPPORTED_DEVICES
   │  ② build_model_library()
   │       └─ 逐模型：下载/JIT 编译出 model lib、按需 copy 权重、汇总成 mlc-app-config.json
   │  ③ validate_model_lib()
   │       └─ 把多个 .tar 合并成单个静态库，校验每个 model_lib 的符号确实存在
   │  ④ 按 device 调用对应 binding 构建器：
   │       android → build_android_binding()   (调 android/mlc4j/prepare_libs.py)
   │       iphone  → build_iphone_binding()    (调 ios/prepare_libs.sh)
   │       macabi  → build_macabi_binding()    (调 ios/prepare_libs.sh --catalyst)
   ▼
产物：output/lib/libmodel_*.a、output/bundle/mlc-app-config.json、(可选) 打包好的权重
```

#### 4.3.3 源码精读

**① CLI 入口：三个参数 + 源码目录强校验**

[python/mlc_llm/cli/package.py:12-68](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/package.py#L12-L68)（中文说明：package CLI 层声明三个参数——配置 JSON、MLC LLM 源码目录、输出目录；其中 `_parse_mlc_llm_source_dir` 还会顺手把路径写进环境变量 `MLC_LLM_SOURCE_DIR`，因为后续要调用的 iOS/Android 构建脚本会读这个变量）。

注意这里的**强校验**：[python/mlc_llm/cli/package.py:57-63](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/package.py#L57-L63)（中文说明：若没给源码目录也没设环境变量，直接抛错并提示去 clone 仓库——因为构建移动端 binding 必须用到仓库里的 `android/`、`ios/` 脚本）。

**② 支持的平台**

[python/mlc_llm/interface/package.py:18](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/package.py#L18)（中文说明：package 只支持 iphone / macabi / android 三种 device，其他值会在主流程里报错）：

```python
SUPPORTED_DEVICES = ["iphone", "macabi", "android"]
```

**③ 主流程**

[python/mlc_llm/interface/package.py:350-402](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/package.py#L350-L402)（中文说明：`package()` 依次读配置、校验 device、构建模型库、校验模型库、按 device 分派到对应 binding 构建器）：

```python
device = package_config["device"]
if device not in SUPPORTED_DEVICES: raise ValueError(...)
model_lib_path_for_prepare_libs = build_model_library(...)
validate_model_lib(...)
if   device == "android": build_android_binding(mlc_llm_source_dir, output)
elif device == "iphone":  build_iphone_binding(mlc_llm_source_dir, output)
elif device == "macabi":  build_macabi_binding(mlc_llm_source_dir, output)
```

**④ build_model_library：逐模型处理**

[python/mlc_llm/interface/package.py:21-162](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/package.py#L21-L162)（中文说明：遍历配置里的 `model_list`，对每个模型：取/下权重、必要时 JIT 编译出 model lib、按 `bundle_weight` 决定是否把权重 copy 进 bundle、最后把所有模型的元信息汇总成 `mlc-app-config.json`）。两个关键点：

- **JIT 兜底**：若没给 `model_lib`，会调 `jit.jit(...)` 现场编译，见 [L82-L104](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/package.py#L82-L104)（中文说明：缺失模型库时复用 u1-l4 讲过的 JIT 机制现场编译，并把结果缓存到 `model_lib_path_for_prepare_libs` 避免重复编译）。
- **bundle_weight 约束**：本地模型（非 `HF://`/`https://`）必须 `bundle_weight=true` 把权重打进包，否则报错，见 [L113-L117](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/package.py#L113-L117)。

**⑤ validate_model_lib：合并 + 符号校验**

[python/mlc_llm/interface/package.py:165-262](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/package.py#L165-L262)（中文说明：用 TVM 的 `cc.create_staticlib` 把多个模型的 `.tar` 合并成单个静态库 `libmodel_iphone.a` 或 `libmodel_android.a`，再扫描全局符号表确认 `mlc-app-config.json` 里声明的每个 `model_lib` 都真实存在于静态库中；找不到就 `sys.exit(255)`）。其中识别模型库符号的规则在 [L199-L210](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/package.py#L199-L210)：以 `___tvm_ffi__library_bin` 结尾的符号名即为一个模型库。

**⑥ 各平台 binding 构建器**

- [build_android_binding（L265-L306）](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/package.py#L265-L306)：把静态库搬到 `build/lib/`，调 `android/mlc4j/prepare_libs.py` 构建 mlc4j，再把产物与 `mlc-app-config.json` 拷回输出目录。
- [build_iphone_binding（L309-L323）](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/package.py#L309-L323)：调 `ios/prepare_libs.sh` 构建 iOS binding。
- [build_macabi_binding（L326-L347）](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/package.py#L326-L347)：调同一个脚本但加 `--catalyst`，支持用环境变量 `MLC_MACABI_DEPLOYMENT_TARGET` / `MLC_MACABI_ARCH` 定制。

**⑦ 真实配置示例**

[ios/MLCChat/mlc-package-config.json](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/ios/MLCChat/mlc-package-config.json)（中文说明：这是 iOS App 的真实打包配置，`device=iphone`，`model_list` 列了 5 个模型，每个含 `model`（HF 链接）、`model_id`、`estimated_vram_bytes`、`overrides`、可选 `bundle_weight`）。一个条目长这样：

```json
{
  "model": "HF://mlc-ai/Llama-3.2-3B-Instruct-q4f16_1-MLC",
  "model_id": "Llama-3.2-3B-Instruct-q4f16_1-MLC",
  "estimated_vram_bytes": 3000000000,
  "overrides": { "prefill_chunk_size": 128, "context_window_size": 2048 },
  "bundle_weight": true
}
```

`estimated_vram_bytes` 是给 App 在运行期判断「这个模型在当前设备上装不装得下」用的——移动端没有 `gpu_memory_utilization` 这种动态推断，需要预声明。

#### 4.3.4 代码实践

**目标**：通过阅读真实配置与源码，理解 package 的输入输出，无需真正具备 iOS/Android 工具链。

**操作步骤**：

1. 打开 [ios/MLCChat/mlc-package-config.json](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/ios/MLCChat/mlc-package-config.json)，列出 `model_list` 每个条目的 `model_id` 与是否 `bundle_weight`。
2. 在 [interface/package.py 的 build_model_library](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/package.py#L21-L162) 里追踪：对于一个 `bundle_weight=true` 的 iPhone 本地模型，权重会被 copy 到 `bundle_dir/<model_id>`，且 `app_config_model_entry["model_path"]` 被设为 `model_id`（见 [L140-L141](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/package.py#L140-L141)）。
3. 回答：为什么第 2 个模型（`gemma-2-2b-it`）没有写 `bundle_weight`？因为它用 `HF://` 远程链接，App 运行期会从 HuggingFace 下载，无需打进包。

**需要观察的现象**：

- 配置里同时存在「打权重」与「不下权重、运行期下载」两种模型；
- `estimated_vram_bytes` 在所有条目里都填了，因为它用于移动端容量判断。

**预期结果**：能用一句话说清 package 的三段流程（构建模型库 → 合并校验 → 构建 binding），并指出 `bundle_weight` 的取舍依据是「模型来源是否为本地路径」。

> 待本地验证：真正执行 `mlc_llm package` 需要对应平台的交叉编译工具链（Xcode / Android NDK），本实践以源码阅读为主。

#### 4.3.5 小练习与答案

**练习 1**：`package` 支持哪些 `device`？为什么不支持 `cuda`？

**参考答案**：仅 `iphone` / `macabi` / `android`（见 [interface/package.py:18](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/package.py#L18)）。因为 package 的目标是把模型打成**移动端 App 可加载的静态库 + app config**，服务器端 GPU 部署用 `serve`/`compile` 直接出 `.so` 即可，不需要这一步打包。

**练习 2**：若配置里某个模型既没给 `model_lib` 也没在 `model_lib_path_for_prepare_libs` 里登记，会发生什么？

**参考答案**：`build_model_library` 会触发 JIT 兜底，调 `jit.jit(...)` 现场编译出模型库，并把结果回填到 `model_lib_path_for_prepare_libs` 以免后续重复编译（见 [L82-L104](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/package.py#L82-L104)）。

**练习 3**：`validate_model_lib` 用什么特征判断一个静态库符号是不是「模型库」？

**参考答案**：看符号名是否以 `___tvm_ffi__library_bin` 结尾（见 [L203-L209](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/package.py#L203-L209)）。每个 TVM 编译产物都带这个后缀，去掉后缀（并处理开头的下划线、把 `-` 换成 `_`）就是 `model_lib` 名。

---

## 5. 综合实践

把三条命令串起来，完成一次「从交互到服务」的对照实验。选一个本地或 `HF://` 的小模型（如 `Llama-3.2-1B-Instruct-q4f16_1-MLC`）：

1. **用 chat 跑通基线**：`mlc_llm chat <model> --device auto`，问一句「写一首关于秋天的两句诗」，记下 `/stats` 的 decode 速度；再用 `/set temperature=0.2` 重问同样问题，对比输出稳定性。

2. **改用 serve 暴露同样能力**：`mlc_llm serve <model> --device auto --port 8000`，用 `curl` 向 `/v1/chat/completions` 发送**相同 prompt 与 temperature=0.2**，确认返回的文本与 chat 模式下一致——这验证了「chat 与 serve 共享同一个引擎、同一套 OpenAI 兼容请求路径」。

3. **读 package 配置做对照**：打开 [ios/MLCChat/mlc-package-config.json](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/ios/MLCChat/mlc-package-config.json)，把你刚用的模型的 `overrides`（如 `prefill_chunk_size`、`context_window_size`）和你 serve 时通过 `--overrides` 能覆盖的字段做对比，体会「运行期可调参数」与「打包期预声明参数」的交集。

**交付物**：一段说明，包含 (a) chat 与 serve 在相同输入下的输出是否一致及原因；(b) chat 的 `/set` 与 serve 的请求体 `temperature` 字段是否等价；(c) package 的 `overrides` 与 serve 的 `--overrides` 字段名是否完全相同，若不同请举出一例（提示：`context_window_size` vs `max_single_sequence_length`）。

> 待本地验证：实验需要可用的模型与后端；若没有 GPU，可改用 CPU 后端或仅完成第 3 步的配置对照。

---

## 6. 本讲小结

- `chat` 是交互式终端 REPL：CLI 层极薄（4 个参数），接口层固定用 `JSONFFIEngine(mode="interactive")`，靠 `ChatState` 的前缀分发实现 `/set`、`/stats`、`/metrics`、`/reset`、`/exit`、`/help` 特殊指令。
- `/set` 用 `;` 分隔多个覆盖项且**不能有空格**，覆盖的是**生成参数**（`ChatCompletionOverride`）；而启动时的 `--overrides` 覆盖的是**模型结构参数**（`ModelConfigOverride`），两者不同。
- `/stats` 的速度来自引擎回传的 `usage.extra`（`prefill_tokens_per_s` / `decode_tokens_per_s`），与 u1-l3 同源。
- `serve` 是 OpenAI 兼容 HTTP 服务器：CLI 把众多参数与 `EngineConfigOverride` 翻译成结构化对象，接口层编排 `AsyncMLCEngine` + 可选 embedding 引擎 + `ServerContext` + FastAPI + CORS + 路由 + `uvicorn`。
- `--mode`（local/interactive/server）决定并发与显存的自动推断策略，`--overrides` 可手动覆盖；`context_window_size` 在传给引擎时被映射成 `max_single_sequence_length`。
- `package` 是面向 iphone/macabi/android 的部署期打包命令：读配置 → `build_model_library`（逐模型 JIT/取库 + 按需打权重 + 生成 `mlc-app-config.json`）→ `validate_model_lib`（合并静态库 + 符号校验）→ 按 device 构建 binding，最终产出移动端 App 可用的静态库。

---

## 7. 下一步学习建议

本讲把「运行期入口」讲完了。接下来推荐：

- **深入引擎内部**：进入 U9（C++ 推理引擎架构），看 chat/serve 背后那个 `ThreadedEngine` 究竟怎么跑——`Engine`、`ThreadedEngine`、`EngineState` 与事件-动作循环。
- **理解 Python↔C++ 桥接**：学 U11-l1（MLCEngine 与 JSON FFI 桥接），搞清楚 chat 用的 `JSONFFIEngine` 如何把请求序列化成 JSON 调进 C++。
- **服务端进阶**：学 U11-l2（REST 服务器与 OpenAI 端点），深入 `openai_entrypoints.py` 里流式响应的 async generator 实现。
- **多端部署**：若你对移动端感兴趣，可结合 U12-l4（多端部署与工程化）阅读 `android/`、`ios/` 目录与 `prepare_libs.sh` / `prepare_libs.py`，把本讲的 package 流程补全。
