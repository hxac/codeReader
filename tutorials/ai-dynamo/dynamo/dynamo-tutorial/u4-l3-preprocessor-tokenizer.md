# 预处理与分词：OpenAIPreprocessor 与 LocalModel

## 1. 本讲目标

学完本讲，你应该能够：

1. 追踪一条 chat 请求从「OpenAI 格式的消息列表」到「token id 序列」的完整整形路径：套模板 → 分词 → 媒体收集 → 装配 `PreprocessedRequest`。
2. 解释带图片/视频的多模态请求如何被计数、抓取与解码（包括本次新增的 libjpeg-turbo 解码后端入口），以及前端在抓取用户提供的 URL 时如何用多层 SSRF 防护把内网目标挡在门外。
3. 说出 `LocalModel` / ModelDeploymentCard 如何把「模型目录、分词器、KV 块大小」交给预处理器。
4. 手算一个 token 块的 `BlockHash` 与链式 `SequenceHash`，并解释「相同前缀 ⇒ 相同前缀块哈希」为什么是 KV 感知路由（u6）与 KVBM（u9）得以工作的地基。
5. 说出 nvext 透传（`extra_fields` / `metadata_upload` / `token_data` / `cache_salt`）的搬运路径，以及它为什么要与 tool-call jail 交互、legacy jail 下 `n=1` 约束的由来。

本讲是 u4-l1 的续篇：u4-l1 讲了「引擎怎么装配」，本讲讲装配好之后，请求在进入引擎前经历的最后一道整形。

本次更新对应两个上游变更：#13730 对 `preprocessor.rs` 做了约 860 行重构（nvext 穿透 tool-call jail），#11157 把图像解码拆到了 `decoders/image` 的后端体系（单独成篇 u4-l5，本讲只讲入口）。

## 2. 前置知识

- **token 与分词器**：LLM 内部不认识字符串，只认识整数 token id。分词器（BPE 等）负责文本 ↔ id 双向映射，HuggingFace 格式的模型目录里通常有一个 `tokenizer.json`。分词是「相同文本永远得到相同 id 序列」的确定性过程——这一点是后面所有哈希推理的前提。
- **chat 模板**：OpenAI 请求是结构化的 `messages` 数组（system/user/assistant），而模型训练时见到的是一段拼好的文本（如 `<|im_start|>user\n...<|im_end||>\n`）。模板（通常是一个 MiniJinja 脚本）负责这次拼接。
- **KV cache 与前缀复用**：自回归推理中，每个 token 都会产生一份 KV（Key/Value）中间结果。**相同前缀的请求会计算出完全相同的 KV**，因此第二条请求可以复用第一条已算好的部分。引擎的 KV cache 按「块」（block/page，通常 16 或 32 个 token 一块）管理，所以复用粒度也是块。
- **nvext（NVIDIA 扩展信封）**：Dynamo 在 OpenAI 兼容协议之上附加的扩展字段，请求侧挂在 `nvext` 下、响应侧挂在同名信封里。本讲关心四个字段：`extra_fields`（点播响应里要额外返回的字段名列表）、`metadata_upload`（要求后端把响应元数据带外上传）、`token_data`（调用方已经分好的词，随请求直接给 token）、`cache_salt`（前缀缓存隔离盐）。它们的服务对象主要是 RL rollout 等高级消费方（见 u8-l7）。
- **tool-call jail（工具调用监狱）**：很多模型把工具调用以原生标记（如 `<tool_call>{...}</tool_call>`）混在普通文本里输出。前端必须在流式响应上「缓冲 + 解析 + 改写」，把标记改写成结构化的 `tool_calls` 字段——这段缓冲/改写逻辑俗称 jail。jail 会拆开并重组响应 chunk，所以任何「附加在 chunk 上的元数据」都有被弄丢的风险，这正是本讲 4.5 节的主题。
- **SSRF（服务端请求伪造）**：多模态请求里图片以 URL 形式传入，服务器必须去抓这个 URL。如果攻击者传入 `http://169.254.169.254/latest/meta-data/`（云厂商元数据地址）或 `http://10.0.0.5:6379/`（内网 Redis），你的前端就替攻击者访问了内网。防护思路：只允许 http(s)/data 协议、拒绝 IP 直连、维护私网 CIDR 与内部主机名黑名单、DNS 解析结果逐 IP 复查、重定向每一跳重新检查。
- **承接 u4-l1 的术语**：`EngineConfig::InProcessTokens` 表示「框架替引擎分词」，本讲的 `OpenAIPreprocessor` 就是那个「框架」的核心实现；`PreprocessedRequest` 是它产出的内部统一请求格式。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲视角 |
|---|---|---|
| `lib/llm/src/preprocessor.rs` | 预处理器主体（约 7000 行），模板/分词/媒体/token 预算/nvext 透传/工具调用解析 | 只读骨架：结构体字段 + `preprocess_request_with_options` 主流程 + 4.5 的 nvext 与 jail |
| `lib/llm/src/protocols/common/extensions.rs` | nvext 协议定义与合并规则（`NvExtProvider`/`request_cache_salt`/`merge_response_nvext`） | 4.5 的协议侧 |
| `lib/llm/src/preprocessor/media/loader.rs` | Rust 侧媒体抓取器与 SSRF 策略（`MediaFetcher`/`MediaLoader`） | 重点精读 |
| `lib/llm/src/preprocessor/media/decoders/image.rs`（含 `backends/`） | 图像解码编排与后端选择（image_reader / turbojpeg，#11157 新增） | 只看入口，细节在 u4-l5 |
| `components/src/dynamo/common/http/url_validator.py` | Python 侧同构的 URL 策略（后端解码路径用） | 与 Rust 侧对照 |
| `lib/llm/src/local_model.rs` | 本地模型（目录 + ModelDeploymentCard）与构建器 | 看 `kv_cache_block_size` 从哪来 |
| `lib/tokens/src/lib.rs` | token 块化与哈希的全部数学（`TokenBlockSequence` 等） | 重点精读（注意：token 类型定义在此文件，仓库中没有 `tokens.rs`） |
| `lib/kv-hashing/src/{block,request,salt}.rs` | 把「请求 → 块哈希」封装成统一入口 | 前端与路由器的字节级一致性契约 |

## 4. 核心概念与源码讲解

### 4.1 OpenAIPreprocessor：请求整形的总调度

#### 4.1.1 概念说明

HTTP 进来的 `NvCreateChatCompletionRequest` 属于「OpenAI 世界」：消息是结构化 JSON、采样参数是可选字段、图片是 URL。而推理引擎需要的是 `PreprocessedRequest`：一串 `token_ids`、一份采样/停止条件、可选的多模态数据。`OpenAIPreprocessor` 就是这两者之间的翻译官，同时兼任门卫（token 预算校验，超限请求在进入引擎前就被拒绝）。

文件开头的模块注释把这个大文件切成四步：translation（入站消息 → 内部表示）、apply（填默认值）、prompt（模板）、tokenize（分词）（[lib/llm/src/preprocessor.rs:4-12](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L4-L12)）。

#### 4.1.2 核心流程

`preprocess_request_with_options` 的主轴（简化伪代码）：

```text
builder = builder_with_lora(request, lora_name)        # 抽取采样/停止/输出选项 + nvext 透传进 extra_args
formatted = apply_template(request)                    # ① MiniJinja 渲染 chat 模板
(token_ids, annotations) = gather_tokens(request, formatted)   # ② 分词（或直取）
(mm_images, image_tokens) = gather_multi_modal_data_with_image_tokens(...)  # ③ 媒体
builder.token_ids(token_ids)                          # ④ 装配
if nvext.router 存在: builder.router(router_params)    #    外部路由参数透传
preprocessed = builder.build()
validate_preprocessed_token_budget(preprocessed)       # ⑤ token 预算门卫
```

注意顺序上的讲究：③ 在 ④ 之前执行，是因为多模态路由信息（见 4.6.3）需要借用 `token_ids` 来做「图片占位符对齐」，注释明确写了「Install tokens on the builder. Done after MM routing built its view」（[lib/llm/src/preprocessor.rs:2133-2136](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L2133-L2136)）。另外第一步的 `builder_with_lora` 现在还负责把 nvext 透传参数折进 `extra_args`（见 4.5）。

#### 4.1.3 源码精读

**结构体：预处理器持有的一切**（[lib/llm/src/preprocessor.rs:1085-1136](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L1085-L1136)）。这段定义了 `OpenAIPreprocessor` 的全部状态，几个关键字段：

- `formatter: Arc<dyn OAIPromptFormatter>` — chat 模板渲染器；
- `tokenizer: Arc<dyn Tokenizer>` — 分词器；
- `runtime_config` — 模型级运行时配置（reasoning/tool 解析器等），4.5 的路由选择要用它；
- `kv_cache_block_size: usize` — 从模型部署卡带来的块大小，直接决定后续块哈希的切分粒度；
- `tool_call_parser: Option<String>` — 工具调用解析器名（如 `glm47`、`kimi_k3`），4.5 的 jail 路由依据；
- `media_loader: Option<MediaLoader>` — 媒体抓取器，`None` 时走 URL 透传（抓取留给后端）；
- `image_token_counter` — 每张图片折算多少 token 的计数器（`mm-routing` 特性）。

**构造**：`OpenAIPreprocessor::new(mdc)` 从 ModelDeploymentCard 取出 formatter 与 tokenizer，再进 `new_with_parts`（[lib/llm/src/preprocessor.rs:1751-1757](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L1751-L1757)）；`kv_cache_block_size` 与 `media_loader` 都在这一步从卡片落位（[lib/llm/src/preprocessor.rs:1797-1797](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L1797-L1797)、[lib/llm/src/preprocessor.rs:1831-1834](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L1831-L1834)）。

**主流程**（[lib/llm/src/preprocessor.rs:2074-2199](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L2074-L2199)）：模板段与分词段分别用 `TEMPLATE_SECONDS` / `TOKENIZE_SECONDS` 打点，并用 NVTX range 标注（`preprocess.template`、`preprocess.tokenize`），可被 nsys 这类剖析器直接看到。`nvext.router` 若存在则原样挂到 builder 上（[lib/llm/src/preprocessor.rs:2142-2146](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L2142-L2146)）。末段（[lib/llm/src/preprocessor.rs:2162-2191](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L2162-L2191)）在 `max_tokens` 缺省时补一个「上下文剩余量」默认值并复查预算——门卫逻辑集中在 `validate_requested_token_budget`（[lib/llm/src/preprocessor.rs:1185-1230](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L1185-L1230)），错误类型是 `InvalidArgument`（HTTP 400 语义，而非 500）。

**分词三分支**：`gather_tokens`（[lib/llm/src/preprocessor.rs:3275-3404](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L3275-L3404)）按 `request.prompt_input_type()` 分流：

1. 客户端直接给 token（`PromptInput::Tokens`）：原样采用，完全跳过分词（L3292-3310）；
2. `nvext.token_data` 存在：外部组件（如 GAIE 的 EPP KV 路由器）已经分好词，直接复用并打日志 `[SIDECAR-SKIP-TOKENIZE]`（[lib/llm/src/preprocessor.rs:3328-3351](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L3328-L3351)）；附带一个兼容分支：设了 `backend_instance_id` 却没给 `token_data` 时警告并回落到本地分词；
3. 普通文本：`encode_prompt_with_timing` 调 `tokenize_rendered_prompt` 分词（[lib/llm/src/preprocessor.rs:2035-2035](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L2035-L2035)）——若渲染结果带特殊 token 边界（segments）就按段编码，避免 BPE 把 `<|im_end|>` 这类特殊 token 切碎。

**模板**：`apply_template_inner`（[lib/llm/src/preprocessor.rs:2475-2512](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L2475-L2512)）只在「文本输入」时渲染，且支持 `nvext.use_raw_prompt` 直接用原始 prompt 绕过模板（completions 端点走这条路）。

**挂接点**：`OpenAIPreprocessor` 实现为流水线上的流式算子，chat 流式入口在（[lib/llm/src/preprocessor.rs:5965-6084](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L5965-L6084)）：输入 `NvCreateChatCompletionRequest`，先做思考模式归一化，调用 `preprocess_request_with_options`（L6026-6037）得到 `PreprocessedRequest` 交给下游引擎，随后立刻做工具路由决策与 nvext 校验（L6040-6054，见 4.5）。也就是说它天然是 u3-l3 引擎流水线上的一个「前置算子 + 后置流改写器」。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：确认主流程各阶段的真实顺序与门卫位置。
2. **操作步骤**：打开 [lib/llm/src/preprocessor.rs:2074-2199](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L2074-L2199)，抄下 ①~⑤ 每一步的函数名与行号；再打开 [lib/llm/src/preprocessor.rs:3275-3404](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L3275-L3404) 找出三分支各自的判断条件。
3. **需要观察的现象**：`builder.token_ids(token_ids)`（L2136）在 `gather_multi_modal_data_with_image_tokens` 之后、`builder.build()` 之前。
4. **预期结果**：你能回答「为什么 token_ids 的装配要放在媒体收集之后」——注释原文是 MM 路由需要先借用 `token_ids` 做占位符对齐。
5. 本实践为静态阅读，无需运行（若想看运行效果，可在 u1-l2 的 sample 服务上发一条请求，用 `RUST_LOG=dynamo_llm=trace` 观察 `Pre-processed request` 日志，L6056）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `nvext.token_data` 存在时可以跳过分词？什么场景会用到？
**答案**：GAIE 拓扑下 EPP 侧的 KV 路由器已经为了计算块哈希对 prompt 分过词（token 是路由的输入），把结果随请求带回可以避免前端重复分词（L3328-3334 的注释）。

**练习 2**：`validate_requested_token_budget` 拒绝请求时返回的错误类型是什么？这意味着客户端会收到什么？
**答案**：`ErrorType::InvalidArgument`（L121-127 的 `invalid_argument_error`），HTTP 层映射为 400，表示是调用方的参数问题而不是服务端故障。

**练习 3**：`MultimodalCounts::from_preprocessed` 统计的是什么？它依赖 `mm-routing` feature 吗？
**答案**：统计请求里 `image_url` / `video_url` / `audio_url` 三类内容部件的个数，用于指标标注；它从 `multi_modal_data` 派生，因此与 `mm-routing` 特性无关（[lib/llm/src/preprocessor.rs:865-895](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L865-L895)，`from_preprocessed` 在 L873）。

---

### 4.2 媒体加载与 fetch policy：抓用户 URL 时的安全底线

#### 4.2.1 概念说明

图片以 URL（而非字节）随请求到达后，有两种处理方式：**前端解码**（`media_loader` 存在时，前端自己抓取、解码成像素张量、注册进 NIXL，供 worker 直接 RDMA 取走）和 **URL 透传**（前端只做安全检查与维度探测，抓取交给后端）。两条路径共用同一套 SSRF 策略。

Dynamo 在两处实现了这套策略：Rust 的 `MediaFetcher`（前端解码 + 维度探测路径）和 Python 的 `UrlValidationPolicy`（Python 后端解码路径）。两边维护**逐条对齐**的黑名单，注释互相点名要求同步修改（Rust 侧 L37、L72 明确指向 Python 文件）。

解码本身在 #11157 之后不再是一坨：`MediaLoader` 调用的 `ImageDecoder` 被拆进 `preprocessor/media/decoders/image.rs`，它在一个后端体系（`backends/`）里选择 `image_reader`（`image` crate/PIL 系路径）或 `turbojpeg`（libjpeg-turbo 绑定）。本讲只讲到「解码入口与选择发生在哪」，后端的性能与兼容性细节（CMYK、并行解码、基准）单独成篇 u4-l5。

#### 4.2.2 核心流程

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

#### 4.2.3 源码精读

**黑名单**（与 Python 侧逐条相同）：`BLOCKED_IP_NETWORKS` 覆盖 RFC1918 私网、CGNAT、环回、链路本地（含云元数据 `169.254.169.254`）、IPv6 保留段等 20 个 CIDR（[lib/llm/src/preprocessor/media/loader.rs:37-63](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor/media/loader.rs#L37-L63)）；`BLOCKED_HOSTS` 按字面匹配 `localhost`、`metadata.google.internal`、`kubernetes.default.svc` 等内部服务名（[lib/llm/src/preprocessor/media/loader.rs:72-89](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor/media/loader.rs#L72-L89)）——后者防御 `/etc/hosts` 伪造与恶意解析器。

**策略对象 `MediaFetcher`**（[lib/llm/src/preprocessor/media/loader.rs:104-123](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor/media/loader.rs#L104-L123)）：默认 `allow_direct_ip=false`、`allow_direct_port=false`、`allow_private_ips=false`。三个开关由环境变量 `DYN_MM_ALLOW_INTERNAL=1` 一次性打开（`from_env`，[lib/llm/src/preprocessor/media/loader.rs:148-160](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor/media/loader.rs#L148-L160)）——这是给「前端与图片服务同在内网」的本地部署留的逃生门，注释用加粗警告**绝不能**开在公网服务上（注意 `allow_private_ips` 同时控制 IP 段与主机名两道黑名单，见 L108-119 的语义说明）。

**异步全查 `check_if_url_allowed_with_dns`**（[lib/llm/src/preprocessor/media/loader.rs:256-290](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor/media/loader.rs#L256-L290)）：内部先做同步快查 `check_if_url_allowed`（协议 → IP 直连 → 端口 → 主机名黑名单 → CIDR → 可选域名白名单，[lib/llm/src/preprocessor/media/loader.rs:195-254](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor/media/loader.rs#L195-L254)），再对域名做 `lookup_host`，**每个**解析出的 IP 都过一遍 `is_blocked_ip`。策略违规统一构造成 `InvalidArgument` 的 `DynamoError`（`policy_rejection`，L96-101），HTTP 层映射为 400。

**带安检的 HTTP 客户端 `build_http_client`**（[lib/llm/src/preprocessor/media/loader.rs:292-325](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor/media/loader.rs#L292-L325)）：重定向策略是自定义的——最多 3 跳，且**每一跳的目标 URL 重新过一遍 `check_if_url_allowed`**，防止「公网 URL 302 跳内网」。DNS 解析器换成 `BlocklistResolver`（[lib/llm/src/preprocessor/media/loader.rs:327-357](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor/media/loader.rs#L327-L357)）：解析结果先滤掉黑名单 IP，滤到空就报策略错误——连接期防线。

**抓取与缓存 `fetch_and_decode_media_part`**（[lib/llm/src/preprocessor/media/loader.rs:515-623](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor/media/loader.rs#L515-L623)）：图片路径上，安检（L552-555）通过后才 `from_url` 抓字节、`decode_async` 解码（L556-563）；产物经 `into_rdma_descriptor` 注册 NIXL。可选的字节预算 LRU 缓存（`DYN_MULTIMODAL_LOADER_CACHE_GB`，默认 0 关闭）命中时同时跳过网络与解码（L520-541）。视频需要 `media-ffmpeg` 特性，音频尚不支持（L565 起）。

**解码后端选择（#11157 新增的入口）**：`MediaLoader::new` 现在会先调用 `media_decoder.warn_if_unavailable_backends()`（[lib/llm/src/preprocessor/media/loader.rs:457-459](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor/media/loader.rs#L457-L459)，实现在 [lib/llm/src/preprocessor/media/decoders.rs:53-58](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor/media/decoders.rs#L53-L58)）——请求了 libjpeg 后端但运行环境没有该库时提前告警。真正的选择发生在 `ImageDecoder::decode`（[lib/llm/src/preprocessor/media/decoders/image.rs:132-145](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor/media/decoders/image.rs#L132-L145)）：`enable_libjpeg && turbojpeg.supports(format)` 时先试 turbojpeg，被拒/失败则回落 `image_reader`；两个后端实现为 `ImageDecodeBackend` trait 的对象（[lib/llm/src/preprocessor/media/decoders/image/backends.rs:130-130](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor/media/decoders/image/backends.rs#L130-L130)）。细节（CMYK、decline 语义、Python 侧开关与基准）留给 u4-l5。

**Python 侧对照**（[components/src/dynamo/common/http/url_validator.py:85-163](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/components/src/dynamo/common/http/url_validator.py#L85-L163)）：`UrlValidationPolicy` 默认比 Rust 更严——**只允许 `https://` 和 `data:`**，`http://` 需要显式放开。`validate_url` 的逻辑与 Rust 同构：协议 → 主机名黑名单 → IP 字面量 → `loop.getaddrinfo` 逐 IP 复查。`from_env` 同样读 `DYN_MM_ALLOW_INTERNAL`，另外还有 `DYN_MM_LOCAL_PATH` 控制本地文件访问（Rust 侧没有本地路径入口，`validate_local_path` 会把 `file://` 解析后限制在白名单目录内，[components/src/dynamo/common/http/url_validator.py:166-202](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/components/src/dynamo/common/http/url_validator.py#L166-L202)）。

#### 4.2.4 代码实践

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
4. **预期结果**：`169.254.169.254` 走「IP 字面量在黑名单段」分支，`metadata.google.internal` 走主机名黑名单分支——与 4.2.3 的源码分析一一对应。
5. Rust 侧对应验证：`cargo test -p dynamo-llm test_direct_ip_blocked test_blocked_ip_literal test_blocked_hostname_rejected test_allow_private_ips_bypasses_blocklist`（这些用例在 [lib/llm/src/preprocessor/media/loader.rs:989-1132](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor/media/loader.rs#L989-L1132)，纯策略测试、无需 NIXL）。若本机未编过 Rust workspace，标注「待本地验证」并先以 Python 结果为准。

#### 4.2.5 小练习与答案

**练习 1**：攻击者把公网图片 URL 302 重定向到 `http://127.0.0.1:8080/admin`，会被哪一道防线拦住？
**答案**：重定向自定义策略——`build_http_client` 的 redirect policy 对每一跳目标重跑 `check_if_url_allowed`，`127.0.0.1` 在 `127.0.0.0/8` 黑名单段，`attempt.error()` 直接终止；单测 `test_redirect_to_blocked_target_is_terminal_and_makes_no_connection` 断言了目标端口甚至收不到连接（[lib/llm/src/preprocessor/media/loader.rs:1198-1241](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor/media/loader.rs#L1198-L1241)）。

**练习 2**：`BlocklistResolver` 与 `check_if_url_allowed_with_dns` 都检查 DNS 结果，为什么两道都要？
**答案**：前者防 rebinding（检查之后、连接之前 DNS 答案被换）；后者在发起请求前更早拒绝，语义更清晰且 Python 侧只有等价的后一种。纵深防御：任何一道被绕过（如 reqwest 行为变化）另一道仍兜底。

**练习 3**：为什么 Rust 默认允许 `http://` 而 Python 默认不允许？
**答案**：Rust 前端运行在集群内、图片源通常是受控对象存储；Python 侧 `UrlValidationPolicy` 服务于更靠外的后端解码路径，默认只信 `https://` 与 `data:`，需要内网 http 时用 `DYN_MM_ALLOW_INTERNAL=1` 显式放开（[components/src/dynamo/common/http/url_validator.py:85-101](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/components/src/dynamo/common/http/url_validator.py#L85-L101) 的类 docstring 与 `from_env`）。

**练习 4**：`MediaLoader::new` 为什么要调用 `warn_if_unavailable_backends`？
**答案**：#11157 引入 libjpeg-turbo 后端后，部署可能配置了 `enable_libjpeg` 但容器里没有 `libturbojpeg`。在构造 `MediaLoader` 时（而非第一次解码失败时）告警，运维能在启动日志里立刻看到「后端不可用、将回落 image_reader」，而不是在流量进来后才发现性能不达预期。

---

### 4.3 LocalModel：模型落地与「块大小」从哪来

#### 4.3.1 概念说明

`LocalModel` 是「一个已在本地的模型」的完整描述：磁盘路径 + ModelDeploymentCard（MDC，模型部署卡，携带 tokenizer、chat 模板、运行时配置、媒体解码器等）。预处理器的一切家当（formatter、tokenizer、块大小、媒体加载器）最终都来自它。它由 `LocalModelBuilder` 构建，Python 侧的 `EntrypointArgs` 透传参数给这个 builder——这是 u2-l2「PyO3 薄壳」模式的又一次体现。

#### 4.3.2 核心流程

```text
Python: EntrypointArgs(kv_cache_block_size=...)     # lib/bindings/python/rust/llm/entrypoint.rs:594
   ↓ PyO3
LocalModelBuilder::kv_cache_block_size(...)          # local_model.rs:151（None → 默认 16）
   ↓ build()
LocalModel { card: ModelDeploymentCard, ... }        # local_model.rs:436
   ↓ OpenAIPreprocessor::new(mdc)
预处理器拿到 tokenizer / formatter / kv_cache_block_size / media_loader
```

模型本体按需拉取：`LocalModel::fetch(remote_name, ignore_weights)` 走 HuggingFace 下载，`ignore_weights=true` 时只下配置类文件（前端不需要权重，只需要 tokenizer/模板/配置）（[lib/llm/src/local_model.rs:513-520](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/local_model.rs#L513-L520)）。

#### 4.3.3 源码精读

- **默认块大小**：`DEFAULT_KV_CACHE_BLOCK_SIZE: u32 = 16`——引擎不提供时 Dynamo 的兜底值（[lib/llm/src/local_model.rs:33-38](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/local_model.rs#L33-L38)，常量在 L34）。它必须与 worker 引擎的 KV page 大小一致，否则前端算出的块哈希与 worker 上报的对不上号。
- **builder 字段**（[lib/llm/src/local_model.rs:56-92](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/local_model.rs#L56-L92)）：`model_path` / `source_path` / `template_file` / `kv_cache_block_size` / `media_decoder` / `media_fetcher` / `runtime_config` 等；setter `kv_cache_block_size` 传 `None` 即重置为默认（[lib/llm/src/local_model.rs:151-152](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/local_model.rs#L151-L152)）。
- **LocalModel 本体**（[lib/llm/src/local_model.rs:436-455](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/local_model.rs#L436-L455)）：核心是 `card: ModelDeploymentCard` 字段，`display_name`/`service_name`/`path` 等都是便捷访问器。
- **分词器加载**：`ModelDeploymentCard::tokenizer()`（[lib/llm/src/model_card.rs:1255-1298](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/model_card.rs#L1255-L1298)）从 `tokenizer.json` 构造 HF 分词器，随后合并 `tokenizer_config.json` 的特殊 token、禁用 `tokenizer.json` 内嵌的截断配置（避免 prompt 被无声裁剪），并支持 `DYN_TOKENIZER_CACHE*` 系列环境变量做前缀缓存。
- **Python 参数入口**：PyO3 签名里 `kv_cache_block_size=None`（[lib/bindings/python/rust/llm/entrypoint.rs:594-594](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/bindings/python/rust/llm/entrypoint.rs#L594-L594)），随后 `.kv_cache_block_size(args.kv_cache_block_size)` 落到 builder（[lib/bindings/python/rust/llm/entrypoint.rs:741-741](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/bindings/python/rust/llm/entrypoint.rs#L741-L741)）。

#### 4.3.4 代码实践（参数链追踪）

1. **实践目标**：把「Python 参数 → builder → 预处理器」的传递链亲手走一遍。
2. **操作步骤**：从 [lib/bindings/python/rust/llm/entrypoint.rs:594](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/bindings/python/rust/llm/entrypoint.rs#L594-L594) 的签名出发，依次 `Grep` `kv_cache_block_size` 在 `lib/` 下的全部出现点（共约 6 处），给每一处标注它在链上的角色。
3. **需要观察的现象**：值沿着 `EntrypointArgs.new → LocalModelBuilder → LocalModel/MDC → OpenAIPreprocessor::kv_cache_block_size` 单向流动，没有任何回头修改。
4. **预期结果**：得到一张 5 节点的传递链图；并回答：不传该参数时前端用什么块大小切块？（16）
5. 纯静态追踪，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：前端把 `kv_cache_block_size` 配成 32，而 worker 引擎的 page 是 16，会发生什么？
**答案**：前端切出的块与 worker 按 16 切的块边界不重合，双方计算/上报的块哈希无法对齐——KV 感知路由会把这些块当作不匹配，前缀复用失效（最坏情况是错误复用）。所以该值必须与引擎一致，`build_mm_exact_routing_info` 里块大小为 0 时直接放弃 MM 路由（[lib/llm/src/preprocessor.rs:2993-3000](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L2993-L3000)）也体现了「块大小不可靠宁可降级」的思路。

**练习 2**：`LocalModel::fetch(name, true)` 的 `ignore_weights=true` 适合谁用？
**答案**：前端/路由器——它们只需要 tokenizer、模板与配置来整形请求和算哈希，不需要几 GB 的权重；worker 才需要完整权重。

---

### 4.4 token 块：TokenBlockSequence 与 block 化

#### 4.4.1 概念说明

`lib/tokens`（crate 名 `dynamo-tokens`）解决一个问题：把一串 token id 切成固定大小的块并为每块算出稳定身份。为什么必须块化？因为 KV cache 的分配、迁移、去重、路由匹配全都以块为单位；「请求 A 的前 3 块与请求 B 的前 3 块相同」是可执行的路由判断，而「两个 prompt 文本相似」不是。

三种哈希身份（记住这套命名，u6/u9 会反复出现）：

| 哈希 | 定义 | 回答的问题 |
|---|---|---|
| `BlockHash` (u64) | `XXH3_64(块内 token 字节, salt_hash)` | 这一块内容是什么 |
| `SequenceHash` (u64) | 从第 0 块起链式累积 | 从序列开头到这块的完整路径 |
| `PositionalLineageHash` (u128) | 打包（位置, 当前序列哈希, 父哈希片段） | 树上哪个节点（自包含、可直接扩展） |

#### 4.4.2 核心流程与数学

**块哈希**：token 是 `u32`（[lib/tokens/src/lib.rs:22-23](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/tokens/src/lib.rs#L22-L23)），一个 N 个 token 的块被 `bytemuck::cast_slice` 原样重解释为 4N 字节的小端字节流，然后用请求级种子做 XXH3：

\[
\text{block\_hash}_i = \text{XXH3\_64}\big(\text{le\_bytes}(\text{tokens}_i),\ \text{salt\_hash}\big)
\]

（实现：[lib/tokens/src/lib.rs:147-163](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/tokens/src/lib.rs#L147-L163)）

**链式序列哈希**：第 0 块的序列哈希等于其块哈希；之后每块把「父序列哈希 + 本块块哈希」两个 u64 拼成 16 字节再哈希，种子是常量 `CHAIN_XXH3_SEED = 1337`：

\[
\text{seq}_0 = \text{block\_hash}_0,\qquad
\text{seq}_i = \text{XXH3\_64}\big(\text{le\_bytes}([\text{seq}_{i-1},\ \text{block\_hash}_i]),\ 1337\big)
\]

（[lib/tokens/src/lib.rs:61-83](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/tokens/src/lib.rs#L61-L83)）

**关键推论（本讲最重要的一句话）**：哈希只依赖「本块内容 + 父链」。因此两条请求只要拥有相同的前缀 token，它们前缀里的每个完整块都会得到**逐块相同**的 `block_hash` 与 `sequence_hash`——与后面说什么完全无关。这就是「路由器凭块哈希知道哪个 worker 已缓存了这段前缀」的全部数学基础。

**切分流程**（`TokenBlockSequence::split_tokens`，[lib/tokens/src/lib.rs:1672-1715](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/tokens/src/lib.rs#L1672-L1715)）：

```text
tokens.chunks_exact(block_size)        # 每 block_size 个一块
  → TokenBlockChunk::from_tokens       #   每块先算 block_hash（可并行）
  → 顺序循环 TokenBlock::from_chunk    #   链式串起 sequence_hash（必须顺序）
  → 余数进 PartialTokenBlock           #   末尾不满一块：用 UUID 标识，不参加完整块哈希
```

**多模态块（选读）**：图片在 token 序列里表现为一串「占位符槽位」。带 MM 信息的块不走 4 字节编码，而是每个槽位输出 13 字节帧：真实 token 是 `tag=0x00 | token u32 | pad`，占位符是 `tag=0x01 | run_offset u32 | mm_hash u64`（`mm_hash` 是图片身份哈希）。两种编码每槽宽度不同（4 vs 13），天然不会碰撞（设计文档与实现在 [lib/tokens/src/lib.rs:306-310](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/tokens/src/lib.rs#L306-L310) 起的 `compute_block_bytes_with_mm`）。效果：**图片之前的纯文本前缀块哈希完全不受图片影响**，仍然可以复用。

#### 4.4.3 源码精读

- **`TokenBlock::from_chunk`**（[lib/tokens/src/lib.rs:1128-1159](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/tokens/src/lib.rs#L1128-L1159)）：链递归的唯一落点——无父则序列哈希=块哈希，有父则 `compute_next_sequence_hash`。同一函数被请求侧（`split_tokens`）、流式侧（`commit_current`）与路由器复用。
- **`TokenBlockSequence`**（[lib/tokens/src/lib.rs:1234-1273](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/tokens/src/lib.rs#L1234-L1273)）：`Vec<TokenBlock>` + 一个 `PartialTokenBlock` 尾块。`extend` 流式追加 token 并在块满时提交（[lib/tokens/src/lib.rs:1290-1323](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/tokens/src/lib.rs#L1290-L1323)）——解码期逐 token 生成的场景就用它增量成块。
- **`PartialTokenBlock::commit`**（[lib/tokens/src/lib.rs:1009-1030](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/tokens/src/lib.rs#L1009-L1030)）：提交后把自己重置为「下一块」，继承刚提交块的序列哈希作为父——增量式链维护。
- **可验证的测试常量**：`hash([1,2,3,4], salt=1337) = 14643705804678351452`，第 2、3 块的链值也硬编码在测试里（[lib/tokens/src/lib.rs:1911-1975](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/tokens/src/lib.rs#L1911-L1975)，`test_validate_hash_constants` 在 L1945）——这是把「数学定义」固化成「回归基准」的好范例。
- **`UniqueBlock` 的两态**（[lib/tokens/src/blocks.rs:13-26](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/tokens/src/blocks.rs#L13-L26)）：完整块用 `FullBlock(GlobalHash)`，未满块用随机 `PartialBlock(Uuid)`——不满一块的尾巴没有稳定内容，用唯一 Id 标识即可。

#### 4.4.4 代码实践

1. **实践目标**：用仓库自带的回归测试验证 4.4.2 的公式与常量。
2. **操作步骤**：运行 `cargo test -p dynamo-tokens test_validate_hash_constants`（这是个小 crate，编译很快；macOS 上也无需特殊 feature）。
3. **需要观察的现象**：测试通过，意味着 `compute_block_hash(cast_slice([1,2,3,4]), 1337)` 确实等于 14643705804678351452，且第 2 块序列哈希等于 `xxh3([seq1, hash2], 1337)`。
4. **预期结果**：3 个断言组（三块各自的 block_hash/seq_hash）全绿。想再进一步，可加跑 `token_hash_helper_matches_canonical_byte_encoding` 验证 `compute_block_hash_for_tokens` 与 `cast_slice` 路径等价。
5. 具体输出以本机为准；若 cargo 环境不可用则标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：请求 A 的 token 是 `[t1..t40]`，请求 B 是 `[t1..t40, u1..u5]`（block_size=16，salt 相同）。B 能复用 A 的哪些块？
**答案**：`chunks_exact(16)` 只切完整块：A 的 40 个 token 切出 2 个完整块（t1-16、t17-32），剩 8 个进 PartialBlock；B 的 45 个 token 同样只有前 2 块是完整的。这 2 块内容相同 → block_hash 与 sequence_hash 逐块相同，可复用。A、B 的第 3 块都不满 16 个 token（分别 8 个与 13 个），用 UUID 标识、不产生稳定哈希。结论：复用块数 = 2 = ⌊40/16⌋。

**练习 2**：为什么链式哈希的种子是常量 1337，而块哈希的种子是每请求的 `salt_hash`？
**答案**：salt 的隔离职责已经在第 0 块的 block_hash 里兑现（`seq_0 = block_hash_0`），并沿链传播；每一步再喂 salt 是冗余。链种子固定为 1337 是为了与 kv-router 的线格式逐字节兼容，注释明确要求改动必须多方同步（[lib/tokens/src/lib.rs:56-75](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/tokens/src/lib.rs#L56-L75)）。

**练习 3**：`PositionalLineageHash` 为什么要打包成 128 位而不是直接用 `SequenceHash`？
**答案**：序列哈希只是「内容指纹」，不含位置与父子关系；PLH 把（位置、当前序列哈希全量、父哈希片段）打进一个 u128，使得子节点可以**只凭父 PLH + 子块哈希**完成 `extend`，无需查表——这对 radix 树的插入/回溯（u6-l4）是关键性能性质（结构体与 `extend` 见 [lib/tokens/src/lib.rs:549-619](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/tokens/src/lib.rs#L549-L619)）。

---

### 4.5 nvext 透传与 tool-call jail：扩展字段不能半路蒸发（本次新增）

#### 4.5.1 概念说明

nvext 的四个关键字段各有用途：`extra_fields` 让调用方点播响应附加字段（`worker_id`、`engine_data`、`prompt_logprobs` 等，协议侧的可选集定义在 `NvExtResponseFieldSelection`，[lib/llm/src/protocols/common/extensions.rs:713-724](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/protocols/common/extensions.rs#L713-L724)）；`metadata_upload` 要求后端把响应元数据带外上传（SGLang 的 meta_info 通道，见 u8-l7）；`token_data` 让调用方直接给 token（对应 4.1 的分词分支 2）；`cache_salt` 是前缀缓存隔离盐（与 4.6 的盐推导衔接）。

问题在于：这些字段要么需要**跟着请求走到后端**（请求侧透传），要么是**后端贴在响应 chunk 上的数据**（响应侧保全）。而前端恰恰在响应路径上部署了一个会拆装 chunk 的组件——tool-call jail。jail 会把 N 个输入 chunk 缓冲改写成 M 个输出 chunk，每次改写都是「克隆一个 chunk 当模板」。#13730 之前，改写路径上 `nvext` 会被置 `None`（连同旧版 `scrub_synthetic_chunk_metadata` 的一揽子清洗），结果就是：开启了工具调用解析的模型上，RL 消费方点播的 token 序列、logprobs 等数据无声消失。#13730 的修复分三件事：请求侧统一透传入口、响应侧让 nvext 穿狱存活、对无法安全支持的场景显式拒绝。

#### 4.5.2 核心流程

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

合并规则（与一元聚合器一致）：`completion_token_ids` **追加拼接**（否则 `[42]` 会变 `[42,42]`），其余顶层字段（`prompt_logprobs`、`engine_data` 等）**最新值胜出**（[lib/llm/src/protocols/common/extensions.rs:676-711](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/protocols/common/extensions.rs#L676-L711)）。

**约束（显式拒绝）**：`engine_data` / `routed_experts` / `stop_reason` 是 choice 级字段（每个候选各一份），而 legacy jail 的缓冲改写无法在 `n>1` 时把它们正确归位到各 choice——所以这种组合直接以 400 拒绝，而不是返回错乱数据。

#### 4.5.3 源码精读

**请求侧收集**：`nvext_passthrough_args`（[lib/llm/src/preprocessor.rs:1261-1290](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L1261-L1290)）逐字段搬进一个 JSON map：`extra_fields`、`metadata_upload` 原样序列化，`token_data.is_some()` 折叠成 `"token_in": true`；`cache_salt` 不是从 nvext 直接读，而是走 `request_cache_salt`（[lib/llm/src/protocols/common/extensions.rs:603-621](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/protocols/common/extensions.rs#L603-L621)）——它实现了公共优先级：`nvext.cache_salt` 是规范入口，顶层 `cache_salt`（不支持字段的请求类型保留的遗留位）是回退，空串视为未设置。

**装进内部请求**：`backend_extra_args`（[lib/llm/src/preprocessor.rs:1317-1356](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L1317-L1356)）把上面的 map 挂到 `extra_args["nvext"]`（L1324-1329），旁边还有 `sampling_options`（透传 `allowed_token_ids` 等不支持字段）与 `reasoning_parser_kwargs`。调用点在 `builder_with_lora` 尾部（[lib/llm/src/preprocessor.rs:2349-2355](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L2349-L2355)），紧挨着 LoRA/盐的路由提示装配。

**工具处理路由**：#13730 把原先散落在流处理函数里的一串 if 提炼成 `ToolProcessingRoute` 枚举（[lib/llm/src/preprocessor.rs:179-191](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L179-L191)）：`MuseUnified` / `QwenUnified`（新一代统一解析器，不改写 chunk）、`ParserV2`（实验性 v2 解析器）、`LegacyJail`（传统监狱）、`PassThrough`（什么都不做）。决策集中在 `tool_processing_route`（[lib/llm/src/preprocessor.rs:3574-3645](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L3574-L3645)），依据是解析器族、`tool_choice` 形态、structural-tag 与 K3 特例——一次决策同时供流处理与 nvext 校验使用，保证两边看到同一条路由。

**门卫**：`validate_legacy_jail_nvext_choice_count`（[lib/llm/src/preprocessor.rs:155-176](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L155-L176)）：`n>1` 且路由是 `LegacyJail` 且 `extra_fields` 点了 choice 级字段（`engine_data`/`routed_experts`/`stop_reason`）→ `invalid_argument_error`（400）。它在流式算子里紧跟 `tool_processing_route` 之后被调用（[lib/llm/src/preprocessor.rs:6040-6054](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L6040-L6054)），即引导解码约束安装之后、请求下发之前。

**响应侧穿狱**：`apply_tool_calling_jail`（[lib/llm/src/preprocessor.rs:4443-4450](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L4443-L4450)）是 dynamo-parsers 里 jail 的边界适配器（拆 `Annotated<Nv>` → 跑 jail → 再包回去）。缓冲结构从旧的 `PendingMetrics` 升级为 `PendingDynamoMetadata`（[lib/llm/src/preprocessor.rs:4460-4485](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L4460-L4485)）：除了原有的 `llm_metrics` 累计，还多了 `nvext`（用 `merge_response_nvext` 逐输入 chunk 合并，入口在 L4543 起的 `jail_input` 映射）与 `response_template`（首个带元数据的 chunk 留下身份字段，如 id/model）。出口侧的投放条件见注释：指标可以搭「payload-only usage chunk」的车，而**客户端可见的 nvext 必须等到一个带 choice 的非 payload-usage 输出**才随行发出。

**回归测试**：`legacy_jail_rejects_multiple_choices_for_choice_specific_nvext`（[lib/llm/src/preprocessor.rs:6456-6481](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L6456-L6481)）把三条边界钉死：`n=2 + engine_data + legacy jail` 拒绝；`n=1` 放行；`n=2 + 请求级字段（timing/worker_id）` 放行；四条非 legacy 路由在 `n=2` 下都不受此约束。

#### 4.5.4 代码实践（源码阅读型）

1. **实践目标**：沿源码走通一条「带 nvext 的请求被拒绝/放行」的判定路径。
2. **操作步骤**：
   a. 从 [lib/llm/src/preprocessor.rs:6040-6054](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L6040-L6054) 出发，向上读 `tool_processing_route`（L3574-3645），列出让一条请求落入 `LegacyJail` 而非 `ParserV2`/`PassThrough` 的全部条件；
   b. 再构造三个假想请求（写在笔记里即可）：① `n=2`、`extra_fields=["engine_data"]`、模型配置了 legacy 解析器；② 同 ① 但 `n=1`；③ 同 ① 但 `extra_fields=["worker_id","timing"]`；
   c. 运行 `cargo test -p dynamo-llm legacy_jail_rejects_multiple_choices_for_choice_specific_nvext` 验证源码阅读结论。
3. **需要观察的现象**：请求 ① 在 `validate_legacy_jail_nvext_choice_count` 处得到 400，错误消息里点名 choice-specific 字段名；② ③ 通过校验进入正常整形。
4. **预期结果**：你能说清「为什么拒绝发生在预处理阶段而不是响应阶段」——choice 级字段一旦进了 jail 就无法按 choice 归位，晚拒绝等于返回错数据，早拒绝是 400。
5. 若本机未构建 Rust workspace，标注「待本地验证」，以静态阅读为准。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `completion_token_ids` 用追加合并，而 `prompt_logprobs` 用覆盖？
**答案**：`completion_token_ids` 是逐 chunk 生成的 token 流，jail 把 N 个输入 chunk 折成 M 个输出时语义是「有序拼接」；覆盖会只剩最后一个 chunk 的 token。`prompt_logprobs` 描述的是同一个 prompt 的一次性结果，逐 chunk 重复携带时只有最新值有意义，追加反而会制造重复数据（合并规则见 [lib/llm/src/protocols/common/extensions.rs:684-705](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/protocols/common/extensions.rs#L684-L705)）。

**练习 2**：请求里 `nvext.token_data` 给了一串 token，透传给后端的是什么？
**答案**：token 本体进入 `PreprocessedRequest.token_ids`（分词被跳过），透传 map 里只有一个 `"token_in": true` 布尔——后端如 SGLang 的 handler 用它识别「token 进来的请求」（消费点如 [components/src/dynamo/sglang/request_handlers/handler_base.py:1140](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/components/src/dynamo/sglang/request_handlers/handler_base.py#L1140-L1140)），语义细节在 u8-l7 展开。

**练习 3**：`cache_salt` 的「规范入口 + 回退」优先级为什么要把空串当作未设置？
**答案**：`nvext.cache_salt` 是规范字段，顶层 `cache_salt` 是部分请求类型保留的遗留位。若调用方显式传了空串（意为「不设盐」）而遗留位有旧值，把空串当有效值会意外吃到遗留盐；视空为缺，才能让「显式清空 + 隐式回退」两种用法都正确（[lib/llm/src/protocols/common/extensions.rs:603-608](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/protocols/common/extensions.rs#L603-L608) 的 docstring）。

---

### 4.6 块哈希进阶：SaltHash、kv-hashing 封装与 MM 路由收口

#### 4.6.1 概念说明

`salt` 是**前缀缓存隔离键**：两个不该共享缓存的请求必须算出不同的盐，两个该共享的必须相同。例如同一基底模型挂不同 LoRA 适配器时，KV 内容不同，必须隔离。`lib/kv-hashing` crate 在 `dynamo-tokens` 之上提供统一的 `Request` 入口与盐推导，保证「前端算的块哈希」与「路由器/worker 算的块哈希」字节级一致——这是跨进程协作的硬契约，任何一侧单独改动都会让缓存查找失配。它和 4.5 也有交接：请求里的 `nvext.cache_salt` 正是盐的调用方入口之一。

#### 4.6.2 核心流程

**盐推导**（[lib/kv-hashing/src/salt.rs:49-69](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/kv-hashing/src/salt.rs#L49-L69)）：

\[
\text{salt\_hash} = 1337 \;\;{\boxed{+}}\;\; \big[\text{XXH3}(\text{lora\_name}, 0)\big] \;\;{\boxed{+}}\;\; \big[\text{XXH3}(\text{salt}, 1)\big]
\]

（`+` 为 wrapping 加；空字符串归一化为 None；方括号表示该项存在时才加。）文件头部的注释给出了与路由器 `compute_block_hash_for_seq` 的对照表——同公式同种子，这就是「字节级一致」的出处（[lib/kv-hashing/src/salt.rs:16-20](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/kv-hashing/src/salt.rs#L16-L20)）。注意**多模态数据不进盐**：它被折叠进逐块哈希，这样「同一张图在同一位置」的请求仍能共享图片之前的文本前缀块。

**统一入口**：`kv_hashing::Request { tokens, lora_name, salt, mm_info }` 用 builder 构造，`build()` 时校验并排序 `mm_info`（无重叠、不越界、非零长度）（[lib/kv-hashing/src/request.rs:68-129](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/kv-hashing/src/request.rs#L68-L129)）；每块的产出被封装为 `UniversalBlock { block_hash, plh }`，其中 PLH 内嵌完整 u64 序列哈希，调用方不再需要侧表（[lib/kv-hashing/src/block.rs:23-33](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/kv-hashing/src/block.rs#L23-L33)）。

#### 4.6.3 源码精读：MM 路由信息——整条流水线的收口

`build_mm_exact_routing_info`（[lib/llm/src/preprocessor.rs:2962-3074](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L2962-L3074)）把本讲所有零件串成一件事：**为路由器构造一条与 worker 侧完全同构的 token 序列**。流程：

1. 门槛检查：`routing_image_token_id`（图片占位符的 token id）、`routing_image_prompt_layout`、`image_token_counter`、`kv_cache_block_size`（为 0）任一缺失就返回 `None`，路由降级为纯文本前缀匹配（L2972-3000）；
2. 对齐检查：token 序列里占位符 token 的个数必须等于图片张数，否则放弃（L3007-3018）；
3. 每张图按 `(width, height)` 用 `image_token_counter.count_tokens` 算出折算 token 数 N（L3022-3025）——`MmImageEntry{mm_hash,width,height}` 由 4.2 的抓取/维度探测路径填充（[lib/llm/src/preprocessor.rs:667-671](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L667-L671)；URL 透传时 `mm_hash` 就是 URL 字节的 XXH3，且刻意不去除签名参数，见 [lib/llm/src/preprocessor.rs:3076-3086](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L3076-L3086)）；
4. 把占位符**展开**成 N 个 pad token（`pad_value` 编码 mm_hash；必要时补 BOS），再补零对齐到 `kv_cache_block_size` 的整数倍（L3035-3055）——注释点明：路由器只哈希完整块，对齐是为了让长度与块数一致；
5. 产出 `MmRoutingInfo { routing_token_ids, expanded_prompt_len }` 挂到 `PreprocessedRequest` 上，交给 u6 的 KV 路由器去算块哈希、查 radix 树。

这条链也解释了 4.1 里「媒体收集先于 token 装配」的顺序安排：构造路由序列需要同时拿到「分词结果」与「图片尺寸」。

#### 4.6.4 代码实践

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

   （注：示例用 `dynamo_tokens::compute_salt_hash_from_bytes` 直接从字节构造盐，与 4.6.2 的公式等价；若想精确复刻 `(salt, lora_name)` 的正则化推导，可在本仓库外的小工程里同时依赖 `dynamo-tokens` 与 `kv-hashing` 两个 crate，改用 `kv_hashing::compute_salt_hash(None, Some("lora-x"))`。）运行：`cargo run -p dynamo-tokens --example block_prefix`。
3. **需要观察的现象**：A 与 B 的前 3 个完整块逐块打印出**完全相同**的 `(block_hash, sequence_hash)`，只有末尾的 PartialBlock 不同；C 与 B 的 token 完全一致但每个哈希都不同。
4. **预期结果**：直观看到「前缀复用的粒度 = 完整块」与「盐=隔离边界」。分词部分（把真实 prompt 变成 token id）可用 Python 的 `transformers.AutoTokenizer.encode` 先行完成再填进示例，见第 5 节综合实践。
5. 具体数值每次构建都一致（确定性哈希）；若 cargo 环境不可用则标注「待本地验证」。

#### 4.6.5 小练习与答案

**练习 1**：`(salt=None, lora=None)` 时盐哈希是多少？这有什么好处？
**答案**：等于 `CHAIN_XXH3_SEED`（1337），即最常见的「裸模型」请求共享同一个种子，天然共享前缀缓存（[lib/kv-hashing/src/salt.rs:16-20](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/kv-hashing/src/salt.rs#L16-L20)）。

**练习 2**：客户端 A 发 `lora_name=""`（空串），客户端 B 不发 lora_name。它们共享缓存吗？
**答案**：共享。`compute_salt_hash` 把空字符串归一化为 `None`（salt.rs L53-54），与路由器在 `lib/kv-router/src/protocols.rs` 的既有行为对齐。

**练习 3**：`build_mm_exact_routing_info` 里 `block_mm_infos` 为什么总是空的？
**答案**：因为 MM 身份（mm_hash）已经被编进 pad token 本身（`pad_value` 编码了 mm_hash），随 token 流进入块哈希，不需要侧信道；这与 vLLM/SGLang 两侧的归一化方案一致（[lib/llm/src/preprocessor.rs:3057-3060](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/preprocessor.rs#L3057-L3060)）。

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

观察：prompt 0 与 1 的前缀 token 完全一致（模板 + 相同 system/user 开头），prompt 2 只有 system 前缀一致。

**第二步：块哈希（Rust）。** 把三组 token id 粘贴进 4.6.4 的示例（`block_size=16`、同一盐），运行 `cargo run -p dynamo-tokens --example block_prefix`，记录表格：

| 对比 | 预期共同完整块数 |
|---|---|
| prompt 0 vs 1 | ≈ 共同前缀 token 数 ÷ 16（向下取整） |
| prompt 0 vs 2 | 仅 system 部分对应的块数 |

若某对比的实测值比预期少 1，检查是不是共同前缀长度恰好跨块边界——这正是「复用粒度是块而不是 token」的直接证据。

**第三步：SSRF 拒绝路径（Python mock）。** 运行 4.2.4 的 `ssrf_probe.py`，把 7 条 URL 的 PASS/REJECT 结果与 [components/src/dynamo/common/http/url_validator.py:103-163](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/components/src/dynamo/common/http/url_validator.py#L103-L163) 的分支一一对应；再用 `DYN_MM_ALLOW_INTERNAL=1 python ssrf_probe.py` 重跑，观察内网 IP 与 http 分支被放开（`metadata.google.internal` 也会被放行，因为 `allow_private_ips` 同时控制 IP 段与主机名两道检查——这正是该开关必须只用于内网部署的原因）。

**产出**：一份实验记录（两个表格 + 拒绝原因清单），它同时证明了你理解了本讲的两个核心机制。

## 6. 本讲小结

- `OpenAIPreprocessor` 是请求进入引擎前的总整形器：模板（MiniJinja）→ 分词（三分支：token 直取 / nvext 预分词 / 本地分词）→ 媒体收集 → `PreprocessedRequest` 装配，并兼任 token 预算门卫（InvalidArgument/400）。
- 媒体抓取受三道 SSRF 防线保护：静态黑名单检查（协议/IP 直连/端口/主机名/CIDR）、请求前 DNS 逐 IP 复查、连接期 `BlocklistResolver` + 重定向逐跳复检；Rust（前端）与 Python（后端）两套实现逐条对齐，`DYN_MM_ALLOW_INTERNAL=1` 是唯一的内网逃生门。图像解码在 #11157 后拆为 `decoders/image` 的后端体系（image_reader / turbojpeg），启动时即检查后端可用性。
- `LocalModel`/ModelDeploymentCard 是预处理器家当的来源；`kv_cache_block_size` 默认 16，由 Python `EntrypointArgs` 透传进 builder，必须与引擎 page 大小一致。
- token 块化给出三种身份：`BlockHash = XXH3(块字节, salt)`、链式 `SequenceHash`（种子常量 1337）、自包含的 128 位 `PositionalLineageHash`；「相同前缀 ⇒ 相同前缀块哈希」是 KV 感知路由的数学地基。
- nvext 透传（#13730）：请求侧 `nvext_passthrough_args` 把 `extra_fields`/`metadata_upload`/`token_in`/`cache_salt` 折进 `extra_args["nvext"]` 抵达后端；响应侧 nvext 以「token 追加、其余最新值胜出」的规则穿过 tool-call jail（`PendingDynamoMetadata` + `merge_response_nvext`）；legacy jail 下 `n>1` 点播 choice 级字段会在预处理阶段被 400 拒绝。
- `salt_hash = 1337 + xxh3(lora) + xxh3(salt)` 是前缀缓存隔离键（调用方入口 `nvext.cache_salt`，顶层字段为回退）；多模态身份不进盐、折叠进块内 13 字节帧编码，图片前的文本前缀仍可复用。
- MM 路由收口：`build_mm_exact_routing_info` 把「分词结果 + 每图折算 token 数」展开成与 worker 同构的路由 token 序列，对齐块大小后交给路由器——本讲所有模块在此汇合。

## 7. 下一步学习建议

- **u4-l5（图像解码后端，本次新增）**：本讲 4.2 只指到了 `ImageDecoder::decode` 的后端选择入口；libjpeg-turbo 的 FFI 封装、CMYK 处理、Python 侧并行解码开关与 `media_decode` 基准都在那一篇展开。
- **u4-l4（worker 类型与服务发现）**：看 `PreprocessedRequest` 出了预处理器之后如何找到愿意接单的 worker，理解 `mm_routing_info` 被谁消费。
- **u6 系列（KV 感知路由）**：本讲的块哈希在 u6-l3（KV 事件流）与 u6-l4（radix 树索引）里变成路由决策的输入；届时回头重读 `salt.rs` 的「router parity」注释会更有体会。
- **u8-l7（RL rollout）**：nvext 四个字段的最终消费方——`token_in`/`token_data`、`metadata_upload`、`extra_fields` 点播与 `cache_salt`——在 RL 训练循环视角下有完整语义。
- **源码延伸阅读**：`lib/kv-router/src/protocols.rs` 的 `compute_block_hash_for_seq`（与本讲 `compute_block_hash` 的对接点）；`lib/llm/src/preprocessor/lightseek_mm.rs`（每图 token 数的注册表）；`lib/llm/src/tokenizers/`（对应 crate `dynamo-tokenizers`，分词器多后端与 L1 前缀缓存）。
