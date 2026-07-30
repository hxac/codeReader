# 测试与 smoke 测试

## 1. 本讲目标

本讲是 typst-cli 学习手册专家层的最后一篇，聚焦 `tests/smoke.rs` 这一个文件。前面十几篇讲义都是在「读源码、理解逻辑」，本讲换一个视角：**项目自己如何验证这套 CLI 真的能用？**

读完后你应该掌握：

- 理解 typst-cli 为什么用「**子进程端到端测试（smoke test）**」而不是普通单元测试来验证 CLI，以及它如何借助 Cargo 的 `CARGO_BIN_EXE_<name>` 环境变量定位刚刚编译出的 `typst` 二进制。
- 读懂 `smoke.rs` 里那套自制的、轻量的测试夹具（fixture）：`exec()`、`TempFs`、`Stream`，以及 `must_succeed` / `must_fail` / `must_contain` / `must_match_lines` 等断言辅助。
- 把 17 个测试函数按场景归类：编译/PDF、字体、依赖、包与路径解析、诊断与 tracepoints，理解它们各自验证了哪一条核心链路。
- 能够参考现有测试，为尚未覆盖的 CLI 选项（如 `--diagnostic-format short`）新增一个最小 smoke 测试。

## 2. 前置知识

本讲假设你已经读过：

- **u1-l2 入口与命令分发**：知道 `main()` 的退出码机制（`set_failed()` 软失败）——这一点对理解 `must_succeed` / `must_fail` 至关重要。
- **u2-l2 编译配置与单次编译**：知道 `compile_once` 在编译失败时返回 `Ok(())` 但把退出码改成非 0。
- **u2-l4 诊断与终端输出**：知道 `--diagnostic-format` 有 `human`（默认）与 `short` 两种，short 不含源码片段与 tracepoint。
- **u3-l1 字体发现 / u3-l2 包存储 / u3-l4 依赖追踪**：知道 `--font-path`、`--ignore-system-fonts`、`--package-path`、`--deps` 这些选项的语义。

下面补充几个本讲专有、前面没出现过的术语：

- **Smoke test（冒烟测试）**：源自硬件行业「通电看看会不会冒烟」。它不追求覆盖率，只验证「主要链路能跑通、产物大致正确」。在 CLI 场景，最忠实的做法是把 CLI 当成一个黑盒子，真正起一个子进程去跑它。
- **Fixture（测试夹具）**：为了让测试好写而准备的「脚手架代码」，例如创建临时目录、封装命令执行、提供断言方法。本讲的 `TempFs` 和 `Stream` 都是夹具。
- **`CARGO_BIN_EXE_<name>`**：Cargo 在运行集成测试时注入的环境变量，值是「本次构建产出的那个二进制」的绝对路径。对 typst-cli，`[[bin]] name = "typst"`（见 `Cargo.toml:15-18`），所以变量名是 `CARGO_BIN_EXE_typst`。
- **`#[track_caller]`**：Rust 属性，让被标注的函数在 panic 时，错误位置指向「调用它的那一行」而非函数内部。它让断言失败信息能精确定位到具体的测试用例。

## 3. 本讲源码地图

本讲几乎只读一个文件：

| 文件 | 作用 |
| --- | --- |
| [tests/smoke.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/tests/smoke.rs) | 唯一的集成测试文件。17 个 `#[test]` 函数 + 一套自制夹具（`exec` / `CommandExt` / `TestOutput` / `TempFs` / `Stream`）。 |

会顺带引用（但不展开）两个被夹具依赖的事实：

- `Cargo.toml` 的 `[[bin]]` 与 `[dev-dependencies]`，说明 `CARGO_BIN_EXE_typst` 的来源以及 `tempfile` / `memchr` / `typst-dev-assets` 三个开发依赖。
- `src/args.rs` 的 `DiagnosticFormat` 与 `ProcessArgs.diagnostic_format`，说明 `--diagnostic-format` 选项的定义位置。

> Rust 集成测试约定：`tests/` 目录下每个 `.rs` 文件都是一个**独立的 crate**，会被单独编译成一个测试二进制。所以 `cargo test --test smoke` 只跑 `smoke.rs` 这一个文件。正因为它独立编译，它看不到 `src/` 里的私有模块，只能通过「启动子进程」来测，这正好与 smoke 测试的黑盒理念吻合。

## 4. 核心概念与源码讲解

### 4.1 测试夹具：exec、CommandFs 与 Stream

#### 4.1.1 概念说明

测试一个 CLI 有两条路：

1. **直接调用函数**：把 `crate::compile::compile` 当函数调用。问题是它依赖大量全局状态（`ARGS` 用 `LazyLock` 解析真实的命令行、`terminal::out()` 是 singleton、退出码用 `thread_local`），在测试里很难伪造，而且一旦真的改了 `thread_local` 退出码，测试进程本身就被污染了。
2. **子进程黑盒测试**：启动一个真正的 `typst` 子进程，给它命令行参数，检查它的退出码和 stdout/stderr。

`smoke.rs` 选择了第二条路。这条路的代价是「慢」（每次测试都要起进程、要做真实编译），收益是「忠实」——它测的就是用户真正会跑到的二进制，包括 `clap` 参数解析、终端输出、退出码，一条都不会漏。

为了让第二条路写起来不啰嗦，`smoke.rs` 写了三个小夹具：

- `exec()`：构造一个指向 `typst` 二进制的 `Command`。
- `CommandExt` trait：给 `Command` 加 `must_succeed` / `must_fail`，执行并在退出码不符合预期时 panic。
- `TempFs`：包装 `tempfile::TempDir`，提供 `write` / `read` / `path` / `resolve`，方便在临时目录里摆出项目结构。
- `Stream<T>`：包装字节流（`Vec<u8>`），提供 `must_contain` / `must_start_with` / `must_match_lines` 等断言。

#### 4.1.2 核心流程

一个典型测试的生命周期是：

```text
tempfs()                 → 在系统临时目录建一个空目录，返回 TempFs
project.write("a.typ", …)→ 在该目录里写文件，返回绝对路径
exec().arg("compile")…   → 构造 typst 子进程命令
.must_succeed()          → 真正 .output() 执行；断言退出码 == 成功，返回 TestOutput
output.stdout            → 取 stdout（一个 Stream）
.must_contain("…")       → 断言 stdout 含某子串
// 临时目录随 TempFs 析构自动删除
```

断言失败时，因为 `#[track_caller]`，panic 会指向调用 `must_succeed()` / `must_contain()` 的那一行测试代码，而不是夹具内部。`must_succeed` 失败时还会把子进程的 stderr 打印出来，方便定位。

#### 4.1.3 源码精读

**定位被测二进制**——`exec` 是整个文件的入口，用 `env!` 宏在编译期读取 Cargo 注入的环境变量：

[tests/smoke.rs:258-261](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/tests/smoke.rs#L258-L261) 把 `CARGO_BIN_EXE_typst` 焊进测试二进制。这个变量名里的 `typst` 来自 `Cargo.toml` 的 `[[bin]] name = "typst"`。

**执行并断言退出码**——`CommandExt` 把「执行 + 判退出码」合二为一：

[tests/smoke.rs:263-287](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/tests/smoke.rs#L263-L287) 中，`must_succeed` 断言 `output.status.success()`（退出码为 0），失败时用 `Stream(output.stderr)` 把子进程错误打印出来；`must_fail` 断言其反面。注意这里的「成功/失败」直接对应进程退出码——这正是 u1-l2 讲的软失败机制的「用户侧观测点」：编译报错时 `typst compile` 的退出码非 0，`must_fail` 才会通过。

**包装输出流**——`TestOutput` 只是把 `Output` 的 stdout / stderr 包成 `Stream`：

[tests/smoke.rs:289-301](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/tests/smoke.rs#L289-L301) 让后续可以分别对 stdout 和 stderr 做断言。

**临时目录夹具**——`TempFs` 是写测试最常用的工具：

[tests/smoke.rs:303-333](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/tests/smoke.rs#L303-L333) 中，`tempfs()` 用 `tempfile::tempdir()` 建目录；`write` 会自动 `create_dir_all` 出父目录（所以可以写 `dir/a.typ` 这种嵌套路径）并返回绝对路径；`read` 读回字节供断言。`TempDir` 析构时自动删目录，测试不会留下垃圾。

**断言辅助**——`Stream` 是泛型 `Stream<T = Vec<u8>>`，三个核心断言：

[tests/smoke.rs:335-371](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/tests/smoke.rs#L335-L371) 提供三种断言：

- `must_contain`：子串包含，底层用 `memchr::memmem::find`（高性能字节子串搜索），所以能匹配二进制内容（如 PDF 的 `%PDF` 头）。
- `must_start_with`：前缀匹配，用于校验文件头或固定首行（如 `info` 的 `Version` 开头）。
- `must_match_lines`：**整行精确匹配**，把字节按行切分后与期望的行数组逐一 `assert_eq!`，适合校验「输出恰好是这几行」（如 `eval "1+2"` 必须只输出一行 `3`）。

注意 `must_contain` / `must_start_with` / `must_match_lines` 都返回 `&Self`，所以可以链式调用：`.must_contain("a").must_contain("b")`。`must_contain` 接收 `impl Debug + AsRef<[u8]>`，因此既能传 `&str` 也能传 `&[u8]` / `Vec<u8>`——`test_compile_pdf` 就传了 `title`（字符串）和 `.as_bytes()`（字节）两种。

`Debug`/`Display` 实现（[tests/smoke.rs:373-383](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/tests/smoke.rs#L373-L383)）用 `String::from_utf8_lossy` 把字节渲染成可读文本，保证 panic 信息里看到的是文字而不是一堆数字。

#### 4.1.4 代码实践

**实践目标**：亲手跑一次 smoke 测试，观察夹具是如何把「起进程 + 断言」变成一行链式调用的。

**操作步骤**：

1. 在 `crates/typst-cli` 目录下执行 `cargo test --test smoke -- --nocapture`。`--test smoke` 指定只编译并运行 `tests/smoke.rs`；`--nocapture` 让测试里若有 `println!` 也能显示出来。
2. 观察输出：每个测试名前会出现 `test test_compile_pdf ... ok` 这样的行。注意第一条测试 `test_help` 会真正打印帮助文本（因为它校验 `--help` 输出）。
3. 临时改坏一个测试以观察失败信息：在本地副本里把 `test_compile_pdf` 的 `must_succeed()` 改成 `must_fail()`，重新运行，观察 panic 信息如何（a）指向你改的那一行（`#[track_caller]` 的功劳），（b）把子进程的 stderr 完整打印出来。

**需要观察的现象**：

- 全绿时 17 个测试全部通过、耗时在秒级（因为每个测试都要起子进程并真实编译）。
- 故意改坏时，失败信息里能看到子进程的 stderr，定位到具体测试行。

**预期结果**：未改坏时全部 `ok`；改坏后该测试 `FAILED` 且信息包含子进程输出。删除你的临时改动恢复原状。

> 说明：本实践需要本地能 `cargo build` 整个 typst-cli（含默认 feature `embedded-fonts`、`http-server`）。若构建环境受限，可只做「阅读源码 + 在脑中模拟」而不实际执行，标记为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Stream::must_contain` 用 `memchr::memmem::find` 而不是 `str::contains`？

<details><summary>参考答案</summary>

`str::contains` 要求内容是合法 UTF-8 字符串，而 smoke 测试经常要检查二进制产物（PDF 头 `%PDF`、PNG 头等），这些字节流未必全程合法 UTF-8。`memchr::memmem::find` 直接在字节层面搜索子串，既快又能处理任意字节，配合 `AsRef<[u8]>` 接口可以同时容纳 `&str` 和 `&[u8]`。

</details>

**练习 2**：`must_succeed` 在失败断言里为什么把 `Stream(output.stderr)` 打印出来，而 `must_fail` 没有？

<details><summary>参考答案</summary>

预期成功的命令如果失败了，原因几乎总在 stderr（编译报错、应用级错误都打到 stderr），所以把它打印出来便于排查；而预期失败的命令，stderr 往往就是测试接下来要用 `must_contain` 校验的「预期错误」，测试自己会断言它，不必在 panic 信息里重复。

</details>

### 4.2 编译与 PDF 测试

#### 4.2.1 概念说明

这是最基本的一类 smoke 测试：**给一个 `.typ` 输入，编译出文件，校验产物**。它验证的是 u2-l2/u2-l3 讲的「`compile_once` → `compile_and_export` → 落盘」整条链路真的能端到端跑通，而不只是函数返回了 `Ok`。

两个测试递进地覆盖 PDF 产物：

- `test_compile_pdf`：校验产物「是 PDF 且含预期内容」。
- `test_compile_pdf_version`：校验产物里的 `/Creator` 元数据嵌入的是「当前版本号」，把 `--version` 与 PDF 元数据联动起来。

#### 4.2.2 核心流程

```text
test_compile_pdf:
  建临时目录 → 写 hello.typ（含 #set document(title: "...")）
  → typst compile hello.typ（默认输出 hello.pdf）
  → 读回 hello.pdf：
      must_start_with("%PDF")     ← PDF 魔数头
      must_contain(title)         ← 文档标题被写进 PDF

test_compile_pdf_version:
  typst --version → 解析第二个空白分隔单词（版本号）
  → typst compile hello.typ
  → hello.pdf 含 /Creator(Typst <version>)
```

#### 4.2.3 源码精读

[tests/smoke.rs:18-25](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/tests/smoke.rs#L18-L25) 是最典型的 smoke 用例：写文件、编译、读回、断言产物。`project.read("hello.pdf")` 返回 `Stream<Vec<u8>>`，于是能同时用 `must_start_with`（PDF 头）和 `must_contain`（标题字节）。这里没指定 `--format` 或 `--output`，靠默认推断（输出路径换扩展名、格式由扩展名推断），间接也测了 u2-l2 的输出格式推断逻辑。

[tests/smoke.rs:27-42](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/tests/smoke.rs#L27-L42) 把 `--version` 输出与 PDF 元数据串起来。`output.stdout.lines()...nth(1)` 取 `typst <version> (...)` 这一行里的第二个空白分隔词。然后断言 PDF 里含 `/Creator(Typst <version>)`——这验证了编译器版本号确实被写进了 PDF 的文档信息字典。

此外，`test_target_available`（[tests/smoke.rs:251-256](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/tests/smoke.rs#L251-L256)）用一个 `#context target()` 的文档确认「当前构建确实启用了某个 target feature」，是一个极简的「能编译就算过」冒烟检查。

#### 4.2.4 代码实践

**实践目标**：理解 PDF 产物里的魔数与元数据。

**操作步骤**：

1. 自己写一个 `meta.typ`：`#set document(title: "My Smoke Title"); Hello`，用本地 `typst` 编译成 `meta.pdf`。
2. 用任意十六进制/文本查看器看 `meta.pdf` 的前几个字节，确认以 `%PDF` 开头（PDF 规范要求的魔数）。
3. 在文件里搜索 `Creator`，应能看到 `/Creator(Typst <版本>)`；再搜索 `Title`，应能看到你设置的标题。

**需要观察的现象**：`%PDF` 出现在文件最开头；`/Creator` 与 `/Title` 出现在 PDF 的信息字典附近。

**预期结果**：与你从源码推断的一致。若用默认 feature 编译，版本号与 `typst --version` 第二个单词一致。

#### 4.2.5 小练习与答案

**练习 1**：`test_compile_pdf_version` 为什么用 `.flat_map(|line| line.split_whitespace()).nth(1)` 而不是直接取第一行的第二个字符？

<details><summary>参考答案</summary>

`typst --version` 的输出形如 `typst 0.13.0 (...)`，想要的版本号是第二个空白分隔的「单词」。先 `lines()` 再 `split_whitespace()` 再 `flat_map` 展平成一个单词流，`nth(1)`（0-indexed 的第二个）正好取到版本号，对「第一行可能有多个括号注释」也更鲁棒。

</details>

### 4.3 字体与依赖测试

#### 4.3.1 概念说明

这一组覆盖 u3-l1（字体发现）和 u3-l4（依赖导出）：

- `test_fonts_embedded` / `test_fonts_path`：验证 `typst fonts` 能列出内嵌字体，也能扫描自定义路径字体，且结果与命令行开关一致。
- `test_deps`：验证 `typst compile --deps -` 能把一次编译真正访问过的文件写出来。

字体测试用到了开发依赖 `typst-dev-assets`——它是 typst 仓库内置的一组测试资源（字体、图片），让测试不依赖外部下载。

#### 4.3.2 核心流程

```text
test_fonts_embedded:
  typst fonts --ignore-system-fonts
  → stdout 用 must_match_lines 断言含 4 个内嵌字体族（顺序敏感）

test_fonts_path:
  遍历 typst_dev_assets::fonts()，把每个字体写成 <i>.ttf，并记录其 family
  typst fonts --ignore-embedded-fonts --ignore-system-fonts --font-path <dir>
  → 把 stdout 每行收集成 HashSet，与 expected 集合做 assert_eq!

test_deps:
  写 main.typ 引用 tiger.jpg，并把 tiger.jpg 落盘
  typst compile main.typ --deps -
  → stdout 必须含 tiger.jpg 和 main.typ
```

#### 4.3.3 源码精读

[tests/smoke.rs:50-59](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/tests/smoke.rs#L50-L59) 关掉了系统字体（`--ignore-system-fonts`），只看内嵌字体。`must_match_lines` 是**严格逐行匹配**，因此这里同时校验了字体族的**输出顺序**——内嵌字体列表是确定性的，顺序由 `discover_fonts` 的累加顺序决定（见 u3-l1）。

[tests/smoke.rs:61-83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/tests/smoke.rs#L61-L83) 更巧妙：它把 `typst-dev-assets` 里的每个字体数据先用 `typst::text::Font::new` 解析出真实的 `family`，作为期望集合；再把同样的字体文件铺到临时目录，用 `--font-path` 让 CLI 扫描；最后比对「CLI 扫描出来的族名集合」与「期望集合」是否相等。这里用 `HashSet` 而非 `must_match_lines`，是因为多个字体文件可能映射到同一个族名、且顺序不必敏感——集合相等才是正确的语义。这直接验证了 u3-l1 讲的「`discover_fonts` 的 scan 分支」。

[tests/smoke.rs:91-98](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/tests/smoke.rs#L91-L98) 验证依赖导出。`#image("tiger.jpg")` 让编译器真正读取了图片；`--deps -` 把依赖以默认 JSON 格式打到 stdout（见 u3-l4 的 `write_deps`）。断言 stdout 同时含 `tiger.jpg`（被访问的资源）和 `main.typ`（主文件），证明依赖清单来自「实际访问」而非「文档里写没写」。

#### 4.3.4 代码实践

**实践目标**：体会「字体可见性由开关决定」与「依赖来自实际访问」。

**操作步骤**：

1. 准备一个含两三个 `.ttf` 的目录 `myfonts/`，运行 `typst fonts --ignore-embedded-fonts --ignore-system-fonts --font-path myfonts/`，观察输出是否恰好是这几个字体的族名。
2. 写一个 `img.typ`：`#image("tiger.jpg")`（或任意本地图片），运行 `typst compile img.typ --deps -`，把 JSON 输出与源码里 `#image` 的写法对照。
3. 再写一个 `fake.typ`：在文件里用注释写上 `#image("never-loaded.jpg")`（用注释使其不被真正求值），重新 `--deps -`，观察 `never-loaded.jpg` **不会**出现在依赖里。

**需要观察的现象**：步骤 1 只列出 `--font-path` 指定的字体；步骤 2 的依赖含被读取的图片；步骤 3 注释掉的资源不出现在依赖里。

**预期结果**：与 u3-l1 / u3-l4 描述一致。步骤 3 是关键——它证明依赖清单由「实际访问」驱动。

#### 4.3.5 小练习与答案

**练习 1**：`test_fonts_path` 为什么用 `HashSet` 比较，而 `test_fonts_embedded` 用 `must_match_lines`？

<details><summary>参考答案</summary>

内嵌字体列表是固定的、顺序确定的，用 `must_match_lines` 能同时锁定「内容 + 顺序」；而扫描自定义目录时，字体文件的遍历顺序、同一族多个变体的去重都可能影响行序，测试关心的是「族名集合正确」而非顺序，所以用 `HashSet` 做无序比较。

</details>

### 4.4 包与路径解析测试

#### 4.4.1 概念说明

这一组覆盖 u2-l1（SystemWorld 路径解析）和 u3-l2（包存储）。它用一个**很聪明的共同技巧**：故意写出会 `#panic(42)` 的源码，让编译**失败**，然后通过断言错误信息里是否出现 `panicked with: 42` 来**间接证明某条 import/include 路径是否被正确解析到了目标文件**。如果路径解析错了，要么报「file not found / package not found」，要么 panic 的值不是 42。

这组测试包含：

- `test_path_resolved` / `test_path_unresolved` / `test_path_project_root`：相对路径、绝对路径（以 `/` 开头，相对项目根）、文件找不到。
- `test_package_resolved` / `test_package_unresolved` / `test_path_to_package`：本地包能解析、找不到包、以及「包内 vs 项目内」同名文件的路径上下文隔离。

#### 4.4.2 核心流程

以 `test_path_resolved` 为例（故意制造 panic 作为探针）：

```text
main.typ: #include "dir/a.typ"
dir/a.typ: #include "/dir/b.typ"            ← 绝对路径（相对项目根）
dir/b.typ: #import "../utils.typ": f; #f()! ← 跳出两级到 utils.typ
utils.typ: #let f() = panic(42)

typst compile main.typ → 失败 → stderr 含 "error: panicked with: 42"
```

只要这条链路上任何一处路径解析不对，就会报别的错误（找不到文件）而非 `panicked with: 42`，测试就会失败。

#### 4.4.3 源码精读

[tests/smoke.rs:100-109](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/tests/smoke.rs#L100-L109) 一次测了三种路径：相对包含（`"dir/a.typ"`）、绝对包含（`"/dir/b.typ"`，相对项目根，见 u2-l1 的 VirtualRoot）、`#import` 带 `..` 的相对跳转。探针 `panic(42)` 落在最末端的 `utils.typ`。

[tests/smoke.rs:111-120](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/tests/smoke.rs#L111-L120) 是「找不到文件」的负例：断言 stderr 同时含 `error: file not found` **和** `#include "other.typ"`（诊断会把出问题的源码片段/标签带出来）。

[tests/smoke.rs:122-134](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/tests/smoke.rs#L122-L134) 用 `--root` 指定项目根，验证 `/a.typ` 解析到 root 下的 `a.typ`（其内容是 `#panic(42)`）。

包测试里，[tests/smoke.rs:136-157](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/tests/smoke.rs#L136-L157) 在临时目录里手工铺出 `local/demo/0.1.0/` 的三层目录结构（对应 u3-l2 的 `namespace/name/version` 约定），写好 `typst.toml` 清单，再用 `--package-path` 指过去，验证 `@local/demo:0.1.0` 能被定位。探针同样落在包内最末端。

最精巧的是 [tests/smoke.rs:175-203](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/tests/smoke.rs#L175-L203) 的 `test_path_to_package`：项目根和包里各有一个 `a.typ`（内容不同），包内函数 `g(p)` 会 `import p: f`。调用 `g(path("a.typ"))`（项目内）得到 7，调用 `g("a.typ")`（包内，相对包根）得到 42，最后 `#panic((x, y))` 断言得到 `(7, 42)`——一次性证明「同一相对名 `a.typ` 在项目上下文与包上下文里解析到不同文件」，这是 u2-l1 讲的 VirtualRoot 隔离的关键体现。

[tests/smoke.rs:159-173](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/tests/smoke.rs#L159-L173) 是包的负例：`--package-path` 指向空目录，断言报错信息含 `error: package not found (searched for @local/demo:0.1.0)`，校验了 u3-l2「找不到包」的提示措辞。

#### 4.4.4 代码实践

**实践目标**：用 panic 探针法亲手验证一条 include 链。

**操作步骤**：

1. 仿照 `test_path_resolved`，在本地建一个临时目录，按 `main.typ` / `sub/a.typ` / `sub/b.typ` / `leaf.typ` 的结构写四个文件，在 `leaf.typ` 里放 `#panic(99)`。
2. 用相对、绝对（配合 `--root`）两种 include 串起来，运行 `typst compile main.typ`，观察 stderr 是否含 `panicked with: 99`。
3. 故意把其中一个 include 路径写错，观察错误信息如何从 `panicked with: 99` 变成 `file not found`。

**需要观察的现象**：路径正确时 panic 信息透传；路径错误时报 file not found 且诊断带出出错位置的源码片段。

**预期结果**：与源码断言一致。这个「把 panic 当探针」的技巧值得收藏——它把「路径是否解析对」这个难以直接观测的事实，转化成了「错误信息里是否出现某个确定值」这个容易断言的事实。

#### 4.4.5 小练习与答案

**练习 1**：`test_path_to_package` 里，为什么 `g(path("a.typ"))` 得到 7 而 `g("a.typ")` 得到 42？

<details><summary>参考答案</summary>

`path("a.typ")` 在调用点（项目根的 `main.typ`）求值成一个绝对路径，再传给包内函数，因此 `import p` 解析的是**项目**根下的 `a.typ`（`#let f() = 7`）；而字符串 `"a.typ"` 作为 `import` 的目标时是相对**当前文件（包内 lib.typ）**解析的，指向**包**里的 `a.typ`（`#let f() = 42`）。这正是 VirtualRoot 让项目与包拥有各自独立的路径根的结果。

</details>

### 4.5 诊断与 tracepoints 测试

#### 4.5.1 概念说明

最后一组覆盖 u2-l4（诊断输出）。`typst` 的诊断不只是「文件:行:列: 错误」，还会带 **tracepoint**——类似栈帧的「上下文面包屑」，例如 `while including 'chapter1.typ' at ...`、`while calling 'my-figure' at ...`。这些 tracepoint 对定位「错误到底是在哪一层调用链里触发的」非常关键，也正因为它们与终端渲染强相关，用 smoke 测试来守卫最合适。

两个测试：

- `test_tracepoints`：构造一个「show 规则触发 include 字符串拼接」的复杂场景，验证多级 tracepoint 全部正确显示。
- `test_network_access_hint`：验证文档里尝试访问网络（`#image("https://...")`）时，CLI 给出 `hint: network access is not supported` 提示。

#### 4.5.2 核心流程

`test_tracepoints` 的源码本身就是「极限情况」：用 `#show strong: it => include "chap"+"ter1.typ"`，让一个 `*strong text*` 在展示时**动态拼接出一个文件名并 include**，进而触发包内 `my-figure`。这条链路横跨 show 规则、动态 include、import，产生多层 tracepoint：

```text
编译失败，stderr 应含三组 tracepoint：
  while calling `my-figure` at … / chapter1.typ:2:12 / my-figure(…)
  while including `chapter1.typ` at … / main.typ:1:19 / include "chap" + "ter1.typ"
  while showing strong element at … / main.typ:2:11 / *Slightly unusual…*
```

#### 4.5.3 源码精读

[tests/smoke.rs:216-249](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/tests/smoke.rs#L216-L249) 对同一份 stderr 连续做多次 `must_contain`，分别校验「tracepoint 标题」「源码定位（file:line:col）」「源码片段」三层信息。注意它用的是默认的 **human** 诊断格式（没传 `--diagnostic-format`），因为 tracepoint 和源码片段**只在 human 格式下才输出**——这一点会在本讲的实践任务里再次用到。

[tests/smoke.rs:205-214](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/tests/smoke.rs#L205-L214) 的注释解释了「为什么用 CLI 测试」：网络错误信息在不同操作系统上措辞不同，难以在普通单元测试里断言；而 CLI 层会把它统一翻译成 `hint: network access is not supported`。这正体现了 smoke 测试的另一价值——**锁定面向用户的最终措辞**，而不仅仅是内部错误类型。

#### 4.5.4 代码实践：为 `--diagnostic-format short` 新增一个 smoke 测试

这是本讲的主实践任务。它把「读懂夹具」与「读懂诊断格式」结合起来。

**实践目标**：参考现有测试，为尚未被 smoke 覆盖的 `--diagnostic-format short` 选项新增一个最小测试，验证 short 格式确实「精简、不含源码片段/tracepoint」。

**背景**：`--diagnostic-format` 定义在 [src/args.rs:445-447](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L445-L447)（`ProcessArgs`，被 `CompileArgs` `#[clap(flatten)]` 引入，故 compile/watch 可用），枚举见 [src/args.rs:636-642](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L636-L642)。在 [src/compile.rs:720-732](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L720-L732) 的 `print_diagnostics` 里，`Human` 映射为富文本（含 tracepoint），`Short` 映射为精简格式。

**操作步骤**：

1. 先手动观察两种格式的差异。准备 `err.typ` 内容为 `#include "missing.typ"`，分别运行：

   ```bash
   typst compile err.typ                          # 默认 human
   typst compile err.typ --diagnostic-format short
   ```

   对比两次 stderr：human 会有源码片段（带 `╭` 等边框/下划线）和 `hint:`，short 通常只有单行 `err.typ:1:1: error: file not found`。把 short 的确切首行格式记下来。

2. 在 `tests/smoke.rs` 里，紧挨着 `test_path_unresolved` 之后，新增一个测试（**示例代码**，非项目原有代码）：

   ```rust
   #[test]
   fn test_diagnostic_format_short() {
       let project = tempfs();
       let main = project.write("main.typ", "#include \"missing.typ\"");
       let output = exec()
           .arg("compile")
           .arg(&main)
           .arg("--diagnostic-format")
           .arg("short")
           .must_fail();
       // short 格式仍然报告错误本身
       output.stderr.must_contain("error: file not found");
       // short 格式不渲染富文本源码片段，因此不应出现 human 格式的诊断边框
       // （若你观察到 short 仍含某固定标记，请以实际观察为准调整断言）
   }
   ```

3. 运行 `cargo test --test smoke test_diagnostic_format_short`。

**需要观察的现象**：

- 步骤 1 中两种格式输出明显不同；记下 short 格式的实际行结构。
- 步骤 3 中新测试通过。

**预期结果**：short 格式不含源码片段与 tracepoint，断言 `error: file not found` 成立。关于 short 的精确行格式（是否一定以 `<file>:<line>:<col>:` 开头）请以本地实际输出为准——若想进一步断言，可加一条 `output.stderr.must_contain("main.typ:1")`，但这部分**待本地验证**后再决定是否纳入。

> 提示：如果你在步骤 1 发现 `--diagnostic-format short` 下 `test_tracepoints` 里那些 `while ... at` 的 tracepoint **消失**了，那就反过来证明了「tracepoint 只在 human 格式输出」——这正是 short 与 human 的根本差异，也是为什么 `test_tracepoints` 不带 `--diagnostic-format`（默认 human）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `test_tracepoints` 不适合改用 `--diagnostic-format short`？

<details><summary>参考答案</summary>

`test_tracepoints` 的全部断言都围绕 tracepoint（`while calling ...`、`while including ...`、`while showing strong element ...`）和源码片段（`my-figure(…)`、`*Slightly unusual…*`），而这些内容只在 human 格式下输出。改用 short 格式后这些断言会全部落空、测试必然失败。

</details>

**练习 2**：`test_network_access_hint` 的注释说「错误信息因操作系统而异」。CLI 层做了什么让它变得可断言？

<details><summary>参考答案</summary>

CLI 层把底层（操作系统/网络栈）五花八门的网络错误统一翻译成一条面向用户的 `hint: network access is not supported` 提示。smoke 测试断言的是这条**稳定的应用级措辞**，而不是不稳定的底层错误文本，因而跨平台都能通过。

</details>

## 5. 综合实践

把本讲全部知识串起来，做一个「**为 typst-cli 加一个端到端冒烟测试**」的小任务。

**任务**：选择一个目前 `smoke.rs` **没有**覆盖的 CLI 选项或子命令（候选：`--diagnostic-format short`、`eval --field`/`--pretty`、`fonts --variants`、`info --format json`、`completions <shell>`），按下面的流程完整地加一个测试。

**步骤**：

1. **选目标**：从候选里挑一个，先用命令行手动跑通，观察它的 stdout/stderr 与退出码。例如 `typst info --format json` 应在 stdout 输出合法 JSON、退出码 0。
2. **定断言**：基于观察，决定用 `must_contain`（子串）、`must_match_lines`（整行）还是 `must_start_with`（前缀），以及对 stdout 还是 stderr 断言。注意：JSON 这类结构化输出，断言「关键 key 存在」往往比断言完整内容更稳健。
3. **写测试**：仿照现有测试，用 `tempfs()`（若需要文件）、`exec().arg(...)`、`must_succeed()`/`must_fail()`、`Stream` 的断言方法。
4. **跑测试**：`cargo test --test smoke <你的测试名>`，确认通过。
5. **故意改坏**验证断言真的有效：把断言里期望的字符串故意改错，确认测试会 FAIL，再改回来。

**验收标准**：

- 测试在不改任何 `src/` 源码的前提下通过。
- 故意改坏断言时，失败信息能（经 `#[track_caller]`）指向你的测试行，并附上子进程输出。
- 你能解释清楚这个测试验证了哪一条用户可见行为、为什么用子进程而不是直接调函数。

## 6. 本讲小结

- `smoke.rs` 采用**子进程黑盒测试**策略，用 `env!("CARGO_BIN_EXE_typst")` 定位刚构建出的 `typst` 二进制，真正起进程跑命令——最忠实地覆盖了 clap 解析、编译、终端输出、退出码整条链路。
- 一套自制轻量夹具让测试写起来像链式 DSL：`exec()` 起命令、`CommandExt::must_succeed/must_fail` 守退出码、`TempFs` 管临时目录、`Stream` 的 `must_contain/must_start_with/must_match_lines` 做断言；`#[track_caller]` 保证失败定位到测试行。
- 编译/PDF 测试（`test_compile_pdf` 等）通过校验产物魔数与元数据，端到端守住「能编出合法 PDF」；字体/依赖测试（`test_fonts_*`/`test_deps`）验证 `--font-path` 与 `--deps` 的真实行为，依赖清单来自「实际访问」。
- 包/路径测试大量使用 **panic 探针法**：故意在最末端 `#panic(42)`，靠错误信息里是否出现 `panicked with: 42` 来间接证明路径解析正确；`test_path_to_package` 还证明了项目与包的 VirtualRoot 路径隔离。
- 诊断测试（`test_tracepoints`/`test_network_access_hint`）守住 tracepoint 多级栈与统一的应用级 hint 措辞，且依赖默认 **human** 格式（tracepoint 仅在 human 输出）。
- smoke 测试的两大价值：**锁定面向用户的最终措辞**（跨平台稳定），以及**验证软失败的退出码语义**（`must_fail` 直接观测非 0 退出码）。

## 7. 下一步学习建议

本讲是 typst-cli 学习手册的收官篇。到此你已经读完了从入口分发、编译流水线、资源基础设施到高级机制的全部核心源码。后续建议：

- **横向对照 typst-eval 的测试**：仓库里 `crates/typst-eval` 也有一套自己的测试（在另一份学习手册里），可以对比「库 crate 的单元测试」与「CLI 的子进程 smoke 测试」在风格与取舍上的差异。
- **尝试扩展开源贡献**：找到一个 `smoke.rs` 尚未覆盖的选项或边界场景（例如 `watch` 模式无法用子进程简单测试，但 `--timings`、`--pdf-standard`、HTML 导出都可以），按本讲「综合实践」的流程提一个测试 PR——这是切入 typst 贡献的低风险入口。
- **回顾整条链路**：从 u1-l1 的「项目定位」到本讲的「如何验证」，回头重读 `main.rs` → `dispatch()` → `compile::compile` → `compile_once` → `compile_and_export` → `print_diagnostics`，你会发现 smoke 测试里的每一条断言，正好对应这条链路上某个用户可见的承诺。
