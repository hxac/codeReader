# 构建与测试：跑通 editor crate 的第一个测试

## 1. 本讲目标

上一讲我们建立了 editor crate 的整体地图。本讲解决一个更实际的问题：**怎么验证自己对源码的理解？怎么让每一次阅读都有可运行的证据？**

学完本讲，你应该能够：

1. 说清 `test-support` 这个 feature 到底打开了什么、给谁用，以及它和 `[dev-dependencies]` 的分工。
2. 独立运行 editor crate 中的任意一个测试，并理解 `cargo test -p editor <名字>` 的过滤规则。
3. 看懂 `src/editor_tests.rs`（43366 行）的四种测试写法，以及几乎每个测试开头那行 `init_test(cx, |_| {})` 做了什么。
4. 逐行读懂 `test_undo_redo_with_selection_restoration`，并预告第 4 单元将要深入的「编辑事务」机制。
5. 故意让一个测试失败、读懂失败输出、再还原——这是后续所有讲义中「改一点、看一点」实践方法的基础。

## 2. 前置知识

### 2.1 Cargo 的 feature 与 optional 依赖

Cargo 允许在 `[features]` 里声明一组「开关」。一个 feature 可以：

- **开启依赖的 feature**：写成 `"gpui/test-support"`，表示「启用我这个 feature 时，请同时打开 gpui 的 test-support feature」。
- **启用可选依赖**：在 `[dependencies]` 里用 `optional = true` 声明的依赖，只有在某个 feature 点名它时才会被编译进来。

`test-support` 这个名字是 Zed 工作区里的约定：**某个 crate 的测试工具代码**。它不进发布产物，只服务于测试。

### 2.2 `cfg(test)` 与 `cfg(feature = ...)`

Rust 的条件编译有两种常见门控：

- `#[cfg(test)]`：只在**编译本 crate 自己的测试**时为真（`cargo test -p editor` 时）。别的 crate 依赖你时，这个条件永远为假。
- `#[cfg(feature = "test-support")]`：只要依赖方在 `Cargo.toml` 里写了 `editor = { features = ["test-support"] }` 就为真。

所以 editor 里的测试模块普遍写成 `#[cfg(any(test, feature = "test-support"))]`——**「我自己测试时要它，下游打开开关时也要它」**。

### 2.3 `#[test]` 与 `#[gpui::test]`

标准库的 `#[test]` 只能标记无参（或返回 `Result`）的函数。而 editor 的测试需要初始化整个 GPUI 应用环境（设置存储、主题、字体、测试执行器）。所以这个 crate 里绝大多数测试用的是 `#[gpui::test]` 属性宏：它让测试函数可以携带一个 `cx: &mut TestAppContext` 参数。`TestAppContext` 是 GPUI 提供的测试上下文，能：

- `cx.add_window(...)` 创建一个真实（但离屏）的窗口；
- `cx.new(|cx| ...)` 创建实体；
- `cx.executor().run_until_parked()` 把所有挂起的异步任务跑到没有可推进的工作为止（代替真实等待）。

### 2.4 marked text：用「记号文本」做断言

editor 的测试大量使用「带标记的文本」来同时表达**内容**和**位置**：

- `|` 表示一个光标位置（偏移标记），例如 `"ab|cd"` 表示光标在偏移 2；
- `«»` 成对出现，表示一个选区范围，例如 `"ab«cd»ef"` 表示选中 `cd`。

这些记号会在构造时被剥掉，换算成真实的偏移/区间；断言时再反向生成带标记的文本做整体比较。工具函数来自 `util` crate 的 `util::test` 模块（`marked_text_offsets`、`marked_text_ranges`、`generate_marked_text`），editor 在自己的 `src/test.rs` 里对其做了二次封装。

## 3. 本讲源码地图

| 文件 | 行数 | 在本讲中的角色 |
| --- | --- | --- |
| `Cargo.toml` | 140 | 定义 `test-support` feature 与 `[dev-dependencies]`，是「测试开关三层设计」的源头 |
| `src/editor.rs` | 大 | 第 50–60 行的模块声明处，能看到 `editor_tests` 与 `test` 两个模块的不同门控 |
| `src/editor_tests.rs` | 43366 | 主测试文件：507 处 `#[gpui::test]`、3 个顶格 `#[test]`、1049 处 `assert_eq!` |
| `src/test.rs` | 293 | 面向下游 crate 的公共测试工具箱（marked text 封装、`build_editor`、块内容断言） |
| `src/test/editor_test_context.rs` | — | `EditorTestContext`：更高层的声明式测试上下文（本讲只指路，第 8 单元精读） |
| `src/test/editor_lsp_test_context.rs` | — | 搭假语言服务器用的 `EditorLspTestContext`（第 6、7 单元用到） |
| `src/editor_tests/property_test.rs` | — | 基于 proptest 的随机编辑性质测试（第 8 单元精读） |

## 4. 核心概念与源码讲解

本讲的四个最小模块：**测试开关的三层设计** → **test.rs 公共工具箱** → **editor_tests.rs 的组织方式** → **精读一个具体测试**。

### 4.1 测试开关的三层设计：feature、dev-dependencies 与 cfg 门控

#### 4.1.1 概念说明

「editor 的测试工具」要服务两类用户：

1. **editor crate 自己**跑测试时（`cargo test -p editor`）；
2. **下游 crate**（vim、project_search、terminal_view……）想复用这些工具来测自己的功能时。

这就需要一个既能在 `cfg(test)` 下编译、又能按需导出给别人的结构。editor 用「三层」来实现：

- 第一层：`[features]` 里的 `test-support`——对外的总开关；
- 第二层：`[dev-dependencies]`——只有 editor 自己跑测试时才生效的依赖；
- 第三层：源码里的 `#[cfg(any(test, feature = "test-support"))]` 门控——决定哪些模块按哪种条件参与编译。

#### 4.1.2 核心流程

```text
下游 crate 要复用 editor 测试工具
  └─ 在自己的 dev-dependencies 里写 editor = { features = ["test-support"] }
       └─ editor/test-support 被打开
            ├─ 连带打开 text/language/gpui/... 共 8 个依赖的 test-support
            └─ 连带启用 tree-sitter 语法树、proptest、unindent 这 6 个可选依赖
                 └─ editor 源码中 #[cfg(any(test, feature = "test-support"))] 的模块被编译并 pub 导出

editor 自己跑测试（cargo test -p editor）
  └─ cfg(test) 为真 → 同样的模块被编译
       └─ [dev-dependencies] 保证 proptest、unindent、tree-sitter 语法等此时必然可用
```

#### 4.1.3 源码精读

先看库根配置。上一讲已经确认库根是 `src/editor.rs`，这里还隐藏着一个与测试相关的细节：`doctest = false`——本 crate 不把文档里的代码块当作测试运行：

- [Cargo.toml:L11-L13](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/Cargo.toml#L11-L13)：`[lib]` 段声明库根路径为 `src/editor.rs`，并关闭 doctest。像 editor 这样依赖完整应用环境的 crate，文档示例很难独立编译成测试，所以直接关掉。

接着是本讲的主角之一，`test-support` feature 的完整清单：

- [Cargo.toml:L15-L31](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/Cargo.toml#L15-L31)：`test-support` feature 的 11 个条目可以分成两类——前 8 条（`text/test-support`、`language/test-support`、`gpui/test-support`、`multi_buffer/test-support`、`project/test-support`、`theme/test-support`、`util/test-support`、`workspace/test-support`）是**转发**：把下游对「测试模式」的请求继续传给自己的依赖，保证整个测试环境里各层行为一致（例如缓冲区提供测试专用的构造器）；后几条（`tree-sitter-c`、`tree-sitter-rust`、`tree-sitter-typescript`、`tree-sitter-html`、`proptest`、`unindent`）是**启用可选依赖**：让测试能解析真实语法、做属性测试、剥离多行字符串缩进。

可选依赖的声明位置：

- [Cargo.toml:L69-L70](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/Cargo.toml#L69-L70)：`proptest` 与 `proptest-derive` 被声明为 `optional = true`。
- [Cargo.toml:L88-L97](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/Cargo.toml#L88-L97)：四个 tree-sitter 语法与 `unindent` 同样是可选依赖。注意它们**既出现在 feature 清单里，又出现在下面的 dev-dependencies 里**——前者服务下游，后者保证 editor 自测时必然可用。

最后是 dev-dependencies：

- [Cargo.toml:L109-L140](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/Cargo.toml#L109-L140)：`[dev-dependencies]` 只在编译本 crate 的测试/示例时生效。这里的门道是：`gpui`、`language`、`multi_buffer`、`project`、`theme`、`workspace` 等都以 `features = ["test-support"]` 的形式引入，另外还拉进了 `languages`（真实语言定义）、`tree-sitter-go/yaml/bash/md` 等更多语法、`semver`、`release_channel` 等。**editor 自测时并不需要打开自己的 `test-support` feature**——`cfg(test)` 已经放行了那些门控，而原本由 feature 负责启用的可选依赖在这里被 dev-dependencies 兜底。

再看源码侧的两道门（呼应上一讲的模块声明区）：

- [src/editor.rs:L50-L60](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor.rs#L50-L60)：这是理解三层设计的最好对照——`#[cfg(test)] mod editor_tests;`：**43366 行的主测试文件只有 editor 自己测试时才编译**，下游永远看不到它；而 `#[cfg(any(test, feature = "test-support"))] pub mod test;`：`test` 模块（`src/test.rs`）是**要导出给下游复用的公共工具**，所以用「或」门控并且 `pub`。同一个声明区里，一个纯私有、一个可导出，差别就在这两行 cfg 属性上。

一个值得注意的细节：`proptest-derive` 是可选依赖，却**没有**出现在 `test-support` 的 feature 清单里，只在 dev-dependencies 中强制启用；而 `editor_tests.rs` 里的 `property_test` 模块又是用 `any(test, feature = "test-support")` 门控的。这意味着属性测试的宏派生目前主要保证「editor 自测」这条路畅通。这类「feature 清单与门控不完全对齐」的小缝隙在大 crate 里很常见，读到时多留个心眼，想想它是疏漏还是有意为之。

#### 4.1.4 代码实践

**实践目标**：把 `test-support` feature 的 11 个条目按「转发依赖 feature / 启用可选依赖」分类，并验证三层门控的编译差异。

**操作步骤**：

1. 打开 [Cargo.toml:L15-L31](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/Cargo.toml#L15-L31)，把 11 个条目抄进你的笔记，逐条标注它属于哪一类。
2. 对照 [Cargo.toml:L88-L97](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/Cargo.toml#L88-L97) 确认「可选依赖」类条目确实都带 `optional = true`。
3. （可选，待本地验证）在仓库根目录执行 `cargo tree -p editor --features test-support -i proptest`，观察 `proptest` 是被谁、经由哪条 feature 边引入依赖树的。

**需要观察的现象**：分类结果应该是 8 条转发 + 6 条可选依赖启用（清单里 `tree-sitter-*` 占 4 条）。

**预期结果**：你会发现自己能不看答案说出「为什么 `cargo test -p editor` 不需要 `--features test-support`」——因为 `cfg(test)` 为真，可选依赖由 dev-dependencies 兜底。`cargo tree` 的具体输出**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `#[cfg(any(test, feature = "test-support"))] pub mod test;` 改成只写 `#[cfg(test)] mod test;`，下游 crate 会发生什么？

**答案**：下游（如 vim crate）的测试将无法 `use editor::test::...`，因为 `cfg(test)` 在编译依赖时恒为假，模块根本不会被编译；即便编译了，非 `pub` 也导不出去。这正是 editor 要用「或」门控加 `pub` 的原因——同一份工具，两个入口条件。

**练习 2**：`[dev-dependencies]` 里的 `gpui = { workspace = true, features = ["test-support"] }`，与 `[features]` 里的 `"gpui/test-support"`，两者会在什么场景下分别生效？

**答案**：前者只在**编译 editor 自己的测试**时生效；后者在**下游打开 editor 的 test-support feature** 时，把 gpui 的 test-support 一并带上。两条路最终都保证「用到测试工具的编译单元里，gpui 也处于测试模式」。

**练习 3**：为什么 `doctest = false` 对 editor 是合理的选择？

**答案**：文档示例通常假设只有 crate 本身可用，而 editor 的几乎所有类型都依赖窗口、主题、设置等完整环境，单独编译成 doctest 会大量失败；项目选择用 4 万余行的真实测试代替文档示例测试。

### 4.2 src/test.rs：面向下游的公共测试工具箱

#### 4.2.1 概念说明

`src/test.rs` 只有 293 行，却是 53 个下游 crate 天天在用的「测试工具箱」。它解决的问题是：**editor 的测试需要反复做同样几件事**——构造字体、把带标记的文本变成快照、给编辑器设置选区、把编辑器内容连同块一起吐成字符串。这些能力抽出来放到 `pub mod test` 里，配合 `#[cfg(any(test, feature = "test-support"))]` 门控，就成了整个编辑器技术栈共享的测试基础设施。

#### 4.2.2 核心流程

`test.rs` 的函数可以按「测试生命周期」排成一条链：

```text
准备环境        test_font()                    固定的测试字体（非 Windows 用 Helvetica）
     │
构造数据        marked_display_snapshot()      "带|标记的文本" → (DisplaySnapshot, 各标记的 DisplayPoint)
     │
构造编辑器      build_editor()/build_editor_with_project()
     │          用 MultiBuffer 直接造一个 full 模式的 Editor
设置选区        select_ranges()                用 «»/|| 标记文本描述想要的选区，写进编辑器
     │
断言结果        assert_text_with_selections()  编辑器实际文本+选区 → 反生成标记文本 → 整体比较
     │
块级断言        editor_content_with_blocks()   渲染整个编辑器，把自定义块/头部块也序列化进字符串
```

#### 4.2.3 源码精读

模块顶部先声明两个子模块，它们是更高层的测试上下文（本讲只建立位置认知）：

- [src/test.rs:L1-L2](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/test.rs#L1-L2)：`pub mod editor_lsp_test_context;` 与 `pub mod editor_test_context;`。前者用于搭假 LSP 服务器（第 6 单元），后者提供声明式断言（第 8 单元精读）。

测试进程启动时的第一件事是初始化日志：

- [src/test.rs:L22-L26](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/test.rs#L22-L26)：用 `ctor` crate 的构造函数在**任何测试运行之前**调用 `zlog::init_test()`。`ctor` 会把函数变成「加载该动态库/二进制时执行」的初始化器，保证最早的日志也不会丢。

字体与标记文本快照：

- [src/test.rs:L28-L42](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/test.rs#L28-L42)：`test_font()` 用 `LazyLock` 缓存一个按平台选择的字体——测试里所有排版都基于它，保证结果确定。
- [src/test.rs:L44-L82](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/test.rs#L44-L82)：`marked_display_snapshot` 是「标记文本」与「显示坐标」之间的桥梁——先用 `marked_text_offsets` 剥掉 `|` 拿到干净文本和偏移列表，用 `MultiBuffer::build_simple` 建缓冲区，再为它单独建一个 `DisplayMap`（指定 `.ZedMono` 字体、14px），最后把每个标记偏移用 `to_display_point` 换算成 `DisplayPoint` 返回。注意这个函数**不需要 Editor**，它测的是坐标管线本身（第 3 单元会大量用到它）。

选区设置与断言，一对「写入/读出」函数：

- [src/test.rs:L84-L100](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/test.rs#L84-L100)：`select_ranges` 先断言编辑器现有文本与标记文本的「干净部分」一致（防止写错前提），再通过 `editor.change_selections(...)` 把 `marked_text_ranges` 解析出的区间设为选区。
- [src/test.rs:L102-L121](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/test.rs#L102-L121)：`assert_text_with_selections` 反向操作——取出编辑器当前文本与选区，用 `generate_marked_text` 重新生成带 `|`/`«»` 的标记文本，与期望值整体比较。第 118 行 `marked_text.contains("«")` 决定用单光标还是范围标记来渲染。两处断言都带了中文注释式的失败信息（`"text doesn't match"` / `"Selections don't match"`），失败时能立刻分辨是内容错了还是选区错了。

编辑器构造捷径：

- [src/test.rs:L123-L139](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/test.rs#L123-L139)：`build_editor` 与 `build_editor_with_project` 只是一行 `Editor::new(EditorMode::full(), ...)` 的封装，但让几百个测试的开头保持了同样的形状（第 2 单元会拆解 `Editor::new` 的全部参数）。

块内容的序列化断言（本讲了解即可）：

- [src/test.rs:L141-L157](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/test.rs#L141-L157)：`TestBlockContent` 是一个 GPUI 全局状态，按 `(编辑器实体 id, 块 id)` 存「这个块该显示什么字符串」的回调，`set_block_content_for_tests`/`block_content_for_tests` 是它的读写口。
- [src/test.rs:L173-L193](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/test.rs#L173-L193)：`editor_content_with_blocks` 先 `cx.simulate_resize` 再 `cx.draw` 真实渲染一次编辑器，随后把文本行与所有块（自定义块、摘录边界、缓冲区头部、spacer）拼成一个用 `§` 标记块行的字符串。这样「块也参与排版」这件事就能用纯字符串断言来验证（第 3 单元讲 BlockMap 时会回来细看）。

#### 4.2.4 代码实践

**实践目标**：掌握 marked text 的编码约定，能够双向读写这种记号。

**操作步骤**：

1. 阅读 [src/test.rs:L44-L82](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/test.rs#L44-L82) 与 [src/test.rs:L84-L100](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/test.rs#L84-L100)，确认 `|` 与 `«»` 各自被谁解析。
2. 在 `src/editor_tests.rs` 里执行（只读操作）：`grep -c 'select_ranges(' src/editor_tests.rs`，数一数这个工具被调用了多少次，再随机挑一个调用点阅读其上下文测试。
3. 手工推演：如果期望状态写成 `"abc«def»gh|ij"`，编辑器实际文本是什么？有几个选区、几个光标？各自覆盖/位于哪些偏移？

**需要观察的现象**：`select_ranges` 在测试文件中出现频率极高——它是「准备阶段」的标准姿势。

**预期结果**：推演答案见下面练习 1。grep 的具体次数**待本地验证**（本讲不预先给出，请以你机器上的输出为准）。

#### 4.2.5 小练习与答案

**练习 1**：`"abc«def»gh|ij"` 描述了怎样的状态？

**答案**：实际文本是 `abcdefghij`；一个选区覆盖偏移 3..6（即 `def`），一个光标位于偏移 8（`h` 与 `i` 之间）。注意 editor 支持多光标，「选区 + 光标并存」是合法状态。

**练习 2**：`marked_display_snapshot` 为什么不构造 `Editor`，而是直接建 `MultiBuffer` 和 `DisplayMap`？

**答案**：它服务于「坐标换算」类测试——只需要从文本偏移到显示坐标的映射（`to_display_point`），不涉及选区、滚动、渲染等 Editor 层的状态。少建一层实体，测试更快也更聚焦。这也提示了 editor crate 的分层：**坐标变换属于 DisplayMap，不属于 Editor**（第 3 单元主线）。

**练习 3**：`assert_text_with_selections` 失败时，输出里能直接看出「文本对了但选区错了」吗？

**答案**：能。函数做了两次独立断言：先 `assert_eq!(editor.text(cx), unmarked_text, "text doesn't match")`，再用生成的标记文本断言 `"Selections don't match"`。若第一次通过而第二次失败，说明内容一致、选区位置不对。

### 4.3 editor_tests.rs 的组织方式：507 个 `#[gpui::test]` 与 `init_test`

#### 4.3.1 概念说明

`src/editor_tests.rs` 有 43366 行，是整个 crate 最大的文件之一。它不是「一个模块一个测试文件」的风格，而是**把编辑器的行为测试集中在一个文件里**，以 `use super::*` 直接复用 `editor.rs` 的全部名字空间。理解它的组织方式，比逐行读完重要得多——本讲给你一张「按形状分类」的地图。

#### 4.3.2 核心流程

一个典型测试的骨架：

```text
#[gpui::test]
fn test_xxx(cx: &mut TestAppContext) {
    init_test(cx, |_| {});                     // ① 初始化应用环境
    let buffer = cx.new(|cx| MultiBuffer::singleton(
        language::Buffer::local("...", cx), cx));   // ② 造缓冲区
    let editor = cx.add_window(|window, cx|    // ③ 造窗口 + 编辑器
        build_editor(buffer, window, cx));
    _ = editor.update(cx, |editor, window, cx| {   // ④ 在窗口上下文里操作
        editor.insert(..., window, cx);
        assert_eq!(editor.text(cx), "...");
    });
}
```

文件里能见到四种形状：

| 形状 | 标记 | 代表 | 适用场景 |
| --- | --- | --- | --- |
| 同步 GPUI 测试 | `#[gpui::test] fn ...(cx: &mut TestAppContext)` | `test_edit_events` | 不需要等待异步任务 |
| 异步 GPUI 测试 | `#[gpui::test] async fn ...` | 大量 LSP 相关测试 | 需要-await 后台任务（文件里有 444 处 `async fn`，含测试本体与异步辅助函数） |
| 纯逻辑测试 | 顶格 `#[test]`，无 `cx` | `test_split_words` | 不碰 GPUI 的纯函数 |
| 属性测试 | proptest | `editor_tests/property_test.rs` | 随机编辑序列的性质校验 |

#### 4.3.3 源码精读

文件第一行就是它的组织秘诀：

- [src/editor_tests.rs:L1](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor_tests.rs#L1)：`use super::*;`——`editor_tests` 是 `editor.rs` 的子模块，`super::*` 把库根的所有 `use` 与类型全部拉进来，所以 4 万行测试里可以直接写 `Editor`、`MultiBufferOffset`、`Undo` 这些名字而不用重复导入。这是「巨型测试文件」能保持可写的关键。

断言的美观度专门引入了第三方库：

- [src/editor_tests.rs:L45](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor_tests.rs#L45)：`use pretty_assertions::{assert_eq, assert_ne};`——用彩色逐行 diff 替代标准库的原始输出。字符串内容断言遍布全文件（共 1049 处 `assert_eq!`），失败时的可读性完全靠它。

模块内部再挂一个属性测试子模块：

- [src/editor_tests.rs:L88-L89](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor_tests.rs#L88-L89)：`#[cfg(any(test, feature = "test-support"))] pub mod property_test;`——和 `test` 模块用同一套门控，说明属性测试也计划对下游开放（呼应 4.1 提到的 proptest-derive 细节）。

第一个测试就是「同步形状」的样板：

- [src/editor_tests.rs:L91-L92](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor_tests.rs#L91-L92)：`#[gpui::test] fn test_edit_events(cx: &mut TestAppContext)`，函数体第一行同样是 `init_test(cx, |_| {});`。它验证「编辑操作会以事件形式广播给订阅者」，第 8 单元讲工作区条目时会精读它。

而几乎所有测试的第一行 `init_test`，定义在文件靠后的位置：

- [src/editor_tests.rs:L36340-L36351](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor_tests.rs#L36340-L36351)：`init_test` 依次做六件事——`assets::Assets.load_test_fonts` 加载测试字体、`SettingsStore::test` 安装测试设置存储、`theme_settings::init` 初始化基础主题、`release_channel::init` 固定版本号、`crate::init` 执行 editor 自己的全局注册、`zlog::init_test` 初始化日志；最后 `update_test_language_settings` 应用调用方传入的语言设置覆盖（`f: fn(&mut AllLanguageSettingsContent)`——注意参数是裸函数指针，测试直接传闭包字面量 `|_| {}`）。**没有这一行，任何与设置、主题、字体相关的 Editor 构造都会失败**。

除了 `init_test`，文件尾部还沉淀了一批带 `#[track_caller]` 的断言辅助函数，`#[track_caller]` 让失败报错指向**调用处**而不是辅助函数内部：

- [src/editor_tests.rs:L82-L86](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor_tests.rs#L82-L86)：`display_ranges`——把编辑器选区转成显示坐标区间列表，一行工具函数也是测试基建。
- [src/editor_tests.rs:L36353-L36363](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor_tests.rs#L36353-L36363)：`assert_hunk_revert`——「设置状态 → 设置 diff 基线 → 跑完后台任务 → 断言 hunk 状态」的 git diff 场景三连（第 7 单元用到）。

最后看「纯逻辑形状」的样子：

- [src/editor_tests.rs:L23995-L23996](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor_tests.rs#L23995-L23996)：顶格 `#[test] fn test_split_words()`——不接收 `cx`、不需要 `init_test`，测的是纯字符串切分逻辑。全文件顶格 `#[test]` 只有 3 个（另外两个是 `test_split_words_for_snippet_prefix` 在 24011 行、`test_open_results_in_action_argument_parsing` 在 39664 行），其余 507 处全部是 `#[gpui::test]`。**如果你在这个文件里 grep `#[test]`，会得到「几乎没有测试」的错觉——这正是本讲综合实践要让你亲手踩一下的坑。**

#### 4.3.4 代码实践

**实践目标**：用正确的姿势统计这个巨型测试文件的测试数量，并体会「grep 什么关键词决定你看到什么世界」。

**操作步骤**（在 `crates/editor` 目录下执行）：

1. `grep -c 'gpui::test' src/editor_tests.rs`——统计 GPUI 测试宏的数量。
2. `grep -c '^#\[test\]' src/editor_tests.rs`——统计顶格标准测试的数量。
3. `grep -n '^#\[test\]' src/editor_tests.rs`——列出这三个「例外」的行号和名字，分别打开看一眼它们测的是什么。
4. `grep -c 'assert_eq!' src/editor_tests.rs`——感受断言密度。

**需要观察的现象**：第 1 步应得到 507，第 2 步应得到 3（以当前 HEAD `a7d74150` 为准；上游演进后数字会变，这正是要用命令而不是背数字的原因）。

**预期结果**：你会得出结论——「统计测试数」必须按测试宏的种类分别统计；`#[test]` 的 3 个结果完全不能代表 43366 行文件的测试规模。各命令的具体输出**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `editor_tests.rs` 能有 43366 行而不拆分？

**答案**：一方面 `use super::*`（第 1 行）免去了巨型导入块，测试之间零耦合、可以随意增删；另一方面它本身就是「按行为而非按模块」组织的测试集合，拆分收益低。项目约定（见仓库 CLAUDE.md「优先在既有文件中实现功能」）也倾向于避免大量小文件。

**练习 2**：`init_test` 的第二个参数为什么是 `fn(&mut AllLanguageSettingsContent)` 而不是 `impl Fn(...)`？

**答案**：它只需要「对默认语言设置做一次固定修改」的能力，不需要捕获环境；用裸函数指针明确表达「传一个无状态的设置改写函数」即可，例如需要修改标点自动配对设置的测试会传入具体的改写闭包字面量。

**练习 3**：一个测试如果忘写 `init_test(cx, |_| {})` 会怎样？

**答案**：凡是依赖全局设置存储、主题、测试字体的构造路径（例如 `Editor::new` 内部读取 `EditorSettings`）会因为全局未初始化而失败。所以它成了几乎每个测试的第一行——把它当作这个测试世界的「开机键」。

### 4.4 精读 test_undo_redo_with_selection_restoration

#### 4.4.1 概念说明

现在把前面所有知识用在一个真实测试上。`test_undo_redo_with_selection_restoration` 验证三件事：

1. **事务分组**：时间上接近（在 `group_interval` 内）的多笔编辑会被合并成一次撤销；
2. **选区恢复**：撤销/重做不仅恢复文本，还恢复**当时的选区/光标位置**；
3. **跨编辑器边界**：发生在别的编辑器（直接操作缓冲区）的事务，即便时间接近也不合并，且撤销时不恢复选区。

这个测试是第 4 单元「编辑事务」（u4-l5）的预告片，现在只需看懂它的**结构**；分组与恢复的内部实现在那一讲展开。

#### 4.4.2 核心流程

测试的推进过程（文本列即 `editor.text(cx)` 的值）：

```text
初始                "123456"
事务①（本编辑器，t0）选 2..4，插入 "cd"   → "12cd56"   光标 4..4
事务②（本编辑器，t0）选 4..5，插入 "e"    → "12cde6"   光标 5..5
时间前进超过 group_interval
事务③（另一编辑器，直接改 buffer）
                     0..1 改成 "a"，1 处插 "b" → "ab2cde6"  光标 3..3
undo（撤销③）        → "12cde6"   光标 2..2   ← 不恢复选区，回到撤销前手动选的位置
undo（①②一起撤销）  → "123456"   光标 0..0   ← 恢复事务①之前的选区
undo（栈已空，no-op） → "123456"
redo（重做①②）      → "12cde6"   光标 5..5
redo（重做③）        → "ab2cde6"  光标 6..6
start_transaction + end_transaction（空事务，不进栈）
undo（撤销的是③）    → "12cde6"
```

分组判定的直觉：**同一编辑器、间隔小于 `group_interval` 的连续事务视为一组**，撤销以组为单位；组撤销时顺带恢复该组开始前的选区快照。

#### 4.4.3 源码精读

- [src/editor_tests.rs:L220-L231](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor_tests.rs#L220-L231)：测试声明与环境准备——`init_test` 开机；创建一个本地 `language::Buffer`，内容 `"123456"`，并 `set_group_interval(Duration::from_millis(1))` 把分组窗口压到 1 毫秒，让后面的 `now += ...` 能明确跨越窗口；`MultiBuffer::singleton` 把单缓冲区包装成 editor 使用的多缓冲形态（第 2 单元讲这两个类型的关系）。

- [src/editor_tests.rs:L232-L232](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor_tests.rs#L232)：`cx.add_window(|window, cx| build_editor(buffer.clone(), window, cx))` 创建窗口并在其中构造编辑器，返回的 `editor` 是一个窗口句柄，后续所有操作都要通过 `editor.update(cx, ...)` 进入。

- [src/editor_tests.rs:L234-L246](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor_tests.rs#L234-L246)：事务①的完整写法——`start_transaction_at(now, window, cx)` 显式开启事务（用手工控制的时钟 `now` 而非真实时间，保证测试确定性）；`change_selections` 把选区设为偏移 2..4；`insert("cd", window, cx)` 在选区处插入（选中即替换）；`end_transaction_at(now, cx)` 提交。随后立刻断言文本与光标位置——**每一小步都断言，是这类行为测试的基本功**。

- [src/editor_tests.rs:L248-L258](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor_tests.rs#L248-L258)：事务②同构，选中 4..5 插入 `"e"`。两笔事务都发生在同一个 `now`，落在同一个分组窗口内。

- [src/editor_tests.rs:L260-L279](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor_tests.rs#L260-L279)：先 `now += group_interval + 1ms` 把时间推过分组窗口，然后**绕过编辑器直接在 buffer 上开事务**：`buffer.update(cx, |buffer, cx| { buffer.start_transaction_at(now, cx); buffer.edit(...); ... })` 模拟「另一个编辑器改了同一个文件」。注释明确写着 `// Simulate an edit in another editor`。

- [src/editor_tests.rs:L289-L304](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor_tests.rs#L289-L304)：三次撤销验证两种行为——撤销③时文本回到 `"12cde6"`，光标停在 `2..2`（即撤销前手动选择的位置，**没有**恢复旧选区）；继续撤销两次（第二次栈已空，注释标明 `this is a no-op`），①②作为一组被同时撤销，文本回到 `"123456"`，光标恢复到事务①开始前的 `0..0`。

- [src/editor_tests.rs:L307-L320](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor_tests.rs#L307-L320)：两次重做的镜像验证——①②作为一组重做（光标回到 5..5），③单独重做（光标到 6..6）。

- [src/editor_tests.rs:L322-L326](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor_tests.rs#L322-L326)：空事务收尾——`start_transaction_at` 后立刻 `end_transaction_at`，没有任何编辑，这样的空事务不进入撤销栈，因此随后的 `undo` 撤销的是前一笔真实事务③，文本回到 `"12cde6"`。

注意整个测试主体被包在 `_ = editor.update(cx, |editor, window, cx| { ... });` 里：`update` 返回闭包的返回值（这里是 `()`），用 `_ =` 显式丢弃。这与仓库规范「不要用 `let _ =` 静默丢弃可失败操作」并不冲突——`update` 不是可失败操作，这里只是标注「无需返回值」。

#### 4.4.4 代码实践

**实践目标**：跑通这个测试，再故意让它失败，读懂失败输出，最后完整还原。

**操作步骤**：

1. 进入 Zed 仓库根目录（`crates/editor` 的上两级）。
2. 运行单个测试（首次会编译整个依赖树，耗时较长，具体时间**待本地验证**）：

   ```bash
   cargo test -p editor test_undo_redo_with_selection_restoration
   ```

3. 观察输出形状：`running 1 test`、`test editor_tests::test_undo_redo_with_selection_restoration ... ok`，以及最后的统计行 `test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; N filtered out; ...`——`filtered out` 的数量就是被你的过滤词排除掉的其他测试。
4. 故意制造失败：把 [src/editor_tests.rs:L242](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor_tests.rs#L242) 的

   ```rust
   assert_eq!(editor.text(cx), "12cd56");
   ```

   改成

   ```rust
   assert_eq!(editor.text(cx), "99cd56");
   ```

   重新运行同一条命令。
5. 阅读失败输出：pretty_assertions 会把左右两侧按字符对齐打印成 diff 块，报错位置直接指向 242 行。
6. 还原修改：

   ```bash
   git restore crates/editor/src/editor_tests.rs
   ```

   再次运行，确认回到 `1 passed`。

**需要观察的现象**：失败输出中 diff 的左右两侧分别是 `12cd56` 与 `99cd56`；`test result` 行变为 `1 failed`；其余测试仍然显示为 filtered out（因为过滤词没变）。

**预期结果**：你获得了「定位 → 修改 → 观察 → 还原」的完整闭环经验。注意：这是在你本地副本上的临时实验，观察完务必还原，不要提交（具体耗时与 `filtered out` 数值**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1**：为什么测试要用 `start_transaction_at(now, ...)` 手工传时间，而不是直接 `insert` 让编辑器自己开事务？

**答案**：分组规则依赖「两笔事务之间的时间间隔是否小于 group_interval」。用手工时钟 `now: Instant` 可以精确控制事务①②「同刻发生」、事务③「跨越窗口」，测试因此完全确定，不受真实时间抖动影响。

**练习 2**：撤销事务③后光标为什么在 `2..2`，而不是事务③开始前的某个位置？

**答案**：事务③不是本编辑器发起的（直接改 buffer），编辑器没有为它记录选区快照；撤销后编辑器只把光标保持在用户当前选定的位置（测试前面手动选了 `2..2`）。选区恢复是「本编辑器成组撤销」才有的待遇。

**练习 3**：如果把 `set_group_interval` 的参数从 1 毫秒改成 1 秒，同时保持 `now += group_interval + 1ms` 的写法，测试还会通过吗？

**答案**：仍会通过，因为测试用的是 `group_interval` 变量参与推算（`now += group_interval + Duration::from_millis(1)`），跨越窗口的关系不随具体数值改变。反过来说，如果写死成 `now += Duration::from_millis(2)`，改成 1 秒后事务③就会和①②落进同一窗口，撤销行为改变，断言将失败——这提示你：**改参数观察行为**是最直接的源码实验手段。

## 5. 综合实践

把本讲四个模块串成一个完整流程。全部操作在你的本地副本进行，最后保证 `git status` 干净。

**任务：给「测试基建」建立一份你自己的体检报告。**

1. **分类**（对应 4.1）：写出 `test-support` feature 的 11 个条目分类表（转发 8 条 / 可选依赖 6 条，其中 tree-sitter 占 4 条），并用一句话回答「为什么 `cargo test -p editor` 不需要 `--features test-support`」。
2. **统计**（对应 4.3）：在 `crates/editor` 下执行：

   ```bash
   grep -c 'gpui::test' src/editor_tests.rs
   grep -c '^#\[test\]' src/editor_tests.rs
   grep -n '^#\[test\]' src/editor_tests.rs
   ```

   把三个数字记入报告，并回答：如果同事只 grep 了 `#[test]` 就说「这个文件几乎没测试」，你的反驳证据是什么？
3. **运行**（对应 4.4）：执行 `cargo test -p editor test_undo_redo_with_selection_restoration`，记录编译耗时与运行耗时（**待本地验证**）。
4. **破坏性实验**（对应 4.4）：把 242 行的期望文本改错，运行并保存失败输出到你的笔记（不要写进仓库）；再 `git restore` 还原并确认通过。
5. **溯源**（对应 4.2/4.3）：在测试文件中找到 `init_test` 的定义（36340 行附近），列出它初始化的六个组件；再找到 `select_ranges` 的定义（src/test.rs），说明「`|` 与 `«»` 分别由哪个工具函数解析」。

**验收标准**：报告能回答以上全部问题，且 `git status` 显示工作区无改动。耗时数据因机器而异，标注「待本地验证」即可。

## 6. 本讲小结

- editor 的测试开关是**三层设计**：`[features]` 的 `test-support` 是对下游的总开关（转发 8 个依赖的 feature + 启用 tree-sitter/proptest/unindent 等可选依赖）；`[dev-dependencies]` 保证 editor 自测时这些依赖必然可用；源码用 `#[cfg(any(test, feature = "test-support"))]` 同时放行「自己测试」与「下游复用」两条路。
- `#[cfg(test)] mod editor_tests;` 与 `#[cfg(any(test, feature = "test-support"))] pub mod test;` 的对比是理解这套设计的最短路径：前者纯私有，后者可导出。
- `src/test.rs` 是 53 个下游 crate 共享的测试工具箱：marked text（`|` 光标、`«»` 选区）的双向转换、`build_editor` 构造捷径、`editor_content_with_blocks` 把块序列化进字符串断言。
- `src/editor_tests.rs` 有 43366 行、507 处 `#[gpui::test]`、仅 3 个顶格 `#[test]`；几乎每个测试的第一行 `init_test(cx, |_| {})` 负责安装字体、设置存储、主题、发布渠道与 editor 全局注册——它是这个测试世界的开机键。
- `test_undo_redo_with_selection_restoration` 展示了行为测试的标准写法：手工时钟 + 显式事务 + 每步断言；它验证的「事务分组 + 撤销恢复选区 + 跨编辑器不合并」是第 4 单元编辑事务机制的预告。
- 「运行 → 改断言 → 读失败输出 → 还原」是后续所有讲义通用的实验方法，`git restore` 是你的安全网。

## 7. 下一步学习建议

下一讲（u1-l3《模块地图》）将把上一讲的五类模块分类深化为**带依赖方向的模块地图**：你会用 grep 验证 `element` 如何引用 `display_map`、`editor` 如何引用 `element/items/git`，并亲手画出一张依赖草图——那也是本系列第一张需要你动笔画图的作业。

在此之前的巩固建议：

1. 重跑一遍综合实践，直到不需要翻讲义就能说出 `test-support` 与 dev-dependencies 的分工。
2. 随机打开 `editor_tests.rs` 中三个不同位置的 `#[gpui::test]`（例如一个同步、一个 `async fn`、一个用到 `EditorLspTestContext` 的），只看结构不看细节，确认你能说出它们各自的「①②③④」骨架。
3. 保存好你的「体检报告」，第 8 单元（u8-l5 性能与测试基建）会回头补充 `EditorTestContext`、`EditorLspTestContext` 与 property_test 的深入内容。
