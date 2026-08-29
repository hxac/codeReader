# 预处理与分词：OpenAIPreprocessor 与 LocalModel

## 1. 本讲目标

学完本讲，你应该能够：

1. 追踪一条 chat 请求从「OpenAI 格式的消息列表」到「token id 序列」的完整整形路径：套模板 → 分词 → 媒体收集 → 装配 `PreprocessedRequest`。
2. 解释 `prompt.rs` 如何处理 `continue_final_message`（HF 的「续写最后一条消息」语义），以及 embeddings 端点专用的构造器 `new_for_embeddings` 与「右截断」分词路径。
3. 解释带图片/视频的多模态请求如何被计数、抓取与解码（包括图像解码后端的入口，细节在 u4-l5），以及前端在抓取用户提供的 URL 时如何用多层 SSRF 防护把内网目标挡在门外。
4. 说出 `LocalModel` / ModelDeploymentCard 如何把「模型目录、分词器、KV 块大小」交给预处理器。
5. 手算一个 token 块的 `BlockHash` 与链式 `SequenceHash`，并解释「相同前缀 ⇒ 相同前缀块哈希」为什么是 KV 感知路由（u6）与 KVBM（u9）得以工作的地基。
6. 说出 nvext 透传（`extra_fields` / `metadata_upload` / `token_data` / `cache_salt`）的搬运路径，以及它为什么要与 tool-call jail 交互、legacy jail 下 `n=1` 约束的由来。
7. 解释 **jail 如何旁路终端错误**（本次 #13930）：终端错误被 `take_while` 拦在 jail 之外、`JailAnnotated.error` 一律置空、原始带类型的 Dynamo 注解在流末尾「链回」给调用方，以及 HTTP 层因此可以删掉字符串前缀恢复分支。

本讲是 u4-l1 的续篇：u4-l1 讲了「引擎怎么装配」，本讲讲装配好之后，请求在进入引擎前经历的最后一道整形。

本次更新对应两条上游变更：其一是历史积欠的补课——`prompt.rs` 的 `continue_final_message`（#13841）与 embeddings 分词路径（#12157）此前只在主题里点名、未正式成节，本次补入 4.2；其二是 **#13930「保留带类型的 v1 jail 错误」**，它改写了 jail 与错误类型的边界，新增 4.7 专讲，并刷新了全文行号与永久链接。

## 2. 前置知识

- **token 与分词器**：LLM 内部不认识字符串，只认识整数 token id。分词器（BPE 等）负责文本 ↔ id 双向映射，HuggingFace 格式的模型目录里通常有一个 `tokenizer.json`。分词是「相同文本永远得到相同 id 序列」的确定性过程——这一点是后面所有哈希推理的前提。
- **chat 模板**：OpenAI 请求是结构化的 `messages` 数组（system/user/assistant），而模型训练时见到的是一段拼好的文本（如 `<|im_start|>user\n...<|im_end|>\n`）。模板（通常是一个 MiniJinja 脚本）负责这次拼接。
- **continue_final_message**：HF `apply_chat_template` 的一个开关——不闭合最后一条消息（不追加「assistant 起始标记」），让模型接着这条消息继续写。多轮写作、渐进式补全都靠它。实现难点在于：模板渲染后你必须在渲染结果里**可靠地找到最后一条消息的结尾**再截断，而模板可能改写甚至重复早先的文本。
- **KV cache 与前缀复用**：自回归推理中，每个 token 都会产生一份 KV（Key/Value）中间结果。**相同前缀的请求会计算出完全相同的 KV**，因此第二条请求可以复用第一条已算好的部分。引擎的 KV cache 按「块」（block/page，通常 16 或 32 个 token 一块）管理，所以复用粒度也是块。
- **nvext（NVIDIA 扩展信封）**：Dynamo 在 OpenAI 兼容协议之上附加的扩展字段，请求侧挂在 `nvext` 下、响应侧挂在同名信封里。本讲关心四个字段：`extra_fields`（点播响应里要额外返回的字段名列表）、`metadata_upload`（要求后端把响应元数据带外上传）、`token_data`（调用方已经分好的词，随请求直接给 token）、`cache_salt`（前缀缓存隔离盐）。它们的服务对象主要是 RL rollout 等高级消费方（见 u8-l7）。
- **tool-call jail（工具调用监狱）**：很多模型把工具调用以原生标记（如 `<tool_call>{...}</tool_call>`）混在普通文本里输出。前端必须在流式响应上「缓冲 + 解析 + 改写」，把标记改写成结构化的 `tool_calls` 字段——这段缓冲/改写逻辑俗称 jail。jail 会拆开并重组响应 chunk，所以任何「附加在 chunk 上的元数据」都有被弄丢的风险；它也**分不清「上游报错导致的 EOF」和「正常结束的 EOF」**，这正是 4.7 的主题。
- **SSRF（服务端请求伪造）**：多模态请求里图片以 URL 形式传入，服务器必须去抓这个 URL。如果攻击者传入 `http://169.254.169.254/latest/meta-data/`（云厂商元数据地址）或 `http://10.0.0.5:6379/`（内网 Redis），你的前端就替攻击者访问了内网。防护思路：只允许 http(s)/data 协议、拒绝 IP 直连、维护私网 CIDR 与内部主机名黑名单、DNS 解析结果逐 IP 复查、重定向每一跳重新检查。
- **承接 u4-l1 的术语**：`EngineConfig::InProcessTokens` 表示「框架替引擎分词」，本讲的 `OpenAIPreprocessor` 就是那个「框架」的核心实现；`PreprocessedRequest` 是它产出的内部统一请求格式。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲视角 |
|---|---|---|
| `lib/llm/src/preprocessor.rs` | 预处理器主体（约 10000 行），模板/分词/媒体/token 预算/nvext 透传/工具调用解析 | 只读骨架：结构体字段 + `preprocess_request_with_options` 主流程 + nvext/jail 相关小节 |
| `lib/llm/src/preprocessor/prompt.rs` | 模板工程的 lib/llm 侧粘合层：消息规范化、`continue_final_message`、从 MDC 构造 formatter | **本次新增精读**（4.2） |
| `lib/llm/src/protocols/common/extensions.rs` | nvext 协议定义与合并规则（`NvExtProvider`/`request_cache_salt`/`merge_response_nvext`） | 4.6 的协议侧 |
| `lib/llm/src/preprocessor/media/loader.rs` | Rust 侧媒体抓取器与 SSRF 策略（`MediaFetcher`/`MediaLoader`） | 重点精读（4.3） |
| `lib/llm/src/preprocessor/media/decoders/image.rs`（含 `backends/`） | 图像解码编排与后端选择（image_reader / turbojpeg） | 只看入口，细节在 u4-l5 |
| `components/src/dynamo/common/http/url_validator.py` | Python 侧同构的 URL 策略（后端解码路径用） | 与 Rust 侧对照 |
| `lib/llm/src/local_model.rs` | 本地模型（目录 + ModelDeploymentCard）与构建器 | 看 `kv_cache_block_size` 从哪来 |
| `lib/tokens/src/lib.rs` | token 块化与哈希的全部数学（`TokenBlockSequence` 等） | 重点精读（注意：token 类型定义在此文件，仓库中没有 `tokens.rs`） |
| `lib/kv-hashing/src/{block,request,salt}.rs` | 把「请求 → 块哈希」封装成统一入口 | 前端与路由器的字节级一致性契约 |
| `lib/llm/src/http/service/openai.rs` | HTTP 层如何消费带类型的终端错误 | 4.7 的下游 |

## 4. 核心概念与源码讲解

### 4.1 OpenAIPreprocessor：请求整形的总调度

#### 4.1.1 概念说明

HTTP 进来的 `NvCreateChatCompletionRequest` 属于「OpenAI 世界」：消息是结构化 JSON、采样参数是可选字段、图片是 URL。而推理引擎需要的是 `PreprocessedRequest`：一串 `token_ids`、一份采样/停止条件、可选的多模态数据。`OpenAIPreprocessor` 就是这两者之间的翻译官，同时兼任门卫（token 预算校验，超限请求在进入引擎前就被拒绝）。

文件开头的模块注释把这个大文件切成四步：translation（入站消息 → 内部表示）、apply（填默认值）、prompt（模板）、tokenize（分词）（[lib/llm/src/preprocessor.rs:4-12](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L4-L12)）。

#### 4.1.2 核心流程

`preprocess_request_with_options` 的主轴（简化伪代码）：

```text
builder = builder_with_lora(request, lora_name)        # 抽取采样/停止/输出选项 + nvext 透传进 extra_args
formatted = apply_template(request)                    # ① MiniJinja 渲染 chat 模板（含 continue_final_message 处理）
(token_ids, annotations) = gather_tokens(request, formatted)   # ② 分词（或直取）
(mm_image_entries, image_tokens) = gather_multi_modal_data_with_image_tokens(...)  # ③ 媒体
builder.token_ids(token_ids)                          # ④ 装配
if nvext.router 存在: builder.router(router_params)    #    外部路由参数透传
preprocessed = builder.build()
validate_preprocessed_token_budget(preprocessed)       # ⑤ token 预算门卫
```

注意顺序上的讲究：③ 在 ④ 之前执行，是因为多模态路由信息（见 4.8.3）需要借用 `token_ids` 来做「图片占位符对齐」，注释明确写了「Install tokens on the builder. Done after MM routing built its view」（[lib/llm/src/preprocessor.rs:2370-2373](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L2370-L2373)）。另外第一步的 `builder_with_lora` 还负责把 nvext 透传参数折进 `extra_args`（见 4.6）。

#### 4.1.3 源码精读

**结构体：预处理器持有的一切**（[lib/llm/src/preprocessor.rs:1291-1347](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L1291-L1347)）。这段定义了 `OpenAIPreprocessor` 的全部状态，几个关键字段：

- `formatter: Arc<dyn OAIPromptFormatter>` — chat 模板渲染器；
- `tokenizer: Arc<dyn Tokenizer>` — 分词器；
- `embedding_tokenizers: Option<EmbeddingTokenizerState>` — **只为 embeddings 管线存在**，双变体（带/不带特殊 token）惰性初始化（见 4.2.4）；
- `runtime_config` — 模型级运行时配置（reasoning/tool 解析器等），4.6 的路由选择要用它；
- `kv_cache_block_size: usize` — 从模型部署卡带来的块大小，直接决定后续块哈希的切分粒度；
- `token_budget: Option<TokenBudget>` — 引擎发布的准入策略（预算门卫的依据）；
- `context_length: u32` — embeddings 截断契约用的上下文上限；
- `tool_call_parser: Option<String>` — 工具调用解析器名（如 `glm47`、`kimi_k3`），4.6 的 jail 路由依据；
- `media_loader: Option<MediaLoader>` — 媒体抓取器，`None` 时走 URL 透传（抓取留给后端）；
- `image_token_counter` / `routing_image_token_id` 等（`mm-routing` 特性）— 多模态路由的输入。

**构造**：`OpenAIPreprocessor::new(mdc)` 从 ModelDeploymentCard 取出 formatter 与 tokenizer，再进 `new_with_parts`（[lib/llm/src/preprocessor.rs:1962-1968](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L1962-L1968)）；embeddings 管线改用 `new_for_embeddings`（4.2.4）。`kv_cache_block_size` 与 `media_loader` 都在 `new_with_parts_inner` 里从卡片落位（[lib/llm/src/preprocessor.rs:2029-2029](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L2029-L2029)、[lib/llm/src/preprocessor.rs:2064-2067](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L2064-L2067)）。

**主流程**（[lib/llm/src/preprocessor.rs:2310-2436](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L2310-L2436)）：模板段与分词段分别用 `TEMPLATE_SECONDS` / `TOKENIZE_SECONDS` 打点，并用 NVTX range 标注（`preprocess.template`、`preprocess.tokenize`），可被 nsys 这类剖析器直接看到。`nvext.router` 若存在则原样挂到 builder 上（[lib/llm/src/preprocessor.rs:2379-2383](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L2379-L2383)）。末段（[lib/llm/src/preprocessor.rs:2399-2428](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L2399-L2428)）在 `max_tokens` 缺省时补一个「上下文剩余量」默认值并复查预算——门卫逻辑集中在 `validate_preprocessed_token_budget`（[lib/llm/src/preprocessor.rs:1445-1445](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L1445-L1445)）与 `validate_requested_token_budget`（[lib/llm/src/preprocessor.rs:1396-1440](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L1396-L1440)），错误类型是 `InvalidArgument`（HTTP 400 语义，而非 500）。

**分词三分支**：`gather_tokens`（[lib/llm/src/preprocessor.rs:3528-3657](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L3528-L3657)）按 `request.prompt_input_type()` 分流：

1. 客户端直接给 token（`PromptInput::Tokens`）：原样采用，完全跳过分词（L3545-3563）；
2. `nvext.token_data` 存在：外部组件（如 GAIE 的 EPP KV 路由器）已经分好词，直接复用并打日志 `[SIDECAR-SKIP-TOKENIZE]`（[lib/llm/src/preprocessor.rs:3581-3604](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L3581-L3604)）；附带一个兼容分支：设了 `backend_instance_id` 却没给 `token_data` 时警告并回落到本地分词（L3605-3616）；
3. 普通文本：`encode_prompt_with_timing` 分词（[lib/llm/src/preprocessor.rs:3696-3696](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L3696-L3696)）——若渲染结果带特殊 token 边界（segments）就按段编码，避免 BPE 把 `<|im_end|>` 这类特殊 token 切碎。

**模板**：`apply_template`（[lib/llm/src/preprocessor.rs:2695-2726](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L2695-L2726)）只在「文本输入」时渲染，且支持 `nvext.use_raw_prompt` 直接用原始 prompt 绕过模板（completions 端点走这条路，[lib/llm/src/preprocessor.rs:2742-2755](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L2742-L2755)）；它同时是 `continue_final_message` 的接线点（见 4.2.2）。

**挂接点**：`OpenAIPreprocessor` 实现为流水线上的流式算子，chat 流式入口在（[lib/llm/src/preprocessor.rs:6281-6296](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L6281-L6296)）：输入 `NvCreateChatCompletionRequest`，先做思考模式归一化，调用 `preprocess_request_with_options`（L6354-6366）得到 `PreprocessedRequest` 交给下游引擎，随后立刻做工具路由决策与 nvext 校验（L6374-6383，见 4.6），并在 trace 级日志里打印整条整形结果（L6385）。也就是说它天然是 u3-l3 引擎流水线上的一个「前置算子 + 后置流改写器」。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：确认主流程各阶段的真实顺序与门卫位置。
2. **操作步骤**：打开 [lib/llm/src/preprocessor.rs:2310-2436](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L2310-L2436)，抄下 ①~⑤ 每一步的函数名与行号；再打开 [lib/llm/src/preprocessor.rs:3528-3657](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L3528-L3657) 找出三分支各自的判断条件。
3. **需要观察的现象**：`builder.token_ids(token_ids)`（L2373）在 `gather_multi_modal_data_with_image_tokens` 之后、`builder.build()` 之前。
4. **预期结果**：你能回答「为什么 token_ids 的装配要放在媒体收集之后」——注释原文是 MM 路由需要先借用 `token_ids` 做占位符对齐。
5. 本实践为静态阅读，无需运行（若想看运行效果，可在 u1-l2 的 sample 服务上发一条请求，用 `RUST_LOG=dynamo_llm=trace` 观察 `Pre-processed request` 日志，L6385）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `nvext.token_data` 存在时可以跳过分词？什么场景会用到？
**答案**：GAIE 拓扑下 EPP 侧的 KV 路由器已经为了计算块哈希对 prompt 分过词（token 是路由的输入），把结果随请求带回可以避免前端重复分词（L3581-3587 的注释）。

**练习 2**：`validate_requested_token_budget` 拒绝请求时返回的错误类型是什么？这意味着客户端会收到什么？
**答案**：`ErrorType::InvalidArgument`（L1428-1437），HTTP 层映射为 400，表示是调用方的参数问题而不是服务端故障。

**练习 3**：`MultimodalCounts::from_preprocessed` 统计的是什么？它依赖 `mm-routing` feature 吗？
**答案**：统计请求里 `image_url` / `video_url` / `audio_url` 三类内容部件的个数，用于指标标注；它从 `multi_modal_data` 派生，因此与 `mm-routing` 特性无关（[lib/llm/src/preprocessor.rs:936-961](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L936-L961)，`from_preprocessed` 在 L947）。

---

### 4.2 prompt.rs：模板粘合层、continue_final_message 与 embeddings 分词（本次补入）

#### 4.2.1 概念说明

模板渲染的「通用引擎」放在独立的 `dynamo_renderer` crate（不依赖运行时），而 `lib/llm/src/preprocessor/prompt.rs` 只保留**必须贴着 lib/llm 生活的胶水**：给 Dynamo 的 `Nv*` 请求包装实现 `OAIChatLikeRequest` trait、用 `MediaRequestExt` 把媒体 IO 配置挡在渲染 trait 之外、把 ModelDeploymentCard 适配成 `PromptFormatter`（[lib/llm/src/preprocessor/prompt.rs:4-15](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/prompt.rs#L4-L15)）。这个文件还承载两个「请求整形特例」：消息规范化与 `continue_final_message`。

embeddings 则是另一条平行管线：`/v1/embeddings` 请求不走 chat 模板，直接分词 + 可选右截断，产出 `PreprocessedEmbeddingRequest`。它由专门的构造器 `new_for_embeddings`（#12157）装配，且必须与 Python 侧 worker 的分词**逐 token 一致**（parity 测试见 u8-l8）。

#### 4.2.2 核心流程

**continue_final_message 的三段式**（与 HF Transformers 语义对齐）：

```text
① 请求带 continue_final_message=true
     ↓ apply_template 识别到（preprocessor.rs:2707）
② NormalizedArgsRequest::messages() 把 CONTINUE_FINAL_MESSAGE_TAG 追加到最后一条消息的
   文本尾部（prompt.rs:497-520；接线在 preprocessor.rs:1156-1164）
     ↓ MiniJinja 渲染（标记随文本进入渲染结果）
③ apply_continue_final_message 在渲染结果里 rfind 标记，截到标记处并丢弃其后所有内容
   （prompt.rs:534-549）→ prompt 恰好「停在最后一条消息的文本末尾、未闭合」
```

**embeddings 分词管线**（[lib/llm/src/preprocessor.rs:3737-3820](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L3737-L3820)）：

```text
new_for_embeddings(mdc)            # 校验模型支持 embedding，构造 EmbeddingTokenizerState
     ↓
preprocess_embedding_request(req)  # 不走 chat 模板！
   input 为字符串/字符串数组 → embedding_tokenizers.tokenizer(add_special_tokens).encode
   input 为 token 数组      → 原样采用（不再改动）
     ↓
truncate(limit)                    # 右截断：保留前 N 个 token（L3791-3797）
     ↓
PreprocessedEmbeddingRequest { token_ids, encoding_format, dimensions, ... }
```

#### 4.2.3 源码精读

**消息规范化**：`normalize_tool_call_arguments`（[lib/llm/src/preprocessor/prompt.rs:175-232](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/prompt.rs#L175-L232)）把历史 `tool_calls[*].function.arguments` 从 JSON 字符串解析成对象（GLM-5.2 的模板会对它调 `.items()`）；其中 `parse_args_object`（[lib/llm/src/preprocessor/prompt.rs:45-81](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/prompt.rs#L45-L81)）做了一件细致的事——**会因 f64 往返而失真的数字（如 30 位整数）保留原始拼写为字符串**，模板拿到的仍是调用方写的值。空串规范化为 `{}`，非法 JSON 返回错误而非伪造空参数（对应测试 L608-647）。

**为什么不直接搜「最后一条消息的原文」来截断**：`CONTINUE_FINAL_MESSAGE_TAG`（[lib/llm/src/preprocessor/prompt.rs:493-493](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/prompt.rs#L493-L493)）被追加到**最后一条消息**的文本尾部（数组内容取最后一个非空 `text` 部件，[lib/llm/src/preprocessor/prompt.rs:497-520](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/prompt.rs#L497-L520)）；渲染后在结果里 `rfind` 这个标记（[lib/llm/src/preprocessor/prompt.rs:534-549](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/prompt.rs#L534-L549)）。注释点明原因：模板可能**改写或重复**早先轮次的文本，搜原文无法证明命中的是最后一轮；搜唯一标记才行。若模板把标记尾随的空格裁掉了，则额外 rstrip 前缀（对齐 Transformers）。找不到标记、末条内容为空、模板把标记丢了——都按 400 拒绝而不是悄悄返回闭合的 prompt。

**保留分段边界**：`truncate_rendered_prompt`（[lib/llm/src/preprocessor/prompt.rs:553-587](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/prompt.rs#L553-L587)）截断时保持 `RenderedSegment` 的信任边界（Kimi K3 XTML 的控制 token span），这样分词仍走 `encode_segments` 而不会摊平成普通文本。

**与 add_generation_prompt 的互斥**：`should_add_generation_prompt` 在 `continue_final_message == Some(true)` 时直接返回 false（[lib/llm/src/preprocessor/prompt.rs:274-283](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/prompt.rs#L274-L283)）——「让模型接着写」与「追加新的 assistant 起始标记」天然矛盾，校验层已拒绝该组合，这里是给跳过校验的内部调用方兜底。

**接线**：`apply_template` 只在 `normalize_tool_call_args` 或 `continue_final` 打开时才包一层 `NormalizedArgsRequest`（[lib/llm/src/preprocessor.rs:2707-2716](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L2707-L2716)），渲染完再调 `apply_continue_final_message` 收尾（L2720-2725）。`NormalizedArgsRequest::messages()` 是两处改写的落点（[lib/llm/src/preprocessor.rs:1144-1165](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L1144-L1165)）。

**formatter 的来源**：`prompt_formatter_from_mdc`（[lib/llm/src/preprocessor/prompt.rs:379-485](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/prompt.rs#L379-L485)）优先用 `config.json` 的 `model_type`（作者钦定、不受 `--served-model-name` 改名影响）匹配 Kimi K3 / DeepSeek 的原生 Rust formatter；其余模型加载 HF `tokenizer_config.json` 的 Jinja 模板，并支持模板单独存文件（`.jinja` 或包一层 JSON 的 `chat_template` 字段）。

**embeddings 构造器与双分词器**：`new_for_embeddings`（[lib/llm/src/preprocessor.rs:1971-1980](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L1971-L1980)）先确认模型具备 embedding 能力，再构造 `EmbeddingTokenizerState`。后者（[lib/llm/src/preprocessor.rs:1246-1289](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L1246-L1289)）按「带/不带特殊 token」两个变体惰性初始化分词器（`OnceLock` + 初始化锁，双检查），`add_special_tokens` 的缺省值来自 `embedding_add_special_tokens_env()` 环境变量（L1051）；底层走 `model_card.rs` 的 `embedding_tokenizer_with_options`（[lib/llm/src/model_card.rs:1259-1264](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/model_card.rs#L1259-L1264)）。

**右截断契约**：`embedding_truncation_limit`（[lib/llm/src/preprocessor.rs:3822-3863](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L3822-L3863)）：`truncate_prompt_tokens` 缺省 → 不截；`< -1` → 400；`-1` → 用模型上下文长度（此时必须已配置，否则 400）；正数 → 不能超过 `max_model_len`。**调用方直接给 token 数组时不截断**（L3837-3840 注释：token 已预处理，不再改动）。截断发生在 [lib/llm/src/preprocessor.rs:3791-3797](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L3791-L3797)，注释明说「intentionally follows right truncation: preserve the first N tokens」——语义是「保住开头，扔掉结尾」，与 OpenAI embeddings 的惯例一致。

#### 4.2.4 代码实践

1. **实践目标**：用仓库自带的测试验证 `continue_final_message` 的边界行为与右截断语义。
2. **操作步骤**：
   a. 阅读 [lib/llm/src/preprocessor/prompt.rs:708-915](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/prompt.rs#L708-L915) 的测试组，重点三个：`continue_final_message_marker_survives_rewritten_final_turn`（模板改写过最后轮次时仍截对位置，L796-804）、`continue_final_message_preserves_segment_boundaries`（截断后仍保持分段，L854-890）、`continue_final_message_whitespace_only_does_not_match_structural_spaces`（纯空白内容不会误配早先的结构性空格，L807-826）；
   b. 运行 `cargo test -p dynamo-llm continue_final_message`（macOS 需按 AGENTS.md 加 `--no-default-features`，除非目标依赖 `block-manager`）；
   c. 再运行 `cargo test -p dynamo-llm embedding_truncation`（或 grep 测试名确认后运行），对照 4.2.3 的四条边界。
3. **需要观察的现象**：`continue_final_message` 相关测试全部通过；若有失败，先确认是否是特性开关问题而不是语义问题。
4. **预期结果**：你能口头回答「为什么标记必须由 Dynamo 追加、而不能搜请求里最后一条消息的原文」以及「`truncate_prompt_tokens=-1` 与不传该参数的区别」。
5. 若本机未构建 Rust workspace，标注「待本地验证」，以静态阅读测试断言为准——断言本身就是行为规范。

#### 4.2.5 小练习与答案

**练习 1**：`continue_final_message=true` 且模板把标记整个吞掉（渲染结果里找不到），会发生什么？
**答案**：`apply_continue_final_message` 找不到标记时 `bail!`（[lib/llm/src/preprocessor/prompt.rs:537-539](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/prompt.rs#L537-L539)），`apply_template` 把它转成 `invalid_argument_error`（400，[lib/llm/src/preprocessor.rs:2723-2725](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L2723-L2725)）——宁可拒绝也不返回一个「看似正常但其实是闭合 prompt」的结果。

**练习 2**：embeddings 请求给了 `input=[101, 2035, ...]`（token 数组）同时给了 `truncate_prompt_tokens=8`，会截断吗？
**答案**：不会。`embedding_truncation_limit` 对非文本输入直接返回 `None`（L3837-3840），注释写明调用方给的 token 已经过预处理、不再改动；截断只对文本输入分词后的结果生效。

**练习 3**：为什么 embeddings 需要两个「分词器变体」，而 chat 管线只有一个？
**答案**：`add_special_tokens` 会改变 token 序列本身（是否加 BOS/EOS）。embeddings 的语义由调用方决定（OpenAI 的 `add_special_tokens` 参数），所以按需准备两种配置的分词器并惰性初始化；chat 管线的模板已经显式写出了特殊 token，统一按模板分词即可。

---

### 4.3 媒体加载与 fetch policy：抓用户 URL 时的安全底线

#### 4.3.1 概念说明

图片以 URL（而非字节）随请求到达后，有两种处理方式：**前端解码**（`media_loader` 存在时，前端自己抓取、解码成像素张量、注册进 NIXL，供 worker 直接 RDMA 取走）和 **URL 透传**（前端只做安全检查与维度探测，抓取交给后端）。两条路径共用同一套 SSRF 策略。

Dynamo 在两处实现了这套策略：Rust 的 `MediaFetcher`（前端解码 + 维度探测路径）和 Python 的 `UrlValidationPolicy`（Python 后端解码路径）。两边维护**逐条对齐**的黑名单，注释互相点名要求同步修改（Rust 侧 loader.rs L37、L72 明确指向 Python 文件）。

解码本身不是一坨：`MediaLoader` 调用的 `ImageDecoder` 在 `preprocessor/media/decoders/image.rs` 的后端体系（`backends/`）里选择 `image_reader`（`image` crate/PIL 系路径）或 `turbojpeg`（libjpeg-turbo 绑定）。本讲只讲到「解码入口与选择发生在哪」，后端的性能与兼容性细节（CMYK、并行解码、基准）单独成篇 u4-l5。

#### 4.3.2 核心流程

一次图片抓取的安检流水（`fetch_and_decode_media_part`）：

```text
check_if_url_allowed_with_dns(url)  # 异步全查：静态规则 + DNS 解析，逐 IP 对照黑名单
        ↓
EncodedMediaData::from_url(...)     # 用共享 reqwest::Client 抓字节
        ↓   （该 Client 自带：重定向逐跳复检 + BlocklistResolver 连接期过滤）
ImageDecoder::decode_async(data)    # 解码为像素（形状 [H, W, C]）
        ↓   （内部选后端：enable_libjpeg 且 turbojpeg 支持该格式 → turbojpeg，
        ↓     否则/被拒 → image_reader 回退；见 decoders/image.rs:132 起）
into_rdma_descriptor(&nixl_agent)   # 注册进 NIXL，产出可 RDMA 传输的描述符
        ↓
LoaderCache（按字节数预算的 LRU，可选）
```

**三道防线的层次**：静态检查（不等 DNS）→ 请求前 DNS 复查（防 DNS 记录指向内网）→ 连接期解析器过滤（防「检查与连接之间 DNS 被换」的 rebinding：reqwest 自定义 resolver 直接把黑名单 IP 从解析结果里滤掉，reqwest 根本不知道这些地址存在）。

URL 透传路径还有一个轻量表亲：`fetch_image_dims` 只发 Range 请求抓前 64 KB 解析图片头拿宽高（路由要按尺寸折算 token 数），结果按 `(mm_hash, dimension_policy)` 缓存（[lib/llm/src/preprocessor.rs:3347-3358](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L3347-L3358) 的注释）。

#### 4.3.3 源码精读

**黑名单**（与 Python 侧逐条相同）：`BLOCKED_IP_NETWORKS` 覆盖 RFC1918 私网、CGNAT、环回、链路本地（含云元数据 `169.254.169.254`）、IPv6 保留段等 20 个 CIDR（[lib/llm/src/preprocessor/media/loader.rs:37-63](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/loader.rs#L37-L63)）；`BLOCKED_HOSTS` 按字面匹配 `localhost`、`metadata.google.internal`、`kubernetes.default.svc` 等内部服务名（[lib/llm/src/preprocessor/media/loader.rs:72-89](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/loader.rs#L72-L89)）——后者防御 `/etc/hosts` 伪造与恶意解析器。

**策略对象 `MediaFetcher`**（[lib/llm/src/preprocessor/media/loader.rs:104-123](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/loader.rs#L104-L123)）：默认 `allow_direct_ip=false`、`allow_direct_port=false`、`allow_private_ips=false`。三个开关由环境变量 `DYN_MM_ALLOW_INTERNAL=1` 一次性打开（`from_env`，[lib/llm/src/preprocessor/media/loader.rs:148-160](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/loader.rs#L148-L160)）——这是给「前端与图片服务同在内网」的本地部署留的逃生门，注释用加粗警告**绝不能**开在公网服务上（注意 `allow_private_ips` 同时控制 IP 段与主机名两道黑名单）。

**异步全查 `check_if_url_allowed_with_dns`**（[lib/llm/src/preprocessor/media/loader.rs:256-290](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/loader.rs#L256-L290)）：内部先做同步快查 `check_if_url_allowed`（协议 → IP 直连 → 端口 → 主机名黑名单 → CIDR → 可选域名白名单，[lib/llm/src/preprocessor/media/loader.rs:195-254](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/loader.rs#L195-L254)），再对域名做 `lookup_host`，**每个**解析出的 IP 都过一遍 `is_blocked_ip`。策略违规统一构造成 `InvalidArgument` 的 `DynamoError`（`policy_rejection`），HTTP 层映射为 400。

**带安检的 HTTP 客户端 `build_http_client`**（[lib/llm/src/preprocessor/media/loader.rs:292-325](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/loader.rs#L292-L325)）：重定向策略是自定义的——最多 3 跳，且**每一跳的目标 URL 重新过一遍 `check_if_url_allowed`**，防止「公网 URL 302 跳内网」。DNS 解析器换成 `BlocklistResolver`（[lib/llm/src/preprocessor/media/loader.rs:327-357](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/loader.rs#L327-L357)）：解析结果先滤掉黑名单 IP，滤到空就报策略错误——连接期防线。

**抓取与缓存 `fetch_and_decode_media_part`**（[lib/llm/src/preprocessor/media/loader.rs:515-623](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/loader.rs#L515-L623)）：图片路径上，安检通过后才 `from_url` 抓字节、`decode_async` 解码；产物经 `into_rdma_descriptor` 注册 NIXL。可选的字节预算 LRU 缓存（`DYN_MULTIMODAL_LOADER_CACHE_GB`，默认 0 关闭）命中时同时跳过网络与解码。视频需要 `media-ffmpeg` 特性，音频尚不支持。

**解码后端选择的入口**：`MediaLoader::new` 会先调用 `media_decoder.warn_if_unavailable_backends()`（[lib/llm/src/preprocessor/media/loader.rs:457-459](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/loader.rs#L457-L459)，实现在 [lib/llm/src/preprocessor/media/decoders.rs:53-58](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders.rs#L53-L58)）——请求了 libjpeg 后端但运行环境没有该库时提前告警。真正的选择发生在 `ImageDecoder::decode`（[lib/llm/src/preprocessor/media/decoders/image.rs:132-145](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders/image.rs#L132-L145)）：`enable_libjpeg && turbojpeg.supports(format)` 时先试 turbojpeg，被拒/失败则回落 `image_reader`；两个后端实现为 `ImageDecodeBackend` trait 的对象（[lib/llm/src/preprocessor/media/decoders/image/backends.rs:130-130](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders/image/backends.rs#L130-L130)）。细节留给 u4-l5。

**URL 透传时的图片身份哈希**：`hash_image_url` 是「URL 原始字节的 XXH3-64」（[lib/llm/src/preprocessor.rs:3329-3345](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L3329-L3345)）。注释解释了两个刻意选择：不去除签名/缓存参数（`?v=2` 语义不明，宁可不猜）；带轮换签名 URL 的负载（S3/GCS/SAS）应改用 `--frontend-decoding`——那条路径对**解码后的 RGB 字节**做哈希，跨 URL 的缓存复用随之恢复（端到端链路见 u8-l9）。`MmImageEntry{mm_hash,width,height}` 由本节的抓取/维度探测路径填充（[lib/llm/src/preprocessor.rs:738-745](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L738-L745)）。

**Python 侧对照**（[components/src/dynamo/common/http/url_validator.py:85-163](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/components/src/dynamo/common/http/url_validator.py#L85-L163)）：`UrlValidationPolicy` 默认比 Rust 更严——**只允许 `https://` 和 `data:`**，`http://` 需要显式放开。`validate_url` 的逻辑与 Rust 同构：协议 → 主机名黑名单 → IP 字面量 → `loop.getaddrinfo` 逐 IP 复查。`from_env` 同样读 `DYN_MM_ALLOW_INTERNAL`，另外还有 `DYN_MM_LOCAL_PATH` 控制本地文件访问（Rust 侧没有本地路径入口，`validate_local_path` 会把 `file://` 解析后限制在白名单目录内，[components/src/dynamo/common/http/url_validator.py:166-202](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/components/src/dynamo/common/http/url_validator.py#L166-L202)）。

#### 4.3.4 代码实践

1. **实践目标**：亲眼看到恶意内网 URL 在策略层被拒绝（本地 mock，不需要网络与 GPU）。
2. **操作步骤**（Python 侧，最快路径——示例代码，非项目原有文件）：

   ```python
   # ssrf_probe.py —— 用仓库自带策略离线探测一组 URL（示例代码）
   import asyncio
   from dynamo.common.http.url_validator import UrlValidationPolicy, validate_url, UrlValidationError

   POLICY = UrlValidationPolicy.from_env()  # 默认严格策略
   URLS = [
       "https://example.com/cat.png",                    # 公网 https：应通过
       "http://example.com/cat.png",                     # http：默认被拒
       "http://169.254.169.254/latest/meta-data/",       # 云元数据：IP 黑名单
       "http://10.0.0.5/x.jpg",                          # RFC1918 私网
       "https://metadata.google.internal/x",             # 内部主机名
       "file:///etc/passwd",                             # 协议不允许
       "data:image/png;base64,iVBORw0KGgo=",             # data: 应通过
   ]
   for u in URLS:
       try:
           asyncio.run(validate_url(u, POLICY))
           print(f"PASS  {u}")
       except UrlValidationError as e:
           print(f"REJECT {u}  ->  {e}")
   ```

   在装好 `ai-dynamo`（或把 `components/src` 加入 `PYTHONPATH`）的环境运行 `python ssrf_probe.py`。
3. **需要观察的现象**：7 条 URL 中恰好 2 条 PASS（`https://` 与 `data:`），其余各自命中不同的拒绝分支；拒绝信息里能明确看到原因（blocked range / not allowed / …）。
4. **预期结果**：`169.254.169.254` 走「IP 字面量在黑名单段」分支，`metadata.google.internal` 走主机名黑名单分支——与 4.3.3 的源码分析一一对应。
5. Rust 侧对应验证：`cargo test -p dynamo-llm test_direct_ip_blocked test_blocked_ip_literal test_blocked_hostname_rejected test_allow_private_ips_bypasses_blocklist`（这些用例在 [lib/llm/src/preprocessor/media/loader.rs:989-1132](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/loader.rs#L989-L1132)，纯策略测试、无需 NIXL）。若本机未编过 Rust workspace，标注「待本地验证」并先以 Python 结果为准。

#### 4.3.5 小练习与答案

**练习 1**：攻击者把公网图片 URL 302 重定向到 `http://127.0.0.1:8080/admin`，会被哪一道防线拦住？
**答案**：重定向自定义策略——`build_http_client` 的 redirect policy 对每一跳目标重跑 `check_if_url_allowed`，`127.0.0.1` 在 `127.0.0.0/8` 黑名单段，`attempt.error()` 直接终止；单测 `test_redirect_to_blocked_target_is_terminal_and_makes_no_connection` 断言了目标端口甚至收不到连接（[lib/llm/src/preprocessor/media/loader.rs:1198-1241](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/loader.rs#L1198-L1241)）。

**练习 2**：`BlocklistResolver` 与 `check_if_url_allowed_with_dns` 都检查 DNS 结果，为什么两道都要？
**答案**：前者防 rebinding（检查之后、连接之前 DNS 答案被换）；后者在发起请求前更早拒绝，语义更清晰且 Python 侧只有等价的后一种。纵深防御：任何一道被绕过（如 reqwest 行为变化）另一道仍兜底。

**练习 3**：为什么 Rust 默认允许 `http://` 而 Python 默认不允许？
**答案**：Rust 前端运行在集群内、图片源通常是受控对象存储；Python 侧 `UrlValidationPolicy` 服务于更靠外的后端解码路径，默认只信 `https://` 与 `data:`，需要内网 http 时用 `DYN_MM_ALLOW_INTERNAL=1` 显式放开（[components/src/dynamo/common/http/url_validator.py:85-101](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/components/src/dynamo/common/http/url_validator.py#L85-L101) 的类 docstring 与 `from_env`）。

**练习 4**：带签名参数轮换的 S3 URL 为什么会破坏 URL 透传路径的缓存复用？官方推荐的解法是什么？
**答案**：透传路径用 `hash_image_url`（URL 原始字节）当图片身份，同一张图的每个签名 URL 都不同 → 身份不同 → 无法复用；官方解法是 `--frontend-decoding`，改对解码后的 RGB 字节做哈希（[lib/llm/src/preprocessor.rs:3333-3341](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L3333-L3341) 注释原文推荐）。

---

### 4.4 LocalModel：模型落地与「块大小」从哪来

#### 4.4.1 概念说明

`LocalModel` 是「一个已在本地的模型」的完整描述：磁盘路径 + ModelDeploymentCard（MDC，模型部署卡，携带 tokenizer、chat 模板、运行时配置、媒体解码器等）。预处理器的一切家当（formatter、tokenizer、块大小、媒体加载器）最终都来自它。它由 `LocalModelBuilder` 构建，Python 侧的 `EntrypointArgs` 透传参数给这个 builder——这是 u2-l2「PyO3 薄壳」模式的又一次体现。

#### 4.4.2 核心流程

```text
Python: EntrypointArgs(kv_cache_block_size=...)     # lib/bindings/python/rust/llm/entrypoint.rs:594
   ↓ PyO3
LocalModelBuilder::kv_cache_block_size(...)          # local_model.rs:151（None → 默认 16）
   ↓ build()
LocalModel { card: ModelDeploymentCard, ... }        # local_model.rs:436
   ↓ OpenAIPreprocessor::new(mdc)
预处理器拿到 tokenizer / formatter / kv_cache_block_size / media_loader
```

模型本体按需拉取：`LocalModel::fetch(remote_name, ignore_weights)` 走 HuggingFace 下载，`ignore_weights=true` 时只下配置类文件（前端不需要权重，只需要 tokenizer/模板/配置）（[lib/llm/src/local_model.rs:513-520](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/local_model.rs#L513-L520)）。

#### 4.4.3 源码精读

- **默认块大小**：`DEFAULT_KV_CACHE_BLOCK_SIZE: u32 = 16`——引擎不提供时 Dynamo 的兜底值（[lib/llm/src/local_model.rs:33-38](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/local_model.rs#L33-L38)，常量在 L34）。它必须与 worker 引擎的 KV page 大小一致，否则前端算出的块哈希与 worker 上报的对不上号。
- **builder 字段**（[lib/llm/src/local_model.rs:56-92](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/local_model.rs#L56-L92)）：`model_path` / `source_path` / `template_file` / `kv_cache_block_size` / `media_decoder` / `media_fetcher` / `runtime_config` 等；setter `kv_cache_block_size` 传 `None` 即重置为默认（[lib/llm/src/local_model.rs:151-152](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/local_model.rs#L151-L152)）。
- **LocalModel 本体**（[lib/llm/src/local_model.rs:436-455](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/local_model.rs#L436-L455)）：核心是 `card: ModelDeploymentCard` 字段，`display_name`/`service_name`/`path` 等都是便捷访问器。
- **分词器加载**：`ModelDeploymentCard::tokenizer()`（[lib/llm/src/model_card.rs:1255-1264](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/model_card.rs#L1255-L1264)）按 `TokenizerOptions` 构造，支持多后端与回退；`DYN_TOKENIZER_CACHE` / `DYN_TOKENIZER_CACHE_BYTES` / `DYN_TOKENIZER_CACHE_EXTEND` 环境变量控制前缀缓存（[lib/llm/src/model_card.rs:1278-1286](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/model_card.rs#L1278-L1286)）。
- **Python 参数入口**：PyO3 签名里 `kv_cache_block_size=None`（[lib/bindings/python/rust/llm/entrypoint.rs:594-594](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/bindings/python/rust/llm/entrypoint.rs#L594-L594)），随后 `.kv_cache_block_size(args.kv_cache_block_size)` 落到 builder（[lib/bindings/python/rust/llm/entrypoint.rs:741-741](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/bindings/python/rust/llm/entrypoint.rs#L741-L741)）。

#### 4.4.4 代码实践（参数链追踪）

1. **实践目标**：把「Python 参数 → builder → 预处理器」的传递链亲手走一遍。
2. **操作步骤**：从 [lib/bindings/python/rust/llm/entrypoint.rs:594](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/bindings/python/rust/llm/entrypoint.rs#L594-L594) 的签名出发，依次 `Grep` `kv_cache_block_size` 在 `lib/` 下的全部出现点，给每一处标注它在链上的角色。
3. **需要观察的现象**：值沿着 `EntrypointArgs.new → LocalModelBuilder → LocalModel/MDC → OpenAIPreprocessor::kv_cache_block_size` 单向流动，没有任何回头修改。
4. **预期结果**：得到一张 5 节点的传递链图；并回答：不传该参数时前端用什么块大小切块？（16）
5. 纯静态追踪，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：前端把 `kv_cache_block_size` 配成 32，而 worker 引擎的 page 是 16，会发生什么？
**答案**：前端切出的块与 worker 按 16 切的块边界不重合，双方计算/上报的块哈希无法对齐——KV 感知路由会把这些块当作不匹配，前缀复用失效（最坏情况是错误复用）。所以该值必须与引擎一致，`build_mm_exact_routing_info` 里块大小为 0 时直接放弃 MM 路由（[lib/llm/src/preprocessor.rs:3246-3253](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L3246-L3253)）也体现了「块大小不可靠宁可降级」的思路。

**练习 2**：`LocalModel::fetch(name, true)` 的 `ignore_weights=true` 适合谁用？
**答案**：前端/路由器——它们只需要 tokenizer、模板与配置来整形请求和算哈希，不需要几 GB 的权重；worker 才需要完整权重。

---

### 4.5 token 块：TokenBlockSequence 与 block 化

#### 4.5.1 概念说明

`lib/tokens`（crate 名 `dynamo-tokens`）解决一个问题：把一串 token id 切成固定大小的块并为每块算出稳定身份。为什么必须块化？因为 KV cache 的分配、迁移、去重、路由匹配全都以块为单位；「请求 A 的前 3 块与请求 B 的前 3 块相同」是可执行的路由判断，而「两个 prompt 文本相似」不是。

三种哈希身份（记住这套命名，u6/u9 会反复出现）：

| 哈希 | 定义 | 回答的问题 |
|---|---|---|
| `BlockHash` (u64) | `XXH3_64(块内 token 字节, salt_hash)` | 这一块内容是什么 |
| `SequenceHash` (u64) | 从第 0 块起链式累积 | 从序列开头到这块的完整路径 |
| `PositionalLineageHash` (u128) | 打包（位置, 当前序列哈希, 父哈希片段） | 树上哪个节点（自包含、可直接扩展） |

#### 4.5.2 核心流程与数学

**块哈希**：token 是 `u32`（[lib/tokens/src/lib.rs:22-23](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/tokens/src/lib.rs#L22-L23)），一个 N 个 token 的块被 `bytemuck::cast_slice` 原样重解释为 4N 字节的小端字节流，然后用请求级种子做 XXH3：

\[
\text{block\_hash}_i = \text{XXH3\_64}\big(\text{le\_bytes}(\text{tokens}_i),\ \text{salt\_hash}\big)
\]

（实现：[lib/tokens/src/lib.rs:147-163](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/tokens/src/lib.rs#L147-L163)）

**链式序列哈希**：第 0 块的序列哈希等于其块哈希；之后每块把「父序列哈希 + 本块块哈希」两个 u64 拼成 16 字节再哈希，种子是常量 `CHAIN_XXH3_SEED = 1337`：

\[
\text{seq}_0 = \text{block\_hash}_0,\qquad
\text{seq}_i = \text{XXH3\_64}\big(\text{le\_bytes}([\text{seq}_{i-1},\ \text{block\_hash}_i]),\ 1337\big)
\]

（[lib/tokens/src/lib.rs:61-83](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/tokens/src/lib.rs#L61-L83)）

**关键推论（本讲最重要的一句话）**：哈希只依赖「本块内容 + 父链」。因此两条请求只要拥有相同的前缀 token，它们前缀里的每个完整块都会得到**逐块相同**的 `block_hash` 与 `sequence_hash`——与后面说什么完全无关。这就是「路由器凭块哈希知道哪个 worker 已缓存了这段前缀」的全部数学基础。

**切分流程**（`TokenBlockSequence::split_tokens`，[lib/tokens/src/lib.rs:1672-1715](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/tokens/src/lib.rs#L1672-L1715)）：

```text
tokens.chunks_exact(block_size)        # 每 block_size 个一块
  → TokenBlockChunk::from_tokens       #   每块先算 block_hash（可并行）
  → 顺序循环 TokenBlock::from_chunk    #   链式串起 sequence_hash（必须顺序）
  → 余数进 PartialTokenBlock           #   末尾不满一块：用 UUID 标识，不参加完整块哈希
```

**多模态块（选读）**：图片在 token 序列里表现为一串「占位符槽位」。带 MM 信息的块不走 4 字节编码，而是每个槽位输出 13 字节帧：真实 token 是 `tag=0x00 | token u32 | pad`，占位符是 `tag=0x01 | run_offset u32 | mm_hash u64`（`mm_hash` 是图片身份哈希）。两种编码每槽宽度不同（4 vs 13），天然不会碰撞（实现在 [lib/tokens/src/lib.rs:306-310](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/tokens/src/lib.rs#L306-L310) 起的 `compute_block_bytes_with_mm`）。效果：**图片之前的纯文本前缀块哈希完全不受图片影响**，仍然可以复用。

#### 4.5.3 源码精读

- **`TokenBlock::from_chunk`**（[lib/tokens/src/lib.rs:1128-1159](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/tokens/src/lib.rs#L1128-L1159)）：链递归的唯一落点——无父则序列哈希=块哈希，有父则 `compute_next_sequence_hash`。同一函数被请求侧（`split_tokens`）、流式侧（`commit_current`）与路由器复用。
- **`TokenBlockSequence`**（[lib/tokens/src/lib.rs:1234-1273](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/tokens/src/lib.rs#L1234-L1273)）：`Vec<TokenBlock>` + 一个 `PartialTokenBlock` 尾块。`extend` 流式追加 token 并在块满时提交（[lib/tokens/src/lib.rs:1290-1323](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/tokens/src/lib.rs#L1290-L1323)）——解码期逐 token 生成的场景就用它增量成块。
- **`PartialTokenBlock::commit`**（[lib/tokens/src/lib.rs:1009-1030](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/tokens/src/lib.rs#L1009-L1030)）：提交后把自己重置为「下一块」，继承刚提交块的序列哈希作为父——增量式链维护。
- **可验证的测试常量**：`hash([1,2,3,4], salt=1337) = 14643705804678351452`，第 2、3 块的链值也硬编码在测试里（[lib/tokens/src/lib.rs:1911-1975](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/tokens/src/lib.rs#L1911-L1975)，`test_validate_hash_constants` 在 L1945）——这是把「数学定义」固化成「回归基准」的好范例。
- **`UniqueBlock` 的两态**（[lib/tokens/src/blocks.rs:13-26](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/tokens/src/blocks.rs#L13-L26)）：完整块用 `FullBlock(GlobalHash)`，未满块用随机 `PartialBlock(Uuid)`——不满一块的尾巴没有稳定内容，用唯一 Id 标识即可。

#### 4.5.4 代码实践

1. **实践目标**：用仓库自带的回归测试验证 4.5.2 的公式与常量。
2. **操作步骤**：运行 `cargo test -p dynamo-tokens test_validate_hash_constants`（这是个小 crate，编译很快；也无需特殊 feature）。
3. **需要观察的现象**：测试通过，意味着 `compute_block_hash(cast_slice([1,2,3,4]), 1337)` 确实等于 14643705804678351452，且第 2 块序列哈希等于 `xxh3([seq1, hash2], 1337)`。
4. **预期结果**：3 个断言组（三块各自的 block_hash/seq_hash）全绿。想再进一步，可加跑 `token_hash_helper_matches_canonical_byte_encoding` 验证 `compute_block_hash_for_tokens` 与 `cast_slice` 路径等价。
5. 具体输出以本机为准；若 cargo 环境不可用则标注「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：请求 A 的 token 是 `[t1..t40]`，请求 B 是 `[t1..t40, u1..u5]`（block_size=16，salt 相同）。B 能复用 A 的哪些块？
**答案**：`chunks_exact(16)` 只切完整块：A 的 40 个 token 切出 2 个完整块（t1-16、t17-32），剩 8 个进 PartialBlock；B 的 45 个 token 同样只有前 2 块是完整的。这 2 块内容相同 → block_hash 与 sequence_hash 逐块相同，可复用。A、B 的第 3 块都不满 16 个 token（分别 8 个与 13 个），用 UUID 标识、不产生稳定哈希。结论：复用块数 = 2 = ⌊40/16⌋。

**练习 2**：为什么链式哈希的种子是常量 1337，而块哈希的种子是每请求的 `salt_hash`？
**答案**：salt 的隔离职责已经在第 0 块的 block_hash 里兑现（`seq_0 = block_hash_0`），并沿链传播；每一步再喂 salt 是冗余。链种子固定为 1337 是为了与 kv-router 的线格式逐字节兼容，注释明确要求改动必须多方同步（[lib/tokens/src/lib.rs:56-75](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/tokens/src/lib.rs#L56-L75)）。

**练习 3**：`PositionalLineageHash` 为什么要打包成 128 位而不是直接用 `SequenceHash`？
**答案**：序列哈希只是「内容指纹」，不含位置与父子关系；PLH 把（位置、当前序列哈希全量、父哈希片段）打进一个 u128，使得子节点可以**只凭父 PLH + 子块哈希**完成 `extend`，无需查表——这对 radix 树的插入/回溯（u6-l4）是关键性能性质（结构体与 `extend` 见 [lib/tokens/src/lib.rs:549-619](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/tokens/src/lib.rs#L549-L619)）。

---

### 4.6 nvext 透传与 tool-call jail：扩展字段不能半路蒸发

#### 4.6.1 概念说明

nvext 的四个关键字段各有用途：`extra_fields` 让调用方点播响应附加字段（`worker_id`、`engine_data`、`prompt_logprobs` 等，协议侧的可选集定义在 `NvExtResponseFieldSelection`，[lib/llm/src/protocols/common/extensions.rs:713-724](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/protocols/common/extensions.rs#L713-L724)）；`metadata_upload` 要求后端把响应元数据带外上传（SGLang 的 meta_info 通道，见 u8-l7）；`token_data` 让调用方直接给 token（对应 4.1 的分词分支 2）；`cache_salt` 是前缀缓存隔离盐（与 4.8 的盐推导衔接）。

问题在于：这些字段要么需要**跟着请求走到后端**（请求侧透传），要么是**后端贴在响应 chunk 上的数据**（响应侧保全）。而前端恰恰在响应路径上部署了一个会拆装 chunk 的组件——tool-call jail。jail 会把 N 个输入 chunk 缓冲改写成 M 个输出 chunk，每次改写都是「克隆一个 chunk 当模板」。若改写路径上 `nvext` 被置 `None`，开启了工具调用解析的模型上，RL 消费方点播的 token 序列、logprobs 等数据就会无声消失。修复分三件事：请求侧统一透传入口、响应侧让 nvext 穿狱存活、对无法安全支持的场景显式拒绝。

#### 4.6.2 核心流程

**请求侧（透传）**：

```text
nvext_passthrough_args(request)           # 收集：extra_fields / metadata_upload / token_in / cache_salt
        ↓
backend_extra_args(request, ...)          # 包成 extra_args["nvext"]（外加 sampling/推理参数）
        ↓
builder_with_lora → builder.extra_args    # 随 PreprocessedRequest 抵达后端
```

其中 `token_data` 不整包搬运，而是折叠成一个布尔 `token_in: true`——token 本身已经进入 `token_ids`（4.1 分词分支 2），后端（如 SGLang 的 handler）只需要知道「这条请求是以 token 进来的」这一事实。

**响应侧（穿狱）**：

```text
后端 chunk 带 nvext
        ↓ jail 入口（Annotated<Nv> → Annotated<Create>）
PendingDynamoMetadata 缓冲 nvext（merge_response_nvext 逐 chunk 合并）
        ↓ jail 出口（Annotated<Create> → Annotated<Nv>）
下一个「带 choice 的非 payload-usage 输出 chunk」把合并后的 nvext 带走
```

合并规则（与一元聚合器一致）：`completion_token_ids` **追加拼接**（否则 `[42]` 会变 `[42,42]`），其余顶层字段（`prompt_logprobs`、`engine_data` 等）**最新值胜出**（[lib/llm/src/protocols/common/extensions.rs:676-711](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/protocols/common/extensions.rs#L676-L711)）。

**约束（显式拒绝）**：`engine_data` / `routed_experts` / `stop_reason` 是 choice 级字段（每个候选各一份），而 legacy jail 的缓冲改写无法在 `n>1` 时把它们正确归位到各 choice——所以这种组合直接以 400 拒绝，而不是返回错乱数据。

#### 4.6.3 源码精读

**请求侧收集**：`nvext_passthrough_args`（[lib/llm/src/preprocessor.rs:1472-1501](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L1472-L1501)）逐字段搬进一个 JSON map：`extra_fields`、`metadata_upload` 原样序列化，`token_data.is_some()` 折叠成 `"token_in": true`；`cache_salt` 不是从 nvext 直接读，而是走 `request_cache_salt`（[lib/llm/src/protocols/common/extensions.rs:603-621](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/protocols/common/extensions.rs#L603-L621)）——它实现了公共优先级：`nvext.cache_salt` 是规范入口，顶层 `cache_salt`（不支持字段的请求类型保留的遗留位）是回退，空串视为未设置。

**装进内部请求**：`backend_extra_args`（[lib/llm/src/preprocessor.rs:1528-1570](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L1528-L1570)）把上面的 map 挂到 `extra_args["nvext"]`（L1535-1540），旁边还有 `sampling_options`（透传 `allowed_token_ids` 等不支持字段）与 `reasoning_parser_kwargs`。调用点在 `builder_with_lora` 尾部（[lib/llm/src/preprocessor.rs:2586-2592](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L2586-L2592)），紧挨着 LoRA/盐的路由提示装配（L2548-2584）。

**工具处理路由**：原先散落在流处理函数里的一串 if 被提炼成 `ToolProcessingRoute` 枚举（[lib/llm/src/preprocessor.rs:181-194](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L181-L194)）：`MuseUnified` / `QwenUnified`（新一代统一解析器，不改写 chunk）、`ParserV2`（实验性 v2 解析器）、`LegacyJail`（传统监狱）、`PassThrough`（什么都不做）。决策集中在 `tool_processing_route`（[lib/llm/src/preprocessor.rs:3896-3967](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L3896-L3967)），依据是解析器族、`tool_choice` 形态、structural-tag 与 K3 特例——一次决策同时供流处理与 nvext 校验使用，保证两边看到同一条路由。

**门卫**：`validate_legacy_jail_nvext_choice_count`（[lib/llm/src/preprocessor.rs:158-179](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L158-L179)）：`n>1` 且路由是 `LegacyJail` 且 `extra_fields` 点了 choice 级字段（`engine_data`/`routed_experts`/`stop_reason`）→ `invalid_argument_error`（400）。它在流式算子里紧跟 `tool_processing_route` 之后被调用（[lib/llm/src/preprocessor.rs:6374-6383](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L6374-L6383)），即引导解码约束安装之后、请求下发之前。

**响应侧穿狱**：`apply_tool_calling_jail`（[lib/llm/src/preprocessor.rs:4763-4805](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L4763-L4805)）是 dynamo-parsers 里 jail 的边界适配器（拆 `Annotated<Nv>` → 跑 jail → 再包回去）。缓冲结构 `PendingDynamoMetadata`（L4798-4805）除了原有的 `llm_metrics` 累计，还多了 `nvext`（用 `merge_response_nvext` 逐输入 chunk 合并，入口在 [lib/llm/src/preprocessor.rs:4864-4894](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L4864-L4894) 的 `jail_input` 映射）与 `response_template`（首个带元数据的 chunk 留下身份字段，如 id/model）。出口侧的投放条件见注释：指标可以搭「payload-only usage chunk」的车，而**客户端可见的 nvext 必须等到一个带 choice 的非 payload-usage 输出**才随行发出（[lib/llm/src/preprocessor.rs:4974-4993](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L4974-L4993)）。

**回归测试**：`legacy_jail_rejects_multiple_choices_for_choice_specific_nvext`（[lib/llm/src/preprocessor.rs:6787-6810](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L6787-L6810)）把三条边界钉死：`n=2 + engine_data + legacy jail` 拒绝；`n=1` 放行；`n=2 + 请求级字段（timing/worker_id）` 放行；四条非 legacy 路由在 `n=2` 下都不受此约束。

#### 4.6.4 代码实践（源码阅读型）

1. **实践目标**：沿源码走通一条「带 nvext 的请求被拒绝/放行」的判定路径。
2. **操作步骤**：
   a. 从 [lib/llm/src/preprocessor.rs:6374-6383](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L6374-L6383) 出发，向上读 `tool_processing_route`（L3896-3967），列出让一条请求落入 `LegacyJail` 而非 `ParserV2`/`PassThrough` 的全部条件；
   b. 再构造三个假想请求（写在笔记里即可）：① `n=2`、`extra_fields=["engine_data"]`、模型配置了 legacy 解析器；② 同 ① 但 `n=1`；③ 同 ① 但 `extra_fields=["worker_id","timing"]`；
   c. 运行 `cargo test -p dynamo-llm legacy_jail_rejects_multiple_choices_for_choice_specific_nvext` 验证源码阅读结论。
3. **需要观察的现象**：请求 ① 在 `validate_legacy_jail_nvext_choice_count` 处得到 400，错误消息里点名 choice-specific 字段名；② ③ 通过校验进入正常整形。
4. **预期结果**：你能说清「为什么拒绝发生在预处理阶段而不是响应阶段」——choice 级字段一旦进了 jail 就无法按 choice 归位，晚拒绝等于返回错数据，早拒绝是 400。
5. 若本机未构建 Rust workspace，标注「待本地验证」，以静态阅读为准。

#### 4.6.5 小练习与答案

**练习 1**：为什么 `completion_token_ids` 用追加合并，而 `prompt_logprobs` 用覆盖？
**答案**：`completion_token_ids` 是逐 chunk 生成的 token 流，jail 把 N 个输入 chunk 折成 M 个输出时语义是「有序拼接」；覆盖会只剩最后一个 chunk 的 token。`prompt_logprobs` 描述的是同一个 prompt 的一次性结果，逐 chunk 重复携带时只有最新值有意义，追加反而会制造重复数据（合并规则见 [lib/llm/src/protocols/common/extensions.rs:684-705](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/protocols/common/extensions.rs#L684-L705)）。

**练习 2**：请求里 `nvext.token_data` 给了一串 token，透传给后端的是什么？
**答案**：token 本体进入 `PreprocessedRequest.token_ids`（分词被跳过），透传 map 里只有一个 `"token_in": true` 布尔——后端如 SGLang 的 handler 用它识别「token 进来的请求」（消费点如 [components/src/dynamo/sglang/request_handlers/handler_base.py:1140](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/components/src/dynamo/sglang/request_handlers/handler_base.py#L1140-L1140)），语义细节在 u8-l7 展开。

**练习 3**：`cache_salt` 的「规范入口 + 回退」优先级为什么要把空串当作未设置？
**答案**：`nvext.cache_salt` 是规范字段，顶层 `cache_salt` 是部分请求类型保留的遗留位。若调用方显式传了空串（意为「不设盐」）而遗留位有旧值，把空串当有效值会意外吃到遗留盐；视空为缺，才能让「显式清空 + 隐式回退」两种用法都正确（[lib/llm/src/protocols/common/extensions.rs:603-608](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/protocols/common/extensions.rs#L603-L608) 的 docstring）。

---

### 4.7 jail 如何旁路终端错误：让类型信息活着到达客户端（#13930，本次更新）

#### 4.7.1 概念说明

jail 有一个先天缺陷：它的收尾（finalize）逻辑**分不清「上游报错导致的流终止」和「正常结束」**——对它来说两者都是普通 EOF。于是当上游出错时，jail 可能把缓冲到一半的工具调用参数**合成成一个完整的 `tool_calls` 收尾 chunk** 发给客户端：一个实际失败的请求，看起来却像正常完成了一次工具调用。

#13930 之前还有第二层问题：jail 的 `Annotated`（dynamo-parsers crate 的 `JailAnnotated`）错误槽装不下 Dynamo 的**带类型**错误——旧代码入狱时 `a.error.map(|e| e.to_string())` 把 `DynamoError` 压成字符串，出狱时 `a.error.map(DynamoError::msg)` 再用一个「泛型消息错误」装回去。类型信息（`InvalidArgument`？过载？未知？）在这一来一回中丢光。为了找回 400 语义，部分适配路径会把 `"BackendInvalidArgument: <消息>"` 编进错误字符串，HTTP 层再靠**字符串前缀**识别——一个脆弱的约定式补丁。

#13930 的做法是让终端错误**根本不进监狱**：

1. 入口 `take_while` 拦住第一个错误 `Annotated`，把它原封不动「扣留」在 `terminal_error` 槽里——jail 从未见过它，finalize 也就从不会因它而跑；
2. `JailAnnotated.error` 一律置 `None`（两端各加一条 `debug_assert`，把「dynamo-parsers 不构造错误」固化成契约）；
3. 出口再一个 `take_while` 丢掉 jail 在此之后合成的一切输出，流末尾把**原始带类型注解**链回给调用方；
4. HTTP 层因此可以直接读 `event.error.error_type()` 分类，字符串前缀恢复分支被整段删除（-26 行）。

#### 4.7.2 核心流程

```text
上游流: [chunk1, chunk2, ERROR(带类型), ...]
        │
        ▼ 入口 take_while（L4853-4861）
   ERROR 被扣进 terminal_error 槽，不再向下游传播
   → jail 看到的是 [chunk1, chunk2, EOF]（自然 EOF）
        │
        ▼ jail 正常缓冲/改写，可能合成 finalize chunk
        │
        ▼ 出口 take_while（L5149-5155）
   一旦 terminal_error 非空，jail 后续输出全部丢弃
        │
        ▼ 流末尾（L5157-5181）
   清空 pending 元数据（metrics/nvext/response_template）
   yield 原始 ERROR 注解（类型完好）→ return
```

这套契约与两个统一适配器（`unified_parser::apply_stream_with_constraint`、`tool_parser_v2::apply_stream`/`apply_unified_stream`）已有的行为对齐：终端错误出现即「yield 该错误然后 return」，其后内容全部丢弃——注释原文见 [lib/llm/src/preprocessor.rs:4835-4849](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L4835-L4849)。

#### 4.7.3 源码精读

**入口扣留**：`terminal_error` 是 `Arc<Mutex<Option<Annotated<...>>>>`；`take_while` 闭包对每个元素检查 `is_error()`，是错误就存档并返回 false（流在此截断），否则放行（[lib/llm/src/preprocessor.rs:4850-4861](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L4850-L4861)）。

**入狱转换**：`jail_input` 的 map 里，新的 `debug_assert!(a.error.is_none(), "terminal errors must bypass the jail")` 与 `error: None` 取代了旧的 `a.error.map(|e| e.to_string())`；注释写明「Terminal errors were removed by `take_while` above and are chained back as the original typed Dynamo annotation below」（[lib/llm/src/preprocessor.rs:4926-4935](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L4926-L4935)）。

**出狱转换**：对称的第二条 `debug_assert!(a.error.is_none(), "dynamo-parsers must not construct errors")`（[lib/llm/src/preprocessor.rs:4969-4973](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L4969-L4973)），重组 `Annotated` 时 `error: None`——旧代码在这里用 `DynamoError::msg` 把字符串包回泛型错误，类型就此蒸发，这个转换现在被整体跳过（[lib/llm/src/preprocessor.rs:5005-5007](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L5005-L5007)）。

**出口截断与链回**：jail 输出先过 `take_while`（只要 `terminal_error` 槽非空就停，[lib/llm/src/preprocessor.rs:5145-5155](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L5145-L5155)）；随后 `async_stream` 收尾：取出扣留的错误、**清空 pending 的 metrics/nvext/response_template**（失败的请求不该再吐 usage 或指标）、`yield error` 然后 `return`（[lib/llm/src/preprocessor.rs:5163-5181](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L5163-L5181)）。

**HTTP 层的对应简化**（`extract_backend_error_if_present`，[lib/llm/src/http/service/openai.rs:2245-2351](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/http/service/openai.rs#L2245-L2351)）：现在直接按 `event.error.error_type()` 分类——`InvalidArgument` / `Backend(InvalidArgument)` 过滤器在 L2262-2267，命中则映射 400（L2332-2337）；未知错误统一消毒为 500（L2347-2350）。旧的 `SERIALIZED_BACKEND_INVALID_ARGUMENT_PREFIX` 字符串前缀恢复分支（约 26 行）已删除——类型不再需要从字符串里「考古」。

**集成测试**：`test_deepseek_v4_upstream_typed_errors_suppress_jail_finalize`（[lib/llm/tests/test_streaming_tool_parsers.rs:2437-2495](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/tests/test_streaming_tool_parsers.rs#L2437-L2495)）把契约钉成四条断言：终端错误必须到达调用方（流非空）；它必须是**最后一个**元素且 `is_error()`；`error_type()` 与消息逐字保留（对 `Backend(InvalidArgument)`、`Backend(EngineShutdown)`、`Backend(Disconnected)` 三种类型循环验证）；整个输出里错误恰好出现一次，且聚合结果**不含任何合成的 tool_calls**。

#### 4.7.4 代码实践

1. **实践目标**：用集成测试亲证「带类型的终端错误穿过 jail 后类型不变、且 jail 不再合成 tool_calls」。
2. **操作步骤**：运行 `cargo test -p dynamo-llm --test test_streaming_tool_parsers test_deepseek_v4_upstream_typed_errors_suppress_jail_finalize`（macOS 按 AGENTS.md 加 `--no-default-features`）。若想对照旧世界，可用 `git log -S "BackendInvalidArgument: " -- lib/llm/src/http/service/openai.rs` 找到删除该分支的提交，`git show` 查看被删的 26 行。
3. **需要观察的现象**：测试通过。三个错误类型（InvalidArgument/EngineShutdown/Disconnected）各自跑一遍断言；错误是流中最后一个元素、只出现一次、类型与消息原样。
4. **预期结果**：结合源码你能回答「为什么删掉字符串前缀分支是安全的」——因为错误对象从未被字符串化，`event.error` 上的 `error_type()` 全程可用，前缀匹配这个补丁失去了存在的前提。
5. 若本机未构建 Rust workspace，标注「待本地验证」，以静态阅读断言为准。

#### 4.7.5 小练习与答案

**练习 1**：为什么出口侧要多一个 `take_while`？只有入口的 `take_while` 不够吗？
**答案**：不够。入口截断后，jail 看到的是一个「自然 EOF」的短流，它仍会正常跑 finalize、把缓冲的半截参数合成成完整的 `tool_calls` chunk 发出来。出口 `take_while` 在错误已扣留的前提下把这些合成输出全部丢弃，保证「合成的完成」永远到不了调用方（[lib/llm/src/preprocessor.rs:5145-5155](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L5145-L5155)）。

**练习 2**：两条 `debug_assert` 分别防什么？为什么用 `debug_assert` 而不是 `if ... return Err`？
**答案**：入口那条断言「进入 jail 的元素不可能带错误」（入口 take_while 已保证）；出口那条断言「dynamo-parsers 自己不会构造错误」（它是外部 crate，这条契约约束它的行为）。用 `debug_assert` 是因为这是**不变量**而非可恢复条件：release 构建里违反它也没有安全的降级路径，开发/测试构建里它会立刻炸出来暴露契约破坏（[lib/llm/src/preprocessor.rs:4926](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L4926-L4926)、[lib/llm/src/preprocessor.rs:4970-4973](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L4970-L4973)）。

**练习 3**：错误链回时为什么要清空 `PendingDynamoMetadata`（metrics/nvext/response_template）？
**答案**：这些是「成功响应的伴随数据」。若不清空，EOF 收尾逻辑可能再补发一个带 usage/指标的收尾 chunk——失败的请求携带一份看似正常的 usage 统计，会污染计费与监控（清空动作在 [lib/llm/src/preprocessor.rs:5169-5178](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L5169-L5178)）。

---

### 4.8 块哈希进阶：SaltHash、kv-hashing 封装与 MM 路由收口

#### 4.8.1 概念说明

`salt` 是**前缀缓存隔离键**：两个不该共享缓存的请求必须算出不同的盐，两个该共享的必须相同。例如同一基底模型挂不同 LoRA 适配器时，KV 内容不同，必须隔离。`lib/kv-hashing` crate 在 `dynamo-tokens` 之上提供统一的 `Request` 入口与盐推导，保证「前端算的块哈希」与「路由器/worker 算的块哈希」字节级一致——这是跨进程协作的硬契约，任何一侧单独改动都会让缓存查找失配。它和 4.6 也有交接：请求里的 `nvext.cache_salt` 正是盐的调用方入口之一。

#### 4.8.2 核心流程

**盐推导**（[lib/kv-hashing/src/salt.rs:49-69](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/kv-hashing/src/salt.rs#L49-L69)）：

\[
\text{salt\_hash} = 1337 \;\;{\boxed{+}}\;\; \big[\text{XXH3}(\text{lora\_name}, 0)\big] \;\;{\boxed{+}}\;\; \big[\text{XXH3}(\text{salt}, 1)\big]
\]

（`+` 为 wrapping 加；空字符串归一化为 None；方括号表示该项存在时才加。）文件头部的注释给出了与路由器 `compute_block_hash_for_seq` 的对照表——同公式同种子，这就是「字节级一致」的出处（[lib/kv-hashing/src/salt.rs:16-20](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/kv-hashing/src/salt.rs#L16-L20)）。注意**多模态数据不进盐**：它被折叠进逐块哈希，这样「同一张图在同一位置」的请求仍能共享图片之前的文本前缀块。

**统一入口**：`kv_hashing::Request { tokens, lora_name, salt, mm_info }` 用 builder 构造，`build()` 时校验并排序 `mm_info`（无重叠、不越界、非零长度）（[lib/kv-hashing/src/request.rs:68-129](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/kv-hashing/src/request.rs#L68-L129)）；每块的产出被封装为 `UniversalBlock { block_hash, plh }`，其中 PLH 内嵌完整 u64 序列哈希，调用方不再需要侧表（[lib/kv-hashing/src/block.rs:23-33](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/kv-hashing/src/block.rs#L23-L33)）。

#### 4.8.3 源码精读：MM 路由信息——整条流水线的收口

`build_mm_exact_routing_info`（[lib/llm/src/preprocessor.rs:3215-3327](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L3215-L3327)）把本讲所有零件串成一件事：**为路由器构造一条与 worker 侧完全同构的 token 序列**。流程：

1. 门槛检查：`routing_image_token_id`（图片占位符的 token id）、`routing_image_prompt_layout`、`image_token_counter`、`kv_cache_block_size`（为 0）任一缺失就返回 `None`，路由降级为纯文本前缀匹配（L3225-3253）；
2. 对齐检查：token 序列里占位符 token 的个数必须等于图片张数，否则放弃（L3260-3271）；
3. 每张图按 `(width, height)` 用 `image_token_counter.count_tokens` 算出折算 token 数 N（L3275-3278）——`MmImageEntry{mm_hash,width,height}` 由 4.3 的抓取/维度探测路径填充（[lib/llm/src/preprocessor.rs:741-745](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L741-L745)）；
4. 把占位符**展开**成 N 个 pad token（`pad_value` 编码 mm_hash；必要时补 BOS），再补零对齐到 `kv_cache_block_size` 的整数倍（L3288-3308）——注释点明：路由器只哈希完整块，对齐是为了让长度与块数一致；
5. 产出 `MmRoutingInfo { routing_token_ids, expanded_prompt_len }` 挂到 `PreprocessedRequest` 上，交给 u6 的 KV 路由器去算块哈希、查 radix 树。

这条链也解释了 4.1 里「媒体收集先于 token 装配」的顺序安排：构造路由序列需要同时拿到「分词结果」与「图片尺寸」。

#### 4.8.4 代码实践

1. **实践目标**：亲手验证「相同前缀 ⇒ 相同前缀块序列哈希，且盐不同则全部失配」。
2. **操作步骤**：`dynamo-tokens` 是普通 crate，`examples/` 目录下的文件会被 cargo 自动发现，无需改 `Cargo.toml`。新建 `lib/tokens/examples/block_prefix.rs`（示例代码，非项目原有文件；仅本地练习用，不要提交）：

   ```rust
   // 示例代码：三条"请求"共用前缀，观察块哈希的复用与失配
   use dynamo_tokens::{TokenBlockSequence, Tokens, compute_salt_hash_from_bytes};

   fn show(name: &str, tokens: Vec<u32>, salt: u64) {
       let seq = TokenBlockSequence::new(Tokens::from(tokens), 16, Some(salt));
       let hashes: Vec<String> = seq.blocks().iter()
           .map(|b| format!("{}→{}", b.block_hash(), b.sequence_hash()))
           .collect();
       println!("{name}: {} 个完整块\n  {}", hashes.len(), hashes.join("\n  "));
   }

   fn main() {
       let salt_a = compute_salt_hash_from_bytes(b"base-model");       // 同一"模型盐"
       let prefix: Vec<u32> = (1..=48u32).collect();                    // 3 个完整块的公共前缀
       show("A(前缀+X)", [prefix.clone(), vec![101, 102]].concat(), salt_a);
       show("B(前缀+Y)", [prefix.clone(), vec![201, 202, 203]].concat(), salt_a);
       show("C(同B但换盐)", [prefix, vec![201, 202, 203]].concat(),
            compute_salt_hash_from_bytes(b"lora-x"));                    // 不同盐
   }
   ```

   （注：示例用 `dynamo_tokens::compute_salt_hash_from_bytes` 直接从字节构造盐，与 4.8.2 的公式等价；若想精确复刻 `(salt, lora_name)` 的正则化推导，可在本仓库外的小工程里同时依赖 `dynamo-tokens` 与 `kv-hashing` 两个 crate，改用 `kv_hashing::compute_salt_hash(None, Some("lora-x"))`。）运行：`cargo run -p dynamo-tokens --example block_prefix`。
3. **需要观察的现象**：A 与 B 的前 3 个完整块逐块打印出**完全相同**的 `(block_hash, sequence_hash)`，只有末尾的 PartialBlock 不同；C 与 B 的 token 完全一致但每个哈希都不同。
4. **预期结果**：直观看到「前缀复用的粒度 = 完整块」与「盐=隔离边界」。分词部分（把真实 prompt 变成 token id）可用 Python 的 `transformers.AutoTokenizer.encode` 先行完成再填进示例，见第 5 节综合实践。
5. 具体数值每次构建都一致（确定性哈希）；若 cargo 环境不可用则标注「待本地验证」。

#### 4.8.5 小练习与答案

**练习 1**：`(salt=None, lora=None)` 时盐哈希是多少？这有什么好处？
**答案**：等于 `CHAIN_XXH3_SEED`（1337），即最常见的「裸模型」请求共享同一个种子，天然共享前缀缓存（[lib/kv-hashing/src/salt.rs:16-20](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/kv-hashing/src/salt.rs#L16-L20)）。

**练习 2**：客户端 A 发 `lora_name=""`（空串），客户端 B 不发 lora_name。它们共享缓存吗？
**答案**：共享。`compute_salt_hash` 把空字符串归一化为 `None`（salt.rs L53-54），与路由器在 `lib/kv-router/src/protocols.rs` 的既有行为对齐。

**练习 3**：`build_mm_exact_routing_info` 里 `block_mm_infos` 为什么总是空的？
**答案**：因为 MM 身份（mm_hash）已经被编进 pad token 本身（`pad_value` 编码了 mm_hash），随 token 流进入块哈希，不需要侧信道；这与 vLLM/SGLang 两侧的归一化方案一致（[lib/llm/src/preprocessor.rs:3310-3313](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L3310-L3313)）。

---

## 5. 综合实践

把本讲内容串成一次端到端的离线实验（对应大纲实践任务，全程不需要 GPU）：

**任务：三条 prompt 的块哈希实验 + 一次恶意图片 URL 的安检观察。**

**第一步：分词（Python，可选任一分词器）。**

```python
# step1_tokenize.py —— 示例代码
from transformers import AutoTokenizer
tok = AutoTokenizer.from_pretrained("Qwen/Qwen2.5-0.5B-Instruct")  # 任选小模型
SYSTEM = "You are a helpful assistant."
prompts = [
    tok.apply_chat_template([{"role":"system","content":SYSTEM},
        {"role":"user","content":"讲讲 KV cache 的原理"}], tokenize=True),
    tok.apply_chat_template([{"role":"system","content":SYSTEM},
        {"role":"user","content":"讲讲 KV cache 的原理，并举个例子"}], tokenize=True),
    tok.apply_chat_template([{"role":"system","content":SYSTEM},
        {"role":"user","content":"写一首诗"}], tokenize=True),
]
for i, p in enumerate(prompts):
    print(i, len(p), p[:8], "...")
```

观察：prompt 0 与 1 的前缀 token 完全一致（模板 + 相同 system/user 开头），prompt 2 只有 system 前缀一致。顺手做一个小对照：给第三条再加 `continue_final_message=True` 重渲染一次，用 4.2 的知识解释它为什么比原 prompt **短**（截到末条消息文本为止、未闭合）。

**第二步：块哈希（Rust）。** 把三组 token id 粘贴进 4.8.4 的示例（`block_size=16`、同一盐），运行 `cargo run -p dynamo-tokens --example block_prefix`，记录表格：

| 对比 | 预期共同完整块数 |
|---|---|
| prompt 0 vs 1 | ≈ 共同前缀 token 数 ÷ 16（向下取整） |
| prompt 0 vs 2 | 仅 system 部分对应的块数 |

若某对比的实测值比预期少 1，检查是不是共同前缀长度恰好跨块边界——这正是「复用粒度是块而不是 token」的直接证据。

**第三步：SSRF 拒绝路径（Python mock）。** 运行 4.3.4 的 `ssrf_probe.py`，把 7 条 URL 的 PASS/REJECT 结果与 [components/src/dynamo/common/http/url_validator.py:103-163](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/components/src/dynamo/common/http/url_validator.py#L103-L163) 的分支一一对应；再用 `DYN_MM_ALLOW_INTERNAL=1 python ssrf_probe.py` 重跑，观察内网 IP 与 http 分支被放开（`metadata.google.internal` 也会被放行，因为 `allow_private_ips` 同时控制 IP 段与主机名两道检查——这正是该开关必须只用于内网部署的原因）。

**第四步（可选，需 Rust 环境）：错误类型穿狱。** 运行 4.7.4 的 `test_deepseek_v4_upstream_typed_errors_suppress_jail_finalize`，把三种错误类型的断言结果记进同一份实验记录——它验证的是「整形层不仅要产出正确的成功响应，也要产出**类型正确**的失败响应」。

**产出**：一份实验记录（两个表格 + 拒绝原因清单 + 可选的错误类型记录），它同时证明了你理解了本讲的两个核心机制。

## 6. 本讲小结

- `OpenAIPreprocessor` 是请求进入引擎前的总整形器：模板（MiniJinja）→ 分词（三分支：token 直取 / nvext 预分词 / 本地分词）→ 媒体收集 → `PreprocessedRequest` 装配，并兼任 token 预算门卫（InvalidArgument/400）。
- `prompt.rs` 是模板工程的粘合层：消息规范化（大整数保真、空参数归一）与 `continue_final_message`（追加唯一标记 → 渲染 → 按标记截断，保持分段边界）；embeddings 走独立构造器 `new_for_embeddings`，双分词器变体按需初始化，文本输入按「保留前 N 个」右截断，token 输入不截断。
- 媒体抓取受三道 SSRF 防线保护：静态黑名单检查（协议/IP 直连/端口/主机名/CIDR）、请求前 DNS 逐 IP 复查、连接期 `BlocklistResolver` + 重定向逐跳复检；Rust（前端）与 Python（后端）两套实现逐条对齐，`DYN_MM_ALLOW_INTERNAL=1` 是唯一的内网逃生门。URL 透传用 URL 字节哈希当图片身份，轮换签名 URL 场景应改用 `--frontend-decoding`。
- `LocalModel`/ModelDeploymentCard 是预处理器家当的来源；`kv_cache_block_size` 默认 16，由 Python `EntrypointArgs` 透传进 builder，必须与引擎 page 大小一致。
- token 块化给出三种身份：`BlockHash = XXH3(块字节, salt)`、链式 `SequenceHash`（种子常量 1337）、自包含的 128 位 `PositionalLineageHash`；「相同前缀 ⇒ 相同前缀块哈希」是 KV 感知路由的数学地基。
- nvext 透传：请求侧 `nvext_passthrough_args` 把 `extra_fields`/`metadata_upload`/`token_in`/`cache_salt` 折进 `extra_args["nvext"]` 抵达后端；响应侧 nvext 以「token 追加、其余最新值胜出」的规则穿过 tool-call jail；legacy jail 下 `n>1` 点播 choice 级字段会在预处理阶段被 400 拒绝。
- **jail 终端错误旁路（#13930）**：终端错误被入口 `take_while` 扣留、`JailAnnotated.error` 两端一律置 `None`（各有一条 `debug_assert` 固化契约）、出口 `take_while` 丢弃 jail 的合成输出、流末尾把**原始带类型注解**链回并清空 pending 元数据；HTTP 层因此改读 `error_type()` 分类，字符串前缀恢复分支（26 行）整段删除。
- `salt_hash = 1337 + xxh3(lora) + xxh3(salt)` 是前缀缓存隔离键（调用方入口 `nvext.cache_salt`，顶层字段为回退）；多模态身份不进盐、折叠进块内 13 字节帧编码，图片前的文本前缀仍可复用；MM 路由收口 `build_mm_exact_routing_info` 把「分词结果 + 每图折算 token 数」展开成与 worker 同构的路由 token 序列——本讲所有模块在此汇合。

## 7. 下一步学习建议

- **u4-l5（图像解码后端）**：本讲 4.3 只指到了 `ImageDecoder::decode` 的后端选择入口；libjpeg-turbo 的 FFI 封装、CMYK 处理、Python 侧并行解码开关与 `media_decode` 基准都在那一篇展开。
- **u4-l4（worker 类型与服务发现）**：看 `PreprocessedRequest` 出了预处理器之后如何找到愿意接单的 worker，理解 `mm_routing_info` 被谁消费，以及 embeddings 端点如何在 watcher 里改用 `new_for_embeddings`。
- **u6 系列（KV 感知路由）**：本讲的块哈希在 u6-l3（KV 事件流）与 u6-l4（radix 树索引）里变成路由决策的输入；届时回头重读 `salt.rs` 的「router parity」注释会更有体会。
- **u8-l7（RL rollout）**：nvext 四个字段的最终消费方——`token_in`/`token_data`、`metadata_upload`、`extra_fields` 点播与 `cache_salt`——在 RL 训练循环视角下有完整语义。
- **u8-l8（embeddings 服务化）**：本讲 4.2 的 Rust 侧分词/右截断，与 Python worker 侧如何保证跨语言 token 序列逐位一致（parity 测试）配套成完整链路。
- **u8-l9（前端图像解码与 E/P/D）**：本讲 4.3 提到的 `--frontend-decoding` 路径（对解码 RGB 字节哈希、NIXL 注册内存跨跳存活）在那一篇端到端展开。
- **源码延伸阅读**：`lib/kv-router/src/protocols.rs` 的 `compute_block_hash_for_seq`（与本讲 `compute_block_hash` 的对接点）；`lib/llm/src/tokenizers/`（对应 crate `dynamo-tokenizers`，分词器多后端与 L1 前缀缓存）；`lib/llm/tests/test_streaming_tool_parsers.rs`（jail 行为的最大测试集）。
