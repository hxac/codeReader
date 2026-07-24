# 启动 API Server 与第一次请求

## 1. 本讲目标

在 u1-l2 里我们用 `uv` 装好了包、用 `sgl-omni --help` 确认 CLI 可用；在 u1-l3 里我们把 `serve/` 目录对应到「HTTP API 层」、把 `client/` 对应到「内部 Client」。本讲把这两块串起来：**亲手启动一个服务，并向它发出第一条真正能得到回复的请求。**

读完本讲，你应当能够：

- 用 `sgl-omni serve` 启动一个 OpenAI 兼容的服务，并说清楚 `--model-path` / `--host` / `--port` / `--model-name` / `--log-level` 等常用 flag 各自的作用。
- 用 `curl` 完成 `/health`（健康检查）与 `/v1/models`（列出模型）两个最小验证。
- 用 `curl` 发送一条文本 `chat/completions` 请求并拿到回复。
- 画出一条请求从 `curl` 到模型前向所经过的层次：`HTTP → FastAPI → Client → Coordinator → Pipeline`，并说清「这一层只负责什么」。

> 本讲是「会用」层：我们关心**怎么启动、怎么验证、请求走哪条路**，暂不深入 Coordinator、Stage、Scheduler 的内部机制（那是 u2/u3/u4 的事）。

## 2. 前置知识

- **OpenAI 兼容 API**：指接口形状（路由路径、请求/响应 JSON 字段）与 OpenAI 官方一致。好处是任何已经写好对接 OpenAI 的客户端（SDK、curl 脚本、LangChain 等）几乎可以零改动地指向我们的服务。
- **FastAPI**：Python 的异步 Web 框架，用装饰器（如 `@app.get("/health")`）声明路由。本讲里它扮演「HTTP 翻译层」。
- **Uvicorn**：一个 ASGI 服务器，负责真正监听 socket、收发 HTTP 字节，并把请求交给 FastAPI 处理。
- **curl**：命令行 HTTP 客户端，本讲用它代替真实 SDK 来验证服务。
- **多阶段运行时（回顾 u1-l1）**：一次生成由 preprocessing、encoders、AR 引擎、talker、decoders、vocoder、aggregators 等异构阶段接力完成。本讲我们**不展开这些阶段**，只需知道「服务背后有一整条管线在跑」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/get_started/apiserver_quickstart.md](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/get_started/apiserver_quickstart.md) | 官方「从零到第一条请求」最短路径文档，本讲实践的依据 |
| [sglang_omni/cli/\_\_init\_\_.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/__init__.py) | 用 Typer 注册 `serve` / `config` 两个子命令，`sgl-omni` 的真正入口 |
| [sglang_omni/cli/serve.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/serve.py) | `serve` 命令的全部 flag 定义、配置解析、以及最终对 `launch_server` 的调用 |
| [sglang_omni/serve/launcher.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/launcher.py) | 启动管线运行时、创建 `Client`、挂载 FastAPI、跑 Uvicorn 的完整生命周期 |
| [sglang_omni/serve/openai_api.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py) | FastAPI 应用、所有 HTTP 路由（`/health`、`/v1/models`、`/v1/chat/completions` 等）、请求转换 |
| [sglang_omni/client/client.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py) | 内部 `Client`：把 `GenerateRequest` 转成 `OmniRequest`，提交给 Coordinator，聚合结果 |

## 4. 核心概念与源码讲解

### 4.1 `sgl-omni serve` 命令入口

#### 4.1.1 概念说明

`sgl-omni` 是安装后系统里可用的命令行程序（在 u1-l2 中已验证）。它由 [pyproject.toml:86-88](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/pyproject.toml#L86-L88) 的 `[project.scripts]` 声明，指向 `sglang_omni.cli:app`：

```toml
[project.scripts]
sgl-omni = "sglang_omni.cli:app"
sgl-omni-router = "sglang_omni_router.serve:main"
```

`app` 是一个 [Typer](https://typer.tiangolo.com/) 应用。Typer 用函数签名（参数的类型注解 + `typer.Option(help=...)`）自动生成命令行接口和 `--help`。在 [sglang_omni/cli/\_\_init\_\_.py:6-12](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/__init__.py#L6-L12) 里注册了两个子命令：

```python
app = Typer()
app.add_typer(config_app, name="config")            # sgl-omni config ...
app.command(
    "serve", context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)(_serve)                                            # sgl-omni serve ...
```

注意 `serve` 命令的两个 `context_settings`：

- `allow_extra_args=True`：命令行里**未被显式声明的额外参数**不会报错，而是被收集进 `ctx.args`。
- `ignore_unknown_options=True`：未知选项（如 `--xxx=yyy`）不直接抛错。

这两项合起来，使得 `serve` 可以接受「任意键值对」作为对管线配置的即时覆盖——这点在 4.2 会展开。

#### 4.1.2 核心流程

`sgl-omni serve` 这个命令在内部要做的事，可以用一句话概括：**把一堆命令行 flag 翻译成一份合并后的 `PipelineConfig`，再交给 `launch_server` 去启动。**

```text
sgl-omni serve --model-path ... --host ... --port ...
        │
        ▼
Typer 解析 flag，调用 serve() 函数
        │
        ▼
解析配置来源：--config 文件 / --model-path(默认) / --model-path + --text-only(text 变体)
        │
        ▼
ConfigManager.parse_extra_args(ctx.args)   # 收集额外键值对
ConfigManager.merge_config(extra_args)     # 合并成最终 PipelineConfig
        │
        ▼
一系列 apply_*_cli_overrides(...)            # 把 --tp-size / --cuda-graph 等 flag 写进配置
        │
        ▼
launch_server(merged_config, host=..., port=..., ...)   # 真正启动
```

关键在于：`serve` 命令本身**不碰模型、不开 GPU**，它只做「配置翻译」。真正干活的是 `launch_server`（见 4.4）。

#### 4.1.3 源码精读

`serve` 函数的签名非常长（因为它把几乎所有可调参数都暴露成了 flag），但其骨架很简单。核心定义在 [sglang_omni/cli/serve.py:894](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/serve.py#L894) 开始。本讲最常用的几个 flag 的声明位置如下：

- [`--model-path`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/serve.py#L896-L904)：HuggingFace 模型 ID 或本地目录，未给 `--config` 时必填。
- [`--config`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/serve.py#L905-L907)：直接给一份管线配置文件（u1-l5 会详讲导出）。
- [`--text-only`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/serve.py#L908-L914)：使用 thinker-only 管线（单 GPU、无 talker/语音输出）。**这是本讲实践要用到的开关。**
- [`--host` / `--port`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/serve.py#L922-L925)：绑定地址与端口，默认 `0.0.0.0:8000`。
- [`--model-name`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/serve.py#L926-L928)：覆盖 `/v1/models` 返回的模型名，默认用管线名。
- [`--log-level`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/serve.py#L1015-L1018)：日志级别，取值 `debug|info|warning|error|critical`，默认 `info`。

其中配置来源的解析逻辑在 [sglang_omni/cli/serve.py:1216-1226](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/serve.py#L1216-L1226)：

```python
if config:
    config_manager = ConfigManager.from_file(config)
elif text_only:
    if model_path is None:
        raise typer.BadParameter("--model-path is required unless --config is set")
    config_manager = ConfigManager.from_model_path(model_path, variant="text")
else:
    if model_path is None:
        raise typer.BadParameter("--model-path is required unless --config is set")
    config_manager = ConfigManager.from_model_path(model_path)
```

这段代码解释了一个常被初学者忽略的规则：**配置有三条互斥的来源路径**——`--config` 文件优先；否则用 `--model-path`，并且 `--text-only` 会选取 `variant="text"` 的变体配置。

最后，所有 flag 处理完，统一收口到 [sglang_omni/cli/serve.py:1309-1321](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/serve.py#L1309-L1321) 对 `launch_server` 的调用，把 `host/port/model_name/log_level` 等透传下去。

#### 4.1.4 代码实践

实践目标：在不启动模型的前提下，验证 `serve` 命令的 flag 解析与帮助文本。

1. 运行 `sgl-omni serve --help`，观察输出。
2. 在帮助里找到 `--model-path`、`--host`、`--port`、`--model-name`、`--log-level`、`--text-only` 这几项，确认它们的存在与默认值。
3. 故意只运行 `sgl-omni serve`（不给 `--model-path` 也不给 `--config`）。

预期结果：

- `--help` 列出全部 flag，其中 `--host` 默认 `0.0.0.0`、`--port` 默认 `8000`、`--log-level` 默认 `info`。
- 不给任何配置直接运行时，Typer 会因 [serve.py:1224-1225](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/serve.py#L1224-L1225) 抛出 `BadParameter: --model-path is required unless --config is set`，进程立即退出、**不会去加载模型**。

> 待本地验证：不同安装方式下 `--help` 的具体排版可能略有差异，以你本机输出为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `sgl-omni serve` 能接受「帮助里没列出来的 `--xxx=yyy` 参数」而不报错？

**答案**：因为在 [cli/\_\_init\_\_.py:10-12](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/__init__.py#L10-L12) 注册命令时设置了 `allow_extra_args=True, ignore_unknown_options=True`，未知参数被收进 `ctx.args`，随后由 `ConfigManager.parse_extra_args` 当作配置覆盖处理。

**练习 2**：如果不传 `--host`，服务会监听在哪个地址？为什么这个默认值对「容器内部跑、外部访问」友好？

**答案**：默认 `0.0.0.0`（见 [serve.py:922-924](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/serve.py#L922-L924)）。`0.0.0.0` 表示监听本机所有网卡，容器把端口映射出去后，宿主机才能访问到服务。

---

### 4.2 常用 flag 与配置解析

#### 4.2.1 概念说明

`serve` 的 flag 可以分成三类：

1. **网络与服务元信息**：`--host` / `--port` / `--model-name` / `--log-level`。它们只影响 HTTP 层，与模型本身无关。
2. **配置来源与拓扑**：`--model-path` / `--config` / `--text-only`，以及一批「覆盖类」flag（如 `--thinker-tp-size`、`--thinker-gpus`、`--quantization`、`--mem-fraction-static`、`--thinker-cuda-graph` 等）。这些 flag 在启动前**改写 `PipelineConfig`**。
3. **透传覆盖**：任何未被显式声明的 `--key=value`，会被当成对管线配置的覆盖。

第 2、3 类的共同点是：它们都发生在 `launch_server` **之前**，结果是「一份合并后的 `PipelineConfig`」。这呼应了 u1-l1 的设计哲学——配置（声明式）与运行时（执行）是分开的两层，flag 只是「在命令行临时改配置」的便捷入口。

#### 4.2.2 核心流程

flag → 配置 的合并流水线（见 [serve.py:1230-1304](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/serve.py#L1230-L1304)）：

```text
ctx.args (未知键值对)
   │  ConfigManager.parse_extra_args      → extra_args(dict)
   ▼
ConfigManager.merge_config(extra_args)    → 基线 merged_config
   │
   ├── apply_mem_fraction_cli_overrides        (--mem-fraction-static 等)
   ├── apply_encoder_mem_reserve_cli_override  (--encoder-mem-reserve)
   ├── apply_thinker_server_args_cli_overrides (--cpu-offload-gb / --quantization)
   ├── apply_parallelism_cli_overrides         (--thinker-tp-size / --thinker-gpus / --talker-gpu ...)
   ├── apply_cuda_graph_cli_overrides          (--thinker-cuda-graph / --talker-cuda-graph)
   ├── apply_torch_compile_cli_overrides       (--thinker-torch-compile ...)
   ├── apply_decode_mode_cli_overrides         (--decode-mode async|sync)
   └── apply_partial_start_cli_overrides       (--talker-partial-start)
   ▼
最终 merged_config  →  launch_server(...)
```

每个 `apply_*` 函数都遵循同一模式：在配置里找到对应 stage，把 flag 值写进该 stage 的 `factory_args` 或 `runtime.sglang_server_args`。这些细节属于 u2/u4，本讲只需建立「flag 改的是配置、不是运行时」的直觉。

一个特别有用的调试技巧：当 `--log-level debug` 或使用 `--colocate` 时，合并后的完整配置会被打印出来（见 [serve.py:1306-1307](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/serve.py#L1306-L1307) 与 [`_print_merged_config`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/serve.py#L89-L99)）。这是排查「我的 flag 到底有没有生效」的最直接手段。

#### 4.2.3 源码精读

额外参数如何变成配置覆盖，见 [sglang_omni/cli/serve.py:1228-1233](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/serve.py#L1228-L1233)：

```python
# we use ctx to capture the arguments that are used to modify the configuration on the fly
# we do expect the extra arguments to be pairs of names and values
extra_args = config_manager.parse_extra_args(ctx.args)
merged_config = config_manager.merge_config(extra_args)
if model_path is not None:
    merged_config = merged_config.model_copy(update={"model_path": model_path})
```

注释点明了设计意图：额外参数被期望是「成对的 name/value」，用于在运行前微调配置。`model_path` 在最后用 `model_copy` 覆盖一遍，保证命令行显式指定的模型路径优先级最高。

`ConfigManager` 的两个关键方法在 [sglang_omni/config/manager.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/manager.py) 中：[`parse_extra_args`（第 45 行）](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/manager.py#L45) 负责把 `ctx.args` 解析成字典，[`merge_config`（第 78 行）](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/manager.py#L78) 负责把字典合并进基线配置。

#### 4.2.4 代码实践

实践目标：用 `--log-level debug` 触发完整配置打印，亲眼看到「合并后的 `PipelineConfig`」。

1. 准备好一个可用的本地模型路径或 HF ID（例如 `Qwen/Qwen3-Omni-30B-A3B-Instruct`）。
2. 运行（**先不要等它把模型加载完**，看到配置打印后即可 Ctrl-C 中止）：

   ```bash
   sgl-omni serve --model-path Qwen/Qwen3-Omni-30B-A3B-Instruct \
     --text-only --log-level debug --port 8000
   ```

3. 在终端输出里找到 `==== Merged Configuration ====` 这一段。

需要观察的现象：打印出的 YAML 里包含 `stages` 列表，每个 stage 有 `name`、`factory`、`gpu`、`next` 等字段；因为加了 `--text-only`，里面**不会出现 talker / code2wav 等语音相关 stage**。

预期结果：你看到了一份完整的、合并后的管线配置；中止后进程正常退出。**待本地验证**：模型较大时，debug 日志会非常详尽，建议加 `2>&1 | head -n 200` 截断查看。

> 注意：这一步需要 GPU 与已下载的模型权重。若本机不具备，可跳过实际运行，改为阅读 u1-l5 导出的 YAML 达到同样认知目的。

#### 4.2.5 小练习与答案

**练习 1**：`--model-name` 改的是模型权重，还是只是 `/v1/models` 返回的名字？

**答案**：只是名字。见 [serve.py:926-928](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/serve.py#L926-L928) 的 help：`Model name for /v1/models (default: pipeline name)`，它最终透传给 `launch_server` 用于响应模型列表，不影响加载哪个权重。

**练习 2**：如果我同时传了 `--config my.yaml` 和 `--model-path /some/path`，会发生什么？

**答案**：`--config` 决定基线配置（[serve.py:1217-1218](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/serve.py#L1217-L1218)），随后 `--model-path` 会通过 [serve.py:1232-1233](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/serve.py#L1232-L1233) 的 `model_copy(update={"model_path": ...})` 覆盖配置里的 `model_path`。即 YAML 提供拓扑，命令行覆盖权重位置。

---

### 4.3 健康检查与模型列表

#### 4.3.1 概念说明

服务起来后，最先要回答两个问题：

- **它活着吗？** → `GET /health`
- **它在服务哪个模型？** → `GET /v1/models`

这两个端点是最轻量的「冒烟测试」，不触发任何推理。`/health` 的特别之处在于它区分两种「活着」：

- HTTP 服务器本身已经能响应（进程没崩）；
- 背后的**管线运行时**也真正就绪（模型加载完、各 stage 启动完）。

只有两者都满足，才返回 `200`；若 HTTP 起来了但运行时不健康，返回 `503`。这对上游的负载均衡 / 探活很重要——避免把流量打到「端口通但还不能推理」的实例。

#### 4.3.2 核心流程

`/health` 的状态码由运行时的 `running` 标志决定：

\[
\text{status\_code} =
\begin{cases}
200 & \text{if } \texttt{running} = \text{true} \\
503 & \text{otherwise}
\end{cases}
\]

调用链很短：

```text
curl GET /health
   → FastAPI 路由 health()
   → client.health()
   → coordinator.health()      # 返回 {"running": bool, ...} 等运行时信息
   → 据返回值决定 200 / 503
```

`/v1/models` 则更简单：它**不查运行时**，直接返回 `app.state.model_name`（即 `--model-name` 或管线名）包装成的单元素列表。

#### 4.3.3 源码精读

`create_app` 在构建 FastAPI 时，通过一组 `_register_*` 函数注册所有路由，见 [sglang_omni/serve/openai_api.py:262-272](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L262-L272)：

```python
register_favicon(app)
_register_health(app)
_register_models(app)
_register_admin(app, resolved_key)
_register_chat_completions(app)
...
```

`/health` 路由本体在 [sglang_omni/serve/openai_api.py:383-397](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L383-L397)：

```python
def _register_health(app: FastAPI) -> None:
    @app.get("/health")
    async def health() -> JSONResponse:
        """Health check endpoint (includes filesystem browse info)."""
        client: Client = app.state.client
        info = client.health()
        is_running = info.get("running", False)
        status_code = 200 if is_running else 503
        return JSONResponse(
            content={"status": "healthy" if is_running else "unhealthy", **info},
            status_code=status_code,
        )
```

注意 `client.health()` 在 [sglang_omni/client/client.py:278-279](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L278-L279) 只是直接转发：

```python
def health(self) -> dict[str, Any]:
    return self._coordinator.health()
```

也就是说，健康状态最终由 Coordinator（管线运行时的「总调度」）说了算。Client 在这里只当一个透传壳。这正是 u1-l3 所说的分层：`serve/` 负责 HTTP、`client/` 负责对 Coordinator 的封装、`pipeline/` 负责真正的运行时状态。

`/v1/models` 路由在 [sglang_omni/serve/openai_api.py:400-414](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L400-L414)，核心是 `model_name: str = app.state.model_name`，构造一个只含一张 `ModelCard` 的列表返回。

#### 4.3.4 代码实践

实践目标：用 curl 完成健康检查与模型列表（这两个命令**不触发推理**，即使没有 GPU 也能验证 HTTP 层是否就绪）。

1. 先启动服务（任选一种，本机无 GPU 时可跳过实际运行，仅阅读）：

   ```bash
   sgl-omni serve --model-path Qwen/Qwen3-Omni-30B-A3B-Instruct \
     --text-only --model-name qwen3-omni --port 8000
   ```

2. 健康检查：

   ```bash
   curl -s http://localhost:8000/health
   ```

3. 列出模型：

   ```bash
   curl -s http://localhost:8000/v1/models
   ```

需要观察的现象：

- 服务**刚启动、模型还在加载**时，`/health` 可能返回 `{"status": "unhealthy", "running": false, ...}` 且 HTTP 状态码为 `503`。
- 模型加载完成、各 stage 就绪后，`/health` 返回 `{"status": "healthy", "running": true, ...}` 且状态码 `200`。
- `/v1/models` 始终返回包含 `"id": "qwen3-omni"` 的单元素列表（因为我们在 flag 里指定了 `--model-name qwen3-omni`）。

预期结果（健康时）：

```json
// GET /health
{ "status": "healthy", "running": true }
// GET /v1/models
{ "object": "list", "data": [ { "id": "qwen3-omni", "object": "model", ... } ] }
```

> 待本地验证：`/health` 返回里除 `running` 外的字段（如文件浏览信息）可能随版本变化，以你本机输出为准。

#### 4.3.5 小练习与答案

**练习 1**：如果一个实例的 `/health` 返回 `503`，但 `curl` 本身能连上端口，说明什么？

**答案**：说明 HTTP 服务器（Uvicorn + FastAPI）已经起来，但背后的管线运行时还没就绪（`running=false`，见 [openai_api.py:389-390](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L389-L390)）。此时不应把流量导入该实例。

**练习 2**：`/v1/models` 会去查 Coordinator 吗？为什么？

**答案**：不会。它直接读 `app.state.model_name`（[openai_api.py:404](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L404)）。这是单模型服务，模型名在启动时就由 `--model-name` 或管线名定死了，无需运行时查询。

---

### 4.4 请求主链路：HTTP → FastAPI → Client → Coordinator

#### 4.4.1 概念说明

这是本讲最重要的一节。官方设计文档 [docs/developer_reference/apiserver_design.md](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/apiserver_design.md) 把请求路径概括为：

> `HTTP request` → `FastAPI route` → `Client` → `Coordinator` → `Stage pipeline` → `Client aggregation` → `HTTP/SSE response`

每一层只做一件事、不越界：

| 层 | 只负责 | 不负责 |
| --- | --- | --- |
| FastAPI 路由 | 校验请求、转换成内部请求对象、格式化响应 | 调度、执行模型 |
| Client | 提交请求、聚合文本/音频片段、编码音频 | 跑模型前向 |
| Coordinator | 把请求路由进入口 stage、收集终态结果、广播 abort | 算具体的张量 |
| Stage / Scheduler / ModelRunner | 真正执行该阶段的计算（本讲不展开） | 与 HTTP 打交道 |

设计文档还强调了一个关键区分（见 [apiserver_design.md:39-71](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/apiserver_design.md#L39-L71)）：

- `create_app(client, ...)` **只建 FastAPI 应用、注册路由**，不启动运行时。
- `launch_server(pipeline_config, ...)` 是**完整的生命周期**：编译配置 → 启动管线 → 建 Client → 建 app → 挂载 profiler 路由 → 跑 Uvicorn → 关停运行时。

`sgl-omni serve` 走的就是 `launch_server` 这条完整路径。

#### 4.4.2 核心流程

**启动期**（`launch_server`，见 [launcher.py:451-497](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/launcher.py#L451-L497) 与 [`_run_server`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/launcher.py#L323-L410)）：

```text
launch_server(merged_config, host, port, ...)
   │ asyncio.run(_run_server(...))
   ▼
_find_available_port(host, port)        # 端口被占则自动找一个并告警
mp_runner = MultiProcessPipelineRunner(pipeline_config)
await mp_runner.start(timeout=...)      # 启动多进程管线(各 stage 子进程)
coordinator = mp_runner.coordinator     # 取出全局协调器
client = Client(coordinator, ...)       # 用 coordinator 构造内部 Client
app = create_app(client, model_name=...) # 建 FastAPI 并注册路由
uvicorn.Server(...).serve()             # 开始监听 HTTP
# 关停时: await mp_runner.stop()
```

**请求期**（以非流式 chat 为例）：

```text
curl POST /v1/chat/completions  {model, messages, ...}
   │
   ▼ FastAPI 路由 chat_completions(req)
gen_req = _build_chat_generate_request(req)      # ChatCompletionRequest → GenerateRequest
   │
   ▼ Client.completion(gen_req, request_id=...)
omni_request = Client._build_omni_request(gen_req)  # GenerateRequest → OmniRequest
result = await coordinator.submit(req_id, omni_request)  # 提交进管线
   │ (管线内部: Coordinator → 入口 Stage → ... → 终态 Stage，本讲不展开)
   ▼
Client 聚合 text/audio/usage/finish_reason
   │
   ▼ FastAPI 组装成 OpenAI 风格 JSON 返回
```

两个「转换点」是这条链上的关键螺丝：

1. `_build_chat_generate_request`：把 OpenAI 风格的 `ChatCompletionRequest` 转成框架内部的 `GenerateRequest`（设计文档 [apiserver_design.md:141-155](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/apiserver_design.md#L141-L155) 详述了它做哪些归一化）。
2. `_build_omni_request`：把 `GenerateRequest` 再转成真正提交给 Coordinator 的 `OmniRequest`。

之所以分两层转换，是因为 HTTP 层关心「OpenAI 协议」，而 Coordinator 关心「管线能理解的请求」——两套关注点不该耦合。

#### 4.4.3 源码精读

**启动侧**：`_run_server` 把「启动管线 → 建 Client → 建 app → 跑 Uvicorn」串起来，见 [sglang_omni/serve/launcher.py:343-406](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/launcher.py#L343-L406)：

```python
mp_runner = MultiProcessPipelineRunner(pipeline_config)
startup_timeout = float(os.environ.get("SGLANG_OMNI_STARTUP_TIMEOUT", "600"))
await mp_runner.start(timeout=startup_timeout)
coordinator = mp_runner.coordinator
...
client = Client(coordinator, **cl_kwargs)
app = create_app(client, model_name=model_name or pipeline_config.name, ...)
...
config = uvicorn.Config(app, host=host, port=port, log_level=log_level, timeout_keep_alive=120)
server = uvicorn.Server(config)
await _serve_with_failure_watch(server, [mp_runner.wait_failed()])
```

两个值得记住的细节：

- 启动超时由环境变量 `SGLANG_OMNI_STARTUP_TIMEOUT` 控制，默认 600 秒（大模型加载较慢时可能需要调大）。
- `_serve_with_failure_watch`（[launcher.py:413-448](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/launcher.py#L413-L448)）会同时盯着「HTTP 服务任务」和「管线运行时任务」，任意一方先失败都会关停另一方——避免出现「HTTP 还活着但管线已死」的僵尸状态。

**请求侧**：`/v1/chat/completions` 路由在 [sglang_omni/serve/openai_api.py:635-676](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L635-L676)：

```python
@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest) -> Response:
    client: Client = app.state.client
    ...
    request_id = req.request_id or str(uuid.uuid4())
    ...
    gen_req = _build_chat_generate_request(req)   # 转换点①
    ...
    if req.stream:
        return StreamingResponse(_chat_stream(...), media_type="text/event-stream")
    return await _chat_non_stream(client, gen_req, request_id, ...)
```

非流式分支最终调用 `client.completion(...)`，见 [openai_api.py:691](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L691)。转换点① `_build_chat_generate_request` 定义在 [openai_api.py:886](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L886)。

**Client 侧**：`Client.completion` 在 [sglang_omni/client/client.py:75](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L75) 定义，它内部迭代 `generate()`，而 `generate()` 才是真正与 Coordinator 对话的地方，见 [client.py:53-69](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L53-L69)：

```python
async def generate(self, request, request_id=None):
    req_id = request_id or str(uuid.uuid4())
    omni_request = self._build_omni_request(request)   # 转换点②
    if request.stream:
        async for msg in self._coordinator.stream(req_id, omni_request):
            ...                                          # 流式: 逐消息 yield
        return
    result = await self._coordinator.submit(req_id, omni_request)  # 非流式: 一次提交
    yield self._result_builder(req_id, result)
```

转换点② `_build_omni_request` 在 [client.py:442-450](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L442-L450)，把 `GenerateRequest` 的 `inputs/params/metadata` 重新打包成 `OmniRequest`。注意它区别对待 `stream`：流式走 `coordinator.stream(...)`（异步迭代消息），非流式走 `coordinator.submit(...)`（等待终态结果）。这两个 Coordinator 方法的内部实现是 u2-l4 的主题，本讲止步于此。

#### 4.4.4 代码实践（源码阅读型）

实践目标：不依赖 GPU，纯靠读源码把「一次非流式 chat 请求」的调用链补全，并验证两个转换点的存在。

1. 从 [openai_api.py:645](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L645) 的 `gen_req = _build_chat_generate_request(req)` 出发，跳到 [openai_api.py:886](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L886) 阅读该函数，确认它产出一个 `GenerateRequest`。
2. 顺着 [openai_api.py:691](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L691) 的 `client.completion(...)` 进入 [client.py:75](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L75)，再进入 [client.py:53](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L53) 的 `generate`。
3. 在 `generate` 里找到两个名字：`_build_omni_request`（转换点②）与 `self._coordinator.submit`（提交点）。

需要观察的现象：你能画一张只含「函数名 + 文件:行号」的调用栈图，且图里恰好出现两次「类型转换」（`ChatCompletionRequest → GenerateRequest → OmniRequest`）和一次「提交」（`coordinator.submit`）。

预期结果：调用栈大致为
`chat_completions (openai_api.py:636)` → `_build_chat_generate_request (886)` → `Client.completion (client.py:75)` → `Client.generate (client.py:53)` → `_build_omni_request (client.py:442)` → `coordinator.submit (client.py:68)`。

> 这是一条「源码阅读型实践」，无需运行即可完成，适合没有 GPU 的环境。

#### 4.4.5 小练习与答案

**练习 1**：`create_app` 和 `launch_server` 都能产出一个可用的 FastAPI 应用，它们的本质区别是什么？

**答案**：`create_app` 只建 app、注册路由，**假定你已经有一个活的 `Client`**（见 [apiserver_design.md:43-55](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/apiserver_design.md#L43-L55)）；`launch_server` 则负责从 `PipelineConfig` 一路把管线、Coordinator、Client、app、Uvicorn 全部拉起并管理生命周期（[launcher.py:451-497](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/launcher.py#L451-L497)）。`sgl-omni serve` 用的是后者。

**练习 2**：为什么要把请求转换拆成 `_build_chat_generate_request` 和 `_build_omni_request` 两步，而不是一步到位？

**答案**：因为关注点不同。前者属于 HTTP/OpenAI 协议层（处理 `messages`、`temperature`、`images` 等 OpenAI 字段），后者属于运行时层（把归一化后的 `inputs/params/metadata` 打包成 Coordinator 认识的 `OmniRequest`）。拆开使得 HTTP 层可以独立替换（比如未来支持别的协议），而不牵动 Coordinator。

**练习 3**：流式与非流式请求，在 Client 里分别调用 Coordinator 的哪个方法？

**答案**：流式调用 `coordinator.stream(req_id, omni_request)`（异步迭代消息），非流式调用 `coordinator.submit(req_id, omni_request)`（等待终态结果）。见 [client.py:61 与 68](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L61-L68)。

---

## 5. 综合实践

把本讲四个模块串起来，完成「**启动 → 验证 → 发请求 → 拿回复**」的端到端闭环。这是本讲的核心实践任务。

> 前置：你已在容器/本机用 `uv` 装好包（u1-l2），且 `sgl-omni --help` 可用；有一张可用 GPU 和已下载的 Qwen3-Omni 权重（或其本地目录）。若本机不具备 GPU，请把第 1、3、4 步当作「源码阅读 + 命令演练」，重点完成第 5 步的链路分析。

**步骤 1：启动一个 text-only 的 Qwen3-Omni 服务**

```bash
sgl-omni serve \
  --model-path Qwen/Qwen3-Omni-30B-A3B-Instruct \
  --text-only \
  --model-name qwen3-omni \
  --host 0.0.0.0 \
  --port 8000 \
  --log-level info
```

`--text-only` 让服务只跑 thinker（文本）管线、单 GPU 即可，避开 talker/语音阶段，是跑通「第一条文本请求」的最快路径（依据 [serve.py:1219-1222](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/serve.py#L1219-L1222)）。

**步骤 2：确认健康（4.3 的实践）**

```bash
curl -s http://localhost:8000/health
```

等待输出变为 `{"status": "healthy", "running": true, ...}`（HTTP 200）再继续。若长时间 503，查服务日志（常见原因：显存不足、模型文件缺失、启动超时需调大 `SGLANG_OMNI_STARTUP_TIMEOUT`）。

**步骤 3：列出模型（确认 `--model-name` 生效）**

```bash
curl -s http://localhost:8000/v1/models
```

预期看到 `"id": "qwen3-omni"`。

**步骤 4：发送第一条文本 chat 请求并拿到回复**

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3-omni",
    "messages": [{"role": "user", "content": "用一句话介绍你自己。"}],
    "max_tokens": 64,
    "stream": false
  }'
```

预期结果：返回 OpenAI 风格 JSON，回复文本在 `choices[0].message.content`。

**步骤 5：对照源码复盘请求路径（4.4 的实践）**

拿到回复后，回头在源码里标注这次请求经过的每一跳，填出下表（答案见 4.4.4）：

| 阶段 | 函数 / 位置 | 做了什么 |
| --- | --- | --- |
| HTTP 路由 | `chat_completions` @ openai_api.py:636 | 接收 `ChatCompletionRequest` |
| 转换① | `_build_chat_generate_request` @ openai_api.py:886 | ? |
| Client 入口 | `Client.completion` @ client.py:75 | ? |
| 转换② | `_build_omni_request` @ client.py:442 | ? |
| 提交 | `coordinator.submit` @ client.py:68 | ? |

**验收标准**：

- `/health` 在模型加载完成后返回 200 与 `running: true`；
- `/v1/models` 返回 `qwen3-omni`；
- chat 请求返回非空 `content`；
- 你能口头复述「curl → FastAPI → Client → Coordinator」每一层的单一职责。

## 6. 本讲小结

- `sgl-omni serve` 由 [pyproject.toml](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/pyproject.toml#L86-L88) 声明、Typer 实现；它的核心职责是**把 flag 翻译成一份合并后的 `PipelineConfig`**，再交给 `launch_server`。
- 常用 flag 分三类：网络元信息（`--host/--port/--model-name/--log-level`）、配置来源（`--model-path/--config/--text-only`）、覆盖类（`--thinker-tp-size` 等）；未知 `--key=value` 会被当成配置覆盖（`allow_extra_args`）。
- `/health` 的状态码由运行时的 `running` 标志决定：就绪 200、未就绪 503；`/v1/models` 只返回启动时定死的单模型名，不查运行时。
- 请求主链路为 `HTTP → FastAPI → Client → Coordinator → Stage`，其中有两个转换点（`ChatCompletionRequest → GenerateRequest → OmniRequest`）和一次提交（`coordinator.submit` / `coordinator.stream`）。
- `create_app` 只建 FastAPI，`launch_server` 才是含「启动管线 + 跑 Uvicorn + 关停」的完整生命周期；`sgl-omni serve` 走的是后者。
- 启动超时受 `SGLANG_OMNI_STARTUP_TIMEOUT`（默认 600s）控制；`_serve_with_failure_watch` 保证 HTTP 与运行时任一失败则一并关停。

## 7. 下一步学习建议

- **u1-l5（配置查看/导出与 YAML 结构）**：本讲你已用 `--text-only` 触发了一份合并配置的打印，下一讲正式学习用 `sgl-omni config view/export` 导出 YAML，读懂 `stage_overrides` 与 `runtime resources`。
- **u2-l1（请求主链路总览）**：本讲只画到 `Coordinator` 门口。下一单元深入 Coordinator 之内，把「Stage → Scheduler → ModelRunner」补全，并区分 `create_app` 与 `launch_server` 的设计动机。
- **继续阅读**：[docs/get_started/apiserver_quickstart.md](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/get_started/apiserver_quickstart.md) 的「Streaming / Multi-modal / Text-to-Speech」小节，以及 [docs/developer_reference/apiserver_design.md](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/apiserver_design.md) 的「Response Paths」，为 u2-l2（OpenAI 兼容 API 服务层）做准备。
