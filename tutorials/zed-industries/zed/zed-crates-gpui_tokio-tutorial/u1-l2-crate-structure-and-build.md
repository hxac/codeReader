# crate 结构与构建配置精读

## 1. 本讲目标

上一讲（u1-l1）我们从空中俯瞰了 gpui_tokio：它是一个约 100 行、只有两个源文件的「双运行时桥接」crate。本讲把镜头推近，逐行精读它 20 行的 `Cargo.toml`，并顺着 `.workspace = true` 这条线走到仓库根部的 `Cargo.toml`，看清 Zed 这个巨型 workspace 是如何管理上百个 crate 的公共配置的。

学完本讲，你应该能够：

1. 解释 `[lib] path = "src/gpui_tokio.rs"` 这一命名约定的来历（它与 Zed 仓库规范的关系），以及它与默认 `src/lib.rs` 风格的差异。
2. 说清 `.workspace = true` 依赖继承机制：字段从哪里继承、成员的 `features` 如何与工作区条目合并。
3. 解释为什么 tokio 依赖只声明 `rt` 和 `rt-multi-thread` 两个 feature，而本 crate 的代码确实只用到这些。
4. 独立完成 `cargo build -p gpui_tokio` 与 `cargo tree -p gpui_tokio`，看懂输出中的依赖继承关系。

## 2. 前置知识

本讲几乎不涉及 Rust 异步编程，但需要你理解几个 Cargo 的基础概念。如果你已经熟悉，可以快速扫过。

- **crate 与 package**：Rust 里 `package` 是一个可编译单元（有自己的名字、版本、依赖清单），`crate` 是编译产物。日常语境下两者常混用，本讲说「crate」时指 `crates/` 目录下的一个 package。
- **Cargo.toml（manifest，清单文件）**：每个 package 的「身份证 + 说明书」。`[package]` 段写名字和版本，`[lib]` 段告诉 cargo 库根文件在哪，`[dependencies]` 段列出依赖。
- **workspace（工作区）**：多个 package 共享一份锁文件（`Cargo.lock`）、一份输出目录（`target/`）和一份公共配置。Zed 仓库根部那份上千行的 `Cargo.toml` 就是工作区清单，`crates/` 下的一百多个 crate 都是它的成员（member）。
- **依赖继承**：工作区可以「上提」公共字段。成员 manifest 里写 `xxx.workspace = true`，意思是「这个字段的值去问工作区根清单」。版本号只写一处，全仓库统一。
- **feature（特性）**：Rust crate 的可选功能开关，通过 cargo feature 声明和启用。同一个依赖可以被不同 crate 按需启用不同功能子集，最终编译时取并集（后文详述）。
- **`cargo tree`**：打印依赖树的命令行工具，cargo 自带，用来观察「谁依赖谁、带了哪些 feature」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/gpui_tokio/Cargo.toml` | 本讲主角：仅 20 行的 crate 清单，逐行精读 |
| `crates/gpui_tokio/src/gpui_tokio.rs` | 唯一源码文件，用来验证「声明的依赖/feature 与实际用到的 API 一致」 |
| `Cargo.toml`（仓库根） | workspace 清单：members 注册、`[workspace.package]`、`[workspace.dependencies]`、`[workspace.lints]` |
| `Cargo.lock` | 锁定 tokio 等依赖的精确版本 |
| `crates/gpui/Cargo.toml` | 对照组：同样使用 `[lib] path = "src/gpui.rs"` 约定 |
| `crates/gpui_util/Cargo.toml` | 对照组：旧风格，无 `[lib]` 段，使用默认 `src/lib.rs` |
| `crates/livekit_client/Cargo.toml` | 佐证：成员在继承 gpui 依赖时按需叠加 feature 的真实例子 |
| `CLAUDE.md` | Zed 仓库贡献规范，明文规定了 `[lib] path` 命名约定 |

gpui_tokio 这个 crate 的全部文件就两个：`Cargo.toml` 和 `src/gpui_tokio.rs`。没有 README、没有 tests 目录、没有 examples——它是纯粹的基础设施 crate，这也是为什么它的配置能成为「最小 workspace 成员」的教科书样本。

## 4. 核心概念与源码讲解

### 4.1 一个「最小成员」的骨架：`[package]` 与 `[lints]`

#### 4.1.1 概念说明

一个 crate 要加入 workspace，必须在根部清单的 `members` 数组里挂号，然后在自己的 `Cargo.toml` 里声明身份。身份信息里有些是「每家不同」的（名字、版本、license），有些是「全仓库统一」的（Rust edition、是否发布到 crates.io、lint 规则）。Zed 的做法是：统一的字段上提到根清单的 `[workspace.package]` 和 `[workspace.lints]`，成员用 `.workspace = true` 继承，于是每个成员的清单可以瘦到几行。

#### 4.1.2 核心流程

cargo 解析一份成员清单的大致过程：

1. 读取 `crates/gpui_tokio/Cargo.toml` 的 `[package]` 段。
2. 遇到 `edition.workspace = true`，沿着目录向上找到带 `[workspace]` 段的根 `Cargo.toml`。
3. 在根清单 `[workspace.package]` 表里查 `edition` 键，把值填进来（这里是 `"2024"`）。
4. 同样地查 `[workspace.lints]` 填充 lint 配置、查 `[workspace.dependencies]` 填充依赖（见 4.3）。
5. 校验该 package 已在根清单 `members` 数组中挂号。

伪代码表示：

```text
member_manifest["edition"]
    = workspace_root["workspace.package"]["edition"]   # "2024"
member_manifest["publish"]
    = workspace_root["workspace.package"]["publish"]   # false
member_manifest["lints"]
    = workspace_root["workspace.lints"]                # rust/clippy 两组规则
```

#### 4.1.3 源码精读

gpui_tokio 清单的头部（[crates/gpui_tokio/Cargo.toml:L1-L9](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/Cargo.toml#L1-L9)）：

```toml
[package]
name = "gpui_tokio"
version = "0.1.0"
edition.workspace = true
publish.workspace = true
license = "Apache-2.0"

[lints]
workspace = true
```

逐行说明：

- `name` / `version` / `license`：每个 crate 各自不同，因此**行内声明**，不继承。
- `edition.workspace = true`：继承 Rust 语言版次，解析结果见根清单（[Cargo.toml:L267-L269](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/Cargo.toml#L267-L269)）：

  ```toml
  [workspace.package]
  publish = false
  edition = "2024"
  ```

  即全仓库统一使用 Rust 2024 版次。根清单的 `[workspace.package]` 只定义了 `publish` 和 `edition` 两个字段，所以 license、version 必须在各 crate 行内写。
- `publish.workspace = true` → `publish = false`：这个 crate **不发布到 crates.io**，是 Zed 工作区的内部私有 crate。这是 Zed 绝大多数 crate 的属性。
- `[lints] workspace = true`：继承 lint 规则。根清单里（[Cargo.toml:L1072-L1077](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/Cargo.toml#L1072-L1077)）：

  ```toml
  [workspace.lints.rust]
  unexpected_cfgs = { level = "allow" }

  [workspace.lints.clippy]
  dbg_macro = "deny"
  todo = "deny"
  ```

  继承后，任何成员里写 `dbg!()` 或 `todo!()` 都会被 clippy 以 deny 级别拒绝——lint 规则一处定义、全仓库生效。

最后，这个 crate 在 workspace 的挂号处（[Cargo.toml:L91-L100](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/Cargo.toml#L91-L100)）：

```toml
    "crates/gpui",
    "crates/gpui_apple",
    ...
    "crates/gpui_tokio",
    "crates/gpui_util",
```

注意 `members` 数组按字母序排列，`"crates/gpui_tokio"` 恰好排在第 98 行。上一讲说过约 14 个 crate 依赖 gpui_tokio——它们能通过 `gpui_tokio.workspace = true` 引用它，前提正是这里的成员注册加上 `[workspace.dependencies]` 里的条目（见 4.3）。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `.workspace = true` 被 cargo 解析展开后的最终值。

**操作步骤**：

1. 在 zed 仓库根目录执行（`cargo metadata` 只解析清单、不编译代码，速度很快）：

   ```bash
   cargo metadata --no-deps -p gpui_tokio
   ```

2. 在输出的 JSON 里搜索 `"edition"` 和 `"publish"` 字段。

**需要观察的现象**：JSON 里 gpui_tokio 条目的 `"edition": "2024"`、`"publish": false`，以及 `license`、`version` 等字段的值。

**预期结果**：edition 与 publish 的值和根清单 `[workspace.package]` 完全一致，证明继承生效；而 `license: "Apache-2.0"` 只出现在成员清单里。（具体 JSON 字段顺序以本地输出为准，待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：`edition.workspace = true` 最终解析为哪个值？来自哪个文件的哪一段？

答案：`"2024"`，来自仓库根 `Cargo.toml` 的 `[workspace.package]` 段（L267-L269）。

**练习 2**：`publish = false` 对 gpui_tokio 意味着什么？

答案：它不会被发布到 crates.io，是 Zed 工作区内部私有 crate；这个设置通过 `publish.workspace = true` 从根清单继承。

**练习 3**：`[lints] workspace = true` 继承了哪些规则？

答案：根清单 `[workspace.lints.rust]`（如 `unexpected_cfgs = "allow"`）和 `[workspace.lints.clippy]`（如 `dbg_macro = "deny"`、`todo = "deny"`）的全部规则（L1072 起）。

---

### 4.2 `[lib] path`：为什么库根文件叫 `gpui_tokio.rs` 而不是 `lib.rs`

#### 4.2.1 概念说明

cargo 的默认约定是：库 crate 的根文件（crate root）是 `src/lib.rs`。Zed 仓库规范**反其道而行**：新建 crate 应显式写 `[lib] path = "src/<crate名>.rs"`，用描述性文件名代替千篇一律的 `lib.rs`。这条规范写在贡献指南 `CLAUDE.md` 里（[CLAUDE.md:L14-L15](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/CLAUDE.md#L14-L15)）：

> * Never create files with `mod.rs` paths - prefer `src/some_module.rs` instead of `src/some_module/mod.rs`.
> * When creating new crates, prefer specifying the library root path in `Cargo.toml` using `[lib] path = "...rs"` instead of the default `lib.rs`, to maintain consistent and descriptive naming (e.g., `gpui.rs` or `main.rs`).

好处是直观：编辑器标签页、搜索结果、崩溃堆栈里出现的是 `gpui_tokio.rs` 而不是几十个重名的 `lib.rs`。

#### 4.2.2 核心流程

cargo 决定库根文件位置的规则：

```text
若 [lib] 段有 path字段 → 使用该路径（如 src/gpui_tokio.rs）
否则 → 按默认约定找 src/lib.rs
若两者都找不到 → 该 package 没有库目标，构建报错
```

#### 4.2.3 源码精读

gpui_tokio 的 `[lib]` 段（[crates/gpui_tokio/Cargo.toml:L11-L13](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/Cargo.toml#L11-L13)）：

```toml
[lib]
path = "src/gpui_tokio.rs"
doctest = false
```

- `path = "src/gpui_tokio.rs"`：库根指向与 crate 同名的文件。事实上 `src/` 目录下**只有这一个文件**，整个 crate 的全部实现都在里面（我们下一讲系列会精读它）。
- `doctest = false`：不为该 crate 的文档注释生成、编译、运行 doctest。效果是 `cargo test` 跳过它的 doctest 阶段。gpui 也做了同样设置（[crates/gpui/Cargo.toml:L42-L44](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/Cargo.toml#L42-L44)）：

  ```toml
  [lib]
  path = "src/gpui.rs"
  doctest = false
  ```

同一个仓库里也有旧风格的对照组：`gpui_util` 的清单**没有** `[lib]` 段（[crates/gpui_util/Cargo.toml:L1-L15](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_util/Cargo.toml#L1-L15)），它的库根走默认约定 `src/lib.rs`（该目录下确实存在 `src/lib.rs`）。这属于历史遗留——新规范只约束新建的 crate。

三个 crate 对比：

| crate | `[lib]` 段 | 库根文件 |
| --- | --- | --- |
| gpui_tokio | `path = "src/gpui_tokio.rs"`，`doctest = false` | `src/gpui_tokio.rs` |
| gpui | `path = "src/gpui.rs"`，`doctest = false` | `src/gpui.rs` |
| gpui_util | 无 | 默认 `src/lib.rs` |

#### 4.2.4 代码实践

**实践目标**：通过源码阅读体会「默认约定 vs 显式 path」的差异，并验证改动 path 会发生什么。

**操作步骤**：

1. 分别打开 `crates/gpui_tokio`、`crates/gpui`、`crates/gpui_util` 三个目录，观察各自的 `Cargo.toml` 与 `src/` 文件列表，填出上面那张对比表。
2. （可选，需本地验证）在本地把 `crates/gpui_tokio/Cargo.toml` 里 `path = "src/gpui_tokio.rs"` 这一行临时注释掉，运行 `cargo check -p gpui_tokio`，观察报错；看完后用 `git restore crates/gpui_tokio/Cargo.toml` 还原。

**需要观察的现象**：步骤 2 中 cargo 找不到 `src/lib.rs`，会报无法定位库目标的错误。

**预期结果**：构建失败，错误信息提示没有找到库根文件（具体报错文案待本地验证）；还原后恢复正常。这说明 `[lib] path` 不是装饰，而是 cargo 定位编译入口的依据。

#### 4.2.5 小练习与答案

**练习 1**：如果删掉 `[lib]` 的 `path` 行，cargo 会去哪里找库根？能找到吗？

答案：会按默认约定找 `src/lib.rs`；但 gpui_tokio 的 `src/` 下只有 `gpui_tokio.rs`，没有 `lib.rs`，所以找不到，构建失败。

**练习 2**：`doctest = false` 的直接效果是什么？

答案：`cargo test` 不再为该 crate 文档注释里的代码块编译并运行 doctest；gpui 的清单里有完全相同的两行设置。

**练习 3**：Zed 仓库规范（CLAUDE.md）对新建 crate 的库根命名有什么要求？

答案：优先用 `[lib] path = "src/<描述性名字>.rs"` 显式指定（例如 `gpui.rs`），不用默认的 `lib.rs`；同时禁止 `mod.rs`，子模块直接用 `src/some_module.rs`。

---

### 4.3 `[dependencies]` 与 workspace 依赖继承（`.workspace = true`）

#### 4.3.1 概念说明

Zed 有一百多个 crate，如果每个都自己写依赖版本（「我这里 tokio 用 1.40，你那里用 1.52」），版本漂移会带来重复编译和行为不一致。Cargo 的解法是**workspace 依赖继承**：根清单维护一张 `[workspace.dependencies]` 总表，每个依赖的来源、版本、默认特性只写一次；成员声明依赖时写 `依赖名.workspace = true`，表示「用总表里那份」。

需要继承的成员还可以**追加**自己的 `features`——成员 features 与总表 features 取并集。这是 Cargo 的既定合并规则，也是理解下一节 tokio 配置的钥匙。

#### 4.3.2 核心流程

成员依赖条目的解析流程：

```text
成员写: tokio = { workspace = true, features = ["rt", "rt-multi-thread"] }
   ↓
查根清单 [workspace.dependencies].tokio → { version = "1" }
   ↓
合并: 版本/来源等属性来自总表
      最终 features = 总表条目的 features（此处未声明，为空）
                    ∪ 成员追加的 ["rt", "rt-multi-thread"]
   ↓
若总表条目声明了 default-features = false 等属性，同样被继承
```

#### 4.3.3 源码精读

gpui_tokio 的依赖段（[crates/gpui_tokio/Cargo.toml:L15-L19](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/Cargo.toml#L15-L19)）：

```toml
[dependencies]
anyhow.workspace = true
gpui.workspace = true
gpui_util.workspace = true
tokio = { workspace = true, features = ["rt", "rt-multi-thread"] }
```

四个条目全部继承，但它们在总表里的形态分两类。查根清单的 `[workspace.dependencies]`（该表从 L271 开始）：

- **path（路径）依赖**——总表直接指向仓库内的 crate 目录：
  - `gpui = { path = "crates/gpui", default-features = false }`（[Cargo.toml:L357](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/Cargo.toml#L357)）
  - `gpui_util = { path = "crates/gpui_util" }`（[Cargo.toml:L365](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/Cargo.toml#L365)）
  - 顺带一提，gpui_tokio 自己也在总表里：`gpui_tokio = { path = "crates/gpui_tokio" }`（[Cargo.toml:L364](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/Cargo.toml#L364)），这正是上一讲提到的那些下游 crate 能引用它的原因。
- **版本号依赖**——来自 crates.io：
  - `anyhow = "1.0.86"`（[Cargo.toml:L521](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/Cargo.toml#L521)）
  - `tokio = { version = "1" }`（[Cargo.toml:L829](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/Cargo.toml#L829)）

注意 `gpui` 总表条目带 `default-features = false`：gpui 的默认特性是平台显示后端相关的一组功能（见 [crates/gpui/Cargo.toml:L19-L20](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/Cargo.toml#L19-L20)）：

```toml
[features]
default = ["font-kit", "wayland", "x11", "windows-manifest"]
```

总表把默认特性关掉，需要平台能力的成员再按需加回来。比如 livekit_client 在自己的清单里叠加了特性（[crates/livekit_client/Cargo.toml:L29](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/livekit_client/Cargo.toml#L29)）：

```toml
gpui = { workspace = true, features = ["screen-capture", "x11", "wayland"] }
```

这与 gpui_tokio 对 tokio 写 `workspace = true, features = [...]` 是**同一种机制**：基础属性继承自总表，成员只补充自己需要的部分。

#### 4.3.4 代码实践

**实践目标**：用 `cargo tree` 亲眼看到「继承 + 追加」的解析结果。

**操作步骤**：

1. 在 zed 仓库根目录执行：

   ```bash
   cargo build -p gpui_tokio
   cargo tree -p gpui_tokio
   ```

2. 再看带 feature 的视图：

   ```bash
   cargo tree -p gpui_tokio -e features
   ```

**需要观察的现象**：

- 第 1 步输出第一层是 `gpui_tokio v0.1.0`，其下挂着 `anyhow`、`gpui`、`gpui_util`、`tokio` 四个直接依赖，再往下是各自的传递依赖（gpui 的传递依赖相当多）。
- 第 2 步会额外显示 feature 节点，能看到 tokio 名下出现 `rt`、`rt-multi-thread`。

**预期结果**：依赖树与 4.3.3 的分析一致：tokio 的版本来自总表的 `version = "1"`（实际锁定版本见下一节），feature 恰好是成员追加的两个。（`cargo tree` 的具体输出格式与 feature 展示形态待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：gpui_tokio 的四个依赖中，哪几个在总表里是 path 依赖，哪几个是版本号依赖？

答案：`gpui`（L357）和 `gpui_util`（L365）是 path 依赖，指向仓库内目录；`anyhow`（L521，`"1.0.86"`）和 `tokio`（L829，`version = "1"`）是版本号依赖，来自 crates.io。

**练习 2**：成员写 `tokio = { workspace = true, features = ["rt", "rt-multi-thread"] }`，最终 features 怎么算？

答案：总表 tokio 条目没有声明 features，所以最终就是成员追加的两个：`rt` 与 `rt-multi-thread`；成员 features 与总表 features 取并集。

**练习 3**：为什么 livekit_client 要在自己的清单里给 gpui 加 `x11`、`wayland` 等 feature？

答案：总表的 gpui 条目是 `default-features = false`，默认不含平台后端特性；livekit_client 需要屏幕采集等平台能力，于是按需叠加——这是依赖继承机制里「总表定基础、成员补特性」的标准用法。

---

### 4.4 tokio 只开 `rt` 与 `rt-multi-thread`：按需声明特性

#### 4.4.1 概念说明

tokio 是个高度模块化的 crate：调度器、网络、文件、定时器、同步原语等都拆成可选的 cargo feature，按需编译。其中：

- **`rt`**：运行时核心——`Runtime`、`Handle`、`spawn`、`JoinHandle` 等任务驱动能力。
- **`rt-multi-thread`**：多线程调度器（`Builder::new_multi_thread()`），它隐含依赖 `rt`。

gpui_tokio 的角色是**运行时的建立者与转交者**，不是 tokio 功能的使用者：它建 runtime、把 future 扔进去、把 `JoinHandle` 的结果转回 GPUI。所以它只需要调度器能力，不需要 net / fs / time / io-util 那些「跑在运行时之上的库」才要的特性。真正需要那些特性的库（reqwest、tokio-tungstenite 等）会在**它们自己的依赖声明**里启用，Cargo 的 feature 统一（unification）机制保证最终只编译一份 tokio，特性取全图并集。

#### 4.4.2 核心流程

```text
gpui_tokio 声明: tokio 需要 rt, rt-multi-thread
其他 crate（如网络库）声明: tokio 需要 net, time, ...
   ↓ Cargo feature unification
最终二进制中的 tokio = 全部请求特性的并集，只编译一份
   ↓
gpui_tokio 单独构建时: tokio 只带 rt + rt-multi-thread（最省）
zed 整体构建时: tokio 带全图并集（够所有库用）
```

单 crate 声明最小特性集的意义：一是如实记录「本 crate 只用这些」，二是单独构建/测试该 crate 时（例如 CI 里 `cargo build -p gpui_tokio`）不必启用多余功能。

#### 4.4.3 源码精读

先看声明（[crates/gpui_tokio/Cargo.toml:L19](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/Cargo.toml#L19)）：

```toml
tokio = { workspace = true, features = ["rt", "rt-multi-thread"] }
```

再到源码里逐个核对 gpui_tokio 实际用到的 tokio API，验证「声明与用法严格对齐」：

- `pub use tokio::task::JoinError;`（[crates/gpui_tokio/src/gpui_tokio.rs:L6](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L6)）——把取消/panic 错误类型再导出给调用方。
- 构建多线程运行时（[crates/gpui_tokio/src/gpui_tokio.rs:L13-L18](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L13-L18)）：

  ```rust
  let runtime = tokio::runtime::Builder::new_multi_thread()
      // Since we now have two executors, let's try to keep our footprint small
      .worker_threads(2)
      .enable_all()
      .build()
      .expect("Failed to initialize Tokio");
  ```

  `new_multi_thread()` 来自 `rt-multi-thread`；注释也点明了「双执行器下尽量控制占用」，因此只配 2 个工作线程。`enable_all()` 打开该 tokio 构建里可用的 I/O 与时间驱动，供将来跑在上面的库使用。
- 运行时与句柄类型（[crates/gpui_tokio/src/gpui_tokio.rs:L35-L38](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L35-L38)）：`Option<tokio::runtime::Runtime>` 与 `tokio::runtime::Handle`——`rt` 提供。
- 在句柄上派生任务并取取消句柄（[crates/gpui_tokio/src/gpui_tokio.rs:L62-L63](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L62-L63)）：

  ```rust
  let join_handle = tokio.handle.spawn(f);
  let abort_handle = join_handle.abort_handle();
  ```

- 关停（[crates/gpui_tokio/src/gpui_tokio.rs:L44-L46](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L44-L46)）：`runtime.shutdown_background();`

以上全部落在 `rt` / `rt-multi-thread` 的能力范围内，源码里找不到任何 `tokio::net`、`tokio::fs`、`tokio::time` 的直接使用——两个特性的选择与代码严格自洽。

版本方面，整个仓库的 tokio 被锁文件固定在 1.52.1（[Cargo.lock:L18973-L18975](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/Cargo.lock#L18973-L18975)）：

```text
name = "tokio"
version = "1.52.1"
```

总表写 `version = "1"`、锁文件落到 1.52.1、全仓库共享同一份——这就是 4.3 所说「版本单一事实来源」的最终落地。

#### 4.4.4 代码实践

**实践目标**：对比「gpui_tokio 单独构建」与「整个 zed 构建」两种视角下 tokio 的 feature 集合，体会 feature unification。

**操作步骤**：

1. 构建（承接上一讲，你应该已经具备构建环境）：

   ```bash
   cargo build -p gpui_tokio
   ```

2. 查看 tokio 的实际版本：在 `Cargo.lock` 中搜索 `name = "tokio"`。

3. 查看 gpui_tokio 视角下 tokio 启用的特性：

   ```bash
   cargo tree -p gpui_tokio -e features
   ```

4. （可选，输出较多）在整个 zed 图谱里反查谁对 tokio 贡献了哪些特性：

   ```bash
   cargo tree -e features -i tokio | head -50
   ```

**需要观察的现象**：步骤 3 里 tokio 节点下只出现与 `rt`、`rt-multi-thread` 相关的 feature；步骤 4 里会看到多个 crate（网络、RPC 相关库）各自启用了不同的 tokio 特性。

**预期结果**：单独看 gpui_tokio，tokio 特性集合很小；放到整个依赖图里，tokio 的特性是所有请求者的并集，但版本只有一个（1.52.1），只编译一份。（`cargo tree -e features` 的确切输出待本地验证。）

#### 4.4.5 小练习与答案

**练习 1**：`rt` 和 `rt-multi-thread` 分别提供什么？两者什么关系？

答案：`rt` 提供运行时核心（`Runtime`、`Handle`、`spawn`、`JoinHandle` 等）；`rt-multi-thread` 在此基础上提供多线程调度器 `Builder::new_multi_thread()`，并隐含依赖 `rt`。gpui_tokio.rs L13 用的正是它。

**练习 2**：gpui_tokio 自己的代码用到了 tokio 的 net / fs / time 特性吗？

答案：没有。它只用运行时构建与任务句柄类 API（L6、L13-L18、L20、L35-L38、L44-L46、L62-L63、L84-L85、L97-L99），全部属于 `rt` / `rt-multi-thread`。

**练习 3**：既然 gpui_tokio 只开两个特性，最终 zed 二进制里的 tokio 会不会缺少网络能力？

答案：不会。Cargo 的 feature unification 会把整个依赖图中所有 crate 请求的 tokio 特性取并集，reqwest 等库自己声明了所需特性；最终构建里 tokio 只有一份（锁版本 1.52.1）。

---

## 5. 综合实践

把本讲全部知识串起来：**给 zed 工作区新增一个只依赖 gpui_tokio 的最小 crate**，完整体验「成员注册 → `[lib] path` 约定 → 依赖继承 → 构建验证」闭环。

**实践目标**：仿照 gpui_tokio 的写法，创建 `gpui_tokio_demo` crate，验证它能通过 workspace 继承拿到 gpui_tokio 及其依赖。

**操作步骤**：

1. 创建目录与清单 `crates/gpui_tokio_demo/Cargo.toml`，模仿 gpui_tokio 的结构（示例代码）：

   ```toml
   [package]
   name = "gpui_tokio_demo"
   version = "0.1.0"
   edition.workspace = true
   publish.workspace = true
   license = "Apache-2.0"

   [lints]
   workspace = true

   [lib]
   path = "src/gpui_tokio_demo.rs"
   doctest = false

   [dependencies]
   gpui_tokio.workspace = true
   ```

2. 创建库根文件 `crates/gpui_tokio_demo/src/gpui_tokio_demo.rs`（示例代码）：

   ```rust
   pub use gpui_tokio::Tokio;
   ```

   即使不直接依赖 gpui，重导出 `Tokio` 类型也能验证链接成功。

3. 在仓库根 `Cargo.toml` 的 `members` 数组里、按字母序加一行（紧挨 `"crates/gpui_tokio",` 之后，即 L98 附近）：

   ```toml
       "crates/gpui_tokio_demo",
   ```

4. 在仓库根目录执行：

   ```bash
   cargo build -p gpui_tokio_demo
   cargo tree -p gpui_tokio_demo
   ```

5. 实验结束后还原根清单：`git restore Cargo.toml`（或删除整个 demo 目录）。

**需要观察的现象**：步骤 4 的 `cargo tree` 输出中，第一层是 `gpui_tokio_demo → gpui_tokio`，第二层 gpui_tokio 下挂着 `anyhow`、`gpui`、`gpui_util`、`tokio`——你只写了一行 `gpui_tokio.workspace = true`，整条依赖链就到位了。

**预期结果**：构建成功；demo crate 的 edition（2024）、publish（false）、lints 全部与工作区一致，无需手写。（本讲义编写过程未执行以上命令，结果待本地验证。）

**注意**：此练习需要临时修改本地仓库的根 `Cargo.toml`，练习后请务必还原，不要把改动提交进仓库。

## 6. 本讲小结

- gpui_tokio 只有 20 行清单、1 个源码文件，是「最小 workspace 成员」的标准样本：`name/version/license` 行内声明，`edition/publish/lints` 通过 `.workspace = true` 从根清单的 `[workspace.package]`（`publish = false`、`edition = "2024"`）和 `[workspace.lints]` 继承。
- `[lib] path = "src/gpui_tokio.rs"` 是 Zed 仓库规范（CLAUDE.md 明文规定）的描述性命名约定，替代默认 `lib.rs`；`doctest = false` 让 `cargo test` 跳过该 crate 的 doctest。
- 四个依赖全部 `workspace = true` 继承：gpui、gpui_util 是总表里的 path 依赖，anyhow（`"1.0.86"`）、tokio（`version = "1"`，锁文件定为 1.52.1）是版本号依赖；成员的 `features` 与总表条目取并集（livekit_client 给 gpui 叠加 `x11/wayland` 是同一机制的用例）。
- tokio 只声明 `rt` 与 `rt-multi-thread`，与源码严格对齐：本 crate 只建运行时（`new_multi_thread` + 2 工作线程）、spawn、转发 `JoinHandle`，不用 net/fs/time；完整特性由全依赖图经 feature unification 取并集，最终只编译一份 tokio。

## 7. 下一步学习建议

本讲读懂了「配置」，下一讲回到「运行」本身，按大纲推荐顺序：

- **u1-l3（GPUI 的执行器与 Task 模型）**：精读 `crates/gpui/src/executor.rs` 与 `crates/gpui/src/app.rs`，理解 `cx.spawn` / `cx.background_spawn` 与 Task 的 drop 取消语义——这是桥接的另一端。
- **u1-l4（Tokio 运行时、Handle 与 JoinHandle）**：亲手用 `tokio::runtime::Builder` 建运行时，本讲 4.4 节出现的 `rt` / `rt-multi-thread`、`Handle`、`abort_handle` 会在那一讲展开。
- 等两个运行时都理解后，第二单元（u2）将从 `gpui_tokio::init` 开始逐段精读 `src/gpui_tokio.rs` 的实现。

建议同步阅读的配置类源码：`crates/gpui/Cargo.toml`（大型 crate 如何组织 features）与仓库根 `Cargo.toml` 的 `[workspace.dependencies]` 表（感受 Zed 依赖治理的规模）。
