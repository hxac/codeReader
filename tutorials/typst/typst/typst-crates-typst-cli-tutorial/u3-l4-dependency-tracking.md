# 依赖追踪与构建集成

## 1. 本讲目标

本讲聚焦 `typst-cli` 中一个只有 179 行、却把 Typst 接入传统构建系统的「数据导出」模块：`src/deps.rs`。

学完后你应当能够：

- 说清楚「依赖文件（dependency file）」是什么、为什么 Make / Ninja 这类构建工具需要它。
- 解释 `write_deps` 如何按 `DepsFormat` 把一次编译实际读取过的文件分发成 **JSON / Zero / Make** 三种格式。
- 读懂 JSON 的 `inputs`/`outputs` 结构与 UTF-8 校验、Zero 的 NUL 分隔、Make 的 `munge` 转义（源自 GCC `mkdeps`）。
- 理解 `relative_dependencies` 如何把编译器内部的绝对路径换算成「相对当前工作目录」的路径，这正是构建工具消费依赖文件的前提。
- 自己动手用 `--deps` / `--deps-format` 产出三种依赖文件，并解释每一行的含义。

本讲承接 [u2-l2 编译配置与单次编译](u2-l2-compile-config.md)：在那里我们见过 `compile_once` 的主干，本讲补上它最后一步——「写出依赖文件」。

## 2. 前置知识

### 2.1 什么是依赖文件、为什么需要它

当你用 Typst 编译一份文档时，编译器会读取很多文件：主 `.typ` 文件、`#import` 进来的模块、`#image("tiger.jpg")` 引用的图片、`#csv("data.csv")` 读的数据……这些统称为这次编译的**依赖（dependencies）**。

如果你在一个用 Make / Ninja / CMake 驱动的大型项目里把 Typst 文档当作「构建产物」来管理，就会遇到增量编译问题：

> 我怎么让构建工具知道「`report.pdf` 依赖 `report.typ`、`tiger.jpg`、`data.csv`」，从而任意一个改了就重新编译？

答案是让 **Typst 自己告诉构建工具它读了哪些文件**。这就是依赖文件的作用：它把「这次编译实际访问过的文件清单」写成一个构建工具能解析的格式。Make 读 Make 风格的依赖规则，Ninja 读 NUL 分隔的清单，自定义脚本读 JSON。

> 关键点：依赖清单不是「文档里写明的引用」，而是**编译器在这次编译中真正打开过的文件**。这一点决定了它的数据来源——下面 4.2 会看到它取自 `world.dependencies()`。

### 2.2 三种格式的取舍

Typst 提供三种格式，分别应对不同的「路径表达能力」与「消费方」需求：

| 格式 | 分隔方式 | 是否含输出（产物） | 遇到非 Unicode 路径 | 典型消费方 |
| --- | --- | --- | --- | --- |
| `json`（默认） | JSON 数组 | `inputs` + 可选 `outputs` | **报错** | 自定义脚本、工具链 |
| `zero` | NUL（`\0`）字节 | 仅 `inputs` | **兼容**（按字节写出） | Ninja / Text0 风格工具 |
| `make` | 空格 + `:` 规则 | `outputs : inputs` | **静默跳过** | GNU Make |

为什么三者对「非 Unicode 路径」态度不同？因为文件路径在 Rust 里是 `OsStr`（操作系统原生字符串），在 Linux 上可以是任意字节序列、不一定是合法 UTF-8。JSON 只能装 Unicode 字符串，所以遇到坏路径只能报错；Make 的语法无法表达任意字节，只能跳过；Zero 用原始 NUL 字节分隔，能把任何字节序列原样写出。

### 2.3 你需要回忆的几个旧概念

- **`World` trait 与 `SystemWorld`**（见 [u2-l1](u2-l1-system-world.md)）：编译器通过 `World` 读文件，`SystemWorld` 是它在 CLI 里的实现。本讲的依赖清单正是从 `SystemWorld` 取出来的。
- **`Output` 枚举**（见 [u1-l3 命令行参数模型](u1-l3-args-model.md)）：`Output::Path(PathBuf)` 或 `Output::Stdout`（命令行上写 `-`）。依赖文件可以写到 stdout，也可以写到路径。
- **`FileId` / 项目根（project root）**（见 [u2-l1](u2-l1-system-world.md)）：编译器内部用一个全局的 `FileId` 标识文件，并不直接持有磁盘路径。把 `FileId` 翻译回路径正是依赖导出要解决的问题之一。
- **clap 派生宏与 `ValueEnum`**（见 [u1-l3](u1-l3-args-model.md)）：`DepsFormat` 是一个 `#[derive(ValueEnum)]` 的枚举，`json/zero/make` 就是它的命令行取值。

## 3. 本讲源码地图

本讲只深入一个文件，并旁引三个相关文件：

| 文件 | 作用 |
| --- | --- |
| [`src/deps.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/deps.rs) | **核心**。全部依赖导出逻辑：三格式分发、JSON/Zero/Make 写入、`munge` 转义、相对路径换算。 |
| [`src/args.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs) | 定义 `DepsFormat` 枚举与 `--deps` / `--deps-format` / `--make-deps`（已弃用）命令行参数。 |
| [`src/compile.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs) | 在 `compile_once` 末尾调用 `write_deps`，并把弃用参数 `--make-deps` 翻译成新接口。 |
| [`src/world.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs) | 提供 `world.dependencies()`（依赖来源）与 `world.root()`（项目根），是 `relative_dependencies` 的数据源。 |

## 4. 核心概念与源码讲解

### 4.1 依赖追踪的目标与三格式分发

#### 4.1.1 概念说明

`write_deps` 是整个模块的**唯一公共入口**（`pub fn`），其余函数都是私有的。它的职责很单纯：根据用户选择的 `DepsFormat`，把「这次编译读了哪些文件」分发到三个写入函数之一。

这里有一个贯穿全模块的设计原则——**「能表达就写、不能表达就降级」**：

- JSON 表达不了非 Unicode 路径 → 报错（`InvalidData`）。
- Make 表达不了非 Unicode 路径 → 静默跳过该路径，其余照写。
- Zero 能表达任意字节 → 全部原样写出。

这种「按格式能力决定容错策略」的思路，是三种格式分支各自处理路径时的核心差异。

#### 4.1.2 核心流程

`write_deps` 的分发逻辑可以用下面这段伪代码概括：

```
fn write_deps(world, dest, format, outputs):
    match format:
        Json  -> write_deps_json(world, dest, outputs)     # 需要 outputs（可为 None）
        Zero  -> write_deps_zero(world, dest)              # 不关心 outputs
        Make  -> if outputs 是 Some(outputs):
                     write_deps_make(world, dest, outputs) # 必须有产物路径
                 # 否则什么都不做
```

注意三个分支对 `outputs`（编译产物路径列表）的态度完全不同：

- **JSON**：接收 `Option`，把它原样序列化进结果（成功是数组、失败是 `null`）。
- **Zero**：根本不接收 `outputs`——它只列输入依赖。
- **Make**：**必须**有 `outputs` 才有意义，因为 Make 规则的左边是「产物」。没有产物就什么都不写。

`outputs` 从哪来？它是 `compile_once` 把这次编译**真正成功生成**的产物路径传进来的。编译失败时 `outputs` 为 `None`，于是 Make 分支自然跳过——这是合理的：编译都失败了，谈不上「产物依赖了什么」。

#### 4.1.3 源码精读

先看公共入口 `write_deps`：

[src/deps.rs:9-26](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/deps.rs#L9-L26) —— 这是三格式分发的总入口：`Json`/`Zero` 直接调用对应函数；`Make` 在 `if let Some(outputs)` 守卫内调用 `write_deps_make`，没有产物就什么都不写。

```rust
pub fn write_deps(
    world: &mut SystemWorld,
    dest: &Output,
    format: DepsFormat,
    outputs: Option<&[Output]>,
) -> io::Result<()> {
    match format {
        DepsFormat::Json => write_deps_json(world, dest, outputs)?,
        DepsFormat::Zero => write_deps_zero(world, dest)?,
        DepsFormat::Make => {
            if let Some(outputs) = outputs {
                write_deps_make(world, dest, outputs)?;
            }
        }
    }
    Ok(())
}
```

再看 `DepsFormat` 枚举的定义与默认值：

[src/args.rs:608-620](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L608-L620) —— 三个变体的文档注释已经把三者的「路径表达能力」差异说清楚了：`Json` 对非 Unicode 报错、`Zero` 用 NUL 分隔且能表达全部路径、`Make` 省略无法表达的路径。`#[default]` 标在 `Json` 上，所以不指定 `--deps-format` 时默认产出 JSON。

```rust
/// Which format to use for a generated dependency file.
#[derive(Debug, Default, Copy, Clone, Eq, PartialEq, ValueEnum)]
pub enum DepsFormat {
    /// Encodes as JSON, failing for non-Unicode paths.
    #[default]
    Json,
    /// Separates paths with NULL bytes and can express all paths.
    Zero,
    /// Emits in Make format, omitting inexpressible paths.
    Make,
}
```

对应的命令行参数（注意 `--make-deps` 已 `hide = true` 弃用）：

[src/args.rs:359-376](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L359-L376) —— `--make-deps`（旧、隐藏）、`--deps`（新，`-` 表示写到 stdout）、`--deps-format`（默认 JSON）。`--deps` 的 `value_parser` 复用了 output 值解析器，所以同样用 `-` 区分 stdout 与路径。

最后看调用点。在 `compile_once` 末尾，只有当 `config.deps` 存在（即用户指定了 `--deps`）时才调用 `write_deps`：

[src/compile.rs:308-311](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L308-L311) —— 第四个实参 `output.as_deref().ok()`：编译成功时是 `Some(&[Output])`，失败时是 `None`。这个 `Option` 直接决定了 Make 分支是否产出依赖文件。

```rust
if let Some(dest) = &config.deps {
    write_deps(world, dest, config.deps_format, output.as_deref().ok())
        .map_err(|err| eco_format!("failed to create dependency file ({err})"))?;
}
```

> 旁注：`--make-deps` 被翻译成新接口的逻辑在 `CompileConfig::new_impl` 里：[src/compile.rs:198-209](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L198-L209)。当用户给了 `--make-deps` 且没给 `--deps` 时，它会把 `deps` 设为该路径、`deps_format` 设为 `Make`，并 push 一条弃用警告。这是「保留旧接口、底层统一走新代码」的典型做法。

#### 4.1.4 代码实践

**实践目标**：亲手产出一份 JSON 依赖文件，确认 `write_deps` 真的被走到了。

**操作步骤**：

1. 准备一个最小文档 `doc.typ`，里面引用一张图片（如果没有图片，可改成 `#include "chapter.typ"` 之类，关键是产生一个额外依赖）：
   ```typ
   图片：#image("tiger.jpg")
   ```
   （任意一张名为 `tiger.jpg` 的图片放同目录；若没有，用任意被读取的文件即可。）
2. 在该目录下用 `--deps -` 把依赖写到 stdout、`--deps-format json` 指定格式（其实 json 是默认，这里显式写出便于对照）：
   ```bash
   typst compile doc.typ --deps - --deps-format json
   ```
3. 观察终端打印的 JSON。

**需要观察的现象**：stdout 上会先出现 PDF 的二进制内容（因为产物默认也写 stdout），**之后**跟着一段 JSON。如果你不想让产物污染输出，可以把产物写到文件、依赖单独写到另一个文件：
   ```bash
   typst compile doc.typ doc.pdf --deps deps.json --deps-format json
   cat deps.json
   ```

**预期结果**（具体路径取决于你的目录，**待本地验证**）：`deps.json` 形如
```json
{"inputs":["doc.typ","tiger.jpg"],"outputs":["doc.pdf"]}
```

> 说明：`compile_once` 里有一组守卫禁止「产物与依赖都写 stdout」等冲突（[src/compile.rs:211-222](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L211-L222)），所以上面推荐把两者分开写。

#### 4.1.5 小练习与答案

**练习 1**：如果编译失败（比如 `doc.typ` 有语法错误），指定了 `--deps deps.json`，`deps.json` 还会被写出来吗？里面 `outputs` 字段会是什么？

**参考答案**：会被写出来（`write_deps` 在 `match output` 之后、`Ok(())` 之前无条件调用）。但因为编译失败，传入的 `outputs` 是 `None`，所以 JSON 里 `outputs` 序列化为 `null`，`inputs` 仍是本次实际读取过的文件清单。

**练习 2**：为什么 `write_deps` 的 `Make` 分支要用 `if let Some(outputs)` 守卫，而 `Json` 分支不需要？

**参考答案**：Make 规则的语法是「产物 : 依赖」，左边必须有产物路径；没有产物就无法生成一条合法规则，所以静默跳过。JSON 把 `outputs` 当作一个可空字段序列化即可，`null` 也是合法 JSON，不需要守卫。

---

### 4.2 依赖的来源与相对路径换算：`relative_dependencies`

#### 4.2.1 概念说明

三种写入格式都要消费同一份数据：「本次编译读取过的文件路径列表」。这份列表有两个特点需要处理：

1. **来源**：编译器内部只知道 `FileId`，不知道磁盘路径。需要把 `FileId` 解析回真实路径。
2. **坐标**：解析出来的真实路径是**绝对路径**，但构建工具（Make/Ninja）通常以「调用 typst 时的工作目录」为坐标。需要换算成相对路径。

`relative_dependencies` 就是解决第二个问题的私有工具函数；第一个问题由 `SystemWorld::dependencies()` 解决。两者拼起来，才是「写入函数们」真正消费的迭代器。

#### 4.2.2 核心流程

数据流如下：

```
SystemWorld::dependencies()
    └─ 取出 FileStore 里「本次访问过的文件」的 FileId 列表
       └─ loader.resolve(id)  把每个 FileId 翻译成绝对磁盘路径 (PathBuf)
          └─ relative_dependencies()  把绝对路径换算成「相对当前工作目录」
             └─ 三个 writer 消费这个迭代器
```

`relative_dependencies` 的换算思路（注意它是把路径表达成「相对于 `current_dir`」）：

```
root         = 项目根（绝对路径）
current_dir  = std::env::current_dir()   # typst 被调用时的工作目录
relative_root = pathdiff::diff_paths(root, current_dir)  # root 相对 current_dir 的写法
                                          # 例：root=/a/proj, cwd=/a  →  relative_root="proj"

对每个 dependency（绝对路径）:
    若它以 root 为前缀 → strip_prefix 后得到文件在项目内的相对位置 x，
                        再 join relative_root → 相对 cwd 的路径
    否则（不在项目根下，例如系统字体、包）→ 保留原始绝对路径
```

为什么用「项目根」做前缀剥离，而不是直接对每个依赖算 `diff_paths(dep, cwd)`？因为项目根是所有项目文件的公共前缀，先 `strip_prefix` 再 `join` 比逐个 `diff_paths` 更稳定、也更符合「项目内文件用相对写法、项目外文件用绝对写法」的直觉。

#### 4.2.3 源码精读

先看数据来源 `SystemWorld::dependencies()` 与 `root()`：

[src/world.rs:88-101](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L88-L101) —— `dependencies()` 借用 `self.files.dependencies()` 拿到「访问过的 `FileId` 流」，再用 `loader.resolve(id)` 把每个 id 翻译回磁盘路径；`root()` 返回项目根路径。注意签名是 `&mut self`——它会**取走**本次的访问记录（`accessed` 槽位），这也正是 watch 模式每轮都要重新收集的原因（见 [u2-l5](u2-l5-watch-incremental.md)）。

```rust
pub fn root(&self) -> &Path {
    self.files.loader().project.path()
}

// ...

pub fn dependencies(&mut self) -> impl Iterator<Item = PathBuf> + '_ {
    let (loader, deps) = self.files.dependencies();
    deps.filter_map(|id| loader.resolve(id).ok())
}
```

再看换算函数本体：

[src/deps.rs:164-178](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/deps.rs#L164-L178) —— 三步：算出 `relative_root`（项目根相对 cwd 的写法，算不出就退回绝对 root）；对每个依赖 `strip_prefix(root)`，成功则拼到 `relative_root` 上，失败（依赖不在项目根下）则原样保留。`std::env::current_dir()?` 失败时整函数返回 `io::Error`。

```rust
fn relative_dependencies(
    world: &mut SystemWorld,
) -> io::Result<impl Iterator<Item = PathBuf>> {
    let root = world.root().to_owned();
    let current_dir = std::env::current_dir()?;
    let relative_root =
        pathdiff::diff_paths(&root, &current_dir).unwrap_or_else(|| root.clone());
    Ok(world.dependencies().map(move |dependency| {
        dependency
            .strip_prefix(&root)
            .map_or_else(|_| dependency.clone(), |x| relative_root.join(x))
    }))
}
```

> 一个细节：返回类型是 `impl Iterator<Item = PathBuf>`，且闭包用 `move` 捕获了 `root` 与 `relative_root`。这意味着 `world.dependencies()` 是**惰性**的——只有在 writer 真正迭代时才会触发 `FileId → 路径` 的解析。

#### 4.2.4 代码实践

**实践目标**：验证「项目内文件是相对路径、项目外文件是绝对路径」的换算行为。

**操作步骤**：

1. 建立如下目录结构：
   ```
   /tmp/proj/
     doc.typ        # 内容：#image("tiger.jpg")
     tiger.jpg
   ```
2. **在 `/tmp` 目录下**（即项目的父目录）调用 typst，让 `current_dir` 与项目根不同：
   ```bash
   cd /tmp
   typst compile proj/doc.typ proj/doc.pdf --deps /tmp/deps.json --deps-format json
   cat /tmp/deps.json
   ```
3. 再**在 `/tmp/proj` 目录下**重复一次，对比两次 `inputs` 的写法：
   ```bash
   cd /tmp/proj
   typst compile doc.typ doc.pdf --deps /tmp/deps2.json --deps-format json
   cat /tmp/deps2.json
   ```

**需要观察的现象**：

- 第一次（cwd=`/tmp`，root=`/tmp/proj`）：`relative_root = "proj"`，故 `inputs` 形如 `["proj/doc.typ", "proj/tiger.jpg"]`。
- 第二次（cwd=`/tmp/proj`，root=`/tmp/proj`）：`relative_root = "."`（`diff_paths` 同路径返回 `.`），strip 后 join，`inputs` 形如 `["doc.typ", "tiger.jpg"]` 或 `["./doc.typ", ...]`（**待本地验证**确切写法）。

**预期结果**：两次输出的 `inputs` 路径前缀不同，证明换算是相对「调用 typst 的工作目录」而非固定绝对路径。这正是 Make/Ninja 把依赖文件直接 `include` 进 Makefile 时所期望的坐标系。

#### 4.2.5 小练习与答案

**练习 1**：如果一个依赖文件位于项目根之外（比如某系统目录下的字体文件被实际读取），`relative_dependencies` 会怎么处理它？

**参考答案**：`strip_prefix(root)` 会失败（`Err`），于是走 `map_or_else` 的 `_` 分支，返回 `dependency.clone()`——即保留它的**绝对路径**。所以依赖清单里项目内文件是相对路径、项目外文件是绝对路径，二者可以混排。

**练习 2**：为什么 `relative_dependencies` 要返回 `io::Result<...>`？哪一步可能产生 `io::Error`？

**参考答案**：因为 `std::env::current_dir()?` 可能失败（例如工作目录已被删除、权限不足等罕见情况），此时无法确定相对坐标，只能把 `io::Error` 向上冒泡。`pathdiff::diff_paths` 失败时只是退回 `unwrap_or_else(|| root.clone())`，不产生错误。

---

### 4.3 JSON 格式：`write_deps_json`

#### 4.3.1 概念说明

JSON 是默认格式，目标是「机器可读、结构清晰」。它输出一个对象，含两个字段：

- `inputs`：本次编译读取过的所有文件（经 `relative_dependencies` 换算后的路径字符串数组）。
- `outputs`：本次编译产出的文件路径数组；编译失败或未提供时为 `null`。

JSON 的硬约束是**只能装合法 Unicode 字符串**。文件路径是 `OsString`，在 Linux 上可能是任意字节序列（非 UTF-8）。`write_deps_json` 用一个闭包 `to_string` 把 `OsString → String`，失败时返回 `InvalidData` 错误，明确告诉调用方「这个路径我表达不了」。

#### 4.3.2 核心流程

```
inputs = relative_dependencies(world)
         .map(|dep| to_string(dep, "input"))   # OsString → String，非 UTF-8 报错
         .collect::<Result<Vec<String>, _>>()? # 任一失败则整体失败

outputs = outputs（Option<&[Output]>）
          .map(|outs| outs.iter()
                  .filter_map(|o| match o {
                      Output::Path(p) => Some(to_string(p, "output")),
                      Output::Stdout  => None,      # 跳过 stdout
                  }).collect())
          .transpose()?                            # Option<Result> → Result<Option>

序列化 Deps { inputs, outputs } 写入 dest
```

关键点：

- `inputs` 任一非 UTF-8 就整函数报错（`?` 在 `collect` 上）。
- `outputs` 的 `Output::Stdout` 被 `filter_map` 静默跳过——产物写到 stdout 没有路径可言，不应出现在依赖文件里。
- 内联定义了一个 `Deps` 结构体并派生 `Serialize`，用 `serde_json::to_writer` 直接流式写入 `dest.open()?` 打开的句柄。

#### 4.3.3 源码精读

[src/deps.rs:28-71](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/deps.rs#L28-L71) —— 注意三件事：(1) 闭包 `to_string` 把 `OsString` 转 `String`，失败时给出带 `kind`（"input"/"output"）与原始 `dep:?` 的 `InvalidData` 错误；(2) `outputs` 分支用 `filter_map` 跳过 `Output::Stdout`；(3) `Deps` 结构体在函数内部内联定义，`outputs` 是 `Option<Vec<String>>`，故编译失败（`outputs=None`）时序列化为 `"outputs":null`。

```rust
fn write_deps_json(
    world: &mut SystemWorld,
    dest: &Output,
    outputs: Option<&[Output]>,
) -> io::Result<()> {
    let to_string = |dep: PathBuf, kind| {
        dep.into_os_string().into_string().map_err(|dep| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                format!("{kind} {dep:?} is not valid UTF-8"),
            )
        })
    };

    let inputs = relative_dependencies(world)?
        .map(|dep| to_string(dep, "input"))
        .collect::<Result<_, _>>()?;

    let outputs = outputs
        .map(|outputs| {
            outputs
                .iter()
                .filter_map(|output| match output {
                    Output::Path(path) => Some(to_string(path.clone(), "output")),
                    // Skip stdout
                    Output::Stdout => None,
                })
                .collect::<Result<_, _>>()
        })
        .transpose()?;

    #[derive(Serialize)]
    struct Deps {
        inputs: Vec<String>,
        outputs: Option<Vec<String>>,
    }

    serde_json::to_writer(dest.open()?, &Deps { inputs, outputs })?;

    Ok(())
}
```

> 关于 `dest.open()`：`Output::open()` 返回一个 `OpenOutput` 枚举（stdout 锁或文件句柄），它实现了 `Write`（见 [u1-l3](u1-l3-args-model.md) 对 `Output`/`OpenOutput` 的讲解）。所以 `serde_json::to_writer` 既能写文件也能写 stdout。

#### 4.3.4 代码实践

**实践目标**：理解 `inputs`/`outputs` 结构，并验证 stdout 产物会被跳过。

**操作步骤**：

1. 用一份带 `#image` 的 `doc.typ`，把产物写到文件、依赖写到 JSON：
   ```bash
   typst compile doc.typ doc.pdf --deps deps.json --deps-format json
   cat deps.json
   ```
2. 对比「产物写到 stdout」的情况（依赖改写到文件，避免与产物冲突）：
   ```bash
   typst compile doc.typ --deps deps_stdout_product.json --deps-format json < /dev/null > /dev/null 2>&1
   cat deps_stdout_product.json
   ```
   （这里产物走 stdout，所以 JSON 的 `outputs` 字段应当被跳过成空数组。）

**需要观察的现象**：

- 第 1 步：`outputs` 是 `["doc.pdf"]`。
- 第 2 步：`outputs` 是 `[]`（空数组），因为唯一的产物是 stdout，被 `filter_map` 跳过。

**预期结果**：`inputs` 始终包含 `doc.typ` 与图片路径；`outputs` 在「产物为路径」时列出、在「产物为 stdout」时为空数组（编译失败时为 `null`，见 4.1.5 练习 1）。**待本地验证**空数组的确切表现。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Deps` 结构体要定义在 `write_deps_json` 函数内部，而不是提到模块顶层？

**参考答案**：因为它只服务于这一处序列化、字段就是两个局部变量，属于「一次性、局部」的数据形状。函数内定义可以避免污染模块命名空间，也清楚地表达「这个结构只为 JSON 输出而存在」。这是 Rust 里常见的「就近定义临时序列化结构」的写法。

**练习 2**：如果某张图片的文件名包含非 UTF-8 字节（Linux 上合法但少见），`write_deps_json` 会怎样？

**参考答案**：`to_string` 闭包的 `into_string()` 会失败，返回 `io::ErrorKind::InvalidData`，错误信息形如 `input "<bytes>" is not valid UTF-8`。这个错误经 `collect::<Result<_,_>>()?` 冒泡，最终在 `compile_once` 里被包成 `failed to create dependency file (...)`。也就是说，JSON 格式遇到非 Unicode 路径会**让依赖导出整体失败**——这正是 4.1 表格里 JSON「报错」一栏的来源。

---

### 4.4 Zero 格式：`write_deps_zero`

#### 4.4.1 概念说明

Zero（也叫 Text0）格式用一个 NUL 字节（`\0`）分隔每个依赖路径，且**只列输入依赖、不列产物**。它的最大优势是「能表达任意路径」：因为它直接按字节写出 `OsStr`，不经过 `String`/UTF-8 转换，所以无论路径里有什么字节都能原样落盘。

这种格式适合 Ninja 这类工具以及任何喜欢用 NUL 分隔（`xargs -0`、`sort -z`）的 Unix 管道。

#### 4.4.2 核心流程

```
打开 dest 句柄
对 relative_dependencies(world) 里的每个 dep:
    写出 dep.as_os_str().as_encoded_bytes()   # 原始字节，不转码
    写出一个 \0
```

极简、不校验、不转义、不关心产物。

#### 4.4.3 源码精读

[src/deps.rs:73-81](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/deps.rs#L73-L81) —— 用 `as_os_str().as_encoded_bytes()` 取路径的原始字节（不经过 UTF-8 校验），逐个写出并追加 `\0`。注意它接收 `dest: &Output` 但**不接收** `outputs`，所以 Zero 格式天然不包含产物信息。

```rust
/// Writes dependencies in the Zero / Text0 format.
fn write_deps_zero(world: &mut SystemWorld, dest: &Output) -> io::Result<()> {
    let mut dest = dest.open()?;
    for dep in relative_dependencies(world)? {
        dest.write_all(dep.as_os_str().as_encoded_bytes())?;
        dest.write_all(b"\0")?;
    }
    Ok(())
}
```

> `as_encoded_bytes()` 是 `OsStr` 的方法，返回底层字节切片（在 Unix 上就是原始 bytes，在 Windows 上是 WTF-8）。用它而非 `to_str()` 正是 Zero 格式「能表达全部路径」的关键——它绕开了 UTF-8 这一关。

#### 4.4.4 代码实践

**实践目标**：看到 NUL 分隔字节，体会 Zero 与 JSON 的根本差异。

**操作步骤**：

1. 产出 Zero 格式依赖（产物写到文件，依赖单独写）：
   ```bash
   typst compile doc.typ doc.pdf --deps deps.zero --deps-format zero
   ```
2. 用 `od` / `xxd` 查看字节，肉眼定位 NUL：
   ```bash
   od -c deps.zero
   # 或
   xxd deps.zero
   ```
3. 也可用 `tr` 把 NUL 换成换行，便于阅读：
   ```bash
   tr '\0' '\n' < deps.zero
   ```

**需要观察的现象**：`od -c` 输出里每个路径之后都跟着一个 `\0`。例如形如 `d o c . t y p \0 t i g e r . j p g \0`。

**预期结果**：每个依赖路径之间确实由单个 NUL 字节分隔，文件末尾在最后一个路径后也有一个 NUL。这正是 `xargs -0` 等工具期望的格式。**待本地验证**确切字节序列。

#### 4.4.5 小练习与答案

**练习 1**：Zero 格式为什么不像 JSON 那样在遇到非 Unicode 路径时报错？

**参考答案**：因为它用 `as_encoded_bytes()` 直接写原始字节，从不把 `OsStr` 转成 `String`，所以根本不存在「UTF-8 校验」这一步，任何字节序列都能被忠实地写出来并用 `\0` 分隔。

**练习 2**：Zero 格式的输出里能看出「哪些是产物」吗？为什么？

**参考答案**：不能。`write_deps_zero` 根本不接收 `outputs` 参数，只写输入依赖。如果构建工具需要产物信息，应选 JSON 或 Make；选 Zero 通常意味着消费方只关心「输入」。

---

### 4.5 Make 格式与 `munge` 转义

#### 4.5.1 概念说明

Make 格式输出一条标准的 **Make 依赖规则**：

```make
产物: 依赖1 依赖2 依赖3
```

把这一行 `include` 进 Makefile，Make 就会在任一依赖比产物新时重新触发对应规则。这是 C/C++ 项目里 `gcc -MMD` 生成的 `.d` 文件的同款用法。

难点在于 Make 语法对一些字符有特殊含义：空格分隔字段、`:` 区分目标和依赖、`#` 开始注释、`$` 触发变量展开、`\` 是转义符。如果文件名里恰好含这些字符（例如 `my report.typ`、`a:b.typ`），直接写进 Makefile 会被误解析。`munge` 函数负责把这些特殊字符转义，使含特殊字符的路径也能安全地出现在依赖规则里。

`munge` 的算法**直接源自 GCC 的 `libcpp/mkdeps.cc`**——也就是说，Typst 复用了 GCC 处理依赖文件转义的成熟方案，源码注释也明确标注了这一点。

#### 4.5.2 核心流程

`write_deps_make` 分两段写：

```
# 第一段：左边的「产物」（targets），先攒进内存 buffer
对 outputs 里每个产物路径（带序号 i）:
    若是 Output::Stdout → 立即返回错误（Make 依赖必须有产物路径，不能是 stdout）
    path.to_str() 失败（非 Unicode）→ 静默 continue 跳过
    否则：i!=0 时先写一个空格，再写 munge(string)

# 把 buffer 写入 dest，然后写一个 ':'

# 第二段：右边的「依赖」
对 relative_dependencies(world) 里每个依赖:
    dep.to_str() 失败 → 静默 continue 跳过
    否则：先写一个空格，再写 munge(string)

最后写一个 '\n'
```

`munge(s)` 的转义规则（用 `slashes` 计数器跟踪连续反斜杠）：

| 输入字符 | 处理 | 直觉解释 |
| --- | --- | --- |
| `\` | `slashes += 1`，原样输出 | 累计反斜杠，留给后随的空格/制表符处理 |
| `$` | 先输出一个 `$`，再输出本字符 → `$$` | Make 用 `$$` 表示字面 `$` |
| `:` | 先输出 `\`，再输出本字符 → `\:` | 转义目标/依赖分隔符 |
| `#` | 先输出 `\`，再输出本字符 → `\#` | 转义注释起始符 |
| 空格/制表符 | 先输出 `slashes+1` 个 `\`，再输出本字符 | 「2N+1 个反斜杠 + 空格」表示「N 个反斜杠 + 字面空格」（GCC 规则） |
| 其它 | 重置 `slashes=0`，原样输出 | 无需转义 |

> 核心直觉：连续反斜杠后若跟空格，需要把反斜杠「翻倍再加一」，这样 Make 解析时才会把它们还原成字面反斜杠加字面空格，而不会把空格当作字段分隔符。

#### 4.5.3 源码精读

先看 `write_deps_make`：

[src/deps.rs:83-126](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/deps.rs#L83-L126) —— 三个要点：(1) 产物若是 `Output::Stdout` 立即返回 `InvalidInput` 错误；(2) 产物与依赖都通过 `to_str()` 过滤非 Unicode 路径（`let Some(string) = ... else { continue }`，静默跳过）；(3) 产物先攒进 `buffer` 再一次性写出，依赖直接流式写到 `dest`。规则形如 `<munge(产物)>: <munge(dep1)> <munge(dep2)>...\n`。

```rust
fn write_deps_make(
    world: &mut SystemWorld,
    dest: &Output,
    outputs: &[Output],
) -> io::Result<()> {
    let mut buffer = Vec::new();
    for (i, output) in outputs.iter().enumerate() {
        let path = match output {
            Output::Path(path) => path.as_os_str(),
            Output::Stdout => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "make dependencies contain the output path, \
                     but the output was stdout",
                ));
            }
        };

        // Silently skip paths that aren't valid Unicode so we still
        // produce a rule that will work for the other paths that can be
        // processed.
        let Some(string) = path.to_str() else { continue };
        if i != 0 {
            buffer.write_all(b" ")?;
        }
        buffer.write_all(munge(string).as_bytes())?;
    }

    // Only create the deps file in case of valid output paths.
    let mut dest = dest.open()?;
    dest.write_all(&buffer)?;
    dest.write_all(b":")?;

    for dep in relative_dependencies(world)? {
        // See above.
        let Some(string) = dep.to_str() else { continue };
        dest.write_all(b" ")?;
        dest.write_all(munge(string).as_bytes())?;
    }
    dest.write_all(b"\n")?;

    Ok(())
}
```

再看转义函数 `munge`：

[src/deps.rs:128-162](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/deps.rs#L128-L162) —— 注释明确标注「Based on `munge` in libcpp/mkdeps.cc from the GCC source code」。`slashes` 计数器跟踪尚未被消费的连续反斜杠；遇到空格/制表符时按 `0..=slashes`（即 `slashes+1` 次）补反斜杠，实现「2N+1 → N + 空格」的 GCC 语义；`$`/`:`/`#` 各自加前缀转义。每次 `match` 之后都会 `res.push(c)` 把原字符追加进去。

```rust
// Based on `munge` in libcpp/mkdeps.cc from the GCC source code. This isn't
// perfect as some special characters can't be escaped.
fn munge(s: &str) -> String {
    let mut res = String::with_capacity(s.len());
    let mut slashes = 0;
    for c in s.chars() {
        match c {
            '\\' => slashes += 1,
            '$' => {
                res.push('$');
                slashes = 0;
            }
            ':' => {
                res.push('\\');
                slashes = 0;
            }
            ' ' | '\t' => {
                // `munge`'s source contains a comment here that says: "A
                // space or tab preceded by 2N+1 backslashes represents N
                // backslashes followed by space..."
                for _ in 0..=slashes {
                    res.push('\\');
                }
                slashes = 0;
            }
            '#' => {
                res.push('\\');
                slashes = 0;
            }
            _ => slashes = 0,
        }
        res.push(c);
    }
    res
}
```

> 注释里那句「This isn't perfect as some special characters can't be escaped」很诚实——Make 语法本身的表达力有限，`munge` 已尽力，但仍有个别极端字符无法完美转义。这也是为什么 Typst 同时提供 Zero（无转义、能表达一切）作为后备。

#### 4.5.4 代码实践

**实践目标**：产出一条 Make 依赖规则，并观察 `munge` 对含空格路径的转义。

**操作步骤**：

1. 准备一个文件名**含空格**的依赖，例如把图片命名为 `my tiger.jpg`，`doc.typ` 里写 `#image("my tiger.jpg")`。
2. 产出 Make 格式依赖（产物必须写到路径，不能是 stdout）：
   ```bash
   typst compile doc.typ doc.pdf --deps deps.mk --deps-format make
   cat deps.mk
   ```
3. 对照 `munge` 的代码，手工推导 `my tiger.jpg`（含一个空格、前面 0 个反斜杠，故 `slashes=0`）应当被转义成什么。

**需要观察的现象**：`deps.mk` 是一行，形如：
```
doc.pdf: doc.typ my\ tiger.jpg
```
注意空格前出现了一个反斜杠（因为 `slashes=0`，循环执行 `0..=0` 即 1 次，补 1 个 `\`）。产物 `doc.pdf` 不含特殊字符，原样写出。

**预期结果**：每个空格都被 `\ ` 转义；`:` 被转义成 `\:`（若文件名含冒号）；`$` 被转义成 `$$`。把这一行 `include` 进 Makefile，Make 能正确把它解析成「`doc.pdf` 依赖 `doc.typ` 和 `my tiger.jpg`」。**待本地验证**含空格路径的转义输出。

#### 4.5.5 小练习与答案

**练习 1**：如果用户把产物写到 stdout（`typst compile doc.typ - --deps deps.mk --deps-format make`），会发生什么？

**参考答案**：`write_deps_make` 在遍历 `outputs` 时命中 `Output::Stdout` 分支，立即返回 `io::ErrorKind::InvalidInput`，信息是「make dependencies contain the output path, but the output was stdout」。该错误在 `compile_once` 被包成 `failed to create dependency file (...)`。所以 Make 格式 + stdout 产物是不兼容组合（JSON 则会跳过 stdout、不报错）。

**练习 2**：`munge` 为什么需要一个 `slashes` 计数器，而不是见到空格就简单加一个 `\`？

**参考答案**：因为路径里可能本来就有反斜杠。如果路径是 `a\b `（一个反斜杠后跟空格），简单加一个 `\` 会变成 `a\b\ `，Make 解析时会误判反斜杠数量。GCC 的规则是「空格前 2N+1 个反斜杠表示 N 个反斜杠 + 字面空格」，所以要数清前面的反斜杠数 N，再补足到 2N+1。`slashes` 计数器正是为了正确实现这套「翻倍加一」语义。

**练习 3**：三种格式里，哪一种最适合「文件名可能含任意字节、且构建工具是 Ninja」的场景？为什么？

**参考答案**：Zero。它用 NUL 分隔、按原始字节写出，能表达任意路径；Ninja 及很多 Unix 工具（`xargs -0`、`sort -z`）原生支持 NUL 分隔。JSON 会因非 UTF-8 报错，Make 会因转义能力有限而静默丢路径，都不如 Zero 健壮。

---

## 5. 综合实践

把本讲的知识串起来：模拟一个「用 Make 驱动 Typst 文档构建」的小工程，亲手走一遍依赖文件的产出与消费。

**任务**：建立如下目录，并让它能在任意 `tiger.jpg` 更新后自动重新编译。

```
proj/
  Makefile
  doc.typ        # 内容：#image("tiger.jpg")
  tiger.jpg
```

**步骤**：

1. 在 `proj/` 下先用 Typst 产出 Make 格式依赖，确认依赖规则正确：
   ```bash
   cd proj
   typst compile doc.typ doc.pdf --deps doc.d --deps-format make
   cat doc.d
   ```
   预期看到一行 `doc.pdf: doc.typ tiger.jpg`（路径相对 `proj/`）。

2. 编写 `Makefile`，把 `doc.d` include 进来：
   ```makefile
   doc.pdf: doc.typ
   	typst compile doc.typ doc.pdf --deps doc.d --deps-format make
   -include doc.d
   ```
   （`-include` 前缀的 `-` 表示首次没有 `doc.d` 时不报错。）

3. 第一次 `make`：因为 `doc.d` 不存在，走默认规则编译一次并生成 `doc.d`。

4. **只更新 `tiger.jpg` 的时间戳**（`touch tiger.jpg`），再次 `make`。因为 `doc.d` 里声明了 `doc.pdf: ... tiger.jpg`，Make 检测到 `tiger.jpg` 比 `doc.pdf` 新，于是重新编译。

5. 换成 JSON 与 Zero 格式各做一遍（`--deps-format json` / `zero`），用 `cat`/`od` 观察输出差异，对照 4.1 的对比表确认三种格式对「产物、分隔符、非 Unicode 路径」的不同处理。

**需要观察的现象**：

- Make 格式下，`make` 能根据 `doc.d` 正确识别 `tiger.jpg` 的变化并触发重编译——这就是依赖文件接入构建系统的实际效果。
- JSON 给出结构化对象、Zero 给出 NUL 分隔的字节流，二者都可用于自定义脚本，但消费方式不同。

**预期结果**：你得到一个最小可用的「Typst + Make 增量构建」示例，并亲历了依赖文件「由 Typst 产出 → 被 Make 消费」的完整闭环。若某步行为与预期不符，回到对应模块的源码精读核对（**待本地验证**各命令在本地环境的实际输出）。

> 进阶思考：如果 `doc.typ` 里 `#import` 了别的模块，`doc.d` 会自动把被 import 的文件也列进依赖吗？会的——因为依赖清单取自 `world.dependencies()`（本次**实际读取**的所有文件），而不只是 `#image` 引用。这正是 4.2 强调的「依赖 = 实际访问过的文件」。

## 6. 本讲小结

- `write_deps` 是依赖导出的唯一入口，按 `DepsFormat` 分发到 `write_deps_json` / `write_deps_zero` / `write_deps_make`；Make 分支仅在「有产物」时才写。
- 依赖清单的数据来源是 `SystemWorld::dependencies()`——它把本次编译**实际访问过**的 `FileId` 解析回磁盘路径，而非文档里写明的引用。
- `relative_dependencies` 把绝对路径换算成「相对调用 typst 时的工作目录」，项目内文件相对、项目外文件绝对，这是 Make/Ninja 消费依赖文件所需的坐标系。
- **JSON**（默认）：输出 `{inputs, outputs}` 结构，UTF-8 严格校验，遇非 Unicode 路径整体报错；产物为 stdout 时跳过。
- **Zero**：用 NUL 字节分隔、按原始字节写出，只列输入、能表达任意路径，适合 Ninja/`xargs -0`。
- **Make**：输出 `产物: 依赖` 规则，用源自 GCC `mkdeps` 的 `munge` 函数转义空格/`:`/`#`/`$`/反斜杠；非 Unicode 路径静默跳过；产物为 stdout 时报错。

## 7. 下一步学习建议

- 继续横向阅读 typst-cli 的「命令」模块：建议接着看 [u3-l5 init 项目脚手架](u3-l5-init-scaffold.md)，理解 `init` 如何复用 `packages::system()`（与依赖导出同样属于「CLI 与文件系统/包生态的接口层」）。
- 想深入依赖清单的源头？回到 [src/world.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs) 里 `FileStore::dependencies` 的实现（位于 `typst-kit`），看 `accessed` 槽位是如何在编译过程中被填充、又如何被 `dependencies()` 取走的；这条链路与 [u2-l5 watch 模式](u2-l5-watch-incremental.md) 的 `watcher.update(world.dependencies())` 直接相关。
- 想了解 `--make-deps` 这类弃用参数是如何平滑迁移到新接口的？重读 [src/compile.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs) 的 `CompileConfig::new_impl`，它把旧参数翻译成 `deps` + `deps_format` 并 push 弃用警告，是「保留兼容、底层统一」的范例。
