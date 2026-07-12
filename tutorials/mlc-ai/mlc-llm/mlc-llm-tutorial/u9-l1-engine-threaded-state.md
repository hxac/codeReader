# Engine、ThreadedEngine 与 EngineState

> 本讲是「C++ 推理引擎架构」单元（U9）的第一篇，也是从 Python/编译器世界跨入 C++ 运行期世界的入口。我们只读 `cpp/serve/` 下的三个核心头文件及其实现，建立「引擎长什么样、谁在驱动它、它的状态装在哪里」的心智模型，暂不展开单个 Action 与采样算法（那是 u9-l2 与 U10 的事）。

## 1. 本讲目标

学完本讲后，读者应当能够：

- 说清 `Engine` 抽象类提供的三类接口（引擎管理 / 请求管理 / 动作循环）各自包含哪些方法、分别解决什么问题；
- 解释 `ThreadedEngine` 为什么存在：它如何用一个后台线程 + 指令队列 + 条件变量，把「线程不安全的引擎」包装成「线程安全的服务入口」，并说清楚 **`Step()` 到底由前台线程还是后台线程驱动**；
- 说出 `EngineState` 这个「状态容器」里都装了什么（`running_queue` / `waiting_queue` / `request_states` / `prefix_cache` / `metrics` 等），以及它如何被三个角色共享读写。

一句话定位：**`ThreadedEngine` 是外壳（管线程安全与消息循环），`Engine`（`EngineImpl`）是内核（真正跑模型），`EngineState` 是两者共享的「一块状态」。**

## 2. 前置知识

在进入 C++ 引擎之前，请确认你已经具备以下认知（它们在前序讲义中已建立）：

- **编译产物驱动运行期**（u1-l4、u7-l1）：`mlc_llm compile` 产出平台专用 model lib（`.so`/`.tar`/`.wasm`），里面是 TVM 编译好的 Relax/TIR 函数；`mlc-chat-config.json` 是编译期与运行期共享的契约。本讲的 `Engine` 就是「加载这些产物并驱动它们推理」的对象。
- **JSON FFI 桥**（u1-l2、u1-l3）：Python 侧把请求序列化成 JSON 字符串经 FFI 调进 C++，C++ 侧再把生成的 token 流回 Python。本讲要讲的「回调流回」就是这条桥的 C++ 端落点。
- **TVM Object 系统**：`cpp/serve/` 下的对象（如 `EngineStateObj`、`EngineActionObj`）都继承自 `tvm::ffi::Object`，用 `ObjectRef` 作为引用句柄，通过 `TVM_MODULE_VTABLE` / `TVM_FFI_STATIC_INIT_BLOCK` 注册为可跨 FFI 调用的 TVM Module。看到 `->` 作用在 `ObjectRef` 上时，它实际作用在底层 `Object` 节点上。

本讲会用到的两个并发原语（C++ 标准库）：

- `std::mutex` + `std::lock_guard` / `std::unique_lock`：保护临界区，同一时刻只允许一个线程访问共享数据。
- `std::condition_variable`（条件变量，cv）：让等待数据的线程挂起（不占 CPU 自旋），直到生产者 `notify_one()` 唤醒它。典型用法是「加锁 → 在 cv 上 `wait(谓词)` → 谓词为真时醒来并仍持有锁 → 取走数据 → 解锁」。

## 3. 本讲源码地图

| 文件 | 角色 | 关键内容 |
| --- | --- | --- |
| [cpp/serve/engine.h](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.h) | `Engine` **抽象接口** | 三类公开方法（管理 / 请求 / 动作）、`EngineCreationOutput` |
| [cpp/serve/engine.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc) | `EngineImpl` **真实实现** + TVM Module 包装 | `Engine::Create`、`AddRequest`、`Step`、`EngineModule` |
| [cpp/serve/threaded_engine.h](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.h) | `ThreadedEngine` **线程安全外壳接口** | `RunBackgroundLoop` / `AddRequest` 等 |
| [cpp/serve/threaded_engine.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc) | `ThreadedEngineImpl` **后台循环实现** | 指令队列、两个后台循环、reload/unload |
| [cpp/serve/engine_state.h](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_state.h) | `EngineStateObj` **状态容器声明** | running/waiting 队列、id 管理器、metrics |
| [cpp/serve/engine_state.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_state.cc) | `EngineStateObj` **状态容器实现** | `Reset`、`GetRunningRequestStateEntries` |
| [cpp/serve/engine_actions/action.h](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action.h) | `EngineAction` **动作接口**（仅作为 `Step` 的循环体引用） | `EngineActionObj::Step` |
| [python/mlc_llm/serve/engine_base.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py) | Python 侧 **启动后台线程** | 调用 `run_background_loop` 等的 Python 线程 |

> 提醒：本讲不会逐行读 `engine.cc` 里关于 disco 多 GPU 会话、NVSHMEM、配置自动推断（`AutoDecideEngineConfig`）等内容——它们分别属于 u12-l1（多 GPU）与 u9-l4（model runtime/config）。本讲只摘取与「接口、线程模型、状态」直接相关的部分。

## 4. 核心概念与源码讲解

### 4.1 Engine 抽象接口

#### 4.1.1 概念说明

`Engine` 是 MLC LLM 推理引擎的**核心抽象基类**，定义在 [cpp/serve/engine.h](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.h)。它描述的是「一个能持续吃请求、持续吐 token 的推理后端」，而**不关心**谁在调用它、是单线程还是多线程。

`Engine` 的头注释把公开接口明确划成三类，这是理解整个 C++ 引擎的「目录」：

> 引擎可以内部跑一个或多个模型；多模型时通常启用**推测解码**（speculative inference），其中 index 0 是主「大模型」，其余是用于「起草（draft）」的小模型。请求经 `AddRequest` 进入；引擎持续为它生成 token 直到满足停止条件；完成后经请求自带的回调返回结果。
>
> 公开接口分三类：**引擎管理、高层请求管理、引擎 Step 动作**。
>
> ——译自 [cpp/serve/engine.h:34-54](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.h#L34-L54)

需要先建立一个关键区分：`Engine` 是**抽象**，`EngineImpl`（在 `engine.cc` 里）是**真实实现**。`EngineImpl` 内部持有一个 `EngineState estate_`（共享状态）、一组 `models_`（已加载的模型）和一组 `actions_`（动作流水线）。此外还有一个用于测试的 `MockEchoEngineImpl`（把输入原样回显，不真正跑模型）。本讲讲「接口与实现的关系」时，默认指 `EngineImpl`。

#### 4.1.2 核心流程

一个 `Engine` 的生命周期大致如下（伪代码）：

```text
# 1. 创建（一次性）
Engine::Create(engine_config_json, device, callback, trace)
  └─ EngineImpl::Create
       ├─ 解析 config，加载每个 model lib 与 mlc-chat-config.json
       ├─ 建 disco 会话（多 GPU 时）→ Model::Create 一组模型
       ├─ 推断 InferrableEngineConfig（显存能装多少 KV 等）
       ├─ 为每个模型 LoadParams + CreateKVCache
       ├─ CreateEngineActions(...)  → 得到 actions_ 列表
       └─ 返回 EngineCreationOutput{ engine, completed_config, default_gen_cfg }

# 2. 持续接收请求（可被多线程调用——但 Engine 本身不保证线程安全）
engine->AddRequest(req)    # req 进入 waiting_queue，并登记 RequestState
engine->AbortRequest(id)   # 从队列移除、释放 KV、回调 abort

# 3. 推进（驱动模型前进一步）
engine->Step()             # 依次试每个 action，命中一个就执行并后处理
```

`Step()` 的内循环尤其重要——它是整个引擎的「心跳」：

```text
Step():
    for action in actions_:               # 动作按优先级排序，见 4.1.3
        processed = action->Step(estate_) # 让该动作分析状态、跑模型、采样、改状态
        if processed 非空:                 # 该动作本轮「抢到了」要做的事
            ActionStepPostProcess(...)     # 统一收尾：回调、回收、状态机推进
            return                         # 一次 Step 只做一个动作
```

即：**一次 `Step()` 只会命中并执行第一个「有事可做」的动作**（要么 prefill、要么 decode、要么推测解码的一步），然后立刻返回。引擎的「持续推进」是由外部反复调用 `Step()` 实现的（见 4.2）。

#### 4.1.3 源码精读

**三类接口的声明。** `Engine` 是纯抽象类（所有方法都是 `virtual ... = 0`），三类用注释明确分隔——引擎管理：[engine.h:57-83](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.h#L57-L83)；高层请求管理：[engine.h:85-94](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.h#L85-L94)；动作 `Step`：[engine.h:96-108](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.h#L96-L108)。`Step` 的头注释把它的语义说得很清楚：

```cpp
// The main function that the engine takes a step of action.
// At each step, the engine may decide to
// - run prefill for one (or more) requests,
// - run one-step decode for the all existing requests ...
```

引自 [cpp/serve/engine.h:98-108](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.h#L98-L108)：每一步引擎**自主决定**是 prefill 还是 decode，并在动作末尾检查是否有请求完成。

**`EngineCreationOutput`——创建引擎的「三件套」返回值。** 见 [engine.h:27-31](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.h#L27-L31)：除引擎本身外，还返回「补全后的 `EngineConfig`」（把可推断字段如 `max_num_sequence` 填好）和「默认 `GenerationConfig`」（请求未指定采样参数时的兜底）。这两个返回值会被 `ThreadedEngine` 缓存起来，供查询接口使用。

**`AddRequest` 的真实落点。** 抽象方法在 [engine.h:88](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.h#L88)，实现在 `EngineImpl`（[engine.cc:668-728](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L668-L728)）。关键两步——入队与建状态：

```cpp
// Append to the waiting queue and create the request state.
estate_->waiting_queue.push_back(request);          // ← 入「等待队列」，见 engine.cc:698
...
estate_->request_states.emplace(request->id, rstate); // ← 登记状态，见 engine.cc:727
```

引自 [engine.cc:697-727](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L697-L727)。注意几件事：

1. 进入的是 **`waiting_queue`（等待队列）**，不是 `running_queue`——请求要等被 prefill 才「转正」进入 running。
2. 若 `n > 1`（一次生成多路分支），会为每路分支创建独立的 `RequestStateEntry`，挂在同一个 `RequestState` 下，形成一个树状结构（[engine.cc:708-719](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L708-L719)）。这是 U9-l3 的主题，这里只先建立印象。
3. 进入 `AddRequest` 前还有两道前置闸门：把文本输入 token 化（`Request::FromUntokenized`）、检查 `prompt_tokens` 不超过 `max_single_sequence_length`（超过则直接回调 `"length"` 错误并返回，不进队列，见 [engine.cc:680-687](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L680-L687)）。

**`Step` 的动作循环。** 实现在 [engine.cc:749-769](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L749-L769)：

```cpp
void Step() final {
  ...
  for (EngineAction action : actions_) {
    Array<Request> processed_requests;
    { NVTXScopedRange nvtx_scope("Action step");
      processed_requests = action->Step(estate_);        // 让动作试一试
    }
    if (!processed_requests.empty()) {                   // 该动作本轮有产出
      ActionStepPostProcess(processed_requests, estate_, models_, ...);
      return;                                            // 一次 Step 只做一个动作
    }
  }
  ICHECK(estate_->running_queue.empty()) << "...";       // 走到这里说明有 bug
}
```

`actions_` 是什么？它由 `CreateEngineActions` 在创建期生成，是一组按优先级排序的 `EngineAction`。在**普通模式**（无推测解码、非分离式）下，它就是 `[NewRequestPrefill, BatchJumpForward, BatchDecode]`——见 [engine_actions/action_commons.cc:113-125](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L113-L125)：

```cpp
// The normal mode.
actions = {EngineAction::NewRequestPrefill(...),   // 优先：给等待中的请求做 prefill
           EngineAction::BatchJumpForward(...),    // 其次：grammar jump-forward
           EngineAction::BatchDecode(...)};        // 最后：给 running 请求做一步 decode
```

所以「优先 prefill 新请求，没有就 decode」的调度策略，其实就编码在 `actions_` 的**顺序**里——这是 U9-l2「事件-动作循环」要深入的主题。本讲你只需记住：`Step` 依次问每个动作「你有事吗」，第一个说「有」的就执行。

**`Engine::Create` 的转发。** 抽象基类的静态工厂方法 [engine.cc:1016-1022](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L1016-L1022) 只是把调用转发给 `EngineImpl::Create`（[engine.cc:352](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L352) 起）。`EngineImpl::Create` 是一长串初始化（加载模型、建 KV cache、建 actions），本讲不展开，但有一处值得注意——当配置里 model lib 是 `"mock://echo"` 时，会改走 `MockEchoEngineImpl::Create`（[engine.cc:393-396](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L393-L396)），这是测试用的「回显引擎」，实现见 [engine.cc:160-341](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L160-L341)。

**TVM Module 包装——`EngineModule`。** C++ 的 `Engine` 对象要能被 Python 经 FFI 调用，需要包成一个 TVM Module。`EngineModule`（[engine.cc:1031-1093](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L1031-L1093)）用 `TVM_MODULE_VTABLE` 把 `step`/`add_request`/`reset` 等名字绑定到对内部 `engine_` 的转发，最后注册全局函数 `mlc.serve.create_engine`（[engine.cc:1095-1098](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L1095-L1098)）。不过要注意：**生产路径里 Python 直接用的并不是 `mlc.serve.create_engine`，而是下一节的 `ThreadedEngine`**；`EngineModule` 更多用于测试与同步场景。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：亲手验证「三类接口」的划分与 `Step` 的「命中即返回」语义。
2. **步骤**：
   - 打开 [cpp/serve/engine.h](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.h)，在第 57、85、96 行附近找到三段注释分隔符 `/*** ... ***/`，把每段下面的纯虚方法填进下表：

     | 类别 | 方法 | 一句话作用 |
     | --- | --- | --- |
     | 引擎管理 | `Create`、`Reset`、`Empty`、… | |
     | 请求管理 | `AddRequest`、`AbortRequest`、`AbortAllRequests` | |
     | 动作 | `Step` | |

   - 打开 [engine.cc 的 `Step`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L749-L769)，确认 `for (EngineAction action : actions_)` 循环里，命中后 `return`（第 764 行）使一次 `Step` 只执行一个动作。
   - 打开 [action_commons.cc 的 `CreateEngineActions`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L113-L125)，确认普通模式 `actions_` 的三个动作顺序。
3. **现象/预期结果**：你会清楚地看到「优先 prefill、否则 jump-forward、否则 decode」的调度被编码为列表顺序，而不是任何 if-else 分支。
4. 本实践为纯阅读，无运行结果。

#### 4.1.5 小练习与答案

**练习 1**：`Engine::Create` 的返回类型是 `Result<EngineCreationOutput>`，而不是直接 `EngineCreationOutput`。这里的 `Result<...>` 包装有什么作用？

> **答**：`Result<T>` 是带错误传播的「成功/失败」联合类型（`IsOk()`/`Unwrap()`/`UnwrapErr()`）。引擎创建可能因为配置 JSON 非法、model lib 找不到、模型加载失败等原因失败；用 `Result` 把错误信息一路带回调用方（见 [engine.cc:362-364](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L362-L364) 多处 `return TResult::Error(...)`），而不是抛异常，便于在 FFI 边界转成 Python 异常。

**练习 2**：如果一个请求的输入 token 数超过 `max_single_sequence_length`，它会被加进 `waiting_queue` 吗？

> **答**：不会。`AddRequest` 在入队前先检查（[engine.cc:683-687](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L683-L687)）：超长则调用 `StreamBackError(request, "length")` 直接回调一个 finish_reason 为 `"length"` 的输出并 `return`，根本不走到 `waiting_queue.push_back`。

---

### 4.2 ThreadedEngine 后台循环

#### 4.2.1 概念说明

`Engine`（`EngineImpl`）本身**不是线程安全的**——它的 `AddRequest` / `Step` 直接读写共享的 `estate_`，没有加锁。但服务端场景里，必然有多个 Python 线程在同时：提交新请求、启动生成、读取流式结果。直接让多线程调 `Engine` 会数据竞争。

`ThreadedEngine` 就是解决这个问题的**线程安全外壳**——头注释说得很直白：

> The threaded engine keeps running a background request processing loop on a standalone thread. Ensuring thread safety, it exposes `AddRequest` and `AbortRequest` to receive new requests or abortions from other threads, and the internal request processing is backed by a normal engine wrapped inside.
>
> ——译自 [threaded_engine.h:19-26](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.h#L19-L26)

它的设计思路是经典的「**生产者-消费者 + 单线程消费者**」：

- **所有外部调用**（`AddRequest` / `AbortRequest` / `Reset` / `Reload` …）都不直接碰引擎，而是把一条「指令」塞进一个加锁的指令队列，然后立刻返回（非阻塞）。
- **唯一的后台线程**（`RunBackgroundLoop`）独占引擎：它从队列里取出全部待办指令、依次执行，然后调用一次 `background_engine_->Step()`。因为只有这一个线程碰 `Engine`，所以 `Engine` 内部无需加锁。

这样就把「多线程安全」问题降维成了「用一个 mutex 保护一个队列」。

#### 4.2.2 核心流程

`ThreadedEngineImpl` 内部有两个常驻循环（对应两个后台线程）、三把互斥锁、三个条件变量。先看主循环（请求处理）：

```text
外部线程:                          后台线程 RunBackgroundLoop():
  AddRequest(req)                    while !exit_now_:
    lock(mutex)                        lock(mutex)
    queue.push(kAddRequest, req)       wait(cv, 直到 有指令 或 引擎非空 或 exit)
    ++pending_cnt                      engine_waiting = false
    need_notify = engine_waiting       local = queue          # 整批取走
    unlock                             queue.clear()
    if need_notify: cv.notify_one()    pending_cnt = 0
                                       unlock
                                       for (kind, arg) in local:     # 逐条执行
                                         kAddRequest → background_engine_->AddRequest(arg)
                                         kAbortRequest → background_engine_->AbortRequest(arg)
                                         kReloadEngine → unload + reload
                                         kResetEngine → background_engine_->Reset()
                                         ...
                                       background_engine_->Step()    # 推进一步 ★
```

**关键结论**：`Engine::Step()` 是由**后台线程**驱动的，不是前台调用方。前台调 `ThreadedEngine::AddRequest` 只是「投递指令」，真正的 `Step` 发生在后台线程把指令消化完之后。

第二个循环 `RunBackgroundStreamBackLoop` 负责**把生成结果送回 Python**。引擎内部不直接调用户回调，而是把结果（`Array<RequestStreamOutput>`）塞进另一个加锁队列；这个专门的线程负责取出、摊平、再调用用户回调。把「产生结果」和「送回用户」拆到两个线程，是为了避免用户回调（可能慢、可能抛异常）阻塞引擎主循环。

`Reload`/`Unload` 还用第三把锁 `reload_unload_mutex_` + `reload_unload_cv_` 做「**同步等待**」——它们必须等后台线程真的把新引擎建好（或把旧引擎卸完）才返回，否则后续 `AddRequest` 会落到 `nullptr` 引擎上。

#### 4.2.3 源码精读

**指令种类。** 一切外部意图都被归一化成 `InstructionKind`（[threaded_engine.cc:31-38](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc#L31-L38)）：

```cpp
enum class InstructionKind : int {
  kAddRequest = 0, kAbortRequest = 1, kUnloadEngine = 2,
  kReloadEngine = 3, kResetEngine = 4, kDebugCallFuncOnAllAllWorker = 5,
};
```

指令队列本身存的是 `std::vector<std::pair<InstructionKind, Any>>`（`instruction_queue_`，[threaded_engine.cc:356](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc#L356)）——`Any` 是 TVM 的万能类型容器，能装 `Request`、`String`、`ObjectRef(nullptr)` 等。

**`AddRequest`：投递而非执行。** 抽象方法在 [threaded_engine.h:69](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.h#L69)，实现 [threaded_engine.cc:110-121](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc#L110-L121)：

```cpp
void AddRequest(Request request) final {
  bool need_notify = false;
  {
    std::lock_guard<std::mutex> lock(background_loop_mutex_);
    instruction_queue_.emplace_back(InstructionKind::kAddRequest, request);  // 入队
    ++pending_request_operation_cnt_;
    need_notify = engine_waiting_;     // 引擎线程是否在 wait（在才需要叫醒）
  }
  if (need_notify) {
    background_loop_cv_.notify_one();  // 出锁后再 notify
  }
}
```

注意三点：①全程**不碰** `background_engine_`，只是入队；②`need_notify` 只在后台线程真的在 `wait` 时才叫醒，避免无谓的系统调用；③`notify_one()` 在**解锁后**调用（这是 cv 的正确用法，避免唤醒后又立刻阻塞）。

**`RunBackgroundLoop`：消费 + Step。** 见 [threaded_engine.cc:136-189](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc#L136-L189)，核心三段：

```cpp
while (!exit_now_.load(std::memory_order_relaxed)) {
  { std::unique_lock<std::mutex> lock(background_loop_mutex_);
    engine_waiting_ = true;
    background_loop_cv_.wait(lock, [this] {
      return (background_engine_ != nullptr && !background_engine_->Empty()) ||
             pending_request_operation_cnt_.load() > 0 ||
             exit_now_.load(std::memory_order_relaxed);
    });
    engine_waiting_ = false;
    local_instruction_queue = instruction_queue_;   // 整批搬走
    instruction_queue_.clear();
    pending_request_operation_cnt_ = 0;
  }
  for (const auto& [kind, arg] : local_instruction_queue) {  // 逐条执行
    if (kind == InstructionKind::kAddRequest) {
      background_engine_->AddRequest(arg.as_or_throw<Request>());
    } else if (kind == InstructionKind::kAbortRequest) { ... }
    else if (kind == InstructionKind::kUnloadEngine) { EngineUnloadImpl(); }
    else if (kind == InstructionKind::kReloadEngine) { EngineUnloadImpl(); EngineReloadImpl(...); }
    else if (kind == InstructionKind::kResetEngine) { background_engine_->Reset(); }
    ...
  }
  if (background_engine_ != nullptr) {
    background_engine_->Step();   // ★ Step 发生在这里——后台线程
  }
}
```

`wait` 的谓词（[threaded_engine.cc:144-148](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc#L144-L148)）有三个唤醒条件：有指令待办、引擎里有在跑的请求（要继续 decode 它）、或要求退出。这保证即使没有新请求，后台线程也会被「引擎非空」唤醒，继续为 running 队列里的请求 decode——这就是流式生成能持续推进的根因。

**`RunBackgroundStreamBackLoop`：送回用户。** 见 [threaded_engine.cc:191-221](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc#L191-L221)：它 `wait` 在 `request_stream_callback_cv_` 上，被唤醒后把 `request_stream_callback_inputs_`（一个 `vector<Array<RequestStreamOutput>>`）整批取走、摊平成一维、再调用真正的用户回调 `request_stream_callback_(...)`。

那么 `request_stream_callback_inputs_` 是谁填的？是 `EngineReloadImpl` 里包的一层 wrapper——见 [threaded_engine.cc:271-299](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc#L271-L299)：

```cpp
void EngineReloadImpl(const std::string& engine_config_json_str) {
  auto frequest_stream_callback_wrapper = [this](Array<RequestStreamOutput> delta_outputs) {
    bool need_notify = false;
    { std::lock_guard<std::mutex> lock(request_stream_callback_mutex_);
      request_stream_callback_inputs_.push_back(std::move(delta_outputs));  // 塞队列
      ++pending_request_stream_callback_cnt_;
      need_notify = stream_callback_waiting_;
    }
    if (need_notify) { request_stream_callback_cv_.notify_one(); }
  };
  FRequestStreamCallback request_stream_callback(frequest_stream_callback_wrapper);
  // 用这个 wrapper 作为回调去建引擎：
  auto output_res = Engine::Create(engine_config_json_str, device_, request_stream_callback, ...);
  ...
}
```

引自 [threaded_engine.cc:271-299](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc#L271-L299)。这意味着：**引擎内部 `ActionStepPostProcess` 触发的回调，并不会直接打到 Python，而是先入「送回队列」，再由专用线程送回**——引擎主循环因此绝不会被用户回调拖慢。

**TVM Module 与注册。** `ThreadedEngineModule`（[threaded_engine.cc:384-402](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc#L384-L402)）把方法暴露为 `"add_request"` / `"run_background_loop"` 等名字，并注册全局工厂 `mlc.serve.create_threaded_engine`（[threaded_engine.cc:404-408](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc#L404-L408)）。

**Python 侧「谁启动了后台线程」。** 在 [engine_base.py:612](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py#L612) 取到 threaded engine 模块，随后：

```python
background_loop = self._ffi["run_background_loop"]
background_stream_back_loop = self._ffi["run_background_stream_back_loop"]
self._background_loop_thread = threading.Thread(target=background_loop)
self._background_stream_back_loop_thread = threading.Thread(target=background_stream_back_loop)
self._background_loop_thread.start()
self._background_stream_back_loop_thread.start()
```

引自 [engine_base.py:636-645](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py#L636-L645)。这两个 Python 线程跑的就是上面两个 C++ `RunBackgroundLoop`——也就是说，**「后台线程」从 Python 看是 `threading.Thread`，它进入 C++ 后就是 `RunBackgroundLoop`，`Step` 在它里面执行**。`json_ffi/engine.py` 的 `BackgroundLoops` 类做了完全相同的事（[json_ffi/engine.py:75-91](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/json_ffi/engine.py#L75-L91)）。

#### 4.2.4 代码实践（调用链跟踪型）

1. **目标**：验证「`ThreadedEngine::AddRequest` 投递指令 → 后台线程执行 `Engine::AddRequest` + `Engine::Step`」的完整链路，并回答 Step 由谁驱动。
2. **步骤**：
   - 在 [threaded_engine.cc:110](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc#L110) 的 `AddRequest` 打断点（或用 `LOG(INFO)` 临时加日志），再在 [threaded_engine.cc:157](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc#L157) 的 `background_engine_->AddRequest` 与 [threaded_engine.cc:186](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc#L186) 的 `background_engine_->Step()` 各加一条日志，打印 `std::this_thread::get_id()`。
   - 用 `MLCEngine`（本地 mode）跑一次最小 chat（参考 u1-l3 的示例脚本），发起一次请求。
3. **现象/预期结果**：
   - 你会看到 `AddRequest`（投递点）的线程 id 与 `background_engine_->AddRequest` / `Step` 的线程 id **不同**：前者是调用方（Python 主线程），后者是后台循环线程。
   - 日志顺序是：投递 `AddRequest` → 后台 `background_engine_->AddRequest` → 后台 `Step` → （多轮）后台 `Step` … 直到请求完成。
4. **结论**：`Step` 由**后台线程**驱动；前台只负责投递指令。日志属于临时改动，验证后请还原。

> ⚠️ 注意：若仅阅读而无法本地运行大模型，可只做静态跟踪：从 [engine_base.py:630-645](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py#L630-L645) → [threaded_engine.cc:110](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc#L110) → [threaded_engine.cc:136-189](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc#L136-L189) 的链路也能完全说清问题，运行结果标注「待本地验证」即可。

#### 4.2.5 小练习与答案

**练习 1**：`RunBackgroundLoop` 的 `wait` 谓词里有 `background_engine_ != nullptr && !background_engine_->Empty()` 这一项。它解决什么问题？

> **答**：它保证即使**没有新指令**，只要引擎里还有「在跑的请求」（`Empty()` 为假，即 running/waiting 队列非空），后台线程也会被唤醒继续 `Step()`。否则流式 decode 会因为「投递一次请求却没人继续叫醒」而卡死——这是持续生成 token 的关键。

**练习 2**：为什么 `EngineReloadImpl` 要把回调包一层 wrapper，而不是直接把用户回调交给 `Engine::Create`？

> **答**：为了把「引擎产生结果」与「送回用户」解耦到两个线程。wrapper 只把结果塞进 `request_stream_callback_inputs_` 队列就返回（极快、不阻塞引擎主循环）；真正的用户回调由专门的 `RunBackgroundStreamBackLoop` 线程调用。这样即使用户回调很慢或抛异常，也不会拖慢或拖垮推理主循环。

**练习 3**：`Reload` 与 `Unload` 为什么需要第三把锁 `reload_unload_mutex_` + 同步等待？

> **答**：因为 reload/unload 是「重活」（建/拆引擎），且后续操作依赖它完成。若 `Reload` 投递指令后立刻返回，调用方紧接着 `AddRequest` 可能命中一个还没建好的 `nullptr` 引擎。因此 `Reload` 投递指令后在 `reload_unload_cv_` 上**阻塞等待**，直到后台线程执行完 `EngineReloadImpl` 把 `reload_finished_` 置真并唤醒它（[threaded_engine.cc:68-72](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc#L68-L72) 与 [threaded_engine.cc:293-298](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc#L293-L298)）。

---

### 4.3 EngineState 状态容器

#### 4.3.1 概念说明

如果说 `Engine` 是「行为」、`ThreadedEngine` 是「线程模型」，那么 `EngineState` 就是它们共同读写的「**一块状态**」。`EngineStateObj` 是一个 TVM `Object`（[engine_state.h:64](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_state.h#L64)），它的字段就是引擎运行期的全部「软状态」。

`EngineImpl` 持有一个 `EngineState estate_`（[engine.cc:994](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L994)）。注意一个微妙的点：`ThreadedEngine` 并**不直接持有** `EngineState`，它通过内部的 `background_engine_`（即 `EngineImpl`）间接访问。因为只有后台线程碰 `EngineImpl`，所以对 `EngineState` 的读写也是天然单线程的——这正是 4.2 那套设计的红利。

#### 4.3.2 核心流程

`EngineStateObj` 的字段可以按职责分成五组：

| 分组 | 字段 | 作用 |
| --- | --- | --- |
| **请求队列** | `waiting_queue`、`running_queue` | 两级流水线：新请求先进 waiting，被 prefill 后转 running |
| **请求状态** | `request_states`（id → `RequestState`） | 每个请求的细粒度运行状态（committed/draft tokens、状态机） |
| **资源管理** | `id_manager`、`prefix_cache` | 内部 id 回收复用；前缀缓存（复用 KV，详见 U10-l2） |
| **观测** | `metrics` | 运行期指标（prefill/decode 吞吐等，对外经 `/stats`） |
| **配置/工作区** | `spec_draft_length`、`disaggregation`、`postproc_workspace`、`request_stream_callback_` | 当前模式标志、动作后处理复用缓冲、回调 |

两个队列的流转（请求生命周期，U9-l3 详讲）：

```text
AddRequest                       NewRequestPrefill (action)         BatchDecode (action)
   │                                      │                                │
   ▼                                      ▼                                ▼
waiting_queue  ──── prefill 完成 ────> running_queue  ──── 每步 decode ────> running_queue
                                          │
                                   满足停止条件 / 被抢占
                                          ▼
                                     (从队列移除，回调结果)
```

#### 4.3.3 源码精读

**状态字段全集。** 见 [engine_state.h:64-96](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_state.h#L64-L96)。几处关键声明：

```cpp
class EngineStateObj : public Object {
 public:
  std::vector<Request> running_queue;          // 正在跑的（已 prefill），L67
  std::vector<Request> waiting_queue;          // 还没开始处理的，L69
  std::unordered_map<String, RequestState> request_states;  // id→状态，L71
  EngineInternalIDManager id_manager;          // 内部 id 分配/回收，L73
  EngineMetrics metrics;                       // 运行期指标，L75
  PrefixCache prefix_cache{nullptr};           // 前缀缓存，L77
  bool running_rsentries_changed = true;       // 「脏标记」，L79
  int spec_draft_length = 0;                   // 推测解码当前 draft 长度，L86
  bool disaggregation = false;                 // 是否分离式推理，L88
  FRequestStreamCallback request_stream_callback_;  // 回调，L90
  ActionPostProcessWorkspace postproc_workspace;    // 后处理复用缓冲，L95
  ...
};
```

**内部 id 管理器——用栈回收复用。** `EngineInternalIDManager`（[engine_state.h:29-52](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_state.h#L29-L52)）维护一个 `available_ids` 栈和单调递增的 `id_cnt`。`GetNewId()` 优先复用已回收的 id，不够才递增计数；`RecycleId()` 把 id 压回栈。这样请求频繁进出时，内部 id 不会无限增长，模型侧 KV cache 的序列槽位也能被复用。

**脏标记 + 惰性重建：`GetRunningRequestStateEntries`。** 这是最能体现「状态容器」设计的一处。引擎在每个 `Step` 里需要「当前真正在 decode 的请求状态条目列表」，但不必每次重算。实现见 [engine_state.cc:33-50](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_state.cc#L33-L50)：

```cpp
const std::vector<RequestStateEntry>& EngineStateObj::GetRunningRequestStateEntries() {
  if (running_rsentries_changed) {            // 仅当脏时重建
    cached_running_rsentries_.clear();
    for (const Request& request : running_queue) {
      for (const RequestStateEntry& rsentry : GetRequestState(request)->entries) {
        // 只有「叶子且已完成 prefill（inputs 为空）的 alive 条目」才算 running
        if (rsentry->status == RequestStateStatus::kAlive &&
            rsentry->child_indices.empty() &&
            rsentry->mstates[0]->inputs.empty()) {
          cached_running_rsentries_.push_back(rsentry);
        }
      }
    }
    running_rsentries_changed = false;
  }
  return cached_running_rsentries_;
}
```

这套机制有三个要点：①`running_rsentries_changed` 是「脏标记」，任何改变 running 队列或其条目状态的动作（prefill 转正、decode 完成、abort、抢占）都要把它置真（例如 [engine.cc:148](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L148) 的 abort 末尾、[engine.cc:661](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L661) 的 disagg 处理）；②过滤条件 `child_indices.empty()` 保证只取**叶子**条目（多分支生成时，分支点是内部节点，不直接 decode）；③`mstates[0]->inputs.empty()` 表示该条目的输入 token 已被 prefill 吃完，真正进入「逐 token decode」阶段。

**`Reset`——彻底清空。** 见 [engine_state.cc:15-26](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_state.cc#L15-L26)：清两个队列、清 `request_states`、重置 id 管理器、重置 metrics、重置 prefix_cache、置脏标记、重置工作区。`EngineImpl::Reset`（[engine.cc:508-514](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L508-L514)）在调用它之前还先 `AbortAllRequests()` 并逐个 `model->Reset()`。

**`Empty` 判空。** `EngineImpl::Empty` 直接读状态的两个队列（[engine.cc:516](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L516)）：`estate_->running_queue.empty() && estate_->waiting_queue.empty()`。这正是 `ThreadedEngine` 后台循环 wait 谓词里 `!background_engine_->Empty()` 的依据。

**`metrics` 的对外出口。** `JSONMetrics`（[engine.cc:518](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L518)）把 `estate_->metrics` 序列化成 JSON；运行期还能经 `HandleSpecialRequests` 里的 `kQueryEngineMetrics` 特殊请求把 metrics 当 usage 流回（[engine.cc:535-547](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L535-L547)）——这正是 u1-l3 提到的 `/stats` 与 `usage.extra` 里 `prefill/decode_tokens_per_s` 的来源。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：理清两级队列与脏标记缓存的关系，并能解释「为什么新请求先进 waiting」。
2. **步骤**：
   - 在 [engine_state.h:67-69](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_state.h#L67-L69) 确认两个 `std::vector<Request>` 字段。
   - 用 Grep 在 `cpp/serve/engine.cc` 与 `cpp/serve/engine_actions/` 下搜索 `waiting_queue.push_back` 与 `running_queue.push_back`，观察谁往哪个队列里塞请求。
   - 在 [engine_state.cc:33-50](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_state.cc#L33-L50) 的 `GetRunningRequestStateEntries` 处，确认它如何用脏标记跳过重复计算。
3. **预期结果**：
   - `AddRequest`（[engine.cc:698](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L698)）只往 `waiting_queue` 塞；
   - 真正把请求从 waiting 搬进 running、并写入其 `RequestState` 的，是 `NewRequestPrefill` 这一 Action（动作内部细节属 u9-l2/u9-l3，本讲只需确认「搬移发生在 prefill 动作里」）。
4. 本实践为纯阅读，无运行结果。

#### 4.3.5 小练习与答案

**练习 1**：`EngineStateObj` 声明为 `_type_mutable = true`（[engine_state.h:111](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_state.h#L111)）。这意味着什么？为什么合理？

> **答**：TVM Object 默认是不可变（函数式）的，`_type_mutable = true` 表示这个对象允许原地修改字段。引擎状态天然是「会被频繁原地更新」的可变状态（队列进进出出、metrics 累加），若每次改动都复制整个对象代价太大，因此声明为 mutable。代价是它不能安全地被多线程并发读写——这正回扣了 4.2 的结论：只有后台线程能碰它。

**练习 2**：`GetRunningRequestStateEntries` 的过滤条件里有 `mstates[0]->inputs.empty()`。如果去掉这个条件会怎样？

> **答**：会把「输入 token 还没 prefill 完」的条目也误算成 running，于是 `BatchDecode` 这类动作会对尚未完成 prefill 的请求发起 decode，行为错误。这个条件保证了「只有输入已全部消化、真正进入逐 token 生成阶段」的条目才参与 batch decode。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这张「**三者关系 + 一次请求的数据流**」图（用你喜欢的画图工具或纸笔）。要求图里必须出现下列元素，并用箭头标出数据流方向：

```text
┌───────────────────────────── Python 进程 ─────────────────────────────┐
│                                                                       │
│  主线程                        后台线程 A              后台线程 B       │
│  MLCEngine.chat(...)          run_background_loop    run_background_  │
│       │                       (RunBackgroundLoop)    stream_back_loop │
│       │  add_request               │                      │          │
│       ├──────────────────────►  ┌──▼───────────────────┐  │          │
│       │                          │ ThreadedEngineImpl    │  │          │
│       │                          │  instruction_queue_   │  │          │
│       │                          │  (mutex+cv 保护)      │  │          │
│       │                          │       │               │  │          │
│       │                          │  消化指令 + Step()     │  │          │
│       │                          │       │               │  │          │
│       │                          │  ┌────▼────────────┐  │  │          │
│       │                          │  │ EngineImpl       │  │  │          │
│       │                          │  │ (background_     │  │  │          │
│       │                          │  │   engine_)       │  │  │          │
│       │                          │  │  ├─ models_      │  │  │          │
│       │                          │  │  ├─ actions_     │  │  │          │
│       │                          │  │  └─ estate_ ─┐   │  │  │          │
│       │                          │  └─────────────┼──┘  │  │          │
│       │                          │            ┌───▼────┐ │  │          │
│       │                          │            │EngineState│ │          │
│       │                          │            │ waiting_q │ │          │
│       │                          │            │ running_q │ │          │
│       │                          │            │ metrics … │ │          │
│       │                          │            └──────────┘ │          │
│       │                          │  回调(wrapper)塞队列     │          │
│       │                          ├──────────────────────────►          │
│       │                          │            streamback 队列          │
│       │  ◄────────────────────────────────── 用户回调 (delta tokens)   │
│                                                                       │
└───────────────────────────────────────────────────────────────────────┘
```

在图边用文字回答两个问题（这是本讲规格里的核心实践任务）：

1. **`AddRequest` 如何把请求放入 `waiting_queue`？**
   答：分两层。`ThreadedEngine::AddRequest` 只是把 `(kAddRequest, request)` 塞进 `instruction_queue_` 并 notify（[threaded_engine.cc:110-121](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc#L110-L121)）；后台线程 `RunBackgroundLoop` 取出指令后调用 `background_engine_->AddRequest`，后者在 token 化与长度校验通过后，执行 `estate_->waiting_queue.push_back(request)` 并登记 `request_states`（[engine.cc:698-727](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L698-L727)）。
2. **`Step` 由谁驱动（前台还是后台）？**
   答：由**后台线程 A**（即 `RunBackgroundLoop`）驱动。前台主线程只投递指令；后台线程消化完指令后调用 `background_engine_->Step()`（[threaded_engine.cc:185-187](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc#L185-L187)）。这个后台线程在 Python 侧由 `threading.Thread(target=...)` 启动（[engine_base.py:636-645](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py#L636-L645)）。`Step` 内部依次问 `actions_` 里每个动作，命中第一个「有事可做」的就执行并后处理（[engine.cc:749-769](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L749-L769)）。

> 若需运行验证：用 `MLCEngine` 跑一次最简 chat（参考 u1-l3 的 `examples/python/sample_mlc_engine.py`），用 `nvidia-smi -l 1` 或 `/stats` 观察到「请求提交后即使 Python 主线程阻塞在 `__next__` 上，GPU 仍在持续推进、token 持续流回」，即可佐证后台线程在独立驱动 `Step`。运行结果若无 GPU 环境则标注「待本地验证」。

## 6. 本讲小结

- `Engine`（抽象基类）把接口分成三类——**引擎管理**（`Create`/`Reset`/`Empty`…）、**请求管理**（`AddRequest`/`AbortRequest`/`AbortAllRequests`）、**动作循环**（`Step`），定义于 [engine.h:55-117](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.h#L55-L117)；真实实现是 `EngineImpl`。
- `Step` 是「心跳」：依次问 `actions_` 里每个动作，**命中第一个有事的就执行并立刻返回**；普通模式下动作顺序是 `NewRequestPrefill → BatchJumpForward → BatchDecode`。
- `ThreadedEngine` 是线程安全外壳：用「**单消费者后台线程 + 加锁指令队列**」把线程不安全的 `Engine` 包装成可被多线程调用的服务入口（[threaded_engine.h:19-26](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.h#L19-L26)）。
- **`Step` 由后台线程驱动**；前台 `AddRequest` 只投递指令，真正的入队与 `Step` 都发生在后台循环里（[threaded_engine.cc:136-189](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc#L136-L189)）。
- 引擎的回调被包了一层 wrapper，先入「送回队列」再由独立的 `RunBackgroundStreamBackLoop` 线程送回用户，避免用户回调拖慢推理。
- `EngineState` 是三者共享的「状态容器」：`waiting_queue`/`running_queue`（两级流水线）、`request_states`（每个请求的细粒度状态）、`id_manager`、`prefix_cache`、`metrics`，用脏标记 + 惰性重建优化 `GetRunningRequestStateEntries`；因声明为 mutable，必须由唯一后台线程访问。

## 7. 下一步学习建议

- **u9-l2（事件-动作循环与 Action 接口）**：本讲把 `Step` 当成黑盒「问每个 action」；下一讲打开 `EngineActionObj::Step`，讲清 NewRequestPrefill/BatchDecode/BatchVerify/Eagle*/AutoSpecDecode/Disagg* 等 Action 各自处理哪一阶段，以及 `ActionStepPostProcess` 的统一收尾。
- **u9-l3（请求生命周期与状态机）**：本讲只说「请求从 waiting 到 running」；下一讲深入 `Request` / `RequestState` / `RequestStateEntry` / `RequestModelState`，讲清 committed/draft tokens、状态机与抢占（preemption）。
- **u9-l4（模型运行时与 FunctionTable）**：本讲把 `Engine::Create` 里加载模型、建 KV cache 的细节略过了；下一讲打开 `model.h` / `function_table.h` / `config.h`，看 model lib 如何被解析成可调用函数。
- 复习时，可回到本讲重画第 5 节的关系图，确保你能不看答案讲清「一次请求从 Python 到 token 流回」的完整跨线程链路。
