# init 项目脚手架

## 1. 本讲目标

本讲聚焦 `typst init` 命令。它把一个**模板包**（template package）变成一个**可以立即开始写作的项目目录**。学完本讲，你应该能够：

- 说清 `typst init @preview/xxx` 在背后经历的六个步骤，以及每一步调用哪个函数。
- 理解「包规格（PackageSpec）」如何被解析，以及省略版本号时 Typst 如何自动推断最新版本。
- 读懂模板包的 `typst.toml` 清单，知道 `[package]` 与 `[template]` 两个表分别承担什么职责，以及 `PackageManifest::validate` 校验了什么。
- 解释 `scaffold_project` 如何把模板目录原样复制成新项目，以及 `print_summary` 打印的「下一步命令」从何而来。
- 动手写一个本地 `@local` 模板包，并离线用 `typst init` 把它铺开成项目。

## 2. 前置知识

本讲是 [u3-l2 包存储与解析](u3-l2-package-storage.md) 的直接后续，会大量复用那里的结论。如果你还没读过，至少需要先理解下面三个概念：

- **包规格（PackageSpec）**：形如 `@preview/charged-ieee:0.1.0` 的字符串。它由三段组成：命名空间（`preview`）、名字（`charged-ieee`）、版本（`0.1.0`）。编译器和 CLI 都用同一个结构体来表达它。
- **`packages::system()`**：typst-cli 里只有十几行的薄壳函数，把命令行的 `PackageArgs`（`--package-path` / `--package-cache-path` 及同名环境变量）装配成一个 `SystemPackages`。
- **`SystemPackages::obtain(spec)`**：按「data 目录 → cache 目录 → 在线下载」三级优先级查找包，找到即返回包根目录（`FsRoot`）。

另外，你需要了解 **TOML** 是什么（一种类似 INI 的配置文件格式），以及一点 Rust 的 `Result` / `?` 错误冒泡与 serde 反序列化的基本概念。

> 关键直觉：`init` 本质上是一个「复制粘贴」命令。它不编译任何东西，只是：定位一个模板包 → 读取它的清单 → 把清单里 `[template].path` 指向的子目录，整体复制到用户指定的目录下。编译发生在用户随后运行 `typst watch` 时。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst-cli/src/init.rs` | 本讲的主角。`init` 命令的全部逻辑：解析规格、定位包、读清单、校验、复制目录、打印提示。 |
| `crates/typst-cli/src/packages.rs` | 薄壳：把 `PackageArgs` 装配成 `SystemPackages`，提供 `obtain` 与 `latest_version`。 |
| `crates/typst-cli/src/args.rs` | 定义 `InitCommand`（`init` 的命令行参数结构）与 `PackageArgs`。 |
| `crates/typst-kit/src/packages.rs` | `SystemPackages` / `FsPackages` 的真正实现：`obtain` 的三级查找、`latest_version` 的本地/在线推断。 |
| `crates/typst-syntax/src/package.rs` | 清单与规格的数据模型：`PackageManifest`、`TemplateInfo`、`PackageSpec`、`VersionlessPackageSpec`，以及 `validate`。 |
| `crates/typst-cli/tests/smoke.rs` | 端到端测试，其中的 `test_package_resolved` 展示了本地 `@local` 包目录的标准布局，是本讲实践的依据。 |

`init` 命令的接入点在 `main.rs` 的 `dispatch` 里，和其它子命令一样是一行分发：

[crates/typst-cli/src/main.rs:L71-L75](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L71-L75) —— 把 `Command::Init(command)` 分发给 `crate::init::init(command)`。

## 4. 核心概念与源码讲解

### 4.1 init 命令的整体流程与命令接入

#### 4.1.1 概念说明

`init` 要解决的问题是：用户想用某个现成模板（比如论文、简历、幻灯片模板）开始一个新文档，但不想手动去 Typst Universe 网站下载、解压、整理目录结构。`typst init @preview/charged-ieee` 一条命令，就应该在当前目录下铺出一个可立即编译的项目。

为此，`init` 需要回答四个问题：

1. **要哪个模板？** —— 从命令行拿到一个包规格字符串。
2. **模板在哪？** —— 用包存储（u3-l2 讲过的 `SystemPackages`）定位到磁盘上的包根目录。
3. **它真的是模板吗？** —— 读它的 `typst.toml` 清单，确认里面有 `[template]` 段。
4. **复制哪些文件、复制到哪？** —— 把 `[template].path` 指向的子目录复制到用户指定（或默认）的项目目录。

#### 4.1.2 核心流程

整个 `init` 是一个顺序的六步流水线，任何一步失败都用 `?` 冒泡成 `StrResult` 错误：

```
init(command)
  │
  ├─ ① 装配 SystemPackages          packages::system(&command.package)
  │
  ├─ ② 解析包规格（含版本自动推断）   command.template.parse()  ── 失败则 fallback 到 VersionlessPackageSpec + latest_version
  │
  ├─ ③ 定位包根目录                  packages.obtain(&spec) → root → package_path = root.path()
  │
  ├─ ④ 读清单 + 校验                 parse_manifest(package_path) → manifest.validate(&spec)
  │
  ├─ ⑤ 确认是模板 + 复制目录          manifest.template 必须是 Some → scaffold_project(...)
  │
  └─ ⑥ 打印成功提示                  print_summary(spec, project_dir, template)
```

#### 4.1.3 源码精读

整个 `init` 函数体就是上面流程的直接翻译，结构非常平整。先看它的命令参数来源 `InitCommand`：

[crates/typst-cli/src/args.rs:L137-L152](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L137-L152) —— `init` 只接收三个参数：`template`（模板规格字符串，必填）、`dir`（项目目录，可选）、`package`（用 `#[clap(flatten)]` 拼入的 `PackageArgs`，决定去哪找包）。注意 `template` 是一个裸 `String` 而非自定义类型，解析逻辑在 `init` 内部完成。

然后是 `init` 主体：

[crates/typst-cli/src/init.rs:L16-L53](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L16-L53) —— 六步流水线。几个关键点：

- 第 17 行先用 `packages::system(&command.package)` 装配包存储，这一步在 u3-l2 已讲透，这里只是复用。
- 第 31 行 `packages.obtain(&spec)` 返回包根目录；第 32 行 `root.path()` 取出磁盘路径。
- 第 35–36 行解析清单后立刻 `manifest.validate(&spec)`，确保磁盘上的清单和命令行指定的规格一致。
- 第 39–41 行用 `let-else` 守卫：清单里没有 `[template]` 段就直接 `bail!`，因为非模板包没有可复制的目录。
- 第 44 行决定项目目录：用户给了 `--dir` 就用用户的，否则用**清单里 `[package].name`**（不是命令行字符串）作为目录名。

#### 4.1.4 代码实践

**实践目标**：确认 `init` 的命令行接口与源码字段一一对应，并跟踪一次调用链。

**操作步骤**：

1. 构建出 typst 二进制（参考 [u1-l1](u1-l1-project-positioning.md)）：在仓库根运行 `cargo build`。
2. 运行 `./target/debug/typst init --help`，阅读输出。
3. 把 `--help` 列出的每个选项，对应到 [args.rs 的 `InitCommand`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L137-L152)：`<TEMPLATE>` → `template`、`[DIR]` → `dir`、`--package-path` / `--package-cache-path` → `package: PackageArgs`。

**需要观察的现象**：`init` 的选项比 `compile` 少得多——它不需要输入文件、输出格式、字体路径等，因为它根本不编译。

**预期结果**：你能把 `--help` 的每一行映射到 `InitCommand` 的某个字段，并理解为什么 `init` 不需要 `WorldArgs`。

**待本地验证**：`typst init --help` 的精确文案以本地输出为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `init` 不接受像 `compile` 那样的 `--format`、`--font-path` 等参数？

> **参考答案**：因为 `init` 只负责「定位包 + 复制目录」，不做任何编译或导出。字体、输出格式等只有在用户随后运行 `typst compile` / `typst watch` 时才有意义，所以这些参数属于编译命令，不属于 `init`。

**练习 2**：`init` 函数的返回类型是 `StrResult<()>`，它如何区分「成功」和「失败」？

> **参考答案**：返回 `Ok(())` 表示成功铺开了项目；任何一步（解析规格、定位包、读清单、校验、复制）失败都用 `?` 把错误冒泡成 `Err(EcoString)`，由 `main.rs` 的 `print_error` 打印并以非零退出码退出。注意 `init` 没有「软失败」概念（不像 `compile` 会返回 `Ok(())` 但退出码非 0），它要么全做成功、要么直接报错。

---

### 4.2 包规格解析与版本自动推断

#### 4.2.1 概念说明

用户在命令行里写的模板字符串，并不一定带版本号。下面两种写法都合法：

```
typst init @preview/charged-ieee:0.1.0   # 带版本
typst init @preview/charged-ieee          # 不带版本，自动取最新
```

这背后是一段「先严格、后宽松」的解析逻辑：先按**完整规格**（`PackageSpec`，必须有版本）解析；失败的话，再尝试按**无版本规格**（`VersionlessPackageSpec`）解析，然后想办法把版本补上。

「补版本」的方式取决于命名空间：

- `@preview` 命名空间：去 Typst Universe 在线索引查最新版本（需要网络）。
- 其它命名空间（如 `@local`）：在本地 **data 目录**里扫描该包的所有版本子目录，取最大值；找不到就报「请指定版本」。

#### 4.2.2 核心流程

规格解析与版本推断的判定树如下：

```
command.template.parse::<PackageSpec>()
  │
  ├─ 成功 → 直接用（用户带了版本）
  │
  └─ 失败 → 再试 VersionlessPackageSpec::from_str
              │
              ├─ 也失败 → 用最初的（更友好的）PackageSpec 错误信息
              │
              └─ 成功 → packages.latest_version(&versionless)
                          │
                          ├─ @preview → 联网查 index.json，取最大版本
                          ├─ 其它    → 扫本地 data 目录，取最大版本
                          └─ 找不到  → "please specify the desired version"
                        → versionless.at(version) 拼成完整 PackageSpec
```

注意一个细节：fallback 分支里「再试 `VersionlessPackageSpec`」如果也失败，会**故意丢弃**它自己的错误，改用第一步 `PackageSpec` 的原始错误信息——因为对用户而言「缺版本」和「格式错误」里，原始错误更准确地指出了格式问题。

#### 4.2.3 源码精读

规格解析的 fallback 写法用 `or_else` 串起来：

[crates/typst-cli/src/init.rs:L22-L28](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L22-L28) —— 先 `command.template.parse()`（即 `PackageSpec::from_str`），失败时进入闭包：闭包内先尝试把字符串解析成 `VersionlessPackageSpec`，若失败则用 `map_err(|_| err)?` 抛回**最初的** `err`；成功则用 `packages.latest_version(&spec)?` 补版本，再 `spec.at(version)` 拼成完整规格。

`PackageSpec` 与 `VersionlessPackageSpec` 的解析器都定义在核心语法 crate：

[crates/typst-syntax/src/package.rs:L244-L254](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/package.rs#L244-L254) —— `PackageSpec::from_str` 用 `unscanny::Scanner` 依次吃掉 `@命名空间`、`/名字`、`:版本` 三段。

[crates/typst-syntax/src/package.rs:L288-L300](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/package.rs#L288-L300) —— `VersionlessPackageSpec::from_str` 只吃 `@命名空间/名字` 两段；如果后面还剩字符（说明用户其实带了版本），就报「unexpected version」。

「补版本」的两个分支：

[crates/typst-kit/src/packages.rs:L127-L142](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L127-L142) —— `SystemPackages::latest_version`：`@preview` 委托给 `universe.latest_version`（联网），其它命名空间只在 `self.data`（data 目录）里找，**不查 cache 目录**——因为 cache 目录是给自动下载的包用的，不打算让用户手动放本地包。找不到就返回 `please specify the desired version`。

[crates/typst-kit/src/packages.rs:L199-L214](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L199-L214) —— `FsPackages::latest_version` 是本地扫描的实现：读 `命名空间/名字/` 目录下所有子目录名，把它们解析成 `PackageVersion` 取 `max()`。这就是「不带版本 → 自动取最新」在本地的工作方式。

补完版本后回到 `init`，第 31 行用 `packages.obtain(&spec)` 定位包：

[crates/typst-kit/src/packages.rs:L95-L124](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L95-L124) —— `obtain` 的三级短路：先查 `data`，再查 `cache`，仍没有且命名空间是 `@preview` 才联网下载并写回 cache。对 `@local` 这类命名空间，永远不会触发联网，只会查 data 和 cache。

#### 4.2.4 代码实践

**实践目标**：直观感受「带版本」与「不带版本」两条路径的不同，以及格式错误时报的是哪种错。

**操作步骤**：

1. 在有网络的环境运行 `./target/debug/typst init @preview/charged-ieee`（不带版本），观察它会去查在线索引、然后下载、最后铺出项目。
2. 删除刚生成的目录，再运行 `./target/debug/typst init @preview/charged-ieee:0.1.0`（带版本，版本号按索引里实际存在的填），对比是否跳过了「查最新版本」这一步。
3. 故意写一个格式错误的规格，例如 `./target/debug/typst init charged-ieee`（漏了开头的 `@`），观察错误信息。

**需要观察的现象**：第 3 步报的是 `PackageSpec::from_str` 的原始错误（提示规格必须以 `@` 开头），而不是 fallback 分支的错误。

**预期结果**：格式错误时，错误信息精准指向「必须以 `@` 开头」；不带版本时会多一次网络请求（查 index.json）。

**待本地验证**：`@preview` 的在线行为依赖网络与索引可用性；离线环境下第 1 步会失败，可改为本节 4.4 的本地 `@local` 实践来验证版本推断。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `latest_version` 对非 `@preview` 命名空间只查 data 目录、不查 cache 目录？

> **参考答案**：因为两类目录职责不同。data 目录是「用户有意存放的本地包」，cache 目录是「CLI 自动下载的在线包缓存」。本地包的「最新版本」应当反映用户放进去的版本，而 cache 里的内容是下载副产物，版本受下载历史影响、语义不可靠，所以不用它来推断最新版本。

**练习 2**：用户写 `typst init @local/demo`（不带版本），但 data 目录里没有任何 `@local/demo` 的版本，会发生什么？

> **参考答案**：`FsPackages::latest_version` 扫描失败返回 `None`，`SystemPackages::latest_version` 把它转成错误 `please specify the desired version`，`init` 用 `?` 冒泡，最终打印这条错误并退出。用户必须显式写 `@local/demo:0.1.0`。

---

### 4.3 清单解析与模板校验

#### 4.3.1 概念说明

定位到包根目录后，`init` 要读包根下的 `typst.toml`——这就是**包清单（manifest）**。清单是一份 TOML 文件，分几张表，对本讲最重要的是两张：

- `[package]`：包的元信息，至少要有 `name`、`version`、`entrypoint`。
- `[template]`：只有模板包才有这张表。它告诉 `init` 该复制哪个子目录、用户随后该编译哪个入口文件。

读清单之后，`init` 会调用 `manifest.validate(&spec)` 做一次**自洽性校验**：磁盘清单里写的名字/版本，必须和命令行指定的规格完全一致；如果清单声明了最低编译器版本，当前 typst 也得满足。

#### 4.3.2 核心流程

清单相关三步：

```
parse_manifest(package_path)
  │   读 <package_path>/typst.toml → toml::from_str → PackageManifest
  │   读失败 → "failed to read package manifest (...)"
  │   解析失败 → "package manifest is malformed (...)"
  ▼
manifest.validate(&spec)
  │   name 不一致 → "mismatched name"
  │   version 不一致 → "mismatched version"
  │   compiler 要求高于当前 → "requires Typst X or newer"
  ▼
manifest.template 必须是 Some，否则 "package {spec} is not a template"
```

#### 4.3.3 源码精读

清单数据模型在核心语法 crate：

[crates/typst-syntax/src/package.rs:L22-L34](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/package.rs#L22-L34) —— `PackageManifest` 用 serde 派生：`package: PackageInfo`（必填）、`template: Option<TemplateInfo>`（模板包才有）、`tool: ToolInfo`（第三方工具配置），以及一个 `#[serde(flatten)]` 的 `unknown_fields` 用来收集未知字段以供校验。

[crates/typst-syntax/src/package.rs:L80-L94](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/package.rs#L80-L94) —— `TemplateInfo` 是 `[template]` 表的映射：`path`（要复制的子目录）、`entrypoint`（相对 `path` 的编译入口文件）、可选的 `thumbnail`（缩略图）。这两个字段正是 `scaffold_project` 和 `print_summary` 依赖的信息。

解析与校验的 CLI 端实现：

[crates/typst-cli/src/init.rs:L56-L67](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L56-L67) —— `parse_manifest`：拼出 `typst.toml` 路径，`std::fs::read_to_string` 读出来（IO 错误包成 `FileError::from_io`），再用 `toml::from_str` 反序列化（解析错误只取 `err.message()`）。

[crates/typst-syntax/src/package.rs:L157-L183](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/package.rs#L157-L183) —— `validate` 做三项检查：`package.name == spec.name`、`package.version == spec.version`，以及（若清单声明了 `compiler`）当前编译器版本 `matches_ge(&required)`。任一不符都返回带原因的 `EcoString` 错误。

> **为什么需要 `validate`？** `obtain` 只检查「目录存不存在」，并不读清单。理论上磁盘上的 `typst.toml` 可能写着和目录名不一致的名字/版本（比如用户手动放错了）。`validate` 就是防止这种「目录名与清单不符」的情况静默通过。

#### 4.3.4 代码实践

**实践目标**：亲手触发 `validate` 的失败分支，理解它守护的是什么不变量。

**操作步骤**（源码阅读型 + 离线可验证）：

1. 准备一个本地包目录（完整做法见 4.4），把 `typst.toml` 里的 `name` 故意写成和目录名不同的值，例如目录是 `local/demo/0.1.0/` 但清单写 `name = "wrong"`。
2. 运行 `./target/debug/typst init @local/demo:0.1.0 --package-path <包根>`。
3. 阅读错误信息，对照 [validate 源码](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/package.rs#L157-L183) 确认命中的是哪一条分支。

**需要观察的现象**：报 `package manifest contains mismatched name 'wrong'`，并且**不会**生成项目目录。

**预期结果**：`validate` 在 `scaffold_project` 之前执行，所以校验失败时当前目录下不会留下任何残文件。

**待本地验证**：错误文案以本地实际输出为准。

#### 4.3.5 小练习与答案

**练习 1**：如果一个包的 `typst.toml` 里只有 `[package]` 表、没有 `[template]` 表，`init` 它会怎样？

> **参考答案**：`toml::from_str` 成功，`manifest.template` 是 `None`。`init` 第 39–41 行的 `let Some(template) = &manifest.template else { bail!(...) }` 守卫命中，报 `package {spec} is not a template` 并退出。也就是说，`init` 只接受「声明了自己是模板」的包。

**练习 2**：`validate` 为什么要检查编译器版本（`compiler` 字段）？

> **参考答案**：模板可能用到了较新版本 Typst 才有的语法或标准库功能。如果清单声明了 `compiler = "X.Y.Z"`，说明该模板至少需要这个版本。`validate` 在铺项目前就拒绝不满足要求的运行环境，避免用户随后 `typst watch` 时遇到莫名其妙的语法错误。

---

### 4.4 脚手架复制与成功提示

#### 4.4.1 概念说明

校验通过、确认是模板之后，剩下两件事都很「轻」：

- **复制目录**：把 `<包根>/<template.path>/` 这个子目录，整体复制成新项目目录。用的是第三方库 `fs_extra` 的 `dir::copy`，带 `content_only(true)` 选项——只复制目录里的内容，不额外套一层目录。
- **打印提示**：告诉用户项目建好了，并给出两条「下一步命令」（`cd` 进目录、`typst watch` 入口文件）。入口文件名取自清单的 `[template].entrypoint`，所以提示里的命令是准确的。

复制前有两个守卫：项目目录不能已存在（防止覆盖用户已有项目）；模板子目录必须存在（防止清单指向了不存在的路径）。

#### 4.4.2 核心流程

```
project_dir = command.dir 或 manifest.package.name

scaffold_project(project_dir, package_path, template)
  │   project_dir 已存在 → bail "project directory already exists"
  │   template_dir(<package_path>/<template.path>) 不存在 → bail "template directory does not exist"
  │   fs_extra::dir::copy(template_dir → project_dir, content_only=true)
  ▼
print_summary(spec, project_dir, template)
      打印 "Successfully created new project from {spec} 🎉"
      打印 "> cd <project_dir>"      （shell_escape 转义）
      打印 "> typst watch <template.entrypoint>"  （shell_escape 转义）
```

#### 4.4.3 源码精读

项目目录的取值：

[crates/typst-cli/src/init.rs:L44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L44) —— `Path::new(command.dir.as_deref().unwrap_or(&manifest.package.name))`。注意默认值取的是**清单里的包名**，而不是命令行写的模板字符串名。由于 `validate` 已保证 `manifest.package.name == spec.name`，二者其实相等；用清单里的值更稳妥（它来自磁盘权威数据）。

复制实现：

[crates/typst-cli/src/init.rs:L71-L93](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L71-L93) —— `scaffold_project`：先两个 `exists()` 守卫，再调 `fs_extra::dir::copy`。`CopyOptions::new().content_only(true)` 的作用是：把 `template_dir` **里面的文件**直接复制到 `project_dir` 下，而不是把 `template_dir` 本身作为一个子目录放进去。这样用户拿到的项目根，正好就是模板作者设计的项目根。

成功提示：

[crates/typst-cli/src/init.rs:L96-L126](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/init.rs#L96-L126) —— `print_summary`：用 `crate::terminal::out()` 拿到统一的彩色终端输出（与诊断、watch 共用同一条 stderr singleton，见 [u2-l4](u2-l4-diagnostics-terminal.md)）；两条「下一步命令」用 `shell_escape::escape` 转义路径，防止目录名或入口文件名里有空格等特殊字符导致用户复制粘贴执行时出错。命令前缀 `> ` 用灰色（`ColorSpec` 设为 dimmed）输出。

#### 4.4.4 代码实践（本讲核心实践，可离线完成）

**实践目标**：手工构造一个本地 `@local` 模板包，用 `typst init` 把它铺成项目，验证整条流水线。这一实践同时覆盖了 4.2 的本地版本推断与 4.3 的清单校验。

**操作步骤**：

1. 构建二进制：仓库根运行 `cargo build`。

2. 准备一个临时「包根」目录（模拟 data 目录），目录结构必须遵守 `命名空间/名字/版本` 三层约定（参考 [smoke.rs 的 `test_package_resolved`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/tests/smoke.rs#L137-L157)）：

   ```
   pkgs/local/demo/0.1.0/typst.toml
   pkgs/local/demo/0.1.0/template/main.typ
   ```

3. 写 `typst.toml`（注意：`[package]` 的 `name`/`version` 必须和目录一致；`[template]` 的 `path` 指向要复制的子目录，`entrypoint` 相对 `path`）：

   ```toml
   # 示例清单（自己手写，非项目原有文件）
   [package]
   name = "demo"
   version = "0.1.0"
   entrypoint = "lib.typ"

   [template]
   path = "template"
   entrypoint = "main.typ"
   ```

   > 这里 `[package].entrypoint` 写 `lib.typ` 只是满足清单必填字段要求（`init` 不会编译它）；真正影响 `init` 复制和提示的是 `[template]` 两个值。

4. 在 `template/main.typ` 里随便写点内容，例如：

   ```typ
   #set page(paper: "a4")
   Hello from my template.
   ```

5. 进到一个**空**工作目录，运行（注意 `--package-path` 指向 `pkgs` 这一层）：

   ```bash
   ./target/debug/typst init @local/demo:0.1.0 --package-path /绝对路径/pkgs
   ```

6. 现在再试一次**不带版本**的写法（先删掉生成的 `demo/` 目录）：

   ```bash
   ./target/debug/typst init @local/demo --package-path /绝对路径/pkgs
   ```

**需要观察的现象**：

- 第 5 步：当前目录下出现 `demo/` 子目录，里面有 `main.typ`（不是 `template/main.typ`——因为 `content_only(true)` 把内容直接铺到了 `demo/` 下）。终端打印 `Successfully created new project from @local/demo:0.1.0 🎉` 和两条灰色 `>` 提示。
- 第 6 步：不带版本也能成功——因为 `--package-path` 装配进了 data 槽，`FsPackages::latest_version` 在 `local/demo/` 下扫到 `0.1.0` 并取最大值，自动补全了版本。

**预期结果**：

```
Successfully created new project from @local/demo:0.1.0 🎉
To start writing, run:
> cd demo
> typst watch main.typ
```

随后 `cd demo && ./target/debug/typst watch main.typ` 应能正常编译（验证 `entrypoint` 提示是准确的）。

**待本地验证**：提示文案、目录名、是否自动推断版本，均以本地实际输出为准。若 `typst init` 报 `package @local/demo:0.1.0 is not found`，多半是 `--package-path` 指错了层级（应指向 `pkgs`，而非 `pkgs/local`）。

#### 4.4.5 小练习与答案

**练习 1**：如果你先 `mkdir demo` 再运行 `init`，会发生什么？为什么这样设计？

> **参考答案**：`scaffold_project` 第一个守卫发现 `project_dir.exists()` 为真，立刻 `bail! "project directory already exists"`。这样设计是为了**保护用户已有数据**——`fs_extra::dir::copy` 默认可能合并/覆盖目标目录里的同名文件，若不拦截，用户现有项目可能被模板文件污染甚至覆盖。

**练习 2**：`print_summary` 里那条 `typst watch` 命令的文件名从哪来？为什么不会写错？

> **参考答案**：来自清单 `[template].entrypoint`（即 `TemplateInfo::entrypoint`），它由模板作者声明、相对 `[template].path`。因为 `scaffold_project` 复制的正是 `[template].path` 子目录，所以入口文件一定落在新项目根下、路径与提示完全一致。若清单没写 `[template]`，`init` 早在守卫处就报 `not a template`，根本走不到 `print_summary`。

**练习 3**：`content_only(true)` 如果去掉，第 5 步的结果会有什么不同？

> **参考答案**：去掉后 `fs_extra::dir::copy` 会把源目录本身也复制过去，结果会多套一层：得到 `demo/template/main.typ` 而不是 `demo/main.typ`，于是 `print_summary` 提示的 `typst watch main.typ` 会找不到文件。`content_only(true)` 正是为了让新项目根正好对齐模板作者设计的项目根。

## 5. 综合实践

把本讲的知识串起来，做一个端到端的小任务：**从零造一个最小的本地模板包，并完成「自动推断版本 → 校验 → 复制 → 编译」全链路**。

1. 在 `pkgs/local/greeting/0.2.0/` 下放一个 `typst.toml` 和一个 `template/hello.typ`。清单里 `[package]` 的 `name="greeting"`、`version="0.2.0"`；`[template]` 的 `path="template"`、`entrypoint="hello.typ"`。
2. 故意再放一个 `pkgs/local/greeting/0.1.0/`（旧版本），验证「不带版本」时会取 `0.2.0` 而非 `0.1.0`（对照 [`FsPackages::latest_version` 取 `max()`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/packages.rs#L199-L214)）。
3. 运行 `typst init @local/greeting --package-path <pkgs>`，确认生成的目录名是 `greeting`、入口文件是 `hello.typ`。
4. `cd greeting && typst watch hello.typ` 验证可编译。
5. 在 0.2.0 的清单里加一行 `compiler = "999.0.0"`，重新 `init`（先删旧目录），观察 [`validate` 的编译器版本检查](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/package.rs#L172-L180) 如何拦住你。

完成这个任务，你就把本讲的四个最小模块（整体流程、版本推断、清单校验、脚手架复制）全部走了一遍。

## 6. 本讲小结

- `init` 是一条六步顺序流水线：装配 `SystemPackages` → 解析规格（含版本推断）→ `obtain` 定位包 → `parse_manifest` + `validate` → 确认是模板并 `scaffold_project` 复制 → `print_summary` 提示。
- 规格解析采用「先严格后宽松」策略：先按 `PackageSpec` 解析，失败再退到 `VersionlessPackageSpec` 并补版本；`@preview` 联网查索引，其它命名空间扫本地 data 目录取最大版本。
- `obtain` 按 data → cache → 在线下载 三级短路，仅 `@preview` 会联网；`@local` 这类永远只在本地查找。
- `typst.toml` 的 `[package]` 表是元信息、`[template]` 表声明「复制哪个子目录、入口是哪个文件」；`validate` 保证磁盘清单与命令行规格、与当前编译器版本自洽。
- `scaffold_project` 用 `fs_extra` + `content_only(true)` 把模板子目录原样铺成项目根，复制前用两个 `exists()` 守卫保护用户数据与防止清单指向空目录。
- `print_summary` 的「下一步命令」入口文件名取自清单 `[template].entrypoint`，并用 `shell_escape` 转义路径，确保可直接复制执行。

## 7. 下一步学习建议

- 本讲的 `packages::system()` 是 `init` 与 `SystemWorld` 共用的同一套包存储。想看「编译时」如何消费包，可回到 [u3-l2 包存储与解析](u3-l2-package-storage.md) 复习 `obtain` 与 `VirtualRoot::Package` 的关系。
- `init` 完成后用户的第一条命令通常是 `typst watch`。建议接着读 [u2-l5 Watch 模式与增量重编译](u2-l5-watch-incremental.md)，理解为什么 `typst watch hello.typ` 能在保存后自动重编译。
- 想了解模板包是如何被发布到 `@preview` 的、以及 `typst.toml` 完整字段含义，可直接阅读核心 crate 的 [crates/typst-syntax/src/package.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/package.rs)，它是清单模型的唯一权威定义。
- 如果你对 `init` 的端到端测试写法感兴趣，[tests/smoke.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/tests/smoke.rs) 里的 `test_package_resolved` / `test_package_unresolved` 展示了如何用子进程 + 临时目录验证包解析行为，是 u4-l7「测试与 smoke 测试」的预演。
