# 后端差异化 Processor：vLLM / SGLang 如何替换前端的前后处理

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `--dyn-chat-processor` 这个三档开关（`dynamo` / `vllm` / `sglang`）分别让请求走哪条管线，以及 `chat_engine_factory` 是在哪个时刻、被谁、以什么参数回调的。
2. 逐段读懂 `VllmProcessor` 与 `SglangProcessor` 的 `generator` 主流程，并能对比两者的关键差异（复用谁的解析器、是否支持 `n>1`、是否有进程池、流式刷新策略）。
3. 理解「带外注解通道」（`_dynamo_annotated` 信封里的 `event` / `comment`）为什么是给每个 chunk 附加延迟信息而不污染客户端响应的正确位置。
4. 自己写一个 `MyProcessor`，用环境变量切换启用，并证明前端响应内容不变。

本讲是 u5 单元的最后一讲，承接 u5-l1 的 `make_engine` 注入链和 u5-l2 的 `prepost.py` 请求整形，把「前端里那段 Python 代码」彻底打开。

## 2. 前置知识

阅读本讲前，你应当已经理解（来自前几讲的结论，这里只做最小重述）：

- **chat 管线有两条装配路径**（u4-l1 / u5-l1）：`EntrypointArgs(EngineType.Dynamic)` 携带 `chat_engine_factory` 回调时，chat 管线由 Rust 在发现模型后回调 Python 构造；不携带时走全 Rust 的 `build_pipeline`（Rust 侧 `OpenAIPreprocessor` 负责整形）。
- **`PreprocessedRequest` 是跨语言契约**（u4-l3）：frontend 送给 worker 的请求是一个 Rust 类型化结构。Python 侧拼出的 `dynamo_preproc` 字典会被 `depythonize` 成这个结构，**字段名必须逐一对上，多写的字段会被静默丢弃**（u5-l2 讲过 delta 的同样教训）。
- **`StreamingPostProcessor` 是跨 chunk 状态机**（u5-l2）：它把混杂的 token 流增量切成 `content` / `reasoning_content` / `tool_calls` 三种 delta。
- **取消链靠 `context` 透传**（u2-l3）：中间层只要漏传 `context`，客户端断连就传不到 worker。

本讲新增两个术语，先给直觉：

- **chat processor（聊天处理器）**：frontend 进程里负责「OpenAI 请求 → token 进引擎 → token 出引擎 → OpenAI 流式响应」这一整段整形逻辑的组件。Rust 有一份（`OpenAIPreprocessor`），vLLM 有一份（`InputProcessor`/`OutputProcessor`），SGLang 也有一份（`FunctionCallParser`/`ReasoningParser`）。本讲讲的就是「怎么把后两份搬进 frontend」。
- **带外注解（annotation）**：Dynamo 流式响应的信封里，除了 `data`（真正发给客户端的 chunk），还有 `event` 和 `comment` 两个字段。它们在 HTTP 层被识别为注解并剥离，不进入客户端响应体——这就是 `llm_metrics`（token 数、媒体数）能逐帧上报而不被用户看见的机制。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [components/src/dynamo/frontend/main.py](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py) | 前端主流程；`chat_processor` 三档分发点在这里 |
| [components/src/dynamo/frontend/frontend_args.py](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/frontend_args.py) | `--dyn-chat-processor`、`--dyn-preprocess-workers` 参数定义 |
| [components/src/dynamo/frontend/vllm_processor.py](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py) | `VllmProcessor` + `EngineFactory`：复用 vLLM 的前后处理 |
| [components/src/dynamo/frontend/sglang_processor.py](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_processor.py) | `SglangProcessor` + `SglangEngineFactory`：复用 SGLang 的前后处理，含进程池 |
| [components/src/dynamo/frontend/sglang_prepost.py](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_prepost.py) | SGLang 版预处理与 `SglangStreamingPostProcessor` |
| [lib/bindings/python/rust/llm/routed_engine.rs](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/llm/routed_engine.rs) | `RoutedEngine` 的 PyO3 包装：`generate` 如何跨语言、如何接取消链 |
| [lib/llm/src/discovery/watcher.rs](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/discovery/watcher.rs) | Rust 侧调用 `chat_engine_factory` 的现场 |
| [lib/llm/src/protocols/common/metrics.rs](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/protocols/common/metrics.rs) | `LLMMetricAnnotation`：注解通道在 Rust 侧的类型化落点 |
| [components/src/dynamo/frontend/tests/_routed_engine_fakes.py](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/tests/_routed_engine_fakes.py) | `FakeRoutedEngine`：无 GPU 驱动 processor 的测试脚手架 |

## 4. 核心概念与源码讲解

### 4.1 EngineFactory：`chat_engine_factory` 的挂载点

#### 4.1.1 概念说明

Dynamo frontend 的默认形态是「全 Rust 前后处理」：Rust 侧的 `OpenAIPreprocessor` 做模板渲染、分词、多模态计数（u4-l3），Rust 侧的流式解析器做 delta 切分。这条路性能最好，但意味着每种引擎的「方言」——vLLM 的工具调用解析器、SGLang 的 reasoning 解析器、各家 chat 模板的怪癖——都得在 Rust 里重新实现一遍。

`chat_engine_factory` 就是这个矛盾的解法：**把「前后处理」这一段从 Rust 换成一段 Python 代码，而路由、服务发现、HTTP 服务、指标仍然留在 Rust**。vLLM 和 SGLang 各自带了一套久经考验的 Python 前后处理，与其重写，不如直接复用。于是 frontend 提供了三档开关：

| `--dyn-chat-processor` | 前后处理由谁做 | frontend 需要安装 |
|---|---|---|
| `dynamo`（默认） | Rust（`OpenAIPreprocessor` + Rust 流式解析器） | 无额外依赖 |
| `vllm` | Python（`VllmProcessor`，复用 vLLM 的 `InputProcessor`/`OutputProcessor`/解析器） | `vllm` |
| `sglang` | Python（`SglangProcessor`，复用 SGLang 的模板渲染与解析器） | `sglang` |

这也解释了 u1-l3 讲过的「引擎依赖按 extra 拆分且互斥」在 frontend 侧的对应面：选哪档 processor，就要求 frontend 容器里装了哪家的包。

#### 4.1.2 核心流程

从命令行到一个可服务的 chat 引擎，链路是：

```text
python -m dynamo.frontend ... --dyn-chat-processor sglang
        │
        ▼
main.py 解析参数（FrontendArgGroup + 引擎自己的 flag 解析器）
        │  config.chat_processor == "sglang"
        ▼
setup_sglang_engine_factory(config, sglang_flags) ──► SglangEngineFactory 实例
        │
        ▼
kwargs["chat_engine_factory"] = factory.chat_engine_factory   （绑定方法）
        │
        ▼
EntrypointArgs(EngineType.Dynamic, **kwargs) ──► make_engine(runtime, args)
        │                                    （跨 PyO3，u5-l1 的白名单注入链）
        ▼
Rust 侧 watcher 发现一个支持 chat 的模型
        │
        ▼
factory(instance_id, mdc, routed_engine)        （Rust 回调 Python）
        │
        ▼
加载 tokenizer / 解析器 ──► 构造 Processor ──► PythonAsyncEngine(gen.generator, loop)
        │
        ▼
该模型的 chat 引擎就绪，HttpService 开始转发请求到它
```

注意「惰性」：工厂函数在** frontend 启动时**只是被挂上，真正的构造发生在**第一个匹配的模型被发现时**。这就是 u4-l4 讲的 `WorkerSet` 物化过程在 Python 侧的镜像。

#### 4.1.3 源码精读

**分发点**。三档开关在这里落地：

- [components/src/dynamo/frontend/main.py:451-463](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L451-L463)：`config.chat_processor` 为 `"vllm"` 时调用 `setup_engine_factory(...).chat_engine_factory`，为 `"sglang"` 时调用 `setup_sglang_engine_factory(...).chat_engine_factory`，两种情况都把结果塞进 `kwargs["chat_engine_factory"]`。注意第三种情况（默认 `dynamo`）：**什么也不做**，`kwargs` 里没有这个键，于是 Rust 侧走全 Rust 的 `build_pipeline`。

**参数定义**。这个开关标记为 EXPERIMENTAL：

- [components/src/dynamo/frontend/frontend_args.py:599-610](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/frontend_args.py#L599-L610)：`--dyn-chat-processor`（环境变量 `DYN_CHAT_PROCESSOR`，默认 `dynamo`）。help 文本明确说 vLLM 档是「local vLLM for pre and post processing」——是借用它的**处理器**，不是在 frontend 里跑一个推理引擎。
- [components/src/dynamo/frontend/frontend_args.py:628-640](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/frontend_args.py#L628-L640)：`--dyn-preprocess-workers`，`> 0` 时把 CPU 密集的整形工作丢进 `ProcessPoolExecutor`（每个 worker 独立 GIL）。这是后面 SGLang 进程池路径的开关。

**两个工厂的包装函数**。它们只是「延迟 import + 收拢参数」的薄壳：

- [components/src/dynamo/frontend/main.py:98-108](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L98-L108)：`setup_engine_factory`，函数体内才 `from .vllm_processor import EngineFactory`。
- [components/src/dynamo/frontend/main.py:111-131](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L111-L131)：`setup_sglang_engine_factory`，从 `sglang_flags` 里挑出 `--tool-call-parser`、`--reasoning-parser`、`--chat-template` 三个参数传给 `SglangEngineFactory`。

**引擎专属 flag 的二次解析**。因为 vLLM/SGLang 各有一大堆自己的 CLI 参数，frontend 用 `parse_known_args` 把不认识的参数留给引擎原生的解析器：

- [components/src/dynamo/frontend/main.py:266-312](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L266-L312)：vLLM 档用 `FlexibleArgumentParser` + `FrontendArgs.add_cli_args` + `AsyncEngineArgs.add_cli_args` 解析剩余参数成 `vllm_flags`（Namespace，避免非 vLLM 用户 import 失败）；SGLang 档只认 `--tool-call-parser` / `--reasoning-parser` / `--chat-template` 三个。这里还有一段很务实的注释：无 GPU 主机上 vLLM 会自动探测成 `UnspecifiedPlatform` 导致 `add_cli_args` 崩溃，所以要先强制成 `CpuPlatform`——frontend 只借它的解析器，从不构造引擎。

**Rust 侧的回调现场**。这是整条注入链的最后一环，也是「Python 逻辑、Rust 骨架」分工的物理证据：

- [lib/llm/src/discovery/watcher.rs:699-717](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/discovery/watcher.rs#L699-L717)：当 `card.model_type.supports_chat()` 为真时，先由 `build_preprocessed_pipeline` 造出一个 `routed_engine`（只做路由、不做整形），然后调用 `factory(mcid.clone(), card.clone(), routed_engine).await` 拿到 chat 引擎。紧接着的 `else if let Some(tk) = tokenizer` 分支（[watcher.rs:718-734](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/discovery/watcher.rs#L718-L734)）就是默认的 Rust 路径——构造 `OpenAIPreprocessor` 再 `build_pipeline`。两分支互斥，产出同一种「chat 引擎」。

所以 Python 工厂的签名是固定的三参数：`(instance_id: ModelCardInstanceId, mdc: ModelDeploymentCard, routed_engine: RoutedEngine) -> PythonAsyncEngine`。`instance_id` 区分同模型的多实例，`mdc` 提供模型卡（本地权重目录、runtime_config），`routed_engine` 是通往 worker 的门。

#### 4.1.4 代码实践

**实践目标**：不动任何源码，用「源码阅读 + 运行帮助」的方式验证三档开关的存在与参数流向。

**操作步骤**：

1. 在仓库根目录运行（无需 GPU，只要装好了 `ai-dynamo`）：

   ```bash
   python3 -m dynamo.frontend --help 2>&1 | grep -A 6 "dyn-chat-processor"
   ```

2. 再看 `--dyn-preprocess-workers` 的帮助：

   ```bash
   python3 -m dynamo.frontend --help 2>&1 | grep -A 6 "dyn-preprocess-workers"
   ```

3. 静态追踪一遍调用链：从 [main.py:451](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L451) 的 `if config.chat_processor == "vllm":` 开始，沿着 `setup_engine_factory` → `EngineFactory.__init__` → `chat_engine_factory` → `watcher.rs` 的回调，在纸上画出这条链，标注每一步传递了什么。

**需要观察的现象**：

- `--dyn-chat-processor` 的帮助文本里列出三个可选值，且默认是 `dynamo`。
- `--dyn-preprocess-workers` 的帮助文本说明它只对 `vllm` / `sglang` 两档生效。

**预期结果**：两条帮助文本都能打出来，且与 [frontend_args.py:599-640](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/frontend_args.py#L599-L640) 的源码一致。若本机没装 frontend，此项「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `setup_engine_factory` 要在函数体内才 `from .vllm_processor import EngineFactory`，而不是放在模块顶部？

**答案**：因为 `vllm_processor.py` 顶部就 `from vllm.config import ...`。如果 `main.py` 顶层 import 它，那么每一个用默认 `dynamo` 档、没装 vLLM 的用户都会在启动时直接 `ImportError`。延迟到函数体内，只有真正选了 `vllm` 档（此时必然装了 vLLM，否则 [main.py:266-297](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L266-L297) 会 `sys.exit(1)`）才会触发 import。`main.py:46-47` 里那个 `if TYPE_CHECKING:` 的 import 也同理——只为类型标注，不产生运行时依赖。

**练习 2**：如果把 `chat_engine_factory` 的返回值从一个 `PythonAsyncEngine` 改成一个裸的 async generator 函数，会发生什么？

**答案**：装配会在 Rust 侧失败。回调结果会被 `Python::with_gil(...)` extract 成 `PythonAsyncEngine`（见 [lib/bindings/python/rust/llm/entrypoint.rs:836-839](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/llm/entrypoint.rs#L836-L839)），裸函数不是 `PythonAsyncEngine` 的实例，extract 报错并包装成 `chat_engine_factory callback failed`。`PythonAsyncEngine` 这个包装是必要的，因为它要同时携带「generator 函数」和「构造时的事件循环」（u2-l2 讲过 M4 模块）。

**练习 3**：`--dyn-chat-processor dynamo`（默认档）时，`kwargs` 里有没有 `chat_engine_factory` 这个键？

**答案**：没有。[main.py:451-463](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L451-L463) 只在 `vllm` / `sglang` 两分支里赋值。键不存在时，Rust 侧 `EntrypointArgs` 的 `chat_engine_factory` 字段是 `None`，watcher 于是走 `else if let Some(tk) = tokenizer` 的全 Rust 分支。

---

### 4.2 VllmProcessor：把 vLLM 的前后处理搬进 frontend

#### 4.2.1 概念说明

`VllmProcessor` 解决的问题是：vLLM 生态里有大量「只有 vLLM 自己才认得」的东西——各家模型的工具调用解析器（`ToolParserManager` 注册表里的几十种）、reasoning 解析器、chat 模板渲染细节、多模态 processor。让 Dynamo 在 Rust 里逐一复刻既不现实也不可靠。

它的做法非常聪明：**在 frontend 进程里构造 vLLM 的 `InputProcessor` 和 `OutputProcessor`，但从不构造推理引擎**。`InputProcessor` 本来是 vLLM 引擎入口的「请求整形 + 分词」阶段，`OutputProcessor` 本来是「token 流 → OpenAI 响应」的整形阶段——把这两段单独拎出来放在 frontend，中间的「生成」那一截换成 Dynamo 的 `routed_engine`（把请求发给远端 worker），就得到了一个分布式的前后处理器：

```text
OpenAI 请求 ──► vLLM InputProcessor（frontend）──► dynamo_preproc 字典
                                                      │ RoutedEngine.generate（跨进程）
                                                      ▼
                                              远端 vLLM worker（只管生成）
                                                      │ token_ids 流
                                                      ▼
           OpenAI 流式响应 ◄── vLLM OutputProcessor + StreamingPostProcessor（frontend）
```

worker 端因此变得极薄：它收到的已经是 token、采样参数、停止条件，返回的只是 token id 流。

#### 4.2.2 核心流程

`generator`（每请求调用一次，每次拿到一个独立的异步生成器实例）内部的执行顺序：

1. `request_id = random_uuid()`，规范化消息里的图片 part，`extract_mm_urls` 抽出多模态 URL。
2. 调 **Dynamo 自己的** `prepost.preprocess_chat_request`（u5-l2 那个）做请求整形：工具解析器选择、模板参数、引导解码约束裁决。
3. 用整形结果构造 vLLM 的 `SamplingParams`，再把渲染后的 prompt 喂给 `InputProcessor.process_inputs`，得到 vLLM 类型化的 `EngineCoreRequest`。
4. 把它**翻译回一个普通字典** `dynamo_preproc`——这一步是跨语言契约的「反向出口」：字段名必须对上 Rust 的 `PreprocessedRequest`。
5. 多模态请求额外走 `_prepare_mm_routing`：构建精确路由信息、准备张量直传。
6. 为 `n>1` 的每个 choice 建一个独立的 `StreamingPostProcessor`（u5-l2 讲过为什么不能共享：解析器有可变流式状态）。
7. `routed_engine.generate(dynamo_preproc, context=context)` 发给 worker，逐帧消费返回流。
8. 每帧：包成 vLLM 的 `EngineCoreOutput` → `OutputProcessor.process_outputs`（负责增量 detokenize、logprob 处理、按 `stream_interval` 聚合）→ 逐 choice 用 `StreamingPostProcessor` 切 delta → 产出带注解的信封。

#### 4.2.3 源码精读

**构造函数：一堆「策略旋钮」**。

- [components/src/dynamo/frontend/vllm_processor.py:293-337](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L293-L337)：`VllmProcessor.__init__` 接收 tokenizer、`input_processor`、`output_processor`、工具/reasoning 解析器类、`routed_engine`、`block_size`、思考模式与 structural-tag 三元组。注意 [L334-337](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L334-L337)：多模态张量直传由两个环境变量控制——`DYNAMO_DISABLE_NIXL_MM=1` 整体关闭，`DYNAMO_MM_TRANSFER` 选 `shm`（默认，同节点共享内存，约 2ms）或 `nixl`（跨节点 RDMA）。失败不致命：worker 会退回自己跑 HF processor。

**入口生成器与错误边界**。

- [components/src/dynamo/frontend/vllm_processor.py:507-522](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L507-L522)：`generator` 只做一件事——把内层生成器包一层，捕获 `VLLMClientError` 并翻译成 Dynamo 的 `HttpError`。注释点明动机：vLLM 0.27 把大量请求侧的 `ValueError/TypeError` 换成了这个异常层级，Dynamo 要在 HTTP 边界保住 400/404/422 的区分（对应 u4-l2 讲的错误信封）。`dynamo.llm.exceptions` 里的 `HttpError`（见 [lib/bindings/python/src/dynamo/llm/exceptions.py:25-47](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/src/dynamo/llm/exceptions.py#L25-L47)）就是那条「带状态码出栈」的通道。

**复用 u5-l2 的整形层**。

- [components/src/dynamo/frontend/vllm_processor.py:539-552](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L539-L552)：调用的是 `from .prepost import ... preprocess_chat_request`——**Dynamo 自己的那份**，不是 vLLM 的。也就是说引导解码「单槽」裁决（u5-l2 的核心暗礁）、思考模式优先级这些 frontend 级纪律，在 vLLM 档同样生效。vLLM 只接管「渲染 + 分词 + 输出聚合」这两端的机械部分。
- [components/src/dynamo/frontend/vllm_processor.py:616-634](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L616-L634)：`_build_engine_inputs` 组装引擎输入，再交给 `input_processor.process_inputs`。注释解释了一个刻意行为：带 UUID 的多模态请求**故意**只送 token（`defer_multimodal_processing`），因为在前端处理媒体会把「前端本地缓存 miss」变成硬错误，抢在请求到达可能命中的 worker 之前。

**跨语言契约：`dynamo_preproc` 字典**。

- [components/src/dynamo/frontend/vllm_processor.py:651-681](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L651-L681)：把 vLLM 类型化的 `EngineCoreRequest.sampling_params` 翻译回 Dynamo 的字典：`stop_conditions`（max_tokens / stop / stop_token_ids / min_tokens / ignore_eos / max_thinking_tokens）、`sampling_options`（temperature / top_p / top_k / min_p / seed …）、`output_options`、`eos_token_ids`、`routing`。这个字典随后被 `RoutedEngine.generate` 里的 `depythonize` 转成 Rust 的 `PreprocessedRequest`（见 [lib/bindings/python/rust/llm/routed_engine.rs:39](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/llm/routed_engine.rs#L39)）。**字段名是硬契约**：多写一个键不会报错，只会被丢掉。
- 顺带看 [vllm_processor.py:57-80](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L57-L80)：`map_finish_reason` 把路由器/worker 侧的字符串原因（`eos`/`length`/`error:timeout`/`abort:cancelled`/`content_filter`）映射到 vLLM 的 `FinishReason` 枚举，带前缀的变体用 `startswith` 兜底。这类「字符串枚举对齐」正是接入层最常见的琐碎工作。

**取消链如何不断**。

- [lib/bindings/python/rust/llm/routed_engine.rs:29-58](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/llm/routed_engine.rs#L29-L58)：`RoutedEngine.generate` 收到可选的 `context` 后，用 `SingleIn::with_id_and_metadata` 造子请求、`parent_context.link_child(child_controller)` 建立父子链接，然后**复查**父状态（killed 则 kill、stopped 则 stop_generating）——这正是 u2-l3 讲的「建链后复查以兜底竞态」。所以 `VllmProcessor` 在 [L850-852](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L850-L852) 把 `context=context` 一路透传下去，就是在维护这条取消链。

**多模态精确路由**（本处理器最厚的部分）。

- [components/src/dynamo/frontend/vllm_processor.py:354-397](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L354-L397)：`_prepare_mm_routing` 的三道前置裁决——(a) 用户带了 `multi_modal_uuids` 就直接放弃前端路由（worker 才是 UUID 媒体的事实源，前端缓存条目填不全）；(b) `mm_processor_kwargs` 非空也放弃（vLLM 会对提供的 UUID 重哈希，路由器与 worker 会发布不同的缓存键）；(c) 否则用 `build_mm_routing_info_from_features` 构建 u4-l3 讲过的精确路由序列。
- [components/src/dynamo/frontend/vllm_processor.py:460-496](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L460-L496)：张量直传。混合模态（同时有图和视频）直接放弃，让 worker 自己跑 HF processor；单一模态才惰性构造 `MmKwargsShmSender` 或 `MmKwargsNixlSender` 并 `prepare(...)`。整段包在 `try/except` 里，任何失败都退化为「worker 自己处理」——**多模态加速是尽力而为的快路径，不是正确性路径**。

**`n>1` 的扇出**。

- [components/src/dynamo/frontend/vllm_processor.py:794-835](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L794-L835)：`sp.n == 1` 直接注册；`n > 1` 时用 vLLM 的 `ParentRequest.get_child_info` 为每个 choice 造出独立的子 request_id 和 sampling params，再逐个 `output_processor.add_request(..., request_index=output_idx)`。注释交代了原因：Dynamo 绕过了 vLLM 正常的引擎路径，得自己重建 parent/child 状态，让每个 choice 拥有独立的 detokenizer、logprob 状态和 OpenAI choice 下标。
- [components/src/dynamo/frontend/vllm_processor.py:760-762](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L760-L762)：`post_processors = {output_idx: new_post_processor() for output_idx in range(sp.n)}`——每个 choice 一个状态机，因为后端会交错推送各 choice 的 token 块。

**带外注解信封**。

- [components/src/dynamo/frontend/vllm_processor.py:955-983](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L955-L983)：每次迭代产出一个信封 `{"_dynamo_annotated": True}`，可选地带 `data`（真正的 OpenAI chunk），并**总是**带 `event: "llm_metrics"` 和 `comment: [json.dumps(metrics)]`。注释解释了为什么合并成一帧：客户端取消时，注解不会落在两个 yield 之间被丢掉。metrics 包含 `input_tokens` / `output_tokens` / `chunk_tokens`，非零时附上 `image_count` / `video_count` / `audio_count`。
- Rust 侧的落点是 [lib/llm/src/protocols/common/metrics.rs:21-67](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/protocols/common/metrics.rs#L21-L67) 的 `LLMMetricAnnotation`——一个 `serde::Deserialize` 的类型化结构，已经有 `tokenize_latency`、`detokenize_total_latency`、`prefill_worker_id` 等字段，但**没有**逐 chunk 延迟字段。
- 为什么它不会泄漏给客户端：[lib/llm/src/http/service/openai.rs:2412-2436](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/http/service/openai.rs#L2412-L2436) 显示 HTTP 层把 `comment` 当作潜在错误通道检查，但「带 `event` 类型且无 data」的帧被明确识别为注解；[metrics.rs:13-19](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/protocols/common/metrics.rs#L13-L19) 还定义了 `ANNOTATION_PAYLOAD_USAGE`，注释直接说明它「never sent to the client」。

**工厂本体**。

- [components/src/dynamo/frontend/vllm_processor.py:1021-1047](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L1021-L1047)：`EngineFactory.chat_engine_factory` 开头三道闸：模型必须支持 chat；`preprocess_workers` 必须为 0（vLLM 档不支持进程池，help 里建议改用 SGLang 档）；`mdc.local_dir()` 必须已下载好（`download_config` 必须先跑）。
- [components/src/dynamo/frontend/vllm_processor.py:1070-1096](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L1070-L1096)：构造 `ModelConfig` / `VllmConfig` / `InputProcessor` / `OutputProcessor`。几个值得注意的细节：`load_format` 默认 `"dummy"`（再次印证「不构造引擎」）；多模态时把 `mm_processor_cache_type` 设成 `"processor_only"`，注释解释默认的 `"lru"` 会在缓存命中时丢张量，而这条路需要张量可重复 pickle 后经 NIXL 发送；`VLLM_MEDIA_CONNECTOR=dynamo` 把 Dynamo 的图片加载器（LRU 缓存 + 在途去重，u4-l3）注册成 vLLM 的媒体连接器。
- [components/src/dynamo/frontend/vllm_processor.py:1152-1173](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L1152-L1173)：最终 `VllmProcessor(...)` 构造完，`return PythonAsyncEngine(gen.generator, loop)`——把绑定方法和当前事件循环一起交还给 Rust。注意工厂签名里的 `routed_engine` 参数原封不动传给了 processor：**frontend 从不在工厂里调用它，只在每个请求的 generator 里调用**。

#### 4.2.4 代码实践

**实践目标**：用仓库自带的测试脚手架，无 GPU 地驱动 `VllmProcessor` 的流式主循环，观察注解信封的结构。

**操作步骤**：

1. 先读脚手架 [components/src/dynamo/frontend/tests/_routed_engine_fakes.py:30-46](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/tests/_routed_engine_fakes.py#L30-L46)：`FakeRoutedEngine.generate` 记录收到的请求并回放预设的 item 列表；`FakeRoutedItem` 模拟「有 `is_error()`/`comments()`/`data()` 三方法」的路由响应。
2. 再读一个真实用例 [components/src/dynamo/frontend/tests/test_sglang_processor_metrics_unit.py:171-221](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/tests/test_sglang_processor_metrics_unit.py#L171-L221)：它断言了信封的 `data.usage` 原样透传、`comment[0]` 里的 metrics JSON 结构、零计数字段被省略。这个文件测的是 SGLang 档，但 vLLM 档的 `tests/test_vllm_processor_unit.py` 用的是同一套脚手架。
3. 运行（需要 vLLM 与一个真实 tokenizer，无 GPU 也可以）：

   ```bash
   pytest -m unit components/src/dynamo/frontend/tests/test_vllm_processor_unit.py -x -q
   ```

**需要观察的现象**：测试能跑通；若本机没有 vLLM 或无法联网拉 `Qwen/Qwen3-0.6B` tokenizer，会按 [test_vllm_processor_unit.py:57-80](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/tests/test_vllm_processor_unit.py#L57-L80) 的 marker 被 skip——skip 本身也说明这套测试「需要 vllm 包但不需要 GPU」。

**预期结果**：通过或 skip。若要确认信封内容，可在读测试时重点看对 `envelope["_dynamo_annotated"]` 与 `json.loads(envelope["comment"][0])` 的断言。无法本地运行时，此项「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`VllmProcessor` 里，`preprocess_chat_request` 用的是 Dynamo 的 `prepost.py` 版本；而 `SglangProcessor` 用的是 `sglang_prepost.py` 里另一个同名函数。为什么不统一？

**答案**：两者的「下游」不同。vLLM 档的下游是 vLLM 的 `InputProcessor`，它接受的是 Dynamo 自己整形出的 `engine_prompt` + `prompt_token_ids`，所以直接复用 u5-l2 那套（含引导解码单槽裁决、思考模式优先级）成本最低。SGLang 档的下游是 SGLang 的 `tokenizer.apply_chat_template(..., tokenize=True)`，它一次性完成渲染 + 分词，且要喂 SGLang 类型化的 `SglangTool` 列表，于是需要一套 SGLang 风味的整形（工具转换、force_reasoning 探测、DeepSeek-V4 特判）。统一反而要在两边各写一层适配。

**练习 2**：`_prepare_mm_routing` 在哪些情况下会「主动放弃」多模态精确路由？各自的原因是什么？

**答案**：三种。(1) 请求带 `multi_modal_uuids`（[vllm_processor.py:372-382](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L372-L382)）：worker 侧的 processor 缓存条目必须包含模型相关的 prompt 修改和张量，前端填不全，退回文本前缀路由。(2) `mm_processor_kwargs` 非空（[L385-392](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L385-L392)）：vLLM 会对提供的 UUID 重哈希，路由器和 worker 会算出不同的缓存键。(3) 混合模态时张量直传放弃（[L462-470](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L462-L470)）：sender 只支持单一模态。共同点是**都能退化到正确但较慢的路径**（worker 自己跑 HF processor），不会让请求失败。

**练习 3**：为什么 `flush` 注解和 data 要放在同一个信封里一次 yield，而不是先 yield data 再 yield 注解？

**答案**：见 [vllm_processor.py:953-954](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L953-L954) 的注释：客户端随时可能取消。如果 data 和注解分两帧，取消恰好落在两帧之间时，token 已经发给客户端而对应的 metrics 注解丢了，累计的 `output_tokens` 就会少计。合并成一帧是原子性的最小代价实现。

---

### 4.3 SglangProcessor：SGLang 版处理器与进程池

#### 4.3.1 概念说明

`SglangProcessor` 与 `VllmProcessor` 解决同一个问题，但取舍不同。SGLang 的前后处理 API 是同步的（`tokenizer.apply_chat_template`、`FunctionCallParser`），而且没有 vLLM 那样可以单独拆出来的 `InputProcessor`/`OutputProcessor` 组件，所以这一档是「手写更多、组件更少」：

- 渲染 + 分词：直接调 `tokenizer.apply_chat_template(..., tokenize=True)` 一步到位。
- 流式后处理：自己实现了 `SglangStreamingPostProcessor`（含增量 detokenize、logprob 重建、工具调用重解析）。
- 流式刷新：自己实现了按 `stream_interval` 批量 detokenize 的 `flush_pending` 逻辑（vLLM 档这件事由 `OutputProcessor` 代劳）。

它有两个 vLLM 档没有的能力：**进程池预处理**（`--dyn-preprocess-workers > 0`，绕开 GIL）和**首 chunk 立即刷新**（`stream_interval` 对首帧退化为 1，压 TTFT）。代价是不支持 `n > 1`。

#### 4.3.2 核心流程

`SglangProcessor.generator` 先按有无进程池分流，两条路汇合到同一个 `_generate_and_stream`：

```text
generator(request, context)
   │
   ├── preprocess_pool 为 None ──► _generator_inner（主进程内做完全部整形）
   │        sglang_prepost.preprocess_chat_request
   │        _build_dynamo_preproc ──► dynamo_preproc 字典
   │
   └── preprocess_pool 存在 ──► _generator_inner_pool
            信号量限流 ──► pool.submit(_preprocess_worker)   （子进程，独立 GIL）
            返回可 pickle 的 SglangPreprocessWorkerResult
            主进程里 create_parsers(...) 重建不可 pickle 的解析器
            
   两条路都构造 SglangStreamingPostProcessor ──► _generate_and_stream
        routed_engine.generate(dynamo_preproc, context=context)
        逐帧累积 token ──► 达到刷新阈值或 finish_reason 时 flush_pending
             post.process_output(...) 切 delta
             产出带 llm_metrics 注解的信封
```

#### 4.3.3 源码精读

**整形与跨语言契约**。

- [components/src/dynamo/frontend/sglang_processor.py:356-457](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_processor.py#L356-L457)：`_build_dynamo_preproc` 是 SGLang 档的「翻译出口」，产出与 vLLM 档**同构**的 `dynamo_preproc` 字典（`stop_conditions` / `sampling_options` / `output_options` / `eos_token_ids` / `routing`）。两个值得注意的方言处理：[L421-422](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_processor.py#L421-L422) 注释指出 SGLang 用 `-1` 表示 top_k 禁用而 OpenAI/vLLM 用 `0`，所以 `request.get("top_k", 0) or -1`；[L431-435](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_processor.py#L431-L435) 在有解析器激活时保留特殊 token，否则分隔符会被 detokenize 掉。
- [components/src/dynamo/frontend/sglang_processor.py:113-137](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_processor.py#L113-L137)：`_routing_from_agent_hints` 把 `nvext.agent_hints`（priority / latency_sensitivity / strict_priority / osl）翻译成 `routing` 字典，并做 i32/u32/有限浮点的类型校验。这是「请求级路由提示」进 `PreprocessedRequest.routing` 的入口，供 u6 的路由器消费。
- [components/src/dynamo/frontend/sglang_processor.py:147-180](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_processor.py#L147-L180)：`_model_eos_token_ids` 把 tokenizer 的 EOS 与 `generation_config.json` 里的 EOS 合并。注释给了个真实案例：Kimi-K3 用 XTML 协议 token 163586 `<|end_of_msg|>` 收尾，但 tokenizer 报的是另一个 token 163585 `[EOS]`。原因是 SGLang 档在 frontend 自己 detokenize，绕过了 Dynamo 的 Rust EOS 解析，也绕过了 SGLang 自己的 `trim_matched_stop`，只能在这里重做。

**进程池路径**。

- [components/src/dynamo/frontend/sglang_processor.py:286-307](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_processor.py#L286-L307)：`_init_worker` 是子进程的 `initializer`，在每个 worker 进程里加载一份自己的 tokenizer 并把解析器名、模板、思考模式等存进模块级全局变量——这样 `_preprocess_worker` 每次调用就不用重复传这些重对象。
- [components/src/dynamo/frontend/sglang_processor.py:310-353](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_processor.py#L310-L353)：`_preprocess_worker` 在子进程里跑完整个整形，返回**可 pickle** 的 `SglangPreprocessWorkerResult`（dataclass，[L271-283](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_processor.py#L271-L283)）。注意它只返回解析器的**名字**（`effective_reasoning_parser_name`），不返回解析器对象——解析器有可变状态、不可 pickle。
- [components/src/dynamo/frontend/sglang_processor.py:637-660](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_processor.py#L637-L660)：主进程用 `create_parsers(...)` 按 worker 已做的决定**重建**解析器。注释强调了纪律：池路径和内联路径的输出必须逐字节一致（"mirror those choices to keep pool- and inline-path outputs identical"），否则同一集群会因是否开进程池而表现不同。
- [components/src/dynamo/frontend/sglang_processor.py:615-628](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_processor.py#L615-L628)：`asyncio.Semaphore(preprocess_workers + 2)` 限流并发提交，防止请求洪峰把进程池队列撑爆。

**流式刷新与首 chunk 特例**。

- [components/src/dynamo/frontend/sglang_processor.py:688-709](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_processor.py#L688-L709)：`routed_engine.generate(dynamo_preproc, context=context)`——和 vLLM 档一样，`context` 必须透传，取消链才不断。随后初始化 `pending_*` 累积缓冲。
- [components/src/dynamo/frontend/sglang_processor.py:861-869](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_processor.py#L861-L869)：刷新条件 `flush_threshold = 1 if first_chunk else stream_interval`。注释点明动机：首帧立即刷，压 TTFT；之后按配置批量刷，压 CPU。这是 SGLang 档独有的可见优化。
- [components/src/dynamo/frontend/sglang_processor.py:711-794](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_processor.py#L711-L794)：`flush_pending` 把累积的 token_ids / logprobs / usage 打包，交给 `post.process_output` 切 delta，再产出一个带 `llm_metrics` 注解的信封。注意 [L746-769](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_processor.py#L746-L769) 与 vLLM 档 [L955-983](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L955-L983) 的信封结构完全一致——**两个 processor 产出同一种线格式**，这是它们可以互换的前提。额外多了一步：`nvext_extra_field_requested` 检查客户端是否显式要求 `stop_reason` / `engine_data` 这类 nvext 扩展字段，只在要求时才回填。
- [components/src/dynamo/frontend/sglang_processor.py:825-839](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_processor.py#L825-L839)：如果相邻两块的 logprob 形状不一致（一块有一块没有），先强制 flush 一次再继续累积——避免同一个缓冲里混着两种形状没法对齐。

**SGLang 版整形与后处理**（sglang_prepost.py）。

- [components/src/dynamo/frontend/sglang_prepost.py:708-727](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_prepost.py#L708-L727)：`preprocess_chat_request` 是同步函数——这是它能同时跑在主进程和子进程的前提。它先应用默认思考模式、物化消息，再解析 `force_reasoning`、转换工具、过滤模板工具，最后走 `apply_chat_template(tokenize=True)` 或 DeepSeek-V4 特判编码。
- [components/src/dynamo/frontend/sglang_prepost.py:79-90](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_prepost.py#L79-L90)：`detect_force_reasoning_from_template` 用三个正则扫描 chat 模板（qwen3 / GLM-4.5-5 / 通用兜底），启动时跑一次并缓存。注释解释了为什么做成静态布尔：逐请求解码 prompt 尾部只增加热路径延迟。
- [components/src/dynamo/frontend/sglang_prepost.py:271-319](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_prepost.py#L271-L319)：`create_parsers` 的策略——`tool_choice="required"` 或指名函数时用 `JsonArrayParser`（配合引导解码约束成 JSON 数组），否则用模型特定的 `FunctionCallParser`；引导解码激活时默认跳过 reasoning 解析器，除非 `force_reasoning`。它的 docstring 明说是「内联路径与池路径共用」，这就是上面「重建解析器」的依据。
- [components/src/dynamo/frontend/sglang_prepost.py:941-1004](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_prepost.py#L941-L1004)：`SglangStreamingPostProcessor.__init__` 持有 tokenizer、两个解析器、EOS 集合，以及一组增量解码状态：`_decode_context_ids`（取 prompt 末 5 个 token 作为解码上下文）、`_pending_decode_ids`、`_tool_call_ids/_names/_args`（按 tool_index 累积）。[L993-999](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_prepost.py#L993-L999) 的注释很坦诚：SGLang 的流式解析器每次调用至多产出一个事件，多个工具调用挤在一个批次里可能漏检，所以要累积全部喂过的文本、在 finish 时**重解析全文**兜底。
- [components/src/dynamo/frontend/sglang_prepost.py:1035-1054](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_prepost.py#L1035-L1054)：`_incremental_decode` 的技巧——把「上下文 + 待解码」整体解码，再从上下文解码结果的长度处截取增量；若增量以替换字符 `�` 结尾就先不提交（字节回退序列可能还没收齐），等下一个 token 到了再解。这是「不拆散多字节字符」的标准做法。

**工厂本体**。

- [components/src/dynamo/frontend/sglang_processor.py:889-915](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_processor.py#L889-L915)：`SglangEngineFactory.__init__`，从 `DYN_SGLANG_STREAM_INTERVAL` 环境变量读流式刷新间隔，默认 20，非法值告警回退。
- [components/src/dynamo/frontend/sglang_processor.py:917-945](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_processor.py#L917-L945)：`chat_engine_factory` 与 vLLM 版同样先做 supports_chat / local_dir 检查，然后加载 tokenizer、合并 EOS、探测模板 force_reasoning、解析解析器名（CLI 优先，`mdc.runtime_config()` 兜底——也就是 worker 在模型卡里广播的配置，对应 u4-l4 的卡片机制）。
- [components/src/dynamo/frontend/sglang_processor.py:971-1012](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_processor.py#L971-L1012)：`--dyn-preprocess-workers > 0` 时创建 `ProcessPoolExecutor`，用 `_init_worker` 做 initializer、把 tokenizer 路径等作为 initargs，然后**预热**（submit `worker_warmup`，等 120 秒超时，失败即关池抛错）。预热的意义是让首个真实请求不用付 tokenizer 加载的延迟。
- [components/src/dynamo/frontend/sglang_processor.py:1016-1032](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_processor.py#L1016-L1032)：构造 `SglangProcessor`，最后同样 `return PythonAsyncEngine(gen.generator, loop)`。两个工厂的收尾完全一样——这就是「可替换」的形式保证。

**两档差异速查表**：

| 维度 | VllmProcessor | SglangProcessor |
|---|---|---|
| 整形层来源 | 复用 Dynamo 自己的 `prepost.py` | 自带 `sglang_prepost.py` |
| 分词 | vLLM `InputProcessor.process_inputs` | `tokenizer.apply_chat_template(tokenize=True)` |
| 输出聚合 | vLLM `OutputProcessor`（含 stream_interval） | 自实现 `flush_pending`（含首 chunk 立即刷） |
| `n > 1` | 支持（ParentRequest 扇出 + 每 choice 状态机） | 不支持，`n != 1` 抛 `InvalidArgument` |
| 进程池 | 不支持（`preprocess_workers != 0` 直接报错） | 支持（`ProcessPoolExecutor` + 预热） |
| 多模态 | 精确路由 + shm/nixl 张量直传 | 仅转发 URL，UUID 不支持时显式拒绝 |
| 流式间隔环境变量 | `DYN_VLLM_STREAM_INTERVAL` | `DYN_SGLANG_STREAM_INTERVAL` |
| 错误出口 | `VLLMClientError` → `HttpError` | `InvalidArgument` / `Unknown`（`PreprocessError` 转译） |

#### 4.3.4 代码实践

**实践目标**：用 `FakeRoutedEngine` 驱动 `SglangProcessor._generate_and_stream`，亲眼看到带外注解信封，并验证 `stream_interval` 的批量刷新行为。

**操作步骤**：

1. 读 [components/src/dynamo/frontend/tests/test_sglang_processor_metrics_unit.py:25-52](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/tests/test_sglang_processor_metrics_unit.py#L25-L52) 的 `module_stubs` fixture：它用 `types.ModuleType` 往 `sys.modules` 里装了一套 SGLang 假模块，让 `sglang_processor.py` 顶层那些 `from sglang.srt...` 的 import 在没装 SGLang 的机器上也能通过。这是测「重依赖模块」的通用手法。
2. 读同一个文件的 [L171-221](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/tests/test_sglang_processor_metrics_unit.py#L171-L221)，看它如何只调 `_generate_and_stream`（跳过整形，直接喂一个假的 post-processor）。
3. 本地跑（不需要 SGLang、不需要 GPU）：

   ```bash
   pytest -m unit components/src/dynamo/frontend/tests/test_sglang_processor_metrics_unit.py -q
   ```

4. 做一个小实验：把假引擎的 items 从 1 帧改成 3 帧、每帧 5 个 token、不带 `finish_reason`，`stream_interval` 保持默认，然后数 `collect()` 返回的列表里有几个带 `data` 的帧。（只需改测试文件里的局部变量，不改源码。）

**需要观察的现象**：

- 步骤 3 输出 `2 passed`（或类似）。
- 步骤 4 中，首帧因 `first_chunk` 特例立即刷新，后续帧要攒够 `stream_interval`（默认 20）才刷——所以 3 帧 × 5 token 大概率只产出 1~2 个带 `data` 的信封，最后一个不带 `finish_reason` 的尾部缓冲可能整段不刷。

**预期结果**：信封里 `data` 与 `comment` 共存、`event == "llm_metrics"`；改小 `stream_interval` 后带 `data` 的帧数明显变多。无法本地运行时「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_preprocess_worker` 只把解析器的**名字**传回主进程，而不是解析器对象本身？

**答案**：因为 `ProcessPoolExecutor` 的返回值要跨进程 pickle，而 `FunctionCallParser` / `ReasoningParser` 持有可变的流式解析状态和绑定的 tokenizer，不可序列化。所以 worker 决定「该用哪个解析器」（这是纯数据决策，可以 pickle），主进程用 `create_parsers` 按这个名字重建实例（见 [sglang_processor.py:637-647](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_processor.py#L637-L647) 与 [sglang_prepost.py:279-283](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_prepost.py#L279-L283) 的 docstring）。这也解释了为什么解析器创建被抽成一个共享函数——两条路径必须用同一套逻辑，输出才一致。

**练习 2**：`SglangProcessor` 为什么不支持 `n > 1`，而 `VllmProcessor` 支持？

**答案**：vLLM 档有现成的 `ParentRequest.get_child_info` 和 `OutputProcessor.add_request(..., request_index=...)` 机制可复用（[vllm_processor.py:799-835](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L799-L835)），每个 choice 自动获得独立 detokenizer 与状态。SGLang 档的 detokenize 和 delta 切分全部自己实现，`SglangStreamingPostProcessor` 是按「单 choice」设计的状态机；要支持 `n > 1` 得为每个 choice 复制一整套状态并处理交错帧，成本高、需求少，于是选择在预处理时就拒绝（`_unsupported_n_message`，[sglang_processor.py:203-204](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_processor.py#L203-L204)）。

**练习 3**：两个 processor 的输出信封为什么必须结构一致（`_dynamo_annotated` / `data` / `event` / `comment`）？

**答案**：因为信封的消费者不知道也不关心是哪一档 processor 产出的。下游是 Rust 侧的 `Annotated` 协议（u3-l4 讲的请求面帧格式）和 HTTP 层的注解剥离逻辑（[openai.rs:2412-2436](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/http/service/openai.rs#L2412-L2436)），以及 `LLMMetricAnnotation` 的反序列化。任何一档私自改信封结构，另一档的行为、指标管道、客户端可见输出都会跟着漂移。这也是第 5 节综合实践能「包一层 wrapper 同时服务两档」的前提。

---

## 5. 综合实践

**任务**：写一个 `MyProcessor` 装饰器风格的包装器，给每个流式 chunk 附加「距上一 chunk 的延迟」信息，用环境变量 `MYPROC_LATENCY_ANNOTATE=1` 切换启用，并证明启用前后**客户端可见的响应内容完全不变**。

**为什么这是个好练习**：它同时串起本讲的三个模块——你要理解 `chat_engine_factory` 的挂载方式（4.1）、理解 `generator` 是每请求独立实例的入口（4.2/4.3）、理解带外注解通道为什么是附加元数据的正确位置（不污染 `data`）。而且它完全不需要 GPU。

**设计决策**（先想清楚再动手）：

1. **不修改任何源码**。做成「包装任意 inner processor」的独立类，`inner` 只需要有一个 `generator(request, context=None)` 异步生成器方法——`VllmProcessor` 和 `SglangProcessor` 都满足。
2. **延迟写进 `comment` 里的 metrics JSON，不动 `data`**。这是带外通道，HTTP 层会剥离（见 4.2.3 的 `openai.rs` 引用）。
3. **`context` 必须透传**。否则取消链断裂（u2-l3 的教训）。
4. **非注解帧原样通过**。错误帧（`{"error": ...}`）不添乱。

**第 1 步：创建文件**（放在仓库外的任意目录，比如 `/tmp/myproc/`，避免改动仓库）：

```python
# /tmp/myproc/my_processor.py
# 示例代码：非本项目源码，为本讲实践而写
import json
import os
import time
from typing import Any


class MyProcessor:
    """包装任意 chat processor，在 llm_metrics 注解里附加逐 chunk 延迟。"""

    def __init__(self, inner: Any):
        self.inner = inner
        self.enabled = os.environ.get("MYPROC_LATENCY_ANNOTATE", "") == "1"

    async def generator(self, request: dict[str, Any], context: Any | None = None):
        last_ts: float | None = None
        async for item in self.inner.generator(request, context=context):
            if not (self.enabled and isinstance(item, dict)):
                yield item
                continue

            now = time.monotonic()
            latency_ms = None if last_ts is None else round((now - last_ts) * 1000.0, 3)
            last_ts = now

            # 只碰注解通道：event == "llm_metrics" 且有 comment 列表
            if item.get("event") == "llm_metrics" and item.get("comment"):
                try:
                    metrics = json.loads(item["comment"][0])
                except (ValueError, TypeError):
                    metrics = {}
                if latency_ms is not None:
                    metrics["chunk_gap_ms"] = latency_ms
                item = {**item, "comment": [json.dumps(metrics)]}

            yield item
```

**第 2 步：写验证脚本**（同样放 `/tmp/myproc/`）：

```python
# /tmp/myproc/test_my_processor.py
# 示例代码：非本项目源码，为本讲实践而写
import asyncio
import json
import os
import sys

REPO = "/path/to/ai-dynamo-dynamo"  # 改成你的仓库路径
sys.path.insert(0, os.path.join(REPO, "components/src/dynamo/frontend/tests"))
sys.path.insert(0, "/tmp/myproc")

from _routed_engine_fakes import FakeRoutedEngine  # noqa: E402
from my_processor import MyProcessor  # noqa: E402


class FakeInner:
    """最小的 processor 替身：复刻 _generate_and_stream 的信封形状。"""

    def __init__(self):
        self.engine = FakeRoutedEngine(
            items=[
                {"token_ids": [11, 12], "finish_reason": None},
                {"token_ids": [13], "finish_reason": "stop",
                 "completion_usage": {"prompt_tokens": 4, "completion_tokens": 3}},
            ]
        )

    async def generator(self, request, context=None):
        stream = await self.engine.generate({"model": "m"}, context=context)
        async for resp in stream:
            engine_response = resp.data()
            envelope = {"_dynamo_annotated": True}
            if engine_response:
                envelope["data"] = {
                    "id": "req-1", "choices": [{"index": 0, "delta": {"content": "x"}}],
                    "model": "m", "object": "chat.completion.chunk",
                }
            envelope["event"] = "llm_metrics"
            envelope["comment"] = [json.dumps({"input_tokens": 4, "output_tokens": 3})]
            yield envelope


async def _collect():
    items = []
    async for item in MyProcessor(FakeInner()).generator({"model": "m"}):
        items.append(item)
    return items


# 关：不启用
os.environ.pop("MYPROC_LATENCY_ANNOTATE", None)
off = asyncio.run(_collect())

# 开：启用
os.environ["MYPROC_LATENCY_ANNOTATE"] = "1"
on = asyncio.run(_collect())

# 断言 1：客户端可见内容（data 字段）完全一致
assert [i.get("data") for i in off] == [i.get("data") for i in on], "data 不应变化"

# 断言 2：启用后注解里多了 chunk_gap_ms，且首帧没有（无上一帧可参考）
gaps = [json.loads(i["comment"][0]).get("chunk_gap_ms") for i in on]
assert gaps[0] is None and all(g >= 0 for g in gaps[1:]), gaps

# 断言 3：关闭时注解里没有 chunk_gap_ms
assert all("chunk_gap_ms" not in json.loads(i["comment"][0]) for i in off)

# 断言 4：inner 收到的请求被原样透传（含 context）
assert len(on) == len(off)
print("PASS")
print("  启用时的 chunk_gap_ms 序列:", gaps)
```

**第 3 步：运行**：

```bash
python3 /tmp/myproc/test_my_processor.py
```

**需要观察的现象与预期结果**：

- 打印 `PASS`，且 `chunk_gap_ms` 序列首元素为 `None`、后续为非负小数（通常在 0~2ms 量级，取决于调度）。
- 四条断言分别证明：内容不变（断言 1）、延迟确实被附加（断言 2）、环境变量确实能关掉（断言 3）、帧数不变（断言 4）。

**第 4 步（可选，接真实链路）**：把它接到一个真实的工厂上。正确做法不是去拆 `PythonAsyncEngine` 的内部字段（那不是公开 API，跨版本不保证），而是**复制工厂的尾部、把 processor 包一层再交还**。对照 [sglang_processor.py:1016-1032](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/sglang_processor.py#L1016-L1032)（`gen = SglangProcessor(...)` 到 `return PythonAsyncEngine(gen.generator, loop)` 这十几行）：

```python
# 示例代码：非本项目源码，为本讲实践而写
# 思路：复制 SglangEngineFactory.chat_engine_factory 的函数体，
# 只把最后两处改掉：
#   gen = SglangProcessor(...)          # 原样
#   gen = MyProcessor(gen)              # ← 新增这一行
#   return PythonAsyncEngine(gen.generator, loop)
#
# MyProcessor.generator 的签名与 SglangProcessor.generator 完全一致，
# 所以 PythonAsyncEngine 看不出差别——这就是「鸭子类型替换」。
```

改完后用与 [main.py:111-131](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L111-L131) 的 `setup_sglang_engine_factory` 相同的方式，把你的工厂的 `chat_engine_factory` 塞进 `kwargs`。这一步需要装好 SGLang extra 和一个可用 tokenizer（mocker 后端配 `--model-path` 指向真实 HF 名即可，见 [components/src/dynamo/mocker/main.py:46-55](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/mocker/main.py#L46-L55)，它只下载配置不下载权重），端到端行为「待本地验证」。

**一个重要的边界认知**：`chunk_gap_ms` 这个键只活到 Rust 边界为止。`comment[0]` 里的 JSON 会被反序列化成 `LLMMetricAnnotation`（[metrics.rs:21-67](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/protocols/common/metrics.rs#L21-L67)），该结构没有 `deny_unknown_fields`，所以**不会报错，但未知字段会被静默丢弃**——和 u5-l2 讲的 delta 字段是同一类教训。要让延迟真正进 Prometheus 指标，需要在 `LLMMetricAnnotation` 里加字段（一个 `lib/llm` 内的 Rust 变更，还要同步指标导出）。本实践的价值在于验证「注解注入点选对了、内容零污染」，这两点在 Python 侧就能完全证明。

## 6. 本讲小结

- `--dyn-chat-processor` 是三档开关：默认 `dynamo` 走全 Rust 管线；`vllm` / `sglang` 通过 `kwargs["chat_engine_factory"]` 把前后处理换成 Python，路由、发现、HTTP、指标仍留在 Rust。回调发生在 **watcher 发现支持 chat 的模型时**，签名固定为 `(instance_id, mdc, routed_engine) -> PythonAsyncEngine`。
- `VllmProcessor` 的策略是「借组件」：在 frontend 里构造 vLLM 的 `InputProcessor`/`OutputProcessor` 但从不构造引擎，中间的生成换成 `routed_engine`；整形层仍复用 Dynamo 自己的 `prepost.py`（引导解码单槽、思考模式优先级等纪律不变）。
- `SglangProcessor` 的策略是「自己写」：同步整形 + 自实现批量刷新（首 chunk 立即刷压 TTFT）+ 可选进程池（`--dyn-preprocess-workers`，子进程 pickle 回传结果、主进程重建不可 pickle 的解析器）；不支持 `n > 1`。
- 两档产出的 `dynamo_preproc` 字典和输出信封**结构一致**：前者被 `RoutedEngine.generate` 的 `depythonize` 转成 Rust `PreprocessedRequest`（字段名是硬契约），后者是 `_dynamo_annotated` + `data` + `event` + `comment` 的带外注解格式。
- 附加元数据的正确位置是注解通道（`event: "llm_metrics"` 的 `comment` JSON），它在 HTTP 层被剥离、不进客户端响应；但反序列化目标是类型化的 `LLMMetricAnnotation`，自定义键会被静默丢弃。
- `context` 透传是取消链不断的硬性纪律：`RoutedEngine.generate` 会 `link_child` 并复查父状态，任何一档 processor 漏传都会让客户端断连传不到 worker。

## 7. 下一步学习建议

本讲之后，你已经把 frontend 的 Python 侧（u5-l1 启动链、u5-l2 请求整形、u5-l3 后端差异化 processor）读完了。接下来有三条自然的路：

1. **进入路由**：`dynamo_preproc["routing"]` 里那些 `priority` / `expected_output_tokens` 字段到底被谁消费？去读 u6-l1（`dynamo.router` 独立进程）和 u6-l2（Rust 的 `routing_host` 与 filter-score-pick 策略模型），看请求级提示如何影响选点。
2. **进入 token 身份**：u4-l3 讲了块哈希数学，u6-l3/u6-l4 会讲 KV 事件流与基数树索引——那是 `mm_routing_info` 和 `block_mm_infos` 真正被用起来的地方。
3. **横向对比后端接入层**：u8-l1/u8-l2 分别讲 `dynamo.vllm` 与 `dynamo.sglang` 的 **worker 侧**接入，正好和本讲的 **frontend 侧** processor 互补——读完后你能画出一条请求从 curl 到 GPU 再回来的完整函数级链路。

如果想在本地继续动手，建议先做 u8-l4 的 mocker 全链路实验，把本讲的 `MyProcessor` 接上去端到端验证一遍。
