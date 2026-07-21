# 四大组件基类

## 1. 本讲目标

本讲是「组件与模块架构」单元的第二篇。上一篇 u5-l1 我们建立了 librime 的组件注册骨架：`Class<T, Arg>` 模板定义工厂接口、`Component<T>` 提供 `new T(arg)` 默认实现、`Registry` 单例按名字登记工厂、`T::Require(name)` 按名取货。但那些都是「机制」，还没有回答「引擎到底要造哪些东西」。

本讲聚焦**引擎流水线真正依赖的四种组件基类**：`Processor`、`Segmentor`、`Translator`、`Filter`（外加一个辅助的 `Formatter`）。读完本讲你应该能够：

1. 说出四种基类各自的**核心虚函数签名**与**返回值含义**。
2. 说出每一种组件在 `ProcessKey` 流水线中的**触发时机**与**输入/输出**。
3. 解释为什么四种基类「长得几乎一样」——它们都继承 `Class<T, const Ticket&>`、都用 `Ticket` 构造、都持有 `engine_` 和 `name_space_`。
4. 看懂 `engine.cc` 中 `CreateComponentsFromList` 这一个模板函数如何用同一套代码装配出四组完全不同的组件容器。

本讲**只讲基类契约**，不讲具体实现（`speller`、`script_translator`、`simplifier` 等留给 u6 各组件族讲义）。

## 2. 前置知识

本讲默认你已经读过以下讲义，不再重复其中的细节：

- **u5-l1 组件注册体系**：`ComponentBase`、`Class<T, Arg>` 内嵌的 `Component` 子类、`Component<T>` 默认工厂、`Registry` 单例、`Require(name)`。本讲的四个基类**全部**继承自 `Class<T, const Ticket&>`，这正是 u5-l1 机制的具体落地。
- **u2-l4 Engine 引擎骨架**：`ConcreteEngine` 持有 `processors_`/`segmentors_`/`translators_`/`filters_`/`formatters_`/`post_processors_` 六组容器，由 `InitializeComponents()` 装配。本讲会反复回到这六组容器。
- **u3 输入状态与候选生成**：`Context`（u3-l1）、`Segmentation`/`Segment`（u3-l2）、`Translation`/`Candidate`（u3-l3）、`Menu`（u3-l4）。组件的输入输出就是这些对象。

此外有两个本讲会用到但详细拆解放到 u5-l4 的概念，先给一句话直觉：

- **`Ticket`（工单）**：组件实例化时的「上下文包裹」，携带 `engine`、`schema`、`name_space`、`klass` 四个字段。所有四种基类的构造函数都接收一个 `const Ticket&`。
- **`name_space`（命名空间）**：组件读取自身配置时的「定位锚点」。配置里写 `simplifier@zh_simp` 时，`@` 后面的 `zh_simp` 就是 `name_space`，让同一个 `simplifier` 类可以读不同的配置段、实例化出多个不同行为的对象。

## 3. 本讲源码地图

本讲涉及的关键文件都极短（多数只有 30~40 行），因为基类只定义**契约**，不定义行为。

| 文件 | 作用 | 行数 |
| --- | --- | --- |
| [src/rime/processor.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/processor.h) | `Processor` 基类 + `ProcessResult` 三态枚举 | ~43 |
| [src/rime/segmentor.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/segmentor.h) | `Segmentor` 基类，`Proceed` 纯虚函数 | ~35 |
| [src/rime/translator.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translator.h) | `Translator` 基类，`Query` 纯虚函数 | ~40 |
| [src/rime/filter.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/filter.h) | `Filter` 基类，`Apply` 与 `AppliesToSegment` | ~42 |
| [src/rime/formatter.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/formatter.h) | `Formatter` 基类，`Format` 纯虚函数（辅助） | ~34 |
| [src/rime/engine.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc) | `ConcreteEngine`：装配现场与调用现场 | ~398 |
| [src/rime/ticket.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/ticket.h) / [ticket.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/ticket.cc) | `Ticket` 结构与 `klass@alias` 解析 | ~35 / ~27 |

阅读建议：先看 `engine.cc` 里 `ProcessKey`、`CalculateSegmentation`、`TranslateSegments`、`FormatText` 这四个函数（它们是四种基类的**调用现场**），再回头逐个精读四个 `.h`，就能立刻明白每个虚函数「在什么时候被谁调用、参数从哪来、返回值怎么处理」。

## 4. 核心概念与源码讲解

### 4.0 四种基类的共同骨架（先看共性）

在逐个讲解之前，先抓住一个关键事实：**这四个基类的骨架几乎一模一样**。把它们的类定义并排放在一起，会发现只有「那个核心虚函数」不同，其余结构完全对称。

它们的共同骨架是：

```cpp
class Xxx : public Class<Xxx, const Ticket&> {
 public:
  explicit Xxx(const Ticket& ticket)
      : engine_(ticket.engine), name_space_(ticket.name_space) {}
  virtual ~Xxx() = default;

  virtual /* 核心虚函数 */ = 0;   // 唯一不同的地方

  string name_space() const { return name_space_; }

 protected:
  Engine* engine_;
  string name_space_;
};
```

每一行都值得记住：

1. **`public Class<Xxx, const Ticket&>`**：把产品类型 `Xxx` 和构造参数类型 `const Ticket&` 绑定进 u5-l1 讲的组件体系。这正是 `Xxx::Require(name)` 能按名取到工厂、再 `Create(ticket)` 造出对象的根源。
2. **`explicit Xxx(const Ticket& ticket)`**：构造函数只接收一个 `Ticket`，从中抽取 `engine_` 和 `name_space_`。四种基类的构造签名**完全一致**，所以 `CreateComponentsFromList` 模板可以用同一份代码装配四组容器。
3. **`engine_`**：指向所属引擎的裸指针。组件不拥有引擎（引擎拥有组件），所以这里用裸指针而非智能指针。
4. **`name_space_`**：组件读取自身配置的命名空间，决定它从方案的哪个 YAML 节读配置。
5. **核心虚函数是纯虚（`= 0`）**：基类只定契约，行为由 `gear/` 目录下的各子类提供（u6 详讲）。

「统一构造契约」是这套设计最巧妙的地方。请看 `engine.cc` 里这一个模板函数如何通吃四类组件：

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
      auto c = T::Require(ticket.klass);   // 按名取工厂
      if (!c) { LOG(ERROR) << ...; continue; }   // 缺件仅记错跳过，不崩溃
      auto component = c->Create(ticket);        // 用 ticket 造实例
      ...
      target_collection.push_back(instance);
    }
  }
}
```

> 参见 [src/rime/engine.cc:297-326](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L297-L326)：`CreateComponentsFromList` 模板。注意 `T::Require(ticket.klass)` 这一行——`T` 分别替换成 `Processor`/`Segmentor`/`Translator`/`Filter` 时，由于它们都继承 `Class<T, const Ticket&>`，`Require` 都能正常工作。

装配发生在这里：

> 参见 [src/rime/engine.cc:350-357](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L350-L357)：`InitializeComponents()` 四次调用 `CreateComponentsFromList`，分别读方案的 `engine/processors`、`engine/segmentors`、`engine/translators`、`engine/filters` 四张清单，填入四组容器。换方案就是换这四张清单，引擎代码一行都不用改。

`Ticket` 的解析逻辑（`klass@alias` 拆分）放在构造函数里：

> 参见 [src/rime/ticket.cc:14-24](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/ticket.cc#L14-L24)：遇到 `@` 就把它后面的串作为 `name_space`、前面的串作为 `klass`。例如处方串 `simplifier@zh_simp` 被拆成 `klass="simplifier"`、`name_space="zh_simp"`。

理解了共性，下面逐个看每个基类那个「唯一不同的核心虚函数」。

---

### 4.1 Processor：按键处理器

#### 4.1.1 概念说明

`Processor`（处理器）是流水线的**第一道关卡**。每当用户按下一个键，引擎的 `ProcessKey` 会把这次按键依次交给 `processors_` 容器里的每一个 Processor。Processor 的职责是**决定这个键该怎么处置**：把它追加进输入串、触发某个开关、直接吃掉，还是放给操作系统处理。

Processor 不直接产出候选，它只**操纵 `Context` 的状态**（比如往 `input` 里追加字符、改 option），或者**对按键本身表态**。候选的产出是后面 Segmentor/Translator 的事。

#### 4.1.2 核心流程

Processor 通过一个三态枚举向引擎表态：

| 返回值 | 含义 | 引擎的后续动作 |
| --- | --- | --- |
| `kAccepted` | 这个键我接管了（消耗掉） | 立刻 `return true`，**中断整个 processor 链**，不再问后面的 processor |
| `kRejected` | 这个键不属于输入法管的事 | 中断 processor 链（`break`），交给后续的 `post_processors_`，最终 `return false` 让 OS 默认处理 |
| `kNoop` | 我不关心，问下一个 | 继续循环，把键传给下一个 processor |

关键调用现场在 `ProcessKey`：

```cpp
ProcessResult ret = kNoop;
for (auto& processor : processors_) {
  ret = processor->ProcessKeyEvent(key_event);
  if (ret == kRejected) break;       // 中断，落到 post-processing
  if (ret == kAccepted) return true; // 消耗，整条管线结束
}
// 记录未处理的键（空格、数字、退格等）
context_->commit_history().Push(key_event);
// 后处理 processor 链（post_processors_）
for (auto& processor : post_processors_) {
  ret = processor->ProcessKeyEvent(key_event);
  if (ret == kRejected) break;
  if (ret == kAccepted) return true;
}
context_->unhandled_key_notifier()(context_.get(), key_event);
return false;  // 未消耗，OS 默认处理
```

注意一个容易误解的点：`kRejected` **不会**让函数立刻 `return false`，它只是 `break` 掉主 processor 链，流程仍然会走到 `commit_history` 记录和 `post_processors_`。真正的「未消耗」是函数末尾的 `return false`。

#### 4.1.3 源码精读

先看三态枚举的定义与注释（注释本身就是最准确的语义说明）：

> [src/rime/processor.h:18-22](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/processor.h#L18-L22) —— `ProcessResult` 枚举：`kRejected`（交给 OS 默认处理）、`kAccepted`（消耗掉）、`kNoop`（留给下一个 processor）。

再看基类本身：

> [src/rime/processor.h:24-39](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/processor.h#L24-L39) —— `Processor` 继承 `Class<Processor, const Ticket&>`；构造函数从 `ticket` 抽取 `engine_` 与 `name_space_`；核心虚函数 `ProcessKeyEvent` 默认实现返回 `kNoop`（即「基类不关心任何键」，子类按需 override）。

调用现场：

> [src/rime/engine.cc:99-122](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L99-L122) —— `ConcreteEngine::ProcessKey`，对应上面 4.1.2 的核心流程，是 `kAccepted/kRejected/kNoop` 三态语义的唯一权威解释。

#### 4.1.4 代码实践

**实践目标**：通过阅读源码，验证 `kAccepted` 与 `kRejected` 的早退差异。

**操作步骤**：

1. 打开 [src/rime/engine.cc:99-122](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L99-L122)。
2. 假设 `processors_` 里有三个 Processor：A、B、C。
3. 分别考虑这三种情形，推演 A、B、C 是否还会被调用：
   - 情形一：A 返回 `kAccepted`。
   - 情形二：A 返回 `kNoop`，B 返回 `kRejected`。
   - 情形三：A、B 都返回 `kNoop`，C 返回 `kNoop`。

**需要观察的现象**：`kAccepted` 立刻让整个 `ProcessKey` 返回 `true`；`kRejected` 只 `break` 主循环，但 `commit_history().Push` 与 `post_processors_` 仍然会执行。

**预期结果**：

| 情形 | B 是否调用 | C 是否调用 | 最终 return | commit_history 是否记录 |
| --- | --- | --- | --- | --- |
| 一（A=kAccepted） | 否 | 否 | true | 否 |
| 二（B=kRejected） | 是 | 否 | false | 是 |
| 三（全 kNoop） | 是 | 是 | false | 是 |

> 待本地验证：若你已按 u1-l2 构建了带 `BUILD_TEST=ON` 的 librime，可在 gear 目录下找到具体 Processor 子类的单元测试，对照断言印证上表。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Processor::ProcessKeyEvent` 的默认实现返回 `kNoop` 而不是 `kRejected`？

**参考答案**：返回 `kNoop` 表示「我这个基类不表态」，让按键继续传给下一个 processor，符合「基类只定契约、不干预行为」的设计。若默认返回 `kRejected`，则任何忘记 override 的子类都会直接中断 processor 链，造成难以排查的「按键失效」bug。

**练习 2**：如果一个 Processor 既想让按键继续传给后面的 Processor，又想在 `Context` 上留下副作用（比如打开一个 option），它该返回什么？

**参考答案**：返回 `kNoop`。`kNoop` 的语义是「我不消耗这个键，但我可以顺手改状态」。改 option 这类副作用不构成「消耗按键」，所以仍用 `kNoop` 让链继续。

---

### 4.2 Segmentor：分词器

#### 4.2.1 概念说明

`Segmentor`（分词器）的工作发生在 Processor 之后、候选生成之前。它的任务是把 `Context` 里的原始输入串（一串字符）切成若干个**有意义的片段**（`Segment`），并给每个片段打上 **tag**（标签）。比如输入 `P:ni hao` 可能被切成多段，分别打上 `abc`、`raw`、`punct` 等 tag，以便后续的 Translator 按tag各取所需。

Segmentor 不查词典、不产出候选文字，它只做「**输入串的几何切分**」：决定哪里是一段的边界、这段属于什么类型。

#### 4.2.2 核心流程

引擎的 `CalculateSegmentation` 用一个 while 循环驱动 segmentor 链，每一轮把所有 segmentor 按顺序问一遍：

```cpp
while (!segments->HasFinishedSegmentation()) {
  ...
  for (auto& segmentor : segmentors_) {
    if (!segmentor->Proceed(segments))   // 返回 false 就 break
      break;
  }
  if (start_pos == segments->GetCurrentEndPosition())
    break;   // 这一轮没有任何推进，防死循环
  ...
  segments->Forward();   // 腾出新格子，进入下一段
}
```

`Proceed` 返回 `bool`：

- 返回 `true`：表示「我可能切出了新片段，请继续问下一个 segmentor」（它还可以在同一个起点上叠加更多 tag）。
- 返回 `false`：表示「我在当前起点已经无能为力，停止 segmentor 链」。

多个 segmentor 协作的典型分工是：`abc_segmentor` 先按字母表切出拼音段（打 `abc` tag），`affix_segmentor` 再识别前缀/后缀（打 `xxx_prefix` 之类 tag），都不认识时 `fallback_segmentor` 兜底把剩余字符变成 `raw` 段。这些具体实现见 u6-l3。

#### 4.2.3 源码精读

> [src/rime/segmentor.h:18-31](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/segmentor.h#L18-L31) —— `Segmentor` 继承 `Class<Segmentor, const Ticket&>`，骨架与 `Processor` 完全对称；唯一的核心虚函数是 `Proceed(Segmentation* segmentation)`，是**纯虚**（`= 0`），子类必须实现。

调用现场：

> [src/rime/engine.cc:171-201](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L171-L201) —— `CalculateSegmentation`：注意第 180-183 行的 segmentor 循环（`if (!segmentor->Proceed(segments)) break;`），以及第 186-187 行的「无推进则 break」防死循环保护。

`Proceed` 的输入是一个 `Segmentation*`（u3-l2 讲过的、`vector<Segment>` 的有序集合），segmentor 通过调用它的 `AddSegment` / `Forward` 等方法来扩展切分结果；返回值只是控制循环是否继续。

#### 4.2.4 代码实践

**实践目标**：理解 segmentor 链「同一轮内顺序问、返回 false 即停」的协作方式。

**操作步骤**：

1. 打开 [src/rime/engine.cc:180-183](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L180-L183)。
2. 假设 `segmentors_` = {`abc_segmentor`, `affix_segmentor@pinyin`, `fallback_segmentor`}，当前起点有一段输入 `abc`。
3. 推演：若 `abc_segmentor` 切出片段并返回 `true`，`affix_segmentor` 返回 `false`，`fallback_segmentor` 会不会被调用？

**需要观察的现象**：segmentor 链是「**协作**」而非「**互斥**」——前一个返回 `true` 时后一个仍有机会在**同一起点**叠加 tag；任何一个返回 `false` 就立刻 `break`，后面的不再执行。

**预期结果**：`fallback_segmentor` **不会**被调用（因为 `affix_segmentor` 返回 `false` 触发了 `break`）。这说明 segmentor 的顺序很重要：兜底型（如 fallback）通常排在最后。

#### 4.2.5 小练习与答案

**练习 1**：`Proceed` 为什么接收的是 `Segmentation*` 指针，而不是返回一个新的 `Segmentation`？

**参考答案**：因为 segmentor 是**增量协作**的——多个 segmentor 共享并逐步填充同一个 `Segmentation` 对象。接收指针让每个 segmentor 都能看到前序 segmentor 已切出的片段，并在同一起点上为片段追加 tag。若改成返回值，则无法表达「在别人切好的片段上叠加 tag」这种协作。

**练习 2**：`CalculateSegmentation` 第 186-187 行的 `if (start_pos == segments->GetCurrentEndPosition()) break;` 如果删掉，会发生什么？

**参考答案**：如果某个 segmentor 既不推进 `end_pos`、又始终返回 `true`，外层 `while` 就会无限循环（永远不 `Forward`、永远不 `HasFinishedSegmentation`）。这行是防死循环的「无推进即退出」保护。

---

### 4.3 Translator：翻译器

#### 4.3.1 概念说明

`Translator`（翻译器）是候选的**生产者**。Segmentor 把输入切成带 tag 的片段后，Translator 负责针对**某一个片段**查词典、生成候选词，并把候选包装成一个**惰性候选流** `Translation`（u3-l3 讲过的迭代器）返回。

一个引擎里通常有多个 Translator 并存（如拼音方案里同时有 `script_translator` 查主词典、`punct_translator` 出标点、`reverse_lookup_translator` 做反查）。引擎对**同一个 segment** 会问遍所有 translator，把它们返回的候选流合并。

#### 4.3.2 核心流程

```cpp
for (Segment& segment : *segments) {
  ...
  auto menu = New<Menu>();
  for (auto& translator : translators_) {
    auto translation = translator->Query(input, segment);  // 查这一段
    if (!translation) continue;                             // 这位没结果
    if (translation->exhausted()) continue;                 // 结果是空的
    menu->AddTranslation(translation);                      // 加入菜单归并
  }
  ...
}
```

`Query` 的语义：

- 输入：`input`（这段对应的原始字符串）+ `segment`（带 tag 与区间的片段对象）。
- 输出：一个 `an<Translation>`（即 `shared_ptr<Translation>`）。
  - 返回 `nullptr`：表示「这段我不负责 / 我查不到」，引擎直接跳过。
  - 返回已耗尽的 translation：也表示「无有效候选」，跳过（日志记为 `futile translation`）。
  - 返回非空且未耗尽的 translation：交给 `Menu` 归并。

注意 Translator 的查询是**惰性**的——`Query` 返回的 `Translation` 只是一个「能拉取候选的句柄」，真正的候选词要等到 Menu 调 `Peek/Next` 时才逐个算出来（u3-l3、u3-l4）。

#### 4.3.3 源码精读

> [src/rime/translator.h:22-36](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translator.h#L28-L29) —— `Translator` 的核心虚函数 `Query(const string& input, const Segment& segment)` 返回 `an<Translation>`，是纯虚。骨架同样与 Processor 对称。

调用现场：

> [src/rime/engine.cc:203-233](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L203-L233) —— `TranslateSegments`：第 214-223 行是 translator 循环，注意对 `nullptr` 与 `exhausted()` 两种「无结果」情况都 `continue` 跳过。

一个最小可运行的子类范例（sample 插件里的 `TrivialTranslator`）：

> [sample/src/trivial_translator.h:21-32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/src/trivial_translator.h#L21-L32) —— 继承 `Translator`，实现 `Query`，内部用一个 `map<string,string>` 当词典。这正是 u9-l6 插件开发实战要详细拆解的模板。

#### 4.3.4 代码实践

**实践目标**：理解「同一段被多个 translator 查、结果合并」的机制。

**操作步骤**：

1. 打开 [src/rime/engine.cc:214-223](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L214-L223)。
2. 假设当前 segment 是拼音段 `ni`，`translators_` = {`script_translator`（主词典，返回 5 个候选）、`punct_translator`（对拼音段不感兴趣，返回 `nullptr`）、`history_translator`（无历史，返回一个 exhausted 的 translation）}。
3. 推演：`menu->AddTranslation` 会被调用几次？

**需要观察的现象**：translator 链不像 processor 那样「早退」，而是**全部问完**——因为多个 translator 的候选需要合并；`nullptr` 和 `exhausted()` 都不算有效贡献。

**预期结果**：`menu->AddTranslation` 只被调用 **1 次**（只有 `script_translator` 贡献了非空未耗尽的流）。`punct_translator` 和 `history_translator` 都在第 216 或 218 行被 `continue` 掉了。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Query` 返回「已耗尽的 translation」要单独判断、而不是让 `nullptr` 覆盖这种情况？

**参考答案**：因为「我负责这段，但恰好没查到候选」与「我不负责这段」是两种不同的语义。返回一个 exhausted 的 translation 让引擎知道「这位 translator 被正确激活了，只是无结果」（日志会记 `futile translation`），便于排查；而 `nullptr` 表示「这位根本没接手」。分开判断让诊断信息更清晰。

**练习 2**：`Query` 的第二个参数 `const Segment& segment` 除了提供输入区间，还提供了什么对 translator 有用的信息？

**参考答案**：`segment` 还携带 `tags`（segmentor 打的标签）。translator 通常用 tag 判断「这段该不该我处理」——比如 `punct_translator` 只处理带 `punct` tag 的段，`script_translator` 只处理带 `abc` tag 的段。这就是 Segmentor 与 Translator 之间的 tag 契约（u3-l2、u6-l4 详讲）。

---

### 4.4 Filter：过滤器

#### 4.4.1 概念说明

`Filter`（过滤器）是候选的**后处理者**。Translator 产出原始候选流后，Filter 以**装饰器**的方式逐层包装这条流，对候选做转换、去重、过滤或重排。典型例子：`simplifier`（用 OpenCC 做繁简转换，把候选文字替换或追加字形变体）、`uniquifier`（合并文字相同的重复候选）、`charset_filter`（只保留某字符集内的候选）。

Filter 与 Translator 的关键区别：Translator 是「**从无到有产出**候选流」，Filter 是「**对已有的候选流做变换**」——它接收一条 `Translation`、返回一条新的（通常是被装饰过的）`Translation`。

#### 4.4.2 核心流程

```cpp
for (auto& filter : filters_) {
  if (filter->AppliesToSegment(&segment)) {   // 这位管不管当前段？
    menu->AddFilter(filter.get());            // 把 filter 挂到菜单
  }
}
```

注意这里 Filter 不是在装配期立刻 `Apply`，而是通过 `menu->AddFilter` 注册到 `Menu`（u3-l4）。Menu 内部会把所有 filter 包成一层层洋葱：`result_ = filter->Apply(result_, &candidates_)`。真正的过滤发生在 Menu 按需拉取候选时（拉模型）。

`Apply` 的签名：

```cpp
virtual an<Translation> Apply(an<Translation> translation,
                              CandidateList* candidates) = 0;
```

- 输入：`translation`（上一层的候选流）+ `candidates`（共享候选区指针，详见 u3-l4）。
- 输出：一条新的 `Translation`，通常是把输入流包进一个 `Translation` 子类（装饰器），在 `Peek/Next` 时施加过滤逻辑。
- `AppliesToSegment(segment)`：默认返回 `true`（对所有段都生效），子类可 override 为「只对特定 tag 的段生效」。

#### 4.4.3 源码精读

> [src/rime/filter.h:22-38](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/filter.h#L22-L38) —— `Filter` 的核心虚函数 `Apply(an<Translation>, CandidateList*)` 是纯虚；`AppliesToSegment` 有默认实现返回 `true`。骨架仍与前三者对称。

`CandidateList` 的定义（`Apply` 第二个参数的类型）：

> [src/rime/candidate.h:52](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/candidate.h#L52) —— `using CandidateList = vector<of<Candidate>>;`，即候选指针的数组。这个指针指向 Menu 的「共享候选区」，让 filter 在过滤时能感知「已经吐出过哪些候选」（u3-l4 详述为何要共享）。

调用现场（注册到 Menu，而非立即 Apply）：

> [src/rime/engine.cc:224-228](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L224-L228) —— `TranslateSegments` 里的 filter 循环：`if (filter->AppliesToSegment(&segment)) menu->AddFilter(filter.get());`。真正的 `Apply` 发生在 `Menu` 内部，这里只是挂载。

#### 4.4.4 代码实践

**实践目标**：理解 Filter 的「洋葱式装饰器」装配方式与「按段生效」机制。

**操作步骤**：

1. 打开 [src/rime/engine.cc:224-228](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L224-L228)，确认 filter 是被**注册**到 menu 而非立即执行。
2. 回顾 u3-l4 讲过的 `Menu::AddFilter`：它执行 `result_ = filter->Apply(result_, &candidates_)`，把链头层层包裹。
3. 假设方案配置了 `filters: [simplifier@zh_simp, simplifier@zh_tw, uniquifier]`，三个 filter 依次注册。

**需要观察的现象**：装配后候选流变成一棵嵌套的装饰器树 `uniquifier(simplifier_zh_tw(simplifier_zh_simp(Merged(...))))`。每条候选要依次穿过 simplifier→simplifier→uniquifier 才能进入最终菜单。

**预期结果**：filter 的注册顺序决定了候选被处理的顺序——先注册的在内层、先作用于原始候选；后注册的在外层、作用于前一个 filter 的输出。这就是为什么方案的 `filters:` 列表里 `uniquifier` 通常排在最后（等所有繁简变体都生成完再去重）。

#### 4.4.5 小练习与答案

**练习 1**：`Filter::Apply` 为什么同时接收 `translation` 和 `candidates` 两个参数？只用 `translation` 不够吗？

**参考答案**：`translation` 是「待过滤的候选流」（装饰对象），`candidates` 是 Menu 维护的「**共享候选区**」——记录已经被各 filter 确认吐出的候选。像 `uniquifier` 这种去重 filter 需要查询「之前是否已经吐过文字相同的候选」，这必须借助共享候选区，单靠自己的 translation 流是看不到别的 filter 的输出的（u3-l4 详述）。

**练习 2**：`AppliesToSegment` 默认返回 `true`。请举一个需要 override 成 `false`（对某些段不生效）的场景。

**参考答案**：`reverse_lookup_filter` 这类 filter 可能只应对「反查段」（带特定 tag 的段）生效，而不应处理普通的拼音段。此时 override `AppliesToSegment` 检查 `segment` 的 tag，只在匹配时返回 `true`，避免误伤普通候选。

---

### 4.5 Formatter：格式化器（辅助基类）

#### 4.5.1 概念说明

`Formatter`（格式化器）不属于主流水线的「Processor→Segmentor→Translator→Filter」四阶段，它是一个**辅助基类**，作用是在文字**即将上屏提交**的那一刻，对最终文本做就地改写。典型用途是 `shape_formatter`（半角/全角形状转换），把提交的 ASCII 字符按当前开关转换成全角等形式。

四种主流水线组件都作用于「候选生成阶段」，而 Formatter 作用于「**提交阶段**」——它不碰候选，只碰最终要交给前端的字符串。

#### 4.5.2 核心流程

`Format` 的签名极简：

```cpp
virtual void Format(string* text) = 0;
```

输入是一个指向字符串的指针，Format **就地修改**这个字符串，无返回值。引擎在两个提交入口都调用 formatter 链：

```cpp
void ConcreteEngine::FormatText(string* text) {
  if (formatters_.empty()) return;
  for (auto& formatter : formatters_) {
    formatter->Format(text);   // 依次就地改写
  }
}
```

`FormatText` 被 `CommitText`（直接提交一段文本）和 `OnCommit`（提交组合结果）在调用 `sink_`（把文本送出引擎）之前调用。所以 formatter 链是提交前的最后一道变换。

#### 4.5.3 源码精读

> [src/rime/formatter.h:19-30](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/formatter.h#L19-L30) —— `Formatter` 继承 `Class<Formatter, const Ticket&>`，核心虚函数 `Format(string* text)` 是纯虚。骨架与四大基类完全一致，区别只在「作用于提交文本而非候选」。

调用现场：

> [src/rime/engine.cc:235-242](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L235-L242) —— `FormatText`：遍历 `formatters_` 逐个 `Format(text)`。

提交入口：

> [src/rime/engine.cc:244-249](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L244-L249) 与 [src/rime/engine.cc:251-257](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L251-L257) —— `CommitText` 与 `OnCommit` 都在 `sink_(text)`（送出文本）之前调用 `FormatText(&text)`。

Formatter 的装配方式与四大基类略有不同——它不是从方案的某张清单读取，而是硬编码按名字 `Require("shape_formatter")`：

> [src/rime/engine.cc:358-365](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L358-L365) —— `InitializeComponents` 里用 `Formatter::Require("shape_formatter")` 取工厂；若该组件未注册（比如精简构建），只记一条 WARNING 不影响引擎运行。`post_processors_` 里的 `shape_processor` 同理（第 367-373 行）。

#### 4.5.4 代码实践

**实践目标**：定位 Formatter 在提交链上的位置，理解它是「最后一步变换」。

**操作步骤**：

1. 打开 [src/rime/engine.cc:251-257](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L251-L257)（`OnCommit`）。
2. 按顺序读这三行：`GetCommitText()`（拿到组合出的提交文本）→ `FormatText(&text)`（formatter 改写）→ `sink_(text)`（送出引擎）。
3. 思考：如果 `shape_formatter` 把半角 `a` 改写成全角 `ａ`，前端最终收到的会是哪个？

**需要观察的现象**：Formatter 作用于「拼装好的提交文本」，且发生在文本送出引擎（`sink_`）之前。它看不到候选菜单，只看到最终字符串。

**预期结果**：前端收到的是 formatter 改写**之后**的文本（全角 `ａ`）。这也是为什么「全角模式」下提交的字母会变成全角——formatter 在最后一步做了形状转换。

#### 4.5.5 小练习与答案

**练习 1**：Formatter 为什么不像 Processor/Segmentor/Translator/Filter 那样由方案的 `engine:` 配置清单装配，而是硬编码 `Require("shape_formatter")`？

**参考答案**：因为形状转换是「对所有方案都需要的、与具体输入法无关的」通用后处理，不属于「换方案就换」的可装配部分。把它硬编码为引擎的固定一环，既简化了方案配置，也保证了半角/全角行为在所有方案间一致。

**练习 2**：`Format(string* text)` 为什么用指针参数就地改写，而不是接收 `string` 返回 `string`？

**参考答案**：就地改写避免了字符串拷贝（提交文本可能较长），且多个 formatter 串联时只需维护一份字符串对象，直接在原串上累积变换，效率更高。`FormatText` 的循环也正是依赖这种「共享同一个 text 对象」的语义。

---

## 5. 综合实践

把本讲四个基类串起来，完成下面这张「流水线角色表」。这是本讲的核心实践任务。

**实践目标**：为 `Processor`、`Segmentor`、`Translator`、`Filter`（可加 `Formatter`）各写一行说明，描述它在 `ProcessKey` 流水线中的**触发时机**、**输入**、**输出**与**早退/合并规则**。

**操作步骤**：

1. 在 [src/rime/engine.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc) 中定位四个调用现场：`ProcessKey`（第 99-122 行）、`CalculateSegmentation`（第 171-201 行）、`TranslateSegments`（第 203-233 行）、`FormatText`（第 235-242 行）。
2. 对照四个基类头文件（processor.h / segmentor.h / translator.h / filter.h）的核心虚函数签名，填写下表。

**参考答案表**（填完后请自行对照）：

| 基类 | 触发时机 | 核心方法 | 输入 | 输出 | 链式规则 |
| --- | --- | --- | --- | --- | --- |
| Processor | 每次按键最先 | `ProcessKeyEvent` | `KeyEvent` | `ProcessResult` 三态 | kAccepted 立即返回；kRejected 中断；kNoop 继续 |
| Segmentor | Compose 阶段切分输入 | `Proceed` | `Segmentation*` | `bool` | 返回 false 即 break；同起点多 segmentor 叠加 tag |
| Translator | 切分后逐段查候选 | `Query` | `input` + `Segment&` | `an<Translation>` | 全部问完，nullptr/exhausted 跳过，结果合并入 Menu |
| Filter | 候选流装配后 | `Apply` | `Translation` + `CandidateList*` | `an<Translation>` | 洋葱式装饰，注册到 Menu 按需执行 |
| Formatter | 提交前最后一步 | `Format` | `string*` | （就地改写） | 依次串行改写，作用于最终提交文本 |

**进阶（可选）**：用一个真实方案印证。打开 [data/minimal/luna_pinyin.schema.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml)，找到 `engine:` 下的 `processors`/`segmentors`/`translators`/`filters` 四张清单，把每个组件名对号入座到上表的某一类（例如 `speller` 属 Processor、`abc_segmentor` 属 Segmentor、`script_translator` 属 Translator、`simplifier` 属 Filter）。这正是 `InitializeComponents` 通过 `CreateComponentsFromList` 把这四张清单分别填进四个容器的现实写照。

## 6. 本讲小结

- 四种主流水线基类 `Processor`/`Segmentor`/`Translator`/`Filter`（外加辅助的 `Formatter`）**骨架完全对称**：都继承 `Class<T, const Ticket&>`、都用 `Ticket` 构造、都持有 `engine_` 与 `name_space_`，区别只在那个核心纯虚函数。
- 这种「统一构造契约」让 `engine.cc` 里的**一个模板函数** `CreateComponentsFromList` 能通吃四类装配，换方案 = 换四张 YAML 清单，引擎代码不动。
- `Processor::ProcessKeyEvent` 返回三态：`kAccepted`（消耗，立即返回）、`kRejected`（中断链但落回 OS）、`kNoop`（传给下一个）。
- `Segmentor::Proceed(Segmentation*)` 返回 bool，多 segmentor 在同一起点协作叠加 tag，返回 false 即停。
- `Translator::Query(input, segment)` 返回 `an<Translation>`，所有 translator 对同一段全部问完、结果合并入 Menu。
- `Filter::Apply(translation, candidates)` 以装饰器方式层层包装候选流，`AppliesToSegment` 控制按段生效；`Formatter::Format(string*)` 则在提交前对最终文本就地改写。

## 7. 下一步学习建议

- **下一篇 u5-l3 Module 机制与模块组**：这些基类的具体子类（`speller`、`script_translator`、`simplifier` 等）是在哪里、用什么宏注册到 `Registry` 的？答案在 `core_module.cc`、`gears_module.cc` 等模块注册文件里。u5-l3 讲 `RIME_REGISTER_MODULE` 宏与 `default`/`deployer` 模块组如何把组件按批登记。
- **u5-l4 Ticket 与外部插件加载**：本讲反复出现的 `Ticket` 与 `klass@alias` 拆分将在 u5-l4 完整拆解，并引出 `PluginManager` 如何用 `boost::dll` 动态加载 `librime-*` 外部插件。
- **u6 按键处理流水线**：当你想看四大基类的**具体子类实现**时，进入 u6——u6-l2 讲 Processor 族、u6-l3 讲 Segmentor 族、u6-l4 讲 Translator 族、u6-l5 讲 Filter 族。
- **延伸阅读**：直接打开 [src/rime/gear/gears_module.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/gears_module.cc)，按四大类整理所有注册的组件名，能立刻把本讲的「抽象基类」与「具体齿轮」对应起来。
