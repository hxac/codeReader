# 张量抽象与公共工具

## 1. 本讲目标

前几讲（u5-l1 到 u5-l5）带你看完了运行时的「上层建筑」：`LLMInferenceRuntime`、请求响应模型、引擎执行器、解码策略、KV 缓存管理器。它们都建立在同一块「地基」之上——`cpp/common/` 里的张量抽象与公共工具。

本讲把镜头对准这块地基。读完本讲你应当能够：

1. 说清 `Coords` 类如何用一组不超过 8 维的整数描述任意张量形状，并能与 TensorRT 的 `nvinfer1::Dims` 互转。
2. 说清 `Tensor` 类如何用 RAII 封装一块 GPU/CPU 线性布局内存，理解它的「拥有 / 不拥有」两种所有权模式，以及为什么拷贝构造被删除而移动构造被允许。
3. 掌握 `logger.h` 的进程级单例日志器、`ELLM_CHECK` / `CUDA_CHECK` 校验宏，以及 `safetensorsUtils`、`fileUtils` 等运行时各处复用的公共工具。

这些组件看似平凡，却是整个运行时里被引用最频繁的代码。理解它们，你才能真正读懂后续任何模块里出现的 `Tensor`、`LOG_DEBUG(...)`、`CUDA_CHECK(...)`。

## 2. 前置知识

在进入源码前，先建立两个直觉。

**什么是线性布局（linear layout）张量。** GPU/CPU 上的一块显存/内存本质上是一维的字节序列。多维张量（如 `[batch, heads, seq, dim]`）只是对这块一维内存的一种「视角」。要从多维下标定位到一维地址，就需要**步长（stride）**：每个维度跳过一个元素要越过多少个底层元素。EdgeLLM 的 `Tensor` 只支持行优先（row-major，即 C-contiguous）布局，因此步长可以由形状直接算出。

**什么是 RAII（Resource Acquisition Is Initialization）。** 这是 C++ 管理资源的核心惯用法：在构造函数里申请资源（如 `cudaMalloc`），在析构函数里释放资源（如 `cudaFree`）。这样资源的生命周期就绑定到了对象的生命周期——对象出作用域被销毁时，资源自动释放，不会泄漏。为了不破坏这条「唯一所有者」约定，RAII 类通常会**禁用拷贝**（避免两个对象都以为拥有同一块内存而双重释放）、**只允许移动**（把所有权干净地移交出去）。这正是本讲 `Tensor` 的设计。

几个本讲会用到的术语：

| 术语 | 含义 |
|------|------|
| `nvinfer1::DataType` | TensorRT 定义的张量元素类型枚举，如 `kHALF`(FP16)、`kFLOAT`(FP32)、`kINT32`、`kFP8` 等 |
| `nvinfer1::Dims` | TensorRT 的形状描述结构，含 `nbDims` 与定长数组 `d[8]` |
| owned / non-owned | 张量是否拥有底层内存：拥有则析构时释放，不拥有则只是「借」一块外部内存的视图 |
| 单例（singleton） | 进程内只存在一个实例的对象，本讲的日志器就是 |

## 3. 本讲源码地图

本讲涉及的关键文件集中在 `cpp/common/`：

| 文件 | 作用 |
|------|------|
| [cpp/common/tensor.h](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/tensor.h) | `Coords`、`Tensor` 类与工具函数的声明 |
| [cpp/common/tensor.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/tensor.cpp) | `Coords` / `Tensor` 的方法实现（分配、释放、移动、reshape 等） |
| [cpp/common/logger.h](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/logger.h) | 进程级 `EdgeLLMLogger` 单例、`LOG_*` 宏、函数追踪器 |
| [cpp/common/checkMacros.h](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/checkMacros.h) | `ELLM_CHECK` / `CUDA_CHECK` 校验宏 |
| [cpp/common/safetensorsUtils.{h,cpp}](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/safetensorsUtils.cpp) | 用 `Tensor` 读写 safetensors 文件，是「公共工具如何围绕 `Tensor` 工作」的范本 |
| [cpp/common/fileUtils.h](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/fileUtils.h) | `copyFile` 等文件 I/O 小工具 |
| [cpp/common/stringUtils.h](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/stringUtils.h) | `fmtstr` printf 风格格式化字符串（日志宏依赖它） |

## 4. 核心概念与源码讲解

### 4.1 Coords：形状的统一描述

#### 4.1.1 概念说明

`Coords` 是 EdgeLLM 自定义的「形状」类，用来描述一个张量每一维有多大。它解决的痛点是：TensorRT 自带的 `nvinfer1::Dims` 是一个 C 风格结构体（一个 `nbDims` 加一个定长 `d[]` 数组），既不安全也不方便（没有拷贝、没有越界检查、没有 `volume`）。运行时各处都需要「算总量」「转成 TRT Dims」「格式化打印」这类操作，`Coords` 把这些行为封装成一个值类型，可以随便按值传递。

`Coords` 的核心约束是：**最多 8 维，每维非负**，由常量 `kMAX_DIMS` 界定：

[cpp/common/tensor.h:47-48](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/tensor.h#L47-L48) —— 定义最大维数常量 `kMAX_DIMS = 8`。

#### 4.1.2 核心流程

`Coords` 的内部存储是一个定长 `std::array<int64_t, 8>` 加一个实际维数 `mNumDims`。它的关键行为：

1. **多种构造入口**：可从 `nvinfer1::Dims`、初始化列表 `{2, 3}`、`std::vector`、迭代器区间构造，统一收敛到「拷贝 + 校验不超过 8 维」。
2. **`volume()`**：把所有维相乘得到元素总数（0 维返回 0）。
3. **`getTRTDims()`**：反向转回 `nvinfer1::Dims`，用于与 TensorRT API 对接。
4. **`operator[]`**：带越界检查的逐维访问。

行优先布局下，给定形状 \([d_0, d_1, \dots, d_{n-1}]\)，第 \(i\) 维的步长为：

\[
\mathrm{stride}_i = \prod_{j=i+1}^{n-1} d_j
\]

即「该维每跨一步，要越过后面所有维的乘积个元素」，最后一维步长恒为 1。这个公式就是 `computeStrides` 的实现。

#### 4.1.3 源码精读

从 `nvinfer1::Dims` 构造 `Coords`，用 `std::copy` 把 TRT 的 `d[]` 拷进内部数组：

[cpp/common/tensor.h:114-118](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/tensor.h#L114-L118) —— `Coords(Dims const&)`：记录维数并拷贝各维。

`volume()` 把所有维连乘，特判 0 维返回 0：

[cpp/common/tensor.cpp:103-115](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/tensor.cpp#L103-L115) —— 逐维累乘得到元素总数。

`getTRTDims()` 是反向桥接：把内部数组写回 `nvinfer1::Dims`，运行时调用 TensorRT API（如 `setInputShape`）时要用：

[cpp/common/tensor.cpp:117-126](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/tensor.cpp#L117-L126) —— 把 `Coords` 还原为 `nvinfer1::Dims`。

步长计算 `computeStrides`，最后一维置 1，从后往前累乘：

[cpp/common/tensor.cpp:74-86](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/tensor.cpp#L74-L86) —— 实现 \( \mathrm{stride}_i = \prod_{j>i} d_j \)。

#### 4.1.4 代码实践

**实践目标**：理解 `Coords` 的多种构造方式与 `volume`/`getTRTDims` 的互换。

**操作步骤**（源码阅读型，不依赖 GPU）：

1. 打开 [tensor.h 的 Coords 类](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/tensor.h#L85-L215)，数清楚它有几个构造函数。
2. 手算：形状 `{4, 8, 16}` 的 `volume()` 是多少？它的步长数组按 `computeStrides` 应该是 `[128, 16, 1]`，请自行验证。
3. 在 [safetensorsUtils.cpp:251-256](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/safetensorsUtils.cpp#L251-L256) 观察真实代码如何从 JSON 的 shape 数组构造 `Coords`。

**预期结果**：`{4, 8, 16}` 的 volume 是 512；`getTRTDims()` 产出的 `Dims.nbDims == 3`、`d = {4, 8, 16}`。若手算步长不是 `[128, 16, 1]`，说明对行优先布局的理解有偏差，回头重看公式。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Coords` 用定长 `std::array<int64_t, 8>` 而不是 `std::vector`？
**答案**：定长数组在栈上分配、无堆开销，拷贝/移动廉价，且形状维数天然有上限（8 维足够覆盖所有张量）。`vector` 会引入堆分配与间接寻址，对一个会被高频按值传递的小对象不划算。

**练习 2**：形状 `{2, 0, 3}` 的 `volume()` 是多少？这样的 `Coords` 能用来构造一个拥有内存的 `Tensor` 吗？
**答案**：`volume()` 是 0（任意维含 0 则连乘为 0）。不能——拥有内存的 `Tensor` 构造函数会拒绝 0 volume（见 4.2.3）。

---

### 4.2 Tensor：RAII 线性张量

#### 4.2.1 概念说明

`Tensor` 是运行时里最基础的数据载体：它把「一块线性内存 + 一个形状 + 一个数据类型 + 一个设备（CPU/GPU）」打包成一个对象。它的设计哲学只有两条：

- **RAII 管生命周期**：自己 `cudaMalloc`/`cudaMallocHost` 来的内存，析构时自己 `cudaFree`/`cudaFreeHost`。
- **两种所有权模式**：
  - **owned（拥有）**：内存是自己申请的，析构时释放，可以 `reshape`。
  - **non-owned（不拥有）**：内存是外部借来的（比如 mmap 映射区、或 TensorRT 引擎的绑定缓冲），析构时**不**释放，只当视图用。

这两个模式由两个不同的构造函数区分，这一点是理解整个运行时内存归属的关键（回顾 u5-l3：KV 缓存属于 `SharedResources` 而非执行器，很多时候就是靠 non-owned `Tensor` 把已有缓冲「包」成 `Tensor` 视图来传递）。

#### 4.2.2 核心流程

`Tensor` 的生命周期与状态流转如下：

```
          (owned 构造)                         (move)
  ┌─────────────────────┐   move ctor/assign   ┌─────────────────────┐
  │ cudaMalloc / Host   │ ───────────────────► │ 新对象拿到指针+所有权 │
  │ ownMemory = true    │                      │ 旧对象被清空(不释放) │
  └─────────┬───────────┘                      └─────────┬───────────┘
            │ 出作用域                                    │ 出作用域
            ▼                                             ▼
     releaseResource: cudaFree / Host            releaseResource: data==nullptr, skip
```

关键规则：

1. **拷贝构造 / 拷贝赋值被 `delete`**：防止两个对象都以为拥有同一块内存 → 双重释放。
2. **移动构造 / 移动赋值允许**：把指针搬到新对象，旧对象置空（`data=nullptr`、`ownMemory=false`），于是只有新对象的析构会真正释放。
3. **`reshape` 仅对 owned 张量生效**，且新形状的总字节数不能超过原始 `memoryCapacity`（即只能改「视角」，不能超配）。
4. **non-owned 构造允许 0 volume**（此时不发指针、capacity=0），用于表达「可选/空」张量。

#### 4.2.3 源码精读

先看 owned 构造函数：校验 volume>0、拒绝子字节类型（kINT4/kFP4）、算 capacity、按设备分配。CPU 走 `cudaMallocHost`（页锁定内存，DMA 拷贝更快），GPU 走 `cudaMalloc`：

[cpp/common/tensor.cpp:144-175](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/tensor.cpp#L144-L175) —— owned 构造：`memoryCapacity = volume * getTypeSize(dtype)`，按设备分别 `cudaMallocHost` / `cudaMalloc`。

子字节类型（INT4/FP4）在这里被显式拒绝，因为这类类型每元素不足 1 字节，无法用「元素数 × 每元素字节数」简单算容量（参见 4.3 里 `getTypeSize` 的 `default` 分支抛错）。运行时的 4-bit 权重走的是打包后的整字节容器（如 int8），而非裸 `Tensor`。

再看 non-owned 构造函数：只填元信息、`ownMemory=false`，volume 非 0 时才接管外部指针：

[cpp/common/tensor.cpp:177-201](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/tensor.cpp#L177-L201) —— non-owned 构造：作纯数据容器，不释放外部内存，volume 为 0 时 data 置空。

拷贝删除、移动允许的声明（这是本讲核心问题之一）：

[cpp/common/tensor.h:233-248](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/tensor.h#L233-L248) —— 拷贝构造/赋值 `= delete`，移动构造/赋值 `noexcept`。

移动构造的实现：逐字段搬运指针与元信息，然后把源对象**清空**（关键是 `other.data = nullptr` 和 `other.ownMemory = false`），这样源对象析构时 `releaseResource` 走到「不拥有」分支不会误释放：

[cpp/common/tensor.cpp:218-237](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/tensor.cpp#L218-L237) —— 移动构造：转移所有权并把源置为空张量。

析构委托给 `releaseResource`：仅当 `ownMemory` 为真才调用对应的 `cudaFreeHost` / `cudaFree`，最后把所有字段清空。注意析构整体是 `noexcept`，内部用 `try/catch` 兜住异常只记日志、不向外抛：

[cpp/common/tensor.cpp:353-378](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/tensor.cpp#L353-L378) —— `releaseResource`：按 `ownMemory` 与设备类型选择释放方式。

`reshape` 的实现，体现「容量守恒」：owned 且新体积不超过原 capacity 才放行，只改形状与步长、不重新分配：

[cpp/common/tensor.cpp:336-351](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/tensor.cpp#L336-L351) —— `reshape`：容量不足或非拥有则返回 false。

最后是类型安全的取数指针模板 `dataPointer<T>()`：它用 `is_arithmetic_ext` 在编译期挡住非法类型，再做 `reinterpret_cast`。注意头文件里特别强调「类型不匹配是未定义行为」——它不做运行期类型校验，调用方自己保证 `T` 与 `getDataType()` 一致：

[cpp/common/tensor.h:343-369](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/tensor.h#L343-L369) —— `dataPointer<T>()`：编译期类型约束 + reinterpret_cast。

#### 4.2.4 代码实践

**实践目标**：用 `Coords` 构造一个 GPU 上的 FP16 张量、写入数据、再拷到 CPU，并解释拷贝为何被删而移动被允许。

这是一段**示例代码**（非项目原生代码），用来说明 `Tensor` 的典型用法。要编译它需要链接 `edgellmCore` 静态库并配置好 CUDA/TensorRT 环境（参见 u1-l3 的构建说明）。

```cpp
// 示例代码：演示 Tensor 的拥有/移动语义与设备拷贝
#include "common/tensor.h"
#include <cuda_fp16.h>
#include <vector>

using namespace trt_edgellm::rt;

void demo()
{
    // 1) 用 Coords 描述形状 [2, 3]
    Coords shape{2, 3};

    // 2) 在 GPU 上分配一个 FP16 张量（拥有内存）
    Tensor gpuT(shape, DeviceType::kGPU, nvinfer1::DataType::kHALF, "demo_gpu");

    // 3) 在 CPU 准备 6 个 FP16 值，并写入 GPU
    std::vector<half> host(6, __float2half(1.5f));
    cudaMemcpy(gpuT.rawPointer(), host.data(), 6 * sizeof(half), cudaMemcpyHostToDevice);

    // 4) 再建一个 CPU 上的 FP16 张量，把 GPU 数据拷回来
    Tensor cpuT(shape, DeviceType::kCPU, nvinfer1::DataType::kHALF, "demo_cpu");
    cudaMemcpy(cpuT.rawPointer(), gpuT.rawPointer(), 6 * sizeof(half), cudaMemcpyDeviceToHost);

    // 5) 通过 typed 指针读取一个值（类型由调用方保证一致）
    half const* p = cpuT.dataPointer<half>();
    // p[0] == 1.5

    // 6) 移动构造：把 cpuT 的所有权干净交给 moved（源被清空，不会双重释放）
    Tensor moved(std::move(cpuT));   // OK：移动允许
    // Tensor bad = gpuT;            // 编译错误：拷贝构造已 delete
}
```

**操作步骤**：

1. 阅读 [tensor.h 的 Tensor 类声明](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/tensor.h#L221-L397)，确认两个构造函数分别对应 owned / non-owned。
2. 对照上面示例，追踪一次「CPU 数据 → GPU → CPU」的拷贝路径，标出每一步用的是 `rawPointer()` 还是 `dataPointer<T>()`。
3. 在 [safetensorsUtils.cpp 的 saveSafetensors](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/safetensorsUtils.cpp#L164-L181) 里找真实代码：它如何用 `getDeviceType()` 判断、再把 GPU 张量 `cudaMemcpyAsync` 到 CPU 写盘。

**需要观察的现象**：步骤 6 中，`Tensor bad = gpuT;` 这一行若取消注释，会在**编译期**就报错（因为拷贝构造是 `= delete`），而不是运行期。这正是删除拷贝的意义——把「双重释放」这类灾难性 bug 挡在编译之前。

**关于「为何拷贝被删、移动被允许」的解答**（本讲实践要求的核心问题）：

- **删除拷贝**：`Tensor` 若允许默认拷贝，会按成员逐字段复制，导致两个对象的 `data` 指针相同、且 `ownMemory` 都为 `true`。两者析构时都会对同一块显存调用 `cudaFree`，触发**双重释放**，属于未定义行为（通常是崩溃）。删除拷贝强制开发者显式决定所有权如何流转。若确实需要两个对象共享同一块内存，应改用 **non-owned 构造函数**显式包一个视图，头文件注释也点名了这一点（[tensor.h:227-233](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/tensor.h#L227-L233)）。
- **允许移动**：移动语义把指针「搬」到新对象，并把源对象的 `data` 置为 `nullptr`、`ownMemory` 置为 `false`（见上面移动构造源码）。这样全程只有**一个**所有者，源对象析构时走「不拥有」分支不释放，新对象析构时释放一次，所有权唯一性得以维持——与 `std::unique_ptr` 同理。这也是 `std::vector<Tensor>`（如 `loadSafetensors` 里 `tensors.push_back(std::move(tensor))`）能工作的前提。

**预期结果**：`p[0]` 读回 `1.5`；若环境无 GPU/无 TensorRT，则本例为「待本地验证」，但代码路径与所有权分析可在源码层面完成。

#### 4.2.5 小练习与答案

**练习 1**：一个 owned 的 `Tensor` 经过 `std::move` 后，源对象的 `isEmpty()` 返回什么？它的 `getOwnMemory()` 呢？
**答案**：移动构造会把源的 `mShape` 清成默认 `Coords{}`（0 维，volume=0），故 `isEmpty()` 返回 `true`；`ownMemory` 被置为 `false`。所以源对象析构时不会释放任何内存。

**练习 2**：如果一个 owned GPU 张量原本形状是 `{8, 8}`（FP16，capacity = 128 字节），调用 `reshape({4, 4})` 会成功吗？`reshape({4, 4, 4})` 呢？
**答案**：`reshape({4,4})` 需要 16 × 2 = 32 字节 ≤ 128，成功。`reshape({4,4,4})` 需要 64 × 2 = 128 字节，正好等于 capacity，也成功（条件是「不超过」）。两者都仅改视角、不重新分配。若改成 `{4,4,4,2}`（256 字节）则超容，返回 `false`、形状不变。

---

### 4.3 公共工具：logger / checkMacros / safetensorsUtils / fileUtils

#### 4.3.1 概念说明

除了 `Tensor` / `Coords`，`cpp/common/` 还提供一组全运行时复用的「胶水」工具。它们各自很小，但几乎每个 `.cpp` 都会 include：

- **logger.h**：进程级单例日志器 `EdgeLLMLogger`，同时实现 TensorRT 的 `nvinfer1::ILogger` 接口（接收 TRT 内部日志），并通过 `LOG_DEBUG/INFO/WARNING/ERROR` 宏把 EdgeLLM 自身日志按 `file:line:function` 格式输出。
- **checkMacros.h**：`ELLM_CHECK(cond, msg)`（条件失败抛 `std::runtime_error`，懒求值消息）与 `CUDA_CHECK(stat)` / `CUDA_DRIVER_CHECK(stat)`（把 CUDA 错误码翻译成异常）。
- **stringUtils.h**：`fmtstr`——printf 风格的字符串格式化，是 `LOG_*` 宏的底层依赖。
- **safetensorsUtils**：`saveSafetensors` / `loadSafetensors`，把 `std::vector<Tensor>` 序列化成 `.safetensors` 文件或反向加载。
- **fileUtils.h**：`copyFile` 等文件操作小工具。

理解这组工具的意义在于：你在运行时任何角落看到的 `LOG_DEBUG(...)`、`CUDA_CHECK(...)`、`ELLM_CHECK(...)` 都来自这里，行为是统一且可预测的。

#### 4.3.2 核心流程

**日志器的关键设计**有三处值得专门讲：

1. **永不析构的「不朽单例」**。`instance()` 用函数局部 `static` 指针持有一个 `new` 出来的对象，且**故意从不 delete**。原因是 TensorRT 的全局插件注册表会在进程级持有这个 `ILogger` 的引用，若日志器像普通全局对象那样在静态析构期被销毁，另一线程可能仍在通过注册表回调它，造成 use-after-free。不析构就消除了这个竞态；那份固定的内存在进程退出时由操作系统回收，指针始终可达，不是增长的泄漏：

[cpp/common/logger.h:89-93](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/logger.h#L89-L93) —— 函数局部 static 指针 + `new`，构造线程安全且免疫静态初始化顺序问题。

2. **严重级别方向与流选择**。TensorRT 约定**数值越小越严重**（`kINTERNAL_ERROR=0` … `kVERBOSE=4`）。`shouldLog` 用 `level <= mMinLevel` 判定是否输出；`logWithLocation` 把 `≤ kWARNING` 的输出导向 `std::cerr`，其余导向 `std::cout`：

[cpp/common/logger.h:234-236](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/logger.h#L234-L236) 与 [cpp/common/logger.h:134](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/logger.h#L134) —— 级别判定与错误流/标准流的分流。

3. **线程安全的时间格式化**。`formatLogEntry` 用 `localtime_r` 配栈上 `tm` 缓冲，而不是 `std::localtime`——后者返回指向进程级共享缓冲的指针，多线程并发记日志会互相踩踏：

[cpp/common/logger.h:251-256](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/logger.h#L251-L256) —— 用可重入的 `localtime_r` 避免竞态。

**校验宏**方面，`ELLM_CHECK` 是懒求值的：`msg` 表达式只在 `cond` 为假时才求值。这对热路径很重要——比如带 `std::to_string` / `ostringstream` 拼接的错误消息，正常路径下零开销。失败时消息会带上 `__FILE__:__LINE__` 前缀：

[cpp/common/checkMacros.h:106-113](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/checkMacros.h#L106-L113) —— `ELLM_CHECK`：条件失败才求值 msg 并抛异常。

#### 4.3.3 源码精读

`LOG_*` 宏把「格式化字符串 + 自动位置」粘到一起，最终都汇到 `EdgeLLMLogger::instance()`。注意它直接调 `instance()` 而不是用全局 `gLogger` 别名——因为 `gLogger` 是动态初始化的，静态初始化期不可用（头文件注释明确警告）：

[cpp/common/logger.h:356-370](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/logger.h#L356-L370) —— 四个主日志宏，自动注入 `__FILE__`/`__FUNCTION__`/`__LINE__`。

`gLogger` 这个别名本身，是指向单例的引用，方便像旧式 TensorRT 代码那样按对象使用：

[cpp/common/logger.h:351](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/logger.h#L351) —— `inline EdgeLLMLogger& gLogger = EdgeLLMLogger::instance();`

`getTypeSize` 是 `Tensor` 分配内存与 `safetensorsUtils` 算偏移都要用的基础函数，把 TRT 类型映射到字节数，并对子字节类型抛错：

[cpp/common/tensor.cpp:35-72](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/tensor.cpp#L35-L72) —— `getTypeSize`：FP32/INT32=4，FP16/BF16=2，INT8/FP8/BOOL=1，子字节类型抛异常。

最值得精读的「公共工具与 `Tensor` 协作」范本是 `loadSafetensors`：它用 `MmapReader` 把文件映射进内存、解析 JSON 头、然后**用 owned 构造函数在 GPU 上逐个建 `Tensor`**、`cudaMemcpyAsync` 灌数据，最后 `tensors.push_back(std::move(tensor))`——这里就用到 4.2 讲的移动语义（vector 要求元素可移动，而 `Tensor` 恰好删除了拷贝）：

[cpp/common/safetensorsUtils.cpp:270-277](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/safetensorsUtils.cpp#L270-L277) —— 加载时构造 owned GPU `Tensor` 并 `std::move` 入 vector。

反向的 `saveSafetensors` 则展示了 non-owned/owned 都能透明工作的设计：它只通过 `rawPointer()`、`getDeviceType()`、`getDataType()`、`getShape()` 这些只读接口访问 `Tensor`，遇 GPU 张量就先 `cudaMemcpyAsync` 到 CPU 再写盘：

[cpp/common/safetensorsUtils.cpp:164-181](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/safetensorsUtils.cpp#L164-L181) —— 保存时按设备类型决定是否先拷到 CPU。

#### 4.3.4 代码实践

**实践目标**：用日志宏的级别控制，验证「级别越小越严重」与懒求值。

**操作步骤**（源码阅读 + 思考型，不依赖运行）：

1. 阅读 [logger.h 的 LOG_* 宏](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/logger.h#L356-L370) 与 `setLevel`，回答：默认级别是 `kINFO`，此时一句 `LOG_DEBUG(...)` 会被输出吗？
2. 在 [safetensorsUtils.cpp:87-93](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/safetensorsUtils.cpp#L87-L93) 找一处 `LOG_ERROR`，再在 [tensor.cpp:166](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/tensor.cpp#L166) 找一处 `LOG_DEBUG`，对比二者在默认级别下的可见性。
3. 思考：若把 `ELLM_CHECK` 改成「总是求值 msg」的版本，对一条放在每步 decode 上的检查会有什么性能影响？

**需要观察的现象 / 预期结果**：

- 默认 `mMinLevel = kINFO`，`LOG_DEBUG`（对应 `kVERBOSE`，数值更大）满足 `level <= mMinLevel` 为 **false**，不输出。这也是为什么 `tensor.cpp` 里大量 `LOG_DEBUG("Tensor ... allocated ...")` 默认看不见——只有把级别调到 `kVERBOSE` 才会刷出每块显存的分配日志，便于排查内存。
- 若 `ELLM_CHECK` 总是求值 msg，热路径上每次都要拼字符串，即便检查通过也会付拼接开销，显著拖慢 decode。

**待本地验证**：若你已按 u1-l3 构建，可在运行 `llm_inference` 前调用 `EdgeLLMLogger::instance().setLevel(nvinfer1::ILogger::Severity::kVERBOSE);` 观察是否多出 `Tensor ... allocated on GPU` 这类调试行。

#### 4.3.5 小练习与答案

**练习 1**：`EdgeLLMLogger` 为什么同时继承 `nvinfer1::ILogger`？
**答案**：TensorRT 在引擎构建、插件注册等环节会通过 `ILogger` 回调输出它自己的内部日志。让 `EdgeLLMLogger` 实现该接口，就能把 TRT 的日志与 EdgeLLM 自身日志统一到同一套格式与级别过滤下（见 [log 方法](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/logger.h#L105-L117)，它给 TRT 消息套一个 `SourceLocation("TensorRT", ...)`）。

**练习 2**：`gLogger`（全局引用别名）与 `EdgeLLMLogger::instance()` 有何区别？为什么宏都用后者？
**答案**：`gLogger` 是指向单例的 `inline` 引用，本身是动态初始化的；而 `instance()` 是函数局部 static，首次调用才构造、且线程安全。静态初始化期（如别的全局对象构造函数里）`gLogger` 可能尚未就绪，故日志宏一律直接调 `instance()` 以避开静态初始化顺序陷阱。

---

## 5. 综合实践

把本讲三个模块串起来，设计一个「读懂一块显存从生到死」的小任务。

**场景**：你想追踪运行时里某个张量的分配与释放，并把它落盘成 safetensors。

**任务**：

1. **建图**：画一张时序图，包含以下角色与动作——
   - 调用方用 `Coords{...}` 描述形状；
   - owned 构造函数调 `cudaMalloc`（触发一条 `LOG_DEBUG`）；
   - `reshape` 改变视角但不重新分配；
   - `std::move` 转移所有权（源被清空）；
   - `saveSafetensors` 通过 `rawPointer()` + `getDeviceType()` 读出数据、必要时 D2H 拷贝写盘；
   - 最后一个所有者析构，`releaseResource` 调 `cudaFree`（再触发一条 `LOG_DEBUG`）。
2. **追踪**：在 [tensor.cpp 的构造/析构](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/tensor.cpp#L144-L378) 里，确认每条 `LOG_DEBUG` 打印的字段（name、shape、字节数、MB 数、设备），解释为什么 `toMB` 换算对排查显存占用有用。
3. **验证所有权规则**：在图上标出「哪一时刻有几个对象认为拥有这块内存」。要求全程恒为 0 或 1，绝不出现 2——并指出是哪两条机制（删拷贝 + move 置空源）共同保证了这一点。
4. **连接上层**：回顾 u5-l3，说明 `EngineExecutor` 为何能「对模型一无所知」——它操作的正是这里的 `Tensor`（经 `TensorMap`/`TensorRegistry` 绑定），而 `Tensor` 本身不依赖任何模型知识，只认形状/类型/设备/指针。

**预期产出**：一张时序图 + 一段说明，能清楚回答「一块 EdgeLLM 张量的内存从谁申请、被谁 move、由谁释放、期间日志怎么打」。如无构建环境，至少完成源码层面的图与文字分析（即「待本地验证」运行部分）。

## 6. 本讲小结

- `Coords` 是值类型的形状描述：最多 8 维、按值传递、提供 `volume()` 与 `getTRTDims()`，桥接 EdgeLLM 与 TensorRT 的 `nvinfer1::Dims`。
- `Tensor` 用 RAII 封装线性布局内存，有 **owned**（自申请自释放，可 reshape）与 **non-owned**（外部内存视图，不释放）两种所有权，由不同构造函数区分。
- 拷贝构造/赋值被 `delete` 是为了防止双重释放；移动构造/赋值把指针搬走并把源置空，维持唯一所有者——这是 `std::vector<Tensor>` 能工作的前提。
- `getTypeSize` 是分配与序列化的基础，子字节类型（INT4/FP4）被显式拒绝，运行时的 4-bit 权重走打包容器而非裸 `Tensor`。
- `EdgeLLMLogger` 是「永不析构」的进程级单例（规避 TRT 插件注册表的 use-after-free），级别「越小越严重」，`LOG_*` 宏直接调 `instance()` 以避开静态初始化顺序问题。
- `safetensorsUtils` 是公共工具围绕 `Tensor` 协作的范本：`load` 用 owned 构造 + `std::move`，`save` 只用只读接口并按设备决定是否 D2H。

## 7. 下一步学习建议

本讲的地基已经铺好，接下来建议：

1. **回到 u5-l3（引擎执行器与张量注册表）**，现在你能真正读懂 `TensorMap`（非拥有的名字→`Tensor*` 表）与 `TensorRegistry`（声明式绑定规格）是如何基于这里的 `Tensor` 搭起来的——尤其体会「non-owned `Tensor` 当视图」的设计。
2. **阅读 u5-l5（KV 缓存管理）**，观察 KV 缓存这块大显存是如何被 `Tensor` 化、并在 attention 插件里做 past/present 同址原地更新的——这是 owned/non-owned 视图模式的高阶用法。
3. **如果想深入工具层**，可浏览 `cpp/common/` 下未细讲但同样被广泛复用的文件：`mmapReader`（safetensors 加载的底层映射）、`trtUtils`（TRT 类型/Dims 辅助）、`version`（u4-l1 提到的 `edgellm_version` 校验就在这里）。
4. **想动手的话**：参照本讲示例代码与 u1-l3 的构建说明，写一个最小程序，构造一个 owned GPU `Tensor`、`reshape` 它、再 `saveSafetensors` 落盘，用日志级别 `kVERBOSE` 观察完整的分配/释放日志。
