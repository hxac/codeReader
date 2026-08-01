# typst crate 定位与编译流水线地图

## 1. 本讲目标

本讲是整本「typst crate 学习手册」的第一篇。读完本讲，你应当能够：

- 说清 `typst` crate 在整个 `typst/typst` 仓库里扮演的角色——它是编译器的**顶层门面（facade）**，负责把多个子 crate 重新打包对外暴露，并提供 `compile` / `trace` 两个公开入口。
- 说出 Typst 把源码字符串变成导出产物的**四步流水线**：解析（parse）→ 求值（eval）→ 布局（layout）→ 导出（export），并知道每一步主要由哪个子 crate 负责。
- 通过 `Cargo.toml` 的依赖列表建立一张「子 crate 拓扑地图」，知道后续每一篇讲义会下钻到哪些 crate。
- 把 `lib.rs` 顶部的模块文档当作这本手册的导航来使用。

本讲**不**深入任何一个子 crate 的实现细节，只建立全局地图。后续讲义会沿这张地图逐层下钻。

## 2. 前置知识

在开始前，最好对以下概念有一点印象；没有也没关系，本讲会用通俗语言再解释一遍。

- **crate（包）**：Rust 中的一个编译单元。Typst 把整个编译器拆成了很多个 crate，每个 crate 负责一块独立的工作。
- **门面模式（facade pattern）**：当系统由很多组件组成时，单独提供一个「前台」，对外暴露一组简洁的接口，把内部组件的复杂关系藏在背后。`typst` crate 就是这样一个前台。
- **工作区（workspace）**：多个 crate 共享同一个 `Cargo.toml` 顶层配置、版本号等的组织方式。Typst 仓库就是一个 Cargo workspace。
- **token / 语法树 / AST**：把一段文本先切成一个个最小词法单元（token），再按语法规则组装成一棵树（语法树 / Syntax Tree），树的「带类型视图」就是 AST。这是几乎所有编译器都有的阶段。
- **Markdown / 标记语言**：Typst 自己定义了一种标记语言（markup），你写的 `.typ` 文件就是这种标记语言。

如果你对 Rust 的 `pub use`（再导出）和 `trait` 还不熟，本讲会用到它们但会顺带解释，不必提前掌握。

## 3. 本讲源码地图

本讲只涉及 `typst` crate 自身，且只看两个文件：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `crates/typst/Cargo.toml` | 描述 `typst` crate 的元信息与依赖。 | `[dependencies]` 一节——它列出了所有被组装进来的子 crate。 |
| `crates/typst/src/lib.rs` | `typst` crate 的**全部**源码（这个 crate 源码极小）。 | 顶部模块文档的「Steps」小节、三条 `pub use` 再导出语句、以及公开入口 `compile` / `trace`。 |

> 小提示：`typst` crate 的源码只有一个文件 `src/lib.rs`，加上一个 `Cargo.toml`。它本身几乎没有「实现逻辑」，绝大多数工作是「接线」和「再导出」。理解这一点，是理解整本手册的关键。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 门面角色与再导出**：搞清 `typst` crate 为什么这么「薄」，它是怎么把 `typst_library` 等子 crate 的内容转手交给用户的。
- **4.2 编译四步流水线**：读懂 `lib.rs` 顶部文档里描述的 parse → eval → layout → export。
- **4.3 依赖地图与 crate 切分**：用 `Cargo.toml` 建立子 crate 拓扑，理解「导出」其实并不在本 crate 里完成。

### 4.1 门面角色与再导出

#### 4.1.1 概念说明

如果你直接打开 `typst` crate 的源码，会发现它薄得惊人：除了两个公开函数 `compile` 和 `trace`，几乎没有任何业务实现。那它的价值是什么？

答案是：它是一个**门面（facade）**。Typst 的能力（标准库、语法、求值、布局等）分散在十几个子 crate 里。但作为一个想要「用 Typst 把文档编译成 PDF」的使用者，你不想去认识十几个 crate，你只想 `use typst;` 然后调一个函数。

`typst` crate 就负责做这件事：

1. 把内部子 crate 里有用的类型与函数**再导出（re-export）**到自己的命名空间下，让用户只需要依赖一个 `typst` crate。
2. 提供一个高层入口 `compile()`，把内部的多个步骤串起来。

这就是「门面模式」：一个对外的简洁前台，背后是复杂的组件协作。

#### 4.1.2 核心流程

`typst` crate 的「再导出」结构大致如下（伪代码）：

```
用户代码  use typst::{compile, World, Module, Content, ...};
            │
            ▼
      typst crate (facade)
            │
            ├── pub use typst_library::*;   ← 标准库里的几乎所有公共类型
            ├── pub use typst_syntax as syntax;  ← 以子模块形式暴露语法层
            └── pub use typst_utils as utils;    ← 以子模块形式暴露工具层
```

也就是说，你在 `typst` 这个 crate 里能用到的 `World`、`Module`、`Content`、`Value` 等类型，绝大多数其实定义在 `typst_library` 里，只是被 `typst` 转手导出了一遍。

#### 4.1.3 源码精读

打开 `src/lib.rs`，第 33–40 行就是再导出的全部内容，非常简短：

[crates/typst/src/lib.rs:L33-L40](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L33-L40) —— 这六行先 `extern crate` 把 `comemo` / `ecow` 这两个第三方库重新暴露出来，然后用三条 `pub use` 把 `typst_library` 的全部内容、`typst_syntax`、`typst_utils` 转手导出，构成了门面的核心。

要点解读：

- `pub use typst_library::*;`（第 36 行）：把标准库 crate 的**所有**公共项搬到 `typst` 名下。`World`、`Engine`、`Module`、`Content`、`Value`、`Library`、各种诊断类型等都来自这里。
- `#[doc(inline)] pub use typst_syntax as syntax;`（第 37–38 行）：把 `typst_syntax` 暴露为 `typst::syntax` 子模块。`#[doc(inline)]` 表示在生成的文档里把它的内容「平铺」进 `typst`，而不是显示成一个独立的外部链接。
- `#[doc(inline)] pub use typst_utils as utils;`（第 39–40 行）：同理，把工具层暴露为 `typst::utils`。

正因为有这三行，用户 `use typst;` 就能拿到几乎所有日常需要的类型，而**不必**单独依赖 `typst-library`、`typst-syntax`、`typst-utils`。这就是门面带来的便利。

#### 4.1.4 代码实践

**实践目标**：亲手验证「门面只是再导出」，加深对 facade 的直观印象。

**操作步骤**：

1. 在本仓库根目录执行 `cargo doc --package typst --no-deps --open`（生成 `typst` crate 自身的文档）。
2. 在打开的文档里，进入 `typst` 模块，观察 `World`、`Content`、`Value` 等类型的页面。
3. 点开任意一个（例如 `typst::Content`），看页面顶部「定义于」或「来自」的位置提示。

**需要观察的现象**：

- `typst` 的文档页面会「扁平地」列出大量类型，仿佛它们都定义在 `typst` 里。
- 但若深入查看某一类型的真实定义位置，会跳转到 `typst_library`（或其更下层模块），说明它们是被 `pub use` 进来的。

**预期结果**：你会直观看到，`typst` crate 像一个聚合了多个子 crate 的前台，文档里的类型数量远多于 `src/lib.rs` 本身定义的内容——这正是因为再导出把别处的类型「借」过来了。

> 若无法本地运行 `cargo doc`，可改为「源码阅读型」验证：在 `src/lib.rs` 中搜索，确认除了 `compile` / `trace` / 几个辅助函数与 `LibraryExt` 外，几乎没有 `struct` / `enum` / `fn` 的定义，从而证明本 crate 的类型基本全部来自再导出。**待本地验证**：具体 `cargo doc` 输出以本地为准。

#### 4.1.5 小练习与答案

**练习 1**：如果 `typst` crate 删掉 `pub use typst_library::*;` 这一行，用户写 `typst::Content` 还能编译通过吗？为什么？

> **答案**：不能（除非用户自己再单独依赖 `typst-library`）。`Content` 实际定义在 `typst_library` 中，能以 `typst::Content` 访问，全靠这一行再导出。删掉后，`typst` 名下就找不到 `Content` 了。

**练习 2**：`pub use typst_syntax as syntax;` 和 `pub use typst_library::*;` 在「暴露方式」上有什么不同？

> **答案**：前者把 `typst_syntax` 整体作为一个**具名子模块** `typst::syntax` 暴露（你访问 `typst::syntax::parse`）；后者用通配符 `*` 把 `typst_library` 的所有公共项**平铺**到 `typst` 顶层（你直接访问 `typst::Content`，而不是 `typst::library::Content`）。

---

### 4.2 编译四步流水线

#### 4.2.1 概念说明

Typst 是一个编译器：输入是一段 `.typ` 源码字符串，输出是一份排好版的文档。和大多数编译器一样，它不会一步到位，而是分成几个阶段。`lib.rs` 顶部模块文档明确把整个流程概括为四步：

1. **解析（Parsing）**：把纯文本字符串切成 token，再组装成语法树。
2. **求值（Evaluation）**：把语法树求值成一个模块（module）和一棵带样式的「内容树」（content）。
3. **布局（Layouting）**：把 content 排版成由若干「页」（frame）组成的 `PagedDocument`，每一页上每个元素都有固定坐标。
4. **导出（Exporting）**：把这些 frame 写成最终的输出格式（PDF、PNG、SVG、HTML）。

> ⚠️ 一个容易被忽略但很重要的细节：**第 4 步「导出」并不在 `typst` crate 内部完成**。`typst` crate 的 `compile()` 只产出 `PagedDocument`（或 `HtmlDocument`），真正的 PDF / SVG / PNG 导出由**下游的** `typst-pdf` / `typst-svg` / `typst-png` 等独立 crate 完成。`typst` 的 `Cargo.toml` 里并没有依赖它们（见 4.3）。所以在严格意义上，`typst` crate 实现的是前三步，第四步交给调用方。

#### 4.2.2 核心流程

四个阶段可以用下面这张流程图来理解：

```
.typ 源码字符串
      │  ① Parsing        （typst-syntax）
      ▼
  语法树 SyntaxNode + AST
      │  ② Evaluation     （typst-eval）
      ▼
 module + content（带样式的内容树）
      │  ③ Layouting      （typst-realize → typst-layout，多轮迭代）
      ▼
 PagedDocument / HtmlDocument（每页一个 frame）
      │  ④ Exporting      （下游 typst-pdf / typst-svg / …，不在本 crate）
      ▼
 PDF / PNG / SVG / HTML 文件
```

每一步「做什么」「交给谁」：

| 步骤 | 做什么 | 主要负责的 crate |
| --- | --- | --- |
| ① 解析 | 字符串 → token → 语法树（AST） | `typst-syntax` |
| ② 求值 | 语法树 → module + content | `typst-eval` |
| ③ 布局 | content → `PagedDocument`（frame 集合） | `typst-realize`（先具现化）、`typst-layout`（再排版） |
| ④ 导出 | frame → PDF/PNG/SVG/HTML | 下游 crate（`typst-pdf` 等），**不在本 crate** |

注意第 ③ 步在 `typst` crate 里并不是一次完成的：因为页码、目录、交叉引用等「内省（introspection）」信息依赖最终的排版结果，Typst 会**反复布局直到结果稳定**（最多 5 次）。这个「稳定化循环」是本 crate 最核心的逻辑，但属于进阶篇（u2-l2），本讲只需知道「布局这一步可能要跑好几轮」即可。

#### 4.2.3 源码精读

四步流水线的权威定义就在 `lib.rs` 顶部的模块文档里：

[crates/typst/src/lib.rs:L1-L31](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L1-L31) —— 这是整个 crate 的导航：开篇一句点明「这是 Typst 标记语言的编译器」，随后用 `# Steps` 小节按顺序列出 Parsing / Evaluation / Layouting / Exporting 四个阶段，并在每个阶段的方括号 `[...]` 里标注了对应的子 crate 符号（如 `[evaluate]: typst_eval::eval`、`[laid out]: typst_layout::layout_document`），相当于一张「阶段 → crate」对照表。

这段文档里几个关键链接值得记住，它们正是后续讲义会下钻的入口：

- `[tokens]: typst_syntax::SyntaxKind`、`[parsed]: typst_syntax::parse`、`[syntax tree]: typst_syntax::SyntaxNode`、`[AST]: typst_syntax::ast` → ① 解析阶段都归 `typst_syntax`。
- `[evaluate]: typst_eval::eval`、`[module]: crate::foundations::Module`、`[content]: crate::foundations::Content` → ② 求值由 `typst_eval::eval` 完成，产物是 module 与 content。
- `[laid out]: typst_layout::layout_document`、`[frame]: crate::layout::Frame` → ③ 布局由 `typst_layout` 完成。
- 第 19–21 行单独说明 ④ 导出可写成 PDF / PNG / SVG / HTML（但实现不在本 crate）。

接着看公开入口 `compile`，它是这条流水线的「总开关」：

[crates/typst/src/lib.rs:L63-L82](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L63-L82) —— `pub fn compile<T>(world: &dyn World) -> Warned<SourceResult<T>>` 接收一个实现了 `World` trait 的「外部世界」，返回一个「带警告的结果」。注释里写明支持的产出是 `PagedDocument`（定义在 `typst_layout`）和 `HtmlDocument`（定义在 `typst_html`）。函数体里真正干活的是 `compile_impl`，它把上面四步串起来。

真正把四步串起来的实现在 `compile_impl` 里，本讲只看其中两处关键调用：

[crates/typst/src/lib.rs:L122-L131](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L122-L131) —— ② 求值阶段：调用 `typst_eval::eval(...)` 把主源码求值成 module，并 `.content()` 取出 content 树。这正是模块文档里 `[evaluate]: typst_eval::eval` 在代码里的落点。

[crates/typst/src/lib.rs:L156](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L156) —— ③ 布局阶段：`T::create(&mut engine, &content, styles)` 根据 `T`（如 `PagedDocument`）把 content 排版成文档。`T::create` 的具体实现位于 `typst_layout` / `typst_html`，本 crate 通过 `Output` trait 抽象地调用它（详见专家篇 u3-l1）。

至于 ① 解析：在 `compile_impl` 里它并不表现为一个显式的 `parse(...)` 调用，而是隐藏在 `world.source(main)`（第 117–120 行）取回的 `Source` 对象里——`typst_syntax` 会按需把源码字符串惰性地 tokenize、parse 成语法树。④ 导出则完全不在这段代码里。

> 本讲只要记住：`compile_impl` 串起了「求值 → 布局」两步，解析藏在 `Source` 里，导出在本 crate 之外。完整逐段精读见进阶篇 u2-l1。

#### 4.2.4 代码实践

**实践目标**：用阅读源码的方式，把「四步」对应到 `lib.rs` 里的具体代码位置，建立直觉。

**操作步骤**：

1. 打开 `crates/typst/src/lib.rs`。
2. 找到顶部模块文档（第 1–31 行），抄下四步的英文名。
3. 在 `compile_impl`（第 99 行起）里，逐一标注：
   - 解析发生在哪一行（提示：找 `world.source(main)`）。
   - 求值发生在哪一行（提示：找 `typst_eval::eval`）。
   - 布局发生在哪一行（提示：找 `T::create`）。
   - 导出在不在 `compile_impl` 里？

**需要观察的现象**：解析、求值、布局都能在 `compile_impl` 中找到对应行；而导出确实**找不到**——这印证了「导出不在本 crate」。

**预期结果**：你会得到一份带行号的小抄，例如「解析 ~L117、求值 L123、布局 L156、导出 = 无（下游 crate）」。这正好是四步流水线在代码里的落地证据。

> 本实践是源码阅读型，无需运行命令；行号以本讲引用的 HEAD 为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么说 `compile()` 严格只完成了「四步」中的前三步？

> **答案**：因为 `compile()` 返回的是 `PagedDocument` / `HtmlDocument`（即排好版的 frame 集合），而不是 PDF/SVG 文件。把 frame 转成最终文件格式（第 4 步导出）由 `typst-pdf` 等下游 crate 负责，`typst` crate 的 `Cargo.toml` 并不依赖它们。

**练习 2**：在 `compile_impl` 里，为什么看不到一个明显的 `parse(...)` 调用？

> **答案**：解析被封装进了 `typst_syntax` 的 `Source` 类型，并且是惰性的。`compile_impl` 通过 `world.source(main)` 拿到 `Source`，真正的 tokenize + parse 在需要 AST 时（例如 `typst_eval::eval` 内部访问语法树时）才由 `typst_syntax` 完成，所以 `compile_impl` 里没有一行形如 `parse(source)` 的代码。

**练习 3**：模块文档说布局产物是 `PagedDocument`，但 `compile` 是泛型 `compile<T>`。这两者矛盾吗？

> **答案**：不矛盾。`T` 是泛型参数，约束为 `T: Output`。当 `T = PagedDocument` 时对应分页文档；当 `T = HtmlDocument` 时对应 HTML 文档。模块文档只是举了最常见的 `PagedDocument` 例子，泛型让同一个 `compile` 能产出不同目标。

---

### 4.3 依赖地图与 crate 切分

#### 4.3.1 概念说明

要建立「整本手册会去哪里读源码」的全局地图，最直接的入口就是 `typst` crate 的 `Cargo.toml`。它用 `typst-xxx = { workspace = true }` 的形式列出了所有内部依赖。

为什么 Typst 要把编译器拆成这么多 crate？主要原因是**关注点分离**和**避免循环依赖**：语法、求值、布局、HTML、标准库等是相对独立的领域，拆开后可以各自独立编译、独立测试，也方便像本手册这样「一个 crate 一本讲义」地学习。代价是：有些跨 crate 的调用不能直接 `use`，需要用「函数指针表」（`Routines`）来间接接线（这部分是专家篇 u3-l2 的内容，本讲先有个印象即可）。

`Cargo.toml` 里 `{ workspace = true }` 的意思是「版本号等配置继承自顶层 workspace 的 `Cargo.toml`」，所以这里看不到版本号，只看到依赖名。

#### 4.3.2 核心流程

`typst` crate 的依赖可分为三类：

1. **Typst 内部子 crate**（`typst-*`）：构成编译器的真正能力。
2. **构建期 / 宏依赖**：`typst-macros`（提供过程宏，如 `#[typst_macros::time]`）。
3. **第三方工具库**：`arrayvec`（栈上定长数组）、`comemo`（记忆化缓存）、`ecow`（写时复制的紧凑字符串/向量）、`rustc-hash`（快速哈希）。

一张「内部子 crate → 职责 → 对应流水线步骤」的对照表：

| 子 crate | 主要职责 | 对应流水线步骤 |
| --- | --- | --- |
| `typst-syntax` | 词法、语法树、AST | ① 解析 |
| `typst-eval` | 求值、变量作用域、函数调用 | ② 求值 |
| `typst-realize` | 把 content「具现化」成可排版的元素 | ③ 布局（前置） |
| `typst-layout` | 排版、断行、分页，产出 `PagedDocument` | ③ 布局 |
| `typst-html` | HTML 目标的模块与产出 `HtmlDocument` | ③ 布局（HTML 目标） |
| `typst-library` | 标准库：foundations / model / text / layout / visualize / introspection … | 贯穿②③，提供 `World`、`Engine`、`Library` 等 |
| `typst-utils` | 通用工具（哈希、`Protected` 等） | 底层支撑 |
| `typst-timing` | 性能计时（`#[time]`、`timed!`） | 底层支撑 |
| `typst-macros` | 过程宏 | 构建期 |

注意：`typst-pdf` / `typst-svg` / `typst-png` 等**导出 crate 并不出现在这里**——再次印证「导出不在本 crate」。

#### 4.3.3 源码精读

依赖列表就在 `Cargo.toml` 的 `[dependencies]` 节：

[crates/typst/Cargo.toml:L15-L28](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/Cargo.toml#L15-L28) —— `[dependencies]` 一节列出了全部依赖。第 16–24 行是九个 `typst-*` 内部子 crate，全部用 `{ workspace = true }` 继承 workspace 配置；第 25–28 行是四个第三方库 `arrayvec` / `comemo` / `ecow` / `rustc-hash`。注意这里**没有** `typst-pdf` / `typst-svg` 等导出 crate。

正因为有这份依赖，门面的再导出才成立——`typst` 之所以能 `pub use typst_library::*`，是因为它 `Cargo.toml` 里依赖了 `typst-library`。

而依赖之间是如何「接线」的？最直观的证据是 `lib.rs` 末尾的 `ROUTINES` 静态量（本讲只看它「长什么样」，不展开机制）：

[crates/typst/src/lib.rs:L307-L325](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L307-L325) —— 注释明说这是「用函数指针表实现的动态链接，目的是 crate 切分（This is essentially dynamic linking and done to allow for crate splitting）」。可以看到它把 `typst_layout::register`、`typst_html::register`、`typst_eval::eval_string`、`typst_realize::realize`、`typst_layout::layout_frame` 等子 crate 里的函数，填进一张统一的 `Routines` 表。这就是「门面把多个子 crate 的能力装配在一起」在代码层面的体现。具体机制留到专家篇 u3-l2。

> 本讲你只需要从这张表里读出一个结论：`typst` crate 的全部「实现」来自这些被依赖和被接线的子 crate；`lib.rs` 本身只是装配层。

#### 4.3.4 代码实践

**实践目标**：画出 `typst` crate 与其内部子 crate 的依赖关系图，并据此规划后续阅读路线。

**操作步骤**：

1. 打开 `crates/typst/Cargo.toml`，把第 16–24 行的九个 `typst-*` 依赖抄下来。
2. 按本讲 4.3.2 的职责表，给每个 crate 标注它属于流水线的哪一步。
3. 用纸笔或画图工具画出：以 `typst` 为中心，向外连到九个子 crate，连线上标注「解析 / 求值 / 布局 / HTML / 标准库 / 工具 / 计时 / 宏」。
4. 在图上额外画一个虚线框「下游导出 crate（typst-pdf / typst-svg / typst-png）」，用虚线连到 `typst`，标注「不在 Cargo.toml 内」。

**需要观察的现象**：图里 `typst` 是汇聚点；`typst-syntax` 对应解析、`typst-eval` 对应求值、`typst-realize` + `typst-layout` + `typst-html` 对应布局、`typst-library` 提供标准库与公共抽象；导出 crate 用虚线表示在边界之外。

**预期结果**：得到一张清晰的子 crate 拓扑图。这张图就是本手册后续所有讲义的「地图」——之后每一篇都会在这张图上点亮一个或几个 crate。

> 若不方便画图，可用缩进列表代替，例如：
> ```
> typst (facade)
> ├── 解析: typst-syntax
> ├── 求值: typst-eval
> ├── 布局: typst-realize, typst-layout, typst-html
> ├── 标准库/公共: typst-library
> ├── 工具/计时/宏: typst-utils, typst-timing, typst-macros
> └── (虚线) 导出: typst-pdf / typst-svg / typst-png  ← 不在 Cargo.toml
> ```

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Cargo.toml` 里 `typst-library` 的写法是 `typst-library = { workspace = true }` 而不是带版本号？

> **答案**：因为整个仓库是一个 Cargo workspace，版本号、edition 等公共配置统一在顶层 workspace 的 `Cargo.toml` 里声明，子 crate 用 `{ workspace = true }` 继承，避免在每个 crate 里重复写版本号、也便于统一升版本。

**练习 2**：如果有人问你「`typst` crate 能不能直接输出 PDF」，你怎么根据本讲的源码证据回答？

> **答案**：不能直接输出 PDF。证据有二：① `Cargo.toml` 的 `[dependencies]` 里没有 `typst-pdf`；② `compile()` 返回的是 `PagedDocument`，模块文档也说导出（PDF/PNG/SVG/HTML）是「最终」一步、由别的代码完成。要得到 PDF，需要在拿到 `PagedDocument` 后再调用 `typst-pdf` 等下游 crate。

**练习 3**：`ROUTINES` 静态量里出现了 `typst_layout::register` 和 `typst_html::register`，这暗示了什么？

> **答案**：暗示布局规则（rule）是在子 crate（`typst-layout` / `typst-html`）里定义并通过 `register` 注册进一张统一的规则表，再由 `typst` crate 在启动时装配。这正是「crate 切分 + 动态接线」的体现，细节见专家篇 u3-l2。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个贯穿性小任务：

> **任务：为 `compile()` 写一份「四步流水线 + crate 责任」的带行号说明卡。**

要求：

1. 从 `lib.rs` 顶部模块文档（第 1–31 行）出发，列出四个阶段的名字。
2. 对每个阶段，写明「负责的 crate」和「在 `compile_impl` 里的对应代码行号（或说明为何没有）」。可参考下表填空：

   | 阶段 | 负责 crate | 在 `compile_impl` 中的位置 |
   | --- | --- | --- |
   | 解析 | `typst-syntax` | 隐藏在 `world.source(main)`（约 L117）取回的 `Source` 中，惰性完成 |
   | 求值 | `typst-eval` | _（请填：`typst_eval::eval` 在第几行）_ |
   | 布局 | `typst-realize` + `typst-layout`（+ `typst-html`） | _（请填：`T::create(...)` 在第几行，并简述为何要多轮）_ |
   | 导出 | 下游 crate（不在本 crate） | _（请填：为何在 `compile_impl` 中找不到）_ |

3. 用一句话总结：`typst` crate 在这四步里「自己实现了什么、转手了什么」。提示——它自己实现的只是「编排（orchestration）」，真正的能力全部来自被再导出和被接线的子 crate。

完成这张说明卡后，你就达成了本讲的全部学习目标：认清了门面角色、记住了四步流水线、建立了子 crate 地图，并能用 `lib.rs` 顶部文档导航后续学习。

## 6. 本讲小结

- `typst` crate 是 Typst 编译器的**顶层门面（facade）**：源码极小，几乎只有 `compile` / `trace` 两个公开函数和几条再导出语句。
- 它通过 `pub use typst_library::*`（平铺）、`pub use typst_syntax as syntax`、`pub use typst_utils as utils`（具名子模块）把子 crate 的能力转手暴露给用户。
- 编译流程分四步：**解析 → 求值 → 布局 → 导出**，权威定义在 `lib.rs` 顶部模块文档的 `# Steps` 小节。
- 各步责任：解析归 `typst-syntax`、求值归 `typst-eval`、布局归 `typst-realize` + `typst-layout`（HTML 目标另有 `typst-html`），标准库与公共抽象在 `typst-library`。
- 关键细节：**导出（PDF/SVG/PNG）不在本 crate**——`compile()` 只产出 `PagedDocument` / `HtmlDocument`，`Cargo.toml` 也不依赖 `typst-pdf` 等导出 crate。
- `Cargo.toml` 的 `[dependencies]` 和 `lib.rs` 末尾的 `ROUTINES`（函数指针表）共同构成了「门面如何装配子 crate」的证据，是本手册后续讲义的导航地图。

## 7. 下一步学习建议

本讲建立了全局地图，后续建议按手册的依赖顺序继续：

1. **u1-l2（World trait）**：`compile()` 需要一个 `&dyn World` 作为输入。下一篇就讲清「宿主必须提供什么、缓存职责为何放在 World 一侧」——这是把 Typst 嵌入自己程序的前提。
2. **u1-l3（调用 compile 完成一次编译）**：把 `compile::<T>` 的签名、返回类型 `Warned<SourceResult<T>>` 讲透，并给出最小调用骨架。
3. 之后再进入进阶篇 u2，逐段精读 `compile_impl` 主流程与「内省稳定化循环」。

继续阅读源码时，建议把 `crates/typst/src/lib.rs` 这个文件常备在手——整本手册的「编排逻辑」几乎全在这一个文件里。
