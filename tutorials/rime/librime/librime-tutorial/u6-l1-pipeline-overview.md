# 引擎流水线总览

## 1. 本讲目标

本讲是进阶层「按键处理流水线」单元的开篇。前面几讲我们已经分别认识了引擎骨架（u2-l4）、输入状态容器 `Context`（u3-l1）、候选组织黑盒 `Segmentation`/`Composition`/`Translation`/`Candidate`（u3-l2、u3-l3）、惰性缓冲层 `Menu`（u3-l4），以及四种组件基类 `Processor`/`Segmentor`/`Translator`/`Filter` 的契约（u5-l2）。

本讲要把这些零件**串成一条完整的流水线**，回答一个核心问题：

> 一次按键从被前端喂进引擎，到最终变成候选词出现在菜单里，中间到底发生了什么？

读完本讲你应该能够：

- 说出 `ProcessKey` 中处理器（Processor）循环的**早退规则**：`kAccepted`、`kRejected`、`kNoop` 各自的语义与控制流后果。
- 解释 `Compose` 为什么分成 `CalculateSegmentation` 与 `TranslateSegments` 两个阶段，以及它们各自的职责。
- 看懂方案文件 `engine/{processors,segmentors,translators,filters}` 四张清单是如何在装配期被读取、解析、实例化成四组组件容器的。
- 在 `engine.cc` 里画出「按键 → 候选」的完整时序图。

本讲**只讲骨架与协作关系**，不深入任何具体组件子类（`speller`、`script_translator`、`simplifier` 等留待 u6-l2 ~ u6-l5 逐族展开）。

## 2. 前置知识

在进入流水线之前，先用三句话回顾几个本讲会反复用到的概念：

- **组件（Component）与处方串（prescription）**：方案 YAML 的 `engine` 段里写的 `speller`、`abc_segmentor`、`script_translator@pinyin` 这类字符串，叫做「处方串」。引擎在装配期用 `Ticket` 把它拆成「类名 + 命名空间」，再去 `Registry` 里按名造对象（详见 u5-l1、u5-l2、u5-l4）。
- **Notifier（信号）**：`Context` 内部有一组 Boost 信号槽（`update_notifier_`、`commit_notifier_`、`select_notifier_` 等）。组件之间**不直接互调**，而是靠发信号协作。引擎在构造期就把自己的一组回调「订阅」到了这些信号上（详见 u3-l1）。
- **拉模型（lazy / pull）**：候选不是一次性全部算出来的。`Menu`/`Translation` 是惰性迭代器，前端要看几条才算几条（详见 u3-l3、u3-l4）。

如果你对上面任何一点感到陌生，建议先回看对应讲义再继续。

## 3. 本讲源码地图

本讲几乎所有代码都集中在一个文件里：

| 文件 | 作用 |
| --- | --- |
| `src/rime/engine.h` | `Engine` 抽象基类：持有 `schema_`/`context_`、定义四个核心虚函数（`ProcessKey`/`ApplySchema`/`CommitText`/`Compose`）与静态工厂 `Create`。 |
| `src/rime/engine.cc` | 全部实现都在这里：隐藏子类 `ConcreteEngine`、`ProcessKey` 处理器循环、`Compose` 两阶段、装配模板 `CreateComponentsFromList`。 |

辅助确认契约的基类头文件（本讲只引用签名，不展开实现）：

| 文件 | 关键契约 |
| --- | --- |
| `src/rime/processor.h` | `ProcessResult` 三态枚举、`Processor::ProcessKeyEvent` 签名。 |
| `src/rime/segmentor.h` | `Segmentor::Proceed` 签名。 |
| `src/rime/translator.h` | `Translator::Query` 签名。 |
| `src/rime/filter.h` | `Filter::Apply` / `AppliesToSegment` 签名。 |
| `src/rime/menu.h` | `Menu::AddTranslation` / `AddFilter` 装配接口。 |
| `src/rime/segmentation.h` | `Segmentation` 的 `Forward`/`Trim`/`HasFinishedSegmentation` 等推进方法。 |
| `data/minimal/luna_pinyin.schema.yaml` | 真实方案的 `engine` 段，用于印证装配逻辑。 |

## 4. 核心概念与源码讲解

本讲把流水线拆成四个最小模块，**按执行先后顺序**讲解：

1. **装配阶段**：方案配置如何变成四组组件容器（引擎一启动就发生）。
2. **`ProcessKey`**：按键如何穿过处理器链（按键旅程的前半段）。
3. **`Compose` 入口 + `CalculateSegmentation`**：上下文更新如何触发切分（旅程后半段的第一阶段）。
4. **`TranslateSegments`**：候选如何被生产与过滤并挂到段落上（旅程后半段的第二阶段）。

### 4.1 装配阶段：方案 engine 配置如何变成组件容器

#### 4.1.1 概念说明

在讲按键之前，必须先讲清楚一件事：**处理器、切分器、翻译器、过滤器这四类组件，到底是什么时候、怎么被造出来的？**

答案是一次性的「装配阶段（assembly）」。引擎在创建时（以及每次切换方案时）会读取当前方案的 `engine` 配置段，把里面四张清单分别实例化成四个 `vector`：

```cpp
vector<of<Processor>> processors_;
vector<of<Segmentor>> segmentors_;
vector<of<Translator>> translators_;
vector<of<Filter>> filters_;
```

这就是流水线后续要遍历的「零件清单」。换方案 = 换这四张清单，引擎代码本身一行都不用动。这是 librime 可扩展性的根基。

#### 4.1.2 核心流程

装配的核心是一个模板函数 `CreateComponentsFromList`，它对四类组件完全通用：

```
对配置里的 engine/{processors|segmentors|translators|filters} 列表：
  取出第 i 个元素（一个 ConfigValue，即处方串）
  ┌─ 构造 Ticket{engine, component_type, prescription_str}
  │   Ticket 会把 "script_translator@pinyin" 拆成
  │     klass = "script_translator", name_space = "pinyin"
  ├─ T::Require(ticket.klass)   → 查 Registry 拿到带类型的工厂
  ├─ factory->Create(ticket)    → 真正 new 出组件对象
  └─ push_back 到对应容器
  任何一步失败（找不到类 / 造不出）→ 只记 ERROR 日志，跳过这个组件，继续下一个
```

容错是这个设计的关键：**缺件不会让引擎崩溃**，只会少一个功能并打一条错误日志。这样即使某个方案引用了一个未安装插件提供的组件，引擎仍能跑起来。

#### 4.1.3 源码精读

`ConcreteEngine` 持有四组容器，外加一组格式化器（formatters）和后处理器（post_processors）：

[engine.cc:49-55](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L49-L55) —— 这里声明了流水线的全部容器。注意前四组（processors/segmentors/translators/filters）对应方案四张清单，后两组（formatters/post_processors）是固定的、不由方案配置。

装配的真正入口是 `InitializeComponents`：

[engine.cc:328-374](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L328-L374) —— 关键逻辑分三层：

1. **清空旧容器**（329-334 行）：换方案时会再次调用此函数，必须先把旧组件全清掉。
2. **切换器优先**（336-343 行）：`switcher_`（方案切换器，本身是个 Processor）被无条件插到 `processors_` 最前面；如果当前还是占位方案 `.default`，就用切换器造出真实方案替换掉。
3. **四张清单并行装配**（350-357 行）：用同一个模板各读一张清单。

通用模板本体：

[engine.cc:297-326](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L297-L326) —— 这段是「数据驱动装配」的精华。`config->GetList(config_key)` 读出 YAML 列表，逐个元素取处方串，造 `Ticket`，`Require` + `Create` 两步实例化。注意 311-315 行与 317-321 行的**双重容错**：工厂找不到和工厂造不出是两种不同的失败，分别记不同日志。

#### 4.1.4 代码实践

**实践目标**：用真实方案印证「清单 → 容器」的映射关系。

**操作步骤**：

1. 打开 `data/minimal/luna_pinyin.schema.yaml`，定位到 `engine:` 段（第 39 行起）。
2. 数一下 `processors`、`segmentors`、`translators`、`filters` 四张清单各有几项。
3. 对照 `InitializeComponents` 里四次 `CreateComponentsFromList<...>` 的调用，确认每张清单被填进了哪个容器。

**需要观察的现象**：

- `processors` 列表里有 8 项（`ascii_composer`、`recognizer`、`key_binder`、`speller`、`punctuator`、`selector`、`navigator`、`express_editor`），它们会**按这个顺序**进入 `processors_`，按键也将**按这个顺序**依次穿过它们——顺序很重要。
- `translators` 列表里有 6 项，其中 `table_translator@cangjie` 和 `script_translator@pinyin` 是**同一个组件类用不同 namespace 实例化两次**，这正是 `Ticket` 拆解 `@` 后缀的意义。
- `switcher_` 不在 YAML 清单里，却出现在 `processors_` 最前面——它是引擎硬塞进去的。

**预期结果**：你能说出「方案 YAML 的 engine 段四张清单，与 `ConcreteEngine` 的四个 `vector` 成员一一对应，且 `switcher_` 总在最前」。

### 4.2 ProcessKey：处理器链与早退规则

#### 4.2.1 概念说明

按键旅程的**前半段**是「处理器循环」。`ProcessKey` 把同一个 `KeyEvent` 依次喂给 `processors_` 里的每个 Processor，每个 Processor 用三态返回值表态：

| 返回值 | 含义 | 对控制流的后果 |
| --- | --- | --- |
| `kAccepted` | 「我要了，吃掉这个键」 | 立即结束整个 `ProcessKey`，返回 `true`，**不再问后续处理器** |
| `kRejected` | 「我处理不了，别再问我这组了」 | `break` 跳出 `processors_` 循环，进入**记录与后处理** |
| `kNoop` | 「我不管，交给下一位」 | 继续问下一个处理器 |

注意 `kRejected` 字面像「拒绝」，但它的真实语义是「短路跳出主循环」，而不是报错。任何一个 Processor 返回 `kRejected`，整组处理器就停下来——因为后面的处理器往往依赖前面的判断（比如 `speller` 说不接受，`selector` 也没必要再看）。

#### 4.2.2 核心流程

```
ProcessKey(key):
  for processor in processors_:
      ret = processor->ProcessKeyEvent(key)
      if ret == kRejected: break        # 短路跳出
      if ret == kAccepted: return true  # 吃掉，整个函数结束
  # —— 到这里说明没有处理器接受这个键 ——
  commit_history.Push(key)              # 记录未处理键（空格、数字、退格等）
  for processor in post_processors_:    # 后处理器（如形状处理）
      ret = processor->ProcessKeyEvent(key)
      if ret == kRejected: break
      if ret == kAccepted: return true
  unhandled_key_notifier(key)           # 通知「这个键没人要」
  return false                          # 让 OS 走默认处理（比如直接上屏）
```

返回 `true`/`false` 给调用方（Session → C API → 前端）的含义是：**这个键引擎是否消化了**。返回 `false` 时，前端会让操作系统按默认行为处理（例如把字母直接上屏）。

#### 4.2.3 源码精读

`ProcessKey` 的主循环与早退逻辑：

[engine.cc:99-122](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L99-L122) —— 102-108 行是主处理器循环，三个返回值三个分支；110 行把未被接受的键塞进提交历史；112-118 行是结构完全相同的后处理器循环；120 行触发 `unhandled_key_notifier`。

三态枚举的定义（语义注释很清楚）：

[processor.h:18-22](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/processor.h#L18-L22) —— 注意注释点出 `kRejected` 对应「让 OS 走默认处理」，`kAccepted` 对应「吃掉它」。

> **关键衔接点**：`ProcessKey` 本身**不直接调用 `Compose`**。它只负责让处理器有机会改 `Context`（典型如 `speller` 把字母 `PushInput` 进 `context->input`）。真正触发 `Compose` 的是 `Context` 的 `update_notifier_` 信号——任何修改 `input` 的操作都会发这个信号，而引擎在构造期就订阅了它。

构造期的信号订阅：

[engine.cc:74-85](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L74-L85) —— 这里把 `Context` 的五个信号分别连到引擎的五个回调。其中 `update_notifier_`（76-77 行）连到 `OnContextUpdate`，这就是「输入变了就重算候选」的入口。

#### 4.2.4 代码实践

**实践目标**：亲手验证早退规则如何决定一次按键的命运。

**操作步骤**：

1. 在 [engine.cc:99-122](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L99-L122) 的 `ProcessKey` 里，给每个 `if (ret == ...)` 分支加一行临时日志（**仅本地调试用，勿提交**），例如：
   ```cpp
   if (ret == kAccepted) { LOG(INFO) << "accepted by processor"; return true; }
   ```
2. 用 `rime_api_console`（u1-l5 介绍的工具）启动引擎，依次输入：
   - 一个字母（如 `n`）—— 预期被 `speller` 接受；
   - 一个回车 —— 预期落到 `commit_history` 和后处理器，最终 `unhandled_key`。
3. 观察日志，确认每个按键分别命中了哪个分支。

**需要观察的现象**：输入字母时日志应出现 `accepted` 且函数提前返回；输入回车时应看到键被推进 `commit_history` 并触发 `unhandled_key_notifier`，函数返回 `false`。

**预期结果**：你能用三态返回值解释清楚「为什么打字母时候选立即出现，而按回车时字母才上屏」。

> 注意：若你没有启用 `ENABLE_LOGGING`（见 u1-l2），`LOG(INFO)` 不产生输出，本实践需在开启日志的构建下进行；否则改为「源码阅读型实践」——只读代码推演分支即可。

### 4.3 Compose 入口与 CalculateSegmentation（第一阶段）

#### 4.3.1 概念说明

当 `Context` 的 `input` 被处理器修改（比如 `speller` 追加了一个字母），`update_notifier_` 触发 `OnContextUpdate`，后者直接调用 `Compose`。`Compose` 是按键旅程的**后半段**，它把当前输入串重新组织成候选。

`Compose` 分两阶段：

1. **`CalculateSegmentation`（切分）**：把一整串输入切成若干个 `Segment`（段落），每段打上 `tags`。比如输入 `nihao` 可能被切成 `ni` + `hao` 两段，或保持成一段 `nihao`——切分器（Segmentor）会尝试所有合理边界。
2. **`TranslateSegments`（翻译）**：对每个新切出来的段落，问遍所有翻译器拿候选，再用过滤器包装，最后挂一个 `Menu` 到该段。

本模块只讲第一阶段。

#### 4.3.2 核心流程

```
OnContextUpdate(ctx):  →  Compose(ctx)

Compose(ctx):
  active_input = input 截到光标位置          # 通常光标在末尾，active_input == 全部输入
  comp.Reset(active_input)                  # 增量重置：保留已确认前缀，丢弃受影响尾部
  if 光标在输入中间 且 光标前已全部确认:
      comp.Reset(完整 input)                # 特例：允许翻译光标之后的那一段
  CalculateSegmentation(&comp)              # 【第一阶段】
  TranslateSegments(&comp)                  # 【第二阶段】

CalculateSegmentation(segments):
  while not HasFinishedSegmentation():
      start_pos = 当前段起点
      for segmentor in segmentors_:         # 所有切分器在同一起点协作
          if not segmentor->Proceed(segments): break
      if 当前终点没推进: break               # 防死循环
      if 起点已越过光标: break               # 光标之后只允许一段
      segments->Forward()                   # 腾出新格，进入下一段
  收尾：Trim 掉空段；若末段已选中则再 Forward 留个空段
```

切分器之间是**协作**关系，不是竞争：多个 segmentor 在**同一个起点**轮流表态，每个 `Proceed` 可以给当前段追加 `tags`、延伸 `end`。只有当所有 segmentor 都表态完、当前段仍无法推进时，循环才停下。

#### 4.3.3 源码精读

`OnContextUpdate` 是 `update_notifier_` 的回调，一行委托给 `Compose`：

[engine.cc:124-128](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L124-L128)

`Compose` 主体，注意光标特例处理：

[engine.cc:154-169](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L154-L169) —— 158 行算 `active_input`；161-165 行是「光标在中间」的特例：只有当光标之前的输入恰好都已确认（`GetConfirmedPosition()` 等于光标位置）时，才把完整输入交给后续处理，目的是翻译光标之后的那一段。

`CalculateSegmentation` 的循环与防死循环保护：

[engine.cc:171-201](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L171-L201) —— 180-183 行是切分器协作循环；186-187 行「终点未推进就 break」是**防死循环**的关键（否则段永远不前进，while 会无限转）；190-191 行限制「光标之后只允许有一段」；197-200 行是收尾清理。

`Segmentation` 提供的推进原语：

[segmentation.h:64-74](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/segmentation.h#L64-L74) —— `Forward` 腾出新段、`Trim` 撤掉空段、`HasFinishedSegmentation` 判断是否已覆盖到输入末尾。这些是第一阶段循环依赖的基础方法（其内部实现在 u3-l2 已讲）。

#### 4.3.4 代码实践

**实践目标**：理解切分器如何协作产生段落边界。

**操作步骤**：

1. 阅读 [engine.cc:171-201](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L171-L201) 的 `CalculateSegmentation`。
2. 假设用户输入了 `nihao`（光标在末尾），推演循环：
   - 第 1 轮：起点 `0`，所有 segmentor 在 `[0, ?)` 上表态，假设 `abc_segmentor` 把它识别为一段、终点推进到 `5`；
   - 终点推进了 → `Forward` → 进入第 2 轮；
   - 第 2 轮：起点 `5` 已到末尾，`HasFinishedSegmentation()` 为真，退出循环。
3. 再假设输入 `ni'hao`（带分隔符 `'`），推演它如何被切成两段。

**需要观察的现象**：你能指出「终点是否推进」这一条件如何同时承担了「正常前进」和「防死循环」两种职责。

**预期结果**：你能口述出「切分循环 = 协作表态 + 前进判定 + 防死循环」三要素。

### 4.4 TranslateSegments（第二阶段）

#### 4.4.1 概念说明

第二阶段对第一阶段切出来的每个段落做两件事：**问候选**和**过滤候选**。

- **问候选**：对每一段，把段落文本和段落对象一起，依次问遍所有 `translators_`。每个翻译器返回一个惰性的 `Translation`（候选流），只要它非空且未耗尽，就 `AddTranslation` 进该段的 `Menu`。
- **过滤候选**：再遍历所有 `filters_`，对声明「适用于本段」的过滤器，`AddFilter` 进同一个 `Menu`。过滤器并不立刻运行，而是以**装饰器洋葱**的形式包在候选流外层（详见 u3-l4）。

最后给该段挂上 `menu`、把状态从 `kVoid` 抬到 `kGuess`（表示「已有候选猜测」）、重置 `selected_index = 0`。

#### 4.4.2 核心流程

```
TranslateSegments(segments):
  for segment in segments:
      if segment.status >= kGuess: continue   # 已经算过的段不重算
      input = segments.input 截取 [start, end)
      menu = new Menu
      for translator in translators_:
          t = translator->Query(input, segment)
          if not t or t->exhausted(): continue   # 空翻译或一上来就耗尽 → 跳过
          menu->AddTranslation(t)
      for filter in filters_:
          if filter->AppliesToSegment(&segment):
              menu->AddFilter(filter)
      segment.status = kGuess
      segment.menu = menu
      segment.selected_index = 0
```

`status >= kGuess` 的跳过逻辑（208-209 行）是**增量计算**的关键：用户编辑输入时，已经确认或已经有候选的段不会被重新翻译，只有受影响的尾部段才会重算。

#### 4.4.3 源码精读

`TranslateSegments` 主体：

[engine.cc:203-233](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L203-L233) —— 208-209 行的 `status >= kGuess` 跳过；214-223 行问遍翻译器，注意 218-221 行对「一上来就耗尽」的翻译器也跳过（避免把空流塞进 Menu）；224-228 行挂过滤器，由 `AppliesToSegment` 决定是否生效；229-231 行更新段状态、挂 Menu、重置选中索引。

翻译器与过滤器的契约签名：

- [translator.h:28-29](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translator.h#L28-L29) —— `Query(input, segment)` 返回 `an<Translation>`，返回空表示「本翻译器对这段没话可说」。
- [filter.h:28-31](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/filter.h#L28-L31) —— `Apply` 包装候选流，`AppliesToSegment` 默认对所有段返回 `true`。

`Menu` 的装配接口：

[menu.h:31-35](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/menu.h#L31-L35) —— `AddTranslation` 把翻译器塞进 k 路归并器，`AddFilter` 把过滤器包到链头。这两个调用都发生在**装配期**，真正的候选求值要等到前端读菜单时才发生（拉模型，详见 u3-l4）。

> **衔接后续**：用户从菜单里选中某条候选时，`Context::Select` 会触发 `select_notifier_`，引擎的 `OnSelect` 回调据此推进段状态、可能触发整段提交或继续编辑下一段：

[engine.cc:259-282](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L259-L282) —— 这段属于「选中之后」的逻辑，本讲只需知道它存在；最终提交文本会经 `OnCommit`（251-257 行）取出、`FormatText` 格式化后，通过 `sink_` 推给 Session（拉模型提交，详见 u2-l2、u2-l4）。

#### 4.4.4 代码实践

**实践目标**：用 `luna_pinyin` 方案印证「翻译器 + 过滤器」如何挂到同一段。

**操作步骤**：

1. 打开 `data/minimal/luna_pinyin.schema.yaml` 的 `engine/translators` 与 `engine/filters` 两张清单。
2. 对照 [engine.cc:214-228](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L214-L228) 推演：对于一段普通的拼音输入，`script_translator` 会返回候选，而 `table_translator@cangjie`（仓颉）多半返回空（因为这段不是仓颉码），`punct_translator` 也返回空。于是该段的 Menu 实际只收入了 `script_translator` 的候选。
3. 再看 `filters`：`simplifier@zh_simp`、`simplifier@zh_tw`、`uniquifier` 三个都会经 `AppliesToSegment` 判断后挂上，形成 `uniquifier(simplifier@zh_tw(simplifier@zh_simp(...)))` 的洋葱。

**需要观察的现象**：同一段输入，被多个翻译器查询但只有部分返回非空候选；所有过滤器（若都适用）都会挂上，形成嵌套包装。

**预期结果**：你能解释「为什么一个拼音候选最终可能同时显示简体、繁体两种字形」——因为多个 `simplifier` 过滤器在同一候选流上叠加（具体机制留待 u6-l5）。

### 4.5 小练习与答案

**练习 1**：如果一个方案的 `engine/processors` 清单里写了一个不存在的组件名（比如拼错了 `spelr`），引擎启动会怎样？

> **答案**：`CreateComponentsFromList` 在 `T::Require(ticket.klass)` 返回空时，记一条 `LOG(ERROR)` 并 `continue` 跳过（见 [engine.cc:310-315](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L310-L315)）。引擎**不会崩溃**，只是少了一个处理器——结果是该处理器负责的功能失效（比如拼错的是 `speller`，则字母不会被追加进输入）。

**练习 2**：在 `ProcessKey` 中，`kRejected` 和 `kAccepted` 都会让处理器循环停下来，但它们的后续控制流完全不同。请说出区别。

> **答案**：`kAccepted` 直接 `return true` 结束整个 `ProcessKey`，键被「吃掉」，**不记录到 commit_history、不进后处理器、不触发 unhandled_key_notifier**。`kRejected` 只是 `break` 跳出**主处理器循环**，之后**仍会**记录到 commit_history、继续走 post_processors 循环、最后触发 unhandled_key_notifier 并返回 `false`（让 OS 走默认处理）。

**练习 3**：为什么 `CalculateSegmentation` 的循环里需要 `if (start_pos == segments->GetCurrentEndPosition()) break;` 这一行？

> **答案**：这是**防死循环保护**（见 [engine.cc:186-187](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L186-L187)）。如果一轮下来所有 segmentor 都没能把当前段的终点往前推进，而 `HasFinishedSegmentation()` 又仍为假，`while` 就会无限空转。这一行检测到「无推进」就主动 break，保证循环必然终止。

## 5. 综合实践

**任务**：画出 `luna_pinyin` 方案下「一次按键到候选产出」的完整时序图，并标注每一步对应的源码位置。

**要求**：

1. 选定一个具体场景：用户已输入 `ni`，现在按下 `h` 键。
2. 从 C API 的 `process_key` 出发（可参考 u1-l4、u1-l5），一直画到候选出现在 `Context::composition()` 的某段 `menu` 里为止。
3. 时序图至少包含以下泳道/步骤，并在每步旁边标注 `[engine.cc:行号]`：
   - `process_key` → `KeyEvent` 封装 → `Session` → `Engine::ProcessKey`
   - 处理器循环：`ascii_composer`（kNoop）→ `recognizer`（kNoop）→ `speller`（**kAccepted**，`PushInput('h')`）
   - `PushInput` 触发 `update_notifier_` → `OnContextUpdate` → `Compose`
   - `Compose`：`Reset` → `CalculateSegmentation`（segmentor 协作）→ `TranslateSegments`（translator 查询 + filter 挂载）
   - 候选挂在某段的 `menu` 上
4. 用一段文字解释：为什么按下 `h` 之后候选菜单会**自动**更新？答案应点明「处理器只改输入，更新靠 `update_notifier_` 信号驱动 `Compose`」这条关键链路。

**交付物**：一张时序图（手绘或工具均可）+ 一段说明文字。

**提示**：如果你暂时无法运行引擎，可以只做「源码阅读型」版本——纯靠读 [engine.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc) 推演每一步的行号与数据流，这同样能达到本讲的训练目标。

## 6. 本讲小结

- 流水线由**装配期**和**运行期**两部分组成：装配期把方案 `engine` 段四张清单实例化成 `processors_`/`segmentors_`/`translators_`/`filters_` 四组容器；运行期按按键驱动它们。
- `ProcessKey` 用三态返回值（`kAccepted`/`kRejected`/`kNoop`）实现处理器链的早退：`kAccepted` 立即结束并吃掉按键，`kRejected` 短路跳出主循环但仍记录历史与走后处理，`kNoop` 继续往下问。
- `Compose` 分两阶段：`CalculateSegmentation`（切分器协作切出段落，带防死循环保护）+ `TranslateSegments`（翻译器问候选 + 过滤器洋葱包装 + 挂 Menu）。
- **处理器不直接触发 `Compose`**：处理器只修改 `Context`，由 `update_notifier_` 信号回调 `OnContextUpdate` → `Compose`，这是「输入变了就重算」的唯一入口。
- 装配具备**容错性**：缺件只记 ERROR 不崩溃；翻译具备**增量性**：`status >= kGuess` 的段不重算。
- 候选求值是**拉模型**：`TranslateSegments` 只装配 `Menu` 的翻译器与过滤器链，真正算候选要等前端读菜单时才发生。

## 7. 下一步学习建议

本讲只搭起了流水线的骨架与协作关系，所有具体组件都被当黑盒。接下来按 u6-l2 ~ u6-l5 逐族打开：

- **u6-l2 Processor 组件族**：看 `speller` 如何把按键追加进输入、`ascii_composer`/`recognizer`/`key_binder`/`punctuator` 各管什么、`express_editor` 等编辑器在按下回车/退格时做什么。
- **u6-l3 Segmentor 组件族**：看 `abc_segmentor`、`affix_segmentor@xxx`、`fallback_segmentor` 如何给段落打 `abc`/`raw`/`pinyin` 等 tag。
- **u6-l4 Translator 组件族**：看 `script_translator`（拼音）与 `table_translator`（仓颉）的差异，以及本讲黑盒的 `Query` 内部如何查词典。
- **u6-l5 Filter 组件族**：看本讲黑盒的 `Apply` 如何实现繁简转换（`simplifier` + OpenCC）与去重（`uniquifier`）。

如果想先看「候选是怎么从词典里被查出来的」，也可以跳到 u8（词典系统）；但建议先按 u6 顺序读完四族组件，再进 u8 会更顺。
