# ICU4X 是什么：定位、背景与设计目标

## 1. 本讲目标

本讲是整本学习手册的第一篇。读完本讲后，你应当能够：

- 用自己的话说清「ICU4X 是什么」、它解决什么问题，以及它和传统 ICU4C/ICU4J、ECMA-402、CLDR 之间的关系。
- 记住 ICU4X 的四大核心设计目标，并理解每一条目标背后要解决的工程矛盾。
- 说出 ICU4X 的目标平台（Web、移动端、嵌入式等）和支持的编程语言生态。

本讲不写代码、不讲算法，只帮你建立一张正确的「全局地图」。有了这张地图，后续每一篇讲义你都知道自己站在哪里。

## 2. 前置知识

本讲面向完全没接触过 ICU4X 的读者。你只需要了解以下几个基本概念：

- **国际化（i18n）**：让同一份软件能适配不同语言、地区、文化的处理方式。比如日期怎么写、数字怎么分位、文字怎么排序，都因地区而异。
- **Unicode**：一套给世界上几乎所有字符都分配统一编号的标准，是所有现代文本处理的基础。
- **CLDR（Common Locale Data Repository）**：Unicode 联盟维护的「locale 数据仓库」，里面存放着各地区「星期一该叫什么」「数字千分位用逗号还是点」之类的规则数据。
- **Rust**：ICU4X 的实现语言。你不需要现在就会 Rust，本讲只有极少量 Rust 代码片段，用来展示项目长什么样。

如果你对上面任意一个名词陌生，不用担心，本讲会用通俗语言逐一解释。

## 3. 本讲源码地图

本讲涉及的关键文件都很「顶层」，是了解项目全貌的入口文档：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目的门面，给出项目一句话定位、四大设计目标、一段最小示例代码，以及治理与许可信息。 |
| `documents/process/charter.md` | ICU4X 的「章程」，是最权威的设计意图来源：设计约束、范围（Scope）、目标平台、以及一组常见问答（为什么新建项目而不是改进 ICU 等）。 |
| `CONTRIBUTING.md` | 贡献指南，描述目录职责、开发环境、数据类型与发布就绪规则。本讲只用它来佐证「组件/FFI/数据/工具」的目录划分与发布要求。 |

> 说明：本讲引用的是文档而非代码逻辑，所以「源码精读」环节会聚焦在这些文档的关键段落上，并给出永久链接与行号。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 项目定位与历史背景**：ICU4X 是什么、从哪里来、要取代什么。
- **4.2 四大设计目标解读**：逐条理解项目的核心约束。
- **4.3 目标平台与语言生态概览**：ICU4X 跑在哪里、服务哪些语言。

### 4.1 项目定位与历史背景

#### 4.1.1 概念说明

「国际化库」并不稀奇——历史上最著名的是 **ICU4C**（C/C++ 实现）和 **ICU4J**（Java 实现），它们是服务端国际化的「黄金标准」。而 **ICU4X**（读作 ICU for X）是 Unicode 联盟启动的一个**全新项目**，用 **Rust** 从零重写，目标不是服务端，而是**客户端与资源受限环境**（手机、穿戴设备、浏览器、嵌入式）。

为什么需要新项目而不是继续用 ICU4C/ICU4J？一句话概括：**客户端的需求和服务端根本不同**。服务端追求高性能、不在乎包体积；而客户端（尤其手机、穿戴设备）对代码体积和内存极度敏感。把为服务端优化了几十年的 ICU 改造成客户端友好的库，相当于一次几乎重写的大改造，还不如新建。

ICU4X 还站在两个「前辈」的肩膀上：

- 继承 **ICU4C/ICU4J** 的设计与 API 经验；
- 以 **ECMA-402**（JavaScript `Intl.*` 系列国际化的标准）作为功能范围的参照系；
- 数据来源是 **CLDR**、Unicode、ICU 与时区数据库。

#### 4.1.2 核心流程

理解 ICU4X 定位，可以顺着这条「为什么链」走：

1. 客户端（IoT / 移动 / Web）需要**设备端（on-device）**国际化。
2. 旧方案（如 Closure i18n、Dart Intl）是「半成品」，维护负担重，且各自为政。
3. 直接改造服务端 ICU 代价过大、且无法满足「内存安全 + Rust + 小体积」的新需求。
4. → 结论：新建一个从第一天就把「小体积 + 可插拽数据」作为首要约束的项目，即 **ICU4X**。

它的最终目标是：**用一套 Rust 核心，通过 FFI / WebAssembly 等方式，统一服务多种客户端语言**，从而逐步替代那些零散的客户端国际化方案。

#### 4.1.3 源码精读

项目的「一句话定位」写在 README 开头：

[README.md:L18-L21](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/README.md#L18-L21) —— 这几行说明 ICU4X 提供广泛的国际化组件、源自 ICU4C/J 与 ECMA-402 的经验、依赖 CLDR 数据，且完全用 Rust 实现。

更权威的设计意图在章程开头：

[charter.md:L4-L6](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/documents/process/charter.md#L4-L6) —— 明确 ICU4X 是「一套模块化的国际化组件，适合在客户端与资源受限环境中使用」，并且建立在 ICU4C/ICU4J 与 ECMA-402 的设计与 API 决策之上。

关于「是不是要取代 ICU」，章程有专门的问答：

[charter.md:L65-L67](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/documents/process/charter.md#L65-L67) —— 这里说 ICU4X 希望最终替代客户端方案（Closure i18n、Dart Intl），而 ICU4C/ICU4J 会继续作为服务端/高资源环境的黄金标准。**注意：ICU4X 不是要取代服务端的 ICU。**

而「为什么不改进 ICU」的深层原因在另一段问答中：

[charter.md:L69-L77](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/documents/process/charter.md#L69-L77) —— 解释了 ICU 历史上为服务端优化积累的「包袱（cruft）」，把它改造成客户端库需要彻底重写数据加载机制、拆解 Java 类依赖、重构单例缓存，还要保证不损害服务端性能；更重要的是它解决不了 Fuchsia、Mozilla 对「内存安全的 Rust ICU」的迫切需求。

#### 4.1.4 代码实践

> **实践目标**：用自己的话复述 ICU4X 的定位，并区分它与 ICU4C/ICU4J 的关系。

**操作步骤**：

1. 打开 [README.md:L18-L32](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/README.md#L18-L32)，阅读项目定位段落。
2. 打开 [charter.md:L65-L77](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/documents/process/charter.md#L65-L77)，阅读「Is ICU4X going to replace ICU?」与「Why make a new project instead of improving ICU?」两段。
3. 用三句话写下你的理解。

**需要观察的现象 / 预期结果**：

你的三句话应当覆盖三个要点：(1) ICU4X 是用 Rust 写的、面向客户端与资源受限环境的国际化组件库；(2) 它源自 ICU4C/ICU4J 与 ECMA-402 的经验，依赖 CLDR 数据；(3) 它不打算取代服务端的 ICU，而是希望统一/替代各种零散的客户端国际化方案。

> 本实践是「文档阅读型」，不涉及运行命令，因此不标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：ICU4X 是用哪种编程语言实现的？为什么 charter 里强调「实现语言只是一个内部细节」？

> **参考答案**：用 Rust 实现。charter 在「Why not call it icu4rust?」一节里解释：实现语言是抽象的内部细节、可能随时间改变，ICU4X 的焦点是客户端（Web、Android、iOS 等），Rust 只是众多客户端之一。

**练习 2**：ICU4X 和 ICU4C/ICU4J 是竞争关系吗？

> **参考答案**：不是。charter 明确 ICU4C/ICU4J 仍是服务端/高资源环境的黄金标准，ICU4X 聚焦客户端与资源受限环境；二者分工互补。

### 4.2 四大设计目标解读

#### 4.2.1 概念说明

ICU4X 从立项第一天就确立了四条设计约束，它们贯穿整个仓库的所有 crate：

1. **小而模块化（Small and modular）**：代码体积小、内存占用低，且按模块拆分，让你只编译用得上的部分。
2. **可插拔的 locale 数据（Pluggable locale data）**：国际化高度依赖数据（CLDR），ICU4X 把「数据从哪里来」做成可插拔的，数据可以编译进二进制，也可以运行时从文件/blob 加载，甚至按需生成。
3. **在多种编程语言中可用且易用（Availability and ease of use in multiple languages）**：一套 Rust 核心，通过 FFI 暴露给 C/C++、JS、Dart、Java 等。
4. **由国际化专家编写以鼓励最佳实践（Written by i18n experts）**：强调「对所有语言和 locale 都产出正确结果」，没有任何一种语言/地区应处于结构性劣势。

理解这四条非常重要——后面你会看到，仓库为什么用「组件 / 数据提供器 / 零拷贝工具」三大块来组织，本质上都是为了实现这四条目标。

#### 4.2.2 核心流程

四条目标之间存在内在的因果关系，可以理解成一个「设计推导链」：

```text
目标3（跨语言易用）
      │  需要一个统一、可移植的核心
      ▼
选择 Rust + FFI/WASM 作为实现与分发手段
      │
      ├─► 目标1（小而模块化）：让客户端能塞下 → 逐 crate 拆分 + 零拷贝
      ├─► 目标2（可插拽数据）：不同客户端数据来源不同 → DataProvider 抽象
      └─► 目标4（专家编写/正确性）：保证所有语言结果正确 → 以 ECMA-402/CLDR 为准
```

其中「正确性」是底线：charter 写明「Above all, ICU4X code will produce correct results for all languages and locales.」——任何体积、性能上的取舍都不能以牺牲正确性为代价。

#### 4.2.3 源码精读

README 列出的四条设计目标：

[README.md:L23-L29](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/README.md#L23-L29) —— 这是项目对外的「四大设计目标」原文。

charter 中对第一条目标的措辞更完整，多出了「fast, low-memory」：

[charter.md:L10-L15](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/documents/process/charter.md#L10-L15) —— 注意 charter 里第一条是「Small, modular, fast, low-memory code」，比 README 更强调「快」和「低内存」。

关于「正确性优先」的最高准则：

[charter.md:L17](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/documents/process/charter.md#L17) —— 「Above all, ICU4X code will produce correct results for all languages and locales. No language or locale should be at a structural disadvantage.」

为了让这四条目标落到工程上，仓库用不同目录承载不同职责。CONTRIBUTING 里有一条「发布就绪」规则点出了这些核心目录：

[CONTRIBUTING.md:L93-L98](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/CONTRIBUTING.md#L93-L98) —— 它指出 `components/`、`ffi/`、`provider/`、`utils/` 四棵树里的代码必须随时可发布。这正是四大目标在目录结构上的映射：`components`（模块化组件，对应目标 1/4）、`provider`（可插拂数据，对应目标 2）、`ffi`（跨语言，对应目标 3）、`utils`（支撑小体积/零拷贝的底层工具，对应目标 1）。

为了让你直观感受「易用」，README 给了一段最小示例（用西班牙语格式化日期）：

[README.md:L47-L65](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/README.md#L47-L65) —— 这段代码会输出 `"12 de septiembre de 2020"`，体现了「用很少的代码得到正确的本地化结果」。你暂时不需要读懂每个 API，只需注意到：`locale!("es")` 指定语言、`YMD::long()` 指定显示字段与长度，数据默认已经「编译进来了（compiled data）」——这正是「可插拽数据」的默认形态。

#### 4.2.4 代码实践

> **实践目标**：把四大设计目标与仓库结构对应起来，建立「目标 ↔ 目录」的直觉。

**操作步骤**：

1. 重读 [README.md:L23-L29](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/README.md#L23-L29) 的四条目标。
2. 阅读 [CONTRIBUTING.md:L93-L98](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/CONTRIBUTING.md#L93-L98) 提到的 `components/`、`ffi/`、`provider/`、`utils/`。
3. 在你的笔记里画一张表，把每条设计目标映射到一个或多个目录。

**需要观察的现象 / 预期结果**：你应当能写出类似这样的映射（示例答案，非仓库原文）：

| 设计目标 | 主要承载目录 |
| --- | --- |
| 小而模块化 | `components/`（按功能拆成多个 crate）、`utils/`（零拷贝/紧凑数据工具） |
| 可插拔 locale 数据 | `provider/`（核心抽象、适配器、各种存储后端、数据生成） |
| 多语言可用 | `ffi/`（Diplomat 桥接、各语言绑定） |
| 专家编写 / 正确性 | 全仓库，以 `components/` 的算法实现与测试为代表 |

> 本实践为「源码阅读型」，无需运行命令；上表是教学用的归纳，具体 crate 在后续讲义中逐一展开。

#### 4.2.5 小练习与答案

**练习 1**：charter 第 1 条目标比 README 多了哪两个词？它们反映了客户端的什么诉求？

> **参考答案**：多了「fast」和「low-memory」。这反映了客户端/嵌入式环境对运行速度和内存占用的苛刻要求——这正是 ICU4X 区别于服务端 ICU 的核心。

**练习 2**：为什么说「可插拔 locale 数据」对客户端尤其重要？

> **参考答案**：客户端设备资源有限，不能像服务端那样把全部 CLDR 数据都带在身上。可插拔意味着可以按需选择 locale、按需生成/加载、甚至运行时更新数据，从而控制体积和内存。具体的机制（DataProvider）会在第 5 单元讲。

### 4.3 目标平台与语言生态概览

#### 4.3.1 概念说明

明确「为谁服务」是理解一个项目行为取舍的关键。ICU4X 的目标平台分两层：

- **目标运行平台**：Web 平台（V8、SpiderMonkey、JSC 等引擎）、软件平台（Fuchsia、Gecko）、移动操作系统（iOS、Android）、带 `alloc` 的低功耗操作系统（WearOS、WatchOS）、客户端工具包（Flutter）。
- **目标编程语言**：Rust（原生）、JavaScript、Objective-C、Java、Dart、C++。

这两层清单不是装饰——charter 明确说，当需要在「底层 ICU 式 API」与「高层 ECMA-402 式 API」之间做取舍时，就参考这些清单做决策。也就是说，**ICU4X 的 API 形态是被这些平台和语言反向塑造的**。

此外，charter 还提到 ICU4X 的一个重要子集要支持 **`no_std`**（不依赖标准库），未来探索 `no_std` + `alloc`。这正是它能在穿戴设备/嵌入式上跑起来的前提。

#### 4.3.2 核心流程

可以把「一套核心服务多语言」的过程简化成下图：

```text
            ┌─────────────────────────────────────────┐
            │   ICU4X 核心：Rust（no_std + alloc 友好）  │
            │   组件 + 可插拽数据 + 零拷贝工具          │
            └──────────────────────┬──────────────────┘
                                   │ 通过 FFI / WASM 暴露
   ┌────────────┬────────────┬─────┴──────┬────────────┬────────────┐
   ▼            ▼            ▼            ▼            ▼            ▼
 Rust        JS/WASM      Obj-C/C++     Java       Dart        嵌入式
 (原生)     (Web 平台)   (iOS/macOS)  (Android)   (Flutter)   (FreeRTOS…)
```

要点：

- Rust 既是实现语言，也是一种「客户端」（如 Fuchsia、Gecko 内部直接用）。
- 其他语言通过 FFI 边界调用同一套核心，**逻辑不重复实现**。
- 嵌入式/低功耗平台靠 `no_std` + 零拷贝数据来省内存。

#### 4.3.3 源码精读

charter 的「Target platforms」一节是权威清单：

[charter.md:L33-L55](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/documents/process/charter.md#L33-L55) —— 这里同时列出了运行平台清单与编程语言清单，并说明二者都是动态的、用于评估设计取舍。

README 的 Charter 摘要里也复述了平台与语言：

[README.md:L88](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/README.md#L88) —— 这一行给出完整的平台与语言清单：Web、iOS、Android、WearOS、WatchOS、Flutter、Fuchsia，以及 Rust、JavaScript、Objective-C、Java、Dart、C++。

README 顶部的徽章则反映了项目在多种语言生态里实际的「分发」情况：

[README.md:L9-L11](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/README.md#L9-L11) —— 三个徽章分别指向 crates.io（Rust）、npm（JavaScript）、pub.dev（Dart），印证了「跨语言易用」目标已经落地为真实的包分发。

关于嵌入式友好（`no_std`），charter 的相关说明：

[charter.md:L55](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/documents/process/charter.md#L55) —— ICU4X 的一个可行子集会以 `no_std` 为目标，未来探索 `no_std` + `alloc`。（`no_std` / `alloc` 的具体工程含义会在第 8 单元讲。）

#### 4.3.4 代码实践

> **实践目标**：动手从文档中提取「平台」与「语言」两份清单，并验证项目确实在多语言生态分发。

**操作步骤**：

1. 打开 [charter.md:L37-L51](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/documents/process/charter.md#L37-L51)，把「Current list of target platforms」和后面的编程语言清单分别抄成两张表。
2. 打开 [README.md:L9-L11](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/README.md#L9-L11)，点开 crates.io / npm / pub.dev 三个链接，确认 `icu` / `icu4x` 在这些生态里确实有发布版本。

**需要观察的现象 / 预期结果**：

- 你应能列出至少 3 种目标平台（如 Web Platform、iOS、Android、Flutter、Fuchsia、WearOS/WatchOS 等）。
- 你应能列出至少 3 种目标编程语言（Rust、JavaScript、Objective-C、Java、Dart、C++）。
- 在 crates.io / npm / pub.dev 上能看到对应的发布包（具体版本号随时间变化，以实际页面为准）。

> 若当前环境无法联网查看发布页面，则包版本一项标注「待本地验证」；清单本身直接来自仓库文档，可离线确认。

#### 4.3.5 小练习与答案

**练习 1**：charter 说平台清单和语言清单的主要用途是什么？

> **参考答案**：当需要在「底层 ICU 式 API」和「高层 ECMA-402 式 API」之间做功能/API 取舍时，用这两份清单来评估决策。它们代表当前行业的真实需求，会随时间演进。

**练习 2**：ICU4X 为什么强调 `no_std` 支持？

> **参考答案**：为了覆盖 WearOS、WatchOS 等低功耗/资源受限平台，这类环境往往没有完整的标准库。`no_std` 让 ICU4X 的一个可行子集能在没有 `std` 的情况下编译运行。

## 5. 综合实践

把本讲三个模块串起来，完成一个「项目画像」小任务：

1. **定位**：阅读 [README.md:L18-L21](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/README.md#L18-L21) 与 [charter.md:L4-L6](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/documents/process/charter.md#L4-L6)，写一段 3 句话的项目简介（它是什么、用什么写、服务谁）。
2. **目标**：对照 [README.md:L23-L29](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/README.md#L23-L29) 的四大目标，给每条目标写一句话解释，并在仓库里指出至少一个「承载该目标」的顶层目录（提示：`components/`、`provider/`、`ffi/`、`utils/`）。
3. **生态**：从 [charter.md:L37-L51](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/documents/process/charter.md#L37-L51) 中各挑出至少 3 个平台和 3 种语言，并尝试在 [README.md:L9-L11](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/README.md#L9-L11) 的三个分发渠道里找到对应的实际发布（若离线则标注「待本地验证」）。

**预期成果**：一张「定位 + 四大目标 + 平台/语言」的项目画像。它就是你后续阅读所有源码讲义时的「坐标系」。

## 6. 本讲小结

- ICU4X 是 Unicode 联盟用 **Rust** 从零重写的**模块化国际化组件库**，面向**客户端与资源受限环境**，源自 ICU4C/ICU4J 与 ECMA-402 的经验、依赖 CLDR 数据。
- 它**不取代服务端的 ICU4C/ICU4J**，而是希望统一/替代各种零散的客户端国际化方案（如 Closure i18n、Dart Intl）。
- 四大设计目标是：**小而模块化、可插拔 locale 数据、跨语言易用、由 i18n 专家编写（正确性优先）**；其中 charter 特别强调「快、低内存」与「所有语言/locale 都正确」。
- 这四条目标直接映射到仓库结构：`components/`（模块化组件）、`provider/`（可插拽数据）、`ffi/`（跨语言）、`utils/`（小体积/零拷贝工具）。
- 目标平台覆盖 Web、移动端（iOS/Android）、穿戴/低功耗（WearOS/WatchOS）、Flutter、Fuchsia；目标语言覆盖 Rust、JS、Objective-C、Java、Dart、C++，并通过 crates.io / npm / pub.dev 实际分发。
- ICU4X 的一个子集支持 `no_std`（未来探索 `no_std` + `alloc`），这是它能进入嵌入式的前提。

## 7. 下一步学习建议

建立全局印象后，建议按以下顺序继续：

- **下一讲（u1-l2）仓库结构与 Cargo 工作区组织**：动手看 `Cargo.toml` 工作区，把本讲提到的 `components/`、`provider/`、`ffi/`、`utils/` 落到具体的 crate 上。
- **u1-l3 搭建环境与运行第一个应用**：跟着 quickstart 跑通本讲 4.2 节那段日期格式化示例。
- **延伸阅读**：若想更深入理解设计取舍，可精读 [charter.md](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/documents/process/charter.md) 的「Scope」与全部 FAQ；若关心贡献流程，可读 [CONTRIBUTING.md](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/CONTRIBUTING.md) 的「Release Readiness」与「Testing」两节。
