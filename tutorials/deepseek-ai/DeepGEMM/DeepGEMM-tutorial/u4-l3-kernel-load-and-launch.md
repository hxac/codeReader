# 内核加载与启动句柄

## 1. 本讲目标

在前两讲（u3-l1、u3-l3）里，我们已经看清 JIT 编译系统的骨架：`Compiler::build` 把一段 `.cu` 源码编译成 `kernel.cubin` 并缓存。但 **cubin 只是磁盘上的一个二进制文件**，GPU 不能直接执行文件。从「一个文件」到「真正跑起来的 kernel」，中间还差最后一步：**把 cubin 加载进 CUDA 上下文、找到 kernel 入口符号、组装启动配置、调用启动 API**。

本讲就负责打通这最后一步。学完后你应该能够：

- 说清 `KernelRuntime` 如何把 `kernel.cubin` 加载成一个可启动的 `KernelHandle`，以及为什么 DeepGEMM 坚持「一个 cubin 恰好一个 kernel」的契约；
- 区分 CUDA Driver API（`cu*`）与 Runtime API（`cuda*`）两条加载路径，理解 `cuLibraryLoadFromFile` / `cuLibraryEnumerateKernels` 的符号定位机制；
- 看懂 `construct_launch_config` 如何组装 grid/block/smem，以及 **cluster（线程块簇）** 和 **PDL（程序化依赖启动）** 两个启动属性从何而来；
- 理解 `LaunchRuntime<Derived>` 这个 CRTP 基类如何用 `generate` / `launch` 两个公共方法 + `generate_impl` / `launch_impl` 两个钩子，统一十几个不同 kernel 的「生成→编译→启动」流程。

## 2. 前置知识

在进入源码前，先建立几个关键概念。

### 2.1 cubin、模块与句柄

`nvcc` / `nvrtc` 把 `.cu` 源码编译后，产物之一是 **cubin（CUDA binary）**——一段可直接被 GPU 执行的机器码。但 host 代码不能「调用一个文件」，必须先把 cubin **加载进 CUDA 上下文**，得到一个**模块（module / library）**对象；再从模块里**查找 kernel 的入口符号**，得到一个**句柄（handle）**。之后所有启动 API（如 `cuLaunchKernelEx`）拿的都是这个句柄，而不是文件名。

DeepGEMM 把这条链路的两端分别命名为：

| 概念 | Driver API 类型 | Runtime API 类型 | 含义 |
|---|---|---|---|
| 库 / 模块句柄 `LibraryHandle` | `CUlibrary`（或旧版 `CUmodule`） | `cudaLibrary_t` | 加载后的整个 cubin 容器 |
| 内核句柄 `KernelHandle` | `CUfunction` | `cudaKernel_t` | 可启动的单个 kernel 入口 |
| 启动配置 `LaunchConfigHandle` | `CUlaunchConfig` | `cudaLaunchConfig_t` | grid/block/smem/属性 的打包 |

### 2.2 Driver API 与 Runtime API

CUDA 有两层 API：

- **Driver API（`cu*`）**：底层、显式管理句柄，DeepGEMM **默认走这条路**；
- **Runtime API（`cuda*`）**：高层封装，当编译时定义 `DG_JIT_USE_RUNTIME_API` 且 `CUDART >= 12.8` 时启用，默认关闭（见 [setup.py:25](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/setup.py#L25) 与 [README.md:185](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/README.md#L185)）。

`csrc/jit/handle.hpp` 用宏 `#if/#else` 同时维护这两套实现，对外暴露**完全相同**的函数名（`load_kernel`、`construct_launch_config`、`launch_kernel`），上层无需关心走哪条路。

### 2.3 启动属性：cluster 与 PDL

Hopper（SM90）与 Blackwell（SM100）引入了两个影响 kernel 启动方式的硬件特性：

- **Thread Block Cluster（线程块簇）**：把若干个 CTA（线程块）编成一组，组内可通过**分布式共享内存**互相访问。`cluster_dim` 表示簇在 X 维的尺寸（1 表示不分组，2 表示 SM100 上常见的「2-CTA UMMA」）。
- **PDL（Programmatic Dependent Launch，程序化依赖启动）**：让下一个 kernel 在上一个 kernel **还没完全结束时**就开始初始化（如 TMA 预取），从而把上一个 kernel 的「尾部」与下一个 kernel 的「头部」重叠起来，降低 launch 之间的空泡。

这两个特性不是 kernel 内部行为，而是**启动时声明**的，所以它们被放进 `LaunchConfigHandle` 的**属性数组**里。

### 2.4 CRTP（奇异递归模板模式）

`LaunchRuntime<Derived>` 是一个 CRTP 基类：子类写成 `class FooRuntime : public LaunchRuntime<FooRuntime>`。基类在编译期就知道子类的真实类型，于是可以直接调用 `Derived::generate_impl` / `Derived::launch_impl`，**没有虚函数开销**，却仍能把「所有 kernel 都一样的流程」抽到基类里。理解这一点是看懂第三模块的关键。

## 3. 本讲源码地图

本讲聚焦宿主侧「cubin → 启动」这段链路，涉及以下文件：

| 文件 | 作用 |
|---|---|
| [csrc/jit/handle.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/handle.hpp) | **本讲主角**。定义句柄类型别名、`load_kernel`（加载 cubin + 定位符号）、`construct_launch_config`（组装启动配置 + cluster/PDL 属性）、`launch_kernel`（调用启动 API）。Driver/Runtime 两套实现并存。 |
| [csrc/jit/kernel_runtime.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp) | 定义 `LaunchArgs`（启动参数包）、`KernelRuntime`（cubin 加载器）、`LaunchRuntime<Derived>`（CRTP 基类，统一 generate/launch）。 |
| [csrc/jit/cache.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/cache.hpp) | `KernelRuntimeCache`：内存层缓存，命中则直接返回已构造的 `KernelRuntime`，否则就地构造。 |
| [csrc/jit/compiler.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp) | `Compiler::build`：查缓存→（未命中则）编译→原子重命名→返回 `shared_ptr<KernelRuntime>`。 |
| [csrc/jit/device_runtime.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/device_runtime.hpp) | 进程级单例，提供 `get_pdl()` 等全局旋钮，决定 PDL 的最终取值。 |
| [csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp) | 一个具体的 Runtime 子类示例，展示 `launch_impl`、`LaunchArgs` 构造与 generate/build/launch 触发点。 |

数据流回顾（承接 u3-l1）：`build` 产出 `shared_ptr<KernelRuntime>` → `LaunchRuntime::launch` 取出其中的 `kernel` 句柄 → `construct_launch_config` 组装配置 → `launch_kernel` 调 `cuLaunchKernelEx`。

## 4. 核心概念与源码讲解

### 4.1 cubin 加载与符号枚举

#### 4.1.1 概念说明

这一模块回答两个问题：

1. **如何把磁盘上的 `kernel.cubin` 变成 host 手里一个可启动的句柄？**
2. **cubin 里可能有多个函数，如何精确找到「那个」kernel 入口？**

DeepGEMM 用一条关键契约回避了第二个问题的复杂性：**每个 JIT 生成的 `.cu` 源码里恰好实例化一个 kernel**（u3-l2 讲过，生成代码靠「取函数地址」强制模板实例化，所以每个 `.cu` 对应一个 `__global__` 入口）。于是「一个 cubin 恰好一个 kernel」成为不变量，加载时只需**枚举全部 kernel 并断言数量为 1**，无需知道 kernel 的 mangled name（被 C++ 名称修饰后的真实符号名）。

这套逻辑由 `KernelRuntime` 类封装：它持有一个 `LibraryHandle` 和一个 `KernelHandle`，构造时完成「加载 + 定位」，析构时卸载库。`build` 把它包进 `shared_ptr` 放进 `KernelRuntimeCache`，于是**同一形状只加载一次**。

#### 4.1.2 核心流程

`KernelRuntime` 构造函数的执行流程（默认 Driver API 分支）：

```text
KernelRuntime(dir_path)
  ├─ 读取 dir_path/kernel.cubin
  ├─ load_kernel(cubin_path, func_name={}, &library)
  │     ├─ cuLibraryLoadFromFile(...)        # 加载 cubin → CUlibrary
  │     ├─ cuLibraryGetKernelCount(&n, lib)  # 数 kernel 个数
  │     ├─ assert(n == 1)                     # 守「恰好一个」契约
  │     ├─ cuLibraryEnumerateKernels(&k, 1, lib)  # 取出那一个 CUkernel
  │     └─ cuKernelGetFunction(&kernel, k)   # CUkernel → CUfunction
  └─ 缓存 library + kernel
```

注意三条要点：

1. **函数名 `func_name` 在新驱动分支里被忽略**（传的是 `{}`），因为靠枚举而非按名查找。
2. **旧驱动（< 12.4）没有 `cuLibraryEnumerateKernels`**，于是回退到调用外部命令 `cuobjdump -symbols` 解析符号表文本，过滤掉 `vprintf`、`__instantiate_kernel` 等非法符号，断言剩下恰好一个，再按名 `cuModuleLoad` + `cuModuleGetFunction` 加载。
3. **`check_validity` 守护目录完整性**：若缓存目录存在，则 `kernel.cu` 与 `kernel.cubin` 必同时存在（这是 u3-l3 原子整目录 rename 保证的不变量），否则判定为损坏并要求用户 `rm -rf`。

#### 4.1.3 源码精读

先看 `KernelRuntime` 构造函数的两条分支入口（[csrc/jit/kernel_runtime.hpp:35-90](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L35-L90)）：

```cpp
explicit KernelRuntime(const std::filesystem::path& dir_path) {
    DG_HOST_ASSERT(not cuda_home.empty());
    const auto cubin_path = dir_path / "kernel.cubin";
    // ...计时打印...
#ifdef DG_JIT_USE_LIBRARY_ENUM_KERNELS
    kernel = load_kernel(cubin_path, {}, &library);          // 新驱动：枚举
#else
    // 旧驱动：cuobjdump 解析符号表，过滤后断言恰好一个
    const auto [exit_code, symbols] = call_external_command(
        fmt::format("{} -symbols {}", cuobjdump_path.c_str(), cubin_path.c_str()));
    // ...解析 STT_FUNC && STO_ENTRY 的行，剔除 vprintf/__instantiate_kernel 等...
    DG_HOST_ASSERT(symbol_names.size() == 1);
    kernel = load_kernel(cubin_path, symbol_names[0], &library);  // 旧驱动：按名
#endif
}
```

上面的 `cuobjdump` 分支之所以要**剔除 `__instantiate_kernel` 等符号**，正是因为 u3-l2 的代码生成会写一行 `__instantiate_kernel<<<...>>>(...)` 来强制模板展开——它本身是 host 端的启动桩，并不是真正的 device kernel，必须从符号列表里排除掉，才能让「恰好一个」断言成立。

再看默认 Driver API 分支下 `load_kernel` 的真正实现（[csrc/jit/handle.hpp:135-163](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/handle.hpp#L135-L163)）：

```cpp
static KernelHandle load_kernel(const std::filesystem::path& cubin_path,
                                const std::string& func_name,
                                LibraryHandle *library_opt = nullptr) {
    LibraryHandle library;
    KernelHandle kernel;
#ifdef DG_JIT_USE_LIBRARY_ENUM_KERNELS
    DG_CUDA_DRIVER_CHECK(lazy_cuLibraryLoadFromFile(&library, cubin_path.c_str(), ...));
    unsigned int num_kernels;
    DG_CUDA_DRIVER_CHECK(lazy_cuLibraryGetKernelCount(&num_kernels, library));
    if (num_kernels != 1) {
        // 打印「Corrupted JIT cache directory」并断言失败
        DG_HOST_ASSERT(false and "Corrupted JIT cache directory");
    }
    CUkernel cu_kernel;
    DG_CUDA_DRIVER_CHECK(lazy_cuLibraryEnumerateKernels(&cu_kernel, 1, library));
    DG_CUDA_DRIVER_CHECK(lazy_cuKernelGetFunction(&kernel, cu_kernel));
#else
    DG_CUDA_DRIVER_CHECK(lazy_cuModuleLoad(&library, cubin_path.c_str()));
    DG_CUDA_DRIVER_CHECK(lazy_cuModuleGetFunction(&kernel, library, func_name.c_str()));
#endif
    if (library_opt != nullptr) *library_opt = library;
    return kernel;
}
```

两个细节值得注意：

- 这里出现的 `lazy_cuLibraryLoadFromFile` 等 `lazy_` 前缀函数，来自 [csrc/jit/handle.hpp:14-46](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/handle.hpp#L14-L46) 的「**懒加载 driver 符号**」机制：用 `dlopen("libcuda.so.1")` + `dlsym` 在**首次调用时**才解析 `cu*` 符号地址。好处是 `_C` 扩展在加载时不硬绑定具体 driver 版本——`cuLibraryEnumerateKernels` 是 Driver API 12.4 才有的，懒加载让旧环境也不会因为缺符号而加载失败（只在真正调用时才断言）。
- 当 `num_kernels != 1` 时，DeepGEMM 判定**缓存目录损坏**（因为契约保证恰好一个），提示用户 `rm -rf` 该目录并重启。这把「cubin 被意外污染」变成了一个可自愈的明确错误，而不是神秘的启动失败。

`check_validity` 与析构（[csrc/jit/kernel_runtime.hpp:96-114](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L96-L114)）则负责「目录完整性校验」与「析构时 `unload_library`」。

最后，`KernelRuntime` 是在哪里被构造的？在缓存层（[csrc/jit/cache.hpp:18-26](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/cache.hpp#L18-L26)）：内存缓存未命中且 `check_validity` 通过时，就地 `make_shared<KernelRuntime>(dir_path)`；而它的调用方是 `Compiler::build` 末尾（[csrc/jit/compiler.hpp:146](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L146)）。

#### 4.1.4 代码实践

> 这是一个**源码阅读型实践**（无需 GPU 也能完成）。

**实践目标**：理解 `cuLibraryEnumerateKernels` 这条路径，并亲手验证「恰好一个 kernel」契约的来源。

**操作步骤**：

1. 打开 [csrc/jit/handle.hpp:123-131](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/handle.hpp#L123-L131)，确认 `DG_JIT_USE_LIBRARY_ENUM_KERNELS` 由 `CUDA_VERSION >= 12040` 开启。
2. 打开 [csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp)（u3-l2 精读过），找到 `generate_impl` 生成的 `.cu` 源码里被「取地址」的那个 `__global__` 模板函数，确认**它就是 cubin 里唯一的 device kernel**。
3. 反过来在 [csrc/jit/kernel_runtime.hpp:56-68](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L56-L68) 中找到 `illegal_names` 列表，解释为什么 `__instantiate_kernel` 必须被剔除——它对应生成代码里的哪一行？

**需要观察的现象 / 预期结果**：

- 你应能指出：新驱动分支用 `cuLibraryEnumerateKernels(&k, 1, library)`「最多取 1 个」再用 count 断言，**完全绕开了符号名**；
- `__instantiate_kernel` 是 host 端的启动桩函数，不是 device kernel，若不剔除会使 `symbol_names.size() == 1` 断言失败。

> 若有 SM90/SM100 环境，可设 `DG_JIT_DEBUG=1` 跑一次 GEMM，观察控制台打印的 `Loading CUBIN: .../kernel.cubin`（[csrc/jit/kernel_runtime.hpp:42-43](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L42-L43)）与 `Load time (...): X ms`，即为本模块的运行时佐证。否则标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 DeepGEMM 不直接按 kernel 的 C++ 函数名（如 `sm90_fp8_gemm_1d1d_impl<...>`）去 `cuModuleGetFunction`，而要费力枚举？

**参考答案**：模板实例化后的 kernel，其真实符号会被 C++ 名称修饰（name mangling）成一长串含模板参数的字符串，且随编译器/参数变化。靠「枚举 + 断言恰好一个」可完全无视 mangled name，鲁棒性远高于硬编码或拼字符串。旧驱动没有枚举 API，才不得不退而求其次用 `cuobjdump -symbols` 解析。

**练习 2**：`num_kernels != 1` 时报「Corrupted JIT cache directory」。请举出一种可能触发它的情形。

**参考答案**：两个并发 rank 同时往同一个临时目录写、rename 又因竞争失败后清理不彻底；或外部工具手动改动了 cubin 文件使其符号表损坏，都可能让一个 cubin 里出现 0 个或多个 kernel，从而违反「恰好一个」契约。提示信息给出的修复手段是 `rm -rf` 该缓存目录后重启。

---

### 4.2 LaunchConfig 与 PDL/cluster

#### 4.2.1 概念说明

拿到 `KernelHandle` 后，启动 kernel 还需要告诉 CUDA **怎么启动**：多少个线程块（grid）、每块多少线程（block）、用多少共享内存、跑在哪个 stream 上，以及是否启用 cluster / PDL。这些被打包进 `LaunchConfigHandle`（Driver API 的 `CUlaunchConfig`）。

其中 grid/block/smem/stream 是「常规四件套」，而 **cluster 与 PDL 是两个可选的启动属性（attribute）**，挂在一个 `attrs[]` 数组上。`construct_launch_config` 的职责就是把这六样东西组装成一个 `CUlaunchConfig`。

还有两个容易被忽略但很关键的点：

1. **共享内存上限**：CUDA 默认只允许 kernel 用 48KB 动态共享内存。DeepGEMM 的流水线 kernel 动辄用上百 KB 共享内存，所以必须先调 `cuFuncSetAttribute(..., CU_FUNC_ATTRIBUTE_MAX_DYNAMIC_SHARED_SIZE_BYTES, smem_size)` **把上限抬高**，否则启动会失败。
2. **属性数组必须是 `static`**：源码里 `static LaunchAttrHandle attrs[2];` 有一句注释「must use `static` or the attr will be deconstructed」——因为 `construct_launch_config` 返回的是值类型 `config`，但 `config.attrs` 是个**指针**，指向栈上的 `attrs`；若 `attrs` 不是 `static`，函数返回后栈帧销毁，指针立即悬空。

#### 4.2.2 核心流程

`construct_launch_config(kernel, stream, smem_size, grid_dim, block_dim, cluster_dim, enable_pdl)` 的流程：

```text
1. 若 smem_size > 0：
     cuFuncSetAttribute(kernel, MAX_DYNAMIC_SHARED_SIZE_BYTES, smem_size)  # 抬高共享内存上限
2. 把 grid/block/smem/stream 填进 config
3. numAttrs = 0; config.attrs = &attrs[0]      # static 数组
4. 若 cluster_dim > 1：
     attrs[numAttrs] = {CLUSTER_DIMENSION, {cluster_dim,1,1}}; numAttrs++
5. 若 enable_pdl：
     attrs[numAttrs] = {PROGRAMMATIC_STREAM_SERIALIZATION, allowed=1}; numAttrs++
6. return config
```

随后 `launch_kernel(kernel, config, args...)` 把变长参数 `args...` 打包成 `void* ptr_args[]`，调用 `cuLaunchKernelEx(&config, kernel, ptr_args, nullptr)`。

cluster 维度与 CTA 数的关系可写成：

\[
\text{num\_clusters} = \frac{\text{gridDim}.x}{\text{cluster\_dim}}
\]

即 cluster 把 grid 在 X 维按 `cluster_dim` 个 CTA 切成若干簇。`cluster_dim=1` 时不启用簇，等价于传统启动。

#### 4.2.3 源码精读

默认 Driver API 分支下的 `construct_launch_config`（[csrc/jit/handle.hpp:174-213](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/handle.hpp#L174-L213)）：

```cpp
static LaunchConfigHandle construct_launch_config(const KernelHandle& kernel,
        const cudaStream_t& stream, const int& smem_size,
        const dim3& grid_dim, const dim3& block_dim,
        const int& cluster_dim, const bool& enable_pdl) {
    if (smem_size > 0)
        DG_CUDA_DRIVER_CHECK(lazy_cuFuncSetAttribute(
            kernel, CU_FUNC_ATTRIBUTE_MAX_DYNAMIC_SHARED_SIZE_BYTES, smem_size));

    LaunchConfigHandle config;
    config.gridDimX = grid_dim.x; config.gridDimY = grid_dim.y; config.gridDimZ = grid_dim.z;
    config.blockDimX = block_dim.x; config.blockDimY = block_dim.y; config.blockDimZ = block_dim.z;
    config.sharedMemBytes = smem_size;
    config.hStream = stream;

    // NOTES: must use `static` or the `attr` will be deconstructed
    static LaunchAttrHandle attrs[2];
    config.numAttrs = 0;
    config.attrs = attrs;

    // Cluster size
    if (cluster_dim > 1) {
        auto& attr = attrs[config.numAttrs ++];
        attr.id = CU_LAUNCH_ATTRIBUTE_CLUSTER_DIMENSION;
        attr.value.clusterDim.x = static_cast<unsigned>(cluster_dim);
        attr.value.clusterDim.y = 1; attr.value.clusterDim.z = 1;
    }
    // Dependent kernel launch (PDL)
    if (enable_pdl) {
        auto& attr = attrs[config.numAttrs ++];
        attr.id = CU_LAUNCH_ATTRIBUTE_PROGRAMMATIC_STREAM_SERIALIZATION;
        attr.value.programmaticStreamSerializationAllowed = 1;
    }
    return config;
}
```

注意 `attrs[2]` 的大小正好是 2——因为 cluster 与 PDL 是 DeepGEMM 唯一用到的两个启动属性，最多同时启用，数组无需更大。

`launch_kernel` 则极薄（[csrc/jit/handle.hpp:215-219](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/handle.hpp#L215-L219)）：

```cpp
template<typename... ActTypes>
static auto launch_kernel(const KernelHandle& kernel, const LaunchConfigHandle& config, ActTypes&&... args) {
    void *ptr_args[] = { &args... };                 // 把所有 kernel 参数地址打包
    return lazy_cuLaunchKernelEx(&config, kernel, ptr_args, nullptr);
}
```

变长模板 `ActTypes...` 让每个 kernel 只需在自己的 `launch_impl` 里列出**它专属的参数**（如 TMA 描述符、m/n/k），而不必关心参数如何打包进 `void*[]`——这部分由 `launch_kernel` 统一处理。Runtime API 分支对应实现在 [csrc/jit/handle.hpp:75-114](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/handle.hpp#L75-L114)，字段名从 `gridDimX` 变成 `gridDim`、从 `value.clusterDim` 变成 `val.clusterDim`，但逻辑完全对称。

#### 4.2.4 代码实践

**实践目标**：亲手把「六样东西」与源码逐行对应，并理解 `static attrs` 这个坑。

**操作步骤**：

1. 在 [csrc/jit/handle.hpp:196-210](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/handle.hpp#L196-L210) 分别找到 cluster 与 PDL 两个 `if` 分支，记下它们写入的 `id` 常量名。
2. 假设把 `static LaunchAttrHandle attrs[2];` 改成非 `static` 的局部变量，用你自己的话描述：`construct_launch_config` 返回后，`config.attrs` 会指向什么？调用 `cuLaunchKernelEx` 会发生什么？
3. 数一数：DeepGEMM 最多会同时启用几个启动属性？`attrs[2]` 的容量是否刚好？

**需要观察的现象 / 预期结果**：

- cluster 用 `CU_LAUNCH_ATTRIBUTE_CLUSTER_DIMENSION`，PDL 用 `CU_LAUNCH_ATTRIBUTE_PROGRAMMATIC_STREAM_SERIALIZATION`；
- 去掉 `static` 后 `config.attrs` 成为**悬空指针**（指向已销毁的栈帧），`cuLaunchKernelEx` 行为未定义（多半启动失败或读到垃圾属性）；
- 最多 2 个（cluster + PDL），`attrs[2]` 恰好够用。

> 若有环境，设 `DG_JIT_DEBUG=1` 跑一次 GEMM，可在 [csrc/jit/kernel_runtime.hpp:156-159](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L156-L159) 的打印里看到 `cluster: X, pdl: Y`，与上面的属性一一对应；否则标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么抬高共享内存上限用的是 `cuFuncSetAttribute`（作用于 kernel），而不是放在 `CUlaunchConfig` 里？

**参考答案**：最大动态共享内存是 **kernel 自身的属性**（该 kernel 允许用多少 smem），所以用 `cuFuncSetAttribute` 持久地设置到 kernel 上；而 `CUlaunchConfig` 描述的是**某次具体启动**用多少 smem（`sharedMemBytes`）。两者配合：先用 `cuFuncSetAttribute` 把上限抬到 `smem_size`，本次启动再声明实际要用 `smem_size` 字节。

**练习 2**：`launch_kernel` 里 `void *ptr_args[] = { &args... };` 为什么传的是**地址**而不是值？

**参考答案**：CUDA 启动 API 要求参数以「指向各参数的指针数组」形式传入（即 `void**`），driver 在启动时按 kernel 的参数布局从这些地址读取实参。这是 `cuLaunchKernelEx` 的固定约定。

---

### 4.3 LaunchRuntime 基类

#### 4.3.1 概念说明

DeepGEMM 有十几个不同的 kernel Runtime 类（`SM90FP8Gemm1D1DRuntime`、`SM100FP8FP4Gemm1D1DRuntime`、`SM100MQALogitsRuntime`、`SMxxLayoutRuntime`……）。它们的「**生成源码 → 编译缓存 → 组装配置 → 启动**」骨架完全相同，**只有两处不同**：

1. **生成的 `.cu` 源码不同**（不同的模板参数、不同的 include）；
2. **传给 kernel 的实参不同**（GEMM 传 TMA 描述符 + m/n/k，layout kernel 传 sf/out 等）。

`LaunchRuntime<Derived>`（CRTP 基类）把相同的骨架抽出来，把这两处差异留给子类用两个钩子实现：

- `static std::string generate_impl(const Args& args)` → 子类返回自己的 `.cu` 源码；
- `static void launch_impl(const KernelHandle&, const LaunchConfigHandle&, Args)` → 子类列出自己的实参并调 `launch_kernel`。

基类则提供两个公共入口：

- `generate(args)`：调 `Derived::generate_impl`，再**注入 include 哈希注释**（u3-l3 讲过，使头文件改动触发重编译）；
- `launch(kernel_runtime, args)`：取 kernel 句柄、取 stream、**用全局开关覆盖 `enable_pdl`**、组装 grid/block、调 `construct_launch_config`、最后调 `Derived::launch_impl`。

CRTP 的好处是：基类在编译期就知道子类类型，调用 `Derived::generate_impl` 是**静态绑定**，零虚函数开销——这对热路径上的 kernel 启动很重要。

#### 4.3.2 核心流程

以 `sm90_fp8_gemm_1d1d` 为例，一次完整的「构造到启动」链路如下：

```text
宿主函数 sm90_fp8_gemm_1d1d(a, sfa, b, sfb, c, d, m, n, k, ...)
  ├─ 构造 desc (GemmDesc), config = get_best_config(desc)       # 启发式（第 5 单元）
  ├─ 构造 TMA 描述符 (tensor_map_a/b/sfa/sfb/cd)                # u4-l2
  ├─ 构造 args，其中
  │     launch_args = LaunchArgs(num_sms, num_threads, smem_size, cluster_size)
  │                                                            # cluster_size 来自 config
  ├─ code  = SM90FP8Gemm1D1DRuntime::generate(args)            # → 基类 generate
  │           └─ Derived::generate_impl(args) + include 哈希
  ├─ runtime = compiler->build("sm90_fp8_gemm_1d1d", code)     # 缓存/编译/加载 → KernelRuntime
  └─ SM90FP8Gemm1D1DRuntime::launch(runtime, args)             # → 基类 launch
        ├─ kernel = runtime->kernel
        ├─ stream = at::cuda::getCurrentCUDAStream()
        ├─ launch_args.enable_pdl = device_runtime->get_pdl()  # 全局覆盖！
        ├─ grid/block dim3 组装
        ├─ config = construct_launch_config(kernel, stream, smem, grid, block, cluster_dim, enable_pdl)
        └─ Derived::launch_impl(kernel, config, args)
              └─ launch_kernel(kernel, config, <具体实参>...)
                    └─ cuLaunchKernelEx(...)
```

这条链路里有两个「属性由谁决定」的关键结论（也是本讲综合实践的答案要点）：

- **`cluster_dim` 由启发式配置决定**：它在构造 `LaunchArgs` 时由 `config.layout.get_cluster_size()` 写入（见 [csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp:127-129](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L127-L129)），反映 layout 选择的簇尺寸（如 SM100 的 2-CTA cluster）。
- **`enable_pdl` 由全局开关 `device_runtime->get_pdl()` 决定**：尽管 `LaunchArgs` 构造函数把 `enable_pdl` 默认成 `true`，但基类 `launch` 在 [csrc/jit/kernel_runtime.hpp:146](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L146) **无条件用全局值覆盖**它。而全局开关默认是 `false`（见 [csrc/jit/device_runtime.hpp:16](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/device_runtime.hpp#L16) 与 `set_pdl`/`get_pdl` [csrc/jit/device_runtime.hpp:127-133](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/device_runtime.hpp#L127-L133)）。所以**PDL 默认是关的**，除非用户调 `deep_gemm.set_pdl(True)`——`LaunchArgs` 的那个 `true` 默认值在 launch 路径上其实是被覆盖的。

#### 4.3.3 源码精读

先看 `LaunchArgs` 这个「启动参数包」（[csrc/jit/kernel_runtime.hpp:14-26](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L14-L26)），它就是把 grid/block/smem/cluster/pdl 打包成值类型，便于随 `Args` 一路传递：

```cpp
struct LaunchArgs {
    std::pair<int, int> grid_dim;
    int num_threads;
    int smem_size;
    int cluster_dim;
    bool enable_pdl;

    LaunchArgs(const int& grid_dim_x, const int& num_threads,
               const int& smem_size = 0, const int& cluster_dim = 1,
               const bool& enable_pdl = true):
        grid_dim({grid_dim_x, 1}), num_threads(num_threads), smem_size(smem_size),
        cluster_dim(cluster_dim), enable_pdl(enable_pdl) {}
    // ...另一个接受 pair 的重载...
};
```

接着是 CRTP 基类的两个公共方法。`generate`（[csrc/jit/kernel_runtime.hpp:122-136](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L122-L136)）：

```cpp
template <typename Args>
static std::string generate(const Args& args) {
    auto code = Derived::generate_impl(args);          // 子类生成 .cu 源码
    // include 哈希只算一次（约定 includes 不变）
    static std::string include_hash;
    if (include_hash.empty())
        include_hash = include_parser->get_hash_value(code);
    code = fmt::format("// Includes' hash value: {}\n{}", include_hash, code);
    return code;
}
```

注意 `static std::string include_hash` 是**每个 `LaunchRuntime<Derived>` 实例化各有一份**（因为 CRTP 让每个子类对应不同的基类实例化），所以 include 哈希按 kernel 类型分别缓存，互不干扰。

`launch`（[csrc/jit/kernel_runtime.hpp:138-162](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L138-L162)）是本模块的核心：

```cpp
template <typename Args>
static void launch(const std::shared_ptr<KernelRuntime>& kernel_runtime, const Args& args) {
    const auto kernel = kernel_runtime->kernel;                 // ① 取句柄（模块 4.1 的产物）
    const auto stream = at::cuda::getCurrentCUDAStream();       // ② 取当前 stream
    LaunchArgs launch_args = args.launch_args;

    // Allow runtime override from Python. NOTES: the default is enabled.
    launch_args.enable_pdl = device_runtime->get_pdl();         // ③ 全局 PDL 覆盖

    const dim3 grid_dim  = {static_cast<unsigned>(launch_args.grid_dim.first),
                            static_cast<unsigned>(launch_args.grid_dim.second), 1};
    const dim3 block_dim = {static_cast<unsigned>(launch_args.num_threads), 1, 1};
    auto config = construct_launch_config(kernel, stream, launch_args.smem_size,
                                          grid_dim, block_dim,
                                          launch_args.cluster_dim, launch_args.enable_pdl);  // ④ 模块 4.2

    if (get_env<int>("DG_JIT_DEBUG")) { /* 打印 grid/block/smem/cluster/pdl/stream */ }
    Derived::launch_impl(kernel, config, args);                 // ⑤ 子类列实参 → launch_kernel
}
```

可以看到基类把「① 取句柄、② 取 stream、③ 覆盖 PDL、④ 组装 config」全部固化，只把「⑤ 传哪些实参」留给子类。子类的 `launch_impl` 因此极薄，例如（[csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp:66-75](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L66-L75)）：

```cpp
static void launch_impl(const KernelHandle& kernel, const LaunchConfigHandle& config, Args args) {
    DG_CUDA_UNIFIED_CHECK(launch_kernel(kernel, config,
        args.gmem_a_ptr, args.gmem_b_ptr, args.grouped_layout, args.tensor_map_buffer,
        args.gemm_desc.m, args.gemm_desc.n, args.gemm_desc.k,
        args.tensor_map_a_base, args.tensor_map_b_base,
        args.tensor_map_sfa, args.tensor_map_sfb, args.tensor_map_cd));
}
```

最后，整个 `generate → build → launch` 三连的触发点就在宿主函数末尾（[csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp:140-143](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L140-L143)）：

```cpp
const auto code    = SM90FP8Gemm1D1DRuntime::generate(args);
const auto runtime = compiler->build("sm90_fp8_gemm_1d1d", code);
SM90FP8Gemm1D1DRuntime::launch(runtime, args);
```

这三行就是「宿主拿到 config 后到 GPU 开始计算」的全部胶水，`build` 内部完成「查缓存→编译→加载 cubin→返回 KernelRuntime」（u3-l1、u3-l3），`launch` 内部完成「取句柄→组装配置→启动」。

#### 4.3.4 代码实践

**实践目标**：用本模块的结论解释一个反直觉现象——为什么 `LaunchArgs` 默认 `enable_pdl = true`，但 PDL 实际默认是关的？

**操作步骤**：

1. 在 [csrc/jit/kernel_runtime.hpp:21-22](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L21-L22) 确认 `LaunchArgs` 构造默认 `enable_pdl = true`。
2. 在 [csrc/jit/kernel_runtime.hpp:146](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L146) 找到那行 `launch_args.enable_pdl = device_runtime->get_pdl();`，确认它**无条件执行**（没有 `if`）。
3. 在 [csrc/jit/device_runtime.hpp:16](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/device_runtime.hpp#L16) 确认 `bool enable_pdl = false;`。

**需要观察的现象 / 预期结果**：你应该能得出——launch 路径上 `enable_pdl` 的**唯一真相来源**是全局 `device_runtime->get_pdl()`，`LaunchArgs` 里的默认 `true` 在这里被覆盖，因此 PDL 默认关闭。要打开它，必须从 Python 调 `deep_gemm.set_pdl(True)`（经 [csrc/apis/runtime.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/runtime.hpp) 转发到 `device_runtime->set_pdl`）。

> 若有环境，对比 `deep_gemm.set_pdl(False)` 与 `True` 两次运行，设 `DG_JIT_DEBUG=1` 观察启动打印里 `pdl:` 字段的变化（[csrc/jit/kernel_runtime.hpp:156-159](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L156-L159)）；否则标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `include_hash` 用 `static std::string` 而不是普通局部变量？

**参考答案**：include 哈希的计算依赖递归解析所有 `<deep_gemm/*>` 头文件（u3-l3），开销不小，而同一类型 kernel 的 includes 是约定不变的，只需算一次。`static` 让它在首次 `generate` 后常驻，后续调用直接复用。又因 CRTP，每个子类对应不同的 `LaunchRuntime<Derived>` 实例化，各自有一份独立的 `static`，互不影响。

**练习 2**：基类 `launch` 的 5 个步骤里，哪一步是「子类特有、必须由子类实现」的？

**参考答案**：第 ⑤ 步 `Derived::launch_impl`。前四步（取句柄、取 stream、覆盖 PDL、组装 config）对所有 kernel 都一样，只有「传哪些实参给 kernel」因 kernel 而异，所以由子类的 `launch_impl` 列出。

---

## 5. 综合实践

把三个模块串起来，完成本讲规格里要求的核心任务：**梳理一次内核启动从 `KernelRuntime` 构造到 `cuLaunchKernelEx` 的完整步骤，并说明 `cluster_dim` 与 `enable_pdl` 两个启动属性分别由谁决定。**

**任务**：以 `sm90_fp8_gemm_1d1d` 为例，画出一张「时间线」调用图，标注每一步对应的源码位置与所在模块。

**参考步骤（按时间顺序）**：

1. **触发**：用户在 Python 调 `deep_gemm.fp8_gemm_nt(...)` → 经 u2-l3 的派发链路进入宿主函数 `sm90_fp8_gemm_1d1d`（[csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp:78](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L78)）。
2. **生成源码**：`SM90FP8Gemm1D1DRuntime::generate(args)` → 基类调子类 `generate_impl` + 注入 include 哈希（模块 4.3，[kernel_runtime.hpp:122-136](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L122-L136)）。
3. **编译 + 构造 KernelRuntime**：`compiler->build(...)` 查缓存，未命中则编译成 `kernel.cubin`、原子重命名上线，最后 `KernelRuntimeCache::get` 就地 `make_shared<KernelRuntime>(dir_path)`——其构造函数完成「加载 cubin + 枚举符号 + 得到 `KernelHandle`」（模块 4.1，[kernel_runtime.hpp:35-90](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L35-L90)、[cache.hpp:18-26](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/cache.hpp#L18-L26)）。
4. **组装启动配置**：`SM90FP8Gemm1D1DRuntime::launch(runtime, args)` → 基类取 kernel 句柄、取 stream、**用全局 `get_pdl()` 覆盖 `enable_pdl`**、组装 grid/block、调 `construct_launch_config`（含 cluster/PDL 属性 + 抬高 smem 上限）（模块 4.2 + 4.3，[kernel_runtime.hpp:138-162](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L138-L162)、[handle.hpp:174-213](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/handle.hpp#L174-L213)）。
5. **启动**：`Derived::launch_impl` 列出实参 → `launch_kernel` 打包 `void*[]` → `cuLaunchKernelEx` 真正把 kernel 推上 GPU（[sm90_fp8_gemm_1d1d.hpp:66-75](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L66-L75)、[handle.hpp:215-219](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/handle.hpp#L215-L219)）。

**两个属性由谁决定（必答）**：

- **`cluster_dim`**：由**启发式配置**决定。它在构造 `LaunchArgs` 时由 `config.layout.get_cluster_size()` 写入（[sm90_fp8_gemm_1d1d.hpp:127-129](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L127-L129)），即第 5 单元讲的 layout heuristics 选出的簇尺寸（SM90 多为 1，SM100 2-CTA UMMA 为 2）。基类 `launch` **不修改**它，原样传给 `construct_launch_config`。
- **`enable_pdl`**：由**全局开关 `device_runtime->get_pdl()`** 决定。基类 `launch` 在 [kernel_runtime.hpp:146](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L146) **无条件覆盖** `LaunchArgs` 里携带的值；该开关默认 `false`（[device_runtime.hpp:16](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/device_runtime.hpp#L16)），需用户调 `deep_gemm.set_pdl(True)` 才打开。

> 若有 SM90/SM100 环境，强烈建议设 `DG_JIT_DEBUG=1` 跑一次 `fp8_gemm_nt`，把控制台依次出现的 `Generated kernel code` / `Loading CUBIN` / `Load time` / `Launch kernel with {...} x ..., cluster: X, pdl: Y` 与上面的步骤 2→5 逐行对齐；否则标注「待本地验证」。

## 6. 本讲小结

- **cubin → 句柄**：`KernelRuntime` 用 `load_kernel` 把 `kernel.cubin` 加载成 `LibraryHandle` + `KernelHandle`；默认走 Driver API，靠 `cuLibraryLoadFromFile` + `cuLibraryEnumerateKernels` 枚举并断言「恰好一个 kernel」，从而**无需知道 mangled name**；旧驱动回退到 `cuobjdump -symbols` 按名查找。
- **懒加载 driver 符号**：`lazy_cu*` 用 `dlopen`/`dlsym` 首次调用时才解析符号，使 `_C` 扩展不硬绑定具体 driver 版本，新版 API（如 `cuLibraryEnumerateKernels`，12.4+）也能优雅共存。
- **LaunchConfig = 常规四件套 + 属性数组**：`construct_launch_config` 填 grid/block/smem/stream，先 `cuFuncSetAttribute` 抬高共享内存上限，再按需挂 cluster、PDL 两个属性；注意 `attrs` 必须 `static` 以免悬空指针。
- **cluster 与 PDL 的归属不同**：`cluster_dim` 来自启发式 layout 配置（构造 `LaunchArgs` 时写入），`enable_pdl` 来自全局 `device_runtime->get_pdl()`（基类 `launch` 无条件覆盖），两者分别由「配置层」与「全局运行时旋钮」决定。
- **CRTP 统一流程**：`LaunchRuntime<Derived>` 用 `generate`/`launch` 公共方法 + `generate_impl`/`launch_impl` 钩子，把十几个 kernel 的「生成→编译→组装→启动」骨架抽到基类，子类只写「生成什么源码」「传哪些实参」两件事，零虚函数开销。
- **端到端三行胶水**：宿主函数末尾 `generate → build → launch` 三行，就是「拿到 config 后到 GPU 开始计算」的全部——`build` 负责缓存/编译/加载（u3 单元），`launch` 负责取句柄/组装/启动（本讲）。

## 7. 下一步学习建议

本讲打通了「宿主侧最后一公里」，此后可以沿两个方向继续：

- **向上**：本讲多次提到 `get_best_config`、`config.layout.get_cluster_size()`、`GemmConfig`，这正是**第 5 单元（启发式与配置选择）**的主题——建议接着读 u5-l1「GemmDesc 与配置数据结构」、u5-l2「布局候选与最优配置选择」，理解 `cluster_dim` 等 `LaunchArgs` 字段究竟是怎么被选出来的。
- **向下**：`launch_impl` 把控制权交给设备 kernel 后，进入**第 6 单元（设备内核内部机制）**。建议读 u6-l1「内核入口：SM90 FP8 GEMM 1D1D」，看 `__launch_bounds__`、共享内存划分、TMA/math 线程分工如何消费本讲组装好的 grid/block/smem 配置；以及 u6-l3「PTX 内联函数：TMA 加载与栅栏」，理解 PDL 与 cluster 在设备侧的同步原语（`mbarrier`、`cluster_sync`）是如何配合的。

此外，若你对「同一份代码同时支撑 Driver/Runtime 两套 API」的工程手法感兴趣，可重读 [csrc/jit/handle.hpp:48-220](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/handle.hpp#L48-L220) 的 `#if/#else` 全貌，这是一种可复用的「双后端抽象」写法。
