# 构建脚本与增量重编译

## 1. 本讲目标

在前面几讲里，我们已经知道 `i18n!` 宏在**编译期**就把 YAML/JSON/TOML 翻译文件解析、合并、扁平化，最后代码生成进二进制。这就带来了一个很现实的问题：

> 我改了一个 `locales/en.yml`，为什么有时候要手动 `cargo clean` 或者改一下 `lib.rs`，新的翻译才会生效？

本讲就要回答这个问题。读完本讲，你应该能够：

1. 说清楚根 crate 的 `build.rs` 做了什么，以及它为什么是「改 yml 就自动重编译」的关键。
2. 理解 `cargo:rerun-if-changed` 这条 Cargo 指令的工作原理，以及它的作用范围。
3. 掌握 `workdir()` 用 `CARGO_MANIFEST_DIR` 与 `OUT_DIR` 正则切分这两种策略来定位工程根目录。
4. 学会用 `RUST_I18N_DEBUG=1` 在编译期观察 rust-i18n 打印的加载与调试信息。

本讲承接 [u2-l2 编译期加载与解析本地化文件](u2-l2-load-and-parse-locales.md)，那讲讲了「文件是怎么被加载解析的」，本讲补上「文件改动是怎么被 Cargo 感知并触发重编译的」。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 Cargo 的「指纹」与增量编译

Cargo 不会每次都把整个工程从头编一遍，它会为每个编译单元（每个 crate）维护一份「指纹（fingerprint）」。指纹里记录了：源文件内容、依赖的版本、build script 的输出等。只有指纹发生变化，Cargo 才会重新编译对应的 crate。

问题在于：默认情况下，Cargo 只把 **Rust 源文件** 和「build script 显式声明关注的文件」算进指纹。你的 `locales/en.yml` 不是 Rust 源文件，Cargo 默认**不知道它的存在**。所以光改 yml，Cargo 可能认为「指纹没变」，于是不重编、不重新展开 `i18n!` 宏——翻译自然就不会更新。

### 2.2 过程宏不是「运行时函数」，而是「编译期展开器」

`i18n!("locales")` 是一个**过程宏（proc macro）**，它在编译期把 token 流展开成一大段 Rust 代码（见 [u2-l4 generate_code](u2-l4-generate-code.md)）。它读文件、读 `Cargo.toml`，都发生在编译期。所以「翻译没更新」本质上是「宏没有被重新展开」。要让宏重新展开，就要让 Cargo 重新编译「调用 `i18n!` 的那个 crate」。

### 2.3 build script 是 Cargo 提供的「编译前置钩子」

build script（构建脚本）是 Cargo 允许你在「编译某个 crate 之前」运行的一段程序，写在项目根的 `build.rs`。它可以用 `println!("cargo:指令=值")` 这种特殊格式向 Cargo「回话」——告诉 Cargo 诸如「请重新运行我」「当某个文件变化时请重新编译我」之类的事情。`cargo:rerun-if-changed` 就是其中最重要的一条。

> 术语提示：build script 通过 stdout 向 Cargo 通信，所有指令都以 `cargo:` 开头。普通 `println!` 不带这个前缀，Cargo 只当普通日志处理。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [build.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/build.rs) | 根 crate 的构建脚本：扫描 `locales` 目录，对每个文件发出 `cargo:rerun-if-changed`。本讲主角。 |
| [Cargo.toml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/Cargo.toml) | 用 `build = "build.rs"` 声明构建脚本，并在 `[build-dependencies]` 里声明 build.rs 需要的 `globwalk` / `regex`。 |
| [crates/support/src/lib.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs) | 提供 `is_debug()`（读 `RUST_I18N_DEBUG`），以及编译期加载逻辑里多处 `cargo:i18n-*` 调试输出。 |
| [crates/macro/src/lib.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs) | `i18n!` 过程宏入口：调用 `load_locales` 加载文件，并在 `is_debug()` 时打印生成的代码。 |

---

## 4. 核心概念与源码讲解

### 4.1 build.rs：根 crate 的构建脚本

#### 4.1.1 概念说明

`build.rs` 是 rust-i18n **根 crate（即发布的 `rust-i18n` 包本身）** 的构建脚本。它的存在只为了解决一件事：让 Cargo 在「翻译文件被改动后」主动重新编译，从而重新展开 `i18n!`。

它要做的事情非常简单——**扫描工程里的 `locales` 目录，把里面的每一个文件都登记给 Cargo**。登记之后，这些文件就成了 Cargo 指纹的一部分，任何一个文件变动都会触发重编译。

注意它和 `i18n!` 宏里的加载逻辑（[u2-l2](u2-l2-load-and-parse-locales.md) 里的 `try_load_locales`）分工不同：

- `i18n!` / `try_load_locales`：负责**真正读取并解析**翻译文件，把内容代码生成进二进制。
- `build.rs`：只负责**告诉 Cargo「请关注这些文件」**，它自己不解析、不生成任何翻译内容。

#### 4.1.2 核心流程

```text
cargo 决定编译 rust-i18n crate
        │
        ▼
先运行根 crate 的 build.rs（build script 阶段）
        │
        ├── workdir()  →  得到工程根目录
        ├── glob 扫描  {workdir}/**/locales/**/*
        └── 对每个匹配的文件，println!("cargo:rerun-if-changed=<文件>")
        │
        ▼
cargo 把这些文件记进 rust-i18n crate 的指纹
        │
        ▼
（之后）某个 locales/*.yml 改动 → 指纹变化 → 重编译 → 重新展开 i18n!
```

#### 4.1.3 源码精读

首先，根 `Cargo.toml` 用一行声明这个 crate 有构建脚本：

- [Cargo.toml:3](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/Cargo.toml#L3)：`build = "build.rs"`，告诉 Cargo 编译本 crate 之前先运行 `build.rs`。

构建脚本需要扫描目录，所以它依赖 `globwalk`（glob 匹配）和 `regex`（路径切分）。这两个依赖必须单独放在 `[build-dependencies]`，它们只在 build script 阶段存在、不会进入最终二进制：

- [Cargo.toml:67-69](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/Cargo.toml#L67-L69)：`[build-dependencies]` 段声明 `globwalk` 与 `regex`。

> 对比 `[dev-dependencies]`（只在测试/示例/bench 时编译）和 `[build-dependencies]`（只在 build script 时编译）。把工具型依赖放对段落，是控制二进制体积的常用技巧。

整个 `build.rs` 只有约 40 行，结构非常清晰：

- [build.rs:24-39](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/build.rs#L24-L39)：`fn main()` 是构建脚本入口。

```rust
// build.rs（节选）
fn main() {
    let workdir = workdir().unwrap_or("./".to_string());

    let locale_path = format!("{workdir}/**/locales/**/*");
    if let Ok(globs) = globwalk::glob(locale_path) {
        for entry in globs {
            if let Err(e) = entry {
                println!("cargo:i18n-error={}", e);
                continue;
            }

            let entry = entry.unwrap().into_path();
            println!("cargo:rerun-if-changed={}", entry.display());
        }
    }
}
```

几个要点：

1. `workdir()` 决定从哪个目录开始扫描（见 4.3 节）。
2. glob 模式是 `{workdir}/**/locales/**/*`——注意是 `**/locales/**/*`，会匹配 workdir 下**任意层级**的 `locales` 目录里的**任意文件**。这比 `i18n!` 宏用的 `locales_path/**/*.yml` 更宽：它故意覆盖整个工程（包括 workspace 里其它 crate、示例）下的所有 `locales` 目录，让它们的改动也能被感知。
3. `println!("cargo:i18n-error={}", e)`：注意 `cargo:i18n-error=` **不是** Cargo 认识的标准指令（标准指令如 `rerun-if-changed`、`rerun-if-changed=` 等）。`i18n-error` 是 rust-i18n 自定义的「带前缀日志」，Cargo 会忽略它，只是让它出现在构建输出里、便于你用 `i18n-` 关键字搜索排错。
4. 真正起作用的只有最后那一行 `cargo:rerun-if-changed=<文件>`（4.2 节详述）。

#### 4.1.4 代码实践

**实践目标**：确认 `build.rs` 是「属于根 crate」的，并理解 `[build-dependencies]` 的隔离。

1. 打开根 [Cargo.toml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/Cargo.toml)，找到 `build = "build.rs"` 与 `[build-dependencies]`。
2. 对比各子 crate（`crates/macro/Cargo.toml`、`crates/support/Cargo.toml` 等），用搜索确认它们**没有**自己的 `build.rs`（即只有根 crate 有构建脚本）。
3. **需要观察的现象**：只有根 `rust-i18n` crate 声明了 `build = "build.rs"`，子 crate 都没有。
4. **预期结果**：构建脚本只在「编译 rust-i18n 这个包」时运行一次。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `globwalk` 放进普通的 `[dependencies]` 而不是 `[build-dependencies]`，会有什么后果？

**参考答案**：`globwalk` 及其传递依赖会被编进 rust-i18n 的最终二进制/库，无谓增大体积，而且它只在 build script 阶段有用、运行时根本不需要。`[build-dependencies]` 让它只在构建脚本编译期存在，是正确的隔离。

**练习 2**：`build.rs` 自己有没有解析 YAML？它「知道」每个 yml 里写了什么吗？

**参考答案**：完全没有。`build.rs` 只用 `globwalk` 列出文件名，对每个文件路径打印一条 `cargo:rerun-if-changed=`，它从不打开、不读取、不解析文件内容。解析是 `i18n!` 宏里 `try_load_locales` 的职责。

---

### 4.2 cargo:rerun-if-changed 与增量重编译

#### 4.2.1 概念说明

`cargo:rerun-if-changed=<路径>` 是 Cargo 的一条**标准**构建指令。它的语义是：

> 「Cargo，请把这个路径（文件或目录）加入我（这个 build script 所在 crate）的指纹。当它的内容或时间戳变化时，请重新运行我的 build script，并重新编译我这个 crate。」

这正是把 `locales/*.yml` 纳入 Cargo 增量编译感知的官方手段。

需要特别强调一个**作用域**问题：`rerun-if-changed` 影响的是**发出这条指令的 build script 所在的那个 crate** 的指纹。也就是说，根 crate 的 `build.rs` 发出的 `rerun-if-changed`，主要保障的是「编译 rust-i18n 这个包（及其作为示例一起编译的 `examples/*`）时，locales 文件改动会被感知」。

> 在**本仓库内部**（rust-i18n 作为 path 依赖、示例 `examples/app` 作为根 crate 的 `[[example]]` 一起编译）这套机制是自洽且有效的：`examples/app/locales/*.yml` 在 workdir 之下，会被扫描登记，改动会触发 rust-i18n crate 重编译，进而重新展开示例里的 `i18n!`。对于**外部用户**把自己的 locale 文件放在自己 crate 里的情形，rerun-if-changed 的实际生效情况受 Cargo 版本与工程布局影响，**待本地验证**；必要时用户可在自己的 crate 里加一个等价的 build script 来登记自己的 locales 目录。

#### 4.2.2 核心流程

```text
build.rs 扫描到 locales/en.yml
        │
        ▼
println!("cargo:rerun-if-changed=locales/en.yml")
        │
        ▼
Cargo 把 locales/en.yml 记进 rust-i18n 的指纹
        │
        ▼
（某次 build）你改动了 locales/en.yml 的内容
        │
        ▼
指纹对比：发现关注文件变了 → 重新运行 build.rs → 重新编译 rust-i18n
        │
        ▼
重新展开 i18n! → 新译文进二进制
```

#### 4.2.3 源码精读

关键就在 build.rs 主循环的最后一行：

- [build.rs:36](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/build.rs#L36)：`println!("cargo:rerun-if-changed={}", entry.display());`

```rust
// build.rs（关键片段）
let entry = entry.unwrap().into_path();
println!("cargo:rerun-if-changed={}", entry.display());
```

`entry.display()` 把 `PathBuf` 转成可打印的字符串路径。Cargo 解析 build script 的 stdout 时，会识别 `cargo:` 前缀，并把 `rerun-if-changed=` 后面的路径加入指纹。

注意循环结构 `for entry in globs`（[build.rs:29](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/build.rs#L29)）：globe 匹配到的**每一个**文件都会单独发一条指令。这意味着 `locales/` 下有 50 个文件就会发 50 条 `rerun-if-changed`。这是一种「宁可多发、不可漏发」的策略——多登记的文件最多带来偶尔的额外重编译，漏登记则会导致译文不更新的隐蔽 bug。

另外，`if let Err(e) = entry` 分支（[build.rs:30-33](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/build.rs#L30-L33)）处理的是「遍历过程中某个文件访问失败」（比如权限问题），它打印 `cargo:i18n-error=` 后 `continue` 跳过，不会让整个构建脚本崩溃。

#### 4.2.4 代码实践

**实践目标**：亲手验证「改 yml → 自动重编译 → 译文更新」这条链路。

1. 在本仓库根目录执行 `cargo build --example app`（`examples/app` 是根 crate 的示例，`examples/app/locales/*.yml` 在 workdir 之下）。
2. 打开 [examples/app/locales/en.yml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app/locales/en.yml)，把某个已有键的值改一下（例如给 `messages.hello` 的译文加一个感叹号）。
3. **不要** `cargo clean`，直接再次 `cargo build --example app`。
4. **需要观察的现象**：Cargo 输出里出现 `Compiling rust-i18n`（说明指纹变化触发了重编译），而不是只有 `Finished`。
5. **预期结果**：因为 yml 被登记了 `rerun-if-changed`，Cargo 重新编译并重新展开了 `i18n!`，运行 `./target/debug/examples/app` 时能看到改后的译文。
6. **如果现象不出现**：说明 rerun-if-changed 没生效，可手动 `touch examples/app/main.rs` 强制重编来对照——`touch` 源码一定会触发重编译，这能帮你区分「是 yml 没被感知」还是「译文本来就没被使用」。

> 待本地验证：不同 Cargo 版本对「依赖 crate 的 build script rerun-if-changed 是否波及调用方 crate」的行为细节可能不同；上面步骤在本仓库布局下成立，外部工程请以本地实测为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 build.rs 用 `**/locales/**/*` 而不是 `locales/**/*.yml`？

**参考答案**：`**/locales/...` 能匹配 workdir 下任意层级的 `locales` 目录（workspace 里多个 crate、多个示例各自的 locales 都能覆盖）；`*`（不限扩展名）比 `*.yml` 更宽，连 locales 里的 README、json、toml 一起登记。宁可多登记触发偶尔重编，也不能漏登记导致译文不更新。

**练习 2**：`cargo:i18n-error=` 和 `cargo:rerun-if-changed=` 都是 build script 打印的，Cargo 对它们的处理有什么区别？

**参考答案**：`rerun-if-changed=` 是 Cargo 认识的标准指令，会被解析并影响指纹；`i18n-error=` 不是标准指令，Cargo 忽略它，只是让它出现在构建日志里供人搜索排错。

---

### 4.3 workdir()：定位工程根目录的两种策略

#### 4.3.1 概念说明

build script 要扫描 `{workdir}/**/locales/**/*`，那 `workdir` 到底是哪个目录？直观上应该是「当前工程的根目录」。但 build script 运行时，「当前」这个概念并不总是可靠，所以 `workdir()` 写成了**两级回退**策略：

1. **首选**：读环境变量 `CARGO_MANIFEST_DIR`。
2. **回退**：当上面取不到时，从 `OUT_DIR` 反推工程根目录。

`CARGO_MANIFEST_DIR` 是 Cargo 在运行 build script（以及过程宏）时一定会注入的环境变量，指向「正在编译的这个 crate 的 `Cargo.toml` 所在目录」。对根 crate 而言，它就是仓库根目录。

#### 4.3.2 核心流程

```text
workdir()
   │
   ├─ std::env::var("CARGO_MANIFEST_DIR") 成功？  ──Yes──▶ 返回它（仓库根）
   │
   No
   │
   ├─ std::env::var("OUT_DIR") 成功？  ──No──▶ 返回 None（main 会用 "./" 兜底）
   │
   Yes
   │
   ├─ 用正则把  /target/.../build/  这一截切掉
   ├─ 取切出来的前半段（= 工程根）
   └─ 返回 Some(工程根)
```

#### 4.3.3 源码精读

首选分支非常直接：

- [build.rs:2-5](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/build.rs#L2-L5)：

```rust
fn workdir() -> Option<String> {
    if let Ok(cargo_manifest_dir) = std::env::var("CARGO_MANIFEST_DIR") {
        return Some(cargo_manifest_dir);
    }
    ...
```

绝大多数正常 `cargo build` 都会注入 `CARGO_MANIFEST_DIR`，所以会直接在这里返回仓库根目录。

回退分支则用正则从 `OUT_DIR` 反推。`OUT_DIR` 是 Cargo 给 build script 的「输出目录」，形如 `/home/alice/myapp/target/debug/build/rust-i18n-1a2b3c4d/out`。它的规律是：**工程根目录后面跟着 `/target/<profile>/build/<crate>-<hash>/out`**。于是用一个正则把中间的 `/target/.../build/` 切掉，前半段就是工程根：

- [build.rs:7-22](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/build.rs#L7-L22)：

```rust
// build.rs（回退分支节选）
let dest = std::env::var("OUT_DIR");
if dest.is_err() {
    return None;
}
let dest = dest.unwrap();

let seperator = regex::Regex::new(r"(/target/(.+?)/build/)|(\\target\\(.+?)\\build\\)")
    .expect("Invalid regex");
let parts = seperator.split(dest.as_str()).collect::<Vec<_>>();

if parts.len() >= 2 {
    return Some(parts[0].to_string());
}
None
```

用一个具体例子拆解这段正则（以 Unix 风格为例）：

```text
OUT_DIR  =  /home/alice/myapp/target/debug/build/rust-i18n-1a2b3c/out
正则匹配段 =  /target/debug/build/      （.+? 非贪婪匹配到 "debug"）
split 得到 =  [ "/home/alice/myapp" , "rust-i18n-1a2b3c/out" ]
parts[0]   =  /home/alice/myapp        （= 工程根目录）
```

注意几个细节：

1. 正则同时写了 Unix（`/target/.../build/`）和 Windows（`\\target\\...\\build\\`）两种分隔符，用 `|` 连接，保证跨平台。
2. `(.+?)` 是**非贪婪**匹配，遇到第一个 `/build/` 就停下，避免把多层目录误吞。
3. `split` 会把匹配到的部分剔除、返回剩下的片段数组；`parts[0]` 恰好是「目标目录之前的工程根」。
4. `parts.len() >= 2` 是一道安全检查：只有确实切出了「前段 + 后段」时才返回，否则返回 `None`。

当 `workdir()` 返回 `None` 时，`main()` 会用 `"./"` 兜底（[build.rs:25](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/build.rs#L25)），glob 退化为相对当前工作目录扫描，虽然不够精确但不至于崩溃。

> 回退分支何时会被触发？正常 `cargo build` 总会注入 `CARGO_MANIFEST_DIR`，所以 `OUT_DIR` 分支主要服务于一些非标准构建场景（例如脱离 cargo 直接调用、或某些嵌入/交叉编译环境）。它是一道「兜底中的兜底」。

#### 4.3.4 代码实践

**实践目标**：动手验证 `workdir()` 在常规情况下走的是哪条分支。

1. 在 [build.rs:3](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/build.rs#L3) 那一行后面临时加一句 `println!("cargo:warning=workdir={:?}", cargo_manifest_dir);`（注意：在真实的 build script 里调试时，用 `cargo:warning=` 前缀能确保信息显示出来）。
2. 执行 `cargo build --example app`。
3. **需要观察的现象**：构建输出里出现 `warning: workdir="..."`，打印的正是本仓库根目录的绝对路径。
4. **预期结果**：确认 `CARGO_MANIFEST_DIR` 分支被命中，`workdir()` 返回仓库根，因此 glob 能扫到 `examples/app/locales/*.yml`。
5. **验证后请务必删掉这句调试 `println!**，不要把改动留在源码里。

> ⚠️ 这是「阅读型实践」：步骤里要求临时改 `build.rs` 仅为观察，**做完必须还原**，本讲义不允许修改源码。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `workdir()` 不直接用 `std::env::current_dir()`（当前工作目录）？

**参考答案**：build script 运行时的「当前工作目录」由 Cargo 决定且未必是工程根，跨平台、跨场景不稳定。`CARGO_MANIFEST_DIR` 是 Cargo 明确承诺注入、且语义清晰（crate 的 `Cargo.toml` 所在目录），用它最可靠。

**练习 2**：给定 `OUT_DIR = /srv/app/target/release/build/rust-i18n-deadbeef/out`，`parts[0]` 是什么？

**参考答案**：正则匹配掉 `/target/release/build/`，前段为 `/srv/app`，所以 `parts[0] = "/srv/app"`，即工程根目录。

---

### 4.4 is_debug 与 RUST_I18N_DEBUG 编译期调试

#### 4.4.1 概念说明

`build.rs` 让「改 yml 自动重编译」成立，但编译期发生的事情（扫了哪些文件、加载了哪些 locale、最终生成了什么代码）默认是**静默**的——因为这些发生在 `i18n!` 过程宏展开时，不在你自己的代码里，出了问题很难定位。

rust-i18n 提供了一个开关：环境变量 `RUST_I18N_DEBUG`。设为 `1` 时，编译期会打印大量带 `cargo:i18n-` 前缀的调试信息，以及**生成的代码全文**，让你能直接看到 codegen 的产物。

#### 4.4.2 核心流程

```text
编译期（i18n! 宏展开 / try_load_locales 执行）
        │
        ├─ 调用 is_debug()  →  读 RUST_I18N_DEBUG
        │       │
        │       ├─ "1"  → true
        │       └─ 其它/缺失 → false
        │
        ├─ true 时，打印：
        │     cargo:i18n-locale=<glob 模式>
        │     cargo:i18n-load=<逐个加载的文件>
        │     cargo:i18n-error=<加载过程中的错误>
        │     以及生成代码的全文 dump
        └─ false 时，全部静默
```

#### 4.4.3 源码精读

`is_debug()` 是整个调试开关的源头，定义在 support crate：

- [crates/support/src/lib.rs:18-20](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L18-L20)：

```rust
pub fn is_debug() -> bool {
    std::env::var("RUST_I18N_DEBUG").unwrap_or_else(|_| "0".to_string()) == "1"
}
```

要点：读不到该变量时默认 `"0"`，所以默认不调试；只有**精确等于字符串 `"1"`** 才返回 `true`（设成 `true` 或 `2` 都不算）。

这个函数被编译期加载逻辑大量使用。在 `try_load_locales` 里，每个关键步骤都有配套的调试输出：

- [crates/support/src/lib.rs:99-103](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L99-L103)：打印要扫描的 glob 模式 `cargo:i18n-locale=<模式>`。
- [crates/support/src/lib.rs:121-123](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L121-L123)：每加载一个文件打印 `cargo:i18n-load=<文件>`。
- [crates/support/src/lib.rs:75-77](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L75-L77)、[L88-90](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L88-L90)、[L107-109](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L107-L109)：在路径规范化失败、转换失败、目录不存在时打印 `cargo:i18n-error=<原因>`。

```rust
// crates/support/src/lib.rs（节选）
let path_pattern = format!("{locales_path}/**/*.{yml,yaml,json,toml}");
if is_debug() {
    println!("cargo:i18n-locale={}", &path_pattern);
}
...
let entry = entry.unwrap().into_path();
if is_debug() {
    println!("cargo:i18n-load={}", &entry.display());
}
```

> 这些 `cargo:i18n-*` 是 rust-i18n 自定义的「带前缀日志」，**不是** Cargo 标准指令（标准指令只有 `rerun-if-changed` 等）。它们出现在 `i18n!` 宏展开时的 stdout 里，cargo 会捕获并随构建日志输出。统一加 `i18n-` 前缀是为了让你能用 `grep i18n-` 一次性把所有 rust-i18n 调试行过滤出来。

而最重磅的调试输出在 `i18n!` 过程宏入口：当 `is_debug()` 为真，会把 `generate_code` 生成的**整段代码**打印出来——这正是 [u2-l4](u2-l4-generate-code.md) 里讲的 `_RUST_I18N_BACKEND`、`_rust_i18n_try_translate` 等内容的真身：

- [crates/macro/src/lib.rs:257-265](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L257-L265)：

```rust
// crates/macro/src/lib.rs（i18n! 入口节选）
let data = load_locales(&locales_path.display().to_string(), |_| false);
let code = generate_code(data, args);

if is_debug() {
    println!(
        "\n\n-------------- code --------------\n{}\n----------------------------------\n\n",
        code
    );
}
```

此外，`load_metadata`（从 `Cargo.toml` 读配置）在取不到 `CARGO_MANIFEST_DIR` 时，也只在 `is_debug()` 下报错，否则静默跳过：

- [crates/macro/src/lib.rs:127-143](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L127-L143)：`if let Ok(cargo_dir) = std::env::var("CARGO_MANIFEST_DIR") { ... } else if rust_i18n_support::is_debug() { return Err(...) }`。

> 顺带一提：本仓库的 CI（`.github/workflows/ci.yml`）特意把 `RUST_I18N_DEBUG: 0` 写死，确保 CI 日志不被这些调试行刷屏——这也反向印证了这些输出量不小。

#### 4.4.4 代码实践

**实践目标**：用 `RUST_I18N_DEBUG=1` 直观看到编译期发生了什么。

1. 在仓库根执行（注意环境变量要放在命令最前面）：

   ```bash
   RUST_I18N_DEBUG=1 cargo build --example app 2>&1 | tee /tmp/i18n-debug.log
   ```

2. **需要观察的现象**：输出里能搜到三类带前缀的行：
   - `cargo:i18n-locale=.../locales/**/*.{yml,yaml,json,toml}`：要扫描的模式。
   - `cargo:i18n-load=.../examples/app/locales/en.yml`：逐个被加载的文件（应能看到 en/fr 等）。
   - 一段被 `-------------- code --------------` 包裹的生成代码：里面能看到 `SimpleBackend`、`add_translations`、各 locale 的译文键值等（对应 [u2-l4](u2-l4-generate-code.md)）。

3. 可以用下面的命令把生成代码段单独抠出来：

   ```bash
   sed -n '/-------------- code/,/--------------/p' /tmp/i18n-debug.log
   ```

4. **预期结果**：你拿到一份「编译期生成的 Rust 源码」，把它和 [u2-l4](u2-l4-generate-code.md) 描述的 `_RUST_I18N_BACKEND` / `_rust_i18n_try_translate` 结构逐一对照，能极大加深对 codegen 的理解。
5. 改回 `RUST_I18N_DEBUG=0`（或不设置）再编一次，确认这些调试行不再出现——日常构建保持静默。

> 待本地验证：不同 rust-i18n 版本下生成代码的具体格式会有差异；若 `RUST_I18N_DEBUG=1` 完全无输出，请确认环境变量真的传给了 cargo（写在命令前缀，而非写在程序内），并确保确实触发了重编译（必要时先 `touch` 一下调用 `i18n!` 的源文件）。

#### 4.4.5 小练习与答案

**练习 1**：把 `RUST_I18N_DEBUG` 设成 `true` 或 `2`，`is_debug()` 会返回 `true` 吗？

**参考答案**：不会。`is_debug()` 只在变量值**精确等于字符串 `"1"`** 时返回 `true`；`"true"`、`"2"` 都会被判为 `false`。这是字符串比较，不是布尔解析。

**练习 2**：`cargo:i18n-load=` 这类输出，和 build.rs 里 `cargo:rerun-if-changed=` 都用了 `cargo:` 前缀，二者本质区别是什么？

**参考答案**：`rerun-if-changed=` 是 Cargo 认识的标准指令，会被解析执行；`i18n-load=`、`i18n-locale=`、`i18n-error=` 是 rust-i18n 自定义的标签，Cargo 不识别、只是让它们混在构建日志里，供人用 `i18n-` 关键字检索。两者一个影响构建行为，一个仅供调试观察。

---

## 5. 综合实践

把本讲的四个模块串起来，做一次「端到端诊断」。

**任务**：假设你改了 `examples/app/locales/en.yml` 却发现译文没更新，请用本讲学到的全部手段定位问题。

参考步骤：

1. **先确认是否重编译**：再次 `cargo build --example app`，观察是否出现 `Compiling rust-i18n`。
   - 如果**没出现** `Compiling`（直接 `Finished`）：说明 `rerun-if-changed` 没让 Cargo 感知到 yml 改动。对照 [4.3](#43-workdir-定位工程根目录的两种策略) 检查 `workdir()` 扫描范围，确认该 yml 是否落在 `{workdir}/**/locales/**/*` 之下。
   - 如果**出现了** `Compiling` 但译文仍没变：进入第 2 步。
2. **打开编译期调试**：`RUST_I18N_DEBUG=1 cargo build --example app 2>&1 | tee debug.log`，按 [4.4](#44-is_debug-与-rust_i18n_debug-编译期调试) 的方法检查：
   - `cargo:i18n-load=` 里是否真的加载了你改的那个 yml？
   - 抠出的生成代码段里，对应键的值是不是你改后的？
3. **得出结论并记录**：把「问题出在 rerun-if-changed 未生效」还是「问题出在 codegen 内容」写进学习笔记，并说明你分别依据哪条调试行做的判断。

> 这是一个「源码阅读 + 现场诊断」型综合实践，不需要你写新功能，而是训练你用 build.rs 机制和 `RUST_I18N_DEBUG` 把「编译期黑盒」打开来看。

## 6. 本讲小结

- 根 crate 的 `build.rs` 是「改 yml 自动重编译」的关键，它**只负责登记文件**，从不解析翻译内容；解析由 `i18n!` 里的 `try_load_locales` 完成。
- `cargo:rerun-if-changed=<文件>` 是 Cargo 标准指令，把 locale 文件纳入 build script 所在 crate 的指纹；它影响的是「发出指令的那个 crate」的编译决策。
- `workdir()` 用两级策略定位工程根：首选 `CARGO_MANIFEST_DIR`，取不到时用正则把 `OUT_DIR` 里的 `/target/.../build/` 一段切掉来反推。
- `is_debug()` 读取 `RUST_I18N_DEBUG`（精确等于 `"1"` 才生效），控制编译期所有 `cargo:i18n-*` 调试行与生成代码 dump 的开关。
- `cargo:i18n-*` 系列是 rust-i18n **自定义的日志前缀**，不是 Cargo 标准指令，仅供人检索排错；标准指令只有 `rerun-if-changed` 等。
- `[build-dependencies]` 把 `globwalk`/`regex` 隔离在 build script 阶段，避免它们进入最终二进制。

## 7. 下一步学习建议

- 想了解「编译期到底生成了什么代码」的全貌，回到 [u2-l4 generate_code 生成的运行时代码](u2-l4-generate-code.md)，并用本讲的 `RUST_I18N_DEBUG=1` 实际对照那讲里描述的 `_RUST_I18N_BACKEND` 等结构。
- 想了解 feature flags 如何在「编译期加载」与「运行时加载」之间切换（影响 build.rs 与 codegen 是否参与），继续学 [u5-l3 Feature flags 与可选依赖](u5-l3-feature-flags.md)。
- 想看运行时如何用 `t!` 在生成出来的静态后端里查表，进入 [u3-l1 t! 宏的完整调用链](u3-l1-t-macro-call-chain.md)。
