# 七本书的体系与分层学习路线

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出仓库五级分类（Bridge / Deep Dive / Advanced / Expert / Practices）各自的定义，以及它们之间的递进与并列关系。
2. 读懂任意一本书 `SUMMARY.md` 中的 Part 分部结构，理解 Part 边界是作者对「学习者认知阶段」的切分，而不是随意的排版分组。
3. 根据一位读者的编程背景与目标，从七本书中组合出一条带进入条件、阅读顺序、理由和时长估算的跨书学习路径。

本讲是内容层的第一讲：前面四个单元我们一直在看「书架」（仓库结构、构建工具），从本讲开始我们正式研究「书架上写的到底是什么」。

## 2. 前置知识

本讲默认你已经理解以下来自前几讲的概念，这里只做简短回顾：

- **SUMMARY.md 的语法要素**（u1-l4）：mdBook 用每本书 `src/SUMMARY.md` 里的链接列表生成侧边栏导航。`# Part 标题` 这类不带链接的标题声明「分部」，只起分组作用、不可点击；带链接的条目是章；缩进的条目是某章内部的小节；编号章的序号**跨 Part 连续**，不会因为换了 Part 就重新从 1 开始。
- **BOOKS 注册表**（u1-l2、u2-l4）：`xtask/src/main.rs` 中的 `BOOKS` 常量是 `(slug, title, description, category)` 四元组数组，是书籍元数据的机器可读版本；README 的表格则是人类可读版本。两处语义一致但**字面存在漂移**，需要人工保持同步。
- **落地页**（u2-l4）：`cargo xtask build` 会根据 BOOKS 生成 `site/index.html`，每本书一张卡片，类别决定卡片的颜色标签（`cat-{category}` CSS 类）。

另外解释两个本讲要用的课程设计术语：

- **桥梁书（Bridge book）**：帮助「已经会另一门语言的人」迁移到 Rust 的入门书。它假设你懂编程、不假设你懂 Rust。
- **深潜书（Deep Dive book）**：不再教 Rust 基础，而是把某一个子系统（如异步运行时）从原理到生产讲透的书。它假设你 already 能写同步 Rust。

## 3. 本讲源码地图

本讲涉及的文件都是「内容的元数据」——不涉及任何可执行逻辑：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md) | 五级分类的定义表 + 七本书的级别/受众表 |
| [xtask/src/main.rs](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs) | `BOOKS` 常量：category 字段是五级分类的机器可读版 |
| [async-book/src/SUMMARY.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/SUMMARY.md) | Deep Dive 级的 Part 分部范例 |
| [c-cpp-book/src/SUMMARY.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/c-cpp-book/src/SUMMARY.md) | 桥梁书之一：14 章主线 + 系统向深潜 |
| [csharp-book/src/SUMMARY.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/csharp-book/src/SUMMARY.md) | 桥梁书之二：托管语言向迁移 |
| [python-book/src/SUMMARY.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/SUMMARY.md) | 桥梁书之三：动态语言向迁移 |
| [engineering-book/src/SUMMARY.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/engineering-book/src/SUMMARY.md) | Practices 级的 Part 分部范例 |
| [rust-patterns-book/src/SUMMARY.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/SUMMARY.md) | Advanced 级的主题地图（本讲只看结构） |
| [type-driven-correctness-book/src/SUMMARY.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/SUMMARY.md) | Expert 级的主题地图（含一个编号错位的有趣细节） |
| [async-book/src/ch00-introduction.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch00-introduction.md) | 深潜书的「受众 + 前置条件 + 配速表」写法 |
| [python-book/src/ch00-introduction.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/ch00-introduction.md) | 桥梁书的 Part 划分理由与配速表 |
| [engineering-book/src/ch00-introduction.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/engineering-book/src/ch00-introduction.md) | Practices 级「章章独立」的自述 |

## 4. 核心概念与源码讲解

### 4.1 五级分类体系

#### 4.1.1 概念说明

一个有七本书的仓库，最怕的事情是「读者不知道先读哪本」。这个仓库的解法是用一套**五级难度分类**给所有书定位：

| 级别 | 含义 |
|------|------|
| 🟢 Bridge（桥梁） | 从另一门语言迁移到 Rust 的入门书，从这里开始 |
| 🔵 Deep Dive（深潜） | 对某一个 Rust 子系统的专题深入 |
| 🟡 Advanced（进阶） | 面向有经验 Rustaceans 的模式与技巧 |
| 🟣 Expert（专家） | 前沿的类型级正确性技术 |
| 🟤 Practices（实践） | 工程、工具链与生产就绪 |

关键是要理解五级之间的关系**不是一条严格的链**，而是「三个平行入口 + 四个专项深化」：

```text
                 ┌─ c-cpp-book  (bridge)  ←── C/C++ 背景
读者 ──选入口──┼─ csharp-book (bridge)  ←── C#/Swift/Java 背景
                 └─ python-book (bridge)  ←── Python 背景
                          │
        ┌─────────────────┼──────────────────┬────────────────┐
        ▼                 ▼                  ▼                ▼
  async-book        rust-patterns-book  type-driven-...  engineering-book
  (deep-dive)         (advanced)           (expert)         (practices)
  异步子系统           高级模式             类型级正确性       生产工程化
```

三本桥梁书是**并列关系**——你只需要读其中一本（按背景三选一），而不是三本都读。后四本书是**专项深化**，彼此也基本并列，选哪个取决于你的方向；它们共同的隐含前置是「已经会写基本的 Rust」，也就是先走完一座桥。

#### 4.1.2 核心流程

五级分类在仓库里以「一义三源」的方式存在：

1. **README 级别定义表**：定义每个级别是什么意思（给人看的语义）。
2. **README 书目表**：把每本书映射到一个级别，并给出「适合谁」的一句话。
3. **xtask 的 BOOKS 常量**：category 字段是同一分类的机器可读版，驱动落地页的颜色标签。

任何一本新书要加入分类体系，这三处（实际上是两处文件）都要同步更新——这正是 u1-l1 讲过的「双源维护」问题在内容层的体现。

#### 4.1.3 源码精读

**第一处：README 中的级别定义表。** 这五行定义了整个课程体系的语义，是理解仓库内容组织的入口：

[README.md:L39-L45](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L39-L45)

> 这段是 README 的 Level/Description 两列表。注意 🟢 Bridge 那行明确写着 "start here"——它告诉初学者：如果你还没入门，别看后面四级。

**第二处：README 中的书目表。** 它把七本书逐一挂到级别上，并给出受众：

[README.md:L47-L55](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L47-L55)

> Book/Level/Who it's for 三列。注意 Level 列用 emoji + 级别名双重标记（🟢 Bridge），而 "Who it's for" 列就是每本书的「进入条件」的简写，例如 async-book 一行是 "Tokio, streams, cancellation safety"（主题），type-driven 一行是 "Type-state, phantom types, capability tokens"。

**第三处：xtask 的 BOOKS 常量。** 每个四元组的第四个字段就是类别 slug：

[xtask/src/main.rs:L9-L49](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L9-L49)

> 这个数组按「三本 bridge → 一本 deep-dive（async-book）→ 一本 advanced（rust-patterns-book）→ 一本 expert（type-driven-correctness-book）→ 一本 practices（engineering-book）」的顺序排列，category 字符串依次是 `bridge`、`bridge`、`bridge`、`deep-dive`、`advanced`、`expert`、`practices`——与 README 两张表一一对应。这些字符串会被 u2-l4 讲过的 `category_label` 翻译成落地页标签并拼成 `cat-{cat}` CSS 类。

顺带一个值得注意的漂移实例：README 第 57 行宣称 "Each book has 15–16 chapters"：

[README.md:L57](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L57)

> 但核对各书 SUMMARY 会发现实际编号章数量是 13 到 19 章不等（engineering-book 13 章、c-cpp-book 与 rust-patterns-book 19 章）。这是 README（人读）与 SUMMARY（机器读）之间又一处已经发生的漂移，印证了「双源需要人工同步」的代价。

#### 4.1.4 代码实践

这是一个源码核对型实践，不需要编译运行。

1. **实践目标**：验证五级分类在「README 定义表、README 书目表、BOOKS 常量」三处的数据是否完全一致，并亲手找到至少一处漂移。
2. **操作步骤**：
   - 打开 [README.md:L39-L55](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L39-L55)，抄下七本书的 (书名, 级别) 对。
   - 打开 [xtask/src/main.rs:L9-L49](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L9-L49)，抄下七个 (slug, title, category) 对。
   - 逐行比对：书名↔title、级别↔category、README 的 "Who it's for" 列↔BOOKS 的 description 字段。
   - 可选：运行 `cargo xtask serve` 后打开 http://localhost:3000，观察落地页图例中的五个类别与卡片颜色是否与 category 一一对应。
3. **需要观察的现象**：级别↔category 完全一致，但受众描述不一致。例如 csharp-book 在 BOOKS 中的 description 是 "Best for Swift / C# / Java developers"，而 README 书目表的对应列写的是 "Swift / C# / Java → ownership & type system"。
4. **预期结果**：得到一张 7 行的三源对照表，其中 category 列零差异、description 列存在措辞差异；由此得出结论——改书的「定位描述」需要同时改 README 和 BOOKS 两处，而改「级别」还要额外检查落地页颜色是否仍然合理。落地页渲染效果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`type-driven-correctness-book` 在 README 中属于哪一级？它在 BOOKS 常量中的 category 字符串是什么？

**答案**：属于 🟣 Expert 级；category 字符串是 `expert`（见 [xtask/src/main.rs:L40-L45](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L40-L45)）。

**练习 2**：三本桥梁书为什么同属 `bridge` 一个级别，而不是分成三个级别？

**答案**：级别衡量的是**内容深度**而非读者背景。三本书的深度相同（都是从零教 Rust 基础），差异只在「用哪门语言的经验来类比讲解」，所以共享同一个级别 slug；背景分流是由「三本不同的书」完成的，不是由级别完成的。

**练习 3**：如果维护者新增了第八本书但忘了更新 README 的书目表，只改了 BOOKS，会发生什么？

**答案**：落地页和构建流程完全正常（u2-l3/u2-l4 已讲：构建只遍历 BOOKS），但 README 的读者看不到这本书的介绍——这正是双源结构的典型故障模式：机器侧正确、人读侧过期。

### 4.2 Part 分部结构

#### 4.2.1 概念说明

每本书内部再用 `# Part …` 标题切成几个「分部」。Part 不是排版装饰，而是**教学阶段划分**：作者用它在长长的章列表里告诉读者「到这里，你完成了一个学习阶段」。

七本书的 Part 划分方式可以分成三种典型策略：

1. **认知递进式**（async-book）：Part I 原理 → Part II 生态 → Part III 生产。对应「先懂机制、再会用工具、最后能上线」的学习顺序。
2. **工作流式**（engineering-book）：Part I 构建与交付 → Part II 度量与验证 → Part III 加固与优化 → Part IV 集成。对应软件交付流程的各个环节，所以这本书可以「按需跳章」。
3. **难度断层式**（三本桥梁书）：Part 边界切在「读者认知发生质变」的地方，且三本书切的位置不同——这是 4.3 节的主题。

另外两个结构细节（承接 u1-l4）：

- Part 标题本身没有链接，不可点击，纯粹是分隔与命名。
- 章编号跨 Part 连续。async-book 第 6 章属于 Part II，但编号紧接 Part I 的第 5 章。
- Part 之间的 `---` 分隔线是可选的装饰——type-driven-correctness-book 的各 Part 之间就没有 `---`，分部语义完全由 `# Part` 标题承担。

#### 4.2.2 核心流程

一本书的典型教学骨架：

```text
[Introduction（ch00，不编号）]
   │  说明受众、前置条件、如何使用本书、配速表
   ▼
# Part I —— 第一阶段（若干编号章）
   ▼
# Part II —— 第二阶段（编号章继续累加）
   ▼
# Part III / IV ... —— 后续阶段
   ▼
[Appendices / Capstone（附录或毕业项目）]
```

读者拿到一本书时，判断「它怎么组织教学」的标准动作是：读 ch00（受众与前置）→ 扫 SUMMARY 的 Part 标题（阶段划分）→ 按配速表估算投入。

#### 4.2.3 源码精读

**async-book：认知递进式的典范。** 三个 Part 加附录，各 5 章，结构极其工整：

[async-book/src/SUMMARY.md:L7-L13](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/SUMMARY.md#L7-L13)

> Part I "How Async Works"（第 1–5 章）：Why Async → Future trait → Poll → Pin/Unpin → 状态机揭示。这是「从第一性原理理解机制」的阶段。

[async-book/src/SUMMARY.md:L17-L22](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/SUMMARY.md#L17-L22)

> Part II "The Ecosystem"（第 6–10 章）：手写 future → 执行器与运行时 → Tokio 深潜 → 什么时候不用 Tokio → async trait。从「原理」转入「真实工具链」。

[async-book/src/SUMMARY.md:L27-L33](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/SUMMARY.md#L27-L33) 与 [async-book/src/SUMMARY.md:L37-L40](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/SUMMARY.md#L37-L40)

> Part III "Production Async"（第 11–15 章：streams、常见陷阱、生产模式、架构忠告、练习）与 Appendices（参考卡 + 异步聊天服务器毕业项目）。

这个「原理 → 生态 → 生产」的阶段划分不是我们猜的，作者在引言里写明了：

[async-book/src/ch00-introduction.md:L28-L30](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch00-introduction.md#L28-L30)

> "Read linearly the first time. Parts I–III build on each other."——明确要求第一次线性阅读，因为三个 Part 是层层依赖的。这与 engineering-book 形成鲜明对比（见下）。

**engineering-book：工作流式 + 章章独立。** 四个 Part 按交付流程排列：

[engineering-book/src/SUMMARY.md:L7-L9](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/engineering-book/src/SUMMARY.md#L7-L9)

> Part I "Build & Ship"（build.rs、交叉编译）——先能把东西构建出来发出去。

[engineering-book/src/SUMMARY.md:L14-L18](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/engineering-book/src/SUMMARY.md#L14-L18)

> Part II "Measure & Verify"（基准测试、覆盖率、Miri/Valgrind/消毒器）——然后度量与验证它。

[engineering-book/src/SUMMARY.md:L22-L28](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/engineering-book/src/SUMMARY.md#L22-L28) 与 [engineering-book/src/SUMMARY.md:L32-L36](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/engineering-book/src/SUMMARY.md#L32-L36)

> Part III "Harden & Optimize"（供应链安全、release profile 与二进制体积、编译期工具、no_std、Windows 条件编译）；Part IV "Integrate"（生产 CI/CD 汇总、技巧、速查卡）。

它的引言同样自述了与 async-book 相反的阅读方式：

[engineering-book/src/ch00-introduction.md:L17-L19](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/engineering-book/src/ch00-introduction.md#L17-L19)

> "Each chapter is largely independent — read them in order or jump to the topic you need."——章章独立、允许跳读。两种 Part 策略的差异直接决定你该如何排学习路径：async-book 必须整本啃，engineering-book 可以按需抽章。

**一个证明「SUMMARY 顺序即导航真相」的细节。** type-driven-correctness-book 的编号章与文件名出现了错位：

[type-driven-correctness-book/src/SUMMARY.md:L21](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/SUMMARY.md#L21)

> 显示为 "10. Const Fn — Compile-Time Correctness Proofs" 的章，链接的目标文件却是 `ch15-const-fn-compile-time-correctness-proofs.md`。同书第 12 章链接的是 `ch10-*.md`。这说明：**侧边栏的章节序号、层级、标题完全由 SUMMARY 中条目的出现顺序与缩进决定，文件名只是物理存储名**，改文件名不会自动改变导航，改 SUMMARY 顺序才会。这呼应了 u1-l4 讲过的「SUMMARY 是导航的单一数据源」。

**其余三本的 Part 一览**（供你对照着数）：

- [rust-patterns-book/src/SUMMARY.md:L7-L12](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/SUMMARY.md#L7-L12) Part I "Type-Level Patterns"（4 章：泛型、trait、newtype/type-state、PhantomData）→ [L16-L22](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/SUMMARY.md#L16-L22) Part II "Concurrency & Runtime"（5 章）→ [L26-L35](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/SUMMARY.md#L26-L35) Part III "Systems & Production"（8 章）→ 附录与毕业项目。
- [type-driven-correctness-book/src/SUMMARY.md:L7-L35](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/SUMMARY.md#L7-L35)：Part I 哲学（仅 1 章）→ Part II 核心模式（10 章）→ Part III 集成与实践（Redfish 实战等 5 章）→ Part IV 参考。
- 三本桥梁书的 Part 结构见下一节。

#### 4.2.4 代码实践

1. **实践目标**：为七本书各画一张「Part 结构卡」，掌握用 SUMMARY 快速评估一本书组织方式的方法。
2. **操作步骤**：
   - 依次打开七本书的 `src/SUMMARY.md`（路径见第 3 节源码地图）。
   - 对每本书统计：Part 数、各 Part 标题、各 Part 包含的编号章区间（如 async-book 为 5/5/5/附录 2）。
   - 把七行结果填进一张总表，并给每本书标注你判断的策略（认知递进 / 工作流 / 难度断层）。
3. **需要观察的现象**：Part 标题的措辞风格差异——async-book 和 engineering-book 用「动词性阶段名」（How Async Works / Build & Ship），桥梁书用「难度名词」（Foundations / Core Concepts / Deep Dives）。
4. **预期结果**：得到类似下表的成品（部分已填，其余由你补全）：

| 书 | Part 数 | 各 Part 章数 | 策略 |
|----|---------|--------------|------|
| async-book | 3 + 附录 | 5 / 5 / 5 / 2 | 认知递进 |
| engineering-book | 4 | 2 / 3 / 5 / 3 | 工作流 |
| c-cpp-book | 3 | 14 / 2 / 3 | 难度断层 |
| python-book | 4 | 6 / 6 / 4 / 1 | 难度断层 |
| csharp-book | 3 + Capstone | 12 / 2 / 2 / 1 | 难度断层 |
| rust-patterns-book | ？ | ？ | ？ |
| type-driven-correctness-book | ？ | ？ | ？ |

   （后两行留给你完成；rust-patterns-book 的答案可对照上一节给出的行号自查。）

#### 4.2.5 小练习与答案

**练习 1**：async-book 第 6 章 "Building Futures by Hand" 属于哪个 Part？它的编号为什么不是 1？

**答案**：属于 Part II "The Ecosystem"（[async-book/src/SUMMARY.md:L19](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/SUMMARY.md#L19)）。编号是 6 因为章编号跨 Part 连续累计（Part I 用掉了 1–5），Part 切分不影响编号。

**练习 2**：为什么说 async-book 和 engineering-book 的 Part 策略决定了它们在「学习路径」里的用法不同？

**答案**：async-book 自述 "Parts I–III build on each other"、要求线性阅读（[ch00-introduction.md:L28-L30](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch00-introduction.md#L28-L30)），所以路径里应作为整块投入；engineering-book 自述 "Each chapter is largely independent"（[ch00-introduction.md:L17-L19](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/engineering-book/src/ch00-introduction.md#L17-L19)），可以按主题抽读（比如只读交叉编译 + release profile 两章）。

**练习 3**：type-driven-correctness-book 显示章节号 10 的那一章，实际对应磁盘上哪个文件？这个错位说明什么？

**答案**：`ch15-const-fn-compile-time-correctness-proofs.md`（[type-driven-correctness-book/src/SUMMARY.md:L21](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/SUMMARY.md#L21)）。说明章节号、顺序、层级全部由 SUMMARY 条目的排列决定，文件名中的编号只是历史遗留，不参与导航计算——所以给章节「重新排序」只需要改 SUMMARY，不需要重命名文件。

### 4.3 桥梁书共享主线

#### 4.3.1 概念说明

三本桥梁书最有趣的设计是：**它们共享一条几乎逐字相同的 14 章主线**。从第 1 章 "Introduction and Motivation" 到第 14 章 "Unsafe Rust and FFI"，三本书的章标题基本一致——因为无论你从 C++、C# 还是 Python 来，学会 Rust 所需的知识骨架是同一副。

三本书的差异体现在三个层面：

1. **Part 边界切在哪里**：同一副骨架被切成不同的阶段组合（见 4.3.3）。
2. **章内子节与类比方式**：每本书在公共骨架的章节下挂了为特定背景定制的深潜小节。
3. **第 15 章之后的分化**：c-cpp-book 走系统方向（no_std、嵌入式、真实案例），csharp-book 与 python-book 走迁移方向（迁移模式、最佳实践）+ 毕业项目。

这个设计对学习路径的直接含义是：**读者按背景选一座桥即可，不必读第二座**——第二座桥的 14 章主线对你几乎是重复内容。

#### 4.3.2 核心流程

三本书的公共骨架（章号 → 主题）：

```text
 1  Introduction and Motivation     ← 为什么学 Rust（按背景给理由）
 2  Getting Started                 ← 安装与第一个程序
 3  Built-in Types (and Variables)  ← 类型系统第一课
 4  Control Flow
 5  Data Structures (and Collections)
 6  Enums and Pattern Matching
 7  Ownership and Borrowing         ← 范式转变的核心章
 8  Crates and Modules
 9  Error Handling
10  Traits (and Generics)
11  From and Into Traits
12  Closures (and Iterators)
13  Concurrency
14  Unsafe Rust and FFI
────── 公共主线到此为止，三书分化 ──────
```

而「Part 怎么切这条骨架」，三本书给出了三个不同答案：

- **c-cpp-book**：14 章全部塞进 Part I "Foundations"，Part II 才开始 "Deep Dives"（no_std、真实案例）。
- **csharp-book**：前 12 章为 Part I "Foundations"，13–14 章（并发、Unsafe/FFI）划入 Part II "Concurrency & Systems"。
- **python-book**：只有前 6 章是 Part I "Foundations"，从第 7 章（所有权！）开始就是 Part II "Core Concepts"。

#### 4.3.3 源码精读

**先看公共主线。** c-cpp-book 的 Part I 一口气列完 14 章：

[c-cpp-book/src/SUMMARY.md:L7-L29](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/c-cpp-book/src/SUMMARY.md#L7-L29)

> 第 7–14 章的标题依次是 Ownership and Borrowing、Crates and Modules、Error Handling、Traits、From and Into Traits、Closures、Concurrency、Unsafe Rust and FFI——与 python-book、csharp-book 的同名章一一对应（可对照 [python-book/src/SUMMARY.md:L20-L25](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/SUMMARY.md#L20-L25) 与 [csharp-book/src/SUMMARY.md:L28-L33](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/csharp-book/src/SUMMARY.md#L28-L33)）。注意 c-cpp 的第 10 章标题是 "Traits"，把 Generics 降级为它的子节——唯一一处结构级差异。

**python-book 为什么把 Part 边界切在第 7 章？** 作者在引言里写得非常明白：

[python-book/src/ch00-introduction.md:L8-L10](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/ch00-introduction.md#L8-L10)

> "Part I (ch 1–6) first — these map closely to Python concepts you already know. Part II (ch 7–12) introduces Rust-specific ideas like ownership and traits."——Part I 是「能用 Python 经验直接映射」的舒适区，Part II 从所有权开始是「Rust 特有思维」。对动态语言背景的读者，认知断层出现在第 7 章，Part 边界就切在那里。对应地：

[python-book/src/SUMMARY.md:L7-L14](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/SUMMARY.md#L7-L14) 与 [python-book/src/SUMMARY.md:L18-L25](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/SUMMARY.md#L18-L25)

> Part I "Foundations" 只含第 1–6 章；Part II "Core Concepts" 从第 7 章 "Ownership and Borrowing" 开始。

而 c-cpp-book 的读者本来就熟悉编译语言、静态类型和指针，真正的难点是生命周期细节和 move 语义这类「深水区」，所以它把整条 14 章主线都视为 Foundations，把深水区单独组织成 Part II：

[c-cpp-book/src/SUMMARY.md:L33-L38](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/c-cpp-book/src/SUMMARY.md#L33-L38)

> Part II "Deep Dives"：no_std（含嵌入式深潜小节）与 C++ → Rust 真实案例研究。Part III 则是最佳实践、语义深潜与宏（[L42-L50](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/c-cpp-book/src/SUMMARY.md#L42-L50)）。

csharp-book 与 python-book 则在第 15 章后走「迁移」路线：

[python-book/src/SUMMARY.md:L29-L34](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/SUMMARY.md#L29-L34) 与 [python-book/src/SUMMARY.md:L38-L40](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/SUMMARY.md#L38-L40)

> python-book 的 Part III "Advanced Topics & Migration"（并发、Unsafe/FFI、Migration Patterns、Best Practices）与 Part IV 毕业项目 "CLI Task Manager"。csharp-book 的对应结构见 [csharp-book/src/SUMMARY.md:L46-L54](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/csharp-book/src/SUMMARY.md#L46-L54)（Migration Patterns、Best Practices）与 [L58-L60](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/csharp-book/src/SUMMARY.md#L58-L60)（毕业项目：CLI 天气工具）。

**章内定制：同一个第 7 章，三本书挂的子节完全不同。**

- [c-cpp-book/src/SUMMARY.md:L16-L18](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/c-cpp-book/src/SUMMARY.md#L16-L18)：第 7 章下挂 "Lifetimes and Borrowing Deep Dive" 和 "Smart Pointers and Interior Mutability" 两个深潜子节——C++ 程序员对 RAII 有直觉，卡点在生命周期与共享可变性的细节。
- [csharp-book/src/SUMMARY.md:L20-L23](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/csharp-book/src/SUMMARY.md#L20-L23)：第 7 章下挂三个子节（Memory Safety Deep Dive、Lifetimes Deep Dive、Smart Pointers — Beyond Single Ownership）——GC 背景的读者连「内存为什么要管理」都要先补。
- [python-book/src/SUMMARY.md:L20](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/SUMMARY.md#L20)：第 7 章没有任何子节——动态语言背景的读者第一步是把所有权本身想通，深潜留给后面的书。

**书与书之间会互相指路。** python-book 的引言在读者撞墙时直接指向 async-book：

[python-book/src/ch00-introduction.md:L41](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/ch00-introduction.md#L41)

> "For deeper async patterns, see the companion Async Rust Training"——仓库作者自己也在把读者从桥梁书导向深潜书，这条相对链接（`../async-book/`）就是第 5 节综合实践中跨书路径的官方依据。

#### 4.3.4 代码实践

1. **实践目标**：用「第 7 章的子节差异」亲手验证三本桥梁书是「同一骨架、三种裁剪」。
2. **操作步骤**：
   - 分别打开 [c-cpp-book/src/SUMMARY.md:L16-L18](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/c-cpp-book/src/SUMMARY.md#L16-L18)、[csharp-book/src/SUMMARY.md:L20-L23](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/csharp-book/src/SUMMARY.md#L20-L23)、[python-book/src/SUMMARY.md:L20](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/SUMMARY.md#L20)。
   - 列出三份「第 7 章子节清单」，再对比三本书第 8 章的子节（如 csharp 的 Cargo vs NuGet）。
   - 用一句话回答：子节的多少与读者背景的什么属性相关？
3. **需要观察的现象**：读者的源语言离「手动内存管理」越近，同一条主线章下面挂的深潜子节越多（c-cpp 2 个、csharp 3 个、python 0 个）；而与源语言生态相关的章（包管理、迁移）只出现在对应的书里（如 csharp 的 "Package Management — Cargo vs NuGet"）。
4. **预期结果**：一张三行对照表 + 一句结论，形如：「子节密度与源语言与 Rust 在内存模型上的距离成正比——距离越远，越需要先建直觉而不是钻细节」。本实践纯源码阅读，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：三本桥梁书的公共主线共多少章？覆盖范围是什么？

**答案**：14 章（第 1 章 "Introduction and Motivation" 至第 14 章 "Unsafe Rust and FFI"），覆盖从安装、类型、控制流、数据结构、枚举匹配、所有权、模块、错误处理、trait、From/Into、闭包迭代器、并发到 Unsafe/FFI 的完整 Rust 基础骨架。

**练习 2**：python-book 的 Part II "Core Concepts" 从第几章开始？为什么恰好是那一章？

**答案**：从第 7 章 "Ownership and Borrowing" 开始（[python-book/src/SUMMARY.md:L18-L20](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/SUMMARY.md#L18-L20)）。因为引言明说 Part I 的 1–6 章能映射到 Python 已有概念，而所有权是第一个「Python 里完全没有对应物」的 Rust 特有思维（[ch00-introduction.md:L10](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/ch00-introduction.md#L10)）。

**练习 3**：一位会 C++ 的同事读完 c-cpp-book 后问「要不要再读 python-book 补一补」，你怎么回答？

**答案**：不需要整本读。python-book 第 1–14 章主线与 c-cpp-book 几乎重复；有价值的只有背景定制部分（python-book 第 15 章 Migration Patterns 及 PyO3 相关内容），且仅当工作涉及 Python/Rust 互操作时才值得抽读。

## 5. 综合实践

**任务：为一位虚构读者设计跨书学习路径。**

读者画像示例：*「小周，写了 8 年 Python 的后端工程师，Django/Flask 都熟，最近要做一个高性能 CLI 数据处理工具，没系统学过静态类型语言。」*

**操作步骤：**

1. **确定入口桥**：按背景在三本桥梁书中选一本。判据就是 4.1 讲的受众表——小周显然是 python-book。
2. **拆阶段并估时**：读该书的 ch00 配速表，把主线拆成投入块。python-book 的配速表在 [python-book/src/ch00-introduction.md:L14-L24](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/python-book/src/ch00-introduction.md#L14-L24)（如 1–4 章 1 天、第 7 章 1–2 天、毕业项目 2–3 天），据此估总时长 \(T=\sum_i t_i\)。
3. **核对深潜书的进入条件**：逐本打开候选深潜书的 ch00「Prerequisites」，判断读完桥之后是否满足。例如 async-book 的前置是所有权/借用/生命周期、trait 与泛型（含 `impl Trait`）、`Result` 与 `?`、基本多线程（[async-book/src/ch00-introduction.md:L18-L26](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch00-introduction.md#L18-L26)）——这些恰好是任何一座桥的第 7、9、10、13 章内容，所以「读完桥」天然满足「进深潜」。
4. **按目标裁剪深潜书**：目标决定读法。engineering-book 章章独立（[ch00-introduction.md:L17-L19](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/engineering-book/src/ch00-introduction.md#L17-L19)），按需抽章即可；async-book 必须线性整读（[ch00-introduction.md:L28-L30](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch00-introduction.md#L28-L30)），总时长 22–30 小时（[L45-L54](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch00-introduction.md#L45-L54)）。
5. **产出路径表**，格式：`顺序 | 书 | 章节范围 | 进入条件 | 理由 | 预计投入`。

**参考答案**（小周的路径表）：

| 顺序 | 书 | 章节范围 | 进入条件 | 理由 | 预计投入 |
|------|----|----------|----------|------|----------|
| 1 | python-book | Part I（1–6） | 会 Python | 作者明说这 6 章可映射到已有 Python 概念，快速建立信心 | 约 2–3 天 |
| 2 | python-book | Part II（7–12） | 完成顺序 1 | 所有权/trait 是范式转变核心，也是后续一切深潜书的前置 | 约 3–5 天 |
| 3 | python-book | 13–14 章 | 完成顺序 2 | CLI 工具可能涉及多线程；FFI 章含 PyO3，可复用既有 Python 资产 | 约 2 天 |
| 4 | python-book | 17 章 Capstone（CLI Task Manager） | 完成顺序 1–3 | 毕业项目就是「完整 CLI 应用」，与学习目标直接对口 | 约 2–3 天 |
| 5 | engineering-book | 第 7 章（release profiles 与二进制体积）、第 2 章（交叉编译） | 完成任一桥梁书 | 章章独立可跳读；高性能 CLI 的分发体积与多平台构建正是这两章主题 | 约 2–4 小时 |
| 6 | async-book | 整本（1–17） | 前置四项已由顺序 2–3 覆盖 | 当 CLI 需要并发网络 IO 时再进入；必须线性阅读 | 约 22–30 小时 |
| 7（可选） | rust-patterns-book | Part I（1–4） | 熟练 Rust | 追求 API 设计与类型级表达时的进阶方向 | 按需 |

**需要观察的现象 / 检验方法**：路径中每一行的「进入条件」都能在第 3 节源码地图的某个文件里找到出处（README 受众列或某书 ch00 的 Prerequisites），没有一行是凭感觉写的。

**预期结果**：你能对任意新画像（如「10 年 C# 桌面开发想转后端」「嵌入式 C 工程师做固件重写」）在 15 分钟内产出同样有据可查的路径表。若想在浏览器里边读边验证章节存在，可运行 `cargo xtask serve` 后逐个点开路径表中的章节链接（待本地验证）。

## 6. 本讲小结

- 五级分类是「三个平行入口 + 四个专项深化」：三本 Bridge 书按背景三选一，Deep Dive / Advanced / Expert / Practices 四本按方向选读，共同前置是先走完一座桥。
- 分类以「一义三源」存在：README 级别定义表给出语义，README 书目表给出书↔级映射，xtask 的 BOOKS category 字段是机器可读版——且人读侧与机器侧存在实际漂移（如 README 的 "15–16 chapters" 与实际 13–19 章不符）。
- Part 分部是教学阶段划分而非排版分组：async-book 用认知递进式（原理→生态→生产）且要求线性阅读，engineering-book 用工作流式（构建→度量→加固→集成）且章章独立可跳读——两种策略直接决定它们在学习路径中的用法。
- 章节序号与导航完全由 SUMMARY 条目顺序决定，type-driven-correctness-book 中「第 10 章链接 ch15 文件」的错位就是证据。
- 三本桥梁书共享一条几乎同名的 14 章主线，差异集中在三处：Part 边界切分位置（python 在第 7 章「所有权断层」处切开，c-cpp 把 14 章全归 Foundations）、章内深潜子节密度（离手动内存管理越近越多）、第 15 章后的方向分化（c-cpp 走 no_std/系统，csharp/python 走迁移+Capstone）。

## 7. 下一步学习建议

- 下一讲 **u3-l2「章节写作范式：目标框、Mermaid 图与可运行代码」**：本讲我们只看了书的「目录层」，下一讲进入一章内部，拆解每章固定的 "What you'll learn" 目标框、mermaid 图与 playground 可编辑代码块这三种写作构件。
- 如果你急着读某本书：直接按第 5 节的方法为自己的背景产出一张路径表，然后从入口桥的 ch00 读起——所有书的 ch00 都写了受众、前置与配速。
- 想验证本讲的结论：并行打开三本桥梁书的 SUMMARY.md 对照第 7 章子节（4.3.4 的实践），再浏览 type-driven-correctness-book 的编号错位（4.2.3），这两个观察最能巩固「SUMMARY 是导航单一数据源」的认知。
