# 三座桥梁：C/C++、C#、Python 路线对比

## 1. 本讲目标

学完本讲，你应该能够：

- 列出三本桥梁书（c-cpp-book、csharp-book、python-book）共享的 14 章公共主线，并说出主线各阶段（类型 → 所有权 → trait → 错误处理 → 并发）的教学意图。
- 识别各书为特定语言背景定制的「深潜章节」：C/C++ 的 FFI/no_std/语义深潜、C# 的不可变性与泛型约束、Python 的迁移模式。
- 通过对比三本书同名章节「Ownership and Borrowing」的三种开讲方式，理解「换类比」这一桥梁书的核心写作手法。
- 能按读者的语言背景，推荐最合适的一座「桥」，并说明理由。

## 2. 前置知识

本讲建立在 u3-l1 和 u3-l2 之上，先回顾两个已建立的概念：

- **桥梁书（Bridge book）**：本仓库五级分类中的入门级。三本桥梁书不是三本不同的 Rust 教程，而是**同一条 Rust 主线面向三种背景读者的三种讲法**——读者按自己的源语言（C/C++、C#、Python）三选一。
- **章节写作范式**：每章以 `> **What you'll learn:**` 目标框开篇，正文穿插 Mermaid 图与自包含的 Rust 代码块，章末以 Key Takeaways 收束。本讲会反复利用目标框快速判断一章「为谁而写、讲什么」。

另外需要两个文档层面的基础事实（均在 u1-l4 讲过）：

- `SUMMARY.md` 决定章节顺序与编号，文件名只是标识；两本书的「第 7 章」内容是否相同，取决于两份 `SUMMARY.md` 里对应条目的实际文件。
- 所有引用路径形如 `c-cpp-book/src/chXX-*.md`，都是仓库里真实存在的 Markdown 文件。

一个术语约定：下文把三本书主线中**标题几乎一致的章节**称为「主线章」，把**只有某本书才有（或只有某本书展开成独立小节）的内容**称为「定制深潜章」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [c-cpp-book/src/SUMMARY.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/c-cpp-book/src/SUMMARY.md) | C/C++ 桥梁书的章节目录，含 Part II 的 no_std/嵌入式深潜 |
| [csharp-book/src/SUMMARY.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/csharp-book/src/SUMMARY.md) | C# 桥梁书的章节目录，含不可变性、泛型约束等定制小节 |
| [python-book/src/SUMMARY.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/SUMMARY.md) | Python 桥梁书的章节目录，Part III 即迁移主题 |
| [c-cpp-book/src/ch07-ownership-and-borrowing.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/c-cpp-book/src/ch07-ownership-and-borrowing.md) | C/C++ 版所有权章：从 malloc/RAII 切入 |
| [csharp-book/src/ch07-ownership-and-borrowing.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/csharp-book/src/ch07-ownership-and-borrowing.md) | C# 版所有权章：从 GC 与引用复制切入 |
| [python-book/src/ch07-ownership-and-borrowing.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/ch07-ownership-and-borrowing.md) | Python 版所有权章：从引用计数切入 |
| [python-book/src/ch15-migration-patterns.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/ch15-migration-patterns.md) | Python 书的迁移模式章：模式翻译 + crates 对照 + 渐进式采用 |
| [c-cpp-book/src/ch15-no_std-rust-without-the-standard-library.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/c-cpp-book/src/ch15-no_std-rust-without-the-standard-library.md) | C/C++ 书定制深潜的代表：no_std 与嵌入式 |
| [csharp-book/src/ch03-1-true-immutability-vs-record-illusions.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/csharp-book/src/ch03-1-true-immutability-vs-record-illusions.md) | C# 书定制深潜的代表：record 的「不可变性幻象」 |
| [csharp-book/src/ch10-1-generic-constraints.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/csharp-book/src/ch10-1-generic-constraints.md) | C# 书定制深潜的代表：`where` 约束 vs trait bounds |

## 4. 核心概念与源码讲解

### 4.1 共享章节主线：一份 14 章的公共骨架

#### 4.1.1 概念说明

三本桥梁书之所以能覆盖三种背景的读者，前提是它们讲授的是**同一套 Rust**。把三份 `SUMMARY.md` 并排读可以发现：第 1 章到第 14 章构成一条几乎逐章同名的公共主线，本章称之为「14 章骨架」。

这条骨架解决了教学内容的问题：无论你从哪种语言来，学 Rust 的必经之路是固定的——类型系统、所有权、trait 与泛型、错误处理、并发。三本书在这条主线上**不争内容，只争讲法**：讲什么由骨架统一决定，怎么讲由背景决定。

#### 4.1.2 核心流程

14 章骨架可以分成五个教学阶段：

```text
阶段一 语法地基（ch01–ch06）
   1 Introduction and Motivation     为什么学 Rust
   2 Getting Started                 工具链与第一个程序
   3 Built-in Types (and Variables)  基本类型与变量
   4 Control Flow                    控制流
   5 Data Structures (and Collections) 结构体与集合
   6 Enums and Pattern Matching      枚举与模式匹配
阶段二 内存模型（ch07–ch08）
   7 Ownership and Borrowing         所有权与借用（全书最重的一章）
   8 Crates and Modules             包与模块
阶段三 抽象机制（ch09–ch12）
   9 Error Handling                  错误处理
   10 Traits (and Generics)          trait 与泛型
   11 From and Into Traits           类型转换
   12 Closures (and Iterators)       闭包与迭代器
阶段四 系统边界（ch13–ch14）
   13 Concurrency                    并发
   14 Unsafe Rust and FFI            unsafe 与外部函数接口
阶段五（ch15 起）→ 各书分道扬镳，见 4.2
```

注意一个 u3-l1 已建立的事实在这里再次应验：**Part 分部边界切在读者认知断层处**。python-book 把 Part I 切在第 6 章之后——前 6 章是 Python 程序员「感觉眼熟」的部分；Part II「Core Concepts」从第 7 章所有权开始，那是 Python 背景读者认知断层的第一站。c-cpp-book 则把整个 1–14 章都放在 Part I「Foundations」里，因为对 C/C++ 读者，所有权只是「换了套强制语法的 RAII」，断层在更后面的 no_std 与语义深潜。

#### 4.1.3 源码精读

先看 c-cpp-book 的 Part I——14 章主线在这里一目了然：

[c-cpp-book/src/SUMMARY.md:L9-L29](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/c-cpp-book/src/SUMMARY.md#L9-L29) 定义了从「1. Introduction and Motivation」到「14. Unsafe Rust and FFI」的全部主线章。注意第 7 章挂了两个子节：

[c-cpp-book/src/SUMMARY.md:L16-L18](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/c-cpp-book/src/SUMMARY.md#L16-L18) — 第 7 章主线章「Ownership and Borrowing」之下，还有「Lifetimes and Borrowing Deep Dive」与「Smart Pointers and Interior Mutability」两个缩进子节。子节是桥梁书做「背景定制」的第一种手段：主线章保持同名，深度用子节调节。

对照 csharp-book 的同名区段：

[csharp-book/src/SUMMARY.md:L20-L23](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/csharp-book/src/SUMMARY.md#L20-L23) — 同样是第 7 章「Ownership and Borrowing」，C# 版挂了**三个**子节：Memory Safety Deep Dive、Lifetimes Deep Dive、Smart Pointers — Beyond Single Ownership。对有 GC 背景的读者，内存安全需要补的课更多，所以子节更厚。

再看 python-book 的 Part 边界：

[python-book/src/SUMMARY.md:L18-L25](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/SUMMARY.md#L18-L25) — Part II「Core Concepts」恰好从第 7 章「Ownership and Borrowing」开始，且此章**没有任何子节**：对 Python 读者，主线章本身就已经是最陡的一段路，不再额外挂深潜。

主线章的标题也并非逐字相同。下面这张对照表来自三份 SUMMARY 的逐行比对，能看出「谁在给谁加词」：

| 章 | c-cpp-book | csharp-book / python-book | 差异意图 |
|----|-----------|--------------------------|---------|
| 3 | Built-in Types | Built-in Types **and Variables** | C/C++ 熟悉显式类型声明；托管语言读者需要补「变量绑定」的心智 |
| 5 | Data Structures | Data Structures **and Collections** | C/C++ 手写容器；C#/Python 读者自带集合库，需要映射 |
| 10 | Traits（Generics 为子节） | Traits **and Generics** | C# 把泛型放在语言核心，值得升入章名 |
| 12 | Closures（迭代器为子节） | Closures **and Iterators** | Python/C# 的推导式与 LINQ 使迭代器值得并列 |

#### 4.1.4 代码实践

**实践目标**：亲手验证「14 章骨架」的存在，而不依赖本讲的转述。

**操作步骤**：

1. 打开三份 `SUMMARY.md`（路径见第 3 节源码地图），各读一遍 Part I。
2. 做一张三列对照表（电子表格或纸笔均可），行为 ch01–ch14，列为三本书。
3. 逐行比对章标题：一致的打勾，不一致的抄下两边的原文。
4. 对每个不一致，写一句猜测：「这个加词/减词服务了哪种背景的读者？」

**需要观察的现象**：

- 大多数行的标题完全一致——骨架是真实存在的。
- 差异集中出现在第 3、5、10、12 章，且方向一致：c-cpp-book 更短，另两本更长。
- 三本书挂给第 7 章的子节数量不同：c-cpp 两个、csharp 三个、python 零个。

**预期结果**：你得到一张 14 行的对照表，以及 4 处左右的标题差异记录。这是本讲后续两个模块的分析底稿。

**待本地验证**：表格内容依赖你人工比对，无法由工具代劳。

#### 4.1.5 小练习与答案

**练习 1**：三本书的第 11 章都叫「From and Into Traits」。为什么这一章没有做任何背景定制，三本书标题完全一致？

**参考答案**：`From`/`Into` 是 Rust 特有的转换 trait 体系，C/C++、C#、Python 中都没有直接对应物（C++ 的隐式转换构造函数只覆盖一小部分语义）。没有源语言锚点可以「换类比」，三本书只能直接讲授 Rust 原生概念，因此标题与内容高度趋同。

**练习 2**：python-book 把 Part I/II 的边界切在第 6/7 章之间，而 c-cpp-book 把 1–14 章全放 Part I。用自己的话解释这个差异。

**参考答案**：Part 边界标记「读者认知断层」。对 Python 读者，第 6 章枚举之前的内容都还能用旧经验类比（变量、控制流、集合），第 7 章所有权是第一块完全陌生的领地，所以 Part II 从这里起。对 C/C++ 读者，所有权可以类比成「语法强制的 RAII」，认知断层后移到 no_std、语义深潜等主题，所以 Foundations 一直延伸到第 14 章。

**练习 3**：如果只看 `SUMMARY.md` 而不读正文，你如何快速判断某一章对某背景读者的难度？

**参考答案**：看子节数量与子节标题中的「Deep Dive」字样。同一主线章下挂的深潜子节越多，说明该书认为该主题对这类读者越需要额外展开；零子节（如 python-book 的第 7 章）则说明主线章本身已是full难度。

### 4.2 背景定制深潜章：同一骨架上的三种延伸

#### 4.2.1 概念说明

第 14 章之后，三本书分道扬镳。这部分内容是各书真正的「背景定制」：**它们不再回答「Rust 是什么」，而是回答「从你的旧世界到 Rust 有多远」**。

三种延伸方向：

- **C/C++ → 更靠近金属**：no_std、嵌入式、真实 C++ 迁移案例、C++/Rust 语义对勘、宏（替代 C 预处理器）。
- **C# → 更靠近类型系统**：record 的「不可变性幻象」、泛型约束对照、继承 vs 组合、Cargo vs NuGet。
- **Python → 更靠近迁移工程**：把 dict/class/decorator/context manager 逐个翻译成 Rust 对应物，并给出 PyO3 渐进式采用的路线图。

#### 4.2.2 核心流程

三本书「定制」的实现粒度从粗到细有三种：

```text
粒度 1：独立章（整章只有这本书有）
        c-cpp:   ch15 no_std、ch16 Case Studies、ch18 语义深潜、ch19 Macros
        python:  ch15 Migration Patterns
粒度 2：独立小节（主线章下挂的定制子节）
        csharp:  ch03-1 True Immutability、ch10-1 Generic Constraints、
                 ch10-2 Inheritance vs Composition、ch08-1 Cargo vs NuGet
粒度 3：章内视角（同一章名，正文按背景换例子）
        三本书的 ch07 所有权章 —— 见 4.3
```

#### 4.2.3 源码精读

**C/C++ 书的定制**集中在 Part II「Deep Dives」：

[c-cpp-book/src/SUMMARY.md:L33-L38](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/c-cpp-book/src/SUMMARY.md#L33-L38) — Part II 只有两章：第 15 章 `no_std`（含 Embedded Deep Dive 子节）和第 16 章真实世界 C++→Rust 案例研究。进入 no_std 章内部，它对背景的呼应用一句话就点明了：

[c-cpp-book/src/ch15-no_std-rust-without-the-standard-library.md:L5-L6](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/c-cpp-book/src/ch15-no_std-rust-without-the-standard-library.md#L5-L6) —「如果你来自嵌入式 C，早已习惯没有 `libc` 或只有极小运行时的环境；Rust 有一等价的对应物：`#![no_std]`」。随后该章用一张 `core`/`alloc`/`std` 三层能力表（[L13-L17](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/c-cpp-book/src/ch15-no_std-rust-without-the-standard-library.md#L13-L17)）告诉 C 工程师如何按「是否链接 `-lc`、是否用 `malloc`」选择层级——整段论证完全建立在 C 工程师的既有决策习惯上。

[c-cpp-book/src/SUMMARY.md:L49-L50](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/c-cpp-book/src/SUMMARY.md#L49-L50) — 第 18 章「C++ → Rust Semantic Deep Dives」与第 19 章「Rust Macros: From Preprocessor to Metetaprogramming」是 C/C++ 书独有的收尾：前者对勘两语言的语义差异，后者直接从 C 预处理器讲起过渡到 Rust 宏。注意 c-cpp-book 是三本中唯一**没有 Capstone 项目章**的（另两本第 17 章都是 Capstone），它的「毕业设计」让位给了案例研究与语义深潜。

**C# 书的定制**以「独立小节」为主，且大多挂在类型系统相关的主线章下：

[csharp-book/src/SUMMARY.md:L13](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/csharp-book/src/SUMMARY.md#L13) — 「True Immutability vs Record Illusions」挂在第 3 章（基本类型）之下。章内开篇就展示 C# record 的「不可变性剧场」：

[csharp-book/src/ch03-1-true-immutability-vs-record-illusions.md:L8-L22](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/csharp-book/src/ch03-1-true-immutability-vs-record-illusions.md#L8-L22) — 一段 C# 代码演示 `person with { Age = 31 }` 看似产生新实例，但 `person.Hobbies.Add("gaming")` 仍会波及所有 `with` 出来的副本——因为引用类型字段依然可变。这一节的教学策略是**先让源语言的「安全错觉」翻车，再引出 Rust 的编译期真不可变**。

[csharp-book/src/SUMMARY.md:L29-L30](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/csharp-book/src/SUMMARY.md#L29-L30) — 第 10 章下挂「Generic Constraints」与「Inheritance vs Composition」两个子节。前者开篇即对照：

[csharp-book/src/ch10-1-generic-constraints.md:L8-L13](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/csharp-book/src/ch10-1-generic-constraints.md#L8-L13) — 用 `public class Repository<T> where T : class, IEntity, new()` 这样的 C# 签名做起点，再翻译成 Rust 的 trait bounds。C# 读者最熟悉的 `where` 子句被当作通往 `where F: FnOnce() -> R` 的桥。

**Python 书的定制**干脆独立成章：

[python-book/src/SUMMARY.md:L29-L34](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/SUMMARY.md#L29-L34) — Part III 的标题就是「Advanced Topics & Migration」，第 15 章「Migration Patterns」是 Python 书的招牌定制章。章内是一系列「模式翻译」：

[python-book/src/ch15-migration-patterns.md:L9-L37](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/ch15-migration-patterns.md#L9-L37) — 第一组翻译：Python 的 `dict` 数据容器 → Rust `struct`（配 serde derive）。每个小节都是「左 Python 右 Rust」的双栏代码对照。

[python-book/src/ch15-migration-patterns.md:L39-L69](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/ch15-migration-patterns.md#L39-L69) — 第二组翻译：上下文管理器（`with`/`__exit__`）→ RAII 与 `Drop` trait，落点是「不需要 `with`，离开作用域即清理」。

[python-book/src/ch15-migration-patterns.md:L158-L195](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/ch15-migration-patterns.md#L158-L195) — 第三组翻译：模块级单例 → `std::sync::OnceLock` 的 `get_or_init`，把 Python 里惯用的 `__new__` 单例模式替换成标准库的线程安全惰性初始化。

[python-book/src/ch15-migration-patterns.md:L253-L267](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/ch15-migration-patterns.md#L253-L267) — 章末是一张 Mermaid 流程图，给出「Profile 找热点 → PyO3 + maturin 写扩展 → 同 API 替换调用 → 逐步扩张 → 纯 Rust 或保持混合」的渐进式采用路线——这是三本桥梁书中唯一把「新旧语言共存」当作正式主题来讲的。

#### 4.2.4 代码实践

**实践目标**：体会「定制深潜章」如何精准命中源语言读者的痛点。

**操作步骤**：

1. 读 [csharp-book/src/ch03-1-true-immutability-vs-record-illusions.md:L19-L22](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/csharp-book/src/ch03-1-true-immutability-vs-record-illusions.md#L19-L22)（record 翻车三行），把这段 C# 的行为用一句话说给没写过 C# 的人听。
2. 读 [c-cpp-book/src/ch15-no_std-rust-without-the-standard-library.md:L19-L21](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/c-cpp-book/src/ch15-no_std-rust-without-the-standard-library.md#L19-L21)（「链接 `-lc` 就用 core+alloc」的经验法则），判断你熟悉的一个 C 项目会落在哪一层。
3. 读 [python-book/src/ch15-migration-patterns.md:L218-L222](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/ch15-migration-patterns.md#L218-L222)（web 框架行的 crates 对照），记下 FastAPI/Flask 对应的 Rust 选项。
4. 三处各写一句「这一段只能出现在哪本书里，为什么」。

**需要观察的现象**：三段内容各自引用的源语言设施（`with` 表达式、`-lc` 链接选项、Flask）在其他两本书中几乎不会出现——定制深潜章的「方言感」非常强。

**预期结果**：你能在不看书的其余部分的情况下，仅凭这三段判断出它们各自属于哪座桥。

**待本地验证**：步骤 3 的表格行为可对照本地源码文件确认。

#### 4.2.5 小练习与答案

**练习 1**：三本书的第 14 章都叫「Unsafe Rust and FFI」，那么 FFI 是不是 C/C++ 书独有的定制内容？

**参考答案**：不是。第 14 章属于 14 章共享骨架，三本书都有（python-book 的 ch15 末尾还有「See also: Ch. 14 — Unsafe Rust and FFI covers the low-level FFI details needed for PyO3 bindings」的交叉引用，见 [python-book/src/ch15-migration-patterns.md:L269](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/ch15-migration-patterns.md#L269)）。区别在视角：C/C++ 书的 FFI 指向 C ABI 互操作与后续的 no_std/嵌入式，Python 书的 FFI 指向 PyO3 扩展。章名共享、正文视角不同，是「粒度 3」的定制。

**练习 2**：c-cpp-book 是三本书中唯一没有 Capstone 章的。它的 Part II/III 用什么承担了「综合演练」的职能？

**参考答案**：第 16 章「Case Studies: Real-World C++ to Rust」（含 ch16-cases-3-5 子节）用真实迁移案例承担演练职能，第 18 章「C++ → Rust Semantic Deep Dives」承担语义层面的收束。对已有系统编程经验的读者，案例研究比从头写一个玩具 CLI 更贴近其真实任务。

**练习 3**：如果一位 C# 读者问「Rust 有没有 NuGet」，仓库里哪个文件能直接回答？

**参考答案**：[csharp-book/src/SUMMARY.md:L25](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/csharp-book/src/SUMMARY.md#L25) 的子节「Package Management — Cargo vs NuGet」。它挂在第 8 章 Crates and Modules 下，正是「粒度 2：独立小节」式定制。

### 4.3 三书差异对比：所有权一章的三种开讲方式

#### 4.3.1 概念说明

「换类比」是桥梁书最核心的教学手法：同一个 Rust 概念，用读者源语言里已有的经验做脚手架。本模块以三本书同名的第 7 章「Ownership and Borrowing」为切片，逐个看三种开讲方式。

三书的类比选择：

| 书 | 源语言锚点 | 核心类比 |
|----|-----------|---------|
| c-cpp-book | `malloc`/`free`、RAII、智能指针 | 所有权 = 「语法强制的 RAII」，move 语义 = 「编译器帮你执行的 `std::move`」 |
| csharp-book | GC、引用复制、值/引用类型 | 所有权 = 「用编译期借用检查替代垃圾回收」，Copy/Move 类比 C# 值/引用类型 |
| python-book | 引用计数、「一切皆引用」 | 所有权 = 「单-owner 的引用计数」，move = 「赋值不再共享」 |

#### 4.3.2 核心流程

三书第 7 章的开篇结构都是三步，只是第一步的内容随背景更换：

```text
第一步：激活源语言痛点（各不相同）
   C/C++   → dangling pointer / use-after-free / Rule of Five
   C#      → GC 掩盖了所有权问题 / 引用复制陷阱
   Python  → b = a 之后 a 也变了的 surprise
第二步：给出 Rust 规则（三书几乎一致）
   单一 owner → 离开作用域即 drop → 所有权可转移（move）
第三步：用代码与图示对照两边的差异（形式随背景深浅不同）
```

#### 4.3.3 源码精读

**C/C++ 版**从内存管理 bug 讲起，第 1 行标题就是「Rust memory management」而非「Ownership」：

[c-cpp-book/src/ch07-ownership-and-borrowing.md:L5-L13](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/c-cpp-book/src/ch07-ownership-and-borrowing.md#L5-L13) — 先列 C（`malloc`/`free` 无悬垂检查）与 C++（RAII 有帮助但 `std::move` 后仍可编译通过、use-after-move 是 UB）的缺陷，然后一句话立起全章论点：「Rust makes RAII **foolproof**」——move 是破坏性的，编译器拒绝你触碰已被移出的变量，也不再需要 Rule of Five。

[c-cpp-book/src/ch07-ownership-and-borrowing.md:L15-L25](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/c-cpp-book/src/ch07-ownership-and-borrowing.md#L15-L25) — 一张「For C++ developers — Smart Pointer Mapping」对照表：`unique_ptr`→`Box`、`shared_ptr`→`Rc`/`Arc`、`weak_ptr`→`Weak`、裸指针→仅限 `unsafe` 块。对 C++ 读者，所有权不是新概念，而是旧概念映射表。

**C# 版**第一步是复习 C# 内存模型：

[csharp-book/src/ch07-ownership-and-borrowing.md:L3-L5](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/csharp-book/src/ch07-ownership-and-borrowing.md#L3-L5) — 目标框直接点题：「why `let s2 = s1` invalidates `s1` (unlike C# reference copying)……how the borrow checker replaces garbage collection」。GC 是 C# 读者对内存的全部假设，本章要把「回收」换成「编译期检查」。

[csharp-book/src/ch07-ownership-and-borrowing.md:L11-L28](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/csharp-book/src/ch07-ownership-and-borrowing.md#L11-L28) — 先给一段 `ProcessData()` 的 C# 代码：把 list 传给方法后原变量仍可访问、还可能被方法修改，GC 会在无引用时清理。这段代码不是反例，而是 C# 读者的「日常」——用它做对照基线。

[csharp-book/src/ch07-ownership-and-borrowing.md:L68-L79](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/csharp-book/src/ch07-ownership-and-borrowing.md#L68-L79) — 「Copy Types vs Move Types」小节把 Rust 的 Copy/Move 二分类比成 C# 的值类型/引用类型：`i32` 像 struct 一样复制，`String` 像 class 一样「转移」。这是三本书里最倚重类型系统类比的讲法。

**Python 版**的第一步是一次「惊吓」：

[python-book/src/ch07-ownership-and-borrowing.md:L12-L23](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/ch07-ownership-and-borrowing.md#L12-L23) — 开篇 Python 代码：`b = a` 之后 `b.append(4)`，`print(a)` 输出 `[1, 2, 3, 4]`——「surprise! a changed too」。注释里点明：谁拥有这个 list？两个变量都引用它，GC 在无引用时释放，「你从不需要想这件事」。

[python-book/src/ch07-ownership-and-borrowing.md:L25-L36](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/ch07-ownership-and-borrowing.md#L25-L36) — 紧接着同样的三行换成 Rust：`let b = a;` 之后使用 `a` 直接编译错误，「b 是唯一 owner，b 离开作用域时 Vec 被释放。确定性的。没有 GC。」

[python-book/src/ch07-ownership-and-borrowing.md:L80-L110](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/ch07-ownership-and-borrowing.md#L80-L110) — Python 版还配了三本书中最形象的一组对照：一张 ASCII 双栏图（左边 Python 引用计数 `del a → refcount 1`，右边 Rust `drop(b) → data freed`），紧接一张 Mermaid `stateDiagram` 把两套生命周期画成并列状态机。对最缺内存模型心智的读者，图示承担了最多的解释量。

三书对读者的预期坦白也值得对照：C/C++ 版目标框说「ownership clicks on the second pass for most C/C++ developers」（[c-cpp-book/src/ch07-ownership-and-borrowing.md:L3](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/c-cpp-book/src/ch07-ownership-and-borrowing.md#L3)），C# 版说这是「the biggest conceptual shift for C# developers」（[csharp-book/src/ch07-ownership-and-borrowing.md:L9](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/csharp-book/src/ch07-ownership-and-borrowing.md#L9)），Python 版则直呼「This is the hardest concept for Python developers」（[python-book/src/ch07-ownership-and-borrowing.md:L8](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/ch07-ownership-and-borrowing.md#L8)）。离手动内存管理越远，这一章被预告得越难——这也解释了 4.1 节观察到的子节数量差异（2/3/0 的另一种读法是：C/C++ 读者需要「更深的深潜」，Python 读者需要「主线章本身放慢」）。

#### 4.3.4 代码实践

**实践目标**：完成规格指定的核心任务——阅读三本书「Ownership and Borrowing」章的开头，写一份对比笔记，找出对自己最有效的讲法。

**操作步骤**：

1. 依次阅读以下三段（每段约 40 行，只读开头即可）：
   - [c-cpp-book/src/ch07-ownership-and-borrowing.md:L1-L25](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/c-cpp-book/src/ch07-ownership-and-borrowing.md#L1-L25)
   - [csharp-book/src/ch07-ownership-and-borrowing.md:L1-L48](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/csharp-book/src/ch07-ownership-and-borrowing.md#L1-L48)
   - [python-book/src/ch07-ownership-and-borrowing.md:L1-L36](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/ch07-ownership-and-borrowing.md#L1-L36)
2. 为每本书回答三个问题，各写 1–2 句：
   - 它用源语言里的**哪个具体设施**做类比（如 `std::move`、GC、引用计数）？
   - 它把所有权的**哪一条规则**放在最前面讲？
   - 它的第一段代码示例是「源语言代码」「Rust 代码」还是「两者并排」？
3. 写结论段：哪种讲法对你最有效，为什么（结合你自己的主力语言背景）。
4. （可选，需本地运行）在三个书籍目录下分别执行 `mdbook serve --open`，跳到第 7 章，对照网页确认你读的 Markdown 与渲染效果一致，顺便体验三书的 Mermaid 图（python 版的状态机图尤其值得在浏览器里看一次）。

**需要观察的现象**：

- 三书的「三条所有权规则」内容一致（单一 owner、离开作用域 drop、可转移），差异全在**引入方式**。
- C/C++ 版几乎不解释「为什么需要所有权」（读者天天和内存 bug 打交道），直接进入「Rust 怎么做得更好」；C# 与 Python 版则要先用源语言代码建立「你以为的内存模型」，再推翻它。
- 三书开篇的结构性差异：C/C++ 版标题是 H1「Rust memory management」、以要点列表开篇（承接全书更偏手册式的风格）；C# 与 Python 版以 H2「Understanding Ownership」开篇、正文先行。

**预期结果**：一份三行对比笔记加一段个人结论。如果你写不出「哪种讲法最有效」，通常说明第 2 步的三个问题有一个没落到实处，回头补读对应段落。

**待本地验证**：步骤 4 的浏览器验证依赖本地工具链（u1-l3 已讲过安装方式）。

#### 4.3.5 小练习与答案

**练习 1**：三书第 7 章的第一段代码分别是什么语言？这个顺序说明了什么？

**参考答案**：c-cpp 版第一段代码是 Rust（要点列表之后直接展示 owner/borrow 作用域示例）；csharp 版第一段是 C#（`ProcessData()` 复习）；python 版第一段是 Python（`b = a` 惊吓）。顺序说明了「读者离手动内存管理越远，越需要先用源语言代码铺垫痛点」：C/C++ 读者的痛点（悬垂指针）不需要代码唤醒，可以直接进正题。

**练习 2**：python-book 用「引用计数」做类比有什么潜在误导？书中如何缓解？

**参考答案**：引用计数会让读者以为 Rust 的 `Rc<T>` 是默认状态、多个变量可以随意共享同一个值，而 Rust 默认是单一 owner、赋值即 move。书中通过开篇的对照（Python 两变量共享 vs Rust move 后原变量失效）与 ASCII/状态机双图把「共享是 Python 的默认、move 是 Rust 的默认」直接并置，缓解了这个误导；共享所有权被明确标注为需要显式选择的 `Rc`/`Arc`。

**练习 3**：如果仓库要新增一本 java-book，按本讲总结的规律，它的第 7 章开篇应该怎么写？给出提纲。

**参考答案**：Java 读者与 C# 读者同属 GC 背景，可复用 csharp 版骨架但换源语言设施：第一步用 Java 代码展示「引用赋值之后两个变量指向同一对象、GC 在无引用时回收」，并把 `Clone`/深浅拷贝的困惑作为痛点；第二步给三条所有权规则，把借用检查器定位为「编译期的 GC 替代」；第三步做 Copy/Move 与 Java 基本类型/引用类型的类比，并把「为什么 `String` 在 Java 不可变但可以共享、在 Rust 里 move」作为对照点。

## 5. 综合实践

**任务：为三种背景的读者各写一份「选桥 + 学习计划」推荐卡，并用本地构建验证你的推荐。**

1. 构造三位虚构读者，例如：
   - A：写了 10 年 C 的嵌入式工程师，维护一个跑在 Cortex-M 上的固件；
   - B：写了 6 年 C# 的业务后端工程师，重度使用 LINQ 与 record；
   - C：写了 4 年 Python 的数据工程师，日常用 pandas 和一个内部 FastAPI 服务。
2. 为每人完成：
   - 从三本书中选一座桥，理由必须引用**具体的定制深潜章**（如 A → c-cpp-book 的 ch15 no_std，B → csharp-book 的 ch03-1，C → python-book 的 ch15 及其 PyO3 采用路线）；
   - 标出该读者可以**快速略过**的主线章与必须**放慢精读**的主线章（提示：回到 4.1 的对照表，看哪一章的标题差异或子节数量暗示了难度）；
   - 预告该读者在第 7 章会遇到什么（用 4.3 的三书开篇对比做依据）。
3. 本地验证（需本地工具链，参考 u1-l3）：运行 `cargo xtask build` 后 `cargo xtask serve`，在落地页点进你为三位读者选的三本书，核对：侧边栏的章节顺序与你读过的 SUMMARY 一致；每位读者的「必精读章」在渲染后的页面里确实存在。
4. 把三份推荐卡合并成一页笔记，作为你向团队同事介绍这套培训库时的讲稿底稿。

**预期结果**：三份推荐卡各含「选桥理由（含深潜章引用）/ 略读清单 / 精读清单 / 第 7 章预警」四栏。这个任务会逼你把本讲的三个最小模块——骨架、定制、差异——全部用一遍。

**待本地验证**：步骤 3 依赖本地 mdbook 工具链与网络无关的本地服务。

## 6. 本讲小结

- 三本桥梁书共享一条 **14 章公共主线**（ch01 动机 → ch14 unsafe/FFI），讲什么由骨架统一决定；标题差异（如 Built-in Types vs Built-in Types and Variables）是背景定制的痕迹。
- 定制深潜有三种粒度：**独立章**（C/C++ 的 no_std、案例研究、语义深潜；Python 的迁移模式）、**独立小节**（C# 的不可变性、泛型约束、Cargo vs NuGet）、**章内视角**（同名章正文换类比）。
- 三个定制方向分别是：C/C++ **靠近金属**（no_std/嵌入式/宏替代预处理器）、C# **靠近类型系统**（record 幻象、`where` 约束、继承 vs 组合）、Python **靠近迁移工程**（模式翻译 + PyO3 渐进式采用）。
- 以第 7 章所有权为切片：三书用三种源语言锚点（RAII/`std::move`、GC/值引用二分、引用计数）讲同一组规则，「换类比」是桥梁书的核心手法。
- 三书对第 7 章难度的预告随「离手动内存管理的距离」递增（second pass → biggest shift → hardest），与各书挂在该章的子节数量形成呼应。
- c-cpp-book 是三本中唯一没有 Capstone 章的——对系统程序员，真实迁移案例研究比玩具项目更贴近其任务。

## 7. 下一步学习建议

- 下一讲 u3-l4「Async Rust 深潜」将离开桥梁层，进入 Deep Dive 层的 async-book：Future/poll/Waker 契约、Pin/Unpin 与生产模式。桥梁书第 13 章的 Concurrency（含 csharp-book 的 Async/Await Deep Dive 子节）是最好的预习材料。
- 若想继续横向比较内容层，可读 u3-l5（Patterns 与 Type-Driven Correctness 两本高级书）与 u3-l6（工程实践书）。
- 建议顺手精读的源码：[python-book/src/ch15-migration-patterns.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/ch15-migration-patterns.md)（模式翻译的完整范例，也是 u4-l5 贡献实战的最佳参照）与 [c-cpp-book/src/ch18-cpp-rust-semantic-deep-dives.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/c-cpp-book/src/ch18-cpp-rust-semantic-deep-dives.md)（语义对勘的写法范本）。
- 如果你已选定自己的桥，现在就是跳进正文第 7 章的时机——三本书的作者都同意：所有权这一章，值得读第二遍。
