# 章节写作范式：目标框、Mermaid 图与可运行代码

## 1. 本讲目标

学完本讲，你应该能够：

1. 识别本仓库书籍章节的固定写作范式：**开篇目标框 → 正文（Mermaid 图 + Rust 代码块）→ 收尾 Key Takeaways**。
2. 理解每个范式要素背后的技术支撑：blockquote 目标框、mdbook-mermaid 预处理器的两阶段渲染、playground 的可编辑运行机制、`<details>` 折叠练习。
3. 能按仓库范式独立写出一个包含目标框、Mermaid 图和可运行 Rust 示例的小节，并用 `mdbook serve` 预览验证。

本讲是「内容层」的第二讲：u3-l1 讲了七本书**宏观上**怎么组织（五级分类、Part 分部），本讲 zoom in 到**单章内部**，拆解一章是怎么写出来的。掌握这个范式后，你读任何一章都会更快（知道去哪里找重点），也为 u4-l5 的真实贡献做好了准备。

## 2. 前置知识

本讲假设你已完成 u1-l4（一本书的解剖）和 u3-l1（七本书的体系）。需要用到的概念：

- **Markdown blockquote（引用块）**：以 `>` 开头的行。渲染后左侧有竖线、背景着色，视觉上像一个"提示框"。mdBook 没有专门的 callout 语法，本书就用 blockquote 来扮演提示框。
- **Mermaid**：一个用文本描述图的 JavaScript 库。你写 `sequenceDiagram`、`graph TD` 这样的伪代码，它在浏览器里把它画成 SVG 图。好处：图是纯文本，能进 git、能 diff、能随文档一起演进。
- **Rust Playground**：play.rust-lang.org 上的在线 Rust 编译运行环境。mdBook 的 playground 功能可以把书里的代码块一键送去那里运行。
- **HTML `<details>` 元素**：浏览器原生的折叠组件——`<summary>` 是始终可见的标题，点击后展开内部内容。它不是 Markdown 标准语法，但 mdBook 的 Markdown 处理器允许在章节里直接内嵌这类 HTML。
- **u1-l4 已建立的认知**（本讲直接沿用）：`book.toml` 里 `[preprocessor.mermaid]` 声明了 `mdbook-mermaid` 预处理器；`[output.html.playground]` 的 `editable`/`line-numbers` 是全局开关；mermaid 的两个 JS 资产放在书根目录；无 `fn main` 的代码块会被注入隐藏的 main 包装。
- **u3-l1 已建立的认知**：Part 分部是教学阶段划分；SUMMARY.md 条目顺序决定章节编号与侧边栏。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `async-book/src/ch02-the-future-trait.md` | 本讲的"标本章"：完整包含目标框、Mermaid 图、可运行代码、章内练习、Key Takeaways 五要素 |
| `async-book/book.toml` | 范式的技术支撑：mermaid 预处理器配置、playground 开关 |
| `async-book/src/ch15-exercises.md` | 独立练习章的组织方式：6 道题、`<details>` 折叠解答 |
| `async-book/mermaid-init.js` | 浏览器端 mermaid 初始化与主题适配（理解两阶段渲染的第二阶段） |
| `async-book/src/ch00-introduction.md` | 难度 emoji 图例（🟢🟡🔴）的官方定义 |
| `async-book/src/SUMMARY.md` | 练习章如何作为编号章注册进目录 |

## 4. 核心概念与源码讲解

### 4.1 学习目标框：章节开头的「学习契约」

#### 4.1.1 概念说明

打开本书任何一章，第一眼看到的永远不是正文，而是一个 `> **What you'll learn:**` 开头的引用块，列出 3–5 条"读完这章你会懂什么"。这就是**学习目标框**。

它解决三个问题：

1. **对读者**：这是一份契约。读之前扫一眼，判断这章是否值得读、自己是否已掌握（全懂就跳过）。
2. **对作者**：写作时的自我约束——每条目标都必须在正文中兑现，防止章节跑题。
3. **对复习者**：考试式回顾——只读 15 个目标框，几分钟扫完整本书的骨架。

与目标框配套的是**标题里的难度 emoji**。章标题 `# 2. The Future Trait 🟡` 末尾的 🟡 不是装饰，而是 ch00 引言中正式定义的难度标记。

#### 4.1.2 核心流程

一个读者面对本章时的决策路径：

```text
看标题末尾 emoji（🟢/🟡/🔴）
   → 判断难度是否匹配当前水平
      → 读 "What you'll learn" 目标框
         → 全部已知？跳到下一章
         → 有未知项？进入正文
            → 读完回看 Key Takeaways（4.4 节会讲）核对契约兑现
```

写作侧的规则（从全书中归纳）：

- 目标框紧跟 `# 章标题`，中间空一行，不写引言段落。
- 每条目标一个要点，用能力动词开头（"The `Future` trait: ..."、"Implementing a real future by hand"）。
- 标题 emoji 三档：🟢 入门、🟡 中级、🔴 高级，图例在 ch00。

#### 4.1.3 源码精读

ch02 的开篇——标题带 🟡，紧跟四条目标：

> 引用块用 `>` 前缀写成，`**What you'll learn:**` 加粗作为框标题，四条目标各占一行：[async-book/src/ch02-the-future-trait.md:L1-L7](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L1-L7)

难度 emoji 的官方图例，是 ch00 引言里的一张表：

> `| 🟢 | Beginner — foundational concept |` 等三行定义了三级难度符号，并声明"Parts I–III 层层递进，建议首读线性推进"：[async-book/src/ch00-introduction.md:L30-L36](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch00-introduction.md#L30-L36)

这不是某一章的偶然习惯，而是**全书统一范式**。用 Grep 统计 `async-book/src/` 可验证：`What you'll learn` 恰好出现在 ch00–ch14 这 15 个内容章中（ch15 是练习章、ch16 是总结卡、ch17 是 capstone，不适用）；章末的 `Key Takeaways` 同样是 15 章——**开头立契约、结尾做收束**，首尾呼应。

对比两个不同难度的章标题，感受 emoji 的分档：

- 入门章：[async-book/src/ch01-why-async-is-different-in-rust.md:L1](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch01-why-async-is-different-in-rust.md#L1)
- 高级章：[async-book/src/ch04-pin-and-unpin.md:L1](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch04-pin-and-unpin.md#L1)

#### 4.1.4 代码实践

1. **实践目标**：验证目标框是"活文档"——改动它立即反映到页面上。
2. **操作步骤**：
   1. 在仓库根目录执行 `cd async-book && mdbook serve --open`（浏览器自动打开书）。
   2. 打开第 2 章，对照页面上的目标框与源码 L3–L7。
   3. 临时把 L4 的一行改成 `> - The \`Future\` trait (EDITED)`，保存。
   4. 看到浏览器自动刷新、目标框第一条出现 `(EDITED)`。
   5. 用 `git checkout -- src/ch02-the-future-trait.md` 还原。
3. **需要观察的现象**：保存后约 1 秒内页面自动热刷新；侧边栏不变（目标框不影响导航）。
4. **预期结果**：目标框第一条显示 `(EDITED)`；还原后再次刷新恢复原样。热刷新行为待本地验证（取决于 mdbook 版本，通常无需手动刷新）。

#### 4.1.5 小练习与答案

**练习 1**：为什么目标框用 blockquote（`>`）而不是普通列表或标题？

**参考答案**：mdBook 没有专门的 callout/admonition 语法，blockquote 是 Markdown 标准语法中唯一能让内容获得"框感"（竖线 + 着色背景）的构造，且不占用章节标题层级、不出现在目录导航里。这是零扩展成本下的最佳近似。

**练习 2**：如果你要给 ch15（练习章）也加目标框，合理吗？

**参考答案**：不太合理。ch15 的内容是 6 道练习题，没有"知识点目标"可声明；仓库作者的选择也印证了这一点——练习章开篇直接是 `## Exercises` 和第一道题，不设目标框（见 4.4.3 节源码）。

**练习 3**：目标框和章末 Key Takeaways 是什么关系？

**参考答案**：一对首尾呼应的契约。目标框在阅读前声明"你将学会什么"（面向决策），Key Takeaways 在阅读后总结"你应该记住了什么"（面向复习）。两者条目通常一一对应（如 ch02 目标框第 1 条对应 Key Takeaways 第 1、2 条）。

### 4.2 Mermaid 代码块：两阶段渲染的图示

#### 4.2.1 概念说明

一本讲异步的书，光靠文字很难说清"执行器、Future、反应器三者在一次 I/O 中的时序"。本仓库的解法是 **Mermaid 文本图**：作者在 Markdown 里写一个 ` ```mermaid ` 代码块，块内是图的文本描述，最终读者在页面上看到的是一张真正的矢量图。

u1-l4 已经从**配置角度**讲过 mermaid 预处理器；本讲从**作者使用角度**把它讲透。关键是要理解它是**两阶段渲染**：

- **构建期（预处理器）**：`mdbook-mermaid` 把 ` ```mermaid ` 块改写成 HTML 结构（`<div class="mermaid">` 包裹图源码），并确保页面引入 mermaid 的 JS。
- **浏览器期（渲染器之后的运行时）**：`mermaid.min.js` 读取这些 div，把文本编译成 SVG 插入页面。

这个分工解释了一个 u1-l4 提过的事实：缺了 `mdbook-mermaid`，构建**直接失败**（预处理器阶段就断了）；而图的最终"画出来"发生在读者浏览器里。

#### 4.2.2 核心流程

```text
作者写:  ```mermaid + sequenceDiagram/graph/stateDiagram 文本
                        │
        ┌───────────────┴────────────────┐
        │ 构建期: mdbook 调用 mdbook-mermaid │
        │ （book.toml [preprocessor.mermaid]）│
        │ 把代码块改写为可被 JS 识别的结构      │
        └───────────────┬────────────────┘
                        │  输出静态 HTML + 引入两个 JS
        ┌───────────────┴────────────────┐
        │ 浏览器期: mermaid.min.js 执行      │
        │ mermaid-init.js 按当前主题初始化    │
        │ 文本图 → SVG                      │
        └────────────────────────────────┘
```

作者需要知道的三个使用要点：

1. **图类型**：全书 20 个 mermaid 块分布在 15 个章节文件中，用了三种类型——`graph`（流程图，最常见）、`sequenceDiagram`（时序图，ch02/ch13）、`stateDiagram`（状态图，ch03/ch05）。
2. **资产位置**：`mermaid.min.js` 和 `mermaid-init.js` 放在**书根目录**，`book.toml` 以书根为基准引用（u1-l4 已讲）。
3. **图里可以写 HTML**：如 ch02 时序图参与者名里的 `<br/>` 换行。

#### 4.2.3 源码精读

`book.toml` 中 mermaid 的两条相关配置——`additional-js` 把两个脚本注入每个页面，`[preprocessor.mermaid]` 声明构建期预处理器命令：

> `additional-js = ["mermaid.min.js", "mermaid-init.js"]` 与 `command = "mdbook-mermaid"` 正是两阶段渲染的两个锚点：[async-book/book.toml:L10-L17](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/book.toml#L10-L17)

ch02 的时序图（节选）——四个参与者、编号消息、`Note` 标注，讲的是"一次 poll 返回 Pending 后，Waker 如何把任务重新推回执行器队列"：

> 这段 `sequenceDiagram` 描述了 executor→future→OS→reactor 的完整唤醒循环，参与者名用 `<br/>` 换行：[async-book/src/ch02-the-future-trait.md:L30-L46](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L30-L46)

第二阶段的初始化脚本——按 `<html>` 上的主题 class 判断明暗，选 `default` 或 `dark` 主题初始化 mermaid，并在用户切换主题时整页刷新以重绘图表：

> `mermaid.initialize({ startOnLoad: true, theme })` 是浏览器期渲染的入口；主题切换监听用 `window.location.reload()` 这种"最简单"的方式让图换色：[async-book/mermaid-init.js:L5-L38](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/mermaid-init.js#L5-L38)

值得体会的工程取舍：mermaid 换主题本可以调用 API 局部重绘，作者却选择了整页刷新，并在注释里坦承这是 "Simplest way"。文档工程的判断标准是**够用且零维护**，不是极致体验。

#### 4.2.4 代码实践

1. **实践目标**：亲手走一遍"写文本图 → 浏览器看到 SVG"的完整链路。
2. **操作步骤**：
   1. `cd async-book && mdbook serve --open` 保持运行。
   2. 打开 `src/ch02-the-future-trait.md`，在 L61（时序图结束围栏）之后插入以下**示例代码**（并非仓库原有内容）：

      ````text
      ```mermaid
      graph TD
          A[poll 被调用] --> B{返回什么?}
          B -->|Ready| C[任务完成]
          B -->|Pending| D[注册 Waker<br/>让出线程]
      ```
      ````

   3. 保存后观察浏览器。
   4. 再做一个反向实验：临时注释掉 `book.toml` 的整个 `[preprocessor.mermaid]` 节，保存，观察构建输出与页面。
   5. 结束后 `git checkout -- src/ch02-the-future-trait.md book.toml` 还原。
3. **需要观察的现象**：第一次保存后页面出现一张三节点流程图（方角矩形是 `[]` 语法、菱形判断是 `{}` 语法）；第二次保存后终端里的 `mdbook serve` 报错或警告，页面上的图不再正常渲染。
4. **预期结果**：正向实验渲染出流程图；反向实验证明没有预处理器时 mermaid 块退化为普通代码块或构建失败——这正是 u1-l4 说"缺它则构建直接失败"的亲测版。具体报错文案待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 mermaid 渲染要拆成构建期和浏览器期两个阶段，而不是构建期直接生成 SVG？

**参考答案**：mermaid 是 JS 库，把"文本→SVG"的编译放在浏览器里执行，意味着 mdbook（Rust 程序）无需移植整套 JS 绘制逻辑，只需搬运文本；代价是每个页面都要加载 `mermaid.min.js`。这是"静态站点 + 客户端增强"的典型折中。

**练习 2**：全书最常用的 mermaid 图类型是什么？各适合表达什么？

**参考答案**：`graph`（流程图）最常见（20 块中占大多数），适合表达控制流/依赖关系；`sequenceDiagram`（时序图，ch02、ch13）适合表达多个参与者的协作顺序；`stateDiagram`（状态图，ch03、ch05）适合表达状态机的迁移。选型原则：按"要表达的关系结构"选图，而不是按好看选。

**练习 3**：`mermaid-init.js` 为什么要监听主题切换按钮？

**参考答案**：SVG 图的颜色在 `mermaid.initialize` 时由 `theme` 参数定死，而 mdBook 切换主题只换 CSS、不重跑 JS。不刷新页面的话，暗色主题下会残留为亮色配色的图。脚本干脆在主题变化时整页 reload，让初始化逻辑重跑一遍。

### 4.3 Rust 代码块与 Playground：让示例可运行

#### 4.3.1 概念说明

文档里的代码最大的敌人是"过时"——读者照着敲，编译不过。本仓库的对策是让大多数代码块**直接可运行**：页面上每个 rust 代码块带一个运行按钮，把代码送到 Rust Playground 在线编译执行；`editable = true` 还额外提供一个编辑按钮，读者可以**就地改代码再运行**，把书变成可实验的沙盒。

代码块实际分三档：

| 写法 | 页面效果 | 适用场景 |
|------|----------|----------|
| ` ```rust ` | 带行号 + 运行按钮 + 编辑按钮 | 自包含、可编译的示例 |
| ` ```rust,ignore ` | 带行号，**无任何按钮** | 依赖外部上下文的代码（如引用他章类型） |
| 其他语言/无语言 | 纯着色代码块 | 伪代码、配置、shell 命令 |

一个容易被忽略的行为（u1-l4 已引入，这里从作者视角重申）：**没有 `fn main` 的 rust 块会被注入一个隐藏的 main 包装**。所以书里大量"只定义类型/impl"的片段（如 ch02 的 `Ready42`）不需要伪装出无意义的 main 就能通过 playground 编译。

#### 4.3.2 核心流程

```text
作者写 ```rust 代码块
        │
mdbook 渲染为 <pre class="playground">（含行号 span、按钮）
        │
读者点 ▶ 运行按钮
        │
代码（必要时先注入隐藏 fn main 包装）→ 发往 play.rust-lang.org
        │
playground 返回编译/运行结果
```

作者的检查清单：

1. 示例尽量自包含：需要 `use` 就写 `use`，不依赖"上一段代码还在作用域里"。
2. 实在不能自包含（依赖他章的 `TimerFuture` 之类），标 `,ignore`，明确告诉读者"这段别拿去直接跑"。
3. 想让读者看到输出的，示例里要有 `println!`——只有定义没有调用的代码块即使编译通过也无输出。

#### 4.3.3 源码精读

playground 的两个全局开关：

> `editable = true` 加出编辑按钮，`line-numbers = true` 给代码加行号——它们是 `[output.html.playground]` 节下的整书级配置，作用于所有 rust 块：[async-book/book.toml:L19-L21](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/book.toml#L19-L21)

ch02 开篇的 `Future` trait 定义块——注意它**没有 `fn main`**，却能被 playground 接受，靠的就是隐藏 main 注入：

> 块内是 `pub trait Future` 与 `pub enum Poll` 的定义，作为函数体内的局部项在 Rust 中合法，注入 main 后即可编译：[async-book/src/ch02-the-future-trait.md:L13-L24](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L13-L24)

`ignore` 注解的真实用例——ch15 练习 6 的解答引用了第 6 章的 `TimerFuture`，脱离书本上下文无法编译，因此标记 ignore：

> ` ```rust,ignore ` 让这个代码块不进 playground（无运行按钮），代码中的注释 `// From Chapter 6` 说明了原因：[async-book/src/ch15-exercises.md:L331-L345](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch15-exercises.md#L331-L345)

ch02 的 `Delay` 完整实现是"长而自包含"的代表作——约 65 行，含全部 `use`、结构体定义与 `impl Future`：

> 从 `use std::task::{Context, Poll, Waker}` 到 `Poll::Pending` 收尾，这个块不依赖任何前文即可编译，符合"示例自包含"清单：[async-book/src/ch02-the-future-trait.md:L92-L157](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L92-L157)

顺带一提，ch02 在代码块外还大量使用行内代码（`` `poll()` ``、`` `Pending` ``）与**粗体组件表**（`**Output**`、`**poll()**`）来在不打断段落的情况下指代代码元素——这也是全仓库统一的行文习惯：[async-book/src/ch02-the-future-trait.md:L82-L86](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L82-L86)

#### 4.3.4 代码实践

1. **实践目标**：亲眼确认 playground 三档代码块的页面差异，并观察到隐藏 main 注入。
2. **操作步骤**：
   1. 打开构建好的第 2 章（`mdbook serve` 或 `cargo xtask serve` 后访问 `http://127.0.0.1:3000`）。
   2. 找到 L13 的 trait 定义块，观察它左侧行号、右上角的运行按钮（▶）与编辑按钮（铅笔图标）。
   3. 点击编辑按钮——playground 编辑器打开后，注意代码里出现了一个你没写过的 `fn main()` 包装，这就是"隐藏 main 注入"。
   4. 在编辑器里把代码改为下面这份**示例代码**（能在 playground 编译运行的完整版）后点运行：

      ```rust
      // 示例代码：给 ch02 的 Ready42 加上真正执行它的 main
      use std::future::Future;
      use std::pin::Pin;
      use std::task::{Context, Poll};

      struct Ready42;

      impl Future for Ready42 {
          type Output = i32;
          fn poll(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<i32> {
              Poll::Ready(42)
          }
      }

      fn main() {
          // 一个最小的"执行器"：阻塞式轮询到 Ready
          let mut fut = Box::pin(Ready42);
          let waker = std::task::Waker::noop();
          let mut cx = Context::from_waker(&waker);
          match fut.as_mut().poll(&mut cx) {
              Poll::Ready(v) => println!("got {v}"),
              Poll::Pending => println!("pending"),
          }
      }
      ```

   5. 再打开 ch15 练习 6 的解答块（`rust,ignore`），对比它没有任何按钮。
3. **需要观察的现象**：三档块的外观差异；编辑器里注入的 `fn main` 包装；示例代码运行输出 `got 42`。
4. **预期结果**：步骤 4 输出 `got 42`（`Waker::noop` 需要较新的 Rust 工具链，若 playground 报错可改用返回 `Pending` 分支的观察实验——具体行为待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Delay` 示例（ch02 L92–L157）能通过 playground 编译，却运行不出任何效果？

**参考答案**：该块只有定义（`struct Delay` 与 `impl Future`），没有 `fn main` 主动构造并轮询它。注入的隐藏 main 只是个空壳包装，定义不产生输出。想看到效果需要像 4.3.4 的示例代码那样补一个驱动它的 main——这也正是作者把"驱动 future"留给后续章节（ch03 执行器循环、ch06 手工构建）的原因。

**练习 2**：`line-numbers = true` 对读者的实际价值是什么？

**参考答案**：读者在讨论、提问、提 PR 时可以说"ch02 第 122 行的 `poll` 里"，行号成为沟通坐标系；作者更新代码后行号会随构建自动重算，零维护成本。

**练习 3**：如果一个代码块既想显示语法着色，又确定读者无法在 playground 运行，该怎么标注？本书哪里用了？

**参考答案**：用 ` ```rust,ignore `。本书 ch15 练习 6 的解答（L331）是现成用例——它依赖第 6 章定义的 `TimerFuture`，单独送去编译必然失败，标 ignore 既保留着色又明确"勿运行"。

### 4.4 练习章组织：`<details>` 折叠与「先挑战后答案」

#### 4.4.1 概念说明

练习是这套书的教学闭环的关键一环，其组织有一个核心矛盾：**答案必须存在，但绝不能先入为主**。本仓库的解法是浏览器原生的 `<details>` 折叠元素——题干永远可见，答案默认折叠，读者先自己试，再点开对照。

书里有两种练习形态：

1. **章内练习**：正文讲完一个概念后紧跟的小练习，用**嵌套两层** `<details>`——外层"点击展开题目"，内层"点击展开解答"。ch02 的 `CountdownFuture` 练习是标本。
2. **独立练习章**：ch15 汇总 6 道综合性大题，每题只有**一层** `<details>` 装 `🔑 Solution`，题目本身不折叠（都折叠会让人不知道有什么题）。

另外注意 ch15 的标题是 `## Exercises`（h2），而普通章是 `# N. Title`（h1）——它作为编号章注册在 SUMMARY 里（第 33 行），侧边栏显示"15. Exercises"，但文件内部自降一级，与其"汇编章"而非"教学章"的定位一致。

#### 4.4.2 核心流程

两种练习形态的结构模板：

```text
章内练习（嵌套两层 details）
<details><summary>🏋️ Exercise (click to expand)</summary>
    题干 + *Hint* 提示
    <details><summary>🔑 Solution</summary>
        解答代码 + Key takeaway
    </details>
</details>

独立练习章（一层 details）
### Exercise N: 标题
需求列表（始终可见）
<details><summary>🔑 Solution</summary>
    解答代码 + 补充说明
</details>
---
（下一题）
```

读者动线：读题 → 自己尝试 → 卡住先看 Hint → 实在不行点开 Solution → 读 Key takeaway 提炼。答案的"解锁成本"被刻意做成了两段式。

#### 4.4.3 源码精读

ch02 的章内练习——外层折叠"题目 + 提示"，内层再折叠"解答"，双层保护让读者不会一瞥见答案：

> `<summary>🏋️ Exercise (click to expand)</summary>` 是题目壳，`*Hint*` 一行给提示但不剧透；内层 `<summary>🔑 Solution</summary>` 藏完整解答，最后以 `</details></details>` 双闭合收尾：[async-book/src/ch02-the-future-trait.md:L163-L210](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L163-L210)

ch15 独立练习章的组织——h2 总标题，每题 h3 + 需求列表 + 单层折叠解答：

> `### Exercise 1: Async Echo Server` 下是四条 Requirements，`<details><summary>🔑 Solution</summary>` 装整段可运行的 tokio 解答：[async-book/src/ch15-exercises.md:L1-L27](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch15-exercises.md#L1-L27)

题与题之间用 `---` 分隔，全章以 `***` 收尾：

> 水平分隔线是六道大题之间的视觉切分：[async-book/src/ch15-exercises.md:L61-L63](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch15-exercises.md#L61-L63)

练习章在 SUMMARY 中的注册方式——它是 Part III 里的**编号章**，不是附录：

> `- [15. Exercises](ch15-exercises.md)` 说明练习被当作正文的收尾环节，与 ch16 总结卡、ch17 capstone 同处 Part III 尾部：[async-book/src/SUMMARY.md:L27-L40](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/SUMMARY.md#L27-L40)

ch15 还有一个值得学习的细节：练习 4 的解答后面跟了一段"为什么不用 `Arc<Mutex<T>>`"的 blockquote，记录了**旧版答案踩过的坑**：

> 这段补充说明解释了从 `Arc<Mutex<T>>` 改为 `UnsafeCell` 的原因，把勘误历史沉淀进文档：[async-book/src/ch15-exercises.md:L269-L273](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch15-exercises.md#L269-L273)

#### 4.4.4 代码实践

1. **实践目标**：亲手写一个符合仓库范式的折叠练习，并验证折叠/展开行为。
2. **操作步骤**：
   1. 保持 `mdbook serve` 运行，在 `async-book/src/` 新建文件 `draft-exercise.md`，写入下面这份**示例代码**（非仓库原有内容）：

      ````markdown
      ## Draft: Option 练习

      <details>
      <summary>🏋️ Exercise (click to expand)</summary>

      **Challenge**: 写一个函数 `first_word_len(s: &str) -> Option<usize>`，
      返回第一个空白分隔单词的长度；空串或全空白返回 `None`。

      *Hint*: `s.split_whitespace().next()` 给出 `Option<&str>`。

      <details>
      <summary>🔑 Solution</summary>

      ```rust
      fn first_word_len(s: &str) -> Option<usize> {
          s.split_whitespace().next().map(|w| w.len())
      }

      fn main() {
          println!("{:?}", first_word_len("hello world")); // Some(5)
          println!("{:?}", first_word_len("   "));          // None
      }
      ```

      **Key takeaway**: `Option` + `map` 让"无值"在类型层面可见，调用者必须处理 `None`。

      </details>
      </details>
      ````

   2. 在 `SUMMARY.md` 第 40 行（Appendices 节末尾）临时加一行缩进对齐的 `- [Draft Exercise](draft-exercise.md)`。
   3. 浏览器确认新条目出现在侧边栏 Appendices 下，点开双层折叠，把 Solution 里的代码块用 playground 运行。
   4. 结束后删除 `draft-exercise.md` 并 `git checkout -- src/SUMMARY.md` 还原。
3. **需要观察的现象**：侧边栏出现新链接；页面初始只见 `🏋️ Exercise` 一行；点开后看到题干与 Hint；再点 `🔑 Solution` 才见到代码；代码块带运行按钮且输出 `Some(5)` 与 `None`。
4. **预期结果**：双层折叠顺序展开，playground 输出如注释所示。

#### 4.4.5 小练习与答案

**练习 1**：为什么章内练习嵌套两层 `<details>`，而 ch15 只用一层？

**参考答案**：章内练习出现在正文阅读流中，题目本身若常显会打断阅读节奏，所以"题目"也折叠（外层），答案再折一层（内层），解锁成本两段。ch15 是练习专章，读者打开它就是来做题的，题目必须一览无余地列出（否则连有哪些题都不知道），只有答案需要折叠。

**练习 2**：`<summary>` 里 emoji（🏋️、🔑）起什么作用？去掉会怎样？

**参考答案**：纯视觉 affordance——图标暗示"这里可点、点了会得到什么"（🏋️ 练习 / 🔑 答案）。去掉功能不受影响，只是可点击性和内容类型的暗示变弱。这属于低成本高收益的文档细节。

**练习 3**：ch15 的解答代码为什么大多可以送进 playground 运行，而练习 6 必须标 `ignore`？

**参考答案**：练习 1–5 的解答自包含（自带 `use` 与 `#[tokio::main]` 等，依赖来自 crates.io，playground 支持添加依赖），而练习 6 的 `Timeout` 结构体字段类型 `TimerFuture` 定义在第 6 章、不在本块内，单独编译必失败，故标 `ignore`——再次印证 4.3 的"示例自包含检查清单"。

## 5. 综合实践

现在把四个模块串起来：按仓库范式**完整地写一节小节草稿**。这是 u4-l5"提交真实 PR"前的全流程演练。

**任务**：选一个你熟悉的 Rust 概念（推荐 `Option` 或 `Result`），写一节 300–500 字的小节草稿，必须同时包含：① 开篇 `> **What you'll learn:**` 目标框（3–4 条）；② 一张 mermaid 图（任选类型）；③ 一个自包含、可在 playground 运行的 rust 代码块；④ 一个嵌套 `<details>` 的章内小练习；⑤ 结尾 `> **Key Takeaways**` 块。完成后用 `mdbook serve` 预览并逐项验收，最后还原仓库。

**操作步骤**：

1. `cd async-book && mdbook serve --open`。
2. 新建 `src/draft-section.md`，以 `# DRAFT. My Section 🟢` 开头（带难度 emoji，遵守 4.1 的图例）。
3. 按五要素写作。给你一份可直接落笔的骨架（**示例代码**，非仓库内容）：

   ````markdown
   # DRAFT. The Option Type 🟢

   > **What you'll learn:**
   > - What `Option<T>` models and why Rust has no null
   > - `map`/`unwrap_or` combinators as control flow
   > - When to `?`-propagate vs. provide a default

   ```mermaid
   graph LR
       V[一个 T 值] --> S[Some T]
       N[没有值] --> E[None]
       S & E --> M[match / 组合子处理]
   ```

   ```rust
   fn main() {
       let maybe = Some(3);
       println!("{}", maybe.map(|x| x * 2).unwrap_or(0)); // 6
       let none: Option<i32> = None;
       println!("{}", none.map(|x| x * 2).unwrap_or(0)); // 0
   }
   ```

   <details>
   <summary>🏋️ Exercise (click to expand)</summary>

   **Challenge**: 把上面的示例改成 `Result<i32, String>` 版本。

   <details>
   <summary>🔑 Solution</summary>

   ```rust
   fn main() {
       let ok: Result<i32, String> = Ok(3);
       println!("{:?}", ok.map(|x| x * 2)); // Ok(6)
   }
   ```

   </details>
   </details>

   > **Key Takeaways — The Option Type**
   > - `Option` 把"可能无值"编码进类型，调用者被迫处理 `None`
   > - 组合子让常见模式无需展开完整 `match`
   ````

4. 在 `SUMMARY.md` 的 Appendices 节临时加 `- [Draft Section](draft-section.md)`，保存后浏览器侧边栏出现新页。
5. **逐项验收清单**（本讲四个模块各占一项）：
   - [ ] 目标框渲染为带竖线的提示框，条目与正文一一兑现；
   - [ ] mermaid 图渲染为 SVG（而非代码块），切换暗色主题后刷新页面图随之换色；
   - [ ] rust 块带行号与运行按钮，点运行输出 `6` 与 `0`；
   - [ ] 练习双层折叠可依次展开，Solution 代码块可运行。
6. 验收通过后删除草稿文件、`git checkout -- src/SUMMARY.md`，确认 `git status` 干净。

**预期结果**：五要素全部在浏览器中按预期呈现；`git status` 回到干净状态。若你愿意把草稿打磨成对现有章节的改进（如补一张缺失的图、修一处过时示例），它就是 u4-l5 综合实践里真实 PR 的素材。

## 6. 本讲小结

- 本仓库的章节有统一写作范式：**标题难度 emoji（🟢🟡🔴）→ What you'll learn 目标框 → 正文（mermaid 图 + 自包含 rust 块）→ Key Takeaways 收束**；`What you'll learn` 与 `Key Takeaways` 精确覆盖 ch00–ch14 全部 15 个内容章。
- Mermaid 是**两阶段渲染**：构建期 `mdbook-mermaid` 预处理器改写代码块并注入 JS，浏览器期 `mermaid.min.js` + `mermaid-init.js` 把文本编译成 SVG 并适配明暗主题。
- Rust 代码块分三档：普通 rust（行号 + 运行 + 编辑按钮，无 main 自动注入包装）、`rust,ignore`（只着色不可运行，用于依赖外部上下文的代码）、非 rust 块；示例写作的黄金规则是**自包含**。
- 练习分两种形态：章内练习用**嵌套两层 `<details>`**（题目、答案分别折叠），独立练习章 ch15 用**单层 `<details>`** 只折叠答案，题与题以 `---` 分隔。
- 这些要素全部由 `book.toml` 的三处配置支撑（`additional-js`、`[preprocessor.mermaid]`、`[output.html.playground]`），作者只管写 Markdown，机制层零额外负担。

## 7. 下一步学习建议

- **下一讲 u3-l3（三座桥梁：C/C++、C#、Python 路线对比）**：把本讲的"单章范式"放到三本桥梁书之间横向对比，看同一教学目标（如所有权）如何为不同语言背景定制讲法。
- 若你想继续深挖 async-book 本身，**u3-l4（Async Rust 深潜）**会按 Part I→II→III 逐段走读其内容，本讲的 ch02 标本章会被扩展成整本书的地图。
- 建议源码延伸阅读：`async-book/src/ch03-how-poll-works.md`（看 `stateDiagram` 的用法与执行器循环的讲法）、`async-book/src/ch17-capstone-project.md`（看 capstone 项目章如何组织需求），以及用 Grep 自行统计其他六本书的范式一致程度——你会发现 `What you'll learn` 式目标框是全仓库通用的约定。
