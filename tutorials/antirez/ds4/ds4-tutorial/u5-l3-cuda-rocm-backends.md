# CUDA 与 ROCm 后端

## 1. 本讲目标

本讲是「GPU 后端」单元的第三篇，承接 u5-l1（GPU 张量抽象与 CPU 后端）和 u5-l2（Metal 后端）。学完本讲后，你应该能够：

- 说清 `ds4_cuda.cu`（CUDA）和 `ds4_rocm.cu` + `rocm/*.cuh`（ROCm/HIP）这两套 GPU 后端的代码组织方式。
- 理解 ROCm 后端如何用一个「CUDA → HIP 宏垫片（shim）」让同一份运行时代码几乎逐字复用 CUDA 的 `cuda_*` API 名字。
- 指出三个 GPU 后端（Metal / CUDA / ROCm）如何复用同一份 `ds4_gpu.h` 接口，以及它们各自独有的链接库与构建目标。
- 看懂 CUDA 的「托管 KV（managed KV）」决策、模型零拷贝注册，以及 ROCm 在 Strix Halo（gfx1151）这类统一内存机器上的差异处理。

本讲不展开 MLA/MoE 的数学（见 u4-l1/u4-l2），也不重复 Metal 的运行时编译与图调度（见 u5-l2）。本讲聚焦于「CUDA 与 ROCm 这两个 *编译期* 后端如何落地 u5-l1 提出的张量常驻执行模型」。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（均在前面讲义中讲过）：

- **后端可替换、引擎核心稳定**（u1-l4）：Makefile 用 `CORE_OBJS = ds4.o + ds4_distributed.o + ds4_ssd.o + 一个后端 .o` 拼装每个前端二进制，第四个对象在 `ds4_metal.o` / `ds4_cuda.o` / `ds4_rocm.o` 之间切换。
- **`ds4_gpu.h` 张量常驻执行模型**（u5-l1）：设备张量 `ds4_gpu_tensor` 是不透明类型，对外只暴露 `alloc / view / read / write / copy` 等原语，以及 `begin_commands / flush_commands / end_commands` 命令缓冲生命周期；权重走 `model map` 零拷贝注册。
- **HIP 与 CUDA 的关系**：HIP 是 AMD 对标 CUDA 的运行时 API，函数命名一一对应（`cudaMalloc` ↔ `hipMalloc`、`cudaMemcpy` ↔ `hipMemcpy`）。HIP 的编译器 `hipcc` 在很多方面可以像 `nvcc` 一样使用。如果你没接触过 HIP，只需记住这一句：**HIP 刻意模仿 CUDA 的 API 形状，使得大量代码可以几乎不改地在两个平台间移植**。

两个本讲会用到的术语：

- **统一内存（Unified Memory / UMA）**：CPU 和 GPU 共享同一块物理内存（如 Apple Silicon、Strix Halo）。在这种机器上，「把权重从主机拷到设备」这件事语义上是多余的——两边本来就看到同一片 RAM。
- **托管内存（Managed Memory）**：CUDA 的 `cudaMallocManaged` 分配的内存，由驱动按需在主机与设备之间迁页，避免一次性占用全部显存。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 行数量级 | 作用 |
|------|---------|------|
| `ds4_gpu.h` | ~1024 行 | 三个 GPU 后端共同遵守的 C ABI「合同」：张量/命令/权重注册/算子声明。 |
| `ds4_cuda.cu` | ~1.3 万行 | CUDA 后端的*完整*实现（主机胶水 + `__global__` 内核），由 `nvcc` 编译成一个翻译单元。 |
| `ds4_rocm.cu` | ~130 行 | ROCm 后端的*伞状*翻译单元：设置宏、包含 `ds4_rocm.h`，然后 `#include` 全部 `rocm/*.cuh`。 |
| `ds4_rocm.h` | ~143 行 | CUDA → HIP 宏垫片：把 `cudaMalloc` 等名字宏替换成 `hipMalloc` 等。 |
| `rocm/ds4_rocm_runtime.cuh` | ~5000 行 | ROCm 后端运行时：`ds4_gpu_init`、张量原语、`set_model_map` 等，与 CUDA 对应函数几乎逐字相同。 |
| `rocm/ds4_rocm_attention.cuh` | ~1400 行 | ROCm 的 attention 内核（prefill/decode，raw/mixed KV）。 |
| `rocm/ds4_rocm_q8.cuh` / `rocm/ds4_rocm_moe.cuh` 等 | 多个 | ROCm 的算子模块，按功能切分（Q8 点积、MoE、indexer、compressor……）。 |
| `rocm/ds4_rocm_hipblaslt.cuh` | ~160 行 | ROCm *独有*：用 hipBLASLt 做专门的 F16 GEMM。 |
| `Makefile` | — | 定义 `cuda-spark` / `cuda-generic` / `strix-halo`（=`rocm`）三个 GPU 构建目标与各自链接库。 |
| `STRIXHALO.md` | — | Strix Halo（gfx1151，128GB UMA）上跑 ROCm 的最小配置说明。 |

一个关键的代码组织差异先记在脑子里：**CUDA 把所有东西塞进一个 1.3 万行的 `ds4_cuda.cu`；ROCm 把实现拆成二十多个 `rocm/*.cuh` 头文件，再用一个 130 行的 `ds4_rocm.cu` 把它们拼回一个翻译单元。** 两种做法最终都产出一个 `.o`，塞进 `CORE_OBJS` 的第四个槽位。

## 4. 核心概念与源码讲解

本讲按「先看共同合同 → 再看 CUDA 这一侧 → 再看 ROCm 这一侧」的顺序，对应三个最小模块：统一抽象、CUDA 后端、ROCm/HIP 后端。

### 4.1 统一抽象：三后端如何共用 ds4_gpu.h

#### 4.1.1 概念说明

回顾 u5-l1：ds4 的引擎核心（`ds4.c`）只认识 `ds4_gpu.h` 里声明的那套 C 函数，从不直接调用 Metal / CUDA / ROCm 的原生 API。这意味着 `ds4_gpu.h` 是一份**合同**：任何一个 GPU 后端，只要把合同里所有符号都实现出来，就能让 `ds4.c` 跑起来。

这份合同大体分四块：

1. **张量与命令生命周期**：`ds4_gpu_tensor_alloc`、`_view`、`_read`、`_write`、`_copy`、`begin_commands`、`flush_commands`、`end_commands`。
2. **权重注册**：`ds4_gpu_set_model_map`、`_set_model_map_range`、`_set_model_map_spans`、`_cache_model_range` 等——把 mmap 进来的 GGUF 权重交给后端做零拷贝寻址。
3. **算子**：一大类 `ds4_gpu_*_tensor` 函数（embedding、matmul、RMSNorm、RoPE、attention、compressor、router、MoE、HC……），每个对应推理图里的一个节点。
4. **流式专家缓存（SSD streaming expert cache）**：一组只在 SSD 流式模式下用的函数（见 u9-l1/u9-l2）。

关键观察：合同里**绝大多数**符号三个后端都要实现，但有少量符号被 `#ifdef DS4_ROCM_BUILD` 包起来，是 ROCm 专属的扩展点（见 4.3）。这说明这份合同不是绝对对称的——它在演化过程中给 ROCm 留了几个只为自己存在的钩子，但仍由同一份头文件统一管理。

#### 4.1.2 核心流程

一个 GPU 后端「落地」这份合同的流程，对三个后端是一致的：

```
引擎核心 ds4.c
    │  只调用 ds4_gpu.h 里的 C 函数
    ▼
ds4_gpu.h（合同：符号声明）
    │  同一套符号，三个实现二选一链入
    ├──► ds4_metal.o   （ds4_metal.m，u5-l2）
    ├──► ds4_cuda.o    （ds4_cuda.cu，本讲 4.2）
    └──► ds4_rocm.o    （ds4_rocm.cu + rocm/*.cuh，本讲 4.3）
```

构建期由 Makefile 决定链入哪一个 `.o`：在 Linux 上 `CORE_OBJS` 默认含 `ds4_cuda.o`；`make strix-halo` 把它换成 `ds4_rocm.o` 并改链接器为 `hipcc`；`make cpu` 则走 `-DDS4_NO_GPU` 的纯 CPU 路径（u5-l1）。运行期不存在「同时有两个 GPU 后端」——这是一次编译只选一个的「互斥单选」。

#### 4.1.3 源码精读

合同入口：`ds4_gpu.h` 顶部的张量与命令生命周期声明。

[ds4_gpu.h:11-39](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_gpu.h#L11-L39) — 注释明确写出执行模型是 **tensor-resident**（张量常驻设备）：激活、KV、scratch 在整个 prefill/decode 命令序列里始终留在设备上。下面跟着 `ds4_gpu_tensor` 不透明类型与一组 `alloc / view / free / read / write / copy` 原语。这三个后端实现的是同一组函数签名。

权重注册接口（三个后端共用同一组签名）：

[ds4_gpu.h:57-66](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_gpu.h#L57-L66) — `ds4_gpu_set_model_map` 把 mmap 的模型基址交给后端；中间几个 `#ifdef DS4_ROCM_BUILD` 块（如 `ds4_gpu_tensor_read_after_selected_event`、`ds4_gpu_release_q8_f16_cache`）是 ROCm 专属扩展，CUDA/Metal 后端不实现也不调用。

`#ifdef DS4_ROCM_BUILD` 在合同里反复出现，本讲后面会看到它把一份源码切成「通用 + ROCm 专属」两层的用法。

#### 4.1.4 代码实践

本讲的主实践任务就是规格里要求的这条「源码阅读 + Makefile 对照」：

1. **实践目标**：用 Makefile 证明「三个 GPU 后端复用同一套 `ds4_gpu.h` 接口」，并列出各自独有的链接库。
2. **操作步骤**：
   - 打开 `Makefile`，找到 `CORE_OBJS` 的两个定义（macOS 与 Linux 各一行）。
   - 找到 `cuda-spark` / `cuda-generic` / `strix-halo` 三个目标（约在 93–114 行）。
   - 找到 `CUDA_LDLIBS` 与 `ROCM_LDLIBS` 两个变量（约在 33、37 行），以及 `DS4_LINK` / `DS4_LINK_LIBS`（38–39 行）。
3. **需要观察的现象**：三个 GPU 目标都不重写 `ds4.o` 等核心对象，只切换 `CORE_OBJS` 里的「第四个对象」与链接器；链接库各不相同。
4. **预期结果**（应能自己填出下表）：

| 构建目标 | 第四个对象 | 链接器 | 独有链接库 |
|---------|-----------|--------|-----------|
| macOS 默认（Metal） | `ds4_metal.o` | 系统 `cc` | `-framework Foundation -framework Metal` |
| `cuda-spark` / `cuda-generic` | `ds4_cuda.o` | `nvcc` | `-lcudart -lcublas` |
| `strix-halo`（=`rocm`） | `ds4_rocm.o` | `hipcc` | `-lhipblas -lhipblaslt` |

5. 如果手头有相应硬件，可执行 `make cuda-generic -j$(nproc)` 或 `make strix-halo -j$(nproc)` 验证链接命令；否则**待本地验证**，纯阅读 Makefile 即可完成本实践。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `ds4_gpu.h` 里某个函数（例如 `ds4_gpu_matmul_f16_tensor`）从声明里删掉，但三个后端的 `.c/.cu/.m` 实现都还在，会发生什么？

> **参考答案**：引擎核心 `ds4.c` 里任何对该函数的调用都会在链接期报「未定义符号（undefined reference）」，因为头文件不再声明它，编译器在 `ds4.c` 里看不到原型，且没有任何 `.o` 被链入来提供这个符号（取决于编译器警告等级，可能先有隐式声明的编译警告）。这说明 `ds4_gpu.h` 既是合同也是唯一的「耦合面」。

**练习 2**：为什么 `CORE_OBJS` 里「第四个对象」是 GPU 后端，而不是把后端做成运行期插件？

> **参考答案**：因为 Metal / CUDA / ROCm 的原生 API（`<Metal/Metal.h>`、`cuda_runtime.h`、`<hip/hip_runtime.h>`）在**编译期**就需要各自的头文件与编译器（`clang -ObjC` / `nvcc` / `hipcc`），且后端内核用了各平台专属的语法（如 CUDA 的 `<<<...>>>` 启动、Metal 的 `.metal`）。把它们做成互斥的编译期单选，可以让每个二进制只携带一个平台的全套依赖，二进制更小、启动更简单。

### 4.2 CUDA 后端

#### 4.2.1 概念说明

CUDA 后端的全部实现集中在 `ds4_cuda.cu` 这一个文件里（约 1.3 万行），由 NVIDIA 的 `nvcc` 编译。它既包含主机侧胶水（实现 `ds4_gpu.h` 的每个符号），也包含 `__global__` 设备内核。这一节我们看三件最能体现「CUDA 落地张量常驻模型」的事：

1. **后端初始化**：建 cuBLAS 句柄、设数学模式。
2. **张量分配的两种途径**：普通设备内存 vs. 托管内存，以及「托管 KV」何时启用。
3. **权重零拷贝注册**：用 `cudaHostRegisterMapped` 让设备直接寻址 mmap 进来的 GGUF。

另外有一个体现「后端能力不对称」的小细节：Q4 专家表（expert table）预加载在 CUDA 上是一个**空操作桩（stub）**——它返回成功但什么都不做。这是 Metal/ROCm 才有的优化，CUDA 选择不实现，合同允许每个后端自行决定哪些「加速钩子」真正落地。

#### 4.2.2 核心流程

CUDA 后端的初始化与张量生命周期大致是：

```
ds4_gpu_init()
  ├─ cudaSetDevice(0)
  ├─ 打印设备名与 sm_ 版本
  ├─ cublasCreate(&g_cublas)
  └─ cublasSetMathMode(...)   # quality 模式用精确 FP32，否则开 TF32 加速

ds4_gpu_tensor_alloc(bytes)        # 一般张量：cudaMalloc（纯显存）
ds4_gpu_tensor_alloc_managed(bytes)# 托管张量：cudaMallocManaged（按需迁页）

ds4_gpu_should_use_managed_kv_cache(kv_bytes, ctx_bytes)
  # 决定 KV 缓存这一类「长寿、巨大」的分配是否走托管内存

ds4_gpu_set_model_map(map, size)
  ├─ 首选：cudaHostRegister(Mapped|ReadOnly) + cudaHostGetDevicePointer  # 零拷贝
  └─ 兜底：cudaMalloc + cudaMemcpy（DS4_CUDA_COPY_MODEL 时）或分块拷贝
```

「托管 KV」要不要启用，是一个关于显存预算的判断。直觉是：KV 缓存会随上下文增长，且生命周期贯穿整个会话；如果它大到挤占显存，就用托管内存让驱动按需在主机/设备间迁页，避免把机器卡死。形式化地，决策函数满足：

\[
\text{managed} =
\begin{cases}
1, & \text{kv\_bytes} \ge 8\,\text{GiB} \\
0, & \text{context\_bytes} < 8\,\text{GiB} \\
1, & \text{context\_bytes} > \text{free\_bytes} \\
1, & \text{free\_bytes} - \text{context\_bytes} < \text{reserve} \\
0, & \text{otherwise}
\end{cases}
\]

其中保留量 \(\text{reserve}\) 被钳在 \([8\,\text{GiB},\,40\,\text{GiB}]\)，取显存总量的四分之一。

#### 4.2.3 源码精读

CUDA 后端的初始化：

[ds4_cuda.cu:2255-2273](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cuda.cu#L2255-L2273) — `ds4_gpu_init` 选 0 号设备、打印 `sm_<major><minor>`、创建 cuBLAS 句柄。注意数学模式的选择：`g_quality_mode`（质量优先）或设置了 `DS4_CUDA_NO_TF32` 环境变量时用 `CUBLAS_DEFAULT_MATH`（精确 FP32），否则用 `CUBLAS_TF32_TENSOR_OP_MATH`（TF32 加速）。这是「正确性优先于速度」（见 u1-l1 的设计哲学）在代码里的直接体现——质量模式宁可慢也要更精确。

两种张量分配：

[ds4_cuda.cu:2348-2372](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cuda.cu#L2348-L2372) — `ds4_gpu_tensor_alloc` 用 `cudaMalloc`（设备显存），`ds4_gpu_tensor_alloc_managed` 用 `cudaMallocManaged`（托管内存）。两者都填同一个 `ds4_gpu_tensor` 结构（`ptr` / `bytes` / `owner`），对调用方完全透明——引擎核心不需要知道某块张量是哪一种。

托管 KV 决策：

[ds4_cuda.cu:2383-2408](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cuda.cu#L2383-L2408) — `ds4_gpu_should_use_managed_kv_cache` 把上面那条判定逻辑原样写出来：KV ≥ 8GiB 直接托管；上下文不足 8GiB 直接不托管；否则查 `cudaMemGetInfo` 的剩余显存，若上下文超出剩余、或剩余减去上下文小于保留量，就走托管。注释点明了动机：「巨大的 KV 缓存会让设备显存分配把统一内存机器卡死，托管内存只对这一类长寿分配恢复按页调入的老行为」。

权重零拷贝注册：

[ds4_cuda.cu:2550-2611](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cuda.cu#L2550-L2611) — `ds4_gpu_set_model_map` 的首选路径是 `cudaHostRegister(model_map, size, cudaHostRegisterMapped | cudaHostRegisterReadOnly)`，再 `cudaHostGetDevicePointer` 拿到设备侧地址，存进 `g_model_device_base`。之后所有算子用 `g_model_device_base + weight_offset` 直接寻址权重，**零拷贝**。如果设置了 `DS4_CUDA_COPY_MODEL`，则改成整模型 `cudaMalloc + cudaMemcpy` 一次性拷到显存；注册失败时还有分块拷贝/预取等兜底（见 2613 行的 `_set_model_map_range`）。这正是 u5-l1 讲的「权重走 model map 而非每次拷贝上传」。

Q4 专家表桩（体现后端能力不对称）：

[ds4_cuda.cu:2623-2640](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cuda.cu#L2623-L2640) — `ds4_gpu_pro_q4_expert_table_auto_available` 在 CUDA 上返回 `0`，`ds4_gpu_preload_q4_expert_tables` 把所有参数都 `(void)` 掉然后返回 `1`。也就是说 CUDA 后端「声明我不提供 Q4 专家表预加载」，但调用方不会因此失败——它只是拿不到这项加速。对照 u5-l1：PRO 模型的 Q4 专家表预加载是 Metal（以及 ROCm）的优化，CUDA 选择不实现。

#### 4.2.4 代码实践

这是一个「源码阅读 + 现象推演」型实践（无 GPU 也能做）：

1. **实践目标**：理解 CUDA 后端在「显存紧张」时如何通过托管 KV 自保。
2. **操作步骤**：
   - 阅读 `ds4_cuda.cu:2383-2408` 的 `ds4_gpu_should_use_managed_kv_cache`。
   - 在 `ds4.c` 中搜索它的调用点（`Grep "should_use_managed_kv_cache"`），看引擎核心在分配 KV 缓存前如何用这个返回值在 `ds4_gpu_tensor_alloc` 与 `ds4_gpu_tensor_alloc_managed` 之间二选一。
3. **需要观察的现象**：调用点会形如 `if (ds4_gpu_should_use_managed_kv_cache(kv_bytes, ctx_bytes)) kv = ds4_gpu_tensor_alloc_managed(kv_bytes); else kv = ds4_gpu_tensor_alloc(kv_bytes);`。
4. **预期结果**：你能解释「为什么这一步只对 KV 缓存做，而不对激活、scratch 也做」——因为只有 KV 是「随上下文单调增长、贯穿整个会话」的长寿分配，其它张量是每步临时分配释放的。
5. **待本地验证**：若你有 CUDA 机器，可在一个长上下文 prompt 上用 `nvidia-smi` 观察显存增长曲线，对比开/关托管 KV 时的差异。

#### 4.2.5 小练习与答案

**练习 1**：`ds4_gpu_init` 里 cuBLAS 数学模式的判定是 `(g_quality_mode || getenv("DS4_CUDA_NO_TF32") != NULL) ? CUBLAS_DEFAULT_MATH : CUBLAS_TF32_TENSOR_OP_MATH`。请用一句话解释这两个分支的取舍。

> **参考答案**：默认开 TF32（`TENSOR_OP_MATH`）拿速度、牺牲一点精度；当用户选了质量模式（`g_quality_mode`）或显式设置 `DS4_CUDA_NO_TF32` 时，退回精确 FP32（`DEFAULT_MATH`）以保证数值更贴官方实现——再次体现「正确性优先于速度」。

**练习 2**：`ds4_gpu_set_model_map` 注册失败时，代码并不立刻 `return 0`，而是进入一个「检查模型是否超过单 GPU 启动缓存预算」的分支（2599–2608 行）。这样设计的好处是什么？

> **参考答案**：它把「注册失败」分成两类——可恢复（环境/驱动暂时不让零拷贝，但模型仍在预算内，可走兜底拷贝）与不可恢复（模型太大，单 GPU 装不下，应直接报错并提示改用分布式层加载或调大 `DS4_CUDA_WEIGHT_CACHE_LIMIT_GB`）。这样既能尽量救回一次推理，又能在真的装不下时给出可操作的错误信息而不是悄悄 OOM。

### 4.3 ROCm/HIP 后端

#### 4.3.1 概念说明

ROCm 后端面向 AMD GPU，在 ds4 里专门为 **Strix Halo（gfx1151，128GB 统一内存）** 这类机器调优。它的代码组织与 CUDA 完全不同：

- 实现被拆成二十多个 `rocm/*.cuh` 头文件，每个负责一块算子（attention、q8、moe、indexer、compressor、router、hipblaslt……）。
- 一个 130 行的伞状翻译单元 `ds4_rocm.cu` 用 `#include` 把它们全部拼回一个翻译单元，由 AMD 的 `hipcc` 编译成 `ds4_rocm.o`。

ROCm 后端最巧妙、最值得理解的一点是 **「CUDA → HIP 宏垫片」**：`rocm/ds4_rocm_runtime.cuh` 里的运行时代码几乎逐字照抄 CUDA 版（同样写 `cudaMalloc`、`cudaMemcpy`、`cublasCreate`），但在 ROCm 编译里，`ds4_rocm.h` 用一组 `#define` 把这些名字宏替换成对应的 HIP 名字（`hipMalloc`、`hipMemcpy`、`hipblasCreate`）。于是同一份「长得像 CUDA」的代码，经过预处理器后变成了合法的 HIP 代码。这就是为什么 `ds4_rocm_runtime.cuh` 与 `ds4_cuda.cu` 里 `ds4_gpu_init`、`should_use_managed_kv_cache` 等函数**看起来几乎一模一样**——它们本来就是同一份逻辑，只是一个被 nvcc 编译、一个被 hipcc 编译。

此外，ROCm 后端有几个 CUDA 没有的「专属件」：

- **hipBLASLt**：第二个 GEMM 句柄，专门跑 F16 矩阵乘（`rocm/ds4_rocm_hipblaslt.cuh`），链接 `-lhipblaslt`。
- **rocWMMA**：AMD 的张量核心矩阵乘抽象，用在 Q8 内核里做分块 MMA（`rocm/ds4_rocm_q8.cuh`）。
- **统一内存（UMA）上的权重处理**：Strix Halo 的 CPU/GPU 共享 DDR5，所以不沿用 CUDA 的 `cudaHostRegisterMapped`，而是走「分阶段拷贝进设备镜像」的路径。

#### 4.3.2 核心流程

ROCm 后端的组装链路：

```
ds4_rocm.cu（hipcc 编译，-D__HIP_PLATFORM_AMD__）
  ├─ #include "ds4_rocm.h"          # 宏垫片：cuda* → hip*
  ├─ 定义 DS4_GPU_BACKEND_NAME "ROCm"
  ├─ #include "ds4_iq2_tables_cuda.inc"
  └─ #include "rocm/ds4_rocm_runtime.cuh"   # 运行时（init/张量/set_model_map…）
       ├─ #include "rocm/ds4_rocm_attention.cuh"
       ├─ #include "rocm/ds4_rocm_q8.cuh"   # 用 rocwmma::fragment
       ├─ #include "rocm/ds4_rocm_moe.cuh"
       ├─ #include "rocm/ds4_rocm_hipblaslt.cuh"  # hipBLASLt GEMM
       └─ …（二十多个模块）
              ▼
       产出 ds4_rocm.o，链入 CORE_OBJS 第四槽位
```

宏垫片的工作机制（预处理器视角）：

```
源代码写的：     cudaMalloc(&p, n)
ds4_rocm.h：     #define cudaMalloc hipMalloc
预处理后：       hipMalloc(&p, n)        # 合法 HIP 调用
```

这套命名映射覆盖了运行时（`cudaMalloc`/`cudaFree`/`cudaMemcpy`…）、句柄（`cublasHandle_t`→`hipblasHandle_t`）、错误码（`cudaSuccess`→`hipSuccess`）乃至命名空间（`namespace cub = hipcub;`）。垫片还顺手补了几个 CUDA 内建函数在 AMD 上的等价物，最典型的是 `__dp4a`（4 路 int8 点积）——它在 gfx11+ AMD GPU 上对应单条 `v_dot4_i32_i8` 指令。

#### 4.3.3 源码精读

ROCm 翻译单元的「分叉入口」：

[ds4_rocm.cu:1-22](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_rocm.cu#L1-L22) — 当 `__HIP_PLATFORM_AMD__` 已定义（hipcc + `-D__HIP_PLATFORM_AMD__`）时，包含 `ds4_rocm.h` 宏垫片与 hipblaslt，并把后端名设为 `"ROCm"`、日志前缀设为 `"ds4: ROCm "`。注意：`#else` 分支里的 CUDA 头文件与 `"CUDA"` 名字在 ROCm 构建里是死代码（hipcc 编译时这个文件只走 `#if` 分支）。

伞状包含列表：

[ds4_rocm.cu:92-131](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_rocm.cu#L92-L131) — 这里一次性 `#include` 了全部 `rocm/*.cuh`，顺序有讲究：先 runtime/common/q8/norm_rope/fp8_kv 这些基础件，再 attention/hc/output/indexer，最后 moe 及其 launch 胶水。这等价于把 CUDA 那个 1.3 万行大文件「按模块拆开存放、编译时再拼回去」。

宏垫片本体：

[ds4_rocm.h:10-93](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_rocm.h#L10-L93) — 一长串 `#define cudaMalloc hipMalloc` 之类的映射。包括设备管理（`cudaSetDevice`→`hipSetDevice`）、内存（`cudaMalloc`/`cudaFree`/`cudaMemcpy`…）、流与事件（`cudaStream_t`→`hipStream_t`…）、以及 cuBLAS 句柄与常量（`cublasHandle_t`→`hipblasHandle_t`、`CUBLAS_OP_N`→`HIPBLAS_OP_N`…），最后还有 `namespace cub = hipcub;` 让 CUB 风格的设备端工具换成 hipCUB。

`__dp4a` 的 AMD 内建实现：

[ds4_rocm.h:113-131](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_rocm.h#L113-L131) — 注释解释：gfx11 级 AMD GPU 把 4 路 int8 点积暴露为单条 `v_dot4_i32_i8` 指令，所以在 AMD 分支用 clang 内建 `amd_mixed_dot`，避免把每个 Q8/Q8_K 点积展开成一堆标量字节乘法。这正是 u3-l4 讲的「整数乘加」点积在 AMD 上的硬件加速点。

ROCm 的初始化（带 hipBLASLt 这个 CUDA 没有的额外件）：

[rocm/ds4_rocm_runtime.cuh:4380-4402](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/rocm/ds4_rocm_runtime.cuh#L4380-L4402) — 与 CUDA 版几乎逐字相同（同样 `cudaSetDevice`/`cublasCreate`——记得这些名字已被垫片换成 `hip*`），唯一结构差异是 `#ifdef __HIP_PLATFORM_AMD__` 块里多创建了 `g_hipblaslt` 句柄。日志用的是 `DS4_GPU_LOG_PREFIX` 宏，在 ROCm 下展开成 `"ds4: ROCm "`。

ROCm 的权重处理（UMA 上不 host-register）：

[rocm/ds4_rocm_runtime.cuh:4604-4640](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/rocm/ds4_rocm_runtime.cuh#L4604-L4640) — ROCm 版的 `ds4_gpu_set_model_map` **没有**像 CUDA 那样调 `cudaHostRegisterMapped`。注释（4636–4639 行）点明原因：Strix Halo 用 `_set_model_map_range` 里的「分阶段全拷贝」路径，若在这里 host-register，会让分阶段拷贝器误以为模型已经在设备上了。设备地址由 `cuda_model_image_owned`/`cuda_model_image_ptr` 决定——在 UMA 上这往往就是同一片主机内存。另外 4607–4623 行处理「多模型（MTP 第二个 GGUF）」场景：在 UMA 上主动关掉可选的 Q8→F16 展开缓存，给会话/上下文张量留出内存余量。

ROCm 专属：hipBLASLt GEMM：

[rocm/ds4_rocm_hipblaslt.cuh:48-67](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/rocm/ds4_rocm_hipblaslt.cuh#L48-L67) — 直接用 `hipblasLtMatmulDescCreate` / `hipblasLtMatrixLayoutCreate` 等**原生 hipBLASLt 符号**（不是宏垫片来的），布局是 `HIP_R_16F`（F16 输入）、`HIPBLAS_COMPUTE_32F`（FP32 累加）。这是 ROCm 后端独有的专门化 F16 GEMM 路径，CUDA 上对应功能由 cuBLAS 承担。

ROCm 专属：rocWMMA 张量核心：

[rocm/ds4_rocm_q8.cuh:802-844](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/rocm/ds4_rocm_q8.cuh#L802-L844) — 在 `#if defined(__HIP_PLATFORM_AMD__)` 保护下，用 `rocwmma::fragment<...>` 描述矩阵分片（A/B 行主序 half，C 累加器 float），再用 `rocwmma::load_matrix_sync` / `mma_sync` / `store_matrix_sync` 做分块矩阵乘。这是 AMD 张量核心的 WMMA 接口，对应 CUDA 侧的 `nvcuda::wmma`。

#### 4.3.4 代码实践

这条实践帮你亲手「跑一遍」宏垫片，把抽象的 `#define` 变成可见的预处理结果（无 AMD GPU 也能做，只要有 hipcc；没有 hipcc 则纯阅读）：

1. **实践目标**：直观看到 `cudaMalloc` 在 ROCm 编译里是如何变成 `hipMalloc` 的。
2. **操作步骤**：
   - 阅读 `ds4_rocm.h:10-50`，把其中关于内存分配/拷贝的几条 `#define` 抄下来。
   - 若装了 ROCm（参考 `STRIXHALO.md` 第 1 节的 `hipcc` 安装），执行预处理只看效果：
     `hipcc -D__HIP_PLATFORM_AMD__ -E ds4_rocm.cu -o /tmp/rocm_pp.txt`（可能需要补 `-I` 与某些 `-D`，以本机为准）。
   - 在产物里 `grep` `ds4_gpu_init` 附近的调用，确认 `cudaMalloc` 已被替换为 `hipMalloc`。
3. **需要观察的现象**：预处理后的文本里几乎不再有 `cuda*` 字样（除非是注释或字符串），取而代之的是 `hip*`；`cublasCreate` 变成了 `hipblasCreate`。
4. **预期结果**：你能用一句话向别人解释「ROCm 后端为什么能在源码层面与 CUDA 共享那么多代码」——因为预处理器先把 CUDA 风格的名字改写成 HIP 名字，再交给 hipcc 编译。
5. **待本地验证**：没有 hipcc 时，可改成在 `ds4_rocm.h` 里手动跟踪某一条 `#define`，在脑中把 `rocm/ds4_rocm_runtime.cuh:4477` 的 `cudaMalloc` 改写一遍，结论一致。

#### 4.3.5 小练习与答案

**练习 1**：`ds4_rocm_runtime.cuh` 的 `ds4_gpu_init` 与 `ds4_cuda.cu` 的 `ds4_gpu_init` 几乎逐字相同，都写了 `cudaSetDevice`、`cublasCreate`。既然如此，为什么作者不直接 `#include` 同一个 `.c` 文件来彻底消除重复？

> **参考答案**：两份代码「几乎」相同但「不是完全」相同——ROCm 版多了 `g_hipblaslt` 句柄的创建（`#ifdef __HIP_PLATFORM_AMD__` 块），且后续的算子实现（attention、MoE 等）在两个平台上差异很大（不同的张量核心 API、不同的内存模型）。维护两份高度相似但各有专属逻辑的运行时，比强行用一堆 `#ifdef` 把两套实现焊进同一份源文件更清晰、更易读，也降低了「改一处坏另一处」的风险。

**练习 2**：在 Strix Halo（统一内存）上，ROCm 版 `ds4_gpu_set_model_map` 不调用 `cudaHostRegisterMapped`。如果不小心调用了，会发生什么？

> **参考答案**：在 UMA 上，主机内存与设备内存本就共享同一片物理 RAM，`host register + get device pointer` 的语义意义不大；更糟的是，分阶段拷贝器（`_set_model_map_range`）会检查「模型是否已设备驻留」来决定要不要拷贝，host-register 会让它误判为「已经在设备上」而跳过拷贝，可能导致后续按设备镜像寻址的算子读到未初始化的数据。注释（4636–4639 行）正是为了挡住这个陷阱。

**练习 3**：`ds4_rocm.h` 里 `__dp4a` 在 AMD 分支用 `amd_mixed_dot`，在非 AMD 分支退化成 4 次标量乘加。这对 Q8/Q8_K 点积（u3-l4）的性能意味着什么？

> **参考答案**：在 AMD GPU 上，4 路 int8 点积折叠成单条 `v_dot4_i32_i8` 硬件指令，Q8/Q8_K 点积的核心循环会被大幅加速；若退化成标量路径，每个块 256 个 int8 要展开成大量标量乘法，吞吐骤降。所以这个内建是 ROCm 后端在 Q8 量化上能跑出可用速度的关键之一。

## 5. 综合实践

把本讲三块内容串起来：**画一张「一份合同、三个实现」的全景图，并指出 CUDA 与 ROCm 在落地同一合同时的全部关键差异。**

建议步骤：

1. 在一张图上画出 `ds4.c`（引擎核心）→ `ds4_gpu.h`（合同）→ 三个后端 `.o`（Metal/CUDA/ROCm）的依赖关系，标注每个后端的编译器（clang-ObjC / nvcc / hipcc）。
2. 在 CUDA 与 ROCm 两个分支旁，列出本讲讲到的全部差异点：
   - 代码组织：单文件 vs. 伞状 `#include` 多头。
   - 链接库：`-lcudart -lcublas` vs. `-lhipblas -lhipblaslt`。
   - 复用机制：无（CUDA 原生）vs. `ds4_rocm.h` 宏垫片。
   - 专属件：无 hipBLASLt/rocWMMA vs. 有；Q4 专家表桩 vs. 真实现（Metal/ROCm）。
   - 权重注册：`cudaHostRegisterMapped` 零拷贝 vs. UMA 分阶段拷贝、不 host-register。
   - 多模型（MTP）：默认行为 vs. 关闭 Q8→F16 缓存省内存。
3. 用 Makefile 自证你的图：在 `Makefile` 里找到 `CORE_OBJS` 的两个定义、`CUDA_LDLIBS`、`ROCM_LDLIBS`、`DS4_LINK`、以及 `cuda-spark` / `strix-halo` 两个目标的 `$(MAKE)` 调用，把它们与你图上的箭头一一对应。
4. 最后写一句话结论：**「三个 GPU 后端是同一份 `ds4_gpu.h` 合同的三个编译期互斥实现；CUDA 与 ROCm 之所以能共享大量代码，靠的是 HIP 模仿 CUDA 的 API 形状 + 一个预处理期宏垫片，而非运行期插件。」**

如果你有相应硬件，可以把第 3 步升级为实跑：分别 `make cuda-generic` 与 `make strix-halo`，用 `ldd ds4` 查看最终二进制依赖的共享库，确认 CUDA 版链到 `libcudart`/`libcublas`、ROCm 版链到 `libhipblas`/`libhipblaslt`；否则**待本地验证**，纯阅读 Makefile 即可完成。

## 6. 本讲小结

- `ds4_gpu.h` 是三个 GPU 后端共同遵守的 C ABI 合同；引擎核心 `ds4.c` 只认识这组合同符号，后端是「编译期互斥单选」。
- CUDA 后端实现集中在单个 `ds4_cuda.cu`（nvcc 编译），包含主机胶水与 `__global__` 内核，链接 `-lcudart -lcublas`。
- CUDA 用 `cudaHostRegisterMapped` 做权重零拷贝，用 `ds4_gpu_should_use_managed_kv_cache` 决定 KV 缓存这类长寿大分配是否走托管内存；Q4 专家表预加载在 CUDA 上是空操作桩。
- ROCm 后端用 130 行的 `ds4_rocm.cu`（hipcc 编译）伞状 `#include` 二十多个 `rocm/*.cuh`，链接 `-lhipblas -lhipblaslt`。
- ROCm 复用 CUDA 风格代码的关键是 `ds4_rocm.h` 宏垫片：把 `cudaMalloc` 等名字在预处理期改写成 `hipMalloc` 等，让「长得像 CUDA」的代码变成合法 HIP 代码。
- ROCm 有 CUDA 没有的专属件（hipBLASLt 专门化 GEMM、rocWMMA 张量核心、`__dp4a`→`v_dot4_i32_i8`），并在 Strix Halo 这类统一内存机器上改走「分阶段拷贝、不 host-register」的权重路径。

## 7. 下一步学习建议

本讲把「三个 GPU 后端如何落地同一合同」讲完。接下来：

- **进入高级推理路径**：去 u6-l1（分块 prefill 主路径），看引擎核心如何驱动这些后端算子完成一次长 prompt 的 prefill——你会看到 `DS4_METAL_PREFILL_CHUNK` 与本讲的 KV/权重内存决策如何交织。
- **SSD 流式专家缓存**：`ds4_gpu.h` 里那一组流式专家缓存函数（`ds4_gpu_stream_expert_cache_*`）会在 u9-l1/u9-l2 详讲；届时你会看到本讲提到的「模型 map / 权重缓存」在显存装不下时如何退化为按需从 SSD 读专家。
- **若你想加深对某一后端内核的理解**：CUDA 侧可挑 `ds4_cuda.cu` 里的某个 attention/MoE 内核精读；ROCm 侧可从 `rocm/ds4_rocm_attention.cuh`（本讲已看开头）顺读到 mixed/indexed KV 内核，并对照 u4-l2 的 KV 缓存设计理解它在算什么。
