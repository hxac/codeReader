# Ticket 与外部插件加载

## 1. 本讲目标

本讲承接 u5-l1（组件注册）、u5-l2（四大基类）、u5-l3（模块机制），回答两个把「方案配置」变成「运行时能力」的关键问题：

1. **引擎装配时，方案 YAML 里那一行行字符串处方（如 `affix_segmentor@cangjie`）是怎么变成一个活对象的？** 中间那一层「上下文包」就是 `Ticket`。
2. **那些并不编译进主库的扩展（如 `librime-lua`）是怎么被加载进来、并把它自己注册成模块和组件的？** 这就是 `PluginManager` 的工作。

学完本讲，你应当能够：

- 说清 `Ticket` 的四个字段（`engine`/`schema`/`name_space`/`klass`）各自携带什么上下文，以及 `klass@alias` 处方串如何被拆解。
- 解释为什么同一个组件类（如 `affix_segmentor`、`simplifier`）能在一份方案里被实例化多次、且各自读不同的配置节。
- 描述外部插件从 `librime-xxx.so` 文件到「Registry 里多出一批组件」的完整加载链，以及文件名与模块名之间的命名约定。
- 区分「合并插件（merged）」与「外部插件（external）」两种分发形态。

## 2. 前置知识

本讲默认你已理解以下概念（均在 u5 系列前几讲建立）：

- **组件（Component）与注册表（Registry）**：组件是引擎流水线上的零件，按名字注册进进程级单例 `Registry`，`Class<T,Arg>::Require(name)` 按名取回工厂，再 `Create(arg)` 造出实例（见 u5-l1）。
- **四大基类契约**：`Processor`/`Segmentor`/`Translator`/`Filter` 都以一个 `const Ticket&` 构造（见 u5-l2）。本讲就讲这个 `Ticket` 里到底装了什么。
- **模块（Module）与模块管理器**：模块是「一批组件的打包注册单元」，注册进 `ModuleManager`，加载时触发 `initialize()` 把组件塞进 `Registry`（见 u5-l3）。

本讲新引入的几个工程术语：

- **实例化（instantiation）**：把一个「类」变成一个「对象」的过程，由工厂的 `Create()` 完成。
- **工厂（factory）**：负责造对象的对象。librime 里每个组件类的 `Component` 内嵌子类就是一个工厂。
- **处方（prescription）**：方案 YAML 里写的那一行字符串，如 `"simplifier@zh_simp"`，它「开出处方」告诉引擎要造哪个类、用哪段配置。这是本讲的非正式术语，源码注释里用了这个词（见下文 `ticket.h` 第 25 行）。
- **动态加载（dynamic loading）**：程序运行时把一个共享库（`.so`/`.dylib`/`.dll`）加载进自己的地址空间，Linux 上由 `dlopen` 实现，本讲通过 `boost::dll` 这个跨平台封装来使用。
- **构造器属性（constructor attribute）**：GCC/Clang 的 `__attribute__((constructor))`，标记的函数会在共享库被加载时**自动**执行。这是插件能「自我注册」的底层机制（见 u5-l3）。
- **`dladdr`**：POSIX 提供的函数，给定一个函数指针，反查它所在的共享库文件路径。本讲用它来定位「插件目录在哪里」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/rime/ticket.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/ticket.h) | `Ticket` 结构体声明，只有四个字段加两个构造函数，是本讲第一主角。 |
| [src/rime/ticket.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/ticket.cc) | `Ticket` 的构造实现，`@` 拆解逻辑就在这里。 |
| [src/rime/engine.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc) | `CreateComponentsFromList` 模板：读 YAML 处方 → 造 `Ticket` → 查注册表 → 实例化。 |
| [src/rime/segmentor.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/segmentor.h) | 基类如何从 `Ticket` 取出 `name_space_`，示范「上下文如何流入组件」。 |
| [src/rime/gear/affix_segmentor.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/affix_segmentor.cc) | 一个具体组件如何用 `name_space_` 拼出配置路径（如 `cangjie/prefix`）。 |
| [data/minimal/luna_pinyin.schema.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml) | 真实方案，提供大量 `klass@alias` 处方样例。 |
| [plugins/plugins_module.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/plugins/plugins_module.cc) | `PluginManager` 全部实现，是本讲第二主角。 |
| [plugins/CMakeLists.txt](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/plugins/CMakeLists.txt) | 插件的两种构建形态（merged / external）由这里控制。 |
| [src/rime_api.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h) | `RIME_REGISTER_MODULE` 宏定义，解释「文件名↔模块名」约定的来源。 |
| [src/rime_api.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.cc) | 合并插件路径下 `rime_declare_module_dependencies` 如何显式留住插件符号。 |
| [src/rime/setup.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/setup.cc) | `plugins` 模块如何通过 `RIME_EXTRA_MODULES` 进入默认加载列表。 |

## 4. 核心概念与源码讲解

### 4.1 Ticket：组件实例化的上下文包

#### 4.1.1 概念说明

回顾 u5-l2：四大基类 `Processor`/`Segmentor`/`Translator`/`Filter` 的构造函数签名都是 `T(const Ticket& ticket)`。这个 `Ticket` 就是引擎在「装配流水线」时，递给每个组件的**上下文包**——它告诉组件三件事：

1. **你属于哪个引擎？**（`engine`）——组件需要回访引擎、读写 `Context`、订阅信号。
2. **当前是哪个方案？**（`schema`）——组件要从方案的 `Config` 树里读自己的设置。
3. **你应该读哪一段配置？又应该被实例化成哪个类？**（`name_space` 与 `klass`）。

`Ticket` 之所以重要，是因为它解决了一个核心矛盾：**一份方案里，同一个组件类常常要被实例化好几次，每次读不同的配置。** 最典型的例子是 `luna_pinyin`（明月拼音）同时挂了 `simplifier@zh_simp`（转简体）和 `simplifier@zh_tw`（转台湾正体）——同一个 `Simplifier` 类，两个实例，两套 OpenCC 字典。区分它们的，就是 `Ticket` 里的 `name_space`。

`Ticket` 本身极其轻量：四个字段、两个构造函数，没有任何方法。它只是一个「在装配期临时存在、被传进构造函数后通常就丢弃」的值对象。

#### 4.1.2 核心流程

引擎装配一个组件的完整流程（`Ticket` 是其中第二步的产物）：

```text
方案 YAML 里的一行处方
        │  例如 "affix_segmentor@cangjie"
        ▼
┌───────────────────────────────────────────┐
│ 1. CreateComponentsFromList 读出字符串      │
│    (engine.cc，component_type="segmentor") │
└───────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────┐
│ 2. 构造 Ticket{engine, component_type, 处方} │
│    → 拆 '@'：klass="affix_segmentor"        │
│              name_space="cangjie"           │
│    → schema 取自 engine->schema()           │
└───────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────┐
│ 3. T::Require(ticket.klass) 查 Registry     │
│    → 拿到 AffixSegmentor::Component 工厂     │
└───────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────┐
│ 4. factory->Create(ticket) 造出实例          │
│    → 实例从 ticket.name_space 拼 config 路径 │
│    → 读 "cangjie/prefix" "cangjie/tag" ...  │
└───────────────────────────────────────────┘
        │
        ▼
   塞进 segmentors_ 容器，装配下一个
```

`@` 拆解规则（本模块的核心算法）用伪代码表示：

```text
输入：prescription 字符串（如 "klass@alias"）
默认：name_space = component_type（"processor"/"segmentor"/"translator"/"filter"）
      klass      = prescription

若 prescription 中存在 '@'：
    令 sep = '@' 的位置
    name_space = prescription[sep+1 : 末尾]   # '@' 之后覆盖默认命名空间
    klass      = prescription[0    : sep]     # '@' 之前是真正的类名
否则：
    klass 不变，name_space 保持默认值
```

一句话记忆：**`@` 左边是「用哪个类」，右边是「读哪段配置」**。

#### 4.1.3 源码精读

**Ticket 的字段定义**非常简洁——四个字段，其中三个有默认值：

```cpp
struct Ticket {
  Engine* engine = nullptr;
  Schema* schema = nullptr;
  string name_space;
  string klass;
  ...
};
```

见 [src/rime/ticket.h:17-30](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/ticket.h#L17-L30)。注意头文件里那段注释把 `prescription` 这个词的含义点明了：「`klass` 或 `klass@alias`，其中 alias（若给出）会覆盖默认命名空间」。

**两个构造函数**分别对应两种使用场景。第一个构造函数只接 `Schema*` 和命名空间，不解析处方，用于少数不需要引擎、也不需要从处方拆类的场合：

```cpp
Ticket::Ticket(Schema* s, const string& ns) : schema(s), name_space(ns) {}
```

见 [src/rime/ticket.cc:12](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/ticket.cc#L12)。

**主构造函数**才是引擎装配时实际使用的那个，它接收引擎指针、默认命名空间、处方串三参数，并完成 `@` 拆解：

```cpp
Ticket::Ticket(Engine* e, const string& ns, const string& prescription)
    : engine(e),
      schema(e ? e->schema() : NULL),   // schema 自动从 engine 派生
      name_space(ns),
      klass(prescription) {              // 先把整串当作 klass
  size_t separator = klass.find('@');
  if (separator != string::npos) {
    name_space = klass.substr(separator + 1);  // '@' 之后 → 覆盖 name_space
    klass.resize(separator);                    // '@' 之前 → 截断 klass
  }
}
```

见 [src/rime/ticket.cc:14-24](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/ticket.cc#L14-L24)。这段代码有两个值得品味的细节：

- `schema` 由 `engine->schema()` 自动派生，所以绝大多数组件不必单独传 schema——只要给了引擎，方案就跟着来了；只有 `engine` 为空时 `schema` 才是 `NULL`。
- `name_space` 先被设成默认值（调用方传入的 `component_type`），**只有当处方里真有 `@` 时才被覆盖**。这就是为什么没写 `@` 的组件（如 `ascii_segmentor`）会用默认命名空间。

**装配入口 `CreateComponentsFromList`** 把 Ticket、注册表、工厂三者串起来，它是一个对四种组件通用的模板：

```cpp
Ticket ticket{engine, component_type, prescription->str()};
auto c = T::Require(ticket.klass);   // ① 按名查 Registry，拿回工厂
if (!c) { LOG(ERROR) << ...; continue; }   // 缺件仅记错跳过，不崩溃
auto component = c->Create(ticket);   // ② 工厂造实例，把 ticket 传进去
...
target_collection.push_back(component);
```

见 [src/rime/engine.cc:297-326](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L297-L326)（关键三行在 [L309-L316](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L309-L316)）。注意 `component_type` 就是默认命名空间的实参，它在四个调用点分别是字面量：

```cpp
CreateComponentsFromList<Processor>(this, config, "engine/processors", "processor", processors_);
CreateComponentsFromList<Segmentor>(this, config, "engine/segmentors", "segmentor", segmentors_);
CreateComponentsFromList<Translator>(this, config, "engine/translators", "translator", translators_);
CreateComponentsFromList<Filter>(this, config, "engine/filters", "filter", filters_);
```

见 [src/rime/engine.cc:350-357](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L350-L357)。于是：处理器类默认读 `processor/*` 配置、分段器默认读 `segmentor/*`、翻译器默认读 `translator/*`、过滤器默认读 `filter/*`——一旦处方里写了 `@alias`，就被 alias 覆盖。

**上下文如何流入组件**：四大基类的构造函数都把 `ticket.name_space` 拷进受保护成员 `name_space_`。以 `Segmentor` 为例：

```cpp
explicit Segmentor(const Ticket& ticket)
    : engine_(ticket.engine), name_space_(ticket.name_space) {}
```

见 [src/rime/segmentor.h:20-21](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/segmentor.h#L20-L21)。`AffixSegmentor` 随后用这个 `name_space_` 拼出配置路径，读出前缀、后缀、标签等设置：

```cpp
AffixSegmentor::AffixSegmentor(const Ticket& ticket)
    : Segmentor(ticket), tag_("abc") {
  ...
  config->GetString(name_space_ + "/tag", &tag_);
  config->GetString(name_space_ + "/prefix", &prefix_);
  config->GetString(name_space_ + "/suffix", &suffix_);
  ...
}
```

见 [src/rime/gear/affix_segmentor.cc:15-33](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/affix_segmentor.cc#L15-L33)。当处方是 `affix_segmentor@cangjie` 时，`name_space_` 为 `"cangjie"`，于是它读的是 `cangjie/prefix`、`cangjie/tag`——这正是「`@` 右边决定读哪段配置」的落地。

**真实方案的处方对照**：`luna_pinyin.schema.yaml` 的 `engine` 段同时给出了带 `@` 和不带 `@` 的两类处方，是理解 Ticket 最好的活样本：

```yaml
segmentors:
  - ascii_segmentor            # klass=ascii_segmentor, ns=segmentor（默认）
  - affix_segmentor@alphabet   # klass=affix_segmentor, ns=alphabet
  - affix_segmentor@cangjie    # klass=affix_segmentor, ns=cangjie
  - affix_segmentor@pinyin     # klass=affix_segmentor, ns=pinyin
translators:
  - script_translator          # klass=script_translator, ns=translator（默认）
  - table_translator@cangjie   # klass=table_translator,  ns=cangjie
  - script_translator@pinyin   # klass=script_translator,  ns=pinyin
filters:
  - simplifier@zh_simp         # klass=simplifier, ns=zh_simp
  - simplifier@zh_tw           # klass=simplifier, ns=zh_tw
```

见 [data/minimal/luna_pinyin.schema.yaml:49-67](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L49-L67)。注意 `translators` 里**同一个 `script_translator` 类出现了两次**（第 61 行无 `@`、第 63 行 `@pinyin`），它们是两个独立实例、读不同配置节——这就是 Ticket 的 `name_space` 机制最直接的价值。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `Ticket` 的 `@` 拆解规则，理解「同一类、多实例、多配置节」是如何实现的。本实践为「源码阅读 + 配置推演」型，无需编译。

**操作步骤**：

1. 打开 [src/rime/ticket.cc:14-24](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/ticket.cc#L14-L24)，对着 `script_translator@pinyin` 这条处方，在纸上模拟构造过程：
   - 入参 `ns = "translator"`（来自 [engine.cc:354](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L354) 的 `component_type`）、`prescription = "script_translator@pinyin"`。
   - 执行 `klass = prescription` → `klass = "script_translator@pinyin"`。
   - `find('@')` 命中，`separator = 16`。
   - `name_space = klass.substr(17)` → `"pinyin"`。
   - `klass.resize(16)` → `"script_translator"`。

2. 同样模拟 `affix_segmentor@cangjie`：入参 `ns = "segmentor"`，最终 `klass = "affix_segmentor"`、`name_space = "cangjie"`。

3. 打开 [src/rime/gear/affix_segmentor.cc:19-24](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/affix_segmentor.cc#L19-L24)，确认它读取的配置键是 `cangjie/prefix`、`cangjie/tag` 等（`name_space_` 此时是 `"cangjie"`）。

4. 在 `luna_pinyin.schema.yaml` 中搜索 `cangjie:` 这一顶层节（仓颉反查相关配置），观察 `cangjie/prefix`、`cangjie/tag` 是否存在，验证步骤 3 的路径确实指向真实配置。

**需要观察的现象**：

- 处方里没有 `@` 时，`name_space` 保持调用方给的默认值（`processor`/`segmentor`/`translator`/`filter`）。
- 处方里有 `@` 时，`@` 两侧分别是「类名」和「配置别名」，二者都被正确分离。
- 同一个类名可以在同一份方案里配合不同别名出现多次。

**预期结果**：

| 处方串 | klass | name_space | 该实例读取的配置前缀 |
| --- | --- | --- | --- |
| `ascii_composer` | `ascii_composer` | `processor` | `processor/*`（或组件自定义） |
| `affix_segmentor@cangjie` | `affix_segmentor` | `cangjie` | `cangjie/*` |
| `script_translator@pinyin` | `script_translator` | `pinyin` | `pinyin/*` |
| `simplifier@zh_tw` | `simplifier` | `zh_tw` | `zh_tw/*` |

> 待本地验证：若你已按 u1-l2 构建了 librime，可启用 `RIME_ALSO_LOG_TO_STDERR` 并运行 `rime_api_console`，在装配期日志里能看到每个组件的 `name_space` 实际取值。

#### 4.1.5 小练习与答案

**练习 1**：方案里写了 `- echo_translator`（无 `@`），它的 `klass` 和 `name_space` 分别是什么？这个实例会从哪段配置读设置？

**参考答案**：`klass = "echo_translator"`，`name_space = "translator"`（`CreateComponentsFromList<Translator>` 传入的默认值）。它会从配置树的 `translator/*` 路径读取设置（具体读哪些键取决于 `EchoTranslator` 的实现）。

**练习 2**：为什么 `luna_pinyin.schema.yaml` 要同时写 `simplifier@zh_simp` 和 `simplifier@zh_tw` 两条，而不是只写一个 `simplifier`？

**参考答案**：因为用户可能需要在「简体」和「台湾正体」之间切换。两个处方实例化的是**同一个 `Simplifier` 类的两个独立对象**，分别读 `zh_simp/*`（简体 OpenCC 字典配置）和 `zh_tw/*`（台正 OpenCC 字典配置）。靠 `Ticket` 的 `name_space` 区分，引擎代码完全不用为「多套繁简转换」做特殊处理——这正是「数据驱动装配」的好处。

**练习 3**：如果把处方写成 `@pinyin`（`@` 前面为空），`klass` 会变成什么？后续 `T::Require(ticket.klass)` 会怎样？

**参考答案**：`klass.resize(0)` 后变成空字符串 `""`。`T::Require("")` 在 Registry 里查不到名为空的工厂，返回 `NULL`，于是 [engine.cc:311-315](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L311-L315) 记一条 `ERROR` 日志并 `continue` 跳过，不会崩溃。这说明装配是**容错**的：坏处方只丢一个组件，不拖垮整个引擎。

---

### 4.2 外部插件的动态加载

#### 4.2.1 概念说明

u5-l3 讲过，`core`/`dict`/`gears`/`levers` 这些**内置模块**的代码都编译在 librime 主库里，库一加载，它们的构造器函数就自动把模块注册进 `ModuleManager`。但 librime 还有一类**外部插件**——例如 `librime-lua`（嵌入 Lua 脚本能力）、`librime-octagram`（统计语言模型）——它们：

- 源码不在 librime 仓库里，是独立的 Git 仓库。
- 通常**不编译进主库**，而是各自产出一个共享库文件（如 `librime-lua.so`）。
- 在运行时被「发现并加载」，加载后才把它们提供的模块和组件注册进来。

负责这件事的就是 `PluginManager`。它解决两个子问题：

1. **去哪里找插件？** 约定一个「插件目录」（默认名 `rime-plugins`），位置就在主库所在目录的旁边。
2. **找到 `.so` 后怎么用？** 用 `boost::dll` 把它加载进进程；加载动作会触发插件内部的构造器函数（`RIME_REGISTER_MODULE` 展开出来的那段），从而自动完成模块注册。

理解本模块要先分清 librime 插件的**两种分发形态**（由 u1-l2 讲过的构建选项控制）：

| 形态 | 构建选项 | 产物 | 加载方式 |
| --- | --- | --- | --- |
| **外部插件（external）** | `ENABLE_EXTERNAL_PLUGINS=ON` | 每个插件一个独立 `.so`，放进 `rime-plugins/` 目录 | 运行时由 `PluginManager` 用 `boost::dll` 动态加载 |
| **合并插件（merged）** | `BUILD_MERGED_PLUGINS=ON` | 插件代码直接编进主库（或 `rime-plugins` 库） | 库加载时构造器自动注册，无需 `dlopen` |

外部插件是本模块的主角（更灵活、更像传统「插件」）；合并插件是它的一个对照（更省事、无运行时开销）。两条路最终都把组件送进同一个 `Registry`。

#### 4.2.2 核心流程

外部插件的完整加载链，从「库被前端调用 `rime_api` 初始化」一直追到「Registry 里多出一批组件」：

```text
前端调用 rime_api: initialize → RimeInitialize
        │
        ▼
LoadModules(kDefaultModules)          # setup.cc
        │  kDefaultModules = ["default", "plugins", ...]
        ▼
依次加载每个模块：
  "default" → core + dict + gears      # 内置组件注册
  "plugins" → rime_plugins_initialize()# 本模块的入口
        │
        ▼
PluginManager::LoadPlugins(插件目录)
        │
        ├── 1. 用 dladdr 找到主库所在目录
        ├── 2. 拼出 插件目录 = 主库目录 / "rime-plugins"
        ├── 3. 遍历目录里每个 *.so / *.dylib
        │
        ▼  对每个插件文件：
        ├── a. plugin_name_of(file)：librime-lua.so → "lua"
        ├── b. boost::dll::shared_library(file)：dlopen 加载
        │      └─ 触发插件的构造器函数 → 自动 RimeRegisterModule("lua")
        ├── c. mm.Find("lua")：按名查 ModuleManager
        └── d. mm.LoadModule(module)：调用 module->initialize()
               └─ initialize() 内部把 lua 的组件 Register 进 Registry
        │
        ▼
此后引擎装配时，T::Require("lua_translator") 就能查到插件提供的组件
```

**命名约定**是这条链能跑通的关键约束（下一节细讲）：**插件共享库的文件名（去掉 `librime-` 或 `rime-` 前缀）必须等于它内部 `RIME_REGISTER_MODULE(name)` 注册的模块名**。例如 `librime-lua.so` 必须对应 `RIME_REGISTER_MODULE(lua)`。这条约定把「文件系统里的文件」和「ModuleManager 里的模块」用名字连了起来。

#### 4.2.3 源码精读

**`PluginManager` 类**本身很小，是一个单例，持有一张「模块名 → 共享库」的映射来保证已加载的库不会被提前卸载：

```cpp
class PluginManager {
 public:
  void LoadPlugins(path plugins_dir);
  static string plugin_name_of(path plugin_file);
  static PluginManager& instance();
 private:
  PluginManager() = default;
  map<string, boost::dll::shared_library> plugin_libs_;
};
```

见 [plugins/plugins_module.cc:21-33](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/plugins/plugins_module.cc#L21-L33)。注意 `plugin_libs_` 的生命周期：只要 `PluginManager`（单例）活着，这些 `shared_library` 对象就活着，底层 `dlopen` 的句柄就不会被释放，插件注册的组件指针就一直有效。

**`LoadPlugins` 的扫描与加载循环**：

```cpp
for (fs::directory_iterator iter(plugins_dir), end; iter != end; ++iter) {
  path plugin_file = iter->path();
  if (plugin_file.extension() == boost::dll::shared_library::suffix()) {
    ...
    string plugin_name = plugin_name_of(plugin_file);
    if (plugin_libs_.find(plugin_name) == plugin_libs_.end()) {
      auto plugin_lib = boost::dll::shared_library(plugin_file);  // ← dlopen
      plugin_libs_[plugin_name] = plugin_lib;
    }
    if (RimeModule* module = mm.Find(plugin_name)) {   // 按名查模块
      mm.LoadModule(module);                            // 调 initialize()
    } else {
      LOG(WARNING) << "module '" << plugin_name
                   << "' is not provided by plugin library " << plugin_file;
    }
  }
}
```

见 [plugins/plugins_module.cc:35-70](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/plugins/plugins_module.cc#L35-L70)。这段代码体现了「自我注册」的精妙之处：`PluginManager` 自己**不知道**插件叫什么、有什么组件——它只负责 `dlopen`。真正「告诉系统我叫 lua、我有这些组件」的，是插件自己的构造器函数（在加载瞬间被操作系统自动调用）。`PluginManager` 随后用文件名推算出的 `plugin_name` 去 `ModuleManager` 里**反查**这个模块，查到就加载、查不到就报警告。

**`plugin_name_of` 的文件名→模块名转换**是命名约定的实现：

```cpp
string PluginManager::plugin_name_of(path plugin_file) {
  string name = plugin_file.stem().string();   // 去扩展名：librime-lua.so → librime-lua
  if (boost::starts_with(name, "librime-")) {
    boost::erase_first(name, "librime-");       // → lua
  } else if (boost::starts_with(name, "rime-")) {
    boost::erase_first(name, "rime-");          // → lua
  }
  // 把连字符换成下划线，因为模块名是 initializer 函数名的一部分
  std::replace(name.begin(), name.end(), '-', '_');
  return name;
}
```

见 [plugins/plugins_module.cc:72-84](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/plugins/plugins_module.cc#L72-L84)。最后那一行把 `-` 换成 `_` 至关重要——因为模块名 `lua` 会被拼进初始化函数名 `rime_lua_initialize`（C 函数名不能含连字符），所以如果插件叫 `rime-char-code`，文件名 `librime-char-code.so` 会被转成模块名 `char_code`，对应函数 `rime_char_code_initialize`。

**插件目录是怎么定位的**：`PluginManager` 不接受前端传目录，而是**自己推算**——「我所在的那个共享库（主库或 `rime-plugins` 库）放在哪个目录，插件就挨着它在 `rime-plugins/` 子目录里」。推算靠 POSIX 的 `dladdr`：

```cpp
inline static rime::path symbol_location(const void* symbol) {
  Dl_info info;
  const int res = dladdr(const_cast<void*>(symbol), &info);
  if (res) {
    return rime::path{info.dli_fname};   // 这个符号所在 .so 的文件路径
  }
  ...
}

inline static rime::path current_module_path() {
  void rime_require_module_plugins();
  return symbol_location(
      reinterpret_cast<const void*>(&rime_require_module_plugins));
}
```

见 [plugins/plugins_module.cc:104-119](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/plugins/plugins_module.cc#L104-L119)。这里取的是 `rime_require_module_plugins` 这个函数的地址——它由下面的 `RIME_REGISTER_MODULE(plugins)` 生成（见 [rime_api.h:541-542](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L541-L542)），一定和 `PluginManager` 的代码编在同一个库里。于是 `dladdr` 反查到的 `.so` 路径，就是「包含 plugins 模块的那个库」的路径。随后去掉文件名、拼上 `RIME_PLUGINS_DIR`：

```cpp
static void rime_plugins_initialize() {
  rime::PluginManager::instance().LoadPlugins(
      current_module_path().remove_filename() / RIME_PLUGINS_DIR);
}
```

见 [plugins/plugins_module.cc:122-125](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/plugins/plugins_module.cc#L122-L125)。`RIME_PLUGINS_DIR` 是构建期注入的宏，默认值 `"rime-plugins"`（见 [build_config.h.in:12](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/build_config.h.in#L12) 与 [CMakeLists.txt:34](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L34)）。Windows 分支目前是 TODO（见 [plugins_module.cc:96-100](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/plugins/plugins_module.cc#L96-L100)，返回空路径，故 Windows 上暂不支持 DLL 外部插件）。

**`plugins` 模块本身**用 `RIME_REGISTER_MODULE(plugins)` 收尾，把上面的 `rime_plugins_initialize` 包成一个名为 `"plugins"` 的模块：

```cpp
RIME_REGISTER_MODULE(plugins)
```

见 [plugins/plugins_module.cc:129](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/plugins/plugins_module.cc#L129)。它展开后（见 [rime_api.h:541-552](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L541-L552)）会：定义 `rime_require_module_plugins` 空函数、注册一个构造器函数（库加载时自动调 `RimeRegisterModule`），把这个模块的 `module_name="plugins"`、`initialize=rime_plugins_initialize` 登记进 `ModuleManager`。

**这个 `plugins` 模块是怎么进入默认加载列表的**？答案是 CMake 把它通过 `RIME_EXTRA_MODULES` 编译期宏塞进了 `kDefaultModules`。链路如下：

- `ENABLE_EXTERNAL_PLUGINS=ON` 时，[plugins/CMakeLists.txt:12-22](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/plugins/CMakeLists.txt#L12-L22) 编译 `plugins_module.cc` 并把 `"plugins"` 加入 `plugins_modules` 列表。
- 顶层 [CMakeLists.txt:267-271](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L267-L271) 把它包成 `RIME_SETUP_EXTRA_MODULES="(plugins)"`。
- [src/CMakeLists.txt:173-175](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/CMakeLists.txt#L173) 把它作为编译定义 `RIME_EXTRA_MODULES=(plugins)` 传给源码。
- [src/rime/setup.cc:36-39](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/setup.cc#L36-L39) 用它拼出 `kDefaultModules = ["default", "plugins"]`。

于是 `RimeInitialize → LoadModules(kDefaultModules)` 自然就会加载 `plugins` 模块、触发 `rime_plugins_initialize`、开始扫描插件目录。`ModuleManager::LoadModule` 会用 `loaded_` 集合保证每个模块只初始化一次（幂等），见 [src/rime/module.cc:25-37](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/module.cc#L25-L37)。

**对照：合并插件路径**。当 `BUILD_MERGED_PLUGINS=ON` 时，[plugins/CMakeLists.txt:48-52](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/plugins/CMakeLists.txt#L48-L52) 不再为每个插件单独建 `.so`，而是把它们的对象文件合并进主库。此时插件代码虽然编进了库里，但链接器可能认为「没人引用这些注册函数」而把它们优化掉——所以 [src/rime_api.cc:49-55](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.cc#L49-L55) 提供了 `rime_declare_module_dependencies()`，显式调用每个插件的 `rime_require_module_<name>()`（空函数，但强制造就一个引用），把符号「钉」在库里：

```cpp
void rime_declare_module_dependencies() {
  rime_require_module_core();
  rime_require_module_dict();
  rime_require_module_gears();
  rime_require_module_levers();
  _RIME_SEQ_FOR_EACH(_RIME_PLUGIN_CALL, ~, RIME_EXTRA_MODULES)  // 展开成各插件的调用
}
```

合并插件的模块注册仍由各自的构造器在主库加载时完成，最终效果与外部插件一致：`Registry` 里多出对应的组件。

#### 4.2.4 代码实践

**实践目标**：弄清「插件文件名 ↔ 模块名 ↔ 初始化函数名」三者的一致性约束，并追踪一次外部插件从 `.so` 到组件注册的全过程。本实践为「源码阅读 + 命名推演」型。

**操作步骤**：

1. **推演命名转换**。假设有三个插件文件，逐一用 `plugin_name_of` 的规则（[plugins_module.cc:72-84](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/plugins/plugins_module.cc#L72-L84)）算出模块名，并写出它们各自必须定义的初始化函数名（参考 [rime_api.h:548](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L548) 的 `module.initialize = rime_##name##_initialize`）：
   - `librime-lua.so`
   - `rime-octagram.so`
   - `librime-char-code.so`

2. **追踪加载链**。以 `librime-lua.so` 为例，按时间顺序写下以下八个环节各自发生了什么（参考 4.2.2 的流程图与 4.2.3 的源码）：
   - 前端调用 `rime_api` 的 `initialize`；
   - `LoadModules(kDefaultModules)` 走到 `"plugins"`；
   - `rime_plugins_initialize` 用 `dladdr` 定位插件目录；
   - `PluginManager::LoadPlugins` 遍历到 `librime-lua.so`；
   - `plugin_name_of` 得到 `"lua"`；
   - `boost::dll::shared_library` 执行 `dlopen`，触发 lua 插件的构造器；
   - `mm.Find("lua")` 找到模块，`mm.LoadModule` 调它的 `initialize`；
   - `initialize` 把 `lua_translator` 等组件 `Register` 进 `Registry`。

3. **验证目录定位逻辑**。阅读 [plugins/plugins_module.cc:104-125](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/plugins/plugins_module.cc#L104-L125)，回答：若主库安装在 `/usr/local/lib/librime.so`，`PluginManager` 会去哪个目录找插件？为什么取的是 `rime_require_module_plugins` 这个符号的地址而不是别的？

**需要观察的现象**：

- 文件名去掉前缀和扩展名、再把连字符换下划线后，必须与插件源码里 `RIME_REGISTER_MODULE(name)` 的 `name` 完全一致，否则 `mm.Find(plugin_name)` 会返回空、打出 `"module 'xxx' is not provided by plugin library ..."` 的警告（[plugins_module.cc:63-65](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/plugins/plugins_module.cc#L63-L65)）。
- `dladdr` 反查的是「`rime_require_module_plugins` 符号所在的库文件」，而不是进程可执行文件本身——这保证了无论 plugins 模块编在主库还是独立的 `rime-plugins` 库里，都能正确定位。

**预期结果**：

| 插件文件 | `plugin_name_of` 结果 | 必须存在的初始化函数 | `RIME_REGISTER_MODULE(?)` |
| --- | --- | --- | --- |
| `librime-lua.so` | `lua` | `rime_lua_initialize` | `RIME_REGISTER_MODULE(lua)` |
| `rime-octagram.so` | `octagram` | `rime_octagram_initialize` | `RIME_REGISTER_MODULE(octagram)` |
| `librime-char-code.so` | `char_code` | `rime_char_code_initialize` | `RIME_REGISTER_MODULE(char_code)` |

对步骤 3：插件目录是 `/usr/local/lib/rime-plugins/`；取 `rime_require_module_plugins` 的地址是因为这个符号与 `PluginManager`、`rime_plugins_initialize` 编译在**同一个库里**，`dladdr` 反查它得到的 `.so` 路径就是「包含 plugins 模块代码的库」的路径，去掉文件名即得到正确的库目录。

> 待本地验证：若你已用 `ENABLE_EXTERNAL_PLUGINS=ON` 构建并安装了某个外部插件（如 `librime-lua`），运行任意 rime 前端时查看其日志，应能看到 `loading plugins from .../rime-plugins`、`loading plugin 'lua' from ...`、`loaded plugin: lua` 三条日志（分别对应 [plugins_module.cc:40](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/plugins/plugins_module.cc#L40)、[L49-L50](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/plugins/plugins_module.cc#L49-L50)、[L62](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/plugins/plugins_module.cc#L62)）。

#### 4.2.5 小练习与答案

**练习 1**：外部插件和合并插件，最终都把组件注册进同一个 `Registry`。那为什么还要分两种形态？各自适合什么场景？

**参考答案**：外部插件（`ENABLE_EXTERNAL_PLUGINS`）把每个插件做成独立 `.so`，**运行时**才 `dlopen`——好处是用户/发行版可以按需安装或替换插件、不必重编主库；代价是有运行时加载开销、依赖 `boost::dll` 与 `dladdr`（Windows 暂未支持）。合并插件（`BUILD_MERGED_PLUGINS`）把插件代码在**构建期**直接编进主库——好处是无运行时开销、符号统一、跨平台（含 Windows）；代价是换插件要重编主库。移动端或一体化发行版通常选合并插件，桌面发行版可选外部插件以方便扩展。

**练习 2**：`PluginManager` 自己并不「知道」每个插件提供哪些组件，那它是怎么确保 `dlopen` 之后插件就被正确注册的？

**参考答案**：靠 `RIME_REGISTER_MODULE` 宏展开出的**构造器函数**（GCC 的 `__attribute__((constructor))`，见 [rime_api.h:524-526](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L524-L526) 与 [L543](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L543)）。操作系统在 `dlopen` 加载共享库时，会自动执行其中所有构造器函数，插件就在这一刻自我注册模块（进而由其 `initialize` 注册组件）。`PluginManager` 只需事后用文件名推算的模块名去 `mm.Find` 反查即可。这是一种「控制反转」：被加载者主动登记，加载者只负责触发。

**练习 3**：如果有人把一个第三方 `.so` 误放进了 `rime-plugins/` 目录，但它的文件名不匹配任何 `RIME_REGISTER_MODULE`，会发生什么？会崩溃吗？

**参考答案**：不会崩溃。`boost::dll::shared_library` 会成功加载它（只要它是合法共享库），触发其内部构造器（若它没有任何 rime 模块注册代码，则什么都不发生）；随后 `mm.Find(plugin_name)` 因为找不到对应模块返回空，`PluginManager` 打一条 `WARNING`：`"module 'xxx' is not provided by plugin library ..."`（[plugins_module.cc:63-65](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/plugins/plugins_module.cc#L63-L65)），然后继续处理下一个文件。加载流程是**容错**的。

---

## 5. 综合实践

**任务**：把本讲两个模块串起来，画一张「从方案 YAML 一行处方，到某个外部插件提供的组件实例」的完整端到端追踪图，并用自己的话写一份说明。

具体步骤：

1. **选定一条链路**。例如：方案里的 `engine/translators` 中有一行 `lua_translator@main`（假设已安装 `librime-lua` 插件，它提供了 `lua_translator` 组件和一个名为 `main` 的 Lua 翻译器配置）。

2. **画两张子图并标注衔接点**：
   - **装配期子图（本讲 4.1）**：YAML 处方 `lua_translator@main` → `Ticket{engine, "translator", "lua_translator@main"}` → `klass="lua_translator"`、`name_space="main"` → `Translator::Require("lua_translator")` → `Create(ticket)` → 实例从 `main/*` 读配置。
   - **启动期子图（本讲 4.2）**：`RimeInitialize` → `LoadModules(kDefaultModules)` → `plugins` 模块 → `PluginManager::LoadPlugins` → `dlopen(librime-lua.so)` → lua 插件构造器注册 `"lua"` 模块 → `mm.LoadModule` 调 `rime_lua_initialize` → `lua_translator` 组件被 `Register` 进 `Registry`。

3. **找到衔接点**：指出第一张图里的 `Translator::Require("lua_translator")` 之所以能成功，**前提**是第二张图已经先发生过——即装配期依赖启动期先把插件组件注册进 `Registry`。这解释了为什么 `RimeInitialize`（注册组件）必须在 `create_session`/`process_key`（装配引擎、读处方）之前完成。

4. **回答一个反思题**：如果用户在方案里写了 `lua_translator@main`，但忘了安装 `librime-lua` 插件，装配时会发生什么？（提示：参考 [engine.cc:311-315](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L311-L315) 的容错逻辑。）

**预期产出**：一张包含两个阶段、标注了关键函数与数据流向的追踪图，以及一段说明「启动期注册」与「装配期实例化」如何通过 `Registry` 这个共享黑板协作的文字。

## 6. 本讲小结

- **`Ticket` 是组件实例化的上下文包**，携带 `engine`/`schema`/`name_space`/`klass` 四个字段；其中 `schema` 由 `engine` 自动派生，绝大多数组件不必单独传方案。
- **处方串 `klass@alias`** 在 [ticket.cc:14-24](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/ticket.cc#L14-L24) 被拆解：`@` 左边是 Registry 里的类名 `klass`，右边覆盖默认命名空间 `name_space`。没有 `@` 时 `name_space` 取调用方给的默认值（`processor`/`segmentor`/`translator`/`filter`）。
- **`name_space` 决定组件读哪段配置**：同一类（如 `affix_segmentor`、`simplifier`）可借不同 `@alias` 实例化多次，各自读不同配置节，引擎代码无须为此特判——这是「数据驱动装配」的体现。
- **装配由 `CreateComponentsFromList` 统一驱动**：读处方 → 造 `Ticket` → `T::Require(klass)` 查工厂 → `Create(ticket)` 造实例，缺件仅记 `ERROR` 跳过，不崩溃。
- **外部插件由 `PluginManager` 用 `boost::dll` 动态加载**：扫描 `rime-plugins/` 目录，`dlopen` 每个 `.so`，靠插件自身的构造器函数完成模块自注册，再用文件名推算的模块名反查并 `LoadModule`。
- **命名约定是关键约束**：插件文件名（去 `librime-`/`rime-` 前缀、`-` 换 `_`）必须等于其 `RIME_REGISTER_MODULE(name)` 的模块名，否则反查失败只报警告。
- **合并插件是外部插件的对照形态**：构建期编进主库、用 `rime_declare_module_dependencies` 显式留住符号，无运行时 `dlopen`、跨平台，但换插件需重编主库。两种形态最终都把组件送进同一个 `Registry`。

## 7. 下一步学习建议

本讲把「组件如何被实例化」「插件如何被加载」补齐后，u5 组件与模块架构单元已完整。建议按以下顺序继续：

- **进入 u6「按键处理流水线」**：现在你已经具备看清「一次按键如何穿过 Processors→Segmentors→Translators→Filters」所需的全部前置——Ticket 提供上下文、Registry 提供组件、Module 提供注册。从 [u6-l1 引擎流水线总览](u6-l1-pipeline-overview.md) 开始，你会看到这些零件如何协作。
- **如果想立刻动手做插件**：跳到 [u9-l6 插件开发实战](u9-l6-plugin-development.md)，以 `sample` 插件为模板，亲手实现一个自定义 `Translator`，把本讲的 `Ticket`、`RIME_REGISTER_MODULE`、`Registry::Register` 一次用全。
- **延伸阅读源码**：可对照 `plugins/` 目录下任意真实插件（若已通过 `RIME_PLUGINS` 环境变量拉取），看它的 `*_module.cc` 如何在 `RIME_REGISTER_MODULE(name)` 之前用 `Register` 登记组件，验证本讲的命名约定与自注册机制。
