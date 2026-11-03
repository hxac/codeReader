# Metalink 解析：基于 SAX 的状态机

> 本讲属于第 8 单元「Metalink 与 RPC：服务化能力」，承接 u3-l3《任务创建流水线》中 `createRequestGroupForMetalink` 留下的悬念：一个 `.meta4` / `.metalink` 文件，究竟是如何变成 `MetalinkEntry` 列表的。

## 1. 本讲目标

学完本讲，你应当能够：

1. 解释「推（push）式 SAX 回调 + 状态栈」这一解析架构：为什么 SAX 解析器不保存文档结构、aria2 如何用一个显式栈把元素嵌套还原出来。
2. 说出 `MetalinkParserStateMachine`、`MetalinkParserState` 家族、`MetalinkParserController` 三层的职责边界。
3. 对比 Metalink v3 与 v4 两套状态集的差异（命名空间、元素层级、优先级语义、错误处理）。
4. 解释 `XmlParser` 如何用同一个 `ParserStateMachine` 抽象接口同时适配 libxml2 和 Expat 两个 XML 后端。
5. 动手在解析链路上加日志，亲眼看到每个候选资源与校验和被收集的过程。

## 2. 前置知识

### 2.1 Metalink 回顾

在 u1-l1 我们提过：aria2 支持的「五类协议」中，Metalink 与其他四个不同——它不是传输协议，而是一种**元数据格式**，用一个 XML 文件描述「一个文件有哪些镜像、多校验和、分块哈希、签名」。下载时 aria2 从中挑出若干镜像源并行下载，并用其中的校验和保证数据完整性。

Metalink 有两个主要版本：

- **Metalink 3**（旧版，`http://www.metalinker.org/` 命名空间），元素层级较深：`metalink > files > file > verification > hash / pieces > hash`、`resources > url`。
- **Metalink 4**（RFC 5854，`urn:ietf:params:xml:ns:metalink` 命名空间），层级更扁平：`metalink > file > hash / pieces / url / metaurl`，并新增了 `metaurl`（可指向 torrent 或 magnet）。

注意一个常见误解：`--metalink-version` 选项**不是**用来指定「按 v3 还是 v4 格式解析」的——格式版本由 XML 根元素的命名空间 URI 自动判定；`--metalink-version` 是在解析**之后**按文件自己的 `<version>` 元素过滤条目（详见 4.4 节与 [src/Metalinker.cc:L47-L66](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalinker.cc#L47-L66)）。

### 2.2 SAX：推模型的流式解析

解析 XML 有两大流派：

- **DOM（文档对象模型）**：一次性把整棵 XML 树读进内存，之后随意查询。内存开销与文档大小成正比。
- **SAX（Simple API for XML）**：解析器顺序扫描字节流，每遇到一个事件——元素开始、字符数据、元素结束——就**回调（callback）**你注册的函数。它不保存任何结构，内存占用恒定。

SAX 是「推模型」：不是你向解析器要数据，而是解析器把事件一个个推给你。你的代码必须自己记住「我现在在文档的哪个位置」，这就引出了状态机。

### 2.3 为什么需要状态机

SAX 回调是无状态的：`beginElement("hash")` 这个事件，既可能出现在 `file > hash`（整文件校验和），也可能出现在 `file > pieces > hash`（分块哈希）。仅凭事件名无法区分语义，必须知道「当前处于什么状态」。

aria2 的解法是经典的**状态模式（GoF State）**：

- 每个状态是一个对象，实现 `beginElement` / `endElement` 两个钩子；
- 状态机维护一个**状态栈**：遇到开始标签就压栈，遇到结束标签就弹栈——栈的内容恰好镜像了当前元素的嵌套路径；
- 栈顶状态决定「这个事件怎么解释、下一个状态是什么」。

### 2.4 事务（transaction）思想

解析过程中，一个 `<file>` 条目的各种信息（名字、大小、URL、校验和）是**陆续到达**的。`MetalinkParserController` 用「事务」组织这段收集过程：

- `newXxxTransaction()`：新建一个临时对象（如 `tEntry_`、`tResource_`）；
- 一串 `setXxxOf...()`：往临时对象里填字段；
- `commitXxxTransaction()`：把临时对象挂到父对象上；
- `cancelXxxTransaction()`：丢弃临时对象。

所有 `setXxx` 的第一行都是 `if (!tXxx_) return;`——遇到不合法或乱序的文档时，事务不存在，写入自然被忽略。这是一种以「空指针检查」实现的容错。

### 2.5 与前置讲义的衔接

u3-l3 讲过 `Context` 构造函数按 torrent / metalink / input-file / URI 四级互斥分支创建 `RequestGroup`。Metalink 分支的入口在 [src/Context.cc:L262-L269](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L262-L269)：带 `--show-files` 时只解析并打印文件清单后退出；否则进入 `createRequestGroupForMetalink`。本讲就沿着这条线向下钻。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/ParserStateMachine.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ParserStateMachine.h) | 与具体 XML 库无关的抽象接口：任何「吃 SAX 事件的状态机」都实现它 |
| [src/MetalinkParserStateMachine.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateMachine.h) / [.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateMachine.cc) | 状态机本体：状态栈、全部单例状态、通往 Controller 的全部转发方法 |
| [src/MetalinkParserState.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserState.h) | 抽象状态类（三个虚函数 + 默认空实现） |
| [src/MetalinkParserStateImpl.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateImpl.cc) | 公共状态：`Initial`（版本分派）与 `SkipTag`（跳过未知子树），以及 `findAttr` 工具 |
| [src/MetalinkParserStateV3Impl.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateV3Impl.cc) | Metalink 3 的状态集合 |
| [src/MetalinkParserStateV4Impl.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateV4Impl.cc) | Metalink 4（RFC 5854）的状态集合 |
| [src/MetalinkParserController.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserController.cc) | 数据收集器：事务管理、URL 规范化、哈希算法校验、产出 `Metalinker` |
| [src/XmlParser.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/XmlParser.h) / [src/XmlParser.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/XmlParser.cc) | 后端适配层：编译期二选一引入 libxml2 或 Expat 版 `XmlParser`；`xml::parseFile` 提供 4KB 分块读取循环 |
| [src/Xml2XmlParser.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Xml2XmlParser.cc) | libxml2 SAX2 适配器 |
| [src/ExpatXmlParser.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ExpatXmlParser.cc) | Expat 适配器（用 `\t` 分隔符合成命名空间名） |
| [src/metalink_helper.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/metalink_helper.cc) | 高层门面：`parseFile` / `parseAndQuery`，把解析错误转成异常 |
| [src/Metalink2RequestGroup.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc) | 消费端：把 `MetalinkEntry` 变成 `RequestGroup`（u8-l2 详讲） |
| [test/MetalinkProcessorTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/MetalinkProcessorTest.cc)、[test/metalink4.xml](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/metalink4.xml) | 端到端测试与标准 v4 样例文件 |

整体调用链（自上而下）：

```
Context 构造（-M 分支）
  └─ createRequestGroupForMetalink            src/download_helper.cc
       └─ Metalink2RequestGroup::generate     src/Metalink2RequestGroup.cc
            └─ metalink::parseAndQuery        src/metalink_helper.cc
                 ├─ metalink::parseFile ──► MetalinkParserStateMachine（本讲主角）
                 │        └─ xml::parseFile   src/XmlParser.cc
                 │             └─ XmlParser（libxml2 或 Expat 适配，4.5 节）
                 │                  └─ SAX 回调 ──► psm->beginElement / endElement
                 │                        └─ 状态栈 + MetalinkParserController（4.1–4.4 节）
                 └─ Metalinker::queryEntry   （--metalink-version/language/os 过滤，u8-l2）
```

## 4. 核心概念与源码讲解

### 4.1 MetalinkParserStateMachine：状态栈驱动的解析骨架

#### 4.1.1 概念说明

`MetalinkParserStateMachine` 是解析的「总机」：它不认识任何具体元素，只做三件事——

1. **维护状态栈**：栈顶永远是「当前正在解析的元素」对应的状态。
2. **持有 `MetalinkParserController`**：状态对象不直接碰数据，一律通过状态机的几十个转发方法（`newEntryTransaction`、`setURLOfResource`……）间接调用 Controller。这层纯转发的间接性是刻意设计：状态类只依赖状态机接口，数据收集逻辑集中在一处。
3. **管理全部状态单例**：状态对象无字段、可复用，全局只 new 一次（享元 flyweight），永远不释放。

#### 4.1.2 核心流程

两个入口方法的骨架（伪代码）：

```
beginElement(localname, prefix, nsUri, attrs):
    stateStack_.top()->beginElement(this, ...)   # 委派给栈顶状态
    # 状态在 beginElement 内部自行调用 psm->setXxxState() 压入新状态
    # （或 setSkipTagState() 压入"跳过"状态）

endElement(localname, prefix, nsUri, characters):
    stateStack_.top()->endElement(this, ..., characters)  # 先让栈顶消费文本
    stateStack_.pop()                            # 再弹栈
```

由此得到本模块最重要的**不变量**：

- 每个 `beginElement` 事件恰好压栈一次（具体状态或 SkipTag，二选一，检查每个状态的实现可验证所有分支都压栈）；
- 每个 `endElement` 事件恰好弹栈一次；
- 因此**栈深度 = 当前未闭合元素的嵌套深度 + 1**（栈底永远是 `initialState_`）；
- 栈回到只剩 `initialState_` 时，`finished()` 为真——不过 Metalink 场景下上层并不依赖它，文档是否完整由 XML 解析器自己保证。

#### 4.1.3 源码精读

构造函数压入初始状态，栈由此开始生长：

- [src/MetalinkParserStateMachine.cc:L108-L112](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateMachine.cc#L108-L112)：构造时创建 Controller 并把 `initialState_` 压栈。

全部 25 个状态在文件开头以静态成员指针一次性构造，V3 一组、V4 一组：

- [src/MetalinkParserStateMachine.cc:L49-L81](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateMachine.cc#L49-L81)：`initialState_`、`skipTagState_` 与 Metalink 3 的 14 个状态单例（metalink / files / file / size / version / language / os / verification / hash / pieces / pieceHash / signature / resources / url）。
- [src/MetalinkParserStateMachine.cc:L83-L106](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateMachine.cc#L83-L106)：Metalink 4 的 12 个状态单例（多出 `metaurlStateV4_`，没有 files / verification / resources 三个容器状态）。

压栈与弹栈的两个入口：

- [src/MetalinkParserStateMachine.cc:L505-L511](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateMachine.cc#L505-L511)：`beginElement` 只做一件事——把事件委派给栈顶状态；是否压栈、压什么，由状态自己决定。
- [src/MetalinkParserStateMachine.cc:L513-L521](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateMachine.cc#L513-L521)：`endElement` 先让栈顶状态消费 `characters`（元素文本），然后无条件 `pop()`——弹栈是状态机的职责，不是状态的职责。

一套典型的 `setXxxState` 就是单行压栈（共 20 余个，全部同构）：

- [src/MetalinkParserStateMachine.cc:L126-L129](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateMachine.cc#L126-L129)：`setMetalinkState()` 把 V3 根状态压栈。

字符缓冲的门控查询（4.5 节会看到 SAX 层如何使用它）：

- [src/MetalinkParserStateMachine.cc:L523-L526](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateMachine.cc#L523-L526)：`needsCharactersBuffering()` 转问栈顶状态——「当前元素需要累积文本吗？」

错误与结果出口：

- [src/MetalinkParserStateMachine.cc:L528-L542](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateMachine.cc#L528-L542)：`logError` 最多记录 10 条违规（V4 状态会调用它），`getErrorString` 拼成 `Specification violation: ...` 一句话。
- [src/MetalinkParserStateMachine.cc:L544-L547](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateMachine.cc#L544-L547)：`getResult()` 从 Controller 取走装好的 `Metalinker`（entry 容器）。

#### 4.1.4 代码实践

**实践目标**：不下载任何数据，亲眼确认「-M 一个 .meta4 文件 → 解析 → 打印文件清单 → 退出」这条链路存在。

**操作步骤**：

1. 确认已完成 u1-l2 的编译（仓库根目录下存在 `src/aria2c`）。Metalink 解析依赖 XML 后端，configure 输出里应能看到 libxml2 或 expat 被启用（两者默认都会探测，见 u1-l2 的 `ARIA2_ARG_WITHOUT` 规则）。
2. 执行：
   ```bash
   ./src/aria2c -M test/metalink4.xml --show-files
   ```
3. `--show-files` 与 `-M` 组合时走 [src/Context.cc:L262-L265](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L262-L265) 的提前退出分支，调用 `showMetalinkFile`（[src/Context.cc:L101-L109](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L101-L109)），它内部就是 `metalink::parseAndQuery` + 打印。

**需要观察的现象**：终端输出 `example.ext` 的文件清单条目（含长度 786430），随后进程退出，没有任何网络请求。

**预期结果**：解析完全发生在本地，`--show-files` 是本讲所有实践的安全载体——它跑完解析就退出，绝不会真的去 `ftp.example.com` 下载。具体输出格式：**待本地验证**（不同版本控制台格式可能略有差异）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `endElement` 里的 `pop()` 放在状态机而不是放在各状态的 `endElement` 实现里？

**参考答案**：弹栈是状态机的全局约定（对应 4.1.2 的不变量：一个结束标签必弹一次）。若交给几十个状态类各自实现，任何一个状态忘记弹栈或弹两次，栈就与元素嵌套脱钩且难以排查。集中在一处后，状态类只需表达「结束时提交什么数据」，结构性职责留在状态机。这也是模板方法思想：固定骨架（弹栈）在基类流程里，可变步骤（数据处理）留给子类。

**练习 2**：状态对象为什么可以做成全局单例、连解析结束都不销毁？

**参考答案**：`MetalinkParserState` 的接口（[src/MetalinkParserState.h:L48-L66](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserState.h#L48-L66)）没有任何可写字段——所有中间数据都存在 Controller 的事务对象里，状态只携带行为。无状态对象天然线程安全、可共享，做成静态单例（[src/MetalinkParserStateMachine.cc:L49-L106](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateMachine.cc#L49-L106)）省去了每次解析的分配开销。代价是「当前解析到第几个 file」这类信息必须外置在 Controller，这正是事务机制存在的原因之一。

**练习 3**：`reset()`（[src/MetalinkParserStateMachine.cc:L116-L124](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateMachine.cc#L116-L124)）做了哪几件事？谁会调用它？

**参考答案**：三件事——重置 Controller（换一个全新 `Metalinker`）、清空错误列表、把状态栈清空后重新压入 `initialState_`。调用方是 XML 适配层的 `XmlParser::reset()`（libxml2 与 Expat 两版都有），用于把解析器对象复用于新一轮会话；命令行的一次性解析路径（`metalink::parseFile`）则直接新建状态机，不走 reset。

---

### 4.2 ParserState 家族（一）：Initial 版本分派、SkipTag 与 V3 状态链

#### 4.2.1 概念说明

`MetalinkParserState` 基类提供三个钩子（`beginElement` / `endElement` / `needsCharactersBuffering`），默认全部「什么都不做 / 不需要缓冲」。三个家族成员承担骨架角色：

- **InitialMetalinkParserState**：文档的第一枪。只处理根元素，用**命名空间 URI** 判定文档是 Metalink 3、Metalink 4 还是不认识的杂牌。
- **SkipTagMetalinkParserState**：万能垃圾桶。任何不认识的子树都切到它，它的 `beginElement` 继续压 SkipTag，从而整棵子树被无副作用地跳过。
- **V3 状态链**：与 Metalink 3 的元素层级一一对应。

#### 4.2.2 核心流程

版本分派逻辑（伪代码）：

```
Initial.beginElement(localname, nsUri):
    if localname != "metalink" 或无命名空间:  → SkipTag（整份文档作废）
    elif nsUri == "urn:ietf:params:xml:ns:metalink":  → Metalink V4 根状态
    elif nsUri == "http://www.metalinker.org/":      → Metalink V3 根状态
    else:                                     → SkipTag
```

V3 的状态迁移图（元素层级 → 状态压栈）：

```
metalink          → MetalinkMetalinkParserState
 └ files          → FilesMetalinkParserState        （只是容器）
    └ file@name   → FileMetalinkParserState          + newEntryTransaction
       ├ size     → SizeMetalinkParserState          （文本 → 文件长度）
       ├ version  → VersionMetalinkParserState
       ├ language → LanguageMetalinkParserState
       ├ os       → OSMetalinkParserState
       ├ verification → VerificationMetalinkParserState（容器）
       │  ├ hash@type   → HashMetalinkParserState    + newChecksumTransaction
       │  ├ pieces@length@type → PiecesMetalinkParserState + newChunkChecksumTransaction
       │  │   └ hash@piece=序号 → PieceHashMetalinkParserState
       │  └ signature@type@file → SignatureMetalinkParserState + newSignatureTransaction
       └ resources   → ResourcesMetalinkParserState  （容器）
          └ url@type@location@preference@maxconnections
                         → URLMetalinkParserState     + newResourceTransaction
```

注意配对规律：**容器状态只有 `beginElement`**（把子元素分发出去），**叶子文本状态只有 `endElement`**（消费文本并提交事务），这正是 SAX 事件与元素性质的天然对应。

#### 4.2.3 源码精读

版本分派的三岔口：

- [src/MetalinkParserStateImpl.cc:L74-L92](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateImpl.cc#L74-L92)：`InitialMetalinkParserState::beginElement` 先看标签名必须是 `metalink`，再比较两个命名空间常量，命中 V4 或 V3 分别压对应根状态，否则进 SkipTag。两个常量定义在各自的实现文件里：V3 为 `http://www.metalinker.org/`（[src/MetalinkParserStateV3Impl.cc:L47](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateV3Impl.cc#L47)），V4 为 `urn:ietf:params:xml:ns:metalink`（[src/MetalinkParserStateV4Impl.cc:L47](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateV4Impl.cc#L47)）。

SkipTag 的自我延续：

- [src/MetalinkParserStateImpl.cc:L94-L101](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateImpl.cc#L94-L101)：`SkipTagMetalinkParserState::beginElement` 无论遇到什么子元素都再压一个 SkipTag——未知子树的每一层都被吞掉，且 `needsCharactersBuffering` 用基类默认值 `false`，连文本都不缓冲。测试 [test/MetalinkProcessorTest.cc:L47](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/MetalinkProcessorTest.cc#L47) 的 `testXmlPrefixV3` 就验证了带前缀的未知元素不影响解析。

属性查找工具（全家族共用）：

- [src/MetalinkParserStateImpl.cc:L67-L72](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateImpl.cc#L67-L72)：`findAttr(attrs, localname, nsUri)` 在属性表里线性查找；匹配条件是「名字相同，且属性自身无命名空间或命名空间相等」——这容忍了不同 XML 库对默认命名空间属性的表示差异。

V3 链条的几个关键状态：

- [src/MetalinkParserStateV3Impl.cc:L56-L66](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateV3Impl.cc#L56-L66)：V3 根状态只认子元素 `files`，其余进 SkipTag（所以 `<published>` 之类的全局元素不会误入 `files` 之外的路径）。
- [src/MetalinkParserStateV3Impl.cc:L68-L89](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateV3Impl.cc#L68-L89)：`files > file@name` 处开**条目事务**：先压 `FileState`，再取 `name` 属性；名字为空或检出目录穿越（`util::detectDirTraversal`，防 `../` 逃逸）时直接 `return`——注意此时状态已压栈，元素仍会被正常遍历，只是没有开事务，后续所有 `set` 落空，这个 file 条目最终不会被收集。真实攻击样例见 [test/metalink3-dirtraversal.xml:L4](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/metalink3-dirtraversal.xml#L4) 的 `name="../aria2-0.5.2.tar.bz2"`。
- [src/MetalinkParserStateV3Impl.cc:L91-L134](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateV3Impl.cc#L91-L134)：`FileState::beginElement` 是 V3 的元素级路由器：`size` / `version` / `language` / `os` / `verification` / `resources` 各压对应状态；`resources` 还顺带读取 `maxconnections` 属性（解析失败或 ≤0 一律记 -1 表示未指定）。
- [src/MetalinkParserStateV3Impl.cc:L136-L142](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateV3Impl.cc#L136-L142)：`</file>` 时提交条目事务——这是每个下载条目的收官点。
- [src/MetalinkParserStateV3Impl.cc:L144-L155](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateV3Impl.cc#L144-L155)：V3 对坏 `<size>` 的态度是**静默忽略**（解析失败就不设长度），与 V4 的「取消条目并记错误」形成鲜明对比（见 4.3）。
- [src/MetalinkParserStateV3Impl.cc:L183-L251](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateV3Impl.cc#L183-L251)：`verification` 路由三种完整性元素：`hash@type` 开校验和事务；`pieces@length@type` 开分块校验事务（缺任一属性直接 `return`，事务不开）；`signature@type` 开签名事务，还可带 `file` 属性（同样做目录穿越检查）。
- [src/MetalinkParserStateV3Impl.cc:L262-L307](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateV3Impl.cc#L262-L307)：`pieces > hash@piece=序号`——V3 的每个分块哈希带**显式序号**；序号缺失或非法会 `cancelChunkChecksumTransaction()`，即整个分块校验事务作废（不只是丢这一块）。`</pieces>` 时提交（L290-L297），每个 `</hash>` 时填一块（L299-L307）。
- [src/MetalinkParserStateV3Impl.cc:L319-L392](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateV3Impl.cc#L319-L392)：`resources > url` 的属性装配。三处值得注意：(1) `type` 属性**必填**，缺失直接放弃这个 URL；(2) `preference` 语义翻转——Metalink 3 规定 100 最高，而 aria2 内部统一用「1 最高」的 V4 优先级体系，因此 `preference = 101 - preference`（[L343-L360](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateV3Impl.cc#L343-L360) 的注释写明了这层换算）；(3) `</url>` 时填 URL 文本并提交资源事务（L385-L392）。

#### 4.2.4 代码实践

**实践目标**：验证 Initial 状态的「命名空间决定论」——把 v4 样例的命名空间改坏，观察解析结果如何塌缩。

**操作步骤**：

1. 复制样例：`cp test/metalink4.xml /tmp/broken.meta4`。
2. 把第 2 行的 `xmlns="urn:ietf:params:xml:ns:metalink"` 改成 `xmlns="urn:example:fake"`。
3. 运行 `./src/aria2c -M /tmp/broken.meta4 --show-files`。

**需要观察的现象**：文件清单为空（或提示无可下载条目），但进程**不报 XML 解析错误**——文档本身是合法 XML。

**预期结果**：根元素的命名空间不匹配任何已知 URI，Initial 状态把整个根子树交给 SkipTag（[src/MetalinkParserStateImpl.cc:L80-L91](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateImpl.cc#L80-L91) 的两个 else 分支），`Metalinker` 里一个条目都没有；`--show-files` 打印空清单。是否伴随警告输出：**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：一个 v3 文档的 `<file>` 缺少 `name` 属性，解析会发生什么？条目会进结果吗？

**参考答案**：[src/MetalinkParserStateV3Impl.cc:L74-L84](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateV3Impl.cc#L74-L84) 中 `findAttr` 找不到 `name` 时 `itr == attrs.end()`，整段 if 跳过，`newEntryTransaction()` 不会被调用；`FileState` 仍压栈，其子元素照常解析，但 Controller 的 `tEntry_` 为空指针，所有 set 被空检查挡掉；`</file>` 的 `commitEntryTransaction` 同样因空指针直接返回。条目不会进入 `Metalinker`。V3 在这里选择静默丢弃（无 `logError`），V4 则会显式记错误（4.3 对比）。

**练习 2**：为什么 `FilesMetalinkParserState` 这类容器状态也要 `needsCharactersBuffering() = true`（[src/MetalinkParserStateV3Impl.h:L60](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateV3Impl.h#L60)）？

**参考答案**：字符缓冲栈的检查时机是「元素开始之后、字符到来之前」，问的是**新压栈的状态**（见 4.5.3）。容器元素之间的空白、换行也是字符事件；容器状态开缓冲，保证字符栈的 push/pop 与元素栈严格同步，子元素的文本永远落在栈顶自己的格子里。反过来，SkipTag 关闭缓冲，被跳过子树的文本一律丢弃，节省内存。

**练习 3**：v3 的 `<url type="http">` 里 `type` 缺失会怎样？这和「type 写了不认识的值」有何不同？

**参考答案**：缺失时 [src/MetalinkParserStateV3Impl.cc:L330-L335](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateV3Impl.cc#L330-L335) 直接 `return`，`newResourceTransaction` 都没执行，URL 文本最终无处安放，资源被丢弃。写了不认识的值（如 `type="gopher"`）则会开事务、URL 正常收集，但在 Controller 的 `setTypeOfResource`（4.4.3）里被标为 `TYPE_NOT_SUPPORTED`，资源保留但下载阶段不会用它。一个是「结构性缺失、直接扔」，一个是「语义不识别、保留但降级」。

---

### 4.3 ParserState 家族（二）：V4 状态集与 V3/V4 差异

#### 4.3.1 概念说明

Metalink 4（RFC 5854）对格式做了扁平化改造，aria2 为它另起一套状态而不是在 V3 上打补丁——两套规范元素树不同，硬塞进一套状态类只会让分支爆炸。V4 状态集的共同风格是**显式报错**：凡违反规范的属性一律 `psm->logError(...)` 记录（部分场景还会取消整个条目），解析结束后由上层把这些违规汇总成异常（见 4.4.3）。

#### 4.3.2 核心流程

V4 的状态迁移图：

```
metalink              → MetalinkMetalinkParserStateV4
 └ file@name          → FileMetalinkParserStateV4        + newEntryTransaction
    ├ size            → SizeMetalinkParserStateV4        （坏值 → 取消条目 + logError）
    ├ version         → VersionMetalinkParserStateV4
    ├ language        → LanguageMetalinkParserStateV4
    ├ os              → OSMetalinkParserStateV4
    ├ hash@type       → HashMetalinkParserStateV4        + newChecksumTransaction
    ├ pieces@length@type → PiecesMetalinkParserStateV4   + newChunkChecksumTransactionV4
    │   └ hash        → PieceHashMetalinkParserStateV4   （按文档顺序追加）
    ├ url@location@priority → URLMetalinkParserStateV4   + newResourceTransaction
    ├ metaurl@mediatype@priority@name → MetaurlMetalinkParserStateV4 + newMetaurlTransaction
    └ signature@mediatype → SignatureMetalinkParserStateV4 + newSignatureTransaction
```

V3 与 V4 的关键差异汇总：

| 维度 | Metalink 3 | Metalink 4 |
| --- | --- | --- |
| 命名空间 URI | `http://www.metalinker.org/` | `urn:ietf:params:xml:ns:metalink` |
| 条目容器 | `files > file`（两级） | `file` 直接挂在根下（一级） |
| 完整性容器 | `verification` 包住 hash/pieces/signature | 无容器，三者平级挂在 `file` 下 |
| URL 容器 | `resources > url` | `url` 平级 |
| URL 类型 | `url@type` 必填 | 无 type，从 URL scheme 推断（4.4.3） |
| 优先级 | `@preference`，100 最高，内部换算 `101-preference` | `@priority`，1 最高，直接用 |
| 分块哈希序号 | 每个 `hash@piece=序号` 显式给出，提交时按序号排序 | 无序号，按**文档出现顺序**追加 |
| 种子资源 | `url@type="bittorrent"`（或 torrent），事后转成 metaurl | 专门的 `metaurl@mediatype="torrent"` 元素 |
| 坏 `<size>` | 静默忽略 | 取消整个条目 + `logError("Bad size")` |
| 违规处理 | 多数静默跳过 | `logError` 记录（上限 10 条），汇总成异常 |

#### 4.3.3 源码精读

- [src/MetalinkParserStateV4Impl.cc:L56-L78](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateV4Impl.cc#L56-L78)：V4 根状态直接认 `file`。`name` 缺失或为空记 `Missing file@name`，检出目录穿越记 `Bad file@name`——与 V3 的静默丢弃不同，V4 把违规写进错误列表。
- [src/MetalinkParserStateV4Impl.cc:L80-L228](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateV4Impl.cc#L80-L228)：`FileStateV4::beginElement` 是 V4 的路由器，将近 150 行的 if-else 链，按 `size`/`version`/`language`/`os`/`metaurl`/`url`/`hash`/`pieces`/`signature` 分发。三段典型：
  - `metaurl`（[L101-L145](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateV4Impl.cc#L101-L145)）：`mediatype` 必填（缺失记 `Missing metaurl@mediatype`），`priority` 可选但给了就必须在 `[1, getLowestPriority()=999999]` 内，`name` 可选且做目录穿越检查，全部通过才 `newMetaurlTransaction`。
  - `url`（[L146-L176](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateV4Impl.cc#L146-L176)）：只有 `location` 与 `priority` 两个属性，没有 `type`——类型交给 Controller 从 URL 的 scheme 推断。
  - `pieces`（[L187-L214](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateV4Impl.cc#L187-L214)）：`length` 与 `type` 都必填，坏值分别记 `Bad pieces@length` / `Missing pieces@type` 后放弃事务。
- [src/MetalinkParserStateV4Impl.cc:L239-L254](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateV4Impl.cc#L239-L254)：`SizeStateV4::endElement` 是 V4 严格作风的标本：`<size>` 不是合法非负整数时 `cancelEntryTransaction()`（整个条目作废）+ `logError("Bad size")`。对照 V3 同一位置（4.2.3）只静默忽略。测试 [test/MetalinkProcessorTest.cc:L59-L60](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/MetalinkProcessorTest.cc#L59-L60) 的 `testBadSize` / `testBadSizeV4` 分别锁定了这两种行为。
- [src/MetalinkParserStateV4Impl.cc:L292-L320](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateV4Impl.cc#L292-L320)：V4 的 `pieces > hash` **不带 piece 序号属性**，`beginElement` 只做「是 hash 就压 PieceHashState」，`endElement` 把文本直接 `addHashOfChunkChecksumV4` 追加进临时数组——文档里第几个 `<hash>` 就是第几块的哈希，乱序写文档等于声明错误的分块校验值。

#### 4.3.4 代码实践

**实践目标**：用同一份坏 `<size>` 文档，实证 V3「静默忽略」与 V4「取消条目 + 报规范错误」的差异。

**操作步骤**：

1. 写两个小文件（以下为**示例代码**，基于 v3/v4 元素层级手工构造）：

   `/tmp/bad3.metalink`（v3，命名空间必须是 `http://www.metalinker.org/`）：
   ```xml
   <?xml version="1.0" encoding="utf-8"?>
   <metalink version="3.0" xmlns="http://www.metalinker.org/">
     <files>
       <file name="demo.bin">
         <size>-1</size>
         <resources>
           <url type="http">http://example.com/demo.bin</url>
         </resources>
       </file>
     </files>
   </metalink>
   ```

   `/tmp/bad4.meta4`（v4）：把 `metalink4.xml` 复制一份，仅将 `<size>786430</size>` 改成 `<size>abc</size>`。

2. 分别运行：
   ```bash
   ./src/aria2c -M /tmp/bad3.metalink --show-files
   ./src/aria2c -M /tmp/bad4.meta4 --show-files
   ```

**需要观察的现象**：v3 版正常打印 `demo.bin`（长度未知）；v4 版报错退出，错误信息以 `Specification violation: Bad size` 开头。

**预期结果**：v3 路径走 [src/MetalinkParserStateV3Impl.cc:L144-L155](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateV3Impl.cc#L144-L155)，解析失败只意味着「长度不设置」，条目保留；v4 路径走 [src/MetalinkParserStateV4Impl.cc:L239-L254](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateV4Impl.cc#L239-L254)，条目被取消且 `logError` 的内容经 [src/metalink_helper.cc:L120-L122](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/metalink_helper.cc#L120-L122) 变成 `DL_ABORT_EX2` 异常（错误码 `METALINK_PARSE_ERROR = 20`，[src/error_code.h:L66](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/error_code.h#L66)）。两份输出的确切措辞：**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：同一份 v4 文档里先出现 `<pieces type="sha-1">` 再出现 `<pieces type="sha-256">`，最终条目用哪套分块哈希？

**参考答案**：每次 `<pieces>` 开一个新事务，`</pieces>` 提交一次；提交处（[src/MetalinkParserController.cc:L370-L382](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserController.cc#L370-L382)）用 `MessageDigest::isStronger` 比较——只有新的比已有得更强才覆盖。sha-256 强于 sha-1，所以最终保留 sha-256 那组。[test/metalink4.xml:L12-L21](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/metalink4.xml#L12-L21) 正是两组 pieces 并存，[test/MetalinkProcessorTest.cc:L89-L106](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/MetalinkProcessorTest.cc#L89-L106) 断言构建支持 sha-256 时用 sha-256 组。整文件 `<hash>` 同理（md5 与 sha-1 并存时留 sha-1）。

**练习 2**：为什么 V4 不复用 V3 的 `files`/`verification`/`resources` 三个容器状态，哪怕只是空转？

**参考答案**：两套格式的合法元素树不同。V4 文档里若出现 `<files>` 属于未知元素，必须走 SkipTag 丢弃；如果复用 V3 容器状态，`<files>` 会被当成合法路径继续找 `<file>`，等于悄悄放宽了规范。分开两套状态集，让「哪些元素在哪个版本合法」由类型系统表达，版本间不会互相污染——这正是 Initial 按 URI 一次分派、之后两套状态永不混用的原因。

**练习 3**：V4 的 `<metaurl priority="0">` 会被如何处理？条目本身还保得住吗？

**参考答案**：[src/MetalinkParserStateV4Impl.cc:L120-L131](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserStateV4Impl.cc#L120-L131)：priority 解析成功但小于 1，记 `metaurl@priority is out of range` 后 `return`——此时 `MetaurlState` 已压栈但 `newMetaurlTransaction` 未执行，`</metaurl>` 的提交因空指针落空。**只丢这一个 metaurl**，条目本身（及其他 url/hash）不受影响，这与 `<size>` 坏值「取消整个条目」的力度不同：size 是文件的根本属性，metaurl 只是候选资源之一。

---

### 4.4 MetalinkParserController：事务式收集与数据规范化

#### 4.4.1 概念说明

状态机只负责「听懂 XML」，`MetalinkParserController` 负责「听懂之后把数据放好」。它做四类事：

1. **事务管理**：临时对象 `tEntry_` / `tResource_` / `tMetaurl_` / `tChecksum_` / `tChunkChecksumV4_` / `tChunkChecksum_` / `tSignature_`（声明见 [src/MetalinkParserController.h:L56-L80](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserController.h#L56-L80)），配合空指针检查实现乱序容错。
2. **安全处理**：文件名 `escapePath` 转义 + 目录穿越检查（状态层）。
3. **数据规范化**：相对 URL 按基准 URI 补全、URL scheme → 资源类型推断、哈希算法名归一化与强度择优、v3 优先级换算（状态层）后的落库。
4. **条件编译分叉**：BitTorrent 相关路径包在 `#ifdef ENABLE_BITTORRENT` 里（呼应 u1-l2：configure 决定特性集）。

#### 4.4.2 核心流程

一个 `file` 条目的完整事务生命周期：

```
<file name>            newEntryTransaction()      → tEntry_ 诞生
  <hash type>          newChecksumTransaction()   → tChecksum_ 诞生
  </hash>              setHashOfChecksum + commit → tChecksum_ 并入 tEntry_（择强保留）
  <url ...>            newResourceTransaction()   → tResource_ 诞生
  </url>               setURLOfResource + commit  → tResource_ 并入 tEntry_（BT 型转 metaurl）
  <pieces>             newChunkChecksumTransaction(V4)() → 分块事务 + 临时哈希数组
  <hash>..</hash>*     逐块追加（V4 按文档序 / V3 带 (序号, 哈希) 对）
  </pieces>            commit                     → 排序（仅 V3）后并入 tEntry_（择强保留）
</file>                commitEntryTransaction()   → 级联提交全部子事务 + metalinker_->addEntry
```

`commitEntryTransaction` 的级联提交是**防御性收尾**：合法 XML 中每个子元素都会在自己的 `endElement` 提交，级联兜住的是极端残缺文档下的漏网事务。

#### 4.4.3 源码精读

条目事务与文件名安全：

- [src/MetalinkParserController.cc:L75-L83](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserController.cc#L75-L83)：`newEntryTransaction` 新建 `MetalinkEntry` 并清空全部下层事务——条目边界即事务边界。
- [src/MetalinkParserController.cc:L85-L96](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserController.cc#L85-L96)：文件名经 `util::escapePath` 处理后写入 `FileEntry`——状态层挡了 `../`，这里再做一层字符转义，双保险。
- [src/MetalinkParserController.cc:L142-L165](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserController.cc#L142-L165)：`commitEntryTransaction` 依次级联提交六类子事务，最后 `metalinker_->addEntry`；`cancelEntryTransaction` 对称地全部取消并置空 `tEntry_`。

URL 规范化（资源事务的核心）：

- [src/MetalinkParserController.cc:L167-L192](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserController.cc#L167-L192)：`setURLOfResource` 三步走——`uri::joinUri(baseUri_, url)` 把相对地址补成绝对地址（`--metalink-base-uri` 或 HTTP 场景下的文档地址就是 `baseUri_`）；`uri_split` 验证能否解析成合法 URI；若资源类型仍是 `TYPE_UNKNOWN`（V4 场景），从 URL 的 **scheme 字段**反推类型。解析失败的 URL 原样保留（不在此处丢弃），后续下载阶段自然处理不了它。
- [src/MetalinkParserController.cc:L194-L215](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserController.cc#L194-L215)：`setTypeOfResource` 的字符串到枚举映射：`ftp`/`sftp`→FTP、`http`→HTTP、`https`→HTTPS、`bittorrent`/`torrent`→BITTORRENT（注释说明 `torrent` 是 V4 写法），其余一律 `TYPE_NOT_SUPPORTED`。
- [src/MetalinkParserController.cc:L241-L261](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserController.cc#L241-L261)：`commitResourceTransaction` 的跨版本归一——V3 用 `url@type="bittorrent"` 表达种子资源，提交时若类型是 `TYPE_BITTORRENT`（且启用 BT），把它**改装**成 `MetalinkMetaurl`（mediatype 固定 `torrent`）塞进 `metaurls`，与 V4 的 `<metaurl>` 殊途同归；普通资源才进 `resources`。下载侧因此只需面对统一的数据模型。

校验和与分块校验：

- [src/MetalinkParserController.cc:L276-L301](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserController.cc#L276-L301)：`setTypeOfChecksum` 用 `MessageDigest::getCanonicalHashType` 归一化算法名（如 `sha-1`/`SHA1` → 统一写法），算法不受支持就取消事务；`setHashOfChecksum` 再用 `isValidHash` 校验**十六进制长度与算法匹配**（sha-1 必须 40 个 hex 字符），合法则 `fromHex` 转二进制存入。这两道检查把「算法不认识」「哈希值长度不对」的脏数据挡在解析期。
- [src/MetalinkParserController.cc:L303-L314](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserController.cc#L303-L314)：`commitChecksumTransaction` 用 `MessageDigest::isStronger` 择强——多个 `<hash>` 并存时只保留最强算法的。
- [src/MetalinkParserController.cc:L321-L382](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserController.cc#L321-L382)：V4 分块事务：`tempChunkChecksumsV4_` 按文档顺序逐块 `push_back`（[L357-L368](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserController.cc#L357-L368) 每块都做合法性检查，坏一块取消整组），提交时同样择强覆盖。
- [src/MetalinkParserController.cc:L389-L486](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserController.cc#L389-L486)：V3 分块事务多了**序号维度**：`createNewHashOfChunkChecksum(order)` 记序号 → `setMessageDigestOfChunkChecksum(md)` 存哈希 → `addHashOfChunkChecksum()` 组成 `(序号, 哈希)` 对入列（[L439-L466](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserController.cc#L439-L466) 的三步曲），提交时先 `std::sort` 按序号排好，再抽出纯哈希序列（[L468-L486](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserController.cc#L468-L486)）。这就是 v3 允许乱序写 `<hash piece="8">` 而 v4 不允许的底层原因。

metaurl（种子/magnet 资源）：

- [src/MetalinkParserController.cc:L546-L566](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserController.cc#L546-L566)：`setURLOfMetaurl` 对 magnet 链接（`magnet::parse` 能解析则）原样收下——magnet 本身就是绝对标识；普通 URL 走与资源相同的 joinUri + 校验流程。
- [src/MetalinkParserController.cc:L592-L603](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserController.cc#L592-L603)：`commitMetaurlTransaction` 只收 `mediatype == "torrent"` 的 metaurl，其他 mediatype（未来的图片、视频等）直接丢弃——当前 aria2 只把 metaurl 当种子分发渠道。

解析结果的出口（Controller 与上层 的衔接）：

- [src/metalink_helper.cc:L111-L124](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/metalink_helper.cc#L111-L124)：`metalink::parseFile` 门面——建状态机、设基准 URI、跑 `xml::parseFile`；XML 不合法或 `psm.getErrors()` 非空都抛 `METALINK_PARSE_ERROR` 异常。也就是说 **V4 状态记下的每一条 `logError` 最终都会让整个 metalink 文件被拒绝**，「记错误」不是警告而是判决。
- [src/metalink_helper.cc:L56-L62](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/metalink_helper.cc#L56-L62) 与 [src/Metalinker.cc:L47-L66](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalinker.cc#L47-L66)：`parseAndQuery` 在解析后用 `--metalink-version` / `--metalink-language` / `--metalink-os` **过滤条目**（匹配条目的 version/language/os，空选项不过滤）——再次印证 2.1 节对这些选项语义的澄清。
- [src/Metalink2RequestGroup.cc:L118-L133](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc#L118-L133)：过滤后的条目交给 `Metalink2RequestGroup::createRequestGroup` 变成 `RequestGroup`——那是 u8-l2 的故事。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：在 Controller 的资源/校验和提交点加日志，打印解析出的每个候选资源与校验和，并对照状态机源码解释元素嵌套如何映射状态切换。

**操作步骤**：

1. 编辑 [src/MetalinkParserController.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserController.cc)，文件顶部补 `#include <cstdio>`（示例代码）：

   ```cpp
   // commitResourceTransaction() 中，if (!tResource_) { return; } 之后插入：
   fprintf(stderr, "[ml] resource: url=%s priority=%d location=%s\n",
           tResource_->url.c_str(), tResource_->priority,
           tResource_->location.c_str());

   // commitChecksumTransaction() 中，if (!tChecksum_) { return; } 之后插入：
   fprintf(stderr, "[ml] checksum: type=%s digest=%s\n",
           tChecksum_->getHashType().c_str(),
           util::toHex(tChecksum_->getDigest()).c_str());

   // commitChunkChecksumTransactionV4() 中，if (!tChunkChecksumV4_) { return; } 之后插入：
   fprintf(stderr, "[ml] chunkchecksumV4: type=%s pieceLength=%lu pieces=%zu\n",
           tChunkChecksumV4_->getHashType().c_str(),
           (unsigned long)tChunkChecksumV4_->getPieceLength(),
           tempChunkChecksumsV4_.size());

   // commitMetaurlTransaction() 中，if (!tMetaurl_) { return; } 之后插入：
   fprintf(stderr, "[ml] metaurl: url=%s mediatype=%s priority=%d\n",
           tMetaurl_->url.c_str(), tMetaurl_->mediatype.c_str(),
           tMetaurl_->priority);
   ```

   说明：`util::toHex` 所需的 `util.h` 该文件已包含（[src/MetalinkParserController.cc:L48](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserController.cc#L48)）。也可以改用 `A2_LOG_INFO`（需 `#include "Logger.h"`，并用 `-l <日志文件> --log-level=info` 查看），fprintf 直写 stderr 最省事。
2. 重新编译：`make -j$(nproc)`（只改了一个 .cc，增量编译很快）。
3. 运行：
   ```bash
   ./src/aria2c -M test/metalink4.xml --show-files
   ```

**需要观察的现象**：stderr 上依次出现（以 [test/metalink4.xml](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/metalink4.xml) 为输入）：

```
[ml] checksum: type=sha-1 digest=0beec7b5...        ← 第 11 行 </hash>
[ml] chunkchecksumV4: type=sha-1 pieceLength=262144 pieces=3   ← 第 16 行 </pieces>
[ml] chunkchecksumV4: type=sha-256 pieceLength=262144 pieces=3 ← 第 21 行 </pieces>（覆盖前一组）
[ml] resource: url=ftp://ftp.example.com/example.ext priority=1 location=de   ← 第 22 行 </url>
[ml] resource: url=http://example.com/example.ext priority=1 location=fr      ← 第 23 行 </url>
[ml] metaurl: url=http://example.com/example.ext.torrent mediatype=torrent priority=2  ← 第 24 行 </metaurl>
```

**预期结果**：日志行数与顺序**严格等于 XML 中各元素 `endElement` 的出现顺序**——因为每个提交点都挂在叶子状态的 `endElement` 上，这就是「元素嵌套 → 状态切换 → 事务提交」三元对应的直接证据。第 10 行的 md5 为何没有日志：它的哈希值只有 31 个 hex 字符（合法 md5 应为 32），在 `setHashOfChecksum` 的 `isValidHash` 长度检查处即被取消（[src/MetalinkParserController.cc:L290-L301](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserController.cc#L290-L301)），到 `</hash>` 提交时 `tChecksum_` 已是空指针，日志自然不触发。如果你把两个 `<hash>` 都填成合法值，就会看到两条 checksum 日志——因为插入点在择强判断（`isStronger`）之前，每次提交都会打印。本实践改动仅用于本地观察，验证后请用 `git checkout -- src/MetalinkParserController.cc` 还原，保持源码树干净。上述输出内容：**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`setURLOfResource` 里 `uri_split` 失败时为什么把原始字符串照样存进 `tResource_->url`，而不是丢弃资源？

**参考答案**：见 [src/MetalinkParserController.cc:L189-L191](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserController.cc#L189-L191) 的 else 分支。解析器的职责是**如实转述文档**，URL 是否可用是下载阶段的判断；此处丢弃会吞掉证据，用户在错误报告里看不到坏 URL 长什么样。而且 `uri_split` 的判定未必覆盖所有现实世界 URL 方言，保留原文把最终裁决权交给下载链路。

**练习 2**：V3 的 `tempChunkChecksums_` 存 `(序号, 十六进制字符串)` 对，为什么提交时排序后才转成纯哈希数组，而不是边解析边按下标写入最终数组？

**参考答案**：SAX 是流式的，读到第 N 块时不知道后面还有没有序号更小的块，也无法预知总数（数组大小 = 最大序号+1 未知）。先收集对、提交时 `std::sort` 再投影（[src/MetalinkParserController.cc:L468-L486](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserController.cc#L468-L486)），一步解决乱序与紧凑化两个问题。代价是 O(n log n) 排序和一份临时内存，对分块哈希这种千级规模的数据完全无感。

**练习 3**：如果编译时禁用了 BitTorrent（`--disable-bittorrent`），[test/metalink4.xml:L24](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/metalink4.xml#L24) 的 `<metaurl>` 会怎样？

**参考答案**：`commitMetaurlTransaction`（[src/MetalinkParserController.cc:L592-L603](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserController.cc#L592-L603)）里保留逻辑整段包在 `#ifdef ENABLE_BITTORRENT` 中——禁用 BT 时函数只剩 `tMetaurl_.reset()`，metaurl 被静默丢弃。V3 的 `url@type="bittorrent"` 也一样：`commitResourceTransaction` 的类型判断分支在同一个宏下，禁用 BT 时种子型 url 走 else 分支进 `resources`（[L254-L259](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkParserController.cc#L254-L259)），标记为不受支持类型而闲置。[test/MetalinkProcessorTest.cc:L136-L145](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/MetalinkProcessorTest.cc#L136-L145) 用宏分叉断言了这两种结果。

---

### 4.5 XmlParser 后端适配：libxml2 与 Expat 的统一

#### 4.5.1 概念说明

aria2 支持两个 XML 库：libxml2 与 Expat（configure 里 libxml2 优先，u1-l2 讲过这条优先级）。Metalink 解析代码（状态机、Controller）**一行都不能依赖具体库**，靠的是两层抽象：

1. [src/ParserStateMachine.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ParserStateMachine.h) 定义纯虚接口 `ParserStateMachine`——「能吃 beginElement/endElement 事件的东西」。Metalink 状态机实现它；u8-l4 将遇到的 XML-RPC 解析状态机同样实现它（一套 SAX 基建服务两个子系统）。
2. `xml::XmlParser` 在两个后端文件里**同名不同实现**：`Xml2XmlParser`（libxml2）与 `ExpatXmlParser` 把各自库的回调翻译成统一签名后转发给 `psm`。

#### 4.5.2 核心流程

驱动模型（两后端一致）：

```
xml::parseFile(filename, psm)          # XmlParser.cc
  循环 read(fd, 4KB):
      XmlParser::parseUpdate(chunk)    # 喂给后端推送式解析器
          └─ 后端 SAX 回调:
             mlStartElement  → 组装 XmlAttr 向量 → psm->beginElement(...)
                              → 若 psm->needsCharactersBuffering(): 字符栈 push ""
             mlCharacters    → 若需要缓冲: 字符栈 front 追加文本
             mlEndElement    → 若需要缓冲: 取出栈顶文本并 pop
                              → psm->endElement(..., characters)
  读完: XmlParser::parseFinal(...)     # 通知后端文档结束
```

`SessionData::charactersStack`（`std::deque<std::string>`）是关键中间件：SAX 的字符回调**不知道自己属于哪个元素**，必须靠「元素开始时按需压栈、字符到达时写栈顶、元素结束时弹栈交给状态机」的栈式缓冲，把无上下文的字符流变成带归属的 `characters` 参数。是否缓冲由状态机说了算——跳过的子树一个字节都不存。

#### 4.5.3 源码精读

抽象接口与后端选择：

- [src/ParserStateMachine.h:L47-L65](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ParserStateMachine.h#L47-L65)：五个纯虚函数构成 SAX 世界里的「aria2 标准」：`needsCharactersBuffering` / `finished` / `beginElement` / `endElement` / `reset`。
- [src/XmlParser.h:L63-L67](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/XmlParser.h#L63-L67)：编译期二选一——`HAVE_LIBXML2` 时 `XmlParser` 实为 `Xml2XmlParser`，否则（`HAVE_LIBEXPAT`）实为 `ExpatXmlParser`。上层代码因此可以统一写 `xml::XmlParser ps(&psm);`。
- [src/XmlParser.h:L50-L57](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/XmlParser.h#L50-L57)：错误码（`ERR_XML_PARSE`/`ERR_RESET`）与 `SessionData`（字符栈 + 状态机指针）。

统一驱动循环：

- [src/XmlParser.cc:L46-L78](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/XmlParser.cc#L46-L78)：`xml::parseFile` 打开文件（支持 `-` 即 DEV_STDIN 从标准输入读），4KB 一块地 `read` → `parseUpdate`，读完后 `parseFinal` 收尾。任何一步返回负值即整体失败。这个循环刻意与后端无关，`metalink_helper::parseBinaryStream`（[src/metalink_helper.cc:L126-L156](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/metalink_helper.cc#L126-L156)）把它复制了一遍，只是数据源换成 `BinaryStream::readData`——这服务于 RPC `aria2.addMetalink`（内存中的 metalink 文本）与 HTTP Link 头场景，呼应 u3-l3 讲过的 ByteArrayDiskWriter。

libxml2 后端（SAX2）：

- [src/Xml2XmlParser.cc:L51-L79](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Xml2XmlParser.cc#L51-L79)：`mlStartElement` 把 libxml2 SAX2 的属性数组翻译成 `std::vector<XmlAttr>`。注意 libxml2 的属性是**五元组**（localname / prefix / nsUri / value 起点指针 / value 终点指针），值不是 NUL 结尾的，所以 `XmlAttr` 里存的是 `value + valueLength`（[src/XmlAttr.h:L44-L50](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/XmlAttr.h#L44-L50)）。调用 `psm->beginElement` **之后**再问 `needsCharactersBuffering()`——此时栈顶已是新元素的状态，问的正是「新元素要不要缓冲」，顺序不能反。
- [src/Xml2XmlParser.cc:L82-L105](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Xml2XmlParser.cc#L82-L105)：`mlEndElement` 先（在弹状态栈之前）取走字符栈顶，再把文本交给 `psm->endElement`；`mlCharacters` 只在需要缓冲时向栈顶追加。
- [src/Xml2XmlParser.cc:L109-L152](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Xml2XmlParser.cc#L109-L152)：手工填的 `xmlSAXHandler` 函数表——只注册 characters / startElementNs / endElementNs 三个槽位，其余全空；构造函数用 `xmlCreatePushParserCtxt` 建推送式解析上下文（对应 4KB 分块喂入模式）。
- [src/Xml2XmlParser.cc:L156-L168](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Xml2XmlParser.cc#L156-L168)：`parseUpdate` 包装 `xmlParseChunk`，失败即缓存 `lastError_` 且后续喂入短路返回——错误只报第一次。

Expat 后端：

- [src/ExpatXmlParser.cc:L51-L65](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ExpatXmlParser.cc#L51-L65)：Expat 的元素回调不带独立命名空间参数，构造解析器时传分隔符 `'\t'`（[L143](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ExpatXmlParser.cc#L143)），让名字变成 `nsUri\tlocalname` 形式，`splitNsName` 负责拆开（命名空间串要手工拷贝，因为回调返回后原缓冲即失效，用完 `delete[]`）。
- [src/ExpatXmlParser.cc:L69-L100](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ExpatXmlParser.cc#L69-L100)：Expat 的属性是 NUL 结尾的 `名字/值` 交替数组，逐对拆出并 `strlen` 出长度；同样在 `beginElement` 之后按需压字符栈。
- [src/ExpatXmlParser.cc:L151-L163](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ExpatXmlParser.cc#L151-L163)：`parseUpdate` 包装 `XML_Parse`，与 libxml2 版逐行同构——这就是抽象层的意义：差异全部 confinement 在回调翻译层。

#### 4.5.4 代码实践

**实践目标**：确认你本机构建用的是哪个 XML 后端，并从源码层面验证 `ParserStateMachine` 接口的复用性。

**操作步骤**：

1. 查看构建产物里的宏：`grep -E 'HAVE_LIBXML2|HAVE_LIBEXPAT' config.h`（configure 之后 config.h 在仓库根目录）。
2. 全局搜索接口的实现者：`grep -rn 'public ParserStateMachine' src/`。
3. 对照 `aria2c --version` 的 Features 行（它不会直接列出 XML 后端，但能印证 u1-l2 讲过的「特性随编译而变」）。

**需要观察的现象**：步骤 1 恰好只有一个宏为 `1`（两个都为 0 或都有值都属于异常，值得回到 configure 输出排查）；步骤 2 找到不止一个实现类。

**预期结果**：`MetalinkParserStateMachine` 与 XML-RPC 的解析状态机（u8-l4 主角）都实现 `ParserStateMachine`——一套 SAX 适配层（XmlParser/SessionData/字符栈）被两个协议解析器共享，这是本模块抽象回报的最直接证据。若你的构建 `--without-libxml2`，则走 Expat 路径，`splitNsName` 生效。具体 grep 输出：**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`needsCharactersBuffering` 的查询时机在 `beginElement` 之后、`mlCharacters` 之前，如果把查询挪到 `beginElement` 之前会怎样？

**参考答案**：查到的将是**父元素**状态的意愿。例如 `<size>` 的文本：若提前查询，栈顶还是 `FileState`（恰好也要缓冲），结果碰巧对；但遇到「父不要缓冲、子要」的组合（如 V4 根状态下的 `<file>`，根状态不缓冲而 FileState 要），字符栈会少压一层，后续 `endElement` 弹栈时栈顶错位，文本张冠李戴甚至断言崩溃。libxml2 版 [src/Xml2XmlParser.cc:L73-L78](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Xml2XmlParser.cc#L73-L78) 把查询放在转发 `beginElement` 之后，正是为了问「刚压栈的新状态」。

**练习 2**：为什么字符栈用 `deque` 且 `push_front`/`front`，而不是 `vector` + `back`？

**参考答案**：两者都能表达栈（deque 从头进出、vector 从尾进出，语义等价）。选 deque 头部操作更多是风格选择：`push_front`/`front`/`pop_front` 语义上与「栈顶」的直觉一致。真正不可用的是「按元素随机访问」——任何越层的取值都会破坏 SAX 的嵌套纪律，接口上只暴露栈顶操作本身就是一种约束（见 [src/XmlParser.h:L52-L57](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/XmlParser.h#L52-L57)，`SessionData` 只提供这三个动作加 reset）。

**练习 3**：`xml::parseFile` 为什么每次只读 4KB，而不是一次把文件整个读进内存？

**参考答案**：SAX 的价值就是**常数内存**流式处理（[src/XmlParser.cc:L62-L77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/XmlParser.cc#L62-L77) 的 4KB 缓冲循环）。Metalink 文件通常只有几 KB，但同一套代码也服务于 RPC 传来的任意大小数据（`parseBinaryStream` 同构循环）；一次性读入会让内存上界等于文档大小，违背选择 SAX 的初衷。4KB 是经验值：足够小（内存可忽略）、足够大（系统调用次数合理）。

## 5. 综合实践

把本讲四条线（状态栈、版本分派、事务收集、双后端）串成一个任务。

**任务**：自制一份「考点齐全」的 Metalink 4 文件，用加日志的 aria2 解析它，并手工推演状态栈演化。

**第 1 步 — 准备输入**。以下为**示例代码**（覆盖 V4 全部 8 种 file 子元素，外加两处违规：一个未知元素、一个越界 priority）：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<metalink xmlns="urn:ietf:params:xml:ns:metalink">
  <generator>Example Generator 1.0</generator>
  <file name="suite.tar.gz">
    <size>1048576</size>
    <version>2.1</version>
    <language>zh-CN</language>
    <os>Linux-x86_64</os>
    <hash type="sha-256">a1b2...(填 64 个 hex 字符)</hash>
    <pieces length="524288" type="sha-256">
      <hash>b1c2...(64 hex)</hash>
      <hash>c1d2...(64 hex)</hash>
    </pieces>
    <url location="cn" priority="1">http://mirror1.example.com/suite.tar.gz</url>
    <url location="jp" priority="2">http://mirror2.example.com/suite.tar.gz</url>
    <url location="us" priority="999999999">http://bad.example.com/suite.tar.gz</url>
    <metaurl mediatype="torrent" priority="3">magnet:?xt=urn:btih:0000000000000000000000000000000000000000</metaurl>
    <signature mediatype="application/pgp-signature">a signature</signature>
  </file>
</metalink>
```

保存为 `/tmp/suite.meta4`（哈希值请用 `sha256sum` 对任意 1MiB 文件生成真实值，或随手填 64 个 hex 字符）。

**第 2 步 — 预判**。先笔答三个问题再运行：

1. `<generator>` 会让什么状态压栈？（答：V4 根状态的 `beginElement` 只认 `file`，未知元素压 SkipTag。）
2. 第三个 `<url>` 的 priority=999999999 会怎样？（答：超过 `getLowestPriority()=999999`，记 `url@priority is out of range`，该 url 丢弃；**整个文件会因 logError 非空而被拒绝**。）
3. magnet 形式的 metaurl 走哪条路径？（答：`setURLOfMetaurl` 中 `magnet::parse` 成功后原样保留。）

**第 3 步 — 运行验证**。保留 4.4.4 的四处日志，执行：

```bash
./src/aria2c -M /tmp/suite.meta4 --show-files
```

预期看到 `url@priority is out of range` 导致的 `Specification violation` 异常退出；把越界 priority 改成合法值（或删掉该 url）后重跑，应依次看到 checksum、chunkchecksumV4（pieces=2）、两条 resource、一条 metaurl 日志。具体输出：**待本地验证**。

**第 4 步 — 画状态栈表**。对照 4.1 的不变量，手工填写下表（每行一个 SAX 事件，栈写状态简称），并用日志时间戳核对「每个提交点都发生在对应结束标签那一行」：

| XML 事件 | 压/弹 | 解析后状态栈（底→顶） | 附带动作 |
| --- | --- | --- | --- |
| `<metalink>` | 压 | Initial, MetalinkV4 | — |
| `<generator>` | 压 | Initial, MetalinkV4, Skip | — |
| `</generator>` | 弹 | Initial, MetalinkV4 | — |
| `<file name>` | 压 | Initial, MetalinkV4, FileV4 | newEntryTransaction |
| `<size>` | 压 | ..., FileV4, SizeV4 | — |
| `</size>` | 弹 | ..., FileV4 | setFileLengthOfEntry |
| ...（请补全至 `</metalink>`） | | | |

**第 5 步 — 收尾**。`git checkout -- src/MetalinkParserController.cc` 还原日志改动（若工作区还有其他未提交修改，建议整个实践在专用分支上进行）。

完成本实践后，你应该能不查源码说出：任何一个 Metalink 元素到来时压什么状态、哪个事务被开/提交、数据最终落在 `MetalinkEntry` 的哪个字段。

## 6. 本讲小结

- Metalink 解析采用**推式 SAX + 显式状态栈**架构：栈深度镜像元素嵌套深度，每个开始标签恰好压栈一次（具体状态或 SkipTag）、每个结束标签恰好弹栈一次，这一不变量由 `MetalinkParserStateMachine::beginElement/endElement` 集中保证。
- **版本分派发生在且只发生在根元素**：`InitialMetalinkParserState` 比较命名空间 URI（v3 `http://www.metalinker.org/` vs v4 `urn:ietf:params:xml:ns:metalink`），之后两套状态集永不混用；未知子树交给自我延续的 `SkipTag` 无副作用跳过。
- **V3 与 V4 是两套平行状态**：v4 扁平化（无 files/verification/resources 容器）、优先级语义相反（v3 preference 100 最高换算成 `101-p`，v4 priority 1 最高）、分块哈希 v3 带显式序号（提交时排序）而 v4 按文档顺序、v3 静默容错而 v4 `logError` 且坏 size 取消整个条目。
- `MetalinkParserController` 用**事务 + 空指针检查**收集乱序到达的数据，并做规范化：URL joinUri 补全与 scheme 推断、哈希算法归一化/长度校验/择强保留、种子型资源统一收敛为 `metaurls`。
- V4 的每条 `logError` 都不是警告而是判决：`metalink_helper::parseFile` 看到 `getErrors()` 非空即抛 `METALINK_PARSE_ERROR`（错误码 20）拒绝整份文件。
- **一套 SAX 基建服务多个子系统**：`ParserStateMachine` 抽象 + `SessionData` 字符栈 + libxml2/Expat 双适配（编译期由 `HAVE_LIBXML2`/`HAVE_LIBEXPAT` 二选一），Metalink 与 XML-RPC（u8-l4）共享同一套骨架。

## 7. 下一步学习建议

- **下一讲 u8-l2《Metalink 下载：从 entry 到多源任务》**：本讲产出 `Metalinker`/`MetalinkEntry` 之后的故事——`Metalinker` 如何按 location/preference 排序资源、`Metalink2RequestGroup` 如何把 entry 变成多镜像 `RequestGroup`、Metalink/HTTP（RFC 6249）如何从响应头动态构建等价信息。建议先精读 [src/Metalinker.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalinker.cc) 的 `queryEntry` 之后的排序与 `MetalinkEntry::supported` 相关代码。
- **横向对照另一个 SAX 用户**：u8-l4 的 XML-RPC 解析状态机（`XmlRpcRequestParserStateMachine`）复用了本讲的全部基建，读完本讲再看它会非常快，可作为自测：能否指出它的「Initial 状态」在哪里、如何分派？
- **回看校验和的另一半**：本讲只做了「解析并择强」；哈希如何被用于下载完成后的校验，见 u5-l5《校验与哈希》的 `IteratableChecksumValidator` 一族。
- **测试即文档**：[test/MetalinkProcessorTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/MetalinkProcessorTest.cc) 的 17 个用例几乎每个对应一种违规文档（坏 size、坏优先级、目录穿越、多组 pieces……），把它们当行为规格表读，是检验本讲理解最省力的方式。
