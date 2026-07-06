# 内核启动框架：LaunchArgs 与设备能力探测

## 1. 本讲目标

本讲是 JIT 单元（U4）的收尾。在前三讲里，我们已经打通了「生成 `.cu` 源码 → 编译缓存 → 加载 cubin」这条编译链，得到了一个可用的 `KernelRuntime`（内含一个 `CUfunction` 句柄）。**但拿到内核句柄并不等于把它跑起来**——CUDA 内核启动还需要回答一连串问题：开多少个 block？每个 block 多少线程？用多少共享内存？是否走 cluster？是否协作启动？是否启用 PDL？这些「启动配置」如果每个内核各写一遍，既冗余又容易出错。

本讲的目标是讲清楚 DeepEP 是如何用一个**统一的启动框架**把这些问题一次性解决：

1. 掌握 `LaunchArgs` 六个字段（`grid_dim` / `num_threads` / `smem_size` / `cluster_dim` / `cooperative` / `pdl_enabled`）各自的含义与对 CUDA 启动的影响。
2. 理解 `LaunchRuntime::launch` 如何把这些字段翻译成一张 `CUlaunchConfig` 并真正下发内核。
3. 理解 `DeviceRuntime` 如何一次性探测并缓存 `cudaDeviceProp`（SM 数、共享内存上限、时钟频率、架构），以及时钟频率如何被换算成内核的「超时周期数」。
4. 了解 `IncludeParser` 如何递归计算头文件哈希，让缓存签名对头文件改动保持敏感。

学完后，你应当能读懂任意一个 `launch_xxx` 函数末尾那行 `LaunchArgs(...)` 的构造，并解释它为什么这么写。

## 2. 前置知识

在进入源码前，先用通俗语言铺垫几个 CUDA 启动相关的概念。本讲假设你已读过 u4-l1 ~ u4-l3，知道 `Compiler::build` 会返回一个持有 `CUfunction` 的 `KernelRuntime`。

- **grid / block**：CUDA 的两层并行。grid 由若干 block 组成，每个 block 又含若干 thread。DeepEP 的通信内核几乎都用一维 grid（`grid_dim = {num_sms, 1}`），即「一个 SM 跑一个 block」。
- **动态共享内存（dynamic shared memory）**：内核在 `__launch_bounds__` 之外，还能在启动时申请一块「按需大小」的共享内存。Hopper 上这块内存可以开到接近 228 KB/SM，但必须先用 `cuFuncSetAttribute(... MAX_DYNAMIC_SHARED_SIZE_BYTES, size)`「解锁」上限，否则默认只有很小的额度。
- **线程束（warp）**：GPU 最小执行单元，32 个线程。DeepEP 常用「N 个 warp」来描述 block 大小，`num_threads = num_warps * 32`。
- **Cluster（线程块集群）**：Hopper 引入的概念，把若干 block 编成一组，可共享分布式共享内存（DSMEM）并做 block 间同步。`cluster_dim` 表示一个 cluster 含几个 block。
- **协作启动（cooperative launch）**：一种特殊启动方式，保证 grid 中**所有 block 同时驻留在 SM 上**，从而允许内核内部做 grid 级同步（`cg::this_grid().sync()`）。代价是 grid 规模受限于设备可同时驻留的 block 数。
- **PDL（Programmatic Dependent Launch）**：Hopper 的流式重叠特性，让相邻内核的 prologue/epilogue 与上一个内核的计算重叠。在 CUDA Driver API 里对应 `CU_LAUNCH_ATTRIBUTE_PROGRAMMATIC_STREAM_SERIALIZATION`。
- **超时周期（timeout cycles）**：DeepEP 的通信内核是「发—收」对，接收方会**自旋等待**对端的数据/信号。为防止对端宕机导致 GPU 永久自旋（最终触发 XID 掉卡），内核内部带一个用 GPU 时钟周期数衡量的超时上限。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `csrc/jit/` 下，外加一个真实用到该框架的启动器：

| 文件 | 作用 |
| --- | --- |
| [csrc/jit/launch_runtime.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/launch_runtime.hpp) | 定义 `LaunchArgs` 结构体与 CRTP 基类 `LaunchRuntime<Derived>`，提供 `generate()` 与 `launch()` 两个模板方法。 |
| [csrc/jit/handle.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/handle.hpp) | 把 `LaunchArgs` 翻译成 `CUlaunchConfig` 的 `construct_launch_config`，以及真正下发的 `launch_kernel`。 |
| [csrc/jit/device_runtime.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/device_runtime.hpp) | `DeviceRuntime`：惰性探测并缓存 `cudaDeviceProp`（SM 数、共享内存、时钟频率、架构）。 |
| [csrc/jit/include_parser.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/include_parser.hpp) | `IncludeParser`：递归解析 `<deep_ep/*>` 头文件并计算哈希，供缓存签名使用。 |
| [csrc/utils/lazy_init.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/utils/lazy_init.hpp) | `LazyInit<T>`：首访问才构造的单例包装，`device_runtime` 全局实例由它持有。 |
| [csrc/kernels/elastic/dispatch.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp) | `launch_dispatch`：dispatch 内核的启动器，展示了「真正构造一个 `LaunchArgs`」的完整范例。 |
| [csrc/elastic/buffer.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp) | `ElasticBuffer`：调用 `device_runtime` 探测 SM 数/共享内存，并把超时秒数换算成周期数。 |

调用方向（自下而上）：

```
buffer.hpp ──(取 num_sms / num_smem_bytes / 算 timeout cycles)──> launch_dispatch(dispatch.hpp)
                                                                       │
                                                            构造 DispatchRuntime::Args
                                                            (含一个 LaunchArgs 字段)
                                                                       │
                                           generate() ──> compiler->build() ──> KernelRuntime
                                                                       │
                                              LaunchRuntime::launch(runtime, args, stream)
                                                  │                          (launch_runtime.hpp)
                              construct_launch_config(...)  ──>  launch_kernel(...)
                                                (handle.hpp，把 LaunchArgs 翻成 CUlaunchConfig)
```

## 4. 核心概念与源码讲解

### 4.1 LaunchRuntime：CRTP 两段式的「启动」这一半

#### 4.1.1 概念说明

在 u4-l2 里我们讲过，DeepEP 用 CRTP 基类 `LaunchRuntime<Derived>` 把每个内核的生命周期切成两段：**generate（生成源码）** 和 **launch（启动内核）**。基类负责「与内核无关」的公共流程（计算头文件哈希、构造启动配置、下发），派生类（如 `DispatchRuntime`）只需实现两个钩子：

- `generate_impl(args)`：把模板参数填进 `.cu`（u4-l2 已讲）。
- `launch_impl(kernel, config, args)`：把运行时指针参数按顺序传给 `launch_kernel`（u4-l3 已讲）。

本讲聚焦另一半——基类提供的 `launch()` 方法：它接收 `KernelRuntime` 与 `args`，从 `args.launch_args` 取出启动描述符，构造好 `CUlaunchConfig` 后回调 `Derived::launch_impl`。这样一来，「grid/block/smem/cluster/cooperative/pdl 怎么翻译成 CUDA 调用」这份知识只写在基类一份，所有内核共享。

#### 4.1.2 核心流程

`LaunchRuntime::launch` 的执行步骤：

1. 从 `kernel_runtime` 取出真正的内核句柄 `kernel`（一个 `CUfunction`）。
2. 决定流：优先用调用方传入的 `stream_opt`，否则取当前 PyTorch 流。
3. 从 `args.launch_args` 读出 `LaunchArgs`，把 grid/block 包装成 `dim3`。
4. 调 `construct_launch_config(...)` 把 `LaunchArgs` 翻译成 `CUlaunchConfig`（详见 4.2）。
5. 若 `EP_JIT_DEBUG` 开启，打印启动参数摘要。
6. 回调 `Derived::launch_impl(kernel, config, args)`，由派生类把业务参数挨个传给 `launch_kernel`，真正下发。

注意第 3 步把 `launch_args.grid_dim`（一个 `std::pair<int,int>`）映射成 `dim3{first, second, 1}`：DeepEP 的内核只用一/二维 grid，第三维恒为 1。

#### 4.1.3 源码精读

先看 `launch()` 的全貌：

```cpp
template <typename Args>
static void launch(const std::shared_ptr<KernelRuntime>& kernel_runtime, const Args& args,
                   const std::optional<at::cuda::CUDAStream>& stream_opt = std::nullopt) {
    const auto kernel = kernel_runtime->kernel;
    const auto stream = stream_opt.value_or(at::cuda::getCurrentCUDAStream());
    const LaunchArgs& launch_args = args.launch_args;

    const dim3& grid_dim = {static_cast<unsigned>(launch_args.grid_dim.first),
                            static_cast<unsigned>(launch_args.grid_dim.second), 1};
    const dim3& block_dim = {static_cast<unsigned>(launch_args.num_threads), 1, 1};
    auto config = construct_launch_config(kernel, stream, launch_args.smem_size,
                                          grid_dim, block_dim, launch_args.cluster_dim,
                                          launch_args.cooperative, launch_args.pdl_enabled);
    ...
    Derived::launch_impl(kernel, config, args);
}
```

[csrc/jit/launch_runtime.hpp:48-71](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/launch_runtime.hpp#L48-L71) —— 基类把 `LaunchArgs` 翻成 `dim3` 与 `config`，再回调派生类 `launch_impl`。注意它对 `construct_launch_config` 传了**全部 8 个参数**（含 `cooperative` 与 `pdl_enabled`），这意味着默认构建走的是 handle.hpp 里的 Driver API 分支（见 4.2.3）。

`generate()` 也定义在这个基类里，它额外做了一件与启动无关、但与缓存强相关的事——计算并注入头文件哈希（详见 4.4）：

[csrc/jit/launch_runtime.hpp:32-46](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/launch_runtime.hpp#L32-L46) —— `generate` 调 `Derived::generate_impl` 生成代码后，用 `include_parser->get_hash_value(code)` 算头文件哈希，并把它作为首行注释拼回代码。

#### 4.1.4 代码实践

**实践目标**：验证「基类 `launch` + 派生类 `launch_impl」」的分工，体会 CRTP 如何让新增内核零样板。

**操作步骤**（源码阅读型实践）：

1. 打开 [csrc/kernels/elastic/dispatch.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp)，找到 `DispatchRuntime`（L14）与 `DispatchCopyEpilogueRuntime`（L232）两个派生类。
2. 分别确认它们都**只**实现了 `generate_impl` 与 `launch_impl` 两个静态方法，没有任何 `launch`/`generate` 的代码——这两者继承自基类。
3. 在 `launch_dispatch`（L141）与 `launch_dispatch_copy_epilogue`（L293）末尾，你会看到完全相同的「三连」：

```cpp
const auto code = DispatchRuntime::generate(args);
const auto runtime = jit::compiler->build("dispatch", code);
DispatchRuntime::launch(runtime, args, stream);
```

[csrc/kernels/elastic/dispatch.hpp:227-229](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L227-L229) —— generate → build → launch 三连。`launch` 即基类方法。

**需要观察的现象**：两个内核（dispatch 与 epilogue）共享同一套 `generate/build/launch` 节奏，差异仅被封装在各自的 `generate_impl`/`launch_impl` 与那个 `LaunchArgs(...)` 构造里。

**预期结果**：你能用一句话概括——「新增一个内核，只需写一个派生类 + 一个 `launch_xxx` 启动器，启动框架本身无需改动」。

#### 4.1.5 小练习与答案

**练习 1**：`LaunchRuntime::launch` 为什么是 `static` 模板方法，而不是虚函数？

**参考答案**：因为 CRTP 的核心目的就是**编译期多态、零虚函数开销**。`Derived` 作为模板参数在编译期就确定了，`Derived::launch_impl` 的调用会被静态决议、可内联；而通信内核对每一点开销都敏感。若改成虚函数，每次启动都要查 vtable。

**练习 2**：`launch` 的第三个参数 `stream_opt` 是 `std::optional`，这样的设计给了调用方什么能力？

**参考答案**：调用方可以**显式指定一条流**（例如通信流 `comm_stream`，见 u2-l4），也可以不传、由基类取 `at::cuda::getCurrentCUDAStream()`（即调用方当前所在流）。这让同一个内核既能跑在通信流上，也能跑在计算流上，由上层 EP 工作流决定。

---

### 4.2 LaunchArgs 与 construct_launch_config：六字段如何映射到 CUDA

#### 4.2.1 概念说明

`LaunchArgs` 是一个纯描述结构体——它**只描述**「我想怎么启动」，不关心内核叫什么、参数是什么。把这份描述翻译成 CUDA Driver API 的 `CUlaunchConfig` 是 `construct_launch_config` 的职责。把这两者分离的好处是：描述符可以塞进 `Args` 一起被缓存、被日志打印，而翻译逻辑只写一份。

#### 4.2.2 核心流程

`LaunchArgs` 的六个字段及其 CUDA 含义：

| 字段 | 类型 | CUDA 落点 | 作用 |
| --- | --- | --- | --- |
| `grid_dim` | `std::pair<int,int>` | `config.gridDimX/Y` | grid 维度，DeepEP 用 `{num_sms, 1}` |
| `num_threads` | `int` | `config.blockDimX` | 每 block 线程数（即 warp 数 × 32） |
| `smem_size` | `int` | `cuFuncSetAttribute` + `config.sharedMemBytes` | 动态共享内存字节数 |
| `cluster_dim` | `int` | `CU_LAUNCH_ATTRIBUTE_CLUSTER_DIMENSION` | 每 cluster 的 block 数（>1 才生效） |
| `cooperative` | `bool` | `CU_LAUNCH_ATTRIBUTE_COOPERATIVE` | 是否协作启动（grid 全部驻留） |
| `pdl_enabled` | `bool` | `CU_LAUNCH_ATTRIBUTE_PROGRAMMATIC_STREAM_SERIALIZATION` | 是否启用 PDL 流式重叠 |

`construct_launch_config` 的处理顺序：

1. 若 `smem_size > 0`，先 `cuFuncSetAttribute(kernel, CU_FUNC_ATTRIBUTE_MAX_DYNAMIC_SHARED_SIZE_BYTES, smem_size)`——把内核的动态共享内存上限「解锁」到请求值（Hopper 默认上限很小，必须显式抬升）。
2. 填充 grid/block/sharedMem/stream 等基础字段。
3. 准备一个 `static` 的属性数组 `attrs[3]`（必须 `static`，因为 `config.attrs` 只存指针，局部变量出作用域就悬空）。
4. 依次按需追加 cooperative、cluster、pdl 三个属性，用 `numAttrs` 计数。

#### 4.2.3 源码精读

先看 `LaunchArgs` 的定义与两个构造函数（一个接受 `(x, threads, ...)`，一个接受 `({x,y}, threads, ...)`）：

[csrc/jit/launch_runtime.hpp:14-27](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/launch_runtime.hpp#L14-L27) —— 六字段描述符，默认 `cluster_dim=1, cooperative=false, pdl_enabled=false`。

再看翻译逻辑。handle.hpp 用 `#if` 提供了两套实现：

[csrc/jit/handle.hpp:11](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/handle.hpp#L11) —— 编译期二选一：`CUDART_VERSION >= 12080 && defined(EP_JIT_USE_RUNTIME_API)` 走 Runtime API，否则走 Driver API。

**默认构建走 Driver API 分支**（`#else`），因为 `LaunchRuntime::launch` 永远向 `construct_launch_config` 传 8 个参数（含 cooperative/pdl），只有 Driver API 分支的签名能匹配；Runtime API 分支签名只有 6 个参数且自带 TODO「support cooperative and dependent kernel launch」尚未接上。Driver API 分支即本讲的主线：

```cpp
static LaunchConfigHandle construct_launch_config(const KernelHandle& kernel,
                                                  const cudaStream_t& stream, const int& smem_size,
                                                  const dim3& grid_dim, const dim3& block_dim, const int& cluster_dim,
                                                  const bool& cooperative, const bool& enable_pdl) {
    if (smem_size > 0)
        CUDA_DRIVER_CHECK(lazy_cuFuncSetAttribute(kernel, CU_FUNC_ATTRIBUTE_MAX_DYNAMIC_SHARED_SIZE_BYTES, smem_size));
    LaunchConfigHandle config;
    config.gridDimX = grid_dim.x; ... config.blockDimX = block_dim.x; ...
    config.sharedMemBytes = smem_size;
    config.hStream = stream;

    static LaunchAttrHandle attrs[3];
    config.attrs = attrs; config.numAttrs = 0;

    if (cooperative)        { attrs[numAttrs].id = CU_LAUNCH_ATTRIBUTE_COOPERATIVE; ... }
    if (cluster_dim > 1)    { attrs[numAttrs].id = CU_LAUNCH_ATTRIBUTE_CLUSTER_DIMENSION; ... }
    if (enable_pdl)         { attrs[numAttrs].id = CU_LAUNCH_ATTRIBUTE_PROGRAMMATIC_STREAM_SERIALIZATION; ... }
    return config;
}
```

[csrc/jit/handle.hpp:99-145](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/handle.hpp#L99-L145) —— Driver API 主分支：先解锁共享内存上限，再按需追加三种启动属性。

对照参考，Runtime API 分支（仅当显式定义宏时启用）目前不处理 cooperative/pdl：

[csrc/jit/handle.hpp:40-64](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/handle.hpp#L40-L64) —— Runtime API 分支只处理 cluster，cooperative/pdl 是 TODO。这条分支的存在是为了在 Runtime API 环境下也能编译，但默认不用。

最后是真正下发的 `launch_kernel`：它把可变参数打包成 `void* ptr_args[]`，调用 `cuLaunchKernelEx`：

[csrc/jit/handle.hpp:147-151](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/handle.hpp#L147-L151) —— 把派生类传来的业务参数挨个取地址、打包，调 `cuLaunchKernelEx` 下发。

#### 4.2.4 代码实践

本练习对应本讲规格要求的实践任务，目标是用真实 dispatch 启动器把六个字段讲透。

**实践目标**：在 `launch_dispatch` 中找到 `LaunchArgs` 的构造，解释 `cluster_dim=2-(num_sms%2)`、`cooperative=true` 的作用，并说明 `num_smem_bytes` 来自哪里。

**操作步骤**（源码阅读型实践）：

1. 打开 [csrc/kernels/elastic/dispatch.hpp:225-226](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L225-L226)，定位 dispatch 的 `launch_args` 构造：

```cpp
// NOTES: make cluster dim 2 to overlap with clustered computation kernels
.launch_args = jit::LaunchArgs(num_sms, num_threads, num_smem_bytes, 2 - (num_sms % 2), true)
```

   对照 `LaunchArgs` 构造函数签名 `LaunchArgs(grid_dim_x, num_threads, smem_size, cluster_dim, cooperative, pdl_enabled=false)`，可读出六个字段：
   - `grid_dim = {num_sms, 1}`
   - `num_threads = num_threads`（由 L186-L196 的 warp 划分算出）
   - `smem_size = num_smem_bytes`
   - `cluster_dim = 2 - (num_sms % 2)`
   - `cooperative = true`
   - `pdl_enabled = false`（取默认值）

2. **解释 `cluster_dim = 2 - (num_sms % 2)`**：
   - 当 `num_sms` 为偶数时，`num_sms % 2 == 0`，`cluster_dim = 2`；
   - 当 `num_sms` 为奇数时，`num_sms % 2 == 1`，`cluster_dim = 1`（即不开 cluster，因为 `construct_launch_config` 里 `if (cluster_dim > 1)` 才生效）。
   - 设计意图见紧邻注释：让 dispatch 内核以 cluster=2 启动，**与下游同样使用 cluster=2 的计算内核（如 grouped GEMM）在 cluster 边界上对齐**，便于通信与计算在同一个集群拓扑上衔接。奇数 SM 数（罕见）下退化为不开 cluster，避免越界。

3. **解释 `cooperative = true`**：
   - 协作启动要求 grid 内**所有 block 同时驻留**在 SM 上。dispatch 的 grid 恰为 `num_sms`（= `multiProcessorCount`），配合内核里的 `__launch_bounds__(kNumThreads, 1)`（每 SM 最多 1 个 block，见 u4-l2），正好「一个 SM 一个 block」全部驻留——这是协作启动的典型用法。
   - 有了协作启动，内核内部就能安全使用 grid 级同步（例如让所有 notify warp 先把计数写完、再由 dispatch warp 读走），保证「写计数」与「读计数」的全局先后顺序。

4. **说明 `num_smem_bytes` 来自哪里**：
   - 沿调用链上溯：`launch_dispatch` 的 `num_smem_bytes` 形参由 `ElasticBuffer::dispatch` 传入。
   - 在 [csrc/elastic/buffer.hpp:849](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L849) 可见：

     ```cpp
     const int num_smem_bytes = jit::device_runtime->get_num_smem_bytes();
     ```

   - 而 `get_num_smem_bytes()` 返回 `get_prop()->sharedMemPerBlockOptin`（见 4.3.3）——即当前 GPU「可申请的每 block 最大动态共享内存」。
   - 它在 `launch_dispatch` 里被用于两件事：（a）决定每个 SM 能塞下多少 dispatch warp（[dispatch.hpp:188-189](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L188-L189) 的 `(num_smem_bytes - num_notify_smem_bytes) / token_layout.get_num_bytes<true>()`）；（b）作为 `smem_size` 传给 `construct_launch_config`，由后者调 `cuFuncSetAttribute` 解锁上限。

**需要观察的现象**：把 SM 数从偶数（如 132）改成奇数会在脑中推出 `cluster_dim` 由 2 变 1；把 `cooperative=true` 与 grid=num_sms 联系起来理解「为什么刚好能驻留」。

**预期结果**：你能解释清楚——`num_smem_bytes` 源自 `DeviceRuntime` 缓存的 `sharedMemPerBlockOptin`；`cluster_dim` 用 `2-(num_sms%2)` 在偶数 SM 上启用 size-2 cluster 以贴合下游计算内核；`cooperative=true` 借助 grid=SM 数 + 每 SM 单 block 实现 grid 级同步。**运行结果待本地验证**（可在支持 `EP_JIT_DEBUG` 的环境下打印 L65-L68 的启动摘要核对 cluster/cooperative 值）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `construct_launch_config` 里的 `attrs` 数组必须声明为 `static`？

**参考答案**：因为返回的 `config.attrs` 只是一个指针。若 `attrs` 是局部变量，函数返回后栈帧销毁，`config.attrs` 立刻成为悬空指针，`cuLaunchKernelEx` 读取时是未定义行为。声明为 `static` 让它存活到程序结束。代价是它不是线程安全的——但 DeepEP 的内核启动都在持有 GIL 的 host 线程上串行进行，可接受。

**练习 2**：`dispatch_copy_epilogue` 的 `LaunchArgs` 构造是 `LaunchArgs(num_sms, num_threads, num_smem_bytes, 1, false, true)`（[dispatch.hpp:336](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L336)）。对照 dispatch 主内核，它有哪些不同？为什么？

**参考答案**：epilogue 的 `cluster_dim=1`（不开 cluster）、`cooperative=false`（不协作启动）、`pdl_enabled=true`（开 PDL）。原因：epilogue 是紧随主通信内核之后的纯拷贝收尾，**不需要 grid 级同步**（故不协作），但它希望与前面的内核在流上重叠 prologue/epilogue（故开 PDL）；同时它不与下游 grouped GEMM 共享 cluster 拓扑，故 cluster_dim=1。两个内核的差异恰好说明 `LaunchArgs` 的字段是按内核角色定制的。

---

### 4.3 DeviceRuntime：GPU 能力探测与缓存

#### 4.3.1 概念说明

启动框架要填的字段（grid 的 SM 数、smem 上限）、JIT 编译器要选的 `--gpu-architecture`、内核要用的超时周期数——全都依赖「当前 GPU 是什么型号、有多少资源」。这些信息本可以从 `cudaDeviceProp` 反复查询，但 `cudaGetDeviceProperties` 是一次较重的运行时调用，而 DeepEP 在每次 dispatch/combine 都要启动内核。因此 `DeviceRuntime` 把这些属性**探测一次、缓存复用**，并且用 `LazyInit` 把「第一次访问才探测」的惰性逻辑封装起来。

#### 4.3.2 核心流程

`DeviceRuntime` 提供四个对外能力：

- `get_num_sms()` → `multiProcessorCount`：SM 数，决定 grid 大小。
- `get_num_smem_bytes()` → `sharedMemPerBlockOptin`：每 block 可申请的最大动态共享内存，决定每 SM 能塞多少 warp、并用作 `smem_size`。
- `get_clock_rate()` → 时钟频率（Hz）：把「超时秒数」换算成「超时周期数」。
- `get_arch()` → 形如 `"90a"` / `"100a"`：JIT 编译时给 nvcc 的 `--gpu-architecture`（见 u4-l1）。

三者共享同一份 `cudaDeviceProp` 缓存（`cached_prop`）；时钟频率单独缓存（`cached_clock_rate`，因为它走的是 `cudaDevAttrClockRate` 属性而非 `cudaDeviceProp` 字段）。两者都遵循「`0`/`nullptr` 表示未初始化」的哨兵模式。

超时周期换算是这套缓存最关键的用途之一。`ElasticBuffer` 构造时把用户给的秒数转成 GPU 周期数，公式为：

\[
\text{num\_timeout\_cycles} = \text{num\_timeout\_secs} \times f_{\text{clock}}(\text{Hz})
\]

其中 `cudaDevAttrClockRate` 返回的单位是 kHz，故先 ×1000 转成 Hz。这个周期数被烘焙成内核的**编译期常量**（u4-l2 中 `num_timeout_cycles` 出现在模板参数里），内核内部用 GPU 时钟寄存器自旋到该周期数即判超时。

#### 4.3.3 源码精读

`get_prop()` 是共享的属性缓存入口：

[csrc/jit/device_runtime.hpp:13-22](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/device_runtime.hpp#L13-L22) —— 首次调用 `cudaGetDevice` + `cudaGetDeviceProperties` 探测，存进 `cached_prop`，之后直接返回。

三个派生查询都只是字段读取：

[csrc/jit/device_runtime.hpp:36-47](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/device_runtime.hpp#L36-L47) —— `get_num_smem_bytes`/`get_num_sms`/`get_arch_pair` 都直接读 `cached_prop` 对应字段。

时钟频率独立缓存（注意 kHz → Hz 的换算）：

[csrc/jit/device_runtime.hpp:25-34](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/device_runtime.hpp#L25-L34) —— `get_clock_rate` 用 `cudaDevAttrClockRate` 取 kHz，乘 1000 得 Hz，缓存于 `cached_clock_rate`。

架构字符串（给 nvcc 用）含一个 Blackwell 特判：

[csrc/jit/device_runtime.hpp:49-58](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/device_runtime.hpp#L49-L58) —— 对 `major==10`（Blackwell，且非 10.1）特殊处理返回 `100`/`100a`/`100f`，其余按 `major*10+minor` 拼出如 `90a`。

全局单例由 `LazyInit` 持有，首次 `->` 解引用才构造：

[csrc/jit/device_runtime.hpp:65](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/device_runtime.hpp#L65) —— `static auto device_runtime = LazyInit<DeviceRuntime>(...)`。

`LazyInit` 的实现非常薄：重载 `operator->`，首次访问调 factory 构造，之后复用：

[csrc/utils/lazy_init.hpp:10-25](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/utils/lazy_init.hpp#L10-L25) —— `operator->` 内部判空，`ptr` 为空则调 `factory()` 构造。这就是 `device_runtime->get_num_sms()` 能「用到才探测」的原因。

超时周期的换算发生在 `ElasticBuffer` 构造函数里：

[csrc/elastic/buffer.hpp:120-123](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L120-L123) —— `num_gpu_timeout_cycles = num_gpu_timeout_secs * device_runtime->get_clock_rate()`，把秒数乘以时钟频率（Hz）得到周期数，随后作为模板参数烘焙进内核。

而 dispatch host 函数取 SM 数与共享内存的方式如下（注意 `num_sms == 0` 时回落到设备真实 SM 数）：

[csrc/elastic/buffer.hpp:351](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L351) 与 [csrc/elastic/buffer.hpp:353](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L353) —— `num_sms == 0 ? device_runtime->get_num_sms() : num_sms`、`device_runtime->get_num_smem_bytes()`。这给了上层一个「省 SM 让位计算流」的开关（`prefer_overlap_with_compute`，见 u3-l3）。

#### 4.3.4 代码实践

**实践目标**：体会 `DeviceRuntime` 的「探测一次、处处复用」，并理解超时周期换算。

**操作步骤**（源码阅读型实践）：

1. 在 [csrc/elastic/buffer.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp) 中用搜索定位所有 `device_runtime->` 调用，你会看到它们分布在构造函数（`get_clock_rate`，L123）、dispatch（`get_num_smem_bytes`，L849）、combine、barrier、engram、pp、agrs 等几乎每个启动器前——但 `DeviceRuntime` 的探测只会发生**第一次**。
2. 设想一次完整的 `import deep_ep` → 创建 `ElasticBuffer` → dispatch 流程，列出 `cudaGetDeviceProperties` 实际被调用的次数（答案：1 次，在首次访问 `device_runtime->` 时；之后的 `get_num_sms`/`get_num_smem_bytes`/`get_clock_rate` 全部命中缓存）。
3. **超时换算手算**：若 GPU 时钟频率为 1830 MHz（即 `cudaDevAttrClockRate = 1830000` kHz），`num_gpu_timeout_secs = 10`，则 `num_timeout_cycles = 10 × 1830000 × 1000 = 1.83e13` 个周期。

**需要观察的现象**：无论调度多少次内核，`cudaGetDeviceProperties` 只在第一次出现；超时周期数与「秒数 × Hz」严格成正比。

**预期结果**：你能解释清楚 `DeviceRuntime` 的缓存意义（避免每次启动重复探测），并能用上面的公式把秒数换算成周期数。运行结果待本地验证（可在 `EP_JIT_DEBUG=1` 下观察生成的 `.cu` 里 `num_timeout_cycles` 这个模板常量是否符合手算量级）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `get_clock_rate` 不复用 `cached_prop`，而要单独探测？

**参考答案**：因为 `cudaDeviceProp` 结构体里没有「当前时钟频率」这个字段（它只有 `clockRate` 的最大值语义，且单位是 kHz）。DeepEP 用 `cudaDeviceGetAttribute(.., cudaDevAttrClockRate, ..)` 单独取，并明确注释「convert kHz into Hz」，这样换算出的超时周期数才与内核里用 GPU 时钟寄存器读到的周期数单位一致。

**练习 2**：如果把 `LazyInit` 换成全局静态变量直接构造（`static DeviceRuntime device_runtime;`），会有什么问题？

**参考答案**：构造 `DeviceRuntime` 本身并不调用 CUDA API（探测发生在首次 `get_prop()`），所以「直接构造」在 CUDA 初始化时机上其实也安全。但 `LazyInit` 的真正价值在于**统一惰性模式**——`device_runtime`、`compiler`、`include_parser`、`device_runtime` 等多个单例都用同一套「用到才构造」的语义，避免在 `import deep_ep`（即进程初始化、可能尚未初始化 CUDA 上下文）阶段就触发任何 CUDA 调用，把 CUDA 初始化推迟到真正用到 GPU 时。

---

### 4.4 IncludeParser：头文件哈希如何进入缓存签名

#### 4.4.1 概念说明

u4-l3 讲过，JIT 缓存以「内核签名 + 编译标志 + 源码」的内容哈希作为目录名。但这里有个陷阱：源码里 `#include <deep_ep/impls/dispatch.cuh>`，而 `dispatch.cuh` 又会 include 一串别的头文件。**如果只对生成的 `.cu` 文本求哈希，那么改了 `dispatch.cuh` 里的内核逻辑，缓存却不会失效**——程序会继续加载旧的 cubin，bug 永远修不上。

`IncludeParser` 就是来解决这个的：它递归解析所有 `<deep_ep/*>` 头文件，逐个算哈希并拼接，再把这份「头文件哈希」作为首行注释注入生成的 `.cu`。于是只要任意被 include 的头文件改动，首行注释就变 → 生成的 `.cu` 文本变 → u4-l3 的缓存签名变 → 缓存失效重编。

#### 4.4.2 核心流程

- `get_includes(code)`：用正则 `#\s*include\s*[<"][^>"]+[>"]` 抓出所有 include 行；只保留 `<deep_ep/...>` 形式，遇到非标准 include（如 `"foo.h"` 或带空格的 `< foo >`）直接 `EP_HOST_UNREACHABLE` 报错。
- `get_hash_value_by_path(path)`：读文件 → 递归算哈希，用 `cache[path] = nullopt` 作哨兵检测循环 include → 最终把结果回填到 `cache[path]`。
- `get_hash_value(code, exclude_code=true)`：对当前代码里每个 include 取其递归哈希，用 `$` 拼接，最后整体求一次摘要；`exclude_code=true` 表示**只算头文件、不含本段代码**（因为本段代码的哈希在 u4-l3 由编译器另行计入）。
- `LaunchRuntime::generate` 把这份哈希以 `// Includes' hash value: ...` 形式拼到代码首行，并用函数级 `static` 缓存，保证同一种内核只算一次。

循环 include 检测的要点：进入某文件时先置 `cache[path] = nullopt`；若递归过程中再次访问到它，命中 `nullopt` 即判定为循环并报错；正常返回时再用真实哈希覆盖。

#### 4.4.3 源码精读

include 解析与格式校验：

[csrc/jit/include_parser.hpp:16-38](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/include_parser.hpp#L16-L38) —— 正则抓 include；只认 `<deep_ep/...>`，非法格式直接报错。

顶层哈希计算（默认 `exclude_code=true`，只算头文件部分）：

[csrc/jit/include_parser.hpp:47-54](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/include_parser.hpp#L47-L54) —— 对每个 include 调 `get_hash_value_by_path`，用 `$` 分隔拼接后求总摘要。

按路径递归求哈希 + 循环检测：

[csrc/jit/include_parser.hpp:56-73](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/include_parser.hpp#L56-L73) —— 先查缓存；未命中则读文件、置 `nullopt` 哨兵、递归求 `get_hash_value(code, false)`（此处含代码本身，因为是「这个文件的内容哈希」），回填缓存。

`library_include_path` 在 `init_jit` 阶段被钉入（u2-l1），它给出 `<deep_ep/...>` 解析的物理根目录：

[csrc/jit/include_parser.hpp:41-45](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/include_parser.hpp#L41-L45) —— `prepare_init(library_root_path)` 把 `library_root/include` 存为解析根。

回到 `LaunchRuntime::generate`，它把哈希注入首行、并做函数级静态缓存：

[csrc/jit/launch_runtime.hpp:37-42](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/launch_runtime.hpp#L37-L42) —— `static std::string include_hash` 首次为空才算，之后复用；注释明确「we require that generate_impl's includes never change」（同一类内核的头文件集合固定）。

#### 4.4.4 代码实践

**实践目标**：验证「改头文件 → include 哈希变 → 缓存失效」这条链路。

**操作步骤**（源码阅读 + 可选本地实验）：

1. 设置 `EP_JIT_CACHE_DIR=/tmp/ep_cache` 并开启 `EP_JIT_DEBUG=1`，跑一次 `tests/elastic/test_ep.py`。
2. 在 `EP_JIT_DEBUG` 打印的 "Generated kernel code:" 中，找到首行 `// Includes' hash value: <hex>`，记下该哈希。
3. 进缓存目录 `/tmp/ep_cache/`，观察它按 u4-l3 的内容哈希分目录；任选一个 `kernel.cu` 打开，确认首行也带同样的 include 哈希注释。
4. （可选本地实验）在 `deep_ep/include/deep_ep/impls/dispatch.cuh` 末尾加一行无害注释，再次运行——**预期**会生成一个新的 include 哈希、一个新的缓存目录（即缓存失效、触发重编）。⚠️ 注意：本步骤会修改源码，仅在你自己的副本上做实验，做完请还原。

**需要观察的现象**：include 哈希随头文件内容变化而变化；缓存目录随之新增。

**预期结果**：你能说清 include 哈希是缓存签名的「头文件敏感」维度，弥补了「只对生成代码求哈希」的盲区。运行结果待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `get_hash_value` 默认 `exclude_code=true`，而 `get_hash_value_by_path` 内部递归时却传 `exclude_code=false`？

**参考答案**：两者算的是不同东西。顶层 `get_hash_value(code)` 算的是「这段**生成代码**所依赖的头文件指纹」，生成代码本身的哈希由 u4-l3 的编译器另外计入签名，所以这里要 `exclude_code=true` 避免重复。而 `get_hash_value_by_path` 算的是「某个**头文件本身**的内容哈希」，一个文件的内容当然要把自己算进去，所以递归调 `get_hash_value(code, false)`。

**练习 2**：`cache[path] = std::nullopt` 这个赋值在循环检测里起什么作用？

**参考答案**：它是一个「正在处理中」的哨兵。当递归进入文件 A 时先置 A 为 `nullopt`；若 A（直接或间接）又 include 了 A，再次查 `cache[A]` 会命中这个 `nullopt`，于是 `get_hash_value_by_path` 据此判定出现循环并报 `EP_HOST_UNREACHABLE("Circular include may occur")`，防止无限递归。正常结束时再用真实哈希覆盖 `nullopt`。

---

## 5. 综合实践

把本讲的四个模块串起来，完成下面这个「追踪一次 dispatch 启动配置」的小任务，它综合了 `DeviceRuntime` 探测、`LaunchArgs` 构造、`construct_launch_config` 翻译、`IncludeParser` 签名四个环节。

**任务**：以 `tests/elastic/test_ep.py` 在单机 8 卡上的 dispatch 为对象，画出从「GPU 能力」到「最终 `cuLaunchKernelEx` 调用」的完整数据流，并回答以下问题。

1. **SM 数**：本次 dispatch 用了多少个 SM？追踪 `buffer.hpp` 中 `num_sms == 0 ? device_runtime->get_num_sms() : num_sms`，结合你 `prefer_overlap_with_compute` 的设置，说明 grid 维度是「全部 SM」还是「省下来的部分 SM」。
2. **共享内存**：`num_smem_bytes` 来自 `sharedMemPerBlockOptin`，在你的 GPU 上这个值是多少？它如何同时决定（a）`launch_dispatch` 内每 SM 的 dispatch warp 数、（b）下发给 `cuFuncSetAttribute` 的 `smem_size`？
3. **cluster/cooperative**：写出本次 dispatch 的 `cluster_dim` 与 `cooperative` 值，并用「grid=SM 数 + 每 SM 单 block + `__launch_bounds__(threads,1)`」解释为什么协作启动在这里成立。
4. **超时周期**：根据你的 GPU 时钟频率，把构造函数里的 `num_gpu_timeout_secs` 换算成 `num_timeout_cycles`，确认它作为模板参数出现在 `EP_JIT_DEBUG` 打印的生成代码中。
5. **include 哈希**：在 `EP_JIT_DEBUG` 输出里找到首行 include 哈希，解释它由哪些头文件（递归）贡献，并说明改动其中任一头文件为何会让本次 dispatch 重新 JIT。

**预期产出**：一张数据流图 + 五个问题的回答。本实践为源码阅读型，运行数字待本地验证；答题所需的所有代码线索均可在本讲给出的永久链接中找到。

## 6. 本讲小结

- `LaunchRuntime<Derived>` 用 CRTP 把「启动」标准化：基类 `launch()` 从 `args.launch_args` 取描述符、构造 `CUlaunchConfig`、回调派生类 `launch_impl`，派生类零样板。
- `LaunchArgs` 是一个六字段纯描述符 `grid_dim / num_threads / smem_size / cluster_dim / cooperative / pdl_enabled`，由 `construct_launch_config` 翻译成 CUDA Driver API 的属性数组；默认构建走 Driver API 分支（含 cooperative/cluster/PDL），Runtime API 分支为可选且目前尚未接 cooperative/PDL。
- dispatch 主内核用 `LaunchArgs(num_sms, num_threads, num_smem_bytes, 2-(num_sms%2), true)`：偶数 SM 开 size-2 cluster 以贴合下游计算内核、cooperative=true 借助「一 SM 一 block」实现 grid 级同步；epilogue 内核则反之开 PDL。
- `DeviceRuntime` 用 `LazyInit` 惰性探测并缓存 `cudaDeviceProp`：提供 `get_num_sms`/`get_num_smem_bytes`/`get_clock_rate`/`get_arch`，其中时钟频率把超时秒数换算成 GPU 周期数并烘焙为编译期常量。
- `IncludeParser` 递归解析 `<deep_ep/*>` 头文件并求哈希，经 `LaunchRuntime::generate` 注入生成代码首行，使 JIT 缓存对头文件改动保持敏感（补全 u4-l3 内容寻址缓存的最后一块拼图）。
- 全局单例 `device_runtime`、`include_parser` 均为惰性初始化，保证 `import deep_ep` 阶段不触发任何 CUDA 调用。

## 7. 下一步学习建议

本讲讲完「启动框架」，U4 JIT 单元到此结束。接下来建议：

1. **进入 U5 Dispatch 内核链路深入**：从 [u5-l1 直接模式 Dispatch](u5-l1-direct-dispatch.md) 开始，真正进入 `deep_ep/include/deep_ep/impls/dispatch.cuh` 的 GPU 内核实现，看 notify/dispatch warp 如何用本讲下发的共享内存与 grid 级同步完成 token 发送。
2. **对照阅读其它启动器**：用本讲的框架去读 `csrc/kernels/elastic/` 下的 `combine.hpp`、`barrier.hpp`、`engram.hpp`、`pp_send_recv.hpp`，体会它们各自的 `LaunchArgs` 构造差异（哪些开 cluster、哪些开 PDL、哪些不需要 cooperative）。
3. **若对编译期常量化感兴趣**：回头重读 u4-l2 的 `generate_impl`，把本讲的 `num_timeout_cycles`、`num_sms`（作为 grid_dim）与 u4-l2 的模板参数列表对照，理解「为什么这些量必须编译期化」。
4. **PTX 原语预备**：U5/U6 的内核会大量使用 TMA、mbarrier、fence.proxy 等底层原语，可提前阅读 [u8-l1 PTX 原语](u8-l1-ptx-tma-mbarrier.md) 建立 PTX 直觉。
