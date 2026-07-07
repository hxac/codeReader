# Triton backend 部署

## 1. 本讲目标

本讲讲解 FasterTransformer（FT）如何把自己包装成 Triton Inference Server 的自定义 backend（custom backend），从而具备「接收客户端请求 → 推理 → 返回响应」的完整服务能力。

学完后你应当掌握：

- 理解 `AbstractTransformerModel` 与 `AbstractTransformerModelInstance` 两层抽象的分层意义，以及它为何与 Triton 自身的 `TritonModel` / `TritonModelInstance` 分层一一对应。
- 掌握 `multi_gpu_gpt` / `bert` / `t5` / `gptj` / `gptneox` 等 backend 在 `triton_backend/` 目录下的组织方式。
- 理解 `USE_TRITONSERVER_DATATYPE` 编译开关的作用：让同一份 backend 代码既能编进真正的 tritonserver，也能脱离 tritonserver 独立编译。
- 看懂 `multi_gpu_gpt_triton_example.cc` 如何用「线程做节点内（intra-node）并行、MPI 做节点间（inter-node）并行」来组织多 GPU/多节点推理。

## 2. 前置知识

阅读本讲前，建议你已经掌握以下内容（对应前置讲义）：

- **u6-l1 ParallelGpt 架构**：FT 把 GPT 一次生成拆成 context 阶段（处理整段 prompt、写满 KV cache）与 decoder 阶段（逐 token 自回归）。本讲的 ModelInstance 最终调用的就是 `ParallelGpt::forward`。
- **u7-l2 流水并行与 MPI**：`world_size = tensor_para_size × pipeline_para_size` 的硬约束，以及 `global_rank = pp_rank·TP + tp_rank` 的二维拓扑划分。本讲 `createNcclParams` 与示例的 rank 计算完全沿用这套约定。
- **u2-l1 Tensor / TensorMap**：FT 内部统一的非拥有张量描述符，`where` 标记 CPU/GPU。本讲要在它之上再架一层 `triton::Tensor`。
- **u2-l2 Allocator**：`IAllocator::reMalloc` 的 REUSE/INCREASE/DECREASE 三态，ModelInstance 的输出 buffer 复用就靠它。

几个本讲要用到的概念先说清楚：

- **Triton Inference Server**：NVIDIA 的开源推理服务框架。它定义了一套「backend」接口：你实现一个动态库，暴露 `TRITONBACKEND_Initialize` / `ModelInitialize` / `ModelInstanceInitialize` / `ModelInstanceExecute` 等入口，Triton 就能加载你的库、管理模型生命周期、并把 HTTP/gRPC 请求分发给你。FT 的 `triton_backend/` 就是这套 backend 的实现骨架。
- **custom backend（自定义后端）**：相对于 Triton 内置的 onnxruntime/tensorrt/pytorch 后端而言，custom backend 让你用任意 C++ 代码服务一个模型，FT 走的就是这条路。
- **Model 与 ModelInstance 的区别**：Model 是「模型级」对象，负责加载权重、解析配置、构造并行通信域，整个模型只建一次；ModelInstance 是「实例级」对象，绑定到具体一张 GPU 上的一条 stream，负责一次请求的前向。一个 Model 可以派生多个 ModelInstance（多卡各一个），权重在它们之间共享。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [transformer_triton_backend.hpp](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/transformer_triton_backend.hpp) | 通用封装：定义 `triton::Tensor`、`AbstractTransformerModel` / `AbstractTransformerModelInstance` 两个抽象基类与类型桥接逻辑。是所有具体 backend 的公共根基。 |
| [transformer_triton_backend.cpp](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/transformer_triton_backend.cpp) | `AbstractTransformerModel::createNcclParams` 的实现：用 NCCL group 调用建立 TP/PP 两个通信域。 |
| [multi_gpu_gpt/ParallelGptTritonModel.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModel.cc) | GPT 的 Model 层：工厂函数读 INI、构造参数、加载权重、派生 ModelInstance。 |
| [multi_gpu_gpt/ParallelGptTritonModelInstance.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModelInstance.cc) | GPT 的 Instance 层：把 triton 张量转成 FT 张量、分配输出 buffer、调用 `ParallelGpt::forward`。 |
| [bert/BertTritonModel.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/bert/BertTritonModel.cc) | BERT 的 Model 层，结构与 GPT 同构，可对照看出「一套模板套多个模型」。 |
| [multi_gpu_gpt_triton_example.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_triton_example.cc) | 部署演示：用线程做 intra-node、MPI 做 inter-node 的完整 6 步流程。 |
| [docs/gpt_guide.md](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md) | 官方文档中关于 Triton backend 工作流的说明（第 109-123 行的 6 步流程）。 |

`triton_backend/` 目录按模型分子目录组织，每个子目录一对 `XxxTritonModel` + `XxxTritonModelInstance`：

```
triton_backend/
├── transformer_triton_backend.hpp / .cpp   # 通用根基
├── triton_utils.hpp                        # H2D 搬运、CPU/GPU 张量包装小工具
├── multi_gpu_gpt/        # ParallelGpt（含 OPT/BLOOM 变体）
├── multi_gpu_gpt_fp8/    # GPT FP8
├── bert/                 # BERT
├── t5/  +  t5-encoder/   # T5（encoder-decoder + 独立 encoder）
├── gptj/                 # GPT-J
└── gptneox/              # GPT-NeoX
```

## 4. 核心概念与源码讲解

### 4.1 Triton backend 通用封装：分层抽象与工厂

#### 4.1.1 概念说明

FT 的核心计算代码（`kernels`/`layers`/`models`）与任何推理服务框架都没有关系，它只是「给一坨 `Tensor` 输入，算出一坨 `Tensor` 输出」。要把这坨裸计算塞进 Triton server，需要一层「服务外壳」来处理三件事：

1. **模型生命周期**：启动时加载权重、解析配置、建立多 GPU 通信域；停止时释放。
2. **请求生命周期**：每个进来的请求要转成 FT 认识的张量格式、跑一次 forward、再把输出转回服务框架的格式。
3. **多 GPU/多节点编排**：在正确的 GPU 上、用正确的 rank 执行。

FT 用两个抽象基类把这三件事切成两层：

- `AbstractTransformerModel`：管模型级的事——配置、权重、通信域、派生实例。
- `AbstractTransformerModelInstance`：管请求级的事——一次 `forward`。

这种「Model 管静态资源、Instance 管动态请求」的分层并非 FT 独创，而是直接镜像了 Triton server 自身的 `TritonModel` / `TritonModelInstance` 接口：Triton 加载一个模型时建一个 Model，随后为每张 GPU 建若干 ModelInstance 来并发处理请求。FT 提前按这套结构封装，使得真正接入 tritonserver 时（在 [fastertransformer_backend](https://github.com/triton-inference-server/fastertransformer_backend) 仓库）只需把这两个抽象类的方法一一接到 Triton 的 C API 上即可。

#### 4.1.2 核心流程

`AbstractTransformerModel` 暴露的生命周期方法构成如下调用链：

```
createGptModel(inifile)            # 工厂：读 INI，返回 shared_ptr<Model>
   │
   ├─ getTensorParaSize / getPipelineParaSize   # 取出 TP/PP
   │
   ├─ createNcclParams(node_id)                 # 建 NCCL 通信域（见 4.3）
   ├─ createCustomComms(world_size)             # 可选：建自定义 all-reduce 通道
   │
   ├─ createSharedWeights(device_id, rank)      # 每张卡各调一次：加载该 rank 的权重切片
   │
   └─ createModelInstance(device_id, rank,      # 每张卡各调一次：构造 ft::ParallelGpt
                          stream, nccl, custom_comm)
            │
            └─ 返回 AbstractTransformerModelInstance
                     │
                     └─ instance.forward(input_tensors)   # 处理一次请求
```

注意一个关键设计：`createSharedWeights` 与 `createModelInstance` 是**分开**的两个调用。权重只加载一次（贵），而实例可以按需创建（便宜）。这样多个实例能共享同一份权重，也允许 Triton 在一张卡上放多个 instance 做并发。

#### 4.1.3 源码精读

两个抽象基类的定义在 [transformer_triton_backend.hpp:289-315](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/transformer_triton_backend.hpp#L289-L315)，`AbstractTransformerModel` 把前面流程图里的方法全部声明为纯虚函数，并给出了一组静态工厂：

- [transformer_triton_backend.hpp:290-295](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/transformer_triton_backend.hpp#L290-L295)：`createGptModel` / `createGptFP8Model` / `createGptJModel` / `createGptNeoXModel` / `createT5Model` / `createT5EncoderModel` 六个工厂，每个对应 `triton_backend/` 下一个子目录。调用方只拿抽象指针，不感知具体模板类型。
- [transformer_triton_backend.hpp:303-308](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/transformer_triton_backend.hpp#L303-L308)：`createModelInstance` 接收 `deviceId`、`rank`、`stream`、`nccl_params`、`custom_all_reduce_comm`——正是把 u7-l2 的 TP/PP 通信域注入实例的入口。
- [transformer_triton_backend.hpp:310](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/transformer_triton_backend.hpp#L310)：`createSharedWeights` 单列出来，印证「权重与实例分离」。

`AbstractTransformerModelInstance` 在 [transformer_triton_backend.hpp:266-287](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/transformer_triton_backend.hpp#L266-L287) 定义，它提供**两个 `forward` 重载**（[L267-L271](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/transformer_triton_backend.hpp#L267-L271)）：一个吃 `vector<Tensor>`（旧式按下标），一个吃 `unordered_map<string, Tensor>`（新式按名字）。GPT 用新式，BERT 旧式。此外它还内置了流式回调钩子 `registerCallback`（[L273-L277](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/transformer_triton_backend.hpp#L273-L277)），这是 u6-l3 流式生成接入服务层的挂载点。

#### 4.1.4 代码实践

**实践目标**：在源码层面确认「一对子目录 = 一对 Model/Instance」的组织规律。

**操作步骤**：

1. 用 `ls src/fastertransformer/triton_backend/` 列出所有子目录。
2. 对每个子目录，确认其中是否同时存在 `*TritonModel.{h,cc}` 与 `*TritonModelInstance.{h,cc}` 两个文件。
3. 在 [transformer_triton_backend.hpp:290-295](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/transformer_triton_backend.hpp#L290-L295) 中找到每个工厂函数，确认它返回 `shared_ptr<AbstractTransformerModel>`。

**需要观察的现象**：每个模型子目录的文件名都是严格成对的，且工厂函数返回的都是抽象基类指针——这是典型的「针对接口编程」。

**预期结果**：你会看到 7 个子目录（multi_gpu_gpt、multi_gpu_gpt_fp8、bert、t5、t5-encoder、gptj、gptneox），与 6 个工厂函数（T5 拆成 encoder/decoding 两个）一一对应。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `createSharedWeights` 与 `createModelInstance` 要设计成两个独立调用，而不是在 `createModelInstance` 里顺带加载权重？

**答案**：权重加载昂贵且只做一次，而实例可能需要按并发度创建多个。分开后，多个实例可以共享同一份 `shared_weights_`，既省显存又避免重复磁盘 IO；同时也匹配 Triton server「Model 只初始化一次、ModelInstance 可多实例」的生命周期语义。

**练习 2**：`AbstractTransformerModelInstance` 为何提供两个 `forward` 重载？

**答案**：历史演进。早期 BERT/Decoder 用按下标索引的 `vector<Tensor>`；后来 GPT/Decoding 引入大量「可选运行期参数」（top_k、bad_words 等），按下标难以扩展，改用按名字索引的 `unordered_map`。两个重载让新旧模型共存于同一抽象下。

---

### 4.2 triton::Tensor 与 USE_TRITONSERVER_DATATYPE：类型桥接

#### 4.2.1 概念说明

FT 内部有自己的 `ft::DataType` / `ft::MemoryType` / `ft::Tensor`（见 u2-l1）。而 Triton server 用的是 `TRITONSERVER_DataType` / `TRITONSERVER_MemoryType` 以及它自己的张量表示。两套类型系统不能直接混用。

更麻烦的是：FT 的 `triton_backend/` 代码有两种编译场景——

- **编进真正的 tritonserver**：在 `fastertransformer_backend` 仓库里，能拿到 `triton/core/tritonbackend.h` 头文件，应当直接用 `TRITONSERVER_*` 类型。
- **脱离 tritonserver 独立编译**：比如本讲的 `multi_gpu_gpt_triton_example.cc`，并没有链接 tritonserver，这时拿不到那些头文件。

FT 的解法是：定义一个本地的 `triton::Tensor` 结构体，以及 `DataType`/`MemoryType` 两个类型别名，让它们**在两种编译场景下分别指向不同的底层类型**，由宏 `USE_TRITONSERVER_DATATYPE` 切换。这样同一份 `.cc`/`.hpp` 源码两种场景都能编。

#### 4.2.2 核心流程

类型桥接的决策树如下：

```
编译时是否定义了 USE_TRITONSERVER_DATATYPE？
├─ 是（编进 tritonserver）
│     ├─ #include "triton/core/tritonbackend.h" 等
│     ├─ typedef DataType = TRITONSERVER_DataType
│     ├─ TYPE_FP16 = TRITONSERVER_TYPE_FP16, ...
│     └─ 检查 API 版本 ≥ 1.17 才定义 ENABLE_TRITON_BF16
│
└─ 否（独立编译，如 triton_example）
      ├─ typedef DataType = ft::DataType
      ├─ TYPE_FP16 = ft::TYPE_FP16, ...
      └─ TYPE_BF16 = ft::TYPE_BF16（FT 自带，无需版本判断）
```

无论走哪条分支，最终都得到一组名字相同的 `TYPE_FP16`/`TYPE_INT32`/`MEMORY_GPU` 等常量，于是下游代码（`triton::Tensor` 与各 ModelInstance）可以无差别地书写。运行期需要把 triton 张量喂给 FT 内部模型时，再用三个转换函数在两套类型间搬运：

```
triton::Tensor  ──convertTritonTensorToFt()──▶  ft::Tensor
ft::Tensor      ──convertFtTensorToTriton()──▶  triton::Tensor
triton::DataType ──convertTritonTypeToFt()──▶  ft::DataType
```

#### 4.2.3 源码精读

宏切换的核心在 [transformer_triton_backend.hpp:32-99](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/transformer_triton_backend.hpp#L32-L99)：

- [transformer_triton_backend.hpp:32-35](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/transformer_triton_backend.hpp#L32-L35)：定义了 `USE_TRITONSERVER_DATATYPE` 时才引入 tritonserver 头文件。
- [transformer_triton_backend.hpp:45-48](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/transformer_triton_backend.hpp#L45-L48)：只有 tritonserver API 主版本为 1 且次版本 ≥ 17（或主版本 > 1）时，才定义 `ENABLE_TRITON_BF16`——BF16 支持依赖较新版本的 server。
- [transformer_triton_backend.hpp:50-51](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/transformer_triton_backend.hpp#L50-L51)（triton 分支）与 [transformer_triton_backend.hpp:77-78](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/transformer_triton_backend.hpp#L77-L78)（FT 分支）：同一个 `DataType` 别名指向两种底层，是整个桥接的关键。

`triton::Tensor` 结构体定义在 [transformer_triton_backend.hpp:101-110](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/transformer_triton_backend.hpp#L101-L110)，字段与 `ft::Tensor` 几乎一致（`where`/`type`/`shape`/`data`），但它是个**独立的 plain struct**，不依赖 `ft::Tensor` 的方法。三个转换函数：

- [transformer_triton_backend.hpp:112-168](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/transformer_triton_backend.hpp#L112-L168)：`convertTritonTypeToFt`，逐 case 把 `TYPE_*` 映射到 `ft::DataType::TYPE_*`，BF16 分支受 `ENABLE_TRITON_BF16` 守卫。
- [transformer_triton_backend.hpp:170-186](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/transformer_triton_backend.hpp#L170-L186)：`convertTritonTensorToFt`，先转 type 再转 memory type，构造 `ft::Tensor`。
- [transformer_triton_backend.hpp:188-256](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/transformer_triton_backend.hpp#L188-L256)：`convertFtTensorToTriton`，反方向，输出回服务层时用。

CMake 侧的开关在 [CMakeLists.txt:55-56](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L55-L56)（声明 option，默认 OFF）、[CMakeLists.txt:99-101](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L99-L101)（开启时 `add_definitions("-DUSE_TRITONSERVER_DATATYPE")`）、[CMakeLists.txt:289-291](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L289-L291)（开启时把 tritonserver 的 `repo-core-src/include` 加入头文件搜索路径）。

#### 4.2.4 代码实践

**实践目标**：理解「不开 `USE_TRITONSERVER_DATATYPE` 时，`triton::Tensor` 完全不依赖 tritonserver」。

**操作步骤**：

1. 阅读 [transformer_triton_backend.hpp:75-99](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/transformer_triton_backend.hpp#L75-L99) 的 `#else` 分支，确认此时 `DataType` 就是 `ft::DataType`、`TYPE_BF16` 就是 `ft::TYPE_BF16`，整个文件**没有**任何 `#include "triton/..."`。
2. 对照 [CMakeLists.txt:55-56](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L55-L56)，确认 `USE_TRITONSERVER_DATATYPE` 默认 `OFF`。

**需要观察的现象**：`#else` 分支里所有 `TYPE_*` 常量都直接来自 `ft::` 命名空间，不需要任何外部头文件。

**预期结果**：本讲的 `multi_gpu_gpt_triton_example.cc` 在默认编译选项下能脱离 tritonserver 独立编译运行，正是因为走的是 `#else` 分支。

#### 4.2.5 小练习与答案

**练习 1**：为什么 BF16 在 triton 分支需要 `ENABLE_TRITON_BF16` 守卫，而在 FT 分支不需要？

**答案**：FT 自带的 `ft::DataType` 一直包含 `TYPE_BF16`（受 `ENABLE_BF16` 编译宏控制，CUDA≥11 即有）；而 tritonserver 对 BF16 的支持是后来才加的（API 1.17 起），旧版 server 的 `TRITONSERVER_TYPE_BF16` 不存在，必须用版本宏守卫避免编译错误。

**练习 2**：`triton::Tensor` 与 `ft::Tensor` 字段几乎相同，为什么不直接复用 `ft::Tensor`？

**答案**：为了解耦。`triton::Tensor` 是面向服务层的「协议张量」，其 `DataType` 在编进 server 时是 `TRITONSERVER_DataType`；`ft::Tensor` 是面向计算内核的内部张量，`DataType` 恒为 `ft::DataType`。两者类型不同不能合体，靠显式转换函数 `convertTritonTensorToFt` / `convertFtTensorToTriton` 在边界上搬运，让「服务协议」与「计算实现」各自独立演进。

---

### 4.3 ParallelGptTritonModel：Model 层（加载权重与构造参数）

#### 4.3.1 概念说明

`ParallelGptTritonModel<T>` 是 `AbstractTransformerModel` 的具体实现，承担 Model 层的全部职责：

1. **解析配置**：从一个 INI 文件读出模型结构（head_num、num_layer、vocab_size 等）与并行配置（TP/PP）。
2. **持有共享权重**：用一个 `vector<shared_ptr<ParallelGptWeight<T>>>` 按 `device_id` 索引，每张卡一份权重指针。
3. **建立通信域**：实现 `createNcclParams`（继承自基类）与 `createCustomComms`。
4. **派生实例**：`createModelInstance` 把 FT 内核对象 `ft::ParallelGpt` 装配好，包成 `ParallelGptTritonModelInstance`。

它的关键设计有两点。其一，**权重按 rank 切分加载**：每个 GPU 只加载属于自己的那一片权重（TP 切头、PP 切层，见 u2-l5），由 `tensor_para_rank`/`pipeline_para_rank` 决定。其二，**实例构造时 `max_batch_size`/`max_seq_len`/`max_input_len` 全传 0**——FT 会按运行期请求自动调整 buffer，因此 Model 层不需要预知最大 batch。

#### 4.3.2 核心流程

Model 层从配置到可用实例的流程：

```
createGptModel(inifile)                      # 静态工厂
  ├─ INIReader(inifile)                      # 读 ft_instance_hyperparameter + model_name 两段
  ├─ 解析 model_variant（opt-pre/bloom-pre 等）→ gpt_variant_params
  ├─ 按 data_type 字符串分发 → make_shared<ParallelGptTritonModel<half/bf16/float>>
  │       └─ 构造函数：把所有超参逐个 reader.Get 存进成员
  │
createSharedWeights(device_id, rank)         # 每卡调一次
  ├─ tensor_para_rank   = rank % tensor_para_size_
  ├─ pipeline_para_rank = rank / tensor_para_size_
  ├─ make_shared<ParallelGptWeight<T>>(... tensor_para_rank, pipeline_para_rank ...)
  └─ weight->loadModel(model_dir_)          # 从 .bin 文件加载本 rank 的切片
  │
createModelInstance(device_id, rank, stream, nccl, custom_comm)
  ├─ cudaSetDevice(device_id)
  ├─ 建 Allocator<CUDA>、cublas handle/wrapper、cudaDeviceProp
  ├─ 从 nccl_params 取出本 rank 的 tensor_para / pipeline_para
  ├─ attention_type = getAttentionType(...)  # context 阶段 fused/unfused 决策
  ├─ make_unique<ft::ParallelGpt<T>>(0, 0, 0, ... tensor_para, pipeline_para ...)
  └─ 包成 ParallelGptTritonModelInstance，把 shared_weights_[device_id] 塞进去
```

`createNcclParams`（在基类 [transformer_triton_backend.cpp:19-76](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/transformer_triton_backend.cpp#L19-L76) 实现）负责把 TP/PP 两个 NCCL communicator 建起来：先用 `ftNcclGetUniqueId` 生成 `tensor_para_size + pipeline_para_size` 个唯一 ID，用 `mpi::bcast` 广播到所有节点，再在 `ftNcclGroupStart/GroupEnd` 之间为每张卡调 `ftNcclCommInitRank`。其中 [transformer_triton_backend.cpp:48-50](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/transformer_triton_backend.cpp#L48-L50) 的 `tensor_para_rank = rank % tensor_para_size`、`pipeline_para_rank = rank / tensor_para_size` 正是 u7-l2 的二维拓扑公式。

#### 4.3.3 源码精读

**工厂函数 `createGptModel`** 在 [ParallelGptTritonModel.cc:27-165](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModel.cc#L27-L165)：

- [ParallelGptTritonModel.cc:35-36](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModel.cc#L35-L36)：从 `ft_instance_hyperparameter` 段读 `model_name` 与 `data_type`，这两个是后续分发的依据。
- [ParallelGptTritonModel.cc:40-71](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModel.cc#L40-L71)：解析 `model_variant`（`opt-pre`/`opt-post`/`bloom-pre`/`bloom-post`），填入 `gpt_variant_params`，让 OPT、BLOOM 这些 GPT 变体复用同一套 ParallelGpt 骨架（承接 u6-l4）。
- [ParallelGptTritonModel.cc:96-164](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModel.cc#L96-L164)：按 `data_type` 字符串（`fp16`/`bf16`/`fp32`）`if/else` 分发到不同模板实例，BF16 分支受 `#ifdef ENABLE_BF16` 守卫——这是 u1-l4 提到的「枚举/字符串→模板」dispatch 套路在服务层的再次出现。

**构造函数读 config.ini** 在 [ParallelGptTritonModel.cc:168-262](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModel.cc#L168-L262)：另一个构造函数从 `model_dir + "/config.ini"` 读 `[gpt]` 段（[L180](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModel.cc#L180)），把 `head_num`/`size_per_head`/`inter_size`/`num_layer`/`vocab_size` 等存为成员（[L217-L222](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModel.cc#L217-L222)）。注意它用的是 `reader.Get("gpt", ...)`，而工厂函数读的是 `ft_instance_hyperparameter` 段——**两份 INI**：一份描述运行实例（数据类型、并行度、模型路径），一份描述模型结构本身。

**`createSharedWeights`** 在 [ParallelGptTritonModel.cc:408-429](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModel.cc#L408-L429)：

- [ParallelGptTritonModel.cc:411-412](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModel.cc#L411-L412)：由 `rank` 算出 TP/PP rank，与 u2-l5 的权重切分编码完全对齐。
- [ParallelGptTritonModel.cc:414-427](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModel.cc#L414-L427)：构造 `ParallelGptWeight<T>` 并 `loadModel(model_dir_)`，把该 rank 的权重切片从 `.bin` 载入显存，存进 `shared_weights_[device_id]`。

**`createModelInstance`** 在 [ParallelGptTritonModel.cc:306-405](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModel.cc#L306-L405)：

- [ParallelGptTritonModel.cc:316-331](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModel.cc#L316-L331)：建 `Allocator<CUDA>`、`cublasHandle`/`cublasLtHandle`、`cublasMMWrapper`，这与 u1-l4 的「建资源」步骤同构。
- [ParallelGptTritonModel.cc:348-349](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModel.cc#L348-L349)：从 `nccl_params.first[comms_rank]` / `.second[comms_rank]` 取出本卡的 TP/PP 通信域，`comms_rank = device_id % (TP*PP)`（[L314](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModel.cc#L314)）。
- [ParallelGptTritonModel.cc:351-357](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModel.cc#L351-L357)：用 `getAttentionType` 决定 context 阶段走 fused 还是 unfused 注意力（承接 u3-l3、u6-l1）。
- [ParallelGptTritonModel.cc:359-395](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModel.cc#L359-L395)：构造 `ft::ParallelGpt<T>`，注意前三个参数 `max_batch_size`/`max_seq_len`/`max_input_len` 全是 `0`（[L360-L362](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModel.cc#L360-L362)），注释写明 "FT will adjust the buffer automatically"。
- [ParallelGptTritonModel.cc:397-404](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModel.cc#L397-L404)：把 `gpt`、`shared_weights_[device_id]` 以及一众 `unique_ptr` 资源一并 move 进 `ParallelGptTritonModelInstance`——所有权彻底移交实例。

#### 4.3.4 代码实践

**实践目标**：对照 BERT 的 Model 层，确认「Model 层是同一套模板套不同模型」。

**操作步骤**：

1. 打开 [BertTritonModel.cc:25-74](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/bert/BertTritonModel.cc#L25-L74)，与 GPT 的构造函数对比。
2. 注意 [BertTritonModel.cc:41-42](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/bert/BertTritonModel.cc#L41-L42) 的断言：`int8_mode_ == 0` 与 `is_sparse == false`，说明 BERT 的 triton backend 当前不支持 INT8/稀疏。
3. 对照 [BertTritonModel.cc:169-184](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/bert/BertTritonModel.cc#L169-L184) 的 `createSharedWeights`，确认它和 GPT 一样按 `rank % TP` / `rank / TP` 切分。

**需要观察的现象**：BERT 与 GPT 的 Model 层方法签名、调用顺序几乎完全一致，差别只在「构造哪个 `ft::模型` 与哪个 `Weight`」。

**预期结果**：你会得出结论——新增一个模型的 triton backend，Model 层基本是「复制 GPT 的、把 `ParallelGpt` 换成新模型类」。

#### 4.3.5 小练习与答案

**练习 1**：`createModelInstance` 里 `max_batch_size`/`max_seq_len`/`max_input_len` 为什么传 0？

**答案**：因为 Model 层在构造时还不知道运行期请求的 batch 和序列长度。FT 的 buffer 管理基于带账本的 `IAllocator`（u2-l2、u3-l5），会在首次 `forward` 时按实际请求尺寸自动 `reMalloc`，后续命中 REUSE。传 0 表示「不预设上限，按需自适应」。

**练习 2**：`shared_weights_` 为什么用 `vector` 按 `device_id` 索引，而不是按 `rank`？

**答案**：因为同一节点上可能有多个 instance（比如 Triton 在一张卡上开多实例做并发），它们共享同一份权重。`device_id` 是节点内 GPU 编号，`shared_weights_[device_id]` 保证「同卡一份权重」；而 `rank` 是全局编号，跨节点不连续，不适合直接做节点内 vector 下标。

---

### 4.4 ParallelGptTritonModelInstance：Instance 层（单次请求 forward）

#### 4.4.1 概念说明

`ParallelGptTritonModelInstance<T>` 是 `AbstractTransformerModelInstance` 的具体实现，承担请求级的全部职责：

1. **输入转换**：把服务层送来的 `triton::Tensor` 映射表转成 FT 内部的 `ft::Tensor` 映射表，必要时把 CPU 张量搬到 GPU（`move_tensor_H2D`）。
2. **输出 buffer 管理**：用 `IAllocator::reMalloc` 申请 `output_ids`、`sequence_length`、`output_log_probs` 等输出张量，并在析构时 `freeBuffer`。
3. **调用模型**：组装好输入输出 `unordered_map`，调用 `gpt_->forward(&output_tensors, &ft_input_tensors, gpt_weight_.get())`。
4. **输出转换**：把 FT 的 `ft::Tensor` 输出转回 `triton::Tensor`。
5. **流式回调**：若上层注册了 `stream_cb_`，则把 GPT 的逐 token 输出通过回调吐出去（u6-l3 流式生成的接入点）。

它和 Model 层的分工边界很清晰：**Model 负责「把模型装好、权重就位」，Instance 负责「吃一次请求、吐一次结果」**。Model 一生只建一次，Instance 每次 `forward` 都被调用。

#### 4.4.2 核心流程

一次 `forward` 的内部流程：

```
forward(input_tensors)   # input_tensors: unordered_map<string, triton::Tensor>
  ├─ 校验 input_ids shape == 2、input_lengths shape == 1
  ├─ request_batch_size = input_ids.shape[0]
  ├─ beam_width = input_tensors["beam_width"] 或默认 1（校验合法值）
  ├─ total_length = max_request_output_len + input_ids.shape[1]
  │
  ├─ convert_inputs(input_tensors)              # triton→ft 转换
  │     ├─ move_tensor_H2D(input_ids / input_lengths)
  │     ├─ 计算 h_total_output_lengths_[]（含交互式续写的步数）
  │     ├─ 组装 ft_input_tensors{input_ids, input_lengths, output_seq_len, ...}
  │     └─ 透传其余未处理张量（top_p_decay 等）via convertTritonTensorToFt
  │
  ├─ 处理交互式模式：START / session_len / continue_gen
  ├─ allocateBuffer(batch, beam, total_len, out_len)   # reMalloc 输出 buffer
  │
  ├─ 组装 output_tensors{output_ids, sequence_length, is_finished, ...}
  │     └─ 按需插入 output_log_probs / cum_log_probs / context_embeddings
  │
  ├─ try:
  │     if (stream_cb_) gpt_->registerCallback(triton_stream_callback, this)
  │     gpt_->forward(&output_tensors, &ft_input_tensors, gpt_weight_.get())
  │     if (stream_cb_) gpt_->unRegisterCallback()
  │   catch (...): 把异常指针塞进 output_tensors["error_message"]
  │
  └─ return convert_outputs(output_tensors)    # ft→triton 转换
```

#### 4.4.3 源码精读

**`convert_inputs`** 在 [ParallelGptTritonModelInstance.cc:64-145](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModelInstance.cc#L64-L145)：

- [ParallelGptTritonModelInstance.cc:69-70](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModelInstance.cc#L69-L70)：`move_tensor_H2D` 把 `input_ids`/`input_lengths` 从 CPU 搬到 GPU（若已在 GPU 则直接返回，见 [triton_utils.hpp:29-31](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/triton_utils.hpp#L29-L31)）。
- [ParallelGptTritonModelInstance.cc:81-89](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModelInstance.cc#L81-L89)：组装 `ft_input_tensors`，注意 `input_lengths_h` 用 `as_CPU_tensor`（保留 CPU 副本供 host 端读长度），`input_ids`/`input_lengths` 用 `as_GPU_tensor`（指向 device buffer）——同一份输入同时备 CPU/GPU 两个视图。
- [ParallelGptTritonModelInstance.cc:133-142](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModelInstance.cc#L133-L142)：把前面没有专门处理的张量（如 `top_p_decay`、`bad_words_list` 之外的运行期参数）统一用 `convertTritonTensorToFt` 透传，保证新参数无需改 Instance 代码就能透传到模型。

**`forward`（map 重载）** 在 [ParallelGptTritonModelInstance.cc:161-256](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModelInstance.cc#L161-L256)：

- [ParallelGptTritonModelInstance.cc:169-181](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModelInstance.cc#L169-L181)：取 `request_batch_size`、`beam_width`，并把 `beam_width` 校验到合法集合 `{1,2,3,4,8,16,32}`，否则降级为 1（sampling）。
- [ParallelGptTritonModelInstance.cc:185-199](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModelInstance.cc#L185-L199)：交互式生成分支——若 `START==0` 表示续写轮，插入 `continue_gen` 并把 `total_length` 加上已有步数 `gpt_->getStep()`；若 `START==1` 表示新会话，读 `session_len` 预分配 cache（承接 u6-l3）。
- [ParallelGptTritonModelInstance.cc:201-238](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModelInstance.cc#L201-L238)：组装 `output_tensors`，`output_ids` 形状为 `[batch, beam, total_length]`，并按 `is_return_log_probs`/`is_return_context_embeddings` 两个开关**按需**插入可选输出张量。
- [ParallelGptTritonModelInstance.cc:240-250](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModelInstance.cc#L240-L250)：核心调用——`gpt_->forward(&output_tensors, &ft_input_tensors, gpt_weight_.get())`（[L245](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModelInstance.cc#L245)）。注意它被 `try/catch` 包住，异常被存进 `h_exception_` 并以 `error_message` 张量形式回传，而不是抛穿服务层——避免一次坏请求把整个 server 拖崩。

**`allocateBuffer` / `freeBuffer`** 在 [ParallelGptTritonModelInstance.cc:264-297](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModelInstance.cc#L264-L297)：每个输出指针都用 `allocator_->reMalloc(ptr, bytes, false)` 申请（`false` 表示 `ReallocType`，命中 REUSE 时不真正 malloc，承接 u2-l2/u3-l5），析构时 `freeBuffer` 逐个 `allocator_->free`。

**流式回调** 在 [ParallelGptTritonModelInstance.cc:29-35](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModelInstance.cc#L29-L35)：`triton_stream_callback` 把 GPT 内部每步产出的 `ft::Tensor` 输出经 `convert_outputs` 转成 `triton::Tensor`，再调 `model->stream_cb_`——这正是 u6-l3 中 `GptStreamer` 在服务侧的对接点。

> 提示：另一个 `forward(vector<Tensor>)` 重载在 [ParallelGptTritonModelInstance.cc:57-62](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModelInstance.cc#L57-L62) 直接 `FT_CHECK(false)`，GPT 只支持 map 形式接口。

#### 4.4.4 代码实践

**实践目标**：跟踪一次请求从 `triton::Tensor` 到 `gpt_->forward` 的完整数据流。

**操作步骤**：

1. 从 [ParallelGptTritonModelInstance.cc:161](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModelInstance.cc#L161) 的 `forward` 入口开始读。
2. 跟到 [L183](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModelInstance.cc#L183) 的 `convert_inputs`，确认 `input_ids` 经 `move_tensor_H2D` 后由 `as_GPU_tensor` 包成 `ft::Tensor{MEMORY_GPU, ...}`。
3. 跟到 [L245](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModelInstance.cc#L245)，确认 `gpt_->forward` 的三个参数分别是输出 map、输入 map、权重指针。

**需要观察的现象**：输入张量在 `convert_inputs` 里完成了「triton→ft + CPU→GPU」双重转换；输出张量是 Instance 自己 `reMalloc` 出来的 device buffer，forward 后由 `convert_outputs` 转 triton。

**预期结果**：你会看到一次 forward 经历 `triton输入 → ft输入(含H2D) → gpt_->forward → ft输出 → triton输出` 的完整往返，且整个过程零拷贝地复用了 Instance 持有的 device buffer。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `forward` 要用 `try/catch` 捕获异常并塞进 `error_message` 张量，而不是让异常直接抛出？

**答案**：在服务场景下，一个请求出错（如输入 shape 非法、beam_width 非法）不应导致整个 server 进程崩溃，也不应影响后续请求。把异常捕获并以普通输出张量回传，让上层（Triton）能把这个请求标记为失败、把错误信息返回客户端，同时保持 server 与其他 ModelInstance 继续可用。

**练习 2**：`convert_inputs` 里 `input_lengths` 既放了 GPU 版（`input_lengths`）又放了 CPU 版（`input_lengths_h`），为什么？

**答案**：GPU 版供 kernel 在 device 上读取长度；CPU 版供 host 端逻辑（如计算 `h_total_output_lengths_`、early stopping 判定等需要立即拿到数值的场景）同步读取，避免每次都做 D2H 拷贝。FT 的 Tensor 是非拥有描述符，同时挂两个视图零成本（u2-l1）。

---

### 4.5 triton_example 部署演示：线程 intra-node + MPI inter-node

#### 4.5.1 概念说明

`multi_gpu_gpt_triton_example.cc` 是 FT 自带的一个「不依赖真正 tritonserver」的部署演示：它直接调用 `AbstractTransformerModel` 这套 API，把「服务一个 GPT 模型」的完整 6 步流程跑通。docs/gpt_guide.md 第 [109-123](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md#L109-L123) 行列出了这 6 步：

1. 初始化 NCCL 并设定 TP/PP rank（由 MPI 或线程）。
2. 按 rank 加载权重。
3. 按 rank 构造 `ParallelGpt` 实例。
4. 接收请求并转成 ParallelGpt 的输入张量。
5. forward。
6. 把输出转回响应并返回。

本示例相比 `multi_gpu_gpt_example.cc`（用 MPI 组织一切）的关键差异在于**并行组织方式**：

- **节点内（intra-node）用 `std::thread`**：同一台机器上的多张 GPU 各开一个线程，分别 `cudaSetDevice` 后并发地建实例、跑 forward。线程间共享进程地址空间，NCCL communicator 可以用 `ftNcclGroupStart/GroupEnd` 一次性并发建立，开销极低。
- **节点间（inter-node）用 MPI**：跨机器的进程协调（广播请求、barrier 同步、计时对齐）走 MPI，因为不同机器本来就是不同进程。

这种「线程管节点内、MPI 管节点间」的混合编排，正是 docs 里强调的 *"It uses threading for intra node, and MPI for inter node"*（[gpt_guide.md:112](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md#L112)）。

#### 4.5.2 核心流程

`main` 的 6 步对应代码：

```
main(argc, argv)
  ── step 0: mpi::initialize → node_id, node_num; gpu_count = getDeviceCount()
             world_size = node_num * gpu_count
  ── step 1: model = createGptModel(ini)
             校验 world_size == TP * PP
  ── step 2: nccl_comms = model->createNcclParams(node_id)     # MPI 广播 nccl_id
             model->createCustomComms(world_size)              # 可选
  ── step 3: for device_id in [0, gpu_count):
               thread(threadCreateModelInstances, model, device_id, rank=node_id*gpu_count+device_id, ...)
             join 全部线程
                 └─ 每个线程内：createSharedWeights + createModelInstance
  ── step 4: request_list = prepareRequest(ini, node_id, gpu_count)
                 └─ broadcastRequest: MPI bcast 输入到所有节点，每节点复制 gpu_count 份到各卡
  ── step 5: for round in [0,2):   # 跑两轮（warmup + 计时）
               for device_id: thread(threadForward, instance, request)
               join 全部线程
                 └─ 每个线程内：instance->forward(request)
  ── step 6: node 0 写输出文件；mpi::barrier + cudaDeviceSynchronize 后计时；mpi::finalize
```

关键点：**第 3 步和第 5 步都是「一个 GPU 一个线程」**。每个线程第一行就是 `cudaSetDevice(device_id)`（[threadCreateModelInstances:285](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_triton_example.cc#L285)、[threadForward:301](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_triton_example.cc#L301)），把当前线程「绑」到一张卡上，之后该线程所有 CUDA/cuBLAS/NCCL 调用都默认落在那张卡——这是多线程多 GPU 编程的标准范式。

#### 4.5.3 源码精读

**入口与初始化** 在 [multi_gpu_gpt_triton_example.cc:306-335](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_triton_example.cc#L306-L335)：

- [multi_gpu_gpt_triton_example.cc:313-319](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_triton_example.cc#L313-L319)：`mpi::initialize` 拿 `node_id`/`node_num`，`world_size = node_num * gpu_count`——注意是「节点数 × 每节点 GPU 数」，这正是 inter-node 用 MPI、intra-node 用线程的体现。
- [multi_gpu_gpt_triton_example.cc:326-330](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_triton_example.cc#L326-L330)：校验 `world_size == TP * PP`，即 u7-l2 的硬约束。
- [multi_gpu_gpt_triton_example.cc:335](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_triton_example.cc#L335)：`createNcclParams(node_id)` 建通信域，其内部 [transformer_triton_backend.cpp:32-39](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/transformer_triton_backend.cpp#L32-L39) 用 `mpi::bcast` 把 NCCL unique id 广播到所有节点——这是 MPI 在此处唯一参与的实质通信。

**线程化建实例** 在 [multi_gpu_gpt_triton_example.cc:343-360](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_triton_example.cc#L343-L360)：

- [multi_gpu_gpt_triton_example.cc:348-357](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_triton_example.cc#L348-L357)：循环里 `rank = node_id * gpu_count + device_id`（[L349](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_triton_example.cc#L349)），把每张卡映射成全局 rank，再 `std::thread(threadCreateModelInstances, ...)`。
- 线程函数 [multi_gpu_gpt_triton_example.cc:277-294](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_triton_example.cc#L277-L294)：`cudaSetDevice` → `cudaStreamCreate` → `createSharedWeights` → `createModelInstance`，把实例塞进 `model_instances[device_id]`。
- [multi_gpu_gpt_triton_example.cc:358-360](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_triton_example.cc#L358-L360)：`join` 所有线程——建实例阶段是 barrier，必须全部完成才能进入 forward。

**请求广播** 在 [multi_gpu_gpt_triton_example.cc:31-210](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_triton_example.cc#L31-L210)：`broadcastRequest` 用 `mpi::bcast` 把 node 0 的输入 ids/lengths 广播到所有节点（[L55-56](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_triton_example.cc#L55-L56)、[L70-71](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_triton_example.cc#L70-L71)），然后**在本节点内为每张 GPU 各 `deviceMalloc`+`cudaH2Dcpy` 一份输入**（[L74-92](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_triton_example.cc#L74-L92)），组装成 `gpu_count` 个独立的请求 map（[L99-115](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_triton_example.cc#L99-L115)）。这是「MPI 广播一次、线程各取一份」的混合模式。

**线程化 forward** 在 [multi_gpu_gpt_triton_example.cc:371-383](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_triton_example.cc#L371-L383)：同样每卡一线程，调 `threadForward`（[L296-304](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_triton_example.cc#L296-L304)），其内部 `(*model_instance)->forward(request)`。外层循环 `for (i=0; i<2; i++)` 跑两轮，第一轮当 warmup，第二轮配合 [L430-450](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_triton_example.cc#L430-L450) 的 `mpi::barrier`+`cudaDeviceSynchronize` 做计时。

**收尾** 在 [multi_gpu_gpt_triton_example.cc:461](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_triton_example.cc#L461)：`mpi::finalize`，对应开头的 `initialize`。

#### 4.5.4 代码实践

**实践目标**：确认「线程负责 intra-node、MPI 负责 inter-node」的分工边界。

**操作步骤**：

1. 在 [multi_gpu_gpt_triton_example.cc:313](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_triton_example.cc#L313) 处找到 `mpi::initialize`，确认其后所有 `mpi::` 调用都只做「跨节点」的事：`bcast`（广播请求/nccl_id）、`barrier`（同步计时）、`finalize`。
2. 在 [multi_gpu_gpt_triton_example.cc:348](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_triton_example.cc#L348) 处找到 `std::thread` 循环，确认线程内只做「本节点某张卡」的事：`cudaSetDevice`、`createSharedWeights`、`createModelInstance`、`forward`。
3. 对照 [multi_gpu_gpt_triton_example.cc:349](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_triton_example.cc#L349) 的 `rank = node_id * gpu_count + device_id`，理解全局 rank 如何由「节点号 × 节点内 GPU 数 + 卡号」拼出。

**需要观察的现象**：MPI 调用的次数很少且都跨节点；线程调用的次数等于 `gpu_count` 且都绑定到具体卡。两套机制职责互不重叠。

**预期结果**：你能画出一张表——「建 NCCL id 广播：MPI」「建实例：线程」「输入广播：MPI」「各卡拷输入：线程（各卡独立 deviceMalloc）」「forward：线程」「计时同步：MPI」，清晰体现混合编排。

> 待本地验证：实际运行需多 GPU 环境与编译好的 `bin/multi_gpu_gpt_triton_example`，配合 `examples/cpp/multi_gpu_gpt/gpt_config.ini` 和 `start_ids.csv`。若无 GPU，本实践以源码阅读为主。

#### 4.5.5 小练习与答案

**练习 1**：为什么节点内用线程而不是也用 MPI（每张卡一个 MPI rank）？

**答案**：线程共享进程地址空间，NCCL communicator 可用 `ftNcclGroupStart/GroupEnd` 并发建立，权重指针、配置对象可直接共享（`shared_weights_`），通信与同步开销远低于 MPI；且线程创建比 MPI 进程轻得多。节点间本来就是不同进程，只能用 MPI。所以「线程管节点内、MPI 管节点间」是性能与可行性的折中最优。

**练习 2**：`broadcastRequest` 为什么要在本节点为每张 GPU 各复制一份输入，而不是所有 GPU 共用一份？

**答案**：因为每张 GPU 跑在独立线程、独立 stream 上，`forward` 是异步的。若共用一份 device 输入，多线程并发读写同一块显存会引发竞争。各卡各持一份输入（虽多占一点显存）换来线程间完全无干扰的并发执行，是 correctness 优先的设计。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来，写一份《FT Triton backend 请求处理全景图》说明文档。

具体要求：

1. **分层意义**：对照 [ParallelGptTritonModel.cc:408-429](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModel.cc#L408-L429)（`createSharedWeights`）与 [ParallelGptTritonModelInstance.cc:161-256](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend/multi_gpu_gpt/ParallelGptTritonModelInstance.cc#L161-L256)（`forward`），用一段话说明：Model 负责「加载权重 / 构造参数 / 建通信域」这类一次性、昂贵的静态资源管理，ModelInstance 负责「单次请求 forward」这类高频、轻量的动态计算。指出这种分层带来的两个好处——权重可被多实例共享、坏请求异常可被实例层捕获而不影响 Model。

2. **类型桥接**：在图中标出请求经过 `triton::Tensor → convert_inputs → ft::Tensor → gpt_->forward → ft::Tensor → convert_outputs → triton::Tensor` 的往返路径，并注明 `USE_TRITONSERVER_DATATYPE` 在哪两种编译场景下分别把 `DataType` 指向谁。

3. **并行编排**：对照 [multi_gpu_gpt_triton_example.cc:313-360](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_triton_example.cc#L313-L360) 与 [L371-383](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_triton_example.cc#L371-L383)，用表格列出 6 步流程中每一步分别由「线程」还是「MPI」负责，并解释 `rank = node_id * gpu_count + device_id` 如何把两套机制缝合。

4. **输出**：一张图（可用文字描述的流程图）+ 一张表（线程/MPI 分工表）+ 三段说明。

**预期结果**：你能用自己的话向他人讲清「一个请求从进入 triton server 到拿到 GPT 输出，中间经过了哪些层、哪些类型转换、哪些并行机制」。

## 6. 本讲小结

- FT 用 `AbstractTransformerModel` / `AbstractTransformerModelInstance` 两层抽象把「模型级静态资源管理」与「请求级动态 forward」分离，与 Triton server 自身的 `TritonModel` / `TritonModelInstance` 分层一一对应。
- `triton::Tensor` 与 `USE_TRITONSERVER_DATATYPE` 宏让同一份 backend 代码既能编进真正的 tritonserver（用 `TRITONSERVER_DataType`），也能脱离 server 独立编译（退化为 `ft::DataType`），靠三个 convert 函数在两套类型间桥接。
- `ParallelGptTritonModel`（Model 层）负责读 INI 配置、按 TP/PP rank 切分加载权重、建 NCCL 通信域、派生实例；权重按 `device_id` 索引存于 `shared_weights_` 供多实例共享。
- `ParallelGptTritonModelInstance`（Instance 层）负责 `triton↔ft` 张量转换、H2D 搬运、用 `IAllocator::reMalloc` 管理输出 buffer、调用 `gpt_->forward`、异常捕获与流式回调。
- `triton_backend/` 按模型分子目录，每目录一对 `XxxTritonModel` + `XxxTritonModelInstance`，覆盖 multi_gpu_gpt / bert / t5 / t5-encoder / gptj / gptneox / multi_gpu_gpt_fp8，新增模型基本是「复制 GPT 的、换底层模型类」。
- `multi_gpu_gpt_triton_example.cc` 用「线程做 intra-node、MPI 做 inter-node」的混合编排跑通 6 步部署流程，`rank = node_id * gpu_count + device_id` 把两套机制缝合为全局 rank。

## 7. 下一步学习建议

- **真正接入 tritonserver**：本讲只覆盖 FT 侧的 `AbstractTransformerModel` API。要把它装进真正的 Triton server，需阅读 [fastertransformer_backend](https://github.com/triton-inference-server/fastertransformer_backend) 仓库，看它如何把这两个抽象类的方法接到 `TRITONBACKEND_ModelInstanceExecute` 等 C API 上。
- **TensorRT plugin 路线**：对照本讲，下一讲 u10-l4 讲解 `tensorrt_plugin/`——另一条「把 FT 装进推理框架」的路径，区别在于 TRT 把模型编译进 engine、FT 作为 plugin 节点，而非像 Triton 这样作为独立 backend。
- **流式生成闭环**：本讲提到 `stream_cb_` 是 u6-l3 流式生成在服务侧的接入点，建议回看 u6-l3 的 `GptStreamer`，理解「`std::async` 跑 forward + 主线程轮询 sequence_length + streamHook」如何与本讲的回调对接。
- **深入权重切分**：若对 Model 层 `createSharedWeights` 的 TP/PP 切分细节感兴趣，回看 u2-l5（DenseWeight/BaseWeight）与 u7-l1/u7-l2（张量/流水并行）。
