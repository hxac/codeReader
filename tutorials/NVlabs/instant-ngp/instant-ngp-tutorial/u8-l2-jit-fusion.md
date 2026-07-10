# JIT 融合与全融合内核

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚「JIT 融合（JIT fusion）」到底把什么和什么「融合」了，以及它为什么能省时间和显存带宽。
- 读懂 `NerfNetwork::generate_device_function` 如何在运行时把「位置编码 + 密度 MLP + 方向编码 + 颜色 MLP」四块积木拼成一个名为 `eval_nerf` 的端到端 CUDA 设备函数。
- 区分 `render_nerf`、`train_nerf`、`trace_sdf` 三类融合内核各自的适用场景与触发条件。
- 知道 `m_jit_fusion` 开关的默认值、它受哪些硬件/软件条件限制，以及它在 GUI、Python、命令行里分别如何被控制。
- 理解「编译失败自动降级」和 `rtc/cache` 缓存机制，能判断 JIT 关闭时程序走哪条后备路径。

本讲属于专家层，前置是 **u3-l3（网络构建与 FullyFusedMLP）** 和 **u4-l3（NeRF 光线步进与体渲染）**：前者告诉你 MLP 由 tiny-cuda-nn 的 `FullyFusedMLP` 实现，本讲说明这些 MLP 如何被进一步「融」进渲染/训练主循环；后者给出了非融合的 `NerfTracer` 两段式渲染路径，本讲正是它的「快路径」对照。

## 2. 前置知识

在进入源码前，先用三个比喻建立直觉。

### 2.1 为什么需要「融合」

一次 NeRF 渲染，对每个像素都要沿光线采样几十上百个点，每个点都要跑一遍完整网络。传统做法是把这件事拆成多个 CUDA kernel 串起来：

1. kernel A：对一批采样点做哈希编码 → 写到显存缓冲 `buf1`。
2. kernel B：读 `buf1` → 跑密度 MLP → 写到 `buf2`。
3. kernel C：读 `buf2` → 跑颜色 MLP → 写到 `buf3`。
4. kernel D：读 `buf3` → 做 alpha 合成 → 写到帧缓冲。

这条链有两个明显浪费：

- **显存往返**：`buf1/2/3` 装的全是中间结果，写出去又读回来，而它们其实只被紧邻的下一步用到一次。全局显存带宽是 GPU 上最稀缺的资源之一。
- **kernel 启动开销**：每个 kernel launch 都有微秒级固定开销，且 kernel 之间无法在寄存器层面共享数据。

「融合（fusion）」就是把这几个 kernel 手工拼成**一个大 kernel**，让中间结果留在**寄存器**里直接传给下一步，既不写显存，也只 launch 一次。tiny-cuda-nn 的 `FullyFusedMLP` 已经把「一层 MLP 内部」融合了；本讲讲的 JIT 融合是更外层的一步：把「编码 + 整个双头 MLP + 体渲染合成」**整条管线**融合成一个函数。

### 2.2 为什么必须「运行时编译（RTC）」

如果融合后的函数是固定的，完全可以提前（AOT）写死在源码里。但问题是：这个函数的**具体形态取决于用户配置**——哈希表多大、MLP 多宽多深、激活函数是什么、有几层。这些参数在加载 `configs/nerf/base.json` 之后才知道。所以 instant-ngp 选择**运行时**用 NVRTC（CUDA 的运行时编译库）把一段动态生成的 CUDA 源码字符串编译成可执行 kernel。这就是 RTC（Run-Time Compilation）。

### 2.3 「设备函数（device function）」是什么

CUDA 里 `__device__` 修饰的函数运行在 GPU 上、只能被 GPU 代码调用，不能单独 launch，相当于「GPU 上的普通函数」。`eval_nerf` 就是这样一个设备函数：它不被直接 launch，而是被融合内核 `render_nerf`/`train_nerf` 在每个采样点处内联调用。融合的关键就是让 `eval_nerf` 的全部计算都发生在寄存器里。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/neural-graphics-primitives/nerf_network.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h) | `NerfNetwork` 双头网络的声明。其中的 `generate_device_function` 是本讲的核心：运行时拼出 `eval_nerf` 设备函数。 |
| [include/neural-graphics-primitives/fused_kernels/render_nerf.cuh](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/render_nerf.cuh) | **渲染**融合内核 `render_nerf`：一像素一线程，沿光线边步进边调用内联 `eval_nerf` 做体渲染。 |
| [include/neural-graphics-primitives/fused_kernels/train_nerf.cuh](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/train_nerf.cuh) | **训练**融合内核 `train_nerf`：前向 + 解析损失梯度，支持 Rfl / RflRelax 训练模式。 |
| [include/neural-graphics-primitives/fused_kernels/trace_sdf.cuh](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/trace_sdf.cuh) | **SDF 球面追踪**融合内核 `trace_sdf`，调用内联 `eval_sdf`。 |
| [src/testbed_nerf.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu) | NeRF 训练/渲染的实现，在此**构造并 launch** `render_nerf` 与 `train_nerf` 两个 RTC kernel。 |
| [src/testbed_sdf.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu) | SDF 渲染实现，构造并注入 `trace_sdf` RTC kernel。 |
| [include/neural-graphics-primitives/testbed.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h) | `m_jit_fusion` 开关、`CudaRtcKernel` 持有指针、`SphereTracer` 里的 `m_fused_trace_kernel` 等声明。 |
| [src/testbed.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu) | `jit_fusion()` / `set_jit_fusion()` 的实现，以及 GUI 里的 JIT 复选框。 |
| [CMakeLists.txt](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt) | `NGP_BUILD_WITH_RTC` 开关、`rtc/cache` 缓存目录的创建、用 cmrc 把头文件嵌入二进制。 |

> 说明：`CudaRtcKernel` 这个类本身的实现在外部依赖 **tiny-cuda-nn**（`tiny-cuda-nn/rtc_kernel.h`）中，本仓库只**使用**它。因此本讲对 `CudaRtcKernel` 内部 NVRTC 编译细节不作展开，只讲它在 instant-ngp 里如何被构造与调用。

## 4. 核心概念与源码讲解

### 4.1 CudaRtcKernel：运行时编译的容器

#### 4.1.1 概念说明

`CudaRtcKernel` 是 tiny-cuda-nn 提供的一个类，它封装了「拿一段 CUDA 源码字符串 → 用 NVRTC 编译成 PTX → 加载成可 launch 的 kernel」这一整套流程。对 instant-ngp 而言，它是一台「按需开动的编译器」：你把一段**动态拼接**的源码交给它，它编译完就能像普通 `__global__` kernel 一样 `.launch(...)`。

它的三个关键特性：

1. **按需编译（懒构造）**：kernel 对象只在第一次真正要用时才被 `new` 出来并编译；之后缓存在一个 `unique_ptr` 里反复 launch，不会重复编译。
2. **源码动态生成**：传给它的源码字符串是在运行时用 `fmt::format` 拼出来的——这就是「JIT」二字的本义。
3. **头文件内嵌**：源码里有 `#include <neural-graphics-primitives/fused_kernels/render_nerf.cuh>` 这种引用，NVRTC 编译时需要能找到这些头文件。instant-ngp 用 `cmrc`（Compile-Time Resource Compiler）把整个 `include/` 目录打进二进制，再当作虚拟文件系统喂给 NVRTC。

#### 4.1.2 核心流程

一个 `CudaRtcKernel` 从构造到 launch 的流程：

```text
[NVRTC 编译阶段]
1. C++ 侧用 fmt::format 拼出源码字符串 source
   source = MODEL_BODY（即 generate_device_function 生成的 eval_nerf）
          + 几行 using/constexpr（GRID_T、N_EXTRA_DIMS）
          + #include <...fused_kernels/render_nerf.cuh>
2. new CudaRtcKernel("render_nerf", source, cmrc 虚拟文件系统)
3. 内部 NVRTC 把 source 编译成 PTX → 加载成 CUmodule/CUfunction
   （编译产物缓存到磁盘 rtc/cache/ 目录，见 4.1.3）

[运行阶段]
4. kernel->launch(blocks, threads, stream, 参数1, 参数2, ...)
   —— 与普通 cuda kernel launch 无异
5. 同一个 kernel 对象被反复 launch，不再编译
```

#### 4.1.3 源码精读：缓存目录与头文件嵌入

JIT 融合的总开关在 CMake 阶段就决定好了：`NGP_BUILD_WITH_RTC`（默认 `ON`），它会被透传成 tiny-cuda-nn 的 `TCNN_BUILD_WITH_RTC`：

- [CMakeLists.txt:26](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L26)：声明 `option(NGP_BUILD_WITH_RTC ...)`。
- [CMakeLists.txt:96](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L96)：`set(TCNN_BUILD_WITH_RTC ${NGP_BUILD_WITH_RTC})`，把开关交给底层。

下面这段是本讲关于「缓存」最关键的源码：

- [CMakeLists.txt:339-345](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L339-L345) 在配置阶段做了两件事：

  1. `file(MAKE_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/rtc/cache")` —— 创建 `rtc/cache/` 目录，专门用来**缓存运行时编译产物**（NVRTC 编译出的 PTX）。这样下次启动若源码哈希没变，可以直接命中缓存、跳过几十毫秒到上百毫秒的编译，避免「每次启动都卡顿一下」。
  2. `file(GLOB_RECURSE NGP_HEADERS ...)` + `cmrc_add_resources(...)` —— 把 `include/neural-graphics-primitives/` 下**所有头文件**作为资源嵌入 `ngp-resources` 这个静态库。运行时 NVRTC 编译融合内核、遇到 `#include` 时，就是从这个内嵌的虚拟文件系统里取头文件。

这正是 `testbed_nerf.cu` 里构造 kernel 时第三个参数 `all_files(cmrc::ngp::get_filesystem())` 的来源——它把整个内嵌文件系统交给编译器用于解析 include。

> 小结：「缓存」在本讲里有两层含义：①磁盘上的 `rtc/cache/`（编译产物缓存，跨进程持久）；②进程内的 `unique_ptr<CudaRtcKernel>`（kernel 对象本身只构造一次）。NVRTC 内部具体如何计算哈希、命名缓存文件，位于外部依赖 tiny-cuda-nn，本仓库不可见，此处不再展开。

#### 4.1.4 代码实践：定位三处 RTC kernel 构造点

**实践目标**：确认本仓库在哪些地方真正构造了 `CudaRtcKernel`，验证「融合内核」一共有几类。

**操作步骤**：

1. 在仓库根目录执行（只读检索）：

   ```bash
   grep -rn "make_unique<CudaRtcKernel>" src/
   ```

2. 你应当看到恰好 3 处：`testbed_nerf.cu`（渲染）、`testbed_nerf.cu`（训练）、`testbed_sdf.cu`（SDF 追踪）。

**需要观察的现象**：三处构造的「名字」分别是 `render_nerf`、`train_nerf`、`trace_sdf`，且每处都用 `fmt::format` 把 `MODEL_BODY`（即 `eval_nerf`/`eval_sdf` 设备函数）拼进了源码。

**预期结果**：确认融合内核共三类，分别对应 NeRF 渲染、NeRF 训练、SDF 球面追踪。Volume 和 Image 两种原语**不使用** JIT 融合内核（它们的渲染逻辑不走这条快路径）。

> 本地无 GPU 时，这一步是纯源码检索，结果可即时验证，无需「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `cmrc_add_resources` 那一行注释掉，融合内核还能编译成功吗？为什么？

> **答案**：不能（或会在运行时抛异常）。NVRTC 编译 `render_nerf.cuh` 等源码时，这些 `.cuh` 又 `#include` 了 `bounding_box.cuh`、`nerf_device.cuh` 等头文件；没有内嵌文件系统，NVRTC 找不到这些头文件，编译会失败。失败后会被 try/catch 捕获，进而把 `m_jit_fusion` 置为 `false` 降级（见 4.2.3）。

**练习 2**：`rtc/cache` 目录里存的是什么？删掉它会出错吗？

> **答案**：存的是 NVRTC 编译出的 PTX/缓存元数据。删掉不会出错，只是下次首次触发 JIT 时会重新编译、产生一次性的编译延迟，随后又会被重新写回。

---

### 4.2 三类融合内核：render / train / trace

#### 4.2.1 概念说明

有了 `CudaRtcKernel` 这台「编译器」，instant-ngp 写了三类融合内核，分别覆盖 NeRF 渲染、NeRF 训练、SDF 追踪。它们的共同结构是：

- 一个 `__global__` 入口（如 `render_nerf`），由 host 调度。
- 内部反复调用一个**内联设备函数** `eval_nerf`（或 `eval_sdf`）——这个函数就是 `generate_device_function` 生成的「编码 + MLP」整条管线。
- 用 `__all_sync(0xFFFFFFFF, ...)` 强制整个 warp（32 个线程）保持步调一致，保证每个线程都执行了 MLP，因为融合内核的权重读取依赖 warp 级别的协作访存。

三类内核的差异在于「每个线程负责什么」和「调用 `eval_nerf` 之后干什么」：

| 内核 | 一个线程负责 | 调 `eval_nerf` 之后 |
| --- | --- | --- |
| `render_nerf` | 一个像素 → 一条光线 | 把 RGBσ 做 alpha 合成累加到 `color` |
| `train_nerf` | 若干采样点 | 算损失梯度，写回 `dloss_doutput` 供反向 |
| `trace_sdf` | 一条光线 | 用预测距离推进光线，命中表面即停 |

#### 4.2.2 核心流程（以 render_nerf 为例）

`render_nerf` 的核心是一个 `while(true)` 光线步进循环，每轮：

```text
1. 借 density_grid 位域跳过空体素（if_unoccupied_advance_to_next_occupied_voxel）
2. 在当前点构造 NerfCoordinate（位置/方向/dt）打包进 nerf_in
3. __all_sync 检查：若整个 warp 都已 !alive，提前跳出
4. vec4 nerf_out = eval_nerf(nerf_in, params);   ← 内联融合！
5. alpha = 1 - exp(-density * dt);  合成 color
6. 若累积不透明度足够（color.a > 1 - min_transmittance），提前终止
```

#### 4.2.3 源码精读

**(a) 渲染融合内核 `render_nerf`** 的签名与融合调用点：

- [include/neural-graphics-primitives/fused_kernels/render_nerf.cuh:22-58](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/render_nerf.cuh#L22-L58)：`__global__ void render_nerf(...)`，参数列表极长——它把渲染所需的一切（相机矩阵、密度网格、`params` 网络参数、激活类型等）一次性传进来。注意参数 `const network_precision_t* params` 就是整块网络权重，会被 `eval_nerf` 内部按偏移切分使用。
- [include/neural-graphics-primitives/fused_kernels/render_nerf.cuh:131-150](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/render_nerf.cuh#L131-L150)：这是融合的「心脏」。第 132 行 `if (__all_sync(0xFFFFFFFF, !alive)) break;` 用 warp 同步原语保证步调一致；第 136 行 `vec4 nerf_out = eval_nerf(nerf_in, params);` 直接在寄存器里跑完整网络；第 147-150 行立刻用结果做 alpha 合成。整段没有把中间特征写回显存。

**(b) 训练融合内核 `train_nerf`** 的前向与梯度：

- [include/neural-graphics-primitives/fused_kernels/train_nerf.cuh:22](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/train_nerf.cuh#L22)：`__global__ void train_nerf(...)`，比渲染多了 `loss_output`、`dloss_doutput`、训练模式等参数。
- [include/neural-graphics-primitives/fused_kernels/train_nerf.cuh:344-354](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/train_nerf.cuh#L344-L354)：同样用 `__all_sync` + 内联 `eval_nerf`（第 348 行）。
- [include/neural-graphics-primitives/fused_kernels/train_nerf.cuh:391-410](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/train_nerf.cuh#L391-L410)：训练内核的特别之处——它在内核里**解析地**算损失梯度（`loss_and_gradient`），并根据 `ETrainMode`（`Rfl`/`RflRelax`/`Nerf`）走不同的梯度公式。这也解释了 4.2.4 里「Rfl/RflRelax 只能在 JIT 模式下训练」的原因：这些模式的梯度推导只写在了融合内核里。

**(c) SDF 追踪融合内核 `trace_sdf`**：

- [src/testbed_sdf.cu:1136-1157](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L1136-L1157)：构造 `trace_sdf` RTC kernel，源码里 `#include <...fused_kernels/trace_sdf.cuh>`，设备函数名是 `eval_sdf`（由 `m_network->generate_device_function("eval_sdf")` 生成）。注意 SDF 的网络是单头 `NetworkWithInputEncoding`（见 u3-l3），所以 `eval_sdf` 内部只有「编码 + 一个 MLP」，比 NeRF 的双头简单。构造好后通过 `tracer.set_fused_trace_kernel(...)` 注入 `SphereTracer`。

**(d) 编译失败自动降级**——这是健壮性设计的关键。三处构造都被包在 try/catch 里：

- [src/testbed_nerf.cu:1930-1950](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L1930-L1950)（渲染）、[src/testbed_nerf.cu:3102-3120](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L3102-L3120)（训练）、[src/testbed_sdf.cu:1138-1151](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L1138-L1151)（SDF）模式一致：`catch (const std::runtime_error& e)` 后打印告警并 `m_jit_fusion = false`。即一旦 NVRTC 编译失败（例如显存不足、驱动太老、源码超长），程序不会崩，而是关掉 JIT、回退到非融合路径继续跑。

#### 4.2.4 代码实践：JIT 开关如何改变渲染/训练路径

**实践目标**：验证 `m_jit_fusion` 为真/假时，渲染与训练分别走哪条代码分支。

**操作步骤**：

1. 打开 [src/testbed_nerf.cu:1928](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L1928)。渲染分支的判据是：

   ```cpp
   if (m_jit_fusion && m_render_mode == ERenderMode::Shade
       && m_visualized_dimension == -1 && m_nerf.show_accel == -1) {
   ```

   只有「JIT 开启 + 标准 Shade 渲染 + 没有任何可视化覆盖」时才走融合 `render_nerf`。其余情况（法线/深度/编码可视化、`show_accel` 调试）一律走 `NerfTracer` 两段式路径（见 u4-l3）。

2. 打开 [src/testbed_nerf.cu:3090-3094](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L3090-L3094)：

   ```cpp
   // RFL training only implemented in JIT training mode.
   if (!m_jit_fusion && m_nerf.training.train_mode != ETrainMode::Nerf) {
       m_nerf.training.train_mode = ETrainMode::Nerf;
       tlog::warning() << "JIT fusion is disabled, switching to NeRF training mode.";
   }
   ```

   **训练路径对 JIT 有强依赖**：`Rfl`/`RflRelax` 两种训练模式只在融合内核里实现（4.2.3 b），关掉 JIT 时会被强制降回普通 `Nerf` 模式并打印告警。

3. 顺带注意 [src/testbed_nerf.cu:3096-3097](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L3096-L3097) 的一句开发者注释：

   ```cpp
   //  TODO: the below fused kernel is actually slower than the unfused alternative for NeRF training.
   //        look into optimizing it until it is faster.
   ```

   即：对**标准 NeRF 训练**而言，融合训练内核目前**反而比非融合慢**（作者留了 TODO）。因此融合训练内核主要是为 Rfl/RflRelax 这些新模式服务的，不是为加速普通 NeRF 训练。

**需要观察的现象**：

- 渲染：JIT 开启且 Shade 模式 → 帧率明显更高（少一次 launch、中间结果不落显存）。
- 训练：JIT 开启 → `Rfl`/`RflRelax` 模式可用；JIT 关闭 → 自动退回 `Nerf` 模式。

**预期结果 / 待本地验证**：渲染加速可在有 GPU 的机器上对比同一视角关/开 JIT 的帧率观察到；训练模式降级会直接在控制台打印 `"JIT fusion is disabled, switching to NeRF training mode."`。无 GPU 环境下，以上为源码级结论，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么渲染融合内核要求 `m_render_mode == ERenderMode::Shade`？切到 `Depth` 或 `Normals` 模式时会发生什么？

> **答案**：融合 `render_nerf` 内核内部只做了「体渲染 alpha 合成」这一种合成逻辑，输出颜色与深度副产品。`Depth`/`Normals`/`EncodingVis` 等可视化模式需要不同的输出计算，这些只在 `NerfTracer` 的两段式路径里实现。因此一旦不是 Shade，就退回 `NerfTracer`（它本身也支持所有可视化模式）。JIT 只优化最常见的「正常着色」路径。

**练习 2**：训练融合内核「比非融合还慢」却仍被保留，主要价值是什么？

> **答案**：它提供了 `Rfl`/`RflRelax` 训练模式的**唯一实现**（这两种模式的梯度公式写在融合内核里，非融合路径没有对应代码）。所以它不是「为了快」，而是「为了支持新训练目标」。关掉 JIT 就用不了这两种模式。

---

### 4.3 设备函数生成：generate_device_function

#### 4.3.1 概念说明

前面反复出现的 `eval_nerf`，到底从哪来？它不是手写的，而是 `NerfNetwork::generate_device_function` 在运行时**字符串拼接**出来的。这正是「JIT 融合」名字里「融合」二字的具体落点：把原本独立的四块积木

- `pos_encoding`（位置 → 位置特征，HashGrid）
- `density_network`（位置特征 → 密度特征，一个小 MLP）
- `dir_encoding`（方向 → 方向特征）
- `rgb_network`（密度特征 ‖ 方向特征 → RGB，一个 MLP）

拼成一个签名形如 `vec4 eval_nerf(input, params, fwd_ctx)` 的设备函数。该函数读一整块扁平的 `params` 权重缓冲，按预计算好的字节偏移切给各组件用；返回 4 维 `[R, G, B, σ]`。

要让它能工作，必须解决两个「布局」问题：

1. **参数偏移（params offset）**：四块积木的权重在 `params` 数组里首尾相连。融合函数得知道位置编码的权重从第几个字节开始。这正是 u4-l2 里 `set_params_impl` 的拼装顺序（density_network → rgb_network → pos_encoding → dir_encoding）的对应物。
2. **前向上下文偏移（fwd_ctx offset）**：融合前向需要一块 per-element 的临时上下文（比如 MLP 各层中间值），各组件占用大小不同，融合函数按 `WARP_SIZE`（32）为单位去索引。

#### 4.3.2 核心流程

`generate_device_function("eval_nerf")` 做的事：

```text
1. 为四个子组件各起一个唯一名字：
   eval_nerf_density_network / _rgb_network / _pos_encoding / _dir_encoding
2. 让每个子组件自己生成它的设备函数源码（递归），
   拼成 preamble（前置声明四段 __device__ 函数）
3. 用 dfmt 模板生成「主体 body」：
   a. pos_enc_out = pos_encoding(input 的前几维, params + 偏移A, fwd_ctx + 偏移a)
   b. density_out = density_network(pos_enc_out, params, fwd_ctx)
      把 density_out 塞进 rgb_mlp_in 的前 16 维
   c. dir_enc_out = dir_encoding(input 的方向维, params + 偏移C, fwd_ctx + 偏移c)
      塞进 rgb_mlp_in 的后几维
   d. rgb_out = rgb_network(rgb_mlp_in, params + 偏移B, fwd_ctx + 偏移b)
   e. return {rgb_out[0], rgb_out[1], rgb_out[2], rgb_mlp_in[0]}
                       ↑ RGB 三维           ↑ 密度 = density_network 的第 0 维
4. generate_device_function_from_body 把 body 包成最终 __device__ 函数返回
```

第 3e 步的细节很关键：返回的第 4 维不是 `rgb_out` 的某个分量，而是 `rgb_mlp_in[0]`——因为密度是 `density_network` 输出的第 0 维（回忆 u4-l2：密度头 16 维特征里第 0 维即体密度 σ），而 `rgb_mlp_in` 的前 16 维正好就是 density 输出。

#### 4.3.3 源码精读

**主体在 [include/neural-graphics-primitives/nerf_network.h:476-520](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L476-L520)**。

- 第 477-480 行：给四个子组件加前缀得到唯一名，避免多次融合时函数名冲突。
- 第 482-488 行（preamble）：递归调用各子组件的 `generate_device_function`，把它们的源码拼到前面。注意顺序——先两个 MLP，再两个 encoding，与 `set_params_impl` 的权重布局一致。
- 第 490-500 行（body 模板）：这是融合的「接线图」。其中关键占位符的取值：
  - `POS_ENC_PARAMS_OFFSET = m_density_network->n_params() + m_rgb_network->n_params()`（第 503 行）——位置编码权重排在两个 MLP 之后。
  - `RGB_MLP_PARAMS_OFFSET = m_density_network->n_params()`（第 507 行）——颜色 MLP 紧跟在密度 MLP 之后。
  - `DIR_ENC_PARAMS_OFFSET = ... + m_pos_encoding->n_params()`（第 514 行）——方向编码在最后。
  - 同理 `*_FWD_CTX_OFFSET`（第 504/508/515 行）给出前向上下文里每段的起始位置，注意都乘了 `WARP_SIZE`，因为上下文是 per-thread（warp 内每线程一份）。
- 第 499 行 `return {{rgb_mlp_out[0], rgb_mlp_out[1], rgb_mlp_out[2], rgb_mlp_in[0]}};`：第四维返回 `rgb_mlp_in[0]` 即密度，证实了 4.3.2 的分析。

**配套的反向函数 `generate_backward_device_function` 在 [include/neural-graphics-primitives/nerf_network.h:522-602](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L522-L602)**：它按相反顺序（rgb_network → dir_encoding → density_network → pos_encoding）拼出反向设备函数，供训练融合内核的反向阶段调用。注意第 548 行 `dL_drgb_mlp_in[0] = dL_drgb_mlp_in[0] + dL_dy[3];`——把来自密度维（输出第 4 维）的梯度叠加回密度通道，与正向第 4 维复用密度输出的设计严格对应。

**前向上下文大小的聚合在 [include/neural-graphics-primitives/nerf_network.h:604-611](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L604-L611)**：`device_function_fwd_ctx_bytes()` 把四块组件各自需要的上下文字节数相加，host 据此分配一块刚好够大的 per-element 缓冲。

**JIT 布局转换 [include/neural-graphics-primitives/nerf_network.h:630-642](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L630-L642)**：融合内核对权重在显存里的排布（jit layout）与普通前向（普通 layout）可能不同。`convert_params_to_jit_layout` / `convert_params_from_jit_layout` 在进入/退出融合路径时重排权重。host 侧通过 `jit_guard`（一个 RAII 对象）自动完成这一对调用——见 [src/testbed_nerf.cu:1909](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L1909) 的 `auto jit_guard = nerf_network->jit_guard(stream, true);`。

#### 4.3.4 代码实践：手动追踪一次 eval_nerf 的接线

**实践目标**：把 `generate_device_function` 拼出来的 `eval_nerf` 用伪代码画出来，确认每个组件的参数偏移来源正确。

**操作步骤**：

1. 打开 [include/neural-graphics-primitives/nerf_network.h:490-500](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L490-L500) 的 body 模板。
2. 对照 `set_params_impl`（[nerf_network.h:357-372](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L357-L372)）确认权重拼装顺序确实是 `density_network → rgb_network → pos_encoding → dir_encoding`。
3. 自己把模板里的占位符替换成具体调用，写成下面的伪代码（**示例代码，非项目原样**，用于理解）：

   ```cpp
   // 示例代码：generate_device_function 拼出的 eval_nerf 大致等价于
   __device__ vec4 eval_nerf(input_t input, const T* params, float* fwd_ctx) {
       // 1. 位置编码
       auto pos_enc_out = eval_nerf_pos_encoding(
           input.slice<0, 3>(),                           // 前 3 维是位置
           params + DENSITY_PARAMS + RGB_PARAMS,          // POS_ENC_PARAMS_OFFSET
           fwd_ctx + WARP_SIZE * (DENSITY_CTX + RGB_CTX));
       // 2. 密度 MLP，结果塞进 rgb_mlp_in 的前 16 维
       vec<...> rgb_mlp_in;
       rgb_mlp_in.slice<0, 16>() = eval_nerf_density_network(pos_enc_out, params, fwd_ctx);
       // 3. 方向编码，塞进 rgb_mlp_in 的 16.. 维
       rgb_mlp_in.slice<16, ...>() = eval_nerf_dir_encoding(
           input.slice<DIR_OFFSET, 3>(),
           params + DENSITY_PARAMS + RGB_PARAMS + POS_ENC_PARAMS,
           fwd_ctx + WARP_SIZE * (DENSITY_CTX + RGB_CTX + POS_ENC_CTX));
       // 4. 颜色 MLP → RGB
       auto rgb_mlp_out = eval_nerf_rgb_network(rgb_mlp_in, params + DENSITY_PARAMS, fwd_ctx + WARP_SIZE*DENSITY_CTX);
       // 5. 返回 RGB + 密度(rgb_mlp_in[0])
       return {rgb_mlp_out[0], rgb_mlp_out[1], rgb_mlp_out[2], rgb_mlp_in[0]};
   }
   ```

**需要观察的现象**：四个子组件的参数偏移互不重叠、首尾相接，且方向编码偏移 = 三个前置组件大小之和——与 `n_params()` 的累加完全吻合。

**预期结果**：你能用一句话总结「`eval_nerf` = 位置编码 → 密度 MLP →（拼接方向编码）→ 颜色 MLP，密度取自密度头第 0 维」。这一步是纯源码阅读，结论可直接对账，无需「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么返回值第 4 维是 `rgb_mlp_in[0]` 而不是 `density_network(pos_enc_out)` 的显式调用结果？

> **答案**：因为第 2 步已经把 `density_network` 的输出写进了 `rgb_mlp_in` 的前 16 维，`rgb_mlp_in[0]` 就等于密度头的第 0 维，即体密度 σ。直接复用 `rgb_mlp_in[0]` 避免了重复存放/计算，这是融合省寄存器的体现。这也和 `NerfNetwork` 一贯约定一致：密度头 16 维特征中第 0 维是密度（见 u4-l2）。

**练习 2**：`device_function_fwd_ctx_bytes()` 为什么要把四个组件的大小**相加**而不是取最大值？

> **答案**：因为融合前向是「四块组件在同一个设备函数里**依次**执行」，每块都需要自己那份 per-element 临时上下文，且它们的生命周期互不重叠却都要并存于同一次调用中，所以总的上下文缓冲必须装得下四份之和。host 按这个总和分配一块缓冲，再用 `WARP_SIZE * offset` 让每块组件定位到自己的区段。

**练习 3**：`generate_backward_device_function` 里各组件的调用顺序与正向相反（rgb_network 先于 pos_encoding），为什么？

> **答案**：反向传播（backprop）的数学本质要求按计算图的**逆序**传递梯度。正向是 `pos_enc → density_mlp → dir_enc → rgb_mlp`，反向自然要先算 `rgb_mlp` 的输入梯度，再传给 `dir_enc` 和 `density_mlp`，最后传回 `pos_enc`。这与 `NerfNetwork::backward_impl`（[nerf_network.h:189](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L189)）里非融合反向的顺序一致。

## 5. 综合实践

把本讲三块内容串起来，完成下面这个「JIT 融合全链路追踪」任务。

**任务**：假设你在 GUI 里勾选了「JIT fusion」复选框并加载了一个 fox NeRF 场景，请按时间顺序追踪从「勾选」到「屏幕出现一帧渲染图」之间，JIT 融合子系统发生的全部关键事件，并标注源码位置。

**参考解答（请先自己尝试再对照）**：

1. **开关被设置**：GUI 复选框回调 `ImGui::Checkbox("JIT fusion", &jit)` → `set_jit_fusion(jit)`（[src/testbed.cu:1384-1388](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L1384-L1388)）。`set_jit_fusion` 会遍历所有设备，对每个 `device.network()` 调 `set_jit_fusion(val)`（[src/testbed.cu:627-634](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L627-L634)）。
2. **检查硬件支持**：若 `tcnn::supports_jit_fusion()` 为假（CUDA < 11.8 或算力 < 75），复选框被禁用、`m_jit_fusion` 被强制置假（[src/testbed.cu:1379-1401](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L1379-L1401)）。
3. **下一帧渲染进入快路径**：`frame() → train_and_render() → 渲染分支`，因 `m_jit_fusion && Shade && 无可视化` 为真，进入融合路径（[src/testbed_nerf.cu:1928](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L1928)）。
4. **首次进入构造 kernel**：`device.fused_render_kernel()` 为空 → 构造 `CudaRtcKernel`，源码由 `generate_device_function("eval_nerf")`（[nerf_network.h:476](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L476)）生成，NVRTC 编译（命中 `rtc/cache` 则秒过），头文件从 cmrc 虚拟文件系统取（[src/testbed_nerf.cu:1931-1945](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L1931-L1945)）。
5. **权重转 JIT 布局**：`jit_guard` RAII 对象在进入时调用 `convert_params_to_jit_layout`（[src/testbed_nerf.cu:1909](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L1909)）。
6. **launch 融合内核**：`device.fused_render_kernel()->launch(...)`，每像素一线程，内部 `eval_nerf` 在寄存器里端到端跑完编码+MLP 并做 alpha 合成（[src/testbed_nerf.cu:1959](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L1959) 与 [render_nerf.cuh:136](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/render_nerf.cuh#L136)）。
7. **权重还原**：`jit_guard` 析构时 `convert_params_from_jit_layout`，为下一轮非融合路径（如训练）做好准备。
8. **上屏**：渲染结果经 CUDA-GL 互操作贴到窗口（见 u6-l1）。

> 若在第 4 步 NVRTC 编译抛异常，会落到 catch 分支（[src/testbed_nerf.cu:1946-1950](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L1946-L1950)）把 JIT 关掉，下一帧自动回退到 `NerfTracer` 路径——程序不崩，只是变慢。

## 6. 本讲小结

- **JIT 融合的本质**：把「输入编码 + MLP + 体渲染合成」原本分散的多个 kernel，在运行时用 NVRTC 编译成**单个**融合 kernel，让中间特征留在寄存器、只 launch 一次，从而省下显存带宽与启动开销。
- **CudaRtcKernel 是台按需编译器**：构造时接收动态拼接的源码字符串，编译产物缓存到磁盘 `rtc/cache/`，头文件经 cmrc 内嵌进二进制供 NVRTC 解析 `#include`；kernel 对象本身在进程内只构造一次、反复 launch。
- **三类融合内核**：`render_nerf`（NeRF 渲染快路径）、`train_nerf`（含 Rfl/RflRelax 训练的唯一实现）、`trace_sdf`（SDF 球面追踪）；Volume 与 Image 原语不走此路。
- **`generate_device_function` 是融合的接线图**：递归让四个子组件各生成自己的设备函数，再用预计算的字节偏移（params offset / fwd_ctx offset）拼出一个端到端的 `eval_nerf`，密度取自密度头第 0 维。
- **`m_jit_fusion` 开关**默认取 `tcnn::supports_jit_fusion()`，受 CUDA ≥ 11.8、算力 ≥ 75 限制；GUI 复选框、Python `testbed.jit_fusion` 属性、`set_jit_fusion()` 三处均可控制。
- **优雅降级**：任何一处 RTC 编译失败都被 try/catch 捕获，自动关 JIT 并回退非融合路径；训练侧关掉 JIT 会把 `Rfl`/`RflRelax` 强制降回普通 `Nerf` 模式。此外，标准 NeRF 训练的融合内核目前并不更快（源码 TODO 标注），融合训练主要为新训练模式服务。

## 7. 下一步学习建议

- **本单元后续**：阅读 **u8-l3（相机位姿与镜头优化）** 看 NeRF 自标定，以及 **u8-l5（扩展 instant-ngp）** 了解如何基于 pyngp 做受控实验。
- **深入底层**：`CudaRtcKernel`、`FullyFusedMLP`、`convert_params_to_jit_layout` 的真正实现都在外部依赖 **tiny-cuda-nn**。若想理解 NVRTC 缓存哈希、权重布局细节、16 对齐的全融合内核权重排布，应去 `dependencies/tiny-cuda-nn/`（记得 `git submodule update --init --recursive`）阅读 `rtc_kernel.h`、`fully_fused_mlp.cu`、`network_with_input_encoding.h`。
- **动手实验**：在有 GPU 的机器上，加载 fox 场景后用 GUI 反复勾选/取消「JIT fusion」，观察 Shade 模式下帧率变化；再用 pyngp（`testbed.jit_fusion = False/True`）做对照，体会渲染快路径的收益边界。
