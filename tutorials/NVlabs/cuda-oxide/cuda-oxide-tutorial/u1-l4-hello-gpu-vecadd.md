# Hello GPU：vecadd 端到端

## 1. 本讲目标

前三讲(u1-l1 项目定位、u1-l2 crate 地图、u1-l3 cargo-oxide 驱动)让我们知道了 cuda-oxide「是什么」「由哪些 crate 组成」「怎么被驱动起来」。本讲是整个手册的第一次**端到端落地**——选一个最简单的例子 `vecadd`(向量加法),把「写内核 → 编译 → 宿主加载 → 启动 → 回收结果」这条完整闭环一次性走通,真正建立起**单源编译**的体感。

读完本讲,你应当能够:

- 读懂一个最小的 `#[cuda_module]` 内核模块与配套的宿主 `main`,并说清哪段代码最终跑在 GPU、哪段跑在 CPU。
- 看懂宿主侧 `DeviceBuffer` 的三段式内存搬运(host → device → host)。
- 描述 `kernels::load()` 如何把编译期嵌进可执行文件的 PTX 加载回来,并通过类型安全的 `module.vecadd(...)` 方法启动内核。
- 自己动手把内核改成向量减法或数乘,并核对结果正确。

> 本讲承接 u1-l1 建立的术语:kernel、PTX、MIR、codegen backend、device/host 分流、`-Z mir-enable-passes=-JumpThreading` 硬约束。这里不再重复定义,只在用到时点出。

## 2. 前置知识

如果你写过 CUDA C/C++,可以快速对照下表;如果没写过也不影响,本讲会从零解释。

| 概念 | 直觉解释 |
|---|---|
| **kernel(内核)** | 一个「会被成千上万个线程同时执行」的函数。每个线程拿到自己的编号,处理数据的一小块。 |
| **host(宿主)/ device(设备)** | host = CPU + 主机内存;device = GPU + 显存。两者的代码、内存空间彼此隔离。 |
| **PTX** | NVIDIA 的并行线程指令集(一种类汇编的中间指令)。GPU 不直接执行 Rust,内核最终要变成 PTX。 |
| **grid / block** | 启动内核时把线程组织成「网格(grid)→ 线程块(block)→ 线程(thread)」三级层次。每个线程靠 `threadIdx`/`blockIdx`/`blockDim` 等内置量算出自己的全局编号。 |
| **单源编译(unified / single-source)** | host 代码和 device 代码写在**同一个 `.rs` 文件**里,一次编译同时产出 CPU 可执行码与 GPU 的 PTX。这正是 cuda-oxide 相对传统 CUDA Rust 方案的核心卖点。 |

传统 CUDA 编程里,内核(`.cu`)和宿主(`.cpp`)通常分开编译,还要靠 `nvcc` 这种特殊编译器。cuda-oxide 的目标用一句话概括(来自示例文件顶部注释):

> **THIS IS THE GOAL: Single file, single compilation, no cfg splits.**(一个文件、一次编译、不用 `#[cfg]` 切分。)

## 3. 本讲源码地图

本讲聚焦一个核心文件,并配合几个支撑文件理解细节:

| 文件 | 作用 |
|---|---|
| `crates/rustc-codegen-cuda/examples/vecadd/src/main.rs` | **本讲主角**:一个同时包含内核与宿主 `main` 的单源文件。 |
| `crates/rustc-codegen-cuda/examples/vecadd/README.md` | vecadd 示例说明,含预期输出与硬件要求。 |
| `crates/rustc-codegen-cuda/examples/vecadd/Cargo.toml` | 标记为独立 crate(用空 `[workspace]` 退出父 workspace),声明三个依赖。 |
| `crates/rustc-codegen-cuda/src/lib.rs` | 自定义 codegen 后端,文档里画了端到端架构图,是理解「分流」的权威来源。 |
| `crates/cuda-core/src/launch.rs` | `LaunchConfig` 与 `for_num_elems` 的定义。 |
| `crates/cuda-core/src/device_buffer.rs` | `DeviceBuffer` 的 `from_host` / `zeroed` / `to_host_vec` 搬运 API。 |
| `crates/cuda-device/src/thread.rs` | `thread::index_1d()` 与 `ThreadIndex` 见证类型。 |
| `crates/cuda-device/src/disjoint.rs` | `DisjointSlice::get_mut` 的越界安全写入。 |
| `crates/cuda-macros/src/lib.rs` | `#[cuda_module]` 展开出的 `LoadedModule`、`load()` 与类型化启动方法。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块,对应规格里要求的四块:`#[cuda_module]` mod 块、`#[kernel] fn vecadd`、宿主 `main` 与 `DeviceBuffer`、加载启动与结果回收。最后在第 5 节用一个综合实践把它们串起来。

### 4.1 `#[cuda_module]` mod 块:单源编译的「设备代码容器」

#### 4.1.1 概念说明

在 vecadd 里,内核不是孤立存在的,而是包在一个被 `#[cuda_module]` 标注的模块里。这个模块就是「设备代码的容器」:你把若干个 `#[kernel]` 函数放进去,宏会在编译期扫描它们,并为整个模块生成一套宿主侧的「加载 + 启动」API。

关键点在于:**不需要你手动用 `#[cfg]` 把 host/device 分开**。后端在 codegen 阶段靠「保留命名空间 `cuda_oxide_kernel_<hash>_*` + 调用图可达性」自动识别 device 代码(详见 u1-l1),然后让 host/device 各走各的后端流水线。

#### 4.1.2 核心流程

文件顶部注释把整个流程讲得很清楚,翻译成步骤:

1. **rustc 前端**:解析 `main.rs`,做类型检查,为所有函数(包括 `vecadd` 和 `main`)生成**同一份 MIR**。
2. **rustc-codegen-cuda 介入**:作为 codegen 后端被 rustc 加载,`codegen_crate` 被调用。
3. **内核检测**:扫描代码生成单元(CGU),找到符号 `cuda_oxide_kernel_<hash>_vecadd`。
4. **device 路径**:从 `vecadd` 出发走调用图收集所有 device 函数,进入 cuda-oxide 流水线(dialect-mir → mem2reg → LLVM dialect → LLVM IR → llc → PTX),PTX 作为制品(`.oxart`)嵌入最终二进制。
5. **host 路径**:`main` 等宿主代码委托给标准 LLVM 后端,编成原生 x86_64 机器码。
6. **最终二进制**:既有宿主机器码,也内嵌了 PTX bundle。

注意注释点出一个微妙之处:内核函数在 **host 端也存在于 MIR 中**(用于类型检查),但它的函数体在宿主侧**永远不会被调用**;真正执行的是 device 侧编译出的 PTX。

#### 4.1.3 源码精读

文件顶部的注释直接声明了「单文件、单编译、无 cfg 切分」的目标,并给出 3 步流程:

[crates/rustc-codegen-cuda/examples/vecadd/src/main.rs:6-19](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L6-L19) —— 用中文说明:这段注释宣告本示例就是 cuda-oxide 的「终极目标」——单文件单编译,注释里列出 rustc 解析、codegen-cuda 拦截并分流、最终二进制同时含 host 代码与内嵌 PTX 三步。

第 21 行特意写明 `// No #![cfg_attr(cuda_device, no_std)] - this compiles as ONE unit!`,强调没有 cfg 切分。

后端侧的权威架构图在 codegen 后端的模块文档里:

[crates/rustc-codegen-cuda/src/lib.rs:15-86](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/src/lib.rs#L15-L86) —— 用中文说明:这是一张 ASCII 架构图,画出 rustc 前端产出 MIR 后,由 `rustc_codegen_cuda` 后端做「内核检测 → device 函数收集 → 分成 device/host 两路」。device 路走 cuda-oxide 流水线(dialect-mir → LLVM dialect → LLVM IR → llc → PTX),host 路委托标准 LLVM 后端编成 .o/.rlib。其中 DEVICE PATH 与 HOST PATH 的分流框见 [lib.rs:57-81](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/src/lib.rs#L57-L81)。

`codegen_crate` 真正执行分流的入口与流程文档:

[crates/rustc-codegen-cuda/src/lib.rs:478-505](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/src/lib.rs#L478-L505) —— 用中文说明:这是 `codegen_crate` 的文档流程图与函数签名(505 行),描述它先取单态化条目、数内核数量,若有内核则 `collector::collect_device_functions` 走调用图收集、再 `device_codegen::generate_device_code` 跑流水线产出 .ll/.ptx。

分流在源码里的具体落地——device 跑完流水线后,host 仍然全部交给 LLVM:

[crates/rustc-codegen-cuda/src/lib.rs:730-741](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/src/lib.rs#L730-L741) —— 用中文说明:device 代码处理完后,调用 `self.llvm_backend.codegen_crate(tcx, crate_info)` 把**所有 host 代码**交给标准 LLVM 后端,结果连同 device 制品对象一起打包返回。这就是「host 路径走标准 LLVM」的代码出处。

#### 4.1.4 代码实践(源码阅读型)

1. **实践目标**:亲眼确认「device/host 在同一份 MIR 上分流」这件事。
2. **操作步骤**:
   - 打开 [lib.rs:15-86](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/src/lib.rs#L15-L86) 的架构图。
   - 对照 [vecadd/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs),在纸上把文件里的 `#[kernel] fn vecadd` 圈为「device」,把 `fn main` 圈为「host」。
   - 用一句话写出:rustc 给两者生成 MIR 后,它们各自被路由到哪条流水线。
3. **需要观察的现象**:你会确认 vecadd 走 cuda-oxide 流水线(终点 PTX),main 走标准 LLVM(终点 x86_64)。
4. **预期结果**:能画出一张「源码 → MIR →(vecadd/device | main/host)→ PTX | 机器码 → 嵌入同一二进制」的草图。
5. 若想看实际中间产物:可运行 `cargo oxide pipeline vecadd`(详见 u1-l3)查看生成的 MIR/.ll/.ptx——本机若无 GPU 也能编译产出这些中间文件,运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**:为什么 vecadd 的函数体在宿主侧「存在但不会被调用」?
**答案**:因为它在宿主侧只参与类型检查(rustc 为所有函数生成 MIR),宿主 `main` 从不调用它;真正执行的是 device 侧编译出的 PTX,宿主只是通过加载内嵌 PTX 来启动它。

**练习 2**:如果完全删掉 `#[kernel]` 标注,会发生什么?
**答案**:该函数不会被打上 `cuda_oxide_kernel_*` 保留命名空间,codegen 后端的内核检测扫描不到它,于是它会被当作普通 host 函数编进 x86_64 机器码,根本不会产出 PTX;运行时 `kernels::load` 也找不到对应入口。

---

### 4.2 `#[kernel] fn vecadd`:写在 GPU 上跑的函数

#### 4.2.1 概念说明

`#[kernel]` 标注「这个函数是 GPU 内核」,宏会把它的名字改写成保留命名空间 `cuda_oxide_kernel_<hash>_vecadd`——这正是 4.1 节内核检测赖以工作的「暗号」。vecadd 的内核虽短,却体现了 CUDA 编程的两个关键点:

1. **「我是哪个线程」从哪来**:GPU 上同时跑着成千上万个线程,每个线程需要知道自己该处理第几个元素。`thread::index_1d()` 返回一个代表「我」的线程号。
2. **怎么安全地并行写结果**:1024 个线程同时往 `c` 里写,若两个线程写同一位置就是数据竞争。`DisjointSlice` 配合 `ThreadIndex` 见证类型,保证每个线程只写自己那一格。

#### 4.2.2 核心流程

vecadd 内核的逻辑:

```
每个线程各自执行:
  idx ← thread::index_1d()          // 我的全局线程号(1D)
  i   ← idx.get()                    // 取出原始 usize,用于读 a/b
  如果 idx 在 c 的范围内:
      c[idx] ← a[i] + b[i]           // 越界检查通过才写
```

线程号的计算公式(1D 启动)为:

\[
\text{idx} = \text{blockIdx}_x \cdot \text{blockDim}_x + \text{threadIdx}_x
\]

注意参数的区别:`a`、`b` 是所有线程**只读**的共享切片 `&[f32]`;`c` 是每个线程**写自己格子**的 `DisjointSlice<f32>`。读用普通 `usize` 下标即可,写却必须经过 `get_mut(idx)` 这个带越界检查、且要求传 `ThreadIndex` 见证类型的方法。

#### 4.2.3 源码精读

vecadd 的内核定义与函数体:

[crates/rustc-codegen-cuda/examples/vecadd/src/main.rs:35-47](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L35-L47) —— 用中文说明:`#[cuda_module] mod kernels` 包住 `#[kernel] pub fn vecadd`。签名是 `a: &[f32]`、`b: &[f32]`(只读输入)与 `mut c: DisjointSlice<f32>`(可写输出)。函数体先 `thread::index_1d()` 算线程索引,`idx.get()` 取原始 `usize`,再 `c.get_mut(idx)` 越界安全地写入 `a[idx]+b[idx]`。

`thread::index_1d()` 在 device 侧的真实实现(宿主侧那个只是 `unreachable!` 桩,由 `#[kernel]` 宏改写调用点):

[crates/cuda-device/src/thread.rs:288-296](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/thread.rs#L288-L296) —— 用中文说明:真实 intrinsic 读三个硬件特殊寄存器 `threadIdx_x`、`blockIdx_x`、`blockDim_x`,算出 `bid*bdim+tid` 并封装成 `ThreadIndex`。宿主可见的公开桩见 [thread.rs:376-381](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/thread.rs#L376-L381),它只是 `unreachable!()`,只为让 import/别名能解析,直接调用会 panic。

`idx.get()` 取原始索引:

[crates/cuda-device/src/thread.rs:226-229](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/thread.rs#L226-L229) —— 用中文说明:返回内部 `usize`,用于在只读切片 `a`/`b` 上做普通下标。

`DisjointSlice::get_mut` 的越界安全写入:

[crates/cuda-device/src/disjoint.rs:178-191](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/disjoint.rs#L178-L191) —— 用中文说明:越界返回 `None`;在界内才返回 `&mut T`。它的安全性来自「`ThreadIndex` 只能由可信函数从硬件寄存器构造且每线程唯一」,所以并行写不会撞车。这就是 vecadd 不需要手写 `if idx < N` 也安全的原因。

> 小贴士:为什么读用 `idx_raw`(普通 `usize`)、写却必须用 `idx`(`ThreadIndex` 见证类型)?因为 `DisjointSlice::get_mut` 要求传一个**证明过唯一性**的索引,普通 `usize` 没有这个保证、会被类型系统拒绝。这是 u2-l2 会深入的「类型安全」主题,本讲记住这个写法即可。

#### 4.2.4 代码实践(源码阅读型)

1. **实践目标**:理解「线程数 ≥ 元素数」时为什么不会越界崩溃。
2. **操作步骤**:
   - 读 [disjoint.rs:178-191](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/disjoint.rs#L178-L191) 的 `get_mut`。
   - 假设 `N = 1000`,启动配置用 `for_num_elems(1000)`(见 4.4),算出 grid = ⌈1000/256⌉ = 4 个 block、每 block 256 线程,共 1024 个线程。此时第 1000~1023 号线程的 `idx` 越界。
3. **需要观察的现象**:`get_mut` 对越界 `idx` 返回 `None`,`if let Some` 分支不执行,这些线程什么都不写——程序安全。
4. **预期结果**:能解释「为什么 vecadd 不需要手动 `if idx < N` 也安全」——因为 `get_mut` 已把越界检查包进了返回值。
5. 运行时验证**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**:`thread::index_1d()` 如果在 host 的 `main` 里被调用,会发生什么?
**答案**:会触发 `unreachable!()` panic。公开 `index_1d` 只是个桩,真正的实现是宏改写后的内部 intrinsic,只在 device 上有意义。

**练习 2**:为什么 `c` 用 `DisjointSlice<f32>` 而不是 `&mut [f32]`?
**答案**:`DisjointSlice` 配合 `ThreadIndex` 保证每个线程只写一个**唯一**位置,从而在「没有锁、没有同步」的情况下也不发生数据竞争;普通 `&mut [f32]` 无法在 Rust 类型系统里表达「并行各自写不同格子」的合约(详见 u2-l2)。

---

### 4.3 宿主 `main` 与 `DeviceBuffer`:内存搬运的三段式

#### 4.3.1 概念说明

GPU 有自己独立的显存,宿主的 `Vec<f32>` 没法直接喂给内核。一个完整的 GPU 计算通常分三段:

1. **host → device**:把宿主数据搬上显存。
2. **device 上计算**:启动内核(下一节细讲)。
3. **device → host**:把结果搬回宿主。

cuda-oxide 用 `DeviceBuffer<T>` 这个 RAII 类型封装「显存里的一段连续 `T`」:它持有显存指针并在 `Drop` 时自动释放,并提供 `from_host`(搬上去)、`zeroed`(分配并清零)、`to_host_vec`(搬回来)等搬运 API。这些 API 都要求元素类型实现 `DeviceCopy`(本质是「可按字节原样拷贝」),`f32` 已经实现了。

#### 4.3.2 核心流程

vecadd 的 `main` 前半段复刻了前两段搬运:

1. **初始化**:`CudaContext::new(0)` 选 0 号 GPU 建上下文,`ctx.default_stream()` 取默认流。
2. **准备宿主数据**:`a_host`、`b_host` 两个 `Vec<f32>`(各 1024 个元素)。
3. **host → device**:`a_dev = DeviceBuffer::from_host(&stream, &a_host)`、`b_dev` 同理;`c_dev = DeviceBuffer::<f32>::zeroed(&stream, N)` 分配输出缓冲(初值 0)。

`from_host` 会**同步等待**搬运完成才返回,所以返回后 host 数据可以立即释放或复用。

#### 4.3.3 源码精读

宿主初始化与默认流:

[crates/rustc-codegen-cuda/examples/vecadd/src/main.rs:53-58](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L53-L58) —— 用中文说明:`CudaContext::new(0)` 在 0 号设备上建上下文,`ctx.default_stream()` 取默认执行流;后续所有搬运与内核启动都挂在这条流上。

设备内存分配与 host→device 搬运:

[crates/rustc-codegen-cuda/examples/vecadd/src/main.rs:69-72](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L69-L72) —— 用中文说明:`from_host` 把宿主切片拷上显存(并同步),`zeroed` 分配一段清零的显存作为输出缓冲 `c_dev`。

`DeviceBuffer` 结构体本身(持原始设备指针、元素数、字节数、引用计数的上下文,drop 时自动 `cuMemFree`):

[crates/cuda-core/src/device_buffer.rs:131-146](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L131-L146) —— 用中文说明:可类比「GPU 上的 `Vec<T>`」,所有搬运 API 都在 `impl<T: DeviceCopy> DeviceBuffer<T>` 下。

`from_host` 的实现——分配 + 拷贝 + 同步:

[crates/cuda-core/src/device_buffer.rs:330-355](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L330-L355) —— 用中文说明:先用 `malloc_sync` 分配显存,再 `memcpy_htod_async` 把宿主数据拷上去,最后 `stream.synchronize()` 阻塞等拷贝完成。同步是为了让借用的宿主切片在函数返回后可立即被释放/复用而保持安全。注意它先拿到显存所有权再入队拷贝,若拷贝失败提早 return 会触发 `Drop` 释放显存,不泄漏。

`zeroed` 的实现——分配 + 清零:

[crates/cuda-core/src/device_buffer.rs:453-474](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L453-L474) —— 用中文说明:分配 `len` 个元素的显存后,用 `memset_d8_async` 把每个字节填 0,得到初值为 0 的输出缓冲。

`DeviceCopy` trait:`f32` 等基础类型都实现了它;含 `String` 这种「带主机所有权」的类型会被拒绝。

[crates/cuda-core/src/device_buffer.rs:35-54](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L35-L54) —— 用中文说明:这是设备内存的「按字节原样拷贝」合约,`Copy` 不够(`bool`/`char` 等并非所有字节模式都合法),`DeviceCopy` 是更强的承诺。

#### 4.3.4 代码实践(阅读 + 推算型)

1. **实践目标**:体会「输入要搬上去、输出要先申请」的搬运模型。
2. **操作步骤**:
   - 读 [main.rs:69-72](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L69-L72),数一下有几个 `from_host`、几个 `zeroed`,并解释数量为什么是这样。
   - 对照 [device_buffer.rs:330-355](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L330-L355),指出 `from_host` 内部哪一步是「真正把字节从 CPU 拷到 GPU」(提示:`memcpy_htod_async`)。
3. **需要观察的现象**:输入 `a`/`b` 各搬一次(两次 `from_host`),输出 `c` 只需申请清零(一次 `zeroed`)。
4. **预期结果**:能说清「输入要 H2D、输出先 zeroed」的区别。若把 `c_dev` 换成 `uninitialized`(不清零),未写入位置会是垃圾值,所以**输出缓冲**用 `zeroed` 更安全。
5. 运行时验证**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**:`from_host` 为什么要在返回前 `synchronize`?
**答案**:它接收的是宿主借用切片 `&[T]`;若不等待拷贝完成就返回,调用方可能立刻释放/改写这片宿主内存,导致拷贝读到脏数据。同步保证返回时拷贝已完成,借用可安全结束。异步不阻塞的版本是 `from_host_async_unchecked`(标注了 `unsafe`)。

**练习 2**:把 `c_dev` 的 `zeroed` 换成 `from_host` 填入全 0 的向量,行为会一样吗?
**答案**:最终结果一样(都是初值 0),但 `zeroed` 直接在设备端 `memset`,省一次 host→device 拷贝,更高效。

---

### 4.4 加载、启动与 `to_host_vec`:跑通闭环并回收结果

#### 4.4.1 概念说明

数据上了显存,接下来三步把闭环走完:

- `kernels::load(&ctx)`:运行时从**当前可执行文件**里找到内嵌的 PTX bundle(4.1 节编进去的那个 `.oxart` 制品),加载成 `CudaModule`,再包成 `LoadedModule`。
- `module.vecadd(&stream, config, &a_dev, &b_dev, &mut c_dev)`:宏生成的类型化启动方法,把 `DeviceBuffer` 参数编组(marshal)后调 CUDA Driver API 在 GPU 上启动内核。
- `c_dev.to_host_vec(&stream)`:把输出缓冲整段拷回宿主 `Vec<f32>`,并同步流,返回后即可安全读取。

#### 4.4.2 核心流程

闭环的运行时步骤:

1. **加载**:`kernels::load(&ctx)` → 内部 `load_named(ctx, env!("CARGO_PKG_NAME"))`(即 `"vecadd"`)→ 从内嵌 bundle 取出 PTX → 得到 `CudaModule` → `from_module` 包成 `LoadedModule`(每个内核字段持有一个 `CudaFunction` 句柄)。
2. **启动**:`module.vecadd(stream, config, a, b, c)` → 编组参数 → `cuLaunchKernel` 在指定流上以 `config` 的 grid/block 启动 `vecadd`。每个线程执行:算索引 → 越界检查 → `c[i] = a[i] + b[i]`。
3. **回收**:`c_dev.to_host_vec(&stream)` → `memcpy_dtoh_async` 把显存拷回宿主 → `stream.synchronize()` 等拷贝(也等内核)完成 → 返回 `Vec<f32>`。
4. **校验**:宿主逐元素核对。

启动配置 `LaunchConfig::for_num_elems(N)` 用固定块大小 256,grid 按向上取整算出:

\[
\text{grid\_x} = \left\lceil \frac{N}{256} \right\rceil, \qquad \text{block} = 256
\]

对 \(N = 1024\):grid_x = 4、block = 256,共 1024 个线程,恰好一人算一个元素。

#### 4.4.3 源码精读

模块加载与类型化启动(宿主侧):

[crates/rustc-codegen-cuda/examples/vecadd/src/main.rs:75-84](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L75-L84) —— 用中文说明:`kernels::load(&ctx)` 加载内嵌 PTX 得到 `module`;`module.vecadd(&stream, LaunchConfig::for_num_elems(N as u32), &a_dev, &b_dev, &mut c_dev)` 以「每元素一线程」的配置启动内核。注意启动方法的**参数顺序与内核签名一一对应**(`a, b, mut c`),前面额外多了 `&stream` 和 `LaunchConfig`。

`kernels::load` 与 `LoadedModule` 是 `#[cuda_module]` 宏生成的:

[crates/cuda-macros/src/lib.rs:442-459](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L442-L459) —— 用中文说明:宏展开出一个 `LoadedModule` 结构体(内含 `Arc<CudaModule>`、泛型函数缓存表,以及每个内核一个字段 `#(#function_fields)*`),并提供 `load(ctx)`,内部调 `load_named(ctx, env!("CARGO_PKG_NAME"))`——用当前 crate 名作为制品 bundle 名去加载内嵌 PTX。

启动方法的批量生成:

[crates/cuda-macros/src/lib.rs:485-494](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L485-L494) —— 用中文说明:在 `impl LoadedModule` 块里,宏为每个内核生成一个启动方法 `#(#launch_methods)*`。`module.vecadd(...)` 就是编译期根据内核签名自动生成的,所以宿主调用有类型检查和自动补全。

`for_num_elems` 的实现(就是上面那个公式):

[crates/cuda-core/src/launch.rs:36-44](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/launch.rs#L36-L44) —— 用中文说明:block 固定 256,grid 用 `n.div_ceil(256)`,不申请动态共享内存,适合「线程号直接对应元素下标」的逐元素内核。

结果回收:

[crates/rustc-codegen-cuda/examples/vecadd/src/main.rs:87-90](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L87-L90) —— 用中文说明:`c_dev.to_host_vec(&stream)` 把设备输出拷回宿主 `Vec<f32>`,随后打印前 5 个元素。

`to_host_vec` 的实现——拷回 + 同步:

[crates/cuda-core/src/device_buffer.rs:480-493](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/device_buffer.rs#L480-L493) —— 用中文说明:预分配容量后用 `memcpy_dtoh_async` 把整段设备缓冲拷回宿主指针,`stream.synchronize()` 等拷贝完成,再 `set_len` 暴露长度,返回安全的 `Vec<T>`。同步保证返回的 `Vec` 立刻可读。

预期输出(来自示例 README):

[crates/rustc-codegen-cuda/examples/vecadd/README.md:68-82](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/README.md#L68-L82) —— 用中文说明:跑通后应看到 `c = [0.0, 3.0, 6.0, 9.0, 12.0]`(即 `a[i]+b[i]`)并以 `✓ SUCCESS: All 1024 elements correct!` 收尾。本讲写作环境无 GPU,此为文档记载的预期输出,**待本地验证**。

#### 4.4.4 代码实践(阅读 + 推理型)

1. **实践目标**:理解 `load → launch → to_host_vec` 的先后依赖与同步点。
2. **操作步骤**:按 [main.rs:75-90](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L75-L90) 顺序读三段,画出「load 产出 module → vecadd 启动 → to_host_vec 回收」的时序。
3. **需要观察的现象**:`to_host_vec` 内部会 `synchronize`,所以它返回时内核一定已执行完、结果已拷回。
4. **预期结果**:能解释「为什么 `to_host_vec` 之后读 `c_host` 是安全的」——因为内部已同步流。
5. 运行时验证**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**:`module.vecadd(...)` 的参数顺序是怎么定的?
**答案**:除了开头插入的 `&stream` 和 `LaunchConfig`,其余参数与内核签名 `fn vecadd(a: &[f32], b: &[f32], mut c: DisjointSlice<f32>)` **一一对应**。这是 `#[cuda_module]` 宏按签名自动生成的。

**练习 2**:如果在 `module.vecadd(...)` 之后、`to_host_vec` 之前,立刻读 `c_dev` 对应的宿主内存,会怎样?
**答案**:内核启动是异步入队的,此时可能尚未执行完,读到的可能是未更新的旧数据。必须像示例那样经过 `to_host_vec`(内部同步)或显式 `stream.synchronize()` 之后才能安全读取。

---

## 5. 综合实践

把 vecadd 内核从「向量加法」改成「向量减法」或「数乘」,重新编译运行并核对结果。这一步会把本讲四个模块全部串起来。

**实践目标**:亲手改一处 device 代码,观察它如何同时影响 PTX、宿主调用与最终结果,建立「单源编译」的体感。

**操作步骤**:

1. 备份原文件后,定位内核体 [vecadd/src/main.rs:39-46](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L39-L46)。
2. **方案 A(减法)**:把第 44 行
   ```rust
   *c_elem = a[idx_raw] + b[idx_raw];
   ```
   改成
   ```rust
   *c_elem = a[idx_raw] - b[idx_raw];
   ```
   并把第 95 行校验的 `expected` 从 `a_host[i] + b_host[i]` 改成 `a_host[i] - b_host[i]`。
3. **方案 B(数乘)**:把内核改成 `*c_elem = a[idx_raw] * b[idx_raw];`,校验改成 `a_host[i] * b_host[i]`。
4. 重新运行(按 u1-l3 的驱动方式):
   ```bash
   cargo oxide run vecadd
   ```
5. 核对输出。

**需要观察的现象**:

- 方案 A 下,输出前 5 项应为 `a[i]-b[i]`:对 `a=[0,1,2,3,4]`、`b=[0,2,4,6,8]`,结果应为 `[0.0, -1.0, -2.0, -3.0, -4.0]`。
- 方案 B 下,前 5 项应为 `a[i]*b[i]`:`[0.0, 2.0, 8.0, 18.0, 32.0]`。
- 末尾仍应打印 `✓ SUCCESS: All 1024 elements correct!`。

**预期结果**:你只动了内核体一行和校验一行,重新编译后 GPU 上跑的就是新运算——这正是「单源、单编译」的直观证据:device 代码与 host 代码在同一文件、同一次编译里联动。

**若运行环境无 GPU 或工具链未就绪**:本实践需可用的 CUDA GPU 与 u1-l3 描述的工具链(nightly + llc-21 + CUDA Toolkit)。若条件不足,可退化为「源码阅读型」——只改源码、用 `cargo oxide build vecadd`(无需 GPU)确认能编译通过,并用 `cargo oxide pipeline vecadd` 查看生成的 PTX 是否反映了你的改动(在 PTX 文本里搜加/减/乘指令)。具体能否运行**待本地验证**。

## 6. 本讲小结

- vecadd 用**一个文件、一次编译**同时产出宿主 x86_64 机器码与设备 PTX,无需任何 `#[cfg]` 切分——这正是 cuda-oxide 的核心卖点。
- 分流发生在 codegen 后端的 `codegen_crate`:靠保留命名空间 `cuda_oxide_kernel_<hash>_*` + 调用图可达性识别 device 代码;host 代码全交标准 LLVM 后端(`llvm_backend.codegen_crate`)。
- `#[cuda_module]` 在编译期为每个 `#[kernel]` 生成 `LoadedModule` 结构体、类型安全的启动方法与 `load()` 加载器,所以宿主能写 `kernels::load(&ctx)?.vecadd(...)`。
- 内核靠 `thread::index_1d()` 拿线程号、靠 `DisjointSlice::get_mut` 越界安全地并行写各自那一格,所以线程数略多于元素数也安全。
- 宿主侧 `DeviceBuffer` 实现了 `from_host → 启动 → to_host_vec` 的三段式内存搬运,每段都正确处理了流的同步;`LaunchConfig::for_num_elems(N)` 用固定块大小 256、网格 \(\lceil N/256 \rceil\),配内核内越界检查兜底。

## 7. 下一步学习建议

本讲建立了端到端直觉,后续建议按依赖顺序深入:

- **想懂内核与索引安全**:进入 u2-l1(`#[kernel]`/`#[cuda_module]` 宏的完整展开与保留符号契约)和 u2-l2(`ThreadIndex` 见证类型与 `DisjointSlice` 如何在编译期消灭数据竞争)。
- **想懂启动与内存**:u2-l4(`LaunchConfig`、参数 marshalling、`cuLaunchKernel`)与 u2-l5(`DeviceBuffer` 的 RAII、`DeviceCopy` trait、锁页内存)。
- **想懂内嵌制品加载**:u3-l2(`.oxart` bundle wire 格式、锚符号防 dead-strip、运行时 bundle 发现)。
- **想自己跑更多例子**:u1-l5(examples 导览)会给你一张能力矩阵,挑感兴趣的(原子、集群、异步、张量核)继续。
- **想懂编译流水线细节**:U4 单元(后端入口、dialect-mir、mem2reg、LLVM 导出)会把 4.1 节那张架构图逐步展开成可读的源码。
