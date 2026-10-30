# Typst 是什么：项目定位与语言初印象

## 1. 本讲目标

本讲是整本学习手册的第一篇。读完本讲，你应该能够：

1. 用一两句话说清 Typst 是什么、它的核心卖点有哪些，以及它和 LaTeX 的关系。
2. 说出 `typst/typst` 仓库顶层目录（`crates/`、`docs/`、`tests/`、`tools/` 等）各自的职责。
3. 独立安装 typst CLI，并成功把自己的第一份 `.typ` 文档编译成 PDF 和 PNG。
4. 知道官方文档长在哪里、`docs/` 目录如何组织，为后续阅读源码讲义准备好资料入口。

本讲不要求任何 Rust 知识，也不要求你读过编译原理——我们只建立「地图」，深入源码是后面单元的事。

## 2. 前置知识

- **排版系统（typesetting system）**：把「带结构的纯文本」转换成「精心排版的 PDF / 页面」的工具。你在论文、简历、书籍里看到的精美版面，背后往往是一个排版系统在工作。
- **标记语言（markup language）**：用特殊符号给文本「打标记」来表达结构。例如 Typst 里 `= 引言` 表示一个一级标题，`_强调_` 表示斜体。你不需要用鼠标点按钮，写纯文本就能表达格式。
- **LaTeX**：学术界最流行的老牌排版系统，功能强大但学习曲线陡峭、报错信息出了名地难懂。Typst 的目标就是「和 LaTeX 一样强大，但好学得多」——理解这层对照，就理解了 Typst 存在的意义。
- **CLI（command-line interface，命令行程序）**：在终端里通过命令操作的程序。typst CLI 就是「编译器 + 命令行外壳」，让你在本地把 `.typ` 文件编译成 PDF。
- **crate**：Rust 语言中「包」的单位。Typst 仓库是一个 Cargo workspace，里面装着多个 crate，每个 crate 负责编译器的一部分（本讲只需建立印象，第 u1-l3 讲会详细拆解）。

## 3. 本讲源码地图

本讲涉及的关键文件只有两个，另有一个目录清单供对照：

| 文件 / 目录 | 作用 |
| --- | --- |
| [README.md](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/README.md) | 项目的「门面」：定位说明、功能卖点、官方示例、安装方式、CLI 用法、设计原则，是本讲的主线材料 |
| [docs/README.md](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/README.md) | 官方文档站（typst.app/docs）的源码说明：目录结构与 `cargo docit` 构建命令 |
| `crates/` | 编译器本体，17 个 Rust crate（总装、CLI、语法、求值、排版、导出等），第 u1-l3 讲的主角 |
| `tests/` | 测试套件：含 `suite/`（测试用例）、`ref/`（参考产物）、`src/`（测试框架）等，第 u1-l5 讲的主角 |
| `tools/` | 仓库维护用的辅助脚本 |

> 顶层目录清单以仓库实际文件系统为准；本讲只精读其中 `README.md` 和 `docs/README.md`。

## 4. 核心概念与源码讲解

### 4.1 README 项目介绍与安装说明

#### 4.1.1 概念说明

README 的第一段话给 Typst 下了定义：**Typst 是一个新型的、基于标记（markup-based）的排版系统，设计目标是和 LaTeX 一样强大，同时远比它容易学习和使用**。仓库本身则包含「Typst 编译器 + CLI」，也就是在本地编译 Typst 文档所需的全部东西。

这段定义里有两个关键词值得拆开：

- **基于标记**：常用格式（标题、列表、强调、数学公式）都有内置的轻量写法，比如 `= 标题`、`- 列表项`、`$公式$`。
- **和 LaTeX 一样强大**：不止于标记——Typst 还内置了一套脚本语言（`#` 开头的代码），能定义变量、写函数、做递归计算，把「排版」变成「编程」。

#### 4.1.2 核心流程

使用 Typst 的最短路径（本讲实践的主干）：

```text
安装 CLI  →  编写 hello.typ  →  typst compile hello.typ  →  得到 hello.pdf / hello.png
```

README 的组织顺序也是一样的：定位介绍 → 示例展示 → Installation（安装） → Usage（用法）。

#### 4.1.3 源码精读

**① 项目定位与六大卖点**

[README.md:L23-L31](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/README.md#L23-L31)：这段先给出「markup-based typesetting system」的定义，然后列出六个核心卖点——内置标记语法、灵活的函数、紧密集成的脚本系统、数学排版与参考文献管理、增量编译带来的快速编译、友好的错误信息。这六条几乎每一条都是针对 LaTeX 的痛点给出的答案（LaTeX 恰恰以编译慢、报错难懂著称）。

[README.md:L33-L35](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/README.md#L33-L35)：说明本仓库的内容边界——「编译器及其 CLI」，即在本地编译文档所需的一切；在线协作编辑器（typst.app）不在这个仓库里。

**② 一个浓缩了语言特性的官方示例**

[README.md:L77-L103](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/README.md#L77-L103)：这是 README 里唯一的完整代码示例（斐波那契数列表格）。短短二十几行同时展示了四种语言能力：

| 语法 | 展示的能力 | 对应行 |
| --- | --- | --- |
| `#set page(width: 10cm, height: auto)` | set 规则：配置元素属性 | L78 |
| `= Fibonacci sequence` | 标记语法：`=` 打一级标题 | L81 |
| `$ F_n = round(1 / sqrt(5) phi.alt^n) $` | 数学模式，`phi.alt` 是符号变体修饰 | L86-L87 |
| `#let fib(n) = ...` | 脚本：定义变量与递归函数 | L89-L94 |

[README.md:L46-L52](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/README.md#L46-L52)：README 还专门解释了 set 规则与 show 规则的分工——set 管「最常见的配置」，show 可以「彻底重定义一个元素的外观」。这两个概念是 Typst 样式系统的支柱，第 u3-l7 讲会深入。

**③ 安装方式**

[README.md:L106-L134](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/README.md#L106-L134)：README 列出了五类安装渠道：

1. GitHub [releases 页面](https://github.com/typst/typst/releases/)下载预编译二进制，放进 `PATH`，之后用 `typst update` 自更新；
2. 各平台包管理器：Linux（Repology / Snap）、macOS（`brew install typst`）、Windows（`winget install --id Typst.Typst`）；
3. 有 Rust 工具链时用 [README.md:L122-L126](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/README.md#L122-L126) 的 `cargo install --locked typst-cli`（或加 `--git` 安装开发版）；
4. Nix：`nix-shell -p typst`；
5. Docker：`docker run ghcr.io/typst/typst:latest --help`。

**④ 编译与常用命令**

[README.md:L138-L144](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/README.md#L138-L144)：`typst compile file.typ` 在当前目录生成 `file.pdf`；也可以显式指定输出路径。输出格式由输出文件扩展名推断，CLI 内部定义了 `Png` 等输出格式（见 [crates/typst-cli/src/args.rs:L671-L682](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/args.rs#L671-L682)），所以 `typst compile hello.typ hello.png` 会输出 PNG 图片。

[README.md:L146-L152](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/README.md#L146-L152)：`typst watch file.typ` 监视文件并自动重编译，比每次从头编译快，因为 Typst 有增量编译。

[README.md:L167-L174](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/README.md#L167-L174)：`typst help` 与 `typst help watch` 可以列出全部子命令与某个子命令的详细用法——记不住参数时优先查这里，而不是猜。

**⑤ 三条设计原则**

[README.md:L219-L244](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/README.md#L219-L244)：README 结尾给出了理解整个项目的钥匙——三条设计原则：

- **Simplicity through Consistency（一致性带来简单）**：会一件事就会所有事；同一件事若有多种写法，应处于不同抽象层级（`= Introduction` 只是 `#heading[Introduction]` 的语法糖）；
- **Power through Composability（可组合带来强大）**：少量可自由组合的机制，胜过「每个需求一个旋钮」；
- **Performance through Incrementality（增量带来性能）**：所有语言特性都必须兼容增量编译，繁重工作由 [comemo](https://github.com/typst/comemo/) 在后台完成。

后面每一讲读到具体实现时，都可以回头问一句：这个设计是在服务哪条原则？

#### 4.1.4 代码实践

**实践目标**：亲手完成「安装 → 编写 → 编译」全流程，得到第一份 PDF 和 PNG。

**操作步骤**：

1. 任选 [README.md:L106-L134](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/README.md#L106-L134) 中的一种方式安装（推荐包管理器或 releases 二进制），运行 `typst --version` 确认可用。
2. 新建 `hello.typ`，写入以下内容（示例代码，语法均来自 README 官方示例）：

   ```typst
   #let name = "Typst"      // #let 定义变量

   = Hello, #name!           // = 打一级标题，#name 插入脚本求值结果
   - 第一个列表项            // - 打无序列表
   - 第二个列表项

   勾股定理：$ a^2 + b^2 = c^2 $
   ```

3. 编译 PDF：`typst compile hello.typ`，得到 `hello.pdf`。
4. 编译 PNG：`typst compile hello.typ hello.png`（输出格式由扩展名推断）。
5. 改动一处（比如把 `Typst` 换成你的名字），再运行 `typst watch hello.typ`，观察自动重编译。

**需要观察的现象**：

- `hello.pdf` 与 `hello.png` 出现在工作目录，PNG 可以直接用图片查看器打开；
- 同一份源码，PDF 与 PNG 内容一致，只是格式不同；
- `watch` 模式下保存文件后几乎立刻重新生成产物。

**预期结果**：PDF/PNG 中有一个大标题「Hello, Typst!」、两个圆点列表项，以及一行排版好的数学公式 \( a^2 + b^2 = c^2 \)（字母为数学字体）。整个过程应在数秒内完成。若某步行为与描述不符，请以 `typst help` 的实际输出为准；本实践未在本环境实际运行，编译效果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：README 说 Typst 的设计目标是什么？请用自己的话复述，并指出它针对的「前任」是谁。

**参考答案**：目标是「像 LaTeX 一样强大，但更容易学习和使用」的基于标记的排版系统；它针对的正是 LaTeX：学习曲线陡、编译慢、错误信息不友好这些痛点，对应 Typst 的内置标记、增量编译、友好报错等卖点（[README.md:L23-L31](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/README.md#L23-L31)）。

**练习 2**：`= Introduction` 和 `#heading[Introduction]` 都能生成标题，README 如何解释这种「一件事两种写法」？

**参考答案**：这符合「一致性带来简单」原则——多种写法允许存在，但必须处于不同抽象层级：前者只是后者的语法糖（[README.md:L225-L230](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/README.md#L225-L230)）。

**练习 3**：不加任何参数运行 `typst compile file.typ` 会生成什么文件？想生成 PNG 应该怎么做？

**参考答案**：会在工作目录生成 `file.pdf`（[README.md:L138-L144](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/README.md#L138-L144)）；生成 PNG 可以把输出路径写成 `.png` 扩展名（如 `typst compile file.typ out.png`），CLI 会按扩展名推断输出格式（`OutputFormat::Png` 定义于 [crates/typst-cli/src/args.rs:L671-L673](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/args.rs#L671-L673)）。

### 4.2 docs 文档站结构与阅读入口

#### 4.2.1 概念说明

`docs/` 目录是**官方文档站（typst.app/docs）的源码**。这一点容易误解：它不是「随手写的说明文档」，而是一个完整的文档工程——文档内容本身用 Typst 编写，可以构建成网站，也可以构建成一份独立的 PDF。

对源码学习者来说它还有一个隐藏价值：`docs/dev/` 下有面向开发者的架构文档（第 u1-l3 讲会精读），而 Rust 源码中 `#[elem]`、`#[ty]`、`#[func]` 宏标注的文档注释也会汇入文档站——**文档站和源码是同一份真相的两个视图**。

#### 4.2.2 核心流程

阅读与使用 docs 的路径：

```text
docs/README.md（看目录结构与命令）
  → docs/content/（按 tutorial / reference / guides 分类读内容）
  → cargo docit compile（本地构建网站或 PDF 版文档）
```

#### 4.2.3 源码精读

**① 目录结构**

[docs/README.md:L5-L10](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/README.md#L5-L10)：定义了 docs 的五个子目录职责：

| 子目录 | 职责 |
| --- | --- |
| `src` | 支撑文档基础设施的 Rust 源码 |
| `content` | 文档内容本体（Typst 源码），并与代码库中 `#[elem]`/`#[ty]`/`#[func]` 宏影响的文档注释互补 |
| `components` | 组成文档基础设施的 Typst 源码 |
| `assets` | 会被输出到网站的静态资源 |
| `dev` | 面向代码库的开发者文档 |

其中 `content` 下实际分为 `tutorial`（入门教程）、`reference`（语法与函数参考）、`guides`（指南）、`changelog`（变更日志）等目录——**`docs/content/tutorial/` 就是官方「gentle introduction」教程的源码**，是初学者最好的下一站。

**② 构建命令**

[docs/README.md:L12-L25](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/README.md#L12-L25)：仓库提供了 `cargo docit` 别名来驱动文档基础设施——`cargo docit compile` 构建网站版（产物在 `docs/dist/site`），`cargo docit compile --format pdf` 构建 PDF 版（产物在 `docs/dist/docs.pdf`）。

[docs/README.md:L27-L33](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/README.md#L27-L33)：写文档时可用 `cargo docit watch` 启动热重载模式，同时监听 `docs/` 里的 Typst 源码和 `crates/` 里的 Rust 文档注释，改动即时反映。

#### 4.2.4 代码实践

**实践目标**：不联网也能定位到「教程、语法参考、函数参考」三类资料的源文件，并成功运行 `cargo docit`。

**操作步骤**：

1. 在仓库根目录列出 `docs/content/`，确认存在 `tutorial/`、`reference/`、`guides/` 等子目录。
2. 打开 `docs/content/tutorial/` 下的第一个文件，通读官方入门教程的开头部分（对照 4.1 中学到的语法）。
3. 在仓库根目录运行 `cargo docit compile --format pdf`，构建 PDF 版文档。
4. 运行 `cargo docit help` 查看其余可用选项。

**需要观察的现象**：

- `docs/content/` 的子目录名与 [docs/README.md:L5-L10](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/README.md#L5-L10) 描述的职责一一对应；
- 构建完成后 `docs/dist/docs.pdf` 生成，页数不少——它就是整个官方文档的离线版。

**预期结果**：拿到一份可离线阅读的官方文档 PDF，并知道以后任何语法疑问都可以先查 `docs/content/reference/`。构建耗时取决于机器，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`docs/content` 和 `docs/components` 都装着 Typst 源码，区别是什么？

**参考答案**：`content` 是文档的**内容**（教程、参考、指南正文），`components` 是文档站的**基础设施**（负责把内容渲染成网站/PDF 的那部分代码）——见 [docs/README.md:L7-L8](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/README.md#L7-L8)。

**练习 2**：为什么说「修改 crates/ 里的文档注释会影响文档站」？

**参考答案**：docs 的内容除了 `content` 目录，还由散布在代码库中的文档注释补充——具体是受 `#[elem]`、`#[ty]`、`#[func]` 宏影响的那些注释（[docs/README.md:L7](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/README.md#L7)）；`cargo docit watch` 也同时监听两处（[docs/README.md:L27-L28](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/README.md#L27-L28)）。

## 5. 综合实践

把本讲所有内容串成一个小任务——「**给未来的自己写一份 Typst 速览**」：

1. **编译官方示例**：把 [README.md:L77-L103](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/README.md#L77-L103) 的斐波那契示例原样存为 `fib.typ`，编译成 PDF。
2. **逐行标注**：在 `.typ` 文件里给每行加注释，说明它用到的特性属于哪一类——标记语法 / set 规则 / 数学模式 / 脚本（`#let`、函数定义、展开运算符 `..`）。拿不准的语法去 `docs/content/reference/` 里查证。
3. **改造验证**：把 `#let count = 8` 改成 `count = 10`，重新编译，确认表格多出两列——这验证了「脚本结果参与排版」。
4. **回答一个架构问题**：这个 `.typ` 文本到你手里的 PDF 之间，理论上要经过「解析 → 求值 → 排版 → 导出」几个阶段。你不需要看代码，只需在 `crates/` 目录里找出名字与这些阶段对应的 crate（提示：`typst-syntax`、`typst-eval`、`typst-layout`、`typst-pdf`），列一张表。

**验收标准**：三份产物（fib.pdf、带注释的 fib.typ、crate 对照表）+ 能口头复述 Typst 的定位与三条设计原则。

## 6. 本讲小结

- Typst 是**基于标记的排版系统**，目标是「和 LaTeX 一样强大，但远比它好学好用」；本仓库包含编译器与 CLI，即本地编译文档的全部。
- 六大卖点：内置标记、灵活函数、集成脚本、数学与文献管理、增量编译提速、友好报错。
- 官方示例浓缩了四种语言能力：set 规则、标记语法、数学模式、`#` 脚本——这四条也是后续单元的源码主线。
- 安装后 `typst compile` 出 PDF，输出扩展名决定格式（PNG/SVG 同理）；`typst watch` 利用增量编译实现秒级重编。
- 三条设计原则（一致性、可组合、增量）是阅读整个代码库的「价值观地图」。
- `docs/` 是官方文档站的源码工程，`content/tutorial`、`content/reference` 是初学者资料入口，`cargo docit` 负责本地构建。

## 7. 下一步学习建议

- **语言层面**：通读官方教程源码 `docs/content/tutorial/`，并浏览 `docs/content/reference/` 下的语法与函数参考。
- **下一讲（u1-l2「从源码构建：Cargo workspace 与构建命令」）**：我们将打开根 `Cargo.toml`，看懂 workspace 成员组织，并用 `cargo build -p typst-cli` 从源码构建编译器——为深入源码做好环境准备。
- **提前预告**：第 u1-l3 讲会借助 `docs/dev/architecture.md` 拆解 17 个 crate 的分工地图；如果你好奇「编译器内部长什么样」，可以先去读那份架构文档。
