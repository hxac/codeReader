# 讲义：`#[kernel]` 与 `#[cuda_module]` 宏

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `#[kernel]` 作用在**单个函数**上时，到底把函数改写成了什么（改名、加 `no_mangle`、生成 marker 结构体）。
- 说清 `#[cuda_module]` 作用在**一个模块**上时，如何扫描其中的所有 `#[kernel]`，并生成 `LoadedModule` 结构体、类型安全的 `module.<核>(...)` 启动方法，以及 `load()` 加载器。
- 解释「保留符号前缀」（`cuda_oxide_kernel_246e25db_` 等）如何成为**宏（编译期）**与 **codegen 后端（rustc 插件）** 之间唯一的对话契约。
- 追踪 `load()` 从「加载内嵌 PTX」到「`cuModuleGetFunction` 按名字解析入口」的完整运行时链路。

本讲是 U2「编写 GPU 内核」的第一讲。U1 已经让你跑通了 vecadd（u1-l4），现在我们要把镜头拉近，看清 `#[kernel]` / `#[cuda_module]` 这两个过程宏在编译期到底做了什么——理解了它们，后续的线程索引（u2-l2）、共享内存（u2-l3）、启动配置（u2-l4）才有根基。

## 2. 前置知识

阅读本讲前，请确认你已经了解（这些都在 U1 建立）：

- **过程宏（proc-macro）**：Rust 中一种「在编译期吃进一段 TokenStream、吐出另一段 TokenStream」的扩展机制。`#[kernel]` 和 `#[cuda_module]` 都是 `#[proc_macro_attribute]`，即属性式过程宏——它们像 `#[derive]` 一样贴在某个 item 上，编译器会把该 item 的 AST 交给宏去改写。
- **单源编译模型**：一个 `.rs` 文件被 rustc 编译成同一份 MIR 后，由 `rustc-codegen-cuda` 后端在 `codegen_crate` 中分流——device 代码走 cuda-oxide 流水线产出 PTX，host 代码走标准 LLVM 后端。**全程不需要 `#[cfg]` 切分。** 详见 u1-l1、u1-l4。
- **device 代码的识别方式**：后端不靠 `#[cfg]`，而是靠**保留命名空间** `cuda_oxide_kernel_<hash>_*` 加调用图可达性来判定一个函数是不是 kernel。这正是本讲要展开的核心契约。
- **`LoadedModule` 与 `load()`**：你在 vecadd 里写过 `let module = kernels::load(&ctx)?;` 然后调用 `module.vecadd(...)`。本讲解释这俩东西从哪来。

如果你对「rustc 通过 `__rustc_codegen_backend` 动态加载后端」还不熟，建议先快速回看 u1-l1 的流水线总览。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用它讲什么 |
|------|------|----------------|
| [crates/cuda-macros/src/lib.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs) | 定义 `#[kernel]`、`#[cuda_module]`、`#[constant]` 等过程宏 | 4.2、4.3、4.4 的全部代码生成逻辑 |
| [crates/rustc-codegen-cuda/examples/vecadd/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs) | 最小可运行的「内核 + 宿主」示例 | 宏展开前后的「用户视角」对照 |
| [crates/reserved-oxide-symbols/src/lib.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/reserved-oxide-symbols/src/lib.rs) | 宏与后端共享的「保留符号前缀」唯一真源 | 4.1 的命名契约 |
| [crates/cuda-host/src/launch.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-host/src/launch.rs) | `CudaKernel` / `GenericCudaKernel` trait 定义 | 4.2、4.4 的 PTX 名字绑定 |
| [crates/cuda-core/src/module.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/module.rs) | `CudaModule::load_function`（封装 `cuModuleGetFunction`） | 4.4 的运行时入口解析 |

> 提示：后两个是「辅助源码」，用来把链路补全。本讲的主角仍是前三个文件。

---

## 4. 核心概念与源码讲解

本讲按「**先契约，后展开，再加载**」的顺序拆成四个最小模块。之所以先讲契约（4.1），是因为 `#[kernel]` 改名、`#[cuda_module]` 生成启动方法、`load()` 解析入口——这三件事都建立在同一个命名约定之上。先理解契约，后面每一步都会顺理成章。

### 4.1 保留符号前缀：宏与后端的唯一对话契约

#### 4.1.1 概念说明

过程宏（编译期，跑在普通 rustc 里）和 codegen 后端（`rustc-codegen-cuda`，作为 dylib 被 rustc `dlopen` 进来）是**两个独立的程序**，它们不能直接互相调用函数。那它们怎么就「配合默契」了呢？

答案是：双方约定了一套**保留符号前缀**。宏把 kernel 函数改成一个带特殊前缀的名字写进 MIR；后端在 MIR 里扫到这个名字，就知道「这是个 kernel」。这套前缀的唯一真源，就是 `reserved-oxide-symbols` crate。

这个 crate 的文档开宗明义地点出了它的定位：

> Single source of truth for the mangled symbol prefixes that the `#[kernel]` / `#[device]` proc macros emit and that the codegen backend, MIR-lowering, and LLVM-export passes consume.
>
> —— 见 [crates/reserved-oxide-symbols/src/lib.rs:4-8](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/reserved-oxide-symbols/src/lib.rs#L4-L8)

它还特别声明 `publish = false`、`#[doc(hidden)]`，不是公共 API，只为让「宏侧」和「后端侧」保持锁步（in lockstep）。如果哪天前缀改了，两边必须同时改，否则 kernel 就找不到了。

#### 4.1.2 核心流程

整个命名契约分三层（crate 文档称之为 *Layered API*，见 [crates/reserved-oxide-symbols/src/lib.rs:26-34](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/reserved-oxide-symbols/src/lib.rs#L26-L34)）：

```
Layer 1  原始常量        RESERVED_ROOT / HASH_SUFFIX / KERNEL_PREFIX ...
                          ↑ 谁都不该手写这些字符串
Layer 2  构造器(builder)  kernel_symbol("vecadd") → "cuda_oxide_kernel_246e25db_vecadd"
                          ↑ 宏侧用：把用户函数名拼成保留符号
Layer 3  谓词/提取器      is_kernel_symbol(name) / kernel_base_name(name)
                          ↑ 后端侧用：识别 + 还原出原始函数名
```

关键常量有四个（仅列本讲用得到的）：

| 常量 | 值 | 含义 |
|------|----|----|
| `RESERVED_ROOT` | `cuda_oxide_` | 所有保留符号的共同根前缀 |
| `HASH_SUFFIX` | `246e25db` | 防误撞的哈希后缀 |
| `KERNEL_PREFIX` | `cuda_oxide_kernel_246e25db_` | kernel 函数的完整前缀 |
| `KERNEL_SCOPE_LOCAL` | `cuda_oxide_kernel_scope_246e25db` | 线程索引作用域隐藏绑定名（u2-l2 详讲） |

其中 `HASH_SUFFIX` 的来历很关键：它是 `sha256("cuda_oxide_ + rust")` 截断到 8 个十六进制字符（见 [crates/reserved-oxide-symbols/src/lib.rs:66-71](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/reserved-oxide-symbols/src/lib.rs#L66-L71)）。这个值本身不重要，重要的是它**固定不变**，而且「没人会不小心把 `246e25db` 写进普通函数名里」。这就保证了：只有真正经过 `#[kernel]` 宏改写的函数，才会被后端当成 kernel 收集；用户随手写的 `fn cuda_oxide_kernel_evil()` 因为缺少哈希后缀，会被 `is_kernel_symbol` 判定为 `false`（这条安全性由单测 `user_names_with_old_prefix_are_not_matched` 锁定）。

#### 4.1.3 源码精读

**Layer 1 — 常量**（[crates/reserved-oxide-symbols/src/lib.rs:63-78](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/reserved-oxide-symbols/src/lib.rs#L63-L78)）：

```rust
pub const RESERVED_ROOT: &str = "cuda_oxide_";
pub const HASH_SUFFIX: &str = "246e25db";
/// `#[kernel] fn vecadd(...)` becomes `fn cuda_oxide_kernel_246e25db_vecadd(...)`.
/// The collector finds these by name; the PTX entry name itself is the
/// unprefixed base (e.g., `vecadd`).
pub const KERNEL_PREFIX: &str = "cuda_oxide_kernel_246e25db_";
```

最后两行注释是本讲最重要的一句话，请记住：**收集器按带前缀的全名识别；但 PTX 里的入口名是去掉前缀的原始名 `vecadd`。** 这条规则解释了为什么 4.2 里宏生成的 marker 用 `"vecadd"` 而不是带前缀的名字。

**Layer 2 — 构造器**（宏侧用，[crates/reserved-oxide-symbols/src/lib.rs:146-148](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/reserved-oxide-symbols/src/lib.rs#L146-L148)）：

```rust
pub fn kernel_symbol(base: &str) -> String {
    format!("{KERNEL_PREFIX}{base}")
}
```

`#[kernel]` 宏在改名时，本质上就是 `KERNEL_PREFIX + fn_name`。

**Layer 3 — 谓词/提取器**（后端侧用，[crates/reserved-oxide-symbols/src/lib.rs:272-274](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/reserved-oxide-symbols/src/lib.rs#L272-L274) 与 [crates/reserved-oxide-symbols/src/lib.rs:344-347](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/reserved-oxide-symbols/src/lib.rs#L344-L347)）：

```rust
pub fn is_kernel_symbol(name: &str) -> bool {
    name.contains(KERNEL_PREFIX)          // 用 contains 而非 starts_with，以兼容跨 crate 的 FQDN
}
pub fn kernel_base_name(name: &str) -> Option<&str> {
    name.find(KERNEL_PREFIX)
        .map(|pos| &name[pos + KERNEL_PREFIX.len()..])   // 剥掉前缀，还原 "vecadd"
}
```

注意它用 `contains` 而非 `starts_with`——因为跨 crate 调用时，符号可能带路径限定（如 `kernel_lib::cuda_oxide_kernel_246e25db_scale`），后端照样能识别并提取出 `scale`。

#### 4.1.4 代码实践

1. **目标**：亲眼看见「哈希后缀」如何挡住误撞。
2. **步骤**：阅读 [crates/reserved-oxide-symbols/src/lib.rs:577-594](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/reserved-oxide-symbols/src/lib.rs#L577-L594) 的单测 `user_names_with_old_prefix_are_not_matched`。
3. **观察现象**：测试把 `"cuda_oxide_kernel_evil"` 这类**缺哈希后缀**的名字喂给所有谓词，断言全部返回 `false`。
4. **预期结果**：理解「只有 `#[kernel]` 宏能造出合法 kernel 符号」这一安全性保障——它防止用户代码意外触发 device 编译。
5. 待本地验证：你可以在 `reserved-oxide-symbols` 目录下 `cargo test user_names_with_old_prefix` 跑这条用例。

#### 4.1.5 小练习与答案

**练习 1**：为什么后端识别 kernel 用 `name.contains(KERNEL_PREFIX)` 而不是 `name.starts_with(KERNEL_PREFIX)`？

**答案**：因为 kernel 可能来自另一个 crate，符号名会带 crate 路径前缀（FQDN 形式，如 `kernel_lib::cuda_oxide_kernel_246e25db_scale`）。用 `starts_with` 会漏掉这种情况；`contains` 能在任意位置匹配到前缀，再由 `kernel_base_name` 用 `find` 定位前缀位置并剥除。

**练习 2**：`HASH_SUFFIX` 的值 `246e25db` 为什么必须「永远固定、永不修改」？

**答案**：它是宏（编译期）与 codegen 后端（rustc 插件）之间的**隐式握手凭证**。如果只改一边，宏生成的符号后端就识别不出来，kernel 不会被编译成 PTX，运行时 `load()` 会报 `ModuleNotFound`/函数找不到。crate 用 `hash_value_is_pinned` 单测把它锁死，并注明「修改它是对所有已构建制品的破坏性变更」。

---

### 4.2 `#[kernel]` 的展开：把一个函数变成「可被收集的 kernel」

#### 4.2.1 概念说明

`#[kernel]` 贴在**单个函数**上，回答两个问题：

1. **后端怎么知道它是 kernel？** —— 改名成 `KERNEL_PREFIX + 原名`，并加 `#[no_mangle]`，让这个带前缀的名字原样留在二进制里供后端收集。
2. **宿主怎么按 PTX 名字启动它？** —— 额外生成一个 marker 结构体，实现 `CudaKernel` trait，把「PTX 入口名」（即去掉前缀的原始名）以常量 `PTX_NAME` 暴露出来。

非泛型 kernel 和泛型 kernel 的展开方式不同。本讲聚焦**非泛型**（vecadd 就是这种），泛型 kernel 的单态化与 `_TID_<hash>` 命名留到 u2-l6 详讲。

#### 4.2.2 核心流程

`kernel` 宏入口做几道校验后分流（[crates/cuda-macros/src/lib.rs:1699-1779](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L1699-L1779)）：

```
#[kernel] fn vecadd(...)
   │
   ├─ reject_reserved_name()        名字不能以 cuda_oxide_ 开头（防双重嵌套）
   ├─ impl_trait_parameter_error()  参数不能用 impl Trait（host/device 特化身份要对齐）
   ├─ rewrite_loop_unroll_attrs()   处理循环上的 #[unroll]
   ├─ inject_thread_index_scope()   给 thread::index_1d() 注入隐藏作用域（u2-l2 详讲）
   │
   └─ 非泛型? ──是──▶ generate_simple_kernel()   本讲重点
              ──否──▶ generate_generic_kernel_no_instantiation()  u2-l6 详讲
```

非泛型分支 `generate_simple_kernel` 做三件事（[crates/cuda-macros/src/lib.rs:2393-2420](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L2393-L2420)）：

```
原函数  fn vecadd(a: &[f32], b: &[f32], mut c: DisjointSlice<f32>) { ... }
                                ↓ 宏展开后产出两个 item
1) #[unsafe(no_mangle)]
   fn cuda_oxide_kernel_246e25db_vecadd(...) { ... }   ← 改名 + no_mangle，供后端收集
2) pub struct __vecadd_CudaKernel;
   impl CudaKernel for __vecadd_CudaKernel {
       const PTX_NAME: &'static str = "vecadd";        ← 用原始名，供宿主解析
   }
```

为什么 PTX 入口名用原始名 `vecadd`，而收集用的符号却带前缀？因为 4.1 里说过：后端收集到带前缀的全名后，会用 `kernel_base_name` 剥掉前缀，**以原始名作为 PTX 里的 `.entry` 名字**写出去。于是宿主侧只要也拿原始名去 `cuModuleGetFunction`，两边就对上了。宏生成的 marker 恰好就是原始名，等于把这条「剥前缀」规则在宿主侧复刻了一遍。

#### 4.2.3 源码精读

**入口校验**（[crates/cuda-macros/src/lib.rs:1704-1709](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L1704-L1709)）：

```rust
if let Some(err) = reject_reserved_name(&input.sig.ident) {
    return err;
}
if let Some(err) = impl_trait_parameter_error(&input, "kernel") {
    return err.to_compile_error().into();
}
```

`reject_reserved_name`（[crates/cuda-macros/src/lib.rs:182-195](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L182-L195)）拦截以 `cuda_oxide_` 开头的函数名——否则 `#[kernel] fn cuda_oxide_kernel_foo()` 会展开成双重嵌套的 `cuda_oxide_kernel_246e25db_cuda_oxide_kernel_foo`，污染符号表。

**非泛型展开**（[crates/cuda-macros/src/lib.rs:2393-2420](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L2393-L2420)）：

```rust
fn generate_simple_kernel(mut input: ItemFn) -> TokenStream {
    inject_thread_index_scope(&mut input);
    let fn_name = input.sig.ident.clone();
    let new_name = format_ident!("{}{}", KERNEL_PREFIX, fn_name);   // 拼出保留符号
    let original_fn = input.clone();
    input.sig.ident = new_name;                                    // 改名
    let ptx_entry_name = fn_name.to_string();                      // PTX 入口名 = 原始名
    let cuda_kernel_impl = generate_cuda_kernel_impl(&fn_name, &ptx_entry_name, &original_fn);
    let expanded = quote! {
        #[unsafe(no_mangle)]
        #input                                                     // 改名后的函数
        #cuda_kernel_impl                                          // marker + CudaKernel impl
    };
    TokenStream::from(expanded)
}
```

**marker 生成**（[crates/cuda-macros/src/lib.rs:2524-2540](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L2524-L2540)）：

```rust
fn generate_cuda_kernel_impl(fn_name: &Ident, ptx_name: &str, _func: &ItemFn) -> TokenStream2 {
    let marker_name = format_ident!("__{}_CudaKernel", fn_name);
    quote! {
        #[doc(hidden)]
        pub struct #marker_name;
        impl cuda_host::CudaKernel for #marker_name {
            const PTX_NAME: &'static str = #ptx_name;              // "vecadd"
        }
    }
}
```

`CudaKernel` trait 本身极简，只有一个常量（[crates/cuda-host/src/launch.rs:53-56](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-host/src/launch.rs#L53-L56)）：

```rust
pub trait CudaKernel {
    /// The PTX entry point name (e.g., "vecadd" - the original function name)
    const PTX_NAME: &'static str;
}
```

trait 文档注释（[crates/cuda-host/src/launch.rs:49-52](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-host/src/launch.rs#L49-L52)）再次强调这条规则：函数内部被改名为 `cuda_oxide_kernel_<hash>_vecadd` 供后端检测，**但 PTX 入口点用的是原始名**。

#### 4.2.4 代码实践

1. **目标**：用 `cargo expand` 亲眼看 `#[kernel]` 展开成两个 item。
2. **步骤**：
   - 安装扩展工具：`cargo +nightly-2026-04-03 install cargo-expand`（工具链见 u1-l3）。
   - 在 vecadd 示例目录运行：`cargo oxide build vecadd` 先确认能编（无需 GPU）。
   - 再运行 `cargo expand --package vecadd`（待本地验证：cargo-oxide 是否透传 expand 子命令；若不可用，可直接 `cargo +nightly-2026-04-03 expand`）。
3. **观察现象**：在展开输出里搜索 `cuda_oxide_kernel_246e25db_vecadd`，应能看到改名后的函数；再搜 `__vecadd_CudaKernel`，应能看到 marker 及 `const PTX_NAME: &'static str = "vecadd";`。
4. **预期结果**：亲眼确认「一个 `#[kernel]` 函数 → 一个改名函数 + 一个 marker」的展开形态。
5. 待本地验证：`cargo expand` 在过程宏项目里可能需要额外配置，若报错可改用「读宏源码 + 手工模拟展开」的方式。

#### 4.2.5 小练习与答案

**练习 1**：为什么改名后的函数要加 `#[unsafe(no_mangle)]`？如果不加会怎样？

**答案**：Rust 默认会对函数做名字改写（name mangling），加上版本哈希等。加了 `no_mangle`，符号才会以 `cuda_oxide_kernel_246e25db_vecadd` 这个**字面名字**原样留在目标文件里。如果不加，符号名会被 rustc 改写得面目全非，后端用 `is_kernel_symbol` 做字符串匹配时就会漏掉，kernel 不会被收集，最终 `load()` 时找不到函数。

**练习 2**：marker 结构体 `__vecadd_CudaKernel` 实现的 `PTX_NAME` 是 `"vecadd"` 还是 `"cuda_oxide_kernel_246e25db_vecadd"`？为什么？

**答案**：是 `"vecadd"`。因为后端在生成 PTX 时，会用 `kernel_base_name` 把带前缀的全名剥成原始名 `vecadd` 作为 PTX 入口名（见 4.1.3）。宿主侧的 `load_function(PTX_NAME)` 最终调用 `cuModuleGetFunction("vecadd")`，必须和 PTX 里的入口名一致，所以 marker 也要用原始名。

---

### 4.3 `#[cuda_module]` 生成 `LoadedModule`：扫描模块、生成启动方法

#### 4.3.1 概念说明

如果说 `#[kernel]` 处理的是「单个函数」，那 `#[cuda_module]` 处理的就是「一个装着若干 kernel 的模块」。它做的事可以一句话概括：**扫描模块里所有 `#[kernel]` 函数，为整个模块生成一个 `LoadedModule` 结构体，里面为每个 kernel 生成一个类型安全的启动方法 `module.<核>(...)`，外加一个 `load()` 加载器。**

回头看 vecadd 的写法（[crates/rustc-codegen-cuda/examples/vecadd/src/main.rs:35-47](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L35-L47)）：

```rust
#[cuda_module]
mod kernels {
    use super::*;
    #[kernel]
    pub fn vecadd(a: &[f32], b: &[f32], mut c: DisjointSlice<f32>) {
        let idx = thread::index_1d();
        // ...
    }
}
```

宏展开后，`mod kernels` 里会多出：一个 `LoadedModule` 结构体（持有一个 `vecadd_function: CudaFunction` 字段）、`load()`/`load_named()`/`from_module()` 三个函数，以及 `impl LoadedModule { pub fn vecadd(...) }` 方法。于是宿主侧就能写（[crates/rustc-codegen-cuda/examples/vecadd/src/main.rs:75-84](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L75-L84)）：

```rust
let module = kernels::load(&ctx).expect("Failed to load embedded CUDA module");
module.vecadd(&stream, LaunchConfig::for_num_elems(N as u32), &a_dev, &b_dev, &mut c_dev)
      .expect("Kernel launch failed");
```

#### 4.3.2 核心流程

`expand_cuda_module` 的骨架（[crates/cuda-macros/src/lib.rs:338-497](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L338-L497)）：

```
#[cuda_module] mod kernels { #[kernel] fn vecadd(...); #[kernel] fn scale(...); ... }
   │
   ├─ 要求 inline module（否则报错）
   ├─ collect_cuda_module_kernels()   扫描 #[kernel]，得到 [vecadd, scale, ...]
   │     └─ 至少要有一个 kernel，否则报错
   ├─ collect_cuda_module_constants() 扫描 #[constant] static（本讲略，进阶讲）
   │
   ├─ 生成 LoadedModule 结构体
   │     __module: Arc<CudaModule>
   │     __vecadd_function: CudaFunction   ← 每个 kernel 一个字段
   │     __scale_function: CudaFunction
   │
   ├─ 生成 load() / load_named() / from_module()
   │     from_module 里：__vecadd_function: module.load_function(<__vecadd_CudaKernel>::PTX_NAME)?
   │
   └─ impl LoadedModule {
          pub fn vecadd(&self, stream, config, a, b, c) -> Result<()> { ... }  ← 类型安全启动
          pub fn scale(&self, stream, config, ...) -> Result<()> { ... }
       }
```

**类型映射**是 `module.vecadd(...)` 能保证类型安全的关键。宏把 kernel 签名里的设备端类型，映射成宿主侧启动方法参数类型（文档见 [crates/cuda-macros/src/lib.rs:264-270](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L264-L270)，实现在 [crates/cuda-macros/src/lib.rs:1001-1051](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L1001-L1051)）：

| kernel 签名里的类型 | 启动方法参数类型 |
|--------------------|------------------|
| `&[T]` | `&cuda_core::DeviceBuffer<T>` |
| `&mut [T]` | `&mut cuda_core::DeviceBuffer<T>` |
| `DisjointSlice<T>` | `&mut cuda_core::DeviceBuffer<T>` |
| `Copy` 标量/结构体/裸指针 | 保持原类型，约束为 `KernelScalar` |

所以 vecadd 的 `c: DisjointSlice<f32>` 在宿主侧变成了 `&mut DeviceBuffer<f32>`——这正是 u1-l4 里你写 `&mut c_dev` 能直接传进去的原因。类型不匹配会在**编译期**报错，而不是运行时崩在 GPU 上。

#### 4.3.3 源码精读

**入口**（[crates/cuda-macros/src/lib.rs:294-310](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L294-L310)）：`cuda_module` 不接受参数，解析为 `ItemMod` 后交给 `expand_cuda_module`。

**扫描 kernel**（[crates/cuda-macros/src/lib.rs:499-529](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L499-L529)）：

```rust
fn collect_cuda_module_kernels(items: &[Item]) -> syn::Result<Vec<CudaModuleKernel>> {
    let mut kernels = Vec::new();
    for item in items {
        let Item::Fn(item_fn) = item else { continue; };
        if !has_attr_named(&item_fn.attrs, "kernel") { continue; }   // 只认 #[kernel]
        // ... 提取 cluster_dim / cooperative / params / is_generic
        kernels.push(CudaModuleKernel { fn_name: item_fn.sig.ident.clone(), params, .. });
    }
    Ok(kernels)
}
```

它在模块体里逐项找带 `kernel` 属性的函数，提取出函数名和参数签名。注意：**模块里必须至少有一个 kernel**，否则 `expand_cuda_module` 在 [crates/cuda-macros/src/lib.rs:350-355](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L350-L355) 报错 `cuda_module found no #[kernel] functions`。

**为每个 kernel 生成一个字段**（[crates/cuda-macros/src/lib.rs:360-377](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L360-L377)）：

```rust
let function_fields = non_generic_kernels.clone().map(|kernel| {
    let field = cuda_module_function_field(&kernel.fn_name);   // __vecadd_function
    quote! { #field: ::cuda_core::CudaFunction, }
});
let function_initializers = non_generic_kernels.map(|kernel| {
    let field = cuda_module_function_field(&kernel.fn_name);
    let marker = cuda_kernel_marker_name(&kernel.fn_name);     // __vecadd_CudaKernel
    quote! {
        #field: module.load_function(<#marker as ::cuda_host::CudaKernel>::PTX_NAME)?,
    }
});
```

这是连接 4.2 和 4.4 的关键一行：`load_function(<__vecadd_CudaKernel>::PTX_NAME)` —— 用 4.2 生成的 marker 拿到 `"vecadd"`，再去解析 PTX 入口。

**生成结构体与加载器**（[crates/cuda-macros/src/lib.rs:437-496](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L437-L496)，节选）：

```rust
Ok(quote! {
    #vis mod #ident {
        // ... 原模块内容（kernel 函数等） ...
        #[derive(Clone, Debug)]
        pub struct LoadedModule {
            __module: ::std::sync::Arc<::cuda_core::CudaModule>,
            __generic_functions: /* 泛型核缓存 */,
            #(#function_fields)*            // __vecadd_function: CudaFunction,
        }

        pub fn load(ctx: &Arc<CudaContext>) -> Result<LoadedModule, EmbeddedModuleError> {
            load_named(ctx, env!("CARGO_PKG_NAME"))
        }
        pub fn load_named(ctx: &Arc<CudaContext>, name: &str) -> Result<LoadedModule, EmbeddedModuleError> {
            #artifact_anchor_statements      // keep-alive 握手，见 4.4
            #module_loader                   // load_embedded_module(...) 或合并加载
            from_module(module).map_err(...)
        }
        pub fn from_module(module: Arc<CudaModule>) -> Result<LoadedModule, DriverError> {
            Ok(LoadedModule {
                __module: module.clone(),
                #(#function_initializers)*   // __vecadd_function: module.load_function("vecadd")?,
                ..
            })
        }

        impl LoadedModule {
            #(#launch_methods)*              // pub fn vecadd(&self, ...) { ... }
        }
    }
})
```

**启动方法的形状**（[crates/cuda-macros/src/lib.rs:1084-1126](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L1084-L1126)）：每个启动方法签名是 `fn vecadd(&self, stream: &CudaStream, config: LaunchConfig, /* 映射后的参数 */) -> Result<(), DriverError>`，方法体把参数编组（marshal）成 `Vec<*mut c_void>`，再调用 `launch_kernel_on_stream(&self.__vecadd_function, ...)`。也就是说，`module.vecadd(...)` 最终落到 `cuLaunchKernel`，用的就是加载阶段解析出的 `CudaFunction`。

#### 4.3.4 代码实践

1. **目标**：验证「每加一个 `#[kernel]`，`module.<核>(...)` 就自动多一个方法」。
2. **步骤**：在 vecadd 的 `mod kernels` 里新增第二个核函数（综合实践会完整做一遍，这里先小步）：

   ```rust
   // 示例代码：在 #[cuda_module] mod kernels 内追加
   #[kernel]
   pub fn scale(factor: f32, a: &[f32], mut c: DisjointSlice<f32>) {
       let idx = thread::index_1d();
       let i = idx.get();
       if let Some(c_elem) = c.get_mut(idx) {
           *c_elem = a[i] * factor;
       }
   }
   ```

3. **观察现象**：在 `main` 里写 `module.scale(&stream, LaunchConfig::for_num_elems(N as u32), &2.0_f32, &a_dev, &mut c_dev)?;`，编译应通过。
4. **预期结果**：`module.scale(...)` 这个方法**不是你手写的**，而是 `#[cuda_module]` 在编译期扫描到 `#[kernel] fn scale` 后自动生成的；它的参数类型（`factor: f32`、`a: &DeviceBuffer<f32>`、`c: &mut DeviceBuffer<f32>`）由 4.3.2 的类型映射规则决定。
5. 待本地验证：`cargo oxide build vecadd` 确认编译通过（无需 GPU）。

#### 4.3.5 小练习与答案

**练习 1**：如果 `#[cuda_module] mod kernels { }` 里一个 `#[kernel]` 都没有，会发生什么？

**答案**：编译报错。`expand_cuda_module` 在 [crates/cuda-macros/src/lib.rs:350-355](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L350-L355) 检查到 `kernels.is_empty()` 时，会抛出 `cuda_module found no #[kernel] functions in this module`。这是为了防止生成一个空的、无意义的 `LoadedModule`。

**练习 2**：kernel 签名里写 `c: &mut [f32]` 和写 `c: DisjointSlice<f32>`，生成的启动方法参数类型有区别吗？

**答案**：没有区别，两者都映射为 `&mut cuda_core::DeviceBuffer<f32>`（见 4.3.2 的类型映射表，规则在 [crates/cuda-macros/src/lib.rs:1029-1037](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L1029-L1037)）。但语义不同：`DisjointSlice` 在设备端提供越界安全的 `get_mut(idx)`（u2-l2 详讲），而 `&mut [f32]` 需要你自己写 `if idx < N` 守卫。建议优先用 `DisjointSlice`。

---

### 4.4 `load()` 加载器：把内嵌 PTX 和 PTX 入口符号连起来

#### 4.4.1 概念说明

宏在编译期生成了 `load()`，但「加载」真正的动作发生在**运行时**。`load(&ctx)` 要完成三件事：

1. **保住内嵌的 PTX 制品**：通过一个 link-anchor 符号握手，防止链接器把 lib crate 里的 `.oxart`（oxide artifact）数据段当垃圾裁掉。
2. **找到并解析 PTX bundle**：从当前可执行文件的内嵌段里取出 PTX，交给 CUDA Driver 加载成 `CudaModule`。
3. **逐个解析 kernel 入口**：对每个 kernel，用其 `PTX_NAME`（原始名）调 `cuModuleGetFunction`，拿到 `CudaFunction` 句柄，存进 `LoadedModule` 的对应字段。

做完这三步，`module.vecadd(...)` 才有 `CudaFunction` 可用。

#### 4.4.2 核心流程

```
kernels::load(&ctx)
   │  (宏生成，见 4.3)
   ├─ load_named(ctx, env!("CARGO_PKG_NAME"))
   │     ├─ #artifact_anchor_statements   ← 读取 anchor 符号地址 → 强制链接器保留 .oxart 段
   │     ├─ load_embedded_module(ctx, name)  ← 从内嵌 bundle 取 PTX，cuModuleLoadData → CudaModule
   │     └─ from_module(module)
   │           └─ LoadedModule { __vecadd_function: module.load_function("vecadd")?, ... }
   │                                                  ↓
   │                                           cuModuleGetFunction(module, "vecadd")  ← 解析 PTX 入口
   └─ 返回 LoadedModule（每个 kernel 都有可启动的 CudaFunction 句柄）
```

关于 anchor 握手为什么必要：codegen 后端把每个 crate 的 PTX 打包进一个只有数据、没有符号的小目标文件。对于**二进制 crate**，这个目标文件直接交给链接器，段总在；但对于**库 crate**，它成了 `.rlib` 归档的一员，而链接器「只提取被引用了符号的归档成员」——一个纯数据目标不定义任何符号，就会被静默丢弃，历史上导致 `load()` 运行时报 `ModuleNotFound`（issue #72）。解决方法是后端在 `.oxart` 段头部定义一个 anchor 符号，宏在 `load_named()` 里读这个符号的地址，从而制造一个「未定义引用」，倒逼链接器把该成员拉出来。

#### 4.4.3 源码精读

**`load` / `load_named` / `from_module` 的生成**（[crates/cuda-macros/src/lib.rs:455-481](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L455-L481)）：

```rust
pub fn load(ctx: &Arc<CudaContext>) -> Result<LoadedModule, EmbeddedModuleError> {
    load_named(ctx, env!("CARGO_PKG_NAME"))           // 用包名作为 bundle 名 hint
}
pub fn load_named(ctx: &Arc<CudaContext>, name: &str) -> Result<LoadedModule, EmbeddedModuleError> {
    #artifact_anchor_statements                        // anchor 握手
    #module_loader                                     // load_embedded_module(ctx, name)?
    from_module(module).map_err(EmbeddedModuleError::Driver)
}
pub fn from_module(module: Arc<CudaModule>) -> Result<LoadedModule, DriverError> {
    Ok(LoadedModule {
        __module: module.clone(),
        #(#function_initializers)*                     // 每个字段的 load_function(...)
        ..
    })
}
```

**module_loader 的分流**（[crates/cuda-macros/src/lib.rs:380-394](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L380-L394)）：若模块里有泛型 kernel，走 `load_all_ptx_bundles_merged`（因为泛型核的单态化 PTX 在消费方 crate 里）；否则走 `load_embedded_module`。vecadd 是非泛型，走后者。

**anchor 握手的生成**（[crates/cuda-macros/src/lib.rs:629-642](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L629-L642)，节选关键）：

```rust
let _artifact_anchor: *const u8 = {
    unsafe extern "C" {
        #[link_name = #anchor_name]                    // cuda_oxide_artifact_anchor_246e25db_...
        static CUDA_OXIDE_BUNDLE_ANCHOR: u8;
    }
    ::std::hint::black_box(unsafe { ::core::ptr::addr_of!(CUDA_OXIDE_BUNDLE_ANCHOR) })
};
```

用 `black_box` 取地址，确保优化器不会删掉这个引用，从而保住对 anchor 的「未定义引用」。

**运行时解析入口**（[crates/cuda-core/src/module.rs:241-249](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/module.rs#L241-L249)）：宏生成的 `function_initializers` 里那句 `module.load_function(PTX_NAME)?`，最终调到这里：

```rust
pub fn load_function(self: &Arc<Self>, fn_name: &str) -> Result<CudaFunction, DriverError> {
    self.ctx.bind_to_thread()?;
    let c_name = CString::new(fn_name).unwrap();       // "vecadd"
    let cu_function = unsafe {
        cuda_bindings::cuModuleGetFunction(/* module, c_name */ ...)
    };
    // ...
}
```

`cuModuleGetFunction` 拿着 `"vecadd"` 这个名字，在已加载的 PTX 里找 `.entry vecadd`，找到就返回 `CUfunction` 句柄。这正是 4.1 说的「PTX 入口名用原始名」在运行时的落点。

把整条链路串起来，一条 kernel 从源码到运行时的完整对应关系是：

```
源码        #[kernel] fn vecadd(...)
改名        cuda_oxide_kernel_246e25db_vecadd   (+ #[no_mangle])   ← 后端按全名收集
PTX         .entry vecadd                          ← kernel_base_name 剥前缀后写出的入口名
marker      <__vecadd_CudaKernel as CudaKernel>::PTX_NAME = "vecadd"
运行时      cuModuleGetFunction(module, "vecadd")                  ← 按 PTX 入口名解析
启动        module.vecadd(...) → cuLaunchKernel(__vecadd_function, ...)
```

「全名 `cuda_oxide_kernel_246e25db_vecadd`」和「入口名 `vecadd`」是两个不同的名字，分别服务于「后端收集」和「驱动解析」两个阶段——这是本讲最容易混淆也最值得记牢的一点。

#### 4.4.4 代码实践

1. **目标**：从产物侧确认 PTX 入口名确实是原始名 `vecadd`。
2. **步骤**：
   - 运行 `cargo oxide pipeline vecadd`（u1-l3 介绍过 pipeline 子命令），它会保留中间产物。
   - 在产物目录里找到生成的 `.ptx`（或 `.ll`，路径见 u3-l2），用文本工具搜 `.entry` 或 `vecadd`（待本地验证：具体产物路径与扩展名以本地输出为准）。
3. **观察现象**：PTX 里应有 `.entry vecadd`（而不是 `.entry cuda_oxide_kernel_246e25db_vecadd`）。
4. **预期结果**：印证 4.1—4.4 的结论——收集用全名，PTX 入口用原始名，宿主用原始名解析。
5. 待本地验证：若无 GPU，`pipeline`/`build` 仍可生成 PTX 文本供你检查；`.entry` 的确切拼写依 llc/PTX 版本可能略有差异。

#### 4.4.5 小练习与答案

**练习 1**：`from_module` 里为何对每个 kernel 字段都要调一次 `load_function`？能不能只在启动时才解析？

**答案**：`load_function` 封装了 `cuModuleGetFunction`（[crates/cuda-core/src/module.rs:241](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/module.rs#L241)），它把「PTX 入口名 → `CUfunction` 句柄」的解析固化在加载阶段，之后每次 `module.vecadd(...)` 都直接用这个句柄调 `cuLaunchKernel`，避免每次启动都重复名字查找。这是用一次解析换每次启动的低开销。

**练习 2**：为什么 `load_named` 里要读 `_artifact_anchor` 的地址？删掉这段会怎样（对库 crate）？

**答案**：这是为了制造对 `.oxart` 段 anchor 符号的引用，倒逼链接器把库 crate 里那个纯数据的 artifact 归档成员提取出来（见 4.4.2）。删掉后，对**库 crate** 会发生链接器静默丢弃 bundle，运行时 `load()` 报 `ModuleNotFound`（issue #72）。对**二进制 crate** 影响较小，因为其 artifact 直接进链接，不受归档提取规则约束。

---

## 5. 综合实践

把本讲四个模块串成一个完整任务：**给 vecadd 示例新增第二个 kernel，跑通它，并讲清它如何对应到 PTX 入口符号。**

**任务**：在 vecadd 的 `#[cuda_module] mod kernels` 里新增一个 `double_it` kernel（把输入向量每个元素乘 2），然后在 `main` 里启动它，最后用本讲学到的知识解释整条符号链路。

**步骤**：

1. 复制示例到自己的实验目录（或在原示例上改）：
   ```bash
   cargo oxide new my_vecadd   # 待本地验证：new 子命令用法见 u1-l3
   ```

2. 在 `#[cuda_module] mod kernels` 内，紧挨 `vecadd` 加第二个 kernel（示例代码）：
   ```rust
   #[kernel]
   pub fn double_it(a: &[f32], mut c: DisjointSlice<f32>) {
       let idx = thread::index_1d();
       let i = idx.get();
       if let Some(c_elem) = c.get_mut(idx) {
           *c_elem = a[i] * 2.0;
       }
   }
   ```

3. 在 `main` 里，加载模块后调用自动生成的方法（示例代码）：
   ```rust
   let module = kernels::load(&ctx)?;
   module.double_it(&stream, LaunchConfig::for_num_elems(N as u32), &a_dev, &mut c_dev)?;
   let c_host = c_dev.to_host_vec(&stream)?;
   // 断言 c_host[i] == a_host[i] * 2.0
   ```

4. 编译并运行：`cargo oxide run my_vecadd`（待本地验证：需 GPU；无 GPU 用 `cargo oxide build` 确认编译通过）。

5. **解释链路**（这是本实践的精华，写进你的学习笔记）。针对 `double_it`，填出下表：

   | 阶段 | 名字 / 产物 |
   |------|-------------|
   | 源码 | `#[kernel] fn double_it(...)` |
   | `#[kernel]` 改名后的全名 | ?（提示：`KERNEL_PREFIX + double_it`） |
   | 后端收集用的判定 | `is_kernel_symbol(...)` 为 true |
   | PTX 里的入口名 | ?（提示：剥前缀） |
   | 宏生成的 marker | `__double_it_CudaKernel`，`PTX_NAME = ?` |
   | `LoadedModule` 字段 | `__double_it_function: CudaFunction` |
   | 字段初始化 | `module.load_function(?)` |
   | 运行时解析 | `cuModuleGetFunction(module, ?)` |
   | 启动方法 | `module.double_it(...)` → `cuLaunchKernel(__double_it_function, ...)` |

**参考答案**（表中问号处）：改名后全名 `cuda_oxide_kernel_246e25db_double_it`；PTX 入口名 `double_it`；`PTX_NAME = "double_it"`；`load_function("double_it")`；`cuModuleGetFunction(module, "double_it")`。

**自检**：如果你能不查讲义填全这张表，并说清「全名 vs 入口名」的区别，本讲就过关了。

## 6. 本讲小结

- `reserved-oxide-symbols` 是宏与 codegen 后端之间**唯一的命名契约真源**：`KERNEL_PREFIX = cuda_oxide_kernel_246e25db_`，其中 `246e25db` 是防误撞的固定哈希后缀。
- `#[kernel]` 作用在单个函数上：把它**改名**为 `KERNEL_PREFIX + 原名` 并加 `#[no_mangle]`（供后端按全名收集），同时生成一个 marker 结构体实现 `CudaKernel { const PTX_NAME = 原始名 }`（供宿主按原始名解析）。
- `#[cuda_module]` 作用在模块上：扫描所有 `#[kernel]`，生成 `LoadedModule` 结构体（每个 kernel 一个 `CudaFunction` 字段）、类型安全的 `module.<核>(...)` 启动方法（设备端类型→宿主端 `DeviceBuffer` 的映射保证编译期类型安全），以及 `load()`/`load_named()`/`from_module()` 加载器。
- `load()` 运行时三步走：anchor 握手保住内嵌 `.oxart` 段 → `load_embedded_module` 解析 PTX bundle → 对每个 kernel 用 `PTX_NAME` 调 `cuModuleGetFunction` 拿到 `CudaFunction` 句柄。
- **最易混淆点**：后端收集用「带前缀的全名 `cuda_oxide_kernel_246e25db_vecadd`」，而 PTX 入口与驱动解析用「剥前缀的原始名 `vecadd`」——两个名字服务于两个阶段。

## 7. 下一步学习建议

- **u2-l2（线程索引与类型安全）**：本讲多次出现的 `thread::index_1d()` 和 `DisjointSlice::get_mut(idx)` 是怎么用 `ThreadIndex` 见证类型在编译期防错的？`KERNEL_SCOPE_LOCAL` 那个隐藏作用域绑定又是什么？这是下一讲的主题。
- **u2-l4（从宿主启动内核）**：本讲里 `LaunchConfig::for_num_elems(N)` 和 `module.vecadd(&stream, config, ...)` 的参数编组、`cuLaunchKernel` 调用细节，将在启动专讲里展开。
- **u3-l2（模块加载与内嵌制品）**：想深挖 `.oxart` bundle 的 wire 格式、anchor 符号的 v1/v2 区别、`load_embedded_module` 如何从当前可执行文件里发现 bundle，留到宿主运行时单元。
- **延伸阅读源码**：泛型 kernel 的展开（`generate_generic_kernel_no_instantiation`，[crates/cuda-macros/src/lib.rs:2200-2322](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L2200-L2322)）与 `_TID_<hex32>` 命名，将在 u2-l6（闭包与泛型上 GPU）详讲。
