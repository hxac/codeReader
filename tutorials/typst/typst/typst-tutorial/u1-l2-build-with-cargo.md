# u1-l2 从源码构建：Cargo workspace 与构建命令

## 1. 本讲目标

上一讲我们从 README 认识了 Typst 的定位，并用官方安装包编译了第一份 PDF。这一讲我们换个视角：**把仓库当作一个 Rust 工程来看**。学完本讲，你应该能够：

1. 读懂仓库根目录 `Cargo.toml` 中的 workspace（工作区）声明：成员有哪些、默认构建谁、Rust 版本要求是什么。
2. 说出 workspace 中依赖的三种来源（`path`、`git`、registry），并理解 `typst-assets`、`typst-dev-assets` 这两个 git 依赖的作用。
3. 掌握本仓库的常用构建命令：`cargo build -p typst-cli`、`cargo run -p typst-cli -- compile`、`cargo test`，以及 `cargo testit`、`cargo docit` 两个自定义别名的定义位置与用途。
4. 独立完成一次「克隆 → 构建 → 运行」的完整流程，并记录编译耗时与产物路径，为后续阅读源码打好环境基础。

## 2. 前置知识

本讲只需要一点 Cargo 的常识，不要求你写过 Rust 项目。以下概念用大白话解释：

- **crate**：Rust 的最小编译单元，可以理解成「一个库或一个可执行程序」。上一讲提过，Typst 仓库里有很多个 crate，各司其职。
- **workspace（工作区）**：多个 crate 放在同一个仓库里统一管理的方式。根目录的 `Cargo.toml` 不再声明「我自己这个包」，而是声明「我管辖哪些成员 crate」，并集中管理版本号、依赖版本和编译选项，避免 17 个 crate 各写一份重复配置。
- **`[workspace.dependencies]`**：集中声明依赖的地方。各成员 crate 用 `{ workspace = true }` 来「引用」这里的声明，而不是自己写版本号。这样所有 crate 用到的 `serde`、`comemo` 等依赖版本天然一致。
- **依赖的三种来源**：
  - `path` 依赖：指向仓库内的另一个 crate（如 `crates/typst-syntax`），本地联调最方便；
  - `git` 依赖：指向某个 Git 仓库的固定提交（本仓库的字体资源等采用这种方式）；
  - registry 依赖：写版本号（如 `"1"`），从 crates.io 下载。
- **profile（编译配置）**：Cargo 允许为不同场景配置不同的优化等级，例如 `dev`（默认调试构建）、`release`（发布构建），还可以自定义 profile。
- **cargo 别名（alias）**：在 `.cargo/config.toml` 的 `[alias]` 段里，可以把一长串 cargo 子命令缩写成一个短命令。本仓库的 `cargo testit` 和 `cargo docit` 就是这么来的。

另外补充一个容易混淆的点：README 里提到的 `cargo install --locked typst-cli` 是**从 crates.io 安装发布版**，不会用到你手头这个 git 克隆；而本讲要做的是**在这个仓库里从源码构建**，两者产物来源不同。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [Cargo.toml](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/Cargo.toml) | workspace 根清单：成员列表、共享元数据、集中依赖表、编译 profile |
| [.cargo/config.toml](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/.cargo/config.toml) | cargo 别名定义：`testit`（跑集成测试）与 `docit`（构建文档站） |
| [docs/README.md](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/README.md) | 文档工程的目录结构与 `cargo docit` 使用说明 |
| [docs/Cargo.toml](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/Cargo.toml) | `typst-docs` 包（docs 也是 workspace 成员之一）的清单 |
| [crates/typst-cli/Cargo.toml](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/Cargo.toml) | CLI crate 的清单：可执行文件名、依赖与 feature 开关 |
| [docs/dev/architecture.md](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/dev/architecture.md) | 面向开发者的架构总览：目录职责与编译四阶段 |
| [README.md](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/README.md) | 「Contributing」一节给出了官方的源码构建命令 |

## 4. 核心概念与源码讲解

### 4.1 workspace 骨架：成员、默认成员与共享元数据

#### 4.1.1 概念说明

打开仓库根目录的 `Cargo.toml`，你会发现它**没有 `[package]` 段**——它不是某个包的清单，而是整个 workspace 的「户口本」。它回答三个问题：

1. 这个仓库管辖哪些 crate？（`members`）
2. 在根目录直接敲 `cargo build` 时默认构建谁？（`default-members`）
3. 全体成员共享哪些元数据？（`[workspace.package]`）

#### 4.1.2 核心流程

Cargo 在解析根 `Cargo.toml` 时的大致行为：

```text
读取 [workspace]
  ├─ members = ["crates/*", "docs", "tests", ...]
  │     → 展开通配符，找到 crates/ 下 17 个 crate + docs + tests 三件套
  ├─ default-members = ["crates/typst-cli"]
  │     → 根目录裸跑 cargo build/run/test 时，只作用于 typst-cli
  └─ resolver = "2"
        → 使用版本 2 依赖解析器（按 target/feature 精确合并依赖树）
```

各成员 crate 的 `Cargo.toml` 里写 `version = { workspace = true }`，即从 `[workspace.package]` 继承版本号、Rust 版本、edition 等字段。

#### 4.1.3 源码精读

[Cargo.toml:L1-L4](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/Cargo.toml#L1-L4)：声明 workspace 成员。`"crates/*"` 是通配符，匹配 `crates/` 下的全部 17 个 crate；`docs` 是文档站工程；`tests`、`tests/fuzz`、`tests/wrapper` 是集成测试与模糊测试相关的三个包。关键是 `default-members = ["crates/typst-cli"]`——这就是为什么 README 敢直接写 `cargo build --release` 而不用指定包名：在仓库根目录裸跑 cargo 命令时，默认只构建 CLI 这个可执行目标，而不是整个 workspace 的 21 个包。

[Cargo.toml:L6-L16](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/Cargo.toml#L6-L16)：`[workspace.package]` 集中存放共享元数据：版本号统一为 `0.15.1`，`edition = "2024"`（较新的 Rust 版本特性），`license = "Apache-2.0"` 等。成员 crate 通过 `workspace = true` 继承，改版本时只需改这一处。

[Cargo.toml:L8](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/Cargo.toml#L8)：`rust-version = "1.92"`，并带注释 `# also change in ci.yml`——这是**最低支持的 Rust 工具链版本**（MSRV）。低于这个版本的 rustc 会直接报错拒绝构建；升级 MSRV 时还要同步修改 CI 配置。仓库中没有 `rust-toolchain.toml` 文件（已确认），所以版本下限完全由这一行决定。

[docs/dev/architecture.md:L7-L33](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/dev/architecture.md#L7-L33)：官方开发者文档逐个列出 `crates/` 下各目录的职责（`typst` 主编译 crate、`typst-cli` 命令行、`typst-syntax` 解析器、`typst-eval` 解释器……）。这份清单与 `members` 通配符匹配到的 crate 一一对应，是下一讲「仓库全景」的预习材料。

#### 4.1.4 代码实践

**实践目标**：亲眼验证 workspace 成员与 default-members 的效果。

**操作步骤**：

1. 在仓库根目录执行 `cargo metadata --no-deps --format-version 1`（或加 `| python3 -m json.tool` 美化输出）。
2. 在输出的 JSON 里找 `"packages"` 数组，数一数有几个包，核对包名列表。
3. 再执行 `cargo build --help` 无必要；直接观察第 2 步结果即可。

**需要观察的现象**：`packages` 里恰好是 21 个包（`crates/` 下 17 个 + `typst-docs` + `tests` 下的 3 个包），其中名为 `typst-cli` 的包带有 `[[bin]]` 目标。

**预期结果**：包数量与 `members` 展开结果一致；这验证了「根 Cargo.toml 管辖整个仓库」的说法。具体输出条数以本地运行为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 README 的构建说明可以直接写 `cargo build --release`，而不需要 `-p typst-cli`？

**参考答案**：因为 [Cargo.toml:L3](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/Cargo.toml#L3) 设置了 `default-members = ["crates/typst-cli"]`，在根目录裸跑 cargo 命令时默认只作用于 CLI。写 `-p typst-cli` 只是把这个隐式行为显式化，两者等价。

**练习 2**：如果把某个新 crate 的目录放进 `crates/` 下但忘了写它自己的 `Cargo.toml`，`cargo build` 会发生什么？

**参考答案**：`crates/*` 通配符要求每个匹配的子目录都是一个合法的包（含 `[package]` 清单）。缺少清单的目录会让 Cargo 报错（工作区成员解析失败），提示该路径不是有效的包。因此新增 crate 必须同时提供 `Cargo.toml`。

### 4.2 依赖的三种来源：path、git 与 registry

#### 4.2.1 概念说明

`[workspace.dependencies]` 是本仓库依赖组织的核心：所有成员 crate 的外部依赖和内部依赖都先在这里「挂号」，成员再按名引用。这里集中出现了三种依赖来源：

| 来源 | 语法示例 | 本仓库用途 |
| --- | --- | --- |
| `path` | `typst-syntax = { path = "crates/typst-syntax", ... }` | 仓库内部 17 个 crate 互相引用 |
| `git` | `typst-assets = { git = "...", rev = "94dcb99" }` | 字体等资源仓库、部分第三方库，**锁定到固定提交** |
| registry | `serde = { version = "1.0.184", ... }` | 普通 crates.io 第三方库 |

理解这一点对构建体验很重要：**git 依赖意味着首次构建需要联网拉取外部仓库**，且被 `rev` 钉死，保证任何人任何时候构建拿到的是同一份代码。

#### 4.2.2 核心流程

依赖解析的大致流程：

```text
cargo 读取根 Cargo.toml 的 [workspace.dependencies]
  → 成员 crate（如 typst-cli）写 typst = { workspace = true }
      → Cargo 回查集中表，发现 typst 是 path 依赖 → 加入同一棵依赖树
  → 遇到 typst-assets 是 git 依赖 → 检查 ~/.cargo 缓存，没有则 clone 指定 rev
  → 遇到 serde 是 registry 依赖 → 按版本要求从 crates.io 解析下载
  → resolver = "2" 合并整棵依赖树，开始编译
```

#### 4.2.3 源码精读

[Cargo.toml:L18-L35](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/Cargo.toml#L18-L35)：集中表的前 18 行全部是 `typst-*` 系列的 **path 依赖**，把仓库内部 17 个 crate（`typst`、`typst-bundle`、`typst-cli`、`typst-eval`、`typst-html`、`typst-ide`、`typst-kit`、`typst-layout`、`typst-library`、`typst-macros`、`typst-pdf`、`typst-realize`、`typst-render`、`typst-svg`、`typst-syntax`、`typst-timing`、`typst-utils`）登记在册，并统一标注版本 `0.15.1`——这个版本号同时用于对外发布，所以内部 crate 之间也保持「已发布 crate」的形态。

[Cargo.toml:L36-L37](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/Cargo.toml#L36-L37)：两个特殊的 **git 依赖**。`typst-assets` 锁定到 typst-assets 仓库的 `94dcb99` 提交，主要提供字体等随编译器分发的资源（CLI 通过 feature 启用它，见下文）；`typst-dev-assets` 锁定到 `e26d7ca` 提交，存放开发/测试用的资源。此外集中表里 `codex`、`hayro` 系列、`krilla` 系列也是 git 依赖（[Cargo.toml:L51](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/Cargo.toml#L51)、[L66-L68](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/Cargo.toml#L66-L68)、[L81-L82](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/Cargo.toml#L81-L82)），说明 Typst 会第一时间跟踪一些尚未稳定发版的上游库。

[crates/typst-cli/Cargo.toml:L20-L31](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/Cargo.toml#L20-L31)：CLI crate 的依赖段几乎全部写成 `typst = { workspace = true }` 的形式——它不自己定版本，而是引用根清单的集中表。可以看到 CLI 直接依赖了编译器主体（`typst`）、各导出器（`typst-pdf`、`typst-render`、`typst-svg`、`typst-html`、`typst-bundle`）和解释器（`typst-eval`），印证了 architecture.md 里「CLI 是编译器与导出器之上较薄的一层」的说法。

[crates/typst-cli/Cargo.toml:L91-L104](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/Cargo.toml#L91-L104)：CLI 的 feature 开关。默认开启 `embedded-fonts`（把若干字体直接嵌进二进制，脱机也能排版基本文档）和 `http-server`（`typst watch` 与 HTML 导出内置 HTTP 服务器）；`self-update`、`vendor-openssl` 等按需开启。**从源码构建时默认就自带嵌入字体**，这就是为什么我们不需要额外装字体就能编译出带文字的 PDF。

[docs/Cargo.toml:L1-L7](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/Cargo.toml#L1-L7)：`docs` 目录本身也是 workspace 成员，包名 `typst-docs`，且 `publish = false`（永不发布到 crates.io，纯内部工具）。它同样依赖一大堆 `typst-*` crate 来生成文档站。

#### 4.2.4 代码实践

**实践目标**：弄清「构建 CLI 到底拉了哪些外部仓库」。

**操作步骤**：

1. 执行 `cargo build -p typst-cli`（首次构建，可联网）。
2. 构建完成后执行 `cargo tree -p typst-cli -i typst-assets`，查看 `typst-assets` 这个 git 依赖被谁、通过什么路径引入。
3. 再执行 `cargo tree -p typst-cli --depth 1` 观察 CLI 的直接依赖列表。

**需要观察的现象**：第 1 步首次构建时 Cargo 会输出 `Updating git repository ...` 字样，克隆 typst-assets 等仓库；第 2 步会显示 `typst-assets` 到 CLI 的依赖链路（经由 `typst-kit`）。

**预期结果**：`cargo tree -i typst-assets` 的反向依赖链以 `typst-cli` 为根。具体链路层级以本地输出为准（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `typst-assets` 用 `rev = "94dcb99"` 钉死提交，而不是跟踪分支最新？

**参考答案**：锁定提交保证**构建可复现**——任何人、任何时候、任何 CI 环境构建同一份 typst 源码，拉到的资源完全一致，避免上游仓库变动导致构建突然失败或行为漂移。升级资源需要显式改根 `Cargo.toml` 里的 rev，是一次受控变更。

**练习 2**：成员 crate 里写 `typst = { workspace = true }` 和直接写 `typst = { path = "../typst" }` 有什么区别？

**参考答案**：功能上都指向本地 crate，但 `workspace = true` 从根清单集中表继承版本号与其他配置。集中表统一管理 17 个内部 crate 的版本（`0.15.1`）与 feature 默认值（如 `typst-kit`、`typst-utils` 的 `default-features = false`，见 [Cargo.toml:L25](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/Cargo.toml#L25)、[L35](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/Cargo.toml#L35)），避免各成员配置不一致。

### 4.3 编译 profile：为什么调试构建也不算慢

#### 4.3.1 概念说明

编译器是典型的「自身逻辑重、依赖也重」的项目。默认的 `dev` profile 对所有代码都不做优化（`opt-level = 0`），跑起来会非常慢——对一个排版引擎来说，慢到没法日常使用。Typst 的对策藏在根 `Cargo.toml` 的 profile 段：

1. **依赖一律优化**：`dev` 模式下把所有第三方依赖开到 `opt-level = 2`；
2. **自定义 `dev-fast` profile**：在 `dev` 基础上把自家代码也开 `opt-level = 2`；
3. **`release` 极致优化**：thin LTO、单 codegen unit、给 CLI 二进制 strip 符号。

#### 4.3.2 核心流程

```text
cargo build                → profile = dev
    自家 crate：opt-level 0（编译快，运行慢）
    所有依赖：  opt-level 2（来自 [profile.dev.package."*"]）

cargo build --profile dev-fast
    全部 opt-level 2（Rust 编译稍慢，Typst 运行明显变快）

cargo build --release      → profile = release
    lto = "thin", codegen-units = 1，CLI 产物 strip
    （Rust 编译最慢，运行最快，用于发布）
```

#### 4.3.3 源码精读

[Cargo.toml:L157-L158](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/Cargo.toml#L157-L158)：`[profile.dev.package."*"]` 把 `dev` 模式下**所有依赖包**的优化等级设为 2。这是 Rust 社区常见的技巧：依赖很少改动，一次优化后持续缓存复用；自家代码保持 0 级以便快速增量编译调试。

[Cargo.toml:L160-L162](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/Cargo.toml#L160-L162)：自定义 `dev-fast` profile，继承 `dev` 并把整体 `opt-level` 提到 2。它主要服务于 `cargo docit`（见 4.4 节）——文档站要用 Typst 排版大量内容，纯 `dev` 太慢，纯 `release` 又要等很久的 Rust 编译，`dev-fast` 是折中。

[Cargo.toml:L164-L169](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/Cargo.toml#L164-L169)：`release` profile 启用 thin LTO（跨 crate 链接期优化）且 `codegen-units = 1`（牺牲并行编译换最优机器码），并单独给 `typst-cli` 开 `strip = true` 去掉二进制中的符号信息以减小体积。README 说优化后的二进制在 `target/release/`，对应 [README.md:L200-L210](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/README.md#L200-L210) 的官方构建说明。

#### 4.3.4 代码实践

**实践目标**：体会不同 profile 的「编译耗时 vs 运行性能」权衡。

**操作步骤**：

1. `cargo build -p typst-cli`（dev profile），用 `time` 记录耗时。
2. 写一个约 50 页的长文档（可复制重复内容），用 `./target/debug/typst compile` 编译它，记录排版耗时。
3. `cargo build -p typst-cli --profile dev-fast`，再用 `./target/dev-fast/typst compile` 编译同一文档，对比排版耗时。
4. （可选，时间充裕时）`cargo build -p typst-cli --release`，重复对比。

**需要观察的现象**：dev 版二进制的 Typst 排版耗时明显高于 dev-fast 与 release 版；而 dev-fast 的 Rust 增量编译仍比 release 快得多。

**预期结果**：三种 profile 下同一文档的排版耗时呈 dev > dev-fast ≈ release 的趋势。具体数字因机器而异（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`[profile.dev.package."*"]` 的 `"*"` 与 `[profile.release.package."typst-cli"]` 的具体包名，各自的表达意图是什么？

**参考答案**：`"*"` 是通配符，表示该 profile 覆盖**所有依赖包**但不含 workspace 自身成员——用于「依赖优化一次、自家代码快速迭代」；写具体包名则只对那一个包做额外调整——release 下只给 `typst-cli` 开 `strip`，因为最终分发的只是这一个二进制。

**练习 2**：为什么不直接用 `--release` 做日常开发？

**参考答案**：`release` 开启 thin LTO 且 `codegen-units = 1`（[Cargo.toml:L164-L166](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/Cargo.toml#L164-L166)），每改一行代码的重编译时间会非常长。日常开发更看重「改完尽快看到结果」，dev（依赖已优化）或 dev-fast 是更好的折中。

### 4.4 构建与运行命令速查：别名 testit 与 docit 的真身

#### 4.4.1 概念说明

本仓库的常用命令可以分成两组：**标准 cargo 命令**（`build`/`run`/`test`，配合 `-p` 选择包）和**仓库自定义别名**（`cargo testit`、`cargo docit`）。别名不是魔法，只是 `.cargo/config.toml` 里的一行字符串展开。上一讲留下的悬念——「`cargo docit` 定义在哪」——本节正式揭晓：**定义在仓库根的 [.cargo/config.toml](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/.cargo/config.toml)**。

#### 4.4.2 核心流程

```text
cargo docit compile --format pdf
  └─ Cargo 查 .cargo/config.toml [alias]
      └─ docit = "run --profile dev-fast --package=typst-docs --"
          └─ 实际执行：
             cargo run --profile dev-fast --package=typst-docs -- compile --format pdf
                 └─ 运行 typst-docs 包里的文档生成器

cargo testit <参数>
  └─ testit = "run --quiet --package=test-wrapper --"
      └─ 通过 tests/wrapper 里的 test-wrapper 包驱动 tests/ 集成测试套件
```

#### 4.4.3 源码精读

[.cargo/config.toml:L1-L7](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/.cargo/config.toml#L1-L7)：`testit` 别名定义为 `run --quiet --package=test-wrapper --`，即「运行 `tests/wrapper` 中的 test-wrapper 包，把其后参数原样传给它」。注释说明了 wrapper 存在的原因：需要在别的提交上重新运行测试运行器本身，以便再生成测试产物、产出测试报告。集成测试的详细玩法在 u1-l5 展开。

[.cargo/config.toml:L9-L14](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/.cargo/config.toml#L9-L14)：`docit` 别名定义为 `run --profile dev-fast --package=typst-docs --`。注释解释了 profile 选择：纯 `dev` 让 Typst 排版太慢，纯 `release` 让 Rust 编译太慢，`dev-fast` 是两者的折中——这正是 4.3 节 `dev-fast` profile 存在的直接原因。

[docs/README.md:L12-L25](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/README.md#L12-L25)：`docit` 的使用说明——`cargo docit compile` 生成网站版文档（产物在 `docs/dist/site`），`cargo docit compile --format pdf` 生成 PDF 版（产物在 `docs/dist/docs.pdf`）；[L27-L33](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/README.md#L27-L33) 是带热重载的 `cargo docit watch`。

[crates/typst-cli/Cargo.toml:L15-L18](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/Cargo.toml#L15-L18)：`[[bin]]` 段声明 CLI 的可执行文件名叫 `typst`、入口是 `src/main.rs`。这就是构建产物 `target/debug/typst` / `target/release/typst` 文件名的来源。

常用命令速查表（均可在仓库根目录执行）：

| 命令 | 作用 | 依据 |
| --- | --- | --- |
| `cargo build -p typst-cli` | 构建 CLI（dev profile） | default-members 使 `-p` 可省略 |
| `cargo build --release` | 构建发布版 CLI（官方推荐写法） | [README.md:L204-L208](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/README.md#L204-L208) |
| `cargo run -p typst-cli -- compile a.typ` | 构建并立即用本地 CLI 编译文档 | `--` 之后是传给 typst 的参数 |
| `cargo test -p typst-syntax --lib` | 只跑某个 crate 的单元测试 | workspace 成员可独立测试 |
| `cargo testit` | 跑集成测试套件 | [.cargo/config.toml:L7](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/.cargo/config.toml#L7) |
| `cargo docit compile` | 构建文档站 | [.cargo/config.toml:L14](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/.cargo/config.toml#L14) |

#### 4.4.4 代码实践

**实践目标**：完成「克隆 → 构建 → 运行」闭环，并弄清 `docit` 别名的展开过程。

**操作步骤**：

1. 确认工具链版本满足要求：`rustc --version`，输出应 ≥ `1.92`（[Cargo.toml:L8](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/Cargo.toml#L8) 的 MSRV）。
2. 在仓库根目录执行 `cargo build -p typst-cli`，用 `time` 记录总耗时；结束后确认产物 `target/debug/typst` 存在。
3. 写一个示例文件 `hello.typ`（可含标题与一段文字），执行 `cargo run -p typst-cli -- compile hello.typ`，确认生成 `hello.pdf`，记录本次命令耗时。
4. 执行 `cargo docit help`，观察它打印的是 `typst-docs` 程序的帮助信息——因为别名展开成了 `cargo run --profile dev-fast --package=typst-docs -- help`。

**需要观察的现象**：第 2 步首次构建会编译数百个依赖（含联网拉取 git 依赖），耗时较长；第 3 步 `cargo run` 因为第 2 步已构建完成，几乎不再重编译，直接执行编译；第 4 步会先编译 `typst-docs` 包再运行。

**预期结果**：得到可用的 `target/debug/typst` 与 `hello.pdf`；`cargo docit help` 输出文档生成器的子命令列表（`compile`、`watch` 等）。具体耗时数字因机器而异（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`cargo run -p typst-cli -- compile hello.typ` 中间的 `--` 能省吗？

**参考答案**：不能随意省。`--` 用于把其后参数**原样传给被运行的程序**（typst CLI）而不是交给 cargo 解析。省略后 `compile hello.typ` 会被 cargo 当成自己的参数处理，导致报错或行为不符预期。同理，`testit`/`docit` 别名末尾的 `--` 也是为了透传参数。

**练习 2**：假设你改了 `crates/typst-syntax` 里的代码，`cargo run -p typst-cli -- compile hello.typ` 会重新编译哪些东西？

**参考答案**：Cargo 增量编译只会重建依赖链上受影响的部分：`typst-syntax` 自身，加上依赖它的 `typst-library` → `typst-eval`/`typst-layout` → `typst` → `typst-cli` 等链上的 crate（取决于实际依赖图，见下一讲的 crate 依赖图），未受影响的依赖（如 PDF 库 krilla）不会重编。这也是 workspace + 增量编译对开发体验的价值。

**练习 3**：`cargo test`（在根目录裸跑）和 `cargo testit` 有什么区别？

**参考答案**：裸跑 `cargo test` 因 `default-members = ["crates/typst-cli"]` 只会执行 typst-cli 包自己的测试（单元测试与 CLI 层测试）；`cargo testit` 是别名，展开为运行 `test-wrapper` 包，驱动的是 `tests/` 目录那套「编译整份 Typst 文档并逐阶段比对」的集成测试套件，覆盖面完全不同（详见 u1-l5）。

## 5. 综合实践

**任务：为你的源码阅读环境做一次「构建体检」并产出记录。**

1. **环境检查**：运行 `rustc --version`，对照 [Cargo.toml:L8](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/Cargo.toml#L8) 确认满足 MSRV；运行 `cargo --version` 确认 cargo 可用。
2. **首次构建**：`time cargo build -p typst-cli`，记录总耗时，并执行 `ls -lh target/debug/typst` 记录二进制大小。
3. **功能验证**：编写一份 `hello.typ`（建议包含标题、一段中英文混排文字和一个简单公式），执行 `cargo run -p typst-cli -- compile hello.typ hello.png`，确认生成图片且中文正常显示（默认开启的 `embedded-fonts` feature 应已内嵌支持中文的字体；若缺字，记录现象，这本身就是 u5-l7 字体发现机制的预习）。
4. **增量验证**：随便改动 `crates/` 下任一 crate 的一个文档注释（`///`），再次 `time cargo build -p typst-cli`，对比两次耗时，体会增量编译。
5. **产出记录**：把以上数据整理成一张小表（步骤、命令、耗时、产物路径），存进你的学习笔记；后续讲义的实践都会复用这套环境。

提醒：第 4 步如果不想留下改动，完成后用 `git checkout -- <文件>` 或 `git restore <文件>` 还原。

## 6. 本讲小结

- 仓库根 `Cargo.toml` 是 workspace 清单：`members` 用 `crates/*` 通配 17 个编译器 crate 外加 docs 与 tests 相关包，`default-members = ["crates/typst-cli"]` 让根目录裸跑 cargo 命令默认只构建 CLI。
- `[workspace.package]` 集中管理版本（0.15.1）、MSRV（rust-version = "1.92"）、edition（2024）等元数据，成员以 `workspace = true` 继承。
- 依赖分三种来源：仓库内部用 path 依赖、资源与部分上游库用锁定 rev 的 git 依赖（`typst-assets`、`typst-dev-assets`）、其余走 crates.io；首次构建需要联网。
- profile 有三档：dev（依赖被强制 opt-level 2）、自定义 dev-fast（全 2 级，服务 `cargo docit`）、release（thin LTO + codegen-units 1 + CLI strip）。
- `cargo testit` 与 `cargo docit` 都是 `.cargo/config.toml` 中的普通 cargo 别名，分别展开为运行 `test-wrapper` 包与 `typst-docs` 包。

## 7. 下一步学习建议

环境就绪后，下一讲 **u1-l3「仓库全景：17 个 crate 的分工地图」** 将以 [docs/dev/architecture.md](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/docs/dev/architecture.md) 为纲，画出各 crate 之间的依赖与数据流方向。如果你想提前热身，可以先：

- 通读 architecture.md 的 Directories 与 Compilation 两节，试着用自己的话说出「解析 → 求值 → 布局 → 导出」四阶段各自由哪个 crate 负责；
- 用 `cargo tree -p typst-cli --depth 2` 打印 CLI 的依赖树，与 architecture.md 的目录清单相互印证；
- 浏览 [crates/typst/src/lib.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs) 顶部的文档注释，感受总装 crate 的角色。
