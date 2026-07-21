# Component / Class / Registry 组件体系

## 1. 本讲目标

本讲是「组件与模块架构」单元（u5）的第一篇。学完本讲，读者应该能够：

- 说清楚 librime 为什么需要一套「组件注册体系」，它解决了什么问题。
- 看懂 `Class<T, Arg>` 模板如何用「内嵌 `Component` 子类 + 纯虚 `Create`」定义出一类组件的**创建接口**。
- 理解 `Component<T>` 默认工厂模板如何用一行 `new T(arg)` 免去手写工厂的重复劳动。
- 掌握 `Registry` 单例的 `Register / Find / Unregister` 三件套与「同名覆盖即替换」的语义。
- 读懂 `Class<T, Arg>::Require(name)` 这条「按名查表 → 实例化」的完整调用链，并能解释 sample 插件里 `trivial_translator` 是如何被注册和取出的。

本讲只讲**注册基础设施**本身；具体注册了哪些组件（Processor / Segmentor / Translator / Filter 等）留到 u5-l2、u5-l3，外部插件如何被 `boost::dll` 加载留到 u5-l4。

## 2. 前置知识

阅读本讲前，读者应已具备（见前置讲义摘要）：

- **配置即组件**：u4-l2 已说明 `Config` 本身就是一种组件（`Config` 继承 `Class<Config, const string&>`），配置树由 `Config::Component` 这个工厂生产。本讲把这套机制抽象出来，讲它背后的通用骨架。
- **Engine 装配清单**：u2-l4 提到 `ConcreteEngine::InitializeComponents` 会读取方案 `engine/{processors,segmentors,translators,filters}` 四张清单，把每个「处方串」（如 `script_translator`）变成活生生的 C++ 对象。本讲就讲这个「串 → 对象」的魔法底座。
- **智能指针别名**：u4-l1 引入了 `the<T>` / `an<T>` / `of<T>` / `New<T>` 等别名。本讲会再次用到 `the<Registry>`（独占指针）。
- **C++ 模板基础**：类模板、成员类（nested class）、纯虚函数、`dynamic_cast`。

一句话回顾动机：librime 的引擎是**数据驱动**的——换一个方案 YAML，引擎流水线上的组件就完全不同。引擎代码里不能写死「拼音用 `ScriptTranslator`、仓颉用 `TableTranslator`」，否则每加一个输入法就要改引擎源码。解决办法是：所有可替换的零件都实现统一接口，启动时把「名字 → 工厂」登记进一张全局表，引擎运行时按 YAML 里写的名字去这张表里「要一个实例」。这张表就是 `Registry`，登记动作就是 `Register`，按名索取就是 `Require`。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/rime/component.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/component.h) | 定义组件体系的全部抽象：`ComponentBase` 基类、`Class<T, Arg>` 模板（含内嵌 `Component` 接口与 `Require` 静态方法）、`Component<T>` 默认工厂模板。整个文件不到 40 行，是本讲的核心。 |
| [src/rime/registry.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/registry.h) | 声明 `Registry` 单例：一张 `map<string, ComponentBase*>`，提供 `Register / Find / Unregister / Clear`。 |
| [src/rime/registry.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/registry.cc) | `Registry` 的实现，包括「同名覆盖即 delete 旧值」、惰性单例 `instance()`。 |
| [sample/src/sample_module.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/src/sample_module.cc) | 实战样例：sample 插件如何用一行 `new Component<sample::TrivialTranslator>` 把自己注册成 `trivial_translator`。 |
| [src/rime/translator.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translator.h) | 印证：具体组件基类 `Translator` 如何「长出」组件接口（`public Class<Translator, const Ticket&>`）。 |
| [src/rime/engine.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc) | 印证：`CreateComponentsFromList` 模板如何用 `T::Require(...)` + `Create(...)` 把 YAML 处方串变成对象。 |

记忆口诀：**`component.h` 管「怎么定义和造」，`registry.h/cc` 管「造好登记在哪」，`Require` 是「按名取货」**。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，按「定义接口 → 默认工厂 → 登记表 → 取货调用链」的顺序推进。

### 4.1 Class\<T, Arg\> 与 ComponentBase：组件的接口契约

#### 4.1.1 概念说明

librime 里有四大类可插拔零件（Processor / Segmentor / Translator / Filter），未来还会更多。它们彼此毫无继承关系（一个翻译器不是一个处理器），但引擎需要用**同一种方式**去「按名字造一个出来」。于是需要一个**与具体类型无关**的通用基类，只为了一件事：让 `Registry` 能用统一指针 `ComponentBase*` 存放所有「组件工厂对象」，而不论它们造的是翻译器还是过滤器。

`ComponentBase` 就是这个统一根基类：它没有任何业务方法，只有一个虚析构函数，纯粹是为了让所有工厂对象能被多态地放进同一张表、并安全 `delete`。

但光有 `ComponentBase` 不够——`Registry` 存的是「工厂」，可「造翻译器」和「造处理器」是两种不同的造法，返回类型也不同。librime 用一个巧妙设计解决：**把「工厂接口」内嵌进目标类自身**。具体说，由 `Class<T, Arg>` 模板提供一个内嵌类 `Component`，声明纯虚 `T* Create(Arg)`；任何想成为「可注册组件」的类（如 `Translator`）只要 `public Class<Translator, const Ticket&>`，就自动「长出」一个与之匹配的工厂接口。

#### 4.1.2 核心流程

`Class<T, Arg>` 的角色分工：

```text
想被注册的目标类 T（如 Translator）
        │  public Class<T, Arg>  继承
        ▼
Class<T, Arg>                 ← 模板，T=产品类型，Arg=构造参数类型
   ├─ using Initializer = Arg;
   └─ class Component : virtual public ComponentBase   ← 内嵌「工厂接口」
           └─ virtual T* Create(Initializer arg) = 0; ← 纯虚：每个具体工厂自己实现
```

要点：

- `T` 是**产品类型**（造出来的是什么，如 `Translator`）。
- `Arg` 是**构造参数类型**（用什么造，如 `const Ticket&`），别名成 `Initializer`。
- 内嵌 `Component` 才是真正的「工厂抽象」，它的 `Create` 是纯虚函数，等具体工厂去实现。
- `Component` 虚继承 `ComponentBase`，从而既能被 `Registry` 以 `ComponentBase*` 统一存放，又能被 `dynamic_cast` 还原回 `Class<T,Arg>::Component*` 拿到带类型的 `Create`（见 4.4）。

#### 4.1.3 源码精读

`ComponentBase` 极简，只为统一存放与安全析构：

[component.h:14-18](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/component.h#L14-L18) 定义了只有默认构造与虚析构的根基类。

[component.h:20-32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/component.h#L20-L32) 是本模块的主角 `Class<T, Arg>` 模板：`Initializer` 是 `Arg` 的别名（让「初始化参数」这个词更直观）；内嵌 `Component` 虚继承 `ComponentBase`，声明纯虚 `Create(Initializer arg)`。

印证：`Translator` 真的就是这么「长出」工厂接口的——

[translator.h:22](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translator.h#L22) 写着 `class Translator : public Class<Translator, const Ticket&>`。这一行同时做了两件事：让 `Translator` 拥有内嵌类型 `Translator::Component`（其 `Create` 签名为 `Translator* Create(const Ticket&)`），并让该 `Component` 成为 `ComponentBase` 的子类从而可被 `Registry` 统一存放。

#### 4.1.4 代码实践

1. **实践目标**：体会「内嵌工厂接口」的写法，理解为什么 `Registry` 能用一种指针类型存所有工厂。
2. **操作步骤**：
   - 打开 [src/rime/processor.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/processor.h)、[src/rime/segmentor.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/segmentor.h)、[src/rime/filter.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/filter.h)，分别找到它们继承 `Class<..., ...>` 的那一行。
   - 记录每个类的 `T` 与 `Arg` 分别是什么。
3. **需要观察的现象**：四大基类的 `Arg` 都是 `const Ticket&`，`T` 各自是自己（`Processor` / `Segmentor` / `Filter`）。
4. **预期结果**：你能填出下面这张表——

   | 基类 | T | Arg |
   | --- | --- | --- |
   | `Processor` | `Processor` | `const Ticket&` |
   | `Segmentor` | `Segmentor` | `const Ticket&` |
   | `Translator` | `Translator` | `const Ticket&` |
   | `Filter` | `Filter` | `const Ticket&` |

5. 结论：`Registry` 里存的是 `ComponentBase*`，而 `Processor::Component*` 与 `Translator::Component*` 都能 `dynamic_cast` 自它——这就是统一存放、按需还原的关键。

#### 4.1.5 小练习与答案

**练习 1**：为什么不直接让 `Translator` 自己虚继承 `ComponentBase`，而要套一层 `Class<T, Arg>` 模板？

**答案**：因为不同组件的 `Create` 返回类型（`T*`）和参数类型（`Arg`）不同，无法在 `ComponentBase` 里写一个「万能 `Create`」。`Class<T, Arg>` 模板把 `T`、`Arg` 绑进内嵌 `Component` 的 `Create` 签名，既让每种组件拥有**类型正确**的工厂接口，又通过共同基类 `ComponentBase` 保证它们能被统一存放。模板 = 「为每种组件量身生成一套类型安全的工厂接口」。

**练习 2**：`Component` 为什么要 `virtual public ComponentBase`（虚继承）而不是普通继承？

**答案**：为多重继承下的「菱形结构」留余地。具体工厂（见 4.2 的 `Component<T>`）既要实现 `Class<T,Arg>::Component` 的接口，未来若某类还想混入别的 `ComponentBase` 派生接口，虚继承可保证 `ComponentBase` 子对象唯一，避免二义性。对当前代码而言这是预防性设计。

---

### 4.2 Component\<T\>：默认工厂模板

#### 4.2.1 概念说明

有了 `Class<T, Arg>::Component` 这个「工厂接口」后，最常见的需求是：某个具体类 `T` 就是想用「直接 `new T(arg)`」这种方式被造出来——不需要任何定制逻辑。如果每个这样的类都得手写一个 `Component` 子类去重写 `Create`，重复代码会非常多。

`Component<T>` 模板就是为消除这种重复而生的「默认工厂」。它继承 `T::Component`（也就是 `Class<T, Arg>::Component`），把 `Create` 实现为最朴素的 `new T(arg)`。于是任何带「单参数构造函数」的类，只要写 `new Component<MyClass>` 就能得到一个现成的工厂对象，注册到 `Registry` 即可。

#### 4.2.2 核心流程

```text
用户写：  new Component<sample::TrivialTranslator>
                    │
                    │  Component<T> : public T::Component
                    ▼
          T = sample::TrivialTranslator
          T::Component = Class<TrivialTranslator, const Ticket&>::Component
                    │
                    │  Create(arg) { return new T(arg); }
                    ▼
          返回一个 new sample::TrivialTranslator(ticket)
```

关键点：

- `Component<T>` 继承的是 `T::Component`，而 `T` 必须已经 `public Class<T, Arg>`（否则没有 `T::Component` 这个内嵌类型）——这是一种**概念约束（concept）**：只有「长得像可注册组件」的类才能用 `Component<T>`。
- `Create` 内部 `new T(arg)` 要求 `T` 有一个接受 `Arg` 的构造函数；这正是各组件基类（如 `Translator(const Ticket&)`）规定子类要提供的构造签名。
- `new Component<T>` 得到的是一个**工厂对象**（放在堆上，由 `Registry` 持有并最终 `delete`），不是产品本身。产品（`T` 的实例）是稍后调用 `Create` 时才造出来的。

#### 4.2.3 源码精读

整个默认工厂只有 5 行：

[component.h:34-38](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/component.h#L34-L38) 定义 `Component<T>`，继承 `T::Component`；[component.h:37](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/component.h#L37) 的 `Create` 就是 `return new T(arg);`。

最典型的应用在 sample 插件里：

[sample/src/sample_module.cc:19](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/src/sample_module.cc#L19) 写着 `r.Register("trivial_translator", new Component<sample::TrivialTranslator>);`。这里 `new Component<sample::TrivialTranslator>` 造的是**工厂**，`"trivial_translator"` 是它的**注册名**。日后引擎调用 `Translator::Require("trivial_translator")` 拿到这个工厂、再 `Create(ticket)` 时，才会真正 `new sample::TrivialTranslator(ticket)`。

> 注意：如果一个组件需要比 `new T(arg)` 更复杂的创建逻辑（例如根据参数选择不同子类），就不能用 `Component<T>`，而要手写一个 `Class<T,Arg>::Component` 的子类并重写 `Create`。librime 内置组件中 `Component<T>` 已覆盖绝大多数场景。

#### 4.2.4 代码实践

1. **实践目标**：分清「工厂对象」与「产品对象」这两次 `new`。
2. **操作步骤**：
   - 在 [sample/src/sample_module.cc:19](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/src/sample_module.cc#L19) 上标注「这是第 1 次 new，造的是工厂」。
   - 在 [component.h:37](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/component.h#L37) 上标注「这是第 2 次 new，造的是产品，发生在 `Create` 被调用时」。
3. **需要观察的现象**：模块加载时只有第 1 次 `new` 发生；第 2 次 `new` 直到引擎装配流水线、真正需要这个翻译器时才发生。
4. **预期结果**：你能用自己的话讲清楚——注册阶段是「登记工厂」，使用阶段才「让工厂造产品」。这种**两阶段创建**让同一个工厂可以为不同会话/方案反复 `Create` 出多个独立产品实例。
5. 若想确认行为，可在 `TrivialTranslator` 构造函数里加一行日志（属于「待本地验证」的修改型实践，不要提交）。

#### 4.2.5 小练习与答案

**练习 1**：`Component<sample::TrivialTranslator>` 为什么能继承 `sample::TrivialTranslator::Component`？`TrivialTranslator::Component` 从哪来？

**答案**：因为 `TrivialTranslator` 继承自 `Translator`，而 `Translator` 继承 `Class<Translator, const Ticket&>`，所以 `Translator::Component`（即 `Class<Translator, const Ticket&>::Component`）作为内嵌类型被 `TrivialTranslator` 间接获得。`Component<T>` 的 `T` 必须先满足「是 `Class<T,Arg>` 的派生类」这一隐含约束。

**练习 2**：若想让注册名 `trivial_translator` 根据方案配置在「打印问候语」和「打印时间」两种行为间切换，还能用 `Component<sample::TrivialTranslator>` 吗？

**答案**：不能直接用默认工厂。因为 `Component<T>` 写死了 `new T(arg)`，无法在 `Create` 里做分支选择。需要手写一个继承 `Translator::Component` 的工厂类，在 `Create` 中读 `ticket` 携带的配置并 `return` 不同子类的实例。这正是「默认工厂」与「定制工厂」的分界。

---

### 4.3 Registry：全局注册表单例

#### 4.3.1 概念说明

`Registry` 是 librime 进程内**唯一的**组件仓库：一张 `名字 → 工厂对象` 的映射表。它的职责很纯粹——登记（`Register`）、查找（`Find`）、注销（`Unregister`）、清空（`Clear`）。它不关心工厂造的是什么，也不负责调用 `Create`；它只是个「按名字存取 `ComponentBase*`」的字典。

它是单例（`Registry::instance()` 返回全局唯一实例），因为组件注册是进程级全局状态：所有模块（core / dict / gears / levers / 各插件）在加载时都往这同一张表里登记，引擎装配时也查这同一张表。用单例避免在调用链里到处传递 `Registry&`。

> 这里的 `Registry` 只管**组件工厂**；模块（`RimeModule`）的注册由 `ModuleManager` 负责（见 u5-l3）。两者是不同层级的注册表，不要混淆。

#### 4.3.2 核心流程

```text
模块加载时（各 *_module.cc）：
    Registry::instance().Register("trivial_translator", new Component<...>);
            │  → 若该名字已存在：LOG(WARNING) + delete 旧工厂
            │  → map_["trivial_translator"] = 新工厂
            ▼
引擎装配时：
    Registry::instance().Find("trivial_translator");
            │  → map_.find(...) 命中返回工厂指针，否则 NULL
            ▼
进程结束时：
    Registry::instance().Clear();   // 逐个 delete 所有工厂
```

三条关键语义：

1. **同名覆盖即替换**：`Register` 若发现名字已存在，会 `delete` 旧工厂再覆盖。这意味着「后加载的模块可以覆盖先加载的同名组件」——这既是灵活性（插件可替换内置实现），也要求命名谨慎。
2. **`Registry` 拥有工厂对象的生命周期**：登记进去的 `ComponentBase*` 由 `Registry` 在 `Unregister` / `Clear` / 同名覆盖时负责 `delete`。调用方 `new` 完交出去后就**不要再持有或 `delete`** 它。
3. **`Find` 失败返回 `NULL`**：查不到名字不抛异常，返回空指针，由调用方（`Require`）决定如何处理（通常记 ERROR 并跳过）。

#### 4.3.3 源码精读

`Registry` 的声明紧凑清晰：

[registry.h:17-32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/registry.h#L17-L32) 定义 `Registry`。[registry.h:19](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/registry.h#L19) 的 `ComponentMap` 是 `map<string, ComponentBase*>`；[registry.h:21-23](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/registry.h#L21-L23) 声明 `Find / Register / Unregister`（标 `RIME_DLL` 表示跨动态库导出）；[registry.h:26](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/registry.h#L26) 是单例访问点；构造函数私有（[registry.h:29](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/registry.h#L29)），强制走 `instance()`。

实现里最值得读的是 `Register` 的「同名覆盖」逻辑：

[registry.cc:13-20](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/registry.cc#L13-L20)：先 `Find(name)` 看是否已有，若有则 `LOG(WARNING)` 提示「替换已注册组件」并 `delete existing`，再写入新值。这段就是「插件可覆盖内置组件」的实现机制。

`Find` 与 `Unregister`：

[registry.cc:39-45](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/registry.cc#L39-L45) 的 `Find` 就是普通 `map::find`，找不到返回 `NULL`。

[registry.cc:22-29](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/registry.cc#L22-L29) 的 `Unregister` 先找到再 `delete` 并 `erase`，找不到直接返回（无操作）。

单例的惰性初始化：

[registry.cc:47-53](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/registry.cc#L47-L53) 用函数内 `static the<Registry> s_instance;` 实现线程安全的惰性单例（C++11 起局部 static 初始化是线程安全的），首次访问时 `new Registry`。

#### 4.3.4 代码实践

1. **实践目标**：验证「同名覆盖即替换」与「`Registry` 持有所有权」两条语义。
2. **操作步骤**：
   - 阅读 [registry.cc:13-20](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/registry.cc#L13-L20)，确认同名注册时旧工厂被 `delete`。
   - 用 `git grep -n "Registry::instance().Register"` 列出所有注册点（多为各 `*_module.cc` 文件）。
3. **需要观察的现象**：注册点都集中在模块初始化函数里（如 `rime_sample_initialize`），且都形如 `Register("名字", new Component<某类>)`，注册后代码不再持有这个 `new` 出来的指针。
4. **预期结果**：你应能解释——「`new` 出来的工厂交给 `Registry` 后就不再被原模块管理，它的销毁完全由 `Registry` 负责」。这就是为何各 `*_module.cc` 里只有 `Register(...)` 一行，没有配套 `delete`。
5. 若开启 `ENABLE_LOGGING`（见 u1-l2）运行，可在日志里看到 `"registering component: trivial_translator"` 这条 `LOG(INFO)` 输出（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Registry` 用 `map<string, ComponentBase*>` 存裸指针，而不是 `map<string, unique_ptr<ComponentBase>>`？

**答案**：历史与简洁性考量。代码用裸指针 + 手动 `delete`（在 `Unregister` / `Clear` / 同名覆盖处）来管理所有权，逻辑集中且清晰。改用 `unique_ptr` 在功能上等价，但 librime 此处选择显式管理，便于在「同名覆盖」时精确控制日志与销毁顺序。读者理解「所有权归 `Registry`」这一不变式即可。

**练习 2**：若 `Register("X", factoryA)` 后又 `Register("X", factoryB)`，`factoryA` 会怎样？

**答案**：会被 `delete` 释放，`map_["X"]` 指向 `factoryB`，并打印一条 `WARNING` 日志。即后者覆盖前者，前者被回收，不会内存泄漏。

---

### 4.4 Require 与完整调用链：从注册到实例化

#### 4.4.1 概念说明

`Register` 解决「往表里放」，但引擎要的是「按名字拿出**带类型的工厂**，再造出产品」。这两步——`Find` 还原类型、`Create` 造产品——被封装进 `Class<T, Arg>::Require(name)` 这个静态方法里。

`Require` 做三件事：① 调 `Registry::instance().Find(name)` 拿到 `ComponentBase*`；② `dynamic_cast<Component*>` 把它还原回 `Class<T,Arg>::Component*`（带类型、能调 `Create`）；③ 返回这个工厂指针（找不到或类型不符返回 `nullptr`）。注意 `Require` **不调用 `Create`**——它只返回工厂，是否造产品由调用方决定（因为调用方要往 `Create` 里塞具体的 `Arg`，如 `Ticket`）。

这套设计的好处是**类型安全**：`Translator::Require("trivial_translator")` 返回的是 `Translator::Component*`，其 `Create` 签名编译期就确定为 `Translator* Create(const Ticket&)`，调用方拿到的产品天然就是 `Translator*`，无需再 `dynamic_cast` 产品类型。

#### 4.4.2 核心流程

完整的一次「配置串 → 对象」旅程（以引擎装配 `translators` 为例）：

```text
方案 YAML:  engine/translators: [ script_translator ]
                          │
                          │  Engine 读取清单，得到处方串 "script_translator"
                          ▼
Ticket ticket{engine, "translator", "script_translator"};   // klass="script_translator"
                          │
                          │  T::Require(ticket.klass)   // T = Translator
                          ▼
   Registry::instance().Find("script_translator")          // 返回 ComponentBase*
                          │
                          │  dynamic_cast<Translator::Component*>
                          ▼
              得到带类型的工厂 c   （找不到则 nullptr → LOG(ERROR) 跳过）
                          │
                          │  c->Create(ticket)           // 内部 new ScriptTranslator(ticket)
                          ▼
              得到 Translator* 产品，装入 translators_ 容器
```

早退与容错：`Require` 返回 `nullptr` 时（名字没注册），调用方只记一条 `ERROR` 并 `continue`，不会崩溃——这就是 u2-l4 提到的「装配容错：缺件仅记 ERROR 跳过」。

#### 4.4.3 源码精读

`Require` 的定义就在 `Class<T, Arg>` 模板里：

[component.h:29-31](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/component.h#L29-L31)：`Require(name)` 调 `Registry::instance().Find(name)`，再 `dynamic_cast<Component*>` 还原类型并返回。注意它返回的是**工厂**，不是产品。

引擎装配流水线时对四类组件统一调用 `Require` + `Create` 的模板函数：

[engine.cc:296-320](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L296-L320) 的 `CreateComponentsFromList<T>` 是关键：它遍历 YAML 清单，把每个处方串包成 `Ticket`，[engine.cc:310](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L310) 用 `T::Require(ticket.klass)` 取工厂，[engine.cc:316](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L316) 用 `c->Create(ticket)` 造产品。失败（`c` 为空或 `Create` 返回空）只记 `ERROR` 不中断。

这套模板被复用到四张清单上：

[engine.cc:350-357](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L350-L357) 分别对 `Processor / Segmentor / Translator / Filter` 实例化 `CreateComponentsFromList`，把它们的产物填进 `processors_ / segmentors_ / translators_ / filters_` 四个容器。

`Require` 的另一处常见用法是「直接按固定名字取单例型组件」，如配置组件：

[schema.cc:13](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/schema.cc#L13) 的 `Config::Require("config")->Create("default")`——`Config` 是 `Class<Config, const string&>`（u4-l2），`Require("config")` 取出配置工厂，`Create("default")` 造出 `default.yaml` 对应的 `Config`。这里 `Arg` 是 `const string&` 而非 `const Ticket&`，印证 `Class<T, Arg>` 的 `Arg` 是可变的。

#### 4.4.4 代码实践

1. **实践目标**：把「注册」与「取用」两端在源码里对上号，亲手走一遍 sample 插件的 `trivial_translator` 调用链。
2. **操作步骤**：
   - **注册端**：读 [sample/src/sample_module.cc:16-20](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/src/sample_module.cc#L16-L20)。`rime_sample_initialize` 在模块被加载时执行，[第 19 行](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/src/sample_module.cc#L19) 把 `new Component<sample::TrivialTranslator>` 这个工厂登记为 `"trivial_translator"`。
   - **触发端**：`RIME_REGISTER_MODULE(sample)` 宏（见 [src/rime_api.h:541-552](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L541-L552)）借助 `__attribute__((constructor))`（[src/rime_api.h:524-526](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L524-L526)）在共享库加载时把名为 `sample` 的模块登记进 `ModuleManager`，并绑定其 `initialize = rime_sample_initialize`；模块随后被 `ModuleManager` 加载时才真正调用 `rime_sample_initialize` 完成组件 `Register`（模块加载细节见 u5-l3）。
   - **取用端**：当某个方案的 `engine/translators` 里写了 `trivial_translator`，引擎经 [engine.cc:310](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L310) 的 `Translator::Require("trivial_translator")` 取到该工厂，再 `Create(ticket)` 产出 `TrivialTranslator` 实例。
3. **需要观察的现象**：注册名 `"trivial_translator"`（字符串）在三处出现——注册时、方案 YAML 的 `translators` 清单里、`Require` 调用处。三者必须完全一致，否则 `Find` 返回 `NULL`、`Require` 返回 `nullptr`。
4. **预期结果**：你能画出这样一条链：`RIME_REGISTER_MODULE(sample)` → `ModuleManager` 登记 module → 加载 module → `rime_sample_initialize` → `Registry::Register("trivial_translator", ...)` → 引擎装配时 `Translator::Require("trivial_translator")` → `Create` → `TrivialTranslator` 实例。
5. 编译并运行 sample 插件、用一个引用 `trivial_translator` 的测试方案验证候选出现（详见 u9-l6，本讲为「待本地验证」的源码阅读型实践）。

#### 4.4.5 小练习与答案

**练习 1**：`Translator::Require("trivial_translator")` 返回值的精确类型是什么？为什么调用方拿到后能直接 `->Create(ticket)` 而无需再转型？

**答案**：返回 `Translator::Component*`（即 `Class<Translator, const Ticket&>::Component*`）。因为 `Require` 内部已经 `dynamic_cast<Component*>` 把 `Find` 返回的 `ComponentBase*` 还原成带类型的工厂指针，其 `Create` 签名编译期已知为 `Translator* Create(const Ticket&)`，所以调用方直接 `->Create` 即可，产品类型也是确定的 `Translator*`。

**练习 2**：如果把 `"trivial_translator"` 这个名字拼错（如 `"trivial_translator "` 多了空格），引擎会怎样？

**答案**：`Registry::Find` 按精确字符串匹配，找不到则返回 `NULL`，`Require` 返回 `nullptr`，`CreateComponentsFromList` 在 [engine.cc:311-315](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L311-L315) 记一条 `ERROR creating translator: 'trivial_translator '` 并 `continue` 跳过，不会崩溃，但该翻译器不会出现在流水线上（候选为空）。

**练习 3**：`Require` 为什么不直接 `Create` 并返回产品，而要先返回工厂？

**答案**：因为同一名字的工厂可能要被多次 `Create`（不同会话、不同方案、不同 `Ticket` 上下文各造一个独立产品）。若 `Require` 直接造产品，就无法复用工厂；分离「取工厂」与「造产品」两步，使一个注册项能按需产出任意多个实例，且每次 `Create` 都能传入不同的 `Arg`（如不同 `Ticket`）。

---

## 5. 综合实践

**任务**：用本讲学到的「接口 → 工厂 → 注册表 → 取用」四件套，自己推演一个全新的自定义 Translator 如何接入 librime。

1. 假设你要写一个 `HelloTranslator`（产品类型 `T`），它应继承哪个类、`Class<T, Arg>` 的 `Arg` 是什么？（答：`public Translator`，`Arg` 为 `const Ticket&`，因为 `Translator : public Class<Translator, const Ticket&>`。）
2. 它需要一个怎样的构造函数才能配合 `Component<T>` 默认工厂？（答：接受 `const Ticket&` 的构造函数，参考 [translator.h:24-25](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translator.h#L24-L25)。）
3. 在一个仿照 [sample_module.cc:16-20](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/src/sample_module.cc#L16-L20) 的初始化函数里，写出一行把 `HelloTranslator` 注册为 `"hello_translator"` 的代码。（答：`r.Register("hello_translator", new Component<HelloTranslator>);`。）
4. 写出引擎装配时取用它的调用链（用 `Require` + `Create`）。（答：`if (auto c = Translator::Require("hello_translator")) { an<Translator> t(c->Create(Ticket(this))); ... }`，可参考 [switcher.cc:309-310](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switcher.cc#L309-L310) 的写法。）
5. 回答：如果同一个进程里先后有两个模块都 `Register("hello_translator", ...)`，最终生效的是哪个？为什么？（答：后注册者覆盖先注册者，先注册的工厂被 `delete`，见 [registry.cc:13-20](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/registry.cc#L13-L20)。）

完成本任务后，你就具备了阅读 u9-l6（插件开发实战）所需的全部注册机制知识。

## 6. 本讲小结

- librime 用一套「组件注册体系」实现数据驱动装配：方案 YAML 里写组件**名字**，引擎运行时按名字造**对象**，引擎源码无需为每种输入法硬编码。
- `component.h` 是全部抽象的所在：`ComponentBase` 是统一根基类（只为多态存放）；`Class<T, Arg>` 模板把「工厂接口」以内嵌 `Component` 子类 + 纯虚 `Create(Initializer)` 的形式绑进目标类。
- `Component<T>` 是「默认工厂」模板，继承 `T::Component` 并把 `Create` 实现为 `new T(arg)`，让绝大多数组件一行 `new Component<MyClass>` 即可注册。
- `Registry` 是进程级单例，一张 `map<string, ComponentBase*>`，语义为：`Register`（同名覆盖即 `delete` 旧值）、`Find`（找不到返回 `NULL`）、`Unregister/Clear`（负责 `delete`，即所有权归 `Registry`）。
- `Class<T, Arg>::Require(name)` 是「按名取货」入口：`Find` → `dynamic_cast<Component*>` 还原类型 → 返回带类型工厂（不 `Create`）；调用方再 `c->Create(arg)` 造产品，类型安全。
- 完整调用链：`*_module.cc` 的 `Register` → `Registry::map_` → 引擎装配时 `T::Require` → `Create` → 产品入容器；缺件只记 `ERROR` 跳过，不崩溃。

## 7. 下一步学习建议

- **u5-l2 四大组件基类**：本讲的 `Class<T, Arg>` 是抽象骨架，下一篇把它落到 `Processor / Segmentor / Translator / Filter`（及 `Formatter`）四个真实基类上，看它们各自规定了什么 `Create` 之外的纯虚契约（如 `Translator::Query`）。
- **u5-l3 Module 机制与模块组**：本讲只说「组件如何登记」，下一篇讲「谁在何时调用这些 `Register`」——`RIME_REGISTER_MODULE` / `RIME_REGISTER_MODULE_GROUP` 宏、`ModuleManager` 的加载流程，以及 `core / dict / gears / levers` 各模块组注册了哪些组件。
- **u5-l4 Ticket 与外部插件加载**：本讲的 `Require(name)` 里 `name` 来自 `Ticket::klass`，下一篇详解 `Ticket` 如何拆解 `klass@namespace`、以及 `PluginManager` 如何用 `boost::dll` 动态加载 `librime-*` 插件。
- **延伸阅读**：想看「定制工厂」（非 `Component<T>`）的真实例子，可在 `src/rime/gear/` 下搜索 `: public ...::Component` 的手写工厂类；想确认某组件的注册名，用 `git grep -n "Registry::instance().Register"` 在对应 `*_module.cc` 里查找。
