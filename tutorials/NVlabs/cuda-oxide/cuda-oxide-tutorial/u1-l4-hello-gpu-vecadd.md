# Hello GPU：vecadd 端到端

## 1. 本讲目标

前三讲（u1-l1 项目定位、u1-l2 crate 地图、u1-l3 cargo-oxide 驱动）让我们知道了 cuda-oxide「是什么」「由哪些 crate 组成」「怎么被驱动起来」。本讲是整个手册的第一次**端到端落地**——选一个最简单的例子 `vecadd`（向量加法），把「写内核 → 编译 → 宿主加载 → 启动 → 回收结果」这条完整闭环一次性跑通。

读完本讲，你应当能够：

- 读懂一个最小的 `#[cuda_module]` 内核模块与配套的宿主 `main`，并说清哪段代码最终跑在 GPU、哪段跑在 CPU。
- 看懂宿主侧 `DeviceBuffer` 的三段式内存搬运（host → device → host）。
- 描述 `kernels::load()` 如何把编译期嵌进可执行文件的 PTX 加载回来，并通过类型安全的 `module.vecadd(...)` 方法启动内核。
- 自己动手把内核改成向量减法或数乘，并核对结果正确。

> 本讲承接 u1-l1 建立的术语：kernel、PTX、MIR、codegen backend、`__rustc_codegen_backend` 入口、device/host 分流、`-Z mir-enable-passes=-JumpThreading` 硬约束。这里不再重复它们的定义，只在用到时点出。

## 2. 前置知识

如果你写过 CUDA C/C++，可以快速对照下表；如果没写过也不影响，本讲会从零解释。

| 概念 | 直觉解释 |
|---|---|
| **kernel（内核）** | 一个「会被成千上万个线程同时执行」的函数。每个线程拿到自己的编号，处理数据的一小块。 |
| **host（宿主）/ device（设备）** | host = CPU + 主机内存；device = GPU + 显存。两者的代码、内存空间彼此隔离。 |
| **PTX** | NVIDIA 的并行线程指令集（一种类汇编的中间指令）。GPU 不直接执行 Rust，内核最终要变成 PTX。 |
| **grid / block** | 启动内核时把线程组织成「网格（grid）→ 线程块（block）→ 线程（thread）」三级层次。每个线程靠 `threadIdx`/`blockIdx`/`blockDim` 等内置量算出自己的全局编号。 |
| **单源编译（unified / single-source）** | host 代码和 device 代码写在**同一个 `.rs` 文件**里，一次编译同时产出 CPU 可执行码与 GPU 的 PTX。这正是 cuda-oxide 相对传统 CUDA Rust 方案的核心卖点。 |

传统 CUDA 编程里，内核（`.cu`）和宿主（`.cpp`）通常分开编译，还要靠 `nvcc` 这种特殊编译器。cuda-oxide 的目标用一句话概括（来自示例文件顶部注释）：

> **THIS IS THE GOAL: Single file, single compilation, no cfg splits.**（一个文件、一次编译、不用 `#[cfg]` 切分。）

## 3. 本讲源码地图

本讲聚焦两个核心文件，并配合几个支撑文件理解细节：

| 文件 | 作用 |
|---|---|
| `crates/rustc-codegen-cuda/examples/vecadd/src/main.rs` | **本讲主角**：一个同时包含内核与宿主 `main` 的单源文件。 |
| `crates/rustc-codegen-cuda/examples/vecadd/README.md` | vecadd 示例说明，含预期输出与「底层如何工作」清单。 |
| `crates/rustc-codegen-cuda/examples/vecadd/Cargo.toml` | 标记为独立 crate（用空 `[workspace]` 退出父 workspace），声明三个依赖。 |
| `crates/rustc-codegen-cuda/src/lib.rs` | 自定义 codegen 后端，文档里画了端到端架构图，是理解「分流」的权威来源。 |
| `crates/cuda-core/src/launch.rs` | `LaunchConfig` 与 `for_num_elems` 的定义。 |
| `crates/cuda-core/src/device_buffer.rs` | `DeviceBuffer` 的 `from_host` / `zeroed` / `to_host_vec` 搬运 API。 |
| `crates/cuda-device/src/thread.rs` | `thread::index_1d()` 与 `ThreadIndex` 见证类型。 |
| `crates/cuda-macros/src/lib.rs` | `#[cuda_module]` 展开出的 `LoadedModule`、`load()` 与类型化启动方法。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，对应规格里要求的四块：单源编译模型、`#[cuda_module]`/`#[kernel]`、宿主 `main` 与 `DeviceBuffer`、结果回收。最后在第 5 节用一个综合实践把它们串起来。

### 4.1 单源编译模型：一个文件如何同时产出 host 代码与 PTX

#### 4.1.1 概念说明

`vecadd/src/main.rs` 这一个文件里既有要跑在 GPU 上的内核（`vecadd`），又有跑在 CPU 上的 `main`。cuda-oxide 的关键能力是：**不需要你手动用 `#[cfg]` 把它们分开**，编译器自己会在代码生成（codegen）阶段判别哪部分是 device、哪部分是 host，然后分别走两条不同的后端流水线。

这是怎么做到的？答案在 u1-l1 已经埋下：内核函数被宏打上保留命名空间 `cuda_oxide_kernel_<hash>_*`，后端在 codegen 阶段扫描这些符号 + 走调用图可达性分析，就能识别出 device 代码，无需任何 `#[cfg]`。

#### 4.1.2 核心流程

文件顶部注释把整个流程讲得很清楚，翻译成步骤：

1. **rustc 前端**：解析 `main.rs`，做类型检查，为所有函数（包括 `vecadd` 和 `main`）生成**同一份 MIR**。
2. **rustc-codegen-cuda 介入**：作为 codegen 后端被 rustc 加载，`codegen_crate` 被调用。
3. **内核检测**：扫描代码生成单元（CGU），找到符号 `cuda_oxide_kernel_<hash>_vecadd`。
4. **device 路径**：从 `vecadd` 出发走调用图收集所有 device 函数，进入 cuda-oxide 流水线（dialect-mir → mem2reg → LLVM dialect → LLVM IR → llc → PTX），PTX 作为制品（`.oxart`）嵌入最终二进制。
5. **host 路径**：`main` 等宿主代码委托给标准 LLVM 后端，编成原生 x86_64 机器码。
6. **最终二进制**：既有宿主机器码，也内嵌了 PTX bundle。

注意第 32–34 行注释点出一个微妙之处：内核函数在 **host 端也存在于 MIR 中**（用于类型检查），但它的函数体在宿主侧**永远不会被调用**；真正执行的是 device 侧编译出的 PTX。

#### 4.1.3 源码精读

文件顶部的注释直接声明了「单文件、单编译、无 cfg 切分」的目标，并给出 3 步流程：

[crates/rustc-codegen-cuda/examples/vecadd/src/main.rs:6-19](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L6-L19) — 用中文说明：这段注释宣告了本示例就是 cuda-oxide 的「终极目标」——单文件单编译，注释里列出 rustc 解析、codegen-cuda 拦截并分流、最终二进制同时含 host 代码与内嵌 PTX 三步。

第 21 行特意写明 `// No #![cfg_attr(cuda_device, no_std)] - this compiles as ONE unit!`，强调没有 cfg 切分。

后端侧的权威架构图在 codegen 后端的模块文档里：

[crates/rustc-codegen-cuda/src/lib.rs:13-86](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/src/lib.rs#L13-L86) — 用中文说明：这是一张 ASCII 架构图，画出 rustc 前端产出 MIR 后，由 `rustc_codegen_cuda` 这个后端做「内核检测 → device 函数收集 → 分成 device/host 两路」。device 路走 cuda-oxide 流水线（dialect-mir → LLVM dialect → LLVM IR → llc → PTX），host 路委托标准 LLVM 后端编成 .o/.rlib。

`codegen_crate` 真正执行分流的入口与流程注释：

[crates/rustc-codegen-cuda/src/lib.rs:478-504](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/src/lib.rs#L478-L504) — 用中文说明：这是 `codegen_crate` 的文档流程图，描述它先取单态化条目、数内核数量，若有内核则 `collector::collect_device_functions` 走调用图收集、再 `device_codegen::generate_device_code` 跑流水线产出 .ll/.ptx，最后 `llvm_backend.codegen_crate` 让 LLVM 处理所有 host 代码。

分流在源码里的具体落地——device 跑完流水线后，host 仍然全部交给 LLVM：

[crates/rustc-codegen-cuda/src/lib.rs:732-741](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/src/lib.rs#L732-L741) — 用中文说明：device 代码处理完后，调用 `self.llvm_backend.codegen_crate(tcx, crate_info)` 把**所有 host 代码**交给标准 LLVM 后端，结果连同 device 制品对象一起打包返回。这就是「host 路径走标准 LLVM」的代码出处。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：亲眼确认「device/host 在同一份 MIR 上分流」这件事。
2. **操作步骤**：
   - 打开 [crates/rustc-codegen-cuda/src/lib.rs:13-86](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/src/lib.rs#L13-L86) 的架构图。
   - 对照 [vecadd/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs)，在纸上把文件里的 `#[kernel] fn vecadd` 圈为「device」，把 `fn main` 圈为「host」。
   - 用一句话写出：rustc 给两者生成 MIR 后，它们各自被路由到哪条流水线。
3. **需要观察的现象**：你会确认 vecadd 走 cuda-oxide 流水线（终点 PTX），main 走标准 LLVM（终点 x86_64）。
4. **预期结果**：能画出一张「源码 → MIR →（vecadd/device | main/host）→ PTX | 机器码 → 嵌入同一二进制」的草图。
5. 如果想看实际中间产物：可运行 `cargo oxide pipeline vecadd`（详见 u1-l3）查看生成的 MIR/.ll/.ptx——本机若无 GPU 也能编译产出这些中间文件，运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 vecadd 的函数体在宿主侧「存在但不会被调用」？
**答案**：因为它在宿主侧只参与类型检查（rustc 为所有函数生成 MIR），宿主 `main` 从不调用它；真正执行的是 device 侧编译出的 PTX，宿主只是通过加载内嵌 PTX 来启动它。

**练习 2**：如果完全删掉 `#[kernel]` 标注，会发生什么？
**答案**：该函数不会被打上 `cuda_oxide_kernel_*` 保留命名空间，codegen 后端的内核检测扫描不到它，于是它会被当作普通 host 函数编进 x86_64 机器码，根本不会产出 PTX；运行时 `kernels::load` 也找不到对应入口。

---

### 4.2 `#[cuda_module]` 与 `#[kernel]`：设备代码的标记与展开

#### 4.2.1 概念说明

在 vecadd 里，内核不是孤立存在的，而是包在一个被 `#[cuda_module]` 标注的模块里：

```rust
#[cuda_module]
mod kernels {
    use super::*;
    #[kernel]
    pub fn vecadd(a: &[f32], b: &[f32], mut c: DisjointSlice<f32>) { ... }
}
```

两个宏分工：

- `#[kernel]` 标注「这个函数是 GPU 内核」，并把它的名字改写成保留命名空间 `cuda_oxide_kernel_<hash>_vecadd`——这正是 4.1 节内核检测赖以工作的「暗号」。
- `#[cuda_module]` 扫描模块内所有 `#[kernel]` 函数，**在编译期生成一个 `LoadedModule` 结构体**，里面为每个内核生成一个**类型安全**的启动方法（这里就叫 `vecadd`），外加一个 `load()` 加载器。

这样宿主侧就能写出 `kernels::load(&ctx)?.vecadd(...)` 这样有自动补全、参数类型正确的调用——而不必手写字符串名去查 PTX 入口。

#### 4.2.2 核心流程

宏展开（编译期）做的事：

1. `#[kernel]` 把 `fn vecadd` 改名为保留符号 `cuda_oxide_kernel_<hash>_vecadd`（hash 用于泛型单态化去重，详见 u2-l6）。
2. `#[cuda_module]` 收集模块内所有内核签名，生成：
   - 一个 `LoadedModule` 结构体，每个内核对应一个字段（持有 `CudaFunction` 句柄）。
   - `load(ctx)` / `load_named(ctx, name)`：从内嵌制品加载 PTX 并解析出每个内核的函数句柄。
   - `impl LoadedModule` 上每个内核一个启动方法（如 `vecadd(&self, stream, config, a, b, c)`），参数类型直接来自内核签名。
3. 运行时，宿主调用 `load` 得到 `LoadedModule`，再调 `module.vecadd(...)`，宏生成的方法会把参数编组（marshal）后调用 CUDA Driver API 启动内核。

#### 4.2.3 源码精读

vecadd 的内核定义：

[crates/rustc-codegen-cuda/examples/vecadd/src/main.rs:35-47](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L35-L47) — 用中文说明：`#[cuda_module] mod kernels` 包住 `#[kernel] pub fn vecadd`。内核签名是 `a: &[f32]`、`b: &[f32]`（只读输入切片）与 `mut c: DisjointSlice<f32>`（可写输出，`DisjointSlice` 保证每个线程写唯一位置，见 4.2 注释与 u2-l2）。函数体先算线程索引，再越界安全地写入 `a[idx]+b[idx]`。

`#[cuda_module]` 生成的 `LoadedModule` 与 `load`：

[crates/cuda-macros/src/lib.rs:444-459](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L444-L459) — 用中文说明：宏展开出一个 `LoadedModule` 结构体（内含 `Arc<CudaModule>`、泛型函数缓存表，以及每个内核一个字段 `#(#function_fields)*`），并提供 `load(ctx)`，它内部调 `load_named(ctx, env!("CARGO_PKG_NAME"))`——用当前 crate 名作为制品 bundle 名去加载内嵌 PTX。

启动方法的批量生成：

[crates/cuda-macros/src/lib.rs:485-494](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L485-L494) — 用中文说明：在 `impl LoadedModule` 块里，宏为每个内核生成一个启动方法 `#(#launch_methods)*`。也就是说 `module.vecadd(...)` 这个方法是编译期根据内核签名自动生成的，所以宿主调用有类型检查和自动补全。

> 注：`#[kernel]` 与 `#[cuda_module]` 的完整展开（含保留符号契约、`GenericCudaKernel` 标记类型）是 u2-l1 的主题，本讲只点到为止。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：理解「内核签名如何决定宿主调用方法」。
2. **操作步骤**：
   - 读 [vecadd/src/main.rs:40-46](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L40-L46) 的内核签名 `(a: &[f32], b: &[f32], mut c: DisjointSlice<f32>)`。
   - 再读宿主调用 [vecadd/src/main.rs:76-84](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L76-L84)。
   - 对比两者参数：`&a_dev`、`&b_dev`、`&mut c_dev` 与内核的 `a`、`b`、`mut c` 一一对应。
3. **需要观察的现象**：宿主方法名 `vecadd` 与内核函数名一致；参数顺序、可变性（`mut c` 对应 `&mut c_dev`）也一致。
4. **预期结果**：能说出「改内核签名 → 宿主方法签名自动跟着变；类型不匹配会在编译期报错」。
5. 运行时验证**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么用 `DisjointSlice<f32>` 而不是 `&mut [f32]` 作为输出？
**答案**：`DisjointSlice` 配合 `ThreadIndex` 见证类型，保证每个线程只写自己那一个位置，从类型层面消除并行写的数据竞争（详见 u2-l2）。普通 `&mut [f32]` 无法在类型上表达「每个线程持有一份不相交的写权限」。

**练习 2**：`kernels::load(&ctx)` 里的 `kernels` 是什么？
**答案**：就是 `#[cuda_module] mod kernels` 声明的模块名；宏在该模块下生成了 `load`/`load_named`/`from_module` 等自由函数，所以可以直接 `kernels::load(...)` 调用。

---

### 4.3 宿主 `main` 与 `DeviceBuffer`：内存搬运的三段式

#### 4.3.1 概念说明

GPU 有自己独立的显存，宿主的 `Vec<f32>` 没法直接喂给内核。一个完整的 GPU 计算通常分三段：

1. **host → device**：把宿主数据搬上显存。
2. **device 上计算**：启动内核。
3. **device → host**：把结果搬回宿主。

cuda-oxide 用 `DeviceBuffer<T>` 这个 RAII 类型封装「显存里的一段连续 `T`」：它持有显存指针并在 `Drop` 时自动释放，并提供 `from_host`（搬上去）、`zeroed`（分配并清零）、`to_host_vec`（搬回来）等搬运 API。

#### 4.3.2 核心流程

vecadd 的 `main` 完整复刻这三段式：

1. **初始化**：`CudaContext::new(0)` 选 0 号 GPU 建上下文，`ctx.default_stream()` 取默认流。
2. **准备宿主数据**：`a_host`、`b_host` 两个 `Vec<f32>`（各 1024 个元素）。
3. **host → device**：`a_dev = DeviceBuffer::from_host(&stream, &a_host)`、`b_dev` 同理；`c_dev = DeviceBuffer::<f32>::zeroed(&stream, N)` 分配输出缓冲（初值 0）。
4. **加载并启动**：`module.vecadd(...)`（下一节细讲）。
5. **device → host**：`c_dev.to_host_vec(&stream)` 把结果搬回成 `Vec<f32>`。
6. **校验**：逐元素比对 `c_host[i]` 与 `a_host[i] + b_host[i]`。

启动配置 `LaunchConfig::for_num_elems(N as u32)` 会自动算出「开多少个线程块、每块多少线程」。它用固定块大小 256，按向上取整算网格大小：

\[
\text{grid\_x} = \left\lceil \frac{N}{256} \right\rceil
\]

对 \(N = 1024\)：\(\lceil 1024/256 \rceil = 4\)，即 grid = (4,1,1)、block = (256,1,1)，共 1024 个线程，恰好一人算一个元素。

#### 4.3.3 源码精读

宿主初始化与默认流：

[crates/rustc-codegen-cuda/examples/vecadd/src/main.rs:53-58](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L53-L58) — 用中文说明：`CudaContext::new(0)` 在 0 号设备上建上下文，`ctx.default_stream()` 取默认执行流；后续所有搬运与内核启动都挂在这条流上。

设备内存分配与 host→device 搬运：

[crates/rustc-codegen-cuda/examples/vecadd/src/main.rs:69-72](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L69-L72) — 用中文说明：`from_host` 把宿主切片拷上显存（并同步），`zeroed` 分配一段清零的显存作为输出缓冲 `c_dev`。

`from_host` 的实现——分配 + 拷贝 + 同步：

[crates/cuda-core/src/device_buffer.rs:330-355](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L330-L355) — 用中文说明：先用 `malloc_sync` 分配显存，再 `memcpy_htod_async` 把宿主数据异步拷上去，最后 `stream.synchronize()` 阻塞等拷贝完成。注释强调：同步是为了让借用来的宿主切片在函数返回后可立即被释放/复用而保持安全。

`zeroed` 的实现——分配 + 清零：

[crates/cuda-core/src/device_buffer.rs:453-474](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L453-L474) — 用中文说明：分配 `len` 个元素的显存后，用 `memset_d8_async` 把每个字节填 0，得到初值为 0 的输出缓冲。

`LaunchConfig` 与 `for_num_elems`：

[crates/cuda-core/src/launch.rs:19-45](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/launch.rs#L19-L45) — 用中文说明：`LaunchConfig` 含 `grid_dim`、`block_dim`、`shared_mem_bytes` 三字段；`for_num_elems(n)` 用固定块大小 256、`n.div_ceil(256)` 算网格，适合「线程号直接对应元素下标」的逐元素内核。

#### 4.3.4 代码实践（阅读 + 推算型）

1. **实践目标**：搞清 `for_num_elems` 如何把「元素数」映射成「grid/block」。
2. **操作步骤**：
   - 读 [launch.rs:36-44](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/launch.rs#L36-L44)。
   - 手算 `N = 1024`、`N = 1000`、`N = 1` 时的 grid 与 block。
3. **需要观察的现象**：
   - N=1024 → grid=(4,1,1), block=(256,1,1)（线程总数恰为 1024）。
   - N=1000 → grid=(4,1,1), block=(256,1,1)（线程总数 1024 ≥ 1000，多出的 24 个线程靠内核里的越界检查 `c.get_mut(idx)` 返回 `None` 而空转）。
   - N=1 → grid=(1,1,1), block=(256,1,1)。
4. **预期结果**：理解为何 `for_num_elems` 总是多算不多算——宁可多开线程再用越界检查兜底，也不能漏算元素。
5. 运行时验证**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`from_host` 为什么要在返回前 `synchronize`？
**答案**：它接收的是宿主借用切片 `&[T]`；若不等待拷贝完成就返回，调用方可能立刻释放/改写这片宿主内存，导致拷贝读到脏数据。同步保证返回时拷贝已完成，借用可安全结束。

**练习 2**：若把 `c_dev` 的 `zeroed` 换成 `from_host` 填入全 0 的向量，行为会一样吗？
**答案**：最终结果一样（都是初值 0），但 `zeroed` 直接在设备端 `memset`，省一次 host→device 拷贝，更高效。

---

### 4.4 模块加载、类型化启动与结果回收

#### 4.4.1 概念说明

第 3 段「device 上计算」与「device → host 回收」是闭环的最后两步。宿主侧：

- `kernels::load(&ctx)`：运行时从**当前可执行文件**里找到内嵌的 PTX bundle（4.1 节编进去的那个 `.oxart` 制品），加载成 `CudaModule`，再包成 `LoadedModule`。
- `module.vecadd(&stream, config, &a_dev, &b_dev, &mut c_dev)`：宏生成的类型化启动方法，把 `DeviceBuffer` 参数编组后调 CUDA Driver API `cuLaunchKernel` 真正在 GPU 上启动内核。
- `c_dev.to_host_vec(&stream)`：把输出缓冲整段拷回宿主 `Vec<f32>`，并同步流，返回后即可安全读取。

#### 4.4.2 核心流程

闭环的运行时步骤：

1. **加载**：`kernels::load(&ctx)` → `load_named(ctx, "vecadd")` → 从内嵌 bundle 取出 PTX → `cuModuleLoadData` 得到 `CudaModule` → `from_module` 包成 `LoadedModule`（每个内核字段持有一个 `CudaFunction` 句柄）。
2. **启动**：`module.vecadd(stream, config, a, b, c)` → 编组参数（指针、长度等）→ `cuLaunchKernel` 在指定流上以 `config` 的 grid/block 启动 `vecadd` 内核。每个线程执行：算索引 → 越界检查 → `c[i] = a[i] + b[i]`。
3. **回收**：`c_dev.to_host_vec(&stream)` → `memcpy_dtoh_async` 把显存拷回宿主 → `stream.synchronize()` 等拷贝（也等内核）完成 → 返回 `Vec<f32>`。
4. **校验**：宿主逐元素核对。

每个线程算自己索引的公式（1D 启动）为：

\[
\text{idx} = \text{blockIdx}_x \cdot \text{blockDim}_x + \text{threadIdx}_x
\]

这正是 `thread::index_1d()` 在 device 侧展开成的硬件内置量组合。

#### 4.4.3 源码精读

模块加载与类型化启动（宿主侧）：

[crates/rustc-codegen-cuda/examples/vecadd/src/main.rs:74-84](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L74-L84) — 用中文说明：`kernels::load(&ctx)` 加载内嵌 PTX 得到 `module`；`module.vecadd(&stream, LaunchConfig::for_num_elems(N as u32), &a_dev, &b_dev, &mut c_dev)` 以「每元素一线程」的配置启动内核。

结果回收：

[crates/rustc-codegen-cuda/examples/vecadd/src/main.rs:86-90](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L86-L90) — 用中文说明：`c_dev.to_host_vec(&stream)` 把设备输出拷回宿主 `Vec<f32>`，随后打印前 5 个元素。

`to_host_vec` 的实现——拷回 + 同步：

[crates/cuda-core/src/device_buffer.rs:480-493](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L480-L493) — 用中文说明：预分配容量后用 `memcpy_dtoh_async` 把整段设备缓冲拷回宿主指针，`stream.synchronize()` 等拷贝完成，再 `set_len` 暴露长度，返回安全的 `Vec<T>`。

内核体里线程索引的取法：

[crates/rustc-codegen-cuda/examples/vecadd/src/main.rs:40-46](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L40-L46) — 用中文说明：`thread::index_1d()` 返回类型安全的 `ThreadIndex`，`idx.get()` 取出原始 `usize` 用于切片 `a`/`b`，`c.get_mut(idx)` 做越界检查后返回 `Option<&mut f32>`，写入 `a[idx_raw]+b[idx_raw]`。

`index_1d` 在 device 侧的真实实现（宿主侧那个只是 `unreachable!` 桩，由 `#[kernel]` 宏改写调用点）：

[crates/cuda-device/src/thread.rs:288-296](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/thread.rs#L288-L296) — 用中文说明：真实 intrinsic 读三个硬件特殊寄存器 `threadIdx_x`、`blockIdx_x`、`blockDim_x`，算出 `bid*bdim+tid` 并封装成 `ThreadIndex`。宿主可见的 [index_1d 桩](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/thread.rs#L376-L381) 只是让 import/别名能解析，直接调用会 panic。

预期输出（来自示例 README）：

[crates/rustc-codegen-cuda/examples/vecadd/README.md:69-80](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/README.md#L69-L80) — 用中文说明：跑通后应看到 `c = [0.0, 3.0, 6.0, 9.0, 12.0]`（即 `a[i]+b[i]`）并以 `✓ SUCCESS: All 1024 elements correct!` 收尾。

#### 4.4.4 代码实践（阅读 + 推理型）

1. **实践目标**：理解 `load → launch → to_host_vec` 的先后依赖。
2. **操作步骤**：按 [main.rs:74-90](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L74-L90) 顺序读三段，画出「load 产出 module → vecadd 启动 → to_host_vec 回收」的时序。
3. **需要观察的现象**：`to_host_vec` 内部会 `synchronize`，所以它返回时内核一定已执行完、结果已拷回。
4. **预期结果**：能解释「为什么 `to_host_vec` 之后读 `c_host` 是安全的」——因为内部已同步流。
5. 运行时验证**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：如果在 `module.vecadd(...)` 之后、`to_host_vec` 之前，立刻读 `c_dev` 对应的宿主内存，会怎样？
**答案**：此时内核可能尚未执行完（启动是异步入队的），读到的可能是未更新的旧数据。必须像示例那样经过 `to_host_vec`（内部同步）或显式 `stream.synchronize()` 之后才能安全读取。

**练习 2**：`thread::index_1d()` 为什么在宿主侧是个会 panic 的桩？
**答案**：它依赖 GPU 硬件特殊寄存器，宿主 CPU 上没有意义。宿主侧保留这个函数只是为了让 `use` 与别名正常解析；真正生效的是 `#[kernel]` 宏把调用点改写成 `thread::__internal::index_1d`（device 侧真实实现）。

---

## 5. 综合实践

把 vecadd 内核从「向量加法」改成「向量减法」或「数乘」，重新编译运行并核对结果。这一步会把本讲四个模块全部串起来。

**实践目标**：亲手改一处 device 代码，观察它如何同时影响 PTX、宿主调用与最终结果，建立「单源编译」的体感。

**操作步骤**：

1. 备份原文件后，定位内核体 [vecadd/src/main.rs:40-46](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L40-L46)。
2. **方案 A（减法）**：把第 44 行
   ```rust
   *c_elem = a[idx_raw] + b[idx_raw];
   ```
   改成
   ```rust
   *c_elem = a[idx_raw] - b[idx_raw];
   ```
   并把第 95 行校验的 `expected` 从 `a_host[i] + b_host[i]` 改成 `a_host[i] - b_host[i]`。
3. **方案 B（数乘）**：把内核改成 `*c_elem = a[idx_raw] * b[idx_raw];`，校验改成 `a_host[i] * b_host[i]`。
4. 重新运行（按 u1-l3 的驱动方式）：
   ```bash
   cargo oxide run vecadd
   ```
5. 核对输出。

**需要观察的现象**：
- 方案 A 下，输出前 5 项应为 `a[i]-b[i]`：对 `a=[0,1,2,3,4]`、`b=[0,2,4,6,8]`，结果应为 `[0.0, -1.0, -2.0, -3.0, -4.0]`。
- 方案 B 下，前 5 项应为 `a[i]*b[i]`：`[0.0, 2.0, 8.0, 18.0, 32.0]`。
- 末尾仍应打印 `✓ SUCCESS: All 1024 elements correct!`。

**预期结果**：你只动了内核体一行和校验一行，重新编译后 GPU 上跑的就是新运算——这正是「单源、单编译」的直观证据：device 代码与 host 代码在同一文件、同一次编译里联动。

**若运行环境无 GPU 或工具链未就绪**：本实践需可用的 CUDA GPU 与 u1-l3 描述的工具链（nightly + llc-21 + CUDA Toolkit）。若条件不足，可退化为「源码阅读型」——只改源码、用 `cargo oxide build vecadd`（无需 GPU）确认能编译通过，并用 `cargo oxide pipeline vecadd` 查看生成的 PTX 是否反映了你的改动（搜 PTX 里的加/减/乘指令）。具体能否运行**待本地验证**。

## 6. 本讲小结

- vecadd 用**一个文件、一次编译**同时产出宿主 x86_64 机器码与设备 PTX，无需任何 `#[cfg]` 切分——这正是 cuda-oxide 的核心卖点。
- 分流发生在 codegen 后端的 `codegen_crate`：靠保留命名空间 `cuda_oxide_kernel_<hash>_*` + 调用图可达性识别 device 代码；host 代码全交标准 LLVM。
- `#[cuda_module]` 在编译期为每个 `#[kernel]` 生成类型安全的启动方法与 `load()` 加载器，所以宿主能写 `kernels::load(&ctx)?.vecadd(...)`。
- 宿主侧 `DeviceBuffer` 实现了 `from_host → 启动 → to_host_vec` 的三段式内存搬运，每段都正确处理了流的同步。
- `LaunchConfig::for_num_elems(N)` 用固定块大小 256、网格 \(\lceil N/256 \rceil\)，配内核内的越界检查兜底，保证「不漏算元素」。
- 运行时 `load` 从内嵌 `.oxart` 制品取出 PTX，`vecadd` 编组参数后 `cuLaunchKernel`，`to_host_vec` 同步后安全返回结果。

## 7. 下一步学习建议

本讲建立了端到端直觉，后续建议按依赖顺序深入：

- **想懂内核与索引安全**：进入 u2-l1（`#[kernel]`/`#[cuda_module]` 宏的完整展开与保留符号契约）和 u2-l2（`ThreadIndex` 见证类型与 `DisjointSlice` 如何在编译期消灭数据竞争）。
- **想懂启动与内存**：u2-l4（`LaunchConfig`、参数 marshalling、`cuLaunchKernel`）与 u2-l5（`DeviceBuffer` 的 RAII、`DeviceCopy` trait、锁页内存）。
- **想懂内嵌制品加载**：u3-l2（`.oxart` bundle wire 格式、锚符号防 dead-strip、运行时 bundle 发现）。
- **想自己跑更多例子**：u1-l5（examples 导览）会给你一张能力矩阵，挑感兴趣的（原子、集群、异步、张量核）继续。
- **想懂编译流水线细节**：U4 单元（后端入口、dialect-mir、mem2reg、LLVM 导出）会把 4.1 节那张架构图逐步展开成可读的源码。
