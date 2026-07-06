# 第一个示例：编译并运行 BERT 与 GPT 的 C++ 示例

## 1. 本讲目标

本讲是「让 FasterTransformer（以下简称 FT）真正在你机器上跑起来」的第一步。读完本讲，你应当能够：

- 看懂 FT C++ 示例 `main` 函数的两种入口风格：**命令行参数式**（BERT）与 **INI 配置文件式**（多 GPU GPT）。
- 理解示例如何用「模板 + 数据类型分发」机制，让同一份代码同时支持 FP32 / FP16 / BF16。
- 逐个解释 `./bin/bert_example 32 12 32 12 64 1 0` 这条命令里 7 个参数的含义。
- 说清楚 `multi_gpu_gpt_example` 是如何用 `INIReader` 读配置、用 MPI 组织多进程、再调用 `ParallelGpt::forward` 的。
- 在本地（或脑中）走通一条「编译 → 配置 → 运行 → 看耗时输出」的完整链路。

本讲只聚焦**示例入口与参数/配置解析**，不深入模型 `forward` 内部——那是后续 Unit 4（BERT 模型）和 Unit 6（GPT 与大模型推理）的内容。

---

## 2. 前置知识

在开始前，请确保你已经理解以下概念（来自 [u1-l1 项目总览](u1-l1-project-overview.md)、[u1-l2 构建系统](u1-l2-build-system.md)、[u1-l3 目录结构](u1-l3-directory-structure.md)）：

- **FT 是推理库，不是训练框架**：所有示例都只做前向（forward），目的是测速度、验证正确性。
- **数据类型（data_type）**：FT 的矩阵乘支持 FP32、FP16、BF16（以及 INT8/FP8，但本讲两个示例不涉及）。FT 用 C++ 模板 `template<typename T>` 让同一套代码适配多种精度。
- **CMake 编译产物**：根据 [u1-l2](u1-l2-build-system.md)，所有可执行文件都被统一输出到 `${CMAKE_BINARY_DIR}/bin` 目录（即 `build/bin/`），所以运行命令都以 `./bin/xxx_example` 开头。
- **kernel / layer / model 三层抽象**（[u1-l3](u1-l3-directory-structure.md)）：示例入口属于最上层的「调用方」，它负责构造 model 对象、组装输入张量、调用 `forward`。

另外补充两个本讲会用到的 C++ 小知识：

| 概念 | 通俗解释 |
| --- | --- |
| `argc` / `argv` | C/C++ `main` 函数的标准参数。`argc` 是参数个数（**包含程序名本身**），`argv[0]` 是程序名，`argv[1]` 才是第一个用户参数。 |
| `template<typename T>` | C++ 模板。写一份代码，编译器按需生成 `float`、`half`、`__nv_bfloat16` 等多个版本。FT 用它实现「一份逻辑、多种精度」。 |
| INI 文件 | 一种简单的配置文本格式，用 `[section]` 分段、`key=value` 赋值。FT 用第三方库 `INIReader` 解析它。 |

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [examples/cpp/bert/bert_example.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/bert/bert_example.cc) | BERT 单 GPU C++ 示例，**命令行参数**入口。 |
| [examples/cpp/bert/bert_config.ini](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/bert/bert_config.ini) | BERT 的示例 INI 配置（部署场景参考用，**C++ 版 bert_example 本身不读它**，见 4.1.1 说明）。 |
| [examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc) | 多 GPU GPT C++ 示例，**INI 配置 + MPI** 入口。 |
| [examples/cpp/multi_gpu_gpt/gpt_config.ini](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_config.ini) | GPT 示例的核心配置文件，定义模型结构、并行规模、采样参数。 |
| [examples/cpp/multi_gpu_gpt/gpt_example_utils.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_example_utils.cc) | GPT 示例的配置读取工具实现：`read_model_config` / `read_request_config`。 |
| [examples/cpp/multi_gpu_gpt/gpt_example_utils.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_example_utils.h) | 上述工具的头文件，定义 `model_config_t` / `request_config_t` 两个配置结构体。 |

> 提示：两个示例代表了 FT 里**两种最典型的入口范式**——简单模型用命令行参数，复杂/多 GPU 模型用配置文件。掌握这两种范式，后面看任何 example 都能迅速找到「参数从哪来」。

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

1. **bert_example 的 main**：命令行参数解析与数据类型分发。
2. **bertExample 模板函数**：组装一个能跑的 BERT 并测耗时。
3. **multi_gpu_gpt_example 的 main**：INI 配置 + MPI 多进程入口。
4. **配置读取**：`read_model_config` / `read_request_config` 如何把 INI 翻译成结构体。

---

### 4.1 bert_example：命令行参数解析与数据类型分发

#### 4.1.1 概念说明

BERT 是 FT 里最简单的示例之一：单 GPU、纯前向、用随机生成的假数据测速度。它的所有运行参数都通过**命令行参数**传入，没有配置文件。

> ⚠️ **一个容易踩坑的点**：虽然仓库里有 `examples/cpp/bert/bert_config.ini`，但 C++ 版的 `bert_example.cc` **并不读取它**——你在源码里搜不到任何 `INIReader`。那个 ini 是给部署场景（如 Triton / PyTorch 封装）做参考的样例。C++ 的 BERT 示例完全靠 `argv` 解析参数。这点和 GPT 示例正好相反，对比着记最牢。

#### 4.1.2 核心流程

`bert_example` 的 `main` 做三件事，流程如下：

```
启动 main(argc, argv)
   │
   ├─ ① 校验参数个数：必须是 7 个（argc == 8，含程序名）
   │      不够则打印用法并退出
   │
   ├─ ② 用 atoi 把 argv[1..7] 转成整型/布尔
   │      batch_size / num_layers / seq_len / head_num /
   │      size_per_head / data_type / is_remove_padding
   │
   └─ ③ 根据 data_type 分发到模板实例：
          0 → bertExample<float>           (FP32)
          1 → bertExample<half>            (FP16)
          2 → bertExample<__nv_bfloat16>   (BF16，需 ENABLE_BF16)
```

#### 4.1.3 源码精读

**① 参数个数校验**——`argc != 8` 表示「程序名 + 7 个参数」，少了就直接报错退出，并打印用法示例：

[examples/cpp/bert/bert_example.cc:34-38](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/bert/bert_example.cc#L34-L38) —— 校验参数个数并打印用法。

注意错误信息里给出的示例 `./bin/bert_example 32 12 32 12 64 0 0` 正好是 7 个数字，印证了「7 个参数」的约定。

**② 逐个解析参数**——`argv[1]` 到 `argv[7]` 分别对应一个模型超参，`atoi` 把字符串转成整数；第 6 个参数 `data_type` 被强制转成 `CublasDataType` 枚举：

[examples/cpp/bert/bert_example.cc:40-46](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/bert/bert_example.cc#L40-L46) —— 把 7 个命令行参数映射到变量，注释明确写出 `0 FP32, 1 FP16, 2 BF 16`。

这 7 个参数的含义汇总如下（这也是本讲实践任务要回答的表格）：

| 位置 | 参数名 | 示例值 | 含义 |
| --- | --- | --- | --- |
| argv[1] | `batch_size` | 32 | 一次前向处理多少条序列 |
| argv[2] | `num_layers` | 12 | transformer block 的层数 |
| argv[3] | `seq_len` | 32 | 每条序列的长度（token 数） |
| argv[4] | `head_num` | 12 | 多头注意力的头数 |
| argv[5] | `size_per_head` | 64 | 每个头的维度（hidden = head_num × size_per_head = 768） |
| argv[6] | `data_type` | 1 | 计算精度：0=FP32, 1=FP16, 2=BF16 |
| argv[7] | `is_remove_padding` | 0 | 是否启用去 padding（Effective FasterTransformer），0=false, 1=true |

> 术语解释——**hidden_units（隐层维度）**：BERT 的「宽度」，等于 `head_num × size_per_head`。上例中 `12 × 64 = 768`，正是 `bert-base` 的标准宽度。源码里第 71 行就是这么算的：`const size_t hidden_units = head_num * size_per_head;`。

**③ 数据类型分发**——这是 FT 全库的通用套路：用一组 `if/else if` 把枚举值映射到模板实例。注意 BF16 分支被包在 `#ifdef ENABLE_BF16` 里，只有按 [u1-l2](u1-l2-build-system.md) 用 CUDA ≥ 11 编译时才会存在：

[examples/cpp/bert/bert_example.cc:48-62](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/bert/bert_example.cc#L48-L62) —— 根据 `data_type` 分发到 `bertExample<float>` / `bertExample<half>` / `bertExample<__nv_bfloat16>`。

这种「枚举 → 模板实例」的 dispatch 模式在 FT 的框架外壳（th_op / tf_op / triton_backend）里会反复出现，是理解 FT 入口的钥匙。

#### 4.1.4 代码实践

**实践目标**：把 `./bin/bert_example 32 12 32 12 64 1 0` 这条命令的每个参数对应到源码变量，并预测它的运行行为。

**操作步骤**（源码阅读型，无需 GPU）：

1. 打开 [bert_example.cc:40-46](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/bert/bert_example.cc#L40-L46)。
2. 把命令 `./bin/bert_example 32 12 32 12 64 1 0` 的 7 个数字依次填进下表。

| 参数位置 | 变量名 | 命令中的值 | 含义 |
| --- | --- | --- | --- |
| argv[1] | batch_size | 32 | 一次跑 32 条序列 |
| argv[2] | num_layers | 12 | 12 层 transformer block |
| argv[3] | seq_len | 32 | 每条序列 32 个 token |
| argv[4] | head_num | 12 | 12 个注意力头 |
| argv[5] | size_per_head | 64 | 每头 64 维 → hidden=768 |
| argv[6] | data_type | 1 | **FP16**（因为 1=FP16） |
| argv[7] | is_remove_padding | 0 | 不去 padding |

**需要观察的现象 / 预期结果**：

- 由于 `data_type=1`，程序会进入 [bert_example.cc:51-52](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/bert/bert_example.cc#L51-L52) 的 `HALF_DATATYPE` 分支，最终实例化 `bertExample<half>`，并以 FP16 精度运行。
- `is_remove_padding=0` 表示走「带 padding」的传统路径，attention 类型会在第 111 行由 `getAttentionType` 根据当前 GPU 的 SM 版本决定（可能落到 unfused 或 fused）。
- 运行成功时，终端最后会打印一行类似 `batch_size 32 seq_len 32 layer 12 FT-CPP-time XX.XX ms (100 iterations)` 的耗时统计（见 4.2.3）。

> ⚠️ **运行前置条件**（来自 [docs/bert_guide.md](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/bert_guide.md)）：在跑 `bert_example` 之前，官方文档要求先运行 `./bin/bert_gemm 32 32 12 64 1 0` 生成 `gemm_config.in`（GEMM 算法调优表）。这是因为 [bert_example.cc:85](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/bert/bert_example.cc#L85) 会 `new cublasAlgoMap("gemm_config.in", "")` 去读它。具体调优原理见 [u2-l4 GEMM 自动调优](u2-l4-gemm-autotuning.md)，本讲只需知道「先跑 bert_gemm」即可。**待本地验证**：若缺少该文件，程序行为以本地实测为准。

#### 4.1.5 小练习与答案

**练习 1**：如果把命令改成 `./bin/bert_example 32 12 32 12 64 2 0`，但你的 FT 是用 CUDA 10 编译的（没有定义 `ENABLE_BF16`），会发生什么？

> **答案**：`data_type=2` 对应 `BFLOAT16_DATATYPE`，但该分支被 `#ifdef ENABLE_BF16` 包裹，CUDA 10 编译时这段代码根本不存在。于是所有 `if/else if` 都不命中，落到最后的 `else` 分支抛出 `std::runtime_error`（见 [bert_example.cc:59-62](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/bert/bert_example.cc#L59-L62)），程序报错退出。

**练习 2**：为什么 FT 要用「命令行传整数 + 枚举分发」而不是直接写死一种数据类型？

> **答案**：因为 FT 同一套算子要支持 FP32/FP16/BF16（甚至 INT8/FP8）多种精度。用模板 `template<typename T>` 写一次逻辑，再用命令行参数在运行期选择实例化哪个版本，就能在不重新改源码的前提下切换精度，便于对比性能与精度。

---

### 4.2 bertExample 模板函数：组装一个可运行的 BERT

#### 4.2.1 概念说明

`main` 只负责「读参数 + 选精度」，真正干活的是模板函数 `bertExample<T>`。它演示了**调用 FT 任何一个模型的标准四步套路**：

1. **建资源**：CUDA stream、cuBLAS/cuBLASLt handle、allocator、`cublasMMWrapper`。
2. **建权重**：构造 `BertWeight<T>`（本讲用随机初始化的假权重，不加载真实 checkpoint）。
3. **建模型**：构造 `Bert<T>` 对象，把超参与资源传进去。
4. **组装张量并 forward**：构造输入/输出 `std::vector<Tensor>`，调 `bert.forward(...)`。

这四步是 FT 所有 example 的通用骨架，记住它就抓住了主线。

#### 4.2.2 核心流程

```
bertExample<T>(超参)
   │
   ├─ ① hidden_units = head_num * size_per_head
   │     inter_size  = 4 * hidden_units
   ├─ ② 创建 stream / cublas / cublasLt handle
   │     new cublasAlgoMap("gemm_config.in")
   │     构造 allocator 与 cublasMMWrapper
   │     按 T 设置 GEMM 精度（setFP16GemmConfig 等）
   ├─ ③ 构造 BertWeight<T>（随机权重）
   │     getAttentionType<T>(...) 决定 attention 实现
   │     构造 Bert<T> 模型对象
   ├─ ④ deviceMalloc 输入/输出张量 + 随机 sequence_lengths
   │     组装 input_tensors / output_tensors
   ├─ ⑤ warmup 10 次 → 计时 100 次 forward
   └─ ⑥ 打印平均耗时，释放资源
```

#### 4.2.3 源码精读

**配置 GEMM 精度**——根据模板类型 `T`，调用 `cublasMMWrapper` 上不同的 `setXxxGemmConfig`，让后续所有矩阵乘都跑在对应精度上。这是「模板 → 精度」的第二次落地：

[examples/cpp/bert/bert_example.cc:97-107](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/bert/bert_example.cc#L97-L107) —— 按类型设置 cuBLAS GEMM 配置（FP16/BF16/FP32）。

**构造模型**——`Bert<T>` 的构造参数是一长串，本讲只需关注几个关键位：`head_num`、`size_per_head`、`inter_size`、`num_layers`、`stream`、`&cublas_wrapper`、`&allocator`、`attention_type`、以及最后的 `LayerNormType::post_layernorm`（BERT 用后置 LayerNorm）。前两个 `0` 是已废弃的 `max_batch_size_` 和 `max_seq_len_`：

[examples/cpp/bert/bert_example.cc:113-128](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/bert/bert_example.cc#L113-L128) —— 构造 `Bert<T>` 模型对象，注释标出了两个已废弃参数。

> 术语解释——**attention_type**：FT 的注意力有多种实现（unfused / fused / 各精度版本），`getAttentionType<T>(...)` 会根据「size_per_head、GPU 的 SM 版本、是否去 padding、seq_len」自动选一个最合适的。本讲把它当成一个黑盒返回值即可，原理见 [u3-l2 注意力 kernel](u3-l2-attention-kernels.md)。

**组装张量**——这是看懂 FT 接口的关键。输入是两个 `Tensor`（`from_tensor` 是输入隐藏态，`d_sequence_lengths` 是每条序列的真实长度），输出一个 `Tensor`（`out_tensor`）。注意 `MEMORY_GPU` 标记数据在显存上，`getTensorType<T>()` 把 C++ 类型映射回 `DataType` 枚举（详见 [u2-l1 张量抽象](u2-l1-tensor-data-structure.md)）：

[examples/cpp/bert/bert_example.cc:145-156](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/bert/bert_example.cc#L145-L156) —— 用 `std::vector<Tensor>` 组装输入与输出张量。

**warmup + 计时**——先空跑 10 次（让 GPU 进入稳定状态、完成懒加载/缓存），再用 `CudaTimer` 计时 100 次取平均。这是 GPU 性能测量的标准做法：

[examples/cpp/bert/bert_example.cc:160-182](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/bert/bert_example.cc#L160-L182) —— warmup + 100 次计时 forward，最后 `FT_LOG_INFO` 打印平均耗时。

#### 4.2.4 代码实践

**实践目标**：通过阅读源码，回答「为什么示例要先 warmup 10 次再计时」。

**操作步骤**：

1. 阅读 [bert_example.cc:159-174](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/bert/bert_example.cc#L159-L174)。
2. 注意第 160-162 行的 warmup 循环和第 166 行的 `const int ite = 100`。

**需要观察的现象 / 预期结果**：

- 第一次 `forward` 往往比后续慢很多（cuBLAS 首次调用会做 JIT 编译、kernel 首次加载、allocator 首次分配显存）。
- 如果不 warmup 直接把第一次算进平均，测出来的耗时会偏高、不稳定。
- 因此 FT 的标准测速范式是「warmup N 次 → 计时 M 次取平均」，这个范式在 GPT 示例里也会出现。

> 说明：本实践为源码阅读型，不要求在 GPU 上实跑。若你在本地有 GPU，可尝试把 warmup 改成 0 次，观察打印耗时的变化（预期：变慢且不稳定）。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`bertExample` 里用的是真实 BERT 权重吗？从哪一行可以看出来？

> **答案**：不是真实权重。[bert_example.cc:109](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/bert/bert_example.cc#L109) 直接 `BertWeight<T> bert_weights(hidden_units, inter_size, num_layers);` 构造，构造函数内部用随机值初始化，并且全程没有调用类似 `loadModel(...)` 的方法。所以这个示例只测「前向速度」，输出的具体数值没有语义意义。

**练习 2**：`hidden_units` 和 `inter_size` 是怎么算出来的？

> **答案**：见 [bert_example.cc:71-72](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/bert/bert_example.cc#L71-L72)：`hidden_units = head_num * size_per_head`（注意力输入宽度），`inter_size = 4 * hidden_units`（FFN 中间层宽度，标准 transformer 的 4 倍约定）。

---

### 4.3 multi_gpu_gpt_example：基于 INI 配置文件与 MPI 的入口

#### 4.3.1 概念说明

GPT 示例比 BERT 复杂得多：它要**多 GPU 并行**、要**加载真实权重**、参数也多得多。命令行塞不下，于是 FT 改用 **INI 配置文件**。又因为多 GPU 需要「多个进程协同」，`main` 的第一件事就是 `mpi::initialize`（MPI = Message Passing Interface，多进程通信标准）。

总结两种入口范式的对比：

| 维度 | bert_example（命令行式） | multi_gpu_gpt_example（INI + MPI 式） |
| --- | --- | --- |
| 参数来源 | `argv` 命令行 | `gpt_config.ini` 文件 |
| 进程模型 | 单进程单 GPU | 多进程（每 GPU 一个），MPI 组织 |
| 权重 | 随机假权重 | `loadModel` 加载真实 checkpoint |
| 数据类型选择 | `atoi(argv[6])` 整数 | ini 里 `data_type=fp16` 字符串 |
| 典型启动 | `./bin/bert_example ...` | `mpirun -n 8 ./bin/multi_gpu_gpt_example` |

#### 4.3.2 核心流程

```
main(argc, argv)
   │
   ├─ mpi::initialize(&argc, &argv)        ← 多进程通信初始化
   ├─ 确定 ini_name（argv[1] 或默认 gpt_config.ini）
   ├─ 确定 in_csv（argv[2] 或默认 start_ids.csv，作为输入 prompt）
   ├─ INIReader reader = INIReader(ini_name)
   ├─ reader.ParseError() < 0 ? 报错退出 : 继续
   ├─ data_type = reader.Get("ft_instance_hyperparameter", "data_type")
   └─ 按 "fp32"/"fp16"/"bf16" 分发到 multi_gpu_gpt_example<T>(reader, in_csv)
```

#### 4.3.3 源码精读

**MPI 初始化**——这是 GPT 示例和 BERT 示例最本质的区别。`mpi::initialize` 会解析 mpirun 传入的进程信息，给每个进程分配一个 rank（编号）：

[examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc:37-40](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc#L37-L40) —— `main` 第一步就是 `mpi::initialize`。

> 术语解释——**rank / world_size**：MPI 把每个进程叫做一个 rank，rank 从 0 开始编号；所有参与进程的总数叫 world_size。例如 `mpirun -n 8` 启动 8 个进程，world_size=8，每个进程拿到自己的 rank ∈ {0,1,…,7}。详见 [u7-l2 流水并行与 MPI](u7-l2-pipeline-parallel-mpi.md)。

**配置文件路径解析**——支持 0/1/2 个命令行参数：不传则用默认 ini 与默认 csv。这让你可以直接 `./bin/multi_gpu_gpt_example` 一键跑（前提是在 `build/` 目录下，默认相对路径才指向正确文件）：

[examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc:42-56](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc#L42-L56) —— 解析 ini 与 csv 路径，带默认值。

**读取并校验 INI**——`INIReader` 解析失败时 `ParseError()` 返回负数，程序直接退出。读出 `data_type` 字符串后做分发：

[examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc:58-79](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc#L58-L79) —— 解析 INI 并按字符串 `data_type` 分发模板实例。

注意这里和 BERT 的两点不同：(1) 用**字符串**（"fp16"）而不是整数；(2) 传给模板函数的是 `reader` 对象本身——也就是说，「读配置」的工作被延迟到模板函数里再做（见 4.4）。

**模板函数里的主线**——`multi_gpu_gpt_example<T>` 的骨架和 BERT 类似（建资源→建权重→建模型→forward），但多了三件 BERT 没有的事：

[examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc:89-95](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc#L89-L95) —— 调用 `read_model_config` / `read_request_config` 把 INI 翻译成结构体，再用 `init_multiprocessing` 拿到本进程的 rank/world_size。

[examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc:124-137](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc#L124-L137) —— 构造 `ParallelGptWeight<T>` 并调用 `gpt_weights.loadModel(model_config.model_dir)`，加载真实权重。

[examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc:261-265](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc#L261-L265) —— 计时循环里调 `gpt.forward(&output_tensors, &input_tensors, &gpt_weights)`，与 BERT 的 `bert.forward` 接口形式完全一致。

> 重要观察：尽管 BERT 和 GPT 的复杂度天差地别，但最终**调用 model 的接口形式是统一的**——都是 `model.forward(&output_tensors, &input_tensors, &weights)`。这正是 [u1-l3](u1-l3-directory-structure.md) 讲的「model 层统一前向接口」的体现，也是 [u2-l1](u2-l1-tensor-data-structure.md) 要讲的 `Tensor`/`TensorMap` 统一载体的设计动机。

#### 4.3.4 代码实践

**实践目标**：对照 [docs/gpt_guide.md](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md) 的运行指引，写出一组真实的启动命令，并解释每个参数。

**操作步骤**：

1. 阅读 [multi_gpu_gpt_example.cc:42-56](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc#L42-L56)，记住默认 ini/csv 路径。
2. 阅读 [gpt_config.ini:10-17](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_config.ini#L10-L17)，记住 `tensor_para_size=1`、`pipeline_para_size=1`、`model_name=megatron_345M`。
3. 推导：因为 TP×PP=1×1=1，所以 world_size 必须等于 1，**单进程**就能跑。

**预期命令**（单 GPU 最简启动，来自 [docs/gpt_guide.md](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md) 第 421 行附近）：

```bash
cd build
./bin/multi_gpu_gpt_example
```

等价于显式带参数：

```bash
./bin/multi_gpu_gpt_example ../examples/cpp/multi_gpu_gpt/gpt_config.ini \
                           ../examples/cpp/multi_gpu_gpt/start_ids.csv
```

**多 GPU 启动**（把 ini 里 `tensor_para_size` 改为 8 后，需 8 个进程）：

```bash
mpirun -n 8 ./bin/multi_gpu_gpt_example
```

**需要观察的现象 / 预期结果**：

- 若 `tensor_para_size * pipeline_para_size` 不等于实际进程数（world_size），程序会在 `init_multiprocessing` 里报错退出（见 4.4.3 引用的 FT_CHECK）。
- 成功时，rank 0 会打印一行 `FT-CPP-decoding-beamsearch-time XX.XX ms` 的耗时，并写出输出 token（见 [multi_gpu_gpt_example.cc:275-284](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc#L275-L284)）。
- 这一步需要真实的 megatron 345M checkpoint（ini 里 `model_dir` 指向的路径），**待本地验证**：若没有 checkpoint，`loadModel` 会失败。

#### 4.3.5 小练习与答案

**练习 1**：为什么 GPT 示例用 ini 文件而不是像 BERT 那样用命令行参数？

> **答案**：GPT 的参数太多——模型结构（head_num/size_per_head/vocab_size/decoder_layers/inter_size/start_id/end_id）、并行规模（tensor_para_size/pipeline_para_size）、采样策略（beam_width/top_k/top_p/temperature/repetition_penalty/len_penalty）、请求参数（request_batch_size/request_output_len）等加起来几十个，命令行根本塞不下也不易维护。INI 文件还支持分段、注释，适合复杂配置。

**练习 2**：如果不传任何命令行参数直接运行 `./bin/multi_gpu_gpt_example`，ini 文件从哪里来？

> **答案**：见 [multi_gpu_gpt_example.cc:46-48](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc#L46-L48)，`argc < 2` 时 `ini_name` 取默认值 `../examples/cpp/multi_gpu_gpt/gpt_config.ini`。这就是为什么官方文档强调要在 `build/` 目录下运行——默认路径是相对于 `build/` 的。

---

### 4.4 配置读取：read_model_config / read_request_config

#### 4.4.1 概念说明

INI 文件是纯文本，但模型构造需要的是 C++ 结构体。中间的「翻译官」就是 `read_model_config` 和 `read_request_config`——它们用 `INIReader` 的 `GetInteger` / `GetFloat` / `GetBoolean` / `Get` 方法，把 ini 里的 key=value 逐条读进 `model_config_t` / `request_config_t` 两个结构体（结构体定义在 [gpt_example_utils.h:30-81](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_example_utils.h#L30-L81)）。

这两个结构体把配置清晰地分成两类：

- **model_config_t（模型相关，静态）**：模型名、权重路径、head_num、vocab_size、tensor_para_size……一次推理中不变。
- **request_config_t（请求相关，动态）**：batch_size、输出长度、采样参数、是否返回 log_probs……每次请求可能不同。

这种「模型配置 vs 请求配置」分离的设计，后来也体现在 Triton backend 的 Model / ModelInstance 分层里（见 [u10-l3 Triton backend](u10-l3-triton-backend.md)）。

#### 4.4.2 核心流程

以 `read_model_config` 为例：

```
read_model_config(reader)
   │
   ├─ 从 [ft_instance_hyperparameter] 读：model_name, model_dir, sparse, int8_mode,
   │      tensor_para_size, pipeline_para_size
   ├─ 从 [<model_name>] 读：head_num, size_per_head, vocab_size, decoder_layers, inter_size
   │      （注意 section 名是变量 model_name，所以同一份 ini 可放多个模型定义）
   ├─ 计算 hidden_units = head_num * size_per_head
   ├─ FT_CHECK: head_num % tensor_para_size == 0
   │ FT_CHECK: decoder_layers % pipeline_para_size == 0
   ├─ 读 model_variant（opt-pre / bloom-pre / gpt…）填充 gpt_variants
   └─ 读 prompt_learning 相关可选字段
```

#### 4.4.3 源码精读

**读全局超参与模型结构**——注意 `reader.Get(config.model_name, "head_num")` 这一行：section 名是**变量** `model_name`（如 `megatron_345M`），不是固定字符串。这就是为什么 [gpt_config.ini](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_config.ini) 里同时定义了 `[gpt_124M]`、`[megatron_345M]`、`[bloom_560M]` 等多个 section，但程序只会读「当前选中的那个」：

[examples/cpp/multi_gpu_gpt/gpt_example_utils.cc:101-114](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_example_utils.cc#L101-L114) —— 从 ini 读模型结构参数，并派生 `hidden_units`。

**并行合法性校验**——这是新手最容易忽略但最关键的检查：头数必须能被张量并行数整除、层数必须能被流水并行数整除，否则切分会失败：

[examples/cpp/multi_gpu_gpt/gpt_example_utils.cc:116-117](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_example_utils.cc#L116-L117) —— `FT_CHECK(head_num % tensor_para_size == 0)` 与 `FT_CHECK(decoder_layers % pipeline_para_size == 0)`。

> 直觉解释：张量并行是把每个 attention 的 head 平均切到各卡上（所以 head_num 必须能被 TP 整除）；流水并行是把层平均切到各卡上（所以层数必须能被 PP 整除）。这两条是 FT 并行的硬约束，原理详见 [u7-l1 张量并行](u7-l1-tensor-parallel.md) 和 [u7-l2 流水并行](u7-l2-pipeline-parallel-mpi.md)。

**world_size 校验**——`init_multiprocessing` 里还有一条：进程总数必须等于 TP×PP：

[examples/cpp/multi_gpu_gpt/gpt_example_utils.cc:242-245](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_example_utils.cc#L242-L245) —— 校验 `tensor_para_size * pipeline_para_size == world_size`。

**读请求配置**——从 `[request]` 和 `[ft_instance_hyperparameter]` 两个 section 读出采样、batch、输出长度等。注意 `repetition_penalty` 与 `presence_penalty` 互斥的校验：

[examples/cpp/multi_gpu_gpt/gpt_example_utils.cc:174-194](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_example_utils.cc#L174-L194) —— 读请求配置，含 `request_batch_size` 与 `request_output_len`。

**ini ↔ 结构体对照**——把 [gpt_config.ini](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_config.ini) 默认配置翻译成结构体值：

| ini 行 | key | 默认值 | 落到哪个字段 | 含义 |
| --- | --- | --- | --- | --- |
| L10 | `tensor_para_size` | 1 | model_config.tensor_para_size | 张量并行卡数 |
| L11 | `pipeline_para_size` | 1 | model_config.pipeline_para_size | 流水并行节点数 |
| L12 | `data_type` | fp16 | （main 里直接分发） | 计算精度 |
| L17 | `model_name` | megatron_345M | model_config.model_name | 选中的模型段 |
| L28 | `model_dir` | ../models/.../1-gpu/ | model_config.model_dir | 权重目录 |
| L66-73 | `[megatron_345M]` 段 | head=16,size=64... | model_config 各结构字段 | 模型结构 |
| L34 | `request_batch_size` | 8 | request_config.request_batch_size | 请求 batch |
| L35 | `request_output_len` | 32 | request_config.request_output_len | 生成多少 token |

> 一个细节：[gpt_config.ini:114](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_example_utils.cc#L114) 读取 `inter_size` 时带默认值 `4 * hidden_units`——即如果某个模型段没写 `inter_size`，就自动用 4 倍 hidden。这是 `reader.GetInteger(section, key, default)` 三参重载的用法。

#### 4.4.4 代码实践

**实践目标**：通过修改 `gpt_config.ini` 体会「配置驱动」的运行方式。

**操作步骤**（源码阅读 + 思想实验）：

1. 打开 [gpt_config.ini](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_config.ini)。
2. 找到 L17 `model_name=megatron_345M`，假设改成 `model_name=gpt_124M`（去掉 L16 行首的 `;` 注释，并注释掉 L17）。
3. 追踪：改完后，`read_model_config` 在 [gpt_example_utils.cc:109-112](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_example_utils.cc#L109-L112) 就会去读 `[gpt_124M]` 段（[gpt_config.ini:41-51](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_config.ini#L41-L51)），于是 head_num 从 16 变 12、decoder_layers 从 24 变 12、vocab_size 从 50304 变 50257。
4. 再把 L10 `tensor_para_size` 改成 2，思考会发生什么。

**需要观察的现象 / 预期结果**：

- 改 `model_name` 后，整套模型结构参数跟着变——这体现了「同一份示例代码 + 不同 ini 段 = 不同模型」的复用能力。
- 把 `tensor_para_size` 改成 2 后，必须用 `mpirun -n 2` 启动（world_size=2），否则 [gpt_example_utils.cc:242-245](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_example_utils.cc#L242-L245) 报错；同时 `gpt_124M` 的 head_num=12 能被 2 整除，校验通过。若改成 `tensor_para_size=5`，则因 12%5≠0 在 [L116](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_example_utils.cc#L116) 触发 FT_CHECK 失败。
- **待本地验证**：实际跑通需要对应 TP 切分的 checkpoint 目录（如 `2-gpu/`）。

#### 4.4.5 小练习与答案

**练习 1**：`read_model_config` 是怎么做到「一份 ini 描述多个模型」的？

> **答案**：它先从固定段 `[ft_instance_hyperparameter]` 读出 `model_name`（如 `megatron_345M`），然后把 `model_name` 当作 section 名去读结构参数（`reader.Get(config.model_name, "head_num")`）。所以 ini 里可以并列写 `[gpt_124M]`、`[megatron_345M]`、`[bloom_560M]` 多个段，切换模型只需改 `model_name=` 一行。

**练习 2**：`request_config_t` 和 `model_config_t` 为什么要分开？

> **答案**：模型配置（结构、权重、并行规模）在一次部署里基本不变；而请求配置（batch、输出长度、采样参数、是否返回 log_probs）每次请求都可能不同。把它们分到两个结构体，既符合「静态 vs 动态」的现实语义，也为后续 Triton backend「Model 加载一次 / ModelInstance 处理每次请求」的分层打下基础。

---

## 5. 综合实践

**任务**：以「新增一个最小可运行的 GPT 配置」为目标，把本讲 4 个模块串起来。

假设你想用一个虚构的 `self_defined` 模型（[gpt_config.ini:173-180](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_config.ini#L173-L180) 已经留了这段）跑通 GPT 示例。请完成：

1. **改 ini**：把 [gpt_config.ini:17](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_config.ini#L17) 的 `model_name` 改为 `self_defined`，并把 `model_dir` 指向你准备的权重目录（没有就标注「待准备」）。
2. **追踪配置流**：对照源码写出这条 `model_name` 在程序里的流转路径——
   - `main` 读 ini 拿到 `data_type`（[multi_gpu_gpt_example.cc:63](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc#L63)）→
   - 进入 `multi_gpu_gpt_example<T>` →
   - `read_model_config` 把 `model_name="self_defined"` 读进 `model_config_t`（[gpt_example_utils.cc:101](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_example_utils.cc#L101)）→
   - 再用该名字读 `[self_defined]` 段拿到 head=16、size=64、vocab=30000 等（[gpt_example_utils.cc:109-114](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_example_utils.cc#L109-L114)）→
   - 用这些值构造 `ParallelGptWeight` 与 `ParallelGpt`（[multi_gpu_gpt_example.cc:124-198](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc#L124-L198)）→
   - 最后 `gpt.forward(...)`。
3. **校验**：确认 `self_defined` 的 head_num=16、decoder_layers=12 在 `tensor_para_size=1`、`pipeline_para_size=1` 下能通过 [gpt_example_utils.cc:116-117](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/gpt_example_utils.cc#L116-L117) 的两条 FT_CHECK（显然能通过：16%1=0，12%1=0）。
4. **写出启动命令**：因为是 TP=PP=1，直接 `./bin/multi_gpu_gpt_example` 即可（在 `build/` 下）。

**预期结果**：你能画一张「ini 一行改动 → 配置结构体 → 模型对象 → forward」的完整流转图，并解释沿途每个环节读的是 ini 的哪一段、校验了什么。这就是「配置驱动推理」的全貌。

> 说明：本实践不要求真正下载权重——重点是走通「配置 → 代码」的映射链路。若要实跑，需要先用 `examples/pytorch/gpt` 下的转换脚本把 HuggingFace/Megatron checkpoint 转成 FT 的 `c-model` 格式并放到 `model_dir`。**待本地验证**。

---

## 6. 本讲小结

- FT 的 C++ 示例有**两种入口范式**：BERT 用命令行参数（`argv`），多 GPU GPT 用 INI 配置文件 + MPI。
- `bert_example` 的 `main` 校验 `argc==8`（7 个参数），用 `atoi` 解析，再用「枚举 → 模板」分发到 `bertExample<float/half/__nv_bfloat16>`；数据类型映射是 `0=FP32, 1=FP16, 2=BF16`。
- `bertExample<T>` 演示了调用 FT 模型的**通用四步**：建资源 → 建权重 → 建模型 → 组装张量并 `forward`；并示范了「warmup + 计时 100 次取平均」的标准测速范式。
- `multi_gpu_gpt_example` 的 `main` 先 `mpi::initialize`，再用 `INIReader` 读 ini，按字符串 `data_type` 分发；模板函数里通过 `read_model_config`/`read_request_config` 把 ini 翻译成结构体。
- 配置被清晰分成**模型配置**（静态：结构、权重、并行）与**请求配置**（动态：batch、采样、输出长度）两类，对应 `model_config_t` / `request_config_t`。
- 并行有两条硬约束：`head_num % tensor_para_size == 0`、`decoder_layers % pipeline_para_size == 0`，且 `tensor_para_size * pipeline_para_size == world_size`（进程数）。
- 无论模型多复杂，调用 model 的接口形式统一为 `model.forward(&output_tensors, &input_tensors, &weights)`——这是 FT 设计上的统一性。

---

## 7. 下一步学习建议

本讲只走到了「示例如何把参数喂给模型」，**没有进入 `forward` 内部**。建议接下来：

1. **想真正跑起来** → 回到 [u1-l2 构建系统](u1-l2-build-system.md)，按 `docs/bert_guide.md` / `docs/gpt_guide.md` 的 Build 章节编译，并准备对应的 checkpoint。
2. **理解 `forward` 的输入输出载体** → 学 [u2-l1 统一张量抽象 Tensor/TensorMap/DataType](u2-l1-tensor-data-structure.md)，搞清本讲里反复出现的 `Tensor{MEMORY_GPU, getTensorType<T>(), ...}` 到底是什么。
3. **理解 BERT forward 内部** → 学 [u4-l1 BERT 模型与 forward 主流程](u4-l1-bert-model.md)，看 `bert.forward` 里那 8 个 GEMM 和 6 个 kernel 是怎么排布的。
4. **理解 GPT forward 与多 GPU** → 学 [u6-l1 ParallelGpt 架构](u6-l1-parallel-gpt.md) 和 [u7-l1/u7-l2 并行与分布式](u7-l1-tensor-parallel.md)，把本讲里的 `tensor_para` / `pipeline_para` / MPI 讲透。
5. **想看更多示例变体** → 浏览 `examples/cpp/multi_gpu_gpt/` 下的 `multi_gpu_gpt_interactive_example.cc`（交互式）和 `multi_gpu_gpt_async_example.cc`（异步），它们的入口结构与本讲相同，可作对照阅读。
