# 源码目录结构与代码规范

## 1. 本讲目标

上一篇（u1-l1）我们已经知道 FasterTransformer（以下简称 FT）是一个用 CUDA/cuBLAS 写成的 Transformer 推理加速库，并通过「支持矩阵」了解了它的能力边界。但当你真正打开仓库，会被几千个文件淹没：`kernels`、`layers`、`models`、`utils`、`th_op`、`tf_op`、`triton_backend`、`tensorrt_plugin`、`cutlass_extensions`……这些目录到底是什么关系？

本讲学完后，你应当能够：

1. 说出 `src/fastertransformer` 下每个一级子目录的职责，并能从仓库里为每个目录挑出代表性文件。
2. 区分 **kernel / layer / model** 这三层抽象，理解 FT 代码是如何「自底向上」堆叠出整个模型的。
3. 看懂 FT 的文件名、函数名、变量名命名约定，并在阅读源码时据此快速判断一个文件属于哪一层。
4. 知道新增一个模型时该把代码放在哪里、哪些组件可以直接复用。

本讲是「阅读源码前的地图」，不涉及任何 CUDA 实现细节，目标只是让你**不再迷路**。

## 2. 前置知识

阅读本讲前，你需要具备：

- **Transformer 的基本结构概念**：知道一个 Transformer 模型由若干个「block/layer」堆叠而成，每个 block 里大致有 self-attention 和前馈网络（FFN）两大部分。不需要懂数学推导，只要知道这些名词指代「模型的一个子部件」即可。
- **C++ 头文件与源文件的关系**：`.h`/`.hpp`/`.cuh` 放声明，`.cc`/`.cu` 放实现。FT 的 C++ 源文件用 `.cc` 而不是 `.cpp`，CUDA 源文件用 `.cu`，CUDA 头文件用 `.cuh`。
- **「目录即模块」的工程直觉**：大型 C++ 项目通常用目录来划分逻辑边界，本讲会反复用到这一思路。
- 已完成 u1-l1，了解 FT 的定位与支持矩阵。

几个本讲会用到的术语：

| 术语 | 含义 |
| --- | --- |
| kernel | 在 GPU 上并行执行的一个函数（CUDA 术语），是 FT 性能优化的最小单位。 |
| layer | 「层」，把若干 kernel + 矩阵乘（GEMM）组合成一个可复用的模块，如 attention 层、FFN 层。 |
| model | 「模型」，把若干 layer 串成完整的前向流程，如 BERT、GPT。 |
| GEMM | 通用矩阵乘（GEneral Matrix-Matrix multiplication），FT 里几乎都用 cuBLAS/cuBLASLt 完成。 |
| op | 框架里的「算子」，PyTorch/TensorFlow 把 C++ 模型包装成一个 op 供 Python 调用。 |

## 3. 本讲源码地图

本讲主要围绕两个「说明性文件」展开，它们官方地描述了目录组织与代码规范：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/README.md) | 顶层 README，其 *Advanced* 章节给出了一整段官方目录结构树。 |
| [templates/adding_a_new_model/README.md](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/templates/adding_a_new_model/README.md) | 贡献者指南，规定了新增模型的 7 步流程、命名规范与「不要改老模型」的原则。 |

为了把抽象的目录讲实，本讲还会引用每个目录下的一个**代表性源码文件**作为佐证（这些文件本身会在后续讲义深入讲解，本讲只用它们说明「这一层长什么样」）：

| 目录 | 代表性文件 |
| --- | --- |
| `kernels/` | `add_residual_kernels.h`（残差相加 kernel） |
| `layers/` | `BaseLayer.h`（所有层的抽象基类） |
| `models/` | `bert/Bert.h`（BERT 模型类） |
| `utils/` | `Tensor.h`（贯穿全库的张量抽象） |
| `th_op/` | `th_utils.h`（PyTorch 张量桥接工具） |

## 4. 核心概念与源码讲解

### 4.1 顶层目录与 `src/fastertransformer` 的职责划分

#### 4.1.1 概念说明

FT 仓库的顶层目录其实只有几个，每个都对应一种「使用方式」：

- `src/fastertransformer/`：**核心库代码**。所有模型、算子、工具都在这里，无论你用 C++、PyTorch 还是 Triton，最终调用的都是这里的代码。
- `examples/`：**示例代码**，按语言再分 `cpp/`、`pytorch/`、`tensorflow/`、`tensorrt/`，教你怎么调用核心库。
- `docs/`：每个模型的详细说明（`xxx_guide.md`）和常见问题 `QAList.md`。
- `benchmark/`：跑性能基准的脚本。
- `tests/`：单元测试。
- `templates/`：教你怎么给 FT 贡献新模型/新 kernel。

核心库 `src/fastertransformer/` 内部又按「职责」切成 9 个子目录。理解它们的关键是认清 **「框架外壳」与「核心引擎」的分离**：

- **核心引擎**（与框架无关，纯 C++/CUDA）：`kernels/`、`layers/`、`models/`、`utils/`、`cutlass_extensions/`。
- **框架外壳**（把引擎包装成各框架能调用的形态）：`th_op/`（PyTorch）、`tf_op/`（TensorFlow）、`triton_backend/`（Triton 推理服务器）、`tensorrt_plugin/`（TensorRT 插件）。

这种分离正是 u1-l1 里那句「所有模型在 C++ 下都可用，TF/PyTorch/Triton/TensorRT 只是封装外壳」的代码体现。

#### 4.1.2 核心流程

读 FT 源码时，建议按下面的「自底向上」顺序建立心理模型：

```
                ┌─────────────────────────────────────────┐
  框架层(外壳):  │ th_op  │ tf_op │ triton_backend │ trt   │   ← 接收框架张量，转成内部 Tensor
                └─────────────────────────────────────────┘
                                   │ 调用
                                   ▼
                ┌─────────────────────────────────────────┐
  模型层:        │            models/<model>              │   ← 串起整条前向流程
                └─────────────────────────────────────────┘
                                   │ 调用
                                   ▼
                ┌─────────────────────────────────────────┐
  层(layer):     │  layers/ (attention/ffn/beamsearch...) │   ← 组合 kernel + GEMM
                └─────────────────────────────────────────┘
                                   │ 调用
                                   ▼
                ┌─────────────────────────────────────────┐
  算子(kernel):  │ kernels/ (CUDA kernel) + utils/(GEMM)  │   ← GPU 上真正干活的函数
                └─────────────────────────────────────────┘
```

一句话记忆：**kernel 是砖，layer 是墙，model 是楼，utils 是水电，外壳是门面**。

#### 4.1.3 源码精读

官方目录结构树就在 README 的 *Advanced* 章节里，逐行注释如下（左侧为目录，右侧是官方英文说明的中文意译）：

[README.md:L82-L91](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/README.md#L82-L91)：核心库的 9 个子目录与各自职责——

- `cutlass_extensions/`：对 CUTLASS GEMM/kernel 的扩展实现。
- `kernels/`：各种模型/层/操作的 CUDA kernel，例如 `addBiasResidual`。
- `layers/`：层模块的实现，例如 attention 层、FFN 层。
- `models/`：不同模型的实现，例如 BERT、GPT。
- `tensorrt_plugin/`：把 FT 封装成 TensorRT 插件。
- `tf_op/`：TensorFlow 自定义 OP 实现。
- `th_op/`：PyTorch 自定义 OP 实现（注意 torch 头文件前缀是 `th`，故称 `th_op`）。
- `triton_backend/`：Triton 自定义 backend 实现。
- `utils/`：通用 CUDA 工具，例如 `cublasMMWrapper`、`memory_utils`。

[README.md:L92-L101](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/README.md#L92-L101)：仓库其余顶层目录（`examples/`、`docs/`、`benchmark/`、`tests/`、`templates/`）的职责。

[README.md:L103](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/README.md#L103)：一句重要的提醒——「many folders contains many sub-folders to split different models」，即 `models/`、`th_op/`、`tf_op/`、`triton_backend/`、`tensorrt_plugin/` 内部都**按模型名再分子目录**（如 `models/bert/`、`models/multi_gpu_gpt/`、`th_op/multi_gpu_gpt/`）。这一点在 4.3 节会再次印证。

#### 4.1.4 代码实践

> 实践目标：把「框架外壳 vs 核心引擎」的区分落到具体文件上。

操作步骤：

1. 在仓库根目录执行 `ls src/fastertransformer/`，确认你看到了上文的 9 个子目录。
2. 执行 `ls src/fastertransformer/th_op/`，你会看到 `bert/`、`multi_gpu_gpt/`、`t5/`、`swin/` 等子目录，以及一个公共文件 `th_utils.h`。
3. 执行 `ls src/fastertransformer/models/`，你会看到几乎一一对应的 `bert/`、`multi_gpu_gpt/`、`t5/`、`swin/`。

需要观察的现象：`th_op/<model>/` 里的文件总是对应 `models/<model>/` 里某个模型类，说明外壳目录只是「按模型再切一刀」地镜像了核心模型目录。

预期结果：你会清楚地看到，PyTorch 真正调用的是 `models/` 下的 C++ 类，`th_op/` 只负责张量格式转换。如果无法运行 `ls`（例如在只读镜像里），则标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`docs/`、`benchmark/`、`tests/`、`templates/` 这四个顶层目录，哪一个是「教你怎么给 FT 加新模型」的？

**参考答案**：`templates/`。它的 `adding_a_new_model/README.md` 就是贡献指南。

**练习 2**：如果有人说「我要给 GPT 加一个 PyTorch 接口」，他需要改动哪些目录？

**参考答案**：核心模型逻辑在 `src/fastertransformer/models/multi_gpu_gpt/`（一般已存在），PyTorch 外壳在 `src/fastertransformer/th_op/multi_gpu_gpt/`，最后还要在 `examples/pytorch/gpt/` 下加调用示例。

---

### 4.2 kernel / layer / model 三层抽象

#### 4.2.1 概念说明

这是本讲最重要的概念，也是 FT 整个代码库的骨架。FT 把代码分成三层，自底向上分别是：

1. **kernel（算子）层**：GPU 上**一个具体的并行函数**，做一件最基础的事，比如「把 bias 加到残差上」「做一次 layernorm」。它不关心模型语义，只关心「给我一块显存，我按网格并行算」。命名一律以 `invoke` 开头。

2. **layer（层）层**：把若干 kernel + 若干矩阵乘（GEMM）**组合成一个有语义的模块**，比如「一个 self-attention 层」「一个 FFN 层」。一个 layer 类通常拥有自己的临时显存（buffer），并对外暴露 `forward(...)` 方法。

3. **model（模型）层**：把若干 layer **串成完整的模型前向流程**，比如「堆叠 12 个 BERT block」。一个 model 类负责加载权重、按顺序调用各层、管理输入输出张量。

为什么要分三层？因为**复用**。同一个 `invokeAddBiasResidual` kernel 会被 BERT、GPT、T5、ViT 的所有层共用；同一个 attention 层会被 BERT 和 ViT 共用。分层让「换一个模型」时只需要在 model 层动手，kernel 和 layer 几乎不动。

#### 4.2.2 核心流程

三层的「调用与被调用」关系如下：

```
model.forward()
   │
   ├── for each transformer block:
   │       layer.forward()        // attention 层、FFN 层
   │          ├── cublasMMWrapper.Gemm(...)   // 矩阵乘 (在 utils/)
   │          └── invokeXxxKernel(...)        // CUDA kernel (在 kernels/)
   │
   └── 返回输出张量
```

每一层只调用「正下方」那一层，不会跨层跳跃：model 只调 layer，layer 只调 kernel + utils 里的 GEMM。这种严格的分层是 FT 能在多模型间高效复用代码的根本原因。

#### 4.2.3 源码精读

**kernel 层示例**——`invokeAddBiasResidual`，一个典型的 elementwise 融合 kernel（README 的目录说明里就举了 `addBiasResidual` 这个例子）。它做的是「output = residual + bias」一类逐元素运算：

[add_residual_kernels.h:L28-L44](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/add_residual_kernels.h#L28-L44)：声明了一组 `invokeAddBiasResidual` 的重载，针对不同模板类型 `T`（FP16/FP32/BF16 等）和不同输入组合提供入口。注意函数名一律小驼峰、以 `invoke` 开头，这正是 kernel 层的命名标志。

**layer 层示例**——`BaseLayer`，所有层和模型的抽象基类，规定了「每个层都要能分配/释放临时显存」：

[BaseLayer.h:L27-L55](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/BaseLayer.h#L27-L55)：`class BaseLayer` 声明了 `getStream/setStream`，并把 `allocateBuffer()` 与 `freeBuffer()` 定义为**纯虚函数**——任何子层都必须实现这两个方法。这体现了 layer 层的核心契约：**自己管理临时显存的生命周期**。

**model 层示例**——`Bert`，BERT 模型类。注意它继承自 `BaseLayer`（model 也是「层」的一种特化），并暴露统一的 `forward` 接口：

[Bert.h:L33](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.h#L33)：`class Bert: public BaseLayer`——模型层复用了 layer 层的基类。

[Bert.h:L127-L130](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.h#L127-L130)：`void forward(...)` 的两个重载，一个接收 `std::vector<Tensor>*`，一个接收 `TensorMap*`，这是 FT 所有模型对外统一的入口形态。

**utils 层示例**——`Tensor`，贯穿全库的统一张量抽象。它用一个 `where` 字段标记张量在 CPU 还是 GPU：

[Tensor.h:L101-L112](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/Tensor.h#L101-L112)：`MemoryType` 枚举（`MEMORY_CPU`/`MEMORY_CPU_PINNED`/`MEMORY_GPU`）与 `struct Tensor` 的核心字段（`where`、`type`、`shape`、`data`）。这正是 model 层 `forward` 签名里 `TensorMap*` 背后的数据载体。

#### 4.2.4 代码实践

> 实践目标：用真实文件验证「model 调 layer、layer 调 kernel」的分层。

操作步骤：

1. 打开 [src/fastertransformer/models/bert/Bert.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc)，在 `forward` 函数体内搜索 `attention_layer_`、`ffn_layer_` 之类的调用——你会看到 model 层在调用 layer 层。
2. 打开任意一个 layer 实现，例如 [src/fastertransformer/layers/FfnLayer.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc)，搜索 `invoke` 或 `cublasMMWrapper`——你会看到 layer 层在调用 kernel 与 GEMM。
3. 在 [src/fastertransformer/kernels/add_residual_kernels.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/add_residual_kernels.cu) 里找到 kernel 层最底层的 `__global__` 函数。

需要观察的现象：越往下层（model→layer→kernel），代码越「纯计算」、越不关心模型语义；越往上层，越关心「这是 BERT 还是 GPT」、权重要怎么摆。

预期结果：你能在每一层都找到「调用了下一层」的证据，从而确信三层是严格分明的。如果你无法编译运行，本实践为「源码阅读型」，无需执行命令，标注「待本地验证」即可。

#### 4.2.5 小练习与答案

**练习 1**：函数名 `invokeLayerNorm` 属于哪一层？为什么？

**参考答案**：属于 kernel 层。它以 `invoke` 开头，是一个在 GPU 上做单件事（layernorm）的并行函数，不包含模型语义。

**练习 2**：为什么 `class Bert` 要继承 `BaseLayer`？

**参考答案**：因为 model 也需要管理自己的临时显存（`allocateBuffer/freeBuffer`）和 CUDA stream，复用 `BaseLayer` 的这套契约可以避免重复设计。

**练习 3**：假如你想给所有模型换一个更快的 layernorm kernel，需要改 model 层吗？

**参考答案**：不需要。只要新 kernel 保持同样的 `invokeLayerNorm` 接口，layer 层的调用代码无需改动，model 层更不用动——这正是分层的红利。

---

### 4.3 框架外壳目录：th_op / tf_op / triton_backend / tensorrt_plugin

#### 4.3.1 概念说明

核心引擎是纯 C++/CUDA，但用户往往更习惯用 PyTorch、TensorFlow、Triton 或 TensorRT 来跑推理。FT 用四个「外壳目录」把同一个核心模型包装成四种调用形态：

| 目录 | 对应框架 | 产物形态 |
| --- | --- | --- |
| `th_op/` | PyTorch | torch 自定义 op（一个 `.so` 扩展，可在 Python 里 `import`） |
| `tf_op/` | TensorFlow | TF 自定义 op |
| `triton_backend/` | Triton Inference Server | 自定义 backend（动态库，供 Triton 加载） |
| `tensorrt_plugin/` | TensorRT | TensorRT plugin |

这些目录里的代码**不做任何模型计算**，只做两件事：

1. 把框架传进来的张量（如 `torch::Tensor`、`tensorflow::Tensor`）**零拷贝地转换**成核心库的 `Tensor`（其实就是取出裸指针 + 形状 + 设备号）。
2. 调用 `models/` 下对应的模型类完成真正的前向，再把结果转回框架张量。

> 关于命名：`th_op` 里的 `th` 来自 PyTorch 的历史头文件前缀 `TH`（Torch/Historic），是社区约定俗成的写法，不要和 `tf`（TensorFlow）混淆。

#### 4.3.2 核心流程

一次 PyTorch 推理的调用链：

```
Python: output = ft_gpt_op(input_ids)
   │  (torch 自定义 op 边界)
   ▼
th_op/multi_gpu_gpt/ParallelGptOp.cc   ← torch::Tensor → TensorMap
   │
   ▼
models/multi_gpu_gpt/ParallelGpt.cc    ← 真正的前向 (核心引擎)
   │
   ▼
th_op 把结果 TensorMap → torch::Tensor，返回 Python
```

`triton_backend/` 与 `tensorrt_plugin/` 的套路完全一致，只是两端的「框架张量类型」不同。

#### 4.3.3 源码精读

`th_op/` 公共工具：`th_utils.h` 负责把 torch 张量桥接到 FT 张量。

[th_utils.h:L27](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/th_utils.h#L27)：`#include <torch/custom_class.h>`——这行说明 `th_op` 依赖的是 torch 的自定义类/op 机制，是 PyTorch 与 C++ 的官方胶水层。`th_utils.cu` 中提供 `convert_tensor`、`get_ptr` 等工具，把 `torch::Tensor` 的 data_ptr 与 shape 取出来，包成 FT 的 `Tensor`。

由于 `th_op/`、`tf_op/`、`triton_backend/`、`tensorrt_plugin/` 都「按模型再分子目录」，你可以直接 `ls` 看到对应关系：

- [src/fastertransformer/th_op/](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op) 下有 `bert/`、`multi_gpu_gpt/`、`t5/`、`gptj/`、`vit/` 等。
- [src/fastertransformer/triton_backend/](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/triton_backend) 下有 `multi_gpu_gpt/`、`bert/`、`t5/`、`gptj/` 等，以及公共的 `transformer_triton_backend.hpp`。
- [src/fastertransformer/tensorrt_plugin/](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tensorrt_plugin) 下只有 `bert_fp8/`、`t5/`、`vit/`、`swin/`、`wenet/`——可见 TensorRT 插件只覆盖了**少数**模型（这也是支持矩阵里 TensorRT 行较少的原因）。

#### 4.3.4 代码实践

> 实践目标：用 `ls` 验证「外壳目录镜像了模型目录，但覆盖范围不同」。

操作步骤：

1. `ls src/fastertransformer/models/` 数一下模型子目录总数。
2. `ls src/fastertransformer/th_op/` 数一下 PyTorch 外壳覆盖的模型数。
3. `ls src/fastertransformer/tensorrt_plugin/` 数一下 TensorRT 插件覆盖的模型数。

需要观察的现象：`models/` 覆盖最全；`th_op/`（PyTorch）覆盖其次；`tensorrt_plugin/` 最少。

预期结果：这正好对应 u1-l1 支持矩阵里「C++ 全覆盖、PyTorch 次之、TensorRT 仅个别模型」的现象。如果无法执行命令，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `tensorrt_plugin/` 下的子目录比 `th_op/` 少很多？

**参考答案**：因为 TensorRT 集成需要为每个模型单独写 plugin 并适配 TRT 的图构建流程，成本高，所以官方只为少数高价值模型（bert_fp8/t5/vit/swin/wenet）做了封装；而 PyTorch op 的封装成本低，覆盖更广。

**练习 2**：`th_utils.h` 里的「零拷贝」转换，本质上取的是 `torch::Tensor` 的什么信息？

**参考答案**：取的是底层裸数据指针（`data_ptr`）、形状（`sizes`）和所在设备（CPU/GPU），把它们包装成 FT 的 `Tensor{where, type, shape, data}`，全程不搬运数据。

---

### 4.4 命名规范与新增模型指引

#### 4.4.1 概念说明

FT 有一套清晰的命名约定，掌握它之后，你看到一个文件名就能猜出它属于哪一层、装了什么。这套规范写在 `templates/adding_a_new_model/README.md` 的 *Coding style* 与 *How to add a new model* 两节里。同一个文件还给出了「新增一个模型」的标准 7 步流程，是后续 u11（新增模型）讲义的预览。

#### 4.4.2 核心流程

FT 命名规则一览：

| 命名对象 | 规则 | 例子 |
| --- | --- | --- |
| 文件名（只含一个类） | 大驼峰（UpperCamelCase） | `BertLayer.cc` 只含 `BertLayer` 类 |
| 文件名（工具/多函数） | 全小写 + 下划线 | `cuda_utils.h`、`add_residual_kernels.cu` |
| 函数名 | 小驼峰（lowerCamelCase），kernel 常以 `invoke` 起头 | `invokeLayerNorm`、`forward` |
| 变量名 | 全小写 + 下划线 | `batch_size`、`seq_len` |
| 代码风格 | 尽量遵循仓库根目录的 `.clang-format` | — |

一个实用的判断技巧：看到 `invokeXxx` → kernel 层；看到 `XxxLayer` → layer 层；看到 `Xxx`（不带 Layer 后缀，且文件在 `models/`）→ model 层。

新增模型的核心原则（贡献指南反复强调）：**模型架构相似但不同时，不要改老模型去迁就新模型，而要复用现有 layer/kernel 新建一个类**。贡献指南举的例子是 `Encoder.cc` 与 `Bert.cc` 差别只是 layernorm 的位置——正确做法是复用 attention/FFN/layernorm 组件新建 `Encoder` 类，而不是把 `Bert` 改成「既能当 Bert 又能当 Encoder」。

#### 4.4.3 源码精读

新增模型的 7 步流程（以 Longformer 为例）：

[templates/adding_a_new_model/README.md:L6-L17](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/templates/adding_a_new_model/README.md#L6-L17)：从「检查可复用组件」到「提交 PR」的完整步骤。其中第 2 步要求在 `src/fastertransformer/models/` 下建 `longformer/` 目录；第 3 步要求把模型专属的 attention 层放到 `src/fastertransformer/layers/`，文件名取 `LongformerAttentionLayer`。

[templates/adding_a_new_model/README.md:L13](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/templates/adding_a_new_model/README.md#L13)：那条最重要的原则原文——「don't modify the current model to fit the new model」（不要为了适配新模型而去改老模型）。

命名规范条款：

[templates/adding_a_new_model/README.md:L46-L55](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/templates/adding_a_new_model/README.md#L46-L55)：*Coding style* 一节，规定了文件名（大驼峰 vs 小写下划线）、函数名（小驼峰）、变量名（小写下划线）三条规则，并要求尽量遵循 `.clang-format`。

你可以用真实文件印证这套规则：

- `models/bert/Bert.cc` 只含 `Bert` 类 → 大驼峰文件名 ✓（见 [Bert.h:L33](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.h#L33)）。
- `layers/FfnLayer.h` 只含 `FfnLayer` 类 → 大驼峰文件名 ✓。
- `kernels/add_residual_kernels.cu` 含多个函数 → 小写下划线 ✓。
- `utils/cuda_utils.h` 是工具文件 → 小写下划线 ✓。
- 函数 `invokeAddBiasResidual` → 小驼峰 + `invoke` 前缀 ✓（见 [add_residual_kernels.h:L28](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/add_residual_kernels.h#L28)）。

#### 4.4.4 代码实践

> 实践目标：用命名规则反推一个陌生文件属于哪一层。

操作步骤：

1. 执行 `ls src/fastertransformer/kernels/ | head`，挑 5 个文件名。
2. 对每个文件名判断：它是「单类文件（大驼峰）」还是「工具/多函数文件（小写下划线）」？属于 kernel 层吗？
3. 再到 `src/fastertransformer/layers/`、`src/fastertransformer/models/bert/` 各挑 3 个文件，验证「单类文件用大驼峰」的规律。

需要观察的现象：`kernels/` 下几乎全是小写下划线文件（因为一个 kernel 文件通常含多个 `invokeXxx`）；`models/bert/` 下 `Bert.cc`、`BertWeight.h`、`BertLayerWeight.h` 都是大驼峰（因为各自只含一个类）。

预期结果：你会形成条件反射——看到大驼峰文件名，多半是「一个类」；看到 `invokeXxx` 函数，一定是 kernel。本实践为源码阅读型，无需运行，标注「待本地验证」亦可。

#### 4.4.5 小练习与答案

**练习 1**：文件 `memory_utils.cu` 为什么不是大驼峰？

**参考答案**：因为它是一个工具文件，含多个工具函数（`deviceMalloc`、`deviceFree` 等），不属于「只含一个类」的情况，按规则用小写加下划线。

**练习 2**：我要新增一个叫「FastBert」的模型，文件和目录该怎么放？

**参考答案**：模型类放 `src/fastertransformer/models/fast_bert/FastBert.cc`（文件名大驼峰 `FastBert`，目录按模型名小写带下划线）；若有专属 attention 层，放 `src/fastertransformer/layers/FastBertAttentionLayer.cc`；最后在 `examples/cpp/fast_bert/` 加示例、在 `docs/fast_bert_guide.md` 写说明。注意：不要去改原有的 `bert/Bert.cc`。

---

## 5. 综合实践

把本讲内容串起来：请为 `kernels`、`layers`、`models`、`utils`、`th_op` 五个目录各写**一句话职责说明**，并从仓库中为每个目录挑**一个代表性文件**举例（给出相对路径）。

参考答案模板（你可以照此格式填写，并对照本讲核对）：

| 目录 | 一句话职责 | 代表性文件 |
| --- | --- | --- |
| `kernels/` | GPU 上单件事的并行函数（kernel），命名以 `invoke` 开头，是性能优化的最小单位。 | `src/fastertransformer/kernels/add_residual_kernels.cu` |
| `layers/` | 把 kernel + GEMM 组合成有语义的层模块（attention/FFN…），自管临时显存。 | `src/fastertransformer/layers/BaseLayer.h` |
| `models/` | 把若干层串成完整模型前向流程，按模型名再分子目录。 | `src/fastertransformer/models/bert/Bert.cc` |
| `utils/` | 贯穿全库的通用工具：张量抽象、显存分配、cuBLAS 封装、日志等。 | `src/fastertransformer/utils/Tensor.h` |
| `th_op/` | 把核心模型包装成 PyTorch 自定义 op，做 torch 张量与 FT 张量的零拷贝转换。 | `src/fastertransformer/th_op/th_utils.h` |

进阶一步：对照 4.2 节的分层图，在你挑出的 `models/` 代表文件里找到它调用了哪个 `layers/` 文件、那个 layer 又调用了哪个 `kernels/` 文件，画出一条从 model 到 kernel 的最小调用链。这条链就是后续讲义（u3 算子与层、u4 模型）会逐层拆开的内容。

## 6. 本讲小结

- FT 的核心库 `src/fastertransformer/` 分成「核心引擎」（`kernels/layers/models/utils/cutlass_extensions`）与「框架外壳」（`th_op/tf_op/triton_backend/tensorrt_plugin`）两大块，引擎与框架解耦。
- **kernel / layer / model 三层抽象**是全库骨架：kernel 是 GPU 上的一个并行函数（`invokeXxx`），layer 把 kernel+GEMM 组成有语义的层，model 把层串成完整前向；下层不感知上层，上层复用下层。
- `utils/` 提供 `Tensor`/`TensorMap`、显存分配、cuBLAS 封装等被所有层共用的基础设施；`Tensor.where` 字段标记张量在 CPU 还是 GPU。
- 四个外壳目录都「按模型再分子目录」，但覆盖范围不同：`th_op`（PyTorch）较全，`tensorrt_plugin` 只覆盖少数模型——这正是 u1-l1 支持矩阵在代码层面的体现。
- 命名约定：单类文件用大驼峰（`Bert.cc`），工具/多函数文件用小写下划线（`cuda_utils.h`），函数用小驼峰且 kernel 以 `invoke` 起头，变量用小写下划线（`batch_size`）。
- 新增模型的铁律：**复用现有 layer/kernel 新建类，绝不要改老模型去迁就新模型**。

## 7. 下一步学习建议

本讲只是「地图」。接下来建议：

1. **先把环境跑起来**：进入 u1-l4（编译并运行 BERT/GPT 的 C++ 示例），让你能在终端里真正调用起 `models/bert/Bert.cc`，把本讲的抽象目录变成可运行的程序。
2. **再看日志与调试**：u1-l5 会讲解 `FT_LOG_LEVEL`/`FT_NVTX`/`FT_DEBUG_LEVEL`，这些是你在阅读源码、定位「哪一层出了问题」时的利器。
3. **深入某一层**：当你想真正理解代码，按「自底向上」顺序学 Unit 2（核心基础设施：`Tensor`/显存/cuBLAS）→ Unit 3（kernel 与 layer）→ Unit 4（model）。届时本讲提到的 `BaseLayer.h`、`Tensor.h`、`Bert.cc`、`add_residual_kernels.cu` 都会成为主角。
4. **想贡献代码**：直接精读本讲引用的 [templates/adding_a_new_model/README.md](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/templates/adding_a_new_model/README.md)，它是 u11-l2（新增模型指南）的预览。
