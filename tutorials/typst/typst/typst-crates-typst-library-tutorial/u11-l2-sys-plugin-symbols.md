# sys 模块、WASM plugin 与符号系统

## 1. 本讲目标

本讲聚焦 `typst-library` 里三组「与外界打交道」的设施：系统模块 `sys`、WebAssembly 插件加载器 `plugin`、以及符号系统（外部 `codex` 符号表 + `Symbol` 运行时类型）。学完后你应当能够：

- 说清 `sys` 模块对外暴露了什么（`version`、`inputs`），以及它是怎么在标准库装配阶段被注入的——并理解它为何是「装配期定死、运行期只读」。
- 理解 `plugin()` 如何把一段 WebAssembly 字节加载成一个 Typst 模块，并讲清插件函数为何必须是「纯函数」——以及这条约束在源码里到底由哪些机制强制（这是本讲的重点，也是代码实践任务之一）。
- 讲清 `codex::ROOT` 与 `codex::SYM` 两张静态符号表如何分别流入全局作用域（`sym`/`emoji`）和数学作用域（裸符号），并解释 `codex::SYM` 如何变成 `math.sym` 命名空间（代码实践任务之二）。
- 掌握 `Symbol` 运行时类型的三种内部形态、修饰符（modifier）「最佳匹配」机制，以及用户自定义符号的构造校验过程。

本讲承接 **u3-l4（`func` 宏、NativeFunc 与 Args）**：`plugin`、`plugin.transition` 都是用 `#[func]`/`#[scope]` 定义的原生函数；同时承接 **u2-l3（cast、Type、Module 与 Scope）** 的 `Scope`/`Module` 查找与回退机制——后者正是「为什么数学模式里 `sym.alpha` 也能用」的关键。

## 2. 前置知识

- **模块与作用域**：Typst 标准库在装配期被组织成一棵 `Scope`（名字→`Binding` 的有序映射）树，再包成 `Module`。u2-l3 已讲过 `Scopes` 的查找会按「当前层 → 外层 → 全局 → 特判 `std`」回退，本讲会反复用到这一点。
- **`#[func]` 与 `#[scope]`**：u3-l4 讲过，`#[func]` 把一个 Rust `fn` 变成 Typst 原生函数；`#[func(scope)]` 让函数拥有子作用域（如 `plugin.transition`）。本讲把 `plugin` 当作真实案例再走一遍。
- **comemo 记忆化**：u5-l1/u9-l3 提到，`#[comemo::track]` 与 `#[comemo::memoize]` 是 Typst 增量编译的根基——同样的输入应得到同样的输出。这条性质正是插件「纯函数约束」的源头。
- **WebAssembly（WASM）**：一种可移植的字节码虚拟机格式。本讲不需要你会写 WASM，但要理解它是一种「沙箱化、跨语言」的可执行载体。Typst 用 `wasmi`（一个纯 Rust 的 WASM 解释器）来跑插件。
- **Unicode 与 grapheme**：符号系统的核心是 Unicode 字符。一个「符号变体」的值必须恰好是一个 grapheme cluster（用户感知的一个字符），这点在自定义符号校验里会被强制。
- **`DataSource` / `Load`**：u11-l1 讲过，「文件路径 | 现成字节」两种输入被 `DataSource` 统一抽象，`source.load(engine.world)` 拿到 `Loaded { data: Bytes }`。`plugin()` 的入参正是复用了这一套。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/foundations/sys.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/sys.rs) | `sys` 模块的装配函数，把 `version` 与 `inputs` 注入一个名为 `sys` 的模块。 |
| [src/foundations/plugin.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs) | WASM 插件加载器：`plugin()` 函数、`Plugin`/`PluginInstance`/`PluginFunc` 类型、协议两端（host 与 guest）的胶水代码。 |
| [src/symbols.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/symbols.rs) | 把外部 `codex` crate 提供的静态符号表（`ROOT`/`SYM`）翻译并注入到全局/数学作用域。 |
| [src/foundations/symbol.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/symbol.rs) | `Symbol` 运行时类型：变体、修饰符、用户自定义构造，以及把符号字符送进排版的 `SymbolElem`。 |
| [src/foundations/mod.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/mod.rs) | `foundations::define`，把 `sys`、`plugin` 等注册进全局作用域。 |
| [src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs) | 总装函数 `global()` 与 `LibraryBuilder`，`inputs` 在此流入 `sys`。 |

> 说明：`codex` 是一个发布在 crates.io 的外部依赖（`Cargo.toml` 里 `codex = { workspace = true }`，工作区声明 `codex = "0.3.0"`，`Cargo.lock` 显示其来源为 crates.io registry）。它对外暴露 `Module`、`Symbol`、`Def`、`Binding`、`ModifierSet`、`ROOT`、`SYM` 等类型与常量。typst-library 这侧只消费这些静态符号表，不涉及 codex 内部如何从 Unicode 数据生成它们——那部分不在本仓库内。

---

## 4. 核心概念与源码讲解

### 4.1 sys 模块：编译期可见的版本与输入

#### 4.1.1 概念说明

`sys` 是 Typst 暴露给脚本的「系统信息」窗口。它当前只提供两样东西：

- `sys.version`：当前 Typst 编译器的版本（一个 `version` 类型的值，例如 `0.13.0`）。
- `sys.inputs`：一个字典，存放宿主在编译开始前注入的键值对（典型来源是命令行参数 `--input key=value`）。

设计意图很明确：让文档能根据「在哪个版本的 Typst 上运行」「外部传入了什么参数」做出条件判断。例如 `#if sys.version >= version(0, 13, 0)[ ...]`，或用 `sys.inputs.get("mode")` 切换草稿/正式排版。

注意 `sys` **不**提供文件读写、时间、随机数等「有副作用」的能力——那些要么属于数据加载（u11-l1 的 `read`/`csv`/...），要么干脆不提供（随机数会破坏纯函数性，故 Typst 不提供，见 4.1.5 练习 1）。

#### 4.1.2 核心流程

`sys` 模块的装配非常直接：

```text
LibraryBuilder::with_inputs(dict)   // 宿主把 inputs 字典交给 builder
        │
        ▼
LibraryBuilder::build()             // 取出 inputs（缺省为空 dict）
        │  传入 global(routines, math, inputs, features)
        ▼
foundations::define(&mut global, inputs)
        │
        │  global.define("sys", sys::module(inputs))
        ▼
sys::module(inputs):
    version = 由 typst_utils::version() 的 major/minor/patch 构造
    scope.define("version", version)
    scope.define("inputs", inputs)
    返回 Module::new("sys", scope)
```

关键点：`sys` 的内容是**装配期就定死**的——`version` 来自编译器自身（更确切说是在编译期由 `env!` 烘焙进二进制），`inputs` 来自宿主。运行期脚本只能读取，不能修改。

#### 4.1.3 源码精读

`sys::module` 只有十几行，是本 crate 里最短的模块装配函数之一：

[src/foundations/sys.rs:6-18](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/sys.rs#L6-L18) —— 接收 `inputs: Dict`，把 `version` 和 `inputs` 注入作用域，包成名为 `"sys"` 的模块。

```rust
pub fn module(inputs: Dict) -> Module {
    let typst_version = typst_utils::version();
    let version = Version::from_iter([
        typst_version.major(),
        typst_version.minor(),
        typst_version.patch(),
    ]);

    let mut scope = Scope::deduplicating();
    scope.define("version", version);
    scope.define("inputs", inputs);
    Module::new("sys", scope)
}
```

版本号取自 `typst_utils::version()`，只保留前三段（major/minor/patch），构造为 `Version` 类型。`Version` 本身是一个 `EcoVec<u32>`（任意段数、语义上以无穷个零补齐），定义见 [src/foundations/version.rs:27](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/version.rs#L27)；它的 `Ord` 实现用尾部零补齐再逐段比较 [src/foundations/version.rs:152-169](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/version.rs#L152-L169)，让 `sys.version >= version(0, 13, 0)` 这样的比较成为可能（`0.8` 与 `0.8.0` 视为相等）。

> 版本号是怎么来的？`typst_utils::version()` 在 sibling crate `typst-utils` 里：它用 `env!("TYPST_VERSION")` 把版本号在**编译期**烘焙进二进制，并用 `singleton!` 缓存（[crates/typst-utils/src/version.rs:18-37](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/version.rs#L18-L37)，此文件不在本 crate 内）。`TYPST_VERSION` 通常由 `typst-utils` 的 `build.rs` 从 Cargo 包版本写入。所以 `sys.version` 反映的是「编译你手里这个 typst 二进制时所用的版本」。

`Scope::deduplicating()` 值得一提 [src/foundations/scope.rs:121-123](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/scope.rs#L121-L123)：它打开一个 debug 断言——若同一作用域里 `define` 了重名绑定，就在 debug 构建里 panic（[scope.rs:176-180](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/scope.rs#L176-L180)）。这是给标准库装配期「查重」用的护栏，`sys` 这种手写小模块用它以防 `version`/`inputs` 被误写两次。

`inputs` 的来源链路：`LibraryBuilder::with_inputs` 把字典暂存 [src/lib.rs:206-210](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L206-L210)；`build()` 里 `let inputs = self.inputs.unwrap_or_default();` 取出（缺省空 dict）[src/lib.rs:223](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L223)，再透传给 `global(...)` [src/lib.rs:224](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L224)，最终在 `foundations::define` 里挂上：

[src/foundations/mod.rs:121](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/mod.rs#L121) —— `global.define("sys", sys::module(inputs));`，于是用户脚本里 `sys` 这个名字就绑到了这个模块上。同一段 `define` 还注册了 `plugin`（[mod.rs:118](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/mod.rs#L118) `global.define_func::<plugin>();`），见 4.2。

#### 4.1.4 代码实践

**实践目标**：验证 `sys.version` 与 `sys.inputs` 的来源，并理解「装配期定死、运行期只读」。

**操作步骤（源码阅读型 + 可选运行）**：

1. 在 [src/foundations/sys.rs:6-18](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/sys.rs#L6-L18) 确认 `sys` 只有 `version` 和 `inputs` 两个绑定。
2. 顺着 `inputs` 反向追：`sys::module(inputs)` ← `foundations::define(global, inputs)`（[mod.rs:91](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/mod.rs#L91)）← `global(..., inputs, ...)` ← `LibraryBuilder::build()` 里 `self.inputs.unwrap_or_default()` ← `LibraryBuilder::with_inputs`。
3. 顺着 `version` 追：`typst_utils::version()` ← `env!("TYPST_VERSION")`（编译期烘焙），确认它不在运行期从任何 `World` 取值。
4. （可选，待本地验证）用 typst CLI 跑一段最小文档：
   ```typ
   #sys.version \
   #(sys.inputs.at("greeting", default: "none"))
   ```
   并以 `typst compile --input greeting=hello demo.typ` 传入输入，观察第二行是否变成 `hello`。

**需要观察的现象 / 预期结果**：`sys.version` 打印当前编译器版本；`sys.inputs` 是一个字典，`--input` 传入的键值会出现在其中。未传 `greeting` 时 `at(..., default:)` 回退到 `"none"`。运行结果「待本地验证」——本讲只确认源码逻辑。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `sys` 不暴露一个 `sys.random()` 函数？

> **答案**：因为 Typst 函数必须是纯函数（同样的输入产生同样的输出，这是增量编译/comemo 记忆化的前提，见 4.2）。一个返回随机数的函数会破坏这条性质——同一文档两次编译结果不同，缓存也会失效，故 Typst 刻意不提供。这与插件「纯函数约束」是同一条原则的两个侧面。

**练习 2**：`sys.version` 与 `sys.inputs` 分别在哪个阶段被确定？运行期能改吗？

> **答案**：都在标准库**装配期**确定——`version` 取自编译期烘焙进二进制的编译器版本（`env!("TYPST_VERSION")`），`inputs` 由宿主通过 `LibraryBuilder::with_inputs` 注入。一旦 `Library::build()` 完成，二者就被绑死在 `sys` 模块里，运行期脚本只能读取，不能修改。

---

### 4.2 WASM plugin：加载、协议与纯函数约束

#### 4.2.1 概念说明

`plugin` 让 Typst 能调用任意语言编译出来的 WebAssembly 模块。典型场景：用 Rust/C 写一个计算密集或涉及专门算法的函数，编译成 `.wasm`，在 Typst 里 `#let p = plugin("foo.wasm")` 加载，然后像调用普通模块函数一样 `p.bar(...)`。

它解决的问题是：Typst 脚本本身是解释执行、且受纯函数约束，不适合做重计算；WASM 插件用沙箱化方式补上了「跑任意代码」的能力，同时**把纯函数约束传染给插件**——这是它最关键的设计取舍。

插件函数的接口是**字节级**的：函数接收若干个字节缓冲区（`bytes`）作为参数，返回单个字节缓冲区。因此插件通常要用 Typst 这一侧的包装函数做类型转换（`str`/`bytes` ↔ 字节）。

> 与本 crate 其它子系统的对比：布局、文本、断行等行为的算法都住在 `typst-layout`/`typst-math`，经 `Routines` 回调（u5-l4）。而插件执行（`wasmi` 解释器）**就在本 crate 内**——因为插件是纯函数、不依赖文档/内省状态，没有 crate 分离的理由。这是一种「按是否依赖文档状态来决定放在哪个 crate」的判断。

#### 4.2.2 核心流程

加载与调用一条龙：

```text
plugin("foo.wasm")                         # 用户调用
   │  source.load(engine.world) 取字节（u11-l1 的统一加载链）
   ▼
Plugin::module(bytes)                      # #[comemo::memoize]：同字节只加载一次
   │  Plugin::new(bytes) → wasmi 编译模块 + 校验导出 memory + 注册 host 导入
   ▼
Plugin::into_module(self)                  # 遍历 wasm 导出的每个 func，生成 PluginFunc
   │  对每个导出函数：scope.bind(name, Func::from(PluginFunc{..}))
   ▼
返回 Module::anonymous(scope)              # 用户拿到一个匿名模块

调用 p.concat(a, b):
   PluginFunc::call(args)                  # #[comemo::memoize]：同参同结果，可能只调一次
   │  self.plugin.call("concat", args)
   ▼
Plugin::call:                              # 从实例池 acquire 一个 PluginInstance
   │  instance.call("concat", args)        # 把参数长度作为 i32 传入，执行 wasm
   │    ├─ 校验签名：参数全 i32、返回单个 i32
   │    ├─ CallData.args = 字节参数；wasm 通过 host 导入函数读走参数
   │    ├─ wasm 计算后调 send_result_to_host 写回结果
   │    └─ 返回码 0=成功 / 1=错误(缓冲区当 UTF-8 错误消息)
   ▼
   实例归还池；返回 Bytes
```

**纯函数约束的来源**（本模块最重要的结论，也是代码实践任务之一）：它不是一句口号，而是由两处源码机制共同体现——

1. `PluginFunc::call` 标了 `#[comemo::memoize]`：同样 `(plugin, name, args)` 的调用会被 comemo **缓存**，第二次同样入参直接返回旧结果，wasm 函数根本不会再执行。若插件对同入参返回不同结果，缓存就会给出「错误但确定」的值。
2. `Plugin` 持有一个 `pool: Mutex<Vec<PluginInstance>>`，多线程并发时会为每个线程**新建独立实例**，实例间不共享内存状态。因此插件里维护的全局变量在不同线程看到的初值可能不同。

源码文档 [src/foundations/plugin.rs:51-66](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L51-L66) 把这条约束写得很明确，并强调 Typst **出于效率原因不强制**纯函数性（"Typst does not enforce plugin function purity (for efficiency reasons)"），但违反它会导致不可复现的结果。

#### 4.2.3 源码精读

**入口函数 `plugin()`**——一个标准的 `#[func(scope)]` 原生函数，返回 `Module`：

[src/foundations/plugin.rs:148-156](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L148-L156) —— 加载字节并交给 `Plugin::module`，错误用 `.at(source.span)` 补上源码位置（u5-l3 的 `At` trait）。

```rust
#[func(scope, since = "0.8.0")]
pub fn plugin(
    engine: &mut Engine,
    /// A path to a WebAssembly file or raw WebAssembly bytes.
    source: Spanned<DataSource>,
) -> SourceResult<Module> {
    let loaded = source.load(engine.world)?;
    Plugin::module(loaded.data).at(source.span)
}
```

`source` 是 `Spanned<DataSource>`——`DataSource` 是 u11-l1 讲过的「路径 | 字节」统一抽象，所以插件既可来自文件也可来自内联字节。注意 `plugin` 自身用 `#[func(scope)]`，因为它还有子作用域函数 `plugin.transition`。

**纯函数约束的机制落点**——「might cache the results」对应这个 `#[comemo::memoize]`：

[src/foundations/plugin.rs:220-224](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L220-L224) —— `PluginFunc::call` 被记忆化，同参只真正执行一次。

```rust
#[comemo::memoize]
#[typst_macros::time(name = "call plugin")]
pub fn call(&self, args: Vec<Bytes>) -> StrResult<Bytes> {
    self.plugin.call(&self.name, args)
}
```

`PluginFunc` 是「一个插件导出函数」的句柄，它经 `cast!` 暴露为 Typst 的 `Func`（[plugin.rs:234-238](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L234-L238)），运行时对应 `FuncInner::Plugin` 变体（[func.rs:149-160](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/func.rs#L149-L160)，u3-l4 讲过五类 `Func`）。

**`Plugin` 结构：实例池与快照**——这是「多实例、可过渡」的设计核心，也是「multiple instances in multiple threads」的落点：

[src/foundations/plugin.rs:242-257](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L242-L257) —— `base`（共享的编译后模块）、`pool`（实例池）、`snapshot`（新实例的恢复点）、`fingerprint`（区分同基座不同 transition 的兄弟插件）。

```rust
struct Plugin {
    base: Arc<PluginBase>,
    pool: Mutex<Vec<PluginInstance>>,   // 并发时按需扩容
    snapshot: Option<Snapshot>,          // transition 后新实例从此恢复
    fingerprint: u128,                   // 决定 PartialEq/Hash，让 comemo 区分兄弟
}
```

`acquire` 的取用规则 [src/foundations/plugin.rs:355-363](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L355-L363)：池里有就 pop，没有就新建（必要时从 `snapshot` 恢复）。源码注释特意说明「先释放锁再建实例」，避免在锁内做重活。`call` 在执行成功后才把实例 push 回池（[plugin.rs:311-324](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L311-L324)）；若调用失败则**不归还**，因为实例可能已损坏。

**`into_module`：把 wasm 导出函数变成 Typst 模块**：

[src/foundations/plugin.rs:366-381](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L366-L381) —— 遍历 wasm 模块的所有导出，只挑函数导出，为每个生成一个 `PluginFunc` 绑定到作用域，最后包成**匿名**模块（`Module::anonymous`）。

```rust
fn into_module(self) -> Module {
    let shared = Arc::new(self);
    let mut scope = Scope::new();
    for export in shared.base.module.exports() {
        if matches!(export.ty(), wasmi::ExternType::Func(_)) {
            let name = EcoString::from(export.name());
            let func = PluginFunc { plugin: shared.clone(), name: name.clone() };
            scope.bind(name, Binding::detached(Func::from(func)));
        }
    }
    Module::anonymous(scope)
}
```

**协议两端：host 导入函数**。插件（guest）通过导入两个 host 提供的函数与 Typst（host）通信，这是「字节级接口」的实现：

[src/foundations/plugin.rs:577-595](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L577-L595) —— `wasm_minimal_protocol_write_args_to_buffer(ptr)`：插件调它，host 把本次调用的所有字节参数依次写进插件内存 `ptr` 起始处（写越界会记一个 `MemoryError`）。

[src/foundations/plugin.rs:598-612](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L598-L612) —— `wasm_minimal_protocol_send_result_to_host(ptr, len)`：插件调它，host 从插件内存读走 `len` 字节作为返回值（或错误消息）。

这两个 host 函数在 `Plugin::new` 里经 `wasmi::Linker::func_wrap` 注册到 `"typst_env"` 命名空间（[plugin.rs:283-297](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L283-L297)）。同一处还校验插件必须导出 `memory`（[plugin.rs:278-281](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L278-L281)），否则 `bail!("plugin does not export its memory")`——因为没有线性内存，host↔guest 根本无处交换字节。配置上还显式关闭了 `wasm_relaxed_simd`（[plugin.rs:271-272](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L271-L272)），因为它可能引入非确定性，与纯函数要求冲突。

调用约定可对照文档 [src/foundations/plugin.rs:89-137](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L89-L137) 归纳成下表：

| 环节 | 约定 |
| --- | --- |
| 函数签名 | 接收 `n` 个 `i32`（=各参数字节长度），返回 1 个 `i32`（状态码） |
| 取参数 | 插件先分配 `a1+a2+...+an` 的缓冲区，再调 `write_args_to_buffer(ptr)` 填入 |
| 回结果 | 调 `send_result_to_host(ptr, len)` |
| 返回码 | `0`=成功；`1`=错误，此时缓冲区按 UTF-8 错误消息解读 |

host 这一侧的执行与状态码解析在 `PluginInstance::call`：

[src/foundations/plugin.rs:447-520](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L447-L520) —— 关键几步：**惰性**校验函数签名（参数全 `i32`、返回单个 `i32`，L459-L466；之所以惰性，是因为有些导出函数如 `_initialize` 不符合协议 schema，只在真正被调用时才报错）、检查参数个数（L469-L477）、把参数长度装箱成 `Val::I32`（L480-L483）、把字节参数塞进 `CallData.args`（L486）、调 wasm 函数取状态码 `code`（L489-L492）、再按 `code` 是 0/1/其它分支处理（L508-L517）。

```rust
// 返回码语义（L508-L517）
match code {
    wasmi::Val::I32(0) => {}                              // 成功，output 即结果
    wasmi::Val::I32(1) => match std::str::from_utf8(&output) {
        Ok(message) => bail!("plugin errored with: {message}"),  // 错误消息
        Err(_) => bail!("plugin errored, but did not return a valid error message"),
    },
    _ => bail!("plugin did not respect the protocol"),     // 其它码=违反协议
}
```

**transition API：在纯函数世界里做受控的「变更」**。有些插件需要昂贵的初始化（建表、预计算）。由于纯函数约束，初始化不能藏在普通函数调用里（结果会被缓存、副作用丢失）。Typst 用 transition API 解决：执行一次有副作用的调用，然后**派生出一个新模块**，新模块里的函数能看到这次副作用，而原模块不受影响。

[src/foundations/plugin.rs:192-201](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L192-L201) —— `plugin.transition(func, ..arguments)` 是 `plugin` 的子作用域函数（`#[func(since = "0.13.0")]`，挂在 `#[scope] impl plugin` 下）。

[src/foundations/plugin.rs:328-352](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L328-L352) —— 实现要点：先算新 `fingerprint`（把旧指纹、函数名、参数一起 `hash128`，保证派生插件有确定性身份），执行可变调用，给实例**拍照**（`snapshot`），再把该实例**移动**进新 `Plugin`（注释强调 "this is important!"——原插件因此看不到这次变更）。新指纹让兄弟插件在 `PartialEq`/`Hash` 上可区分（[plugin.rs:389-400](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L389-L400)），comemo 才不会把它们的调用结果混在一起。

#### 4.2.4 代码实践

**实践目标**：把「WASM 插件函数的纯函数约束从何而来」从源码层面说清楚，并定位它的两个强制/体现机制。

**操作步骤（源码阅读型）**：

1. 打开 [src/foundations/plugin.rs:51-66](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L51-L66)，找到文档里「Typst might cache the results」「run multiple instances ... in multiple threads」两句，以及「Typst does not enforce plugin function purity (for efficiency reasons)」。
2. 分别定位它们的源码落点：
   - 「cache the results」→ `PluginFunc::call` 上的 `#[comemo::memoize]` [src/foundations/plugin.rs:220-224](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L220-L224)（同参只真正执行一次）。
   - 「multiple instances ... in multiple threads」→ `Plugin.pool` 字段 [src/foundations/plugin.rs:248-249](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L248-L249) 与 `acquire` [src/foundations/plugin.rs:355-363](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L355-L363)（并发时新建独立实例、无共享状态）。
3. 追一次 host↔guest 数据流：参数如何进 wasm（`write_args_to_buffer` 写入插件线性内存）、结果如何回 host（`send_result_to_host` 从插件内存读字节），对照 `PluginInstance::call` 里参数以 `i32` 长度传入、状态码 0/1 解读的代码 [src/foundations/plugin.rs:447-520](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L447-L520)。

**需要观察的现象 / 预期结果**：你能用一句话回答「WASM 插件函数的纯函数约束从何而来」——**来自 comemo 记忆化（同参只执行一次）+ 多线程独立实例池（实例间无共享状态）**；Typst「不强制」纯函数性（注释明说 for efficiency reasons），但违反它会导致不可复现的结果。

#### 4.2.5 小练习与答案

**练习 1**：假设一个插件函数 `add(x)` 把 `x` 累加进内部全局变量并返回新值。用 `plugin.transition` 与直接调用两条路分别会发生什么？

> **答案**：直接调用 `p.add("a")` 两次，由于 `PluginFunc::call` 被 comemo 记忆化，第二次很可能直接返回缓存结果而不真正执行；且不同线程拿到的实例全局变量初值不同——结果不可预测。正确做法是用 `plugin.transition(p.add, "a")` 得到一个派生模块 `p2`，`p2` 的函数能看到这次累加（因为 transition 把执行过副作用的实例移进了新插件并保存了快照），而原 `p` 保持不变。

**练习 2**：`Plugin` 的 `PartialEq`/`Hash` 为什么把 `fingerprint` 也算进去，而不只比 `base.bytes`？

> **答案**：transition 派生出的「兄弟」插件共享同一份 wasm 字节（`base.bytes` 相同），但它们观察到的副作用不同，是**不同的值**。若只比字节，comemo 会把它们当成同一个键，从而把在一处调用的结果错误地复用到另一处。`fingerprint` 把历次 transition 的函数名与参数哈希进去（`hash128(&(self.fingerprint, func, &args))`，[plugin.rs:330](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L330)），让兄弟插件在相等与哈希上可区分，保证记忆化正确。

**练习 3**：插件协议要求 wasm 模块必须导出名为 `memory` 的线性内存，为什么？又为什么配置里要关闭 relaxed SIMD？

> **答案**：因为 host 与 guest 之间传参/回结果**没有别的通道**——host 必须能把字节写进插件内存（`write_args_to_buffer`）、从插件内存读字节（`send_result_to_host`）。`Plugin::new` 因此在校验阶段就 `bail!("plugin does not export its memory")`（[plugin.rs:278-281](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L278-L281)）。关闭 relaxed SIMD（[plugin.rs:271-272](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L271-L272)）是因为它可能引入非确定性（同一输入在不同 CPU 上结果不同），与纯函数要求冲突。

---

### 4.3 symbols / codex：符号表的注入

#### 4.3.1 概念说明

Typst 内置了成百上千个 Unicode 符号（数学符号、emoji 等），分两个命名空间暴露给用户：

- `sym`：通用符号（如 `#sym.arrow.r`）。
- `emoji`：表情符号（如 `#emoji.face.halo`）。

在**数学模式**里，`sym` 里的符号还能省略前缀直接写（`$arrow.r$`、`$alpha$`），这是排版数学公式的核心便利。

这些符号数据**不是手写在 typst-library 里的**。它们由外部 `codex` crate 提供成两张静态表：

- `codex::ROOT`：根表，迭代产出 `sym`、`emoji` 等**子模块**，注入全局作用域。
- `codex::SYM`：数学符号表，迭代产出**裸符号**，注入数学作用域。

typst-library 这侧（`src/symbols.rs`）只负责「翻译 + 注入」：把 codex 的 `Symbol`/`Module` 定义转成 Typst 的 `Value::Symbol`/`Value::Module`，挂到对应作用域。这种「数据在外部、本 crate 只消费」的分工，和本 crate 一贯的「类型在此、行为在彼」（u1-l1/u5-l4）思路一脉相承——只是这次分离的不是「行为」，而是「庞大的静态数据表」。

#### 4.3.2 核心流程

两条注入路径，共用同一个翻译函数：

```text
路径 A：全局符号（sym / emoji）
  global() ── symbols::define(&mut global)
     │   global.start_category(Category::Symbols)
     │   extend_scope_from_codex_module(global, codex::ROOT)
     │       for (name, binding) in codex::ROOT.iter():
     │           Symbol(s) → Value::Symbol(s.into())   # codex::Symbol → Symbol
     │           Module(m) → Value::Module(...)        # 递归成子模块（sym、emoji）
     │   global.reset_category()
     ▼
  全局可见：#sym.arrow.r、#emoji.face.halo

路径 B：数学裸符号
  math::module() ── symbols::define_math(&mut math)
     │   extend_scope_from_codex_module(math, codex::SYM)
     │       把 codex::SYM 的符号直接铺进 math 作用域
     ▼
  数学模式可见：$alpha$（裸写）、$arrow.r$
```

那 `math.sym.arrow.r` 为什么也能用？这是本讲代码实践任务之二的核心。答案是**作用域回退**（u2-l3）：数学模式的 `Scopes::get` 找不到 `sym` 时，会回退到标准库全局，而全局里已经有路径 A 注入的 `sym` 模块。所以「裸符号」来自路径 B（`codex::SYM` 直铺），「`sym.` 前缀」来自路径 A 经回退可见——两路合起来正好对应文档里「sym 模块的元素在数学模式可省略前缀」这句话。

#### 4.3.3 源码精读

**两个装配函数**：`define` 走全局、`define_math` 走数学，都只是 `extend_scope_from_codex_module` 的薄包装：

[src/symbols.rs:6-15](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/symbols.rs#L6-L15) —— 注意 `define` 用 `start_category`/`reset_category` 给整批符号盖上 `Category::Symbols` 分类标签（u1-l2/u1-l3 讲过的区间式分类）；`define_math` 不盖分类，因为它注册到的是 `math` 模块内部（`math` 模块整体已是 `Category::Math`）。

```rust
pub(super) fn define(global: &mut Scope) {
    global.start_category(crate::Category::Symbols);
    extend_scope_from_codex_module(global, codex::ROOT);
    global.reset_category();
}

pub(super) fn define_math(math: &mut Scope) {
    extend_scope_from_codex_module(math, codex::SYM);
}
```

**翻译函数 `extend_scope_from_codex_module`**——本模块的核心，把 codex 的定义枚举分派成 Typst 的 `Value`：

[src/symbols.rs:17-29](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/symbols.rs#L17-L29) —— 对每个 `(name, binding)`，按 `binding.def` 是 `Symbol` 还是 `Module` 分流；若带 `deprecation` 消息，就在绑定上标记弃用（`deprecated(...)`），用户访问时会收到警告。

```rust
fn extend_scope_from_codex_module(scope: &mut Scope, module: codex::Module) {
    for (name, binding) in module.iter() {
        let value = match binding.def {
            codex::Def::Symbol(s) => Value::Symbol(s.into()),
            codex::Def::Module(m) => Value::Module(Module::new(name, m.into())),
        };
        let scope_binding = scope.define(name, value);
        if let Some(message) = binding.deprecation {
            scope_binding.deprecated(Deprecation::new().with_message(message));
        }
    }
}
```

`Module(m) => Value::Module(Module::new(name, m.into()))` 里的 `m.into()` 用的是 `From<codex::Module> for Scope` [src/symbols.rs:31-37](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/symbols.rs#L31-L37)——它递归地再调一次 `extend_scope_from_codex_module`，于是 `sym`、`emoji` 这样的嵌套命名空间被原样展开成 Typst 的子模块树。

**codex::Symbol → Symbol 的转换**——把外部表示翻译成本 crate 的运行时类型：

[src/symbols.rs:39-46](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/symbols.rs#L39-L46) —— `Single`（单字符、无变体）与 `Multi`（多命名变体）分别对应 `Symbol::single` 与 `Symbol::list`（详见 4.4）。

```rust
impl From<codex::Symbol> for Symbol {
    fn from(symbol: codex::Symbol) -> Self {
        match symbol {
            codex::Symbol::Single(value) => Symbol::single(value),
            codex::Symbol::Multi(list) => Symbol::list(list),
        }
    }
}
```

**挂载点**：`define` 在总装函数里被调用 [src/lib.rs:344](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L344)（`self::symbols::define(&mut global);`）；`define_math` 在 `math::module()` 里被调用 [src/math/mod.rs:105](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/mod.rs#L105)（`crate::symbols::define_math(&mut math);`），而后者最终经 `global.define("math", math)` 挂到全局 [src/lib.rs:346](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L346)。

#### 4.3.4 代码实践

**实践目标**：说清 `codex::SYM` 如何变成数学模式里的裸符号与 `math.sym`。

**操作步骤（源码阅读型）**：

1. 在 [src/symbols.rs:13-15](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/symbols.rs#L13-L15) 确认 `define_math` 把 `codex::SYM` 直铺进 `math` 作用域——这解释了 `$alpha$` 这类裸写。
2. 在 [src/symbols.rs:6-10](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/symbols.rs#L6-L10) 确认 `define` 把 `codex::ROOT` 铺进全局，其中 `sym` 是一个 `codex::Def::Module`，会被递归翻译成全局的 `sym` 子模块。
3. 结合 u2-l3 的 `Scopes::get` 回退规则推出：数学模式写 `sym.alpha` 时，`sym` 在 `math` 作用域里找不到，**回退到全局**命中路径 A 注册的 `sym` 模块。
4. （可选，待本地验证）写一段最小文档验证三种写法等价：
   ```typ
   #sym.arrow.r \
   $arrow.r$ \
   $sym.arrow.r$
   ```
   预期三行渲染出同一个右箭头。

**需要观察的现象 / 预期结果**：你能区分两条来源——裸符号（`$alpha$`）由 `codex::SYM` 经 `define_math` 直铺而来；`sym.` 前缀在数学模式可用，是作用域回退到全局 `sym` 模块的结果。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `define` 要用 `start_category`/`reset_category` 包裹，而 `define_math` 不用？

> **答案**：`define` 直接往**全局**作用域注册符号，需要给这批绑定打上 `Category::Symbols` 分类标签（供文档分组、自动补全）。而 `define_math` 往**数学模块内部**注册，整个 `math` 模块在 `math::module()` 里已处于 `Category::Math` 区间内，数学符号继承 `Math` 分类即可，无需重复。

**练习 2**：codex 给某个符号标注了弃用消息，这条信息是如何传到用户的？

> **答案**：`extend_scope_from_codex_module` 检查 `binding.deprecation`，若有就对该绑定调 `scope_binding.deprecated(Deprecation::new().with_message(message))`（[symbols.rs:25-27](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/symbols.rs#L25-L27)）。于是用户访问该符号时，编译器会发出带消息的弃用警告。

**练习 3**：`extend_scope_from_codex_module` 遇到 `codex::Def::Module` 时为什么要 `Module::new(name, m.into())`，其中的 `m.into()` 做了什么？

> **答案**：`m` 是一个 `codex::Module`（codex 侧的子表，比如 `sym` 表）。`m.into()` 调用 `From<codex::Module> for Scope`（[symbols.rs:31-37](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/symbols.rs#L31-L37)），它新建一个空 `Scope` 并**递归**再调一次 `extend_scope_from_codex_module`，把子表里的每个符号/子模块翻译进去。这样 `sym`、`emoji` 这种嵌套命名空间就被原样展开成 Typst 的子模块树，用户才能写 `sym.arrow.r`、`emoji.face.halo` 这样的点号路径。

---

### 4.4 Symbol 类型：变体与修饰符

#### 4.4.1 概念说明

`Symbol` 是符号在运行时的类型（`#[ty(scope, cast, ...)]` 注册为一等类型，可用 `symbol(...)` 构造）。它的精妙之处在于「**一个符号名 + 多个变体 + 修饰符选择**」：

- 一个符号（如 `arrow`）可以有多个**变体**（variant），每个变体由「一组修饰符 + 一个字符」组成，例如 `(l, ←)`、`(r, →)`、`(t.quad, ⇑)`。
- 用点号追加修饰符来选变体：`sym.arrow.l`、`sym.arrow.r`、`sym.arrow.t.quad`。修饰符顺序无关，Typst 会挑出「包含所有已加修饰符、且额外修饰符最少」的那个变体（最佳匹配）。

这套机制让一个逻辑符号（arrow）能覆盖它的多种字形（左、右、双向、带杠…），而用户只需用点号组合。修饰符的匹配用 `ModifierSet`（来自 codex）实现，本质是集合运算。

`Symbol` 是值；要把它显示到文档里，靠元素 `SymbolElem`（一个只带 `text: EcoString` 的 `#[required]` 字段的最小元素，字段名与 `TextElem` 对齐）。

#### 4.4.2 核心流程

符号的三种内部形态之间会随修饰符追加而迁移：

```text
追加修饰符 arrow.l：
  Single("→")              # 无变体：不支持任何修饰符
  Complex([...变体...])     # 有变体但尚未加修饰符
      │  首次 modified() 把它升级为 Modified
      ▼
  Modified { list, modifiers: {l}, .. }   # 已加修饰符

取最终字符 .get()：
  在 list 的所有变体里，用 modifiers 做 best_match_in：
     筛出「包含全部已加修饰符」的变体，
     再选「额外修饰符最少」的那个，
     返回它的字符。
```

用户自定义符号走另一条构造路径：

```text
symbol("🗙", ("stamped","🏒"), ..)        # 用户构造
   │  construct() 校验：每个值恰好 1 个 grapheme、修饰符是合法标识符、无重复
   ▼
Symbol::runtime(Box<[Variant<EcoString>]>)
   → 内部存为 Modified { list: List::Runtime(..), modifiers: 空, .. }
```

#### 4.4.3 源码精读

**`Symbol` 与三种内部形态**——`Symbol(SymbolInner)`，`SymbolInner` 枚举：

[src/foundations/symbol.rs:49-62](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/symbol.rs#L49-L62) —— `Single`（单值静态符号）、`Complex`（多变体静态符号）、`Modified`（已施加修饰符，用 `Arc<Modified>` 共享以便写时复制）。

```rust
#[ty(scope, cast, since = "forever")]
#[derive(Debug, Clone, Eq, PartialEq, Hash)]
pub struct Symbol(SymbolInner);

enum SymbolInner {
    Single(&'static str),
    Complex(&'static [Variant<&'static str>]),
    Modified(Arc<Modified>),
}
```

`Variant<S>` 是三元组 `(ModifierSet<S>, S, Option<S>)` [src/foundations/symbol.rs:80](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/symbol.rs#L80)：修饰符集合、字符值、可选弃用消息。

**构造器 `single` / `list` / `runtime`**：

[src/foundations/symbol.rs:89-116](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/symbol.rs#L89-L116) —— `single`/`list` 是 `const fn`，用于 codex 静态符号（编译期常量）；`runtime` 给用户自定义符号用，内部用 `List::Runtime(Box<[..]>)` 存储堆上的变体表。

```rust
pub const fn single(value: &'static str) -> Self {
    Self(SymbolInner::Single(value))
}
pub const fn list(list: &'static [Variant<&'static str>]) -> Self {
    debug_assert!(!list.is_empty());
    Self(SymbolInner::Complex(list))
}
```

**取值 `get()`——最佳匹配**：

[src/foundations/symbol.rs:118-130](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/symbol.rs#L118-L130) —— 对 `Complex` 用空修饰符集、对 `Modified` 用已加修饰符集，在变体表里调 `best_match_in` 选最优变体的字符。

```rust
pub fn get(&self) -> &str {
    match &self.0 {
        SymbolInner::Single(value) => value,
        SymbolInner::Complex(_) => ModifierSet::<&'static str>::default()
            .best_match_in(self.variants().map(|(m, v, _)| (m, v))).unwrap(),
        SymbolInner::Modified(arc) => arc
            .modifiers
            .best_match_in(self.variants().map(|(m, v, _)| (m, v))).unwrap(),
    }
}
```

**追加修饰符 `modified()`——形态升级 + 集合插入**。用户写 `arrow.l` 时，求值器最终调到这里（字段访问 `.l` 的求值在 typst-eval，本类型提供 `modified`）：

[src/foundations/symbol.rs:141-174](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/symbol.rs#L141-L174) —— 若当前是 `Complex`，先升级为 `Modified`（这样后续可叠加多个修饰符）；然后在 `Modified` 分支里 `Arc::make_mut` 写时复制后 `modifiers.insert_raw(modifier)`，并用 `best_match_in` 查这组修饰符当前命中哪个变体（顺便触发该变体携带的弃用警告，且每个符号只警告一次）。若修饰符在任何已知变体上都匹配不到，`bail!("unknown symbol modifier")`。

```rust
pub fn modified(mut self, mut sink: impl WarningSink, modifier: &str) -> StrResult<Self> {
    if let SymbolInner::Complex(list) = self.0 {
        // 升级 Complex → Modified
        self.0 = SymbolInner::Modified(Arc::new(Modified {
            list: List::Static(list), modifiers: ModifierSet::default(), deprecated: false,
        }));
    }
    if let SymbolInner::Modified(arc) = &mut self.0 {
        let modified = Arc::make_mut(arc);
        modified.modifiers.insert_raw(modifier);
        // ... 查最佳匹配，必要时发一次弃用警告 ...
        return Ok(self);
    }
    bail!("unknown symbol modifier")
}
```

**用户自定义构造 `Symbol::construct`**——用 `#[func(constructor)]` 暴露为 `symbol(...)`：

[src/foundations/symbol.rs:218-311](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/symbol.rs#L218-L311) —— 接收可变参数（每个是单字符串或 `[修饰符串, 字符]` 二元数组，由 `SymbolVariant` 的 `cast!` 归一，[symbol.rs:416-426](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/symbol.rs#L416-L426)），做四类校验：

1. 值必须恰为一个 grapheme cluster（L249-L254）；
2. 修饰符必须是合法标识符（L258-L268）；
3. 同一变体内修饰符不得重复（L275-L281）；
4. 同一组修饰符不得重复出现——用 `sort_unstable` 规范化顺序后 `hash128` 去重（L284-L301）；

最后 `Symbol::runtime(list)` 落地（L306-L310）。

```rust
if v.1.is_empty() || v.1.graphemes(true).nth(1).is_some() {
    errors.push(error!(span, "invalid variant value: {}", v.1.repr();
        hint: "variant value must be exactly one grapheme cluster";));
}
// ...
modifiers.sort_unstable();   // 规范化修饰符顺序
// ... 用 hash128(&modifiers) 去重 ...
```

> 这里对修饰符做 `sort_unstable` 规范化、再用哈希去重，正是为了保证「修饰符顺序无关」——`(stamped.pen, …)` 与 `(pen.stamped, …)` 被视为同一变体。

**符号如何变成可见内容**：`SymbolElem` 是把符号字符送进排版流水线的元素：

[src/foundations/symbol.rs:450-455](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/symbol.rs#L450-L455) —— 只有一个 `#[required]` 的 `text: EcoString` 字段，实现 `Repr`（显示为 `[X]`）与 `PlainText`（纯文本导出时输出字符）。`SymbolElem::packed` 是便捷构造（[symbol.rs:457-463](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/symbol.rs#L457-L463)）。

```rust
#[elem(Repr, PlainText)]
pub struct SymbolElem {
    #[required]
    pub text: EcoString, // This is called `text` for consistency with `TextElem`.
}
```

此外，少数符号还是「可调用的」：`Symbol::func()`（[symbol.rs:133-138](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/symbol.rs#L133-L138)）会查它是否是某个重音（accent）或定界符伸缩（lr）的同名函数，这在 u10-l2 讲过（`accent`/`lr` 的 `FUNCS` 表惰性生成同名函数）。

#### 4.4.4 代码实践

**实践目标**：通过自定义符号，亲手走一遍「变体 + 修饰符 + 最佳匹配」。

**操作步骤（源码阅读型 + 可选运行）**：

1. 读 [src/foundations/symbol.rs:218-311](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/symbol.rs#L218-L311)，列出 `construct` 的四类校验，并解释为什么「值必须恰为一个 grapheme」。
2. 读 `get()` [src/foundations/symbol.rs:118-130](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/symbol.rs#L118-L130) 与 `modified()` [src/foundations/symbol.rs:141-174](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/symbol.rs#L141-L174)，写出 `sym.arrow.t.quad` 的解析过程：先 `arrow` 取到 `Complex`，加 `t` 升级为 `Modified{modifiers:{t}}`，再加 `quad` 变成 `{t,quad}`，`best_match_in` 选出同时含 `t` 和 `quad` 的变体。
3. （可选，待本地验证）仿照文档示例自定义一个符号并访问其变体：
   ```typ
   #let envelope = symbol(
     "🖂",
     ("stamped", "🖃"),
     ("stamped.pen", "ULK"),
   )
   #envelope \
   #envelope.stamped \
   #envelope.pen.stamped   // 修饰符顺序无关，应与上一行相同
   ```
   预期最后两行渲染出同一个字形（`envelope.pen.stamped` 与 `envelope.stamped.pen` 等价）。运行结果「待本地验证」。

**需要观察的现象 / 预期结果**：你能解释「修饰符顺序无关」是如何被保证的（构造期 `sort_unstable` + 哈希去重，取值期 `ModifierSet` 集合匹配），并能说明 `Symbol`（值）与 `SymbolElem`（元素）的分工——前者是可被点号修饰的值，后者是把字符送进排版的元素。

#### 4.4.5 小练习与答案

**练习 1**：`SymbolInner::Single` 形态的符号支持修饰符吗？为什么？

> **答案**：不支持。`Single` 只有一个字符、没有变体表，`modified()` 里的两个 `if` 分支（`Complex` 升级、`Modified` 插入）都不命中，直接落到 `bail!("unknown symbol modifier")`。只有 `Complex`（多变体）或 `Modified` 形态才能接受修饰符。

**练习 2**：用户写 `symbol("ab")` 会发生什么？为什么？

> **答案**：构造校验会报错 `"invalid variant value"`，提示 `"variant value must be exactly one grapheme cluster"`（[symbol.rs:249-254](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/symbol.rs#L249-L254)）。因为一个符号变体必须恰好是一个用户感知的字符（grapheme cluster），`"ab"` 是两个字符，不合法。校验用的是 `v.1.graphemes(true).nth(1).is_some()`——第二个 grapheme 存在即说明不止一个字符。

**练习 3**：`Symbol` 和 `SymbolElem` 有什么区别？

> **答案**：`Symbol` 是**值类型**（`#[ty]`），描述「一个逻辑符号 + 它的变体 + 已加修饰符」，可被点号继续修饰（`arrow.l`），本身不直接参与排版。`SymbolElem` 是**元素**（`#[elem]`），唯一字段是 `text: EcoString`，负责把最终选定的字符送进排版流水线（并参与纯文本导出）。求值/排版阶段会把 `Symbol` 经 `get()` 解析出的字符打包成 `SymbolElem` 来渲染。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成一次「从外部注入到符号渲染」的全链路追踪。

**任务**：假设你正在为 typst 写一份内部技术备忘，需要向新同事解释「为什么 `$alpha$` 能渲染出一个希腊字母，而 `#sys.inputs` 又是从哪来的，`plugin` 又凭什么要求纯函数」。请按下列步骤产出一份说明（含调用链与源码行号）：

1. **外部输入侧（4.1）**：从 CLI 的 `--input` 出发，追到 `LibraryBuilder::with_inputs` [src/lib.rs:206-210](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L206-L210) → `build()` [src/lib.rs:223-224](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L223-L224) → `global(..., inputs, ...)` → `foundations::define` [src/foundations/mod.rs:121](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/mod.rs#L121) → `sys::module(inputs)` [src/foundations/sys.rs:6-18](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/sys.rs#L6-L18)。说明 `sys.inputs` 为何是只读的（装配期定死）。另追 `sys.version` 来自 `env!("TYPST_VERSION")` 的编译期烘焙。
2. **符号侧（4.3 + 4.4）**：追 `$alpha$` 的来源——`math::module()` 调 `symbols::define_math` [src/symbols.rs:13-15](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/symbols.rs#L13-L15)，后者把 `codex::SYM` 经 `extend_scope_from_codex_module` [src/symbols.rs:17-29](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/symbols.rs#L17-L29) 直铺进数学作用域。说明 codex 的 `Symbol::Single`/`Multi` 如何经 `From<codex::Symbol>` [src/symbols.rs:39-46](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/symbols.rs#L39-L46) 变成 `Symbol::single`/`list`，并指出 `math.sym.alpha` 还能用是作用域回退到全局 `sym` 模块的结果。
3. **插件对照（4.2）**：简述 `plugin("foo.wasm")` 如何经 `Plugin::module` → `into_module` [src/foundations/plugin.rs:366-381](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L366-L381) 把 wasm 导出函数变成模块；并指出 `PluginFunc::call` 上的 `#[comemo::memoize]` [src/foundations/plugin.rs:220-224](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L220-L224) 与 `Plugin.pool` [src/foundations/plugin.rs:248-249](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L248-L249) 共同构成纯函数约束的源头。
4. **产出**：画一张包含「宿主 → LibraryBuilder → global()/math::module() → sys / sym / plugin」的数据流图，并在每个节点标注源码位置。

**预期结果**：你能用一条连贯的故事线，把「外部数据/外部代码如何进入标准库」讲清楚，并区分三种不同的进入方式——`sys` 是装配期注入的常量、`sym`/`emoji` 是外部 codex 提供的静态表、`plugin` 是运行期按需加载的 WASM 模块。实际编译结果「待本地验证」——本任务重在把链路对应到源码。

## 6. 本讲小结

- `sys` 模块只暴露 `version`（编译器版本，编译期烘焙）与 `inputs`（宿主注入的字典），二者都在标准库**装配期**定死，运行期只读；`inputs` 经 `LibraryBuilder::with_inputs` → `build()` → `foundations::define` → `sys::module` 流入。
- `plugin()` 把一段 WASM 字节加载成匿名 `Module`，每个 wasm 导出函数变成一个 `PluginFunc`；host 与 guest 通过两个导入函数（`write_args_to_buffer`/`send_result_to_host`）经线性内存交换字节，状态码 0/1 区分成功/错误。与布局/文本不同，WASM 执行就在本 crate 内。
- **插件纯函数约束**由两处源码机制体现：`PluginFunc::call` 的 `#[comemo::memoize]`（同参只执行一次）+ `Plugin.pool` 多线程实例池（实例间无共享状态）；transition API 是在纯函数世界里做受控变更的官方出口。
- 符号数据由外部 `codex` crate（crates.io，v0.3.0）提供：`codex::ROOT` 经 `symbols::define` 注入全局（产出 `sym`/`emoji` 子模块），`codex::SYM` 经 `symbols::define_math` 直铺进数学作用域（产出裸符号）；`math.sym` 可用则是作用域回退到全局的结果。
- `Symbol` 运行时类型有 `Single`/`Complex`/`Modified` 三种内部形态，修饰符用 `ModifierSet` 集合匹配选最佳变体（顺序无关、构造期 `sort`+哈希去重）；`Symbol` 是值，`SymbolElem` 才是把字符送进排版的元素。

## 7. 下一步学习建议

- **u11-l3（可视化：颜色、描边、形状、曲线与图像）**：这是「面向用户」标准库内容的最后一块，同样遵循「类型在此、行为在彼」；其中的 `ImageElem` 按格式（raster/svg）分发解码，与本讲 `plugin` 的「加载字节 → 分派处理」可对照阅读。
- **u12-l2（性能与并发：comemo、rayon、LazyHash 与 singleton）**：本讲反复出现的 `#[comemo::memoize]`、`Arc`、`Mutex`、实例池、`singleton!`（`typst_utils::version` 里就用到），正是 Typst 性能手段的实例；那一讲会系统讲解。
- **深入 codex**：若你对符号表如何提供 `ROOT`/`SYM` 与 `ModifierSet`、`Variant` 等类型感兴趣，可阅读外部 `codex` crate（v0.3.0），理解它如何组织静态符号数据。注意它不在本仓库内。
- **WASM 协议实战**：参考 plugin 文档里给出的 [wasm-minimal-protocol 仓库](https://github.com/typst-community/wasm-minimal-protocol)，亲手用 Rust 写一个最小插件（导出 `memory`、实现两步协议），再用 `plugin(...)` 加载并调用，验证本讲讲的协议与纯函数约束。
