# 仓库全景：17 个 crate 的分工地图

## 1. 本讲目标

上一讲我们从源码把 Typst 编译了出来，但 `cargo build` 屏幕上滚过的十几个
`Compiling typst-xxx` 可能让你疑惑：为什么一个排版系统要拆成这么多包？

学完本讲，你应该能够：

- 说出 Typst 编译一条源文件到 PDF 的**流水线阶段**，以及每个阶段输入输出的数据形态。
- 画出 `crates/` 下 17 个 crate 之间的**依赖与数据流方向草图**。
- 理解 Typst 最重要的一条架构决策：**「类型定义在 typst-library，行为拆分到 typst-eval / typst-realize / typst-layout 等独立 crate」**，以及支撑这条决策的 `Routines` 函数指针表机制。
- 拿到一个想改的功能（比如「我想改断行算法」「我想加一个内置函数」），能**快速定位应该去哪个 crate**。

## 2. 前置知识

- **crate 与 workspace**：crate 是 Rust 的最小编译单元（一个包），workspace 是多个 crate 共享一个 `Cargo.toml` 和 `target/` 目录的组织方式。上一讲已详细讲过，本讲直接使用。
- **依赖方向**：如果 crate A 的 `Cargo.toml` 里写了依赖 B，就说「A 依赖 B」，箭头从 A 指向 B。Rust 禁止循环依赖：A 依赖 B 时，B 就不能再依赖 A。这个约束逼着大型项目把代码切成清晰的层。
- **函数指针表**：Rust 里函数也是一种值。把若干函数装进一个 struct 字段里传给别人，别人就能在不知道函数定义在哪个模块的情况下调用它。Typst 用这个技巧解决了 crate 拆分带来的循环依赖问题（见 4.4 节）。
- **编译器流水线（pipeline）**：把「源代码变 PDF」这件大事拆成一串小阶段，每个阶段只做一件事、只负责一种数据形态的转换。这是 LaTeX、GCC、LLVM 等编译系统的通用组织方式。

不需要任何排版原理知识，本讲只看「代码放在哪里、为什么放在那里」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [Cargo.toml](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/Cargo.toml) | workspace 清单：成员列表、17 个内部 crate 的集中依赖表 |
| [docs/dev/architecture.md](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/dev/architecture.md) | 官方架构文档：目录职责说明 + 编译四阶段讲解 |
| [crates/typst/src/lib.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs) | 总装 crate：`compile` 入口、`ROUTINES` 函数指针表 |
| [crates/typst/Cargo.toml](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/Cargo.toml) | 总装 crate 的依赖清单：验证「谁在最顶层」 |
| [crates/typst-library/src/routines.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/routines.rs) | `Routines` 结构体的定义：crate 拆分的关键接口 |
| `crates/*/Cargo.toml`（17 个） | 各 crate 的依赖声明，是绘制依赖图的一手材料 |

## 4. 核心概念与源码讲解

### 4.1 architecture.md 的流水线总览

#### 4.1.1 概念说明

Typst 官方在 [docs/dev/architecture.md](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/dev/architecture.md) 里用四个阶段描述「源文件到 PDF」的全过程：

1. **Parsing（解析）**：把源字符串变成语法树。
2. **Evaluation（求值）**：把语法树和它依赖的文件求值成内容（content）。
3. **Layout（排版）**：把内容排版成一页一页的帧（frame）。
4. **Export（导出）**：把帧写成 PDF、SVG、PNG 等输出格式。

为什么分阶段？因为每个阶段的数据形态完全不同（字符串 → 树 → 内容 → 帧 → 字节），
职责单一的阶段更容易测试、更容易做**增量编译**—— Typst 的核心卖点之一。
这份文档明确说明了增量性由 [`comemo`](https://github.com/typst/comemo/) 框架承担，
但编译器本身也处处为增量而写。

#### 4.1.2 核心流程

用数据形态把四个阶段串起来（`→` 上方是转换，下方是产出的 Rust 类型）：

```
 hello.typ          解析                求值                    排版                 导出
(UTF-8 字符串) ─────────→ SyntaxNode ─────────→ Content ─────────→ Frame ─────────→ PDF/SVG/PNG
                     typst-syntax        typst-eval          typst-layout         typst-pdf
                                     （产出 Module：        （先 realize 再        typst-render
                                       Content + Scope）      layout，见 4.4）      typst-svg
```

| 阶段 | 输入 | 输出 | 主要负责的 crate |
| --- | --- | --- | --- |
| Parsing | `&str`（源文本） | `SyntaxNode`（语法树） | typst-syntax |
| Evaluation | `Source`（已解析的源文件） | `Module`（含 `Content` 与 `Scope`） | typst-eval |
| Layout | `Content` | `PagedDocument`（每页一个 `Frame`） | typst-layout（含 typst-realize） |
| Export | `Frame` / `PagedDocument` | PDF / SVG / PNG / HTML 字节 | typst-pdf / typst-svg / typst-render / typst-html |

两个容易踩的坑，先说在前面：

- **文档说的是「四阶段」，代码里 realize（实现/规则应用）是独立的一个 crate**。架构文档把 realize 归入 Layout 阶段内部讲解，但源码层面它是 `typst-realize`。本手册后续按五段「parse → eval → realize → layout → export」来讲，与 crate 一一对应。
- **架构文档可能滞后于代码**。例如文档导出一节写着「HTML 导出尚不存在」，但代码里 `typst-html` crate 早已存在且可用——以源码为准，这也是本手册强调精读源码的原因。

#### 4.1.3 源码精读

架构文档对四个阶段的权威定义：

> The source-to-PDF compilation process of a Typst file proceeds in four phases. 1. **Parsing:** ... 2. **Evaluation:** ... 3. **Layout:** ... 4. **Export:** ...

见 [docs/dev/architecture.md:L36-L49](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/dev/architecture.md#L36-L49)，这段同时说明了编译器是增量的、大部分工作由 comemo 完成。

总装 crate 顶部的文档注释把同样的流程复述了一遍，并给出每步的类型与函数入口，是最好的「一页速查」：

- [crates/typst/src/lib.rs:L1-L31](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L1-L31) —— 依次描述 Parsing（字符串 → 语法树，AST 模块提供类型化层）、Evaluation（求值为 module：Content + 值的作用域）、Layouting（内容排成每页一个 frame 的 `PagedDocument`）、Exporting（frame 导出为 PDF、PNG、SVG、HTML）。注意这里明确写了 **HTML 是已支持的输出**，与架构文档的旧说法形成对照。

解析阶段「永不失败」的容错设计也写在文档里：

- [docs/dev/architecture.md:L52-L59](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/dev/architecture.md#L52-L59) —— Parsing 是纯函数 `&str -> SyntaxNode`，没有任何外部依赖；解析不能失败，语法错误以错误节点的形式留在树里。原因是解析器还要服务语法高亮和 IDE，必须能处理残缺代码。

排版阶段的内省循环（introspection loop）：

- [docs/dev/architecture.md:L133-L139](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/dev/architecture.md#L133-L139) —— 目录、页码这类「依赖自身排版结果」的内容，靠反复重排直到结果稳定来解决；大多数情况一两轮就收敛，永不收敛时五轮后放弃。

#### 4.1.4 代码实践

1. **实践目标**：把流水线数据形态内化为一张随手可画的表。
2. **操作步骤**：
   - 打开 [crates/typst/src/lib.rs:L1-L31](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L1-L31) 和 [docs/dev/architecture.md:L36-L49](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/dev/architecture.md#L36-L49) 对照阅读。
   - 在纸上抄下 4.1.2 的表格，但**遮住输出列和 crate 列**，凭记忆补全。
3. **需要观察的现象**：两份材料描述的同一流程在措辞上的差异（例如 lib.rs 写了 HTML 支持，architecture.md 的 Export 一节没有）。
4. **预期结果**：能不看资料写出「字符串 → SyntaxNode → Module(Content) → PagedDocument(Frame) → 输出字节」以及对应的 crate。

#### 4.1.5 小练习与答案

**练习 1**：架构文档说解析是「纯函数 `&str -> SyntaxNode`」且「解析不能失败」。这对 IDE 场景为什么重要？

**答案**：用户在编辑器里输入到一半的代码几乎总是语法不完整的（比如刚敲下一个 `#`）。如果解析会失败/中断，编辑器的语法高亮和补全就会频繁崩溃；把错误变成树中的错误节点，才能对残缺代码继续提供高亮与分析。见 [docs/dev/architecture.md:L56-L59](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/dev/architecture.md#L56-L59)。

**练习 2**：realize（应用 show 规则）发生在四个阶段的哪一个里？它由哪个 crate 实现？

**答案**：按架构文档的划分属于 Layout 阶段的开头（先 realize 得到可排版元素，再排入 regions）；由独立的 `typst-realize` crate 实现，见 [docs/dev/architecture.md:L118-L131](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/dev/architecture.md#L118-L131)。

### 4.2 crate 清单与职责表

#### 4.2.1 概念说明

上一讲已经确认：根 [Cargo.toml](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/Cargo.toml) 是 workspace 清单，`crates/` 下正好 17 个 crate。这一节回答：**每个 crate 是干什么的？**

职责表的一手来源是架构文档开头的 Directories 一节，它给 16 个 crate 写了一句话职责；另外还有目录 `docs/`（文档站）、`tests/`（集成测试）和 `tools/`（开发工具）。

#### 4.2.2 核心流程

如何自己从 Cargo.toml 验证「17 个」这个数字：

1. 看 [Cargo.toml:L2](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/Cargo.toml#L2)：`members = ["crates/*", "docs", "tests", "tests/fuzz", "tests/wrapper"]`，`crates/*` 是通配符，把 `crates/` 下每个子目录都收进来。
2. 看 [Cargo.toml:L19-L35](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/Cargo.toml#L19-L35)：集中依赖表里 `path = "crates/..."` 开头的条目恰好 17 个，从 `typst` 到 `typst-utils`。
3. 在仓库根目录执行 `ls crates/`，数出来的目录数也是 17。

职责表（中文意译自架构文档 Directories 一节，并补充依赖信息）：

| crate | 职责 | 按流水线分组 |
| --- | --- | --- |
| `typst-syntax` | 解析器与语法树定义的所在地 | Parsing |
| `typst-library` | 标准库（也是所有核心类型与 `World` 抽象的家，见 4.4） | 共享基础 |
| `typst-eval` | Typst 语言的解释器 | Evaluation |
| `typst-realize` | 实现子系统（应用 show 规则） | Layout 前半 |
| `typst-layout` | 排版引擎 | Layout 后半 |
| `typst-pdf` | PDF 导出器 | Export |
| `typst-svg` | SVG 导出器 | Export |
| `typst-render` | 把帧渲染成像素（PNG） | Export |
| `typst-html` | HTML 导出器 | Export |
| `typst-bundle` | 实验性 bundle 导出格式（架构文档尚未收录，见下方说明） | Export |
| `typst` | 主编译器 crate，定义完整语言与库（总装层） | 总装 |
| `typst-cli` | 命令行界面，编译器与导出器之上的薄层 | 应用 |
| `typst-ide` | IDE 功能（补全、跳转、悬停） | 应用 |
| `typst-kit` | 供 `typst-cli` 使用的各种默认实现（字体发现、下载器等） | 应用 |
| `typst-macros` | 编译器使用的过程宏 | 基础设施 |
| `typst-timing` | 性能计时 | 基础设施 |
| `typst-utils` | 通用工具集 | 基础设施 |

**关于 `typst-bundle`**：`crates/typst-bundle/` 存在、也出现在 workspace 依赖表中，但架构文档的 Directories 一节没有列它——它是较新的实验性导出格式（总装代码里有 `Target::Bundle` 分支与之呼应，见 4.4.3）。文档滞后于代码的第二个例子。详细讲解安排在第五单元 u5-l4。

#### 4.2.3 源码精读

- [Cargo.toml:L1-L4](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/Cargo.toml#L1-L4) —— workspace 声明：`members` 用 `crates/*` 通配收编全部编译器 crate（外加 docs、tests 等非编译器包）；`default-members = ["crates/typst-cli"]` 解释了上一讲的观察：在仓库根目录裸跑 `cargo build` 只构建 CLI。
- [Cargo.toml:L19-L37](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/Cargo.toml#L19-L37) —— 集中依赖表：L19–L35 是 17 个仓库内 crate 的 path 依赖；紧接着的 L36–L37 是 `typst-assets` 和 `typst-dev-assets`，它们名字以 typst 开头但**不是**仓库内 crate，而是锁定 rev 的 git 依赖（上一讲讲过）。读这张表时注意区分。
- [docs/dev/architecture.md:L7-L33](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/dev/architecture.md#L7-L33) —— Directories 一节，职责表的一手来源，逐条给出各 crate 一句话职责。
- [docs/dev/architecture.md:L147-L156](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/dev/architecture.md#L147-L156) —— Export 一节，末尾「HTML export does not exist yet」与源码现状不符，可作为「以代码为准」的例证。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证 crate 数量与职责表，不靠背诵。
2. **操作步骤**：
   - 执行 `ls crates/ | wc -l`，应得到 17。
   - 打开根 [Cargo.toml:L19-L35](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/Cargo.toml#L19-L35)，逐个数 `path = "crates/...` 的条目。
   - 对照 4.2.2 的表格和架构文档 Directories 一节，找出「文档没列出的 crate」。
3. **需要观察的现象**：`ls` 结果与依赖表条目一一对应；`typst-bundle` 在依赖表里有、在架构文档里没有。
4. **预期结果**：17 个目录 = 17 个 path 依赖条目；`typst-bundle` 是文档缺口。

#### 4.2.5 小练习与答案

**练习 1**：`typst-assets` 和 `typst-dev-assets` 也出现在 `[workspace.dependencies]` 里，它们算仓库内 crate 吗？怎么判断？

**答案**：不算。判断依据是依赖来源：仓库内 crate 用 `path = "crates/..."`，而这两个用 `git = "..."` 且锁定了 rev，见 [Cargo.toml:L36-L37](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/Cargo.toml#L36-L37)。

**练习 2**：我想给 Typst 加一个新的内置函数（比如 `uppercase2`），应该去哪个 crate 改？想改断行算法呢？

**答案**：内置函数属于标准库，去 `typst-library`（函数的行为定义处，具体到 foundations 或对应模块；u3-l2、u3-l3 详讲）。断行属于行内排版，去 `typst-layout` 的 inline 相关代码（u4-l5 详讲）。定位方法就是本讲的职责表。

**练习 3**：`typst-cli` 的描述是「编译器与导出器之上的一个相对小的层」。从 workspace 哪一行能看出它是整个 workspace 的默认构建目标？

**答案**：[Cargo.toml:L3](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/Cargo.toml#L3)：`default-members = ["crates/typst-cli"]`。

### 4.3 依赖图与数据流方向

#### 4.3.1 概念说明

有职责表还不够——真正决定你「去哪里找代码」的是 **crate 之间的依赖方向**。
一条好用的经验法则：**依赖箭头大致与数据流方向一致**，被依赖最多的 crate
（`typst-library`）是全仓库的地基，依赖别人最多的 crate（`typst-cli`）在最顶层。

依赖关系的证据不在任何文档里，而在每个 crate 自己的 `Cargo.toml` 的
`[dependencies]` 段中。本节的所有箭头都可以用「打开对应 Cargo.toml 数依赖」复现。

#### 4.3.2 核心流程

把 17 个 crate 按「被依赖程度」分层，箭头表示「依赖于」（自下而上依赖）：

```
        应用层    typst-cli ── typst-ide ── typst-kit
                    │    （三者都依赖 ↓）
        总装层            typst
                    │   （把各阶段 crate 组装起来，定义 compile 入口）
        导出层   typst-pdf   typst-svg   typst-render   typst-html   typst-bundle
                    │           │            │             │            │
        阶段层   typst-eval   typst-realize   typst-layout
                    │           │               │
        类型层          typst-library
                          │  （核心类型：Value、Content、World、Library…）
        解析层          typst-syntax
                          │
        基础层     typst-utils    typst-timing    typst-macros
```

读图要点：

- `typst`（总装）依赖各阶段 crate 但**不被任何阶段 crate 依赖**；`typst-cli`、`typst-ide`、`typst-kit` 依赖 `typst`，站在最顶层。
- `typst-library` 不依赖任何阶段 crate（eval/realize/layout），却定义了它们要用的全部类型——这就是 4.4 节要解的谜题：下层如何调用上层的行为而不形成循环依赖。
- 分层不绝对：`typst-realize` 依赖 `typst-html`，`typst-html` 依赖 `typst-svg`，`typst-pdf`/`typst-render` 依赖 `typst-layout`。图中省略了这些横向边，动手画图时建议补上。

#### 4.3.3 源码精读

- [crates/typst/Cargo.toml:L15-L24](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/Cargo.toml#L15-L24) —— 总装 crate 的正式依赖：`typst-eval`、`typst-html`、`typst-layout`、`typst-library`、`typst-macros`、`typst-realize`、`typst-syntax`、`typst-timing`、`typst-utils`。这条依赖清单就是「总装层」的定义。
- [crates/typst/Cargo.toml:L30-L34](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/Cargo.toml#L30-L34) —— 有意思的细节：`typst-bundle`、`typst-pdf`、`typst-render`、`typst-svg` 只出现在 `[dev-dependencies]`（测试依赖）里。也就是说**总装 crate 的正式依赖不含任何导出器**：`compile` 是泛型的，导出器由使用者（CLI、IDE）自己接线。这是 Typst 让库使用者按需选择输出格式、控制编译体积的手段。
- [crates/typst-library/Cargo.toml](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/Cargo.toml) —— `typst-library` 的内部依赖只有 `typst-syntax`、`typst-macros`、`typst-utils`、`typst-timing`（以及外部 git 依赖 `typst-assets`）：地基不依赖楼层。
- `crates/typst-cli/Cargo.toml` —— CLI 依赖 `typst`、`typst-pdf`、`typst-html`、`typst-render`、`typst-svg`、`typst-bundle`、`typst-eval`、`typst-layout`、`typst-macros`、`typst-timing`、`typst-utils`，几乎把全仓库都搬了进来：顶层应用要用到所有能力。

#### 4.3.4 代码实践

1. **实践目标**：不依赖本讲示意图，自己从 Cargo.toml 复现依赖图。
2. **操作步骤**：
   - 对每个 `crates/*/Cargo.toml`，提取 `[dependencies]` 段中以 `typst` 开头且 `path` 指向本仓库的条目（忽略 `typst-assets`/`typst-dev-assets`）。一条可用的命令：
     ```bash
     grep -H '^typst[a-z-]* = ' crates/*/Cargo.toml | grep 'workspace = true'
     ```
   - 把输出整理成「crate → 它依赖的 crate 列表」，再按「谁不被别人依赖」到「谁依赖最多」排序，画出分层图。
3. **需要观察的现象**：`typst-utils`、`typst-timing`、`typst-macros` 不出现在任何依赖列表的「依赖者」一侧（即没有 crate 依赖它们……不对，几乎所有 crate 都依赖它们）；正确观察点是：它们自己不依赖任何仓库内 crate，处于最底层。
4. **预期结果**：得到与 4.3.2 一致的分层，并能发现示意图省略的横向边（如 `typst-realize → typst-html`）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `typst-cli` 而不是 `typst` 处于依赖图最顶层？

**答案**：`typst` 是总装库，还要被 `typst-ide`、`typst-kit`、`typst-cli` 复用，所以它不能去依赖具体导出器（导出器只是它的 dev-dependency，见 [crates/typst/Cargo.toml:L30-L34](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/Cargo.toml#L30-L34)）；`typst-cli` 是最终的可执行程序，不打算被别人依赖，所以可以把所有 crate 都拉进来。

**练习 2**：判断对错：「`typst-layout` 可以直接调用 `typst-eval` 里定义的函数」。

**答案**：错。`typst-layout` 的依赖里没有 `typst-eval`（见 4.3.3 第三条与它自己的 Cargo.toml）。跨阶段的行为调用走 `Routines` 函数指针表，由总装 crate 注入，见 4.4。

### 4.4 typst 总装 crate：compile 入口与 crate 拆分机制

#### 4.4.1 概念说明

现在解 4.3 留下的谜题：**「类型在 typst-library、行为拆分到各阶段 crate」**。

设想没有拆分的世界：`typst-layout` 排版时遇到一个用户闭包（show 规则里写的函数），
需要调用 `typst-eval` 去求值；但 `typst-eval` 求值时又要构造 heading、list 这些
元素——它们的行为又定义在 `typst-layout`。直接互相 `use`，Cargo 会立刻报循环依赖。

Typst 的解法分两半：

1. **类型下沉**：把 `Value`、`Content`、`World`、`Library`、`Frame` 等所有阶段都要用的类型，全部放进 `typst-library`。大家只依赖它，类型上不再有环。
2. **行为上提**：跨阶段的行为（求值字符串、调用闭包、realize、布局单帧）以**函数指针**的形式定义在 `typst-library` 的 `Routines` 结构里；真正的实现写在各阶段 crate，由总装 crate `typst` 在 `ROUTINES` 静态表里「接线」。源码注释原话：*This is essentially dynamic linking and done to allow for crate splitting*（本质上是动态链接，用于实现 crate 拆分）。

于是 `typst` 这个 crate 的角色就清楚了：它是**总装车间**——把散落在各阶段 crate 的
函数装配成一张 `Routines` 表，并提供对外的 `compile` 编译入口。

#### 4.4.2 核心流程

`typst::compile` 的主干（`T` 是输出类型，如 `PagedDocument` 或 `HtmlDocument`）：

```
compile::<T>(world)
  └─ compile_impl(world, traced, sink)
       ├─ 1. 检查目标特性开关（Html / Bundle 需要 feature）
       ├─ 2. world.main() 取主文件，world.source(main) 读源码
       ├─ 3. typst_eval::eval(...) 求值 → 取出 content
       └─ 4. loop：
             ├─ 组装 Engine（library + world + introspector + sink…）
             ├─ T::create(&mut engine, &content, styles)   ← realize + layout 在这里发生
             ├─ 内省结果稳定？→ 是：break
             └─ 轮数超过 MAX_ITERS → 放弃（发出警告）
```

注意求值只做一次，**循环的是布局**（内省机制，对应 4.1 里的 introspection loop，
第四单元 u4-l7 详讲）。`T::create` 里的 `T` 决定分叉到 paged 排版还是 HTML 输出。

`Routines` 表的装配（总装 crate 启动时执行一次）：

```
static ROUTINES = Routines {
    rules:      NativeRuleMap + typst_layout::register + typst_html::register,
    eval_string:   typst_eval::eval_string,      // 行为来自 typst-eval
    eval_closure:  typst_eval::eval_closure,
    realize:       typst_realize::realize,       // 行为来自 typst-realize
    layout_frame:  typst_layout::layout_frame,   // 行为来自 typst-layout
    html_mathml_body / html_span_filled: typst_html::…,
}
```

#### 4.4.3 源码精读

- [crates/typst/src/lib.rs:L64-L83](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L64-L83) —— `compile` 的公开入口：接受一个 `&dyn World`（宿主提供的文件、字体等环境，u3-l1 详讲），返回 `Warned<SourceResult<T>>`（带警告列表的编译结果）。`T: Output` 泛型决定输出目标；文档注释明确支持 `PagedDocument`（typst_layout）与 `HtmlDocument`（typst_html）。
- [crates/typst/src/lib.rs:L100-L132](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L100-L132) —— `compile_impl` 前半：按目标检查特性开关（`Target::Html`、`Target::Bundle` 各有 warn-or-error 分支，证明 bundle 目标真实存在）；取主文件；调用 `typst_eval::eval` 把主源文件求值成模块并取出 content。**这一行就是 Evaluation 阶段在总装层的接缝**。
- [crates/typst/src/lib.rs:L134-L186](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L134-L186) —— 内省循环：反复用上一轮文档的内省器构造 `Engine`，调 `T::create` 重新排版，直到 `constraint.validate(...)` 判定稳定；`history` 装满（`MAX_ITERS` 上限）仍未稳定则调用 `introspection::analyze` 诊断并放弃。架构文档说的「五次尝试后放弃」就落在这里。
- [crates/typst/src/lib.rs:L325-L342](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L325-L342) —— **本讲最重要的代码**：`ROUTINES` 静态表。字段 `rules` 由 `typst_layout::register` 与 `typst_html::register` 填充（内置 show 规则注册）；`eval_string`/`eval_closure` 指向 `typst_eval`；`realize` 指向 `typst_realize`；`layout_frame` 指向 `typst_layout`。结构体定义处的注释直言这是「为了 crate 拆分而做的动态链接」。
- [crates/typst-library/src/routines.rs:L21-L49](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/routines.rs#L21-L49) —— `Routines` 的定义处（由 `routines!` 宏展开）：一个装满 `fn(...) -> ...` 函数指针字段的结构体。它住在 `typst-library` 而不是 `typst`，正是因为下层 crate（layout 等）需要引用这些函数的**签名类型**，却不必依赖实现它们的 crate。
- [crates/typst-library/src/routines.rs:L51-L112](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/routines.rs#L51-L112) —— `routines!` 的实例化：逐条声明 `rules`、`eval_string`、`eval_closure`、`realize`、`layout_frame`、`html_mathml_body`、`html_span_filled` 的完整函数签名。这张表就是「跨 crate 行为调用」的全部清单。
- [crates/typst/src/lib.rs:L289-L323](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L289-L323) —— `LibraryExt` trait：`Library::builder(formats)` 从 `ROUTINES` 出发构建标准库，文档注释里的示例展示了使用方要传入 `typst_html::FORMAT`、`typst_pdf::FORMAT` 等格式集合——再次体现「导出器由使用者接线」的设计。

#### 4.4.4 代码实践

1. **实践目标**：验证一个 crate 可以脱离整个 workspace 单独测试——这是 crate 拆分的直接好处。
2. **操作步骤**：
   - 在仓库根目录执行：
     ```bash
     cargo test -p typst-syntax --lib
     ```
   - `-p typst-syntax` 指定只测这个 crate，`--lib` 只跑库目标内的单元测试（不跑集成测试）。
   - 观察输出里被编译的包列表和测试结果。
3. **需要观察的现象**：只编译 `typst-utils`、`typst-timing`（可选特性启用时）和 `typst-syntax` 本身等少数几个 crate，**不会**编译 `typst-layout`、`typst-cli` 等；测试集中在 `kind.rs`、`span.rs`、`reparser.rs` 等文件的 `#[cfg(test)]` 模块里（可用 `grep -n '#\[cfg(test)\]' crates/typst-syntax/src/*.rs` 预先确认位置）。
4. **预期结果**：测试全部通过；相比全仓库构建，这次只动了最底层的几个包，耗时明显更短。具体测试数量与名称因版本而异，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`typst-library` 里的 `Routines` 结构体只有函数指针的「签名」，实现填在 `typst` crate 的 `ROUTINES` 里。为什么不干脆让 `typst-layout` 直接依赖 `typst-eval` 调用求值函数？

**答案**：那样会形成 `typst-layout → typst-eval → typst-library → …` 加上反向边的依赖环（Rust 禁止循环依赖），或者迫使 `typst-library` 反过来依赖行为 crate，地基不再是地基。函数指针表把「类型/签名」与「实现」解耦：签名放在最底层，实现由总装 crate 注入。见 [crates/typst-library/src/routines.rs:L27-L31](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/routines.rs#L27-L31) 与 [crates/typst/src/lib.rs:L325-L342](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L325-L342)。

**练习 2**：`compile_impl` 里求值（eval）在内省循环之前只执行一次，循环体内反复执行的是什么？为什么？

**答案**：循环体内反复执行 `T::create(&mut engine, &content, styles)`（realize + layout）。因为页码、目录等内省信息依赖排版结果本身，只能「排版 → 收集位置信息 → 再排版」迭代到稳定，见 [crates/typst/src/lib.rs:L137-L159](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L137-L159) 的注释与判定逻辑。

**练习 3**：看 `ROUTINES` 表的 `rules` 字段初始化，说出内置 show 规则来自哪两个 crate。

**答案**：`typst-layout` 与 `typst-html`，见 [crates/typst/src/lib.rs:L330-L335](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L330-L335)（`typst_layout::register(&mut rules)` 与 `typst_html::register(&mut rules)`）。

## 5. 综合实践

**任务：制作你自己的「Typst crate 地图」并完成三个定位挑战。**

1. **画依赖图**：按 4.3.4 的步骤，用 `grep` 提取全部 17 个 crate 的内部依赖，在纸上或任意画图工具里画出分层依赖图（建议自下而上：基础层 → 解析层 → 类型层 → 阶段层 → 导出层 → 总装层 → 应用层），补全 4.3.2 省略的横向边（至少包括 `typst-realize → typst-html`、`typst-pdf → typst-layout`）。
2. **跑单 crate 测试**：执行 `cargo test -p typst-syntax --lib` 和 `cargo test -p typst-utils --lib`，确认底层 crate 可独立测试，记录两者的测试数量。
3. **定位挑战**（不查本讲答案，只用依赖图 + 职责表 + 架构文档）：
   - 挑战 A：「我想修改 `#set text(font: ...)` 的默认字体处理」→ 去 `crates/` 下哪个子目录找代码？
   - 挑战 B：「我想给 CLI 加一个 `typst frog` 子命令（把文档倒过来排版）」→ 改哪些 crate？
   - 挑战 C：「PDF 里嵌入的字体子集出了 bug」→ 去哪修？
4. **对照验证**：把你的答案与架构文档 Directories 一节的描述比对，检查是否一致。

预期结果：A → `typst-library`（text 相关元素与样式定义在标准库）；B → 主要改 `typst-cli`（参数与子命令），倒排的实现在 `typst-layout`/`typst-render` 之间视做法而定；C → `typst-pdf`。定位依据全部来自本讲的依赖图与职责表，无需通读代码。

## 6. 本讲小结

- Typst 编译按「**解析 → 求值 →（实现）→ 排版 → 导出**」流水线组织，数据形态依次是：字符串 → `SyntaxNode` → `Module`/`Content` → `Frame`/`PagedDocument` → 输出字节；架构文档用四阶段表述，realize 在源码中是独立 crate。
- workspace 共 **17 个 crate**：`crates/*` 通配纳入、集中依赖表 [Cargo.toml:L19-L35](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/Cargo.toml#L19-L35) 一一对应；`typst-bundle` 存在于代码但暂未写进架构文档——**文档可能滞后，以源码为准**。
- 依赖方向自下而上：基础（utils/timing/macros）→ 语法→ 标准库类型 → 阶段（eval/realize/layout）→ 导出→ 总装（`typst`）→ 应用（cli/ide/kit）；总装 crate 的正式依赖不含导出器（它们只在 dev-dependencies 里）。
- 核心架构决策「**类型在 typst-library、行为拆分到各阶段 crate**」由 `Routines` 函数指针表实现：签名定义在 [crates/typst-library/src/routines.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/routines.rs)，实现在 [crates/typst/src/lib.rs:L325-L342](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L325-L342) 的 `ROUTINES` 表里装配——「本质上是动态链接，用于 crate 拆分」。
- `compile` 入口先求值一次，再循环「排版 + 内省收敛判定」，最多 `MAX_ITERS` 轮；`T: Output` 泛型在同一入口处分叉出 paged 与 HTML 目标。
- crate 拆分让底层 crate（如 `typst-syntax`）可以脱离全仓库独立构建与测试：`cargo test -p typst-syntax --lib`。

## 7. 下一步学习建议

下一讲（u1-l4《CLI 入口：从命令行到一次编译的调用链》）把镜头对准依赖图最顶层的 `typst-cli`：精读 `main.rs` 的模块划分与 `compile.rs` 的编译主干，完整走一遍「命令行参数 → 组装 World → 调用本讲的 `typst::compile` → 写出文件」的调用链。

继续阅读建议：

- 通读一遍 [docs/dev/architecture.md](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/dev/architecture.md) 的 Evaluation / Layout / IDE 三节（本讲只精读了总览和 Parsing/Export），它们是第二、三、四单元的预告片。
- 想提前感受「总装」的接线方式，可以打开 [crates/typst/src/lib.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs) 从上往下读一遍 `compile` 与 `ROUTINES`。
- 对增量编译好奇的读者，可以顺势浏览 [comemo](https://github.com/typst/comemo/) 仓库的 README（第五单元 u5-l8 会系统讲解）。
