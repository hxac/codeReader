# sys 模块、WASM plugin 与符号系统

## 1. 本讲目标

本讲聚焦 `typst-library` 里三组「与外界打交道」的工具：系统模块 `sys`、WebAssembly 插件加载器 `plugin`、以及符号系统（`codex` 符号表 + `Symbol` 运行时类型）。学完后你应该能够：

- 说清 `sys` 模块对外暴露了什么（`version`、`inputs`），以及它是怎么在标准库装配阶段被注入的。
- 理解 `plugin()` 如何把一段 WebAssembly 字节加载成一个 Typst 模块，并讲清插件函数为何必须是「纯函数」——以及这条约束在源码里到底由哪个机制强制。
- 讲清 `codex::ROOT` 与 `codex::SYM` 两张静态符号表如何分别流入全局作用域（`sym`/`emoji`）和数学作用域（裸符号）。
- 掌握 `Symbol` 运行时类型的三种内部形态、修饰符（modifier）机制，以及用户自定义符号的构造过程。

本讲承接 u3-l4（`func` 宏、`NativeFunc` 与 `Args`）：`plugin`、`plugin.transition` 都是用 `#[func]`/`#[scope]` 定义的原生函数；同时也承接 u2-l3 的 `Scope`/`Module` 查找与回退机制，后者正是「为什么数学模式里 `sym.alpha` 也能用」的关键。

## 2. 前置知识

- **模块与作用域**：Typst 标准库在装配期被组织成一棵 `Scope`（名字→`Binding` 的有序映射）树，再包成 `Module`。u2-l3 已讲过 `Scopes` 的查找会按「当前层 → 外层 → 全局 → 特判 `std`」回退。本讲会反复用到这一点。
- **`#[func]` 与 `#[scope]`**：u3-l4 讲过，`#[func]` 把一个 Rust `fn` 变成 Typst 原生函数；`#[func(scope)]` 让函数拥有子作用域（如 `plugin.transition`）。本讲把 `plugin` 当作真实案例再走一遍。
- **comemo 记忆化**：u5-l1/u12-l2 提到，`#[comemo::track]` 与 `#[comemo::memoize]` 是 Typst 增量编译的根基——同样的输入应得到同样的输出。这条性质正是插件「纯函数约束」的源头。
- **WebAssembly（WASM）**：一种可移植的字节码虚拟机格式。本讲不需要你会写 WASM，但要理解它是一种「沙箱化、跨语言」的可执行载体。Typst 用 `wasmi` 这个纯 Rust 的 WASM 解释器来跑插件。
- **Unicode 与 grapheme**：符号系统的核心是 Unicode 字符。一个「符号变体」的值必须恰好是一个 grapheme cluster（用户感知的一个字符），这点在自定义符号校验里会被强制。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/foundations/sys.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/sys.rs) | `sys` 模块的装配函数，把 `version` 与 `inputs` 注入一个名为 `sys` 的模块。 |
| [src/foundations/plugin.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs) | WASM 插件加载器：`plugin()` 函数、`Plugin`/`PluginInstance`/`PluginFunc` 类型、协议两端（host 与 guest）的胶水代码。 |
| [src/symbols.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/symbols.rs) | 把外部 `codex` crate 生成的静态符号表（`ROOT`/`SYM`）翻译并注入到全局/数学作用域。 |
| [src/foundations/symbol.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/symbol.rs) | `Symbol` 运行时类型：变体、修饰符、用户自定义构造，以及把符号变成可见内容的 `SymbolElem`。 |
| [src/foundations/mod.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/mod.rs) | `foundations::define`，把 `sys`、`plugin` 等注册进全局作用域。 |
| [src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs) | 总装函数 `global()` 与 `LibraryBuilder`，`inputs` 在此流入 `sys`。 |

> 说明：`codex` 是一个 workspace 级外部 crate（`Cargo.toml` 里 `codex = { workspace = true }`），它在构建期从 Unicode 数据生成静态符号表，并暴露 `Module`、`Symbol`、`Def`、`Binding`、`ModifierSet`、`ROOT`、`SYM` 等类型与常量。本讲不深入 codex 的生成过程，只讲 typst-library 这一侧如何消费它。

---

## 4. 核心概念与源码讲解

### 4.1 sys 模块：编译期可见的版本与输入

#### 4.1.1 概念说明

`sys` 是 Typst 暴露给脚本的「系统信息」窗口。它当前只提供两样东西：

- `sys.version`：当前 Typst 编译器的版本（一个 `version` 类型的值，例如 `0.13.0`）。
- `sys.inputs`：一个字典，存放宿主在编译开始前注入的键值对（典型来源是命令行参数 `--input key=value`）。

设计意图很明确：让文档能根据「在哪个版本的 Typst 上运行」「外部传入了什么参数」做出条件判断。例如 `#if sys.version >= version(0, 13, 0) { ... }`，或用 `sys.inputs.get("mode")` 切换草稿/正式排版。

注意 `sys` 不提供文件读写、时间、随机数等「有副作用」的能力——那些要么属于数据加载（u11-l1 的 `read`/`csv`/...），要么干脆不提供（随机数会破坏纯函数性，故 Typst 不提供）。

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

关键点：`sys` 的内容是**装配期就定死**的——`version` 来自编译器自身，`inputs` 来自宿主。运行期脚本只能读取，不能修改。

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

版本号取自 `typst_utils::version()`，只保留前三段（major/minor/patch），构造为 `Version` 类型。`Version` 本身是一个 `EcoVec<u32>`（任意段数、语义上以无穷个零补齐），见 [src/foundations/version.rs:27](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/version.rs#L27) 与它的 `Ord` 实现 [src/foundations/version.rs:152-169](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/version.rs#L152-L169)，后者让 `sys.version >= version(0, 13, 0)` 这样的比较成为可能。

`inputs` 的来源链路：`LibraryBuilder::with_inputs` 把字典暂存 [src/lib.rs:206-210](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L206-L210)，`build()` 里 `let inputs = self.inputs.unwrap_or_default();` 取出（缺省空 dict）[src/lib.rs:223](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L223)，再透传给 `global(...)` [src/lib.rs:224](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L224)，最终在 `foundations::define` 里挂上：

[src/foundations/mod.rs:121](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/mod.rs#L121) —— `global.define("sys", sys::module(inputs));`，于是用户脚本里 `sys` 这个名字就绑到了这个模块上。

#### 4.1.4 代码实践

**实践目标**：验证 `sys.version` 与 `sys.inputs` 的来源，并理解「装配期定死」。

**操作步骤（源码阅读型）**：

1. 在 [src/foundations/sys.rs:6-18](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/sys.rs#L6-L18) 确认 `sys` 只有 `version` 和 `inputs` 两个绑定。
2. 顺着 `inputs` 反向追：`sys::module(inputs)` ← `foundations::define(global, inputs)` ← `global(..., inputs, ...)` ← `LibraryBuilder::build()` 里 `self.inputs.unwrap_or_default()` ← `LibraryBuilder::with_inputs`。
3. （可选，待本地验证）用 typst CLI 跑一段最小文档：
   ```typ
   #sys.version \
   #(sys.inputs.at("greeting", default: "none"))
   ```
   并以 `typst compile --input greeting=hello demo.typ` 传入输入，观察第二行是否变成 `hello`。

**需要观察的现象 / 预期结果**：`sys.version` 打印当前编译器版本；`sys.inputs` 是一个字典，`--input` 传入的键值会出现在其中。未传 `greeting` 时 `at(..., default:)` 回退到 `"none"`。若你无法本地运行，请将 CLI 行为标注为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `sys` 不暴露一个 `sys.random()` 函数？
**答案**：因为 Typst 函数必须是纯函数（同样的输入产生同样的输出，这是增量编译/comemo 记忆化的前提，见 4.2）。一个返回随机数的函数会破坏这条性质——同一文档两次编译结果不同，缓存也会失效，故 Typst 刻意不提供。

**练习 2**：`sys.version` 与 `sys.inputs` 分别在哪个阶段被确定？运行期能改吗？
**答案**：都在标准库**装配期**确定——`version` 取自编译器自身版本，`inputs` 由宿主通过 `LibraryBuilder::with_inputs` 注入。一旦 `Library::build()` 完成，二者就被绑死在 `sys` 模块里，运行期脚本只能读取，不能修改。

---

### 4.2 WASM plugin：加载、协议与纯函数约束

#### 4.2.1 概念说明

`plugin` 让 Typst 能调用任意语言编译出来的 WebAssembly 模块。典型场景：用 Rust/C 写一个计算密集或涉及专门算法（比如复杂排版、图像处理）的函数，编译成 `.wasm`，在 Typst 里 `#let p = plugin("foo.wasm")` 加载，然后像调用普通模块函数一样 `p.bar(...)`。

它解决的问题是：Typst 脚本本身是解释执行、且受纯函数约束，不适合做重计算；而 WASM 插件用沙箱化方式补上了「跑任意代码」的能力，同时**把纯函数约束传染给插件**——这是它最关键的设计取舍。

插件函数的接口是**字节级**的：函数接收若干个字节缓冲区（`bytes`）作为参数，返回单个字节缓冲区。因此插件通常要用 Typst 这一侧的包装函数做类型转换（`str`/`bytes` ↔ 字节）。

#### 4.2.2 核心流程

加载与调用一条龙：

```text
plugin("foo.wasm", ..)                    # 用户调用
   │  source.load(engine.world) 取字节
   ▼
Plugin::module(bytes)                     # comemo 记忆化：同字节只加载一次
   │  Plugin::new(bytes) → wasmi 编译模块 + 校验导出 memory + 注册 host 导入
   ▼
Plugin::into_module(self)                 # 遍历 wasm 导出的每个 func，生成 PluginFunc
   │  对每个导出函数：scope.bind(name, Func::from(PluginFunc{..}))
   ▼
返回 Module::anonymous(scope)             # 用户拿到一个匿名模块

调用 p.concat(a, b):
   PluginFunc::call(args)                 # #[comemo::memoize]：同参同结果，可能只调一次
   │  self.plugin.call("concat", args)
   ▼
Plugin::call:                             # 从实例池 acquire 一个 PluginInstance
   │  instance.call("concat", args)       # 把参数长度作为 i32 传入，执行 wasm
   │    ├─ 校验签名：参数全 i32、返回单个 i32
   │    ├─ CallData.args = 字节参数；wasm 通过 host 导入函数读走参数
   │    ├─ wasm 计算后调 send_result_to_host 写回结果
   │    └─ 返回码 0=成功 / 1=错误(缓冲区当 UTF-8 错误消息)
   ▼
   实例归还池；返回 Bytes
```

**纯函数约束的来源**（本模块最重要的结论）：它不是一句口号，而是由两处源码机制共同强制——

1. `PluginFunc::call` 标了 `#[comemo::memoize]`：同样 `(plugin, name, args)` 的调用会被 comemo **缓存**，第二次同样入参直接返回旧结果，wasm 函数根本不会再执行。若插件有副作用或对同入参返回不同结果，缓存就会给出「错误但确定」的值。
2. `Plugin` 持有一个 `pool: Mutex<Vec<PluginInstance>>`，多线程并发时会为每个线程**新建独立实例**，实例间不共享内存状态。因此插件里维护的全局变量在不同线程看到的初值可能不同。

#### 4.2.3 源码精读

**入口函数 `plugin()`**——一个标准的 `#[func(scope)]` 原生函数，返回 `Module`：

[src/foundations/plugin.rs:148-156](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L148-L156) —— 加载字节并交给 `Plugin::module`，错误用 `.at(source.span)` 补上源码位置（u5-l3 的 `At` trait）。

```rust
pub fn plugin(
    engine: &mut Engine,
    source: Spanned<DataSource>,
) -> SourceResult<Module> {
    let loaded = source.load(engine.world)?;
    Plugin::module(loaded.data).at(source.span)
}
```

`source` 是 `Spanned<DataSource>`——`DataSource` 是 u11-l1 讲过的「路径 | 字节」统一抽象，所以插件既可来自文件也可来自内联字节。

**纯函数约束的文档与机制**：源码文档把这条约束写得很明确 [src/foundations/plugin.rs:51-66](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L51-L66)：

> Plugin functions *must be pure:* ... if a plugin function is called twice with the same arguments, Typst might cache the results and call your function only once. Moreover, Typst may run multiple instances of your plugin in multiple threads, with no state shared between them.

而「might cache the results」在源码里就是这两个 `#[comemo::memoize]`：

[src/foundations/plugin.rs:220-224](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L220-L224) —— `PluginFunc::call` 被记忆化，同参只真正执行一次。

```rust
#[comemo::memoize]
#[typst_macros::time(name = "call plugin")]
pub fn call(&self, args: Vec<Bytes>) -> StrResult<Bytes> {
    self.plugin.call(&self.name, args)
}
```

**`Plugin` 结构：实例池与快照**——这是「多实例、可过渡」的设计核心：

[src/foundations/plugin.rs:242-257](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L242-L257) —— `base`（共享的编译后模块）、`pool`（实例池）、`snapshot`（新实例的恢复点）、`fingerprint`（区分同基座不同 transition 的兄弟插件）。

```rust
struct Plugin {
    base: Arc<PluginBase>,
    pool: Mutex<Vec<PluginInstance>>,   // 并发时按需扩容
    snapshot: Option<Snapshot>,          // transition 后新实例从此恢复
    fingerprint: u128,                   // 决定 PartialEq/Hash，让 comemo 区分兄弟
}
```

`acquire` 的取用规则 [src/foundations/plugin.rs:355-363](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L355-L363)：池里有就 pop，没有就新建（必要时从 `snapshot` 恢复）。注意源码注释特意说明「先释放锁再建实例」，避免在锁内做重活。

**`into_module`：把 wasm 导出函数变成 Typst 模块**：

[src/foundations/plugin.rs:366-381](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L366-L381) —— 遍历 wasm 模块的所有导出，只挑函数导出，为每个生成一个 `PluginFunc` 绑定到作用域，最后包成**匿名**模块。

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

**协议两端：host 导入函数**。插件（guest）通过导入两个 host 提供的函数与 Typst（host）通信：

[src/foundations/plugin.rs:577-595](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L577-L595) —— `wasm_minimal_protocol_write_args_to_buffer(ptr)`：插件调它，host 把本次调用的所有字节参数依次写进插件内存 `ptr` 起始处。

[src/foundations/plugin.rs:598-612](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L598-L612) —— `wasm_minimal_protocol_send_result_to_host(ptr, len)`：插件调它，host 从插件内存读走 `len` 字节作为返回值（或错误消息）。

调用约定可对照文档 [src/foundations/plugin.rs:89-137](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L89-L137) 归纳成下表：

| 环节 | 约定 |
| --- | --- |
| 函数签名 | 接收 `n` 个 `i32`（=各参数字节长度），返回 1 个 `i32`（状态码） |
| 取参数 | 插件先分配 `a1+a2+...+an` 的缓冲区，再调 `write_args_to_buffer(ptr)` 填入 |
| 回结果 | 调 `send_result_to_host(ptr, len)` |
| 返回码 | `0`=成功；`1`=错误，此时缓冲区按 UTF-8 错误消息解读 |

host 这一侧的执行与状态码解析在 `PluginInstance::call`：

[src/foundations/plugin.rs:447-520](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L447-L520) —— 关键几步：把参数长度装箱成 `Val::I32`（L480-L483）、把字节参数塞进 `CallData.args`（L486）、调 wasm 函数取状态码 `code`（L489-L492）、再按 `code` 是 0/1/其它分支处理（L508-L517）。

```rust
// 返回码语义
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

[src/foundations/plugin.rs:192-201](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L192-L201) —— `plugin.transition(func, ..arguments)` 是 `plugin` 的子作用域函数（`#[scope]`）。

[src/foundations/plugin.rs:328-352](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L328-L352) —— 实现要点：先算新 `fingerprint`（把旧指纹、函数名、参数一起哈希，保证派生插件有确定性身份），执行可变调用，给实例**拍照**（`snapshot`），再把该实例**移动**进新 `Plugin`（注释强调 "this is important!"——原插件因此看不到这次变更）。新指纹让兄弟插件在 `PartialEq`/`Hash` 上可区分，comemo 才不会把它们的调用结果混在一起。

#### 4.2.4 代码实践

**实践目标**：把「纯函数约束」从源码层面说清楚，并定位它的两个强制机制。

**操作步骤（源码阅读型）**：

1. 打开 [src/foundations/plugin.rs:51-66](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L51-L66)，找到文档里「Typst might cache the results」「run multiple instances ... in multiple threads」两句。
2. 分别定位它们的源码落点：
   - 「cache the results」→ `PluginFunc::call` 上的 `#[comemo::memoize]` [src/foundations/plugin.rs:220-224](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L220-L224)。
   - 「multiple instances ... in multiple threads」→ `Plugin.pool` 字段 [src/foundations/plugin.rs:248-249](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L248-L249) 与 `acquire` [src/foundations/plugin.rs:355-363](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L355-L363)。
3. 追一次 host↔guest 数据流：参数如何进 wasm（`write_args_to_buffer`）、结果如何回 host（`send_result_to_host`），对照 `PluginInstance::call` 里参数以 `i32` 长度传入、状态码 0/1 解读的代码。

**需要观察的现象 / 预期结果**：你能用一句话回答「WASM 插件函数的纯函数约束从何而来」——**来自 comemo 记忆化（同参只执行一次）+ 多线程独立实例池（实例间无共享状态）**；Typst「不强制」纯函数性（注释明说 for efficiency reasons），但违反它会导致不可复现的结果。

#### 4.2.5 小练习与答案

**练习 1**：假设一个插件函数 `add(x)` 把 `x` 累加进内部全局变量并返回新值。用 `plugin.transition` 与直接调用两条路分别会发生什么？
**答案**：直接调用 `p.add("a")` 两次，由于 `PluginFunc::call` 被 comemo 记忆化，第二次很可能直接返回缓存结果而不真正执行，且不同线程拿到的实例全局变量初值不同——结果不可预测。正确做法是用 `plugin.transition(p.add, "a")` 得到一个派生模块 `p2`，`p2` 的函数能看到这次累加（因为 transition 把执行过副作用的实例移进了新插件并保存了快照），而原 `p` 保持不变。

**练习 2**：`Plugin` 的 `PartialEq`/`Hash` 为什么把 `fingerprint` 也算进去，而不只比 `base.bytes`？
**答案**：transition 派生出的「兄弟」插件共享同一份 wasm 字节（`base.bytes` 相同），但它们观察到的副作用不同，是**不同的值**。若只比字节，comemo 会把它们当成同一个键，从而把在一处调用的结果错误地复用到另一处。`fingerprint` 把历次 transition 的函数名与参数哈希进去（`hash128(&(self.fingerprint, func, &args))`），让兄弟插件在相等与哈希上可区分，保证记忆化正确。

**练习 3**：插件协议要求 wasm 模块必须导出名为 `memory` 的线性内存，为什么？
**答案**：因为 host 与 guest 之间传参/回结果**没有别的通道**——host 必须能把字节写进插件内存（`write_args_to_buffer`）、从插件内存读字节（`send_result_to_host`）。`Plugin::new` 因此在校验阶段就 `bail!("plugin does not export its memory")`（见 [src/foundations/plugin.rs:278-281](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L278-L281)），没有导出内存的模块根本无法当插件用。

---

### 4.3 symbols / codex：符号表的注入

#### 4.3.1 概念说明

Typst 内置了成百上千个 Unicode 符号（数学符号、emoji 等），分两个命名空间暴露给用户：

- `sym`：通用符号（如 `#sym.arrow.r`）。
- `emoji`：表情符号（如 `#emoji.face.halo`）。

在**数学模式**里，`sym` 里的符号还能省略前缀直接写（`$arrow.r$`、`$alpha$`），这是排版数学公式的核心便利。

这些符号数据**不是手写在 typst-library 里的**。它们由外部 `codex` crate 在构建期从 Unicode 数据生成成两张静态表：

- `codex::ROOT`：根表，迭代产出 `sym`、`emoji` 等**子模块**，注入全局作用域。
- `codex::SYM`：数学符号表，迭代产出**裸符号**，注入数学作用域。

typst-library 这侧只负责「翻译 + 注入」：把 codex 的 `Symbol`/`Module` 定义转成 Typst 的 `Value::Symbol`/`Value::Module`，挂到对应作用域。这种「数据在外部生成、本 crate 只消费」的分工，和本 crate 一贯的「类型在此、行为在彼」（u1-l1/u5-l4）思路一脉相承。

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

那 `math.sym.arrow.r` 为什么也能用？因为**作用域回退**（u2-l3）：数学模式的 `Scopes::get` 找不到 `sym` 时，会回退到标准库全局，而全局里已经有路径 A 注入的 `sym` 模块。所以「裸符号」来自路径 B（codex::SYM 直铺），「`sym.` 前缀」来自路径 A 经回退可见——两路合起来正好对应文档里「sym 模块的元素在数学模式可省略前缀」这句话。

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
   预期三行渲染出同一个右箭头。若无法本地运行，请标注「待本地验证」。

**需要观察的现象 / 预期结果**：你能区分两条来源——裸符号（`$alpha$`）由 `codex::SYM` 经 `define_math` 直铺而来；`sym.` 前缀在数学模式可用，是作用域回退到全局 `sym` 模块的结果。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `define` 要用 `start_category`/`reset_category` 包裹，而 `define_math` 不用？
**答案**：`define` 直接往**全局**作用域注册符号，需要给这批绑定打上 `Category::Symbols` 分类标签（供文档分组、自动补全）。而 `define_math` 往**数学模块内部**注册，整个 `math` 模块在 `math::module()` 里已被 `start_category(Category::Math)`（[src/math/mod.rs:45](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/mod.rs#L45)）盖过分类，数学符号继承 `Math` 分类即可，无需重复。

**练习 2**：codex 给某个符号标注了弃用消息，这条信息是如何传到用户的？
**答案**：`extend_scope_from_codex_module` 检查 `binding.deprecation`，若有就对该绑定调 `scope_binding.deprecated(Deprecation::new().with_message(message))`（[src/symbols.rs:25-27](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/symbols.rs#L25-L27)）。于是用户访问该符号时，编译器会发出带消息的弃用警告。

---

### 4.4 Symbol 类型：变体与修饰符

#### 4.4.1 概念说明

`Symbol` 是符号在运行时的类型（`#[ty]` 注册为一等类型，可用 `symbol(...)` 构造）。它的精妙之处在于「**一个符号名 + 多个变体 + 修饰符选择**」：

- 一个符号（如 `arrow`）可以有多个**变体**（variant），每个变体由「一组修饰符 + 一个字符」组成，例如 `(l, ←)`、`(r, →)`、`(t.quad, ⇑)`。
- 用点号追加修饰符来选变体：`sym.arrow.l`、`sym.arrow.r`、`sym.arrow.t.quad`。修饰符顺序无关，Typst 会挑出「包含所有已加修饰符、且额外修饰符最少」的那个变体（最佳匹配）。

这套机制让一个逻辑符号（arrow）能覆盖它的多种字形（左、右、双向、带杠…），而用户只需用点号组合。修饰符的匹配用 `ModifierSet`（来自 codex）实现，本质是集合运算。

`Symbol` 是值；要把它显示到文档里，靠元素 `SymbolElem`（一个只带 `text: EcoString` 的 `#[required]` 字段的最小元素，名字与 `TextElem` 对齐）。

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
symbol("🗙", ("stamped","🗶"), ..)        # 用户构造
   │  construct() 校验：每个值恰好 1 个 grapheme、修饰符是合法标识符、无重复
   ▼
Symbol::runtime(Box<[Variant<EcoString>]>)
   → 内部存为 Modified { list: List::Runtime(..), modifiers: 空, .. }
```

#### 4.4.3 源码精读

**三种内部形态**——`SymbolInner` 枚举：

[src/foundations/symbol.rs:54-62](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/symbol.rs#L54-L62) —— `Single`（单值静态符号）、`Complex`（多变体静态符号）、`Modified`（已施加修饰符，用 `Arc<Modified>` 共享）。

```rust
enum SymbolInner {
    Single(&'static str),
    Complex(&'static [Variant<&'static str>]),
    Modified(Arc<Modified>),
}
```

`Variant<S>` 是三元组 `(ModifierSet<S>, S, Option<S>)` [src/foundations/symbol.rs:80](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/symbol.rs#L80)：修饰符集合、字符值、可选弃用消息。

**构造器 `single` / `list` / `runtime`**：

[src/foundations/symbol.rs:91-116](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/symbol.rs#L91-L116) —— `single`/`list` 是 `const fn`，用于 codex 静态符号；`runtime` 给用户自定义符号用，内部用 `List::Runtime(Box<[..]>)` 存储堆上的变体表。

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

[src/foundations/symbol.rs:119-130](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/symbol.rs#L119-L130) —— 对 `Complex` 用空修饰符集、对 `Modified` 用已加修饰符集，在变体表里调 `best_match_in` 选最优变体的字符。

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

**追加修饰符 `modified()`——形态升级 + 集合插入**：

[src/foundations/symbol.rs:141-174](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/symbol.rs#L141-L174) —— 若当前是 `Complex`，先升级为 `Modified`（这样后续可叠加多个修饰符）；然后在 `Modified` 分支里 `modifiers.insert_raw(modifier)`，并用 `best_match_in` 查这组修饰符当前命中哪个变体（顺便触发该变体携带的弃用警告，且每个符号只警告一次）。若修饰符加在任何已知变体上都匹配不到，`bail!("unknown symbol modifier")`。

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

[src/foundations/symbol.rs:218-311](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/symbol.rs#L218-L311) —— 接收可变参数（每个是单字符串或 `[修饰符串, 字符]` 二元数组），做四类校验：值必须恰为一个 grapheme cluster（L249-L254）、修饰符必须是合法标识符（L258-L268）、同一变体内修饰符不得重复（L275-L281）、同一组修饰符不得重复出现（用 128 位哈希去重，L284-L301），最后 `Symbol::runtime(list)` 落地（L306-L310）。

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

[src/foundations/symbol.rs:450-455](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/symbol.rs#L450-L455) —— 只有一个 `#[required]` 的 `text: EcoString` 字段，实现 `Repr`（显示为 `[X]`）与 `PlainText`（纯文本导出时输出字符）。

```rust
#[elem(Repr, PlainText)]
pub struct SymbolElem {
    #[required]
    pub text: EcoString, // This is called `text` for consistency with `TextElem`.
}
```

#### 4.4.4 代码实践

**实践目标**：通过自定义符号，亲手走一遍「变体 + 修饰符 + 最佳匹配」。

**操作步骤（源码阅读型 + 可选运行）**：

1. 读 [src/foundations/symbol.rs:218-311](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/symbol.rs#L218-L311)，列出 `construct` 的四类校验，并解释为什么「值必须恰为一个 grapheme」。
2. 读 `get()` [src/foundations/symbol.rs:119-130](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/symbol.rs#L119-L130) 与 `modified()` [src/foundations/symbol.rs:141-174](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/symbol.rs#L141-L174)，写出 `sym.arrow.t.quad` 的解析过程：先 `arrow` 取到 `Complex`，加 `t` 升级为 `Modified{modifiers:{t}}`，再加 `quad` 变成 `{t,quad}`，`best_match_in` 选出同时含 `t` 和 `quad` 的变体。
3. （可选，待本地验证）仿照文档示例自定义一个符号并访问其变体：
   ```typ
   #let envelope = symbol(
     "🖂",
     ("stamped", "🖃"),
     ("stamped.pen", "🖆"),
   )
   #envelope \
   #envelope.stamped \
   #envelope.pen.stamped   // 修饰符顺序无关，应与上一行相同
   ```
   预期最后两行渲染出同一个 ` ۴`。若无法本地运行，标注「待本地验证」。

**需要观察的现象 / 预期结果**：你能解释「修饰符顺序无关」是如何被保证的（构造期 `sort_unstable` + 哈希去重，取值期 `ModifierSet` 集合匹配），并能说明 `Symbol`（值）与 `SymbolElem`（元素）的分工——前者是可被点号修饰的值，后者是把字符送进排版的元素。

#### 4.4.5 小练习与答案

**练习 1**：`SymbolInner::Single` 形态的符号支持修饰符吗？为什么？
**答案**：不支持。`Single` 只有一个字符、没有变体表，`modified()` 里的两个 `if` 分支（`Complex` 升级、`Modified` 插入）都不命中，直接落到 `bail!("unknown symbol modifier")`。只有 `Complex`（多变体）或 `Modified` 形态才能接受修饰符。

**练习 2**：用户写 `symbol("ab")` 会发生什么？为什么？
**答案**：构造校验会报错 `"invalid variant value"`，提示 `"variant value must be exactly one grapheme cluster"`（[src/foundations/symbol.rs:249-254](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/symbol.rs#L249-L254)）。因为一个符号变体必须恰好是一个用户感知的字符（grapheme cluster），`"ab"` 是两个字符，不合法。

**练习 3**：`Symbol` 和 `SymbolElem` 有什么区别？
**答案**：`Symbol` 是**值类型**（`#[ty]`），描述「一个逻辑符号 + 它的变体 + 已加修饰符」，可被点号继续修饰（`arrow.l`），本身不直接参与排版。`SymbolElem` 是**元素**（`#[elem]`），唯一字段是 `text: EcoString`，负责把最终选定的字符送进排版流水线（并参与纯文本导出）。求值/排版阶段会把 `Symbol` 解析出的字符打包成 `SymbolElem` 来渲染。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成一次「从外部注入到符号渲染」的全链路追踪。

**任务**：假设你正在为 typst 写一份内部技术备忘，需要向新同事解释「为什么 `$alpha$` 能渲染出一个希腊字母，而 `#sys.inputs` 又是从哪来的」。请按下列步骤产出一份说明（含调用链与源码行号）：

1. **外部输入侧**：从 CLI 的 `--input` 出发，追到 `LibraryBuilder::with_inputs` [src/lib.rs:206-210](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L206-L210) → `build()` → `global(..., inputs, ...)` → `foundations::define` [src/foundations/mod.rs:121](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/mod.rs#L121) → `sys::module(inputs)` [src/foundations/sys.rs:6-18](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/sys.rs#L6-L18)。说明 `sys.inputs` 为何是只读的。
2. **符号侧**：追 `$alpha$` 的来源——`math::module()` [src/math/mod.rs:43-108](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/mod.rs#L43-L108) 调 `symbols::define_math` [src/symbols.rs:13-15](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/symbols.rs#L13-L15)，后者把 `codex::SYM` 经 `extend_scope_from_codex_module` [src/symbols.rs:17-29](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/symbols.rs#L17-L29) 直铺进数学作用域。说明 codex 的 `Symbol::Single`/`Multi` 如何经 `From<codex::Symbol>` [src/symbols.rs:39-46](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/symbols.rs#L39-L46) 变成 `Symbol::single`/`list`。
3. **插件对照**：作为对照，简述 `plugin("foo.wasm")` 如何经 `Plugin::module` → `into_module` [src/foundations/plugin.rs:366-381](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L366-L381) 把 wasm 导出函数变成模块；并指出 `PluginFunc::call` 上的 `#[comemo::memoize]` [src/foundations/plugin.rs:220-224](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/plugin.rs#L220-L224) 是纯函数约束的源头。
4. **产出**：画一张包含「宿主 → LibraryBuilder → global()/math::module() → sys / sym / plugin」的数据流图，并在每个节点标注源码位置。

**预期结果**：你能用一条连贯的故事线，把「外部数据/外部代码如何进入标准库」讲清楚，并区分三种不同的进入方式——`sys` 是装配期注入的常量、`sym`/`emoji` 是构建期生成的静态表、`plugin` 是运行期按需加载的 WASM 模块。

## 6. 本讲小结

- `sys` 模块只暴露 `version`（编译器版本）与 `inputs`（宿主注入的字典），二者都在标准库**装配期**定死，运行期只读；`inputs` 经 `LibraryBuilder::with_inputs` → `build()` → `foundations::define` → `sys::module` 流入。
- `plugin()` 把一段 WASM 字节加载成匿名 `Module`，每个 wasm 导出函数变成一个 `PluginFunc`；host 与 guest 通过两个导入函数（`write_args_to_buffer`/`send_result_to_host`）经线性内存交换字节，状态码 0/1 区分成功/错误。
- **插件纯函数约束**由两处源码机制强制：`PluginFunc::call` 的 `#[comemo::memoize]`（同参只执行一次）+ `Plugin.pool` 多线程实例池（实例间无共享状态）；transition API 是在纯函数世界里做受控变更的官方出口。
- 符号数据由外部 `codex` crate 构建期生成：`codex::ROOT` 经 `symbols::define` 注入全局（产出 `sym`/`emoji` 子模块），`codex::SYM` 经 `symbols::define_math` 直铺进数学作用域（产出裸符号）；`math.sym` 可用则是作用域回退到全局的结果。
- `Symbol` 运行时类型有 `Single`/`Complex`/`Modified` 三种内部形态，修饰符用 `ModifierSet` 集合匹配选最佳变体（顺序无关、构造期规范化去重）；`Symbol` 是值，`SymbolElem` 才是把字符送进排版的元素。

## 7. 下一步学习建议

- **u11-l3（可视化）**：颜色/描边/形状/图像是另一组「面向用户」的标准库内容，同样遵循「类型在此、行为在彼」，可与本讲的符号系统对照阅读。
- **u12-l2（性能与并发）**：本讲反复出现的 `#[comemo::memoize]`、`Arc`、`Mutex`、实例池，正是 Typst 性能手段的实例；那一讲会系统讲解 comemo tracked、`LazyHash`、`singleton!` 等。
- **深入 codex**：若你对符号表如何从 Unicode 数据生成感兴趣，可阅读 workspace 里的 `codex` crate 源码，理解它如何产出 `ROOT`/`SYM` 与 `ModifierSet`、`Variant` 等类型。
- **WASM 协议实战**：参考文档里给出的 [wasm-minimal-protocol 仓库](https://github.com/typst-community/wasm-minimal-protocol)，亲手用 Rust 写一个最小插件（导出 `memory`、实现两步协议），再用 `plugin(...)` 加载并调用，验证本讲讲的协议与纯函数约束。
