# MACA 后端架构总览

> 本讲是 U7「Metax/MACA 后端」系列的首篇，也是整本手册进入**本 fork 核心差异化内容**的第一讲。
> 前面 U1–U6 我们学到的 TileLang 编译流程（lowering、layout 推断、codegen、张量核发射）都是「后端可替换」的：同一份 DSL 可以编到 cuda / hip / maca。
> 本讲回答一个总问题：**为了把 TileLang 跑在 MetaX GPU 上，metax 分支在 C++ 与 Python 两侧到底新增了哪几个零件，它们是怎么拼起来的？**

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「注册一个 MACA target」在 C++ 侧做了哪几件事（设备类型、属性默认值、canonicalizer）。
- 读懂 `MACADeviceAPI` 如何把通用的 `DeviceAPI` 抽象映射到 MetaX 的 `mc*` 运行时接口。
- 解释「设备源码 → 可执行模块」这一段在 MACA 上如何走 `target.build.tilelang_maca` 与 `MACAModule`。
- 把 Python 侧的 **target 检测器** 与 **codegen 注册** 和 C++ 侧的全局函数一一对应起来。
- 理解为什么 `warp_size=64`（区别于 CUDA 的 32）是 MACA 后端最显眼、也是牵一发动全身的属性。

## 2. 前置知识

本讲默认你已经掌握：

- **target 体系**（U3-l1）：target 由 kind（`cuda`/`hip`/`maca`）+ attrs（`mcpu`/`arch`/…）组成，`determine_target` 是统一入口。
- **JIT 与 device codegen**（U3-l2、U4-l1）：`lower` 把 IR 下译后，`device_codegen` 会调用一个**按 target 注册的构建函数**把设备 TIR 变成源码再变成可执行模块。
- **C++ 编译器双层布局**（U5-l1）：公共层（`backend/common/`）做跨后端分发，后端自有层（`src/maca/`）结构上与 `src/cuda/`、`src/rocm/` 对称。
- **MACA 是什么**（U1-l1、U3-l3）：MACA 是 MetaX 的 GPU 编程平台，类比 NVIDIA 的 CUDA；运行时 API 以 `mc` 前缀开头（类比 CUDA 的 `cuda*`/`cu*`），编译器叫 `mxcc`。

一句话复习：CUDA 后端用 `cuda*` 运行时 + `nvcc` 编译器；MACA 后端用 `mc*` 运行时 + `mxcc` 编译器，在 TileLang 内部则是同一套抽象的两个对称实现。

## 3. 本讲源码地图

| 文件 | 角色 | 语言 |
|------|------|------|
| `src/maca/runtime/maca_target_kind.cc` | 注册 `maca` target kind：属性默认值 + 规范化器（canonicalizer） | C++ |
| `src/maca/runtime/maca_device_api.cc` | `MACADeviceAPI`：显存分配/拷贝/设备属性/计时，封装 `mc*` 接口 | C++ |
| `src/maca/runtime/maca_module.cc` | `MACAModule`：装载编译产物（`mcbin`），按设备懒加载并启动 kernel | C++ |
| `src/maca/codegen/rt_mod_maca.cc` | `BuildTileLangMACA`：设备 TIR → 源码 → `mcir`/`mcbin` 模块 | C++ |
| `src/maca/target_utils.cc` | target 属性纯函数助手：`TargetIsMaca`、`TargetMacaGetWarpSize=64` | C++ |
| `tilelang/maca/target.py` | Python 侧：MACA 可用性检测、target 检测器注册 | Python |
| `tilelang/maca/codegen.py` | Python 侧：把 `maca` target 绑定到 C++ 构建函数 | Python |
| `tilelang/maca/execution_backend.py` | Python 侧：注册 MACA 可用的执行后端（tvm_ffi/mcrtc/…） | Python |

把这八张表看成 MACA 后端的「四件套 + 三个 Python 注册点」：

- **运行时四件套（C++）**：target_kind（身份证）、device_api（怎么和 GPU 对话）、module（怎么装载 kernel）、rt_mod（怎么生成模块）。
- **Python 三注册点**：target detector（auto 探测）、device codegen（找构建函数）、execution backend（怎么跑）。

## 4. 核心概念与源码讲解

### 4.1 maca target 注册：给 MetaX GPU 办一张「身份证」

#### 4.1.1 概念说明

要让 TileLang「认识」一种新硬件，第一步是**注册一个 target kind**。target kind 是 TVM 里描述「这是一类什么样的目标平台」的数据结构，由名字（如 `maca`）、设备类型（`DLDeviceType` 枚举值）和一组**属性选项**（`mcpu`、`thread_warp_size`、…）构成。

CUDA 一出生就内置了 `kDLCUDA`、ROCm 有 `kDLROCm`。metax 分支给 vendored 的 TVM/dlpack 打了补丁，新增了 `kDLMACA`（以及 `kDLMACAHost`）这两个设备类型枚举值——这就是 MACA 能成为一个**一等 target** 的根基。`kDLMACA` 的具体定义位于本 fork 打过补丁的 TVM 子模块头件中（vendored dlpack/TVM 内的 `DLDeviceType` 枚举），本仓库工作树内未单独保留该生成头，故不在此标注具体行号（待确认）。

> 关键直觉：**target kind 不是一个开关，而是一张带默认值的属性表**。后续所有 pass、codegen、算子分派都从这张表里读属性来决定行为。

#### 4.1.2 核心流程

注册一个 target kind 需要四步：

1. **登记身份**：`TVM_REGISTER_TARGET_KIND("maca", kDLMACA)` —— 名字 `maca` + 设备类型 `kDLMACA`。
2. **声明属性**：用 `add_attr_option` 逐个列出可选属性并给默认值（如 `thread_warp_size` 默认 64）。
3. **设默认 keys**：`set_default_keys({"maca", "gpu"})` 表示这个 kind 既属于 `maca` 也属于广义 `gpu`，便于跨后端筛选。
4. **挂规范化器**：`set_target_canonicalizer(UpdateMACAAttrs)` —— 用户没显式填的属性（`mtriple`、`mcpu`）在这里自动补齐。

规范化器在 `Target("maca")` 构造时被调用，确保即使你只写一个字符串 `"maca"`，底层 LLVM target 也拿得到合法的三元组（`mtriple`）和 CPU 名（`mcpu`）。

#### 4.1.3 源码精读

注册本体只有一段链式调用：

[maca_target_kind.cc:59-71](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_target_kind.cc#L59-L71) —— 这是 `maca` target 的「户口本」。它声明了全部属性与默认值，并把 `UpdateMACAAttrs` 挂为规范化器。逐行含义：

- `TVM_REGISTER_TARGET_KIND("maca", kDLMACA)`：登记 kind 名与设备类型。
- `add_attr_option<int64_t>("max_num_threads", refl::DefaultValue(1024))`：全卡最大并发线程数 1024。
- `add_attr_option<int64_t>("max_threads_per_block", ... 1024)`：单 block 最大线程数 1024。
- `add_attr_option<int64_t>("max_shared_memory_per_block", ... 65536)`：单 block shared memory 64 KB。
- **`add_attr_option<int64_t>("thread_warp_size", ... 64)`**：warp 大小默认 **64**（CUDA 是 32，这是两后端最关键的差异，下面反复用到）。
- `add_attr_option<int64_t>("max_local_memory_per_block", ... 4095)`：单 block local（寄存器溢出）内存上限。
- `set_default_keys({"maca", "gpu"})`：把 `maca` 归入 `gpu` 大类。
- `set_target_canonicalizer(UpdateMACAAttrs)`：挂规范化器。

规范化器 `UpdateMACAAttrs` 负责「缺啥补啥」：

[maca_target_kind.cc:38-57](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_target_kind.cc#L38-L57) —— 它做两件事：

- 强制设 `mtriple = "mxc-metax-macahca"`（MACA 的 LLVM 目标三元组），这是给底层 LLVM codegen 用的。
- 处理 `mcpu`：若用户给了 `mcpu`（形如 `xcore1000`），抽出其架构号校验；**若没给，则调用全局函数 `tvm_callback_maca_get_arch` 去设备上实时探测架构**，探测不到则用兜底值 `"xcore1000"`。这就是 U3-l3 提到「canonicalizer 会自动补齐 mtriple 与 mcpu，探测不到回退 xcore1000」的落点。

> 设计要点：把「属性补齐」放在 canonicalizer，而不是写死在 Python 里，好处是**任何路径构造 `Target("maca")`（字符串、字典、对象）都会经过同一份逻辑**，行为一致。

#### 4.1.4 代码实践

**目标**：把 MACA target 的属性表抄一遍，并与 CUDA 的默认值对照，体会 `warp_size=64` 的位置。

**操作步骤**：

1. 打开 [maca_target_kind.cc:59-71](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_target_kind.cc#L59-L71)。
2. 在仓库内搜索 CUDA 的 target kind 注册作对照：
   - `grep -rn "TVM_REGISTER_TARGET_KIND(\"cuda\"" 3rdparty/tvm/`（CUDA 注册在 vendored TVM 中）。
3. 填写下表（示例答案见「小练习」）：

   | 属性 | MACA 默认 | CUDA 默认 | 说明 |
   |------|-----------|-----------|------|
   | `thread_warp_size` | **64** | 32 | 一条 warp 的线程数 |
   | `max_threads_per_block` | 1024 | ? | |
   | `max_shared_memory_per_block` | 65536 | ? | |

**需要观察的现象**：MACA 的 `thread_warp_size=64` 与 CUDA 的 32 形成鲜明对比；其余硬件资源量级相近。

**预期结果**：你能口头复述「MACA 一条 warp = 64 线程，CUDA = 32 线程」，并明白这会影响后面 GEMM 算子分派时每个 warp 切多少列（见 U7-l3）。

> 本实践为源码阅读型，无需真实设备即可完成。

#### 4.1.5 小练习与答案

**练习 1**：如果 MetaX 下一代卡把 warp 大小改成 128，最少要改这一段代码的哪一行？
**答案**：改 `thread_warp_size` 的 `DefaultValue(64)` 为 128（[maca_target_kind.cc:67](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_target_kind.cc#L67)）。但注意 `TargetMacaGetWarpSize` 当前是写死返回 64 的（见 4.4.3），那里也得同步改。

**练习 2**：为什么 `mcpu` 的补齐要放到 canonicalizer，而不是写死成默认 `xcore1000` 就好？
**答案**：因为不同代 MetaX 卡架构号不同（`xcore1000` 只是兜底）。canonicalizer 允许在运行时通过 `tvm_callback_maca_get_arch` 探测真实设备架构，做到一份代码适配多代硬件；写死默认值会导致算子指令选择（依赖架构号）选错。

---

### 4.2 MACA device API：把通用「设备抽象」翻译成 mc\* 调用

#### 4.2.1 概念说明

TVM 的 `DeviceAPI` 是一个抽象基类，定义了「任何一种计算设备都必须能做的几件事」：选设备、查属性、分配/释放显存、拷贝数据、建流、同步、计时。CUDA 有 `CUDADeviceAPI`（封装 `cuda*`），ROCm 有 `ROCmDeviceAPI`（封装 `hip*`），MACA 对应 `MACADeviceAPI`（封装 `mc*`）。

它的意义在于：**上层 runtime（模块加载、张量管理、profiler）只跟 `DeviceAPI` 抽象打交道**，不知道底下是 `cudaMalloc` 还是 `mcMalloc`。换硬件 = 写一个新的 `DeviceAPI` 子类 + 注册，上层零改动。

#### 4.2.2 核心流程

`MACADeviceAPI` 要 override 的虚函数与对应的 `mc*` 接口：

| `DeviceAPI` 虚函数 | MACA 实现 | 类比 CUDA |
|--------------------|-----------|-----------|
| `SetDevice(dev)` | `mcSetDevice` | `cudaSetDevice` |
| `GetAttr(kExist)` | `mcGetDeviceCount` | `cudaGetDeviceCount` |
| `GetAttr(kWarpSize)` | `mcDeviceGetAttribute(mcDeviceAttributeWarpSize)` | `cudaDeviceAttributeWarpSize` |
| `AllocDataSpace` | `mcMalloc` / `mcMallocHost` | `cudaMalloc` / `cudaMallocHost` |
| `FreeDataSpace` | `mcFree` / `mcFreeHost` | `cudaFree` |
| `CopyDataFromTo` | `mcMemcpyAsync`（D2D/H2D/D2H） | `cudaMemcpyAsync` |
| `CreateStream` / `SyncStream` | `mcStreamCreate` / `mcStreamSynchronize` | `cudaStreamCreate` / `cudaStreamSynchronize` |

注意一个细节：`GetAttr(kWarpSize)` 走的是**运行时从真实硬件读**的 `mcDeviceAttributeWarpSize`，而 target kind 里的 `thread_warp_size=64` 是**编译期默认值**。两者在 MetaX 卡上应一致（都是 64），但来源不同：一个是「这个 kind 假设的 warp 大小」，一个是「这张卡真实的 warp 大小」。

#### 4.2.3 源码精读

类声明与设备属性查询：

[maca_device_api.cc:43-149](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_device_api.cc#L43-L149) —— `MACADeviceAPI` 的 `GetAttr` 用一个大 `switch` 把 TVM 的设备属性枚举映射到 MACA 的 `mcDeviceGetAttribute`。其中读 warp 大小的一支：

[maca_device_api.cc:60-64](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_device_api.cc#L60-L64) —— 运行时从硬件读真实 warp size。这正是 profiler / layout 推断在校验「理论 warp 大小」时对照的「实际 warp 大小」来源。

显存分配（含 256 字节对齐的硬约束）：

[maca_device_api.cc:150-164](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_device_api.cc#L150-L164) —— 设备显存走 `mcMalloc`，主机锁页内存走 `mcMallocHost`。开头那行 `TVM_FFI_ICHECK_EQ(256 % alignment, 0U)` 是 MACA 的硬约束：**显存指针必须 256 字节对齐**，否则后续 kernel 取 descriptor 会错。

数据拷贝的总入口：

[maca_device_api.cc:176-209](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_device_api.cc#L176-L209) —— 按 `dev_from`/`dev_to` 的设备类型分派到 `mcMemcpyDeviceToDevice` / `DeviceToHost` / `HostToDevice`，并支持跨卡 `mcMemcpyPeerAsync`。这是 torch 张量与 MetaX 显存互传的底层通路。

注册成全局 `device_api.maca`：

[maca_device_api.cc:269-282](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_device_api.cc#L269-L282) —— 在 `TVM_FFI_STATIC_INIT_BLOCK` 里把 `MACADeviceAPI::Global()` 注册为 `device_api.maca` 和 `device_api.maca_host` 两个名字。上层 runtime 用 `DeviceAPI::Get("maca")` 按名取回这个单例。

> 错误处理宏 `MACA_CALL` / `MACA_DRIVER_CALL`（见 [maca_common.h:35-48](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_common.h#L35-L48)）封装了「调用 → 检查 `mcSuccess` → 抛 `MACAError`」的样板，对应 CUDA 侧的 `CUDA_CALL`。这是整段 C++ 能保持简洁的原因。

#### 4.2.4 代码实践

**目标**：体会 `DeviceAPI` 抽象的「一一对应」关系，建立 mc\* ↔ cuda\* 的心算映射。

**操作步骤**：

1. 打开 [maca_device_api.cc:43-149](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_device_api.cc#L43-L149)。
2. 仿照 U3-l1 的对照法，为每个 `case` 写出等价的 CUDA 函数名（例如 `kMaxThreadsPerBlock → mcDeviceGetAttribute(mcDeviceAttributeMaxThreadsPerBlock)` 对应 `cudaDeviceGetAttribute(cudaDevAttrMaxThreadsPerBlock)`）。
3. 注意 `kComputeVersion`（[maca_device_api.cc:70-80](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_device_api.cc#L70-L80)）如何拼出 `"major.minor"` 字符串——这是后续算子按架构选指令的依据。

**需要观察的现象**：除了前缀 `mc`/`cuda` 和个别枚举名，两个后端的 `GetAttr` 结构几乎逐行对称。

**预期结果**：你能凭直觉说出「想给 MACA 加一个新设备属性查询，就在这个 `switch` 里加一个 `case`」。

> 本实践为源码阅读型，无需设备。

#### 4.2.5 小练习与答案

**练习 1**：`AllocDataSpace` 为什么强制 256 字节对齐？
**答案**：MACA 的 TMA/descriptor 类机制要求显存基址 256 字节对齐（见 [maca_device_api.cc:152-153](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_device_api.cc#L152-L153)）。不对齐会导致 kernel 内访问越界或段错误，所以分配期就校验。

**练习 2**：`device_api.maca` 和 `device_api.maca_host` 为什么注册成同一个单例？
**答案**：MACA 的主机锁页内存（`mcMallocHost`）和设备显存（`mcMalloc`）由同一个 `MACADeviceAPI` 类的 `AllocDataSpace` 按 `device_type`（`kDLMACAHost` vs `kDLMACA`）内部分流（见 [maca_device_api.cc:155-163](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_device_api.cc#L155-L163)），所以两个名字共用一个实例即可。

---

### 4.3 MACA 模块加载与构建：从设备 TIR 到可执行 kernel

#### 4.3.1 概念说明

device codegen 的产物不是「源码字符串」就完事——上层需要一个**可调用对象**：给指针和 grid/block 维度，就能在 GPU 上启动 kernel。TVM 用 `Module` 抽象承载这件事。MACA 的 `MACAModule` 就是这个抽象的具体实现，它内部持有编译产物（`mcbin` 二进制），按设备懒加载并暴露 `GetFunction`。

把「设备 TIR」变成「`MACAModule`」需要两个 C++ 全局函数，它们由 Python 侧的 codegen 注册项触发：

- `target.build.tilelang_maca`：完整编译（TIR → 源码 → `mxcc` 编成 `mcbin` → 装进模块）。
- `target.build.tilelang_maca_without_compile`：只取源码字符串不真编译（用于无设备环境看生成代码）。

#### 4.3.2 核心流程

完整构建链路（`BuildTileLangMACA`）：

```
设备 IRModule
  └─(1) CodeGenTileLangMACA 印出 MACA 源码字符串  (code)
       └─(2) tilelang_callback_maca_postproc(code, target)   ← Python 注册的后处理回调
            └─(3) tilelang_callback_maca_compile(code, target, pass_config)
                   └─ 内部调 mxcc 编译，产出 mcir 路径或 mcbin
                 ├─ 返回 '/' 开头路径 → fmt="mcir"
                 └─ 否则              → fmt="mcbin"
            └─(4) MACAModuleCreate(产物, fmt, 函数信息表, 源码)
```

其中第 (2)(3) 步是 U4-l1 讲过的「postproc 回调」机制：**C++ 按名调用 Python 注册的全局函数**。这样编译器本体（C++）和「具体怎么调 mxcc」（Python，依赖 `tilelang.contrib.mxcc`）解耦——后者可以独立演进，不必重编 `libtilelang.so`。

运行期 `MACAModule` 的职责：

- 持有 `mcbin` 二进制 + 函数信息表（每个 kernel 的参数类型、launch 参数标签）。
- **按设备懒加载**：每个 GPU 一份 `mcModule_t`，首次取函数时才 `mcModuleLoadData`。
- `GetFunction(name)` 返回一个 `MACAWrappedFunc`，调用时通过 `mcModuleLaunchKernel` 启动。

#### 4.3.3 源码精读

构建主函数 `BuildTileLangMACA`：

[rt_mod_maca.cc:101-140](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/rt_mod_maca.cc#L101-L140) —— 关键几步：

- [rt_mod_maca.cc:102-104](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/rt_mod_maca.cc#L102-L104)：实例化 `CodeGenTileLangMACA`（U7-l2 详解）。
- [rt_mod_maca.cc:106-109](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/rt_mod_maca.cc#L106-L109)：校验 kernel 入口名唯一 + 调 Python `tilelang_callback_maca_validate`。
- [rt_mod_maca.cc:111-119](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/rt_mod_maca.cc#L111-L119)：把每个 `PrimFunc` 印进源码，并强制 `calling_conv == kDeviceKernelLaunch`（U4-l1 提到的设备函数标记）。
- [rt_mod_maca.cc:121-125](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/rt_mod_maca.cc#L121-L125)：生成源码后，若注册了 `tilelang_callback_maca_postproc` 就调它改写源码（postproc 拦截点）。
- [rt_mod_maca.cc:128-139](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/rt_mod_maca.cc#L128-L139)：调 `tilelang_callback_maca_compile` 真编译；按返回值是否以 `/` 开头判定 `fmt`，最后 `MACAModuleCreate`。

无设备模式 `BuildTileLangMACAWithoutCompile`：

[rt_mod_maca.cc:142-169](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/rt_mod_maca.cc#L142-L169) —— 与上面同构，但不调 compile 回调，直接拿源码字符串造一个 `fmt="mcir"` 的占位模块。这就是 U3-l3 说的「无设备可经 `tilelang_maca_without_compile` 只取源码」。

注册两个全局函数：

[rt_mod_maca.cc:171-177](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/rt_mod_maca.cc#L171-L177) —— 在静态初始化块里把 `BuildTileLangMACA` / `BuildTileLangMACAWithoutCompile` 注册为 `target.build.tilelang_maca[_without_compile]`。Python 侧 codegen 注册项正是按这个名字去找它们的（见 4.4.3）。

模块本体与 kernel 启动：

[maca_module.cc:57-151](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_module.cc#L57-L151) —— `MACAModuleNode`。注意 `module_` 是 `std::array<mcModule_t, kMaxNumGPUs>`：**每张卡一个槽，懒加载**（`GetFunc` 里首次访问才 `mcModuleLoadData`，见 [maca_module.cc:104-120](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_module.cc#L104-L120)），这是多卡安全的关键。

实际启动 kernel 的地方：

[maca_module.cc:216-220](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_module.cc#L216-L220) —— `mcModuleLaunchKernel(func, grid, block, shared, stream, ...)`，参数顺序与 CUDA 的 `cuLaunchKernel` 一致。这行是「TileLang kernel 真正跑在 MetaX GPU 上」的物理落点。

工厂与按字节装载：

[maca_module.cc:289-311](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_module.cc#L289-L311) —— `MACAModuleCreate` 是工厂；`MACAModuleLoadFromBytes`（`ffi.Module.load_from_bytes.maca`）支持把序列化好的模块字节流恢复回来，是 kernel 缓存（U3-l2 的 `KernelCache`）落盘后重新加载的通路。

> 计时器也住在 device_api.cc：[maca_device_api.cc:284-324](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_device_api.cc#L284-L324) 用 `mcEventRecord`/`mcEventElapsedTime` 实现 `profiling.timer.maca`，profiler 测延迟时就是用的事件计时。

#### 4.3.4 代码实践

**目标**：追踪一条「设备 TIR → mcbin → 启动」的完整调用链，看清 postproc 与 compile 回调的位置。

**操作步骤**：

1. 从 [rt_mod_maca.cc:101](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/rt_mod_maca.cc#L101) 的 `BuildTileLangMACA` 出发。
2. 找到 `tilelang_callback_maca_compile` 的注册处（在 Python 侧，搜索 `register_maca_postproc` 或 `maca_compile`，位于 `tilelang/maca/` 下）。
3. 顺着 `MACAModuleCreate` → [maca_module.cc:104](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_module.cc#L104) 的 `GetFunc` → [maca_module.cc:216](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_module.cc#L216) 的 `mcModuleLaunchKernel`。
4. 画出「编译期链路」和「运行期链路」两条线。

**需要观察的现象**：编译期产物（mcbin）被 `MACAModule` 持有但**不在构造时加载到 GPU**，而是延迟到第一次 `GetFunction` → `GetFunc` 才 `mcModuleLoadData`。

**预期结果**：你能解释「为什么多卡安全」——每张卡独立懒加载，互不污染（`mutex_` 保护）。

> 无设备时，可设 `TILELANG_DEFAULT_TARGET` 指向 maca 并用 `without_compile` 路径只看源码（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`BuildTileLangMACA` 里 `fmt` 为什么有两种取值（`mcir` / `mcbin`）？
**答案**：`tilelang_callback_maca_compile` 返回值若以 `/` 开头，说明它把编译产物写成了一个**文件路径**（fmt=`mcir`，模块记录路径）；否则返回的是**二进制内容**（fmt=`mcbin`，模块记录字节）。模块加载端据此选择 `mcModuleLoadData` 的解读方式（见 [rt_mod_maca.cc:126-138](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/rt_mod_maca.cc#L126-L138)）。

**练习 2**：为什么 `MACAModuleNode` 给每张卡各留一个 `mcModule_t` 槽，而不是全局一个？
**答案**：MACA 模块句柄是 per-GPU 的（注释 [maca_module.cc:53-56](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_module.cc#L53-L56) 明说 `mcModule_t is a per-GPU module`）。多卡场景下每张卡要各自 `mcModuleLoadData` 一次才能在该卡启动 kernel，所以用数组按 `device_id` 懒加载，并用 `mutex_` 防并发竞争。

---

### 4.4 Python 侧注册：target 检测、codegen 与执行后端的三条线

#### 4.4.1 概念说明

C++ 侧把「能力」注册成全局函数后，Python 侧还要做三件「接线」工作，把 target 流程接上：

1. **target 检测器（detector）**：回答「现在这台机器上，能不能用 maca？」——供 `auto` target 探测使用（U3-l1 的 detector 注册表）。
2. **device codegen 注册**：告诉引擎「target 是 `maca` 时，请调用哪个构建函数把设备 IR 变成模块」。
3. **执行后端（execution backend）注册**：告诉 JIT「在 maca 上，可以用哪些执行后端（tvm_ffi / mcrtc / cython / cutedsl）来跑 kernel」。

这三条线都在 `tilelang/maca/__init__.py` 被 import 时自动挂载（见 [maca/__init__.py:1-6](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/__init__.py#L1-L6)），而 `tilelang/__init__.py` 末尾的 `from . import maca` 触发了它——这就是 U1-l3 所说「metax 分支挂载 MACA 的那一行」的全貌。

#### 4.4.2 核心流程

三条注册线的协作关系：

```
用户: target={"kind":"maca"} 或 target="auto"
   │
   ├─(A) determine_target → auto_detect_target
   │       遍历 detector 表 → _detect_maca_target()  ← tilelang/maca/target.py
   │       返回 Target("maca")（若可用）
   │
   ├─(B) device codegen → resolve_device_codegen(target)
   │       按 target.kind.name="maca" 查 codegen 表  ← tilelang/maca/codegen.py
   │       → 调 target.build.tilelang_maca(全编) 或 _without_compile(只看源码)
   │
   └─(C) 执行后端选择 → 在 tvm_ffi/mcrtc/cython/cutedsl 里挑可用者
           ← tilelang/maca/execution_backend.py
```

#### 4.4.3 源码精读

**(A) target 检测器**：

[target.py:14-26](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/target.py#L14-L26) —— `check_maca_availability()` 通过 `tilelang.contrib.mxcc.find_maca_path()` 找 MACA SDK 路径，找到即视为可用。

[target.py:29-37](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/target.py#L29-L37) —— `_detect_maca_target()` 两个让步条件：① 若 `torch.version.hip is not None`（torch 已是 ROCm 版），直接返回 `None` 让 ROCm 优先；② 若 MACA 不可用也返回 `None`。否则返回 `Target("maca")`。

[target.py:48](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/target.py#L48) —— `register_target_detector("maca", _detect_maca_target, override=True)` 把检测器登记进 U3-l1 讲的 detector 表。`auto` 探测时按插入顺序遍历，metax 分支的真实顺序是 CUDA→HIP→Metal→MACA（见 U3-l1）。

**(B) device codegen 注册**：

[codegen.py:8-21](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/codegen.py#L8-L21) —— 这是把 Python 与 C++「对接」的核心一段：

- `_is_plain_maca_target` 判定是否纯 maca target（不含 `cutedsl` key）。
- `register_device_codegen("maca", DeviceCodegen(...))` 构造一个 `DeviceCodegen`，其 `build` / `build_without_compile` 分别指向 C++ 全局函数 `target.build.tilelang_maca` / `target.build.tilelang_maca_without_compile`（即 4.3.3 注册的那两个），靠的是 `global_func_device_codegen`（见 [device_codegen.py:18-24](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/device_codegen.py#L18-L24)）。

这段就是「Python 表 → C++ 函数」的胶水：`DeviceCodegen.lower(mod, target, compile_device=True)` 会按 `compile_device` 选 `build` 还是 `build_without_compile`（见 [device_codegen.py:39-44](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/device_codegen.py#L39-L44)）。

**(C) 执行后端注册**：

[execution_backend.py:34-58](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/execution_backend.py#L34-L58) —— 为 maca 登记四个执行后端：

- `tvm_ffi`：默认，始终可用（`enable_host_codegen=True, enable_device_compile=True`）。
- `mcrtc`：类比 CUDA 的 nvrtc，运行时编译；当前登记但 `is_available=_is_mcrtc_available` 控制实际可用性。
- `cython`：Cython 胶水路径。
- `cutedsl`：仅当 target 带 `cutedsl` key 时匹配。

这呼应 U3-l2 讲的「执行后端与 target 正交但受各 target 注册清单约束」。

**(D) target 属性助手（C++ FFI）**：

[target_utils.cc:31-44](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/target_utils.cc#L31-L44) —— `TargetIsMaca` 判设备类型，`TargetMacaGetWarpSize` **写死返回 64**。这两个函数经 FFI 暴露为 `tl.TargetIsMaca` / `tl.TargetMacaGetWarpSize`（见 [target_utils.cc:150-157](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/target_utils.cc#L150-L157)），供 Python 侧 `target_is_maca` / GEMM 分派等处查询。

而它们又被公共层 `TargetHasAsyncCopy` 统一分发：

[target_utils.cc:15-26](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/backend/common/target_utils.cc#L15-L26)（`src/backend/common/target_utils.cc`）—— 按 `TargetIsCuda/IsRocm/IsMaca` 分派到各自 `HasAsyncCopy`。这是 U5-l3 讲的「跨后端能力统一分发」的最终落点：MACA 是否支持异步拷贝，由 `TargetMacaHasAsyncCopy`（[target_utils.cc:35-39](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/target_utils.cc#L35-L39)）回答（当前恒返回 true，但 U4-l4 提到 metax 分支在 pipeline 规划层对 MACA 强制关闭异步拷贝，所以这里的 true 并不等于真的会用 cp.async）。

> **构建期接线**：以上 C++ 文件能否被编进 `libtilelang.so`，取决于 `USE_MACA`。`src/maca/CMakeLists.txt` 把文件分两组：`TILE_LANG_MACA_ALWAYS_SRCS`（target_utils、intrin_rule、lower_maca_intrin、copy_analysis）**总是编译**（即使关 MACA 也得能识别这个 target）；其余（codegen/device_api/module/target_kind/runtime）仅在 `USE_MACA=ON` 时编译（见 [src/maca/CMakeLists.txt:3-26](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/CMakeLists.txt#L3-L26)）。而 `USE_MACA` 由环境变量驱动（[CMakeLists.txt:416-424](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/CMakeLists.txt#L416-L424)），SDK 路径三级搜索（显式路径→`MACA_PATH`→`/opt/maca`）见 [FindMACA.cmake:37-42](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/cmake/FindMACA.cmake#L37-L42)。这是 U1-l2「`USE_MACA=ON`（环境变量而非 -D）」的源码落点。

#### 4.4.4 代码实践

**目标**：用一个最小脚本，验证三条注册线是否在 `import tilelang` 后真的就位。

**操作步骤**（示例代码，可在无设备的机器上跑前两步）：

```python
# 示例代码：检查 MACA 注册线是否挂载（不依赖真实 MetaX 设备）
import tilelang                       # 触发 from . import maca
from tilelang.backend.target import list_target_detectors
from tilelang.backend.device_codegen import _DEVICE_CODEGENS, _LAZY_DEVICE_CODEGENS

print("detectors:", list_target_detectors())        # 应能看到 'maca'
print("maca codegen 条目数:", len(_DEVICE_CODEGENS.get("maca", [])))
print("lazy codegen:", _LAZY_DEVICE_CODEGENS.get("maca"))

# 手动探测（无设备时返回 False 是正常的）
from tilelang.maca.target import check_maca_availability
print("maca available:", check_maca_availability())
```

1. 先确认 `detectors` 列表里含 `maca`（说明 detector 线 A 已挂）。
2. 确认 `_DEVICE_CODEGENS["maca"]` 非空（说明 codegen 线 B 已挂）。
3. 无设备时 `check_maca_availability()` 返回 `False`，解释为什么 `_detect_maca_target` 会返回 `None`。

**需要观察的现象**：`import tilelang` 后即使没有 MetaX 硬件，三张注册表里也都有 `maca` 条目；但 `check_maca_availability()` 为 `False`，故 `auto` 不会选 maca。

**预期结果**：你能区分「注册（已就位）」与「可用（取决于设备/SDK）」两件事——这正是 U3-l3 强调的「`import tilelang` 成功 ≠ MACA 后端就绪」。

> 运行结果待本地验证（取决于构建时是否 `USE_MACA=ON`、是否装了 MACA SDK）。

#### 4.4.5 小练习与答案

**练习 1**：如果一台机器同时装了 ROCm 版 torch 和 MACA SDK，`target="auto"` 会选哪个？
**答案**：选 ROCm（hip）。因为 `_detect_maca_target` 第一行就检查 `torch.version.hip is not None`，是则返回 `None` 主动让步（见 [target.py:30-33](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/target.py#L30-L33)）。再加上 detector 表里 HIP 排在 MACA 之前，故 auto 命中 HIP。

**练习 2**：`TargetMacaGetWarpSize` 写死返回 64，但 target kind 的 `thread_warp_size` 也是 64，这两者冗余吗？
**答案**：不冗余，用途不同。`thread_warp_size` 是 target 属性，供需要 `Target` 对象的通用逻辑查询；`TargetMacaGetWarpSize` 是经 FFI 暴露给 Python（`tilelang/maca/target.py` 没直接用，但 GEMM 分派等 C++ 逻辑用）和算子选择使用的便捷函数。两者当前都返回 64，但维护时要**同步修改**（否则会出现「target 说 64、算子分派说别的」的不一致）。

## 5. 综合实践：画一张 MACA 后端「四件套 + 三注册点」全栈图

把本讲四个最小模块串起来，完成一张端到端的 MACA 后端架构图，并标注每段对应的源码文件与永久链接。

**任务**：

1. 画一张从「用户写 kernel」到「kernel 跑在 MetaX GPU 上」的纵向流程图，包含以下节点（用方框），并用箭头连起来：
   - `@tilelang.jit` / `compile`
   - `determine_target` → `_detect_maca_target`（Python，线 A）
   - `lower`（U4-l1）
   - `resolve_device_codegen` → `target.build.tilelang_maca`（线 B）
   - `BuildTileLangMACA`（印源码 + postproc + compile 回调）
   - `MACAModuleCreate`（产出 mcbin 模块）
   - `MACAWrappedFunc` → `mcModuleLaunchKernel`（运行期启动）
2. 在每个方框旁标注它属于「target 注册 / device API / module 加载 / Python 注册」中的哪一类，以及对应源码文件。
3. 用红色标出 `warp_size=64` 影响到的所有位置（target kind 默认值、`TargetMacaGetWarpSize`、后续 GEMM 分派）。
4. 在图边写一句话：为什么「关掉 `USE_MACA` 编译，`import tilelang` 仍能识别 maca target 但无法真编译」？（提示：`TILE_LANG_MACA_ALWAYS_SRCS` 与条件编译两组文件的区别，见 [src/maca/CMakeLists.txt:3-26](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/CMakeLists.txt#L3-L26)）。

**验收标准**：图上至少出现 8 个源码锚点，且每个锚点都能点开永久链接；能口头解释「编译期链路」与「运行期链路」的分界（分界点就是 `MACAModuleCreate` 把 mcbin 交给 `MACAModule` 那一刻）。

## 6. 本讲小结

- **maca 是一等 target**：通过 `TVM_REGISTER_TARGET_KIND("maca", kDLMACA)` 注册，自带一张属性默认值表，最显眼的是 `thread_warp_size=64`（CUDA 为 32）。
- **canonicalizer 自动补属性**：`UpdateMACAAttrs` 在构造 `Target("maca")` 时补齐 `mtriple` 与 `mcpu`（缺则探测、兜底 `xcore1000`），保证任何输入形式行为一致。
- **device API 是 mc\* 的封装**：`MACADeviceAPI` 把通用 `DeviceAPI`（分配/拷贝/属性/计时）一一映射到 `mcMalloc`/`mcMemcpyAsync`/`mcDeviceGetAttribute`，注册为 `device_api.maca` 单例。
- **模块构建走 postproc/compile 回调**：`BuildTileLangMACA` 印源码后，通过 `tilelang_callback_maca_postproc`/`_compile` 两个 Python 注册的回调完成改写与 `mxcc` 编译，产出 `MACAModule`（持 mcbin，按设备懒加载）。
- **Python 三条注册线**：target detector（`_detect_maca_target`）、device codegen（`target.build.tilelang_maca`）、execution backend（tvm_ffi/mcrtc/cython/cutedsl），均在 `import tilelang` 时经 `from . import maca` 自动挂载。
- **条件编译的两层**：`TILE_LANG_MACA_ALWAYS_SRCS`（识别 target）总是编译，其余设备相关文件仅 `USE_MACA=ON` 时编译——这解释了「关 MACA 仍能识别 target 但不能真编译」。

## 7. 下一步学习建议

- **U7-l2 MACA codegen 实现**：本讲的 `BuildTileLangMACA` 只说了「印源码」，下一讲钻进 `CodeGenTileLangMACA` 看 visitor 如何处理存储作用域、向量化、warp shuffle 与 fastmath。
- **U7-l3 MACA MMA intrinsics（mfma）**：本讲提到 `warp_size=64`，下一讲看它如何具体影响 MFMA 指令的 warp 划分与 `mma_layout` 布局。
- **U9-l1 扩展：新增目标后端**：本讲把 MACA 当成「现成案例」读，U9-l1 会反过来以 MACA 为模板，教你怎么给 TileLang 添加一个全新的假想后端 `mygpu`。
- **延伸阅读源码**：`src/maca/runtime/maca_runtime.cc`（L2 persistent 策略，U7-l4 会用到）、`tilelang/contrib/mxcc.py`（`find_maca_path` 与 `mxcc` 编译的具体实现，本讲的 `tilelang_callback_maca_compile` 最终落到这里）。
