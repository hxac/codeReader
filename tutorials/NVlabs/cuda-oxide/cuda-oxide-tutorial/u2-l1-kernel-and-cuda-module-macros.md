# u2-l1　`#[kernel]` / `#[cuda_module]` 宏与启动契约属性

## 1. 本讲目标

本讲是「编写 GPU 内核」单元的第一讲，专门拆解 cuda-oxide 的过程宏层。学完后你应该能够：

1. 说清楚 `#[kernel]` 在编译期把一个普通 Rust 函数改写成了什么（重命名、入口标记、`CudaKernel` 实现）。
2. 说清楚 `#[cuda_module]` 如何扫描模块（含 #324 起的**嵌套 inline 模块**），生成 `LoadedModule` 视图、`load()` 加载器、以及每个内核的启动方法。
3. 理解 #318 引入的 `#[launch_contract(...)]` 如何把某个内核的启动 API 从「raw `LaunchConfig`」切换为「`prepare_*` → `PreparedLaunch`」受检启动，以及为什么**未签约**内核的同名方法、和签约模块的**所有 loader** 都变成了 `unsafe`。
4. 认识保留符号前缀（`cuda_oxide_kernel_246e25db_*` 等）如何把宏与 codegen 后端、运行时加载器绑定成一个整体。

本讲不讨论线程索引的类型安全细节（那是 u2-l2），也不讨论 `DeviceBuffer` 内存搬运（u2-l5），但会反复用到它们的结论。

## 2. 前置知识

在开始前，请确保你已经具备 u1-l4（vecadd 端到端）的体感，并理解以下术语：

- **过程宏（proc-macro）**：Rust 中一类在编译期消费 token、产出新 token 的「编译插件」。cuda-oxide 的 `cuda-macros` crate 是一个 `proc-macro = true` 的 crate，里面每个 `#[proc_macro_attribute]` 函数就是一个属性宏。
- **单源编译**：同一份 `.rs`，一次编译同时产出宿主机器码与设备 PTX。分流发生在 codegen 后端，宏只负责「打标记」（见 u1-l1、u1-l4）。
- **kernel / device**：`#[kernel]` 是 PTX `.entry` 入口（可被宿主启动）；`#[device]` 是 PTX `.func`（仅设备内部调用）。
- **raw `LaunchConfig`**：一组 `(grid_dim, block_dim, shared_mem_bytes)` 的原始数字，**它不知道**哪个内核会消费它，因此用它启动内核是不安全的（u1-l4 已展示必须包在 `unsafe` 块中）。
- **见证类型（marker type）**：不占运行时空间、只用于在类型系统里「证明」某件事的零大小类型。本讲里 `__<name>_CudaKernel` 就是这种见证类型，它把「这个内核的启动契约」编码进类型。

一句话直觉：**宏的工作就是把「人写的 Rust 函数」翻译成「codegen 后端能按命名约定找到、宿主能按类型安全启动」的双面代码。**

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [crates/cuda-macros/src/lib.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs) | 所有过程宏的实现：`#[kernel]`、`#[cuda_module]`、`#[launch_contract]`、`#[launch_bounds]`、`#[cluster_launch]`、`#[cooperative_launch]`、`#[device]`、`cuda_launch!` 等。本讲的全部代码生成逻辑都在这里。 |
| [crates/cuda-macros/README.md](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/README.md) | 宏的权威文档，逐属性列出语法、生成的代码、安全契约与示例。读源码时强烈建议对照它。 |
| [crates/reserved-oxide-symbols/src/lib.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs) | 保留符号命名契约的「唯一真相源」。定义 `KERNEL_PREFIX`、`DEVICE_PREFIX`、`ARTIFACT_ANCHOR_PREFIX` 等常量与配套 builder / 谓词。 |
| [crates/rustc-codegen-cuda/examples/cuda_module_nested/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cuda_module_nested/src/main.rs) | 嵌套 inline 模块的端到端示例，演示 `LoadedModule::from_parent` 的跨命名空间视图构建。 |
| [crates/cuda-core/src/launch.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs) | 启动契约的宿主侧类型：`LaunchConfig`、`KernelLaunchConfig`(sealed)、`LaunchConfig1D/2D/3D`、`KernelLaunchContract` trait、`PreparedLaunch<C>`、`LaunchContractError`。宏生成的代码会引用这些类型。 |
| [crates/cuda-device/src/disjoint.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/disjoint.rs) | `__LaunchContractDisjointSlice` 品牌化 sealed trait，被启动契约用来在类型系统里校验 `DisjointSlice` 的维度。 |

## 4. 核心概念与源码讲解

本讲按五个最小模块展开：

- 4.1　`#[kernel]` 展开：从普通函数到「保留名 + 见证类型」
- 4.2　`#[cuda_module]` 生成 `LoadedModule`（含嵌套模块视图）
- 4.3　`#[launch_contract]` 与 `prepare_*` → `PreparedLaunch` 受检启动
- 4.4　`unsafe` loader 与 `_unchecked` 专家路径
- 4.5　保留符号前缀：宏与 codegen 后端的命名契约

---

### 4.1　`#[kernel]` 展开：从普通函数到「保留名 + 见证类型」

#### 4.1.1 概念说明

`#[kernel]` 是一个**属性宏**：它吃掉一个 `fn`，吐出改写后的 token。它要做三件事：

1. **重命名**：把 `fn vecadd(...)` 改名为 `fn cuda_oxide_kernel_246e25db_vecadd(...)`，并加 `#[no_mangle]`。这个「难以猜中」的前缀是 codegen 后端识别 device 代码的**唯一信号**——后端按命名空间 `cuda_oxide_kernel_<hash>_*` 扫描代码生成单元（回顾 u1-l1 的 `codegen_crate`）。
2. **保留 PTX 入口名**：真正写进 PTX 的 `.entry` 名字仍然是**未加前缀的原始名**（如 `vecadd`）。前缀只是给宿主/后端「识别用」的，PTX 符号是干净的。
3. **生成见证类型与 `CudaKernel` 实现**：宿主通过这个实现拿到内核的 PTX 名，从而能 `cuModuleGetFunction`。

> 为什么前缀里要带一段 `246e25db` 哈希？因为用户可能偶然写出 `fn cuda_oxide_kernel_foo()`，那就被误当成内核了。带哈希后这种偶然碰撞几乎不可能（见 4.5）。

#### 4.1.2 核心流程

```text
#[kernel] pub fn vecadd(...) { ... }
        │
        ▼  kernel() 宏（lib.rs:3172）
   ┌────────────────────────────────────────────┐
   │ 1. 拒绝保留名 / impl trait 参数             │
   │ 2. 处理循环上的 #[unroll]                   │
   │ 3. 判断是否泛型                             │
   └────────────────────────────────────────────┘
        │ 非泛型
        ▼  generate_simple_kernel()（lib.rs:3915）
   ┌────────────────────────────────────────────┐
   │ new_name = KERNEL_PREFIX + fn_name         │
   │ input.sig.ident = new_name                 │
   │ emit: #[unsafe(no_mangle)] fn <new_name>   │
   │ emit: <CudaKernel impl for __vecadd_CudaKernel> │
   └────────────────────────────────────────────┘
```

泛型内核（`#[kernel] pub fn map<F: GpuFn>(...)`）走 `generate_generic_kernel_no_instantiation` 或 `generate_generic_kernel`，会额外生成 `<name>_ptx_name::<...>()` 辅助函数和单态化强制逻辑。本讲聚焦非泛型路径，泛型/闭包内核留到 u2-l6 详讲。

#### 4.1.3 源码精读

`kernel` 宏的入口先做参数校验与分支选择（[crates/cuda-macros/src/lib.rs:3171-3251](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L3171-L3251)）：

```rust
pub fn kernel(attr: TokenStream, item: TokenStream) -> TokenStream {
    let args = parse_macro_input!(attr as KernelArgs);
    let mut input = parse_macro_input!(item as ItemFn);

    if let Some(err) = reject_reserved_name(&input.sig.ident) { return err; }
    // ... #[unroll] 改写、泛型判断 ...
    if has_generics { generate_generic_kernel(input, args.instantiate_types) }
    else { generate_simple_kernel(input) }
}
```

注意第一道关 `reject_reserved_name`：任何以保留根 `cuda_oxide_` 开头的用户函数名都会被直接拒绝，这是命名契约在宏侧的第一道防线。

非泛型内核的实际改写在 `generate_simple_kernel`（[crates/cuda-macros/src/lib.rs:3915-3941](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L3915-L3941)）：

```rust
fn generate_simple_kernel(mut input: ItemFn) -> TokenStream {
    inject_thread_index_scope(&mut input);
    let fn_name = input.sig.ident.clone();
    let new_name = format_ident!("{}{}", KERNEL_PREFIX, fn_name);   // 加保留前缀
    let original_fn = input.clone();
    input.sig.ident = new_name;
    // PTX 入口名仍是未加前缀的用户名；collector 在生成 PTX 时剥掉前缀。
    let ptx_entry_name = fn_name.to_string();
    let cuda_kernel_impl = generate_cuda_kernel_impl(&fn_name, &ptx_entry_name, &original_fn);

    let expanded = quote! {
        #[unsafe(no_mangle)]
        #input                       // 改名后的函数本体（device 代码）
        #cuda_kernel_impl            // 宿主侧 CudaKernel 实现 + 见证类型
    };
    TokenStream::from(expanded)
}
```

要点：

- `new_name = KERNEL_PREFIX + fn_name` —— 这里把 `vecadd` 拼成 `cuda_oxide_kernel_246e25db_vecadd`。
- `inject_thread_index_scope(&mut input)` —— 给函数体注入一个隐藏的线程索引作用域 token（与 `ThreadIndex` 见证类型配合，见 u2-l2）。
- 见证类型名由 `cuda_kernel_marker_name` 决定（[crates/cuda-macros/src/lib.rs:3102-3104](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L3102-L3104)），即 `__<name>_CudaKernel`：

```rust
fn cuda_kernel_marker_name(fn_name: &Ident) -> Ident {
    format_ident!("__{}_CudaKernel", fn_name)
}
```

`KERNEL_PREFIX` 的取值在保留符号 crate 里（[crates/reserved-oxide-symbols/src/lib.rs:78](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs#L78)），是个 `pub const`：

```rust
pub const KERNEL_PREFIX: &str = "cuda_oxide_kernel_246e25db_";
```

README 里有一张简洁的对照表说明「kernel vs device」的区别（[crates/cuda-macros/README.md:78-83](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/README.md#L78-L83)），建议读完本节后回去扫一眼，巩固「入口 vs 非入口」的概念。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `#[kernel]` 改写出的保留前缀函数名。

**操作步骤**：

1. 打开 [crates/rustc-codegen-cuda/examples/vecadd/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs)，找到其中的 `#[kernel] fn vecadd`。
2. 运行宏展开（**待本地验证**，需要 nightly 与 `cargo-expand`）：
   ```bash
   cd crates/rustc-codegen-cuda/examples/vecadd
   cargo +nightly-2026-04-03 expand
   ```
   若该示例未启用 `cargo-expand`，可临时在示例的 `Cargo.toml` 加 `cargo-expand` 后再跑。
3. 在展开输出里搜索 `cuda_oxide_kernel_246e25db_vecadd`。

**需要观察的现象**：展开结果里会出现一个带 `#[unsafe(no_mangle)]` 的 `fn cuda_oxide_kernel_246e25db_vecadd(...)`，以及一个 `struct __vecadd_CudaKernel` 和它的 `impl CudaKernel`。原始的 `fn vecadd` 这个名字已经不存在了——它被改名了。

**预期结果**：你能在展开文本里同时看到「保留前缀函数」和「`__vecadd_CudaKernel` 见证类型」，从而确认 4.1.2 流程图里的两步产物。

#### 4.1.5 小练习与答案

**练习 1**：如果用户写 `#[kernel] fn cuda_oxide_helper(...)`，会发生什么？为什么？

> **答案**：编译失败。`kernel` 宏一开头就调用 `reject_reserved_name`，任何以保留根 `cuda_oxide_` 开头的名字都被拒。这是为了让用户永远无法手工「伪造」一个被 codegen 后端识别的 device 符号——device 代码只能由宏通过 `KERNEL_PREFIX` 生成。

**练习 2**：为什么 PTX 入口名是干净的 `vecadd`，而 Rust 里的函数名却带 `cuda_oxide_kernel_246e25db_` 前缀？

> **答案**：前缀是宿主/后端识别用的「信号灯」，但 PTX 里我们想要可读、稳定的 `.entry vecadd`。所以宏只在前缀上做识别，PTX 入口名 `ptx_entry_name` 仍取未加前缀的 `fn_name.to_string()`（见 `generate_simple_kernel`）。collector 在生成 PTX 时会剥掉 `KERNEL_PREFIX`（参考 reserved-oxide-symbols 的 `kernel_base_name` 提取器，4.5 节）。

---

### 4.2　`#[cuda_module]` 生成 `LoadedModule`（含嵌套模块视图）

#### 4.2.1 概念说明

`#[kernel]` 只负责「打标记」，它本身不能让宿主启动内核。真正把内核变成「可加载、可启动的宿主对象」的是 `#[cuda_module]`：

- 它包住一个 inline `mod`，扫描里面所有 `#[kernel]`。
- 为这个 mod 生成一个 `pub struct LoadedModule { ... }`，内含每个非泛型内核的 `CudaFunction` 句柄。
- 生成 `load()` / `load_named()` / `from_module()`（以及 async 版本）加载器。
- 为每个内核在 `impl LoadedModule` 上生成一个同名启动方法（如 `module.vecadd(...)`）。

#324 之后，`#[cuda_module]` 还会**递归扫描嵌套 inline 模块**里的 kernel：每个拥有 kernel（或包含更深 kernel 命名空间）的子 mod 都会得到自己的 `LoadedModule` 视图，子视图通过 `LoadedModule::from_parent(&parent_view)` 从父视图借用同一个已加载的 CUDA 模块。所有视图共享同一个 `CudaModule` 与同一个泛型函数缓存。

#### 4.2.2 核心流程

```text
#[cuda_module] mod kernels {
    pub mod init { #[kernel] fn fill_index(...) {} }   // 嵌套
    #[kernel] fn top(...) {}                            // 直接
}
        │
        ▼  cuda_module()（lib.rs:344）→ expand_cuda_module()
   transform_cuda_module_items(items, ...)
        │   递归：对每个 Item::Mod（inline）继续下钻
        ▼
   汇总 direct_kernels + descendant_kernels
        │
        ├── 根 mod：生成 LoadedModule + load* + from_module + 各内核方法
        └── 每个含 kernel 的子 mod：generate_nested_cuda_module_support()
                    └── 生成 LoadedModule { fn from_parent(&super::LoadedModule) { ... } }
```

关键不变量：**PTX 入口符号始终是裸函数名**（不带命名空间）。因此一个 `#[cuda_module]` 树内，所有 kernel 的名字必须**全树唯一**（包括 cfg-gated 的替代品）；宏会主动拒绝重名，否则运行时会加载到错误的 entry。

#### 4.2.3 源码精读

`#[cuda_module]` 入口（[crates/cuda-macros/src/lib.rs:343-359](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L343-L359)）：

```rust
#[proc_macro_attribute]
pub fn cuda_module(attr: TokenStream, item: TokenStream) -> TokenStream {
    if !attr.is_empty() { /* 暂不接受参数 */ }
    let input = parse_macro_input!(item as ItemMod);
    match expand_cuda_module(input) {
        Ok(tokens) => tokens.into(),
        Err(error) => error.to_compile_error().into(),
    }
}
```

`expand_cuda_module` 的最终产物是一整个 mod，里面包含 `LoadedModule` 结构体和它的 `impl` 块（[crates/cuda-macros/src/lib.rs:852-892](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L852-L892)）：

```rust
Ok(quote! {
    #(#module_attrs)*
    #vis mod #ident {
        #(#module_items)*
        #(#launch_contract_impls)*            // 4.3 节讲

        #[derive(Clone, Debug)]
        pub struct LoadedModule {
            __module: Arc<CudaModule>,
            __generic_functions: Arc<Mutex<HashMap<&'static str, CudaFunction>>>,
            #(#function_fields)*              // 每个非泛型内核一个 CudaFunction 字段
            #(#constant_fields)*
        }

        #load_definition                      // load()（4.4 节讲它是否 unsafe）
        #load_named_definition
        #from_module_definition
        #async_module_items

        impl LoadedModule {
            pub fn as_cuda_module(&self) -> &Arc<CudaModule> { &self.__module }
            #(#launch_methods)*               // 每个内核一个启动方法
            #(#prepare_launch_methods)*       // 签约内核的 prepare_*（4.3 节）
            #(#constant_resolver_methods)*
            #(#set_constant_methods)*
            #async_launch_methods
        }
    }
})
```

嵌套模块的递归处理在 `transform_cuda_module_items`（[crates/cuda-macros/src/lib.rs:908-977](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L908-L977)）。核心是 `Item::Mod` 分支：只有 **inline** 子模块（`mod x { ... }`，即 `content` 非空）才会被下钻；文件型 `mod x;` 和 `include!` 因为属性宏拿不到文件内容而被原样保留、但**不**生成 launcher（注释明确说明了这个限制）：

```rust
Item::Mod(item_mod) => {
    let Some((_brace, nested_items)) = &item_mod.content else {
        // 属性宏只能拿到声明本身，拿不到文件内容；原样保留但不假装发现了 kernel。
        transformed_items.push(item.clone());
        continue;
    };
    module_path.push(item_mod.ident.clone());
    // ... 继续递归 transform_cuda_module_items(nested_items, ...) ...
    module_path.pop();
    descendant_kernels.extend(nested.kernels);
    transformed_items.push(Item::Mod(transformed_mod));
}
```

每个含 kernel 的子命名空间由 `generate_nested_cuda_module_support` 生成它自己的 `LoadedModule`，并提供 `from_parent` 构造器（[crates/cuda-macros/src/lib.rs:1102-1179](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L1102-L1179)）：

```rust
impl LoadedModule {
    /// 把本命名空间的 launcher 绑定到「由父命名空间加载的模块」。
    pub fn from_parent(parent: &super::LoadedModule) -> Result<Self, DriverError> {
        let module = parent.as_cuda_module().clone();      // 共享同一个 CudaModule
        Ok(Self {
            __module: module.clone(),
            __generic_functions: parent.__generic_functions.clone(), // 共享泛型缓存
            #(#function_initializers)*                     // load_function(PTX_NAME)
        })
    }
    pub fn as_cuda_module(&self) -> &Arc<CudaModule> { &self.__module }
    #(#launch_methods)*
    #(#prepare_launch_methods)*
    #async_launch_methods
}
```

注意两点：① `from_parent` 通过 `super::LoadedModule` 引用**直接父**视图，所以三级嵌套要从它的直接父（而非根）构造；② `__module` 是 `clone()` 出来的 `Arc`，所有视图指向同一个底层 CUDA 模块，多次绑定不会重复加载 PTX。

为了让生成的命名不与用户冲突，宏保留了若干名字：每个含 kernel 的命名空间里 `LoadedModule` 与 `as_cuda_module` 被占用，嵌套命名空间额外占用 `from_parent`（[crates/cuda-macros/src/lib.rs:1010-1032](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L1010-L1032)）。

#### 4.2.4 代码实践

**实践目标**：用 `cuda_module_nested` 示例验证「跨命名空间共享同一个已加载模块」。

**操作步骤**：

1. 阅读 [crates/rustc-codegen-cuda/examples/cuda_module_nested/src/main.rs:23-85](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cuda_module_nested/src/main.rs#L23-L85)，看清楚四个 kernel 分布在三层嵌套里：`init::fill_index`、`scale::scale_by`、`offset::offset_by`（一层）、`post::double::double_all`（两层）。
2. 阅读 [main.rs:99-110](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cuda_module_nested/src/main.rs#L99-L110) 的视图构建：
   ```rust
   let module = kernels::load(&ctx)?;                                  // 根视图
   let init    = kernels::init::LoadedModule::from_parent(&module)?;   // 一级
   let double  = kernels::post::double::LoadedModule::from_parent(&post)?; // 二级，从 post 构造
   ```
3. （可选，需 GPU）`cargo oxide run cuda_module_nested`。

**需要观察的现象**：`kernels::load(&ctx)` 只调用一次、只加载一次 PTX；之后所有 `from_parent` 都只是从同一个 `module` 派生子视图。最深的 `double` 是从它的直接父 `post` 构造的，而不是从根 `module`。

**预期结果**：程序输出 `✓ SUCCESS: root-loaded nested inline kernels all ran`。注意示例根 mod **故意没有**直接 kernel（只有 `init`/`scale`/`offset`/`post` 子模块），这验证了「根 `load()` 仍能 pin 住完全由后代拥有的 artifact」。

> 注：本示例的启动调用全部包在 `unsafe { ... }` 中（[main.rs:114-127](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cuda_module_nested/src/main.rs#L114-L127)），因为这些 kernel 都没有 `#[launch_contract]`，走的是 raw `LaunchConfig` 路径（见 4.4 节）。

#### 4.2.5 小练习与答案

**练习 1**：如果改成 `pub mod init;`（文件型模块）并在 `init.rs` 里放 `#[kernel]`，会发生什么？

> **答案**：该 kernel **不会**生成 launcher。`transform_cuda_module_items` 的 `Item::Mod` 分支检查 `content`：文件型模块的 `content` 是 `None`，于是原样保留声明、跳过递归。源码注释解释了原因——属性宏拿不到文件内容，重现 rustc 的模块加载器既不完整也不卫生。所以自动启动的嵌套 kernel 必须放在 inline 模块里。

**练习 2**：为什么同一个 `#[cuda_module]` 树里，两个不同命名空间下的同名 kernel 会被拒绝？

> **答案**：因为 PTX 入口符号是裸函数名（不带命名空间），全树共享一个 entry 命名空间。如果两个 `fn reduce` 都进 PTX，运行时 `cuModuleGetFunction` 会拿到歧义的 entry。宏的 `reject_reserved_loaded_module_methods` 与重名检查（配合 `cuda_module_duplicate_nested_kernel` compile_fail 测试）在编译期就拦下这种情况，而不是冒着「加载到错误的 entry」的风险。

---

### 4.3　`#[launch_contract]` 与 `prepare_*` → `PreparedLaunch` 受检启动

#### 4.3.1 概念说明

这是本讲**最重要**也是 #318 引入的最大变化。先回忆痛点：

- raw `LaunchConfig`（u1-l4）只是一组 `(grid, block, shared_mem)` 数字，**它不知道**哪个内核会消费它。你可以用 `LaunchConfig::for_num_elems(N)` 配一个 1D grid 去启动一个其实是 2D 索引的内核，编译器拦不住你——所以 raw 启动必须 `unsafe`，由调用方写 `SAFETY:` 注释自证。

`#[launch_contract(...)]` 让**内核作者**在源码里声明这个内核的启动假设（域维度、block 形状、动态共享内存范围与对齐、最低算力），然后宏把这些假设编码进一个**特化品牌化的见证类型**。宿主端必须先调用 `module.prepare_<name>(config)` 在**活设备**上校验这些假设，拿到一枚 `PreparedLaunch<__name_CudaKernel>` 证明；只有拿着这枚证明，才能调用**安全**的 `module.<name>(&stream, &prepared, ...)` 入队。

```text
prepare_reduce: dimensions + live CUDA limits -> PreparedLaunch<reduce>   （校验过）
reduce:         PreparedLaunch<reduce>         -> enqueue                 （安全）
reduce_unchecked: raw LaunchConfig             -> unsafe expert path      （4.4 节）
```

> 这张三行表来自 [crates/cuda-macros/README.md:124-128](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/README.md#L124-L128)，是理解整套机制的最佳速记。

#### 4.3.2 核心流程

契约属性本身很薄，真正的工作发生在 `#[cuda_module]` 内部：

```text
#[launch_contract(domain=1, block=(256,1,1), ...)]   ← 属性宏：几乎不做什么
pub fn reduce(...) { ... }
        │
        ▼ 注入 __dynamic_shared_alignment::<ALIGN>() 标记到函数体（让对齐对编译器可见）
        │
   被 #[cuda_module] 扫描到 launch_contract 字段：
        │
        ├── add_cuda_module_disjoint_contract_bounds()
        │     给 DisjointSlice 参数加 __LaunchContractDisjointSlice<E, DOMAIN> sealed bound
        │     （品牌化：本地伪造的 DisjointSlice 通不过）
        │
        ├── generate_cuda_module_launch_contract_impl()
        │     为 __reduce_CudaKernel 实现 KernelLaunchContract {
        │         type Config = LaunchConfig1D;     // 域=1 → 1D，rank 在类型里
        │         const SPEC: LaunchContractSpec::new("reduce", BlockRequirement::Exact(...), ...)
        │     }
        │
        └── generate_cuda_module_prepare_launch_methods()
              生成 module.prepare_reduce(config: LaunchConfig1D) -> PreparedLaunch<__reduce_CudaKernel>
              （内部 unsafe 调 PreparedLaunch::__prepare，活设备校验）

宿主使用：
   let prepared = module.prepare_reduce(LaunchConfig1D::new(blocks, 256, smem))?;
   module.reduce(&stream, &prepared, &input, &mut out)?;   // 安全！证明已被消费
```

为什么 `domain` 要显式写？因为通过 device helper 调用的内核会让 AST 推断失效，宏没法可靠地从函数体推断它是 1D 还是 2D。于是作者**声明** domain，宏再用 sealed bound 校验 `DisjointSlice` 的真实维度与声明一致（README 给出的对照见 [crates/cuda-macros/README.md:145-149](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/README.md#L145-L149)）：

```text
type Tile = Index2D<64>;  DisjointSlice<_, Tile>    + domain 2 -> 接受
type Tile = Index1D;      DisjointSlice<_, Tile>    + domain 2 -> 类型错误
本地 struct 名为 DisjointSlice                       -> 类型错误（品牌化拒绝）
```

#### 4.3.3 源码精读

**属性本身**几乎是 pass-through：`#[launch_contract]` 宏只做一件事——当声明了动态共享内存时，往函数体最前面注入一个对齐标记（[crates/cuda-macros/src/lib.rs:4294-4312](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L4294-L4312)）：

```rust
#[proc_macro_attribute]
pub fn launch_contract(attr: TokenStream, item: TokenStream) -> TokenStream {
    let args = parse_macro_input!(attr as LaunchContractArgs);
    let mut input = parse_macro_input!(item as ItemFn);
    inject_launch_contract_alignment_marker(&args, &mut input);  // 注入对齐标记
    quote! { #input }.into()
}

fn inject_launch_contract_alignment_marker(args: &LaunchContractArgs, input: &mut ItemFn) {
    if dynamic_shared_max(args.dynamic_shared) != 0 {
        let alignment = args.dynamic_shared_alignment as usize;
        let alignment_marker: syn::Stmt = parse_quote! {
            ::cuda_device::shared::__dynamic_shared_alignment::<#alignment>();
        };
        input.block.stmts.insert(0, alignment_marker);   // 作为第一条语句插入
    }
}
```

这个 `__dynamic_shared_alignment::<ALIGN>()` 调用让编译器看到「这枚内核至少需要 ALIGN 对齐」，从而把契约声明的对齐与函数体（或可达本地 helper）内 `DynamicSharedArray<T, ALIGN>` 的更高对齐请求**合并**（见 u2-l3）。

真正解析契约字段的是 `LaunchContractArgs::parse`（[crates/cuda-macros/src/lib.rs:399-459](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L399-L459)），支持的字段是：`domain`、`block`、`dynamic_shared`、`dynamic_shared_range`、`dynamic_shared_alignment`、`min_compute_capability`。任何未知字段都会被拒绝，`dynamic_shared` 与 `dynamic_shared_range` 互斥。

`#[cuda_module]` 在扫描到每个 kernel 时调用 `cuda_module_launch_contract` 做编译期校验（[crates/cuda-macros/src/lib.rs:1727-1805](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L1727-L1805)）。这里有几条硬规则：

- 签约内核**不能**用 `&mut [T]` 参数，必须用 `DisjointSlice<T, IndexSpace>` 让写所有权与启动域显式化（错误信息直接引导你改用 `DisjointSlice`）。
- 必须有精确 `block = (x,y,z)` 或配套的 `#[launch_bounds(max_threads)]`，否则报「requires either an exact block or launch_bounds」。
- 精确 block 的总线程数不能超过 `#[launch_bounds]` 上限。
- `domain` 必须是 1/2/3，且 block/cluster 各维不能越出该域。

```rust
if let Some(param) = params.iter().find(|param| param.mutable_slice) {
    return Err(syn::Error::new(param.name.span(),
        "contracted kernels cannot take `&mut [T]`; use `DisjointSlice<T, IndexSpace>` ..."));
}
// ...
if args.exact_block.is_none() && launch_bounds.is_none() {
    return Err(syn::Error::new_spanned(attr,
        "launch_contract requires either an exact `block = (x, y, z)` or #[launch_bounds(max_threads)]"));
}
```

**品牌化 sealed bound** 是契约安全的关键。扫描到契约后，宏调用 `add_cuda_module_disjoint_contract_bounds` 给每个 `DisjointSlice` 参数的 where 子句加一条对 `__LaunchContractDisjointSlice` 的约束（[crates/cuda-macros/src/lib.rs:1985-2002](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L1985-L2002)）：

```rust
generics.make_where_clause().predicates.push(parse_quote! {
    for<#bound_lifetime> #device_ty:
        ::cuda_device::__LaunchContractDisjointSlice<#element_ty, #domain>
});
```

这个 trait 在 cuda-device 里是 sealed 的（[crates/cuda-device/src/disjoint.rs:117-139](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/disjoint.rs#L117-L139)），只为「真正的 `DisjointSlice` + 匹配的 IndexSpace」实现：

```rust
pub trait __LaunchContractDisjointSlice<Element, const DOMAIN: u8>: sealed::Sealed { /* ... */ }
impl<'a, T> __LaunchContractDisjointSlice<T, 1> for DisjointSlice<'a, T, Index1D> {}
impl<'a, T, const ROW_STRIDE: usize> __LaunchContractDisjointSlice<T, 2>
    for DisjointSlice<'a, T, Index2D<ROW_STRIDE>> {}
// ...
```

因为 trait sealed，用户无法为自己的本地 `struct DisjointSlice` 实现它，所以「用一个长得像 `DisjointSlice` 的本地类型骗过契约」会被类型系统拒绝（见 compile_fail 测试 `launch_contract_fake_disjoint_slice.rs`，4.4 节）。这就是「品牌化」的含义：类型名一样不算数，必须真的是 cuda-device 的那个 sealed trait 的实现者。

接着宏为见证类型实现 `KernelLaunchContract`，把契约编进 `const SPEC`（[crates/cuda-macros/src/lib.rs:2045-2112](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L2045-L2112)）：

```rust
impl #impl_generics ::cuda_core::KernelLaunchContract for #marker_ty #where_clause {
    type Config = #config_ty;   // domain 1/2/3 → LaunchConfig1D/2D/3D
    const SPEC: ::cuda_core::LaunchContractSpec =
        ::cuda_core::LaunchContractSpec::new(#kernel_name, #block, #dynamic_shared)
            #cluster
            #cooperative
            #compute_capability;
}
```

注意 `type Config` 把 **rank 锁进类型**：domain=1 的内核只接受 `LaunchConfig1D`，你没法把 3D config 喂给它。`LaunchConfig1D/2D/3D` 是 sealed 的 `KernelLaunchConfig` 实现（[crates/cuda-core/src/launch.rs:59-103](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L59-L103)），下游无法伪造一个绕过「尾随维度恒为 1」的配置。

随后宏生成 `prepare_<name>` 方法（[crates/cuda-macros/src/lib.rs:2120-2194](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L2120-L2194)），它内部 `unsafe` 调用 `PreparedLaunch::__prepare`（宏生成的不安全由「SPEC 是宏如实生成的」这一事实承担）：

```rust
#vis fn #prepare_name #impl_generics (
    &self,
    #config: <#marker_ty as ::cuda_core::KernelLaunchContract>::Config,   // 例：LaunchConfig1D
) -> Result<::cuda_core::PreparedLaunch<#marker_ty>, ::cuda_core::LaunchContractError>
{
    #function_binding
    unsafe {
        ::cuda_core::PreparedLaunch::<#marker_ty>::__prepare(#function.clone(), #config)
    }
}
```

活设备校验发生在 `PreparedLaunch::__prepare`（[crates/cuda-core/src/launch.rs:786-874](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L786-L874)）：先 `validate_static(SPEC, raw)` 查形状/对齐，再从活 context 取 `launch_limits()`、`max_threads_per_block`、共享内存上限、算力、集群/协作支持等，逐项校验：

```rust
pub unsafe fn __prepare(function: CudaFunction, config: C::Config)
    -> Result<Self, LaunchContractError>
{
    let raw = config.__raw();
    validate_static(C::SPEC, raw)?;
    let context = function.context();
    let limits = context.launch_limits()?;
    let function_max_threads = function.max_threads_per_block()?;
    // ... 校验 block 不超 limits、共享内存总量、min_compute_capability、cluster、cooperative ...
}
```

校验全部通过才返回 `PreparedLaunch<C>`。`PreparedLaunch` 用一个 `PhantomData<fn(C) -> C>` 把见证类型 `C` 绑进自己（[crates/cuda-core/src/launch.rs:786-790](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L786-L790)），于是 `module.reduce(&stream, &prepared, ...)` 的签名 `prepared: &PreparedLaunch<__reduce_CudaKernel>` 只能消费「reduce 这个内核」的证明，**别的内核的证明塞不进去**——这就是「特化品牌化证明」。

最后，签约内核的**安全启动方法**（消费 `PreparedLaunch`）由 `generate_cuda_module_prepared_launch_method` 生成（[crates/cuda-macros/src/lib.rs:2251-2307](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L2251-L2307)）：

```rust
#vis #unsafety fn #fn_name #impl_generics (
    &self,
    #stream: &::cuda_core::CudaStream,
    #prepared: &::cuda_core::PreparedLaunch<#marker_ty>,   // 消费品牌化证明
    #(#params),*
) -> Result<(), ::cuda_core::LaunchContractError>
{
    #prepared.validate_stream(#stream)?;
    let #function = #prepared.function();
    let #config = #prepared.__raw_config();
    // ... 装填参数、launch ...
}
```

注意这里的 `#unsafety` 来自内核作者原始的 `fn` 签名——如果作者写的是普通 `pub fn reduce`，生成的方法就是**安全**的（`prepare` 已经把证明义务履行掉了）。这也正是 README 所说「受检启动在该内核本身安全时就是安全的」。

#### 4.3.4 代码实践

**实践目标**：给一个 `#[cuda_module]` 内核加 `#[launch_contract]`，观察宏生成了 `prepare_*` 与受检同名方法。

**操作步骤**：

1. 复制 vecadd 示例到一个新目录（或临时改 vecadd），把内核改成这样（**示例代码**，基于 README 的 reduce 例子改写）：
   ```rust
   use cuda_device::{cuda_module, kernel, thread, DisjointSlice};

   #[cuda_module]
   mod kernels {
       use super::*;
       #[kernel]
       #[launch_contract(domain = 1, block = (256, 1, 1))]
       pub fn fill(mut out: DisjointSlice<f32>) {
           if let Some((e, idx)) = out.get_mut_indexed() {
               *e = idx.get() as f32;
           }
       }
   }
   ```
2. 在宿主 `main` 里写：
   ```rust
   let module = unsafe { kernels::load(&ctx)?; };     // 签约模块的 load 是 unsafe，见 4.4
   let prepared = module.prepare_fill(cuda_core::LaunchConfig1D::new(blocks, 256, 0))?;
   module.fill(&stream, &prepared, &mut out_dev)?;   // 安全，无需 unsafe 块
   ```
3. 用 `cargo expand`（**待本地验证**）查看宏展开，搜索 `prepare_fill` 与 `KernelLaunchContract`。

**需要观察的现象**：

- 展开里有 `fn prepare_fill(&self, config: LaunchConfig1D) -> Result<PreparedLaunch<__fill_CudaKernel>, ...>`。
- 展开里有 `impl KernelLaunchContract for __fill_CudaKernel { type Config = LaunchConfig1D; const SPEC = ... }`。
- 宿主调用 `module.fill(...)` 的签名第二个参数是 `&PreparedLaunch<__fill_CudaKernel>`，且该方法**不带 `unsafe`**。

**预期结果**：编译通过；如果你故意把 `prepare_fill` 的参数换成 `LaunchConfig2D::new(...)`，编译失败——因为 `type Config = LaunchConfig1D`，rank 在类型层就被锁死了。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `#[launch_contract]` 属性宏本身几乎不做任何代码改写，所有工作却仍然「生效」了？

> **答案**：契约属性主要被 `#[cuda_module]` 读取并解释。属性宏只做了一件实质的事：当声明了动态共享内存时，注入 `__dynamic_shared_alignment::<ALIGN>()` 标记（4.3.3 第一段源码）。真正的「把契约变成受检 API」是 `#[cuda_module]` 在 `cuda_module_launch_contract` 里读取这些属性、加 sealed bound、生成 `KernelLaunchContract` impl 与 `prepare_*` 方法完成的。换句话说，`#[launch_contract]` 是**声明**，`#[cuda_module]` 是**执行**。

**练习 2**：`prepare_reduce` 返回的 `PreparedLaunch<__reduce_CudaKernel>` 能不能拿去启动另一个签约内核 `map`？为什么？

> **答案**：不能。`PreparedLaunch<C>` 把见证类型 `C` 绑进了自己的类型（`PhantomData<fn(C) -> C>`）。`module.map` 的签名要求 `&PreparedLaunch<__map_CudaKernel>`，而 `reduce` 的证明类型是 `__reduce_CudaKernel`，类型不匹配，编译失败。这就是「特化品牌化」——证明与内核一一绑定，无法串用。

**练习 3**：如果契约写 `block = (256, 1, 1)` 但内核同时标了 `#[launch_bounds(128)]`，会发生什么？

> **答案**：编译失败。`cuda_module_launch_contract` 会检查「精确 block 的总线程数不能超过 launch_bounds 上限」（4.3.3 第二段源码）。256 > 128，于是报 `launch_contract block (256, 1, 1) has 256 threads, exceeding #[launch_bounds(128)]`。

---

### 4.4　`unsafe` loader 与 `_unchecked` 专家路径

#### 4.4.1 概念说明

`#[launch_contract]` 让「准备 + 启动」变安全了，但它也改变了模块**其他**部分的安全边界。三条规则要记牢：

1. **未签约内核的同名方法也是 `unsafe`。** 即便没写契约，`module.vecadd(...)` 仍然是 `unsafe fn`，因为它吃 raw `LaunchConfig`，没有证明把形状与内核绑定。raw 异步方法（`vecadd_async`、`vecadd_async_owned`）同样 `unsafe`。
2. **签约模块的所有 loader 都是 `unsafe`。** 一旦模块里有任何签约内核，`load`、`load_named`、`from_module`（以及 async 版）全部变成 `unsafe fn`。调用方必须证明「这次绑定加载的代码符合模块声明的 ABI 与资源语义」。这是一次性证明：绑定之后，`prepare_*` 与受检启动就安全了。
3. **签约内核另有 `_unchecked` 专家逃生口。** `module.<name>_unchecked(&stream, raw_config, ...)` 是 `unsafe` 的，直接吃 raw `LaunchConfig`，跳过 `prepare` 校验。它是给「我知道我在干什么」的专家准备的。

#### 4.4.2 核心流程

```text
模块含签约内核？
   ├── 是 → load/load_named/from_module/load_async* 全部 unsafe fn
   │        （调用方自证：绑定代码符合模块声明的 ABI/资源语义；一次性）
   │
   └── 否 → load/load_named/from_module 都是安全 fn

每个内核的启动方法：
   kernel 有契约？
   ├── 是 → module.<name>(&stream, &PreparedLaunch<...>, args)  [安全，消费证明]
   │        module.<name>_unchecked(&stream, raw, args)         [unsafe 专家路径]
   │        module.prepare_<name>(config) -> PreparedLaunch     [内部 unsafe，SPEC 由宏如实生成]
   │
   └── 否 → module.<name>(&stream, raw, args)                   [unsafe，raw 无证明]
```

#### 4.4.3 源码精读

**未签约内核的 raw 方法**生成于 `generate_cuda_module_legacy_launch_method`，签名带 `unsafe`（[crates/cuda-macros/src/lib.rs:2204-2244](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L2204-L2244)）：

```rust
#[doc = "Launches this kernel with an unverified raw launch configuration."]
#[doc = "# Safety"]
#[doc = "The launch dimensions and resources must satisfy every indexing, ..."]
#vis unsafe fn #fn_name #impl_generics (
    &self,
    #stream: &::cuda_core::CudaStream,
    #config: ::cuda_core::LaunchConfig,           // raw，无证明
    #(#params),*
) -> Result<(), ::cuda_core::DriverError>
```

这就是为什么 vecadd、cuda_module_nested 里所有启动都包在 `unsafe { ... }` 中。

**签约内核的 `_unchecked` 逃生口**与安全方法成对生成（[crates/cuda-macros/src/lib.rs:2309-2327](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L2309-L2327)）：

```rust
#[doc = "Unchecked launch escape hatch for this contracted kernel."]
#[doc = "# Safety"]
#[doc = "The caller must uphold the kernel's declared geometry, resource, capability, and context contract."]
#vis unsafe fn #unchecked_name #impl_generics (       // <name>_unchecked
    &self,
    #stream: &::cuda_core::CudaStream,
    #config: ::cuda_core::LaunchConfig,              // raw，绕过 prepare 校验
    #(#params),*
) -> Result<(), ::cuda_core::DriverError>
```

**loader 的 unsafe 切换**由 `has_launch_contract` 标志驱动。展开代码里 `load_definition` 在签约时是这样（[crates/cuda-macros/src/lib.rs:741-767](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L741-L767)）：

```rust
let load_definition = if has_launch_contract {
    quote! {
        /// Loads this package's embedded artifact for a contracted module.
        ///
        /// # Safety
        ///
        /// For a non-generic module, the selected package bundle must be
        /// the artifact compiled from this `cuda_module`; package names are
        /// not yet unique across all library and binary targets. For a
        /// generic module, the merged PTX set must contain each matching
        /// specialization and no conflicting entry definition.
        pub unsafe fn load(ctx: &Arc<CudaContext>) -> Result<LoadedModule, EmbeddedModuleError> {
            unsafe { load_named(ctx, env!("CARGO_PKG_NAME")) }
        }
    }
} else {
    quote! {
        pub fn load(ctx: &Arc<CudaContext>) -> Result<LoadedModule, EmbeddedModuleError> {
            load_named(ctx, env!("CARGO_PKG_NAME"))
        }
    }
};
```

`load_named` 与 `from_module` 同样按这个标志切换（[crates/cuda-macros/src/lib.rs:768-836](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L768-L836)）。README 解释了为什么 loader 必须不安全（[crates/cuda-macros/README.md:159-166](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/README.md#L159-L166)）：当前 bundle 按**包粒度**识别，`load()` 无法仅凭名字区分一个库 artifact 和同包的 bin artifact；泛型加载还会合并所有 PTX bundle。因此调用方必须证明「这次绑定的是符合模块声明 ABI/资源语义的代码」。这是**一次性**证明——绑定后，`prepare_*` 与受检启动就都安全了。

**品牌化的负向测试**。这套不安全性不是说说而已，而是有 compile_fail 测试固化的。`launch_contract_fake_disjoint_slice.rs` 故意定义一个本地 `struct DisjointSlice` 试图通过契约校验（[crates/cuda-macros/tests/compile_fail/launch_contract_fake_disjoint_slice.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/tests/compile_fail/launch_contract_fake_disjoint_slice.rs)）：

```rust
#[repr(C)]
pub struct DisjointSlice<'a, T, IndexSpace = Index1D> {   // 本地「伪造」的 DisjointSlice
    ptr: *mut T,
    len: usize,
    marker: PhantomData<(&'a mut T, IndexSpace)>,
}

#[cuda_module]
mod kernels {
    use super::*;
    #[kernel]
    #[launch_contract(domain = 1, block = (64, 1, 1))]
    pub fn lookalike(mut out: DisjointSlice<u32>) { ... }   // 试图用伪造类型签约
}
```

它编译失败，因为本地 `DisjointSlice` 没有实现 sealed 的 `__LaunchContractDisjointSlice`。同目录还有 `launch_contract_untrusted_loaders.rs`（固化 loader 的 unsafe 边界）、`launch_contract_misleading_index_alias.rs`、`launch_contract_reordered_disjoint_alias.rs` 等。cuda-core 那边还有 `tests/launch_contract/fail_wrong_brand.rs`、`fail_wrong_rank.rs`、`fail_private_construction.rs`、`fail_private_mutation.rs` 等，专门拦截「证明品牌错」「rank 错」「私造证明」「私改证明」。这些负向测试是安全契约的「防回归护栏」（更系统的梳理见 u7-l1）。

#### 4.4.4 代码实践

**实践目标**：亲手触发 loader / `_unchecked` / 品牌化的安全边界。

**操作步骤**（三个小实验，**待本地验证**，多数只需 `cargo check`）：

1. **观察 loader 的 unsafe**：给某个 `#[cuda_module]` 模块加上一个签约内核后，写 `let m = kernels::load(&ctx)?;`（不加 `unsafe`）。`cargo check` 应报「call to unsafe function requires unsafe block」。把它改成 `let m = unsafe { kernels::load(&ctx) }?;` 并补 `// SAFETY:` 注释后通过。
2. **对比三种启动**：对同一个签约内核 `reduce`，分别写出三条调用：
   - 受检：`let p = module.prepare_reduce(LaunchConfig1D::new(...))?; module.reduce(&stream, &p, ...)?;`
   - 专家：`unsafe { module.reduce_unchecked(&stream, LaunchConfig::for_num_elems(N), ...) }?;`
   - （错误的）混用：`module.reduce(&stream, &some_other_kernel_prepared, ...)` —— 编译失败，品牌不符。
3. **跑 compile_fail**：`cargo test -p cuda-macros --test compile_fail`，或直接读 `launch_contract_fake_disjoint_slice.rs`，确认本地伪造的 `DisjointSlice` 被拒。

**需要观察的现象**：

- 实验 1：签约后 loader 变 `unsafe`，编译器强制你写 `unsafe` 块与 `SAFETY:` 注释。
- 实验 2：受检路径无需 `unsafe`；`_unchecked` 需要；混用不同内核的 `PreparedLaunch` 编译失败。
- 实验 3：编译失败信息指向 `__LaunchContractDisjointSlice` trait 未实现。

**预期结果**：三条路径的安全义务与编译器强制一一对应，安全契约不能被「静默绕过」。

#### 4.4.5 小练习与答案

**练习 1**：为什么签约模块要把**所有** loader（不只是签约内核相关的）都变成 `unsafe`？

> **答案**：因为契约的安全性建立在「运行时绑定的设备代码确实符合模块声明的 ABI 与资源语义」之上。但 bundle 当前按包粒度识别，`load()` 无法仅凭名字区分库 artifact 与同包 bin artifact；泛型加载还会合并所有 PTX bundle。所以「这次绑定是否货真价实」只能由调用方证明——这就是 loader 的 unsafe 义务。这是一次性义务：绑定之后 `prepare_*` 与受检启动就安全了。

**练习 2**：`reduce`（受检）与 `reduce_unchecked` 都能启动同一个签约内核，它们的安全证明义务有何不同？

> **答案**：`prepare_reduce` 在活设备上一次性校验 block 形状、共享内存上限、算力、集群/协作支持等，产出 `PreparedLaunch<__reduce_CudaKernel>` 证明；之后 `module.reduce(&stream, &prepared, ...)` 消费这枚证明，**无需**调用方再写 `unsafe`。`reduce_unchecked` 跳过这一切，直接吃 raw `LaunchConfig`，调用方必须在 `unsafe` 块里**亲自**承担「几何/资源/能力/上下文都满足契约」的全部义务。

---

### 4.5　保留符号前缀：宏与 codegen 后端的命名契约

#### 4.5.1 概念说明

宏改写出的符号（`cuda_oxide_kernel_246e25db_vecadd` 等）不是「宏自己用的」，而是**跨越四个工位**的契约语言：

- **宏**（cuda-macros）生成带前缀的符号；
- **codegen 后端**（rustc-codegen-cuda）按前缀扫描、收集 device 代码；
- **MIR-lowering / LLVM-export** 按前缀剥名、生成干净的 PTX 符号；
- **运行时**（cuda-host）按前缀做 `cuModuleGetFunction` / artifact 锚解析。

如果任何一方对前缀的理解不一致，整个系统就崩了。所以 cuda-oxide 把这些前缀抽到一个 `publish = false` 的内部 crate `reserved-oxide-symbols`，作为「唯一真相源」。它三层 API：

- **Layer 1 常量**：`KERNEL_PREFIX`、`DEVICE_PREFIX` 等原始字符串。
- **Layer 2 builder**：`kernel_symbol("vecadd")` 等，给宏用。
- **Layer 3 谓词/提取器**：`is_kernel_symbol`、`kernel_base_name` 等，给消费侧用。

#### 4.5.2 核心流程

所有前缀共享一个保留根 `cuda_oxide_`，并带一段固定哈希 `246e25db`（`sha256("cuda_oxide_ + rust")` 截 8 字符）：

```text
RESERVED_ROOT        = "cuda_oxide_"                   用户名禁止以此开头（宏侧 reject_reserved_name）
HASH_SUFFIX          = "246e25db"                      让偶然碰撞几乎不可能
KERNEL_PREFIX        = "cuda_oxide_kernel_246e25db_"   #[kernel]
DEVICE_PREFIX        = "cuda_oxide_device_246e25db_"   #[device]
DEVICE_EXTERN_PREFIX = "cuda_oxide_device_extern_246e25db_"
INSTANTIATE_PREFIX   = "cuda_oxide_instantiate_246e25db_"
CONSTANT_PREFIX      = "cuda_oxide_const_246e25db_"
ARTIFACT_ANCHOR_PREFIX = "cuda_oxide_artifact_anchor_246e25db_"  锚符号，防 dead-strip
```

`DEVICE_PREFIX` 与 `DEVICE_EXTERN_PREFIX` 因为哈希后缀而**互斥**（一个符号不可能同时包含两者），所以消费侧不需要历史上那种「`contains(DEVICE_PREFIX) && !contains(DEVICE_EXTERN_PREFIX)`」的顺序判断，谓词内部已经处理了歧义。

#### 4.5.3 源码精读

常量层与文档注释（[crates/reserved-oxide-symbols/src/lib.rs:57-79](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs#L57-L79)）：

```rust
/// Reserved root that prefixes every cuda-oxide internal symbol.
pub const RESERVED_ROOT: &str = "cuda_oxide_";

/// Magic suffix ... sha256("cuda_oxide_ + rust") truncated to 8 hex chars.
pub const HASH_SUFFIX: &str = "246e25db";

/// Prefix added to `#[kernel]` functions for collector detection.
/// `#[kernel] fn vecadd(...)` becomes `fn cuda_oxide_kernel_246e25db_vecadd(...)`.
pub const KERNEL_PREFIX: &str = "cuda_oxide_kernel_246e25db_";
```

builder 层（[crates/reserved-oxide-symbols/src/lib.rs:140-194](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs#L140-L194)），宏侧拼名用：

```rust
pub fn kernel_symbol(base: &str) -> String { format!("{KERNEL_PREFIX}{base}") }
// kernel_symbol("vecadd") == "cuda_oxide_kernel_246e25db_vecadd"
```

谓词与提取器层（[crates/reserved-oxide-symbols/src/lib.rs:264-347](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs#L264-L347)），消费侧判断与剥名用。注意 `is_kernel_symbol` 用 `contains` 而非 `starts_with`，因此跨 crate 的 FQDN 形式（`kernel_lib::cuda_oxide_kernel_246e25db_scale`）也能被识别：

```rust
pub fn is_kernel_symbol(name: &str) -> bool { name.contains(KERNEL_PREFIX) }

pub fn kernel_base_name(name: &str) -> Option<&str> {
    name.find(KERNEL_PREFIX).map(|pos| &name[pos + KERNEL_PREFIX.len()..])
}
// kernel_base_name("cuda_oxide_kernel_246e25db_vecadd") == Some("vecadd")
```

特别要提的是 **artifact 锚符号**。它是为了解决「lib crate 的 `.oxart` 数据段被链接器 dead-strip 导致 `load()` 运行时报 `ModuleNotFound`」的问题（u3-l2 会详讲）。codegen 后端在 `.oxart` 数据段开头定义一个全局锚符号，宏生成的 `load_named()` 读取该符号地址——任何对 `load()` 的调用都会产生一个未定义引用，强制链接器把该 archive 成员拉出来（[crates/reserved-oxide-symbols/src/lib.rs:117-134](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs#L117-L134)）。v2 锚还混入 rustc 的 crate target 与 Cargo 的 bin target 名，防止同包的 lib/bin/example/test 互相满足对方的锚引用（[crates/reserved-oxide-symbols/src/lib.rs:228-249](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs#L228-L249)）：

```rust
pub fn artifact_anchor_symbol_v2(
    package_name: &str, package_version: &str, crate_name: &str, binary_name: Option<&str>,
) -> String { /* cuda_oxide_artifact_anchor_246e25db_v2_<pkg>_<ver>_<crate>[_bin_<bin>|_nonbin] */ }
```

crate 末尾有一组单测**钉死**了这些常量的值（[crates/reserved-oxide-symbols/src/lib.rs:460-468](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs#L460-L468)），并验证「`user_names_with_old_prefix_are_not_matched`」——即没有哈希后缀的旧形式（如 `cuda_oxide_kernel_evil`）绝不会被任何谓词匹配。这正是「哈希让命名契约不可伪造」的回归保障。

#### 4.5.4 代码实践

**实践目标**：用一个最小 Rust 程序直接调用 `reserved-oxide-symbols` 的 builder，验证前缀拼接与剥名。

**操作步骤**（**示例代码**，可在任意一个 examples 的 `examples/` 子目录或临时 `cargo run` 里跑）：

```rust
// 需要把 reserved-oxide-symbols 加入 dev-dependencies 才能跑；仅作演示
fn main() {
    use reserved_oxide_symbols::{kernel_symbol, kernel_base_name, is_kernel_symbol};
    let sym = kernel_symbol("vecadd");
    println!("{sym}");                                   // cuda_oxide_kernel_246e25db_vecadd
    assert!(is_kernel_symbol(&sym));
    assert_eq!(kernel_base_name(&sym), Some("vecadd"));
    // 跨 crate FQDN 也能识别
    assert_eq!(
        kernel_base_name("kernel_lib::cuda_oxide_kernel_246e25db_scale"),
        Some("scale"),
    );
    // 旧形式（无哈希）绝不被匹配
    assert!(!is_kernel_symbol("cuda_oxide_kernel_evil"));
}
```

或者更简单：直接运行该 crate 的内置 doctest——`cargo test -p reserved-oxide-symbols --doc`（**待本地验证**）。每个 builder/谓词的文档注释里都带了一条 `assert_eq!` doctest，跑一遍就能看到全部拼接结果。

**需要观察的现象**：所有带 `246e25db` 哈希的符号被正确识别与剥名；缺少哈希的 `cuda_oxide_kernel_evil` 被拒绝。

**预期结果**：doctest 全绿，证明「哈希后缀」这道防线在工作。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `is_kernel_symbol` 用 `contains(KERNEL_PREFIX)` 而不是 `starts_with`？

> **答案**：跨 crate 引用时，符号会带上路径限定符，例如 `kernel_lib::cuda_oxide_kernel_246e25db_scale`。它不以 `KERNEL_PREFIX` 开头，但确实包含它。用 `contains` 就能同时处理「裸符号」和「FQDN 符号」。`kernel_base_name` 用 `find` 定位前缀位置再截取，也是同理（见 4.5.3 源码）。

**练习 2**：假设有人把 `HASH_SUFFIX` 从 `246e25db` 改成别的值，但只改了 `reserved-oxide-symbols` 一处，会发生什么？

> **答案**：整个编译流水线断裂。宏用新哈希生成符号，但旧二进制（或未同步的 consumer crate）用旧哈希扫描/剥名，于是 device 代码识别不到、PTX 符号剥不干净、`cuModuleGetFunction` 找不到入口。所以 `hash_value_is_pinned` 单测（[crates/reserved-oxide-symbols/src/lib.rs:460-468](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs#L460-L468)）把哈希值钉死，改动它是对所有 cuda-oxide 制品的破坏性变更，必须刻意为之。这也解释了为什么要把前缀集中到唯一 crate——保证四方一致。

---

## 5. 综合实践

把本讲五个模块串起来，完成下面这个**「给 vecadd 加一个签约的 reduce 兄弟内核，并放进嵌套模块」**的小任务。

**背景**：你有一个 `#[cuda_module] mod kernels`，里面只有一个未签约的 `vecadd`。现在要：

1. **新增一个嵌套模块 `pub mod reduce`**，里面放一个**签约**的归约内核（演示 4.1 + 4.2 + 4.3）。
2. 正确处理因签约导致的 **loader unsafe 边界**（4.4）。
3. 用宏展开 / compile_fail 测试验证你的理解（4.3、4.4、4.5）。

**建议代码骨架**（**示例代码**）：

```rust
use cuda_core::{CudaContext, DeviceBuffer, LaunchConfig, LaunchConfig1D};
use cuda_device::{cuda_module, kernel, thread, DisjointSlice};

#[cuda_module]
mod kernels {
    use super::*;

    // 未签约内核：raw 启动，unsafe
    #[kernel]
    pub fn vecadd(a: &[f32], b: &[f32], mut c: DisjointSlice<f32>) {
        if let Some((e, idx)) = c.get_mut_indexed() {
            let i = idx.get();
            *e = a[i] + b[i];
        }
    }

    // 嵌套模块 + 签约内核
    pub mod reduce {
        use cuda_device::{DisjointSlice, kernel, launch_contract, thread};

        #[kernel]
        #[launch_contract(domain = 1, block = (256, 1, 1))]
        pub fn sum(input: &[f32], mut out: DisjointSlice<f32>) {
            // 这里只需演示签名与契约能编译；真实归约逻辑见 u5-l1 的 warp 归约
            let _ = (input, out);
        }
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let ctx = CudaContext::new(0)?;
    let stream = ctx.default_stream();

    // 因为模块含签约内核 → load 是 unsafe（一次性证明）
    let module = unsafe {
        // SAFETY: 绑定的就是本编译单元产出的 embedded bundle，ABI 与模块声明一致。
        kernels::load(&ctx)?
    };

    // vecadd：未签约，raw 启动 unsafe（伪代码，省略缓冲区准备）
    let cfg = LaunchConfig::for_num_elems(1024);
    unsafe {
        // SAFETY: 1D over N，缓冲区覆盖全部访问。
        module.vecadd(&stream, cfg, &a_dev, &b_dev, &mut c_dev)?;
    }

    // reduce：从父视图构造子视图，走 prepare → 受检启动（启动本身安全）
    let reduce_view = kernels::reduce::LoadedModule::from_parent(&module)?;
    let prepared = reduce_view.prepare_sum(LaunchConfig1D::new(4, 256, 0))?;
    reduce_view.sum(&stream, &prepared, &a_dev, &mut out_dev)?;   // 安全！

    Ok(())
}
```

**验收清单**（**待本地验证**，多数用 `cargo check` / `cargo expand`）：

- [ ] `cargo expand` 能看到：`vecadd` 被改名带 `cuda_oxide_kernel_246e25db_` 前缀；`reduce` 命名空间有自己的 `LoadedModule` 与 `from_parent`；`__sum_CudaKernel` 实现了 `KernelLaunchContract`，`type Config = LaunchConfig1D`。
- [ ] `module.vecadd(...)` 必须包 `unsafe`；`reduce_view.sum(...)` 不需要 `unsafe`（但 `kernels::load(...)` 需要）。
- [ ] 把 `prepare_sum(LaunchConfig2D::new(...))` 改成 2D → 编译失败（rank 锁死）。
- [ ] 把 `reduce` 内核的 `DisjointSlice` 换成本地伪造的同名 struct → 编译失败（品牌化 sealed bound）。
- [ ] `cargo test -p cuda-macros --test compile_fail` 全绿（含 `launch_contract_*` 系列）。

> 如果手头有 GPU，最后可以 `cargo oxide run` 把它跑起来确认结果；没有 GPU 也至少用 `cargo oxide build` 走完编译，验证宏展开与契约校验。

## 6. 本讲小结

- `#[kernel]` 是属性宏：把 `fn vecadd` 改名为带保留前缀的 `cuda_oxide_kernel_246e25db_vecadd`（加 `#[no_mangle]`），同时生成 `__vecadd_CudaKernel` 见证类型与 `CudaKernel` 实现；PTX 入口名仍是干净的 `vecadd`。
- `#[cuda_module]` 扫描 inline 模块（#324 起含**嵌套 inline 模块**）里的 kernel，生成 `LoadedModule` 结构体、`load*`/`from_module` 加载器，以及每个内核的启动方法；嵌套命名空间通过 `LoadedModule::from_parent` 共享同一个已加载模块；全树 kernel 名必须唯一。
- `#[launch_contract(...)]`（#318）让内核作者声明 domain/block/动态共享内存/算力等假设；宏据此给 `DisjointSlice` 加**品牌化 sealed bound**（`__LaunchContractDisjointSlice`），为见证类型实现 `KernelLaunchContract`（`type Config = LaunchConfig1D/2D/3D` 锁死 rank），并生成 `prepare_*` → `PreparedLaunch<__name_CudaKernel>` 受检启动链路；活设备校验在 `PreparedLaunch::__prepare` 完成。
- 安全边界变化：未签约内核的同名方法是 `unsafe`（吃 raw `LaunchConfig`）；**签约模块的所有 loader 都是 `unsafe`**（一次性证明绑定代码符合模块声明）；签约内核另有 `_unchecked` 专家逃生口；品牌化/rank/loader 边界都有 compile_fail 测试固化。
- `reserved-oxide-symbols` 是命名契约的唯一真相源：`KERNEL_PREFIX` 等常量 + builder + 谓词三层 API，靠 `246e25db` 哈希让前缀不可伪造，把宏、codegen 后端、lowering/export、运行时加载器四方绑成一体；artifact 锚符号防 lib crate 的 `.oxart` 段被 dead-strip。

## 7. 下一步学习建议

- **u2-l2（线程索引与类型安全）**：本讲反复出现的 `ThreadIndex` 见证类型与 `DisjointSlice` 的品牌化 sealed trait 在那里讲透，理解为什么线程索引不能跨 scope 串用。
- **u2-l3（共享内存与同步）**：本讲提到的 `__dynamic_shared_alignment::<ALIGN>()` 标记与 `DynamicSharedArray<T, ALIGN>` 对齐合并的细节在那里展开。
- **u2-l4（从宿主启动内核）**：本讲只讲了宏生成侧，u2-l4 从宿主视角完整讲 raw `LaunchConfig` / `LaunchConfig1D/2D/3D` / `PreparedLaunch` / `_unchecked` 的使用与取舍。
- **u3-l2（模块加载与内嵌制品）**：想搞清楚 `load()` 到底怎么从当前可执行文件里发现 `.oxart` 段、锚符号如何防 dead-strip，就去看这一讲。
- **u7-l1（compile_fail 与安全契约）**：本讲末尾提到的 `launch_contract_*`、`cuda_module_*_boundary`、`fail_wrong_brand` 等负向测试，在那里有系统梳理。
- 想直接看宏文档？通读 [crates/cuda-macros/README.md](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/README.md)，它是每个属性最权威的速查表。
