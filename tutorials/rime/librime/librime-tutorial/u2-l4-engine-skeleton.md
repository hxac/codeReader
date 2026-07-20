# Engine：引擎骨架

## 1. 本讲目标

本讲聚焦于 librime 运行时的「中枢对象」**Engine（引擎）**。学完本讲，你应当能够：

- 说清楚 `Engine` 抽象基类持有哪些核心对象，以及它为什么是抽象的。
- 解释 `CommitSink` 与 `MessageSink` 这两条信号通道的区别，以及它们如何把「提交文本」「状态通知」传递给上层（Session/Service）。
- 理解 `Engine::Create()` 这个静态工厂为什么要返回一个隐藏的 `ConcreteEngine`。
- 看懂引擎是如何**根据方案（Schema）的 `engine` 配置**，把四类组件（Processor / Segmentor / Translator / Filter）装配进容器的。

本讲是单元 u2「核心运行时对象」的收尾。在前几讲里，我们已经认识了 `KeyEvent`（按键）、`Service`/`Session`（会话）、`Schema`（方案）。本讲把这三者粘合在一起：**Session 持有一个 Engine，Engine 持有当前 Schema 与输入状态 Context，并把按键和提交结果在两者之间转发**。理解了 Engine 的骨架，下一篇 u3 才能进入 Context 内部的候选生成机制。

## 2. 前置知识

阅读本讲前，请确认你已经理解以下概念（均来自前置讲义）：

- **按键的内部表示**（u2-l1）：一次按键 = `keycode` + `modifier`，引擎的入口是处理一个 `KeyEvent`。
- **Service / Session**（u2-l2）：每个输入焦点有一个 `Session`，它独占一个 `Engine`；Session 与上层之间通过「拉模型」（`get_commit`）和「推模型」（`notification_handler`）两种方式通信。
- **Schema**（u2-l3）：方案 = `schema_id` + `Config` 句柄；方案文件 `*.schema.yaml` 顶层有一个 `engine:` 块，列出四张组件清单。
- **C++ 基础**：抽象基类、虚函数、智能指针。librime 在 `common.h` 里给智能指针起了别名（见 u1-l3）：
  - `the<T>` = `std::unique_ptr<T>`（独占所有权）
  - `an<T>` = `of<T>` = `std::shared_ptr<T>`（共享所有权）
  - `New<T>(...)` = `std::make_shared<T>(...)`
  - `signal` = `boost::signals2::signal`（Boost 信号槽，用于「观察者模式」式回调）

> 一个直观比喻：把 Engine 想象成一条「输入法流水线」的车间主任。它手上有两样东西——**一份图纸（Schema，告诉它该装哪些机器）**和**一张工作台（Context，存放当前正在加工的输入）**。流水线本身（Processor → Segmentor → Translator → Filter）的具体机器清单写在 Schema 里，本讲只看「车间主任如何把机器摆上流水线」，机器内部如何运转留给 u6。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用途 |
| --- | --- | --- |
| [src/rime/engine.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.h) | `Engine` 抽象基类的声明 | 引擎的对外契约：成员、虚函数、信号、工厂 |
| [src/rime/engine.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc) | `ConcreteEngine` 的实现 | 工厂 `Engine::Create`、信号连接、组件装配 |
| [src/rime/messenger.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/messenger.h) | `Messenger` 混入类，提供 `message_sink_` | 第二条信号通道「通知」 |
| [src/rime/service.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.cc) | `Session` 如何连接引擎的两条信号 | 验证信号「上传」给上层的真实落点 |
| [src/rime/ticket.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/ticket.h) | `Ticket` 结构体 | 装配组件时传递的上下文（engine / klass / name_space） |
| [data/minimal/luna_pinyin.schema.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml) | 默认拼音方案 | 对照 `engine:` 配置块，验证四类容器被填充成什么 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **Engine 抽象基类与两个核心对象**（对应 `engine.h`）
2. **Messenger 与两条信号通道**（对应 `messenger.h` + `engine.h`）
3. **Engine::Create 工厂与 ConcreteEngine**（对应 `engine.cc` 上半部分）
4. **方案驱动装配：四类组件容器**（对应 `engine.cc` 下半部分）

### 4.1 Engine 抽象基类与两个核心对象

#### 4.1.1 概念说明

`Engine` 是 librime 对「输入法引擎」这一概念的**抽象基类**。它不包含任何具体的拼音、仓颉逻辑，只规定了一个引擎**必须持有什么、必须能响应什么**。这正是「换方案即换输入法，引擎不变」的根基——抽象接口稳定，具体行为由装配进来的组件决定。

一个 `Engine` 持有两个核心对象：

- **`schema_`（Schema）**：当前输入方案，是「装配图纸」。
- **`context_`（Context）**：当前输入状态（原始输入串、光标、候选组合等），是「工作台」。

此外它还持有一个 `CommitSink` 信号 `sink_`（见 4.2），以及一个 `active_engine_` 指针（用于切换器旁路，见 4.1.3）。

为什么是抽象的？因为「引擎怎么处理按键、怎么组合候选」有不同实现，基类只给虚函数占位，真正的实现在派生类 `ConcreteEngine` 里（见 4.3）。

#### 4.1.2 核心流程

Engine 基类对外暴露的「能力」可以归纳为四组：

```text
能力分组        基类提供的虚函数（默认空实现）   典型调用方
─────────────  ──────────────────────────────  ────────────────
处理按键        ProcessKey(key)  -> bool         Session::ProcessKey
切换方案        ApplySchema(schema)              Session::ApplySchema
直接提交文本     CommitText(text)                 Processor / Editor
重算候选        Compose(ctx)                     Context 更新回调
```

这四个虚函数在基类里都有**默认空实现**（`return false` 或 `{}`），唯一例外是 `CommitText` 默认就会触发提交信号 `sink_(text)`。也就是说，哪怕派生类什么都不做，引擎的「提交通道」也是通的。

成员读取通过三个 getter：`schema()`、`context()`、`sink()`。它们都是 `protected` 成员的只读访问器。

#### 4.1.3 源码精读

先看基类声明与它的成员：

[src/rime/engine.h:L20-L23](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.h#L20-L23) —— `Engine` 公开继承自 `Messenger`（因此拥有 `message_sink_`，见 4.2），并定义了提交信号的类型别名 `CommitSink`：

```cpp
class Engine : public Messenger {
 public:
  using CommitSink = signal<void(const string& commit_text)>;
```

> 注意：`signal` 在 librime 里是 `boost::signals2::signal` 的别名（见 `common.h`）。`CommitSink` 是一个「信号」，可以连接（`connect`）多个回调函数，触发（`operator()`）时所有回调都会被调用——这就是观察者模式。

[src/rime/engine.h:L25-L28](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.h#L25-L28) —— 四个虚函数都有默认实现，`CommitText` 默认就触发提交信号：

```cpp
virtual bool ProcessKey(const KeyEvent& key_event) { return false; }
virtual void ApplySchema(Schema* schema) {}
virtual void CommitText(string text) { sink_(text); }
virtual void Compose(Context* ctx) {}
```

[src/rime/engine.h:L30-L35](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.h#L30-L35) —— 三个 getter，以及 `active_engine_` 的旁路逻辑：

```cpp
Schema* schema() const { return schema_.get(); }
Context* context() const { return context_.get(); }
CommitSink& sink() { return sink_; }

Engine* active_engine() { return active_engine_ ? active_engine_ : this; }
void set_active_engine(Engine* engine = nullptr) { active_engine_ = engine; }
```

> `active_engine()` 的设计：正常情况下返回 `this`（自己）；当**方案切换器 Switcher** 接管时，`active_engine_` 指向切换器内部的引擎，此时上层（Session）读写 `context()` / `schema()` 时会落到切换器引擎上——这就是 u2-l2 提到的「切换器旁路」。

[src/rime/engine.h:L37-L46](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.h#L37-L46) —— 工厂方法是唯一的公开构造途径，构造函数本身是 `protected`：

```cpp
RIME_DLL static Engine* Create();

protected:
Engine();
the<Schema> schema_;
the<Context> context_;
CommitSink sink_;
Engine* active_engine_ = nullptr;
```

> 把构造函数设为 `protected`、只留一个静态 `Create()` 工厂，是为了强制外部不能直接 `new Engine()`，而必须走工厂（见 4.3）。`RIME_DLL` 是跨平台导出宏（在 Windows 下展开为 `__declspec(dllexport/import)`，Linux/macOS 下为空）。

基类构造函数的实现非常简单——直接造一个默认 Schema 和一个空 Context：

[src/rime/engine.cc:L64-L69](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L64-L69):

```cpp
Engine::Engine() : schema_(new Schema), context_(new Context) {}
```

> `new Schema` 调用默认构造，会加载 `.default`（即 `default.yaml`），这是 u2-l3 讲过的。所以一个「光秃秃的 Engine」天生就带了一份默认方案和空状态——但此时还没有任何处理按键的组件，真正的装配发生在派生类 `ConcreteEngine` 的构造体里（4.4）。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：确认 Engine 基类持有哪些成员、它们的类型别名含义。
2. **操作步骤**：
   - 打开 [src/rime/engine.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.h)。
   - 找到 `protected:` 段（约 L42-L45），列出四个成员。
   - 对照 [src/rime/common.h:L57-L62](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/common.h#L57-L62)，把 `the<Schema>` / `the<Context>` 翻译成标准库类型。
3. **需要观察的现象**：`schema_` 和 `context_` 用的是独占指针 `the<>`（`unique_ptr`），而后面装配出的组件容器用的是共享指针 `an<>`/`of<>`（`shared_ptr`）。
4. **预期结果**：
   - `the<Schema> schema_;` → `std::unique_ptr<Schema>`（引擎独占方案）
   - `the<Context> context_;` → `std::unique_ptr<Context>`（引擎独占状态）
   - `CommitSink sink_;` → 一个 Boost 信号（提交通道）
   - `Engine* active_engine_ = nullptr;` → 裸指针，默认空（旁路用）
5. 待本地验证（仅阅读，无需运行）。

#### 4.1.5 小练习与答案

**练习 1**：基类的 `CommitText(string)` 默认实现是 `sink_(text)`。如果一个派生类完全不重写 `CommitText`，调用它会怎样？

**答案**：会直接触发提交信号 `sink_`，把 `text` 广播给所有连接到 `sink()` 的回调（即 Session 的 `OnCommit`）。换句话说，「提交通道」在基类层就已经是可用的。

**练习 2**：为什么 `schema_` 用 `unique_ptr`（`the<>`）而不是 `shared_ptr`？

**答案**：因为一个引擎在任一时刻只对应一个当前方案，方案的生命周期完全由引擎独占管理，不存在多个持有者共享同一个 Schema 对象的需求，独占语义更准确、开销也更低。

---

### 4.2 Messenger 与两条信号通道

#### 4.2.1 概念说明

Engine 与「上层」（Session、Service、最终到前端）之间需要交换两类信息：

- **提交文本**：用户决定上屏的文字（如「你好」）。这是引擎的「产物」，用 `CommitSink` 传递。
- **状态通知**：方案切换、开关变化等事件（如切到 `ascii_mode`、`schema` 变了）。这是引擎的「广播」，用 `MessageSink` 传递。

为了避免把两件事耦合在一个信号里，librime 用了两个独立的信号槽。`CommitSink` 是 `Engine` 自己定义的；`MessageSink` 则来自一个**混入类（mixin）`Messenger`**——Engine 通过 `class Engine : public Messenger` 把它「继承」过来。

> 为什么要单独抽一个 `Messenger` 基类？因为「能发通知」这个能力不仅 Engine 需要，`Deployer`（部署器）也需要。把 `message_sink_` 抽到 `Messenger` 里，Engine 和 Deployer 就能复用同一套通知机制。这正是 u2-l2 里说的「部署器事件也走 `Notify`」的根源。

#### 4.2.2 核心流程

两条信号的流向（箭头表示数据流方向）：

```text
【提交通道 CommitSink —— 拉模型的产物】
  Engine.sink_  ──触发──▶  Session::OnCommit(text)  ──累加──▶  commit_text_
                                                              （前端用 get_commit 取走）

【通知通道 MessageSink —— 推模型的事件】
  Engine.message_sink_  ──触发──▶  Service::Notify(session_id, type, value)
                                  ──调用──▶  前端的 notification_handler
```

- `CommitSink` 的回调签名是 `void(const string& commit_text)`，只带一个文本参数。
- `MessageSink` 的回调签名是 `void(const string& message_type, const string& message_value)`，带「类型 + 值」两个参数（如 `("schema", "luna_pinyin/朙月拼音")`、`("option", "ascii_mode")`、`("option", "!ascii_mode")` 表示关闭）。

两条信号都是**由 Engine 主动触发、由上层连接回调**——Engine 不关心谁在听，上层（Session）在创建引擎时把回调「挂」上去。

#### 4.2.3 源码精读

先看 `Messenger` 这个混入类有多薄——它只持有一个信号和一个访问器：

[src/rime/messenger.h:L14-L23](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/messenger.h#L14-L23):

```cpp
class Messenger {
 public:
  using MessageSink =
      signal<void(const string& message_type, const string& message_value)>;

  MessageSink& message_sink() { return message_sink_; }

 protected:
  MessageSink message_sink_;
};
```

> `message_sink()` 返回信号的引用，外部用它来 `connect` 回调。`message_sink_` 在 `protected` 段，所以 Engine 的派生类可以直接 `message_sink_("schema", ...)` 来触发通知。

Engine 触发通知的真实例子——切换方案时发 `schema` 事件、开关变化时发 `option` 事件：

[src/rime/engine.cc:L130-L142](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L130-L142) —— `OnOptionUpdate` 把开关变化转成 `option` 通知：

```cpp
void ConcreteEngine::OnOptionUpdate(Context* ctx, const string& option) {
  ...
  bool option_is_on = ctx->get_option(option);
  string msg(option_is_on ? option : "!" + option);
  message_sink_("option", msg);   // 触发通知通道
}
```

> 约定：值带 `!` 前缀表示该开关被**关闭**（如 `"!ascii_mode"`），不带则表示开启。前端据此刷新状态栏图标。

[src/rime/engine.cc:L284-L294](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L284-L294) —— `ApplySchema` 装配完成后发 `schema` 通知：

```cpp
void ConcreteEngine::ApplySchema(Schema* schema) {
  ...
  message_sink_("schema", schema_->schema_id() + "/" + schema_->schema_name());
}
```

现在验证「上层真的把回调挂到了这两条信号上」。看 Session 的构造函数：

[src/rime/service.cc:L17-L24](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.cc#L17-L24):

```cpp
Session::Session() {
  engine_.reset(Engine::Create());
  engine_->sink().connect([this](auto text) { OnCommit(text); });           // 提交通道
  SessionId session_id = reinterpret_cast<SessionId>(this);
  engine_->message_sink().connect([session_id](auto type, auto value) {     // 通知通道
    Service::instance().Notify(session_id, type, value);
  });
}
```

> 这三行就是 u2-l2 所说两条信号链的真实落点：
> - **提交链（拉模型）**：`sink()` 连到 `Session::OnCommit`，把文本累加进 `commit_text_`，等前端 `get_commit` 来取（见 [src/rime/service.cc:L55-L57](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.cc#L55-L57)）。
> - **通知链（推模型）**：`message_sink()` 连到 `Service::Notify`，立即推给前端的 `notification_handler`。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：追踪两条信号从 Engine 触发到前端接收的完整路径。
2. **操作步骤**：
   - 在 [src/rime/engine.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc) 里搜索 `sink_(` 和 `message_sink_(`，统计它们各出现在哪些函数。
   - 在 [src/rime/service.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.cc) 里确认这两个信号的回调分别连到了哪里。
3. **需要观察的现象**：
   - `sink_(` 出现在 `Engine::CommitText`（基类）和 `ConcreteEngine::CommitText` / `ConcreteEngine::OnCommit`。
   - `message_sink_(` 出现在 `OnOptionUpdate`、`OnPropertyUpdate`、`ApplySchema`。
4. **预期结果**：能画出两条独立的数据流图——提交走 `OnCommit→commit_text_→get_commit`，通知走 `Notify→notification_handler`。
5. 待本地验证（仅阅读，无需运行）。

#### 4.2.5 小练习与答案

**练习 1**：`CommitSink` 和 `MessageSink` 的回调签名有何不同？为什么通知信号要带两个参数？

**答案**：`CommitSink` 是 `void(const string& commit_text)`，只带提交文本；`MessageSink` 是 `void(const string& message_type, const string& message_value)`，带类型和值。因为通知有多种（`schema`/`option`/`property`/`deploy` 等），用一个 `type` 区分种类、用一个 `value` 携带具体内容，比给每种通知单独定义一个信号更经济、也方便上层用一个回调统一分发。

**练习 2**：`Messenger` 为什么设计成单独的基类，而不是直接把 `message_sink_` 写进 `Engine`？

**答案**：为了让 `Deployer`（部署器）也能复用同一套通知机制。`Deployer` 同样需要把部署进度/结果通知给前端（u2-l2 提到部署器事件以 `session_id==0` 标记）。把通知能力抽成 `Messenger` 混入类，Engine 和 Deployer 都继承它即可共享，避免重复代码。

---

### 4.3 Engine::Create 工厂与 ConcreteEngine

#### 4.3.1 概念说明

回到 4.1 留的问题：`Engine` 的构造函数是 `protected`，外部怎么造引擎？答案是唯一的静态工厂 `Engine::Create()`。它返回一个 `Engine*`，但实际 `new` 出来的是一个派生类 `ConcreteEngine`。

这是经典的**抽象基类 + 工厂方法 + 隐藏实现**模式：

- 对外（头文件 `engine.h`）只暴露抽象的 `Engine` 接口，调用方依赖抽象。
- 真正的实现 `ConcreteEngine` 被藏在 `engine.cc` 里，不暴露在头文件中。
- 这样**将来若要换一种引擎实现**（比如实验性的新引擎），只需改 `Engine::Create()` 内部 `new` 什么，所有调用方（Session）无需重新编译。

`ConcreteEngine` 重写了基类的全部四个虚函数，并新增了一组**组件容器**（Processor/Segmentor/Translator/Filter 等）和若干**信号回调**。

#### 4.3.2 核心流程

引擎从「被创建」到「就绪」的构造流程：

```text
Session::Session()
   └─ Engine::Create()
        └─ new ConcreteEngine
             ├─ [先] Engine() 基类构造  ──▶ schema_ = new Schema(.default)
             │                             context_ = new Context
             ├─ [后] ConcreteEngine() 构造体：
             │     1. 把 context 的 5 个 Notifier 连到自己的回调
             │     2. 创建 Switcher，恢复用户保存的开关
             │     3. InitializeComponents()  ──▶ 按 schema 装四类组件
             │     4. InitializeOptions()     ──▶ 重置方案里的开关默认值
             └─ 返回 Engine* 给 Session
```

注意 C++ 的构造顺序：**基类先构造，派生类构造体后执行**。所以当 `ConcreteEngine` 的构造体开始跑时，`schema_` 和 `context_` 已经是非空的了，可以直接用。

构造完成后，按键处理的入口是 `ProcessKey`，它驱动 `processors_` 容器：

```text
ProcessKey(key):
  for p in processors_:
     ret = p->ProcessKeyEvent(key)
     if ret == kAccepted: return true   // 有处理器吃掉了，提前返回
     if ret == kRejected: break         // 被明确拒绝，跳出主循环
  // 主循环没吃掉 → 记进 commit_history，再走 post_processors_
  for p in post_processors_: ...同上...
  // 仍没人处理 → 通知 unhandled_key
  context_->unhandled_key_notifier()(context_, key)
  return false
```

> `kAccepted`/`kRejected`/`kNoop` 的三态语义是流水线的核心协议，属于 u6-l1 的内容，本讲只需记住「ProcessKey 就是依次问每个 Processor」。

#### 4.3.3 源码精读

工厂方法只有一行——返回一个 `ConcreteEngine`：

[src/rime/engine.cc:L60-L62](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L60-L62):

```cpp
Engine* Engine::Create() {
  return new ConcreteEngine;
}
```

`ConcreteEngine` 是定义在 `engine.cc` 内部（不在头文件）的派生类，重写了全部四个虚函数，并持有六组容器：

[src/rime/engine.cc:L28-L56](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L28-L56):

```cpp
class ConcreteEngine : public Engine {
 public:
  ConcreteEngine();
  virtual bool ProcessKey(const KeyEvent& key_event);
  virtual void ApplySchema(Schema* schema);
  virtual void CommitText(string text);
  virtual void Compose(Context* ctx);
 ...
 protected:
  vector<of<Processor>> processors_;
  vector<of<Segmentor>> segmentors_;
  vector<of<Translator>> translators_;
  vector<of<Filter>> filters_;
  vector<of<Formatter>> formatters_;
  vector<of<Processor>> post_processors_;
  an<Switcher> switcher_;
};
```

> `vector<of<Processor>>` 就是 `vector<shared_ptr<Processor>>`。这六组容器里，**前四组**（processors/segmentors/translators/filters）正是 `engine.schema.yaml` 里 `engine:` 块的四张清单；后两组（formatters、post_processors）是固定装配的「形状」相关组件（`shape_formatter`、`shape_processor`），不在方案配置里。

`ConcreteEngine` 的构造体把 Context 的五个 Notifier 连到自己的回调，再装配组件：

[src/rime/engine.cc:L71-L93](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L71-L93):

```cpp
ConcreteEngine::ConcreteEngine() {
  LOG(INFO) << "starting engine.";
  // receive context notifications
  context_->commit_notifier().connect([this](Context* ctx) { OnCommit(ctx); });
  context_->select_notifier().connect([this](Context* ctx) { OnSelect(ctx); });
  context_->update_notifier().connect([this](Context* ctx) { OnContextUpdate(ctx); });
  context_->option_update_notifier().connect(
      [this](Context* ctx, const string& option) { OnOptionUpdate(ctx, option); });
  context_->property_update_notifier().connect(
      [this](Context* ctx, const string& property) { OnPropertyUpdate(ctx, property); });

  switcher_ = New<Switcher>(this);
  switcher_->RestoreSavedOptions();   // 每个会话只恢复一次用户开关

  InitializeComponents();
  InitializeOptions();
}
```

> 这段是 Engine 与 Context 的**双向连接**：
> - Context 的信号 → Engine 的回调（这里 `connect`）。比如用户改了输入串，Context 发 `update_notifier`，Engine 收到后调 `OnContextUpdate → Compose` 重算候选。
> - Engine → Context 的提交，则通过 `context_->Commit()` 触发 `commit_notifier → OnCommit`（见 4.2）。

按键处理入口 `ProcessKey` 的三态循环：

[src/rime/engine.cc:L99-L122](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L99-L122):

```cpp
bool ConcreteEngine::ProcessKey(const KeyEvent& key_event) {
  ProcessResult ret = kNoop;
  for (auto& processor : processors_) {
    ret = processor->ProcessKeyEvent(key_event);
    if (ret == kRejected) break;
    if (ret == kAccepted) return true;
  }
  context_->commit_history().Push(key_event);
  for (auto& processor : post_processors_) {
    ret = processor->ProcessKeyEvent(key_event);
    if (ret == kRejected) break;
    if (ret == kAccepted) return true;
  }
  context_->unhandled_key_notifier()(context_.get(), key_event);
  return false;
}
```

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：确认「工厂返回的是 ConcreteEngine，且它重写了全部四个虚函数」。
2. **操作步骤**：
   - 在 [src/rime/engine.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc) 中找到 `Engine::Create`（L60），确认它 `return new ConcreteEngine`。
   - 在同文件找到 `class ConcreteEngine`（L28），数一数它在 `public:` 段重写了几个 `virtual` 方法。
   - 确认 `ConcreteEngine` **没有**出现在 [src/rime/engine.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.h) 里（即实现对头文件不可见）。
3. **需要观察的现象**：`ConcreteEngine` 重写了 `ProcessKey`/`ApplySchema`/`CommitText`/`Compose` 全部四个。
4. **预期结果**：调用方（Session）只拿到 `Engine*` 抽象指针，不感知 `ConcreteEngine` 的存在——符合「依赖抽象、隐藏实现」。
5. 待本地验证（仅阅读，无需运行）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Engine::Create()` 不直接 `return new Engine`，而要 `return new ConcreteEngine`？

**答案**：因为 `Engine` 是抽象基类，它的虚函数多数是空实现、且没有组件容器——直接用基类对象既不能处理按键也不能产出候选。真正的行为（装配组件、响应 Context 信号）都在 `ConcreteEngine` 里。工厂返回 `ConcreteEngine` 但以 `Engine*` 暴露，让调用方依赖抽象接口。

**练习 2**：`ConcreteEngine` 的构造体里 `connect` 了 5 个 Context 信号回调。其中 `update_notifier` 连到的 `OnContextUpdate` 做了什么？

**答案**：`OnContextUpdate(ctx)` 检查 `ctx` 非空后调用 `Compose(ctx)`（见 [engine.cc:L124-L128](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L124-L128)）。也就是说，每当 Context 的输入串/光标发生变化（如 speller 追加了字母），就会触发一次重新切分 + 翻译 + 生成候选——这是「输入一个字母，候选立即刷新」的底层机制。

---

### 4.4 方案驱动装配：四类组件容器

#### 4.4.1 概念说明

这是本讲最关键的一节，直接对应**实践任务**。问题是：`ConcreteEngine` 的那四组容器（`processors_`/`segmentors_`/`translators_`/`filters_`）里到底装了哪些具体组件？答案是——**由当前方案的 `engine:` 配置块决定**。

回顾 u2-l3：方案文件 `*.schema.yaml` 顶层有一个 `engine:` 块，它有四个子列表：

```yaml
engine:
  processors:   [ ... ]   # 按键处理器清单
  segmentors:   [ ... ]   # 切分器清单
  translators:  [ ... ]   # 翻译器清单
  filters:      [ ... ]   # 过滤器清单
```

引擎在装配时读取这四张清单，对每个条目（一个字符串「处方」，如 `script_translator@pinyin`）：

1. 解析成 `Ticket`（携带 engine 指针、组件类型、klass、name_space）。
2. 用 `T::Require(klass)` 从组件注册表（Registry）里查到对应的组件工厂。
3. 调工厂的 `Create(ticket)` 实例化组件，塞进对应容器。

「换方案 = 换四张清单 = 换一套流水线」，而引擎代码本身一行都不用改——这就是 librime 可扩展性的核心来源。

#### 4.4.2 核心流程

装配一个容器的流程（以 `processors` 为例）：

```text
InitializeComponents():
  清空所有容器
  若存在 switcher_：先把 switcher_ 塞进 processors_ 头部
  取 schema_->config()
  for 每个 config_key in ["engine/processors", "engine/segmentors",
                          "engine/translators", "engine/filters"]:
       CreateComponentsFromList<T>(this, config, config_key, 类型名, 目标容器)

CreateComponentsFromList<T>(...):
  list = config->GetList(config_key)            # 读 YAML 列表
  for 第 i 项 in list:
     prescription = As<ConfigValue>(list[i])    # 取字符串，如 "script_translator@pinyin"
     ticket = {engine, component_type, prescription}   # 解析出 klass + name_space
     c = T::Require(ticket.klass)               # 查注册表得到工厂
     组件 = c->Create(ticket)                    # 实例化
     容器.push_back(组件)
```

关于「处方」字符串的解析：`Ticket` 的构造函数接受形如 `"klass"` 或 `"klass@alias"` 的串（见 [src/rime/ticket.h:L25-L29](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/ticket.h#L25-L29)）：

- `script_translator` → klass=`script_translator`，name_space=默认（通常就是 translator 的名字）。
- `script_translator@pinyin` → klass=`script_translator`，name_space=`pinyin`（覆盖默认命名空间，让翻译器去读方案里 `pinyin:` 那段配置）。

`@` 后的别名决定组件去方案里读**哪一段配置**，这是同方案里多个同类型组件（如 `script_translator` + `script_translator@pinyin`）能共存的关键。

#### 4.4.3 源码精读

装配的「模板函数」`CreateComponentsFromList`——对四种组件类型（T = Processor/Segmentor/Translator/Filter）复用同一段逻辑：

[src/rime/engine.cc:L297-L326](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L297-L326):

```cpp
template <typename T>
inline void CreateComponentsFromList(Engine* engine,
                                     Config* config,
                                     const string& config_key,
                                     const string& component_type,
                                     vector<an<T>>& target_collection) {
  if (auto component_list = config->GetList(config_key)) {
    size_t n = component_list->size();
    for (size_t i = 0; i < n; ++i) {
      auto prescription = As<ConfigValue>(component_list->GetAt(i));
      if (!prescription) continue;
      Ticket ticket{engine, component_type, prescription->str()};
      auto c = T::Require(ticket.klass);          // 查注册表
      if (!c) { LOG(ERROR) << ...; continue; }
      auto component = c->Create(ticket);          // 实例化
      if (!component) { LOG(ERROR) << ...; continue; }
      an<T> instance(component);
      target_collection.push_back(instance);
    }
  }
}
```

> 两个容错点值得注意：如果某个 klass 在注册表里查不到（`Require` 返回空），或工厂造不出实例，引擎只记一条 `ERROR` 日志并 `continue`——**不会让整个引擎崩溃**，只是这条清单项被跳过。这就是为什么「少装一个插件」往往表现为某功能失效，而不是引擎起不来。

`InitializeComponents` 调用四次模板，分别装配四组容器，再额外装配固定的 formatter 与 post-processor：

[src/rime/engine.cc:L328-L374](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L328-L374):

```cpp
void ConcreteEngine::InitializeComponents() {
  processors_.clear(); segmentors_.clear(); translators_.clear();
  filters_.clear(); formatters_.clear(); post_processors_.clear();

  if (switcher_) {
    processors_.push_back(switcher_);            // 切换器永远在 processors 最前
    if (schema_->schema_id() == ".default") {
      if (Schema* schema = switcher_->CreateSchema())
        schema_.reset(schema);                    // 从 .default 切到用户上次方案
    }
  }

  Config* config = schema_->config();
  if (!config) return;

  // 装配四类容器（模板复用）
  CreateComponentsFromList<Processor>(this, config, "engine/processors",   "processor",  processors_);
  CreateComponentsFromList<Segmentor>(this, config, "engine/segmentors",   "segmentor",  segmentors_);
  CreateComponentsFromList<Translator>(this, config, "engine/translators", "translator", translators_);
  CreateComponentsFromList<Filter>(this, config,    "engine/filters",      "filter",     filters_);

  // 固定的 shape_formatter（不在方案配置里）
  auto c_formatter = Formatter::Require("shape_formatter");
  if (c_formatter) { formatters_.push_back(...); }
  ...
}
```

> 三个关键细节：
> 1. **Switcher 总是被塞进 `processors_` 的第一个**——所以无论什么方案，切换器的按键（如 F4、Ctrl+`` ` ``）永远最先被识别。
> 2. **`.default` 的特殊处理**：引擎刚构造时 `schema_` 是 `.default`（见 4.1.3），这里通过 `switcher_->CreateSchema()` 把它替换成用户实际使用的方案。换句话说，**真正的方案是在装配阶段由 Switcher 选定的**。
> 3. **`formatters_`/`post_processors_` 不读方案**：它们固定装配 `shape_formatter` 和 `shape_processor`（若这些组件在注册表里存在），用于字符形状处理。

#### 4.4.4 代码实践（实践任务：对照方案配置验证装配）

1. **实践目标**：验证 `Engine::Create` 装出的 `ConcreteEngine`，其四组容器的内容与方案的 `engine:` 配置块逐一对应。
2. **操作步骤**：
   - 打开默认方案 [data/minimal/luna_pinyin.schema.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml)，找到 `engine:` 块（L39-L68）。
   - 把四个列表（`processors`/`segmentors`/`translators`/`filters`）的条目逐条抄下。
   - 对照 [src/rime/engine.cc:L350-L357](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L350-L357) 的四次 `CreateComponentsFromList` 调用，确认每张列表分别填进哪个容器、`config_key` 和 `component_type` 分别是什么。
   - 对带 `@` 的条目（如 `script_translator@pinyin`、`affix_segmentor@cangjie`、`simplifier@zh_tw`），说明 `Ticket` 会把 `@` 前后拆成 klass 与 name_space。
3. **需要观察的现象**：
   - `processors` 列表第一个被装进 `processors_` 的实际不是 `ascii_composer`，而是 **`switcher_`**（由 `InitializeComponents` 在调模板前手动 `push_back`），其后才跟 `ascii_composer`、`recognizer`……
   - `translators` 里同时出现 `script_translator`（无别名）和 `script_translator@pinyin`（带别名），二者是**两个独立实例**，分别读方案里 `translator:` 段和 `pinyin:` 段配置。
4. **预期结果**：能填出下面这张装配对照表（部分）：

   | 方案 `engine:` 条目 | `Ticket.klass` | `Ticket.name_space` | 落入容器 |
   | --- | --- | --- | --- |
   | `ascii_composer` | `ascii_composer` | 默认 | `processors_`（在 switcher_ 之后） |
   | `affix_segmentor@cangjie` | `affix_segmentor` | `cangjie` | `segmentors_` |
   | `script_translator@pinyin` | `script_translator` | `pinyin` | `translators_` |
   | `simplifier@zh_tw` | `simplifier` | `zh_tw` | `filters_` |

5. **若想本地验证**：在 `InitializeComponents` 末尾或各 `CreateComponentsFromList` 的 `push_back` 后临时加一行 `LOG(INFO) << ...`（**示例代码，非项目原有**，且仅用于学习、不要提交），重新编译运行 `rime_api_console`，从日志即可看到实际装配的组件顺序。**注意：本讲不修改源码，这只是可选的本地观察手段。**

#### 4.4.5 小练习与答案

**练习 1**：方案里 `translators` 同时列了 `script_translator` 和 `script_translator@pinyin`。它们的 `klass` 相同，注册表查到的是同一个工厂，那为何实例化后是两个行为不同的翻译器？

**答案**：因为 `Ticket` 不同。前者 name_space 是默认（读方案里 `translator:` 段，对应主词典 `luna_pinyin`）；后者 name_space=`pinyin`（读方案里 `pinyin:` 段，且该段配了 `enable_user_dict: false`、前缀 `P:`）。同一个工厂类用不同的 `Ticket`（尤其不同的 name_space）`Create` 出来，就会读不同的配置段、表现出不同行为。这就是 `@别名` 语法的意义。

**练习 2**：如果用户安装的某个插件（如 `librime-lua`）没装，方案 `filters` 里却写了 `lua`，引擎会怎样？

**答案**：`Filter::Require("lua")` 在注册表里查不到（插件未加载，组件未注册），返回空，`CreateComponentsFromList` 记一条 `LOG(ERROR)` 并 `continue` 跳过这一项（见 [engine.cc:L311-L315](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L311-L315)）。引擎照常运行，只是缺少 lua 过滤能力——不会崩溃。这是「缺失插件可降级」的容错设计。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个**端到端的「引擎诞生记」追踪任务**：

**任务**：从 `Session::Session()` 开始，一路追到「四类组件容器被填满」，画出完整的时序图并写出每一步对应的源码位置。

**建议步骤**：

1. 起点：[src/rime/service.cc:L17-L24](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.cc#L17-L24) —— `Session` 构造时 `Engine::Create()` 并连接两条信号。
2. 工厂：[src/rime/engine.cc:L60-L62](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L60-L62) —— `Create` 返回 `new ConcreteEngine`。
3. 基类构造：[src/rime/engine.cc:L64](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L64) —— 先造默认 Schema（`.default`）和空 Context。
4. 派生类构造：[src/rime/engine.cc:L71-L93](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L71-L93) —— 连接 5 个 Context 信号回调、创建 Switcher、恢复开关、调用 `InitializeComponents`。
5. 装配：[src/rime/engine.cc:L328-L374](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L328-L374) —— 先塞 `switcher_`，再把 `.default` 换成真实方案，最后四次调 `CreateComponentsFromList` 读方案的 `engine/processors|segmentors|translators|filters`。

**交付物**（写在你的学习笔记里）：

- 一张时序图（含上述 5 步）。
- 用 [data/minimal/luna_pinyin.schema.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml) 的 `engine:` 块，具体列出 `processors_`/`segmentors_`/`translators_`/`filters_` 四个容器被填成的实际内容（别忘了 `processors_` 开头那个 `switcher_`）。
- 一句话回答：为什么「换一个 `.schema.yaml` 就能换一种输入法」？

**参考答案要点**：因为引擎装配四类组件时，完全是从方案 `engine:` 配置块读取清单、再按名字从注册表查工厂实例化的。方案变了，四张清单就变了，装配出的流水线就变了，而 `Engine`/`ConcreteEngine` 的代码一行未改。

## 6. 本讲小结

- `Engine` 是 librime 的抽象基类，持有**两个核心对象**：独占的 `schema_`（方案/图纸）和 `context_`（状态/工作台），外加提交信号 `sink_` 和旁路指针 `active_engine_`。
- Engine 继承 `Messenger`，因此拥有**两条独立的信号通道**：`CommitSink`（提交文本，拉模型，经 `Session::OnCommit` 累加给前端 `get_commit`）和 `MessageSink`（状态通知，推模型，经 `Service::Notify` 立即推给前端）。
- 构造被保护、只暴露静态工厂 `Engine::Create()`，它返回**隐藏实现** `ConcreteEngine`——让调用方依赖抽象、便于将来替换实现。
- `ConcreteEngine` 在构造时**双向连接** Context 的 5 个信号回调，并调用 `InitializeComponents` 装配流水线。
- **方案驱动装配**：`InitializeComponents` 读方案 `engine:` 块的四张清单，经模板函数 `CreateComponentsFromList` 把每个「处方字符串」解析成 `Ticket`、查注册表、实例化后塞进 `processors_`/`segmentors_`/`translators_`/`filters_` 四组容器。
- 装配具备**容错**：组件查不到或造不出只记 `ERROR` 日志并跳过，不崩溃；`switcher_` 总在 `processors_` 最前；`.default` 会在装配期被 Switcher 替换为真实方案。

## 7. 下一步学习建议

本讲只搭好了引擎**骨架**——组件容器被填满了，但容器里每个组件「内部如何工作」尚未展开。接下来建议：

- **进入 u3「输入状态与候选生成」**：重点读 `Context`、`Segmentation`、`Composition`、`Translation`、`Menu`。理解本讲里反复出现的 `Compose(ctx)` 到底如何把输入串变成候选——也就是 [engine.cc:L154-L233](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L154-L233) 里 `CalculateSegmentation` + `TranslateSegments` 的细节。
- **预热 u5「组件与模块架构」**：本讲频繁出现 `T::Require(klass)` 和 `Ticket`，它们的底层是 `Component`/`Class`/`Registry` 体系。如果你想搞清楚「`script_translator` 这个名字是怎么和 C++ 类绑定的」，直接跳到 [src/rime/component.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/component.h) 与 [src/rime/registry.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/registry.h)。
- **延伸阅读**：本讲的 `ProcessKey` 三态协议、各 Processor/Segmentor/Translator/Filter 的具体实现，集中在 u6「按键处理流水线」。在那之前，先掌握 u3 的候选数据模型会让 u6 更好懂。
