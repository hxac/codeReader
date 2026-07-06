# 从宿主启动内核：raw 配置与类型化启动契约

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚为什么 `LaunchConfig`（grid/block/shared_mem 三元组）是**原始的、未经证明的**数据，把它喂给内核启动为什么是 `unsafe`。
- 掌握 #318 引入的秩保持配置 `LaunchConfig1D/2D/3D` 与 sealed trait `KernelLaunchConfig`，理解它们如何「隐藏尾随维度」。
- 跟踪一条**受检启动链路**：`module.prepare_<name>(config)` → `PreparedLaunch<Kernel>` → `module.<name>(&stream, &prepared, ...)`，并说出 `PreparedLaunch::__prepare` 在活设备上做了哪些校验。
- 区分三种启动入口在「安全证明」上的差异：受检安全方法、`*_unchecked` 专家逃生口、未签约内核的 `unsafe` 同名方法。
- 理解 cluster / cooperative / 共享内存 / 算力等「活设备」资源约束是如何被一次性证明并缓存在 `PreparedLaunch` 里的。

本讲是 u2-l1（宏与启动契约属性）的宿主侧落地篇：宏在那边声明契约、生成 `prepare_*` 与受检方法，本讲讲这些生成物在 `cuda-core` 里到底执行了什么。

## 2. 前置知识

在继续前，请确认你已理解以下概念（来自前置讲义）：

- **kernel / host / device / 单源编译**（u1-l4）：同一份 `.rs` 一次编译同时产出宿主机器码与内嵌 PTX。
- **grid / block / thread**：CUDA 的执行层级。grid 由若干 block 组成，每个 block 含若干 thread。启动一个内核就是告诉驱动「用多大的 grid、多大的 block、多少动态共享内存」去跑它。
- **`#[cuda_module]` / `#[kernel]`**（u2-l1）：过程宏扫描内核、生成 `LoadedModule` 与每个内核的启动方法。
- **`#[launch_contract(...)]`**（u2-l1）：作者用它声明某内核的 domain/block/动态共享内存/算力等假设；宏据此为该内核生成**品牌化的** `prepare_*` → `PreparedLaunch` 受检启动路径。
- **品牌化 sealed trait**（u2-l2）：用私有 `Sealed` 守门阻止下游伪造见证类型。

几个通俗类比：

- **raw `LaunchConfig` 像一张「未签名的支票」**：上面写着金额（grid/block/shared），但没人证明这张支票跟某个账户（内核）匹配。银行（CUDA 驱动）可能兑现它，也可能因为余额不足（资源超限）退票，但**不会**检查你写错了收款人（索引空间不匹配）。
- **`PreparedLaunch<Kernel>` 像「已经验过户的转账凭证」**：它把「这张支票属于这个内核、且这个内核在这台机器上跑得起来」一次性验明，之后反复转账（启动）就不必再验。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [crates/cuda-core/src/launch.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs) | **本讲主战场**。定义 `LaunchConfig`、`LaunchConfig1D/2D/3D`、`KernelLaunchConfig`、`LaunchContractSpec`、`KernelLaunchContract`、`PreparedLaunch`，以及全部启动契约校验逻辑。 |
| [crates/cuda-core/src/lib.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/lib.rs) | 暴露 `launch_kernel` / `launch_kernel_on_stream` / `launch_kernel_ex` 等对 `cuLaunchKernel`/`cuLaunchKernelEx` 的原始不安全封装——raw 路径的终点。 |
| [crates/cuda-macros/src/lib.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs) | `#[launch_contract]` 属性宏，以及 `#[cuda_module]` 生成的 `prepare_*`、受检同名方法、`*_unchecked`、unsafe loader。本讲引用它来证明「生成物长什么样」。 |
| [crates/cuda-host/src/launch.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/launch.rs) | 宿主侧参数编组（`CudaKernel` trait、`push_kernel_scalar` 等）与异步受检启动包装 `PreparedAsyncKernelLaunch`。 |
| [crates/rustc-codegen-cuda/examples/cuda_module_contract/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cuda_module_contract/src/main.rs) | `#[launch_contract]` 的官方端到端样例，本讲代码实践的蓝本。 |
| [crates/rustc-codegen-cuda/examples/vecadd/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs) | 未签约内核的 `unsafe` raw 启动样例（对照组）。 |

## 4. 核心概念与源码讲解

### 4.1 LaunchConfig 的原始语义与不安全性

#### 4.1.1 概念说明

启动一个 CUDA 内核，本质上要回答四个问题：

1. 启动多大的 **grid**（多少个 block）？
2. 每个 **block** 多大（多少个 thread）？
3. 每个 block 要多少**动态共享内存**（字节）？
4. 在哪条 **stream** 上、用哪些**参数**？

`LaunchConfig` 把前三项打包成一个纯数据结构。它**只是数据**：构造它没有任何副作用，也不会触碰 GPU。模块文档说得很直白：

> `LaunchConfig` bundles raw grid dimensions, block dimensions, and dynamic shared memory size. Constructing one is harmless, but submitting it without proving that it matches a kernel is unsafe.

关键在于「**submitting it without proving that it matches a kernel is unsafe**」。一个 `LaunchConfig` 不知道自己将要喂给哪个内核，因此它无法证明：

- block 形状是否符合该内核的索引假设（比如内核用 `thread::index_1d()`，你却启动了 2D block）；
- 动态共享内存字节数是否与内核里 `DynamicSharedArray` 的对齐/容量假设一致；
- grid/block 是否在该内核的 `#[launch_bounds]` 之内；
- 该内核是否需要 cluster / cooperative 启动模式。

CUDA 驱动（`cuLaunchKernel`）只能检查**机器层**的硬限制（比如 block 总线程数 ≤ 1024、grid.x ≤ 2^31-1），它**无法**检查「索引是否唯一」「同步是否正确」这类语义不变量。这就是为什么 raw 启动是 `unsafe`：剩下的语义证明只能由调用方以 `SAFETY:` 注释自证。

#### 4.1.2 核心流程

raw 启动路径的数据流：

```text
LaunchConfig { grid_dim, block_dim, shared_mem_bytes }   ← 原始数据，未证明
        │
        ▼
宏生成的 unsafe module.<name>(&stream, config, args)      ← 调用方用 SAFETY: 自证
        │  编组参数为 Vec<*mut c_void>
        ▼
cuda_core::launch_kernel_on_stream(func, grid, block, smem, stream, params)
        │  绑定 stream.context() 到当前线程
        ▼
cuda_bindings::cuLaunchKernel(...)                        ← 驱动只查机器硬限制
```

`LaunchConfig::for_num_elems(n)` 是个常用便捷构造器，但它**只算数字**，不替你证明安全：

> The helper does not inspect a kernel, so it does not by itself make a raw launch safe.

#### 4.1.3 源码精读

`LaunchConfig` 是一个普通的 `#[derive(Clone, Copy, Debug)]` 结构体，三个 `pub` 字段，无任何校验逻辑：

```rust
// crates/cuda-core/src/launch.rs
/// Each dimension tuple is `(x, y, z)`. This is inert configuration data: it
/// does not know which kernel will consume it and therefore cannot prove the
/// kernel's indexing, resource, or synchronization assumptions. Passing it to
/// a raw launch is an unsafe operation.
#[derive(Clone, Copy, Debug)]
pub struct LaunchConfig {
    pub grid_dim: (u32, u32, u32),
    pub block_dim: (u32, u32, u32),
    pub shared_mem_bytes: u32,
}
```

详见 [crates/cuda-core/src/launch.rs:18-33](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L18-L33)。注意它的字段是 `(x, y, z)` 三元组——这意味着一个 1D 内核如果误用，调用方完全可能把 `y`/`z` 填成非 1 而不被类型系统拦截。

`for_num_elems` 用固定 256 线程做向上取整：[crates/cuda-core/src/launch.rs:44-52](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L44-L52)。

raw 路径的终点是 `launch_kernel`，它是对 `cuLaunchKernel` 的薄封装，文档里明确列出了调用方必须自证的不变量（包括「index-space uniqueness and synchronization requirements」）：[crates/cuda-core/src/lib.rs:123-169](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/lib.rs#L123-L169)。宿主侧通常走带类型、自动绑定 context 的 `launch_kernel_on_stream`：[crates/cuda-core/src/lib.rs:200-219](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/lib.rs#L200-L219)。

vecadd 示例展示了未签约内核的真实用法——启动调用被包在 `unsafe` 块里，并带 `SAFETY:` 注释：

```rust
// crates/rustc-codegen-cuda/examples/vecadd/src/main.rs
let module = kernels::load(&ctx).expect("Failed to load embedded CUDA module");
// SAFETY: launch shape/resources match the kernel; buffers cover its accesses.
unsafe {
    module.vecadd(
        &stream,
        LaunchConfig::for_num_elems(N as u32),
        &a_dev, &b_dev, &mut c_dev,
    )
}
.expect("Kernel launch failed");
```

见 [crates/rustc-codegen-cuda/examples/vecadd/src/main.rs:75-86](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L75-L86)。注意 `kernels::load` 在未签约模块里是**安全**的（本讲 4.5 会对比签约模块的 unsafe loader）。

#### 4.1.4 代码实践

**实践目标**：体会「raw 配置只算数字、不证安全」。

**操作步骤**：

1. 打开 vecadd 示例，定位 `module.vecadd(...)` 的 `unsafe` 块。
2. 把 `LaunchConfig::for_num_elems(N as u32)` 改成手写的 `LaunchConfig { grid_dim: (4, 1, 1), block_dim: (256, 1, 1), shared_mem_bytes: 0 }`（`N=1024`，4×256=1024，结果应当一致）。
3. 再故意把它改成 `grid_dim: (1, 1, 1)`（只启动 256 个线程，远少于 1024）。

**需要观察的现象**：第 2 步结果不变；第 3 步**编译照样通过**（因为 raw 启动不做语义校验），但只有前 256 个元素被计算，其余保持 `zeroed` 的 0。

**预期结果**：raw 路径对「形状与内核不匹配」完全沉默——这正是它被定为 `unsafe` 的原因。**待本地验证**（若无 GPU，至少 `cargo oxide build vecadd` 应确认编译通过，体会「编译期不拦截」）。

#### 4.1.5 小练习与答案

**练习 1**：`LaunchConfig::for_num_elems(1024)` 返回的 `grid_dim` 和 `block_dim` 各是多少？

**参考答案**：`block_dim = (256, 1, 1)`，`grid_dim = (ceil(1024/256), 1, 1) = (4, 1, 1)`，`shared_mem_bytes = 0`。

**练习 2**：为什么 `for_num_elems` 不能让随后的 raw 启动变成安全操作？

**参考答案**：它只做了「元素数 → grid/block 数字」的算术，没有、也无法读取内核体去验证索引空间、共享内存、launch_bounds 等语义假设。证明这些不变量的责任仍在调用方的 `SAFETY:` 注释里。

---

### 4.2 秩保持配置：LaunchConfig1D/2D/3D 与 KernelLaunchConfig

#### 4.2.1 概念说明

raw `LaunchConfig` 的一个具体痛点：它用 `(x, y, z)` 三元组表示 grid/block，对 1D 内核来说 `y`/`z` 本应是 1，但类型系统不强制——填错了编译期查不出来。

#318 引入了一组**秩保持（rank-preserving）**配置类型，把「这是几维启动」编码进类型：

- `LaunchConfig1D`：只暴露 `grid_x`/`block_x`，`y`/`z` 永远是 1；
- `LaunchConfig2D`：暴露 `(x, y)`，`z` 永远是 1；
- `LaunchConfig3D`：完整 `(x, y, z)`。

它们都实现 sealed trait `KernelLaunchConfig`，该 trait 只有一个隐藏方法 `__raw(self) -> LaunchConfig`，用于把秩保持配置转回驱动能吃的三元组。

「sealed」是关键：trait 被 `mod sealed { pub trait Sealed {} }` 关在模块私有处，下游 crate **无法**为自己的类型实现 `KernelLaunchConfig`，因此也无法伪造一个「跳过尾随维度约束」的配置。这与 u2-l2 讲过的品牌化 sealed trait 是同一手法。

#### 4.2.2 核心流程

秩保持 → raw 的转换是单向、机械的：

```text
LaunchConfig1D { grid_x, block_x, shared }   ──__raw()──▶  LaunchConfig { (grid_x,1,1), (block_x,1,1), shared }
LaunchConfig2D { (gx,gy), (bx,by), shared }  ──__raw()──▶  LaunchConfig { (gx,gy,1),  (bx,by,1),  shared }
LaunchConfig3D { (gx,gy,gz), (bx,by,bz), … } ──__raw()──▶  LaunchConfig { (gx,gy,gz), (bx,by,bz), … }
```

`__raw()` 把隐藏的尾随维度硬编码为 1。下游既不能改写这个映射，也不能新增一个 `KernelLaunchConfig` 的实现来绕过它。

每个内核的契约会通过 `KernelLaunchContract::Config` 关联类型**锁死**秩（见 4.3）：1D 内核的契约声明 `type Config = LaunchConfig1D`，于是 `prepare_<name>` 的形参类型就是 `LaunchConfig1D`，传一个 2D 配置会直接编译失败。

#### 4.2.3 源码精读

`KernelLaunchConfig` 是 sealed trait，`__raw` 标了 `#[doc(hidden)]`：

```rust
// crates/cuda-core/src/launch.rs
mod sealed { pub trait Sealed {} }

/// A rank-preserving launch configuration accepted by a typed kernel contract.
/// This trait is sealed. Use LaunchConfig1D/2D/3D; downstream crates cannot
/// provide a configuration that bypasses their fixed trailing dimensions.
pub trait KernelLaunchConfig: sealed::Sealed + Copy {
    #[doc(hidden)]
    fn __raw(self) -> LaunchConfig;
}
```

见 [crates/cuda-core/src/launch.rs:55-67](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L55-L67)。

`LaunchConfig1D` 的字段是私有的（只能用 `::new` 构造），且其 `__raw` 把 `y`/`z` 写死为 1：

```rust
// crates/cuda-core/src/launch.rs
impl KernelLaunchConfig for LaunchConfig1D {
    fn __raw(self) -> LaunchConfig {
        LaunchConfig {
            grid_dim: (self.grid_x, 1, 1),
            block_dim: (self.block_x, 1, 1),
            shared_mem_bytes: self.shared_mem_bytes,
        }
    }
}
```

见 [crates/cuda-core/src/launch.rs:95-103](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L95-L103)。`LaunchConfig2D`、`LaunchConfig3D` 同理：[crates/cuda-core/src/launch.rs:128-167](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L128-L167)。

注意 `new` 是 `const fn`，零维检测被推迟到「为具体内核 prepare」时（这样错误信息能带上内核名）：[crates/cuda-core/src/launch.rs:79-91](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L79-L91)。

单元测试直观证明了「尾随维度被固定为 1」这一不变量：[crates/cuda-core/src/launch.rs:1357-1370](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L1357-L1370)。

#### 4.2.4 代码实践

**实践目标**：验证 sealed trait 与秩保持约束在编译期生效。

**操作步骤**：

1. 在自己的 crate 里写 `struct MyCfg;` 并尝试 `impl cuda_core::KernelLaunchConfig for MyCfg { ... }`。
2. 用 `LaunchConfig1D::new((N as u32).div_ceil(256), 256, 0)` 构造一个 1D 配置，并把它传给一个声明了 `domain = 2` 的 `#[launch_contract]` 内核的 `prepare_*`。

**需要观察的现象**：第 1 步编译失败，错误指向 `Sealed` trait 不在下游可见；第 2 步编译失败，错误是类型不匹配（期望 `LaunchConfig2D`，得到 `LaunchConfig1D`）。

**预期结果**：sealed 关住了「伪造配置」，关联类型 `Config` 关住了「秩传错」。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么不直接给 `LaunchConfig` 加一个 `const DIMS: u8` 泛型来区分 1D/2D/3D，而要拆成三个独立类型？

**参考答案**：拆成三个独立类型 + sealed trait，可以保证「尾随维度恒为 1」由 `__raw` 的实现单点决定，下游既看不到也无法篡改；若用泛型参数，下游可能构造出非法的秩组合，且 `__raw` 仍需匹配所有情况，类型层证明更弱。

**练习 2**：`LaunchConfig1D::new(0, 256, 0)` 会在 `new` 里报错吗？

**参考答案**：不会。`new` 是纯构造，不做零维检查；零维会在 `PreparedLaunch::__prepare` → `validate_static` 里报 `ZeroDimension` 错误，且错误信息能带上内核名（见 4.3）。

---

### 4.3 prepare_* → PreparedLaunch 受检启动链路

#### 4.3.1 概念说明

这是 #318 的核心机制。当内核带 `#[launch_contract(...)]` 时，宏会生成一套**类型化受检启动**API，分两步：

1. **`module.prepare_<name>(config) -> Result<PreparedLaunch<Kernel>, LaunchContractError>`**：在**活设备**上一次性查询并校验所有资源假设，成功后返回一个品牌化的 `PreparedLaunch<__name_CudaKernel>`。这一步是安全的（`prepare_*` 不是 `unsafe fn`），因为所有不变量由宏生成的 `LaunchContractSpec` 静态描述。
2. **`module.<name>(&stream, &prepared, args) -> Result<(), LaunchContractError>`**：把已验证的配置入队启动。**这个同名方法是安全的**（不是 `unsafe fn`），因为它只接收 `&PreparedLaunch`——安全证明已经浓缩在那个值里。它唯一做的运行期检查是 `validate_stream`（确认 stream 与函数属同一 context）。

`PreparedLaunch<C>` 是一个**特化的品牌化证明类型**：它的类型参数 `C` 是宏为该内核生成的见证标记（如 `__vecadd_CudaKernel`），不同内核的 `PreparedLaunch` 互不兼容，因此你无法把 A 内核 prepare 出的证明喂给 B 内核的启动方法。

`prepare_*` 的安全性建立在一个 `unsafe fn __prepare` 之上，后者带一段 `# Safety` 文档：`C::SPEC` 必须真实描述编译出的设备函数。这份「真实描述」的保证由宏承担（宏读 `#[launch_contract]` 与 `#[launch_bounds]` 生成 `SPEC`），所以应用代码看到的 `prepare_*` 是安全的。

#### 4.3.2 核心流程

受检启动的时间线：

```text
编译期（宏）:
  #[launch_contract(domain=1, block=(256,1,1), dynamic_shared=0)]
  ─▶ 生成 marker __name_CudaKernel
  ─▶ impl KernelLaunchContract for __name_CudaKernel {
          type Config = LaunchConfig1D;            // 锁死秩
          const SPEC: LaunchContractSpec = ...;    // 静态契约
      }
  ─▶ 生成 fn prepare_name(&self, cfg) -> PreparedLaunch<__name_CudaKernel>
  ─▶ 生成 (安全的) fn name(&self, &stream, &PreparedLaunch<__name_CudaKernel>, args)

运行期:
  module.prepare_name(LaunchConfig1D::new(...))
     │  unsafe { PreparedLaunch::__prepare(function, config) }
     │     ├─ validate_static(SPEC, raw)         // 不接触设备：形状/块/共享/cluster 整除
     │     ├─ 查询活设备: launch_limits / max_threads / static_shared / ...
     │     ├─ validate_live_shape(SPEC, raw, limits, fn_max_threads)
     │     ├─ 校验 compute_capability / cluster / cooperative
     │     └─ 设置函数的 max_dynamic_shared_memory（若契约要求更大）
     └─▶ Ok(PreparedLaunch { function, config: raw, .. })

  module.name(&stream, &prepared, args)            // 安全：证明已在 prepared 里
     │  prepared.validate_stream(&stream)?         // 唯一运行期检查：同 context
     │  编组参数 → launch_kernel_on_stream(...)
     └─▶ Ok(())
```

`PreparedLaunch` 设计为可复用：文档指出「Reusing this value performs no contract query; it only compares the stream's context handle」。也就是说，prepare 一次、启动多次时，昂贵的设备/函数资源查询只发生一次。

#### 4.3.3 源码精读

宏生成的 `prepare_*` 方法（注意它不是 `unsafe fn`，但内部用 `unsafe {}` 调 `__prepare`）：

```rust
// crates/cuda-macros/src/lib.rs （generate_cuda_module_prepare_launch_methods 生成）
#vis fn prepare_name /* impl_generics */ (
    &self,
    config: <#marker_ty as ::cuda_core::KernelLaunchContract>::Config,  // 锁死秩
) -> Result<::cuda_core::PreparedLaunch<#marker_ty>, ::cuda_core::LaunchContractError> {
    #function_binding
    unsafe {
        ::cuda_core::PreparedLaunch::<#marker_ty>::__prepare(#function.clone(), #config)
    }
}
```

见 [crates/cuda-macros/src/lib.rs:2171-2190](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L2171-L2190)。对泛型内核，宏还会生成 `prepare_<name>_for`，用类型见证参数把单态化的类型固定下来：[crates/cuda-macros/src/lib.rs:2152-2169](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L2152-L2169)。

宏生成的**安全**同名受检方法（接收 `&PreparedLaunch`，故非 `unsafe`）：

```rust
// crates/cuda-macros/src/lib.rs （generate_cuda_module_prepared_launch_method 生成）
#vis fn name /* impl_generics */ (
    &self,
    stream: &::cuda_core::CudaStream,
    prepared: &::cuda_core::PreparedLaunch<#marker_ty>,
    /* params */
) -> Result<(), ::cuda_core::LaunchContractError> {
    prepared.validate_stream(stream)?;
    let function = prepared.function();
    let config = prepared.__raw_config();
    /* 编组参数 */
    (launch_call).map_err(::cuda_core::LaunchContractError::from)
}
```

见 [crates/cuda-macros/src/lib.rs:2289-2307](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L2289-L2307)。

`PreparedLaunch` 结构体本身——`PhantomData<fn(C) -> C>` 让品牌标记「传染」到类型，且不影响 `Clone`：

```rust
// crates/cuda-core/src/launch.rs
pub struct PreparedLaunch<C: KernelLaunchContract> {
    function: CudaFunction,
    config: LaunchConfig,
    _contract: PhantomData<fn(C) -> C>,
}
```

见 [crates/cuda-core/src/launch.rs:781-790](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L781-L790)。

`__prepare` 是整套机制的校验核心，逐项做：`validate_static`（不接触设备）→ 查询设备/函数资源 → `validate_live_shape` → 共享内存总量与上限 → 算力 → cluster → cooperative：

```rust
// crates/cuda-core/src/launch.rs （PreparedLaunch::__prepare 节选）
pub unsafe fn __prepare(function: CudaFunction, config: C::Config) -> Result<Self, LaunchContractError> {
    let raw = config.__raw();
    validate_static(C::SPEC, raw)?;                       // 静态校验
    let context = function.context();
    let limits = context.launch_limits()?;                // 活设备查询
    let function_max_threads = function.max_threads_per_block()?;
    /* ... */
    validate_live_shape(C::SPEC, raw, limits, function_max_threads)?;
    /* 共享内存 / 算力 / cluster / cooperative 校验 ... */
    Ok(Self { function, config: raw, _contract: PhantomData })
}
```

见 [crates/cuda-core/src/launch.rs:816-940](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L816-L940)。`# Safety` 文档（[crates/cuda-core/src/launch.rs:808-815](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L808-L815)）说明这个 `unsafe` 由宏生成的 marker 承担。

`validate_static` 覆盖了所有「不接触设备就能查」的契约：内核名非空、grid/block 形状非零且不溢出、block 形状匹配 `BlockRequirement`、动态共享内存匹配 `Exact`/`Range`、cluster 维度整除 grid：[crates/cuda-core/src/launch.rs:972-1059](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L972-L1059)。

`validate_stream` 是安全同名方法唯一的运行期检查，且它**不发驱动调用**，只比较 context 句柄：[crates/cuda-core/src/launch.rs:956-969](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L956-L969)。

端到端样例（官方 contract 测试）展示了完整三步——unsafe load → prepare → 安全启动：

```rust
// crates/rustc-codegen-cuda/examples/cuda_module_contract/src/main.rs
let module = unsafe { kernels::load(&ctx)? };                       // 签约模块：loader 是 unsafe
let launch = module.prepare_mixed_abi(LaunchConfig1D::new((N as u32).div_ceil(256), 256, 0))?;
module.mixed_abi(&stream, &launch, scale, bias, extra, &input_dev, /* ... */, &mut output_dev)?;
```

见 [crates/rustc-codegen-cuda/examples/cuda_module_contract/src/main.rs:163-187](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cuda_module_contract/src/main.rs#L163-L187)。

#### 4.3.4 代码实践

**实践目标**：亲手走一遍 prepare → 受检启动。

**操作步骤**：以 `cuda_module_contract` 为蓝本，写一个最小 1D 内核：

```rust
// 示例代码（非项目原有，仿 cuda_module_contract 风格）
#[cuda_module]
mod kernels {
    use super::*;
    #[kernel]
    #[launch_bounds(256)]
    #[launch_contract(domain = 1, block = (256, 1, 1), dynamic_shared = 0)]
    pub fn scale(factor: f32, input: &[f32], mut output: DisjointSlice<f32>) {
        let idx = thread::index_1d();
        if let Some(o) = output.get_mut(idx) { *o = input[idx.get()] * factor; }
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let ctx = CudaContext::new(0)?;
    let stream = ctx.default_stream();
    let module = unsafe { kernels::load(&ctx)? };        // 签约模块 → unsafe loader
    let n = 1024u32;
    let prepared = module.prepare_scale(LaunchConfig1D::new(n.div_ceil(256), 256, 0))?;
    module.scale(&stream, &prepared, 2.0f32, &input_dev, &mut output_dev)?;  // 安全：无需 unsafe
    Ok(())
}
```

**需要观察的现象**：`module.scale(...)` 调用**不需要** `unsafe` 块（与 vecadd 对照）。如果把 `block` 改成 `(128,1,1)`，`prepare_scale` 会在运行期返回 `LaunchContractError::BlockShapeMismatch`。

**预期结果**：受检路径下，错误的块形状以 `Result::Err` 返回，而非沉默地错误执行。**待本地验证**（无 GPU 时可读 `validate_static` 的单元测试确认行为：[crates/cuda-core/src/launch.rs:1403-1414](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L1403-L1414)）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `prepare_*` 是安全函数，而它内部调用的 `__prepare` 是 `unsafe fn`？

**参考答案**：`__prepare` 信任 `C::SPEC` 真实描述了编译出的设备函数（rank、block、共享内存布局、启动模式）——这是无法在类型层证明的语义假设，故 `unsafe`。宏读 `#[launch_contract]`/`#[launch_bounds]` 自动生成 `SPEC`，保证它和设备函数同源，于是把这份保证封装进安全的 `prepare_*`，把 `unsafe` 责任从应用层上移到宏层。

**练习 2**：复用同一个 `PreparedLaunch` 在两条不同的 stream 上启动同一个内核，会发生几次资源查询？

**参考答案**：零次额外的资源查询。prepare 阶段已把所有设备/函数资源查询做完并缓存进 `PreparedLaunch`；后续每次启动只做 `validate_stream`（比较 context 句柄，无驱动调用）。

---

### 4.4 cluster / cooperative 活设备校验

#### 4.4.1 概念说明

有些资源假设无法在编译期或纯算术层验证，必须查询**活设备**与**活函数**：

- **block/grid 维度上限**：每台 GPU 的 `max_grid_dim`/`max_block_dim`/`max_threads_per_block` 不同。
- **每个函数的 `max_threads_per_block`**：受 `#[launch_bounds]` 约束，写入编译产物。
- **静态 + 动态共享内存总量**：函数的静态共享内存（编译产物里）加上你请求的动态共享内存，不能超过设备的可移植上限（或 opt-in 上限）。
- **算力（compute capability）**：cluster 需要 sm_90+，某些 intrinsic 需要更高。
- **cluster**：设备是否支持 cluster launch、cluster 维度是否整除 grid、cluster 大小是否超过 occupancy 上限、是否与编译进函数的 required-cluster 元数据一致。
- **cooperative**：设备是否支持 cooperative launch、整个 cooperative grid 能否全部驻留（resident）在设备上。

`__prepare` 把这些一次性查完，失败原因都收进一个 `#[non_exhaustive]` 的 `LaunchContractError` 枚举，且**每条错误都带 `kernel: &'static str`**，方便定位是哪个内核的假设被违反。

#### 4.4.2 核心流程

活设备校验的顺序（在 `__prepare` 内）：

```text
1. validate_static(SPEC, raw)                 # 纯算术，不查设备
2. 设备/函数资源查询:
     context.launch_limits()                  # max_grid/block/threads/shared
     function.max_threads_per_block()         # 来自 #[launch_bounds]
     function.static_shared_memory_bytes()
     function.max_dynamic_shared_memory_bytes()
3. validate_live_shape(...)                   # grid/block 不超设备/函数上限
4. 共享内存总量 = static + dynamic契约上限;   # 用契约上限而非本次选择，保证并发 prepare 单调
   校验 ≤ 可移植上限，否则试 opt-in 上限
5. 若声明 min_compute_capability: 查 context.compute_capability() 比较
6. 若声明 cluster:
   - context.supports_cluster_launch()?
   - function.required_cluster_dimensions() 与声明一致?
   - function.max_potential_cluster_size / max_active_clusters 做 occupancy 校验
7. 若声明 cooperative:
   - context.supports_cooperative_launch()?
   - 用 occupancy 计算 resident_capacity，校验 grid blocks ≤ resident_capacity
8. 设置 function 的 max_dynamic_shared_memory（若契约上限更大）—— 唯一持久副作用
```

一个精妙之处：共享内存校验用的是**契约声明的上限**（`dynamic_shared_memory_max`），而非本次启动选择的字节数。文档解释了原因：

> Preparation requires the live function/device to support `max_bytes`, even when one prepared configuration chooses fewer bytes. This proves the function's dynamic-memory capacity for the full interval and lets concurrent preparations install one monotonic, race-safe maximum.

也就是说，对 `Range` 型共享内存契约，即使本次只申请少量字节，也要证明「整个区间都跑得起来」，这样多次并发 prepare 写入函数的 `max_dynamic_shared_memory` 是单调、无竞争的。

#### 4.4.3 源码精读

`DeviceLaunchLimits` 把设备级上限打包（字段 `pub(crate)`，只能由 `context.launch_limits()` 构造）：[crates/cuda-core/src/launch.rs:306-336](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L306-L336)。

`validate_live_shape` 把 raw 形状对齐到设备/函数上限，逐轴比较：[crates/cuda-core/src/launch.rs:1061-1105](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L1061-L1105)。

`__prepare` 里 cluster 校验段——先查支持性、再对齐 required 元数据、最后用 `max_potential_cluster_size` 与 `max_active_clusters` 做 occupancy：[crates/cuda-core/src/launch.rs:863-922](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L863-L922)。其中 `max_active_clusters` 返回 `CUDA_ERROR_INVALID_CLUSTER_SIZE` 时被转译成更友好的 `ClusterShapeUnsupported`：[crates/cuda-core/src/launch.rs:904-920](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L904-L920)。

cooperative 段——用 `max_active_blocks_per_multiprocessor × multiprocessor_count` 算出 `resident_capacity`，再校验 grid block 总数能全部驻留：[crates/cuda-core/src/launch.rs:924-933](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L924-L933)。

共享内存「契约上限」的取值函数：[crates/cuda-core/src/launch.rs:1122-1127](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L1122-L1127)。可移植上限不通过时回退到 opt-in 上限的代码：[crates/cuda-core/src/launch.rs:836-848](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L836-L848)。

`LaunchContractError` 是 `#[non_exhaustive]` 枚举，每个变体都带 `kernel` 字段，例如 `ComputeCapabilityTooLow { kernel, required, actual }`、`ClusterHasNoResidency { kernel, cluster }`、`CooperativeGridTooLarge { kernel, blocks, resident_capacity }`：[crates/cuda-core/src/launch.rs:360-586](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L360-L586)。

#### 4.4.4 代码实践

**实践目标**：观察活设备校验如何拦截不合法的 cluster 启动。

**操作步骤**：

1. 阅读单元测试 `cluster_dimensions_must_be_nonzero_and_divide_grid`，它验证了「cluster 必须整除 grid」这条静态契约：[crates/cuda-core/src/launch.rs:1514-1537](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L1514-L1537)。
2. 假想一个 `#[launch_contract(domain = 1, block = (256,1,1))] with_cluster((2,1,1))` 的内核，在 grid `(7,1,1)` 上 prepare。

**需要观察的现象**：第 2 步在静态校验阶段就返回 `ClusterDoesNotDivideGrid { axis: X, grid: 7, cluster: 2 }`，根本到不了活设备查询。

**预期结果**：cluster 不整除 grid 是纯算术错误，被 `validate_static` 提前拦截；而 cluster 是否超 occupancy 上限才会进入活设备段。**这一步是源码阅读型实践**，无需 GPU。

#### 4.4.5 小练习与答案

**练习 1**：`cooperative` 启动为什么需要校验「grid 能全部驻留」？

**参考答案**：cooperative launch 要求网格内所有 block 同时驻留在 SM 上才能保证网格级同步（`cg::sync()`）语义成立。若 grid block 数超过 `resident_capacity`，部分 block 排队等待，网格级同步会死锁，故必须在启动前拒绝。

**练习 2**：为什么共享内存校验用契约的 `max_bytes` 而非本次实际选择的字节数？

**参考答案**：为了让「整个声明区间都安全」一次性得证，使得多个 prepare 并发写入函数的 `max_dynamic_shared_memory` 时是单调递增、无竞争的；同时也保证之后用同一 `PreparedLaunch` 启动不同字节数的选择都在已证明的区间内。

---

### 4.5 三种启动入口与 _unchecked 专家路径

#### 4.5.1 概念说明

把前面几节合起来，一个内核在宿主侧实际上有**三种**启动入口，它们的差异全在「安全证明由谁承担」：

| 入口 | 触发条件 | 签名安全性 | 证明来源 |
| --- | --- | --- | --- |
| **受检同名方法** `module.name(&stream, &prepared, args)` | 内核带 `#[launch_contract]` | **安全** | `prepare_*` 产出的 `PreparedLaunch` 已在活设备上证明 |
| **`*_unchecked` 专家口** `module.name_unchecked(&stream, raw_config, args)` | 内核带 `#[launch_contract]` | **`unsafe`** | 调用方自证（绕过 prepare，跳过所有校验） |
| **未签约同名方法** `module.name(&stream, raw_config, args)` | 内核**无** `#[launch_contract]` | **`unsafe`** | 调用方自证（吃 raw `LaunchConfig`） |

也就是说：

- **签约内核**默认走「安全的受检路径」；如果你确有把握、想跳过查询开销，可以用 `*_unchecked` 显式承担 `unsafe`。
- **未签约内核**没有 `prepare_*`，只有吃 raw 配置的 `unsafe` 同名方法，与 vecadd 一致。

此外，#318 还把**签约模块的所有 loader 都改成了 `unsafe`**（`load`/`load_named`/`from_module`/`load_async`/…）。原因是：签约模块的受检启动默认信任「加载进来的 PTX 与本模块声明的 ABI/资源语义一致」，而这一点只能由调用方证明（绑定到正确编译产物、泛型模块的所有单态化都在 bundle 里且无冲突）。这是一次性的绑定证明：load 时证明一次，之后 prepare + 启动就是安全的。

#### 4.5.2 核心流程

三种入口的对照决策树：

```text
内核带 #[launch_contract]?
├─ 是
│   ├─ 想要安全？  ─▶ module.prepare_name(cfg)?  →  module.name(&stream, &prepared, args)   [安全]
│   └─ 想跳过校验？─▶ unsafe { module.name_unchecked(&stream, raw_cfg, args) }              [unsafe]
└─ 否
    └─              ─▶ unsafe { module.name(&stream, LaunchConfig, args) }                  [unsafe]
```

签约模块加载：

```text
unsafe { kernels::load(&ctx)? }     // 一次性证明：绑定正确编译产物 / 泛型单态化齐全
   └─▶ 此后 prepare_* 与受检同名方法均安全
```

#### 4.5.3 源码精读

宏根据 `kernel.launch_contract.is_some()` 分派到不同生成器：[crates/cuda-macros/src/lib.rs:2196-2202](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L2196-L2202)。

未签约内核的 legacy 方法——`unsafe fn`，吃 raw `LaunchConfig`，文档串明调用方必须自证 indexing/memory/launch-bounds/shared 全部假设：[crates/cuda-macros/src/lib.rs:2227-2248](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L2227-L2248)。

签约内核生成的**一对**方法——安全受检方法（接收 `&PreparedLaunch`）+ `*_unchecked` unsafe 逃生口（吃 raw `LaunchConfig`），后者文档写明「caller must uphold the kernel's declared geometry, resource, capability, and context contract」：

```rust
// crates/cuda-macros/src/lib.rs （generate_cuda_module_prepared_launch_method 生成，节选）
#vis fn name(&self, stream, prepared: &PreparedLaunch<marker>, /* params */) -> Result<(), LaunchContractError> {
    prepared.validate_stream(stream)?;
    /* ... 安全 ... */
}

/// Unchecked launch escape hatch for this contracted kernel.
/// # Safety
/// The caller must uphold the kernel's declared geometry, resource, capability, and context contract.
#vis unsafe fn name_unchecked(&self, stream, config: LaunchConfig, /* params */) -> Result<(), DriverError> {
    /* 直接编组 + 原始 launch_call，无任何校验 */
}
```

见 [crates/cuda-macros/src/lib.rs:2309-2327](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L2309-L2327)。

签约模块的 loader 变 `unsafe`，文档列出调用方必须证明的「绑定正确编译产物 / 泛型单态化齐全无冲突」：

```rust
// crates/cuda-macros/src/lib.rs （load_definition，has_launch_contract 分支）
/// # Safety
/// For a non-generic module, the selected package bundle must be the artifact
/// compiled from this cuda_module; ... For a generic module, the merged PTX
/// set must contain each matching specialization and no conflicting entry definition.
pub unsafe fn load(ctx: &Arc<CudaContext>) -> Result<LoadedModule, EmbeddedModuleError> { /* ... */ }
```

见 [crates/cuda-macros/src/lib.rs:741-758](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L741-L758)；未签约分支对照（安全 loader）：[crates/cuda-macros/src/lib.rs:759-767](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L759-L767)。

异步侧同理：`PreparedAsyncKernelLaunch` / `PreparedOwnedAsyncKernelLaunch` 把 `PreparedLaunch` 与异步启动绑定，`execute` 时只额外做 `validate_stream`：[crates/cuda-host/src/launch.rs:323-336](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/launch.rs#L323-L336)、[crates/cuda-host/src/launch.rs:411-428](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/launch.rs#L411-L428)。异步 raw 路径（`<name>_async`）在未签约时是 `unsafe`，签约时另有受检异步方法消费同一 `PreparedLaunch`。

#### 4.5.4 代码实践

**实践目标**：在同一台机器上对比三种入口的「证明负担」。

**操作步骤**：

1. 复用 4.3 写的 `scale` 签约内核。分别用三种方式启动同一个计算：
   - **受检**：`module.prepare_scale(LaunchConfig1D::new(...))?` → `module.scale(&stream, &prepared, ...)`（无 `unsafe`）。
   - **`_unchecked`**：`unsafe { module.scale_unchecked(&stream, LaunchConfig{...}, ...) }`（编译期不拦，但 `LaunchConfig` 可随便填错）。
   - **对照组**：另写一个**不带** `#[launch_contract]` 的 `scale_raw` 内核，启动时 `unsafe { module.scale_raw(&stream, LaunchConfig::for_num_elems(n), ...) }`。
2. 在 `_unchecked` 路径里故意传一个错误的 block 形状（如 `(128,1,1)`，与契约 `(256,1,1)` 不符）。

**需要观察的现象**：

- 受检路径：错误形状在 `prepare_scale` 阶段返回 `Err(BlockShapeMismatch)`，根本不会错误执行。
- `_unchecked` 路径：错误形状**不会被拦**，直接喂给驱动，可能得到 `DriverError` 或错误的计算结果（取决于硬件后果）。
- 未签约路径：完全没有契约可校验，是否出错完全靠驱动硬限制与调用方自觉。

**预期结果**：三者计算正确时结果一致，但「错误形状」的反馈时机截然不同——这正是安全证明落在不同层的体现。**待本地验证**（无 GPU 时，至少 `cargo oxide build` 应确认三种调用都能编译，重点体会 `unsafe` 出现在哪一行）。

#### 4.5.5 小练习与答案

**练习 1**：签约内核已经有安全的受检路径，为什么还要保留 `*_unchecked`？

**参考答案**：给「已通过其他途径证明」或「需要跳过重复资源查询开销」的专家场景留逃生口。例如内核被嵌入到一个更大的、已自证几何的启动框架里，逐次 prepare 的活设备查询是不必要开销；此时用 `_unchecked` 显式承担 `unsafe`，把责任标记出来。

**练习 2**：签约模块的 `load` 为什么是 `unsafe`，而 vecadd 那种未签约模块的 `load` 是安全的？

**参考答案**：签约模块的受检启动默认信任「加载的 PTX 与宏声明 ABI/资源一致」，这份一致只能由调用方证明（绑定到正确编译产物、泛型单态化齐全无冲突），故把证明责任放在 load 这一次性绑定上、标 `unsafe`；之后 prepare + 启动即安全。未签约模块没有「契约与 PTX 一致」的假设需要证明，load 只是常规加载，故安全。

---

## 5. 综合实践

把本讲的知识串起来：实现一个带 `#[launch_contract]` 的 **block 内 reduce** 内核，分别走三种启动入口，对比它们对错误输入的反馈。

**任务**：在 vecadd 工程基础上新建一个示例 `reduce`，完成以下子任务。

1. **写一个签约 reduce 内核**：每个 block 用 256 个线程，借助静态共享内存做 block 内求和，把每个 block 的部分和写入 `output`。

   ```rust
   // 示例代码（非项目原有）
   #[cuda_module]
   mod kernels {
       use super::*;
       #[kernel]
       #[launch_bounds(256)]
       #[launch_contract(domain = 1, block = (256, 1, 1), dynamic_shared = 0)]
       pub fn block_reduce(input: &[f32], mut output: DisjointSlice<f32>) {
           let idx = thread::index_1d();
           let t = idx.get();
           let mut shared = SharedArray::<f32, 256>::get();
           // 装载 + 共享内存归约（伪代码，省略 sync 细节，参考 u2-l3）
           unsafe { *shared.get_mut(t) = if t < input.len() { input[t] } else { 0.0 }; }
           sync_threads();
           // ... 树形归约 ...
           if t == 0 { if let Some(o) = output.get_mut(idx.grid_block()) { *o = /* sum */ }; }
       }
   }
   ```

   （共享内存与同步细节参考 u2-l3；这里只关心启动侧。）

2. **受检启动（默认）**：

   ```rust
   let module = unsafe { kernels::load(&ctx)? };
   let prepared = module.prepare_block_reduce(LaunchConfig1D::new(num_blocks, 256, 0))?;
   module.block_reduce(&stream, &prepared, &input_dev, &mut output_dev)?;  // 无 unsafe
   ```

3. **`_unchecked` 专家路径**：用 `unsafe { module.block_reduce_unchecked(&stream, LaunchConfig{...}, ...) }` 启动，再故意把 block 改成 `(128,1,1)`，观察它**不被契约拦截**，直接交给驱动。

4. **未签约对照**：再写一个不带 `#[launch_contract]` 的 `block_reduce_raw`，用 `unsafe { module.block_reduce_raw(&stream, LaunchConfig::for_num_elems(n), ...) }` 启动，体会「无契约可证」。

**需要观察并记录**：

- 哪个调用需要 `unsafe`、哪个不需要；
- 把 block 形状填错时，受检路径返回什么 `LaunchContractError`，而另两个路径是否沉默通过；
- `prepare_*` 返回的 `PreparedLaunch` 复用启动两次时，是否还能保持正确（验证「prepare 一次、启动多次」）。

**预期结果**：受检路径把「block 形状不符契约」变成一个明确的 `Err`，把过去靠 `SAFETY:` 注释和人工审查的语义不变量升级为运行期可证、类型层可引导的安全 API。**待本地验证**（无 GPU 时退化为「源码阅读 + 编译验证」：确认三种方法签名符合本讲描述，并阅读 `validate_static` 单测理解错误反馈）。

## 6. 本讲小结

- `LaunchConfig` 是**原始的、未经证明的**配置数据（grid/block/shared 三元组）；`for_num_elems` 只算数字，不替你证明安全，故 raw 启动是 `unsafe`。
- #318 引入 sealed `KernelLaunchConfig` 与秩保持的 `LaunchConfig1D/2D/3D`，把「尾随维度恒为 1」由 `__raw` 单点决定，下游无法伪造或传错秩。
- 受检启动链路 `prepare_*` → `PreparedLaunch<Kernel>` → 安全同名方法：`__prepare` 在活设备上一次性校验形状/块/共享内存/算力/cluster/cooperative，结果缓存进品牌化的 `PreparedLaunch`，复用时只做无驱动的 `validate_stream`。
- cluster 与 cooperative 这类资源假设必须查询活设备/活函数；共享内存用「契约上限」校验，使并发 prepare 单调且无竞争；所有错误都带 `kernel` 名便于定位。
- 三种启动入口——安全受检方法、`*_unchecked` 专家口、未签约 `unsafe` 同名方法——的区别全在「安全证明由谁承担」；签约模块的 loader 因此变 `unsafe`，把绑定证明收敛到一次性 load。

## 7. 下一步学习建议

- **u3-l1（cuda-core 安全封装）**：`CudaModule` 如何承载 `prepare_*` 用到的活设备查询（`launch_limits`/`compute_capability`/`supports_cluster_launch` 等），以及 `CudaFunction` 的资源 attribute 读取。
- **u3-l2（模块加载与内嵌制品）**：签约模块的 `unsafe load` 到底在证明什么——`.oxart` 制品的 entry 记录与 ABI 绑定。
- **u3-l3（异步执行模型）**：`PreparedAsyncKernelLaunch` 如何把 `PreparedLaunch` 与惰性 `DeviceOperation` 结合，raw 异步路径为何变 `unsafe`。
- **u7-l1（compile_fail 与安全契约）**：用负向测试看「品牌化/维度契约被破坏时编译期如何拦截」，与本讲的运行期校验互补。
- 继续阅读 [crates/cuda-core/src/launch.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs) 的 `__prepare` 全文与单元测试，是理解整套启动契约最快的路径。
