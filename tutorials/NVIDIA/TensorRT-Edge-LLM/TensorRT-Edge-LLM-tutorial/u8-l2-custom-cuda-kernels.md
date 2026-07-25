# 自定义 CUDA 算子（FMHA/MoE/Mamba）

> 本讲承接 **u8-l1（TensorRT 插件架构）** 与 **u5-l5（KV 缓存与混合缓存管理）**。
> 上一讲我们讲了插件「壳」（Plugin + Creator 两件套、`enqueue` 执行入口）；本讲打开 `enqueue` 内部，看真正干活的 CUDA 算子族——它们从哪里来、按什么逻辑分发、为什么必须按 GPU 架构（SM）单独构建。

## 1. 本讲目标

学完本讲你应该能够：

1. 说清楚 `cpp/kernels/`（随仓库编译的 `.cu` 源）与 `kernelSrcs/`（独立生成产物）两套算子来源的分工。
2. 区分 **context（prefill）FMHA** 与 **decode（XQA）注意力** 两类注意力算子，并解释为什么 decode 能与 KV 缓存结合实现单 token 高效注意力。
3. 读懂 MoE 算子族的流水线：topk 选路 → 路由元数据 → 按 expert 分组 GEMM → 激活 → 散回。
4. 理解 FMHA / XQA / CuTe DSL 三套「SM 特异性构建」机制，并能回答「新增一个 SM 架构要改哪些配置」。

## 2. 前置知识

本讲默认你已经掌握：

- **三段式流水线**（u1-l2）：检查点 → Python 导出 ONNX → C++ 构建 engine → C++ 运行时推理。
- **TensorRT 插件两件套**（u8-l1）：EdgeLLM 的融合算子在 ONNX 里是自定义节点，落到 C++ 就是 TRT 插件，`enqueue` 里调用的就是本讲的 CUDA 算子。
- **KV 缓存**（u5-l5）：attention 层有一块随序列长度线性增长的 `[batch, 2, numKVHeads, maxSeqLen, headDim]` 缓存，prefill 写、decode 读。
- **prefill / decode 双 profile**（u4-l2、u5-l1）：一次推理被拆成「一次 prefill（吃整条 prompt）」加「若干次 decode（每次吃 1 个 token）」。

几个需要先建立的术语：

| 术语 | 含义 |
|------|------|
| **SM** | Streaming Multiprocessor，GPU 的流式多处理器；SM 版本号（如 sm_89、sm_110）对应 GPU 架构（Ampere/Ada/Blackwell…）。算子的指令调度、张量核心形状都随 SM 不同。 |
| **cubin** | CUDA 二进制内核（`.cubin`），一段已针对特定 SM 编译好的 GPU 机器码。EdgeLLM 把高优算子预先编译成 cubin，运行时按 SM 选对应的加载。 |
| **FMHA** | Fused Multi-Head Attention，融合多头注意力，把 \(QK^\top \to\) softmax \(\to PV\) 融成一个 kernel。 |
| **XQA** | eXtended Query Attention，TensorRT-LLM 系列的 decode 阶段注意力 kernel 族，专门为「query 很短、KV 很长」的场景优化。 |
| **GQA** | Grouped-Query Attention，多个 query head 共享一组 KV head（numKVHeads < numQHeads）。 |
| **CuTe DSL** | NVIDIA CUTlass Tensor DSL，一种用 Python 描述、AOT 编译出 per-SM 静态库的高性能算子生成框架。 |

## 3. 本讲源码地图

本讲涉及的源码分三层：**算子源码**、**算子生成脚本**、**构建配置**。

| 文件 | 作用 |
|------|------|
| [`cpp/CMakeLists.txt`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/CMakeLists.txt) | 定义 `edgellmKernels` 等静态库、FMHA cubin 的 SM 排除逻辑、接入 XQA 与 CuTe DSL。 |
| [`cmake/XQACubins.cmake`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cmake/XQACubins.cmake) | 构建期调用 `gen_cubins.py` 为 decode 注意力生成 per-SM cubin。 |
| [`cmake/CuteDsl.cmake`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cmake/CuteDsl.cmake) | 选择 per-SM CuTe DSL 算子产物（FMHA/GDN/MoE…），按 group 设编译宏。 |
| [`kernelSrcs/README.md`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/kernelSrcs/README.md) | `build_cutedsl.py` 统一入口说明。 |
| [`kernelSrcs/fmha_v2/README.md`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/kernelSrcs/fmha_v2/README.md) | 预编译 context FMHA cubin 的流程（按 SM 分轮、CUDA 版本区分）。 |
| [`kernelSrcs/build_cutedsl.py`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/kernelSrcs/build_cutedsl.py) | 算子 group 与 variant 的注册表。 |
| [`cpp/kernels/contextAttentionKernels/contextFMHARunner.h`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/kernels/contextAttentionKernels/contextFMHARunner.h) | prefill 阶段 FMHA 的运行器（选 cubin / CuTe DSL FMHA）。 |
| [`cpp/kernels/decodeAttentionKernels/decoderXQARunner.h`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/kernels/decodeAttentionKernels/decoderXQARunner.h) | decode 阶段 XQA 的运行器，直接读 KV 缓存。 |
| [`cpp/kernels/moe/f16MoeSupportKernels.cu`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/kernels/moe/f16MoeSupportKernels.cu) | FP16 MoE 的「非 GEMM」支撑算子（路由、gather、激活、scatter）。 |
| [`cpp/kernels/moe/f16MoeSupportKernels.h`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/kernels/moe/f16MoeSupportKernels.h) | 上面那组算子的数据结构与接口。 |
| [`cpp/kernels/moe/moeTopkSoftmaxKernels.h`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/kernels/moe/moeTopkSoftmaxKernels.h) | MoE 门控的 topk + softmax。 |

## 4. 核心概念与源码讲解

### 4.1 算子的两副面孔：`cpp/kernels/` 与 `kernelSrcs/`

#### 4.1.1 概念说明

EdgeLLM 的 CUDA 算子来自两个目录，理解它们的分工是本讲的总钥匙：

- **`cpp/kernels/`**：随主仓库用 `nvcc` 一起编译的「普通」CUDA 源（`.cu` / `.cpp`）。它们被 `GLOB` 进 `edgellmKernels` / `edgellmCore` 静态库，最终链进插件共享库 `NvInfer_edgellm_plugin`。这些算子大多是**架构无关**或**用宏按 SM 分支**的通用实现，例如 MoE 的路由/激活/散回、RoPE、KV 缓存工具、投机解码的 accept 算子。

- **`kernelSrcs/`**：**最高性能、强 SM 特异性**的算子在这里以「源 + 生成脚本」的形式存在，产物是预先编译好的二进制（cubin）或 CuTe DSL 静态库，**不进常规 `nvcc` 编译**，而是由独立的 Python 脚本生成、再被 CMake 当作外部产物链接。`AGENTS.md` 把它列为关键设计点：

  > FMHA kernels are SM-specific — built per SM arch.
  > —— [AGENTS.md:97](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/AGENTS.md#L97)

为什么要分两套？因为像 FMHA、XQA、NVFP4 MoE 这类算子，要把每一拍寄存器分配、张量核心排布、shared memory tiling 都榨干，必须**针对每个 SM 单独手写/生成**；而且这类 kernel 往往依赖 CUTlass/CuTe DSL 这套独立的代码生成管线，不适合塞进普通 `nvcc` 流程。而 MoE 路由、RoPE 这类访存密集型、逻辑相对简单的算子，一份 `.cu` 跨架构编译就够用，于是放在 `cpp/kernels/`。

#### 4.1.2 核心流程

整个算子层的目录划分正好对应「注意力 / MoE / 位置编码 / SSM / 投机解码」这几条数据通路（[AGENTS.md:52](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/AGENTS.md#L52)）：

| `cpp/kernels/` 子目录 | 干什么 | 来源类型 |
|---|---|---|
| `contextAttentionKernels/` | prefill FMHA（flash 风格） | 预编译 cubin + CuTe DSL FMHA |
| `decodeAttentionKernels/` | decode XQA | 构建期生成 cubin |
| `posEncoding/` | RoPE、cos/sin 缓存初始化、把 K/V 写进 KV 缓存 | 普通 `.cu` |
| `moe/` | topk、路由、per-expert scale、激活、散回 | 普通 `.cu` + CuTe DSL grouped GEMM |
| `mamba/` | Mamba/SSM 的 conv、state 更新 | 普通 `.cu` + CuTe DSL SSD |
| `gdnKernels/` | Gated Delta Net（Mamba2 式线性注意力） | CuTe DSL GDN |
| `speculative/` | EAGLE accept、DFlash、DDTree、Gemma4-MTP、batch evict | 普通 `.cu` |
| `kvCacheUtilKernels/` | KV 缓存搬运/转置 | 普通 `.cu` |

对应地，`kernelSrcs/` 提供强 SM 特异性的「重武器」：`fmha_v2/`（预编译 cubin）、`xqa/`（decode cubin 源）、`fmha_cutedsl_blackwell/`、`gdn_cutedsl/`、`gemm_cutedsl/`、`f16_moe_cutedsl/`、`nvfp4_moe_cutedsl/`、`ssd_cutedsl/`、`ffpa_cutedsl/`（[kernelSrcs/build_cutedsl.py:16-30](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/kernelSrcs/build_cutedsl.py#L16-L30)）。

一个判断口诀：**「算子要不要按 SM 单独出一份二进制」**决定了它住 `cpp/kernels/` 还是 `kernelSrcs/`。

#### 4.1.3 源码精读

**`cpp/kernels/` 是如何被收进静态库的**——注意它用递归 `GLOB` 抓所有 `.cpp/.cu`，并显式排除了 Marlin 的两个 `.cu`（它们靠 `#include` 而非独立编译，且开了 `-rdc=true` 可重定位设备码，[cpp/CMakeLists.txt:86-102](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/CMakeLists.txt#L86-L102)）：

```cmake
file(GLOB_RECURSE KERNELS_CPP_SRCS "kernels/*.cpp")
file(GLOB_RECURSE KERNELS_CU_SRCS "kernels/*.cu")
# 排除 Marlin 的 ops/sm80_kernel（靠 include 进 moeMarlin.cu）
list(FILTER KERNELS_CU_SRCS EXCLUDE REGEX
     "marlin_moe_wna16/(ops|sm80_kernel_float16_u4_float16)\\.cu$")
...
add_library(edgellmKernels STATIC ${KERNELS_CPP_SRCS} ${KERNELS_CU_SRCS} ${COMMON_CPP_SRCS})
```

**`kernelSrcs/` 的统一入口**是 `build_cutedsl.py`，它把每个算子族注册成 `(group, variant, script, supported_sms)`，生成 per-SM 静态库到 `cpp/kernels/cuteDSLArtifact/<arch>/<artifact_tag>/`，[kernelSrcs/README.md:8-21](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/kernelSrcs/README.md#L8-L21)：

```text
cpp/kernels/cuteDSLArtifact/<arch>/<artifact_tag>/
  libcutedsl_<arch>.a
  metadata.json        # 记录 gpu_arch / groups / variants，供 CMake 读取
  include/cutedsl_all.h
```

`artifact_tag` 形如 `sm_80`、`sm_110`、`sm_121`——**一个 SM 一份产物**，这正是「SM 特异性」在文件系统上的体现。

#### 4.1.4 代码实践

**实践目标**：用目录游览建立「哪个算子住哪儿」的直觉。

**操作步骤**：

1. 在仓库根目录执行（只读操作）：
   ```bash
   ls cpp/kernels/
   ls kernelSrcs/
   ```
2. 对照本节两张表，给每个 `cpp/kernels/` 子目录标注它「是否依赖 SM 特异性产物」。
3. 进 `cpp/kernels/cuteDSLArtifact/` 看是否存在本地生成的 per-SM 目录；若无，看 `kernelSrcs/cuteDSLPrebuilt/` 里的预编译 tar 包名。

**需要观察的现象**：

- `cpp/kernels/` 下能看到 `contextAttentionKernels/cubin/`、`moe/`、`posEncoding/` 等子目录。
- `kernelSrcs/cuteDSLPrebuilt/` 里能看到形如 `cutedsl_aarch64_sm_110_cuda13.tar.gz` 的包，文件名直接编码了 **CPU 架构 + SM + CUDA 版本**三元组——一份产物绑一个 SM。

**预期结果**：你能口头说出「FMHA 在 `contextAttentionKernels`、decode 在 `decodeAttentionKernels`、RoPE 在 `posEncoding`、EAGLE accept 在 `speculative`」。**待本地验证**：`cuteDSLArtifact/` 默认不进 git，本地未跑过 `build_cutedsl.py` 时该目录可能为空（由 CMake 自动解压预编译包，见 4.4）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Marlin 的 `ops.cu` 被 `list(FILTER ... EXCLUDE)` 掉，却仍能被编译进二进制？
**答案**：它不作为独立翻译单元编译，而是被 `moeMarlin.cu` 用 `#include` 拉进来；同时 Marlin 模板要求可重定位设备码（`-rdc=true`，见 [cpp/CMakeLists.txt:99-102](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/CMakeLists.txt#L99-L102)），独立编译会丢失模板实例化。

**练习 2**：判断「把一个新写的访存密集型 RoPE 变体」应该放 `cpp/kernels/posEncoding/` 还是 `kernelSrcs/`？
**答案**：放 `cpp/kernels/posEncoding/`。RoPE 是访存密集、逻辑通用、不强依赖张量核心排布的算子，一份 `.cu` 跨 SM 编译即可，不需要 per-SM cubin。

---

### 4.2 FMHA 算子：context（prefill）与 decode 两类

#### 4.2.1 概念说明

注意力是 LLM 推理里最贵的算子，而 prefill 和 decode 的「形状」截然不同，所以 EdgeLLM 用**两类完全不同的 kernel**：

- **context（prefill）FMHA**：一次喂进整条 prompt，query 序列长度 \(S_q\) 很大（可能上千）。这是一个「大方阵」问题，用 flash-attention 风格的分块融合 kernel，把 \(Q,K,V \in \mathbb{R}^{S\times d}\) 一次性算完。对应 `ContextFMHARunner`。

- **decode（XQA）注意力**：decode 阶段每步只有 **1 个新 query token**（投机解码的树形/链式提议也只是几个 token），但它要对**迄今为止的全部历史 key/value** 做注意力——而这些历史 KV 全都躺在 **KV 缓存**里。这是一个「query 极短、KV 极长」的问题，专门的 **XQA kernel** 直接从 KV 缓存读、只算新增 query 对全部 key 的注意力。对应 `DecoderXQARunner`。

记一个核心对比：

\[
\text{prefill: } Q\in\mathbb{R}^{S_q\times d},\ K,V\in\mathbb{R}^{S_q\times d}
\quad\text{vs}\quad
\text{decode: } Q\in\mathbb{R}^{1\times d},\ K,V\in\mathbb{R}^{S_{\text{past}}\times d}\text{（在缓存里）}
\]

#### 4.2.2 核心流程

两者的执行链路都由注意力插件（u8-l1 的 `AttentionPlugin::enqueue`）驱动，但分发到不同 runner：

```
Attention 插件 enqueue
        │
        ├── prefill（profile 0，序列长）→ ContextFMHARunner.dispatchFMHAKernel()
        │        └── 按 headSize/layout/mask 在 cubin 表里选一个 flash kernel
        │            （或 Blackwell 上走 CuTe DSL FMHA）
        │
        └── decode（profile 1，query=1）→ DecoderXQARunner.dispatchXQAKernel()
                 └── 直接读 KVCache.data，对单 query 算注意力
```

decode 能「高效」的关键有三点：

1. **不重算历史**：过去 token 的 \(K,V\) 已经在缓存里，XQA 只需读，不必再过一遍前面所有层。
2. **形状极简**：单 query 让 kernel 可以把全部 thread block 用来并行扫描 KV 维度，把访存与计算压满。
3. **K/V 写入与注意力解耦**：新 token 的 \(K,V\) 先由 `posEncoding/applyRopeWriteKV` 算好 RoPE 并写进缓存（[applyRopeWriteKV.h:31-47](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/kernels/posEncoding/applyRopeWriteKV.h#L31-L47)），XQA 只管读——这正好承接 u5-l5 的「past/present 同址绑定」契约。

#### 4.2.3 源码精读

**context 侧运行器**——`ContextFMHARunner` 在构造时就吃进 `smVersion`，并暴露静态方法按 SM 查 kernel 是否存在、加载 cubin（[contextFMHARunner.h:50-74](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/kernels/contextAttentionKernels/contextFMHARunner.h#L50-L74)）：

```cpp
//! Runner for context-phase fused multi-head attention (FMHA)
class ContextFMHARunner {
public:
    ContextFMHARunner(nvinfer1::DataType const dataType, int32_t batchSize, int32_t paddedSeqLen,
        int32_t numQHeads, int32_t numKvHeads, int32_t headSize, int32_t smVersion,
        AttentionInputLayout inputLayout, ContextAttentionMaskType maskType = ..., bool isSPadded = true);
    ...
    static bool canImplement(int32_t headSize, int32_t sm, nvinfer1::DataType dataType,
        AttentionInputLayout inputLayout, ContextAttentionMaskType maskType) noexcept;
    static bool loadContextFMHAKernels(int32_t sm, nvinfer1::DataType dataType);
};
```

注意 `smVersion` 是构造参数——**同一个 runner 在不同 GPU 上会选不同 cubin**。

**decode 侧运行器**——`DecoderXQARunner` 的 launch 参数直接内嵌了一个 `KVCache` 子结构，这是 decode 与缓存耦合的铁证（[decoderXQARunner.h:29-37](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/kernels/decodeAttentionKernels/decoderXQARunner.h#L29-L37)）：

```cpp
struct XQALaunchParams {
    struct KVCache {
        void* data = nullptr;                      // 指向 KV 缓存（或 paged 缓存池）
        int32_t const* sequence_lengths = nullptr; // 每个请求的序列长度
        uint32_t capacity = 0;
        int32_t const* pageList = nullptr;         // paged KV 的页表
        uint32_t tokensPerPage = 0;                // 0 = 连续 KV 缓存
    };
    void* output = nullptr;
    void const* qInputPtr = nullptr;               // 单（或少数）query
    KVCache kvCache;
    float kScale = 1.0f;                           // 量化 KV 的反量化 scale
    float vScale = 1.0f;
    ...
    void* treeAttnMask = nullptr;                  // 投机解码树注意力掩码
};
```

它还有两个分立的派发入口，普通 decode 与投机解码树注意力各走一条（[decoderXQARunner.h:96-102](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/kernels/decodeAttentionKernels/decoderXQARunner.h#L96-L102)）：

```cpp
void dispatchXQAKernel(XQALaunchParams& params, cudaStream_t const& stream);
void dispatchSpecDecodeXQAKernel(XQALaunchParams& params, cudaStream_t const& stream);
```

`kScale/vScale` 的存在说明 XQA 还能直接吃 **FP8 KV 缓存**（u9-l3 的 FP8 KV 特性在这里落地）——读出来先反量化再算注意力。

#### 4.2.4 代码实践

**实践目标**：通过读 cubin 命名，理解 prefill FMHA 是按哪些维度切片、按哪些 SM 出产的。

**操作步骤**：

1. 列出 context FMHA 的 cubin 文件名：
   ```bash
   ls cpp/kernels/contextAttentionKernels/cubin/ | head -20
   ```
2. 解析文件名格式：`fmha_v2_flash_attention_<dtype>_<accType>_<tileM>_<tileN>_<SMode>_<layout>_<headSize>_<mask>_sm<XX>.cubin.cpp`。

**需要观察的现象**：

- 同一个 `<headSize>` + `<layout>` 组合，会有 `sm80/sm86/sm87/sm89/sm100/sm101/sm120/sm121` 一整套——**每个 SM 一份**。
- mask 有 `custom_mask`（视觉块滑窗，u6-l1）与普通 `_256`/`_128`（headSize）两种。

**预期结果**：你能说出「prefill FMHA 的 kernel 多样性 = headSize × layout × mask × SM 的笛卡尔积，且每份都预编译」。这与 4.4 的 SM 排除逻辑直接对应。

#### 4.2.5 小练习与答案

**练习 1**：为什么不在 decode 阶段也用 context FMHA 那个 flash kernel？
**答案**：flash kernel 是为「大方阵 \(S_q\times S_q\)」的分块设计的；decode 时 \(S_q=1\)，大方阵 kernel 的分块、softmax 跨 block 归约都用不上，反而开销大。XQA 专为「1 query × 长 KV」优化，把并行度铺在 KV 维度上。

**练习 2**：`XQALaunchParams` 里 `kScale/vScale` 在 FP16 KV 缓存时应取什么值？
**答案**：`1.0f`。这两个 scale 是「量化→原始」的反量化系数，FP16 不需要反量化，故为 1（[decoderXQARunner.h:44-45](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/kernels/decodeAttentionKernels/decoderXQARunner.h#L44-L45)）。FP8 KV 时才填真实 scale。

---

### 4.3 MoE 算子族：从 topk 到散回

#### 4.3.1 概念说明

MoE（Mixture of Experts）层把一个 FFN 换成「多个专家 FFN + 一个门控」。每个 token 由门控选出 `topK` 个专家，分别过这 `topK` 个专家的 FFN，再用门控权重加权求和。一个 MoE 层的数学表达：

\[
y = \sum_{e\in \text{topK}(g(x))} g_e(x)\cdot \text{Expert}_e(x)
\]

其中 \(g(x)\) 是门控 logit，\(\text{topK}\) 选出 `topK` 个最大的 \(g_e\)。

EdgeLLM 的 MoE 算子分成清晰的几段（[cpp/kernels/moe/](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/kernels/moe/) 目录名就是分工表）：

| 段 | 算子文件 | 职责 |
|---|---|---|
| topk | `moeTopkSoftmaxKernels` | 门控 logit → softmax → 选 topK 专家 + 权重 |
| 路由/分组 | `f16MoeSupportKernels` / `moeAlignSumKernels` | 把 token 按专家重排，构造分组 GEMM 描述符 |
| per-expert scale | `moePerExpertScaleKernels` | NVFP4 等格式的逐专家缩放 |
| 专家 GEMM | `f16_cutedsl` / `nvfp4_cutedsl` / `moe_marlin` | FC1、FC2 两组分组矩阵乘（强 SM 特异性） |
| 激活 | `f16MoeSupportKernels.activateF16Moe` | SwiGLU 或 ReLU² |
| 散回 | `f16MoeSupportKernels.scatterF16MoeOutput` | 按门控权重加权合并回 token 主序 |

本模块精读 `f16MoeSupportKernels.cu`——它是「非 GEMM」的支撑算子集合，把 token 在「token 主序」与「专家主序」之间搬来搬去，并喂给分组 GEMM。文件整体被 `CUTE_DSL_F16_MOE_ENABLED` 宏包裹（[f16MoeSupportKernels.cu:20](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/kernels/moe/f16MoeSupportKernels.cu#L20)），因为它的分组 GEMM 伙伴在 CuTe DSL 产物里。

#### 4.3.2 核心流程

FP16 MoE 一次前向（对一批 token）的完整流水：

```
1. moeTopkSoftmax:   gating[B, E]  →  topkIds[B, topK], topkWeights[B, topK]
2. buildRoutingAndGemmMetadata:
     - 统计每个专家分到几行（expertCounts）
     - 前缀和得每个专家在「专家主序」缓冲里的行偏移（expertOffsets）
     - 建双向映射 sortedToExpanded / expandedToSorted
     - 为 FC1/FC2 填分组 GEMM 描述符（problemShapes/strides/addresses）
3. gatherHiddenRows:  按专家主序把 hidden states gather 到连续缓冲
4. 分组 GEMM FC1:     [Σ rows, K] × [E, N, K]  →  [Σ rows, 2N]   （CuTe DSL）
5. activateF16Moe:    SwiGLU/ReLU² → [Σ rows, N]
6. 分组 GEMM FC2:     [Σ rows, N] × [E, K, N]   →  [Σ rows, K]    （CuTe DSL）
7. scatterOutput:     按门控权重把每个 token 的 topK 个专家结果加权求和 → [B, K]
```

关键是第 2 步的「**重排**」：分组 GEMM 要求每个专家的输入是**连续的一段行**，但 token 原本是按 `[token, slot]` 主序展开的（`expandedRow = token*topK + slot`）。必须先按专家把它们排到一起，并告诉 GEMM kernel「第 e 个专家从第几行开始、共几行」——这就是 `expertOffsets` 与分组描述符的作用。

#### 4.3.3 源码精读

先看数据结构。`F16MoeRoutingBuffers` 是重排用的 5 个 device 缓冲（[f16MoeSupportKernels.h:29-36](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/kernels/moe/f16MoeSupportKernels.h#L29-L36)）：

```cpp
struct F16MoeRoutingBuffers {
    int32_t* expertCounts{};       // INT32 [E]    每个专家分到的行数
    int32_t* expertOffsets{};      // INT32 [E+1]  专家主序下每个专家的起始偏移（前缀和）
    int32_t* expertWriteOffsets{}; // INT32 [E]    散写游标
    int32_t* sortedToExpanded{};   // INT32 [R]    专家主序第 r 行 → 原展开行
    int32_t* expandedToSorted{};   // INT32 [R]    原展开行 → 专家主序第几行
};
```

`F16MoeGemmMetadata` 是分组 GEMM 的「**设备端**描述符」——注意它是 device 指针，因为分组 GEMM kernel 在 GPU 上读它来定位每个专家的子问题（[f16MoeSupportKernels.h:38-44](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/kernels/moe/f16MoeSupportKernels.h#L38-L44)）：

```cpp
struct F16MoeGemmMetadata {
    int32_t* problemShapes{}; // INT32 [E,4]  每个专家的 M,N,K,L
    int32_t* strides{};       // INT32 [E,3,2] A,B,D 的步长
    int64_t* addresses{};     // INT64 [E,3]  A,B,D 的设备地址
};
```

**入口函数 `buildF16MoeRoutingAndGemmMetadata`** 有两条路径：token 数 ≤ 256 时走「单 CTA 融合快路径」，否则走多 kernel 回退（[f16MoeSupportKernels.cu:337-380](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/kernels/moe/f16MoeSupportKernels.cu#L337-L380)）：

```cpp
cudaError_t buildF16MoeRoutingAndGemmMetadata(...) noexcept {
    // 参数校验：topK 上限 kMAX_TOP_K=8，专家数不超过一个 block 的线程数
    if (... || topK > kMAX_TOP_K || numExperts > kTHREADS_PER_BLOCK) return cudaErrorInvalidValue;

    if (numTokens <= kMAX_FUSED_ROUTING_TOKENS) {           // ≤256：融合快路径
        return launchFusedBuildRoutingAndGemmMetadata(...);
    }
    // 回退：统计 → 前缀和 → 散射
    countRoutesKernel<<<...>>>(topkIds, buffers.expertCounts, routedRows, numExperts);
    buildOffsetsKernel<<<1, 256, 0, stream>>>(...);
    scatterRouteMapKernel<<<...>>>(...);
    return cudaGetLastError();
}
```

**快路径 `fusedBuildRoutingAndGemmMetadataKernel`** 把「计数 → 前缀和 → 填 GEMM 描述符 → 散射映射」全压进一个 kernel，靠 shared memory 上的 `sharedExpertCounts` 做块内归约（[f16MoeSupportKernels.cu:98-172](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/kernels/moe/f16MoeSupportKernels.cu#L98-L172)）。它按 `TOP_K` 模板特化，所以 `launchFusedBuildRoutingAndGemmMetadata` 用一个 switch 在 topK=1..8 之间派发（[f16MoeSupportKernels.cu:179-213](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/kernels/moe/f16MoeSupportKernels.cu#L179-L213)）——这是把运行期 `topK` 映射到编译期常量的标准手法（让循环可被 unroll）。

**填描述符** `populateGemmMetadata` 把每个专家的 M（行数）、N、K 与输入/权重/输出的地址写进 device 描述符（[f16MoeSupportKernels.cu:70-96](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/kernels/moe/f16MoeSupportKernels.cu#L70-L96)），权重地址按专家编号偏移 `expert * N * K`。

**激活 `activateF16Moe`** 在 FC1 与 FC2 之间做 SwiGLU 或 ReLU²（[f16MoeSupportKernels.cu:397-409](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/kernels/moe/f16MoeSupportKernels.cu#L397-L409)）。SwiGLU 分支体现了 u2-l3 提到的 SwiGLU gate/up 交错布局（[f16MoeSupportKernels.cu:279-310](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/kernels/moe/f16MoeSupportKernels.cu#L279-L310)）：

```cpp
// SwiGLU: result = up * gate / (1 + exp(-gate))，即 silu(gate)*up
float const up   = __half2float(rawFc1[row * fc1N + upColumn]);
float const gate = __half2float(rawFc1[row * fc1N + gateColumn]);
result = up * gate / (1.0F + expf(-gate));
```

注意它刻意在 **FP32** 里做激活再转回 FP16，避免半精度下 silu 的数值溢出。

**散回 `scatterF16MoeOutput`** 是最后一步，把每个 token 的 `topK` 个专家结果按门控权重加权合并（[f16MoeSupportKernels.cu:411-424](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/kernels/moe/f16MoeSupportKernels.cu#L411-L424)，实现在 [scatterOutputKernel:312-333](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/kernels/moe/f16MoeSupportKernels.cu#L312-L333)）：

```cpp
// 对每个 token 的每个 hidden 维，累加 topK 个专家的加权输出
float accumulator{0.0F};
for (int32_t slot = 0; slot < topK; ++slot) {
    int32_t const expandedRow = token * topK + slot;
    int32_t const sortedRow = expandedToSorted[expandedRow];
    accumulator += topkWeights[expandedRow]
                 * __half2float(routedOutput[sortedRow * hiddenSize + hidden]);
}
output[element] = __float2half_rn(accumulator);   // 同样 FP32 累加、一次转 FP16
```

> 与 topk 段的关系：`topkWeights` 与 `expandedToSorted` 都来自前面的 `moeTopkSoftmax` + `buildRoutingAndGemmMetadata`。`moeTopkSoftmax` 对门控 logit 做 softmax、选 topK、可选 tanh softcapping 与重归一化（[moeTopkSoftmaxKernels.h:41-79](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/kernels/moe/moeTopkSoftmaxKernels.h#L41-L79)），专家数为 2 的幂（1-256）走融合 warp 归约快路径，否则回退到「softmax + topk 两个独立 kernel」。

#### 4.3.4 代码实践

**实践目标**：用「纸上演算」理解 token→专家的重排。

**操作步骤**：

1. 假设 `numTokens=2`、`topK=2`、`numExperts=3`，门控选出：
   - token0 → 专家 {1, 2}，token1 → 专家 {0, 2}
2. 写出 `expandedRow = token*topK + slot` 的取值：`(0,1),(0,2),(1,0),(1,2)`。
3. 手算 `expertCounts = [1, 1, 2]`、`expertOffsets = [0, 1, 2, 4]`。
4. 给出一个合法的 `sortedToExpanded`（专家主序下逐行展开），并验证 `gatherHiddenRows` 会把 token0/token1 的 hidden 按这个顺序拷贝。
5. 阅读快路径 kernel [f16MoeSupportKernels.cu:128-137](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/kernels/moe/f16MoeSupportKernels.cu#L128-L137) 里 `thread==0` 算前缀和的片段，与你手算的 `expertOffsets` 对照。

**需要观察的现象**：专家 2 分到 2 行（来自 token0 的 slot1 与 token1 的 slot1），它俩在专家主序缓冲里必须连续排在第 2、3 行。

**预期结果**：一组自洽的 `expertOffsets / sortedToExpanded / expandedToSorted`，且 `expandedToSorted[expandedRow]` 与 `sortedToExpanded` 互为（限定域内的）逆映射。**待本地验证**：若想跑真实数据，需要构造一个最小 MoE 插件单测（`unittests/` 下参考现有 MoE 测试），本机无 GPU 时只能纸面推演。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `launchFusedBuildRoutingAndGemmMetadata` 要用 switch 把 topK 映射成模板参数，而不是直接传运行期 topK？
**答案**：模板参数是编译期常量，`for (slot = 0; slot < TOP_K; ++slot)` 可被 `#pragma unroll` 完全展开，省掉循环开销；同时 `sharedExpertCounts` 的访问模式在常量 topK 下更可预测。运行期 topK 做不到这些。

**练习 2**：`activateF16Moe` 和 `scatterF16MoeOutput` 为什么都先升到 FP32 算再转回 FP16？
**答案**：silu 的 \(x/(1+e^{-x})\) 与加权累加都涉及指数与多次相加，FP16 动态范围小、易溢出/下溢。在 FP32 累加保证数值精度，只在边界处做一次 FP16 转换，兼顾精度与显存。

---

### 4.4 SM 特异性构建配置

> 这是本讲的重头戏，也是代码实践任务的核心。

#### 4.4.1 概念说明

EdgeLLM 支持的边缘 GPU 跨越多代架构：Ampere（sm_80/86/87/89，如 Jetson Orin 是 sm_87、A30 是 sm_80）、Blackwell 数据中心（sm_100/101/110，如 Thor）、Blackwell 消费级（sm_120/121，如 Spark/GB10）。**同一份高性能算子在不同 SM 上需要不同的二进制**，于是构建系统里有三套并行的「SM 特异性构建」机制：

| 机制 | 服务对象 | 产物形态 | 何时生成 |
|---|---|---|---|
| **A. FMHA cubin 条件编译** | prefill 的 context FMHA | 预编译 `.cubin.cpp`，已进 git | 开发者手工跑 `setup.py`（离线） |
| **B. XQA cubin 构建期生成** | decode 的 XQA 注意力 | `.cubin.cpp`，构建时生成 | CMake 配置期调 `gen_cubins.py` |
| **C. CuTe DSL 算子库** | FMHA(Blackwell)/GDN/MoE/GEMM/SSD | per-SM 静态库 `libcutedsl_<arch>.a` | `build_cutedsl.py` 或预编译 tar |

三者都用同一个核心思路：**根据 `CMAKE_CUDA_ARCHITECTURES` 算出「需要的 SM 集合」，只为这些 SM 编译/链接，其余用宏排除**——既省二进制体积，又保证可移植。

#### 4.4.2 核心流程

**机制 A：FMHA cubin 条件编译**（[cpp/CMakeLists.txt:38-79](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/CMakeLists.txt#L38-L79)）

```text
FMHA_ALL_SM_VERSIONS = {80,86,87,89,100,101,120,121}      # 全集
REQUIRED_FMHA_SM_VERSIONS = 从 CMAKE_CUDA_ARCHITECTURES 剥后缀得到
特殊规则: SM110 运行时复用 SM101 的 cubin   → 若需要110而没101，补上101
对全集里每个不在 REQUIRED 里的 SM，加 EXCLUDE_SM_<NN> 宏
```

这些 `EXCLUDE_SM_<NN>` 宏被加到 `edgellmKernels` / `edgellmCore` / `NvInfer_edgellm_plugin` 三个目标上（[cpp/CMakeLists.txt:109/133/155-156](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/CMakeLists.txt#L104-L112)），cubin 头文件 `fmha_cubin.h` 里用 `#ifndef EXCLUDE_SM_<XX>` 把不需要的 SM 的 `extern` 声明与符号裁掉。

**机制 B：XQA cubin 构建期生成**（[cmake/XQACubins.cmake](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cmake/XQACubins.cmake)）

与 A 不同，B 的 cubin **不进 git**，而是 CMake 配置期现编：

```cmake
_edgellm_xqa_get_required_sms(_required_sm_versions)   # 同样从 CMAKE_CUDA_ARCHITECTURES 推导，SM110→101
execute_process(... ${gen_cubins.py} --arches ${_required_sm_versions} --list-outputs ...)  # 先列出产物
add_custom_command(... ${gen_cubins.py} --arches ${_required_sm_versions} ...)              # 再声明生成规则
add_custom_target(generateXQACubins DEPENDS ...)
```

然后 `edgellm_xqa_add_generated_sources` 把生成的 `.cubin.cpp` 替换掉 `decodeAttentionKernels/cubin/` 下旧的源（[XQACubins.cmake:127-135](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cmake/XQACubins.cmake#L127-L135)）。

**机制 C：CuTe DSL 算子库**（[cmake/CuteDsl.cmake](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cmake/CuteDsl.cmake) + [kernelSrcs/README.md](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/kernelSrcs/README.md)）

C 最复杂：用户用 `-DENABLE_CUTE_DSL=<groups>` 选算子族（`fmha/gdn/f16_moe/nvfp4_moe/...`），用 `-DCUTE_DSL_ARTIFACT_TAG=sm_<NN>` 选 SM。tag 的推断顺序是（[CuteDsl.cmake:104-135](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cmake/CuteDsl.cmake#L104-L135)）：

```text
显式 -DCUTE_DSL_ARTIFACT_TAG > EMBEDDED_TARGET 默认映射 > 目录里唯一候选自动选
EMBEDDED_TARGET 映射:
   gb10          → sm_121
   auto-thor/jetson-thor → sm_110
   jetson-orin   → sm_87
```

若本地没有该 tag 的产物，CMake 会从 `kernelSrcs/cuteDSLPrebuilt/cutedsl_<arch>_<tag>_cuda*.tar.gz` 自动解压（[CuteDsl.cmake:248-264](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cmake/CuteDsl.cmake#L248-L264)）。随后读 `metadata.json` 的 `groups/variants`，为命中的 variant 设 `CUTE_DSL_<GROUP>_ENABLED` 宏——本讲的 `f16MoeSupportKernels.cu` 就是被 `CUTE_DSL_F16_MOE_ENABLED` 点亮的。

> **一个诚实的纠偏**：`AGENTS.md` 提到「新增 SM 时可选更新 `cmake/CuteDslFMHA.cmake`」，但仓库里**并不存在** `cmake/CuteDslFMHA.cmake` 这个文件，实际的 CuTe DSL 构建逻辑全部在 `cmake/CuteDsl.cmake` 一个文件里。以真实文件为准。

#### 4.4.3 源码精读

**SM110 复用 SM101 的规则**在 A、B 两套里都写了，这是 Blackwell 数据中心里的一个重要复用约定。机制 A（[cpp/CMakeLists.txt:60-64](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/CMakeLists.txt#L60-L64)）：

```cmake
# SM 110 uses SM 101 FMHA cubins at runtime
if(110 IN_LIST REQUIRED_FMHA_SM_VERSIONS AND NOT 101 IN_LIST REQUIRED_FMHA_SM_VERSIONS)
  list(APPEND REQUIRED_FMHA_SM_VERSIONS 101)
endif()
```

机制 B 里同样的映射（[XQACubins.cmake:47-49](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cmake/XQACubins.cmake#L47-L49)）：

```cmake
if(_arch_num STREQUAL "110")
  set(_arch_num 101)
endif()
```

**`CMAKE_CUDA_ARCHITECTURES` 的默认值**在顶层 CMakeLists 里设置，且与 CUDA 工具具版本挂钩（[CMakeLists.txt:64-69](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/CMakeLists.txt#L64-L69)）：

```cmake
if(NOT DEFINED AARCH64_BUILD)
  set(CMAKE_CUDA_ARCHITECTURES 80;86;89)
  if(CUDA_CTK_VERSION VERSION_GREATER_EQUAL 12.8)
    list(APPEND CMAKE_CUDA_ARCHITECTURES 100a 120)      # CUDA≥12.8 才加 Blackwell
  endif()
endif()
```

注意 `120`（无 `a` 后缀）会同时编 PTX，而 `100a`（有 `a`）只编 SASS——这影响 cubin 是否能 JIT 到更新的架构。

**f16_moe 的「精确 SM」守卫**很特别：因为 FP16 MoE 的 CuTe DSL 产物是**绑死一个 SM** 的，CMake 会从 metadata 读出 `gpu_arch`，转成整数宏 `CUTE_DSL_F16_MOE_ARTIFACT_SM=<NN>` 注入（[CuteDsl.cmake:426-436](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cmake/CuteDsl.cmake#L426-L436)），源码据此编译对应架构的入口。三个 f16_moe variant 对应三族 GPU（[build_cutedsl.py:692-709](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/kernelSrcs/build_cutedsl.py#L692-L709)）：Ampere、Blackwell DC、Blackwell GeForce。

#### 4.4.4 代码实践（对应本讲指定实践任务）

**实践目标**：回答「新增一个 SM 架构（假设 sm_130）需要更新哪些 FMHA 构建配置」，并解释 decode FMHA 为何能与 KV 缓存结合实现单 token 高效注意力。

**操作步骤**：

1. **通读三套机制的 SM 清单**。定位以下三处「SM 全集」：
   - 机制 A：`FMHA_ALL_SM_VERSIONS`（[cpp/CMakeLists.txt:38-46](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/CMakeLists.txt#L38-L46)）
   - 机制 B：`_all_sm_versions`（[XQACubins.cmake:23-31](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cmake/XQACubins.cmake#L23-L31)）
   - 机制 C：`kernelSrcs/fmha_v2/README.md` 的两轮生成（CUDA 12.8 vs 12.9，[fmha_v2/README.md:15-58](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/kernelSrcs/fmha_v2/README.md#L15-L58)）

2. **写出新增 sm_130 的改动清单**（这是实践任务的核心产出）：

   | 步骤 | 改动点 | 文件 |
   |---|---|---|
   | a | 把目标 SM 加进编译目标架构 | `CMakeLists.txt` 的 `CMAKE_CUDA_ARCHITECTURES`（[CMakeLists.txt:64-69](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/CMakeLists.txt#L64-L69)） |
   | b | 把 `130` 加进 FMHA 全集 | `cpp/CMakeLists.txt` 的 `FMHA_ALL_SM_VERSIONS`（[cpp/CMakeLists.txt:38-46](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/CMakeLists.txt#L38-L46)） |
   | c | 生成 sm130 的 context FMHA cubin | `kernelSrcs/fmha_v2/`：按 README 加一轮 `ENABLE_SM...` 生成 cubin，合并进 `cpp/kernels/contextAttentionKernels/cubin/`（[fmha_v2/README.md:15-58](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/kernelSrcs/fmha_v2/README.md#L15-L58)） |
   | d | 把 `130` 加进 XQA 全集 | `cmake/XQACubins.cmake` 的 `_all_sm_versions`（[XQACubins.cmake:23-31](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cmake/XQACubins.cmake#L23-L31)） |
   | e | 若 sm_130 复用某现有 SM 的 cubin（像 110 复用 101），加映射 | A：[cpp/CMakeLists.txt:60-64](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/CMakeLists.txt#L60-L64)；B：[XQACubins.cmake:47-49](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cmake/XQACubins.cmake#L47-L49) |
   | f | 生成 Blackwell+ 的 CuTe DSL 算子产物 | `python kernelSrcs/build_cutedsl.py --gpu_arch sm_130`，按需 `-DENABLE_CUTE_DSL=fmha`（[kernelSrcs/README.md:40-46](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/kernelSrcs/README.md#L40-L46)） |

3. **回答 decode FMHA + KV 缓存的高效性**。结合 [decoderXQARunner.h:29-37](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/kernels/decodeAttentionKernels/decoderXQARunner.h#L29-L37) 与 [applyRopeWriteKV.h:42-47](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/kernels/posEncoding/applyRopeWriteKV.h#L42-L47) 写下三点：历史 KV 已在缓存只需读、单 query 让并行度全铺在 KV 维度、K/V 写入（RoPE+落盘）与注意力读解耦。

**需要观察的现象**：

- 三套机制的「SM 全集」目前都是 `{80,86,87,89,100,101,120,121}` 八个值——完全一致并非巧合，而是被同一份硬件支持矩阵驱动。
- 机制 A 的产物已在 git（能看到一堆 `*_sm<N>.cubin.cpp`），机制 B 的产物在 `build/generated/xqa/cubin/`（构建后才出现）。

**预期结果**：你能产出上表那样的「改动清单」，并能解释每一处为什么必须改。**待本地验证**：真实新增 SM 还需在该 GPU 上跑 `export → build → inference` 三步验证（AGENTS.md 的硬性要求），无目标硬件时只能完成配置改动与纸面核查。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `CMAKE_CUDA_ARCHITECTURES` 只设成 `89`，`edgellmKernels` 会被加上哪些 `EXCLUDE_SM_*` 宏？
**答案**：`REQUIRED_FMHA_SM_VERSIONS={89}`，于是给全集里其余 7 个都加排除宏：`EXCLUDE_SM_80, EXCLUDE_SM_86, EXCLUDE_SM_87, EXCLUDE_SM_100, EXCLUDE_SM_101, EXCLUDE_SM_120, EXCLUDE_SM_121`（SM110 复用 SM101 的规则不触发，因为 110 不在需求里）。结果只有 sm_89 的 FMHA cubin 被编译，二进制体积最小。

**练习 2**：机制 A（cubin 进 git）和机制 B（cubin 构建期生成）为什么采用不同策略？
**答案**：context FMHA 的 cubin 来自一个**打过补丁的 TRT-LLM fmha_v2 源**（需特定 commit + patch，见 [fmha_v2/README.md:7-13](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/kernelSrcs/fmha_v2/README.md#L7-L13)），生成过程重、依赖外部仓库，故产物固化进 git。XQA 的 `gen_cubins.py` 是仓库自带、轻量、纯函数式，可安全地在每次 CMake 配置期重跑，故不进 git。

**练习 3**：为什么 `CuteDsl.cmake` 对 `f16_moe` 组要强制读 metadata 里的 `gpu_arch` 并注入 `CUTE_DSL_F16_MOE_ARTIFACT_SM`？
**答案**：FP16 MoE 的分组 GEMM 产物是**精确绑定一个 SM** 的（一个 tar 包一个 SM），不像 FMHA 那样一个产物覆盖多 SM。源码必须在编译期知道自己服务的精确 SM，才能选对 AOT 入口与张量核心排布，故把 SM 编进宏。

---

## 5. 综合实践

**任务**：为一个**目标平台**（任选 Jetson Orin / Thor / Spark 三选一）绘制「算子→构建机制→产物」的完整地图，并模拟一次「新增 SM」的配置审查。

1. **选定平台并查 SM**：
   - Jetson Orin → sm_87，`EMBEDDED_TARGET=jetson-orin`
   - Thor → sm_110，`EMBEDDED_TARGET=auto-thor` 或 `jetson-thor`
   - Spark/GB10 → sm_121，`EMBEDDED_TARGET=gb10`
2. **列出该平台会启用哪些算子产物**：
   - context FMHA：哪个 `EXCLUDE_SM_*` **不会**被设？（即该 SM 的 cubin 会被编译；注意 Thor=110 会复用 101 的 cubin。）
   - decode XQA：`gen_cubins.py` 会为哪些 SM 现编？
   - CuTe DSL：`CUTE_DSL_ARTIFACT_TAG` 会被自动推断成什么？需要 `-DENABLE_CUTE_DSL=` 开哪些 group？
3. **追踪一条 decode 数据通路**：从 `applyRopeWriteKV`（写 K/V 入缓存）→ `DecoderXQARunner.dispatchXQAKernel`（读缓存算注意力），标注每一步读/写的张量与 SM 特异性产物。
4. **写一份「若要把该平台升级到下一代 SM」的 PR 描述**：列出按 4.4.4 表格要改的所有文件、要新生成的产物、以及验证用的 `export → build → inference` 命令骨架（参照 u1-l5）。

**产出**：一张平台 × 算子 × 构建机制 的对照表 + 一份数据通路图 + 一份改动清单。

> 本机无 GPU 时，前 3 步可纯靠源码阅读完成；第 4 步的命令骨架可参照 `AGENTS.md` 与 `kernelSrcs/README.md` 组装，但标注「待本地验证」。

## 6. 本讲小结

- EdgeLLM 的 CUDA 算子分两副面孔：`cpp/kernels/` 是随主仓库 `nvcc` 编译的通用 `.cu`（MoE 路由/激活、RoPE、投机解码工具），`kernelSrcs/` 是强 SM 特异性的预编译 cubin 与 CuTe DSL 产物（FMHA、XQA、分组 GEMM）。
- 注意力被拆成两类 kernel：**context FMHA**（prefill，大方阵，预编译 cubin/CuTe DSL）与 **decode XQA**（单 query 读 KV 缓存，构建期生成 cubin）；decode 高效靠「历史 KV 只读不重算 + 并行度铺在 KV 维度 + RoPE/落盘与读解耦」。
- MoE 算子族是一条流水线：`moeTopkSoftmax`（选路）→ `buildF16MoeRoutingAndGemmMetadata`（按专家重排 + 填分组 GEMM 描述符）→ gather → 分组 FC1 → `activateF16Moe`（SwiGLU/ReLU²，FP32 算）→ 分组 FC2 → `scatterF16MoeOutput`（门控加权合并）。
- 三套 SM 特异性构建机制共享「从 `CMAKE_CUDA_ARCHITECTURES` 推需求 SM 集、排除其余」的思路：FMHA cubin 条件编译（A，进 git）、XQA cubin 构建期生成（B，不进 git）、CuTe DSL per-SM 静态库（C，预编译 tar + group/variant 宏）。
- 新增一个 SM 架构是跨多文件的协调改动：顶层架构列表 + A/B 两处 SM 全集 + cubin 生成轮 + 可能的复用映射 + CuTe DSL 产物生成，并必须用 `export → build → inference` 三步验证。
- 一个纠偏：`AGENTS.md` 提到的 `cmake/CuteDslFMHA.cmake` 实际不存在，CuTe DSL 的全部构建逻辑在 `cmake/CuteDsl.cmake`。

## 7. 下一步学习建议

- **回到插件壳**：重读 u8-l1 的 `AttentionPlugin::enqueue`，验证它如何在本讲的 `ContextFMHARunner` 与 `DecoderXQARunner` 之间按 prefill/decode 分派。
- **进入 NVFP4 MoE 细节**：阅读 `cpp/kernels/moe/nvfp4_cutedsl/` 与 `cpp/plugins/nvfp4MoePlugin/`，对照 u3-l3 的 NVFP4 权重布局，理解「SM110 split FC1/FC2」与「SM12x fused」两条 NVFP4 MoE 路径为何按 GeForce/DC 分治。
- **混合模型的算子**：结合 u5-l5，读 `cpp/kernels/mamba/`（causalConv1d、selectiveStateUpdate、CuTe DSL SSD）与 `gdnKernels/`，理解 attention 与 SSM 层如何在同一运行时里共用缓存抽象。
- **投机解码工具算子**：进 `cpp/kernels/speculative/`（`eagleAcceptKernels`、`ddtreeKernels`、`gemma4MTPRuntimeKernels`），承接 u7-l1，看 accept/tree-mask 算子如何与 decode XQA 的 `dispatchSpecDecodeXQAKernel` 配合。
