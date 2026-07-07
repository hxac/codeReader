# u10-l4 TensorRT plugin 集成

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚「TensorRT plugin」是什么、它和一份裸的 FasterTransformer（FT）C++ 模型在**调用方式**上的根本区别。
- 看懂 FT 在 `src/fastertransformer/tensorrt_plugin/` 下采用的统一封装模式：`Config` + `Plugin` + `Creator` 三件套，以及 `IPluginV2DynamicExt` 的生命周期回调。
- 读懂 `BertFp8Plugin::enqueue` 如何把 TensorRT 送来的张量零拷贝拼装成 `ft::TensorMap`，再喂给 `BertFP8::forward`。
- 列出 `tensorrt_plugin` 当前支持的 5 类模型（bert_fp8 / swin / t5 / vit / wenet）及其精度变体，理解 `BUILD_TRT` 与 `ENABLE_FP8` 两道编译闸门如何控制它们。
- 在「已有 FT C++ 模型」的前提下，权衡是否值得再封装一层 TensorRT plugin（收益：融入 TRT 图优化、序列化 engine；代价：额外封装与维护成本）。

## 2. 前置知识

本讲假定你已经建立以下认知（来自前置讲义）：

- **FT 的三层抽象与框架外壳**（u1-l3）：`tensorrt_plugin` 与 `th_op`、`tf_op`、`triton_backend` 同属「框架外壳」——本身不做计算，只负责把框架张量与内部 `ft::Tensor` 桥接。本讲的 `tensorrt_plugin` 对应的外部框架是 **TensorRT**。
- **统一的 `Tensor` / `TensorMap` 接口**（u2-l1）：所有 FT 模型的 `forward` 现在统一签名为 `forward(TensorMap* outputs, TensorMap* inputs, const Weight*)`，`Tensor` 是非拥有的轻量描述符（只持指针+形状+`where` 标记）。本讲会反复看到 plugin 在 `enqueue` 里用 `ft::MEMORY_GPU` 直接包住 TRT 传入的裸指针。
- **显存分配器 `IAllocator`**（u2-l2）：plugin 在 `initialize()` 里会建一个 `Allocator<AllocatorType::CUDA>` 与 `cublasMMWrapper`，这是模型运行的设备环境。
- **GPT/BERT 模型本身**（u4-l1、u6-l1）与 **FP8 量化**（u9-l3）：`bert_fp8` 是 TRT plugin 里最有代表性、也最完整的一个例子，它内部直接实例化 `BertFP8<__nv_fp8_e4m3, __nv_bfloat16>`。
- **Triton backend 部署**（u10-l3）：本讲是 u10-l3 的姊妹篇。Triton 与 TensorRT 是 FT 走向生产的两条不同路线，结尾会做对比。

**TensorRT 是什么（一句话）**：NVIDIA 的高性能推理 SDK。你给它一个网络定义（ONNX 或手写 `INetworkDefinition`），它做层融合、精度校准、kernel 自动选择，最终**编译（build）出一个可序列化的 engine**；运行时加载 engine、喂数据即可推理。当 FT 的某个算子/模型无法用标准 TRT 层表达、或 FT 自己的实现明显更快时，就把它写成一个 **plugin**，让 TRT 把它当成网络中的一个「自定义层」来调用。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
| :--- | :--- |
| [`CMakeLists.txt`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L50-L50) | 顶层开关 `option(BUILD_TRT ...)`，默认 OFF |
| [`src/fastertransformer/CMakeLists.txt`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/CMakeLists.txt#L26-L27) | `if(BUILD_TRT) add_subdirectory(tensorrt_plugin)`，整棵 plugin 子树的总闸门 |
| [`tensorrt_plugin/CMakeLists.txt`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tensorrt_plugin/CMakeLists.txt#L15-L22) | 逐模型 `add_subdirectory`；`bert_fp8` 受 `ENABLE_FP8` 二次守卫 |
| `tensorrt_plugin/bert_fp8/` | **本讲主范例**。FP8 BERT 的完整 plugin（`.h` 声明 + `.cu` 实现） |
| `tensorrt_plugin/vit/` | ViT 的 FP16 与 INT8 plugin |
| `tensorrt_plugin/swin/` | Swin 的 FP16 与 INT8 plugin |
| `tensorrt_plugin/t5/` | T5 的 encoder / decoding 两个 plugin |
| `tensorrt_plugin/wenet/` | WeNet 语音的 encoder / decoder 两个 plugin |

> 命名小提示：TensorRT 的头文件是 `NvInfer.h`（核心）/ `NvInferPlugin.h`（plugin 注册宏）/ `NvInferRuntime.h`（运行时类型），plugin 类都放在 `nvinfer1::` 命名空间下。

## 4. 核心概念与源码讲解

### 4.1 为什么要把 FT 封装成 TensorRT plugin

#### 4.1.1 概念说明

FT 本身已经是一个完整的推理库：你可以在 C++ 里直接 `new BertFP8(...)` 然后 `forward`（见 u1-l4、u9-l3）。那为什么还要再套一层 TensorRT？

关键在于 **TensorRT 提供了一条「网络→engine」的生产流水线**，而 plugin 是让 FT 接入这条流水线的「插头」：

- **融入 TRT 的图优化**：如果你的模型里既有 FT 擅长的部分（如 FP8 BERT），又有 TRT 自己擅长的部分（如 INT8 conv、slice、reshape），把它们拼在同一张 TRT 网络里，TRT 可以跨层做融合与重排，省掉中间缓冲的读写。
- **序列化 engine**：TRT 把整张网络（含 plugin 内部的权重）`serialize` 成一段字节流存盘，部署时 `deserialize` 直接还原，权重加载、kernel 选择都在 build 阶段一次性完成。这一点是裸 FT C++ 调用没有的——裸调用每次启动都要重新从 `.bin` 读权重。
- **统一运行时**：一份 engine 可以喂给 `trtexec`、嵌入 TensorRT server、或与其它 TRT 算子混用，无需自己写 main。

代价是：plugin 必须严格遵循 TRT 的 `IPluginV2DynamicExt` 接口契约（一整套必须实现的虚函数），并把 FT 模型「藏」在 plugin 内部——封装与维护成本不低。所以 **TRT plugin 适合「需要和别的 TRT 算子混合 / 需要 engine 序列化」的场景**；如果只是单独跑 FT 模型，直接用 FT C++ 或 Triton backend 往往更省事。

#### 4.1.2 核心流程

把一个 FT 模型变成 TRT plugin，从用户视角分两个阶段：

1. **Build（编译）阶段**：用户写一段 TRT 网络定义，用 plugin Creator 的 `createPlugin(name, PluginFieldCollection)` 从「字段集合」里读取超参（head_num、num_layers、权重目录路径……），构造出 plugin 实例挂到网络上；plugin 在 `initialize()` 里真正加载权重、构造 FT 模型；最后 TRT 把整个网络 `serialize` 成 engine 字节流，plugin 的 `serialize()` 把权重也写进这段字节流。
2. **Runtime（运行）阶段**：用户加载 engine，TRT 用 `deserializePlugin(name, serialData, length)` 调用 plugin 的「反序列化构造函数」从字节流还原 plugin；之后每次推理，TRT 调用 plugin 的 `enqueue(...)`，plugin 在这里把 TRT 传进来的张量交给 FT 的 `forward`。

```
┌──────── build 阶段 ─────────┐        ┌──────── runtime 阶段 ────────┐
│ Creator::createPlugin       │        │ Creator::deserializePlugin    │
│   → 读 PluginFieldCollection│        │   → new Plugin(data, length)  │
│   → new Plugin(cfg, weightDir)│      │     (从字节流还原权重)        │
│ Plugin::initialize()        │        │ Plugin::enqueue(...)          │
│   → loadModel + transpose   │  ───►  │   → ft::TensorMap 拼装        │
│   → new FT 模型             │ engine │   → FT 模型 forward           │
│ Plugin::serialize()         │ bytes  │   (TRT stream ↔ FT stream 同步)│
│   → 写 config + 写权重      │        │                               │
└─────────────────────────────┘        └───────────────────────────────┘
```

#### 4.1.3 源码精读：两道编译闸门

第一道闸门在顶层 CMake：

[顶层 CMakeLists.txt:L50-L50](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L50-L50) 声明 `BUILD_TRT`，默认 `OFF`（与 `BUILD_PYT`/`BUILD_TF` 一样属于「框架外壳」组，默认不编）：

```cmake
option(BUILD_TRT "Build projects about TensorRT" OFF)
```

第二道闸门在 `src/fastertransformer/CMakeLists.txt`，只有开了 `BUILD_TRT` 才把整棵 plugin 子树加进来（承接 u1-l2 的「框架类开关默认 OFF」分组）：

[src/fastertransformer/CMakeLists.txt:L26-L27](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/CMakeLists.txt#L26-L27) —— `BUILD_TRT` 是 plugin 子树的总闸门：

```cmake
if(BUILD_TRT)
    add_subdirectory(tensorrt_plugin)
```

进入子树后，`tensorrt_plugin/CMakeLists.txt` 再按模型逐个 `add_subdirectory`，其中 `bert_fp8` 还套了第三道条件 `ENABLE_FP8`（承接 u9-l3 的 FP8 三门槛之一）：

[tensorrt_plugin/CMakeLists.txt:L15-L22](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tensorrt_plugin/CMakeLists.txt#L15-L22) —— swin/t5/vit 无条件编，bert_fp8 需要 `ENABLE_FP8`，wenet 无条件编：

```cmake
add_subdirectory(swin)
add_subdirectory(t5)
add_subdirectory(vit)
if(ENABLE_FP8)
    add_subdirectory(bert_fp8)
endif()

add_subdirectory(wenet)
```

> 注意：`BUILD_TRT` 只控制「编译 plugin 这份 `.so`」。要真正生成 engine，你还需要本机装好 TensorRT（提供 `NvInfer.h` 头与 `-lnvinfer` 库），并写一段 build engine 的驱动代码——这部分不在本仓库内（仓库只提供 plugin 本身）。

#### 4.1.4 代码实践

**实践目标**：理解「不开 `BUILD_TRT` 时，plugin 代码完全不参与编译」这一零成本降级（与 u7-l2 的 `BUILD_MULTI_GPU` 退化为空壳同构）。

**操作步骤**：
1. 打开 `src/fastertransformer/CMakeLists.txt`，确认 `tensorrt_plugin` 被包在 `if(BUILD_TRT)` 内。
2. 设想两条 cmake 命令：
   - `cmake -DSM=80 ..`（不传 `BUILD_TRT`）→ plugin 不编。
   - `cmake -DSM=90 -DBUILD_TRT=ON -DENABLE_FP8=ON ..` → 编出 5 个 plugin 的 `.so`，含 `bert_fp8_plugin`。

**需要观察的现象**：第一条命令的构建产物里不会出现 `*_plugin.so`；第二条会出现 `bert_fp8_plugin`、`swin_plugin` 等共享库（名字见各模型 `CMakeLists` 的 `LIB_NAME`）。

**预期结果**：确认 plugin 是「可选外壳」，对纯 FT C++ 用户完全透明。

> 待本地验证：实际 `.so` 名字与是否生成，需在装有 TensorRT 的机器上跑一次 cmake + make 才能确认（本仓库环境不一定有 TensorRT）。

#### 4.1.5 小练习与答案

**Q1**：为什么 `BUILD_TRT` 默认是 `OFF`？  
**A1**：因为它属于「框架外壳」组（u1-l2），依赖外部 TensorRT，且大多数 FT 用户只想用 C++/PyTorch，不需要 TRT 集成；默认 OFF 可避免无 TRT 环境的机器编译失败。

**Q2**：`bert_fp8` 的 plugin 为什么比 `swin`/`vit` 多一道 `ENABLE_FP8` 守卫？  
**A2**：因为它的底层 FT 模型 `BertFP8` 本身依赖 FP8（u9-l3），FP8 需要 CUDA≥11.8 + Hopper sm_90 + `-DENABLE_FP8=ON`，缺任一条件连 FT 侧的 `BertFP8` 类都不存在，plugin 自然也无法编译。

---

### 4.2 TensorRT plugin 的「三件套」：Config / Plugin / Creator

#### 4.2.1 概念说明

FT 的每个 TRT plugin 由三部分组成，这是 TRT 官方推荐的写法，FT 严格沿用：

1. **`Config` 结构体**：保存「可序列化的模型超参」（head_num、num_layers、max_seq_len、fp8_mode 等）。它提供 `serialize(buffer)` / `getSerializationSize()` / 「从 buffer 构造」三件套，用于把配置写进/读出 engine 字节流。
2. **`Plugin` 类**：继承 `nvinfer1::IPluginV2DynamicExt`，是真正的「自定义层」。它内部**直接持有一个 FT 模型实例**（如 `std::unique_ptr<BertFP8<fp8_t,bf16_t>> mBertModel`），并在 `enqueue()` 里调用它的 `forward`。
3. **`Creator` 类**：继承 `nvinfer1::IPluginCreator`，是工厂。TRT 通过它按名字创建 plugin（`createPlugin`，build 阶段）或反序列化（`deserializePlugin`，runtime 阶段）。

> 对比 u10-l2 的 TF `REGISTER_OP`/`OpKernel`/`REGISTER_KERNEL_BUILDER` 三件套：TF 用编译期宏注册，TRT 用运行期 Creator 工厂 + `REGISTER_TENSORRT_PLUGIN` 宏注册。思路一致——都是「声明签名 + 实现执行体 + 注册」。

`IPluginV2DynamicExt` 比 `IPluginV2Ext` 多了「动态形状」能力：`getOutputDimensions` 用 `DimsExprs` 在运行期根据输入形状推导输出形状，`enqueue` 里再从 `inputDesc[i].dims` 读真实尺寸。这对 BERT 这种 batch/seq 可变的模型必不可少。

#### 4.2.2 核心流程：Plugin 的生命周期

TRT 在不同时刻调用 plugin 的不同回调，FT 的实现职责如下：

| 时刻 | TRT 调用 | FT 侧职责 |
| :--- | :--- | :--- |
| build，挂到网络 | `Creator::createPlugin(name, fc)` | 从 `PluginFieldCollection` 读超参 → 构造 `Config` → `new Plugin(cfg, weightDir)` |
| build，准备运行 | `Plugin::initialize()` | 加载权重（`loadModel`+`transposeWeight`）、建 allocator/cublas、`new FT 模型` |
| build，存盘 | `Plugin::serialize()` | 写 `Config` + 写权重 |
| runtime，加载 engine | `Creator::deserializePlugin(name, data, len)` | `new Plugin(data, length)`（反序列化构造函数从字节流还原权重） |
| runtime，每次推理 | `Plugin::enqueue(...)` | 拼装 `ft::TensorMap` → `mBertModel->forward(...)` |
| 复制（多 execution context） | `Plugin::clone()` | 共享权重（`shared_ptr`）、重建模型 |

关键设计：`mBertWeights` 是 `std::shared_ptr`，`clone()` 时把权重指针共享给副本，避免每个 execution context 都拷一份权重（注释里写明 “can share weights among different execution contexts”）。

#### 4.2.3 源码精读：bert_fp8 的三件套

先看 `Config`——一个纯 POD 结构 + 顺序读写：

[bertFp8Plugin.h:L100-L170](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tensorrt_plugin/bert_fp8/bertFp8Plugin.h#L100-L170) 是 `BertFp8Config`，包含 9 个 int32 超参与两个派生量（`hidden_units = num_heads*size_per_head`、`intermediate_size = 4*hidden_units`），并提供「从 buffer 读」「写回 buffer」「返回 9×sizeof(int32_t) 大小」三个方法。`write`/`read` 是模板化的逐字段搬运：

```cpp
template<typename T>
void write(uint8_t*& buffer, const T& val) { *reinterpret_cast<T*>(buffer) = val; buffer += sizeof(T); }
template<typename T>
void read(const uint8_t*& buffer, T& val) { val = *reinterpret_cast<const T*>(buffer); buffer += sizeof(T); }
```

> 注意这种「无长度前缀、按固定顺序平铺」的序列化要求 build 与 runtime 的字段顺序**完全一致**——一旦改了 `Config` 字段顺序，旧 engine 就反序列化不出来。这是 plugin 的固有脆弱点。

再看 `Plugin` 类——核心是它内嵌的 FT 模型与权重成员：

[bertFp8Plugin.h:L172-L245](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tensorrt_plugin/bert_fp8/bertFp8Plugin.h#L172-L245) —— `class BertFp8Plugin: public nvinfer1::IPluginV2DynamicExt`。最关键的私有成员是这两个：

```cpp
// can share weights among different execution contexts
std::shared_ptr<fastertransformer::BertFP8Weight<fp8_t, bf16_t>> mBertWeights;
std::unique_ptr<fastertransformer::BertFP8<fp8_t, bf16_t>>       mBertModel;
```

> 这就是「封装」的全部本质：plugin 不重新实现 BERT，只是把一个现成的 `BertFP8` 对象攥在手里。`fp8_t = __nv_fp8_e4m3`、`bf16_t = __nv_bfloat16`，承接 u9-l3 的 FP8 实例化。

最后是 `Creator`：

[bertFp8Plugin.h:L247-L271](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tensorrt_plugin/bert_fp8/bertFp8Plugin.h#L247-L271) —— `class BertFp8PluginCreator: public nvinfer1::IPluginCreator`，工厂只负责 `createPlugin`（build）与 `deserializePlugin`（runtime）两条路径。

整个 plugin 库靠一行宏注册到 TRT 的全局 plugin registry：

[bertFp8Plugin.cu:L33-L33](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tensorrt_plugin/bert_fp8/bertFp8Plugin.cu#L33-L33) —— 这是 plugin 被 TRT「按名字找到」的入口（宏来自 `NvInferPlugin.h`）：

```cpp
REGISTER_TENSORRT_PLUGIN(BertFp8PluginCreator);
```

#### 4.2.4 代码实践

**实践目标**：对照 `BertFp8Plugin` 的成员，理解「plugin = 一层壳 + 一个 FT 模型指针」。

**操作步骤**：
1. 打开 `bertFp8Plugin.h`，找到 `mBertModel`（第 244 行）和 `mBertWeights`（第 243 行）。
2. 想象把这两行删掉——plugin 还能干什么？答案：什么都不能，它就只剩一堆 TRT 回调的空壳。

**需要观察的现象**：plugin 类里**没有任何**矩阵乘、注意力、layernorm 的实现代码——全部来自它持有的 FT 模型。

**预期结果**：确认「封装」而非「重写」——这正是 FT 所有框架外壳（th_op/tf_op/triton_backend/tensorrt_plugin）的共同哲学（u1-l3）。

#### 4.2.5 小练习与答案

**Q1**：为什么 `mBertWeights` 用 `shared_ptr` 而 `mBertModel` 用 `unique_ptr`？  
**A1**：权重体积大、只读，多个 execution context（通过 `clone()` 产生）可以共享同一份权重，故用 `shared_ptr`；模型对象持有 stream/allocator 等执行环境、不能共享，故 `unique_ptr`。

**Q2**：`IPluginV2DynamicExt` 中的 “Dynamic” 体现在哪里？  
**A2**：体现在 `getOutputDimensions` 用 `DimsExprs`（表达式）而非固定整数描述输出形状，运行期 `enqueue` 再从 `inputDesc[i].dims` 读真实 batch/seq——支持可变形状输入。

---

### 4.3 enqueue：把 TRT 张量零拷贝桥接进 FT forward

#### 4.3.1 概念说明

`enqueue` 是 plugin 唯一的「执行体」，TRT 在每次推理时调用它。它要做四件事：

1. **读形状**：从 `inputDesc[0].dims` 拿到运行期真实的 `batchSize`、`maxSeqLen`（动态形状的体现）。
2. **裸指针转换**：TRT 传入的 `inputs[i]` / `outputs[i]` 是 `void*`，强转成 `const int32_t*` 等类型指针。
3. **拼装 `ft::TensorMap`**：用 `ft::MEMORY_GPU` + `ft::TYPE_INT32` 把裸指针包成非拥有的 `ft::Tensor`（u2-l1：Tensor 不分配内存，只持有指针+形状）——**零拷贝**。
4. **同步 stream + 调 forward**：FT 内部用自己的 cublas stream，而 TRT 用自己的 stream，两者必须用 `cudaEvent` 做一次握手，否则会乱序。

这与 u10-l2 的 `convert_tensor`（TF→FT 零拷贝）、u10-l3 的 `convertTritonTensorToFt` 完全同构——都是「指针直接包装，不搬数据」。

#### 4.3.2 核心流程：stream 握手

```
TRT stream                    FT(cublas) stream
   │ enqueue() 被调用              │
   ├─ cudaEventRecord(E, TRT)     │
   ├─ cudaStreamWaitEvent(FT, E)──┼──▶ 等 TRT 把上游算子算完
   │                              ├─ mBertModel->forward(...)
   │                              ├─ cudaEventRecord(E, FT)
   ├─ cudaStreamWaitEvent(TRT, E)─┘   等 FT 算完
   └─ enqueue 返回（TRT 继续下游算子）
```

为什么必须握手？因为 cuBLAS handle 在 `initialize()` 里绑到了 plugin 自己创建的 `mCublasCtx->mStream`，而 TRT 调用 `enqueue` 时传进来的 `stream` 是 TRT runtime 的流。两个流上的操作默认不同步，必须靠 event 做「等待点」，保证 FT 读输入时 TRT 上游已写完、TRT 读输出时 FT 已写完。

#### 4.3.3 源码精读：enqueue 全文

[bertFp8Plugin.cu:L234-L273](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tensorrt_plugin/bert_fp8/bertFp8Plugin.cu#L234-L273) 是 `BertFp8Plugin::enqueue` 的全部，只保留关键行：

```cpp
int32_t batchSize        = inputDesc[0].dims.d[0];   // 动态形状：运行期才知 batch
int32_t maxSeqLenInBatch = inputDesc[0].dims.d[1];
const int32_t* inputIds        = static_cast<const int32_t*>(inputs[0]);  // 零拷贝
const int32_t* tokenTypeIds    = static_cast<const int32_t*>(inputs[1]);
const int32_t* sequenceLengths = static_cast<const int32_t*>(inputs[2]);

auto inputTensors = ft::TensorMap(std::unordered_map<std::string, ft::Tensor>{
    {"input_ids",        ft::Tensor{ft::MEMORY_GPU, ft::TYPE_INT32, {batchSize_s, maxSeqLen_s}, inputIds}},
    {"sequence_lengths", ft::Tensor{ft::MEMORY_GPU, ft::TYPE_INT32, {batchSize_s}, sequenceLengths}},
    {"token_type_ids",   ft::Tensor{ft::MEMORY_GPU, ft::TYPE_INT32, {batchSize_s, maxSeqLen_s}, tokenTypeIds}}});
auto outputTensors = ft::TensorMap(...{"output_hidden_state", ...outputs[0]});

FT_CHECK(cudaEventRecord(mSyncEvent.get(), stream) == cudaSuccess);
FT_CHECK(cudaStreamWaitEvent(mCublasCtx->mStream, mSyncEvent.get(), 0) == cudaSuccess);
mBertModel->forward(&outputTensors, &inputTensors, mBertWeights.get());   // FT 统一接口
FT_CHECK(cudaEventRecord(mSyncEvent.get(), mCublasCtx->mStream) == cudaSuccess);
FT_CHECK(cudaStreamWaitEvent(stream, mSyncEvent.get(), 0) == cudaSuccess);
return 0;
```

对照要点：
- `inputTensors` / `outputTensors` 的 key（`input_ids`、`sequence_lengths`、`output_hidden_state`）正是 FT `BertFP8::forward` 期望的 `TensorMap` key（u4-l1 的统一接口），plugin 只是个翻译层。
- `ft::getTensorType<half>()` 用来推导输出张量的 DataType（承接 u2-l1 的「枚举↔模板」桥梁）。
- 两次 `cudaEventRecord` + `cudaStreamWaitEvent` 实现上面流程图的双向握手。

`initialize()` 里则负责建好这些设备环境：

[bertFp8Plugin.cu:L63-L120](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tensorrt_plugin/bert_fp8/bertFp8Plugin.cu#L63-L120) —— `BertFp8Plugin::initialize()` 在 build 阶段被调用一次，做四件事：加载并转置权重（`mBertWeights->loadModel` / `transposeWeight`）、建 `Allocator<CUDA>`、建 `CublasCtx`（含 cublasLt handle、`cublasFP8MMWrapper`、`cublasAlgoMap("gemm_config.in")`，全部承接 u2-l2/u2-l3/u9-l3）、最后用 `getAttentionType` 推导注意力类型并 `new BertFP8<fp8_t,bf16_t>(...)`。注意 `mTensorPara`/`mPipelinePara` 用默认值（单卡 `{0,1}`，承接 u7-l1）——这个 plugin 当前是单卡的。

反序列化构造函数则在 runtime 阶段从字节流还原权重，不重新 `loadModel`：

[bertFp8Plugin.cu:L42-L61](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tensorrt_plugin/bert_fp8/bertFp8Plugin.cu#L42-L61) —— 注释写明 “assume this is called ONCE PER GPU”，因为 `deserialize()` 会把权重拷到**当前** GPU 的显存。

序列化与 build 期的 `createPlugin`：

[bertFp8Plugin.cu:L282-L287](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tensorrt_plugin/bert_fp8/bertFp8Plugin.cu#L282-L287) —— `serialize()` 先写 `Config` 再写权重。

[bertFp8Plugin.cu:L320-L374](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tensorrt_plugin/bert_fp8/bertFp8Plugin.cu#L320-L374) —— `createPlugin` 用两个 `std::map<string, T*>` 把 `PluginFieldCollection` 里的字段按名字映射到本地变量（9 个 int + 1 个 `weightDirPath` 字符串），读完校验 `found == 全部字段数`，再构造 `Config` 并 `new BertFp8Plugin(cfg, weightDirPath)`。

[bertFp8Plugin.cu:L376-L380](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tensorrt_plugin/bert_fp8/bertFp8Plugin.cu#L376-L380) —— `deserializePlugin` 一行：`new BertFp8Plugin(serialData, serialLength)`，交给反序列化构造函数。

#### 4.3.4 代码实践

**实践目标**：理解 `enqueue` 是「TRT 张量 → FT TensorMap」的零拷贝翻译层。

**操作步骤**：
1. 打开 `bertFp8Plugin.cu` 的 `enqueue`（L234），数一数它构造了几个 `ft::Tensor`：3 个输入（input_ids / sequence_lengths / token_type_ids）+ 1 个输出（output_hidden_state）。
2. 对照 u2-l1 的 `Tensor` 构造函数签名 `Tensor{MemoryType, DataType, vector<size_t> shape, void* data}`，确认每个 `ft::MEMORY_GPU` 张量都直接复用了 TRT 的 `inputs[i]` 指针，没有任何 `cudaMemcpy`。

**需要观察的现象**：`enqueue` 全程**没有一次显存拷贝**，唯一的跨流开销是两个 `cudaEvent`。

**预期结果**：确认 plugin 不引入数据搬运开销，FT 与 TRT 共享同一块显存。

> 待本地验证：若想看实际握手时序，可用 Nsight Systems（`FT_NVTX=1`，见 u1-l5）抓一次推理的 timeline，会看到 TRT stream 与 FT cublas stream 之间的 event 等待。

#### 4.3.5 小练习与答案

**Q1**：如果删掉两次 `cudaEventRecord` + `cudaStreamWaitEvent`，会发生什么？  
**A1**：FT 的 cublas stream 与 TRT stream 失去同步，FT 可能在 TRT 上游写完输入前就读输入（读到脏数据），或在 TRT 下游读输出前 FT 还没写完（读到半成品），表现为结果偶发错误。

**Q2**：`enqueue` 里的 `batchSize` 从哪里来？为什么不能写死？  
**A2**：从 `inputDesc[0].dims.d[0]` 来。因为是动态形状 plugin，每次推理的 batch 可能不同，必须运行期读取，这正是 `IPluginV2DynamicExt` 存在的意义。

---

### 4.4 支持的模型集合与精度变体

#### 4.4.1 概念说明

`tensorrt_plugin` 当前支持 **5 类模型**，每类是一个子目录：

| 子目录 | 底层 FT 模型 | 精度变体 | 备注 |
| :--- | :--- | :--- | :--- |
| `bert_fp8/` | `BertFP8` | FP8 (E4M3) | 仅 `ENABLE_FP8` 时编译，单 plugin |
| `vit/` | `ViT` | FP16 + INT8 | 两个 plugin（`ViTPlugin` / `ViTINT8Plugin`） |
| `swin/` | `Swin` | FP16 + INT8 | 两个 plugin（`SwinTransformerPlugin` / `SwinTransformerINT8Plugin`） |
| `t5/` | T5 encoder + decoding | FP16/FP32 | 拆成 encoder / decoding **两个** plugin |
| `wenet/` | WeNet encoder + decoder | FP16 | 拆成 encoder / decoder **两个** plugin |

规律：纯前馈的编码器（ViT/Swin/BERT）是「一个模型一个 plugin」；encoder-decoder 模型（T5）和流水线分段的模型（WeNet）则拆成「encoder plugin + decoder/decoding plugin」两个，让 TRT 网络可以在中间插入别的算子。精度变体通过「同一目录下并列两个 plugin 类」实现（如 swin 的 `swinTransformerPlugin` 与 `swinTransformerINT8Plugin`），而非运行期开关。

#### 4.4.2 核心流程：plugin 如何被链接成一个独立 .so

每个 plugin子目录都有自己的 `CMakeLists.txt`，把 plugin 源文件编译成一个独立 `SHARED` 库，并链接底层 FT 模型所需的全部依赖。以 `bert_fp8` 为例：

[bert_fp8/CMakeLists.txt:L20-L30](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tensorrt_plugin/bert_fp8/CMakeLists.txt#L20-L30) —— 产物是 `bert_fp8_plugin` 共享库，链接一长串 FT 静态库 + TRT 的 `-lnvinfer`：

```cmake
if(BUILD_TRT)
  set(LIB_NAME "bert_fp8_plugin")
  add_library(${LIB_NAME} SHARED ${bert_fp8_trt_files})
  set_target_properties(${LIB_NAME} PROPERTIES CUDA_RESOLVE_DEVICE_SYMBOLS ON)
  target_link_libraries(${LIB_NAME} BertFP8 BertFP8Weight activation_kernels activation_fp8_kernels
                        memory_utils layernorm_fp8_kernels cuda_fp8_utils ... cublasFP8MMWrapper ...
                        tensor -lcudnn -lcublas -lcudart -lnvinfer)
endif()
```

要点：
- `CUDA_RESOLVE_DEVICE_SYMBOLS ON`：让 host 侧链接时能正确解析 `.cu` 里的 device 符号（plugin 的 `.cu` 里有 kernel）。
- 末尾 `-lnvinfer` 是 TRT 运行时库，`-lcudnn`/`-lcublas` 是 FT 用到的底层库。
- 每类模型产出**独立** `.so`（如 `bert_fp8_plugin`、`swin_plugin`），用户在 build engine 前用 `initLibNvInferPlugins` 或按需 `dlopen` 加载对应的 `.so`，让 TRT registry 能找到该 plugin。

#### 4.4.3 源码精读：5 类 plugin 的入口类

- **ViT**（FP16）：[ViTPlugin.h:L34-L37](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tensorrt_plugin/vit/ViTPlugin.h#L34-L37) 定义 plugin 名 `"CustomVisionTransformerPlugin"`，[ViTPlugin.h:L57-L58](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tensorrt_plugin/vit/ViTPlugin.h#L57-L58) `class VisionTransformerPlugin: public nvinfer1::IPluginV2DynamicExt`，内部直接 `#include "models/vit/ViT.h"`（u4-l3）。同目录还有 `ViTINT8Plugin` 提供 INT8 变体。
- **Swin**（FP16 + INT8）：[swinTransformerPlugin.h:L62-L62](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tensorrt_plugin/swin/swinTransformerPlugin.h#L62-L62) `class SwinTransformerPlugin`（名 `"CustomSwinTransformerPlugin"`），INT8 变体 [swinTransformerINT8Plugin.h:L62-L62](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tensorrt_plugin/swin/swinTransformerINT8Plugin.h#L62-L62) 名 `"CustomSwinTransformerINT8Plugin"`。
- **T5**（encoder + decoding 两个 plugin）：[t5/README.md:L11-L64](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tensorrt_plugin/t5/README.md#L11-L64) 用表格列出了两个 plugin 的输入张量契约（`T5EncoderPlugin` 吃 `[batch, seq]` token id，输出 `[batch, seq, d_model]`；`T5DecodingPlugin` 吃 encoder 输出，输出 `[batch, beam, seq]`）。README 里还有运行期采样参数（top_k/top_p/temperature/len_penalty/repetition_penalty）作为 `PluginField` 传入——与 u8 的 DynamicDecode 同源。
- **WeNet**（encoder + decoder）：[wenet/EncoderPlugin.h:L18-L19](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tensorrt_plugin/wenet/EncoderPlugin.h#L18-L19) 直接 `#include "models/wenet/WenetEncoder.h"`，同目录有 `DecoderPlugin`。
- **BERT FP8**：即本讲主线 `BertFp8Plugin`（见 4.2/4.3）。

README 支持矩阵也印证了这一集合——T5/Swin/ViT 在 TensorRT 列均标 “Yes”：

[README.md:L60-L65](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/README.md#L60-L65) 支持矩阵中这三行，是判断「某模型是否有 TRT plugin」的权威来源。`README.md:L87-L87` 还把 `tensorrt_plugin` 描述为 “encapluate FasterTransformer into TensorRT plugin”（原文拼写）。

#### 4.4.4 代码实践

**实践目标**：列出 5 类 plugin，并对比「裸 FT 模型」与「TRT plugin」的取舍。

**操作步骤**：
1. 在 `tensorrt_plugin/` 下数子目录：`bert_fp8`、`swin`、`t5`、`vit`、`wenet`，共 5 类。
2. 列一张表，分两栏「收益 / 代价」：
   - **收益**：① 融入 TRT 图优化（可与 TRT 原生 conv/slice/reshape 等层混排、跨层融合）；② 序列化 engine（权重在 build 期加载、运行期 `deserialize` 直接还原，省去每次启动读 `.bin`）；③ 统一运行时（`trtexec`、嵌入式 TRT server）；④ INT8/FP8 plugin 复用 FT 已校准好的 kernel（如 swin INT8、bert FP8）。
   - **代价**：① 必须实现一整套 `IPluginV2DynamicExt` 回调（initialize/enqueue/serialize/clone/configurePlugin/supportsFormatCombination…）；② 序列化格式脆弱（改 `Config` 字段顺序就破坏旧 engine）；③ 每类模型一个独立 `.so`、需手动加载注册；④ 当前 plugin 多为单卡（`bert_fp8` 的 TP/PP 是默认 `{0,1}`），多卡并行不如 Triton backend（u10-l3）灵活；⑤ 本仓库 FT 已冻结、转向 TensorRT-LLM，新场景应直接用 TRT-LLM。

**需要观察的现象**：5 类 plugin 中，encoder-decoder 模型（t5/wenet）天然拆成两个 plugin，给 TRT 在中间插算子的空间；纯编码器（vit/swin/bert）则是一个 plugin 包整模型。

**预期结果**：能说清「什么场景值得做 TRT plugin」——需要和别的 TRT 算子混合、或需要 engine 序列化与统一 TRT 运行时时才值得；否则裸 FT C++ 或 Triton backend 更简单。

> 待本地验证：实际能否 build 出 engine，需在有 TensorRT + 对应模型权重的机器上跑驱动代码，本仓库不含该驱动。

#### 4.4.5 小练习与答案

**Q1**：为什么 swin 在同一个目录下放 `swinTransformerPlugin` 和 `swinTransformerINT8Plugin` 两个类，而不是一个类加运行期开关？  
**A1**：因为 TRT plugin 的精度由 `supportsFormatCombination` 在 build 期决定，FP16 与 INT8 的数据布局、量化 scale、kernel 选择差异大，用两个独立 plugin 类更清晰；这也是 FT「精度特化用类型/类区分」一贯风格（对比 u3-l1 激活用模板、u9 INT8 用独立模型类）。

**Q2**：T5 的 encoder 和 decoding 拆成两个 plugin，相比合成一个有什么好处？  
**A2**：拆开后，TRT 可以在 encoder 输出与 decoding 输入之间插入别的 TRT 原生算子，或让两者分别在不同 stream/精度上调度；合成一个就失去这种灵活性，等于退化为裸 FT 调用。

---

## 5. 综合实践

**任务**：以 `bert_fp8` plugin 为模板，画出「一份 TRT engine 从 build 到推理的完整数据与控制流」，并标注每一步调用了 FT 的哪个组件。

要求你的图覆盖以下节点，并写出对应的源码行号佐证：

1. **build 期创建**：`Creator::createPlugin` 从 `PluginFieldCollection` 读 9 个 int + weightDirPath（[bertFp8Plugin.cu:L320-L374](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tensorrt_plugin/bert_fp8/bertFp8Plugin.cu#L320-L374)）。
2. **build 期初始化**：`initialize()` 调 `BertFP8Weight::loadModel`/`transposeWeight`、建 `CublasCtx`（含 `cublasFP8MMWrapper` + `gemm_config.in`）、`new BertFP8<fp8_t,bf16_t>`（[bertFp8Plugin.cu:L63-L120](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tensorrt_plugin/bert_fp8/bertFp8Plugin.cu#L63-L120)）。
3. **build 期序列化**：`serialize()` 写 `BertFp8Config` + 写权重（[bertFp8Plugin.cu:L282-L287](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tensorrt_plugin/bert_fp8/bertFp8Plugin.cu#L282-L287)）。
4. **runtime 期反序列化**：`deserializePlugin` → 反序列化构造函数 `BertFp8Plugin(data, length)`，从字节流还原 `Config` 与权重（[bertFp8Plugin.cu:L42-L61](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tensorrt_plugin/bert_fp8/bertFp8Plugin.cu#L42-L61)、[bertFp8Plugin.cu:L376-L380](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tensorrt_plugin/bert_fp8/bertFp8Plugin.cu#L376-L380)）。
5. **runtime 期推理**：`enqueue()` 拼装 `ft::TensorMap` → stream 握手 → `mBertModel->forward(&outputTensors, &inputTensors, mBertWeights.get())`（[bertFp8Plugin.cu:L234-L273](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tensorrt_plugin/bert_fp8/bertFp8Plugin.cu#L234-L273)）。
6. **编译闸门**：`BUILD_TRT`（[CMakeLists.txt:L50-L50](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L50-L50)）+ `ENABLE_FP8`（[tensorrt_plugin/CMakeLists.txt:L18-L20](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tensorrt_plugin/CMakeLists.txt#L18-L20)）。

最后用一句话回答：「在已经有 `BertFP8` 这个 FT C++ 模型的前提下，封装成 TRT plugin 的核心收益与核心代价分别是什么？」（参考答案：核心收益是获得 TRT 的 engine 序列化与图融合能力、统一 TRT 运行时；核心代价是必须维护一整套 `IPluginV2DynamicExt` 回调与脆弱的序列化格式，且当前仅单卡。）

## 6. 本讲小结

- `tensorrt_plugin` 是 FT 的第四个框架外壳（前三：th_op / tf_op / triton_backend），对应外部框架是 **TensorRT**，本质仍是「指针桥接 + 持有一个 FT 模型」，不做任何重算。
- 编译受两（三）道闸门控制：`BUILD_TRT`（顶层 `CMakeLists.txt:50`）是总开关，`tensorrt_plugin/CMakeLists.txt` 逐模型 `add_subdirectory`，其中 `bert_fp8` 额外需要 `ENABLE_FP8`。
- 每个 plugin 是 **Config / Plugin / Creator 三件套**：`Plugin` 继承 `IPluginV2DynamicExt` 内嵌一个 FT 模型（如 `BertFP8<fp8_t,bf16_t>`），`Creator` 是工厂，靠 `REGISTER_TENSORRT_PLUGIN` 宏注册进 TRT 全局 registry。
- **生命周期**：build 期 `createPlugin`→`initialize`（loadModel + 建 cublas + new FT 模型）→`serialize`；runtime 期 `deserializePlugin`→反序列化构造→`enqueue` 调 `forward`。
- `enqueue` 是核心执行体：从 `inputDesc` 读动态形状、把 TRT 的 `void*` 零拷贝包成 `ft::TensorMap`，用两次 `cudaEvent` 做 TRT stream 与 FT cublas stream 的握手，再调 FT 统一 `forward`。
- 支持 **5 类模型**：bert_fp8（FP8）、vit（FP16/INT8）、swin（FP16/INT8）、t5（encoder+decoding）、wenet（encoder+decoder）；纯编码器一个 plugin，encoder-decoder 拆两个。
- 取舍：TRT plugin 的价值在于「融入 TRT 图优化 + engine 序列化 + 统一运行时」，代价是回调维护与序列化格式脆弱；若不需这些，裸 FT C++ 或 Triton backend（u10-l3）更简单。

## 7. 下一步学习建议

- **对比 Triton backend**（u10-l3）：Triton 用 `AbstractTransformerModel/Instance` 两层抽象管多请求并发与多卡，而 TRT plugin 更偏「单 engine、build 一次反复推理」。需要服务化多请求并发选 Triton，需要与其它 TRT 算子融合或序列化 engine 选 plugin。
- **深入 FP8**（u9-l3）：`bert_fp8` plugin 直接实例化 `BertFP8<__nv_fp8_e4m3, __nv_bfloat16>`，想理解 `initialize()` 里建的 `cublasFP8MMWrapper` 与 FP8 kernel，回去重读 u9-l3。
- **看一个非 FP8 plugin 的完整实现**：读 `tensorrt_plugin/swin/swinTransformerPlugin.cpp` 与 `swinTransformerINT8Plugin.cpp`，对比 FP16 与 INT8 两个 plugin 在 `supportsFormatCombination`、`configurePlugin` 上的差异，体会「精度特化用并列类」的设计。
- **T5 plugin 的输入输出契约**：读 `tensorrt_plugin/t5/README.md` 的两张表，对照 u6-l4 的 T5 模型理解 encoder/decoding 两个 plugin 如何对应 FT 的 `T5Encoder` 与 `T5Decoding`。
- **迁移到 TensorRT-LLM**：本仓库 FT 已冻结，TRT plugin 这条路线的官方演进是 [TensorRT-LLM](https://github.com/NVIDIA/TensorRT-LLM)；学完本讲后建议把「plugin 三件套 + enqueue 桥接」的思想迁移过去对照阅读。
