# 过程宏 crate 的骨架：proc-macro = true 与 TokenStream

## 1. 本讲目标

上一讲我们认识了 ui_macros 的定位：一个只暴露两个宏的小型过程宏 crate。本讲深入它的"骨架"，学完后你应该能：

1. 说出 Rust 过程宏的三种形式（函数式、派生、属性），并指出 ui_macros 用了哪两种。
2. 解释 `Cargo.toml` 中 `[lib] path = "src/ui_macros.rs"` 与 `proc-macro = true` 各自的含义与后果。
3. 理解 `TokenStream` 是什么：过程宏的输入和输出都是它，宏本质上是一个"token 进、token 出"的编译期函数。
4. 独立创建一个最小可运行的过程宏 crate，并在另一个 crate 里使用它。

## 2. 前置知识

- **声明宏 vs 过程宏**：你大概率用过 `vec![]`、`println!` 这类用 `macro_rules!` 写的"声明宏"。过程宏（procedural macro）是另一种：它是一个真正的 Rust 函数，由编译器在你程序编译期间调用，输入和输出都是 token。声明宏擅长简单的模式匹配拼接；过程宏能做完整的语法分析（比如解析 `[(1, 2, 4), 24]` 这样的结构），ui_macros 里的两个宏都属于过程宏。
- **Token（词法单元）**：编译器读源代码的第一步是把字符流切成 token：标识符（`foo`）、字面量（`24`、`"hi"`）、标点（`+`、`,`）、以及带括号的分组。`TokenStream` 就是一串这样的 token。"注释和空白在 token 化之后基本不保留"（文档注释例外，它会变成 `#[doc = "..."]` 属性 token）——这一点后面会用到。
- **crate 类型**：普通库 crate 编译成 `.rlib` 供别人链接；而 `proc-macro = true` 的 crate 编译成一个由 rustc 在**编译你的下游代码时**加载执行的动态/静态插件。它里面的代码"运行在你使用者的编译期"。
- **承接上一讲**：上一讲已说明 ui_macros 的正式依赖只有 `syn` 与 `quote`，`component` 与 `ui` 只是 doctest 用的 dev 依赖。本讲解释这些配置在"过程宏 crate"这个特殊语境下意味着什么。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [Cargo.toml](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/Cargo.toml#L1-L22) | 声明这是一个 proc-macro crate，`[lib] path` 指向非默认的库根文件，并声明 syn/quote 依赖 |
| [src/ui_macros.rs](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/ui_macros.rs#L1-L43) | crate 的库根：声明两个对外的宏并转发到内部模块，唯一 import 的是 `proc_macro::TokenStream` |
| [src/dynamic_spacing.rs](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L1-L60) | `derive_dynamic_spacing!` 的真实实现（本讲只看它的入口签名，精读留给单元二） |
| [src/derive_register_component.rs](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/derive_register_component.rs#L1-L29) | `RegisterComponent` 派生宏的真实实现（本讲只引用它的一处类型转换细节，精读留给单元四） |

本讲重点是前两个文件；后两个只作为"转发目标"出现，帮助你确认入口文件的转发链路。

## 4. 核心概念与源码讲解

### 4.1 proc-macro crate 声明

#### 4.1.1 概念说明

一个 Rust crate 编译成什么产物，由 `Cargo.toml` 决定。对过程宏 crate 来说有两个关键声明：

1. **`proc-macro = true`**：告诉 Cargo/rustc "这个 crate 不是一个普通库，而是一组编译器插件"。它带来几条硬性规则：
   - crate 里**只能**导出过程宏（被 `#[proc_macro]`、`#[proc_macro_derive]`、`#[proc_macro_attribute]` 标注的函数），不能导出普通的 struct、fn、trait。
   - 它的依赖（这里是 `syn`、`quote`）会在**下游 crate 编译时**被加载运行，因此它们实际上是下游的"编译期依赖"，而不是运行时依赖。
   - 特殊的 `proc_macro` 标准库（提供 `TokenStream` 类型）只在 proc-macro crate 里可用。
2. **`[lib] path = "src/ui_macros.rs"`**：把库根从默认的 `src/lib.rs` 改成与 crate 同名的 `src/ui_macros.rs`。这是 Zed 仓库自己的编码规范（见仓库根 `CLAUDE.md`：创建新 crate 时优先用 `[lib] path` 指向描述性命名的文件），ui_macros 遵循了这条规范。

#### 4.1.2 核心流程

从"写下一个宏调用"到"宏代码执行"的链路：

```text
你在 ui crate 里写下 derive_dynamic_spacing![24, (1, 2, 4)]
        │
        ▼
rustc 编译 ui crate 时发现该调用来自 ui_macros（编译期依赖）
        │
        ▼
rustc 先把 ui_macros 连同它的 syn/quote 依赖编译成一个插件
        │
        ▼
把调用点括号里的 token 打包成 proc_macro::TokenStream
        │
        ▼
调用 ui_macros::derive_dynamic_spacing(input) —— 一段普通的 Rust 代码在"编译 ui"的过程中运行
        │
        ▼
返回的 TokenStream 被拼接回 ui 的源码位置，继续正常编译
```

也就是说：宏 crate 的代码运行时机 = **使用者的编译期**；运行它的进程 = rustc 编译进程。

#### 4.1.3 源码精读

先看 `Cargo.toml` 中与本模块直接相关的三段：

- [Cargo.toml:L11-L13](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/Cargo.toml#L11-L13)：`[lib]` 段里 `path = "src/ui_macros.rs"` 指定库根文件，`proc-macro = true` 把整个 crate 声明成过程宏插件——这两行是"成为过程宏 crate"的全部条件。
- [Cargo.toml:L15-L17](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/Cargo.toml#L15-L17)：正式依赖只有 `quote` 与 `syn`（版本由 workspace 根 `Cargo.toml` 统一继承，上一讲已讲过；根配置里 syn 带了 `full`、`extra-traits`、`visit-mut` feature，并对 syn/quote/proc-macro2 设了 `opt-level = 3` 以加速宏本身的编译，感兴趣可自行查阅仓库根 `Cargo.toml`）。
- [Cargo.toml:L19-L21](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/Cargo.toml#L19-L21)：`component` 与 `ui` 是 dev-dependencies，只在编译 doctest/测试时生效——上一讲说过，这是为 [src/ui_macros.rs](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/ui_macros.rs#L21-L36) 里的那个 doctest 服务的。注意方向性：ui_macros 在 dev 时依赖 ui，而 ui 在正式编译时依赖 ui_macros——两者并不冲突，因为作用域不同。

再看库根文件开头的模块声明：

- [src/ui_macros.rs:L1-L4](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/ui_macros.rs#L1-L4)：声明两个私有子模块，并 `use proc_macro::TokenStream`。`proc_macro` 是编译器内置的标准库，**无需写入 Cargo.toml** 任何依赖即可使用，但只在 proc-macro crate 中可见。整个文件只有这一个 use，可见入口层做的事情极薄。

#### 4.1.4 代码实践

1. **实践目标**：用 Cargo 自己的命令验证 ui_macros 确实以"过程宏插件"的身份参与编译，而不是普通库。
2. **操作步骤**：
   - 在 Zed 仓库根目录运行 `cargo tree -p ui_macros --depth 1`，观察它的依赖里只有 syn/quote（以及 dev 的 component/ui）。
   - 再运行 `cargo tree -p ui -e proc-macro --depth 1`（`-e proc-macro` 表示把"过程宏依赖"这种边也显示出来）。
3. **需要观察的现象**：第二条命令的输出中，`ui_macros` 应该以一种特殊的边（proc-macro 依赖）挂在 `ui` 下面，而不是普通的 normal 依赖。
4. **预期结果**：你能指出 ui 对 ui_macros 的依赖属于"编译 ui 时要先构建并运行"的那一类。具体输出格式随 Cargo 版本略有差异，待本地验证。
5. 若网络或磁盘受限无法运行，直接对照 [Cargo.toml:L13](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/Cargo.toml#L13) 也能得到同样结论。

#### 4.1.5 小练习与答案

**练习 1**：如果删掉 `Cargo.toml` 里的 `proc-macro = true`（只是假设），`src/ui_macros.rs` 里的 `#[proc_macro]` 会发生什么？
**答案**：`proc_macro` 标准库在非 proc-macro crate 中不可用，`use proc_macro::TokenStream` 与 `#[proc_macro]` 属性都会直接编译失败。换句话说，`proc-macro = true` 是那两个宏声明合法的前提。

**练习 2**：为什么 ui_macros 的 `syn`/`quote` 不会成为 Zed 编辑器最终二进制的运行时负担？
**答案**：过程宏 crate 的依赖在"下游 crate（如 ui）编译期"被加载运行，用来生成代码；生成完毕后，进入最终二进制的是生成出来的代码，而不是 syn/quote 本身。

**练习 3**：`[lib] path = "src/ui_macros.rs"` 与默认的 `src/lib.rs` 相比有什么实际差别？
**答案**：功能上没有差别，只是库根文件名不同。这是 Zed 仓库的编码规范（crate 根文件用描述性命名），好处是打开编辑器时一眼能从文件名认出 crate。

### 4.2 TokenStream：宏的输入与输出

#### 4.2.1 概念说明

过程宏的签名永远是同一个形状：

```rust
fn 宏名(input: TokenStream) -> TokenStream
```

`proc_macro::TokenStream` 是编译器交给你的"token 序列"。三点直觉：

1. **它不是字符串**。你的宏拿到的不是源代码文本，而是编译器已经切好的 token 树：标识符、字面量、标点、以及递归分组的括号。空白、换行、普通注释都已丢失。
2. **它不是类型化数据**。`24, (1, 2, 4)` 这样的输入对编译器来说只是"一堆 token"，语义要靠你自己解析——这正是 `syn` 存在的意义：把 token 解析成类型化的语法树。反过来，`quote` 负责把你在 Rust 里拼好的模板变成 token。
3. **返回值会被原样拼回源码**。rustc 拿到你返回的 token 流，放到调用点继续编译。返回的代码和手写的代码地位完全相同，同样要做名称解析和类型检查。

一个容易混淆的细节：`syn`/`quote` 内部操作的是 `proc_macro2::TokenStream`（`proc-macro2` 是 `proc_macro` 的跨版本封装，让宏代码可以被普通工具处理）。在 ui_macros 里能看到两种流的边界转换：入口函数收到 `proc_macro::TokenStream`，`quote!` 宏产出 `proc_macro2` 的流，最后再转回去。真实转换点在 [src/derive_register_component.rs:L28](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/derive_register_component.rs#L28)：`expanded.into()` 把 quote 的产物转回 `proc_macro::TokenStream` 返回给编译器。本讲只需记住"存在这两种流、边界在哪"，细节在单元二精读。

#### 4.2.2 核心流程

```text
调用点源码                宏函数内部                     使用者 crate 继续编译
────────────            ────────────────────           ─────────────────────
derive_dynamic_spacing!  收到 TokenStream:              拿到返回的 TokenStream
[24, (1, 2, 4)]    ──▶   [ 24 , ( 2 , 4 ) ... ]   ──▶   （一组枚举定义、方法等）
   （字符）               （token，非文本）               （当普通代码编译）
```

伪代码概括 ui_macros 的两个入口（真实代码见 4.3.3）：

```text
函数式宏:  括号里的 token        ──▶ dynamic_spacing::derive_spacing      ──▶ 生成的枚举代码
派生宏:    整个 struct/enum 的 token ──▶ derive_register_component::...    ──▶ 追加在类型后面的注册代码
```

#### 4.2.3 源码精读

- [src/ui_macros.rs:L4](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/ui_macros.rs#L4)：`use proc_macro::TokenStream;`——整个入口文件唯一的导入，因为两个入口函数的签名只需要它。
- [src/ui_macros.rs:L7-L10](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/ui_macros.rs#L7-L10)：`derive_dynamic_spacing` 的签名 `pub fn derive_dynamic_spacing(input: TokenStream) -> TokenStream`，函数体只有一行：把 input 原样交给 [src/dynamic_spacing.rs:L49-L51](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L49-L51) 的 `derive_spacing`。注意转发时**没有做任何转换**——内部模块继续使用 `proc_macro::TokenStream`（见 [src/dynamic_spacing.rs:L1](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L1)），到需要语法分析时才用 `parse_macro_input!` 转换。
- [src/derive_register_component.rs:L28](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/derive_register_component.rs#L28)：`expanded.into()`——`quote!` 生成的是 `proc_macro2::TokenStream`，这里通过 `.into()` 转回 `proc_macro::TokenStream` 才能满足返回类型。这是"两种流"边界的一个真实锚点。

#### 4.2.4 代码实践（本讲主实践·上）

1. **实践目标**：亲手写一个"token 进、token 出"的恒等宏 `identity!`，证明过程宏只是普通函数 + token 流。
2. **操作步骤**（在 Zed workspace 之外任意目录，例如 `~/scratch/proc-macro-lab`）：

   建立如下三个 crate 的小 workspace（过程宏 crate 无法使用自己导出的宏，所以必须"宏 crate + 使用者 crate"两个）：

   ```text
   proc-macro-lab/
   ├── Cargo.toml
   ├── identity_macros/
   │   ├── Cargo.toml
   │   └── src/identity_macros.rs
   └── usage/
       ├── Cargo.toml
       └── src/main.rs
   ```

   根 `Cargo.toml`（示例代码）：

   ```toml
   [workspace]
   members = ["identity_macros", "usage"]
   resolver = "2"
   ```

   `identity_macros/Cargo.toml`（示例代码，注意它和 ui_macros 的写法一一对应）：

   ```toml
   [package]
   name = "identity_macros"
   version = "0.1.0"
   edition = "2021"

   [lib]
   path = "src/identity_macros.rs"
   proc-macro = true
   ```

   `identity_macros/src/identity_macros.rs`（示例代码）：

   ```rust
   use proc_macro::TokenStream;

   /// 恒等宏：把输入的 token 原样返回。
   #[proc_macro]
   pub fn identity(input: TokenStream) -> TokenStream {
       input
   }
   ```

   `usage/Cargo.toml`（示例代码）：

   ```toml
   [package]
   name = "usage"
   version = "0.1.0"
   edition = "2021"

   [dependencies]
   identity_macros = { path = "../identity_macros" }
   ```

   `usage/src/main.rs`（示例代码）：

   ```rust
   use identity_macros::identity;

   fn main() {
       // identity! 把括号里的 token 原样吐回来，这行等价于 let x = 1 + 2;
       let x = identity!(1 + 2);
       println!("x = {x}");
   }
   ```

   然后在 `proc-macro-lab` 目录运行 `cargo run -p usage`。
3. **需要观察的现象**：编译先构建 `identity_macros`（像 ui_macros 一样作为插件），再编译 `usage`；运行输出 `x = 3`。
4. **预期结果**：程序正常编译并打印 `x = 3`，说明 `identity!(1 + 2)` 展开后就是 `1 + 2`。若安装了 `cargo-expand`（`cargo install cargo-expand`，需要 nightly 工具链），可用 `cargo expand -p usage` 直接看到展开后的源码；没有安装也不影响本实践结论。
5. 本实践不修改 Zed 源码；所有命令在 scratch 目录中运行。

#### 4.2.5 小练习与答案

**练习 1**：`identity!( 1 + 2 )` 和 `identity!(1+2)` 的返回结果有区别吗？
**答案**：语义上没有。token 流里只有 `1`、`+`、`2` 三个 token，空白数量不进入 token 流，两种写法产生的 token 相同（`proc_macro` 内部保留一点间距提示用于打印美化，但不影响语义）。

**练习 2**：为什么宏拿不到调用点外面的代码？比如 `identity!` 能读到它所在函数的参数吗？
**答案**：不能。编译器只把"调用点括号内"（函数式宏）或"被标注的 item"（派生宏）的 token 交给宏，宏对周围代码一无所知。这也是为什么 ui_macros 的 `derive_dynamic_spacing` 需要调用方把所有信息（如 `::theme::` 路径的存在）当作环境约定。

**练习 3**：`quote!` 生成的是哪种 `TokenStream`？ui_macros 在哪里把它转回编译器要的那种？
**答案**：`proc_macro2::TokenStream`；在 [src/derive_register_component.rs:L28](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/derive_register_component.rs#L28) 通过 `.into()` 转换并返回。

### 4.3 函数式宏与派生宏的区别

#### 4.3.1 概念说明

Rust 过程宏共三种形式，声明它们的属性和调用方式各不相同：

| 形式 | 声明属性 | 调用方式 | 输入 | 输出去向 | ui_macros 中的例子 |
| --- | --- | --- | --- | --- | --- |
| 函数式 | `#[proc_macro]` | `name!(...)` | 括号内的 token | **替换**调用点 | `derive_dynamic_spacing!` |
| 派生 | `#[proc_macro_derive(Name)]` | `#[derive(Name)]` | 被标注的整个 item（struct/enum 定义） | **追加**在原 item 之后（原 item 保留） | `#[derive(RegisterComponent)]` |
| 属性 | `#[proc_macro_attribute]` | `#[name(args)]` 标注在某 item 上 | 属性参数 + 被标注的 item | **替换**被标注的 item | 无（Zed 仓库里 `crates/gpui_macros`、`crates/settings_macros` 等有使用，可自行搜索 `#[proc_macro_attribute]` 验证） |

两个关键差异要记住：

1. **输入不同**：函数式宏只拿到括号里的内容（`derive_dynamic_spacing![24, (1, 2, 4)]` 拿到的是 `24 , (1, 2, 4)`）；派生宏拿到的是整个类型定义（`struct Tooltip { ... }` 的全部 token），所以派生宏"认识"被标注的类型。
2. **输出去向不同**：函数式宏的输出顶替调用点，所以调用点必须恰好放得下生成的内容；派生宏的输出拼在原类型后面，原类型原样保留，宏只负责"额外添加"实现——这就是 `RegisterComponent` 能在不动组件定义的情况下追加注册代码的原因。

另外注意派生宏的**名字写在属性里**：`#[proc_macro_derive(RegisterComponent)]` 中的 `RegisterComponent` 才是使用者在 `#[derive(RegisterComponent)]` 里写的名字，它与函数名 `derive_register_component` 可以不同。

#### 4.3.2 核心流程

两个入口的调用链完全对称，都是"声明 + 一行转发"：

```text
使用者（ui crate）                              ui_macros 内部
─────────────────                              ──────────────
spacing.rs: derive_dynamic_spacing![...]  ──▶  ui_macros.rs::derive_dynamic_spacing
                                                    │ 一行转发
                                                    ▼
                                              dynamic_spacing::derive_spacing   ← 真正实现

tooltip.rs: #[derive(RegisterComponent)]  ──▶  ui_macros.rs::derive_register_component
              struct Tooltip { ... }              │ 一行转发
                                                    ▼
                                              derive_register_component::
                                              derive_register_component        ← 真正实现
```

这种"薄入口"分层让 [src/ui_macros.rs](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/ui_macros.rs#L1-L43) 保持极短：对外 API、文档（包括 doctest）集中在入口，解析与生成逻辑按宏分文件存放。

#### 4.3.3 源码精读

- [src/ui_macros.rs:L6-L10](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros.rs#L6-L10)：函数式宏的完整声明。`///` 文档注释（第 6 行）会成为这个宏在 rustdoc 里的说明；`#[proc_macro]`（第 7 行）声明函数式宏；函数体第 9 行 `dynamic_spacing::derive_spacing(input)` 把 token 流转发给实现模块。
- [src/ui_macros.rs:L40-L43](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/ui_macros.rs#L40-L43)：派生宏的完整声明。`#[proc_macro_derive(RegisterComponent)]` 决定了使用者写的是 `#[derive(RegisterComponent)]`；函数体同样只转发到 `derive_register_component::derive_register_component`。
- [src/ui_macros.rs:L12-L39](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/ui_macros.rs#L12-L39)：`RegisterComponent` 的文档注释。其中第 21–36 行的 ` ``` ` 代码块是一个 **doctest**：`cargo test -p ui_macros` 会把它当真实程序编译运行（这正是 Cargo.toml 需要 `ui`/`component` dev 依赖的原因，也是这一宏唯一来自仓库的自动化验证，单元五会专门讨论）。文档写在入口而非实现文件，因为 rustdoc 以宏声明处为准。
- 对照差异的输入侧证据：函数式宏的输入解析（`24`、`(1, 2, 4)`）从 [src/dynamic_spacing.rs:L49-L51](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L49-L51) 开始，用 `parse_macro_input!(input as DynamicSpacingInput)` 解析"括号里的列表"；派生宏的输入解析在 [src/derive_register_component.rs:L5-L8](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/derive_register_component.rs#L5-L8)，用 `parse_macro_input!(input as DeriveInput)` 解析"整个类型定义"——两者拿到的 token 内容截然不同，正好印证 4.3.1 的对比表。

#### 4.3.4 代码实践（本讲主实践·下：通过报错观察展开时机）

1. **实践目标**：用故意写错的调用，亲眼确认"宏输出会被当作普通代码继续编译"，以及函数式宏的输出必须放得进调用点的语法位置。
2. **操作步骤**（接着 4.2.4 的 scratch 工程，改 `usage/src/main.rs` 后 `cargo check -p usage`，以下均为示例代码）：

   ```rust
   use identity_macros::identity;

   fn main() {
       // 实验 A：输入不是合法表达式
       let x = identity!(1 +);

       // 实验 B：输出类型与标注不符
       let y: i32 = identity!("hello");

       // 实验 C：在 item 位置使用（顶层，不在 fn 里）
       // identity!(fn generated_fn() {});
       // generated_fn();
   }
   ```

   逐个取消注释/注释观察，每次只留一个实验。
3. **需要观察的现象**：
   - 实验 A：报错应该指向你的调用处（`expected expression, found \`+\`` 一类），说明错误发生在"宏返回的 token 继续编译"阶段。
   - 实验 B：报错是普通的类型不匹配错误（`i32` vs `&str`），说明返回的 token 按普通代码做类型检查。
   - 实验 C：顶层位置展开成 `fn generated_fn() {}` 应能通过编译并可调用；而若把同一调用放进 `main` 函数内的表达式位置则会报语法错误——同一份输出，放的位置不同结果不同。
4. **预期结果**：三个实验的报错/通过情况如上。报错的精确措辞随编译器版本变化，以本地输出为准（待本地验证）。
5. 思考题（不必运行）：`#[derive(RegisterComponent)]` 为什么不需要担心"输出放错位置"？——因为派生宏的输出永远追加在类型定义之后，位置由编译器固定。

#### 4.3.5 小练习与答案

**练习 1**：`#[proc_macro_derive(RegisterComponent)]` 里的 `RegisterComponent` 和函数名 `derive_register_component` 是什么关系？
**答案**：两者独立。使用者写的是 `#[derive(RegisterComponent)]`，即属性括号里的名字；函数名只是实现载体，改成别的名字不影响使用方。ui_macros 让函数名带上 `derive_` 前缀是可读性约定。

**练习 2**：如果给 `struct Tooltip` 加了 `#[derive(RegisterComponent)]`，编译后 `struct Tooltip` 的定义还存在吗？
**答案**：存在。派生宏的输出追加在原 item 之后，原类型定义原样保留；宏生成的注册代码（单元四会精读）是"额外添加"的。而函数式宏的输出是替换调用点，这是两种形式最本质的行为差异。

**练习 3**：ui_macros 里为什么没有第三种形式（属性宏）？举一个 Zed 仓库里真实使用属性宏的 crate。
**答案**：因为这个 crate 的两个需求都不需要"改写被标注的 item"：间距枚举是凭空生成（函数式最合适），组件注册是纯追加（派生最合适）。Zed 仓库里 `crates/gpui_macros` 与 `crates/settings_macros` 等 crate 声明了 `#[proc_macro_attribute]` 宏（可用全文搜索验证）。

## 5. 综合实践

**任务：把 scratch 工程升级为"一个 crate、两种形式"的迷你 ui_macros。**

在 4.2.4 的 `identity_macros` crate 里再加一个派生宏 `DumpName`（示例代码），并让 `usage` 同时使用两个宏：

1. 给 `identity_macros/Cargo.toml` 的 `[dependencies]` 加上 `syn = "2"` 与 `quote = "1"`（和 ui_macros 一样）。
2. 新建 `identity_macros/src/dump_name.rs`（示例代码）：

   ```rust
   use proc_macro::TokenStream;
   use quote::quote;
   use syn::DeriveInput;

   pub fn derive_dump_name(input: TokenStream) -> TokenStream {
       let input = syn::parse_macro_input!(input as DeriveInput);
       let name = input.ident;
       quote! {
           impl #name {
               pub fn dump_name() -> &'static str {
                   stringify!(#name)
               }
           }
       }
       .into()
   }
   ```

3. 在 `identity_macros.rs` 里仿照 [src/ui_macros.rs:L1-L4](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/ui_macros.rs#L1-L4) 加 `mod dump_name;`，并仿照 [src/ui_macros.rs:L40-L43](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/ui_macros.rs#L40-L43) 写：

   ```rust
   /// 为类型生成 dump_name() 方法。
   #[proc_macro_derive(DumpName)]
   pub fn derive_dump_name(input: TokenStream) -> TokenStream {
       dump_name::derive_dump_name(input)
   }
   ```

4. 在 `usage/src/main.rs` 里定义 `#[derive(DumpName)] struct Tooltip;`，在 `main` 中调用 `Tooltip::dump_name()` 和 `identity!(1 + 2)` 并打印。
5. **验收标准**：`cargo run -p usage` 同时打印表达式结果和 `Tooltip`——函数式与派生两种形式在同一个 crate 内并存，入口声明、转发、`proc-macro = true`、TokenStream 输入输出全部走通，结构与你读过的 ui_macros 完全同构。
6. 进阶（可选）：给 `derive_dump_name` 加上 `///` 文档注释和一个 doctest，运行 `cargo test -p identity_macros` 验证 doctest 生效——这正是 ui_macros 在 [src/ui_macros.rs:L12-L39](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/ui_macros.rs#L12-L39) 采用的做法。

## 6. 本讲小结

- `Cargo.toml` 中 `proc-macro = true`（[Cargo.toml:L13](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/Cargo.toml#L13)）把 crate 变成编译器插件：只能导出宏，依赖（syn/quote）在下游编译期运行。
- 过程宏 = 签名为 `TokenStream -> TokenStream` 的普通函数；token 不是文本，返回的 token 会被当普通代码继续做名称解析和类型检查。
- `#[proc_macro]`（函数式）与 `#[proc_macro_derive(Name)]`（派生）的核心差异：输入（括号内容 vs 整个 item）与输出去向（替换调用点 vs 追加在原 item 后）；第三种形式属性宏 `#[proc_macro_attribute]` 在 ui_macros 中没有使用。
- [src/ui_macros.rs](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/ui_macros.rs#L1-L43) 是"薄入口"模式：声明 + 文档 + 一行转发，实现放在 `dynamic_spacing.rs` 与 `derive_register_component.rs`。
- 两种 `TokenStream`（`proc_macro` 与 `proc_macro2`）通过 `parse_macro_input!` / `.into()` 在边界互转。

## 7. 下一步学习建议

下一讲（u1-l3）会拉远镜头，画出两个宏与它们的消费者（ui、component、theme）之间的调用关系图，为进入单元二做准备。此后单元二第一讲（u2-l1）将从 [src/dynamic_spacing.rs:L23-L46](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L23-L46) 的 `Parse` 实现开始，把本讲的"token 进、token 出"具体化为 syn 的结构化解析——建议届时带着一个问题去读：`24, (1, 2, 4)` 这串 token 是怎么一步步变成 `Single(LitInt)` 与 `Tuple(LitInt, LitInt, LitInt)` 的。
