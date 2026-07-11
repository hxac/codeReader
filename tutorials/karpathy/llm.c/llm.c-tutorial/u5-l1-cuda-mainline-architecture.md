# CUDA 主线架构与 llmc 头文件库

## 1. 本讲目标

本讲是「CUDA 主线 train_gpt2.cu 与 llmc 头文件库」单元的第一篇，目标是为后续所有 CUDA 讲义建立一张**架构地图**。读完本讲你应该能够：

1. 看懂 `train_gpt2.cu`（约 1900 行）如何通过一组 `llmc/` 头文件把「数据加载、层算子、多卡通信」模块化拼装成一个完整训练器。
2. 说清楚 `floatX` 这个类型别名是怎样由 Makefile 的 `PRECISION` 变量，经过 `-DENABLE_*` 宏，在编译期决定整份代码用 FP32 / FP16 / BF16 中的哪一种。
3. 理解 CUDA 版的 `ParameterTensors` / `ActivationTensors` 与 CPU 参考实现的异同，以及为什么部分缓冲区（mean/rstd/losses）永远是 `float` 而不是 `floatX`。
4. 掌握 `TensorSpec` 机制：它如何让一次 `cudaMalloc` 同时容纳**混合精度**的激活，以及 `recompute` 选项如何用「换算力省显存」的方式压缩激活缓冲。

本讲**只看骨架与装配**，不展开任何单个 kernel 的内部实现——那是 u5-l2 之后各篇的任务。

## 2. 前置知识

阅读本讲前，请确认你已理解以下内容（对应前置讲义）：

- **u3-l1 反向组装**：前向算子与反向算子严格镜像，梯度一律 `+=` 累加并依赖每步清零。本讲的 `ActivationTensors` 正是「前向要把所有中间结果存下来供反向复用」这一约束的直接产物。
- **u4-l3 GPU fp32 legacy 版**：CUDA 的 grid/block/thread 三层模型、`cudaMalloc` / `cudaMemcpy` 的 host↔device 显存搬运，以及「只换执行器、不换数学」的 CUDA 化原则。本讲把这些思路升级到**工程化、模块化、混合精度**的主线版本。

此外，几个贯穿全讲的术语：

| 术语 | 含义 |
|------|------|
| `floatX` | 一个**编译期类型别名**，等于 `float` / `half` / `__nv_bfloat16` 三选一，由精度宏决定。 |
| B / T / C / V / Vp / L / NH | batch、序列长、通道数、真实词表、填充词表、层数、注意力头数（与 CPU 讲义一致）。 |
| 激活（activation） | 前向产生的中间张量，需要保存到反向。 |
| `recompute` | 用「反向时重算前向」来减少激活显存的策略，取值 0 / 1 / 2。 |
| `TensorSpec` | 本讲引入的核心数据结构，把一个缓冲的「指针、元素数、数据类型」三元组打包。 |

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [train_gpt2.cu](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu) | CUDA 主线训练器：定义 `GPT2Config` / `ParameterTensors` / `ActivationTensors` / `TensorSpec` / `GPT2` 结构体，装配前向、反向、更新，以及 `main` 训练循环。 |
| [llmc/cuda_common.h](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_common.h) | CUDA 公共定义：`floatX` 精度宏、`WARP_SIZE` / `CEIL_DIV` / `cudaCheck`、文件↔显存搬运。 |
| [llmc/cuda_utils.cuh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_utils.cuh) | 设备端工具：`Packed128`、`DType` 枚举、`sizeof_dtype` / `dtype_of`、warp/block 归约、随机舍入。 |
| [Makefile](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile) | 把 `PRECISION` 变量翻译成 `-DENABLE_*` 编译宏，驱动 `floatX` 的选择。 |

`llmc/` 目录下一共 23 个头文件（`.h` 是纯 host 工具，`.cuh` 含 CUDA 设备代码）。本讲只关注**它们如何被 `train_gpt2.cu` 组织进来**；逐个 kernel 留给后续讲义。

## 4. 核心概念与源码讲解

### 4.1 头文件库组织与精度宏

#### 4.1.1 概念说明

`train_gpt2.cu` 虽然有 1900 行，但真正「干活」的层算子（matmul、attention、layernorm……）并不写在这个文件里，而是分布在 `llmc/` 的一组头文件中。`train_gpt2.cu` 的角色更像**总装车间**：它 `#include` 一堆头文件，把每层算子按顺序调用，再加上结构体定义、显存管理和训练循环。

这种「头文件库」写法的好处是：

- 每个头文件专注一层或一类工具，方便单独阅读、单独优化（后续 u7 的 `dev/cuda` 内核库正是对它们的逐层深挖）。
- 同一份头文件既能被主线 `train_gpt2.cu` 用，也能被测试 `test_gpt2.cu`、剖析 `profile_gpt2.cu` 复用，避免代码重复。

而贯穿这一切的「粘合剂」是 `floatX`——一个让整份代码对精度**无感**的类型别名。CPU 参考实现 `train_gpt2.c` 全程用 `float`；CUDA 主线把 `float` 换成 `floatX`，于是同一套源码只要改一个编译宏，就能在 FP32 / FP16 / BF16 之间切换，无需改动任何算子代码。

#### 4.1.2 核心流程

精度切换的链路是**纯编译期**的，运行时没有任何分支开销：

```text
Makefile: PRECISION ?= BF16        (默认 BF16，可选 FP32 / FP16)
   │  ifeq 映射
   ▼
PFLAGS = -DENABLE_FP32 / -DENABLE_FP16 / -DENABLE_BF16
   │  nvcc 命令行 (train_gpt2cu 目标)
   ▼
cuda_common.h 顶部的 #if defined(ENABLE_*)
   │  typedef
   ▼
floatX = float | half | __nv_bfloat16
   │  流入所有结构体与 kernel
   ▼
ParameterTensors / ActivationTensors 里的指针全是 floatX*
```

关键直觉：`floatX` 不是一个变量，而是 `#if` 选出的 `typedef`。预处理结束后，`floatX` 已经被替换成具体类型，编译器看到的就是一份针对该精度完全展开的代码。

#### 4.1.3 源码精读

`train_gpt2.cu` 顶部的 `#include` 分成**四个语义组**，注释也贴心地用分隔线划开。第一组是 host 端 CPU 工具（数据、采样、日志等），与 `train_gpt2.c` 共用：

[train_gpt2.cu:15-32](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L15-L32) 引入 tokenizer、dataloader、rand、schedulers、sampler、logger、mfu、outlier_detector——这些是「训练工程」周边设施，不含 CUDA kernel。

第二组是 GPU 基础设施，本讲的主角之一：

[train_gpt2.cu:37-44](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L37-L44) 引入 `cuda_common.h`（精度宏、`cudaCheck`、`CEIL_DIV`）、`cuda_utils.cuh`（`Packed128`、warp 归约、`DType`）、`cublas_common.h`（cuBLAS handle）。

第三组是**各层 CUDA 实现**：

[train_gpt2.cu:47-64](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L47-L64) 注意 attention 的二选一：若编译时定义了 `ENABLE_CUDNN`（由 Makefile 的 `USE_CUDNN=1` 触发），就 include `cudnn_att.h` 走 cuDNN Flash Attention；否则 include `attention.cuh` 走手写 kernel。这是本讲能看到的第二个「编译期切换」。

第四组是多卡支持：

[train_gpt2.cu:71](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L71) 引入 `zero.cuh`，定义 ZeRO 分片与 NCCL 通信（u6 展开）。

而精度的真正定义在 `cuda_common.h`。这是本讲最重要的一小段代码：

[cuda_common.h:82-92](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_common.h#L82-L92) 三个分支分别对应三种 `ENABLE_*` 宏；**注意 else 分支（默认）才是 BF16**，所以即便你忘了传 `PRECISION`，默认就是 `__nv_bfloat16`。同时每个分支还顺便定义了一个 `PRECISION_MODE` 枚举值，供运行时日志打印用。

```c
#if defined(ENABLE_FP32)
typedef float floatX;
#define PRECISION_MODE PRECISION_FP32
#elif defined(ENABLE_FP16)
typedef half floatX;
#define PRECISION_MODE PRECISION_FP16
#else // Default to bfloat16
typedef __nv_bfloat16 floatX;
#define PRECISION_MODE PRECISION_BF16
#endif
```

宏从哪来？看 Makefile：

[Makefile:233-243](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L233-L243) `PRECISION` 默认 `BF16`，校验合法性后映射到 `PFLAGS`（`-DENABLE_*`）。

[Makefile:273-274](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L273-L274) `train_gpt2cu` 目标把 `$(PFLAGS)` 传给 nvcc，于是宏进入了 `cuda_common.h`，`floatX` 的 typedef 生效。

> 小贴士：`half` 与 `__nv_bfloat16` 都是 CUDA 提供的 16 位浮点类型，前者数值范围小、精度略高；后者（BF16）范围与 FP32 相当、精度略低，更适合深度学习训练。它们的具体取舍会在 u6-l1 混合精度一篇详述。

#### 4.1.4 代码实践

**实践目标**：亲手梳理头文件库的组织方式，并验证 `floatX` 切换机制。

**操作步骤**：

1. 打开 `train_gpt2.cu` 第 4–71 行，把所有 `#include "llmc/..."` 摘出来，按下表分类（把「定义了什么」那一列对照注释补全）：

   | 类别 | 头文件 | 负责什么 |
   |------|--------|----------|
   | CPU 工具 | utils.h / tokenizer.h / dataloader.h / rand.h / schedulers.h / sampler.h / logger.h / mfu.h / outlier_detector.h | （自己填） |
   | GPU 工具 | cuda_common.h / cuda_utils.cuh / cublas_common.h | （自己填） |
   | 层算子 | encoder.cuh / layernorm.cuh / matmul.cuh / attention.cuh 或 cudnn_att.h / fused_classifier.cuh / adamw.cuh / global_norm.cuh | （自己填） |
   | 多卡 | zero.cuh | （自己填） |

2. 用一条命令验证 `PRECISION → -DENABLE_*` 的映射（只看预处理结果，不实际编译）：
   ```bash
   make -n train_gpt2cu PRECISION=FP32 | grep -- -DENABLE
   make -n train_gpt2cu PRECISION=BF16 | grep -- -DENABLE
   ```

**需要观察的现象**：`make -n`（dry-run）会打印出 nvcc 命令行；`PRECISION=FP32` 时应出现 `-DENABLE_FP32`，`PRECISION=BF16`（或不传）时应出现 `-DENABLE_BF16`。

**预期结果**：命令行里的 `-DENABLE_*` 与 `cuda_common.h:82-92` 的 `#if` 分支一一对应，从而 `floatX` 分别被 typedef 成 `float` 与 `__nv_bfloat16`。若环境没有 GPU/nvcc，`make -n` 仍能展开命令行（它只是打印不执行），此步可作「源码阅读型验证」；实际编译链接则**待本地验证（需要 CUDA 环境）**。

#### 4.1.5 小练习与答案

**练习 1**：如果同时定义了 `ENABLE_FP32` 和 `ENABLE_BF16`，`floatX` 会是什么？为什么？

**答案**：会是 `float`（FP32）。因为 [cuda_common.h:82-92](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_common.h#L82-L92) 用 `#if defined(ENABLE_FP32) #elif ... #else ...` 结构，第一个命中的分支生效，FP32 排在最前。Makefile 的 `ifeq` 链只会赋一个 `PFLAGS`，不会同时定义两个宏，所以正常构建不会出现这种冲突。

**练习 2**：为什么 `attention` 的两个实现用 `#ifdef ENABLE_CUDNN ... #else ... #endif` 在**编译期**二选一，而不是运行时用 `if` 判断？

**答案**：因为 `llmc/cudnn_att.cpp` 与 `llmc/attention.cuh` 依赖的库（cuDNN）并非所有环境都有。编译期切换可以让没有 cuDNN 的机器直接编译手写版，既省去运行时分支，也避免链接缺失符号（见 [train_gpt2.cu:52-58](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L52-L58)）。

---

### 4.2 CUDA 版参数 / 激活结构体

#### 4.2.1 概念说明

你已经在 u1-l3 见过 CPU 参考实现的「一次性 `malloc` + 指针排布」技巧：把所有参数张量塞进一整块连续内存，再用一个指针数组把它们「钉」到各自的偏移上。CUDA 主线继承了完全相同的思路，只是：

- 内存从 host 的 `malloc` 换成 device 的 `cudaMalloc`；
- 张量类型从 `float*` 换成 `floatX*`（精度可变）；
- 激活张量的**种类**与 CPU 版略有不同——CUDA 版为了融合算子和省显存做了一些调整。

这里要特别注意一个细节：**并非所有激活都跟着 `floatX` 走**。像 LayerNorm 的均值/方差倒数（mean/rstd）、loss 这些「统计量」，即便权重和激活是 BF16，它们也仍然存成 `float`。原因是这些量会参与除法、指数等数值敏感运算，低精度会放大误差。于是 CUDA 版的结构体天然是**混合精度**的，这恰好引出下一节 4.3 的 `TensorSpec`。

#### 4.2.2 核心流程

参数与激活的分配时机不同（沿用了 CPU 版的「懒分配」哲学）：

```text
gpt2_allocate_weights()          ← 读 checkpoint 后立即调用
   fill_in_parameter_sizes()     ← 算出 16 个张量各自的元素数
   malloc_and_point_parameters() ← 一次 cudaMalloc，排布 16 个 floatX* 指针

gpt2_allocate_state(B, T)        ← 第一次前向/反向时才调用（依赖运行时 B、T）
   malloc_and_point_parameters() ← 再分配一份同样大小的「梯度」(grads)
   fill_in_activation_sizes()    ← 算出 21 个激活张量的规格（见 4.3）
   malloc_and_point_activations()← 一次 cudaMalloc，排布 21 个指针
```

「懒分配」的原因：参数形状只依赖 config（编译/加载时已知），而激活形状依赖 batch_size B 与 seq_len T（运行时才知道），所以激活必须推迟到第一次见到真实 batch 时才分配。

#### 4.2.3 源码精读

`GPT2Config` 与 CPU 版完全一致，6 个字段决定一切形状：

[train_gpt2.cu:87-94](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L87-L94) GPT-2 124M 对应 1024 / 50257 / 50304 / 12 / 12 / 768。

参数结构体——注意所有指针都是 `floatX*`，并且最后有一句 `static_assert`：

[train_gpt2.cu:98-116](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L98-L116) `NUM_PARAMETER_TENSORS = 16`，16 个权重张量（含权重绑定的 `wte`）。

```c
constexpr const int NUM_PARAMETER_TENSORS = 16;
typedef struct {
    floatX* wte;     // (V, C)
    floatX* wpe;     // (maxT, C)
    ...
    floatX* lnfb;    // (C)
} ParameterTensors;
static_assert(sizeof(ParameterTensors) == NUM_PARAMETER_TENSORS * sizeof(void*),
              "Inconsistent sizes!");
```

> 这句 `static_assert` 是**精度宏的配套一致性检查**：无论 `floatX` 是哪种 16 位或 32 位类型，`floatX*` 永远是一个指针（大小固定为 `sizeof(void*)`）。如果有人不小心把某个成员写成 `float` 而非 `floatX*`，结构体大小就会变化，编译期直接报错。这是一种防止「精度改动悄悄破坏内存布局」的护栏。

`fill_in_parameter_sizes` 列出 16 个张量的元素数（顺序与结构体字段一一对应）：

[train_gpt2.cu:118-144](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L118-L144) 第 141-143 行还顺手把每个张量的 `param_sizeof`（每元素字节数）都填成 `sizeof(floatX)`——所有参数同精度，所以这里是个统一的循环。

`malloc_and_point_parameters` 就是 CPU 版技巧的 GPU 翻译：

[train_gpt2.cu:147-168](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L147-L168) 先累加总字节数 → `cudaMalloc` 一整块 → 用 `char*` 迭代器把 16 个 `floatX*` 指针依次钉到对应偏移。返回的是这块 device 内存的首地址，后续 `cudaMemcpy` 装填权重时一次 `fread` + H2D 即可让所有指针就位。

激活结构体是本节的重点。它有 **21** 个成员（CPU 版是 23 个）：

[train_gpt2.cu:170-207](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L170-L207) 请重点观察三类成员：

1. **跟随 `floatX` 的主激活**：`encoded` / `ln1` / `atty` / `residual2` / `ln2` / `fch` / `fch_gelu` / `residual3` / `lnf` / `qkvr` / `output`。
2. **永远是 `float` 的统计量**：`ln1_mean` / `ln1_rstd` / `ln2_mean` / `ln2_rstd` / `lnf_mean` / `lnf_rstd` / `losses`。这就是「混合精度」的由来。
3. **CUDA 版新增的 scratch**：`qkvr`（注意注释 195-201 行，`output` 被设计成可复用的大 scratchpad，容量取 `3C`、`NH*T`、`Vp` 三者的最大值，供 attention / fcproj / logits 投影轮流使用）、`scratch_bt4c` / `scratch_btc`。

> 为什么 CUDA 版是 21 而不是 CPU 版的 23？因为 CUDA 版把若干「只在前向用一下、反向不需要」的中间量（如 attention 内部的 `preatt`、非 cuDNN 路径下的 `qkvi`）做成了**就地 scratch**（统一塞进 `output`），不再单独列结构体成员；同时又**新增**了 `qkvr`、`scratch_bt4c`、`scratch_btc` 三个为 kernel 服务的缓冲。两相抵消，净成员数从 23 变为 21。另外，启用 cuDNN 时 `att` 从 `(L,B,NH,T,T)` 大矩阵缩成 `(L,B,NH,T)` 的小统计张量（[train_gpt2.cu:178-182](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L178-L182)），可见结构体本身也会随编译选项微调。

把参数与各种状态打包在一起的顶层结构体：

[train_gpt2.cu:285-322](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L285-L322) `GPT2` 把 config、params、grads、AdamW 的 `m_memory`/`v_memory`/`master_weights`、激活 `acts` 及其 `acts_specs`、运行时 B/T、loss 缓冲、以及一串控制开关（`use_master_weights` / `gelu_fusion` / `recompute`）全部聚到一处。注意第 300 行 `master_weights` 注释「is NULL unless fp32 weights is enabled」——这是 u6-l1 混合精度的伏笔。

#### 4.2.4 代码实践

**实践目标**：对照 CPU 版与 CUDA 版的激活结构体，理解「为什么 CUDA 版是混合精度」。

**操作步骤**：

1. 同时打开 `train_gpt2.c`（CPU 版 `ActivationTensors`）与 `train_gpt2.cu:170-207`（CUDA 版）。
2. 找出 CUDA 版里所有类型为 `float*`（而非 `floatX*`）的成员，数一数有几个。

**需要观察的现象**：这些 `float*` 成员的共同点是它们都是**统计量或损失**，而非「会反向传播的激活」。

**预期结果**：CUDA 版中 `ln1_mean` / `ln1_rstd` / `ln2_mean` / `ln2_rstd` / `lnf_mean` / `lnf_rstd` / `losses` 共 7 个成员是 `float`（启用 cuDNN 时 `att` 也是 `float`）。即使 `PRECISION=BF16`，这些缓冲仍按 4 字节分配，从而保证 LayerNorm 与 loss 的数值精度。这一观察将直接解释下一节 `TensorSpec` 为什么必须记录 `DType`。

#### 4.2.5 小练习与答案

**练习 1**：`static_assert(sizeof(ParameterTensors) == NUM_PARAMETER_TENSORS * sizeof(void*))` 这条检查，在什么情况下会失败？

**答案**：当结构体里混入了非指针成员，或某成员写成了 `floatX`（值类型）而非 `floatX*`（指针）时，结构体大小就不再是「16 × 指针大小」，编译期断言失败。它专门防止「精度改动时有人误把指针写成值」这类低级但致命的错误。

**练习 2**：为什么 `losses` 是 `float` 而 `encoded` 是 `floatX`？

**答案**：`losses` 要在 B·T 个位置上累加成 `mean_loss`，并参与交叉熵 `-log(p)` 计算，低精度累加会有明显误差；`encoded` 只是 token/position embedding 的相加结果，后续会进 LayerNorm 重新归一化，对精度不那么敏感，因此跟随 `floatX` 以省显存。这是「精度预算」的典型分配。

---

### 4.3 TensorSpec 与激活分配（含 recompute）

#### 4.3.1 概念说明

4.2 节留下一个工程难题：激活结构体里**既有 `floatX*` 又有 `float*`**，而我们要把它们塞进同一次 `cudaMalloc`。CPU 版没有这个问题（全是 `float*`，直接按元素数 `× 4` 字节累加即可）。CUDA 版需要一种机制，能**逐张量记录它的数据类型**，再据此换算字节数。

这个机制就是 `TensorSpec`：一个把「指向该缓冲的指针、元素数、数据类型」三元组打包的小结构。配合 `dtype_of` / `sizeof_dtype` 两个工具函数，就能在统一的循环里算出总字节数并排布指针。

本节还要讲一个与显存息息相关的选项 `recompute`（梯度检查点 / activation checkpointing 的一种变体）。直觉是：前向算出来的某些激活，**不在显存里存着**，而是等反向需要时**临时重算一遍**。代价是多花一次前向计算的时间，收益是大幅减少显存占用，从而能塞下更大的 batch。`recompute` 取三档：

| recompute | 含义 | 省掉的激活 |
|-----------|------|------------|
| 0 | 全不重算（最省算力、最费显存） | 无 |
| 1 | 反向时重算 GELU（默认） | `fch_gelu` 从 L 份缩成 1 份 |
| 2 | 再重算 LayerNorm（最省显存、最费算力） | `ln1`/`ln2` 直接不分配，复用 `lnf` |

#### 4.3.2 核心流程

`TensorSpec` 驱动的分配流程：

```text
fill_in_activation_sizes(acts, specs[], B, T, config, recompute)
   │  对 21 个张量，逐个 TENSOR_SPEC(ptr, 元素数)
   │  其中元素数随 recompute 变化（ln1/ln2/fch_gelu）
   ▼
malloc_and_point_activations(specs[])
   total_bytes = Σ specs[i].size × sizeof_dtype(specs[i].type)   ← 混合精度换算
   cudaMalloc(total_bytes)                                        ← 一次分配
   cudaMemset(0)                                                  ← 清零（attention 依赖）
   for 每个张量: *(specs[i].ptr) = 当前偏移; 偏移 += 字节数        ← 排布指针
```

而 `recompute` 在前向/反向里是这样起作用的（以 GELU 为例）：

```text
recompute == 0：前向为每层 l 写 fch_gelu[l]，反向直接读 → 需 L 份显存
recompute >= 1：前向所有层复用同一个 fch_gelu 缓冲（只 1 份），
              反向到第 l 层时，临时 gelu_forward 重算该层的 fch_gelu
              → 省 (L-1) 份显存，但反向多算 L 次 gelu
```

#### 4.3.3 源码精读

先看 `DType` 与两个换算函数（它们是 `TensorSpec` 的基础）：

[cuda_utils.cuh:86-108](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_utils.cuh#L86-L108) `DType` 是个三值枚举；`sizeof_dtype` 把枚举换算成字节数（FP32=4，FP16/BF16=2）；`dtype_of` 用**三个重载**，根据传入指针的静态类型返回对应 `DType`。重载是编译期决议的——传 `floatX*`（无论 floatX 是哪种）和传 `float*` 会分别命中不同重载。

`TensorSpec` 与构造它的宏：

[train_gpt2.cu:210-217](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L210-L217) `ptr` 存的是「指向缓冲指针的指针」（`void**`），这样分配函数才能回填。`TENSOR_SPEC` 宏用 `dtype_of(pointer)` 自动推断类型——传 `acts.encoded`（`floatX*`）就推断成当前精度，传 `acts.losses`（`float*`）就推断成 FP32。

```c
struct TensorSpec {
    void** ptr;     // 指向「该缓冲指针」的地址，用于回填
    size_t size;    // 元素数（不是字节数！）
    DType type;     // 该缓冲的数据类型
};
#define TENSOR_SPEC(pointer, size) \
    TensorSpec{(void**)(&pointer), (size), dtype_of(pointer)};
```

`fill_in_activation_sizes` 把 21 个张量逐个登记，并在三个位置根据 `recompute` 改写元素数：

[train_gpt2.cu:219-254](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L219-L254) 重点看这三行（`recompute` 的核心）：

```c
tensors[1]  = TENSOR_SPEC(data->ln1,     (recompute < 2) ? L*B*T*C   : 0);           // LN 重算
tensors[7]  = TENSOR_SPEC(data->ln2,     (recompute < 2) ? L*B*T*C   : 0);           // LN 重算
tensors[11] = TENSOR_SPEC(data->fch_gelu,(recompute < 1) ? L*B*T*4*C : B*T*4*C);     // GELU 重算
```

- `recompute < 2` 为假（即 `recompute >= 2`）时，`ln1`/`ln2` 的元素数直接取 0——不分配；反向改用唯一的 `lnf` 缓冲（见下方前向代码）。
- `recompute < 1` 为假（即 `recompute >= 1`）时，`fch_gelu` 只申请 **1 份**（`B*T*4*C`）而非 **L 份**（`L*B*T*4*C`）；反向到每层时重算。
- 另注意 `tensors[18]` 的 `output`：[train_gpt2.cu:250](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L250) 取 `max(3*C, max(NH*T, Vp))`，因为它要在不同阶段轮换承担 qkv 投影、attention scratch、logits 投影三种形状，按最大者开。

`malloc_and_point_activations` 是统一的分配-排布函数：

[train_gpt2.cu:256-283](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L256-L283) 关键是第 259 行用 `sizeof_dtype(tensors[i].type)` 把「元素数」换算成「字节数」，于是 `floatX` 张量与 `float` 张量在同一循环里正确累加。第 270 行的 `cudaMemset(..., 0)` 不可省——非 cuDNN 的 attention 假设 `att` 缓冲初始为 0（注释 267-269 行说明了这点）。第 275-276 行还有一个保护：元素数为 0 的张量（`recompute>=2` 时的 `ln1`/`ln2`）会被显式置成 `NULL`，防止误用。

`recompute` 默认值在初始化里设为 1：

[train_gpt2.cu:347-349](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L347-L349) `recompute = 1` 是「重算 GELU 但不重算 LN」的折中默认值。

前向如何根据 `recompute` 选择「写到分层缓冲还是共享缓冲」：

[train_gpt2.cu:683](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L683) 第一层 LayerNorm：`recompute >= 2` 时把结果写进 `acts.lnf`（而非 `acts.ln1`）。

[train_gpt2.cu:703-713](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L703-L713) 循环内：`l_ln1` 与 `l_ln2` 在 `recompute >= 2` 时都指向共享的 `acts.lnf`；`l_fch_gelu` 在 `recompute >= 1` 时指向唯一的 `acts.fch_gelu`（不再加 `l*B*T*4*C` 偏移）。注释 711-712 行明确道出动机：「reuse the same activation buffer at each layer ... dramatically reduce VRAM usage」。

反向则在需要时把重算补上：

[train_gpt2.cu:895-902](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L895-L902) `recompute >= 1` 时调用 `gelu_forward` 重算本层 GELU；`recompute >= 2` 时再重算 LayerNorm。这就是「换算力省显存」的代价所在。

> 复用 `lnf` 是否安全？安全——因为每层的 LayerNorm 在前向里**只被消费一次**（紧随其后的 matmul/attention 用完即可丢），反向时按层号 `l` 依次重算、用完，不会与别的层并发，所以一个缓冲轮流承载 L 层的 LN 结果没有数据竞争。`fch_gelu` 同理。

#### 4.3.4 代码实践

**实践目标**：定量感受 `recompute` 对激活显存的影响。

**操作步骤**：

1. 设 GPT-2 124M：`L=12, C=768, NH=12`，取 `B=4, T=512`，`PRECISION=BF16`（故 `floatX` 占 2 字节，`float` 占 4 字节，为简化估算可只算 `fch_gelu` 这一项）。
2. 用 `fch_gelu` 的元素数公式分别算 `recompute=0` 与 `recompute=1` 时该项的字节数：
   - `recompute=0`：`L * B * T * 4C` 个 `floatX`
   - `recompute=1`：`B * T * 4C` 个 `floatX`
3. 算出二者之差（即省下的显存），换算成 MiB。

**需要观察的现象**：`recompute` 从 0 调到 1，仅 `fch_gelu` 一项就省下 `(L-1) × B × T × 4C` 个 `floatX`。

**预期结果**：

- `4C = 4×768 = 3072`
- 单层 `fch_gelu` 元素数 = `B×T×4C = 4×512×3072 = 6,291,456`，BF16 下约 12 MiB
- `recompute=0` 需 12 层 ≈ 144 MiB；`recompute=1` 只需 1 层 ≈ 12 MiB；**仅这一项就省下约 132 MiB**。
- 验证手段：运行时 `malloc_and_point_activations` 会打印 `allocating N MiB for activations`（[train_gpt2.cu:262](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L262)），用 `-r 0` 与 `-r 1` 各跑一次，对比打印的 MiB 差值。实际运行**待本地验证（需要 GPU）**；上述手算可作为阅读型实践的预期对照。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `acts.losses`（类型 `float*`）登记成 `TENSOR_SPEC(data->losses, B*T)`，`dtype_of` 会返回什么？为什么这样设计是对的？

**答案**：返回 `DType::FP32`。因为 `losses` 成员的静态类型是 `float*`，命中 [cuda_utils.cuh:106](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_utils.cuh#L106) 的 `dtype_of(float*)` 重载。这样 `malloc_and_point_activations` 用 `sizeof_dtype(FP32)=4` 计算字节数，保证即便 `PRECISION=BF16`，losses 仍按 4 字节分配——这正是「混合精度」想要的。

**练习 2**：为什么 `recompute>=2` 时 `ln1`/`ln2` 的元素数取 0，而不是干脆从结构体里删掉这两个成员？

**答案**：因为结构体布局是编译期固定的（`floatX* ln1` 这个指针成员始终存在），而「是否真正分配显存、指针是否为 NULL」是运行期由 `recompute` 决定的。取 0 让分配循环把该指针置为 `NULL`（[train_gpt2.cu:275-276](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L275-L276)），前向/反向则改用共享的 `lnf` 缓冲。这样同一份源码能兼容三档 `recompute`，无需为每档单独维护结构体。

**练习 3**：`output` 缓冲的元素数为何取 `max(3*C, max(NH*T, Vp))` 而非三者之和？

**答案**：因为 `qkv 投影(3C)`、`attention scratch(NH*T*T)`、`logits 投影(Vp)` 这三种形状在**不同时间阶段**使用 `output`，彼此不重叠（前向注释 [train_gpt2.cu:196-201](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L196-L201)）。取最大者即可轮流复用，比取 sum 节省显存——这和 `fch_gelu` 跨层复用是同一类「时间复用」思想。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**源码阅读 + 显存预算**小任务：

**任务**：假设你要在一块显存有限的 GPU 上跑 GPT-2 124M（`L=12, C=768, NH=12, Vp=50304`），`B=4, T=1024`。

1. **选精度**：阅读 [Makefile:233-243](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L233-L243)，说明用 `make train_gpt2cu PRECISION=BF16` 时 `floatX` 是什么类型，并解释为什么低显存机器更该选 BF16 而非 FP32（提示：参数 + 梯度 + 优化器状态都按 `floatX` 算字节数）。
2. **算参数显存**：用 [train_gpt2.cu:118-144](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L118-L144) 的公式估算 16 类参数的总元素数（约 1.24 亿），分别按 BF16（2 字节）与 FP32（4 字节）算参数显存，体会差距。
3. **选 recompute**：阅读 [train_gpt2.cu:219-254](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L219-L254)，指出若把启动参数从 `-r 0` 改成 `-r 1`，`fch_gelu` 一项省下多少显存；再说明若继续改成 `-r 2`，`ln1`/`ln2` 又省下多少。
4. **验证**：用 `-r 0`、`-r 1`、`-r 2` 各跑一两步训练，记录程序打印的 `allocating N MiB for activations` 行，与你手算的差值核对。

**预期产出**：一张小表，列出「精度 × recompute」组合下的「参数显存 / 激活显存」估算与实测值，并据此给出「这块卡该用哪组配置」的建议。其中第 4 步**待本地验证（需要 GPU 与 starter pack）**；前三步纯源码阅读即可完成。

## 6. 本讲小结

- `train_gpt2.cu` 是「总装车间」，真正的层算子都在 `llmc/` 头文件里，按「CPU 工具 / GPU 工具 / 层算子 / 多卡」四组 include 进来，便于复用与单独优化。
- `floatX` 是编译期类型别名：Makefile 的 `PRECISION` → `-DENABLE_*` 宏 → [cuda_common.h:82-92](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_common.h#L82-L92) 的 `#if` 选定 `float`/`half`/`__nv_bfloat16`，使整份源码无需改动即可切换精度。
- `ParameterTensors` 沿用 CPU 版的「一次 `cudaMalloc` + 指针排布」技巧，`static_assert` 保证结构体大小与精度无关。
- `ActivationTensors` 是**混合精度**的：主激活跟 `floatX`，而 mean/rstd/losses 等统计量恒为 `float`，CUDA 版还引入了 `output` / `scratch_*` 等可复用 scratch。
- `TensorSpec{ptr, size, DType}` + `dtype_of`/`sizeof_dtype` 让一次 `cudaMalloc` 能同时容纳混合精度的 21 个激活。
- `recompute` 用「反向重算前向」换显存：`=1` 把 `fch_gelu` 从 L 份缩成 1 份，`=2` 进一步让 `ln1`/`ln2` 不分配、复用 `lnf`，代价是反向多算。

## 7. 下一步学习建议

本讲建立了 CUDA 主线的**架构骨架**，但刻意回避了 kernel 内部细节。建议接下来：

1. **u5-l2 CUDA 工具层**：深入 [llmc/cuda_common.h](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_common.h) 与 [llmc/cuda_utils.cuh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_utils.cuh) 的 `Packed128`、warp/block 归约、stream 与 cuBLAS handle——理解本讲提到的 `x128`、`blockReduce` 是怎么用的。
2. **u5-l3 / u5-l4 / u5-l5**：分别看 matmul（cuBLASLt）、各层 header kernel、attention（手写 vs cuDNN），把本讲 include 进来的那些 `.cuh` 逐个打开。
3. **u6-l1 混合精度**：本讲埋下的 `master_weights`、`use_master_weights`、stochastic rounding 等 FP32 备份机制会在那里展开。
4. 继续阅读时，建议随手对照 [train_gpt2.cu:646-755](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L646-L755) 的 `gpt2_forward`，它是检验你「是否真看懂装配」的最佳试金石——能说清每一行调用的输入输出缓冲来自哪个 `acts.*`，就说明本讲过关了。
