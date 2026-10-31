# step 契约线类型：StepOutputs 与扁平 RequestUpdate

## 1. 本讲目标

本讲精读新一代 step 驱动引擎契约的**线类型**（wire types）——即数据在「模型调度器」与「协议栈」两个模块边界上的实际形状。读完本讲你应当能够：

1. 解释 `RequestId` 为何是一个 `Copy` 的整数 newtype：它是进程内路由 id，外部协议的字符串请求 id 留在协议栈，不越过接线。
2. 说出 `Request` 上 `history_tokens` 与 `history_marks`（新者在前）在多轮会话中的含义：它们是前缀快照的恢复点坐标。
3. 论证 `RequestUpdate` 的扁平结构如何把旧 `TokenEvent` 协议里「`Scheduled` 先于 token、terminal 恰一次」的**时序约定**变成**类型本身**——一步之内「terminal 之后再出 token」在这个类型上根本不可表达。

本讲承接 [u3-l1](u3-l1-contract-overview.md) 的两代契约地图与 [u3-l2](u3-l2-legacy-wire.md) 的 legacy 线类型，是理解 u3-l4（账本）与 u4-l3（stepped 桥）的前置。

## 2. 前置知识

### 2.1 什么是「线类型」

「线」（wire）指两个模块之间的数据通道。**线类型**就是允许出现在通道上的数据形状。在 PegaInfer 的引擎契约里，线有两段：

- **下行（请求方向）**：协议栈 → 调度器，走 `Request` / `QueuedRequest`。
- **上行（事件方向）**：调度器 → 协议栈，走 `StepOutputs` / `RequestUpdate`。

设计一条线，本质上是在回答三个问题：**什么事实允许被传递**（字段集合）、**事实之间什么顺序是合法的**（时序）、**谁来保证这个顺序**（生产者、消费者，还是类型系统）。本讲的核心论点正是：新契约把第二个问题的答案从「文档约定」升级成了「类型结构」。

### 2.2 事件流 vs 扁平记录

legacy 契约（u3-l2）的上行线是一条**事件序列**：每生成一个 token 就发一条 `TokenEvent`，事实的顺序体现在消息的先后排列上。「`Scheduled` 必须先于 token」「terminal（`Finished`/`Error`/`Rejected`）至多出现一次且之后不再有任何事件」——这些是**所有生产者必须遵守的纪律**，类型本身并不阻止你先发一个 `Token` 再发 `Scheduled`。

新契约的上行线是一条**扁平记录**：一个调度器步（step）产出一个 `StepOutputs` 消息，其中每个被触碰的请求**至多一条** `RequestUpdate`。一步内发生的所有事实不再排队，而是各占一个字段。顺序由字段在结构体里的固定排列隐含——这就是「把时序变成结构」。

一个直观对比：

| legacy 事件序列（一步内） | step 记录（一个 `RequestUpdate`） |
|---|---|
| `Scheduled` | `scheduled: Option<ScheduledInfo>` |
| `Token(10)`、`Token(11)` | `tokens: vec![10, 11]` + `logprobs` 平行数组 |
| `KvTransfer{...}` | `kv_transfer: Option<Value>` |
| `Finished{...}` | `terminal: Option<Terminal>` |

### 2.3 需要的 Rust 背景

- **newtype**：`struct RequestId(u64)` 这类单字段包装，用来让一个裸整数获得独立类型身份。
- **`Copy`**：可按位复制的类型，传值不发生所有权转移；整数 newtype 天然适合。
- **`Arc<[u32]>`**：原子引用计数的不可变切片，多个克隆共享同一份堆内存。
- **`Option<T>` 即「至多一次」**：字段用 `Option` 表达「这条事实这一步可能没有」；Vec 表达「零或多次」。
- **`#[non_exhaustive]`**：枚举对 crate 外禁止穷举匹配，给未来新增变体留余地。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用途 |
|---|---|---|
| `pegainfer-frontend/src/engine/step.rs` | **主角**：step 契约全部线类型 | `RequestId`、`Request`、`QueuedRequest`、`StepOutputs`、`RequestUpdate`、`ScheduledInfo`、`RejectReason`、`Terminal` |
| `pegainfer-frontend/src/engine/stop.rs` | **主角**：请求内停止规则 | `EosPolicy`、`StopPolicy`、`StopCause` |
| `pegainfer-frontend/src/engine/ledger.rs` | 辅：把 `RequestUpdate` 铸出来的账本 | 理解扁平记录的生产侧与「terminal 恰一次」的跨步强制 |
| `pegainfer-frontend/src/engine/driver.rs` | 辅：驱动循环 | 线类型的消费节奏（每迭代一次 commit） |
| `pegainfer-frontend/src/engine/wiring.rs` | 辅：接线 | `SchedulerHandle::submit` 铸造 `RequestId` 的现场 |
| `pegainfer-frontend/src/engine/event.rs` | 辅：legacy 词汇 | `FinishReason`、`TokenLogprob` 被新契约复用 |
| `pegainfer-frontend/src/vllm/bridge/stepped.rs` | 辅：真实消费者 | `reduce_update` 按字段顺序折叠记录；`history_tokens` 的注入点 |

## 4. 核心概念与源码讲解

### 4.1 请求侧线类型：RequestId、Request 与 QueuedRequest

#### 4.1.1 概念说明

一个生成请求从 HTTP 进入 PegaInfer 时带着一个**字符串** id（vLLM 协议的 `request_id`，形如 `req_000123`）。但引擎内部并不用字符串路由——字符串比较慢、占堆内存、还可能重复。于是接线处（`SchedulerHandle::submit`）为每个请求铸造一个**进程内路由 id**：`RequestId(u64)`。

这是一个典型的「两个世界、两种身份」设计：

- **外部世界（协议栈）**：字符串 id，面向客户端、日志、HTTP 响应。
- **内部世界（引擎）**：`u64` id，面向 HashMap 键、日志、账本。

两者的映射表由协议栈自己保管（stepped 桥里就叫 `names: HashMap<String, RequestId>`，专门为 abort 反查用）。字符串 id **从不**进入 `Request`——`Request` 上只有一个 `client_label` 字段，文档明确写着「仅供调度器侧日志，绝不用于路由」。

`Request` 本体是纯数据负载：prompt token、采样参数、停止策略、长度上限等。注意它**不含** id、排队时间戳和 abort 标志——文档写明这三样在 submit 时铸造、活在请求的**账本账户**上（u3-l4 详讲）。`QueuedRequest` 则是把 id 与负载配对后的形态，是调度器 `submit` 方法实际收到的东西。

#### 4.1.2 核心流程

请求侧的完整旅程：

```
HTTP body（字符串 request_id）
    │ 协议栈翻译（vllm/wire）
    ▼
SchedulerHandle::submit(Request)
    │  铸造：RequestId ← per-scheduler AtomicU64 计数器 fetch_add
    │       abort 标志 ← Arc<AtomicBool>
    │       queued_at ← Instant::now()
    │  三者装进 RequestEnvelope（请求信封，见 u3-l4/u5-l1）
    ▼
crossbeam 提交通道
    ▼
driver 循环排空提交 → ledger.register(envelope)
    │  开账户（id、prompt 长度、queued_at、abort）
    ▼
QueuedRequest { id, request } → scheduler.submit(queued)
```

铸造点在接线层而非协议栈，意味着**每个调度器分区有自己独立的 id 空间**（从 0 递增）；协议栈同时服务多个分区时，用 `(分区, RequestId)` 二元组才能唯一定位一个请求。

#### 4.1.3 源码精读

**`RequestId`：为路由而生的整数 newtype**

[pegainfer-frontend/src/engine/step.rs:L20-L46](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/step.rs#L20-L46) 定义了 `RequestId`：文档注释直说它是「进程内路由 id，由 `SchedulerHandle::submit` 从 per-scheduler 计数器铸造」，并强调「**故意**做成 `Copy` 且整数键控：外部协议的字符串请求 id 留在协议栈，由协议栈在自己的边界上映射成它」。

派生列表值得逐个看：`Copy`（传值零成本）、`Eq/Hash`（可作 HashMap 键）、`Ord/PartialOrd`（可排序、可作 BTreeSet 键）、`Debug`。构造函数 `new` 是 `pub` 的——文档说这是为了让「调度器单元测试与工具」能伪造请求，生产铸造权仍在 `submit`。`Display` 输出 `req-{n}`，这是它在日志里的样子。

铸造现场在 [pegainfer-frontend/src/engine/wiring.rs:L85-L95](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L85-L95)：`let id = RequestId::new(self.next_id.fetch_add(1, Ordering::Relaxed));`——一句原子递增完成铸造。计数器 `next_id: AtomicU64` 是 `SchedulerHandle` 的字段（[wiring.rs:L68-L77](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L68-L77)），每个 `scheduler_pair()` 各铸一个从 0 开始的新计数器（[wiring.rs:L124-L131](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L124-L131)）。

字符串 id 与 `RequestId` 的映射由桥保管，证据在 [pegainfer-frontend/src/vllm/bridge/stepped.rs:L455-L459](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge/stepped.rs#L455-L459)：submit 之后立刻 `names.insert(request_id.clone(), control.id())`，把线上的字符串 id 映到刚铸造的内部 id。

**`Request`：纯负载，身份外置**

[pegainfer-frontend/src/engine/step.rs:L51-L85](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/step.rs#L51-L85) 是完整的 `Request` 结构。文档第一句就是关键设计陈述：「身份（`RequestId`）、排队时间戳、abort 标志在 submit 时铸造，活在请求的账本账户上，**不在这里**」。

逐字段读一遍（表格）：

| 字段 | 类型 | 职责 |
|---|---|---|
| `prompt_tokens` | `Vec<u32>` | prompt 的 token 序列 |
| `params` | `SamplingParams` | 采样参数（legacy 降级路径仍在用，见 4.3） |
| `history_tokens` | `Option<usize>` | 前缀 prompt 中渲染「会话消息部分」的 token 数（见下文专段） |
| `history_marks` | `Vec<usize>` | 更早各轮会话的结束点，**新者在前** |
| `stop_policy` | `StopPolicy` | 请求内停止规则（4.3 专讲） |
| `max_tokens` | `usize` | 生成上限 |
| `lora_adapter` | `Option<String>` | 请求指定的 LoRA 适配器名 |
| `kv_transfer_params` | `Option<Value>` | 不透明 router/P-D 元数据，原样来自请求的 `vllm_xargs` |
| `logprobs` / `prompt_logprobs` | `Option<usize>` | 补全/prompt 的 top-k 打分请求；`Some(0)` 是合法值（只给选中 token 打分） |
| `trace_parent` | `Option<SpanContext>` | 调用方请求 span 的追踪上下文；调度器的 queue/prefill/decode span 以它为父 |
| `client_label` | `Option<Arc<str>>` | 外部请求名，**仅供调度器日志，绝不用于路由** |

注意 `Option<usize>` 表达的细微语义：`logprobs: None` 是「不请求打分」，`Some(0)` 是「请求打分但只给选中 token」——同一个字段上「不想要」与「想要零个」是两个不同的事实。

**专段：`history_tokens` 与 `history_marks` 的多轮会话含义**

[pegainfer-frontend/src/engine/step.rs:L54-L67](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/step.rs#L54-L67) 是这两个字段的文档，值得逐句翻译：

- `history_tokens`：prompt **前缀**中渲染会话消息的 token 数；chat 模板的 generation prompt（即 `<|im_start|>assistant\n` 这类「请开始回答」的引导串）跟在它们后面。**下一轮对话会精确重复这些 token**，所以在该点做了状态快照的调度器，服务下一轮时可以直接从快照恢复。`None` 表示前端没有测量（一个补全请求，或 chat 层读不出来的请求体）。
- `history_marks`：会话**更早各轮**的结束位置，**新者在前**（newest first）——即产生它们的那些请求各自的 `history_tokens`，由 chat 层从消息列表重新测得。每个点都是「某个更早轮次的快照可能被找到的地方」，服务于保留多份快照的调度器；全部**严格小于** `history_tokens`。未测量时为空。

用图表示三轮对话中第 3 轮请求的字段：

```
prompt = [ 轮1消息 | 轮1回答 | 轮2消息 | 轮2回答 | 轮3消息 | generation prompt ]
           0        m2         m2        m1        m1      ← m1 = history_tokens
                    ↑                    ↑
              history_marks = [m1, m2]（新者在前，且 m2 < m1）
```

调度器在 `m1` 处拍的快照服务第 3 轮；若它还保留着第 2 轮结束时在 `m2` 拍的快照，`history_marks` 告诉它 `m2` 也是一个可用恢复点。

这两个值不是调度器算的，是 chat 层测好后随请求带进来的。注入点在桥侧：[pegainfer-frontend/src/vllm/bridge/stepped.rs:L399-L416](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge/stepped.rs#L399-L416)——从 sampling_params 的 extra args 里读出 generation prompt 的尾长 `tail`，`history_tokens = prompt_len - tail`（并防御 `checked_sub`：一个 prompt 装不下的尾长不构成边界）；`history_marks` 则过滤掉 `m == 0` 或 `m >= history_tokens` 的值。测量算法本身（如何得到 `tail`）在 u4-l4 详讲。

**`QueuedRequest`：id 与负载的配对**

[pegainfer-frontend/src/engine/step.rs:L87-L93](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/step.rs#L87-L93)：调度器收到的形态 = 「其开启中的账本账户的 id」+ 负载。文档强调 id 由 `submit` 铸造、**负载从不自带 id**——身份只有一个来源，不存在两个可能不一致的来源。

#### 4.1.4 代码实践

**实践目标**：亲手铸造几个 `RequestId`，验证「per-scheduler 计数器从 0 递增」与「两个 id 世界的分工」。

**操作步骤**：

1. 在**仓库外**新建一个草稿 crate（放外面是为了不并入 PegaInfer 的 workspace）：

```bash
cargo new --lib /tmp/step-wire-lab
cd /tmp/step-wire-lab
```

2. 在 `Cargo.toml` 加路径依赖（`edition` 用 2024，frontend crate 本身是 2024）：

```toml
[dependencies]
pegainfer-frontend = { path = "<仓库绝对路径>/pegainfer-frontend" }
```

3. 在 `src/lib.rs` 写如下测试（**示例代码**，非项目原有代码；`Request` 构造抄自 [ledger.rs:L403-L418](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L403-L418) 的测试帮手 `request()`）：

```rust
use pegainfer_frontend::engine::{Request, StopPolicy, scheduler_pair};

pub fn request(prompt: Vec<u32>) -> Request {
    Request {
        prompt_tokens: prompt,
        params: Default::default(),
        history_tokens: None,
        history_marks: Vec::new(),
        stop_policy: StopPolicy::default(),
        max_tokens: 8,
        lora_adapter: None,
        kv_transfer_params: None,
        logprobs: None,
        prompt_logprobs: None,
        trace_parent: None,
        client_label: None,
    }
}

#[test]
fn submit_mints_sequential_request_ids() {
    let (handle, _backend) = scheduler_pair();
    let c0 = handle.submit(request(vec![1]));
    let c1 = handle.submit(request(vec![2, 3]));
    assert_eq!(c0.id().raw(), 0);
    assert_eq!(c1.id().raw(), 1);
    assert_eq!(c0.id().to_string(), "req-0"); // Display 格式
}

#[test]
fn each_scheduler_has_its_own_id_space() {
    let (h0, _b0) = scheduler_pair();
    let (h1, _b1) = scheduler_pair();
    assert_eq!(h0.submit(request(vec![1])).id().raw(), 0);
    assert_eq!(h1.submit(request(vec![1])).id().raw(), 0); // 独立计数器
}
```

4. 运行 `cargo test`。

**需要观察的现象**：id 从 0 严格递增；两个独立 `scheduler_pair()` 各自从 0 开始——同 `RequestId::new(0)` 在不同分区是两个不同请求，这就是协议栈必须按分区持有 `names` 映射的原因。

**预期结果**：两个测试通过。`scheduler_pair()` 是纯 CPU 的通道铸造（crossbeam + tokio mpsc + 共享 metrics 单元），不触达 CUDA；`pegainfer-frontend` 的依赖表里没有任何 CUDA crate，本实践无需 GPU。若你的工具链版本过旧导致 edition 2024 编译失败，升级 rustc 即可（**待本地验证**具体最低版本）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Request` 上不直接放一个 `id: RequestId` 字段，而要拆出 `QueuedRequest`？

**答案**：身份只在 submit 时铸造一次，之后由账本账户承载（身份、queued_at、abort 都在账户上）。如果 `Request` 自带 id，就出现两个可能不一致的身份来源：调用方可以伪造、可以填错。`QueuedRequest` 的文档明确「id 由 `SchedulerHandle::submit` 铸造；负载从不自带 id」——单一事实来源（single source of truth）。

**练习 2**：`client_label` 与 `RequestId` 都是「请求的名字」，它们会被混用吗？

**答案**：不会。`client_label` 的文档写明「仅供调度器侧日志，**绝不用于路由**（路由是 `RequestId` 的事）」。外部字符串名只服务于日志与协议栈映射（`names` 表），引擎内部所有寻址（账本键、桥的 `streams` 键）都用 `RequestId`。

**练习 3**：三轮对话的第 3 轮，chat 层测得第 3 轮 `history_tokens = m1`，第 2 轮的 `history_tokens = m2`，第 1 轮的为 `m3`。写出第 3 轮 `Request.history_marks` 的内容。

**答案**：`vec![m2, m3]`——新者在前；且由测量保证 `m3 < m2 < m1`（每轮的 history 严格包含更早的所有轮次，`m1` 是当前轮的 `history_tokens`，不出现在 marks 里）。

---

### 4.2 StepOutputs 与扁平 RequestUpdate：一步一条记录

#### 4.2.1 概念说明

上行线的主类型是 `StepOutputs`：**一个调度器步产出的一切，装在一个消息里**。它只有一个字段 `updates: Vec<RequestUpdate>`——每个本步被触碰的请求至多一条记录。

`RequestUpdate` 是本讲的核心：**一个请求在一步之内发生的一切，摊平成一个结构体**。它不是一个事件，而是一份「这一步的该请求小结」。消费方（协议栈）按**字段顺序**折叠：先 admission 事实（`scheduled`、`cached_tokens`、`kv_transfer`），再新 token（`tokens`/`logprobs`/`prompt_echo`），最后终态（`terminal`）。

为什么扁平化是改进？回到 2.2 的三问：

- **什么事实**：legacy 用七个 `TokenEvent` 变体表达，step 契约用字段表达——集合等价，但字段自带文档与类型。
- **什么顺序合法**：legacy 靠纪律（「`Scheduled` 先行、terminal 恰一次」写在文档里，靠 review 与测试守护）；step 契约里，一步内的合法顺序**就是字段的物理排列**。
- **谁来保证**：legacy 全靠生产者自律；step 契约把一步内的保证交给类型（下文论证），跨步的保证交给账本（u3-l4：碰已关账户即 panic）。

#### 4.2.2 核心流程

**生产侧**（调度器 → 账本 → 线）：

```
scheduler.step(&mut ledger) 期间，对每个触碰的 id 调账本方法：
    admit(id)          → statement[id].scheduled = Some(...)
    push_tokens(id,…)  → statement[id].tokens.extend(…)，logprobs 对齐
    set_cached_tokens  → statement[id].cached_tokens = Some(n)
    echo_prompt        → statement[id].prompt_echo = Some(...)
    kv_transfer        → statement[id].kv_transfer = Some(...)
    finish/fail/reject → statement[id].terminal = Some(...)
    retire             → 把该 id 本步缓冲的记录整条抽走（abort 后无人可寻址）
driver 循环每次迭代末尾：
    ledger.commit_step(metrics_changed)
        → statement.take_updates()：滤掉空记录（is_vacant）
        → tx.send(StepOutputs { updates })
```

**消费侧**（协议栈折叠，以 stepped 桥为例）：

```
for update in step.updates:
    1. scheduled   → 记录 prompt_tokens、生成 Queued/Scheduled 线上事件
    2. cached_tokens / kv_transfer → 存进该请求的流状态
    3. prompt_echo → 翻译成 prompt logprobs 载荷
    4. tokens + logprobs → 翻译成线上 new_token_ids / positions
    5. terminal    → 翻译 finish_reason / stop_reason，终结流状态
```

一步内「至多一条记录」由谁保证？由账本的 `StepStatement` 保证：它内部是 `Vec<Option<RequestUpdate>>` + `HashMap<RequestId, usize>` 索引，`entry(id)` 对同一 id **合并**到同一条记录（[ledger.rs:L69-L102](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L69-L102)）——调度器在一个步里对同一请求先 `admit` 再 `push_tokens` 再 `finish`，三个事实落进**同一条** `RequestUpdate`。

#### 4.2.3 源码精读

**模块文档：设计宣言**

[pegainfer-frontend/src/engine/step.rs:L1-L10](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/step.rs#L1-L10) 的模块文档是理解整个设计的钥匙，直译关键句：「一个调度器步产生一个 `StepOutputs` 消息。每个请求该步至多携带一条 `RequestUpdate`——**一条扁平记录而非事件序列**，因此旧 `TokenEvent` 协议的步内顺序规则（`Scheduled` 先于 token、token 先于唯一 terminal）是**结构而非约定**：一条 `RequestUpdate` 无法表达 terminal 之后的 token。跨步顺序由 `RequestLedger` 在生产侧强制：请求的账户在其 terminal 处关闭，对已关账户的写入会 panic。」

注意「步内保证交给类型、跨步保证交给账本」的明确分工——这正是 u3-l1 地图里 step 与 ledger 两个模块的职责边界。

**`StepOutputs`：一个消息**

[pegainfer-frontend/src/engine/step.rs:L95-L101](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/step.rs#L95-L101) 只有一个字段。文档说明两个特殊情形：**空的 `updates` 也有用途**——通知前端「metrics 变了」（典型场景：取消请求后负载变化）；既无请求更新也无 metrics 变化的步**什么都不发**。发送与否由 [ledger.rs:L343-L351](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L343-L351) 的 `commit_step` 裁决：`if updates.is_empty() && !metrics_changed { return; }`。

**`RequestUpdate`：字段的区段语义**

[pegainfer-frontend/src/engine/step.rs:L103-L129](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/step.rs#L103-L129) 定义本体，文档要求消费者「按字段顺序折叠各区段：admission 事实，然后新 token，然后 terminal」。逐字段读：

| 字段 | 出现次数语义 | 文档要点 |
|---|---|---|
| `id` | 每条必有 | 寻址键 |
| `scheduled` | **每请求生命周期恰一次**，在被接纳的那个步 | admission 事实 |
| `tokens` | 本步提交的 token；纯 prefill 步为空 | 多 token 跨度（投机/MTP 提交）一起到达 |
| `logprobs` | 与 `tokens` 平行 | 未请求打分时全为 `None` |
| `cached_tokens` | 至多一次 | **在调度器得知的那个步报告**（第一个 prefill 分块），不在 admission 时——admission 时刻的 `Scheduled` 还不可能知道它 |
| `prompt_echo` | 至多一次，prefill 完成时 | 回显 prompt |
| `kv_transfer` | 至多 | 不透明 P/D 交接元数据 |
| `terminal` | 至多一次 | **生命周期的终点，之后的步不再提及此 id** |

两个内部帮手（[step.rs:L131-L156](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/step.rs#L131-L156)）是 `pub(crate)` 的：`empty(id)` 造一条空记录，`is_vacant()` 判断「是否没携带任何可观察事实」——账本用它把纯簿记的空记录**丢弃而非上线**。这两个不出现在公共 API 里，因为只有账本需要它们；外部消费者只读字段。（后果：4.2.4 的实践里我们要用**结构体字面量**手工构造记录。）

**`ScheduledInfo`：单调时钟的 admission 戳**

[pegainfer-frontend/src/engine/step.rs:L158-L168](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/step.rs#L158-L168)：`queued_at`（submit 时）、`scheduled_at`（admit 时）与 prompt 长度。文档强调两个设计点：这两个戳**由契约层盖，绝不由模型代码盖**；且用的是**单调 `Instant`**——vLLM 协议需要的墙钟（unix 浮点）由协议栈对着**自己的锚点**换算（桥侧的 `UnixAnchor`，u4-l3 详讲）。单调钟不受系统改时间影响，适合测排队延迟。

**`RejectReason`：类型化的拒绝原因**

[pegainfer-frontend/src/engine/step.rs:L178-L208](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/step.rs#L178-L208) 是 admission 期拒绝的类型化原因：超上下文（`ContextLength`）、超整段 prefill 步预算（`PrefillStepBudget`）、echo 超 prefill 上限（`EchoPrefillTokens`）、KV 预算不够（`KvBudget`）、未知 LoRA 适配器（`UnknownLoraAdapter`）、引擎不支持该特性（`Unsupported`）。

设计点有三：**类型化**让前端把拒绝类别映射到自己的错误面（HTTP 状态码、重试策略）而不是对渲染文本做模式匹配；`Display`（[step.rs:L210-L254](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/step.rs#L210-L254)）输出**面向客户端**的消息；`#[non_exhaustive]` 允许模型线未来增加新拒绝（前端必须保留通配分支），且文档约定「一个变体只在某调度器真的产生它之后才存在」。step.rs 自己的测试（[step.rs:L283-L297](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/step.rs#L283-L297)）就逐字断言了 `Unsupported` 的渲染文本。

**`Terminal`：终态三态**

[pegainfer-frontend/src/engine/step.rs:L256-L281](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/step.rs#L256-L281)：

- `Finished { reason, stop_cause, prompt_tokens, completion_tokens }`——正常完结。`reason` 复用 legacy 的 `FinishReason`（[event.rs:L11-L16](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/event.rs#L11-L16)：`Length`/`Stop`/`Error`，u3-l2 讲过的「被新契约复用的完结词汇」）；`stop_cause` 是 4.3 的类型化停止原因，**触发停止的 token 仍留在 `RequestUpdate.tokens` 里**（连同其 logprob）。
- `Rejected { reason, prompt_tokens }`——admission 期被拒：从未持有槽位、没有产出任何 token。
- `Failed { message, prompt_tokens, completion_tokens }`——引擎侧错误（执行失败、调度器 bug、引擎拆除）。

文档的关键一句：「token 计数由账本从**实际发出的内容**累计，而非模型代码手工维护」——`completion_tokens` 是账本 `push_tokens` 的流水合计，模型侧算错了也骗不了账本。

**为什么「terminal 之后再出 token」不可表达**

legacy 协议里一步内的合法事件序列是一个正则语言：

\[ \text{Scheduled}^{\le 1} \; \big( \text{Token} \mid \text{PromptTokens} \mid \text{KvTransfer} \big)^{*} \; \text{Terminal}^{\le 1} \]

类型系统只能约束「单条消息是哪个变体」，**约束不了消息之间的排列**——先发 `Token` 再发 `Finished` 再发一个 `Token`，每条消息单独看都合法，序列整体却违约。守护它的只有文档与测试。

扁平记录把这个语言直接编码成类型的积（product）：

\[ \text{RequestUpdate} = \text{Opt(Scheduled)} \times \text{List(Token)} \times \text{Opt(Cached)} \times \text{Opt(PromptEcho)} \times \text{Opt(KvTransfer)} \times \text{Opt(Terminal)} \]

「terminal 在最后」不再是一条需要遵守的规则，而是**不存在违反它的写法**：你无法构造一条「terminal 之后又出现 token」的 `RequestUpdate`，因为 `terminal` 是一个**字段**而非序列中的**位置**——记录里没有「之后」。类型上，一步内的保证是结构性的；唯一步内无法表达的跨步约束（「之后的步不再提及此 id」）则由账本承接：账户在 terminal 时关闭，`close()` 对已关账户 panic（[ledger.rs:L125-L129](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L125-L129)），测试 [ledger.rs:L596-L606](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L596-L606) 逐字断言了这个 panic（`expected = "no open account"`）。

还要诚实地说出扁平化的**代价**：legacy 序列能表达「Token、KvTransfer、又 Token」这样的交替，扁平记录不能（`kv_transfer` 只有一个槽）。但文档的立场是这些交替从未有过语义意义——一次步内的 KV 交接元数据与 token 谁先谁后对消费者不构成可观察差异，所以压扁不丢信息。

**真实消费者：`reduce_update`**

桥侧的折叠函数在 [pegainfer-frontend/src/vllm/bridge/stepped.rs:L551-L653](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge/stepped.rs#L551-L653)，函数文档：「把一个请求的步记录翻译成至多一条线上输出。只携带元数据（scheduled、cached、kv-transfer）的记录**不发任何东西**——它的事实留在 `state` 里等第一个真正的输出」。读它的骨架正好复习字段顺序：`if let Some(scheduled)`（[L556-L568](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge/stepped.rs#L556-L568)）→ `cached_tokens`/`kv_transfer`（[L569-L574](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge/stepped.rs#L569-L574)）→ `prompt_echo` → tokens/logprobs 循环（[L580-L593](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge/stepped.rs#L580-L593)）→ `match update.terminal`（[L598-L634](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge/stepped.rs#L598-L634)）。u4-l3 会完整走它；本讲只需确认：**生产侧按字段写、消费侧按同一顺序读**，约定就变成了对接线。

#### 4.2.4 代码实践

**实践目标**：手写一个 fold 函数消费 `Vec<RequestUpdate>`，把每条记录按字段顺序（admission 事实 → 新 token → terminal）还原成「该步该请求发生了什么」，用三份手工样例写成单元测试——亲身验证「顺序即结构」。

**操作步骤**：

1. 继续用 4.1.4 的草稿 crate。在 `src/lib.rs` 追加（**示例代码**）：

```rust
use std::collections::BTreeMap;
use std::time::Instant;

use pegainfer_frontend::engine::{
    FinishReason, RequestId, RequestUpdate, ScheduledInfo, StopCause, Terminal,
};

/// fold 一条更新产生的可观察事实，按字段顺序产生。
#[derive(Debug, PartialEq)]
pub enum FoldEvent {
    Scheduled { prompt_tokens: usize },
    Tokens(Vec<u32>),
    Finished { reason: FinishReason, completion_tokens: usize },
    Rejected,
    Failed,
}

/// 把一步的 updates 折叠成「每请求一串事实」，事实顺序 = 字段顺序。
pub fn fold_step(updates: Vec<RequestUpdate>) -> BTreeMap<RequestId, Vec<FoldEvent>> {
    let mut out = BTreeMap::new();
    for update in updates {
        let events = out.entry(update.id).or_insert_with(Vec::new);
        // ① admission 事实
        if let Some(s) = update.scheduled {
            events.push(FoldEvent::Scheduled {
                prompt_tokens: s.prompt_tokens,
            });
        }
        // ② 新 token（cached_tokens/prompt_echo/kv_transfer 也是 admission
        //    侧事实，本练习从简省略）
        if !update.tokens.is_empty() {
            events.push(FoldEvent::Tokens(update.tokens));
        }
        // ③ terminal
        match update.terminal {
            Some(Terminal::Finished {
                reason,
                completion_tokens,
                ..
            }) => events.push(FoldEvent::Finished {
                reason,
                completion_tokens,
            }),
            Some(Terminal::Rejected { .. }) => events.push(FoldEvent::Rejected),
            Some(Terminal::Failed { .. }) => events.push(FoldEvent::Failed),
            None => {}
        }
    }
    out
}

/// 外部 crate 造记录只能用字面量：empty() 是 pub(crate) 的账本内部帮手。
fn update(id: u64) -> RequestUpdate {
    RequestUpdate {
        id: RequestId::new(id),
        scheduled: None,
        tokens: Vec::new(),
        logprobs: Vec::new(),
        cached_tokens: None,
        prompt_echo: None,
        kv_transfer: None,
        terminal: None,
    }
}

#[test]
fn sample_a_admission_tokens_and_finish_in_one_update() {
    let now = Instant::now();
    let mut u = update(0);
    u.scheduled = Some(ScheduledInfo {
        queued_at: now,
        scheduled_at: now,
        prompt_tokens: 3,
    });
    u.tokens = vec![10, 11];
    u.terminal = Some(Terminal::Finished {
        reason: FinishReason::Stop,
        stop_cause: Some(StopCause::Token(11)),
        prompt_tokens: 3,
        completion_tokens: 2,
    });
    let folded = fold_step(vec![u]);
    assert_eq!(
        folded[&RequestId::new(0)],
        vec![
            FoldEvent::Scheduled { prompt_tokens: 3 },
            FoldEvent::Tokens(vec![10, 11]),
            FoldEvent::Finished {
                reason: FinishReason::Stop,
                completion_tokens: 2
            },
        ]
    );
}

#[test]
fn sample_b_rejection_carries_no_tokens() {
    let mut u = update(1);
    u.terminal = Some(Terminal::Rejected {
        reason: pegainfer_frontend::engine::RejectReason::ContextLength {
            prompt_tokens: 9,
            max_tokens: 8,
            limit: 16,
        },
        prompt_tokens: 9,
    });
    let folded = fold_step(vec![u]);
    assert_eq!(folded[&RequestId::new(1)], vec![FoldEvent::Rejected]);
}

#[test]
fn sample_c_two_requests_interleaved_in_one_step() {
    let now = Instant::now();
    let mut a = update(0);
    a.tokens = vec![5];
    let mut b = update(1);
    b.scheduled = Some(ScheduledInfo {
        queued_at: now,
        scheduled_at: now,
        prompt_tokens: 7,
    });
    b.tokens = vec![0, 1];
    let folded = fold_step(vec![a, b]);
    assert_eq!(folded[&RequestId::new(0)], vec![FoldEvent::Tokens(vec![5])]);
    assert_eq!(
        folded[&RequestId::new(1)],
        vec![
            FoldEvent::Scheduled { prompt_tokens: 7 },
            FoldEvent::Tokens(vec![0, 1]),
        ]
    );
}
```

2. `cargo test` 运行三个样例。

**需要观察的现象**：

- 样例 a：一条记录里 admission、两个 token、finish **按固定顺序**还原——这个顺序由你在 fold 里写的「①②③」决定，而合法数据的顺序**只有这一种**。
- 样例 b：拒绝记录里 `scheduled` 与 `tokens` 都没有——被拒的请求从未被接纳（对照 `Terminal::Rejected` 文档「从未持有槽位、没有产出 token」）。
- 样例 c：一个步里多个请求的记录互不干扰，`BTreeMap` 按 `RequestId` 排序输出（这正是 `RequestId` 派生 `Ord` 的用途之一）。

**预期结果**：三个测试全部通过。注意 `update(id)` 帮手必须用结构体字面量——`RequestUpdate::empty` 与 `is_vacant` 是 `pub(crate)`，外部 crate 够不着；而所有字段都是 `pub`，所以字面量构造畅通。

**思考题（实践要求回答）**：为什么「terminal 之后再出 token」在这个类型上不可表达，而旧 `TokenEvent` 协议只能靠约定保证？

**参考答案**：`RequestUpdate` 的 `terminal` 是一个**字段**，不是序列里的一个**位置**。一条记录内不存在「terminal 之后」这个坐标，想再发一个 token 只能出现在（a）同一条记录的 `tokens` 字段里——但那不是「之后」，是同一步内先于 terminal 的区段；或（b）**下一步**的另一条 `RequestUpdate` 里——但账本已在该请求 terminal 时关闭其账户，`push_tokens` 碰已关账户会在调用点 panic（`no open account for ...`）。两条路都被堵死。而 legacy 的 `TokenEvent` 是同质序列，类型只能约束单条消息的形状，约束不了消息间的排列，所以「`Finished` 之后再发 `Token`」类型上完全合法，只能靠文档约定与测试守护。

#### 4.2.5 小练习与答案

**练习 1**：`StepOutputs { updates: vec![] }` 会被发送吗？什么时候？

**答案**：会。空 `updates` 的用途是通知前端 metrics 变了（例如取消请求后 running/waiting 计数变化）。裁决在 `commit_step`：只有「updates 为空 **且** metrics 未变化」才跳过发送；既无更新也无 metrics 变化的步什么都不发。

**练习 2**：为什么 `cached_tokens` 不放在 `ScheduledInfo` 里、而要单独一个字段？

**答案**：文档明说：前缀缓存命中数「在调度器得知的那个步报告（第一个 prefill 分块），不在 admission 时——admission 时刻的 `Scheduled` 不可能知道它」。分块 prefill 下，命中数要到第一个分块才确定；`scheduled` 字段又受「每生命周期恰一次」约束，不能事后补写，所以缓存事实必须是一个独立于 admission 的、时机自由的字段。

**练习 3**：扁平化丢掉了 legacy 序列的哪种表达能力？为什么可以丢？

**答案**：丢掉了「不同类事实在一步内交替出现」的能力（如 Token、KvTransfer、再 Token）——每类非 token 事实只有一个槽。可以丢是因为这些交替从未有过语义差异：消费者对一步内的 KV 交接元数据与 token 的相对先后没有可观察的依赖，压扁不损失信息，却换来了「顺序即结构」的类型保证。

---

### 4.3 StopPolicy 与 StopCause：请求内的停止规则

#### 4.3.1 概念说明

「什么时候停止生成」是每个请求自带的规则：模型可能有自己的 EOS（end-of-sequence）token，协议可能指定一个不同的主 EOS，请求还可能显式列出一组 stop token。`StopPolicy` 把这三者归一成一张可查询的策略表，随 `Request` 下行；调度器每生成一个 token 就拿它问一次「这个 token 算停止吗」，答案 `StopCause` 是**类型化**的——分「EOS 停」与「显式 stop 集合停」两种，最终挂在 `Terminal::Finished.stop_cause` 上随记录上行。

它为什么是独立字段而不是折进 `params`？桥侧的注释给出了答案：`params` 是「legacy `SamplingParams` 降级，为尚未迁移到 `StopPolicy` 的 stepped 生产者保持不变」；`stop_policy` 是 Qwen3 使用的**独立**停止分类策略（[stepped.rs:L439-L445](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge/stepped.rs#L439-L445)）。迁移期两套并存，新路径走新字段。

#### 4.3.2 核心流程

```
wire 的 stop_token_ids / eos_token_id
    │ convert_stop_policy（协议栈）
    ▼
StopPolicy::new(eos, token_ids)
    │ 归一化恰一次：sort_unstable + dedup，装进 Arc<[u32]>
    ▼
随 Request 下行；调度器对每个生成的 token：
    policy.classify(token_id, is_model_eos)
        → Some(StopCause::Eos(id))    主/模型 EOS 命中
        → Some(StopCause::Token(id))  显式 stop 集合命中（二分查找）
        → None                        继续
    ▼
命中时：触发 token 仍进 RequestUpdate.tokens，
        调度器调 ledger.finish_with_cause(id, FinishReason::Stop, Some(cause))
```

分类优先级是 vLLM 约定：**EOS 优先于显式 stop 集合**——一个 token 同时是两者时按 `Eos` 报告。

#### 4.3.3 源码精读

**`EosPolicy`：EOS 的三种态度**

[pegainfer-frontend/src/engine/stop.rs:L3-L14](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/stop.rs#L3-L14)：`Ignore`（不因 EOS 停）、`ModelDefault`（**默认**，用模型执行器配置的 EOS 集合）、`Token(u32)`（用协议给的这一个主 EOS；次级 EOS 已被降级进显式 stop 集合）。

**`StopPolicy`：归一化一次，处处共享**

[pegainfer-frontend/src/engine/stop.rs:L16-L30](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/stop.rs#L16-L30) 的结构体只有两个私有字段：`eos: EosPolicy` 与 `token_ids: Arc<[u32]>`。字段文档解释了 `Arc` 的选择：「请求在构建计划、发往 worker rank 时会克隆这份策略；把归一化后的集合放在 `Arc` 后面让这些克隆廉价，并让分类对大 stop 集合可用二分查找，而不拖慢最常见的单 ID 情形」。

构造函数 [stop.rs:L33-L46](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/stop.rs#L33-L46) 的文档是「归一化恰好在请求边界发生一次；内部拷贝随后共享不可变切片，而不是反复排序、去重或克隆调用方的向量」——排序 + 去重 + `into()` 转 `Arc<[u32]>`，三行完成。

**`classify`：EOS 优先，然后二分**

[pegainfer-frontend/src/engine/stop.rs:L48-L69](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/stop.rs#L48-L69)。注意两个细节：

1. `is_model_eos` 是 `impl FnOnce(u32) -> bool` 参数——**模型 EOS 的判定知识以闭包注入**，`StopPolicy` 不 import 任何模型侧类型，保持纯词汇地位。`EosPolicy::Ignore` 时闭包根本不会被调用（`match` 直接走 `false` 分支）。
2. 显式集合用 `binary_search`——因为构造时已排序，查找 \( O(\log n) \)。

**`StopCause`：上行侧的类型化答案**

[pegainfer-frontend/src/engine/stop.rs:L72-L79](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/stop.rs#L72-L79) 只有两个变体：`Eos(u32)`（主 EOS 或模型默认 EOS）与 `Token(u32)`（显式 stop 集合）。结构体文档点出 vLLM 语义差异：「vLLM 主 EOS **没有** stop_reason；其他命中（含降级进显式集合的次级 EOS）报告 token ID」。

它上行时挂在 `Terminal::Finished { stop_cause, .. }`（[step.rs:L259-L267](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/step.rs#L259-L267)），文档重申「触发 token 仍在 `RequestUpdate.tokens` 里，请求打分时连同其 logprob」。桥侧的翻译（[stepped.rs:L612-L617](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge/stepped.rs#L612-L617)）严格对应：`StopCause::Token(id)` → 线上 `StopReason::TokenId(id)`；`StopCause::Eos` → **不设** stop_reason。桥的测试 `model_eos_has_no_wire_stop_reason` 与 `request_stop_maps_the_actual_token_and_preserves_its_logprob`（[stepped.rs:L890-L953](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge/stepped.rs#L890-L953)）分别锁住这两条。

stop.rs 自带的五个单元测试（[stop.rs:L81-L133](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/stop.rs#L81-L133)）本身就是一份行为规格：模型默认 EOS 分类、`Ignore` 不废掉显式 stop、未排序重复 id 的归一化、模型 EOS 优先于显式 stop、主 EOS 保留而次级模型 EOS 按 token stop 报告。

#### 4.3.4 代码实践

**实践目标**：用行为实验验证归一化与优先级两条规则。

**操作步骤**：

1. 先跑项目自带的规格（在**仓库根目录**执行，frontend 单元测试是纯 CPU 的）：

```bash
cargo test -p pegainfer-frontend --lib engine::stop
```

应运行 stop.rs 测试模块的 5 个测试并通过（**待本地验证**：命令形式标准，但当前环境是否可用 cargo 请自行确认）。

2. 在草稿 crate 里补一个综合实验（**示例代码**）：

```rust
use pegainfer_frontend::engine::{EosPolicy, StopCause, StopPolicy};

#[test]
fn normalization_and_priority_in_one_experiment() {
    // 传入未排序且含重复的 id
    let policy = StopPolicy::new(EosPolicy::Ignore, vec![7, 3, 7, 1]);

    // Ignore：模型 EOS 判定闭包不会被咨询（这里给 |_| true 也不会误报 EOS）
    assert_eq!(policy.classify(3, |_| true), Some(StopCause::Token(3)));
    assert_eq!(policy.classify(8, |_| true), None); // 去重+排序后集合是 {1,3,7}

    // 优先级：EOS 命中时即使也在显式集合里，也按 Eos 报告
    let policy = StopPolicy::new(EosPolicy::Token(1), vec![1, 2]);
    assert_eq!(policy.classify(1, |_| true), Some(StopCause::Eos(1)));
    assert_eq!(policy.classify(2, |_| true), Some(StopCause::Token(2)));
}
```

**需要观察的现象**：`classify(8, |_| true)` 返回 `None`——尽管闭包说「一切 token 都是模型 EOS」，`Ignore` 策略下 EOS 判定根本不参与；`classify(1)` 命中 `Eos` 而非 `Token`——优先级的直接证据。

**预期结果**：测试通过；与 stop.rs 自带测试 `normalizes_unsorted_duplicate_stop_ids`、`primary_eos_keeps_secondary_model_eos_as_token` 的断言逐条对应。

#### 4.3.5 小练习与答案

**练习 1**：`classify` 为什么把 `is_model_eos` 设计成闭包参数而不是一个 `&HashSet<u32>` 字段？

**答案**：让 `StopPolicy` 保持纯词汇、不依赖任何模型侧类型。模型 EOS 集合的定义属于各模型线（执行器配置），闭包 `impl FnOnce(u32) -> bool` 把这份知识注入而不引入耦合；`FnOnce` 还允许判定逻辑携带任意上下文。同时 `EosPolicy::Ignore` 时闭包不被调用，零成本。

**练习 2**：`StopCause::Eos` 与 `StopCause::Token` 在线上（vLLM 协议）的结局有何不同？

**答案**：`Token(id)` 被桥翻译成 `StopReason::TokenId(id)`，客户端能看到具体触发 token；`Eos` 则**不设** stop_reason（vLLM 约定：主 EOS 停止无需理由）。这就是结构体文档说的「vLLM 主 EOS 没有 stop_reason」。

**练习 3**：触发停止的那个 token 会不会丢失？

**答案**：不会。`Terminal::Finished` 的文档保证「触发 token 仍在 `RequestUpdate.tokens` 里，请求打分时连同其匹配的 logprob」；桥测试 `request_stop_maps_the_actual_token_and_preserves_its_logprob` 断言线上输出 `new_token_ids == vec![11, 43]` 且 43 的 logprob 完好。另有兼容路径：老式 stepped 生产者不带类型化 cause 时，桥会补一个合成 sentinel token（`legacy_stop_without_typed_cause_keeps_the_wire_sentinel`）。

## 5. 综合实践

**任务：写一个「迷你步流消费者」，把本讲三个模块串起来。**

在草稿 crate 中实现一个 `SessionSummary` 聚合器，输入是**跨多个步**的 `Vec<StepOutputs>`，输出每个请求的摘要（首 token 是否到来、token 总数、终态、是否与停止策略自洽）。要求：

1. **复用 4.2.4 的 `fold_step`**，但改为跨步折叠：同 id 的后续步**追加**事实。用 `RequestId` 作键。
2. **实现两条消费侧断言**（对应契约的隐含规则）：
   - `scheduled` 在该 id 的全部事实流里**恰出现一次**（第二次出现即消费侧 bug，返回错误而不是 panic——体会与账本生产侧「panic 策略」的分工差异）；
   - terminal 之后该 id **不再出现任何事实**（若出现，说明生产侧违约，返回错误）。
3. **接入 StopPolicy 做交叉验证**：构造一个 `StopPolicy::new(EosPolicy::Token(99), vec![42])`，对每份样例终态里的最后一个 token 调 `classify`，若返回 `Some(cause)` 则断言 `Terminal::Finished.stop_cause` 与该 cause 一致（自洽性检查——你在扮演一个「不信任生产者」的严格消费者）。
4. **样例数据**至少三份：
   - 请求 A：步 1 = scheduled + tokens[5]；步 2 = tokens[6, 99] + Finished(Stop, stop_cause=Eos(99))——验证练习 3 的「触发 token 不丢」；
   - 请求 B：一步内 Rejected——验证「拒绝无 scheduled、无 token」；
   - 请求 C：步 1 = scheduled；步 2 = tokens[1]；步 3 = Failed——验证 `completion_tokens` 由账本流水而来（你需要在样例里手工填这个数并说明真实系统里它来自哪）。

**验收标准**：`cargo test` 全绿；每份样例各对应一条断言链。完成后回答：你的消费者在哪几处**依赖**了「一步至多一条记录、字段顺序固定」这两条结构性质？（提示：跨步追加时你不需要处理「同一步内的乱序」，因为这种数据无法被构造出来——这就是 4.2 的论点在工程上的兑现。）

## 6. 本讲小结

- **`RequestId` 是进程内路由 id**：`Copy` 的 `u64` newtype，由 `SchedulerHandle::submit` 从 per-scheduler 原子计数器铸造；外部字符串 id 留在协议栈（桥的 `names` 映射表），`Request.client_label` 仅供日志。
- **`Request` 是纯负载，身份外置**：id、排队时间戳、abort 标志活在账本账户上；`history_tokens` 是「下一轮会精确重复的前缀长度」（前缀快照的恢复坐标），`history_marks` 新者在前、严格更小，是更早轮次快照的候选恢复点。
- **`StepOutputs` = 一个步的全部产出**：每请求至多一条扁平 `RequestUpdate`，按字段顺序折叠——admission 事实 → 新 token → terminal；空 updates 用来通知 metrics 变化。
- **「顺序即结构」**：legacy 靠约定守护的「`Scheduled` 先行、terminal 恰一次」在扁平记录里变成字段的物理排列——「terminal 之后再出 token」在类型上不可表达；跨步保证由账本承接（碰已关账户即 panic）。
- **终态类型化**：`Terminal` 三态（Finished/Rejected/Failed），token 计数来自账本流水而非模型侧手工维护；`RejectReason` 类型化且 `#[non_exhaustive]`，前端映射到自己的错误面而非解析文本。
- **`StopPolicy` 是请求内停止规则**：EOS 三态 × 归一化（排序去重进 `Arc<[u32]>`）× 二分查找，EOS 优先于显式 stop 集合；答案 `StopCause` 随 `Terminal::Finished.stop_cause` 上行，触发 token 保留在 `tokens` 里。

## 7. 下一步学习建议

本讲只讲了**线类型的形状**，两块关键机制被刻意推迟：

1. **下一讲 [u3-l4]（RequestLedger）**：这些扁平记录是谁铸的、为什么「对已关账户再 finish 会 panic」、`completion_tokens` 的流水如何累计、`register/commit_step` 为何是 `pub(crate)`——把本讲「跨步保证由账本承接」这句话展开成完整机制。
2. **[u3-l5]（Scheduler 与 drive 循环）**：`QueuedRequest` 进入 `scheduler.submit` 之后的节奏，以及「每迭代恰好一次 `commit_step`」如何决定 `StepOutputs` 消息的粒度。
3. **延伸阅读源码**：[bridge/stepped.rs 的 `reduce_update`](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge/stepped.rs#L551-L653) 是本讲线类型最完整的真实消费者，等 u4-l3 讲完两代桥后值得重读一遍——届时你会看到 `UnixAnchor` 如何把本讲的 `Instant` 渲染成线上墙钟。
4. 若想看 `history_tokens` 的测量源头（`GENERATION_PROMPT_TOKENS` 这个 extra arg 是怎么算出来的），跳到 u4-l4 的 `history.rs` 精读。
