# Rust 原生分词器与 Python 回退

## 1. 本讲目标

本讲聚焦 sglang-grpc 服务里两个最「轻量」但调用最频繁的 RPC：`tokenize` 与 `detokenize`。读完本讲，你应当能够：

- 说清楚为什么要在 Rust 侧实现一套「原生分词器」，以及它和 Python 分词器是什么关系。
- 读懂 `tokenizers.rs` 的三层结构：对外包装 `RustTokenizer`、后端抽象 `TokenizerBackend` trait、唯一真实后端 `HuggingFaceTokenizerBackend`。
- 准确列出 `from_tokenizer_path` 在哪些情况下返回 `None`（即「禁用 Rust 分词器，改走 Python」）。
- 在 `server.rs` 的 `tokenize` / `detokenize` 中追踪「原生优先 → Python 回退」的分发逻辑，并理解为什么回退路径要用 `tokio::task::spawn_blocking` 包住 GIL 调用。

本讲是进阶层的第 8 篇，承接 [u1-l4](u1-l4-start-server-lifecycle.md) 的 `start_server` 生命周期与 [u2-l2](u2-l2-service-impl-overview.md) 的服务实现总览。它不再涉及流式通道与背压，而是一个相对独立、不依赖请求通道的「同步直调 + 原生加速」子主题。

## 2. 前置知识

- **tokenizer（分词器）是什么**：把一段自然语言文本切成一组整数 token id 的组件；反向操作 detokenize 把 token id 还原成文本。它是大模型推理的入口与出口。
- **HuggingFace `tokenizers` 库**：一个用 Rust 编写、提供 Python 绑定的快速分词器库。其模型文件通常是模型目录下的 `tokenizer.json`。sglang-grpc 直接以 Rust 依赖的形式复用它，不再经过 Python。
- **GIL（全局解释器锁）**：CPython 同一时刻只允许一个线程执行 Python 字节码。任何「从 Rust 调 Python 方法」都必须先获取 GIL（`Python::with_gil`），这会带来跨语言开销和潜在的阻塞。本讲的核心动机之一就是「能不碰 GIL 就不碰」。
- **`spawn_blocking`**：Tokio 提供的工具，把一段可能阻塞的同步任务丢到专门的阻塞线程池执行，避免长时间占用异步 worker 线程。本讲的 Python 回退路径就靠它承载 GIL 调用。
- **`Option` 与 `?` 的回退语义**：Rust 里 `Option<T>` 表示「可能有、可能没有」。`from_tokenizer_path` 返回 `Option`，`None` 就是「Rust 分词器不可用，请回退 Python」的信号。

如果你还不熟悉 SGLang 的 `RuntimeHandle` 与 PyBridge，建议先读 [u1-l4](u1-l4-start-server-lifecycle.md) 和 [u2-l5](u2-l5-pybridge-and-channels.md) 的桥接骨架。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `rust/sglang-grpc/src/tokenizers.rs` | Rust 原生分词器的全部实现：包装层 `RustTokenizer`、后端 trait `TokenizerBackend`、HuggingFace 后端、构造函数 `from_tokenizer_path`、后端探测 `load_backend`。 |
| `rust/sglang-grpc/src/server.rs` | gRPC 服务实现。本讲只看 `tokenize` / `detokenize` 两个 async fn 的「原生优先 + Python 回退」分发逻辑。 |
| `rust/sglang-grpc/src/lib.rs` | `start_server` 在启动期一次性持 GIL 抽取 `TokenizerInfo`，并据此构造 `RustTokenizer` 交给 `PyBridge`。 |
| `rust/sglang-grpc/src/bridge.rs` | `PyBridge` 持有 `Option<RustTokenizer>` 与 `context_len`，并提供 Python 回退方法 `tokenize_py` / `detokenize_py`。 |
| `proto/sglang/runtime/v1/sglang.proto` | `TokenizeRequest/Response`、`DetokenizeRequest/Response` 四个消息的契约。 |

数据流总览（本讲的核心链路）：

```
启动期(一次性 GIL)                          运行期(每个 tokenize 请求)
┌─────────────────────┐                    ┌──────────────────────────┐
│ runtime_handle      │                    │ proto::TokenizeRequest   │
│  └─ tokenizer_manager│                    │  └─ text, add_special    │
│      └─ server_args  │── extract ──▶      └────────────┬─────────────┘
│         ├tokenizer_path│ TokenizerInfo                 │
│         ├tokenizer_mode│                              ▼
│         └─context_len  │      ┌──────────────────────────────────┐
└─────────────────────┘       │ RustTokenizer::from_tokenizer_path │
                              │   Some ─▶ 装进 PyBridge            │
                              │   None  ─▶ 不装(运行期回退 Python)  │
                              └──────────────────────────────────┘
                                            ▼
                              tokenize(): bridge.rust_tokenizer()
                                  Some ─▶ tok.encode (无 GIL)
                                  None  ─▶ spawn_blocking(tokenize_py)
```

下面按最小模块逐层展开。

## 4. 核心概念与源码讲解

### 4.1 RustTokenizer：原生分词器的对外包装与整体流程

#### 4.1.1 概念说明

`tokenize` / `detokenize` 这类请求的特点是：**单次极轻量，但被高频调用**（每个 OpenAI 透传请求进来前、每次日志统计都要数 token）。如果每次都从 Rust 跨进 Python 拿 GIL、调 `runtime_handle.tokenize`、再返回，往返开销会盖过真正的分词计算。

`RustTokenizer` 的设计目标就是：**对最常见的那一类分词器（带 `tokenizer.json` 的 HuggingFace 快速分词器），在 Rust 进程内直接完成分词，完全不获取 GIL**。它是一个薄包装，对外只暴露 `encode` / `decode` / `backend_name` 三个能力，把「用哪个后端」这件事藏在内部。

但 SGLang 支持的分词器家族远不止这一种（slow 分词器、没有 `tokenizer.json` 的旧格式等）。因此 `RustTokenizer` 不是「替代」Python 分词器，而是「能加速就加速，不能加速就让位」——它的构造可能失败（返回 `None`），运行期由 `server.rs` 决定是否回退。这种「原生优先、Python 兜底」是本讲贯穿始终的主线。

#### 4.1.2 核心流程

`RustTokenizer` 在整个生命周期里只经历两个阶段：

1. **启动期构造（一次性）**：`start_server` 持一次 GIL，从 `runtime_handle` 抽出 `TokenizerInfo`（路径、模式、上下文长度），调用 `RustTokenizer::from_tokenizer_path`。成功就得到 `Some(RustTokenizer)`，失败得到 `None`。结果装进 `PyBridge`，之后整个服务生命周期不变。
2. **运行期调用（每请求）**：`server.rs` 的 `tokenize` / `detokenize` 通过 `bridge.rust_tokenizer()` 拿到一个 `Option<&RustTokenizer>`。`Some` 走原生路径，`None` 走 Python 回退。

关键点：**构造期与调用期分离**。GIL 只在构造期碰一次；调用期如果走原生路径，永远不碰 GIL。这就是性能收益的来源。

#### 4.1.3 源码精读

`RustTokenizer` 结构体本身极其简单——一个被擦除类型的后端：

> [rust/sglang-grpc/src/tokenizers.rs:47-49](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/tokenizers.rs#L47-L49) —— `RustTokenizer` 只持有 `Box<dyn TokenizerBackend>`，把具体后端擦除成 trait object。

`encode` / `decode` / `backend_name` 全是直接转交给后端：

> [rust/sglang-grpc/src/tokenizers.rs:98-110](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/tokenizers.rs#L98-L110) —— 三个公开方法都是薄委托。

构造发生在 `start_server` 里，把启动期抽出的 `TokenizerInfo` 喂给 `from_tokenizer_path`，结果（`Some` 或 `None`）原样塞进 `PyBridge`：

> [rust/sglang-grpc/src/lib.rs:205-211](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L205-L211) —— `tokenizer_path.as_deref().and_then(...)`：路径缺失时直接得到 `None`，连后端探测都不做。

`PyBridge` 则把它当普通字段存着，并提供一个只读访问器：

> [rust/sglang-grpc/src/bridge.rs:116-124](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L116-L124) —— `rust_tokenizer()` 返回 `Option<&RustTokenizer>`，`context_len()` 返回启动期摘出的上下文长度。

注意一个**容易误解的点**：`context_len`（上下文长度）在 `from_tokenizer_path` 里只被写进 `tracing::info!` 日志，**并不会被用来截断 token**。原生 `encode` 返回的是完整 token 列表；`context_len` 的真正用途是在 `tokenize` 响应里作为 `max_model_len` 字段回传给客户端（见 4.4）。

#### 4.1.4 代码实践

这是一个源码阅读型实践，目标是建立「启动期数据流」的直觉。

1. 实践目标：在源码里走通「Python RuntimeHandle → TokenizerInfo → RustTokenizer → PyBridge」这条构造链。
2. 操作步骤：
   - 打开 `src/lib.rs`，读 `extract_tokenizer_info`（94–139 行），注意它如何从 `tokenizer_manager.server_args` 取 `tokenizer_path`、`tokenizer_mode`，并有三段降级（`tokenizer_path` → `model_path` → `tokenizer_manager.model_path`）。
   - 找到上文引用的 205–211 行，确认 `Option` 的传递。
   - 打开 `src/bridge.rs` 的 `PyBridge::new`（95–114 行），确认 `rust_tokenizer` 与 `context_len` 被存为字段。
3. 需要观察的现象：构造链中**没有任何运行期状态**——一旦 `PyBridge` 建好，`rust_tokenizer` 字段在服务整个生命周期里都不变。
4. 预期结果：你能用一句话回答「Rust 分词器是在请求处理时加载的吗？」——不是，是启动期一次性加载。
5. 待本地验证：无（纯源码阅读）。

#### 4.1.5 小练习与答案

**练习 1**：如果 `runtime_handle` 完全没有 `tokenizer_path`（且没有 `model_path` 可降级），`RustTokenizer` 会怎样？运行期 `tokenize` 走哪条路？

**答案**：`extract_tokenizer_info` 里 `tokenizer_path` 为 `None`，`lib.rs:205` 的 `as_deref()` 得到 `None`，`and_then` 短路，`RustTokenizer` 为 `None`，不装入 `PyBridge`。运行期 `bridge.rust_tokenizer()` 恒为 `None`，`tokenize` 走 Python 回退路径。

**练习 2**：`RustTokenizer::encode` 签名返回 `Result<Vec<u32>, String>`，而 proto 里 `tokens` 是 `repeated int32`。这个类型差异在哪里被抹平？

**答案**：在 `server.rs` 的 `tokenize` 原生分支里，用 `tokens.iter().map(|&t| t as i32).collect()` 把 `u32` 转成 `i32`（见 4.4）。这也意味着超过 `i32` 上限的 token id 理论上会溢出，但现实中词表大小远小于 21 亿，不构成问题。

---

### 4.2 可插拔后端：TokenizerBackend trait 与 HuggingFaceTokenizerBackend

#### 4.2.1 概念说明

为什么 `RustTokenizer` 内部要用一个 trait？因为「Rust 原生分词器」未来可能不止支持 HuggingFace 一家——比如 SentencePiece、自定义后端等。为了让「换后端」不影响 `RustTokenizer` 的对外 API，作者抽出了一个 **后端抽象** `TokenizerBackend` trait，并用 trait object `Box<dyn TokenizerBackend>` 擦除具体类型。

这个 trait 是 **crate 私有**的（`trait TokenizerBackend`，没有 `pub`），外部代码无法实现它；它纯粹是 `tokenizers.rs` 内部的扩展点。目前唯一实现是 `HuggingFaceTokenizerBackend`，它包装 `tokenizers` crate 的 `Tokenizer`。

#### 4.2.2 核心流程

trait 定义了三个方法的契约：

```
trait TokenizerBackend: Send + Sync {
    fn name(&self) -> &'static str;                                      // 用于日志
    fn encode(&self, text, add_special_tokens) -> Result<Vec<u32>, String>;
    fn decode(&self, ids, skip_special_tokens)  -> Result<String, String>;
}
```

- `Send + Sync` 约束：后端要能被 `Arc<PyBridge>` 跨线程共享（服务是多线程 Tokio 运行时）。
- 错误类型统一为 `String`（人类可读消息），由上层映射成 gRPC `Status`。

**扩展点**在自由函数 `load_backend`：它按顺序尝试各个后端的探测函数，返回第一个成功的 `Box<dyn TokenizerBackend>`。新增后端就是在这里加一行探测。

#### 4.2.3 源码精读

trait 定义极简：

> [rust/sglang-grpc/src/tokenizers.rs:5-9](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/tokenizers.rs#L5-L9) —— 三个方法的契约，`Send + Sync` 保证跨线程安全。

唯一后端 `HuggingFaceTokenizerBackend` 从 `tokenizer.json` 加载，并实现三个方法：

> [rust/sglang-grpc/src/tokenizers.rs:11-41](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/tokenizers.rs#L11-L41) —— `from_file` 用 `Tokenizer::from_file` 加载；`encode` 调 `inner.encode(...).get_ids()`；`decode` 调 `inner.decode(ids, skip_special_tokens)`。

`load_backend` 是扩展点，注释明确写了「在此添加新的原生后端探测，不支持的格式应返回 `Err` 以便回退 Python」：

> [rust/sglang-grpc/src/tokenizers.rs:113-118](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/tokenizers.rs#L113-L118) —— 目前只有 HuggingFace 一条探测；返回 `Result`，失败变 `Err(String)`。

#### 4.2.4 代码实践

1. 实践目标：理解「新增一个分词器后端」需要改哪里。
2. 操作步骤：
   - 在 `tokenizers.rs` 里定义一个新结构体，例如 `struct DummyBackend;`，实现 `TokenizerBackend` 三个方法（`name` 返回 `"dummy"`，`encode`/`decode` 可先返回 `Err("not implemented".into())`）。
   - 在 `load_backend` 里，在 HuggingFace 探测**之前**或**之后**加一行 `DummyBackend::try_from_file(tokenizer_json).map(|b| Box::new(b) as Box<dyn TokenizerBackend>)`。
   - 思考：放在 HuggingFace 之前 vs 之后，探测顺序会如何影响结果？（目前只有一条，顺序无影响；多条时按短路返回第一个 `Ok`。）
3. 需要观察的现象：`RustTokenizer` 的对外 API（`from_tokenizer_path` / `encode` / `decode`）完全不需要改动——这就是 trait object 抽象的好处。
4. 预期结果：你能列出新增后端的「最小改动清单」——只动 `tokenizers.rs` 一个文件，且不改任何公开签名。
5. 待本地验证：编译是否通过需本地 `cargo build` 确认（本讲不修改源码，仅设计）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `TokenizerBackend` 是 crate 私有 trait，而 `RustTokenizer` 是 `pub`？

**答案**：`RustTokenizer` 是对外（`lib.rs`、`bridge.rs`）使用的类型，必须 `pub`；而 `TokenizerBackend` 只是内部实现细节与扩展点，外部（包括 Python 扩展的使用者）不需要也不应感知具体后端类型，故设为私有，缩小 API 面。

**练习 2**：`HuggingFaceTokenizerBackend::encode` 返回 `encoding.get_ids().to_vec()`。这里的 `.to_vec()` 是否多余？

**答案**：不多余。`get_ids()` 返回的是 `&[u32]` 切片引用，其生命周期绑在 `encoding` 上；要让 token 列表在 `encoding` 离开作用域后仍可用，必须 `.to_vec()` 拷贝出独立的 `Vec<u32>`。

---

### 4.3 from_tokenizer_path：三道 None 关卡

#### 4.3.1 概念说明

`from_tokenizer_path` 是 `RustTokenizer` 的构造函数，也是本讲最关键的函数。它的签名返回 `Option<Self>`——`None` 就是「Rust 分词器不可用，请回退 Python」。

它内部设了**三道关卡**，任何一道不通过都返回 `None`：

1. **找不到 tokenizer.json**：路径既不是 `tokenizer.json` 文件本身，目录下也没有 `tokenizer.json`。
2. **显式 slow 模式**：`tokenizer_mode == "slow"` 时，Python 侧用的是 slow 分词器（基于 Python 实现的 `transformers` 分词器），Rust 快速分词器与它不等价，必须让位。
3. **后端加载失败**：`load_backend` 返回 `Err`（比如 `tokenizer.json` 损坏、格式 HuggingFace 后端读不了）。

注意三道关卡的**日志级别不同**：前两道是 `info`（这是预期的「不支持」，正常降级），第三道是 `warn`（这是「本想用 Rust 但加载失败」，值得注意）。

#### 4.3.2 核心流程

伪代码（省略日志）：

```
fn from_tokenizer_path(path, mode, ctx_len) -> Option<Self>:
    json = resolve_tokenizer_json(path)        # 关卡1：定位 tokenizer.json
    if json is None: return None

    if mode == Some("slow"): return None        # 关卡2：slow 模式让位

    backend = load_backend(json)                # 关卡3：后端能否加载
    match backend:
        Ok(b)  => return Some(RustTokenizer{backend: b})
        Err(_) => return None
```

`resolve_tokenizer_json` 的两条分支很关键：

```
fn resolve_tokenizer_json(path):
    candidate = if path.is_file() { path }            # path 直接就是 tokenizer.json
                else { path.join("tokenizer.json") }   # path 是模型目录
    return candidate.exists() ? Some(candidate) : None
```

也就是说，`tokenizer_path` 既可以是「模型目录」（常见），也可以直接指向「某个 `tokenizer.json` 文件」。

#### 4.3.3 源码精读

`from_tokenizer_path` 的完整三段式：

> [rust/sglang-grpc/src/tokenizers.rs:54-96](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/tokenizers.rs#L54-L96) —— 关卡1 `resolve_tokenizer_json` 返回 `None`（info 日志）；关卡2 `matches!(tokenizer_mode, Some("slow"))`（info 日志）；关卡3 `load_backend` 的 `Err` 分支（warn 日志）。

`resolve_tokenizer_json` 的双分支定位逻辑：

> [rust/sglang-grpc/src/tokenizers.rs:120-127](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/tokenizers.rs#L120-L127) —— `path.is_file()` 区分「直接给文件」与「给目录」，`.exists()` 决定是否 `Some`。

#### 4.3.4 代码实践

这就是本讲规格指定的核心实践之一。

1. 实践目标：穷举所有让 `from_tokenizer_path` 返回 `None` 的情形。
2. 操作步骤：对照 54–96 行，逐一列出每个 `return None` 点及其触发条件与日志级别，填入下表。

   | 关卡 | 触发条件 | 返回值 | 日志级别 |
   | --- | --- | --- | --- |
   | 1 | `resolve_tokenizer_json` 返回 `None` | `None` | `info` |
   | 2 | `tokenizer_mode == Some("slow")` | `None` | `info` |
   | 3 | `load_backend` 返回 `Err` | `None` | `warn` |

   （此外还有一道「隐式关卡 0」：`lib.rs:205` 在 `tokenizer_path` 本身为 `None` 时就不调用本函数，直接得到 `None`。）

3. 需要观察的现象：三道关卡只有第三道是「意外失败」（warn），前两道都是「主动放弃」（info）。
4. 预期结果：你能解释为什么对 slow 分词器要主动让位——因为 HuggingFace Rust 快速分词器与 Python slow 分词器的分词结果可能不一致，混用会导致 token id 错位。
5. 待本地验证：无。

#### 4.3.5 小练习与答案

**练习 1**：`tokenizer_mode` 取值 `"auto"` 或 `None` 时，关卡2 是否拦截？

**答案**：不拦截。关卡2 用 `matches!(tokenizer_mode, Some("slow"))`，只有精确等于 `"slow"` 才命中。`"auto"`（SGLang 默认）和 `None` 都会通过，继续走后端加载。

**练习 2**：假设 `tokenizer_path` 指向一个目录，目录里有 `tokenizer.json`，但文件内容损坏导致 `Tokenizer::from_file` 报错。`from_tokenizer_path` 走哪条路、返回什么、日志级别是什么？

**答案**：关卡1 通过（文件存在），关卡2 假设非 slow 也通过，关卡3 `load_backend` → `HuggingFaceTokenizerBackend::from_file` 报错 → `Err`，命中 87–94 行的 `Err` 分支，返回 `None`，日志级别 `warn`，服务回退 Python 分词。

---

### 4.4 tokenize / detokenize 的分发：原生优先 + Python 回退

#### 4.4.1 概念说明

`server.rs` 的 `tokenize` / `detokenize` 是本讲设计的「指挥部」。它们不创建请求通道、不涉及 rid、不走 `submit_request`——这是它们与 [u2-l2](u2-l2-service-impl-overview.md) 里那些「数据型 RPC」最大的区别。它们是**同步直调型** RPC：拿到请求 → 立刻算 → 立刻返回。

分发逻辑是经典的「短路回退」模式：

- **原生路径（`Some`）**：直接 `tok.encode` / `tok.decode`，**完全不碰 GIL**，不 `spawn_blocking`，在当前 async 任务里同步完成（因为原生分词是纯 Rust 计算，不会长时间阻塞）。
- **Python 回退路径（`None`）**：用 `tokio::task::spawn_blocking` 把 `bridge.tokenize_py` / `bridge.detokenize_py` 丢到阻塞线程池执行——因为这两个方法内部要 `Python::with_gil` 调 Python，是同步阻塞调用，绝不能在 async worker 线程上直接 `await`。

#### 4.4.2 核心流程

`tokenize` 的分发：

```
add_special = req.add_special_tokens.unwrap_or(true)      # proto optional，默认 true

if let Some(tok) = bridge.rust_tokenizer():               # 原生优先
    tokens = tok.encode(text, add_special)?               # Err -> Status::internal
    return TokenizeResponse { tokens, count, max_model_len, input_text }

# 回退 Python
json_str = spawn_blocking(|| bridge.tokenize_py(text, add_special)).await??   # 注意双 ?
v = serde_json::from_str(json_str)?
return TokenizeResponse {
    tokens: v["tokens"] as array -> i32,
    count:  v["count"] as i64 -> i32 (默认0),
    max_model_len, input_text,
}
```

`detokenize` 同构，但有两处不同：
1. 先校验 `tokens` 全部非负，负数直接 `INVALID_ARGUMENT`。
2. 原生路径 `tok.decode(&ids, true)` 的 `skip_special_tokens` **硬编码为 `true`**——因为 proto 的 `DetokenizeRequest` 没有 `skip_special_tokens` 字段可读。

回退路径里那个「双 `?`」（`.await??`）值得展开：第一个 `?` 处理 `spawn_blocking` 本身的 `JoinError`（比如运行时关闭），第二个 `?` 处理闭包返回的 `PyResult`（即 Python 调用失败，经 `pyerr_to_status` 转成 gRPC `Status`）。

#### 4.4.3 源码精读

`tokenize` 的原生分支（无 GIL，直接返回）与回退分支（`spawn_blocking` + JSON 解析）：

> [rust/sglang-grpc/src/server.rs:474-520](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L474-L520) —— 482–493 行原生分支；496–503 行 `spawn_blocking` + 双 `?`；505–519 行 `serde_json` 解析 `tokens`/`count`。

`detokenize` 的负数校验、原生解码（`skip_special_tokens` 硬编码 `true`）与回退：

> [rust/sglang-grpc/src/server.rs:522-555](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L522-L555) —— 527–531 行参数校验；534–538 行原生解码；541–548 行 `spawn_blocking` 回退；550–554 行 JSON 解析 `text`。

Python 回退的两个桥接方法都走 `Python::with_gil` 调 `runtime_handle` 的同名方法，返回 JSON 字符串：

> [rust/sglang-grpc/src/bridge.rs:288-306](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L288-L306) —— `tokenize_py` 调 `runtime_handle.tokenize(text, add_special_tokens)`；`detokenize_py` 调 `runtime_handle.detokenize(tokens)`。

错误映射用的是 [u2-l2](u2-l2-service-impl-overview.md) 介绍过的 `pyerr_to_status`：Python 的 `ValueError`/`TypeError` → `INVALID_ARGUMENT`，其余 → `INTERNAL`：

> [rust/sglang-grpc/src/server.rs:66-76](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L66-L76) —— 回退路径里 `pyerr_to_status(e, "Tokenize failed")` 把 Python 异常分诊为客户端错误或服务端错误。

#### 4.4.4 代码实践

这是本讲规格指定的第二个核心实践——追踪 `tokenize` 的两条路径。

1. 实践目标：当 `bridge.rust_tokenizer()` 为 `Some` 时返回什么；为 `None` 时如何回退并解析 JSON。
2. 操作步骤：
   - **原生路径**（`Some`）：读 482–493 行。`tok.encode(&req.text, add_special)` 得 `Vec<u32>`；`count = tokens.len()`；返回的 `TokenizeResponse` 四个字段分别是：`tokens`（`u32`→`i32`）、`count`、`max_model_len: bridge.context_len()`、`input_text: req.text`。注意**没有任何 GIL 调用，也没有 `spawn_blocking`**。
   - **回退路径**（`None`）：读 496–519 行。`spawn_blocking` 里调 `bridge.tokenize_py(&text, add_special)`，返回一个 JSON 字符串（形如 `{"tokens":[...],"count":N,...}`）；用 `serde_json::from_str` 解析，`v["tokens"]` 按 `i64→i32` 映射、`v["count"]` 取 `i64`（默认 0）；`max_model_len` 与 `input_text` 的填法和原生路径**完全一致**。
   - 对比：两条路径产出的 `TokenizeResponse` 字段语义相同，区别只在 `tokens`/`count` 的来源（Rust 计算 vs Python JSON）。
3. 需要观察的现象：原生路径里 `encode` 的错误用 `Status::internal`（`map_err(Status::internal)`），而回退路径里 Python 错误用 `pyerr_to_status`（可能变成 `INVALID_ARGUMENT`）——两者的错误码语义不同。
4. 预期结果：你能画出一张对照表，说明两条路径在「是否持 GIL / 是否 spawn_blocking / 错误映射 / 数据来源」上的差异。
5. 待本地验证：若想实跑，需启动一个带 gRPC 前端的 SGLang 服务并用 gRPC 客户端发 `tokenize` 请求，对比 `tokenizer_mode=fast`（走原生）与 `tokenizer_mode=slow`（走回退）两次的日志——本环境暂无法验证，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`tokenize` 回退路径里 `spawn_blocking(...).await` 后面跟了两个 `?`（`.await??`）。这两个 `?` 各处理什么错误？

**答案**：第一个 `?` 处理 `spawn_blocking` 返回的 `Result<T, JoinError>`——即任务本身没能正常完成（如运行时关闭、panic）；第二个 `?` 处理闭包返回的 `PyResult<String>` 里的 `PyErr`，经 `pyerr_to_status` 转成 gRPC `Status`。

**练习 2**：为什么原生路径**不**用 `spawn_blocking`，而回退路径必须用？

**答案**：原生路径的 `tok.encode` / `tok.decode` 是纯 Rust 同步计算，耗时极短且不获取 GIL，直接在 async 任务里同步调用即可，不会阻塞 Tokio worker。回退路径的 `tokenize_py` / `detokenize_py` 内部要 `Python::with_gil` 并同步执行 Python 代码，可能长时间阻塞；若直接在 async 任务里调用，会卡住一个 worker 线程，所以必须 `spawn_blocking` 丢到阻塞线程池。

**练习 3**：`detokenize` 原生路径写的是 `tok.decode(&ids, true)`，第二个参数 `true` 是什么意思？为什么是硬编码而不是来自请求？

**答案**：`true` 是 `skip_special_tokens`（跳过 special token，如 `<s>`、`</s>`）。硬编码是因为 proto 的 `DetokenizeRequest` 只有 `repeated int32 tokens` 一个字段，没有 `skip_special_tokens` 入参可读。这与 `tokenize` 端 `add_special_tokens` 能从请求读取形成不对称——属于当前 proto 契约的限制。

## 5. 综合实践

把本讲三块知识（后端抽象 / None 条件 / 双路径分发）串成一个完整任务。

**任务：画一张「tokenize 请求全链路决策图」并回答三个问题。**

1. 假设你用 `--tokenizer-mode slow` 启动一个 SGLang 服务并开启 gRPC 前端：
   - 启动期：`extract_tokenizer_info` 抽出的 `tokenizer_mode` 是什么？`from_tokenizer_path` 命中第几道关卡？`PyBridge.rust_tokenizer` 最终是 `Some` 还是 `None`？
   - 运行期：客户端发来一个 `tokenize` 请求，`bridge.rust_tokenizer()` 返回什么？走原生路径还是 Python 回退？回退时 `spawn_blocking` 里调的是哪个方法？
   - 若此时 Python 的 `tokenize` 抛了一个 `ValueError`，客户端收到的 gRPC 状态码是 `INVALID_ARGUMENT` 还是 `INTERNAL`？依据是哪段代码？

2. 参考做法（自行核对）：
   - 启动期：`tokenizer_mode = Some("slow")`；命中关卡2（`tokenizers.rs:68`）；`from_tokenizer_path` 返回 `None`；`PyBridge.rust_tokenizer` 为 `None`。
   - 运行期：`bridge.rust_tokenizer()` 返回 `None`；走 Python 回退（`server.rs:496`）；`spawn_blocking` 调 `bridge.tokenize_py`（`bridge.rs:289`），它再调 `runtime_handle.tokenize`。
   - `ValueError` 经 `pyerr_to_status`（`server.rs:66-76`）判为客户端错误，返回 `INVALID_ARGUMENT`。

3. 进阶（可选）：如果把 `--tokenizer-mode` 改回默认 `auto`，且模型目录有合法 `tokenizer.json`，请预测 `tokenize` 走原生路径后，响应里 `max_model_len` 字段的值来自哪里？（答：`bridge.context_len()`，即启动期从 `model_config.context_len` 摘出的值，`lib.rs:124-131`。）

## 6. 本讲小结

- `RustTokenizer` 是一个薄包装，内部用 `Box<dyn TokenizerBackend>` 擦除具体后端，对外只暴露 `encode` / `decode` / `backend_name`。
- `TokenizerBackend` trait 是 **crate 私有**的扩展点；目前唯一实现 `HuggingFaceTokenizerBackend` 包装 `tokenizers` crate；新增后端只需在 `load_backend` 加一行探测。
- `from_tokenizer_path` 设三道 `None` 关卡：找不到 `tokenizer.json`（info）、`tokenizer_mode=slow`（info）、后端加载失败（warn）；另有「路径缺失」隐式关卡在 `lib.rs`。
- `tokenize` / `detokenize` 是**同步直调型** RPC，不走请求通道：原生路径不碰 GIL、不 `spawn_blocking`；Python 回退路径用 `spawn_blocking` 包住 `Python::with_gil` 调用。
- 回退路径的 `spawn_blocking(...).await??` 双 `?` 分别处理 `JoinError` 与 `PyErr`，后者经 `pyerr_to_status` 映射成 gRPC 状态码。
- 两条路径产出的 `TokenizeResponse` 字段语义一致，`max_model_len` 都取自启动期摘出的 `context_len`——原生 `encode` **不做截断**，`context_len` 仅作信息回传。

## 7. 下一步学习建议

- 想看 Rust→Python 的**通用**回退写法（而非分词器专用），可读 `server.rs` 的 `health_check` / `get_model_info` / `list_models`，它们同样用 `spawn_blocking` + `pyerr_to_status`，但返回的是控制型 JSON，可对照 [u2-l4](u2-l4-unary-rpcs-and-json.md)。
- 想深入 gRPC 错误码的分诊规则，进到专家层读 [u3-l3 错误映射：PyErr 到 gRPC Status](u3-l3-error-mapping.md)，那里系统讲解 `pyerr_to_status`、`terminal_error_status`、`closed_stream_status` 的完整映射。
- 想验证「新增后端」的扩展流程，可读 [u3-l6 测试组织与扩展实践](u3-l6-testing-and-extension.md)，其中包含为 `TokenizerBackend` 新增占位后端并在 `load_backend` 注册探测的完整步骤。
- 若关心启动期 GIL 抽取的更多细节（`TokenizerInfo` 的降级链、`try_get_attr` 家族），回到 [u1-l4](u1-l4-start-server-lifecycle.md) 复习 `start_server` 与 `extract_tokenizer_info`。
