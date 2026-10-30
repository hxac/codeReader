# 插件系统：WASM 插件的加载与调用

## 1. 本讲目标

Typst 的标准库是固定的：`upper`、`range`、`luma` 这些函数都编译在编译器里。如果你需要标准库给不了的计算能力——比如对大数组做数值拟合、调用某个只存在于 Rust 生态的库——就只能寄希望于一种受控的扩展机制。这个机制就是**插件（plugin）**：把任意语言编译成 WebAssembly 字节码，在编译器进程内的沙箱里运行，通过**字节缓冲区**交换数据。

学完本讲，你应该能够：

1. 完整描述一次插件调用的路径：从脚本里的 `p.hello()` 出发，经过 `typst-eval` 的通用函数调用、`FuncInner::Plugin` 的派发，最终进入 wasmi 解释器执行 WASM 字节码；
2. 讲清楚「字节缓冲区协议」的每一个细节——为什么参数传的是**长度**而不是指针、两个宿主导入函数各自做什么、返回码 0/1 的语义；
3. 说出插件系统在安全上的三道边界：WASM 沙箱隔离、确定性保证、实例池与快照隔离，以及为什么插件函数**必须是纯函数**；
4. 独立用 Rust 写出一个最小 WASM 插件，编译后在 Typst 文档中加载并调用。

一个先行的更正：manifest 里把 `crates/typst-eval/src/lib.rs` 列为本讲关键源码，实际核查后发现 `typst-eval` 中**没有任何** plugin 相关代码——插件调用的派发完全位于 `typst-library` 的 `Func::call_impl` 内（见 4.2.3）。求值器只是经通用的函数调用路径「路过」插件函数。本讲会按真实代码讲解。

## 2. 前置知识

### WebAssembly（WASM）最小概念集

- **模块（module）**：一个 `.wasm` 文件就是一份模块，包含编译后的代码与声明（哪些内存、哪些函数对外可见）。
- **实例（instance）**：模块被加载、链接、启动后的"运行体"。同一个模块可以实例化多次，彼此状态独立——这是本讲 4.3 实例池的基础。
- **线性内存（linear memory）**：WASM 唯一的内存形态，一块连续字节，按 64 KiB 的"页"分配。WASM 代码不能触碰内存之外的任何宿主资源，指针只是这块内存里的 32 位偏移。
- **导入 / 导出（imports / exports）**：模块可以声明"我需要宿主提供哪些函数"（导入），也可以声明"我对外提供哪些函数和内存"（导出）。插件协议正是围绕这两者规定的。

### wasmi：解释器型运行时

Typst 没有把 WASM 编译成机器码（JIT），而是用 [wasmi](https://github.com/wasmi-labs/wasmi)（workspace 依赖版本 1.0.9，见 [Cargo.toml:L151](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/Cargo.toml#L151)）**解释执行**字节码。解释器慢于原生代码，但实现简单、行为完全可预测——这对编译结果的**可复现性**至关重要（PDF 输出必须只由输入决定）。

### 承接前几讲的两个概念

- **`Func` 的五形态**（u3-l5）：Typst 里所有函数值统一装在 `Func` 里，内部区分原生、元素、闭包、插件、`with` 绑定五种形态。插件函数就是其中之一。
- **comemo 记忆化**（u5-l8）：以「实际读取集」为缓存键的增量机制。插件的加载与调用都被 `#[comemo::memoize]` 包裹——这直接解释了「插件函数必须是纯函数」这条铁律的动机。

### 为什么是「字节缓冲区」

字节是所有语言、所有序列化格式的公共底线。用 JSON 就绑死了 schema，用某个 RPC 框架就绑死了生态；而 `bytes` 让插件作者自己决定上层协议（官方文档的建议是：写 Typst 侧的包装函数做 `str`/`bytes` 转换）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/typst-library/src/foundations/plugin.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs) | 本讲主战场：`plugin()` 入口、`Plugin`/`PluginInstance`/`CallData`、两个宿主函数、协议文档 |
| [crates/typst-library/src/foundations/func.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/func.rs) | `FuncInner::Plugin` 形态与 `call_impl` 的插件派发分支 |
| [crates/typst-library/src/loading/mod.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/loading/mod.rs) | `DataSource`（路径或字节）与 `Load` trait，`plugin()` 靠它拿到 wasm 字节 |
| [crates/typst-eval/src/call.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-eval/src/call.rs) | 求值器的通用函数调用点，插件调用的"必经之路" |
| [tests/suite/foundations/plugin.typ](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/suite/foundations/plugin.typ) | 10 个插件测试：正常调用、import、transition、五类错误 |
| [tests/src/world.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/src/world.rs) | 测试世界里 `/assets/...` 路径如何解析到 `typst-dev-assets` |
| [Cargo.toml](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/Cargo.toml) | workspace 依赖表中的 `wasmi` 条目 |

## 4. 核心概念与源码讲解

### 4.1 模块一：Plugin 加载协议

#### 4.1.1 概念说明

「加载」要回答三个问题：

1. **wasm 字节从哪来？** —— 路径（经 `World::file`）或直接传 `bytes` 值，统一抽象为 `DataSource`。
2. **什么样的模块算合法插件？** —— 必须导出名为 `memory` 的线性内存（否则宿主无法与它交换字节）；函数导出在**调用时**才校验签名（因为像 `_initialize` 这类启动函数不符合协议但也无害）。
3. **加载产物是什么？** —— 一个 `Module`。这是 Typst 的模块值（和 `#import` 一个 `.typ` 文件得到的同一种值），作用域里每个 WASM 函数导出都对应一个可调用的 `function` 值。正因为产物是模块，`#import plugin("hello.wasm"): hello` 这种语法才成立。

#### 4.1.2 核心流程

```text
plugin("hello.wasm")                     脚本层调用
  │
  ▼ plugin()  [plugin.rs L148-156]
  │  source.load(engine.world)
  │     ├─ DataSource::Path → world.file(id) → Bytes
  │     └─ DataSource::Bytes → 直接使用
  ▼ Plugin::module(bytes)   [comemo::memoize，同一字节只加载一次]
  │
  ▼ Plugin::new(bytes)     [L267-308]
  │  1. wasmi::Config：关闭 relaxed SIMD（保证确定性）
  │  2. wasmi::Module::new：校验并编译字节码
  │  3. 检查导出 "memory" 必须存在且为内存类型
  │  4. Linker 注册两个宿主函数（typst_env 模块）
  │  5. 实例化第一个 PluginInstance 放入池
  │
  ▼ Plugin::into_module()  [L365-381]
     遍历所有函数导出 → 逐个包装成 PluginFunc → 绑定进匿名模块作用域
     返回 Module
```

#### 4.1.3 源码精读

**入口函数 `plugin()`**——一个再普通不过的 `#[func]` 原生函数，声明了 `scope`（所以它有 `plugin.transition` 子函数）：

[crates/typst-library/src/foundations/plugin.rs:L148-L156](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L148-L156)

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

加载就是一行 `source.load(engine.world)`；错误用 `.at(source.span)` 挂上调用点的 Span 变成诊断。它被注册进全局作用域的位置在 [crates/typst-library/src/foundations/mod.rs:L118](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/mod.rs#L118)（`global.define_func::<plugin>()`）。

**`DataSource` 的两种形态**：

[crates/typst-library/src/loading/mod.rs:L46-L53](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/loading/mod.rs#L46-L53)

```rust
pub enum DataSource {
    /// A path to a file.
    Path(PathOrStr),
    /// Raw bytes.
    Bytes(Bytes),
}
```

路径形态的实现在 [loading/mod.rs:L82-L112](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/loading/mod.rs#L82-L112)：先 `path.resolve` 成绝对路径（挂到当前文件的 FileId 下），再调 `world.file(...)` 读字节；顺带对 `http://`/`https://` 开头的"路径"给出「不支持网络访问」的提示。这意味着插件文件与 `.typ` 文件一样走 World 契约——编译器内核仍然不知道文件从哪来。

**构造 `Plugin`**：

[crates/typst-library/src/foundations/plugin.rs:L267-L308](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L267-L308)

```rust
fn new(bytes: Bytes) -> StrResult<Self> {
    let mut config = wasmi::Config::default();
    // Disable relaxed SIMD as it can introduce non-determinism.
    config.wasm_relaxed_simd(false);
    ...
    // Ensure that the plugin exports its memory.
    if !matches!(module.get_export("memory"), Some(wasmi::ExternType::Memory(_))) {
        bail!("plugin does not export its memory");
    }
    let mut linker = wasmi::Linker::new(&engine);
    linker.func_wrap("typst_env", "wasm_minimal_protocol_send_result_to_host", ...)
    linker.func_wrap("typst_env", "wasm_minimal_protocol_write_args_to_buffer", ...)
```

四步：配引擎、编译模块、**强制要求导出 `memory`**、把两个宿主函数挂进 `typst_env` 导入模块。注意 `Plugin::module` 本身带 `#[comemo::memoize]`（[L261-L265](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L261-L265)）——同一份字节重复 `plugin(...)` 只编译一次。

**变成 Typst 模块**：

[crates/typst-library/src/foundations/plugin.rs:L365-L381](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L365-L381)

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

遍历**所有**函数导出，一视同仁地包装成 `PluginFunc`（`Arc<Plugin>` + 函数名）绑定进作用域。注意此刻**不校验签名**——签名检查被推迟到调用时，注释解释了原因：像 `_initialize` 这样的导出不符合协议但也不该让加载失败。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `plugin()` 返回模块、每个函数导出都可调用，并验证测试用的 wasm 文件从哪来。

**操作步骤**：

1. 仓库测试套件自带一组插件资产。先看它们如何被解析：`tests/suite/foundations/plugin.typ` 里写的是 `plugin("/assets/plugins/hello.wasm")`，而 `/assets/` 前缀在测试世界里有特殊处理——[tests/src/world.rs:L191-L200](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/src/world.rs#L191-L200) 中 `TestFiles::load` 把 `assets/` 前缀的路径转交给 `typst_dev_assets::get(...)`（一个独立的 git 依赖仓库，见根 [Cargo.toml:L37](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/Cargo.toml#L37)）。**仓库里没有 `tests/assets/` 目录，wasm 文件全部来自 typst-dev-assets。**
2. 运行前三个加载相关测试（`cargo testit` 的第一个位置参数是正则，见 [tests/src/args.rs:L17-L19](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/src/args.rs#L17-L19)）：

   ```bash
   cargo testit plugin
   ```

   这会匹配 `tests/suite/foundations/plugin.typ` 中全部 10 个测试。
3. 阅读测试 [plugin.typ:L3-L21](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/suite/foundations/plugin.typ#L3-L21)：`plugin-basic` 验证调用结果，`plugin-func` 验证 `type(p.hello) == function`、还能把插件函数传给 `map` 高阶使用，`plugin-import` 验证 `#import plugin(...): hello, double_it` 语法。
4.（可选，待本地验证）把 `plugin-basic` 复制一份改为不存在的路径 `plugin("/assets/plugins/nope.wasm")`，再跑一次，观察报错是「file not found」类诊断而非 panic。

**需要观察的现象**：`plugin-basic` 中 `p.hello()` 返回 `bytes("Hello from wasm!!!")`——返回值类型是 `bytes` 不是 `str`，脚本侧要再包一层 `str(...)` 才能当文本用。

**预期结果**：10 个测试全部通过；理解了「模块 = 每个函数导出一个绑定」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `plugin()` 强制检查模块导出 `memory`，而不是等调用出错了再说？

**答案**：宿主与插件之间所有的数据交换（写参数、读结果）都发生在插件的线性内存里（见 4.2 的两个宿主函数，都通过 `caller.get_export("memory")` 拿内存句柄）。没有导出内存的模块根本无法参与协议，早失败能把错误定位到加载点，而不是每次调用都报一个费解的错误。

**练习 2**：`plugin()` 为什么能和 `#import` 语法配合（`#import plugin("hello.wasm"): hello`）？

**答案**：`#import ...: names` 的语义是"从模块值里取绑定"。`plugin()` 返回的正是 `Module`（`Module::anonymous(scope)`），作用域里的每个绑定对应一个 WASM 函数导出，所以 import 直接可用。

**练习 3**：`Plugin::new` 里 `config.wasm_relaxed_simd(false)` 注释说 relaxed SIMD 会引入非确定性。结合本手册哪一讲的内容解释为什么 Typst 特别在意这一点？

**答案**：u5-l8 讲过，comemo 以输入的哈希为键缓存结果、watch 场景下要求两次编译产出完全一致；更根本地，同一份 `.typ` 在任何机器上应编译出字节级相同的 PDF。relaxed SIMD 允许同一条指令在不同 CPU 上产生（轻微）不同的浮点结果，会破坏可复现性，所以关闭。

### 4.2 模块二：调用与序列化

#### 4.2.1 概念说明

这是整个插件系统最精巧的部分，回答"一次调用如何跨越 Rust 与 WASM 的边界"。协议（完整定义在 [plugin.rs:L89-L137](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L89-L137) 的文档注释里）可以浓缩成三条：

1. **参数传长度，不传指针**。导出函数的签名被规定为「`n` 个 32 位整数参数 + 返回一个 32 位整数」。调用时宿主把每个字节缓冲区的**长度**作为 i32 实参传进去——插件由此知道自己要分配多大的缓冲区。指针没法直接传，因为宿主不知道插件内存的布局，让插件自己分配再回传指针才安全。
2. **两个宿主导入函数完成字节搬运**：
   - `wasm_minimal_protocol_write_args_to_buffer(ptr)`：插件分配好总长缓冲区后调用它，宿主把所有参数依次写入该地址；
   - `wasm_minimal_protocol_send_result_to_host(ptr, len)`：插件算完后调用它，宿主从该地址读走 `len` 字节作为返回值。
3. **返回码即成功/错误信号**：WASM 函数返回 `0` 表示成功（缓冲区内容是结果）；返回 `1` 表示失败（缓冲区内容被解释为 UTF-8 错误消息）；其他值都算「不遵守协议」。

Rust 侧的中转站是 `CallData`——挂在 wasmi `Store` 上的宿主数据，三个字段：待写入的参数、已读出的结果、以及可能发生的越界错误记录。宿主函数与调用方之间不直接传值，全靠它接力。

另外注意脚本侧的**类型契约**：`Func::call_impl` 的插件分支用 `args.all::<Bytes>()` 消费参数，`Bytes` 的 cast 是恒等转换（`#[ty(scope, cast, ...)]`，见 [bytes.rs:L42-L46](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/bytes.rs#L42-L46)），所以**每个实参都必须是 `bytes` 类型的值**——传字符串要显式 `bytes("...")`，传 `true`/`10` 会在 cast 阶段就报「expected bytes, found boolean/integer」（测试 [plugin.typ:L49-L54](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/suite/foundations/plugin.typ#L49-L54) 验证了这一点）。

#### 4.2.2 核心流程

一次 `p.double_it(bytes("hey!"))` 的完整时序：

```text
脚本层:  p.double_it(bytes("hey!"))
   │
   ▼ typst-eval: call_func                    [call.rs L167-169]
   │   func.call(&mut vm.engine, vm.context, args, span)
   ▼ typst-library: Func::call_impl 匹配 Plugin 形态   [func.rs L422-427]
   │   inputs = args.all::<Bytes>()?            // 全部实参转 bytes
   │   output = func.call(inputs)               // StrResult，附上 args.span
   │   返回 Value::Bytes(output)
   ▼ PluginFunc::call   [comemo::memoize]      [plugin.rs L219-L224]
   ▼ Plugin::call       从实例池取一个实例        [plugin.rs L310-324]
   ▼ PluginInstance::call                      [plugin.rs L446-L520]
   │   ① 惰性校验签名：参数全为 i32、恰好返回一个 i32
   │   ② 校验个数：i32 参数个数 == 字节缓冲区个数
   │   ③ lengths = [每个缓冲区的长度]
   │   ④ CallData.args = 字节参数（暂存宿主侧）
   │   ⑤ handle.call(lengths, &mut code)   ──进入 WASM──┐
   │   ┌──────────────────────────────────────────────┐ │
   │   │ 插件侧: 分配 len₁+len₂+... 的缓冲区 buf        │ │
   │   │   write_args_to_buffer(buf.ptr)  ◄────────────┼─┘ 宿主写入 L576-595
   │   │   ……用参数计算，生成 result……                 │
   │   │   send_result_to_host(res.ptr, res.len) ◄─────┼─── 宿主读出 L597-612
   │   │   return 0 / 1                              │ │
   │   └──────────────────────────────────────────────┘ │
   │   ⑥ 检查 CallData.memory_error（越界读写 → 诊断）
   │   ⑦ code==0 → Ok(Bytes(output))
   │      code==1 → "plugin errored with: {UTF-8 消息}"
   │      其他   → "plugin did not respect the protocol"
   ▼ 实例归还池中，Value::Bytes 一路上浮回脚本层
```

#### 4.2.3 源码精读

**求值器的"路过"点**——`typst-eval` 对插件一无所知，只是通用调用：

[crates/typst-eval/src/call.rs:L167-L169](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-eval/src/call.rs#L167-L169)

```rust
fn call_func(vm: &mut Vm, func: Func, args: Args, span: Span) -> SourceResult<Value> {
    let func = func.spanned(span);
    let f = || func.call(&mut vm.engine, vm.context, args, span);
```

**派发分支**——`Func` 的内部表示里有专门的插件形态：

[crates/typst-library/src/foundations/func.rs:L150-L163](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/func.rs#L150-L163) 定义了 `FuncInner::Plugin(Arc<PluginFunc>)`；调用时在 [func.rs:L422-L427](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/func.rs#L422-L427) 派发：

```rust
FuncInner::Plugin(func) => {
    let inputs = args.all::<Bytes>()?;
    let output = func.call(inputs).at(args.span)?;
    args.finish()?;
    Ok(Value::Bytes(output))
}
```

四行就是脚本世界的全部接口：实参全部转成 `bytes`、调用、`finish()` 兜底报多余参数、结果包成 `Value::Bytes`。

**实例调用**（协议的宿主侧核心）：

[crates/typst-library/src/foundations/plugin.rs:L446-L477](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L446-L477)

```rust
fn call(&mut self, func: &str, args: Vec<Bytes>) -> StrResult<Bytes> {
    ...
    // Check function signature. Do this lazily only when a function is called
    // because there might be exported functions like `_initialize` ...
    if ty.params().iter().any(|&v| v != wasmi::ValType::I32) {
        bail!("plugin function `{func}` has a parameter that is not a 32-bit integer");
    }
    if ty.results() != [wasmi::ValType::I32] {
        bail!("plugin function `{func}` does not return exactly one 32-bit integer");
    }
    ...
    if expected != given {
        bail!("plugin function takes {expected} argument{}, but {given} {} given", ...);
```

[plugin.rs:L479-L492](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L479-L492) 把长度装箱并真正进入 WASM：

```rust
// Collect the lengths of the argument buffers.
let lengths = args.iter().map(|a| wasmi::Val::I32(a.len() as i32)).collect::<Vec<_>>();
// Store the input data.
self.store.data_mut().args = args;
// Call the function.
let mut code = wasmi::Val::I32(-1);
handle.call(&mut self.store, &lengths, std::slice::from_mut(&mut code))
    .map_err(|err| eco_format!("plugin panicked: {err}"))?;
```

注意 `-1` 作为 code 初值：如果 WASM 陷阱（trap）导致函数没返回，`map_err` 先报 "plugin panicked"；正常返回后 code 才是插件设置的 0/1。

**返回码解释**：

[plugin.rs:L507-L517](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L507-L517)

```rust
match code {
    wasmi::Val::I32(0) => {}
    wasmi::Val::I32(1) => match std::str::from_utf8(&output) {
        Ok(message) => bail!("plugin errored with: {message}"),
        Err(_) => bail!("plugin errored, but did not return a valid error message"),
    },
    _ => bail!("plugin did not respect the protocol"),
}
```

**两个宿主函数**——字节搬运的全部实现，都短得可以整段读完：

[crates/typst-library/src/foundations/plugin.rs:L576-L595](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L576-L595) 把参数依次写进插件内存：

```rust
fn wasm_minimal_protocol_write_args_to_buffer(mut caller: wasmi::Caller<CallData>, ptr: u32) {
    let memory = caller.get_export("memory").unwrap().into_memory().unwrap();
    let arguments = std::mem::take(&mut caller.data_mut().args);
    let mut offset = ptr as usize;
    for arg in arguments {
        if memory.write(&mut caller, offset, arg.as_slice()).is_err() {
            caller.data_mut().memory_error = Some(MemoryError { offset: offset as u32, .. });
            return;
        }
        offset += arg.len();
    }
}
```

[plugin.rs:L597-L612](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L597-L612) 从插件内存读出结果：

```rust
fn wasm_minimal_protocol_send_result_to_host(mut caller: wasmi::Caller<CallData>, ptr: u32, len: u32) {
    let memory = caller.get_export("memory").unwrap().into_memory().unwrap();
    let mut buffer = std::mem::take(&mut caller.data_mut().output);
    buffer.resize(len as usize, 0);
    if memory.read(&caller, ptr as usize, &mut buffer).is_err() {
        caller.data_mut().memory_error = Some(MemoryError { offset: ptr, length: len, write: false });
        return;
    }
    caller.data_mut().output = buffer;
}
```

两者都不返回 `Result` 给 WASM——失败时只把 `MemoryError` 记进 `CallData`（[plugin.rs:L557-L574](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L557-L574)），等调用返回后由 [plugin.rs:L494-L502](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L494-L502) 统一翻译成「plugin tried to read/write out of bounds: pointer 0x… is out of bounds …」。

#### 4.2.4 代码实践

**实践目标**：用测试资产把协议的每条错误分支和源码一一对应，验证你对返回码的理解。

**操作步骤**：

1. 打开 [tests/suite/foundations/plugin.typ:L43-L78](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/suite/foundations/plugin.typ#L43-L78)，这是五个"诊断型测试"（`// Error:` 注解声明期望的错误消息，这是 u1-l5 讲过的三类测试之一）。
2. 逐条把期望错误消息映射回源码分支：

   | 测试节 | 期望错误 | 源码位置 |
   | --- | --- | --- |
   | `plugin-wrong-number-of-arguments` | `plugin function takes 0 arguments, but 1 was given` | [plugin.rs:L471-L477](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L471-L477) |
   | `plugin-wrong-argument-type` | `expected bytes, found boolean/integer` | func.rs 的 `args.all::<Bytes>()`（cast 层） |
   | `plugin-error` | `plugin errored with: This is an \`Err\`` | [plugin.rs:L510-L511](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L510-L511)（code==1 分支） |
   | `plugin-panic` | `plugin panicked: wasm \`unreachable\` instruction executed` | [plugin.rs:L490-L492](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L490-L492) |
   | `plugin-out-of-bounds-read/write` | `plugin tried to read/write out of bounds: pointer 0x40000000 …` | [plugin.rs:L494-L502](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L494-L502) |

3. 运行验证（诊断型测试通过 = 实际错误与注解逐字符一致）：

   ```bash
   cargo testit plugin-wrong plugin-error plugin-panic plugin-out-of-bounds
   ```

   （多个位置参数是"或"关系的正则过滤，待本地验证精确输出。）

**需要观察的现象**：`plugin-panic` 的消息里带 wasm 陷阱原因（`unreachable` 指令），说明陷阱发生在插件字节码内部而非宿主函数里；`plugin-out-of-bounds-*` 的指针是 `0x40000000`（1 GiB）——插件故意用一个远超内存大小的地址调用宿主函数。

**预期结果**：所有测试通过；五类错误消息都能在源码里找到唯一的产生点。

#### 4.2.5 小练习与答案

**练习 1**：协议为什么规定「函数参数是各缓冲区的**长度**」而不是「各缓冲区的指针」？

**答案**：调用发生前，参数数据在宿主（Rust）侧，宿主无法把 Rust 指针写进 WASM 的线性内存语义里；而插件内存的分配权在插件（`Vec::with_capacity` 等），只有它知道自己能把缓冲区放在哪。所以协议让宿主先传长度，插件据此分配缓冲区、把地址通过 `write_args_to_buffer(ptr)` 告诉宿主，宿主再把数据写过去——指针永远由内存的拥有方提供。

**练习 2**：`PluginInstance::call` 里为什么要把 code 初始化为 `-1` 而不是 `0`？

**答案**：`0` 是"成功"的返回码。若初始化为 0，当 WASM 调用因陷阱中断、没来得及写返回值时，宿主会误判为成功并返回（可能不完整的）output。`-1` 既不是 0 也不是 1，一旦调用异常中断且 `map_err` 未捕获（理论上 `call` 出错会走 `map_err` 返回 Err），残留的 -1 会落入 `_ => bail!("plugin did not respect the protocol")` 分支，宁可疑罪从有也不误报成功。

**练习 3**：`p.shuffle(a, b, c)` 返回 `value3-value1-value2`（见 [plugin.typ:L7-L10](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/suite/foundations/plugin.typ#L7-L10)）。请推断插件函数收到的 i32 参数是什么，`write_args_to_buffer` 之后缓冲区长什么样。

**答案**：i32 参数是三个缓冲区的长度，即 `"value1"`、`"value2"`、`"value3"` 各 6。`write_args_to_buffer` 把三者首尾相接写进插件分配的 18 字节缓冲区：`value1value2value3`；插件再按 6/6/6 切开、重排成 `value3-value1-value2`，经 `send_result_to_host` 送回。

### 4.3 模块三：安全边界

#### 4.3.1 概念说明

插件是**编译进文档的任意代码**——你 `#import` 一个别人的 Typst 包，那个包可能加载了你不认识的 wasm。Typst 用三道相互独立的边界来兜底：

1. **能力沙箱（capability sandbox）**：WASM 模块拿不到任何宿主能力。链接器里只注册了 `typst_env` 的两个数据搬运函数（[plugin.rs:L283-L297](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L283-L297)），没有 `fd_read`、没有时钟、没有随机数、没有网络。官方文档明说：「printing, reading files, or similar things are not supported」（[plugin.rs:L28-L29](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L28-L29)）。这也解释了为什么**默认的 WASI 目标不能直接用**——WASI 程序一启动就要导入一堆系统调用，链接不上（文档 [plugin.rs:L82-L87](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L82-L87) 给出了两条出路：换目标或用 wasi-stub 打桩）。
2. **纯度要求（purity）**：Typst 函数必须纯（这是 u3 讲过的语言级设计），插件函数被 Typst 函数调用，也就继承了这条要求。Typst **不强制**检查（为了效率），而是把 `PluginFunc::call` 用 `#[comemo::memoize]` 缓存、把实例池多线程复用——同一个插件可能在多个线程各跑一个实例、无共享状态。如果你写了有副作用的插件函数，缓存会让第二次调用返回旧值，结果"不可预测且不可复现"（[plugin.rs:L51-L66](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L51-L66)）。
3. **状态隔离与 transition API**：有些插件确实需要昂贵的初始化（如建索引表）——一次性、确定性的初始化不违反纯度。为此 0.13 起提供了 `plugin.transition(func, args..)`：在**一个实例上**执行这次有副作用的调用，然后快照其内存，派生出一个"能看到副作用"的新插件；原插件保持 untouched。派生插件用 `fingerprint`（把历次 transition 的函数名与参数哈希进一个 u128）保证相等性与哈希的确定性——两次相同的 transition 序列得到的派生插件 `==` 为真，这就是测试里 `hello == hello2` 为 `true`、`hello == world` 为 `false` 的来源。

另外还有一条工程上的边界：**损坏实例即弃**。调用一旦失败，实例不归还池（[plugin.rs:L315-L317](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L315-L317) 注释："it might be irrecoverably damaged"），因为插件可能已把内存改得面目全非。

#### 4.3.2 核心流程

transition 的派生流程：

```text
plugin.transition(base.add, bytes("hello"))
   │
   ▼ plugin.transition()   [plugin.rs L192-201]
   │   参数: func: PluginFunc + variadic Vec<Bytes>
   ▼ PluginFunc::transition  [comemo::memoize]  [plugin.rs L226-231]
   ▼ Plugin::transition     [plugin.rs L326-352]
   │   ① fingerprint = hash128((旧指纹, 函数名, 参数))   ← 链式累积
   │   ② 从池中取实例，执行这次可变调用（失败即弃实例）
   │   ③ snapshot = 实例内存快照（页数 + 全部字节）
   │   ④ 构造新 Plugin：
   │        base     = 同一个 Arc<PluginBase>（同一份字节码）
   │        snapshot = ③ 的快照
   │        pool     = [刚用过的那个实例]   ← move，原插件看不见它
   │        fingerprint = ①
   ▼ 之后再从新 Plugin 的池里补建实例时
       PluginInstance::new 会先常规实例化、再 restore(snapshot)
       [plugin.rs L428-L444 / L532-L545]
```

#### 4.3.3 源码精读

**纯度与并发的文档化契约**：

[crates/typst-library/src/foundations/plugin.rs:L52-L66](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L52-L66)

> Plugin functions *must be pure:* … In particular, if a plugin function is called twice with the same arguments, Typst might cache the results and call your function only once. Moreover, Typst may run multiple instances of your plugin in multiple threads, with no state shared between them.

对应实现是 `PluginFunc` 上的两个 memoize 标注：

[plugin.rs:L219-L231](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L219-L231)

```rust
#[comemo::memoize]
#[typst_macros::time(name = "call plugin")]
pub fn call(&self, args: Vec<Bytes>) -> StrResult<Bytes> { ... }

#[comemo::memoize]
#[typst_macros::time(name = "transition plugin")]
pub fn transition(&self, args: Vec<Bytes>) -> StrResult<Module> { ... }
```

**实例池：借—用—还（或弃）**：

[plugin.rs:L310-L324](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L310-L324)

```rust
fn call(&self, func: &str, args: Vec<Bytes>) -> StrResult<Bytes> {
    let mut instance = self.acquire()?;
    // If the call fails, we return early and _don't_ return the instance
    // to the pool as it might be irrecoverably damaged.
    let output = instance.call(func, args)?;
    self.pool.lock().unwrap().push(instance);
    Ok(output)
}
```

`acquire`（[L354-L363](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L354-L363)）池空时新开实例（必要时从快照恢复），注释特意说明"先释放锁再建实例"。`Plugin` 的字段定义在 [L240-L257](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L240-L257)，`fingerprint` 的文档注释点明它用于区分"同字节码、不同 transition 历史"的兄弟插件。

**快照与恢复**——transition 的物理基础就是"把线性内存整个抄下来"：

[plugin.rs:L520-L545](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L520-L545)

```rust
fn snapshot(&self) -> Snapshot {
    let memory = self.memory();
    let mem_pages = memory.size(&self.store);
    let mem_data = memory.data(&self.store).to_vec();
    Snapshot { mem_pages, mem_data }
}

fn restore(&mut self, snapshot: &Snapshot) {
    let memory = self.memory();
    ... // 先按需 grow 到快照页数，再整块拷回内存数据
}
```

文档同时坦承了限制（[plugin.rs:L177-L183](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L177-L183)）：transition 只保证反映**内存**的变化，不保证 WASM **全局变量**的变化。

**确定性身份**：派生插件的相等性/哈希只看 `bytes + fingerprint`：

[plugin.rs:L389-L400](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L389-L400)

```rust
impl PartialEq for Plugin {
    fn eq(&self, other: &Self) -> bool {
        self.base.bytes == other.base.bytes && self.fingerprint == other.fingerprint
    }
}
```

测试 [plugin.typ:L39-L41](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/suite/foundations/plugin.typ#L39-L41) 正是对这条语义的行为验证。

**越界访问的防护**：两个宿主函数对每次内存读写都检查结果（见 4.2.3），失败时记录 `MemoryError`，调用收尾时翻译成带指针值的诊断（[plugin.rs:L494-L502](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L494-L502)）。wasmi 本身也会在插件访问自己内存越界时陷阱，双层保险。

#### 4.3.4 代码实践

**实践目标**：验证 transition 的"派生可见、原始不受影响"语义与指纹相等性。

**操作步骤**：

1. 运行现成测试：

   ```bash
   cargo testit plugin-transition
   ```

2. 对照阅读 [plugin.typ:L23-L41](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/suite/foundations/plugin.typ#L23-L41)：`empty.add("hello")` 派生出 `hello`，此后 `empty.get()` 仍是 `"[]"`、`hello.get()` 是 `"[hello]"`；从 `hello` 再 transition 出 `hello_you`，三者互不干扰；最后 `hello2` 走了与 `hello` 完全相同的 transition 序列，断言 `hello == hello2`。
3.（源码阅读型实践）在纸上为这四个模块值标注 `(bytes, fingerprint)` 二元组：`empty` 是 `(b, 0)`（初始指纹 0，见 [plugin.rs:L305](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L305)）；`hello`/`hello2` 是 `(b, h(0,"add",["hello"]))`；`hello_you` 是 `(b, h(h(0,…),"add",["you"]))`；`world` 又不同。据此解释四个断言为何成立。
4.（可选，待本地验证）`hello-mut.wasm` 来自 typst-dev-assets，本仓库内没有它的 Rust 源码；若想看真实实现，可到 [typst-community/wasm-minimal-protocol](https://github.com/typst-community/wasm-minimal-protocol) 仓库找示例插件（plugin.rs 文档 [L139-L147](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L139-L147) 明确指路：示例集合、Rust 封装、WASI 打桩器都在那里）。

**需要观察的现象**：transition 之后**原模块的行为完全不变**——这不是靠"重新加载一份字节码"实现的（`base` 是同一个 `Arc`），而是靠把被污染的实例**移动**进新插件、令原插件无实例可用只能另起炉灶。

**预期结果**：`plugin-transition` 测试通过；能用自己的话讲清"共享字节码、隔离运行状态"这句话对应哪几个字段。

#### 4.3.5 小练习与答案

**练习 1**：一个插件函数内部调用了 WASI 的 `clock_time_get` 想获取当前时间。加载这个插件会发生什么？

**答案**：实例化失败。`Plugin::new` 的 Linker 只提供 `typst_env` 的两个函数，模块声明的 WASI 导入解析不到，`instantiate_and_start` 报错，最终 `plugin(...)` 以「failed to load WebAssembly module …」类诊断失败。文档给出的出路：改用不依赖 WASI 的目标（如 `wasm32-unknown-unknown`）编译，或用 wasi-stub 把 WASI 导入打成空桩。

**练习 2**：既然插件必须是纯函数，`plugin.transition` 为什么不算"违反纯度"的逃生门？

**答案**：transition 并没有让普通调用变得可变——它派生出一个**新的**模块值，原模块不变；派生模块的行为由 `(字节码, transition 历史)` 完全决定，是确定性的（fingerprint 参与相等性与哈希）。也就是说它把"副作用"转化成了"值的一部分"，就像函数式语言里的状态机：状态在变，但每个状态都是确定的值。代价是它只覆盖内存状态、不覆盖 WASM globals（文档坦承的限制）。

**练习 3**：为什么 `Plugin::call` 在调用失败时不把实例还回池，而成功时要还？

**答案**：WASM 实例的线性内存是可变状态。成功路径意味着插件遵守协议、内存处于一致状态，复用可以省去重新实例化（比如跳过昂贵的初始化）；失败路径下内存可能停在任意中间态（甚至半途 traps），继续复用会让后续调用读到脏数据、且错误不可复现——宁可丢弃这个实例、下次重新实例化。这与 4.2 练习 2 的"宁可疑罪从有"是同一种防御性思路。

## 5. 综合实践

**任务**：亲手写一个最小 WASM 插件——一个向宿主输出字符串的函数加一个做字节处理的函数——编译成 wasm 目标后在 Typst 文档中加载、调用并把结果排进 PDF。这个任务贯穿本讲三个模块：加载协议（把 `.wasm` 变成模块）、调用与序列化（`bytes` 进 `bytes` 出）、安全边界（不能依赖 WASI）。

**步骤 1：建插件 crate**（以下为示例代码，Rust 宏 API 以 [wasm-minimal-protocol](https://github.com/typst-community/wasm-minimal-protocol) 仓库 README 为准，待本地验证）：

```bash
cargo new --lib typst-hello-plugin
cd typst-hello-plugin
```

`Cargo.toml`：

```toml
[package]
name = "typst-hello-plugin"
version = "0.1.0"
edition = "2021"

[lib]
crate-type = ["cdylib"]       # 产出 .wasm 而非 .rlib

[dependencies]
wasm-minimal-protocol = "0.1"
```

`src/lib.rs`（示例代码——该 crate 的宏负责把符合协议的 Rust 函数展开成「长度参数 + 两个宿主调用 + 返回码」的样板）：

```rust
use wasm_minimal_protocol::{init_func, rust_func};

init_func!(env_init);

/// 无参数：向宿主发送一个固定字符串。
#[rust_func]
pub fn hello() -> Result<Vec<u8>, String> {
    Ok(b"Hello from my first plugin!".to_vec())
}

/// 单参数：把输入字节转成大写后返回。
#[rust_func]
pub fn shout(data: Vec<u8>) -> Result<Vec<u8>, String> {
    Ok(data.to_ascii_uppercase())
}
```

返回 `Err(String)` 时包装层会走「返回码 1 + 缓冲区放 UTF-8 消息」的错误路径——正是 4.2 读过的 `plugin errored with: …` 分支。

**步骤 2：编译成 32 位 wasm**。两条路线任选：

```bash
rustup target add wasm32-unknown-unknown
cargo build --release --target wasm32-unknown-unknown
```

- 路线 A（推荐起步）：`wasm32-unknown-unknown`，纯裸机目标，天生不链接 WASI，与沙箱最契合；
- 路线 B（按任务要求体验 `wasm32-wasip1`）：先 `rustup target add wasm32-wasip1` 并照常 `cargo build --release --target wasm32-wasip1`，产物会带 WASI 导入、**直接加载会失败**（对应 4.3 练习 1）；此时需按 [plugin.rs:L82-L87](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L82-L87) 文档指路，用 [wasi-stub](https://github.com/typst-community/wasm-minimal-protocol/tree/main/crates/wasi-stub) 工具把 WASI 导入打成空桩后再用。这条失败→修复的弯路本身就是最好的协议课（待本地验证 wasi-stub 的具体调用方式）。

产物路径形如 `target/wasm32-unknown-unknown/release/typst_hello_plugin.wasm`。

**步骤 3：在 Typst 文档中调用**（示例代码，`hello.typ`）：

```typst
#let p = plugin("target/wasm32-unknown-unknown/release/typst_hello_plugin.wasm")

// 包装成惯用类型转换层（官方文档推荐的写法）
#let hello() = str(p.hello())
#let shout(s) = str(p.shout(bytes(s)))

= 插件实验

#hello()

#shout("wasm works!")
```

**步骤 4：编译并验证**：

```bash
cargo run -p typst-cli -- compile hello.typ
```

**预期结果**：PDF 中出现 `Hello from my first plugin!` 与 `WASM WORKS!`（`to_ascii_uppercase` 的效果）。

**排错对照表**（全部能在本讲源码里找到分支）：加载失败报 memory/导入问题 → 4.1；`expected bytes, found string` → 忘了 `bytes(...)` 包装（4.2）；返回值显示为字节字面量 → 忘了 `str(...)` 包装；调用报协议类错误 → 检查导出函数签名是否满足「n 个 i32 进、1 个 i32 出」。

若本地无法安装 wasm 目标（网络受限），替代方案是源码阅读型综合实践：用 `cargo testit plugin` 跑通全部 10 个测试，并为其中 6 个非错误测试各写一句"这段 .typ 验证了 plugin.rs 的哪一行"。

## 6. 本讲小结

- **加载**：`plugin()` 是普通原生函数，经 `DataSource`（路径/字节）与 World 契约拿字节，`#[comemo::memoize]` 保证同字节只编译一次；合法插件的硬性要求只有一条——导出 `memory`；产物是 `Module`，每个函数导出绑定为一个 `PluginFunc`。
- **调用**：求值器对插件无感知，派发点在 `Func::call_impl` 的 `FuncInner::Plugin` 分支——实参全部 `bytes`、结果 `bytes`。协议核心是「**长度作参数**」+ 两个宿主函数（写参数、读结果）+ 返回码 0/1/其他，`CallData` 是跨越 WASM 边界的中转站。
- **安全**：三道边界——能力沙箱（只有两个宿主函数，无 WASI/时钟/随机/文件）、确定性（解释执行、关 relaxed SIMD、纯度要求 + 调用级 memoize）、状态隔离（实例池、失败即弃、transition 以内存快照派生新插件、fingerprint 决定身份）。
- **工程启示**：插件是「宿主提供最小能力、guest 自带内存布局」的极简 RPC 设计；`Arc<PluginBase>` 共享字节码 + 独立实例池的组合，是"不可变骨架 + 可变执行体"的典型 Rust 架构。
- **事实更正**：manifest 推测的 `typst-eval` 参与被证伪——`typst-eval/src/call.rs` 只提供通用调用点 `call_func`，插件逻辑全部住在 `typst-library`。

## 7. 下一步学习建议

本讲是第五单元（专家层）的收官之作，手册的源码之旅到此完整覆盖了「解析 → 求值 → realize → 排版 → 导出 → 扩展」全链路。接下来建议三个方向：

1. **动手写一个真实插件**：到 [typst-community/wasm-minimal-protocol](https://github.com/typst-community/wasm-minimal-protocol) 仓库阅读官方示例集合与测试运行器（plugin.rs 文档 L139-L147 列出的三项资源），尝试把一个纯计算型 Rust 库（如某哈希或解析器）封装成 Typst 包：wasm + 包装函数 + `typst.toml`。
2. **回顾贯穿性主题**：纯度与缓存的关系在本讲第三次出现（u3-l6 的 flow 信号、u5-l8 的 comemo、本讲的 memoize 调用）——值得重读 [plugin.rs:L51-L74](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/plugin.rs#L51-L74) 并思考：如果让你设计 transition API，指纹为什么必须链式累积而不是每次独立哈希？
3. **向外延伸**：对比其他嵌入式脚本系统的沙箱设计（如 Lua/ WASM 在别的排版引擎中的应用），思考 Typst 选择"解释器 + 无 WASI + 字节协议"在性能、安全、可复现三角上的取舍；若关注 wasmi 本身，可读其仓库关于确定性执行的文档。
