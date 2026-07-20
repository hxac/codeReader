# Service 与 Session：会话管理

## 1. 本讲目标

在上一篇（u2-l1）里，我们看清了「一次按键」在 librime 内部如何被表示成 `KeyEvent`。但按键不是凭空进入引擎的——前端要先告诉引擎「我现在要操作的是哪一个输入上下文」。这就引出了**会话（Session）**这个概念。

本讲学完后，你应该能够：

- 说清楚「会话」为什么存在，以及 `SessionId` 是怎么编号的。
- 理解 `Service` 作为单例，如何用一个 `map` 统一管理所有会话。
- 描述一个会话从 `CreateSession` 到 `DestroySession` 的完整生命周期，包括 5 分钟过期回收。
- 解释 `Session` 内部如何持有一个 `Engine`，并把按键转发给它。
- 追踪「提交文字」和「通知消息」两条信号链：引擎 → Session → 前端。

本讲只看会话管理的骨架，**不展开** Engine 内部的按键流水线（那是 u6 的主题），也**不展开** Context 的内部结构（那是 u3-l1 的主题）。

## 2. 前置知识

阅读本讲前，建议你已经掌握：

- **C API 总入口**（u1-l4）：知道 `rime_get_api()` 返回一张方法指针表 `RimeApi`，会话相关方法都以 `session_id` 为主键。
- **console 示例**（u1-l5）：见过 `create_session` / `destroy_session` / `get_commit` 的调用顺序。
- **KeyEvent**（u2-l1）：知道一次按键 = `(keycode, modifier)` 两个 int。

本讲会用到的几个 C++ 背景，先做个 30 秒速览：

- **智能指针别名**（来自 `common.h`）：librime 用 `the<T>` 表示 `unique_ptr<T>`（独占所有权），用 `an<T>` 表示 `shared_ptr<T>`（共享所有权）。看到 `the<Engine> engine_` 就读作「这个 Session 独占一个 Engine」。
- **Boost 信号槽**：`signal<void(Args...)>` 是一个可被多个回调订阅的「信号」，`.connect(fn)` 订阅，`signal_(args)` 触发时所有订阅者都会被调用。这是 librime 内部传递事件的主要机制。
- **`reinterpret_cast<uintptr_t>`**：把一个指针的地址值转成一个整数。本讲里它被用来「把 Session 对象的地址当成它的编号」。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `src/rime/service.h` | `Session` 与 `Service` 两个类的声明，是本讲的核心地图。 |
| `src/rime/service.cc` | 上述两个类的实现：会话的创建、查找、销毁、过期回收、通知回调。 |
| `src/rime_api_impl.h` | C API 包装层。`RimeCreateSession`/`RimeDestroySession` 等 wrapper 在这里把 C 调用委托给 `Service` 单例。 |
| `src/rime/engine.h` | `Engine` 抽象基类，声明了提交信号 `CommitSink` 和工厂 `Engine::Create()`。 |
| `src/rime/engine.cc` | `Engine::Create()` 的实现，以及 `ConcreteEngine` 如何把 Context 的提交信号接到引擎的 `sink_`。 |
| `src/rime/messenger.h` | `Messenger` 基类，定义了通知信号 `MessageSink`，供 `Service::Notify` 使用。 |

> 一个易混点：本讲的源码引用会同时出现 `rime_api.cc` 与 `rime_api_impl.h`。`rime_api.cc` 通过 `#include "rime_api_impl.h"` 把大量 wrapper 引入；**会话管理相关的 C wrapper（`RimeCreateSession` 等）以及 `RimeApi` 结构体的方法指针填充，实际都写在 `rime_api_impl.h` 里**。所以追踪 C API → Service 的委托链时，目标文件是 `rime_api_impl.h`，请以源码为准，不要被文件名误导。

## 4. 核心概念与源码讲解

### 4.1 为什么需要「会话」：会话抽象与 SessionId

#### 4.1.1 概念说明

想象一个真实场景：你在 macOS 上开着 Squirrel（RIME 前端），同时在一个浏览器输入框和一个聊天窗口里打字。两个输入框**各自有自己的候选词状态、自己的光标位置、自己刚打了一半的拼音**。如果引擎只有一份全局状态，两边的输入会互相串扰。

所以 librime 引入了 **Session（会话）**：每个「输入焦点」对应一个独立的 Session，Session 内部装着完整的引擎状态（Engine + Context）。前端拿到一个 `SessionId` 当作「凭证」，之后所有的 `process_key` / `get_commit` / `set_option` 都带上这个凭证，引擎就知道该操作哪一份状态。

**SessionId 是怎么编号的？** 看 `service.h` 的开头：

```cpp
using SessionId = uintptr_t;
static const SessionId kInvalidSessionId = 0;
```

[service.h:18-20](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.h#L18-L20) — 中文说明：把 `SessionId` 定义成指针宽度的无符号整数（`uintptr_t`），并用 `0` 表示「无效会话」。选 `uintptr_t` 而不是 `int`，是为了能安全地把「Session 对象的地址」塞进去（见 4.1.3）。

#### 4.1.2 核心流程

一个前端和 librime 打交道时，会话这条线的典型流程是：

```
前端                                librime 内部
 |                                     |
 |-- create_session() ---------------->| Service 新建一个 Session，返回 id
 |<-- session_id --------------------- |
 |                                     |
 |-- process_key(id, keycode, mask) -->| 用 id 找到 Session，把按键交给它的 Engine
 |-- get_commit(id) ----------------->| 读取该 Session 积累的提交文字
 |-- set_option(id, ...) ------------->| 修改该 Session 的选项
 |          ...                        |
 |-- destroy_session(id) ------------->| 从 map 里删掉，释放 Engine/Context
```

关键点：`id` 是贯穿所有会话级 API 的「主键」。前端的典型用法是「一个输入焦点持有一个 id，焦点消失时 destroy」。

#### 4.1.3 源码精读

SessionId 的值从哪来？看 `Session` 构造函数和 `CreateSession`：

```cpp
Session::Session() {
  engine_.reset(Engine::Create());
  ...
  SessionId session_id = reinterpret_cast<SessionId>(this);
  ...
}
```

[service.cc:17-24](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.cc#L17-L24) — 中文说明：Session 构造时，把 `this` 指针（Session 对象自身的地址）`reinterpret_cast` 成整数，作为自己的 `session_id`。这就是上面「`SessionId` 用 `uintptr_t`」的原因——它要能装下一个指针。

而 `CreateSession` 里真正登记进 map 的 id 是「`shared_ptr` 管理的对象裸指针」：

```cpp
SessionId Service::CreateSession() {
  SessionId id = kInvalidSessionId;
  if (disabled()) return id;
  try {
    auto session = New<Session>();
    session->Activate();
    id = reinterpret_cast<uintptr_t>(session.get());
    sessions_[id] = session;
  } catch (...) { LOG(ERROR) << "Error creating session: ..."; }
  return id;
}
```

[service.cc:85-106](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.cc#L85-L106) — 中文说明：用 `New<Session>()`（等价 `make_shared`）造一个 Session，`Activate()` 打时间戳，再把「裸指针地址」当成 `id` 存进 `sessions_` 这个 map。`try/catch` 把任何异常都吞掉，只记日志并返回 `kInvalidSessionId`（0），保证 C 边界不会抛出 C++ 异常。

> 小提示：用「对象地址当 id」是个常见技巧——只要对象还活着，地址就唯一；对象一旦从 map 里 `erase`，id 自然失效。缺点是地址会被复用（旧 Session 销毁后，新 Session 可能拿到同一个地址），所以不能把 id 当成「永久身份」来缓存，必须每次用 `GetSession` 重新校验。

#### 4.1.4 代码实践

**实践目标**：亲手验证「SessionId = Session 对象地址」这个设计。

**操作步骤**（源码阅读型）：

1. 打开 [service.cc:85-106](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.cc#L85-L106)，确认 `id` 来自 `reinterpret_cast<uintptr_t>(session.get())`。
2. 打开 [service.cc:108-118](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.cc#L108-L118) 的 `GetSession`，注意它用同一个 `id` 去 `sessions_.find(id)` 查表。
3. 思考：如果两次 `CreateSession` 之间没有任何 Session 被销毁，两个 id 会不会相等？为什么？

**需要观察的现象**：`id` 与「map 的 key」是同一个值；`find` 用 key 查 value，构成了「凭证 → 对象」的回路。

**预期结果**：只要两个 Session 同时存活，它们的地址不同，id 也不同；若一个 Session 被 `erase`，其 id 立刻查不到（`GetSession` 返回 `nullptr`）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `kInvalidSessionId` 选 `0` 而不是 `-1`？

**参考答案**：`SessionId` 是 `uintptr_t`（无符号），`-1` 会变成一个巨大的合法地址值，可能误判；而 `new` 出来的对象地址不会是 `0`（空指针），用 `0` 当「无效」标记天然安全。

**练习 2**：前端把一个 `session_id` 缓存了一小时后再用，会有什么风险？

**参考答案**：该 Session 可能已被 `DestroySession` 或过期回收，`GetSession` 返回 `nullptr`；更隐蔽的是，地址可能被复用，缓存的老 id 可能意外命中一个**全新**的 Session。所以前端应把 id 当作短期凭证，配合 `find_session` 校验。

---

### 4.2 Service：单例化的会话管理器

#### 4.2.1 概念说明

整个 librime 进程里，会话需要被**集中**管理——否则过期回收、维护模式判断、通知回调都无处安放。这个「中枢」就是 `Service`。它被设计成**单例**：全局只有一个 `Service::instance()`，谁都能拿到它，状态全局共享。

`Service` 同时还持有 `Deployer`（部署器，u9 会讲），所以它其实是「运行时中枢 + 部署入口」的结合体。本讲只关注它作为「会话容器」的一面。

#### 4.2.2 核心流程

```
        ┌────────────── Service::instance() (单例) ──────────────┐
        │                                                          │
        │   SessionMap sessions_;   ←  id -> shared_ptr<Session>  │
        │   Deployer  deployer_;    ←  数据目录 + 维护模式开关     │
        │   NotificationHandler notification_handler_; ← 前端回调 │
        │   std::mutex mutex_;      ←  保护 notification_handler_ │
        │   bool started_ = false;  ←  服务是否已 Start           │
        │                                                          │
        │   disabled() == (!started_ || deployer_.IsMaintenanceMode()) │
        └──────────────────────────────────────────────────────────┘
```

`disabled()` 是个很关键的开关：**只要服务没启动，或者正在做部署维护（编译词典等），就拒绝创建/获取会话**。这样能避免前端在引擎还没准备好时塞按键进来。

#### 4.2.3 源码精读

单例实现是经典的「函数内静态变量 + 懒加载」：

```cpp
Service& Service::instance() {
  static the<Service> s_instance;
  if (!s_instance) {
    s_instance.reset(new Service);
  }
  return *s_instance;
}
```

[service.cc:196-202](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.cc#L196-L202) — 中文说明：第一次调用时 `new` 一个 `Service` 存进静态 `the<Service>`（`unique_ptr`），之后每次都返回同一个引用。C++11 起函数内静态变量的初始化是线程安全的。

启停与禁用判断：

```cpp
void Service::StartService() { started_ = true; }
void Service::StopService() { started_ = false; CleanupAllSessions(); }
...
bool disabled() { return !started_ || deployer_.IsMaintenanceMode(); }
```

[service.cc:76-83](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.cc#L76-L83) 与 [service.h:85](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.h#L85) — 中文说明：`StartService` 只是把 `started_` 置真；`StopService` 置假并清空所有会话。`disabled()` 综合两个条件——「未启动」或「部署器在维护中」——任一为真就视作禁用。

成员声明（这是本讲最该记住的一张表）：

```cpp
using SessionMap = map<SessionId, an<Session>>;
SessionMap sessions_;
Deployer deployer_;
NotificationHandler notification_handler_;
std::mutex mutex_;
bool started_ = false;
```

[service.h:92-97](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.h#L92-L97) — 中文说明：`sessions_` 是 `SessionId → shared_ptr<Session>` 的有序映射（用 `map` 而非 `unordered_map`，因为后面过期回收要按 key 遍历，`map` 的迭代顺序稳定）。`mutex_` 仅用于保护 `Notify`（见 4.5）。

#### 4.2.4 代码实践

**实践目标**：确认 `initialize` → `StartService` 这条链。

**操作步骤**：

1. 在 [rime_api_impl.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L51-L56) 找到 `RimeInitialize`，看它最后调用 `Service::instance().StartService()`。
2. 在 [rime_api_impl.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L58-L63) 找到 `RimeFinalize`，看它调用 `StopService()`。
3. 联系 [service.h:85](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.h#L85) 的 `disabled()`：如果前端忘了 `initialize` 就直接 `create_session`，会发生什么？

**需要观察的现象**：`StartService` 是会话能被创建的前提；`disabled()` 让未初始化的调用安全失败。

**预期结果**：未 `initialize` 时 `started_==false`，`CreateSession` 因 `disabled()` 返回 `kInvalidSessionId`（0）。

#### 4.2.5 小练习与答案

**练习 1**：`Service` 为什么用单例而不是让每个前端各持一份？

**参考答案**：会话需要全局可见——部署维护模式是进程级状态，通知回调也只有一个落点；用单例保证所有 C API 调用都打到同一份 `sessions_`，避免状态碎片化。

**练习 2**：`disabled()` 把「部署维护中」也当作禁用，为什么？

**参考答案**：维护期间往往在重编译词典、写盘（u9），此时会话的 Engine/词典可能正在被替换或不可用；直接拒绝会话操作比让按键打到半成品状态更安全。

---

### 4.3 会话生命周期：Create / Get / Destroy / CleanupStale

#### 4.3.1 概念说明

会话不是永久存在的。前端打开一个输入框 → 创建；输入框关闭 → 销毁。但前端**可能忘记销毁**（比如窗口崩溃），于是 librime 还需要一个**兜底机制**：超过一定时间没人用的会话，自动回收。这就是 `Session::kLifeSpan`（5 分钟）和 `CleanupStaleSessions` 的用途。

#### 4.3.2 核心流程

会话状态机可以画成：

```
            CreateSession
       (disabled? -> 失败)
                 │
                 ▼
        ┌─────────────────┐    GetSession     ┌──────────────┐
        │  存活 (in map)   │ ───(刷新时间戳)──> │  存活 (活跃)   │
        └─────────────────┘                   └──────────────┘
           │        │                              │
   DestroySession  │ 距上次活跃 > 5 min             │
           │        │ (CleanupStaleSessions)        │
           ▼        ▼                               │
        ┌─────────────────┐  <──────────────────────┘
        │  已移除 (erase)  │     StopService / CleanupAllSessions
        └─────────────────┘     会无差别清空所有会话
```

时间戳的维护靠两处 `Activate()`：

- `CreateSession` 里新建后立即 `session->Activate()`。
- `GetSession` 命中后也会 `session->Activate()`——**每次被访问就续命**。

#### 4.3.3 源码精读

生命周期常量与时间戳：

```cpp
class Session {
 public:
  static const int kLifeSpan = 5 * 60;  // seconds
  ...
  time_t last_active_time() const { return last_active_time_; }
 private:
  time_t last_active_time_ = 0;
};
void Session::Activate() { last_active_time_ = time(NULL); }
```

[service.h:33](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.h#L33) 与 [service.cc:30-32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.cc#L30-L32) — 中文说明：每个 Session 记一个 `last_active_time_`，`Activate()` 把它更新为当前 Unix 时间（秒）。

销毁与过期回收：

```cpp
bool Service::DestroySession(SessionId session_id) {
  auto it = sessions_.find(session_id);
  if (it == sessions_.end()) return false;
  sessions_.erase(it);
  return true;
}

void Service::CleanupStaleSessions() {
  time_t now = time(NULL);
  for (auto it = sessions_.begin(); it != sessions_.end();) {
    if (it->second && it->second->last_active_time() < now - Session::kLifeSpan)
      sessions_.erase(it++);
    else
      ++it;
  }
}
```

[service.cc:120-143](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.cc#L120-L143) — 中文说明：`DestroySession` 是「点名删除」；`CleanupStaleSessions` 是「批量扫地」——遍历所有会话，凡是 `last_active_time` 落后于 `now - 5min` 的统统 `erase`。注意边遍历边删除时用 `erase(it++)` 这个经典写法，避免迭代器失效。

全部清空：

```cpp
void Service::CleanupAllSessions() { sessions_.clear(); }
```

[service.cc:145-147](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.cc#L145-L147) — 中文说明：一把清空。它被 `StopService` 和 `RimeSyncUserData`（同步用户数据前先清场）调用。

谁触发过期回收？看 C API 这一层：

```cpp
RIME_DEPRECATED void RimeCleanupStaleSessions() {
  Service::instance().CleanupStaleSessions();
}
```

[rime_api_impl.h:161-163](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L161-L163) — 中文说明：过期回收不是自动定时器，而是**由前端主动调用** `cleanup_stale_sessions`。前端通常在空闲时（比如收到系统空闲通知）调一次。

#### 4.3.4 代码实践

**实践目标**：把四个生命周期方法串成一条可观测的链。

**操作步骤**：

1. 在 [service.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.cc#L85-L147) 依次阅读 `CreateSession` → `GetSession` → `DestroySession` → `CleanupStaleSessions`，记下每个方法是否会调用 `Activate()`。
2. 对照 [rime_api_impl.h:149-167](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L149-L167)，确认 `RimeCreateSession` / `RimeDestroySession` / `RimeCleanupStaleSessions` / `RimeCleanupAllSessions` 一一对应到 Service 方法。

**需要观察的现象**：`Create` 与 `Get` 都续命（`Activate`），`Destroy` 与 `CleanupStale` 不续命只删除。

**预期结果**：一张「方法 → 是否刷新时间戳」的表：Create=是，Get=是，Destroy=否（直接删），CleanupStale=否（按旧时间戳判断删除）。

#### 4.3.5 小练习与答案

**练习 1**：`CleanupStaleSessions` 用 `map` 而不是 `unordered_map`，有什么好处？

**参考答案**：过期回收需要遍历，`map` 的迭代顺序按 key（地址值）稳定，行为可预测；本场景对查找性能要求不高（会话数量通常很少），`map` 足够。

**练习 2**：如果把 `kLifeSpan` 改成 0，会发生什么？

**参考答案**：`now - 0 == now`，所有 `last_active_time_ < now` 的会话都会被回收——即除了「正好在这一秒被 Activate」的会话外全部被清掉，相当于每次清理都清空。

---

### 4.4 Session 与 Engine 的持有关系

#### 4.4.1 概念说明

Session 自己不处理按键逻辑，它**持有**一个 `Engine`，按键转发给引擎，状态从引擎读取。这是典型的「外观/门面」关系：Session 是面向 C API 的薄壳，Engine 才是干活的。

所有权上，Session 用 `the<Engine>`（`unique_ptr`）独占一个 Engine——Session 销毁，Engine 立刻跟着销毁。这保证了「一个会话一份引擎状态」的隔离。

#### 4.4.2 核心流程

```
   Session
   ├─ the<Engine> engine_;        ← 独占所有权
   │    Engine
   │    ├─ the<Schema>  schema_;
   │    └─ the<Context> context_;
   ├─ time_t last_active_time_;
   └─ string commit_text_;        ← 缓存引擎提交的文字

   Session::ProcessKey(key)  ──>  engine_->ProcessKey(key)
   Session::context()        ──>  engine_->active_engine()->context()
   Session::schema()         ──>  engine_->active_engine()->schema()
```

注意 `context()` / `schema()` 走的是 `active_engine()` 而非 `engine_` 本身——这是因为引擎内部可能临时切到「切换器引擎」（Switcher，u9-l4），`active_engine()` 会返回当前真正生效的那个。

#### 4.4.3 源码精读

Session 持有与构造：

```cpp
class Session {
 private:
  the<Engine> engine_;
  time_t last_active_time_ = 0;
  string commit_text_;
};

Session::Session() {
  engine_.reset(Engine::Create());
  engine_->sink().connect([this](auto text) { OnCommit(text); });
  SessionId session_id = reinterpret_cast<SessionId>(this);
  engine_->message_sink().connect([session_id](auto type, auto value) {
    Service::instance().Notify(session_id, type, value);
  });
}
```

[service.h:51-53](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.h#L51-L53) 与 [service.cc:17-24](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.cc#L17-L24) — 中文说明：`the<Engine> engine_` 表示 Session 独占引擎。构造时 `Engine::Create()` 造出引擎（实际返回 `ConcreteEngine`），并**连接两条信号**：引擎的提交信号 `sink()` 接到 `Session::OnCommit`，引擎的消息信号 `message_sink()` 接到 `Service::Notify`（带本会话 id）。

按键转发与状态透传：

```cpp
bool Session::ProcessKey(const KeyEvent& key_event) {
  return engine_->ProcessKey(key_event);
}
Context* Session::context() const {
  return engine_ ? engine_->active_engine()->context() : NULL;
}
Schema* Session::schema() const {
  return engine_ ? engine_->active_engine()->schema() : NULL;
}
```

[service.cc:26-28](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.cc#L26-L28) 与 [service.cc:59-65](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.cc#L59-L65) — 中文说明：`ProcessKey` 把按键原样交给引擎；`context()`/`schema()` 从 `active_engine()` 取当前生效引擎的状态，引擎不存在时返回 `NULL`。

引擎工厂与持有对象：

```cpp
class Engine : public Messenger {
  ...
  CommitSink& sink() { return sink_; }
  Engine* active_engine() { return active_engine_ ? active_engine_ : this; }
  RIME_DLL static Engine* Create();
 protected:
  the<Schema> schema_;
  the<Context> context_;
  CommitSink sink_;
  Engine* active_engine_ = nullptr;
};
Engine* Engine::Create() { return new ConcreteEngine; }
Engine::Engine() : schema_(new Schema), context_(new Context) {}
```

[engine.h:30-45](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.h#L30-L45) 与 [engine.cc:60-64](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L60-L64) — 中文说明：`Engine::Create()` 返回一个 `ConcreteEngine`（engine.cc 内的私有子类）；`Engine` 基类自己持有 `schema_` 与 `context_`，并提供提交信号 `sink_`。于是「Session → Engine → Schema/Context」三层独占持有链成立。

#### 4.4.4 代码实践

**实践目标**：画出 Session 与 Engine 的持有关系图（本讲的指定实践）。

**操作步骤**：

1. 阅读上述四段代码，确认所有权箭头：`Session --owns--> Engine --owns--> {Schema, Context}`。
2. 在图上标出两条**信号**（注意是「观察」而非「拥有」）：`Engine::sink_ ──observed by──> Session::OnCommit`、`Engine::message_sink_ ──observed by──> Service::Notify`。
3. 在图上标出 `active_engine()` 这条「可能旁路到 Switcher」的访问路径。

**需要观察的现象**：所有权（实线）自上而下独占；信号订阅（虚线）自下而上回调。两者方向相反。

**预期结果**：得到一张类似下面的小图——

```
   Service (singleton)
     │ owns (map)
     ▼
   Session ──owns──> Engine ──owns──> Schema
     │                 │   └──owns──> Context
     │                 │
     │  <──signal────  sink()         (提交文字)
     │  <──signal────  message_sink() (通知消息) ──> Service::Notify
     ▼
   process_key / get_commit / set_option  (C API 入口)
```

#### 4.4.5 小练习与答案

**练习 1**：为什么 Session 用 `the<Engine>`（`unique_ptr`）而不是 `an<Engine>`（`shared_ptr`）？

**参考答案**：一个 Session 恰好对应一个 Engine，是严格的 1:1 独占关系，没有共享需求；`unique_ptr` 更轻、语义更明确，且保证 Session 析构时 Engine 一定被释放。

**练习 2**：`context()` 为什么要走 `active_engine()` 而不直接用 `engine_->context()`？

**参考答案**：当用户呼出方案切换器（Switcher）时，引擎会把 `active_engine_` 指向切换器子引擎；此时 C API 想读的「当前候选/状态」应该来自切换器，而不是底层拼音引擎，所以要用 `active_engine()` 取真正生效的那个。

---

### 4.5 提交回写与通知回调：两条信号链

#### 4.5.1 概念说明

Session 在 Engine 与前端之间，还承担**两条信号传递**职责：

1. **提交链（Commit）**：用户选定了候选词，引擎把要「上屏」的文字通过 `sink_` 发出；Session 接住，追加到自己的 `commit_text_` 缓冲；前端随后用 `get_commit` 把它取走。
2. **通知链（Notify）**：引擎或部署器产生事件（如切换了方案、改变了选项、部署完成），通过 `message_sink_` 发出；Session 转交给 `Service::Notify`，Service 再调用前端注册的 `notification_handler`。

这两条链的终点都是前端，但走的路径不同：提交是「拉」模型（前端主动 `get_commit`），通知是「推」模型（引擎主动回调前端）。

#### 4.5.2 核心流程

```
【提交链 —— 拉模型】
  Engine 决定提交
     │ sink_(text)               (CommitSink 触发)
     ▼
  Session::OnCommit(text)        commit_text_ += text
     │
     ▼  (前端稍后调用)
  RimeGetCommit(id) ─> session->commit_text()
     │ 拷贝到 C 结构体
     └─> session->ResetCommitText()   (取走即清空)

【通知链 —— 推模型】
  Engine / Deployer 产生事件
     │ message_sink_(type, value)
     ▼
  Session 构造时连接的 lambda ─> Service::instance().Notify(session_id, type, value)
     │ std::lock_guard(mutex_) 保护
     ▼
  notification_handler_(id, type, value)   (前端回调)
```

#### 4.5.3 源码精读

提交链：Session 接住引擎的 sink：

```cpp
void Session::OnCommit(const string& commit_text) {
  commit_text_ += commit_text;
}
```

[service.cc:55-57](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.cc#L55-L57) — 中文说明：`OnCommit` 把引擎发来的提交文字**累加**到 `commit_text_`（用 `+=` 而非 `=`，因为一次操作可能分多次提交）。这是 Session 缓冲提交的落点。

前端如何取走提交（注意是「C 边界」代码，要自己管理内存）：

```cpp
RIME_DEPRECATED Bool RimeGetCommit(RimeSessionId session_id, RimeCommit* commit) {
  ...
  an<Session> session(Service::instance().GetSession(session_id));
  if (!session) return False;
  const string& commit_text(session->commit_text());
  if (!commit_text.empty()) {
    commit->text = new char[commit_text.length() + 1];
    std::strcpy(commit->text, commit_text.c_str());
    session->ResetCommitText();
    return True;
  }
  return False;
}
```

[rime_api_impl.h:309-325](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L309-L325) — 中文说明：`RimeGetCommit` 拿到 Session 的 `commit_text_`，复制到一个堆分配的 C 字符串（`new char[]`）返回给前端，**然后立刻 `ResetCommitText()` 清空缓冲**——这就是 u1-l5 强调的「`get_*` 与 `free_*` 必须成对调用」的根源：前端拿到这个指针后要负责 `RimeFreeCommit` 释放。

通知链：Session 构造时连接引擎消息信号到 Service：

```cpp
SessionId session_id = reinterpret_cast<SessionId>(this);
engine_->message_sink().connect([session_id](auto type, auto value) {
  Service::instance().Notify(session_id, type, value);
});
```

[service.cc:20-23](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.cc#L20-L23) — 中文说明：捕获 `session_id`（值捕获，避免 `this` 悬垂），把引擎的每条消息转发给 `Service::Notify`，并带上本会话 id，让前端知道是哪个会话发的事件。

`MessageSink` 的类型定义：

```cpp
class Messenger {
 public:
  using MessageSink =
      signal<void(const string& message_type, const string& message_value)>;
  MessageSink& message_sink() { return message_sink_; }
};
```

[messenger.h:14-23](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/messenger.h#L14-L23) — 中文说明：`Engine` 继承自 `Messenger`，于是自带一个 `message_sink_` 信号。`message_type` 形如 `"schema"`/`"option"`/`"property"`（见 engine.cc 里 `message_sink_("option", msg)` 等处）。

Service 如何把消息推给前端：

```cpp
void Service::Notify(SessionId session_id,
                     const string& message_type,
                     const string& message_value) {
  if (notification_handler_) {
    std::lock_guard<std::mutex> lock(mutex_);
    notification_handler_(session_id, message_type.c_str(),
                          message_value.c_str());
  }
}
```

[service.cc:157-165](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.cc#L157-L165) — 中文说明：若有前端注册的 `notification_handler_`，就在 `mutex_` 保护下调用它。加锁是因为消息可能来自部署器的后台线程，而 handler 通常操作前端 UI（必须在主线程安全调用）。

前端如何注册这个 handler：

```cpp
// rime_api_impl.h:42-48
Service::instance().SetNotificationHandler(
    [context_object, handler](auto id, auto type, auto value) {
      handler(context_object, id, type, value);
    });
```

[rime_api_impl.h:40-49](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L40-L49) — 中文说明：`RimeSetNotificationHandler` 把前端传来的 C 函数指针 `handler` 和上下文 `context_object` 包成一个 C++ 闭包，存进 `Service::notification_handler_`。

> 一个细节：部署器（Deployer）也持有 `message_sink_`，Service 构造时把它接到 `Notify(0, ...)`——部署事件用 `session_id == 0` 表示「不属于任何会话」（全局事件）。见 [service.cc:67-70](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.cc#L67-L70)。

#### 4.5.4 代码实践

**实践目标**：用 console 程序实地观察这两条链。

**操作步骤**：

1. 先确认构建产物可用（参考 u1-l2 的构建步骤）。运行 `tools/rime_api_console`。
2. 在 console 里输入一段拼音并选词上屏。观察终端输出：
   - **提交链**：上屏的文字对应 `get_commit` 的返回值（console 会打印提交内容）。
   - **通知链**：执行 `select schema <id>` 切换方案时，会触发 `message_type == "schema"` 的通知；切换选项（如 ascii_mode）会触发 `"option"` 通知——这些就是 console 注册的 `on_message` 回调收到的。
3. 在源码侧核对：console 的 `on_message`（u1-l5 讲过）正是通过 `set_notification_handler` 注册的，对应 [rime_api_impl.h:40-49](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L40-L49)。

**需要观察的现象**：上屏文字来自「拉」模型（前端主动取）；方案/选项变化的通知来自「推」模型（引擎主动回调）。

**预期结果**：能在 console 输出里分别指认哪些行是提交链产物、哪些行是通知链产物。若本地无法运行，标注「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：`OnCommit` 用 `+=` 累加提交文字，为什么不是直接覆盖？

**参考答案**：一次「确定」操作里引擎可能分多次 `sink_(...)`（例如标点和汉字分开提交），累加才能保证 `commit_text_` 收齐完整文本；`RimeGetCommit` 取走后 `ResetCommitText` 再清空，进入下一轮。

**练习 2**：`Notify` 为什么要加 `mutex_`，而 `CreateSession`/`GetSession` 没有加？

**参考答案**：通知可能来自部署器的**后台维护线程**，而 `notification_handler_` 可能同时被替换（前端重新 `set_notification_handler`）；两者并发访问同一 `std::function` 不安全，故加锁。`sessions_` 的增删查通常发生在前端的单一调用线程，源码未对其加锁（实际多线程安全由调用方约定保证）——这是一个值得在阅读时留意的边界假设。

**练习 3**：部署器事件为什么用 `session_id == 0`？

**参考答案**：部署（编译词典、同步数据）是进程级操作，不属于任何输入会话；用 `0`（`kInvalidSessionId`）作为「全局/无会话」的哨兵，前端收到 `id==0` 时就知道这是部署进度而非某个输入框的事件。

## 5. 综合实践

把本讲的内容串起来，完成下面这个「**会话生命周期 + 持有关系**」的综合追踪任务：

1. **画一张完整的对象关系图**，要求包含：`Service`（单例）、`SessionMap`、若干 `Session`、每个 `Session` 持有的 `Engine`/`Schema`/`Context`、前端的 `notification_handler`。用实线标「所有权」，虚线标「信号订阅」。
2. **用箭头标注一次完整的会话旅程**：`RimeInitialize`（StartService）→ `RimeCreateSession`（造 Session + Engine）→ `RimeProcessKey`（GetSession 续命 + 转发按键）→ 引擎提交 → `RimeGetCommit`（取走文字 + Reset）→ `RimeDestroySession`（erase）。
3. **在图上标出三个「禁用闸门」**：`started_==false`、`IsMaintenanceMode()`、会话过期（`>5min`）分别在哪一步生效。
4. **（可选，待本地验证）** 用 `rime_api_console` 验证：故意不调用 `select schema` 直接输入，观察通知链是否安静；切换方案后观察通知链是否出现 `schema` 事件。

完成后，你应该能用一句话向别人解释：**「前端拿着一个 id，Service 用 map 找到 Session，Session 把按键交给它独占的 Engine，引擎把提交文字和事件分别通过拉、推两条链送回前端。」**

## 6. 本讲小结

- **会话 = 隔离的输入上下文**：每个输入焦点一个 Session，`SessionId` 取自 Session 对象的地址（`uintptr_t`），`0` 表示无效。
- **Service 是单例中枢**：用 `SessionMap`（`map<id, shared_ptr<Session>>`）统一管理会话，`disabled()` 在「未启动」或「维护中」时拒绝会话操作。
- **生命周期四件套**：`CreateSession`（造 + 续命）、`GetSession`（查 + 续命）、`DestroySession`（点名删）、`CleanupStaleSessions`（按 5 分钟过期批量删）；`CleanupAllSessions` 用于停服/同步前清场。
- **Session 独占 Engine**：`the<Engine>` 保证 1:1 所有权，按键转发给引擎，状态从 `active_engine()` 透传（支持切换器旁路）。
- **提交链是「拉」模型**：引擎 `sink_` → Session `OnCommit` 累加进 `commit_text_` → 前端 `get_commit` 取走并 `Reset`，C 字符串由前端 `free`。
- **通知链是「推」模型**：引擎 `message_sink_` → `Service::Notify`（加锁）→ 前端 `notification_handler`；部署器事件用 `session_id==0`。

## 7. 下一步学习建议

本讲把「会话 → 引擎」的骨架立起来了，但 Engine 内部到底装了什么、按键进去之后发生什么，都还没展开。建议按这个顺序继续：

1. **u2-l3 Schema**：先认识 Engine 持有的另一半——`Schema`（输入方案），它是引擎装配流水线的「图纸」。
2. **u2-l4 Engine 骨架**：回到 `engine.h`/`engine.cc`，看 `ConcreteEngine` 如何根据方案装配出 Processor/Segmentor/Translator/Filter 四类组件。
3. **u3-l1 Context**：进入引擎持有的 `Context`，它是按键流水线读写状态的中央容器，也是本讲多次出现的 `context()` 的真正落点。

想提前感受整体流程，可以回头重跑 `rime_api_console`（u1-l5），这次带着「会话/引擎/提交链/通知链」的视角去看它的输出，会有完全不同的理解深度。
