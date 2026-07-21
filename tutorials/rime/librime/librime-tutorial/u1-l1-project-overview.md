# 项目总览：RIME 输入法引擎是什么

## 1. 本讲目标

本讲是整本《librime 学习手册》的第一篇，面向**从未接触过 librime 源码**的读者。读完本讲，你应当能够：

- 说清楚 **librime 是什么**：一个用 C++ 写的、跨平台的「输入法引擎」，而不是带界面的输入法程序。
- 划清 **「引擎」与「前端」的职责边界**：知道为什么 librime 自己不弹候选窗口，而是要搭配 Squirrel / Weasel / ibus-rime 等前端使用。
- 复述 RIME 的 **核心特性**：方案（schema）、拼写代数（Spelling Algebra）、OpenCC 繁简转换、和弦输入（chord-typing）。
- 认出 librime 依赖的 **开源技术栈**：Boost、LevelDB、marisa、OpenCC、yaml-cpp、glog，并区分「构建依赖」与「运行时依赖」。

本讲几乎全部内容来自项目根目录的 [README.md](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/README.md)。我们的目的是**先建立全局地图**，不纠结代码细节——那是后续讲义的任务。

## 2. 前置知识

在开始之前，最好对下面几个概念有一点直觉。不熟悉也没关系，我们会顺带解释。

- **输入法（Input Method, IM）**：当你用键盘敲 `n-i-h-a-o`，屏幕上却出现「你好」时，中间那段「把按键翻译成汉字」的程序就是输入法。它要解决的核心问题是：键盘上只有几十个键，而汉字有几万个。
- **音码 vs 形码**：拼音、注音这类按「发音」输入的方案叫**音码（phonetic-based）**；仓颉、五笔这类按「字形」拆分输入的叫**形码（shape-based）**。librime 两类都支持。
- **繁体 / 简体**：汉字有繁体（如「臺灣」）与简体（如「台湾」）之分，不同地区还用不同字形标准（如港台用「台」 vs 大陆用「台」字形差异）。OpenCC 是专门做这类转换的开源库。
- **YAML**：一种用缩进表示层级的配置文件格式。RIME 的输入方案就是用 YAML 写的，本讲后面会看到。
- **C++17 / CMake**：librime 用 C++17 编写，用 CMake 构建。这些是工具链层面的概念，本讲只要求你知道它们的存在。

> 一句话直觉：**librime 是一个「只管把按键变成文字，不管怎么显示」的库。** 显示和交互交给前端。

## 3. 本讲源码地图

本讲涉及的文件非常少，主要就是一份说明文档：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/README.md) | 项目门面文档，记录定位、特性、依赖、构建方式、前端与插件清单。本讲的主要依据。 |
| [data/minimal/](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml) | 最小可运行数据集，里面有 `luna_pinyin.schema.yaml`（明月拼音方案）。本讲只用来「看一眼」方案长什么样，不讲细节。 |
| `src/`、`src/rime/` | 源码目录。本讲不深入，只在最后给出一张「后续会去哪里读」的地图。 |

> 提示：本讲的「源码精读」其实是「文档精读」。这是有意为之——第一篇讲义的目标是**理解项目定位**，而不是读懂某段 C++。从第二篇开始，我们才会真正进入代码。

## 4. 核心概念与源码讲解

本讲把 README 拆成四个最小模块来讲：**项目定位**、**核心特性**、**技术栈与依赖**、**前端生态与插件**。

### 4.1 项目定位：输入法引擎 vs 前端

#### 4.1.1 概念说明

很多人第一次接触 RIME 时会困惑：「我装了鼠须管（Squirrel），那 librime 又是什么？」

答案是：**librime 是「引擎」，Squirrel 是「前端」。** 它们是分工合作的关系。

- **引擎（engine）**：负责输入法的「大脑」——接收按键、维护输入状态、查词典、生成候选词、决定提交什么文字。它**没有界面**，不画候选框，不处理鼠标，不和操作系统的输入法框架深度耦合。
- **前端（frontend）**：负责输入法的「身体」——在屏幕上画出候选窗口、接收来自操作系统（IBus / Fcitx / Text Input Services 等）的按键事件、把引擎算出来的候选结果展示给用户。

把两者拆开的好处是：**一份引擎代码，可以对接任意平台的前端。** librime 用跨平台的 C++ 写成，于是 macOS、Windows、Linux、Android、iOS、甚至 Vim/Emacs，都能复用同一套输入逻辑。

#### 4.1.2 核心流程

一次按键从敲下到上屏，引擎与前端的大致分工如下：

```text
用户敲键 ──▶ [前端] 捕获按键事件（来自 OS 输入法框架）
          │
          ▼
        [引擎 librime] process_key(...)
          │  内部流水线：Processor → Segmentor → Translator → Filter
          │  产出：候选词列表 + 是否有文字被「提交」(commit)
          ▼
        [前端] 读取候选列表 → 在屏幕上画出候选窗口
          │
          ▼
        用户选词 ──▶ [引擎] 提交文字 ──▶ [前端] 把文字送到目标应用
```

关键点：**「算」都在引擎里，「画」和「与操作系统打交道」都在前端里。** 这就是为什么 README 把 librime 称为 *a modular, extensible input method engine*（一个模块化、可扩展的输入法引擎）。

#### 4.1.3 源码精读

README 的第一段（标题与标语）就点明了项目性质：

> RIME: Rime Input Method Engine
> Rime with your keystrokes.

对应文件开头：[README.md:1-9](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/README.md#L1-L9)（标题、徽章与标语）。

而 **Features** 段的第一条，给出了最权威的定位描述：

> - A modular, extensible input method engine in cross-platform C++ code,
>   built on top of open-source technologies

这句话里每一个定语都值得记住：**modular（模块化）** 说明内部按组件拼装；**extensible（可扩展）** 说明可以加插件；**cross-platform（跨平台）** 说明它不绑定某一个 OS；**built on top of open-source technologies** 说明它站在 Boost、OpenCC 等开源库的肩膀上。对应：[README.md:19-22](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/README.md#L19-L22)。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「引擎 / 前端」的分工在 README 里是如何落笔的。

**操作步骤**：

1. 打开 [README.md 的 Frontends 段](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/README.md#L71-L77)。
2. 注意三个「Official（官方）」前端分别对应三个桌面平台：
   - **ibus-rime**：Linux 上的 IBus 前端
   - **Squirrel（鼠须管）**：macOS 前端
   - **Weasel（小狼毫）**：Windows 前端
3. 想象一个场景：你在 macOS 上用 Squirrel 打字。此时 Squirrel 负责画候选框、接收系统按键，而真正的「ni hao → 你好」这步计算，是 Squirrel 调用 librime 完成的。

**需要观察的现象**：README 把这三个项目放在「Frontends」标题下，而不是「Download」或「Binary」下——这本身就是一种声明：**librime 仓库里并没有成品输入法程序，要真正打字，你得装一个前端。**

**预期结果**：你能用自己的话讲出「如果我只想在 Windows 上用 RIME 打字，我应该装 Weasel；Weasel 内部会调用 librime 这个引擎」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 librime 不自己画一个候选窗口？

> **参考答案**：因为画窗口、与操作系统的输入法框架交互是「平台相关」的工作（macOS 用 Input Method Kit，Windows 用 TSF，Linux 用 IBus/Fcitx）。把这部分交给各平台的前端，librime 自身就能保持一份纯 C++ 的跨平台核心，专注于「按键 → 文字」的算法逻辑。

**练习 2**：如果有人说「我用的是 fcitx5-rime」，这里的 fcitx5-rime 扮演什么角色？

> **参考答案**：fcitx5-rime 是一个**前端**（社区前端，见 [README.md:93](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/README.md#L93)），它对接 Linux 的 Fcitx5 输入法框架，内部调用 librime 引擎来计算候选。

---

### 4.2 RIME 的核心特性

#### 4.2.1 概念说明

README 的 **Features** 段列出了 librime 的六大卖点。我们逐条用大白话翻译：

1. **跨平台 C++ 引擎**：已在 4.1 讲过。
2. **同时支持音码和形码**：既能做拼音（音码），也能做仓颉、五笔（形码），覆盖面广。
3. **原生繁体 + OpenCC 转换**：内部以繁体中文为基，按需用 OpenCC 转成简体或各地区字形标准。
4. **Rime 输入方案（schema）**：一套用 YAML 写的「领域专用语言（DSL）」，让你能快速试验新的输入法设计，而不用改 C++ 代码、重新编译。
5. **拼写代数（Spelling Algebra）**：一种机制，用来从一个「基础拼写」派生出变体拼写——这对设计方言输入法特别有用（比如同一个音在不同方言里的多种拼法）。
6. **和弦输入（chord-typing）**：支持同时按下多个键来输入一个音，类似钢琴和弦。combo-pinyin 就是这种玩法。

#### 4.2.2 核心流程

「方案（schema）」是理解 RIME 的钥匙。可以用下面的关系来理解：

```text
   输入方案 *.schema.yaml  （YAML 写的配置 / DSL）
        │
        │  描述了：用哪些 Processor / Segmentor / Translator / Filter
        │         词典在哪、拼写代数规则、开关、繁简选项……
        ▼
   librime 引擎 读取方案 ──▶ 装配出一条「按键处理流水线」
        │
        ▼
   就能按这套方案打字了
```

也就是说：**换一个方案，等于换一种输入法**，但引擎代码不变。拼音、双拼、仓颉、五笔、方言……都是不同的方案。`data/minimal/` 里的 `luna_pinyin.schema.yaml` 就是「明月拼音」方案，`cangjie5.schema.yaml` 是「仓颉五代」方案。

而「拼写代数」是方案里的一种规则段（`speller/algebra`），它的作用是在词典之外，再生成一批「等价拼法」。例如拼音里允许把 `zh` 简写成 `v`，就是用拼写代数派生出来的。详细原理见进阶层 u7 单元。

#### 4.2.3 源码精读

README 的 Features 段把这六条特性列得很清楚，对应：[README.md:19-32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/README.md#L19-L32)。其中关于方案与拼写代数的原文是：

> - Rime input schema, a DSL in YAML syntax for fast trying out innovative ideas
>   of input method design
> - Spelling Algebra, a mechanism to create variant spelling, especially useful
>     for Chinese dialects

注意两个关键词：schema 被明确称为 **DSL in YAML syntax**（用 YAML 语法写的领域专用语言）；拼写代数被描述为用来 **create variant spelling**（创造变体拼写）。这两句话是后续 u4（配置系统）和 u7（拼写代数）两整个单元的起点。

#### 4.2.4 代码实践

**实践目标**：在真实数据文件里「瞄一眼」方案的样子，建立对 schema 的直观感受。

**操作步骤**：

1. 打开 [data/minimal/luna_pinyin.schema.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml)。
2. 不必读懂细节，只找下面几个顶层键（它们是几乎所有方案都会出现的骨架）：
   - `schema`: 方案的元信息（名字、id、版本）。
   - `switches`: 开关，比如中/英文、简/繁。
   - `engine`: 声明流水线要挂载哪些组件，会分成 `processors` / `segmentors` / `translators` / `filters` 四组。
   - `speller`: 拼写相关配置，里面有 `algebra:` 段——那就是拼写代数规则。
   - `translator`: 翻译器配置，会指向一个词典。

**需要观察的现象**：你会看到一个 `.yaml` 文件就描述了一整套拼音输入法的行为，却没有任何 C++ 代码。这正是「方案即 DSL」的体现。

**预期结果**：你能指出 `speller/algebra` 这一段在哪，并能说出「这里写的规则会被引擎用来派生变体拼写」。具体的运算符（`xform/derive/abbrev/...`）留到 u7 再讲。

> 说明：本实践是「源码阅读型实践」，不要求你运行任何程序，只要打开文件观察结构即可。

#### 4.2.5 小练习与答案

**练习 1**：Features 里说 schema 是 "a DSL in YAML syntax"。DSL 是什么意思？为什么说 schema 是一种 DSL？

> **参考答案**：DSL = Domain-Specific Language（领域专用语言），指为某个特定领域定制的「小语言」，而不是通用编程语言。schema 用 YAML 语法，但它的关键字（如 `engine`、`speller/algebra`、`switches`）只在「描述输入法」这个领域有意义，所以称为 DSL。它的好处是：不重新编译 librime，只改 YAML 就能改变输入法行为。

**练习 2**：拼写代数（Spelling Algebra）和 OpenCC 分别解决什么问题？它们是一回事吗？

> **参考答案**：不是一回事。拼写代数处理的是**按键到音节/编码的映射**——比如让 `v` 也能代表 `zh`，属于「输入侧」。OpenCC 处理的是**汉字到汉字的转换**——比如把候选词「臺灣」转成「台湾」，属于「输出侧」。前者生成更多可接受的输入拼法，后者变换最终输出的汉字字形。

---

### 4.3 技术栈与依赖

#### 4.3.1 概念说明

librime 不是从零造轮子，而是站在一批成熟开源库之上。README 把依赖分成了两组，理解这两组的区别非常重要：

- **Build dependencies（构建依赖）**：编译 librime 时必须存在的工具/库。包括编译器、构建工具，以及运行测试需要的库。
- **Runtime dependencies（运行时依赖）**：运行 librime 时需要的库。它是构建依赖的一个**子集**——因为有些库只在编译/测试期需要（比如 gtest），运行时并不依赖。

#### 4.3.2 核心流程

下面这张表把 README 里出现的依赖逐一翻译成「它在 librime 里大概干什么」。本讲只要求你有印象，具体用在哪一行的代码，后续单元会展开。

| 依赖库 | 类别 | 在 librime 中的作用（直觉版） |
| --- | --- | --- |
| Boost（libboost ≥ 1.74） | 构建 + 运行 | C++ 通用工具库：信号槽、字符串处理、`boost::dll`（动态加载插件）等。 |
| LevelDB（libleveldb） | 构建 + 运行 | Google 的嵌入式 KV 数据库，用来存「用户词典」（你打字的学习记录）。 |
| marisa（libmarisa） | 构建 + 运行 | 高效的 trie（前缀树）索引库，用来做音节/拼写索引（Prism）。 |
| OpenCC（libopencc ≥ 1.0.2） | 构建 + 运行 | 繁简/地区字形转换，对应特性里的 Simplifier。 |
| yaml-cpp（libyaml-cpp ≥ 0.5） | 构建 + 运行 | 解析 YAML，加载输入方案和配置。 |
| glog（libglog ≥ 0.7） | 构建 + 运行（均**可选**） | Google 的日志库。注意它标注为 *optional*。 |
| Google Test（libgtest） | 仅构建（**可选**） | 单元测试框架，只在编译测试时需要，运行时不依赖。 |
| CMake ≥ 3.12 + C++17 编译器 | 仅构建 | 构建工具链。 |

> 一个常被忽略的细节：**glog 在两组里都标了 optional**，意味着你可以编译出一个「不带 glog」的 librime（对应构建选项 `ENABLE_LOGGING`，下一讲 u1-l2 会见到）。

#### 4.3.3 源码精读

README 的 **Build dependencies** 与 **Runtime dependencies** 两段对应：[README.md:39-58](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/README.md#L39-L58)。原文如下（摘关键行）：

```text
Build dependencies
  - compiler with C++17 support
  - cmake>=3.12
  - libboost>=1.74
  - libglog>=0.7 (optional)
  - libleveldb
  - libmarisa
  - libopencc>=1.0.2
  - libyaml-cpp>=0.5
  - libgtest (optional)

Runtime dependencies
  - libboost
  - libglog (optional)
  - libleveldb
  - libmarisa
  - libopencc
  - libyaml-cpp
```

注意两点（可以直接在源文件里对照验证）：

1. **gtest 只出现在构建依赖，不出现在运行时依赖**——印证了「测试框架只在编译期需要」。
2. **运行时依赖里没有版本号，构建依赖里有**——因为版本要求是「能否编译」的约束，一旦编译产物（`.so` / `.dll`）生成，运行时只看 ABI 是否兼容。

而最权威的「这些库各自的许可证是什么」清单，在 README 末尾的 **Credits** 段：[README.md:122-132](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/README.md#L122-L132)。Credits 里列出的七个库（Boost、glog、Google Test、LevelDB、marisa-trie、OpenCC、yaml-cpp）正好和我们上面表格里的依赖一一对应。

#### 4.3.4 代码实践

**实践目标**：亲手从 README 提炼出「运行时依赖清单」，并区分它与构建依赖。

**操作步骤**：

1. 再次打开 [README.md 的依赖两段](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/README.md#L39-L58)。
2. 在自己的笔记里画一张两列对照表：左列「只在构建期需要」，右列「运行时也需要」。
3. 把每个库填进去。提示：`gtest` 只在左列；`glog` 带括号 optional，两边都可能出现但要标注可选。

**需要观察的现象**：你会发现「运行时也需要」这一列恰好等于「Credits 段感谢的那些库」去掉 gtest。这说明 Credits 段不是随便列的，它就是 librime 实际依赖运行时核心库的清单。

**预期结果**：你能回答下面这个问题——*「如果我想在一台只装了 librime 的 `.so` 但没装 gtest 的机器上运行程序，会出问题吗？」* 答案应该是：**不会**，因为 gtest 不是运行时依赖。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `libgtest` 是「构建依赖（可选）」而不是「运行时依赖」？

> **参考答案**：gtest 是单元测试框架，只在编译 librime 的测试目标（`BUILD_TEST=ON` 时）参与链接，生成的测试可执行文件才需要它。librime 主库本身不依赖 gtest，所以最终运行输入法引擎的机器不需要装 gtest。

**练习 2**：用户词（你的打字习惯）是用哪个依赖库存储的？音节索引又是哪个？

> **参考答案**：用户词典用 **LevelDB** 存储（它是一个嵌入式 KV 数据库，适合频繁读写的学习记录）；音节/拼写索引用 **marisa**（高效的 double-array trie）。详细实现见 u8 单元（词典系统）。

---

### 4.4 前端生态与插件扩展

#### 4.4.1 概念说明

librime 的「可扩展性」体现在两个层面，README 用两个独立段落描述：

- **Frontends（前端）**：把引擎接入各种平台 / 应用。这是「引擎对外」的扩展点。
- **Plugins（插件）**：直接给引擎本身加新功能（新组件、新算法），通常以 `librime-*` 命名的动态库形式存在。这是「引擎对内」的扩展点。

理解这两个层面，能帮你后续看懂源码里的两套加载机制：前端通过 librime 的 C API（`rime_api.h`）调用引擎；插件则通过 `PluginManager` 在引擎启动时被动态加载（u5-l4 会讲）。

#### 4.4.2 核心流程

```text
       librime 引擎核心
        │            │
   ┌────┘            └─────┐
   ▼（对外）              ▼（对内）
 前端 Frontends         插件 Plugins
 （调 C API）          （动态加载 librime-*.so）
   │                        │
   │ ibus-rime / Squirrel    │ librime-lua（Lua 脚本）
   │ Weasel / fcitx5-rime    │ librime-octagram（语言模型）
   │ Trime / Hamster ...     │ librime-predict（预测下一个词）
   │                        │ librime-proto（IPC）...
   ▼                        ▼
 接入 OS 输入法框架        给引擎加新组件/算法
```

注意 README 里 Plugins 段还标了两个 **(Deprecated)** 的插件（`librime-charcode`、`librime-legacy`），说明生态在演进，有些旧模块已经废弃——读文档时要留意这类标记。

#### 4.4.3 源码精读

- **Frontends 段**：[README.md:71-101](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/README.md#L71-L101)。其中三个官方前端（Official）：[README.md:74-77](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/README.md#L74-L77)。
- **Plugins 段**：[README.md:103-112](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/README.md#L103-L112)。可以看到既有活跃插件（lua / octagram / predict / proto），也有标了 `(Deprecated)` 的旧插件。
- **Related works 段**：[README.md:114-120](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/README.md#L114-L120)。这里的 `plum` 是「配置/方案安装器」，`rime-essay` 是「预设词库」——它们和 librime 引擎本身是配合使用的关系，但不是引擎的一部分。

> 一个常被混的边界：**plum 不是前端，也不是插件**。它是「帮你安装方案文件」的工具，属于 Related works。

#### 4.4.4 代码实践

**实践目标**：梳理 RIME 生态里「谁干什么」，避免把前端、插件、配套工具混为一谈。

**操作步骤**：

1. 阅读 [Frontends 段](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/README.md#L71-L101) 与 [Plugins 段](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/README.md#L103-L112) 以及 [Related works 段](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/README.md#L114-L120)。
2. 按下表把项目归类（在笔记里填空）：

| 项目 | 属于（前端/插件/配套工具） | 一句话作用 |
| --- | --- | --- |
| Squirrel | | |
| Weasel | | |
| ibus-rime | | |
| librime-lua | | |
| librime-octagram | | |
| plum | | |
| rime-essay | | |

**需要观察的现象**：你会发现「前端」数量远多于「插件」。这是因为前端要覆盖无数平台和应用（连 Vim、Emacs、Tmux、Zsh 都有），而插件只需要在「引擎核心不够用」时才出现。

**预期结果**：填完后，你应该能脱口而出：*「我想给引擎加 Lua 脚本能力 → 装 librime-lua 插件；我想在 Android 上用 → 装 Trime 前端；我想批量下载一批输入方案 → 用 plum。」*

#### 4.4.5 小练习与答案

**练习 1**：`librime-lua` 和 Squirrel 的本质区别是什么？

> **参考答案**：`librime-lua` 是**插件**，它给 librime 引擎本身增加「用 Lua 脚本扩展输入逻辑」的能力，属于引擎对内的扩展。Squirrel 是**前端**，它把 librime 引擎接入 macOS 系统、负责画候选窗口和与系统交互。两者一个改引擎内部能力，一个改引擎对外的接入方式。

**练习 2**：README 里 Plugins 段有两个 `(Deprecated)`，看到这种标记读者应该怎么做？

> **参考答案**：`Deprecated` 表示已废弃，不再推荐使用、通常也不再维护。看到这个标记应避免在新项目里依赖它，并留意是否有推荐的替代方案（例如 `librime-legacy` 里的功能可能已被合并进主线或被其它插件取代）。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个**一页纸的「librime 速览」**小任务（纯阅读 + 写笔记，不需要编译）：

1. **定位**：用一句话写出 librime 是什么（提示：modular / extensible / cross-platform / engine）。
2. **依赖**：列出 librime 的**运行时依赖**（共 6 个，其中 1 个可选），并标注哪个库负责「用户词典」、哪个负责「音节索引」、哪个负责「繁简转换」。
3. **分工**：写一段话（5–8 句），说明 *librime 引擎* 与 *某个具体前端（如 Squirrel）* 的分工。要求覆盖：谁接收按键、谁算候选、谁画窗口、谁把文字送到目标应用。
4. **生态**：把 Squirrel、Weasel、ibus-rime、librime-lua、plum 五个项目分成「官方前端 / 插件 / 配套工具」三类。
5. **自检**：翻回 [README.md](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/README.md) 核对，确认你的每一条都能在 README 里找到出处。

> 完成后，你就具备了阅读后续讲义的「全局坐标系」：知道这个项目处在生态的哪一层、依赖什么、靠什么扩展。

## 6. 本讲小结

- **librime 是一个跨平台的 C++ 输入法引擎**，只负责「按键 → 文字」的核心计算，不画界面（对应 [README.md:19-22](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/README.md#L19-L22)）。
- **引擎 vs 前端**：librime 是引擎；Squirrel（macOS）、Weasel（Windows）、ibus-rime（Linux）等是前端，负责与操作系统交互和显示候选（对应 [README.md:71-77](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/README.md#L71-L77)）。
- **六大特性**：跨平台 C++、音码+形码、繁体+OpenCC、Rime 方案（YAML DSL）、拼写代数、和弦输入（对应 [README.md:19-32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/README.md#L19-L32)）。
- **方案（schema）是 DSL**：换方案等于换一种输入法，引擎代码不变——这是后续 u4/u6 单元的核心。
- **依赖分两组**：构建依赖（含可选的 glog / gtest）与运行时依赖（gtest 不在其中）；核心库包括 Boost、LevelDB、marisa、OpenCC、yaml-cpp（对应 [README.md:39-58](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/README.md#L39-L58)）。
- **扩展分两层**：前端（对外接入平台）与插件（对内增强引擎，如 librime-lua），另有 plum 等配套工具（对应 [README.md:71-120](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/README.md#L71-L120)）。

## 7. 下一步学习建议

本讲只读了 README，还没碰一行 C++。下一步建议：

- **下一讲 u1-l2《构建与安装：CMake 与依赖》**：动手用 `make` / `cmake` 把 librime 构建出来，把本讲的「依赖」从纸面变成真实链接进来的库。
- **之后 u1-l3《源码目录与分层架构》**：带你认识 `src/rime/` 下的 `algo / config / dict / gear / lever` 五个子目录，建立源码地图。
- **再之后 u1-l4 / u1-l5**：进入 C API 入口 `rime_api.h`，并用 `tools/rime_api_console.cc` 端到端体验一次输入流程。

> 如果你急于看代码，可以现在就扫一眼 [src/rime_api.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h)——那是引擎对前端暴露的总入口，但**建议先跟着顺序走**，打好地图再进源码会更轻松。
