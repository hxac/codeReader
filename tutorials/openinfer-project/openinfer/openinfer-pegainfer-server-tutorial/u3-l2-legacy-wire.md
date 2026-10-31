# u3-l2 legacy 契约线类型:GenerateRequest、TokenEvent 与 TokenSink

## 1. 本讲目标

上一讲(u3-l1)我们画出了 engine 模块「两代契约并存」的地图。本讲深入 legacy 侧的**线类型**(wire types)——也就是跨线程真正被搬运的那些数据结构。读完本讲,你应该能:

1. 逐一说出 `GenerateRequest` 十二个字段的职责,特别是 `data_parallel_rank` 的路由含义、`token_tx` 的事件出口、`trace_parent` 的链路追踪上下文;
2. 区分 `TokenEvent` 的七个事件变体与 `FinishReason` 三个完结原因,并复述「Scheduled 先行、terminal 恰一次」这条约定;
3. 解释 `TokenSink` 的共享打标通道设计:为什么一个引擎所有请求共用一条通道、事件如何被打上 `RequestTag`、以及为什么取消一个请求从「drop 接收端」改成了「原子 abort reason」。

这些类型今天仍服务于 deepseek-v2-lite、glm52、kimi-k2 这三条尚未迁移到 step 契约的模型线,也是理解 u4 单元里 legacy 引擎桥(`LocalEngineBridge`)如何 demux 事件流的前提。

## 2. 前置知识

本讲只需要几块通用的 Rust 并发基础,每条用一段话讲透:

- **mpsc 无界通道(tokio 版)**:`tokio::sync::mpsc::unbounded_channel()` 返回一对 sender/receiver。sender `send` 永不阻塞、绝不失败——除非 receiver 已被 drop;receiver 端 `try_recv` 取下一条消息。它是「生产者(调度器线程)→ 消费者(前端 demux 循环)」的单向传送带。
- **`Arc<T>`(原子引用计数指针)**:让多个持有者共享同一份数据,克隆只是一次计数加一。本讲里 `Arc<str>` 用来共享请求 id 字符串,`Arc<AtomicU8>` 用来共享取消标志。
- **原子量与内存序**:`AtomicU8::store/load` 是不带锁的读写;`Release` 写保证「这次写之前的所有普通写」对看到它的读者可见,`Acquire` 读与之配对。本讲的 abort 标志就用这一对序。
- **每请求事件流的「生命周期」**:一个推理请求从被引擎接收,到产出 token,再到以某种方式终结,这条时间线上每一步在协议里都是一个事件。理解本讲不需要 GPU 知识——这些类型全部住在 CPU 侧的 frontend 里。

## 3. 本讲源码地图

所有类型都经 `pegainfer_frontend::engine::*` 的扁平 re-export 暴露(见 u3-l1),所以外部代码从不写 `engine::sink::TokenSink` 这样的深路径。

| 文件 | 角色 |
|------|------|
| `pegainfer-frontend/src/engine/request.rs` | `GenerateRequest`——进引擎的请求本体,全 32 行 |
| `pegainfer-frontend/src/engine/event.rs` | `TokenEvent` 事件族、`FinishReason`、`TokenLogprob`、`RequestTag` |
| `pegainfer-frontend/src/engine/sink.rs` | `TokenSink` 共享打标通道 + `RequestAbortReason` |
| `pegainfer-frontend/src/sampler.rs` | `SamplingParams`——请求携带的采样参数(`GenerateRequest.params` 的类型) |
| `pegainfer-frontend/src/engine/handle.rs` | `EngineHandle`——本讲只看它作为 `GenerateRequest` 的提交方与 `Rejected` 事件的另一个生产者 |
| `pegainfer-frontend/src/vllm/bridge.rs`(辅读) | 生产侧唯一构造点:前端如何把 HTTP 请求折成 `GenerateRequest`,以及 abort 在哪被置位 |
| `pegainfer-frontend/src/engine/kv.rs`(辅读) | `SubmittedRequest = (GenerateRequest, KvPrefix)`——提交通道上实际搬运的元组 |
| `pegainfer-frontend/src/engine/mod.rs`(辅读) | legacy 契约四模块的分组文档与扁平 re-export |

## 4. 核心概念与源码讲解

### 4.1 GenerateRequest:进引擎的请求是什么形状

#### 4.1.1 概念说明

`GenerateRequest` 是 legacy 契约里「一个生成请求」的完整描述:prompt 是什么、怎么采样、最多生成多少、事件发回哪里。它由协议栈(前端)构造,经 `EngineHandle` 的提交通道进入模型 crate 的调度器线程,此后**只读**。

理解它的关键不是背字段,而是注意它的两个「不在场证明」:

1. 它**不含任何引擎内部状态**——没有 KV 页表、没有 batch 槽位。它是纯输入,引擎把它翻译成自己的调度器状态(对比:新一代契约的 `Request` 带 `history_tokens`/`history_marks`,那已经是引擎视角的会话状态)。
2. 它**不携带返回通道的另一半**——只有一个 `token_tx`(下述),事件的接收端由前端 demux 循环持有,请求对象自己不知道消费者是谁。

#### 4.1.2 核心流程

一个请求穿过这条接缝的旅程:

```
HTTP 请求
  │  vllm 协议栈:分词、抽采样参数(bridge.rs 构造点,见 4.1.3)
  ▼
GenerateRequest ──► EngineHandle::submit(req)   ← 附 KvPrefix::none()(无前缀解析)
  │                     │
  │                     ▼  tokio mpsc 提交通道,搬运 (GenerateRequest, KvPrefix)
  ▼
模型 crate 的调度器线程:排队 → prefill → decode
  │  每个 token / 每次状态变化,经 req.token_tx 发 TokenEvent(4.2)
  ▼
前端 demux 循环:按 tag 分发到各请求的输出流 → SSE 响应
```

十二个字段的职责一览(类型与行号见 4.1.3 的精读链接):

| 字段 | 类型 | 职责 |
|------|------|------|
| `request_id` | `Option<String>` | 外部请求 id(vLLM 的 `request_id`),供调度器侧日志/追踪引用 |
| `queued_at_unix_s` | `Option<f64>` | 请求到达时刻(UNIX 秒),用于排队延迟统计;`None` 时引擎自己取当前时刻 |
| `trace_parent` | `Option<SpanContext>` | 链路追踪上下文;调度器把它作为 queue/prefill/decode 子 span 的父 |
| `data_parallel_rank` | `Option<usize>` | **路由指令**:`Some(r)` 钉死去第 r 个分区;`None` 交给 handle 选最低负载分区 |
| `prompt_tokens` | `Vec<u32>` | 已分词的 prompt(注意:是 token id,不是文本) |
| `params` | `SamplingParams` | 采样参数(temperature/top_k/top_p/min_p/seed/ignore_eos) |
| `max_tokens` | `usize` | 生成 token 上限 |
| `lora_adapter` | `Option<String>` | 本次推理要挂的 LoRA 适配器名 |
| `kv_transfer_params` | `Option<Value>` | 不透明的 P/D(预填充/解码分离)交接元数据,原样透传 |
| `token_tx` | `TokenSink` | **事件出口**:调度器所有回信都从这里发(4.3) |
| `logprobs` | `Option<usize>` | 要求返回每 token 的 top-N logprobs |
| `prompt_logprobs` | `Option<usize>` | 要求返回 prompt 各 token 的 logprobs(对应 `PromptTokens` 事件) |

#### 4.1.3 源码精读

先看请求本体——整个文件只有两个 import 和一个结构体:

[pegainfer-frontend/src/engine/request.rs:L4-L32](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/request.rs#L4-L32)
——`GenerateRequest` 的全部定义:十二个公开字段,没有方法、没有 trait 实现。一个「纯数据」的线类型。

三个字段的文档注释值得逐句读:

[pegainfer-frontend/src/engine/request.rs:L7-L14](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/request.rs#L7-L14)
——`trace_parent` 的文档解释了三件事:调度器把自己的 queue/prefill/decode span 挂到前端开的 trace 上,使主机侧耗时分解落在同一条链;tracing 关闭时为 `None`,调度器「skips span work entirely」(零开销);`SpanContext` 是 `Copy`,能随调度器里 `Clone` 的请求状态一起复制——这是选它而不是持有活 `Span`(非 `Clone`)的原因。

[pegainfer-frontend/src/engine/request.rs:L15-L18](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/request.rs#L15-L18)
——`data_parallel_rank` 的文档直接写明了路由语义:`None` 时 handle 把请求放到最低负载分区,「waiting requests weigh 4x」——即评分 \( s = \text{running} + 4 \times \text{waiting} \)(完整路由逻辑属 `EngineHandle`,u3-l6 精读,本讲只记这条路由指令的存在)。

[pegainfer-frontend/src/engine/request.rs:L23-L29](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/request.rs#L23-L29)
——`kv_transfer_params` 从请求的 `vllm_xargs.kv_transfer_params` 而来,是不透明 JSON;`token_tx` 的文档预告了 4.3 的核心设计:「同一引擎的所有请求共享一条打标输出通道,前端按 tag demux」。

`params` 字段的类型在同级的 sampler 模块里:

[pegainfer-frontend/src/sampler.rs:L1-L29](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/sampler.rs#L1-L29)
——`SamplingParams` 六个字段全部可 `Copy`,带 serde 序列化;`Default` 是一组「贪婪解码」缺省:`temperature: 0.0, top_k: -1, top_p: 1.0, min_p: 0.0, seed: None, ignore_eos: false`。注意 `min_p` 的语义是相对概率阈值——概率 \( p < \text{min\_p} \cdot p_{\max} \) 的 token 被掩掉。

最后看生产侧的唯一构造点——vllm 桥如何把一条 HTTP 请求折成这个结构体:

[pegainfer-frontend/src/vllm/bridge.rs:L319-L352](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge.rs#L319-L352)
——构造三部曲:L319-L321 铸 tag(`Arc<str>`)、新建 abort 原子量、`TokenSink::new` 拼出事件出口;L330-L336 开请求级根 span 并导出 `SpanContext`(tracing 关闭时是 `Span::noop()`,`from_span` 得 `None`,与 4.1.3 第一条链接的文档互证);L337-L352 用字面量填满全部字段并 `self.handle.submit(...)`。值得划线的是 L342:`data_parallel_rank: Some(self.engine_index as usize)`——**桥总是绑定 rank**,从不留 `None`;「最低负载」路径留给直连驱动(基准测试、模拟器)使用。

提交通道上实际搬运的不是裸请求,而是二元组:

[pegainfer-frontend/src/engine/kv.rs:L90](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/kv.rs#L90)
——`pub type SubmittedRequest = (GenerateRequest, KvPrefix);`。`handle.submit` 的便捷形态就是附一个空的 `KvPrefix::none()`(见 [handle.rs:L191-L200](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L191-L200)),前缀解析的细节在 u5-l1。

#### 4.1.4 代码实践:给构造点做一次「字段溯源」

1. **实践目标**:不看任何总结,仅凭 [bridge.rs:L337-L352](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge.rs#L337-L352) 的构造字面量,为十二个字段各写一句「值从哪来」。
2. **操作步骤**:
   1. 打开上面链接,把 L337-L352 的结构体字面量抄成两列:字段名 → 冒号后的表达式;
   2. 对每个表达式追问一次来源:是 HTTP 请求体直接给的(如 `max_tokens`)、采样参数转换来的(`params`)、分词得到的(`prompt_tokens`)、还是桥自己合成的(`token_tx`、`trace_parent`);
   3. 再打开 [handle.rs:L336-L355](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L336-L355) 的 `routed_request` 测试帮手,对比两处构造的字段取值差异。
3. **需要观察的现象**:测试帮手里 `data_parallel_rank` 是参数 `rank: Option<usize>`(可 `None` 可 `Some`),而桥里恒为 `Some(engine_index)`;测试帮手里 `trace_parent: None`、`request_id: None`,桥里两者都可能为 `Some`。
4. **预期结果**:得到一张十二行的溯源表。关键结论应有两条:(a) 唯一「既非请求体也非环境」的字段是 `token_tx`——它是桥当场铸造的;(b) 从 vLLM 路径进来的请求永远带绑定 rank,`Option` 的弹性是给直连驱动留的。全部为源码阅读,无需运行,无「待本地验证」项。

#### 4.1.5 小练习与答案

**练习 1**:`data_parallel_rank` 为什么是 `Option<usize>` 而不是 `usize`?

答案:`Some(r)` 表示前端已决定放置(路由指令),`None` 表示把放置权交回 handle——由它按 \( \text{running} + 4 \times \text{waiting} \) 最低分选分区(request.rs L15-L18 文档)。两种语义都真实存在:桥恒绑 rank,直连驱动常留 `None`。

**练习 2**:`trace_parent` 为什么装 `SpanContext` 而不是持有 `Span`?

答案:文档(request.rs L7-L13)给了两个理由——`SpanContext` 是 `Copy`,能跟着调度器里 `Clone` 出来的请求状态走,而 `Span` 不是 `Clone`;其次 `None`(tracing 关闭)让调度器完全跳过 span 工作,活 `Span` 则必须被持有和正确 drop。

**练习 3**:`kv_transfer_params` 为什么是 `serde_json::Value` 这种「无类型」的形态?

答案:它是 P/D 分离部署的路由器元数据,对引擎与前端都是不透明的——前端从请求的 `vllm_xargs.kv_transfer_params` 取出(request.rs L23-L25),引擎在合适时机经 `TokenEvent::KvTransfer` 原样送回(event.rs L36-L38)。双方都不解释内容,所以类型上就声明「我不理解它」。

### 4.2 TokenEvent 与 RequestTag:回来的是一族事件

#### 4.2.1 概念说明

请求进引擎,回来的不是「生成结果」一个值,而是一条**事件流**:什么时候被排上、每个 token 何时产出、最终为什么结束。`TokenEvent` 就是这条流上的消息族,共七个变体;`RequestTag` 是把每条消息路由回所属请求的标签。

之所以要七个变体而不是「token 数组 + 结束原因」三件套,是因为流式服务的需要:调度器每产出一个 token 就能立刻推给客户端,而排队时刻、前缀缓存命中数、P/D 交接这类「过程事实」也都要有地方搭载。这是一个**协议约定**——类型只定义了词汇,「先发什么后发什么」在 legacy 契约里靠约定保证(新一代契约把顺序做进了类型,见 u3-l3)。

#### 4.2.2 核心流程

一个被接受请求的典型事件序列:

```
submit
  │
  ▼
Scheduled ──────────── 准入回执:排队/调度时间戳、prompt 长度、前缀缓存命中数
  │
  ├─(可选)PromptTokens ── 仅当请求了 prompt_logprobs,prompt 逐 token 概率回传
  │
  ├─ Token × N ────────── 逐 token 输出,每个携带可选 logprob
  │
  ├─(可选)KvTransfer ──── P/D 交接元数据,可与 token 流混排、突发到达
  │
  ▼
terminal(恰一次):Finished | Error | Rejected
```

两条硬约定(本讲先作为事实陈述,u3-l3 会对照新契约复盘):

- **Scheduled 先行**:请求被接纳后的第一个事件是 `Scheduled`;连「无法路由」这种失败也先发 `Scheduled` 再发 `Rejected`(见 4.2.3 末条)。
- **terminal 恰一次**:`Finished`/`Error`/`Rejected` 三选一,恰好一次;此后该请求不再有任何事件。

#### 4.2.3 源码精读

事件族本体:

[pegainfer-frontend/src/engine/event.rs:L18-L54](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/event.rs#L18-L54)
——七个变体的完整定义。逐个看载荷:`Scheduled`(L20-L27)带排队/调度两个时间戳、prompt 长度、`cached_tokens`——文档注明「无前缀缓存或发事件时未知则为 0」;`Token`(L28-L31)是一个 token id 加可选 logprob;`PromptTokens`(L32-L35)整段 prompt 的 id 与逐位概率;`KvTransfer`(L36-L38)不透明 JSON;`Finished`(L39-L43)带完结原因与两个 token 计数;`Error`(L44-L48)与 `Rejected`(L49-L53)形态相同,都是 message + 两个计数,语义分工见下方练习 1。

两个配套词汇类型:

[pegainfer-frontend/src/engine/event.rs:L11-L16](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/event.rs#L11-L16)
——`FinishReason { Length, Stop, Error }`:「完结方式」的分类,是 `Finished` 事件的载荷词汇(注意它是 `Copy + PartialEq`,事件断言测试靠这一点)。它会作为词汇类型被新契约复用——u3-l1 已指出 `step.rs`/`ledger.rs` 都 import 它。

[pegainfer-frontend/src/engine/event.rs:L3-L9](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/event.rs#L3-L9)
——`TokenLogprob`:logprob 值、`rank`(按 vLLM 约定,「词表里 logit 不低于本 token 的个数,含并列」)、top 候选列表。`logprobs: Option<usize>` 请求字段决定 `Token.logprob` 是否被填。

打标与时间基:

[pegainfer-frontend/src/engine/event.rs:L56-L59](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/event.rs#L56-L59)
——`pub type RequestTag = Arc<str>;`——把事件路由回请求的标签就是外部请求 id,`Arc<str>` 让「每个事件打一次标」只是一次引用计数加一,而不是字符串拷贝。

[pegainfer-frontend/src/engine/event.rs:L61-L68](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/event.rs#L61-L68)
——`unix_now_s()`:所有事件时间戳的公共时钟基(UNIX 秒,`f64`)。

最后看「Scheduled → Rejected」这对儿的一个非显然生产者——handle 自己。当请求绑定的 rank 超出引擎分区拓扑时,`submit` 并不返回错误,而是替引擎回答:

[pegainfer-frontend/src/engine/handle.rs:L271-L287](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L271-L287)
——`reject_unroutable`:先 `send(Scheduled {...})` 再 `send(Rejected { message: "data_parallel_rank {partition} is outside 0..{partitions}", ... })`。函数文档点明动机:「与模型调度器准入拒绝相同的表面」——调用方不需要为「提交失败」另写一条错误处理路径。

对应的测试把这对事件钉成了断言:

[pegainfer-frontend/src/engine/handle.rs:L404-L420](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L404-L420)
——`multi_partition_out_of_range_rank_is_rejected_not_dropped`:给单分区引擎提交 `data_parallel_rank: Some(7)`,断言 `submit` 返回 `Ok`,且事件流前两条恰是 `Scheduled` 与含 "outside 0..1" 的 `Rejected`。

#### 4.2.4 代码实践:运行并读懂「不可路由请求」的应答

1. **实践目标**:亲眼看到 `Scheduled → Rejected` 事件对从 `handle.submit` 里发出来,并理解「应答而非报错」的取舍。
2. **操作步骤**:
   1. 通读 [handle.rs:L404-L420](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L404-L420) 的测试与它用的 `routed_request` 帮手([handle.rs:L336-L355](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L336-L355));
   2. 在仓库环境运行单个测试:`cargo test --release -p pegainfer-frontend --lib multi_partition_out_of_range -- --nocapture`;
   3. 回答:如果把 `reject_unroutable` 改成让 `submit` 返回 `Err`,调用方(桥)在 [bridge.rs:L337-L352](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge.rs#L337-L352) 的 `?` 处会发生什么?客户端会收到什么?
3. **需要观察的现象**:测试通过;`--nocapture` 下无额外输出(该测试没有 println,这是正常的——断言全在 `assert!(matches!(...))` 里)。
4. **预期结果**:测试通过(属 `--lib` 单元测试,无需 GPU 权重,但编译 frontend 需要本仓库的构建环境;实际运行结果**待本地验证**)。第 3 步的推演答案:`?` 会把 `SendError` 变成桥的错误向上传播,整条 HTTP 请求以内部错误收场——而事件对应答让这个调用方错误以一条正常的请求失败(Rejected)返回,不污染桥的错误处理。

#### 4.2.5 小练习与答案

**练习 1**:`Error` 与 `Rejected` 都是「带 message 的终态」,分工是什么?

答案:`Rejected` 是**准入拒绝**——请求还没开始生成就被拒(如 rank 越界、批量已满);`Error` 是**生成途中的引擎侧失败**。两者都带 `prompt_tokens`/`completion_tokens` 计数,方便计费与统计口径一致。类型上二者载荷相同(event.rs L44-L48 与 L49-L53),区别只在语义与发出时机。

**练习 2**:为什么 handle 对不可路由请求要先发 `Scheduled` 再发 `Rejected`,而不是只发 `Rejected`?

答案:为了让「提交后被接纳」的调用方心智模型统一:提交成功后事件流总是以 `Scheduled` 开头、以某个 terminal 结束。`reject_unroutable` 的文档(handle.rs L272-L274)写明这是「与模型调度器的准入拒绝相同的表面」——调用方一处代码处理所有拒绝。

**练习 3**:`FinishReason::Error` 和 `TokenEvent::Error` 是什么关系?

答案:两层不同的东西。`FinishReason` 是 `Finished` 事件的**载荷词汇**(Length/Stop/Error 三分类);`TokenEvent::Error` 是一个**独立的事件变体**,自己带 message。前者修饰「以 Finished 收尾时的原因」,后者是另一种终态事件本身。词汇类型与事件变体分层,正是 u3-l1 说的「词汇可跨代复用、协议本体才随迁移删除」的一个例子。

### 4.3 TokenSink:共享打标通道与 abort reason

#### 4.3.1 概念说明

`TokenSink` 是调度器手里的事件出口——`GenerateRequest.token_tx` 的类型。它的两段设计是本讲最值得学的工程决策:

1. **共享打标通道**:整个引擎只有一个 `(RequestTag, TokenEvent)` 通道,所有请求的事件都推进去,由唯一一个前端 demux 循环按 tag 分发。它取代了更直观的「每请求一条通道 + 每请求一个消费任务」。
2. **abort reason 原子状态**:取消一个请求,从「drop 它那条通道的接收端」(旧设计)改成「置位一个每请求的 `AtomicU8`」——因为在共享通道下,接收端只有一份,drop 它等于掐断所有请求。

为什么共享是对的?旧设计里 N 个请求 = N 个睡着的消费任务,调度器每步产出事件时要唤醒 N 个;共享后一个消费者一步只醒约一次。写成公式:

\[ \text{每步唤醒数}:\quad \underbrace{N}_{\text{每请求通道}} \;\longrightarrow\; \underbrace{1}_{\text{共享通道}} \]

#### 4.3.2 核心流程

`send` 的判定流程(与源码逐行对应):

```
sink.send(event)
  │
  ├─ abort_reason ≠ None ?          ── 是 ──► Err(事件原样退回)
  │      否                            (调度器读作「消费者没了,retire 此请求」)
  ├─ 共享通道接收端已消失 ?
  │      否
  └─ tx.send((tag.clone(), event))  ── 接收端没了 ──► Err
                                      ── 成功 ──► Ok
```

abort 状态机(三个值,一次单向迁移):

```
None(0) ──前端置位──► Cancelled(1)     流已开始后,前端显式取消(如客户端中断 SSE)
None(0) ──前端置位──► Disconnected(2)  首个响应块到达客户端之前就发现断连
```

置位用 `Release`、读取用 `Acquire`:保证「前端丢弃该请求流状态」的写,在调度器读到 abort 之前完成排序。`send`/`is_closed` 一旦看到 abort,就把该请求当作消费者已消失——**反应式 retire**,和旧「drop 接收端」的合同相同,只是信号从通道关闭换成了 reason。

#### 4.3.3 源码精读

先看通道类型与动机原文:

[pegainfer-frontend/src/engine/sink.rs:L10-L16](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/sink.rs#L10-L16)
——`TokenStreamSender/Receiver = mpsc::UnboundedSender/Receiver<(RequestTag, TokenEvent)>`。文档把设计动机写得明白:「N 个独立睡眠的消费者每步耗 N 次唤醒,一个共享消费者约 1 次」。

[pegainfer-frontend/src/engine/sink.rs:L18-L40](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/sink.rs#L18-L40)
——`TokenSink` 的文档 + 结构体。三个要点:(1) 它是旧 `UnboundedSender<TokenEvent>` 的**直接替换**——保留同样的 `send`/`is_closed`/`Clone` 表面,「scheduler call sites are unchanged」;(2) 取消从「drop 每请求接收端」改为共享 abort reason,前端取消单个请求不再影响别的请求共用的通道;(3) `tx.is_closed()` 是**引擎级**信号(demux 整个没了),abort reason 才是**每请求**信号。结构体只有三个私有字段:`tag`、`tx`、`abort_reason: Arc<AtomicU8>`。

核心方法:

[pegainfer-frontend/src/engine/sink.rs:L51-L64](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/sink.rs#L51-L64)
——`send`:先查 abort,再进通道;两条失败路径都把事件原样还给调用者(`SendError(event)`)。文档点明调度器把两种失败都读作「consumer dropped, retire the request」。

[pegainfer-frontend/src/engine/sink.rs:L66-L91](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/sink.rs#L66-L91)
——三兄弟谓词:`is_closed`(被 abort **或** 通道没了)、`is_cancelled`(仅显式取消)、`is_disconnected`(仅首块前断连);以及 `abort_reason()`(L83-L86,`Acquire` 读取)和 `tag()`(L88-L91)。

[pegainfer-frontend/src/engine/sink.rs:L93-L105](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/sink.rs#L93-L105)
——`standalone()`:给「直连驱动」(基准、集成测试、模拟器)开的口子——自带一条**私有**通道,tag 固定为 `"local"`,「cancel 标志永远不会被置位」。这是本讲实践的入场券。

abort 的三个取值与内存序:

[pegainfer-frontend/src/engine/sink.rs:L108-L128](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/sink.rs#L108-L128)
——`RequestAbortReason { None=0, Cancelled=1, Disconnected=2 }`;`from_raw` 对未知值一律回 `None`(向前兼容);`store` 是 `pub(crate)` 且用 `Release`——**置位能力刻意只留给 frontend 内部**(谁调用它?见下面 bridge 两段)。

消费侧测试三个,恰好把三态区别钉死:

[pegainfer-frontend/src/engine/sink.rs:L134-L161](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/sink.rs#L134-L161)
——置位 `Cancelled` 后:`is_cancelled()` 真、`send` 失败——且此时**通道接收端还活着**(测试里 `rx` 未 drop),证明失败来自 reason 而非通道。

[pegainfer-frontend/src/engine/sink.rs:L163-L179](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/sink.rs#L163-L179)
——反向对照:drop `standalone` 的接收端,`is_closed()` 真、`send` 失败,但 `is_cancelled()` **假**——「通道关闭 ≠ 显式取消」。

[pegainfer-frontend/src/engine/sink.rs:L181-L199](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/sink.rs#L181-L199)
——`Disconnected` 与 `Cancelled` 可区分:两者都使 `is_closed()` 真,但谓词各答各的。

最后看前端在生产环境怎么置位 abort——谁在 `store`:

[pegainfer-frontend/src/vllm/bridge.rs:L362-L405](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge.rs#L362-L405)
——`RequestStreamState`:demux 循环按 `RequestTag` 持有的每请求状态,其中 `abort_reason: Arc<AtomicU8>` 与提交时铸进 `TokenSink` 的那个是**同一份**(`Arc` 克隆);`abort(&self, reason)` 就是 `reason.store(&self.abort_reason)`——`pub(crate)` 的唯一消费入口。

[pegainfer-frontend/src/vllm/bridge.rs:L211-L223](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge.rs#L211-L223)
——选哪个 reason?以 `has_emitted_tokens` 为界:客户端断连发生在**首个 token 到达之前**算 `Disconnected`,之后算 `Cancelled`——注释写明 `Scheduled` 元数据随首个引擎输出一起冲刷,不足以证明「客户端已见到 token」。另见 [bridge.rs:L168-L172](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge.rs#L168-L172):桥退出时对全部在飞请求统一置 `Cancelled`,让调度器在下次 emit 时自行 retire,「而不是往一条没人排水的通道里推」。

#### 4.3.4 代码实践:亲手走一遍 Scheduled → Token → Finished

这是本讲的主实践:仿照 `handle.rs` 测试里的 `routed_request` 帮手,在草稿 crate 里驱动真实的 `TokenSink` 与 `SamplingParams`。

1. **实践目标**:用 `TokenSink::standalone` 构造请求通道,按 `Scheduled → Token → Finished` 顺序手动 send 三个事件并断言接收顺序;再验证 `SamplingParams::is_greedy` 在 `temperature=0` 与 `top_k=1` 两种参数下都返回 `true`。
2. **操作步骤**:
   1. 在仓库外(或仓库内任意目录,Cargo.toml 里加一行空 `[workspace]` 脱离根 workspace)建草稿 crate:

      ```toml
      # legacy-wire-lab/Cargo.toml —— 示例代码
      [package]
      name = "legacy-wire-lab"
      version = "0.1.0"
      edition = "2024"

      [workspace] # 放在仓库目录内时需要;放在仓库外可删

      [dependencies]
      pegainfer-frontend = { path = "指向本仓库的 pegainfer-frontend" }
      ```

   2. 写测试(示例代码,依赖扁平 re-export 路径 `pegainfer_frontend::engine::*` 与公开模块 `pegainfer_frontend::sampler`):

      ```rust
      // legacy-wire-lab/src/lib.rs —— 示例代码
      use pegainfer_frontend::engine::{FinishReason, TokenEvent, TokenSink};
      use pegainfer_frontend::sampler::SamplingParams;

      #[test]
      fn standalone_sink_preserves_event_order() {
          let (sink, mut rx) = TokenSink::standalone();

          sink.send(TokenEvent::Scheduled {
              queued_at_unix_s: 100.0,
              scheduled_at_unix_s: 100.5,
              prompt_tokens: 2,
              cached_tokens: 0,
          })
          .expect("uncancelled sink should send");
          sink.send(TokenEvent::Token { id: 42, logprob: None })
              .expect("uncancelled sink should send");
          sink.send(TokenEvent::Finished {
              finish_reason: FinishReason::Length,
              prompt_tokens: 2,
              completion_tokens: 1,
          })
          .expect("uncancelled sink should send");

          // 顺序断言:token 不带 PartialEq,用 matches! 匹配形态
          let (tag, e) = rx.try_recv().expect("event 1");
          assert_eq!(tag.as_ref(), "local"); // standalone 的固定 tag
          assert!(matches!(e, TokenEvent::Scheduled { .. }));
          let (_, e) = rx.try_recv().expect("event 2");
          assert!(matches!(e, TokenEvent::Token { id: 42, .. }));
          let (_, e) = rx.try_recv().expect("event 3");
          assert!(matches!(
              e,
              TokenEvent::Finished { finish_reason: FinishReason::Length, .. }
          ));
          assert!(rx.try_recv().is_err()); // 排空
      }

      #[test]
      fn greedy_recognizes_both_argmax_spellings() {
          let temp_zero = SamplingParams { temperature: 0.0, ..Default::default() };
          let top_k_one = SamplingParams { temperature: 1.0, top_k: 1, ..Default::default() };
          let sampled   = SamplingParams { temperature: 0.7, top_k: 50, ..Default::default() };

          assert!(temp_zero.is_greedy()); // temperature < 1e-5
          assert!(top_k_one.is_greedy()); // top_k == 1:掩码后只剩一个 token
          assert!(!sampled.is_greedy());  // 其余都要走真实采样
      }
      ```

   3. 运行:`cargo test --release -p legacy-wire-lab`。
3. **需要观察的现象**:两个测试全绿;`standalone` 的 tag 是 `"local"`;第三个事件后 `try_recv` 返回 `Err`(通道已空但未关闭,错误是 `Empty` 而非 `Disconnected`)。
4. **预期结果**:如上。若想顺手跑仓库自带的三个 sink 测试对照:`cargo test --release -p pegainfer-frontend --lib token_sink`。注意草稿 crate 需要能编译 `pegainfer-frontend`(本仓库构建环境,含 CUDA 工具链);本讲写作时未在此环境实际执行,运行输出**待本地验证**。另注:`RequestAbortReason::store` 是 `pub(crate)`,草稿 crate 无法直接调用它置位——想模拟 abort 见第 5 节综合实践的做法。

#### 4.3.5 小练习与答案

**练习 1**:共享通道设计下,为什么不能继续用「drop 每请求接收端」来实现单个请求的取消?

答案:因为接收端只有一份——它是全引擎共享的 demux 循环持有的。drop 它会掐断**所有**请求的事件回流。所以取消语义改由每请求的 `AtomicU8` abort reason 承担(sink.rs L25-L29 文档:前端「设置该请求的 reason,而不关闭其他请求还在用的通道」)。

**练习 2**:`is_closed()` 为真有两种互斥的原因,分别对应什么?另两个谓词各答哪一种?

答案:该请求被 abort(reason ≠ None),或共享通道的接收端没了(`tx.is_closed()`,引擎级信号)。`is_cancelled()` 只答 `Cancelled`;`is_disconnected()` 只答 `Disconnected`(sink.rs L66-L81)。

**练习 3**:`store` 用 `Release`、读用 `Acquire`,这个内存序配对在这里保证什么?

答案:保证 abort 的写与「前端丢弃该请求流状态」的写之间有 happens-before 排序(sink.rs L31-L34 文档)——调度器一旦 `Acquire` 读到 abort,前端侧先前的状态清理对它已可见,不会观察到半拆除的流状态。

**练习 4**:为什么 `RequestTag` 用 `Arc<str>` 而不是 `String`?

答案:事件流每条消息都要带 tag。`Arc<str>` 克隆只是一次引用计数加一;`String` 则是每次都拷贝整个 id 字符串(event.rs L56-L59 文档原文:「per-event tagging to a refcount bump instead of a string copy」)。

## 5. 综合实践

把三个模块串起来:**模拟一个双请求共享通道的微型 demux**。要证明的命题是——一条共享通道 + tag,足以还原两条独立、顺序各自保真的事件流;且 abort 一个请求不影响它的邻居。

在 4.3.4 的草稿 crate 里加一个测试(示例代码;`TokenSink::new` 是公开的,`AtomicU8` 的写是通用能力,所以外部 crate 也能完整模拟前端行为——`store` 虽是 `pub(crate)`,但直接对共享的 `Arc<AtomicU8>` 写入判别值 `1`(即 `Cancelled`)是等价操作):

```rust
// legacy-wire-lab/src/lib.rs 追加 —— 示例代码
use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::Arc;

use pegainfer_frontend::engine::{TokenEvent, TokenSink, TokenStreamSender};

#[test]
fn shared_channel_demuxes_two_requests_and_abort_is_per_request() {
    let (tx, mut rx): (TokenStreamSender, _) = tokio::sync::mpsc::unbounded_channel();
    let abort_a = Arc::new(AtomicU8::new(0));
    let abort_b = Arc::new(AtomicU8::new(0));
    let sink_a = TokenSink::new(Arc::from("req-a"), tx.clone(), Arc::clone(&abort_a));
    let sink_b = TokenSink::new(Arc::from("req-b"), tx.clone(), Arc::clone(&abort_b));

    // 交错推送:a 准入 → b 准入 → a 一个 token → 取消 b → b 仍试发(应失败)→ a 收尾
    sink_a.send(TokenEvent::Scheduled {
        queued_at_unix_s: 1.0, scheduled_at_unix_s: 1.1,
        prompt_tokens: 1, cached_tokens: 0,
    }).unwrap();
    sink_b.send(TokenEvent::Scheduled {
        queued_at_unix_s: 1.0, scheduled_at_unix_s: 1.2,
        prompt_tokens: 1, cached_tokens: 0,
    }).unwrap();
    sink_a.send(TokenEvent::Token { id: 7, logprob: None }).unwrap();

    abort_b.store(1, Ordering::Release); // 1 = RequestAbortReason::Cancelled 的判别值
    assert!(sink_b.is_closed() && sink_b.is_cancelled());
    assert!(sink_b.send(TokenEvent::Token { id: 9, logprob: None }).is_err());
    sink_a
        .send(TokenEvent::Finished {
            finish_reason: pegainfer_frontend::engine::FinishReason::Stop,
            prompt_tokens: 1,
            completion_tokens: 1,
        })
        .expect("a 未被 abort,应成功");

    // demux:按 tag 把共享通道分回两条流
    let mut a_events = Vec::new();
    let mut b_events = Vec::new();
    while let Ok((tag, event)) = rx.try_recv() {
        match tag.as_ref() {
            "req-a" => a_events.push(event),
            "req-b" => b_events.push(event),
            _ => unreachable!(),
        }
    }

    assert_eq!(a_events.len(), 3); // Scheduled, Token, Finished —— 顺序即入队顺序
    assert_eq!(b_events.len(), 1); // 只有 Scheduled;被 abort 后的 send 从未入队
    assert!(matches!(b_events[0], TokenEvent::Scheduled { .. }));
}
```

两个实现细节值得注意:`TokenEvent` 不实现 `PartialEq`,断言一律用 `matches!` 检查形态;而对 `Arc<AtomicU8>` 直接写判别值 `1` 之所以等价于 `RequestAbortReason::Cancelled.store(...)`,是因为枚举标了 `#[repr(u8)]` 且 `from_raw(1)` 映射到 `Cancelled`(sink.rs L108-L122)。完成标准:测试通过,并且你能口头回答——(1) `b_events` 为什么恰好一条?(abort 后的 send 在**入队之前**就被拦下,sink.rs L57-L59 的第一道判断);(2) `sink_a` 为什么完全不受 `abort_b` 影响?(reason 是每请求的独立 `Arc`,共享的只有通道本身)。运行结果**待本地验证**(编译 frontend 需仓库构建环境;另需给草稿 crate 加 `tokio = { version = "1", features = ["sync"] }` 依赖)。

## 6. 本讲小结

- `GenerateRequest`(request.rs)是纯数据的请求本体:十二个字段里 `data_parallel_rank` 是路由指令(`None` = 交给 handle 选最低负载)、`token_tx` 是唯一回信出口、`trace_parent` 携带 `Copy` 的链路上下文让调度器把耗时分解挂进前端的 trace。
- `TokenEvent`(event.rs)是七变体事件族:`Scheduled` 准入回执、`Token`/`PromptTokens` 输出、`KvTransfer` P/D 透传、`Finished`/`Error`/`Rejected` 三种终态;`FinishReason { Length, Stop, Error }` 是被跨代复用的完结词汇;「Scheduled 先行、terminal 恰一次」在 legacy 契约里是约定而非类型保证。
- `RequestTag = Arc<str>`:每事件打标只是一次引用计数加一;tag 即外部请求 id,是 demux 的路由键。
- `TokenSink`(sink.rs)把「N 条每请求通道、N 个睡眠消费者」合并成「一条共享打标通道、一个 demux 循环」,每步唤醒从 \( N \) 降到约 1。
- 取消从「drop 接收端」改为每请求的 `AtomicU8` abort reason(`None`/`Cancelled`/`Disconnected`,`Release` 写 `Acquire` 读);`tx.is_closed()` 是引擎级信号,abort 才是每请求信号;`standalone()` 给直连驱动留了无 demux 的私通道入口。
- 桥(bridge.rs)是生产侧的完整样板:铸造 tag + abort + sink、总是绑定 rank 提交、以 `has_emitted_tokens` 为界选择 `Disconnected`/`Cancelled`,退出时对全部在飞请求置 `Cancelled`。

## 7. 下一步学习建议

下一讲 **u3-l3(step 契约线类型:StepOutputs 与扁平 RequestUpdate)** 是本讲的最佳对照:同样的「请求 + 事件」问题,新契约用一条扁平 `RequestUpdate` 把「一步内发生的一切」压进类型,让「terminal 之后再出 token」不可表达——建议把本讲 4.2.2 的事件序列图带着过去逐条对照。之后再往 **u3-l4(RequestLedger)** 看 terminal 恰一次如何从「约定」升级为「碰已关账户即 panic」;**u3-l6(接线与路由)** 会补全本讲只顺带一提的 `EngineHandle` 最低负载路由;若想立刻看 legacy 契约在协议栈端的另一半,**u4-l3(两代引擎桥)** 精读 demux 循环如何把共享打标通道折叠成 vLLM 的输出突发。想动手的读者可回头跑一遍 4.3.4 与第 5 节的测试,再阅读 [sink.rs:L130-L200](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/sink.rs#L130-L200) 的三个官方测试与你的版本互证。
