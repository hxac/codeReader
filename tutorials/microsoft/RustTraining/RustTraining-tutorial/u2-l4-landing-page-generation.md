# write_landing_page：统一落地页生成

## 1. 本讲目标

上一讲（u2-l3）我们拆开了 `build_to` 这个构建引擎，但刻意留下了一个黑盒：它在构建完所有书之后调用的 `write_landing_page(&out)`。本讲就把这个黑盒拆开。学完本讲，你应该能够：

1. 说出 `BOOKS` 常量的元组结构，以及它如何同时充当「构建清单」和「落地页数据源」这两个角色。
2. 读懂 `category_label` 的类别映射，以及类别字符串如何一路传导到 CSS 类名和颜色变量。
3. 读懂 `write_landing_page` 用 `format!` + iterator 链把数据拼成 HTML 的完整流程，包括 `r##"…"##` 原始字符串和 `{{ }}` 花括号转义这两个实战技巧。
4. 能通过修改 `BOOKS` 或内嵌模板改变落地页，并在本地重新构建验证。

## 2. 前置知识

本讲会用到几个 Rust 与 Web 的基础概念，先用通俗语言过一遍：

- **`format!` 宏与占位符**：`format!("你好，{name}")` 会把 `{name}` 替换成变量的值，返回一个 `String`。它是在 Rust 里做字符串拼接最常用的方式。注意：如果模板里要输出**字面的**花括号 `{` 或 `}`，必须写成 `{{` 和 `}}`，否则 `format!` 会把它当成占位符而报错——这一点在本讲的 CSS 模板里会大量出现。

- **原始字符串（raw string）**：普通字符串里的双引号要写 `\"`，很麻烦。`r#"…"#` 表示「原始字符串」：引号、反斜杠都不需要转义，字符串在遇到 `"#` 这个序列时才结束。如果内容里可能出现 `"#`，还可以升级为 `r##"…"##`（两个井号），只有 `"##` 才会结束字符串。

- **iterator 适配器链**：`iter()` 逐个取出元素，`map(|x| …)` 对每个元素做变换，`collect::<Vec<_>>()` 把结果收进 `Vec`，`.join("\n")` 把字符串序列用换行符连成一个大字符串。这一条链就是本讲生成 HTML 卡片的核心手法。

- **CSS 自定义属性（CSS 变量）**：CSS 里可以定义变量，例如 `:root { --clr-bridge: #4ade80; }`，之后用 `var(--clr-bridge)` 引用。它的一个强大用法是「局部覆盖」：某个元素上定义 `--stripe: var(--clr-bridge)`，它的子元素再用 `var(--stripe)` 就能取到这个颜色——本讲的落地页正是靠这个机制让不同类别的卡片呈现不同颜色。

- **元组解构**：`for &(slug, _, _, _) in BOOKS` 表示每轮循环把四元组拆开，只绑定第一个字段，其余用 `_` 忽略。这是 Rust 处理固定结构数据的惯用写法。

如果你对 `build_to` 的整体流程（遍历 `BOOKS`、逐书调 mdbook、输出到 `site/` 或 `docs/`）已经陌生，建议先回看 u2-l3。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [xtask/src/main.rs:8-52](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L8-L52) | `BOOKS` 常量：七本书的 (slug, title, description, category) 注册表，本讲的数据源 |
| [xtask/src/main.rs:170-179](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L170-L179) | `category_label`：把类别 slug 翻译成给人看的标签 |
| [xtask/src/main.rs:181-311](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L181-L311) | `write_landing_page`：本讲主角，生成站点根目录的 `index.html` |
| [xtask/src/main.rs:139-167](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L139-L167) | `build_to` 的书籍循环与收尾，是 `write_landing_page` 的唯一调用方 |
| [README.md:39-55](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L39-L55) | README 中的五级分类表和书籍清单——供人读的「另一份数据源」，可与 `BOOKS` 对照 |

本讲只涉及一个源码文件（`xtask/src/main.rs`）加一个文档文件（`README.md`），范围非常集中。

## 4. 核心概念与源码讲解

### 4.1 BOOKS 数据表：单一数据源的两个消费者

#### 4.1.1 概念说明

`BOOKS` 是一个编译期常量：一个由四元组组成的切片，每个四元组描述一本书。它在 u1-l2 中被介绍为「磁盘目录与构建系统之间的注册契约」，本讲从**数据结构**和**消费者**的角度再深入一层：

```rust
/// (slug, title, description, category)
const BOOKS: &[(&str, &str, &str, &str)] = &[ /* 七个条目 */ ];
```

四个字段的职责分别是：

| 字段 | 类型 | 落地页中的用途 | 构建循环中的用途 |
|------|------|----------------|------------------|
| `slug` | `&str` | 卡片链接 `href="{slug}/"` | 磁盘目录名 + 输出子目录名 |
| `title` | `&str` | 卡片标题 `<h2>{title}</h2>` | 被解构为 `_` 忽略 |
| `description` | `&str` | 卡片描述 `<p>{desc}</p>` | 被解构为 `_` 忽略 |
| `category` | `&str` | CSS 类名 `cat-{cat}` + 标签映射 | 被解构为 `_` 忽略 |

关键点在于：**同一个 `BOOKS` 被两处代码以不同的方式解构消费**。构建循环只关心 slug，落地页生成则四个字段全用。这就是「单一数据源」（single source of truth）的价值——改一处常量，构建清单和落地页同时更新，永远不会出现「构建了八本书但落地页只列了七本」的漂移（前提是书都真实存在，见 4.3.4 的边界情况）。

#### 4.1.2 核心流程

`BOOKS` 在一次 `cargo xtask build` 中的生命周期：

```text
cargo xtask build
  └─ cmd_build
       └─ build_to("site")
            ├─ 消费者 1：for &(slug, _, _, _) in BOOKS
            │    └─ 逐书：mdbook build --dest-dir site/<slug>
            │         （目录不存在 → 打印 "✗ <slug>/ not found, skipping" 并跳过）
            ├─ 消费者 2：write_landing_page(&out)
            │    └─ BOOKS.iter() → 每本书生成一张卡片 → 拼成 index.html
            └─ 写入零字节 .nojekyll
```

注意两个消费者对「目录缺失」的防御是不对称的：

- **消费者 1（构建循环）会检查** `book_dir.is_dir()`，缺目录就跳过并告警；
- **消费者 2（落地页）不检查**，它纯粹根据 `BOOKS` 生成卡片——即使某个 slug 的目录根本不存在，卡片照发，点进去是 404。

这个不对称是本讲综合实践要亲手验证的重点。

#### 4.1.3 源码精读

BOOKS 常量的完整定义，注释 `/// (slug, title, description, category)` 说明了四元组每个位置的语义：

[xtask/src/main.rs:8-27](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L8-L27) 定义了 `BOOKS` 的类型（`&[(&str, &str, &str, &str)]`）和前三本书——三本 bridge 级的桥梁书（C/C++、C#、Python），注意每个条目第四个字段都是 `"bridge"`。

[xtask/src/main.rs:28-52](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L28-L52) 是后四本：async-book（deep-dive）、rust-patterns-book（advanced）、type-driven-correctness-book（expert）、engineering-book（practices）。到此五个类别各就各位。

消费者 1 的解构方式——只取 slug，其余用 `_` 丢弃：

[xtask/src/main.rs:140-145](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L140-L145) 遍历 `BOOKS` 时用 `for &(slug, _, _, _)` 解构，只绑定 slug；随后检查 `root.join(slug)` 是否是目录，不存在则打印 `✗ {slug}/ not found, skipping` 并 `continue`——这就是「目录缺失则跳过」的分支。

消费者 2 的调用时机：

[xtask/src/main.rs:161-167](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L161-L167) 打印 `ok/总数` 统计后**无条件**调用 `write_landing_page(&out)`（`&PathBuf` 自动解引用成函数签名要求的 `&Path`），再写 `.nojekyll`。即使七本书全部构建失败，落地页也照样生成。

顺带一个与 README 的对照：`BOOKS` 里 async-book 的 title 是 `"Async Rust: From Futures to Production"`，而 [README.md:52](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L52) 表格里写的是 `Async Rust`；engineering-book 的描述在 `BOOKS` 里是 `coverage, CI/CD`，README 里是 `CI/CD, Miri`。这正是 u1-l1 提过的「书籍元数据双源维护、已现字面漂移」——落地页渲染的是 `BOOKS`，不是 README。

#### 4.1.4 代码实践：找出落地页与 README 的数据漂移

这是一个纯阅读/比对型实践，不改任何代码。

1. **实践目标**：确认落地页的数据来自 `BOOKS` 而非 README，并体会双源维护的漂移风险。
2. **操作步骤**：
   1. 如果你还没构建过，先运行 `cargo xtask build`（需要 mdbook，安装方式见 u1-l3）。
   2. 打开生成的 `site/index.html`，找到 async-book 那张卡片，看它的 `<h2>` 标题和 `<p>` 描述。
   3. 对照 `xtask/src/main.rs` 第 29-31 行的元组与 [README.md:47-55](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L47-L55) 的表格，逐书比对标题与描述。
3. **需要观察的现象**：`site/index.html` 中卡片的文案与 `BOOKS` 逐字一致，与 README 表格存在至少两处措辞差异。
4. **预期结果**：你能列出至少两处「同一本书在两个数据源里描述不一致」的地方（上面已给出两处）。这解释了为什么给书改副标题时必须记得同时改 README 和 `BOOKS`——只有后者会影响线上落地页。
5. 具体渲染效果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `BOOKS` 里某个条目的 slug 改成一个磁盘上不存在的名字（假设其他都不变），`cargo xtask build` 会失败吗？退出码是多少？

**答案**：构建不会失败，进程退出码仍是 0。构建循环会走 `!book_dir.is_dir()` 分支，打印 `✗ <slug>/ not found, skipping` 并 `continue`；统计行变成类似 `7/8 books built`（ok 计数不增加，但分母是 `BOOKS.len()`）。没有任何代码路径因目录缺失而返回非零退出码。

**练习 2**：`BOOKS` 的类型是 `&[(&str, &str, &str, &str)]`，为什么要用元组数组而不是定义一个 `struct Book { slug: &str, … }` 的数组？

**答案**：对只有 4 个字段、纯静态、只在本文件内消费的数据来说，元组 + 一行文档注释（`/// (slug, title, description, category)`）是最小成本方案：不需要额外定义类型，解构（`&(slug, _, _, _)`）也最简洁。代价是字段只有位置语义、可读性依赖注释，且位置写反了（比如 title 和 description 对调）编译器不会报错——如果数据规模或消费者继续增长，升级成具名结构体才是更稳妥的选择。这是「数据规模决定抽象层级」的典型取舍。

### 4.2 category_label 映射：从机器 slug 到人类标签

#### 4.2.1 概念说明

`BOOKS` 里的 category 是给机器用的短横线 slug（`"deep-dive"`），但落地页上读者看到的应该是漂亮的标题（`Deep Dive`）。`category_label` 就是这中间的翻译层——一个极简的「映射函数」：

- 输入：`BOOKS` 第四个字段，如 `"bridge"`。
- 输出：用于展示的标签文本，如 `"Bridge"`。
- 兜底：`_ => cat`——未识别的类别**原样返回**，不报错。

这个「宽松兜底」的设计意味着：新增一个类别时，即使忘了更新 `category_label`，页面也不会崩，只是显示原始 slug（`intro` 而不是 `Intro`）。宽容但可被发现——这是处理展示类映射的常见工程取舍。

#### 4.2.2 核心流程

```text
BOOKS 条目 (slug, title, desc, cat)
        │
        ▼
category_label(cat)
        │  "bridge"     → "Bridge"
        │  "deep-dive"  → "Deep Dive"
        │  "advanced"   → "Advanced"
        │  "expert"     → "Expert"
        │  "practices"  → "Practices"
        │  其他          → 原样返回 cat
        ▼
卡片 <span class="label">{label}</span> 中的药丸标签文本
```

要注意类别信息其实有**两条独立通路**进入落地页：一条经 `category_label` 变成标签**文本**，另一条直接拼进 `class="card cat-{cat}"` 变成 CSS **类名**（见 4.3.3）。两条通路都依赖 category 字符串与既有约定的精确匹配，任何一条断掉都会产生「标签还是对的但颜色没了」或反过来的一侧失效。

#### 4.2.3 源码精读

[xtask/src/main.rs:170-179](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L170-L179) 是 `category_label` 的全部实现：一个五臂 `match` 把五个 slug 映射成展示标签，`_ => cat` 兜底返回原文。函数签名 `fn category_label(cat: &str) -> &str`——进的都是 `'static` 字面量，出的也是 `'static` 字面量，所以可以直接借用返回 `&str`，零分配。

类别 slug、展示标签与 README 中五级分类的对应关系（README 一侧的说明见 [README.md:39-45](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L39-L45)）：

| BOOKS 中的 slug | category_label 输出 | README 中的定位 |
|-----------------|---------------------|----------------|
| `bridge` | `Bridge` | 从其他语言过渡到 Rust 的入门起点 |
| `deep-dive` | `Deep Dive` | 对某个子系统的专注深潜 |
| `advanced` | `Advanced` | 面向有经验者的模式与技巧 |
| `expert` | `Expert` | 类型层面的前沿正确性技术 |
| `practices` | `Practices` | 工程化、工具链与生产就绪 |

这个函数在 `write_landing_page` 的闭包里被调用（[xtask/src/main.rs:185](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L185)），每本书一次。

#### 4.2.4 代码实践：观察兜底分支与 CSS 断链

1. **实践目标**：亲眼看到 `_ => cat` 兜底生效，以及「类别不在 CSS 表里」导致的视觉降级。
2. **操作步骤**：
   1. 打开 `xtask/src/main.rs`，把 python-book 条目的第四个字段从 `"bridge"` 临时改成 `"intro"`。
   2. 运行 `cargo xtask build`，然后浏览器打开 `site/index.html`（或 `cargo xtask serve`）。
   3. 观察 python-book 卡片的药丸标签文字和左侧竖条颜色。
   4. **改回 `"bridge"`**，重新构建。
3. **需要观察的现象**：标签文字显示为小写的 `intro`（走了 `_ => cat` 兜底）；同时卡片 class 变成 `cat-intro`——CSS 里没有这个类，`--stripe` 变量未定义，左侧彩色竖条和药丸标签的背景色会失去颜色（CSS 变量未定义时相关属性回退到初始值）。
4. **预期结果**：文字降级 + 颜色降级同时出现，且构建**不报任何错**。这说明「类别」这一概念横跨 Rust 数据、映射函数、CSS 类名三处约定，新增类别必须三处同步。
5. 具体渲染效果**待本地验证**（颜色回退的精确视觉表现因浏览器而异）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `category_label` 的兜底是 `_ => cat` 而不是 `panic!` 或返回 `Option<&str>`？

**答案**：这是一个纯展示层的翻译函数，输入是维护者写死在 `BOOKS` 里的编译期常量，不是外部输入。对于「数据错了」这种情况，让页面降级显示（显示原始 slug）比让整个构建崩溃（panic）更合理——书还是要构建出来的；返回 `Option` 则强迫调用方处理一个实际几乎不会发生的情况，增加噪音。如果这个函数的输入来自用户或配置文件，答案就会反过来。

**练习 2**：落地页顶部有一排「图例」（Bridge/Deep Dive/…五个彩色圆点）。结合源码说出：新增第六个类别 `intro` 需要改动哪几处？

**答案**：至少五处——(1) `BOOKS` 里某本书的 category 改成 `"intro"`；(2) `category_label` 加 `"intro" => "Intro"` 臂；(3) CSS `:root` 加 `--clr-intro` 颜色变量（[xtask/src/main.rs:204-215](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L204-L215)）；(4) CSS 加 `.cat-intro { --stripe: var(--clr-intro); }`（[xtask/src/main.rs:269-274](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L269-L274)）；(5) 图例 HTML 是手写的（[xtask/src/main.rs:291-297](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L291-L297)），要加一条 `<span class="legend-item">…`。这正是「类别清单没有被单一数据源化」的代价——好的小练习是思考如何把这五处收敛成一处（例如用 `const CATEGORIES` 数组驱动全部生成）。

### 4.3 HTML/CSS 模板拼接：format! 与 iterator 链

#### 4.3.1 概念说明

`write_landing_page` 要解决的問題是：**不用任何模板引擎、不加任何依赖**，把 `BOOKS` 数据渲染成一个完整的 `index.html`。这与 u2-l1 介绍的 xtask「极小依赖面」原则一脉相承——整个 xtask 只有 ctrlc 一个外部依赖，引入 tera/handlebars 之类模板库去渲染一个单页站显然过重。

它的做法分两步：

1. **数据 → 片段**：用 `BOOKS.iter().map(…).collect::<Vec<_>>().join("\n")` 把每本书渲染成一小段卡片 HTML，连成 `cards` 字符串。
2. **片段 → 页面**：用一个大 `format!` 把 `cards` 注入到内嵌的整页 HTML/CSS 模板的 `{cards}` 占位处。

值得学的 Rust 实战技巧有两个：

- **`{{ }}` 转义**：整页模板要作为 `format!` 的格式字符串，而 CSS 里全是花括号（`body { … }`），必须逐个写成 `{{ … }}`，唯一的真占位符 `{cards}` 保持单花括号。
- **`r##"…"##` 双井号原始字符串**：模板里到处是双引号（HTML 属性）和 `#`（CSS 十六进制颜色）。`r#"…"#` 遇到 `"#` 序列就会提前结束字符串——只要模板任何位置出现「引号紧跟井号」，字符串就会被悄悄截断。用两个井号 `r##"…"##` 定界，只有 `"##` 才会结束，彻底排除这种风险。这是一种防御式写法。

#### 4.3.2 核心流程

```text
BOOKS
  │ .iter()
  ▼
每本书 (slug, title, desc, cat)
  │ map(闭包):
  │    label = category_label(cat)
  │    format!(卡片模板, …)  →  一段卡片 HTML 字符串
  ▼
Vec<String>
  │ .join("\n")            ← 用换行连接，保证生成的 HTML 可读
  ▼
cards: String
  │ 外层 format!(整页模板, cards)   ← 注入到 {cards} 占位处
  ▼
html: String（完整 HTML 文档）
  │ fs::write(site/index.html, html)
  ▼
站点根目录 index.html
```

卡片颜色的工作原理（CSS 变量链路）：

```text
:root 定义五个颜色变量        .cat-bridge 类定义局部变量
  --clr-bridge: #4ade80   ──►  .cat-bridge { --stripe: var(--clr-bridge); }
                                    │
                 ┌──────────────────┴──────────────────┐
                 ▼                                     ▼
   .card { border-left: 4px solid var(--stripe) }   .label { background: var(--stripe) }
   （卡片左侧彩色竖条）                              （标题旁的药丸标签）
```

也就是说：类别 → CSS 类名 `cat-{cat}` → 该类上定义的 `--stripe` → 卡片竖条与标签背景色。同一变量还被子元素继承，所以改一个 `--clr-*` 能同时点亮卡片、标签、图例三处。

#### 4.3.3 源码精读

**第一步：卡片生成（iterator 链）。**

[xtask/src/main.rs:182-194](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L182-L194) 用 `map` + `collect::<Vec<_>>()` + `join("\n")` 生成所有卡片：闭包里先调 `category_label`，再用 `format!` 套卡片模板。每张卡片是一个 `<a>` 超链接——整张卡可点，`href="{slug}/"` 指向该书的输出子目录（末尾斜杠不可少，原因见下面的「衔接」）。用 `join("\n")` 而不是直接 `collect::<String>()`，是为了在卡片之间插入换行，让生成的 `index.html` 保持人类可读、可 diff。

[xtask/src/main.rs:186-191](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L186-L191) 是卡片模板本体（原文照录）：

```rust
format!(
    r#"    <a class="card cat-{cat}" href="{slug}/">
      <h2>{title} <span class="label">{label}</span></h2>
      <p>{desc}</p>
    </a>"#
)
```

三个细节：`cat-{cat}` 把数据直接拼成 CSS 类名（这是 4.2 说的第二条通路）；`r#"…"#` 单井号原始字符串让 HTML 属性的双引号免转义；模板里没有字面花括号，所以不需要 `{{ }}`。

**第二步：整页模板与注入。**

[xtask/src/main.rs:196-306](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L196-L306) 用一个 `format!` 包住整页 HTML：`<style>` 里全部 CSS 花括号都写成 `{{ }}`，唯一的占位符 `{cards}` 出现在网格容器内。外层定界符用的是 `r##"…"##`。

几个关键片段：

- [xtask/src/main.rs:204-215](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L204-L215)：`:root` 里定义全站颜色变量，包括五个类别色 `--clr-bridge` 到 `--clr-practices`（注意源码里这些都写作 `{{ … }}` 转义形式）。
- [xtask/src/main.rs:269-274](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L269-L274)：五个 `.cat-*` 类各自把 `--stripe` 绑到对应的 `--clr-*` 上——类别到颜色的「接线板」。
- [xtask/src/main.rs:250-259](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L250-L259)：`.card` 样式，`border-left: 4px solid var(--stripe)` 就是卡片左侧的彩色竖条。
- [xtask/src/main.rs:291-297](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L291-L297)：图例区。注意这五个图例项是**手写死**的 HTML，不来自 `BOOKS`——图例圆点用内联 `style="background:var(--clr-bridge)"` 引用同一变量，所以颜色会跟着变量变，但**条目本身不会随类别增减自动增减**（呼应 4.2.5 练习 2）。
- [xtask/src/main.rs:299-301](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L299-L301)：`<div class="grid">` 容器里的 `{cards}` 占位符——整页模板唯一的格式化注入点。

**第三步：写盘。**

[xtask/src/main.rs:308-310](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L308-L310) 把结果写到 `site.join("index.html")`（`site` 参数实际是 `build_to` 传入的输出目录，`build` 时是 `site/`，`deploy` 时是 `docs/`），并在终端打印 `✓ index.html`——你在 `cargo xtask build` 输出末尾看到的就是这一行。

**一个衔接点**：卡片 `href` 末尾的斜杠不是随手写的。每本书的产物是输出目录下的一个**子目录**，u2-l5 将要精读的内置服务器对「指向目录但没有尾斜杠」的请求会返回 301 重定向到带斜杠版本；直接带上斜杠可以省掉一次往返。落地页与服务器在这个细节上是配合的。

#### 4.3.4 代码实践：改一个颜色变量，点亮三处

1. **实践目标**：验证 CSS 变量链路——改一处 `--clr-*`，卡片竖条、药丸标签、图例圆点同时变色；同时熟悉「改模板 → 重新 build → 刷新」的迭代循环。
2. **操作步骤**：
   1. 打开 `xtask/src/main.rs`，在 `write_landing_page` 的 `:root` 段找到 `--clr-bridge`（源码中写作 `--clr-bridge: #4ade80;`，注意外层的 `{{` 转义），把色值改成醒目的红色，如 `#ff0000`。
   2. 运行 `cargo xtask build`。
   3. 浏览器打开 `site/index.html`（或先 `cargo xtask serve` 再访问 http://localhost:3000）。如果颜色没变，强制刷新（Ctrl+Shift+R / Cmd+Shift+R）排除缓存。
   4. 观察三处：三本 bridge 书卡片的左侧竖条、标题旁的药丸标签背景、顶部图例第一个圆点。
   5. 改回 `#4ade80`，重新构建。
3. **需要观察的现象**：三处同时变红——因为它们最终都引用 `var(--clr-bridge)` 这一个变量；其余四类书的颜色不受影响。
4. **预期结果**：确认「改一个变量 = 改一个类别的全部视觉呈现」。这就是模板把颜色收敛到变量的收益。
5. 具体渲染效果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果把外层模板里某个 CSS 规则的花括号从 `{{ }}` 误改成了 `{ }`，会发生什么？

**答案**：`format!` 会把 `{ … }` 当作格式化占位符解析。如果里面恰好是合法的参数名（比如你把 `body { … }` 改成 `{body}`，而 `format!` 只传了 `cards` 一个参数），编译器直接报错：`there is no argument named body`——这是好事，错误在编译期暴露。但如果花括号内容恰好与某个传入参数同名（本模板中只有 `cards`），就会静默注入错误内容。所以模板里的花括号转义错误**大部分在编译期被抓**，这也是选择编译期 `format!` 而非运行时模板引擎的一个隐性优点。

**练习 2**：卡片模板里 `{title}`、`{desc}` 直接插值进 HTML，没有做 HTML 转义（比如把 `<` 转成 `&lt;`）。这有问题吗？

**答案**：在本仓库语境下可接受——这两个值是维护者写死在 `BOOKS` 里的编译期常量，不是用户输入，不存在注入面。但必须意识到这是个**前提依赖**：如果将来 `BOOKS` 的数据来源改成读取外部文件或用户提交，未转义的插值就会变成 XSS 漏洞。工程判断是「数据可信 → 省掉转义依赖」，而不是「永远不需要转义」。

**练习 3**：`write_landing_page` 为什么用 `.collect::<Vec<_>>().join("\n")` 而不是让 `map` 直接 `collect::<String>()`？

**答案**：`map` 产出的是 `Vec<String>`（每张卡片一个字符串），`String` 的 `collect` 只能顺序拼接、无法插入分隔符，生成的 HTML 会挤成一行。`join("\n")` 在卡片之间插入换行，让产物可读、可 grep、可 diff。这是「生成物也要对人友好」的小习惯。

## 5. 综合实践

把本讲三个模块串起来，做一次完整的「数据 → 页面」实验。对应大纲里的实践任务。

**任务**：验证落地页完全由 `BOOKS` 与内嵌模板驱动，并亲手触发「目录缺失则跳过」分支，观察构建循环与落地页生成之间的防御不对称。

**步骤**：

1. **基线构建**：确认 `cargo xtask build` 成功，记录终端最后几行（`✓ index.html`、`Done! Output in site/`）。
2. **改数据源**：把 `BOOKS` 中 csharp-book 的描述改成一句你自己的话（例如 `"My custom description"`），重新 `cargo xtask build`，刷新 `site/index.html`——csharp-book 卡片的 `<p>` 应变成你的新文案。这一步证明文案来自 `BOOKS`。
3. **改模板**：把 `:root` 里的 `--clr-bridge` 换成别的颜色，重新构建，按 4.3.4 观察三处变色。这一步证明视觉来自内嵌模板。
4. **触发跳过分支**：向 `BOOKS` 末尾追加一条四元组，slug 用一个磁盘上不存在的目录，例如：
   ```rust
   (
       "ghost-book",
       "Ghost Book",
       "This directory does not exist",
       "bridge",
   ),
   ```
   （示例代码，实验后请删除。）再运行 `cargo xtask build`。
5. **观察三个信号**：
   - 终端出现 `✗ ghost-book/ not found, skipping`（来自 `build_to` 的检查）；
   - 统计行变成 `7/8 books built`，但**进程仍以退出码 0 结束**、`index.html` 仍生成；
   - 打开 `site/index.html`，发现落地页上**仍有一张 ghost-book 卡片**，点击进入是 404——因为 `write_landing_page` 只读 `BOOKS`，不检查目录。
6. **清理现场**：删除 ghost-book 条目，恢复描述与颜色，最后 `cargo xtask build` 确认回到 7/7，或用 `cargo xtask clean` 清掉产物。

**预期结论**（现象细节待本地验证）：`BOOKS` 是落地页的唯一数据源；构建循环对缺目录有防御（跳过 + 告警），落地页生成则完全没有——两者的一致性依赖于「注册进 `BOOKS` 的 slug 一定有对应目录」这条人工约定。这正 是 u4-l4「添加一本新书」实战中必须遵守的契约：**先建目录，再注册 `BOOKS`**。

## 6. 本讲小结

- `BOOKS` 是四元组 `(slug, title, description, category)` 的编译期常量表，被两个消费者以不同方式解构：构建循环只用 slug，落地页生成四个字段全用——一份注册表同时驱动构建清单与站点首页。
- `category_label` 用五臂 `match` + `_ => cat` 兜底，把机器 slug 翻译成人类标签；类别还有第二条通路——直接拼成 CSS 类名 `cat-{cat}`，两条通路都依赖字符串精确匹配。
- `write_landing_page` 的两步渲染：`iter().map().collect::<Vec<_>>().join("\n")` 生成卡片片段，再用整页 `format!` 注入 `{cards}`；零模板引擎依赖，延续 xtask 的极小依赖面原则。
- 两个 `format!` 实战技巧：CSS 花括号必须写成 `{{ }}`；充满引号和 `#` 的长模板用 `r##"…"##` 定界更安全。
- 颜色系统靠 CSS 变量链路收敛：`:root` 定义 `--clr-*` → `.cat-*` 绑定 `--stripe` → 卡片竖条与标签背景引用 `var(--stripe)`，改一个变量即可换掉一个类别的全部视觉呈现；但图例区是手写 HTML，不吃自动增减。
- 构建循环对「目录缺失」有跳过 + 告警的防御，落地页生成则完全没有——`BOOKS` 里注册了不存在的东西，会得到一张死链接卡片；两个消费者的鲁棒性不对称是维护时要知道的坑。

## 7. 下一步学习建议

本讲结束了 `build_to` 及其下游的最后一环——至此你已经读完了 xtask 构建侧的全部源码。接下来有两条路：

1. **u2-l5（内置静态服务器 I：路径解析与多层安全防护）**：`cargo xtask serve` 起的那个服务器是怎么把请求路径映射到 `site/` 下文件的？重点看它对目录请求的 301 重定向——正好解释了本讲卡片 `href="{slug}/"` 为什么必须带尾斜杠，以及四层路径安全防护各自防的是什么攻击。
2. **u4-l4（综合实战：向仓库添加一本新书）**：如果你更想动手，可以直接跳到 capstone，把本讲的结论（先建目录、再注册 `BOOKS`、类别三处约定）付诸一次端到端的新书实验。

如果想在读代码间隙换换口味，可以对照 [README.md:35-59](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L35-L59) 的「Start Reading」一节浏览线上落地页，体会 `BOOKS` 的数据是如何最终呈现在读者面前的。
