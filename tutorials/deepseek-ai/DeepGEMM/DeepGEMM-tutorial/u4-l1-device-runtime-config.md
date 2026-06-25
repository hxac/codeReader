# DeviceRuntime 与运行时配置

## 1. 本讲目标

在前几讲里，我们反复看到一个 `device_runtime->get_arch_major()` 的调用——它像一个总开关，决定一次 GEMM 走 SM90 还是 SM100。本讲就把这个「开关」背后的对象彻底拆开。

学完本讲，你应该能够：

- 说清 `DeviceRuntime` 这个**单例对象**承担了哪些职责：缓存设备属性、持有 cuBLASLt 资源、保存三个全局旋钮。
- 理解三个运行时旋钮 `num_sms` / `tc_util` / `pdl` 的语义，以及它们各自如何流到下游影响 GPU kernel 的启动与计算。
- 解释 `get_arch_major()` 返回 `9` 或 `10` 为什么能成为全库派发的核心开关，并看懂 `get_arch()` 拼出的 `90a` / `100a` / `100f` 这样的架构字符串是怎么来的。
- 能够用 `deep_gemm.set_num_sms / set_tc_util / set_pdl` 改变全局配置，并用 bench 工具观察到性能变化。

## 2. 前置知识

本讲是宿主侧（CPU 侧）的源码精读，不涉及 GPU 代码细节，但需要你先接受几个概念。

- **宿主（host）vs 设备（device）**：CPU 上跑的调度、校验、编译代码叫宿主代码；GPU 上跑的张量核计算叫设备代码。`DeviceRuntime` 是一个**宿主侧对象**，它本身不计算，只负责「告诉设备该怎么算」。
- **单例（singleton）**：整个进程里只存在一个实例的对象。`DeviceRuntime` 就是单例——设备属性、cuBLASLt 句柄、全局旋钮，全进程共享一份。
- **懒初始化（lazy init）**：对象在「第一次被使用」时才真正构造，而不是程序启动时就构造。DeepGEMM 用一个 `LazyInit` 模板实现它。
- **CUDA 的 `cudaDeviceProp`**：CUDA Runtime 提供的一个结构体，描述当前 GPU 的硬件属性，比如 SM 数量（`multiProcessorCount`）、L2 缓存大小（`l2CacheSize`）、计算能力（`major.minor`，如 `9.0`、`10.0`）等。查询它要调用 CUDA API，有一定开销，所以适合缓存。
- **cuBLASLt**：NVIDIA 官方的高性能矩阵乘库。DeepGEMM 把它当作**参考基准（baseline）**，用来和自己 JIT 出来的 kernel 比 TFLOPS。跑 cuBLASLt 需要一个「句柄（handle）」和一块「工作区（workspace）」显存。
- **arch_major（架构主版本号）**：`cudaDeviceProp.major` 的值。Hopper 是 `9`（SM90），Blackwell 是 `10`（SM100/SM101）。这是 DeepGEMM 区分两代架构的依据。

如果你对 `get_arch_major()` 在派发链路里的位置还不熟，建议先回看 u2-l3（C++ 绑定与 API 派发层）。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `csrc/jit/device_runtime.hpp` | `DeviceRuntime` 类本体 | 全部旋钮、设备属性缓存、arch 判定、cuBLASLt 资源 |
| `csrc/apis/runtime.hpp` | Python↔C++ 绑定（pybind11） | `set_*` / `get_*` 旋钮如何暴露给 Python |
| `csrc/utils/lazy_init.hpp` | 懒初始化模板 | `device_runtime` 单例的构造时机 |
| `csrc/jit_kernels/heuristics/config.hpp` | `GemmDesc` 数据结构 | `num_sms` / `tc_util` 如何进入一次 GEMM 的描述 |
| `csrc/jit/kernel_runtime.hpp` | 内核启动基类 | `enable_pdl` 在启动时如何被读取 |
| `csrc/jit/handle.hpp` | 启动配置构造 | PDL 属性如何写进 `cuLaunchKernelEx` |
| `csrc/jit_kernels/impls/smxx_cublaslt.hpp` | cuBLASLt 参考实现 | handle/workspace/num_sms 如何喂给 cuBLASLt |

一句话定位：`DeviceRuntime` 是 DeepGEMM 宿主侧的「**全局运行时上下文**」——它把「这块卡是谁、有什么属性、用户想用多少资源」这些信息集中在一处，供派发层、JIT 编译器、启发式选配置、内核启动四个环节随时取用。

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：**设备属性缓存**、**运行时旋钮**、**架构判定**。其中 cuBLASLt 资源管理与设备属性缓存同属「单例持有的资源」，放在第一个模块讲。

### 4.1 设备属性缓存与单例资源

#### 4.1.1 概念说明

`DeviceRuntime` 是一个**进程级单例**。它解决的问题是：DeepGEMM 的很多环节都需要「当前 GPU 是什么卡」这个信息——派发层要知道架构、JIT 编译器要拼架构后缀、启发式要知道有多少 SM、cuBLASLt 参考实现要句柄和工作区。如果每个环节都各自去查 CUDA、各自创建 cuBLASLt 句柄，既浪费（重复查询、重复创建）又容易不一致。

所以 DeepGEMM 把这些都收拢进一个对象：

- **缓存** `cudaDeviceProp`：只查一次 CUDA API，之后全程复用。
- **持有** cuBLASLt 句柄与工作区：要么自己创建（默认），要么复用 PyTorch 管理的句柄（按需）。
- **保存**三个全局旋钮 `num_sms` / `tc_util` / `enable_pdl`（见 4.2）。

并且它用**懒初始化**：进程启动时不构造，等第一次有人通过 `device_runtime->` 访问它时才构造。

#### 4.1.2 核心流程

```text
进程启动
   │
   │  （此时 DeviceRuntime 尚未构造，只是登记了一个工厂函数）
   ▼
某处第一次写 device_runtime->xxx()
   │
   │  LazyInit::operator->() 发现内部指针为空
   │  → 调用工厂函数 std::make_shared<DeviceRuntime>()
   │      → 构造函数里：读环境变量决定 cuBLASLt 来源
   │                     创建 cuBLASLt handle（或不用）
   │                     分配 32MB workspace（或不用）
   ▼
之后所有 device_runtime-> 访问都拿到同一个 shared_ptr
```

构造完成后，`get_prop()` 第一次被调用时会查询并缓存设备属性；之后所有取属性的操作（架构、SM 数、L2 大小）都直接读缓存，零 CUDA 调用。

#### 4.1.3 源码精读

先看单例本身是怎么声明的。它是一个**文件级静态变量**，用 `LazyInit` 模板包住一个工厂函数：

[csrc/jit/device_runtime.hpp:136-136](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/device_runtime.hpp#L136-L136) —— `static auto device_runtime = LazyInit<DeviceRuntime>([](){ ... });`。注意它不带参数、没有显式初始化调用，全靠「第一次解引用」触发构造。

懒初始化的全部魔法就在 `operator->` 里：

[csrc/utils/lazy_init.hpp:16-20](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/lazy_init.hpp#L16-L20) —— 指针为空就调 `factory()` 造一个 `shared_ptr`，否则直接返回。这样 `device_runtime->get_arch_major()` 这种写法看起来像在用普通指针，实际上第一次访问才真正构造对象。

再看 `DeviceRuntime` 缓存设备属性的关键方法：

[csrc/jit/device_runtime.hpp:72-81](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/device_runtime.hpp#L72-L81) —— `get_prop()` 用一个 `std::shared_ptr<cudaDeviceProp> cached_prop` 做缓存：为空时调 `cudaGetDevice` + `cudaGetDeviceProperties` 查一次并存下，之后直接返回。这就是「查一次、用全程」的实现。

cuBLASLt 资源则在**构造函数**里就准备好。这里有两个环境变量开关，值得细看：

[csrc/jit/device_runtime.hpp:29-49](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/device_runtime.hpp#L29-L49) —— 构造逻辑：

- `DG_USE_PYTORCH_CUBLASLT_HANDLE`：默认关。开启则改用 `at::cuda::getCurrentCUDABlasLtHandle()` 复用 PyTorch 的句柄；默认情况下 DeepGEMM **自己 `cublasLtCreate`** 一个，因为注释里说 PyTorch 那条路径在某些版本 CPU 开销很大。
- `DG_USE_TEMP_CUBLASLT_WORKSPACE`：默认关。开启则每次调用现分配一块 workspace，而不是常驻一块。这是为 `compute-sanitizer` 测试准备的——常驻 tensor 在 CUDA driver 关闭后析构会触发 `cudaErrorCudartUnloading`，现分配现释放可以规避（见 u10-l4 sanitizer 测试）。
- 默认路径下：自建 handle + 预分配一块 `kCublasLtWorkspaceSize = 32MB` 的 `torch::Tensor`。

工作区大小是个编译期常量：

[csrc/jit/device_runtime.hpp:20-20](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/device_runtime.hpp#L20-L20) —— `static constexpr size_t kCublasLtWorkspaceSize = 32 * 1024 * 1024;`，即 32MB。

cuBLASLt 参考实现取资源时就走这两个 getter：

[csrc/jit_kernels/impls/smxx_cublaslt.hpp:60-61](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/smxx_cublaslt.hpp#L60-L61) —— `get_cublaslt_handle()` 与 `get_cublaslt_workspace()` 把句柄和工作区交给 cuBLASLt 调用。

注意 `device_runtime` 是单例、句柄和 workspace 是它的成员，所以**析构时机由单例生命周期决定**（通常是进程退出）。`~DeviceRuntime` 会 `cublasLtDestroy` 掉自建的句柄：

[csrc/jit/device_runtime.hpp:51-54](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/device_runtime.hpp#L51-L54) —— 析构时若是自己管的 handle 就销毁。

#### 4.1.4 代码实践

**实践目标**：确认 `DeviceRuntime` 是懒初始化的，且设备属性只查一次。

**操作步骤**（源码阅读型实践）：

1. 在 `csrc/jit/device_runtime.hpp` 的 `get_prop()` 第 77 行（`cudaGetDeviceProperties` 之后）临时想象加一条 `printf("Querying device prop once\n");`（**注意：本讲义不修改源码，你可以在自己 fork 上试验**）。
2. 或者更简单：开启 `DG_JIT_DEBUG=1` 运行一次 GEMM，在启动 kernel 时观察打印里出现的 grid 维度与 SM 数。
3. 跟踪 `LazyInit::operator->` 被首次调用的位置——它一定发生在「第一次派发需要 `get_arch_major()`」的时刻（即第一次真正调用某个 GEMM API 时），而非 `import deep_gemm` 时。

**需要观察的现象**：

- `import deep_gemm` 成功并不代表 `DeviceRuntime` 已构造（构造发生在第一次 kernel 派发）。
- 多次调用不同形状的 GEMM，「查询设备属性」只会发生一次。

**预期结果**：单例 + 缓存使得设备属性查询与 cuBLASLt 句柄创建各只发生一次。若你不在真机上，这一条「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `DeviceRuntime` 要缓存 `cudaDeviceProp`，而不是每次需要 `multiProcessorCount` 时都现查？
**答案**：`cudaGetDeviceProperties` 是一次 CUDA Runtime API 调用，有一定 CPU 开销；而设备属性在一次进程里不变。缓存后，频繁读取 SM 数、架构号、L2 大小的开销从「一次 CUDA 调用」降为「一次内存读」。

**练习 2**：默认情况下 DeepGEMM 自己创建 cuBLASLt 句柄，而不是用 PyTorch 的。源码注释给出的理由是什么？
**答案**：`at::cuda::getCurrentCUDABlasLtHandle` 在某些 PyTorch 版本上 CPU 开销很大，所以默认自建（由 `DG_USE_PYTORCH_CUBLASLT_HANDLE=0` 控制）。

### 4.2 运行时旋钮：num_sms / tc_util / pdl

#### 4.2.1 概念说明

`DeviceRuntime` 暴露三个**全局旋钮（knob）**，让用户在不改 kernel 代码的前提下调节「这块卡用多少资源」：

- **`num_sms`（SM 数量上限）**：限制 kernel 最多使用多少个 SM。默认 `0` 表示「用满全部 SM」。GPU 上的 SM 数是物理固定的（如 Hopper 132 个、Blackwell 148 个），但有时你想留一部分 SM 给别的任务（比如通信 kernel），就可以调小它。
- **`tc_util`（tensor core 利用率的近似比例）**：一个 `0~100` 的整数，告诉启发式「我期望这次计算的张量核利用率大概是多少」。默认 `0` 表示 `100`。它会被烤进设备 kernel 当作一个编译期常量，影响 kernel 内部的工作划分，从而近似达到目标利用率。
- **`pdl`（Programmatic Dependent Launch）**：一个布尔开关。PDL 是 Hopper 起支持的硬件特性，允许「依赖前一个 kernel 的 kernel」在前者还没完全结束时就开始跑 prologue（比如加载下一批数据），从而重叠两段 kernel 的尾巴与开头，减少流水线空泡。默认关。

这三个旋钮都是**进程级全局状态**：设一次，影响之后所有 kernel，直到再次设置。

#### 4.2.2 核心流程

三个旋钮的「流到下游」路径各不相同：

```text
num_sms:
  device_runtime.get_num_sms()
    ├─→ GemmDesc.num_sms  → 启发式计算 grid/num_waves（用多少 wave 跑完所有 block）
    └─→ cuBLASLt 的 SM_COUNT_TARGET 属性（参考实现也受限）

tc_util:
  device_runtime.get_tc_util()
    └─→ GemmDesc.tc_util → 进入代码生成的编译期常量 → 烤进设备 kernel 模板

pdl:
  device_runtime.get_pdl()
    └─→ LaunchArgs.enable_pdl（启动前覆盖默认值）
        └─→ construct_launch_config 写入 CU_LAUNCH_ATTRIBUTE_PROGRAMMATIC_STREAM_SERIALIZATION
```

注意一个关键设计：`get_num_sms()` 和 `get_tc_util()` 都把 `0` 当作「未设置 → 用默认值」，这是 DeepGEMM 统一的「0 表示默认」约定（与 u3-l2 的 `compiled_dims` 同源）。

#### 4.2.3 源码精读

先看三个旋钮在 `DeviceRuntime` 里的 get/set 全貌。成员变量只有三个：

[csrc/jit/device_runtime.hpp:15-16](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/device_runtime.hpp#L15-L16) —— `int num_sms = 0, tc_util = 0; bool enable_pdl = false;`。初值就是「默认值」。

`num_sms` 的 set 带合法性校验，get 有「默认填满」逻辑：

[csrc/jit/device_runtime.hpp:103-112](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/device_runtime.hpp#L103-L112) —— `set_num_sms` 断言 `0 <= 新值 <= multiProcessorCount`（不能超过物理上限）；`get_num_sms` 在 `num_sms == 0` 时回退到 `get_prop()->multiProcessorCount`，即「没设就用满」。

`tc_util` 类似，区间是 `0~100`：

[csrc/jit/device_runtime.hpp:118-125](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/device_runtime.hpp#L118-L125) —— `set_tc_util` 断言 `0 <= 新值 <= 100`；`get_tc_util` 在 `tc_util == 0` 时返回 `100`。

`pdl` 最简单，纯存取：

[csrc/jit/device_runtime.hpp:127-133](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/device_runtime.hpp#L127-L133) —— `set_pdl` / `get_pdl`，初值 `false`。

**下游之一：旋钮进入 `GemmDesc`**。各 Runtime 类在构造 `GemmDesc` 时，把 `num_sms` / `tc_util` 填进去：

[csrc/jit_kernels/heuristics/config.hpp:22-22](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/config.hpp#L22-L22) —— `int num_sms, tc_util;` 是 `GemmDesc` 的字段。例如 SM90 BF16 的 Runtime 直接写 `.num_sms = device_runtime->get_num_sms(), .tc_util = device_runtime->get_tc_util()`（见 `sm90_bf16_gemm.hpp` 等，全库统一模式）。`tc_util` 之后会作为编译期常量进入设备 kernel 模板，`num_sms` 则参与启发式的 wave 数计算。

`check_validity` 还对 `num_sms` 加了一条额外约束：

[csrc/jit_kernels/heuristics/config.hpp:47-47](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/config.hpp#L47-L47) —— `DG_HOST_ASSERT(num_sms % 2 == 0);`。这是因为部分 kernel（如 SM100 的 2-CTA cluster 模型，见 u8-l3）要求 SM 数为偶数，所以无论用户设多少，最终都必须是偶数。

**下游之二：`pdl` 在启动时被读取**。这是三旋钮里唯一在「启动那一刻」才读取的——它甚至会在启动前覆盖 `LaunchArgs` 里的默认值：

[csrc/jit/kernel_runtime.hpp:144-146](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L144-L146) —— `launch_args.enable_pdl = device_runtime->get_pdl();`。注释写明「Allow runtime override from Python. NOTES: the default is enabled.」——即各 Runtime 在构造 `LaunchArgs` 时默认把 PDL 设成开，但这里用全局旋钮覆盖之，让你能在运行时关掉。

`enable_pdl` 最终落到 `cuLaunchKernelEx` 的属性里：

[csrc/jit/handle.hpp:205-210](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/handle.hpp#L205-L210) —— 当 `enable_pdl` 为真时，追加一个 `CU_LAUNCH_ATTRIBUTE_PROGRAMMATIC_STREAM_SERIALIZATION` 属性并设为 1。这就是 PDL 真正打开的硬件开关。

`DG_JIT_DEBUG=1` 时，启动还会把当前旋钮值打印出来，方便你确认配置确实生效：

[csrc/jit/kernel_runtime.hpp:156-159](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L156-L159) —— 打印 grid、shared memory、**cluster**、**pdl**、stream。

**Python 侧的绑定**：这三个旋钮就是 `csrc/apis/runtime.hpp` 里 `register_apis` 暴露的：

[csrc/apis/runtime.hpp:12-29](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/runtime.hpp#L12-L29) —— `set_num_sms`/`get_num_sms`、`set_tc_util`/`get_tc_util`、`set_pdl`/`get_pdl` 六个函数，每个都是一行 lambda 转发给 `device_runtime`。它们在 `deep_gemm/__init__.py` 里被 re-export 到顶层，所以你能直接写 `deep_gemm.set_num_sms(64)`。README 的 Utilities 小节也列了这三个：

[README.md:146-148](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/README.md#L146-L148) —— 官方说明：`set_num_sms` 设最大 SM 数、`set_tc_util` 设近似张量核利用率、`set_pdl` 开关 PDL。

> 说明：`set_pdl` 之所以值得单独列一个旋钮，是因为 PDL 的收益依赖调用者是否真的把「依赖前一个 kernel 的下一个 kernel」紧挨着排在同一 stream 上。DeepGEMM 把它做成全局开关，默认关，让用户在确认自己的调用模式能受益时再开。

#### 4.2.4 代码实践

**实践目标**：用三个旋钮改变同一 GEMM 的执行，观察 TFLOPS 变化并解释原因。

**操作步骤**（需要 SM90/SM100 真机；若无则改为源码阅读型，见末尾）：

1. 参考 `tests/test_fp8_fp4.py` 里的 `generate_normal`，构造一对 FP8 输入 `(a, a_sf)` / `(b, b_sf)` 与输出 `d`（参考 u1-l4）。
2. 写一个测时函数，用 `deep_gemm.testing.bench.bench_kineto` 包裹一次 `deep_gemm.fp8_fp4_gemm_nt(...)` 调用（该工具见 u10-l3）。
3. 分别在三种配置下测同一形状：

   ```python
   # 示例代码（非项目原有）
   import deep_gemm
   from deep_gemm.testing import bench_kineto

   # 配置 A：默认（用满 SM / tc_util=100 / pdl 关）
   t_a = bench_kineto(lambda: deep_gemm.fp8_fp4_gemm_nt((a, a_sf), (b, b_sf), d), "...")

   # 配置 B：只用一半 SM
   deep_gemm.set_num_sms(deep_gemm.get_num_sms() // 2)
   t_b = bench_kineto(lambda: deep_gemm.fp8_fp4_gemm_nt((a, a_sf), (b, b_sf), d), "...")
   deep_gemm.set_num_sms(0)   # 恢复

   # 配置 C：开 PDL
   deep_gemm.set_pdl(True)
   t_c = bench_kineto(lambda: deep_gemm.fp8_fp4_gemm_nt((a, a_sf), (b, b_sf), d), "...")
   deep_gemm.set_pdl(False)   # 恢复
   ```

**需要观察的现象**：

- 配置 B（SM 减半）：理想情况下耗时约翻倍（计算量不变但算力减半）；实际可能因 SM 减少后 wave 数变化而非严格翻倍。
- 配置 C（PDL 开）：单独一个 GEMM 几乎看不到收益——PDL 的价值在「前一个 kernel 还没结束就开始下一个」，单测一次 GEMM 体现不出来。需要连续两次 GEMM 排在同一 stream 才可能观察到尾部/开头重叠。
- `tc_util` 的效果较隐蔽，因为改变它会触发**重新 JIT**（它是编译期常量），建议单独对比。

**预期结果**：`num_sms` 的影响最直观、最可复现；`pdl` 在单次 GEMM 上通常无明显变化。如果你不在真机上，把上面三段配置的预期写成判断即可，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`get_num_sms()` 返回 `0` 时实际会发生什么？为什么用 `0` 而不是用 `multiProcessorCount` 作初值？
**答案**：`get_num_sms()` 在 `num_sms == 0` 时回退为 `get_prop()->multiProcessorCount`（用满）。用 `0` 作「未设置」哨兵，可以区分「用户没设」与「用户设成某个具体值」——这是 DeepGEMM 全库统一的「0 表示默认」约定，`tc_util` 同理（`0` → `100`）。

**练习 2**：为什么 `check_validity` 要求 `num_sms % 2 == 0`？
**答案**：部分 kernel（如 SM100 的 2-CTA cluster 模型）要求 SM 成对使用，SM 数必须是偶数。所以无论物理卡有多少 SM、用户设了多少，进入 `GemmDesc` 的 `num_sms` 都得是偶数，否则断言失败。

### 4.3 架构判定：get_arch 与 get_arch_major

#### 4.3.1 概念说明

架构判定是 `DeviceRuntime` 最重要的职责——它提供的 `get_arch_major()` 是**全库派发的核心开关**。回顾前几讲：u2-l3 里 `fp8_fp4_gemm_nt` 正是靠 `device_runtime->get_arch_major()` 返回 `9` 还是 `10`，决定走 SM90 实现还是 SM100 实现。

这里有两个相关但不同的方法：

- **`get_arch_major()`**：直接返回 `cudaDeviceProp.major`，即 `9`（Hopper/SM90）或 `10`（Blackwell/SM100/SM101）。这是**派发层**用的——它只关心「是第几代」，不关心小版本。
- **`get_arch(number_only, support_arch_family)`**：返回一个**架构字符串**，如 `90a`、`100a`、`100f`、`101a`。这是 **JIT 编译器**用的——nvcc/nvrtc 需要 `-arch=sm_90a` 这样的精确目标，且不同小版本对应不同的架构后缀。

两者的分工很清晰：派发用 `get_arch_major()`（粗粒度），编译用 `get_arch()`（细粒度）。

#### 4.3.2 核心流程

`get_arch_major()` 的逻辑极简：

```text
get_arch_major():
    返回 get_arch_pair().first   # 即 cudaDeviceProp.major
    # SM90 → 9, SM100/SM101 → 10
```

`get_arch()` 的逻辑稍复杂，因为它要区分 SM100（10.0）和 SM101（10.1），并为 SM100 选择家族后缀：

```text
get_arch(number_only, support_arch_family):
    (major, minor) = get_arch_pair()
    if major == 10 and minor != 1:        # SM100 (Blackwell, 10.0)
        if number_only:      return "100"
        if support_arch_family: return "100f"   # 家族后缀
        return "100a"
    else:                                  # SM90(9.0) 或 SM101(10.1)
        return (major*10 + minor) + (number_only ? "" : "a")
        # SM90 → "90a", SM101 → "101a"
```

`get_arch_pair()` 来自缓存的设备属性：

[csrc/jit/device_runtime.hpp:83-86](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/device_runtime.hpp#L83-L86) —— 返回 `{prop->major, prop->minor}`，所以架构判定也吃「设备属性缓存」的红利。

#### 4.3.3 源码精读

先看核心派发开关 `get_arch_major`，一行：

[csrc/jit/device_runtime.hpp:99-101](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/device_runtime.hpp#L99-L101) —— `return get_arch_pair().first;`。就这么简单，但它被全库几十处调用（见 `apis/gemm.hpp`、`apis/attention.hpp`、`apis/einsum.hpp`、`apis/mega.hpp`、`apis/layout.hpp` 等几乎每个 API 开头都有 `const auto arch_major = device_runtime->get_arch_major();`）。

派发层的典型用法（以 `apis/gemm.hpp` 为例）：

[csrc/apis/gemm.hpp:94-94](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L94-L94) —— `const auto arch_major = device_runtime->get_arch_major();`。拿到 `9` 或 `10` 后，API 用它配合变换后 SF 的 dtype 派发到具体 SM90/SM100 实现（详见 u2-l3）。

再看 `get_arch` 的字符串拼接，这是 JIT 编译器拼 `-arch` 的依据：

[csrc/jit/device_runtime.hpp:88-97](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/device_runtime.hpp#L88-L97) —— 关键分支：

- SM100（`major==10 and minor!=1`）：`number_only` 给 `"100"`；`support_arch_family` 给 `"100f"`，否则 `"100a"`。`100f`（family）vs `100a` 的选择取决于编译器版本是否够新——这与 u3-l4 讲的「编译器 ≥ 12.9 才用家族后缀」衔接。
- 其余（SM90、SM101）：`major*10+minor` 拼 `"a"`，即 `"90a"` / `"101a"`。

注意 `get_arch_major()` 把 SM100 和 SM101 都归为 `10`——也就是说，**对派发层而言，SM100 和 SM101 是「同一代」**，都走 SM100 实现路径；它们在小版本上的差异（如 arch 后缀 `100a` vs `101a`）只对 JIT 编译器有意义，不影响派发。

最后，架构判定也驱动着 `HeuristicsRuntime` 里的对齐推导，进一步说明 `get_arch_major()` 是个被多方依赖的总开关：

[csrc/jit_kernels/heuristics/runtime.hpp:47-57](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/runtime.hpp#L47-L57) —— `get_theoretical_mk_alignment_for_contiguous_layout` 用 `device_runtime->get_arch_major() != 10` 判断：只有 SM100（arch_major==10）才走按 `block_m` 推导的精细对齐，否则用 legacy 的 128 对齐。这是「架构判定影响启发式」的又一例证（对齐的细节见 u5-l3）。

#### 4.3.4 代码实践

**实践目标**：确认你手上的卡被判定为哪一代，并理解派发路径。

**操作步骤**：

1. 在 Python 里直接读 PyTorch 的设备属性，对照 `get_arch_major` 的口径：

   ```python
   # 示例代码（非项目原有）
   import torch
   print(torch.cuda.get_device_properties(0))   # 看 major / minor / multiProcessorCount
   ```

2. 在 `DG_JIT_DEBUG=1` 下跑一次 GEMM，观察启动打印里的 arch 后缀（编译器日志会带上 `-arch=sm_XXa` 之类信息）。
3. （源码阅读）在仓库里搜索 `device_runtime->get_arch_major()`，统计它在 `csrc/apis/` 下被调用了多少次，体会它作为「总开关」的覆盖面。

**需要观察的现象**：

- Hopper 卡：`major=9, minor=0`，`get_arch_major()` 返回 `9`，`get_arch()` 返回 `"90a"`，派发走 SM90。
- Blackwell 卡：`major=10`，`get_arch_major()` 返回 `10`，`get_arch()` 返回 `"100a"` 或 `"100f"`，派发走 SM100。

**预期结果**：`get_arch_major()` 的返回值与卡的 `major` 完全一致，并直接决定你跑的是哪一套实现。不在真机上则「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`get_arch_major()` 对 SM100（10.0）和 SM101（10.1）分别返回什么？这意味着派发层会怎么对待它们？
**答案**：都返回 `10`。对派发层而言，SM100 和 SM101 是同一代，都走 SM100 实现路径。两者的小版本差异只在 JIT 编译器拼 arch 字符串时（`100a`/`100f` vs `101a`）才体现，不影响派发。

**练习 2**：为什么 DeepGEMM 要区分 `get_arch_major()` 和 `get_arch()` 两个方法，而不是只用一个？
**答案**：派发层只需要「第几代」这种粗粒度信息（`9` vs `10`），用 `get_arch_major()` 更直观；而 JIT 编译器需要 `-arch=sm_90a` 这种精确目标字符串，且要处理 SM100/SM101 的小版本与家族后缀，用 `get_arch()`。两者粒度不同、消费方不同，分开更清晰。

## 5. 综合实践

把三个模块串起来：写一个「**运行时体检脚本**」，打印当前 `DeviceRuntime` 的全部关键状态，并用旋钮做一次对比实验。

**任务**：

1. 打印设备画像：`get_arch_major()`、`get_arch_pair()`（可借 PyTorch 读）、`get_num_sms()`、`get_l2_cache_size()`、当前 `tc_util` / `pdl`。
2. 选一个中等大小的 FP8 GEMM（如 `M=4096, N=4096, K=4096`），用 `bench_kineto` 测默认配置下的 kernel 耗时，并按 TFLOPS 公式估算：

   \[
   \text{TFLOPS} = \frac{2 \cdot M \cdot N \cdot K}{\text{耗时(s)} \cdot 10^{12}}
   \]

   其中 \(2 M N K\) 是一次 GEMM 的浮点运算次数（乘加各一）。
3. 用 `set_num_sms` 把 SM 砍到一半，重测，对比 TFLOPS 变化，并用本讲「`num_sms` 影响 wave 数」的原理解释。
4. 用 `DG_JIT_DEBUG=1` 重跑，在启动日志里确认 `pdl` 和 `cluster` 的实际取值与你的旋钮设置一致。
5. 实验结束后务必 `set_num_sms(0)`、`set_pdl(False)` 恢复默认，避免污染后续测试。

**验收标准**：

- 能说清自己卡上 `get_arch_major()` 的值，以及它决定了走哪套实现。
- 能观察到 `num_sms` 减半后耗时上升，并能用 wave 数解释。
- 能在 JIT 调试日志里看到 `pdl` 字段随 `set_pdl` 变化。

若无真机，至少完成第 1、5 步的源码阅读部分，并写出第 3 步的**预期**判断（标注「待本地验证」）。

## 6. 本讲小结

- `DeviceRuntime` 是 DeepGEMM 宿主侧的**进程级单例**，用 `LazyInit` 实现**懒初始化**——第一次被访问时才构造，集中持有设备属性缓存、cuBLASLt 资源与三个全局旋钮。
- 设备属性（`cudaDeviceProp`）只查一次后缓存，之后读取 SM 数、L2 大小、架构号都是零 CUDA 调用；cuBLASLt 句柄与 32MB workspace 默认自建，可由环境变量改用 PyTorch 的或临时分配。
- 三个运行时旋钮：`num_sms`（SM 上限，`0`=用满）影响启发式 wave 数与 cuBLASLt 的 `SM_COUNT_TARGET`；`tc_util`（0~100，`0`=100）作为编译期常量烤进设备 kernel；`pdl`（默认关）在启动时覆盖 `LaunchArgs` 并写入 `cuLaunchKernelEx` 的 PDL 属性。
- `get_arch_major()` 返回 `9`（Hopper）或 `10`（Blackwell），是**全库派发的核心开关**，几十处 API 靠它选 SM90/SM100 实现。
- `get_arch()` 返回 `90a`/`100a`/`100f`/`101a` 等架构字符串供 JIT 编译器使用，并区分 SM100/SM101 的小版本与家族后缀。
- 旋钮和架构判定都遵守「0 表示默认」约定，且都通过 `device_runtime` 单例对外暴露，Python 侧经 `csrc/apis/runtime.hpp` 的 pybind 绑定到 `deep_gemm.set_*`/`get_*`。

## 7. 下一步学习建议

本讲讲清了「运行时上下文」里有什么。接下来沿着调用链继续往下：

- **u4-l2 TMA 描述符与 swizzle**：`DeviceRuntime` 决定走哪套实现后，Runtime 类会用 `cuTensorMapEncodeTiled` 构造 TMA 描述符——那一步会用到本讲的架构信息。
- **u4-l3 内核加载与启动句柄**：本讲只讲了 `enable_pdl` 如何进入 `construct_launch_config`，下一讲完整讲 cubin 加载、符号枚举与 `LaunchConfig` 的全貌。
- **u5-l1 GemmDesc 与配置数据结构**：本讲提到 `num_sms`/`tc_util` 进入 `GemmDesc`，下一单元会完整拆解 `GemmDesc` 及其关联的 `Layout`/`LaunchConfig`。
- 如果对 `get_arch()` 的家族后缀 `100f`/`100a` 如何随编译器版本变化感兴趣，回看 **u3-l4（NVCC 与 NVRTC）**，那里讲清了 `get_arch(support_arch_family)` 与 nvcc/nvrtc 版本的衔接。
