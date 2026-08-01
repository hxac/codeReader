# 版本信息与定义位点 DefSite

## 1. 本讲目标

学完本讲后，你应当能够：

- 画出从 `build.rs` 到运行时 `version()` 的完整版本信息链路，并分别说清 `env!`/`option_env!`、`cargo:rustc-env`、`cargo:rerun-if-env-changed` 各自在哪一环发挥作用。
- 读懂 `version()` 如何用 `singleton!`（`LazyLock`）把一次性的 SemVer 解析缓存为全局唯一的 `&'static TypstVersion`，并解释 `*crate::singleton!(...)` 中的解引用为何必要。
- 掌握 `TypstVersion` 的字段封装（私有字段 + 访问器）、`Copy` 语义，以及 `display_commit` 的截断规则与「无 commit」兜底。
- 解释 `DefSite` 为何用 `(path, key)` 而非行号来定位「定义位点」，并跟踪 `typst-macros` 如何在 `#[func]`/`#[elem]` 宏展开时为函数、类型、元素、字段生成各自的 `key`。

本讲是专家层的收官篇。入门篇 u1-l1 已经搭起「`build.rs` → `env!` → `version()`」的骨架，本讲把这条链路往**深**处挖：讲透 `singleton!` 缓存、模块路径重命名的小技巧、版本结构体的封装设计；同时补上 u1-l1 完全没展开的另一半——`DefSite`。它是 Typst 在「宏展开后仍能稳定定位源码」这件事上的关键设计，被 `typst-macros`、`typst-library`、文档生成器广泛使用。

## 2. 前置知识

本讲假定你已读过 u1-l1，知道 `build.rs` 用 `cargo:rustc-env` 注入 `TYPST_VERSION`/`TYPST_COMMIT_SHA`、`version.rs` 用 `env!`/`option_env!` 读回。这里只补三个入门篇没展开、但本讲绕不开的点。

**编译期宏 `env!`/`option_env!` 读取的是「编译当前 crate 的 rustc 进程」的环境。** 这两个宏在编译期求值，把环境变量的值直接「烧」成字符串字面量写进二进制，和运行时的 `std::env::var` 是两回事。关键推论：变量是否可见，取决于 Cargo 给 rustc 准备的环境——它由两部分构成：Cargo 通过 `cargo:rustc-env` **追加**的变量，加上 rustc **继承自父进程**的环境。

**Cargo 会把「调用 cargo 时的环境变量」透传给所有子进程（build 脚本与 rustc 都在内）。** 所以 `TYPST_VERSION=9.9.9 cargo build` 时，这个变量既被 build 脚本的 `option_env!` 看到（用于决定要不要兜底），也被 rustc 的 `env!` 看到（用于烧进二进制）。`cargo:rustc-env` 只是「追加」，并非唯一来源。这条透传规则，正是 `build.rs`「外部设置优先、自身兜底」逻辑能成立的前提——否则外部设了变量、build.rs 又不注入，`env!` 就会扑空。

**`LazyLock<T>`：惰性、线程安全、全局唯一的单例。** 标准库 `std::sync::LazyLock<T>` 包装一个初始化闭包，首次 `Deref` 访问时才运行闭包，之后永远返回同一个值，全程线程安全。u1-l3 讲过 `singleton!` 宏正是基于它。本讲 `version()` 借它把「解析 SemVer」这件事保证全局只做一次。

> 名词速查：**SemVer** 指「语义化版本」`MAJOR.MINOR.PATCH`（如 `0.15.1`），`semver` crate 负责解析与比较。**`file!()`** 是编译期宏，展开为「当前源文件相对 crate 根的路径」字符串字面量。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [build.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/build.rs) | 编译 typst-utils 前运行，确保 `TYPST_VERSION`/`TYPST_COMMIT_SHA` 可用：外部已设则不动（依赖透传），未设则用包版本/git 兜底注入。仅 21 行。 |
| [src/version.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/version.rs) | `version()` 函数、`TypstVersion` 结构体及其访问器、`display_commit` 函数。99 行，本讲主角之一。 |
| [src/macros.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/macros.rs) | `singleton!` 宏定义（第 2-8 行），是 `version()` 惰性解析的底座。 |
| [src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs) | 用 `#[path]` 把 `version.rs` 挂为私有模块 `version_` 并 `pub use` 导出（第 16-17、28 行）；内联定义 `DefSite` 结构（第 459-476 行），本讲另一主角。 |

依赖层面，[Cargo.toml](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/Cargo.toml) 引入了 `semver`（解析版本字符串），是版本链路唯一的外部依赖。`DefSite` 的消费者主要在 `typst-macros`、`typst-library` 与 `docs`，第 4.4 节会跨 crate 跟踪。

---

## 4. 核心概念与源码讲解

本讲拆为四个最小模块：4.1 深挖 `build.rs` 的「外部优先 / 兜底注入」两条路径；4.2 讲 `version()` 如何用 `singleton!` 把一次性解析缓存成全局单例，并解释模块路径重命名的小技巧；4.3 讲 `TypstVersion` 的封装设计与 `display_commit` 截断；4.4 讲 `DefSite` 为什么用 key 而非行号，并跟踪 `typst-macros` 的 key 生成规则。

### 4.1 build.rs 环境变量注入：外部优先、兜底注入

#### 4.1.1 概念说明

u1-l1 已讲过这条链路的骨架。本节聚焦一个进阶问题：**为什么 `build.rs` 在变量「已设」时什么都不做，却仍然正确？** 答案就藏在第 2 节那条「Cargo 透传环境」的规则里。`build.rs` 对每个变量只有两种动作：检测到外部已设 → **不注入**（依赖透传让 rustc 自己看到）；检测到未设 → **注入一个兜底值**。它从不「覆盖」，只「补位」。

这种「外部优先、build.rs 兜底」的设计，把版本号的**最终决定权交给打包/发布工具链**：官方发版脚本可以设 `TYPST_VERSION` 指定一个与 Cargo 包版本不同的展示版本；distro 打包者、CI 也能各自注入；只有当所有人都没设时，才退回 `CARGO_PKG_VERSION`（Cargo 内置变量，等于 `Cargo.toml` 的 `version`）这个最朴素的默认值。

#### 4.1.2 核心流程

`build.rs` 对两个变量分别走一遍「检测 → 兜底」：

```text
对每个变量 VAR ∈ {TYPST_VERSION, TYPST_COMMIT_SHA}:
    声明 cargo:rerun-if-env-changed=VAR      # VAR 变化 → 重跑 build.rs
    if option_env!(VAR) 外部已设:
        什么都不做                            # 依赖 cargo 透传给 rustc
    else:
        取兜底值
        if 兜底值可用:
            println!("cargo:rustc-env=VAR=兜底值")   # 追加给 rustc
```

两个变量的兜底来源不同：

- `TYPST_VERSION` 的兜底值恒为 `CARGO_PKG_VERSION`，必然可用，所以是无条件兜底。
- `TYPST_COMMIT_SHA` 的兜底值要现去问 git：执行 `git rev-parse HEAD`，只有「命令存在、退出成功、输出是合法 UTF-8」三者同时满足才注入。因此它可能**两个值都没有**（外部没设、git 又失败），这正是 `version.rs` 用 `option_env!`（而非 `env!`）的原因。

#### 4.1.3 源码精读

- 引用 [build.rs:4-5](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/build.rs#L4-L5)：声明只要 `TYPST_VERSION` 或 `TYPST_COMMIT_SHA` 变化就重跑 build 脚本。没有这两行，Cargo 会缓存编译产物，外部改了变量也不会重新「烧」进二进制——这是「环境变量透传」常被忽视的搭档。
- 引用 [build.rs:7-9](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/build.rs#L7-L9)：`option_env!("TYPST_VERSION").is_none()` 为真（外部没设）时，才用 `cargo:rustc-env` 把 `TYPST_VERSION` 设为 `CARGO_PKG_VERSION`。注意 `is_none()` 取反：**已设则跳过**，正是「外部优先」。
- 引用 [build.rs:11-20](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/build.rs#L11-L20)：处理 `TYPST_COMMIT_SHA`。`if ... && let Some(sha) = Command::new("git").args(["rev-parse","HEAD"]).output()...` 这段用到了 **let-chain** 语法（`Cargo.toml` 继承自 workspace 的 `edition = "2024"`），把「外部未设」与「git 命令成功且输出合法」两个条件链式写在一起，取出 `sha` 后注入。

> 一个易踩的坑：`build.rs` 里的 `option_env!` 读的是「build 脚本编译运行时」的环境（即被 Cargo 透传的外部环境），而 `version.rs` 里的 `env!` 读的是「lib 编译时」的环境（由透传 + `cargo:rustc-env` 共同构成）。`cargo:rustc-env` **只对 lib/bin 编译可见，不影响 build.rs 自己**——所以 build.rs 用 `option_env!` 判断「外部真实值」、再决定是否给 lib「补」一个值，两边各看各的，逻辑自洽。

#### 4.1.4 代码实践

**实践目标**：亲手验证「外部优先」与「兜底注入」两条路径，体会 build.rs 的补位语义。

**操作步骤**（源码阅读 + 思想实验型，不改 typst 源码）：

1. 重读 [build.rs:7-9](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/build.rs#L7-L9)，确认「只有 `TYPST_VERSION` **未设**时才兜底」。
2. 在 typst 仓库根执行 `TYPST_VERSION=9.9.9 cargo build -p typst-utils`，再写一个临时二进制调用 `typst_utils::version()` 打印 `raw()`：
   - build.rs 的 `option_env!("TYPST_VERSION")` 得到 `Some("9.9.9")` → **不走兜底**；
   - 由于 Cargo 透传，rustc 的 `env!("TYPST_VERSION")` 也读到 `"9.9.9"`。
3. 再不带变量执行 `cargo build -p typst-utils`（先 `cargo clean -p typst-utils` 强制重编），此时 `option_env!` 为 `None` → build.rs 注入 `CARGO_PKG_VERSION`。

**需要观察的现象**：外部设置的环境变量**优先于** build.rs 的兜底默认值；不设时回退到包版本。

**预期结果**：`version().raw()` 在第 2 步为 `"9.9.9"`，第 3 步为包版本（如 `"0.15.1"`）。`display_commit(version().commit())` 在 git 仓库内构建返回 commit 前 8 位，在 crates.io 源码（无 `.git`）构建返回 `"unknown commit"`。**（待本地验证：第 2 步若未生效，多半是 Cargo 缓存未失效，确认 `rerun-if-env-changed` 已触发重编。）**

> **进阶提示**：若你想让**依赖 typst-utils 的自己的项目**也注入版本，要分清作用域。`env!("TYPST_VERSION")` 是在编译 **typst-utils 本身**时求值的，读的是 typst-utils 的 rustc 环境。所以：用 `TYPST_VERSION=x cargo build`（**环境变量**）会影响 typst-utils；但在你**自己的 build.rs** 里写 `println!("cargo:rustc-env=TYPST_VERSION=x")` 只会作用于你自己的 crate，**不会**改到 typst-utils。要改 typst-utils，必须用环境变量这条全局通道。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `build.rs` 在 `TYPST_VERSION` 已被外部设置时选择「什么都不做」，而不是再 `cargo:rustc-env` 一次？

> **参考答案**：因为 Cargo 已经把外部环境变量透传给了 rustc，rustc 的 `env!` 能直接读到。再注入一次是多余的；更糟的是，若 build.rs 读到的外部值与它自己能取到的值不一致，重复注入反而可能造成混淆。所以「外部优先、build.rs 只补位」是最简洁正确的策略。

**练习 2**：从 crates.io 下载的 `typst-utils` 源码包，`version().commit()` 最可能返回什么？为什么？

> **参考答案**：最可能返回 `None`（对应 `display_commit` 显示 `"unknown commit"`）。因为 crates.io 打包时会剥离 `.git` 目录，`build.rs` 执行 `git rev-parse HEAD` 会失败（或源码根本不在 git 仓库内），于是不注入 `TYPST_COMMIT_SHA`，`option_env!` 得到 `None`。

---

### 4.2 version() 惰性解析与 singleton!

#### 4.2.1 概念说明

`version()` 要返回当前 Typst 版本。它面对两个工程诉求：**(1) 解析有成本，但结果不变**——把 `"0.15.1"` 跑一遍 `semver::Version::parse` 是有开销的字符串处理，而版本号在一次运行里永远不变，没必要每次调用都解析；**(2) 返回值要能像常量一样便宜地传递**。typst-utils 的解法是：用 `singleton!` 宏把解析结果放进一个全局 `LazyLock`，第一次调用时解析一次并缓存为 `&'static TypstVersion`，此后所有调用直接返回同一个引用，零解析、零分配。

这是 u1-l3 讲过的 `singleton!` 的典型应用场景：**「昂贵且不变」的值，适合惰性单例**。与之对照，`Deferred`（u3-l3）是「昂贵且可并行」的值，走的是另一条路（后台线程）。两者都用「提前/延迟计算 + 缓存」的思路，区别只在「在哪个线程算」。

#### 4.2.2 核心流程

`version()` 的执行流程：

1. `singleton!(TypstVersion, { ... })` 展开为一个 `static VALUE: LazyLock<TypstVersion>`（见 4.2.3 的宏定义）。
2. 首次调用时，`LazyLock` 运行初始化闭包：`env!("TYPST_VERSION")` 取已烧入的版本字符串，`option_env!("TYPST_COMMIT_SHA")` 取 commit（可能 `None`）。
3. `semver::Version::parse(raw)` 解析；成功则填装 `TypstVersion`，失败则 `panic!`（文档明确标注了 `# Panics`）。
4. 之后每次调用直接返回缓存的 `&'static TypstVersion`，`version()` 再用 `*` 解引用复制出一个 `TypstVersion` 值返回。

伪代码：

```text
fn version() -> TypstVersion:
    static CACHE = LazyLock::new(|| {
        raw    = env!("TYPST_VERSION")            # 编译期已烧入
        commit = option_env!("TYPST_COMMIT_SHA")  # Some 或 None
        parse raw -> TypstVersion { raw, commit, ... }   # 只在首次执行
    })
    return *CACHE           # &'static TypstVersion 解引用复制
```

#### 4.2.3 源码精读

- 引用 [version.rs:18-37](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/version.rs#L18-L37)：整个函数体被 `crate::singleton!(TypstVersion, { ... })` 包裹，末尾的 `*)` 前面有个 `*`——因为 `singleton!` 返回的是 `&'static TypstVersion`，而函数签名要返回 `TypstVersion`（值），所以用 `*` 解引用。能这么做的前提是 `TypstVersion` 实现了 `Copy`（见 4.3）。
- 引用 [version.rs:20-21](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/version.rs#L20-L21)：`env!` 读 `TYPST_VERSION`（build.rs 保证有值），`option_env!` 读 `TYPST_COMMIT_SHA`（可能 `None`）。两者的 `'static` 生命周期来自「编译期烧入」。
- 引用 [version.rs:22-35](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/version.rs#L22-L35)：`match semver::Version::parse(raw)`。成功分支用 `version.major.try_into().unwrap()` 把 `semver` 的 `u64` 字段转成 `u32`——`try_into().unwrap()` 在版本号超过 `u32::MAX` 时会 panic，实际上不可能发生，故直接 `unwrap`。
- 引用 [macros.rs:2-8](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/macros.rs#L2-L8)：`singleton!` 宏本体，展开为 `static VALUE: LazyLock<$ty> = LazyLock::new(|| $value); &*VALUE`。注意 `static` 意味着「每个**调用点**生成一个独立的静态变量」——所以 `version()` 内的那一处 `singleton!` 全局只有一份缓存。

**模块路径重命名的小技巧。** 引用 [lib.rs:16-17](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L16-L17)：

```rust
#[path = "version.rs"]
mod version_;
```

这里把文件 `version.rs` 挂为私有模块，却刻意取名 `version_`（带下划线）而非 `version`。原因是 [lib.rs:28](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L28) 又 `pub use` 出了一个名为 `version` 的**函数**：

```rust
pub use self::version_::{TypstVersion, display_commit, version};
```

若模块也叫 `version`，就会和函数 `version` 同名冲突。加下划线是最轻量的规避手段，既保留「文件名 = `version.rs`」的直觉，又让模块名与导出的函数名错开。`#[path = "version.rs"]` 则告诉编译器「这个模块的源文件在 `version.rs`」，覆盖默认的「模块名 `version_` → 文件 `version_.rs`」推断。

#### 4.2.4 代码实践

**实践目标**：验证 `singleton!` 保证「全局只解析一次」。

**操作步骤**（源码阅读型）：

1. 读 [macros.rs:2-8](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/macros.rs#L2-L8)，确认 `singleton!` 展开出的 `static VALUE` 是「每调用点一份」。
2. 读 [version.rs:18-37](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/version.rs#L18-L37)，确认整个解析闭包在 `LazyLock::new` 里，因此只在首次 `Deref` 时执行。
3. 思想实验：在一个二进制里循环调用 `typst_utils::version()` 一万次。因为每次返回的都是同一个 `LazyLock` 缓存（`*` 解引用只是按位复制一个 5 字段的 `Copy` 值），解析只发生一次。

**需要观察的现象**：无论调用多少次，行为等价于「第一次解析、之后读常量」。

**预期结果**：解析成本摊销为 O(1)；若把 `semver::Version::parse` 想象成有副作用的日志，日志只会打印一次。**（待本地验证：可临时在 `version.rs` 解析分支加一行 `eprintln!`，观察到只输出一次。）**

#### 4.2.5 小练习与答案

**练习 1**：`version()` 返回 `TypstVersion`（值），而 `singleton!` 返回 `&'static TypstVersion`（引用）。函数体里的 `*` 解引用为什么不会「取走」全局缓存？

> **参考答案**：因为 `TypstVersion` 实现了 `Copy`（4.3 节会看到 `#[derive(Debug, Clone, Copy)]`）。对 `Copy` 类型，`*` 只是按位复制一个新值返回，原缓存安然不动。若 `TypstVersion` 不是 `Copy`，`*` 解引用后再按值返回就会触发 move，编译都过不了。

**练习 2**：为什么不直接写 `static VERSION: TypstVersion = parse(...)`，而要用 `LazyLock`？

> **参考答案**：Rust 的 `static` 常量必须是 **const 上下文**可求值的，而 `semver::Version::parse` 不是 `const fn`、`env!` 的值也无法在 const 上下文里解析成结构体字段，所以无法直接 `static` 初始化。`LazyLock` 正是用来「把运行时才能算的值，安全地放进全局」的标准工具。

---

### 4.3 TypstVersion 字段与 display_commit

#### 4.3.1 概念说明

`TypstVersion` 是版本解析后的**结果载体**。它的设计体现了一个 Rust 常见的封装纪律：**字段私有、访问器公开**。即便结构体派生了 `Clone + Copy`、字段本身是简单的数字和 `&'static str`，作者仍把 `major/minor/patch/raw/commit` 全部设为私有，只通过同名方法（`major()`、`minor()` …）暴露。这样做的好处是：将来若想改字段类型（比如把 `raw` 换成 `Cow<'static, str>`）或新增校验，不会破坏外部调用方。

它还派生了 `Copy`，这让 `version()` 能廉价地把值「复制」给调用方（呼应 4.2 的 `*` 解引用），调用方拿到的是一个独立的栈上副本，无需关心生命周期。

`display_commit` 是一个独立的自由函数，负责把「可能很长的 commit hash」转成人类友好的短形式（前 8 位），并在没有 commit 时给出 `"unknown commit"`。它不绑定在 `TypstVersion` 上，而是接收 `Option<&'static str>`，这样调用方可以自由选择是否带 commit 显示。

#### 4.3.2 核心流程

`TypstVersion` 的数据流向：

```text
build.rs 注入 ──▶ version() 解析 ──▶ TypstVersion { major, minor, patch, raw, commit }
                                            │
                                            ├─ major()/minor()/patch() → u32
                                            ├─ raw()    → &'static str   (保证符合 SemVer)
                                            └─ commit() → Option<&'static str>

commit ──▶ display_commit(commit) ──▶ 前 8 位字符串 或 "unknown commit"
```

`display_commit` 的截断规则：取 `s[..s.len().min(8)]`，即「不超过 8 个字符」的前缀。若 commit 本身短于 8 位则原样返回；`None` 则返回字面量 `"unknown commit"`。

#### 4.3.3 源码精读

- 引用 [version.rs:49-61](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/version.rs#L49-L61)：`TypstVersion` 结构体定义。`#[derive(Debug, Clone, Copy)]`（第 49 行），五个字段全部私有：`major/minor/patch: u32`、`raw: &'static str`、`commit: Option<&'static str>`。`'static` 来自编译期 `env!`/`option_env!`。
- 引用 [version.rs:63-90](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/version.rs#L63-L90)：五个访问器。`major()`/`minor()`/`patch()`/`raw()`/`commit()` 分别返回对应字段，`raw()` 文档注释特别声明「Guaranteed to conform to SemVer」——因为 `version()` 在解析失败时会 panic，所以走到这里的 `raw` 一定是合法 SemVer。
- 引用 [version.rs:93-99](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/version.rs#L93-L99)：`display_commit`。`const LENGTH: usize = 8;`，`match commit { Some(s) => &s[..s.len().min(LENGTH)], None => "unknown commit" }`。

> 关于 `&s[..s.len().min(LENGTH)]` 的安全性：这是对字符串切片。`s.len().min(LENGTH)` 保证结束索引 ≤ 8 且 ≤ `s.len()`，因此 `..` 不会越界。但要注意，这个写法**假定 commit 是 ASCII / 字符边界对齐的**——git 的短 hash 都是十六进制 ASCII，所以 8 字节必然落在字符边界上，切片安全。若哪天 commit 可能含多字节字符，这种按字节切片就得改成按 `char` 截断。

**真实消费者。** `TypstVersion` 在主仓库里被三处直接消费，印证了它的字段设计：

- 引用 [typst-library/.../sys.rs:7-12](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/sys.rs#L7-L12)：Typst 的 `sys.version` 把 `major()/minor()/patch()` 组装成脚本可见的 `Version`。
- 引用 [typst-pdf/src/metadata.rs:29](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-pdf/src/metadata.rs#L29)：PDF 元数据里写入 `format!("Typst {}", typst_utils::version().raw())`，直接用 `raw()` 拿原始字符串。
- 引用 [typst-syntax/src/package.rs:367](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/package.rs#L367)：包序列化时记录编译它的 Typst 版本。

#### 4.3.4 代码实践

**实践目标**：在二进制里调用 `version()`，逐字段打印，并体会 `display_commit` 的两种返回。

**操作步骤**：写一个依赖 `typst-utils`（path 或 crates.io）的小二进制（**示例代码**）：

```rust
use typst_utils::{display_commit, version};

fn main() {
    let v = version();
    println!("major = {}", v.major());
    println!("minor = {}", v.minor());
    println!("patch = {}", v.patch());
    println!("raw   = {}", v.raw());
    println!("commit = {:?}", v.commit());
    println!("display_commit = {}", display_commit(v.commit()));
}
```

**需要观察的现象**：五项字段一致地来自同一次解析；`display_commit` 把长 hash 截成 8 位。

**预期结果**：在 git 仓库内构建，`display_commit` 输出形如 `32fd4cc3`（当前 HEAD `32fd4cc38…` 的前 8 位）；在无 git 的源码构建输出 `unknown commit`。**（待本地验证。）**

#### 4.3.5 小练习与答案

**练习 1**：`TypstVersion` 的字段都是简单类型，为什么不让它们 `pub`、省掉五个访问器？

> **参考答案**：封装纪律。字段私有 + 访问器公开，把「内部表示」与「对外契约」解耦。日后若改字段类型或表示方式（例如 `raw` 改成预解析结构、`commit` 改成 `u64` 哈希），只要访问器签名不变，调用方代码就不用改。`#[derive(Copy)]` 已经让使用足够便利，不必为此牺牲封装。

**练习 2**：`display_commit(None)` 返回的 `"unknown commit"` 是 `&'static str`。这个返回类型和 `commit: Option<&'static str>` 的生命周期是怎么对齐的？

> **参考答案**：`display_commit` 的签名是 `fn display_commit(commit: Option<&'static str>) -> &'static str`。`Some(s)` 分支返回 `&s[..]`，因为 `s: &'static str`，其切片也是 `'static`；`None` 分支返回字符串字面量 `"unknown commit"`，本身即是 `'static`。两端都是 `'static`，故统一返回 `'static str`，调用方无需管理生命周期。

---

### 4.4 DefSite 定义位点设计：为什么用 key 而非行号

#### 4.4.1 概念说明

`DefSite` 回答的问题是：**「这个东西」是在源码的哪里定义的？** 这里的「东西」特指 Typst 脚本世界里的函数、类型、元素（element）和字段——它们都由 Rust 侧的 `#[func]`/`#[elem]` 等宏生成。知道定义位点有两个用途：一是文档生成器要标注「此项定义于某文件」；二是热重载（hot reload）要在文件被编辑后重新定位到对应项。

最朴素的定位方式是「文件路径 + 行号」，但 Typst 在这里刻意**不用行号，改用一个「文件内唯一的 key」**。原因是 [lib.rs:459-466](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L459-L466) 文档注释里讲的两条：

1. **宏展开会让行号失真。** 一个标了 `#[func]` 的方法位于某个 `#[scope]` 的 `impl` 块里，宏展开后，该方法拿到的「行号」其实是整个 `impl` 块的行，而不是方法自身的行——几乎没用。
2. **编辑会让行号漂移。** 热重载场景下，用户在文件里增删几行，原来第 42 行的定义就跑到了别处。用行号定位的话，每次编辑都得重新编译才能对上号。

用「文件内唯一的语义 key」（比如函数名 `page`、或带父级的 `Elem::field`）就稳定得多：只要定义的名字没改，文件怎么编辑都不影响定位。这正是 `DefSite` 用 `(path, key)` 而非 `(path, line)` 的根本动机。

#### 4.4.2 核心流程

`DefSite` 的生命周期分「构造」与「消费」两段：

```text
① 构造（编译期，在 typst-macros 的 #[func]/#[elem] 宏里）
   file!()                  → path   (当前源文件相对 crate 根的路径)
   由宏算出 def_site_key    → key    (文件内唯一标识，可含语义父级)
   生成代码: DefSite { path: file!(), key: #def_site_key }

② 消费（运行时）
   文档生成器: 把 (path, key) 写进文档元数据，标注「定义于 path，标识为 key」
   热重载:     用 (path, key) 在编辑后的文件里重新定位同一项
```

关键在于 **key 的生成规则**，它由 `typst-macros` 在宏展开时决定，规则很统一：

| 项的种类 | key 生成规则 | 示例 |
|----------|--------------|------|
| 顶层函数 | 函数名 | `page` |
| 某类型的关联函数（方法） | `父类型::方法名` | `Enum::from` |
| 元素（element） | 元素名 | `heading` |
| 元素的字段 | `元素名::字段名` | `heading::level` |
| 函数的参数 | `父key::参数名` | `page::paper` |

这套「父子用 `::` 拼接」的命名，保证同一个文件内不同项的 key 互不冲突，且能反映语义归属。

#### 4.4.3 源码精读

- 引用 [lib.rs:459-476](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L459-L476)：`DefSite` 定义。文档注释（459-466 行）解释了「为何不用行号」。`#[derive(Debug, Copy, Clone, Eq, PartialEq)]`（467 行）——它是个轻量值类型，两个字段都是 `&'static str`（`path`、`key`，472/475 行），`Copy` 让它能像整数一样到处传递、比较。`Eq + PartialEq` 让它能作为「定义位点」被比较、去重。

**key 的生成（在 typst-macros）。** 跨 crate 跟踪四处典型构造点：

- 引用 [typst-macros/src/func.rs:309-315](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-macros/src/func.rs#L309-L315)：函数的 key。若该函数有关联的父类型，key = `format!("{parent}::{ident}")`，否则就是 `ident.to_string()`。这就是「顶层函数用名字、方法用 `父::名`」的规则。
- 引用 [typst-macros/src/func.rs:350](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-macros/src/func.rs#L350)：把算好的 key 填进 `DefSite { path: file!(), key: #def_site_key }`，包进 `NativeFuncData`。注意 `file!()` 是在**宏展开点**求值的，所以 path 指向用户写 `#[func]` 的那个真实源文件。
- 引用 [typst-macros/src/elem.rs:389](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-macros/src/elem.rs#L389)：元素的 key 直接取元素名 `ident.to_string()`。
- 引用 [typst-macros/src/elem.rs:468-476](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-macros/src/elem.rs#L468-L476)：元素字段的 key = `format!("{elem_ident}::{ident}")`，即「元素名::字段名」，并同样用 `file!()` 作为 path。

**消费（运行时）。** 引用 [docs/src/reflect.rs:125-134](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/docs/src/reflect.rs#L125-L134)：文档生成器的 `describe_def_site(site: DefSite)` 把 `site.path` 虚拟化成仓库内路径、`site.key` 原样写入字典，最终体现在文档里「此项定义于某文件」的标注。在 `typst-library` 里，`DefSite` 还被存进元素/字段/函数的元数据（如 [content/field.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/field.rs)、[foundations/func.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/func.rs)），通过 `def_site()` 访问器暴露。

> 设计哲学：`DefSite` 与 u3-l2 的 `Protected`、u3-l3 的 `defer()` 一脉相承——都是「用数据/类型的**形状**来表达纪律」。这里把「稳定的定义位置」显式建模成 `(path, key)`，逼着所有定位逻辑放弃易变的行号、改用语义 key，从源头消除了「宏展开行号失真」「编辑行号漂移」两类顽疾。

#### 4.4.4 代码实践

**实践目标**：亲手构造一个 `DefSite`，理解两个字段的含义，并跟踪一个真实宏的 key 生成。

**操作步骤**：

1. 写一段依赖 `typst-utils` 的**示例代码**，构造并打印 `DefSite`：

   ```rust
   use typst_utils::DefSite;

   fn main() {
       // file!() 在编译期展开为当前文件相对 crate 根的路径
       let site = DefSite { path: file!(), key: "my-item" };
       println!("{site:?}");        // 用 derive 的 Debug 打印
       println!("path = {}", site.path);
       println!("key  = {}", site.key);

       // 两个字段都是 &'static str，DefSite 是 Copy，可以随意复制、比较
       let site2 = site;
       assert_eq!(site, site2);
   }
   ```

   注意 `path`/`key` 是 `pub` 字段，可以直接结构体字面量构造。

2. 跟踪真实 key 生成：打开 [typst-macros/src/func.rs:309-315](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-macros/src/func.rs#L309-L315)，对照一个你熟悉的 Typst 函数（如 `page`），判断它的 `def_site.key` 会是 `page`（顶层函数，无父类型）还是 `某父::page`。

**需要观察的现象**：`file!()` 展开出的路径是相对 crate 根的；`DefSite` 可 `Copy`、可比较；key 是纯字符串、不含行号。

**预期结果**：第 1 步打印形如 `DefSite { path: "src/main.rs", key: "my-item" }`（path 因项目布局而异，**待本地验证**）；第 2 步确认顶层函数的 key 就是函数名本身。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `DefSite` 的 `key` 用「`父类型::字段`」这种字符串，而不是 `(模块路径, 行号)`？

> **参考答案**：两个原因。其一，宏展开会让行号失真——`#[func]` 方法拿到的行号其实是整个 `impl` 块的行；其二，热重载时用户编辑文件会让行号漂移。语义 key（名字）只要不改定义就稳定不变，既能跨编辑可靠定位，又能在文件内唯一区分各项。

**练习 2**：`DefSite` 派生了 `Eq + PartialEq`。这有什么实际用途？

> **参考答案**：让「定义位点」可比较、可去重。例如把若干项的 `DefSite` 收集起来做去重，或在热重载时用「新旧 `DefSite` 是否相等」判断「这是不是同一个定义」。因为字段都是 `&'static str`（指针比较即内容比较），相等判断非常廉价。

**练习 3**：`DefSite` 用 `file!()` 取 path。如果两个不同 crate 里都有名为 `page` 的函数，光看 `key = "page"` 会冲突吗？

> **参考答案**：不会，因为定位用的是 `(path, key)` **二元组**，不是单看 key。`path` 来自 `file!()`，包含了相对 crate 根的文件路径，不同 crate 的同名函数 path 不同，组合起来就能区分。key 只需在**单个文件内**唯一即可。

---

## 5. 综合实践

把本讲的两条主线（版本链路 + DefSite）串成一个贯通任务。

**任务**：在一个依赖 `typst-utils` 的二进制里，做三件事并用断言/打印验证。

1. **版本链路端到端**。不带任何环境变量编译，打印 `version()` 的 `major/minor/patch/raw` 与 `display_commit(version().commit())`；再 `cargo clean -p typst-utils` 后用 `TYPST_VERSION=9.9.9` 重新编译，确认 `raw()` 变成 `"9.9.9"`、`major()` 变成 `9`。这验证了「build.rs 兜底 vs 外部优先」两条路径（**待本地验证**）。

2. **阅读 singleton! 缓存**。在 [version.rs:22](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/version.rs#L22) 的 `Ok` 分支临时加一行 `eprintln!("parsing version once");`，循环调用 `version()` 一万次，观察这行日志只打印一次——直观证明 `singleton!` 把解析缓存为全局单例。验证完记得删掉这行（**不要提交对源码的修改**）。

3. **DefSite 构造与定位**。用 `DefSite { path: file!(), key: "my-item" }` 构造一个位点，打印它；再打开 [typst-macros/src/elem.rs:468-476](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-macros/src/elem.rs#L468-L476)，任选一个 Typst 元素（如 `heading`）和它的一个字段（如 `level`），写出该字段在宏展开后生成的 `DefSite` 的 `path`（`typst-library` 里定义该元素的文件）和 `key`（`heading::level`）。

**验收**：能口头复述「外部环境变量 → build.rs 检测 → env! 烧入 → singleton! 缓存 → version() 返回 Copy 值」整条链路；能解释 `DefSite` 为何放弃行号、改用语义 key，并能说出 key 的父子拼接规则。

## 6. 本讲小结

- 版本信息链路是「**build.rs 检测+兜底 → `env!`/`option_env!` 编译期烧入 → `singleton!` 惰性缓存 → `version()` 返回 `Copy` 值**」四环；Cargo 透传环境变量是「外部优先」成立的前提，`rerun-if-env-changed` 是缓存正确失效的保障。
- `version()` 用 `singleton!`（`LazyLock`）把一次性的 SemVer 解析缓存为全局唯一 `&'static TypstVersion`，末尾 `*` 解引用能按值返回，全靠 `TypstVersion: Copy`。
- `lib.rs` 用 `#[path = "version.rs"] mod version_` 把文件挂成私有模块，刻意带下划线以避开与导出函数 `version` 同名；`pub use self::version_::{TypstVersion, display_commit, version}` 完成选择性导出。
- `TypstVersion` 字段全私有、靠访问器暴露（封装纪律），派生 `Copy` 便于廉价传递；`display_commit` 把 commit 截成前 8 位、`None` 时返回 `"unknown commit"`。
- `DefSite` 用 `(path, key)` 而非行号定位定义位点：key 由 `typst-macros` 在宏展开时按「顶层用名、父子用 `::` 拼接」生成，规避了「宏展开行号失真」与「编辑行号漂移」两个顽疾，服务文档生成与热重载。

## 7. 下一步学习建议

- **横向打通「形状即纪律」的设计主线**：把本讲的 `DefSite`（用 `(path,key)` 强制稳定定位）与 u3-l2 的 `Protected`（用 newtype 强制访问说明理由）、u3-l3 的 `defer()`（用 RAII 强制作用域还原）放在一起复习，体会 typst-utils 如何反复用「数据/类型的形状」编码使用纪律。
- **跟踪一个 `DefSite` 的完整一生**：从 [typst-macros/src/func.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-macros/src/func.rs) 的构造，到 [typst-library/src/foundations/func.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/func.rs) 的存储与 `def_site()` 访问器，再到 [docs/src/reflect.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/docs/src/reflect.rs) 的消费，跨三个 crate 画出 `DefSite` 的数据流。
- **扩展阅读 `singleton!` 的同类**：对比 `LazyLock`（本讲与 u1-l3）、`Deferred`+`OnceCell`（u3-l3）、`HashLock`+`AtomicU128`（u2-l6）三种「全局/惰性缓存」机制，理解它们各自的线程模型与适用场景。
- 至此 typst-utils 学习手册 13 篇全部完成，建议回到 [manifest](manifest.json) 通读一遍各篇小结，把工具箱整体串成一张地图。
