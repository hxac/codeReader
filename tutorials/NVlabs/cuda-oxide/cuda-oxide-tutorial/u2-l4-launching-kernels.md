# 从宿主启动内核

## 1. 本讲目标

在 u2-l1 里我们看到了 `#[kernel]` 与 `#[cuda_module]` 如何在编译期把内核函数变成带类型的 `module.vecadd(...)` 启动方法。本讲回答紧接着的下一个问题:**这个 `module.vecadd(...)` 在运行时到底做了什么,内核是怎么真正在 GPU 上跑起来的。**

学完本讲你应该能够:

- 说出 `LaunchConfig` 三个字段的含义,并能解释 grid / block / shared memory 分别决定什么。
- 手写一个 `LaunchConfig`,并说明它与 `LaunchConfig::for_num_elems(n)` 自动算出的配置之间的换算关系。
- 复述「参数 marshalling」的全过程:宿主端的 Rust 参数如何被压成一个 `Vec<*mut c_void>`,以及为什么一个 `&[T]` 切片会变成两个驱动层参数。
- 跟踪从 `module.vecadd(...)` 到 CUDA 驱动 `cuLaunchKernel` 的完整调用链,理解为什么启动是「异步入队」而非「同步执行」。

## 2. 前置知识

本讲建立在 u2-l1（宏展开与命名契约）之上。开始前,请确保你理解以下几个概念:

- **kernel（内核）**:一段运行在 GPU 上的函数。本讲只关心「宿主如何把它启动起来」,不关心内核体内部。
- **PTX 入口符号**:内核在编译后的 PTX 里的函数名。`#[cuda_module]` 生成的启动方法内部会用这个名字去驱动里查函数句柄(`CudaFunction`)。
- **`CudaFunction`**:cuda-core 对驱动 `CUfunction` 的安全封装,代表「一个已加载、可启动的内核入口」。它是启动调用的第一个参数。
- **`CudaStream`**:CUDA 流,内核启动的「队列」。启动只是把内核排进流里,真正执行由 GPU 异步完成。
- **宿主（host）/ 设备（device）**:本讲全程在宿主侧(Rust 主程序,CPU)看问题,设备侧(GPU)只在「线程索引如何对应元素」时短暂涉及。

如果你还没读过 vecadd 示例,建议先看一遍 [u1-l4 Hello GPU](u1-l4-hello-gpu-vecadd.md) 建立体感,因为本讲的所有实践都基于 vecadd。

一个贯穿全讲的核心事实:**CUDA 内核的启动由两个正交的维度描述——「启动多少个线程」(`LaunchConfig`) 和「给这些线程喂什么参数」(marshalling)。** 本讲的四个最小模块正好对应这两个维度。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用:

| 文件 | 作用 |
|------|------|
| `crates/cuda-core/src/launch.rs` | 定义 `LaunchConfig` 结构与 `for_num_elems` 便捷构造器。**只有 46 行**,是本讲最核心、也最短的一个文件。 |
| `crates/cuda-core/src/lib.rs` | 提供 `launch_kernel` / `launch_kernel_on_stream` 等 `cuLaunchKernel` 家族的安全封装,是启动链的「最后一公里」。 |
| `crates/cuda-host/src/launch.rs` | 参数 marshalling 工具函数(`push_kernel_scalar`、`push_kernel_device_slice` 等),以及 `CudaKernel` / `KernelScalar` trait。 |
| `crates/cuda-macros/src/lib.rs` | `#[cuda_module]` 在此生成 `module.<kernel>(...)` 方法体——把 config、stream、参数拼装起来调用 `launch_kernel_on_stream`。 |
| `crates/rustc-codegen-cuda/examples/vecadd/src/main.rs` | 本讲所有实践的素材:一个最小的内核 + 宿主启动。 |

一个观察:启动相关的代码被刻意分到了三个 crate——`cuda-core`(配置 + 驱动调用)、`cuda-host`(参数压栈)、`cuda-macros`(把二者粘合成类型安全的启动方法)。这种分层正是 u1-l2 提到的「用户面分层」在启动路径上的体现。

## 4. 核心概念与源码讲解

### 4.1 LaunchConfig 结构

#### 4.1.1 概念说明

启动一个 CUDA 内核,驱动需要知道三件事:

1. **启动多少个线程块(block)?** —— grid 维度。
2. **每个线程块里有多少个线程?** —— block 维度。
3. **每个块要预留多少动态共享内存?** —— 这是 u2-l3 讲过的共享内存在启动时才确定的「动态」部分。

CUDA 的执行模型是:一次启动会创建 `grid_dim.x × grid_dim.y × grid_dim.z` 个**线程块**,每个块内部有 `block_dim.x × block_dim.y × block_dim.z` 个**线程**。`LaunchConfig` 就是把这三组信息打包成一个值。

#### 4.1.2 核心流程

线程总数与维度的关系:

\[
\text{总线程数}(x) = \text{grid\_dim}.x \times \text{block\_dim}.x
\]

对于一维逐元素内核(如 vecadd),我们希望「每个线程处理一个元素」,于是希望总线程数 ≥ 元素数 `n`。在 block 固定为 `B` 时,所需 block 数为:

\[
\text{grid\_x} = \left\lceil \frac{n}{B} \right\rceil
\]

这就是 `for_num_elems` 要做的事(见 4.2)。而 `LaunchConfig` 本身只是这三个字段的**纯数据容器**——它不做任何计算,也不调用任何驱动 API。

#### 4.1.3 源码精读

整个 `LaunchConfig` 定义极其简洁:

[crates/cuda-core/src/launch.rs:18-26](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/launch.rs#L18-L26) —— `LaunchConfig` 把 grid/block/shared 三个维度打包成一个 `Copy` 结构体。

关键点:

- `grid_dim` 与 `block_dim` 都是 `(u32, u32, u32)`,即 `(x, y, z)` 三元组,直接对应 `cuLaunchKernel` 的六个维度参数。
- `shared_mem_bytes` 是**每块**的字节数(不是总量)。静态共享内存(`SharedArray<T,N>`,见 u2-l3)不在这里体现——它在编译期就已分配;这里只管**动态**共享内存(`DynamicSharedArray<T>`)。
- 派生了 `Clone, Copy, Debug`:它是个轻量值,可以随意按值传递。

文件顶部的模块文档说得很直白——「`LaunchConfig` bundles the grid dimensions, block dimensions, and dynamic shared memory size」:

[crates/cuda-core/src/launch.rs:6-10](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/launch.rs#L6-L10) —— 模块文档说明 `LaunchConfig` 的用途与 `for_num_elems` 的定位。

#### 4.1.4 代码实践

**实践目标**:亲手感受 `LaunchConfig` 是纯数据,不触发任何 GPU 操作。

1. 打开 vecadd 示例 [crates/rustc-codegen-cuda/examples/vecadd/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs)。
2. 在 `main` 里、调用 `module.vecadd(...)` 之前,加一行构造并打印配置(示例代码):

   ```rust
   // 示例代码:仅用于观察 LaunchConfig 的字段
   let cfg = LaunchConfig::for_num_elems(N as u32);
   println!("{cfg:?}");
   ```

3. 观察:`{:?}` 会打印出 `grid_dim`、`block_dim`、`shared_mem_bytes` 三个字段的值。
4. 预期结果:对 `N = 1024`,应看到 `grid_dim = (4, 1, 1)`、`block_dim = (256, 1, 1)`、`shared_mem_bytes = 0`。
5. 待本地验证:若无 GPU,可用 `cargo oxide build vecadd` 仅确认编译通过,逻辑推断同上。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `LaunchConfig` 要派生 `Copy`?如果它不 `Copy`,本讲后面的启动代码哪里会出问题?

> **答案**:启动方法签名是 `fn vecadd(&self, stream: &CudaStream, config: LaunchConfig, ...)`——`config` 是按值传入的。宏生成的启动方法内部还会读取 `config.grid_dim` 等字段(见 4.4)。若 `LaunchConfig` 不是 `Copy`,每次读取字段后原值会被移动,且无法简单地在调用处传字面量。`Copy` 让这个纯数据结构可以廉价、无脑地复制使用。

**练习 2**:`shared_mem_bytes` 是「整个 grid 的总量」还是「每个 block 的量」?如果你的内核用 `DynamicSharedArray<f32, 4>` 且希望每块有 1024 个 `f32`,这里该填多少?

> **答案**:是**每块**的字节数。1024 个 `f32` = `1024 × 4 = 4096` 字节,故填 `4096`。

### 4.2 for_num_elems 推导

#### 4.2.1 概念说明

大多数一维逐元素内核(向量加、向量数乘、ReLU 等)都遵循同一个模式:「每个线程处理一个元素,线程号就是元素下标」。为这种最常见情况手写 `grid_dim`/`block_dim` 很啰嗦,于是 cuda-core 提供了便捷构造器 `for_num_elems(n)`:**给它元素数,它替你算出 grid/block。**

#### 4.2.2 核心流程

`for_num_elems` 的推导只有两步:

1. 固定每块线程数 `DEFAULT_BLOCK_SIZE = 256`(一个经验性的好值,既不太小影响占用率,又不太大撞上限)。
2. 用向上取整除法算出所需块数:

\[
\text{grid\_x} = \left\lceil \frac{n}{256} \right\rceil = n \,\text{div\_ceil}\, 256
\]

`div_ceil` 是 Rust 整数的「向上取整除法」(稳定于较新版 std),等价于 `(n + 255) / 256`。当 `n` 不是 256 的倍数时,最后一个块里会有一部分线程「没有元素可处理」——这正是 u2-l2 里 `c.get_mut(idx)` 返回 `Option`、vecadd 内核靠 `if let Some(...)` 跳过越界线程的原因。

总线程数 `grid_x × 256 ≥ n`,于是每个元素至少有一个线程覆盖。

#### 4.2.3 源码精读

[crates/cuda-core/src/launch.rs:28-45](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/launch.rs#L28-L45) —— `for_num_elems` 用固定块大小 256 + 向上取整推导 grid。

要点:

- `DEFAULT_BLOCK_SIZE` 是函数内的 `const`,值为 256。这是 cuda-oxide 为「简单逐元素内核」选的默认值,**不是** CUDA 的硬性规定。
- 三维里 y、z 都设为 1,即纯粹的一维启动。
- `shared_mem_bytes: 0`——不请求动态共享内存(动态共享内存留给 `DynamicSharedArray`,见 u2-l3)。

#### 4.2.4 代码实践

**实践目标**:验证 `for_num_elems` 的换算,并理解 grid/block 如何映射到元素。

1. 仍用 vecadd 示例,把 `N` 改成几个不同的值,重跑 4.1.4 里的 `println!("{cfg:?}")`。
2. 需要观察并填表:

   | N | grid_dim.0 | block_dim.0 | 总线程数 | 是否 ≥ N |
   |---|------------|-------------|----------|----------|
   | 1024 | 4 | 256 | 1024 | 是 |
   | 1000 | ? | 256 | ? | 是 |
   | 1 | ? | 256 | ? | 是 |
   | 100000 | ? | 256 | ? | 是 |

3. 预期结果:`N=1000` → `grid_dim.0 = 4`(⌈1000/256⌉ = 4,即 4×256 = 1024 ≥ 1000);`N=1` → `grid_dim.0 = 1`;`N=100000` → `grid_dim.0 = 391`(⌈100000/256⌉ = 391)。
4. 待本地验证:若无 GPU,可用 `cargo oxide build vecadd` 确认编译;数值可由 `div_ceil` 公式手算验证。

#### 4.2.5 小练习与答案

**练习 1**:`for_num_elems(0)` 会返回什么?这种「没有元素」的情况内核会怎样?

> **答案**:`grid_x = 0u32.div_ceil(256) = 0`,所以返回 `grid_dim = (0,1,1)`、`block_dim = (256,1,1)`、`shared_mem_bytes = 0`。`div_ceil` 是「向上取整除法」,但 `0/256` 的商本就是 0,向上取整仍是 0。grid 维度为 0 是一次「退化启动」——没有 block 被创建、没有任何线程运行,这正是「没有元素要处理」时逐元素内核该有的行为。**待本地验证**:驱动对 grid 维度为 0 的 `cuLaunchKernel` 的具体表现(通常是无操作或返回成功但不执行任何线程),以本地实测为准。

**练习 2**:为什么默认块大小选 256 而不是 1024(很多 GPU 的块线程上限)?

> **答案**:256 是占用率(occupancy)与每块资源占用之间的常见折中。块越大,单块占用的寄存器/共享内存越多,SM(流多处理器)上能并存的块数就越少;块太小又会增加调度开销。256 是许多元素级内核的经验甜点。需要更高占用率或合作规约时,你会手动构造别的 `LaunchConfig`(见 4.1 与综合实践)。

### 4.3 参数 marshalling

#### 4.3.1 概念说明

内核启动的第二大主题是「把宿主端的参数喂给 GPU」。CUDA 驱动的 `cuLaunchKernel` 要求参数以一个特定格式提交:**一个指针数组** `kernelParams`,数组里每个元素都是「指向某个参数值的指针」。

问题在于:Rust 侧的参数类型丰富多彩(`f32`、`&[f32]`、`&mut DeviceBuffer<T>`、闭包……),而驱动只认 `*mut c_void`。**marshalling(参数编组)** 就是把 Rust 参数逐个转换成驱动要求的指针条目、塞进一个 `Vec<*mut c_void>` 的过程。

这里有一个 cuda-oxide 的关键设计决策:**设备切片在 PTX 层会被拆成两个独立参数——指针 + 长度。** 宿主侧 marshalling 必须和这个拆分严格对齐,否则驱动拿到的参数下标会错位,内核读到垃圾数据。

#### 4.3.2 核心流程

`#[cuda_module]` 为每个 `#[kernel]` 生成的启动方法体(伪代码):

```
fn vecadd(&self, stream, config, a, b, mut c) -> Result<(), DriverError> {
    let function = &self.vecadd;          // 拿到 CudaFunction 句柄(4.4)
    let mut args: Vec<*mut c_void> = Vec::new();
    // —— 以下逐参数 marshalling ——
    // a: &[f32]   → (ptr, len) → push 两条
    // b: &[f32]   → (ptr, len) → push 两条
    // c: DisjointSlice<f32> (可写) → (ptr, len) → push 两条
    let (mut a_ptr, mut a_len) = read_only_device_buffer_arg(a);
    push_kernel_device_slice(&mut args, &mut a_ptr, &mut a_len);
    ... // b、c 同理
    // —— 调用驱动(4.4)——
    unsafe { launch_kernel_on_stream(function, config.grid_dim, config.block_dim,
                                     config.shared_mem_bytes, stream, &mut args) }
}
```

两类参数的 marshalling 规则:

| Rust 参数类型 | marshalling 方式 | 对应几个驱动参数 |
|---------------|------------------|------------------|
| 标量(`T: Copy`,含闭包) | `push_kernel_scalar`:压入 `&mut T` 的地址 | 1(若 `T` 非零大小) |
| 标量但 `T` 是 ZST(零大小,如无捕获闭包) | `push_kernel_scalar`:**跳过,不压** | 0 |
| `&[T]` / `&DeviceBuffer<T>`(只读切片) | `read_only_device_buffer_arg` → `(CUdeviceptr, u64)`,再 `push_kernel_device_slice` | 2(指针 + 长度) |
| `&mut [T]` / `DisjointSlice<T>`(可写切片) | `writable_device_buffer_arg` → `(CUdeviceptr, u64)`,再 `push_kernel_device_slice` | 2(指针 + 长度) |

#### 4.3.3 源码精读

**标量压栈**——`push_kernel_scalar`:

[crates/cuda-host/src/launch.rs:137-144](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-host/src/launch.rs#L137-L144) —— 把标量参数的地址压进 `args`;ZST 直接跳过。

注意 ZST 跳过逻辑(`size_of::<T>() == 0` 时 `return`):无捕获闭包、单元结构体这类零大小值,在设备侧 PTX 里也会被去掉对应的 `.param` 声明,所以宿主侧必须同步跳过,否则 `kernelParams[]` 下标会和内核声明错位。文档对此有详细说明:

[crates/cuda-host/src/launch.rs:122-136](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-host/src/launch.rs#L122-L136) —— 解释 ZST 跳过的正确性原因:设备端 PTX 同样会删除对应的 `.param`。

**切片拆 (ptr, len)**——先取对,再压两条:

[crates/cuda-host/src/launch.rs:146-164](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-host/src/launch.rs#L146-L164) —— `read_only_device_buffer_arg` / `writable_device_buffer_arg` 把切片编码成 `(CUdeviceptr, 元素数)`。

[crates/cuda-host/src/launch.rs:166-181](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-host/src/launch.rs#L166-L181) —— `push_kernel_device_slice` 把指针和长度各压一条进 `args`。

这套「切片 = 指针 + 长度」的 ABI 在文档里被点名为刻意的稳定约定:

[crates/cuda-host/src/launch.rs:166-172](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-host/src/launch.rs#L166-L172) 注释——cuda-oxide 的设备切片在 PTX 层就是两个参数,这里保持一致以对齐下标。

**宏侧的 marshalling 分派**——`#[cuda_module]` 按参数种类选择上面哪个 helper:

[crates/cuda-macros/src/lib.rs:1360-1389](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L1360-L1389) —— 三种参数分别走 `Scalar` / `ReadOnlyDeviceBuffer` / `WritableDeviceBuffer` 三条分支,调对应 helper。

参数种类本身是个三态枚举:

[crates/cuda-macros/src/lib.rs:332-336](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L332-L336) —— `CudaModuleParamMarshal` 把内核参数分成标量/只读切片/可写切片三类。

#### 4.3.4 代码实践

**实践目标**:用单元测试直观看到 marshalling 把参数压成了什么。

1. 阅读 `cuda-host` 里针对 marshalling 的单元测试:

   [crates/cuda-host/src/launch.rs:488-541](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-host/src/launch.rs#L488-L541) —— 这些测试断言了 `push_kernel_scalar` 压入的是「值的地址」,`push_kernel_device_slice` 压入「指针 + 长度」两条。

2. 重点看 `test_push_kernel_device_slice_records_pointer_and_len`:它构造 `ptr = 0xfeed_beef`、`len = 1024`,调用后断言 `args.len() == 2`,且 `args[0]` 解引用等于 ptr、`args[1]` 解引用等于 len。
3. 需要观察的现象:一个切片参数 → `args` 多了 **2** 条;一个标量参数 → 多 **1** 条。
4. 预期结果:断言全部通过(这是项目自带的测试)。
5. 若想本地跑:`待本地验证`——需要在能编译 `cuda-host` 的环境执行 `cargo test -p cuda-host`(注意 cuda-host 可能依赖工具链,见 u1-l3)。

#### 4.3.5 小练习与答案

**练习 1**:vecadd 的签名是 `fn vecadd(a: &[f32], b: &[f32], mut c: DisjointSlice<f32>)`。启动时 `args` 这个 `Vec` 里最终有几条 `*mut c_void`?为什么?

> **答案**:**6** 条。`a`、`b`、`c` 都是切片(只读两个 + 可写一个),每个切片拆成 (指针, 长度) 两条,共 `3 × 2 = 6`。这也意味着 PTX 里 `vecadd` 的 `.param` 声明有 6 个参数。

**练习 2**:如果把内核改成 `fn scale(factor: f32, input: &[f32], mut out: DisjointSlice<f32>)`,`args` 有几条?

> **答案**:**5** 条。`factor` 是标量 → 1 条;`input` → 2 条;`out` → 2 条;合计 5。

### 4.4 cuLaunchKernel 调用

#### 4.4.1 概念说明

marshalling 准备好 `args`,config 准备好维度,最后一步就是真正调用驱动。本模块跟踪从 `module.vecadd(...)` 到 `cuLaunchKernel` 的完整链路,并强调两个容易踩坑的事实:

- **启动是「异步入队」**:`cuLaunchKernel` 把内核排进 stream 后**立即返回**,内核很可能还没开始执行。要等结果必须同步 stream(如 vecadd 里的 `to_host_vec` 内部会做同步)。
- **启动前必须绑定 context**:CUDA 驱动要求调用线程已绑定正确的 context,否则启动失败。cuda-oxide 的 `_on_stream` 封装替你做了这件事。

#### 4.4.2 核心流程

完整调用链(vecadd 场景):

```
module.vecadd(&stream, cfg, &a, &b, &mut c)        // 用户调用(宏生成)
  └─ 取 self.vecadd 这个 CudaFunction              // cuda_module_function_binding
  └─ 建 args: Vec<*mut c_void>,逐参数压栈          // 4.3
  └─ unsafe { launch_kernel_on_stream(func, grid, block, shared, stream, &mut args) }
       └─ stream.context().bind_to_thread()?       // 先绑定 context!
       └─ launch_kernel(func.cu_function(), grid, block, shared, stream.cu_stream(), args)
            └─ cuda_bindings::cuLaunchKernel(...)  // 真正的驱动调用
            └─ .result()?                          // 把 CUDA 错误码转成 DriverError
```

三个层次各有分工:宏**拼装**、`launch_kernel_on_stream`**绑定 context**、`launch_kernel`**翻译成驱动调用**。

#### 4.4.3 源码精读

**第一层:宏生成的启动方法体**——把 function、config、stream、args 拼起来:

[crates/cuda-macros/src/lib.rs:1108-1125](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L1108-L1125) —— 生成的 `vecadd` 方法:取 function、建 `args`、跑 marshalling、最后调 `launch_call`。

非泛型内核(如 vecadd)的 `function` 就是 `LoadedModule` 上对应的 `CudaFunction` 字段:

[crates/cuda-macros/src/lib.rs:1470-1475](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L1470-L1475) —— 非泛型内核直接用 `&self.<字段>` 拿到函数句柄。

而普通的(无 cluster、无 cooperative)启动,`launch_call` 选中的就是 `launch_kernel_on_stream`:

[crates/cuda-macros/src/lib.rs:1523-1534](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L1523-L1534) —— `(None, false)` 分支:无 cluster、非 cooperative → 走 `launch_kernel_on_stream`。

(注:`match (cluster_dim, kernel.cooperative)` 的另外三个分支分别走 `launch_kernel_ex_cooperative_on_stream`、`launch_kernel_ex_on_stream`、`launch_kernel_cooperative_on_stream`,对应 u5 会讲的集群与协作启动。)

**第二层:`launch_kernel_on_stream` 绑定 context**:

[crates/cuda-core/src/lib.rs:188-208](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/lib.rs#L188-L208) —— 先 `stream.context().bind_to_thread()`,再转发到裸的 `launch_kernel`。

关键一行是 `stream.context().bind_to_thread()?`(lib.rs:197)——它把 stream 所属的 CUDA context 设为当前线程的 current context,免去调用方每次手写 `cuCtxSetCurrent`。文档明确说这是「正常宿主代码的首选入口」:

[crates/cuda-core/src/lib.rs:163-172](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/lib.rs#L163-L172) —— 推荐用带类型的 `launch_kernel_on_stream`,它自动绑 context。

**第三层:`launch_kernel` 翻译成驱动调用**:

[crates/cuda-core/src/lib.rs:137-161](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/lib.rs#L137-L161) —— 把六个维度 + shared + stream + 参数数组展开成 `cuLaunchKernel` 的固定参数列表。

注意两个细节:

- `kernel_params.as_mut_ptr()`(lib.rs:156):驱动要的是「指向参数指针数组的指针」,正好是 `&mut [*mut c_void]` 的裸指针。
- `std::ptr::null_mut()`(lib.rs:157):这是 `cuLaunchKernel` 的 `extra` 参数,设为 null 表示「不用高级启动模式」(如通过 buffer 传参)。

文档强调启动的异步语义:

[crates/cuda-core/src/lib.rs:99-113](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/lib.rs#L99-L113) —— 「The launch is **asynchronous** with respect to the host」——函数返回时内核只是入队,需用 stream/event 同步来等待完成。

最后,vecadd 的实际启动点把这些都串起来:

[crates/rustc-codegen-cuda/examples/vecadd/src/main.rs:75-84](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L75-L84) —— 用户侧只写一句 `module.vecadd(&stream, LaunchConfig::for_num_elems(N as u32), &a_dev, &b_dev, &mut c_dev)`,背后就是上面整条链。

#### 4.4.4 代码实践

**实践目标**:体会「启动是异步入队」,理解为什么必须在拿结果前同步。

1. 看 vecadd 的 `main`:启动内核(75-84 行)之后,紧接着是 `let c_host = c_dev.to_host_vec(&stream).unwrap();`(87 行)。
2. 操作:在 `module.vecadd(...)` 之后、`to_host_vec` 之前,临时插入一行 `println!("已调用 vecadd,但 GPU 可能还没算完");`。
3. 需要观察的现象:这行 println 几乎一定会在内核真正执行完**之前**打印——因为 `vecadd` 调用只是入队。
4. 预期结果:程序仍输出正确结果,因为 `to_host_vec` 内部会同步 stream(等内核与拷贝都完成)才返回。如果你把 `to_host_vec` 那行删掉直接读 `c_dev`,会读到未初始化/旧数据(本讲不建议真删,理解即可)。
5. 待本地验证:需要可运行的 CUDA GPU;若没有,「源码阅读型实践」——阅读 `launch_kernel` 的文档注释([lib.rs:99-113](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/lib.rs#L99-L113))复述它「异步」的三层含义(对宿主异步、需同步等待、stream/event 是同步手段)。

#### 4.4.5 小练习与答案

**练习 1**:`launch_kernel` 是 `unsafe fn`,但宏生成的 `module.vecadd(...)` 在用户侧调用时却**不需要**写 `unsafe`。这是为什么?

> **答案**:因为宏生成的 `vecadd` 方法体内部已经把 `launch_kernel_on_stream(...)` 包在了一个 `unsafe { ... }` 块里(见 [lib.rs:1524-1533](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L1524-L1533))。`#[cuda_module]` 通过严格的类型化参数(把 `&[T]` 映射成 `DeviceBuffer`、标量约束到 `KernelScalar` 等)来保证 unsafe 的前置条件(参数大小/对齐匹配)成立,从而把 unsafe 边界封在方法内部,给用户一个安全接口。这正是 u2-l1 讲过的「宏与后端的类型安全契约」在启动路径上的兑现。

**练习 2**:`launch_kernel_on_stream` 比裸 `launch_kernel` 多做了哪一步?为什么这步不能省?

> **答案**:多了 `stream.context().bind_to_thread()?`(见 [lib.rs:197](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/lib.rs#L197))。CUDA 驱动要求「启动内核的线程必须已绑定 owning context」,否则 `cuLaunchKernel` 会失败。裸 `launch_kernel` 故意不做这步(留给高级用户/已有 context 的场景),所以文档反复推荐普通代码用 `_on_stream` 版本,避免每次手写 `cuCtxSetCurrent`。

## 5. 综合实践

把本讲的 grid/block/marshalling/cuLaunchKernel 四块串起来,做一个对照实验:**手动构造 `LaunchConfig` 启动 vecadd,再和 `for_num_elems` 的自动配置对比,验证两者结果一致但线程映射不同。**

**步骤:**

1. 复制 vecadd 示例为一个新的 standalone crate(参考 u1-l5 的示例组织方式,或直接在 vecadd 里临时改)。
2. 把 `N` 设为一个非 256 倍数的值,例如 `const N: usize = 1000;`。
3. 用两种 config 各启动一次内核(写到不同的输出缓冲,避免互相干扰),示例代码:

   ```rust
   // 示例代码:对比两种 LaunchConfig
   let cfg_auto = LaunchConfig::for_num_elems(N as u32);

   // 手动构造:故意用 block = 128,于是 grid = ⌈1000/128⌉ = 8
   let cfg_manual = LaunchConfig {
       grid_dim: ((N as u32).div_ceil(128), 1, 1),
       block_dim: (128, 1, 1),
       shared_mem_bytes: 0,
   };

   println!("auto  = {cfg_auto:?}");
   println!("manual= {cfg_manual:?}");

   // 分别启动到 c_auto / c_manual,再各自 to_host_vec 比对
   ```

4. 需要观察并解释:
   - `cfg_auto` 的 `block_dim.0 = 256`、`grid_dim.0 = 4`;`cfg_manual` 的 `block_dim.0 = 128`、`grid_dim.0 = 8`。**总线程数都 ≥ 1000**,所以都能覆盖所有元素。
   - 两个输出向量应当**完全一致**(都在容差内等于 `a[i]+b[i]`)。这说明:**对逐元素内核,block 大小的选择不影响正确性,只影响性能/占用率**——因为 u2-l2 的 `thread::index_1d()` 用的是「全局线程号」公式 `blockIdx.x * blockDim.x + threadIdx.x`,自动适配任意 block 大小。
5. 预期结果:两个缓冲的逐元素校验都报 `✓ SUCCESS`,且彼此相等。
6. 进阶思考:把 `cfg_manual` 的 `grid_dim.0` 故意改小(比如改成 3,使总线程 `3×128 = 384 < 1000`),观察哪些元素没被计算——这会直观展示「总线程数 < 元素数」时尾部的元素被漏掉。
7. 待本地验证:本实践需要可运行的 CUDA GPU;若无 GPU,可降级为「源码阅读型实践」:在纸上对 `N=1000`、`block=128` 算出 `grid=8`、总线程 `1024`,并指出第 `1000..1024` 号线程(共 24 个)会因为 `c.get_mut(idx)` 返回 `None` 而空转。

## 6. 本讲小结

- `LaunchConfig` 是一个 `Copy` 的纯数据结构,只有三个字段:`grid_dim`、`block_dim`、`shared_mem_bytes`(每块动态共享内存)。
- `for_num_elems(n)` 是一维逐元素内核的便捷构造器:固定 block=256,`grid_x = n.div_ceil(256)`,shared=0。
- 参数 marshalling 把 Rust 参数压成驱动要的 `Vec<*mut c_void>`:标量压 1 条(ZST 跳过),切片拆成 (指针, 长度) 压 2 条——宿主侧的拆分必须与 PTX 的 `.param` 声明严格对齐。
- 启动链是三层:`#[cuda_module]` 生成的方法拼装 → `launch_kernel_on_stream` 绑定 context → `launch_kernel` 调 `cuLaunchKernel`。
- 启动是**异步入队**:`cuLaunchKernel` 立即返回,内核排队执行;取结果前必须同步 stream/event。
- 用户侧的 `module.vecadd(...)` 不需要 `unsafe`,因为宏在方法体内把 unsafe 调用包了起来,用类型化参数保证了驱动的前置条件。

## 7. 下一步学习建议

本讲讲透了「同步启动」全链路。接下来可以沿三条线深入:

1. **设备内存与数据搬运(u2-l5)**:本讲反复出现的 `DeviceBuffer::from_host` / `to_host_vec` 是怎么实现的?它和本讲的启动链如何配合完成一次完整计算?这是自然的下一讲。
2. **异步执行模型(u3-l3)**:本讲的 `module.vecadd(...)` 是同步启动。如果你想在一条流上串起多个内核、或与 async Rust 集成,就需要 `module.vecadd_async(...)` 与 `DeviceOperation` 的惰性模型——那是 cuda-async 的事。
3. **集群与协作启动(u5)**:本讲只用了最普通的 `launch_kernel_on_stream`。`match (cluster_dim, cooperative)` 的另外三个分支(`launch_kernel_ex_on_stream`、`launch_kernel_cooperative_on_stream` 等)对应 Hopper 集群与网格级同步,留到专家层讲。

建议先读 u2-l5 把「内存搬运 + 启动」的闭环走完,再回头看本讲的启动链会觉得非常自然。
